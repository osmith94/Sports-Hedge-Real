from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from sports_hedge.accounting.dimensions import CapitalSource
from sports_hedge.accounting.paper_journal import DataProvenance, PaperJournalEntry
from sports_hedge.arbitrage.priority_alerts.models import (
    ExternalLegConfirmation,
    LegExecutionMode,
    PriorityAlert,
)
from sports_hedge.arbitrage.watchlist.models import NearOpportunity, OpportunityStatus
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import VenueCostSnapshot
from sports_hedge.paper.entry_freshness import PaperEntryFreshness
from sports_hedge.paper.fills import PaperOpportunityFills, PaperOpportunityLeg
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision


class PaperChainStep(StrEnum):
    SCAN_DECISION = "scan_decision"
    PRIORITY_ALERT = "priority_alert"
    EXTERNAL_CONFIRMATION = "external_confirmation"
    HEDGE_REVALIDATION = "hedge_revalidation"
    SIMULATED_FILL = "simulated_fill"
    WATCHLIST_FILL = "watchlist_fill"
    JOURNAL_POSTING = "journal_posting"
    RECONCILIATION = "reconciliation"
    PAPER_AUTOFILL = "paper_autofill"
    PAPER_ENTRY_REJECTED = "paper_entry_rejected"


class PaperFillPlan(BaseModel):
    """Immutable solver-qualified snapshot used to simulate one paper fill.

    `scanned_at` is T1, the qualified decision. `autofill_dispatched_at` is T2
    telemetry only and must not age the snapshot.
    """

    opportunity_id: str
    canonical_event_id: str | None = None
    canonical_market_id: str | None = None
    scanned_at: datetime
    quote_age_ms: int | None = None
    quote_age_at_decision_ms: int | None = None
    quote_captured_at: datetime | None = None
    autofill_dispatched_at: datetime | None = None
    simulated_latency_ms: int = Field(default=500, ge=0)
    paper_entry_max_quote_age_ms: int = Field(default=2000, ge=0)
    mapping_confidence: float | None = Field(default=None, ge=0, le=1)
    execution_risk_score: int | None = Field(default=None, ge=0, le=100)
    gross_edge: Decimal | None = None
    net_edge: Decimal | None = None
    entry_freshness: PaperEntryFreshness | None = None
    eligible_for_paper_simulation: bool
    settlement_equivalent: bool
    legs: list[PaperOpportunityLeg] = Field(default_factory=list)
    execution_modes: dict[VenueName, LegExecutionMode] = Field(default_factory=dict)
    venue_costs: list[VenueCostSnapshot] = Field(default_factory=list)
    fx_snapshots: list[FxRateSnapshot] = Field(default_factory=list)
    decision: PaperScanDecision
    provenance: DataProvenance = DataProvenance.LIVE_PAPER
    pricing_lane: str | None = None
    execution_authoritative: bool = False
    execution_snapshot_json: str | None = None

    @model_validator(mode="after")
    def ensure_timezone(self) -> PaperFillPlan:
        if self.scanned_at.tzinfo is None:
            self.scanned_at = self.scanned_at.replace(tzinfo=UTC)
        if self.quote_captured_at is not None and self.quote_captured_at.tzinfo is None:
            self.quote_captured_at = self.quote_captured_at.replace(tzinfo=UTC)
        if self.autofill_dispatched_at is not None and self.autofill_dispatched_at.tzinfo is None:
            self.autofill_dispatched_at = self.autofill_dispatched_at.replace(tzinfo=UTC)
        if self.quote_age_at_decision_ms is None:
            self.quote_age_at_decision_ms = self.quote_age_ms
        return self

    @property
    def decision_at(self) -> datetime:
        return self.scanned_at


class PaperChainTrace(BaseModel):
    trace_id: str = Field(default_factory=lambda: str(uuid4()))
    opportunity_id: str
    steps: list[PaperChainStep]
    scan_eligible: bool
    watchlist_status: OpportunityStatus | None = None
    alert_id: str | None = None
    fill_ids: list[str] = Field(default_factory=list)
    journal_ids: list[str] = Field(default_factory=list)
    balanced_gbp: bool = False
    native_totals: dict[str, Decimal] = Field(default_factory=dict)
    provenance: DataProvenance = DataProvenance.LIVE_PAPER
    paper_only: bool = True
    places_orders: bool = False
    detail: str | None = None


class SimulatePaperFillRequest(BaseModel):
    opportunity_id: str
    operator_note: str = "PAPER-ONLY explicit simulate fill"
    capital_source: CapitalSource = CapitalSource.AUTO_POOL
    confirm_external: ExternalLegConfirmation | None = None
    provenance: DataProvenance = DataProvenance.LIVE_PAPER
    prepared_deployment_id: str | None = None
    requested_size_gbp: Decimal | None = Field(default=None, gt=0)
    simulate_external: bool = False


class SimulatePaperFillResult(BaseModel):
    opportunity: NearOpportunity
    fills: PaperOpportunityFills
    journals: list[PaperJournalEntry] = Field(default_factory=list)
    alert: PriorityAlert | None = None
    hedge_still_valid: bool | None = None
    trace: PaperChainTrace
    paper_only: bool = True
    places_orders: bool = False
    trade_id: str | None = None
    solver_model: str | None = None
    entry_complete: bool = False
    rejection_reason: str | None = None
    allocated_requested_stakes: dict[str, Decimal] = Field(default_factory=dict)
    prepared_deployment_id: str | None = None
    entry_freshness: PaperEntryFreshness | None = None
