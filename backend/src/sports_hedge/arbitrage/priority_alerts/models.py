from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field, model_validator

from sports_hedge.accounting.dimensions import CapitalSource, StrategyBook
from sports_hedge.arbitrage.models import ArbitrageSolution, ArbitrageStake, ExecutableQuote
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import VenueCostSnapshot
from sports_hedge.fees.models import FeeSnapshot
from sports_hedge.paper.models import FxRateSnapshot


class PrioritySeverity(StrEnum):
    PRIORITY = "PRIORITY"
    HIGH_PRIORITY = "HIGH_PRIORITY"
    CRITICAL = "CRITICAL"


class FillConfidence(StrEnum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class LegExecutionMode(StrEnum):
    INTERNAL = "INTERNAL"
    EXTERNAL_OPERATOR = "EXTERNAL_OPERATOR"


class PriorityAlertState(StrEnum):
    OPEN = "OPEN"
    AWAITING_EXTERNAL_LEG_CONFIRMATION = "AWAITING_EXTERNAL_LEG_CONFIRMATION"
    EXTERNAL_LEG_CONFIRMED = "EXTERNAL_LEG_CONFIRMED"
    HEDGE_REVALIDATED = "HEDGE_REVALIDATED"
    HEDGE_REVALIDATION_FAILED = "HEDGE_REVALIDATION_FAILED"
    EXPIRED = "EXPIRED"


class OperatorAction(StrEnum):
    PREPARE_MANUAL_OVERRIDE = "PREPARE_MANUAL_OVERRIDE"
    PREPARE_PROCEED_WITH_EXTERNAL_COUNTERPARTY = "PREPARE_PROCEED_WITH_EXTERNAL_COUNTERPARTY"


class PriorityAlertEventType(StrEnum):
    PRIORITY_ALERT_OPENED = "PRIORITY_ALERT_OPENED"
    PRIORITY_ALERT_UPGRADED = "PRIORITY_ALERT_UPGRADED"
    PRIORITY_ALERT_DOWNGRADED = "PRIORITY_ALERT_DOWNGRADED"
    PRIORITY_ALERT_EXPIRED = "PRIORITY_ALERT_EXPIRED"
    MANUAL_OVERRIDE_PREPARED = "MANUAL_OVERRIDE_PREPARED"
    MANUAL_OVERRIDE_CANCELLED = "MANUAL_OVERRIDE_CANCELLED"
    EXTERNAL_COUNTERPARTY_PREPARED = "EXTERNAL_COUNTERPARTY_PREPARED"
    EXTERNAL_LEG_CONFIRMATION_RECORDED = "EXTERNAL_LEG_CONFIRMATION_RECORDED"
    EXTERNAL_HEDGE_REVALIDATED = "EXTERNAL_HEDGE_REVALIDATED"
    EXTERNAL_HEDGE_REVALIDATION_FAILED = "EXTERNAL_HEDGE_REVALIDATION_FAILED"
    EXTERNAL_COUNTERPARTY_CANCELLED = "EXTERNAL_COUNTERPARTY_CANCELLED"


class NotificationChannel(StrEnum):
    IN_APP = "IN_APP"
    EMAIL = "EMAIL"
    PUSH = "PUSH"
    SMS = "SMS"


SEVERITY_RANK = {
    PrioritySeverity.PRIORITY: 1,
    PrioritySeverity.HIGH_PRIORITY: 2,
    PrioritySeverity.CRITICAL: 3,
}

FILL_CONFIDENCE_RANK = {
    FillConfidence.LOW: 1,
    FillConfidence.MEDIUM: 2,
    FillConfidence.HIGH: 3,
}


class VenueCurrencyAmount(BaseModel):
    """Native capital in one venue/currency. Never mix USD and GBP in this record."""

    venue: VenueName
    currency: str
    amount: Decimal = Field(ge=0)
    capital_source: CapitalSource = CapitalSource.MANUAL_OVERRIDE

    @model_validator(mode="after")
    def normalize_currency(self) -> "VenueCurrencyAmount":
        self.currency = self.currency.upper()
        return self


class AutomatedPoolBalance(BaseModel):
    venue: VenueName
    currency: str
    amount: Decimal = Field(ge=0)
    capital_source: CapitalSource = CapitalSource.AUTO_POOL

    @model_validator(mode="after")
    def normalize_currency(self) -> "AutomatedPoolBalance":
        self.currency = self.currency.upper()
        return self


class PriorityLeg(BaseModel):
    """One already-normalized executable leg presented to the escalation layer."""

    outcome: str
    venue: VenueName
    source_market_id: str
    source_runner_id: str | None = None
    net_decimal_odds: Decimal = Field(gt=Decimal("1"))
    max_stake_reporting: Decimal = Field(gt=Decimal("0"))
    native_currency: str
    native_max_stake: Decimal = Field(gt=Decimal("0"))
    gbp_per_unit: Decimal = Field(gt=Decimal("0"))
    levels_consumed: int = Field(default=1, ge=1)
    quote_age_ms: int = Field(default=0, ge=0)
    quote_persistence: Decimal = Field(default=Decimal("1"), ge=0, le=1)
    venue_cancellation_rate: Decimal | None = Field(default=None, ge=0, le=1)
    assumed_latency_ms: int = Field(default=0, ge=0)
    historical_paper_fill_rate: Decimal | None = Field(default=None, ge=0, le=1)
    execution_mode: LegExecutionMode = LegExecutionMode.INTERNAL

    @model_validator(mode="after")
    def normalize_currency(self) -> "PriorityLeg":
        self.native_currency = self.native_currency.upper()
        return self

    def as_executable_quote(self) -> ExecutableQuote:
        return ExecutableQuote(
            outcome=self.outcome,
            venue=self.venue,
            source_market_id=self.source_market_id,
            net_decimal_odds=self.net_decimal_odds,
            max_stake=self.max_stake_reporting,
        )


class FillConfidenceInputs(BaseModel):
    depth_coverage_ratio: Decimal = Field(ge=0)
    quote_age_ms: int = Field(ge=0)
    quote_persistence: Decimal = Field(default=Decimal("1"), ge=0, le=1)
    levels_consumed: int = Field(ge=1)
    assumed_latency_ms: int = Field(default=0, ge=0)
    venue_cancellation_rate: Decimal | None = Field(default=None, ge=0, le=1)
    historical_paper_fill_rate: Decimal | None = Field(default=None, ge=0, le=1)


class FillConfidenceBreakdown(BaseModel):
    band: FillConfidence
    score: int = Field(ge=0, le=100)
    inputs: FillConfidenceInputs
    reasons: list[str] = Field(default_factory=list)


class VolatilityRegime(StrEnum):
    CALM = "CALM"
    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    TURBULENT = "TURBULENT"
    UNKNOWN = "UNKNOWN"


class SurvivabilityConfidence(StrEnum):
    """Evidence / sample-quality indicator. UNKNOWN until a scorer supplies it."""

    UNKNOWN = "UNKNOWN"
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class SurvivalHorizon(BaseModel):
    """P(opportunity still economically valid) at a named horizon. Estimate only."""

    horizon_seconds: int = Field(ge=0)
    survival_probability: Decimal | None = Field(default=None, ge=0, le=1)


class SurvivabilityComponent(BaseModel):
    """Inspectable driver so UI can explain high/medium/low survivability."""

    name: str
    assessment: str | None = None
    value: Decimal | None = None
    reason: str | None = None


class OpportunitySurvivability(BaseModel):
    """Optional plug-in seam for a later historical survivability scorer.

    Distinct from current arb edge and fill confidence. Missing values mean
    the scorer has not run. This layer must not invent probabilities.
    """

    estimate_not_guarantee: bool = True
    data_insufficient: bool = True
    survivability_score: int | None = Field(default=None, ge=0, le=100)
    survival_probability_at_required_latency: Decimal | None = Field(default=None, ge=0, le=1)
    required_action_latency_seconds: Decimal | None = Field(default=None, ge=0)
    survival_probability_at_action_latency: Decimal | None = Field(default=None, ge=0, le=1)
    expected_action_latency_seconds: Decimal | None = Field(default=None, ge=0)
    expected_external_confirmation_latency_seconds: Decimal | None = Field(default=None, ge=0)
    survival_probability_5s: Decimal | None = Field(default=None, ge=0, le=1)
    survival_probability_15s: Decimal | None = Field(default=None, ge=0, le=1)
    survival_probability_30s: Decimal | None = Field(default=None, ge=0, le=1)
    survival_probability_60s: Decimal | None = Field(default=None, ge=0, le=1)
    horizons: list[SurvivalHorizon] = Field(default_factory=list)
    estimated_median_remaining_life_seconds: Decimal | None = Field(default=None, ge=0)
    historical_dislocation_half_life_seconds: Decimal | None = Field(default=None, ge=0)
    volatility_regime: VolatilityRegime = VolatilityRegime.UNKNOWN
    recent_volatility: Decimal | None = Field(default=None, ge=0)
    adverse_move_rate: Decimal | None = None
    survivability_confidence: SurvivabilityConfidence = SurvivabilityConfidence.UNKNOWN
    reasons: list[str] = Field(default_factory=list)
    components: list[SurvivabilityComponent] = Field(default_factory=list)
    low_survivability_warning: bool = False

    @model_validator(mode="after")
    def align_latency_aliases(self) -> "OpportunitySurvivability":
        probability = self.survival_probability_at_required_latency
        if probability is None:
            probability = self.survival_probability_at_action_latency
        latency = self.required_action_latency_seconds
        if latency is None:
            latency = self.expected_action_latency_seconds
        object.__setattr__(self, "survival_probability_at_required_latency", probability)
        object.__setattr__(self, "survival_probability_at_action_latency", probability)
        object.__setattr__(self, "required_action_latency_seconds", latency)
        object.__setattr__(self, "expected_action_latency_seconds", latency)
        return self

    @computed_field
    @property
    def survivability_reasons(self) -> list[str]:
        return list(self.reasons)

    def probability_for_horizon(self, seconds: int | Decimal) -> Decimal | None:
        target = int(seconds)
        for horizon in self.horizons:
            if horizon.horizon_seconds == target:
                return horizon.survival_probability
        named = {
            5: self.survival_probability_5s,
            15: self.survival_probability_15s,
            30: self.survival_probability_30s,
            60: self.survival_probability_60s,
        }
        return named.get(target)


class RecommendedManualSize(BaseModel):
    raw_limiting_depth: Decimal
    safety_haircut: Decimal
    recommended_size: Decimal
    maximum_validated_size: Decimal
    limiting_leg_outcome: str
    limiting_leg_venue: VenueName
    total_stake_reporting: Decimal
    guaranteed_payoff: Decimal
    guaranteed_profit: Decimal
    guaranteed_roi: Decimal
    capital_efficiency: Decimal
    stake_plan: list[ArbitrageStake]
    capital_required: list[VenueCurrencyAmount]
    additional_capital_required: list[VenueCurrencyAmount]
    quote_age_ms: int
    fill_confidence: FillConfidenceBreakdown
    execution_risk_score: int
    reporting_currency: str = "GBP"
    auto_pool_draw: list[VenueCurrencyAmount] = Field(default_factory=list)
    has_external_leg: bool = False
    survivability: OpportunitySurvivability | None = None
    limiting_constraint: str | None = None
    limiting_constraint_detail: str | None = None
    recommended_committed_capital: Decimal | None = None
    maximum_validated_capital: Decimal | None = None
    reduction_factors: list[dict[str, Any]] = Field(default_factory=list)
    free_balance_after: list[dict[str, Any]] = Field(default_factory=list)
    reserve_remaining: list[dict[str, Any]] = Field(default_factory=list)
    expected_lock_duration_hours: Decimal | None = None
    expected_lock_basis: str | None = None
    estimated_time_to_release: dict[str, Any] | None = None
    settled_at: datetime | None = None
    capital_turnover: dict[str, Any] | None = None
    capital_turnover_label: str = "modelled_ranking_input_not_guaranteed_return_rate"


class PriorityAlertEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    event_type: PriorityAlertEventType
    occurred_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    alert_id: str
    opportunity_id: str
    severity: PrioritySeverity | None = None
    detail: str | None = None
    channels: list[NotificationChannel] = Field(
        default_factory=lambda: [NotificationChannel.IN_APP]
    )

    @model_validator(mode="after")
    def ensure_timezone(self) -> "PriorityAlertEvent":
        if self.occurred_at.tzinfo is None:
            self.occurred_at = self.occurred_at.replace(tzinfo=UTC)
        return self


class PriorityAlert(BaseModel):
    alert_id: str = Field(default_factory=lambda: str(uuid4()))
    opportunity_id: str
    canonical_event_id: str | None = None
    canonical_market_id: str | None = None
    opened_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    expired_at: datetime | None = None
    severity: PrioritySeverity
    strategy_book: StrategyBook = StrategyBook.ARBITRAGE
    capital_source: CapitalSource = CapitalSource.MANUAL_OVERRIDE
    lifecycle_state: PriorityAlertState = PriorityAlertState.OPEN
    operator_action: OperatorAction = OperatorAction.PREPARE_MANUAL_OVERRIDE
    net_guaranteed_edge: Decimal
    recommendation: RecommendedManualSize
    settlement_equivalent: bool = True
    ordinary_arb_confirmed: bool = True
    paper_mode: bool = True
    commits_automated_legs: bool = False
    places_orders: bool = False
    eligibility_confirmed: bool = False
    rejection_reasons: list[str] = Field(default_factory=list)
    history: list[PriorityAlertEvent] = Field(default_factory=list)
    prepared_override: ManualOverrideRecommendation | None = None
    prepared_external: ExternalCounterpartyPlan | None = None
    external_confirmation: ExternalLegConfirmation | None = None
    hedge_revalidation: ExternalHedgeRevalidation | None = None
    survivability: OpportunitySurvivability | None = None

    @model_validator(mode="after")
    def ensure_timezone(self) -> "PriorityAlert":
        if self.opened_at.tzinfo is None:
            self.opened_at = self.opened_at.replace(tzinfo=UTC)
        if self.updated_at.tzinfo is None:
            self.updated_at = self.updated_at.replace(tzinfo=UTC)
        if self.expired_at is not None and self.expired_at.tzinfo is None:
            self.expired_at = self.expired_at.replace(tzinfo=UTC)
        return self

    @property
    def is_open(self) -> bool:
        return self.expired_at is None


class ManualOverrideRecommendation(BaseModel):
    ticket_id: str = Field(default_factory=lambda: str(uuid4()))
    alert_id: str
    opportunity_id: str
    strategy_book: StrategyBook = StrategyBook.ARBITRAGE
    capital_source: CapitalSource = CapitalSource.MANUAL_OVERRIDE
    requested_size: Decimal
    applied_size: Decimal
    accepted: bool
    capped: bool = False
    rejection_reason: str | None = None
    stake_plan: list[ArbitrageStake] = Field(default_factory=list)
    capital_required: list[VenueCurrencyAmount] = Field(default_factory=list)
    additional_capital_required: list[VenueCurrencyAmount] = Field(default_factory=list)
    guaranteed_payoff: Decimal = Decimal("0")
    guaranteed_profit: Decimal = Decimal("0")
    guaranteed_roi: Decimal = Decimal("0")
    limiting_leg_outcome: str | None = None
    quote_age_ms: int = 0
    fill_confidence: FillConfidenceBreakdown | None = None
    paper_mode: bool = True
    places_orders: bool = False
    commits_automated_legs: bool = False


class ExternalLegConfirmation(BaseModel):
    """Operator-recorded fill on a leg Sports Hedge cannot execute itself."""

    outcome: str
    venue: VenueName
    product_id: str
    operator_counterparty_reference: str
    executed_price: Decimal = Field(gt=Decimal("1"))
    executed_size: Decimal = Field(gt=Decimal("0"))
    currency: str
    executed_at: datetime
    eligibility_confirmed: bool
    external_reference: str | None = None
    evidence: str | None = None

    @model_validator(mode="after")
    def normalize(self) -> "ExternalLegConfirmation":
        self.currency = self.currency.upper()
        if self.executed_at.tzinfo is None:
            self.executed_at = self.executed_at.replace(tzinfo=UTC)
        return self


class ExternalCounterpartyPlan(BaseModel):
    plan_id: str = Field(default_factory=lambda: str(uuid4()))
    alert_id: str
    opportunity_id: str
    strategy_book: StrategyBook = StrategyBook.ARBITRAGE
    capital_source: CapitalSource = CapitalSource.MANUAL_EXTERNAL
    operator_action: OperatorAction = OperatorAction.PREPARE_PROCEED_WITH_EXTERNAL_COUNTERPARTY
    lifecycle_state: PriorityAlertState = (
        PriorityAlertState.AWAITING_EXTERNAL_LEG_CONFIRMATION
    )
    requested_size: Decimal
    applied_size: Decimal
    accepted: bool
    capped: bool = False
    rejection_reason: str | None = None
    external_legs: list[PriorityLeg] = Field(default_factory=list)
    stake_plan: list[ArbitrageStake] = Field(default_factory=list)
    capital_required: list[VenueCurrencyAmount] = Field(default_factory=list)
    additional_capital_required: list[VenueCurrencyAmount] = Field(default_factory=list)
    auto_pool_draw: list[VenueCurrencyAmount] = Field(default_factory=list)
    guaranteed_payoff: Decimal = Decimal("0")
    guaranteed_profit: Decimal = Decimal("0")
    guaranteed_roi: Decimal = Decimal("0")
    paper_mode: bool = True
    places_orders: bool = False
    commits_automated_legs: bool = False
    survivability: OpportunitySurvivability | None = None


class ExternalHedgeRevalidation(BaseModel):
    accepted: bool
    reasons: list[str] = Field(default_factory=list)
    lifecycle_state: PriorityAlertState
    confirmation: ExternalLegConfirmation
    net_guaranteed_edge: Decimal | None = None
    guaranteed_profit: Decimal | None = None
    capital_required: list[VenueCurrencyAmount] = Field(default_factory=list)
    stake_plan: list[ArbitrageStake] = Field(default_factory=list)
    fixed_external_stake: Decimal | None = None
    paper_mode: bool = True
    places_orders: bool = False
    commits_automated_legs: bool = False


class PriorityAlertCandidate(BaseModel):
    """Input to the escalation layer after ordinary strict arb checks."""

    opportunity_id: str
    canonical_event_id: str | None = None
    canonical_market_id: str | None = None
    settlement_equivalent: bool
    ordinary_solution: ArbitrageSolution
    legs: list[PriorityLeg]
    fee_snapshots: list[FeeSnapshot] = Field(default_factory=list)
    venue_costs: list[VenueCostSnapshot] = Field(default_factory=list)
    fx_snapshots: list[FxRateSnapshot] = Field(default_factory=list)
    automated_pools: list[AutomatedPoolBalance] = Field(default_factory=list)
    execution_risk_score: int = Field(default=0, ge=0, le=100)
    eligibility_confirmed: bool = False
    survivability: OpportunitySurvivability | None = None
    observed_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def ensure_timezone(self) -> "PriorityAlertCandidate":
        if self.observed_at.tzinfo is None:
            self.observed_at = self.observed_at.replace(tzinfo=UTC)
        return self

    def has_external_leg(self) -> bool:
        return any(leg.execution_mode == LegExecutionMode.EXTERNAL_OPERATOR for leg in self.legs)


class QualificationResult(BaseModel):
    qualifies: bool
    reasons: list[str] = Field(default_factory=list)
    severity: PrioritySeverity | None = None
    fill_confidence: FillConfidenceBreakdown | None = None
    recommendation: RecommendedManualSize | None = None
    ordinary_solution: ArbitrageSolution | None = None


PriorityAlert.model_rebuild()

