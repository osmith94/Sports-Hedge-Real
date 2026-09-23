"""Backend-owned HOT pricing / UNIVERSE discovery venue participation.

Operator selections persist across browser refresh and process restart.
Env defaults are the fallback when no operator row exists. This is not a
venue write path.

File-backed databases use a connection per operation with WAL and a busy
timeout, matching the paper-audit discipline from #156. In-memory databases
keep one connection (required for ``:memory:``) and serialize access with a
lock. ``check_same_thread=False`` is not used as a concurrency strategy.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from sports_hedge.application.lane_venues import (
    MALFORMED_VENUE_SETTINGS_WARNING,
    LaneVenueParticipation,
    participation_from_lists,
    venues_from_settings,
)
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.models import VenueName

LOGGER = logging.getLogger(__name__)


class SqliteLaneVenueSettingsStore:
    """Singleton-row SQLite store for lane-specific operator venue toggles."""

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
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS lane_venue_participation (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                hot_venues_json TEXT NOT NULL,
                universe_venues_json TEXT NOT NULL,
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

    def load(self) -> LaneVenueParticipation | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT hot_venues_json, universe_venues_json, source, updated_at
                FROM lane_venue_participation
                WHERE id = 1
                """
            ).fetchone()
        if row is None:
            return None
        return _participation_from_row(row)

    def save(
        self,
        hot: list[VenueName] | tuple[VenueName, ...],
        universe: list[VenueName] | tuple[VenueName, ...],
        *,
        source: str = "operator",
    ) -> LaneVenueParticipation:
        participation = participation_from_lists(
            hot,
            universe,
            source="operator" if source != "env_default" else "env_default",
            allow_empty=True,
        )
        now = (participation.updated_at or datetime.now(UTC)).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO lane_venue_participation (
                    id, hot_venues_json, universe_venues_json, source, updated_at
                )
                VALUES (1, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    hot_venues_json = excluded.hot_venues_json,
                    universe_venues_json = excluded.universe_venues_json,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    json.dumps([item.value for item in participation.hot]),
                    json.dumps([item.value for item in participation.universe]),
                    participation.source,
                    now,
                ),
            )
        return participation

    def close(self) -> None:
        with self._lock:
            if self._shared_connection is not None:
                self._shared_connection.close()
                self._shared_connection = None


def resolve_lane_venue_participation(
    store: SqliteLaneVenueSettingsStore,
    settings: Settings | None = None,
) -> LaneVenueParticipation:
    saved = store.load()
    if saved is not None:
        return saved.with_warnings()
    resolved = settings or get_settings()
    return participation_from_lists(
        venues_from_settings(resolved.paper_hot_venues),
        venues_from_settings(resolved.paper_universe_venues),
        source="env_default",
        allow_empty=False,
    )


@lru_cache
def get_lane_venue_settings_store() -> SqliteLaneVenueSettingsStore:
    settings = get_settings()
    database = settings.paper_settings_db_path
    if database != ":memory:":
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteLaneVenueSettingsStore(database)


def _participation_from_row(row: sqlite3.Row) -> LaneVenueParticipation:
    hot_read = _decode_venue_payload(row["hot_venues_json"])
    universe_read = _decode_venue_payload(row["universe_venues_json"])
    malformed = hot_read[1] or universe_read[1]
    if malformed:
        LOGGER.warning("malformed lane venue participation row; fail-closed empty venue sets")
    try:
        updated = datetime.fromisoformat(str(row["updated_at"]))
    except ValueError:
        updated = datetime.now(UTC)
        malformed = True
    source_raw = str(row["source"] or "")
    source = source_raw if source_raw in {"operator", "env_default"} else "operator"
    return participation_from_lists(
        hot_read[0],
        universe_read[0],
        source=source,
        updated_at=updated,
        allow_empty=True,
        config_diagnostic=MALFORMED_VENUE_SETTINGS_WARNING if malformed else None,
    )


def _decode_venue_payload(payload: Any) -> tuple[list[str], bool]:
    """Return (names, malformed). Empty list is valid operator all-off.

    Malformed/non-list JSON fails closed to empty rather than silently
    re-enabling Matchbook/Polymarket/Kalshi.
    """

    if not isinstance(payload, str):
        return [], True
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return [], True
    if not isinstance(parsed, list):
        return [], True
    return [str(item) for item in parsed], False
