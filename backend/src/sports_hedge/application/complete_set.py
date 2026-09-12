from __future__ import annotations

from sports_hedge.domain.football import (
    CanonicalMarket,
    CanonicalOutcome,
    FootballPeriod,
    MarketFamily,
    SettlementScope,
    line_push_possible,
)

# Step 7 allowlist: conventional families whose canonical outcome space is
# already mutually exclusive and exhaustive for the existing complete-set solver.
# Correct Score, Next Goal, First Team To Score, Double Chance, corners/cards,
# team totals and player props stay inventory-visible and solver-ineligible.
STEP7_COMPLETE_SET_FAMILIES: frozenset[MarketFamily] = frozenset(
    {
        MarketFamily.MATCH_RESULT,
        MarketFamily.DRAW_NO_BET,
        MarketFamily.BOTH_TEAMS_TO_SCORE,
        MarketFamily.TOTAL_GOALS,
        MarketFamily.ASIAN_HANDICAP,
        MarketFamily.TO_QUALIFY,
    }
)

COMPLETE_OUTCOME_SPACE: dict[MarketFamily, frozenset[CanonicalOutcome]] = {
    MarketFamily.MATCH_RESULT: frozenset(
        {CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY}
    ),
    MarketFamily.DRAW_NO_BET: frozenset({CanonicalOutcome.HOME, CanonicalOutcome.AWAY}),
    MarketFamily.BOTH_TEAMS_TO_SCORE: frozenset({CanonicalOutcome.YES, CanonicalOutcome.NO}),
    MarketFamily.TOTAL_GOALS: frozenset({CanonicalOutcome.OVER, CanonicalOutcome.UNDER}),
    MarketFamily.ASIAN_HANDICAP: frozenset({CanonicalOutcome.HOME, CanonicalOutcome.AWAY}),
    MarketFamily.TO_QUALIFY: frozenset(
        {CanonicalOutcome.HOME_QUALIFY, CanonicalOutcome.AWAY_QUALIFY}
    ),
}

LINE_FAMILIES: frozenset[MarketFamily] = frozenset(
    {MarketFamily.TOTAL_GOALS, MarketFamily.ASIAN_HANDICAP}
)

SOLVER_INELIGIBLE_REASON = "unsupported_outcome_model"
INCOMPLETE_OUTCOME_REASON = "incomplete_outcome_set"


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
    if market.family in LINE_FAMILIES:
        if market.line is None or settlement.line is None or settlement.line != market.line:
            return False
        if settlement.push_possible is None:
            return False
        expected_push = line_push_possible(market.line)
        if expected_push is None or settlement.push_possible is not expected_push:
            return False
    if market.family is MarketFamily.DRAW_NO_BET:
        if settlement.push_possible is not True:
            return False
        if settlement.scope is SettlementScope.INCLUDING_PENALTIES:
            return False
    if market.family is MarketFamily.TO_QUALIFY:
        if market.period is not FootballPeriod.FULL_TIME:
            return False
        if settlement.scope is SettlementScope.REGULATION_TIME:
            return False
        if settlement.extra_time_included is not True:
            return False
        if settlement.penalties_included is not True:
            return False
        if settlement.scope is not SettlementScope.INCLUDING_PENALTIES:
            return False
    return True


def solver_eligible_market(market: CanonicalMarket) -> bool:
    """Phase 1 complete-set solver may inspect only proven Step-7 families.

    Unsupported/ambiguous families, Correct Score, OTHER runners, incomplete
    runner sets and unproven settlement fingerprints stay visible in inventory
    but must not enter paper_scan.
    """

    if market.family not in STEP7_COMPLETE_SET_FAMILIES:
        return False
    if CanonicalOutcome.OTHER in runner_outcomes(market):
        return False
    if not has_complete_canonical_outcomes(market):
        return False
    return settlement_ready_for_complete_set(market)


def solver_ineligibility_reason(market: CanonicalMarket) -> str:
    if market.family not in STEP7_COMPLETE_SET_FAMILIES:
        return SOLVER_INELIGIBLE_REASON
    if CanonicalOutcome.OTHER in runner_outcomes(market):
        return SOLVER_INELIGIBLE_REASON
    if not has_complete_canonical_outcomes(market):
        return INCOMPLETE_OUTCOME_REASON
    if not settlement_ready_for_complete_set(market):
        return "incomplete_settlement"
    return SOLVER_INELIGIBLE_REASON
