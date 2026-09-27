"""Owner-approved NFL PAPER register. Three venues, three families.

GAME_WINNER and exact .5 spread/total are PAPER-admitted. Exceptional
lifecycle differences are not an audit caveat. Live execution stays disabled.
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
from sports_hedge.nfl.constants import (
    CANONICAL_NFL_GAME_WINNER,
    CANONICAL_NFL_POINT_SPREAD,
    CANONICAL_NFL_TOTAL_POINTS,
)
from sports_hedge.nfl.detect import is_nfl_canonical_event, is_nfl_market_family
from sports_hedge.nfl.markets import is_exact_half_line

NFL_PAPER_VENUES = frozenset(
    {VenueName.MATCHBOOK, VenueName.KALSHI, VenueName.POLYMARKET}
)
GAME_WINNER_OUTCOMES = frozenset({CanonicalOutcome.HOME, CanonicalOutcome.AWAY})
SPREAD_OUTCOMES = frozenset({CanonicalOutcome.HOME, CanonicalOutcome.AWAY})
TOTAL_OUTCOMES = frozenset({CanonicalOutcome.OVER, CanonicalOutcome.UNDER})


def _outcomes(market: CanonicalMarket) -> set[CanonicalOutcome]:
    return {runner.outcome for runner in market.runners}


def _line_key(line: Decimal) -> str:
    return format(line.normalize(), "f")


def is_nfl_register_market(market: CanonicalMarket) -> bool:
    return is_nfl_canonical_event(market.event) and is_nfl_market_family(market.family)


def nfl_structural_identity(market: CanonicalMarket) -> bool:
    if not is_nfl_register_market(market):
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


def nfl_canonical_key_for_market(market: CanonicalMarket) -> str | None:
    if not nfl_structural_identity(market):
        return None
    if market.family is MarketFamily.GAME_WINNER:
        return CANONICAL_NFL_GAME_WINNER
    if market.family is MarketFamily.POINT_SPREAD:
        assert market.line is not None
        return f"{CANONICAL_NFL_POINT_SPREAD}:{_line_key(market.line)}"
    if market.family is MarketFamily.TOTAL_POINTS:
        assert market.line is not None
        return f"{CANONICAL_NFL_TOTAL_POINTS}:{_line_key(market.line)}"
    return None


def nfl_approved_paper_venue_pair(left: CanonicalMarket, right: CanonicalMarket) -> bool:
    if left.source_venue == right.source_venue:
        return False
    venues = {left.source_venue, right.source_venue}
    return venues <= NFL_PAPER_VENUES


def nfl_registered_canonical_key(
    left: CanonicalMarket, right: CanonicalMarket
) -> str | None:
    if not is_nfl_register_market(left) or not is_nfl_register_market(right):
        return None
    if not nfl_approved_paper_venue_pair(left, right):
        return None
    left_key = nfl_canonical_key_for_market(left)
    right_key = nfl_canonical_key_for_market(right)
    if left_key is None or left_key != right_key:
        return None
    return left_key
