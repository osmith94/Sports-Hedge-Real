"""Pause scheduled UNIVERSE scans without disabling one-shot refresh.

PAPER / read-only. Deterministic coordinator clocks. Not live quotes.
Pause is the periodic fresh-generation timer only — not a giant cadence,
not a second worker, and not a HOT/BACKGROUND/ACTIVE TRADE stop.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from sports_hedge.api import paper as paper_api
from sports_hedge.api.main import app
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.price_engine import CataloguePriceEngine
from sports_hedge.application.provider_access import (
    DEFAULT_PROVIDER_CONCURRENCY,
    get_shared_provider_access,
)
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.paper.trades import PaperTradeState
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore
from sports_hedge.persistence.operator_scanner_settings import (
    UNIVERSE_SCHEDULED_PAUSED,
    SqliteOperatorScannerSettingsStore,
    bind_runtime_operator_scanner_settings_store,
)
from sports_hedge.persistence.operator_universe_scope import (
    SqliteOperatorUniverseScopeStore,
    bind_runtime_operator_universe_scope_store,
)
from test_dual_cadence_scheduler import NOW, FakeClock
from test_issue303_universe_persistent_worker import _open_owner_live_generation
from test_issue367_operator_scanner_controls import _bind_store, _unbind
from test_operator_universe_scope import DEFAULT_EIGHT, _catalogue_row

REPO_ROOT = Path(__file__).resolve().parents[2]


def _finish_startup_oneshot(coordinator: LiveRefreshCoordinator, when: datetime) -> None:
    if coordinator._universe_generation_started_at is None:
        coordinator._universe_generation_started_at = when
        coordinator._universe_generation_id = max(1, int(coordinator._universe_generation_id))
    coordinator._close_universe_generation(when)


def test_paused_scheduler_does_not_start_periodic_universe_after_cadence_expiry(
    tmp_path: Path,
) -> None:
    clock = FakeClock(NOW)
    store = SqliteOperatorScannerSettingsStore(tmp_path / "pause-periodic.sqlite")
    coordinator = LiveRefreshCoordinator(clock=clock, operator_settings_store=store)
    coordinator.configure_from_settings()
    coordinator._clock = clock
    _finish_startup_oneshot(coordinator, NOW)
    coordinator.apply_universe_scans_paused(True)
    clock.now = NOW + timedelta(seconds=10_000)
    idle = coordinator.plan_universe_tick(now=clock.now)
    assert idle.lane == "idle"
    assert idle.reason == UNIVERSE_SCHEDULED_PAUSED
    public = coordinator.public_status()
    assert public.universe_scans_paused is True
    assert public.universe.next_due_at is None
    assert public.universe.cadence_seconds == 3600
    bind_runtime_operator_scanner_settings_store(None)
    store.close()


def test_hot_background_and_active_trade_continue_while_universe_paused(
    tmp_path: Path,
) -> None:
    clock = FakeClock(NOW)
    store = SqliteOperatorScannerSettingsStore(tmp_path / "other-lanes.sqlite")
    coordinator = LiveRefreshCoordinator(clock=clock, operator_settings_store=store)
    coordinator.configure_from_settings()
    coordinator._clock = clock
    _finish_startup_oneshot(coordinator, NOW)
    coordinator._next_hot_due = NOW
    coordinator._next_background_due = NOW
    coordinator._next_active_trade_due = NOW
    coordinator.apply_universe_scans_paused(True)
    hot = coordinator.plan_hot_tick(now=NOW)
    assert hot.reason in {"hot_due", "hot_scope_empty"}
    assert hot.reason != UNIVERSE_SCHEDULED_PAUSED
    background = coordinator.plan_background_tick(now=NOW)
    assert background.lane == "background"
    active = coordinator.plan_active_trade_tick(now=NOW)
    assert active.reason != UNIVERSE_SCHEDULED_PAUSED
    assert coordinator.plan_universe_tick(now=NOW + timedelta(hours=2)).reason == (
        UNIVERSE_SCHEDULED_PAUSED
    )
    bind_runtime_operator_scanner_settings_store(None)
    store.close()


def test_manual_universe_now_works_while_paused_then_returns_to_paused(
    tmp_path: Path,
) -> None:
    clock = FakeClock(NOW)
    store = SqliteOperatorScannerSettingsStore(tmp_path / "run-now.sqlite")
    coordinator = LiveRefreshCoordinator(clock=clock, operator_settings_store=store)
    coordinator.configure_from_settings()
    coordinator._clock = clock
    _finish_startup_oneshot(coordinator, NOW)
    coordinator.apply_universe_scans_paused(True)
    assert coordinator.plan_universe_tick(now=NOW).reason == UNIVERSE_SCHEDULED_PAUSED
    state = coordinator.request_universe_run_now()
    assert state in {"due", "running"}
    due = coordinator.plan_universe_tick(now=NOW)
    assert due.lane == ScanLane.UNIVERSE.value
    _finish_startup_oneshot(coordinator, NOW + timedelta(seconds=20))
    idle = coordinator.plan_universe_tick(now=NOW + timedelta(hours=1))
    assert idle.reason == UNIVERSE_SCHEDULED_PAUSED
    assert coordinator.universe_scans_paused is True
    bind_runtime_operator_scanner_settings_store(None)
    store.close()


def test_material_scope_change_coalesces_one_fresh_generation_while_paused(
    tmp_path: Path,
) -> None:
    clock = FakeClock(NOW)
    scope_store = SqliteOperatorUniverseScopeStore(tmp_path / "scope.sqlite")
    settings_store = SqliteOperatorScannerSettingsStore(tmp_path / "settings.sqlite")
    coordinator = LiveRefreshCoordinator(
        clock=clock,
        operator_settings_store=settings_store,
        universe_scope_store=scope_store,
    )
    coordinator.configure_from_settings()
    coordinator._clock = clock
    _finish_startup_oneshot(coordinator, NOW)
    coordinator.apply_universe_scans_paused(True)
    same = coordinator.apply_universe_scope(list(DEFAULT_EIGHT), run_universe_now=False)
    assert same.selected_count == len(DEFAULT_EIGHT)
    assert coordinator.plan_universe_tick(now=NOW).reason == UNIVERSE_SCHEDULED_PAUSED
    changed = coordinator.apply_universe_scope(
        ["premier_league", "championship"],
        run_universe_now=False,
    )
    assert changed.selected_competition_codes == ["premier_league", "championship"]
    due = coordinator.plan_universe_tick(now=NOW)
    assert due.lane == ScanLane.UNIVERSE.value
    assert due.selected_competition_codes == ["premier_league", "championship"]
    _finish_startup_oneshot(coordinator, NOW + timedelta(seconds=12))
    idle = coordinator.plan_universe_tick(now=NOW + timedelta(hours=1))
    assert idle.reason == UNIVERSE_SCHEDULED_PAUSED
    bind_runtime_operator_scanner_settings_store(None)
    bind_runtime_operator_universe_scope_store(None)
    settings_store.close()
    scope_store.close()


def test_restart_hydrates_saved_scope_runs_startup_refresh_then_remains_paused(
    tmp_path: Path,
) -> None:
    db = tmp_path / "restart-pause.sqlite"
    scope_db = tmp_path / "restart-scope.sqlite"
    first_settings = SqliteOperatorScannerSettingsStore(db)
    first_scope = SqliteOperatorUniverseScopeStore(scope_db)
    first_scope.save_scope(["premier_league", "la_liga"], source="operator")
    first = LiveRefreshCoordinator(
        clock=FakeClock(NOW),
        operator_settings_store=first_settings,
        universe_scope_store=first_scope,
    )
    first.configure_from_settings()
    first.apply_universe_scans_paused(True)
    first_settings.close()
    first_scope.close()

    restarted_settings = SqliteOperatorScannerSettingsStore(db)
    restarted_scope = SqliteOperatorUniverseScopeStore(scope_db)
    clock = FakeClock(NOW + timedelta(hours=3))
    restarted = LiveRefreshCoordinator(
        clock=clock,
        operator_settings_store=restarted_settings,
        universe_scope_store=restarted_scope,
    )
    restarted.configure_from_settings()
    restarted._clock = clock
    assert restarted.universe_scans_paused is True
    hydrated = restarted.effective_universe_scope()
    assert hydrated.selected_competition_codes == ["premier_league", "la_liga"]
    startup = restarted.plan_universe_tick(now=clock.now)
    assert startup.lane == ScanLane.UNIVERSE.value
    assert startup.selected_competition_codes == ["premier_league", "la_liga"]
    _finish_startup_oneshot(restarted, clock.now + timedelta(seconds=8))
    after = restarted.plan_universe_tick(now=clock.now + timedelta(hours=2))
    assert after.reason == UNIVERSE_SCHEDULED_PAUSED
    bind_runtime_operator_scanner_settings_store(None)
    bind_runtime_operator_universe_scope_store(None)
    restarted_settings.close()
    restarted_scope.close()


def test_resume_restores_cadence_without_catch_up_burst(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    store = SqliteOperatorScannerSettingsStore(tmp_path / "resume.sqlite")
    coordinator = LiveRefreshCoordinator(clock=clock, operator_settings_store=store)
    coordinator.configure_from_settings()
    coordinator._clock = clock
    _finish_startup_oneshot(coordinator, NOW)
    coordinator.apply_operator_scan_settings(
        min_net_edge=Decimal("0.01"),
        max_execution_risk=60,
        hot_cadence_seconds=30,
        background_cadence_seconds=90,
        universe_cadence_seconds=900,
    )
    coordinator.apply_universe_scans_paused(True)
    clock.now = NOW + timedelta(seconds=50_000)
    coordinator.apply_universe_scans_paused(False)
    assert coordinator.universe_scans_paused is False
    assert coordinator._next_universe_due == clock.now + timedelta(seconds=900)
    waiting = coordinator.plan_universe_tick(now=clock.now)
    assert waiting.reason == "universe_cooldown"
    due = coordinator.plan_universe_tick(now=clock.now + timedelta(seconds=900))
    assert due.lane == ScanLane.UNIVERSE.value
    bind_runtime_operator_scanner_settings_store(None)
    store.close()


def test_pause_resume_toggle_does_not_scan_or_create_workers(tmp_path: Path) -> None:
    coordinator, store = _bind_store(tmp_path)
    client = TestClient(app)
    ticks: list[str] = []

    async def boom(_plan=None) -> None:
        ticks.append("tick")
        raise AssertionError("UNIVERSE pause/resume must not trigger scanner work")

    original = paper_api.server_owned_refresh_tick
    paper_api.server_owned_refresh_tick = boom  # type: ignore[method-assign]
    _finish_startup_oneshot(coordinator, coordinator.now())
    before = get_shared_provider_access().snapshot().as_dict()
    try:
        paused = client.post("/paper/scanner/universe-schedule/pause")
        assert paused.status_code == 200
        body = paused.json()
        assert body["universe_scans_paused"] is True
        assert body["operator_settings"]["universe_scans_paused"] is True
        assert body["universe"]["next_due_at"] is None
        resumed = client.post("/paper/scanner/universe-schedule/resume")
        assert resumed.status_code == 200
        assert resumed.json()["universe_scans_paused"] is False
        assert ticks == []
        after = get_shared_provider_access().snapshot().as_dict()
        assert after["limits"] == before["limits"]
        for venue, cap in DEFAULT_PROVIDER_CONCURRENCY.items():
            assert after["limits"][venue.value] == cap
        pause_src = inspect.getsource(paper_api.pause_universe_schedule)
        resume_src = inspect.getsource(paper_api.resume_universe_schedule)
        apply_src = inspect.getsource(LiveRefreshCoordinator.apply_universe_scans_paused)
        start_src = inspect.getsource(LiveRefreshCoordinator.start_server_loop)
        assert "server_owned_refresh_tick" not in pause_src
        assert "server_owned_refresh_tick" not in resume_src
        assert "create_task" not in apply_src
        assert "start_server_loop" not in apply_src
        assert start_src.count("create_task") == 4
        assert 'name="universe-worker"' in start_src
        assert start_src.count("universe-worker") == 1
    finally:
        paper_api.server_owned_refresh_tick = original
        _unbind(coordinator, store)


def test_open_and_partial_positions_remain_managed_while_universe_paused(
    tmp_path: Path,
    monkeypatch,
) -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    epl = _catalogue_row(suffix="epl", competition="Premier League", event_id="evt-epl")
    ucl = _catalogue_row(suffix="ucl", competition="UEFA Champions League", event_id="evt-ucl")
    store.upsert_catalogue_row(epl)
    store.upsert_catalogue_row(ucl)
    engine = CataloguePriceEngine(catalogue_store=store)
    settings_store = SqliteOperatorScannerSettingsStore(tmp_path / "pos.sqlite")
    coordinator = LiveRefreshCoordinator(
        catalogue_store=store,
        price_engine=engine,
        operator_settings_store=settings_store,
    )
    coordinator.configure_from_settings()
    coordinator.apply_universe_scans_paused(True)
    monkeypatch.setattr(
        coordinator,
        "_open_paper_event_ids",
        lambda: frozenset({"evt-ucl"}),
    )
    coordinator.bind_universe_scope_store(SqliteOperatorUniverseScopeStore(":memory:"))
    coordinator.apply_universe_scope(["premier_league"])
    ids = {item.identity.canonical_event_id for item in engine.items()}
    assert "evt-epl" in ids
    assert "evt-ucl" in ids

    class _PartialTrade:
        state = PaperTradeState.PARTIAL
        canonical_event_id = "evt-ucl"

    class _Ops:
        def list_active_trades(self) -> list[_PartialTrade]:
            return [_PartialTrade()]

    monkeypatch.setattr(
        "sports_hedge.api.paper.get_paper_operations_service",
        lambda: _Ops(),
    )
    assert coordinator._open_paper_event_ids() == frozenset({"evt-ucl"})
    bind_runtime_operator_scanner_settings_store(None)
    settings_store.close()
    store.close()


def test_open_generation_continues_while_scheduled_scans_are_paused(
    tmp_path: Path,
) -> None:
    clock = FakeClock(NOW)
    store = SqliteOperatorScannerSettingsStore(tmp_path / "open-gen.sqlite")
    coordinator = LiveRefreshCoordinator(clock=clock, operator_settings_store=store)
    coordinator.configure_from_settings()
    coordinator._clock = clock
    _open_owner_live_generation(coordinator)
    coordinator.apply_universe_scans_paused(True)
    plan = coordinator.plan_universe_tick(now=NOW)
    assert plan.lane == ScanLane.UNIVERSE.value
    bind_runtime_operator_scanner_settings_store(None)
    store.close()


def test_cadence_remains_editable_while_paused_and_update_does_not_scan(
    tmp_path: Path,
) -> None:
    coordinator, store = _bind_store(tmp_path)
    client = TestClient(app)
    ticks: list[str] = []

    async def boom(_plan=None) -> None:
        ticks.append("tick")
        raise AssertionError("cadence Update while paused must not scan")

    original = paper_api.server_owned_refresh_tick
    paper_api.server_owned_refresh_tick = boom  # type: ignore[method-assign]
    try:
        _finish_startup_oneshot(coordinator, coordinator.now())
        coordinator.apply_universe_scans_paused(True)
        response = client.put(
            "/paper/operator-scanner-settings",
            json={
                "min_net_edge": "0.01",
                "max_execution_risk": 60,
                "hot_cadence_seconds": 30,
                "background_cadence_seconds": 90,
                "universe_cadence_seconds": 1200,
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["universe_scans_paused"] is True
        assert body["operator_settings"]["universe_cadence_seconds"] == 1200
        assert body["universe"]["cadence_seconds"] == 1200
        assert ticks == []
    finally:
        paper_api.server_owned_refresh_tick = original
        _unbind(coordinator, store)


def test_frontend_exposes_paused_control_without_fake_countdown() -> None:
    scan = (REPO_ROOT / "frontend" / "components" / "run-paper-scan.tsx").read_text(
        encoding="utf-8"
    )
    display = (REPO_ROOT / "frontend" / "lib" / "scan-status-display.ts").read_text(
        encoding="utf-8"
    )
    api = (REPO_ROOT / "frontend" / "lib" / "api.ts").read_text(encoding="utf-8")
    paper = (REPO_ROOT / "backend" / "src" / "sports_hedge" / "api" / "paper.py").read_text(
        encoding="utf-8"
    )
    assert "Pause scheduled UNIVERSE" in scan
    assert "Resume scheduled UNIVERSE" in scan
    assert "UNIVERSE SCHEDULE PAUSED" in scan
    assert "pauseUniverseSchedule" in scan
    assert "detail: `Paused${cadence}" in display
    assert "universe_scans_paused" in api
    assert "/scanner/universe-schedule/pause" in paper
    assert "create_task" not in paper.split("def pause_universe_schedule", 1)[1].split(
        "def resume_universe_schedule", 1
    )[0]
