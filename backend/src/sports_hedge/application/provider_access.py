"""Shared provider-access layer for concurrent HOT and UNIVERSE workers.

HOT may receive the next free slot when capacity is saturated. That priority
must not cancel or reset UNIVERSE. After a bounded run of HOT grants while
UNIVERSE is waiting, the next slot goes to UNIVERSE so a busy HOT roster
cannot starve discovery.

Matchbook and Kalshi keep their physical caps (4). After startup pricing is
released, lower-priority occupancy follows HOT demand:

- no runnable HOT work: BACKGROUND and UNIVERSE may use 3 of 4, leaving one
  slot for ACTIVE even when ACTIVE is not queued;
- runnable HOT work: new lower-priority leases stop at 1 of 4 so ACTIVE and
  HOT can use the rest. HOT is not capped. ACTIVE still outranks HOT.

Already-running lower-priority HTTP calls are not cancelled when demand
changes. Anti-starvation may still grant a lower-priority waiter into a
non-protected slot; it must not spend the protected headroom.

EXECUTION CANDIDATE is Price-2 work before any position exists. It ranks
below ACTIVE TRADE and above ordinary HOT, BACKGROUND, and UNIVERSE. It may
use an otherwise-idle slot reserved for ACTIVE TRADE. It is not labeled
active_trade. If ACTIVE TRADE is waiting, that work wins the next free slot.
Candidate grants do not spend the anti-starvation budget that would promote
lower lanes ahead of an open position.

During the startup UNIVERSE barrier, HOT and BACKGROUND are already gated.
The reservation is one slot so ACTIVE cannot be locked out, and UNIVERSE may
use the other three. That is the whole startup exception. Once pricing opens,
the dynamic policy above applies.

Issue #473 adds deadline/value-aware ranking and queue metrics on top of
those slot caps. Limits are never raised here.

Local wait / HOT-priority deferral is not a venue outage.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from enum import IntEnum
from time import monotonic
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
PRICE_ENGINE_EXECUTION_CANDIDATE_LANE = "execution_candidate"
PRICE_ENGINE_SETTLEMENT_LANE = "settlement"
# Ungranted BACKGROUND waiters refused because the operator paused the lane.
BACKGROUND_ADMISSION_PAUSED = "background_admission_paused"
# No runnable HOT work: leave one physical slot for ACTIVE (3 of 4 for lower).
IDLE_ACTIVE_RESERVED_SLOTS = 1
# Runnable HOT work: new lower-priority leases stop at one slot.
HOT_DEMAND_LOWER_OCCUPANCY_CEILING = 1
# Startup UNIVERSE barrier: leave one slot for ACTIVE. HOT/BACKGROUND are gated.
STARTUP_ACTIVE_RESERVED_SLOTS = 1


class ProviderPriority(IntEnum):
    ACTIVE_TRADE = 0
    EXECUTION_CANDIDATE = 1
    HOT = 2
    UNIVERSE = 3
    MANUAL = 3
    BACKGROUND = 4


def priority_for_lane(lane: ScanLane | str | None) -> ProviderPriority:
    text = str(lane or "").strip().casefold()
    if text in {PRICE_ENGINE_ACTIVE_TRADE_LANE, "active-trade"}:
        return ProviderPriority.ACTIVE_TRADE
    if text in {PRICE_ENGINE_EXECUTION_CANDIDATE_LANE, "execution-candidate"}:
        return ProviderPriority.EXECUTION_CANDIDATE
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
    requested_lane: str = ""
    event: asyncio.Event = field(default_factory=asyncio.Event)
    granted: bool = False
    cancelled: bool = False
    reason: str = HEALTH_WAITING
    work: Any = None
    enqueued_mono: float = 0.0
    granted_mono: float | None = None
    deadline_mono: float | None = None


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
    queue: dict[str, dict[str, Any]] = field(default_factory=dict)
    deadline_misses_by_lane: dict[str, int] = field(default_factory=dict)
    lower_priority_inflight: dict[str, int] = field(default_factory=dict)
    lower_priority_ceiling: dict[str, int | None] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "inflight": dict(self.inflight),
            "waiting": dict(self.waiting),
            "waiting_by_lane": {
                lane: dict(counts) for lane, counts in self.waiting_by_lane.items()
            },
            "hot_grants_since_universe": dict(self.hot_grants_since_universe),
            "limits": dict(self.limits),
            "queue": {venue: dict(metrics) for venue, metrics in self.queue.items()},
            "deadline_misses_by_lane": dict(self.deadline_misses_by_lane),
            "lower_priority_inflight": dict(self.lower_priority_inflight),
            "lower_priority_ceiling": dict(self.lower_priority_ceiling),
        }


class ProviderAccessLayer:
    """Process-shared per-venue concurrency with HOT priority and anti-starvation."""

    def __init__(
        self,
        limits: Mapping[VenueName, int] | None = None,
        *,
        starvation_hot_grants: int = DEFAULT_STARVATION_HOT_GRANTS,
        monotonic_clock: Callable[[], float] | None = None,
    ) -> None:
        resolved = limits or DEFAULT_PROVIDER_CONCURRENCY
        self._limits = {
            venue: max(1, int(resolved.get(venue, DEFAULT_PROVIDER_CONCURRENCY[venue])))
            for venue in DEFAULT_PROVIDER_CONCURRENCY
        }
        self._starvation_hot_grants = max(1, int(starvation_hot_grants))
        self._in_use = {venue: 0 for venue in self._limits}
        self._lower_in_use = {venue: 0 for venue in self._limits}
        self._peak_lower_in_use = {venue: 0 for venue in self._limits}
        self._startup_active_headroom = False
        self._hot_provider_demand = False
        self._background_admission_paused = False
        self._bound_loop: asyncio.AbstractEventLoop | None = None
        self._waiters: dict[VenueName, list[_Waiter]] = {venue: [] for venue in self._limits}
        self._hot_grants_since_universe = {venue: 0 for venue in self._limits}
        self._active_grants_since_hot = {venue: 0 for venue in self._limits}
        self._seq = 0
        self._cond = asyncio.Condition()
        self._peak_inflight = {venue: 0 for venue in self._limits}
        self._clock = monotonic_clock or monotonic
        self._ewma_latency_ms = {venue: 0 for venue in self._limits}
        self._last_service_ms = {venue: 0 for venue in self._limits}
        self._deadline_misses = {venue: 0 for venue in self._limits}
        self._deadline_misses_by_lane = {
            PRICE_ENGINE_ACTIVE_TRADE_LANE: 0,
            ScanLane.HOT.value: 0,
            ScanLane.UNIVERSE.value: 0,
            PRICE_ENGINE_BACKGROUND_LANE: 0,
        }
        self._rate_limited_until_mono = {venue: 0.0 for venue in self._limits}
        self._backoff_until_mono = {venue: 0.0 for venue in self._limits}

    @property
    def limits(self) -> dict[VenueName, int]:
        return dict(self._limits)

    @property
    def lower_in_use(self) -> dict[VenueName, int]:
        return dict(self._lower_in_use)

    @property
    def peak_lower_in_use(self) -> dict[VenueName, int]:
        return dict(self._peak_lower_in_use)

    def set_startup_active_headroom(self, enabled: bool) -> None:
        """Reserve one Matchbook/Kalshi slot for ACTIVE during startup UNIVERSE.

        HOT and BACKGROUND are gated while the startup barrier is closed, so
        the runnable-HOT ceiling would only slow discovery. One reserved slot
        is enough for ACTIVE. Pass False when startup pricing is released;
        lower-priority occupancy then follows HOT demand.
        """

        self._startup_active_headroom = bool(enabled)

    def set_hot_provider_demand(self, active: bool) -> None:
        """Record that HOT has due provider work or is waiting on a slot.

        The price engine sets this from in-memory scheduler truth. Admission
        only reads the flag. It does not query the catalogue. A change in
        either direction reconsiders queued waiters immediately so a cleared
        HOT demand flag can fill the expanded lower-priority ceiling without
        waiting for another enqueue or release.
        """

        active = bool(active)
        if self._hot_provider_demand == active:
            return
        self._hot_provider_demand = active
        self._reconsider_admission()

    @property
    def hot_provider_demand(self) -> bool:
        return self._hot_provider_demand

    def set_background_admission_paused(self, paused: bool) -> None:
        """Refuse new BACKGROUND leases while the operator pause is on.

        Already granted leases are left to finish. Queued, not-yet-granted
        BACKGROUND waiters are woken immediately and are not granted later.
        HOT, ACTIVE, UNIVERSE, and settlement keep their own admission rules.
        """

        paused = bool(paused)
        if self._background_admission_paused == paused:
            return
        self._background_admission_paused = paused
        self._reconsider_admission()

    @property
    def background_admission_paused(self) -> bool:
        return self._background_admission_paused

    def _bind_loop(self) -> None:
        if self._bound_loop is None:
            self._bound_loop = asyncio.get_running_loop()

    def _reconsider_admission(self) -> None:
        """Grant legal waiters or refuse paused BACKGROUND waiters now.

        Safe on the event loop when the condition is free, and safe from a
        worker thread by scheduling onto the loop that owns the waiters.
        Does not block the event loop and does not require the setter to be
        async.
        """

        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is not None and not self._cond.locked():
            self._reconsider_unlocked()
            return
        loop = self._bound_loop or running
        if loop is None or loop.is_closed():
            return

        def _later() -> None:
            if self._cond.locked():
                loop.call_soon(self._reconsider_when_free)
                return
            self._reconsider_unlocked()

        if running is loop:
            loop.call_soon(_later)
        else:
            loop.call_soon_threadsafe(_later)

    def _reconsider_when_free(self) -> None:
        loop = self._bound_loop
        if self._cond.locked():
            if loop is not None and not loop.is_closed():
                loop.call_soon(self._reconsider_when_free)
            return
        self._reconsider_unlocked()

    def _reconsider_unlocked(self) -> None:
        if self._background_admission_paused:
            for waiters in self._waiters.values():
                for waiter in waiters:
                    if waiter.granted or not self._background_admission_blocked(waiter):
                        continue
                    self._refuse_ungranted_background(waiter)
        for venue in self._limits:
            self._pump(venue)

    def _background_admission_blocked(self, waiter: _Waiter) -> bool:
        return (
            self._background_admission_paused
            and waiter.requested_lane == PRICE_ENGINE_BACKGROUND_LANE
        )

    def _refuse_ungranted_background(self, waiter: _Waiter) -> None:
        if waiter.granted or waiter.cancelled:
            return
        waiter.cancelled = True
        waiter.reason = BACKGROUND_ADMISSION_PAUSED
        waiter.event.set()

    def _hot_demand_active(self) -> bool:
        """True when HOT can use a slot or is already using one.

        Queued and granted HOT leases count even if the engine flag has not
        been refreshed yet. That keeps a HOT request from waiting behind a
        lower-priority grant that would fill the last protected slot.
        """

        if self._hot_provider_demand:
            return True
        for waiters in self._waiters.values():
            for item in waiters:
                if item.cancelled or item.priority is not ProviderPriority.HOT:
                    continue
                return True
        return False

    def lower_priority_occupancy_ceiling(self, venue: VenueName) -> int | None:
        """Max simultaneous BACKGROUND+UNIVERSE leases, or None when uncapped."""

        protected = self._protected_high_priority_slots(venue)
        if protected <= 0 or venue not in self._limits:
            return None
        return max(0, self._limits[venue] - protected)

    def _protected_high_priority_slots(self, venue: VenueName) -> int:
        if venue not in {VenueName.MATCHBOOK, VenueName.KALSHI}:
            return 0
        if venue not in self._limits:
            return 0
        limit = self._limits[venue]
        if limit < 4:
            return 0
        if self._startup_active_headroom:
            return STARTUP_ACTIVE_RESERVED_SLOTS
        if self._hot_demand_active():
            return limit - HOT_DEMAND_LOWER_OCCUPANCY_CEILING
        return IDLE_ACTIVE_RESERVED_SLOTS

    @staticmethod
    def _is_lower_priority(priority: ProviderPriority) -> bool:
        return priority not in {
            ProviderPriority.ACTIVE_TRADE,
            ProviderPriority.EXECUTION_CANDIDATE,
            ProviderPriority.HOT,
        }

    def _lower_grant_allowed(self, venue: VenueName, waiter: _Waiter) -> bool:
        if not self._is_lower_priority(waiter.priority):
            return True
        ceiling = self.lower_priority_occupancy_ceiling(venue)
        if ceiling is None:
            return True
        return self._lower_in_use[venue] < ceiling

    def snapshot(self) -> ProviderAccessSnapshot:
        waiting_by_lane = {
            "active_trade": {venue.value: 0 for venue in self._limits},
            "execution_candidate": {venue.value: 0 for venue in self._limits},
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
                elif waiter.priority is ProviderPriority.EXECUTION_CANDIDATE:
                    lane = "execution_candidate"
                elif waiter.priority is ProviderPriority.HOT:
                    lane = "hot"
                elif waiter.priority is ProviderPriority.BACKGROUND:
                    lane = "background"
                else:
                    lane = "universe"
                waiting_by_lane[lane][venue.value] += 1
        now = self._clock()
        queue: dict[str, dict[str, Any]] = {}
        for venue in self._limits:
            pending = [
                item
                for item in self._waiters[venue]
                if not item.granted and not item.cancelled
            ]
            wait_ages = [
                max(0, int((now - item.enqueued_mono) * 1000))
                for item in pending
                if item.enqueued_mono
            ]
            remaining = self._rate_limit_remaining(venue, now=now)
            backoff = self._backoff_remaining(venue, now=now)
            saturated = self.venue_saturated(venue)
            backpressure = bool(
                saturated
                or remaining > 0
                or backoff > 0
                or self._ewma_latency_ms[venue] >= 1500
                or (self._limits[venue] > 0 and len(pending) >= self._limits[venue])
            )
            queue[venue.value] = {
                "depth": len(pending),
                "wait_age_ms": max(wait_ages) if wait_ages else 0,
                "mean_wait_age_ms": (sum(wait_ages) // len(wait_ages)) if wait_ages else 0,
                "service_latency_ms": int(self._ewma_latency_ms[venue]),
                "last_service_ms": int(self._last_service_ms[venue]),
                "deadline_misses": int(self._deadline_misses[venue]),
                "saturated": saturated,
                "backpressure": backpressure,
                "rate_limited": remaining > 0,
                "rate_limit_remaining_s": remaining,
                "backoff_remaining_s": backoff,
            }
        return ProviderAccessSnapshot(
            inflight={venue.value: count for venue, count in self._in_use.items()},
            waiting=waiting,
            waiting_by_lane=waiting_by_lane,
            hot_grants_since_universe={
                venue.value: count for venue, count in self._hot_grants_since_universe.items()
            },
            limits={venue.value: limit for venue, limit in self._limits.items()},
            queue=queue,
            deadline_misses_by_lane=dict(self._deadline_misses_by_lane),
            lower_priority_inflight={
                venue.value: count for venue, count in self._lower_in_use.items()
            },
            lower_priority_ceiling={
                venue.value: self.lower_priority_occupancy_ceiling(venue)
                for venue in self._limits
            },
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
        work: Any = None,
    ) -> AsyncIterator[ProviderLease]:
        if venue not in self._limits:
            yield ProviderLease.unbound()
            return
        waiter = await self._enqueue(venue, lane=lane, stage=stage, work=work)
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
        work: Any = None,
    ) -> _Waiter:
        async with self._cond:
            self._bind_loop()
            waiter = self._make_waiter(venue, lane=lane, stage=stage, work=work)
            self._waiters[venue].append(waiter)
            if self._background_admission_blocked(waiter):
                self._refuse_ungranted_background(waiter)
            else:
                self._pump(venue)
            return waiter

    def _make_waiter(
        self,
        venue: VenueName,
        *,
        lane: ScanLane | str | None,
        stage: str,
        work: Any = None,
    ) -> _Waiter:
        self._seq += 1
        priority = priority_for_lane(lane)
        requested_lane = str(lane or "").strip().casefold()
        reason = HEALTH_WAITING
        if (
            priority
            not in {
                ProviderPriority.HOT,
                ProviderPriority.ACTIVE_TRADE,
                ProviderPriority.EXECUTION_CANDIDATE,
            }
            and self._hot_ahead(venue)
        ):
            reason = HEALTH_DEFERRED
        if priority is ProviderPriority.ACTIVE_TRADE:
            waiter_lane = PRICE_ENGINE_ACTIVE_TRADE_LANE
        elif priority is ProviderPriority.EXECUTION_CANDIDATE:
            waiter_lane = PRICE_ENGINE_EXECUTION_CANDIDATE_LANE
        elif priority is ProviderPriority.HOT:
            waiter_lane = ScanLane.HOT.value
        elif priority is ProviderPriority.BACKGROUND:
            waiter_lane = PRICE_ENGINE_BACKGROUND_LANE
        else:
            waiter_lane = ScanLane.UNIVERSE.value
        resolved_work = work
        if resolved_work is None:
            from sports_hedge.application.adaptive_scheduler import work_from_lane

            resolved_work = work_from_lane(waiter_lane, seq=self._seq)
        deadline = getattr(resolved_work, "deadline_mono", None)
        return _Waiter(
            priority=priority,
            seq=self._seq,
            lane=waiter_lane,
            stage=stage,
            requested_lane=requested_lane,
            reason=reason,
            work=resolved_work,
            enqueued_mono=self._clock(),
            deadline_mono=None if deadline is None else float(deadline),
        )

    @asynccontextmanager
    async def acquire_wait(
        self,
        venue: VenueName,
        *,
        lane: ScanLane | str | None = None,
        stage: str = "provider",
        timeout: float | None = None,
        work: Any = None,
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
        waiter = await self._enqueue(venue, lane=lane, stage=stage, work=work)
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
                if waiter.reason == BACKGROUND_ADMISSION_PAUSED:
                    yield None
                    return
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
        work: Any = None,
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
            self._bind_loop()
            waiter = self._make_waiter(venue, lane=lane, stage=stage, work=work)
            self._waiters[venue].append(waiter)
            picked = self._pick(venue)
            if picked is waiter and self._in_use[venue] < self._limits[venue]:
                self._grant(venue, waiter)
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
                self._record_service(venue, waiter)
                self._in_use[venue] = max(0, self._in_use[venue] - 1)
                if self._is_lower_priority(waiter.priority):
                    self._lower_in_use[venue] = max(0, self._lower_in_use[venue] - 1)
            else:
                waiter.cancelled = True
                self._record_unserved_deadline(venue, waiter)
            self._pump(venue)
            self._cond.notify_all()

    def _hot_ahead(self, venue: VenueName) -> bool:
        return any(
            item.priority
            in {
                ProviderPriority.HOT,
                ProviderPriority.ACTIVE_TRADE,
                ProviderPriority.EXECUTION_CANDIDATE,
            }
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
        elif priority is ProviderPriority.EXECUTION_CANDIDATE:
            # Do not promote UNIVERSE/BACKGROUND over an open position, and do
            # not treat pre-entry work as ACTIVE grant pressure on HOT.
            return
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

    def _background_waiting(self, venue: VenueName) -> bool:
        return any(
            item.priority is ProviderPriority.BACKGROUND
            and not item.granted
            and not item.cancelled
            for item in self._waiters[venue]
        )

    def _pick(self, venue: VenueName) -> _Waiter | None:
        pending = [
            item
            for item in self._waiters[venue]
            if (
                not item.granted
                and not item.cancelled
                and not self._background_admission_blocked(item)
                and self._lower_grant_allowed(venue, item)
            )
        ]
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
        starve_background = (
            self._background_waiting(venue)
            and self._hot_grants_since_universe[venue] >= self._starvation_hot_grants
        )
        starve_lower = starve_universe or starve_background
        now = self._clock()
        pressure = self.pressure_by_venue(now=now)

        def sort_key(item: _Waiter) -> tuple[int, ...]:
            from sports_hedge.application.adaptive_scheduler import (
                SchedulerWork,
                rank_scheduler_work,
                work_from_lane,
            )

            work = item.work
            wait_age_ms = 0
            if item.enqueued_mono:
                wait_age_ms = max(0, int((now - item.enqueued_mono) * 1000))
            if not isinstance(work, SchedulerWork):
                work = work_from_lane(item.lane, seq=item.seq)
            work = SchedulerWork(
                lane=work.lane,
                work_id=work.work_id,
                viable_venue_count=work.viable_venue_count,
                skip_expensive_work=work.skip_expensive_work,
                viability_reason=work.viability_reason,
                viability_assessed=work.viability_assessed,
                near_threshold=work.near_threshold,
                qualifying=work.qualifying,
                in_play=work.in_play,
                required_venues=work.required_venues or (venue,),
                due_mono=work.due_mono,
                deadline_mono=item.deadline_mono if item.deadline_mono is not None else work.deadline_mono,
                cadence_seconds=work.cadence_seconds,
                seq=item.seq,
                wait_age_ms=wait_age_ms,
                now_mono=now,
            )
            decision = rank_scheduler_work(
                work,
                pressure_by_venue=pressure,
                starve_lower=starve_lower,
                starve_hot=starve_hot and item.priority is ProviderPriority.HOT,
            )
            return decision.rank_key

        return min(pending, key=sort_key)

    def _pump(self, venue: VenueName) -> None:
        while self._in_use[venue] < self._limits[venue]:
            nxt = self._pick(venue)
            if nxt is None:
                return
            self._grant(venue, nxt)

    def _grant(self, venue: VenueName, waiter: _Waiter) -> None:
        waiter.granted = True
        waiter.granted_mono = self._clock()
        waiter.event.set()
        self._in_use[venue] += 1
        self._peak_inflight[venue] = max(self._peak_inflight[venue], self._in_use[venue])
        if self._is_lower_priority(waiter.priority):
            self._lower_in_use[venue] += 1
            self._peak_lower_in_use[venue] = max(
                self._peak_lower_in_use[venue], self._lower_in_use[venue]
            )
        self._count_high_priority_grant(venue, waiter.priority)
        self._record_grant_deadline(venue, waiter)

    def _record_service(self, venue: VenueName, waiter: _Waiter) -> None:
        if waiter.granted_mono is None:
            return
        service_ms = max(0, int((self._clock() - waiter.granted_mono) * 1000))
        self._last_service_ms[venue] = service_ms
        previous = self._ewma_latency_ms[venue]
        if previous <= 0:
            self._ewma_latency_ms[venue] = service_ms
        else:
            self._ewma_latency_ms[venue] = int(0.7 * previous + 0.3 * service_ms)

    def _record_grant_deadline(self, venue: VenueName, waiter: _Waiter) -> None:
        if waiter.deadline_mono is None or waiter.granted_mono is None:
            return
        if waiter.granted_mono <= waiter.deadline_mono:
            return
        self.record_deadline_miss(venue, lane=waiter.lane)

    def _record_unserved_deadline(self, venue: VenueName, waiter: _Waiter) -> None:
        if waiter.deadline_mono is None:
            return
        if self._clock() <= waiter.deadline_mono:
            return
        self.record_deadline_miss(venue, lane=waiter.lane)

    def record_deadline_miss(self, venue: VenueName, *, lane: str | None = None) -> None:
        if venue in self._deadline_misses:
            self._deadline_misses[venue] += 1
        key = str(lane or ScanLane.UNIVERSE.value).strip().casefold()
        if key not in self._deadline_misses_by_lane:
            self._deadline_misses_by_lane[key] = 0
        self._deadline_misses_by_lane[key] += 1

    def observe_rate_limit(self, venue: VenueName, retry_after_seconds: float) -> None:
        if venue not in self._limits:
            return
        wait = max(0.0, float(retry_after_seconds))
        self._rate_limited_until_mono[venue] = self._clock() + wait

    def observe_backoff(self, venue: VenueName, remaining_seconds: float) -> None:
        if venue not in self._limits:
            return
        wait = max(0.0, float(remaining_seconds))
        self._backoff_until_mono[venue] = self._clock() + wait

    def observe_latency_ms(self, venue: VenueName, latency_ms: int) -> None:
        if venue not in self._limits:
            return
        service_ms = max(0, int(latency_ms))
        self._last_service_ms[venue] = service_ms
        previous = self._ewma_latency_ms[venue]
        if previous <= 0:
            self._ewma_latency_ms[venue] = service_ms
        else:
            self._ewma_latency_ms[venue] = int(0.7 * previous + 0.3 * service_ms)

    def _rate_limit_remaining(self, venue: VenueName, *, now: float | None = None) -> float:
        until = self._rate_limited_until_mono.get(venue, 0.0)
        remaining = until - (now if now is not None else self._clock())
        return remaining if remaining > 0 else 0.0

    def _backoff_remaining(self, venue: VenueName, *, now: float | None = None) -> float:
        until = self._backoff_until_mono.get(venue, 0.0)
        remaining = until - (now if now is not None else self._clock())
        return remaining if remaining > 0 else 0.0

    def pressure_by_venue(self, *, now: float | None = None) -> dict[VenueName, Any]:
        from sports_hedge.application.adaptive_scheduler import ProviderPressure

        evaluated = now if now is not None else self._clock()
        payload: dict[VenueName, Any] = {}
        for venue in self._limits:
            remaining = self._rate_limit_remaining(venue, now=evaluated)
            backoff = self._backoff_remaining(venue, now=evaluated)
            waiting = sum(
                1
                for item in self._waiters[venue]
                if not item.granted and not item.cancelled
            )
            payload[venue] = ProviderPressure(
                venue=venue,
                inflight=self._in_use[venue],
                waiting=waiting,
                limit=self._limits[venue],
                ewma_latency_ms=int(self._ewma_latency_ms[venue]),
                rate_limited=remaining > 0,
                rate_limit_remaining_s=remaining,
                backoff_remaining_s=backoff,
            )
        return payload


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
