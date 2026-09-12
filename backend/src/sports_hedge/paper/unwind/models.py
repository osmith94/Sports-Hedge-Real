"""Paper-only unwind / mark-to-market models.

Conditionally releasable capital is analytical. This module does not post
treasury journals or mutate standing liquidity pools.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from sports_hedge.application.quote_freshness import require_aware_instant
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import MarketAction, VenueCostSnapshot
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.paper.trades import PaperLegFillKind
from sports_hedge.risk.execution import ExecutionRiskResult


class UnwindRecommendation(StrEnum):
    HOLD = "HOLD"
    UNWIND_ELIGIBLE = "UNWIND_ELIGIBLE"
    UNWIND_NOT_SAFE = "UNWIND_NOT_SAFE"


class VenueCloseMechanics(StrEnum):
    """Provider-neutral close mechanics. Named by economics, not a venue pair."""

    EXCHANGE_BACK_LAY = "exchange_back_lay"
    PREDICTION_BINARY_BUY_SELL = "prediction_binary_buy_sell"
    BOOKMAKER_BACK_ONLY = "bookmaker_back_only"
    UNSUPPORTED = "unsupported"


class CapitalPressure(StrEnum):
    ABUNDANT = "abundant"
    SCARCE = "scarce"


class UnwindPolicy(BaseModel):
    """Deterministic conservative close rule. Headline spread is never enough."""

    max_quote_age_ms: int = Field(default=2000, ge=0)
    max_execution_risk: int = Field(default=60, ge=0, le=100)
    max_profit_give_up_gbp: Decimal = Field(default=Decimal("0"), ge=0)
    max_profit_give_up_gbp_when_scarce: Decimal = Field(default=Decimal("2"), ge=0)
    max_profit_give_up_ratio_when_scarce: Decimal = Field(default=Decimal("0.10"), ge=0, le=1)
    min_retained_exit_pnl_gbp: Decimal | None = Field(default=None)
    allow_partial_close: bool = False
    require_known_fx: bool = True
    require_known_exit_fees: bool = True

    @model_validator(mode="after")
    def reject_partial_policy(self) -> UnwindPolicy:
        if self.allow_partial_close:
            raise ValueError("partial closes are not a validated 8D unwind")
        return self


class CapitalScarcityInput(BaseModel):
    """Allocator-owned input. 8D does not decide pool sizes or spend capital."""

    pressure: CapitalPressure = CapitalPressure.ABUNDANT
    detail: str | None = None
    opportunity_cost_gbp: Decimal | None = Field(default=None, ge=0)


class OpenPaperLeg(BaseModel):
    """Identity required to find the economically correct reverse transaction."""

    venue: VenueName
    source_event_id: str
    source_market_id: str
    source_runner_id: str
    source_contract_id: str | None = None
    canonical_market_id: str
    canonical_outcome: str
    canonical_state: str | None = None
    opening_action: MarketAction
    filled_price: Decimal = Field(gt=1)
    filled_size: Decimal = Field(gt=0)
    native_currency: str
    fee_snapshot_id: str | None = None
    settlement_fingerprint_key: str
    fill_kind: PaperLegFillKind = PaperLegFillKind.INTERNAL_SIMULATED
    fill_id: str | None = None

    @model_validator(mode="after")
    def require_identity(self) -> OpenPaperLeg:
        self.native_currency = self.native_currency.upper()
        for field in (
            self.source_event_id,
            self.source_market_id,
            self.source_runner_id,
            self.canonical_market_id,
            self.canonical_outcome,
            self.settlement_fingerprint_key,
        ):
            if not str(field).strip():
                raise ValueError("unknown_or_stale_leg_identity")
        if self.opening_action not in {MarketAction.BACK, MarketAction.BUY}:
            raise ValueError("opening_lay_not_supported")
        return self


class OpenPaperPosition(BaseModel):
    """Provider-neutral open paper trade evaluated against fresh observations."""

    trade_id: str
    opportunity_id: str
    canonical_event_id: str
    canonical_market_id: str
    settlement_fingerprint_key: str
    solver_model: str
    hold_pnl_gbp: Decimal
    capital_locked_native: dict[str, Decimal] = Field(default_factory=dict)
    expected_settlement_at: datetime | None = None
    remaining_lock_minutes: Decimal | None = Field(default=None, ge=0)
    legs: list[OpenPaperLeg] = Field(min_length=1)
    paper_only: bool = True
    places_orders: bool = False

    @model_validator(mode="after")
    def validate_position(self) -> OpenPaperPosition:
        if not self.settlement_fingerprint_key.strip():
            raise ValueError("unknown_or_stale_settlement_identity")
        if not self.canonical_event_id.strip() or not self.canonical_market_id.strip():
            raise ValueError("unknown_or_stale_canonical_identity")
        if self.places_orders:
            raise ValueError("Phase 1 unwind evaluation cannot place orders")
        keys = {leg.settlement_fingerprint_key for leg in self.legs}
        if keys != {self.settlement_fingerprint_key}:
            raise ValueError("settlement_identity_mismatch")
        return self


class ReverseQuote(BaseModel):
    """Fresh reverse-side book for one already-open leg."""

    venue: VenueName
    source_event_id: str
    source_market_id: str
    source_runner_id: str
    canonical_outcome: str
    settlement_fingerprint_key: str
    native_currency: str
    levels: list[BookLevel] = Field(default_factory=list)
    quote_age_ms: int | None = Field(default=None, ge=0)
    quote_age_basis: str | None = None
    quoted_at: datetime
    closing_cost: VenueCostSnapshot
    fill_confidence: Decimal | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def normalize(self) -> ReverseQuote:
        self.native_currency = self.native_currency.upper()
        self.quoted_at = require_aware_instant(self.quoted_at, "quoted_at")
        if not self.settlement_fingerprint_key.strip():
            raise ValueError("unknown_or_stale_settlement_identity")
        return self


class CloseLegPlan(BaseModel):
    venue: VenueName
    canonical_outcome: str
    opening_action: MarketAction
    close_action: MarketAction
    mechanics: VenueCloseMechanics
    required_close_quantity: Decimal
    filled_close_quantity: Decimal
    available_closing_capacity: Decimal
    levels_consumed: int = Field(default=0, ge=0)
    weighted_closing_price: Decimal | None = None
    worst_closing_price: Decimal | None = None
    slippage_vs_top: Decimal | None = None
    matched_stake: Decimal = Decimal("0")
    liability: Decimal = Decimal("0")
    proceeds: Decimal = Decimal("0")
    closing_fee: Decimal = Decimal("0")
    native_close_pnl: Decimal = Decimal("0")
    gbp_close_pnl: Decimal = Decimal("0")
    native_currency: str
    quote_age_ms: int | None = None
    quote_age_basis: str | None = None
    fill_confidence: Decimal | None = None
    executable: bool
    rejection_reason: str | None = None
    fee_snapshot_id: str | None = None
    deferred_profit_commission: bool = False
    paper_only: bool = True


class ClosePlan(BaseModel):
    evaluated_at: datetime
    fully_executable: bool
    legs: list[CloseLegPlan] = Field(default_factory=list)
    rejection_reasons: list[str] = Field(default_factory=list)
    paper_only: bool = True
    places_orders: bool = False

    @model_validator(mode="after")
    def ensure_timezone(self) -> ClosePlan:
        self.evaluated_at = require_aware_instant(self.evaluated_at, "evaluated_at")
        return self


class UnwindDecision(BaseModel):
    """Hold-vs-unwind economics. Conditionally releasable is not spendable."""

    trade_id: str
    recommendation: UnwindRecommendation
    decision_reason: str
    hold_pnl_gbp: Decimal | None = None
    validated_exit_pnl_gbp: Decimal | None = None
    profit_give_up_gbp: Decimal | None = None
    remaining_lock_minutes: Decimal | None = None
    capital_turnover_hint: str | None = None
    conditionally_releasable_by_venue_currency: dict[str, Decimal] = Field(default_factory=dict)
    additional_close_capital_native: dict[str, Decimal] = Field(default_factory=dict)
    close_plan: ClosePlan
    execution_risk: ExecutionRiskResult | None = None
    capital_pressure: CapitalPressure = CapitalPressure.ABUNDANT
    paper_only: bool = True
    places_orders: bool = False
    spendable: bool = False
    data_kind: str = "modelled_paper_unwind"

    @model_validator(mode="after")
    def never_spendable(self) -> UnwindDecision:
        self.spendable = False
        self.places_orders = False
        self.paper_only = True
        if not self.close_plan.fully_executable:
            self.conditionally_releasable_by_venue_currency = {}
        return self


class PaperClosePlanRequest(BaseModel):
    """PAPER-ONLY analytical close plan. Never places a venue order or posts treasury."""

    quotes: list[ReverseQuote] = Field(min_length=1)
    fx: list[FxRateSnapshot] | None = None
    policy: UnwindPolicy = Field(default_factory=UnwindPolicy)
    scarcity: CapitalScarcityInput = Field(default_factory=CapitalScarcityInput)
    paper_only: bool = True
    places_orders: bool = False

    @model_validator(mode="after")
    def reject_live_execution(self) -> PaperClosePlanRequest:
        if self.places_orders:
            raise ValueError("close-plan evaluation cannot place orders")
        self.paper_only = True
        return self


class UnwindEvaluationRequest(BaseModel):
    position: OpenPaperPosition
    quotes: list[ReverseQuote] = Field(min_length=1)
    fx: list[FxRateSnapshot] = Field(default_factory=list)
    policy: UnwindPolicy = Field(default_factory=UnwindPolicy)
    scarcity: CapitalScarcityInput = Field(default_factory=CapitalScarcityInput)
    evaluated_at: datetime | None = None
    assumed_latency_ms: int = Field(default=500, ge=0)
    recent_volatility_bps: float = Field(default=0, ge=0)
