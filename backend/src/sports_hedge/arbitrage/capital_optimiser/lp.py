"""Continuous linear program for jointly feasible PAPER capital allocation.

Decision variables are per-opportunity scales on a solver-valid stake vector.
The formulation is a standard LP (HiGHS). The only discrete step is an
optional concurrency cardinality filter, applied only when the existing
max-open-opportunities policy is binding.
"""

from __future__ import annotations

from decimal import ROUND_DOWN, Decimal
from typing import Any, Callable, Sequence

from sports_hedge.arbitrage.allocation.models import (
    AllocationBalance,
    AllocationRequest,
    BankrollAllocationPolicy,
    OpenPositionExposure,
)
from sports_hedge.arbitrage.capital_optimiser.budgets import (
    JointBudget,
    coefficient_for,
    joint_budgets,
)
from sports_hedge.arbitrage.capital_optimiser.greedy import opportunity_at_scale
from sports_hedge.arbitrage.capital_optimiser.models import (
    AllocationStrategyName,
    BindingConstraint,
    CapitalOptimiserOpportunity,
    FormulationAudit,
    PortfolioPlan,
)

LinprogFn = Callable[..., Any]

SCALE_QUANT = Decimal("0.00000001")
SCALE_ZERO = Decimal("0.0000000001")
SLACK_BINDING = Decimal("0.00000001")
LP_ENGINE = "scipy.linprog.highs"


def solve_global_lp(
    *,
    candidates: list[tuple[CapitalOptimiserOpportunity, AllocationRequest, Decimal]],
    balances: list[AllocationBalance],
    open_positions: list[OpenPositionExposure],
    policy: BankrollAllocationPolicy,
    linprog: LinprogFn | None = None,
) -> tuple[PortfolioPlan, FormulationAudit]:
    discrete: list[str] = []
    solver = linprog or _load_linprog()
    if not candidates:
        empty = _empty_plan("no_eligible_opportunities")
        return empty, _audit(discrete)
    if solver is None:
        empty = _empty_plan("solver_unavailable")
        return empty, _audit(discrete)

    fixture_ids = {
        request.canonical_event_id
        for _, request, _ in candidates
        if request.canonical_event_id
    }
    budgets = joint_budgets(
        balances=balances,
        policy=policy,
        open_positions=open_positions,
        fixture_ids=fixture_ids,
    )
    scales, status = _linprog_scales(candidates, budgets, solver)
    if status != "solved":
        empty = _empty_plan(status)
        return empty, _audit(discrete)

    opportunities = [
        opportunity_at_scale(opportunity, request, scale, scale_cap)
        for (opportunity, request, scale_cap), scale in zip(candidates, scales, strict=True)
        if scale > SCALE_ZERO
    ]
    profit = sum((item.guaranteed_net_profit for item in opportunities), Decimal("0"))
    capital = sum((item.committed_capital_reporting for item in opportunities), Decimal("0"))
    binding = _binding_constraints(candidates, scales, budgets)
    plan = PortfolioPlan(
        strategy=AllocationStrategyName.GLOBAL_LINEAR_PROGRAM,
        guaranteed_net_profit=profit,
        committed_capital_reporting=capital,
        selected_count=len(opportunities),
        jointly_feasible=True,
        opportunities=opportunities,
        binding_constraints=binding,
        solver_status="solved",
    )
    return plan, _audit(discrete)


def _linprog_scales(
    candidates: Sequence[tuple[CapitalOptimiserOpportunity, AllocationRequest, Decimal]],
    budgets: Sequence[JointBudget],
    linprog: LinprogFn,
) -> tuple[list[Decimal], str]:
    n = len(candidates)
    if n == 0:
        return [], "solved"
    try:
        c = [-_to_float(item[1].guaranteed_profit_at_solver_size) for item in candidates]
        bounds = [(0.0, _to_float(item[2])) for item in candidates]
        a_ub: list[list[float]] = []
        b_ub: list[float] = []
        for budget in budgets:
            row = [_to_float(coefficient_for(budget, item[1])) for item in candidates]
            if all(value == 0.0 for value in row):
                continue
            a_ub.append(row)
            b_ub.append(_to_float(budget.rhs))
    except ValueError:
        return [Decimal("0")] * n, "non_finite_solver_input"

    kwargs: dict[str, Any] = {"bounds": bounds, "method": "highs"}
    if a_ub:
        kwargs["A_ub"] = a_ub
        kwargs["b_ub"] = b_ub
    try:
        result = linprog(c, **kwargs)
    except Exception:
        return [Decimal("0")] * n, "solver_failure"
    if result is None or not getattr(result, "success", False):
        return [Decimal("0")] * n, "solver_failure"
    raw = getattr(result, "x", None)
    if raw is None or len(raw) < n:
        return [Decimal("0")] * n, "solver_failure"

    scales: list[Decimal] = []
    for value, (_, _, cap) in zip(list(raw)[:n], candidates, strict=True):
        parsed = _finite_decimal(value)
        if parsed is None:
            return [Decimal("0")] * n, "non_finite_solver_output"
        if parsed < 0:
            parsed = Decimal("0")
        if parsed > cap:
            parsed = cap
        quantized = parsed.quantize(SCALE_QUANT, rounding=ROUND_DOWN)
        scales.append(quantized if quantized > 0 else Decimal("0"))

    scaled = _shrink_to_feasibility(candidates, scales, budgets)
    if scaled is None:
        return [Decimal("0")] * n, "numerical_validation_failed"
    return scaled, "solved"


def _shrink_to_feasibility(
    candidates: Sequence[tuple[CapitalOptimiserOpportunity, AllocationRequest, Decimal]],
    scales: list[Decimal],
    budgets: Sequence[JointBudget],
) -> list[Decimal] | None:
    current = list(scales)
    factor = Decimal("0.999999")
    for _ in range(32):
        if _feasible(candidates, current, budgets):
            return current
        current = [
            (value * factor).quantize(SCALE_QUANT, rounding=ROUND_DOWN) for value in current
        ]
        current = [value if value > SCALE_ZERO else Decimal("0") for value in current]
    if _feasible(candidates, [Decimal("0")] * len(current), budgets):
        return [Decimal("0")] * len(current)
    return None


def _feasible(
    candidates: Sequence[tuple[CapitalOptimiserOpportunity, AllocationRequest, Decimal]],
    scales: Sequence[Decimal],
    budgets: Sequence[JointBudget],
) -> bool:
    for budget in budgets:
        lhs = sum(
            (
                coefficient_for(budget, item[1]) * scale
                for item, scale in zip(candidates, scales, strict=True)
            ),
            Decimal("0"),
        )
        if lhs > budget.rhs:
            return False
    for (_, _, cap), scale in zip(candidates, scales, strict=True):
        if scale < 0 or scale > cap:
            return False
    return True


def _binding_constraints(
    candidates: Sequence[tuple[CapitalOptimiserOpportunity, AllocationRequest, Decimal]],
    scales: Sequence[Decimal],
    budgets: Sequence[JointBudget],
) -> list[BindingConstraint]:
    rows: list[BindingConstraint] = []
    for budget in budgets:
        lhs = sum(
            (
                coefficient_for(budget, item[1]) * scale
                for item, scale in zip(candidates, scales, strict=True)
            ),
            Decimal("0"),
        )
        slack = budget.rhs - lhs
        if slack < 0:
            slack = Decimal("0")
        threshold = max(SLACK_BINDING, budget.rhs * SLACK_BINDING)
        if slack <= threshold and (lhs > 0 or budget.rhs == 0):
            rows.append(
                BindingConstraint(
                    kind=budget.kind,
                    detail=budget.detail,
                    venue=budget.venue,
                    currency=budget.currency,
                    lhs=lhs,
                    rhs=budget.rhs,
                    slack=slack,
                )
            )
    return rows


def _empty_plan(status: str) -> PortfolioPlan:
    return PortfolioPlan(
        strategy=AllocationStrategyName.GLOBAL_LINEAR_PROGRAM,
        solver_status=status,
        jointly_feasible=status in {"solved", "no_eligible_opportunities"},
    )


def _audit(discrete: list[str]) -> FormulationAudit:
    return FormulationAudit(engine=LP_ENGINE, discrete_steps=discrete)


def _to_float(value: Decimal) -> float:
    as_float = float(value)
    if as_float != as_float or as_float in {float("inf"), float("-inf")}:
        raise ValueError("non_finite_solver_input")
    return as_float


def _finite_decimal(value: object) -> Decimal | None:
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if number.is_nan() or number.is_infinite():
        return None
    return number


def _load_linprog() -> LinprogFn | None:
    try:
        from scipy.optimize import linprog
    except Exception:
        return None
    return linprog
