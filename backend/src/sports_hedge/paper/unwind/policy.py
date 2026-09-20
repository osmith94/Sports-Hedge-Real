"""Deterministic conservative hold-vs-unwind rule.

Unwind cost is hold_pnl minus validated exit after fees/slippage/FX.
Remaining lock is advisory context. When 8C/treasury supplies
opportunity_cost_gbp, compare that modelled benefit of freeing capital
against unwind_cost. Do not invent a return from duration. Predicted
wall-clock match completion never settles or releases capital.

Exit margin is the same rule's economic slack: permitted give-up minus
unwind cost. It is not a parallel close model, not realised P&L, and
never overrides UNWIND_NOT_SAFE.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sports_hedge.paper.unwind.models import (
    CapitalPressure,
    CapitalScarcityInput,
    ExitMarginBasis,
    UnwindPolicy,
    UnwindRecommendation,
)


@dataclass(frozen=True)
class ExitEconomics:
    """Recommendation plus the economic threshold that produced it."""

    recommendation: UnwindRecommendation
    reason: str
    exit_threshold_gbp: Decimal | None = None
    exit_margin_gbp: Decimal | None = None
    exit_margin_basis: ExitMarginBasis = ExitMarginBasis.UNAVAILABLE
    exit_margin_actionable: bool = False
    close_blocker: str | None = None

    def blocked(self, reason: str) -> ExitEconomics:
        """Keep known economics, but safety/executability wins."""

        return ExitEconomics(
            recommendation=UnwindRecommendation.UNWIND_NOT_SAFE,
            reason=reason,
            exit_threshold_gbp=self.exit_threshold_gbp,
            exit_margin_gbp=self.exit_margin_gbp,
            exit_margin_basis=self.exit_margin_basis,
            exit_margin_actionable=False,
            close_blocker=reason,
        )


def _give_up_threshold(
    hold_pnl_gbp: Decimal,
    policy: UnwindPolicy,
    scarcity: CapitalScarcityInput,
) -> tuple[Decimal, ExitMarginBasis]:
    if scarcity.opportunity_cost_gbp is not None:
        return scarcity.opportunity_cost_gbp, ExitMarginBasis.SUPPLIED_OPPORTUNITY_COST
    if scarcity.pressure is CapitalPressure.SCARCE:
        proportional_cap = hold_pnl_gbp * policy.max_profit_give_up_ratio_when_scarce
        allowed = min(policy.max_profit_give_up_gbp_when_scarce, proportional_cap)
        return allowed, ExitMarginBasis.SCARCE_CAPITAL_BOUNDED_GIVE_UP
    return policy.max_profit_give_up_gbp, ExitMarginBasis.ABUNDANT_CAPITAL_GIVE_UP


def evaluate_exit_economics(
    *,
    fully_executable: bool,
    fail_reasons: list[str],
    hold_pnl_gbp: Decimal,
    exit_pnl_gbp: Decimal,
    policy: UnwindPolicy,
    scarcity: CapitalScarcityInput,
    execution_risk_score: int | None,
) -> ExitEconomics:
    """Same branches as decide_recommendation(), with threshold and margin.

    Exit margin = permitted give-up − actual give-up.
    Positive = inside the economic close threshold; negative = GBP away from
    eligibility. Numeric margin is not permission to close when unsafe.
    """

    if fail_reasons or not fully_executable:
        reason = fail_reasons[0] if fail_reasons else "close_not_fully_executable"
        return ExitEconomics(
            recommendation=UnwindRecommendation.UNWIND_NOT_SAFE,
            reason=reason,
            close_blocker=reason,
        )

    give_up = hold_pnl_gbp - exit_pnl_gbp

    if policy.min_retained_exit_pnl_gbp is not None and exit_pnl_gbp < policy.min_retained_exit_pnl_gbp:
        allowed = hold_pnl_gbp - policy.min_retained_exit_pnl_gbp
        return ExitEconomics(
            recommendation=UnwindRecommendation.HOLD,
            reason="exit_below_minimum_retained_profit",
            exit_threshold_gbp=allowed,
            exit_margin_gbp=allowed - give_up,
            exit_margin_basis=ExitMarginBasis.MIN_RETAINED_EXIT_PNL,
            exit_margin_actionable=True,
        )

    allowed, basis = _give_up_threshold(hold_pnl_gbp, policy, scarcity)
    margin = allowed - give_up

    if execution_risk_score is not None and execution_risk_score > policy.max_execution_risk:
        return ExitEconomics(
            recommendation=UnwindRecommendation.UNWIND_NOT_SAFE,
            reason="close_execution_risk_exceeded",
            exit_threshold_gbp=allowed,
            exit_margin_gbp=margin,
            exit_margin_basis=basis,
            exit_margin_actionable=False,
            close_blocker="close_execution_risk_exceeded",
        )

    if give_up <= 0:
        return ExitEconomics(
            recommendation=UnwindRecommendation.UNWIND_ELIGIBLE,
            reason="exit_pnl_not_inferior_to_hold",
            exit_threshold_gbp=allowed,
            exit_margin_gbp=margin,
            exit_margin_basis=basis,
            exit_margin_actionable=True,
        )

    if scarcity.opportunity_cost_gbp is not None:
        if give_up <= scarcity.opportunity_cost_gbp:
            return ExitEconomics(
                recommendation=UnwindRecommendation.UNWIND_ELIGIBLE,
                reason="unwind_cost_within_supplied_opportunity_cost",
                exit_threshold_gbp=allowed,
                exit_margin_gbp=margin,
                exit_margin_basis=basis,
                exit_margin_actionable=True,
            )
        return ExitEconomics(
            recommendation=UnwindRecommendation.HOLD,
            reason="unwind_cost_exceeds_supplied_opportunity_cost",
            exit_threshold_gbp=allowed,
            exit_margin_gbp=margin,
            exit_margin_basis=basis,
            exit_margin_actionable=True,
        )

    if scarcity.pressure is CapitalPressure.SCARCE:
        if give_up <= allowed:
            return ExitEconomics(
                recommendation=UnwindRecommendation.UNWIND_ELIGIBLE,
                reason="scarce_capital_accepts_bounded_give_up",
                exit_threshold_gbp=allowed,
                exit_margin_gbp=margin,
                exit_margin_basis=basis,
                exit_margin_actionable=True,
            )
        return ExitEconomics(
            recommendation=UnwindRecommendation.HOLD,
            reason="give_up_exceeds_scarce_capital_threshold",
            exit_threshold_gbp=allowed,
            exit_margin_gbp=margin,
            exit_margin_basis=basis,
            exit_margin_actionable=True,
        )

    if give_up <= policy.max_profit_give_up_gbp:
        return ExitEconomics(
            recommendation=UnwindRecommendation.UNWIND_ELIGIBLE,
            reason="give_up_within_abundant_threshold",
            exit_threshold_gbp=allowed,
            exit_margin_gbp=margin,
            exit_margin_basis=basis,
            exit_margin_actionable=True,
        )
    return ExitEconomics(
        recommendation=UnwindRecommendation.HOLD,
        reason="exit_inferior_to_hold_after_fees",
        exit_threshold_gbp=allowed,
        exit_margin_gbp=margin,
        exit_margin_basis=basis,
        exit_margin_actionable=True,
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
    economics = evaluate_exit_economics(
        fully_executable=fully_executable,
        fail_reasons=fail_reasons,
        hold_pnl_gbp=hold_pnl_gbp,
        exit_pnl_gbp=exit_pnl_gbp,
        policy=policy,
        scarcity=scarcity,
        execution_risk_score=execution_risk_score,
    )
    return economics.recommendation, economics.reason
