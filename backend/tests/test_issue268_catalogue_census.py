"""Issue #268 catalogue census v1: pairwise approval, corpus, production gate.

Deterministic fixture/demo plus cited captured public payloads. Matcher
recognition, HOT/UNIVERSE concurrency, paper autofill, and execution are
unchanged. Solver/paper admission requires APPROVED_EQUIVALENT or the
Issue #326 PAPER_ASSUMED_EQUIVALENT locked-family exception.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from venue_cost_helpers import matchbook_polymarket_costs

from sports_hedge.application.complete_set import scan_eligible_pair
from sports_hedge.application.market_observation import (
    KalshiObservationBuilder,
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.catalogue.admission import (
    assess_catalogue_admission,
    catalogue_allows_live_execution,
    catalogue_allows_solver,
)
from sports_hedge.catalogue.classify import (
    CataloguePairAssessment,
    PayloadSide,
    classify_pair,
    classify_payload_pair,
    normalize_payload_side,
)
from sports_hedge.catalogue.corpus import (
    CENSUS_KICKOFF,
    GAMEWIN_TEMPLATE,
    KALSHI_GAMEWIN_SERIES,
    MB_EVENT,
    PM_EVENT,
    _kalshi,
    _kalshi_1x2,
    _mb,
    _mb_1x2,
    _pm,
    _pm_1x2,
    census_corpus,
)
from sports_hedge.catalogue.coverage import coverage_from_corpus, render_coverage_markdown
from sports_hedge.catalogue.matrix import PAIRWISE_MATRIX, matrix_cell, render_matrix_markdown
from sports_hedge.catalogue.states import CatalogueApprovalState, CatalogueArchetype
from sports_hedge.config import Settings
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
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.matching.ordinary_1x2 import allow_unknown_settlement_for_ordinary_1x2
from sports_hedge.normalization.venues import VenueNormalizationError
from sports_hedge.paper.models import FxRateSnapshot

FIXTURES = Path(__file__).resolve().parent / "fixtures"
PINNED_GAMMA_934146 = FIXTURES / "polymarket_gamma_event_934146_chelsea_hull.json"
PINNED_MATCHBOOK_CHELSEA_HULL = FIXTURES / "matchbook_event_chelsea_hull.json"
OBSERVED = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)

CENSUS_DOC = Path(__file__).resolve().parents[2] / "docs" / "APPROVED_MARKET_CATALOGUE_CENSUS_V1.md"


def test_classifier_has_no_scan_lane_and_is_shared_by_hot_and_universe() -> None:
    assert "scan_lane" not in inspect.signature(classify_pair).parameters
    assert "scan_lane" not in inspect.signature(classify_payload_pair).parameters
    assert "scan_lane" not in inspect.signature(assess_catalogue_admission).parameters
    assert "scan_lane" not in inspect.signature(scan_eligible_pair).parameters
    sample = next(item for item in census_corpus() if item.entry_id == "good-1x2-mb-pm")
    assessment = classify_payload_pair(sample.left, sample.right)
    assert assessment.catalogue_shared_by == ("hot", "universe")
    assert assessment.execution_eligible is False
    assert assessment.data_class == "deterministic_fixture"
    admission = assess_catalogue_admission(
        normalize_payload_side(sample.left),
        normalize_payload_side(sample.right),
    )
    assert admission.catalogue_shared_by == ("hot", "universe")
    assert admission.allowed is True


def test_execution_remains_disabled() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False


def test_pairwise_matrix_covers_intended_catalogue_and_three_pairs() -> None:
    pairs = {"matchbook_kalshi", "matchbook_polymarket", "kalshi_polymarket"}
    archetypes = set(CatalogueArchetype)
    observed = {(cell.archetype, cell.venue_pair) for cell in PAIRWISE_MATRIX}
    expected = {(archetype, pair) for archetype in archetypes for pair in pairs}
    assert observed == expected
    assert matrix_cell(
        CatalogueArchetype.TOTAL_GOALS_INTEGER, "matchbook_kalshi"
    ).state is CatalogueApprovalState.UNSUPPORTED
    assert matrix_cell(
        CatalogueArchetype.MATCH_RESULT_1X2, "matchbook_polymarket"
    ).state is CatalogueApprovalState.APPROVED_EQUIVALENT
    assert matrix_cell(
        CatalogueArchetype.TEAM_TOTAL_GOALS, "matchbook_polymarket"
    ).state is CatalogueApprovalState.REVIEW_REQUIRED
    assert matrix_cell(
        CatalogueArchetype.HANDICAP, "matchbook_polymarket"
    ).state is CatalogueApprovalState.UNSUPPORTED
    assert matrix_cell(
        CatalogueArchetype.DRAW_NO_BET, "matchbook_polymarket"
    ).state is CatalogueApprovalState.APPROVED_EQUIVALENT
    assert matrix_cell(
        CatalogueArchetype.DOUBLE_CHANCE, "matchbook_polymarket"
    ).state is CatalogueApprovalState.UNSUPPORTED
    assert matrix_cell(
        CatalogueArchetype.TEAM_TO_SCORE, "matchbook_polymarket"
    ).state is CatalogueApprovalState.UNSUPPORTED
    assert matrix_cell(
        CatalogueArchetype.TEAM_CLEAN_SHEET, "matchbook_polymarket"
    ).state is CatalogueApprovalState.UNSUPPORTED


def test_corpus_classifications_match_expected_states() -> None:
    corpus = census_corpus()
    assert len(corpus) >= 20
    known_good = [item for item in corpus if item.known_kind == "known_good"]
    known_bad = [item for item in corpus if item.known_kind == "known_bad"]
    assert len(known_good) == 14
    assert known_bad
    for entry in corpus:
        assessment = classify_payload_pair(entry.left, entry.right)
        assert assessment.state is entry.expected_state, (
            f"{entry.entry_id}: expected {entry.expected_state}, got "
            f"{assessment.state} ({assessment.reason})"
        )
        if entry.known_kind == "known_good":
            assert assessment.state is CatalogueApprovalState.APPROVED_EQUIVALENT
            assert assessment.solver_model is not None
            assert assessment.settlement_complete is True
            assert assessment.paper_mode_admitted is True
            assert assessment.execution_eligible is False
        elif entry.known_kind == "paper_assumed":
            assert assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
            assert assessment.settlement_complete is False
            assert assessment.paper_mode_admitted is True
            assert assessment.execution_eligible is False
            assert assessment.settlement_assumption == "regulation_time"
        else:
            assert assessment.state is not CatalogueApprovalState.APPROVED_EQUIVALENT
            assert assessment.state is not CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT


def test_gamewin_unknown_1x2_is_paper_assumed_not_settlement_proven() -> None:
    entry = next(
        item for item in census_corpus() if item.entry_id == "bad-1x2-mb-k-gamewin-unknown"
    )
    assessment = classify_payload_pair(entry.left, entry.right)
    assert assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert assessment.reason == "paper_assumed_equivalent"
    assert assessment.matcher_matched is True
    assert assessment.matcher_admits_unknown_1x2 is True
    assert assessment.solver_model == "simple_complete_set"
    assert assessment.known_conflict_with_current_matcher is False
    assert assessment.paper_mode_admitted is True
    assert assessment.execution_eligible is False
    assert assessment.settlement_assumption == "regulation_time"
    matchbook = normalize_payload_side(entry.left)
    kalshi = normalize_payload_side(entry.right)
    assert allow_unknown_settlement_for_ordinary_1x2(matchbook, kalshi) is True
    assert kalshi.settlement.is_economically_complete() is False
    assert catalogue_allows_solver(matchbook, kalshi) is True
    assert catalogue_allows_live_execution(matchbook, kalshi) is False


def test_high_confidence_does_not_approve_incomplete_settlement() -> None:
    event_mb = CanonicalEvent(
        competition="Premier League",
        home_team="Tottenham",
        away_team="Everton",
        kickoff_utc=CENSUS_KICKOFF,
        source_venue=VenueName.MATCHBOOK,
        source_event_id="mb-conf",
        confidence=0.99,
    )
    event_pm = event_mb.model_copy(
        update={"source_venue": VenueName.POLYMARKET, "source_event_id": "pm-conf"}
    )
    runners = [
        CanonicalRunner(source_runner_id="h", outcome=CanonicalOutcome.HOME, label="Tottenham"),
        CanonicalRunner(source_runner_id="d", outcome=CanonicalOutcome.DRAW, label="Draw"),
        CanonicalRunner(source_runner_id="a", outcome=CanonicalOutcome.AWAY, label="Everton"),
    ]
    complete = SettlementFingerprint(
        scope=SettlementScope.REGULATION_TIME,
        period=FootballPeriod.FULL_TIME,
        extra_time_included=False,
        penalties_included=False,
        push_possible=False,
    )
    incomplete = SettlementFingerprint(
        scope=SettlementScope.UNKNOWN,
        period=FootballPeriod.FULL_TIME,
        extra_time_included=None,
        penalties_included=None,
        push_possible=False,
    )
    left = CanonicalMarket(
        event=event_mb,
        source_venue=VenueName.MATCHBOOK,
        source_market_id="mb-1x2",
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        settlement=complete,
        runners=runners,
        confidence=0.99,
    )
    right = CanonicalMarket(
        event=event_pm,
        source_venue=VenueName.POLYMARKET,
        source_market_id="pm-1x2",
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        settlement=incomplete,
        runners=runners,
        confidence=0.99,
    )
    assessment = classify_pair(left, right)
    assert assessment.state is CatalogueApprovalState.REVIEW_REQUIRED
    assert assessment.reason == "incomplete_settlement"
    assert assessment.matcher_matched is False


def test_review_required_examples_are_explicit() -> None:
    ids = {
        "bad-1x2-mb-pm-unknown-settlement",
        "bad-ftts-missing-no-goal-both",
        "review-team-total-mb-pm",
    }
    by_id = {item.entry_id: item for item in census_corpus()}
    for entry_id in ids:
        assessment = classify_payload_pair(by_id[entry_id].left, by_id[entry_id].right)
        assert assessment.state is CatalogueApprovalState.REVIEW_REQUIRED, entry_id
    paper_assumed_ids = {
        "bad-btts-k-ambiguous-rules",
        "bad-totals-k-ambiguous-rules",
        "bad-ftts-k-unproven-regulation",
        "bad-1x2-mb-k-cancel-reschedule-fair-price",
    }
    for entry_id in paper_assumed_ids:
        assessment = classify_payload_pair(by_id[entry_id].left, by_id[entry_id].right)
        assert assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT, entry_id
        assert assessment.execution_eligible is False


def test_unsupported_and_parameter_and_contradiction_examples() -> None:
    by_id = {item.entry_id: item for item in census_corpus()}
    assert (
        classify_payload_pair(
            by_id["bad-totals-integer-mb-k"].left, by_id["bad-totals-integer-mb-k"].right
        ).state
        is CatalogueApprovalState.UNSUPPORTED
    )
    assert (
        classify_payload_pair(
            by_id["bad-totals-line-mismatch"].left, by_id["bad-totals-line-mismatch"].right
        ).state
        is CatalogueApprovalState.APPROVED_PARAMETER_MISMATCH
    )
    assert (
        classify_payload_pair(
            by_id["bad-1x2-et-contradiction"].left, by_id["bad-1x2-et-contradiction"].right
        ).state
        is CatalogueApprovalState.KNOWN_CONTRADICTION
    )
    assert (
        classify_payload_pair(
            by_id["bad-ftts-asymmetric-no-goal"].left,
            by_id["bad-ftts-asymmetric-no-goal"].right,
        ).state
        is CatalogueApprovalState.KNOWN_CONTRADICTION
    )
    assert (
        classify_payload_pair(
            by_id["bad-ftts-player-goalscorer"].left,
            by_id["bad-ftts-player-goalscorer"].right,
        ).state
        is CatalogueApprovalState.UNSUPPORTED
    )
    assert (
        classify_payload_pair(
            by_id["bad-dnb-mb-k"].left, by_id["bad-dnb-mb-k"].right
        ).state
        is CatalogueApprovalState.UNSUPPORTED
    )
    assert (
        classify_payload_pair(
            by_id["bad-double-chance-mb-pm"].left, by_id["bad-double-chance-mb-pm"].right
        ).state
        is CatalogueApprovalState.UNSUPPORTED
    )
    assert (
        classify_payload_pair(
            by_id["good-dnb-mb-pm"].left, by_id["good-dnb-mb-pm"].right
        ).state
        is CatalogueApprovalState.APPROVED_EQUIVALENT
    )


def test_kalshi_ftts_and_btts_pairs_are_approved_when_complete() -> None:
    by_id = {item.entry_id: item for item in census_corpus()}
    for entry_id in ("good-ftts-mb-k", "good-ftts-k-pm", "good-btts-mb-k", "good-btts-k-pm"):
        assessment = classify_payload_pair(by_id[entry_id].left, by_id[entry_id].right)
        assert assessment.state is CatalogueApprovalState.APPROVED_EQUIVALENT, entry_id
        if "ftts" in entry_id:
            assert assessment.solver_model == "generalized_payoff"
        else:
            assert assessment.solver_model == "simple_complete_set"


def test_family_coverage_report_has_no_silent_regressions() -> None:
    report = coverage_from_corpus()
    assert report.execution_enabled is False
    assert report.paper_mode == "paper"
    assert report.data_class == "deterministic_fixture"
    assert report.unexpected_known_good_regressions == 0
    assert report.unexpected_known_bad_approvals == 0
    assert report.known_good_retained == 14
    assert report.paper_assumed_count == 5
    assert report.matcher_catalogue_conflicts == 0
    assert report.conflict_entry_ids == []
    assert report.families["match_result_1x2"].after_approved == 3
    assert report.families["match_result_1x2"].paper_assumed_equivalent == 2
    assert report.families["match_result_1x2"].before_solver_admitted == 5
    assert report.families["both_teams_to_score"].paper_assumed_equivalent == 1
    assert report.families["total_goals_half_line"].paper_assumed_equivalent == 1
    assert report.families["first_team_to_score"].paper_assumed_equivalent == 1
    rendered = render_coverage_markdown(report)
    assert "PAPER_ASSUMED_EQUIVALENT" in " ".join(report.notes)
    assert "match_result_1x2" in rendered


def test_census_document_contains_matrix_and_honesty_labels() -> None:
    text = CENSUS_DOC.read_text(encoding="utf-8")
    assert render_matrix_markdown() in text
    assert "PAPER MODE" in text
    assert "deterministic fixture" in text.casefold() or "DETERMINISTIC FIXTURE" in text
    assert "REVIEW_REQUIRED" in text
    assert "GAMEWIN" in text
    assert "HOT" in text and "UNIVERSE" in text
    assert "execution" in text.casefold()


def test_gamewin_payload_helpers_remain_available_for_review() -> None:
    assessment = classify_payload_pair(
        _mb([_mb_1x2()]),
        _kalshi(_kalshi_1x2(rules=GAMEWIN_TEMPLATE), series=KALSHI_GAMEWIN_SERIES),
    )
    assert isinstance(assessment, CataloguePairAssessment)
    assert assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert classify_payload_pair(_mb([_mb_1x2()]), _pm([_pm_1x2()])).state is (
        CatalogueApprovalState.APPROVED_EQUIVALENT
    )


def _priced_matchbook(market: dict) -> dict:
    priced = dict(market)
    runners = []
    for index, runner in enumerate(market["runners"]):
        item = dict(runner)
        item["prices"] = [
            {"side": "back", "odds": "2.10", "available-amount": "80"},
            {"side": "lay", "odds": "2.20", "available-amount": "80"},
        ]
        if index:
            item["prices"][0]["odds"] = "3.50"
        runners.append(item)
    priced["runners"] = runners
    return priced


def _pm_books(tokens: list[str]) -> dict[str, dict]:
    books: dict[str, dict] = {}
    for token in tokens:
        books[token] = {
            "asset_id": token,
            "bids": [{"price": "0.40", "size": "200"}],
            "asks": [{"price": "0.45", "size": "200"}],
        }
    return books


def _scan_service() -> tuple[PaperScanService, SqliteMarketIntelligenceRepository]:
    repository = SqliteMarketIntelligenceRepository()
    return PaperScanService(MarketIntelligenceService(repository)), repository


def test_approved_catalogue_pair_reaches_solver_eligibility() -> None:
    entry = next(item for item in census_corpus() if item.entry_id == "good-1x2-mb-pm")
    left = normalize_payload_side(entry.left)
    right = normalize_payload_side(entry.right)
    match = MarketMatcher().match(left, right)
    assert match.matched is True
    assert catalogue_allows_solver(left, right) is True
    assert scan_eligible_pair(left, right, match) is True

    service, repository = _scan_service()
    try:
        matchbook = MatchbookObservationBuilder().build(
            MB_EVENT,
            _priced_matchbook(_mb_1x2()),
            observed_at=OBSERVED,
            quote_age_ms=80,
        )
        polymarket = PolymarketObservationBuilder().build(
            PM_EVENT,
            {
                "id": "pm-1x2",
                "question": "Match result?",
                "sportsMarketType": "moneyline",
                "outcomes": '["Tottenham", "Draw", "Everton"]',
                "clobTokenIds": '["h", "d", "a"]',
                "description": "Resolves based on 90 minutes of regulation time.",
            },
            _pm_books(["h", "d", "a"]),
            observed_at=OBSERVED,
            quote_age_ms=90,
        )
        decision = service.scan_pair(
            matchbook,
            polymarket,
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=[
                FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test_fx")
            ],
            maximum_execution_risk=100,
        )
        assert decision.market_match.matched is True
        assert decision.solver_model == "simple_complete_set"
        assert not any(reason.startswith("catalogue_") for reason in decision.rejection_reasons)
        assert Settings().sports_hedge_execution_enabled is False
    finally:
        repository.close()


def test_paper_assumed_gamewin_is_paper_admitted_never_live_execution() -> None:
    entry = next(
        item for item in census_corpus() if item.entry_id == "bad-1x2-mb-k-gamewin-unknown"
    )
    left = normalize_payload_side(entry.left)
    right = normalize_payload_side(entry.right)
    match = MarketMatcher().match(left, right)
    assert match.matched is True
    assert allow_unknown_settlement_for_ordinary_1x2(left, right) is True
    admission = assess_catalogue_admission(left, right)
    assert admission.allowed is True
    assert admission.paper_mode_admitted is True
    assert admission.live_execution_eligible is False
    assert admission.assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert admission.settlement_assumption == "regulation_time"
    assert scan_eligible_pair(left, right, match) is True
    assert catalogue_allows_solver(left, right) is True
    assert catalogue_allows_live_execution(left, right) is False

    service, repository = _scan_service()
    try:
        matchbook = MatchbookObservationBuilder().build(
            MB_EVENT,
            _priced_matchbook(_mb_1x2()),
            observed_at=OBSERVED,
            quote_age_ms=80,
        )
        kalshi = KalshiObservationBuilder().build(
            entry.right.event,
            entry.right.markets,
            {},
            series=entry.right.series,
            observed_at=OBSERVED,
            quote_age_ms=90,
        )
        decision = service.scan_pair(
            matchbook,
            kalshi,
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=[
                FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test_fx")
            ],
            maximum_execution_risk=100,
        )
        assert decision.market_match.matched is True
        assert "catalogue_review_required" not in decision.rejection_reasons
        assert not any(reason.startswith("catalogue_") for reason in decision.rejection_reasons)
        assert Settings().sports_hedge_execution_enabled is False
    finally:
        repository.close()


def test_unsupported_mismatch_and_contradiction_are_blocked_from_solver() -> None:
    by_id = {item.entry_id: item for item in census_corpus()}
    cases = {
        "bad-totals-integer-mb-k": "catalogue_unsupported",
        "bad-totals-line-mismatch": "catalogue_approved_parameter_mismatch",
        "bad-1x2-et-contradiction": "catalogue_known_contradiction",
        "bad-handicap-mb-pm": "catalogue_unsupported",
        "review-team-total-mb-pm": "catalogue_review_required",
    }
    for entry_id, rejection in cases.items():
        entry = by_id[entry_id]
        assessment = classify_payload_pair(entry.left, entry.right)
        assert assessment.state is not CatalogueApprovalState.APPROVED_EQUIVALENT, entry_id
        try:
            left = normalize_payload_side(entry.left)
            right = normalize_payload_side(entry.right)
        except (VenueNormalizationError, ValueError):
            continue
        match = MarketMatcher().match(left, right)
        admission = assess_catalogue_admission(left, right)
        assert admission.allowed is False, entry_id
        assert admission.rejection_reason == rejection, entry_id
        assert scan_eligible_pair(left, right, match) is False, entry_id


def test_hot_and_universe_use_the_same_catalogue_gate() -> None:
    entry = next(
        item for item in census_corpus() if item.entry_id == "bad-1x2-mb-k-gamewin-unknown"
    )
    left = normalize_payload_side(entry.left)
    right = normalize_payload_side(entry.right)
    hot = assess_catalogue_admission(left, right)
    universe = assess_catalogue_admission(left, right)
    assert hot.allowed is True
    assert universe.allowed is True
    assert hot.assessment.state is universe.assessment.state
    assert hot.assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert hot.rejection_reason == universe.rejection_reason
    assert hot.catalogue_shared_by == ("hot", "universe")
    assert universe.catalogue_shared_by == ("hot", "universe")
    source = inspect.getsource(scan_eligible_pair)
    assert "catalogue_allows_solver" in source
    assert "scan_lane" not in inspect.signature(scan_eligible_pair).parameters


def test_pinned_public_gamma_binary_moneyline_is_not_approved_1x2() -> None:
    gamma = json.loads(PINNED_GAMMA_934146.read_text(encoding="utf-8"))
    matchbook = json.loads(PINNED_MATCHBOOK_CHELSEA_HULL.read_text(encoding="utf-8"))
    assert gamma["source"] == "https://gamma-api.polymarket.com/events/934146"
    assert "not authenticated live" in matchbook["identified_as"].casefold() or (
        "not authenticated" in matchbook["identified_as"].casefold()
    )
    mb_event = matchbook["payload"]
    mb_market = next(item for item in mb_event["markets"] if item["name"] == "Match Odds")
    pm_event = gamma["payload"]
    pm_market = pm_event["markets"][0]
    assessment = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=mb_event, markets=[mb_market]),
        PayloadSide(venue=VenueName.POLYMARKET, event=pm_event, markets=[pm_market]),
    )
    assert assessment.state is not CatalogueApprovalState.APPROVED_EQUIVALENT
    assert assessment.state in {
        CatalogueApprovalState.REVIEW_REQUIRED,
        CatalogueApprovalState.UNSUPPORTED,
        CatalogueApprovalState.KNOWN_CONTRADICTION,
    }
