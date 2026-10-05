"""HOT proximity depth gate and BACKGROUND Opportunity Monitor retention.

Data class: deterministic fixture/demo current-state and paper-scan payloads.
Not live venue quotes. Phase 1 execution stays disabled.

The universe-lane clock test records the mechanism that emptied the monitor:
BACKGROUND pricing is stamped `scan_lane=universe`, and `freshness_class` for
that lane expires at the 360s UNIVERSE current-state TTL. A full BACKGROUND
catalogue pass is longer than that. Retention is a separate clock.
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from decimal import Decimal

from test_issue200_universe_hot_promotion import (
    CANONICAL_ID,
    NOW,
    _decision,
    _fixture,
    _market_row,
    _report,
)
from test_near_arbitrage_watchlist import _observation

from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.scan_lanes import (
    DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS,
    DEFAULT_HOT_TTL_SECONDS,
    DEFAULT_UNIVERSE_TTL_SECONDS,
    FRESHNESS_EXECUTABLE,
    FRESHNESS_EXPIRED,
    FRESHNESS_RADAR_CURRENT,
    ScanLane,
    freshness_class,
)
from sports_hedge.arbitrage.depth import DepthQuoteCandidate
from sports_hedge.arbitrage.models import PayoffSolution
from sports_hedge.arbitrage.payoff_scan import PayoffScanResult
from sports_hedge.arbitrage.watchlist.adapter import observation_from_paper_decision
from sports_hedge.arbitrage.watchlist.economics import (
    DEFAULT_HOT_MINIMUM_LIMITING_DEPTH_GBP,
    DEFAULT_HOT_PROXIMITY_BAND_PP,
    distance_to_trigger_pp,
    limiting_depth_gbp_from_decision,
    promotes_hot_net_proximity,
)
from sports_hedge.arbitrage.watchlist.ranking import opportunity_id_for_canonical_market
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.persistence.operator_scanner_settings import (
    SqliteOperatorScannerSettingsStore,
)

MIN_NET = Decimal("0.005")  # +0.50%
EDGE_NEG_010 = Decimal("-0.001")  # -0.10%
EDGE_ZERO = Decimal("0")
EDGE_POS_020 = Decimal("0.002")  # +0.20%
DEPTH_OK = Decimal("150")
DEPTH_EIGHTY = Decimal("80")
DEPTH_PENNY = Decimal("0.11")
DEPTH_SMALL = Decimal("1.83")


def _quote(depth: Decimal, *, outcome: str = "yes") -> DepthQuoteCandidate:
    return DepthQuoteCandidate(
        outcome=outcome,
        venue=VenueName.MATCHBOOK,
        source_market_id="mb",
        source_runner_id=outcome,
        gross_weighted_odds=Decimal("2.10"),
        net_decimal_odds=Decimal("2.05"),
        cumulative_depth=depth,
        levels_consumed=1,
    )


def _priced_decision(*depths: Decimal) -> PaperScanDecision:
    return PaperScanDecision(
        scanned_at=NOW,
        canonical_event_id="evt-depth",
        canonical_market_id="mkt-depth",
        market_match=MarketMatchResult(matched=True, confidence=1, reasons=["register"]),
        payoff_scan=PayoffScanResult(
            solution=PayoffSolution(
                is_arbitrage=False,
                roi=EDGE_NEG_010,
                minimum_state_pnl=Decimal("0"),
                numerically_validated=True,
            ),
            selected_quotes=[_quote(depth, outcome=f"leg-{index}") for index, depth in enumerate(depths)],
        ),
        minimum_net_edge=MIN_NET,
        solver_model="strict_complete_set",
        eligible_for_paper_simulation=False,
    )


def _publish(
    store: FixtureCurrentStateStore,
    row,
    *,
    when=NOW,
    lane: ScanLane = ScanLane.UNIVERSE,
    pricing_refresh: bool = False,
    canonical_id: str = CANONICAL_ID,
    fixture_status: str | None = None,
    decisions: list | None = None,
    markets: list | None = None,
) -> str:
    fixture = _fixture(
        canonical_id,
        when=when,
        opportunity="near",
        arb=False,
        qualifying=0,
        fixture_status=fixture_status,
    )
    market_id = f"mkt-{canonical_id}"
    report = _report(
        [fixture],
        when=when,
        scan_lane=lane.value,
        markets={canonical_id: [row] if markets is None else markets},
        decisions=[_decision(canonical_id, market_id, when=when)] if decisions is None else decisions,
    )
    store.upsert_from_report(report, scan_lane=lane, now=when, pricing_refresh=pricing_refresh)
    return opportunity_id_for_canonical_market(market_id)


def test_universe_lane_clock_expires_inside_a_background_catalogue_pass() -> None:
    """Prove the monitor-emptying clock before retention is applied.

    BACKGROUND pricing still publishes `scan_lane=universe`. That lane's
    freshness expires at 360s. A ~629-row pass is longer than 360s, so a
    priced opportunity drops out of current radar before BACKGROUND returns.
    """

    assert DEFAULT_UNIVERSE_TTL_SECONDS == 360
    assert DEFAULT_HOT_TTL_SECONDS == 90
    assert DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS > 21 * 60
    assert DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS == 45 * 60
    inside = NOW + timedelta(seconds=DEFAULT_UNIVERSE_TTL_SECONDS - 1)
    outside = NOW + timedelta(seconds=DEFAULT_UNIVERSE_TTL_SECONDS + 1)
    assert (
        freshness_class(
            lane=ScanLane.UNIVERSE,
            last_scanned_at=NOW,
            now=inside,
            quote_age_ms=5_000,
        )
        == FRESHNESS_RADAR_CURRENT
    )
    assert (
        freshness_class(
            lane=ScanLane.UNIVERSE,
            last_scanned_at=NOW,
            now=outside,
            quote_age_ms=5_000,
        )
        == FRESHNESS_EXPIRED
    )
    from sports_hedge.application.price_engine import CataloguePriceEngine

    source = inspect.getsource(CataloguePriceEngine._project_item_state)
    assert "ScanLane.UNIVERSE" in source
    assert "PriceEnginePriority.BACKGROUND" in source


def test_plain_universe_discovery_still_uses_the_short_ttl() -> None:
    store = FixtureCurrentStateStore()
    row = _market_row(edge=EDGE_ZERO, arb=False, trigger=MIN_NET, limiting_depth_gbp=DEPTH_OK)
    opportunity_id = _publish(store, row, pricing_refresh=False)
    assert opportunity_id in store.current_tracked_opportunity_ids(NOW)
    expired = NOW + timedelta(seconds=DEFAULT_UNIVERSE_TTL_SECONDS + 1)
    assert opportunity_id not in store.current_tracked_opportunity_ids(expired)


def test_negative_edge_with_depth_promotes_inside_configured_band() -> None:
    assert DEFAULT_HOT_PROXIMITY_BAND_PP == Decimal("0.60")
    assert DEFAULT_HOT_MINIMUM_LIMITING_DEPTH_GBP == Decimal("10")
    assert distance_to_trigger_pp(EDGE_NEG_010, MIN_NET) == Decimal("0.6000")
    assert promotes_hot_net_proximity(EDGE_NEG_010, MIN_NET, DEPTH_OK) is True
    row = _market_row(
        edge=EDGE_NEG_010,
        arb=False,
        trigger=MIN_NET,
        limiting_depth_gbp=DEPTH_OK,
    )
    store = FixtureCurrentStateStore()
    _publish(store, row)
    assert CANONICAL_ID in store.hot_identity_scope(NOW)


def test_zero_edge_penny_depth_does_not_promote() -> None:
    assert distance_to_trigger_pp(EDGE_ZERO, MIN_NET) == Decimal("0.5000")
    assert promotes_hot_net_proximity(EDGE_ZERO, MIN_NET, DEPTH_PENNY) is False
    row = _market_row(
        edge=EDGE_ZERO,
        arb=False,
        trigger=MIN_NET,
        limiting_depth_gbp=DEPTH_PENNY,
    )
    store = FixtureCurrentStateStore()
    opportunity_id = _publish(store, row, pricing_refresh=True)
    assert CANONICAL_ID not in store.hot_identity_scope(NOW)
    assert opportunity_id in store.current_tracked_opportunity_ids(NOW)


def test_positive_edge_small_depth_does_not_promote() -> None:
    assert distance_to_trigger_pp(EDGE_POS_020, MIN_NET) == Decimal("0.3000")
    assert promotes_hot_net_proximity(EDGE_POS_020, MIN_NET, DEPTH_SMALL) is False
    assert promotes_hot_net_proximity(EDGE_POS_020, MIN_NET, DEPTH_EIGHTY) is True
    row = _market_row(
        edge=EDGE_POS_020,
        arb=False,
        trigger=MIN_NET,
        limiting_depth_gbp=DEPTH_SMALL,
    )
    store = FixtureCurrentStateStore()
    _publish(store, row)
    assert CANONICAL_ID not in store.hot_identity_scope(NOW)


def test_low_depth_observation_stays_persisted_for_the_monitor() -> None:
    observation = _observation(
        edge=EDGE_ZERO,
        eligible=False,
        rejection_reasons=["net_edge_below_threshold"],
        limiting_depth=DEPTH_PENNY,
        trigger=MIN_NET,
    )
    service = WatchlistService(
        repository=SqliteWatchlistRepository(":memory:"),
        clock=lambda: NOW,
    )
    saved = service.observe(observation)
    assert saved.limiting_depth_gbp == DEPTH_PENNY
    assert any(
        item.opportunity_id == saved.opportunity_id
        for item in service.repository.list_opportunities()
    )

    store = FixtureCurrentStateStore()
    row = _market_row(
        edge=EDGE_ZERO,
        arb=False,
        trigger=MIN_NET,
        limiting_depth_gbp=DEPTH_PENNY,
    )
    opportunity_id = _publish(store, row, pricing_refresh=True)
    assert opportunity_id in store.current_tracked_opportunity_ids(NOW)
    assert CANONICAL_ID not in store.hot_identity_scope(NOW)


def test_limiting_depth_is_the_scan_watchlist_gbp_minimum() -> None:
    decision = _priced_decision(Decimal("150"), Decimal("80"), Decimal("0.11"))
    assert limiting_depth_gbp_from_decision(decision) == DEPTH_PENNY
    observed = observation_from_paper_decision(decision)
    assert observed is not None
    assert observed.limiting_depth_gbp == limiting_depth_gbp_from_decision(decision)
    from sports_hedge.application.fixture_inventory import _decision_limiting_depth

    assert _decision_limiting_depth(decision) == observed.limiting_depth_gbp


def test_hot_proximity_gate_has_no_sport_branch() -> None:
    source = inspect.getsource(promotes_hot_net_proximity)
    lowered = source.casefold()
    for token in ("football", "tennis", "nba", "nfl", "ncaab", "mlb", "soccer", "sport"):
        assert token not in lowered
    assert promotes_hot_net_proximity(EDGE_NEG_010, MIN_NET, DEPTH_OK) is True
    assert promotes_hot_net_proximity(EDGE_NEG_010, MIN_NET, DEPTH_PENNY) is False


def test_background_row_stays_radar_current_across_universe_ttl_while_stale() -> None:
    store = FixtureCurrentStateStore()
    row = _market_row(
        edge=EDGE_ZERO,
        arb=False,
        trigger=MIN_NET,
        limiting_depth_gbp=DEPTH_PENNY,
        quote_age_ms=5_000,
    )
    opportunity_id = _publish(store, row, pricing_refresh=True)
    between_revisits = NOW + timedelta(seconds=DEFAULT_UNIVERSE_TTL_SECONDS + 60)
    assert opportunity_id in store.current_tracked_opportunity_ids(between_revisits)
    radar = store.current_radar_rows(between_revisits)
    assert len(radar) == 1
    assert radar[0].freshness == FRESHNESS_RADAR_CURRENT
    assert radar[0].freshness != FRESHNESS_EXECUTABLE
    assert radar[0].markets[0].radar_freshness == FRESHNESS_RADAR_CURRENT


def test_authoritative_removal_drops_a_retained_background_row() -> None:
    store = FixtureCurrentStateStore()
    row = _market_row(edge=EDGE_ZERO, arb=False, trigger=MIN_NET, limiting_depth_gbp=DEPTH_OK)
    opportunity_id = _publish(store, row, pricing_refresh=True)
    later = NOW + timedelta(seconds=30)
    fixture = _fixture(when=later, opportunity="matched", arb=False, qualifying=0)
    report = _report(
        [fixture],
        when=later,
        markets={CANONICAL_ID: []},
        decisions=[_decision(CANONICAL_ID, "", when=later)],
    )
    store.upsert_from_report(report, scan_lane=ScanLane.UNIVERSE, now=later, pricing_refresh=False)
    assert opportunity_id not in store.current_tracked_opportunity_ids(later)


def test_terminal_lifecycle_drops_a_retained_background_row() -> None:
    store = FixtureCurrentStateStore()
    row = _market_row(edge=EDGE_ZERO, arb=False, trigger=MIN_NET, limiting_depth_gbp=DEPTH_OK)
    opportunity_id = _publish(store, row, pricing_refresh=True)
    later = NOW + timedelta(seconds=30)
    _publish(store, row, when=later, pricing_refresh=True, fixture_status="completed")
    assert opportunity_id not in store.current_tracked_opportunity_ids(later)


def test_background_retention_expiry_removes_the_row() -> None:
    store = FixtureCurrentStateStore()
    row = _market_row(edge=EDGE_ZERO, arb=False, trigger=MIN_NET, limiting_depth_gbp=DEPTH_OK)
    opportunity_id = _publish(store, row, pricing_refresh=True)
    still = NOW + timedelta(seconds=DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS - 1)
    gone = NOW + timedelta(seconds=DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS + 1)
    assert opportunity_id in store.current_tracked_opportunity_ids(still)
    assert opportunity_id not in store.current_tracked_opportunity_ids(gone)


def test_tracked_cohort_does_not_backfill_audit_history() -> None:
    service = WatchlistService(
        repository=SqliteWatchlistRepository(":memory:"),
        clock=lambda: NOW,
    )
    saved = service.observe(
        _observation(
            edge=EDGE_POS_020,
            eligible=False,
            rejection_reasons=["net_edge_below_threshold"],
            limiting_depth=DEPTH_OK,
            trigger=MIN_NET,
        )
    )
    store = FixtureCurrentStateStore()
    cohort = store.current_tracked_opportunity_ids(NOW)
    assert saved.opportunity_id not in cohort
    assert service.tracked(collection_cohort_ids=cohort) == []
    assert any(
        item.opportunity_id == saved.opportunity_id
        for item in service.repository.list_opportunities()
    )


def test_hot_ttl_is_not_extended_by_background_retention() -> None:
    store = FixtureCurrentStateStore()
    row = _market_row(edge=Decimal("0.02"), arb=True, trigger=MIN_NET, limiting_depth_gbp=DEPTH_OK)
    opportunity_id = _publish(store, row, lane=ScanLane.HOT, pricing_refresh=True)
    inside = NOW + timedelta(seconds=DEFAULT_HOT_TTL_SECONDS - 1)
    outside = NOW + timedelta(seconds=DEFAULT_HOT_TTL_SECONDS + 1)
    assert opportunity_id in store.current_tracked_opportunity_ids(inside)
    assert opportunity_id not in store.current_tracked_opportunity_ids(outside)
    assert outside < NOW + timedelta(seconds=DEFAULT_BACKGROUND_CURRENT_STATE_TTL_SECONDS)


def test_operator_hot_proximity_defaults_round_trip() -> None:
    store = SqliteOperatorScannerSettingsStore(":memory:")
    saved = store.save_settings(min_net_edge=Decimal("0.005"), max_execution_risk=40)
    assert saved.hot_proximity_band_pp == Decimal("0.60")
    assert saved.hot_minimum_limiting_depth_gbp == Decimal("10.00")
    updated = store.save_settings(
        min_net_edge=Decimal("0.005"),
        max_execution_risk=40,
        hot_proximity_band_pp=Decimal("0.40"),
        hot_minimum_limiting_depth_gbp=Decimal("25"),
    )
    assert updated.hot_proximity_band_pp == Decimal("0.40")
    assert updated.hot_minimum_limiting_depth_gbp == Decimal("25.00")
    assert promotes_hot_net_proximity(
        EDGE_NEG_010,
        MIN_NET,
        DEPTH_OK,
        band_pp=updated.hot_proximity_band_pp,
        minimum_limiting_depth_gbp=updated.hot_minimum_limiting_depth_gbp,
    ) is False


def test_execution_remains_disabled() -> None:
    assert Settings.model_fields["sports_hedge_execution_enabled"].default is False
