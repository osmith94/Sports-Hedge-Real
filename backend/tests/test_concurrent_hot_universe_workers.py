"""Tenet 19: concurrent HOT and UNIVERSE workers.

Deterministic fakes and coordinator barriers. Not owner-live evidence.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic

import pytest

from sports_hedge.application.collector import (
    CollectionReport,
    DiscoveredFixture,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.current_market_inventory import (
    stored_row_proves_surveillance_opportunity,
)
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.hot_identity import hot_scheduling_key, scheduling_team_key
from sports_hedge.application.live_refresh import DualCadencePlan, LiveRefreshCoordinator
from sports_hedge.application.universe_checkpoint import SWEEP_RETRY_WAIT
from sports_hedge.application.provider_access import (
    HEALTH_DEFERRED,
    HEALTH_DISCOVERY_TIMEOUT,
    HEALTH_WAITING,
    ProviderAccessLayer,
    reset_shared_provider_access,
)
from sports_hedge.application.scan_lanes import (
    HOT_REASON_SURVEILLANCE,
    WORKER_RUNNING,
    ScanLane,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.persistence.universe_checkpoint import SqliteUniverseCheckpointStore
from test_dual_cadence_scheduler import NOW, FakeClock, _decision, _fixture, _report
from test_issue200_universe_hot_promotion import (
    CANONICAL_ID,
    _market_row,
    _qualifying_universe_report,
    _report as _promo_report,
)

KICKOFF = NOW + timedelta(days=2)


def _universe_fixture(canonical_id: str, *, evaluation: str = "evaluated") -> DiscoveredFixture:
    return _fixture(canonical_id, kickoff=KICKOFF, evaluation=evaluation)


def _named_fixture(
    canonical_id: str,
    *,
    home: str,
    away: str,
    kickoff: datetime = KICKOFF,
    evaluation: str = "evaluated",
) -> DiscoveredFixture:
    fixture = _fixture(canonical_id, kickoff=kickoff, evaluation=evaluation)
    return fixture.model_copy(update={"home_team": home, "away_team": away})


@pytest.fixture(autouse=True)
def _reset_provider_layer() -> None:
    reset_shared_provider_access()
    yield
    reset_shared_provider_access()


@pytest.mark.asyncio
async def test_hot_begins_while_universe_remains_active() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    live = _fixture("live", kickoff=NOW - timedelta(minutes=1), in_running=True)
    coordinator.record_report(_report([live], when=NOW), scan_lane=ScanLane.HOT)
    coordinator._next_hot_due = NOW
    coordinator._next_universe_due = NOW
    universe_started = asyncio.Event()
    universe_hold = asyncio.Event()
    hot_started = asyncio.Event()
    trace: list[tuple[float, str]] = []
    origin = monotonic()

    async def universe_runner() -> CollectionReport:
        trace.append((monotonic() - origin, "universe_start"))
        universe_started.set()
        await universe_hold.wait()
        trace.append((monotonic() - origin, "universe_progress"))
        return _report([_universe_fixture("u1")], when=clock.now, scan_lane=ScanLane.UNIVERSE.value)

    async def hot_runner() -> CollectionReport:
        trace.append((monotonic() - origin, "hot1_start"))
        hot_started.set()
        await asyncio.sleep(0.01)
        trace.append((monotonic() - origin, "hot1_complete"))
        return _report(
            [_fixture("live", kickoff=NOW - timedelta(minutes=1), in_running=True)],
            when=clock.now,
            scan_lane=ScanLane.HOT.value,
        )

    universe_task = asyncio.create_task(
        coordinator.run_cycle(universe_runner, timeout_seconds=None, scan_lane=ScanLane.UNIVERSE)
    )
    await universe_started.wait()
    assert coordinator._universe_in_progress is True
    assert coordinator.plan_hot_tick(now=clock.now).lane == "hot"
    hot_task = asyncio.create_task(
        coordinator.run_cycle(hot_runner, timeout_seconds=2.0, scan_lane=ScanLane.HOT)
    )
    await hot_started.wait()
    assert coordinator._hot_in_progress is True
    assert coordinator._universe_in_progress is True
    await hot_task
    assert coordinator._universe_in_progress is True
    assert coordinator.status.universe.cycle_in_progress is True
    universe_hold.set()
    await universe_task
    labels = [item[1] for item in trace]
    assert labels.index("hot1_start") < labels.index("universe_progress")
    assert coordinator._universe_in_progress is False


@pytest.mark.asyncio
async def test_universe_remains_active_after_hot_completes() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    universe_started = asyncio.Event()
    universe_hold = asyncio.Event()

    async def universe_runner() -> CollectionReport:
        universe_started.set()
        await universe_hold.wait()
        return _report([_universe_fixture("keep")], when=clock.now, scan_lane=ScanLane.UNIVERSE.value)

    async def hot_runner() -> CollectionReport:
        return _report(
            [_fixture("live", kickoff=NOW - timedelta(minutes=1), in_running=True)],
            when=clock.now,
            scan_lane=ScanLane.HOT.value,
        )

    universe_task = asyncio.create_task(
        coordinator.run_cycle(universe_runner, timeout_seconds=None, scan_lane=ScanLane.UNIVERSE)
    )
    await universe_started.wait()
    await coordinator.run_cycle(hot_runner, timeout_seconds=2.0, scan_lane=ScanLane.HOT)
    assert coordinator._universe_in_progress is True
    assert coordinator.status.universe.worker_state == WORKER_RUNNING
    universe_hold.set()
    await universe_task


@pytest.mark.asyncio
async def test_multiple_hot_cycles_during_one_universe_sweep() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    universe_started = asyncio.Event()
    universe_hold = asyncio.Event()
    hot_starts = 0
    trace: list[tuple[float, str]] = []
    origin = monotonic()

    async def universe_runner() -> CollectionReport:
        trace.append((monotonic() - origin, "universe_start"))
        universe_started.set()
        await universe_hold.wait()
        trace.append((monotonic() - origin, "universe_complete"))
        return _report(
            [_universe_fixture("u-final")],
            when=clock.now,
            scan_lane=ScanLane.UNIVERSE.value,
        )

    async def hot_runner() -> CollectionReport:
        nonlocal hot_starts
        hot_starts += 1
        trace.append((monotonic() - origin, f"hot{hot_starts}_start"))
        await asyncio.sleep(0.01)
        trace.append((monotonic() - origin, f"hot{hot_starts}_complete"))
        return _report(
            [_fixture("live", kickoff=NOW - timedelta(minutes=1), in_running=True)],
            when=clock.now,
            scan_lane=ScanLane.HOT.value,
        )

    universe_task = asyncio.create_task(
        coordinator.run_cycle(universe_runner, timeout_seconds=None, scan_lane=ScanLane.UNIVERSE)
    )
    await universe_started.wait()
    await coordinator.run_cycle(hot_runner, timeout_seconds=2.0, scan_lane=ScanLane.HOT)
    assert coordinator._universe_in_progress is True
    await coordinator.run_cycle(hot_runner, timeout_seconds=2.0, scan_lane=ScanLane.HOT)
    assert hot_starts == 2
    assert coordinator._universe_in_progress is True
    universe_hold.set()
    await universe_task
    labels = [item[1] for item in trace]
    assert labels.index("hot1_complete") < labels.index("hot2_start")
    assert labels.index("hot2_complete") < labels.index("universe_complete")
    print("OVERLAP_TRACE")
    for offset, label in trace:
        print(f"{offset:06.3f} {label}")


@pytest.mark.asyncio
async def test_hot_never_resets_universe_cursor_or_completed_work() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator._mark_lane_started(ScanLane.UNIVERSE, NOW)
    coordinator.record_universe_fixture_progress(
        None,
        _universe_fixture("done-1"),
        [],
        [],
    )
    cursor = coordinator._universe_cursor
    evaluated = set(coordinator._universe_evaluated_ids)
    async def hot_runner() -> CollectionReport:
        return _report(
            [_fixture("live", kickoff=NOW - timedelta(minutes=1), in_running=True)],
            when=clock.now,
            scan_lane=ScanLane.HOT.value,
        )

    await coordinator.run_cycle(hot_runner, timeout_seconds=2.0, scan_lane=ScanLane.HOT)
    assert coordinator._universe_cursor == cursor
    assert coordinator._universe_evaluated_ids == evaluated
    assert coordinator._universe_generation_started_at is not None


@pytest.mark.asyncio
async def test_universe_reaches_final_fixture_under_healthy_fakes() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    ids = [f"fix-{index:02d}" for index in range(8)]

    async def runner() -> CollectionReport:
        return _report(
            [_universe_fixture(item) for item in ids],
            when=clock.now,
            scan_lane=ScanLane.UNIVERSE.value,
        )

    plan = coordinator.plan_universe_tick(now=clock.now)
    assert plan.lane == "universe"
    assert plan.collector_timeout_seconds is not None
    assert plan.unbounded_cycle is False
    report = await coordinator.run_cycle(runner, timeout_seconds=None, scan_lane=ScanLane.UNIVERSE)
    assert {item.canonical_event_id for item in report.discovered_fixtures} == set(ids)
    assert coordinator._universe_generation_started_at is None
    assert coordinator.status.universe.worker_state == "complete"
    assert coordinator._status_universe_evaluated_count() == 8


@pytest.mark.asyncio
async def test_shared_provider_concurrency_limit_is_obeyed() -> None:
    layer = ProviderAccessLayer({VenueName.MATCHBOOK: 1}, starvation_hot_grants=8)
    peak = 0
    current = 0
    lock = asyncio.Lock()

    async def occupy(lane: str) -> None:
        nonlocal peak, current
        async with layer.acquire(VenueName.MATCHBOOK, lane=lane):
            async with lock:
                current += 1
                peak = max(peak, current)
            await asyncio.sleep(0.03)
            async with lock:
                current -= 1

    await asyncio.gather(occupy("universe"), occupy("universe"), occupy("hot"))
    assert peak == 1
    assert layer.snapshot().limits["matchbook"] == 1


@pytest.mark.asyncio
async def test_hot_receives_priority_under_provider_contention() -> None:
    layer = ProviderAccessLayer({VenueName.MATCHBOOK: 1}, starvation_hot_grants=32)
    order: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with layer.acquire(VenueName.MATCHBOOK, lane="universe"):
            started.set()
            await release.wait()

    async def waiter(lane: str) -> None:
        await started.wait()
        async with layer.acquire(VenueName.MATCHBOOK, lane=lane):
            order.append(lane)

    tasks = [
        asyncio.create_task(holder()),
        asyncio.create_task(waiter("universe")),
        asyncio.create_task(waiter("hot")),
    ]
    await started.wait()
    await asyncio.sleep(0.02)
    assert layer.venue_wait_reason(VenueName.MATCHBOOK, lane="universe") in {
        HEALTH_WAITING,
        HEALTH_DEFERRED,
    }
    release.set()
    await asyncio.gather(*tasks)
    assert order[0] == "hot"


@pytest.mark.asyncio
async def test_universe_resumes_after_provider_contention_without_restart() -> None:
    layer = ProviderAccessLayer({VenueName.MATCHBOOK: 1}, starvation_hot_grants=2)
    cursor = ["start"]

    async def universe_step() -> None:
        async with layer.acquire(VenueName.MATCHBOOK, lane="universe"):
            cursor.append("universe")
            await asyncio.sleep(0.01)

    async def hot_step() -> None:
        async with layer.acquire(VenueName.MATCHBOOK, lane="hot"):
            cursor.append("hot")
            await asyncio.sleep(0.01)

    await asyncio.gather(universe_step(), hot_step(), universe_step())
    assert cursor.count("universe") == 2
    assert "hot" in cursor


@pytest.mark.asyncio
async def test_universe_is_not_starved_indefinitely() -> None:
    layer = ProviderAccessLayer({VenueName.MATCHBOOK: 1}, starvation_hot_grants=2)
    grants: list[str] = []
    stop = asyncio.Event()

    async def hot_loop() -> None:
        while not stop.is_set():
            async with layer.acquire(VenueName.MATCHBOOK, lane="hot"):
                grants.append("hot")
                await asyncio.sleep(0.005)

    async def universe_once() -> None:
        async with layer.acquire(VenueName.MATCHBOOK, lane="universe"):
            grants.append("universe")
            stop.set()

    hot_task = asyncio.create_task(hot_loop())
    await asyncio.sleep(0.01)
    await asyncio.wait_for(universe_once(), timeout=1.0)
    hot_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await hot_task
    assert "universe" in grants


@pytest.mark.asyncio
async def test_one_provider_failure_does_not_erase_successful_work() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator._mark_lane_started(ScanLane.UNIVERSE, NOW)
    coordinator.record_universe_fixture_progress(None, _universe_fixture("mb-ok"), [], [])
    coordinator.record_universe_fixture_progress(
        None,
        _universe_fixture("kalshi-fail", evaluation="market_fetch_unavailable"),
        [],
        [],
    )
    assert "mb-ok" in coordinator._universe_evaluated_ids
    failed = coordinator._universe_work["kalshi-fail"]
    assert failed.state == SWEEP_RETRY_WAIT
    assert "kalshi-fail" not in coordinator._universe_failed_ids
    assert coordinator._universe_sweep_is_complete_unlocked() is False
    assert coordinator._universe_last_successful == "mb-ok"


def test_useful_fixture_result_persists_before_universe_completion() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator._mark_lane_started(ScanLane.UNIVERSE, NOW)
    fixture = _universe_fixture("mid-sweep").model_copy(update={"last_scanned_at": NOW})
    coordinator.record_universe_fixture_progress(None, fixture, [], [])
    assert coordinator.fixture_current_state().detail("mid-sweep", now=NOW) is not None
    assert coordinator._universe_generation_started_at is not None
    assert coordinator.status.universe.evaluated_count >= 1


def test_promotion_occurs_mid_sweep_and_does_not_stop_universe() -> None:
    coordinator = LiveRefreshCoordinator()
    coordinator._mark_lane_started(ScanLane.UNIVERSE, NOW)
    report = _qualifying_universe_report()
    fixture = report.discovered_fixtures[0]
    markets = report.fixture_markets[CANONICAL_ID]
    coordinator.record_universe_fixture_progress(None, fixture, list(report.paper_decisions), markets)
    assert CANONICAL_ID in coordinator.fixture_current_state().hot_identity_scope(NOW)
    assert coordinator._universe_in_progress is True
    assert coordinator._universe_generation_started_at is not None
    assert coordinator._universe_hot_promotions >= 1
    nxt = coordinator.plan_universe_tick(now=NOW)
    assert nxt.lane == "idle"
    assert nxt.reason == "universe_in_progress"


def test_sub_threshold_positive_edge_promotes_without_paper_trade() -> None:
    store = FixtureCurrentStateStore()
    row = _market_row(edge=Decimal("0.004"), arb=False, trigger=Decimal("0.01"))
    assert stored_row_proves_surveillance_opportunity(row) is True
    fixture = _fixture(CANONICAL_ID, kickoff=KICKOFF, evaluation="evaluated")
    fixture = fixture.model_copy(update={"opportunity_state": "near", "solver_is_arbitrage": False})
    decision = _decision(CANONICAL_ID, "mkt-sub", when=NOW)
    assert decision.eligible_for_paper_simulation is False
    store.upsert_from_report(
        _promo_report(
            [fixture],
            markets={CANONICAL_ID: [row]},
            decisions=[decision],
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    assert CANONICAL_ID in store.hot_identity_scope(NOW)
    inventory = store.inventory(NOW)
    shown = next(item for item in inventory if item.canonical_event_id == CANONICAL_ID)
    assert HOT_REASON_SURVEILLANCE in shown.hot_reasons


def test_source_aliases_collapse_to_one_hot_scheduling_unit() -> None:
    assert scheduling_team_key("Real Betis Balompié") == scheduling_team_key("Real Betis")
    assert scheduling_team_key("Getafe CF") == scheduling_team_key("Getafe")
    left = _named_fixture("betis-a", home="Real Betis Balompié", away="Getafe CF")
    right = _named_fixture("betis-b", home="Real Betis", away="Getafe")
    assert hot_scheduling_key(left) == hot_scheduling_key(right)
    store = FixtureCurrentStateStore()
    store.upsert_from_report(
        _report([left, right], when=NOW, scan_lane=ScanLane.UNIVERSE.value),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    scope = store.hot_identity_scope(NOW + timedelta(days=0))
    # Distant kickoff: only surveillance/lifecycle. Force lifecycle HOT via near kickoff.
    near_left = left.model_copy(update={"kickoff_utc": NOW + timedelta(minutes=20)})
    near_right = right.model_copy(update={"kickoff_utc": NOW + timedelta(minutes=20)})
    store.clear()
    store.upsert_from_report(
        _report([near_left, near_right], when=NOW, scan_lane=ScanLane.UNIVERSE.value),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    scope = store.hot_identity_scope(NOW)
    unique, _lifecycle, _promoted = store.hot_membership_breakdown(NOW)
    assert len(scope) == 1
    assert unique == 1
    assert {"betis-a", "betis-b"} <= set(store._rows)
    assert len(store._rows) == 2


def test_repeated_promotion_does_not_duplicate_hot() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(_qualifying_universe_report(), scan_lane=ScanLane.UNIVERSE, now=NOW)
    store.upsert_from_report(
        _qualifying_universe_report(),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW + timedelta(seconds=5),
    )
    unique, _lifecycle, promoted = store.hot_membership_breakdown(NOW + timedelta(seconds=5))
    assert unique == 1
    assert promoted == 1
    assert store.hot_identity_scope(NOW + timedelta(seconds=5)).count(CANONICAL_ID) == 1


def test_older_universe_observation_cannot_overwrite_fresher_hot() -> None:
    store = FixtureCurrentStateStore()
    later = NOW + timedelta(seconds=30)
    hot_fixture = _fixture("keep", kickoff=NOW + timedelta(minutes=20), evaluation="evaluated")
    hot_fixture = hot_fixture.model_copy(update={"home_team": "Alpha", "away_team": "Beta"})
    store.upsert_from_report(
        _report([hot_fixture], when=later, scan_lane=ScanLane.HOT.value),
        scan_lane=ScanLane.HOT,
        now=later,
    )
    older = hot_fixture.model_copy(update={"home_team": "Stale Alpha", "away_team": "Stale Beta"})
    store.upsert_from_report(
        _report([older], when=NOW, scan_lane=ScanLane.UNIVERSE.value),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    detail = store.detail("keep", now=later)
    assert detail is not None
    assert detail.fixture.home_team == "Alpha"


def test_restart_resume_preserves_completed_universe_progress(tmp_path: Path) -> None:
    database = tmp_path / "universe.sqlite"
    store = SqliteUniverseCheckpointStore(database)
    first = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    first.configure_from_settings()
    first._mark_lane_started(ScanLane.UNIVERSE, NOW)
    first.record_universe_discovery_snapshot(
        {
            "matchbook": [{"id": "1", "name": "A v B"}],
            "polymarket": [],
            "kalshi": [],
        }
    )
    first.record_universe_fixture_progress(None, _universe_fixture("done-a"), [], [])
    first.record_universe_fixture_progress(None, _universe_fixture("done-b"), [], [])
    evaluated = set(first._universe_evaluated_ids)
    sweep_id = first._universe_sweep_id
    restarted = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    restarted.configure_from_settings()
    assert restarted._universe_evaluated_ids == evaluated
    assert restarted._universe_sweep_id == sweep_id
    assert restarted._universe_discovery_snapshot is not None
    plan = restarted.plan_universe_tick(now=NOW)
    assert plan.generation_resume is True
    assert plan.reuse_discovery is True
    assert set(plan.skip_event_ids) == evaluated


def test_lane_specific_health_can_be_hot_ok_and_universe_discovery_degraded() -> None:
    coordinator = LiveRefreshCoordinator()
    coordinator.status = coordinator.status.model_copy(
        update={
            "hot": coordinator.status.hot.model_copy(
                update={"venue_health": {"matchbook": "ok", "kalshi": "ok"}}
            ),
            "universe": coordinator.status.universe.model_copy(
                update={
                    "venue_health": {
                        "matchbook": HEALTH_DISCOVERY_TIMEOUT,
                        "kalshi": HEALTH_DISCOVERY_TIMEOUT,
                    },
                    "operation_health": {
                        "matchbook": {"list_events": HEALTH_DISCOVERY_TIMEOUT},
                        "kalshi": {"list_events": HEALTH_DISCOVERY_TIMEOUT},
                    },
                    "degraded": True,
                    "worker_state": "degraded",
                }
            ),
        }
    )
    from sports_hedge.application.live_refresh import _merge_top_level_venue_health

    merged = _merge_top_level_venue_health(
        coordinator.status.hot.venue_health,
        coordinator.status.universe.venue_health,
    )
    assert coordinator.status.hot.venue_health["matchbook"] == "ok"
    assert coordinator.status.universe.venue_health["matchbook"] == HEALTH_DISCOVERY_TIMEOUT
    assert merged["matchbook"] == "degraded"
    assert merged["matchbook"] != "timeout"


def test_no_global_cycle_lock_prevents_wall_clock_overlap() -> None:
    coordinator = LiveRefreshCoordinator()
    assert coordinator._hot_lock is not coordinator._universe_lock
    coordinator._universe_in_progress = True
    coordinator.status = coordinator.status.model_copy(
        update={
            "cycle_in_progress": True,
            "universe": coordinator.status.universe.model_copy(
                update={"cycle_in_progress": True, "worker_state": WORKER_RUNNING}
            ),
        }
    )
    coordinator._next_hot_due = NOW
    live = _fixture("live", kickoff=NOW - timedelta(minutes=1), in_running=True)
    coordinator.record_explicit_report(_report([live], when=NOW, scan_lane=ScanLane.HOT.value))
    plan = coordinator.plan_hot_tick(now=NOW)
    assert plan.lane == "hot"
    assert coordinator.plan_tick(now=NOW).lane != "idle" or plan.lane == "hot"
    idle = coordinator.plan_universe_tick(now=NOW)
    assert idle.reason == "universe_in_progress"


def test_plan_tick_does_not_idle_because_the_other_lane_is_running() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator._universe_in_progress = True
    coordinator.status = coordinator.status.model_copy(update={"cycle_in_progress": True})
    coordinator._next_hot_due = NOW
    live = _fixture("live", kickoff=NOW - timedelta(minutes=1), in_running=True)
    coordinator.record_report(_report([live], when=NOW), scan_lane=ScanLane.HOT)
    coordinator._next_hot_due = NOW
    plan = coordinator.plan_tick(now=NOW)
    assert plan.lane == "hot"
    assert plan.reason != "cycle_in_progress"


def test_discovery_snapshot_is_sweep_state() -> None:
    coordinator = LiveRefreshCoordinator()
    coordinator._mark_lane_started(ScanLane.UNIVERSE, NOW)
    snapshot = {
        "matchbook": [{"id": "mb-1"}, {"id": "mb-2"}],
        "polymarket": [{"id": "pm-1"}],
        "kalshi": [],
    }
    coordinator.record_universe_discovery_snapshot(snapshot)
    plan = DualCadencePlan(
        lane="universe",
        reuse_discovery=True,
        discovery_snapshot=coordinator._universe_discovery_snapshot,
        unbounded_cycle=True,
        reason="universe_sweep",
    )
    assert plan.reuse_discovery is True
    assert plan.discovery_snapshot == snapshot


@pytest.mark.asyncio
async def test_collector_reuses_discovery_snapshot_without_list_events() -> None:
    class CountingMatchbook:
        def __init__(self) -> None:
            self.calls = 0

        async def list_events(self, **filters):
            self.calls += 1
            return {"events": []}

        async def list_markets(self, event_id, **filters):
            return {"markets": []}

    class EmptyPolymarket:
        async def list_events(self, **filters):
            return []

        async def list_markets(self, event_id, **filters):
            return []

        async def get_order_book(self, *args, **kwargs):
            return {}

    matchbook = CountingMatchbook()
    from sports_hedge.application.paper_scan import PaperScanService
    from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
    from sports_hedge.market_intelligence.service import MarketIntelligenceService

    service = PaperScanService(MarketIntelligenceService(SqliteMarketIntelligenceRepository()))
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=EmptyPolymarket(),
        paper_scan=service,
        cycle_timeout_seconds=None,
    )
    report = await collector.collect_and_scan(
        scan_lane=ScanLane.UNIVERSE.value,
        reuse_discovery=True,
        discovery_snapshot={"matchbook": [], "polymarket": [], "kalshi": []},
        unbounded_cycle=True,
        max_event_pairs=1,
    )
    assert matchbook.calls == 0
    assert report.discovery_reused is True
