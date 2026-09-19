from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, Field

from sports_hedge.application.hot_market_relationships import HotMarketRelationship
from sports_hedge.application.collector import (
    DIAGNOSTIC_PROVIDERS,
    DIAGNOSTIC_STAGES,
    UNIVERSE_COMPLETENESS_STALE_GENERATION_STATE,
    CollectionReport,
    DiscoveredFixture,
    FixtureDetailReadModel,
)
from sports_hedge.application.price_engine import (
    CataloguePriceEngine,
    PriceEnginePriority,
)
from sports_hedge.application.universe_checkpoint import (
    SERIES_TERMINAL_STATES,
    STALE_ORPHAN_REASON,
    SWEEP_EVALUATED,
    SWEEP_FINAL_FAILED,
    SWEEP_OK,
    SWEEP_PENDING,
    SWEEP_RUNNING,
    SWEEP_RETRY_WAIT,
    SWEEP_SKIPPED_UNSUPPORTED,
    SWEEP_STALE_ORPHAN,
    SWEEP_TERMINAL_STATES,
    UNIVERSE_CHECKPOINT_SEMANTICS_VERSION,
    SeriesWorkUnit,
    SweepWorkUnit,
    UniverseCheckpointTooLarge,
    UniverseGenerationCheckpoint,
    checkpoint_from_payload,
    collection_report_snapshot,
    discovery_event_snapshot,
    merge_series_reports,
    series_work_key,
    universe_provider_backoff_seconds,
    universe_work_retry_backoff_seconds,
)
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.lane_venues import (
    MALFORMED_VENUE_SETTINGS_WARNING,
    LaneVenueParticipation,
    LaneVenueSet,
    default_operator_venues,
    is_provider_health_failure,
    last_scan_venue_clause,
    participation_from_lists,
)
from sports_hedge.application.fixture_clusters import (
    cluster_identity_aliases,
    cluster_member_events,
)
from sports_hedge.application.provider_access import (
    HEALTH_AUTH_FAILURE,
    HEALTH_DISCOVERY_TIMEOUT,
    HEALTH_MARKET_TIMEOUT,
    HEALTH_UNAVAILABLE,
    get_shared_provider_access,
)
from sports_hedge.application.scanner_observability import (
    PriceEnginePublicStatus,
    ScannerObservabilitySink,
    empty_price_engine_status,
)
from sports_hedge.application.system_load import SystemLoadSummary, system_load_from_status
from sports_hedge.application.quote_freshness import require_aware_instant
from sports_hedge.application.scan_lanes import (
    UNIVERSE_MIN_CHUNK_SECONDS,
    WORKER_COMPLETE,
    WORKER_DEGRADED,
    WORKER_IDLE,
    WORKER_RUNNING,
    WORKER_WAITING,
    ScanLane,
    universe_chunk_wall_seconds,
)
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.audit import PaperScanCycleRecord
from sports_hedge.persistence.lane_venue_settings import (
    SqliteLaneVenueSettingsStore,
    get_lane_venue_settings_store,
    resolve_lane_venue_participation,
)
from sports_hedge.persistence.universe_checkpoint import SqliteUniverseCheckpointStore
from sports_hedge.venues.matchbook import MatchbookAuthError, MatchbookDiscoveryError

LOGGER = logging.getLogger(__name__)


class ScanCycleTimeout(TimeoutError):
    """Raised when a live-refresh cycle exceeds its bounded deadline."""


class CollectionRunnerTimeout(TimeoutError):
    """Raised when harvest expires while the child collection task is still running."""

    def __init__(self, leftover: asyncio.Task[Any] | None = None) -> None:
        super().__init__("collection runner harvest expired")
        self.leftover = leftover


class ExplicitCollectBusy(RuntimeError):
    """Raised when an explicit collect cannot start because a scheduled lane is active."""


# Collector soft-stops at the per-run cycle timeout minus a finalisation
# reserve. Coordinator waits this extra grace after the collector has
# already returned a (possibly partial) CollectionReport.
#
# Owner-Windows `scan_cycle_timeout after 30s` is the *full scheduled runner*
# (leftover collect + HTTP aclose + persist/auto-capture) inside one wait.
# Python 3.12 `asyncio.wait_for(coro)` uses `timeouts.timeout()` and awaits
# the coroutine on the current task. If that coroutine catches CancelledError
# and returns, wait_for returns the value — leftover assembly is not by
# itself TimeoutError. Two overrun shapes:
# 1. Timeout fires *during* persist/aclose after a timely leftover → TimeoutError.
# 2. Leftover swallows cancel, then persist still runs and stretches past the
#    envelope without TimeoutError.
#
# Persist therefore stays *outside* `run_cycle`. Collection waits on a child
# task via asyncio.wait (not wait_for) so envelope expiry cancels the child
# rather than the scheduler tick, and leftover + aclose finish inside harvest.
SCAN_CYCLE_RETURN_GRACE_SECONDS = 5.0
SCAN_CYCLE_PARTIAL_HARVEST_SECONDS = 0.8
UNIVERSE_ORPHAN_TASK_WARN_LIMIT = 3
UNIVERSE_CHECKPOINT_FLUSH_FIXTURE_THRESHOLD = 20


class LaneRefreshStatus(BaseModel):
    cadence_seconds: int
    cycle_timeout_seconds: float | None = None
    generation_budget_seconds: float | None = None
    generation_work_used_s: float = 0
    chunk_last_duration_ms: int | None = Field(default=None, ge=0)
    cycle_in_progress: bool = False
    worker_state: str = WORKER_IDLE
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_duration_ms: int | None = Field(default=None, ge=0)
    next_due_at: datetime | None = None
    fixture_count: int = 0
    lifecycle_hot_count: int = 0
    promoted_hot_count: int = 0
    evaluated_count: int = 0
    not_evaluated_count: int = 0
    discovered_total: int = 0
    remaining: int = 0
    matched_fixtures: int = 0
    equivalent_markets: int = 0
    near_count: int = 0
    positive_count: int = 0
    qualifying_count: int = 0
    hot_promotions: int = 0
    last_successful_fixture: str | None = None
    current_fixture: str | None = None
    sweep_id: str | None = None
    generation_id: int | None = None
    last_error: str | None = None
    last_diagnostics: dict[str, Any] | None = None
    last_persist_error: str | None = None
    persist_ok: bool | None = None
    last_heartbeat_at: datetime | None = None
    last_plan_reason: str | None = None
    degraded: bool = False
    resume_cursor: str | None = None
    operator_summary: str | None = None
    active_venues: list[VenueName] = Field(
        default_factory=lambda: list(default_operator_venues())
    )
    pending_venues: list[VenueName] = Field(
        default_factory=lambda: list(default_operator_venues())
    )
    comparison_ready: bool = True
    venue_warning: str | None = None
    applies_next_cycle: bool = False
    venue_health: dict[str, str] = Field(default_factory=dict)
    operation_health: dict[str, Any] = Field(default_factory=dict)
    raw_events_discovered_by_venue: dict[str, int] = Field(default_factory=dict)
    canonical_work_total: int = 0
    canonical_evaluated: int = 0
    canonical_retryable: int = 0
    canonical_final_failed: int = 0
    canonical_stale_orphan: int = 0
    canonical_remaining: int = 0
    series_work_total: int = 0
    series_ok: int = 0
    series_retryable: int = 0
    series_final_failed: int = 0
    series_skipped: int = 0


class LiveRefreshStatus(BaseModel):
    discovery_source: VenueName = VenueName.MATCHBOOK
    discovery_mode: str = "venue_union"
    matching_venue: VenueName | None = VenueName.POLYMARKET
    matching_venues: list[VenueName] = Field(
        default_factory=lambda: [VenueName.POLYMARKET, VenueName.KALSHI]
    )
    server_loop_enabled: bool
    paper_autofill_enabled: bool = False
    paper_auto_unwind_enabled: bool = False
    interval_seconds: int = Field(ge=15, le=300)
    cycle_in_progress: bool = False
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_duration_ms: int | None = Field(default=None, ge=0)
    last_error: str | None = None
    last_matched_event_pairs: int | None = None
    last_matched_market_pairs: int | None = None
    last_paper_decisions: int | None = None
    last_issue_count: int | None = None
    skipped_out_of_scope: int | None = None
    operator_summary: str | None = None
    config_warnings: list[str] = Field(default_factory=list)
    venue_health: dict[str, str] = Field(default_factory=dict)
    live_scores: str = "unavailable_unless_matchbook_payload_includes_scores"
    discovered_fixtures: list[DiscoveredFixture] = Field(default_factory=list)
    hot: LaneRefreshStatus = Field(
        default_factory=lambda: LaneRefreshStatus(
            cadence_seconds=30,
            cycle_timeout_seconds=25,
        )
    )
    universe: LaneRefreshStatus = Field(
        default_factory=lambda: LaneRefreshStatus(
            cadence_seconds=8,
            # Per-chunk watchdog, not a generation lifetime. UNIVERSE generations
            # remain resumable/unbounded (Tenet 19 / Issue #328).
            cycle_timeout_seconds=150,
            generation_budget_seconds=150,
        )
    )
    background: LaneRefreshStatus = Field(
        default_factory=lambda: LaneRefreshStatus(
            cadence_seconds=180,
            cycle_timeout_seconds=None,
        )
    )
    price_engine: PriceEnginePublicStatus = Field(default_factory=empty_price_engine_status)
    venue_participation: LaneVenueParticipation | None = None
    recent_scan_cycles: list[PaperScanCycleRecord] = Field(default_factory=list)
    provider_access: dict[str, Any] = Field(default_factory=dict)
    system_load: SystemLoadSummary = Field(default_factory=SystemLoadSummary)


class DualCadencePlan(BaseModel):
    lane: Literal["hot", "universe", "background", "idle"]
    collector_timeout_seconds: float | None = None
    coordinator_timeout_seconds: float | None = None
    identity_scope: list[str] | None = None
    resume_cursor: str | None = None
    skip_event_ids: list[str] = Field(default_factory=list)
    universe_generation_id: int = 0
    generation_resume: bool = False
    known_source_events: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    hot_market_relationships: dict[str, list[HotMarketRelationship]] = Field(
        default_factory=dict
    )
    discovery_snapshot: dict[str, list[dict[str, Any]]] | None = None
    reuse_discovery: bool = False
    retry_series: dict[str, list[str]] = Field(default_factory=dict)
    unbounded_cycle: bool = False
    sweep_id: str | None = None
    enabled_venues: list[VenueName] = Field(
        default_factory=lambda: list(default_operator_venues())
    )
    reason: str = ""


def _schedule_capped_retry(
    unit: SweepWorkUnit | SeriesWorkUnit,
    *,
    reason: str,
    scanned: datetime,
) -> None:
    """Keep transient provider failures retryable. Attempt count is telemetry.

    Backoff indexes into a capped table; it never becomes FINAL_FAILED solely
    because the attempt count grew.
    """

    unit.attempt_count += 1
    unit.reason = reason
    unit.state = SWEEP_RETRY_WAIT
    unit.retryable = True
    unit.next_retry_at = scanned + timedelta(
        seconds=universe_work_retry_backoff_seconds(unit.attempt_count)
    )


class _CountedRLock:
    """RLock that exposes whether any thread currently holds it.

    Checkpoint SQLite I/O must observe `held is False`.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._depth = 0
        self._owner: int | None = None

    def __enter__(self) -> _CountedRLock:
        self.acquire()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.release()

    def acquire(self, blocking: bool = True, timeout: float = -1) -> bool:
        acquired = self._lock.acquire(blocking, timeout)
        if acquired:
            self._depth += 1
            self._owner = threading.get_ident()
        return acquired

    def release(self) -> None:
        self._depth -= 1
        if self._depth == 0:
            self._owner = None
        self._lock.release()

    @property
    def held(self) -> bool:
        return self._depth > 0

    @property
    def held_by_current_thread(self) -> bool:
        return self._depth > 0 and self._owner == threading.get_ident()


@dataclass(frozen=True)
class _UniverseCheckpointWrite:
    token: int
    action: Literal["save", "clear"]
    payload: dict[str, Any] | None = None
    updated_at: str | None = None


class LiveRefreshCoordinator:
    """Independent HOT and UNIVERSE workers with scoped locks (Tenet 19)."""

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        venue_settings_store: SqliteLaneVenueSettingsStore | None = None,
        universe_checkpoint_store: SqliteUniverseCheckpointStore | None = None,
        catalogue_store: Any | None = None,
        price_engine: CataloguePriceEngine | None = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._hot_lock = asyncio.Lock()
        self._universe_lock = asyncio.Lock()
        self._background_lock = asyncio.Lock()
        self._state_lock = _CountedRLock()
        self._universe_checkpoint_io_lock = threading.Lock()
        self._task: asyncio.Task[None] | None = None
        self._hot_task: asyncio.Task[None] | None = None
        self._universe_task: asyncio.Task[None] | None = None
        self._background_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_request: dict[str, Any] = {}
        self._last_report: CollectionReport | None = None
        self._fixture_state = FixtureCurrentStateStore()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._hot_in_progress = False
        self._universe_in_progress = False
        self._background_in_progress = False
        self._manual_hot_in_progress = False
        self._next_hot_due: datetime | None = None
        self._next_universe_due: datetime | None = None
        self._next_background_due: datetime | None = None
        self._catalogue_store = catalogue_store
        self._price_engine = price_engine
        self._observability = ScannerObservabilitySink()
        self._hot_due_started: datetime | None = None
        self._universe_generation_id = 0
        self._universe_generation_started_at: datetime | None = None
        self._universe_work_used = 0.0
        self._universe_cursor: str | None = None
        self._universe_evaluated_ids: set[str] = set()
        self._universe_needs_rehydration: set[str] = set()
        self._universe_rehydration_retry_at: dict[str, datetime] = {}
        self._universe_rehydration_attempts: dict[str, int] = {}
        self._universe_progress_generation_id: int | None = None
        self._universe_closed_generation_id = 0
        self._universe_closed_work_used = 0.0
        self._universe_closed_cursor: str | None = None
        self._universe_closed_evaluated_count = 0
        self._universe_budget_paused = False
        self._universe_retry_at: datetime | None = None
        self._universe_provider_failures = 0
        self._universe_last_report_snapshot: dict[str, Any] | None = None
        self._universe_sweep_id: str | None = None
        self._universe_discovery_snapshot: dict[str, list[dict[str, Any]]] | None = None
        self._universe_discovered_total = 0
        self._universe_failed_ids: dict[str, str] = {}
        self._universe_skipped_ids: dict[str, str] = {}
        self._universe_last_successful: str | None = None
        self._universe_current_fixture: str | None = None
        self._universe_matched_fixtures = 0
        self._universe_equivalent_markets = 0
        self._universe_near_count = 0
        self._universe_positive_count = 0
        self._universe_qualifying_count = 0
        self._universe_hot_promotions = 0
        self._universe_work: dict[str, SweepWorkUnit] = {}
        self._universe_raw_events: dict[str, int] = {}
        self._universe_series_results: dict[str, list[dict[str, Any]]] = {}
        self._universe_series_work: dict[str, SeriesWorkUnit] = {}
        self._universe_series_applied_this_cycle: set[str] = set()
        self._universe_chunk_seq = 0
        self._universe_active_chunk_epoch: int | None = None
        self._universe_orphaned_tasks: list[asyncio.Task[Any]] = []
        self._universe_orphaned_chunk_count = 0
        self._universe_stale_callback_count = 0
        self._universe_checkpoint_store = universe_checkpoint_store
        self._universe_checkpoint_restored = False
        self._universe_checkpoint_dirty = False
        self._universe_checkpoint_unpersisted_fixtures = 0
        self._universe_checkpoint_write_seq = 0
        self._universe_checkpoint_persist_task: asyncio.Task[None] | None = None
        self._venue_store = venue_settings_store
        self._pending_participation = participation_from_lists(
            default_operator_venues(),
            default_operator_venues(),
            source="env_default",
            allow_empty=False,
        )
        self._cycle_hot_venues: tuple[VenueName, ...] | None = None
        self._cycle_universe_venues: tuple[VenueName, ...] | None = None
        self._cycle_enabled_venues: tuple[VenueName, ...] | None = None
        self.status = LiveRefreshStatus(
            server_loop_enabled=False,
            interval_seconds=30,
        )

    def now(self) -> datetime:
        return require_aware_instant(self._clock(), "now")

    def configure_from_settings(self, settings: Settings | None = None) -> None:
        resolved = settings or get_settings()
        hot_cadence = resolved.paper_live_refresh_hot_interval_seconds
        pending = resolve_lane_venue_participation(
            self._resolved_store(resolved),
            resolved,
        )
        with self._state_lock:
            self._pending_participation = pending
            self.status = self.status.model_copy(
                update={
                    "server_loop_enabled": resolved.paper_live_refresh_enabled,
                    "paper_autofill_enabled": resolved.paper_autofill_enabled,
                    "paper_auto_unwind_enabled": resolved.paper_auto_unwind_enabled,
                    "interval_seconds": hot_cadence,
                    "hot": self.status.hot.model_copy(
                        update={
                            "cadence_seconds": hot_cadence,
                            "cycle_timeout_seconds": float(
                                resolved.paper_scan_hot_cycle_timeout_seconds
                            ),
                        }
                    ),
                    "universe": self.status.universe.model_copy(
                        update={
                            "cadence_seconds": resolved.paper_universe_worker_cooldown_seconds,
                            "cycle_timeout_seconds": self._universe_chunk_collector_timeout(
                                now=self.now(), settings=resolved
                            ),
                            "generation_budget_seconds": float(
                                resolved.paper_scan_universe_generation_budget_seconds
                            ),
                        }
                    ),
                    "background": self.status.background.model_copy(
                        update={
                            "cadence_seconds": resolved.paper_live_refresh_universe_interval_seconds,
                        }
                    ),
                }
            )
            self._sync_venue_status_unlocked()
            self._ensure_due_times_unlocked(self.now(), resolved)
        self._restore_universe_checkpoint()

    def _resolved_store(self, settings: Settings | None = None) -> SqliteLaneVenueSettingsStore:
        if self._venue_store is None:
            self._venue_store = get_lane_venue_settings_store()
        return self._venue_store

    def bind_universe_checkpoint_store(self, store: SqliteUniverseCheckpointStore) -> None:
        with self._state_lock:
            self._universe_checkpoint_store = store
            self._universe_checkpoint_restored = False
        self._restore_universe_checkpoint()

    def bind_venue_store(self, store: SqliteLaneVenueSettingsStore) -> None:
        pending = resolve_lane_venue_participation(store)
        with self._state_lock:
            self._venue_store = store
            self._pending_participation = pending
            self._sync_venue_status_unlocked()

    def apply_venue_participation(
        self,
        hot: list[VenueName] | tuple[VenueName, ...],
        universe: list[VenueName] | tuple[VenueName, ...],
    ) -> LaneVenueParticipation:
        store = self._resolved_store()
        pending = store.save(hot, universe, source="operator")
        with self._state_lock:
            self._pending_participation = pending
            self._sync_venue_status_unlocked()
            return self._pending_participation

    def pending_venues_for(self, lane: ScanLane | str | None) -> tuple[VenueName, ...]:
        return self._pending_participation.venues_for(lane)

    def running_cycle_venues(self) -> tuple[VenueName, ...]:
        if self._cycle_enabled_venues is not None:
            return self._cycle_enabled_venues
        if self._hot_in_progress:
            return self._cycle_hot_venues or self._pending_participation.venues_for(ScanLane.HOT)
        if self._universe_in_progress:
            return self._cycle_universe_venues or self._pending_participation.venues_for(
                ScanLane.UNIVERSE
            )
        return self._pending_participation.venues_for(ScanLane.UNIVERSE)

    def _sync_venue_status(self) -> None:
        with self._state_lock:
            self._sync_venue_status_unlocked()

    def _sync_venue_status_unlocked(self) -> None:
        pending = self._pending_participation.with_warnings()
        hot_active = (
            self._cycle_hot_venues
            if self._hot_in_progress and self._cycle_hot_venues is not None
            else tuple(pending.hot)
        )
        universe_active = (
            self._cycle_universe_venues
            if self._universe_in_progress and self._cycle_universe_venues is not None
            else tuple(pending.universe)
        )
        hot_set = LaneVenueSet.from_enabled(
            hot_active,
            pending=pending.hot,
            in_progress=self._hot_in_progress,
        )
        universe_set = LaneVenueSet.from_enabled(
            universe_active,
            pending=pending.universe,
            in_progress=self._universe_in_progress,
        )
        warnings = [
            item
            for item in self.status.config_warnings
            if item != MALFORMED_VENUE_SETTINGS_WARNING
        ]
        diagnostic = pending.config_diagnostic
        if diagnostic and diagnostic not in warnings:
            warnings.append(diagnostic)
        hot_warning = hot_set.warning
        universe_warning = universe_set.warning
        if diagnostic:
            hot_warning = diagnostic if hot_warning is None else hot_warning
            universe_warning = diagnostic if universe_warning is None else universe_warning
        self.status = self.status.model_copy(
            update={
                "hot": self.status.hot.model_copy(
                    update={
                        "active_venues": hot_set.enabled,
                        "pending_venues": hot_set.pending,
                        "comparison_ready": hot_set.comparison_ready,
                        "venue_warning": hot_warning,
                        "applies_next_cycle": hot_set.applies_next_cycle,
                    }
                ),
                "universe": self.status.universe.model_copy(
                    update={
                        "active_venues": universe_set.enabled,
                        "pending_venues": universe_set.pending,
                        "comparison_ready": universe_set.comparison_ready,
                        "venue_warning": universe_warning,
                        "applies_next_cycle": universe_set.applies_next_cycle,
                    }
                ),
                "venue_participation": pending,
                "config_warnings": warnings,
            }
        )

    def remember_request(self, payload: dict[str, Any]) -> None:
        with self._state_lock:
            self._last_request = dict(payload)

    def last_request(self) -> dict[str, Any]:
        with self._state_lock:
            return dict(self._last_request)

    def reset(self) -> None:
        # Quarantine pre-reset observability before clearing current-state so
        # an already-running UI projection cannot resurrect emptied rows.
        # Drain is not awaited here: a blocked consumer must not deadlock
        # operator reset. Commit is generation-guarded instead.
        self._observability.reset()
        self._fixture_state.clear()
        orphans: list[asyncio.Task[Any]] = []
        with self._state_lock:
            self._last_request = {}
            self._last_report = None
            self._hot_in_progress = False
            self._universe_in_progress = False
            self._background_in_progress = False
            self._manual_hot_in_progress = False
            self._next_hot_due = None
            self._next_universe_due = None
            self._next_background_due = None
            self._hot_due_started = None
            self._universe_generation_id = 0
            self._universe_generation_started_at = None
            self._universe_work_used = 0.0
            self._universe_cursor = None
            self._universe_evaluated_ids = set()
            self._universe_needs_rehydration = set()
            self._universe_rehydration_retry_at = {}
            self._universe_rehydration_attempts = {}
            self._universe_progress_generation_id = None
            self._universe_closed_generation_id = 0
            self._universe_closed_work_used = 0.0
            self._universe_closed_cursor = None
            self._universe_closed_evaluated_count = 0
            self._universe_budget_paused = False
            self._universe_retry_at = None
            self._universe_provider_failures = 0
            self._universe_last_report_snapshot = None
            self._universe_sweep_id = None
            self._universe_discovery_snapshot = None
            self._universe_discovered_total = 0
            self._universe_failed_ids = {}
            self._universe_skipped_ids = {}
            self._universe_last_successful = None
            self._universe_current_fixture = None
            self._universe_matched_fixtures = 0
            self._universe_equivalent_markets = 0
            self._universe_near_count = 0
            self._universe_positive_count = 0
            self._universe_qualifying_count = 0
            self._universe_hot_promotions = 0
            self._universe_work = {}
            self._universe_raw_events = {}
            self._universe_series_results = {}
            self._universe_series_work = {}
            self._universe_series_applied_this_cycle = set()
            orphans = list(self._universe_orphaned_tasks)
            self._universe_orphaned_tasks = []
            self._universe_active_chunk_epoch = None
            self._universe_chunk_seq = 0
            self._universe_orphaned_chunk_count = 0
            self._universe_stale_callback_count = 0
            self._universe_checkpoint_dirty = True
            self._universe_checkpoint_unpersisted_fixtures = 0
            self._universe_checkpoint_restored = False
            self._cycle_hot_venues = None
            self._cycle_universe_venues = None
            self._cycle_enabled_venues = None
            self.status = LiveRefreshStatus(
                server_loop_enabled=False,
                interval_seconds=30,
            )
        self.flush_universe_checkpoint()
        self.configure_from_settings()
        self._drain_orphaned_collection_tasks(orphans)
        if self._price_engine is not None:
            self._price_engine.restart()

    def _ensure_due_times(self, now: datetime, settings: Settings | None = None) -> None:
        with self._state_lock:
            self._ensure_due_times_unlocked(now, settings)

    def _ensure_due_times_unlocked(
        self, now: datetime, settings: Settings | None = None
    ) -> None:
        resolved = settings or get_settings()
        if self._next_universe_due is None:
            # UNIVERSE generation 0 is due immediately on startup.
            self._next_universe_due = now
        if self._next_hot_due is None:
            self._next_hot_due = now
        if self._next_background_due is None:
            self._next_background_due = now
        self.status = self.status.model_copy(
            update={
                "hot": self.status.hot.model_copy(
                    update={
                        "next_due_at": self._next_hot_due,
                        "cadence_seconds": resolved.paper_live_refresh_hot_interval_seconds,
                    }
                ),
                "universe": self.status.universe.model_copy(
                    update={
                        "next_due_at": self._next_universe_due,
                        "cadence_seconds": resolved.paper_universe_worker_cooldown_seconds,
                        "resume_cursor": self._status_universe_cursor(),
                        "generation_work_used_s": round(self._status_universe_work_used(), 3),
                    }
                ),
            }
        )

    def plan_tick(
        self,
        now: datetime | None = None,
        settings: Settings | None = None,
    ) -> DualCadencePlan:
        """Compatibility planner: HOT if due, else UNIVERSE if due.

        The other lane being active is not a reason to idle. Concurrent
        workers call `plan_hot_tick` / `plan_universe_tick` directly.
        """

        resolved = settings or get_settings()
        evaluated = require_aware_instant(now or self.now(), "now")
        hot_plan = self.plan_hot_tick(now=evaluated, settings=resolved)
        if hot_plan.lane == ScanLane.HOT.value:
            return hot_plan
        universe_plan = self.plan_universe_tick(now=evaluated, settings=resolved)
        if universe_plan.lane == ScanLane.UNIVERSE.value:
            return universe_plan
        return universe_plan if universe_plan.reason else hot_plan

    def plan_hot_tick(
        self,
        now: datetime | None = None,
        settings: Settings | None = None,
    ) -> DualCadencePlan:
        resolved = settings or get_settings()
        evaluated = require_aware_instant(now or self.now(), "now")
        with self._state_lock:
            self._ensure_due_times_unlocked(evaluated, resolved)
            if self._hot_in_progress or self._manual_hot_in_progress:
                return DualCadencePlan(lane="idle", reason="hot_in_progress")
            next_hot_due = self._next_hot_due
        hot_due = next_hot_due is not None and evaluated >= next_hot_due
        hot_scope = self._hot_identity_scope(evaluated, resolved)
        if hot_due and hot_scope:
            return self._hot_plan(hot_scope, resolved, reason="hot_due")
        if hot_due:
            return DualCadencePlan(lane="idle", reason="hot_scope_empty")
        return DualCadencePlan(lane="idle", reason="waiting")

    def plan_background_tick(
        self,
        now: datetime | None = None,
        settings: Settings | None = None,
    ) -> DualCadencePlan:
        resolved = settings or get_settings()
        evaluated = require_aware_instant(now or self.now(), "now")
        with self._state_lock:
            self._ensure_due_times_unlocked(evaluated, resolved)
            if self._background_in_progress:
                return DualCadencePlan(lane="idle", reason="background_in_progress")
            next_due = self._next_background_due
        if next_due is not None and evaluated >= next_due:
            return DualCadencePlan(
                lane="background",
                collector_timeout_seconds=None,
                coordinator_timeout_seconds=None,
                enabled_venues=list(self.pending_venues_for(ScanLane.HOT)),
                reason="background_due",
            )
        return DualCadencePlan(lane="idle", reason="waiting")

    def bind_catalogue_store(self, store: Any) -> None:
        self._catalogue_store = store
        if self._price_engine is not None:
            self._price_engine.catalogue_store = store

    def bind_price_engine(self, engine: CataloguePriceEngine) -> None:
        self._price_engine = engine
        engine.fixture_state = self._fixture_state
        engine.observability = self._observability
        if engine.catalogue_store is None:
            engine.catalogue_store = self._catalogue_store

    def price_engine(self) -> CataloguePriceEngine:
        if self._price_engine is None:
            self._price_engine = CataloguePriceEngine(
                catalogue_store=self._catalogue_store,
                fixture_state=self._fixture_state,
                clock=self.now,
                observability=self._observability,
            )
        else:
            self._price_engine.observability = self._observability
            if self._price_engine.fixture_state is None:
                self._price_engine.fixture_state = self._fixture_state
        return self._price_engine

    async def run_price_engine_slice(
        self,
        priority: PriceEnginePriority,
        *,
        slice_wall_seconds: float | None = None,
        matchbook: Any = None,
        kalshi: Any = None,
        paper_scan: Any = None,
        venue_costs: list[Any] | None = None,
        fx_snapshots: list[Any] | None = None,
    ) -> Any:
        """Price ACTIVE catalogue rows without holding the UNIVERSE lock."""

        engine = self.price_engine()
        if matchbook is not None:
            engine.matchbook = matchbook
        if kalshi is not None:
            engine.kalshi = kalshi
        if paper_scan is not None:
            engine.paper_scan = paper_scan
        if venue_costs is not None:
            engine.venue_costs = list(venue_costs)
        if fx_snapshots is not None:
            engine.fx_snapshots = list(fx_snapshots)
        engine.fixture_state = self._fixture_state
        result = await engine.run_slice(priority, slice_wall_seconds=slice_wall_seconds)
        self._apply_price_engine_slice_status(priority, result)
        if priority is PriceEnginePriority.BACKGROUND:
            with self._state_lock:
                settings = get_settings()
                self._next_background_due = self.now() + timedelta(
                    seconds=settings.paper_live_refresh_universe_interval_seconds
                )
        return result

    def _apply_price_engine_slice_status(self, priority: PriceEnginePriority, result: Any) -> None:
        """Record HOT/BACKGROUND engine truth without leftover-as-exhausted assembly."""

        engine_status = self.price_engine().public_status()
        tier = (
            engine_status.hot
            if priority is PriceEnginePriority.HOT
            else engine_status.background
        )
        last_error = None
        for issue in list(getattr(result, "issues", []) or []):
            detail = getattr(issue, "detail", None)
            if detail and str(detail) != "scan_budget_exhausted":
                last_error = str(detail)
        lane_update = {
            "evaluated_count": tier.evaluated,
            "not_evaluated_count": (
                tier.not_started_this_cadence + tier.retry_wait + tier.deferred
            ),
            "operation_health": dict(tier.operation_health),
            "venue_health": dict(tier.venue_health),
            "degraded": any(
                is_provider_health_failure(value) for value in tier.venue_health.values()
            ),
            "last_diagnostics": {
                "price_engine": True,
                "priority": priority.value,
                "working_set": tier.working_set,
                "due": tier.due,
                "queued": tier.queued,
                "in_flight": tier.in_flight,
                "evaluated": tier.evaluated,
                "evaluated_definition": tier.evaluated_definition,
                "retry_wait": tier.retry_wait,
                "deferred": tier.deferred,
                "provider_capacity_saturated": tier.provider_capacity_saturated,
                "not_started_this_cadence": tier.not_started_this_cadence,
                "revalidation_needed": tier.revalidation_needed,
                "persist_failures": list(getattr(result, "persist_failures", []) or []),
            },
            "last_error": last_error,
            "worker_state": WORKER_RUNNING
            if (
                (priority is PriceEnginePriority.HOT and self._hot_in_progress)
                or (
                    priority is PriceEnginePriority.BACKGROUND
                    and self._background_in_progress
                )
            )
            else WORKER_IDLE,
        }
        with self._state_lock:
            if priority is PriceEnginePriority.HOT:
                hot = self.status.hot.model_copy(update=lane_update)
                background = self.status.background
            else:
                background = self.status.background.model_copy(update=lane_update)
                hot = self.status.hot
            self.status = self.status.model_copy(
                update={
                    "hot": hot,
                    "background": background,
                    "price_engine": engine_status,
                    "venue_health": _merge_top_level_venue_health(
                        hot.venue_health,
                        background.venue_health,
                        self.status.universe.venue_health,
                    ),
                }
            )

    def plan_universe_tick(
        self,
        now: datetime | None = None,
        settings: Settings | None = None,
    ) -> DualCadencePlan:
        resolved = settings or get_settings()
        evaluated = require_aware_instant(now or self.now(), "now")
        with self._state_lock:
            self._ensure_due_times_unlocked(evaluated, resolved)
            if self._universe_in_progress:
                return DualCadencePlan(lane="idle", reason="universe_in_progress")
            next_universe_due = self._next_universe_due
            universe_generation_started_at = self._universe_generation_started_at
            universe_retry_at = self._universe_retry_at
            (
                universe_cursor,
                skip_event_ids,
                universe_generation_id,
                generation_resume,
            ) = self._universe_plan_resume_state_unlocked(evaluated)
            snapshot = (
                dict(self._universe_discovery_snapshot)
                if self._universe_discovery_snapshot
                else None
            )
            sweep_id = self._universe_sweep_id
            universe_venues = list(self._pending_participation.venues_for(ScanLane.UNIVERSE))
            retry_series = self._due_retry_series_unlocked(evaluated)
        work_retry_at = self._earliest_retry_wait_unlocked(evaluated)
        if work_retry_at is not None and evaluated < work_retry_at:
            return DualCadencePlan(lane="idle", reason="universe_retry_wait")
        if universe_retry_at is not None and evaluated < universe_retry_at:
            return DualCadencePlan(lane="idle", reason="universe_provider_backoff")
        universe_due = universe_generation_started_at is not None or (
            next_universe_due is not None and evaluated >= next_universe_due
        )
        if not universe_due:
            if universe_generation_started_at is None and next_universe_due is not None:
                return DualCadencePlan(lane="idle", reason="universe_cooldown")
            return DualCadencePlan(lane="idle", reason="waiting")
        reuse = bool(snapshot) and generation_resume
        collector_timeout, coordinator_timeout = self._universe_chunk_timeouts(
            now=evaluated, settings=resolved
        )
        return DualCadencePlan(
            lane=ScanLane.UNIVERSE.value,
            collector_timeout_seconds=collector_timeout,
            coordinator_timeout_seconds=coordinator_timeout,
            resume_cursor=universe_cursor,
            skip_event_ids=skip_event_ids,
            universe_generation_id=universe_generation_id,
            generation_resume=generation_resume,
            discovery_snapshot=snapshot,
            reuse_discovery=reuse,
            retry_series=retry_series,
            unbounded_cycle=False,
            sweep_id=sweep_id,
            enabled_venues=universe_venues,
            reason="universe_sweep",
        )

    def _universe_chunk_timeouts(
        self,
        now: datetime | None = None,
        settings: Settings | None = None,
    ) -> tuple[float, float]:
        """Return (collector_timeout, coordinator_timeout) for one UNIVERSE chunk.

        Generation lifetime stays unbounded. Only the scheduled chunk is bounded.
        """

        resolved = settings or get_settings()
        evaluated = require_aware_instant(now or self.now(), "now")
        budget = float(resolved.paper_scan_universe_generation_budget_seconds)
        wall = universe_chunk_wall_seconds(
            now=evaluated,
            next_hot_due=self._next_hot_due,
            remaining_generation_budget=budget,
            safety_margin_seconds=float(resolved.paper_universe_hot_yield_safety_margin_seconds),
        )
        collector = float(wall if wall is not None else UNIVERSE_MIN_CHUNK_SECONDS)
        return collector, collector + SCAN_CYCLE_RETURN_GRACE_SECONDS

    def _universe_chunk_collector_timeout(
        self,
        now: datetime | None = None,
        settings: Settings | None = None,
    ) -> float:
        collector, _coordinator = self._universe_chunk_timeouts(now=now, settings=settings)
        return collector

    def _universe_chunk_coordinator_timeout(
        self,
        now: datetime | None = None,
        settings: Settings | None = None,
    ) -> float:
        _collector, coordinator = self._universe_chunk_timeouts(now=now, settings=settings)
        return coordinator

    def manual_hot_plan(
        self,
        now: datetime | None = None,
        settings: Settings | None = None,
    ) -> DualCadencePlan:
        """Build the same bounded identity/venue plan as a scheduled HOT scan.

        An empty known scope deliberately remains an empty HOT refresh. Manual
        operator intent must never fall through to universe-wide discovery.
        """

        resolved = settings or get_settings()
        evaluated = require_aware_instant(now or self.now(), "now")
        hot_scope = self._hot_identity_scope(evaluated, resolved)
        return self._hot_plan(hot_scope, resolved, reason="manual_hot")

    def _hot_identity_scope(self, now: datetime, settings: Settings) -> list[str]:
        return self._fixture_state.hot_identity_scope(
            now,
            hot_horizon=timedelta(minutes=settings.paper_hot_pre_kickoff_horizon_minutes),
            post_kickoff_unknown_horizon=timedelta(
                hours=settings.paper_hot_post_kickoff_unknown_horizon_hours
            ),
            post_kickoff_current_radar_ceiling=timedelta(
                hours=settings.paper_hot_post_kickoff_current_radar_ceiling_hours
            ),
        )

    def _hot_plan(
        self,
        hot_scope: list[str],
        settings: Settings,
        *,
        reason: str,
    ) -> DualCadencePlan:
        timeout = float(settings.paper_scan_hot_cycle_timeout_seconds)
        return DualCadencePlan(
            lane=ScanLane.HOT.value,
            collector_timeout_seconds=timeout,
            coordinator_timeout_seconds=timeout + SCAN_CYCLE_RETURN_GRACE_SECONDS,
            identity_scope=list(hot_scope),
            known_source_events=self._fixture_state.known_source_events(hot_scope),
            hot_market_relationships=self._fixture_state.hot_market_relationships(
                hot_scope, now=self.now()
            ),
            enabled_venues=list(self.pending_venues_for(ScanLane.HOT)),
            reason=reason,
        )

    def seconds_until_next_work(
        self,
        now: datetime | None = None,
        settings: Settings | None = None,
    ) -> float:
        hot = self.plan_hot_tick(now=now, settings=settings)
        universe = self.plan_universe_tick(now=now, settings=settings)
        if hot.lane != "idle" or universe.lane != "idle":
            return 0.0
        evaluated = require_aware_instant(now or self.now(), "now")
        candidates: list[float] = []
        if self._next_hot_due is not None:
            candidates.append((self._next_hot_due - evaluated).total_seconds())
        if self._universe_retry_at is not None:
            candidates.append((self._universe_retry_at - evaluated).total_seconds())
        work_retry = self._earliest_retry_wait_unlocked(evaluated)
        if work_retry is not None:
            candidates.append((work_retry - evaluated).total_seconds())
        if self._universe_generation_started_at is None and self._next_universe_due is not None:
            candidates.append((self._next_universe_due - evaluated).total_seconds())
        if not candidates:
            return float(self.status.interval_seconds)
        return max(0.05, min(candidates))

    def universe_due_immediately(self) -> bool:
        return self._next_universe_due is not None and self._universe_generation_id == 0

    def _universe_generation_open(self, now: datetime, settings: Settings) -> bool:
        del settings
        if self._universe_generation_started_at is not None:
            return True
        return self._next_universe_due is not None and now >= self._next_universe_due

    def _lane_lock(self, lane: ScanLane) -> asyncio.Lock:
        return self._hot_lock if lane is ScanLane.HOT else self._universe_lock

    async def run_cycle(
        self,
        runner,
        *,
        timeout_seconds: float | None = None,
        scan_lane: ScanLane | str | None = None,
    ) -> CollectionReport:
        settings = get_settings()
        lane = _coerce_lane(scan_lane)
        if timeout_seconds is None and lane is ScanLane.HOT:
            timeout: float | None = float(
                settings.paper_scan_hot_cycle_timeout_seconds + SCAN_CYCLE_RETURN_GRACE_SECONDS
            )
        elif timeout_seconds is None and lane is ScanLane.UNIVERSE:
            timeout = self._universe_chunk_coordinator_timeout(now=self.now(), settings=settings)
        elif timeout_seconds is None:
            timeout = None
        else:
            timeout = float(timeout_seconds)
        async with self._lane_lock(lane):
            started = self.now()
            self._mark_lane_started(lane, started)
            try:
                report = await _await_collection_runner(runner, timeout)
                self.record_report(report, scan_lane=lane)
                return report
            except TimeoutError as exc:
                leftover = getattr(exc, "leftover", None)
                self._adopt_leftover_collection_task(leftover, lane=lane)
                finished = self.now()
                message = (
                    f"scan_cycle_timeout after {timeout:g}s"
                    if timeout is not None
                    else "scan_cycle_timeout"
                )
                self._mark_lane_error(lane, started, finished, message)
                raise ScanCycleTimeout(message) from exc
            except Exception as exc:
                finished = self.now()
                self._mark_lane_error(lane, started, finished, str(exc))
                raise
            finally:
                lane_busy = (
                    self.status.hot.cycle_in_progress
                    if lane is ScanLane.HOT
                    else self.status.universe.cycle_in_progress
                )
                if lane_busy:
                    finished = self.now()
                    self._mark_lane_error(
                        lane,
                        started,
                        finished,
                        self.status.last_error or "scan_cycle_abandoned",
                    )
                if lane is ScanLane.UNIVERSE:
                    await self._await_universe_checkpoint_persist()

    def scheduled_hot_active(self) -> bool:
        return (
            self._hot_lock.locked()
            or self._hot_in_progress
            or self._manual_hot_in_progress
            or self.status.hot.cycle_in_progress
        )

    def scheduled_universe_active(self) -> bool:
        return (
            self._universe_lock.locked()
            or self._universe_in_progress
            or self.status.universe.cycle_in_progress
        )

    def scheduled_collection_active(self) -> bool:
        return self.scheduled_hot_active() or self.scheduled_universe_active()

    def explicit_collect_timeout_seconds(self, settings: Settings | None = None) -> float:
        """Coordinator envelope for manual diagnostic collect.

        Scheduled Fast/Full keep their own HOT 25s / UNIVERSE-chunk budgets.
        Advanced full diagnostic is a bounded one-shot and must return before
        the frontend's 60s PAPER_COLLECTION_TIMEOUT_MS. Scheduled UNIVERSE
        generations stay unbounded across chunks; each scheduled chunk uses
        `universe_chunk_wall_seconds` rather than awaiting the runner forever.
        """

        resolved = settings or get_settings()
        return float(
            resolved.paper_scan_manual_diagnostic_timeout_seconds
            + SCAN_CYCLE_RETURN_GRACE_SECONDS
        )

    async def run_manual_hot(
        self,
        runner,
        *,
        timeout_seconds: float | None = None,
    ) -> CollectionReport:
        """Run a manual HOT refresh without moving scheduled lane due-times."""

        if self._manual_hot_in_progress:
            raise ExplicitCollectBusy("manual HOT refresh in progress")
        if self.scheduled_hot_active():
            raise ExplicitCollectBusy("HOT scan in progress")
        settings = get_settings()
        timeout = float(
            settings.paper_scan_hot_cycle_timeout_seconds + SCAN_CYCLE_RETURN_GRACE_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        )
        with self._state_lock:
            self._manual_hot_in_progress = True
        started = self.now()
        try:
            async with self._hot_lock:
                if self._hot_in_progress:
                    raise ExplicitCollectBusy("HOT scan in progress")
                self._mark_manual_hot_started(started)
                try:
                    report = await _await_collection_runner(runner, timeout)
                    self.record_report(
                        report,
                        scan_lane=ScanLane.HOT,
                        advance_hot_due=False,
                    )
                    return report
                except TimeoutError as exc:
                    leftover = getattr(exc, "leftover", None)
                    self._adopt_leftover_collection_task(leftover, lane=ScanLane.HOT)
                    message = f"scan_cycle_timeout after {timeout:g}s"
                    self._mark_lane_error(
                        ScanLane.HOT,
                        started,
                        self.now(),
                        message,
                        advance_hot_due=False,
                    )
                    raise ScanCycleTimeout(message) from exc
                except Exception as exc:
                    self._mark_lane_error(
                        ScanLane.HOT,
                        started,
                        self.now(),
                        str(exc),
                        advance_hot_due=False,
                    )
                    raise
                finally:
                    if self.status.hot.cycle_in_progress:
                        self._mark_lane_error(
                            ScanLane.HOT,
                            started,
                            self.now(),
                            self.status.last_error or "scan_cycle_abandoned",
                            advance_hot_due=False,
                        )
        finally:
            with self._state_lock:
                self._manual_hot_in_progress = False
                self._cycle_enabled_venues = None
                self._sync_venue_status_unlocked()

    async def run_explicit_collect(self, runner) -> CollectionReport:
        """Manual diagnostic collect. Does not own HOT/UNIVERSE generation progress."""

        if self.scheduled_universe_active():
            raise ExplicitCollectBusy("UNIVERSE scan in progress")
        timeout = self.explicit_collect_timeout_seconds()
        async with self._universe_lock:
            if self._universe_in_progress or self.status.universe.cycle_in_progress:
                raise ExplicitCollectBusy("UNIVERSE scan in progress")
            self._cycle_universe_venues = self._pending_participation.venues_for(
                ScanLane.UNIVERSE
            )
            self._cycle_enabled_venues = self._cycle_universe_venues
            try:
                report = await _await_collection_runner(runner, timeout)
                self.record_explicit_report(report)
                return report
            except TimeoutError as exc:
                leftover = getattr(exc, "leftover", None)
                self._adopt_leftover_collection_task(leftover, lane=None)
                message = f"scan_cycle_timeout after {timeout:g}s"
                raise ScanCycleTimeout(message) from exc
            finally:
                with self._state_lock:
                    self._cycle_enabled_venues = None
                    self._sync_venue_status_unlocked()

    def record_explicit_report(self, report: CollectionReport) -> None:
        """Upsert current-state from a manual collect without moving scheduler dues."""

        self._last_report = report
        lane = _coerce_lane(report.scan_lane or ScanLane.UNIVERSE)
        generation_id = (
            self._universe_generation_id_for_upsert() if lane is ScanLane.UNIVERSE else None
        )
        self._fixture_state.upsert_from_report(
            report, scan_lane=lane, universe_generation_id=generation_id
        )
        duration_ms = max(
            0, int((report.completed_at - report.started_at).total_seconds() * 1000)
        )
        inventory = self._fixture_state.inventory(report.completed_at)
        _hot_count, universe_count = self._fixture_state.membership_counts(report.completed_at)
        degraded = any(
            is_provider_health_failure(value) for value in report.venue_health.values()
        )
        with self._state_lock:
            lane_update = {
                "last_completed_at": report.completed_at,
                "last_duration_ms": duration_ms,
                "last_diagnostics": _lane_diagnostics(report),
                "venue_health": _frozen_venue_health(report.venue_health),
                "operation_health": dict(report.operation_health or {}),
                "degraded": (
                    degraded or self.status.universe.degraded
                    if lane is ScanLane.UNIVERSE
                    else degraded or self.status.hot.degraded
                ),
                "last_error": None,
            }
            if lane is ScanLane.HOT:
                hot = self.status.hot.model_copy(update=lane_update)
                universe = self.status.universe
            else:
                universe = self.status.universe.model_copy(update=lane_update)
                hot = self.status.hot
            any_running = (
                self._hot_in_progress
                or self._universe_in_progress
                or self._manual_hot_in_progress
            )
            self.status = self.status.model_copy(
                update={
                    "hot": hot,
                    "universe": universe,
                    "cycle_in_progress": any_running,
                    "last_completed_at": report.completed_at,
                    "last_duration_ms": duration_ms,
                    "discovery_mode": report.discovery_mode,
                    "matching_venue": report.matching_venue,
                    "matching_venues": report.matching_venues,
                    "last_matched_event_pairs": report.matched_event_pairs,
                    "last_matched_market_pairs": report.matched_market_pairs,
                    "last_paper_decisions": len(report.paper_decisions),
                    "last_issue_count": len(report.issues),
                    "skipped_out_of_scope": report.skipped_out_of_scope,
                    "operator_summary": _combined_operator_summary(
                        hot, universe, universe_count
                    ),
                    "config_warnings": report.config_warnings,
                    "venue_health": _merge_top_level_venue_health(
                        hot.venue_health,
                        universe.venue_health,
                    ),
                    "discovered_fixtures": inventory,
                    "last_error": None,
                }
            )

    def record_report(
        self,
        report: CollectionReport,
        *,
        scan_lane: ScanLane | str | None = None,
        advance_hot_due: bool = True,
    ) -> None:
        lane = _coerce_lane(scan_lane or report.scan_lane)
        self._last_report = report
        generation_id = (
            self._universe_generation_id_for_upsert() if lane is ScanLane.UNIVERSE else None
        )
        self._fixture_state.upsert_from_report(
            report, scan_lane=lane, universe_generation_id=generation_id
        )
        duration_ms = max(
            0, int((report.completed_at - report.started_at).total_seconds() * 1000)
        )
        evaluated_n = sum(
            1
            for item in report.discovered_fixtures
            if item.market_evaluation_state == "evaluated"
        )
        leftover_n = sum(
            1
            for item in report.discovered_fixtures
            if item.market_evaluation_state == "not_evaluated_scan_deadline"
        )
        degraded = leftover_n > 0 or any(
            is_provider_health_failure(value) for value in report.venue_health.values()
        )
        hot_count = 0
        lifecycle_hot = 0
        promoted_hot = 0
        if lane is ScanLane.HOT:
            hot_count, lifecycle_hot, promoted_hot = self._fixture_state.hot_membership_breakdown(
                report.completed_at,
                hot_horizon=timedelta(minutes=get_settings().paper_hot_pre_kickoff_horizon_minutes),
                post_kickoff_unknown_horizon=timedelta(
                    hours=get_settings().paper_hot_post_kickoff_unknown_horizon_hours
                ),
                post_kickoff_current_radar_ceiling=timedelta(
                    hours=get_settings().paper_hot_post_kickoff_current_radar_ceiling_hours
                ),
            )
        inventory = self._fixture_state.inventory(report.completed_at)
        _hot_count, universe_count = self._fixture_state.membership_counts(report.completed_at)
        with self._state_lock:
            if lane is ScanLane.HOT:
                if advance_hot_due:
                    self._advance_hot_due(report.completed_at)
                self.status = self.status.model_copy(
                    update={
                        "hot": self.status.hot.model_copy(
                            update={
                                "cycle_in_progress": False,
                                "last_completed_at": report.completed_at,
                                "last_duration_ms": duration_ms,
                                "evaluated_count": evaluated_n,
                                "not_evaluated_count": leftover_n,
                                "fixture_count": hot_count,
                                "lifecycle_hot_count": lifecycle_hot,
                                "promoted_hot_count": promoted_hot,
                                "degraded": degraded,
                                "worker_state": WORKER_DEGRADED if degraded else WORKER_IDLE,
                                "last_error": None,
                                "last_persist_error": None,
                                "persist_ok": None,
                                "last_diagnostics": _lane_diagnostics(report),
                                "next_due_at": self._next_hot_due,
                                "venue_health": _frozen_venue_health(report.venue_health),
                                "operation_health": dict(report.operation_health or {}),
                                "operator_summary": _hot_operator_summary(
                                    report.completed_at,
                                    duration_ms,
                                    self._next_hot_due,
                                    hot_count,
                                    leftover_n,
                                    degraded,
                                    venue_health=report.venue_health,
                                    active_venues=self.status.hot.active_venues,
                                ),
                            }
                        )
                    }
                )
            else:
                self._record_universe_progress(
                    report,
                    duration_ms,
                    evaluated_n,
                    leftover_n,
                    degraded,
                    universe_count=universe_count,
                )
            compat_completed = (
                self.status.hot.last_completed_at or self.status.universe.last_completed_at
            )
            compat_duration = self.status.hot.last_duration_ms
            if compat_duration is None:
                compat_duration = self.status.universe.last_duration_ms
            if lane is ScanLane.HOT:
                self._hot_in_progress = False
                self._cycle_hot_venues = None
            else:
                self._universe_in_progress = False
                self._cycle_universe_venues = None
            any_running = self._hot_in_progress or self._universe_in_progress
            self.status = self.status.model_copy(
                update={
                    "cycle_in_progress": any_running,
                    "last_started_at": self.status.last_started_at
                    or self.status.hot.last_started_at
                    or self.status.universe.last_started_at,
                    "last_completed_at": compat_completed,
                    "last_duration_ms": compat_duration,
                    "discovery_mode": report.discovery_mode,
                    "matching_venue": report.matching_venue,
                    "matching_venues": report.matching_venues,
                    "last_matched_event_pairs": report.matched_event_pairs,
                    "last_matched_market_pairs": report.matched_market_pairs,
                    "last_paper_decisions": len(report.paper_decisions),
                    "last_issue_count": len(report.issues),
                    "skipped_out_of_scope": report.skipped_out_of_scope,
                    "operator_summary": _combined_operator_summary(
                        self.status.hot, self.status.universe, universe_count
                    ),
                    "config_warnings": report.config_warnings,
                    "venue_health": _merge_top_level_venue_health(
                        self.status.hot.venue_health,
                        self.status.universe.venue_health,
                    ),
                    "discovered_fixtures": inventory,
                    "last_error": None,
                    "interval_seconds": self.status.hot.cadence_seconds,
                    "provider_access": get_shared_provider_access().snapshot().as_dict(),
                }
            )
            self._cycle_enabled_venues = None
            if lane is not ScanLane.HOT:
                self._apply_universe_honesty_unlocked()
                self.status = self.status.model_copy(
                    update={
                        "operator_summary": _combined_operator_summary(
                            self.status.hot, self.status.universe, universe_count
                        ),
                        "cycle_in_progress": self._hot_in_progress
                        or self._universe_in_progress
                        or self._manual_hot_in_progress,
                    }
                )
            self._sync_venue_status_unlocked()
        if lane is not ScanLane.HOT:
            self._request_universe_checkpoint_persist(force=True)

    def record_persist_outcome(
        self,
        *,
        ok: bool,
        error: str | None,
        duration_ms: int,
        scan_lane: ScanLane | str | None = None,
    ) -> None:
        """Record scheduled persist/auto-capture after the scan envelope.

        Scanner timing (`last_error`, `last_duration_ms`) stays as the
        collection result. Persist failure is a separate honesty field.
        """

        lane = _coerce_lane(scan_lane)
        with self._state_lock:
            persist_stage = {
                "calls": 1,
                "elapsed_ms": max(0, int(duration_ms)),
                "timeouts": 0,
                "cancels": 0,
                "ok": ok,
                "error": error,
            }
            current = self.status.hot if lane is ScanLane.HOT else self.status.universe
            diagnostics = dict(current.last_diagnostics or {})
            stages = dict(diagnostics.get("stages") or {})
            stages["persistence"] = persist_stage
            diagnostics["stages"] = stages
            summary = current.operator_summary or ""
            if not ok and "persist/auto-capture failed" not in summary:
                summary = f"{summary} · persist/auto-capture failed".strip(" ·")
            updated = current.model_copy(
                update={
                    "last_diagnostics": diagnostics,
                    "operator_summary": summary or None,
                    "last_persist_error": None if ok else error,
                    "persist_ok": ok,
                    "degraded": current.degraded or (not ok),
                }
            )
            if lane is ScanLane.HOT:
                self.status = self.status.model_copy(update={"hot": updated})
            else:
                self.status = self.status.model_copy(update={"universe": updated})
            self.status = self.status.model_copy(
                update={
                    "operator_summary": _combined_operator_summary(
                        self.status.hot, self.status.universe, self.status.universe.fixture_count
                    )
                }
            )

    def _record_universe_progress(
        self,
        report: CollectionReport,
        duration_ms: int,
        evaluated_n: int,
        leftover_n: int,
        degraded: bool,
        *,
        universe_count: int,
    ) -> None:
        if self._universe_generation_started_at is None:
            self._ensure_universe_generation(report.started_at)
        self._invalidate_universe_chunk_epoch_unlocked()
        duration_s = max(0.0, (report.completed_at - report.started_at).total_seconds())
        newly_evaluated = [
            item.canonical_event_id
            for item in report.discovered_fixtures
            if item.market_evaluation_state == "evaluated"
        ]
        self._universe_evaluated_ids.update(newly_evaluated)
        if newly_evaluated:
            self._universe_cursor = newly_evaluated[-1]
        self._universe_provider_failures = 0
        self._universe_retry_at = None
        self._universe_last_report_snapshot = collection_report_snapshot(report)
        if report.series_results:
            self._universe_series_results = merge_series_reports(
                self._universe_series_results, report.series_results
            )
            self._apply_series_reports_unlocked(
                report.series_results, scanned=report.completed_at
            )
        if report.sweep_id:
            self._universe_sweep_id = report.sweep_id
        completeness = (report.scan_diagnostics or {}).get("completeness")
        diagnostics = report.scan_diagnostics or {}
        canonical_total = int(
            diagnostics.get("canonical_work_total")
            or diagnostics.get("clusters_before_resume")
            or 0
        )
        if canonical_total > self._universe_discovered_total:
            self._universe_discovered_total = canonical_total
        raw_events = diagnostics.get("raw_events_by_venue")
        if isinstance(raw_events, dict):
            self._universe_raw_events = {
                str(key): int(value or 0) for key, value in raw_events.items()
            }
        for item in report.discovered_fixtures:
            canonical_id = item.canonical_event_id
            if not canonical_id:
                continue
            unit = self._universe_work.get(canonical_id)
            state = str(item.market_evaluation_state or "")
            if unit is None or unit.state in {SWEEP_PENDING, SWEEP_RUNNING}:
                self._apply_work_unit_result_unlocked(
                    canonical_id,
                    state=state,
                    reason=str(getattr(item, "market_evaluation_reason", "") or ""),
                    scanned=report.completed_at,
                )
            elif unit.state == SWEEP_RETRY_WAIT and state == "evaluated":
                self._apply_work_unit_result_unlocked(
                    canonical_id,
                    state=state,
                    reason="",
                    scanned=report.completed_at,
                )
        counts = self._lane_progress_fields()
        series_snapshot = dict(self._universe_series_work)
        self._universe_discovered_total = counts["canonical_work_total"] or self._universe_discovered_total
        degraded = degraded or counts["canonical_retryable"] > 0 or counts["series_retryable"] > 0
        budget = self._charge_successful_universe_work(
            duration_s,
            report.completed_at,
            leftover_n=leftover_n,
            completeness=completeness,
        )
        self._mark_universe_checkpoint_dirty_unlocked()
        work_used = self._status_universe_work_used()
        evaluated_count = self._status_universe_evaluated_count()
        resume_cursor = self._status_universe_cursor()
        closed = self._universe_generation_started_at is None
        retry_wait = None if closed else self._earliest_retry_wait_unlocked(report.completed_at)
        if closed:
            worker_state = WORKER_COMPLETE
        elif retry_wait is not None:
            worker_state = WORKER_WAITING
        elif degraded:
            worker_state = WORKER_DEGRADED
        else:
            worker_state = WORKER_IDLE
        self.status = self.status.model_copy(
            update={
                "universe": self.status.universe.model_copy(
                    update={
                        "cycle_in_progress": False,
                        "worker_state": worker_state,
                        "sweep_id": self._universe_sweep_id,
                        "last_completed_at": report.completed_at,
                        "last_duration_ms": duration_ms,
                        "chunk_last_duration_ms": duration_ms,
                        "generation_work_used_s": round(work_used, 3),
                        **counts,
                        "not_evaluated_count": leftover_n,
                        "matched_fixtures": self._universe_matched_fixtures,
                        "equivalent_markets": self._universe_equivalent_markets,
                        "near_count": self._universe_near_count,
                        "positive_count": self._universe_positive_count,
                        "qualifying_count": self._universe_qualifying_count,
                        "hot_promotions": self._universe_hot_promotions,
                        "last_successful_fixture": self._universe_last_successful,
                        "fixture_count": universe_count,
                        "degraded": degraded,
                        "last_heartbeat_at": report.completed_at,
                        "last_error": None,
                        "last_persist_error": None,
                        "persist_ok": None,
                        "last_diagnostics": _lane_diagnostics(
                            report,
                            series_work=series_snapshot,
                            sweep_id=self._universe_sweep_id,
                            generation_id=self._universe_generation_id,
                        ),
                        "resume_cursor": resume_cursor,
                        "next_due_at": self._next_universe_due,
                        "venue_health": _honest_universe_venue_health(
                            _frozen_venue_health(report.venue_health),
                            retryable=counts["canonical_retryable"] + counts["series_retryable"],
                        ),
                        "operation_health": dict(report.operation_health or {}),
                        "operator_summary": _universe_operator_summary(
                            duration_ms,
                            work_used,
                            budget,
                            universe_count,
                            evaluated_count,
                            leftover_n,
                            discovered_total=self._universe_discovered_total,
                            worker_state=worker_state,
                            venue_health=report.venue_health,
                            active_venues=self.status.universe.active_venues,
                        ),
                    }
                )
            }
        )

    def record_universe_discovery_snapshot(
        self,
        snapshot: dict[str, list[dict[str, Any]]],
        *,
        chunk_epoch: int | None = None,
    ) -> None:
        with self._state_lock:
            if self._reject_stale_universe_chunk_unlocked(chunk_epoch):
                return
            if self._universe_generation_started_at is None:
                self._ensure_universe_generation(self.now())
            series = snapshot.get("series_results")
            if isinstance(series, dict):
                self._universe_series_results = merge_series_reports(
                    self._universe_series_results, series
                )
                self._apply_series_reports_unlocked(series, scanned=self.now())
            self._universe_discovery_snapshot = discovery_event_snapshot(snapshot)
            self._universe_raw_events = {
                key: len(value or [])
                for key, value in self._universe_discovery_snapshot.items()
            }
            self._mark_universe_checkpoint_dirty_unlocked()
            self.status = self.status.model_copy(
                update={
                    "universe": self.status.universe.model_copy(
                        update={
                            "sweep_id": self._universe_sweep_id,
                            "raw_events_discovered_by_venue": dict(self._universe_raw_events),
                            "worker_state": WORKER_RUNNING
                            if self._universe_in_progress
                            else self.status.universe.worker_state,
                        }
                    )
                }
            )
        self._request_universe_checkpoint_persist(force=True)

    def record_universe_work_set(
        self,
        canonical_ids: list[str],
        *,
        authoritative: bool = False,
        partial_reason: str | None = None,
        chunk_epoch: int | None = None,
    ) -> None:
        with self._state_lock:
            if self._reject_stale_universe_chunk_unlocked(chunk_epoch):
                return
            if self._universe_generation_started_at is None:
                self._ensure_universe_generation(self.now())
            current: list[str] = []
            for raw_id in canonical_ids:
                canonical_id = str(raw_id or "").strip()
                if not canonical_id:
                    continue
                current.append(canonical_id)
                existing = self._universe_work.get(canonical_id)
                if existing is None:
                    self._universe_work[canonical_id] = SweepWorkUnit(canonical_id=canonical_id)
                elif existing.state == SWEEP_STALE_ORPHAN:
                    existing.state = SWEEP_PENDING
                    existing.reason = None
                    existing.retryable = False
                    existing.next_retry_at = None
            if authoritative:
                self._reconcile_universe_work_set_unlocked(
                    current,
                    partial_reason=partial_reason,
                )
                self._prune_universe_rehydration_unlocked(current)
            self._universe_discovered_total = len(self._universe_work)
            self._mark_universe_checkpoint_dirty_unlocked()
            counts = self._lane_progress_fields()
            self.status = self.status.model_copy(
                update={
                    "universe": self.status.universe.model_copy(
                        update={
                            **counts,
                            "sweep_id": self._universe_sweep_id,
                            "worker_state": WORKER_RUNNING
                            if self._universe_in_progress
                            else self.status.universe.worker_state,
                        }
                    )
                }
            )
        self._request_universe_checkpoint_persist(force=True)

    def _reconcile_universe_work_set_unlocked(
        self,
        current_ids: list[str],
        *,
        partial_reason: str | None,
    ) -> None:
        """Retire unreachable nonterminal work only against a full cluster set.

        A genuinely healthy empty set is authoritative: every persisted
        nonterminal id is absent and must be retired. Partial discovery
        (provider failure, truncation, auth, retry-series, unknown/degraded
        health) must leave PENDING/RETRY_WAIT items untouched. Terminal
        evaluated history is never reclassified.
        """

        del partial_reason
        current = {item for item in current_ids if item}
        if not self._universe_work_set_may_retire_unlocked():
            return
        retired = 0
        for canonical_id, unit in list(self._universe_work.items()):
            if unit.state in SWEEP_TERMINAL_STATES:
                continue
            if canonical_id in current:
                continue
            unit.state = SWEEP_STALE_ORPHAN
            unit.retryable = False
            unit.reason = STALE_ORPHAN_REASON
            unit.next_retry_at = None
            self._universe_skipped_ids[canonical_id] = STALE_ORPHAN_REASON
            retired += 1
        if retired:
            LOGGER.info(
                "retired %s stale/orphan UNIVERSE work items absent from "
                "authoritative cluster set of %s",
                retired,
                len(current),
            )

    def _universe_work_set_may_retire_unlocked(self) -> bool:
        if self._universe_series_work and not self._universe_series_is_terminal_unlocked():
            return False
        return True

    def _prune_universe_rehydration_unlocked(self, current_ids: list[str]) -> None:
        """Drop vanished evaluated IDs so they cannot pin an open generation.

        Authoritative rediscovery is allowed to omit fixtures that no longer
        exist. Those IDs stay EVALUATED for accounting but must not block
        sweep completion waiting for current-state that will never return.
        Partial/non-authoritative cluster sets must not prune.
        """

        if not self._universe_needs_rehydration:
            return
        if not self._universe_work_set_may_retire_unlocked():
            return
        current = {item for item in current_ids if item}
        dropped = self._universe_needs_rehydration - current
        self._universe_needs_rehydration.intersection_update(current)
        for canonical_id in dropped:
            self._universe_rehydration_retry_at.pop(canonical_id, None)
            self._universe_rehydration_attempts.pop(canonical_id, None)

    def record_universe_fixture_progress(
        self,
        cluster: Any,
        fixture: Any,
        decisions: list[Any],
        inventory: list[Any],
        *,
        chunk_epoch: int | None = None,
    ) -> None:
        canonical_id = str(getattr(fixture, "canonical_event_id", "") or "")
        if not canonical_id:
            return
        aliases = cluster_identity_aliases(cluster) if cluster is not None else {canonical_id: canonical_id}
        source_events = []
        if cluster is not None:
            source_events = [
                {
                    "venue": item.venue.value,
                    "source_event_id": item.source_event_id,
                    "raw": item.raw,
                }
                for item in cluster_member_events(cluster)
            ]
        scanned = getattr(fixture, "last_scanned_at", None) or self.now()
        with self._state_lock:
            if self._reject_stale_universe_chunk_unlocked(chunk_epoch):
                return
            state = str(getattr(fixture, "market_evaluation_state", "") or "")
            rehydrating = canonical_id in self._universe_needs_rehydration
            if rehydrating and state != "evaluated":
                self._schedule_universe_rehydration_retry_unlocked(canonical_id, scanned)
                self._universe_current_fixture = canonical_id
                self.status = self.status.model_copy(
                    update={
                        "universe": self.status.universe.model_copy(
                            update={
                                "current_fixture": canonical_id,
                                "last_heartbeat_at": scanned,
                                "worker_state": WORKER_RUNNING,
                            }
                        )
                    }
                )
                return
            generation_id = self._ensure_store_universe_generation(scanned)
            before_hot, _before_universe = self._fixture_state.membership_counts(scanned)
            self._fixture_state.upsert_evaluated_fixture(
                fixture,
                markets=inventory,
                decisions=decisions,
                aliases=aliases,
                source_events=source_events,
                scan_lane=ScanLane.UNIVERSE,
                now=scanned,
                universe_generation_id=generation_id,
            )
            if rehydrating:
                self._clear_universe_rehydration_unlocked(canonical_id)
            after_hot, _after_universe = self._fixture_state.membership_counts(scanned)
            promoted_now = after_hot > before_hot
            inventory_now = self._fixture_state.inventory(scanned)
            if self._universe_generation_started_at is None:
                self._ensure_universe_generation(scanned)
            previous = self._universe_work.get(canonical_id)
            already_evaluated = previous is not None and previous.state == SWEEP_EVALUATED
            self._apply_work_unit_result_unlocked(
                canonical_id,
                state=state,
                reason=str(getattr(fixture, "market_evaluation_reason", "") or ""),
                scanned=scanned,
            )
            unit = self._universe_work.get(canonical_id)
            if unit is not None and unit.state == SWEEP_EVALUATED:
                self._universe_evaluated_ids.add(canonical_id)
                self._universe_cursor = canonical_id
                self._universe_last_successful = canonical_id
            elif unit is not None and unit.state == SWEEP_FINAL_FAILED:
                self._universe_failed_ids[canonical_id] = unit.reason or "final_failed"
            elif unit is not None and unit.state == SWEEP_SKIPPED_UNSUPPORTED:
                self._universe_skipped_ids[canonical_id] = unit.reason or "skipped"
            self._universe_current_fixture = canonical_id
            if not already_evaluated:
                if int(getattr(fixture, "matched_equivalent_count", 0) or 0) > 0:
                    self._universe_matched_fixtures += 1
                    self._universe_equivalent_markets += int(
                        getattr(fixture, "matched_equivalent_count", 0) or 0
                    )
                opportunity = str(getattr(fixture, "opportunity_state", "") or "").casefold()
                if opportunity in {"near", "near_executable", "approaching"}:
                    self._universe_near_count += 1
                if (getattr(fixture, "current_net_edge", None) or 0) > 0:
                    self._universe_positive_count += 1
                if bool(getattr(fixture, "solver_is_arbitrage", False)) or opportunity in {
                    "qualifying",
                    "triggered",
                    "arbitrage",
                }:
                    self._universe_qualifying_count += 1
                if promoted_now:
                    self._universe_hot_promotions += 1
            counts = self._lane_progress_fields()
            self._universe_discovered_total = counts["canonical_work_total"]
            self._mark_universe_checkpoint_dirty_unlocked(fixture=True)
            venue_health = _honest_universe_venue_health(
                self.status.universe.venue_health,
                retryable=counts["canonical_retryable"] + counts["series_retryable"],
            )
            self.status = self.status.model_copy(
                update={
                    "discovered_fixtures": inventory_now,
                    "venue_health": _merge_top_level_venue_health(
                        self.status.hot.venue_health,
                        venue_health,
                    ),
                    "universe": self.status.universe.model_copy(
                        update={
                            **counts,
                            "degraded": self.status.universe.degraded
                            or counts["canonical_retryable"] > 0,
                            "venue_health": venue_health,
                            "worker_state": WORKER_RUNNING,
                            "sweep_id": self._universe_sweep_id,
                            "matched_fixtures": self._universe_matched_fixtures,
                            "equivalent_markets": self._universe_equivalent_markets,
                            "near_count": self._universe_near_count,
                            "positive_count": self._universe_positive_count,
                            "qualifying_count": self._universe_qualifying_count,
                            "hot_promotions": self._universe_hot_promotions,
                            "last_successful_fixture": self._universe_last_successful,
                            "current_fixture": self._universe_current_fixture,
                            "resume_cursor": self._universe_cursor,
                            "cycle_in_progress": True,
                            "last_heartbeat_at": scanned,
                        }
                    ),
                }
            )
        self._request_universe_checkpoint_persist(force=False)

    def _apply_work_unit_result_unlocked(
        self,
        canonical_id: str,
        *,
        state: str,
        reason: str,
        scanned: datetime,
    ) -> None:
        unit = self._universe_work.get(canonical_id) or SweepWorkUnit(canonical_id=canonical_id)
        if unit.state == SWEEP_EVALUATED:
            self._universe_work[canonical_id] = unit
            return
        if unit.state in {
            SWEEP_FINAL_FAILED,
            SWEEP_SKIPPED_UNSUPPORTED,
            SWEEP_STALE_ORPHAN,
        } and state != "evaluated":
            self._universe_work[canonical_id] = unit
            return
        unit.last_attempted_at = scanned
        if state == "evaluated":
            unit.state = SWEEP_EVALUATED
            unit.retryable = False
            unit.reason = None
            unit.next_retry_at = None
        elif state == "market_fetch_unavailable":
            _schedule_capped_retry(
                unit,
                reason=reason or "provider_failure",
                scanned=scanned,
            )
        elif state in {"unsupported", "skipped_unsupported"}:
            unit.state = SWEEP_SKIPPED_UNSUPPORTED
            unit.retryable = False
            unit.reason = reason or state
            unit.next_retry_at = None
        elif state in {HEALTH_AUTH_FAILURE, "auth_failure"}:
            unit.state = SWEEP_FINAL_FAILED
            unit.retryable = False
            unit.reason = reason or state
            unit.next_retry_at = None
            if unit.attempt_count == 0:
                unit.attempt_count = 1
        elif state:
            unit.state = SWEEP_PENDING
            unit.reason = state
        self._universe_work[canonical_id] = unit

    def _apply_series_reports_unlocked(
        self,
        incoming: dict[str, list[dict[str, Any]]] | None,
        *,
        scanned: datetime,
    ) -> None:
        for venue, rows in (incoming or {}).items():
            for row in rows or []:
                if isinstance(row, dict):
                    self._apply_series_result_unlocked(str(venue), row, scanned)

    def _apply_series_result_unlocked(
        self,
        venue: str,
        row: dict[str, Any],
        scanned: datetime,
    ) -> None:
        series = str(row.get("series") or "").strip()
        if not series:
            return
        key = series_work_key(venue, series)
        unit = self._universe_series_work.get(key) or SeriesWorkUnit(venue=venue, series=series)
        status = str(row.get("status") or "").strip()
        retryable_flag = bool(row.get("retryable"))
        reason = str(row.get("reason") or status or "")
        event_count = int(row.get("event_count") or 0)
        if key in self._universe_series_applied_this_cycle:
            if status == "ok" and unit.state != SWEEP_OK:
                unit.state = SWEEP_OK
                unit.retryable = False
                unit.reason = None
                unit.next_retry_at = None
                unit.event_count = event_count
                unit.last_attempted_at = scanned
                self._universe_series_work[key] = unit
            return
        self._universe_series_applied_this_cycle.add(key)
        if unit.state == SWEEP_OK and status == "ok":
            unit.event_count = max(unit.event_count, event_count)
            unit.last_attempted_at = scanned
            self._universe_series_work[key] = unit
            return
        if unit.state in {SWEEP_FINAL_FAILED, SWEEP_SKIPPED_UNSUPPORTED} and status != "ok":
            self._universe_series_work[key] = unit
            return
        unit.last_attempted_at = scanned
        unit.event_count = event_count
        if status == "ok":
            unit.state = SWEEP_OK
            unit.retryable = False
            unit.reason = None
            unit.next_retry_at = None
        elif status in {"unsupported", "skipped_unsupported"}:
            unit.state = SWEEP_SKIPPED_UNSUPPORTED
            unit.retryable = False
            unit.reason = reason or status
            unit.next_retry_at = None
            if unit.attempt_count == 0:
                unit.attempt_count = 1
        elif status == HEALTH_AUTH_FAILURE or (not retryable_flag and status == "auth_failure"):
            unit.state = SWEEP_FINAL_FAILED
            unit.retryable = False
            unit.reason = reason or status
            unit.next_retry_at = None
            if unit.attempt_count == 0:
                unit.attempt_count = 1
        elif retryable_flag or status in {
            HEALTH_DISCOVERY_TIMEOUT,
            HEALTH_MARKET_TIMEOUT,
            HEALTH_UNAVAILABLE,
            "rate_limited",
            "timeout",
        }:
            _schedule_capped_retry(unit, reason=reason or status, scanned=scanned)
        elif status and not retryable_flag:
            unit.state = SWEEP_FINAL_FAILED
            unit.retryable = False
            unit.reason = reason or status
            unit.next_retry_at = None
            if unit.attempt_count == 0:
                unit.attempt_count = 1
        self._universe_series_work[key] = unit

    def _due_retry_series_unlocked(self, now: datetime) -> dict[str, list[str]]:
        retry: dict[str, list[str]] = {}
        for unit in self._universe_series_work.values():
            if unit.state not in {SWEEP_PENDING, SWEEP_RETRY_WAIT}:
                continue
            if (
                unit.state == SWEEP_RETRY_WAIT
                and unit.next_retry_at is not None
                and now < unit.next_retry_at
            ):
                continue
            retry.setdefault(unit.venue, []).append(unit.series)
        return {key: value for key, value in retry.items() if value}

    def _open_universe_chunk_epoch_unlocked(self) -> int:
        self._universe_chunk_seq += 1
        self._universe_active_chunk_epoch = self._universe_chunk_seq
        return self._universe_chunk_seq

    def _invalidate_universe_chunk_epoch_unlocked(self) -> None:
        self._universe_active_chunk_epoch = None

    def _reject_stale_universe_chunk_unlocked(self, chunk_epoch: int | None) -> bool:
        if chunk_epoch is None:
            return False
        if chunk_epoch == self._universe_active_chunk_epoch:
            return False
        self._universe_stale_callback_count += 1
        LOGGER.info(
            "quarantined stale UNIVERSE chunk callback epoch=%s active=%s total=%s",
            chunk_epoch,
            self._universe_active_chunk_epoch,
            self._universe_stale_callback_count,
        )
        return True

    def _prune_finished_orphaned_universe_tasks_unlocked(self) -> None:
        self._universe_orphaned_tasks = [
            task for task in self._universe_orphaned_tasks if not task.done()
        ]

    def _on_orphaned_universe_task_done(self, task: asyncio.Task[Any]) -> None:
        _consume_orphaned_task_result(task)
        with self._state_lock:
            self._prune_finished_orphaned_universe_tasks_unlocked()

    def _adopt_orphaned_universe_task(self, task: asyncio.Task[Any]) -> None:
        with self._state_lock:
            self._universe_orphaned_chunk_count += 1
            self._universe_orphaned_tasks.append(task)
        task.add_done_callback(self._on_orphaned_universe_task_done)
        with self._state_lock:
            self._prune_finished_orphaned_universe_tasks_unlocked()
            live = sum(1 for item in self._universe_orphaned_tasks if not item.done())
            adopted = self._universe_orphaned_chunk_count
        if live >= UNIVERSE_ORPHAN_TASK_WARN_LIMIT:
            LOGGER.warning(
                "UNIVERSE cancellation-ignoring orphaned chunks live=%s total_adopted=%s",
                live,
                adopted,
            )

    def _adopt_leftover_collection_task(
        self,
        leftover: asyncio.Task[Any] | None,
        *,
        lane: ScanLane | None,
    ) -> None:
        if leftover is None:
            return
        if leftover.done():
            _consume_orphaned_task_result(leftover)
            return
        leftover.cancel()
        if lane is ScanLane.UNIVERSE:
            self._adopt_orphaned_universe_task(leftover)
            return
        leftover.add_done_callback(_consume_orphaned_task_result)

    def _drain_orphaned_collection_tasks(self, tasks: list[asyncio.Task[Any]]) -> None:
        for task in tasks:
            if not task.done():
                task.cancel()
            if task.done():
                _consume_orphaned_task_result(task)
            else:
                task.add_done_callback(_consume_orphaned_task_result)

    def universe_collect_callbacks(
        self,
    ) -> tuple[Callable[..., None], Callable[..., None], Callable[..., None]]:
        with self._state_lock:
            epoch = self._universe_active_chunk_epoch

        def on_discovery(snapshot: dict[str, list[dict[str, Any]]]) -> None:
            self.record_universe_discovery_snapshot(snapshot, chunk_epoch=epoch)

        def on_fixture(
            cluster: Any,
            fixture: Any,
            decisions: list[Any],
            inventory: list[Any],
        ) -> None:
            self.record_universe_fixture_progress(
                cluster,
                fixture,
                decisions,
                inventory,
                chunk_epoch=epoch,
            )

        def on_work(
            canonical_ids: list[str],
            *,
            authoritative: bool = False,
            partial_reason: str | None = None,
        ) -> None:
            self.record_universe_work_set(
                canonical_ids,
                authoritative=authoritative,
                partial_reason=partial_reason,
                chunk_epoch=epoch,
            )

        return on_discovery, on_fixture, on_work

    def _universe_plan_resume_state_unlocked(
        self,
        now: datetime | None = None,
    ) -> tuple[str | None, list[str], int, bool]:
        """Bind skip/cursor to the open generation. Closed gens plan empty resume."""

        if self._universe_generation_started_at is None:
            return (None, [], self._universe_generation_id + 1, False)
        progress_id = self._universe_progress_generation_id
        if progress_id is not None and progress_id != self._universe_generation_id:
            return (None, [], self._universe_generation_id + 1, False)
        plan_id = self._universe_generation_id if self._universe_generation_id > 0 else 1
        skip = self._universe_skip_ids_unlocked(now)
        return (
            self._universe_cursor,
            skip,
            plan_id,
            True,
        )

    def _universe_skip_ids_unlocked(self, now: datetime | None = None) -> list[str]:
        pending_rehydration = self._universe_needs_rehydration
        now = now or self.now()
        if self._universe_work:
            skip: list[str] = []
            for canonical_id, unit in self._universe_work.items():
                if canonical_id in pending_rehydration:
                    retry_at = self._universe_rehydration_retry_at.get(canonical_id)
                    if retry_at is not None and now < retry_at:
                        skip.append(canonical_id)
                    continue
                if unit.state in SWEEP_TERMINAL_STATES:
                    skip.append(canonical_id)
                elif (
                    unit.state == SWEEP_RETRY_WAIT
                    and unit.next_retry_at is not None
                    and now < unit.next_retry_at
                ):
                    skip.append(canonical_id)
            return sorted(skip)
        skip: list[str] = []
        for canonical_id in self._universe_evaluated_ids:
            if canonical_id in pending_rehydration:
                retry_at = self._universe_rehydration_retry_at.get(canonical_id)
                if retry_at is not None and now < retry_at:
                    skip.append(canonical_id)
                continue
            skip.append(canonical_id)
        return sorted(skip)

    def _earliest_retry_wait_unlocked(self, now: datetime) -> datetime | None:
        if any(
            unit.state in {SWEEP_PENDING, SWEEP_RUNNING} for unit in self._universe_work.values()
        ) or any(unit.state == SWEEP_PENDING for unit in self._universe_series_work.values()):
            return None
        if any(
            self._universe_rehydration_retry_at.get(canonical_id) is None
            or now >= self._universe_rehydration_retry_at[canonical_id]
            for canonical_id in self._universe_needs_rehydration
        ):
            return None
        times = [
            unit.next_retry_at
            for unit in (*self._universe_work.values(), *self._universe_series_work.values())
            if unit.state == SWEEP_RETRY_WAIT and unit.next_retry_at is not None
        ]
        times.extend(
            self._universe_rehydration_retry_at[canonical_id]
            for canonical_id in self._universe_needs_rehydration
            if canonical_id in self._universe_rehydration_retry_at
        )
        if not times:
            return None
        earliest = min(times)
        return earliest if now < earliest else None

    def _canonical_counts_unlocked(self) -> dict[str, int]:
        work = self._universe_work
        evaluated = sum(1 for unit in work.values() if unit.state == SWEEP_EVALUATED)
        retryable = sum(1 for unit in work.values() if unit.state == SWEEP_RETRY_WAIT)
        final_failed = sum(1 for unit in work.values() if unit.state == SWEEP_FINAL_FAILED)
        skipped = sum(1 for unit in work.values() if unit.state == SWEEP_SKIPPED_UNSUPPORTED)
        stale_orphan = sum(1 for unit in work.values() if unit.state == SWEEP_STALE_ORPHAN)
        total = len(work) or self._universe_discovered_total
        remaining = max(0, total - evaluated - final_failed - skipped - stale_orphan)
        return {
            "canonical_work_total": total,
            "canonical_evaluated": evaluated,
            "canonical_retryable": retryable,
            "canonical_final_failed": final_failed,
            "canonical_stale_orphan": stale_orphan,
            "canonical_remaining": remaining,
            "discovered_total": total,
            "evaluated_count": evaluated,
            "remaining": remaining,
        }

    def _universe_series_is_terminal_unlocked(self) -> bool:
        return all(
            unit.state in SERIES_TERMINAL_STATES for unit in self._universe_series_work.values()
        )

    def _universe_sweep_is_complete_unlocked(self) -> bool:
        if self._universe_needs_rehydration:
            return False
        series_terminal = self._universe_series_is_terminal_unlocked()
        if self._universe_series_work and not series_terminal:
            return False
        if self._universe_work:
            return all(unit.state in SWEEP_TERMINAL_STATES for unit in self._universe_work.values())
        return bool(self._universe_series_work) and series_terminal

    def _lane_progress_fields(self) -> dict[str, Any]:
        counts = self._canonical_counts_unlocked()
        series = self._universe_series_work
        return {
            **counts,
            "raw_events_discovered_by_venue": dict(self._universe_raw_events),
            "series_work_total": len(series),
            "series_ok": sum(1 for unit in series.values() if unit.state == SWEEP_OK),
            "series_retryable": sum(1 for unit in series.values() if unit.state == SWEEP_RETRY_WAIT),
            "series_final_failed": sum(
                1 for unit in series.values() if unit.state == SWEEP_FINAL_FAILED
            ),
            "series_skipped": sum(
                1 for unit in series.values() if unit.state == SWEEP_SKIPPED_UNSUPPORTED
            ),
        }

    def _status_universe_work_used(self) -> float:
        if self._universe_generation_started_at is not None:
            return self._universe_work_used
        return self._universe_closed_work_used

    def _status_universe_cursor(self) -> str | None:
        if self._universe_generation_started_at is not None:
            return self._universe_cursor
        return self._universe_closed_cursor

    def _status_universe_evaluated_count(self) -> int:
        if self._universe_generation_started_at is not None:
            if self._universe_work:
                return self._canonical_counts_unlocked()["canonical_evaluated"]
            return len(self._universe_evaluated_ids)
        return self._universe_closed_evaluated_count

    def _clear_universe_generation_local_state(self) -> None:
        self._universe_closed_generation_id = self._universe_generation_id
        self._universe_closed_work_used = self._universe_work_used
        self._universe_closed_cursor = self._universe_cursor
        if self._universe_work:
            self._universe_closed_evaluated_count = self._canonical_counts_unlocked()[
                "canonical_evaluated"
            ]
        else:
            self._universe_closed_evaluated_count = len(self._universe_evaluated_ids)
        self._universe_evaluated_ids = set()
        self._universe_needs_rehydration = set()
        self._universe_rehydration_retry_at = {}
        self._universe_rehydration_attempts = {}
        self._universe_work = {}
        self._universe_series_work = {}
        self._universe_series_results = {}
        self._universe_series_applied_this_cycle = set()
        self._universe_cursor = None
        self._universe_work_used = 0.0
        self._universe_progress_generation_id = None
        self._universe_discovery_snapshot = None

    def _mark_universe_checkpoint_dirty_unlocked(self, *, fixture: bool = False) -> None:
        self._universe_checkpoint_dirty = True
        if fixture:
            self._universe_checkpoint_unpersisted_fixtures += 1

    def _persist_universe_checkpoint_unlocked(self) -> None:
        """Mark resume state dirty. Must not perform SQLite or JSON file I/O."""

        self._mark_universe_checkpoint_dirty_unlocked()

    def _snapshot_universe_checkpoint_write_unlocked(self) -> _UniverseCheckpointWrite | None:
        if not self._universe_checkpoint_dirty:
            return None
        self._universe_checkpoint_dirty = False
        self._universe_checkpoint_unpersisted_fixtures = 0
        self._universe_checkpoint_write_seq += 1
        token = self._universe_checkpoint_write_seq
        if self._universe_generation_started_at is None:
            return _UniverseCheckpointWrite(token=token, action="clear")
        checkpoint = UniverseGenerationCheckpoint(
            generation_id=max(1, self._universe_generation_id),
            generation_started_at=self._universe_generation_started_at,
            successful_work_used_s=self._universe_work_used,
            resume_cursor=self._universe_cursor,
            evaluated_ids=sorted(self._universe_evaluated_ids),
            next_universe_due=self._next_universe_due,
            provider_failure_count=self._universe_provider_failures,
            retry_at=self._universe_retry_at,
            budget_paused=False,
            updated_at=self.now(),
            sweep_id=self._universe_sweep_id,
            discovered_total=self._universe_discovered_total,
            failed_ids=dict(self._universe_failed_ids),
            skipped_ids=dict(self._universe_skipped_ids),
            last_successful_fixture=self._universe_last_successful,
            matched_fixtures=self._universe_matched_fixtures,
            equivalent_markets=self._universe_equivalent_markets,
            near_count=self._universe_near_count,
            positive_count=self._universe_positive_count,
            qualifying_count=self._universe_qualifying_count,
            hot_promotions=self._universe_hot_promotions,
            raw_events_by_venue=dict(self._universe_raw_events),
            work_units=dict(self._universe_work),
            series_work=dict(self._universe_series_work),
            semantics_version=UNIVERSE_CHECKPOINT_SEMANTICS_VERSION,
        )
        return _UniverseCheckpointWrite(
            token=token,
            action="save",
            payload=checkpoint.model_dump(mode="json"),
            updated_at=checkpoint.updated_at.isoformat(),
        )

    def _commit_universe_checkpoint_write(self, write: _UniverseCheckpointWrite) -> bool:
        if self._state_lock.held_by_current_thread:
            raise RuntimeError("universe checkpoint I/O while _state_lock is held")
        store = self._universe_checkpoint_store
        if store is None:
            self._apply_universe_checkpoint_write_outcome(write, ok=True, retryable=False, error=None)
            return True
        try:
            if write.action == "clear":
                store.clear()
            elif write.payload is None or write.updated_at is None:
                self._apply_universe_checkpoint_write_outcome(
                    write, ok=True, retryable=False, error=None
                )
                return True
            else:
                store.save(write.payload, updated_at=write.updated_at)
        except UniverseCheckpointTooLarge as exc:
            LOGGER.warning("refusing oversized universe checkpoint payload", exc_info=True)
            self._apply_universe_checkpoint_write_outcome(
                write,
                ok=False,
                retryable=False,
                error=str(exc) or "universe_checkpoint_too_large",
            )
            return False
        except Exception as exc:
            LOGGER.warning("failed to persist universe generation checkpoint", exc_info=True)
            self._apply_universe_checkpoint_write_outcome(
                write,
                ok=False,
                retryable=True,
                error=str(exc) or "universe_checkpoint_persist_failed",
            )
            return False
        self._apply_universe_checkpoint_write_outcome(write, ok=True, retryable=False, error=None)
        return True

    def _apply_universe_checkpoint_write_outcome(
        self,
        write: _UniverseCheckpointWrite,
        *,
        ok: bool,
        retryable: bool,
        error: str | None,
    ) -> None:
        """Keep transient I/O failures retryable; never tight-loop oversize."""

        with self._state_lock:
            superseded = self._universe_checkpoint_write_seq != write.token
            if not ok and retryable and not superseded:
                self._universe_checkpoint_dirty = True
            self.status = self.status.model_copy(
                update={
                    "universe": self.status.universe.model_copy(
                        update={
                            "last_persist_error": None if ok else error,
                            "persist_ok": ok,
                        }
                    )
                }
            )

    def _run_universe_checkpoint_persist_once(self) -> bool:
        with self._universe_checkpoint_io_lock:
            with self._state_lock:
                write = self._snapshot_universe_checkpoint_write_unlocked()
            if write is None:
                return True
            return self._commit_universe_checkpoint_write(write)

    def flush_universe_checkpoint(self) -> None:
        """Persist coalesced compact resume state without holding `_state_lock`."""

        while True:
            with self._state_lock:
                if not self._universe_checkpoint_dirty:
                    return
                if self._universe_checkpoint_store is None:
                    self._universe_checkpoint_dirty = False
                    self._universe_checkpoint_unpersisted_fixtures = 0
                    return
            if not self._run_universe_checkpoint_persist_once():
                return

    def _request_universe_checkpoint_persist(self, *, force: bool) -> None:
        with self._state_lock:
            dirty = self._universe_checkpoint_dirty
            threshold = (
                self._universe_checkpoint_unpersisted_fixtures
                >= UNIVERSE_CHECKPOINT_FLUSH_FIXTURE_THRESHOLD
            )
            store = self._universe_checkpoint_store
        if store is None or not dirty:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            self.flush_universe_checkpoint()
            return
        if not force and not threshold:
            return
        self._schedule_universe_checkpoint_persist(loop)

    def _schedule_universe_checkpoint_persist(self, loop: asyncio.AbstractEventLoop) -> None:
        task = self._universe_checkpoint_persist_task
        if task is not None and not task.done():
            return
        self._universe_checkpoint_persist_task = loop.create_task(
            self._universe_checkpoint_persist_worker(),
            name="universe-checkpoint-persist",
        )

    async def _universe_checkpoint_persist_worker(self) -> None:
        try:
            while True:
                with self._state_lock:
                    dirty = self._universe_checkpoint_dirty
                if not dirty:
                    return
                ok = await asyncio.to_thread(self._run_universe_checkpoint_persist_once)
                if not ok:
                    return
        finally:
            self._universe_checkpoint_persist_task = None

    async def _await_universe_checkpoint_persist(self) -> None:
        task = self._universe_checkpoint_persist_task
        if task is not None and not task.done():
            await task
        with self._state_lock:
            dirty = self._universe_checkpoint_dirty
            store = self._universe_checkpoint_store
        if store is None or not dirty:
            return
        await asyncio.to_thread(self.flush_universe_checkpoint)

    def health_live_refresh_fields(self) -> dict[str, object]:
        """Already-configured status snapshot. No settings DB or checkpoint I/O."""

        status = self.status
        return {
            "discovery_source": "matchbook",
            "discovery_mode": "venue_union",
            "matching_venue": "polymarket",
            "matching_venues": ["polymarket", "kalshi"],
            "server_loop_enabled": status.server_loop_enabled,
            "paper_autofill_enabled": status.paper_autofill_enabled,
            "interval_seconds": status.interval_seconds,
            "hot_in_progress": self._hot_in_progress,
            "background_in_progress": self._background_in_progress,
            "universe_in_progress": self._universe_in_progress,
        }

    def _restore_universe_checkpoint(self) -> None:
        with self._state_lock:
            if self._universe_checkpoint_restored:
                return
            self._universe_checkpoint_restored = True
            if self._universe_generation_started_at is not None:
                return
            store = self._universe_checkpoint_store
        if store is None:
            return
        try:
            payload = store.load()
        except Exception:
            LOGGER.warning("failed to load universe generation checkpoint", exc_info=True)
            return
        checkpoint = checkpoint_from_payload(payload)
        if checkpoint is None:
            if payload is not None:
                try:
                    store.clear()
                except Exception:
                    LOGGER.warning(
                        "failed to invalidate unsafe universe checkpoint",
                        exc_info=True,
                    )
            return
        self._fixture_state.open_universe_generation(
            checkpoint.generation_id, started_at=checkpoint.generation_started_at
        )
        inventory = self._fixture_state.inventory(self.now())
        _hot_count, universe_count = self._fixture_state.membership_counts(self.now())
        with self._state_lock:
            self._universe_generation_id = checkpoint.generation_id
            self._universe_generation_started_at = checkpoint.generation_started_at
            self._universe_work_used = checkpoint.successful_work_used_s
            self._universe_cursor = checkpoint.resume_cursor
            self._universe_evaluated_ids = set(checkpoint.evaluated_ids)
            self._universe_progress_generation_id = checkpoint.generation_id
            if checkpoint.next_universe_due is not None:
                self._next_universe_due = checkpoint.next_universe_due
            self._universe_provider_failures = checkpoint.provider_failure_count
            self._universe_retry_at = checkpoint.retry_at
            self._universe_budget_paused = False
            self._universe_sweep_id = checkpoint.sweep_id or (
                f"sweep-{checkpoint.generation_id}-{checkpoint.generation_started_at.isoformat()}"
            )
            self._universe_discovered_total = checkpoint.discovered_total
            self._universe_failed_ids = dict(checkpoint.failed_ids)
            self._universe_skipped_ids = dict(checkpoint.skipped_ids)
            self._universe_last_successful = checkpoint.last_successful_fixture
            self._universe_matched_fixtures = checkpoint.matched_fixtures
            self._universe_equivalent_markets = checkpoint.equivalent_markets
            self._universe_near_count = checkpoint.near_count
            self._universe_positive_count = checkpoint.positive_count
            self._universe_qualifying_count = checkpoint.qualifying_count
            self._universe_hot_promotions = checkpoint.hot_promotions
            self._universe_raw_events = dict(checkpoint.raw_events_by_venue)
            self._universe_series_work = {
                key: value if isinstance(value, SeriesWorkUnit) else SeriesWorkUnit.model_validate(value)
                for key, value in (checkpoint.series_work or {}).items()
            }
            self._universe_series_applied_this_cycle = set()
            self._universe_work = {
                key: value if isinstance(value, SweepWorkUnit) else SweepWorkUnit.model_validate(value)
                for key, value in (checkpoint.work_units or {}).items()
            }
            if not self._universe_work and checkpoint.evaluated_ids:
                self._universe_work = {
                    item: SweepWorkUnit(canonical_id=item, state=SWEEP_EVALUATED)
                    for item in checkpoint.evaluated_ids
                }
            self._seed_universe_rehydration_unlocked()
            self.status = self.status.model_copy(
                update={
                    "discovered_fixtures": inventory,
                    "universe": self.status.universe.model_copy(
                        update={
                            "generation_work_used_s": round(self._universe_work_used, 3),
                            **self._lane_progress_fields(),
                            "resume_cursor": self._universe_cursor,
                            "fixture_count": universe_count,
                            "next_due_at": self._next_universe_due,
                            "sweep_id": self._universe_sweep_id,
                            "matched_fixtures": self._universe_matched_fixtures,
                            "equivalent_markets": self._universe_equivalent_markets,
                            "hot_promotions": self._universe_hot_promotions,
                            "last_successful_fixture": self._universe_last_successful,
                            "worker_state": WORKER_WAITING
                            if self._universe_retry_at is not None
                            else WORKER_IDLE,
                            "degraded": self._universe_provider_failures > 0,
                        }
                    ),
                }
            )

    def _seed_universe_rehydration_unlocked(self) -> None:
        """Mark restored EVALUATED IDs as missing process-memory current-state.

        Compact checkpoints do not persist FixtureCurrentStateStore rows or
        ApprovedEquivalent HOT relationships. A fresh process must rediscover
        and re-upsert those fixtures before skip_event_ids may omit them.
        The set is process-memory only and is never written to SQLite.
        """

        pending = {
            canonical_id
            for canonical_id, unit in self._universe_work.items()
            if unit.state == SWEEP_EVALUATED
        }
        pending.update(self._universe_evaluated_ids)
        self._universe_needs_rehydration = pending
        self._universe_rehydration_retry_at = {}
        self._universe_rehydration_attempts = {}

    def _schedule_universe_rehydration_retry_unlocked(
        self, canonical_id: str, scanned: datetime
    ) -> None:
        """Backoff a failed current-state rebuild without changing durable EVALUATED."""

        attempts = self._universe_rehydration_attempts.get(canonical_id, 0) + 1
        self._universe_rehydration_attempts[canonical_id] = attempts
        self._universe_rehydration_retry_at[canonical_id] = scanned + timedelta(
            seconds=universe_work_retry_backoff_seconds(attempts)
        )

    def _clear_universe_rehydration_unlocked(self, canonical_id: str) -> None:
        self._universe_needs_rehydration.discard(canonical_id)
        self._universe_rehydration_retry_at.pop(canonical_id, None)
        self._universe_rehydration_attempts.pop(canonical_id, None)

    def _ensure_universe_generation(self, started: datetime) -> None:
        if self._universe_generation_started_at is not None:
            return
        self._universe_generation_id += 1
        self._universe_generation_started_at = started
        self._universe_work_used = 0.0
        self._universe_evaluated_ids = set()
        self._universe_needs_rehydration = set()
        self._universe_rehydration_retry_at = {}
        self._universe_rehydration_attempts = {}
        self._universe_cursor = None
        self._universe_progress_generation_id = self._universe_generation_id
        self._universe_sweep_id = f"sweep-{self._universe_generation_id}-{started.isoformat()}"
        self._fixture_state.open_universe_generation(
            self._universe_generation_id, started_at=started
        )
        self._universe_discovery_snapshot = None
        self._universe_discovered_total = 0
        self._universe_failed_ids = {}
        self._universe_skipped_ids = {}
        self._universe_last_successful = None
        self._universe_current_fixture = None
        self._universe_matched_fixtures = 0
        self._universe_equivalent_markets = 0
        self._universe_near_count = 0
        self._universe_positive_count = 0
        self._universe_qualifying_count = 0
        self._universe_hot_promotions = 0
        self._universe_work = {}
        self._universe_raw_events = {}
        self._universe_series_results = {}
        self._universe_series_work = {}
        self._universe_series_applied_this_cycle = set()
        self._mark_universe_checkpoint_dirty_unlocked()

    def _charge_successful_universe_work(
        self,
        duration_s: float,
        finished: datetime,
        *,
        leftover_n: int,
        completeness: str | None,
    ) -> float:
        settings = get_settings()
        budget = float(settings.paper_scan_universe_generation_budget_seconds)
        if self._universe_generation_started_at is None:
            return budget
        self._universe_work_used += max(0.0, duration_s)
        if self._universe_work or self._universe_series_work:
            complete = self._universe_sweep_is_complete_unlocked()
        else:
            complete = leftover_n == 0
        if complete and completeness != UNIVERSE_COMPLETENESS_STALE_GENERATION_STATE:
            self._close_universe_generation(finished)
        return budget

    def _pause_universe_generation(self, finished: datetime) -> None:
        if self._universe_generation_started_at is None:
            return
        settings = get_settings()
        interval = timedelta(seconds=settings.paper_universe_worker_cooldown_seconds)
        self._next_universe_due = finished + interval
        self._universe_budget_paused = True

    def _refresh_paused_universe_window_unlocked(self) -> None:
        if not self._universe_budget_paused:
            return
        self._universe_work_used = 0.0
        self._universe_budget_paused = False
        self._mark_universe_checkpoint_dirty_unlocked()

    def _close_universe_generation(self, finished: datetime) -> None:
        if self._universe_generation_started_at is None:
            return
        settings = get_settings()
        cooldown = timedelta(seconds=settings.paper_universe_worker_cooldown_seconds)
        self._next_universe_due = finished + cooldown
        self._fixture_state.close_universe_generation(
            self._universe_generation_id, closed_at=finished
        )
        self._clear_universe_generation_local_state()
        self._universe_generation_started_at = None
        self._universe_budget_paused = False
        self._universe_retry_at = None
        self._universe_provider_failures = 0
        self._universe_last_report_snapshot = None
        self._mark_universe_checkpoint_dirty_unlocked()

    def _advance_hot_due(self, now: datetime) -> None:
        settings = get_settings()
        interval = timedelta(seconds=settings.paper_live_refresh_hot_interval_seconds)
        due = self._hot_due_started or self._next_hot_due or now
        nxt = due + interval
        evaluated = require_aware_instant(now, "now")
        while nxt <= evaluated:
            nxt += interval
        self._next_hot_due = nxt
        self._hot_due_started = None

    def _mark_lane_started(self, lane: ScanLane, started: datetime) -> None:
        unique = lifecycle = promoted = 0
        if lane is ScanLane.HOT:
            unique, lifecycle, promoted = self._fixture_state.hot_membership_breakdown(
                started, **self.radar_horizon_kwargs()
            )
        with self._state_lock:
            if lane is ScanLane.HOT:
                self._hot_in_progress = True
                self._cycle_hot_venues = self._pending_participation.venues_for(ScanLane.HOT)
                self._cycle_enabled_venues = self._cycle_hot_venues
            else:
                self._universe_in_progress = True
                self._universe_series_applied_this_cycle = set()
                self._cycle_universe_venues = self._pending_participation.venues_for(
                    ScanLane.UNIVERSE
                )
                self._cycle_enabled_venues = self._cycle_universe_venues
            top = {
                "cycle_in_progress": self._hot_in_progress or self._universe_in_progress,
                "last_started_at": started,
                "last_error": None,
            }
            if lane is ScanLane.HOT:
                self._hot_due_started = self._next_hot_due or started
                self.status = self.status.model_copy(
                    update={
                        **top,
                        "hot": self.status.hot.model_copy(
                            update={
                                "cycle_in_progress": True,
                                "worker_state": WORKER_RUNNING,
                                "last_started_at": started,
                                "last_error": None,
                                "last_persist_error": None,
                                "persist_ok": None,
                                "fixture_count": unique,
                                "lifecycle_hot_count": lifecycle,
                                "promoted_hot_count": promoted,
                            }
                        ),
                    }
                )
                self._sync_venue_status_unlocked()
                return
            self._ensure_universe_generation(started)
            self._open_universe_chunk_epoch_unlocked()
            chunk_timeout = self._universe_chunk_collector_timeout(now=started)
            counts = self._lane_progress_fields()
            self.status = self.status.model_copy(
                update={
                    **top,
                    "universe": self.status.universe.model_copy(
                        update={
                            "cycle_in_progress": True,
                            "worker_state": WORKER_RUNNING,
                            "sweep_id": self._universe_sweep_id,
                            "generation_id": self._universe_generation_id or None,
                            "cycle_timeout_seconds": chunk_timeout,
                            "last_started_at": started,
                            "last_heartbeat_at": started,
                            "last_error": None,
                            "last_persist_error": None,
                            "persist_ok": None,
                            "discovered_total": self._universe_discovered_total,
                            "evaluated_count": len(self._universe_evaluated_ids),
                            "remaining": max(
                                0,
                                self._universe_discovered_total
                                - len(self._universe_evaluated_ids),
                            ),
                            **counts,
                        }
                    ),
                }
            )
            self._apply_universe_honesty_unlocked()
            self._sync_venue_status_unlocked()
        self._request_universe_checkpoint_persist(force=True)

    def _mark_manual_hot_started(self, started: datetime) -> None:
        """Expose manual HOT activity without claiming a scheduled due slot."""

        with self._state_lock:
            self._manual_hot_in_progress = True
            self._cycle_hot_venues = self._pending_participation.venues_for(ScanLane.HOT)
            self._cycle_enabled_venues = self._cycle_hot_venues
            self.status = self.status.model_copy(
                update={
                    "cycle_in_progress": True,
                    "last_started_at": started,
                    "last_error": None,
                    "hot": self.status.hot.model_copy(
                        update={
                            "cycle_in_progress": True,
                            "worker_state": WORKER_RUNNING,
                            "last_started_at": started,
                            "last_error": None,
                            "last_persist_error": None,
                            "persist_ok": None,
                        }
                    ),
                }
            )
            self._sync_venue_status_unlocked()

    def _mark_lane_error(
        self,
        lane: ScanLane,
        started: datetime,
        finished: datetime,
        message: str,
        *,
        advance_hot_due: bool = True,
    ) -> None:
        universe_count = 0
        if lane is not ScanLane.HOT:
            _, universe_count = self._fixture_state.membership_counts(finished)
        with self._state_lock:
            duration = max(0, int((finished - started).total_seconds() * 1000))
            if lane is ScanLane.HOT:
                self._hot_in_progress = False
                self._cycle_hot_venues = None
            else:
                self._universe_in_progress = False
                self._cycle_universe_venues = None
            update: dict[str, Any] = {
                "cycle_in_progress": self._hot_in_progress or self._universe_in_progress,
                "last_error": message,
                "last_completed_at": self.status.last_completed_at or finished,
                "last_duration_ms": duration,
            }
            if lane is ScanLane.HOT:
                if advance_hot_due:
                    self._advance_hot_due(finished)
                update["hot"] = self.status.hot.model_copy(
                    update={
                        "cycle_in_progress": False,
                        "worker_state": WORKER_DEGRADED,
                        "last_error": message,
                        "last_completed_at": finished,
                        "last_duration_ms": duration,
                        "next_due_at": self._next_hot_due,
                    }
                )
                update["last_completed_at"] = finished
            else:
                self._ensure_universe_generation(started)
                self._invalidate_universe_chunk_epoch_unlocked()
                self._universe_provider_failures += 1
                delay = universe_provider_backoff_seconds(self._universe_provider_failures)
                self._universe_retry_at = finished + timedelta(seconds=delay)
                budget = float(get_settings().paper_scan_universe_generation_budget_seconds)
                work_used = self._status_universe_work_used()
                evaluated_count = self._status_universe_evaluated_count()
                self._mark_universe_checkpoint_dirty_unlocked()
                update["universe"] = self.status.universe.model_copy(
                    update={
                        "cycle_in_progress": False,
                        "worker_state": WORKER_WAITING,
                        "last_error": message,
                        "last_completed_at": finished,
                        "last_duration_ms": duration,
                        "last_heartbeat_at": finished,
                        "chunk_last_duration_ms": duration,
                        "generation_work_used_s": round(work_used, 3),
                        "evaluated_count": evaluated_count,
                        "fixture_count": universe_count,
                        "degraded": True,
                        "sweep_id": self._universe_sweep_id,
                        "resume_cursor": self._status_universe_cursor(),
                        "next_due_at": self._next_universe_due,
                        "operator_summary": _universe_operator_summary(
                            duration,
                            work_used,
                            budget,
                            universe_count,
                            evaluated_count,
                            self.status.universe.not_evaluated_count,
                            discovered_total=self._universe_discovered_total,
                            worker_state=WORKER_WAITING,
                            venue_health=self.status.universe.venue_health,
                            active_venues=self.status.universe.active_venues,
                        ),
                    }
                )
                if self.status.last_completed_at is None:
                    update["last_completed_at"] = finished
            self.status = self.status.model_copy(update=update)
            if lane is not ScanLane.HOT:
                self._apply_universe_honesty_unlocked()
            self._cycle_enabled_venues = None
            self._sync_venue_status_unlocked()
        if lane is not ScanLane.HOT:
            self._request_universe_checkpoint_persist(force=True)

    def last_report(self) -> CollectionReport | None:
        return self._last_report

    def fixture_current_state(self) -> FixtureCurrentStateStore:
        return self._fixture_state

    def hot_market_relationships(
        self, canonical_ids: list[str]
    ) -> dict[str, list[HotMarketRelationship]]:
        """Coordinator facade over current-state ApprovedEquivalent HOT identities."""

        return self._fixture_state.hot_market_relationships(canonical_ids, now=self.now())

    def _universe_generation_id_for_upsert(self) -> int | None:
        with self._state_lock:
            if self._universe_generation_started_at is None:
                return None
            return self._universe_generation_id

    def _diagnostics_belong_to_current_generation(
        self, diagnostics: dict[str, Any] | None
    ) -> bool:
        if not diagnostics:
            return True
        current_sweep = self._universe_sweep_id
        diag_sweep = diagnostics.get("sweep_id")
        if current_sweep and diag_sweep and str(diag_sweep) != str(current_sweep):
            return False
        diag_gen = diagnostics.get("generation_id")
        if diag_gen is not None:
            try:
                if int(diag_gen) != int(self._universe_generation_id):
                    return False
            except (TypeError, ValueError):
                return False
        if self._universe_generation_started_at is not None and not diag_sweep:
            completeness = str(diagnostics.get("completeness") or "").casefold()
            if completeness == "complete":
                return False
        return True

    def _universe_live_worker_state_unlocked(self) -> str:
        if self._universe_in_progress:
            return WORKER_RUNNING
        if self._universe_generation_started_at is None:
            if self.status.universe.worker_state == WORKER_COMPLETE:
                return WORKER_COMPLETE
            return self.status.universe.worker_state or WORKER_IDLE
        retry_wait = self._earliest_retry_wait_unlocked(self.now())
        if self._universe_retry_at is not None or retry_wait is not None:
            return WORKER_WAITING
        if self.status.universe.degraded:
            return WORKER_DEGRADED
        if self.status.universe.worker_state == WORKER_COMPLETE:
            return WORKER_WAITING
        return self.status.universe.worker_state or WORKER_IDLE

    def _apply_universe_honesty_unlocked(self) -> None:
        """Keep UNIVERSE operator status generation-consistent.

        A running/waiting generation must not display a prior generation's
        complete summary or diagnostics. cycle_in_progress follows the actual
        in-progress flag so retry_wait cannot look permanently running.
        """

        current = self.status.universe
        worker_state = self._universe_live_worker_state_unlocked()
        diagnostics = current.last_diagnostics
        if not self._diagnostics_belong_to_current_generation(diagnostics):
            diagnostics = None
        counts = self._lane_progress_fields() if self._universe_generation_started_at is not None else {}
        evaluated = (
            counts.get("canonical_evaluated", self._status_universe_evaluated_count())
            if counts
            else self._status_universe_evaluated_count()
        )
        discovered = (
            counts.get("canonical_work_total", self._universe_discovered_total)
            if counts
            else (current.canonical_work_total or current.discovered_total or 0)
        )
        leftover = current.not_evaluated_count
        remaining = (
            counts.get("canonical_remaining")
            if counts
            else max(0, int(discovered or 0) - int(evaluated or 0))
        )
        if remaining is None:
            remaining = max(0, int(discovered or 0) - int(evaluated or 0))
        budget = float(get_settings().paper_scan_universe_generation_budget_seconds)
        update = {
            "cycle_in_progress": bool(self._universe_in_progress),
            "worker_state": worker_state,
            "sweep_id": self._universe_sweep_id or current.sweep_id,
            "generation_id": self._universe_generation_id or current.generation_id,
            "last_diagnostics": diagnostics,
            "operator_summary": _universe_operator_summary(
                current.last_duration_ms or 0,
                self._status_universe_work_used(),
                budget,
                current.fixture_count,
                int(evaluated or 0),
                leftover,
                discovered_total=int(discovered or 0),
                remaining=int(remaining or 0),
                worker_state=worker_state,
                venue_health=current.venue_health,
                active_venues=current.active_venues,
                plan_reason=current.last_plan_reason,
            ),
        }
        if counts:
            update.update(counts)
        self.status = self.status.model_copy(
            update={
                "universe": current.model_copy(update=update),
                "cycle_in_progress": self._hot_in_progress
                or self._universe_in_progress
                or self._manual_hot_in_progress,
            }
        )

    def _ensure_store_universe_generation(self, scanned: datetime) -> int:
        with self._state_lock:
            if self._universe_generation_started_at is None:
                self._ensure_universe_generation(scanned)
            generation_id = self._universe_generation_id
            started = self._universe_generation_started_at
        if started is not None:
            self._fixture_state.open_universe_generation(generation_id, started_at=started)
        return generation_id

    def public_status(self) -> LiveRefreshStatus:
        """Current Discovery/Tracked inventory as of now, after lifecycle eviction."""

        now = self.now()
        horizon = self.radar_horizon_kwargs()
        classify = {
            "hot_horizon": horizon["hot_horizon"],
            "post_kickoff_unknown_horizon": horizon["post_kickoff_unknown_horizon"],
            "post_kickoff_current_radar_ceiling": horizon["post_kickoff_current_radar_ceiling"],
        }
        inventory = self._fixture_state.inventory(
            now,
            **classify,
            hot_interval_seconds=horizon["hot_interval_seconds"],
            universe_interval_seconds=horizon["universe_interval_seconds"],
            hot_ttl_seconds=horizon["hot_ttl_seconds"],
            universe_ttl_seconds=horizon["universe_ttl_seconds"],
        )
        hot_count, universe_count = self._fixture_state.membership_counts(now, **classify)
        unique, lifecycle, promoted = self._fixture_state.hot_membership_breakdown(
            now, **classify
        )
        engine_status = (
            self._price_engine.public_status(now=now)
            if self._price_engine is not None
            else empty_price_engine_status()
        )
        with self._state_lock:
            hot_update = {
                "fixture_count": unique or hot_count,
                "lifecycle_hot_count": lifecycle,
                "promoted_hot_count": promoted,
                "cycle_in_progress": self._hot_in_progress or self._manual_hot_in_progress,
                "evaluated_count": engine_status.hot.evaluated,
                "not_evaluated_count": (
                    engine_status.hot.not_started_this_cadence
                    + engine_status.hot.retry_wait
                    + engine_status.hot.deferred
                ),
                "operation_health": dict(engine_status.hot.operation_health)
                or dict(self.status.hot.operation_health),
                "venue_health": dict(engine_status.hot.venue_health)
                or dict(self.status.hot.venue_health),
            }
            background_update = {
                "cycle_in_progress": self._background_in_progress,
                "next_due_at": self._next_background_due,
                "evaluated_count": engine_status.background.evaluated,
                "not_evaluated_count": (
                    engine_status.background.not_started_this_cadence
                    + engine_status.background.retry_wait
                    + engine_status.background.deferred
                ),
                "operation_health": dict(engine_status.background.operation_health)
                or dict(self.status.background.operation_health),
                "venue_health": dict(engine_status.background.venue_health)
                or dict(self.status.background.venue_health),
                "worker_state": WORKER_RUNNING
                if self._background_in_progress
                else self.status.background.worker_state,
            }
            hot = self.status.hot.model_copy(update=hot_update)
            background = self.status.background.model_copy(update=background_update)
            self.status = self.status.model_copy(
                update={
                    "discovered_fixtures": inventory,
                    "hot": hot,
                    "background": background,
                    "price_engine": engine_status,
                    "universe": self.status.universe.model_copy(
                        update={"fixture_count": universe_count}
                    ),
                    "venue_health": _merge_top_level_venue_health(
                        hot.venue_health,
                        background.venue_health,
                        self.status.universe.venue_health,
                    ),
                    "provider_access": get_shared_provider_access().snapshot().as_dict(),
                    "cycle_in_progress": self._hot_in_progress
                    or self._universe_in_progress
                    or self._background_in_progress
                    or self._manual_hot_in_progress,
                }
            )
            self._apply_universe_honesty_unlocked()
            self.status = self.status.model_copy(
                update={
                    "operator_summary": _combined_operator_summary(
                        self.status.hot, self.status.universe, universe_count
                    ),
                    "cycle_in_progress": self._hot_in_progress
                    or self._universe_in_progress
                    or self._background_in_progress
                    or self._manual_hot_in_progress,
                    "system_load": system_load_from_status(
                        self.status,
                        universe_work_used_s=self._status_universe_work_used(),
                    ),
                }
            )
            return self.status

    def fixture_detail(self, canonical_event_id: str) -> FixtureDetailReadModel | None:
        return self._fixture_state.detail(
            canonical_event_id, now=self.now(), **self.radar_horizon_kwargs()
        )

    def fixture_identities(self, canonical_event_id: str) -> frozenset[str]:
        return self._fixture_state.identities_for(canonical_event_id)

    def radar_horizon_kwargs(self, settings: Settings | None = None) -> dict[str, Any]:
        resolved = settings or get_settings()
        return {
            "hot_horizon": timedelta(minutes=resolved.paper_hot_pre_kickoff_horizon_minutes),
            "post_kickoff_unknown_horizon": timedelta(
                hours=resolved.paper_hot_post_kickoff_unknown_horizon_hours
            ),
            "post_kickoff_current_radar_ceiling": timedelta(
                hours=resolved.paper_hot_post_kickoff_current_radar_ceiling_hours
            ),
            "hot_ttl_seconds": resolved.paper_hot_current_state_ttl_seconds,
            "universe_ttl_seconds": resolved.paper_universe_current_state_ttl_seconds,
            "hot_interval_seconds": resolved.paper_live_refresh_hot_interval_seconds,
            "universe_interval_seconds": resolved.paper_live_refresh_universe_interval_seconds,
        }

    async def start_server_loop(self, tick) -> None:
        self.configure_from_settings()
        if not self.status.server_loop_enabled:
            return
        if self._hot_task is not None and not self._hot_task.done():
            return
        self._stop = asyncio.Event()
        self._hot_task = asyncio.create_task(self._hot_loop(tick), name="hot-worker")
        self._universe_task = asyncio.create_task(
            self._universe_loop(tick), name="universe-worker"
        )
        self._background_task = asyncio.create_task(
            self._background_loop(tick), name="background-price-worker"
        )
        self._task = self._hot_task

    async def stop_server_loop(self) -> None:
        self._stop.set()
        await self._await_universe_checkpoint_persist()
        persist_task = self._universe_checkpoint_persist_task
        if persist_task is not None and not persist_task.done():
            persist_task.cancel()
            try:
                await persist_task
            except (asyncio.CancelledError, Exception):
                pass
        for task in (self._hot_task, self._universe_task, self._background_task, self._task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._hot_task = None
        self._universe_task = None
        self._background_task = None
        self._task = None

    async def _invoke_tick(self, tick, plan: DualCadencePlan | None = None) -> None:
        try:
            result = tick(plan) if plan is not None else tick()
        except TypeError:
            result = tick()
        if asyncio.iscoroutine(result):
            await result

    async def _hot_loop(self, tick) -> None:
        while not self._stop.is_set():
            plan = self.plan_hot_tick()
            self._record_hot_heartbeat(plan)
            if plan.lane == ScanLane.HOT.value or plan.reason == "hot_scope_empty":
                try:
                    await self._invoke_tick(tick, plan)
                except asyncio.CancelledError:
                    raise
                except (MatchbookAuthError, MatchbookDiscoveryError, ScanCycleTimeout, Exception):
                    pass
            delay = min(self._seconds_until_hot(), float(self.status.interval_seconds))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.05, delay))
            except TimeoutError:
                continue

    def _record_hot_heartbeat(self, plan: DualCadencePlan) -> None:
        """Mark the HOT worker alive, including intentional empty-scope idle.

        Empty scope must not look like a dead/never-scheduled worker and must
        not trigger a dummy provider call.
        """

        now = self.now()
        with self._state_lock:
            hot_update: dict[str, Any] = {
                "last_heartbeat_at": now,
                "last_plan_reason": plan.reason,
            }
            if plan.lane != ScanLane.HOT.value and not self._hot_in_progress:
                if plan.reason == "hot_scope_empty":
                    hot_update["worker_state"] = WORKER_WAITING
                    hot_update["cycle_in_progress"] = False
                    hot_update["operator_summary"] = (
                        "Fast scan · worker alive · scope empty · polling · no provider call"
                    )
                elif plan.reason == "waiting" and self.status.hot.last_started_at is None:
                    hot_update["worker_state"] = WORKER_WAITING
                    hot_update["operator_summary"] = (
                        "Fast scan · worker alive · waiting · no provider call"
                    )
            self.status = self.status.model_copy(
                update={"hot": self.status.hot.model_copy(update=hot_update)}
            )

    async def _universe_loop(self, tick) -> None:
        while not self._stop.is_set():
            plan = self.plan_universe_tick()
            self._record_universe_heartbeat(plan)
            if plan.lane == ScanLane.UNIVERSE.value:
                try:
                    await self._invoke_tick(tick, plan)
                except asyncio.CancelledError:
                    raise
                except (MatchbookAuthError, MatchbookDiscoveryError, ScanCycleTimeout, Exception):
                    pass
            delay = min(self._seconds_until_universe(), 30.0)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.05, delay))
            except TimeoutError:
                continue

    def _record_universe_heartbeat(self, plan: DualCadencePlan) -> None:
        """Mark the UNIVERSE worker alive, including retry/backoff idle.

        Retry wait must not look like a permanently running in-progress cycle.
        """

        now = self.now()
        with self._state_lock:
            universe_update: dict[str, Any] = {
                "last_heartbeat_at": now,
                "last_plan_reason": plan.reason,
            }
            if plan.lane != ScanLane.UNIVERSE.value and not self._universe_in_progress:
                universe_update["cycle_in_progress"] = False
            self.status = self.status.model_copy(
                update={"universe": self.status.universe.model_copy(update=universe_update)}
            )
            self._apply_universe_honesty_unlocked()

    async def _background_loop(self, tick) -> None:
        while not self._stop.is_set():
            plan = self.plan_background_tick()
            if plan.lane == "background":
                with self._state_lock:
                    self._background_in_progress = True
                try:
                    await self._invoke_tick(tick, plan)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    with self._state_lock:
                        settings = get_settings()
                        self._next_background_due = self.now() + timedelta(
                            seconds=settings.paper_live_refresh_universe_interval_seconds
                        )
                finally:
                    with self._state_lock:
                        self._background_in_progress = False
            delay = min(self._seconds_until_background(), 30.0)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.05, delay))
            except TimeoutError:
                continue

    def _seconds_until_background(self) -> float:
        now = self.now()
        if self._background_in_progress:
            return 1.0
        if self._next_background_due is None:
            return 0.05
        return max(0.05, (self._next_background_due - now).total_seconds())

    def _seconds_until_hot(self) -> float:
        now = self.now()
        if self._hot_in_progress:
            return 0.25
        if self._next_hot_due is None:
            return 0.05
        return max(0.05, (self._next_hot_due - now).total_seconds())

    def _seconds_until_universe(self) -> float:
        now = self.now()
        if self._universe_in_progress:
            return 1.0
        if self._universe_retry_at is not None and self._universe_retry_at > now:
            return max(0.05, (self._universe_retry_at - now).total_seconds())
        work_retry = self._earliest_retry_wait_unlocked(now)
        if work_retry is not None:
            return max(0.05, (work_retry - now).total_seconds())
        if self._universe_generation_started_at is not None:
            return 0.05
        if self._next_universe_due is None:
            return 0.05
        return max(0.05, (self._next_universe_due - now).total_seconds())

    async def _loop(self, tick) -> None:
        await self._hot_loop(tick)


_COORDINATOR = LiveRefreshCoordinator()


def get_live_refresh_coordinator() -> LiveRefreshCoordinator:
    return _COORDINATOR


def _consume_orphaned_task_result(task: asyncio.Task[Any]) -> None:
    if not task.done():
        return
    try:
        task.result()
    except asyncio.CancelledError:
        return
    except Exception as exc:
        LOGGER.info("orphaned collection task finished with %s", type(exc).__name__)


def _collection_task_result(task: asyncio.Task[Any]) -> CollectionReport:
    try:
        return task.result()
    except asyncio.CancelledError as exc:
        raise TimeoutError from exc


async def _await_collection_runner(runner, timeout: float | None) -> CollectionReport:
    """Wait for collection/aclose on a child task; persist stays outside this wait.

    `asyncio.wait_for(coro)` is not used here: a timeout context would cancel
    the scheduler tick, and a swallowed leftover is not TimeoutError. Persist
    after a timely leftover *inside* that wait is the envelope overrun. This
    helper cancels only the child and harvests leftover + aclose inside
    `timeout` so the coordinator envelope stays ≤30s for HOT. If the child
    ignores cancellation, harvest still returns and the leftover task is
    attached to CollectionRunnerTimeout for the coordinator to quarantine.
    """

    if timeout is None:
        return await runner()
    task = asyncio.create_task(runner())
    started = monotonic()
    harvest = min(SCAN_CYCLE_PARTIAL_HARVEST_SECONDS, max(0.05, float(timeout) / 2))
    try:
        wait_budget = max(0.0, float(timeout) - harvest)
        done, _pending = await asyncio.wait({task}, timeout=wait_budget)
        if task in done:
            return _collection_task_result(task)
        task.cancel()
        remaining = max(0.0, float(timeout) - (monotonic() - started))
        if remaining > 0:
            await asyncio.wait({task}, timeout=remaining)
        if task.done():
            return _collection_task_result(task)
        raise CollectionRunnerTimeout(task)
    finally:
        if not task.done():
            task.cancel()


def _lane_diagnostics(
    report: CollectionReport,
    *,
    series_work: dict[str, SeriesWorkUnit] | None = None,
    sweep_id: str | None = None,
    generation_id: int | None = None,
) -> dict[str, Any]:
    payload = dict(report.scan_diagnostics or {})
    if series_work:
        payload["series_work"] = {
            key: unit.model_dump(mode="json") for key, unit in series_work.items()
        }
    resolved_sweep = sweep_id or report.sweep_id
    if resolved_sweep:
        payload["sweep_id"] = resolved_sweep
    if generation_id is not None:
        payload["generation_id"] = generation_id
    payload.setdefault("soft_deadline_reached", False)
    payload.setdefault("cancelled", False)
    payload.setdefault("provider_cancels", 0)
    payload.setdefault("cancel_count", payload.get("provider_cancels", 0))
    payload.setdefault("inflight_orphaned", 0)
    payload.setdefault("inflight_live", 0)
    payload.setdefault("provider_calls", 0)
    leftover_n = sum(
        1
        for item in report.discovered_fixtures
        if item.market_evaluation_state == "not_evaluated_scan_deadline"
    )
    evaluated_n = sum(
        1
        for item in report.discovered_fixtures
        if item.market_evaluation_state == "evaluated"
    )
    payload.setdefault("evaluated_count", evaluated_n)
    payload.setdefault("not_evaluated_count", leftover_n)
    payload.setdefault("paper_decision_count", len(report.paper_decisions))
    payload.setdefault("qualifying_arb_count", report.qualifying_arbs)
    payload.setdefault("timeout_count", 0)
    completeness = payload.get("completeness")
    if completeness is None:
        if leftover_n or payload.get("soft_deadline_reached") or payload.get("cancelled"):
            completeness = "deadline_leftover"
        elif evaluated_n == 0:
            completeness = "empty_universe"
        else:
            completeness = "complete"
        payload["completeness"] = completeness
    payload["partial"] = bool(
        leftover_n
        or payload.get("soft_deadline_reached")
        or payload.get("cancelled")
        or completeness == UNIVERSE_COMPLETENESS_STALE_GENERATION_STATE
    )
    providers = dict(payload.get("providers") or {})
    for name in DIAGNOSTIC_PROVIDERS:
        providers.setdefault(name, {"calls": 0, "elapsed_ms": 0, "timeouts": 0, "cancels": 0})
    payload["providers"] = providers
    stages = dict(payload.get("stages") or {})
    for name in DIAGNOSTIC_STAGES:
        if name == "persistence":
            stages.setdefault(
                name,
                {"calls": 0, "elapsed_ms": 0, "timeouts": 0, "cancels": 0, "ok": None, "error": None},
            )
        else:
            stages.setdefault(
                name, {"calls": 0, "elapsed_ms": 0, "timeouts": 0, "cancels": 0}
            )
    payload["stages"] = stages
    return payload


def _coerce_lane(value: ScanLane | str | None) -> ScanLane:
    if value is None:
        return ScanLane.UNIVERSE
    if isinstance(value, ScanLane):
        return ScanLane.UNIVERSE if value is ScanLane.DROP else value
    text = str(value).strip().casefold()
    if text == ScanLane.HOT.value:
        return ScanLane.HOT
    return ScanLane.UNIVERSE


def _frozen_venue_health(venue_health: dict[str, str] | None) -> dict[str, str]:
    """Copy a completed scan's provider-health map so later lanes cannot mutate it."""

    return dict(venue_health or {})


def _iso_stamp(moment: datetime | None) -> str:
    if moment is None:
        return "—"
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _hot_operator_summary(
    completed_at: datetime,
    duration_ms: int,
    next_due: datetime | None,
    fixture_count: int,
    leftover_n: int,
    degraded: bool,
    *,
    venue_health: dict[str, str] | None = None,
    active_venues: list[VenueName] | None = None,
) -> str:
    duration_s = round(duration_ms / 1000, 1)
    venue_clause = last_scan_venue_clause(venue_health, configured=active_venues)
    summary = (
        f"Fast scan · completed at {_iso_stamp(completed_at)} · ran {duration_s}s · "
        f"next due {_iso_stamp(next_due)} · {fixture_count} hot · {venue_clause}"
    )
    if leftover_n:
        summary += f" · partial ({leftover_n} not evaluated)"
    elif degraded:
        summary += " · partial (provider degraded)"
    return summary


def _universe_operator_summary(
    duration_ms: int,
    work_used: float,
    budget: float,
    fixture_count: int,
    evaluated_n: int,
    leftover_n: int,
    *,
    discovered_total: int = 0,
    remaining: int | None = None,
    worker_state: str = WORKER_IDLE,
    venue_health: dict[str, str] | None = None,
    active_venues: list[VenueName] | None = None,
    plan_reason: str | None = None,
) -> str:
    del budget
    elapsed_s = round(max(work_used, duration_ms / 1000), 1)
    venue_clause = last_scan_venue_clause(venue_health, configured=active_venues)
    discovered = discovered_total or fixture_count
    computed_remaining = remaining if remaining is not None else max(0, discovered - evaluated_n)
    state = worker_state or WORKER_IDLE
    reason = str(plan_reason or "")
    if reason in {"universe_retry_wait", "universe_provider_backoff"} and state == WORKER_WAITING:
        state = "waiting · retry"
    elif reason == "universe_cooldown" and state in {WORKER_IDLE, WORKER_WAITING, WORKER_COMPLETE}:
        state = "waiting"
    return (
        f"Full sweep · {state} · elapsed {elapsed_s}s · "
        f"{evaluated_n}/{discovered} evaluated · {computed_remaining} remaining · "
        f"{fixture_count} universe · {leftover_n} leftover · {venue_clause}"
    )


def _combined_operator_summary(
    hot: LaneRefreshStatus,
    universe: LaneRefreshStatus,
    universe_count: int,
) -> str:
    fast = hot.operator_summary or (
        f"Fast scan · never · ran — · next due — · {hot.fixture_count} hot"
    )
    full = universe.operator_summary or (
        f"Full sweep · {universe.worker_state or WORKER_IDLE} · "
        f"{universe.evaluated_count}/{universe.discovered_total or universe_count} evaluated · "
        f"{universe_count} universe"
    )
    return f"{fast} · {full}"


def _honest_universe_venue_health(
    venue_health: dict[str, str] | None,
    *,
    retryable: int,
) -> dict[str, str]:
    """Retryable leftover work is not a green sweep, even if one venue stayed ok."""

    updated = dict(venue_health or {})
    if retryable <= 0:
        return updated
    if any(value in _UNHEALTHY_VENUE_HEALTH for value in updated.values()):
        return updated
    painted = False
    for key, value in list(updated.items()):
        if value == "ok":
            updated[key] = "retry_wait"
            painted = True
    if not painted:
        updated["universe"] = "retry_wait"
    return updated


_UNHEALTHY_VENUE_HEALTH = frozenset(
    {
        "timeout",
        HEALTH_DISCOVERY_TIMEOUT,
        HEALTH_MARKET_TIMEOUT,
        "degraded",
        HEALTH_UNAVAILABLE,
        HEALTH_AUTH_FAILURE,
        "error",
        "failed",
        "retry_wait",
        "partial",
    }
)
_SCHEDULER_VENUE_HEALTH = frozenset(
    {"waiting", "deferred", "rate_limited", "cancelled", "provider_capacity_saturated"}
)


def _merge_top_level_venue_health(
    *lane_maps: dict[str, str] | None,
) -> dict[str, str]:
    """Top-level ok only when every relevant lane path is healthy.

    One healthy lane must never paint the other lane's provider failure green.
    Scheduler wait / capacity saturation is not a provider outage.
    A Kalshi operation failure never becomes Matchbook FAILED.
    """

    maps = [dict(item or {}) for item in lane_maps]
    merged: dict[str, str] = {}
    keys: set[str] = set()
    for item in maps:
        keys.update(item)
    for venue in keys:
        values = [item.get(venue) for item in maps if item.get(venue)]
        bad = [value for value in values if value in _UNHEALTHY_VENUE_HEALTH]
        ok = [value for value in values if value == "ok"]
        waiting = [value for value in values if value in _SCHEDULER_VENUE_HEALTH]
        if len(bad) >= 2:
            merged[venue] = bad[0] if len(set(bad)) == 1 else "degraded"
        elif bad and ok:
            merged[venue] = "degraded"
        elif bad:
            merged[venue] = bad[0]
        elif ok:
            merged[venue] = "ok"
        elif waiting:
            merged[venue] = "ok"
        elif values:
            merged[venue] = values[-1]
    return merged
