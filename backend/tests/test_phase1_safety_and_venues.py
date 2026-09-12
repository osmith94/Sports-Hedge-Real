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


@pytest.mark.asyncio
async def test_polymarket_list_events_applies_configured_epl_series_filter() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        if request.url.path == "/events":
            return httpx.Response(200, json=[{"id": "934146", "title": "Chelsea FC vs. Hull City AFC"}])
        return httpx.Response(404)

    settings = Settings(polymarket_gamma_series_id="10188")
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=settings.polymarket_gamma_base_url,
    ) as http:
        venue = PolymarketClient(settings, client=http)
        events = await venue.list_events()
        overridden = await venue.list_events(series_id="99999")

    assert events[0]["id"] == "934146"
    assert seen[0].params["series_id"] == "10188"
    assert seen[0].params["limit"] == "100"
    assert seen[1].params["series_id"] == "99999"
    assert overridden[0]["id"] == "934146"


@pytest.mark.asyncio
async def test_polymarket_list_events_omits_series_when_filter_disabled() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        return httpx.Response(200, json=[])

    settings = Settings(polymarket_gamma_series_id="")
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=settings.polymarket_gamma_base_url,
    ) as http:
        venue = PolymarketClient(settings, client=http)
        await venue.list_events()

    assert "series_id" not in seen[0].params


@pytest.mark.asyncio
async def test_polymarket_list_events_fetches_target_series_and_paginates() -> None:
    seen: list[httpx.URL] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url)
        series = request.url.params.get("series_id")
        offset = request.url.params.get("offset", "0")
        if request.url.path != "/events":
            return httpx.Response(404)
        if series == "10188" and offset == "0":
            return httpx.Response(
                200,
                json=[{"id": "epl-1", "title": "Chelsea vs Hull", "series": [{"title": "Premier League"}]}],
            )
        if series == "10355" and offset == "0":
            return httpx.Response(
                200,
                json=[
                    {"id": f"elc-{index}", "title": f"Club {index} vs Club B"}
                    for index in range(100)
                ],
            )
        if series == "10355" and offset == "100":
            return httpx.Response(200, json=[{"id": "elc-page-2", "title": "Leeds vs Leicester"}])
        if series == "10193":
            return httpx.Response(200, json=[])
        return httpx.Response(200, json=[])

    settings = Settings(polymarket_gamma_page_limit=100, polymarket_gamma_max_pages_per_series=5)
    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        transport=transport,
        base_url=settings.polymarket_gamma_base_url,
    ) as http:
        venue = PolymarketClient(settings, client=http)
        events = await venue.list_events()

    series_ids = [url.params.get("series_id") for url in seen]
    assert series_ids.count("10188") == 1
    assert series_ids.count("10355") == 2
    assert series_ids.count("10193") == 1
    ids = [item["id"] for item in events]
    assert "epl-1" in ids
    assert "elc-page-2" in ids
    assert len(events) == 102

