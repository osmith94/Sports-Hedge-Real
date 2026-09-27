"""Owner-approved PAPER-mode equivalence for the four locked families.

Issue #326: Matchbook↔Kalshi MATCH_RESULT / BTTS / exact-line TOTAL / FTTS may
enter the paper opportunity pipeline once canonical fixture identity and
canonical market identity/parameters match. Cross-venue settlement equivalence
for these four families is an owner-approved product assumption in PAPER /
READ-ONLY mode. The scanner must not re-litigate settlement text on every scan.

This path is never live-execution eligible. Independently proven complete
fingerprints remain APPROVED_EQUIVALENT. Proven extra-time / penalties /
to-qualify contradictions, wrong fixture/family/period, TOTAL line mismatch,
and incomplete outcome-space identity stay fail-closed. Exceptional
cancel/postpone/void/fair-price differences do not block PAPER admission.

Polymarket joins Matchbook and Kalshi when it normalizes to the same
canonical key. Integer/quarter TOTAL push markets stay outside this set.
"""

from __future__ import annotations

from sports_hedge.domain.football import (
    CanonicalMarket,
    CanonicalOutcome,
    FootballPeriod,
    MarketFamily,
    line_push_possible,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.approved_register import (
    REGISTER_ADMITTED_REASON,
    registered_structural_match,
)
from sports_hedge.matching.ordinary_1x2 import (
    ORDINARY_1X2_OUTCOMES,
    PAPER_ASSUMED_1X2_REASON,
    SETTLEMENT_ASSUMPTION_REGULATION_TIME,
    allow_unknown_settlement_for_ordinary_1x2,
    is_complete_regulation_time_1x2,
    is_ordinary_full_time_1x2,
    settlement_fingerprints_contradict,
)
from sports_hedge.normalization.kalshi_contract_terms import (
    KALSHI_CONTRACT_FAMILY_NOT_MATCH_RESULT_REASON,
)
from sports_hedge.normalization.venues import (
    KALSHI_UNMODELLED_CANCEL_RESCHEDULE_FAIR_PRICE_REASON,
    KALSHI_UNMODELLED_EXTRA_TIME_OR_PENALTIES_REASON,
)

LOCKED_PAPER_FAMILIES: frozenset[MarketFamily] = frozenset(
    {
        MarketFamily.MATCH_RESULT,
        MarketFamily.BOTH_TEAMS_TO_SCORE,
        MarketFamily.TOTAL_GOALS,
        MarketFamily.FIRST_TEAM_TO_SCORE,
    }
)
BTTS_OUTCOMES = frozenset({CanonicalOutcome.YES, CanonicalOutcome.NO})
TOTAL_OUTCOMES = frozenset({CanonicalOutcome.OVER, CanonicalOutcome.UNDER})
FTTS_OUTCOMES = frozenset(
    {CanonicalOutcome.HOME, CanonicalOutcome.AWAY, CanonicalOutcome.NO_GOAL}
)
PAPER_ASSUMED_REASON = PAPER_ASSUMED_1X2_REASON
OWNER_APPROVED_PAPER_EQUIVALENCE_REASON = "owner_approved_paper_equivalence"
FAIR_PRICE_PAPER_ADMITTED_REASON = "kalshi_fair_price_does_not_block_paper_admission"
PAPER_ASSUMED_AUDIT_REASONS = (
    PAPER_ASSUMED_REASON,
    f"settlement_assumption={SETTLEMENT_ASSUMPTION_REGULATION_TIME}",
    OWNER_APPROVED_PAPER_EQUIVALENCE_REASON,
    REGISTER_ADMITTED_REASON,
)
PAPER_NONBLOCKING_REJECTION_REASONS = frozenset(
    {
        PAPER_ASSUMED_REASON,
        "paper_assumed_not_live_execution_eligible",
        "paper_mode_only_not_live_execution_eligible",
        "paper_assumed_equivalent_not_settlement_proven",
        "owner_approved_nfl_paper_normal_completion",
        "owner_approved_nba_paper_normal_completion",
        "exceptional_settlement_mismatch_possible",
        "nfl_paper_not_live_execution_equivalent",
        "nba_paper_not_live_execution_equivalent",
        "settlement_assumption=normal_full_game_completion",
        "settlement_assumption=regulation_time",
    }
)

_PROVEN_CONTRADICTION_TOKENS = (
    "extra time",
    "extra-time",
    "penalties",
    "to qualify",
    "to-qualify",
)


def is_locked_paper_family(family: MarketFamily) -> bool:
    return family in LOCKED_PAPER_FAMILIES


def runner_outcomes(market: CanonicalMarket) -> set[CanonicalOutcome]:
    return {runner.outcome for runner in market.runners}


def required_outcomes_for_family(family: MarketFamily) -> frozenset[CanonicalOutcome] | None:
    if family is MarketFamily.MATCH_RESULT:
        return ORDINARY_1X2_OUTCOMES
    if family is MarketFamily.BOTH_TEAMS_TO_SCORE:
        return BTTS_OUTCOMES
    if family is MarketFamily.TOTAL_GOALS:
        return TOTAL_OUTCOMES
    if family is MarketFamily.FIRST_TEAM_TO_SCORE:
        return FTTS_OUTCOMES
    return None


def has_complete_locked_outcomes(market: CanonicalMarket) -> bool:
    required = required_outcomes_for_family(market.family)
    if required is None:
        return False
    present = runner_outcomes(market)
    if CanonicalOutcome.OTHER in present:
        return False
    return present == required


def is_safe_half_line_total(market: CanonicalMarket) -> bool:
    if market.family is not MarketFamily.TOTAL_GOALS:
        return False
    if market.line is None:
        return False
    return line_push_possible(market.line) is False


def structural_locked_identity(market: CanonicalMarket) -> bool:
    """Canonical family/period/outcome/line identity. Settlement proof is separate."""

    if market.family not in LOCKED_PAPER_FAMILIES:
        return False
    if market.period is not FootballPeriod.FULL_TIME:
        return False
    if market.settlement.period not in {FootballPeriod.FULL_TIME, FootballPeriod.UNKNOWN}:
        return False
    if not has_complete_locked_outcomes(market):
        return False
    if market.family is MarketFamily.TOTAL_GOALS:
        return is_safe_half_line_total(market)
    if market.family is MarketFamily.MATCH_RESULT:
        return is_ordinary_full_time_1x2(market)
    return True


def pair_structural_identity_matches(left: CanonicalMarket, right: CanonicalMarket) -> bool:
    if left.family != right.family:
        return False
    if not structural_locked_identity(left) or not structural_locked_identity(right):
        return False
    if left.period != right.period or left.line != right.line:
        return False
    return True


def matchbook_regulation_convention(market: CanonicalMarket) -> bool:
    """Matchbook documented full-time football convention. Payloads have no rule text."""

    if market.source_venue is not VenueName.MATCHBOOK:
        return False
    if not structural_locked_identity(market):
        return False
    if market.family is MarketFamily.MATCH_RESULT:
        return is_complete_regulation_time_1x2(market)
    settlement = market.settlement
    if not settlement.is_economically_complete():
        return False
    from sports_hedge.domain.football import SettlementScope

    if settlement.scope is not SettlementScope.REGULATION_TIME:
        return False
    if settlement.extra_time_included is not False or settlement.penalties_included is not False:
        return False
    if settlement.push_possible is True:
        return False
    if market.family is MarketFamily.TOTAL_GOALS:
        if settlement.line is None or settlement.line != market.line:
            return False
        if line_push_possible(market.line) is not False:
            return False
    return True


def kalshi_has_proven_settlement_contradiction(market: CanonicalMarket) -> bool:
    """True for extra-time / penalties / to-qualify evidence. Fair-price is not this."""

    from sports_hedge.domain.football import SettlementScope

    if market.source_venue is not VenueName.KALSHI:
        return False
    settlement = market.settlement
    if settlement.extra_time_included is True or settlement.penalties_included is True:
        return True
    if settlement.scope in {
        SettlementScope.INCLUDING_EXTRA_TIME,
        SettlementScope.INCLUDING_PENALTIES,
    }:
        return True
    if settlement.unknown_reason == KALSHI_CONTRACT_FAMILY_NOT_MATCH_RESULT_REASON:
        return True
    if settlement.unknown_reason == KALSHI_UNMODELLED_CANCEL_RESCHEDULE_FAIR_PRICE_REASON:
        return False
    if settlement.unknown_reason == KALSHI_UNMODELLED_EXTRA_TIME_OR_PENALTIES_REASON:
        return True
    unknown = str(settlement.unknown_reason or "").casefold()
    return any(token in unknown for token in _PROVEN_CONTRADICTION_TOKENS)


def both_independently_proven_regulation(left: CanonicalMarket, right: CanonicalMarket) -> bool:
    from sports_hedge.nba.settlement import nba_market_uses_paper_caveat
    from sports_hedge.nfl.settlement import nfl_market_uses_paper_caveat
    from sports_hedge.tennis.settlement import tennis_executable_block_reason

    if tennis_executable_block_reason(left, right) is not None:
        return False
    if nfl_market_uses_paper_caveat(left) or nfl_market_uses_paper_caveat(right):
        return False
    if nba_market_uses_paper_caveat(left) or nba_market_uses_paper_caveat(right):
        return False
    return (
        left.settlement.is_economically_complete()
        and right.settlement.is_economically_complete()
        and not settlement_fingerprints_contradict(left.settlement, right.settlement)
        and left.settlement.deterministic_key() == right.settlement.deterministic_key()
    )


def paper_assumed_locked_family(left: CanonicalMarket, right: CanonicalMarket) -> bool:
    """PAPER admission is the Approved Match Register after fixture identity.

    Settlement fingerprints, fair-price wording, and learned labels are not
    consulted. Extra-time / penalties / to-qualify contracts are a different
    native archetype and do not receive a register key.
    """

    return registered_structural_match(left, right)


def allow_unknown_settlement_for_paper_assumed(
    left: CanonicalMarket, right: CanonicalMarket
) -> bool:
    """Incomplete Kalshi settlement is allowed when the register admits the pair."""

    if allow_unknown_settlement_for_ordinary_1x2(left, right):
        return True
    return registered_structural_match(left, right)


def paper_assumed_match_reasons() -> list[str]:
    return list(PAPER_ASSUMED_AUDIT_REASONS)


def paper_assumed_solver_model(left: CanonicalMarket, right: CanonicalMarket) -> str | None:
    """Solver path for a registered PAPER pair. None if the register does not admit."""

    from sports_hedge.tennis.settlement import tennis_executable_block_reason

    if tennis_executable_block_reason(left, right) is not None:
        return None
    if not registered_structural_match(left, right):
        return None
    if left.family is MarketFamily.FIRST_TEAM_TO_SCORE:
        return "generalized_payoff"
    return "simple_complete_set"
