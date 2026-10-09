"""Independent HOT pricing pause. PAPER / read-only. No venue orders."""

from __future__ import annotations

import asyncio
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_hot_protected_capacity import _bind_rows, _engine, _fixture_rows
from test_issue367_operator_scanner_controls import _bind_store, _unbind
from test_universe_scheduled_scans_pause import _finish_startup_oneshot

from sports_hedge.api import paper as paper_api
from sports_hedge.api.main import app
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.price_engine import PriceEnginePriority
from sports_hedge.application.provider_access import DEFAULT_PROVIDER_CONCURRENCY
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.domain.models import VenueName
from sports_hedge.persistence.operator_scanner_settings import (
    SqliteOperatorScannerSettingsStore,
)

NOW = datetime(2026, 9, 19, 12, 0, tzinfo=UTC)


def test_hot_pause_stops_new_slices_and_keeps_cursor(tmp_path: Path) -> None:
    rows = _fixture_rows(fixtures=12, markets=1, near=True)
    engine, _mb, _ks, _layer = _engine(rows, timeout=8, hot_interval=0)
    first = engine.due_items(PriceEnginePriority.HOT, now=NOW)
    cursor = engine.coverage_cursor(PriceEnginePriority.HOT)
    anchor = cursor.cursor_after_id
    visited = set(cursor.visited)
    pass_number = cursor.pass_number
    assert first
    store = SqliteOperatorScannerSettingsStore(tmp_path / "hot-pause.sqlite")
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW, operator_settings_store=store)
    coordinator.configure_from_settings()
    coordinator.bind_price_engine(engine)
    coordinator._next_hot_due = NOW
    saved = coordinator.apply_hot_pricing_paused(True)
    assert saved.hot_pricing_paused is True
    assert saved.background_pricing_paused is False
    assert coordinator.plan_hot_tick(now=NOW).reason == "hot_paused"
    assert coordinator.plan_hot_tick(now=NOW).lane == "idle"
    status = coordinator.public_status()
    assert status.hot_pricing_paused is True
    assert status.hot.last_plan_reason == "hot_paused"
    assert "paused by operator" in (status.hot.operator_summary or "")
    assert status.hot.worker_state == "waiting"
    assert status.hot.degraded is False
    assert cursor.cursor_after_id == anchor
    assert cursor.visited == visited
    assert cursor.pass_number == pass_number
    reloaded = LiveRefreshCoordinator(clock=lambda: NOW, operator_settings_store=store)
    reloaded.configure_from_settings()
    assert reloaded.hot_pricing_paused is True
    store.close()


def test_hot_pause_leaves_universe_background_and_active_trade_due(tmp_path: Path) -> None:
    store = SqliteOperatorScannerSettingsStore(tmp_path / "other-lanes.sqlite")
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW, operator_settings_store=store)
    coordinator.configure_from_settings()
    _finish_startup_oneshot(coordinator, NOW)
    coordinator._next_hot_due = NOW
    coordinator._next_background_due = NOW
    coordinator._next_active_trade_due = NOW
    coordinator._next_universe_due = NOW
    coordinator.apply_hot_pricing_paused(True)
    assert coordinator.plan_hot_tick(now=NOW).reason == "hot_paused"
    background = coordinator.plan_background_tick(now=NOW)
    assert background.reason != "hot_paused"
    assert background.lane == "background"
    active = coordinator.plan_active_trade_tick(now=NOW)
    assert active.reason not in {"hot_paused", "operator_stopped"}
    universe = coordinator.plan_universe_tick(now=NOW)
    assert universe.reason != "hot_paused"
    assert universe.lane == ScanLane.UNIVERSE.value
    coordinator.apply_operator_scanner_stopped(True)
    assert coordinator.plan_hot_tick(now=NOW).reason == "operator_stopped"
    assert coordinator.plan_background_tick(now=NOW).reason == "operator_stopped"
    assert coordinator.plan_universe_tick(now=NOW).reason == "operator_stopped"
    assert coordinator.plan_active_trade_tick(now=NOW).reason == "operator_stopped"
    store.close()


def test_hot_resume_keeps_cursor_without_row_one_reset(tmp_path: Path) -> None:
    rows = _fixture_rows(fixtures=12, markets=1, near=True)
    engine, _mb, _ks, _layer = _engine(rows, timeout=8, hot_interval=0)
    first = engine.due_items(PriceEnginePriority.HOT, now=NOW)
    cursor = engine.coverage_cursor(PriceEnginePriority.HOT)
    anchor = cursor.cursor_after_id
    visited = set(cursor.visited)
    pass_number = cursor.pass_number
    store = SqliteOperatorScannerSettingsStore(tmp_path / "hot-resume.sqlite")
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW, operator_settings_store=store)
    coordinator.configure_from_settings()
    coordinator.bind_price_engine(engine)
    _finish_startup_oneshot(coordinator, NOW)
    coordinator._next_hot_due = NOW
    coordinator.apply_hot_pricing_paused(True)
    assert coordinator.plan_hot_tick(now=NOW).reason == "hot_paused"
    coordinator.apply_hot_pricing_paused(False)
    resumed = coordinator.public_status()
    assert resumed.hot_pricing_paused is False
    assert resumed.hot.last_plan_reason == "waiting"
    assert "paused by operator" not in (resumed.hot.operator_summary or "")
    assert cursor.cursor_after_id == anchor
    assert cursor.visited == visited
    assert cursor.pass_number == pass_number
    coordinator._next_hot_due = NOW
    due = coordinator.plan_hot_tick(now=NOW)
    assert due.reason != "hot_paused"
    assert due.reason in {"hot_due", "hot_scope_empty"}
    assert first
    store.close()


def test_unrelated_settings_save_preserves_hot_pause(tmp_path: Path) -> None:
    store = SqliteOperatorScannerSettingsStore(tmp_path / "preserve.sqlite")
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW, operator_settings_store=store)
    coordinator.configure_from_settings()
    coordinator.apply_hot_pricing_paused(True)
    coordinator.apply_universe_scans_paused(True)
    coordinator.apply_background_pricing_paused(True)
    saved = coordinator.apply_operator_scan_settings(
        min_net_edge=Decimal("0.0125"),
        max_execution_risk=33,
        hot_cadence_seconds=15,
    )
    assert saved.hot_pricing_paused is True
    assert saved.universe_scans_paused is True
    assert saved.background_pricing_paused is True
    coordinator.apply_operator_scanner_stopped(True)
    stopped = store.load()
    assert stopped is not None
    assert stopped.hot_pricing_paused is True
    coordinator.apply_operator_scanner_stopped(False)
    resumed = store.load()
    assert resumed is not None
    assert resumed.hot_pricing_paused is True
    store.close()


def test_legacy_minimal_schema_migrates_hot_pricing_paused(tmp_path: Path) -> None:
    db = tmp_path / "legacy-hot-pause.sqlite"
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
    assert loaded.hot_pricing_paused is False
    columns = {
        str(row[1])
        for row in sqlite3.connect(db).execute("PRAGMA table_info(operator_scanner_settings)")
    }
    assert "hot_pricing_paused" in columns
    paused = store.save_hot_pricing_paused(True)
    assert paused.hot_pricing_paused is True
    assert paused.min_net_edge == Decimal("0.0125")
    store.close()


def test_hot_pause_resume_http_does_not_scan(tmp_path: Path) -> None:
    coordinator, store = _bind_store(tmp_path)
    client = TestClient(app)
    ticks: list[str] = []

    async def boom(_plan=None) -> None:
        ticks.append("tick")
        raise AssertionError("HOT pause/resume must not trigger scanner work")

    original = paper_api.server_owned_refresh_tick
    paper_api.server_owned_refresh_tick = boom  # type: ignore[method-assign]
    try:
        paused = client.post("/paper/scanner/hot-pricing/pause")
        assert paused.status_code == 200
        body = paused.json()
        assert body["hot_pricing_paused"] is True
        assert body["operator_settings"]["hot_pricing_paused"] is True
        assert body["hot"]["last_plan_reason"] == "hot_paused"
        resumed = client.post("/paper/scanner/hot-pricing/resume")
        assert resumed.status_code == 200
        assert resumed.json()["hot_pricing_paused"] is False
        assert ticks == []
        assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.MATCHBOOK] == 4
        assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.KALSHI] == 4
        assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.POLYMARKET] == 8
    finally:
        paper_api.server_owned_refresh_tick = original
        _unbind(coordinator, store)


@pytest.mark.asyncio
async def test_hot_pause_finishes_granted_calls_and_refuses_ungranted(tmp_path: Path) -> None:
    from test_issue344_price_engine import FakeKalshi, FakeMatchbook, _mb_btts

    class _GatedMatchbook(FakeMatchbook):
        def __init__(self) -> None:
            super().__init__()
            self.release = asyncio.Event()
            self.entered = asyncio.Event()

        async def get_market(self, event_id, market_id, **filters):  # type: ignore[no-untyped-def]
            del filters
            self.get_market_calls.append((str(event_id), str(market_id)))
            self.entered.set()
            await self.release.wait()
            return _mb_btts(int(market_id))

    rows = _fixture_rows(fixtures=8, markets=1, near=True)
    matchbook = _GatedMatchbook()
    kalshi = FakeKalshi()
    engine, _mb, _ks, layer = _engine(
        [],
        timeout=8,
        hot_interval=0,
        matchbook=matchbook,
        kalshi=kalshi,
    )
    _bind_rows(engine, rows)
    store = SqliteOperatorScannerSettingsStore(tmp_path / "hot-drain.sqlite")
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW, operator_settings_store=store)
    coordinator.configure_from_settings()
    coordinator.bind_price_engine(engine)
    slice_task = asyncio.create_task(engine.run_slice(PriceEnginePriority.HOT, now=NOW))
    await asyncio.wait_for(matchbook.entered.wait(), timeout=2)
    calls_while_open = list(matchbook.get_market_calls)
    assert calls_while_open
    coordinator.apply_hot_pricing_paused(True)
    assert layer.hot_admission_paused is True
    async with layer.acquire_wait(VenueName.KALSHI, lane="hot", timeout=0.5) as refused:
        assert refused is None
    async with layer.acquire_wait(VenueName.KALSHI, lane="universe", timeout=0.5) as universe:
        assert universe is not None
    async with layer.acquire_wait(
        VenueName.KALSHI, lane="active_trade", timeout=0.5
    ) as active:
        assert active is not None
    async with layer.acquire_wait(
        VenueName.KALSHI, lane="execution_candidate", timeout=0.5
    ) as price2:
        assert price2 is not None
    assert matchbook.get_market_calls == calls_while_open
    matchbook.release.set()
    result = await asyncio.wait_for(slice_task, timeout=3)
    assert matchbook.get_market_calls == calls_while_open
    cursor = engine.coverage_cursor(PriceEnginePriority.HOT)
    assert cursor.pass_number == 1
    coordinator.apply_hot_pricing_paused(False)
    assert layer.hot_admission_paused is False
    del result
    store.close()
