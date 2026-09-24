"""Protected Matchbook/Kalshi headroom and fair HOT fixture coverage.

Synthetic clocks, gates, and fixture catalogue rows. No live venue calls.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta

import pytest
from test_issue344_price_engine import NEAR_KICKOFF, NOW, _engine, _row

from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.price_engine import PriceEnginePriority
from sports_hedge.application.provider_access import (
    DEFAULT_PROVIDER_CONCURRENCY,
    HOT_DEMAND_LOWER_OCCUPANCY_CEILING,
    ProviderAccessLayer,
    get_shared_provider_access,
    reset_shared_provider_access,
)
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName


def _fixture_rows(*, fixtures: int, markets: int, near: bool):
    rows = []
    kickoff = NEAR_KICKOFF if near else NOW + timedelta(days=14)
    for fixture in range(fixtures):
        for market in range(markets):
            suffix = f"f{fixture:02d}-m{market:02d}"
            row = _row(
                suffix=suffix,
                kickoff=kickoff,
                matchbook_event_id=str(5000 + fixture),
                matchbook_market_id=str(70000 + fixture * 20 + market),
                kalshi_event=f"KXPROT{fixture:02d}{market:02d}",
            )
            rows.append(row.model_copy(update={"canonical_event_id": f"fixture-{fixture:02d}"}))
    return rows


def _bind_rows(engine, rows) -> None:
    """Serve an in-memory roster. Several markets share one fixture id."""

    engine.catalogue_store.list_active = lambda: list(rows)
    engine.reconstruct(rows)


def _interleaved_ids(rows) -> list[str]:
    by_fixture: dict[str, list[str]] = {}
    for row in rows:
        by_fixture.setdefault(row.canonical_event_id, []).append(row.catalogue_row_id)
    ordered = []
    depth = 0
    while True:
        added = False
        for fixture_id in sorted(by_fixture):
            ids = by_fixture[fixture_id]
            if depth < len(ids):
                ordered.append(ids[depth])
                added = True
        if not added:
            return ordered
        depth += 1


@pytest.mark.asyncio
async def test_lower_priority_cannot_consume_protected_matchbook_or_kalshi_slots() -> None:
    layer = ProviderAccessLayer(
        {VenueName.MATCHBOOK: 4, VenueName.KALSHI: 4, VenueName.POLYMARKET: 8}
    )
    assert layer.limits[VenueName.MATCHBOOK] == 4
    assert layer.limits[VenueName.KALSHI] == 4
    assert layer.limits[VenueName.POLYMARKET] == 8
    assert layer.hot_provider_demand is False
    assert layer.lower_priority_occupancy_ceiling(VenueName.MATCHBOOK) == 3
    assert layer.lower_priority_occupancy_ceiling(VenueName.KALSHI) == 3
    assert layer.lower_priority_occupancy_ceiling(VenueName.POLYMARKET) is None
    assert Settings.model_fields["paper_scan_provider_timeout_seconds"].default == 8

    release = asyncio.Event()

    async def _hold(venue: VenueName, lane: str) -> None:
        async with layer.acquire(venue, lane=lane):
            await release.wait()

    background = [
        asyncio.create_task(_hold(venue, "background"))
        for venue in (VenueName.MATCHBOOK, VenueName.KALSHI)
        for _ in range(3)
    ]
    for _ in range(20):
        if (
            layer.lower_in_use[VenueName.MATCHBOOK] == 3
            and layer.lower_in_use[VenueName.KALSHI] == 3
        ):
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("idle lower-priority leases did not reach 3 of 4")
    assert layer.snapshot().inflight[VenueName.MATCHBOOK.value] == 3
    assert layer._peak_inflight[VenueName.MATCHBOOK] <= 4

    active_entered = asyncio.Event()

    async def _active(venue: VenueName) -> None:
        async with layer.acquire(venue, lane="active_trade"):
            active_entered.set()
            await release.wait()

    active = asyncio.create_task(_active(VenueName.MATCHBOOK))
    await active_entered.wait()
    assert layer.snapshot().inflight[VenueName.MATCHBOOK.value] == 4
    assert layer.lower_in_use[VenueName.MATCHBOOK] == 3
    release.set()
    await asyncio.wait_for(asyncio.gather(*background, active), timeout=1)
    assert layer.peak_lower_in_use[VenueName.MATCHBOOK] <= 3
    assert layer.peak_lower_in_use[VenueName.KALSHI] <= 3
    assert layer._peak_inflight[VenueName.MATCHBOOK] <= 4
    assert layer._peak_inflight[VenueName.KALSHI] <= 4


@pytest.mark.asyncio
async def test_active_is_granted_before_hot_on_the_next_protected_slot() -> None:
    layer = ProviderAccessLayer({VenueName.MATCHBOOK: 4, VenueName.KALSHI: 4, VenueName.POLYMARKET: 8})
    release = asyncio.Event()
    grants: list[str] = []

    async def _hold(lane: str) -> None:
        async with layer.acquire(VenueName.MATCHBOOK, lane=lane):
            await release.wait()

    async def _take(lane: str) -> None:
        async with layer.acquire(VenueName.MATCHBOOK, lane=lane):
            grants.append(lane)

    held = [
        asyncio.create_task(_hold(lane))
        for lane in ("background", "background", "hot", "hot")
    ]
    for _ in range(20):
        if layer.snapshot().inflight[VenueName.MATCHBOOK.value] == 4:
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("expected Matchbook to be physically full")
    assert layer.lower_in_use[VenueName.MATCHBOOK] == 2
    active = asyncio.create_task(_take("active_trade"))
    nxt = asyncio.create_task(_take("hot"))
    await asyncio.sleep(0)
    assert grants == []
    release.set()
    await asyncio.wait_for(asyncio.gather(*held, active, nxt), timeout=1)
    assert grants[0] == "active_trade"
    assert grants[1] == "hot"


@pytest.mark.asyncio
async def test_startup_reserves_one_slot_then_restores_the_two_of_four_ceiling() -> None:
    reset_shared_provider_access()
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW)
    coordinator.arm_startup_pricing_barrier()
    layer = get_shared_provider_access()
    assert layer.lower_priority_occupancy_ceiling(VenueName.MATCHBOOK) == 3
    assert layer.lower_priority_occupancy_ceiling(VenueName.KALSHI) == 3
    release = asyncio.Event()

    async def _universe() -> None:
        async with layer.acquire(VenueName.MATCHBOOK, lane="universe"):
            await release.wait()

    held = [asyncio.create_task(_universe()) for _ in range(4)]
    for _ in range(20):
        if layer.lower_in_use[VenueName.MATCHBOOK] == 3:
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("startup UNIVERSE did not stop at three slots")
    assert layer.snapshot().inflight[VenueName.MATCHBOOK.value] == 3

    active_entered = asyncio.Event()

    async def _active() -> None:
        async with layer.acquire(VenueName.MATCHBOOK, lane="active_trade"):
            active_entered.set()
            await release.wait()

    active = asyncio.create_task(_active())
    await active_entered.wait()
    assert layer.snapshot().inflight[VenueName.MATCHBOOK.value] == 4
    assert layer.lower_in_use[VenueName.MATCHBOOK] == 3
    release.set()
    await asyncio.wait_for(asyncio.gather(*held, active), timeout=1)

    coordinator.mark_startup_pricing_ready()
    assert layer._startup_active_headroom is False
    assert layer.hot_provider_demand is False
    assert layer.lower_priority_occupancy_ceiling(VenueName.MATCHBOOK) == 3
    reset_shared_provider_access()


@pytest.mark.asyncio
async def test_hot_pass_covers_every_fixture_and_unstarted_rows_stay_pending() -> None:
    rows = _fixture_rows(fixtures=8, markets=4, near=True)
    expected = _interleaved_ids(rows)
    engine, _mb, _ks, layer = _engine([], timeout=8, hot_interval=0)
    _bind_rows(engine, rows)
    assert engine._provider_timeout == 8
    assert layer.limits == {
        VenueName.MATCHBOOK: 4,
        VenueName.KALSHI: 4,
        VenueName.POLYMARKET: 8,
    }
    due = engine.due_items(PriceEnginePriority.HOT, now=NOW)
    due_ids = [item.identity.catalogue_row_id for item in due]
    assert due_ids[:8] == expected[:8]
    assert len({row.canonical_event_id for row in rows if row.catalogue_row_id in due_ids[:8]}) == 8
    # The claim above is test-only ordering. Release it so the slice can claim honestly.
    engine.coverage_cursor(PriceEnginePriority.HOT).release_unstarted(due_ids)
    engine.note_hot_provider_demand(NOW)
    assert layer.hot_provider_demand is True
    assert (
        layer.lower_priority_occupancy_ceiling(VenueName.MATCHBOOK)
        == HOT_DEMAND_LOWER_OCCUPANCY_CEILING
    )

    hot = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
    fixtures_evaluated = {
        item.identity.canonical_event_id
        for item in engine.items()
        if item.identity.catalogue_row_id in hot.evaluated
    }
    assert fixtures_evaluated == {f"fixture-{index:02d}" for index in range(8)}
    assert len(fixtures_evaluated) > 2
    coverage = hot.hot_coverage or {}
    assert coverage["roster_fixtures"] == 8
    assert coverage["roster_rows"] == 32
    assert coverage["fixtures_evaluated"] == 8
    assert coverage["evaluated_rows"] == 32
    assert "8 HOT fixtures · 32 rows" in str(coverage["summary"])
    assert layer._peak_inflight[VenueName.MATCHBOOK] <= 4
    assert layer._peak_inflight[VenueName.KALSHI] <= 4
    assert layer._peak_inflight[VenueName.POLYMARKET] <= 8

    wide = _fixture_rows(fixtures=10, markets=8, near=True)
    wide_engine, _wmb, _wks, wide_layer = _engine([], timeout=8, hot_interval=0)
    _bind_rows(wide_engine, wide)
    first_id = _interleaved_ids(wide)[0]
    first = await wide_engine.run_slice(PriceEnginePriority.HOT, now=NOW)
    cursor = wide_engine.coverage_cursor(PriceEnginePriority.HOT)
    assert len(first.evaluated) == 64
    assert cursor.pass_number == 1
    assert cursor.hold_until is None
    assert cursor.cursor_after_id != first_id
    second = await wide_engine.run_slice(PriceEnginePriority.HOT, now=NOW)
    assert len(second.evaluated) == 16
    assert cursor.pass_number == 1
    assert cursor.hold_until is not None
    cursor.hold_for_target(target_seconds=10, now=NOW)
    assert wide_engine.due_items(PriceEnginePriority.HOT, now=NOW) == []
    wrapped = wide_engine.due_items(
        PriceEnginePriority.HOT, now=NOW + timedelta(seconds=10)
    )
    assert cursor.pass_number == 2
    assert wrapped[0].identity.catalogue_row_id == first_id
    assert wide_layer.limits[VenueName.MATCHBOOK] == DEFAULT_PROVIDER_CONCURRENCY[VenueName.MATCHBOOK]

    stalled = _fixture_rows(fixtures=8, markets=2, near=True)
    stalled_engine, _smb, _sks, _slayer = _engine([], timeout=8, hot_interval=0)
    _bind_rows(stalled_engine, stalled)
    missed = await stalled_engine.run_slice(
        PriceEnginePriority.HOT, slice_wall_seconds=0, now=NOW
    )
    stalled_cursor = stalled_engine.coverage_cursor(PriceEnginePriority.HOT)
    assert set(missed.not_started) == {row.catalogue_row_id for row in stalled}
    assert stalled_cursor.visited == set()
    assert stalled_cursor.pass_number == 1
    assert stalled_cursor.hold_until is None
    assert stalled_cursor.cursor_after_id is not None
    resumed = stalled_engine.due_items(PriceEnginePriority.HOT, now=NOW)
    assert {item.identity.catalogue_row_id for item in resumed} == {
        row.catalogue_row_id for row in stalled
    }
    assert stalled_cursor.pass_number == 1
    missed_coverage = missed.hot_coverage or {}
    assert missed_coverage["not_started_this_cadence"] == len(stalled)
    assert missed_coverage["started_rows"] == 0
    assert missed_coverage["fixtures_touched"] == 0
    assert "0 fixtures touched · 0 evaluated" in str(missed_coverage["summary"])
    assert missed_coverage["scheduler_disposition"] == "provider_capacity"


@pytest.mark.asyncio
async def test_hot_demand_tightens_then_releases_lower_priority_ceiling() -> None:
    layer = ProviderAccessLayer(
        {VenueName.MATCHBOOK: 4, VenueName.KALSHI: 4, VenueName.POLYMARKET: 8}
    )
    stops = [asyncio.Event() for _ in range(3)]

    async def _hold(stop: asyncio.Event) -> None:
        async with layer.acquire(VenueName.MATCHBOOK, lane="background"):
            await stop.wait()

    held = [asyncio.create_task(_hold(stop)) for stop in stops]
    for _ in range(20):
        if layer.lower_in_use[VenueName.MATCHBOOK] == 3:
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("expected three idle lower-priority slots")
    layer.set_hot_provider_demand(True)
    assert layer.lower_priority_occupancy_ceiling(VenueName.MATCHBOOK) == 1
    assert layer.lower_in_use[VenueName.MATCHBOOK] == 3

    extra_entered = asyncio.Event()

    async def _extra() -> None:
        async with layer.acquire(VenueName.MATCHBOOK, lane="background"):
            extra_entered.set()
            await stops[0].wait()

    extra = asyncio.create_task(_extra())
    await asyncio.sleep(0)
    assert not extra_entered.is_set()
    grants: list[str] = []

    async def _take(lane: str) -> None:
        async with layer.acquire(VenueName.MATCHBOOK, lane=lane):
            grants.append(lane)

    active = asyncio.create_task(_take("active_trade"))
    hot = asyncio.create_task(_take("hot"))
    stops[0].set()
    await asyncio.wait({active, hot}, timeout=1)
    assert grants[0] == "active_trade"
    assert grants[1] == "hot"
    assert not extra_entered.is_set()
    stops[1].set()
    stops[2].set()
    await asyncio.wait_for(asyncio.gather(*held), timeout=1)
    for _ in range(20):
        if layer.lower_in_use[VenueName.MATCHBOOK] <= 1:
            break
        await asyncio.sleep(0)
    assert layer.lower_in_use[VenueName.MATCHBOOK] <= 1
    layer.set_hot_provider_demand(False)
    assert layer.lower_priority_occupancy_ceiling(VenueName.MATCHBOOK) == 3
    await asyncio.wait_for(extra, timeout=1)
    assert extra_entered.is_set()
    refill = [asyncio.create_task(_hold(asyncio.Event())) for _ in range(3)]
    for _ in range(20):
        if layer.lower_in_use[VenueName.MATCHBOOK] == 3:
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("lower-priority work did not reuse idle capacity")
    for task in refill:
        task.cancel()
    await asyncio.gather(*refill, return_exceptions=True)


def test_background_pause_keeps_the_coverage_cursor(tmp_path) -> None:
    from sports_hedge.persistence.operator_scanner_settings import (
        SqliteOperatorScannerSettingsStore,
    )

    rows = _fixture_rows(fixtures=30, markets=1, near=False)
    engine, _mb, _ks, _layer = _engine(rows, timeout=8, background_interval=0)
    first = engine.due_items(PriceEnginePriority.BACKGROUND, now=NOW)
    cursor = engine.coverage_cursor(PriceEnginePriority.BACKGROUND)
    anchor = cursor.cursor_after_id
    visited = set(cursor.visited)
    assert len(first) == 24
    assert anchor == first[-1].identity.catalogue_row_id
    store = SqliteOperatorScannerSettingsStore(tmp_path / "ops.sqlite")
    coordinator = LiveRefreshCoordinator(
        clock=lambda: NOW,
        operator_settings_store=store,
    )
    coordinator.configure_from_settings()
    coordinator.bind_price_engine(engine)
    coordinator._next_background_due = NOW
    saved = coordinator.apply_background_pricing_paused(True)
    assert saved.background_pricing_paused is True
    assert coordinator.plan_background_tick(now=NOW).reason == "background_paused"
    assert coordinator.plan_hot_tick(now=NOW).reason != "background_paused"
    assert cursor.cursor_after_id == anchor
    assert cursor.visited == visited
    assert cursor.pass_number == 1
    reloaded = LiveRefreshCoordinator(
        clock=lambda: NOW,
        operator_settings_store=store,
    )
    reloaded.configure_from_settings()
    assert reloaded.background_pricing_paused is True
    coordinator.apply_background_pricing_paused(False)
    resumed = engine.due_items(PriceEnginePriority.BACKGROUND, now=NOW)
    assert resumed
    assert resumed[0].identity.catalogue_row_id != first[0].identity.catalogue_row_id
    assert cursor.pass_number == 1
    store.close()


@pytest.mark.asyncio
async def test_retry_waiting_hot_wake_is_not_a_pricing_cycle() -> None:
    from sports_hedge.application.price_engine import HOT_IDLE_SCHEDULER_DISPOSITIONS

    rows = _fixture_rows(fixtures=8, markets=2, near=True)
    engine, _mb, _ks, layer = _engine([], timeout=8, hot_interval=0)
    _bind_rows(engine, rows)
    for runtime in engine.items():
        if runtime.priority is PriceEnginePriority.HOT:
            runtime.next_retry_at = NOW + timedelta(seconds=30)
    assert engine.peek_hot_idle_reason(NOW) == "retry_waiting"
    assert layer.hot_provider_demand is False
    result = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
    coverage = result.hot_coverage or {}
    assert coverage["claimed_rows"] == 0
    assert coverage["scheduler_disposition"] in HOT_IDLE_SCHEDULER_DISPOSITIONS
    assert coverage["deferred_rows"] == 0
    assert coverage["not_started_this_cadence"] == 0


@pytest.mark.asyncio
async def test_clearing_hot_demand_admits_queued_lower_priority_immediately() -> None:
    layer = ProviderAccessLayer(
        {VenueName.MATCHBOOK: 4, VenueName.KALSHI: 4, VenueName.POLYMARKET: 8}
    )
    layer.set_hot_provider_demand(True)
    assert layer.lower_priority_occupancy_ceiling(VenueName.MATCHBOOK) == HOT_DEMAND_LOWER_OCCUPANCY_CEILING
    release = asyncio.Event()
    entered: list[str] = []

    async def _hold(name: str) -> None:
        async with layer.acquire(VenueName.MATCHBOOK, lane="background"):
            entered.append(name)
            await release.wait()

    holder = asyncio.create_task(_hold("held"))
    for _ in range(20):
        if layer.lower_in_use[VenueName.MATCHBOOK] == 1:
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("lower-priority lease was not granted")
    queued = [asyncio.create_task(_hold(name)) for name in ("q1", "q2")]
    for _ in range(20):
        waiting = layer.snapshot().waiting_by_lane["background"][VenueName.MATCHBOOK.value]
        if waiting == 2:
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("queued lower-priority waiters were not sitting")
    assert entered == ["held"]
    assert layer.snapshot().inflight[VenueName.MATCHBOOK.value] == 1
    layer.set_hot_provider_demand(False)
    assert layer.hot_provider_demand is False
    assert layer.lower_priority_occupancy_ceiling(VenueName.MATCHBOOK) == 3
    assert layer.lower_in_use[VenueName.MATCHBOOK] == 3
    assert layer.snapshot().inflight[VenueName.MATCHBOOK.value] == 3
    assert layer.snapshot().waiting_by_lane["background"][VenueName.MATCHBOOK.value] == 0
    assert layer._peak_inflight[VenueName.MATCHBOOK] <= 4
    release.set()
    await asyncio.wait_for(asyncio.gather(holder, *queued), timeout=1)
    assert set(entered) == {"held", "q1", "q2"}


@pytest.mark.asyncio
async def test_background_pause_stops_ungranted_provider_work(tmp_path) -> None:
    from test_issue344_price_engine import FakeKalshi, FakeMatchbook, _mb_btts

    from sports_hedge.persistence.operator_scanner_settings import (
        SqliteOperatorScannerSettingsStore,
    )

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

    rows = _fixture_rows(fixtures=30, markets=1, near=False)
    matchbook = _GatedMatchbook()
    kalshi = FakeKalshi()
    engine, _mb, _ks, layer = _engine(
        [],
        timeout=8,
        background_interval=0,
        matchbook=matchbook,
        kalshi=kalshi,
    )
    _bind_rows(engine, rows)
    layer.set_hot_provider_demand(True)
    store = SqliteOperatorScannerSettingsStore(tmp_path / "ops.sqlite")
    coordinator = LiveRefreshCoordinator(
        clock=lambda: NOW,
        operator_settings_store=store,
    )
    coordinator.configure_from_settings()
    coordinator.bind_price_engine(engine)
    slice_task = asyncio.create_task(
        engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    )
    await asyncio.wait_for(matchbook.entered.wait(), timeout=2)
    for _ in range(40):
        waiting = layer.snapshot().waiting_by_lane["background"][VenueName.MATCHBOOK.value]
        if waiting >= 2 and len(matchbook.get_market_calls) >= 1:
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError(
            f"expected granted and queued BACKGROUND work, calls={matchbook.get_market_calls} "
            f"waiting={layer.snapshot().waiting_by_lane['background']}"
        )
    calls_while_open = list(matchbook.get_market_calls)
    assert layer.lower_in_use[VenueName.MATCHBOOK] >= 1
    assert layer.snapshot().inflight[VenueName.MATCHBOOK.value] <= 4
    coordinator.apply_background_pricing_paused(True)
    for _ in range(20):
        if layer.snapshot().waiting_by_lane["background"][VenueName.MATCHBOOK.value] == 0:
            break
        await asyncio.sleep(0)
    assert layer.background_admission_paused is True
    assert layer.snapshot().waiting_by_lane["background"][VenueName.MATCHBOOK.value] == 0
    assert matchbook.get_market_calls == calls_while_open
    async with layer.acquire_wait(
        VenueName.KALSHI, lane="background", timeout=0.5
    ) as refused:
        assert refused is None
    async with layer.acquire_wait(VenueName.KALSHI, lane="universe", timeout=0.5) as universe:
        assert universe is not None
    async with layer.acquire_wait(
        VenueName.KALSHI, lane="settlement", timeout=0.5
    ) as settlement:
        assert settlement is not None
    assert matchbook.get_market_calls == calls_while_open
    assert kalshi.book_calls == []
    matchbook.release.set()
    result = await asyncio.wait_for(slice_task, timeout=3)
    assert matchbook.get_market_calls == calls_while_open
    assert kalshi.book_calls == []
    assert result.evaluated == []
    assert result.not_started
    cursor = engine.coverage_cursor(PriceEnginePriority.BACKGROUND)
    assert cursor.pass_number == 1
    anchor = cursor.cursor_after_id
    first_claimed = cursor.last_claimed[0]
    assert first_claimed not in cursor.visited
    for row_id in result.not_started:
        assert row_id not in cursor.visited
    coordinator.apply_background_pricing_paused(False)
    assert layer.background_admission_paused is False
    assert cursor.cursor_after_id == anchor
    assert cursor.pass_number == 1
    resumed = engine.due_items(PriceEnginePriority.BACKGROUND, now=NOW)
    assert resumed
    assert resumed[0].identity.catalogue_row_id != first_claimed
    assert cursor.pass_number == 1
    store.close()


def test_background_resume_clears_paused_status_before_the_next_heartbeat(tmp_path) -> None:
    from sports_hedge.persistence.operator_scanner_settings import (
        SqliteOperatorScannerSettingsStore,
    )

    rows = _fixture_rows(fixtures=12, markets=1, near=False)
    engine, _mb, _ks, _layer = _engine(rows, timeout=8, background_interval=0)
    first = engine.due_items(PriceEnginePriority.BACKGROUND, now=NOW)
    cursor = engine.coverage_cursor(PriceEnginePriority.BACKGROUND)
    anchor = cursor.cursor_after_id
    visited = set(cursor.visited)
    pass_number = cursor.pass_number
    assert first
    store = SqliteOperatorScannerSettingsStore(tmp_path / "ops.sqlite")
    coordinator = LiveRefreshCoordinator(
        clock=lambda: NOW,
        operator_settings_store=store,
    )
    coordinator.configure_from_settings()
    coordinator.bind_price_engine(engine)
    coordinator.apply_background_pricing_paused(True)
    paused = coordinator.public_status()
    assert paused.background_pricing_paused is True
    assert paused.background.last_plan_reason == "background_paused"
    assert "paused" in (paused.background.operator_summary or "")
    coordinator.apply_background_pricing_paused(False)
    resumed = coordinator.public_status()
    assert resumed.background_pricing_paused is False
    assert resumed.background.last_plan_reason == "waiting"
    assert "paused" not in (resumed.background.operator_summary or "").casefold()
    assert "paused" not in (resumed.operator_summary or "").casefold()
    assert resumed.background.worker_state == "waiting"
    assert cursor.cursor_after_id == anchor
    assert cursor.visited == visited
    assert cursor.pass_number == pass_number
    store.close()
