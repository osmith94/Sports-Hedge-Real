"""Issue #305: authoritative UNIVERSE work-set reconciliation for stale/orphan IDs.

Owner-live shape on PR #304 head: generation 26 restored 77 canonical work /
75 evaluated / 2 PENDING orphans while the reused snapshot clustered 74 current
IDs, none matching the pending pair. Skip IDs skipped all 74 current clusters,
so the orphans were never seen and the generation could not close.

Deterministic coordinator/collector fixtures. Not owner-live quotes.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sports_hedge.application.collector import (
    CANONICAL_WORK_SET_PARTIAL_REASONS,
    UNIVERSE_COMPLETENESS_COMPLETE,
    CollectorIssue,
    ReadOnlyCrossVenueCollector,
    canonical_work_set_authority,
)
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.provider_access import HEALTH_AUTH_FAILURE, HEALTH_DISCOVERY_TIMEOUT
from sports_hedge.application.scan_lanes import WORKER_COMPLETE, ScanLane
from sports_hedge.application.universe_checkpoint import (
    STALE_ORPHAN_REASON,
    SWEEP_EVALUATED,
    SWEEP_OK,
    SWEEP_PENDING,
    SWEEP_RETRY_WAIT,
    SWEEP_STALE_ORPHAN,
    UNIVERSE_CHECKPOINT_SEMANTICS_VERSION,
    SeriesWorkUnit,
    SweepWorkUnit,
    checkpoint_from_payload,
    series_work_key,
    universe_work_retry_backoff_seconds,
)
from sports_hedge.config import get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.persistence.universe_checkpoint import SqliteUniverseCheckpointStore
from test_concurrent_hot_universe_workers import _universe_fixture
from test_dual_cadence_scheduler import NOW, FakeClock, _report
from test_issue200_universe_hot_promotion import (
    _market_row,
    _qualifying_universe_report,
)
from test_issue303_universe_persistent_worker import _cooldown_seconds
from test_read_only_collector import FakeMatchbook, FakePolymarket
from venue_cost_helpers import matchbook_polymarket_costs

EVALUATED_IDS = [f"evt:ok-{index:03d}" for index in range(1, 76)]
ORPHAN_IDS = ["evt:orphan-a", "evt:orphan-b"]
CURRENT_CLUSTER_IDS = EVALUATED_IDS[:74]


def _seed_owner_live_generation_26(store: SqliteUniverseCheckpointStore) -> None:
    clock = FakeClock(NOW)
    first = LiveRefreshCoordinator(clock=clock, universe_checkpoint_store=store)
    first._clock = clock
    first.configure_from_settings()
    first._universe_generation_id = 26
    first._universe_generation_started_at = NOW
    first._universe_progress_generation_id = 26
    first._universe_sweep_id = "sweep-26-owner-live"
    first._universe_cursor = EVALUATED_IDS[-1]
    first._universe_discovery_snapshot = {
        "matchbook": [{"id": 1, "name": "West Ham vs Chelsea"}],
        "polymarket": [],
        "kalshi": [{"event_ticker": "KX-1", "title": "West Ham vs Chelsea"}],
    }
    first._universe_work = {
        canonical_id: SweepWorkUnit(canonical_id=canonical_id, state=SWEEP_EVALUATED)
        for canonical_id in EVALUATED_IDS
    }
    for orphan in ORPHAN_IDS:
        first._universe_work[orphan] = SweepWorkUnit(canonical_id=orphan, state=SWEEP_PENDING)
    first._universe_evaluated_ids = set(EVALUATED_IDS)
    first._universe_discovered_total = 77
    first._universe_matched_fixtures = 3
    first._universe_equivalent_markets = 3
    first._universe_series_work = {
        series_work_key("kalshi", f"KX{index:02d}"): SeriesWorkUnit(
            venue="kalshi",
            series=f"KX{index:02d}",
            state=SWEEP_OK,
            event_count=1,
        )
        for index in range(1, 31)
    }
    first._next_universe_due = NOW
    first._persist_universe_checkpoint_unlocked()
    first.flush_universe_checkpoint()


def _restore_owner_live(store: SqliteUniverseCheckpointStore) -> tuple[FakeClock, LiveRefreshCoordinator]:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock, universe_checkpoint_store=store)
    coordinator._clock = clock
    coordinator.configure_from_settings()
    return clock, coordinator


def _assert_owner_live_restored(coordinator: LiveRefreshCoordinator) -> None:
    counts = coordinator._canonical_counts_unlocked()
    assert coordinator._universe_generation_id == 26
    assert coordinator._universe_generation_started_at is not None
    assert counts["canonical_work_total"] == 77
    assert counts["canonical_evaluated"] == 75
    assert counts["canonical_retryable"] == 0
    assert counts["canonical_remaining"] == 2
    assert {coordinator._universe_work[item].state for item in ORPHAN_IDS} == {SWEEP_PENDING}
    assert len(coordinator._universe_series_work) == 30
    assert all(unit.state == SWEEP_OK for unit in coordinator._universe_series_work.values())
    skip = set(coordinator._universe_skip_ids_unlocked(NOW))
    assert skip == set()
    assert set(EVALUATED_IDS) <= coordinator._universe_needs_rehydration
    assert set(ORPHAN_IDS).isdisjoint(skip)
    assert set(ORPHAN_IDS).isdisjoint(coordinator._universe_needs_rehydration)


def _rehydrate_restored_evaluated(coordinator: LiveRefreshCoordinator) -> None:
    for canonical_id in list(coordinator._universe_needs_rehydration):
        coordinator.record_universe_fixture_progress(
            None, _universe_fixture(canonical_id), [], []
        )


def _zero_work_complete_report(*, when) -> object:
    return _report([], when=when, scan_lane=ScanLane.UNIVERSE.value).model_copy(
        update={
            "completed_at": when + timedelta(seconds=4),
            "matched_event_pairs": 41,
            "matched_market_pairs": 0,
            "discovery_reused": True,
            "scan_diagnostics": {
                "completeness": UNIVERSE_COMPLETENESS_COMPLETE,
                "evaluated_count": 0,
                "not_evaluated_count": 0,
                "generation_resume": True,
                "universe_generation_id": 26,
                "clusters_before_resume": 74,
                "skipped_by_resume_count": 74,
                "canonical_work_total": 74,
                "canonical_work_set_authoritative": True,
                "canonical_work_set_partial_reason": None,
            },
        }
    )


def test_owner_live_orphan_shape_is_reconciled_without_false_evaluated(tmp_path: Path) -> None:
    store = SqliteUniverseCheckpointStore(tmp_path / "issue305-owner-live.sqlite")
    _seed_owner_live_generation_26(store)
    clock, coordinator = _restore_owner_live(store)
    _assert_owner_live_restored(coordinator)

    coordinator.record_universe_work_set(CURRENT_CLUSTER_IDS, authoritative=True)
    for orphan in ORPHAN_IDS:
        unit = coordinator._universe_work[orphan]
        assert unit.state == SWEEP_STALE_ORPHAN
        assert unit.reason == STALE_ORPHAN_REASON
        assert unit.state != SWEEP_EVALUATED
        assert unit.retryable is False
    for canonical_id in EVALUATED_IDS:
        assert coordinator._universe_work[canonical_id].state == SWEEP_EVALUATED
    counts = coordinator._canonical_counts_unlocked()
    assert counts["canonical_evaluated"] == 75
    assert counts["canonical_stale_orphan"] == 2
    assert counts["canonical_remaining"] == 0
    vanished = set(EVALUATED_IDS) - set(CURRENT_CLUSTER_IDS)
    assert vanished <= set(EVALUATED_IDS)
    assert coordinator._universe_needs_rehydration == set(CURRENT_CLUSTER_IDS)
    assert coordinator._universe_sweep_is_complete_unlocked() is False
    _rehydrate_restored_evaluated(coordinator)
    assert coordinator._universe_needs_rehydration == set()
    assert coordinator._universe_sweep_is_complete_unlocked() is True

    coordinator.record_report(_zero_work_complete_report(when=clock.now), scan_lane=ScanLane.UNIVERSE)
    assert coordinator._universe_generation_id == 26
    assert coordinator._universe_generation_started_at is None
    assert coordinator.status.universe.worker_state == WORKER_COMPLETE
    cooldown = timedelta(seconds=_cooldown_seconds())
    finished = clock.now + timedelta(seconds=4)
    assert coordinator._next_universe_due == finished + cooldown
    assert coordinator._seconds_until_universe() > 0.05

    idle = coordinator.plan_universe_tick(now=clock.now)
    assert idle.lane == "idle"
    assert idle.reason == "universe_cooldown"
    still_idle = coordinator.plan_universe_tick(now=finished + cooldown - timedelta(seconds=1))
    assert still_idle.lane == "idle"

    clock.now = finished + cooldown
    nxt = coordinator.plan_universe_tick(now=clock.now)
    assert nxt.lane == ScanLane.UNIVERSE.value
    assert nxt.universe_generation_id == 27
    assert nxt.generation_resume is False
    assert nxt.skip_event_ids == []
    assert nxt.resume_cursor is None
    assert nxt.reuse_discovery is False
    assert nxt.discovery_snapshot is None


def test_partial_discovery_does_not_retire_absent_pending_ids(tmp_path: Path) -> None:
    store = SqliteUniverseCheckpointStore(tmp_path / "issue305-partial.sqlite")
    _seed_owner_live_generation_26(store)
    _clock, coordinator = _restore_owner_live(store)
    _assert_owner_live_restored(coordinator)

    coordinator.record_universe_work_set(
        CURRENT_CLUSTER_IDS,
        authoritative=False,
        partial_reason="provider_failure",
    )
    assert {coordinator._universe_work[item].state for item in ORPHAN_IDS} == {SWEEP_PENDING}
    assert coordinator._canonical_counts_unlocked()["canonical_remaining"] == 2
    assert coordinator._universe_sweep_is_complete_unlocked() is False

    coordinator._universe_series_work[series_work_key("kalshi", "KX01")].state = SWEEP_RETRY_WAIT
    coordinator._universe_series_work[series_work_key("kalshi", "KX01")].retryable = True
    coordinator.record_universe_work_set(CURRENT_CLUSTER_IDS, authoritative=True)
    assert {coordinator._universe_work[item].state for item in ORPHAN_IDS} == {SWEEP_PENDING}
    assert coordinator._universe_generation_started_at is not None


@pytest.mark.parametrize(
    ("kwargs", "reason"),
    [
        ({"clustering_truncated": True}, "clustering_truncated"),
        (
            {"issues": [CollectorIssue(stage="normalize_match", detail="scan_cycle_deadline_reached")]},
            "deadline_truncation",
        ),
        ({"retry_series": {"kalshi": ["KXBAD"]}}, "retry_series_partial"),
        (
            {"venue_health": {"matchbook": "ok", "polymarket": "ok", "kalshi": HEALTH_DISCOVERY_TIMEOUT}},
            "provider_failure",
        ),
        (
            {"venue_health": {"matchbook": "ok", "polymarket": "ok", "kalshi": HEALTH_AUTH_FAILURE}},
            "auth_failure",
        ),
        ({"provider_cancels": 1}, "incomplete_discovery"),
        (
            {"venue_health": {"matchbook": "ok", "polymarket": "ok", "kalshi": "degraded"}},
            "incomplete_discovery",
        ),
        (
            {"venue_health": {"matchbook": "ok", "polymarket": "ok", "kalshi": "unknown"}},
            "incomplete_discovery",
        ),
        (
            {"venue_health": {"matchbook": "ok", "polymarket": "ok", "kalshi": ""}},
            "incomplete_discovery",
        ),
        (
            {"venue_health": {"matchbook": "ok", "polymarket": "ok"}},
            "incomplete_discovery",
        ),
        (
            {
                "series_results": {
                    "kalshi": [{"series": "KXBAD", "status": "discovery_timeout", "retryable": True}]
                }
            },
            "retry_series_partial",
        ),
    ],
)
def test_canonical_work_set_authority_fails_closed_on_partial_evidence(
    kwargs: dict,
    reason: str,
) -> None:
    payload = {
        "cluster_ids": list(CURRENT_CLUSTER_IDS),
        "clustering_truncated": False,
        "retry_series": None,
        "venue_health": {"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"},
        "enabled": {VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI},
        "issues": [],
        "series_results": {},
        "provider_cancels": 0,
    }
    payload.update(kwargs)
    authoritative, partial_reason = canonical_work_set_authority(**payload)
    assert authoritative is False
    assert partial_reason == reason
    assert partial_reason in CANONICAL_WORK_SET_PARTIAL_REASONS


def test_canonical_work_set_authority_accepts_complete_reused_snapshot() -> None:
    authoritative, partial_reason = canonical_work_set_authority(
        cluster_ids=list(CURRENT_CLUSTER_IDS),
        clustering_truncated=False,
        retry_series={},
        venue_health={"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"},
        enabled={VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI},
        issues=[],
        series_results={
            "kalshi": [{"series": "KX01", "status": "ok", "retryable": False, "event_count": 1}]
        },
        provider_cancels=0,
    )
    assert authoritative is True
    assert partial_reason is None


def test_canonical_work_set_authority_accepts_healthy_empty_cluster_set() -> None:
    authoritative, partial_reason = canonical_work_set_authority(
        cluster_ids=[],
        clustering_truncated=False,
        retry_series={},
        venue_health={"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"},
        enabled={VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI},
        issues=[],
        series_results={},
        provider_cancels=0,
    )
    assert authoritative is True
    assert partial_reason is None


def test_degraded_enabled_venue_does_not_retire_pending_orphans(tmp_path: Path) -> None:
    store = SqliteUniverseCheckpointStore(tmp_path / "issue305-degraded.sqlite")
    _seed_owner_live_generation_26(store)
    _clock, coordinator = _restore_owner_live(store)
    authoritative, reason = canonical_work_set_authority(
        cluster_ids=list(CURRENT_CLUSTER_IDS),
        clustering_truncated=False,
        retry_series=None,
        venue_health={"matchbook": "ok", "polymarket": "ok", "kalshi": "degraded"},
        enabled={VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI},
        issues=[],
        series_results={},
        provider_cancels=0,
    )
    assert authoritative is False
    assert reason == "incomplete_discovery"
    coordinator.record_universe_work_set(
        CURRENT_CLUSTER_IDS,
        authoritative=authoritative,
        partial_reason=reason,
    )
    assert {coordinator._universe_work[item].state for item in ORPHAN_IDS} == {SWEEP_PENDING}
    assert coordinator._canonical_counts_unlocked()["canonical_remaining"] == 2
    assert coordinator._universe_generation_started_at is not None


def test_unknown_enabled_venue_does_not_retire_pending_orphans(tmp_path: Path) -> None:
    store = SqliteUniverseCheckpointStore(tmp_path / "issue305-unknown.sqlite")
    _seed_owner_live_generation_26(store)
    _clock, coordinator = _restore_owner_live(store)
    authoritative, reason = canonical_work_set_authority(
        cluster_ids=list(CURRENT_CLUSTER_IDS),
        clustering_truncated=False,
        retry_series=None,
        venue_health={"matchbook": "ok", "polymarket": "ok", "kalshi": "unknown"},
        enabled={VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI},
        issues=[],
        series_results={},
        provider_cancels=0,
    )
    assert authoritative is False
    assert reason == "incomplete_discovery"
    coordinator.record_universe_work_set(
        CURRENT_CLUSTER_IDS,
        authoritative=authoritative,
        partial_reason=reason,
    )
    assert {coordinator._universe_work[item].state for item in ORPHAN_IDS} == {SWEEP_PENDING}
    assert coordinator._universe_sweep_is_complete_unlocked() is False


def test_healthy_empty_authoritative_set_retires_pending_and_closes_generation(
    tmp_path: Path,
) -> None:
    store = SqliteUniverseCheckpointStore(tmp_path / "issue305-healthy-empty.sqlite")
    _seed_owner_live_generation_26(store)
    clock, coordinator = _restore_owner_live(store)
    authoritative, reason = canonical_work_set_authority(
        cluster_ids=[],
        clustering_truncated=False,
        retry_series=None,
        venue_health={"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"},
        enabled={VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI},
        issues=[],
        series_results={},
        provider_cancels=0,
    )
    assert authoritative is True
    assert reason is None
    coordinator.record_universe_work_set([], authoritative=True)
    for orphan in ORPHAN_IDS:
        unit = coordinator._universe_work[orphan]
        assert unit.state == SWEEP_STALE_ORPHAN
        assert unit.state != SWEEP_EVALUATED
        assert unit.reason == STALE_ORPHAN_REASON
    assert coordinator._canonical_counts_unlocked()["canonical_evaluated"] == 75
    assert coordinator._canonical_counts_unlocked()["canonical_remaining"] == 0
    assert coordinator._universe_sweep_is_complete_unlocked() is True
    coordinator.record_report(
        _zero_work_complete_report(when=clock.now).model_copy(
            update={
                "scan_diagnostics": {
                    "completeness": UNIVERSE_COMPLETENESS_COMPLETE,
                    "evaluated_count": 0,
                    "not_evaluated_count": 0,
                    "generation_resume": True,
                    "universe_generation_id": 26,
                    "clusters_before_resume": 0,
                    "skipped_by_resume_count": 0,
                    "canonical_work_total": 0,
                    "canonical_work_set_authoritative": True,
                    "canonical_work_set_partial_reason": None,
                }
            }
        ),
        scan_lane=ScanLane.UNIVERSE,
    )
    assert coordinator._universe_generation_started_at is None
    assert coordinator.status.universe.worker_state == WORKER_COMPLETE
    idle = coordinator.plan_universe_tick(now=clock.now)
    assert idle.reason == "universe_cooldown"


def test_unhealthy_empty_cluster_set_does_not_retire_pending(tmp_path: Path) -> None:
    store = SqliteUniverseCheckpointStore(tmp_path / "issue305-unhealthy-empty.sqlite")
    _seed_owner_live_generation_26(store)
    _clock, coordinator = _restore_owner_live(store)
    authoritative, reason = canonical_work_set_authority(
        cluster_ids=[],
        clustering_truncated=False,
        retry_series=None,
        venue_health={"matchbook": "ok", "polymarket": "ok", "kalshi": "degraded"},
        enabled={VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI},
        issues=[],
        series_results={},
        provider_cancels=0,
    )
    assert authoritative is False
    assert reason == "incomplete_discovery"
    coordinator.record_universe_work_set([], authoritative=False, partial_reason=reason)
    assert {coordinator._universe_work[item].state for item in ORPHAN_IDS} == {SWEEP_PENDING}
    assert coordinator._canonical_counts_unlocked()["canonical_remaining"] == 2
    assert coordinator._universe_generation_started_at is not None


def test_fresh_generation_repopulates_fixture_radar_and_equivalents(tmp_path: Path) -> None:
    store = SqliteUniverseCheckpointStore(tmp_path / "issue305-radar.sqlite")
    _seed_owner_live_generation_26(store)
    clock, coordinator = _restore_owner_live(store)
    assert coordinator.fixture_current_state().inventory(clock.now) == []
    assert coordinator.fixture_current_state().hot_identity_scope(clock.now) == []
    coordinator.record_universe_work_set(CURRENT_CLUSTER_IDS, authoritative=True)
    _rehydrate_restored_evaluated(coordinator)
    coordinator.record_report(_zero_work_complete_report(when=clock.now), scan_lane=ScanLane.UNIVERSE)

    clock.now = coordinator._next_universe_due
    plan = coordinator.plan_universe_tick(now=clock.now)
    assert plan.universe_generation_id == 27
    assert plan.generation_resume is False
    report = _qualifying_universe_report(when=clock.now, row=_market_row())
    coordinator.record_report(report, scan_lane=ScanLane.UNIVERSE)
    radar = coordinator.fixture_current_state().inventory(clock.now)
    assert radar
    assert any((item.matched_equivalent_count or 0) > 0 or item.qualifying_market_count for item in radar) or any(
        row.comparison_status.value == "matched_equivalent"
        for rows in (report.fixture_markets or {}).values()
        for row in rows
    )
    assert coordinator.status.universe.equivalent_markets >= 1 or report.fixture_markets


@pytest.mark.asyncio
async def test_fresh_generation_collector_discovers_non_empty_fixtures() -> None:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=FakeMatchbook(),
        polymarket=FakePolymarket(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    captured: dict[str, object] = {}

    def capture(ids: list[str], **kwargs: object) -> None:
        captured["ids"] = ids
        captured["kwargs"] = kwargs

    try:
        costs = matchbook_polymarket_costs("0.02", "0.02")
        fx = [FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))]
        report = await collector.collect_and_scan(
            venue_costs=costs,
            fx_snapshots=fx,
            maximum_execution_risk=100,
            scan_lane=ScanLane.UNIVERSE.value,
            generation_resume=False,
            universe_generation_id=27,
            enabled_venues=(VenueName.MATCHBOOK, VenueName.POLYMARKET),
            on_canonical_work_set=capture,
        )
        assert report.discovered_fixtures
        assert captured["ids"]
        kwargs = captured["kwargs"]
        assert isinstance(kwargs, dict)
        assert kwargs.get("authoritative") is True
        assert report.scan_diagnostics["canonical_work_set_authoritative"] is True
    finally:
        repository.close()


def test_incompatible_checkpoint_semantics_version_starts_fresh(tmp_path: Path) -> None:
    store = SqliteUniverseCheckpointStore(tmp_path / "issue305-version.sqlite")
    _seed_owner_live_generation_26(store)
    payload = store.load()
    assert payload is not None
    payload["semantics_version"] = UNIVERSE_CHECKPOINT_SEMANTICS_VERSION + 1
    store.save(payload, updated_at=NOW.isoformat())

    clock = FakeClock(NOW)
    restarted = LiveRefreshCoordinator(clock=clock, universe_checkpoint_store=store)
    restarted._clock = clock
    restarted.configure_from_settings()
    assert restarted._universe_generation_started_at is None
    assert restarted._universe_work == {}
    assert store.load() is None
    restarted._next_universe_due = clock.now
    plan = restarted.plan_universe_tick(now=clock.now)
    assert plan.lane == ScanLane.UNIVERSE.value
    assert plan.generation_resume is False
    assert plan.skip_event_ids == []


def test_compatible_retry_wait_checkpoint_still_restores(tmp_path: Path) -> None:
    store = SqliteUniverseCheckpointStore(tmp_path / "issue305-retry.sqlite")
    first = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    first.configure_from_settings()
    first.record_universe_work_set(["retry-b"])
    first.record_universe_fixture_progress(
        None,
        _universe_fixture("retry-b", evaluation="market_fetch_unavailable"),
        [],
        [],
    )
    assert first._universe_work["retry-b"].state == SWEEP_RETRY_WAIT
    payload = store.load()
    assert payload is not None
    assert payload["semantics_version"] == UNIVERSE_CHECKPOINT_SEMANTICS_VERSION
    assert payload.get("report") in (None, {}, [])
    assert payload.get("discovery_snapshot") in (None, {}, [])

    restarted = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    restarted.configure_from_settings()
    restored = restarted._universe_work["retry-b"]
    assert restored.state == SWEEP_RETRY_WAIT
    assert restored.retryable is True
    assert restarted._universe_generation_started_at is not None
    assert restarted._universe_sweep_is_complete_unlocked() is False


def test_unversioned_legacy_checkpoint_invalidates_when_runtime_semantics_bumped(
    tmp_path: Path,
) -> None:
    store = SqliteUniverseCheckpointStore(tmp_path / "issue305-legacy-bump.sqlite")
    first = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    first.configure_from_settings()
    first.record_universe_work_set(["retry-b"])
    first.record_universe_fixture_progress(
        None,
        _universe_fixture("retry-b", evaluation="market_fetch_unavailable"),
        [],
        [],
    )
    payload = store.load()
    assert payload is not None
    payload.pop("semantics_version", None)
    store.save(payload, updated_at=NOW.isoformat())
    parsed = checkpoint_from_payload(store.load())
    assert parsed is None
    restarted = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    restarted.configure_from_settings()
    assert restarted._universe_generation_started_at is None
    assert restarted._universe_work == {}
    assert store.load() is None


@pytest.mark.asyncio
async def test_three_generation_continuous_worker_closes_and_replans() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator._next_hot_due = NOW + timedelta(days=1)
    coordinator._next_universe_due = NOW
    fixtures = [_universe_fixture("gen-a"), _universe_fixture("gen-b")]
    closed: list[int] = []
    for expected in (1, 2, 3):
        plan = coordinator.plan_universe_tick(now=clock.now)
        if plan.lane == "idle":
            clock.now = coordinator._next_universe_due or clock.now
            plan = coordinator.plan_universe_tick(now=clock.now)
        assert plan.lane == ScanLane.UNIVERSE.value
        assert plan.universe_generation_id == expected
        assert plan.generation_resume is False
        assert plan.skip_event_ids == []

        async def runner(payload=fixtures):
            when = clock.now
            return _report(payload, when=when, scan_lane=ScanLane.UNIVERSE.value).model_copy(
                update={
                    "completed_at": when + timedelta(seconds=1),
                    "scan_diagnostics": {"completeness": UNIVERSE_COMPLETENESS_COMPLETE},
                }
            )

        await coordinator.run_cycle(runner, timeout_seconds=None, scan_lane=ScanLane.UNIVERSE)
        assert coordinator._universe_generation_started_at is None
        closed.append(coordinator._universe_closed_generation_id)
        clock.now = coordinator._next_universe_due or clock.now
    assert closed == [1, 2, 3]


def test_retry_backoff_caps_remain_2_5_10_and_hot_stays_independent(tmp_path: Path) -> None:
    assert universe_work_retry_backoff_seconds(1) == 2.0
    assert universe_work_retry_backoff_seconds(2) == 5.0
    assert universe_work_retry_backoff_seconds(3) == 10.0
    assert universe_work_retry_backoff_seconds(9) == 10.0
    assert get_settings().paper_universe_discovery_interval_seconds == 1800

    store = SqliteUniverseCheckpointStore(tmp_path / "issue305-hot.sqlite")
    _seed_owner_live_generation_26(store)
    clock, coordinator = _restore_owner_live(store)
    coordinator._next_hot_due = clock.now
    hot = coordinator.plan_hot_tick(now=clock.now)
    assert hot.lane in {ScanLane.HOT.value, "idle"}
    coordinator.record_universe_work_set(CURRENT_CLUSTER_IDS, authoritative=True)
    _rehydrate_restored_evaluated(coordinator)
    coordinator.record_report(_zero_work_complete_report(when=clock.now), scan_lane=ScanLane.UNIVERSE)
    coordinator._next_hot_due = clock.now
    hot_during_cooldown = coordinator.plan_hot_tick(now=clock.now)
    universe_during_cooldown = coordinator.plan_universe_tick(now=clock.now)
    assert universe_during_cooldown.reason == "universe_cooldown"
    assert hot_during_cooldown.lane in {ScanLane.HOT.value, "idle"}
    assert hot_during_cooldown.reason != "universe_in_progress"
