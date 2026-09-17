"""Snapshot-bound paper-entry freshness.

Phase-1 paper economic simulation ages the qualified snapshot by configured
simulated execution latency only. Backend persistence / UI dispatch delay is
telemetry, not venue quote age.

T0 quote capture
T1 qualified decision (`PaperScanDecision.scanned_at`)
T2 autofill dispatch (wall-clock telemetry)
T3 simulated market arrival = T1 + simulated_execution_latency

quote_age_at_decision = T1 - T0
simulated_arrival_age = quote_age_at_decision + simulated_execution_latency
reject if simulated_arrival_age >= paper_entry_max_quote_age_ms
"""

from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel, Field, model_validator

from sports_hedge.application.quote_freshness import require_aware_instant

SNAPSHOT_STALE_AT_DECISION = "snapshot_stale_at_decision"
SNAPSHOT_STALE_AT_SIMULATED_ARRIVAL = "snapshot_stale_at_simulated_arrival"
UNKNOWN_QUOTE_AGE = "unknown_quote_age"
MARKET_REVALIDATION_FAILED = "market_revalidation_failed"

PAPER_ENTRY_FRESHNESS_REASONS = frozenset(
    {
        SNAPSHOT_STALE_AT_DECISION,
        SNAPSHOT_STALE_AT_SIMULATED_ARRIVAL,
        UNKNOWN_QUOTE_AGE,
        MARKET_REVALIDATION_FAILED,
    }
)


class PaperEntryFreshness(BaseModel):
    """Immutable timing diagnostics for one bound paper-entry snapshot."""

    decision_at: datetime
    autofill_dispatched_at: datetime
    quote_captured_at: datetime | None = None
    quote_age_at_decision_ms: int | None = Field(default=None, ge=0)
    simulated_latency_ms: int = Field(ge=0)
    paper_entry_max_quote_age_ms: int = Field(ge=0)
    simulated_arrival_quote_age_ms: int | None = Field(default=None, ge=0)
    decision_to_autofill_dispatch_ms: int = Field(ge=0)
    rejection_reason: str | None = None

    @model_validator(mode="after")
    def ensure_timezone(self) -> PaperEntryFreshness:
        if self.decision_at.tzinfo is None:
            self.decision_at = self.decision_at.replace(tzinfo=UTC)
        if self.autofill_dispatched_at.tzinfo is None:
            self.autofill_dispatched_at = self.autofill_dispatched_at.replace(tzinfo=UTC)
        if self.quote_captured_at is not None and self.quote_captured_at.tzinfo is None:
            self.quote_captured_at = self.quote_captured_at.replace(tzinfo=UTC)
        return self

    @property
    def accepted(self) -> bool:
        return self.rejection_reason is None

    def as_diagnostics(self) -> dict[str, int | str | None]:
        return {
            "decision_to_autofill_dispatch_ms": self.decision_to_autofill_dispatch_ms,
            "quote_age_at_decision_ms": self.quote_age_at_decision_ms,
            "simulated_latency_ms": self.simulated_latency_ms,
            "simulated_arrival_quote_age_ms": self.simulated_arrival_quote_age_ms,
            "paper_entry_max_quote_age_ms": self.paper_entry_max_quote_age_ms,
            "rejection_reason": self.rejection_reason,
        }

    def detail(self) -> str:
        parts = [
            f"decision_to_autofill_dispatch_ms={self.decision_to_autofill_dispatch_ms}",
            f"quote_age_at_decision_ms={self.quote_age_at_decision_ms}",
            f"simulated_latency_ms={self.simulated_latency_ms}",
            f"simulated_arrival_quote_age_ms={self.simulated_arrival_quote_age_ms}",
            f"paper_entry_max_quote_age_ms={self.paper_entry_max_quote_age_ms}",
        ]
        if self.rejection_reason:
            parts.append(f"rejection_reason={self.rejection_reason}")
        return " ".join(parts)


def snapshot_freshness_rejection(
    *,
    quote_age_at_decision_ms: int | None,
    simulated_latency_ms: int,
    paper_entry_max_quote_age_ms: int,
) -> str | None:
    """Reject using T1 quote age + simulated latency. Dispatch delay is ignored."""

    if paper_entry_max_quote_age_ms < 0:
        raise ValueError("paper_entry_max_quote_age_ms must be non-negative")
    if simulated_latency_ms < 0:
        raise ValueError("simulated_latency_ms must be non-negative")
    if quote_age_at_decision_ms is None:
        return UNKNOWN_QUOTE_AGE
    if quote_age_at_decision_ms < 0:
        raise ValueError("quote_age_at_decision_ms must be non-negative")
    if quote_age_at_decision_ms >= paper_entry_max_quote_age_ms:
        return SNAPSHOT_STALE_AT_DECISION
    simulated_arrival_age = quote_age_at_decision_ms + simulated_latency_ms
    if simulated_arrival_age >= paper_entry_max_quote_age_ms:
        return SNAPSHOT_STALE_AT_SIMULATED_ARRIVAL
    return None


def assess_paper_entry_freshness(
    *,
    decision_at: datetime,
    dispatched_at: datetime,
    quote_age_at_decision_ms: int | None,
    simulated_latency_ms: int,
    paper_entry_max_quote_age_ms: int,
    quote_captured_at: datetime | None = None,
) -> PaperEntryFreshness:
    """Evaluate bound-snapshot freshness and record T2 dispatch as telemetry only."""

    decision = require_aware_instant(decision_at, "decision_at")
    dispatched = require_aware_instant(dispatched_at, "autofill_dispatched_at")
    captured = None
    if quote_captured_at is not None:
        captured = require_aware_instant(quote_captured_at, "quote_captured_at")
    elapsed_ms = int((dispatched - decision).total_seconds() * 1000)
    dispatch_ms = max(0, elapsed_ms)
    arrival_age = None
    if quote_age_at_decision_ms is not None:
        arrival_age = quote_age_at_decision_ms + simulated_latency_ms
    reason = snapshot_freshness_rejection(
        quote_age_at_decision_ms=quote_age_at_decision_ms,
        simulated_latency_ms=simulated_latency_ms,
        paper_entry_max_quote_age_ms=paper_entry_max_quote_age_ms,
    )
    return PaperEntryFreshness(
        decision_at=decision,
        autofill_dispatched_at=dispatched,
        quote_captured_at=captured,
        quote_age_at_decision_ms=quote_age_at_decision_ms,
        simulated_latency_ms=simulated_latency_ms,
        paper_entry_max_quote_age_ms=paper_entry_max_quote_age_ms,
        simulated_arrival_quote_age_ms=arrival_age,
        decision_to_autofill_dispatch_ms=dispatch_ms,
        rejection_reason=reason,
    )


__all__ = [
    "MARKET_REVALIDATION_FAILED",
    "PAPER_ENTRY_FRESHNESS_REASONS",
    "PaperEntryFreshness",
    "SNAPSHOT_STALE_AT_DECISION",
    "SNAPSHOT_STALE_AT_SIMULATED_ARRIVAL",
    "UNKNOWN_QUOTE_AGE",
    "assess_paper_entry_freshness",
    "snapshot_freshness_rejection",
]
