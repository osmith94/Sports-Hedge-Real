from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from sports_hedge.accounting.dimensions import CapitalSource
from sports_hedge.arbitrage.allocation.models import AllocationConstraintKind, VenueNativeAmount
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.bet_ticket import (
    BetTicketExecutionSeam,
    BetTicketFxAssumption,
    BetTicketSurvivability,
    BetTicketTreasuryRemaining,
)

EXTERNAL_OPERATOR = "EXTERNAL_OPERATOR"


def preparation_capital_source(
    execution_mode: str,
    allocator_source: CapitalSource,
) -> CapitalSource:
    """Map allocator capital-source enums onto Phase 1 paper preview semantics.

    The allocator may tag EXTERNAL_OPERATOR legs as MANUAL_EXTERNAL. Preparation
    never confirms a live external action, so that enum must not be shown as
    operator MANUAL_EXTERNAL. Keep execution/fill mode on a separate field.
    """

    mode = execution_mode if isinstance(execution_mode, str) else str(execution_mode)
    if mode == EXTERNAL_OPERATOR and allocator_source is CapitalSource.MANUAL_EXTERNAL:
        return CapitalSource.PAPER_SIMULATED_EXTERNAL
    return allocator_source


class PreparedPaperLeg(BaseModel):
    """Exact pre-trade leg. Modelled; does not lock capital or open a trade."""

    venue: VenueName
    native_currency: str
    outcome: str
    source_market_id: str
    source_runner_id: str | None = None
    displayed_odds: Decimal | None = None
    stake_native: Decimal
    stake_reporting: Decimal
    capital_native: Decimal
    capital_reporting: Decimal
    venue_fee: Decimal | None = None
    net_payoff: Decimal | None = None
    fee_basis: str | None = None
    cost_status: str = "modelled"
    capital_source: CapitalSource
    execution_mode: str
    action: str | None = None
    displayed_depth_native: Decimal | None = None
    depth_consumed_pct: Decimal | None = None
    fx_gbp_per_unit: Decimal | None = None
    fx_source: str | None = None
    data_kind: Literal["modelled"] = "modelled"

    @model_validator(mode="after")
    def normalize(self) -> "PreparedPaperLeg":
        self.native_currency = self.native_currency.upper()
        return self


class PreparePaperDeploymentRequest(BaseModel):
    opportunity_id: str
    requested_size_gbp: Decimal = Field(gt=0)
    operator_note: str = "PAPER-ONLY fixed-size preparation; does not OPEN or lock"


class PreparedPaperDeployment(BaseModel):
    """Operator-visible Bet Ticket / fixed paper deployment. Preparation only — no OPEN."""

    prepared_deployment_id: str | None = None
    opportunity_id: str
    accepted: bool
    requested_size_gbp: Decimal
    operator_entered_size_gbp: Decimal | None = None
    recommended_size_gbp: Decimal = Decimal("0")
    applied_size_gbp: Decimal = Decimal("0")
    maximum_validated_size_gbp: Decimal = Decimal("0")
    resized: bool = False
    rejection_reason: str | None = None
    limiting_constraint: AllocationConstraintKind | None = None
    limiting_constraint_detail: str | None = None
    legs: list[PreparedPaperLeg] = Field(default_factory=list)
    capital_required: list[VenueNativeAmount] = Field(default_factory=list)
    native_requirements_reconciled: bool = False
    guaranteed_profit_gbp: Decimal = Decimal("0")
    guaranteed_roi: Decimal = Decimal("0")
    gross_edge: Decimal | None = None
    net_edge: Decimal | None = None
    market_label: str | None = None
    settlement_definition: str | None = None
    venue_pair: list[str] = Field(default_factory=list)
    quote_age_ms: int | None = None
    quote_age_basis: str | None = None
    execution_risk_score: int | None = None
    execution_risk_band: str | None = None
    execution_risk_reasons: list[str] = Field(default_factory=list)
    survivability: BetTicketSurvivability = Field(default_factory=BetTicketSurvivability)
    fx_assumptions: list[BetTicketFxAssumption] = Field(default_factory=list)
    treasury_remaining: list[BetTicketTreasuryRemaining] = Field(default_factory=list)
    solver_model: str | None = None
    settlement_equivalent: bool = False
    paper_only: bool = True
    places_orders: bool = False
    opens_trade: bool = False
    locks_treasury: bool = False
    execution_seam: BetTicketExecutionSeam = Field(default_factory=BetTicketExecutionSeam)
    data_kind: Literal["modelled"] = "modelled"
    operator_note: str = "PAPER-ONLY fixed-size preparation; does not OPEN or lock"


class PreparablePaperOpportunity(BaseModel):
    opportunity_id: str
    canonical_event_id: str | None = None
    canonical_market_id: str | None = None
    solver_model: str | None = None
    eligible_for_paper_simulation: bool = False
    settlement_equivalent: bool = False
    bet_actionable: bool = False
    bet_blocked_reason: str | None = None
    recommended_size_gbp: Decimal | None = None
    maximum_validated_size_gbp: Decimal | None = None
    market_label: str | None = None
    settlement_definition: str | None = None
