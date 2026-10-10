"""Stage 1: dashboard status reads must not hold fixture_current_state while projecting.

Owner-live Windows evidence after #573: the process stayed alive, but /health
timed out around 2s and the event loop logged fixture_current_state waits of
about 0.9–1.8s (scheduler waits above 6s). public_status projected the full
fixture board inside that lock, so a poll stalled UNIVERSE publication.

Data class: synthetic fixture/demo providers. Not live, historical, or modelled
venue quotes. PAPER / read-only. Provider capacities, the 8s provider timeout,
the startup UNIVERSE barrier, and scanner economics are unchanged.
"""

from __future__ import annotations

import gc
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from loop_liveness_harness import (
    CallbackProfiler,
    HeartbeatProbe,
    RealisticUniverse,
    stream_runtime_is_unstarted,
)

from sports_hedge.application import fixture_current_state as current_state_module
from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.event_loop_activity import LOOP_ACTIVITY, reset_loop_activity
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.persistence.lane_venue_settings import SqliteLaneVenueSettingsStore
from sports_hedge.persistence.operator_scanner_settings import SqliteOperatorScannerSettingsStore
from sports_hedge.persistence.operator_universe_scope import SqliteOperatorUniverseScopeStore
from sports_hedge.persistence.universe_checkpoint import SqliteUniverseCheckpointStore

LIVENESS_BOUND_S = 0.25
REPRESENTATIVE_FIXTURES = 280
POLL_INTERVAL_S = 2.0
ALL_VENUES = [VenueName.MATCHBOOK, VenueName.KALSHI, VenueName.POLYMARKET]


def _coordinator() -> LiveRefreshCoordinator:
    return LiveRefreshCoordinator(
        operator_settings_store=SqliteOperatorScannerSettingsStore(":memory:"),
        venue_settings_store=SqliteLaneVenueSettingsStore(":memory:"),
        universe_scope_store=SqliteOperatorUniverseScopeStore(":memory:"),
        universe_checkpoint_store=SqliteUniverseCheckpointStore(":memory:"),
    )


def _load(store, report, now: datetime) -> None:
    store.upsert_from_report(report, scan_lane=ScanLane.UNIVERSE, now=now)


@pytest.mark.asyncio
async def test_operator_board_matches_separate_status_reads() -> None:
    """One off-lock board matches inventory, membership, and HOT breakdown."""

    from test_issue200_universe_hot_promotion import _qualifying_universe_report
    from test_issue572_event_loop_stalls import _mixed_store_report, _universe_sweep

    universe = RealisticUniverse(36, latency_s=0, hot_every=9)
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=universe.matchbook(),
        polymarket=universe.polymarket(),
        kalshi=universe.kalshi(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        report = await _universe_sweep(collector)
    finally:
        repository.close()
    now = datetime.now(UTC)
    qualifying = _qualifying_universe_report(
        "stage1-qualifying", kickoff=now + timedelta(days=4), when=now
    )
    mixed = _mixed_store_report(now)

    def populate(target) -> None:
        _load(target, report, now)
        _load(target, mixed, now)
        _load(target, qualifying, now)

    from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore

    inventory_store = FixtureCurrentStateStore()
    membership_store = FixtureCurrentStateStore()
    breakdown_store = FixtureCurrentStateStore()
    board_store = FixtureCurrentStateStore()
    for store in (inventory_store, membership_store, breakdown_store, board_store):
        populate(store)

    board = board_store.operator_board(now)
    assert [item.model_dump(mode="json") for item in board.discovered] == [
        item.model_dump(mode="json") for item in inventory_store.inventory(now)
    ]
    assert board.membership == membership_store.membership_counts(now)
    assert board.breakdown == breakdown_store.hot_membership_breakdown(now)
    assert board.as_of == now
    assert board.breakdown[2] >= 1


@pytest.mark.asyncio
async def test_public_status_projects_outside_the_fixture_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """UNIVERSE publication keeps running while status is polled at the UI cadence.

    The poll thread may project every fixture. It must not do that while it
    holds fixture_current_state. Scanner lock waits and non-GC callbacks stay
    inside the 250ms responsiveness target. Heartbeat wall-clock may include
    interpreter gen2 of this scan's unfrozen allocations; the SLA is non-GC
    lateness. STREAM stays default-off and is not started here.
    """

    import sports_hedge.api.paper  # noqa: F401 — production has this imported
    assert stream_runtime_is_unstarted()

    reset_loop_activity()
    coordinator_warm = _coordinator()
    coordinator_warm.public_status()
    universe = RealisticUniverse(REPRESENTATIVE_FIXTURES, latency_s=0)
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=universe.matchbook(),
        polymarket=universe.polymarket(),
        kalshi=universe.kalshi(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    coordinator = _coordinator()
    store = coordinator._fixture_state
    depth: dict[int, int] = {}
    holds: list[float] = []
    entered: dict[int, float] = {}
    original_acquire = store._lock.acquire
    original_release = store._lock.release

    def acquire(blocking: bool = True, timeout: float = -1) -> bool:
        ok = original_acquire(blocking, timeout)
        if not ok:
            return False
        ident = threading.get_ident()
        level = depth.get(ident, 0) + 1
        depth[ident] = level
        if level == 1:
            entered[ident] = time.perf_counter()
        return True

    def release() -> None:
        ident = threading.get_ident()
        level = depth.get(ident, 1) - 1
        if level <= 0:
            depth.pop(ident, None)
            started = entered.pop(ident, None)
            if started is not None and threading.current_thread().name == "status-poll":
                holds.append(time.perf_counter() - started)
        else:
            depth[ident] = level
        original_release()

    store._lock.acquire = acquire  # type: ignore[method-assign]
    store._lock.release = release  # type: ignore[method-assign]

    projections = {"poll": 0, "under_lock": 0}
    original_project = current_state_module.apply_current_market_inventory

    def counting(*args, **kwargs):
        if threading.current_thread().name == "status-poll":
            projections["poll"] += 1
            if depth.get(threading.get_ident(), 0) > 0:
                projections["under_lock"] += 1
        return original_project(*args, **kwargs)

    monkeypatch.setattr(current_state_module, "apply_current_market_inventory", counting)
    on_discovery, on_fixture, on_work = coordinator.universe_collect_callbacks()
    latencies: list[float] = []
    fixture_counts: list[int] = []
    stop = threading.Event()

    def poll() -> None:
        while not stop.is_set():
            started = time.perf_counter()
            status = coordinator.public_status()
            latencies.append(time.perf_counter() - started)
            fixture_counts.append(len(status.discovered_fixtures))
            assert status.fixture_board_as_of is not None
            stop.wait(POLL_INTERVAL_S)

    poller = threading.Thread(target=poll, name="status-poll", daemon=True)
        # Same pre-window as #572: collect and freeze objects retained by earlier
        # tests so a gen2 scan of that heap is not charged to this measurement.
        # Objects allocated by the scan below stay unfrozen. Heartbeat records
        # raw lateness and the loop-thread GC in each interval; the 250ms SLA
        # is non-GC lateness, not a raised bound.
    gc.collect()
    gc.freeze()
    heartbeat = HeartbeatProbe(interval_s=0.05).start()
    poller.start()
    published: list[int] = []

    def counted_on_fixture(cluster, fixture, decisions, inventory) -> None:
        on_fixture(cluster, fixture, decisions, inventory)
        published.append(len(store._rows))

    try:
        with CallbackProfiler() as profiler:
            report = await collector.collect_and_scan(
                fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
                maximum_execution_risk=100,
                max_event_pairs=10_000_000,
                scan_lane=ScanLane.UNIVERSE.value,
                universe_generation_id=1,
                unbounded_cycle=True,
                enabled_venues=ALL_VENUES,
                on_discovery_complete=on_discovery,
                on_fixture_evaluated=counted_on_fixture,
                on_canonical_work_set=on_work,
            )
    finally:
        stop.set()
        poller.join(timeout=5)
        await heartbeat.stop()
        gc.unfreeze()
        gc.collect()
        repository.close()

    wait = LOOP_ACTIVITY.lock_waits.get("fixture_current_state")
    wait_s = 0.0 if wait is None else wait.max_s
    assert len(report.discovered_fixtures) == REPRESENTATIVE_FIXTURES
    assert published[-1] == REPRESENTATIVE_FIXTURES
    assert published == sorted(published)
    assert projections["poll"] > 0
    assert projections["under_lock"] == 0
    assert holds, "status poll never acquired the fixture store lock"
    assert max(holds) < 0.05, f"status held fixture_current_state for {max(holds):.3f}s"
    assert wait_s < LIVENESS_BOUND_S, f"scanner waited {wait_s:.3f}s for fixture_current_state"
    assert heartbeat.worst_non_gc_s < LIVENESS_BOUND_S, (
        f"non-GC heartbeat {heartbeat.worst_non_gc_s:.3f}s ({heartbeat.report()})"
    )
    assert profiler.profile.longest_non_gc_s < LIVENESS_BOUND_S, profiler.profile.report()
    assert stream_runtime_is_unstarted()
    assert latencies
    # Steady-state polls, after process caches exist. The 250ms figure is the
    # scanner synchronous budget, asserted above. Status projection of the full
    # board stays under a second; a shared runner has measured about 0.6s.
    assert max(latencies) < 1.0, f"public_status {max(latencies):.3f}s {latencies}"
    assert fixture_counts[-1] > 0
