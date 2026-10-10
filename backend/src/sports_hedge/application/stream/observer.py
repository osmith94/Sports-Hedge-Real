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
    RESYNC_MIN_INTERVAL_SECONDS,
    STALE_AFTER_SECONDS,
    TRIGGER_RECONNECT,
    UNSUBSCRIBE_OPERATION,
    encode_ws_message,
    iter_ws_events,
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
from sports_hedge.application.stream.trigger import StreamBookSignal

ConnectionStatus = str
STATUS_DISABLED = "disabled"
STATUS_NOT_SELECTED = "not_selected"
STATUS_CONNECTING = "connecting"
STATUS_SUBSCRIBED = "subscribed"
STATUS_STALE = "stale"
STATUS_DEGRADED = "degraded"
STATUS_PAUSED = "paused"

OnBookUpdate = Callable[[StreamBookSignal], None]


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
        resync_min_interval: float = RESYNC_MIN_INTERVAL_SECONDS,
    ) -> None:
        self.token_ids = [str(token).strip() for token in token_ids if str(token).strip()]
        self.transport_factory = transport_factory or WebsocketMarketTransport
        self.ping_interval = ping_interval
        self.pong_timeout = pong_timeout
        self.stale_after = stale_after
        self.on_update = on_update
        self.resync_min_interval = resync_min_interval
        self._jitter = jitter or (lambda: random.random() * 0.2)
        self.books = StreamOrderBooks(self.token_ids)
        self.status = STATUS_DISABLED
        self.reconnect_count = 0
        self.subscription_resync_count = 0
        self.last_snapshot_at: datetime | None = None
        self.last_incremental_at: datetime | None = None
        self.last_pong_mono: float | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._resync_task: asyncio.Task[None] | None = None
        self._transport: MarketWsTransport | None = None
        self._stop = asyncio.Event()
        self._subscribed = False
        self._awaiting_baseline = False
        self._recovery_kind: str | None = None
        self._last_resync_mono: float | None = None
        self._session_started_at: datetime | None = None

    @property
    def subscribed(self) -> bool:
        return self._subscribed and self.status in {STATUS_SUBSCRIBED, STATUS_STALE, STATUS_DEGRADED}

    def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self.status = STATUS_CONNECTING
        self._recovery_kind = "initial"
        self._task = asyncio.create_task(self._run(), name="stream-polymarket-market-ws")

    async def stop(self) -> None:
        self._stop.set()
        resync = self._resync_task
        self._resync_task = None
        if resync is not None:
            resync.cancel()
            try:
                await resync
            except asyncio.CancelledError:
                pass
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

    def _is_stale(self, now: datetime | None = None) -> bool:
        return bool(self._stale_tokens(now))

    def _stale_tokens(self, now: datetime | None = None) -> list[str]:
        evaluated = now or datetime.now(UTC)
        stale: list[str] = []
        for token in self.token_ids:
            book = self.books.books.get(token)
            if book is None:
                stale.append(token)
                continue
            if book.last_applied_wall is None:
                started = self._session_started_at
                if (
                    started is not None
                    and (evaluated - started).total_seconds() > self.stale_after
                ):
                    stale.append(token)
                continue
            age = (evaluated - book.last_applied_wall).total_seconds()
            if age > self.stale_after:
                stale.append(token)
        return stale

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
                self._awaiting_baseline = True
                self._recovery_kind = TRIGGER_RECONNECT
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
        self._awaiting_baseline = True
        if self.reconnect_count:
            self._recovery_kind = TRIGGER_RECONNECT
        elif self._recovery_kind is None:
            self._recovery_kind = "initial"
        await transport.connect()
        await transport.send(encode_ws_message(market_subscribe_payload(self.token_ids)))
        self._subscribed = True
        self._session_started_at = datetime.now(UTC)
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
            self._refresh_status()
            return
        now = datetime.now(UTC)
        results: list[str] = []
        applied_tokens: list[str] = []
        applied_any = False
        for payload in iter_ws_events(parsed):
            event = message_event_type(payload)
            result = self.books.apply_message(payload, received_at=now)
            results.append(result)
            token = str(payload.get("asset_id") or "").strip()
            if not token and event == EVENT_PRICE_CHANGE:
                changes = payload.get("price_changes")
                if isinstance(changes, list):
                    for change in changes:
                        if isinstance(change, dict):
                            asset = str(change.get("asset_id") or "").strip()
                            if asset:
                                applied_tokens.append(asset)
            elif token:
                applied_tokens.append(token)
            if event == EVENT_BOOK and result == "snapshot":
                self.last_snapshot_at = now
                self.last_incremental_at = now
                applied_any = True
            elif event == EVENT_PRICE_CHANGE and result == "delta":
                self.last_incremental_at = now
                applied_any = True
        was_awaiting = self._awaiting_baseline
        complete = bool(self.token_ids) and self.books.healthy_token_count() == len(self.token_ids)
        if complete:
            self.status = STATUS_SUBSCRIBED
            self.last_error = None
            self._awaiting_baseline = False
        elif self.books.resync_count:
            self.status = STATUS_DEGRADED
        baseline_ready = complete and was_awaiting
        recovery = self._recovery_kind if baseline_ready else None
        if baseline_ready:
            self._recovery_kind = None
        if (applied_any or baseline_ready) and self.on_update is not None:
            self.on_update(
                StreamBookSignal(
                    results=tuple(results),
                    books_complete=complete,
                    baseline_ready=baseline_ready,
                    recovery_kind=TRIGGER_RECONNECT if recovery == TRIGGER_RECONNECT else recovery,
                    applied_tokens=tuple(dict.fromkeys(applied_tokens)),
                )
            )
        self._refresh_status()

    def _refresh_status(self) -> None:
        if self._stop.is_set() or not self._subscribed:
            return
        stale = self._stale_tokens()
        if not stale:
            if self.books.healthy_token_count() == len(self.token_ids) and self.token_ids:
                self.status = STATUS_SUBSCRIBED
            return
        self.status = STATUS_STALE
        newly = False
        for token in stale:
            book = self.books.books.get(token)
            if book is None:
                continue
            if book.healthy or book.degraded_reason not in {"unobserved_stale", "awaiting_snapshot"}:
                book.mark_unhealthy("unobserved_stale")
                newly = True
        if newly:
            self.books.resync_count += 1
        self._maybe_resync()

    def _maybe_resync(self) -> None:
        if self._stop.is_set() or not self._subscribed:
            return
        if self._resync_task is not None and not self._resync_task.done():
            return
        now_mono = monotonic()
        if (
            self._last_resync_mono is not None
            and now_mono - self._last_resync_mono < self.resync_min_interval
        ):
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        self._resync_task = loop.create_task(self._resync_subscription(), name="stream-market-resync")

    async def _resync_subscription(self) -> None:
        transport = self._transport
        if transport is None or self._stop.is_set() or not self._subscribed:
            return
        self._last_resync_mono = monotonic()
        self.subscription_resync_count += 1
        self.books.reset()
        self._awaiting_baseline = True
        self._recovery_kind = TRIGGER_RECONNECT
        self._session_started_at = datetime.now(UTC)
        self.status = STATUS_STALE
        try:
            await transport.send(
                encode_ws_message(
                    market_subscribe_payload(self.token_ids, operation=UNSUBSCRIBE_OPERATION)
                )
            )
            await transport.send(encode_ws_message(market_subscribe_payload(self.token_ids)))
        except Exception as exc:
            self.last_error = type(exc).__name__
            raise
