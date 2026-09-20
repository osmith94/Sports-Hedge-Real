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
HEALTH_CAPACITY_SATURATED = "provider_capacity_saturated"

DEFAULT_STARVATION_HOT_GRANTS = 8
PRICE_ENGINE_BACKGROUND_LANE = "background"
PRICE_ENGINE_ACTIVE_TRADE_LANE = "active_trade"
PRICE_ENGINE_SETTLEMENT_LANE = "settlement"


class ProviderPriority(IntEnum):
    ACTIVE_TRADE = 0
    HOT = 1
    UNIVERSE = 2
    MANUAL = 2
    BACKGROUND = 3


def priority_for_lane(lane: ScanLane | str | None) -> ProviderPriority:
    text = str(lane or "").strip().casefold()
    if text in {PRICE_ENGINE_ACTIVE_TRADE_LANE, "active-trade"}:
        return ProviderPriority.ACTIVE_TRADE
    if text == ScanLane.HOT.value:
        return ProviderPriority.HOT
    if text in {PRICE_ENGINE_BACKGROUND_LANE, PRICE_ENGINE_SETTLEMENT_LANE}:
        return ProviderPriority.BACKGROUND
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
        self._active_grants_since_hot = {venue: 0 for venue in self._limits}
        self._seq = 0
        self._cond = asyncio.Condition()
        self._peak_inflight = {venue: 0 for venue in self._limits}

    @property
    def limits(self) -> dict[VenueName, int]:
        return dict(self._limits)

    def snapshot(self) -> ProviderAccessSnapshot:
        waiting_by_lane = {
            "active_trade": {venue.value: 0 for venue in self._limits},
            "hot": {venue.value: 0 for venue in self._limits},
            "universe": {venue.value: 0 for venue in self._limits},
            "background": {venue.value: 0 for venue in self._limits},
        }
        waiting = {venue.value: 0 for venue in self._limits}
        for venue, waiters in self._waiters.items():
            pending = [item for item in waiters if not item.granted and not item.cancelled]
            waiting[venue.value] = len(pending)
            for waiter in pending:
                if waiter.priority is ProviderPriority.ACTIVE_TRADE:
                    lane = "active_trade"
                elif waiter.priority is ProviderPriority.HOT:
                    lane = "hot"
                elif waiter.priority is ProviderPriority.BACKGROUND:
                    lane = "background"
                else:
                    lane = "universe"
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
            waiter = self._make_waiter(venue, lane=lane, stage=stage)
            self._waiters[venue].append(waiter)
            self._pump(venue)
            return waiter

    def _make_waiter(
        self,
        venue: VenueName,
        *,
        lane: ScanLane | str | None,
        stage: str,
    ) -> _Waiter:
        self._seq += 1
        priority = priority_for_lane(lane)
        reason = HEALTH_WAITING
        if (
            priority not in {ProviderPriority.HOT, ProviderPriority.ACTIVE_TRADE}
            and self._hot_ahead(venue)
        ):
            reason = HEALTH_DEFERRED
        if priority is ProviderPriority.ACTIVE_TRADE:
            waiter_lane = PRICE_ENGINE_ACTIVE_TRADE_LANE
        elif priority is ProviderPriority.HOT:
            waiter_lane = ScanLane.HOT.value
        elif priority is ProviderPriority.BACKGROUND:
            waiter_lane = PRICE_ENGINE_BACKGROUND_LANE
        else:
            waiter_lane = ScanLane.UNIVERSE.value
        return _Waiter(
            priority=priority,
            seq=self._seq,
            lane=waiter_lane,
            stage=stage,
            reason=reason,
        )

    @asynccontextmanager
    async def acquire_wait(
        self,
        venue: VenueName,
        *,
        lane: ScanLane | str | None = None,
        stage: str = "provider",
        timeout: float | None = None,
    ) -> AsyncIterator[ProviderLease | None]:
        """Wait locally for a free slot, or return None when the wait expires.

        Ordinary in-slice work should flow through capacity as soon as a slot
        is released. A wait that expires while every slot is still occupied is
        ``provider_capacity_saturated`` / deferred, not a scan-budget failure
        and not a venue outage. This does not raise the 4/4 caps.
        """

        if venue not in self._limits:
            yield ProviderLease.unbound()
            return
        if timeout is not None and timeout <= 0:
            yield None
            return
        waiter = await self._enqueue(venue, lane=lane, stage=stage)
        lease = ProviderLease(
            _layer=self,
            _venue=venue,
            _waiter=waiter,
            _loop=asyncio.get_running_loop(),
        )
        try:
            try:
                if timeout is None:
                    await waiter.event.wait()
                else:
                    await asyncio.wait_for(waiter.event.wait(), timeout=timeout)
            except TimeoutError:
                yield None
                return
            if waiter.cancelled:
                raise asyncio.CancelledError
            yield lease
        finally:
            await lease.release()

    @asynccontextmanager
    async def try_acquire(
        self,
        venue: VenueName,
        *,
        lane: ScanLane | str | None = None,
        stage: str = "provider",
    ) -> AsyncIterator[ProviderLease | None]:
        """Grant a free slot immediately, or return None without queueing.

        Immediate non-blocking admission for callers that cannot wait. The
        price engine uses ``acquire_wait`` so a busy-but-completing roster can
        flow through freed slots in the same slice.
        """

        if venue not in self._limits:
            yield ProviderLease.unbound()
            return
        lease: ProviderLease | None = None
        async with self._cond:
            waiter = self._make_waiter(venue, lane=lane, stage=stage)
            self._waiters[venue].append(waiter)
            picked = self._pick(venue)
            if picked is waiter and self._in_use[venue] < self._limits[venue]:
                waiter.granted = True
                waiter.event.set()
                self._in_use[venue] += 1
                self._peak_inflight[venue] = max(self._peak_inflight[venue], self._in_use[venue])
                self._count_high_priority_grant(venue, waiter.priority)
                lease = ProviderLease(
                    _layer=self,
                    _venue=venue,
                    _waiter=waiter,
                    _loop=asyncio.get_running_loop(),
                )
            else:
                self._waiters[venue].remove(waiter)
        if lease is None:
            yield None
            return
        try:
            yield lease
        finally:
            await lease.release()

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
            item.priority in {ProviderPriority.HOT, ProviderPriority.ACTIVE_TRADE}
            and not item.granted
            and not item.cancelled
            for item in self._waiters[venue]
        )

    def _hot_waiting(self, venue: VenueName) -> bool:
        return any(
            item.priority is ProviderPriority.HOT and not item.granted and not item.cancelled
            for item in self._waiters[venue]
        )

    def _count_high_priority_grant(self, venue: VenueName, priority: ProviderPriority) -> None:
        if priority is ProviderPriority.HOT:
            self._hot_grants_since_universe[venue] += 1
            self._active_grants_since_hot[venue] = 0
        elif priority is ProviderPriority.ACTIVE_TRADE:
            self._hot_grants_since_universe[venue] += 1
            self._active_grants_since_hot[venue] += 1
        else:
            self._hot_grants_since_universe[venue] = 0
            self._active_grants_since_hot[venue] = 0

    def available_slots(self, venue: VenueName) -> int:
        if venue not in self._limits:
            return 0
        return max(0, self._limits[venue] - self._in_use[venue])

    def venue_saturated(self, venue: VenueName) -> bool:
        return venue in self._limits and self.available_slots(venue) <= 0

    def _universe_waiting(self, venue: VenueName) -> bool:
        return any(
            item.priority is ProviderPriority.UNIVERSE
            and not item.granted
            and not item.cancelled
            for item in self._waiters[venue]
        )

    def _pick(self, venue: VenueName) -> _Waiter | None:
        pending = [item for item in self._waiters[venue] if not item.granted and not item.cancelled]
        if not pending:
            return None
        starve_universe = (
            self._universe_waiting(venue)
            and self._hot_grants_since_universe[venue] >= self._starvation_hot_grants
        )
        starve_hot = (
            self._hot_waiting(venue)
            and self._active_grants_since_hot[venue] >= self._starvation_hot_grants
        )

        def sort_key(item: _Waiter) -> tuple[int, int]:
            effective = int(item.priority)
            if starve_universe and item.priority is ProviderPriority.UNIVERSE:
                effective = -2
            elif starve_hot and item.priority is ProviderPriority.HOT:
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
            self._count_high_priority_grant(venue, nxt.priority)
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
