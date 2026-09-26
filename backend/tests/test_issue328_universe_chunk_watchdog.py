"""Issue #328: bounded UNIVERSE chunks, generation-consistent status.

Deterministic coordinator fakes. Not owner-live quotes.
PAPER / read-only. Does not change #326/#329 catalogue matching.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta
from time import monotonic

import pytest

from sports_hedge.application.collector import CollectionReport
from sports_hedge.application.live_refresh import (
    SCAN_CYCLE_RETURN_GRACE_SECONDS,
    LiveRefreshCoordinator,
    ScanCycleTimeout,
)
from sports_hedge.application.scan_lanes import (
    UNIVERSE_MIN_CHUNK_SECONDS,
    ScanLane,
    universe_chunk_wall_seconds,
)
from sports_hedge.application.target_competitions import polymarket_series_ids_for_targets
from sports_hedge.config import Settings
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.markets import MarketMatcher
from test_concurrent_hot_universe_workers import _universe_fixture
from test_dual_cadence_scheduler import NOW, FakeClock, _fixture, _report


def _partial_report(*ids: str, leftover: str | None = None, when=NOW) -> CollectionReport:
    fixtures = [_universe_fixture(item) for item in ids]
    if leftover is not None:
        fixtures.append(_universe_fixture(leftover, evaluation="not_evaluated_scan_deadline"))
    report = _report(fixtures, when=when, scan_lane=ScanLane.UNIVERSE.value)
    return report.model_copy(
        update={
            "scan_diagnostics": {
                "canonical_work_total": len(fixtures),
                "completeness": "deadline_leftover" if leftover else "complete",
            }
        }
    )


def test_chunk_wall_is_independent_of_next_hot_due() -> None:
    """Tenet 19: HOT due times must not time-slice UNIVERSE chunks."""

    hot_due = NOW + timedelta(seconds=3)
    wall = universe_chunk_wall_seconds(
        now=NOW,
        next_hot_due=hot_due,
        remaining_generation_budget=150,
        safety_margin_seconds=2.0,
    )
    assert wall is not None
    assert wall >= UNIVERSE_MIN_CHUNK_SECONDS
    assert wall == pytest.approx(148.0)
    assert wall > (hot_due - NOW).total_seconds()


def test_scheduled_universe_plan_uses_chunk_wall_not_unbounded() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    plan = coordinator.plan_universe_tick(now=NOW)
    collector, coordinator_timeout = coordinator._universe_chunk_timeouts(now=NOW)
    assert plan.lane == "universe"
    assert plan.unbounded_cycle is False
    assert plan.collector_timeout_seconds == collector
    assert plan.coordinator_timeout_seconds == coordinator_timeout
    assert plan.coordinator_timeout_seconds == collector + SCAN_CYCLE_RETURN_GRACE_SECONDS
    assert plan.collector_timeout_seconds is not None
    assert "universe_chunk_wall_seconds" in inspect.getsource(
        coordinator._universe_chunk_timeouts
    )
    assert "unbounded_cycle=False" in inspect.getsource(LiveRefreshCoordinator.plan_universe_tick)


@pytest.mark.asyncio
async def test_hung_provider_cannot_leave_universe_chunk_in_progress() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    started = asyncio.Event()

    async def hung_runner() -> CollectionReport:
        coordinator.record_universe_discovery_snapshot(
            {"matchbook": [{"id": "mb-1"}], "polymarket": [], "kalshi": []}
        )
        coordinator.record_universe_work_set(["a", "b", "c"], authoritative=True)
        coordinator.record_universe_fixture_progress(
            None, _universe_fixture("a"), [], []
        )
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("hung runner resumed")

    started_mono = monotonic()
    with pytest.raises(ScanCycleTimeout):
        await coordinator.run_cycle(
            hung_runner, timeout_seconds=0.25, scan_lane=ScanLane.UNIVERSE
        )
    elapsed = monotonic() - started_mono
    assert started.is_set()
    assert elapsed < 1.5
    status = coordinator.public_status()
    assert status.universe.cycle_in_progress is False
    assert coordinator._universe_in_progress is False
    assert coordinator._universe_generation_started_at is not None
    assert "a" in coordinator._universe_evaluated_ids
    assert coordinator._universe_cursor == "a"
    assert status.universe.last_heartbeat_at is not None
    assert status.universe.worker_state in {"waiting", "degraded"}
    assert "complete" not in (status.universe.operator_summary or "").casefold()


@pytest.mark.asyncio
async def test_cancel_ignoring_work_unit_still_returns_control() -> None:
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW)

    async def stubborn_runner() -> CollectionReport:
        coordinator.record_universe_work_set(["a", "b"], authoritative=True)
        coordinator.record_universe_fixture_progress(
            None, _universe_fixture("a"), [], []
        )
        ignored = 0
        while True:
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                ignored += 1
                if ignored >= 8:
                    return _partial_report("a", leftover="b")
                continue

    started = monotonic()
    with pytest.raises(ScanCycleTimeout):
        await coordinator.run_cycle(
            stubborn_runner, timeout_seconds=0.2, scan_lane=ScanLane.UNIVERSE
        )
    assert monotonic() - started < 1.5
    assert coordinator.status.universe.cycle_in_progress is False
    assert "a" in coordinator._universe_evaluated_ids
    assert coordinator._universe_orphaned_chunk_count >= 1
    assert coordinator._universe_active_chunk_epoch is None
    for _ in range(12):
        live = [task for task in coordinator._universe_orphaned_tasks if not task.done()]
        if not live:
            break
        for task in live:
            task.cancel()
        await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_timed_out_chunk_cannot_mutate_replacement_checkpoint() -> None:
    """Cancellation-ignoring chunk cannot append to a later UNIVERSE chunk."""

    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    recorded_a = asyncio.Event()
    after_replacement = asyncio.Event()
    stale_attempted = asyncio.Event()
    replacement_started = asyncio.Event()
    zombie_epoch: dict[str, int | None] = {}
    original_snapshot = {
        "matchbook": [{"id": "mb-safe"}],
        "polymarket": [],
        "kalshi": [],
    }

    async def zombie_chunk() -> CollectionReport:
        on_discovery, on_fixture, on_work = coordinator.universe_collect_callbacks()
        zombie_epoch["value"] = coordinator._universe_active_chunk_epoch
        on_discovery(original_snapshot)
        on_work(["a", "b", "c"], authoritative=True)
        on_fixture(None, _universe_fixture("a"), [], [])
        recorded_a.set()
        while not after_replacement.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                continue
        on_discovery(
            {
                "matchbook": [{"id": "zombie-stale"}],
                "polymarket": [],
                "kalshi": [],
            }
        )
        on_work(["a", "b", "zombie-extra"], authoritative=True)
        on_fixture(None, _universe_fixture("b"), [], [])
        stale_attempted.set()
        return _partial_report("a", leftover="b", when=clock.now)

    started = monotonic()
    with pytest.raises(ScanCycleTimeout):
        await coordinator.run_cycle(
            zombie_chunk, timeout_seconds=0.25, scan_lane=ScanLane.UNIVERSE
        )
    assert monotonic() - started < 1.5
    await asyncio.wait_for(recorded_a.wait(), timeout=1)
    assert "a" in coordinator._universe_evaluated_ids
    assert "b" not in coordinator._universe_evaluated_ids
    assert coordinator._universe_active_chunk_epoch is None
    assert coordinator._universe_orphaned_chunk_count >= 1
    generation = coordinator._universe_generation_id
    sweep = coordinator._universe_sweep_id
    safe_events = (coordinator._universe_discovery_snapshot or {}).get("matchbook") or []
    assert any(isinstance(row, dict) and row.get("id") == "mb-safe" for row in safe_events)

    live = _fixture("live", kickoff=NOW - timedelta(minutes=1), in_running=True)

    async def hot_runner() -> CollectionReport:
        return _report([live], when=clock.now, scan_lane=ScanLane.HOT.value)

    await coordinator.run_cycle(hot_runner, timeout_seconds=2.0, scan_lane=ScanLane.HOT)
    assert coordinator.status.hot.cycle_in_progress is False
    assert coordinator._universe_generation_started_at is not None
    assert coordinator._universe_generation_id == generation

    coordinator._universe_retry_at = None
    plan = coordinator.plan_universe_tick(now=clock.now)
    assert plan.lane == "universe"
    assert plan.generation_resume is True
    assert "a" in plan.skip_event_ids
    assert plan.unbounded_cycle is False

    async def replacement_chunk() -> CollectionReport:
        replacement_started.set()
        await stale_attempted.wait()
        on_discovery, on_fixture, on_work = coordinator.universe_collect_callbacks()
        assert coordinator._universe_active_chunk_epoch != zombie_epoch["value"]
        on_work(["a", "b", "c"], authoritative=True)
        on_fixture(None, _universe_fixture("c"), [], [])
        return _partial_report("c", leftover="b", when=clock.now)

    replacement_task = asyncio.create_task(
        coordinator.run_cycle(
            replacement_chunk, timeout_seconds=2.0, scan_lane=ScanLane.UNIVERSE
        )
    )
    await asyncio.wait_for(replacement_started.wait(), timeout=1)
    after_replacement.set()
    await asyncio.wait_for(stale_attempted.wait(), timeout=1)
    assert "b" not in coordinator._universe_evaluated_ids
    assert "zombie-extra" not in coordinator._universe_work
    discovery = coordinator._universe_discovery_snapshot or {}
    matchbook_events = discovery.get("matchbook") or []
    assert not any(
        isinstance(row, dict) and row.get("id") == "zombie-stale" for row in matchbook_events
    )
    assert coordinator._universe_stale_callback_count >= 3
    running = coordinator.public_status()
    summary = (running.universe.operator_summary or "") + " " + (running.operator_summary or "")
    assert "68/68" not in summary
    assert "complete" not in (running.universe.operator_summary or "").casefold()
    diagnostics = running.universe.last_diagnostics or {}
    if diagnostics:
        assert diagnostics.get("sweep_id") in {None, running.universe.sweep_id, sweep}
        assert diagnostics.get("generation_id") in {None, generation}
    await replacement_task
    leftovers = [task for task in coordinator._universe_orphaned_tasks if not task.done()]
    if leftovers:
        await asyncio.wait(leftovers, timeout=1.0)
    assert coordinator._universe_generation_id == generation
    assert coordinator._universe_evaluated_ids >= {"a", "c"}
    assert "b" not in coordinator._universe_evaluated_ids
    assert coordinator._universe_cursor == "c"

    async def finishing_chunk() -> CollectionReport:
        on_discovery, on_fixture, on_work = coordinator.universe_collect_callbacks()
        on_work(["a", "b", "c"], authoritative=True)
        on_fixture(None, _universe_fixture("b"), [], [])
        report = _report(
            [
                _universe_fixture("a"),
                _universe_fixture("b"),
                _universe_fixture("c"),
            ],
            when=clock.now,
            scan_lane=ScanLane.UNIVERSE.value,
        )
        return report.model_copy(
            update={
                "sweep_id": coordinator._universe_sweep_id,
                "scan_diagnostics": {
                    "completeness": "complete",
                    "canonical_work_total": 3,
                },
            }
        )

    await coordinator.run_cycle(
        finishing_chunk, timeout_seconds=2.0, scan_lane=ScanLane.UNIVERSE
    )
    finished = coordinator.public_status()
    assert finished.universe.cycle_in_progress is False
    assert coordinator._universe_generation_started_at is None
    assert finished.universe.worker_state == "complete"
    assert coordinator._universe_closed_evaluated_count == 3
    assert finished.universe.sweep_id in {None, sweep}
    assert "68/68" not in (finished.universe.operator_summary or "")


@pytest.mark.asyncio
async def test_next_chunk_resumes_checkpoint_without_rediscovery() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    snapshot = {
        "matchbook": [{"id": "mb-1"}, {"id": "mb-2"}, {"id": "mb-3"}],
        "polymarket": [],
        "kalshi": [],
    }

    async def first_chunk() -> CollectionReport:
        coordinator.record_universe_discovery_snapshot(snapshot)
        coordinator.record_universe_work_set(["a", "b", "c"], authoritative=True)
        coordinator.record_universe_fixture_progress(
            None, _universe_fixture("a"), [], []
        )
        return _partial_report("a", leftover="b", when=clock.now)

    await coordinator.run_cycle(first_chunk, timeout_seconds=2.0, scan_lane=ScanLane.UNIVERSE)
    assert coordinator._universe_generation_started_at is not None
    generation = coordinator._universe_generation_id
    sweep = coordinator._universe_sweep_id
    assert "a" in coordinator._universe_evaluated_ids
    plan = coordinator.plan_universe_tick(now=clock.now)
    assert plan.lane == "universe"
    assert plan.generation_resume is True
    assert plan.reuse_discovery is True
    assert plan.discovery_snapshot == coordinator._universe_discovery_snapshot
    assert "a" in plan.skip_event_ids
    assert plan.universe_generation_id == generation
    assert plan.sweep_id == sweep
    assert plan.unbounded_cycle is False

    async def second_chunk() -> CollectionReport:
        coordinator.record_universe_fixture_progress(
            None, _universe_fixture("b"), [], []
        )
        return _partial_report("b", leftover="c", when=clock.now)

    await coordinator.run_cycle(second_chunk, timeout_seconds=2.0, scan_lane=ScanLane.UNIVERSE)
    assert coordinator._universe_generation_id == generation
    assert coordinator._universe_sweep_id == sweep
    assert coordinator._universe_evaluated_ids >= {"a", "b"}
    assert coordinator._universe_generation_started_at is not None


@pytest.mark.asyncio
async def test_generation_stays_open_across_bounded_chunks() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock

    async def chunk(evaluated: str, leftover: str) -> CollectionReport:
        coordinator.record_universe_work_set(
            ["one", "two", "three"], authoritative=True
        )
        coordinator.record_universe_fixture_progress(
            None, _universe_fixture(evaluated), [], []
        )
        return _partial_report(evaluated, leftover=leftover, when=clock.now)

    async def first() -> CollectionReport:
        return await chunk("one", "two")

    await coordinator.run_cycle(first, timeout_seconds=2.0, scan_lane=ScanLane.UNIVERSE)
    first_id = coordinator._universe_generation_id
    assert coordinator._universe_generation_started_at is not None
    clock.advance(1)

    async def second() -> CollectionReport:
        return await chunk("two", "three")

    await coordinator.run_cycle(second, timeout_seconds=2.0, scan_lane=ScanLane.UNIVERSE)
    assert coordinator._universe_generation_id == first_id
    assert coordinator.status.universe.worker_state != "complete"


@pytest.mark.asyncio
async def test_hot_stays_independent_while_universe_chunks_or_retries() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    live = _fixture("live", kickoff=NOW - timedelta(minutes=1), in_running=True)
    coordinator.record_report(_report([live], when=NOW), scan_lane=ScanLane.HOT)
    coordinator._next_hot_due = NOW
    hung = asyncio.Event()

    async def universe_runner() -> CollectionReport:
        hung.set()
        await asyncio.Event().wait()
        return _partial_report("u1", leftover="u2")

    universe_task = asyncio.create_task(
        coordinator.run_cycle(
            universe_runner, timeout_seconds=0.4, scan_lane=ScanLane.UNIVERSE
        )
    )
    await hung.wait()
    hot_plan = coordinator.plan_hot_tick(now=clock.now)
    assert hot_plan.lane == "hot"

    async def hot_runner() -> CollectionReport:
        return _report([live], when=clock.now, scan_lane=ScanLane.HOT.value)

    await coordinator.run_cycle(hot_runner, timeout_seconds=2.0, scan_lane=ScanLane.HOT)
    assert coordinator.status.hot.cycle_in_progress is False
    with pytest.raises(ScanCycleTimeout):
        await universe_task
    assert coordinator.status.universe.cycle_in_progress is False


@pytest.mark.asyncio
async def test_running_generation_does_not_reuse_prior_complete_summary() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock

    async def complete_runner() -> CollectionReport:
        coordinator.record_universe_work_set(["old-a", "old-b"], authoritative=True)
        coordinator.record_universe_fixture_progress(
            None, _universe_fixture("old-a"), [], []
        )
        coordinator.record_universe_fixture_progress(
            None, _universe_fixture("old-b"), [], []
        )
        report = _report(
            [_universe_fixture("old-a"), _universe_fixture("old-b")],
            when=clock.now,
            scan_lane=ScanLane.UNIVERSE.value,
        )
        return report.model_copy(
            update={
                "sweep_id": coordinator._universe_sweep_id,
                "scan_diagnostics": {
                    "completeness": "complete",
                    "canonical_work_total": 2,
                    "sweep_id": "sweep-54-old",
                    "generation_id": 56,
                },
            }
        )

    await coordinator.run_cycle(
        complete_runner, timeout_seconds=2.0, scan_lane=ScanLane.UNIVERSE
    )
    completed = coordinator.public_status()
    prior_summary = completed.universe.operator_summary or ""
    assert "complete" in prior_summary.casefold() or completed.universe.worker_state == "complete"

    async def hung_new_generation() -> CollectionReport:
        coordinator.record_universe_work_set(
            [f"new-{index}" for index in range(5)], authoritative=True
        )
        await asyncio.Event().wait()
        raise AssertionError("should time out")

    with pytest.raises(ScanCycleTimeout):
        await coordinator.run_cycle(
            hung_new_generation, timeout_seconds=0.2, scan_lane=ScanLane.UNIVERSE
        )
    live = coordinator.public_status()
    summary = (live.universe.operator_summary or "") + " " + (live.operator_summary or "")
    assert "2/2" not in summary
    assert "68/68" not in summary
    assert "complete" not in (live.universe.operator_summary or "").casefold()
    diagnostics = live.universe.last_diagnostics or {}
    assert diagnostics.get("sweep_id") != "sweep-54-old"
    assert diagnostics.get("generation_id") != 56
    if diagnostics:
        assert diagnostics.get("sweep_id") in {None, live.universe.sweep_id}


@pytest.mark.asyncio
async def test_stale_diagnostics_are_not_presented_as_current_sweep() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator._mark_lane_started(ScanLane.UNIVERSE, NOW)
    coordinator.status = coordinator.status.model_copy(
        update={
            "universe": coordinator.status.universe.model_copy(
                update={
                    "operator_summary": "Full sweep · complete · elapsed 781.5s · 68/68 evaluated · 0 remaining",
                    "last_diagnostics": {
                        "sweep_id": "sweep-54-old",
                        "generation_id": 56,
                        "completeness": "complete",
                        "canonical_work_total": 68,
                    },
                    "canonical_work_total": 113,
                    "canonical_evaluated": 72,
                    "canonical_remaining": 41,
                }
            )
        }
    )
    status = coordinator.public_status()
    assert "68/68" not in (status.universe.operator_summary or "")
    assert "complete" not in (status.universe.operator_summary or "").casefold()
    assert status.universe.cycle_in_progress is True
    diagnostics = status.universe.last_diagnostics
    assert diagnostics is None or diagnostics.get("sweep_id") != "sweep-54-old"


@pytest.mark.asyncio
async def test_retry_wait_clears_cycle_in_progress() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock

    async def retryable_chunk() -> CollectionReport:
        coordinator.record_universe_work_set(["ok-id", "retry-id"], authoritative=True)
        coordinator.record_universe_fixture_progress(
            None, _universe_fixture("ok-id"), [], []
        )
        retry = _universe_fixture("retry-id", evaluation="market_fetch_unavailable")
        retry = retry.model_copy(update={"market_evaluation_reason": "provider_timeout"})
        report = _report(
            [_universe_fixture("ok-id"), retry],
            when=clock.now,
            scan_lane=ScanLane.UNIVERSE.value,
        )
        return report.model_copy(
            update={"scan_diagnostics": {"canonical_work_total": 2, "completeness": "partial"}}
        )

    await coordinator.run_cycle(
        retryable_chunk, timeout_seconds=2.0, scan_lane=ScanLane.UNIVERSE
    )
    status = coordinator.public_status()
    assert status.universe.cycle_in_progress is False
    assert coordinator._universe_in_progress is False
    plan = coordinator.plan_universe_tick(now=clock.now)
    if plan.lane == "idle":
        assert plan.reason in {"universe_retry_wait", "universe_provider_backoff", "waiting"}
        coordinator._record_universe_heartbeat(plan)
        waiting = coordinator.public_status()
        assert waiting.universe.cycle_in_progress is False
        assert waiting.universe.worker_state == "waiting"
        assert "in progress" not in (waiting.universe.operator_summary or "").casefold()
        assert waiting.universe.last_heartbeat_at is not None
    else:
        assert status.universe.worker_state in {"waiting", "degraded", "idle"}
        assert "complete" not in (status.universe.operator_summary or "").casefold()


def test_issue328_does_not_expand_polymarket_or_enable_execution() -> None:
    settings = Settings()
    assert settings.sports_hedge_execution_enabled is False
    assert settings.sports_hedge_mode == "paper"
    assert settings.resolved_polymarket_series_ids() == polymarket_series_ids_for_targets()
    assert EventMatcher().threshold == 0.92


def test_issue328_does_not_rewrite_market_catalogue_matcher() -> None:
    # Stage 1B caches identical fixture-local results in match(). The register
    # body lives in _match_uncached and is still the only admission path.
    source = inspect.getsource(MarketMatcher.match) + inspect.getsource(
        MarketMatcher._match_uncached
    )
    assert "event_mismatch" in source
    assert "registered_canonical_key" in source
    assert "REGISTER_ADMITTED_REASON" in source
    assert "economic_mismatch_reasons" not in source
    assert "_match_uncached" in inspect.getsource(MarketMatcher.match)
