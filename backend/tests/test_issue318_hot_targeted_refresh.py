"""Issue #318: HOT refreshes persisted ApprovedEquivalent markets, not rediscovery.

UNIVERSE proves MATCHED_EQUIVALENT and persists source identity. HOT reads that
identity and refreshes only current quote/depth. Deterministic fixture/demo
providers. Not live, historical, or modelled venue quotes. Paper-only.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from sports_hedge.application.collector import (
    MarketEvaluationState,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.current_market_inventory import canonical_current_market_key
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.fixture_inventory import InventoryComparisonStatus
from sports_hedge.application.hot_market_relationships import (
    HOT_RELATIONSHIP_MISSING_REASON,
    HOT_REVALIDATION_NEEDED_REASON,
    HotMarketRelationship,
    HotVenueLeg,
    relationships_from_fixture_markets,
)
from sports_hedge.application.live_refresh import DualCadencePlan, LiveRefreshCoordinator
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.provider_access import ProviderAccessLayer
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.venues.matchbook import MatchbookMarketGoneError
from test_current_market_inventory import NOW, _fixture, _market_row, _report
from test_dual_cadence_scheduler import FakeClock
from venue_cost_helpers import matchbook_polymarket_costs, profit_commission_cost


KICKOFF = datetime(2026, 9, 16, 18, 30, tzinfo=UTC)
REGULATION = "Resolves based on 90 minutes of regulation time."
EPL_SERIES = {
    "ticker": "KXEPLGAME",
    "title": "Premier League",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "settlement_sources": [{"name": "Opta"}],
}
SIX_FIXTURES = [
    ("Arsenal", "Chelsea"),
    ("Liverpool", "Everton"),
    ("Newcastle United", "Tottenham Hotspur"),
    ("Manchester City", "Brighton"),
    ("Aston Villa", "West Ham"),
    ("Fulham", "Brentford"),
]


def _costs():
    captured = datetime.now(UTC)
    return [
        *matchbook_polymarket_costs("0.02", "0.02", captured_at=captured),
        kalshi_cost_from_series(EPL_SERIES, captured_at=captured),
        profit_commission_cost(VenueName.KALSHI, "0"),
    ]


def _fx():
    return [FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test")]


def _btts_matchbook(market_id: int, *, odds: str = "2.20") -> dict[str, Any]:
    return {
        "id": market_id,
        "name": "Both Teams To Score",
        "status": "open",
        "runners": [
            {
                "id": market_id * 10 + 1,
                "name": "Yes",
                "prices": [
                    {"side": "back", "odds": odds, "available-amount": "100"},
                    {"side": "lay", "odds": "2.22", "available-amount": "100"},
                ],
            },
            {
                "id": market_id * 10 + 2,
                "name": "No",
                "prices": [
                    {"side": "back", "odds": "1.80", "available-amount": "100"},
                    {"side": "lay", "odds": "1.82", "available-amount": "100"},
                ],
            },
        ],
    }


def _kalshi_event(index: int, home: str, away: str) -> dict[str, Any]:
    ticker = f"KXEPLGAME-{index:02d}"
    return {
        "event_ticker": ticker,
        "series_ticker": "KXEPLGAME",
        "title": f"{home} vs {away}",
        "category": "Sports",
        "strike_date": KICKOFF.isoformat(),
        "competition": "Premier League",
        "fee_type_override": "quadratic",
        "fee_multiplier_override": 1,
        "markets": [
            {
                "ticker": f"{ticker}-BTTS",
                "event_ticker": ticker,
                "title": "Both Teams To Score",
                "yes_sub_title": "Yes",
                "rules_primary": REGULATION,
            }
        ],
    }


class SixMatchbook:
    def __init__(self) -> None:
        self.list_events_calls = 0
        self.list_markets_calls: list[str] = []
        self.get_market_calls: list[tuple[str, str]] = []
        self.gone_ids: set[str] = set()
        self.timeout_ids: set[str] = set()
        self.odds_by_event: dict[str, str] = {}

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_events_calls += 1
        return {
            "events": [
                {
                    "id": 8800 + index,
                    "name": f"{home} vs {away}",
                    "start": KICKOFF.isoformat(),
                    "sport-name": "Football",
                    "competition-name": "Premier League",
                    "status": "open",
                }
                for index, (home, away) in enumerate(SIX_FIXTURES)
            ]
        }

    def _market(self, event_id: int | str) -> dict[str, Any]:
        numeric = int(event_id)
        odds = self.odds_by_event.get(str(event_id), "2.20")
        return _btts_matchbook(numeric, odds=odds)

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_markets_calls.append(str(event_id))
        return {"markets": [self._market(event_id), _extra_matchbook_market(event_id)]}

    async def get_market(
        self,
        event_id: int | str,
        market_id: int | str,
        **filters: Any,
    ) -> dict[str, Any]:
        del filters
        self.get_market_calls.append((str(event_id), str(market_id)))
        if str(market_id) in self.gone_ids or str(event_id) in self.gone_ids:
            raise MatchbookMarketGoneError(event_id, market_id, 404)
        if str(market_id) in self.timeout_ids or str(event_id) in self.timeout_ids:
            await asyncio.sleep(30)
        market = self._market(event_id)
        if str(market["id"]) != str(market_id):
            raise MatchbookMarketGoneError(event_id, market_id, 404)
        return market


def _extra_matchbook_market(event_id: int | str) -> dict[str, Any]:
    return {
        "id": int(event_id) + 5000,
        "name": "Correct Score",
        "runners": [{"id": 1, "name": "1-0", "prices": []}],
    }


class SixKalshi:
    def __init__(self) -> None:
        self.list_events_calls = 0
        self.list_markets_calls: list[str] = []
        self.get_series_calls: list[str] = []
        self.get_market_calls: list[str] = []
        self.book_calls: list[str] = []

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_events_calls += 1
        return {
            "events": [
                _kalshi_event(index, home, away)
                for index, (home, away) in enumerate(SIX_FIXTURES)
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_markets_calls.append(str(event_id))
        return {"markets": []}

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        self.get_series_calls.append(str(series_ticker))
        return dict(EPL_SERIES)

    async def get_market(self, ticker: str) -> dict[str, Any]:
        self.get_market_calls.append(str(ticker))
        return {"ticker": ticker, "title": "Both Teams To Score", "rules_primary": REGULATION}

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, outcome_id, filters
        self.book_calls.append(str(market_id))
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "100.00"]],
                "no_dollars": [["0.49", "200.00"]],
            }
        }


class EmptyPolymarket:
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return []

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return []

    async def get_order_book(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {"asset_id": "x", "bids": [], "asks": []}


class SlowUniverseMatchbook(SixMatchbook):
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        await asyncio.sleep(0.4)
        return await super().list_events(**filters)


def _collector(matchbook, kalshi) -> tuple[ReadOnlyCrossVenueCollector, SqliteMarketIntelligenceRepository]:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=EmptyPolymarket(),
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    return collector, repository


def _clear_provider_call_logs(matchbook: SixMatchbook, kalshi: SixKalshi) -> None:
    """Drop UNIVERSE discovery counts so later asserts measure HOT-only work."""

    matchbook.list_events_calls = 0
    matchbook.list_markets_calls.clear()
    matchbook.get_market_calls.clear()
    kalshi.list_events_calls = 0
    kalshi.list_markets_calls.clear()
    kalshi.get_series_calls.clear()
    kalshi.get_market_calls.clear()
    kalshi.book_calls.clear()


async def _universe_then_hot(
    matchbook: SixMatchbook,
    kalshi: SixKalshi,
    *,
    hot_kwargs: dict[str, Any] | None = None,
):
    collector, repository = _collector(matchbook, kalshi)
    try:
        universe = await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            scan_lane=ScanLane.UNIVERSE.value,
        )
        clustered = [
            item
            for item in universe.discovered_fixtures
            if item.matchbook_matched and item.kalshi_matched
        ]
        relationships = relationships_from_fixture_markets(universe.fixture_markets)
        _clear_provider_call_logs(matchbook, kalshi)
        hot = await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            scan_lane=ScanLane.HOT.value,
            identity_scope=[item.canonical_event_id for item in clustered],
            known_source_events=universe.fixture_source_events,
            hot_market_relationships=relationships,
            **(hot_kwargs or {}),
        )
        return universe, hot, clustered, relationships
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_hot_six_approved_equivalents_use_direct_reads_not_discovery() -> None:
    matchbook = SixMatchbook()
    kalshi = SixKalshi()
    universe, hot, clustered, relationships = await _universe_then_hot(matchbook, kalshi)

    assert len(clustered) == 6
    assert sum(len(rows) for rows in relationships.values()) == 6
    assert matchbook.list_markets_calls == []
    assert kalshi.list_markets_calls == []
    assert kalshi.get_series_calls == []
    assert kalshi.get_market_calls == []
    assert len(matchbook.get_market_calls) == 6
    assert len(kalshi.book_calls) == 6
    assert all(
        item.market_evaluation_state == MarketEvaluationState.EVALUATED.value
        for item in hot.discovered_fixtures
        if item.canonical_event_id in {row.canonical_event_id for row in clustered}
    )
    assert hot.scan_diagnostics["hot_targeted_refresh"]["matchbook_get_market"] == 6
    assert hot.scan_diagnostics["hot_targeted_refresh"]["kalshi_order_books"] == 6


@pytest.mark.asyncio
async def test_hot_provider_calls_follow_persisted_relationships_not_event_catalogue() -> None:
    matchbook = SixMatchbook()
    kalshi = SixKalshi()
    _universe, _hot, _clustered, relationships = await _universe_then_hot(matchbook, kalshi)
    persisted = sum(len(rows) for rows in relationships.values())
    assert persisted == 6
    assert len(matchbook.get_market_calls) == persisted
    assert len(kalshi.book_calls) == persisted
    assert all(call[1] == call[0] for call in matchbook.get_market_calls)
    extra_ids = {str(8800 + index + 5000) for index in range(6)}
    assert extra_ids.isdisjoint({call[1] for call in matchbook.get_market_calls})


@pytest.mark.asyncio
async def test_hot_refresh_updates_quotes_without_deleting_other_universe_rows() -> None:
    matchbook = SixMatchbook()
    kalshi = SixKalshi()
    collector, repository = _collector(matchbook, kalshi)
    try:
        universe = await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            scan_lane=ScanLane.UNIVERSE.value,
        )
        fixture = next(
            item
            for item in universe.discovered_fixtures
            if item.matchbook_matched and item.kalshi_matched
        )
        store = FixtureCurrentStateStore()
        extra = _market_row(family="match_result", edge=Decimal("-0.004"))
        approved = next(
            row
            for row in universe.fixture_markets[fixture.canonical_event_id]
            if row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT
        )
        _clear_provider_call_logs(matchbook, kalshi)
        stamped = universe.model_copy(
            update={
                "fixture_markets": {
                    fixture.canonical_event_id: [approved, extra],
                }
            }
        )
        store.upsert_from_report(stamped, scan_lane=ScanLane.UNIVERSE, now=NOW)
        before_keys = set(store._rows[fixture.canonical_event_id].markets or {})
        matchbook.odds_by_event[str(fixture.source_event_id)] = "2.55"
        hot = await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            scan_lane=ScanLane.HOT.value,
            identity_scope=[fixture.canonical_event_id],
            known_source_events=universe.fixture_source_events,
            hot_market_relationships=relationships_from_fixture_markets(
                {fixture.canonical_event_id: [approved]}
            ),
        )
        store.upsert_from_report(hot, scan_lane=ScanLane.HOT, now=NOW + timedelta(seconds=5))
        after = store._rows[fixture.canonical_event_id].markets or {}
        assert canonical_current_market_key(extra) in after
        assert canonical_current_market_key(approved) in after
        assert set(after) >= before_keys
        refreshed = after[canonical_current_market_key(approved)].row
        assert refreshed.matchbook is not None
        backs = [quote.decimal_odds for quote in refreshed.matchbook.best_backs]
        assert Decimal("2.55") in backs
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_lifecycle_hot_without_approved_relationship_does_not_discover_markets() -> None:
    matchbook = SixMatchbook()
    kalshi = SixKalshi()
    collector, repository = _collector(matchbook, kalshi)
    try:
        universe = await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            scan_lane=ScanLane.UNIVERSE.value,
        )
        fixture = next(item for item in universe.discovered_fixtures if item.matchbook_matched)
        _clear_provider_call_logs(matchbook, kalshi)
        hot = await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            scan_lane=ScanLane.HOT.value,
            identity_scope=[fixture.canonical_event_id],
            known_source_events=universe.fixture_source_events,
            hot_market_relationships={},
        )
        assert matchbook.list_markets_calls == []
        assert kalshi.list_markets_calls == []
        assert kalshi.get_series_calls == []
        assert kalshi.get_market_calls == []
        assert matchbook.get_market_calls == []
        assert kalshi.book_calls == []
        target = next(
            item for item in hot.discovered_fixtures if item.canonical_event_id == fixture.canonical_event_id
        )
        assert target.market_evaluation_state == MarketEvaluationState.HOT_RELATIONSHIP_MISSING.value
        assert target.market_evaluation_reason == HOT_RELATIONSHIP_MISSING_REASON
        assert hot.scan_diagnostics["hot_targeted_refresh"]["missing"] >= 1
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_known_market_404_fails_closed_and_requests_universe_revalidation() -> None:
    matchbook = SixMatchbook()
    kalshi = SixKalshi()
    collector, repository = _collector(matchbook, kalshi)
    try:
        universe = await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            scan_lane=ScanLane.UNIVERSE.value,
        )
        fixture = next(
            item
            for item in universe.discovered_fixtures
            if item.matchbook_matched and item.kalshi_matched
        )
        relationships = relationships_from_fixture_markets(
            {fixture.canonical_event_id: universe.fixture_markets[fixture.canonical_event_id]}
        )
        matchbook.gone_ids.add(str(fixture.source_event_id))
        _clear_provider_call_logs(matchbook, kalshi)
        hot = await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            scan_lane=ScanLane.HOT.value,
            identity_scope=[fixture.canonical_event_id],
            known_source_events=universe.fixture_source_events,
            hot_market_relationships=relationships,
        )
        assert matchbook.list_markets_calls == []
        assert kalshi.list_markets_calls == []
        target = next(
            item for item in hot.discovered_fixtures if item.canonical_event_id == fixture.canonical_event_id
        )
        assert target.market_evaluation_state == MarketEvaluationState.EVALUATED.value
        rows = hot.fixture_markets[fixture.canonical_event_id]
        assert any(row.reason == HOT_REVALIDATION_NEEDED_REASON for row in rows)
        assert all(
            row.comparison_status is not InventoryComparisonStatus.MATCHED_EQUIVALENT for row in rows
        )
        assert any(
            issue.detail.startswith(HOT_REVALIDATION_NEEDED_REASON) for issue in hot.issues
        )
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_hot_direct_refresh_completes_while_universe_is_running() -> None:
    matchbook = SixMatchbook()
    kalshi = SixKalshi()
    collector, repository = _collector(matchbook, kalshi)
    access = ProviderAccessLayer()
    collector._provider_access = access
    try:
        universe = await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            scan_lane=ScanLane.UNIVERSE.value,
        )
        clustered = [
            item
            for item in universe.discovered_fixtures
            if item.matchbook_matched and item.kalshi_matched
        ]
        relationships = relationships_from_fixture_markets(universe.fixture_markets)
        _clear_provider_call_logs(matchbook, kalshi)
        slow = SlowUniverseMatchbook()
        universe_collector, universe_repo = _collector(slow, SixKalshi())
        universe_collector._provider_access = access
        universe_task = asyncio.create_task(
            universe_collector.collect_and_scan(
                venue_costs=_costs(),
                fx_snapshots=_fx(),
                scan_lane=ScanLane.UNIVERSE.value,
            )
        )
        await asyncio.sleep(0.05)
        started = datetime.now(UTC)
        hot = await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            scan_lane=ScanLane.HOT.value,
            identity_scope=[item.canonical_event_id for item in clustered],
            known_source_events=universe.fixture_source_events,
            hot_market_relationships=relationships,
        )
        elapsed = (datetime.now(UTC) - started).total_seconds()
        assert elapsed < 2.0
        assert hot.scan_diagnostics["hot_targeted_refresh"]["matchbook_get_market"] == 6
        assert not universe_task.done()
        universe_task.cancel()
        try:
            await universe_task
        except asyncio.CancelledError:
            pass
        universe_repo.close()
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_direct_quote_timeout_stays_on_that_relationship_without_discovery() -> None:
    matchbook = SixMatchbook()
    kalshi = SixKalshi()
    collector, repository = _collector(matchbook, kalshi)
    try:
        universe = await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            scan_lane=ScanLane.UNIVERSE.value,
        )
        clustered = [
            item
            for item in universe.discovered_fixtures
            if item.matchbook_matched and item.kalshi_matched
        ]
        timed_out = clustered[0]
        matchbook.timeout_ids.add(str(timed_out.source_event_id))
        _clear_provider_call_logs(matchbook, kalshi)
        hot = await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            scan_lane=ScanLane.HOT.value,
            identity_scope=[item.canonical_event_id for item in clustered],
            known_source_events=universe.fixture_source_events,
            hot_market_relationships=relationships_from_fixture_markets(universe.fixture_markets),
            provider_call_timeout_seconds=0.2,
            cycle_timeout_seconds=8.0,
        )
        assert matchbook.list_markets_calls == []
        assert kalshi.list_markets_calls == []
        evaluated = [
            item
            for item in hot.discovered_fixtures
            if item.market_evaluation_state == MarketEvaluationState.EVALUATED.value
        ]
        assert len(evaluated) >= 5
        timed = next(
            item
            for item in hot.discovered_fixtures
            if item.canonical_event_id == timed_out.canonical_event_id
        )
        assert timed.market_evaluation_state in {
            MarketEvaluationState.MARKET_FETCH_UNAVAILABLE.value,
            MarketEvaluationState.EVALUATED.value,
        }
        assert matchbook.get_market_calls
    finally:
        repository.close()


def test_hot_plan_carries_persisted_approved_relationships() -> None:
    store_row = _market_row(family="both_teams_to_score", edge=Decimal("0.004"))
    fixture = _fixture(equivalent=1, when=NOW, opportunity="matched")
    fixture = fixture.model_copy(update={"in_running": True})
    report = _report(
        fixture=fixture,
        markets=[store_row],
        market_ids=["mkt-btts"],
        lane=ScanLane.UNIVERSE,
        when=NOW,
    )
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator.record_report(report, scan_lane=ScanLane.UNIVERSE)
    coordinator._next_hot_due = NOW
    plan = coordinator.plan_hot_tick(now=NOW)
    assert plan.lane == ScanLane.HOT.value
    assert isinstance(plan, DualCadencePlan)
    assert plan.hot_market_relationships
    carried = next(iter(plan.hot_market_relationships.values()))[0]
    assert carried.proof_status == InventoryComparisonStatus.MATCHED_EQUIVALENT.value
    assert carried.matchbook is not None
    assert carried.kalshi is not None
    facade = coordinator.hot_market_relationships([fixture.canonical_event_id])
    assert facade


def test_store_hot_market_relationships_skips_absent_and_non_equivalent() -> None:
    store = FixtureCurrentStateStore()
    equivalent = _market_row(family="both_teams_to_score")
    other = _market_row(
        family="match_result",
        status=InventoryComparisonStatus.OTHER,
        reason="not_equivalent",
        edge=None,
    )
    fixture = _fixture(equivalent=1, when=NOW)
    store.upsert_from_report(
        _report(fixture=fixture, markets=[equivalent, other], market_ids=["btts"], lane=ScanLane.UNIVERSE, when=NOW),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    payload = store.hot_market_relationships([fixture.canonical_event_id], now=NOW)
    assert list(payload) == [fixture.canonical_event_id]
    assert len(payload[fixture.canonical_event_id]) == 1
    assert payload[fixture.canonical_event_id][0].family == "both_teams_to_score"
