from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic
from typing import Any

import pytest

from sports_hedge.application.collector import (
    CollectionReport,
    DiscoveredFixture,
    MarketEvaluationState,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.live_refresh import (
    LiveRefreshCoordinator,
    SCAN_CYCLE_PARTIAL_HARVEST_SECONDS,
    SCAN_CYCLE_RETURN_GRACE_SECONDS,
    ScanCycleTimeout,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from test_dual_cadence_scheduler import NOW, FakeClock, _fixture, _report
from test_read_only_collector import FakeMatchbook, FakePolymarket
from venue_cost_helpers import matchbook_polymarket_costs


class SlowCancellableMarketsMatchbook(FakeMatchbook):
    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        await asyncio.sleep(30)
        return {"markets": []}


class SlowCancellableMarketsPolymarket(FakePolymarket):
    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        await asyncio.sleep(30)
        return []


class StickyShortPolymarket(FakePolymarket):
    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(0.05)
            raise
        return []


def _hot_leftover_report(*, cancelled: bool = True) -> CollectionReport:
    completed = datetime.now(UTC)
    leftover = DiscoveredFixture(
        source=VenueName.MATCHBOOK,
        source_event_id="leeds-newcastle",
        canonical_event_id="hot-leeds-newcastle",
        home_team="Leeds United",
        away_team="Newcastle United",
        competition="Premier League",
        kickoff_utc=NOW,
        last_seen_at=completed,
        in_running=True,
        market_evaluation_state=MarketEvaluationState.NOT_EVALUATED_SCAN_DEADLINE.value,
        market_evaluation_reason="not_evaluated_scan_deadline",
        scan_lane=ScanLane.HOT.value,
    )
    return CollectionReport(
        started_at=NOW,
        completed_at=completed,
        discovered_fixtures=[leftover],
        operator_summary="partial (1 not evaluated)",
        scan_lane=ScanLane.HOT.value,
        scan_diagnostics={
            "cancelled": cancelled,
            "soft_deadline_reached": True,
            "provider_cancels": 1,
            "inflight_orphaned": 0,
            "provider_calls": 2,
        },
    )


@pytest.mark.asyncio
async def test_hot_swallowed_cancel_is_harvested_as_partial_not_last_error() -> None:
    """Owner-Windows shape: envelope cancel must not become scan_cycle_timeout after 30s."""

    coordinator = LiveRefreshCoordinator()
    coordinator.reset()

    async def runner() -> CollectionReport:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            return _hot_leftover_report(cancelled=True)
        raise AssertionError("coordinator must cancel the child runner")

    started = monotonic()
    report = await coordinator.run_cycle(
        runner,
        timeout_seconds=0.2,
        scan_lane=ScanLane.HOT,
    )
    elapsed = monotonic() - started
    assert elapsed <= 0.35
    assert elapsed < 1.0
    assert report.scan_diagnostics["cancelled"] is True
    assert coordinator.status.hot.last_error is None
    assert coordinator.status.last_error is None
    assert coordinator.status.hot.last_diagnostics is not None
    assert coordinator.status.hot.last_diagnostics["partial"] is True
    assert coordinator.status.hot.next_due_at is not None
    assert coordinator.status.hot.cycle_in_progress is False


@pytest.mark.asyncio
async def test_hung_runner_still_records_scan_cycle_timeout() -> None:
    coordinator = LiveRefreshCoordinator()
    coordinator.reset()

    async def hung() -> CollectionReport:
        await asyncio.sleep(30)
        raise AssertionError("hung runner must not complete")

    with pytest.raises(ScanCycleTimeout, match="scan_cycle_timeout after 0.2s"):
        await coordinator.run_cycle(hung, timeout_seconds=0.2, scan_lane=ScanLane.HOT)
    assert coordinator.status.hot.last_error is not None
    assert "timeout" in coordinator.status.hot.last_error


@pytest.mark.asyncio
async def test_hot_uncooperative_books_return_partial_before_envelope() -> None:
    cycle = 0.8
    envelope = cycle + 0.2
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=SlowCancellableMarketsMatchbook(),
        polymarket=StickyShortPolymarket(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        venue_timeout_seconds=0.2,
        provider_call_timeout_seconds=0.2,
        cycle_timeout_seconds=cycle,
    )
    coordinator = LiveRefreshCoordinator()
    coordinator.reset()
    try:
        async def runner() -> CollectionReport:
            return await collector.collect_and_scan(
                venue_costs=matchbook_polymarket_costs("0.02", "0.02"),
                fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"))],
                maximum_execution_risk=100,
                cycle_timeout_seconds=cycle,
                scan_lane=ScanLane.HOT.value,
            )

        started = monotonic()
        report = await coordinator.run_cycle(
            runner,
            timeout_seconds=envelope,
            scan_lane=ScanLane.HOT,
        )
        elapsed = monotonic() - started
        assert elapsed < envelope + 0.05
        assert coordinator.status.hot.last_error is None
        assert coordinator.status.last_error is None
        assert report.discovered_fixtures
        leftovers = [
            item
            for item in report.discovered_fixtures
            if item.market_evaluation_state
            == MarketEvaluationState.NOT_EVALUATED_SCAN_DEADLINE.value
        ]
        assert leftovers or report.scan_diagnostics.get("soft_deadline_reached")
        assert report.scan_diagnostics["inflight_live"] == 0
        assert report.scan_diagnostics["provider_cancels"] >= 1
        assert "partial" in (coordinator.status.hot.operator_summary or report.operator_summary)
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_repeated_slow_hot_cycles_do_not_accumulate_tasks() -> None:
    cycle = 0.5
    envelope = cycle + 0.2
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=SlowCancellableMarketsMatchbook(),
        polymarket=SlowCancellableMarketsPolymarket(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        venue_timeout_seconds=0.15,
        provider_call_timeout_seconds=0.15,
        cycle_timeout_seconds=cycle,
    )
    coordinator = LiveRefreshCoordinator()
    coordinator.reset()
    live_before = {id(task) for task in asyncio.all_tasks()}

    async def runner() -> CollectionReport:
        return await collector.collect_and_scan(
            venue_costs=matchbook_polymarket_costs("0.02", "0.02"),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"))],
            maximum_execution_risk=100,
            cycle_timeout_seconds=cycle,
            scan_lane=ScanLane.HOT.value,
        )

    try:
        for _ in range(5):
            report = await coordinator.run_cycle(
                runner,
                timeout_seconds=envelope,
                scan_lane=ScanLane.HOT,
            )
            assert coordinator.status.hot.last_error is None
            assert report.scan_diagnostics["inflight_live"] == 0
        await asyncio.sleep(0.05)
        leftover = [
            task
            for task in asyncio.all_tasks()
            if id(task) not in live_before and not task.done()
        ]
        assert leftover == [], leftover
        assert coordinator.status.hot.next_due_at is not None
        assert coordinator.status.hot.cycle_in_progress is False
        assert coordinator.status.hot.last_diagnostics is not None
        assert coordinator.status.hot.last_diagnostics.get("inflight_live") == 0
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_collect_cancel_uncancels_so_aclose_can_run() -> None:
    """Python 3.11+ must uncancel after leftover assembly or HTTP aclose re-raises."""

    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=SlowCancellableMarketsMatchbook(),
        polymarket=SlowCancellableMarketsPolymarket(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        venue_timeout_seconds=0.2,
        provider_call_timeout_seconds=0.2,
        cycle_timeout_seconds=8.0,
    )
    try:
        task = asyncio.create_task(
            collector.collect_and_scan(
                venue_costs=matchbook_polymarket_costs("0.02", "0.02"),
                fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"))],
                maximum_execution_risk=100,
                cycle_timeout_seconds=8.0,
                scan_lane=ScanLane.HOT.value,
            )
        )
        await asyncio.sleep(0.05)
        task.cancel()
        report = await task
        assert task.cancelled() is False
        assert report.scan_diagnostics["cancelled"] is True
        assert report.scan_diagnostics["inflight_live"] == 0
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_universe_resumes_after_slow_hot_partial() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator.reset()
    coordinator._clock = clock
    live = _fixture("live", kickoff=NOW - timedelta(minutes=1), in_running=True)
    far_a = _fixture("a", kickoff=NOW + timedelta(days=2), evaluation="evaluated")
    far_b = _fixture(
        "b",
        kickoff=NOW + timedelta(days=3),
        evaluation="not_evaluated_scan_deadline",
    )
    coordinator.record_report(_report([far_a, far_b], when=NOW), scan_lane=ScanLane.UNIVERSE)
    coordinator.record_report(_report([live], when=NOW), scan_lane=ScanLane.HOT)
    coordinator._next_hot_due = NOW
    coordinator._next_universe_due = NOW
    coordinator._universe_generation_started_at = NOW
    coordinator._universe_work_used = 8.0
    coordinator._universe_evaluated_ids = {"a"}
    coordinator._universe_cursor = "a"

    async def hot_runner() -> CollectionReport:
        await asyncio.sleep(0.15)
        return _hot_leftover_report(cancelled=False)

    plan = coordinator.plan_tick(now=clock.now)
    assert plan.lane == "hot"
    report = await coordinator.run_cycle(
        hot_runner,
        timeout_seconds=0.5,
        scan_lane=ScanLane.HOT,
    )
    assert coordinator.status.hot.last_error is None
    assert report.discovered_fixtures
    clock.advance(1)
    resumed = coordinator.plan_tick(now=clock.now)
    assert resumed.lane == "universe"
    assert resumed.resume_cursor == "a"
    assert "a" in resumed.skip_event_ids
    assert coordinator._universe_work_used == 8.0


def test_hot_envelope_defaults_remain_25s_collector_and_30s_coordinator() -> None:
    settings = Settings()
    assert settings.paper_scan_hot_cycle_timeout_seconds == 25
    assert SCAN_CYCLE_RETURN_GRACE_SECONDS == 5.0
    assert settings.paper_scan_hot_cycle_timeout_seconds + SCAN_CYCLE_RETURN_GRACE_SECONDS == 30
    assert SCAN_CYCLE_PARTIAL_HARVEST_SECONDS < SCAN_CYCLE_RETURN_GRACE_SECONDS
    assert settings.paper_scan_cycle_timeout_seconds == 45
    import sports_hedge.application.live_refresh as live_refresh
    from sports_hedge.api import paper as paper_api

    assert "asyncio.wait_for" not in inspect.getsource(LiveRefreshCoordinator.run_cycle)
    assert "create_task" in inspect.getsource(live_refresh._await_collection_runner)
    assert "_collect_report(" in inspect.getsource(paper_api.server_owned_refresh_tick)
    persist_idx = inspect.getsource(paper_api.server_owned_refresh_tick).index(
        "_persist_collection_report"
    )
    run_idx = inspect.getsource(paper_api.server_owned_refresh_tick).index("run_cycle")
    assert persist_idx > run_idx
