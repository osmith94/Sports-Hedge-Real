from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueCapabilities, VenueHealth, VenueName
from sports_hedge.venues.base import ReadOnlyVenue

_FOOTBALL_SERIES_TOKENS = frozenset(
    {"soccer", "football", "association football", "epl", "premier league", "la liga", "championship"}
)


class KalshiDiscoveryError(RuntimeError):
    """Raised when Kalshi market-data discovery cannot proceed without guessing."""


class KalshiClient(ReadOnlyVenue):
    """Public Kalshi Trade API v2 market-data client.

    Phase 1 uses unauthenticated market-data endpoints only. There is no order
    placement, cancellation, signing, or portfolio mutation here. Capability
    flags match Matchbook: data and paper enabled, execution disabled because
    Sports Hedge globally has real execution off in Phase 1.
    """

    name = VenueName.KALSHI
    capabilities = VenueCapabilities(
        data_enabled=True,
        paper_enabled=True,
        execution_enabled=False,
    )

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._clock = clock or (lambda: datetime.now(UTC))
        self._base_url = settings.resolved_kalshi_base_url().rstrip("/")
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(10.0),
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": "sports-hedge/0.1 paper-research",
            },
        )

    async def _get(self, path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        url = path if str(path).startswith("http") else f"{self._base_url}{path}"
        response = await self._client.get(url, params=params)
        try:
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise KalshiDiscoveryError(
                f"Kalshi GET {path} failed with HTTP {response.status_code}"
            ) from exc
        payload = response.json()
        return payload if isinstance(payload, dict) else {}

    async def list_series(self, **filters: Any) -> dict[str, Any]:
        limit = int(filters.get("limit") or self.settings.kalshi_event_page_limit)
        params = {**filters, "limit": limit}
        if "cursor" in filters:
            page = await self._get("/series", params=params)
            return {**page, "series": _extract_items(page, "series"), "truncated": False}
        return await self._paginate(
            "/series",
            params=params,
            item_key="series",
            limit=limit,
            max_pages=self.settings.kalshi_event_max_pages,
        )

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        payload = await self._get(f"/series/{series_ticker}")
        series = payload.get("series")
        if isinstance(series, dict):
            return series
        return payload

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        """List open Kalshi events, optionally restricted to configured series."""

        limit = int(filters.get("limit") or self.settings.kalshi_event_page_limit)
        series_tickers = _requested_series(filters, self.settings.kalshi_series_tickers)
        if "cursor" in filters or filters.get("series_ticker"):
            params = {
                "status": filters.get("status", "open"),
                "limit": limit,
                "with_nested_markets": filters.get("with_nested_markets", "true"),
                **{key: value for key, value in filters.items() if key != "series_tickers"},
            }
            page = await self._get("/events", params=params)
            events = _extract_items(page_payload(page), "events")
            return {**page, "events": events, "truncated": False}

        if series_tickers:
            events: list[dict[str, Any]] = []
            seen: set[str] = set()
            truncated = False
            for ticker in series_tickers:
                page = await self._paginate(
                    "/events",
                    params={
                        "status": filters.get("status", "open"),
                        "limit": limit,
                        "series_ticker": ticker,
                        "with_nested_markets": filters.get("with_nested_markets", "true"),
                    },
                    item_key="events",
                    limit=limit,
                    max_pages=self.settings.kalshi_event_max_pages,
                )
                truncated = truncated or bool(page.get("truncated"))
                for item in page.get("events", []):
                    event_id = str(item.get("event_ticker") or item.get("ticker") or "").strip()
                    if event_id and event_id in seen:
                        continue
                    if event_id:
                        seen.add(event_id)
                    events.append(item)
            return {"events": events, "truncated": truncated, "total": len(events)}

        return await self._paginate(
            "/events",
            params={
                "status": filters.get("status", "open"),
                "limit": limit,
                "with_nested_markets": filters.get("with_nested_markets", "true"),
            },
            item_key="events",
            limit=limit,
            max_pages=self.settings.kalshi_event_max_pages,
        )

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        limit = int(filters.get("limit") or self.settings.kalshi_event_page_limit)
        params = {
            "event_ticker": str(event_id),
            "limit": limit,
            "status": filters.get("status", "open"),
            **{key: value for key, value in filters.items() if key not in {"event_ticker", "limit"}},
        }
        if "cursor" in filters:
            page = await self._get("/markets", params=params)
            return {**page, "markets": _extract_items(page, "markets"), "truncated": False}
        return await self._paginate(
            "/markets",
            params=params,
            item_key="markets",
            limit=limit,
            max_pages=self.settings.kalshi_event_max_pages,
        )

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, outcome_id
        ticker = str(market_id)
        params = {**filters}
        payload = await self._get(f"/markets/{ticker}/orderbook", params=params)
        if "orderbook_fp" not in payload and "orderbook" in payload:
            raise KalshiDiscoveryError(
                f"Kalshi orderbook for {ticker} lacked orderbook_fp dollar/fixed-point fields"
            )
        return payload

    async def health(self) -> VenueHealth:
        try:
            await self._get("/exchange/status")
            return VenueHealth(
                venue=self.name,
                ok=True,
                authenticated=False,
                checked_at=self._clock(),
                detail="Public Kalshi market data reachable; execution disabled (Phase 1)",
            )
        except Exception as exc:  # health reports, does not raise
            try:
                await self._get("/events", params={"limit": 1, "status": "open"})
                return VenueHealth(
                    venue=self.name,
                    ok=True,
                    authenticated=False,
                    checked_at=self._clock(),
                    detail="Public Kalshi events reachable; execution disabled (Phase 1)",
                )
            except Exception:
                return VenueHealth(
                    venue=self.name,
                    ok=False,
                    authenticated=False,
                    checked_at=self._clock(),
                    detail=str(exc),
                )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def _paginate(
        self,
        path: str,
        *,
        params: dict[str, Any],
        item_key: str,
        limit: int,
        max_pages: int,
    ) -> dict[str, Any]:
        items: list[dict[str, Any]] = []
        cursor: str | None = None
        truncated = False
        last_page: dict[str, Any] = {}
        for page_index in range(max_pages):
            page_params = {**params, "limit": limit}
            if cursor:
                page_params["cursor"] = cursor
            last_page = await self._get(path, params=page_params)
            page_items = _extract_items(last_page, item_key)
            items.extend(page_items)
            cursor = str(last_page.get("cursor") or "").strip() or None
            if not page_items or cursor is None:
                break
            if page_index == max_pages - 1 and cursor:
                truncated = True
        else:
            truncated = True
        return {
            **last_page,
            item_key: items,
            "truncated": truncated,
            "total": len(items),
        }


def football_series_ticker(series: dict[str, Any]) -> bool:
    """True when series metadata is association-football relevant. Not NFL."""

    tokens = " ".join(
        [
            str(series.get("ticker") or ""),
            str(series.get("title") or ""),
            str(series.get("category") or ""),
            " ".join(str(tag) for tag in (series.get("tags") or []) if tag),
        ]
    ).casefold()
    if "nfl" in tokens or "american football" in tokens:
        return False
    return any(token in tokens for token in _FOOTBALL_SERIES_TOKENS)


def page_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return payload


def _requested_series(filters: dict[str, Any], configured: list[str]) -> list[str]:
    explicit = filters.get("series_tickers")
    if explicit is not None:
        if isinstance(explicit, str):
            return [part.strip() for part in explicit.split(",") if part.strip()]
        return [str(item).strip() for item in explicit if str(item).strip()]
    ticker = filters.get("series_ticker")
    if ticker:
        return [str(ticker).strip()]
    return [item.strip() for item in configured if str(item).strip()]


def _extract_items(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key, [])
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    nested = payload.get("data")
    if isinstance(nested, dict) and isinstance(nested.get(key), list):
        return [item for item in nested[key] if isinstance(item, dict)]
    return []
