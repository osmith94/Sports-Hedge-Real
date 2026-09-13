from __future__ import annotations

from collections.abc import Mapping
from datetime import date, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from sports_hedge.fx.models import (
    DailyFxRate,
    FxCheckStatus,
    FxRateUnavailable,
    PublishedFxClose,
    variance_bps,
)
from sports_hedge.fx.repository import SqliteFxRateRepository
from sports_hedge.paper.models import FxRateSnapshot

LONDON = ZoneInfo("Europe/London")
GBP = FxRateSnapshot(
    currency="GBP",
    gbp_per_unit=Decimal("1"),
    source="functional_currency",
)


class FxRateService:
    """Persist ECB primary closes, BoE checks, and London valuation carry-forward."""

    def __init__(
        self,
        repository: SqliteFxRateRepository,
        *,
        check_tolerance_bps: Decimal = Decimal("25"),
        stale_after_days: int = 7,
    ) -> None:
        self.repository = repository
        self.check_tolerance_bps = Decimal(check_tolerance_bps)
        self.stale_after_days = stale_after_days

    def persist_ecb_closes(
        self,
        closes: list[PublishedFxClose],
        *,
        checks: Mapping[tuple[str, date], PublishedFxClose] | None = None,
    ) -> list[DailyFxRate]:
        stored: list[DailyFxRate] = []
        check_map = checks or {}
        for close in closes:
            check = check_map.get((close.currency, close.source_date))
            candidate = self._published_row(close, check=check)
            existing = self.repository.get(close.currency, close.source_date)
            if existing is None:
                stored.append(self.repository.insert_if_absent(candidate))
                continue
            if existing.status is FxCheckStatus.CARRIED_FORWARD:
                stored.append(self.repository.replace_carried_forward_with_published(candidate))
                continue
            if existing.gbp_per_unit != candidate.gbp_per_unit:
                stored.append(existing)
                continue
            if check is not None and existing.status is not FxCheckStatus.CARRIED_FORWARD:
                stored.append(self.repository.update_check_fields(candidate.model_copy(
                    update={
                        "gbp_per_unit": existing.gbp_per_unit,
                        "primary_source": existing.primary_source,
                        "primary_source_id": existing.primary_source_id,
                        "raw_primary": existing.raw_primary,
                        "retrieved_at": existing.retrieved_at,
                        "captured_at": existing.captured_at,
                    }
                )))
            else:
                stored.append(existing)
        return stored

    def has_published_primary(self, currency: str, source_date: date) -> bool:
        """True when a non-carried-forward ECB primary exists for this source date."""

        row = self.repository.get(currency, source_date)
        return (
            row is not None
            and row.status is not FxCheckStatus.CARRIED_FORWARD
            and row.source_date == source_date
        )

    def apply_boe_checks(self, checks: list[PublishedFxClose]) -> list[DailyFxRate]:
        updated: list[DailyFxRate] = []
        for check in checks:
            existing = self.repository.get(check.currency, check.source_date)
            if existing is None:
                continue
            if existing.status is FxCheckStatus.CARRIED_FORWARD:
                continue
            merged = self._with_check(existing, check)
            updated.append(self.repository.update_check_fields(merged))
        return updated

    def ensure_valuation_rate(
        self,
        currency: str,
        valuation_date: date,
        *,
        as_of: datetime,
    ) -> DailyFxRate:
        currency = currency.upper()
        if currency == "GBP":
            return DailyFxRate(
                currency="GBP",
                gbp_per_unit=Decimal("1"),
                valuation_date=valuation_date,
                source_date=valuation_date,
                retrieved_at=as_of,
                primary_source="functional_currency",
                primary_source_id="gbp",
                status=FxCheckStatus.AGREED,
            )
        existing = self.repository.get(currency, valuation_date)
        if existing is not None:
            return existing
        published = self.repository.latest_published(currency, on_or_before=valuation_date)
        if published is None:
            raise FxRateUnavailable(
                f"missing_fx_rate:{currency}",
                f"no persisted ECB primary rate for {currency} on or before {valuation_date}",
            )
        if published.source_date == valuation_date:
            return published
        carried = DailyFxRate(
            currency=currency,
            gbp_per_unit=published.gbp_per_unit,
            valuation_date=valuation_date,
            source_date=published.source_date,
            retrieved_at=as_of,
            primary_source=published.primary_source,
            primary_source_id=published.primary_source_id,
            raw_primary=published.raw_primary,
            check_source=published.check_source,
            check_source_id=published.check_source_id,
            check_gbp_per_unit=published.check_gbp_per_unit,
            raw_check=published.raw_check,
            variance_bps=published.variance_bps,
            status=FxCheckStatus.CARRIED_FORWARD,
            captured_at=published.captured_at,
        )
        return self.repository.insert_if_absent(carried)

    def resolve_for_scanner(self, currency: str, *, as_of: datetime) -> FxRateSnapshot:
        currency = currency.upper()
        if currency == "GBP":
            return GBP.model_copy(update={"captured_at": as_of})
        london_date = as_of.astimezone(LONDON).date()
        published = self.repository.latest_published(currency, on_or_before=london_date)
        if published is None:
            raise FxRateUnavailable(
                f"missing_fx_rate:{currency}",
                f"required {currency} FX rate is not persisted",
            )
        age_days = (london_date - published.source_date).days
        if age_days > self.stale_after_days:
            raise FxRateUnavailable(
                f"stale_fx_rate:{currency}",
                (
                    f"{currency} ECB source date {published.source_date.isoformat()} "
                    f"is older than {self.stale_after_days} days"
                ),
            )
        snapshot = FxRateSnapshot(
            currency=published.currency,
            gbp_per_unit=published.gbp_per_unit,
            source=published.primary_source,
            captured_at=published.captured_at or published.retrieved_at,
            source_date=published.source_date,
            valuation_date=published.valuation_date,
            check_status=published.status.value,
            variance_bps=published.variance_bps,
        )
        if london_date != published.source_date:
            snapshot = snapshot.model_copy(
                update={
                    "valuation_date": london_date,
                    "check_status": FxCheckStatus.CARRIED_FORWARD.value,
                }
            )
        return snapshot

    def paper_snapshots(self, currencies: set[str], *, as_of: datetime) -> list[FxRateSnapshot]:
        snapshots = [self.resolve_for_scanner(currency, as_of=as_of) for currency in sorted(currencies)]
        if not any(item.currency == "GBP" for item in snapshots):
            snapshots.append(self.resolve_for_scanner("GBP", as_of=as_of))
        return snapshots

    def economics_status(self, *, as_of: datetime) -> list[DailyFxRate]:
        london_date = as_of.astimezone(LONDON).date()
        published = self.repository.list_latest_published()
        rows: list[DailyFxRate] = []
        for rate in published:
            if rate.currency == "GBP":
                continue
            display = rate
            if london_date != rate.source_date:
                try:
                    display = self.ensure_valuation_rate(
                        rate.currency, london_date, as_of=as_of
                    )
                except FxRateUnavailable:
                    display = rate
            rows.append(display)
        return rows

    def _published_row(
        self,
        close: PublishedFxClose,
        *,
        check: PublishedFxClose | None,
    ) -> DailyFxRate:
        status = FxCheckStatus.PENDING_CHECK
        check_gbp = None
        variance = None
        check_source = None
        check_source_id = None
        raw_check: dict = {}
        if check is not None:
            check_gbp = check.gbp_per_unit
            variance = variance_bps(close.gbp_per_unit, check.gbp_per_unit)
            check_source = check.source
            check_source_id = check.source_id
            raw_check = check.raw
            status = (
                FxCheckStatus.AGREED
                if variance <= self.check_tolerance_bps
                else FxCheckStatus.EXCEPTION
            )
        return DailyFxRate(
            currency=close.currency,
            gbp_per_unit=close.gbp_per_unit,
            valuation_date=close.source_date,
            source_date=close.source_date,
            retrieved_at=close.retrieved_at,
            primary_source=close.source,
            primary_source_id=close.source_id,
            raw_primary=close.raw,
            check_source=check_source,
            check_source_id=check_source_id,
            check_gbp_per_unit=check_gbp,
            raw_check=raw_check,
            variance_bps=variance,
            status=status,
            captured_at=close.retrieved_at,
        )

    def _with_check(self, existing: DailyFxRate, check: PublishedFxClose) -> DailyFxRate:
        variance = variance_bps(existing.gbp_per_unit, check.gbp_per_unit)
        status = (
            FxCheckStatus.AGREED
            if variance <= self.check_tolerance_bps
            else FxCheckStatus.EXCEPTION
        )
        return existing.model_copy(
            update={
                "check_source": check.source,
                "check_source_id": check.source_id,
                "check_gbp_per_unit": check.gbp_per_unit,
                "raw_check": check.raw,
                "variance_bps": variance,
                "status": status,
            }
        )


def london_calendar_date(instant: datetime) -> date:
    return instant.astimezone(LONDON).date()


ECB_PUBLICATION_HOUR_LONDON = 16
ECB_PUBLICATION_MINUTE_LONDON = 15


def ecb_publication_window_open(instant: datetime) -> bool:
    """Daily ECB ingest is due from 16:15 Europe/London on working days, not at midnight."""

    local = instant.astimezone(LONDON)
    if local.weekday() >= 5:
        return False
    return (local.hour, local.minute) >= (ECB_PUBLICATION_HOUR_LONDON, ECB_PUBLICATION_MINUTE_LONDON)
