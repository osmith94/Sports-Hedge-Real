"""Treasury/allocator scarcity input for hold-vs-unwind. No new bankroll model."""

from __future__ import annotations

from decimal import Decimal

from sports_hedge.arbitrage.allocation.adapters import balances_from_treasury
from sports_hedge.arbitrage.allocation.models import (
    AllocationBalance,
    AllocationConstraintKind,
    BankrollAllocationPolicy,
)
from sports_hedge.arbitrage.allocation.policy import default_bankroll_policy
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.paper.position_management.models import CompetingOpportunityInput
from sports_hedge.paper.unwind.models import CapitalPressure, CapitalScarcityInput, venue_currency_key


_CAPITAL_CONSTRAINTS = frozenset(
    {
        AllocationConstraintKind.NATIVE_VENUE_BALANCE,
        AllocationConstraintKind.VENUE_LIMIT,
        AllocationConstraintKind.MAX_POOL_FRACTION,
        AllocationConstraintKind.MIN_FREE_RESERVE,
        AllocationConstraintKind.PORTFOLIO_CAP,
        AllocationConstraintKind.CONCURRENCY,
    }
)


def build_capital_scarcity(
    *,
    treasury_snapshot=None,
    policy: BankrollAllocationPolicy | None = None,
    competing: list[CompetingOpportunityInput] | None = None,
    locked_venue_keys: set[str] | None = None,
    gbp_per_unit: dict[str, Decimal] | None = None,
) -> CapitalScarcityInput:
    """Distinguish ABUNDANT vs SCARCE from authoritative treasury and allocator facts.

    ``opportunity_cost_gbp`` is populated only from a competing qualifying
    opportunity's expected guaranteed profit. Minutes-to-settlement never
    fabricates opportunity cost.
    """

    resolved_policy = policy or default_bankroll_policy()
    competing_rows = list(competing or [])
    locked = locked_venue_keys or set()
    pressure = CapitalPressure.ABUNDANT
    details: list[str] = []

    if treasury_snapshot is not None:
        balances = balances_from_treasury(treasury_snapshot, gbp_per_unit=gbp_per_unit)
        for balance in balances:
            reserve = _reserve_required(resolved_policy, balance)
            remaining = balance.spendable - reserve
            if remaining <= 0:
                pressure = CapitalPressure.SCARCE
                details.append(
                    f"reserve_pressure:{balance.venue.value}:{balance.currency}"
                )
            venue_cap = resolved_policy.venue_limits_native.get(balance.venue)
            if venue_cap is not None and balance.locked >= venue_cap:
                pressure = CapitalPressure.SCARCE
                details.append(f"venue_cap_pressure:{balance.venue.value}")

    opportunity_cost: Decimal | None = None
    for row in competing_rows:
        overlaps = _overlaps_locked(row.required_native, locked) if locked else bool(row.required_native)
        contends = row.allocator_accepted or row.capital_constrained
        if not contends:
            continue
        if locked and row.required_native and not overlaps:
            continue
        pressure = CapitalPressure.SCARCE
        details.append(f"competing_opportunity:{row.opportunity_id}")
        if row.expected_guaranteed_profit_gbp > 0:
            if opportunity_cost is None or row.expected_guaranteed_profit_gbp > opportunity_cost:
                opportunity_cost = row.expected_guaranteed_profit_gbp

    return CapitalScarcityInput(
        pressure=pressure,
        detail="; ".join(details) if details else "treasury_abundant_no_competing_opportunity",
        opportunity_cost_gbp=opportunity_cost,
    )


def _reserve_required(policy: BankrollAllocationPolicy, balance: AllocationBalance) -> Decimal:
    fractional = balance.pool_total * policy.min_reserve_fraction
    if policy.min_reserve_amount is None:
        return fractional
    return max(fractional, policy.min_reserve_amount)


def _overlaps_locked(required_native: dict[str, Decimal], locked: set[str]) -> bool:
    for key, amount in required_native.items():
        if amount > 0 and key in locked:
            return True
    return False


def competing_from_paper_decisions(
    decisions: list[PaperScanDecision],
    *,
    exclude_opportunity_ids: set[str] | None = None,
) -> list[CompetingOpportunityInput]:
    """Traceable competing allocator economics from the current scan cycle."""

    excluded = exclude_opportunity_ids or set()
    rows: list[CompetingOpportunityInput] = []
    for decision in decisions:
        if not decision.canonical_market_id:
            continue
        opportunity_id = decision.canonical_market_id
        if opportunity_id in excluded:
            continue
        allocation = decision.allocation
        if allocation is None:
            continue
        if not decision.eligible_for_paper_simulation and not allocation.accepted:
            continue
        constrained = allocation.limiting_constraint in _CAPITAL_CONSTRAINTS
        if not allocation.accepted and not constrained:
            continue
        required: dict[str, Decimal] = {}
        for item in allocation.capital_required:
            key = venue_currency_key(item.venue, item.currency)
            required[key] = required.get(key, Decimal("0")) + item.amount
        profit = allocation.guaranteed_profit
        if profit <= 0:
            continue
        rows.append(
            CompetingOpportunityInput(
                opportunity_id=opportunity_id,
                expected_guaranteed_profit_gbp=profit,
                allocator_accepted=allocation.accepted,
                capital_constrained=constrained,
                required_native=required,
                detail=(
                    f"scan_allocator:{opportunity_id}:accepted={allocation.accepted}:"
                    f"constraint={allocation.limiting_constraint}"
                ),
            )
        )
    return rows
