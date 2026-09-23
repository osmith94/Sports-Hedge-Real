"""Owner-gated NCAAB PAPER register.

Structurally recognise GAME_WINNER and exact x.5 spread/total. No venue pair
is PAPER-admitted: the 2026-09-22 census did not recover NCAAB-specific
settlement equivalence for K↔PM, MB↔K, MB↔PM, or three-venue cells.
"""

from __future__ import annotations

from decimal import Decimal

from sports_hedge.domain.football import (
    CanonicalMarket,
    CanonicalOutcome,
    FootballPeriod,
    MarketFamily,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.ncaab.constants import (
    CANONICAL_NCAAB_GAME_WINNER,
    CANONICAL_NCAAB_POINT_SPREAD,
    CANONICAL_NCAAB_TOTAL_POINTS,
)
from sports_hedge.ncaab.detect import is_ncaab_canonical_event, is_ncaab_market_family
from sports_hedge.ncaab.markets import is_exact_half_line

NCAAB_NORMALIZER_VENUES = frozenset(
    {VenueName.MATCHBOOK, VenueName.KALSHI, VenueName.POLYMARKET}
)
# Empty on purpose. Census did not prove any NCAAB pair/family cell.
NCAAB_PAPER_APPROVED_CELLS: frozenset[tuple[frozenset[VenueName], MarketFamily]] = frozenset()
GAME_WINNER_OUTCOMES = frozenset({CanonicalOutcome.HOME, CanonicalOutcome.AWAY})
SPREAD_OUTCOMES = frozenset({CanonicalOutcome.HOME, CanonicalOutcome.AWAY})
TOTAL_OUTCOMES = frozenset({CanonicalOutcome.OVER, CanonicalOutcome.UNDER})


def _outcomes(market: CanonicalMarket) -> set[CanonicalOutcome]:
    return {runner.outcome for runner in market.runners}


def _line_key(line: Decimal) -> str:
    return format(line.normalize(), "f")


def is_ncaab_register_market(market: CanonicalMarket) -> bool:
    return is_ncaab_canonical_event(market.event)


def ncaab_structural_identity(market: CanonicalMarket) -> bool:
    if not is_ncaab_canonical_event(market.event) or not is_ncaab_market_family(market.family):
        return False
    if market.period is not FootballPeriod.FULL_TIME:
        return False
    if CanonicalOutcome.OTHER in _outcomes(market):
        return False
    if CanonicalOutcome.DRAW in _outcomes(market):
        return False
    if market.family is MarketFamily.GAME_WINNER:
        return market.line is None and _outcomes(market) == GAME_WINNER_OUTCOMES
    if market.family is MarketFamily.POINT_SPREAD:
        return is_exact_half_line(market.line) and _outcomes(market) == SPREAD_OUTCOMES
    if market.family is MarketFamily.TOTAL_POINTS:
        return is_exact_half_line(market.line) and _outcomes(market) == TOTAL_OUTCOMES
    return False


def ncaab_canonical_key_for_market(market: CanonicalMarket) -> str | None:
    if not ncaab_structural_identity(market):
        return None
    if market.family is MarketFamily.GAME_WINNER:
        return CANONICAL_NCAAB_GAME_WINNER
    if market.family is MarketFamily.POINT_SPREAD:
        assert market.line is not None
        return f"{CANONICAL_NCAAB_POINT_SPREAD}:{_line_key(market.line)}"
    if market.family is MarketFamily.TOTAL_POINTS:
        assert market.line is not None
        return f"{CANONICAL_NCAAB_TOTAL_POINTS}:{_line_key(market.line)}"
    return None


def ncaab_approved_paper_venue_pair(left: CanonicalMarket, right: CanonicalMarket) -> bool:
    """Always false until an NCAAB-specific settlement cell is owner-approved."""

    del left, right
    return False


def ncaab_registered_canonical_key(
    left: CanonicalMarket, right: CanonicalMarket
) -> str | None:
    if not is_ncaab_register_market(left) or not is_ncaab_register_market(right):
        return None
    if not ncaab_approved_paper_venue_pair(left, right):
        return None
    left_key = ncaab_canonical_key_for_market(left)
    right_key = ncaab_canonical_key_for_market(right)
    if left_key is None or left_key != right_key:
        return None
    return left_key
