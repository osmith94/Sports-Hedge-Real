"""Shared provider-access layer for concurrent HOT and UNIVERSE workers.

HOT may receive the next free slot when capacity is saturated. That priority
must not cancel or reset UNIVERSE. After a bounded run of HOT grants while
UNIVERSE is waiting, the next slot goes to UNIVERSE so a busy HOT roster
cannot starve discovery.

Local wait / HOT-priority deferral is not a venue outage.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import IntEnum
from typing import Any

from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.models import VenueName

DEFAULT_PROVIDER_CONCURRENCY = {
    VenueName.MATCHBOOK: 4,
    VenueName.POLYMARKET: 8,
    VenueName.KALSHI: 4,
}

HEALTH_OK = "ok"
HEALTH_WAITING = "waiting"
HEALTH_DEFERRED = "deferred"
HEALTH_RATE_LIMITED = "rate_limited"
HEALTH_DISCOVERY_TIMEOUT = "discovery_timeout"
HEALTH_MARKET_TIMEOUT = "market_timeout"
HEALTH_TIMEOUT = "timeout"
HEALTH_UNAVAILABLE = "unavailable"
HEALTH_AUTH_FAILURE = "auth_failure"
HEALTH_CANCELLED = "cancelled"
HEALTH_DEGRADED = "degraded"
HEALTH_DISABLED = "disabled"

DEFAULT_STARVATION_HOT_GRANTS = 8


class ProviderPriority(IntEnum):
    HOT = 0
    UNIVERSE = 1
    MANUAL = 1


def priority_for_lane(lane: ScanLane | str | None) -> ProviderPriority:
    text = str(lane or "").strip().casefold()
    if text == ScanLane.HOT.value:
        return ProviderPriority.HOT
    return ProviderPriority.UNIVERSE


@dataclass(slots=True)
class _Waiter:
    priority: ProviderPriority
    seq: int
    lane: str
    stage: str
    event: asyncio.Event = field(default_factory=asyncio.Event)
    granted: bool = False
    cancelled: bool = False
    reason: str = HEALTH_WAITING


def _consume_task_result(task: asyncio.Task[Any]) -> None:
    """Retrieve a finished task result so exceptions are not left unconsumed."""

    if not task.done():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        return


@dataclass(slots=True)
class ProviderLease:
    """Capacity unit that stays occupied until the underlying call finishes.

    ``async with acquire()`` must not make the slot reusable while the HTTP
    work it protected is still alive after timeout/cancel. Transfer ownership
    to that live task with ``hold_until_task``.
    """

    _layer: ProviderAccessLayer | None
    _venue: VenueName | None
    _waiter: _Waiter | None
    _loop: asyncio.AbstractEventLoop | None
    _owned: bool = True

    @classmethod
    def unbound(cls) -> ProviderLease:
        return cls(_layer=None, _venue=None, _waiter=None, _loop=None, _owned=False)

    def hold_until_task(self, task: asyncio.Task[Any]) -> bool:
        if not self._owned or self._layer is None or self._venue is None or self._waiter is None:
            return False
        if task.done():
            return False
        self._owned = False
        loop = self._loop
        layer = self._layer
        venue = self._venue
        waiter = self._waiter

        def _done(done: asyncio.Task[Any]) -> None:
            _consume_task_result(done)
            if loop is None:
                return
            try:
                release_task = loop.create_task(
                    layer._release(venue, waiter),
                    name="provider-lease-release",
                )
            except RuntimeError:
                return
            release_task.add_done_callback(_consume_task_result)

        task.add_done_callback(_done)
        return True

    async def release(self) -> None:
        if not self._owned:
            return
        self._owned = False
        if self._layer is None or self._venue is None or self._waiter is None:
            return
        await self._layer._release(self._venue, self._waiter)


@dataclass(slots=True)
class ProviderAccessSnapshot:
    inflight: dict[str, int]
    waiting: dict[str, int]
    waiting_by_lane: dict[str, dict[str, int]]
    hot_grants_since_universe: dict[str, int]
    limits: dict[str, int]

    def as_dict(self) -> dict[str, Any]:
        return {
            "inflight": dict(self.inflight),
            "waiting": dict(self.waiting),
            "waiting_by_lane": {
                lane: dict(counts) for lane, counts in self.waiting_by_lane.items()
            },
            "hot_grants_since_universe": dict(self.hot_grants_since_universe),
            "limits": dict(self.limits),
        }


class ProviderAccessLayer:
    """Process-shared per-venue concurrency with HOT priority and anti-starvation."""

    def __init__(
        self,
        limits: Mapping[VenueName, int] | None = None,
        *,
        starvation_hot_grants: int = DEFAULT_STARVATION_HOT_GRANTS,
    ) -> None:
        resolved = limits or DEFAULT_PROVIDER_CONCURRENCY
        self._limits = {
            venue: max(1, int(resolved.get(venue, DEFAULT_PROVIDER_CONCURRENCY[venue])))
            for venue in DEFAULT_PROVIDER_CONCURRENCY
        }
        self._starvation_hot_grants = max(1, int(starvation_hot_grants))
        self._in_use = {venue: 0 for venue in self._limits}
        self._waiters: dict[VenueName, list[_Waiter]] = {venue: [] for venue in self._limits}
        self._hot_grants_since_universe = {venue: 0 for venue in self._limits}
        self._seq = 0
        self._cond = asyncio.Condition()
        self._peak_inflight = {venue: 0 for venue in self._limits}

    @property
    def limits(self) -> dict[VenueName, int]:
        return dict(self._limits)

    def snapshot(self) -> ProviderAccessSnapshot:
        waiting_by_lane = {
            "hot": {venue.value: 0 for venue in self._limits},
            "universe": {venue.value: 0 for venue in self._limits},
        }
        waiting = {venue.value: 0 for venue in self._limits}
        for venue, waiters in self._waiters.items():
            pending = [item for item in waiters if not item.granted and not item.cancelled]
            waiting[venue.value] = len(pending)
            for waiter in pending:
                lane = "hot" if waiter.priority is ProviderPriority.HOT else "universe"
                waiting_by_lane[lane][venue.value] += 1
        return ProviderAccessSnapshot(
            inflight={venue.value: count for venue, count in self._in_use.items()},
            waiting=waiting,
            waiting_by_lane=waiting_by_lane,
            hot_grants_since_universe={
                venue.value: count for venue, count in self._hot_grants_since_universe.items()
            },
            limits={venue.value: limit for venue, limit in self._limits.items()},
        )

    def lane_wait_reason(self, lane: ScanLane | str | None) -> str | None:
        want_hot = str(lane or "").strip().casefold() == ScanLane.HOT.value
        for waiters in self._waiters.values():
            for waiter in waiters:
                if waiter.granted or waiter.cancelled:
                    continue
                is_hot = waiter.priority is ProviderPriority.HOT
                if is_hot == want_hot:
                    return waiter.reason
        return None

    def venue_wait_reason(self, venue: VenueName, *, lane: ScanLane | str | None = None) -> str | None:
        want_hot = None if lane is None else str(lane).strip().casefold() == ScanLane.HOT.value
        for waiter in self._waiters.get(venue, []):
            if waiter.granted or waiter.cancelled:
                continue
            if want_hot is None:
                return waiter.reason
            if (waiter.priority is ProviderPriority.HOT) == want_hot:
                return waiter.reason
        return None

    @asynccontextmanager
    async def acquire(
        self,
        venue: VenueName,
        *,
        lane: ScanLane | str | None = None,
        stage: str = "provider",
    ) -> AsyncIterator[ProviderLease]:
        if venue not in self._limits:
            yield ProviderLease.unbound()
            return
        waiter = await self._enqueue(venue, lane=lane, stage=stage)
        lease = ProviderLease(
            _layer=self,
            _venue=venue,
            _waiter=waiter,
            _loop=asyncio.get_running_loop(),
        )
        try:
            await waiter.event.wait()
            if waiter.cancelled:
                raise asyncio.CancelledError
            yield lease
        finally:
            await lease.release()

    async def _enqueue(
        self,
        venue: VenueName,
        *,
        lane: ScanLane | str | None,
        stage: str,
    ) -> _Waiter:
        async with self._cond:
            self._seq += 1
            priority = priority_for_lane(lane)
            reason = HEALTH_WAITING
            if priority is ProviderPriority.UNIVERSE and self._hot_ahead(venue):
                reason = HEALTH_DEFERRED
            waiter = _Waiter(
                priority=priority,
                seq=self._seq,
                lane=ScanLane.HOT.value if priority is ProviderPriority.HOT else ScanLane.UNIVERSE.value,
                stage=stage,
                reason=reason,
            )
            self._waiters[venue].append(waiter)
            self._pump(venue)
            return waiter

    async def _release(self, venue: VenueName, waiter: _Waiter) -> None:
        async with self._cond:
            if waiter in self._waiters[venue]:
                self._waiters[venue].remove(waiter)
            if waiter.granted:
                self._in_use[venue] = max(0, self._in_use[venue] - 1)
            else:
                waiter.cancelled = True
            self._pump(venue)
            self._cond.notify_all()

    def _hot_ahead(self, venue: VenueName) -> bool:
        return any(
            item.priority is ProviderPriority.HOT and not item.granted and not item.cancelled
            for item in self._waiters[venue]
        )

    def _universe_waiting(self, venue: VenueName) -> bool:
        return any(
            item.priority is not ProviderPriority.HOT and not item.granted and not item.cancelled
            for item in self._waiters[venue]
        )

    def _pick(self, venue: VenueName) -> _Waiter | None:
        pending = [item for item in self._waiters[venue] if not item.granted and not item.cancelled]
        if not pending:
            return None
        starve = (
            self._universe_waiting(venue)
            and self._hot_grants_since_universe[venue] >= self._starvation_hot_grants
        )

        def sort_key(item: _Waiter) -> tuple[int, int]:
            effective = int(item.priority)
            if starve and item.priority is not ProviderPriority.HOT:
                effective = -1
            return (effective, item.seq)

        return min(pending, key=sort_key)

    def _pump(self, venue: VenueName) -> None:
        while self._in_use[venue] < self._limits[venue]:
            nxt = self._pick(venue)
            if nxt is None:
                return
            nxt.granted = True
            self._in_use[venue] += 1
            self._peak_inflight[venue] = max(self._peak_inflight[venue], self._in_use[venue])
            if nxt.priority is ProviderPriority.HOT:
                self._hot_grants_since_universe[venue] += 1
            else:
                self._hot_grants_since_universe[venue] = 0
            nxt.event.set()


def limits_from_settings(settings: Settings | None = None) -> dict[VenueName, int]:
    resolved = settings or get_settings()
    return {
        VenueName.MATCHBOOK: resolved.paper_scan_matchbook_concurrency,
        VenueName.POLYMARKET: resolved.paper_scan_polymarket_concurrency,
        VenueName.KALSHI: resolved.paper_scan_kalshi_concurrency,
    }


_SHARED: ProviderAccessLayer | None = None


def get_shared_provider_access(settings: Settings | None = None) -> ProviderAccessLayer:
    global _SHARED
    if _SHARED is None:
        resolved = settings or get_settings()
        _SHARED = ProviderAccessLayer(
            limits_from_settings(resolved),
            starvation_hot_grants=resolved.paper_provider_hot_starvation_grants,
        )
    return _SHARED


def set_shared_provider_access(layer: ProviderAccessLayer | None) -> None:
    global _SHARED
    _SHARED = layer


def reset_shared_provider_access() -> None:
    set_shared_provider_access(None)


def operation_health_from_stage(stage: str, *, timed_out: bool) -> str:
    """Map a collector stage timeout to a truthful operation-level health code."""

    if not timed_out:
        return HEALTH_OK
    if stage == "list_events":
        return HEALTH_DISCOVERY_TIMEOUT
    if stage in {"list_markets", "get_series", "get_market", "get_contract_terms", "get_order_book", "order_book"}:
        return HEALTH_MARKET_TIMEOUT
    return HEALTH_TIMEOUT


def health_is_provider_outage(value: str | None) -> bool:
    return value in {
        HEALTH_UNAVAILABLE,
        HEALTH_AUTH_FAILURE,
        HEALTH_TIMEOUT,
        HEALTH_DISCOVERY_TIMEOUT,
        HEALTH_MARKET_TIMEOUT,
    }


def merge_lane_operation_health(
    current: dict[str, Any] | None,
    *,
    venue: str,
    operation: str,
    status: str,
) -> dict[str, Any]:
    payload = {key: dict(value) if isinstance(value, dict) else value for key, value in (current or {}).items()}
    bucket = dict(payload.get(venue) or {})
    bucket[operation] = status
    payload[venue] = bucket
    return payload
