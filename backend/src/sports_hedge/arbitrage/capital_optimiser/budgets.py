"""Shared native-capital budgets for global PAPER allocation.

Mirrors the existing per-opportunity hard constraints in
`arbitrage.allocation.engine` but as jointly consumed capacities rather than
per-opportunity scale caps. Locked, transit and conditionally-releasable
amounts are never treated as spendable cash.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from sports_hedge.arbitrage.allocation.engine import (
    _fixture_native,
    _native_need_all,
    _native_need_external,
    _open_native,
    _reserve_required,
)
from sports_hedge.arbitrage.allocation.models import (
    AllocationBalance,
    AllocationConstraintKind,
    AllocationRequest,
    BankrollAllocationPolicy,
    OpenPositionExposure,
)
from sports_hedge.domain.models import VenueName


@dataclass(frozen=True)
class JointBudget:
    kind: AllocationConstraintKind
    detail: str
    venue: VenueName | None
    currency: str | None
    rhs: Decimal
    key: str


def unit_native_need(request: AllocationRequest) -> dict[tuple[VenueName, str], Decimal]:
    return _native_need_all(request.legs)


def unit_external_need(request: AllocationRequest) -> dict[tuple[VenueName, str], Decimal]:
    return _native_need_external(request.legs)


def reporting_capital(request: AllocationRequest) -> Decimal:
    return request.committed_capital_at_solver_size


def joint_budgets(
    *,
    balances: list[AllocationBalance],
    policy: BankrollAllocationPolicy,
    open_positions: list[OpenPositionExposure],
    fixture_ids: set[str],
) -> list[JointBudget]:
    """Capacities that simultaneous new allocations must share."""

    budgets: list[JointBudget] = []
    by_key = {(item.venue, item.currency): item for item in balances}

    for (venue, currency), balance in by_key.items():
        spendable = balance.spendable
        reserve = _reserve_required(policy, balance)
        remaining_after_reserve = spendable - reserve
        if remaining_after_reserve < 0:
            remaining_after_reserve = Decimal("0")
        budgets.append(
            JointBudget(
                kind=AllocationConstraintKind.NATIVE_VENUE_BALANCE,
                detail=f"native available {venue.value} {currency}",
                venue=venue,
                currency=currency,
                rhs=spendable,
                key=f"native:{venue.value}:{currency}",
            )
        )
        budgets.append(
            JointBudget(
                kind=AllocationConstraintKind.MIN_FREE_RESERVE,
                detail=f"reserve {venue.value} {currency}",
                venue=venue,
                currency=currency,
                rhs=remaining_after_reserve,
                key=f"reserve:{venue.value}:{currency}",
            )
        )
        venue_cap = policy.venue_limits_native.get(venue)
        if venue_cap is not None:
            open_native = _open_native(open_positions, venue, currency)
            room = venue_cap - open_native
            if room < 0:
                room = Decimal("0")
            budgets.append(
                JointBudget(
                    kind=AllocationConstraintKind.VENUE_LIMIT,
                    detail=f"configured venue limit {venue.value}",
                    venue=venue,
                    currency=currency,
                    rhs=room,
                    key=f"venue_limit:{venue.value}:{currency}",
                )
            )
        open_native = _open_native(open_positions, venue, currency)
        open_room = balance.pool_total * policy.max_open_capital_fraction - open_native
        if open_room < 0:
            open_room = Decimal("0")
        budgets.append(
            JointBudget(
                kind=AllocationConstraintKind.PORTFOLIO_CAP,
                detail=f"max open fraction {venue.value} {currency}",
                venue=venue,
                currency=currency,
                rhs=open_room,
                key=f"open_fraction:{venue.value}:{currency}",
            )
        )
        for event_id in sorted(fixture_ids):
            fixture_native = _fixture_native(open_positions, event_id, venue, currency)
            fixture_room = (
                balance.pool_total * policy.max_same_fixture_capital_fraction - fixture_native
            )
            if fixture_room < 0:
                fixture_room = Decimal("0")
            budgets.append(
                JointBudget(
                    kind=AllocationConstraintKind.FIXTURE_CONCENTRATION,
                    detail=f"same-fixture cap {event_id} {venue.value} {currency}",
                    venue=venue,
                    currency=currency,
                    rhs=fixture_room,
                    key=f"fixture:{event_id}:{venue.value}:{currency}",
                )
            )

    if policy.portfolio_cap_reporting is not None:
        open_reporting = sum(
            (position.capital_reporting or Decimal("0") for position in open_positions),
            Decimal("0"),
        )
        room = policy.portfolio_cap_reporting - open_reporting
        if room < 0:
            room = Decimal("0")
        budgets.append(
            JointBudget(
                kind=AllocationConstraintKind.PORTFOLIO_CAP,
                detail="total open-paper reporting cap",
                venue=None,
                currency=None,
                rhs=room,
                key="portfolio_reporting",
            )
        )
    if policy.external_leg_cap_native is not None:
        budgets.append(
            JointBudget(
                kind=AllocationConstraintKind.EXTERNAL_LEG_CAP,
                detail="manual/external native cap",
                venue=None,
                currency=None,
                rhs=policy.external_leg_cap_native,
                key="external_leg_cap",
            )
        )
    return budgets


def coefficient_for(budget: JointBudget, request: AllocationRequest) -> Decimal:
    native = unit_native_need(request)
    if budget.key.startswith("native:") or budget.key.startswith("reserve:"):
        assert budget.venue is not None and budget.currency is not None
        return native.get((budget.venue, budget.currency), Decimal("0"))
    if budget.key.startswith("venue_limit:") or budget.key.startswith("open_fraction:"):
        assert budget.venue is not None and budget.currency is not None
        return native.get((budget.venue, budget.currency), Decimal("0"))
    if budget.key.startswith("fixture:"):
        assert budget.venue is not None and budget.currency is not None
        event_id = request.canonical_event_id
        if not event_id:
            return Decimal("0")
        expected = f"fixture:{event_id}:{budget.venue.value}:{budget.currency}"
        if budget.key != expected:
            return Decimal("0")
        return native.get((budget.venue, budget.currency), Decimal("0"))
    if budget.key == "portfolio_reporting":
        return reporting_capital(request)
    if budget.key == "external_leg_cap":
        return sum(unit_external_need(request).values(), Decimal("0"))
    return Decimal("0")
