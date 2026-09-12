from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from sports_hedge.accounting.dimensions import CapitalSource
from sports_hedge.domain.models import VenueName


class VenueNativeAmount(BaseModel):
    """Native capital in one venue/currency. Never mix USD and GBP in this record."""

    venue: VenueName
    currency: str
    amount: Decimal = Field(ge=0)
    capital_source: CapitalSource = CapitalSource.AUTO_POOL

    @model_validator(mode="after")
    def normalize_currency(self) -> "VenueNativeAmount":
        self.currency = self.currency.upper()
        return self


class AllocationConstraintKind(StrEnum):
    EXECUTABLE_DEPTH = "executable_depth"
    NATIVE_VENUE_BALANCE = "native_venue_balance"
    VENUE_LIMIT = "venue_limit"
    PER_OPPORTUNITY_LIMIT = "per_opportunity_limit"
    MAX_POOL_FRACTION = "max_pool_fraction"
    FIXTURE_CONCENTRATION = "fixture_concentration"
    PORTFOLIO_CAP = "portfolio_cap"
    MIN_FREE_RESERVE = "min_free_reserve"
    EXTERNAL_LEG_CAP = "external_leg_cap"
    SOLVER_CAPITAL = "solver_capital"
    CONCURRENCY = "concurrency"
    MISSING_BALANCE_DATA = "missing_balance_data"
    CANNOT_RESIZE = "cannot_resize"
    SOLVER_NOT_ARBITRAGE = "solver_not_arbitrage"
    ZERO_STAKE_EXCLUDED = "zero_stake_excluded"


class EstimateConfidence(StrEnum):
    MODELLED = "modelled"
    PROVIDER_LIVE = "provider_live"
    UNKNOWN = "unknown"


class EstimatedTimeToRelease(BaseModel):
    """Advisory modelled hours until capital *might* become free.

    Never an authoritative settlement time. Never releases spendable cash.
    Actual cash changes only after a completed unwind (8E) or recorded settlement.
    """

    hours: Decimal = Field(gt=0)
    estimate_basis: str
    estimate_confidence: EstimateConfidence = EstimateConfidence.MODELLED
    label: str = "modelled_estimate_not_authoritative_settlement"
    does_not_release_capital: Literal[True] = True
    is_not_settlement: Literal[True] = True
    data_kind: Literal["modelled"] = "modelled"


class ReductionInputStatus(StrEnum):
    KNOWN = "known"
    UNKNOWN = "unknown"
    DEFAULT_POLICY = "default_policy"


class BankrollAllocationPolicy(BaseModel):
    """Conservative, operator-configurable reserve and recommendation policy."""

    min_reserve_amount: Decimal | None = Field(default=None, ge=0)
    min_reserve_fraction: Decimal = Field(default=Decimal("0.30"), ge=0, le=1)
    max_pool_fraction_per_opportunity: Decimal = Field(default=Decimal("0.25"), gt=0, le=1)
    max_open_capital_fraction: Decimal = Field(default=Decimal("0.70"), gt=0, le=1)
    max_same_fixture_capital_fraction: Decimal = Field(default=Decimal("0.40"), gt=0, le=1)
    max_concurrent_open_opportunities: int | None = Field(default=4, ge=1)
    per_opportunity_limit_reporting: Decimal | None = Field(default=None, gt=0)
    venue_limits_native: dict[VenueName, Decimal] = Field(default_factory=dict)
    portfolio_cap_reporting: Decimal | None = Field(default=None, gt=0)
    safety_haircut: Decimal = Field(default=Decimal("0.05"), ge=0, lt=1)
    external_leg_cap_native: Decimal | None = Field(default=None, gt=0)
    operator_recommended_cap_reporting: Decimal | None = Field(default=None, gt=0)
    risk_limit_reporting: Decimal | None = Field(default=None, gt=0)
    quote_age_reduction_start_ms: int = Field(default=2000, ge=0)
    quote_age_reduction_full_ms: int = Field(default=8000, ge=0)
    max_quote_age_reduction: Decimal = Field(default=Decimal("0.25"), ge=0, lt=1)
    execution_risk_reduction_start: int = Field(default=20, ge=0, le=100)
    execution_risk_reduction_full: int = Field(default=80, ge=0, le=100)
    max_execution_risk_reduction: Decimal = Field(default=Decimal("0.35"), ge=0, lt=1)
    fill_confidence_low_reduction: Decimal = Field(default=Decimal("0.30"), ge=0, lt=1)
    fill_confidence_medium_reduction: Decimal = Field(default=Decimal("0.10"), ge=0, lt=1)
    extra_level_reduction: Decimal = Field(default=Decimal("0.05"), ge=0, lt=1)
    max_levels_reduction: Decimal = Field(default=Decimal("0.20"), ge=0, lt=1)
    unknown_survivability_reduction: Decimal = Field(default=Decimal("0.10"), ge=0, lt=1)
    low_survivability_reduction: Decimal = Field(default=Decimal("0.25"), ge=0, lt=1)
    external_latency_reduction: Decimal = Field(default=Decimal("0.15"), ge=0, lt=1)
    scarcity_start_free_fraction: Decimal = Field(default=Decimal("0.50"), ge=0, le=1)
    max_scarcity_reduction: Decimal = Field(default=Decimal("0.30"), ge=0, lt=1)
    concurrency_reduction_per_open: Decimal = Field(default=Decimal("0.08"), ge=0, lt=1)
    max_concurrency_reduction: Decimal = Field(default=Decimal("0.40"), ge=0, lt=1)
    long_lock_hours: Decimal = Field(default=Decimal("24"), gt=0)
    max_lock_duration_reduction: Decimal = Field(default=Decimal("0.20"), ge=0, lt=1)
    unknown_volatility_reduction: Decimal = Field(default=Decimal("0.05"), ge=0, lt=1)
    elevated_volatility_bps: Decimal = Field(default=Decimal("40"), ge=0)
    elevated_volatility_reduction: Decimal = Field(default=Decimal("0.15"), ge=0, lt=1)
    football_regulation_playing_minutes: Decimal = Field(default=Decimal("90"), gt=0)
    football_halftime_minutes: Decimal = Field(default=Decimal("15"), ge=0)
    football_stoppage_and_settlement_buffer_minutes: Decimal = Field(
        default=Decimal("15"), ge=0
    )


class AllocationBalance(BaseModel):
    """Native venue/currency cash view. GBP carrying value is reporting-only."""

    venue: VenueName
    currency: str
    available: Decimal = Field(ge=0)
    locked: Decimal = Field(default=Decimal("0"), ge=0)
    transit: Decimal = Field(default=Decimal("0"), ge=0)
    conditionally_releasable: Decimal = Field(default=Decimal("0"), ge=0)
    gbp_per_unit: Decimal | None = Field(default=None, gt=0)

    @model_validator(mode="after")
    def normalize(self) -> "AllocationBalance":
        self.currency = self.currency.upper()
        if self.conditionally_releasable > self.locked:
            raise ValueError("conditionally_releasable cannot exceed locked capital")
        return self

    @property
    def pool_total(self) -> Decimal:
        return self.available + self.locked + self.transit

    @property
    def spendable(self) -> Decimal:
        """Immediately allocatable cash. Locked and unwindable-but-unfilled cash are excluded."""

        return self.available


class OpenPositionExposure(BaseModel):
    opportunity_id: str
    canonical_event_id: str | None = None
    capital_native: list[VenueNativeAmount] = Field(default_factory=list)
    capital_reporting: Decimal | None = Field(default=None, ge=0)


class AllocationLeg(BaseModel):
    """One solver-selected positive-stake leg. Zero-stake legs must be omitted."""

    leg_id: str
    outcome: str
    venue: VenueName
    source_market_id: str
    source_runner_id: str | None = None
    solver_stake: Decimal = Field(gt=0)
    max_stake: Decimal = Field(gt=0)
    capital_per_unit: Decimal = Field(default=Decimal("1"), gt=0)
    native_currency: str
    gbp_per_unit: Decimal = Field(gt=0)
    execution_mode: str = "INTERNAL"
    levels_consumed: int = Field(default=1, ge=1)
    quote_age_ms: int = Field(default=0, ge=0)
    quote_persistence: Decimal = Field(default=Decimal("1"), ge=0, le=1)
    assumed_latency_ms: int = Field(default=0, ge=0)
    payoff_per_unit: dict[str, Decimal] | None = None

    @model_validator(mode="after")
    def normalize(self) -> "AllocationLeg":
        self.native_currency = self.native_currency.upper()
        if self.solver_stake > self.max_stake:
            raise ValueError("solver_stake exceeds executable max_stake")
        return self

    @property
    def capital_reporting(self) -> Decimal:
        return self.solver_stake * self.capital_per_unit

    @property
    def capital_native(self) -> Decimal:
        return self.capital_reporting / self.gbp_per_unit


class AllocationRequest(BaseModel):
    solver_model: str
    is_arbitrage: bool
    canonical_event_id: str | None = None
    roi: Decimal = Field(ge=0)
    guaranteed_profit_at_solver_size: Decimal
    committed_capital_at_solver_size: Decimal = Field(ge=0)
    legs: list[AllocationLeg]
    state_pnl_at_solver_size: dict[str, Decimal] | None = None
    balances: list[AllocationBalance] = Field(default_factory=list)
    open_positions: list[OpenPositionExposure] = Field(default_factory=list)
    policy: BankrollAllocationPolicy = Field(default_factory=BankrollAllocationPolicy)
    execution_risk_score: int | None = Field(default=None, ge=0, le=100)
    fill_confidence: Any | None = None
    survivability: Any | None = None
    expected_lock_duration_hours: Decimal | None = Field(default=None, ge=0)
    expected_lock_basis: str | None = None
    estimated_time_to_release_hours: Decimal | None = Field(default=None, ge=0)
    estimate_basis: str | None = None
    estimate_confidence: EstimateConfidence | None = None
    quote_age_ms: int | None = Field(default=None, ge=0)
    external_confirmation_latency_seconds: Decimal | None = Field(default=None, ge=0)
    recent_volatility_bps: Decimal | None = Field(default=None, ge=0)
    reporting_currency: str = "GBP"
    require_internal_balances: bool = True

    @model_validator(mode="after")
    def reject_zero_stake_legs(self) -> "AllocationRequest":
        if any(leg.solver_stake <= 0 for leg in self.legs):
            raise ValueError("zero-stake legs must be excluded from allocation input")
        if self.estimated_time_to_release_hours is None:
            self.estimated_time_to_release_hours = self.expected_lock_duration_hours
        if self.estimate_basis is None:
            self.estimate_basis = self.expected_lock_basis
        return self


class ConstraintBinding(BaseModel):
    kind: AllocationConstraintKind
    scale: Decimal = Field(ge=0, le=1)
    detail: str
    venue: VenueName | None = None
    currency: str | None = None


class ReductionFactor(BaseModel):
    name: str
    amount: Decimal = Field(ge=0, lt=1)
    reason: str
    input_status: ReductionInputStatus = ReductionInputStatus.KNOWN


class AllocatedStake(BaseModel):
    leg_id: str
    outcome: str
    venue: VenueName
    source_market_id: str
    source_runner_id: str | None = None
    stake_reporting: Decimal
    stake_native: Decimal
    capital_reporting: Decimal
    capital_native: Decimal
    native_currency: str
    capital_source: CapitalSource
    execution_mode: str


class NativeBalanceAfter(BaseModel):
    venue: VenueName
    currency: str
    free_balance: Decimal
    reserve_required: Decimal
    reserve_remaining: Decimal
    pool_total: Decimal
    locked: Decimal
    transit: Decimal
    conditionally_releasable: Decimal
    allocated_native: Decimal

    @model_validator(mode="after")
    def normalize(self) -> "NativeBalanceAfter":
        self.currency = self.currency.upper()
        return self


class CapitalTurnoverMetric(BaseModel):
    """Advisory ranking input from estimated time-to-release. Not a return rate.

    Does not release capital or settle a position.
    """

    metric: Decimal
    formula: str = "guaranteed_profit / committed_capital / estimated_time_to_release_hours"
    estimated_time_to_release_hours: Decimal
    estimate_basis: str
    estimate_confidence: EstimateConfidence = EstimateConfidence.MODELLED
    expected_lock_duration_hours: Decimal | None = None
    expected_lock_basis: str | None = None
    guaranteed_profit: Decimal
    committed_capital: Decimal
    label: str = "modelled_ranking_input_not_guaranteed_return_rate"
    estimate_not_guarantee: bool = True
    does_not_release_capital: Literal[True] = True


class AllocationResult(BaseModel):
    accepted: bool
    solver_model: str
    reporting_currency: str = "GBP"
    maximum_validated_capital: Decimal = Decimal("0")
    recommended_committed_capital: Decimal = Decimal("0")
    maximum_validated_size: Decimal = Decimal("0")
    recommended_size: Decimal = Decimal("0")
    maximum_limiting_stake: Decimal = Decimal("0")
    recommended_limiting_stake: Decimal = Decimal("0")
    recommended_stakes: list[AllocatedStake] = Field(default_factory=list)
    capital_required: list[VenueNativeAmount] = Field(default_factory=list)
    guaranteed_profit: Decimal = Decimal("0")
    guaranteed_roi: Decimal = Decimal("0")
    free_balance_after: list[NativeBalanceAfter] = Field(default_factory=list)
    reserve_remaining: list[NativeBalanceAfter] = Field(default_factory=list)
    limiting_constraint: AllocationConstraintKind | None = None
    limiting_constraint_detail: str | None = None
    hard_constraints: list[ConstraintBinding] = Field(default_factory=list)
    reduction_factors: list[ReductionFactor] = Field(default_factory=list)
    expected_lock_duration_hours: Decimal | None = None
    expected_lock_basis: str | None = None
    estimated_time_to_release: EstimatedTimeToRelease | None = None
    settled_at: datetime | None = None
    capital_turnover: CapitalTurnoverMetric | None = None
    fill_confidence: Any | None = None
    execution_risk_score: int | None = None
    survivability: Any | None = None
    rejection_reason: str | None = None
    scale_maximum: Decimal = Decimal("0")
    scale_recommended: Decimal = Decimal("0")
    paper_only: bool = True
    places_orders: bool = False
    data_kind: Literal["modelled"] = "modelled"

    @property
    def maximum_validated_size_capital(self) -> Decimal:
        return self.maximum_validated_capital
