"""Issue #165: merged current market inventory across HOT and UNIVERSE."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.application.collector import CollectionReport, DiscoveredFixture
from sports_hedge.application.current_market_inventory import (
    canonical_current_market_key,
    equivalent_comparison_count,
)
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.fixture_inventory import (
    FixtureMarketInventoryRow,
    InventoryComparisonStatus,
    InventoryPairResult,
    VenueMarketFacts,
    VenueQuoteFact,
)
from sports_hedge.application.live_refresh import get_live_refresh_coordinator
from sports_hedge.application.scan_lanes import (
    DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    DEFAULT_HOT_TTL_SECONDS,
    FRESHNESS_RADAR_CURRENT,
    ScanLane,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.models import PaperScanDecision

NOW = datetime(2026, 9, 15, 14, 50, tzinfo=UTC)
KICKOFF = NOW + timedelta(minutes=20)
CANONICAL_ID = "leeds-newcastle"


def _facts(
    venue: VenueName,
    *,
    source_market_id: str,
    family: str,
    quote_age_ms: int | None = 80,
) -> VenueMarketFacts:
    if family == "both_teams_to_score":
        quotes = [
            VenueQuoteFact(outcome="yes", decimal_odds=Decimal("2.10"), size_at_touch=Decimal("100")),
            VenueQuoteFact(outcome="no", decimal_odds=Decimal("1.80"), size_at_touch=Decimal("100")),
        ]
    else:
        quotes = [
            VenueQuoteFact(outcome="home", decimal_odds=Decimal("2.40"), size_at_touch=Decimal("100")),
            VenueQuoteFact(outcome="draw", decimal_odds=Decimal("3.50"), size_at_touch=Decimal("100")),
            VenueQuoteFact(outcome="away", decimal_odds=Decimal("3.10"), size_at_touch=Decimal("100")),
        ]
    return VenueMarketFacts(
        venue=venue,
        source_event_id=f"{venue.value}-leeds-newcastle",
        source_market_id=source_market_id,
        family=family,
        period="full_time",
        settlement_key="regulation_time|full_time",
        settlement_complete=True,
        best_backs=quotes,
        quote_age_ms=quote_age_ms,
        quote_age_basis="source",
        native_currency="GBP" if venue is VenueName.MATCHBOOK else "USD",
    )


def _pair(left: VenueName, right: VenueName, *, edge: Decimal | None, arb: bool = False) -> InventoryPairResult:
    return InventoryPairResult(
        left_venue=left,
        right_venue=right,
        entered_solver=True,
        solver_model="strict_complete_set",
        current_net_edge=edge,
        rejection_reasons=[] if arb or edge is not None else ["no_positive_edge"],
        solver_is_arbitrage=arb,
    )


def _market_row(
    *,
    family: str,
    status: InventoryComparisonStatus = InventoryComparisonStatus.MATCHED_EQUIVALENT,
    edge: Decimal | None = Decimal("-0.004"),
    arb: bool = False,
    quote_age_ms: int | None = 80,
    reason: str | None = None,
) -> FixtureMarketInventoryRow:
    display = "Match Result" if family == "match_result" else "BTTS"
    prefix = "mr" if family == "match_result" else "btts"
    return FixtureMarketInventoryRow(
        display_name=display,
        family=family,
        period="full_time",
        comparison_status=status,
        reason=reason,
        rejection_reasons=[] if status is InventoryComparisonStatus.MATCHED_EQUIVALENT else [reason or "unmatched"],
        match_reasons=[],
        entered_solver=status is InventoryComparisonStatus.MATCHED_EQUIVALENT,
        solver_model="strict_complete_set"
        if status is InventoryComparisonStatus.MATCHED_EQUIVALENT
        else None,
        current_net_edge=edge,
        trigger_net_edge=Decimal("0.01"),
        distance_to_trigger_pp=Decimal("0.014") if edge is not None else None,
        solver_is_arbitrage=arb,
        matchbook=_facts(VenueName.MATCHBOOK, source_market_id=f"mb-{prefix}", family=family, quote_age_ms=quote_age_ms),
        polymarket=_facts(
            VenueName.POLYMARKET, source_market_id=f"pm-{prefix}", family=family, quote_age_ms=quote_age_ms
        ),
        kalshi=_facts(VenueName.KALSHI, source_market_id=f"k-{prefix}", family=family, quote_age_ms=quote_age_ms),
        pair_results=[
            _pair(VenueName.MATCHBOOK, VenueName.POLYMARKET, edge=edge, arb=arb),
            _pair(VenueName.MATCHBOOK, VenueName.KALSHI, edge=edge, arb=arb),
            _pair(VenueName.POLYMARKET, VenueName.KALSHI, edge=edge, arb=arb),
        ],
    )


def _fixture(
    *,
    equivalent: int | None = 1,
    evaluation: str = "evaluated",
    opportunity: str = "matched",
    when: datetime = NOW,
    arb: bool = False,
    best_arb: str | None = None,
    qualifying: int = 0,
    near: int = 0,
) -> DiscoveredFixture:
    return DiscoveredFixture(
        source=VenueName.MATCHBOOK,
        source_event_id="mb-leeds-newcastle",
        canonical_event_id=CANONICAL_ID,
        home_team="Leeds United",
        away_team="Newcastle United",
        competition="Premier League",
        kickoff_utc=KICKOFF,
        matchbook_matched=True,
        polymarket_matched=True,
        kalshi_matched=True,
        last_seen_at=when,
        matched_equivalent_count=equivalent,
        qualifying_market_count=qualifying,
        near_executable_market_count=near,
        best_arb_market=best_arb,
        solver_is_arbitrage=arb,
        opportunity_state=opportunity,
        market_evaluation_state=evaluation,
        no_comparison_reason=None if equivalent else "no_settlement_equivalent_market_pair",
    )


def _decision(market_id: str, *, when: datetime = NOW) -> PaperScanDecision:
    return PaperScanDecision(
        canonical_event_id=CANONICAL_ID,
        canonical_market_id=market_id,
        fixture_canonical_event_id=CANONICAL_ID,
        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=[]),
        scanned_at=when,
    )


def _report(
    *,
    fixture: DiscoveredFixture,
    markets: list[FixtureMarketInventoryRow],
    market_ids: list[str],
    lane: ScanLane,
    when: datetime,
) -> CollectionReport:
    aliases = {
        CANONICAL_ID: CANONICAL_ID,
        fixture.source_event_id: CANONICAL_ID,
        "pm-leeds-newcastle": CANONICAL_ID,
        "k-leeds-newcastle": CANONICAL_ID,
        "pair-mb-pm": CANONICAL_ID,
    }
    return CollectionReport(
        started_at=when,
        completed_at=when,
        paper_decisions=[_decision(item, when=when) for item in market_ids],
        discovered_fixtures=[fixture],
        fixture_markets={CANONICAL_ID: markets},
        fixture_identity_aliases=aliases,
        scan_lane=lane.value,
        operator_summary=f"{lane.value} current-market-inventory",
        venue_health={"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"},
    )


def _universe_full(*, when: datetime = NOW, btts_arb: bool = False) -> CollectionReport:
    mr = _market_row(family="match_result", edge=Decimal("-0.004"))
    btts = _market_row(
        family="both_teams_to_score",
        edge=Decimal("0.012") if btts_arb else Decimal("0.008"),
        arb=btts_arb,
    )
    fixture = _fixture(
        equivalent=1,
        when=when,
        best_arb="BTTS · Yes" if btts_arb else None,
        qualifying=1 if btts_arb else 0,
        near=0 if btts_arb else 1,
        arb=btts_arb,
        opportunity="qualifying" if btts_arb else "near",
    )
    return _report(
        fixture=fixture,
        markets=[mr, btts],
        market_ids=["mkt-mr", "mkt-btts"],
        lane=ScanLane.UNIVERSE,
        when=when,
    )


def test_canonical_keys_are_family_not_display_aliases() -> None:
    mr = _market_row(family="match_result")
    btts = _market_row(family="both_teams_to_score")
    alias = mr.model_copy(update={"display_name": "Leeds v Newcastle Match Odds"})
    assert canonical_current_market_key(mr) == canonical_current_market_key(alias)
    assert canonical_current_market_key(mr) != canonical_current_market_key(btts)
    assert "Leeds" not in canonical_current_market_key(alias)
    assert equivalent_comparison_count([mr, btts]) == 6


def test_same_family_distinct_source_markets_do_not_collapse() -> None:
    complete = _market_row(family="first_team_to_score")
    incomplete = complete.model_copy(
        update={
            "display_name": "First Team To Score incomplete",
            "entered_solver": False,
            "reason": "incomplete_outcome_set",
            "comparison_status": InventoryComparisonStatus.UNSUPPORTED_OUTCOME_MODEL,
            "matchbook": _facts(
                VenueName.MATCHBOOK,
                source_market_id="mb-fts-incomplete",
                family="first_team_to_score",
            ),
            "polymarket": None,
            "kalshi": None,
            "pair_results": [],
        }
    )
    store = FixtureCurrentStateStore()
    store.upsert_from_report(
        _report(
            fixture=_fixture(equivalent=1),
            markets=[complete, incomplete],
            market_ids=["mkt-fts"],
            lane=ScanLane.UNIVERSE,
            when=NOW,
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    detail = store.detail(CANONICAL_ID, now=NOW)
    assert detail is not None
    fts = [item for item in detail.markets if item.family == "first_team_to_score"]
    assert len(fts) == 2
    assert any(item.entered_solver for item in fts)
    assert any(not item.entered_solver and item.reason == "incomplete_outcome_set" for item in fts)


def test_hot_partial_refresh_does_not_collapse_still_current_equivalents() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(_universe_full(), scan_lane=ScanLane.UNIVERSE, now=NOW)
    hot_only_mr = _report(
        fixture=_fixture(equivalent=1, when=NOW + timedelta(seconds=25), best_arb=None),
        markets=[_market_row(family="match_result", edge=Decimal("-0.002"))],
        market_ids=["mkt-mr"],
        lane=ScanLane.HOT,
        when=NOW + timedelta(seconds=25),
    )
    store.upsert_from_report(hot_only_mr, scan_lane=ScanLane.HOT, now=NOW + timedelta(seconds=25))

    inventory = store.inventory(NOW + timedelta(seconds=25))
    assert len(inventory) == 1
    row = inventory[0]
    assert row.matched_equivalent_count == 6
    assert row.matched_equivalent_count != 1
    families = {item.family for item in store.detail(CANONICAL_ID, now=NOW + timedelta(seconds=25)).markets}
    assert families == {"match_result", "both_teams_to_score"}
    assert row.near_executable_market_count >= 1 or row.qualifying_market_count >= 1 or row.best_arb_market


def test_fresher_hot_supersedes_older_universe_for_same_canonical_market() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(_universe_full(), scan_lane=ScanLane.UNIVERSE, now=NOW)
    later = NOW + timedelta(seconds=20)
    store.upsert_from_report(
        _report(
            fixture=_fixture(equivalent=1, when=later),
            markets=[_market_row(family="match_result", edge=Decimal("0.009"))],
            market_ids=["mkt-mr"],
            lane=ScanLane.HOT,
            when=later,
        ),
        scan_lane=ScanLane.HOT,
        now=later,
    )
    detail = store.detail(CANONICAL_ID, now=later)
    assert detail is not None
    match_result = next(item for item in detail.markets if item.family == "match_result")
    btts = next(item for item in detail.markets if item.family == "both_teams_to_score")
    assert match_result.current_net_edge == Decimal("0.009")
    assert match_result.scan_lane == ScanLane.HOT.value
    assert btts.current_net_edge == Decimal("0.008")
    assert btts.scan_lane == ScanLane.UNIVERSE.value


def test_not_evaluated_does_not_erase_still_valid_prior_current_state() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(_universe_full(), scan_lane=ScanLane.UNIVERSE, now=NOW)
    leftover = _fixture(
        equivalent=None,
        evaluation="not_evaluated_scan_deadline",
        opportunity="not_evaluated",
        when=NOW + timedelta(seconds=30),
    )
    store.upsert_from_report(
        _report(
            fixture=leftover,
            markets=[],
            market_ids=[],
            lane=ScanLane.HOT,
            when=NOW + timedelta(seconds=30),
        ),
        scan_lane=ScanLane.HOT,
        now=NOW + timedelta(seconds=30),
    )
    detail = store.detail(CANONICAL_ID, now=NOW + timedelta(seconds=30))
    assert detail is not None
    assert detail.fixture.matched_equivalent_count == 6
    assert {item.family for item in detail.markets} == {"match_result", "both_teams_to_score"}
    assert leftover.matched_equivalent_count is None


def test_explicit_re_evaluation_to_unmatched_updates_current_state() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(_universe_full(), scan_lane=ScanLane.UNIVERSE, now=NOW)
    later = NOW + timedelta(seconds=20)
    unmatched = _market_row(
        family="match_result",
        status=InventoryComparisonStatus.SETTLEMENT_MISMATCH,
        edge=None,
        reason="settlement_mismatch",
        quote_age_ms=80,
    )
    store.upsert_from_report(
        _report(
            fixture=_fixture(equivalent=0, when=later, opportunity="unmatched"),
            markets=[unmatched],
            market_ids=["mkt-mr"],
            lane=ScanLane.HOT,
            when=later,
        ),
        scan_lane=ScanLane.HOT,
        now=later,
    )
    detail = store.detail(CANONICAL_ID, now=later)
    assert detail is not None
    match_result = next(item for item in detail.markets if item.family == "match_result")
    assert match_result.comparison_status is InventoryComparisonStatus.SETTLEMENT_MISMATCH
    assert any(item.family == "both_teams_to_score" for item in detail.markets)
    assert detail.fixture.matched_equivalent_count == 3


def test_expiry_removes_stale_current_market_rows() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(_universe_full(), scan_lane=ScanLane.UNIVERSE, now=NOW)
    hot_at = NOW + timedelta(seconds=10)
    store.upsert_from_report(
        _report(
            fixture=_fixture(equivalent=1, when=hot_at),
            markets=[_market_row(family="match_result")],
            market_ids=["mkt-mr"],
            lane=ScanLane.HOT,
            when=hot_at,
        ),
        scan_lane=ScanLane.HOT,
        now=hot_at,
    )
    after_hot_ttl = hot_at + timedelta(seconds=DEFAULT_HOT_TTL_SECONDS + 1)
    detail = store.detail(CANONICAL_ID, now=after_hot_ttl)
    assert detail is not None
    families = {item.family for item in detail.markets}
    assert "match_result" not in families
    assert families == {"both_teams_to_score"}
    assert detail.fixture.matched_equivalent_count == 3


def test_headline_counts_match_merged_inventory_and_executable_freshness() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(
        _universe_full(btts_arb=True),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    at_scan = store.inventory(NOW)[0]
    assert at_scan.matched_equivalent_count == 6
    assert at_scan.qualifying_market_count == 1
    assert at_scan.best_arb_market == "BTTS"
    assert at_scan.opportunity_state == "qualifying"

    aged = NOW + timedelta(seconds=5)
    later = store.inventory(aged)[0]
    assert later.matched_equivalent_count == 6
    assert later.qualifying_market_count == 0
    assert later.solver_is_arbitrage is False
    detail = store.detail(CANONICAL_ID, now=aged)
    assert detail is not None
    assert all(item.radar_freshness == FRESHNESS_RADAR_CURRENT for item in detail.markets)
    assert DEFAULT_EXECUTABLE_QUOTE_AGE_MS == 1000
    assert aged - NOW > timedelta(milliseconds=DEFAULT_EXECUTABLE_QUOTE_AGE_MS)


def test_radar_current_rows_stay_visible_without_paper_eligibility() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(
        _universe_full(btts_arb=True),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    aged = NOW + timedelta(seconds=2)
    radar = store.current_radar_rows(aged)
    assert len(radar) == 1
    assert radar[0].freshness == FRESHNESS_RADAR_CURRENT
    assert set(radar[0].paper_market_ids) == {"mkt-mr", "mkt-btts"}
    detail = store.detail(CANONICAL_ID, now=aged)
    assert detail is not None
    btts = next(item for item in detail.markets if item.family == "both_teams_to_score")
    assert btts.solver_is_arbitrage is True
    assert btts.radar_freshness == FRESHNESS_RADAR_CURRENT
    assert detail.fixture.qualifying_market_count == 0


def test_identity_click_through_survives_hot_partial_and_has_no_demo_fallback() -> None:
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    coordinator.record_report(_universe_full(), scan_lane=ScanLane.UNIVERSE)
    coordinator.record_report(
        _report(
            fixture=_fixture(equivalent=1, when=NOW + timedelta(seconds=25)),
            markets=[_market_row(family="match_result")],
            market_ids=["mkt-mr"],
            lane=ScanLane.HOT,
            when=NOW + timedelta(seconds=25),
        ),
        scan_lane=ScanLane.HOT,
    )
    client = TestClient(app)
    try:
        store = coordinator.fixture_current_state()
        assert store.resolve_canonical_id(CANONICAL_ID) == CANONICAL_ID
        assert store.resolve_canonical_id("mb-leeds-newcastle") == CANONICAL_ID
        assert store.resolve_canonical_id("pair-mb-pm") == CANONICAL_ID
        for identity in (CANONICAL_ID, "mb-leeds-newcastle", "pair-mb-pm"):
            response = client.get(f"/operations/fixtures/{identity}")
            assert response.status_code == 200
            body = response.json()
            assert body["fixture"]["canonical_event_id"] == CANONICAL_ID
            assert body["fixture"]["home_team"] == "Leeds United"
            assert body["fixture"]["matched_equivalent_count"] == 6
            families = {item["family"] for item in body["markets"]}
            assert families == {"match_result", "both_teams_to_score"}
            assert body["data_class"] == "live_paper_when_collected"
        missing = client.get("/operations/fixtures/demo-leeds-newcastle")
        assert missing.status_code == 404
        assert "demo" not in missing.json()["detail"].lower()
        health = client.get("/health").json()
        assert health["execution_enabled"] is False
    finally:
        coordinator.reset()
