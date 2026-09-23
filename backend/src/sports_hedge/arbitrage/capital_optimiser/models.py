"""PAPER decision-support models for global capital allocation.

These objects describe a modelled portfolio recommendation. They never lock
treasury, place venue orders, or alter scan-cycle state.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from sports_hedge.arbitrage.allocation.models import (
    AllocatedStake,
    AllocationBalance,
    AllocationConstraintKind,
    AllocationRequest,
    BankrollAllocationPolicy,
    OpenPositionExposure,
    VenueNativeAmount,
)
from sports_hedge.domain.models import VenueName


class OpportunityExclusionReason(StrEnum):
    NOT_APPROVED_EQUIVALENT = "not_approved_equivalent"
    SOLVER_NOT_ARBITRAGE = "solver_not_arbitrage"
    BELOW_MIN_NET_ARB = "below_min_net_arb"
    MISSING_FX_SNAPSHOT = "missing_fx_snapshot"
    FX_RATE_MISMATCH = "fx_rate_mismatch"
    STANDALONE_REJECTED = "standalone_rejected"
    NON_POSITIVE_PROFIT = "non_positive_profit"


class AllocationStrategyName(StrEnum):
    GLOBAL_LINEAR_PROGRAM = "global_linear_program"
    GREEDY_SEQUENTIAL = "greedy_per_opportunity"
    INDEPENDENT_PER_OPPORTUNITY = "independent_per_opportunity"


class UnusedCapitalClass(StrEnum):
    HELD_AS_RESERVE = "held_as_reserve"
    NO_REMAINING_ELIGIBLE_USE = "no_remaining_eligible_use"
    BLOCKED_BY_OTHER_CONSTRAINT = "blocked_by_other_constraint"
    LOCKED_NOT_SPENDABLE = "locked_not_spendable"
    TRANSIT_NOT_SPENDABLE = "transit_not_spendable"
    FULLY_ALLOCATED = "fully_allocated"


class CapitalOptimiserOpportunity(BaseModel):
    """One already-solved, approved-equivalent PAPER opportunity.

    `request.guaranteed_profit_at_solver_size` must already be net of the
    venue fee model used by the scanner/solver. This module does not recompute
    fees or settlement equivalence.
    """

    opportunity_id: str
    approved_equivalent: bool = False
    fee_snapshot_ids: list[str] = Field(default_factory=list)
    request: AllocationRequest


class CapitalOptimiserRequest(BaseModel):
    """Inputs for a read-only global allocation run."""

    opportunities: list[CapitalOptimiserOpportunity]
    balances: list[AllocationBalance] = Field(default_factory=list)
    open_positions: list[OpenPositionExposure] = Field(default_factory=list)
    policy: BankrollAllocationPolicy = Field(default_factory=BankrollAllocationPolicy)
    min_net_arb: Decimal = Field(default=Decimal("0"), ge=0)
    approved_fx: dict[str, Decimal] = Field(default_factory=dict)
    fx_source: str = ""
    fx_as_of: datetime | None = None
    paper_only: Literal[True] = True

    @model_validator(mode="after")
    def normalize_fx(self) -> "CapitalOptimiserRequest":
        self.approved_fx = {key.upper(): value for key, value in self.approved_fx.items()}
        if "GBP" not in self.approved_fx:
            self.approved_fx["GBP"] = Decimal("1")
        return self


class ExcludedOpportunity(BaseModel):
    opportunity_id: str
    reason: OpportunityExclusionReason
    detail: str


class OptimisedOpportunity(BaseModel):
    opportunity_id: str
    canonical_event_id: str | None = None
    accepted: bool
    scale: Decimal = Field(ge=0)
    scale_maximum: Decimal = Field(default=Decimal("0"), ge=0)
    guaranteed_net_profit: Decimal = Decimal("0")
    committed_capital_reporting: Decimal = Decimal("0")
    roi: Decimal = Decimal("0")
    stakes: list[AllocatedStake] = Field(default_factory=list)
    capital_required: list[VenueNativeAmount] = Field(default_factory=list)


class BindingConstraint(BaseModel):
    kind: AllocationConstraintKind
    detail: str
    venue: VenueName | None = None
    currency: str | None = None
    lhs: Decimal
    rhs: Decimal
    slack: Decimal


class UnusedCapital(BaseModel):
    venue: VenueName
    currency: str
    available_before: Decimal
    allocated_native: Decimal
    unallocated_spendable: Decimal
    reserve_required: Decimal
    reserve_remaining: Decimal
    locked: Decimal
    transit: Decimal
    conditionally_releasable: Decimal
    classification: UnusedCapitalClass
    notes: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def normalize(self) -> "UnusedCapital":
        self.currency = self.currency.upper()
        return self


class FormulationAudit(BaseModel):
    """Reproducible statement of the optimisation problem actually solved."""

    formulation: Literal["linear_program"] = "linear_program"
    engine: str = "scipy.linprog.highs"
    objective: str = "maximize sum(guaranteed_net_profit_at_unit_scale * scale_i)"
    decision_variables: str = "continuous scale_i in [0, per_opportunity_hard_max]"
    discrete_steps: list[str] = Field(default_factory=list)
    mixed_integer: Literal[False] = False
    on_scan_critical_path: Literal[False] = False
    mutates_treasury: Literal[False] = False
    places_orders: Literal[False] = False
    paper_only: Literal[True] = True
    data_kind: Literal["modelled"] = "modelled"


class PortfolioPlan(BaseModel):
    strategy: AllocationStrategyName
    guaranteed_net_profit: Decimal = Decimal("0")
    committed_capital_reporting: Decimal = Decimal("0")
    selected_count: int = 0
    jointly_feasible: bool = True
    infeasibility_reasons: list[str] = Field(default_factory=list)
    opportunities: list[OptimisedOpportunity] = Field(default_factory=list)
    unused_capital: list[UnusedCapital] = Field(default_factory=list)
    binding_constraints: list[BindingConstraint] = Field(default_factory=list)
    solver_status: str = "solved"
    paper_only: Literal[True] = True
    places_orders: Literal[False] = False
    mutates_treasury: Literal[False] = False
    data_kind: Literal["modelled"] = "modelled"


class StrategyComparison(BaseModel):
    """Auditable comparison of global LP vs greedy/per-opportunity allocation."""

    global_guaranteed_net_profit: Decimal
    greedy_guaranteed_net_profit: Decimal
    independent_guaranteed_net_profit: Decimal
    independent_jointly_feasible: bool
    improvement_vs_greedy: Decimal
    note: str = (
        "Global LP maximises jointly feasible guaranteed net PAPER profit. "
        "Independent per-opportunity totals may over-allocate shared native pools."
    )


class CapitalOptimiserResult(BaseModel):
    paper_only: Literal[True] = True
    places_orders: Literal[False] = False
    mutates_treasury: Literal[False] = False
    on_scan_critical_path: Literal[False] = False
    execution_enabled: Literal[False] = False
    mode: Literal["paper"] = "paper"
    data_kind: Literal["modelled"] = "modelled"
    formulation: FormulationAudit = Field(default_factory=FormulationAudit)
    recommended: PortfolioPlan
    maximum_validated: PortfolioPlan
    greedy: PortfolioPlan
    independent: PortfolioPlan
    comparison: StrategyComparison
    excluded: list[ExcludedOpportunity] = Field(default_factory=list)
    fx_source: str = ""
    fx_as_of: datetime | None = None
    min_net_arb: Decimal = Decimal("0")
    note: str = (
        "PAPER MODE decision support only. Running the optimiser does not lock "
        "treasury, open trades, or place venue orders. Allocations are modelled."
    )
