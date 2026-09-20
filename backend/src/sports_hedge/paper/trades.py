"""Durable paper trade/position read models.

This is the Phase 1 paper trade book, not live execution and not a production GL.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from sports_hedge.accounting.dimensions import CapitalSource
from sports_hedge.accounting.paper_journal import DataProvenance, PaperJournalEntry
from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import MarketAction, VenueCostSnapshot
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.paper.risk_snapshot import PaperExecutionRiskSnapshot


class PaperTradeState(StrEnum):
    PENDING = "PENDING"
    PARTIAL = "PARTIAL"
    OPEN = "OPEN"
    AWAITING_MANUAL_EXTERNAL = "AWAITING_MANUAL_EXTERNAL"
    CLOSED = "CLOSED"


class PaperLegFillKind(StrEnum):
    """How a persisted paper leg was recorded.

    INTERNAL_SIMULATED — Sports Hedge paper simulator, internal venue path.
    PAPER_SIMULATED_EXTERNAL — paper-only stand-in for a future EXTERNAL_OPERATOR
    leg. Distinct from MANUAL_EXTERNAL, which is an operator-recorded confirmation.
    """

    INTERNAL_SIMULATED = "INTERNAL_SIMULATED"
    PAPER_SIMULATED_EXTERNAL = "PAPER_SIMULATED_EXTERNAL"
    MANUAL_EXTERNAL = "MANUAL_EXTERNAL"
    UNFILLED = "UNFILLED"


OPENING_TRANCHE_ID = "opening"
OPENING_TRANCHE_SEQUENCE = 0


class PaperTradeTrancheKind(StrEnum):
    OPENING = "opening"
    TOP_UP = "top_up"
    RECOVERY = "recovery"


class PaperActiveTradePhase(StrEnum):
    ACCUMULATING = "accumulating"
    MONITORING_CAP_REACHED = "monitoring_cap_reached"
    EXIT_MANAGEMENT = "exit_management"
    RECOVERING_PARTIAL_ENTRY = "recovering_partial_entry"


class PaperTradeTranche(BaseModel):
    """One complete-set paper tranche. Opening fill is never overwritten."""

    tranche_id: str
    sequence: int = Field(ge=0)
    kind: PaperTradeTrancheKind
    occurred_at: datetime
    capital_locked_gbp: Decimal = Field(ge=0)
    guaranteed_profit_gbp: Decimal | None = None
    fill_ids: list[str] = Field(default_factory=list)
    idempotency_key: str

    @model_validator(mode="after")
    def ensure_timezone(self) -> PaperTradeTranche:
        if self.occurred_at.tzinfo is None:
            self.occurred_at = self.occurred_at.replace(tzinfo=UTC)
        return self


class PaperTradeAuditEventType(StrEnum):
    TRADE_OPENED = "trade_opened"
    REPEAT_OBSERVATION_NO_TOP_UP = "repeat_observation_no_top_up"
    DEFERRED_TO_ACTIVE_TRADE = "deferred_to_active_trade"
    ACTIVE_TRADE_PROMOTED = "active_trade_promoted"
    TOP_UP_TRANCHE_RECORDED = "top_up_tranche_recorded"
    TOP_UP_INCOMPLETE_ABORTED = "top_up_incomplete_aborted"
    TOP_UP_PARTIAL_RECORDED = "top_up_partial_recorded"
    ENTRY_RECOVERY_RECORDED = "entry_recovery_recorded"
    TOP_UP_CAP_REACHED = "top_up_cap_reached"
    TOP_UP_BELOW_MIN_NET = "top_up_below_min_net"
    AWAITING_MANUAL_EXTERNAL = "awaiting_manual_external"
    PAPER_AUTOFILL = "paper_autofill"
    PAPER_ENTRY_REJECTED = "paper_entry_rejected"
    PAPER_SIMULATED_EXTERNAL_FILL = "paper_simulated_external_fill"
    MANUAL_EXTERNAL_CONFIRMED = "manual_external_confirmed"
    HEDGE_REVALIDATED = "hedge_revalidated"
    FILLS_RECORDED = "fills_recorded"
    ENTRY_RISK_RECORDED = "entry_risk_recorded"
    SETTLED = "settled"
    SETTLEMENT_IDEMPOTENT = "settlement_idempotent"
    SETTLEMENT_BLOCKED = "settlement_blocked"
    CLOSE_PLAN_EVALUATED = "close_plan_evaluated"
    CLOSE_FILLS_RECORDED = "close_fills_recorded"
    UNWIND_COMPLETED = "unwind_completed"
    UNWIND_IDEMPOTENT = "unwind_idempotent"
    UNWIND_RISK_RECORDED = "unwind_risk_recorded"
    POSITION_MANAGEMENT_CHANGED = "position_management_changed"
    UNWIND_ATTEMPTED = "unwind_attempted"
    UNWIND_ABORTED = "unwind_aborted"
    DEMO_STORE_REINITIALIZED = "demo_store_reinitialized"


PAPER_UNWIND_SOURCE = "paper_unwind"


def paper_unwind_source_id(trade_id: str) -> str:
    return f"unwind:{trade_id}"


def paper_close_fill_id(opening_fill_id: str) -> str:
    return f"close:{opening_fill_id}"


class PaperTradeLeg(BaseModel):
    venue: VenueName
    outcome: str
    currency: str
    requested_stake: Decimal = Field(ge=0)
    filled_stake: Decimal = Field(ge=0)
    displayed_odds: Decimal | None = Field(default=None, gt=1)
    filled_odds: Decimal | None = Field(default=None, gt=1)
    source_market_id: str
    source_event_id: str | None = None
    source_runner_id: str | None = None
    source_contract_id: str | None = None
    opening_action: MarketAction | None = None
    canonical_state: str | None = None
    settlement_fingerprint_key: str | None = None
    fill_id: str | None = None
    fill_kind: PaperLegFillKind = PaperLegFillKind.UNFILLED
    capital_source: CapitalSource = CapitalSource.AUTO_POOL
    execution_mode: str = "INTERNAL"
    tranche_id: str = OPENING_TRANCHE_ID

    @model_validator(mode="after")
    def normalize(self) -> PaperTradeLeg:
        self.currency = self.currency.upper()
        return self


class PaperCloseFill(BaseModel):
    """Persisted paper close fill. Separate from opening legs; never a venue order."""

    fill_id: str
    opening_fill_id: str
    venue: VenueName
    outcome: str
    native_currency: str
    close_action: MarketAction
    filled_close_quantity: Decimal = Field(ge=0)
    weighted_closing_price: Decimal | None = None
    proceeds_native: Decimal
    closing_fee_native: Decimal = Field(ge=0)
    native_close_pnl: Decimal
    gbp_close_pnl: Decimal
    fx_rate_gbp_per_unit: Decimal = Field(gt=0)
    lock_id: str
    fee_snapshot_id: str | None = None
    quote_age_ms: int | None = Field(default=None, ge=0)
    paper_only: bool = True

    @model_validator(mode="after")
    def normalize(self) -> PaperCloseFill:
        self.native_currency = self.native_currency.upper()
        self.paper_only = True
        return self


class PaperTradeAuditEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    occurred_at: datetime
    event_type: PaperTradeAuditEventType
    detail: str | None = None

    @model_validator(mode="after")
    def ensure_timezone(self) -> PaperTradeAuditEvent:
        if self.occurred_at.tzinfo is None:
            self.occurred_at = self.occurred_at.replace(tzinfo=UTC)
        return self


class PaperTrade(BaseModel):
    trade_id: str
    opportunity_id: str
    canonical_event_id: str | None = None
    canonical_market_id: str | None = None
    settlement_key: str | None = None
    solver_model: str | None = None
    market_family: MarketFamily | None = None
    period: FootballPeriod | None = None
    competition: str | None = None
    home_team: str | None = None
    away_team: str | None = None
    fixture_label: str | None = None
    market_label: str | None = None
    state: PaperTradeState
    opened_at: datetime
    last_updated_at: datetime
    settled_at: datetime | None = None
    guaranteed_profit_gbp_at_open: Decimal | None = None
    realised_pnl_gbp: Decimal | None = None
    capital_locked_native: dict[str, Decimal] = Field(default_factory=dict)
    capital_locked_gbp: Decimal | None = None
    settlement_outcome: str | None = None
    settlement_source: str | None = None
    settlement_source_id: str | None = None
    settlement_detail: str | None = None
    provenance: DataProvenance = DataProvenance.LIVE_PAPER
    paper_only: bool = True
    places_orders: bool = False
    legs: list[PaperTradeLeg] = Field(default_factory=list)
    fx_snapshots: list[FxRateSnapshot] = Field(default_factory=list)
    venue_costs: list[VenueCostSnapshot] = Field(default_factory=list)
    entry_risk: PaperExecutionRiskSnapshot | None = None
    close_risks: list[PaperExecutionRiskSnapshot] = Field(default_factory=list)
    close_fills: list[PaperCloseFill] = Field(default_factory=list)
    # Runtime type is PositionManagementSnapshot | None. Imported lazily to
    # avoid trades <-> unwind.models <-> position_management cycles.
    position_management: Any | None = None
    tranches: list[PaperTradeTranche] = Field(default_factory=list)
    active_trade_phase: PaperActiveTradePhase | None = None
    residual_exposure_gbp: Decimal | None = None
    unresolved_recovery: bool = False
    audit: list[PaperTradeAuditEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def ensure_timezone(self) -> PaperTrade:
        if self.opened_at.tzinfo is None:
            self.opened_at = self.opened_at.replace(tzinfo=UTC)
        if self.last_updated_at.tzinfo is None:
            self.last_updated_at = self.last_updated_at.replace(tzinfo=UTC)
        if self.settled_at is not None and self.settled_at.tzinfo is None:
            self.settled_at = self.settled_at.replace(tzinfo=UTC)
        return self

    @property
    def is_active(self) -> bool:
        return self.state is not PaperTradeState.CLOSED


class PaperTradeDetail(PaperTrade):
    journals: list[PaperJournalEntry] = Field(default_factory=list)


class PaperTradeBookSummary(BaseModel):
    """Headline metrics derived only from persisted paper trades."""

    data_kind: str = "persisted_paper_trades"
    paper_only: bool = True
    open_count: int = 0
    closed_count: int = 0
    awaiting_manual_external_count: int = 0
    capital_locked_native: dict[str, Decimal] = Field(default_factory=dict)
    capital_locked_gbp: Decimal | None = None
    realised_pnl_gbp: Decimal | None = None
    gbp_unavailable_reason: str | None = None


class PaperSettlementRequest(BaseModel):
    """Explicit labelled settlement. Never inferred from elapsed kickoff time."""

    winning_outcome: str = Field(min_length=1)
    source: str = Field(min_length=1)
    source_id: str = Field(min_length=1)
    settled_at: datetime | None = None
    detail: str | None = None
    provenance: DataProvenance = DataProvenance.FIXTURE_DEMO

    @model_validator(mode="after")
    def ensure_timezone(self) -> PaperSettlementRequest:
        if self.settled_at is not None and self.settled_at.tzinfo is None:
            self.settled_at = self.settled_at.replace(tzinfo=UTC)
        return self
