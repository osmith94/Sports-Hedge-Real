"""Startup HOT/BACKGROUND pricing barrier until terminal-complete UNIVERSE.

PAPER / read-only. Deterministic coordinator clocks and fake ticks. Not live quotes.
This is a process-start sequencing gate, not a runtime scan lock.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta
from pathlib import Path

import pytest

from sports_hedge.application.collector import CollectionReport
from sports_hedge.application.event_loop_activity import close_loop_slice, mark_loop_phase
from sports_hedge.application.live_refresh import ExplicitCollectBusy, LiveRefreshCoordinator
from sports_hedge.application.scan_lanes import (
    STARTUP_PRICING_GATED_SLEEP_SECONDS,
    STARTUP_UNIVERSE_PENDING,
    ScanLane,
)
from sports_hedge.application.universe_checkpoint import SWEEP_RETRY_WAIT
from sports_hedge.config import get_settings
from sports_hedge.persistence.operator_scanner_settings import (
    SqliteOperatorScannerSettingsStore,
    bind_runtime_operator_scanner_settings_store,
)
from test_concurrent_hot_universe_workers import _universe_fixture
from test_dual_cadence_scheduler import NOW, FakeClock, _fixture, _report


@pytest.fixture(autouse=True)
def _unbind_operator_settings_store() -> None:
    yield
    bind_runtime_operator_scanner_settings_store(None)


def _coordinator(clock: FakeClock | None = None) -> LiveRefreshCoordinator:
    resolved = clock or FakeClock(NOW)
    store = SqliteOperatorScannerSettingsStore(":memory:")
    coordinator = LiveRefreshCoordinator(clock=resolved, operator_settings_store=store)
    coordinator._clock = resolved
    coordinator.configure_from_settings()
    coordinator.arm_startup_pricing_barrier()
    return coordinator


def _commit_startup_universe(coordinator: LiveRefreshCoordinator, when=NOW) -> None:
    if coordinator._universe_generation_started_at is None:
        coordinator._universe_generation_id = max(1, int(coordinator._universe_generation_id))
        coordinator._universe_generation_started_at = when
    coordinator.record_universe_work_set(["startup-complete"])
    coordinator.record_universe_fixture_progress(
        None, _universe_fixture("startup-complete"), [], []
    )
    assert coordinator._universe_sweep_is_complete_unlocked() is True
    coordinator._charge_successful_universe_work(
        0.0, when, leftover_n=0, completeness=None
    )
    assert coordinator._universe_generation_started_at is None
    assert coordinator.startup_pricing_ready() is True


def test_startup_universe_is_immediately_runnable_while_pricing_is_gated() -> None:
    coordinator = _coordinator()
    assert coordinator.universe_due_immediately() is True
    universe = coordinator.plan_universe_tick(now=NOW)
    assert universe.lane == ScanLane.UNIVERSE.value
    hot = coordinator.plan_hot_tick(now=NOW)
    assert hot.lane == "idle"
    assert hot.reason == STARTUP_UNIVERSE_PENDING
    background = coordinator.plan_background_tick(now=NOW)
    assert background.lane == "idle"
    assert background.reason == STARTUP_UNIVERSE_PENDING
    active = coordinator.plan_active_trade_tick(now=NOW)
    assert active.reason != STARTUP_UNIVERSE_PENDING
    status = coordinator.public_status()
    assert status.startup_pricing_ready is False
    assert status.hot.last_plan_reason == STARTUP_UNIVERSE_PENDING
    assert status.background.last_plan_reason == STARTUP_UNIVERSE_PENDING
    assert status.hot.cycle_in_progress is False
    assert status.background.cycle_in_progress is False
    assert "waiting for startup universe" in (status.hot.operator_summary or "")
    assert "waiting for startup universe" in (status.background.operator_summary or "")


def test_incomplete_chunk_retry_and_provider_failure_do_not_open_barrier() -> None:
    clock = FakeClock(NOW)
    coordinator = _coordinator(clock)
    coordinator._universe_generation_id = 1
    coordinator._universe_generation_started_at = NOW
    coordinator.record_universe_work_set(["fix-a", "fix-b"])
    coordinator.record_universe_fixture_progress(None, _universe_fixture("fix-a"), [], [])
    assert coordinator._universe_sweep_is_complete_unlocked() is False
    coordinator._charge_successful_universe_work(
        1.0, clock.now, leftover_n=1, completeness="partial"
    )
    assert coordinator.startup_pricing_ready() is False
    assert coordinator.plan_hot_tick(now=clock.now).reason == STARTUP_UNIVERSE_PENDING

    coordinator._pause_universe_generation(clock.now)
    assert coordinator.startup_pricing_ready() is False

    coordinator.record_universe_fixture_progress(
        None,
        _universe_fixture("fix-b", evaluation="market_fetch_unavailable"),
        [],
        [],
    )
    assert coordinator._universe_work["fix-b"].state == SWEEP_RETRY_WAIT
    assert coordinator._universe_sweep_is_complete_unlocked() is False
    coordinator._universe_in_progress = False
    retry = coordinator.plan_universe_tick(now=clock.now)
    assert retry.reason == "universe_retry_wait"
    assert coordinator.startup_pricing_ready() is False
    assert coordinator.plan_background_tick(now=clock.now).reason == STARTUP_UNIVERSE_PENDING

    coordinator._universe_work["fix-b"].next_retry_at = None
    coordinator._universe_retry_at = clock.now + timedelta(seconds=8)
    failed = coordinator.plan_universe_tick(now=clock.now)
    assert failed.reason == "universe_provider_backoff"
    coordinator._mark_lane_error(
        ScanLane.UNIVERSE, NOW, clock.now, "matchbook discovery timeout"
    )
    assert coordinator.startup_pricing_ready() is False
    assert coordinator.plan_hot_tick(now=clock.now).reason == STARTUP_UNIVERSE_PENDING


def test_terminal_complete_committed_generation_opens_barrier_once() -> None:
    clock = FakeClock(NOW)
    coordinator = _coordinator(clock)
    live = _fixture("live", kickoff=NOW - timedelta(minutes=5), in_running=True)
    coordinator.record_report(_report([live], when=NOW), scan_lane=ScanLane.HOT)
    coordinator._next_hot_due = NOW
    coordinator._next_background_due = NOW
    _commit_startup_universe(coordinator, NOW)
    hot = coordinator.plan_hot_tick(now=NOW)
    assert hot.lane == ScanLane.HOT.value
    background = coordinator.plan_background_tick(now=NOW)
    assert background.lane == "background"

    coordinator._universe_generation_id = 2
    coordinator._universe_generation_started_at = NOW + timedelta(seconds=3600)
    concurrent = coordinator.plan_universe_tick(now=NOW + timedelta(seconds=3600))
    assert concurrent.lane == ScanLane.UNIVERSE.value
    still_hot = coordinator.plan_hot_tick(now=NOW + timedelta(seconds=3600))
    assert still_hot.reason != STARTUP_UNIVERSE_PENDING
    still_background = coordinator.plan_background_tick(now=NOW + timedelta(seconds=3600))
    assert still_background.reason != STARTUP_UNIVERSE_PENDING
    assert coordinator.startup_pricing_ready() is True


def test_universe_scans_paused_still_runs_startup_oneshot_then_stays_paused(
    tmp_path: Path,
) -> None:
    clock = FakeClock(NOW)
    store = SqliteOperatorScannerSettingsStore(tmp_path / "paused-startup.sqlite")
    store.save_universe_scans_paused(True)
    coordinator = LiveRefreshCoordinator(clock=clock, operator_settings_store=store)
    coordinator.configure_from_settings()
    coordinator.arm_startup_pricing_barrier()
    coordinator._clock = clock
    assert coordinator.plan_universe_tick(now=NOW).lane == ScanLane.UNIVERSE.value
    assert coordinator.plan_hot_tick(now=NOW).reason == STARTUP_UNIVERSE_PENDING
    _commit_startup_universe(coordinator, NOW)
    assert coordinator.startup_pricing_ready() is True
    later = coordinator.plan_universe_tick(now=NOW + timedelta(hours=2))
    assert later.lane == "idle"
    assert later.reason == "universe_scheduled_paused"
    bind_runtime_operator_scanner_settings_store(None)
    store.close()


@pytest.mark.asyncio
async def test_manual_hot_and_background_cannot_bypass_closed_barrier() -> None:
    coordinator = _coordinator()
    calls = {"n": 0}

    async def runner() -> CollectionReport:
        calls["n"] += 1
        return CollectionReport(started_at=NOW, completed_at=NOW)

    with pytest.raises(ExplicitCollectBusy, match="startup universe pending"):
        await coordinator.run_manual_hot(runner)
    with pytest.raises(ExplicitCollectBusy, match="startup universe pending"):
        await coordinator.run_manual_background()
    assert calls["n"] == 0
    plan = coordinator.manual_hot_plan()
    assert plan.lane == "idle"
    assert plan.reason == STARTUP_UNIVERSE_PENDING


def test_clear_and_update_closes_pricing_until_replacement_commits() -> None:
    coordinator = _coordinator()
    _commit_startup_universe(coordinator, NOW)
    assert coordinator.plan_hot_tick(now=NOW).reason != STARTUP_UNIVERSE_PENDING
    coordinator.clear_universe_working_set(run_after=True)
    assert coordinator.startup_pricing_ready() is False
    assert coordinator.plan_hot_tick(now=NOW).reason == STARTUP_UNIVERSE_PENDING
    assert coordinator.plan_background_tick(now=NOW).reason == STARTUP_UNIVERSE_PENDING
    assert coordinator.plan_universe_tick(now=NOW).lane == ScanLane.UNIVERSE.value
    _commit_startup_universe(coordinator, NOW)
    assert coordinator.startup_pricing_ready() is True
    assert coordinator.plan_background_tick(now=NOW).lane == "background"


def test_gated_hot_background_do_not_busy_spin() -> None:
    coordinator = _coordinator()
    delay_hot = coordinator._seconds_until_hot()
    delay_background = coordinator._seconds_until_background()
    assert delay_hot == pytest.approx(STARTUP_PRICING_GATED_SLEEP_SECONDS)
    assert delay_background == pytest.approx(STARTUP_PRICING_GATED_SLEEP_SECONDS)
    assert delay_hot >= 0.25
    hot_loop = inspect.getsource(LiveRefreshCoordinator._hot_loop)
    background_loop = inspect.getsource(LiveRefreshCoordinator._background_loop)
    assert "close_loop_slice" in hot_loop
    assert "close_loop_slice" in background_loop
    assert "mark_loop_phase" in hot_loop
    sleep_src = inspect.getsource(LiveRefreshCoordinator._sleep_interruptible)
    assert "wait_for" in sleep_src
    assert "0.25" in inspect.getsource(LiveRefreshCoordinator._seconds_until_hot)


@pytest.mark.asyncio
async def test_server_loop_gates_pricing_ticks_until_universe_commits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("PAPER_LIVE_REFRESH_ENABLED", "true")
    get_settings.cache_clear()
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    pricing_ticks: list[str] = []
    discovery_ticks: list[str] = []
    active_ticks: list[str] = []

    async def tick(plan=None) -> None:
        lane = getattr(plan, "lane", None)
        if lane == ScanLane.UNIVERSE.value:
            discovery_ticks.append(lane)
            return
        if lane == ScanLane.HOT.value or lane == "background":
            pricing_ticks.append(str(lane))
            return
        if lane == "active_trade":
            active_ticks.append(str(lane))

    try:
        await coordinator.start_server_loop(tick)
        await asyncio.sleep(0.2)
        assert discovery_ticks
        assert pricing_ticks == []
        assert coordinator.plan_hot_tick(now=clock.now).reason == STARTUP_UNIVERSE_PENDING
        live = _fixture("live", kickoff=NOW - timedelta(minutes=5), in_running=True)
        coordinator.record_report(_report([live], when=NOW), scan_lane=ScanLane.HOT)
        coordinator._next_hot_due = clock.now
        coordinator._next_background_due = clock.now
        _commit_startup_universe(coordinator, clock.now)
        coordinator._pulse_control()
        await asyncio.sleep(0.25)
        assert "hot" in pricing_ticks or "background" in pricing_ticks
        assert coordinator.plan_hot_tick(now=clock.now).reason != STARTUP_UNIVERSE_PENDING
    finally:
        await coordinator.stop_server_loop()
        get_settings.cache_clear()


def test_existing_event_loop_liveness_hooks_remain() -> None:
    hot_src = inspect.getsource(LiveRefreshCoordinator._hot_loop)
    universe_src = inspect.getsource(LiveRefreshCoordinator._universe_loop)
    background_src = inspect.getsource(LiveRefreshCoordinator._background_loop)
    active_src = inspect.getsource(LiveRefreshCoordinator._active_trade_loop)
    for src in (hot_src, universe_src, background_src, active_src):
        assert "mark_loop_phase" in src
        assert "close_loop_slice" in src
    assert close_loop_slice is not None
    assert mark_loop_phase is not None


def test_runtime_hot_during_later_universe_is_unchanged_without_configure() -> None:
    """Tenet 19 overlap remains when this is not a configured process start."""

    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    live = _fixture("live", kickoff=NOW - timedelta(minutes=5), in_running=True)
    coordinator.record_report(_report([live], when=NOW), scan_lane=ScanLane.HOT)
    coordinator._next_hot_due = NOW
    coordinator._universe_generation_id = 2
    coordinator._universe_generation_started_at = NOW
    hot = coordinator.plan_hot_tick(now=NOW)
    assert hot.lane == ScanLane.HOT.value
    universe = coordinator.plan_universe_tick(now=NOW)
    assert universe.lane == ScanLane.UNIVERSE.value
