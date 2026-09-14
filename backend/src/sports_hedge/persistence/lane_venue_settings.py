"""Backend-owned Fast Scan / Full Sweep venue participation.

Operator selections persist across browser refresh and process restart.
Env defaults are the fallback when no operator row exists. This is not a
venue write path.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from sports_hedge.application.lane_venues import (
    LaneVenueParticipation,
    default_operator_venues,
    participation_from_lists,
    venues_from_settings,
)
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.models import VenueName


class SqliteLaneVenueSettingsStore:
    """Singleton-row SQLite store for lane-specific operator venue toggles."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
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
        self._connection.commit()

    def load(self) -> LaneVenueParticipation | None:
        row = self._connection.execute(
            """
            SELECT hot_venues_json, universe_venues_json, source, updated_at
            FROM lane_venue_participation
            WHERE id = 1
            """
        ).fetchone()
        if row is None:
            return None
        updated = datetime.fromisoformat(row["updated_at"])
        source = row["source"] if row["source"] in {"operator", "env_default"} else "operator"
        return participation_from_lists(
            _decode_venues(row["hot_venues_json"]),
            _decode_venues(row["universe_venues_json"]),
            source=source,
            updated_at=updated,
            allow_empty=True,
        )

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
        self._connection.execute(
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
        self._connection.commit()
        return participation

    def close(self) -> None:
        self._connection.close()


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


def _decode_venues(payload: str) -> list[str]:
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return [item.value for item in default_operator_venues()]
    if not isinstance(parsed, list):
        return [item.value for item in default_operator_venues()]
    return [str(item) for item in parsed]
