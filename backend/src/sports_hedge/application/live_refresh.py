from __future__ import annotations

import asyncio
import logging
import threading
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, Field

from sports_hedge.application.collector import (
    DIAGNOSTIC_PROVIDERS,
    DIAGNOSTIC_STAGES,
    UNIVERSE_COMPLETENESS_STALE_GENERATION_STATE,
    CollectionReport,
    DiscoveredFixture,
    FixtureDetailReadModel,
)
from sports_hedge.application.universe_checkpoint import (
    UniverseGenerationCheckpoint,
    checkpoint_from_payload,
    collection_report_from_snapshot,
    collection_report_snapshot,
    universe_provider_backoff_seconds,
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
    HEALTH_DISCOVERY_TIMEOUT,
    HEALTH_MARKET_TIMEOUT,
    get_shared_provider_access,
)
from sports_hedge.application.quote_freshness import require_aware_instant
from sports_hedge.application.scan_lanes import (
    WORKER_COMPLETE,
    WORKER_DEGRADED,
    WORKER_IDLE,
    WORKER_RUNNING,
    WORKER_WAITING,
    ScanLane,
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
    last_error: str | None = None
    last_diagnostics: dict[str, Any] | None = None
    last_persist_error: str | None = None
    persist_ok: bool | None = None
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
            cadence_seconds=180,
            generation_budget_seconds=150,
        )
    )
    venue_participation: LaneVenueParticipation | None = None
    recent_scan_cycles: list[PaperScanCycleRecord] = Field(default_factory=list)
    provider_access: dict[str, Any] = Field(default_factory=dict)


class DualCadencePlan(BaseModel):
    lane: Literal["hot", "universe", "idle"]
    collector_timeout_seconds: float | None = None
    coordinator_timeout_seconds: float | None = None
    identity_scope: list[str] | None = None
    resume_cursor: str | None = None
    skip_event_ids: list[str] = Field(default_factory=list)
    universe_generation_id: int = 0
    generation_resume: bool = False
    known_source_events: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    discovery_snapshot: dict[str, list[dict[str, Any]]] | None = None
    reuse_discovery: bool = False
    unbounded_cycle: bool = False
    sweep_id: str | None = None
    enabled_venues: list[VenueName] = Field(
        default_factory=lambda: list(default_operator_venues())
    )
    reason: str = ""


class LiveRefreshCoordinator:
    """Independent HOT and UNIVERSE workers with scoped locks (Tenet 19)."""

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        venue_settings_store: SqliteLaneVenueSettingsStore | None = None,
        universe_checkpoint_store: SqliteUniverseCheckpointStore | None = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._hot_lock = asyncio.Lock()
        self._universe_lock = asyncio.Lock()
        self._state_lock = threading.RLock()
        self._task: asyncio.Task[None] | None = None
        self._hot_task: asyncio.Task[None] | None = None
        self._universe_task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_request: dict[str, Any] = {}
        self._last_report: CollectionReport | None = None
        self._fixture_state = FixtureCurrentStateStore()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._hot_in_progress = False
        self._universe_in_progress = False
        self._manual_hot_in_progress = False
        self._next_hot_due: datetime | None = None
        self._next_universe_due: datetime | None = None
        self._hot_due_started: datetime | None = None
        self._universe_generation_id = 0
        self._universe_generation_started_at: datetime | None = None
        self._universe_work_used = 0.0
        self._universe_cursor: str | None = None
        self._universe_evaluated_ids: set[str] = set()
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
        self._universe_checkpoint_store = universe_checkpoint_store
        self._universe_checkpoint_restored = False
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
                            "cadence_seconds": resolved.paper_live_refresh_universe_interval_seconds,
                            "generation_budget_seconds": float(
                                resolved.paper_scan_universe_generation_budget_seconds
                            ),
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
        self._fixture_state.clear()
        with self._state_lock:
            self._last_request = {}
            self._last_report = None
            self._hot_in_progress = False
            self._universe_in_progress = False
            self._manual_hot_in_progress = False
            self._next_hot_due = None
            self._next_universe_due = None
            self._hot_due_started = None
            self._universe_generation_id = 0
            self._universe_generation_started_at = None
            self._universe_work_used = 0.0
            self._universe_cursor = None
            self._universe_evaluated_ids = set()
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
            self._clear_universe_checkpoint_unlocked()
            self._universe_checkpoint_restored = False
            self._cycle_hot_venues = None
            self._cycle_universe_venues = None
            self._cycle_enabled_venues = None
            self.status = LiveRefreshStatus(
                server_loop_enabled=False,
                interval_seconds=30,
            )
        self.configure_from_settings()

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
            ) = self._universe_plan_resume_state_unlocked()
            snapshot = (
                dict(self._universe_discovery_snapshot)
                if self._universe_discovery_snapshot
                else None
            )
            sweep_id = self._universe_sweep_id
            universe_venues = list(self._pending_participation.venues_for(ScanLane.UNIVERSE))
        if universe_retry_at is not None and evaluated < universe_retry_at:
            return DualCadencePlan(lane="idle", reason="universe_provider_backoff")
        universe_due = universe_generation_started_at is not None or (
            next_universe_due is not None and evaluated >= next_universe_due
        )
        if not universe_due:
            return DualCadencePlan(lane="idle", reason="waiting")
        reuse = bool(snapshot) and generation_resume
        return DualCadencePlan(
            lane=ScanLane.UNIVERSE.value,
            collector_timeout_seconds=None,
            coordinator_timeout_seconds=None,
            resume_cursor=universe_cursor,
            skip_event_ids=skip_event_ids,
            universe_generation_id=universe_generation_id,
            generation_resume=generation_resume,
            discovery_snapshot=snapshot,
            reuse_discovery=reuse,
            unbounded_cycle=True,
            sweep_id=sweep_id,
            enabled_venues=universe_venues,
            reason="universe_sweep",
        )

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
        the frontend's 60s PAPER_COLLECTION_TIMEOUT_MS rather than competing
        with the 150s Full Sweep generation.
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
                message = f"scan_cycle_timeout after {timeout:g}s"
                raise ScanCycleTimeout(message) from exc
            finally:
                with self._state_lock:
                    self._cycle_enabled_venues = None
                    self._sync_venue_status_unlocked()

    def record_explicit_report(self, report: CollectionReport) -> None:
        """Upsert current-state from a manual collect without moving scheduler dues."""

        self._last_report = report
        self._fixture_state.upsert_from_report(report, scan_lane=ScanLane.UNIVERSE)
        duration_ms = max(
            0, int((report.completed_at - report.started_at).total_seconds() * 1000)
        )
        inventory = self._fixture_state.inventory(report.completed_at)
        _hot_count, universe_count = self._fixture_state.membership_counts(report.completed_at)
        with self._state_lock:
            self.status = self.status.model_copy(
                update={
                    "cycle_in_progress": False,
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
                        self.status.hot, self.status.universe, universe_count
                    ),
                    "config_warnings": report.config_warnings,
                    "venue_health": report.venue_health,
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
        self._fixture_state.upsert_from_report(report, scan_lane=lane)
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
            self._sync_venue_status_unlocked()

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
        duration_s = max(0.0, (report.completed_at - report.started_at).total_seconds())
        newly_evaluated = [
            item.canonical_event_id
            for item in report.discovered_fixtures
            if item.market_evaluation_state == "evaluated"
        ]
        self._universe_evaluated_ids.update(newly_evaluated)
        if newly_evaluated:
            self._universe_cursor = newly_evaluated[-1]
        elif report.resume_cursor:
            self._universe_cursor = report.resume_cursor
        self._universe_provider_failures = 0
        self._universe_retry_at = None
        self._universe_last_report_snapshot = collection_report_snapshot(report)
        if report.sweep_id:
            self._universe_sweep_id = report.sweep_id
        completeness = (report.scan_diagnostics or {}).get("completeness")
        discovered = max(
            self._universe_discovered_total,
            int((report.scan_diagnostics or {}).get("clusters_before_resume") or 0),
            len(report.discovered_fixtures) + leftover_n,
        )
        self._universe_discovered_total = discovered
        budget = self._charge_successful_universe_work(
            duration_s,
            report.completed_at,
            leftover_n=leftover_n,
            completeness=completeness,
        )
        self._persist_universe_checkpoint_unlocked()
        work_used = self._status_universe_work_used()
        evaluated_count = self._status_universe_evaluated_count()
        resume_cursor = self._status_universe_cursor()
        closed = self._universe_generation_started_at is None
        worker_state = WORKER_COMPLETE if closed else (WORKER_DEGRADED if degraded else WORKER_IDLE)
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
                        "evaluated_count": evaluated_count,
                        "not_evaluated_count": leftover_n,
                        "discovered_total": self._universe_discovered_total,
                        "remaining": max(0, self._universe_discovered_total - evaluated_count),
                        "matched_fixtures": self._universe_matched_fixtures,
                        "equivalent_markets": self._universe_equivalent_markets,
                        "near_count": self._universe_near_count,
                        "positive_count": self._universe_positive_count,
                        "qualifying_count": self._universe_qualifying_count,
                        "hot_promotions": self._universe_hot_promotions,
                        "last_successful_fixture": self._universe_last_successful,
                        "fixture_count": universe_count,
                        "degraded": degraded,
                        "last_error": None,
                        "last_persist_error": None,
                        "persist_ok": None,
                        "last_diagnostics": _lane_diagnostics(report),
                        "resume_cursor": resume_cursor,
                        "next_due_at": self._next_universe_due,
                        "venue_health": _frozen_venue_health(report.venue_health),
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
        self, snapshot: dict[str, list[dict[str, Any]]]
    ) -> None:
        with self._state_lock:
            if self._universe_generation_started_at is None:
                self._ensure_universe_generation(self.now())
            self._universe_discovery_snapshot = {
                key: list(value) for key, value in snapshot.items()
            }
            discovered = 0
            for rows in snapshot.values():
                discovered += len(rows or [])
            if discovered > self._universe_discovered_total:
                self._universe_discovered_total = discovered
            self._persist_universe_checkpoint_unlocked()
            self.status = self.status.model_copy(
                update={
                    "universe": self.status.universe.model_copy(
                        update={
                            "sweep_id": self._universe_sweep_id,
                            "discovered_total": self._universe_discovered_total,
                            "worker_state": WORKER_RUNNING
                            if self._universe_in_progress
                            else self.status.universe.worker_state,
                        }
                    )
                }
            )

    def record_universe_fixture_progress(
        self,
        cluster: Any,
        fixture: Any,
        decisions: list[Any],
        inventory: list[Any],
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
        before_hot, _before_universe = self._fixture_state.membership_counts(scanned)
        self._fixture_state.upsert_evaluated_fixture(
            fixture,
            markets=inventory,
            decisions=decisions,
            aliases=aliases,
            source_events=source_events,
            scan_lane=ScanLane.UNIVERSE,
            now=scanned,
        )
        after_hot, _after_universe = self._fixture_state.membership_counts(scanned)
        promoted_now = after_hot > before_hot
        inventory_now = self._fixture_state.inventory(scanned)
        with self._state_lock:
            if self._universe_generation_started_at is None:
                self._ensure_universe_generation(scanned)
            state = str(getattr(fixture, "market_evaluation_state", "") or "")
            if state == "evaluated":
                self._universe_evaluated_ids.add(canonical_id)
                self._universe_cursor = canonical_id
                self._universe_last_successful = canonical_id
            elif state == "market_fetch_unavailable":
                self._universe_failed_ids[canonical_id] = str(
                    getattr(fixture, "market_evaluation_reason", "") or "provider_failure"
                )
            else:
                self._universe_skipped_ids[canonical_id] = state or "skipped"
            self._universe_current_fixture = canonical_id
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
            if self._universe_discovered_total < len(self._universe_evaluated_ids):
                self._universe_discovered_total = len(self._universe_evaluated_ids)
            self._persist_universe_checkpoint_unlocked()
            evaluated_count = len(self._universe_evaluated_ids)
            self.status = self.status.model_copy(
                update={
                    "discovered_fixtures": inventory_now,
                    "universe": self.status.universe.model_copy(
                        update={
                            "worker_state": WORKER_RUNNING,
                            "sweep_id": self._universe_sweep_id,
                            "evaluated_count": evaluated_count,
                            "discovered_total": self._universe_discovered_total,
                            "remaining": max(
                                0, self._universe_discovered_total - evaluated_count
                            ),
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
                        }
                    ),
                }
            )

    def universe_collect_callbacks(self) -> tuple[Callable[..., None], Callable[..., None]]:
        return (
            self.record_universe_discovery_snapshot,
            self.record_universe_fixture_progress,
        )

    def _universe_plan_resume_state_unlocked(
        self,
    ) -> tuple[str | None, list[str], int, bool]:
        """Bind skip/cursor to the open generation. Closed gens plan empty resume."""

        if self._universe_generation_started_at is None:
            return (None, [], self._universe_generation_id + 1, False)
        progress_id = self._universe_progress_generation_id
        if progress_id is not None and progress_id != self._universe_generation_id:
            return (None, [], self._universe_generation_id + 1, False)
        plan_id = self._universe_generation_id if self._universe_generation_id > 0 else 1
        return (
            self._universe_cursor,
            sorted(self._universe_evaluated_ids),
            plan_id,
            True,
        )

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
            return len(self._universe_evaluated_ids)
        return self._universe_closed_evaluated_count

    def _clear_universe_generation_local_state(self) -> None:
        self._universe_closed_generation_id = self._universe_generation_id
        self._universe_closed_work_used = self._universe_work_used
        self._universe_closed_cursor = self._universe_cursor
        self._universe_closed_evaluated_count = len(self._universe_evaluated_ids)
        self._universe_evaluated_ids = set()
        self._universe_cursor = None
        self._universe_work_used = 0.0
        self._universe_progress_generation_id = None

    def _persist_universe_checkpoint_unlocked(self) -> None:
        store = self._universe_checkpoint_store
        if store is None:
            return
        if self._universe_generation_started_at is None:
            try:
                store.clear()
            except Exception:
                LOGGER.warning("failed to clear universe generation checkpoint", exc_info=True)
            return
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
            report=self._universe_last_report_snapshot,
            updated_at=self.now(),
            sweep_id=self._universe_sweep_id,
            discovery_snapshot=self._universe_discovery_snapshot,
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
        )
        try:
            store.save(
                checkpoint.model_dump(mode="json"),
                updated_at=checkpoint.updated_at.isoformat(),
            )
        except Exception:
            LOGGER.warning("failed to persist universe generation checkpoint", exc_info=True)

    def _clear_universe_checkpoint_unlocked(self) -> None:
        store = self._universe_checkpoint_store
        if store is None:
            return
        try:
            store.clear()
        except Exception:
            LOGGER.warning("failed to clear universe generation checkpoint", exc_info=True)

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
        report = collection_report_from_snapshot(checkpoint.report)
        if report is not None:
            self._last_report = report
            self._fixture_state.upsert_from_report(report, scan_lane=ScanLane.UNIVERSE)
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
            self._universe_last_report_snapshot = checkpoint.report
            self._universe_sweep_id = checkpoint.sweep_id or (
                f"sweep-{checkpoint.generation_id}-{checkpoint.generation_started_at.isoformat()}"
            )
            self._universe_discovery_snapshot = checkpoint.discovery_snapshot
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
            status_update: dict[str, Any] = {
                "discovered_fixtures": inventory,
                "universe": self.status.universe.model_copy(
                    update={
                        "generation_work_used_s": round(self._universe_work_used, 3),
                        "evaluated_count": len(self._universe_evaluated_ids),
                        "resume_cursor": self._universe_cursor,
                        "fixture_count": universe_count,
                        "next_due_at": self._next_universe_due,
                        "sweep_id": self._universe_sweep_id,
                        "discovered_total": self._universe_discovered_total,
                        "remaining": max(
                            0,
                            self._universe_discovered_total
                            - len(self._universe_evaluated_ids),
                        ),
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
            if report is not None:
                status_update["last_matched_event_pairs"] = report.matched_event_pairs
                status_update["last_matched_market_pairs"] = report.matched_market_pairs
                status_update["last_completed_at"] = report.completed_at
            self.status = self.status.model_copy(update=status_update)

    def _ensure_universe_generation(self, started: datetime) -> None:
        if self._universe_generation_started_at is not None:
            return
        self._universe_generation_id += 1
        self._universe_generation_started_at = started
        self._universe_work_used = 0.0
        self._universe_evaluated_ids = set()
        self._universe_cursor = None
        self._universe_progress_generation_id = self._universe_generation_id
        self._universe_sweep_id = f"sweep-{self._universe_generation_id}-{started.isoformat()}"
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
        self._persist_universe_checkpoint_unlocked()

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
        if leftover_n == 0 and completeness != UNIVERSE_COMPLETENESS_STALE_GENERATION_STATE:
            self._close_universe_generation(finished)
        return budget

    def _pause_universe_generation(self, finished: datetime) -> None:
        if self._universe_generation_started_at is None:
            return
        settings = get_settings()
        interval = timedelta(seconds=settings.paper_live_refresh_universe_interval_seconds)
        self._next_universe_due = finished + interval
        self._universe_budget_paused = True

    def _refresh_paused_universe_window_unlocked(self) -> None:
        if not self._universe_budget_paused:
            return
        self._universe_work_used = 0.0
        self._universe_budget_paused = False
        self._persist_universe_checkpoint_unlocked()

    def _close_universe_generation(self, finished: datetime) -> None:
        if self._universe_generation_started_at is None:
            return
        settings = get_settings()
        started = self._universe_generation_started_at
        next_due = started + timedelta(
            seconds=settings.paper_live_refresh_universe_interval_seconds
        )
        next_due = max(finished, next_due)
        self._next_universe_due = next_due
        self._clear_universe_generation_local_state()
        self._universe_generation_started_at = None
        self._universe_budget_paused = False
        self._universe_retry_at = None
        self._universe_provider_failures = 0
        self._universe_last_report_snapshot = None
        self._persist_universe_checkpoint_unlocked()

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
            self.status = self.status.model_copy(
                update={
                    **top,
                    "universe": self.status.universe.model_copy(
                        update={
                            "cycle_in_progress": True,
                            "worker_state": WORKER_RUNNING,
                            "sweep_id": self._universe_sweep_id,
                            "last_started_at": started,
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
                        }
                    ),
                }
            )
            self._sync_venue_status_unlocked()

    def _mark_manual_hot_started(self, started: datetime) -> None:
        """Expose manual HOT activity without claiming a scheduled due slot."""

        with self._state_lock:
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
                self._universe_provider_failures += 1
                delay = universe_provider_backoff_seconds(self._universe_provider_failures)
                self._universe_retry_at = finished + timedelta(seconds=delay)
                budget = float(get_settings().paper_scan_universe_generation_budget_seconds)
                work_used = self._status_universe_work_used()
                evaluated_count = self._status_universe_evaluated_count()
                self._persist_universe_checkpoint_unlocked()
                update["universe"] = self.status.universe.model_copy(
                    update={
                        "cycle_in_progress": False,
                        "worker_state": WORKER_WAITING,
                        "last_error": message,
                        "last_completed_at": finished,
                        "last_duration_ms": duration,
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
            self._cycle_enabled_venues = None
            self._sync_venue_status_unlocked()

    def last_report(self) -> CollectionReport | None:
        return self._last_report

    def fixture_current_state(self) -> FixtureCurrentStateStore:
        return self._fixture_state

    def public_status(self) -> LiveRefreshStatus:
        """Current Discovery/Tracked inventory as of now, after lifecycle eviction."""

        now = self.now()
        horizon = self.radar_horizon_kwargs()
        classify = {
            "hot_horizon": horizon["hot_horizon"],
            "post_kickoff_unknown_horizon": horizon["post_kickoff_unknown_horizon"],
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
        with self._state_lock:
            self.status = self.status.model_copy(
                update={
                    "discovered_fixtures": inventory,
                    "hot": self.status.hot.model_copy(
                        update={
                            "fixture_count": unique or hot_count,
                            "lifecycle_hot_count": lifecycle,
                            "promoted_hot_count": promoted,
                        }
                    ),
                    "universe": self.status.universe.model_copy(
                        update={"fixture_count": universe_count}
                    ),
                    "operator_summary": _combined_operator_summary(
                        self.status.hot, self.status.universe, universe_count
                    ),
                    "provider_access": get_shared_provider_access().snapshot().as_dict(),
                    "cycle_in_progress": self._hot_in_progress or self._universe_in_progress,
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
        self._task = self._hot_task

    async def stop_server_loop(self) -> None:
        self._stop.set()
        for task in (self._hot_task, self._universe_task, self._task):
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._hot_task = None
        self._universe_task = None
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
            if plan.lane == ScanLane.HOT.value:
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

    async def _universe_loop(self, tick) -> None:
        while not self._stop.is_set():
            plan = self.plan_universe_tick()
            if plan.lane == ScanLane.UNIVERSE.value:
                try:
                    await self._invoke_tick(tick, plan)
                except asyncio.CancelledError:
                    raise
                except (MatchbookAuthError, MatchbookDiscoveryError, ScanCycleTimeout, Exception):
                    pass
            delay = min(self._seconds_until_universe(), 5.0)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.05, delay))
            except TimeoutError:
                continue

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
    `timeout` so the coordinator envelope stays ≤30s for HOT.
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
        raise TimeoutError
    finally:
        if not task.done():
            task.cancel()


def _lane_diagnostics(report: CollectionReport) -> dict[str, Any]:
    payload = dict(report.scan_diagnostics or {})
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
    worker_state: str = WORKER_IDLE,
    venue_health: dict[str, str] | None = None,
    active_venues: list[VenueName] | None = None,
) -> str:
    elapsed_s = round(max(work_used, duration_ms / 1000), 1)
    venue_clause = last_scan_venue_clause(venue_health, configured=active_venues)
    discovered = discovered_total or fixture_count
    remaining = max(0, discovered - evaluated_n)
    state = worker_state or WORKER_IDLE
    return (
        f"Full sweep · {state} · elapsed {elapsed_s}s · "
        f"{evaluated_n}/{discovered} evaluated · {remaining} remaining · "
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


def _merge_top_level_venue_health(
    hot: dict[str, str] | None,
    universe: dict[str, str] | None,
) -> dict[str, str]:
    merged: dict[str, str] = {}
    keys = set(hot or {}) | set(universe or {})
    for venue in keys:
        left = (hot or {}).get(venue)
        right = (universe or {}).get(venue)
        if left == "ok" and right == "ok":
            merged[venue] = "ok"
        elif left == "ok" and right in {
            "timeout",
            HEALTH_DISCOVERY_TIMEOUT,
            HEALTH_MARKET_TIMEOUT,
            "degraded",
        }:
            merged[venue] = "degraded"
        elif right == "ok" and left in {
            "timeout",
            HEALTH_DISCOVERY_TIMEOUT,
            HEALTH_MARKET_TIMEOUT,
            "degraded",
        }:
            merged[venue] = "degraded"
        elif left == "ok" or right == "ok":
            merged[venue] = "ok"
        else:
            merged[venue] = right or left or "unknown"
    return merged
