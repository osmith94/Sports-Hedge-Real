from __future__ import annotations

import httpx
import pytest
from pydantic import ValidationError

from sports_hedge.config import Settings
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient


def test_phase1_rejects_live_execution() -> None:
    with pytest.raises(ValidationError, match="Live execution is intentionally unavailable"):
        Settings(sports_hedge_execution_enabled=True)


@pytest.mark.asyncio
async def test_matchbook_login_and_event_read_are_read_only() -> None:
    seen_paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_paths.append(request.url.path)
        if request.url.path == "/bpapi/rest/security/session":
            return httpx.Response(200, json={"session-token": "paper-session"})
        if request.url.path == "/edge/rest/events":
            assert request.headers["session-token"] == "paper-session"
            assert request.url.params["currency"] == "GBP"
            return httpx.Response(200, json={"events": []})
        return httpx.Response(404)

    settings = Settings(
        matchbook_username="test-user",
        matchbook_password="test-password",
    )
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=settings.matchbook_base_url,
    ) as http:
        venue = MatchbookClient(settings, client=http)
        result = await venue.list_events()

    assert result == {"events": []}
    assert seen_paths == ["/bpapi/rest/security/session", "/edge/rest/events"]
    assert venue.capabilities.execution_enabled is False
    assert not hasattr(venue, "place_order")


@pytest.mark.asyncio
async def test_polymarket_public_order_book_has_no_execution_capability() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "clob.polymarket.com" and request.url.path == "/book":
            assert request.url.params["token_id"] == "token-123"
            return httpx.Response(
                200,
                json={
                    "asset_id": "token-123",
                    "bids": [{"price": "0.45", "size": "100"}],
                    "asks": [{"price": "0.46", "size": "120"}],
                    "tick_size": "0.01",
                    "min_order_size": "1",
                },
            )
        return httpx.Response(404)

    settings = Settings()
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as http:
        venue = PolymarketClient(settings, client=http)
        book = await venue.get_order_book("event", "market", "token-123")

    assert book["asset_id"] == "token-123"
    assert venue.capabilities.execution_enabled is False
    assert not hasattr(venue, "place_order")
