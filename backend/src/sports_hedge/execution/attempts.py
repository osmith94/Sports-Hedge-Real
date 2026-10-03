"""Durable live-package attempts. A reserved row blocks a second submission.

Matchbook has no documented submit idempotency key. The attempt row is written
before any venue call and is the only restart-safe guard. It stores order
facts, never credentials.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from typing import Any

_CREATE = """
CREATE TABLE IF NOT EXISTS live_execution_attempts (
    package_id TEXT PRIMARY KEY,
    trade_id TEXT NOT NULL,
    tranche_id TEXT NOT NULL,
    opportunity_id TEXT NOT NULL,
    snapshot_ref TEXT,
    snapshot_json TEXT,
    status TEXT NOT NULL,
    outcome TEXT,
    detail TEXT,
    orders_json TEXT NOT NULL,
    recovery_json TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
)
"""

_SECRET_MARKERS = (
    "password",
    "secret",
    "token",
    "private_key",
    "api_key",
    "authorization",
    "credential",
    "session-token",
    "session_token",
)


def scrub_secrets(value: Any) -> Any:
    """Drop credential-like keys. Nested dicts and lists are walked."""

    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            if _secret_key(str(key)):
                continue
            cleaned[str(key)] = scrub_secrets(item)
        return cleaned
    if isinstance(value, list):
        return [scrub_secrets(item) for item in value]
    return value


def _secret_key(key: str) -> bool:
    folded = key.casefold().replace(" ", "")
    return any(marker in folded for marker in _SECRET_MARKERS)


class LiveExecutionAttemptStore:
    """SQLite or in-memory attempt log with the same reserve-once contract."""

    def __init__(self, connection: sqlite3.Connection | None = None) -> None:
        self._connection = connection
        self._memory: dict[str, dict[str, Any]] = {}
        if connection is not None:
            connection.execute(_CREATE)
            columns = {row[1] for row in connection.execute("PRAGMA table_info(live_execution_attempts)")}
            if "recovery_json" not in columns:
                connection.execute("ALTER TABLE live_execution_attempts ADD COLUMN recovery_json TEXT")

    def get(self, package_id: str) -> dict[str, Any] | None:
        if self._connection is None:
            row = self._memory.get(package_id)
            return None if row is None else dict(row)
        found = self._connection.execute(
            "SELECT * FROM live_execution_attempts WHERE package_id = ?",
            (package_id,),
        ).fetchone()
        if found is None:
            return None
        return _row_to_dict(found)

    def list_all(self) -> list[dict[str, Any]]:
        if self._connection is None:
            return [dict(row) for row in self._memory.values()]
        rows = self._connection.execute(
            "SELECT * FROM live_execution_attempts ORDER BY created_at, package_id"
        ).fetchall()
        return [_row_to_dict(row) for row in rows]

    def reserve(self, record: dict[str, Any]) -> bool:
        """Insert the pre-write reservation. False means the package already exists."""

        package_id = str(record["package_id"])
        if self.get(package_id) is not None:
            return False
        stored = {
            "package_id": package_id,
            "trade_id": str(record["trade_id"]),
            "tranche_id": str(record["tranche_id"]),
            "opportunity_id": str(record["opportunity_id"]),
            "snapshot_ref": record.get("snapshot_ref"),
            "snapshot_json": record.get("snapshot_json"),
            "status": "reserved",
            "outcome": None,
            "detail": record.get("detail"),
            "orders_json": "[]",
            "recovery_json": record.get("recovery_json"),
            "created_at": _iso(record["created_at"]),
            "updated_at": _iso(record["created_at"]),
        }
        if self._connection is None:
            self._memory[package_id] = stored
            return True
        try:
            self._connection.execute(
                """
                INSERT INTO live_execution_attempts (
                    package_id, trade_id, tranche_id, opportunity_id, snapshot_ref,
                    snapshot_json, status, outcome, detail, orders_json, recovery_json,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    stored["package_id"],
                    stored["trade_id"],
                    stored["tranche_id"],
                    stored["opportunity_id"],
                    stored["snapshot_ref"],
                    stored["snapshot_json"],
                    stored["status"],
                    stored["outcome"],
                    stored["detail"],
                    stored["orders_json"],
                    stored["recovery_json"],
                    stored["created_at"],
                    stored["updated_at"],
                ),
            )
        except sqlite3.IntegrityError:
            return False
        return True

    def complete(
        self,
        package_id: str,
        *,
        outcome: str | None,
        detail: str | None,
        orders: list[dict[str, Any]],
        updated_at: datetime,
    ) -> None:
        payload = json.dumps(scrub_secrets(orders), default=str)
        if self._connection is None:
            current = self._memory.get(package_id)
            if current is None:
                return
            current["status"] = "completed"
            current["outcome"] = outcome
            current["detail"] = detail
            current["orders_json"] = payload
            current["updated_at"] = updated_at.isoformat()
            return
        self._connection.execute(
            """
            UPDATE live_execution_attempts
            SET status = ?, outcome = ?, detail = ?, orders_json = ?, updated_at = ?
            WHERE package_id = ?
            """,
            ("completed", outcome, detail, payload, updated_at.isoformat(), package_id),
        )


def _iso(value: datetime | str) -> str:
    if isinstance(value, datetime):
        return value.isoformat()
    return str(value)


def _row_to_dict(row: sqlite3.Row) -> dict[str, Any]:
    return dict(zip(row.keys(), tuple(row), strict=True))
