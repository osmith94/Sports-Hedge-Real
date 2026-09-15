from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any, Literal

from pydantic import BaseModel, Field

from sports_hedge.application.collector import (
    DIAGNOSTIC_PROVIDERS,
    DIAGNOSTIC_STAGES,
    CollectionReport,
    DiscoveredFixture,
    FixtureDetailReadModel,
)
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.lane_venues import (
    MALFORMED_VENUE_SETTINGS_WARNING,
    LaneVenueParticipation,
    LaneVenueSet,
    default_operator_venues,
    participation_from_lists,
)
from sports_hedge.application.quote_freshness import require_aware_instant
from sports_hedge.application.scan_lanes import (
    ScanLane,
    universe_chunk_wall_seconds,
)
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.persistence.lane_venue_settings import (
    SqliteLaneVenueSettingsStore,
    get_lane_venue_settings_store,
    resolve_lane_venue_participation,
)
from sports_hedge.venues.matchbook import MatchbookAuthError, MatchbookDiscoveryError


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
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_duration_ms: int | None = Field(default=None, ge=0)
    next_due_at: datetime | None = None
    fixture_count: int = 0
    evaluated_count: int = 0
    not_evaluated_count: int = 0
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


class DualCadencePlan(BaseModel):
    lane: Literal["hot", "universe", "idle"]
    collector_timeout_seconds: float | None = None
    coordinator_timeout_seconds: float | None = None
    identity_scope: list[str] | None = None
    resume_cursor: str | None = None
    skip_event_ids: list[str] = Field(default_factory=list)
    known_source_events: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    enabled_venues: list[VenueName] = Field(
        default_factory=lambda: list(default_operator_venues())
    )
    reason: str = ""


class LiveRefreshCoordinator:
    """One scheduler with HOT and UNIVERSE lanes. No stacked scanners."""

    def __init__(
        self,
        clock: Callable[[], datetime] | None = None,
        venue_settings_store: SqliteLaneVenueSettingsStore | None = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_request: dict[str, Any] = {}
        self._last_report: CollectionReport | None = None
        self._fixture_state = FixtureCurrentStateStore()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._hot_in_progress = False
        self._universe_in_progress = False
        self._next_hot_due: datetime | None = None
        self._next_universe_due: datetime | None = None
        self._hot_due_started: datetime | None = None
        self._universe_generation_id = 0
        self._universe_generation_started_at: datetime | None = None
        self._universe_work_used = 0.0
        self._universe_cursor: str | None = None
        self._universe_evaluated_ids: set[str] = set()
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
        self._pending_participation = resolve_lane_venue_participation(
            self._resolved_store(resolved),
            resolved,
        )
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
        self._sync_venue_status()
        self._ensure_due_times(self.now(), resolved)

    def _resolved_store(self, settings: Settings | None = None) -> SqliteLaneVenueSettingsStore:
        if self._venue_store is None:
            self._venue_store = get_lane_venue_settings_store()
        return self._venue_store

    def bind_venue_store(self, store: SqliteLaneVenueSettingsStore) -> None:
        self._venue_store = store
        self._pending_participation = resolve_lane_venue_participation(store)
        self._sync_venue_status()

    def apply_venue_participation(
        self,
        hot: list[VenueName] | tuple[VenueName, ...],
        universe: list[VenueName] | tuple[VenueName, ...],
    ) -> LaneVenueParticipation:
        store = self._resolved_store()
        self._pending_participation = store.save(hot, universe, source="operator")
        self._sync_venue_status()
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
        self._last_request = dict(payload)

    def last_request(self) -> dict[str, Any]:
        return dict(self._last_request)

    def reset(self) -> None:
        self._last_request = {}
        self._last_report = None
        self._fixture_state.clear()
        self._hot_in_progress = False
        self._universe_in_progress = False
        self._next_hot_due = None
        self._next_universe_due = None
        self._hot_due_started = None
        self._universe_generation_id = 0
        self._universe_generation_started_at = None
        self._universe_work_used = 0.0
        self._universe_cursor = None
        self._universe_evaluated_ids = set()
        self._cycle_hot_venues = None
        self._cycle_universe_venues = None
        self._cycle_enabled_venues = None
        self.status = LiveRefreshStatus(
            server_loop_enabled=False,
            interval_seconds=30,
        )
        self.configure_from_settings()

    def _ensure_due_times(self, now: datetime, settings: Settings | None = None) -> None:
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
                        "resume_cursor": self._universe_cursor,
                        "generation_work_used_s": round(self._universe_work_used, 3),
                    }
                ),
            }
        )

    def plan_tick(
        self,
        now: datetime | None = None,
        settings: Settings | None = None,
    ) -> DualCadencePlan:
        resolved = settings or get_settings()
        evaluated = require_aware_instant(now or self.now(), "now")
        self._ensure_due_times(evaluated, resolved)
        if self._hot_in_progress or self.status.cycle_in_progress:
            return DualCadencePlan(lane="idle", reason="cycle_in_progress")
        hot_due = self._next_hot_due is not None and evaluated >= self._next_hot_due
        hot_scope = self._fixture_state.hot_identity_scope(
            evaluated,
            hot_horizon=timedelta(minutes=resolved.paper_hot_pre_kickoff_horizon_minutes),
            post_kickoff_unknown_horizon=timedelta(
                hours=resolved.paper_hot_post_kickoff_unknown_horizon_hours
            ),
        )
        hot_venues = list(self.pending_venues_for(ScanLane.HOT))
        universe_venues = list(self.pending_venues_for(ScanLane.UNIVERSE))
        if hot_due and hot_scope:
            timeout = float(resolved.paper_scan_hot_cycle_timeout_seconds)
            return DualCadencePlan(
                lane="hot",
                collector_timeout_seconds=timeout,
                coordinator_timeout_seconds=timeout + SCAN_CYCLE_RETURN_GRACE_SECONDS,
                identity_scope=list(hot_scope),
                known_source_events=self._fixture_state.known_source_events(hot_scope),
                enabled_venues=hot_venues,
                reason="hot_due",
            )
        universe_due = self._universe_generation_open(evaluated, resolved)
        if universe_due:
            remaining = float(resolved.paper_scan_universe_generation_budget_seconds) - self._universe_work_used
            if remaining <= 0:
                return DualCadencePlan(lane="idle", reason="universe_budget_exhausted")
            hot_cadence = timedelta(seconds=resolved.paper_live_refresh_hot_interval_seconds)
            next_hot = self._next_hot_due or (evaluated + hot_cadence)
            if not hot_scope and next_hot <= evaluated:
                # Empty HOT must not starve generation-0 / empty-scope UNIVERSE.
                next_hot = evaluated + hot_cadence
            chunk_wall = universe_chunk_wall_seconds(
                now=evaluated,
                next_hot_due=next_hot,
                remaining_generation_budget=remaining,
                safety_margin_seconds=float(
                    resolved.paper_universe_hot_yield_safety_margin_seconds
                ),
            )
            if chunk_wall is None:
                return DualCadencePlan(lane="idle", reason="yield_to_hot")
            collector_timeout = chunk_wall - SCAN_CYCLE_RETURN_GRACE_SECONDS
            if collector_timeout <= 0:
                return DualCadencePlan(lane="idle", reason="chunk_below_grace")
            return DualCadencePlan(
                lane="universe",
                collector_timeout_seconds=collector_timeout,
                coordinator_timeout_seconds=chunk_wall,
                resume_cursor=self._universe_cursor,
                skip_event_ids=sorted(self._universe_evaluated_ids),
                enabled_venues=universe_venues,
                reason="universe_chunk",
            )
        if hot_due:
            timeout = float(resolved.paper_scan_hot_cycle_timeout_seconds)
            return DualCadencePlan(
                lane="hot",
                collector_timeout_seconds=timeout,
                coordinator_timeout_seconds=timeout + SCAN_CYCLE_RETURN_GRACE_SECONDS,
                identity_scope=list(hot_scope),
                known_source_events=self._fixture_state.known_source_events(hot_scope),
                enabled_venues=hot_venues,
                reason="hot_due",
            )
        return DualCadencePlan(lane="idle", reason="waiting")

    def seconds_until_next_work(
        self,
        now: datetime | None = None,
        settings: Settings | None = None,
    ) -> float:
        plan = self.plan_tick(now=now, settings=settings)
        if plan.lane != "idle":
            return 0.0
        evaluated = require_aware_instant(now or self.now(), "now")
        candidates: list[float] = []
        if self._next_hot_due is not None:
            candidates.append((self._next_hot_due - evaluated).total_seconds())
        if self._next_universe_due is not None:
            candidates.append((self._next_universe_due - evaluated).total_seconds())
        if not candidates:
            return float(self.status.interval_seconds)
        return max(0.05, min(candidates))

    def universe_due_immediately(self) -> bool:
        return self._next_universe_due is not None and self._universe_generation_id == 0

    def _universe_generation_open(self, now: datetime, settings: Settings) -> bool:
        if self._universe_generation_started_at is not None:
            budget = float(settings.paper_scan_universe_generation_budget_seconds)
            return self._universe_work_used < budget
        return self._next_universe_due is not None and now >= self._next_universe_due

    async def run_cycle(
        self,
        runner,
        *,
        timeout_seconds: float | None = None,
        scan_lane: ScanLane | str | None = None,
    ) -> CollectionReport:
        settings = get_settings()
        lane = _coerce_lane(scan_lane)
        timeout = float(
            settings.paper_scan_cycle_timeout_seconds + SCAN_CYCLE_RETURN_GRACE_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        )
        async with self._lock:
            started = self.now()
            self._mark_lane_started(lane, started)
            try:
                report = await _await_collection_runner(runner, timeout)
                self.record_report(report, scan_lane=lane)
                return report
            except TimeoutError as exc:
                finished = self.now()
                message = f"scan_cycle_timeout after {timeout:g}s"
                self._mark_lane_error(lane, started, finished, message)
                raise ScanCycleTimeout(message) from exc
            except Exception as exc:
                finished = self.now()
                self._mark_lane_error(lane, started, finished, str(exc))
                raise
            finally:
                if self.status.cycle_in_progress:
                    finished = self.now()
                    self._mark_lane_error(
                        lane,
                        started,
                        finished,
                        self.status.last_error or "scan_cycle_abandoned",
                    )

    def scheduled_collection_active(self) -> bool:
        return (
            self._lock.locked()
            or self.status.cycle_in_progress
            or self._hot_in_progress
            or self._universe_in_progress
        )

    async def run_explicit_collect(self, runner) -> CollectionReport:
        """Manual diagnostic collect. Does not own HOT/UNIVERSE generation progress."""

        if self.scheduled_collection_active():
            raise ExplicitCollectBusy("scheduled scan in progress")
        settings = get_settings()
        timeout = float(
            settings.paper_scan_cycle_timeout_seconds + SCAN_CYCLE_RETURN_GRACE_SECONDS
        )
        async with self._lock:
            if self._hot_in_progress or self._universe_in_progress or self.status.cycle_in_progress:
                raise ExplicitCollectBusy("scheduled scan in progress")
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
                self._cycle_enabled_venues = None
                self._sync_venue_status()

    def record_explicit_report(self, report: CollectionReport) -> None:
        """Upsert current-state from a manual collect without moving scheduler dues."""

        self._last_report = report
        self._fixture_state.upsert_from_report(report, scan_lane=ScanLane.UNIVERSE)
        duration_ms = max(
            0, int((report.completed_at - report.started_at).total_seconds() * 1000)
        )
        inventory = self._fixture_state.inventory(report.completed_at)
        _hot_count, universe_count = self._fixture_state.membership_counts(report.completed_at)
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
            value in {"timeout", "degraded", "unavailable"}
            for value in report.venue_health.values()
        )
        if lane is ScanLane.HOT:
            self._advance_hot_due(report.completed_at)
            hot_count, _universe_count = self._fixture_state.membership_counts(
                report.completed_at,
                hot_horizon=timedelta(minutes=get_settings().paper_hot_pre_kickoff_horizon_minutes),
                post_kickoff_unknown_horizon=timedelta(
                    hours=get_settings().paper_hot_post_kickoff_unknown_horizon_hours
                ),
            )
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
                            "degraded": degraded,
                            "last_error": None,
                            "last_persist_error": None,
                            "persist_ok": None,
                            "last_diagnostics": _lane_diagnostics(report),
                            "next_due_at": self._next_hot_due,
                            "operator_summary": _hot_operator_summary(
                                report.completed_at,
                                duration_ms,
                                self._next_hot_due,
                                hot_count,
                                leftover_n,
                            ),
                        }
                    )
                }
            )
        else:
            self._record_universe_progress(report, duration_ms, evaluated_n, leftover_n, degraded)
        inventory = self._fixture_state.inventory(report.completed_at)
        _hot_count, universe_count = self._fixture_state.membership_counts(report.completed_at)
        compat_completed = self.status.hot.last_completed_at or self.status.universe.last_completed_at
        compat_duration = self.status.hot.last_duration_ms
        if compat_duration is None:
            compat_duration = self.status.universe.last_duration_ms
        self.status = self.status.model_copy(
            update={
                "cycle_in_progress": False,
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
                "venue_health": report.venue_health,
                "discovered_fixtures": inventory,
                "last_error": None,
                "interval_seconds": self.status.hot.cadence_seconds,
            }
        )
        self._hot_in_progress = False
        self._universe_in_progress = False
        self._cycle_enabled_venues = None
        self._sync_venue_status()

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
    ) -> None:
        if self._universe_generation_started_at is None:
            self._ensure_universe_generation(report.started_at)
        duration_s = max(0.0, (report.completed_at - report.started_at).total_seconds())
        budget = self._charge_universe_work(duration_s, report.completed_at)
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
        if leftover_n == 0 and self._universe_generation_started_at is not None:
            self._close_universe_generation(report.completed_at)
        _hot_count, universe_count = self._fixture_state.membership_counts(report.completed_at)
        self.status = self.status.model_copy(
            update={
                "universe": self.status.universe.model_copy(
                    update={
                        "cycle_in_progress": False,
                        "last_completed_at": report.completed_at,
                        "last_duration_ms": duration_ms,
                        "chunk_last_duration_ms": duration_ms,
                        "generation_work_used_s": round(self._universe_work_used, 3),
                        "evaluated_count": len(self._universe_evaluated_ids),
                        "not_evaluated_count": leftover_n,
                        "fixture_count": universe_count,
                        "degraded": degraded,
                        "last_error": None,
                        "last_persist_error": None,
                        "persist_ok": None,
                        "last_diagnostics": _lane_diagnostics(report),
                        "resume_cursor": self._universe_cursor,
                        "next_due_at": self._next_universe_due,
                        "operator_summary": _universe_operator_summary(
                            duration_ms,
                            self._universe_work_used,
                            budget,
                            self._next_hot_due,
                            universe_count,
                            len(self._universe_evaluated_ids),
                            leftover_n,
                        ),
                    }
                )
            }
        )

    def _ensure_universe_generation(self, started: datetime) -> None:
        if self._universe_generation_started_at is not None:
            return
        self._universe_generation_id += 1
        self._universe_generation_started_at = started
        self._universe_work_used = 0.0
        self._universe_evaluated_ids = set()
        self._universe_cursor = None

    def _charge_universe_work(self, duration_s: float, finished: datetime) -> float:
        settings = get_settings()
        budget = float(settings.paper_scan_universe_generation_budget_seconds)
        self._universe_work_used += max(0.0, duration_s)
        if self._universe_work_used >= budget:
            self._close_universe_generation(finished)
        return budget

    def _close_universe_generation(self, finished: datetime) -> None:
        settings = get_settings()
        started = self._universe_generation_started_at or finished
        next_due = started + timedelta(
            seconds=settings.paper_live_refresh_universe_interval_seconds
        )
        next_due = max(finished, next_due)
        self._next_universe_due = next_due
        self._universe_generation_started_at = None

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
        self._hot_in_progress = lane is ScanLane.HOT
        self._universe_in_progress = lane is not ScanLane.HOT
        if lane is ScanLane.HOT:
            self._cycle_hot_venues = self._pending_participation.venues_for(ScanLane.HOT)
            self._cycle_enabled_venues = self._cycle_hot_venues
        else:
            self._cycle_universe_venues = self._pending_participation.venues_for(
                ScanLane.UNIVERSE
            )
            self._cycle_enabled_venues = self._cycle_universe_venues
        top = {
            "cycle_in_progress": True,
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
                            "last_started_at": started,
                            "last_error": None,
                            "last_persist_error": None,
                            "persist_ok": None,
                        }
                    ),
                }
            )
            self._sync_venue_status()
            return
        self._ensure_universe_generation(started)
        self.status = self.status.model_copy(
            update={
                **top,
                "universe": self.status.universe.model_copy(
                    update={
                        "cycle_in_progress": True,
                        "last_started_at": started,
                        "last_error": None,
                        "last_persist_error": None,
                        "persist_ok": None,
                    }
                ),
            }
        )
        self._sync_venue_status()

    def _mark_lane_error(
        self,
        lane: ScanLane,
        started: datetime,
        finished: datetime,
        message: str,
    ) -> None:
        duration = max(0, int((finished - started).total_seconds() * 1000))
        update: dict[str, Any] = {
            "cycle_in_progress": False,
            "last_error": message,
            "last_completed_at": self.status.last_completed_at or finished,
            "last_duration_ms": duration,
        }
        if lane is ScanLane.HOT:
            self._advance_hot_due(finished)
            update["hot"] = self.status.hot.model_copy(
                update={
                    "cycle_in_progress": False,
                    "last_error": message,
                    "last_completed_at": finished,
                    "last_duration_ms": duration,
                    "next_due_at": self._next_hot_due,
                }
            )
            update["last_completed_at"] = finished
        else:
            self._ensure_universe_generation(started)
            duration_s = max(0.0, (finished - started).total_seconds())
            budget = self._charge_universe_work(duration_s, finished)
            _hot_count, universe_count = self._fixture_state.membership_counts(finished)
            update["universe"] = self.status.universe.model_copy(
                update={
                    "cycle_in_progress": False,
                    "last_error": message,
                    "last_completed_at": finished,
                    "last_duration_ms": duration,
                    "chunk_last_duration_ms": duration,
                    "generation_work_used_s": round(self._universe_work_used, 3),
                    "fixture_count": universe_count,
                    "degraded": True,
                    "resume_cursor": self._universe_cursor,
                    "next_due_at": self._next_universe_due,
                    "operator_summary": _universe_operator_summary(
                        duration,
                        self._universe_work_used,
                        budget,
                        self._next_hot_due,
                        universe_count,
                        len(self._universe_evaluated_ids),
                        self.status.universe.not_evaluated_count,
                    ),
                }
            )
            if self.status.last_completed_at is None:
                update["last_completed_at"] = finished
        self.status = self.status.model_copy(update=update)
        self._hot_in_progress = False
        self._universe_in_progress = False
        self._cycle_enabled_venues = None
        self._sync_venue_status()

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
        self.status = self.status.model_copy(
            update={
                "discovered_fixtures": inventory,
                "hot": self.status.hot.model_copy(update={"fixture_count": hot_count}),
                "universe": self.status.universe.model_copy(
                    update={"fixture_count": universe_count}
                ),
                "operator_summary": _combined_operator_summary(
                    self.status.hot, self.status.universe, universe_count
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
            "hot_ttl_seconds": resolved.paper_hot_current_state_ttl_seconds,
            "universe_ttl_seconds": resolved.paper_universe_current_state_ttl_seconds,
            "hot_interval_seconds": resolved.paper_live_refresh_hot_interval_seconds,
            "universe_interval_seconds": resolved.paper_live_refresh_universe_interval_seconds,
        }

    async def start_server_loop(self, tick) -> None:
        self.configure_from_settings()
        if not self.status.server_loop_enabled:
            return
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop(tick))

    async def stop_server_loop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self, tick) -> None:
        while not self._stop.is_set():
            if self.status.cycle_in_progress:
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=1.0)
                except TimeoutError:
                    continue
                continue
            try:
                await tick()
            except asyncio.CancelledError:
                raise
            except (MatchbookAuthError, MatchbookDiscoveryError, ScanCycleTimeout, Exception):
                pass
            delay = min(self.seconds_until_next_work(), float(self.status.interval_seconds))
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=max(0.05, delay))
            except TimeoutError:
                continue


_COORDINATOR = LiveRefreshCoordinator()


def get_live_refresh_coordinator() -> LiveRefreshCoordinator:
    return _COORDINATOR


def _collection_task_result(task: asyncio.Task[Any]) -> CollectionReport:
    try:
        return task.result()
    except asyncio.CancelledError as exc:
        raise TimeoutError from exc


async def _await_collection_runner(runner, timeout: float) -> CollectionReport:
    """Wait for collection/aclose on a child task; persist stays outside this wait.

    `asyncio.wait_for(coro)` is not used here: a timeout context would cancel
    the scheduler tick, and a swallowed leftover is not TimeoutError. Persist
    after a timely leftover *inside* that wait is the envelope overrun. This
    helper cancels only the child and harvests leftover + aclose inside
    `timeout` so the coordinator envelope stays ≤30s for HOT.
    """

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
    payload.setdefault("timeout_count", 0)
    payload["partial"] = bool(
        leftover_n
        or payload.get("soft_deadline_reached")
        or payload.get("cancelled")
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


def _ago_label(moment: datetime | None, now: datetime | None = None) -> str:
    if moment is None:
        return "never"
    current = now or datetime.now(UTC)
    delta = int((current - moment).total_seconds())
    delta = max(delta, 0)
    return f"{delta}s ago"


def _hot_operator_summary(
    completed_at: datetime,
    duration_ms: int,
    next_due: datetime | None,
    fixture_count: int,
    leftover_n: int,
) -> str:
    duration_s = round(duration_ms / 1000, 1)
    next_s = "—"
    if next_due is not None:
        next_s = f"{max(0, int((next_due - completed_at).total_seconds()))}s"
    summary = (
        f"Fast scan · {_ago_label(completed_at, completed_at)} · {duration_s}s · "
        f"next {next_s} · {fixture_count} hot"
    )
    if leftover_n:
        summary += f" · partial ({leftover_n} not evaluated)"
    return summary


def _universe_operator_summary(
    duration_ms: int,
    work_used: float,
    budget: float,
    next_hot: datetime | None,
    fixture_count: int,
    evaluated_n: int,
    leftover_n: int,
) -> str:
    chunk_s = round(duration_ms / 1000, 1)
    next_hot_s = "—"
    if next_hot is not None:
        next_hot_s = f"{max(0, int((next_hot - datetime.now(UTC)).total_seconds()))}s"
    return (
        f"Full sweep · chunk {chunk_s}s · gen {int(work_used)}/{int(budget)}s · "
        f"next HOT in {next_hot_s} · {fixture_count} universe · "
        f"{evaluated_n} evaluated / {leftover_n} not evaluated"
    )


def _combined_operator_summary(
    hot: LaneRefreshStatus,
    universe: LaneRefreshStatus,
    universe_count: int,
) -> str:
    fast = hot.operator_summary or (
        f"Fast scan · never · — · next — · {hot.fixture_count} hot"
    )
    full = universe.operator_summary or (
        f"Full sweep · chunk — · gen 0/{int(universe.generation_budget_seconds or 150)}s · "
        f"{universe_count} universe"
    )
    return f"{fast} · {full}"
