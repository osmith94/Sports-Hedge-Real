"""Owner-gated NBA PAPER register.

Normal-completion PAPER equivalence is admitted only for evidence-backed
Kalshi↔Polymarket GAME_WINNER. Spreads, totals, and every Matchbook pair stay
fail-closed until NBA game-book / OT-wording evidence exists.
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
from sports_hedge.nba.constants import (
    CANONICAL_NBA_GAME_WINNER,
    CANONICAL_NBA_POINT_SPREAD,
    CANONICAL_NBA_TOTAL_POINTS,
)
from sports_hedge.nba.detect import is_nba_canonical_event, is_nba_market_family
from sports_hedge.nba.markets import is_exact_half_line

NBA_NORMALIZER_VENUES = frozenset(
    {VenueName.MATCHBOOK, VenueName.KALSHI, VenueName.POLYMARKET}
)
NBA_PAPER_APPROVED_VENUES = frozenset({VenueName.KALSHI, VenueName.POLYMARKET})
GAME_WINNER_OUTCOMES = frozenset({CanonicalOutcome.HOME, CanonicalOutcome.AWAY})
SPREAD_OUTCOMES = frozenset({CanonicalOutcome.HOME, CanonicalOutcome.AWAY})
TOTAL_OUTCOMES = frozenset({CanonicalOutcome.OVER, CanonicalOutcome.UNDER})


def _outcomes(market: CanonicalMarket) -> set[CanonicalOutcome]:
    return {runner.outcome for runner in market.runners}


def _line_key(line: Decimal) -> str:
    return format(line.normalize(), "f")


def is_nba_register_market(market: CanonicalMarket) -> bool:
    return is_nba_canonical_event(market.event) and is_nba_market_family(market.family)


def nba_structural_identity(market: CanonicalMarket) -> bool:
    if not is_nba_register_market(market):
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


def nba_canonical_key_for_market(market: CanonicalMarket) -> str | None:
    if not nba_structural_identity(market):
        return None
    if market.family is MarketFamily.GAME_WINNER:
        return CANONICAL_NBA_GAME_WINNER
    if market.family is MarketFamily.POINT_SPREAD:
        assert market.line is not None
        return f"{CANONICAL_NBA_POINT_SPREAD}:{_line_key(market.line)}"
    if market.family is MarketFamily.TOTAL_POINTS:
        assert market.line is not None
        return f"{CANONICAL_NBA_TOTAL_POINTS}:{_line_key(market.line)}"
    return None


def nba_approved_paper_venue_pair(left: CanonicalMarket, right: CanonicalMarket) -> bool:
    """Kalshi↔Polymarket GAME_WINNER only. Spreads/totals/Matchbook stay blocked."""

    if left.source_venue == right.source_venue:
        return False
    if left.family is not MarketFamily.GAME_WINNER or right.family is not MarketFamily.GAME_WINNER:
        return False
    venues = {left.source_venue, right.source_venue}
    return venues == NBA_PAPER_APPROVED_VENUES


def nba_registered_canonical_key(
    left: CanonicalMarket, right: CanonicalMarket
) -> str | None:
    if not is_nba_register_market(left) or not is_nba_register_market(right):
        return None
    if not nba_approved_paper_venue_pair(left, right):
        return None
    left_key = nba_canonical_key_for_market(left)
    right_key = nba_canonical_key_for_market(right)
    if left_key is None or left_key != right_key:
        return None
    return left_key
