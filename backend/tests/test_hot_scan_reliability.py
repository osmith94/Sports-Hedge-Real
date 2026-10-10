from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Any

import pytest

from sports_hedge.application.collector import (
    DIAGNOSTIC_PROVIDERS,
    DIAGNOSTIC_STAGES,
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
from sports_hedge.application.hot_market_relationships import (
    HotMarketRelationship,
    HotVenueLeg,
    relationships_from_fixture_markets,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.paper.trades import PaperTradeState
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from test_dual_cadence_scheduler import NOW, FakeClock, _fixture, _report
from test_read_only_collector import FakeMatchbook, FakePolymarket, KICKOFF
from registered_kalshi import FakeKalshiBTTS
from venue_cost_helpers import matchbook_kalshi_costs, matchbook_polymarket_costs
from test_step8f_automatic_paper_entry import (
    FX as AUTOFILL_FX,
    _matchbook_btts,
    _ops_bundle,
    _kalshi_btts,
    _kalshi_costs,
    _standing,
)


def _hung_hot_relationships() -> dict[str, list[HotMarketRelationship]]:
    relationship = HotMarketRelationship(
        canonical_event_id="hung-newcastle-chelsea",
        market_key="both_teams_to_score|full_time|",
        family="both_teams_to_score",
        period="full_time",
        matchbook=HotVenueLeg(
            venue=VenueName.MATCHBOOK,
            source_event_id="1001",
            source_market_id="2001",
            family="both_teams_to_score",
            period="full_time",
        ),
        polymarket=HotVenueLeg(
            venue=VenueName.POLYMARKET,
            source_event_id="pm-event-1",
            source_market_id="pm-market-1",
            source_runner_ids=["yes-token", "no-token"],
            family="both_teams_to_score",
            period="full_time",
        ),
    )
    return {relationship.canonical_event_id: [relationship]}


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

    async def get_order_book(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        await asyncio.sleep(30)
        return {"asset_id": "x", "bids": [], "asks": []}


class CloseTerminatedPolymarket(FakePolymarket):
    """Ignores CancelledError until aclose(), matching a stuck HTTP client."""

    def __init__(self) -> None:
        super().__init__()
        self._closed = asyncio.Event()
        self.live_calls = 0

    def reopen(self) -> None:
        self._closed = asyncio.Event()

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        self.live_calls += 1
        try:
            while not self._closed.is_set():
                try:
                    await asyncio.wait_for(self._closed.wait(), timeout=0.05)
                except TimeoutError:
                    continue
                except asyncio.CancelledError:
                    continue
            return []
        finally:
            self.live_calls -= 1

    async def get_order_book(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        self.live_calls += 1
        try:
            while not self._closed.is_set():
                try:
                    await asyncio.wait_for(self._closed.wait(), timeout=0.05)
                except TimeoutError:
                    continue
                except asyncio.CancelledError:
                    continue
            return {"asset_id": "x", "bids": [], "asks": []}
        finally:
            self.live_calls -= 1

    async def aclose(self) -> None:
        self._closed.set()


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
        polymarket=CloseTerminatedPolymarket(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        venue_timeout_seconds=0.2,
        provider_call_timeout_seconds=0.2,
        cycle_timeout_seconds=cycle,
    )
    coordinator = LiveRefreshCoordinator()
    coordinator.reset()
    try:
        async def runner() -> CollectionReport:
            try:
                return await collector.collect_and_scan(
                    venue_costs=matchbook_polymarket_costs("0.02", "0.02"),
                    fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"))],
                    maximum_execution_risk=100,
                    cycle_timeout_seconds=cycle,
                    scan_lane=ScanLane.HOT.value,
                    hot_market_relationships=_hung_hot_relationships(),
                )
            finally:
                await collector.polymarket.aclose()

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
        # Uncooperative books stay in inflight until the underlying call
        # finishes. aclose() below is what actually terminates them.
        assert report.scan_diagnostics["provider_cancels"] >= 1
        assert report.scan_diagnostics["cancel_count"] >= 1
        assert report.scan_diagnostics["inflight_orphaned"] >= 1
        assert report.scan_diagnostics["inflight_live"] == report.scan_diagnostics["inflight_orphaned"]
        assert report.scan_diagnostics["cancel_count"] >= report.scan_diagnostics["inflight_orphaned"]
        assert "partial" in (coordinator.status.hot.operator_summary or report.operator_summary)
        await asyncio.sleep(0.08)
        assert collector.polymarket.live_calls == 0
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_repeated_slow_hot_cycles_do_not_accumulate_tasks() -> None:
    cycle = 0.5
    envelope = cycle + 0.2
    repository = SqliteMarketIntelligenceRepository()
    polymarket = CloseTerminatedPolymarket()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=SlowCancellableMarketsMatchbook(),
        polymarket=polymarket,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        venue_timeout_seconds=0.15,
        provider_call_timeout_seconds=0.15,
        cycle_timeout_seconds=cycle,
    )
    coordinator = LiveRefreshCoordinator()
    coordinator.reset()
    live_before = {id(task) for task in asyncio.all_tasks()}

    async def runner() -> CollectionReport:
        polymarket.reopen()
        try:
            return await collector.collect_and_scan(
                venue_costs=matchbook_polymarket_costs("0.02", "0.02"),
                fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"))],
                maximum_execution_risk=100,
                cycle_timeout_seconds=cycle,
                scan_lane=ScanLane.HOT.value,
                hot_market_relationships=_hung_hot_relationships(),
            )
        finally:
            await polymarket.aclose()

    try:
        for _ in range(5):
            report = await coordinator.run_cycle(
                runner,
                timeout_seconds=envelope,
                scan_lane=ScanLane.HOT,
            )
            assert coordinator.status.hot.last_error is None
            assert report.scan_diagnostics["cancel_count"] >= 1
            assert report.scan_diagnostics["inflight_orphaned"] >= 1
            assert (
                report.scan_diagnostics["inflight_live"]
                == report.scan_diagnostics["inflight_orphaned"]
            )
            await asyncio.sleep(0.08)
            assert polymarket.live_calls == 0
        leftover = [
            task
            for task in asyncio.all_tasks()
            if id(task) not in live_before and not task.done()
        ]
        assert leftover == [], leftover
        assert coordinator.status.hot.next_due_at is not None
        assert coordinator.status.hot.cycle_in_progress is False
        assert coordinator.status.hot.last_diagnostics is not None
        assert coordinator.status.hot.last_diagnostics.get("inflight_orphaned", 0) >= 1
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
                hot_market_relationships=_hung_hot_relationships(),
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
    tick_src = inspect.getsource(paper_api.server_owned_refresh_tick)
    assert tick_src.index("persist_scheduled_collection_report") > tick_src.index("run_cycle")


@pytest.mark.asyncio
async def test_wait_for_returns_swallowed_cancel_and_old_path_times_out_in_persist() -> None:
    """Pre-fix: swallowed leftover is not TimeoutError; persist inside wait_for is."""

    async def swallowed() -> str:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            return "partial"
        raise AssertionError("must be cancelled")

    assert await asyncio.wait_for(swallowed(), timeout=0.1) == "partial"

    phases: dict[str, Any] = {}

    async def old_end_to_end_runner() -> CollectionReport:
        started = monotonic()
        phases["phase"] = "collect"
        report = _hot_leftover_report(cancelled=False)
        phases["collect_s"] = monotonic() - started
        phases["phase"] = "persist"
        persist_started = monotonic()
        await asyncio.sleep(0.45)
        phases["persist_s"] = monotonic() - persist_started
        phases["phase"] = "aclose"
        await asyncio.sleep(0.05)
        phases["aclose_done"] = True
        return report

    envelope = 0.3
    with pytest.raises(TimeoutError):
        await asyncio.wait_for(old_end_to_end_runner(), timeout=envelope)
    assert phases["collect_s"] < envelope
    assert phases["phase"] == "persist"
    assert "persist_s" not in phases
    assert phases["collect_s"] + 0.45 > envelope

    leftover_phases: dict[str, Any] = {}

    async def leftover_then_persist_still_runs() -> CollectionReport:
        try:
            await asyncio.sleep(5)
        except asyncio.CancelledError:
            task = asyncio.current_task()
            leftover_phases["cancelling"] = 0 if task is None else task.cancelling()
            report = _hot_leftover_report(cancelled=True)
        persist_started = monotonic()
        await asyncio.sleep(0.25)
        leftover_phases["persist_after_timeout_s"] = monotonic() - persist_started
        return report

    started = monotonic()
    leftover_report = await asyncio.wait_for(leftover_then_persist_still_runs(), timeout=0.05)
    leftover_elapsed = monotonic() - started
    assert leftover_report.scan_diagnostics["cancelled"] is True
    assert leftover_phases["cancelling"] >= 1
    assert leftover_phases["persist_after_timeout_s"] >= 0.2
    assert leftover_elapsed >= 0.2
    assert leftover_elapsed > 0.05

    coordinator = LiveRefreshCoordinator()
    coordinator.reset()

    async def collect_only() -> CollectionReport:
        await asyncio.sleep(0.05)
        return _hot_leftover_report(cancelled=False)

    report = await coordinator.run_cycle(
        collect_only, timeout_seconds=envelope, scan_lane=ScanLane.HOT
    )
    persist_started = monotonic()
    await asyncio.sleep(0.45)
    persist_s = monotonic() - persist_started
    assert persist_s >= 0.4
    assert coordinator.status.hot.last_error is None
    assert report.discovered_fixtures


@pytest.mark.asyncio
async def test_hot_diagnostics_include_provider_and_stage_attribution() -> None:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=FakeMatchbook(),
        polymarket=FakePolymarket(),
        kalshi=FakeKalshiBTTS([("Newcastle United", "Chelsea", KICKOFF)]),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        venue_timeout_seconds=0.4,
        provider_call_timeout_seconds=0.4,
        cycle_timeout_seconds=8.0,
    )
    try:
        universe = await collector.collect_and_scan(
            enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
            venue_costs=matchbook_kalshi_costs("0"),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"))],
            maximum_execution_risk=100,
        )
        report = await collector.collect_and_scan(
            enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
            venue_costs=matchbook_kalshi_costs("0"),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"))],
            maximum_execution_risk=100,
            scan_lane=ScanLane.HOT.value,
            identity_scope=[item.canonical_event_id for item in universe.discovered_fixtures],
            known_source_events=universe.fixture_source_events,
            hot_market_relationships=relationships_from_fixture_markets(universe.fixture_markets),
        )
        diagnostics = report.scan_diagnostics
        for venue in DIAGNOSTIC_PROVIDERS:
            assert venue in diagnostics["providers"]
            assert {"calls", "elapsed_ms", "timeouts", "cancels"} <= set(
                diagnostics["providers"][venue]
            )
        for stage in DIAGNOSTIC_STAGES:
            assert stage in diagnostics["stages"]
            assert {"calls", "elapsed_ms", "timeouts", "cancels"} <= set(
                diagnostics["stages"][stage]
            )
        assert diagnostics["providers"]["matchbook"]["calls"] >= 1
        assert diagnostics["providers"]["kalshi"]["calls"] >= 1
        assert diagnostics["providers"]["polymarket"]["calls"] == 0
        assert diagnostics["stages"]["event_lookup"]["elapsed_ms"] >= 0
        assert diagnostics["stages"]["market_discovery"]["calls"] >= 1
        assert diagnostics["stages"]["book_depth"]["calls"] >= 0
        assert diagnostics["stages"]["mapping_equivalence"]["calls"] >= 1
        assert diagnostics["stages"]["fees_fx_risk"]["calls"] >= 0
        assert diagnostics["stages"]["solver_allocation"]["calls"] >= 0
        assert diagnostics["stages"]["current_state_finalization"]["calls"] >= 1
        assert diagnostics["evaluated_count"] + diagnostics["not_evaluated_count"] >= 1
        assert "timeout_count" in diagnostics
        assert "cancel_count" in diagnostics
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_cooperative_cancel_is_not_counted_as_orphan() -> None:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=SlowCancellableMarketsMatchbook(),
        polymarket=SlowCancellableMarketsPolymarket(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        venue_timeout_seconds=0.15,
        provider_call_timeout_seconds=0.15,
        cycle_timeout_seconds=0.5,
    )
    try:
        report = await collector.collect_and_scan(
            venue_costs=matchbook_polymarket_costs("0.02", "0.02"),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"))],
            maximum_execution_risk=100,
            cycle_timeout_seconds=0.5,
            scan_lane=ScanLane.HOT.value,
            hot_market_relationships=_hung_hot_relationships(),
        )
        assert report.scan_diagnostics["cancel_count"] >= 1
        assert report.scan_diagnostics["inflight_orphaned"] == 0
        assert report.scan_diagnostics["inflight_live"] == 0
        assert report.scan_diagnostics["cancel_count"] == report.scan_diagnostics["provider_cancels"]
    finally:
        repository.close()


def _native_lock_facts(ledger: SqlitePaperLedger, trade_id: str) -> list[tuple[str, ...]]:
    with ledger.exclusive():
        rows = ledger._connection.execute(
            """
            SELECT venue, native_currency, locked_native, COALESCE(fill_id, lock_id)
            FROM paper_treasury_locks
            WHERE trade_id = ?
            ORDER BY venue, native_currency, lock_id
            """,
            (trade_id,),
        ).fetchall()
        return [(row[0], row[1], str(row[2]), str(row[3])) for row in rows]


def _journal_source_facts(ledger: SqlitePaperLedger, opportunity_id: str) -> list[tuple[str, str]]:
    entries = ledger.journal.list_entries(opportunity_id=opportunity_id)
    return sorted((entry.source, entry.source_id) for entry in entries)


def _treasury_event_facts(ledger: SqlitePaperLedger, trade_id: str) -> list[tuple[str, str]]:
    with ledger.exclusive():
        rows = ledger._connection.execute(
            """
            SELECT source, source_id FROM paper_treasury_events
            WHERE trade_id = ?
            ORDER BY source, source_id
            """,
            (trade_id,),
        ).fetchall()
        return [(row[0], row[1]) for row in rows]


async def test_scheduled_persist_failure_is_visible_without_scan_timeout(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Production persist: fail after a real OPEN, retry without double lock/journal."""

    from sports_hedge.api import paper as paper_api

    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    audit = SqlitePaperScanRepository(tmp_path / "paper-audit.sqlite")
    try:
        decision = scan.scan_pair(
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
            fx_snapshots=AUTOFILL_FX,
            maximum_execution_risk=100,
            liquidity_snapshot=_standing(),
        )
        assert decision.eligible_for_paper_simulation is True, decision.rejection_reasons
        assert decision.canonical_market_id
        assert decision.allocation is not None and decision.allocation.accepted

        coordinator = LiveRefreshCoordinator()
        coordinator.reset()
        report = _hot_leftover_report(cancelled=False).model_copy(
            update={"paper_decisions": [decision]}
        )
        coordinator.record_report(report, scan_lane=ScanLane.HOT)
        assert coordinator.status.hot.last_error is None

        def operations_factory(watchlist_arg=None, alerts=None):
            del alerts
            if watchlist_arg is not None:
                ops.watchlist = watchlist_arg
            return ops

        monkeypatch.setattr(paper_api, "get_paper_operations_service", operations_factory)
        real_persist = paper_api._persist_decision
        persist_calls = {"n": 0}

        def persist_after_open_then_fail_once(decision_arg: Any, **kwargs: Any) -> None:
            real_persist(decision_arg, **kwargs)
            persist_calls["n"] += 1
            if persist_calls["n"] == 1:
                raise RuntimeError("audit_write_failed")

        monkeypatch.setattr(paper_api, "_persist_decision", persist_after_open_then_fail_once)

        await paper_api.persist_scheduled_collection_report(
            coordinator,
            report,
            service=scan,
            audit=audit,
            watchlist=watchlist,
            scan_lane=ScanLane.HOT,
        )
        active = ops.list_active_trades()
        assert len(active) == 1
        trade = active[0]
        assert trade.state is PaperTradeState.OPEN
        assert trade.paper_only is True
        assert trade.places_orders is False
        filled_legs = [leg for leg in trade.legs if leg.filled_stake > 0]
        assert filled_legs
        lock_facts = _native_lock_facts(ledger, trade.trade_id)
        assert len(lock_facts) == len(filled_legs)
        journal_facts = _journal_source_facts(ledger, trade.opportunity_id)
        event_facts = _treasury_event_facts(ledger, trade.trade_id)
        assert journal_facts
        assert len(journal_facts) == len(set(journal_facts))
        assert len(event_facts) == len(set(event_facts))
        snapshot = ledger.treasury.snapshot()
        locked_after_open = {
            (pool.venue.value, pool.native_currency): pool.locked_capital
            for pool in snapshot.pools
            if pool.locked_capital > 0
        }
        assert locked_after_open
        assert coordinator.status.hot.last_error is None
        assert coordinator.status.last_error is None
        assert coordinator.status.hot.persist_ok is False
        assert coordinator.status.hot.last_persist_error == "audit_write_failed"
        assert coordinator.status.hot.degraded is True
        assert "persist/auto-capture failed" in (coordinator.status.hot.operator_summary or "")
        assert coordinator.status.hot.last_diagnostics is not None
        assert coordinator.status.hot.last_diagnostics["stages"]["persistence"]["ok"] is False

        await paper_api.persist_scheduled_collection_report(
            coordinator,
            report,
            service=scan,
            audit=audit,
            watchlist=watchlist,
            scan_lane=ScanLane.HOT,
        )
        retried = ops.list_active_trades()
        assert len(retried) == 1
        assert retried[0].trade_id == trade.trade_id
        assert retried[0].state is PaperTradeState.OPEN
        assert persist_calls["n"] == 2
        assert _native_lock_facts(ledger, trade.trade_id) == lock_facts
        retry_journal = _journal_source_facts(ledger, trade.opportunity_id)
        retry_events = _treasury_event_facts(ledger, trade.trade_id)
        assert retry_journal == journal_facts
        assert retry_events == event_facts
        retry_snapshot = ledger.treasury.snapshot()
        locked_after_retry = {
            (pool.venue.value, pool.native_currency): pool.locked_capital
            for pool in retry_snapshot.pools
            if pool.locked_capital > 0
        }
        assert locked_after_retry == locked_after_open
        assert coordinator.status.hot.last_error is None
        assert coordinator.status.hot.persist_ok is True
        assert coordinator.status.hot.last_persist_error is None
        assert coordinator.status.hot.last_diagnostics["stages"]["persistence"]["ok"] is True
        assert ledger.reconcile().ok
    finally:
        repository.close()
        ledger.close()
