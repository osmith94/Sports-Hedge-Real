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
    LOWER_PRIORITY_OCCUPANCY_CEILING,
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
    assert layer.lower_priority_occupancy_ceiling(VenueName.MATCHBOOK) == LOWER_PRIORITY_OCCUPANCY_CEILING
    assert layer.lower_priority_occupancy_ceiling(VenueName.KALSHI) == 2
    assert layer.lower_priority_occupancy_ceiling(VenueName.POLYMARKET) is None
    assert Settings.model_fields["paper_scan_provider_timeout_seconds"].default == 8

    release = asyncio.Event()

    async def _hold(venue: VenueName, lane: str) -> None:
        async with layer.acquire(venue, lane=lane):
            await release.wait()

    background = [
        asyncio.create_task(_hold(venue, "background"))
        for venue in (VenueName.MATCHBOOK, VenueName.KALSHI)
        for _ in range(2)
    ]
    universe = [
        asyncio.create_task(_hold(venue, "universe"))
        for venue in (VenueName.MATCHBOOK, VenueName.KALSHI)
    ]
    for _ in range(20):
        if (
            layer.lower_in_use[VenueName.MATCHBOOK] == 2
            and layer.lower_in_use[VenueName.KALSHI] == 2
        ):
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("lower-priority leases did not reach the ceiling")
    assert all(not task.done() for task in universe)

    hot_entered = asyncio.Event()

    async def _hot(venue: VenueName) -> None:
        async with layer.acquire(venue, lane="hot"):
            hot_entered.set()
            await release.wait()

    hot = asyncio.create_task(_hot(VenueName.MATCHBOOK))
    await hot_entered.wait()
    assert layer.lower_in_use[VenueName.MATCHBOOK] == 2
    assert layer.snapshot().inflight[VenueName.MATCHBOOK.value] == 3
    assert layer.peak_lower_in_use[VenueName.MATCHBOOK] <= 2
    assert layer.peak_lower_in_use[VenueName.KALSHI] <= 2

    release.set()
    await asyncio.wait_for(asyncio.gather(*background, *universe, hot), timeout=1)
    assert layer.peak_lower_in_use[VenueName.MATCHBOOK] <= 2
    assert layer.peak_lower_in_use[VenueName.KALSHI] <= 2


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
    assert layer.lower_priority_occupancy_ceiling(VenueName.MATCHBOOK) == 2
    assert layer._startup_active_headroom is False
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

    background_rows = _fixture_rows(fixtures=4, markets=1, near=False)
    background, _bmb, _bks, _blayer = _engine(
        background_rows, access=layer, timeout=8, background_interval=0
    )
    background_result = await background.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    assert background_result.evaluated
    assert layer.peak_lower_in_use[VenueName.MATCHBOOK] <= 2
    assert layer.peak_lower_in_use[VenueName.KALSHI] <= 2

    release = asyncio.Event()
    universe_entered: list[int] = []

    async def _universe(index: int) -> None:
        async with layer.acquire(VenueName.MATCHBOOK, lane="universe"):
            universe_entered.append(index)
            if len(universe_entered) < 3:
                await release.wait()

    universe = [asyncio.create_task(_universe(index)) for index in range(3)]
    for _ in range(20):
        if len(universe_entered) == 2:
            break
        await asyncio.sleep(0)
    else:
        raise AssertionError("UNIVERSE did not make forward progress inside the ceiling")
    assert layer.lower_in_use[VenueName.MATCHBOOK] == 2

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
    assert layer.peak_lower_in_use[VenueName.MATCHBOOK] <= 2
    assert layer._peak_inflight[VenueName.MATCHBOOK] <= 4
    assert layer._peak_inflight[VenueName.KALSHI] <= 4
    assert layer._peak_inflight[VenueName.POLYMARKET] <= 8

    release.set()
    await asyncio.wait_for(asyncio.gather(*universe), timeout=1)
    assert len(universe_entered) == 3
    assert layer.peak_lower_in_use[VenueName.MATCHBOOK] <= 2

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
