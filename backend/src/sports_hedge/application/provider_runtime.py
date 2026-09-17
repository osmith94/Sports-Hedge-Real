"""Process-shared venue clients, HTTP sessions, and cooldown state.

HOT and UNIVERSE must observe the same provider-wide session and cooldown
truth. Creating a new Polymarket/Kalshi HTTP client per collect recreates
rate-limit pressure and hides cooldown from the other lane.

Matchbook already has a process-local client; this runtime reuses it and
owns the Polymarket/Kalshi sessions.
"""

from __future__ import annotations

import threading
from typing import Any

import httpx

from sports_hedge.application.provider_access import (
    ProviderAccessLayer,
    get_shared_provider_access,
)
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.venues.base import market_data_http_timeout
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import get_shared_matchbook_client
from sports_hedge.venues.polymarket import PolymarketClient
from sports_hedge.venues.rate_limit import ProviderCooldown, RateLimitPolicy

_LOCK = threading.Lock()
_SHARED: SharedProviderRuntime | None = None


def _public_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=market_data_http_timeout(),
        headers={
            "Accept": "application/json",
            "Accept-Encoding": "gzip",
            "User-Agent": "sports-hedge/0.1 paper-research",
        },
    )


class SharedProviderRuntime:
    """One shared manager for concurrent HOT and UNIVERSE provider access."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        access: ProviderAccessLayer | None = None,
        matchbook: Any = None,
        polymarket: Any = None,
        kalshi: Any = None,
    ) -> None:
        resolved = settings or get_settings()
        self.settings = resolved
        self.access = access or get_shared_provider_access(resolved)
        self.cooldowns = {
            VenueName.MATCHBOOK: ProviderCooldown(
                RateLimitPolicy(provider=VenueName.MATCHBOOK.value)
            ),
            VenueName.POLYMARKET: ProviderCooldown(
                RateLimitPolicy(provider=VenueName.POLYMARKET.value)
            ),
            VenueName.KALSHI: ProviderCooldown(
                RateLimitPolicy(provider=VenueName.KALSHI.value)
            ),
        }
        self._closed = False
        self._close_lock = threading.Lock()
        self._close_count = 0
        self._matchbook = matchbook or get_shared_matchbook_client(resolved)
        self._owns_pm_http = False
        self._owns_k_http = False
        if polymarket is not None:
            self._polymarket = polymarket
            http = getattr(polymarket, "_client", None)
            self._pm_http = http if isinstance(http, httpx.AsyncClient) else None
        else:
            self._pm_http = _public_http_client()
            self._owns_pm_http = True
            self._polymarket = PolymarketClient(
                resolved,
                client=self._pm_http,
                cooldown=self.cooldowns[VenueName.POLYMARKET],
            )
        if kalshi is not None:
            self._kalshi = kalshi
            http = getattr(kalshi, "_client", None)
            self._k_http = http if isinstance(http, httpx.AsyncClient) else None
        else:
            self._k_http = _public_http_client()
            self._owns_k_http = True
            self._kalshi = KalshiClient(
                resolved,
                client=self._k_http,
                cooldown=self.cooldowns[VenueName.KALSHI],
            )

    @property
    def matchbook(self) -> Any:
        return self._matchbook

    @property
    def polymarket(self) -> PolymarketClient:
        return self._polymarket

    @property
    def kalshi(self) -> KalshiClient:
        return self._kalshi

    def client(self, venue: VenueName):
        if venue is VenueName.MATCHBOOK:
            return self._matchbook
        if venue is VenueName.POLYMARKET:
            return self._polymarket
        if venue is VenueName.KALSHI:
            return self._kalshi
        raise KeyError(venue)

    def cooldown(self, venue: VenueName) -> ProviderCooldown:
        return self.cooldowns[venue]

    def http_client(self, venue: VenueName) -> httpx.AsyncClient | None:
        if venue is VenueName.POLYMARKET:
            return self._pm_http
        if venue is VenueName.KALSHI:
            return self._k_http
        matchbook_http = getattr(self._matchbook, "_client", None)
        return matchbook_http if isinstance(matchbook_http, httpx.AsyncClient) else None

    async def aclose(self) -> None:
        """Close shared HTTP clients exactly once."""

        with self._close_lock:
            if self._closed:
                return
            self._closed = True
            self._close_count += 1
        if self._owns_pm_http and self._pm_http is not None:
            await self._pm_http.aclose()
        if self._owns_k_http and self._k_http is not None:
            await self._k_http.aclose()

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def close_count(self) -> int:
        return self._close_count


def get_shared_provider_runtime(settings: Settings | None = None) -> SharedProviderRuntime:
    global _SHARED
    with _LOCK:
        current = _SHARED
        if current is None or current.closed:
            _SHARED = SharedProviderRuntime(settings)
        return _SHARED


def set_shared_provider_runtime(runtime: SharedProviderRuntime | None) -> None:
    global _SHARED
    with _LOCK:
        _SHARED = runtime


async def aclose_shared_provider_runtime() -> None:
    global _SHARED
    with _LOCK:
        runtime = _SHARED
        _SHARED = None
    if runtime is not None:
        await runtime.aclose()


async def reset_shared_provider_runtime() -> None:
    await aclose_shared_provider_runtime()
