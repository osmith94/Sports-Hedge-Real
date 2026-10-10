"""Injectable market-channel transports. Tests use the fake; production uses WS."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Protocol

from sports_hedge.application.stream.protocol import POLYMARKET_MARKET_WS_URL


class StreamTransportClosed(RuntimeError):
    """Market-channel socket closed or recv ended."""


class MarketWsTransport(Protocol):
    async def connect(self) -> None: ...
    async def send(self, text: str) -> None: ...
    async def recv(self) -> str: ...
    async def close(self) -> None: ...


class FakeMarketWsTransport:
    """Deterministic in-process WS stand-in. No network."""

    def __init__(self, *, auto_pong: bool = True) -> None:
        self.auto_pong = auto_pong
        self.sent: list[str] = []
        self.connect_count = 0
        self.close_count = 0
        self.closed = True
        self._incoming: asyncio.Queue[str | BaseException | None] = asyncio.Queue()

    async def connect(self) -> None:
        self.connect_count += 1
        self.closed = False
        while not self._incoming.empty():
            try:
                self._incoming.get_nowait()
            except asyncio.QueueEmpty:
                break

    async def send(self, text: str) -> None:
        if self.closed:
            raise StreamTransportClosed("fake_ws_closed")
        self.sent.append(text)
        if self.auto_pong and text == "PING":
            await self._incoming.put("PONG")

    async def recv(self) -> str:
        if self.closed and self._incoming.empty():
            raise StreamTransportClosed("fake_ws_closed")
        item = await self._incoming.get()
        if item is None:
            self.closed = True
            raise StreamTransportClosed("fake_ws_eof")
        if isinstance(item, BaseException):
            raise item
        return item

    async def close(self) -> None:
        self.close_count += 1
        self.closed = True
        if not self._incoming.empty():
            return
        self._incoming.put_nowait(None)

    def push(self, message: str) -> None:
        self._incoming.put_nowait(message)

    def fail(self, exc: BaseException) -> None:
        self._incoming.put_nowait(exc)


class WebsocketMarketTransport:
    """Public Polymarket market channel. No auth headers or user-channel payloads."""

    def __init__(self, url: str = POLYMARKET_MARKET_WS_URL) -> None:
        self.url = url
        self._ws: object | None = None

    async def connect(self) -> None:
        try:
            import websockets
        except ImportError as exc:
            raise RuntimeError("polymarket_market_ws_unavailable") from exc
        self._ws = await websockets.connect(
            self.url,
            ping_interval=None,
            ping_timeout=None,
            close_timeout=2,
            max_size=2**20,
        )

    async def send(self, text: str) -> None:
        ws = self._require()
        await ws.send(text)

    async def recv(self) -> str:
        ws = self._require()
        raw = await ws.recv()
        if isinstance(raw, bytes):
            return raw.decode("utf-8")
        return str(raw)

    async def close(self) -> None:
        ws = self._ws
        self._ws = None
        if ws is None:
            return
        close = getattr(ws, "close", None)
        if callable(close):
            await close()

    def _require(self) -> AnyWs:
        if self._ws is None:
            raise StreamTransportClosed("market_ws_not_connected")
        return self._ws  # type: ignore[return-value]


class AnyWs(Protocol):
    def send(self, text: str) -> Awaitable[None]: ...
    def recv(self) -> Awaitable[str | bytes]: ...
    def close(self) -> Awaitable[None]: ...


TransportFactory = Callable[[], MarketWsTransport]
