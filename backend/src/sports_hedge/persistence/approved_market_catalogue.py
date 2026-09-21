"""Additive SQLite persistence for the approved-market catalogue.

Tables live beside the existing paper-settings database. This is not the
UNIVERSE generation checkpoint, not a durable price-engine work queue, and
not a second equivalence authority.

Current-row table is scheduler truth. Lifecycle/version history is append-only
and written in the same transaction as the current-row mutation. Kalshi fee
snapshots are semantically immutable once inserted.

PAPER / read-only. No venue writes.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Any

from sports_hedge.application.approved_market_catalogue import (
    CATALOGUE_FORBIDDEN_COLUMNS,
    CATALOGUE_SCHEMA_VERSION,
    FEE_SNAPSHOT_FORBIDDEN_COLUMNS,
    HISTORY_FORBIDDEN_COLUMNS,
    ApprovedMarketCatalogueRow,
    CatalogueHistoryRecord,
    CatalogueRowState,
    KalshiFeeSnapshotRecord,
    OutcomeNativeId,
    derived_price_engine_working_set,
)
from sports_hedge.config import get_settings
from sports_hedge.domain.market_scope import MarketScope
from sports_hedge.domain.models import VenueName

_CREATE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS kalshi_fee_snapshot (
    snapshot_id TEXT PRIMARY KEY,
    series_ticker TEXT NOT NULL,
    event_ticker TEXT,
    market_ticker TEXT,
    fee_type TEXT,
    fee_multiplier TEXT,
    fee_type_override TEXT,
    fee_multiplier_override TEXT,
    series_fee_type TEXT,
    series_fee_multiplier TEXT,
    fee_provenance TEXT,
    fee_resolution_status TEXT NOT NULL,
    fee_resolution_error TEXT,
    captured_at TEXT NOT NULL,
    confirmed_at TEXT,
    source TEXT
);

CREATE TABLE IF NOT EXISTS approved_market_catalogue (
    catalogue_row_id TEXT PRIMARY KEY,
    schema_version INTEGER NOT NULL,
    register_version TEXT NOT NULL,
    register_canonical_key TEXT NOT NULL,
    canonical_event_id TEXT NOT NULL,
    competition TEXT,
    home_canonical TEXT,
    away_canonical TEXT,
    kickoff_utc TEXT,
    matchbook_event_id TEXT,
    matchbook_market_id TEXT,
    matchbook_runner_ids_json TEXT NOT NULL,
    kalshi_event_ticker TEXT,
    kalshi_market_tickers_json TEXT NOT NULL,
    kalshi_outcome_ids_json TEXT NOT NULL,
    kalshi_series_ticker TEXT,
    polymarket_event_id TEXT,
    polymarket_market_id TEXT,
    polymarket_condition_id TEXT,
    polymarket_token_ids_json TEXT,
    family TEXT,
    period TEXT,
    line TEXT,
    required_outcomes_json TEXT NOT NULL,
    kalshi_fee_snapshot_id TEXT,
    row_state TEXT NOT NULL,
    invalidation_reason TEXT,
    first_catalogued_at TEXT NOT NULL,
    last_confirmed_at TEXT,
    last_seen_generation_id TEXT,
    content_version INTEGER NOT NULL,
    market_scope TEXT NOT NULL DEFAULT 'FIXTURE_MATCH',
    source_venue TEXT,
    season_id TEXT,
    competition_code TEXT,
    participant_type TEXT,
    participant_canonical_id TEXT,
    settlement_fingerprint_version TEXT,
    expected_settlement_horizon TEXT,
    polymarket_clob_token_ids_json TEXT NOT NULL DEFAULT '[]',
    polymarket_event_slug TEXT,
    polymarket_event_ticker TEXT,
    FOREIGN KEY (kalshi_fee_snapshot_id) REFERENCES kalshi_fee_snapshot(snapshot_id)
);

CREATE TABLE IF NOT EXISTS approved_market_catalogue_history (
    history_id INTEGER PRIMARY KEY AUTOINCREMENT,
    catalogue_row_id TEXT NOT NULL,
    prior_row_state TEXT,
    new_row_state TEXT NOT NULL,
    prior_content_version INTEGER,
    new_content_version INTEGER NOT NULL,
    prior_native_identity_json TEXT,
    new_native_identity_json TEXT NOT NULL,
    prior_kalshi_fee_snapshot_id TEXT,
    new_kalshi_fee_snapshot_id TEXT,
    invalidation_reason TEXT,
    generation_id TEXT,
    recorded_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_approved_catalogue_active_identity
ON approved_market_catalogue (canonical_event_id, register_canonical_key)
WHERE row_state = 'ACTIVE';

CREATE INDEX IF NOT EXISTS idx_approved_catalogue_event
ON approved_market_catalogue (canonical_event_id);

CREATE INDEX IF NOT EXISTS idx_approved_catalogue_state
ON approved_market_catalogue (row_state);

CREATE INDEX IF NOT EXISTS idx_approved_catalogue_history_row
ON approved_market_catalogue_history (catalogue_row_id, history_id);

CREATE INDEX IF NOT EXISTS idx_approved_catalogue_market_scope
ON approved_market_catalogue (market_scope, row_state);
"""

_CATALOGUE_ADDITIVE_COLUMNS: tuple[tuple[str, str], ...] = (
    ("polymarket_event_id", "TEXT"),
    ("polymarket_market_id", "TEXT"),
    ("polymarket_condition_id", "TEXT"),
    ("polymarket_token_ids_json", "TEXT"),
    ("market_scope", "TEXT NOT NULL DEFAULT 'FIXTURE_MATCH'"),
    ("source_venue", "TEXT"),
    ("season_id", "TEXT"),
    ("competition_code", "TEXT"),
    ("participant_type", "TEXT"),
    ("participant_canonical_id", "TEXT"),
    ("settlement_fingerprint_version", "TEXT"),
    ("expected_settlement_horizon", "TEXT"),
    ("polymarket_clob_token_ids_json", "TEXT NOT NULL DEFAULT '[]'"),
    ("polymarket_event_slug", "TEXT"),
    ("polymarket_event_ticker", "TEXT"),
)


class ApprovedMarketCatalogueTransaction:
    """Connection-bound helpers for one complete catalogue mutation."""

    def __init__(
        self,
        store: SqliteApprovedMarketCatalogueStore,
        connection: sqlite3.Connection,
    ) -> None:
        self._store = store
        self._connection = connection

    def get_row(self, catalogue_row_id: str) -> ApprovedMarketCatalogueRow | None:
        return self._store._get_row_on(self._connection, catalogue_row_id)

    def get_row_for_identity(
        self, canonical_event_id: str, register_canonical_key: str
    ) -> ApprovedMarketCatalogueRow | None:
        return self._store._get_row_for_identity_on(
            self._connection, canonical_event_id, register_canonical_key
        )

    def list_rows_for_event(self, canonical_event_id: str) -> list[ApprovedMarketCatalogueRow]:
        return self._store._list_rows_for_event_on(self._connection, canonical_event_id)

    def insert_fee_snapshot(self, snapshot: KalshiFeeSnapshotRecord) -> str:
        return self._store._insert_fee_snapshot_on(self._connection, snapshot)

    def upsert_catalogue_row(self, row: ApprovedMarketCatalogueRow) -> ApprovedMarketCatalogueRow:
        return self._store._upsert_catalogue_row_on(self._connection, row)

    def mark_disappeared(
        self,
        catalogue_row_id: str,
        *,
        reason: str,
        now: datetime,
        generation_id: str | None,
    ) -> ApprovedMarketCatalogueRow | None:
        return self._store._mark_disappeared_on(
            self._connection,
            catalogue_row_id,
            reason=reason,
            now=now,
            generation_id=generation_id,
        )

    def mark_fixture_terminal(
        self,
        canonical_event_id: str,
        *,
        reason: str,
        now: datetime,
        generation_id: str | None,
    ) -> list[ApprovedMarketCatalogueRow]:
        return self._store._mark_fixture_terminal_on(
            self._connection,
            canonical_event_id,
            reason=reason,
            now=now,
            generation_id=generation_id,
        )


class SqliteApprovedMarketCatalogueStore:
    """File-backed or in-memory store for catalogue rows and Kalshi fee snapshots."""

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
        connection.execute("PRAGMA foreign_keys=ON")
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

    def run_in_transaction(
        self, mutator: Callable[[ApprovedMarketCatalogueTransaction], Any]
    ) -> Any:
        """Run a complete catalogue read/modify/write as one SQLite transaction."""

        with self._connect() as connection:
            return mutator(ApprovedMarketCatalogueTransaction(self, connection))

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(_CREATE_SCHEMA_SQL)
        existing = {
            str(row["name"])
            for row in connection.execute("PRAGMA table_info(approved_market_catalogue)")
        }
        for name, spec in _CATALOGUE_ADDITIVE_COLUMNS:
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE approved_market_catalogue ADD COLUMN {name} {spec}"
                )
        connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_approved_catalogue_market_scope
            ON approved_market_catalogue (market_scope, row_state)
            """
        )
        self._assert_no_forbidden_columns(connection)

    def _assert_no_forbidden_columns(self, connection: sqlite3.Connection) -> None:
        catalogue_cols = {
            str(row["name"]).casefold()
            for row in connection.execute("PRAGMA table_info(approved_market_catalogue)")
        }
        fee_cols = {
            str(row["name"]).casefold()
            for row in connection.execute("PRAGMA table_info(kalshi_fee_snapshot)")
        }
        history_cols = {
            str(row["name"]).casefold()
            for row in connection.execute("PRAGMA table_info(approved_market_catalogue_history)")
        }
        forbidden_catalogue = catalogue_cols & {item.casefold() for item in CATALOGUE_FORBIDDEN_COLUMNS}
        forbidden_fees = fee_cols & {item.casefold() for item in FEE_SNAPSHOT_FORBIDDEN_COLUMNS}
        forbidden_history = history_cols & {item.casefold() for item in HISTORY_FORBIDDEN_COLUMNS}
        if forbidden_catalogue or forbidden_fees or forbidden_history:
            raise RuntimeError(
                "approved-market catalogue schema contains forbidden policy/quote columns: "
                f"catalogue={sorted(forbidden_catalogue)} fees={sorted(forbidden_fees)} "
                f"history={sorted(forbidden_history)}"
            )

    def table_columns(self, table: str) -> list[str]:
        with self._connect() as connection:
            return [
                str(row["name"])
                for row in connection.execute(f"PRAGMA table_info({table})")
            ]

    def upsert_fee_snapshot(self, snapshot: KalshiFeeSnapshotRecord) -> str:
        with self._connect() as connection:
            return self._insert_fee_snapshot_on(connection, snapshot)

    def _insert_fee_snapshot_on(
        self, connection: sqlite3.Connection, snapshot: KalshiFeeSnapshotRecord
    ) -> str:
        payload = (
            snapshot.snapshot_id,
            snapshot.series_ticker,
            snapshot.event_ticker,
            snapshot.market_ticker,
            snapshot.fee_type,
            snapshot.fee_multiplier,
            _json_or_text(snapshot.fee_type_override),
            _json_or_text(snapshot.fee_multiplier_override),
            _json_or_text(snapshot.series_fee_type),
            _json_or_text(snapshot.series_fee_multiplier),
            snapshot.fee_provenance,
            snapshot.fee_resolution_status,
            snapshot.fee_resolution_error,
            snapshot.captured_at.isoformat(),
            None if snapshot.confirmed_at is None else snapshot.confirmed_at.isoformat(),
            snapshot.source,
        )
        connection.execute(
            """
            INSERT INTO kalshi_fee_snapshot (
                snapshot_id, series_ticker, event_ticker, market_ticker,
                fee_type, fee_multiplier, fee_type_override, fee_multiplier_override,
                series_fee_type, series_fee_multiplier, fee_provenance,
                fee_resolution_status, fee_resolution_error, captured_at,
                confirmed_at, source
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(snapshot_id) DO NOTHING
            """,
            payload,
        )
        return snapshot.snapshot_id

    def get_fee_snapshot(self, snapshot_id: str) -> KalshiFeeSnapshotRecord | None:
        with self._connect() as connection:
            return self._get_fee_snapshot_on(connection, snapshot_id)

    def _get_fee_snapshot_on(
        self, connection: sqlite3.Connection, snapshot_id: str
    ) -> KalshiFeeSnapshotRecord | None:
        row = connection.execute(
            "SELECT * FROM kalshi_fee_snapshot WHERE snapshot_id = ?",
            (snapshot_id,),
        ).fetchone()
        if row is None:
            return None
        return _fee_from_row(row)

    def list_fee_snapshots(self) -> list[KalshiFeeSnapshotRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM kalshi_fee_snapshot ORDER BY captured_at, snapshot_id"
            ).fetchall()
        return [_fee_from_row(row) for row in rows]

    def get_active(
        self, canonical_event_id: str, register_canonical_key: str
    ) -> ApprovedMarketCatalogueRow | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT * FROM approved_market_catalogue
                WHERE canonical_event_id = ?
                  AND register_canonical_key = ?
                  AND row_state = 'ACTIVE'
                """,
                (canonical_event_id, register_canonical_key),
            ).fetchone()
        if row is None:
            return None
        return _catalogue_from_row(row)

    def get_row(self, catalogue_row_id: str) -> ApprovedMarketCatalogueRow | None:
        with self._connect() as connection:
            return self._get_row_on(connection, catalogue_row_id)

    def _get_row_on(
        self, connection: sqlite3.Connection, catalogue_row_id: str
    ) -> ApprovedMarketCatalogueRow | None:
        row = connection.execute(
            "SELECT * FROM approved_market_catalogue WHERE catalogue_row_id = ?",
            (catalogue_row_id,),
        ).fetchone()
        if row is None:
            return None
        return _catalogue_from_row(row)

    def get_row_for_identity(
        self, canonical_event_id: str, register_canonical_key: str
    ) -> ApprovedMarketCatalogueRow | None:
        with self._connect() as connection:
            return self._get_row_for_identity_on(
                connection, canonical_event_id, register_canonical_key
            )

    def _get_row_for_identity_on(
        self,
        connection: sqlite3.Connection,
        canonical_event_id: str,
        register_canonical_key: str,
    ) -> ApprovedMarketCatalogueRow | None:
        row = connection.execute(
            """
            SELECT * FROM approved_market_catalogue
            WHERE canonical_event_id = ? AND register_canonical_key = ?
            """,
            (canonical_event_id, register_canonical_key),
        ).fetchone()
        if row is None:
            return None
        return _catalogue_from_row(row)

    def list_active(self) -> list[ApprovedMarketCatalogueRow]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM approved_market_catalogue
                WHERE row_state = 'ACTIVE'
                ORDER BY canonical_event_id, register_canonical_key
                """
            ).fetchall()
        return [_catalogue_from_row(row) for row in rows]

    def list_rows_for_event(self, canonical_event_id: str) -> list[ApprovedMarketCatalogueRow]:
        with self._connect() as connection:
            return self._list_rows_for_event_on(connection, canonical_event_id)

    def _list_rows_for_event_on(
        self, connection: sqlite3.Connection, canonical_event_id: str
    ) -> list[ApprovedMarketCatalogueRow]:
        rows = connection.execute(
            """
            SELECT * FROM approved_market_catalogue
            WHERE canonical_event_id = ?
            ORDER BY register_canonical_key
            """,
            (canonical_event_id,),
        ).fetchall()
        return [_catalogue_from_row(row) for row in rows]

    def list_history(self, catalogue_row_id: str) -> list[CatalogueHistoryRecord]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM approved_market_catalogue_history
                WHERE catalogue_row_id = ?
                ORDER BY history_id ASC
                """,
                (catalogue_row_id,),
            ).fetchall()
        return [_history_from_row(row) for row in rows]

    def upsert_catalogue_row(self, row: ApprovedMarketCatalogueRow) -> ApprovedMarketCatalogueRow:
        with self._connect() as connection:
            return self._upsert_catalogue_row_on(connection, row)

    def _upsert_catalogue_row_on(
        self, connection: sqlite3.Connection, row: ApprovedMarketCatalogueRow
    ) -> ApprovedMarketCatalogueRow:
        existing = self._get_row_on(connection, row.catalogue_row_id)
        if existing is None:
            existing = self._get_row_for_identity_on(
                connection, row.canonical_event_id, row.register_canonical_key
            )
        self._insert_history_if_changed(connection, prior=existing, new=row)
        payload = _catalogue_to_sql(row)
        connection.execute(
            """
            INSERT INTO approved_market_catalogue (
                catalogue_row_id, schema_version, register_version, register_canonical_key,
                canonical_event_id, competition, home_canonical, away_canonical, kickoff_utc,
                matchbook_event_id, matchbook_market_id, matchbook_runner_ids_json,
                kalshi_event_ticker, kalshi_market_tickers_json, kalshi_outcome_ids_json,
                kalshi_series_ticker, polymarket_event_id, polymarket_market_id,
                polymarket_condition_id, polymarket_token_ids_json, family, period, line,
                required_outcomes_json, kalshi_fee_snapshot_id, row_state, invalidation_reason,
                first_catalogued_at, last_confirmed_at, last_seen_generation_id,
                content_version, market_scope, source_venue, season_id, competition_code,
                participant_type, participant_canonical_id, settlement_fingerprint_version,
                expected_settlement_horizon, polymarket_clob_token_ids_json,
                polymarket_event_slug, polymarket_event_ticker
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?
            )
            ON CONFLICT(catalogue_row_id) DO UPDATE SET
                schema_version = excluded.schema_version,
                register_version = excluded.register_version,
                register_canonical_key = excluded.register_canonical_key,
                canonical_event_id = excluded.canonical_event_id,
                competition = excluded.competition,
                home_canonical = excluded.home_canonical,
                away_canonical = excluded.away_canonical,
                kickoff_utc = excluded.kickoff_utc,
                matchbook_event_id = excluded.matchbook_event_id,
                matchbook_market_id = excluded.matchbook_market_id,
                matchbook_runner_ids_json = excluded.matchbook_runner_ids_json,
                kalshi_event_ticker = excluded.kalshi_event_ticker,
                kalshi_market_tickers_json = excluded.kalshi_market_tickers_json,
                kalshi_outcome_ids_json = excluded.kalshi_outcome_ids_json,
                kalshi_series_ticker = excluded.kalshi_series_ticker,
                polymarket_event_id = excluded.polymarket_event_id,
                polymarket_market_id = excluded.polymarket_market_id,
                polymarket_condition_id = excluded.polymarket_condition_id,
                polymarket_token_ids_json = excluded.polymarket_token_ids_json,
                family = excluded.family,
                period = excluded.period,
                line = excluded.line,
                required_outcomes_json = excluded.required_outcomes_json,
                kalshi_fee_snapshot_id = excluded.kalshi_fee_snapshot_id,
                row_state = excluded.row_state,
                invalidation_reason = excluded.invalidation_reason,
                last_confirmed_at = excluded.last_confirmed_at,
                last_seen_generation_id = excluded.last_seen_generation_id,
                content_version = excluded.content_version,
                market_scope = excluded.market_scope,
                source_venue = excluded.source_venue,
                season_id = excluded.season_id,
                competition_code = excluded.competition_code,
                participant_type = excluded.participant_type,
                participant_canonical_id = excluded.participant_canonical_id,
                settlement_fingerprint_version = excluded.settlement_fingerprint_version,
                expected_settlement_horizon = excluded.expected_settlement_horizon,
                polymarket_clob_token_ids_json = excluded.polymarket_clob_token_ids_json,
                polymarket_event_slug = excluded.polymarket_event_slug,
                polymarket_event_ticker = excluded.polymarket_event_ticker
            """,
            payload,
        )
        loaded = self._get_row_on(connection, row.catalogue_row_id)
        assert loaded is not None
        return loaded

    def _insert_history_if_changed(
        self,
        connection: sqlite3.Connection,
        *,
        prior: ApprovedMarketCatalogueRow | None,
        new: ApprovedMarketCatalogueRow,
    ) -> None:
        if not _history_worthy(prior, new):
            return
        recorded_at = new.last_confirmed_at or new.first_catalogued_at
        connection.execute(
            """
            INSERT INTO approved_market_catalogue_history (
                catalogue_row_id, prior_row_state, new_row_state,
                prior_content_version, new_content_version,
                prior_native_identity_json, new_native_identity_json,
                prior_kalshi_fee_snapshot_id, new_kalshi_fee_snapshot_id,
                invalidation_reason, generation_id, recorded_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                new.catalogue_row_id,
                None if prior is None else prior.row_state.value,
                new.row_state.value,
                None if prior is None else int(prior.content_version),
                int(new.content_version),
                None if prior is None else prior.native_identity_json(),
                new.native_identity_json(),
                None if prior is None else prior.kalshi_fee_snapshot_id,
                new.kalshi_fee_snapshot_id,
                new.invalidation_reason,
                new.last_seen_generation_id,
                recorded_at.isoformat(),
            ),
        )

    def mark_disappeared(
        self,
        catalogue_row_id: str,
        *,
        reason: str,
        now: datetime,
        generation_id: str | None,
    ) -> ApprovedMarketCatalogueRow | None:
        with self._connect() as connection:
            return self._mark_disappeared_on(
                connection,
                catalogue_row_id,
                reason=reason,
                now=now,
                generation_id=generation_id,
            )

    def _mark_disappeared_on(
        self,
        connection: sqlite3.Connection,
        catalogue_row_id: str,
        *,
        reason: str,
        now: datetime,
        generation_id: str | None,
    ) -> ApprovedMarketCatalogueRow | None:
        existing = self._get_row_on(connection, catalogue_row_id)
        if existing is None:
            return None
        updated = existing.model_copy(
            update={
                "row_state": CatalogueRowState.DISAPPEARED,
                "invalidation_reason": reason,
                "last_confirmed_at": now,
                "last_seen_generation_id": generation_id,
            }
        )
        return self._upsert_catalogue_row_on(connection, updated)

    def mark_fixture_terminal(
        self,
        canonical_event_id: str,
        *,
        reason: str,
        now: datetime,
        generation_id: str | None,
    ) -> list[ApprovedMarketCatalogueRow]:
        with self._connect() as connection:
            return self._mark_fixture_terminal_on(
                connection,
                canonical_event_id,
                reason=reason,
                now=now,
                generation_id=generation_id,
            )

    def _mark_fixture_terminal_on(
        self,
        connection: sqlite3.Connection,
        canonical_event_id: str,
        *,
        reason: str,
        now: datetime,
        generation_id: str | None,
    ) -> list[ApprovedMarketCatalogueRow]:
        updated: list[ApprovedMarketCatalogueRow] = []
        for row in self._list_rows_for_event_on(connection, canonical_event_id):
            if row.row_state is CatalogueRowState.TERMINAL:
                updated.append(row)
                continue
            changed = row.model_copy(
                update={
                    "row_state": CatalogueRowState.TERMINAL,
                    "invalidation_reason": reason,
                    "last_confirmed_at": now,
                    "last_seen_generation_id": generation_id,
                }
            )
            updated.append(self._upsert_catalogue_row_on(connection, changed))
        return updated

    def active_price_engine_working_set(self) -> list[Any]:
        return derived_price_engine_working_set(self.list_active())

    def close(self) -> None:
        with self._lock:
            if self._shared_connection is not None:
                self._shared_connection.close()
                self._shared_connection = None


@lru_cache
def get_approved_market_catalogue_store() -> SqliteApprovedMarketCatalogueStore:
    settings = get_settings()
    database = settings.paper_settings_db_path
    if database != ":memory:":
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteApprovedMarketCatalogueStore(database)


def _history_worthy(
    prior: ApprovedMarketCatalogueRow | None, new: ApprovedMarketCatalogueRow
) -> bool:
    if prior is None:
        return True
    return (
        prior.row_state != new.row_state
        or prior.content_version != new.content_version
        or prior.native_identity_tuple() != new.native_identity_tuple()
        or prior.kalshi_fee_snapshot_id != new.kalshi_fee_snapshot_id
        or prior.invalidation_reason != new.invalidation_reason
    )


def _json_or_text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return json.dumps(value, separators=(",", ":"), default=str)
    return str(value)


def _catalogue_to_sql(row: ApprovedMarketCatalogueRow) -> tuple[Any, ...]:
    return (
        row.catalogue_row_id,
        int(row.schema_version or CATALOGUE_SCHEMA_VERSION),
        row.register_version,
        row.register_canonical_key,
        row.canonical_event_id,
        row.competition,
        row.home_canonical,
        row.away_canonical,
        None if row.kickoff_utc is None else row.kickoff_utc.isoformat(),
        row.matchbook_event_id,
        row.matchbook_market_id,
        json.dumps([item.model_dump() for item in row.matchbook_runner_ids], separators=(",", ":")),
        row.kalshi_event_ticker,
        json.dumps(list(row.kalshi_market_tickers), separators=(",", ":")),
        json.dumps([item.model_dump() for item in row.kalshi_outcome_ids], separators=(",", ":")),
        row.kalshi_series_ticker,
        row.polymarket_event_id,
        row.polymarket_market_id,
        row.polymarket_condition_id,
        json.dumps([item.model_dump() for item in row.polymarket_token_ids], separators=(",", ":")),
        row.family,
        row.period,
        row.line,
        json.dumps(list(row.required_outcomes), separators=(",", ":")),
        row.kalshi_fee_snapshot_id,
        row.row_state.value,
        row.invalidation_reason,
        row.first_catalogued_at.isoformat(),
        None if row.last_confirmed_at is None else row.last_confirmed_at.isoformat(),
        row.last_seen_generation_id,
        int(row.content_version),
        row.market_scope.value,
        None if row.source_venue is None else row.source_venue.value,
        row.season_id,
        row.competition_code,
        row.participant_type,
        row.participant_canonical_id,
        row.settlement_fingerprint_version,
        row.expected_settlement_horizon,
        json.dumps(list(row.polymarket_clob_token_ids), separators=(",", ":")),
        row.polymarket_event_slug,
        row.polymarket_event_ticker,
    )


def _fee_from_row(row: sqlite3.Row) -> KalshiFeeSnapshotRecord:
    return KalshiFeeSnapshotRecord(
        snapshot_id=row["snapshot_id"],
        series_ticker=row["series_ticker"],
        event_ticker=row["event_ticker"],
        market_ticker=row["market_ticker"],
        fee_type=row["fee_type"],
        fee_multiplier=row["fee_multiplier"],
        fee_type_override=_decode_maybe_json(row["fee_type_override"]),
        fee_multiplier_override=_decode_maybe_json(row["fee_multiplier_override"]),
        series_fee_type=_decode_maybe_json(row["series_fee_type"]),
        series_fee_multiplier=_decode_maybe_json(row["series_fee_multiplier"]),
        fee_provenance=row["fee_provenance"],
        fee_resolution_status=row["fee_resolution_status"],
        fee_resolution_error=row["fee_resolution_error"],
        captured_at=datetime.fromisoformat(row["captured_at"]),
        confirmed_at=(
            None if not row["confirmed_at"] else datetime.fromisoformat(row["confirmed_at"])
        ),
        source=row["source"],
    )


def _row_value(row: sqlite3.Row, name: str, default: Any = None) -> Any:
    keys = row.keys()
    if name not in keys:
        return default
    value = row[name]
    return default if value is None else value


def _catalogue_from_row(row: sqlite3.Row) -> ApprovedMarketCatalogueRow:
    runners = [
        OutcomeNativeId.model_validate(item)
        for item in json.loads(row["matchbook_runner_ids_json"] or "[]")
    ]
    kalshi_outcomes = [
        OutcomeNativeId.model_validate(item)
        for item in json.loads(row["kalshi_outcome_ids_json"] or "[]")
    ]
    keys = set(row.keys())
    polymarket_tokens = [
        OutcomeNativeId.model_validate(item)
        for item in json.loads(
            (row["polymarket_token_ids_json"] if "polymarket_token_ids_json" in keys else None) or "[]"
        )
    ]
    source_venue_raw = _row_value(row, "source_venue")
    scope_raw = _row_value(row, "market_scope", MarketScope.FIXTURE_MATCH.value)
    return ApprovedMarketCatalogueRow(
        catalogue_row_id=row["catalogue_row_id"],
        schema_version=int(row["schema_version"]),
        register_version=row["register_version"],
        register_canonical_key=row["register_canonical_key"],
        canonical_event_id=row["canonical_event_id"],
        competition=row["competition"],
        home_canonical=row["home_canonical"],
        away_canonical=row["away_canonical"],
        kickoff_utc=(
            None if not row["kickoff_utc"] else datetime.fromisoformat(row["kickoff_utc"])
        ),
        matchbook_event_id=row["matchbook_event_id"],
        matchbook_market_id=row["matchbook_market_id"],
        matchbook_runner_ids=runners,
        kalshi_event_ticker=row["kalshi_event_ticker"],
        kalshi_market_tickers=list(json.loads(row["kalshi_market_tickers_json"] or "[]")),
        kalshi_outcome_ids=kalshi_outcomes,
        kalshi_series_ticker=row["kalshi_series_ticker"],
        polymarket_event_id=_row_optional(row, keys, "polymarket_event_id"),
        polymarket_market_id=_row_optional(row, keys, "polymarket_market_id"),
        polymarket_condition_id=_row_optional(row, keys, "polymarket_condition_id"),
        polymarket_token_ids=polymarket_tokens,
        family=row["family"],
        period=row["period"],
        line=row["line"],
        required_outcomes=list(json.loads(row["required_outcomes_json"] or "[]")),
        kalshi_fee_snapshot_id=row["kalshi_fee_snapshot_id"],
        row_state=CatalogueRowState(row["row_state"]),
        invalidation_reason=row["invalidation_reason"],
        first_catalogued_at=datetime.fromisoformat(row["first_catalogued_at"]),
        last_confirmed_at=(
            None
            if not row["last_confirmed_at"]
            else datetime.fromisoformat(row["last_confirmed_at"])
        ),
        last_seen_generation_id=row["last_seen_generation_id"],
        content_version=int(row["content_version"]),
        market_scope=MarketScope(str(scope_raw or MarketScope.FIXTURE_MATCH.value)),
        source_venue=None if not source_venue_raw else VenueName(str(source_venue_raw)),
        season_id=_row_value(row, "season_id"),
        competition_code=_row_value(row, "competition_code"),
        participant_type=_row_value(row, "participant_type"),
        participant_canonical_id=_row_value(row, "participant_canonical_id"),
        settlement_fingerprint_version=_row_value(row, "settlement_fingerprint_version"),
        expected_settlement_horizon=_row_value(row, "expected_settlement_horizon"),
        polymarket_clob_token_ids=list(
            json.loads(_row_value(row, "polymarket_clob_token_ids_json", "[]") or "[]")
        ),
        polymarket_event_slug=_row_value(row, "polymarket_event_slug"),
        polymarket_event_ticker=_row_value(row, "polymarket_event_ticker"),
    )


def _history_from_row(row: sqlite3.Row) -> CatalogueHistoryRecord:
    return CatalogueHistoryRecord(
        history_id=int(row["history_id"]),
        catalogue_row_id=row["catalogue_row_id"],
        prior_row_state=row["prior_row_state"],
        new_row_state=row["new_row_state"],
        prior_content_version=(
            None if row["prior_content_version"] is None else int(row["prior_content_version"])
        ),
        new_content_version=int(row["new_content_version"]),
        prior_native_identity_json=row["prior_native_identity_json"],
        new_native_identity_json=row["new_native_identity_json"],
        prior_kalshi_fee_snapshot_id=row["prior_kalshi_fee_snapshot_id"],
        new_kalshi_fee_snapshot_id=row["new_kalshi_fee_snapshot_id"],
        invalidation_reason=row["invalidation_reason"],
        generation_id=row["generation_id"],
        recorded_at=datetime.fromisoformat(row["recorded_at"]),
    )


def _decode_maybe_json(value: Any) -> Any:
    if value is None or value == "":
        return None
    if not isinstance(value, str):
        return value
    text = value.strip()
    if text[:1] in {"{", "[", '"'}:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return value
    return value


def _row_optional(row: sqlite3.Row, keys: set[str], name: str) -> str | None:
    if name not in keys:
        return None
    value = row[name]
    if value is None:
        return None
    text = str(value).strip()
    return text or None
