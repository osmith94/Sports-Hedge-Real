from __future__ import annotations

from enum import StrEnum

from sports_hedge.domain.football import (
    CanonicalMarket,
    CanonicalOutcome,
    FootballPeriod,
    MarketFamily,
    SettlementScope,
    line_push_possible,
)
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.matching.paper_assumed import paper_assumed_solver_model

# Step 7 allowlist: conventional families whose listed outcomes are mutually
# exclusive AND exhaustive for the existing complete-set solver (no unmodelled
# push/void state). DNB and integer-line Totals use the Step 8A generalized
# path instead. First Team To Score (HOME/AWAY/NO_GOAL, regulation time)
# uses the Step 8B generalized path. Asian Handicap, To Qualify, Correct Score,
# Next Goal, Double Chance, corners/cards, team totals and player props stay
# inventory-visible and solver-ineligible.
STEP7_COMPLETE_SET_FAMILIES: frozenset[MarketFamily] = frozenset(
    {
        MarketFamily.MATCH_RESULT,
        MarketFamily.BOTH_TEAMS_TO_SCORE,
        MarketFamily.TOTAL_GOALS,
    }
)

SOLVER_MODEL_SIMPLE = "simple_complete_set"
SOLVER_MODEL_GENERALIZED = "generalized_payoff"

DNB_STATES = ("home", "draw", "away")
INTEGER_TOTAL_STATES = ("over", "push", "under")
FIRST_TEAM_TO_SCORE_STATES = ("home_first", "away_first", "no_goal")
DNB_RUNNERS = frozenset({CanonicalOutcome.HOME, CanonicalOutcome.AWAY})
TOTAL_RUNNERS = frozenset({CanonicalOutcome.OVER, CanonicalOutcome.UNDER})
FIRST_TEAM_TO_SCORE_RUNNERS = frozenset(
    {CanonicalOutcome.HOME, CanonicalOutcome.AWAY, CanonicalOutcome.NO_GOAL}
)


class GeneralizedStateModel(StrEnum):
    DRAW_NO_BET = "draw_no_bet"
    INTEGER_TOTAL_GOALS = "integer_total_goals"
    FIRST_TEAM_TO_SCORE = "first_team_to_score"


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
UNPROVEN_HANDICAP_REASON = "unproven_handicap_semantics"
SPLIT_LINE_REASON = "generalized_split_line_not_modelled"
UNKNOWN_DRAW_VOID_REASON = "unknown_draw_void_semantics"
UNSUPPORTED_STATE_PAYOFF_FEE_BASIS = "unsupported_state_payoff_fee_basis"


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
    if market.family is MarketFamily.ASIAN_HANDICAP:
        return UNPROVEN_HANDICAP_REASON
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


def generalized_dnb_ready(market: CanonicalMarket) -> bool:
    if market.family is not MarketFamily.DRAW_NO_BET:
        return False
    present = runner_outcomes(market)
    if CanonicalOutcome.OTHER in present or present != DNB_RUNNERS:
        return False
    settlement = market.settlement
    if not settlement.is_economically_complete():
        return False
    return settlement.push_possible is True


def generalized_integer_totals_ready(market: CanonicalMarket) -> bool:
    if market.family is not MarketFamily.TOTAL_GOALS:
        return False
    present = runner_outcomes(market)
    if CanonicalOutcome.OTHER in present or present != TOTAL_RUNNERS:
        return False
    if market.line is None or market.settlement.line is None or market.settlement.line != market.line:
        return False
    if line_push_possible(market.line) is not True:
        return False
    settlement = market.settlement
    if not settlement.is_economically_complete():
        return False
    return settlement.push_possible is True


def generalized_first_team_to_score_ready(market: CanonicalMarket) -> bool:
    if market.family is not MarketFamily.FIRST_TEAM_TO_SCORE:
        return False
    present = runner_outcomes(market)
    if CanonicalOutcome.OTHER in present or present != FIRST_TEAM_TO_SCORE_RUNNERS:
        return False
    if market.period is not FootballPeriod.FULL_TIME:
        return False
    settlement = market.settlement
    if not settlement.is_economically_complete():
        return False
    if settlement.period is not FootballPeriod.FULL_TIME:
        return False
    if settlement.scope is not SettlementScope.REGULATION_TIME:
        return False
    if settlement.extra_time_included is not False or settlement.penalties_included is not False:
        return False
    return settlement.push_possible is False


def generalized_state_model(market: CanonicalMarket) -> GeneralizedStateModel | None:
    if generalized_dnb_ready(market):
        return GeneralizedStateModel.DRAW_NO_BET
    if generalized_integer_totals_ready(market):
        return GeneralizedStateModel.INTEGER_TOTAL_GOALS
    if generalized_first_team_to_score_ready(market):
        return GeneralizedStateModel.FIRST_TEAM_TO_SCORE
    return None


def generalized_payoff_eligible_market(market: CanonicalMarket) -> bool:
    return generalized_state_model(market) is not None


def generalized_payoff_eligible_pair(left: CanonicalMarket, right: CanonicalMarket) -> bool:
    model = generalized_state_model(left)
    return model is not None and model == generalized_state_model(right)


def solver_model_for_pair(left: CanonicalMarket, right: CanonicalMarket) -> str | None:
    if solver_eligible_market(left) and solver_eligible_market(right):
        return SOLVER_MODEL_SIMPLE
    paper_model = paper_assumed_solver_model(left, right)
    if paper_model is not None:
        return paper_model
    if generalized_payoff_eligible_pair(left, right):
        return SOLVER_MODEL_GENERALIZED
    return None


def generalized_state_model_for_pair(
    left: CanonicalMarket, right: CanonicalMarket
) -> GeneralizedStateModel | None:
    """Pair-level generalized model. Paper-assumed FTTS is Matchbook↔Kalshi only."""

    model = generalized_state_model(left)
    if model is not None and model == generalized_state_model(right):
        return model
    if (
        paper_assumed_solver_model(left, right) == SOLVER_MODEL_GENERALIZED
        and left.family is MarketFamily.FIRST_TEAM_TO_SCORE
    ):
        return GeneralizedStateModel.FIRST_TEAM_TO_SCORE
    return None


def scan_eligible_pair(left: CanonicalMarket, right: CanonicalMarket, match: MarketMatchResult) -> bool:
    if not match.matched:
        return False
    from sports_hedge.catalogue.admission import catalogue_allows_solver

    return catalogue_allows_solver(left, right)


def scan_ineligibility_reason(market: CanonicalMarket) -> str:
    if market.family is MarketFamily.ASIAN_HANDICAP:
        return UNPROVEN_HANDICAP_REASON
    if market.family is MarketFamily.TO_QUALIFY:
        return UNPROVEN_SETTLEMENT_REASON
    if market.family is MarketFamily.DRAW_NO_BET:
        present = runner_outcomes(market)
        if CanonicalOutcome.OTHER in present or present != DNB_RUNNERS:
            return INCOMPLETE_OUTCOME_REASON
        if market.settlement.push_possible is not True:
            return UNKNOWN_DRAW_VOID_REASON
        if not market.settlement.is_economically_complete():
            return "incomplete_settlement"
        return PUSH_STATE_REASON
    if market.family is MarketFamily.TOTAL_GOALS:
        if line_push_possible(market.line) is None:
            return SPLIT_LINE_REASON
        if line_push_possible(market.line) is True and not generalized_integer_totals_ready(market):
            present = runner_outcomes(market)
            if CanonicalOutcome.OTHER in present or present != TOTAL_RUNNERS:
                return INCOMPLETE_OUTCOME_REASON
            return "incomplete_settlement"
    if market.family is MarketFamily.FIRST_TEAM_TO_SCORE:
        present = runner_outcomes(market)
        if CanonicalOutcome.OTHER in present or present != FIRST_TEAM_TO_SCORE_RUNNERS:
            return INCOMPLETE_OUTCOME_REASON
        if not market.settlement.is_economically_complete():
            return "incomplete_settlement"
        if (
            market.period is not FootballPeriod.FULL_TIME
            or market.settlement.period is not FootballPeriod.FULL_TIME
            or market.settlement.scope is not SettlementScope.REGULATION_TIME
            or market.settlement.extra_time_included is not False
            or market.settlement.penalties_included is not False
        ):
            return UNPROVEN_SETTLEMENT_REASON
        if market.settlement.push_possible is not False:
            return UNPROVEN_SETTLEMENT_REASON
        return SOLVER_INELIGIBLE_REASON
    return solver_ineligibility_reason(market)
