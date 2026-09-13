from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field

from sports_hedge.arbitrage.allocation.models import AllocationConstraintKind, NativeBalanceAfter
from sports_hedge.arbitrage.watchlist.models import NearOpportunity, OpportunityStatus
from sports_hedge.paper.chain import PaperFillPlan

ACTIONABLE_STATUSES = {
    OpportunityStatus.TRIGGERED,
    OpportunityStatus.PAPER_FILLING,
}

PAPER_CONFIRM_CTA = "Confirm paper OPEN"
LIVE_PLACE_CTA = "Place bets via API"
LIVE_BLOCKED_REASON = "phase_1_paper_only_execution_disabled"


def bet_ticket_action(
    *,
    plan: PaperFillPlan | None,
    watch: NearOpportunity | None,
    solver_is_arbitrage: bool,
) -> tuple[bool, str | None]:
    """Whether a row may expose an active BET control.

    Qualification is the existing prepare/fill-plan seam, not raw edge.
    Rejected, unevaluated, stale, unsupported, or below-trigger rows stay
    non-actionable even if displayed edge is positive.
    """

    if watch is not None and watch.status not in ACTIONABLE_STATUSES:
        if watch.status is OpportunityStatus.REJECTED:
            return False, (watch.rejection_reasons[0] if watch.rejection_reasons else "rejected")
        if watch.status in {OpportunityStatus.WATCHING, OpportunityStatus.APPROACHING}:
            return False, "below_trigger"
        if watch.status is OpportunityStatus.EXPIRED:
            return False, "expired"
        if watch.status is OpportunityStatus.CLOSED:
            return False, "closed"
        return False, f"status_{watch.status.value.lower()}"
    if plan is None:
        return False, "unevaluated"
    if not plan.settlement_equivalent or not plan.decision.market_match.matched:
        return False, "market_not_equivalent"
    if not plan.eligible_for_paper_simulation:
        return False, "not_eligible_for_paper_simulation"
    if not solver_is_arbitrage:
        return False, "solver_not_arbitrage"
    blockers = [
        reason
        for reason in plan.decision.rejection_reasons
        if not reason.startswith("allocation_failed")
    ]
    if blockers:
        return False, blockers[0]
    return True, None


class BetTicketExecutionSeam(BaseModel):
    """Phase 1 paper confirm CTA. Live venue placement is a future, separately approved mode."""

    mode: Literal["paper"] = "paper"
    execution_enabled: bool = False
    places_orders: bool = False
    paper_confirm_cta: str = PAPER_CONFIRM_CTA
    live_cta: str = LIVE_PLACE_CTA
    live_cta_available: bool = False
    live_blocked_reason: str = LIVE_BLOCKED_REASON


class BetTicketTreasuryRemaining(BaseModel):
    venue: str
    currency: str
    free_balance: Decimal
    reserve_remaining: Decimal
    locked: Decimal
    allocated_native: Decimal

    @classmethod
    def from_balance(cls, row: NativeBalanceAfter) -> "BetTicketTreasuryRemaining":
        return cls(
            venue=row.venue.value,
            currency=row.currency,
            free_balance=row.free_balance,
            reserve_remaining=row.reserve_remaining,
            locked=row.locked,
            allocated_native=row.allocated_native,
        )


class BetTicketFxAssumption(BaseModel):
    currency: str
    gbp_per_unit: Decimal
    source: str
    source_date: str | None = None
    valuation_date: str | None = None
    check_status: str | None = None
    data_kind: Literal["modelled"] = "modelled"


class BetTicketSurvivability(BaseModel):
    available: bool = False
    survivability_score: int | None = None
    low_survivability_warning: bool | None = None
    volatility_regime: str | None = None
    estimate_not_guarantee: bool = True
    data_kind: Literal["modelled"] = "modelled"
    note: str = (
        "Survivability is an estimate, not a guarantee. Shown only when already "
        "present on the decision/allocation model."
    )


class RecommendPaperDeploymentRequest(BaseModel):
    opportunity_id: str
    operator_note: str = (
        "PAPER-ONLY recommended size from existing allocator constraints; does not OPEN or lock"
    )


class RecommendedPaperDeployment(BaseModel):
    """Authoritative recommended GBP size. Uses allocate(), never a new allocator."""

    opportunity_id: str
    accepted: bool
    bet_actionable: bool
    bet_blocked_reason: str | None = None
    recommended_size_gbp: Decimal = Decimal("0")
    maximum_validated_size_gbp: Decimal = Decimal("0")
    limiting_constraint: AllocationConstraintKind | None = None
    limiting_constraint_detail: str | None = None
    reduction_factors: list[str] = Field(default_factory=list)
    paper_only: bool = True
    places_orders: bool = False
    opens_trade: bool = False
    locks_treasury: bool = False
    execution_seam: BetTicketExecutionSeam = Field(default_factory=BetTicketExecutionSeam)
    data_kind: Literal["modelled"] = "modelled"
    operator_note: str = (
        "Recommended size is bounded by treasury, executable depth, fees/FX, "
        "risk and allocator policy. Preparation still required before OPEN."
    )
