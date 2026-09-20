"""SQLite persistence for the append-only ACTIVE TRADE event journal."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any, Iterable

from sports_hedge.paper.active_trade_journal import (
    ACTIVE_TRADE_EVENT_SCHEMA,
    ActiveTradeEvent,
    ActiveTradeEventType,
    ActiveTradeReasonCode,
    json_safe,
)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS active_trade_events (
    event_id TEXT PRIMARY KEY,
    sequence INTEGER NOT NULL,
    occurred_at TEXT NOT NULL,
    schema_version TEXT NOT NULL,
    serving_git_sha TEXT,
    trade_id TEXT NOT NULL,
    opportunity_id TEXT,
    canonical_event_id TEXT,
    canonical_market_id TEXT,
    competition TEXT,
    market_family TEXT,
    line TEXT,
    active_phase TEXT,
    event_type TEXT NOT NULL,
    reason_code TEXT NOT NULL,
    operator_copy TEXT NOT NULL,
    venue TEXT,
    cycle_id TEXT,
    tranche_id TEXT,
    fill_id TEXT,
    dedupe_key TEXT NOT NULL UNIQUE,
    payload_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_active_trade_events_trade
    ON active_trade_events(trade_id, occurred_at, sequence);
CREATE INDEX IF NOT EXISTS idx_active_trade_events_type
    ON active_trade_events(event_type, occurred_at);
CREATE INDEX IF NOT EXISTS idx_active_trade_events_reason
    ON active_trade_events(reason_code, occurred_at);
CREATE INDEX IF NOT EXISTS idx_active_trade_events_venue
    ON active_trade_events(venue, occurred_at);
CREATE INDEX IF NOT EXISTS idx_active_trade_events_cycle
    ON active_trade_events(cycle_id);
"""


class SqliteActiveTradeEventJournal:
    """Append-only journal on the paper ledger connection. Shares nested transactions."""

    def __init__(self, ledger: Any) -> None:
        self._ledger = ledger
        self._connection = ledger._connection

    def append(self, event: ActiveTradeEvent) -> bool:
        """Insert one event. Returns False when the dedupe key already exists."""

        payload = json.dumps(json_safe(event.payload))
        try:
            self._connection.execute(
                """
                INSERT INTO active_trade_events (
                    event_id, sequence, occurred_at, schema_version, serving_git_sha,
                    trade_id, opportunity_id, canonical_event_id, canonical_market_id,
                    competition, market_family, line, active_phase, event_type, reason_code,
                    operator_copy, venue, cycle_id, tranche_id, fill_id, dedupe_key, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.sequence,
                    event.occurred_at.isoformat(),
                    event.schema_version or ACTIVE_TRADE_EVENT_SCHEMA,
                    event.serving_git_sha,
                    event.trade_id,
                    event.opportunity_id,
                    event.canonical_event_id,
                    event.canonical_market_id,
                    event.competition,
                    event.market_family,
                    event.line,
                    event.active_phase,
                    event.event_type.value,
                    event.reason_code.value,
                    event.operator_copy,
                    event.venue,
                    event.cycle_id,
                    event.tranche_id,
                    event.fill_id,
                    event.dedupe_key,
                    payload,
                ),
            )
        except sqlite3.IntegrityError:
            return False
        self._ledger._commit()
        return True

    def next_sequence(self) -> int:
        row = self._connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) AS seq FROM active_trade_events"
        ).fetchone()
        return int(row["seq"] if row is not None else 0) + 1

    def query(
        self,
        *,
        trade_id: str | None = None,
        event_type: str | None = None,
        venue: str | None = None,
        reason_code: str | None = None,
        after: datetime | None = None,
        before: datetime | None = None,
        cycle_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ActiveTradeEvent]:
        clauses: list[str] = []
        params: list[Any] = []
        if trade_id:
            clauses.append("trade_id = ?")
            params.append(trade_id)
        if event_type:
            clauses.append("event_type = ?")
            params.append(event_type)
        if venue:
            clauses.append("venue = ?")
            params.append(venue)
        if reason_code:
            clauses.append("reason_code = ?")
            params.append(reason_code)
        if cycle_id:
            clauses.append("cycle_id = ?")
            params.append(cycle_id)
        if after is not None:
            clauses.append("occurred_at >= ?")
            params.append(after.isoformat())
        if before is not None:
            clauses.append("occurred_at <= ?")
            params.append(before.isoformat())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        bound = max(1, min(int(limit), 500))
        skip = max(0, int(offset))
        rows = self._connection.execute(
            f"""
            SELECT * FROM active_trade_events
            {where}
            ORDER BY sequence ASC, occurred_at ASC
            LIMIT ? OFFSET ?
            """,
            [*params, bound, skip],
        ).fetchall()
        return [_event_from_row(row) for row in rows]

    def recent(self, *, limit: int = 12, trade_id: str | None = None) -> list[ActiveTradeEvent]:
        """Newest events first in storage order, returned oldest→newest for display."""

        bound = max(1, min(int(limit), 50))
        clauses: list[str] = []
        params: list[Any] = []
        if trade_id:
            clauses.append("trade_id = ?")
            params.append(trade_id)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"""
            SELECT * FROM active_trade_events
            {where}
            ORDER BY sequence DESC, occurred_at DESC
            LIMIT ?
            """,
            [*params, bound],
        ).fetchall()
        return list(reversed([_event_from_row(row) for row in rows]))


def ensure_active_trade_event_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(SCHEMA_SQL)
    connection.commit()


def _event_from_row(row: sqlite3.Row) -> ActiveTradeEvent:
    payload = json.loads(row["payload_json"] or "{}")
    return ActiveTradeEvent(
        event_id=row["event_id"],
        sequence=int(row["sequence"]),
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        schema_version=row["schema_version"],
        serving_git_sha=row["serving_git_sha"],
        trade_id=row["trade_id"],
        opportunity_id=row["opportunity_id"],
        canonical_event_id=row["canonical_event_id"],
        canonical_market_id=row["canonical_market_id"],
        competition=row["competition"],
        market_family=row["market_family"],
        line=row["line"],
        active_phase=row["active_phase"],
        event_type=ActiveTradeEventType(row["event_type"]),
        reason_code=ActiveTradeReasonCode(row["reason_code"]),
        operator_copy=row["operator_copy"],
        venue=row["venue"],
        cycle_id=row["cycle_id"],
        tranche_id=row["tranche_id"],
        fill_id=row["fill_id"],
        dedupe_key=row["dedupe_key"],
        payload=payload if isinstance(payload, dict) else {},
    )
