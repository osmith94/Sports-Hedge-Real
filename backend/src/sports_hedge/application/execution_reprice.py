"""One exact-ID execution reprice immediately before a PAPER entry.

Discovery pricing may qualify an opportunity from the latest known books,
including when those books are past the paper-entry quote-age gate. That
discovery fact is QUALIFYING only. The fill uses a second, contemporaneous
complete-set read of the same persisted native IDs. PAPER_ELIGIBLE is that
refreshed decision, not the discovery snapshot.

PAPER only. This module does not place venue orders and does not rediscover
fixtures, markets, or matches.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from logging import getLogger
from typing import Any

from sports_hedge.application.executable_liquidity import (
    decision_is_solver_arbitrage,
    decision_net_edge,
)
from sports_hedge.application.paper_scan import _paper_blocking_reasons
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.application.execution_snapshot import ExecutionSnapshot
from sports_hedge.arbitrage.watchlist.ranking import opportunity_id_for_canonical_market
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.models import PaperScanDecision

LOGGER = getLogger("sports_hedge.execution_reprice")

EXECUTION_REPRICE_FAILED = "execution_reprice_failed"
EXECUTION_REPRICE_STALE = "execution_reprice_stale"
EXECUTION_REPRICE_NO_LONGER_QUALIFYING = "execution_reprice_no_longer_qualifying"
EXECUTION_REPRICE_SKEW = "execution_reprice_skew"
EXECUTION_REPRICE_DUPLICATE = "execution_reprice_duplicate"

PHASE_DISCOVERY_PRICE = "DISCOVERY_PRICE"
PHASE_REPRICE_STARTED = "EXECUTION_REPRICE_STARTED"
PHASE_BOOK_RETRIEVED = "EXECUTION_BOOK_RETRIEVED"
PHASE_SNAPSHOT_COMPLETE = "EXECUTION_SNAPSHOT_COMPLETE"
PHASE_REJECTED = "EXECUTION_REPRICE_REJECTED"
PHASE_PAPER_ELIGIBLE = "PAPER_ELIGIBLE"
PHASE_FILL_ATTEMPTED = "PAPER_FILL_ATTEMPTED"
PHASE_FILL_COMPLETE = "PAPER_FILL_COMPLETE"

# Known quote age is the only capture blocker this path may carry into the
# execution reprice. Unknown age, missing costs, mapping, depth, and edge
# failures stay fail-closed before any second provider read.
_QUOTE_AGE_REVALIDATION_REASONS = frozenset({"stale_quote"})


@dataclass(frozen=True)
class ExecutionProviderCall:
    """One exact-ID provider read inside a Price-2 complete set."""

    venue: str
    stage: str
    source_id: str
    outcome: str
    slot_wait_ms: int
    io_ms: int


@dataclass
class ExecutionRepriceDiagnostics:
    """Timing for one execution reprice. Ages are the books at evaluation."""

    started_at: datetime
    assembly_ms: int = 0
    calls: tuple[ExecutionProviderCall, ...] = ()
    quote_age_ms: dict[str, int | None] = field(default_factory=dict)
    reason: str | None = None

    def venue_slot_wait_ms(self, venue: str) -> int:
        waits = [call.slot_wait_ms for call in self.calls if call.venue == venue]
        return max(waits) if waits else 0

    def venue_io_ms(self, venue: str) -> int:
        durations = [call.io_ms for call in self.calls if call.venue == venue]
        return max(durations) if durations else 0

    def oldest_quote_age_ms(self) -> int | None:
        if not self.quote_age_ms:
            return None
        ages = [age for age in self.quote_age_ms.values() if age is not None]
        if len(ages) != len(self.quote_age_ms):
            return None
        return max(ages)

    def compact(self) -> str:
        """One audit line: start, assembly, per-venue wait, I/O, and quote age."""

        parts = [
            f"started_at={self.started_at.isoformat()}",
            f"assembly_ms={self.assembly_ms}",
        ]
        venues = list(dict.fromkeys(call.venue for call in self.calls))
        for venue in [*venues, *[name for name in self.quote_age_ms if name not in venues]]:
            parts.append(f"{venue}.slot_wait_ms={self.venue_slot_wait_ms(venue)}")
            parts.append(f"{venue}.io_ms={self.venue_io_ms(venue)}")
            age = self.quote_age_ms.get(venue)
            parts.append(f"{venue}.quote_age_ms={'unknown' if age is None else age}")
        return " ".join(parts)


def log_execution_phase(phase: str, **fields: object) -> None:
    """One structured audit line. Phase names are stable for log search."""

    parts = [f"phase={phase}"]
    for key, value in fields.items():
        if value is None:
            continue
        parts.append(f"{key}={value}")
    LOGGER.info(" ".join(parts))


def execution_reprice_audit_detail(
    reason: str,
    diagnostics: ExecutionRepriceDiagnostics | None,
    snapshot: ExecutionSnapshot | None = None,
) -> str:
    """Reason code plus snapshot skew and the Price-2 timings."""

    parts = [reason]
    if snapshot is not None:
        parts.append(snapshot.audit_line())
    if diagnostics is not None and diagnostics.compact():
        parts.append(diagnostics.compact())
    return " ".join(parts)


@dataclass
class ExecutionRepriceResult:
    """One execution-time PaperScanDecision, or a fail-closed reason."""

    decision: PaperScanDecision | None = None
    reason: str | None = None
    refreshed_venues: tuple[VenueName, ...] = ()
    diagnostics: ExecutionRepriceDiagnostics | None = None
    snapshot: ExecutionSnapshot | None = None
    duplicate: bool = False


@dataclass
class ExecutionCaptureResult:
    """Discovery audit history plus the authoritative entry decision, if any."""

    discovery_history: list[Any] = field(default_factory=list)
    discovery_decision: PaperScanDecision | None = None
    entry_decision: PaperScanDecision | None = None
    entry_history: list[Any] | None = None


def execution_reprice_permitted(decision: PaperScanDecision) -> bool:
    """True when a solver-qualified decision may request one execution reprice.

    Fully capture-eligible decisions reprice too. A decision whose only
    blocking reason is known quote-age staleness may also reprice. That does
    not mark the stale decision capture-eligible.
    """

    if not decision.canonical_market_id or not decision.market_match.matched:
        return False
    if not decision_is_solver_arbitrage(decision):
        return False
    edge = decision_net_edge(decision)
    minimum = decision.minimum_net_edge
    if edge is None or minimum is None or edge < minimum:
        return False
    blocking = _paper_blocking_reasons(list(decision.rejection_reasons))
    remaining = [reason for reason in blocking if reason not in _QUOTE_AGE_REVALIDATION_REASONS]
    if remaining:
        return False
    if decision.eligible_for_paper_simulation:
        return not blocking
    return "stale_quote" in blocking and "unknown_quote_age" not in blocking


def execution_entry_block(decision: PaperScanDecision) -> str | None:
    """Why a refreshed decision must not fill. None means it is capture-eligible."""

    blocking = _paper_blocking_reasons(list(decision.rejection_reasons))
    if any(reason in {"stale_quote", "unknown_quote_age"} for reason in blocking):
        return EXECUTION_REPRICE_STALE
    if not decision.eligible_for_paper_simulation or not decision_is_solver_arbitrage(decision):
        return EXECUTION_REPRICE_NO_LONGER_QUALIFYING
    edge = decision_net_edge(decision)
    minimum = decision.minimum_net_edge
    if edge is None or minimum is None or edge < minimum:
        return EXECUTION_REPRICE_NO_LONGER_QUALIFYING
    return None


def hedge_venues(decision: PaperScanDecision) -> tuple[VenueName, ...]:
    """Venues required for one contemporaneous complete-set reprice."""

    ordered: list[VenueName] = []

    def add(venue: VenueName | None) -> None:
        if venue is not None and venue not in ordered:
            ordered.append(venue)

    for leg in decision.fill_legs:
        add(getattr(leg, "venue", None))
    depth = decision.depth_scan
    if depth is not None:
        for quote in depth.selected_quotes:
            add(getattr(quote, "venue", None))
    payoff = decision.payoff_scan
    if payoff is not None:
        for quote in payoff.selected_quotes:
            add(getattr(quote, "venue", None))
    return tuple(ordered)


def _diagnostic_quote_age(diagnostics: ExecutionRepriceDiagnostics | None) -> int | None:
    if diagnostics is None:
        return None
    return diagnostics.oldest_quote_age_ms()


def _history(service: Any, decision: PaperScanDecision) -> list[Any]:
    if not decision.canonical_market_id:
        return []
    records = service.market_intelligence.market_history(
        canonical_market_id=decision.canonical_market_id,
    )
    return list(records or [])


def _observe(
    watchlist: WatchlistService,
    service: Any,
    decision: PaperScanDecision,
    *,
    pricing_lane: str | None,
    history: Sequence[Any] | None = None,
) -> list[Any]:
    records = list(history) if history is not None else _history(service, decision)
    watchlist.observe_paper_decision(
        decision,
        records,
        quote_age_ms=decision.quote_age_ms,
        pricing_lane=pricing_lane,
    )
    return records


async def capture_with_execution_reprice(
    decision: PaperScanDecision,
    *,
    runtime: Any,
    engine: Any,
    service: Any,
    watchlist: WatchlistService,
    pricing_lane: str | None,
) -> ExecutionCaptureResult:
    """Qualify from the discovery decision, then fill only from one reprice.

    The discovery observation never emits PAPER_ELIGIBLE. The refreshed
    decision is the capture snapshot. One provider failure, a still-stale
    book, or an edge that no longer clears the trigger does not fill and
    does not schedule another reprice.
    """

    from sports_hedge.api.paper import persist_price_engine_item_capture

    if not execution_reprice_permitted(decision):
        history = persist_price_engine_item_capture(
            decision,
            service=service,
            watchlist=watchlist,
            pricing_lane=pricing_lane,
        )
        return ExecutionCaptureResult(
            discovery_history=list(history or []),
            discovery_decision=decision,
            entry_decision=decision,
            entry_history=list(history or []),
        )

    discovery = decision
    if decision.eligible_for_paper_simulation:
        discovery = decision.model_copy(update={"eligible_for_paper_simulation": False})
    discovery_history = _observe(
        watchlist,
        service,
        discovery,
        pricing_lane=pricing_lane,
    )
    log_execution_phase(
        PHASE_DISCOVERY_PRICE,
        canonical_market_id=decision.canonical_market_id,
        quote_age_ms=decision.quote_age_ms,
        net_edge=decision_net_edge(decision),
        pricing_lane=pricing_lane,
    )
    opportunity_id = opportunity_id_for_canonical_market(decision.canonical_market_id or "")
    if watchlist.has_active_bound_attempt(opportunity_id):
        log_execution_phase(
            PHASE_REJECTED,
            reason=EXECUTION_REPRICE_DUPLICATE,
            canonical_market_id=decision.canonical_market_id,
        )
        return ExecutionCaptureResult(
            discovery_history=discovery_history,
            discovery_decision=discovery,
        )
    venues = hedge_venues(decision)
    refreshed = await engine.reprice_for_paper_entry(runtime, venues=venues)
    if refreshed.duplicate:
        log_execution_phase(
            PHASE_REJECTED,
            reason=EXECUTION_REPRICE_DUPLICATE,
            canonical_market_id=decision.canonical_market_id,
        )
        return ExecutionCaptureResult(
            discovery_history=discovery_history,
            discovery_decision=discovery,
        )
    if refreshed.decision is None:
        reason = refreshed.reason or EXECUTION_REPRICE_FAILED
        watchlist.note_execution_reprice_miss(
            decision,
            occurred_at=datetime.now(UTC),
            reason=reason,
            pricing_lane=pricing_lane,
            detail=execution_reprice_audit_detail(
                reason,
                refreshed.diagnostics,
                refreshed.snapshot,
            ),
            quote_age_ms=_diagnostic_quote_age(refreshed.diagnostics),
        )
        log_execution_phase(
            PHASE_REJECTED,
            reason=reason,
            canonical_market_id=decision.canonical_market_id,
            snapshot=None if refreshed.snapshot is None else refreshed.snapshot.audit_line(),
        )
        return ExecutionCaptureResult(
            discovery_history=discovery_history,
            discovery_decision=discovery,
        )

    block = execution_entry_block(refreshed.decision)
    if block is not None:
        entry_history = _observe(
            watchlist,
            service,
            refreshed.decision,
            pricing_lane=pricing_lane,
        )
        watchlist.note_execution_reprice_miss(
            refreshed.decision,
            occurred_at=datetime.now(UTC),
            reason=block,
            pricing_lane=pricing_lane,
            detail=execution_reprice_audit_detail(
                block,
                refreshed.diagnostics,
                refreshed.snapshot,
            ),
            quote_age_ms=_diagnostic_quote_age(refreshed.diagnostics),
        )
        log_execution_phase(
            PHASE_REJECTED,
            reason=block,
            canonical_market_id=refreshed.decision.canonical_market_id,
        )
        return ExecutionCaptureResult(
            discovery_history=discovery_history,
            discovery_decision=discovery,
            entry_decision=refreshed.decision,
            entry_history=entry_history,
        )

    log_execution_phase(
        PHASE_PAPER_ELIGIBLE,
        canonical_market_id=refreshed.decision.canonical_market_id,
        net_edge=decision_net_edge(refreshed.decision),
        snapshot=None if refreshed.snapshot is None else refreshed.snapshot.snapshot_id,
    )
    snapshot_json = None if refreshed.snapshot is None else refreshed.snapshot.to_json()
    entry_history = persist_price_engine_item_capture(
        refreshed.decision,
        service=service,
        watchlist=watchlist,
        refreshed_venues=refreshed.refreshed_venues,
        pricing_lane=pricing_lane,
        execution_authoritative=True,
        execution_snapshot_json=snapshot_json,
    )
    return ExecutionCaptureResult(
        discovery_history=discovery_history,
        discovery_decision=discovery,
        entry_decision=refreshed.decision,
        entry_history=list(entry_history or []),
    )
