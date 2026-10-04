"""MLB structural catalogue keys.

Game Winner and exact Total Runs x.5 are the only families that receive a key.
Run line, first-five, inning, props, series, and futures do not. The owner
approved venue-pair comparison for those structural keys on 2026-09-26.
Fixture identity, team identity, line, and outcome-shape gates are unchanged.
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
from sports_hedge.mlb.constants import CANONICAL_MLB_GAME_WINNER, CANONICAL_MLB_TOTAL_RUNS
from sports_hedge.mlb.detect import is_mlb_canonical_event, is_mlb_market_family
from sports_hedge.mlb.markets import is_exact_half_line

MLB_REGISTERED_VENUES = frozenset(
    {VenueName.MATCHBOOK, VenueName.KALSHI, VenueName.POLYMARKET}
)
# LEGACY name. Same venue set; not a paper-only restriction.
MLB_PAPER_VENUES = MLB_REGISTERED_VENUES
GAME_WINNER_OUTCOMES = frozenset({CanonicalOutcome.HOME, CanonicalOutcome.AWAY})
TOTAL_OUTCOMES = frozenset({CanonicalOutcome.OVER, CanonicalOutcome.UNDER})


def _outcomes(market: CanonicalMarket) -> set[CanonicalOutcome]:
    return {runner.outcome for runner in market.runners}


def _line_key(line: Decimal) -> str:
    return format(line.normalize(), "f")


def is_mlb_register_market(market: CanonicalMarket) -> bool:
    return is_mlb_canonical_event(market.event)


def mlb_structural_identity(market: CanonicalMarket) -> bool:
    if not is_mlb_canonical_event(market.event) or not is_mlb_market_family(market.family):
        return False
    if market.period is not FootballPeriod.FULL_TIME:
        return False
    if not market.event.scheduled_game_key:
        return False
    if CanonicalOutcome.OTHER in _outcomes(market) or CanonicalOutcome.DRAW in _outcomes(market):
        return False
    if market.family is MarketFamily.GAME_WINNER:
        return market.line is None and _outcomes(market) == GAME_WINNER_OUTCOMES
    if market.family is MarketFamily.TOTAL_RUNS:
        return is_exact_half_line(market.line) and _outcomes(market) == TOTAL_OUTCOMES
    return False


def mlb_canonical_key_for_market(market: CanonicalMarket) -> str | None:
    """Structural key only. Not permission to compare prices."""

    if not mlb_structural_identity(market):
        return None
    if market.family is MarketFamily.GAME_WINNER:
        return CANONICAL_MLB_GAME_WINNER
    if market.family is MarketFamily.TOTAL_RUNS:
        assert market.line is not None
        return f"{CANONICAL_MLB_TOTAL_RUNS}:{_line_key(market.line)}"
    return None


def mlb_approved_paper_venue_pair(left: CanonicalMarket, right: CanonicalMarket) -> bool:
    """Only cross-venue pairs among the three registered MLB venues."""

    if left.source_venue == right.source_venue:
        return False
    venues = {left.source_venue, right.source_venue}
    return venues <= MLB_REGISTERED_VENUES


def mlb_registered_canonical_key(left: CanonicalMarket, right: CanonicalMarket) -> str | None:
    if not is_mlb_register_market(left) or not is_mlb_register_market(right):
        return None
    if not mlb_approved_paper_venue_pair(left, right):
        return None
    left_key = mlb_canonical_key_for_market(left)
    right_key = mlb_canonical_key_for_market(right)
    if left_key is None or left_key != right_key:
        return None
    return left_key
