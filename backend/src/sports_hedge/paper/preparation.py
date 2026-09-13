from __future__ import annotations

from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from sports_hedge.accounting.dimensions import CapitalSource
from sports_hedge.arbitrage.allocation.models import AllocationConstraintKind, VenueNativeAmount
from sports_hedge.domain.models import VenueName


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
    """Operator-visible fixed paper deployment. Preparation only — no OPEN."""

    opportunity_id: str
    accepted: bool
    requested_size_gbp: Decimal
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
    solver_model: str | None = None
    settlement_equivalent: bool = False
    paper_only: bool = True
    places_orders: bool = False
    opens_trade: bool = False
    locks_treasury: bool = False
    data_kind: Literal["modelled"] = "modelled"
    operator_note: str = "PAPER-ONLY fixed-size preparation; does not OPEN or lock"


class PreparablePaperOpportunity(BaseModel):
    opportunity_id: str
    canonical_market_id: str | None = None
    solver_model: str | None = None
    eligible_for_paper_simulation: bool = False
    settlement_equivalent: bool = False
