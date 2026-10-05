"""Already-admitted relationships are not rejected only for historical paper policy.

Structural mismatches stay fail-closed. No venue order is submitted.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from test_mlb_stage1 import _moneyline_markets, _total_market
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
from sports_hedge.catalogue.classify import classify_pair
from sports_hedge.catalogue.states import CatalogueApprovalState
from sports_hedge.domain.football import MarketFamily
from sports_hedge.matching.approved_register import registered_canonical_key
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.mlb.constants import (
    CANONICAL_MLB_GAME_WINNER,
    CANONICAL_MLB_TOTAL_RUNS,
    MLB_LINE_MISMATCH_REASON,
    MLB_SETTLEMENT_NOT_EXECUTABLE,
)
from sports_hedge.nfl.constants import (
    CANONICAL_NFL_GAME_WINNER,
    CANONICAL_NFL_POINT_SPREAD,
    CANONICAL_NFL_TOTAL_POINTS,
)
from sports_hedge.normalization.venues import MatchbookNormalizer, VenueNormalizationError
from sports_hedge.paper.result_resolution import _family_blocker
from sports_hedge.paper.trades import PaperTrade, PaperTradeState


def _admitted(left, right) -> None:
    assessment = classify_pair(left, right)
    assert assessment.paper_mode_admitted is True
    assert assessment.execution_eligible is True
    assert catalogue_allows_solver(left, right) is True
    assert catalogue_allows_live_execution(left, right) is True
    assert "never live-execution eligible" not in " ".join(assessment.notes)


def test_mlb_game_winner_is_not_blocked_by_historical_paper_marker() -> None:
    kalshi, polymarket, matchbook = _moneyline_markets()
    assert kalshi.settlement.unknown_reason == MLB_SETTLEMENT_NOT_EXECUTABLE
    assert registered_canonical_key(kalshi, polymarket) == CANONICAL_MLB_GAME_WINNER
    _admitted(kalshi, polymarket)
    _admitted(kalshi, matchbook)
    assert classify_pair(kalshi, polymarket).state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    trade = PaperTrade(
        trade_id="mlb-winner",
        opportunity_id="mlb-winner",
        competition="mlb",
        market_family=MarketFamily.GAME_WINNER,
        state=PaperTradeState.OPEN,
        opened_at=datetime(2026, 9, 24, tzinfo=UTC),
        last_updated_at=datetime(2026, 9, 24, tzinfo=UTC),
    )
    assert _family_blocker(trade) is None


def test_mlb_same_line_half_run_total_is_not_blocked_by_historical_paper_marker() -> None:
    kalshi = _total_market("kalshi", "7.5")
    polymarket = _total_market("polymarket", "7.5")
    matchbook = _total_market("matchbook", "7.5")
    assert registered_canonical_key(kalshi, polymarket) == f"{CANONICAL_MLB_TOTAL_RUNS}:7.5"
    _admitted(kalshi, polymarket)
    _admitted(polymarket, matchbook)
    trade = PaperTrade(
        trade_id="mlb-total",
        opportunity_id="mlb-total",
        competition="mlb",
        market_family=MarketFamily.TOTAL_RUNS,
        line=Decimal("7.5"),
        state=PaperTradeState.OPEN,
        opened_at=datetime(2026, 9, 24, tzinfo=UTC),
        last_updated_at=datetime(2026, 9, 24, tzinfo=UTC),
    )
    assert _family_blocker(trade) is None


def test_structural_mlb_mismatches_remain_blocked() -> None:
    kalshi = _total_market("kalshi", "7.5")
    other = _total_market("polymarket", "8.5")
    assessment = classify_pair(kalshi, other)
    assert assessment.reason == MLB_LINE_MISMATCH_REASON
    assert assessment.execution_eligible is False
    assert catalogue_allows_live_execution(kalshi, other) is False
    assert catalogue_allows_solver(kalshi, other) is False
    whole = kalshi.model_copy(update={"line": Decimal(7)})
    assert registered_canonical_key(whole, other) is None
    assert catalogue_allows_live_execution(whole, _total_market("polymarket", "7.5")) is False
    winner = _moneyline_markets()[0]
    crossed = classify_pair(winner, kalshi)
    assert crossed.execution_eligible is False
    assert catalogue_allows_live_execution(winner, kalshi) is False


def test_nfl_approved_families_remain_admitted() -> None:
    _, game = _normalize_kalshi_game()
    _, spread = _normalize_kalshi_spread()
    _, total = _normalize_kalshi_total()
    _, mb_game = _normalize_mb_family(_mb_market(_mb_indkc(), name="Moneyline"))
    _, mb_spread = _normalize_mb_family(_clone_mb_spread(_mb_indkc(), home_line=Decimal("-6.5")))
    _, mb_total = _normalize_mb_family(_clone_mb_total(_mb_indkc(), line=Decimal("47.5")))
    assert registered_canonical_key(game, mb_game) == CANONICAL_NFL_GAME_WINNER
    assert registered_canonical_key(spread, mb_spread) == f"{CANONICAL_NFL_POINT_SPREAD}:-6.5"
    assert registered_canonical_key(total, mb_total) == f"{CANONICAL_NFL_TOTAL_POINTS}:47.5"
    _admitted(game, mb_game)
    _admitted(spread, mb_spread)
    _admitted(total, mb_total)


def test_nfl_player_prop_false_equivalence_stays_rejected() -> None:
    event = MatchbookNormalizer().normalize_event(_mb_indkc())
    with pytest.raises(VenueNormalizationError, match="truth table"):
        MatchbookNormalizer().normalize_market(
            event,
            {
                "id": "prop-1",
                "name": "First Touchdown Scorer",
                "market-type": "money_line",
                "runners": [
                    {"id": "1", "name": "Kansas City Chiefs"},
                    {"id": "2", "name": "Indianapolis Colts"},
                ],
            },
        )
    _, spread = _normalize_kalshi_spread()
    _, other_line = _normalize_mb_family(_mb_market(_mb_indkc(), name="Handicap", handicap=4.5))
    assert MarketMatcher().match(spread, other_line).matched is False
    assert catalogue_allows_live_execution(spread, other_line) is False
