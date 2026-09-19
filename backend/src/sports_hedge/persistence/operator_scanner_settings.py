"""Backend-owned Paper Scanner operator settings.

One singleton SQLite row is the operator override for:

- Min Net Arb / ``minimum_net_edge``
- Max Risk / ``maximum_execution_risk`` (scan/watchlist threshold only)
- HOT cadence seconds
- operator Stop / Resume pause flag

Environment/config values remain the defaults when no operator settings
override exists. This store never mutates ``.env``. Update, Stop and Resume
are persistence/control seams only: they must not scan, discover, or call
providers.

Backend-restart semantics (safety-first): a persisted operator Stop remains
stopped across process restart until an explicit Resume. Catalogue, fixture
state, paper trades, treasury and diagnostics are not cleared by Stop/Resume.
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from sports_hedge.config import Settings, get_settings

LOGGER = logging.getLogger(__name__)

HOT_CADENCE_MIN_SECONDS = 15
HOT_CADENCE_MAX_SECONDS = 60
OPERATOR_SCANNER_RESTART_SEMANTICS = "remain_stopped_until_resume"


class OperatorScannerSettings(BaseModel):
    """Authoritative operator scanner settings as returned by the backend."""

    min_net_edge: Decimal = Field(ge=0, lt=1)
    max_execution_risk: int = Field(ge=0, le=100)
    hot_cadence_seconds: int = Field(ge=HOT_CADENCE_MIN_SECONDS, le=HOT_CADENCE_MAX_SECONDS)
    scanner_stopped: bool = False
    source: Literal["operator", "env_default"] = "env_default"
    updated_at: datetime | None = None
    restart_semantics: str = OPERATOR_SCANNER_RESTART_SEMANTICS


class OperatorScannerSettingsUpdate(BaseModel):
    min_net_edge: Decimal = Field(ge=0, lt=1)
    max_execution_risk: int = Field(ge=0, le=100)
    hot_cadence_seconds: int = Field(ge=HOT_CADENCE_MIN_SECONDS, le=HOT_CADENCE_MAX_SECONDS)


def clamp_hot_cadence_seconds(value: int) -> int:
    return min(HOT_CADENCE_MAX_SECONDS, max(HOT_CADENCE_MIN_SECONDS, int(value)))


def env_operator_scanner_settings(
    settings: Settings | None = None,
    *,
    scanner_stopped: bool = False,
    updated_at: datetime | None = None,
) -> OperatorScannerSettings:
    resolved = settings or get_settings()
    return OperatorScannerSettings(
        min_net_edge=Decimal(str(resolved.min_net_edge)),
        max_execution_risk=int(resolved.max_execution_risk),
        hot_cadence_seconds=clamp_hot_cadence_seconds(
            resolved.paper_live_refresh_hot_interval_seconds
        ),
        scanner_stopped=scanner_stopped,
        source="env_default",
        updated_at=updated_at,
        restart_semantics=OPERATOR_SCANNER_RESTART_SEMANTICS,
    )


class SqliteOperatorScannerSettingsStore:
    """Singleton-row SQLite store for Paper Scanner operator settings."""

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
            CREATE TABLE IF NOT EXISTS operator_scanner_settings (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                min_net_edge TEXT NOT NULL,
                max_execution_risk INTEGER NOT NULL,
                hot_cadence_seconds INTEGER NOT NULL,
                scanner_stopped INTEGER NOT NULL,
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

    def load(self) -> OperatorScannerSettings | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT min_net_edge, max_execution_risk, hot_cadence_seconds,
                       scanner_stopped, source, updated_at
                FROM operator_scanner_settings
                WHERE id = 1
                """
            ).fetchone()
        if row is None:
            return None
        return _settings_from_row(row)

    def save_settings(
        self,
        *,
        min_net_edge: Decimal,
        max_execution_risk: int,
        hot_cadence_seconds: int,
        scanner_stopped: bool | None = None,
    ) -> OperatorScannerSettings:
        current = self.load()
        stopped = current.scanner_stopped if current is not None and scanner_stopped is None else bool(
            scanner_stopped if scanner_stopped is not None else False
        )
        payload = OperatorScannerSettings(
            min_net_edge=min_net_edge,
            max_execution_risk=int(max_execution_risk),
            hot_cadence_seconds=clamp_hot_cadence_seconds(hot_cadence_seconds),
            scanner_stopped=stopped,
            source="operator",
            updated_at=datetime.now(UTC),
            restart_semantics=OPERATOR_SCANNER_RESTART_SEMANTICS,
        )
        self._upsert(payload)
        loaded = self.load()
        assert loaded is not None
        return loaded

    def save_stopped(
        self,
        stopped: bool,
        *,
        settings: Settings | None = None,
    ) -> OperatorScannerSettings:
        current = self.load()
        env = env_operator_scanner_settings(settings, scanner_stopped=stopped)
        if current is None or current.source != "operator":
            payload = env.model_copy(
                update={
                    "scanner_stopped": bool(stopped),
                    "updated_at": datetime.now(UTC),
                }
            )
        else:
            payload = current.model_copy(
                update={
                    "scanner_stopped": bool(stopped),
                    "updated_at": datetime.now(UTC),
                }
            )
        self._upsert(payload)
        loaded = self.load()
        assert loaded is not None
        return loaded

    def _upsert(self, payload: OperatorScannerSettings) -> None:
        now = (payload.updated_at or datetime.now(UTC)).isoformat()
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO operator_scanner_settings (
                    id, min_net_edge, max_execution_risk, hot_cadence_seconds,
                    scanner_stopped, source, updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    min_net_edge = excluded.min_net_edge,
                    max_execution_risk = excluded.max_execution_risk,
                    hot_cadence_seconds = excluded.hot_cadence_seconds,
                    scanner_stopped = excluded.scanner_stopped,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    str(payload.min_net_edge),
                    int(payload.max_execution_risk),
                    int(payload.hot_cadence_seconds),
                    1 if payload.scanner_stopped else 0,
                    payload.source,
                    now,
                ),
            )

    def close(self) -> None:
        with self._lock:
            if self._shared_connection is not None:
                self._shared_connection.close()
                self._shared_connection = None


def resolve_operator_scanner_settings(
    store: SqliteOperatorScannerSettingsStore,
    settings: Settings | None = None,
) -> OperatorScannerSettings:
    resolved = settings or get_settings()
    saved = store.load()
    if saved is None:
        return env_operator_scanner_settings(resolved)
    if saved.source != "operator":
        return env_operator_scanner_settings(
            resolved,
            scanner_stopped=saved.scanner_stopped,
            updated_at=saved.updated_at,
        )
    return saved.model_copy(
        update={"restart_semantics": OPERATOR_SCANNER_RESTART_SEMANTICS}
    )


_RUNTIME_STORE: SqliteOperatorScannerSettingsStore | None = None


def bind_runtime_operator_scanner_settings_store(
    store: SqliteOperatorScannerSettingsStore | None,
) -> None:
    global _RUNTIME_STORE
    _RUNTIME_STORE = store


def runtime_operator_scanner_settings_store() -> SqliteOperatorScannerSettingsStore:
    if _RUNTIME_STORE is not None:
        return _RUNTIME_STORE
    return get_operator_scanner_settings_store()


def effective_operator_scanner_settings(
    settings: Settings | None = None,
) -> OperatorScannerSettings:
    return resolve_operator_scanner_settings(
        runtime_operator_scanner_settings_store(),
        settings,
    )


@lru_cache
def get_operator_scanner_settings_store() -> SqliteOperatorScannerSettingsStore:
    settings = get_settings()
    database = settings.paper_settings_db_path
    if database != ":memory:":
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteOperatorScannerSettingsStore(database)


def _settings_from_row(row: sqlite3.Row) -> OperatorScannerSettings:
    source_raw = str(row["source"] or "")
    source: Literal["operator", "env_default"] = (
        "operator" if source_raw == "operator" else "env_default"
    )
    try:
        min_net_edge = Decimal(str(row["min_net_edge"]))
        if min_net_edge < 0 or min_net_edge >= 1:
            raise InvalidOperation
    except (InvalidOperation, ArithmeticError, TypeError, ValueError):
        LOGGER.warning("malformed operator scanner min_net_edge; using env default")
        min_net_edge = Decimal(str(get_settings().min_net_edge))
        source = "env_default"
    try:
        max_execution_risk = int(row["max_execution_risk"])
        if not 0 <= max_execution_risk <= 100:
            raise ValueError
    except (TypeError, ValueError):
        LOGGER.warning("malformed operator scanner max_execution_risk; using env default")
        max_execution_risk = int(get_settings().max_execution_risk)
        source = "env_default"
    try:
        hot_cadence_seconds = clamp_hot_cadence_seconds(int(row["hot_cadence_seconds"]))
    except (TypeError, ValueError):
        LOGGER.warning("malformed operator scanner hot_cadence_seconds; using env default")
        hot_cadence_seconds = clamp_hot_cadence_seconds(
            get_settings().paper_live_refresh_hot_interval_seconds
        )
        source = "env_default"
    try:
        updated = datetime.fromisoformat(str(row["updated_at"]))
    except ValueError:
        updated = datetime.now(UTC)
        source = "env_default"
    return OperatorScannerSettings(
        min_net_edge=min_net_edge,
        max_execution_risk=max_execution_risk,
        hot_cadence_seconds=hot_cadence_seconds,
        scanner_stopped=bool(int(row["scanner_stopped"] or 0)),
        source=source,
        updated_at=updated,
        restart_semantics=OPERATOR_SCANNER_RESTART_SEMANTICS,
    )
