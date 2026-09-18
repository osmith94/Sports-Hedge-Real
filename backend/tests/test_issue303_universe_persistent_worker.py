"""Issue #303: completed UNIVERSE generation must close, cool down, then start a new id.

Owner-live shape on PR #302 head: generation 26 reported completeness=complete,
0 newly evaluated / 0 leftover, same resume cursor, then reran every few seconds.
Deterministic coordinator/collector fixtures. Not owner-live quotes.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sports_hedge.application.collector import (
    UNIVERSE_COMPLETENESS_COMPLETE,
    CollectionReport,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import WORKER_COMPLETE, ScanLane
from sports_hedge.application.universe_checkpoint import (
    SWEEP_PENDING,
    SWEEP_RETRY_WAIT,
    SweepWorkUnit,
)
from sports_hedge.config import Settings, get_settings
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.persistence.universe_checkpoint import SqliteUniverseCheckpointStore
from test_concurrent_hot_universe_workers import _universe_fixture
from test_dual_cadence_scheduler import NOW, FakeClock, _fixture, _report
from test_issue200_universe_hot_promotion import _qualifying_universe_report
from test_read_only_collector import FakeMatchbook, FakePolymarket
from venue_cost_helpers import matchbook_polymarket_costs

OWNER_CURSOR = "evt:e7aa2185e41d94d21a885260"
OWNER_PENDING = "evt:pending-unscanned-fixture"


def _cooldown_seconds() -> int:
    return int(get_settings().paper_universe_worker_cooldown_seconds)


def _owner_live_complete_report(*, when, resume_cursor: str = OWNER_CURSOR) -> CollectionReport:
    return CollectionReport(
        started_at=when,
        completed_at=when + timedelta(milliseconds=4374),
        matched_event_pairs=41,
        matched_market_pairs=0,
        discovered_fixtures=[],
        scan_lane=ScanLane.UNIVERSE.value,
        venue_health={"matchbook": "ok", "kalshi": "ok"},
        resume_cursor=resume_cursor,
        discovery_reused=True,
        operator_summary=(
            "0 fixtures discovered · MB 80 · PM 0 · K 41 · 41 cross-venue matches · "
            "0 equivalent markets · 0 qualifying arbs · 0 out-of-scope skipped"
        ),
        scan_diagnostics={
            "completeness": UNIVERSE_COMPLETENESS_COMPLETE,
            "evaluated_count": 0,
            "not_evaluated_count": 0,
            "generation_resume": True,
            "universe_generation_id": 26,
            "resume_cursor": resume_cursor,
            "clusters_before_resume": 41,
            "skipped_by_resume_count": 41,
            "canonical_work_total": 41,
        },
    )


def _open_owner_live_generation(coordinator: LiveRefreshCoordinator) -> None:
    coordinator._universe_generation_id = 26
    coordinator._universe_generation_started_at = NOW
    coordinator._universe_progress_generation_id = 26
    coordinator._universe_cursor = OWNER_CURSOR
    coordinator._universe_discovery_snapshot = {
        "matchbook": [{"id": 1, "name": "West Ham vs Chelsea"}],
        "polymarket": [],
        "kalshi": [{"event_ticker": "KX-1", "title": "West Ham vs Chelsea"}],
    }
    coordinator._universe_work = {
        OWNER_CURSOR: SweepWorkUnit(canonical_id=OWNER_CURSOR, state=SWEEP_PENDING),
        OWNER_PENDING: SweepWorkUnit(canonical_id=OWNER_PENDING, state=SWEEP_PENDING),
    }
    coordinator._universe_discovered_total = 2
    coordinator._next_universe_due = NOW
    coordinator._universe_in_progress = False


def test_owner_live_complete_zero_work_does_not_skip_pending_or_spin_same_generation() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    _open_owner_live_generation(coordinator)

    coordinator.record_report(
        _owner_live_complete_report(when=clock.now),
        scan_lane=ScanLane.UNIVERSE,
    )
    assert coordinator._universe_generation_started_at is not None
    assert coordinator._universe_generation_id == 26
    assert coordinator._universe_sweep_is_complete_unlocked() is False
    assert coordinator.status.universe.worker_state != WORKER_COMPLETE

    plan = coordinator.plan_universe_tick(now=clock.now)
    assert plan.lane == ScanLane.UNIVERSE.value
    assert plan.universe_generation_id == 26
    assert plan.generation_resume is True
    assert OWNER_CURSOR not in plan.skip_event_ids
    assert OWNER_PENDING not in plan.skip_event_ids
    assert coordinator._seconds_until_universe() == pytest.approx(0.05)


def test_terminal_generation_closes_cools_down_then_starts_new_id() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator._next_hot_due = NOW + timedelta(seconds=10_000)
    coordinator._next_universe_due = NOW
    fixtures = [
        _universe_fixture(OWNER_CURSOR),
        _universe_fixture(OWNER_PENDING),
    ]
    coordinator.record_report(
        _report(fixtures, when=NOW, scan_lane=ScanLane.UNIVERSE.value).model_copy(
            update={
                "completed_at": NOW + timedelta(seconds=4),
                "scan_diagnostics": {"completeness": UNIVERSE_COMPLETENESS_COMPLETE},
            }
        ),
        scan_lane=ScanLane.UNIVERSE,
    )
    closed_generation = coordinator._universe_generation_id
    assert coordinator._universe_generation_started_at is None
    assert coordinator._universe_cursor is None
    assert coordinator._universe_evaluated_ids == set()
    assert coordinator.status.universe.worker_state == WORKER_COMPLETE
    finished = NOW + timedelta(seconds=4)
    cooldown = timedelta(seconds=_cooldown_seconds())
    assert coordinator._next_universe_due == finished + cooldown

    idle = coordinator.plan_universe_tick(now=clock.now)
    assert idle.lane == "idle"
    assert idle.reason == "universe_cooldown"
    still_idle = coordinator.plan_universe_tick(now=finished + cooldown - timedelta(seconds=1))
    assert still_idle.lane == "idle"
    assert still_idle.reason == "universe_cooldown"

    clock.now = finished + cooldown
    nxt = coordinator.plan_universe_tick(now=clock.now)
    assert nxt.lane == ScanLane.UNIVERSE.value
    assert nxt.generation_resume is False
    assert nxt.resume_cursor is None
    assert nxt.skip_event_ids == []
    assert nxt.universe_generation_id == closed_generation + 1


def test_same_generation_is_not_replanned_after_successful_close() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator._next_hot_due = NOW + timedelta(seconds=10_000)
    coordinator.record_report(
        _report([_universe_fixture("done-a")], when=NOW, scan_lane=ScanLane.UNIVERSE.value).model_copy(
            update={
                "completed_at": NOW + timedelta(seconds=3),
                "scan_diagnostics": {"completeness": UNIVERSE_COMPLETENESS_COMPLETE},
            }
        ),
        scan_lane=ScanLane.UNIVERSE,
    )
    closed = coordinator._universe_generation_id
    for offset in (0, 1, 4):
        clock.now = NOW + timedelta(seconds=offset)
        plan = coordinator.plan_universe_tick(now=clock.now)
        assert plan.lane == "idle"
        assert plan.generation_resume is False
        assert plan.universe_generation_id == 0 or plan.lane == "idle"
        assert coordinator._universe_generation_id == closed
        assert coordinator._universe_generation_started_at is None


def test_retryable_unit_waits_instead_of_false_complete_busy_loop() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator._next_hot_due = NOW + timedelta(seconds=10_000)
    coordinator.record_universe_work_set(["ok-a", "retry-b"])
    coordinator.record_universe_fixture_progress(None, _universe_fixture("ok-a"), [], [])
    coordinator.record_universe_fixture_progress(
        None,
        _universe_fixture("retry-b", evaluation="market_fetch_unavailable"),
        [],
        [],
    )
    coordinator._universe_in_progress = False
    assert coordinator._universe_work["retry-b"].state == SWEEP_RETRY_WAIT
    assert coordinator._universe_sweep_is_complete_unlocked() is False
    coordinator._charge_successful_universe_work(
        0.0, clock.now, leftover_n=0, completeness=UNIVERSE_COMPLETENESS_COMPLETE
    )
    assert coordinator._universe_generation_started_at is not None
    waiting = coordinator.plan_universe_tick(now=clock.now)
    assert waiting.lane == "idle"
    assert waiting.reason == "universe_retry_wait"
    assert coordinator._seconds_until_universe() >= 1.0

    clock.advance(3)
    due = coordinator.plan_universe_tick(now=clock.now)
    assert due.lane == ScanLane.UNIVERSE.value
    assert due.generation_resume is True
    assert "ok-a" in due.skip_event_ids
    assert "retry-b" not in due.skip_event_ids


def test_hot_remains_schedulable_during_universe_cooldown_and_retry_wait() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    live = _fixture("live", kickoff=NOW - timedelta(minutes=5), in_running=True)
    coordinator.record_report(_report([live], when=NOW), scan_lane=ScanLane.HOT)
    coordinator._next_hot_due = NOW

    coordinator.record_report(
        _report([_universe_fixture("done-a")], when=NOW, scan_lane=ScanLane.UNIVERSE.value).model_copy(
            update={"completed_at": NOW + timedelta(seconds=1)}
        ),
        scan_lane=ScanLane.UNIVERSE,
    )
    universe_idle = coordinator.plan_universe_tick(now=NOW)
    assert universe_idle.reason == "universe_cooldown"
    hot = coordinator.plan_hot_tick(now=NOW)
    assert hot.lane == ScanLane.HOT.value

    coordinator.record_universe_work_set(["retry-only"])
    coordinator.record_universe_fixture_progress(
        None,
        _universe_fixture("retry-only", evaluation="market_fetch_unavailable"),
        [],
        [],
    )
    coordinator._universe_in_progress = False
    retry_idle = coordinator.plan_universe_tick(now=clock.now)
    assert retry_idle.reason == "universe_retry_wait"
    coordinator._next_hot_due = clock.now
    hot_during_retry = coordinator.plan_hot_tick(now=clock.now)
    assert hot_during_retry.lane == ScanLane.HOT.value


def test_approved_pair_promotes_before_generation_completes() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator._mark_lane_started(ScanLane.UNIVERSE, NOW)
    report = _qualifying_universe_report()
    fixture = report.discovered_fixtures[0]
    markets = report.fixture_markets[fixture.canonical_event_id]
    coordinator.record_universe_work_set([fixture.canonical_event_id, OWNER_PENDING])
    coordinator.record_universe_fixture_progress(
        None, fixture, list(report.paper_decisions), markets
    )
    assert fixture.canonical_event_id in coordinator.fixture_current_state().hot_identity_scope(NOW)
    assert coordinator._universe_generation_started_at is not None
    assert coordinator._universe_sweep_is_complete_unlocked() is False
    assert coordinator._universe_in_progress is True
    coordinator._universe_in_progress = False
    nxt = coordinator.plan_universe_tick(now=NOW)
    assert nxt.lane == ScanLane.UNIVERSE.value
    assert nxt.generation_resume is True
    assert OWNER_PENDING not in nxt.skip_event_ids


def test_checkpoint_restore_keeps_running_and_retry_wait_safe(tmp_path: Path) -> None:
    store = SqliteUniverseCheckpointStore(tmp_path / "issue303.sqlite")
    first = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    first.configure_from_settings()
    first.record_universe_discovery_snapshot(
        {
            "matchbook": [{"id": 1, "name": "A vs B"}],
            "polymarket": [],
            "kalshi": [],
            "series_results": {
                "kalshi": [
                    {"series": "KXOK", "status": "ok", "retryable": False, "event_count": 1},
                    {
                        "series": "KXBAD",
                        "status": "discovery_timeout",
                        "retryable": True,
                        "event_count": 0,
                    },
                ]
            },
        }
    )
    first.record_universe_work_set(["done-a", "retry-b", "pending-c"])
    first.record_universe_fixture_progress(None, _universe_fixture("done-a"), [], [])
    first.record_universe_fixture_progress(
        None,
        _universe_fixture("retry-b", evaluation="market_fetch_unavailable"),
        [],
        [],
    )
    sweep_id = first._universe_sweep_id
    generation = first._universe_generation_id
    assert store.load() is not None

    restarted = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    restarted.configure_from_settings()
    assert restarted._universe_generation_id == generation
    assert restarted._universe_sweep_id == sweep_id
    assert restarted._universe_generation_started_at is not None
    assert restarted._universe_work["done-a"].state == "evaluated"
    assert restarted._universe_work["retry-b"].state == SWEEP_RETRY_WAIT
    assert restarted._universe_work["pending-c"].state == SWEEP_PENDING
    waiting = restarted.plan_universe_tick(now=NOW)
    assert waiting.generation_resume is True
    assert waiting.reuse_discovery is True
    assert waiting.reason in {None, "universe_sweep", "universe_retry_wait"}
    if waiting.lane == "idle":
        assert waiting.reason == "universe_retry_wait"
        later = restarted.plan_universe_tick(now=NOW + timedelta(seconds=3))
        assert later.lane == ScanLane.UNIVERSE.value
        assert later.universe_generation_id == generation
        assert "pending-c" not in later.skip_event_ids
    else:
        assert waiting.lane == ScanLane.UNIVERSE.value
        assert "pending-c" not in waiting.skip_event_ids
    assert restarted._universe_generation_started_at is not None
    assert store.load() is not None


@pytest.mark.asyncio
async def test_collector_resume_cursor_without_skip_does_not_empty_the_sweep() -> None:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=FakeMatchbook(),
        polymarket=FakePolymarket(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        costs = matchbook_polymarket_costs("0.02", "0.02")
        fx = [FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))]
        baseline = await collector.collect_and_scan(
            venue_costs=costs,
            fx_snapshots=fx,
            maximum_execution_risk=100,
            scan_lane=ScanLane.UNIVERSE.value,
            generation_resume=False,
            universe_generation_id=26,
        )
        ids = [item.canonical_event_id for item in baseline.discovered_fixtures]
        assert ids
        resumed = await collector.collect_and_scan(
            venue_costs=costs,
            fx_snapshots=fx,
            maximum_execution_risk=100,
            scan_lane=ScanLane.UNIVERSE.value,
            skip_event_ids=[],
            resume_cursor=ids[-1],
            generation_resume=True,
            universe_generation_id=26,
        )
        assert resumed.scan_diagnostics["skipped_by_resume_count"] == 0
        assert len(resumed.discovered_fixtures) == len(baseline.discovered_fixtures)
        assert resumed.scan_diagnostics["completeness"] == UNIVERSE_COMPLETENESS_COMPLETE
        skipped = await collector.collect_and_scan(
            venue_costs=costs,
            fx_snapshots=fx,
            maximum_execution_risk=100,
            scan_lane=ScanLane.UNIVERSE.value,
            skip_event_ids=ids,
            resume_cursor=ids[-1],
            generation_resume=True,
            universe_generation_id=26,
        )
        assert skipped.scan_diagnostics["skipped_by_resume_count"] >= 1
        assert skipped.discovered_fixtures == []
    finally:
        repository.close()


def test_universe_status_cadence_is_worker_cooldown_not_180s_cron() -> None:
    settings = Settings()
    assert settings.paper_universe_worker_cooldown_seconds == 8
    coordinator = LiveRefreshCoordinator()
    coordinator.configure_from_settings()
    assert coordinator.status.universe.cadence_seconds == 8
    assert coordinator.status.hot.cadence_seconds == 30
