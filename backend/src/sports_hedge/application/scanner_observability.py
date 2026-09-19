"""In-process scanner observability. Consumers, never scheduler authority.

Phase 5: audit/history/UI projection/health read-models sit behind the
price-engine critical path. This module is process-memory only. It is not a
durable work queue and must not decide what to price next.

Accepted work is a bounded in-memory queue plus a persistent daemon thread.
Overflow drops a **not-yet-started** queued event. Running callbacks are never
cancelled, including via ``asyncio.Task.cancel`` / ``asyncio.to_thread``.
``drain()`` returns only after every accepted callback has actually finished.

PAPER / read-only. Fixture/demo clocks in tests; no live HTTP.
"""

from __future__ import annotations

import asyncio
import logging
import threading
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.application.lane_venues import is_provider_health_failure
from sports_hedge.application.provider_access import (
    HEALTH_CAPACITY_SATURATED,
    HEALTH_DEFERRED,
    HEALTH_MARKET_TIMEOUT,
    HEALTH_OK,
)

LOGGER = logging.getLogger(__name__)

PRICE_ENGINE_EVALUATED_DEFINITION = (
    "Items currently in status evaluated whose last_priced_at is still inside "
    "this tier's cadence window. Counted from process-memory engine items, "
    "not collector leftovers."
)

_CAPACITY_HEALTH = frozenset(
    {HEALTH_CAPACITY_SATURATED, HEALTH_DEFERRED, "deferred"}
)


class PriceEngineTierStatus(BaseModel):
    """HOT or BACKGROUND counters from current process-memory engine truth."""

    working_set: int = 0
    due: int = 0
    queued: int = 0
    in_flight: int = 0
    evaluated: int = 0
    evaluated_definition: str = PRICE_ENGINE_EVALUATED_DEFINITION
    retry_wait: int = 0
    deferred: int = 0
    provider_capacity_saturated: int = 0
    not_started_this_cadence: int = 0
    revalidation_needed: int = 0
    persist_failures: int = 0
    last_error: str | None = None
    operation_health: dict[str, Any] = Field(default_factory=dict)
    venue_health: dict[str, str] = Field(default_factory=dict)


class PriceEnginePublicStatus(BaseModel):
    """Truthful HOT vs BACKGROUND status. Never a leftover-assembly reconstruction."""

    hot: PriceEngineTierStatus = Field(default_factory=PriceEngineTierStatus)
    background: PriceEngineTierStatus = Field(default_factory=PriceEngineTierStatus)
    durable_queue: bool = False
    observability_lag: int = 0
    observability_dropped: int = 0
    observability_error: str | None = None


@dataclass(frozen=True)
class _ObservabilityJob:
    """One accepted callback tagged with the sink generation at enqueue."""

    generation: int
    fn: Callable[[], Any]


@dataclass
class ScannerObservabilitySink:
    """Bounded in-process fan-out for non-critical projection/audit work.

    Emit never waits for the consumer. Overflow drops the oldest **queued**
    event before that callback starts. A persistent daemon thread runs accepted
    callbacks; there is no per-item ``asyncio.to_thread`` wrapper, so cancelling
    an asyncio waiter cannot orphan a still-running consumer. Pricing/capture
    must never await ``drain()`` on the critical path.

    ``reset()`` starts a new generation, drops not-yet-started work, and never
    cancels a backing thread. In-flight callbacks may finish, but consumers
    that capture the emit-time generation must refuse to commit into
    post-reset state.
    """

    maxsize: int = 256
    lag: int = 0
    dropped: int = 0
    last_error: str | None = None
    _queue: deque[_ObservabilityJob] = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _work: threading.Event = field(default_factory=threading.Event)
    _idle: threading.Event = field(default_factory=threading.Event)
    _in_flight: int = 0
    _closed: bool = False
    _worker: threading.Thread | None = None
    _generation: int = 0

    def __post_init__(self) -> None:
        self._idle.set()

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def emit(self, fn: Callable[[], Any]) -> None:
        """Schedule ``fn`` behind the critical path. Never await it here."""

        with self._lock:
            if self._closed:
                self.dropped += 1
                self._refresh_lag_unlocked()
                LOGGER.warning("observability sink closed; dropping consumer")
                return
            if self.maxsize > 0 and len(self._queue) >= self.maxsize:
                self._queue.popleft()
                self.dropped += 1
                LOGGER.warning("observability sink saturated; dropping oldest queued consumer")
            self._queue.append(_ObservabilityJob(generation=self._generation, fn=fn))
            self._idle.clear()
            self._refresh_lag_unlocked()
        self._ensure_worker()
        self._work.set()

    def _ensure_worker(self) -> None:
        with self._lock:
            if self._worker is not None and self._worker.is_alive():
                return
            worker = threading.Thread(
                target=self._worker_loop,
                name="scanner-observability-worker",
                daemon=True,
            )
            self._worker = worker
            worker.start()

    def _refresh_lag_unlocked(self) -> None:
        self.lag = len(self._queue) + self._in_flight

    def _maybe_idle_unlocked(self) -> None:
        if not self._queue and self._in_flight == 0:
            self._idle.set()
        else:
            self._idle.clear()

    def _pop_work(self) -> _ObservabilityJob | None:
        with self._lock:
            if self._queue:
                job = self._queue.popleft()
                self._in_flight += 1
                self._idle.clear()
                self._refresh_lag_unlocked()
                return job
            self._maybe_idle_unlocked()
            self._work.clear()
            if self._queue:
                job = self._queue.popleft()
                self._in_flight += 1
                self._idle.clear()
                self._refresh_lag_unlocked()
                return job
            return None

    def _worker_loop(self) -> None:
        """Run accepted callbacks on a real thread. Never cancelled for maxsize."""

        while True:
            job = self._pop_work()
            if job is None:
                with self._lock:
                    if self._closed and not self._queue and self._in_flight == 0:
                        self._idle.set()
                        return
                self._work.wait(timeout=0.25)
                continue
            try:
                if job.generation == self.generation:
                    job.fn()
            except Exception as exc:
                self.last_error = str(exc)
                LOGGER.exception("observability consumer failed")
            finally:
                with self._lock:
                    self._in_flight -= 1
                    self._refresh_lag_unlocked()
                    self._maybe_idle_unlocked()

    def wait_until_idle(self) -> None:
        """Block until accepted queued/running callbacks have finished."""

        while True:
            with self._lock:
                if not self._queue and self._in_flight == 0:
                    return
            self._idle.wait(timeout=0.05)

    async def drain(self) -> None:
        """Return only after every accepted queued/running callback has finished.

        Cancelling this waiter does **not** complete or orphan accepted work.
        Call ``shutdown()`` before closing repositories used by consumers.
        """

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self.wait_until_idle()
            return
        while True:
            with self._lock:
                if not self._queue and self._in_flight == 0:
                    return
            await asyncio.sleep(0.01)

    def reset(self) -> None:
        """Start a new generation and drop not-yet-started work.

        Does not interrupt a running callback and does not wait for it.
        Pricing/capture never calls this. In-flight consumers must still
        refuse to commit into post-reset current-state.
        """

        with self._lock:
            self._generation += 1
            self._queue.clear()
            self.dropped = 0
            self.last_error = None
            self._closed = False
            self._refresh_lag_unlocked()
            self._maybe_idle_unlocked()
        self._work.set()

    async def shutdown(self) -> None:
        """Stop accepting work, finish accepted callbacks, then join the worker."""

        with self._lock:
            self._closed = True
        self._work.set()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.wait_until_idle()
        else:
            try:
                await loop.run_in_executor(None, self.wait_until_idle)
            except asyncio.CancelledError:
                self.wait_until_idle()
                raise
        worker = self._worker
        if worker is not None and worker.is_alive() and worker is not threading.current_thread():
            await asyncio.sleep(0)
            worker.join(timeout=2.0)

    def snapshot(self) -> dict[str, Any]:
        return {
            "lag": self.lag,
            "dropped": self.dropped,
            "error": self.last_error,
            "durable_queue": False,
        }


def empty_price_engine_status() -> PriceEnginePublicStatus:
    return PriceEnginePublicStatus()


def venue_health_from_operation_health(
    operation_health: dict[str, Any] | None,
) -> dict[str, str]:
    """Per-venue summary from venue+operation health. Never copies one venue onto another."""

    payload: dict[str, str] = {}
    for venue, operations in (operation_health or {}).items():
        if not isinstance(operations, dict):
            continue
        statuses = [str(value) for value in operations.values()]
        if not statuses:
            continue
        failures = [item for item in statuses if is_provider_health_failure(item)]
        capacity = [item for item in statuses if item in _CAPACITY_HEALTH]
        oks = [item for item in statuses if item == HEALTH_OK]
        if failures and oks:
            payload[str(venue)] = "degraded"
        elif failures:
            payload[str(venue)] = failures[0]
        elif capacity:
            payload[str(venue)] = HEALTH_DEFERRED
        elif oks:
            payload[str(venue)] = HEALTH_OK
        else:
            payload[str(venue)] = statuses[-1]
    return payload


def record_operation_health(
    current: dict[str, Any] | None,
    *,
    venue: str,
    operation: str,
    status: str,
) -> dict[str, Any]:
    """Merge one venue+operation without touching sibling venues or the other tier."""

    payload = {
        key: dict(value) if isinstance(value, dict) else value
        for key, value in (current or {}).items()
    }
    bucket = dict(payload.get(venue) or {})
    bucket[operation] = status
    payload[venue] = bucket
    return payload


def lane_key(priority: str) -> str:
    text = str(priority or "").strip().casefold()
    if text == "hot":
        return "hot"
    return "background"


def operation_status_for_item(
    *,
    timed_out: bool,
    deferred: bool,
    saturated: bool,
) -> str:
    if deferred or saturated:
        return HEALTH_CAPACITY_SATURATED
    if timed_out:
        return HEALTH_MARKET_TIMEOUT
    return HEALTH_OK
