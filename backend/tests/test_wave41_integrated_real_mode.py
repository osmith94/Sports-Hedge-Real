"""Wave 41 combined Real-mode integration. Fake transports only. No venue I/O.

Cross-wave checks for STREAM fee/FX (Wave 38), Price-2 Matchbook tail refresh
(Wave 39), and runtime Execution OFF→ON (Wave 40). Operator Max Event /
Opportunity / One-Time stay on the existing settings store. No session cap.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.application.execution_snapshot import DEFAULT_MAX_SNAPSHOT_SKEW_MS
from sports_hedge.application.paper_operations import PaperOperationsError
from sports_hedge.config import Settings
from sports_hedge.execution.live_canary import (
    LiveCanaryTradingFacts,
    live_runtime_armed,
    public_canary_status,
    reset_live_canary_runtime,
    set_live_canary_trading_facts,
    set_live_runtime_arm,
    trading_permission_reasons,
)
from sports_hedge.execution.package import execution_armed
from sports_hedge.execution.runtime import set_execution_runtime
from sports_hedge.persistence.operator_scanner_settings import (
    bind_runtime_operator_scanner_settings_store,
)
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from test_wave38_stream_pricing import (
    test_missing_fx_fails_closed_before_price2,
    test_missing_polymarket_fee_fails_closed,
    test_shadow_does_not_put_stream_shadow_into_quote_age_reason,
    test_stream_scan_kwargs_do_not_invent_capital_caps,
)
from test_wave39_matchbook_tail_refresh import (
    DEFAULT_MAX_SNAPSHOT_SKEW_MS as WAVE39_SKEW,
)
from test_wave40_live_canary import (
    FROZEN_SUBMIT,
    LOCAL_ORIGIN,
    _arm,
    _canary_settings,
    _ops,
    _plan,
    _register,
    _runtime,
    _snapshot,
)

NOW = datetime(2026, 10, 11, 12, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_combined(monkeypatch: pytest.MonkeyPatch) -> None:
    set_execution_runtime(None)
    reset_live_canary_runtime()
    set_live_canary_trading_facts(None)
    bind_runtime_operator_scanner_settings_store(None)
    monkeypatch.setattr(
        "sports_hedge.execution.package.wall_clock_utc",
        lambda: FROZEN_SUBMIT,
    )
    yield
    set_execution_runtime(None)
    reset_live_canary_runtime()
    set_live_canary_trading_facts(None)
    bind_runtime_operator_scanner_settings_store(None)


def test_wave38_fee_fx_still_authoritative_on_combined_tree() -> None:
    test_shadow_does_not_put_stream_shadow_into_quote_age_reason()
    test_missing_fx_fails_closed_before_price2()
    test_missing_polymarket_fee_fails_closed()
    test_stream_scan_kwargs_do_not_invent_capital_caps()


def test_wave39_skew_and_quote_age_gates_unchanged() -> None:
    assert DEFAULT_MAX_SNAPSHOT_SKEW_MS == 500
    assert WAVE39_SKEW == 500
    settings = Settings()
    assert int(settings.paper_execution_max_snapshot_skew_ms) == 500
    assert int(settings.paper_entry_max_quote_age_ms) == 2000


def test_real_bootstrap_health_is_off_without_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _canary_settings(tmp_path)
    monkeypatch.setattr("sports_hedge.api.live_canary.get_settings", lambda: settings)
    monkeypatch.setattr("sports_hedge.api.main.get_settings", lambda: settings)
    client = TestClient(app)
    health = client.get("/health").json()
    assert health["mode"] == "real"
    assert health["execution_enabled"] is False
    assert health["live_canary"]["runtime_armed"] is False
    assert health["live_canary"]["execution_toggle"] == "OFF"
    assert health["live_canary"]["no_session_cap"] is True
    assert Decimal(health["live_canary"]["max_event_gbp"]) == Decimal("7")
    assert Decimal(health["live_canary"]["max_opportunity_gbp"]) == Decimal("7")
    assert Decimal(health["live_canary"]["max_one_time_gbp"]) == Decimal("5")
    assert live_runtime_armed() is False
    assert execution_armed(settings) is False


def test_price2_accepted_while_off_is_not_an_active_trade(tmp_path: Path) -> None:
    settings = _canary_settings(tmp_path)
    runtime, mb, pm = _runtime()
    plan = _plan()
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "off.sqlite"), plan)
    try:
        ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    except PaperOperationsError:
        pass
    assert mb.calls == []
    assert pm.calls == []
    assert ops.list_active_trades() == []


def test_arm_then_two_independent_fake_opens_then_off(tmp_path: Path) -> None:
    settings = _canary_settings(tmp_path)
    _arm(settings)
    status = public_canary_status(settings)
    assert status["execution_toggle"] == "ON"
    assert status["no_session_cap"] is True
    assert status["no_one_opportunity_limit"] is True
    runtime, mb, pm = _runtime()
    first = _plan("watch:first", stake="1.50")
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "on.sqlite"), first)
    ops.capture_opening_fill(first.opportunity_id, now=NOW)
    assert len(mb.calls) == 1
    assert len(pm.calls) == 1
    second = _plan(
        "watch:second",
        stake="1.50",
        snapshot=_snapshot(stake="1.50", snapshot_id="exec:second"),
        canonical_event_id="evt-2",
    )
    _register(ops, second)
    ops.capture_opening_fill(second.opportunity_id, now=NOW)
    assert len(mb.calls) == 2
    assert len(pm.calls) == 2
    from sports_hedge.execution.live_canary import disarm_live_runtime

    disarm_live_runtime(settings)
    assert live_runtime_armed() is False
    before = len(mb.calls)
    third = _plan(
        "watch:third",
        stake="1.50",
        snapshot=_snapshot(stake="1.50", snapshot_id="exec:third"),
        canonical_event_id="evt-3",
    )
    _register(ops, third)
    try:
        ops.capture_opening_fill(third.opportunity_id, now=NOW)
    except PaperOperationsError:
        pass
    assert len(mb.calls) == before


def test_combined_denies_unknown_currency_geoblock_and_orphan(tmp_path: Path) -> None:
    settings = _canary_settings(tmp_path)
    runtime, mb, _pm = _runtime()
    set_live_runtime_arm(True)

    unknown_fx = LiveCanaryTradingFacts(
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
    set_live_canary_trading_facts(unknown_fx)
    assert "matchbook_currency_unknown" in trading_permission_reasons(settings, unknown_fx)
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "deny.sqlite"), _plan())
    ops.capture_opening_fill("watch:mkt-canary", now=NOW)
    assert mb.calls == []

    geo = LiveCanaryTradingFacts(
        polymarket_geoblock_permitted=False,
        polymarket_geoblock_reason="geoblock_blocked",
        polymarket_credentials_present=True,
        polymarket_wallet_valid=True,
        polymarket_balance_readable=True,
        polymarket_trading_ready=True,
        matchbook_credentials_present=True,
        matchbook_authenticated=True,
        matchbook_free_funds_positive=True,
        matchbook_currency_compatible=True,
        injected=True,
    )
    set_live_canary_trading_facts(geo)
    ops.capture_opening_fill("watch:mkt-canary", now=NOW)
    assert mb.calls == []

    orphan = LiveCanaryTradingFacts(
        polymarket_geoblock_permitted=True,
        polymarket_geoblock_reason="geoblock_permitted",
        polymarket_credentials_present=True,
        polymarket_wallet_valid=True,
        polymarket_balance_readable=True,
        polymarket_trading_ready=True,
        matchbook_credentials_present=True,
        matchbook_authenticated=True,
        matchbook_free_funds_positive=True,
        matchbook_currency_compatible=True,
        orphan_inspection_failed=True,
        outstanding_unknown_orders=True,
        injected=True,
    )
    set_live_canary_trading_facts(orphan)
    reasons = trading_permission_reasons(settings, orphan)
    assert "orphan_inspection_failed" in reasons
    ops.capture_opening_fill("watch:mkt-canary", now=NOW)
    assert mb.calls == []


def test_nonlocal_arm_is_rejected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _canary_settings(tmp_path)
    monkeypatch.setattr("sports_hedge.api.live_canary.get_settings", lambda: settings)
    client = TestClient(app)
    denied = client.post(
        "/live-canary/arm",
        json={"phrase": "ARM LIVE CANARY"},
        headers={"Origin": "https://evil.example"},
    )
    assert denied.status_code == 403
    missing = client.post("/live-canary/arm", json={"phrase": "ARM LIVE CANARY"})
    assert missing.status_code == 403
    local = client.post(
        "/live-canary/arm",
        json={"phrase": "nope"},
        headers=LOCAL_ORIGIN,
    )
    assert local.status_code == 200
    assert local.json()["runtime_armed"] is False
    assert live_runtime_armed() is False
