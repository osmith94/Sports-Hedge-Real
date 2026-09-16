"""Issue #247: process-local Matchbook session reuse and 429 login backoff.

Characterization against frozen R1 head 4886366 (2026-09-16):
`_collect_report()` constructed `MatchbookClient(settings)` and closed it after
every HOT/UNIVERSE/manual collection. Two sequential cycles therefore POSTed
`/bpapi/rest/security/session` twice (CLIENTS_CREATED=2, SESSION_POSTS=2).
Health constructed a third client. After this change those cycles share one
login, and 429 cools down without a login storm.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from email.utils import format_datetime
from typing import Any

import httpx
import pytest

from sports_hedge.api import main as main_api
from sports_hedge.api import paper as paper_api
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueHealth, VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.venues.matchbook import (
    MATCHBOOK_SESSION_PATH,
    MatchbookClient,
    MatchbookRateLimitedError,
    _retry_after_seconds,
    aclose_shared_matchbook_client,
    get_shared_matchbook_client,
    reset_shared_matchbook_client,
    set_shared_matchbook_client,
)

PASSWORD = "test-password-never-log"
USERNAME = "test-user"


class FakeMono:
    def __init__(self, value: float = 1_000.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value


class _EmptyOtherVenue:
    name = VenueName.POLYMARKET

    def __init__(self, venue: VenueName | None = None) -> None:
        if venue is not None:
            self.name = venue

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return []

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return []

    async def get_order_book(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {}

    async def health(self) -> VenueHealth:
        return VenueHealth(
            venue=self.name,
            ok=True,
            authenticated=False,
            checked_at=datetime.now(UTC),
            detail="stub",
        )

    async def aclose(self) -> None:
        return None


def _settings() -> Settings:
    return Settings(matchbook_username=USERNAME, matchbook_password=PASSWORD)


def _paper_service() -> PaperScanService:
    return PaperScanService(MarketIntelligenceService(SqliteMarketIntelligenceRepository()))


def _football_ok() -> httpx.Response:
    return httpx.Response(
        200,
        json={"sports": [{"id": 15, "name": "Football", "type": "SPORT"}]},
    )


def _events_ok() -> httpx.Response:
    return httpx.Response(200, json={"events": [], "total": 0, "per-page": 100})


@pytest.fixture
async def isolated_shared_matchbook() -> Any:
    await reset_shared_matchbook_client()
    yield
    await reset_shared_matchbook_client()


def _secret_free(text: str | None) -> None:
    blob = text or ""
    assert PASSWORD not in blob
    assert USERNAME not in blob


async def _client_for(
    handler: Any,
    *,
    settings: Settings | None = None,
    monotonic_clock: FakeMono | None = None,
    login_cooldown_seconds: float = 5.0,
    login_cooldown_max_seconds: float = 30.0,
    clock: Any = None,
) -> tuple[MatchbookClient, httpx.AsyncClient]:
    resolved = settings or _settings()
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport, base_url=resolved.matchbook_base_url)
    venue = MatchbookClient(
        resolved,
        client=http,
        clock=clock,
        monotonic_clock=monotonic_clock,
        login_cooldown_seconds=login_cooldown_seconds,
        login_cooldown_max_seconds=login_cooldown_max_seconds,
    )
    return venue, http


@pytest.mark.asyncio
async def test_two_sequential_list_events_reuse_one_login() -> None:
    session_posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MATCHBOOK_SESSION_PATH:
            session_posts.append(request.method)
            return httpx.Response(200, json={"session-token": "session-1"})
        if request.url.path == "/edge/rest/lookups/sports":
            return _football_ok()
        if request.url.path == "/edge/rest/events":
            return _events_ok()
        return httpx.Response(404)

    venue, http = await _client_for(handler)
    async with http:
        await venue.list_events()
        await venue.list_events()
    assert session_posts == ["POST"]
    assert not hasattr(venue, "place_order")
    assert not hasattr(venue, "cancel_order")
    assert venue.capabilities.execution_enabled is False


@pytest.mark.asyncio
async def test_hot_and_universe_collect_cycles_share_one_login(
    isolated_shared_matchbook: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_posts: list[str] = []
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MATCHBOOK_SESSION_PATH:
            session_posts.append(request.method)
            return httpx.Response(200, json={"session-token": "session-shared"})
        if request.url.path == "/edge/rest/lookups/sports":
            return _football_ok()
        if request.url.path == "/edge/rest/events":
            return _events_ok()
        return httpx.Response(404)

    venue, http = await _client_for(handler, settings=settings)
    set_shared_matchbook_client(venue)
    monkeypatch.setattr(paper_api, "get_settings", lambda: settings)
    monkeypatch.setattr(paper_api, "PolymarketClient", lambda _s: _EmptyOtherVenue())
    monkeypatch.setattr(paper_api, "KalshiClient", lambda _s: _EmptyOtherVenue())
    service = _paper_service()
    async with http:
        await paper_api._collect_report({}, service=service, scan_lane=ScanLane.HOT)
        await paper_api._collect_report({}, service=service, scan_lane=ScanLane.UNIVERSE)
        assert venue._session_token == "session-shared"
        assert not venue.closed
    assert session_posts == ["POST"]
    collect_src = inspect.getsource(paper_api._collect_report)
    assert "get_shared_matchbook_client(settings)" in collect_src
    assert "MatchbookClient(settings)" not in collect_src
    assert "_aclose_soon(matchbook" not in collect_src


@pytest.mark.asyncio
async def test_two_sequential_scan_cycles_reuse_one_login(
    isolated_shared_matchbook: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_posts: list[str] = []
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MATCHBOOK_SESSION_PATH:
            session_posts.append(request.method)
            return httpx.Response(200, json={"session-token": "session-cycle"})
        if request.url.path == "/edge/rest/lookups/sports":
            return _football_ok()
        if request.url.path == "/edge/rest/events":
            return _events_ok()
        return httpx.Response(404)

    venue, http = await _client_for(handler, settings=settings)
    set_shared_matchbook_client(venue)
    monkeypatch.setattr(paper_api, "get_settings", lambda: settings)
    monkeypatch.setattr(paper_api, "PolymarketClient", lambda _s: _EmptyOtherVenue())
    monkeypatch.setattr(paper_api, "KalshiClient", lambda _s: _EmptyOtherVenue())
    service = _paper_service()
    async with http:
        await paper_api._collect_report({}, service=service, scan_lane=ScanLane.HOT)
        await paper_api._collect_report({}, service=service, scan_lane=ScanLane.HOT)
    assert session_posts == ["POST"]


@pytest.mark.asyncio
async def test_401_clears_token_and_relogins_once() -> None:
    session_posts: list[str] = []
    tokens: list[str] = []
    expired: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MATCHBOOK_SESSION_PATH:
            token = f"session-{len(tokens) + 1}"
            tokens.append(token)
            session_posts.append(token)
            return httpx.Response(200, json={"session-token": token})
        token = request.headers.get("session-token")
        if token in expired or token not in tokens:
            return httpx.Response(401, json={"error": "unauthorized"})
        if request.url.path == "/edge/rest/lookups/sports":
            return _football_ok()
        if request.url.path == "/edge/rest/events":
            return _events_ok()
        return httpx.Response(404)

    venue, http = await _client_for(handler)
    async with http:
        await venue.list_events()
        assert session_posts == ["session-1"]
        expired.add("session-1")
        result = await venue.list_events()
    assert result["events"] == []
    assert session_posts == ["session-1", "session-2"]
    assert venue._session_token == "session-2"


@pytest.mark.asyncio
async def test_concurrent_requests_after_expiry_single_flight_relogin() -> None:
    session_posts: list[str] = []
    tokens: list[str] = []
    expired: set[str] = set()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MATCHBOOK_SESSION_PATH:
            token = f"session-{len(tokens) + 1}"
            tokens.append(token)
            session_posts.append(token)
            return httpx.Response(200, json={"session-token": token})
        token = request.headers.get("session-token")
        if token in expired or token not in tokens:
            return httpx.Response(401, json={"error": "unauthorized"})
        if request.url.path == "/edge/rest/lookups/sports":
            return _football_ok()
        if request.url.path == "/edge/rest/events":
            return _events_ok()
        return httpx.Response(404)

    venue, http = await _client_for(handler)
    async with http:
        await venue.list_events()
        expired.add("session-1")
        results = await asyncio.gather(*[venue.list_events() for _ in range(6)])
    assert session_posts == ["session-1", "session-2"]
    assert all(item["events"] == [] for item in results)
    assert venue._session_token == "session-2"


@pytest.mark.asyncio
async def test_concurrent_first_login_is_single_flight() -> None:
    session_posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MATCHBOOK_SESSION_PATH:
            session_posts.append(request.method)
            return httpx.Response(200, json={"session-token": "session-1"})
        if request.url.path == "/edge/rest/lookups/sports":
            return _football_ok()
        if request.url.path == "/edge/rest/events":
            return _events_ok()
        return httpx.Response(404)

    venue, http = await _client_for(handler)
    async with http:
        await asyncio.gather(*[venue.list_events() for _ in range(8)])
    assert session_posts == ["POST"]


@pytest.mark.asyncio
async def test_429_login_honors_retry_after_and_skips_repeat_posts() -> None:
    session_posts: list[str] = []
    mono = FakeMono(10.0)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MATCHBOOK_SESSION_PATH:
            session_posts.append(request.method)
            return httpx.Response(429, headers={"Retry-After": "12"}, text="Too Many Requests")
        return httpx.Response(404)

    venue, http = await _client_for(handler, monotonic_clock=mono, login_cooldown_seconds=5)
    async with http:
        with pytest.raises(MatchbookRateLimitedError) as first:
            await venue.list_events()
        assert first.value.retry_after_seconds == 12
        with pytest.raises(MatchbookRateLimitedError) as second:
            await venue.list_events()
        assert 0 < second.value.retry_after_seconds <= 12
        health = await venue.health()
        assert health.ok is False
        assert health.authenticated is False
        assert "429" in (health.detail or "")
        _secret_free(health.detail)
        mono.value = 22.1
        with pytest.raises(MatchbookRateLimitedError):
            await venue.login()
    assert session_posts == ["POST", "POST"]


@pytest.mark.asyncio
async def test_429_without_retry_after_uses_bounded_fallback() -> None:
    session_posts: list[str] = []
    mono = FakeMono(50.0)

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MATCHBOOK_SESSION_PATH:
            session_posts.append(request.method)
            return httpx.Response(429, text="Too Many Requests")
        return httpx.Response(404)

    venue, http = await _client_for(
        handler,
        monotonic_clock=mono,
        login_cooldown_seconds=7,
        login_cooldown_max_seconds=9,
    )
    async with http:
        with pytest.raises(MatchbookRateLimitedError) as first:
            await venue.login()
        assert first.value.retry_after_seconds == 7
        with pytest.raises(MatchbookRateLimitedError):
            await venue.login()
        assert session_posts == ["POST"]
        mono.value = 57.1
        with pytest.raises(MatchbookRateLimitedError):
            await venue.login()
    assert session_posts == ["POST", "POST"]


@pytest.mark.asyncio
async def test_429_retry_after_http_date_and_cap() -> None:
    now = datetime(2026, 9, 16, 20, 30, tzinfo=UTC)
    retry_at = datetime(2026, 9, 16, 20, 40, tzinfo=UTC)
    response = httpx.Response(
        429,
        headers={"Retry-After": format_datetime(retry_at, usegmt=True)},
    )
    assert _retry_after_seconds(
        response,
        now=now,
        fallback_seconds=5,
        max_seconds=30,
    ) == 30
    uncapped = _retry_after_seconds(
        response,
        now=now,
        fallback_seconds=5,
        max_seconds=900,
    )
    assert uncapped == 600
    missing = httpx.Response(429)
    assert _retry_after_seconds(
        missing,
        now=now,
        fallback_seconds=5,
        max_seconds=30,
    ) == 5


@pytest.mark.asyncio
async def test_health_during_cooldown_does_not_login(
    isolated_shared_matchbook: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_posts: list[str] = []
    mono = FakeMono(1.0)
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MATCHBOOK_SESSION_PATH:
            session_posts.append(request.method)
            return httpx.Response(429, headers={"Retry-After": "20"})
        return httpx.Response(404)

    venue, http = await _client_for(handler, settings=settings, monotonic_clock=mono)
    set_shared_matchbook_client(venue)
    monkeypatch.setattr(main_api, "get_settings", lambda: settings)
    monkeypatch.setattr(
        main_api,
        "PolymarketClient",
        lambda _s: _EmptyOtherVenue(VenueName.POLYMARKET),
    )
    monkeypatch.setattr(
        main_api,
        "KalshiClient",
        lambda _s: _EmptyOtherVenue(VenueName.KALSHI),
    )
    async with http:
        first = await main_api.venue_health()
        second = await main_api.venue_health()
        third_client = await venue.health()
    matchbook = next(item for item in first if item["venue"] == VenueName.MATCHBOOK)
    matchbook_again = next(item for item in second if item["venue"] == VenueName.MATCHBOOK)
    assert matchbook["ok"] is False
    assert matchbook["authenticated"] is False
    assert "rate-limited" in str(matchbook["detail"]).lower()
    assert "429" in str(matchbook["detail"])
    _secret_free(str(matchbook["detail"]))
    assert matchbook_again["ok"] is False
    assert third_client.ok is False
    assert session_posts == ["POST"]
    health_src = inspect.getsource(main_api.venue_health)
    assert "get_shared_matchbook_client(settings)" in health_src
    assert "MatchbookClient(settings)" not in health_src


@pytest.mark.asyncio
async def test_shared_client_cleanup_closes_once_and_recreates(
    isolated_shared_matchbook: None,
) -> None:
    first = get_shared_matchbook_client(_settings())
    assert first is get_shared_matchbook_client(_settings())
    await aclose_shared_matchbook_client()
    await aclose_shared_matchbook_client()
    assert first.closed is True
    assert first._client.is_closed is True
    second = get_shared_matchbook_client(_settings())
    assert second is not first
    assert second.closed is False
    await reset_shared_matchbook_client()
    third = get_shared_matchbook_client(_settings())
    assert third is not second
    await reset_shared_matchbook_client()


def test_lifespan_closes_shared_matchbook_and_collect_does_not() -> None:
    lifespan_src = inspect.getsource(main_api.lifespan)
    assert "aclose_shared_matchbook_client()" in lifespan_src
    collect_src = inspect.getsource(paper_api._collect_report)
    assert "get_shared_matchbook_client(settings)" in collect_src
    assert "_aclose_soon(polymarket, kalshi)" in collect_src


def test_no_execution_surface_on_matchbook_client() -> None:
    assert not hasattr(MatchbookClient, "place_order")
    assert not hasattr(MatchbookClient, "cancel_order")
    assert not hasattr(MatchbookClient, "sign_order")
    assert MatchbookClient.capabilities.execution_enabled is False
    assert MatchbookClient.capabilities.data_enabled is True
    assert MatchbookClient.capabilities.paper_enabled is True
