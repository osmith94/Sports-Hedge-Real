from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.application.collector import (
    CollectionReport,
    DiscoveredFixture,
    FixtureDetailReadModel,
)
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.venues.matchbook import MatchbookAuthError, MatchbookDiscoveryError


class ScanCycleTimeout(TimeoutError):
    """Raised when a live-refresh cycle exceeds its bounded deadline."""


# Collector soft-stops at paper_scan_cycle_timeout_seconds minus a finalisation
# reserve. Coordinator waits this extra grace for persist/aclose after the
# collector has already returned a (possibly partial) CollectionReport.
SCAN_CYCLE_RETURN_GRACE_SECONDS = 5.0


class LiveRefreshStatus(BaseModel):
    discovery_source: VenueName = VenueName.MATCHBOOK
    discovery_mode: str = "venue_union"
    matching_venue: VenueName = VenueName.POLYMARKET
    matching_venues: list[VenueName] = Field(
        default_factory=lambda: [VenueName.POLYMARKET, VenueName.KALSHI]
    )
    server_loop_enabled: bool
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


class LiveRefreshCoordinator:
    """Repeated read-only venue-union collection without stacking cycles."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_request: dict[str, Any] = {}
        self._last_report: CollectionReport | None = None
        self.status = LiveRefreshStatus(
            server_loop_enabled=False,
            interval_seconds=30,
        )

    def configure_from_settings(self, settings: Settings | None = None) -> None:
        resolved = settings or get_settings()
        self.status = self.status.model_copy(
            update={
                "server_loop_enabled": resolved.paper_live_refresh_enabled,
                "interval_seconds": resolved.paper_live_refresh_interval_seconds,
            }
        )

    def remember_request(self, payload: dict[str, Any]) -> None:
        self._last_request = dict(payload)

    def last_request(self) -> dict[str, Any]:
        return dict(self._last_request)

    def reset(self) -> None:
        self._last_request = {}
        self._last_report = None
        self.status = LiveRefreshStatus(
            server_loop_enabled=False,
            interval_seconds=30,
        )
        self.configure_from_settings()

    async def run_cycle(self, runner, *, timeout_seconds: float | None = None) -> CollectionReport:
        settings = get_settings()
        timeout = float(
            settings.paper_scan_cycle_timeout_seconds + SCAN_CYCLE_RETURN_GRACE_SECONDS
            if timeout_seconds is None
            else timeout_seconds
        )
        async with self._lock:
            started = datetime.now(UTC)
            self.status = self.status.model_copy(
                update={
                    "cycle_in_progress": True,
                    "last_started_at": started,
                    "last_error": None,
                }
            )
            try:
                report = await asyncio.wait_for(runner(), timeout=timeout)
                self.record_report(report)
                return report
            except TimeoutError as exc:
                finished = datetime.now(UTC)
                message = f"scan_cycle_timeout after {timeout:g}s"
                self.status = self.status.model_copy(
                    update={
                        "cycle_in_progress": False,
                        "last_error": message,
                        "last_completed_at": finished,
                        "last_duration_ms": max(
                            0, int((finished - started).total_seconds() * 1000)
                        ),
                    }
                )
                raise ScanCycleTimeout(message) from exc
            except Exception as exc:
                finished = datetime.now(UTC)
                self.status = self.status.model_copy(
                    update={
                        "cycle_in_progress": False,
                        "last_error": str(exc),
                        "last_completed_at": finished,
                        "last_duration_ms": max(
                            0, int((finished - started).total_seconds() * 1000)
                        ),
                    }
                )
                raise
            finally:
                if self.status.cycle_in_progress:
                    finished = datetime.now(UTC)
                    self.status = self.status.model_copy(
                        update={
                            "cycle_in_progress": False,
                            "last_error": self.status.last_error or "scan_cycle_abandoned",
                            "last_completed_at": self.status.last_completed_at or finished,
                            "last_duration_ms": max(
                                0, int((finished - started).total_seconds() * 1000)
                            ),
                        }
                    )

    def record_report(self, report: CollectionReport) -> None:
        self._last_report = report
        self.status = self.status.model_copy(
            update={
                "cycle_in_progress": False,
                "last_completed_at": report.completed_at,
                "discovery_mode": report.discovery_mode,
                "matching_venues": report.matching_venues,
                "last_matched_event_pairs": report.matched_event_pairs,
                "last_matched_market_pairs": report.matched_market_pairs,
                "last_paper_decisions": len(report.paper_decisions),
                "last_issue_count": len(report.issues),
                "skipped_out_of_scope": report.skipped_out_of_scope,
                "operator_summary": report.operator_summary,
                "config_warnings": report.config_warnings,
                "venue_health": report.venue_health,
                "last_duration_ms": max(
                    0, int((report.completed_at - report.started_at).total_seconds() * 1000)
                ),
                "discovered_fixtures": report.discovered_fixtures,
                "last_error": None,
            }
        )

    def last_report(self) -> CollectionReport | None:
        return self._last_report

    def fixture_detail(self, canonical_event_id: str) -> FixtureDetailReadModel | None:
        report = self._last_report
        if report is None:
            return None
        wanted = canonical_event_id.strip()
        for fixture in report.discovered_fixtures:
            if fixture.canonical_event_id == wanted or fixture.source_event_id == wanted:
                return FixtureDetailReadModel(
                    fixture=fixture,
                    markets=list(report.fixture_markets.get(fixture.canonical_event_id, [])),
                )
        return None

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
                break
            try:
                await tick()
            except asyncio.CancelledError:
                raise
            except (MatchbookAuthError, MatchbookDiscoveryError, ScanCycleTimeout, Exception):
                pass
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.status.interval_seconds,
                )
            except TimeoutError:
                continue


_COORDINATOR = LiveRefreshCoordinator()


def get_live_refresh_coordinator() -> LiveRefreshCoordinator:
    return _COORDINATOR
