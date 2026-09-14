"""Prove Paper scan history disappearance is a latest-N audit window.

Investigation gate for Wave A / Item 10 at frozen checkpoint
``dfda32e0a3c71a6fadc3c38a61ad12cbfd986d7f``.

Does **not** redesign current-state, delete audit rows, or change append-only
write semantics. Small ``N`` is used instead of creating 100+ records.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.paper import get_paper_audit_repository
from sports_hedge.application.collector import CollectionReport, DiscoveredFixture
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.audit import PaperScanRecord
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from test_paper_audit_repository import SCANNED, make_record

VISIBLE_WINDOW = 5
MARKER_ID = "unique-audit-window-marker-leeds-newcastle"
AUDIT_SOURCE = Path(__file__).resolve().parents[1] / "src" / "sports_hedge" / "persistence" / "paper.py"
API_SOURCE = Path(__file__).resolve().parents[1] / "src" / "sports_hedge" / "api" / "paper.py"
PAGE_SOURCE = Path(__file__).resolve().parents[2] / "frontend" / "app" / "page.tsx"
API_TS_SOURCE = Path(__file__).resolve().parents[2] / "frontend" / "lib" / "api.ts"


def _newer(offset: int, *, record_id: str) -> PaperScanRecord:
    return make_record(
        record_id=record_id,
        canonical_event_id=f"evt-newer-{offset}",
        canonical_market_id=f"mkt-newer-{offset}",
        home_team=f"NewerHome{offset}",
        away_team=f"NewerAway{offset}",
        scanned_at=SCANNED + timedelta(seconds=offset),
    )


def _raw_ids(path: Path) -> list[str]:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute(
            "SELECT record_id FROM paper_scan_records ORDER BY scanned_at DESC"
        ).fetchall()
        return [str(row[0]) for row in rows]
    finally:
        connection.close()


def _universe_report(event_id: str, market_id: str, when: datetime) -> CollectionReport:
    fixture = DiscoveredFixture(
        source=VenueName.MATCHBOOK,
        source_event_id=event_id,
        canonical_event_id=event_id,
        home_team="Leeds United",
        away_team="Newcastle United",
        competition="Premier League",
        kickoff_utc=when + timedelta(days=6),
        last_seen_at=when,
        market_evaluation_state="evaluated",
        opportunity_state="matched",
    )
    return CollectionReport(
        started_at=when,
        completed_at=when,
        paper_decisions=[
            PaperScanDecision(
                canonical_event_id=event_id,
                canonical_market_id=market_id,
                fixture_canonical_event_id=event_id,
                market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=[]),
                scanned_at=when,
            )
        ],
        discovered_fixtures=[fixture],
        scan_lane=ScanLane.UNIVERSE.value,
        venue_health={"matchbook": "ok"},
        operator_summary="audit-window investigation",
    )


def test_production_audit_store_is_insert_only() -> None:
    paper = AUDIT_SOURCE.read_text(encoding="utf-8")
    api = API_SOURCE.read_text(encoding="utf-8")
    assert "INSERT INTO paper_scan_records" in paper
    assert "DELETE FROM paper_scan_records" not in paper
    assert "UPDATE paper_scan_records" not in paper
    assert "DELETE FROM paper_scan_records" not in api
    assert "ON CONFLICT" not in inspect.getsource(SqlitePaperScanRepository.append_scan)


def test_known_audit_row_falls_out_of_latest_n_but_remains_persisted(tmp_path: Path) -> None:
    path = tmp_path / "paper_audit.sqlite"
    repo = SqlitePaperScanRepository(path)
    try:
        repo.append_scan(
            make_record(
                record_id=MARKER_ID,
                canonical_event_id="evt-leeds-newcastle",
                canonical_market_id="mkt-leeds-newcastle-mr",
                home_team="Leeds United",
                away_team="Newcastle United",
                scanned_at=SCANNED,
            )
        )
        for index in range(1, VISIBLE_WINDOW + 1):
            repo.append_scan(_newer(index, record_id=f"newer-{index}"))

        window = repo.list_scans(limit=VISIBLE_WINDOW)
        window_ids = [row.record_id for row in window]
        assert MARKER_ID not in window_ids
        assert window_ids == [f"newer-{index}" for index in range(VISIBLE_WINDOW, 0, -1)]

        wider = repo.list_scans(limit=VISIBLE_WINDOW + 1)
        assert [row.record_id for row in wider][-1] == MARKER_ID
        assert any(
            row.home_team == "Leeds United" and row.away_team == "Newcastle United"
            for row in wider
        )

        stored = _raw_ids(path)
        assert MARKER_ID in stored
        assert len(stored) == VISIBLE_WINDOW + 1
        assert path.exists()
    finally:
        repo.close()


def test_get_paper_scans_default_and_explicit_limit_are_newest_n(tmp_path: Path) -> None:
    path = tmp_path / "api-audit-window.sqlite"
    audit = SqlitePaperScanRepository(path)
    audit.append_scan(
        make_record(record_id=MARKER_ID, scanned_at=SCANNED, home_team="Leeds United")
    )
    for index in range(1, VISIBLE_WINDOW + 1):
        audit.append_scan(_newer(index, record_id=f"api-newer-{index}"))

    app.dependency_overrides[get_paper_audit_repository] = lambda: audit
    client = TestClient(app)
    try:
        bounded = client.get("/paper/scans", params={"limit": VISIBLE_WINDOW})
        assert bounded.status_code == 200
        bounded_ids = [row["record_id"] for row in bounded.json()]
        assert MARKER_ID not in bounded_ids
        assert len(bounded_ids) == VISIBLE_WINDOW

        default = client.get("/paper/scans")
        assert default.status_code == 200
        default_ids = [row["record_id"] for row in default.json()]
        assert MARKER_ID in default_ids
        assert default_ids[-1] == MARKER_ID

        wider = client.get("/paper/scans", params={"limit": 1000})
        assert wider.status_code == 200
        assert [row["record_id"] for row in wider.json()][-1] == MARKER_ID
        sidecar = sqlite3.connect(path)
        try:
            remaining = sidecar.execute(
                "SELECT COUNT(*) FROM paper_scan_records WHERE record_id = ?",
                (MARKER_ID,),
            ).fetchone()[0]
        finally:
            sidecar.close()
        assert remaining == 1
    finally:
        app.dependency_overrides.clear()
        audit.close()


def test_malformed_row_occupies_sql_window_but_is_not_deleted(tmp_path: Path) -> None:
    path = tmp_path / "malformed-window.sqlite"
    repo = SqlitePaperScanRepository(path)
    try:
        repo.append_scan(make_record(record_id="oldest-good", scanned_at=SCANNED))
        repo.append_scan(
            make_record(record_id="broken-window-slot", scanned_at=SCANNED + timedelta(seconds=1))
        )
        repo.append_scan(
            make_record(record_id="newest-good", scanned_at=SCANNED + timedelta(seconds=2))
        )
        sidecar = sqlite3.connect(path)
        sidecar.execute(
            "UPDATE paper_scan_records SET net_edge = 'not-a-decimal' WHERE record_id = ?",
            ("broken-window-slot",),
        )
        sidecar.commit()
        sidecar.close()

        decoded = repo.list_scans(limit=2)
        assert [row.record_id for row in decoded] == ["newest-good"]
        assert "oldest-good" not in [row.record_id for row in decoded]

        summary = repo.summary(since=datetime(2020, 1, 1, tzinfo=UTC))
        assert summary.malformed_count == 1
        assert summary.malformed_issues[0].record_id == "broken-window-slot"
        assert _raw_ids(path) == ["newest-good", "broken-window-slot", "oldest-good"]
    finally:
        repo.close()


def test_radar_ttl_eviction_is_not_the_audit_window(tmp_path: Path) -> None:
    path = tmp_path / "current-state-vs-audit.sqlite"
    repo = SqlitePaperScanRepository(path)
    store = FixtureCurrentStateStore()
    observed = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)
    event_id = "evt-ttl-distinct"
    market_id = "mkt-leeds-newcastle-ttl"
    try:
        repo.append_scan(
            make_record(
                record_id=MARKER_ID,
                canonical_event_id=event_id,
                canonical_market_id=market_id,
                home_team="Leeds United",
                away_team="Newcastle United",
                scanned_at=observed,
            )
        )
        store.upsert_from_report(
            _universe_report(event_id, market_id, observed),
            scan_lane=ScanLane.UNIVERSE,
            now=observed,
        )

        still_current = store.current_radar_rows(
            observed + timedelta(seconds=29),
            universe_ttl_seconds=30,
        )
        assert [row.canonical_event_id for row in still_current] == [event_id]

        expired = store.current_radar_rows(
            observed + timedelta(seconds=30),
            universe_ttl_seconds=30,
        )
        assert expired == []
        assert [row.record_id for row in repo.list_scans(limit=1)] == [MARKER_ID]

        for index in range(1, VISIBLE_WINDOW + 1):
            repo.append_scan(
                make_record(
                    record_id=f"ttl-newer-{index}",
                    canonical_event_id=f"evt-ttl-newer-{index}",
                    canonical_market_id=f"mkt-ttl-newer-{index}",
                    scanned_at=observed + timedelta(seconds=index),
                )
            )
        assert MARKER_ID not in [row.record_id for row in repo.list_scans(limit=VISIBLE_WINDOW)]
        assert MARKER_ID in _raw_ids(path)
        assert store.current_radar_rows(
            observed + timedelta(seconds=30),
            universe_ttl_seconds=30,
        ) == []
    finally:
        repo.close()


def test_frontend_paper_scan_history_is_latest_100_audit_not_radar() -> None:
    page = PAGE_SOURCE.read_text(encoding="utf-8")
    api_ts = API_TS_SOURCE.read_text(encoding="utf-8")
    assert 'getPaperScans("limit=100")' in page
    assert 'query = "limit=100"' in api_ts
    assert "Latest 100 audit observations" in page
    assert "Not current scanner radar" in page
    assert "LATEST 100 AUDIT" in page
    assert "scans.filter" not in page
    assert "dedupe" not in page.lower()
    assert "current_radar_rows" not in page
