from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.paper import get_paper_audit_repository
from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.audit import PaperScanRecord
from sports_hedge.persistence.paper import PAPER_SCAN_SCHEMA_VERSION, SqlitePaperScanRepository

SCANNED = datetime(2026, 9, 14, 12, 0, tzinfo=UTC)
KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
SUMMARY_SINCE = datetime(2020, 1, 1, tzinfo=UTC)


def make_record(**overrides: object) -> PaperScanRecord:
    payload: dict[str, object] = {
        "scanned_at": SCANNED,
        "canonical_event_id": "evt-1",
        "canonical_market_id": "mkt-1",
        "competition": "Premier League",
        "home_team": "Arsenal",
        "away_team": "Chelsea",
        "kickoff_utc": KICKOFF,
        "market_family": MarketFamily.BOTH_TEAMS_TO_SCORE,
        "period": FootballPeriod.FULL_TIME,
        "line": None,
        "venues": [VenueName.MATCHBOOK, VenueName.POLYMARKET],
        "source_market_ids": ["mb-1", "pm-1"],
        "mapping_confidence": 0.99,
        "is_arbitrage": True,
        "eligible_for_paper_simulation": True,
        "gross_edge": Decimal("0.04"),
        "net_edge": Decimal("0.03"),
        "executable_stake_gbp": Decimal("100"),
        "guaranteed_profit_gbp": Decimal("3"),
        "execution_risk_score": 10,
        "execution_risk_band": "low",
        "rejection_reasons": [],
        "decision_json": "{}",
    }
    payload.update(overrides)
    return PaperScanRecord.model_validate(payload)


def _legacy_scan_schema(connection: sqlite3.Connection) -> None:
    connection.executescript(
        """
        CREATE TABLE paper_scan_records (
            record_id TEXT PRIMARY KEY,
            scanned_at TEXT NOT NULL,
            canonical_event_id TEXT NOT NULL,
            canonical_market_id TEXT NOT NULL,
            competition TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            kickoff_utc TEXT NOT NULL,
            market_family TEXT NOT NULL,
            period TEXT NOT NULL,
            line TEXT,
            venues_json TEXT NOT NULL,
            source_market_ids_json TEXT NOT NULL,
            mapping_confidence REAL NOT NULL,
            is_arbitrage INTEGER NOT NULL,
            eligible_for_paper_simulation INTEGER NOT NULL,
            gross_edge TEXT,
            net_edge TEXT,
            executable_stake_gbp TEXT,
            guaranteed_profit_gbp TEXT,
            rejection_reasons_json TEXT NOT NULL,
            decision_json TEXT NOT NULL
        );
        """
    )


def _table_columns(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        return {str(row[1]) for row in connection.execute("PRAGMA table_info(paper_scan_records)")}
    finally:
        connection.close()


def _index_names(path: Path) -> set[str]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'index' AND tbl_name = 'paper_scan_records'"
        ).fetchall()
        return {str(row[0]) for row in rows if row[0] is not None}
    finally:
        connection.close()


def test_file_backed_append_list_summary_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "paper_audit.sqlite"
    repo = SqlitePaperScanRepository(path)
    try:
        repo.append_scan(make_record(record_id="scan-1"))
        rows = repo.list_scans(limit=10)
        assert len(rows) == 1
        assert rows[0].record_id == "scan-1"
        assert rows[0].net_edge == Decimal("0.03")
        summary = repo.summary(since=SUMMARY_SINCE)
        assert summary.scan_count == 1
        assert summary.malformed_count == 0
        assert repo.schema_version() == PAPER_SCAN_SCHEMA_VERSION
    finally:
        repo.close()


def test_empty_string_decimal_is_treated_as_null(tmp_path: Path) -> None:
    path = tmp_path / "paper_audit.sqlite"
    repo = SqlitePaperScanRepository(path)
    try:
        repo.append_scan(make_record(record_id="empty-decimal"))
        sidecar = sqlite3.connect(path)
        sidecar.execute(
            "UPDATE paper_scan_records SET net_edge = '', line = '' WHERE record_id = ?",
            ("empty-decimal",),
        )
        sidecar.commit()
        sidecar.close()
        rows = repo.list_scans(limit=10)
        assert len(rows) == 1
        assert rows[0].net_edge is None
        assert rows[0].line is None
        assert repo.summary(since=SUMMARY_SINCE).malformed_count == 0
    finally:
        repo.close()


def test_malformed_decimal_row_is_preserved_and_does_not_block_reads(tmp_path: Path) -> None:
    path = tmp_path / "paper_audit.sqlite"
    repo = SqlitePaperScanRepository(path)
    try:
        repo.append_scan(
            make_record(record_id="good", canonical_event_id="evt-good", scanned_at=SCANNED)
        )
        repo.append_scan(
            make_record(
                record_id="bad-decimal",
                canonical_event_id="evt-bad",
                scanned_at=SCANNED + timedelta(minutes=1),
            )
        )
        sidecar = sqlite3.connect(path)
        sidecar.execute(
            "UPDATE paper_scan_records SET net_edge = 'not-a-decimal' WHERE record_id = ?",
            ("bad-decimal",),
        )
        sidecar.commit()
        sidecar.close()
        assert path.exists()

        rows = repo.list_scans(limit=10)
        assert [row.record_id for row in rows] == ["good"]
        summary = repo.summary(since=SUMMARY_SINCE)
        assert summary.scan_count == 1
        assert summary.malformed_count == 1
        assert summary.malformed_issues[0].record_id == "bad-decimal"
        assert "invalid decimal" in summary.malformed_issues[0].reason

        repo.append_scan(
            make_record(
                record_id="after-bad",
                canonical_event_id="evt-after",
                scanned_at=SCANNED + timedelta(minutes=2),
            )
        )
        rows = repo.list_scans(limit=10)
        assert [row.record_id for row in rows] == ["after-bad", "good"]
        raw = sqlite3.connect(path)
        count = raw.execute("SELECT COUNT(*) FROM paper_scan_records").fetchone()[0]
        raw.close()
        assert count == 3
        assert path.exists()
    finally:
        repo.close()


def test_legacy_schema_missing_optional_columns_migrates_without_deleting(tmp_path: Path) -> None:
    path = tmp_path / "legacy-audit.sqlite"
    connection = sqlite3.connect(path)
    _legacy_scan_schema(connection)
    scanned = SCANNED.isoformat()
    kickoff = KICKOFF.isoformat()
    connection.execute(
        """
        INSERT INTO paper_scan_records (
            record_id, scanned_at, canonical_event_id, canonical_market_id,
            competition, home_team, away_team, kickoff_utc, market_family, period,
            line, venues_json, source_market_ids_json, mapping_confidence,
            is_arbitrage, eligible_for_paper_simulation, gross_edge, net_edge,
            executable_stake_gbp, guaranteed_profit_gbp, rejection_reasons_json,
            decision_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-ok",
            scanned,
            "evt-legacy",
            "mkt-legacy",
            "Premier League",
            "Arsenal",
            "Chelsea",
            kickoff,
            "both_teams_to_score",
            "full_time",
            '["matchbook"]',
            '["mb-legacy"]',
            0.98,
            1,
            1,
            "0.04",
            "0.03",
            "100",
            "3",
            "[]",
            "{}",
        ),
    )
    connection.commit()
    connection.close()

    repo = SqlitePaperScanRepository(path)
    try:
        assert path.exists()
        rows = repo.list_scans(limit=10)
        assert len(rows) == 1
        assert rows[0].record_id == "legacy-ok"
        assert rows[0].execution_risk_score is None
        assert rows[0].execution_risk_band is None
        assert repo.schema_version() == PAPER_SCAN_SCHEMA_VERSION
        summary = repo.summary(since=SUMMARY_SINCE)
        assert summary.scan_count == 1
        assert summary.malformed_count == 0
    finally:
        repo.close()
    assert path.exists()


def test_legacy_schema_missing_indexed_column_migrates_then_creates_indexes(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-missing-eligible.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE paper_scan_records (
            record_id TEXT PRIMARY KEY,
            scanned_at TEXT NOT NULL,
            canonical_event_id TEXT NOT NULL,
            canonical_market_id TEXT NOT NULL,
            competition TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            kickoff_utc TEXT NOT NULL,
            market_family TEXT NOT NULL,
            period TEXT NOT NULL,
            line TEXT,
            venues_json TEXT NOT NULL,
            source_market_ids_json TEXT NOT NULL,
            mapping_confidence REAL NOT NULL,
            is_arbitrage INTEGER NOT NULL,
            gross_edge TEXT,
            net_edge TEXT,
            executable_stake_gbp TEXT,
            guaranteed_profit_gbp TEXT,
            rejection_reasons_json TEXT NOT NULL,
            decision_json TEXT NOT NULL
        );
        """
    )
    connection.execute(
        """
        INSERT INTO paper_scan_records (
            record_id, scanned_at, canonical_event_id, canonical_market_id,
            competition, home_team, away_team, kickoff_utc, market_family, period,
            line, venues_json, source_market_ids_json, mapping_confidence,
            is_arbitrage, gross_edge, net_edge, executable_stake_gbp,
            guaranteed_profit_gbp, rejection_reasons_json, decision_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-no-eligible",
            SCANNED.isoformat(),
            "evt-legacy-indexed",
            "mkt-legacy-indexed",
            "Premier League",
            "Arsenal",
            "Chelsea",
            KICKOFF.isoformat(),
            "both_teams_to_score",
            "full_time",
            '["matchbook"]',
            '["mb-legacy"]',
            0.98,
            1,
            "0.04",
            "0.03",
            "100",
            "3",
            "[]",
            "{}",
        ),
    )
    connection.commit()
    connection.close()

    repo = SqlitePaperScanRepository(path)
    try:
        columns = _table_columns(path)
        assert "scanned_at" in columns
        assert "eligible_for_paper_simulation" in columns
        assert "canonical_event_id" in columns
        assert _index_names(path) >= {
            "idx_paper_scan_time",
            "idx_paper_scan_eligible_time",
            "idx_paper_scan_event_time",
        }

        assert repo.list_scans(limit=10) == []
        summary = repo.summary(since=SUMMARY_SINCE)
        assert summary.scan_count == 0
        assert summary.malformed_count == 1
        assert summary.malformed_issues[0].record_id == "legacy-no-eligible"

        repo.append_scan(make_record(record_id="current-after-indexed-migration"))
        assert [row.record_id for row in repo.list_scans(limit=10)] == [
            "current-after-indexed-migration"
        ]
        assert repo.summary(since=SUMMARY_SINCE).malformed_count == 1

        raw = sqlite3.connect(path)
        remaining = raw.execute(
            "SELECT record_id FROM paper_scan_records WHERE record_id = ?",
            ("legacy-no-eligible",),
        ).fetchone()
        raw.close()
        assert remaining is not None
        assert path.exists()
    finally:
        repo.close()


def test_legacy_row_missing_required_column_is_surfaced_not_deleted(tmp_path: Path) -> None:
    path = tmp_path / "legacy-missing-required.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE paper_scan_records (
            record_id TEXT PRIMARY KEY,
            scanned_at TEXT NOT NULL,
            canonical_event_id TEXT NOT NULL,
            canonical_market_id TEXT NOT NULL,
            competition TEXT NOT NULL,
            home_team TEXT NOT NULL,
            away_team TEXT NOT NULL,
            kickoff_utc TEXT NOT NULL,
            market_family TEXT NOT NULL,
            period TEXT NOT NULL,
            venues_json TEXT NOT NULL,
            source_market_ids_json TEXT NOT NULL,
            mapping_confidence REAL NOT NULL,
            is_arbitrage INTEGER NOT NULL,
            eligible_for_paper_simulation INTEGER NOT NULL,
            rejection_reasons_json TEXT NOT NULL
        );
        """
    )
    connection.execute(
        """
        INSERT INTO paper_scan_records (
            record_id, scanned_at, canonical_event_id, canonical_market_id,
            competition, home_team, away_team, kickoff_utc, market_family, period,
            venues_json, source_market_ids_json, mapping_confidence, is_arbitrage,
            eligible_for_paper_simulation, rejection_reasons_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "legacy-incomplete",
            SCANNED.isoformat(),
            "evt-incomplete",
            "mkt-incomplete",
            "Premier League",
            "Arsenal",
            "Chelsea",
            KICKOFF.isoformat(),
            "both_teams_to_score",
            "full_time",
            '["matchbook"]',
            '["mb-1"]',
            0.9,
            0,
            0,
            "[]",
        ),
    )
    connection.commit()
    connection.close()

    repo = SqlitePaperScanRepository(path)
    try:
        rows = repo.list_scans(limit=10)
        assert rows == []
        summary = repo.summary(since=SUMMARY_SINCE)
        assert summary.scan_count == 0
        assert summary.malformed_count == 1
        assert summary.malformed_issues[0].record_id == "legacy-incomplete"
        raw = sqlite3.connect(path)
        remaining = raw.execute(
            "SELECT record_id FROM paper_scan_records WHERE record_id = ?",
            ("legacy-incomplete",),
        ).fetchone()
        raw.close()
        assert remaining is not None
        assert path.exists()
        repo.append_scan(make_record(record_id="current"))
        assert [row.record_id for row in repo.list_scans(limit=10)] == ["current"]
        assert repo.summary(since=SUMMARY_SINCE).malformed_count == 1
    finally:
        repo.close()


def test_windows_relevant_concurrent_append_list_summary(tmp_path: Path) -> None:
    path = tmp_path / "concurrent-audit.sqlite"
    repo = SqlitePaperScanRepository(path)
    errors: list[Exception] = []
    writer_count = 24

    def write(index: int) -> None:
        repo.append_scan(
            make_record(
                record_id=str(uuid4()),
                canonical_event_id=f"evt-{index}",
                canonical_market_id=f"mkt-{index}",
                scanned_at=SCANNED + timedelta(milliseconds=index),
            )
        )

    def read() -> None:
        repo.list_scans(limit=100)
        repo.summary(since=SUMMARY_SINCE)

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(write, index) for index in range(writer_count)]
            futures.extend(pool.submit(read) for _ in range(writer_count))
            for future in as_completed(futures):
                try:
                    future.result()
                except Exception as exc:
                    errors.append(exc)
        assert errors == []
        assert len(repo.list_scans(limit=1000)) == writer_count
        assert repo.summary(since=SUMMARY_SINCE).scan_count == writer_count
        assert repo.summary(since=SUMMARY_SINCE).malformed_count == 0
    finally:
        repo.close()


def test_paper_scan_api_does_not_500_on_malformed_legacy_row(tmp_path: Path) -> None:
    path = tmp_path / "api-audit.sqlite"
    audit = SqlitePaperScanRepository(path)
    audit.append_scan(make_record(record_id="visible", scanned_at=SCANNED))
    audit.append_scan(
        make_record(record_id="broken", scanned_at=SCANNED + timedelta(minutes=1))
    )
    sidecar = sqlite3.connect(path)
    sidecar.execute(
        "UPDATE paper_scan_records SET net_edge = 'not-a-decimal' WHERE record_id = ?",
        ("broken",),
    )
    sidecar.commit()
    sidecar.close()

    app.dependency_overrides[get_paper_audit_repository] = lambda: audit
    client = TestClient(app)
    try:
        scans = client.get("/paper/scans")
        assert scans.status_code == 200
        payload = scans.json()
        assert [row["record_id"] for row in payload] == ["visible"]

        summary = client.get("/paper/scans/summary", params={"since": "2020-01-01T00:00:00Z"})
        assert summary.status_code == 200
        body = summary.json()
        assert body["scan_count"] == 1
        assert body["malformed_count"] == 1
        assert body["malformed_issues"][0]["record_id"] == "broken"
    finally:
        app.dependency_overrides.clear()
        audit.close()
