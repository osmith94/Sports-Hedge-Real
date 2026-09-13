from __future__ import annotations

import json
import sqlite3
from datetime import date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sports_hedge.fx.models import DailyFxRate, FxCheckStatus


class SqliteFxRateRepository:
    """Append-friendly daily FX store. Primary rates are not overwritten."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS fx_daily_rates (
                currency TEXT NOT NULL,
                valuation_date TEXT NOT NULL,
                source_date TEXT NOT NULL,
                gbp_per_unit TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                primary_source TEXT NOT NULL,
                primary_source_id TEXT NOT NULL,
                raw_primary_json TEXT NOT NULL,
                check_source TEXT,
                check_source_id TEXT,
                check_gbp_per_unit TEXT,
                raw_check_json TEXT NOT NULL,
                variance_bps TEXT,
                status TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                PRIMARY KEY (currency, valuation_date)
            );
            CREATE INDEX IF NOT EXISTS idx_fx_rates_source_date
                ON fx_daily_rates(currency, source_date DESC);
            """
        )
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()

    def get(self, currency: str, valuation_date: date) -> DailyFxRate | None:
        row = self._connection.execute(
            """
            SELECT * FROM fx_daily_rates
            WHERE currency = ? AND valuation_date = ?
            """,
            (currency.upper(), valuation_date.isoformat()),
        ).fetchone()
        return _row_to_rate(row) if row is not None else None

    def latest_published(self, currency: str, *, on_or_before: date | None = None) -> DailyFxRate | None:
        clauses = ["currency = ?", "status != ?"]
        params: list[Any] = [currency.upper(), FxCheckStatus.CARRIED_FORWARD.value]
        if on_or_before is not None:
            clauses.append("source_date <= ?")
            params.append(on_or_before.isoformat())
        row = self._connection.execute(
            f"""
            SELECT * FROM fx_daily_rates
            WHERE {' AND '.join(clauses)}
            ORDER BY source_date DESC, valuation_date DESC
            LIMIT 1
            """,
            params,
        ).fetchone()
        return _row_to_rate(row) if row is not None else None

    def latest_for_valuation(self, currency: str, *, on_or_before: date) -> DailyFxRate | None:
        row = self._connection.execute(
            """
            SELECT * FROM fx_daily_rates
            WHERE currency = ? AND valuation_date <= ?
            ORDER BY valuation_date DESC
            LIMIT 1
            """,
            (currency.upper(), on_or_before.isoformat()),
        ).fetchone()
        return _row_to_rate(row) if row is not None else None

    def list_for_valuation_date(self, valuation_date: date) -> list[DailyFxRate]:
        rows = self._connection.execute(
            """
            SELECT * FROM fx_daily_rates
            WHERE valuation_date = ?
            ORDER BY currency
            """,
            (valuation_date.isoformat(),),
        ).fetchall()
        return [_row_to_rate(row) for row in rows]

    def list_latest_published(self) -> list[DailyFxRate]:
        rows = self._connection.execute(
            """
            SELECT * FROM fx_daily_rates
            WHERE status != ?
            ORDER BY currency, source_date DESC, valuation_date DESC
            """,
            (FxCheckStatus.CARRIED_FORWARD.value,),
        ).fetchall()
        latest: dict[str, DailyFxRate] = {}
        for row in rows:
            rate = _row_to_rate(row)
            latest.setdefault(rate.currency, rate)
        return list(latest.values())

    def insert_if_absent(self, rate: DailyFxRate) -> DailyFxRate:
        existing = self.get(rate.currency, rate.valuation_date)
        if existing is not None:
            return existing
        self._connection.execute(
            """
            INSERT INTO fx_daily_rates (
                currency, valuation_date, source_date, gbp_per_unit, retrieved_at,
                primary_source, primary_source_id, raw_primary_json, check_source,
                check_source_id, check_gbp_per_unit, raw_check_json, variance_bps,
                status, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            _rate_to_row(rate),
        )
        self._connection.commit()
        return rate

    def replace_carried_forward_with_published(self, rate: DailyFxRate) -> DailyFxRate:
        """Upgrade a same-date carry-forward placeholder to the published ECB primary.

        Never replaces an already-persisted published primary.
        """

        existing = self.get(rate.currency, rate.valuation_date)
        if existing is None:
            return self.insert_if_absent(rate)
        if existing.status is not FxCheckStatus.CARRIED_FORWARD:
            return existing
        self._connection.execute(
            """
            UPDATE fx_daily_rates
            SET source_date = ?, gbp_per_unit = ?, retrieved_at = ?,
                primary_source = ?, primary_source_id = ?, raw_primary_json = ?,
                check_source = ?, check_source_id = ?, check_gbp_per_unit = ?,
                raw_check_json = ?, variance_bps = ?, status = ?, captured_at = ?
            WHERE currency = ? AND valuation_date = ? AND status = ?
            """,
            (
                rate.source_date.isoformat(),
                str(rate.gbp_per_unit),
                rate.retrieved_at.isoformat(),
                rate.primary_source,
                rate.primary_source_id,
                json.dumps(rate.raw_primary),
                rate.check_source,
                rate.check_source_id,
                None if rate.check_gbp_per_unit is None else str(rate.check_gbp_per_unit),
                json.dumps(rate.raw_check),
                None if rate.variance_bps is None else str(rate.variance_bps),
                rate.status.value,
                (rate.captured_at or rate.retrieved_at).isoformat(),
                rate.currency,
                rate.valuation_date.isoformat(),
                FxCheckStatus.CARRIED_FORWARD.value,
            ),
        )
        self._connection.commit()
        stored = self.get(rate.currency, rate.valuation_date)
        if stored is None:
            raise KeyError(f"FX row vanished for {rate.currency} {rate.valuation_date}")
        return stored

    def update_check_fields(self, rate: DailyFxRate) -> DailyFxRate:
        """Attach/replace independent-check fields only. Never changes gbp_per_unit."""

        existing = self.get(rate.currency, rate.valuation_date)
        if existing is None:
            raise KeyError(f"no FX row for {rate.currency} {rate.valuation_date}")
        if existing.gbp_per_unit != rate.gbp_per_unit:
            raise ValueError("refusing to overwrite primary gbp_per_unit")
        self._connection.execute(
            """
            UPDATE fx_daily_rates
            SET check_source = ?, check_source_id = ?, check_gbp_per_unit = ?,
                raw_check_json = ?, variance_bps = ?, status = ?
            WHERE currency = ? AND valuation_date = ?
            """,
            (
                rate.check_source,
                rate.check_source_id,
                None if rate.check_gbp_per_unit is None else str(rate.check_gbp_per_unit),
                json.dumps(rate.raw_check),
                None if rate.variance_bps is None else str(rate.variance_bps),
                rate.status.value,
                rate.currency,
                rate.valuation_date.isoformat(),
            ),
        )
        self._connection.commit()
        stored = self.get(rate.currency, rate.valuation_date)
        if stored is None:
            raise KeyError(f"FX row vanished for {rate.currency} {rate.valuation_date}")
        return stored


def _rate_to_row(rate: DailyFxRate) -> tuple[Any, ...]:
    captured = rate.captured_at or rate.retrieved_at
    return (
        rate.currency,
        rate.valuation_date.isoformat(),
        rate.source_date.isoformat(),
        str(rate.gbp_per_unit),
        rate.retrieved_at.isoformat(),
        rate.primary_source,
        rate.primary_source_id,
        json.dumps(rate.raw_primary),
        rate.check_source,
        rate.check_source_id,
        None if rate.check_gbp_per_unit is None else str(rate.check_gbp_per_unit),
        json.dumps(rate.raw_check),
        None if rate.variance_bps is None else str(rate.variance_bps),
        rate.status.value,
        captured.isoformat(),
    )


def _row_to_rate(row: sqlite3.Row) -> DailyFxRate:
    return DailyFxRate(
        currency=row["currency"],
        gbp_per_unit=Decimal(row["gbp_per_unit"]),
        valuation_date=date.fromisoformat(row["valuation_date"]),
        source_date=date.fromisoformat(row["source_date"]),
        retrieved_at=datetime.fromisoformat(row["retrieved_at"]),
        primary_source=row["primary_source"],
        primary_source_id=row["primary_source_id"],
        raw_primary=json.loads(row["raw_primary_json"]),
        check_source=row["check_source"],
        check_source_id=row["check_source_id"],
        check_gbp_per_unit=(
            None if row["check_gbp_per_unit"] is None else Decimal(row["check_gbp_per_unit"])
        ),
        raw_check=json.loads(row["raw_check_json"] or "{}"),
        variance_bps=None if row["variance_bps"] is None else Decimal(row["variance_bps"]),
        status=FxCheckStatus(row["status"]),
        captured_at=datetime.fromisoformat(row["captured_at"]),
    )
