"""Issue #14: Opportunity Monitor clocks and Kalshi-off coverage copy.

Data class: deterministic fixture/demo current-state and inventory rows.
Not live venue quotes. Execution stays disabled. No provider calls.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from fastapi.testclient import TestClient
from test_issue200_universe_hot_promotion import (
    NOW,
    _decision,
    _fixture,
    _market_row,
    _report,
)

from sports_hedge.api.main import app
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.application.fixture_inventory import (
    FixtureMarketInventoryRow,
    InventoryComparisonStatus,
    VenueMarketFacts,
    VenueQuoteFact,
)
from sports_hedge.application.current_market_inventory import slot_freshness
from sports_hedge.application.live_refresh import get_live_refresh_coordinator
from sports_hedge.application.scan_lanes import (
    DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS,
    ScanLane,
)
from sports_hedge.arbitrage.watchlist.models import WatchLeg, WatchObservation
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.catalogue.coverage_rows import fixture_catalogue_coverage
from sports_hedge.catalogue.registry import CatalogueCoverageState
from sports_hedge.config import Settings
from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName

EVENT = "issue14-dolphins-bengals"
T0 = NOW
T1 = NOW + timedelta(minutes=1)
T2 = NOW + timedelta(minutes=2)
T3 = NOW + timedelta(minutes=3)


def _named_row(family: str, matchbook_id: str, polymarket_id: str) -> FixtureMarketInventoryRow:
    row = _market_row(edge=Decimal(0), arb=False, trigger=Decimal("0.01"))
    assert row.matchbook is not None and row.polymarket is not None
    return row.model_copy(
        update={
            "family": family,
            "display_name": family,
            "matchbook": row.matchbook.model_copy(
                update={"source_market_id": matchbook_id, "family": family}
            ),
            "polymarket": row.polymarket.model_copy(
                update={"source_market_id": polymarket_id, "family": family}
            ),
        }
    )


def _observation(market_id: str, sources: tuple[str, str], when) -> WatchObservation:
    return WatchObservation(
        observed_at=when,
        canonical_event_id=EVENT,
        canonical_market_id=market_id,
        competition="NFL",
        home_team="Miami Dolphins",
        away_team="Cincinnati Bengals",
        market_family=MarketFamily.POINT_SPREAD,
        period=FootballPeriod.FULL_TIME,
        venues=[VenueName.MATCHBOOK, VenueName.POLYMARKET],
        legs=[
            WatchLeg(
                outcome="home",
                venue=VenueName.MATCHBOOK,
                source_market_id=sources[0],
                currency="GBP",
                net_decimal_odds=Decimal("2.10"),
            ),
            WatchLeg(
                outcome="away",
                venue=VenueName.POLYMARKET,
                source_market_id=sources[1],
                currency="USD",
                net_decimal_odds=Decimal("2.05"),
            ),
        ],
        trigger_net_edge=Decimal("0.01"),
        current_net_edge=Decimal("0.002"),
        quote_age_ms=120,
        quote_age_basis="source",
        data_kind="live_paper",
    )


def _publish(
    store,
    rows,
    decisions,
    when,
    *,
    pricing_refresh: bool,
    fixture_status: str | None = None,
    scan_lane: ScanLane = ScanLane.UNIVERSE,
):
    fixture = _fixture(EVENT, when=when, opportunity="near", arb=False, qualifying=0)
    if fixture_status is not None:
        fixture = fixture.model_copy(update={"fixture_status": fixture_status})
    report = _report(
        [fixture],
        when=when,
        markets={EVENT: rows},
        decisions=decisions,
    )
    store.upsert_from_report(
        report,
        scan_lane=scan_lane,
        now=when,
        pricing_refresh=pricing_refresh,
    )


def test_background_price_clock_advances_without_moving_discovery_or_economics() -> None:
    """Prove the frozen Age: radar last_scanned_at stays on the UNIVERSE lane."""

    assert Settings().sports_hedge_execution_enabled is False
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    repository = SqliteWatchlistRepository()
    clock = {"at": T0}
    service = WatchlistService(repository, clock=lambda: clock["at"])
    store = coordinator.fixture_current_state()
    row_a = _named_row("both_teams_to_score", "mb-a", "pm-a")
    row_b = _named_row("match_result", "mb-b", "pm-b")
    decisions = [
        _decision(EVENT, "mkt-a", when=T0),
        _decision(EVENT, "mkt-b", when=T0),
    ]
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    try:
        _publish(store, [row_a, row_b], decisions, T0, pricing_refresh=False)
        service.observe(_observation("mkt-a", ("mb-a", "pm-a"), T0))
        service.observe(_observation("mkt-b", ("mb-b", "pm-b"), T0))
        record = store._rows[EVENT]
        assert record.universe is not None
        assert record.universe.last_scanned_at == T0

        before = {row["canonical_market_id"]: row for row in client.get("/paper/watchlist/tracked").json()}
        assert before["mkt-a"]["last_scanned_at"].startswith("2026-09-16T12:00:00")
        assert before["mkt-a"]["last_discovered_at"].startswith("2026-09-16T12:00:00")
        assert before["mkt-a"]["last_priced_at"] is None
        assert before["mkt-a"]["price_lane"] is None
        assert before["mkt-a"]["last_seen_at"].startswith("2026-09-16T12:00:00")
        assert before["mkt-b"]["last_priced_at"] is None

        _publish(store, [row_a], [_decision(EVENT, "mkt-a", when=T1)], T1, pricing_refresh=True)
        assert record.universe.last_scanned_at == T0
        slot_a = next(
            slot
            for slot in record.markets.values()
            if slot.row.matchbook is not None and slot.row.matchbook.source_market_id == "mb-a"
        )
        assert slot_a.background_priced_at == T1
        clock_a = store.market_price_clock("mkt-a", T1, source_market_ids=("mb-a", "pm-a"))
        clock_b = store.market_price_clock("mkt-b", T1, source_market_ids=("mb-b", "pm-b"))
        assert clock_a.discovered_at == T0
        assert clock_a.priced_at == T1
        assert clock_a.price_lane == "background"
        assert clock_b.priced_at is None
        assert clock_b.discovered_at == T0
        radar = store.radar_meta_for_market("mkt-a", T1)
        assert radar is not None
        assert radar.last_scanned_at == T0

        clock["at"] = T1
        lagged = {row["canonical_market_id"]: row for row in client.get("/paper/watchlist/tracked").json()}
        assert lagged["mkt-a"]["last_priced_at"].startswith("2026-09-16T12:01:00")
        assert lagged["mkt-a"]["last_discovered_at"].startswith("2026-09-16T12:00:00")
        assert lagged["mkt-a"]["last_scanned_at"].startswith("2026-09-16T12:00:00")
        assert lagged["mkt-a"]["last_seen_at"].startswith("2026-09-16T12:00:00")
        assert lagged["mkt-a"]["price_lane"] == "background"
        assert lagged["mkt-a"]["quote_age_ms"] < 120_000
        assert lagged["mkt-b"]["last_priced_at"] is None
        assert lagged["mkt-b"]["last_seen_at"].startswith("2026-09-16T12:00:00")

        service.observe(_observation("mkt-a", ("mb-a", "pm-a"), T1))
        priced = {row["canonical_market_id"]: row for row in client.get("/paper/watchlist/tracked").json()}
        assert priced["mkt-a"]["last_seen_at"].startswith("2026-09-16T12:01:00")
        assert priced["mkt-a"]["current_net_edge"] is not None

        _publish(store, [row_a], [_decision(EVENT, "mkt-a", when=T2)], T2, pricing_refresh=True)
        service.observe(_observation("mkt-a", ("mb-a", "pm-a"), T2))
        clock["at"] = T2
        second = {row["canonical_market_id"]: row for row in client.get("/paper/watchlist/tracked").json()}
        assert second["mkt-a"]["last_priced_at"].startswith("2026-09-16T12:02:00")
        assert second["mkt-a"]["last_discovered_at"].startswith("2026-09-16T12:00:00")
        assert second["mkt-b"]["last_priced_at"] is None
        assert second["mkt-a"]["quote_age_ms"] < 5_000

        _publish(
            store,
            [row_a, row_b],
            [_decision(EVENT, "mkt-a", when=T3), _decision(EVENT, "mkt-b", when=T3)],
            T3,
            pricing_refresh=False,
        )
        clock["at"] = T3
        confirmed = {row["canonical_market_id"]: row for row in client.get("/paper/watchlist/tracked").json()}
        assert confirmed["mkt-a"]["last_discovered_at"].startswith("2026-09-16T12:03:00")
        assert confirmed["mkt-a"]["last_priced_at"].startswith("2026-09-16T12:02:00")
        priced_slot = next(
            slot
            for slot in record.markets.values()
            if slot.row.matchbook is not None and slot.row.matchbook.source_market_id == "mb-a"
        )
        assert priced_slot.background_priced_at == T2

        late = T2 + timedelta(seconds=DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS - 60)
        clock["at"] = late
        retained = {row["canonical_market_id"]: row for row in client.get("/paper/watchlist/tracked").json()}
        assert retained["mkt-a"]["last_priced_at"].startswith("2026-09-16T12:02:00")
        assert retained["mkt-a"]["freshness_class"] == "radar_current"
        assert retained["mkt-a"]["quote_age_ms"] > 60_000
        assert retained["mkt-a"]["bet_actionable"] is False

        _publish(
            store,
            [row_a],
            [_decision(EVENT, "mkt-a", when=late)],
            late,
            pricing_refresh=True,
            fixture_status="completed",
        )
        assert client.get("/paper/watchlist/tracked").json() == []
    finally:
        app.dependency_overrides.clear()
        coordinator.reset()
        repository.close()


def test_price_slot_refresh_does_not_make_stale_economics_executable() -> None:
    """A new provider quote on the price slot must not relabel an old edge.

    The slot carries an 80ms source quote at the price instant, so slot
    freshness is executable. The watchlist edge was observed earlier and its
    provider quote has aged past the paper-entry limit. Those clocks stay
    separate, and the tracked row stays non-executable.
    """

    assert Settings().sports_hedge_execution_enabled is False
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    repository = SqliteWatchlistRepository()
    clock = {"at": T0}
    service = WatchlistService(repository, clock=lambda: clock["at"], max_quote_age_ms=2_000)
    store = coordinator.fixture_current_state()
    fresh_quote = _named_row("both_teams_to_score", "mb-a", "pm-a")
    unknown_row = _named_row("match_result", "mb-b", "pm-b")
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    provider_calls: list[str] = []

    async def _forbid(self, *args, **kwargs):
        provider_calls.append(type(self).__name__)
        raise AssertionError("tracked read must not call a venue")

    try:
        _publish(store, [fresh_quote, unknown_row], [
            _decision(EVENT, "mkt-a", when=T0),
            _decision(EVENT, "mkt-b", when=T0),
        ], T0, pricing_refresh=False)
        service.observe(
            _observation("mkt-a", ("mb-a", "pm-a"), T0).model_copy(
                update={"quote_age_ms": 1_500, "current_net_edge": Decimal("0.018")}
            )
        )
        service.observe(
            _observation("mkt-b", ("mb-b", "pm-b"), T0).model_copy(
                update={"quote_age_ms": None, "quote_age_basis": None, "current_net_edge": Decimal("0.011")}
            )
        )
        with patch("sports_hedge.venues.kalshi.KalshiClient.list_events", _forbid), patch(
            "sports_hedge.venues.kalshi.KalshiClient.list_markets", _forbid
        ), patch("sports_hedge.venues.kalshi.KalshiClient.get_market", _forbid), patch(
            "sports_hedge.venues.kalshi.KalshiClient.get_order_book", _forbid
        ), patch("sports_hedge.venues.matchbook.MatchbookClient.list_events", _forbid), patch(
            "sports_hedge.venues.matchbook.MatchbookClient.get_order_book", _forbid
        ), patch("sports_hedge.venues.polymarket.PolymarketClient.list_events", _forbid), patch(
            "sports_hedge.venues.polymarket.PolymarketClient.get_order_book", _forbid
        ), patch(
            "sports_hedge.application.price_engine.CataloguePriceEngine.run_slice", _forbid
        ):
            opened = {row["canonical_market_id"]: row for row in client.get("/paper/watchlist/tracked").json()}
        assert provider_calls == []
        assert opened["mkt-a"]["freshness_class"] == "executable"
        assert opened["mkt-a"]["quote_age_ms"] == 1_500
        assert opened["mkt-b"]["quote_age_ms"] is None
        assert opened["mkt-b"]["freshness_class"] == "radar_current"
        assert opened["mkt-b"]["bet_actionable"] is False
        assert opened["mkt-b"]["last_priced_at"] is None

        priced_at = T0 + timedelta(seconds=45)
        _publish(
            store,
            [fresh_quote],
            [_decision(EVENT, "mkt-a", when=priced_at)],
            priced_at,
            pricing_refresh=True,
        )
        clock["at"] = priced_at
        slot = next(
            item
            for item in store._rows[EVENT].markets.values()
            if item.row.matchbook is not None and item.row.matchbook.source_market_id == "mb-a"
        )
        assert slot.row.matchbook is not None
        assert slot.row.matchbook.quote_age_ms == 80
        assert slot_freshness(slot, now=priced_at) == "executable"
        with patch("sports_hedge.venues.kalshi.KalshiClient.get_order_book", _forbid), patch(
            "sports_hedge.application.price_engine.CataloguePriceEngine.run_slice", _forbid
        ):
            lagged = {row["canonical_market_id"]: row for row in client.get("/paper/watchlist/tracked").json()}
        assert provider_calls == []
        aged = lagged["mkt-a"]
        assert aged["last_priced_at"].startswith("2026-09-16T12:00:45")
        assert aged["last_discovered_at"].startswith("2026-09-16T12:00:00")
        assert aged["last_scanned_at"].startswith("2026-09-16T12:00:00")
        assert aged["last_seen_at"].startswith("2026-09-16T12:00:00")
        assert Decimal(str(aged["current_net_edge"])) == Decimal("0.018")
        assert aged["quote_age_basis"] == "source"
        assert aged["quote_age_ms"] >= 45_000
        assert aged["quote_age_ms"] != 80
        assert aged["price_lane"] == "background"
        assert aged["freshness_class"] == "radar_current"
        assert aged["bet_actionable"] is False
        untouched = lagged["mkt-b"]
        assert untouched["last_priced_at"] is None
        assert untouched["quote_age_ms"] is None
        assert untouched["freshness_class"] == "radar_current"
        assert Decimal(str(untouched["current_net_edge"])) == Decimal("0.011")

        held_at = priced_at + timedelta(seconds=DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS - 30)
        clock["at"] = held_at
        held = {row["canonical_market_id"]: row for row in client.get("/paper/watchlist/tracked").json()}
        assert held["mkt-a"]["last_priced_at"].startswith("2026-09-16T12:00:45")
        assert held["mkt-a"]["last_seen_at"].startswith("2026-09-16T12:00:00")
        assert Decimal(str(held["mkt-a"]["current_net_edge"])) == Decimal("0.018")
        assert held["mkt-a"]["freshness_class"] == "radar_current"
        assert held["mkt-a"]["bet_actionable"] is False
        assert held["mkt-a"]["quote_age_ms"] > 2_000

        clock["at"] = priced_at + timedelta(seconds=DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS + 1)
        assert client.get("/paper/watchlist/tracked").json() == []
        assert provider_calls == []
    finally:
        app.dependency_overrides.clear()
        coordinator.reset()
        repository.close()


def test_hot_price_does_not_refresh_stale_economics() -> None:
    """HOT slot quote freshness is not the persisted edge's executable clock."""

    assert Settings().sports_hedge_execution_enabled is False
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    repository = SqliteWatchlistRepository()
    clock = {"at": T0}
    service = WatchlistService(repository, clock=lambda: clock["at"], max_quote_age_ms=2_000)
    store = coordinator.fixture_current_state()
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    try:
        _publish(
            store,
            [_named_row("both_teams_to_score", "mb-a", "pm-a")],
            [_decision(EVENT, "mkt-a", when=T0)],
            T0,
            pricing_refresh=False,
        )
        service.observe(
            _observation("mkt-a", ("mb-a", "pm-a"), T0).model_copy(
                update={"quote_age_ms": 1_500, "current_net_edge": Decimal("0.018")}
            )
        )
        hot_at = T0 + timedelta(seconds=55)
        hot_quote = _named_row("both_teams_to_score", "mb-a", "pm-a")
        assert hot_quote.matchbook is not None
        hot_quote = hot_quote.model_copy(
            update={"matchbook": hot_quote.matchbook.model_copy(update={"quote_age_ms": 40})}
        )
        _publish(
            store,
            [hot_quote],
            [_decision(EVENT, "mkt-a", when=hot_at)],
            hot_at,
            pricing_refresh=False,
            scan_lane=ScanLane.HOT,
        )
        clock["at"] = hot_at
        hot_slot = next(
            item
            for item in store._rows[EVENT].markets.values()
            if item.row.matchbook is not None and item.row.matchbook.source_market_id == "mb-a"
        )
        assert hot_slot.scan_lane is ScanLane.HOT
        assert slot_freshness(hot_slot, now=hot_at) == "executable"
        rows = {row["canonical_market_id"]: row for row in client.get("/paper/watchlist/tracked").json()}
        hot = rows["mkt-a"]
        assert hot["price_lane"] == "hot"
        assert hot["last_priced_at"].startswith("2026-09-16T12:00:55")
        assert hot["last_discovered_at"].startswith("2026-09-16T12:00:00")
        assert hot["last_seen_at"].startswith("2026-09-16T12:00:00")
        assert Decimal(str(hot["current_net_edge"])) == Decimal("0.018")
        assert hot["quote_age_ms"] >= 55_000
        assert hot["freshness_class"] == "radar_current"
        assert hot["bet_actionable"] is False
        clock["at"] = hot_at + timedelta(seconds=91)
        aged = {row["canonical_market_id"]: row for row in client.get("/paper/watchlist/tracked").json()}
        # Universe discovery TTL still holds the fixture. The expired HOT slot must
        # not advance the economics quote or mark the old edge executable.
        assert aged["mkt-a"]["last_priced_at"].startswith("2026-09-16T12:00:55")
        assert aged["mkt-a"]["last_seen_at"].startswith("2026-09-16T12:00:00")
        assert aged["mkt-a"]["freshness_class"] == "radar_current"
        assert aged["mkt-a"]["bet_actionable"] is False
        assert aged["mkt-a"]["quote_age_ms"] >= 90_000
        clock["at"] = T0 + timedelta(seconds=360)
        assert client.get("/paper/watchlist/tracked").json() == []
    finally:
        app.dependency_overrides.clear()
        coordinator.reset()
        repository.close()


def _facts(venue: VenueName, source_market_id: str, *, complete: bool | None, key: str | None) -> VenueMarketFacts:
    return VenueMarketFacts(
        venue=venue,
        source_event_id=f"{venue.value}-evt",
        source_market_id=source_market_id,
        family=MarketFamily.POINT_SPREAD.value,
        settlement_key=key,
        settlement_complete=complete,
        best_backs=[VenueQuoteFact(outcome="home", decimal_odds=Decimal("1.91"))],
    )


def _spread(line: str, *, reason: str, kalshi: bool, complete: bool | None = None) -> FixtureMarketInventoryRow:
    return FixtureMarketInventoryRow(
        display_name=f"Point Spread {line}",
        family=MarketFamily.POINT_SPREAD.value,
        line=Decimal(line),
        comparison_status=InventoryComparisonStatus.OTHER,
        reason=reason,
        rejection_reasons=[reason],
        matchbook=_facts(VenueName.MATCHBOOK, f"mb-{line}", complete=True, key="including_extra_time"),
        polymarket=_facts(VenueName.POLYMARKET, f"pm-{line}", complete=True, key="including_extra_time"),
        kalshi=(
            _facts(VenueName.KALSHI, f"k-{line}", complete=complete, key=None if complete is False else "spread")
            if kalshi
            else None
        ),
    )


def test_review_required_names_kalshi_only_with_kalshi_settlement_evidence() -> None:
    spreads = [
        _spread("-3.5", reason="catalogue_review_required", kalshi=False),
        _spread("-2.5", reason="incomplete_settlement", kalshi=False),
        _spread("-7.5", reason="catalogue_review_required", kalshi=False),
        _spread("3.5", reason="incomplete_settlement", kalshi=True, complete=False),
    ]
    coverage = fixture_catalogue_coverage(
        spreads,
        matchbook_matched=True,
        polymarket_matched=True,
        kalshi_matched=False,
        sport="american_football",
    )
    by_line = {row.line: row for row in coverage.rows if row.line}
    assert len(by_line) == 4
    assert by_line["-3.5"].state is CatalogueCoverageState.REVIEW_REQUIRED
    assert by_line["-3.5"].reason == "catalogue review required (matchbook, polymarket)"
    assert "Kalshi" not in by_line["-3.5"].reason
    assert by_line["-2.5"].reason == "incomplete settlement (matchbook, polymarket)"
    assert by_line["-7.5"].reason == "catalogue review required (matchbook, polymarket)"
    assert by_line["3.5"].reason == "Kalshi settlement proof missing"
    assert all(row.line for row in coverage.rows if "Point Spread" in row.display_label)
