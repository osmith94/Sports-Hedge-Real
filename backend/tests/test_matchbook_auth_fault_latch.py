"""Issue #252: process-local Matchbook HTTP 400 auth-fault latch.

Characterization against frozen R4 head 4c3950cf0d5e8e7a87a153b90af34381b3b3c0de
(2026-09-16). A LOGIN_ACCOUNT_LOCKED_2 / bad-password 400 from POST
`/bpapi/rest/security/session` raised httpx.HTTPStatusError and was retried on
every later cycle:

    after_first_list_events=1
    after_second_list_events=2
    after_hot=3
    after_universe=4
    after_client_health=5
    after_venues_health=6

Health detail was a generic httpx 400 URL string and did not include
LOGIN_ACCOUNT_LOCKED_2. After this change the first rejected login POST latches;
later HOT / UNIVERSE / manual / /venues/health calls fail fast with zero extra
POSTs until explicit reset or process recreation.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest

from sports_hedge.api import main as main_api
from sports_hedge.api import paper as paper_api
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.venues.matchbook import (
    LOGIN_ACCOUNT_LOCKED_CODE,
    MATCHBOOK_SESSION_PATH,
    MatchbookAuthFaultError,
    MatchbookClient,
    MatchbookPermissionError,
    matchbook_login_failure_from_response,
    parse_matchbook_login_error_metadata,
    reset_shared_matchbook_client,
    set_shared_matchbook_client,
)
from sports_hedge.application.provider_runtime import reset_shared_provider_runtime
from test_matchbook_session_reliability import (
    PASSWORD,
    USERNAME,
    _client_for,
    _events_ok,
    _football_ok,
    _install_shared_runtime,
    _secret_free,
    _settings,
)

LOCKED_BODY = {
    "errors": [
        {
            "messages": [
                "Too many failed password attempts, your account is now locked. "
                "Please reset your password and try again."
            ],
            "codes": [LOGIN_ACCOUNT_LOCKED_CODE],
        }
    ]
}
BAD_PASSWORD_BODY = {
    "errors": [
        {
            "messages": ["Incorrect username or password"],
            "codes": ["LOGIN_INVALID"],
        }
    ]
}
MFA_BODY = {
    "errors": [
        {
            "messages": ["Invalid MFA code"],
            "codes": ["LOGIN_MFA_INVALID"],
        }
    ]
}


def _paper_service() -> PaperScanService:
    return PaperScanService(MarketIntelligenceService(SqliteMarketIntelligenceRepository()))


def _lock_handler(
    session_posts: list[str],
    body: dict[str, Any] | None = None,
) -> Any:
    payload = LOCKED_BODY if body is None else body

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MATCHBOOK_SESSION_PATH:
            session_posts.append(request.method)
            return httpx.Response(400, json=payload)
        return httpx.Response(404)

    return handler


@pytest.fixture
async def isolated_shared_matchbook() -> Any:
    await reset_shared_matchbook_client()
    await reset_shared_provider_runtime()
    yield
    await reset_shared_provider_runtime()
    await reset_shared_matchbook_client()


@pytest.mark.asyncio
async def test_first_bad_password_400_posts_once_then_latches() -> None:
    session_posts: list[str] = []
    venue, http = await _client_for(_lock_handler(session_posts, BAD_PASSWORD_BODY))
    async with http:
        with pytest.raises(MatchbookAuthFaultError) as first:
            await venue.list_events()
        assert first.value.status_code == 400
        assert "LOGIN_INVALID" in first.value.codes
        _secret_free(str(first.value))
        with pytest.raises(MatchbookAuthFaultError) as second:
            await venue.list_events()
        assert second.value is first.value
        health = await venue.health()
    assert session_posts == ["POST"]
    assert health.ok is False
    assert health.authenticated is False
    assert "HTTP 400" in (health.detail or "")
    assert "LOGIN_INVALID" in (health.detail or "")
    assert "latched" in (health.detail or "")
    _secret_free(health.detail)


@pytest.mark.asyncio
async def test_latched_hot_universe_health_issue_zero_additional_login_posts(
    isolated_shared_matchbook: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_posts: list[str] = []
    settings = _settings()
    venue, http = await _client_for(_lock_handler(session_posts, LOCKED_BODY), settings=settings)
    set_shared_matchbook_client(venue)
    _install_shared_runtime(settings, venue)
    monkeypatch.setattr(paper_api, "get_settings", lambda: settings)
    monkeypatch.setattr(main_api, "get_settings", lambda: settings)
    service = _paper_service()
    async with http:
        with pytest.raises(MatchbookAuthFaultError):
            await venue.login()
        assert session_posts == ["POST"]
        await paper_api._collect_report({}, service=service, scan_lane=ScanLane.HOT)
        await paper_api._collect_report({}, service=service, scan_lane=ScanLane.UNIVERSE)
        client_health = await venue.health()
        payload = await main_api.venue_health()
    matchbook = next(item for item in payload if item["venue"] == VenueName.MATCHBOOK)
    assert session_posts == ["POST"]
    assert client_health.ok is False
    assert client_health.authenticated is False
    assert LOGIN_ACCOUNT_LOCKED_CODE in (client_health.detail or "")
    assert matchbook["ok"] is False
    assert matchbook["authenticated"] is False
    assert LOGIN_ACCOUNT_LOCKED_CODE in str(matchbook["detail"])
    assert "account locked" in str(matchbook["detail"]).lower()
    _secret_free(client_health.detail)
    _secret_free(str(matchbook["detail"]))


@pytest.mark.asyncio
async def test_account_locked_code_in_health_without_secrets() -> None:
    session_posts: list[str] = []
    venue, http = await _client_for(_lock_handler(session_posts, LOCKED_BODY))
    async with http:
        with pytest.raises(MatchbookAuthFaultError) as fault:
            await venue.login()
        health = await venue.health()
        again = await venue.health()
    assert session_posts == ["POST"]
    assert LOGIN_ACCOUNT_LOCKED_CODE in fault.value.codes
    assert LOGIN_ACCOUNT_LOCKED_CODE in str(fault.value)
    assert LOGIN_ACCOUNT_LOCKED_CODE in (health.detail or "")
    assert "account locked" in (health.detail or "").lower()
    assert health.detail == again.detail
    _secret_free(str(fault.value))
    _secret_free(health.detail)


@pytest.mark.asyncio
async def test_mfa_style_400_latches_without_repeat_posts() -> None:
    session_posts: list[str] = []
    settings = Settings(
        matchbook_username=USERNAME,
        matchbook_password=PASSWORD,
        matchbook_mfa_code="123456",
    )
    venue, http = await _client_for(
        _lock_handler(session_posts, MFA_BODY),
        settings=settings,
    )
    async with http:
        with pytest.raises(MatchbookAuthFaultError) as fault:
            await venue.login()
        with pytest.raises(MatchbookAuthFaultError):
            await venue.list_events()
        health = await venue.health()
    assert session_posts == ["POST"]
    assert "LOGIN_MFA_INVALID" in fault.value.codes
    assert "LOGIN_MFA_INVALID" in (health.detail or "")
    assert "123456" not in str(fault.value)
    assert "123456" not in (health.detail or "")
    _secret_free(str(fault.value))
    _secret_free(health.detail)


@pytest.mark.asyncio
async def test_explicit_reset_and_process_recreation_allow_one_new_login(
    isolated_shared_matchbook: None,
) -> None:
    session_posts: list[str] = []
    settings = _settings()
    login_ok = False

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path != MATCHBOOK_SESSION_PATH:
            if request.url.path == "/edge/rest/lookups/sports":
                return _football_ok()
            if request.url.path == "/edge/rest/events":
                return _events_ok()
            return httpx.Response(404)
        session_posts.append(request.method)
        if login_ok:
            return httpx.Response(200, json={"session-token": "session-after-reset"})
        return httpx.Response(400, json=BAD_PASSWORD_BODY)

    venue, http = await _client_for(handler, settings=settings)
    set_shared_matchbook_client(venue)
    async with http:
        with pytest.raises(MatchbookAuthFaultError):
            await venue.login()
        assert session_posts == ["POST"]
        login_ok = True
        with pytest.raises(MatchbookAuthFaultError):
            await venue.login()
        assert session_posts == ["POST"]
        venue.clear_auth_fault()
        token = await venue.login()
        reused = await venue.login()
        assert token == reused == "session-after-reset"
        assert session_posts == ["POST", "POST"]

    await reset_shared_matchbook_client()
    session_posts.clear()
    login_ok = False
    venue2, http2 = await _client_for(handler, settings=settings)
    set_shared_matchbook_client(venue2)
    async with http2:
        with pytest.raises(MatchbookAuthFaultError):
            await venue2.login()
        with pytest.raises(MatchbookAuthFaultError):
            await venue2.login()
        assert session_posts == ["POST"]
        await reset_shared_matchbook_client()
        venue3, http3 = await _client_for(handler, settings=settings)
        async with http3:
            with pytest.raises(MatchbookAuthFaultError):
                await venue3.login()
            with pytest.raises(MatchbookAuthFaultError):
                await venue3.login()
        assert session_posts == ["POST", "POST"]


@pytest.mark.asyncio
async def test_good_credentials_still_authenticate_and_reuse_one_session() -> None:
    session_posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MATCHBOOK_SESSION_PATH:
            session_posts.append(request.method)
            return httpx.Response(200, json={"session-token": "session-good"})
        if request.url.path == "/edge/rest/lookups/sports":
            return _football_ok()
        if request.url.path == "/edge/rest/events":
            return _events_ok()
        return httpx.Response(404)

    venue, http = await _client_for(handler)
    async with http:
        first = await venue.list_events()
        second = await venue.list_events()
        health = await venue.health()
    assert first["events"] == second["events"] == []
    assert session_posts == ["POST"]
    assert health.ok is True
    assert health.authenticated is True
    assert venue._session_token == "session-good"
    assert venue._auth_fault is None
    assert not hasattr(venue, "place_order")
    assert venue.capabilities.execution_enabled is False


@pytest.mark.asyncio
async def test_concurrent_400_login_is_single_flight_then_latched() -> None:
    session_posts: list[str] = []
    venue, http = await _client_for(_lock_handler(session_posts, LOCKED_BODY))
    async with http:
        results = await asyncio.gather(
            *[venue.login() for _ in range(8)],
            return_exceptions=True,
        )
        health = await venue.health()
    assert session_posts == ["POST"]
    assert all(isinstance(item, MatchbookAuthFaultError) for item in results)
    assert health.ok is False
    assert LOGIN_ACCOUNT_LOCKED_CODE in (health.detail or "")


@pytest.mark.asyncio
async def test_400_payload_secrets_are_not_surfaced_in_fault_or_health() -> None:
    session_posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MATCHBOOK_SESSION_PATH:
            session_posts.append(request.method)
            return httpx.Response(
                400,
                json={
                    "username": USERNAME,
                    "password": PASSWORD,
                    "session-token": "leaked-session-token",
                    "mfa-code": "999999",
                    "errors": [
                        {
                            "messages": [
                                f"rejected for {USERNAME} / {PASSWORD} token=leaked-session-token"
                            ],
                            "codes": ["LOGIN_INVALID"],
                        }
                    ],
                },
            )
        return httpx.Response(404)

    venue, http = await _client_for(handler)
    async with http:
        with pytest.raises(MatchbookAuthFaultError) as fault:
            await venue.login()
        health = await venue.health()
    blob = f"{fault.value}\n{health.detail}"
    _secret_free(blob)
    assert "leaked-session-token" not in blob
    assert USERNAME not in blob
    assert PASSWORD not in blob
    assert "LOGIN_INVALID" in str(fault.value)
    assert session_posts == ["POST"]


@pytest.mark.asyncio
async def test_login_forbidden_403_is_sanitized_and_not_latched() -> None:
    session_posts: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MATCHBOOK_SESSION_PATH:
            session_posts.append(request.method)
            return httpx.Response(
                403,
                json={
                    "errors": [
                        {
                            "codes": ["API_ACCESS_DENIED"],
                            "messages": [f"{PASSWORD} denied for this resource"],
                        }
                    ],
                    "session-token": "must-not-print",
                },
            )
        return httpx.Response(404)

    venue, http = await _client_for(handler)
    async with http:
        with pytest.raises(MatchbookPermissionError) as first:
            await venue.login()
        with pytest.raises(MatchbookPermissionError) as second:
            await venue.login()
    assert session_posts == ["POST", "POST"]
    assert venue._auth_fault is None
    assert "HTTP 403" in str(first.value)
    assert "API_ACCESS_DENIED" in str(first.value)
    assert "[redacted]" in str(first.value)
    assert "must-not-print" not in str(first.value)
    _secret_free(str(first.value))
    _secret_free(str(second.value))


def test_matchbook_login_failure_from_response_maps_403() -> None:
    response = httpx.Response(
        403,
        json={"errors": [{"codes": ["FORBIDDEN"], "messages": ["Access denied"]}]},
    )
    failure = matchbook_login_failure_from_response(response)
    assert isinstance(failure, MatchbookPermissionError)
    assert "HTTP 403" in str(failure)
    assert "FORBIDDEN" in str(failure)


def test_parse_matchbook_login_error_metadata_ignores_non_error_fields() -> None:
    response = httpx.Response(
        400,
        json={
            "username": USERNAME,
            "session-token": "should-ignore",
            "errors": [
                {
                    "codes": [LOGIN_ACCOUNT_LOCKED_CODE, "<script>"],
                    "messages": [
                        "Too many failed password attempts, your account is now locked.",
                        "session-token=abc",
                    ],
                }
            ],
        },
    )
    codes, messages = parse_matchbook_login_error_metadata(
        response,
        secrets=(USERNAME, PASSWORD),
    )
    assert codes == (LOGIN_ACCOUNT_LOCKED_CODE,)
    assert messages == ("Too many failed password attempts, your account is now locked.",)


def test_no_execution_surface_on_matchbook_client() -> None:
    assert not hasattr(MatchbookClient, "place_order")
    assert not hasattr(MatchbookClient, "cancel_order")
    assert not hasattr(MatchbookClient, "sign_order")
    assert MatchbookClient.capabilities.execution_enabled is False
    assert issubclass(MatchbookAuthFaultError, Exception)
