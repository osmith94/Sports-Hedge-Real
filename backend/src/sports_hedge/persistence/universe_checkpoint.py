"""Durable open-UNIVERSE generation checkpoint.

Persists coordinator resume state in the existing local paper-settings SQLite
file. This is not a second canonical identity store, not fixture-inventory
history, and not a venue write path.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from sports_hedge.config import get_settings

LOGGER = logging.getLogger(__name__)

_CHECKPOINT_ROW_ID = 1

_CREATE_CHECKPOINT_SQL = """
CREATE TABLE IF NOT EXISTS universe_generation_checkpoint (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    payload_json TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
"""


class SqliteUniverseCheckpointStore:
    """Singleton-row SQLite store for one open UNIVERSE generation."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self._database = str(database)
        self._lock = threading.RLock()
        self._shared_connection: sqlite3.Connection | None = None
        if not self._is_file_backed():
            self._shared_connection = self._open_connection(shared=True)
        with self._connect() as connection:
            self._ensure_schema(connection)

    def _is_file_backed(self) -> bool:
        lowered = self._database.lower()
        return self._database != ":memory:" and "mode=memory" not in lowered

    def _open_connection(self, *, shared: bool) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database,
            timeout=30.0,
            check_same_thread=not shared,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        if self._is_file_backed():
            connection.execute("PRAGMA journal_mode=WAL").fetchone()
        return connection

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            shared = self._shared_connection
            if shared is not None:
                try:
                    yield shared
                    shared.commit()
                except Exception:
                    shared.rollback()
                    raise
                return
            connection = self._open_connection(shared=False)
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(_CREATE_CHECKPOINT_SQL)

    def save(self, payload: dict[str, Any], *, updated_at: str) -> None:
        encoded = json.dumps(payload, separators=(",", ":"), default=str)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO universe_generation_checkpoint (id, payload_json, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    payload_json = excluded.payload_json,
                    updated_at = excluded.updated_at
                """,
                (_CHECKPOINT_ROW_ID, encoded, updated_at),
            )

    def load(self) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT payload_json
                FROM universe_generation_checkpoint
                WHERE id = ?
                """,
                (_CHECKPOINT_ROW_ID,),
            ).fetchone()
        if row is None:
            return None
        raw = row["payload_json"]
        if not isinstance(raw, str) or not raw.strip():
            LOGGER.warning("empty universe checkpoint payload; treating as missing")
            return None
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            LOGGER.warning("malformed universe checkpoint JSON; ignoring for fail-closed resume")
            return None
        if not isinstance(parsed, dict):
            LOGGER.warning("universe checkpoint payload is not an object; ignoring")
            return None
        return parsed

    def clear(self) -> None:
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM universe_generation_checkpoint WHERE id = ?",
                (_CHECKPOINT_ROW_ID,),
            )

    def close(self) -> None:
        with self._lock:
            if self._shared_connection is not None:
                self._shared_connection.close()
                self._shared_connection = None


@lru_cache
def get_universe_checkpoint_store() -> SqliteUniverseCheckpointStore:
    settings = get_settings()
    database = settings.paper_settings_db_path
    if database != ":memory:":
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteUniverseCheckpointStore(database)
