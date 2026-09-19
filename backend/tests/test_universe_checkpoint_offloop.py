"""Compact, off-loop, debounced UNIVERSE checkpoint persistence.

Deterministic coordinator/store harness. Not owner-live quotes.
PAPER / read-only. Does not change matching, venue scope, or execution.
"""

from __future__ import annotations

import asyncio
import inspect
import json
import sqlite3
import threading
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest

from sports_hedge.api import main as main_api
from sports_hedge.application.collector import CollectionReport
from sports_hedge.application.fixture_inventory import (
    FixtureMarketInventoryRow,
    InventoryComparisonStatus,
    InventoryPairResult,
    VenueMarketFacts,
    VenueQuoteFact,
)
from sports_hedge.application.live_refresh import (
    UNIVERSE_CHECKPOINT_FLUSH_FIXTURE_THRESHOLD,
    LiveRefreshCoordinator,
    ScanCycleTimeout,
)
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.application.universe_checkpoint import (
    UNIVERSE_CHECKPOINT_MAX_ENCODED_BYTES,
    UNIVERSE_CHECKPOINT_SEMANTICS_VERSION,
    SweepWorkUnit,
    UniverseCheckpointTooLarge,
    encode_durable_universe_checkpoint,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.persistence.universe_checkpoint import SqliteUniverseCheckpointStore
from test_concurrent_hot_universe_workers import _universe_fixture
from test_dual_cadence_scheduler import NOW, FakeClock, _decision, _fixture, _report


class _SlowCheckpointStore(SqliteUniverseCheckpointStore):
    def __init__(self, database: str | Path, delay: float = 0.5) -> None:
        super().__init__(database)
        self.delay = delay
        self.save_calls = 0
        self.save_started = threading.Event()
        self.save_threads: list[int] = []
        self.lock_held_by_saver: list[bool] = []
        self.coordinator: LiveRefreshCoordinator | None = None

    def save(self, payload: dict[str, Any], *, updated_at: str) -> None:
        self.save_calls += 1
        self.save_threads.append(threading.get_ident())
        coordinator = self.coordinator
        if coordinator is not None:
            self.lock_held_by_saver.append(coordinator._state_lock.held_by_current_thread)
        self.save_started.set()
        time.sleep(self.delay)
        super().save(payload, updated_at=updated_at)


class _FailingCheckpointStore(SqliteUniverseCheckpointStore):
    def __init__(
        self,
        database: str | Path,
        *,
        fail_saves: int = 0,
        fail_clears: int = 0,
        oversize: bool = False,
    ) -> None:
        super().__init__(database)
        self.fail_saves = fail_saves
        self.fail_clears = fail_clears
        self.oversize = oversize
        self.save_calls = 0
        self.clear_calls = 0

    def save(self, payload: dict[str, Any], *, updated_at: str) -> None:
        self.save_calls += 1
        if self.oversize:
            raise UniverseCheckpointTooLarge(
                "universe checkpoint encoded size exceeds compact cap"
            )
        if self.fail_saves > 0:
            self.fail_saves -= 1
            raise sqlite3.OperationalError("database is locked")
        super().save(payload, updated_at=updated_at)

    def clear(self) -> None:
        self.clear_calls += 1
        if self.fail_clears > 0:
            self.fail_clears -= 1
            raise sqlite3.OperationalError("database is locked")
        super().clear()


def _hot_equivalent_row(canonical_id: str) -> FixtureMarketInventoryRow:
    def facts(venue: VenueName, market_id: str) -> VenueMarketFacts:
        return VenueMarketFacts(
            venue=venue,
            source_event_id=f"{venue.value}-{canonical_id}",
            source_market_id=market_id,
            family="both_teams_to_score",
            period="full_time",
            settlement_key="regulation_time|full_time",
            settlement_complete=True,
            best_backs=[
                VenueQuoteFact(
                    outcome="yes", decimal_odds=Decimal("2.10"), size_at_touch=Decimal(100)
                ),
                VenueQuoteFact(
                    outcome="no", decimal_odds=Decimal("1.80"), size_at_touch=Decimal(100)
                ),
            ],
            quote_age_ms=80,
            quote_age_basis="source",
            native_currency="GBP" if venue is VenueName.MATCHBOOK else "USD",
        )

    return FixtureMarketInventoryRow(
        display_name="BTTS",
        family="both_teams_to_score",
        period="full_time",
        comparison_status=InventoryComparisonStatus.MATCHED_EQUIVALENT,
        entered_solver=True,
        solver_model="strict_complete_set",
        current_net_edge=Decimal("0.015"),
        trigger_net_edge=Decimal("0.01"),
        solver_is_arbitrage=True,
        matchbook=facts(VenueName.MATCHBOOK, "mb-btts"),
        polymarket=facts(VenueName.POLYMARKET, "pm-btts"),
        pair_results=[
            InventoryPairResult(
                left_venue=VenueName.MATCHBOOK,
                right_venue=VenueName.POLYMARKET,
                entered_solver=True,
                solver_model="strict_complete_set",
                current_net_edge=Decimal("0.015"),
                solver_is_arbitrage=True,
            )
        ],
    )


def _lifecycle_hot_fixture(canonical_id: str) -> Any:
    fixture = _fixture(
        canonical_id,
        kickoff=NOW - timedelta(minutes=1),
        in_running=True,
        evaluation="evaluated",
    )
    return fixture.model_copy(
        update={
            "matched_equivalent_count": 1,
            "last_scanned_at": NOW,
            "opportunity_state": "matched",
        }
    )


def _assert_compact_checkpoint(payload: dict[str, Any] | None) -> None:
    assert payload is not None
    assert payload["semantics_version"] == UNIVERSE_CHECKPOINT_SEMANTICS_VERSION
    assert "report" not in payload
    assert "discovery_snapshot" not in payload
    assert "series_results" not in payload
    encoded = encode_durable_universe_checkpoint(payload)
    assert len(encoded.encode("utf-8")) <= UNIVERSE_CHECKPOINT_MAX_ENCODED_BYTES


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


def test_health_and_build_info_are_cheap_and_skip_settings_db() -> None:
    health_src = inspect.getsource(main_api.health)
    build_src = inspect.getsource(main_api.build_info)
    assert "configure_from_settings" not in health_src
    assert "paper_settings" not in health_src
    assert "configure_from_settings" not in build_src
    assert inspect.iscoroutinefunction(main_api.health)
    assert inspect.iscoroutinefunction(main_api.build_info)


@pytest.mark.asyncio
async def test_slow_checkpoint_save_does_not_block_health_or_build_info(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = _SlowCheckpointStore(tmp_path / "slow.sqlite", delay=0.5)
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    store.coordinator = coordinator
    monkeypatch.setattr(main_api, "get_live_refresh_coordinator", lambda: coordinator)
    monkeypatch.setattr(main_api, "get_universe_checkpoint_store", lambda: store)

    transport = httpx.ASGITransport(app=main_api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        coordinator.record_universe_work_set(["a", "b", "c"], authoritative=True)
        for _ in range(50):
            if store.save_started.is_set():
                break
            await asyncio.sleep(0.02)
        assert store.save_started.is_set()
        loop_thread = threading.get_ident()
        started = time.monotonic()
        health = await client.get("/health")
        build = await client.get("/build-info")
        elapsed = time.monotonic() - started
        assert health.status_code == 200
        assert build.status_code == 200
        assert health.json()["mode"] == "paper"
        assert health.json()["execution_enabled"] is False
        assert elapsed < 1.0
        assert store.save_threads
        assert store.save_threads[0] != loop_thread

    await coordinator._await_universe_checkpoint_persist()


@pytest.mark.asyncio
async def test_health_does_not_open_paper_settings_sqlite(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    opened: list[str] = []
    orig_connect = sqlite3.connect

    def wrapped(database, *args, **kwargs):
        opened.append(str(database))
        return orig_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", wrapped)
    transport = httpx.ASGITransport(app=main_api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        opened.clear()
        health = await client.get("/health")
        build = await client.get("/build-info")
        assert health.status_code == 200
        assert build.status_code == 200
    assert not any("paper_settings" in item for item in opened)


def test_durable_checkpoint_is_compact_and_under_cap(tmp_path: Path) -> None:
    store = SqliteUniverseCheckpointStore(tmp_path / "compact.sqlite")
    first = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    first.configure_from_settings()
    ids = [f"evt:{index:03d}" for index in range(200)]
    first.record_universe_work_set(ids, authoritative=True)
    first.record_universe_discovery_snapshot(
        {
            "matchbook": [{"id": f"mb-{index}", "raw": "x" * 5000} for index in range(200)],
            "polymarket": [{"id": f"pm-{index}", "raw": "y" * 5000} for index in range(200)],
            "kalshi": [],
        }
    )
    for item in ids[:40]:
        first.record_universe_fixture_progress(None, _universe_fixture(item), [], [])
    payload = store.load()
    assert payload is not None
    assert payload["semantics_version"] == UNIVERSE_CHECKPOINT_SEMANTICS_VERSION
    assert "report" not in payload
    assert "discovery_snapshot" not in payload
    assert "series_results" not in payload
    encoded = encode_durable_universe_checkpoint(payload)
    assert len(encoded.encode("utf-8")) <= UNIVERSE_CHECKPOINT_MAX_ENCODED_BYTES
    blob = json.dumps(payload)
    assert "mb-0" not in blob
    assert ("x" * 80) not in blob


def test_fat_v1_checkpoint_fail_closes(tmp_path: Path) -> None:
    store = SqliteUniverseCheckpointStore(tmp_path / "fat.sqlite")
    first = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    first.configure_from_settings()
    first.record_universe_work_set(["keep-me"])
    payload = store.load()
    assert payload is not None
    payload["semantics_version"] = 1
    payload["discovery_snapshot"] = {"matchbook": [{"id": "raw-event", "markets": [{}]}]}
    payload["report"] = {"discovered_fixtures": [{"canonical_event_id": "keep-me"}]}
    store.save(payload, updated_at=NOW.isoformat())
    restarted = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    restarted.configure_from_settings()
    assert restarted._universe_generation_started_at is None
    assert restarted._universe_work == {}
    assert store.load() is None


def test_checkpoint_save_never_holds_state_lock(tmp_path: Path) -> None:
    store = _SlowCheckpointStore(tmp_path / "lock.sqlite", delay=0.0)
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    store.coordinator = coordinator
    coordinator.configure_from_settings()
    coordinator.record_universe_work_set(["a"])
    coordinator.record_universe_fixture_progress(None, _universe_fixture("a"), [], [])
    assert store.save_calls >= 1
    assert store.lock_held_by_saver
    assert all(held is False for held in store.lock_held_by_saver)


@pytest.mark.asyncio
async def test_fixture_progress_coalesces_checkpoint_writes(tmp_path: Path) -> None:
    store = _SlowCheckpointStore(tmp_path / "coalesce.sqlite", delay=0.05)
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    store.coordinator = coordinator
    coordinator.configure_from_settings()
    coordinator.record_universe_work_set(
        [f"evt:{index:02d}" for index in range(UNIVERSE_CHECKPOINT_FLUSH_FIXTURE_THRESHOLD - 1)]
    )
    await coordinator._await_universe_checkpoint_persist()
    baseline = store.save_calls
    for index in range(UNIVERSE_CHECKPOINT_FLUSH_FIXTURE_THRESHOLD - 1):
        coordinator.record_universe_fixture_progress(
            None, _universe_fixture(f"evt:{index:02d}"), [], []
        )
    await asyncio.sleep(0.02)
    assert store.save_calls == baseline
    coordinator.record_universe_fixture_progress(
        None,
        _universe_fixture(f"evt:{UNIVERSE_CHECKPOINT_FLUSH_FIXTURE_THRESHOLD - 1:02d}"),
        [],
        [],
    )
    await coordinator._await_universe_checkpoint_persist()
    assert store.save_calls == baseline + 1
    fixture_saves = store.save_calls - baseline
    fixture_callbacks = UNIVERSE_CHECKPOINT_FLUSH_FIXTURE_THRESHOLD
    assert fixture_saves < fixture_callbacks


@pytest.mark.asyncio
async def test_slow_persist_does_not_block_chunk_timeout(tmp_path: Path) -> None:
    store = _SlowCheckpointStore(tmp_path / "timeout.sqlite", delay=1.5)
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock, universe_checkpoint_store=store)
    store.coordinator = coordinator
    coordinator._clock = clock
    started = asyncio.Event()

    async def hung_runner() -> CollectionReport:
        coordinator.record_universe_discovery_snapshot(
            {"matchbook": [{"id": "mb-1"}], "polymarket": [], "kalshi": []}
        )
        coordinator.record_universe_work_set(["a", "b"], authoritative=True)
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("hung runner resumed")

    cycle = asyncio.create_task(
        coordinator.run_cycle(hung_runner, timeout_seconds=0.25, scan_lane=ScanLane.UNIVERSE)
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    await asyncio.sleep(0.6)
    assert coordinator.status.universe.cycle_in_progress is False
    assert coordinator._universe_active_chunk_epoch is None
    with pytest.raises(ScanCycleTimeout):
        await asyncio.wait_for(cycle, timeout=3.0)


@pytest.mark.asyncio
async def test_stale_chunk_cannot_commit_after_epoch_invalidation(tmp_path: Path) -> None:
    store = _SlowCheckpointStore(tmp_path / "stale.sqlite", delay=0.4)
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock, universe_checkpoint_store=store)
    store.coordinator = coordinator
    coordinator._clock = clock
    recorded_a = asyncio.Event()
    after_replacement = asyncio.Event()
    stale_attempted = asyncio.Event()

    async def zombie_chunk() -> CollectionReport:
        on_discovery, on_fixture, on_work = coordinator.universe_collect_callbacks()
        on_discovery({"matchbook": [{"id": "mb-safe"}], "polymarket": [], "kalshi": []})
        on_work(["a", "b", "c"], authoritative=True)
        on_fixture(None, _universe_fixture("a"), [], [])
        recorded_a.set()
        while not after_replacement.is_set():
            try:
                await asyncio.sleep(0.01)
            except asyncio.CancelledError:
                continue
        on_work(["a", "b", "zombie-extra"], authoritative=True)
        on_fixture(None, _universe_fixture("b"), [], [])
        stale_attempted.set()
        return _partial_report("a", leftover="b", when=clock.now)

    with pytest.raises(ScanCycleTimeout):
        await coordinator.run_cycle(
            zombie_chunk, timeout_seconds=0.25, scan_lane=ScanLane.UNIVERSE
        )
    await asyncio.wait_for(recorded_a.wait(), timeout=1)
    assert "a" in coordinator._universe_evaluated_ids

    async def replacement_chunk() -> CollectionReport:
        await stale_attempted.wait()
        on_discovery, on_fixture, on_work = coordinator.universe_collect_callbacks()
        on_work(["a", "b", "c"], authoritative=True)
        on_fixture(None, _universe_fixture("c"), [], [])
        return _partial_report("c", leftover="b", when=clock.now)

    replacement = asyncio.create_task(
        coordinator.run_cycle(
            replacement_chunk, timeout_seconds=2.0, scan_lane=ScanLane.UNIVERSE
        )
    )
    after_replacement.set()
    await replacement
    await coordinator._await_universe_checkpoint_persist()
    payload = store.load()
    assert payload is not None
    assert "zombie-extra" not in (payload.get("work_units") or {})
    assert "c" in (payload.get("work_units") or {})
    assert "b" not in coordinator._universe_evaluated_ids


def test_restart_restores_compact_resume_state(tmp_path: Path) -> None:
    store = SqliteUniverseCheckpointStore(tmp_path / "resume.sqlite")
    first = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    first.configure_from_settings()
    first.record_universe_work_set(["done-a", "retry-b", "pending-c"])
    first.record_universe_fixture_progress(None, _universe_fixture("done-a"), [], [])
    first.record_universe_fixture_progress(
        None,
        _universe_fixture("retry-b", evaluation="market_fetch_unavailable"),
        [],
        [],
    )
    generation = first._universe_generation_id
    sweep = first._universe_sweep_id
    restarted = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    restarted.configure_from_settings()
    assert restarted._universe_generation_id == generation
    assert restarted._universe_sweep_id == sweep
    assert restarted._universe_work["done-a"].state == "evaluated"
    assert restarted._universe_work["retry-b"].state == "retry_wait"
    assert restarted._universe_work["pending-c"].state == "pending"
    plan = restarted.plan_universe_tick(now=NOW + timedelta(seconds=3))
    assert plan.generation_resume is True
    assert plan.universe_generation_id == generation
    assert "done-a" in restarted._universe_needs_rehydration
    assert "done-a" not in plan.skip_event_ids
    assert "pending-c" not in plan.skip_event_ids
    assert restarted._universe_discovery_snapshot is None


def test_restart_rehydrates_hot_identity_without_double_counting(tmp_path: Path) -> None:
    store = SqliteUniverseCheckpointStore(tmp_path / "hot-restart.sqlite")
    first = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    first.configure_from_settings()
    fixture = _lifecycle_hot_fixture("hot-a")
    inventory = [_hot_equivalent_row("hot-a")]
    decisions = [_decision("hot-a", "mkt-hot-a")]
    first.record_universe_work_set(["hot-a", "pending-b"], authoritative=True)
    first.record_universe_fixture_progress(None, fixture, decisions, inventory)
    assert "hot-a" in first.fixture_current_state().hot_identity_scope(NOW)
    assert first.hot_market_relationships(["hot-a"]).get("hot-a")
    matched = first._universe_matched_fixtures
    equivalent = first._universe_equivalent_markets
    evaluated = first.status.universe.canonical_evaluated
    promotions = first._universe_hot_promotions
    _assert_compact_checkpoint(store.load())

    restarted = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    restarted.configure_from_settings()
    assert restarted._universe_work["hot-a"].state == "evaluated"
    assert "hot-a" in restarted._universe_needs_rehydration
    assert restarted.fixture_current_state().hot_identity_scope(NOW) == []
    assert restarted.hot_market_relationships(["hot-a"]) == {}
    first_plan = restarted.plan_universe_tick(now=NOW)
    assert first_plan.generation_resume is True
    assert "hot-a" not in first_plan.skip_event_ids
    assert "pending-b" not in first_plan.skip_event_ids
    assert restarted._universe_sweep_is_complete_unlocked() is False

    restarted.record_universe_work_set(["hot-a", "pending-b"], authoritative=True)
    restarted.record_universe_fixture_progress(None, fixture, decisions, inventory)
    assert "hot-a" not in restarted._universe_needs_rehydration
    assert "hot-a" in restarted.fixture_current_state().hot_identity_scope(NOW)
    relationships = restarted.hot_market_relationships(["hot-a"])
    assert "hot-a" in relationships
    assert relationships["hot-a"][0].proof_status == InventoryComparisonStatus.MATCHED_EQUIVALENT.value
    assert restarted._universe_matched_fixtures == matched
    assert restarted._universe_equivalent_markets == equivalent
    assert restarted.status.universe.canonical_evaluated == evaluated
    assert restarted._universe_hot_promotions == promotions
    later = restarted.plan_universe_tick(now=NOW)
    assert "hot-a" in later.skip_event_ids
    assert "pending-b" not in later.skip_event_ids
    _assert_compact_checkpoint(store.load())


def test_restart_mixed_set_rehydrates_only_missing_evaluated_current_state(
    tmp_path: Path,
) -> None:
    store = SqliteUniverseCheckpointStore(tmp_path / "mixed-restart.sqlite")
    first = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    first.configure_from_settings()
    first.record_universe_work_set(["done-a", "retry-b", "pending-c"], authoritative=True)
    first.record_universe_fixture_progress(None, _universe_fixture("done-a"), [], [])
    first.record_universe_fixture_progress(
        None,
        _universe_fixture("retry-b", evaluation="market_fetch_unavailable"),
        [],
        [],
    )
    evaluated = first.status.universe.canonical_evaluated

    restarted = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    restarted.configure_from_settings()
    assert restarted._universe_work["done-a"].state == "evaluated"
    assert restarted._universe_work["retry-b"].state == "retry_wait"
    assert restarted._universe_work["pending-c"].state == "pending"
    waiting = restarted.plan_universe_tick(now=NOW)
    assert "done-a" in restarted._universe_needs_rehydration
    assert "retry-b" not in restarted._universe_needs_rehydration
    assert "pending-c" not in restarted._universe_needs_rehydration
    assert "done-a" not in waiting.skip_event_ids
    assert "retry-b" in waiting.skip_event_ids
    assert "pending-c" not in waiting.skip_event_ids

    restarted.record_universe_work_set(
        ["done-a", "retry-b", "pending-c"], authoritative=True
    )
    restarted.record_universe_fixture_progress(None, _universe_fixture("done-a"), [], [])
    assert "done-a" not in restarted._universe_needs_rehydration
    assert restarted.status.universe.canonical_evaluated == evaluated
    after = restarted.plan_universe_tick(now=NOW)
    assert "done-a" in after.skip_event_ids
    assert "retry-b" in after.skip_event_ids
    assert "pending-c" not in after.skip_event_ids
    due = restarted.plan_universe_tick(now=NOW + timedelta(seconds=3))
    assert "done-a" in due.skip_event_ids
    assert "retry-b" not in due.skip_event_ids
    assert "pending-c" not in due.skip_event_ids
    _assert_compact_checkpoint(store.load())


def test_transient_checkpoint_save_failure_remains_retryable(tmp_path: Path) -> None:
    store = _FailingCheckpointStore(tmp_path / "retry.sqlite", fail_saves=1)
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    coordinator.configure_from_settings()
    coordinator.record_universe_work_set(["a"])
    assert store.save_calls == 1
    assert coordinator._universe_checkpoint_dirty is True
    assert coordinator.status.universe.persist_ok is False
    assert store.load() is None
    coordinator.flush_universe_checkpoint()
    assert store.save_calls == 2
    assert coordinator._universe_checkpoint_dirty is False
    assert coordinator.status.universe.persist_ok is True
    assert store.load() is not None


def test_oversize_checkpoint_is_surfaced_and_does_not_tight_loop(tmp_path: Path) -> None:
    store = _FailingCheckpointStore(tmp_path / "oversize.sqlite", oversize=True)
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    coordinator.configure_from_settings()
    coordinator.record_universe_work_set(["a"])
    assert store.save_calls == 1
    assert coordinator._universe_checkpoint_dirty is False
    assert coordinator.status.universe.persist_ok is False
    assert "exceeds compact cap" in (coordinator.status.universe.last_persist_error or "")
    coordinator.flush_universe_checkpoint()
    assert store.save_calls == 1
    store.oversize = False
    coordinator.record_universe_work_set(["a", "b"], authoritative=True)
    assert store.save_calls == 2
    assert coordinator._universe_checkpoint_dirty is False
    assert coordinator.status.universe.persist_ok is True
    _assert_compact_checkpoint(store.load())


def test_failed_generation_close_clear_remains_retryable(tmp_path: Path) -> None:
    store = _FailingCheckpointStore(tmp_path / "clear.sqlite")
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    coordinator.configure_from_settings()
    coordinator.record_universe_work_set(["a"], authoritative=True)
    assert store.load() is not None
    coordinator._universe_generation_started_at = None
    coordinator._universe_checkpoint_dirty = True
    store.fail_clears = 1
    coordinator.flush_universe_checkpoint()
    assert store.clear_calls == 1
    assert coordinator._universe_checkpoint_dirty is True
    assert coordinator.status.universe.persist_ok is False
    assert store.load() is not None
    coordinator.flush_universe_checkpoint()
    assert store.clear_calls == 2
    assert coordinator._universe_checkpoint_dirty is False
    assert store.load() is None


def test_compact_work_units_stay_under_cap_for_synthetic_counts() -> None:
    units = {
        f"evt:{index:04d}": SweepWorkUnit(
            canonical_id=f"evt:{index:04d}",
            state="evaluated",
        )
        for index in range(400)
    }
    from sports_hedge.application.universe_checkpoint import UniverseGenerationCheckpoint

    checkpoint = UniverseGenerationCheckpoint(
        generation_id=1,
        generation_started_at=NOW,
        updated_at=NOW,
        work_units=units,
        evaluated_ids=sorted(units),
        semantics_version=UNIVERSE_CHECKPOINT_SEMANTICS_VERSION,
    )
    encoded = encode_durable_universe_checkpoint(checkpoint.model_dump(mode="json"))
    assert len(encoded.encode("utf-8")) <= UNIVERSE_CHECKPOINT_MAX_ENCODED_BYTES
