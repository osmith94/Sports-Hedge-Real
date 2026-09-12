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


class PaperFillPlan(BaseModel):
    """Last solver-validated books needed to simulate an explicit paper fill."""

    opportunity_id: str
    canonical_event_id: str | None = None
    canonical_market_id: str | None = None
    scanned_at: datetime
    quote_age_ms: int | None = None
    eligible_for_paper_simulation: bool
    settlement_equivalent: bool
    legs: list[PaperOpportunityLeg] = Field(default_factory=list)
    execution_modes: dict[VenueName, LegExecutionMode] = Field(default_factory=dict)
    venue_costs: list[VenueCostSnapshot] = Field(default_factory=list)
    fx_snapshots: list[FxRateSnapshot] = Field(default_factory=list)
    decision: PaperScanDecision
    provenance: DataProvenance = DataProvenance.LIVE_PAPER

    @model_validator(mode="after")
    def ensure_timezone(self) -> PaperFillPlan:
        if self.scanned_at.tzinfo is None:
            self.scanned_at = self.scanned_at.replace(tzinfo=UTC)
        return self


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
