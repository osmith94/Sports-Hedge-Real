from __future__ import annotations

from sports_hedge.domain.football import (
    CanonicalMarket,
    CanonicalOutcome,
    MarketFamily,
    line_push_possible,
)

# Step 7 allowlist: conventional families whose listed outcomes are mutually
# exclusive AND exhaustive for the existing complete-set solver (no unmodelled
# push/void state). DNB, To Qualify, Correct Score, Next Goal, First Team To
# Score, Double Chance, corners/cards, team totals and player props stay
# inventory-visible and solver-ineligible.
STEP7_COMPLETE_SET_FAMILIES: frozenset[MarketFamily] = frozenset(
    {
        MarketFamily.MATCH_RESULT,
        MarketFamily.BOTH_TEAMS_TO_SCORE,
        MarketFamily.TOTAL_GOALS,
        MarketFamily.ASIAN_HANDICAP,
    }
)

COMPLETE_OUTCOME_SPACE: dict[MarketFamily, frozenset[CanonicalOutcome]] = {
    MarketFamily.MATCH_RESULT: frozenset(
        {CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY}
    ),
    MarketFamily.BOTH_TEAMS_TO_SCORE: frozenset({CanonicalOutcome.YES, CanonicalOutcome.NO}),
    MarketFamily.TOTAL_GOALS: frozenset({CanonicalOutcome.OVER, CanonicalOutcome.UNDER}),
    MarketFamily.ASIAN_HANDICAP: frozenset({CanonicalOutcome.HOME, CanonicalOutcome.AWAY}),
}

LINE_FAMILIES: frozenset[MarketFamily] = frozenset(
    {MarketFamily.TOTAL_GOALS, MarketFamily.ASIAN_HANDICAP}
)

SOLVER_INELIGIBLE_REASON = "unsupported_outcome_model"
INCOMPLETE_OUTCOME_REASON = "incomplete_outcome_set"
PUSH_STATE_REASON = "push_state_not_modelled"
UNPROVEN_SETTLEMENT_REASON = "unproven_settlement_semantics"


def complete_set_outcomes(family: MarketFamily) -> frozenset[CanonicalOutcome] | None:
    return COMPLETE_OUTCOME_SPACE.get(family)


def runner_outcomes(market: CanonicalMarket) -> set[CanonicalOutcome]:
    return {runner.outcome for runner in market.runners}


def has_complete_canonical_outcomes(market: CanonicalMarket) -> bool:
    required = complete_set_outcomes(market.family)
    if required is None:
        return False
    present = runner_outcomes(market)
    if CanonicalOutcome.OTHER in present:
        return False
    return present == required


def settlement_ready_for_complete_set(market: CanonicalMarket) -> bool:
    settlement = market.settlement
    if not settlement.is_economically_complete():
        return False
    # The simple complete-set solver cannot represent a refund/void state.
    if settlement.push_possible is True:
        return False
    if market.family in LINE_FAMILIES:
        if market.line is None or settlement.line is None or settlement.line != market.line:
            return False
        expected_push = line_push_possible(market.line)
        if expected_push is not False or settlement.push_possible is not False:
            return False
    return True


def solver_eligible_market(market: CanonicalMarket) -> bool:
    """Phase 1 solver may inspect only proven no-push complete canonical spaces."""

    if market.family not in STEP7_COMPLETE_SET_FAMILIES:
        return False
    if CanonicalOutcome.OTHER in runner_outcomes(market):
        return False
    if not has_complete_canonical_outcomes(market):
        return False
    return settlement_ready_for_complete_set(market)


def solver_ineligibility_reason(market: CanonicalMarket) -> str:
    if market.family is MarketFamily.DRAW_NO_BET:
        return PUSH_STATE_REASON
    if market.family is MarketFamily.TO_QUALIFY:
        return UNPROVEN_SETTLEMENT_REASON
    if market.family not in STEP7_COMPLETE_SET_FAMILIES:
        return SOLVER_INELIGIBLE_REASON
    if CanonicalOutcome.OTHER in runner_outcomes(market):
        return SOLVER_INELIGIBLE_REASON
    if not has_complete_canonical_outcomes(market):
        return INCOMPLETE_OUTCOME_REASON
    if market.settlement.push_possible is True:
        return PUSH_STATE_REASON
    if market.family in LINE_FAMILIES and line_push_possible(market.line) is not False:
        return PUSH_STATE_REASON
    if not settlement_ready_for_complete_set(market):
        return "incomplete_settlement"
    return SOLVER_INELIGIBLE_REASON
