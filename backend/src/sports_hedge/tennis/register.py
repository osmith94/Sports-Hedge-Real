"""Tennis catalogue identity. Structurally identical singles Match Winner is registered."""

from __future__ import annotations

from sports_hedge.domain.football import (
    CanonicalMarket,
    CanonicalOutcome,
    FootballPeriod,
    MarketFamily,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.tennis.constants import CANONICAL_TENNIS_MATCH_WINNER, TENNIS_EVENT_SINGLES
from sports_hedge.tennis.detect import is_tennis_canonical_event, is_tennis_market_family

TENNIS_CATALOGUE_VENUES = frozenset(
    {VenueName.MATCHBOOK, VenueName.KALSHI, VenueName.POLYMARKET}
)
MATCH_WINNER_OUTCOMES = frozenset({CanonicalOutcome.HOME, CanonicalOutcome.AWAY})


def _outcomes(market: CanonicalMarket) -> set[CanonicalOutcome]:
    return {runner.outcome for runner in market.runners}


def is_tennis_register_market(market: CanonicalMarket) -> bool:
    return is_tennis_canonical_event(market.event) and is_tennis_market_family(market.family)


def tennis_structural_identity(market: CanonicalMarket) -> bool:
    if not is_tennis_register_market(market):
        return False
    if market.source_venue not in TENNIS_CATALOGUE_VENUES:
        return False
    if market.period is not FootballPeriod.FULL_TIME:
        return False
    if market.line is not None:
        return False
    event = market.event
    if event.event_type != TENNIS_EVENT_SINGLES:
        return False
    if not event.tournament or not event.round_label:
        return False
    if event.competition not in {"ATP", "WTA"}:
        return False
    return _outcomes(market) == MATCH_WINNER_OUTCOMES


def tennis_canonical_key_for_market(market: CanonicalMarket) -> str | None:
    if not tennis_structural_identity(market):
        return None
    if market.family is not MarketFamily.GAME_WINNER:
        return None
    return CANONICAL_TENNIS_MATCH_WINNER


def tennis_registered_canonical_key(
    left: CanonicalMarket, right: CanonicalMarket
) -> str | None:
    """Catalogue key for an exact registered singles Match Winner.

    Catalogue eligibility follows this key. Venue orders stay separately gated.
    """

    if left.source_venue == right.source_venue:
        return None
    if left.source_venue not in TENNIS_CATALOGUE_VENUES or right.source_venue not in TENNIS_CATALOGUE_VENUES:
        return None
    left_key = tennis_canonical_key_for_market(left)
    right_key = tennis_canonical_key_for_market(right)
    if left_key is None or left_key != right_key:
        return None
    if left.event.competition != right.event.competition:
        return None
    if left.event.tournament != right.event.tournament:
        return None
    if left.event.round_label != right.event.round_label:
        return None
    if {left.event.home_team, left.event.away_team} != {right.event.home_team, right.event.away_team}:
        return None
    return left_key
