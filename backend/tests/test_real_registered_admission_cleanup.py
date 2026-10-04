"""Registered admission is catalogue-eligible. Historical paper labels are not a veto.

Fixture markets only. This module does not enable execution or call a venue write.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from test_ncaab_paper_onboard import _gw_market, _ncaab_event
from test_nfl_stage1b_paper_markets import (
    _clone_mb_spread,
    _clone_mb_total,
    _mb_indkc,
    _mb_market,
    _normalize_kalshi_game,
    _normalize_kalshi_spread,
    _normalize_kalshi_total,
    _normalize_mb_family,
)

from sports_hedge.catalogue.admission import (
    catalogue_allows_live_execution,
    catalogue_allows_solver,
)
from sports_hedge.catalogue.classify import classify_pair, normalize_payload_side
from sports_hedge.catalogue.corpus import census_corpus
from sports_hedge.catalogue.legacy_paper_labels import (
    LEGACY_STORED_REGISTERED_EQUIVALENT,
    legacy_label_blocks_real_execution,
    semantic_admission_label,
)
from sports_hedge.catalogue.states import CatalogueApprovalState
from sports_hedge.config import Settings
from sports_hedge.domain.football import FootballPeriod
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.approved_register import registered_canonical_key
from sports_hedge.nba.settlement import nba_paper_audit_reasons
from sports_hedge.ncaab.register import ncaab_registered_canonical_key
from sports_hedge.ncaab.settlement import ncaab_paper_audit_reasons
from sports_hedge.nfl.constants import NFL_NOT_LIVE_EXECUTION_REASON
from sports_hedge.nfl.settlement import nfl_paper_audit_reasons


def _corpus_markets(entry_id: str):
    entry = next(item for item in census_corpus() if item.entry_id == entry_id)
    return normalize_payload_side(entry.left), normalize_payload_side(entry.right)


def test_registered_football_relationship_stays_execution_eligible() -> None:
    left, right = _corpus_markets("bad-1x2-mb-k-gamewin-unknown")
    assert registered_canonical_key(left, right) == "MATCH_RESULT_FT"
    assert catalogue_allows_solver(left, right) is True
    assert catalogue_allows_live_execution(left, right) is True
    assessment = classify_pair(left, right)
    assert assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert assessment.execution_eligible is True
    assert semantic_admission_label(assessment.state.value) == "registered_equivalent"


def test_registered_nfl_game_winner_spread_and_total_stay_execution_eligible() -> None:
    _, game = _normalize_kalshi_game()
    _, spread = _normalize_kalshi_spread()
    _, total = _normalize_kalshi_total()
    _, mb_game = _normalize_mb_family(_mb_market(_mb_indkc(), name="Moneyline"))
    _, mb_spread = _normalize_mb_family(_clone_mb_spread(_mb_indkc(), home_line=Decimal("-6.5")))
    _, mb_total = _normalize_mb_family(_clone_mb_total(_mb_indkc(), line=Decimal("47.5")))
    for left, right in ((game, mb_game), (spread, mb_spread), (total, mb_total)):
        assert registered_canonical_key(left, right) is not None
        assert catalogue_allows_live_execution(left, right) is True
        assert catalogue_allows_solver(left, right) is True


def test_unsupported_nfl_period_and_mismatched_lines_stay_rejected() -> None:
    _, spread = _normalize_kalshi_spread()
    _, mb_spread = _normalize_mb_family(_clone_mb_spread(_mb_indkc(), home_line=Decimal("-6.5")))
    first_half = spread.model_copy(update={"period": FootballPeriod.FIRST_HALF})
    assert catalogue_allows_live_execution(first_half, mb_spread) is False
    _, other_line = _normalize_mb_family(_mb_market(_mb_indkc(), name="Handicap", handicap=4.5))
    assert catalogue_allows_live_execution(spread, other_line) is False
    _, total = _normalize_kalshi_total()
    _, other_total = _normalize_mb_family(_clone_mb_total(_mb_indkc(), line=Decimal("41.5")))
    assert catalogue_allows_live_execution(total, other_total) is False


def test_unregistered_and_contradictory_relationships_stay_ineligible() -> None:
    _, game = _normalize_kalshi_game()
    _, total = _normalize_kalshi_total()
    assert registered_canonical_key(game, total) is None
    assert catalogue_allows_live_execution(game, total) is False
    left, right = _corpus_markets("bad-1x2-et-contradiction")
    assert catalogue_allows_live_execution(left, right) is False
    assert catalogue_allows_solver(left, right) is False


def test_ncaab_stays_blocked_without_an_admitted_venue_pair() -> None:
    home, away = "Xavier Musketeers", "Duke Blue Devils"
    left = _gw_market(
        _ncaab_event(home=home, away=away, venue=VenueName.KALSHI, source_id="k-1"),
        VenueName.KALSHI,
        "k-ml",
    )
    right = _gw_market(
        _ncaab_event(home=home, away=away, venue=VenueName.POLYMARKET, source_id="pm-1"),
        VenueName.POLYMARKET,
        "pm-ml",
    )
    assert ncaab_registered_canonical_key(left, right) is None
    assert catalogue_allows_live_execution(left, right) is False
    assert catalogue_allows_solver(left, right) is False
    reasons = ncaab_paper_audit_reasons()
    assert "ncaab_venue_pair_family_not_evidence_backed" in reasons
    assert "ncaab_paper_not_live_execution_equivalent" not in reasons


def test_legacy_paper_labels_are_readable_and_are_not_a_real_veto() -> None:
    assert semantic_admission_label(LEGACY_STORED_REGISTERED_EQUIVALENT) == "registered_equivalent"
    assert semantic_admission_label("PAPER_ASSUMED_EQUIVALENT") == "registered_equivalent"
    assert legacy_label_blocks_real_execution(NFL_NOT_LIVE_EXECUTION_REASON) is False
    assert legacy_label_blocks_real_execution("nba_paper_not_live_execution_equivalent") is False
    assert "nfl_paper_not_live_execution_equivalent" not in nfl_paper_audit_reasons()
    assert "nba_paper_not_live_execution_equivalent" not in nba_paper_audit_reasons()
    with pytest.raises(ValueError):
        legacy_label_blocks_real_execution("line_mismatch")


def test_execution_stays_disabled_and_this_module_places_no_order() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
