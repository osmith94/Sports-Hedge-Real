"""Read-only global PAPER capital optimiser.

Maximises jointly feasible guaranteed net PAPER profit across simultaneous
approved-equivalent opportunities. This service is not on the scanning
critical path. Calling `optimise` never mutates treasury or places orders.
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any, Callable

from sports_hedge.arbitrage.allocation.engine import allocate
from sports_hedge.arbitrage.allocation.models import (
    AllocationBalance,
    AllocationRequest,
    BankrollAllocationPolicy,
    OpenPositionExposure,
)
from sports_hedge.arbitrage.capital_optimiser.budgets import joint_budgets
from sports_hedge.arbitrage.capital_optimiser.diagnostics import (
    allocated_native_from_opportunities,
    unused_capital_rows,
)
from sports_hedge.arbitrage.capital_optimiser.greedy import (
    greedy_sequential,
    independent_per_opportunity,
)
from sports_hedge.arbitrage.capital_optimiser.lp import solve_global_lp
from sports_hedge.arbitrage.capital_optimiser.models import (
    AllocationStrategyName,
    CapitalOptimiserOpportunity,
    CapitalOptimiserRequest,
    CapitalOptimiserResult,
    ExcludedOpportunity,
    OpportunityExclusionReason,
    PortfolioPlan,
    StrategyComparison,
)


class PaperCapitalOptimiser:
    """Independent PAPER decision-support allocator."""

    def __init__(self, *, linprog: Callable[..., Any] | None = None) -> None:
        self._linprog = linprog

    def optimise(self, request: CapitalOptimiserRequest) -> CapitalOptimiserResult:
        excluded, candidates = self._eligible(request)

        max_candidates = [(opp, req, cap) for opp, req, cap, _rec in candidates]
        rec_candidates = [(opp, req, rec) for opp, req, _cap, rec in candidates]

        greedy = greedy_sequential(
            candidates=max_candidates,
            balances=request.balances,
            open_positions=request.open_positions,
            policy=request.policy,
            use_recommended=False,
        )
        independent_rows = independent_per_opportunity(
            candidates=max_candidates,
            balances=request.balances,
            open_positions=request.open_positions,
            policy=request.policy,
            use_recommended=False,
        )
        independent = _independent_plan(
            independent_rows,
            balances=request.balances,
            policy=request.policy,
            open_positions=request.open_positions,
        )
        maximum, max_audit = solve_global_lp(
            candidates=max_candidates,
            balances=request.balances,
            open_positions=request.open_positions,
            policy=request.policy,
            linprog=self._linprog,
        )
        recommended, rec_audit = solve_global_lp(
            candidates=rec_candidates,
            balances=request.balances,
            open_positions=request.open_positions,
            policy=request.policy,
            linprog=self._linprog,
        )
        formulation = rec_audit.model_copy(
            update={"discrete_steps": list(dict.fromkeys(max_audit.discrete_steps + rec_audit.discrete_steps))}
        )
        maximum = _with_unused(maximum, request)
        recommended = _with_unused(recommended, request)
        greedy = _with_unused(greedy, request)
        comparison = StrategyComparison(
            global_guaranteed_net_profit=maximum.guaranteed_net_profit,
            greedy_guaranteed_net_profit=greedy.guaranteed_net_profit,
            independent_guaranteed_net_profit=independent.guaranteed_net_profit,
            independent_jointly_feasible=independent.jointly_feasible,
            improvement_vs_greedy=maximum.guaranteed_net_profit - greedy.guaranteed_net_profit,
        )
        return CapitalOptimiserResult(
            formulation=formulation,
            recommended=recommended,
            maximum_validated=maximum,
            greedy=greedy,
            independent=independent,
            comparison=comparison,
            excluded=excluded,
            fx_source=request.fx_source,
            fx_as_of=request.fx_as_of,
            min_net_arb=request.min_net_arb,
        )

    def _eligible(
        self, request: CapitalOptimiserRequest
    ) -> tuple[
        list[ExcludedOpportunity],
        list[tuple[CapitalOptimiserOpportunity, AllocationRequest, Decimal, Decimal]],
    ]:
        excluded: list[ExcludedOpportunity] = []
        candidates: list[
            tuple[CapitalOptimiserOpportunity, AllocationRequest, Decimal, Decimal]
        ] = []
        for opportunity in request.opportunities:
            reason = _exclusion(opportunity, request)
            if reason is not None:
                excluded.append(reason)
                continue
            live = opportunity.request.model_copy(
                update={
                    "policy": request.policy,
                    "balances": request.balances,
                    "open_positions": request.open_positions,
                }
            )
            standalone = allocate(live)
            if not standalone.accepted or standalone.scale_maximum <= 0:
                excluded.append(
                    ExcludedOpportunity(
                        opportunity_id=opportunity.opportunity_id,
                        reason=OpportunityExclusionReason.STANDALONE_REJECTED,
                        detail=standalone.rejection_reason
                        or (
                            standalone.limiting_constraint.value
                            if standalone.limiting_constraint
                            else "standalone_rejected"
                        ),
                    )
                )
                continue
            rec = standalone.scale_recommended if standalone.scale_recommended > 0 else standalone.scale_maximum
            candidates.append((opportunity, live, standalone.scale_maximum, rec))
        return excluded, candidates


def request_from_treasury(
    snapshot,
    *,
    opportunities: list[CapitalOptimiserOpportunity],
    policy: BankrollAllocationPolicy | None = None,
    open_positions: list[OpenPositionExposure] | None = None,
    min_net_arb: Decimal = Decimal("0"),
) -> CapitalOptimiserRequest:
    """Build an optimiser request from a treasury snapshot without mutating it."""

    from sports_hedge.arbitrage.allocation.adapters import balances_from_treasury

    session = getattr(snapshot, "session", None)
    fx: dict[str, Decimal] = {"GBP": Decimal("1")}
    fx_source = ""
    fx_as_of = None
    if session is not None:
        fx["USD"] = session.fx_rate_usd_gbp
        fx_source = session.fx_source
        fx_as_of = session.fx_as_of
    balances = balances_from_treasury(snapshot, gbp_per_unit=fx)
    return CapitalOptimiserRequest(
        opportunities=opportunities,
        balances=balances,
        open_positions=open_positions or [],
        policy=policy or BankrollAllocationPolicy(),
        min_net_arb=min_net_arb,
        approved_fx=fx,
        fx_source=fx_source,
        fx_as_of=fx_as_of,
    )


def _exclusion(
    opportunity: CapitalOptimiserOpportunity, request: CapitalOptimiserRequest
) -> ExcludedOpportunity | None:
    if not opportunity.approved_equivalent:
        return ExcludedOpportunity(
            opportunity_id=opportunity.opportunity_id,
            reason=OpportunityExclusionReason.NOT_APPROVED_EQUIVALENT,
            detail="opportunity is not marked approved-equivalent",
        )
    alloc = opportunity.request
    if not alloc.is_arbitrage:
        return ExcludedOpportunity(
            opportunity_id=opportunity.opportunity_id,
            reason=OpportunityExclusionReason.SOLVER_NOT_ARBITRAGE,
            detail="solver_not_arbitrage",
        )
    if alloc.guaranteed_profit_at_solver_size <= 0:
        return ExcludedOpportunity(
            opportunity_id=opportunity.opportunity_id,
            reason=OpportunityExclusionReason.NON_POSITIVE_PROFIT,
            detail="guaranteed net profit is not strictly positive",
        )
    if alloc.roi < request.min_net_arb:
        return ExcludedOpportunity(
            opportunity_id=opportunity.opportunity_id,
            reason=OpportunityExclusionReason.BELOW_MIN_NET_ARB,
            detail=f"roi={alloc.roi} < min_net_arb={request.min_net_arb}",
        )
    currencies = {leg.native_currency.upper() for leg in alloc.legs}
    needs_fx = any(currency != "GBP" for currency in currencies)
    if needs_fx and not request.fx_source.strip():
        return ExcludedOpportunity(
            opportunity_id=opportunity.opportunity_id,
            reason=OpportunityExclusionReason.MISSING_FX_SNAPSHOT,
            detail="approved FX snapshot source missing; conversion is not assumed 1:1",
        )
    for leg in alloc.legs:
        currency = leg.native_currency.upper()
        if currency == "GBP":
            continue
        rate = request.approved_fx.get(currency)
        if rate is None:
            return ExcludedOpportunity(
                opportunity_id=opportunity.opportunity_id,
                reason=OpportunityExclusionReason.MISSING_FX_SNAPSHOT,
                detail=f"missing approved FX rate for {currency}",
            )
        if rate != leg.gbp_per_unit:
            return ExcludedOpportunity(
                opportunity_id=opportunity.opportunity_id,
                reason=OpportunityExclusionReason.FX_RATE_MISMATCH,
                detail=f"{currency} leg rate {leg.gbp_per_unit} != snapshot {rate}",
            )
    return None


def _independent_plan(
    rows,
    *,
    balances: list[AllocationBalance],
    policy: BankrollAllocationPolicy,
    open_positions: list[OpenPositionExposure],
) -> PortfolioPlan:
    profit = sum((item.guaranteed_net_profit for item in rows), Decimal("0"))
    capital = sum((item.committed_capital_reporting for item in rows), Decimal("0"))
    feasible, reasons = _jointly_feasible(
        rows, balances=balances, policy=policy, open_positions=open_positions
    )
    return PortfolioPlan(
        strategy=AllocationStrategyName.INDEPENDENT_PER_OPPORTUNITY,
        guaranteed_net_profit=profit,
        committed_capital_reporting=capital,
        selected_count=len(rows),
        jointly_feasible=feasible,
        infeasibility_reasons=reasons,
        opportunities=rows,
        solver_status="independent_full_snapshot",
    )


def _jointly_feasible(
    rows,
    *,
    balances: list[AllocationBalance],
    policy: BankrollAllocationPolicy,
    open_positions: list[OpenPositionExposure],
) -> tuple[bool, list[str]]:
    if not rows:
        return True, []
    fixture_ids = {item.canonical_event_id for item in rows if item.canonical_event_id}
    budgets = joint_budgets(
        balances=balances,
        policy=policy,
        open_positions=open_positions,
        fixture_ids=fixture_ids,
    )
    reasons: list[str] = []
    used = allocated_native_from_opportunities(rows)
    for budget in budgets:
        lhs = Decimal("0")
        if budget.venue is not None and budget.currency is not None:
            if budget.key.startswith("fixture:"):
                event_id = budget.key.split(":")[1]
                lhs = sum(
                    (
                        stake.amount
                        for row in rows
                        if row.canonical_event_id == event_id
                        for stake in row.capital_required
                        if stake.venue is budget.venue and stake.currency == budget.currency
                    ),
                    Decimal("0"),
                )
            elif budget.key == "external_leg_cap":
                lhs = Decimal("0")
            else:
                lhs = used.get((budget.venue, budget.currency), Decimal("0"))
        elif budget.key == "portfolio_reporting":
            lhs = sum((row.committed_capital_reporting for row in rows), Decimal("0"))
        elif budget.key == "external_leg_cap":
            lhs = Decimal("0")
            for row in rows:
                for stake in row.stakes:
                    if stake.execution_mode == "EXTERNAL_OPERATOR":
                        lhs += stake.capital_native
        if lhs > budget.rhs:
            reasons.append(f"{budget.key}:lhs={lhs}:rhs={budget.rhs}")
    return (not reasons), reasons


def _with_unused(plan: PortfolioPlan, request: CapitalOptimiserRequest) -> PortfolioPlan:
    used = allocated_native_from_opportunities(plan.opportunities)
    unused = unused_capital_rows(
        balances=request.balances,
        policy=request.policy,
        allocated_native=used,
        binding=plan.binding_constraints,
    )
    return plan.model_copy(update={"unused_capital": unused})
