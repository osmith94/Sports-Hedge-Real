"""Backend-owned Paper Scanner operator settings.

One singleton SQLite row is the operator override for:

- Min Net Arb / ``minimum_net_edge`` (FIXTURE_MATCH only)
- Outright Min Net Arb / ``outright_min_net_edge`` (COMPETITION_SEASON only;
  None is unconfigured and fails closed; never falls back to fixture)
- Max Risk / ``maximum_execution_risk`` (scan/watchlist threshold only)
- HOT target refresh (desired time between completed HOT passes)
- UNIVERSE discovery refresh (fresh generation restart interval)
- Max allocated per trade (GBP) — allocator per-opportunity cap authority
- operator Stop / Resume pause flag
- Pause scheduled UNIVERSE scans (periodic fresh-generation timer only)

Environment/config values remain the defaults when no operator settings
override exists. This store never mutates ``.env``. Update, Stop, Resume and
UNIVERSE schedule pause/resume are persistence/control seams only: they must
not scan, discover, or call providers. UNIVERSE discovery refresh here is the
post-completion fresh-generation interval only — not radar TTL, intra-generation
worker cooldown, or budget. Pausing scheduled UNIVERSE scans does not
substitute a giant discovery refresh. BACKGROUND coverage is continuous.
HOT waits only for ``hot_target_refresh_seconds`` between completed passes.
ACTIVE TRADE stays on its own exact-ID cadence. Legacy scan-interval and
reprice-after columns remain for compatibility and do not schedule work.
Legacy ``*_cadence_seconds`` columns migrate to reprice-after or discovery
refresh when the new column is absent. A missing HOT target migrates from
the saved HOT reprice-after value.

Backend-restart semantics (safety-first): a persisted operator Stop remains
stopped across process restart until an explicit Resume. A persisted UNIVERSE
schedule pause survives restart; startup still performs one fresh generation
after saved scope hydration, then remains paused. Catalogue, fixture state,
paper trades, treasury and diagnostics are not cleared by Stop/Resume or
UNIVERSE schedule pause.
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
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

from sports_hedge.config import Settings, get_settings

LOGGER = logging.getLogger(__name__)

_UNSET: Any = object()

HOT_CADENCE_MIN_SECONDS = 15
HOT_CADENCE_MAX_SECONDS = 60
DEFAULT_HOT_REPRICE_AFTER_SECONDS = 30
HOT_SCAN_INTERVAL_MIN_SECONDS = 5
HOT_SCAN_INTERVAL_MAX_SECONDS = 60
DEFAULT_HOT_SCAN_INTERVAL_SECONDS = 10
HOT_TARGET_REFRESH_MIN_SECONDS = 5
HOT_TARGET_REFRESH_MAX_SECONDS = 60
DEFAULT_HOT_TARGET_REFRESH_SECONDS = 10
BACKGROUND_CADENCE_MIN_SECONDS = 60
BACKGROUND_CADENCE_MAX_SECONDS = 600
DEFAULT_BACKGROUND_REPRICE_AFTER_SECONDS = 600
DEFAULT_BACKGROUND_CADENCE_SECONDS = DEFAULT_BACKGROUND_REPRICE_AFTER_SECONDS
BACKGROUND_SCAN_INTERVAL_MIN_SECONDS = 5
BACKGROUND_SCAN_INTERVAL_MAX_SECONDS = 60
DEFAULT_BACKGROUND_SCAN_INTERVAL_SECONDS = 10
UNIVERSE_CADENCE_MIN_SECONDS = 60
UNIVERSE_CADENCE_MAX_SECONDS = 3600
DEFAULT_UNIVERSE_DISCOVERY_REFRESH_SECONDS = 3600
DEFAULT_UNIVERSE_CADENCE_SECONDS = DEFAULT_UNIVERSE_DISCOVERY_REFRESH_SECONDS
MAX_ALLOCATED_PER_TRADE_MIN_GBP = Decimal("1")
MAX_ALLOCATED_PER_TRADE_MAX_GBP = Decimal("1000000")
DEFAULT_MAX_ALLOCATED_PER_TRADE_GBP = Decimal("1000")
OPERATOR_SCANNER_RESTART_SEMANTICS = "remain_stopped_until_resume"
SCANNER_STOPPED_BY_OPERATOR = "scanner_stopped_by_operator"
UNIVERSE_SCHEDULED_PAUSED = "universe_scheduled_paused"


class OperatorScannerSettings(BaseModel):
    """Authoritative operator scanner settings as returned by the backend."""

    min_net_edge: Decimal = Field(ge=0, lt=1)
    outright_min_net_edge: Decimal | None = Field(default=None, ge=0, lt=1)
    max_execution_risk: int = Field(ge=0, le=100)
    hot_target_refresh_seconds: int = Field(
        default=DEFAULT_HOT_TARGET_REFRESH_SECONDS,
        ge=HOT_TARGET_REFRESH_MIN_SECONDS,
        le=HOT_TARGET_REFRESH_MAX_SECONDS,
    )
    hot_scan_interval_seconds: int = Field(
        default=DEFAULT_HOT_SCAN_INTERVAL_SECONDS,
        ge=HOT_SCAN_INTERVAL_MIN_SECONDS,
        le=HOT_SCAN_INTERVAL_MAX_SECONDS,
    )
    hot_reprice_after_seconds: int = Field(
        default=DEFAULT_HOT_REPRICE_AFTER_SECONDS,
        ge=HOT_CADENCE_MIN_SECONDS,
        le=HOT_CADENCE_MAX_SECONDS,
    )
    background_scan_interval_seconds: int = Field(
        default=DEFAULT_BACKGROUND_SCAN_INTERVAL_SECONDS,
        ge=BACKGROUND_SCAN_INTERVAL_MIN_SECONDS,
        le=BACKGROUND_SCAN_INTERVAL_MAX_SECONDS,
    )
    background_reprice_after_seconds: int = Field(
        default=DEFAULT_BACKGROUND_REPRICE_AFTER_SECONDS,
        ge=BACKGROUND_CADENCE_MIN_SECONDS,
        le=BACKGROUND_CADENCE_MAX_SECONDS,
    )
    universe_discovery_refresh_seconds: int = Field(
        default=DEFAULT_UNIVERSE_DISCOVERY_REFRESH_SECONDS,
        ge=UNIVERSE_CADENCE_MIN_SECONDS,
        le=UNIVERSE_CADENCE_MAX_SECONDS,
    )
    max_allocated_per_trade_gbp: Decimal = Field(
        default=DEFAULT_MAX_ALLOCATED_PER_TRADE_GBP,
        ge=MAX_ALLOCATED_PER_TRADE_MIN_GBP,
        le=MAX_ALLOCATED_PER_TRADE_MAX_GBP,
    )
    scanner_stopped: bool = False
    universe_scans_paused: bool = False
    source: Literal["operator", "env_default"] = "env_default"
    updated_at: datetime | None = None
    restart_semantics: str = OPERATOR_SCANNER_RESTART_SEMANTICS

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_cadence_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        _copy_legacy_when_new_absent(payload, "hot_reprice_after_seconds", "hot_cadence_seconds")
        _copy_legacy_when_new_absent(
            payload, "background_reprice_after_seconds", "background_cadence_seconds"
        )
        _copy_legacy_when_new_absent(
            payload, "universe_discovery_refresh_seconds", "universe_cadence_seconds"
        )
        return payload

    @computed_field
    @property
    def hot_cadence_seconds(self) -> int:
        """Compatibility alias for the HOT reprice-after age."""

        return self.hot_reprice_after_seconds

    @computed_field
    @property
    def background_cadence_seconds(self) -> int:
        """Compatibility alias for the BACKGROUND reprice-after age."""

        return self.background_reprice_after_seconds

    @computed_field
    @property
    def universe_cadence_seconds(self) -> int:
        """Compatibility alias for the UNIVERSE discovery refresh."""

        return self.universe_discovery_refresh_seconds


class OperatorScannerSettingsUpdate(BaseModel):
    min_net_edge: Decimal = Field(ge=0, lt=1)
    outright_min_net_edge: Decimal | None = Field(default=None, ge=0, lt=1)
    max_execution_risk: int = Field(ge=0, le=100)
    hot_target_refresh_seconds: int | None = Field(
        default=None,
        ge=HOT_TARGET_REFRESH_MIN_SECONDS,
        le=HOT_TARGET_REFRESH_MAX_SECONDS,
    )
    hot_scan_interval_seconds: int | None = Field(
        default=None,
        ge=HOT_SCAN_INTERVAL_MIN_SECONDS,
        le=HOT_SCAN_INTERVAL_MAX_SECONDS,
    )
    hot_reprice_after_seconds: int | None = Field(
        default=None,
        ge=HOT_CADENCE_MIN_SECONDS,
        le=HOT_CADENCE_MAX_SECONDS,
    )
    hot_cadence_seconds: int | None = Field(
        default=None,
        ge=HOT_CADENCE_MIN_SECONDS,
        le=HOT_CADENCE_MAX_SECONDS,
    )
    background_scan_interval_seconds: int | None = Field(
        default=None,
        ge=BACKGROUND_SCAN_INTERVAL_MIN_SECONDS,
        le=BACKGROUND_SCAN_INTERVAL_MAX_SECONDS,
    )
    background_reprice_after_seconds: int | None = Field(
        default=None,
        ge=BACKGROUND_CADENCE_MIN_SECONDS,
        le=BACKGROUND_CADENCE_MAX_SECONDS,
    )
    background_cadence_seconds: int | None = Field(
        default=None,
        ge=BACKGROUND_CADENCE_MIN_SECONDS,
        le=BACKGROUND_CADENCE_MAX_SECONDS,
    )
    universe_discovery_refresh_seconds: int | None = Field(
        default=None,
        ge=UNIVERSE_CADENCE_MIN_SECONDS,
        le=UNIVERSE_CADENCE_MAX_SECONDS,
    )
    universe_cadence_seconds: int | None = Field(
        default=None,
        ge=UNIVERSE_CADENCE_MIN_SECONDS,
        le=UNIVERSE_CADENCE_MAX_SECONDS,
    )
    max_allocated_per_trade_gbp: Decimal | None = Field(
        default=None,
        ge=MAX_ALLOCATED_PER_TRADE_MIN_GBP,
        le=MAX_ALLOCATED_PER_TRADE_MAX_GBP,
    )

    @field_validator("outright_min_net_edge", mode="before")
    @classmethod
    def blank_outright_is_unconfigured(cls, value: Any) -> Any:
        if value == "":
            return None
        return value

    @model_validator(mode="before")
    @classmethod
    def migrate_legacy_cadence_fields(cls, data: Any) -> Any:
        if not isinstance(data, dict):
            return data
        payload = dict(data)
        _copy_legacy_when_new_absent(payload, "hot_reprice_after_seconds", "hot_cadence_seconds")
        _copy_legacy_when_new_absent(
            payload, "background_reprice_after_seconds", "background_cadence_seconds"
        )
        _copy_legacy_when_new_absent(
            payload, "universe_discovery_refresh_seconds", "universe_cadence_seconds"
        )
        return payload


def _copy_legacy_when_new_absent(payload: dict[str, Any], new_key: str, old_key: str) -> None:
    """Map a persisted/API cadence name onto the new field only when the new field is absent."""

    if payload.get(new_key) is None and payload.get(old_key) is not None:
        payload[new_key] = payload[old_key]


def clamp_hot_cadence_seconds(value: int) -> int:
    return min(HOT_CADENCE_MAX_SECONDS, max(HOT_CADENCE_MIN_SECONDS, int(value)))


def clamp_hot_target_refresh_seconds(value: int) -> int:
    return min(
        HOT_TARGET_REFRESH_MAX_SECONDS,
        max(HOT_TARGET_REFRESH_MIN_SECONDS, int(value)),
    )


def clamp_hot_scan_interval_seconds(value: int) -> int:
    return min(
        HOT_SCAN_INTERVAL_MAX_SECONDS,
        max(HOT_SCAN_INTERVAL_MIN_SECONDS, int(value)),
    )


def clamp_background_cadence_seconds(value: int) -> int:
    return min(
        BACKGROUND_CADENCE_MAX_SECONDS,
        max(BACKGROUND_CADENCE_MIN_SECONDS, int(value)),
    )


def clamp_background_scan_interval_seconds(value: int) -> int:
    return min(
        BACKGROUND_SCAN_INTERVAL_MAX_SECONDS,
        max(BACKGROUND_SCAN_INTERVAL_MIN_SECONDS, int(value)),
    )


def _resolve_optional_seconds(
    explicit_new: int | None,
    explicit_legacy: int | None,
    current: int | None,
    fallback: int,
    clamp,
) -> int:
    """New field wins, then a legacy cadence alias, then the stored value, then env/default."""

    if explicit_new is not None:
        return clamp(explicit_new)
    if explicit_legacy is not None:
        return clamp(explicit_legacy)
    if current is not None:
        return clamp(current)
    return clamp(fallback)


def clamp_universe_cadence_seconds(value: int) -> int:
    return min(
        UNIVERSE_CADENCE_MAX_SECONDS,
        max(UNIVERSE_CADENCE_MIN_SECONDS, int(value)),
    )


def clamp_max_allocated_per_trade_gbp(value: Decimal | float | int | str) -> Decimal:
    amount = Decimal(str(value))
    if amount < MAX_ALLOCATED_PER_TRADE_MIN_GBP:
        return MAX_ALLOCATED_PER_TRADE_MIN_GBP
    if amount > MAX_ALLOCATED_PER_TRADE_MAX_GBP:
        return MAX_ALLOCATED_PER_TRADE_MAX_GBP
    return amount


def _env_max_allocated_per_trade_gbp(settings: Settings) -> Decimal:
    configured = settings.allocation_per_opportunity_limit_gbp
    if configured is None:
        configured = settings.max_allocated_per_trade_gbp
    return clamp_max_allocated_per_trade_gbp(configured)


def _env_outright_min_net_edge(settings: Settings) -> Decimal | None:
    configured = settings.outright_min_net_edge
    if configured is None:
        return None
    return Decimal(str(configured))


def env_operator_scanner_settings(
    settings: Settings | None = None,
    *,
    scanner_stopped: bool = False,
    universe_scans_paused: bool = False,
    updated_at: datetime | None = None,
) -> OperatorScannerSettings:
    resolved = settings or get_settings()
    return OperatorScannerSettings(
        min_net_edge=Decimal(str(resolved.min_net_edge)),
        outright_min_net_edge=_env_outright_min_net_edge(resolved),
        max_execution_risk=int(resolved.max_execution_risk),
        hot_target_refresh_seconds=DEFAULT_HOT_TARGET_REFRESH_SECONDS,
        hot_scan_interval_seconds=clamp_hot_scan_interval_seconds(
            resolved.paper_hot_scan_interval_seconds
        ),
        hot_reprice_after_seconds=clamp_hot_cadence_seconds(
            resolved.paper_live_refresh_hot_interval_seconds
        ),
        background_scan_interval_seconds=clamp_background_scan_interval_seconds(
            resolved.paper_background_scan_interval_seconds
        ),
        background_reprice_after_seconds=clamp_background_cadence_seconds(
            resolved.paper_background_price_interval_seconds
        ),
        universe_discovery_refresh_seconds=clamp_universe_cadence_seconds(
            resolved.paper_universe_discovery_interval_seconds
        ),
        max_allocated_per_trade_gbp=_env_max_allocated_per_trade_gbp(resolved),
        scanner_stopped=scanner_stopped,
        universe_scans_paused=universe_scans_paused,
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
                outright_min_net_edge TEXT,
                max_execution_risk INTEGER NOT NULL,
                hot_cadence_seconds INTEGER NOT NULL,
                hot_scan_interval_seconds INTEGER,
                hot_reprice_after_seconds INTEGER,
                background_cadence_seconds INTEGER NOT NULL DEFAULT 90,
                background_scan_interval_seconds INTEGER,
                background_reprice_after_seconds INTEGER,
                universe_cadence_seconds INTEGER NOT NULL DEFAULT 1800,
                universe_discovery_refresh_seconds INTEGER,
                max_allocated_per_trade_gbp TEXT,
                scanner_stopped INTEGER NOT NULL,
                universe_scans_paused INTEGER NOT NULL DEFAULT 0,
                source TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(operator_scanner_settings)")
        }
        if "background_cadence_seconds" not in columns:
            connection.execute(
                "ALTER TABLE operator_scanner_settings "
                "ADD COLUMN background_cadence_seconds INTEGER NOT NULL DEFAULT 90"
            )
        if "max_allocated_per_trade_gbp" not in columns:
            connection.execute(
                "ALTER TABLE operator_scanner_settings "
                "ADD COLUMN max_allocated_per_trade_gbp TEXT"
            )
        if "universe_cadence_seconds" not in columns:
            connection.execute(
                "ALTER TABLE operator_scanner_settings "
                "ADD COLUMN universe_cadence_seconds INTEGER NOT NULL DEFAULT 1800"
            )
        if "universe_scans_paused" not in columns:
            connection.execute(
                "ALTER TABLE operator_scanner_settings "
                "ADD COLUMN universe_scans_paused INTEGER NOT NULL DEFAULT 0"
            )
        if "outright_min_net_edge" not in columns:
            connection.execute(
                "ALTER TABLE operator_scanner_settings "
                "ADD COLUMN outright_min_net_edge TEXT"
            )
        for column in (
            "hot_scan_interval_seconds",
            "hot_reprice_after_seconds",
            "background_scan_interval_seconds",
            "background_reprice_after_seconds",
            "universe_discovery_refresh_seconds",
            "hot_target_refresh_seconds",
        ):
            if column not in columns:
                connection.execute(
                    f"ALTER TABLE operator_scanner_settings ADD COLUMN {column} INTEGER"
                )

    def load(self) -> OperatorScannerSettings | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT *
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
        hot_cadence_seconds: int | None = None,
        hot_target_refresh_seconds: int | None = None,
        hot_scan_interval_seconds: int | None = None,
        hot_reprice_after_seconds: int | None = None,
        background_cadence_seconds: int | None = None,
        background_scan_interval_seconds: int | None = None,
        background_reprice_after_seconds: int | None = None,
        universe_cadence_seconds: int | None = None,
        universe_discovery_refresh_seconds: int | None = None,
        max_allocated_per_trade_gbp: Decimal | None = None,
        outright_min_net_edge: Any = _UNSET,
        scanner_stopped: bool | None = None,
        universe_scans_paused: bool | None = None,
    ) -> OperatorScannerSettings:
        current = self.load()
        stopped = current.scanner_stopped if current is not None and scanner_stopped is None else bool(
            scanner_stopped if scanner_stopped is not None else False
        )
        paused = (
            current.universe_scans_paused
            if current is not None and universe_scans_paused is None
            else bool(universe_scans_paused if universe_scans_paused is not None else False)
        )
        hot_reprice = _resolve_optional_seconds(
            hot_reprice_after_seconds,
            hot_cadence_seconds,
            current.hot_reprice_after_seconds if current is not None else None,
            get_settings().paper_live_refresh_hot_interval_seconds,
            clamp_hot_cadence_seconds,
        )
        hot_target = _resolve_optional_seconds(
            hot_target_refresh_seconds,
            None,
            current.hot_target_refresh_seconds if current is not None else None,
            DEFAULT_HOT_TARGET_REFRESH_SECONDS,
            clamp_hot_target_refresh_seconds,
        )
        hot_scan = _resolve_optional_seconds(
            hot_scan_interval_seconds,
            None,
            current.hot_scan_interval_seconds if current is not None else None,
            DEFAULT_HOT_SCAN_INTERVAL_SECONDS,
            clamp_hot_scan_interval_seconds,
        )
        background = _resolve_optional_seconds(
            background_reprice_after_seconds,
            background_cadence_seconds,
            current.background_reprice_after_seconds if current is not None else None,
            get_settings().paper_background_price_interval_seconds,
            clamp_background_cadence_seconds,
        )
        background_scan = _resolve_optional_seconds(
            background_scan_interval_seconds,
            None,
            current.background_scan_interval_seconds if current is not None else None,
            DEFAULT_BACKGROUND_SCAN_INTERVAL_SECONDS,
            clamp_background_scan_interval_seconds,
        )
        universe = _resolve_optional_seconds(
            universe_discovery_refresh_seconds,
            universe_cadence_seconds,
            current.universe_discovery_refresh_seconds if current is not None else None,
            get_settings().paper_universe_discovery_interval_seconds,
            clamp_universe_cadence_seconds,
        )
        if max_allocated_per_trade_gbp is None:
            if current is not None:
                allocated = current.max_allocated_per_trade_gbp
            else:
                allocated = _env_max_allocated_per_trade_gbp(get_settings())
        else:
            allocated = clamp_max_allocated_per_trade_gbp(max_allocated_per_trade_gbp)
        if outright_min_net_edge is _UNSET:
            if current is not None:
                outright = current.outright_min_net_edge
            else:
                outright = _env_outright_min_net_edge(get_settings())
        else:
            outright = outright_min_net_edge
            if outright is not None:
                outright = Decimal(str(outright))
                if outright < 0 or outright >= 1:
                    raise ValueError("outright_min_net_edge must be >= 0 and < 1")
        payload = OperatorScannerSettings(
            min_net_edge=min_net_edge,
            outright_min_net_edge=outright,
            max_execution_risk=int(max_execution_risk),
            hot_target_refresh_seconds=hot_target,
            hot_scan_interval_seconds=hot_scan,
            hot_reprice_after_seconds=hot_reprice,
            background_scan_interval_seconds=background_scan,
            background_reprice_after_seconds=background,
            universe_discovery_refresh_seconds=universe,
            max_allocated_per_trade_gbp=allocated,
            scanner_stopped=stopped,
            universe_scans_paused=paused,
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
        env = env_operator_scanner_settings(
            settings,
            scanner_stopped=stopped,
            universe_scans_paused=current.universe_scans_paused if current is not None else False,
        )
        if current is None or current.source != "operator":
            payload = env.model_copy(
                update={
                    "scanner_stopped": bool(stopped),
                    "universe_scans_paused": (
                        current.universe_scans_paused if current is not None else False
                    ),
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

    def save_universe_scans_paused(
        self,
        paused: bool,
        *,
        settings: Settings | None = None,
    ) -> OperatorScannerSettings:
        current = self.load()
        env = env_operator_scanner_settings(
            settings,
            scanner_stopped=current.scanner_stopped if current is not None else False,
            universe_scans_paused=paused,
        )
        if current is None or current.source != "operator":
            payload = env.model_copy(
                update={
                    "scanner_stopped": (
                        current.scanner_stopped if current is not None else False
                    ),
                    "universe_scans_paused": bool(paused),
                    "updated_at": datetime.now(UTC),
                }
            )
        else:
            payload = current.model_copy(
                update={
                    "universe_scans_paused": bool(paused),
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
                    id, min_net_edge, outright_min_net_edge, max_execution_risk,
                    hot_cadence_seconds, hot_scan_interval_seconds, hot_reprice_after_seconds,
                    hot_target_refresh_seconds,
                    background_cadence_seconds, background_scan_interval_seconds,
                    background_reprice_after_seconds,
                    universe_cadence_seconds, universe_discovery_refresh_seconds,
                    max_allocated_per_trade_gbp, scanner_stopped,
                    universe_scans_paused, source, updated_at
                )
                VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    min_net_edge = excluded.min_net_edge,
                    outright_min_net_edge = excluded.outright_min_net_edge,
                    max_execution_risk = excluded.max_execution_risk,
                    hot_cadence_seconds = excluded.hot_cadence_seconds,
                    hot_scan_interval_seconds = excluded.hot_scan_interval_seconds,
                    hot_reprice_after_seconds = excluded.hot_reprice_after_seconds,
                    hot_target_refresh_seconds = excluded.hot_target_refresh_seconds,
                    background_cadence_seconds = excluded.background_cadence_seconds,
                    background_scan_interval_seconds = excluded.background_scan_interval_seconds,
                    background_reprice_after_seconds = excluded.background_reprice_after_seconds,
                    universe_cadence_seconds = excluded.universe_cadence_seconds,
                    universe_discovery_refresh_seconds = excluded.universe_discovery_refresh_seconds,
                    max_allocated_per_trade_gbp = excluded.max_allocated_per_trade_gbp,
                    scanner_stopped = excluded.scanner_stopped,
                    universe_scans_paused = excluded.universe_scans_paused,
                    source = excluded.source,
                    updated_at = excluded.updated_at
                """,
                (
                    str(payload.min_net_edge),
                    None
                    if payload.outright_min_net_edge is None
                    else str(payload.outright_min_net_edge),
                    int(payload.max_execution_risk),
                    int(payload.hot_reprice_after_seconds),
                    int(payload.hot_scan_interval_seconds),
                    int(payload.hot_reprice_after_seconds),
                    int(payload.hot_target_refresh_seconds),
                    int(payload.background_reprice_after_seconds),
                    int(payload.background_scan_interval_seconds),
                    int(payload.background_reprice_after_seconds),
                    int(payload.universe_discovery_refresh_seconds),
                    int(payload.universe_discovery_refresh_seconds),
                    str(payload.max_allocated_per_trade_gbp),
                    1 if payload.scanner_stopped else 0,
                    1 if payload.universe_scans_paused else 0,
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
            universe_scans_paused=saved.universe_scans_paused,
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


def _migrated_timing_seconds(raw_new: Any, legacy: int, clamp) -> int:
    """Use the new column when present. A NULL column keeps the legacy cadence or the scan default."""

    if raw_new is None or str(raw_new).strip() == "":
        return clamp(legacy)
    try:
        return clamp(int(raw_new))
    except (TypeError, ValueError):
        return clamp(legacy)


def _row_optional(row: sqlite3.Row, key: str) -> Any:
    try:
        if key not in row.keys():
            return None
        return row[key]
    except Exception:
        return None


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
    outright_min_net_edge: Decimal | None
    try:
        raw_outright = _row_optional(row, "outright_min_net_edge")
        if raw_outright is None or str(raw_outright).strip() == "":
            outright_min_net_edge = None
        else:
            outright_min_net_edge = Decimal(str(raw_outright))
            if outright_min_net_edge < 0 or outright_min_net_edge >= 1:
                raise InvalidOperation
    except (InvalidOperation, ArithmeticError, TypeError, ValueError):
        LOGGER.warning(
            "malformed operator scanner outright_min_net_edge; leaving unconfigured"
        )
        # Fail closed for COMPETITION_SEASON. Never substitute fixture min_net_edge.
        outright_min_net_edge = None
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
        raw_background = _row_optional(row, "background_cadence_seconds")
        if raw_background is None:
            raise ValueError
        background_cadence_seconds = clamp_background_cadence_seconds(int(raw_background))
    except (KeyError, IndexError, TypeError, ValueError):
        LOGGER.warning("malformed operator scanner background_cadence_seconds; using env default")
        background_cadence_seconds = clamp_background_cadence_seconds(
            get_settings().paper_background_price_interval_seconds
        )
        source = "env_default"
    try:
        raw_universe = _row_optional(row, "universe_cadence_seconds")
        if raw_universe is None:
            raise ValueError
        universe_cadence_seconds = clamp_universe_cadence_seconds(int(raw_universe))
    except (KeyError, IndexError, TypeError, ValueError):
        LOGGER.warning("malformed operator scanner universe_cadence_seconds; using env default")
        universe_cadence_seconds = clamp_universe_cadence_seconds(
            get_settings().paper_universe_discovery_interval_seconds
        )
        source = "env_default"
    hot_reprice_after_seconds = _migrated_timing_seconds(
        _row_optional(row, "hot_reprice_after_seconds"),
        hot_cadence_seconds,
        clamp_hot_cadence_seconds,
    )
    hot_scan_interval_seconds = _migrated_timing_seconds(
        _row_optional(row, "hot_scan_interval_seconds"),
        DEFAULT_HOT_SCAN_INTERVAL_SECONDS,
        clamp_hot_scan_interval_seconds,
    )
    background_reprice_after_seconds = _migrated_timing_seconds(
        _row_optional(row, "background_reprice_after_seconds"),
        background_cadence_seconds,
        clamp_background_cadence_seconds,
    )
    background_scan_interval_seconds = _migrated_timing_seconds(
        _row_optional(row, "background_scan_interval_seconds"),
        DEFAULT_BACKGROUND_SCAN_INTERVAL_SECONDS,
        clamp_background_scan_interval_seconds,
    )
    raw_target = _row_optional(row, "hot_target_refresh_seconds")
    if raw_target is None:
        # Documented migration: a saved HOT reprice-after age becomes the
        # single pass target when the new column has not been written yet.
        hot_target_refresh_seconds = clamp_hot_target_refresh_seconds(
            hot_reprice_after_seconds
        )
    else:
        hot_target_refresh_seconds = clamp_hot_target_refresh_seconds(int(raw_target))
    universe_discovery_refresh_seconds = _migrated_timing_seconds(
        _row_optional(row, "universe_discovery_refresh_seconds"),
        universe_cadence_seconds,
        clamp_universe_cadence_seconds,
    )
    try:
        raw_allocated = _row_optional(row, "max_allocated_per_trade_gbp")
        if raw_allocated is None or str(raw_allocated).strip() == "":
            max_allocated_per_trade_gbp = _env_max_allocated_per_trade_gbp(get_settings())
        else:
            max_allocated_per_trade_gbp = clamp_max_allocated_per_trade_gbp(raw_allocated)
    except (InvalidOperation, ArithmeticError, TypeError, ValueError):
        LOGGER.warning("malformed operator scanner max_allocated_per_trade_gbp; using env default")
        max_allocated_per_trade_gbp = _env_max_allocated_per_trade_gbp(get_settings())
        source = "env_default"
    try:
        updated = datetime.fromisoformat(str(row["updated_at"]))
    except ValueError:
        updated = datetime.now(UTC)
        source = "env_default"
    return OperatorScannerSettings(
        min_net_edge=min_net_edge,
        outright_min_net_edge=outright_min_net_edge,
        max_execution_risk=max_execution_risk,
        hot_target_refresh_seconds=hot_target_refresh_seconds,
        hot_scan_interval_seconds=hot_scan_interval_seconds,
        hot_reprice_after_seconds=hot_reprice_after_seconds,
        background_scan_interval_seconds=background_scan_interval_seconds,
        background_reprice_after_seconds=background_reprice_after_seconds,
        universe_discovery_refresh_seconds=universe_discovery_refresh_seconds,
        max_allocated_per_trade_gbp=max_allocated_per_trade_gbp,
        scanner_stopped=bool(int(row["scanner_stopped"] or 0)),
        universe_scans_paused=bool(int(_row_optional(row, "universe_scans_paused") or 0)),
        source=source,
        updated_at=updated,
        restart_semantics=OPERATOR_SCANNER_RESTART_SEMANTICS,
    )
