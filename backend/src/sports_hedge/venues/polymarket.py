from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueCapabilities, VenueHealth, VenueName
from sports_hedge.venues.base import ReadOnlyVenue


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
            timeout=httpx.Timeout(10.0),
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": "sports-hedge/0.1 paper-research",
            },
        )

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        params: dict[str, Any] = {
            "active": "true",
            "closed": "false",
            "limit": 100,
            **filters,
        }
        response = await self._client.get(
            f"{self.settings.polymarket_gamma_base_url.rstrip('/')}/events",
            params=params,
        )
        response.raise_for_status()
        return response.json()

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
