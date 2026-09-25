"""Issue #542: honor operator-selected Polymarket series_ids.

``series_ids`` is a client-side discovery control. Gamma requests receive
singular ``series_id`` only. Explicit plural selection wins over the settings
default list, which does not include UEFA Nations League series 11446.
Provider responses in this file are fixtures.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import httpx
import pytest

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.venues.polymarket import PolymarketClient

UNL = "uefa_nations_league"
NATIONS_LEAGUE_SERIES = "11446"


def _gamma_client(settings: Settings, handler: Any) -> tuple[httpx.AsyncClient, PolymarketClient]:
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url=settings.polymarket_gamma_base_url)
    return http, PolymarketClient(settings, client=http)


def _assert_series_ids_not_forwarded(seen: list[httpx.Request]) -> None:
    for request in seen:
        assert "series_ids" not in request.url.params
        assert "series_ids" not in str(request.url)


class _IdleVenue:
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        raise AssertionError("this venue must not be discovered in the selected-series test")


@pytest.mark.asyncio
async def test_explicit_series_ids_query_nations_league_absent_from_settings_default() -> None:
    seen: list[httpx.Request] = []
    settings = Settings()
    assert NATIONS_LEAGUE_SERIES not in settings.resolved_polymarket_series_ids()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        series = request.url.params.get("series_id")
        if request.url.path == "/events" and series == NATIONS_LEAGUE_SERIES:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "pm-andorra-malta",
                        "title": "Andorra vs Malta",
                        "series": [{"id": NATIONS_LEAGUE_SERIES, "title": "UEFA Nations League"}],
                    }
                ],
            )
        return httpx.Response(200, json=[])

    http, venue = _gamma_client(settings, handler)
    async with http:
        events = await venue.list_events(series_ids=[NATIONS_LEAGUE_SERIES])

    assert [request.url.params.get("series_id") for request in seen] == [NATIONS_LEAGUE_SERIES]
    _assert_series_ids_not_forwarded(seen)
    assert [item["id"] for item in events] == ["pm-andorra-malta"]
    assert venue.last_series_report == [
        {
            "series": NATIONS_LEAGUE_SERIES,
            "status": "ok",
            "retryable": False,
            "event_count": 1,
            "pages_attempted": 1,
            "http_attempted": True,
            "empty": False,
            "reason": None,
        }
    ]


@pytest.mark.asyncio
async def test_collector_selected_nations_league_queries_series_11446() -> None:
    seen: list[httpx.Request] = []
    settings = Settings()
    assert NATIONS_LEAGUE_SERIES not in settings.resolved_polymarket_series_ids()

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        series = request.url.params.get("series_id")
        if request.url.path == "/events" and series == NATIONS_LEAGUE_SERIES:
            return httpx.Response(
                200,
                json=[
                    {
                        "id": "pm-andorra-malta",
                        "title": "Andorra vs Malta",
                        "startTime": "2026-09-24T18:45:00Z",
                        "series": [{"id": NATIONS_LEAGUE_SERIES, "title": "UEFA Nations League"}],
                    }
                ],
            )
        if request.url.path.startswith("/events/"):
            return httpx.Response(200, json={"markets": []})
        return httpx.Response(200, json=[])

    repository = SqliteMarketIntelligenceRepository()
    http, polymarket = _gamma_client(settings, handler)
    collector = ReadOnlyCrossVenueCollector(
        matchbook=_IdleVenue(),
        polymarket=polymarket,
        kalshi=None,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        async with http:
            report = await collector.collect_and_scan(
                selected_competition_codes=[UNL],
                enabled_venues=[VenueName.POLYMARKET],
                unbounded_cycle=True,
                polymarket_discovery_now=datetime(2026, 9, 24, 12, 0, tzinfo=UTC),
            )
    finally:
        repository.close()

    discovery = [request for request in seen if request.url.path == "/events"]
    assert [request.url.params.get("series_id") for request in discovery] == [NATIONS_LEAGUE_SERIES]
    _assert_series_ids_not_forwarded(seen)
    assert report.raw_polymarket_events == 1
    assert any(request.url.path == "/events/pm-andorra-malta" for request in seen)
    assert "10188" not in {request.url.params.get("series_id") for request in seen}


@pytest.mark.asyncio
async def test_series_ids_are_deduped_and_queried_in_order() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    settings = Settings()
    http, venue = _gamma_client(settings, handler)
    async with http:
        await venue.list_events(series_ids=["11446", "10188", "11446", " 10193 ", "", 11446])

    assert [request.url.params.get("series_id") for request in seen] == [
        "11446",
        "10188",
        "10193",
    ]
    _assert_series_ids_not_forwarded(seen)


@pytest.mark.asyncio
async def test_singular_series_id_still_queries_exactly_that_series() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.params.get("series_id") == "99999":
            return httpx.Response(200, json=[{"id": "only-singular"}])
        return httpx.Response(200, json=[])

    settings = Settings()
    http, venue = _gamma_client(settings, handler)
    async with http:
        events = await venue.list_events(
            series_id="99999",
            series_ids=["11446", "10188"],
        )
        disabled = await venue.list_events(series_id="", series_ids=["11446"])

    assert [request.url.params.get("series_id") for request in seen[:1]] == ["99999"]
    assert events[0]["id"] == "only-singular"
    assert "series_id" not in seen[1].url.params
    assert disabled == []
    _assert_series_ids_not_forwarded(seen)


@pytest.mark.asyncio
async def test_missing_series_selection_keeps_settings_default() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[])

    settings = Settings()
    defaults = settings.resolved_polymarket_series_ids()
    assert defaults
    assert NATIONS_LEAGUE_SERIES not in defaults
    http, venue = _gamma_client(settings, handler)
    async with http:
        await venue.list_events()

    assert [request.url.params.get("series_id") for request in seen] == defaults
    _assert_series_ids_not_forwarded(seen)


@pytest.mark.asyncio
async def test_explicit_empty_series_ids_do_not_fall_back_to_settings() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json=[{"id": "should-not-query"}])

    settings = Settings()
    http, venue = _gamma_client(settings, handler)
    async with http:
        events = await venue.list_events(series_ids=[])

    assert events == []
    assert seen == []
    assert venue.last_series_report == []
