"""#572 Windows event-loop stalls: UNIVERSE market_evaluation, scheduler, settlement.

Owner-live Windows evidence: ``GET /health`` timed out after 5s while the
phase clock reported ``universe/market_evaluation`` 46.9s (events=275),
``hot/scheduler`` 12.7s, ``background/scheduler`` 13.7s and
``active_trade/paper_settlement`` 7.5s / 6.6s.

Proven root causes:

1. ``record_universe_fixture_progress`` (the UNIVERSE ``on_fixture_evaluated``
   callback) rebuilt the whole current-state store three times per streamed
   fixture: two ``membership_counts`` plus one ``inventory``, each re-projecting
   every stored fixture's market rows. O(N) per fixture, O(N^2) per sweep, on
   the event loop inside ``market_evaluation``.
2. Lane classification (eviction, HOT scope, membership) stamped every market
   row of every fixture although it only reads status / kickoff / in-running.
   ``plan_hot_tick`` paid that on the loop every HOT wake.
3. ``public_status`` runs in the API threadpool and held the coordinator
   ``_state_lock`` across SQLite reads. Every scheduler lane and paper
   settlement take that lock on the event loop.
4. Paper settlement read and wrote the RLock-serialized paper ledger (5s SQLite
   busy timeout) on the event loop thread, so an API handler or accounting job
   holding the ledger stalled every coroutine.

The phase clock also reported await time: ``market_evaluation`` stayed open
across every awaited provider call, so 46.9s was stage wall time, not one
synchronous slice. ``CallbackProfiler`` measures the real slices.

Data class: synthetic fixture/demo providers. Not live, historical, or modelled
venue quotes. PAPER / read-only. Provider capacities, the 8s provider timeout,
EventMatcher thresholds and the Approved Match Register are unchanged.
"""

from __future__ import annotations

import asyncio
import gc
import itertools
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from loop_liveness_harness import (
    CallbackProfiler,
    HeartbeatProbe,
    RealisticUniverse,
    SchedulingGap,
    SlowCallback,
    extreme_stress_liveness_failures,
    extreme_stress_summary,
    probe_gc_attributed_gaps,
)

from sports_hedge.application import fixture_current_state as current_state_module
from sports_hedge.application.collector import (
    DEFAULT_PROVIDER_CONCURRENCY,
    CollectionReport,
    MarketEvaluationState,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.event_loop_activity import (
    LOOP_ACTIVITY,
    TimedRLock,
    loop_subphase_snapshot,
    reset_loop_activity,
)
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore, _FixtureRecord
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.paper_settlement_agent import PaperSettlementAgent
from sports_hedge.application.provider_access import reset_shared_provider_access
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.persistence.lane_venue_settings import SqliteLaneVenueSettingsStore
from sports_hedge.persistence.operator_scanner_settings import SqliteOperatorScannerSettingsStore
from sports_hedge.persistence.operator_universe_scope import SqliteOperatorUniverseScopeStore
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.persistence.universe_checkpoint import SqliteUniverseCheckpointStore

LIVENESS_BOUND_S = 0.25
REPRESENTATIVE_FIXTURES = 275
ALL_VENUES = [VenueName.MATCHBOOK, VenueName.KALSHI, VenueName.POLYMARKET]


@pytest.fixture(autouse=True)
def _isolated_loop_state():
    reset_shared_provider_access()
    reset_loop_activity()
    yield
    reset_shared_provider_access()
    reset_loop_activity()


def _coordinator() -> LiveRefreshCoordinator:
    return LiveRefreshCoordinator(
        operator_settings_store=SqliteOperatorScannerSettingsStore(":memory:"),
        venue_settings_store=SqliteLaneVenueSettingsStore(":memory:"),
        universe_scope_store=SqliteOperatorUniverseScopeStore(":memory:"),
        universe_checkpoint_store=SqliteUniverseCheckpointStore(":memory:"),
    )


def _collector(universe: RealisticUniverse) -> tuple[ReadOnlyCrossVenueCollector, SqliteMarketIntelligenceRepository]:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=universe.matchbook(),
        polymarket=universe.polymarket(),
        kalshi=universe.kalshi(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    return collector, repository


async def _universe_sweep(
    collector: ReadOnlyCrossVenueCollector,
    *,
    callbacks: tuple | None = None,
) -> CollectionReport:
    on_discovery, on_fixture, on_work = callbacks or (None, None, None)
    return await collector.collect_and_scan(
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
        maximum_execution_risk=100,
        max_event_pairs=10_000_000,
        scan_lane=ScanLane.UNIVERSE.value,
        universe_generation_id=1,
        unbounded_cycle=True,
        enabled_venues=ALL_VENUES,
        on_discovery_complete=on_discovery,
        on_fixture_evaluated=on_fixture,
        on_canonical_work_set=on_work,
    )


def _scanner_signature(report: CollectionReport) -> list[tuple]:
    """Scanner outputs that identity/equivalence changes would move."""

    rows = []
    for fixture in sorted(report.discovered_fixtures, key=lambda item: item.canonical_event_id):
        markets = report.fixture_markets.get(fixture.canonical_event_id, [])
        rows.append(
            (
                fixture.canonical_event_id,
                fixture.market_evaluation_state,
                fixture.matched_market_count,
                fixture.matched_equivalent_count,
                tuple(
                    sorted(
                        (
                            str(row.comparison_status),
                            str(getattr(row, "canonical_market_key", "") or ""),
                        )
                        for row in markets
                    )
                ),
            )
        )
    return rows


def test_contract_constants_unchanged() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    assert Settings.model_fields["paper_scan_provider_timeout_seconds"].default == 8
    assert Settings.model_fields["paper_scan_cycle_timeout_seconds"].default == 45
    assert DEFAULT_PROVIDER_CONCURRENCY == {
        VenueName.MATCHBOOK: 4,
        VenueName.POLYMARKET: 8,
        VenueName.KALSHI: 4,
    }


@pytest.mark.asyncio
async def test_universe_fixture_publication_does_not_reproject_the_whole_store(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Deterministic O(N^2) guard: streaming one fixture must not re-project every row.

    Before #572 each streamed fixture re-projected every stored fixture six
    times (two membership passes and one inventory, each with an eviction
    pass): ~6 * N^2 / 2 projections per sweep. Only HOT rows need the market
    projection now (HOT sort / dedup / qualifying flag).
    """

    fixture_count = 60
    hot_every = 10
    hot_fixtures = len(range(0, fixture_count, hot_every))
    universe = RealisticUniverse(fixture_count, latency_s=0, hot_every=hot_every)
    collector, repository = _collector(universe)
    coordinator = _coordinator()
    projections = {"inside_publication": 0, "publishing": False}
    original = current_state_module.apply_current_market_inventory

    def counting(*args, **kwargs):
        if projections["publishing"]:
            projections["inside_publication"] += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(current_state_module, "apply_current_market_inventory", counting)
    on_discovery, on_fixture, on_work = coordinator.universe_collect_callbacks()
    published: list[int] = []

    def counted_on_fixture(cluster, fixture, decisions, inventory) -> None:
        projections["publishing"] = True
        try:
            on_fixture(cluster, fixture, decisions, inventory)
        finally:
            projections["publishing"] = False
        published.append(len(coordinator._fixture_state._rows))

    try:
        report = await _universe_sweep(
            collector, callbacks=(on_discovery, counted_on_fixture, on_work)
        )
    finally:
        repository.close()

    assert len(report.discovered_fixtures) == fixture_count
    assert len(published) == fixture_count
    # Immediate publication: the store grows with every streamed fixture.
    assert published == sorted(published)
    assert published[-1] == fixture_count
    assert published[0] < published[-1]
    budget = 2 * hot_fixtures * fixture_count
    assert projections["inside_publication"] <= budget, (
        f"{projections['inside_publication']} market projections while streaming "
        f"{fixture_count} fixtures (budget {budget}); publication is O(N) per fixture again"
    )
    assert coordinator._universe_hot_promotions == hot_fixtures


def _mixed_store_report(now: datetime) -> CollectionReport:
    from test_dual_cadence_scheduler import _fixture, _report

    fixtures = [
        _fixture("572-near-kickoff", kickoff=now + timedelta(minutes=20), last_seen=now),
        _fixture(
            "572-in-running",
            kickoff=now - timedelta(minutes=30),
            in_running=True,
            last_seen=now,
        ),
        _fixture(
            "572-terminal",
            kickoff=now - timedelta(minutes=100),
            fixture_status="graded",
            last_seen=now,
        ),
        _fixture("572-too-old", kickoff=now - timedelta(hours=5), last_seen=now),
        _fixture(
            "572-postponed",
            kickoff=now - timedelta(hours=1),
            fixture_status="postponed",
            last_seen=now,
        ),
        _fixture("572-distant", kickoff=now + timedelta(days=3), last_seen=now),
    ]
    for index, fixture in enumerate(fixtures):
        fixtures[index] = fixture.model_copy(
            update={"home_team": f"Home572 {index}", "away_team": f"Away572 {index}"}
        )
    return _report(fixtures, when=now)


async def _parity_store(now: datetime, universe_report: CollectionReport) -> FixtureCurrentStateStore:
    from test_issue200_universe_hot_promotion import _qualifying_universe_report

    store = FixtureCurrentStateStore()
    store.upsert_from_report(universe_report, scan_lane=ScanLane.UNIVERSE, now=now)
    store.upsert_from_report(_mixed_store_report(now), scan_lane=ScanLane.UNIVERSE, now=now)
    store.upsert_from_report(
        _qualifying_universe_report("572-qualifying", kickoff=now + timedelta(days=4), when=now),
        scan_lane=ScanLane.UNIVERSE,
        now=now,
    )
    return store


def _store_outputs(store: FixtureCurrentStateStore, now: datetime) -> dict[str, object]:
    return {
        "hot_scope": store.hot_identity_scope(now),
        "membership": store.membership_counts(now),
        "breakdown": store.hot_membership_breakdown(now),
        "inventory": [item.model_dump(mode="json") for item in store.inventory(now)],
        "rows": sorted(store._rows),
        "tombstones": sorted(store._tombstones),
    }


@pytest.mark.asyncio
async def test_lifecycle_classification_matches_full_projection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Classifying from the unprojected fixture must give identical store outputs."""

    universe = RealisticUniverse(45, latency_s=0, hot_every=9)
    collector, repository = _collector(universe)
    try:
        universe_report = await _universe_sweep(collector)
    finally:
        repository.close()
    now = datetime.now(UTC)

    optimized = _store_outputs(await _parity_store(now, universe_report), now)
    with monkeypatch.context() as patch:
        # Reference: the pre-#572 classification input (full market projection).
        patch.setattr(
            _FixtureRecord,
            "lifecycle_fixture",
            lambda self: self.status_fixture(now),
        )
        reference = _store_outputs(await _parity_store(now, universe_report), now)

    assert optimized == reference
    assert "572-near-kickoff" in optimized["hot_scope"]
    assert "572-in-running" in optimized["hot_scope"]
    assert "572-qualifying" in optimized["hot_scope"]
    assert "572-terminal" in optimized["tombstones"]
    assert "572-too-old" not in optimized["rows"]
    assert "572-postponed" in optimized["rows"]
    assert optimized["breakdown"][2] >= 1


@pytest.mark.asyncio
async def test_single_pass_hot_counting_matches_two_membership_passes() -> None:
    """Publication's exact HOT promotion counting, one classification pass per fixture."""

    from test_issue200_universe_hot_promotion import _qualifying_universe_report

    universe = RealisticUniverse(40, latency_s=0, hot_every=5)
    collector, repository = _collector(universe)
    try:
        report = await _universe_sweep(collector)
    finally:
        repository.close()
    base = datetime.now(UTC)
    qualifying = _qualifying_universe_report(
        "572-qualifying", kickoff=base + timedelta(days=4), when=base
    )
    sequence = [
        (fixture, report.fixture_markets.get(fixture.canonical_event_id, []))
        for fixture in report.discovered_fixtures
    ]
    sequence += [(fixture, []) for fixture in _mixed_store_report(base).discovered_fixtures]
    sequence += [
        (fixture, qualifying.fixture_markets.get(fixture.canonical_event_id, []))
        for fixture in qualifying.discovered_fixtures
    ]
    first, first_markets = sequence[0]
    sequence.append((first, first_markets))
    sequence.append(
        (first.model_copy(update={"canonical_event_id": f"{first.canonical_event_id}-alias"}), [])
    )

    counting = FixtureCurrentStateStore()
    reference = FixtureCurrentStateStore()
    promotions = 0
    for step, (fixture, markets) in enumerate(sequence):
        now = base + timedelta(seconds=step)
        expected_before = reference.membership_counts(now)[0]
        reference.upsert_evaluated_fixture(
            fixture, markets=markets, scan_lane=ScanLane.UNIVERSE, now=now
        )
        expected_after = reference.membership_counts(now)[0]
        counted = counting.upsert_evaluated_fixture_counting_hot(
            fixture, markets=markets, scan_lane=ScanLane.UNIVERSE, now=now
        )
        assert counted == (expected_before, expected_after), (step, fixture.canonical_event_id)
        assert _store_outputs(counting, now) == _store_outputs(reference, now), step
        promotions += int(expected_after > expected_before)

    assert promotions >= len(range(0, 40, 5)) + 2
    assert "572-terminal" in counting._tombstones


@pytest.mark.asyncio
async def test_public_status_reads_sqlite_outside_the_coordinator_lock() -> None:
    """The API threadpool must not hold ``_state_lock`` across SQLite reads.

    Every scheduler lane and paper settlement take ``_state_lock`` on the event
    loop, so a slow read held under it stalled every coroutine.
    """

    coordinator = _coordinator()
    held_during_reads: list[bool] = []
    read_s = 0.4

    def slow_ledger_read(*_args, **_kwargs):
        held_during_reads.append(coordinator._state_lock.held_by_current_thread)
        time.sleep(read_s)

    coordinator._active_trade_locked_gbp = slow_ledger_read  # type: ignore[method-assign]
    coordinator._recent_active_trade_timeline = lambda: (slow_ledger_read() or [])  # type: ignore[method-assign]
    stop = threading.Event()

    def ui_polling() -> None:
        while not stop.is_set():
            coordinator.public_status()

    heartbeat = HeartbeatProbe().start()
    poller = threading.Thread(target=ui_polling, daemon=True)
    poller.start()
    try:
        deadline = time.perf_counter() + 4 * read_s
        while time.perf_counter() < deadline:
            coordinator.plan_hot_tick()
            coordinator.plan_background_tick()
            coordinator.plan_active_trade_tick()
            await asyncio.sleep(0.01)
    finally:
        stop.set()
        await heartbeat.stop()
        await asyncio.get_running_loop().run_in_executor(None, poller.join, 10)

    assert held_during_reads, "public_status never reached its SQLite reads"
    assert not any(held_during_reads)
    assert heartbeat.worst_s < LIVENESS_BOUND_S, (
        f"heartbeat delayed {heartbeat.worst_s:.3f}s behind public_status SQLite reads"
    )
    waits = loop_subphase_snapshot()["lock_waits"]
    assert waits.get("coordinator_state", {}).get("max_ms", 0) < LIVENESS_BOUND_S * 1000


@pytest.mark.asyncio
async def test_paper_settlement_does_not_wait_for_the_ledger_on_the_event_loop(
    tmp_path: Path,
) -> None:
    """An API handler or accounting job holding the ledger must not stall the loop.

    The settlement result is unchanged: the graded trade still closes.
    """

    from test_paper_auto_settlement import NOW, FakeKalshi, _kalshi_settled, _leg
    from test_paper_settlement_hotfix import _filled_trade, _persist_open
    from test_paper_trade_lifecycle import _ops

    ledger = SqlitePaperLedger(tmp_path / "issue572-ledger.sqlite")
    assert isinstance(ledger._lock, TimedRLock)
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    ticker = "KXEPLGAME-26SEP20NEWCHE-BTTS"
    try:
        trade = _filled_trade(
            extra_legs=[
                _leg(
                    venue=VenueName.KALSHI,
                    outcome="yes",
                    source_event_id="",
                    source_market_id=ticker,
                    source_contract_id=ticker,
                ),
                _leg(
                    venue=VenueName.KALSHI,
                    outcome="no",
                    source_event_id="",
                    source_market_id=ticker,
                    source_contract_id=ticker,
                ),
            ]
        )
        trade.legs[0].source_event_id = None
        trade.legs[1].source_event_id = None
        _persist_open(ops, trade)
        agent = PaperSettlementAgent(
            operations=ops,
            matchbook=None,
            kalshi=FakeKalshi({ticker: _kalshi_settled(result="yes")}),
            provider_access=None,
            clock=lambda: NOW,
        )
        held = threading.Event()
        hold_s = 0.6

        def accounting_job_holding_ledger() -> None:
            with ledger.exclusive():
                held.set()
                time.sleep(hold_s)

        holder = threading.Thread(target=accounting_job_holding_ledger, daemon=True)
        holder.start()
        assert await asyncio.get_running_loop().run_in_executor(None, held.wait, 5)
        heartbeat = HeartbeatProbe().start()
        started = time.perf_counter()
        result = await agent.run_cycle(now=NOW)
        elapsed = time.perf_counter() - started
        await heartbeat.stop()
        holder.join(timeout=5)
    finally:
        repository.close()
        ledger.close()

    assert elapsed >= hold_s * 0.8, "settlement did not contend with the ledger holder"
    assert trade.trade_id in result.settled_trade_ids
    assert heartbeat.worst_s < LIVENESS_BOUND_S, (
        f"event loop blocked {heartbeat.worst_s:.3f}s waiting for the paper ledger"
    )
    assert LOOP_ACTIVITY.lock_waits.get("paper_ledger") is None


@pytest.mark.asyncio
async def test_paper_settlement_builds_ledger_services_off_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """First settlement cycle opens the SQLite ledger / catalogue off the loop.

    Windows CI evidence before this: the #517 startup probe starved 0.601s
    with longest_phase=paper_settlement while those services were created.
    """

    from sports_hedge.application import live_refresh as live_refresh_module
    from sports_hedge.application.event_loop_activity import _on_event_loop_thread

    built_on_loop: list[bool] = []
    build_s = 0.4

    class _NoOpenTrades:
        trades = None

        def list_active_trades(self) -> list:
            return []

    def slow_first_use() -> tuple[object, object]:
        built_on_loop.append(_on_event_loop_thread())
        time.sleep(build_s)
        return _NoOpenTrades(), None

    monkeypatch.setattr(live_refresh_module, "_paper_settlement_dependencies", slow_first_use)
    coordinator = _coordinator()
    heartbeat = HeartbeatProbe().start()
    await coordinator._maybe_run_paper_settlement()
    await heartbeat.stop()

    assert built_on_loop == [False]
    assert coordinator._settlement_in_progress is False
    assert coordinator._next_settlement_due is not None
    assert heartbeat.worst_s < LIVENESS_BOUND_S, heartbeat.worst_s
    assert LOOP_ACTIVITY.longest is None or LOOP_ACTIVITY.longest.elapsed_s < LIVENESS_BOUND_S


@pytest.mark.asyncio
async def test_representative_universe_keeps_loop_live_and_lanes_progressing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """~275-cluster UNIVERSE with HOT / BACKGROUND / ACTIVE scheduled alongside it.

    Before #572 this sweep produced >100 callbacks >=0.25s (up to ~0.56s on a
    fast Linux VM) and delayed the heartbeat past 0.5s, all inside
    ``record_universe_fixture_progress``.
    """

    universe = RealisticUniverse(REPRESENTATIVE_FIXTURES, latency_s=0.002, hot_every=25)
    hot_fixtures = len(range(0, REPRESENTATIVE_FIXTURES, 25))
    collector, repository = _collector(universe)
    reference_collector, reference_repository = _collector(
        RealisticUniverse(REPRESENTATIVE_FIXTURES, latency_s=0, hot_every=25)
    )
    try:
        reference = await _universe_sweep(reference_collector)
    finally:
        reference_repository.close()

    monkeypatch.setenv("PAPER_LIVE_REFRESH_ENABLED", "true")
    get_settings.cache_clear()
    coordinator = _coordinator()
    coordinator.mark_startup_pricing_ready()
    monkeypatch.setattr(coordinator, "_seconds_until_hot", lambda: 0.2)
    monkeypatch.setattr(coordinator, "_seconds_until_background", lambda: 0.2)
    monkeypatch.setattr(coordinator, "_seconds_until_active_trade", lambda: 0.1)
    monkeypatch.setattr(coordinator, "_effective_hot_target_refresh_seconds", lambda: 0.2)
    trace: list[tuple[float, str, bool]] = []
    universe_window: dict[str, float] = {}
    universe_done = asyncio.Event()
    report_holder: dict[str, CollectionReport] = {}

    async def fake_settlement() -> None:
        trace.append((time.perf_counter(), "active", coordinator._universe_in_progress))

    monkeypatch.setattr(coordinator, "_maybe_run_paper_settlement", fake_settlement)

    async def tick(plan=None) -> None:
        lane = getattr(plan, "lane", None)
        if lane == ScanLane.UNIVERSE.value:
            if report_holder:
                await asyncio.sleep(0)
                return

            async def runner() -> CollectionReport:
                return await _universe_sweep(
                    collector, callbacks=coordinator.universe_collect_callbacks()
                )

            universe_window["start"] = time.perf_counter()
            report_holder["report"] = await coordinator.run_cycle(
                runner, timeout_seconds=None, scan_lane=ScanLane.UNIVERSE
            )
            universe_window["end"] = time.perf_counter()
            universe_done.set()
            return
        if lane in {ScanLane.HOT.value, "background"}:
            trace.append((time.perf_counter(), str(lane), coordinator._universe_in_progress))
        await asyncio.sleep(0.005)

    heartbeat = HeartbeatProbe()
    # Measure this workload's slices, not gen-2 GC over objects left behind by
    # the thousands of earlier tests in the same pytest process.
    gc.collect()
    gc.freeze()
    try:
        with CallbackProfiler() as profiler:
            heartbeat.start()
            await coordinator.start_server_loop(tick)
            await asyncio.wait_for(universe_done.wait(), timeout=240)
            await asyncio.sleep(0.6)
            await heartbeat.stop()
    finally:
        await coordinator.stop_server_loop()
        gc.unfreeze()
        # Pay the full-heap collection here, outside every measured window,
        # instead of inside whichever liveness test runs next.
        gc.collect()
        repository.close()
        get_settings.cache_clear()

    report = report_holder["report"]
    evaluated = [
        item
        for item in report.discovered_fixtures
        if item.market_evaluation_state == MarketEvaluationState.EVALUATED.value
    ]
    over = profiler.profile.over(LIVENESS_BOUND_S)
    detail = f"{profiler.profile.report()}\nsubphases={loop_subphase_snapshot()}"
    print(
        "issue572_representative "
        f"fixtures={REPRESENTATIVE_FIXTURES} callbacks={profiler.profile.callbacks} "
        f"busy_s={profiler.profile.total_s:.2f} "
        f"longest_callback_ms={int(profiler.profile.longest_s * 1000)} "
        f"longest_non_gc_callback_ms={int(profiler.profile.longest_non_gc_s * 1000)} "
        f"longest_iteration_ms={int(profiler.profile.longest_iteration_s * 1000)} "
        f"heartbeat_worst_ms={int(heartbeat.worst_s * 1000)} "
        f"callbacks_over_250ms={len(over)}"
    )

    assert len(report.discovered_fixtures) == REPRESENTATIVE_FIXTURES
    assert len(evaluated) == REPRESENTATIVE_FIXTURES
    assert _scanner_signature(report) == _scanner_signature(reference)
    assert coordinator._universe_hot_promotions == hot_fixtures
    assert over == [], f"{len(over)} event-loop callbacks >= 0.25s\n{detail}"
    assert heartbeat.worst_s < LIVENESS_BOUND_S, f"heartbeat {heartbeat.worst_s:.3f}s\n{detail}"
    assert LOOP_ACTIVITY.longest is not None
    assert LOOP_ACTIVITY.longest.elapsed_s < LIVENESS_BOUND_S, LOOP_ACTIVITY.longest

    # Runs recorded while UNIVERSE was in progress: true wall-clock overlap.
    during = [item for item in trace if item[2]]
    for lane in ("hot", "background", "active"):
        stamps = [item[0] for item in during if item[1] == lane]
        assert len(stamps) >= 3, f"{lane} starved during UNIVERSE ({len(stamps)} runs)"
    active = [item[0] for item in during if item[1] == "active"]
    worst_active_gap = max(b - a for a, b in itertools.pairwise(active))
    assert worst_active_gap < 0.1 + 2 * LIVENESS_BOUND_S, worst_active_gap
    end = universe_window["end"]
    assert any(item[1] == "background" and item[0] > end for item in trace), (
        "BACKGROUND did not resume after UNIVERSE completed"
    )
    assert coordinator._universe_in_progress is False


def _callback(elapsed_s: float, gc_s: float) -> SlowCallback:
    return SlowCallback(elapsed_s, "task:synthetic", "universe", "market_evaluation", gc_s=gc_s)


@pytest.mark.parametrize(
    "gaps,callbacks,longest_gc,expected",
    [
        pytest.param([SchedulingGap(0.12, 0.0)], [], 0.0, [], id="normal"),
        pytest.param([SchedulingGap(0.30, 0.0)], [], 0.0, ["non-GC scheduling gap"], id="non_gc_gap"),
        pytest.param([SchedulingGap(0.30, 0.10)], [], 0.10, [], id="gc_explains_gap"),
        pytest.param([SchedulingGap(0.40, 0.12)], [], 0.12, ["non-GC scheduling gap"], id="gc_too_small"),
        pytest.param([SchedulingGap(0.52, 0.45)], [], 0.45, ["scheduling gap"], id="gap_over_500"),
        pytest.param([SchedulingGap(0.10, 0.0)], [], 0.51, ["GC pause"], id="gc_pause_over_500"),
        pytest.param([], [_callback(0.30, 0.04)], 0.04, ["non-GC callback"], id="non_gc_callback"),
        pytest.param([], [_callback(0.30, 0.20)], 0.20, [], id="gc_callback"),
    ],
)
def test_extreme_stress_policy_excuses_only_garbage_collection(
    gaps, callbacks, longest_gc, expected
) -> None:
    failures = extreme_stress_liveness_failures(gaps, callbacks, longest_gc_pause_s=longest_gc)
    assert len(failures) == len(expected), failures
    assert all(item.startswith(prefix) for item, prefix in zip(failures, expected)), failures
    summary = extreme_stress_summary("synthetic", gaps, callbacks, longest_gc_pause_s=longest_gc)
    over = [gap for gap in gaps if gap.gap_s >= LIVENESS_BOUND_S]
    assert f"excursions_over_250ms={len(over)}" in summary


@pytest.mark.asyncio
async def test_extreme_stress_policy_still_fails_a_real_application_stall() -> None:
    """A 0.3s synchronous stretch with no GC must fail the extreme-stress policy."""

    reset_loop_activity()
    stop = asyncio.Event()
    with CallbackProfiler() as profiler:
        probe = asyncio.create_task(probe_gc_attributed_gaps(stop))
        await asyncio.sleep(0.05)
        loop = asyncio.get_running_loop()
        stalled = loop.create_future()

        def blocking_application_code() -> None:
            time.sleep(0.3)
            stalled.set_result(None)

        loop.call_soon(blocking_application_code)
        await stalled
        await asyncio.sleep(0.05)
        stop.set()
        gaps = await probe
    failures = extreme_stress_liveness_failures(
        gaps, profiler.profile.slow, longest_gc_pause_s=0.0
    )
    assert any(item.startswith("non-GC scheduling gap") for item in failures), failures
    assert any(item.startswith("non-GC callback") for item in failures), failures
    summary = extreme_stress_summary("stall", gaps, profiler.profile.slow, longest_gc_pause_s=0.0)
    assert "non_gc_excursions=1" in summary, summary
