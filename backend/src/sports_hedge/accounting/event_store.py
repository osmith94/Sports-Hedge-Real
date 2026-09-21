"""Append-only accounting event store with deterministic idempotent IDs.

Duplicates with matching facts are no-ops. Conflicting facts fail closed.
This store never talks to venue providers and is not on the scan critical path
except as a best-effort emit that must not raise into operational code.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from sqlite3 import IntegrityError
from typing import Protocol

from sports_hedge.accounting.events import (
    AccountingDomainEvent,
    DuplicateAccountingEventError,
)

ACCOUNTING_EVENTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS accounting_domain_events (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id TEXT NOT NULL UNIQUE,
    schema_version INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    source TEXT NOT NULL,
    source_id TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    provenance TEXT NOT NULL,
    recorded_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    UNIQUE(source, source_id)
);

CREATE INDEX IF NOT EXISTS idx_accounting_domain_events_occurred
    ON accounting_domain_events(occurred_at, sequence);

CREATE TABLE IF NOT EXISTS accounting_projection_snapshots (
    projection_name TEXT NOT NULL,
    projection_version INTEGER NOT NULL,
    event_schema_version INTEGER NOT NULL,
    last_event_sequence INTEGER NOT NULL,
    last_event_id TEXT,
    event_count INTEGER NOT NULL,
    rebuilt_at TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    PRIMARY KEY (projection_name, projection_version)
);
"""


class AccountingEventStore(Protocol):
    def append(self, event: AccountingDomainEvent) -> AccountingDomainEvent: ...

    def append_idempotent(
        self, event: AccountingDomainEvent
    ) -> tuple[AccountingDomainEvent, bool]: ...

    def get(self, source: str, source_id: str) -> AccountingDomainEvent | None: ...

    def get_by_id(self, event_id: str) -> AccountingDomainEvent | None: ...

    def list_in_order(self) -> list[AccountingDomainEvent]: ...

    def last_sequence(self) -> int: ...


class InMemoryAccountingEventStore:
    def __init__(self) -> None:
        self._events: list[AccountingDomainEvent] = []
        self._by_source: dict[tuple[str, str], AccountingDomainEvent] = {}
        self._by_id: dict[str, AccountingDomainEvent] = {}

    def append(self, event: AccountingDomainEvent) -> AccountingDomainEvent:
        key = (event.source, event.source_id)
        existing = self._by_source.get(key) or self._by_id.get(event.event_id)
        if existing is not None:
            raise DuplicateAccountingEventError(
                f"duplicate accounting event {event.source}:{event.source_id}"
            )
        stored = event.model_copy(
            update={
                "sequence": len(self._events) + 1,
                "recorded_at": event.recorded_at or datetime.now(UTC),
            }
        )
        self._events.append(stored)
        self._by_source[key] = stored
        self._by_id[stored.event_id] = stored
        return stored

    def get(self, source: str, source_id: str) -> AccountingDomainEvent | None:
        return self._by_source.get((source, source_id))

    def get_by_id(self, event_id: str) -> AccountingDomainEvent | None:
        return self._by_id.get(event_id)

    def append_idempotent(
        self, event: AccountingDomainEvent
    ) -> tuple[AccountingDomainEvent, bool]:
        existing = self.get(event.source, event.source_id) or self.get_by_id(event.event_id)
        if existing is not None:
            if not existing.facts_match(event):
                raise DuplicateAccountingEventError(
                    f"conflicting_accounting_event {event.source}:{event.source_id}"
                )
            return existing, False
        return self.append(event), True

    def list_in_order(self) -> list[AccountingDomainEvent]:
        return list(self._events)

    def last_sequence(self) -> int:
        return self._events[-1].sequence or 0 if self._events else 0

    def reload_from(self, events: list[AccountingDomainEvent]) -> None:
        self._events = []
        self._by_source = {}
        self._by_id = {}
        for event in events:
            self._events.append(event)
            self._by_source[(event.source, event.source_id)] = event
            self._by_id[event.event_id] = event


class SqliteAccountingEventStore:
    """Durable wrap of the in-memory accounting event contract."""

    def __init__(self, connection: sqlite3.Connection, *, commit=None) -> None:
        self._connection = connection
        self._commit = commit or (lambda: None)
        self._memory = InMemoryAccountingEventStore()
        self.ensure_schema()
        self._hydrate_memory()

    def ensure_schema(self) -> None:
        self._connection.executescript(ACCOUNTING_EVENTS_TABLE_SQL)
        self._commit()

    def _hydrate_memory(self) -> None:
        rows = self._connection.execute(
            """
            SELECT sequence, event_id, schema_version, event_type, source, source_id,
                   occurred_at, provenance, recorded_at, payload_json
            FROM accounting_domain_events
            ORDER BY sequence
            """
        ).fetchall()
        loaded: list[AccountingDomainEvent] = []
        for row in rows:
            loaded.append(_event_from_row(row))
        self._memory.reload_from(loaded)

    def reload(self) -> None:
        self._memory = InMemoryAccountingEventStore()
        self._hydrate_memory()

    def append(self, event: AccountingDomainEvent) -> AccountingDomainEvent:
        posted = self._memory.append(event)
        try:
            self._connection.execute(
                """
                INSERT INTO accounting_domain_events (
                    sequence, event_id, schema_version, event_type, source, source_id,
                    occurred_at, provenance, recorded_at, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    posted.sequence,
                    posted.event_id,
                    posted.schema_version,
                    posted.event_type.value,
                    posted.source,
                    posted.source_id,
                    posted.occurred_at.isoformat(),
                    posted.provenance.value,
                    posted.recorded_at.isoformat() if posted.recorded_at else datetime.now(UTC).isoformat(),
                    json.dumps(posted.payload.model_dump(mode="json")),
                ),
            )
            self._commit()
        except IntegrityError as exc:
            self._memory = InMemoryAccountingEventStore()
            self._hydrate_memory()
            raise DuplicateAccountingEventError(
                f"duplicate accounting event {event.source}:{event.source_id}"
            ) from exc
        return posted

    def get(self, source: str, source_id: str) -> AccountingDomainEvent | None:
        return self._memory.get(source, source_id)

    def get_by_id(self, event_id: str) -> AccountingDomainEvent | None:
        return self._memory.get_by_id(event_id)

    def append_idempotent(
        self, event: AccountingDomainEvent
    ) -> tuple[AccountingDomainEvent, bool]:
        existing = self.get(event.source, event.source_id) or self.get_by_id(event.event_id)
        if existing is not None:
            if not existing.facts_match(event):
                raise DuplicateAccountingEventError(
                    f"conflicting_accounting_event {event.source}:{event.source_id}"
                )
            return existing, False
        return self.append(event), True

    def list_in_order(self) -> list[AccountingDomainEvent]:
        return self._memory.list_in_order()

    def last_sequence(self) -> int:
        return self._memory.last_sequence()

    def save_projection_snapshot(
        self,
        *,
        projection_name: str,
        projection_version: int,
        event_schema_version: int,
        last_event_sequence: int,
        last_event_id: str | None,
        event_count: int,
        rebuilt_at: datetime,
        payload: dict,
    ) -> None:
        self._connection.execute(
            """
            INSERT INTO accounting_projection_snapshots (
                projection_name, projection_version, event_schema_version,
                last_event_sequence, last_event_id, event_count, rebuilt_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(projection_name, projection_version) DO UPDATE SET
                event_schema_version = excluded.event_schema_version,
                last_event_sequence = excluded.last_event_sequence,
                last_event_id = excluded.last_event_id,
                event_count = excluded.event_count,
                rebuilt_at = excluded.rebuilt_at,
                payload_json = excluded.payload_json
            """,
            (
                projection_name,
                projection_version,
                event_schema_version,
                last_event_sequence,
                last_event_id,
                event_count,
                rebuilt_at.isoformat(),
                json.dumps(payload),
            ),
        )
        self._commit()

    def load_projection_snapshot(
        self, projection_name: str, projection_version: int
    ) -> dict | None:
        row = self._connection.execute(
            """
            SELECT event_schema_version, last_event_sequence, last_event_id, event_count,
                   rebuilt_at, payload_json
            FROM accounting_projection_snapshots
            WHERE projection_name = ? AND projection_version = ?
            """,
            (projection_name, projection_version),
        ).fetchone()
        if row is None:
            return None
        return {
            "event_schema_version": row["event_schema_version"],
            "last_event_sequence": row["last_event_sequence"],
            "last_event_id": row["last_event_id"],
            "event_count": row["event_count"],
            "rebuilt_at": row["rebuilt_at"],
            "payload": json.loads(row["payload_json"]),
        }


def _event_from_row(row: sqlite3.Row) -> AccountingDomainEvent:
    payload = json.loads(row["payload_json"])
    return AccountingDomainEvent.model_validate(
        {
            "event_id": row["event_id"],
            "schema_version": row["schema_version"],
            "event_type": row["event_type"],
            "source": row["source"],
            "source_id": row["source_id"],
            "occurred_at": row["occurred_at"],
            "provenance": row["provenance"],
            "payload": payload,
            "sequence": row["sequence"],
            "recorded_at": row["recorded_at"],
        }
    )
