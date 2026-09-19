"""Versioned Approved Match Register (Issue #331).

Runtime scanning consumes this register. Equivalence is decided at venue
onboarding, not by confidence, learned labels, or settlement-text review on
each scan.

Venue-native archetypes map to canonical keys so a future Polymarket/Smarkets
onboarding adds one mapping per native market rather than N² pair rules.

Current PAPER / READ-ONLY pair: Matchbook ↔ Kalshi for the four locked
full-time football families. Live execution remains ineligible.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sports_hedge.domain.football import (
    CanonicalMarket,
    CanonicalOutcome,
    FootballPeriod,
    MarketFamily,
    SettlementScope,
    line_push_possible,
)
from sports_hedge.domain.models import VenueName

REGISTER_VERSION = "v1"
REGISTER_ISSUE = 331
REGISTER_PARENT_COMMIT = "8c0b31e5b593ce098866b38f930102aadffcccc8"
REGISTER_SHARED_BY = ("hot", "universe")
DATA_CLASS = "deterministic_approved_match_register"

CANONICAL_MATCH_RESULT_FT = "MATCH_RESULT_FT"
CANONICAL_BTTS_FT = "BTTS_FT"
CANONICAL_TOTAL_GOALS_FT = "TOTAL_GOALS_FT"
CANONICAL_FTTS_FT = "FTTS_FT"

APPROVED_PAPER_VENUE_PAIR = frozenset({VenueName.MATCHBOOK, VenueName.KALSHI})
REGISTER_ADMITTED_REASON = "approved_match_register"
REGISTER_PAPER_MODE_REASON = "register_paper_admitted_not_live_execution"
NOT_REGISTERED_REASON = "not_registered"
CANONICAL_KEY_REASON_PREFIX = "canonical_key="
_EXTRA_TIME_OR_PENALTIES_ARCHETYPE_REASON = (
    "kalshi_unmodelled_extra_time_or_penalties_wording"
)

MATCH_RESULT_OUTCOMES = frozenset(
    {CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY}
)
BTTS_OUTCOMES = frozenset({CanonicalOutcome.YES, CanonicalOutcome.NO})
TOTAL_OUTCOMES = frozenset({CanonicalOutcome.OVER, CanonicalOutcome.UNDER})
FTTS_OUTCOMES = frozenset(
    {CanonicalOutcome.HOME, CanonicalOutcome.AWAY, CanonicalOutcome.NO_GOAL}
)


@dataclass(frozen=True)
class VenueNativeArchetype:
    """One venue's native standard market mapped to a canonical key."""

    venue: VenueName
    native_archetype: str
    display_name: str
    canonical_key: str
    family: MarketFamily
    period: FootballPeriod
    required_outcomes: frozenset[CanonicalOutcome]
    parameterized_line: bool = False
    safe_half_line_only: bool = False


VENUE_NATIVE_ARCHETYPES: tuple[VenueNativeArchetype, ...] = (
    VenueNativeArchetype(
        venue=VenueName.MATCHBOOK,
        native_archetype="match_odds",
        display_name="Match Odds / Final Result",
        canonical_key=CANONICAL_MATCH_RESULT_FT,
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        required_outcomes=MATCH_RESULT_OUTCOMES,
    ),
    VenueNativeArchetype(
        venue=VenueName.KALSHI,
        native_archetype="GAME",
        display_name="GAME",
        canonical_key=CANONICAL_MATCH_RESULT_FT,
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        required_outcomes=MATCH_RESULT_OUTCOMES,
    ),
    VenueNativeArchetype(
        venue=VenueName.MATCHBOOK,
        native_archetype="both_teams_to_score",
        display_name="Both Teams To Score",
        canonical_key=CANONICAL_BTTS_FT,
        family=MarketFamily.BOTH_TEAMS_TO_SCORE,
        period=FootballPeriod.FULL_TIME,
        required_outcomes=BTTS_OUTCOMES,
    ),
    VenueNativeArchetype(
        venue=VenueName.KALSHI,
        native_archetype="BTTS",
        display_name="BTTS",
        canonical_key=CANONICAL_BTTS_FT,
        family=MarketFamily.BOTH_TEAMS_TO_SCORE,
        period=FootballPeriod.FULL_TIME,
        required_outcomes=BTTS_OUTCOMES,
    ),
    VenueNativeArchetype(
        venue=VenueName.MATCHBOOK,
        native_archetype="total_goals",
        display_name="Total Goals",
        canonical_key=CANONICAL_TOTAL_GOALS_FT,
        family=MarketFamily.TOTAL_GOALS,
        period=FootballPeriod.FULL_TIME,
        required_outcomes=TOTAL_OUTCOMES,
        parameterized_line=True,
        safe_half_line_only=True,
    ),
    VenueNativeArchetype(
        venue=VenueName.KALSHI,
        native_archetype="TOTAL",
        display_name="TOTAL",
        canonical_key=CANONICAL_TOTAL_GOALS_FT,
        family=MarketFamily.TOTAL_GOALS,
        period=FootballPeriod.FULL_TIME,
        required_outcomes=TOTAL_OUTCOMES,
        parameterized_line=True,
        safe_half_line_only=True,
    ),
    VenueNativeArchetype(
        venue=VenueName.MATCHBOOK,
        native_archetype="first_team_to_score",
        display_name="First Team To Score",
        canonical_key=CANONICAL_FTTS_FT,
        family=MarketFamily.FIRST_TEAM_TO_SCORE,
        period=FootballPeriod.FULL_TIME,
        required_outcomes=FTTS_OUTCOMES,
    ),
    VenueNativeArchetype(
        venue=VenueName.KALSHI,
        native_archetype="FTTS",
        display_name="FTTS",
        canonical_key=CANONICAL_FTTS_FT,
        family=MarketFamily.FIRST_TEAM_TO_SCORE,
        period=FootballPeriod.FULL_TIME,
        required_outcomes=FTTS_OUTCOMES,
    ),
)


def _outcomes(market: CanonicalMarket) -> set[CanonicalOutcome]:
    return {runner.outcome for runner in market.runners}


def _line_key(line: Decimal) -> str:
    return format(line.normalize(), "f")


def register_key_reason(key: str) -> str:
    return f"{CANONICAL_KEY_REASON_PREFIX}{key}"


def _is_onboarded_full_time_regulation_contract(market: CanonicalMarket) -> bool:
    """FT register archetypes are regulation-time contracts, not extra-time/to-qualify."""

    if market.period is not FootballPeriod.FULL_TIME:
        return False
    if market.family is MarketFamily.TO_QUALIFY:
        return False
    settlement = market.settlement
    if settlement.extra_time_included is True or settlement.penalties_included is True:
        return False
    if settlement.scope in {
        SettlementScope.INCLUDING_EXTRA_TIME,
        SettlementScope.INCLUDING_PENALTIES,
    }:
        return False
    if settlement.unknown_reason == _EXTRA_TIME_OR_PENALTIES_ARCHETYPE_REASON:
        return False
    return True


def venue_native_archetype_for(market: CanonicalMarket) -> VenueNativeArchetype | None:
    """Map a normalized venue market onto one onboarded native archetype."""

    outcomes = _outcomes(market)
    if CanonicalOutcome.OTHER in outcomes:
        return None
    if not _is_onboarded_full_time_regulation_contract(market):
        return None
    for item in VENUE_NATIVE_ARCHETYPES:
        if item.venue is not market.source_venue:
            continue
        if item.family is not market.family:
            continue
        if item.period is not market.period:
            continue
        if outcomes != item.required_outcomes:
            continue
        if item.parameterized_line:
            if market.line is None:
                continue
            if item.safe_half_line_only and line_push_possible(market.line) is not False:
                continue
        elif market.line is not None:
            continue
        return item
    return None


def canonical_key_for_market(market: CanonicalMarket) -> str | None:
    """Instance canonical key. TOTAL includes the exact safe half-line."""

    archetype = venue_native_archetype_for(market)
    if archetype is None:
        return None
    if archetype.parameterized_line:
        assert market.line is not None
        return f"{archetype.canonical_key}:{_line_key(market.line)}"
    return archetype.canonical_key


def approved_paper_venue_pair(left: CanonicalMarket, right: CanonicalMarket) -> bool:
    return {left.source_venue, right.source_venue} == APPROVED_PAPER_VENUE_PAIR


def registered_canonical_key(left: CanonicalMarket, right: CanonicalMarket) -> str | None:
    """Same canonical key on the approved Matchbook↔Kalshi PAPER pair, or None."""

    if not approved_paper_venue_pair(left, right):
        return None
    left_key = canonical_key_for_market(left)
    right_key = canonical_key_for_market(right)
    if left_key is None or left_key != right_key:
        return None
    return left_key


def registered_structural_match(left: CanonicalMarket, right: CanonicalMarket) -> bool:
    """True when onboarded native archetypes resolve to one canonical key."""

    return registered_canonical_key(left, right) is not None


def structural_mismatch_reasons(left: CanonicalMarket, right: CanonicalMarket) -> list[str]:
    """Family / period / line / outcome-space only. Not settlement fingerprints."""

    reasons: list[str] = []
    if left.family != right.family:
        reasons.append("market_family_mismatch")
    if left.period != right.period:
        reasons.append("period_mismatch")
    if left.line != right.line:
        reasons.append("line_mismatch")
    if _outcomes(left) != _outcomes(right):
        reasons.append("outcome_space_mismatch")
    return reasons
