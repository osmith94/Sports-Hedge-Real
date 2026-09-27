"""Issue #326: PAPER admission for all four locked Matchbook↔Kalshi families.

Owner clarification: once canonical fixture + canonical market identity match,
MATCH_RESULT / BTTS / exact-line TOTAL / FTTS are owner-approved cross-venue
equivalents in PAPER / READ-ONLY mode. Kalshi fair-price wording does not
block PAPER admission. Live execution stays ineligible. No Polymarket
expansion. No timeout inflation.

Deterministic fixture/demo providers. Not owner-live quotes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from sports_hedge.application.collector import MarketEvaluationState, ReadOnlyCrossVenueCollector
from sports_hedge.application.complete_set import scan_eligible_pair, solver_model_for_pair
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.fixture_inventory import (
    InventoryComparisonStatus,
    inventory_is_comparable_opportunity,
)
from sports_hedge.application.hot_market_relationships import relationships_from_fixture_markets
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.catalogue.admission import (
    assess_catalogue_admission,
    catalogue_allows_live_execution,
    catalogue_allows_solver,
)
from sports_hedge.catalogue.classify import PayloadSide, classify_payload_pair, normalize_payload_side
from sports_hedge.catalogue.corpus import (
    CANCEL_RESCHEDULE_FAIR_PRICE,
    ET_RULES,
    GAMEWIN_TEMPLATE,
    REGULATION,
    _mb,
    _mb_1x2,
    _mb_btts as _census_mb_btts,
    _pm,
    _pm_1x2,
    _pm_btts,
)
from sports_hedge.catalogue.coverage_rows import fixture_catalogue_coverage
from sports_hedge.catalogue.registry import (
    REGISTRY_PARENT_COMMIT,
    REGISTRY_VERSION,
    CatalogueCoverageState,
    registry_cell,
)
from sports_hedge.catalogue.states import CatalogueApprovalState, CatalogueArchetype
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.matching.paper_assumed import (
    FAIR_PRICE_PAPER_ADMITTED_REASON,
    OWNER_APPROVED_PAPER_EQUIVALENCE_REASON,
    paper_assumed_locked_family,
)
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient
from test_issue316_catalogue_registry import (
    MB_EVENT_ID,
    _DisabledPolymarket,
    _all_books,
    _collect,
    _costs,
    _fx,
    _kalshi_btts_event,
    _kalshi_ftts_event,
    _kalshi_game_event,
    _kalshi_total_event,
    _mb_btts,
    _mb_event,
    _mb_ftts,
    _mb_match_odds,
    _mb_totals,
    _series,
)
from sports_hedge.application.capture_replay import FORBIDDEN_WRITE_METHODS
from test_issue324_four_market_sibling_convergence import HotOverlapMatchbook
from test_issue293_owner_live_overlap import OverlapKalshi, OverlapMatchbook

NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
INCOMPLETE = "See contract URL."
LOCKED_FAMILIES = {
    "match_result",
    "both_teams_to_score",
    "total_goals",
    "first_team_to_score",
}


def test_paper_boundary_and_timeouts_unchanged() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    assert Settings.model_fields["paper_scan_cycle_timeout_seconds"].default == 45
    assert Settings.model_fields["paper_scan_hot_cycle_timeout_seconds"].default == 25
    assert Settings.model_fields["paper_scan_provider_timeout_seconds"].default == 8
    for client in (MatchbookClient, KalshiClient, PolymarketClient):
        for method in FORBIDDEN_WRITE_METHODS:
            assert not hasattr(client, method)
    assert REGISTRY_VERSION == "v4"
    assert REGISTRY_PARENT_COMMIT == "cf541aa857b19efb2d96cce54489c916bc1a38ba"


def test_registry_marks_all_four_paper_assumed_operational() -> None:
    for archetype in (
        CatalogueArchetype.MATCH_RESULT_1X2,
        CatalogueArchetype.BOTH_TEAMS_TO_SCORE,
        CatalogueArchetype.TOTAL_GOALS_HALF_LINE,
        CatalogueArchetype.FIRST_TEAM_TO_SCORE,
    ):
        cell = registry_cell(archetype, "matchbook_kalshi")
        assert cell.phase1_four_family is True
        assert cell.paper_assumed_operational is True
        assert cell.venue_available is True


def _assert_paper_assumed(left: PayloadSide, right: PayloadSide) -> None:
    assessment = classify_payload_pair(left, right)
    assert assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert assessment.state is not CatalogueApprovalState.APPROVED_EQUIVALENT
    assert assessment.paper_mode_admitted is True
    assert assessment.execution_eligible is False
    assert assessment.settlement_assumption == "regulation_time"
    assert OWNER_APPROVED_PAPER_EQUIVALENCE_REASON in assessment.notes
    mb = normalize_payload_side(left)
    kalshi = normalize_payload_side(right)
    assert paper_assumed_locked_family(mb, kalshi) is True
    match = MarketMatcher().match(mb, kalshi)
    assert match.matched is True
    assert "paper_assumed_equivalent" in match.reasons
    assert catalogue_allows_solver(mb, kalshi) is True
    assert catalogue_allows_live_execution(mb, kalshi) is False
    assert scan_eligible_pair(mb, kalshi, match) is True
    assert solver_model_for_pair(mb, kalshi) is not None
    admission = assess_catalogue_admission(mb, kalshi)
    assert admission.allowed is True
    assert admission.live_execution_eligible is False


def test_1x2_fair_price_is_paper_assumed_not_approved() -> None:
    left = PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_mb_match_odds()])
    event = _kalshi_game_event(rules=REGULATION, secondary=CANCEL_RESCHEDULE_FAIR_PRICE)
    right = PayloadSide(
        venue=VenueName.KALSHI,
        event=event,
        markets=list(event["markets"]),
        series=_series("KXEPLGAME"),
    )
    _assert_paper_assumed(left, right)
    assessment = classify_payload_pair(left, right)
    assert FAIR_PRICE_PAPER_ADMITTED_REASON in assessment.notes


def test_btts_incomplete_settlement_is_paper_assumed() -> None:
    left = PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_mb_btts()])
    event = _kalshi_btts_event()
    event["markets"][0]["rules_primary"] = INCOMPLETE
    right = PayloadSide(
        venue=VenueName.KALSHI,
        event=event,
        markets=list(event["markets"]),
        series=_series("KXEPLBTTS"),
    )
    _assert_paper_assumed(left, right)


def test_exact_line_total_incomplete_settlement_is_paper_assumed() -> None:
    left = PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_mb_totals("2.5")])
    event = _kalshi_total_event("2.5")
    event["markets"][0]["rules_primary"] = INCOMPLETE
    right = PayloadSide(
        venue=VenueName.KALSHI,
        event=event,
        markets=list(event["markets"]),
        series=_series("KXEPLTOTAL"),
    )
    _assert_paper_assumed(left, right)


def test_ftts_incomplete_settlement_is_paper_assumed_when_three_states_listed() -> None:
    left = PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_mb_ftts()])
    event = _kalshi_ftts_event()
    for market in event["markets"]:
        market["rules_primary"] = "Winner of the match."
    right = PayloadSide(
        venue=VenueName.KALSHI,
        event=event,
        markets=list(event["markets"]),
        series=_series("KXEPLFTTS"),
    )
    _assert_paper_assumed(left, right)


def test_identity_gates_remain_fail_closed() -> None:
    mb = PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_mb_totals("2.5")])
    wrong_line = _kalshi_total_event("3.5")
    totals = classify_payload_pair(
        mb,
        PayloadSide(
            venue=VenueName.KALSHI,
            event=wrong_line,
            markets=list(wrong_line["markets"]),
            series=_series("KXEPLTOTAL"),
        ),
    )
    assert totals.state is CatalogueApprovalState.APPROVED_PARAMETER_MISMATCH
    et = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_mb_match_odds()]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=_kalshi_game_event(rules=ET_RULES),
            markets=list(_kalshi_game_event(rules=ET_RULES)["markets"]),
            series=_series("KXEPLGAME"),
        ),
    )
    assert et.state is CatalogueApprovalState.UNSUPPORTED
    assert et.paper_mode_admitted is False
    assert et.state is not CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert et.state is not CatalogueApprovalState.APPROVED_EQUIVALENT
    incomplete_1x2 = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_mb_match_odds()]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=_kalshi_game_event(rules=GAMEWIN_TEMPLATE, drop_draw=True),
            markets=list(_kalshi_game_event(rules=GAMEWIN_TEMPLATE, drop_draw=True)["markets"]),
            series=_series("KXEPLGAME"),
        ),
    )
    assert incomplete_1x2.state is not CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert incomplete_1x2.state is not CatalogueApprovalState.APPROVED_EQUIVALENT
    integer = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_mb_totals("2.0")]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=_kalshi_total_event("2.0"),
            markets=list(_kalshi_total_event("2.0")["markets"]),
            series=_series("KXEPLTOTAL"),
        ),
    )
    assert integer.state is not CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert integer.state is not CatalogueApprovalState.APPROVED_EQUIVALENT
    missing_no_goal = _kalshi_ftts_event()
    missing_no_goal["markets"] = missing_no_goal["markets"][:2]
    ftts = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_mb_ftts()]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=missing_no_goal,
            markets=list(missing_no_goal["markets"]),
            series=_series("KXEPLFTTS"),
        ),
    )
    assert ftts.state is not CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert ftts.state is not CatalogueApprovalState.APPROVED_EQUIVALENT


def test_polymarket_structural_pairs_stay_paper_admitted_without_settlement_text() -> None:
    one_x_two = classify_payload_pair(
        _mb([_mb_1x2()]),
        _pm([_pm_1x2(description="See market rules.")]),
    )
    assert one_x_two.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert one_x_two.paper_mode_admitted is True
    assert one_x_two.execution_eligible is False
    btts = classify_payload_pair(
        _mb([_census_mb_btts()]),
        _pm([_pm_btts(description="See market rules.")]),
    )
    assert btts.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert btts.paper_mode_admitted is True
    assert btts.execution_eligible is False


def _incomplete_kalshi_events() -> list[dict[str, Any]]:
    game = _kalshi_game_event(rules=REGULATION, secondary=CANCEL_RESCHEDULE_FAIR_PRICE)
    btts = _kalshi_btts_event()
    btts["markets"][0]["rules_primary"] = INCOMPLETE
    total = _kalshi_total_event("2.5")
    total["markets"][0]["rules_primary"] = INCOMPLETE
    ftts = _kalshi_ftts_event()
    for market in ftts["markets"]:
        market["rules_primary"] = "Winner of the match."
    return [game, btts, total, ftts]


@pytest.mark.asyncio
async def test_four_families_reach_inventory_solver_hot_and_read_model() -> None:
    matchbook = HotOverlapMatchbook(
        [_mb_event()],
        {str(MB_EVENT_ID): [_mb_match_odds(), _mb_btts(), _mb_totals("2.5"), _mb_ftts()]},
    )
    kalshi = OverlapKalshi(
        _incomplete_kalshi_events(),
        series_by_ticker={
            "KXEPLGAME": _series("KXEPLGAME"),
            "KXEPLBTTS": _series("KXEPLBTTS"),
            "KXEPLTOTAL": _series("KXEPLTOTAL"),
            "KXEPLFTTS": _series("KXEPLFTTS"),
        },
        books=_all_books(),
    )
    report = await _collect(matchbook, kalshi)
    matched = [
        item
        for item in report.discovered_fixtures
        if item.matchbook_matched and item.kalshi_matched
    ]
    assert len(matched) == 1
    fixture = matched[0]
    assert fixture.matched_equivalent_count == 4
    rows = report.fixture_markets[fixture.canonical_event_id]
    comparable = {
        row.family: row
        for row in rows
        if inventory_is_comparable_opportunity(row.comparison_status)
    }
    assert set(comparable) == LOCKED_FAMILIES
    assert comparable["match_result"].comparison_status is InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT
    assert comparable["both_teams_to_score"].comparison_status is InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT
    assert comparable["total_goals"].comparison_status is InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT
    assert comparable["total_goals"].line == Decimal("2.5")
    assert comparable["first_team_to_score"].comparison_status is InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT
    for row in comparable.values():
        assert row.entered_solver is True or row.solver_model is not None

    coverage = fixture.catalogue_coverage or fixture_catalogue_coverage(
        rows,
        matchbook_matched=True,
        kalshi_matched=True,
        target_competition_code="premier_league",
    )
    by_label = {row.display_label: row for row in coverage.rows}
    assert by_label["1X2"].state is CatalogueCoverageState.PAPER_ASSUMED_EQUIVALENT
    assert by_label["BTTS"].state is CatalogueCoverageState.PAPER_ASSUMED_EQUIVALENT
    assert by_label["FTTS"].state is CatalogueCoverageState.PAPER_ASSUMED_EQUIVALENT
    total_row = next(row for row in coverage.rows if row.display_label.startswith("TOTAL"))
    assert total_row.state is CatalogueCoverageState.PAPER_ASSUMED_EQUIVALENT
    assert by_label["DNB"].state is CatalogueCoverageState.VENUE_UNAVAILABLE

    matched_decisions = [item for item in report.paper_decisions if item.market_match.matched]
    assert len(matched_decisions) >= 4
    opportunity_ids = {
        item.canonical_market_id for item in matched_decisions if item.canonical_market_id
    }
    assert len(opportunity_ids) >= 4
    for decision in matched_decisions:
        assert "catalogue_review_required" not in decision.rejection_reasons
        assert not any(reason.startswith("catalogue_") for reason in decision.rejection_reasons)
        assert decision.solver_model is not None

    relationships = relationships_from_fixture_markets(report.fixture_markets)
    persisted = [item for group in relationships.values() for item in group]
    assert {item.family for item in persisted} == LOCKED_FAMILIES
    proofs = {item.family: item.proof_status for item in persisted}
    assert proofs["match_result"] == InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT.value
    assert proofs["both_teams_to_score"] == InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT.value
    assert proofs["total_goals"] == InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT.value
    assert proofs["first_team_to_score"] == InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT.value

    store = FixtureCurrentStateStore()
    store.upsert_from_report(report, scan_lane=ScanLane.UNIVERSE, now=NOW)
    unique, _lifecycle, _promoted = store.hot_membership_breakdown(NOW)
    assert unique == 1

    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=_DisabledPolymarket(),
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        matchbook.list_events_calls = 0
        matchbook.list_markets_calls.clear()
        matchbook.get_market_calls.clear()
        kalshi.list_events_calls = 0
        kalshi.list_markets_calls.clear()
        kalshi.book_calls.clear()
        hot = await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            scan_lane=ScanLane.HOT.value,
            identity_scope=[fixture.canonical_event_id],
            known_source_events=report.fixture_source_events,
            hot_market_relationships=relationships,
            unbounded_cycle=True,
        )
    finally:
        repository.close()

    assert matchbook.list_events_calls == 0
    assert kalshi.list_events_calls == 0
    assert matchbook.list_markets_calls == []
    assert kalshi.list_markets_calls == []
    persisted_mb_ids = {
        str(item.matchbook.source_market_id)
        for item in persisted
        if item.matchbook is not None
    }
    assert {call[1] for call in matchbook.get_market_calls} <= persisted_mb_ids
    persisted_kalshi_ids = {
        str(contract)
        for item in persisted
        if item.kalshi is not None
        for contract in (
            item.kalshi.constituent_contract_ids
            or ([item.kalshi.source_market_id] if item.kalshi.source_market_id else [])
        )
    }
    assert kalshi.book_calls
    assert set(kalshi.book_calls) <= persisted_kalshi_ids
    hot_fixture = next(
        item for item in hot.discovered_fixtures if item.canonical_event_id == fixture.canonical_event_id
    )
    assert hot_fixture.market_evaluation_state == MarketEvaluationState.EVALUATED.value
    diagnostics = hot.scan_diagnostics.get("hot_targeted_refresh") or {}
    assert diagnostics.get("missing", 0) == 0
    assert Settings().sports_hedge_execution_enabled is False


@pytest.mark.asyncio
async def test_absent_ftts_stays_venue_unavailable_and_is_not_fabricated() -> None:
    matchbook = OverlapMatchbook(
        [_mb_event()],
        {str(MB_EVENT_ID): [_mb_match_odds(), _mb_btts(), _mb_totals("2.5"), _mb_ftts()]},
    )
    kalshi = OverlapKalshi(
        [
            _kalshi_game_event(rules=GAMEWIN_TEMPLATE),
            _kalshi_btts_event(),
            _kalshi_total_event("2.5"),
        ],
        series_by_ticker={
            "KXEPLGAME": _series("KXEPLGAME"),
            "KXEPLBTTS": _series("KXEPLBTTS"),
            "KXEPLTOTAL": _series("KXEPLTOTAL"),
        },
        books=_all_books(),
    )
    report = await _collect(matchbook, kalshi)
    fixture = next(
        item
        for item in report.discovered_fixtures
        if item.matchbook_matched and item.kalshi_matched
    )
    rows = report.fixture_markets[fixture.canonical_event_id]
    comparable = {
        row.family
        for row in rows
        if inventory_is_comparable_opportunity(row.comparison_status)
    }
    assert "first_team_to_score" not in comparable
    coverage = fixture.catalogue_coverage or fixture_catalogue_coverage(
        rows,
        matchbook_matched=True,
        kalshi_matched=True,
        target_competition_code="premier_league",
    )
    by_label = {row.display_label: row for row in coverage.rows}
    assert by_label["FTTS"].state in {
        CatalogueCoverageState.VENUE_UNAVAILABLE,
        CatalogueCoverageState.NOT_LISTED,
    }
    relationships = [
        item
        for group in relationships_from_fixture_markets(report.fixture_markets).values()
        for item in group
    ]
    assert "first_team_to_score" not in {item.family for item in relationships}
