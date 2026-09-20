"""Issue #371: independent BACKGROUND 90s / UNIVERSE 10m cadences and canonical lane names.

PAPER / read-only. Deterministic coordinator clocks. Not live quotes.
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from pathlib import Path

import pytest

from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.price_engine import (
    DEFAULT_BACKGROUND_CADENCE_SECONDS,
    DEFAULT_HOT_CADENCE_SECONDS,
    PriceEnginePriority,
)
from sports_hedge.application.provider_access import DEFAULT_PROVIDER_CONCURRENCY
from sports_hedge.application.scan_lanes import (
    DEFAULT_BACKGROUND_INTERVAL_SECONDS,
    DEFAULT_HOT_INTERVAL_SECONDS,
    DEFAULT_UNIVERSE_DISCOVERY_INTERVAL_SECONDS,
    OPERATOR_BACKGROUND_PRICING_LABEL,
    OPERATOR_HOT_PRICING_LABEL,
    OPERATOR_UNIVERSE_DISCOVERY_LABEL,
    ScanLane,
)
from sports_hedge.application.system_load import (
    SYSTEM_LOAD_JSON_BUDGET_BYTES,
    system_load_from_status,
    system_load_payload_bytes,
)
from sports_hedge.application.universe_checkpoint import universe_work_retry_backoff_seconds
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.models import VenueName
from test_dual_cadence_scheduler import NOW, FakeClock

REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "frontend"


def test_cadence_settings_are_independent_and_named_honestly() -> None:
    settings = Settings()
    assert settings.paper_live_refresh_hot_interval_seconds == 30
    assert settings.paper_background_price_interval_seconds == 90
    assert settings.paper_universe_discovery_interval_seconds == 600
    assert settings.paper_universe_worker_cooldown_seconds == 8
    assert settings.paper_live_refresh_universe_interval_seconds == 180
    assert DEFAULT_HOT_CADENCE_SECONDS == DEFAULT_HOT_INTERVAL_SECONDS == 30
    assert DEFAULT_BACKGROUND_CADENCE_SECONDS == DEFAULT_BACKGROUND_INTERVAL_SECONDS == 90
    assert DEFAULT_UNIVERSE_DISCOVERY_INTERVAL_SECONDS == 600
    assert settings.paper_scan_hot_cycle_timeout_seconds == 25
    assert settings.paper_scan_universe_generation_budget_seconds == 150
    assert settings.paper_scan_provider_timeout_seconds == 8
    assert settings.paper_scan_kalshi_concurrency == 4
    assert settings.paper_scan_matchbook_concurrency == 4
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.KALSHI] == 4
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.MATCHBOOK] == 4


def test_startup_universe_is_immediately_due() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator.configure_from_settings()
    assert coordinator.universe_due_immediately() is True
    assert coordinator.status.universe.cadence_seconds == 600
    plan = coordinator.plan_universe_tick(now=NOW)
    assert plan.lane == ScanLane.UNIVERSE.value
    assert plan.universe_generation_id == 1
    assert plan.generation_resume is False


def test_completed_universe_schedules_next_fresh_generation_at_600s() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator._universe_generation_id = 3
    coordinator._universe_generation_started_at = NOW
    coordinator._close_universe_generation(NOW + timedelta(seconds=12))
    assert coordinator._universe_generation_started_at is None
    assert coordinator._next_universe_due == NOW + timedelta(seconds=612)
    idle = coordinator.plan_universe_tick(now=NOW + timedelta(seconds=13))
    assert idle.lane == "idle"
    assert idle.reason == "universe_cooldown"
    still_idle = coordinator.plan_universe_tick(now=NOW + timedelta(seconds=611))
    assert still_idle.reason == "universe_cooldown"
    nxt = coordinator.plan_universe_tick(now=NOW + timedelta(seconds=612))
    assert nxt.lane == ScanLane.UNIVERSE.value
    assert nxt.generation_resume is False
    assert nxt.universe_generation_id == 4


def test_incomplete_generation_resumes_without_600s_sleep() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator._universe_generation_id = 2
    coordinator._universe_generation_started_at = NOW
    coordinator._next_universe_due = NOW + timedelta(seconds=600)
    plan = coordinator.plan_universe_tick(now=NOW + timedelta(seconds=1))
    assert plan.lane == ScanLane.UNIVERSE.value
    assert plan.generation_resume is True or plan.reason == "universe_sweep"
    assert coordinator._seconds_until_universe() == pytest.approx(0.05)


def test_incomplete_universe_chunk_yields_about_8s_not_zero_or_600() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator.configure_from_settings()
    coordinator._clock = clock
    coordinator._universe_generation_id = 2
    coordinator._universe_generation_started_at = NOW
    coordinator._pause_universe_generation(NOW + timedelta(seconds=1))
    paused = NOW + timedelta(seconds=1)
    assert coordinator._next_universe_due == paused + timedelta(seconds=8)
    clock.now = paused + timedelta(seconds=7)
    waiting = coordinator.plan_universe_tick(now=clock.now)
    assert waiting.lane == "idle"
    assert waiting.reason == "universe_cooldown"
    delay = coordinator._seconds_until_universe()
    assert delay == pytest.approx(1.0)
    clock.now = paused + timedelta(seconds=8)
    resumed = coordinator.plan_universe_tick(now=clock.now)
    assert resumed.lane == ScanLane.UNIVERSE.value
    assert coordinator.status.universe.cadence_seconds == 600


def test_retry_wait_can_resume_sooner_than_discovery_interval() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator._universe_generation_id = 2
    coordinator._universe_generation_started_at = NOW
    coordinator._universe_retry_at = NOW + timedelta(seconds=10)
    waiting = coordinator.plan_universe_tick(now=NOW)
    assert waiting.lane == "idle"
    assert waiting.reason == "universe_provider_backoff"
    delay = coordinator._seconds_until_universe()
    assert 9.0 <= delay <= 10.0
    assert delay < get_settings().paper_universe_discovery_interval_seconds
    assert universe_work_retry_backoff_seconds(1) == 2.0
    resumed = coordinator.plan_universe_tick(now=NOW + timedelta(seconds=10))
    assert resumed.lane == ScanLane.UNIVERSE.value


@pytest.mark.asyncio
async def test_background_next_due_is_90s_after_slice_and_independent_of_universe() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator.configure_from_settings()
    await coordinator.run_price_engine_slice(PriceEnginePriority.BACKGROUND)
    assert coordinator.status.background.cadence_seconds == 90
    assert coordinator._next_background_due == NOW + timedelta(seconds=90)
    assert coordinator.status.background.next_due_at == coordinator._next_background_due
    summary = coordinator.status.background.operator_summary or ""
    assert OPERATOR_BACKGROUND_PRICING_LABEL in summary
    assert coordinator.status.universe.cadence_seconds == 600
    assert coordinator.status.hot.cadence_seconds == 30


def test_hot_cadence_remains_operator_controlled_default_30() -> None:
    settings = Settings()
    assert settings.paper_live_refresh_hot_interval_seconds == 30
    field = Settings.model_json_schema()["properties"]["paper_live_refresh_hot_interval_seconds"]
    assert field.get("minimum") == 15
    assert field.get("maximum") == 60
    coordinator = LiveRefreshCoordinator()
    coordinator.configure_from_settings()
    assert coordinator.status.hot.cadence_seconds == 30
    assert coordinator.status.interval_seconds == 30


def test_system_load_and_status_expose_truthful_independent_cadences() -> None:
    coordinator = LiveRefreshCoordinator()
    coordinator.configure_from_settings()
    status = coordinator.public_status()
    assert status.hot.cadence_seconds == 30
    assert status.background.cadence_seconds == 90
    assert status.universe.cadence_seconds == 600
    load = system_load_from_status(status)
    assert load.hot.cadence_seconds == 30
    assert load.background.cadence_seconds == 90
    assert load.universe.cadence_seconds == 600
    assert system_load_payload_bytes(load) < SYSTEM_LOAD_JSON_BUDGET_BYTES
    combined = status.operator_summary or ""
    assert OPERATOR_HOT_PRICING_LABEL in combined
    assert OPERATOR_BACKGROUND_PRICING_LABEL in combined
    assert OPERATOR_UNIVERSE_DISCOVERY_LABEL in combined
    assert "Fast scan" not in combined
    assert "Full sweep" not in combined


def test_no_new_polling_loop_or_provider_concurrency_increase() -> None:
    live_src = inspect.getsource(LiveRefreshCoordinator.start_server_loop)
    assert live_src.count("create_task") == 4
    assert "hot-worker" in live_src
    assert "universe-worker" in live_src
    assert "background-price-worker" in live_src
    assert "active-trade-worker" in live_src
    settings = Settings()
    assert settings.paper_scan_kalshi_concurrency == 4
    assert settings.paper_scan_matchbook_concurrency == 4
    assert settings.paper_scan_provider_timeout_seconds == 8
    assert settings.paper_scan_venue_timeout_seconds == 15


def test_operator_facing_copy_uses_canonical_lane_names() -> None:
    live_refresh = (REPO_ROOT / "backend/src/sports_hedge/application/live_refresh.py").read_text(
        encoding="utf-8"
    )
    scan_lanes = (REPO_ROOT / "backend/src/sports_hedge/application/scan_lanes.py").read_text(
        encoding="utf-8"
    )
    scan_status = (FRONTEND / "lib/scan-status-display.ts").read_text(encoding="utf-8")
    system_load = (FRONTEND / "lib/system-load-display.ts").read_text(encoding="utf-8")
    paper_scan = (FRONTEND / "components/run-paper-scan.tsx").read_text(encoding="utf-8")
    opportunity_monitor = (FRONTEND / "components/opportunity-monitor.tsx").read_text(
        encoding="utf-8"
    )
    for blob in (live_refresh, scan_lanes, scan_status, system_load, paper_scan, opportunity_monitor):
        assert "Fast scan" not in blob
        assert "Full sweep" not in blob
        assert "Fast Scan" not in blob
        assert "Full Sweep" not in blob
    assert OPERATOR_HOT_PRICING_LABEL in scan_lanes
    assert OPERATOR_BACKGROUND_PRICING_LABEL in scan_lanes
    assert OPERATOR_UNIVERSE_DISCOVERY_LABEL in scan_lanes
    assert "OPERATOR_HOT_PRICING_LABEL" in live_refresh
    assert "OPERATOR_BACKGROUND_PRICING_LABEL" in live_refresh
    assert "OPERATOR_UNIVERSE_DISCOVERY_LABEL" in live_refresh
    assert "Manual HOT refresh" in paper_scan
    assert "Run full diagnostic" in paper_scan
    assert "is not UNIVERSE discovery" in paper_scan
    assert '"hot"' in scan_status or "hot" in scan_status
    assert "HOT pricing" in system_load
    assert "BACKGROUND pricing" in system_load
    assert "UNIVERSE discovery" in system_load
    assert 'label: "HOT pricing"' in opportunity_monitor
    assert 'label: "BACKGROUND pricing"' in opportunity_monitor
    assert 'label: "UNIVERSE discovery"' in opportunity_monitor
