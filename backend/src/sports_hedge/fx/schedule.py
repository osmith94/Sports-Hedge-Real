from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

from sports_hedge.accounting.paper_journal import PaperJournal
from sports_hedge.accounting.revaluation import DailyFxRevaluationService
from sports_hedge.fx.service import FxRateService, ecb_publication_window_open, london_calendar_date
from sports_hedge.fx.sources import BoeXudlussSource, EcbEurofxrefSource

LONDON = ZoneInfo("Europe/London")
Clock = Callable[[], datetime]


class AccountingSchedule:
    """Separate source ingestion from the 00:00 Europe/London valuation cut-off."""

    def __init__(
        self,
        fx: FxRateService,
        *,
        journal: PaperJournal | None = None,
        clock: Clock | None = None,
        http_factory: Callable[[], httpx.Client] | None = None,
        enabled: bool = False,
    ) -> None:
        self.fx = fx
        self.revaluation = DailyFxRevaluationService(fx, journal or PaperJournal())
        self.clock = clock or (lambda: datetime.now(UTC))
        self.http_factory = http_factory or (lambda: httpx.Client())
        self.enabled = enabled
        self.last_ingest_source_date: date | None = None
        self.last_revaluation_date: date | None = None
        self.last_error: str | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def run_due_jobs(self, *, now: datetime | None = None) -> dict[str, str]:
        instant = now or self.clock()
        actions: dict[str, str] = {}
        london_date = london_calendar_date(instant)
        if self.last_revaluation_date != london_date:
            try:
                self.revaluation.run(valuation_date=london_date, as_of=instant)
                self.last_revaluation_date = london_date
                actions["revaluation"] = london_date.isoformat()
            except Exception as exc:  # noqa: BLE001 — scheduler must keep ticking
                self.last_error = str(exc)
                actions["revaluation_error"] = str(exc)
        if ecb_publication_window_open(instant):
            try:
                ingested = self.ingest_published_closes(retrieved_at=instant)
                if ingested:
                    self.last_ingest_source_date = ingested[0].source_date
                    actions["ingest"] = ingested[0].source_date.isoformat()
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                actions["ingest_error"] = str(exc)
        return actions

    def ingest_published_closes(self, *, retrieved_at: datetime) -> list:
        with self.http_factory() as client:
            primary = EcbEurofxrefSource().fetch(client, retrieved_at=retrieved_at)
            stored = self.fx.persist_ecb_closes(primary)
            try:
                checks = BoeXudlussSource().fetch(client, retrieved_at=retrieved_at)
            except Exception:
                checks = []
            if checks:
                self.fx.apply_boe_checks(checks)
            return stored

    def next_london_midnight(self, *, now: datetime | None = None) -> datetime:
        instant = (now or self.clock()).astimezone(LONDON)
        today_midnight = datetime.combine(instant.date(), time(0, 0), tzinfo=LONDON)
        if instant > today_midnight:
            return today_midnight + timedelta(days=1)
        return today_midnight

    async def start(self) -> None:
        if not self.enabled:
            return
        if self._task is not None and not self._task.done():
            return
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                await asyncio.to_thread(self.run_due_jobs)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=60)
            except TimeoutError:
                continue
