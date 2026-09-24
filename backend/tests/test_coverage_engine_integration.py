"""Engine-level coverage scheduling. Synthetic fixtures only. No live venues."""

from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta
from decimal import Decimal

import pytest

from sports_hedge.application.active_trade_lane import DEFAULT_ACTIVE_TRADE_CADENCE_SECONDS
from sports_hedge.application.background_exact_id_planner import run_background_exact_id_slice
from sports_hedge.application.hot_latency_exact_id import run_hot_latency_exact_id_slice
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.price_engine import PriceEnginePriority
from sports_hedge.application.provider_access import (
    DEFAULT_PROVIDER_CONCURRENCY,
    ProviderAccessLayer,
    ProviderPriority,
)
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.approved_register import CANONICAL_BTTS_FT
from sports_hedge.persistence.operator_scanner_settings import SqliteOperatorScannerSettingsStore
from test_issue344_price_engine import (
    DISTANT_KICKOFF,
    NEAR_KICKOFF,
    NOW,
    _engine,
    _row,
)
from test_dual_cadence_scheduler import FakeClock


def _catalogue(count: int, *, kickoff=DISTANT_KICKOFF):
    rows = []
    for index in range(1, count + 1):
        suffix = f"row-{index:03d}"
        rows.append(
            _row(
                suffix=suffix,
                key=CANONICAL_BTTS_FT,
                kickoff=kickoff,
                matchbook_event_id=str(80000 + index),
                matchbook_market_id=str(90000 + index),
                kalshi_event=f"KXEPLBTTS-{index:03d}",
            )
        )
    return rows


def test_retry_rows_stay_in_membership_and_do_not_spin() -> None:
    rows = _catalogue(30)
    engine, _mb, _ks, _layer = _engine(rows, clock=FakeClock(NOW), timeout=8)
    first = engine.due_items(PriceEnginePriority.BACKGROUND, now=NOW)
    assert [item.identity.catalogue_row_id for item in first][:3] == [
        "amc-row-001",
        "amc-row-002",
        "amc-row-003",
    ]
    assert first[-1].identity.catalogue_row_id == "amc-row-024"
    cursor = engine.coverage_cursor(PriceEnginePriority.BACKGROUND)
    assert cursor.catalogue_count == 30
    assert cursor.hold_until is None
    blocked = engine.item("amc-row-025")
    assert blocked is not None
    blocked.next_retry_at = NOW + timedelta(seconds=5)
    second = engine.due_items(PriceEnginePriority.BACKGROUND, now=NOW)
    second_ids = [item.identity.catalogue_row_id for item in second]
    assert "amc-row-025" not in second_ids
    assert second_ids[0] == "amc-row-026"
    assert cursor.catalogue_count == 30
    assert cursor.cursor_after_id != "amc-row-001"
    for runtime in engine.items():
        if runtime.identity.catalogue_row_id not in cursor.visited:
            runtime.next_retry_at = NOW + timedelta(seconds=5)
    assert engine.due_items(PriceEnginePriority.BACKGROUND, now=NOW) == []
    assert cursor.catalogue_count == 30
    nxt = engine.next_runnable_at(PriceEnginePriority.BACKGROUND, now=NOW)
    assert nxt == NOW + timedelta(seconds=5)
    assert (nxt - NOW).total_seconds() > 0.2
    released = engine.due_items(
        PriceEnginePriority.BACKGROUND, now=NOW + timedelta(seconds=5)
    )
    assert released[0].identity.catalogue_row_id == "amc-row-025"


@pytest.mark.asyncio
async def test_large_catalogue_fairness_is_synthetic(tmp_path) -> None:
    """Deterministic fixture timings. Not a live performance measurement."""

    handed: list[str] = []
    rows = _catalogue(300)

    def on_decision(_decision, runtime) -> None:
        cursor = engine.coverage_cursor(PriceEnginePriority.BACKGROUND)
        assert cursor.hold_until is None
        handed.append(runtime.identity.catalogue_row_id)

    engine, matchbook, _kalshi, layer = _engine(
        rows,
        clock=FakeClock(NOW),
        timeout=8,
        on_item_decision=on_decision,
    )
    assert engine._provider_timeout == 8
    assert layer.limits[VenueName.MATCHBOOK] == 4
    assert layer.limits[VenueName.KALSHI] == 4
    assert layer.limits[VenueName.POLYMARKET] == 8
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.MATCHBOOK] == 4
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.KALSHI] == 4
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.POLYMARKET] == 8
    assert Settings.model_fields["paper_scan_provider_timeout_seconds"].default == 8
    assert Settings().sports_hedge_mode == "paper"
    assert DEFAULT_ACTIVE_TRADE_CADENCE_SECONDS == 5
    assert ProviderPriority.ACTIVE_TRADE < ProviderPriority.HOT < ProviderPriority.BACKGROUND
    assert "list_events" not in inspect.getsource(run_background_exact_id_slice)
    assert "list_markets" not in inspect.getsource(run_background_exact_id_slice)
    assert "list_events" not in inspect.getsource(run_hot_latency_exact_id_slice)

    await engine.run_slice(PriceEnginePriority.BACKGROUND)
    cursor = engine.coverage_cursor(PriceEnginePriority.BACKGROUND)
    assert cursor.cursor_after_id == "amc-row-024"
    assert cursor.catalogue_count == 300
    assert handed
    assert cursor.hold_until is None
    assert matchbook.list_events_calls == 0
    assert matchbook.list_markets_calls == []

    hot = _row(
        suffix="hot-1",
        key=CANONICAL_BTTS_FT,
        kickoff=NEAR_KICKOFF,
        matchbook_event_id="70001",
        matchbook_market_id="71001",
        kalshi_event="KXEPLBTTS-HOT",
    )
    engine.catalogue_store.upsert_catalogue_row(hot)
    engine.reconstruct()
    hot_due = engine.due_items(PriceEnginePriority.HOT, now=NOW)
    hot_ids = [item.identity.catalogue_row_id for item in hot_due]
    assert "amc-hot-1" in hot_ids
    stalled = engine.item("amc-row-026")
    assert stalled is not None
    stalled.next_retry_at = NOW + timedelta(seconds=4)
    resumed = engine.due_items(PriceEnginePriority.BACKGROUND, now=NOW)
    resumed_ids = [item.identity.catalogue_row_id for item in resumed]
    assert resumed_ids[0] == "amc-row-025"
    assert "amc-row-001" not in resumed_ids
    assert "amc-row-026" not in resumed_ids
    assert "amc-row-027" in resumed_ids
    background_membership = [
        item.identity.catalogue_row_id
        for item in engine.items()
        if item.priority is PriceEnginePriority.BACKGROUND
    ]
    assert "amc-row-026" in background_membership
    assert (
        engine.coverage_cursor(PriceEnginePriority.BACKGROUND).catalogue_count
        == len(background_membership)
    )

    for runtime in engine.items():
        row_id = runtime.identity.catalogue_row_id
        if row_id not in engine.coverage_cursor(PriceEnginePriority.BACKGROUND).visited:
            if runtime.priority is PriceEnginePriority.BACKGROUND:
                runtime.next_retry_at = NOW + timedelta(seconds=4)
    assert engine.due_items(PriceEnginePriority.BACKGROUND, now=NOW) == []
    waiting = engine.next_runnable_at(PriceEnginePriority.BACKGROUND, now=NOW)
    assert waiting == NOW + timedelta(seconds=4)

    clock = FakeClock(NOW)
    store = SqliteOperatorScannerSettingsStore(tmp_path / "ops.sqlite")
    coordinator = LiveRefreshCoordinator(clock=clock, operator_settings_store=store)
    coordinator.configure_from_settings()
    coordinator.bind_price_engine(engine)
    coordinator._clock = clock
    with coordinator._state_lock:
        coordinator._release_background_pass_unlocked()
        coordinator._next_background_due = engine.next_runnable_at(
            PriceEnginePriority.BACKGROUND, now=NOW
        )
    assert coordinator._seconds_until_background() >= 4
    assert coordinator.plan_background_tick(now=NOW).lane == "idle"

    hot_cursor = engine.coverage_cursor(PriceEnginePriority.HOT)
    before = hot_cursor.cursor_after_id
    before_pass = hot_cursor.pass_number
    coordinator.apply_operator_scan_settings(
        min_net_edge=Decimal("0.01"),
        max_execution_risk=60,
        hot_target_refresh_seconds=25,
    )
    assert hot_cursor.cursor_after_id == before
    assert hot_cursor.pass_number == before_pass
    assert hot_cursor.visited
    store.close()

    grants: list[str] = []
    priority_layer = ProviderAccessLayer()

    async def _take(lane: str) -> None:
        async with priority_layer.acquire(VenueName.MATCHBOOK, lane=lane):
            grants.append(lane)

    held = [priority_layer.acquire(VenueName.MATCHBOOK, lane="background") for _ in range(4)]
    for slot in held:
        await slot.__aenter__()
    assert priority_layer.snapshot().inflight[VenueName.MATCHBOOK.value] == 4
    background_wait = asyncio.create_task(_take("background"))
    await asyncio.sleep(0)
    hot_wait = asyncio.create_task(_take("hot"))
    await asyncio.sleep(0)
    active_wait = asyncio.create_task(_take("active_trade"))
    await asyncio.sleep(0)
    await held[0].__aexit__(None, None, None)
    await asyncio.wait({background_wait, hot_wait, active_wait}, timeout=1)
    assert grants == ["active_trade", "hot", "background"]
    for slot in held[1:]:
        await slot.__aexit__(None, None, None)


def test_open_hot_pass_keeps_its_cursor_when_target_changes(tmp_path) -> None:
    rows = _catalogue(80, kickoff=NEAR_KICKOFF)
    engine, _mb, _ks, _layer = _engine(rows, clock=FakeClock(NOW), timeout=8)
    claimed = engine.due_items(PriceEnginePriority.HOT, now=NOW)
    assert len(claimed) == 64
    cursor = engine.coverage_cursor(PriceEnginePriority.HOT)
    assert cursor.hold_until is None
    assert cursor.cursor_after_id == "amc-row-064"
    clock = FakeClock(NOW)
    store = SqliteOperatorScannerSettingsStore(tmp_path / "hot-open.sqlite")
    coordinator = LiveRefreshCoordinator(clock=clock, operator_settings_store=store)
    coordinator.configure_from_settings()
    coordinator.bind_price_engine(engine)
    coordinator._clock = clock
    coordinator._next_hot_due = NOW
    coordinator.apply_operator_scan_settings(
        min_net_edge=Decimal("0.01"),
        max_execution_risk=60,
        hot_target_refresh_seconds=30,
    )
    assert cursor.cursor_after_id == "amc-row-064"
    assert cursor.pass_number == 1
    assert cursor.hold_until is None
    assert coordinator._next_hot_due == NOW
    store.close()
