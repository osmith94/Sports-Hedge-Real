"""Issue #268 catalogue census v1: pairwise approval, corpus, coverage.

Deterministic fixture/demo only. Does not change matcher admission, HOT/UNIVERSE
concurrency, paper autofill, or execution.
"""

from __future__ import annotations

import inspect
from pathlib import Path

from sports_hedge.catalogue.classify import (
    CataloguePairAssessment,
    classify_pair,
    classify_payload_pair,
)
from sports_hedge.catalogue.corpus import (
    CENSUS_KICKOFF,
    GAMEWIN_TEMPLATE,
    KALSHI_GAMEWIN_SERIES,
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
from sports_hedge.matching.ordinary_1x2 import allow_unknown_settlement_for_ordinary_1x2

CENSUS_DOC = Path(__file__).resolve().parents[2] / "docs" / "APPROVED_MARKET_CATALOGUE_CENSUS_V1.md"


def test_classifier_has_no_scan_lane_and_is_shared_by_hot_and_universe() -> None:
    assert "scan_lane" not in inspect.signature(classify_pair).parameters
    assert "scan_lane" not in inspect.signature(classify_payload_pair).parameters
    sample = next(item for item in census_corpus() if item.entry_id == "good-1x2-mb-pm")
    assessment = classify_payload_pair(sample.left, sample.right)
    assert assessment.catalogue_shared_by == ("hot", "universe")
    assert assessment.execution_eligible is False
    assert assessment.data_class == "deterministic_fixture"


def test_execution_remains_disabled() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False


def test_pairwise_matrix_covers_four_archetypes_and_three_pairs() -> None:
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


def test_corpus_classifications_match_expected_states() -> None:
    corpus = census_corpus()
    assert len(corpus) >= 20
    known_good = [item for item in corpus if item.known_kind == "known_good"]
    known_bad = [item for item in corpus if item.known_kind == "known_bad"]
    assert len(known_good) == 13
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
        else:
            assert assessment.state is not CatalogueApprovalState.APPROVED_EQUIVALENT


def test_gamewin_unknown_1x2_is_review_required_despite_matcher_admission() -> None:
    entry = next(
        item for item in census_corpus() if item.entry_id == "bad-1x2-mb-k-gamewin-unknown"
    )
    assessment = classify_payload_pair(entry.left, entry.right)
    assert assessment.state is CatalogueApprovalState.REVIEW_REQUIRED
    assert assessment.reason == "incomplete_settlement"
    assert assessment.matcher_matched is True
    assert assessment.matcher_admits_unknown_1x2 is True
    assert assessment.solver_model == "simple_complete_set"
    assert assessment.known_conflict_with_current_matcher is True
    from sports_hedge.catalogue.classify import normalize_payload_side

    matchbook = normalize_payload_side(entry.left)
    kalshi = normalize_payload_side(entry.right)
    assert allow_unknown_settlement_for_ordinary_1x2(matchbook, kalshi) is True
    assert kalshi.settlement.is_economically_complete() is False


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
        "bad-1x2-mb-k-gamewin-unknown",
        "bad-1x2-mb-pm-unknown-settlement",
        "bad-btts-k-ambiguous-rules",
        "bad-ftts-missing-no-goal-both",
        "bad-ftts-k-unproven-regulation",
    }
    by_id = {item.entry_id: item for item in census_corpus()}
    for entry_id in ids:
        assessment = classify_payload_pair(by_id[entry_id].left, by_id[entry_id].right)
        assert assessment.state is CatalogueApprovalState.REVIEW_REQUIRED, entry_id


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
    assert report.known_good_retained == 13
    assert report.matcher_catalogue_conflicts == 1
    assert report.conflict_entry_ids == ["bad-1x2-mb-k-gamewin-unknown"]
    assert report.families["match_result_1x2"].after_approved == 3
    assert report.families["match_result_1x2"].before_solver_admitted == 4
    rendered = render_coverage_markdown(report)
    assert "GAMEWIN-unknown" in " ".join(report.notes)
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
    assert assessment.state is CatalogueApprovalState.REVIEW_REQUIRED
    assert classify_payload_pair(_mb([_mb_1x2()]), _pm([_pm_1x2()])).state is (
        CatalogueApprovalState.APPROVED_EQUIVALENT
    )
