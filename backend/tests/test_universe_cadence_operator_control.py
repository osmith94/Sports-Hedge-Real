"""Fresh UNIVERSE discovery cadence is an operator-editable persisted setting.

PAPER / read-only. Deterministic coordinator clocks. Not live quotes.
This is the post-completion fresh-generation interval only — not radar TTL,
intra-generation worker cooldown, or generation budget.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.scan_lanes import (
    DEFAULT_UNIVERSE_DISCOVERY_INTERVAL_SECONDS,
    ScanLane,
)
from sports_hedge.config import Settings
from sports_hedge.persistence.operator_scanner_settings import (
    DEFAULT_UNIVERSE_CADENCE_SECONDS,
    UNIVERSE_CADENCE_MAX_SECONDS,
    UNIVERSE_CADENCE_MIN_SECONDS,
    SqliteOperatorScannerSettingsStore,
    bind_runtime_operator_scanner_settings_store,
    resolve_operator_scanner_settings,
)
from test_dual_cadence_scheduler import NOW, FakeClock
from test_issue303_universe_persistent_worker import _open_owner_live_generation
from test_issue367_operator_scanner_controls import _bind_store, _unbind

REPO_ROOT = Path(__file__).resolve().parents[2]


def test_fresh_install_resolves_universe_cadence_to_1800s(tmp_path: Path) -> None:
    settings = Settings()
    assert settings.paper_universe_discovery_interval_seconds == 1800
    assert DEFAULT_UNIVERSE_CADENCE_SECONDS == 1800
    assert DEFAULT_UNIVERSE_DISCOVERY_INTERVAL_SECONDS == 1800
    assert settings.paper_live_refresh_universe_interval_seconds == 180
    assert settings.paper_universe_worker_cooldown_seconds == 8
    assert settings.paper_scan_universe_generation_budget_seconds == 150
    assert settings.paper_universe_current_state_ttl_seconds == 360
    store = SqliteOperatorScannerSettingsStore(tmp_path / "empty.sqlite")
    resolved = resolve_operator_scanner_settings(store, settings)
    assert resolved.universe_cadence_seconds == 1800
    assert resolved.source == "env_default"
    coordinator = LiveRefreshCoordinator(operator_settings_store=store)
    coordinator.configure_from_settings(settings)
    assert coordinator.status.universe.cadence_seconds == 1800
    assert coordinator.status.operator_settings is not None
    assert coordinator.status.operator_settings.universe_cadence_seconds == 1800
    store.close()


def test_universe_cadence_is_distinct_from_ttl_cooldown_and_budget() -> None:
    settings = Settings()
    assert settings.paper_universe_discovery_interval_seconds != settings.paper_live_refresh_universe_interval_seconds
    assert settings.paper_universe_discovery_interval_seconds != settings.paper_universe_worker_cooldown_seconds
    assert settings.paper_universe_discovery_interval_seconds != settings.paper_scan_universe_generation_budget_seconds
    assert settings.paper_universe_discovery_interval_seconds != settings.paper_universe_current_state_ttl_seconds
    assert settings.paper_universe_discovery_interval_seconds != settings.paper_background_price_interval_seconds
    close_src = inspect.getsource(LiveRefreshCoordinator._close_universe_generation)
    pause_src = inspect.getsource(LiveRefreshCoordinator._pause_universe_generation)
    apply_src = inspect.getsource(LiveRefreshCoordinator._apply_operator_settings_unlocked)
    assert "_effective_universe_cadence_seconds" in close_src
    assert "paper_universe_worker_cooldown_seconds" in pause_src
    assert "paper_universe_discovery_interval_seconds" not in pause_src
    assert "universe_cadence_changed" in apply_src
    assert "_universe_generation_started_at is None" in apply_src


def test_persisted_universe_cadence_is_used_by_scheduler_without_restart(
    tmp_path: Path,
) -> None:
    clock = FakeClock(NOW)
    store = SqliteOperatorScannerSettingsStore(tmp_path / "universe-cadence.sqlite")
    coordinator = LiveRefreshCoordinator(clock=clock, operator_settings_store=store)
    try:
        coordinator.configure_from_settings()
        coordinator._clock = clock
        coordinator._next_universe_due = NOW
        saved = coordinator.apply_operator_scan_settings(
            min_net_edge=Decimal("0.005"),
            max_execution_risk=60,
            hot_cadence_seconds=30,
            background_cadence_seconds=90,
            universe_cadence_seconds=900,
        )
        assert saved.universe_cadence_seconds == 900
        assert saved.source == "operator"
        assert coordinator.status.universe.cadence_seconds == 900
        assert coordinator._next_universe_due == NOW + timedelta(seconds=900)
        waiting = coordinator.plan_universe_tick(now=NOW + timedelta(seconds=899))
        assert waiting.lane == "idle"
        assert waiting.reason == "universe_cooldown"
        due = coordinator.plan_universe_tick(now=NOW + timedelta(seconds=900))
        assert due.lane == ScanLane.UNIVERSE.value
        assert due.generation_resume is False
        load = coordinator.public_status().system_load
        assert load.universe.cadence_seconds == 900
        assert coordinator.status.hot.cadence_seconds == 30
        assert coordinator.status.background.cadence_seconds == 90
    finally:
        bind_runtime_operator_scanner_settings_store(None)
        store.close()


def test_universe_cadence_survives_store_reopen(tmp_path: Path) -> None:
    db = tmp_path / "restart-universe.sqlite"
    first = SqliteOperatorScannerSettingsStore(db)
    first.save_settings(
        min_net_edge=Decimal("0.01"),
        max_execution_risk=40,
        hot_cadence_seconds=20,
        background_cadence_seconds=90,
        universe_cadence_seconds=2400,
    )
    first.close()
    restarted = SqliteOperatorScannerSettingsStore(db)
    loaded = restarted.load()
    assert loaded is not None
    assert loaded.hot_cadence_seconds == 20
    assert loaded.background_cadence_seconds == 90
    assert loaded.universe_cadence_seconds == 2400
    assert loaded.source == "operator"
    coordinator = LiveRefreshCoordinator(operator_settings_store=restarted)
    coordinator.configure_from_settings()
    assert coordinator.status.universe.cadence_seconds == 2400
    assert coordinator.status.hot.cadence_seconds == 20
    assert coordinator.status.background.cadence_seconds == 90
    restarted.close()


def test_put_get_contract_exposes_universe_cadence(tmp_path: Path) -> None:
    coordinator, store = _bind_store(tmp_path)
    client = TestClient(app)
    try:
        response = client.put(
            "/paper/operator-scanner-settings",
            json={
                "min_net_edge": "0.005",
                "max_execution_risk": 60,
                "hot_cadence_seconds": 30,
                "background_cadence_seconds": 90,
                "universe_cadence_seconds": 1200,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["operator_settings"]["universe_cadence_seconds"] == 1200
        assert body["universe"]["cadence_seconds"] == 1200
        fetched = client.get("/paper/operator-scanner-settings").json()
        assert fetched["universe_cadence_seconds"] == 1200
        assert fetched["source"] == "operator"
        live = client.get("/paper/live-refresh").json()
        assert live["operator_settings"]["universe_cadence_seconds"] == 1200
        assert live["universe"]["cadence_seconds"] == 1200
        assert live["hot"]["cadence_seconds"] == 30
        assert live["background"]["cadence_seconds"] == 90
    finally:
        _unbind(coordinator, store)


def test_universe_cadence_invalid_range_rejected(tmp_path: Path) -> None:
    coordinator, store = _bind_store(tmp_path)
    client = TestClient(app)
    try:
        for value in (59, 3601, 0, UNIVERSE_CADENCE_MIN_SECONDS - 1, UNIVERSE_CADENCE_MAX_SECONDS + 1):
            response = client.put(
                "/paper/operator-scanner-settings",
                json={
                    "min_net_edge": "0.005",
                    "max_execution_risk": 60,
                    "hot_cadence_seconds": 30,
                    "background_cadence_seconds": 90,
                    "universe_cadence_seconds": value,
                },
            )
            assert response.status_code == 422, value
        ok = client.put(
            "/paper/operator-scanner-settings",
            json={
                "min_net_edge": "0.005",
                "max_execution_risk": 60,
                "hot_cadence_seconds": 30,
                "background_cadence_seconds": 90,
                "universe_cadence_seconds": 1800,
            },
        )
        assert ok.status_code == 200
        assert ok.json()["operator_settings"]["universe_cadence_seconds"] == 1800
        bounds = client.put(
            "/paper/operator-scanner-settings",
            json={
                "min_net_edge": "0.005",
                "max_execution_risk": 60,
                "hot_cadence_seconds": 30,
                "background_cadence_seconds": 90,
                "universe_cadence_seconds": 3600,
            },
        )
        assert bounds.status_code == 200
        assert bounds.json()["universe"]["cadence_seconds"] == 3600
        low = client.put(
            "/paper/operator-scanner-settings",
            json={
                "min_net_edge": "0.005",
                "max_execution_risk": 60,
                "hot_cadence_seconds": 30,
                "background_cadence_seconds": 90,
                "universe_cadence_seconds": 60,
            },
        )
        assert low.status_code == 200
        assert low.json()["universe"]["cadence_seconds"] == 60
    finally:
        _unbind(coordinator, store)


def test_legacy_singleton_row_migrates_universe_cadence_default_1800(tmp_path: Path) -> None:
    db = tmp_path / "legacy-universe.sqlite"
    connection = sqlite3.connect(db)
    connection.execute(
        """
        CREATE TABLE operator_scanner_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            min_net_edge TEXT NOT NULL,
            max_execution_risk INTEGER NOT NULL,
            hot_cadence_seconds INTEGER NOT NULL,
            background_cadence_seconds INTEGER NOT NULL DEFAULT 90,
            max_allocated_per_trade_gbp TEXT,
            scanner_stopped INTEGER NOT NULL,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO operator_scanner_settings (
            id, min_net_edge, max_execution_risk, hot_cadence_seconds,
            background_cadence_seconds, scanner_stopped, source, updated_at
        ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("0.0125", 33, 15, 90, 0, "operator", datetime.now(UTC).isoformat()),
    )
    connection.commit()
    connection.close()
    store = SqliteOperatorScannerSettingsStore(db)
    loaded = store.load()
    assert loaded is not None
    assert loaded.source == "operator"
    assert loaded.hot_cadence_seconds == 15
    assert loaded.background_cadence_seconds == 90
    assert loaded.universe_cadence_seconds == 1800
    columns = {
        str(row[1])
        for row in sqlite3.connect(db).execute("PRAGMA table_info(operator_scanner_settings)")
    }
    assert "universe_cadence_seconds" in columns
    store.close()


def test_changing_universe_cadence_shifts_next_due_without_running_scan(tmp_path: Path) -> None:
    coordinator, store = _bind_store(tmp_path)
    client = TestClient(app)
    ticks: list[str] = []

    async def boom(_plan=None) -> None:
        ticks.append("tick")
        raise AssertionError("UNIVERSE cadence Update must not trigger scanner work")

    from sports_hedge.api import paper as paper_api

    original = paper_api.server_owned_refresh_tick
    paper_api.server_owned_refresh_tick = boom  # type: ignore[method-assign]
    try:
        coordinator.configure_from_settings()
        coordinator._next_universe_due = NOW
        response = client.put(
            "/paper/operator-scanner-settings",
            json={
                "min_net_edge": "0.005",
                "max_execution_risk": 60,
                "hot_cadence_seconds": 30,
                "background_cadence_seconds": 90,
                "universe_cadence_seconds": 1200,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["operator_settings"]["universe_cadence_seconds"] == 1200
        assert body["universe"]["cadence_seconds"] == 1200
        assert body["hot"]["cadence_seconds"] == 30
        assert ticks == []
        put_src = inspect.getsource(paper_api.put_operator_scanner_settings)
        assert "collect_and_scan" not in put_src
        assert "run_price_engine_slice" not in put_src
        assert "server_owned_refresh_tick" not in put_src
        assert "request_universe_run_now" not in put_src
    finally:
        paper_api.server_owned_refresh_tick = original
        _unbind(coordinator, store)


def test_update_shifts_universe_due_without_opening_a_generation(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    store = SqliteOperatorScannerSettingsStore(tmp_path / "universe-due.sqlite")
    coordinator = LiveRefreshCoordinator(clock=clock, operator_settings_store=store)
    coordinator.configure_from_settings()
    coordinator._clock = clock
    coordinator._next_universe_due = NOW
    coordinator.apply_operator_scan_settings(
        min_net_edge=Decimal("0.005"),
        max_execution_risk=60,
        hot_cadence_seconds=30,
        background_cadence_seconds=90,
        universe_cadence_seconds=1500,
    )
    assert coordinator._next_universe_due == NOW + timedelta(seconds=1500)
    assert coordinator._universe_generation_started_at is None
    assert coordinator.plan_universe_tick(now=NOW).reason == "universe_cooldown"
    due = coordinator.plan_universe_tick(now=NOW + timedelta(seconds=1500))
    assert due.lane == ScanLane.UNIVERSE.value
    bind_runtime_operator_scanner_settings_store(None)
    store.close()


def test_open_generation_keeps_worker_cooldown_when_cadence_changes(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    store = SqliteOperatorScannerSettingsStore(tmp_path / "open-gen.sqlite")
    coordinator = LiveRefreshCoordinator(clock=clock, operator_settings_store=store)
    coordinator.configure_from_settings()
    coordinator._clock = clock
    _open_owner_live_generation(coordinator)
    finished = NOW + timedelta(seconds=4)
    coordinator._pause_universe_generation(finished)
    paused_due = finished + timedelta(seconds=8)
    assert coordinator._next_universe_due == paused_due
    coordinator.apply_operator_scan_settings(
        min_net_edge=Decimal("0.005"),
        max_execution_risk=60,
        hot_cadence_seconds=30,
        background_cadence_seconds=90,
        universe_cadence_seconds=900,
    )
    assert coordinator.status.universe.cadence_seconds == 900
    assert coordinator._next_universe_due == paused_due
    assert coordinator._universe_generation_started_at is not None
    bind_runtime_operator_scanner_settings_store(None)
    store.close()


def test_completed_generation_uses_saved_universe_cadence(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    store = SqliteOperatorScannerSettingsStore(tmp_path / "complete-gen.sqlite")
    coordinator = LiveRefreshCoordinator(clock=clock, operator_settings_store=store)
    coordinator.configure_from_settings()
    coordinator._clock = clock
    coordinator.apply_operator_scan_settings(
        min_net_edge=Decimal("0.005"),
        max_execution_risk=60,
        hot_cadence_seconds=30,
        background_cadence_seconds=90,
        universe_cadence_seconds=900,
    )
    coordinator._universe_generation_id = 3
    coordinator._universe_generation_started_at = NOW
    coordinator._close_universe_generation(NOW + timedelta(seconds=12))
    assert coordinator._next_universe_due == NOW + timedelta(seconds=912)
    idle = coordinator.plan_universe_tick(now=NOW + timedelta(seconds=911))
    assert idle.reason == "universe_cooldown"
    nxt = coordinator.plan_universe_tick(now=NOW + timedelta(seconds=912))
    assert nxt.lane == ScanLane.UNIVERSE.value
    bind_runtime_operator_scanner_settings_store(None)
    store.close()


def test_universe_now_still_bypasses_cadence_wait(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    store = SqliteOperatorScannerSettingsStore(tmp_path / "universe-now.sqlite")
    coordinator = LiveRefreshCoordinator(clock=clock, operator_settings_store=store)
    coordinator.configure_from_settings()
    coordinator._clock = clock
    coordinator.apply_operator_scan_settings(
        min_net_edge=Decimal("0.005"),
        max_execution_risk=60,
        hot_cadence_seconds=30,
        background_cadence_seconds=90,
        universe_cadence_seconds=1800,
    )
    coordinator._next_universe_due = NOW + timedelta(seconds=1800)
    waiting = coordinator.plan_universe_tick(now=NOW)
    assert waiting.reason == "universe_cooldown"
    state = coordinator.request_universe_run_now()
    assert state in {"due", "running"}
    assert coordinator._next_universe_due == NOW
    due = coordinator.plan_universe_tick(now=NOW)
    assert due.lane == ScanLane.UNIVERSE.value
    bind_runtime_operator_scanner_settings_store(None)
    store.close()


def test_changing_universe_cadence_does_not_change_ttl_or_cooldown(tmp_path: Path) -> None:
    settings = Settings()
    store = SqliteOperatorScannerSettingsStore(tmp_path / "ttl.sqlite")
    coordinator = LiveRefreshCoordinator(operator_settings_store=store)
    coordinator.configure_from_settings(settings)
    coordinator.apply_operator_scan_settings(
        min_net_edge=Decimal("0.005"),
        max_execution_risk=60,
        hot_cadence_seconds=30,
        background_cadence_seconds=90,
        universe_cadence_seconds=2400,
    )
    resolved = Settings()
    assert resolved.paper_live_refresh_universe_interval_seconds == 180
    assert resolved.paper_universe_worker_cooldown_seconds == 8
    assert resolved.paper_scan_universe_generation_budget_seconds == 150
    assert resolved.paper_universe_current_state_ttl_seconds == 360
    assert coordinator.status.universe.generation_budget_seconds == 150
    assert coordinator.status.universe.cadence_seconds == 2400
    bind_runtime_operator_scanner_settings_store(None)
    store.close()


def test_frontend_exposes_universe_cadence_beside_hot_and_background() -> None:
    scan = (REPO_ROOT / "frontend" / "components" / "run-paper-scan.tsx").read_text(
        encoding="utf-8"
    )
    api = (REPO_ROOT / "frontend" / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "UNIVERSE cadence s" in scan
    assert "HOT cadence s" in scan
    assert "BACKGROUND cadence s" in scan
    assert scan.index("HOT cadence s") < scan.index("BACKGROUND cadence s")
    assert scan.index("BACKGROUND cadence s") < scan.index("UNIVERSE cadence s")
    assert "universe_cadence_seconds: universeCadence" in scan
    assert "clampUniverseCadenceSeconds" in scan
    assert "Math.min(3600, Math.max(60" in scan
    assert "DEFAULT_UNIVERSE_CADENCE_SECONDS = 1800" in scan
    assert "does not trigger a scan" in scan
    assert "universe_cadence_seconds" in api
    assert DEFAULT_UNIVERSE_CADENCE_SECONDS == 1800
    assert "request_universe_run_now" not in (REPO_ROOT / "backend/src/sports_hedge/api/paper.py").read_text(
        encoding="utf-8"
    ).split("def put_operator_scanner_settings", 1)[1].split("def stop_paper_scanner", 1)[0]
