"""Attribute synchronous event-loop stretches to the worker phase that owns them.

HOT, UNIVERSE, BACKGROUND, ACTIVE TRADE and paper settlement share one asyncio
loop with cheap API handlers. A slice is open only while the marked phase is
running synchronous work. Awaiting closes it, so sleep time is not reported as
a stall. Stretches at or above the 0.25s liveness bound are logged for the
owner-live Windows probe.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass

LOGGER = logging.getLogger(__name__)

STALL_LOG_SECONDS = 0.25
_HISTORY_LIMIT = 64


@dataclass(frozen=True)
class PhaseSample:
    lane: str
    phase: str
    shard_id: str | None
    events: int
    candidates: int
    elapsed_s: float = 0.0


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

    def note_offloop_persistence(self) -> None:
        self.offloop_persistence += 1

    def current_open_sync_ms(self) -> int:
        """Elapsed milliseconds of the open synchronous slice, if one is running."""

        if not self._slice_open or self.current.phase == "idle":
            return 0
        elapsed = max(0.0, time.perf_counter() - self._slice_started)
        return int(elapsed * 1000)


LOOP_ACTIVITY = LoopActivity()


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
        }
    )
    return snapshot


async def yield_event_loop() -> None:
    """Give cheap API handlers a turn, then resume the same phase."""

    LOOP_ACTIVITY.note_yield()
    await asyncio.sleep(0)
    LOOP_ACTIVITY.restart_slice()
