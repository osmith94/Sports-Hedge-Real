"""Matchbook execution preflight. Mock HTTP only. No live account and no offers."""

from __future__ import annotations

import json

import httpx
import pytest

from sports_hedge.config import Settings
from sports_hedge.execution.matchbook_http import (
    BALANCE_PATH,
    SUBMIT_PATH,
    MatchbookHttpExecutionTransport,
)
from sports_hedge.execution.matchbook_preflight import (
    SPENDABLE_BALANCE_FIELD,
    collect_matchbook_preflight,
    format_matchbook_preflight,
)

PASSWORD = "matchbook-password-should-not-leak"
USERNAME = "mb-user-should-not-print"
SESSION = "mb-session-TOKEN-do-not-print"
OFFER_PATHS = (SUBMIT_PATH, "/edge/rest/v2/offers")


def _settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "sports_hedge_mode": "real",
        "sports_hedge_execution_enabled": False,
        "matchbook_username": USERNAME,
        "matchbook_password": PASSWORD,
        "matchbook_currency": "GBP",
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://venue.test")


def _assert_read_only(seen: list[httpx.Request]) -> None:
    for request in seen:
        assert request.url.path not in OFFER_PATHS
        assert request.method != "DELETE"
        assert request.method != "PUT"
        assert request.method != "PATCH"
        if request.method == "POST":
            assert request.url.path.endswith("/security/session")
        else:
            assert request.method == "GET"
            assert request.url.path == BALANCE_PATH


def _official_balance(**overrides: object) -> dict[str, object]:
    body: dict[str, object] = {
        "id": 12345,
        "balance": 25,
        "exposure": 5,
        "commission-reserve": 1,
        SPENDABLE_BALANCE_FIELD: 19,
    }
    body.update(overrides)
    return body


def _handler(
    seen: list[httpx.Request],
    *,
    login_status: int = 200,
    login_body: object | None = None,
    login_content: bytes | None = None,
    balance_status: int = 200,
    balance_body: object | None = None,
    balance_content: bytes | None = None,
):
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/security/session"):
            assert request.method == "POST"
            if login_content is not None:
                return httpx.Response(login_status, content=login_content)
            payload = {"session-token": SESSION} if login_body is None else login_body
            return httpx.Response(login_status, json=payload)
        assert request.url.path == BALANCE_PATH
        assert request.method == "GET"
        assert request.headers.get("session-token") == SESSION
        if balance_content is not None:
            return httpx.Response(balance_status, content=balance_content)
        return httpx.Response(balance_status, json={} if balance_body is None else balance_body)

    return handler


def _assert_secrets_absent(text: str) -> None:
    assert SESSION not in text
    assert PASSWORD not in text
    assert USERNAME not in text
    assert "12345" not in text
    assert "mfa" not in text.lower()


async def _run(settings: Settings, handler) -> tuple[dict[str, object], str, list[httpx.Request]]:
    seen: list[httpx.Request] = []
    transport = MatchbookHttpExecutionTransport(settings, client=_client(handler(seen)))

    async def account() -> dict[str, object]:
        return await transport.account_snapshot()

    try:
        report = await collect_matchbook_preflight(settings, account=account)
    finally:
        await transport.aclose()
    text = format_matchbook_preflight(report)
    _assert_read_only(seen)
    _assert_secrets_absent(text)
    _assert_secrets_absent(json.dumps(report))
    assert text.endswith("LIVE ORDER SUBMISSION:\n  DISABLED")
    assert "Execution enabled: false" in text or settings.sports_hedge_execution_enabled is True
    return report, text, seen


@pytest.mark.asyncio
async def test_missing_credentials_fail_closed_without_a_venue_request() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        raise AssertionError("credentials absent must not call Matchbook")

    for overrides in (
        {"matchbook_username": None, "matchbook_password": None},
        {"matchbook_username": "  ", "matchbook_password": PASSWORD},
        {"matchbook_username": USERNAME, "matchbook_password": ""},
    ):
        seen.clear()
        settings = _settings(**overrides)
        report = await collect_matchbook_preflight(settings)
        assert report["transport_ready"] is False
        assert report["authenticated_read"] is False
        text = format_matchbook_preflight(report)
        assert "LIVE ORDER SUBMISSION:\n  DISABLED" in text
        assert seen == []
        transport = MatchbookHttpExecutionTransport(settings, client=_client(handler))
        snapshot = await transport.account_snapshot()
        await transport.aclose()
        assert snapshot["transport_ready"] is False
        assert seen == []


@pytest.mark.asyncio
async def test_login_and_positive_free_funds_are_ready_without_execution() -> None:
    report, text, seen = await _run(
        _settings(),
        lambda seen: _handler(seen, balance_body=_official_balance()),
    )
    assert report["authenticated_read"] is True
    assert report["balance_readable"] is True
    assert report["balance_positive"] is True
    assert report["currency"] is None
    assert report["currency_compatible"] is None
    assert report["free_funds"] == "19"
    assert report["transport_ready"] is True
    assert report["execution_enabled"] is False
    assert "authenticated read: PASS" in text
    assert "balance readable: yes" in text
    assert "currency: unavailable" in text
    assert "spendable balance: 19" in text
    assert "spendable balance positive: yes" in text
    assert "ready: yes" in text
    assert [request.method for request in seen] == ["POST", "GET"]


@pytest.mark.asyncio
async def test_explicit_matching_currency_is_shown_and_ready() -> None:
    report, text, _seen = await _run(
        _settings(),
        lambda seen: _handler(seen, balance_body=_official_balance(currency="gbp")),
    )
    assert report["currency"] == "GBP"
    assert report["currency_compatible"] is True
    assert report["transport_ready"] is True
    assert "currency: GBP" in text
    assert "currency: unavailable" not in text


@pytest.mark.asyncio
async def test_execution_enabled_still_does_not_submit_an_offer() -> None:
    report, text, seen = await _run(
        _settings(sports_hedge_execution_enabled=True),
        lambda seen: _handler(seen, balance_body=_official_balance(currency="GBP")),
    )
    assert report["execution_enabled"] is True
    assert report["transport_ready"] is True
    assert "Execution enabled: true" in text
    assert "LIVE ORDER SUBMISSION:\n  DISABLED" in text
    assert [request.method for request in seen] == ["POST", "GET"]


@pytest.mark.asyncio
async def test_login_failure_is_not_ready() -> None:
    report, text, seen = await _run(
        _settings(),
        lambda seen: _handler(
            seen,
            login_status=401,
            login_body={"errors": [{"messages": [PASSWORD, SESSION]}]},
        ),
    )
    assert report["authenticated_read"] is False
    assert report["balance_readable"] is False
    assert report["transport_ready"] is False
    assert "authenticated read: FAIL" in text
    assert "currency:" not in text
    assert "spendable balance" not in text
    assert len(seen) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "login_body",
    [{}, {"session-token": ""}, {"session-token": None}, {"user-id": 9}],
)
async def test_missing_session_token_is_not_ready(login_body: dict[str, object]) -> None:
    report, _text, seen = await _run(
        _settings(),
        lambda seen: _handler(seen, login_body=login_body),
    )
    assert report["authenticated_read"] is False
    assert report["transport_ready"] is False
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_malformed_login_body_is_not_ready() -> None:
    report, _text, seen = await _run(
        _settings(),
        lambda seen: _handler(seen, login_content=b"not-json"),
    )
    assert report["authenticated_read"] is False
    assert report["transport_ready"] is False
    assert len(seen) == 1


@pytest.mark.asyncio
async def test_balance_401_is_not_ready() -> None:
    report, text, seen = await _run(
        _settings(),
        lambda seen: _handler(
            seen,
            balance_status=401,
            balance_body={"errors": [{"messages": [SESSION]}]},
        ),
    )
    assert report["authenticated_read"] is True
    assert report["balance_readable"] is False
    assert report["balance_positive"] is False
    assert report["transport_ready"] is False
    assert "balance readable: no" in text
    assert "currency:" not in text
    assert len(seen) == 2


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "balance_body",
    [
        [],
        {"id": 12345, "balance": 40},
        {"free-funds": "nope", "id": 12345},
        {"free-funds": True, "balance": 10},
        "19",
    ],
)
async def test_malformed_balance_is_not_ready(balance_body: object) -> None:
    report, _text, seen = await _run(
        _settings(),
        lambda seen: _handler(seen, balance_body=balance_body),
    )
    assert report["authenticated_read"] is True
    assert report["balance_readable"] is False
    assert report["transport_ready"] is False
    assert len(seen) == 2


@pytest.mark.asyncio
async def test_non_json_balance_is_not_ready() -> None:
    report, _text, _seen = await _run(
        _settings(),
        lambda seen: _handler(seen, balance_content=b"<!DOCTYPE html>"),
    )
    assert report["authenticated_read"] is True
    assert report["balance_observed"] is False
    assert report["balance_readable"] is False
    assert report["transport_ready"] is False


@pytest.mark.asyncio
async def test_zero_free_funds_are_readable_but_not_ready() -> None:
    report, text, _seen = await _run(
        _settings(),
        lambda seen: _handler(seen, balance_body=_official_balance(**{SPENDABLE_BALANCE_FIELD: 0})),
    )
    assert report["balance_readable"] is True
    assert report["balance_positive"] is False
    assert report["free_funds"] == "0"
    assert report["transport_ready"] is False
    assert "spendable balance positive: no" in text
    assert "ready: no" in text


@pytest.mark.asyncio
async def test_explicit_wrong_currency_is_not_ready() -> None:
    report, text, _seen = await _run(
        _settings(),
        lambda seen: _handler(
            seen,
            balance_body=_official_balance(currency="USD", **{SPENDABLE_BALANCE_FIELD: "12.50"}),
        ),
    )
    assert report["currency"] == "USD"
    assert report["currency_compatible"] is False
    assert report["balance_positive"] is True
    assert report["free_funds"] == "12.50"
    assert report["transport_ready"] is False
    assert "currency: USD" in text
    assert "ready: no" in text


@pytest.mark.asyncio
async def test_unusable_currency_value_is_not_echoed() -> None:
    report, text, _seen = await _run(
        _settings(),
        lambda seen: _handler(
            seen,
            balance_body=_official_balance(currency=SESSION),
        ),
    )
    assert report["currency"] is None
    assert report["currency_compatible"] is False
    assert report["transport_ready"] is False
    assert "currency: incompatible" in text
