"""Issue #459: migrate legacy approved-market catalogues without deleting rows.

PAPER-only persistence. Not owner-live quotes. Not modelled probabilities.
Does not recreate the catalogue database.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from sports_hedge.application.approved_market_catalogue import CATALOGUE_SCHEMA_VERSION
from sports_hedge.domain.market_scope import MarketScope
from sports_hedge.matching.approved_register import (
    CANONICAL_MATCH_RESULT_FT,
    REGISTER_VERSION,
)
from sports_hedge.persistence.approved_market_catalogue import (
    SqliteApprovedMarketCatalogueStore,
    _CATALOGUE_ADDITIVE_COLUMNS,
    _CATALOGUE_POST_MIGRATION_INDEX_SQL,
    _CREATE_SCHEMA_SQL,
)

LEGACY_ROW_ID = "amc-legacy-pre-outrights"
LEGACY_EVENT_ID = "evt-legacy-catalogue"
LEGACY_CATALOGUED_AT = "2026-09-01T12:00:00+00:00"

# Exact owner-live catalogue DDL from before Outrights Phase 1A (722a5ed).
# Existing local DBs already have this table, so CREATE TABLE IF NOT EXISTS is
# a no-op and market_scope is absent until ALTER.
_PRE_PHASE_1A_CATALOGUE_SCHEMA_SQL = """
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
"""

# Original Phase 2 catalogue table, before any additive columns existed.
_ORIGINAL_CATALOGUE_TABLE_SQL = """
CREATE TABLE approved_market_catalogue (
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
    content_version INTEGER NOT NULL
);
"""


def _table_columns(path: Path, table: str = "approved_market_catalogue") -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {str(row[1]) for row in connection.execute(f"PRAGMA table_info({table})")}
    finally:
        connection.close()


def _index_names(path: Path, table: str = "approved_market_catalogue") -> set[str]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = ?",
            (table,),
        ).fetchall()
        return {str(row[0]) for row in rows if row[0] is not None}
    finally:
        connection.close()


def _raw_row_count(path: Path) -> int:
    connection = sqlite3.connect(path)
    try:
        return int(connection.execute("SELECT COUNT(*) FROM approved_market_catalogue").fetchone()[0])
    finally:
        connection.close()


def _insert_legacy_fixture_row(connection: sqlite3.Connection) -> None:
    connection.execute(
        """
        INSERT INTO approved_market_catalogue (
            catalogue_row_id, schema_version, register_version, register_canonical_key,
            canonical_event_id, competition, home_canonical, away_canonical, kickoff_utc,
            matchbook_event_id, matchbook_market_id, matchbook_runner_ids_json,
            kalshi_event_ticker, kalshi_market_tickers_json, kalshi_outcome_ids_json,
            kalshi_series_ticker, family, period, line, required_outcomes_json,
            kalshi_fee_snapshot_id, row_state, invalidation_reason, first_catalogued_at,
            last_confirmed_at, last_seen_generation_id, content_version
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, NULL, ?, NULL, ?, NULL, ?, ?)
        """,
        (
            LEGACY_ROW_ID,
            CATALOGUE_SCHEMA_VERSION,
            REGISTER_VERSION,
            CANONICAL_MATCH_RESULT_FT,
            LEGACY_EVENT_ID,
            "Premier League",
            "Arsenal",
            "Chelsea",
            "2026-09-12T15:00:00+00:00",
            "mb-event-legacy",
            "mb-market-legacy",
            "[]",
            "KXEPLGAME-LEGACY",
            "[]",
            "[]",
            "KXEPLGAME",
            "match_result",
            "full_time",
            '["home","draw","away"]',
            "ACTIVE",
            LEGACY_CATALOGUED_AT,
            "gen-legacy",
            1,
        ),
    )


def _assert_migrated_catalogue(path: Path) -> None:
    assert path.exists()
    columns = _table_columns(path)
    assert "market_scope" in columns
    for name, _spec in _CATALOGUE_ADDITIVE_COLUMNS:
        assert name in columns, name
    assert "idx_approved_catalogue_market_scope" in _index_names(path)
    assert _raw_row_count(path) == 1

    connection = sqlite3.connect(path)
    try:
        row = connection.execute(
            """
            SELECT catalogue_row_id, canonical_event_id, register_canonical_key,
                   market_scope, home_canonical, away_canonical, content_version
            FROM approved_market_catalogue
            WHERE catalogue_row_id = ?
            """,
            (LEGACY_ROW_ID,),
        ).fetchone()
    finally:
        connection.close()
    assert row is not None
    assert row[0] == LEGACY_ROW_ID
    assert row[1] == LEGACY_EVENT_ID
    assert row[2] == CANONICAL_MATCH_RESULT_FT
    assert row[3] == MarketScope.FIXTURE_MATCH.value
    assert row[4] == "Arsenal"
    assert row[5] == "Chelsea"
    assert row[6] == 1


def test_create_schema_sql_does_not_index_additive_columns_before_alter() -> None:
    assert "idx_approved_catalogue_market_scope" not in _CREATE_SCHEMA_SQL
    assert "ON approved_market_catalogue (market_scope, row_state)" not in _CREATE_SCHEMA_SQL
    assert "idx_approved_catalogue_market_scope" in _CATALOGUE_POST_MIGRATION_INDEX_SQL
    assert "market_scope TEXT NOT NULL DEFAULT 'FIXTURE_MATCH'" in _CREATE_SCHEMA_SQL


def test_fresh_catalogue_creates_market_scope_column_and_index(tmp_path: Path) -> None:
    path = tmp_path / "fresh-catalogue.sqlite"
    store = SqliteApprovedMarketCatalogueStore(path)
    try:
        columns = set(store.table_columns("approved_market_catalogue"))
        assert "market_scope" in columns
        for name, _spec in _CATALOGUE_ADDITIVE_COLUMNS:
            assert name in columns
        assert "idx_approved_catalogue_market_scope" in _index_names(path)
        assert store.list_active() == []
        assert path.exists()
    finally:
        store.close()


def test_legacy_pre_phase_1a_catalogue_migrates_without_deleting_rows(tmp_path: Path) -> None:
    path = tmp_path / "legacy-pre-phase-1a.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(_PRE_PHASE_1A_CATALOGUE_SCHEMA_SQL)
    _insert_legacy_fixture_row(connection)
    connection.commit()
    connection.close()

    before_columns = _table_columns(path)
    assert "market_scope" not in before_columns
    assert "season_id" not in before_columns
    assert "polymarket_clob_token_ids_json" not in before_columns
    assert "polymarket_event_id" in before_columns
    assert _raw_row_count(path) == 1
    inode_before = path.stat().st_ino

    store = SqliteApprovedMarketCatalogueStore(path)
    try:
        _assert_migrated_catalogue(path)
        loaded = store.get_row(LEGACY_ROW_ID)
        assert loaded is not None
        assert loaded.catalogue_row_id == LEGACY_ROW_ID
        assert loaded.canonical_event_id == LEGACY_EVENT_ID
        assert loaded.register_canonical_key == CANONICAL_MATCH_RESULT_FT
        assert loaded.market_scope is MarketScope.FIXTURE_MATCH
        assert loaded.home_canonical == "Arsenal"
        assert loaded.away_canonical == "Chelsea"
        assert loaded.content_version == 1
        assert store.list_active() == [loaded]
    finally:
        store.close()

    assert path.exists()
    assert path.stat().st_ino == inode_before
    assert _raw_row_count(path) == 1


def test_legacy_catalogue_missing_all_additive_columns_migrates_in_place(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-original-catalogue.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(_ORIGINAL_CATALOGUE_TABLE_SQL)
    _insert_legacy_fixture_row(connection)
    connection.commit()
    connection.close()

    before_columns = _table_columns(path)
    for name, _spec in _CATALOGUE_ADDITIVE_COLUMNS:
        assert name not in before_columns
    inode_before = path.stat().st_ino

    store = SqliteApprovedMarketCatalogueStore(path)
    try:
        _assert_migrated_catalogue(path)
        loaded = store.get_row(LEGACY_ROW_ID)
        assert loaded is not None
        assert loaded.market_scope is MarketScope.FIXTURE_MATCH
        assert loaded.polymarket_event_id is None
        assert loaded.polymarket_clob_token_ids == []
        assert loaded.season_id is None
        assert store.list_rows_for_event(LEGACY_EVENT_ID) == [loaded]
    finally:
        store.close()

    assert path.exists()
    assert path.stat().st_ino == inode_before
    assert _raw_row_count(path) == 1
