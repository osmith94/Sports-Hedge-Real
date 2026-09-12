from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.application.collector import CollectionReport, DiscoveredFixture
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.venues.matchbook import MatchbookAuthError


class LiveRefreshStatus(BaseModel):
    discovery_source: VenueName = VenueName.MATCHBOOK
    matching_venue: VenueName = VenueName.POLYMARKET
    server_loop_enabled: bool
    interval_seconds: int = Field(ge=15, le=300)
    cycle_in_progress: bool = False
    last_started_at: datetime | None = None
    last_completed_at: datetime | None = None
    last_error: str | None = None
    last_matched_event_pairs: int | None = None
    last_matched_market_pairs: int | None = None
    last_paper_decisions: int | None = None
    last_issue_count: int | None = None
    live_scores: str = "unavailable_unless_matchbook_payload_includes_scores"
    discovered_fixtures: list[DiscoveredFixture] = Field(default_factory=list)


class LiveRefreshCoordinator:
    """Repeated read-only Matchbook→Polymarket collection without stacking cycles."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._last_request: dict[str, Any] = {}
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

    async def run_cycle(self, runner) -> CollectionReport:
        async with self._lock:
            self.status = self.status.model_copy(
                update={
                    "cycle_in_progress": True,
                    "last_started_at": datetime.now(UTC),
                    "last_error": None,
                }
            )
            try:
                report = await runner()
                self.record_report(report)
                return report
            except Exception as exc:
                self.status = self.status.model_copy(
                    update={
                        "cycle_in_progress": False,
                        "last_error": str(exc),
                        "last_completed_at": datetime.now(UTC),
                    }
                )
                raise

    def record_report(self, report: CollectionReport) -> None:
        self.status = self.status.model_copy(
            update={
                "cycle_in_progress": False,
                "last_completed_at": report.completed_at,
                "last_matched_event_pairs": report.matched_event_pairs,
                "last_matched_market_pairs": report.matched_market_pairs,
                "last_paper_decisions": len(report.paper_decisions),
                "last_issue_count": len(report.issues),
                "discovered_fixtures": report.discovered_fixtures,
                "last_error": None,
            }
        )

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
            try:
                await tick()
            except asyncio.CancelledError:
                raise
            except (MatchbookAuthError, Exception):
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
