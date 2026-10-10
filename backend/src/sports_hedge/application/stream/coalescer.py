"""Coalesced Matchbook exact-market refresh via the shared provider-access layer."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sports_hedge.application.provider_access import ProviderAccessLayer, get_shared_provider_access
from sports_hedge.application.stream.protocol import STREAM_LANE
from sports_hedge.domain.models import VenueName
from sports_hedge.venues.rate_limit import ProviderRateLimitedError

FetchMarket = Callable[[str, str], Awaitable[dict[str, Any]]]

MAX_PENDING_MATCHBOOK = 1
MATCHBOOK_SLOT_WAIT_SECONDS = 2.0


@dataclass
class MatchbookRefreshStats:
    request_count: int = 0
    coalesced_count: int = 0
    dropped_count: int = 0
    rate_limited_count: int = 0
    last_error: str | None = None


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
        self._pending_keys: set[tuple[str, str]] = set()
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
        self._pending_keys.clear()
        self._pending_event.set()
        if worker is not None:
            worker.cancel()
            try:
                await worker
            except asyncio.CancelledError:
                pass
        self._inflight = False
        self._pending_event = asyncio.Event()

    def schedule(self, keys: list[tuple[str, str]]) -> None:
        if not self.enabled or self._fetch_market is None:
            return
        cleaned = [
            (str(event_id).strip(), str(market_id).strip())
            for event_id, market_id in keys
            if str(event_id).strip() and str(market_id).strip()
        ]
        if not cleaned:
            return
        if self._inflight or self._pending_keys:
            self.stats.coalesced_count += 1
            if self._inflight and self._pending_keys and len(self._pending_keys) >= MAX_PENDING_MATCHBOOK:
                extra = [key for key in cleaned if key not in self._pending_keys]
                if extra:
                    self.stats.dropped_count += 1
                    cleaned = [key for key in cleaned if key in self._pending_keys]
        if not cleaned:
            return
        self._pending_keys.update(cleaned)
        self._pending_event.set()

    async def _run(self) -> None:
        while True:
            await self._pending_event.wait()
            self._pending_event.clear()
            keys = list(self._pending_keys)
            self._pending_keys.clear()
            if not keys or not self.enabled or self._fetch_market is None:
                continue
            self._inflight = True
            try:
                for event_id, market_id in keys:
                    await self._fetch_one(event_id, market_id)
            finally:
                self._inflight = False

    async def _fetch_one(self, event_id: str, market_id: str) -> None:
        access = self.provider_access
        if access is None:
            access = get_shared_provider_access()
        fetch = self._fetch_market
        if fetch is None:
            return
        self.stats.request_count += 1
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
                payload = await fetch(event_id, market_id)
        except ProviderRateLimitedError as exc:
            self.stats.rate_limited_count += 1
            self.stats.last_error = "matchbook_rate_limited"
            access.observe_rate_limit(VenueName.MATCHBOOK, exc.retry_after_seconds)
            return
        except Exception as exc:
            self.stats.last_error = type(exc).__name__
            return
        quote_key = f"{event_id}:{market_id}"
        self.quotes[quote_key] = MatchbookQuote(
            event_id=event_id,
            market_id=market_id,
            payload=payload,
            retrieved_at=datetime.now(UTC),
        )
        self.stats.last_error = None
        if self.on_quote is not None:
            self.on_quote()
