"""Snapshot-bound paper-entry freshness.

Phase-1 paper economic simulation ages the qualified snapshot by configured
simulated execution latency only. Backend persistence / UI dispatch delay is
telemetry, not venue quote age — and only for an already-started autofill
attempt.

T0 quote capture
T1 qualified decision (`PaperScanDecision.scanned_at`)
T2 autofill dispatch (wall-clock telemetry)
T3 simulated market arrival = T1 + simulated_execution_latency

quote_age_at_decision = max over required legs of
    known provider/source age at capture + (T1 - capture/observation)
simulated_arrival_age = quote_age_at_decision + simulated_execution_latency
reject if simulated_arrival_age >= paper_entry_max_quote_age_ms
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, Field, model_validator

from sports_hedge.application.quote_freshness import require_aware_instant
from sports_hedge.paper.fills import PaperOpportunityLeg

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


def _elapsed_ms(start: datetime, end: datetime) -> int:
    return max(0, int((end - start).total_seconds() * 1000))


def conservative_quote_age_at_decision_ms(
    *,
    decision_at: datetime,
    known_age_ms: int | None = None,
    quote_captured_at: datetime | None = None,
    legs: Sequence[PaperOpportunityLeg] = (),
) -> int | None:
    """T1 quote age from each required opening leg. Never cross-combine legs.

    For every required leg: provider/source age at that leg's capture plus
    elapsed capture → T1. The conservative result is the max of those real
    legs. Plan-level `known_age_ms` / `quote_captured_at` are used only when
    no per-leg timing exists. A required leg with neither age nor capture is
    unknown.
    """

    decision = require_aware_instant(decision_at, "decision_at")
    required = [leg for leg in legs if leg.requested_stake > 0]
    if required:
        ages: list[int] = []
        for leg in required:
            if leg.quote_age_ms is None and leg.quote_captured_at is None:
                return None
            elapsed = 0
            if leg.quote_captured_at is not None:
                captured = require_aware_instant(leg.quote_captured_at, "quote_captured_at")
                elapsed = _elapsed_ms(captured, decision)
            provider_age = 0 if leg.quote_age_ms is None else leg.quote_age_ms
            ages.append(provider_age + elapsed)
        return max(ages)
    if quote_captured_at is not None:
        captured = require_aware_instant(quote_captured_at, "quote_captured_at")
        elapsed = _elapsed_ms(captured, decision)
        if known_age_ms is None:
            return elapsed
        return known_age_ms + elapsed
    return known_age_ms


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
    legs: Sequence[PaperOpportunityLeg] = (),
    snapshot_bound: bool = False,
) -> PaperEntryFreshness:
    """Evaluate paper-entry freshness.

    `snapshot_bound=True` is for an already-started autofill attempt: T2 dispatch
    is telemetry only. Manual/public fills include T1→T2 elapsed in the
    current-age gate so an aged radar row cannot be revived from T1 alone.
    """

    decision = require_aware_instant(decision_at, "decision_at")
    dispatched = require_aware_instant(dispatched_at, "autofill_dispatched_at")
    captured = None
    if quote_captured_at is not None:
        captured = require_aware_instant(quote_captured_at, "quote_captured_at")
    derived_age = conservative_quote_age_at_decision_ms(
        decision_at=decision,
        known_age_ms=quote_age_at_decision_ms,
        quote_captured_at=captured,
        legs=legs,
    )
    dispatch_ms = _elapsed_ms(decision, dispatched)
    arrival_age = None if derived_age is None else derived_age + simulated_latency_ms
    reason = snapshot_freshness_rejection(
        quote_age_at_decision_ms=derived_age,
        simulated_latency_ms=simulated_latency_ms,
        paper_entry_max_quote_age_ms=paper_entry_max_quote_age_ms,
    )
    if reason is None and not snapshot_bound and derived_age is not None:
        current_age = derived_age + dispatch_ms
        arrival_age = current_age + simulated_latency_ms
        if current_age >= paper_entry_max_quote_age_ms:
            reason = MARKET_REVALIDATION_FAILED
        elif arrival_age >= paper_entry_max_quote_age_ms:
            reason = SNAPSHOT_STALE_AT_SIMULATED_ARRIVAL
    return PaperEntryFreshness(
        decision_at=decision,
        autofill_dispatched_at=dispatched,
        quote_captured_at=captured,
        quote_age_at_decision_ms=derived_age,
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
    "conservative_quote_age_at_decision_ms",
    "snapshot_freshness_rejection",
]
