"""Backend-authoritative operator UNIVERSE competition scope.

One singleton SQLite row stores the operator-selected canonical football
competition codes and a monotonic scope version. Environment defaults seed a
clean install; a persisted row wins after the operator confirms.

This store never mutates ``.env`` and never calls providers.
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
from typing import Literal

from pydantic import BaseModel, Field

from sports_hedge.application.target_competitions import (
    OPERATOR_COMPETITION_REGISTRY_VERSION,
    OPERATOR_UNIVERSE_SPORT,
    default_operator_competition_code_values,
    kalshi_series_tickers_for_codes,
    normalize_selected_competition_codes,
    operator_competition_catalog,
    polymarket_series_ids_for_codes,
)
from sports_hedge.config import get_settings

LOGGER = logging.getLogger(__name__)

UNIVERSE_SCOPE_EMPTY_SELECTION = "universe_scope_empty_selection"
UNIVERSE_MANUAL_IDLE = "idle"
UNIVERSE_MANUAL_RUNNING = "running"
UNIVERSE_MANUAL_PENDING = "pending"


class OperatorCompetitionOption(BaseModel):
    code: str
    display_name: str
    selector_label: str
    group_id: str
    group_label: str
    default_selected: bool = False
    selectable: bool = True
    unavailable_reason: str | None = None


class OperatorUniverseScope(BaseModel):
    sport: str = OPERATOR_UNIVERSE_SPORT
    selected_competition_codes: list[str] = Field(
        default_factory=lambda: list(default_operator_competition_code_values())
    )
    selected_count: int = 0
    scope_version: int = Field(default=0, ge=0)
    registry_version: int = OPERATOR_COMPETITION_REGISTRY_VERSION
    source: Literal["operator", "env_default"] = "env_default"
    updated_at: datetime | None = None
    needs_first_run_confirmation: bool = True
    new_competitions_available: bool = False
    catalog: list[OperatorCompetitionOption] = Field(default_factory=list)
    generation_scope_version: int | None = None
    generation_selected_competition_codes: list[str] = Field(default_factory=list)
    manual_universe_state: Literal["idle", "running", "pending"] = UNIVERSE_MANUAL_IDLE
    manual_background_busy: bool = False

    def selected_set(self) -> frozenset[str]:
        return frozenset(self.selected_competition_codes)


class OperatorUniverseScopeUpdate(BaseModel):
    selected_competition_codes: list[str]
    sport: str = OPERATOR_UNIVERSE_SPORT
    run_universe_now: bool = False


def default_universe_scope(
    *,
    needs_first_run_confirmation: bool = True,
    updated_at: datetime | None = None,
) -> OperatorUniverseScope:
    codes = list(default_operator_competition_code_values())
    return OperatorUniverseScope(
        sport=OPERATOR_UNIVERSE_SPORT,
        selected_competition_codes=codes,
        selected_count=len(codes),
        scope_version=0,
        registry_version=OPERATOR_COMPETITION_REGISTRY_VERSION,
        source="env_default",
        updated_at=updated_at,
        needs_first_run_confirmation=needs_first_run_confirmation,
        new_competitions_available=False,
        catalog=_catalog_models(),
    )


def discovery_filters_for_codes(codes: list[str] | tuple[str, ...] | None) -> dict[str, list[str]]:
    """Venue discovery filters derived from canonical codes. No raw UI tickers."""

    selected = list(codes or default_operator_competition_code_values())
    return {
        "kalshi_series_tickers": kalshi_series_tickers_for_codes(selected),
        "polymarket_series_ids": polymarket_series_ids_for_codes(selected),
    }


def _catalog_models() -> list[OperatorCompetitionOption]:
    return [OperatorCompetitionOption.model_validate(row) for row in operator_competition_catalog()]


class SqliteOperatorUniverseScopeStore:
    """Singleton-row SQLite store for operator UNIVERSE competition scope."""

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
            CREATE TABLE IF NOT EXISTS operator_universe_scope (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                sport TEXT NOT NULL,
                selected_codes TEXT NOT NULL,
                scope_version INTEGER NOT NULL,
                registry_version INTEGER NOT NULL,
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

    def load(self) -> OperatorUniverseScope | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
                FROM operator_universe_scope
                WHERE id = 1
                """
            ).fetchone()
        if row is None:
            return None
        return _scope_from_row(row)

    def save_scope(
        self,
        selected_competition_codes: list[str] | tuple[str, ...],
        *,
        sport: str = OPERATOR_UNIVERSE_SPORT,
    ) -> OperatorUniverseScope:
        codes = list(normalize_selected_competition_codes(selected_competition_codes))
        current = self.load()
        previous = list(current.selected_competition_codes) if current is not None else None
        scope_version = int(current.scope_version) if current is not None else 0
        if previous != codes:
            scope_version += 1
        payload = OperatorUniverseScope(
            sport=str(sport or OPERATOR_UNIVERSE_SPORT).strip() or OPERATOR_UNIVERSE_SPORT,
            selected_competition_codes=codes,
            selected_count=len(codes),
            scope_version=scope_version,
            registry_version=OPERATOR_COMPETITION_REGISTRY_VERSION,
            source="operator",
            updated_at=datetime.now(UTC),
            needs_first_run_confirmation=False,
            new_competitions_available=False,
            catalog=_catalog_models(),
        )
        self._upsert(payload)
        loaded = self.load()
        assert loaded is not None
        return loaded

    def _upsert(self, payload: OperatorUniverseScope) -> None:
        now = (payload.updated_at or datetime.now(UTC)).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO operator_universe_scope (
                    id, sport, selected_codes, scope_version, registry_version,
                    source, updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    sport = excluded.sport,
                    selected_codes = excluded.selected_codes,
                    scope_version = excluded.scope_version,
                    registry_version = excluded.registry_version,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    payload.sport,
                    json.dumps(list(payload.selected_competition_codes)),
                    int(payload.scope_version),
                    int(payload.registry_version),
                    payload.source,
                    now,
                ),
            )

    def close(self) -> None:
        with self._lock:
            if self._shared_connection is not None:
                self._shared_connection.close()
                self._shared_connection = None


def resolve_operator_universe_scope(
    store: SqliteOperatorUniverseScopeStore,
) -> OperatorUniverseScope:
    saved = store.load()
    if saved is None:
        return default_universe_scope(needs_first_run_confirmation=True)
    return saved


_RUNTIME_STORE: SqliteOperatorUniverseScopeStore | None = None


def bind_runtime_operator_universe_scope_store(
    store: SqliteOperatorUniverseScopeStore | None,
) -> None:
    global _RUNTIME_STORE
    _RUNTIME_STORE = store


def runtime_operator_universe_scope_store() -> SqliteOperatorUniverseScopeStore:
    if _RUNTIME_STORE is not None:
        return _RUNTIME_STORE
    return get_operator_universe_scope_store()


def effective_operator_universe_scope() -> OperatorUniverseScope:
    return resolve_operator_universe_scope(runtime_operator_universe_scope_store())


@lru_cache
def get_operator_universe_scope_store() -> SqliteOperatorUniverseScopeStore:
    settings = get_settings()
    database = settings.paper_settings_db_path
    if database != ":memory:":
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteOperatorUniverseScopeStore(database)


def _scope_from_row(row: sqlite3.Row) -> OperatorUniverseScope:
    try:
        codes = json.loads(str(row["selected_codes"] or "[]"))
        if not isinstance(codes, list):
            raise ValueError
        normalized = list(normalize_selected_competition_codes(codes))
    except (ValueError, TypeError, json.JSONDecodeError):
        LOGGER.warning("malformed operator universe scope codes; using defaults")
        return default_universe_scope(needs_first_run_confirmation=True)
    try:
        scope_version = max(0, int(row["scope_version"] or 0))
    except (TypeError, ValueError):
        scope_version = 0
    try:
        registry_version = int(row["registry_version"] or 0)
    except (TypeError, ValueError):
        registry_version = 0
    try:
        updated = datetime.fromisoformat(str(row["updated_at"]))
    except ValueError:
        updated = datetime.now(UTC)
    source_raw = str(row["source"] or "")
    source: Literal["operator", "env_default"] = (
        "operator" if source_raw == "operator" else "env_default"
    )
    return OperatorUniverseScope(
        sport=str(row["sport"] or OPERATOR_UNIVERSE_SPORT) or OPERATOR_UNIVERSE_SPORT,
        selected_competition_codes=normalized,
        selected_count=len(normalized),
        scope_version=scope_version,
        registry_version=OPERATOR_COMPETITION_REGISTRY_VERSION,
        source=source,
        updated_at=updated,
        needs_first_run_confirmation=False,
        new_competitions_available=registry_version < OPERATOR_COMPETITION_REGISTRY_VERSION,
        catalog=_catalog_models(),
    )
