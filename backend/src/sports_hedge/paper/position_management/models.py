"""Read-model for autonomous paper position management.

Recommendation/evaluation is operational evidence, not GL P&L. Capital is
spendable only after a validated unwind or authoritative settlement posts 8E.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from sports_hedge.application.quote_freshness import require_aware_instant
from sports_hedge.paper.unwind.models import (
    CapitalPressure,
    IncrementalCloseCapitalStatus,
    UnwindRecommendation,
)


class PositionManagementAutoAction(StrEnum):
    NONE = "none"
    ADVISORY_ONLY = "advisory_only"
    UNWIND_ATTEMPTED = "unwind_attempted"
    UNWIND_ABORTED = "unwind_aborted"
    UNWIND_COMPLETED = "unwind_completed"
    SKIPPED_AWAITING_MANUAL_EXTERNAL = "skipped_awaiting_manual_external"
    SKIPPED_MANUAL_EXTERNAL = "skipped_manual_external"


class CloseFeeByVenue(BaseModel):
    venue: str
    native_currency: str
    closing_fee_native: Decimal = Decimal("0")
    fee_snapshot_id: str | None = None
    deferred_profit_commission: bool = False


class PositionManagementSnapshot(BaseModel):
    """Latest honest HOLD / UNWIND ELIGIBLE / NOT SAFE evaluation for one trade."""

    trade_id: str
    recommendation: UnwindRecommendation
    decision_reason: str
    evaluated_at: datetime
    hold_pnl_gbp: Decimal | None = None
    validated_exit_pnl_gbp: Decimal | None = None
    unwind_cost_gbp: Decimal | None = None
    closing_fees: list[CloseFeeByVenue] = Field(default_factory=list)
    releasable_native: dict[str, Decimal] = Field(default_factory=dict)
    capital_pressure: CapitalPressure = CapitalPressure.ABUNDANT
    opportunity_cost_gbp: Decimal | None = None
    opportunity_cost_detail: str | None = None
    close_executable: bool = False
    close_execution_risk_score: int | None = None
    quote_age_ms: int | None = Field(default=None, ge=0)
    quote_age_basis: str | None = None
    incremental_close_capital_status: IncrementalCloseCapitalStatus = (
        IncrementalCloseCapitalStatus.UNKNOWN_NOT_MODELLED
    )
    auto_action: PositionManagementAutoAction = PositionManagementAutoAction.NONE
    auto_unwind_enabled: bool = False
    auto_close_allowed: bool = False
    paper_only: bool = True
    places_orders: bool = False
    spendable: bool = False
    data_kind: str = "modelled_paper_position_management"

    @model_validator(mode="after")
    def honesty(self) -> PositionManagementSnapshot:
        self.evaluated_at = require_aware_instant(self.evaluated_at, "evaluated_at")
        self.paper_only = True
        self.places_orders = False
        self.spendable = False
        if not self.close_executable:
            self.releasable_native = {}
        return self


class CompetingOpportunityInput(BaseModel):
    """Traceable competing allocator economics. Never invented from a clock."""

    opportunity_id: str
    expected_guaranteed_profit_gbp: Decimal = Field(ge=0)
    allocator_accepted: bool = False
    capital_constrained: bool = False
    required_native: dict[str, Decimal] = Field(default_factory=dict)
    detail: str | None = None


class PositionManagementCycleResult(BaseModel):
    trade_id: str
    snapshot: PositionManagementSnapshot
    mutated: bool = False
    aborted_reason: str | None = None
    paper_only: bool = True
    places_orders: bool = False
