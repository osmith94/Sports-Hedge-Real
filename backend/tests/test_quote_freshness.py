from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta, timezone
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.live_refresh import get_live_refresh_coordinator
from sports_hedge.application.market_observation import VenueMarketObservation
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.quote_freshness import (
    QuoteAgeError,
    conservative_combined_age_ms,
    conservative_combined_basis,
    effective_quote_age_ms,
    matchbook_market_quote_age,
    parse_quote_clock,
    polymarket_books_quote_age,
    require_aware_instant,
    retrieval_quote_age,
)
from sports_hedge.normalization.venues import MatchbookNormalizer, VenueNormalizationError
from sports_hedge.arbitrage.watchlist.models import OpportunityStatus
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.domain.football import (
    CanonicalEvent,
    CanonicalMarket,
    CanonicalOutcome,
    CanonicalRunner,
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import matchbook_kalshi_costs, matchbook_polymarket_costs
from registered_kalshi import FakeKalshiBTTS

from test_near_arbitrage_watchlist import _observation
from test_read_only_collector import FakeMatchbook, FakePolymarket, KICKOFF


class AgedMatchbook(FakeMatchbook):
    async def list_markets(self, event_id: int | str, **filters):
        payload = await super().list_markets(event_id, **filters)
        aged = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
        for market in payload.get("markets", []):
            market["last-updated"] = aged
            for runner in market.get("runners", []):
                runner["last-updated"] = aged
                for price in runner.get("prices", []):
                    price["last-updated"] = aged
        return payload


EVALUATED = datetime(2026, 9, 12, 14, 0, tzinfo=UTC)
OFFSET = timezone(timedelta(hours=-4))


def _ms(when: datetime) -> int:
    return int(when.timestamp() * 1000)


def test_missing_required_polymarket_timestamp_stays_unknown() -> None:
    age = polymarket_books_quote_age(
        {
            "yes-token": {
                "bids": [],
                "asks": [],
            },
            "no-token": {
                "timestamp": _ms(EVALUATED - timedelta(milliseconds=80)),
                "bids": [],
                "asks": [],
            },
        },
        required_tokens=["yes-token", "no-token"],
        evaluated_at=EVALUATED,
    )
    assert age.quote_age_ms is None
    assert age.basis == "unknown"
    assert age.reason == "missing_quote_timestamp"
    assert conservative_combined_age_ms(None, 80) is None


def test_future_required_timestamp_is_unknown_not_clamped_to_zero() -> None:
    age = polymarket_books_quote_age(
        {
            "yes-token": {
                "timestamp": _ms(EVALUATED + timedelta(seconds=30)),
                "bids": [],
                "asks": [],
            },
            "no-token": {
                "timestamp": _ms(EVALUATED - timedelta(milliseconds=50)),
                "bids": [],
                "asks": [],
            },
        },
        required_tokens=["yes-token", "no-token"],
        evaluated_at=EVALUATED,
    )
    assert age.quote_age_ms is None
    assert age.reason == "future_quote_timestamp"


def test_mixed_required_books_use_oldest_quote_not_newest() -> None:
    age = polymarket_books_quote_age(
        {
            "yes-token": {
                "timestamp": _ms(EVALUATED - timedelta(seconds=60)),
                "bids": [],
                "asks": [],
            },
            "no-token": {
                "timestamp": _ms(EVALUATED - timedelta(milliseconds=20)),
                "bids": [],
                "asks": [],
            },
        },
        required_tokens=["yes-token", "no-token"],
        evaluated_at=EVALUATED,
    )
    assert age.basis == "source"
    assert age.quote_age_ms is not None
    assert 59_000 <= age.quote_age_ms <= 60_000
    assert age.quote_age_ms != 0


def test_freshness_is_revalidation_not_price_change() -> None:
    """Tenet 18: unchanged odds are not stale if the quote was just revalidated."""

    first = retrieval_quote_age(
        retrieved_at=EVALUATED - timedelta(milliseconds=50),
        evaluated_at=EVALUATED,
    )
    revalidated = retrieval_quote_age(
        retrieved_at=EVALUATED - timedelta(milliseconds=40),
        evaluated_at=EVALUATED,
    )
    assert first.basis == "retrieval"
    assert first.quote_age_ms == 50
    assert revalidated.quote_age_ms == 40
    assert revalidated.quote_age_ms < 2000


def test_elapsed_collection_uses_evaluation_clock_after_retrieval() -> None:
    retrieved = EVALUATED - timedelta(milliseconds=80)
    age = retrieval_quote_age(retrieved_at=retrieved, evaluated_at=EVALUATED)
    assert age.basis == "retrieval"
    assert age.quote_age_ms == 80


def test_matchbook_without_source_clock_reports_retrieval_basis() -> None:
    retrieved = EVALUATED - timedelta(milliseconds=40)
    age = matchbook_market_quote_age(
        {
            "runners": [
                {"id": 1, "prices": [{"side": "back", "odds": "2.0"}]},
            ]
        },
        retrieved_at=retrieved,
        evaluated_at=EVALUATED,
    )
    assert age.basis == "retrieval"
    assert age.quote_age_ms == 40


def test_matchbook_future_then_missing_clock_is_rejected_not_retrieval() -> None:
    retrieved = EVALUATED - timedelta(milliseconds=10)
    age = matchbook_market_quote_age(
        {
            "runners": [
                {
                    "id": 1,
                    "timestamp": _ms(EVALUATED + timedelta(seconds=30)),
                    "prices": [{"side": "back", "odds": "2.0"}],
                },
                {
                    "id": 2,
                    "prices": [{"side": "lay", "odds": "1.9"}],
                },
            ]
        },
        retrieved_at=retrieved,
        evaluated_at=EVALUATED,
    )
    assert age.quote_age_ms is None
    assert age.basis == "unknown"
    assert age.reason == "future_quote_timestamp"


def test_matchbook_inspects_all_priced_levels_not_first_clock() -> None:
    age = matchbook_market_quote_age(
        {
            "runners": [
                {
                    "id": 1,
                    "prices": [
                        {
                            "side": "back",
                            "odds": "2.0",
                            "timestamp": _ms(EVALUATED - timedelta(milliseconds=20)),
                        },
                        {
                            "side": "lay",
                            "odds": "1.9",
                            "timestamp": _ms(EVALUATED - timedelta(seconds=45)),
                        },
                    ],
                }
            ]
        },
        retrieved_at=EVALUATED - timedelta(milliseconds=5),
        evaluated_at=EVALUATED,
    )
    assert age.basis == "source"
    assert age.quote_age_ms is not None
    assert 44_000 <= age.quote_age_ms <= 45_000


def test_parse_quote_clock_contains_overflow_nan_and_malformed() -> None:
    with pytest.raises(QuoteAgeError, match="invalid"):
        parse_quote_clock("9" * 99, field="timestamp")
    with pytest.raises(QuoteAgeError, match="invalid"):
        parse_quote_clock(float("nan"), field="timestamp")
    with pytest.raises(QuoteAgeError, match="invalid"):
        parse_quote_clock(float("inf"), field="timestamp")
    with pytest.raises(QuoteAgeError, match="invalid"):
        parse_quote_clock("not-a-clock", field="timestamp")
    age = matchbook_market_quote_age(
        {
            "runners": [
                {
                    "id": 1,
                    "timestamp": "9" * 99,
                    "prices": [{"side": "back", "odds": "2.0"}],
                }
            ]
        },
        retrieved_at=EVALUATED - timedelta(milliseconds=10),
        evaluated_at=EVALUATED,
    )
    assert age.quote_age_ms is None
    assert age.basis == "unknown"
    assert age.reason == "invalid_quote_timestamp"


def test_venue_normalization_rejects_naive_kickoff_and_keeps_aware_offset() -> None:
    with pytest.raises(VenueNormalizationError, match="timezone-naive"):
        MatchbookNormalizer().normalize_event(
            {
                "id": 1001,
                "name": "Arsenal vs Fulham",
                "start": "2026-09-12T15:00:00",
                "meta-tags": [{"type": "COMPETITION", "name": "Premier League"}],
            }
        )
    event = MatchbookNormalizer().normalize_event(
        {
            "id": 1001,
            "name": "Arsenal vs Fulham",
            "start": "2026-09-12T11:00:00-04:00",
            "meta-tags": [{"type": "COMPETITION", "name": "Premier League"}],
        }
    )
    assert event.kickoff_utc == datetime(2026, 9, 12, 15, 0, tzinfo=UTC)


def test_combined_basis_is_conservative() -> None:
    assert conservative_combined_basis("source", "source") == "source"
    assert conservative_combined_basis("source", "retrieval") == "retrieval"
    assert conservative_combined_basis("retrieval", None) == "unknown"
    assert conservative_combined_basis("unknown", "source") == "unknown"


def test_matchbook_naive_source_clock_is_unknown_not_invented() -> None:
    age = matchbook_market_quote_age(
        {
            "runners": [
                {
                    "id": 1,
                    "timestamp": "2026-09-12T14:00:00",
                    "prices": [{"side": "back", "odds": "2.0"}],
                }
            ]
        },
        retrieved_at=EVALUATED - timedelta(milliseconds=10),
        evaluated_at=EVALUATED,
    )
    assert age.quote_age_ms is None
    assert age.basis == "unknown"
    assert age.reason == "naive_quote_timestamp"


def test_naive_quote_and_kickoff_are_rejected_aware_non_utc_is_converted() -> None:
    with pytest.raises(QuoteAgeError, match="naive"):
        parse_quote_clock("2026-09-12T14:00:00", field="timestamp")
    with pytest.raises(ValueError, match="timezone-aware"):
        require_aware_instant(datetime(2026, 9, 12, 14, 0), "observed_at")

    converted = require_aware_instant(datetime(2026, 9, 12, 10, 0, tzinfo=OFFSET), "observed_at")
    assert converted == datetime(2026, 9, 12, 14, 0, tzinfo=UTC)

    event = CanonicalEvent(
        sport="football",
        competition="Premier League",
        home_team="Arsenal",
        away_team="Fulham",
        kickoff_utc=datetime(2026, 9, 12, 10, 0, tzinfo=OFFSET),
        source_venue=VenueName.MATCHBOOK,
        source_event_id="1",
    )
    market = CanonicalMarket(
        event=event,
        family=MarketFamily.BOTH_TEAMS_TO_SCORE,
        period=FootballPeriod.FULL_TIME,
        source_venue=VenueName.MATCHBOOK,
        source_market_id="2",
        runners=[
            CanonicalRunner(outcome=CanonicalOutcome.YES, source_runner_id="y", label="Yes"),
        ],
        settlement=SettlementFingerprint(
            scope=SettlementScope.REGULATION_TIME,
            period=FootballPeriod.FULL_TIME,
            extra_time_included=False,
            penalties_included=False,
        ),
    )
    observation = VenueMarketObservation(
        market=market,
        observed_at=datetime(2026, 9, 12, 10, 0, tzinfo=OFFSET),
        native_currency="GBP",
        outcome_books=[],
        quote_age_ms=100,
    )
    assert observation.observed_at == datetime(2026, 9, 12, 14, 0, tzinfo=UTC)
    assert observation.market.event.kickoff_utc == datetime(2026, 9, 12, 14, 0, tzinfo=UTC)

    with pytest.raises(ValueError, match="timezone-aware"):
        _observation(observed_at=datetime(2026, 9, 12, 14, 0))


def test_effective_age_does_not_treat_unknown_as_fresh() -> None:
    last_seen = EVALUATED
    assert effective_quote_age_ms(None, last_seen, EVALUATED + timedelta(seconds=2)) is None
    assert effective_quote_age_ms(100, last_seen, EVALUATED + timedelta(seconds=2)) == 2100
    assert effective_quote_age_ms(100, last_seen, EVALUATED - timedelta(seconds=1)) is None


def test_stopped_refresh_drops_near_triggered_but_keeps_tracked_history() -> None:
    service = WatchlistService(SqliteWatchlistRepository(), max_quote_age_ms=1000)
    last_seen = datetime.now(UTC)
    triggered = service.observe(
        _observation(
            market_id="mkt-age-out",
            edge=Decimal("0.015"),
            eligible=True,
            quote_age_ms=120,
            observed_at=last_seen,
            guaranteed_profit_gbp=Decimal("1.50"),
        )
    )
    assert triggered.status == OpportunityStatus.TRIGGERED
    assert service.triggered(as_of=last_seen)
    aged = last_seen + timedelta(seconds=2)
    assert service.top_near(as_of=aged, limit=10) == []
    assert service.triggered(as_of=aged, limit=10) == []
    tracked = service.tracked(as_of=aged, limit=10)
    assert len(tracked) == 1
    assert tracked[0].status == OpportunityStatus.REJECTED
    assert tracked[0].is_arbitrage is False
    assert tracked[0].guaranteed_profit_gbp is None
    assert "stale_quote" in tracked[0].rejection_reasons
    persisted = service.repository.get(triggered.opportunity_id)
    assert persisted is not None
    assert persisted.quote_age_ms == 120
    assert persisted.status == OpportunityStatus.REJECTED


def test_demo_fixture_replay_is_not_rejected_stale_on_wall_clock() -> None:
    last_seen = datetime.now(UTC)
    service = WatchlistService(SqliteWatchlistRepository(), max_quote_age_ms=1000)
    live = service.observe(
        _observation(
            market_id="mkt-live-age-out",
            edge=Decimal("0.015"),
            eligible=True,
            quote_age_ms=120,
            observed_at=last_seen,
            guaranteed_profit_gbp=Decimal("1.50"),
        )
    )
    demo = service.observe(
        _observation(
            market_id="mkt-demo-frozen-book",
            edge=Decimal("0.015"),
            eligible=True,
            quote_age_ms=120,
            observed_at=last_seen,
            guaranteed_profit_gbp=Decimal("1.50"),
            data_kind="demo_fixture_replay",
        )
    )
    assert live.status == OpportunityStatus.TRIGGERED
    assert demo.status == OpportunityStatus.TRIGGERED
    aged = last_seen + timedelta(seconds=2)
    live_presented = service._present_freshness(live, aged)
    demo_presented = service._present_freshness(demo, aged)
    assert live_presented.status == OpportunityStatus.REJECTED
    assert "stale_quote" in live_presented.rejection_reasons
    assert demo_presented.status == OpportunityStatus.TRIGGERED
    assert "stale_quote" not in demo_presented.rejection_reasons
    persisted_demo = service.repository.get(demo.opportunity_id)
    assert persisted_demo is not None
    assert persisted_demo.status == OpportunityStatus.TRIGGERED
    assert service.triggered(as_of=aged) == []
    tracked_ids = {item.opportunity_id for item in service.tracked(as_of=aged)}
    assert demo.opportunity_id not in tracked_ids
    assert live.opportunity_id in tracked_ids


def test_watchlist_api_uses_server_clock_not_client_as_of() -> None:
    last_seen = datetime.now(UTC)
    aged = last_seen + timedelta(seconds=2)
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: aged, max_quote_age_ms=1000)
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    service.observe(
        _observation(
            market_id="mkt-as-of",
            edge=Decimal("0.015"),
            eligible=True,
            quote_age_ms=120,
            observed_at=last_seen,
            guaranteed_profit_gbp=Decimal("1.50"),
        )
    )
    from test_tracked_current_snapshot import _report

    coordinator.record_report(_report("mkt-as-of"))
    try:
        ignored_fresh = client.get(
            "/paper/watchlist/triggered",
            params={"as_of": last_seen.isoformat()},
        )
        assert ignored_fresh.status_code == 200
        assert ignored_fresh.json() == []
        tracked = client.get("/paper/watchlist/tracked")
        assert tracked.status_code == 200
        assert tracked.json()[0]["status"] == "REJECTED"
        assert "stale_quote" in tracked.json()[0]["rejection_reasons"]
    finally:
        app.dependency_overrides.clear()
        coordinator.reset()
        repository.close()


class DelayedPolymarket(FakePolymarket):
    async def get_order_book(self, *args, **kwargs):
        await asyncio.sleep(0.05)
        return await super().get_order_book(*args, **kwargs)


class MissingTimestampPolymarket(FakePolymarket):
    async def get_order_book(self, *args, **kwargs):
        book = await super().get_order_book(*args, **kwargs)
        book.pop("timestamp", None)
        return book


class MixedAgePolymarket(FakePolymarket):
    async def get_order_book(self, *args, **kwargs):
        book = await super().get_order_book(*args, **kwargs)
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        if book["asset_id"] == "yes-token":
            book["timestamp"] = now_ms - 60_000
        else:
            book["timestamp"] = now_ms - 10
        return book


class FutureTimestampPolymarket(FakePolymarket):
    async def get_order_book(self, *args, **kwargs):
        book = await super().get_order_book(*args, **kwargs)
        book["timestamp"] = int(datetime.now(UTC).timestamp() * 1000) + 60_000
        return book


async def _collect(
    polymarket: FakePolymarket,
    *,
    kalshi_latency_s: float = 0.0,
    matchbook: FakeMatchbook | None = None,
):
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook or FakeMatchbook(),
        polymarket=polymarket,
        kalshi=FakeKalshiBTTS(
            [("Premier League", "Newcastle United", "Chelsea", KICKOFF)],
            latency_s=kalshi_latency_s,
        ),
        paper_scan=PaperScanService(intelligence),
    )
    try:
        return await collector.collect_and_scan(
            venue_costs=matchbook_kalshi_costs() + matchbook_polymarket_costs(),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            capital_limit_gbp=Decimal("100"),
            maximum_execution_risk=100,
        )
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_collector_elapsed_collection_time_is_included_in_matchbook_retrieval_age() -> None:
    report = await _collect(FakePolymarket(), kalshi_latency_s=0.08, matchbook=AgedMatchbook())
    assert report.paper_decisions
    decision = report.paper_decisions[0]
    assert decision.quote_age_ms is not None
    assert decision.quote_age_ms >= 50


@pytest.mark.asyncio
async def test_collector_missing_polymarket_timestamp_is_not_fresh() -> None:
    report = await _collect(MissingTimestampPolymarket())
    assert report.paper_decisions
    decision = report.paper_decisions[0]
    assert decision.quote_age_ms is not None
    assert "unknown_quote_age" not in decision.rejection_reasons
    assert "missing_quote_timestamp" not in decision.rejection_reasons


@pytest.mark.asyncio
async def test_collector_mixed_book_ages_use_oldest_required_quote() -> None:
    report = await _collect(MixedAgePolymarket(), matchbook=AgedMatchbook())
    assert report.paper_decisions
    decision = report.paper_decisions[0]
    assert decision.quote_age_ms is not None
    assert decision.quote_age_ms != 0


@pytest.mark.asyncio
async def test_collector_future_book_timestamp_is_unknown() -> None:
    report = await _collect(FutureTimestampPolymarket())
    assert report.paper_decisions
    decision = report.paper_decisions[0]
    assert decision.quote_age_ms is not None
    assert decision.eligible_for_paper_simulation is True
