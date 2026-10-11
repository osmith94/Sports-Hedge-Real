"""Operator Real-arm correction. Fake/mocks only. No live venue I/O or orders."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.application.paper_operations import PaperOperationsError
from sports_hedge.config import Settings
from sports_hedge.execution.live_canary import (
    LiveCanaryTradingFacts,
    arm_live_runtime,
    canary_submit_refusal,
    disarm_live_runtime,
    explain_permission_reasons,
    live_runtime_armed,
    reset_live_canary_runtime,
    set_live_canary_trading_facts,
    set_live_runtime_arm,
    trading_permission_reasons,
)
from sports_hedge.execution.matchbook_preflight import (
    SPENDABLE_BALANCE_FIELD,
    snapshot_from_balance_body,
)
from sports_hedge.execution.polymarket_geoblock import fetch_geoblock, parse_geoblock
from sports_hedge.execution.runtime import set_execution_runtime
from sports_hedge.persistence.operator_scanner_settings import (
    bind_runtime_operator_scanner_settings_store,
)
from test_wave40_live_canary import (
    LOCAL_ORIGIN,
    NOW,
    PERMITTED,
    _canary_settings,
    _ops,
    _plan,
    _runtime,
)
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger

NOW_OFFICIAL = {
    "id": 999001,
    "balance": 25,
    "exposure": 5,
    "commission-reserve": 1,
    SPENDABLE_BALANCE_FIELD: 19,
}


@pytest.fixture(autouse=True)
def _reset() -> None:
    set_execution_runtime(None)
    reset_live_canary_runtime()
    set_live_canary_trading_facts(None)
    bind_runtime_operator_scanner_settings_store(None)
    yield
    set_execution_runtime(None)
    reset_live_canary_runtime()
    set_live_canary_trading_facts(None)
    bind_runtime_operator_scanner_settings_store(None)


def test_official_matchbook_sample_has_no_currency_field() -> None:
    assert "currency" not in NOW_OFFICIAL
    snapshot = snapshot_from_balance_body(
        NOW_OFFICIAL,
        configured_currency="GBP",
        authenticated=True,
        observed=True,
    )
    assert snapshot["currency"] is None
    assert snapshot["currency_compatible"] is None
    assert snapshot["transport_ready"] is True
    assert snapshot["balance_positive"] is True


@pytest.mark.asyncio
async def test_arm_passes_when_official_balance_has_no_currency(tmp_path: Path) -> None:
    settings = _canary_settings(tmp_path)
    facts = LiveCanaryTradingFacts(
        polymarket_geoblock_permitted=True,
        polymarket_geoblock_reason="geoblock_permitted",
        polymarket_credentials_present=True,
        polymarket_wallet_valid=True,
        polymarket_balance_readable=True,
        polymarket_trading_ready=True,
        matchbook_credentials_present=True,
        matchbook_authenticated=True,
        matchbook_free_funds_positive=True,
        matchbook_currency_compatible=None,
        injected=True,
    )
    set_live_canary_trading_facts(facts)
    result = await arm_live_runtime(settings)
    assert result["runtime_armed"] is True
    assert "matchbook_currency_unknown" not in result["not_ready_reasons"]
    assert "matchbook_currency_mismatch" not in result["not_ready_reasons"]
    assert live_runtime_armed() is True


@pytest.mark.asyncio
async def test_arm_accepts_intent_but_explicit_currency_mismatch_prevents_native_writes(tmp_path: Path) -> None:
    settings = _canary_settings(tmp_path)
    snapshot = snapshot_from_balance_body(
        {**NOW_OFFICIAL, "currency": "USD"},
        configured_currency="GBP",
        authenticated=True,
        observed=True,
    )
    assert snapshot["currency_compatible"] is False
    facts = LiveCanaryTradingFacts(
        polymarket_geoblock_permitted=True,
        polymarket_geoblock_reason="geoblock_permitted",
        polymarket_credentials_present=True,
        polymarket_wallet_valid=True,
        polymarket_balance_readable=True,
        polymarket_trading_ready=True,
        matchbook_credentials_present=True,
        matchbook_authenticated=True,
        matchbook_free_funds_positive=True,
        matchbook_currency_compatible=snapshot["currency_compatible"],
        injected=True,
    )
    set_live_canary_trading_facts(facts)
    result = await arm_live_runtime(settings)
    assert result["runtime_armed"] is True
    assert result["trading_permission"] is False
    assert "matchbook_currency_mismatch" in result["not_ready_reasons"]
    assert "matchbook_currency_mismatch" in trading_permission_reasons(settings, facts)
    assert canary_submit_refusal(_plan(), settings=settings, facts=facts) == "matchbook_currency_mismatch"
    runtime, mb, _pm = _runtime()
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "fx.sqlite"), _plan())
    try:
        ops.capture_opening_fill("watch:mkt-canary", now=NOW)
    except PaperOperationsError:
        pass
    assert mb.calls == []



@pytest.mark.asyncio
async def test_arm_is_independent_of_all_remote_checks_and_order_gate_stays_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _canary_settings(tmp_path)
    calls = {"preflight": 0, "bind": 0}

    async def fail_preflight(_settings: Settings) -> LiveCanaryTradingFacts:
        calls["preflight"] += 1
        raise AssertionError("turning ON must not make venue requests")

    def fail_binding(_settings: Settings):
        calls["bind"] += 1
        raise AssertionError("turning ON must not construct venue clients")

    monkeypatch.setattr(
        "sports_hedge.execution.live_canary.collect_live_canary_facts",
        fail_preflight,
    )
    monkeypatch.setattr(
        "sports_hedge.execution.composition.bind_process_execution_runtime",
        fail_binding,
    )
    assert live_runtime_armed() is False
    result = await arm_live_runtime(settings)
    assert result["runtime_armed"] is True
    assert result["execution_toggle"] == "ON"
    assert result["trading_permission"] is False
    assert "canary_preflight_not_collected" in result["not_ready_reasons"]
    assert calls == {"preflight": 0, "bind": 0}

    # On the actual submit seam an explicit denial still blocks the order.
    denied = LiveCanaryTradingFacts(
        polymarket_geoblock_permitted=False,
        polymarket_geoblock_reason="geoblock_unreachable_timeout",
        injected=True,
    )
    assert canary_submit_refusal(_plan(), settings=settings, facts=denied) == "geoblock_unreachable_timeout"
    assert calls == {"preflight": 0, "bind": 0}
    # Turning OFF must still stop new execution immediately.
    monkeypatch.undo()
    off = disarm_live_runtime(settings)
    assert off["runtime_armed"] is False


@pytest.mark.asyncio
async def test_paper_mode_cannot_be_armed(tmp_path: Path) -> None:
    settings = Settings(sports_hedge_mode="paper", sports_hedge_execution_enabled=False)
    status = await arm_live_runtime(settings)
    assert status["runtime_armed"] is False
    assert status["armed"] is False
    assert "not_real_mode" in status["not_ready_reasons"]

def test_geoblock_blocked_and_unreachable_are_distinct_and_both_deny() -> None:
    blocked = parse_geoblock({"blocked": True, "country": "US"})
    assert blocked.permitted is False
    assert blocked.reason == "geoblock_blocked"
    assert blocked.reachable is True
    settings = Settings(sports_hedge_mode="real")
    blocked_facts = LiveCanaryTradingFacts(
        polymarket_geoblock_permitted=blocked.permitted,
        polymarket_geoblock_reason=blocked.reason,
        polymarket_credentials_present=True,
        polymarket_wallet_valid=True,
        polymarket_balance_readable=True,
        polymarket_trading_ready=True,
        matchbook_credentials_present=True,
        matchbook_authenticated=True,
        matchbook_free_funds_positive=True,
        matchbook_currency_compatible=None,
        injected=True,
    )
    assert "geoblock_blocked" in trading_permission_reasons(settings, blocked_facts, for_arm=True)
    explanations = explain_permission_reasons(["geoblock_blocked", "geoblock_unreachable_timeout"])
    assert "territory rejection" in explanations[0]
    assert "not a location rejection" in explanations[1]


@pytest.mark.asyncio
async def test_geoblock_timeout_is_unknown_not_blocked() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("read timeout")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    try:
        result = await fetch_geoblock("https://polymarket.com/api/geoblock", client=client)
    finally:
        await client.aclose()
    assert result.permitted is False
    assert result.blocked is None
    assert result.reason == "geoblock_unreachable_timeout"
    assert result.reason != "geoblock_blocked"


@pytest.mark.asyncio
async def test_geoblock_timeout_retry_uses_later_success_not_error_inference() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.TimeoutException("read timeout")
        return httpx.Response(200, json={"blocked": False, "country": "GB"})

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    try:
        result = await fetch_geoblock("https://polymarket.com/api/geoblock", client=client)
    finally:
        await client.aclose()
    assert calls["n"] == 2
    assert result.permitted is True
    assert result.reason == "geoblock_permitted"


@pytest.mark.asyncio
async def test_geoblock_http_failure_is_not_retried_and_not_permitted() -> None:
    calls = {"n": 0}

    def handler(_request: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    try:
        result = await fetch_geoblock("https://polymarket.com/api/geoblock", client=client)
    finally:
        await client.aclose()
    assert calls["n"] == 1
    assert result.permitted is False
    assert result.reason == "geoblock_unreachable_http"


def test_get_cannot_arm_and_foreign_origin_is_403(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _canary_settings(tmp_path)
    monkeypatch.setattr("sports_hedge.api.live_canary.get_settings", lambda: settings)
    set_live_canary_trading_facts(PERMITTED)
    client = TestClient(app)
    assert client.get("/live-canary/arm", headers=LOCAL_ORIGIN).status_code == 405
    assert live_runtime_armed() is False
    denied = client.post("/live-canary/arm", json={}, headers={"Origin": "https://evil.example"})
    assert denied.status_code == 403
    assert live_runtime_armed() is False
    armed = client.post("/live-canary/arm", json={}, headers=LOCAL_ORIGIN)
    assert armed.status_code == 200
    assert armed.json()["runtime_armed"] is True
