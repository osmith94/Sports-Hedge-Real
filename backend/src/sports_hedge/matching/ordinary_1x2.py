"""Ordinary Matchbook↔Kalshi 1X2 equivalence.

Owner time-box after Kalshi GAME/WIN contract terms failed to supply a default
``<result scope>``. Matchbook complete regulation-time 1X2 may match Kalshi
ordinary full-time HOME/DRAW/AWAY when Kalshi scope is UNKNOWN solely because
SOCCERGAMEWIN's listed result-scope placeholder cannot be recovered.

This is an explicit, documented narrowing of Core Tenet 03 for this venue pair
and family only. Polymarket, To Qualify, two-way books, non-full-time periods,
proven extra-time/penalties contradictions, and unknown Kalshi settlement
without GAMEWIN placeholder evidence stay fail-closed.

Issue #316 additionally admits a bounded PAPER-MODE assumption for ordinary
Matchbook↔Kalshi 1X2 when GAME HOME/DRAW/AWAY is complete and there is no
contradictory wording. That assumption is never live-execution eligible.
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
from sports_hedge.normalization.kalshi_contract_terms import GAMEWIN_SCOPE_UNAVAILABLE_REASON

ORDINARY_1X2_OUTCOMES = frozenset(
    {CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY}
)
MATCHBOOK_KALSHI_VENUES = frozenset({VenueName.MATCHBOOK, VenueName.KALSHI})
ORDINARY_1X2_REASON = "ordinary_match_result_1x2"
UNKNOWN_SETTLEMENT_ALLOWED_REASON = "settlement_unknown_not_contradictory"
GAMEWIN_ORDINARY_1X2_AUDIT_REASON = "ordinary_3way_1x2_kalshi_gamewin_scope_unavailable"
PAPER_ASSUMED_1X2_REASON = "paper_assumed_equivalent"
SETTLEMENT_ASSUMPTION_REGULATION_TIME = "regulation_time"
ORDINARY_1X2_AUDIT_REASONS = (
    ORDINARY_1X2_REASON,
    UNKNOWN_SETTLEMENT_ALLOWED_REASON,
    GAMEWIN_ORDINARY_1X2_AUDIT_REASON,
)

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


def is_complete_regulation_time_1x2(market: CanonicalMarket) -> bool:
    if not is_ordinary_full_time_1x2(market):
        return False
    settlement = market.settlement
    if not settlement.is_economically_complete():
        return False
    if settlement.scope is not SettlementScope.REGULATION_TIME:
        return False
    if settlement.extra_time_included is not False:
        return False
    if settlement.penalties_included is not False:
        return False
    return True


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


def kalshi_gamewin_scope_unavailable(market: CanonicalMarket) -> bool:
    """Kalshi ordinary 1X2 whose listed GAMEWIN result scope cannot be recovered."""

    if market.source_venue is not VenueName.KALSHI:
        return False
    if not is_ordinary_full_time_1x2(market):
        return False
    settlement = market.settlement
    if settlement.scope is not SettlementScope.UNKNOWN:
        return False
    if settlement.is_economically_complete():
        return False
    return settlement.unknown_reason == GAMEWIN_SCOPE_UNAVAILABLE_REASON


def allow_unknown_settlement_for_ordinary_1x2(
    left: CanonicalMarket,
    right: CanonicalMarket,
) -> bool:
    """Allow Matchbook regulation 1X2 vs Kalshi GAMEWIN-unknown, without contradiction."""

    if not is_matchbook_kalshi_pair(left, right):
        return False
    if not is_ordinary_full_time_1x2(left) or not is_ordinary_full_time_1x2(right):
        return False
    if settlement_fingerprints_contradict(left.settlement, right.settlement):
        return False
    matchbook = left if left.source_venue is VenueName.MATCHBOOK else right
    kalshi = right if matchbook is left else left
    if not is_complete_regulation_time_1x2(matchbook):
        return False
    if kalshi_gamewin_scope_unavailable(kalshi):
        return True
    return paper_assumed_ordinary_1x2(left, right)


_CONTRADICTION_TOKENS = (
    "extra time",
    "extra-time",
    "penalties",
    "to qualify",
    "to-qualify",
    "fair price",
    "fair-price",
    "reschedule",
    "cancelled",
    "canceled",
)


def paper_assumed_ordinary_1x2(left: CanonicalMarket, right: CanonicalMarket) -> bool:
    """Owner-accepted paper-mode 1X2 assumption. Never live-execution eligible.

    Requires exact ordinary HOME/DRAW/AWAY, Matchbook regulation convention,
    structurally consistent period, and no known contradictory Kalshi wording.
    Independent settlement proof is not required.
    """

    if not is_matchbook_kalshi_pair(left, right):
        return False
    if left.family is not MarketFamily.MATCH_RESULT or right.family is not MarketFamily.MATCH_RESULT:
        return False
    if not is_ordinary_full_time_1x2(left) or not is_ordinary_full_time_1x2(right):
        return False
    if left.period != right.period or left.line != right.line:
        return False
    if settlement_fingerprints_contradict(left.settlement, right.settlement):
        return False
    matchbook = left if left.source_venue is VenueName.MATCHBOOK else right
    kalshi = right if matchbook is left else left
    if not is_complete_regulation_time_1x2(matchbook):
        return False
    if kalshi.settlement.extra_time_included is True or kalshi.settlement.penalties_included is True:
        return False
    if kalshi.settlement.scope in {
        SettlementScope.INCLUDING_EXTRA_TIME,
        SettlementScope.INCLUDING_PENALTIES,
    }:
        return False
    unknown = str(kalshi.settlement.unknown_reason or "").casefold()
    if any(token in unknown for token in _CONTRADICTION_TOKENS):
        return False
    if kalshi.settlement.is_economically_complete() and is_complete_regulation_time_1x2(kalshi):
        return False
    return True


def ordinary_1x2_match_reasons() -> list[str]:
    return list(ORDINARY_1X2_AUDIT_REASONS)


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
