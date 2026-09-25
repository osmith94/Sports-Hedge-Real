"""Attribute synchronous event-loop stretches to the worker phase that owns them.

HOT, UNIVERSE, BACKGROUND, ACTIVE TRADE and paper settlement share one asyncio
loop with cheap API handlers. A slice is open from ``mark_loop_phase`` until
``close_loop_slice`` / ``yield_event_loop`` / the next mark. A raw ``await``
does not close it: code that awaits inside a marked phase must close the slice
first, or the reported "slice" includes await time and other coroutines' work.
Stretches at or above the 0.25s liveness bound are logged for the owner-live
Windows probe.

``sync_subphase`` times one region that contains no await, so its elapsed time
is a true synchronous stretch. ``TimedRLock`` reports how long the event-loop
thread waited for a lock that API threadpool handlers also take; that wait
blocks every coroutine exactly like CPU work does. ``install_gc_pause_monitor``
records garbage-collection pauses on the loop thread for the same reason.
"""

from __future__ import annotations

import asyncio
import gc
import logging
import threading
import time
from collections import deque
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)

STALL_LOG_SECONDS = 0.25
_HISTORY_LIMIT = 64
_SUBPHASE_REPORT_LIMIT = 12


@dataclass(frozen=True)
class PhaseSample:
    lane: str
    phase: str
    shard_id: str | None
    events: int
    candidates: int
    elapsed_s: float = 0.0


@dataclass
class SubphaseStat:
    count: int = 0
    total_s: float = 0.0
    max_s: float = 0.0
    max_events: int = 0
    max_candidates: int = 0


def _on_event_loop_thread() -> bool:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class LoopActivity:
    """Single-threaded phase clock. Only one coroutine runs synchronous code."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.current = PhaseSample("idle", "idle", None, 0, 0)
        self.longest: PhaseSample | None = None
        self.history: deque[PhaseSample] = deque(maxlen=_HISTORY_LIMIT)
        self.phases_seen: set[tuple[str, str]] = set()
        self.lanes_seen: set[str] = set()
        self.mark_counts: dict[tuple[str, str], int] = {}
        self.offloop_persistence: int = 0
        self.subphases: dict[tuple[str, str], SubphaseStat] = {}
        self.lock_waits: dict[str, SubphaseStat] = {}
        self.gc_pauses: dict[str, SubphaseStat] = {}
        self._gc_started: float | None = None
        self._slice_open = False
        self._slice_started = time.perf_counter()

    def mark(
        self,
        *,
        lane: str,
        phase: str,
        shard_id: str | None = None,
        events: int = 0,
        candidates: int = 0,
        inherit_shard: bool = False,
    ) -> None:
        self.note_yield()
        resolved_shard = shard_id
        if resolved_shard is None and inherit_shard:
            resolved_shard = self.current.shard_id
        sample = PhaseSample(lane, phase, resolved_shard, int(events), int(candidates))
        self.current = sample
        self.history.append(sample)
        key = (lane, phase)
        self.phases_seen.add(key)
        self.lanes_seen.add(lane)
        self.mark_counts[key] = self.mark_counts.get(key, 0) + 1
        self._slice_started = time.perf_counter()
        self._slice_open = True

    def note_yield(self) -> float:
        """Close the open synchronous slice. A closed slice ignores await time."""

        if not self._slice_open:
            return 0.0
        elapsed = max(0.0, time.perf_counter() - self._slice_started)
        self._slice_open = False
        self._slice_started = time.perf_counter()
        if elapsed < 0.001 or self.current.phase == "idle":
            return elapsed
        if self.longest is None or elapsed > self.longest.elapsed_s:
            self.longest = PhaseSample(
                self.current.lane,
                self.current.phase,
                self.current.shard_id,
                self.current.events,
                self.current.candidates,
                elapsed,
            )
        if elapsed >= STALL_LOG_SECONDS:
            LOGGER.warning(
                "event_loop_stall lane=%s phase=%s shard=%s elapsed_ms=%s events=%s candidates=%s",
                self.current.lane,
                self.current.phase,
                self.current.shard_id,
                int(elapsed * 1000),
                self.current.events,
                self.current.candidates,
            )
        return elapsed

    def restart_slice(self) -> None:
        self._slice_started = time.perf_counter()
        self._slice_open = True

    def resume(self, sample: PhaseSample) -> None:
        """Reopen ``sample``'s slice. Another lane may have marked while we awaited."""

        self.current = sample
        self.restart_slice()

    def record_subphase(
        self,
        lane: str,
        subphase: str,
        elapsed_s: float,
        *,
        events: int = 0,
        candidates: int = 0,
    ) -> None:
        stat = self.subphases.setdefault((lane, subphase), SubphaseStat())
        _accumulate(stat, elapsed_s, events, candidates)
        if elapsed_s >= STALL_LOG_SECONDS:
            LOGGER.warning(
                "event_loop_sync_subphase lane=%s subphase=%s elapsed_ms=%s events=%s candidates=%s",
                lane,
                subphase,
                int(elapsed_s * 1000),
                int(events),
                int(candidates),
            )

    def record_lock_wait(self, name: str, waited_s: float) -> None:
        stat = self.lock_waits.setdefault(name, SubphaseStat())
        _accumulate(stat, waited_s, 0, 0)
        if waited_s >= STALL_LOG_SECONDS:
            LOGGER.warning(
                "event_loop_lock_wait lock=%s waited_ms=%s lane=%s phase=%s",
                name,
                int(waited_s * 1000),
                self.current.lane,
                self.current.phase,
            )

    def on_gc(self, phase: str, info: dict[str, object]) -> None:
        """``gc.callbacks`` hook: a collection on the loop thread blocks every coroutine."""

        if not _on_event_loop_thread():
            return
        if phase == "start":
            self._gc_started = time.perf_counter()
            return
        started, self._gc_started = self._gc_started, None
        if started is None:
            return
        elapsed = time.perf_counter() - started
        generation = f"gen{info.get('generation', '?')}"
        _accumulate(self.gc_pauses.setdefault(generation, SubphaseStat()), elapsed, 0, 0)
        if elapsed >= STALL_LOG_SECONDS:
            LOGGER.warning(
                "event_loop_gc_pause generation=%s elapsed_ms=%s lane=%s phase=%s",
                generation,
                int(elapsed * 1000),
                self.current.lane,
                self.current.phase,
            )

    def note_offloop_persistence(self) -> None:
        self.offloop_persistence += 1

    def current_open_sync_ms(self) -> int:
        """Elapsed milliseconds of the open synchronous slice, if one is running."""

        if not self._slice_open or self.current.phase == "idle":
            return 0
        elapsed = max(0.0, time.perf_counter() - self._slice_started)
        return int(elapsed * 1000)


def _accumulate(stat: SubphaseStat, elapsed_s: float, events: int, candidates: int) -> None:
    stat.count += 1
    stat.total_s += elapsed_s
    if elapsed_s >= stat.max_s:
        stat.max_s = elapsed_s
        stat.max_events = int(events)
        stat.max_candidates = int(candidates)


LOOP_ACTIVITY = LoopActivity()


def install_gc_pause_monitor() -> None:
    """Record event-loop-thread GC pauses. Idempotent."""

    if LOOP_ACTIVITY.on_gc not in gc.callbacks:
        gc.callbacks.append(LOOP_ACTIVITY.on_gc)


@contextmanager
def sync_subphase(
    lane: str,
    subphase: str,
    *,
    events: int = 0,
    candidates: int = 0,
) -> Iterator[None]:
    """Time one await-free region. Aggregated per (lane, subphase); logs >=0.25s.

    Off the event-loop thread this is a no-op: threadpool work does not block
    the loop unless the loop waits for its lock, which ``TimedRLock`` reports.
    """

    if not _on_event_loop_thread():
        yield
        return
    started = time.perf_counter()
    try:
        yield
    finally:
        LOOP_ACTIVITY.record_subphase(
            lane,
            subphase,
            time.perf_counter() - started,
            events=events,
            candidates=candidates,
        )


class TimedRLock:
    """``threading.RLock`` that records event-loop-thread wait time.

    The uncontended path is one non-blocking acquire. Only a contended acquire
    on the event-loop thread is timed.
    """

    def __init__(self, name: str) -> None:
        self.name = name
        self._lock = threading.RLock()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        if self._lock.acquire(False):
            return True
        if not blocking:
            return False
        started = time.perf_counter()
        acquired = self._lock.acquire(True, timeout)
        if _on_event_loop_thread():
            LOOP_ACTIVITY.record_lock_wait(self.name, time.perf_counter() - started)
        return acquired

    def release(self) -> None:
        self._lock.release()

    def __enter__(self) -> TimedRLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()


def mark_loop_phase(
    *,
    lane: str,
    phase: str,
    shard_id: str | None = None,
    events: int = 0,
    candidates: int = 0,
    inherit_shard: bool = False,
) -> None:
    LOOP_ACTIVITY.mark(
        lane=lane,
        phase=phase,
        shard_id=shard_id,
        events=events,
        candidates=candidates,
        inherit_shard=inherit_shard,
    )


def close_loop_slice() -> float:
    return LOOP_ACTIVITY.note_yield()


def reset_loop_activity() -> None:
    LOOP_ACTIVITY.reset()


def loop_activity_snapshot() -> dict[str, object]:
    longest = LOOP_ACTIVITY.longest
    return {
        "event_loop_longest_sync_ms": 0 if longest is None else int(longest.elapsed_s * 1000),
        "event_loop_longest_sync_phase": None if longest is None else longest.phase,
        "event_loop_longest_sync_lane": None if longest is None else longest.lane,
        "event_loop_longest_sync_shard": None if longest is None else longest.shard_id,
        "event_loop_longest_sync_events": 0 if longest is None else longest.events,
        "event_loop_longest_sync_candidates": 0 if longest is None else longest.candidates,
        "event_loop_offloop_persistence": LOOP_ACTIVITY.offloop_persistence,
    }


def loop_subphase_snapshot() -> dict[str, object]:
    """Aggregated await-free subphase timings and event-loop lock waits."""

    return {
        "subphases": _subphase_report(),
        "lock_waits": {
            name: _stat_dict(stat) for name, stat in LOOP_ACTIVITY.lock_waits.items()
        },
        "gc_pauses": {
            name: _stat_dict(stat) for name, stat in LOOP_ACTIVITY.gc_pauses.items()
        },
    }


def _worst_label(items: dict[object, SubphaseStat]) -> str | None:
    if not items:
        return None
    key, stat = max(items.items(), key=lambda item: item[1].max_s)
    name = "/".join(key) if isinstance(key, tuple) else str(key)
    return f"{name}:{int(stat.max_s * 1000)}ms"


def _stat_dict(stat: SubphaseStat) -> dict[str, int]:
    return {
        "count": stat.count,
        "total_ms": int(stat.total_s * 1000),
        "max_ms": int(stat.max_s * 1000),
        "max_events": stat.max_events,
        "max_candidates": stat.max_candidates,
    }


def _subphase_report() -> list[dict[str, object]]:
    ranked = sorted(
        list(LOOP_ACTIVITY.subphases.items()), key=lambda item: item[1].max_s, reverse=True
    )[:_SUBPHASE_REPORT_LIMIT]
    return [
        {"lane": lane, "subphase": subphase, **_stat_dict(stat)}
        for (lane, subphase), stat in ranked
    ]


def loop_activity_diagnostic_snapshot() -> dict[str, object]:
    """Current open slice plus the longest closed slice from this process.

    Crash logs use this view. Scanner partition logs keep loop_activity_snapshot().
    """

    current = LOOP_ACTIVITY.current
    snapshot = loop_activity_snapshot()
    snapshot.update(
        {
            "event_loop_current_lane": current.lane,
            "event_loop_current_phase": current.phase,
            "event_loop_current_shard": current.shard_id,
            "event_loop_current_events": current.events,
            "event_loop_current_candidates": current.candidates,
            "event_loop_current_open_sync_ms": LOOP_ACTIVITY.current_open_sync_ms(),
            "event_loop_worst_subphase": _worst_label(dict(LOOP_ACTIVITY.subphases)),
            "event_loop_worst_lock_wait": _worst_label(dict(LOOP_ACTIVITY.lock_waits)),
            "event_loop_worst_gc_pause": _worst_label(dict(LOOP_ACTIVITY.gc_pauses)),
        }
    )
    return snapshot


async def yield_event_loop() -> None:
    """Give cheap API handlers a turn, then resume the same phase."""

    sample = LOOP_ACTIVITY.current
    LOOP_ACTIVITY.note_yield()
    await asyncio.sleep(0)
    LOOP_ACTIVITY.resume(sample)
