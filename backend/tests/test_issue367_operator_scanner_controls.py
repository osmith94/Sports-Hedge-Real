"""Issue #367 — Paper Scanner operator Update / Stop / Resume controls."""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api import paper as paper_api
from sports_hedge.api.main import app
from sports_hedge.application.collector import CollectionReport
from sports_hedge.application.live_refresh import (
    ExplicitCollectBusy,
    LiveRefreshCoordinator,
    get_live_refresh_coordinator,
)
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.config import Settings
from sports_hedge.persistence.operator_scanner_settings import (
    SCANNER_STOPPED_BY_OPERATOR,
    SqliteOperatorScannerSettingsStore,
    bind_runtime_operator_scanner_settings_store,
    effective_operator_scanner_settings,
    get_operator_scanner_settings_store,
    resolve_operator_scanner_settings,
)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def _bind_store(tmp_path: Path) -> tuple[LiveRefreshCoordinator, SqliteOperatorScannerSettingsStore]:
    store = SqliteOperatorScannerSettingsStore(tmp_path / "operator-scanner.sqlite")
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    coordinator.bind_operator_settings_store(store)
    return coordinator, store


def _unbind(coordinator: LiveRefreshCoordinator, store: SqliteOperatorScannerSettingsStore) -> None:
    memory = SqliteOperatorScannerSettingsStore(":memory:")
    coordinator.bind_operator_settings_store(memory)
    coordinator.reset()
    bind_runtime_operator_scanner_settings_store(None)
    coordinator._operator_store = None
    get_operator_scanner_settings_store.cache_clear()
    store.close()
    memory.close()


def test_absent_override_uses_environment_defaults(tmp_path: Path) -> None:
    store = SqliteOperatorScannerSettingsStore(tmp_path / "empty.sqlite")
    settings = Settings(
        min_net_edge=0.02,
        max_execution_risk=40,
        paper_live_refresh_interval_seconds=45,
    )
    resolved = resolve_operator_scanner_settings(store, settings)
    assert resolved.min_net_edge == Decimal("0.02")
    assert resolved.max_execution_risk == 40
    assert resolved.hot_cadence_seconds == 45
    assert resolved.hot_reprice_after_seconds == 45
    assert resolved.hot_scan_interval_seconds == 10
    assert resolved.background_reprice_after_seconds == 600
    assert resolved.background_scan_interval_seconds == 10
    assert resolved.universe_discovery_refresh_seconds == 3600
    assert resolved.max_allocated_per_trade_gbp == Decimal("1000")
    assert resolved.scanner_stopped is False
    assert resolved.source == "env_default"
    assert resolved.restart_semantics == "remain_stopped_until_resume"
    store.close()


def test_saved_settings_used_by_scheduled_kwargs_and_survive_restart(tmp_path: Path) -> None:
    coordinator, store = _bind_store(tmp_path)
    try:
        saved = coordinator.apply_operator_scan_settings(
            min_net_edge=Decimal("0.0125"),
            max_execution_risk=33,
            hot_cadence_seconds=15,
            background_cadence_seconds=90,
        )
        assert saved.source == "operator"
        scheduled = paper_api.scheduled_collection_kwargs()
        assert Decimal(str(scheduled["minimum_net_edge"])) == Decimal("0.0125")
        assert scheduled["maximum_execution_risk"] == 33
        assert coordinator.status.interval_seconds == 10
        assert coordinator.status.hot.scan_interval_seconds == 10
        assert coordinator.status.hot.reprice_after_seconds == 15
        store.close()
        restarted = SqliteOperatorScannerSettingsStore(tmp_path / "operator-scanner.sqlite")
        loaded = resolve_operator_scanner_settings(restarted)
        assert loaded.min_net_edge == Decimal("0.0125")
        assert loaded.max_execution_risk == 33
        assert loaded.hot_cadence_seconds == 15
        assert loaded.background_cadence_seconds == 90
        restarted.close()
        store = SqliteOperatorScannerSettingsStore(tmp_path / "operator-scanner.sqlite")
        coordinator.bind_operator_settings_store(store)
    finally:
        _unbind(coordinator, store)


def test_update_http_is_backend_authoritative_and_does_not_scan(tmp_path: Path) -> None:
    coordinator, store = _bind_store(tmp_path)
    client = TestClient(app)
    ticks: list[str] = []

    async def boom(_plan=None) -> None:
        ticks.append("tick")
        raise AssertionError("Update must not trigger scanner work")

    original = paper_api.server_owned_refresh_tick
    paper_api.server_owned_refresh_tick = boom  # type: ignore[method-assign]
    try:
        response = client.put(
            "/paper/operator-scanner-settings",
            json={
                "min_net_edge": "0.0075",
                "max_execution_risk": 41,
                "hot_cadence_seconds": 20,
                "background_cadence_seconds": 90,
            },
        )
        assert response.status_code == 200
        body = response.json()
        settings = body["operator_settings"]
        assert Decimal(str(settings["min_net_edge"])) == Decimal("0.0075")
        assert settings["max_execution_risk"] == 41
        assert settings["hot_cadence_seconds"] == 20
        assert settings["background_cadence_seconds"] == 90
        assert settings["source"] == "operator"
        assert body["interval_seconds"] == 10
        assert body["hot"]["scan_interval_seconds"] == 10
        assert body["hot"]["reprice_after_seconds"] == 20
        assert body["background"]["scan_interval_seconds"] == 10
        assert body["background"]["reprice_after_seconds"] == 90
        again = client.get("/paper/live-refresh").json()
        assert Decimal(str(again["operator_settings"]["min_net_edge"])) == Decimal("0.0075")
        assert ticks == []
        put_src = inspect.getsource(paper_api.put_operator_scanner_settings)
        assert "collect_and_scan" not in put_src
        assert "server_owned_refresh_tick" not in put_src
        assert "run_price_engine_slice" not in put_src
    finally:
        paper_api.server_owned_refresh_tick = original
        _unbind(coordinator, store)


def test_update_shifts_hot_due_without_running_a_cycle(tmp_path: Path) -> None:
    clock = {"now": NOW}

    def now() -> datetime:
        return clock["now"]

    store = SqliteOperatorScannerSettingsStore(tmp_path / "cadence.sqlite")
    coordinator = LiveRefreshCoordinator(clock=now, operator_settings_store=store)
    coordinator.configure_from_settings()
    coordinator._next_hot_due = NOW
    coordinator.apply_operator_scan_settings(
        min_net_edge=Decimal("0.005"),
        max_execution_risk=60,
        hot_cadence_seconds=15,
        background_cadence_seconds=90,
    )
    assert coordinator._next_hot_due == NOW + timedelta(seconds=10)
    assert coordinator.status.hot.reprice_after_seconds == 15
    assert coordinator.plan_hot_tick(now=NOW).reason == "waiting"
    due = coordinator.plan_hot_tick(now=NOW + timedelta(seconds=10))
    assert due.reason in {"hot_due", "hot_scope_empty"}
    bind_runtime_operator_scanner_settings_store(None)
    store.close()


def test_stop_prevents_hot_universe_background_and_preserves_state(tmp_path: Path) -> None:
    coordinator, store = _bind_store(tmp_path)
    client = TestClient(app)
    sentinel = object()
    coordinator._catalogue_store = sentinel
    coordinator.status = coordinator.status.model_copy(
        update={"last_matched_event_pairs": 7}
    )
    previous_pairs = coordinator.status.last_matched_event_pairs
    try:
        stopped = client.post("/paper/scanner/stop").json()
        assert stopped["scanner_stopped"] is True
        assert stopped["operator_settings"]["scanner_stopped"] is True
        assert stopped["operator_settings"]["restart_semantics"] == "remain_stopped_until_resume"
        assert coordinator.plan_hot_tick(now=NOW).reason == "operator_stopped"
        assert coordinator.plan_universe_tick(now=NOW).reason == "operator_stopped"
        assert coordinator.plan_background_tick(now=NOW).reason == "operator_stopped"
        assert coordinator.plan_active_trade_tick(now=NOW).reason == "operator_stopped"
        assert coordinator.status.last_matched_event_pairs == previous_pairs
        assert coordinator._catalogue_store is sentinel
        live = client.get("/paper/live-refresh").json()
        assert live["scanner_stopped"] is True
        stop_src = inspect.getsource(paper_api.stop_paper_scanner)
        resume_src = inspect.getsource(paper_api.resume_paper_scanner)
        assert "collect_and_scan" not in stop_src
        assert "collect_and_scan" not in resume_src
        restored = SqliteOperatorScannerSettingsStore(tmp_path / "operator-scanner.sqlite")
        loaded = restored.load()
        assert loaded is not None
        assert loaded.scanner_stopped is True
        restored.close()
        resumed = client.post("/paper/scanner/resume").json()
        assert resumed["scanner_stopped"] is False
        assert coordinator.plan_hot_tick(now=NOW).reason != "operator_stopped"
        assert coordinator._catalogue_store is sentinel
    finally:
        _unbind(coordinator, store)


@pytest.mark.asyncio
async def test_stopped_workers_do_not_invoke_ticks(tmp_path: Path) -> None:
    store = SqliteOperatorScannerSettingsStore(tmp_path / "pause.sqlite")
    ticks: list[str] = []

    async def tick(plan=None) -> CollectionReport:
        ticks.append(getattr(plan, "lane", "unknown"))
        now = datetime.now(UTC)
        return CollectionReport(started_at=now, completed_at=now)

    coordinator = LiveRefreshCoordinator(operator_settings_store=store)
    coordinator.configure_from_settings()
    coordinator.apply_operator_scanner_stopped(True)
    coordinator._next_hot_due = coordinator.now()
    coordinator._next_universe_due = coordinator.now()
    coordinator._next_background_due = coordinator.now()
    hot = asyncio.create_task(coordinator._hot_loop(tick))
    universe = asyncio.create_task(coordinator._universe_loop(tick))
    background = asyncio.create_task(coordinator._background_loop(tick))
    active = asyncio.create_task(coordinator._active_trade_loop(tick))
    try:
        await asyncio.sleep(0.15)
        assert ticks == []
        assert coordinator.status.hot.last_plan_reason == "operator_stopped"
        assert coordinator.status.active_trade.last_plan_reason == "operator_stopped"
        coordinator._stop.set()
        coordinator._pulse_control()
        await asyncio.wait_for(asyncio.gather(hot, universe, background, active), timeout=2)
    finally:
        for task in (hot, universe, background, active):
            if not task.done():
                task.cancel()
        bind_runtime_operator_scanner_settings_store(None)
        store.close()


def test_stop_does_not_clear_trades_or_add_fill_veto() -> None:
    stop_src = inspect.getsource(paper_api.stop_paper_scanner)
    resume_src = inspect.getsource(paper_api.resume_paper_scanner)
    apply_src = inspect.getsource(LiveRefreshCoordinator.apply_operator_scanner_stopped)
    persist_src = inspect.getsource(PaperOperationsService.persist_triggered_chain)
    assert "trades.clear" not in stop_src
    assert "fixture_state.clear" not in apply_src
    assert "reset(" not in resume_src
    assert "execution_risk_above_threshold" not in persist_src


def test_frontend_renders_backend_settings_not_a_second_authority() -> None:
    scan = Path(__file__).resolve().parents[2] / "frontend" / "components" / "run-paper-scan.tsx"
    text = scan.read_text(encoding="utf-8")
    assert "DEFAULT_SCANNER_ASSUMPTIONS" not in text
    assert "operator_settings" in text
    assert "forceSettings" in text
    assert "saveOperatorScannerSettings" in text
    assert "Auto refresh view" in text
    assert "HOT target refresh s" in text
    assert "HOT scan interval s" not in text
    assert "HOT reprice after s" not in text
    assert "BACKGROUND scan interval s" not in text
    assert "BACKGROUND reprice after s" not in text
    assert "UNIVERSE discovery refresh s" in text
    assert "hot_target_refresh_seconds" in text
    assert "background_reprice_after_seconds" not in text
    assert "universe_discovery_refresh_seconds" in text
    assert "HOT cadence s" not in text
    assert "BACKGROUND cadence s" not in text
    assert "UNIVERSE cadence s" not in text
    assert "disabled={loading || scannerStopped}" in text
    assert "if (liveRefresh?.scanner_stopped) return;" in text
    assert text.count("disabled={loading || scannerStopped}") >= 2


def test_effective_settings_follow_runtime_store(tmp_path: Path) -> None:
    coordinator, store = _bind_store(tmp_path)
    try:
        coordinator.apply_operator_scan_settings(
            min_net_edge=Decimal("0.009"),
            max_execution_risk=22,
            hot_cadence_seconds=60,
            background_cadence_seconds=90,
        )
        effective = effective_operator_scanner_settings()
        assert effective.min_net_edge == Decimal("0.009")
        assert effective.max_execution_risk == 22
        assert effective.hot_cadence_seconds == 60
        assert effective.background_cadence_seconds == 90
        invalid = TestClient(app).put(
            "/paper/operator-scanner-settings",
            json={
                "min_net_edge": "1.5",
                "max_execution_risk": 22,
                "hot_cadence_seconds": 60,
                "background_cadence_seconds": 90,
            },
        )
        assert invalid.status_code == 422
    finally:
        _unbind(coordinator, store)


def test_health_exposes_stopped_flag_without_provider_io(tmp_path: Path) -> None:
    coordinator, store = _bind_store(tmp_path)
    try:
        coordinator.apply_operator_scanner_stopped(True)
        health = TestClient(app).get("/health").json()
        assert health["execution_enabled"] is False
        assert health["mode"] == "paper"
        assert health["live_refresh"]["scanner_stopped"] is True
        assert "place_order" not in health
    finally:
        _unbind(coordinator, store)


@pytest.mark.asyncio
async def test_stopped_coordinator_manuals_raise_without_running_runner(
    tmp_path: Path,
) -> None:
    coordinator, store = _bind_store(tmp_path)
    calls: list[str] = []

    async def runner() -> CollectionReport:
        calls.append("run")
        raise AssertionError("stopped manuals must not start collector work")

    try:
        coordinator.apply_operator_scanner_stopped(True)
        with pytest.raises(ExplicitCollectBusy, match=SCANNER_STOPPED_BY_OPERATOR):
            await coordinator.run_manual_hot(runner)
        with pytest.raises(ExplicitCollectBusy, match=SCANNER_STOPPED_BY_OPERATOR):
            await coordinator.run_explicit_collect(runner)
        assert calls == []

        coordinator.apply_operator_scanner_stopped(False)
        now = datetime.now(UTC)

        async def ok() -> CollectionReport:
            calls.append("ok")
            return CollectionReport(started_at=now, completed_at=now)

        await coordinator.run_manual_hot(ok, timeout_seconds=0.5)
        await coordinator.run_explicit_collect(ok)
        assert calls == ["ok", "ok"]
    finally:
        _unbind(coordinator, store)


def test_stopped_manual_http_does_not_invoke_collector_and_resume_restores(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sports_hedge.api.watchlist import get_watchlist_service

    coordinator, store = _bind_store(tmp_path)
    collect_calls: list[str] = []
    persist_calls: list[str] = []
    previous_request = coordinator.last_request()

    async def boom(_kwargs, **_kwargs_extra):
        collect_calls.append("collect")
        raise AssertionError("stopped manuals must not invoke collector/provider work")

    def persist_boom(*_args, **_kwargs) -> None:
        persist_calls.append("persist")
        raise AssertionError("stopped manuals must not persist")

    monkeypatch.setattr(paper_api, "_collect_report", boom)
    monkeypatch.setattr(paper_api, "persist_manual_hot_after_http_response", persist_boom)
    monkeypatch.setattr(paper_api, "persist_explicit_collect_after_http_response", persist_boom)
    app.dependency_overrides[paper_api.get_paper_scan_service] = lambda: object()
    app.dependency_overrides[paper_api.get_paper_audit_repository] = lambda: object()
    app.dependency_overrides[get_watchlist_service] = lambda: object()
    client = TestClient(app)
    try:
        stopped = client.post("/paper/scanner/stop")
        assert stopped.status_code == 200
        assert stopped.json()["scanner_stopped"] is True

        hot = client.post("/paper/collect/hot", json={"maximum_execution_risk": 60})
        assert hot.status_code == 409
        assert hot.json()["detail"] == SCANNER_STOPPED_BY_OPERATOR

        diagnostic = client.post("/paper/collect", json={"maximum_execution_risk": 60})
        assert diagnostic.status_code == 409
        assert diagnostic.json()["detail"] == SCANNER_STOPPED_BY_OPERATOR

        assert collect_calls == []
        assert persist_calls == []
        assert coordinator.last_request() == previous_request
        assert coordinator.operator_scanner_stopped is True

        live = client.get("/paper/live-refresh")
        assert live.status_code == 200
        assert live.json()["scanner_stopped"] is True

        update = client.put(
            "/paper/operator-scanner-settings",
            json={
                "min_net_edge": "0.006",
                "max_execution_risk": 55,
                "hot_cadence_seconds": 18,
                "background_cadence_seconds": 90,
            },
        )
        assert update.status_code == 200
        assert update.json()["scanner_stopped"] is True
        assert collect_calls == []

        hot_src = inspect.getsource(paper_api.refresh_hot_read_only_market_data)
        collect_src = inspect.getsource(paper_api.collect_read_only_market_data)
        assert hot_src.index("operator_scanner_stopped") < hot_src.index("remember_request")
        assert collect_src.index("operator_scanner_stopped") < collect_src.index(
            "remember_request"
        )

        resumed = client.post("/paper/scanner/resume")
        assert resumed.status_code == 200
        assert resumed.json()["scanner_stopped"] is False

        now = datetime.now(UTC)

        async def fake_collect(_kwargs, **_kwargs_extra):
            collect_calls.append("collect")
            return CollectionReport(started_at=now, completed_at=now)

        monkeypatch.setattr(paper_api, "_collect_report", fake_collect)
        monkeypatch.setattr(
            paper_api, "persist_manual_hot_after_http_response", lambda *_a, **_k: None
        )
        monkeypatch.setattr(
            paper_api, "persist_explicit_collect_after_http_response", lambda *_a, **_k: None
        )

        restored_hot = client.post("/paper/collect/hot", json={"maximum_execution_risk": 60})
        assert restored_hot.status_code == 200, restored_hot.text
        restored_diag = client.post("/paper/collect", json={"maximum_execution_risk": 60})
        assert restored_diag.status_code == 200, restored_diag.text
        assert collect_calls == ["collect", "collect"]
        assert persist_calls == []
    finally:
        app.dependency_overrides.clear()
        _unbind(coordinator, store)
