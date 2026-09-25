"""Explicit UNIVERSE generation / chunk / sweep lifecycle contracts.

These tables wrap the existing LiveRefreshCoordinator mutations. They do not
replace coordinator state; they decide whether a mutation is legal and emit an
auditable reason when it is not.

Operator-visible worker_state labels (idle/running/waiting/degraded/complete)
are unchanged. Generation completeness is distinct from chunk timeout.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Iterable

from sports_hedge.application.scan_lanes import (
    WORKER_COMPLETE,
    WORKER_DEGRADED,
    WORKER_IDLE,
    WORKER_RUNNING,
    WORKER_WAITING,
)
from sports_hedge.application.universe_checkpoint import (
    SERIES_TERMINAL_STATES,
    SWEEP_EVALUATED,
    SWEEP_FINAL_FAILED,
    SWEEP_OK,
    SWEEP_PENDING,
    SWEEP_RETRY_WAIT,
    SWEEP_RUNNING,
    SWEEP_SINGLE_VENUE,
    SWEEP_SKIPPED_UNSUPPORTED,
    SWEEP_STALE_ORPHAN,
    SWEEP_TERMINAL_STATES,
)
from sports_hedge.lifecycle.decisions import (
    LifecycleDecision,
    allowed_decision,
    rejected_decision,
)

MACHINE_GENERATION = "universe_generation"
MACHINE_CHUNK = "universe_chunk"
MACHINE_SWEEP = "universe_sweep_unit"
MACHINE_SERIES = "universe_series_unit"
MACHINE_WORKER = "universe_worker"

UNIVERSE_COMPLETENESS_COMPLETE = "complete"
UNIVERSE_COMPLETENESS_DEADLINE_LEFTOVER = "deadline_leftover"
UNIVERSE_COMPLETENESS_STALE_GENERATION_STATE = "stale_generation_state"
UNFINISHED_COMPLETENESS = frozenset(
    {
        UNIVERSE_COMPLETENESS_DEADLINE_LEFTOVER,
        UNIVERSE_COMPLETENESS_STALE_GENERATION_STATE,
    }
)


class UniverseGenerationPhase(StrEnum):
    IDLE = "idle"
    OPEN = "open"
    COMPLETE = "complete"


class UniverseChunkPhase(StrEnum):
    INACTIVE = "inactive"
    RUNNING = "running"


# Explicit generation graph. Same-state is always allowed (idempotent ensure).
GENERATION_TRANSITIONS: dict[str, frozenset[str]] = {
    UniverseGenerationPhase.IDLE: frozenset(
        {UniverseGenerationPhase.IDLE, UniverseGenerationPhase.OPEN}
    ),
    UniverseGenerationPhase.OPEN: frozenset(
        {
            UniverseGenerationPhase.OPEN,
            UniverseGenerationPhase.COMPLETE,
            UniverseGenerationPhase.IDLE,
        }
    ),
    UniverseGenerationPhase.COMPLETE: frozenset(
        {UniverseGenerationPhase.COMPLETE, UniverseGenerationPhase.OPEN, UniverseGenerationPhase.IDLE}
    ),
}

# complete requires a genuine sweep finish; idle is reset/clear/supersede.
GENERATION_ACTIONS: dict[tuple[str, str], frozenset[str]] = {
    (UniverseGenerationPhase.IDLE, UniverseGenerationPhase.OPEN): frozenset(
        {"start", "resume", "restore_checkpoint"}
    ),
    (UniverseGenerationPhase.OPEN, UniverseGenerationPhase.OPEN): frozenset(
        {
            "ensure_already_open",
            "timeout",
            "retry",
            "resume",
            "chunk_progress",
            "provider_degraded",
        }
    ),
    (UniverseGenerationPhase.OPEN, UniverseGenerationPhase.COMPLETE): frozenset({"complete"}),
    (UniverseGenerationPhase.OPEN, UniverseGenerationPhase.IDLE): frozenset(
        {"clear", "reset", "supersede"}
    ),
    (UniverseGenerationPhase.COMPLETE, UniverseGenerationPhase.OPEN): frozenset({"start", "resume"}),
    (UniverseGenerationPhase.COMPLETE, UniverseGenerationPhase.IDLE): frozenset({"clear", "reset"}),
    (UniverseGenerationPhase.IDLE, UniverseGenerationPhase.IDLE): frozenset(
        {"clear", "reset", "ensure_idle"}
    ),
    (UniverseGenerationPhase.COMPLETE, UniverseGenerationPhase.COMPLETE): frozenset(
        {"already_complete"}
    ),
}

CHUNK_TRANSITIONS: dict[str, frozenset[str]] = {
    UniverseChunkPhase.INACTIVE: frozenset(
        {UniverseChunkPhase.INACTIVE, UniverseChunkPhase.RUNNING}
    ),
    UniverseChunkPhase.RUNNING: frozenset(
        {UniverseChunkPhase.RUNNING, UniverseChunkPhase.INACTIVE}
    ),
}

SWEEP_TRANSITIONS: dict[str, frozenset[str]] = {
    SWEEP_PENDING: frozenset(
        {
            SWEEP_PENDING,
            SWEEP_RUNNING,
            SWEEP_EVALUATED,
            SWEEP_RETRY_WAIT,
            SWEEP_SINGLE_VENUE,
            SWEEP_SKIPPED_UNSUPPORTED,
            SWEEP_FINAL_FAILED,
            SWEEP_STALE_ORPHAN,
        }
    ),
    SWEEP_RUNNING: frozenset(
        {
            SWEEP_PENDING,
            SWEEP_RUNNING,
            SWEEP_EVALUATED,
            SWEEP_RETRY_WAIT,
            SWEEP_SINGLE_VENUE,
            SWEEP_SKIPPED_UNSUPPORTED,
            SWEEP_FINAL_FAILED,
        }
    ),
    SWEEP_RETRY_WAIT: frozenset(
        {
            SWEEP_PENDING,
            SWEEP_RETRY_WAIT,
            SWEEP_EVALUATED,
            SWEEP_SINGLE_VENUE,
            SWEEP_SKIPPED_UNSUPPORTED,
            SWEEP_FINAL_FAILED,
        }
    ),
    # Evaluated is sticky: a later leftover/timeout must not reopen it.
    SWEEP_EVALUATED: frozenset({SWEEP_EVALUATED}),
    # Terminal failures may recover to evaluated if a later authoritative pass succeeds.
    SWEEP_FINAL_FAILED: frozenset({SWEEP_FINAL_FAILED, SWEEP_EVALUATED}),
    SWEEP_SKIPPED_UNSUPPORTED: frozenset({SWEEP_SKIPPED_UNSUPPORTED, SWEEP_EVALUATED}),
    SWEEP_STALE_ORPHAN: frozenset({SWEEP_STALE_ORPHAN, SWEEP_PENDING, SWEEP_EVALUATED}),
    SWEEP_SINGLE_VENUE: frozenset(
        {
            SWEEP_SINGLE_VENUE,
            SWEEP_EVALUATED,
            SWEEP_PENDING,
            SWEEP_RETRY_WAIT,
            SWEEP_SKIPPED_UNSUPPORTED,
            SWEEP_FINAL_FAILED,
        }
    ),
}

SERIES_TRANSITIONS: dict[str, frozenset[str]] = {
    SWEEP_PENDING: frozenset(
        {SWEEP_PENDING, SWEEP_OK, SWEEP_RETRY_WAIT, SWEEP_SKIPPED_UNSUPPORTED, SWEEP_FINAL_FAILED}
    ),
    SWEEP_RETRY_WAIT: frozenset(
        {SWEEP_PENDING, SWEEP_RETRY_WAIT, SWEEP_OK, SWEEP_SKIPPED_UNSUPPORTED, SWEEP_FINAL_FAILED}
    ),
    SWEEP_OK: frozenset({SWEEP_OK, SWEEP_RETRY_WAIT}),
    SWEEP_FINAL_FAILED: frozenset({SWEEP_FINAL_FAILED, SWEEP_OK}),
    SWEEP_SKIPPED_UNSUPPORTED: frozenset({SWEEP_SKIPPED_UNSUPPORTED, SWEEP_OK}),
}

WORKER_TRANSITIONS: dict[str, frozenset[str]] = {
    WORKER_IDLE: frozenset({WORKER_IDLE, WORKER_RUNNING, WORKER_WAITING, WORKER_DEGRADED}),
    WORKER_RUNNING: frozenset(
        {WORKER_RUNNING, WORKER_IDLE, WORKER_WAITING, WORKER_DEGRADED, WORKER_COMPLETE}
    ),
    WORKER_WAITING: frozenset(
        {WORKER_WAITING, WORKER_RUNNING, WORKER_IDLE, WORKER_DEGRADED, WORKER_COMPLETE}
    ),
    WORKER_DEGRADED: frozenset(
        {WORKER_DEGRADED, WORKER_RUNNING, WORKER_WAITING, WORKER_IDLE, WORKER_COMPLETE}
    ),
    WORKER_COMPLETE: frozenset({WORKER_COMPLETE, WORKER_IDLE, WORKER_RUNNING, WORKER_WAITING}),
}

EVALUATION_TO_SWEEP_STATE: dict[str, str] = {
    "evaluated": SWEEP_EVALUATED,
    "single_venue_no_cross_venue_candidate": SWEEP_SINGLE_VENUE,
    "market_fetch_unavailable": SWEEP_RETRY_WAIT,
    "unsupported": SWEEP_SKIPPED_UNSUPPORTED,
    "skipped_unsupported": SWEEP_SKIPPED_UNSUPPORTED,
    "auth_failure": SWEEP_FINAL_FAILED,
}

SERIES_STATUS_TO_STATE: dict[str, str] = {
    "ok": SWEEP_OK,
    "unsupported": SWEEP_SKIPPED_UNSUPPORTED,
    "skipped_unsupported": SWEEP_SKIPPED_UNSUPPORTED,
    "auth_failure": SWEEP_FINAL_FAILED,
}


def generation_phase(*, generation_id: int, started: bool, closed_generation_id: int) -> str:
    if started:
        return UniverseGenerationPhase.OPEN
    if generation_id <= 0:
        return UniverseGenerationPhase.IDLE
    if closed_generation_id == generation_id:
        return UniverseGenerationPhase.COMPLETE
    return UniverseGenerationPhase.IDLE


def chunk_phase(active_epoch: int | None) -> str:
    if active_epoch is None:
        return UniverseChunkPhase.INACTIVE
    return UniverseChunkPhase.RUNNING


def map_evaluation_to_sweep_state(evaluation_state: str) -> str | None:
    key = str(evaluation_state or "").strip()
    if not key:
        return None
    if key in EVALUATION_TO_SWEEP_STATE:
        return EVALUATION_TO_SWEEP_STATE[key]
    if key in SWEEP_TRANSITIONS:
        return key
    return SWEEP_PENDING


def map_series_status_to_state(status: str, *, retryable: bool = False) -> str | None:
    key = str(status or "").strip()
    if not key:
        return None
    if key in SERIES_STATUS_TO_STATE:
        return SERIES_STATUS_TO_STATE[key]
    if retryable or key in {
        "timeout",
        "rate_limited",
        "discovery_timeout",
        "market_timeout",
        "unavailable",
        "not_started",
        "deferred",
    }:
        return SWEEP_RETRY_WAIT
    return SWEEP_FINAL_FAILED


def _graph_decision(
    *,
    machine: str,
    graph: dict[str, frozenset[str]],
    from_state: str,
    to_state: str,
    action: str,
    illegal_reason: str,
    **kwargs,
) -> LifecycleDecision:
    allowed = graph.get(str(from_state), frozenset())
    if str(to_state) not in allowed:
        return rejected_decision(
            from_state=from_state,
            to_state=to_state,
            action=action,
            machine=machine,
            reason=illegal_reason,
            **kwargs,
        )
    return allowed_decision(
        from_state=from_state,
        to_state=to_state,
        action=action,
        machine=machine,
        **kwargs,
    )


def decide_generation_transition(
    from_phase: str,
    to_phase: str,
    *,
    action: str,
    unfinished: bool = False,
    completeness: str | None = None,
    leftover_n: int | None = None,
    generation_id: int | None = None,
    detail: str | None = None,
) -> LifecycleDecision:
    """Decide a generation-level transition.

    ``complete`` is refused when the sweep is unfinished, leftover, or the
    collector reported a non-complete completeness string. Clear/reset/supersede
    may leave OPEN without claiming COMPLETE.
    """

    kwargs = {"generation_id": generation_id, "detail": detail or completeness}
    if action == "complete" or to_phase == UniverseGenerationPhase.COMPLETE:
        if unfinished or leftover_n not in {None, 0} or completeness in UNFINISHED_COMPLETENESS:
            return rejected_decision(
                from_state=from_phase,
                to_state=UniverseGenerationPhase.COMPLETE,
                action=action,
                machine=MACHINE_GENERATION,
                reason="illegal_complete_unfinished_generation",
                extra={"leftover_n": leftover_n, "completeness": completeness},
                **kwargs,
            )
        if from_phase != UniverseGenerationPhase.OPEN:
            return rejected_decision(
                from_state=from_phase,
                to_state=UniverseGenerationPhase.COMPLETE,
                action=action,
                machine=MACHINE_GENERATION,
                reason="illegal_complete_generation_not_open",
                **kwargs,
            )
    allowed_actions = GENERATION_ACTIONS.get((str(from_phase), str(to_phase)))
    if allowed_actions is not None and action not in allowed_actions and from_phase != to_phase:
        return rejected_decision(
            from_state=from_phase,
            to_state=to_phase,
            action=action,
            machine=MACHINE_GENERATION,
            reason=f"illegal_generation_action:{action}",
            **kwargs,
        )
    return _graph_decision(
        machine=MACHINE_GENERATION,
        graph=GENERATION_TRANSITIONS,
        from_state=from_phase,
        to_state=to_phase,
        action=action,
        illegal_reason=f"illegal_generation_transition:{from_phase}->{to_phase}",
        **kwargs,
    )


def decide_chunk_transition(
    from_phase: str,
    to_phase: str,
    *,
    action: str,
    generation_id: int | None = None,
    chunk_epoch: int | None = None,
    generation_open: bool = True,
) -> LifecycleDecision:
    if action == "open" and not generation_open:
        return rejected_decision(
            from_state=from_phase,
            to_state=to_phase,
            action=action,
            machine=MACHINE_CHUNK,
            reason="illegal_chunk_open_without_generation",
            generation_id=generation_id,
            chunk_epoch=chunk_epoch,
        )
    return _graph_decision(
        machine=MACHINE_CHUNK,
        graph=CHUNK_TRANSITIONS,
        from_state=from_phase,
        to_state=to_phase,
        action=action,
        illegal_reason=f"illegal_chunk_transition:{from_phase}->{to_phase}",
        generation_id=generation_id,
        chunk_epoch=chunk_epoch,
    )


def decide_stale_chunk_callback(
    *,
    callback_epoch: int | None,
    active_epoch: int | None,
    generation_id: int | None = None,
) -> LifecycleDecision:
    """Epoch-quarantine for Issue #330. Matching epoch is accepted.

    ``callback_epoch is None`` remains accepted for legacy callers, matching
    production ``_reject_stale_universe_chunk_unlocked``.
    """

    if callback_epoch is None or callback_epoch == active_epoch:
        return allowed_decision(
            from_state=chunk_phase(active_epoch),
            to_state=chunk_phase(active_epoch),
            action="chunk_callback",
            machine=MACHINE_CHUNK,
            reason="current_chunk_callback",
            generation_id=generation_id,
            chunk_epoch=callback_epoch,
        )
    return rejected_decision(
        from_state=chunk_phase(active_epoch),
        to_state=chunk_phase(active_epoch),
        action="stale_callback",
        machine=MACHINE_CHUNK,
        reason="stale_chunk_callback_quarantined",
        generation_id=generation_id,
        chunk_epoch=callback_epoch,
        extra={"active_epoch": active_epoch},
    )


def decide_sweep_transition(
    from_state: str,
    to_state: str,
    *,
    action: str = "apply_result",
    generation_id: int | None = None,
) -> LifecycleDecision:
    return _graph_decision(
        machine=MACHINE_SWEEP,
        graph=SWEEP_TRANSITIONS,
        from_state=from_state,
        to_state=to_state,
        action=action,
        illegal_reason=f"illegal_sweep_transition:{from_state}->{to_state}",
        generation_id=generation_id,
    )


def decide_series_transition(
    from_state: str,
    to_state: str,
    *,
    action: str = "apply_result",
    generation_id: int | None = None,
) -> LifecycleDecision:
    return _graph_decision(
        machine=MACHINE_SERIES,
        graph=SERIES_TRANSITIONS,
        from_state=from_state,
        to_state=to_state,
        action=action,
        illegal_reason=f"illegal_series_transition:{from_state}->{to_state}",
        generation_id=generation_id,
    )


def decide_worker_transition(
    from_state: str,
    to_state: str,
    *,
    action: str,
    generation_id: int | None = None,
    unfinished: bool = False,
) -> LifecycleDecision:
    if to_state == WORKER_COMPLETE and unfinished:
        return rejected_decision(
            from_state=from_state,
            to_state=to_state,
            action=action,
            machine=MACHINE_WORKER,
            reason="illegal_worker_complete_unfinished_generation",
            generation_id=generation_id,
        )
    return _graph_decision(
        machine=MACHINE_WORKER,
        graph=WORKER_TRANSITIONS,
        from_state=from_state,
        to_state=to_state,
        action=action,
        illegal_reason=f"illegal_worker_transition:{from_state}->{to_state}",
        generation_id=generation_id,
    )


def sweep_units_unfinished(states: Iterable[str]) -> bool:
    return any(state not in SWEEP_TERMINAL_STATES for state in states)


def series_units_unfinished(states: Iterable[str]) -> bool:
    return any(state not in SERIES_TERMINAL_STATES for state in states)
