"""Narrow persisted Matchbook account commission override.

Not a generic settings system. One optional rate replaces the provider
UK Matchbook football net-win commission until reset.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel, Field

from sports_hedge.fees.cost import FeeBasis


class MatchbookAccountFeeStatus(BaseModel):
    provider_default_rate: Decimal
    override_rate: Decimal | None = None
    effective_rate: Decimal
    fee_basis: FeeBasis = FeeBasis.PROFIT_COMMISSION
    account_assumption: bool
    label: str
    source: str
    detail: str
    updated_at: datetime | None = None


class MatchbookFeeUpdate(BaseModel):
    commission_rate: Decimal = Field(ge=0, lt=1)


class SqliteMatchbookAccountFeeStore:
    """Singleton-row SQLite store for the operator Matchbook commission."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS matchbook_account_fee_override (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                commission_rate TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                source TEXT NOT NULL
            );
            """
        )
        self._connection.commit()

    def get_override(self) -> tuple[Decimal, datetime] | None:
        row = self._connection.execute(
            "SELECT commission_rate, updated_at FROM matchbook_account_fee_override WHERE id = 1"
        ).fetchone()
        if row is None:
            return None
        return Decimal(row["commission_rate"]), datetime.fromisoformat(row["updated_at"])

    def set_override(self, rate: Decimal) -> Decimal:
        if rate < 0 or rate >= 1:
            raise ValueError("Matchbook commission rate must be in [0, 1)")
        now = datetime.now(UTC).isoformat()
        self._connection.execute(
            """
            INSERT INTO matchbook_account_fee_override (id, commission_rate, updated_at, source)
            VALUES (1, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                commission_rate = excluded.commission_rate,
                updated_at = excluded.updated_at,
                source = excluded.source
            """,
            (str(rate), now, "operator_account_assumption:matchbook_net_win_commission"),
        )
        self._connection.commit()
        return rate

    def clear_override(self) -> None:
        self._connection.execute("DELETE FROM matchbook_account_fee_override WHERE id = 1")
        self._connection.commit()

    def close(self) -> None:
        self._connection.close()
