"""Deterministic conservative hold-vs-unwind rule."""

from __future__ import annotations

from decimal import Decimal

from sports_hedge.paper.unwind.models import (
    CapitalPressure,
    CapitalScarcityInput,
    UnwindPolicy,
    UnwindRecommendation,
)


def decide_recommendation(
    *,
    fully_executable: bool,
    fail_reasons: list[str],
    hold_pnl_gbp: Decimal,
    exit_pnl_gbp: Decimal,
    policy: UnwindPolicy,
    scarcity: CapitalScarcityInput,
    execution_risk_score: int | None,
) -> tuple[UnwindRecommendation, str]:
    if fail_reasons or not fully_executable:
        return UnwindRecommendation.UNWIND_NOT_SAFE, fail_reasons[0] if fail_reasons else "close_not_fully_executable"

    if policy.min_retained_exit_pnl_gbp is not None and exit_pnl_gbp < policy.min_retained_exit_pnl_gbp:
        return (
            UnwindRecommendation.HOLD,
            "exit_below_minimum_retained_profit",
        )
    if execution_risk_score is not None and execution_risk_score > policy.max_execution_risk:
        return UnwindRecommendation.UNWIND_NOT_SAFE, "close_execution_risk_exceeded"

    give_up = hold_pnl_gbp - exit_pnl_gbp
    if give_up <= 0:
        return UnwindRecommendation.UNWIND_ELIGIBLE, "exit_pnl_not_inferior_to_hold"

    if scarcity.pressure is CapitalPressure.SCARCE:
        proportional_cap = hold_pnl_gbp * policy.max_profit_give_up_ratio_when_scarce
        allowed = min(policy.max_profit_give_up_gbp_when_scarce, proportional_cap)
        if give_up <= allowed:
            return UnwindRecommendation.UNWIND_ELIGIBLE, "scarce_capital_accepts_bounded_give_up"
        return UnwindRecommendation.HOLD, "give_up_exceeds_scarce_capital_threshold"

    if give_up <= policy.max_profit_give_up_gbp:
        return UnwindRecommendation.UNWIND_ELIGIBLE, "give_up_within_abundant_threshold"
    return UnwindRecommendation.HOLD, "exit_inferior_to_hold_after_fees"
