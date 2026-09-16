from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueCapabilities, VenueHealth, VenueName
from sports_hedge.venues.base import ReadOnlyVenue, market_data_http_timeout


class PolymarketClient(ReadOnlyVenue):
    """Public-data-only Polymarket adapter.

    Phase 1 deliberately contains no authentication, wallet, signing, or order
    submission code. Only public Gamma/CLOB market-data endpoints are used.
    """

    name = VenueName.POLYMARKET
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
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            timeout=market_data_http_timeout(),
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": "sports-hedge/0.1 paper-research",
            },
        )

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        """List public Gamma events for configured target-competition series.

        Default series IDs come from public ``GET /sports`` for the current
        target competitions. Pagination is per-series, bounded, and read-only.
        Empty series results stay empty rather than inventing markets.
        """

        page_limit = self.settings.polymarket_gamma_page_limit
        params: dict[str, Any] = {
            "active": "true",
            "closed": "false",
            "limit": page_limit,
            **filters,
        }
        caller_series = params.get("series_id")
        if caller_series is not None and str(caller_series).strip() == "":
            params.pop("series_id", None)
            return await self._get_event_page(params)

        if caller_series is not None:
            return await self._list_series_events(str(caller_series), params)

        series_ids = self.settings.resolved_polymarket_series_ids()
        if not series_ids:
            return await self._get_event_page(params)

        events: list[dict[str, Any]] = []
        seen: set[str] = set()
        for series_id in series_ids:
            for item in await self._list_series_events(series_id, params):
                event_id = str(item.get("id", "")).strip()
                if event_id and event_id in seen:
                    continue
                if event_id:
                    seen.add(event_id)
                events.append(item)
        return events

    async def _list_series_events(
        self,
        series_id: str,
        base_params: dict[str, Any],
    ) -> list[dict[str, Any]]:
        events: list[dict[str, Any]] = []
        page_limit = int(base_params.get("limit") or self.settings.polymarket_gamma_page_limit)
        max_pages = self.settings.polymarket_gamma_max_pages_per_series
        for page in range(max_pages):
            params = {
                **base_params,
                "series_id": series_id,
                "limit": page_limit,
                "offset": page * page_limit,
            }
            page_items = await self._get_event_page(params)
            events.extend(page_items)
            if len(page_items) < page_limit:
                break
        return events

    async def _get_event_page(self, params: dict[str, Any]) -> list[dict[str, Any]]:
        response = await self._client.get(
            f"{self.settings.polymarket_gamma_base_url.rstrip('/')}/events",
            params=params,
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)]
        return []

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        response = await self._client.get(
            f"{self.settings.polymarket_gamma_base_url.rstrip('/')}/events/{event_id}",
            params=filters,
        )
        response.raise_for_status()
        payload = response.json()
        markets = payload.get("markets", []) if isinstance(payload, dict) else []
        return markets

    async def get_market(self, market_id: int | str) -> dict[str, Any]:
        response = await self._client.get(
            f"{self.settings.polymarket_gamma_base_url.rstrip('/')}/markets/{market_id}"
        )
        response.raise_for_status()
        return response.json()

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id  # CLOB book lookup is token/outcome-id based.
        if outcome_id is None:
            raise ValueError("Polymarket order books require a CLOB token id")
        params = {"token_id": str(outcome_id), **filters}
        response = await self._client.get(
            f"{self.settings.polymarket_clob_base_url.rstrip('/')}/book",
            params=params,
        )
        response.raise_for_status()
        return response.json()

    async def health(self) -> VenueHealth:
        try:
            response = await self._client.get(
                f"{self.settings.polymarket_gamma_base_url.rstrip('/')}/events",
                params={"limit": 1},
            )
            response.raise_for_status()
            return VenueHealth(
                venue=self.name,
                ok=True,
                authenticated=False,
                checked_at=datetime.now(UTC),
                detail="Public Gamma/CLOB market data reachable; execution disabled",
            )
        except Exception as exc:  # health endpoint should report, not raise
            return VenueHealth(
                venue=self.name,
                ok=False,
                authenticated=False,
                checked_at=datetime.now(UTC),
                detail=str(exc),
            )

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()
