from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx

from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueHealth, VenueName
from sports_hedge.venues.base import ReadOnlyVenue


class MatchbookAuthError(RuntimeError):
    pass


class MatchbookClient(ReadOnlyVenue):
    """Read-only Matchbook market-data client for Sports Hedge Phase 1."""

    name = VenueName.MATCHBOOK

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.matchbook_base_url.rstrip("/"),
            timeout=httpx.Timeout(10.0),
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "gzip",
                "User-Agent": "sports-hedge/0.1 paper-research",
            },
        )
        self._session_token: str | None = None

    async def login(self) -> str:
        username = self.settings.matchbook_username
        password = self.settings.matchbook_password
        if not username or not password:
            raise MatchbookAuthError(
                "MATCHBOOK_USERNAME and MATCHBOOK_PASSWORD are required for Matchbook API access"
            )

        payload: dict[str, str] = {"username": username, "password": password}
        if self.settings.matchbook_mfa_code:
            payload["mfa-code"] = self.settings.matchbook_mfa_code

        response = await self._client.post(
            "/bpapi/rest/security/session",
            json=payload,
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()

        body = response.json()
        token = (
            body.get("session-token")
            or body.get("session_token")
            or response.cookies.get("session-token")
        )
        if not token:
            raise MatchbookAuthError("Matchbook login succeeded but returned no session token")

        self._session_token = str(token)
        self._client.headers["session-token"] = self._session_token
        return self._session_token

    async def _ensure_session(self) -> None:
        if not self._session_token:
            await self.login()

    async def _get(self, path: str, *, params: dict[str, Any]) -> dict[str, Any]:
        await self._ensure_session()
        response = await self._client.get(path, params=params)
        if response.status_code == 401:
            self._session_token = None
            self._client.headers.pop("session-token", None)
            await self.login()
            response = await self._client.get(path, params=params)
        response.raise_for_status()
        return response.json()

    def _market_data_params(self) -> dict[str, Any]:
        return {
            "exchange-type": "back-lay",
            "odds-type": "DECIMAL",
            "currency": self.settings.matchbook_currency,
            "minimum-liquidity": self.settings.matchbook_minimum_liquidity,
            "price-mode": "expanded",
        }

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        params = {
            **self._market_data_params(),
            "states": "open,suspended",
            "include-prices": "false",
            "per-page": 100,
            **filters,
        }
        return await self._get("/edge/rest/events", params=params)

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        params = {
            **self._market_data_params(),
            "states": "open,suspended",
            "include-prices": "true",
            "price-depth": self.settings.matchbook_price_depth,
            "per-page": 100,
            **filters,
        }
        return await self._get(f"/edge/rest/events/{event_id}/markets", params=params)

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        if outcome_id is None:
            raise ValueError("Matchbook order books require a runner/outcome id")
        params = {
            **self._market_data_params(),
            "depth": self.settings.matchbook_price_depth,
            **filters,
        }
        return await self._get(
            f"/edge/rest/events/{event_id}/markets/{market_id}/runners/{outcome_id}/prices",
            params=params,
        )

    async def health(self) -> VenueHealth:
        try:
            await self._ensure_session()
            return VenueHealth(
                venue=self.name,
                ok=True,
                authenticated=True,
                checked_at=datetime.now(UTC),
                detail="Matchbook session authenticated",
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
