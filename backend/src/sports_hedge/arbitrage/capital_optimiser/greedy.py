"""Greedy sequential per-opportunity PAPER allocation.

This is the feasible baseline that walks the existing `allocate()` engine in
deterministic ROI order, consuming remaining native cash as it goes. It is
not the global optimum when opportunities compete across multiple venue
pools.
"""

from __future__ import annotations

from decimal import Decimal

from sports_hedge.arbitrage.allocation.engine import (
    _capital_required,
    _plan_at_scale,
    allocate,
)
from sports_hedge.arbitrage.allocation.models import (
    AllocationBalance,
    AllocationRequest,
    BankrollAllocationPolicy,
    OpenPositionExposure,
    VenueNativeAmount,
)
from sports_hedge.arbitrage.capital_optimiser.models import (
    AllocationStrategyName,
    CapitalOptimiserOpportunity,
    OptimisedOpportunity,
    PortfolioPlan,
)
from sports_hedge.domain.models import VenueName


def greedy_sequential(
    *,
    candidates: list[tuple[CapitalOptimiserOpportunity, AllocationRequest, Decimal]],
    balances: list[AllocationBalance],
    open_positions: list[OpenPositionExposure],
    policy: BankrollAllocationPolicy,
    use_recommended: bool,
) -> PortfolioPlan:
    """Allocate in ROI order using the current per-opportunity engine.

    `candidates` is (opportunity, request_with_original_policy, standalone_scale_cap).
    """

    remaining_balances = [item.model_copy(deep=True) for item in balances]
    remaining_open = [item.model_copy(deep=True) for item in open_positions]
    ranked = sorted(
        candidates,
        key=lambda item: (
            -item[1].roi,
            -item[1].guaranteed_profit_at_solver_size,
            item[0].opportunity_id,
        ),
    )
    selected: list[OptimisedOpportunity] = []
    for opportunity, request, scale_cap in ranked:
        live = request.model_copy(
            update={
                "policy": policy,
                "balances": remaining_balances,
                "open_positions": remaining_open,
            }
        )
        result = allocate(live)
        if not result.accepted:
            continue
        scale = result.scale_recommended if use_recommended else result.scale_maximum
        if scale_cap > 0:
            scale = min(scale, scale_cap)
        if scale <= 0:
            continue
        selected.append(_opportunity_at_scale(opportunity, live, scale, result.scale_maximum))
        remaining_balances = _consume_balances(remaining_balances, live, scale)
        remaining_open = remaining_open + [_exposure_at_scale(opportunity, live, scale)]

    profit = sum((item.guaranteed_net_profit for item in selected), Decimal("0"))
    capital = sum((item.committed_capital_reporting for item in selected), Decimal("0"))
    return PortfolioPlan(
        strategy=AllocationStrategyName.GREEDY_SEQUENTIAL,
        guaranteed_net_profit=profit,
        committed_capital_reporting=capital,
        selected_count=len(selected),
        jointly_feasible=True,
        opportunities=selected,
        solver_status="greedy_sequential_allocate",
    )


def independent_per_opportunity(
    *,
    candidates: list[tuple[CapitalOptimiserOpportunity, AllocationRequest, Decimal]],
    balances: list[AllocationBalance],
    open_positions: list[OpenPositionExposure],
    policy: BankrollAllocationPolicy,
    use_recommended: bool,
) -> list[OptimisedOpportunity]:
    """Current scan-style sizing: each opportunity sees the full snapshot."""

    rows: list[OptimisedOpportunity] = []
    for opportunity, request, scale_cap in candidates:
        live = request.model_copy(
            update={
                "policy": policy,
                "balances": balances,
                "open_positions": open_positions,
            }
        )
        result = allocate(live)
        if not result.accepted:
            continue
        scale = result.scale_recommended if use_recommended else result.scale_maximum
        if scale_cap > 0:
            scale = min(scale, scale_cap)
        if scale <= 0:
            continue
        rows.append(_opportunity_at_scale(opportunity, live, scale, result.scale_maximum))
    return rows


def opportunity_at_scale(
    opportunity: CapitalOptimiserOpportunity,
    request: AllocationRequest,
    scale: Decimal,
    scale_maximum: Decimal,
) -> OptimisedOpportunity:
    return _opportunity_at_scale(opportunity, request, scale, scale_maximum)


def _opportunity_at_scale(
    opportunity: CapitalOptimiserOpportunity,
    request: AllocationRequest,
    scale: Decimal,
    scale_maximum: Decimal,
) -> OptimisedOpportunity:
    stakes = _plan_at_scale(request, scale) if scale > 0 else []
    return OptimisedOpportunity(
        opportunity_id=opportunity.opportunity_id,
        canonical_event_id=request.canonical_event_id,
        accepted=scale > 0,
        scale=scale,
        scale_maximum=scale_maximum,
        guaranteed_net_profit=request.guaranteed_profit_at_solver_size * scale,
        committed_capital_reporting=request.committed_capital_at_solver_size * scale,
        roi=request.roi,
        stakes=stakes,
        capital_required=_capital_required(stakes) if stakes else [],
    )


def _exposure_at_scale(
    opportunity: CapitalOptimiserOpportunity,
    request: AllocationRequest,
    scale: Decimal,
) -> OpenPositionExposure:
    stakes = _plan_at_scale(request, scale)
    native: dict[tuple[VenueName, str], Decimal] = {}
    for stake in stakes:
        key = (stake.venue, stake.native_currency)
        native[key] = native.get(key, Decimal("0")) + stake.capital_native
    return OpenPositionExposure(
        opportunity_id=opportunity.opportunity_id,
        canonical_event_id=request.canonical_event_id,
        capital_native=[
            VenueNativeAmount(venue=venue, currency=currency, amount=amount)
            for (venue, currency), amount in native.items()
        ],
        capital_reporting=request.committed_capital_at_solver_size * scale,
    )


def _consume_balances(
    balances: list[AllocationBalance],
    request: AllocationRequest,
    scale: Decimal,
) -> list[AllocationBalance]:
    used = unit_need_scaled(request, scale)
    updated: list[AllocationBalance] = []
    for balance in balances:
        amount = used.get((balance.venue, balance.currency), Decimal("0"))
        available = balance.available - amount
        if available < 0:
            available = Decimal("0")
        updated.append(
            balance.model_copy(
                update={
                    "available": available,
                    "locked": balance.locked + amount,
                }
            )
        )
    return updated


def unit_need_scaled(
    request: AllocationRequest, scale: Decimal
) -> dict[tuple[VenueName, str], Decimal]:
    from sports_hedge.arbitrage.capital_optimiser.budgets import unit_native_need

    return {key: value * scale for key, value in unit_native_need(request).items()}
