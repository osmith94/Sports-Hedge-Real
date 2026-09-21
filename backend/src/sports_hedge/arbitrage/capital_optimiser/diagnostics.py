"""Diagnostics for unused/shadow capital after a modelled portfolio plan."""

from __future__ import annotations

from decimal import Decimal

from sports_hedge.arbitrage.allocation.engine import _reserve_required
from sports_hedge.arbitrage.allocation.models import (
    AllocationBalance,
    AllocationConstraintKind,
    BankrollAllocationPolicy,
)
from sports_hedge.arbitrage.capital_optimiser.models import (
    BindingConstraint,
    UnusedCapital,
    UnusedCapitalClass,
)
from sports_hedge.domain.models import VenueName


def unused_capital_rows(
    *,
    balances: list[AllocationBalance],
    policy: BankrollAllocationPolicy,
    allocated_native: dict[tuple[VenueName, str], Decimal],
    binding: list[BindingConstraint],
) -> list[UnusedCapital]:
    binding_keys = {
        (item.kind, item.venue, item.currency)
        for item in binding
        if item.venue is not None and item.currency is not None
    }
    rows: list[UnusedCapital] = []
    for balance in balances:
        used = allocated_native.get((balance.venue, balance.currency), Decimal("0"))
        unallocated = balance.spendable - used
        if unallocated < 0:
            unallocated = Decimal("0")
        reserve = _reserve_required(policy, balance)
        reserve_remaining = min(unallocated, reserve)
        extra = unallocated - reserve_remaining
        notes: list[str] = []
        if balance.locked > 0:
            notes.append("locked_capital_is_not_spendable")
        if balance.transit > 0:
            notes.append("transit_capital_is_not_spendable")
        if balance.conditionally_releasable > 0:
            notes.append("conditionally_releasable_is_not_spendable_until_unwind_or_settlement")
        classification = _classify(
            extra=extra,
            unallocated=unallocated,
            reserve_remaining=reserve_remaining,
            balance=balance,
            binding_keys=binding_keys,
            notes=notes,
        )
        rows.append(
            UnusedCapital(
                venue=balance.venue,
                currency=balance.currency,
                available_before=balance.available,
                allocated_native=used,
                unallocated_spendable=unallocated,
                reserve_required=reserve,
                reserve_remaining=reserve_remaining,
                locked=balance.locked,
                transit=balance.transit,
                conditionally_releasable=balance.conditionally_releasable,
                classification=classification,
                notes=notes,
            )
        )
    return rows


def allocated_native_from_opportunities(opportunities) -> dict[tuple[VenueName, str], Decimal]:
    used: dict[tuple[VenueName, str], Decimal] = {}
    for opportunity in opportunities:
        for item in opportunity.capital_required:
            key = (item.venue, item.currency)
            used[key] = used.get(key, Decimal("0")) + item.amount
    return used


def _classify(
    *,
    extra: Decimal,
    unallocated: Decimal,
    reserve_remaining: Decimal,
    balance: AllocationBalance,
    binding_keys: set[tuple[AllocationConstraintKind, VenueName | None, str | None]],
    notes: list[str],
) -> UnusedCapitalClass:
    if extra <= 0 and unallocated <= 0 and balance.locked == 0 and balance.transit == 0:
        return UnusedCapitalClass.FULLY_ALLOCATED
    if extra <= 0 and reserve_remaining > 0:
        notes.append("unallocated_cash_held_as_configured_reserve")
        return UnusedCapitalClass.HELD_AS_RESERVE
    if extra <= 0 and balance.locked > 0:
        return UnusedCapitalClass.LOCKED_NOT_SPENDABLE
    if extra <= 0 and balance.transit > 0:
        return UnusedCapitalClass.TRANSIT_NOT_SPENDABLE
    other_binding = any(
        kind
        in {
            AllocationConstraintKind.NATIVE_VENUE_BALANCE,
            AllocationConstraintKind.MIN_FREE_RESERVE,
            AllocationConstraintKind.VENUE_LIMIT,
            AllocationConstraintKind.PORTFOLIO_CAP,
            AllocationConstraintKind.FIXTURE_CONCENTRATION,
        }
        and (venue, currency) != (balance.venue, balance.currency)
        for kind, venue, currency in binding_keys
    )
    if extra > 0 and other_binding:
        notes.append("leftover_native_cash_cannot_fund_another_leg_of_the_selected_set")
        return UnusedCapitalClass.BLOCKED_BY_OTHER_CONSTRAINT
    notes.append("no_remaining_eligible_opportunity_can_use_this_native_cash")
    return UnusedCapitalClass.NO_REMAINING_ELIGIBLE_USE
