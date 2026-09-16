from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from sports_hedge.application.collector import (
    UNIVERSE_COMPLETENESS_COMPLETE,
    UNIVERSE_COMPLETENESS_DEADLINE_LEFTOVER,
    UNIVERSE_COMPLETENESS_EMPTY_UNIVERSE,
    UNIVERSE_COMPLETENESS_STALE_GENERATION_STATE,
    ReadOnlyCrossVenueCollector,
    universe_sweep_completeness,
)
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from test_dual_cadence_scheduler import NOW, FakeClock, _fixture, _report
from test_read_only_collector import FakeMatchbook, FakePolymarket
from venue_cost_helpers import matchbook_polymarket_costs

SKIP_N = 8


def _universe_fixtures(count: int = SKIP_N):
    return [
        _fixture(f"ev-{index}", kickoff=NOW + timedelta(days=2, minutes=index))
        for index in range(count)
    ]


def _live_fixture():
    return _fixture("live", kickoff=NOW - timedelta(minutes=1), in_running=True)


def test_universe_sweep_completeness_distinguishes_leftover_empty_and_stale() -> None:
    assert (
        universe_sweep_completeness(
            leftover_n=3,
            evaluated_n=5,
            clusters_before_resume=8,
            skipped_by_resume=0,
            deadline_hit=True,
            generation_resume=True,
        )
        == UNIVERSE_COMPLETENESS_DEADLINE_LEFTOVER
    )
    assert (
        universe_sweep_completeness(
            leftover_n=0,
            evaluated_n=0,
            clusters_before_resume=0,
            skipped_by_resume=0,
            deadline_hit=False,
            generation_resume=False,
        )
        == UNIVERSE_COMPLETENESS_EMPTY_UNIVERSE
    )
    assert (
        universe_sweep_completeness(
            leftover_n=0,
            evaluated_n=0,
            clusters_before_resume=8,
            skipped_by_resume=8,
            deadline_hit=False,
            generation_resume=False,
        )
        == UNIVERSE_COMPLETENESS_STALE_GENERATION_STATE
    )
    assert (
        universe_sweep_completeness(
            leftover_n=0,
            evaluated_n=8,
            clusters_before_resume=8,
            skipped_by_resume=0,
            deadline_hit=False,
            generation_resume=False,
        )
        == UNIVERSE_COMPLETENESS_COMPLETE
    )
    assert (
        universe_sweep_completeness(
            leftover_n=0,
            evaluated_n=3,
            clusters_before_resume=8,
            skipped_by_resume=5,
            deadline_hit=False,
            generation_resume=True,
        )
        == UNIVERSE_COMPLETENESS_COMPLETE
    )


@pytest.mark.asyncio
async def test_completed_generation_does_not_leak_skip_cursor_into_next_generation() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    live = _live_fixture()
    universe = _universe_fixtures()
    coordinator.record_report(_report([live, *universe], when=NOW), scan_lane=ScanLane.UNIVERSE)
    closed_generation = coordinator._universe_generation_id
    assert coordinator._universe_generation_started_at is None
    assert coordinator._universe_evaluated_ids == set()
    assert coordinator._universe_cursor is None
    assert coordinator._universe_work_used == 0.0
    assert coordinator._universe_progress_generation_id is None
    coordinator._next_hot_due = coordinator._next_universe_due + timedelta(seconds=1_000)
    clock.now = coordinator._next_universe_due
    plan = coordinator.plan_tick(now=clock.now)
    assert plan.lane == "universe"
    assert plan.skip_event_ids == []
    assert plan.resume_cursor is None
    assert plan.generation_resume is False
    assert plan.universe_generation_id == closed_generation + 1
    evaluated: list[str] = []

    async def runner():
        fixtures = [live, *universe]
        evaluated.extend(item.canonical_event_id for item in fixtures)
        when = clock.now
        return _report(fixtures, when=when, scan_lane=ScanLane.UNIVERSE.value).model_copy(
            update={"completed_at": when + timedelta(seconds=1)}
        )

    await coordinator.run_cycle(
        runner,
        timeout_seconds=plan.coordinator_timeout_seconds,
        scan_lane=ScanLane.UNIVERSE,
    )
    assert set(evaluated) >= {item.canonical_event_id for item in universe}
    assert coordinator._universe_generation_id == closed_generation + 1
    assert coordinator._universe_generation_started_at is None
    assert coordinator._universe_evaluated_ids == set()


@pytest.mark.asyncio
async def test_closing_generation_clears_local_state_before_planning_next() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator._next_hot_due = NOW + timedelta(seconds=10_000)
    coordinator._next_universe_due = NOW
    universe = _universe_fixtures()

    async def runner():
        when = clock.now
        return _report(universe, when=when, scan_lane=ScanLane.UNIVERSE.value).model_copy(
            update={"completed_at": when + timedelta(seconds=2)}
        )

    first = coordinator.plan_tick(now=clock.now)
    assert first.lane == "universe"
    await coordinator.run_cycle(
        runner,
        timeout_seconds=first.coordinator_timeout_seconds,
        scan_lane=ScanLane.UNIVERSE,
    )
    assert coordinator._universe_generation_started_at is None
    assert coordinator._universe_evaluated_ids == set()
    assert coordinator._universe_cursor is None
    assert coordinator._universe_work_used == 0.0
    assert coordinator._universe_progress_generation_id is None
    assert coordinator.status.universe.evaluated_count == SKIP_N
    next_due = coordinator._next_universe_due
    assert next_due is not None and next_due > clock.now
    idle = coordinator.plan_tick(now=clock.now)
    assert idle.lane != "universe"
    clock.now = next_due
    nxt = coordinator.plan_tick(now=clock.now)
    assert nxt.lane == "universe"
    assert nxt.skip_event_ids == []
    assert nxt.resume_cursor is None
    assert nxt.generation_resume is False
    assert nxt.universe_generation_id == first.universe_generation_id + 1


@pytest.mark.asyncio
async def test_partial_generation_hot_preempt_resumes_same_skip_and_cursor() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    live = _live_fixture()
    coordinator.record_report(_report([live], when=NOW), scan_lane=ScanLane.HOT)
    coordinator._next_hot_due = NOW + timedelta(seconds=20)
    coordinator._next_universe_due = NOW
    coordinator._universe_generation_started_at = None
    coordinator._universe_work_used = 0.0
    coordinator._universe_evaluated_ids = set()
    coordinator._universe_cursor = None

    async def universe_runner():
        when = clock.now
        if not coordinator._universe_evaluated_ids:
            fixtures = [
                _fixture("a", kickoff=NOW + timedelta(days=2), evaluation="evaluated"),
                _fixture(
                    "b",
                    kickoff=NOW + timedelta(days=3),
                    evaluation="not_evaluated_scan_deadline",
                ),
            ]
        else:
            fixtures = [_fixture("b", kickoff=NOW + timedelta(days=3), evaluation="evaluated")]
        return _report(fixtures, when=when, scan_lane=ScanLane.UNIVERSE.value).model_copy(
            update={"completed_at": when + timedelta(seconds=3)}
        )

    first = coordinator.plan_tick(now=clock.now)
    assert first.lane == "universe"
    assert first.generation_resume is False
    first_generation = first.universe_generation_id
    await coordinator.run_cycle(
        universe_runner,
        timeout_seconds=first.coordinator_timeout_seconds,
        scan_lane=ScanLane.UNIVERSE,
    )
    assert coordinator._universe_generation_started_at is not None
    assert "a" in coordinator._universe_evaluated_ids
    clock.now = NOW + timedelta(seconds=20)
    hot_plan = coordinator.plan_tick(now=clock.now)
    assert hot_plan.lane == "hot"

    async def hot_runner():
        return _report([live], when=clock.now, scan_lane=ScanLane.HOT.value)

    await coordinator.run_cycle(
        hot_runner,
        timeout_seconds=hot_plan.coordinator_timeout_seconds,
        scan_lane=ScanLane.HOT,
    )
    clock.advance(1)
    resumed = coordinator.plan_tick(now=clock.now)
    assert resumed.lane == "universe"
    assert resumed.generation_resume is True
    assert resumed.universe_generation_id == first_generation
    assert resumed.resume_cursor == "a"
    assert "a" in resumed.skip_event_ids
    await coordinator.run_cycle(
        universe_runner,
        timeout_seconds=resumed.coordinator_timeout_seconds,
        scan_lane=ScanLane.UNIVERSE,
    )
    assert coordinator._universe_cursor == "b" or coordinator._universe_closed_cursor == "b"


def test_empty_hot_does_not_block_startup_universe() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator.reset()
    coordinator._clock = clock
    assert coordinator.fixture_current_state().has_collection() is False
    plan = coordinator.plan_tick(now=NOW)
    assert plan.lane == "universe"
    assert plan.skip_event_ids == []
    assert plan.resume_cursor is None
    assert plan.generation_resume is False
    assert plan.universe_generation_id == 1
    assert coordinator.universe_due_immediately()


@pytest.mark.asyncio
async def test_mixed_ticks_every_new_generation_re_evaluates_universe() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    live = _live_fixture()
    universe = _universe_fixtures()
    universe_ids = {item.canonical_event_id for item in universe}
    evaluations_by_generation: dict[int, set[str]] = {}
    false_complete = 0
    lane_ticks = 0
    plans = 0
    while plans < 60:
        plan = coordinator.plan_tick(now=clock.now)
        plans += 1
        if plan.lane == "idle":
            wait = coordinator.seconds_until_next_work(now=clock.now)
            clock.advance(max(wait, 1.0))
            continue
        lane_ticks += 1
        if plan.lane == "hot":

            async def hot_runner():
                return _report([live], when=clock.now, scan_lane=ScanLane.HOT.value)

            await coordinator.run_cycle(
                hot_runner,
                timeout_seconds=plan.coordinator_timeout_seconds,
                scan_lane=ScanLane.HOT,
            )
            clock.advance(0.05)
            continue
        skip = set(plan.skip_event_ids)
        gen = plan.universe_generation_id
        if not plan.generation_resume:
            assert skip == set()
            assert plan.resume_cursor is None
        remaining = [item for item in universe if item.canonical_event_id not in skip]
        if not remaining and skip:
            false_complete += 1
            fixtures = []
        else:
            fixtures = [
                item.model_copy(update={"market_evaluation_state": "evaluated"})
                for item in remaining
            ]
            if live.canonical_event_id not in skip:
                fixtures.append(live)
        evaluations_by_generation.setdefault(gen, set()).update(
            item.canonical_event_id for item in fixtures
        )

        async def universe_runner(payload=fixtures):
            when = clock.now
            return _report(payload, when=when, scan_lane=ScanLane.UNIVERSE.value).model_copy(
                update={"completed_at": when + timedelta(seconds=1)}
            )

        await coordinator.run_cycle(
            universe_runner,
            timeout_seconds=plan.coordinator_timeout_seconds,
            scan_lane=ScanLane.UNIVERSE,
        )
        clock.advance(0.05)
    assert plans >= 50
    assert lane_ticks >= 20
    assert false_complete == 0
    completed_gens = [
        gen
        for gen, ids in evaluations_by_generation.items()
        if universe_ids <= ids
    ]
    assert len(completed_gens) >= 2
    for gen, ids in evaluations_by_generation.items():
        if gen in completed_gens:
            assert universe_ids <= ids


@pytest.mark.asyncio
async def test_provider_failure_does_not_leak_skip_into_next_generation() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    live = _live_fixture()
    coordinator.record_report(_report([live], when=NOW), scan_lane=ScanLane.HOT)
    coordinator._next_hot_due = NOW + timedelta(seconds=20)
    coordinator._next_universe_due = NOW

    async def partial_runner():
        when = clock.now
        return _report(
            [
                _fixture("a", kickoff=NOW + timedelta(days=2), evaluation="evaluated"),
                _fixture(
                    "b",
                    kickoff=NOW + timedelta(days=3),
                    evaluation="not_evaluated_scan_deadline",
                ),
            ],
            when=when,
            scan_lane=ScanLane.UNIVERSE.value,
        ).model_copy(update={"completed_at": when + timedelta(seconds=2)})

    first = coordinator.plan_tick(now=clock.now)
    await coordinator.run_cycle(
        partial_runner,
        timeout_seconds=first.coordinator_timeout_seconds,
        scan_lane=ScanLane.UNIVERSE,
    )
    open_generation = coordinator._universe_generation_id
    assert "a" in coordinator._universe_evaluated_ids

    async def fail_runner():
        clock.advance(50)
        raise RuntimeError("provider_timeout")

    second = coordinator.plan_tick(now=clock.now)
    assert second.lane == "universe"
    assert second.generation_resume is True
    assert "a" in second.skip_event_ids
    with pytest.raises(RuntimeError, match="provider_timeout"):
        await coordinator.run_cycle(
            fail_runner,
            timeout_seconds=second.coordinator_timeout_seconds,
            scan_lane=ScanLane.UNIVERSE,
        )
    assert coordinator._universe_generation_id == open_generation
    assert "a" in coordinator._universe_evaluated_ids
    while coordinator._universe_generation_started_at is not None:
        coordinator._next_hot_due = clock.now + timedelta(seconds=300)
        plan = coordinator.plan_tick(now=clock.now)
        assert plan.lane == "universe"
        with pytest.raises(RuntimeError, match="provider_timeout"):
            await coordinator.run_cycle(
                fail_runner,
                timeout_seconds=plan.coordinator_timeout_seconds,
                scan_lane=ScanLane.UNIVERSE,
            )
    assert coordinator._universe_evaluated_ids == set()
    assert coordinator._universe_cursor is None
    assert coordinator._universe_work_used == 0.0
    clock.now = coordinator._next_universe_due
    coordinator._next_hot_due = clock.now + timedelta(seconds=1_000)
    nxt = coordinator.plan_tick(now=clock.now)
    assert nxt.lane == "universe"
    assert nxt.generation_resume is False
    assert nxt.skip_event_ids == []
    assert nxt.resume_cursor is None
    assert nxt.universe_generation_id == open_generation + 1


@pytest.mark.asyncio
async def test_cancellation_does_not_leak_closed_generation_skip() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator._next_hot_due = NOW + timedelta(seconds=10_000)
    coordinator._next_universe_due = NOW

    async def cancel_runner():
        clock.advance(50)
        raise RuntimeError("scan_cancelled")

    plan = coordinator.plan_tick(now=clock.now)
    with pytest.raises(RuntimeError, match="scan_cancelled"):
        await coordinator.run_cycle(
            cancel_runner,
            timeout_seconds=plan.coordinator_timeout_seconds,
            scan_lane=ScanLane.UNIVERSE,
        )
    assert coordinator._universe_generation_started_at is not None
    resumed = coordinator.plan_tick(now=clock.now)
    assert resumed.lane == "universe"
    assert resumed.universe_generation_id == plan.universe_generation_id
    coordinator._universe_evaluated_ids = {f"ev-{index}" for index in range(SKIP_N)}
    coordinator._universe_cursor = "ev-7"
    while coordinator._universe_generation_started_at is not None:
        coordinator._next_hot_due = clock.now + timedelta(seconds=300)
        fail_plan = coordinator.plan_tick(now=clock.now)
        assert fail_plan.lane == "universe"
        with pytest.raises(RuntimeError, match="scan_cancelled"):
            await coordinator.run_cycle(
                cancel_runner,
                timeout_seconds=fail_plan.coordinator_timeout_seconds,
                scan_lane=ScanLane.UNIVERSE,
            )
    clock.now = coordinator._next_universe_due
    nxt = coordinator.plan_tick(now=clock.now)
    assert nxt.lane == "universe"
    assert nxt.skip_event_ids == []
    assert nxt.resume_cursor is None
    assert nxt.generation_resume is False


@pytest.mark.asyncio
async def test_collector_ignores_stale_skip_on_new_generation() -> None:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=FakeMatchbook(),
        polymarket=FakePolymarket(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        baseline = await collector.collect_and_scan(
            venue_costs=matchbook_polymarket_costs("0.02", "0.02"),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            maximum_execution_risk=100,
            scan_lane=ScanLane.UNIVERSE.value,
            generation_resume=False,
            universe_generation_id=2,
        )
        ids = [item.canonical_event_id for item in baseline.discovered_fixtures]
        assert ids
        leaked = await collector.collect_and_scan(
            venue_costs=matchbook_polymarket_costs("0.02", "0.02"),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            maximum_execution_risk=100,
            scan_lane=ScanLane.UNIVERSE.value,
            skip_event_ids=ids,
            resume_cursor=ids[-1],
            generation_resume=False,
            universe_generation_id=2,
        )
        assert leaked.scan_diagnostics["stale_generation_state_ignored"] is True
        assert leaked.scan_diagnostics["completeness"] == UNIVERSE_COMPLETENESS_COMPLETE
        assert leaked.scan_diagnostics["partial"] is False
        assert len(leaked.discovered_fixtures) == len(baseline.discovered_fixtures)
        resumed = await collector.collect_and_scan(
            venue_costs=matchbook_polymarket_costs("0.02", "0.02"),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            maximum_execution_risk=100,
            scan_lane=ScanLane.UNIVERSE.value,
            skip_event_ids=ids,
            resume_cursor=ids[-1],
            generation_resume=True,
            universe_generation_id=1,
        )
        assert resumed.scan_diagnostics["stale_generation_state_ignored"] is False
        assert resumed.scan_diagnostics["skipped_by_resume_count"] >= 1
    finally:
        repository.close()


def test_stale_skip_after_close_cannot_bind_to_next_plan() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    universe = _universe_fixtures()
    coordinator.record_report(_report(universe, when=NOW), scan_lane=ScanLane.UNIVERSE)
    coordinator._universe_evaluated_ids = {item.canonical_event_id for item in universe}
    coordinator._universe_cursor = "ev-7"
    coordinator._next_hot_due = coordinator._next_universe_due + timedelta(seconds=1_000)
    clock.now = coordinator._next_universe_due
    plan = coordinator.plan_tick(now=clock.now)
    assert plan.lane == "universe"
    assert plan.skip_event_ids == []
    assert plan.resume_cursor is None
    assert plan.generation_resume is False
