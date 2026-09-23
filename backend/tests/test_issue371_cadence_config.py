"""Issue #371: independent HOT / BACKGROUND / UNIVERSE cadence authorities.

PAPER / read-only. Deterministic coordinator clocks. Not live quotes.
"""

from __future__ import annotations

import ast
import inspect
import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.application.collector import UNIVERSE_COMPLETENESS_COMPLETE
from sports_hedge.application.live_refresh import LiveRefreshCoordinator, get_live_refresh_coordinator
from sports_hedge.application.price_engine import (
    DEFAULT_BACKGROUND_CADENCE_SECONDS,
    PriceEnginePriority,
)
from sports_hedge.application.provider_access import DEFAULT_PROVIDER_CONCURRENCY
from sports_hedge.application.scan_lanes import ScanLane, WORKER_COMPLETE
from sports_hedge.application.universe_checkpoint import (
    universe_provider_backoff_seconds,
    universe_work_retry_backoff_seconds,
)
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.persistence.operator_scanner_settings import (
    BACKGROUND_CADENCE_MAX_SECONDS,
    BACKGROUND_CADENCE_MIN_SECONDS,
    SqliteOperatorScannerSettingsStore,
    bind_runtime_operator_scanner_settings_store,
)
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient
from test_concurrent_hot_universe_workers import _universe_fixture
from test_dual_cadence_scheduler import NOW, FakeClock, _fixture, _report
from test_issue303_universe_persistent_worker import (
    OWNER_CURSOR,
    _cooldown_seconds,
    _open_owner_live_generation,
)
from test_issue367_operator_scanner_controls import _bind_store, _unbind

REPO_ROOT = Path(__file__).resolve().parents[2]
FORBIDDEN_WRITE_METHODS = ("place_order", "cancel_order", "sign_order")


def test_cadence_authorities_are_independent_and_named_honestly() -> None:
    settings = Settings()
    assert settings.paper_live_refresh_hot_interval_seconds == 30
    assert settings.paper_background_price_interval_seconds == 90
    assert settings.paper_universe_discovery_interval_seconds == 1800
    assert settings.paper_live_refresh_universe_interval_seconds == 180
    assert settings.paper_universe_worker_cooldown_seconds == 8
    assert DEFAULT_BACKGROUND_CADENCE_SECONDS == 90
    assert _cooldown_seconds() == 1800
    coordinator = LiveRefreshCoordinator()
    coordinator.configure_from_settings()
    assert coordinator.status.hot.cadence_seconds == 30
    assert coordinator.status.background.cadence_seconds == 90
    assert coordinator.status.universe.cadence_seconds == 1800


def test_startup_universe_is_immediately_due() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator.reset()
    coordinator._clock = clock
    assert coordinator.universe_due_immediately()
    assert coordinator._next_universe_due == NOW
    assert coordinator._universe_generation_id == 0
    plan = coordinator.plan_universe_tick(now=NOW)
    assert plan.lane == ScanLane.UNIVERSE.value
    assert plan.reason == "universe_sweep"
    assert plan.generation_resume is False


def test_completed_universe_schedules_next_fresh_generation_at_plus_600s() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator._next_hot_due = NOW + timedelta(seconds=10_000)
    coordinator._next_universe_due = NOW
    finished = NOW + timedelta(seconds=4)
    coordinator.record_report(
        _report([_universe_fixture(OWNER_CURSOR)], when=NOW, scan_lane=ScanLane.UNIVERSE.value).model_copy(
            update={
                "completed_at": finished,
                "scan_diagnostics": {"completeness": UNIVERSE_COMPLETENESS_COMPLETE},
            }
        ),
        scan_lane=ScanLane.UNIVERSE,
    )
    assert coordinator._universe_generation_started_at is None
    assert coordinator.status.universe.worker_state == WORKER_COMPLETE
    assert coordinator._next_universe_due == finished + timedelta(seconds=1800)
    assert coordinator.status.universe.cadence_seconds == 1800
    status = coordinator.public_status()
    assert status.universe.next_due_at == finished + timedelta(seconds=1800)
    assert status.universe.cadence_seconds == 1800

    idle = coordinator.plan_universe_tick(now=finished + timedelta(seconds=1799))
    assert idle.lane == "idle"
    assert idle.reason == "universe_cooldown"
    due = coordinator.plan_universe_tick(now=finished + timedelta(seconds=1800))
    assert due.lane == ScanLane.UNIVERSE.value
    assert due.generation_resume is False
    assert due.resume_cursor is None


def test_incomplete_generation_resumes_without_600s_sleep() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    _open_owner_live_generation(coordinator)
    later = NOW + timedelta(seconds=30)
    clock.now = later
    plan = coordinator.plan_universe_tick(now=later)
    assert plan.lane == ScanLane.UNIVERSE.value
    assert plan.generation_resume is True
    assert plan.universe_generation_id == 26
    assert coordinator._seconds_until_universe() == pytest.approx(0.05)
    assert coordinator.status.universe.cadence_seconds == 1800


def test_incomplete_universe_chunk_yields_about_8s_not_zero_or_600() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator.configure_from_settings()
    coordinator._clock = clock
    _open_owner_live_generation(coordinator)
    finished = NOW + timedelta(seconds=4)
    coordinator._pause_universe_generation(finished)
    assert coordinator._universe_budget_paused is True
    assert coordinator._next_universe_due == finished + timedelta(seconds=8)
    assert coordinator._universe_generation_started_at is not None
    clock.now = finished + timedelta(seconds=7)
    waiting = coordinator.plan_universe_tick(now=clock.now)
    assert waiting.lane == "idle"
    assert waiting.reason == "universe_cooldown"
    delay = coordinator._seconds_until_universe()
    assert delay == pytest.approx(1.0)
    assert delay < 1800
    status = coordinator.public_status()
    assert status.universe.cadence_seconds == 1800
    assert status.universe.next_due_at == finished + timedelta(seconds=8)
    clock.now = finished + timedelta(seconds=8)
    due = coordinator.plan_universe_tick(now=clock.now)
    assert due.lane == ScanLane.UNIVERSE.value
    assert due.generation_resume is True
    assert due.universe_generation_id == 26


def test_retry_wait_and_provider_backoff_resume_sooner_than_600s() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator._next_hot_due = NOW + timedelta(seconds=10_000)
    coordinator.record_universe_work_set(["ok-a", "retry-b"])
    coordinator.record_universe_fixture_progress(None, _universe_fixture("ok-a"), [], [])
    coordinator.record_universe_fixture_progress(
        None,
        _universe_fixture("retry-b", evaluation="market_fetch_unavailable"),
        [],
        [],
    )
    coordinator._universe_in_progress = False
    coordinator._charge_successful_universe_work(
        0.0, clock.now, leftover_n=0, completeness=UNIVERSE_COMPLETENESS_COMPLETE
    )
    waiting = coordinator.plan_universe_tick(now=clock.now)
    assert waiting.lane == "idle"
    assert waiting.reason == "universe_retry_wait"
    delay = coordinator._seconds_until_universe()
    assert 1.0 <= delay < 600
    assert delay == pytest.approx(universe_work_retry_backoff_seconds(1))
    clock.advance(delay)
    due = coordinator.plan_universe_tick(now=clock.now)
    assert due.lane == ScanLane.UNIVERSE.value
    assert due.generation_resume is True

    backoff_clock = FakeClock(NOW)
    backoff = LiveRefreshCoordinator(clock=backoff_clock)
    backoff._clock = backoff_clock
    _open_owner_live_generation(backoff)
    backoff._mark_lane_error(
        ScanLane.UNIVERSE,
        NOW,
        NOW + timedelta(seconds=1),
        "kalshi_timeout",
        advance_hot_due=False,
    )
    provider_delay = universe_provider_backoff_seconds(1)
    assert provider_delay < 600
    still_open = backoff.plan_universe_tick(now=NOW + timedelta(seconds=1))
    assert still_open.reason == "universe_provider_backoff"
    backoff_clock.now = NOW + timedelta(seconds=1) + timedelta(seconds=provider_delay)
    resumed = backoff.plan_universe_tick(now=backoff_clock.now)
    assert resumed.lane == ScanLane.UNIVERSE.value
    assert resumed.generation_resume is True


@pytest.mark.asyncio
async def test_background_next_due_is_plus_90s_after_slice() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator.configure_from_settings()
    coordinator._clock = clock
    coordinator._next_background_due = NOW
    await coordinator.run_price_engine_slice(PriceEnginePriority.BACKGROUND)
    assert coordinator._next_background_due == NOW + timedelta(seconds=90)
    assert coordinator.status.background.cadence_seconds == 90
    assert coordinator.status.background.next_due_at == NOW + timedelta(seconds=90)
    waiting = coordinator.plan_background_tick(now=NOW + timedelta(seconds=89))
    assert waiting.lane == "idle"
    due = coordinator.plan_background_tick(now=NOW + timedelta(seconds=90))
    assert due.lane == "background"


def test_hot_cadence_remains_operator_controlled_default_30s(tmp_path) -> None:
    settings = Settings()
    assert settings.paper_live_refresh_interval_seconds == 30
    assert settings.paper_live_refresh_hot_interval_seconds == 30
    coordinator, store = _bind_store(tmp_path)
    try:
        saved = coordinator.apply_operator_scan_settings(
            min_net_edge=Decimal(str(settings.min_net_edge)),
            max_execution_risk=settings.max_execution_risk,
            hot_cadence_seconds=15,
            background_cadence_seconds=90,
        )
        assert saved.hot_cadence_seconds == 15
        assert saved.background_cadence_seconds == 90
        assert coordinator.status.hot.cadence_seconds == 15
        assert coordinator.status.interval_seconds == 15
        assert coordinator.status.background.cadence_seconds == 90
        assert coordinator.status.universe.cadence_seconds == 1800
        restored = coordinator.apply_operator_scan_settings(
            min_net_edge=Decimal(str(settings.min_net_edge)),
            max_execution_risk=settings.max_execution_risk,
            hot_cadence_seconds=30,
            background_cadence_seconds=90,
        )
        assert restored.hot_cadence_seconds == 30
    finally:
        _unbind(coordinator, store)


def test_live_refresh_and_system_load_report_truthful_independent_cadences() -> None:
    get_live_refresh_coordinator().reset()
    client = TestClient(app)
    payload = client.get("/paper/live-refresh").json()
    assert payload["hot"]["cadence_seconds"] == 30
    assert payload["background"]["cadence_seconds"] == 90
    assert payload["universe"]["cadence_seconds"] == 1800
    load = payload["system_load"]
    assert load["hot"]["cadence_seconds"] == 30
    assert load["background"]["cadence_seconds"] == 90
    assert load["universe"]["cadence_seconds"] == 1800
    assert load["hot"]["cadence_seconds"] != load["background"]["cadence_seconds"]
    assert load["background"]["cadence_seconds"] != load["universe"]["cadence_seconds"]


def test_no_provider_concurrency_increase_or_new_polling_loop() -> None:
    settings = Settings()
    assert settings.paper_scan_provider_timeout_seconds == 8
    assert settings.paper_scan_venue_timeout_seconds == 15
    assert settings.paper_scan_matchbook_concurrency == 4
    assert settings.paper_scan_kalshi_concurrency == 4
    assert settings.paper_provider_hot_starvation_grants == 8
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.MATCHBOOK] == 4
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.KALSHI] == 4
    for client in (MatchbookClient, KalshiClient, PolymarketClient):
        for method in FORBIDDEN_WRITE_METHODS:
            assert not hasattr(client, method)

    live_refresh_mod = __import__("sports_hedge.application.live_refresh", fromlist=["live_refresh"])
    live_refresh = inspect.getsource(live_refresh_mod)
    tree = ast.parse(live_refresh)
    loop_funcs = {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef)
        and node.name in {"_hot_loop", "_universe_loop", "_background_loop", "_loop"}
    }
    assert loop_funcs == {"_hot_loop", "_universe_loop", "_background_loop", "_loop"}
    assert 'name="hot-worker"' in live_refresh
    assert 'name="universe-worker"' in live_refresh
    assert 'name="background-price-worker"' in live_refresh

    frontend_bar = (REPO_ROOT / "frontend/components/venue-health-bar.tsx").read_text()
    frontend_load = (REPO_ROOT / "frontend/lib/system-load-display.ts").read_text()
    assert frontend_bar.count("setInterval") == 1
    assert "setInterval" not in frontend_load
    start_ps1 = (REPO_ROOT / "scripts/windows/Start-SportsHedge-Demo.ps1").read_text()
    refresh_ps1 = (REPO_ROOT / "scripts/windows/Refresh-SportsHedge-Demo.ps1").read_text()
    assert "PAPER_UNIVERSE_WORKER_COOLDOWN" not in start_ps1
    assert "PAPER_LIVE_REFRESH_UNIVERSE_INTERVAL" not in start_ps1
    assert "PAPER_BACKGROUND_PRICE_INTERVAL" not in start_ps1
    assert "PAPER_UNIVERSE_DISCOVERY_INTERVAL" not in start_ps1
    assert "$env:PAPER_LIVE_REFRESH_ENABLED = \"true\"" in start_ps1
    assert "PAPER_UNIVERSE_WORKER_COOLDOWN" not in refresh_ps1


def test_family_scoped_completeness_and_stop_resume_contracts_remain() -> None:
    from sports_hedge.application.catalogue_maintenance import (
        persist_universe_catalogue_pass,
    )
    from sports_hedge.persistence.operator_scanner_settings import SCANNER_STOPPED_BY_OPERATOR

    source = inspect.getsource(persist_universe_catalogue_pass)
    assert "family" in source.casefold()
    assert SCANNER_STOPPED_BY_OPERATOR
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False


def test_old_universe_interval_env_does_not_control_background_or_discovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAPER_LIVE_REFRESH_UNIVERSE_INTERVAL_SECONDS", "180")
    monkeypatch.setenv("PAPER_UNIVERSE_WORKER_COOLDOWN_SECONDS", "8")
    settings = Settings()
    assert settings.paper_live_refresh_universe_interval_seconds == 180
    assert settings.paper_universe_worker_cooldown_seconds == 8
    assert settings.paper_background_price_interval_seconds == 90
    assert settings.paper_universe_discovery_interval_seconds == 1800
    coordinator = LiveRefreshCoordinator()
    coordinator.configure_from_settings(settings)
    assert coordinator.status.background.cadence_seconds == 90
    assert coordinator.status.universe.cadence_seconds == 1800


def test_hot_remains_schedulable_during_600s_universe_discovery_gap() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    live = _fixture("live", kickoff=NOW - timedelta(minutes=5), in_running=True)
    coordinator.record_report(_report([live], when=NOW), scan_lane=ScanLane.HOT)
    coordinator._next_hot_due = NOW
    coordinator.record_report(
        _report([_universe_fixture("done-a")], when=NOW, scan_lane=ScanLane.UNIVERSE.value).model_copy(
            update={"completed_at": NOW + timedelta(seconds=1)}
        ),
        scan_lane=ScanLane.UNIVERSE,
    )
    universe_idle = coordinator.plan_universe_tick(now=NOW)
    assert universe_idle.reason == "universe_cooldown"
    hot = coordinator.plan_hot_tick(now=NOW)
    assert hot.lane == ScanLane.HOT.value
    background = coordinator.plan_background_tick(now=NOW)
    assert background.lane in {"background", "idle"}


def test_background_cadence_defaults_to_90s(tmp_path) -> None:
    coordinator, store = _bind_store(tmp_path)
    try:
        resolved = coordinator.effective_scanner_settings()
        assert resolved.background_cadence_seconds == 90
        assert resolved.source == "env_default"
        coordinator.configure_from_settings()
        assert coordinator.status.background.cadence_seconds == 90
        assert coordinator.status.operator_settings is not None
        assert coordinator.status.operator_settings.background_cadence_seconds == 90
        assert coordinator.status.universe.cadence_seconds == 1800
    finally:
        _unbind(coordinator, store)


@pytest.mark.asyncio
async def test_persisted_background_cadence_is_used_by_scheduler_without_restart(
    tmp_path,
) -> None:
    clock = FakeClock(NOW)
    store = SqliteOperatorScannerSettingsStore(tmp_path / "background-cadence.sqlite")
    coordinator = LiveRefreshCoordinator(clock=clock, operator_settings_store=store)
    try:
        coordinator.configure_from_settings()
        coordinator._clock = clock
        coordinator._next_background_due = NOW
        saved = coordinator.apply_operator_scan_settings(
            min_net_edge=Decimal("0.005"),
            max_execution_risk=60,
            hot_cadence_seconds=30,
            background_cadence_seconds=180,
        )
        assert saved.background_cadence_seconds == 180
        assert saved.source == "operator"
        assert coordinator.status.background.cadence_seconds == 180
        assert coordinator._next_background_due == NOW + timedelta(seconds=180)
        waiting = coordinator.plan_background_tick(now=NOW + timedelta(seconds=179))
        assert waiting.lane == "idle"
        due = coordinator.plan_background_tick(now=NOW + timedelta(seconds=180))
        assert due.lane == "background"
        clock.now = NOW + timedelta(seconds=180)
        coordinator._next_background_due = clock.now
        await coordinator.run_price_engine_slice(PriceEnginePriority.BACKGROUND)
        assert coordinator._next_background_due == clock.now + timedelta(seconds=180)
        assert coordinator.status.background.cadence_seconds == 180
        load = coordinator.public_status().system_load
        assert load.background.cadence_seconds == 180
        assert coordinator.status.universe.cadence_seconds == 1800
    finally:
        bind_runtime_operator_scanner_settings_store(None)
        store.close()


def test_background_cadence_survives_store_reopen(tmp_path) -> None:
    db = tmp_path / "restart-background.sqlite"
    first = SqliteOperatorScannerSettingsStore(db)
    first.save_settings(
        min_net_edge=Decimal("0.01"),
        max_execution_risk=40,
        hot_cadence_seconds=20,
        background_cadence_seconds=240,
    )
    first.close()
    restarted = SqliteOperatorScannerSettingsStore(db)
    loaded = restarted.load()
    assert loaded is not None
    assert loaded.hot_cadence_seconds == 20
    assert loaded.background_cadence_seconds == 240
    assert loaded.source == "operator"
    coordinator = LiveRefreshCoordinator(operator_settings_store=restarted)
    coordinator.configure_from_settings()
    assert coordinator.status.background.cadence_seconds == 240
    assert coordinator.status.hot.cadence_seconds == 20
    assert coordinator.status.universe.cadence_seconds == 1800
    restarted.close()


def test_background_cadence_invalid_range_rejected(tmp_path) -> None:
    coordinator, store = _bind_store(tmp_path)
    client = TestClient(app)
    try:
        for value in (59, 601, 0, 30, BACKGROUND_CADENCE_MIN_SECONDS - 1, BACKGROUND_CADENCE_MAX_SECONDS + 1):
            response = client.put(
                "/paper/operator-scanner-settings",
                json={
                    "min_net_edge": "0.005",
                    "max_execution_risk": 60,
                    "hot_cadence_seconds": 30,
                    "background_cadence_seconds": value,
                },
            )
            assert response.status_code == 422, value
        missing = client.put(
            "/paper/operator-scanner-settings",
            json={
                "min_net_edge": "0.005",
                "max_execution_risk": 60,
                "hot_cadence_seconds": 30,
            },
        )
        assert missing.status_code == 422
        ok = client.put(
            "/paper/operator-scanner-settings",
            json={
                "min_net_edge": "0.005",
                "max_execution_risk": 60,
                "hot_cadence_seconds": 30,
                "background_cadence_seconds": 90,
            },
        )
        assert ok.status_code == 200
        assert ok.json()["operator_settings"]["background_cadence_seconds"] == 90
        bounds = client.put(
            "/paper/operator-scanner-settings",
            json={
                "min_net_edge": "0.005",
                "max_execution_risk": 60,
                "hot_cadence_seconds": 30,
                "background_cadence_seconds": 600,
            },
        )
        assert bounds.status_code == 200
        assert bounds.json()["background"]["cadence_seconds"] == 600
        low = client.put(
            "/paper/operator-scanner-settings",
            json={
                "min_net_edge": "0.005",
                "max_execution_risk": 60,
                "hot_cadence_seconds": 30,
                "background_cadence_seconds": 60,
            },
        )
        assert low.status_code == 200
        assert low.json()["background"]["cadence_seconds"] == 60
    finally:
        _unbind(coordinator, store)


def test_legacy_singleton_row_migrates_background_cadence_default_90(tmp_path) -> None:
    db = tmp_path / "legacy-operator.sqlite"
    connection = sqlite3.connect(db)
    connection.execute(
        """
        CREATE TABLE operator_scanner_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            min_net_edge TEXT NOT NULL,
            max_execution_risk INTEGER NOT NULL,
            hot_cadence_seconds INTEGER NOT NULL,
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
            scanner_stopped, source, updated_at
        ) VALUES (1, ?, ?, ?, ?, ?, ?)
        """,
        ("0.0125", 33, 15, 0, "operator", datetime.now(UTC).isoformat()),
    )
    connection.commit()
    connection.close()
    store = SqliteOperatorScannerSettingsStore(db)
    loaded = store.load()
    assert loaded is not None
    assert loaded.source == "operator"
    assert loaded.hot_cadence_seconds == 15
    assert loaded.background_cadence_seconds == 90
    columns = {
        str(row[1])
        for row in sqlite3.connect(db).execute("PRAGMA table_info(operator_scanner_settings)")
    }
    assert "background_cadence_seconds" in columns
    store.close()


def test_background_cadence_update_does_not_trigger_scan(tmp_path) -> None:
    coordinator, store = _bind_store(tmp_path)
    client = TestClient(app)
    ticks: list[str] = []

    async def boom(_plan=None) -> None:
        ticks.append("tick")
        raise AssertionError("BACKGROUND cadence Update must not trigger scanner work")

    from sports_hedge.api import paper as paper_api

    original = paper_api.server_owned_refresh_tick
    paper_api.server_owned_refresh_tick = boom  # type: ignore[method-assign]
    try:
        response = client.put(
            "/paper/operator-scanner-settings",
            json={
                "min_net_edge": "0.005",
                "max_execution_risk": 60,
                "hot_cadence_seconds": 30,
                "background_cadence_seconds": 120,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["operator_settings"]["background_cadence_seconds"] == 120
        assert body["background"]["cadence_seconds"] == 120
        assert body["hot"]["cadence_seconds"] == 30
        assert body["universe"]["cadence_seconds"] == 1800
        assert ticks == []
        put_src = inspect.getsource(paper_api.put_operator_scanner_settings)
        assert "collect_and_scan" not in put_src
        assert "run_price_engine_slice" not in put_src
        assert "server_owned_refresh_tick" not in put_src
    finally:
        paper_api.server_owned_refresh_tick = original
        _unbind(coordinator, store)


def test_frontend_exposes_background_cadence_beside_hot() -> None:
    scan = (REPO_ROOT / "frontend" / "components" / "run-paper-scan.tsx").read_text(
        encoding="utf-8"
    )
    api = (REPO_ROOT / "frontend" / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "BACKGROUND cadence s" in scan
    assert "HOT cadence s" in scan
    assert scan.index("HOT cadence s") < scan.index("BACKGROUND cadence s")
    assert "background_cadence_seconds: backgroundCadence" in scan
    assert "clampBackgroundCadenceSeconds" in scan
    assert "Math.min(600, Math.max(60" in scan
    assert "does not trigger a scan" in scan
    assert "HOT cadence, BACKGROUND cadence and UNIVERSE cadence" in scan
    assert "background_cadence_seconds: number" in api
    assert DEFAULT_BACKGROUND_CADENCE_SECONDS == 90
