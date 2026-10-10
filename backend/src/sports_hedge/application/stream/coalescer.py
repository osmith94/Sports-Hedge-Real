"""Coalesced Matchbook exact-market refresh via the shared provider-access layer."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic
from typing import Any

from sports_hedge.application.provider_access import ProviderAccessLayer, get_shared_provider_access
from sports_hedge.application.stream.protocol import (
    STREAM_LANE,
    TRIGGER_BASELINE,
    TRIGGER_DEPTH_CHANGE,
    TRIGGER_PERIODIC,
    TRIGGER_POTENTIAL_EDGE,
    TRIGGER_PRICE_MOVE,
    TRIGGER_RECONNECT,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.venues.rate_limit import ProviderRateLimitedError

FetchMarket = Callable[[str, str], Awaitable[dict[str, Any]]]

MAX_PENDING_MATCHBOOK = 8
MATCHBOOK_SLOT_WAIT_SECONDS = 2.0
REASON_RANK = {
    TRIGGER_RECONNECT: 100,
    TRIGGER_BASELINE: 90,
    TRIGGER_PRICE_MOVE: 80,
    TRIGGER_POTENTIAL_EDGE: 70,
    TRIGGER_DEPTH_CHANGE: 60,
    TRIGGER_PERIODIC: 10,
}


@dataclass
class PendingRefresh:
    event_id: str
    market_id: str
    reason: str
    probability_delta: Decimal | None
    enqueued_mono: float


@dataclass
class MatchbookRefreshStats:
    request_count: int = 0
    coalesced_count: int = 0
    dropped_count: int = 0
    rate_limited_count: int = 0
    suppressed_count: int = 0
    last_error: str | None = None
    last_trigger_reason: str | None = None
    last_probability_delta: str | None = None
    last_dispatch_delay_ms: int | None = None
    requests_by_trigger: dict[str, int] = field(default_factory=dict)


@dataclass
class MatchbookQuote:
    event_id: str
    market_id: str
    payload: dict[str, Any]
    retrieved_at: datetime


class StreamMatchbookCoalescer:
    """At most one in-flight exact Matchbook GET; extra WS ticks coalesce or drop."""

    def __init__(
        self,
        *,
        fetch_market: FetchMarket | None = None,
        provider_access: ProviderAccessLayer | None = None,
        slot_wait_seconds: float = MATCHBOOK_SLOT_WAIT_SECONDS,
        enabled: bool = False,
    ) -> None:
        self._fetch_market = fetch_market
        self.provider_access = provider_access
        self.slot_wait_seconds = slot_wait_seconds
        self.enabled = enabled
        self.stats = MatchbookRefreshStats()
        self.quotes: dict[str, MatchbookQuote] = {}
        self._inflight = False
        self._inflight_keys: set[tuple[str, str]] = set()
        self._pending: dict[tuple[str, str], PendingRefresh] = {}
        self._pending_event = asyncio.Event()
        self._worker: asyncio.Task[None] | None = None
        self.on_quote: Callable[[], None] | None = None

    def bind_fetch(self, fetch_market: FetchMarket) -> None:
        self._fetch_market = fetch_market

    def start(self) -> None:
        if self._worker is not None and not self._worker.done():
            return
        self._worker = asyncio.create_task(self._run(), name="stream-matchbook-coalesce")

    async def stop(self) -> None:
        worker = self._worker
        self._worker = None
        self._pending.clear()
        self._pending_event.set()
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._inflight = False
        self._inflight_keys.clear()
        self._pending_event = asyncio.Event()

    def note_suppressed(self, count: int = 1) -> None:
        self.stats.suppressed_count += max(0, count)

    def schedule(
        self,
        keys: list[tuple[str, str]] | tuple[tuple[str, str], ...],
        *,
        reason: str = TRIGGER_BASELINE,
        probability_delta: Decimal | None = None,
    ) -> None:
        if not self.enabled or self._fetch_market is None:
            return
        cleaned = [
            (str(event_id).strip(), str(market_id).strip())
            for event_id, market_id in keys
            if str(event_id).strip() and str(market_id).strip()
        ]
        if not cleaned:
            return
        rank = REASON_RANK.get(reason, 0)
        for event_id, market_id in cleaned:
            key = (event_id, market_id)
            existing = self._pending.get(key)
            if existing is not None or key in self._inflight_keys or self._inflight:
                self.stats.coalesced_count += 1
            if existing is not None:
                if rank > REASON_RANK.get(existing.reason, 0):
                    existing.reason = reason
                if probability_delta is not None:
                    existing.probability_delta = probability_delta
                continue
            if key in self._inflight_keys:
                if len(self._pending) >= MAX_PENDING_MATCHBOOK:
                    self.stats.dropped_count += 1
                    continue
                self._pending[key] = PendingRefresh(
                    event_id=event_id,
                    market_id=market_id,
                    reason=reason,
                    probability_delta=probability_delta,
                    enqueued_mono=monotonic(),
                )
                continue
            if len(self._pending) >= MAX_PENDING_MATCHBOOK:
                dropped_periodic = [
                    pending_key
                    for pending_key, item in self._pending.items()
                    if item.reason == TRIGGER_PERIODIC
                ]
                if rank <= REASON_RANK[TRIGGER_PERIODIC]:
                    self.stats.dropped_count += 1
                    continue
                if dropped_periodic:
                    self._pending.pop(dropped_periodic[0], None)
                    self.stats.dropped_count += 1
                else:
                    self.stats.dropped_count += 1
                    continue
            self._pending[key] = PendingRefresh(
                event_id=event_id,
                market_id=market_id,
                reason=reason,
                probability_delta=probability_delta,
                enqueued_mono=monotonic(),
            )
        if self._pending:
            self._pending_event.set()

    async def _run(self) -> None:
        while True:
            await self._pending_event.wait()
            self._pending_event.clear()
            items = list(self._pending.values())
            self._pending.clear()
            if not items or not self.enabled or self._fetch_market is None:
                continue
            self._inflight = True
            self._inflight_keys = {(item.event_id, item.market_id) for item in items}
            try:
                for item in items:
                    await self._fetch_one(item)
            finally:
                self._inflight = False
                self._inflight_keys.clear()
                if self._pending:
                    self._pending_event.set()

    async def _fetch_one(self, item: PendingRefresh) -> None:
        access = self.provider_access
        if access is None:
            access = get_shared_provider_access()
        fetch = self._fetch_market
        if fetch is None:
            return
        self.stats.request_count += 1
        self.stats.last_trigger_reason = item.reason
        self.stats.last_probability_delta = (
            None if item.probability_delta is None else format(item.probability_delta, "f")
        )
        self.stats.last_dispatch_delay_ms = int(max(0.0, monotonic() - item.enqueued_mono) * 1000)
        self.stats.requests_by_trigger[item.reason] = (
            self.stats.requests_by_trigger.get(item.reason, 0) + 1
        )
        try:
            async with access.acquire_wait(
                VenueName.MATCHBOOK,
                lane=STREAM_LANE,
                stage="stream_get_market",
                timeout=self.slot_wait_seconds,
            ) as lease:
                if lease is None:
                    self.stats.dropped_count += 1
                    self.stats.last_error = "provider_capacity_saturated"
                    return
                payload = await fetch(item.event_id, item.market_id)
        except ProviderRateLimitedError as exc:
            self.stats.rate_limited_count += 1
            self.stats.last_error = "matchbook_rate_limited"
            access.observe_rate_limit(VenueName.MATCHBOOK, exc.retry_after_seconds)
            return
        except Exception as exc:
            self.stats.last_error = type(exc).__name__
            return
        quote_key = f"{item.event_id}:{item.market_id}"
        self.quotes[quote_key] = MatchbookQuote(
            event_id=item.event_id,
            market_id=item.market_id,
            payload=payload,
            retrieved_at=datetime.now(UTC),
        )
        self.stats.last_error = None
        if self.on_quote is not None:
            self.on_quote()
