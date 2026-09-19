"""In-process scanner observability. Consumers, never scheduler authority.

Phase 5: audit/history/UI projection/health read-models sit behind the
price-engine critical path. This module is process-memory only. It is not a
durable work queue and must not decide what to price next.

PAPER / read-only. Fixture/demo clocks in tests; no live HTTP.
"""

from __future__ import annotations

import asyncio
import logging
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

    Emit never waits for the consumer. Overflow drops the oldest event and
    records honest lag. Blocking handlers run in a worker thread so they cannot
    stall the event loop, the next price item, or ``/health``.
    """

    maxsize: int = 256
    lag: int = 0
    dropped: int = 0
    last_error: str | None = None
    _pending: set[asyncio.Task[Any]] = field(default_factory=set)

    def emit(self, fn: Callable[[], Any]) -> None:
        """Schedule ``fn`` behind the critical path. Never await it here."""

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            try:
                fn()
            except Exception as exc:
                self.last_error = str(exc)
                LOGGER.exception("observability consumer failed without a running loop")
            return
        if len(self._pending) >= self.maxsize:
            self.dropped += 1
            self.lag = len(self._pending)
            LOGGER.warning("observability sink saturated; dropping oldest consumer")
            oldest = next(iter(self._pending), None)
            if oldest is not None and not oldest.done():
                oldest.cancel()
        task = loop.create_task(self._run(fn), name="scanner-observability")
        self._pending.add(task)
        task.add_done_callback(self._done)
        self.lag = len(self._pending)

    async def _run(self, fn: Callable[[], Any]) -> None:
        try:
            outcome = await asyncio.to_thread(fn)
            if isawaitable(outcome):
                await outcome
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.last_error = str(exc)
            LOGGER.exception("observability consumer failed")

    def _done(self, task: asyncio.Task[Any]) -> None:
        self._pending.discard(task)
        self.lag = len(self._pending)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            self.last_error = str(exc)

    async def drain(self) -> None:
        pending = list(self._pending)
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)

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
