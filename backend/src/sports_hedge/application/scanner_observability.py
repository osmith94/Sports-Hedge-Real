"""In-process scanner observability. Consumers, never scheduler authority.

Phase 5: audit/history/UI projection/health read-models sit behind the
price-engine critical path. This module is process-memory only. It is not a
durable work queue and must not decide what to price next.

Accepted work is a bounded in-memory queue plus at most one running
``to_thread`` callback. Overflow drops a **not-yet-started** event. Already
running worker threads are never cancelled, and ``drain()`` returns only after
every accepted callback has actually finished.

PAPER / read-only. Fixture/demo clocks in tests; no live HTTP.
"""

from __future__ import annotations

import asyncio
import logging
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from inspect import isawaitable
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


@dataclass
class ScannerObservabilitySink:
    """Bounded in-process fan-out for non-critical projection/audit work.

    Emit never waits for the consumer. Overflow drops the oldest **queued**
    event before it starts a worker thread. A worker task runs accepted
    callbacks via ``asyncio.to_thread`` until the queue is empty, then exits;
    the next emit starts another worker. Running callbacks are never cancelled
    to enforce maxsize. Pricing/capture must never await ``drain()`` on the
    critical path.
    """

    maxsize: int = 256
    lag: int = 0
    dropped: int = 0
    last_error: str | None = None
    _queue: deque[Callable[[], Any]] = field(default_factory=deque)
    _in_flight: int = 0
    _closed: bool = False
    _loop: asyncio.AbstractEventLoop | None = None
    _worker: asyncio.Task[Any] | None = None
    _idle: asyncio.Event | None = None

    def emit(self, fn: Callable[[], Any]) -> None:
        """Schedule ``fn`` behind the critical path. Never await it here."""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            if self._closed:
                self.dropped += 1
                self._refresh_lag()
                return
            try:
                fn()
            except Exception as exc:
                self.last_error = str(exc)
                LOGGER.exception("observability consumer failed without a running loop")
            return
        if self._closed:
            self.dropped += 1
            self._refresh_lag()
            LOGGER.warning("observability sink closed; dropping consumer")
            return
        if self.maxsize > 0 and len(self._queue) >= self.maxsize:
            self._queue.popleft()
            self.dropped += 1
            LOGGER.warning("observability sink saturated; dropping oldest queued consumer")
        self._queue.append(fn)
        self._ensure_idle_event(loop)
        self._mark_busy()
        self._ensure_worker(loop)
        self._refresh_lag()

    def _ensure_idle_event(self, loop: asyncio.AbstractEventLoop) -> None:
        if self._loop is not loop or self._idle is None:
            self._loop = loop
            self._idle = asyncio.Event()
            if not self._queue and not self._in_flight:
                self._idle.set()

    def _ensure_worker(self, loop: asyncio.AbstractEventLoop) -> None:
        self._ensure_idle_event(loop)
        if self._worker is None or self._worker.done():
            self._worker = loop.create_task(
                self._run_queued(),
                name="scanner-observability-worker",
            )

    def _mark_busy(self) -> None:
        if self._idle is not None and self._idle.is_set():
            self._idle.clear()

    def _maybe_idle(self) -> None:
        if self._queue or self._in_flight:
            return
        if self._idle is not None and not self._idle.is_set():
            self._idle.set()

    def _refresh_lag(self) -> None:
        self.lag = len(self._queue) + self._in_flight

    def _pop_queued(self) -> Callable[[], Any] | None:
        if not self._queue:
            return None
        fn = self._queue.popleft()
        self._refresh_lag()
        return fn

    async def _run_queued(self) -> None:
        """Run accepted callbacks until the queue is empty. Do not wait forever."""

        try:
            while True:
                fn = self._pop_queued()
                if fn is None:
                    await asyncio.sleep(0)
                    fn = self._pop_queued()
                    if fn is None:
                        return
                self._in_flight += 1
                self._mark_busy()
                self._refresh_lag()
                try:
                    outcome = await asyncio.to_thread(fn)
                    if isawaitable(outcome):
                        await outcome
                except Exception as exc:
                    self.last_error = str(exc)
                    LOGGER.exception("observability consumer failed")
                finally:
                    self._in_flight -= 1
                    self._refresh_lag()
        finally:
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                self._worker = None
                self._maybe_idle()
                return
            if self._queue and not self._closed:
                self._worker = loop.create_task(
                    self._run_queued(),
                    name="scanner-observability-worker",
                )
            else:
                self._worker = None
                self._maybe_idle()

    async def drain(self) -> None:
        """Return only after every accepted queued/running callback has finished."""

        while self._queue or self._in_flight or (
            self._worker is not None and not self._worker.done()
        ):
            if self._idle is not None:
                await self._idle.wait()
            else:
                worker = self._worker
                if worker is None or worker.done():
                    return
                await worker
            await asyncio.sleep(0)

    def reset(self) -> None:
        """Drop not-yet-started work. Do not cancel a running ``to_thread``."""

        self._queue.clear()
        self.dropped = 0
        self.last_error = None
        self._closed = False
        self._refresh_lag()
        self._maybe_idle()

    async def shutdown(self) -> None:
        """Stop accepting work, then finish accepted callbacks."""

        self._closed = True
        await self.drain()
        self._worker = None

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
