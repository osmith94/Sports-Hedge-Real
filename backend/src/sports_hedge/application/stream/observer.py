"""Public Polymarket market-channel observer. Read-only. No credentials."""

from __future__ import annotations

import asyncio
import random
from collections.abc import Callable
from datetime import UTC, datetime
from time import monotonic

from sports_hedge.application.stream.order_book import StreamOrderBooks
from sports_hedge.application.stream.protocol import (
    EVENT_BOOK,
    EVENT_PRICE_CHANGE,
    PING_INTERVAL_SECONDS,
    PING_TEXT,
    PONG_TEXT,
    PONG_TIMEOUT_SECONDS,
    RECONNECT_BACKOFF_MAX_SECONDS,
    RECONNECT_BACKOFF_START_SECONDS,
    STALE_AFTER_SECONDS,
    UNSUBSCRIBE_OPERATION,
    encode_ws_message,
    market_subscribe_payload,
    message_event_type,
    parse_ws_message,
)
from sports_hedge.application.stream.transport import (
    MarketWsTransport,
    StreamTransportClosed,
    TransportFactory,
    WebsocketMarketTransport,
)

ConnectionStatus = str
STATUS_DISABLED = "disabled"
STATUS_NOT_SELECTED = "not_selected"
STATUS_CONNECTING = "connecting"
STATUS_SUBSCRIBED = "subscribed"
STATUS_STALE = "stale"
STATUS_DEGRADED = "degraded"
STATUS_PAUSED = "paused"

OnBookUpdate = Callable[[], None]


class PolymarketMarketObserver:
    def __init__(
        self,
        token_ids: list[str],
        *,
        transport_factory: TransportFactory | None = None,
        ping_interval: float = PING_INTERVAL_SECONDS,
        pong_timeout: float = PONG_TIMEOUT_SECONDS,
        stale_after: float = STALE_AFTER_SECONDS,
        on_update: OnBookUpdate | None = None,
        jitter: Callable[[], float] | None = None,
    ) -> None:
        self.token_ids = [str(token).strip() for token in token_ids if str(token).strip()]
        self.transport_factory = transport_factory or WebsocketMarketTransport
        self.ping_interval = ping_interval
        self.pong_timeout = pong_timeout
        self.stale_after = stale_after
        self.on_update = on_update
        self._jitter = jitter or (lambda: random.random() * 0.2)
        self.books = StreamOrderBooks(self.token_ids)
        self.status = STATUS_DISABLED
        self.reconnect_count = 0
        self.last_snapshot_at: datetime | None = None
        self.last_incremental_at: datetime | None = None
        self.last_pong_mono: float | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._transport: MarketWsTransport | None = None
        self._stop = asyncio.Event()
        self._subscribed = False

    @property
    def subscribed(self) -> bool:
        return self._subscribed and self.status in {STATUS_SUBSCRIBED, STATUS_STALE, STATUS_DEGRADED}

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self.status = STATUS_CONNECTING
        self._task = asyncio.create_task(self._run(), name="stream-polymarket-market-ws")

    async def stop(self) -> None:
        self._stop.set()
        transport = self._transport
        if transport is not None and self._subscribed:
            try:
                await transport.send(
                    encode_ws_message(
                        market_subscribe_payload(self.token_ids, operation=UNSUBSCRIBE_OPERATION)
                    )
                )
            except Exception:
                pass
        if transport is not None:
            try:
                await transport.close()
            except Exception:
                pass
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._subscribed = False
        self._transport = None
        self.books.reset()
        self.status = STATUS_DISABLED

    def connection_status(self, *, paused: bool = False, selected: bool = True) -> str:
        if paused:
            return STATUS_PAUSED
        if not selected:
            return STATUS_NOT_SELECTED
        if self.status == STATUS_SUBSCRIBED and self._is_stale():
            return STATUS_STALE
        return self.status

    def _is_stale(self) -> bool:
        last = self.last_incremental_at or self.last_snapshot_at
        if last is None:
            return True
        age = (datetime.now(UTC) - last).total_seconds()
        return age > self.stale_after

    async def _run(self) -> None:
        backoff = RECONNECT_BACKOFF_START_SECONDS
        while not self._stop.is_set():
            try:
                await self._session()
                backoff = RECONNECT_BACKOFF_START_SECONDS
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                self.last_error = type(exc).__name__
                self.status = STATUS_DEGRADED
                self._subscribed = False
                self.books.reset()
                self.reconnect_count += 1
            if self._stop.is_set():
                return
            jittered = backoff * (1.0 + self._jitter())
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=min(jittered, RECONNECT_BACKOFF_MAX_SECONDS))
                return
            except TimeoutError:
                backoff = min(backoff * 2.0, RECONNECT_BACKOFF_MAX_SECONDS)
                self.status = STATUS_CONNECTING

    async def _session(self) -> None:
        transport = self.transport_factory()
        self._transport = transport
        self.status = STATUS_CONNECTING
        self.books.reset()
        self._subscribed = False
        await transport.connect()
        await transport.send(encode_ws_message(market_subscribe_payload(self.token_ids)))
        self._subscribed = True
        self.last_pong_mono = monotonic()
        ping_task = asyncio.create_task(self._ping_loop(transport), name="stream-market-ping")
        try:
            while not self._stop.is_set():
                if (
                    self.last_pong_mono is not None
                    and monotonic() - self.last_pong_mono > self.pong_timeout
                ):
                    raise StreamTransportClosed("pong_timeout")
                try:
                    raw = await asyncio.wait_for(transport.recv(), timeout=1.0)
                except TimeoutError:
                    self._refresh_status()
                    continue
                self._handle(raw)
        finally:
            ping_task.cancel()
            try:
                await ping_task
            except asyncio.CancelledError:
                pass
            try:
                await transport.close()
            except Exception:
                pass
            if self._transport is transport:
                self._transport = None
            self._subscribed = False

    async def _ping_loop(self, transport: MarketWsTransport) -> None:
        while not self._stop.is_set():
            await asyncio.sleep(self.ping_interval)
            await transport.send(PING_TEXT)

    def _handle(self, raw: str) -> None:
        try:
            parsed = parse_ws_message(raw)
        except ValueError:
            self.books.bad_message_count += 1
            self.status = STATUS_DEGRADED
            return
        if parsed == PONG_TEXT:
            self.last_pong_mono = monotonic()
            return
        if not isinstance(parsed, dict):
            return
        event = message_event_type(parsed)
        result = self.books.apply_message(parsed)
        now = datetime.now(UTC)
        if event == EVENT_BOOK and result == "snapshot":
            self.last_snapshot_at = now
            self.last_incremental_at = now
        elif event == EVENT_PRICE_CHANGE and result == "delta":
            self.last_incremental_at = now
        if self.books.healthy_token_count() == len(self.token_ids):
            self.status = STATUS_SUBSCRIBED
            self.last_error = None
        elif self.books.resync_count:
            self.status = STATUS_DEGRADED
        if result in {"snapshot", "delta"} and self.on_update is not None:
            self.on_update()
        self._refresh_status()

    def _refresh_status(self) -> None:
        if self.status == STATUS_SUBSCRIBED and self._is_stale():
            self.status = STATUS_STALE
            self.books.reset()
        elif self.status == STATUS_STALE and not self._is_stale() and self.books.healthy_token_count() == len(
            self.token_ids
        ):
            self.status = STATUS_SUBSCRIBED
