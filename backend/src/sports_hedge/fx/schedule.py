from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, date, datetime, time, timedelta
from logging import getLogger
from zoneinfo import ZoneInfo

import httpx

from sports_hedge.accounting.paper_journal import PaperJournal
from sports_hedge.accounting.revaluation import DailyFxRevaluationService
from sports_hedge.fx.models import FxRateUnavailable
from sports_hedge.fx.service import FxRateService, ecb_publication_window_open, london_calendar_date
from sports_hedge.fx.sources import BoeXudlussSource, EcbEurofxrefSource

LOGGER = getLogger(__name__)
LONDON = ZoneInfo("Europe/London")
Clock = Callable[[], datetime]

# Startup is on the demo critical path. ECB must fail closed quickly; BoE is a
# best-effort independent check and is skipped on startup so it cannot add ~30s.
ECB_STARTUP_TIMEOUT_SECONDS = 5.0
ECB_SCHEDULED_TIMEOUT_SECONDS = 8.0
BOE_CHECK_TIMEOUT_SECONDS = 2.0
PRIOR_CLOSE_RETRY_SECONDS = 300.0


class AccountingSchedule:
    """Separate source ingestion from the 00:00 Europe/London valuation cut-off.

    Daily ECB ingest is contracted around 16:15 Europe/London on working days and
    is idempotent after a successful pull for that London date. Startup bootstraps
    the latest published close when scanner USD is missing or stale, including
    weekends and holidays (carry-forward of the latest working-day close).
    """

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
        self.last_daily_ingest_london_date: date | None = None
        self.last_revaluation_date: date | None = None
        self.last_error: str | None = None
        self.last_bootstrap_source_date: date | None = None
        self._next_window_attempt_at: datetime | None = None
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def scanner_usd_usable(self, *, as_of: datetime) -> bool:
        try:
            self.fx.resolve_for_scanner("USD", as_of=as_of)
            return True
        except FxRateUnavailable:
            return False

    def bootstrap_if_needed(self, *, now: datetime | None = None) -> dict[str, str]:
        """Fetch the latest published ECB daily close when scanner USD is unusable.

        Ignores the 16:15 UK publication window so a Sunday/holiday demo start can
        persist Friday's close. Does not invent a treasury/demo fallback rate.
        """

        instant = now or self.clock()
        if self.scanner_usd_usable(as_of=instant):
            return {}
        return self._ingest(
            instant,
            action_key="bootstrap",
            mark_daily_window=False,
            ecb_timeout=ECB_STARTUP_TIMEOUT_SECONDS,
            include_boe=False,
        )

    def has_todays_published_usd(self, london_date: date) -> bool:
        return self.fx.has_published_primary("USD", london_date)

    def _window_ingest_due(self, instant: datetime, london_date: date) -> bool:
        if not ecb_publication_window_open(instant):
            return False
        if self.has_todays_published_usd(london_date):
            return False
        if self._next_window_attempt_at is not None:
            if instant.astimezone(UTC) < self._next_window_attempt_at.astimezone(UTC):
                return False
        return True

    def run_due_jobs(self, *, now: datetime | None = None, startup: bool = False) -> dict[str, str]:
        instant = now or self.clock()
        actions: dict[str, str] = {}
        london_date = london_calendar_date(instant)
        window_open = ecb_publication_window_open(instant)
        ecb_timeout = ECB_STARTUP_TIMEOUT_SECONDS if startup else ECB_SCHEDULED_TIMEOUT_SECONDS
        include_boe = not startup
        if not self.scanner_usd_usable(as_of=instant) and not window_open:
            actions.update(self.bootstrap_if_needed(now=instant))
        ingested_new_primary = False
        if self._window_ingest_due(instant, london_date):
            ingested = self._ingest(
                instant,
                action_key="ingest",
                mark_daily_window=True,
                ecb_timeout=ecb_timeout,
                include_boe=include_boe,
            )
            actions.update(ingested)
            ingested_new_primary = ingested.get("ingest") == london_date.isoformat()
        if self.last_revaluation_date != london_date or ingested_new_primary:
            try:
                self.revaluation.run(valuation_date=london_date, as_of=instant)
                self.last_revaluation_date = london_date
                actions["revaluation"] = london_date.isoformat()
            except Exception as exc:  # noqa: BLE001 — scheduler must keep ticking
                self.last_error = str(exc)
                actions["revaluation_error"] = str(exc)
        return actions

    def _ingest(
        self,
        instant: datetime,
        *,
        action_key: str,
        mark_daily_window: bool,
        ecb_timeout: float,
        include_boe: bool,
    ) -> dict[str, str]:
        try:
            stored = self.ingest_published_closes(
                retrieved_at=instant,
                ecb_timeout=ecb_timeout,
                include_boe=include_boe,
            )
        except Exception as exc:  # noqa: BLE001
            self.last_error = str(exc)
            LOGGER.warning("FX %s failed closed: %s", action_key, exc)
            return {f"{action_key}_error": str(exc)}
        if not stored:
            self.last_error = "ecb_eurofxref returned no published closes"
            LOGGER.warning("FX %s produced no published closes", action_key)
            return {f"{action_key}_error": self.last_error}
        source_date = stored[0].source_date
        usd_row = next((item for item in stored if item.currency == "USD"), None)
        if usd_row is not None:
            source_date = usd_row.source_date
        self.last_ingest_source_date = source_date
        if action_key == "bootstrap":
            self.last_bootstrap_source_date = source_date
        london_date = london_calendar_date(instant)
        if mark_daily_window:
            if source_date == london_date:
                self.last_daily_ingest_london_date = london_date
                self._next_window_attempt_at = None
            else:
                retry_at = instant + timedelta(seconds=PRIOR_CLOSE_RETRY_SECONDS)
                self._next_window_attempt_at = retry_at
                LOGGER.info(
                    "FX ingest still prior close source_date=%s london_date=%s; retry after %ss",
                    source_date.isoformat(),
                    london_date.isoformat(),
                    int(PRIOR_CLOSE_RETRY_SECONDS),
                )
                self.last_error = None
                self._log_scanner_usd(as_of=instant, action=f"{action_key}_prior_close")
                return {"ingest_pending": source_date.isoformat()}
        self.last_error = None
        self._log_scanner_usd(as_of=instant, action=action_key)
        return {action_key: source_date.isoformat()}

    def ingest_published_closes(
        self,
        *,
        retrieved_at: datetime,
        ecb_timeout: float = ECB_SCHEDULED_TIMEOUT_SECONDS,
        include_boe: bool = True,
        boe_timeout: float = BOE_CHECK_TIMEOUT_SECONDS,
    ) -> list:
        with self.http_factory() as client:
            primary = EcbEurofxrefSource().fetch(
                client, retrieved_at=retrieved_at, timeout=ecb_timeout
            )
            stored = self.fx.persist_ecb_closes(primary)
            if include_boe:
                try:
                    checks = BoeXudlussSource().fetch(
                        client, retrieved_at=retrieved_at, timeout=boe_timeout
                    )
                except Exception:
                    checks = []
                if checks:
                    self.fx.apply_boe_checks(checks)
            return stored

    def operator_status(self, *, as_of: datetime | None = None) -> dict[str, object]:
        instant = as_of or self.clock()
        payload: dict[str, object] = {
            "enabled": self.enabled,
            "last_ingest_source_date": (
                None
                if self.last_ingest_source_date is None
                else self.last_ingest_source_date.isoformat()
            ),
            "last_daily_ingest_london_date": (
                None
                if self.last_daily_ingest_london_date is None
                else self.last_daily_ingest_london_date.isoformat()
            ),
            "last_bootstrap_source_date": (
                None
                if self.last_bootstrap_source_date is None
                else self.last_bootstrap_source_date.isoformat()
            ),
            "last_error": self.last_error,
            "next_window_attempt_at": (
                None
                if self._next_window_attempt_at is None
                else self._next_window_attempt_at.isoformat()
            ),
        }
        try:
            snapshot = self.fx.resolve_for_scanner("USD", as_of=instant)
        except FxRateUnavailable as exc:
            payload["scanner_usd"] = {
                "available": False,
                "reason": exc.reason,
                "detail": exc.detail,
            }
            return payload
        payload["scanner_usd"] = {
            "available": True,
            "currency": snapshot.currency,
            "gbp_per_unit": str(snapshot.gbp_per_unit),
            "source": snapshot.source,
            "source_date": None if snapshot.source_date is None else snapshot.source_date.isoformat(),
            "valuation_date": (
                None if snapshot.valuation_date is None else snapshot.valuation_date.isoformat()
            ),
            "retrieved_at": snapshot.captured_at.isoformat() if snapshot.captured_at else None,
            "check_status": snapshot.check_status,
            "variance_bps": None if snapshot.variance_bps is None else str(snapshot.variance_bps),
        }
        return payload

    def _log_scanner_usd(self, *, as_of: datetime, action: str = "status") -> None:
        try:
            snapshot = self.fx.resolve_for_scanner("USD", as_of=as_of)
        except FxRateUnavailable as exc:
            LOGGER.warning(
                "FX scanner USD unavailable after %s (%s): %s",
                action,
                exc.reason,
                exc.detail,
            )
            return
        LOGGER.info(
            "FX scanner USD gbp_per_unit=%s source=%s source_date=%s "
            "valuation_date=%s check_status=%s retrieved_at=%s variance_bps=%s action=%s",
            snapshot.gbp_per_unit,
            snapshot.source,
            None if snapshot.source_date is None else snapshot.source_date.isoformat(),
            snapshot.valuation_date.isoformat() if snapshot.valuation_date else None,
            snapshot.check_status,
            snapshot.captured_at.isoformat() if snapshot.captured_at else None,
            snapshot.variance_bps,
            action,
        )

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
        await asyncio.to_thread(self._startup)
        self._stop = asyncio.Event()
        self._task = asyncio.create_task(self._loop())

    def _startup(self) -> None:
        instant = self.clock()
        self.run_due_jobs(now=instant, startup=True)
        self._log_scanner_usd(as_of=instant, action="startup")

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
