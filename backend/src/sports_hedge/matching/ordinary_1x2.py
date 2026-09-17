"""Ordinary Matchbook↔Kalshi 1X2 equivalence.

Owner time-box after Kalshi GAME/WIN contract terms failed to supply a default
``<result scope>``. Unknown Kalshi settlement is not treated as a contradiction
for an otherwise clean full-time HOME/DRAW/AWAY Match Result pair.

This is an explicit, documented narrowing of Core Tenet 03 for this venue pair
and family only. Polymarket, To Qualify, two-way books, non-full-time periods,
and proven extra-time/penalties contradictions stay fail-closed.
"""

from __future__ import annotations

from sports_hedge.domain.football import (
    CanonicalMarket,
    CanonicalOutcome,
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName

ORDINARY_1X2_OUTCOMES = frozenset(
    {CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY}
)
MATCHBOOK_KALSHI_VENUES = frozenset({VenueName.MATCHBOOK, VenueName.KALSHI})
ORDINARY_1X2_REASON = "ordinary_match_result_1x2"
UNKNOWN_SETTLEMENT_ALLOWED_REASON = "settlement_unknown_not_contradictory"

SETTLEMENT_STATUS_KNOWN = "known"
SETTLEMENT_STATUS_UNKNOWN = "unknown"
SETTLEMENT_STATUS_CONFLICT = "conflict"


def is_ordinary_full_time_1x2(market: CanonicalMarket) -> bool:
    """Structural ordinary 1X2: Match Result, full time, exhaustive H/D/A."""

    if market.family is not MarketFamily.MATCH_RESULT:
        return False
    if market.period is not FootballPeriod.FULL_TIME:
        return False
    if market.settlement.period not in {FootballPeriod.FULL_TIME, FootballPeriod.UNKNOWN}:
        return False
    outcomes = {runner.outcome for runner in market.runners}
    if CanonicalOutcome.OTHER in outcomes:
        return False
    return outcomes == ORDINARY_1X2_OUTCOMES


def is_matchbook_kalshi_pair(left: CanonicalMarket, right: CanonicalMarket) -> bool:
    return {left.source_venue, right.source_venue} == MATCHBOOK_KALSHI_VENUES


def settlement_fingerprints_contradict(
    left: SettlementFingerprint,
    right: SettlementFingerprint,
) -> bool:
    """True only when both sides prove incompatible known settlement semantics."""

    if (
        left.scope is not SettlementScope.UNKNOWN
        and right.scope is not SettlementScope.UNKNOWN
        and left.scope != right.scope
    ):
        return True
    if (
        left.period not in {FootballPeriod.UNKNOWN}
        and right.period not in {FootballPeriod.UNKNOWN}
        and left.period != right.period
    ):
        return True
    if left.extra_time_included is not None and right.extra_time_included is not None:
        if left.extra_time_included != right.extra_time_included:
            return True
    if left.penalties_included is not None and right.penalties_included is not None:
        if left.penalties_included != right.penalties_included:
            return True
    return False


def allow_unknown_settlement_for_ordinary_1x2(
    left: CanonicalMarket,
    right: CanonicalMarket,
) -> bool:
    """Matchbook↔Kalshi ordinary 1X2 may proceed when Kalshi scope is unknown."""

    if not is_matchbook_kalshi_pair(left, right):
        return False
    if not is_ordinary_full_time_1x2(left) or not is_ordinary_full_time_1x2(right):
        return False
    if settlement_fingerprints_contradict(left.settlement, right.settlement):
        return False
    return True


def pair_settlement_status(left: CanonicalMarket, right: CanonicalMarket) -> str:
    if settlement_fingerprints_contradict(left.settlement, right.settlement):
        return SETTLEMENT_STATUS_CONFLICT
    if (
        left.settlement.scope is SettlementScope.UNKNOWN
        or right.settlement.scope is SettlementScope.UNKNOWN
        or not left.settlement.is_economically_complete()
        or not right.settlement.is_economically_complete()
    ):
        return SETTLEMENT_STATUS_UNKNOWN
    return SETTLEMENT_STATUS_KNOWN


def pair_hda_complete(left: CanonicalMarket, right: CanonicalMarket) -> bool:
    return is_ordinary_full_time_1x2(left) and is_ordinary_full_time_1x2(right)
