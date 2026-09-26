"""Issue #245: append-only last-100 scan-cycle history.

One row per completed HOT/UNIVERSE refresh cycle, including zero
paper_decisions. Distinct from market-decision ``paper_scan_records``.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from sports_hedge.api import paper as paper_api
from sports_hedge.api.main import app
from sports_hedge.api.paper import get_paper_audit_repository
from sports_hedge.application.collector import CollectionReport, DiscoveredFixture
from sports_hedge.application.scan_cycle_audit import build_paper_scan_cycle_record
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.domain.models import VenueName
from sports_hedge.persistence.paper import SqlitePaperScanRepository

NOW = datetime(2026, 9, 16, 18, 0, tzinfo=UTC)


def _fixture(event_id: str, *, evaluation: str = "evaluated") -> DiscoveredFixture:
    return DiscoveredFixture(
        source=VenueName.MATCHBOOK,
        source_event_id=f"mb-{event_id}",
        canonical_event_id=event_id,
        home_team="Leeds United",
        away_team="Newcastle United",
        competition="Premier League",
        kickoff_utc=NOW + timedelta(minutes=20),
        last_seen_at=NOW,
        market_evaluation_state=evaluation,
        opportunity_state="matched" if evaluation == "evaluated" else "not_evaluated",
        scan_lane=ScanLane.HOT.value,
    )


def _report(
    *,
    started_at: datetime,
    completed_at: datetime | None = None,
    scan_lane: str = ScanLane.HOT.value,
    paper_decisions: list | None = None,
    fixtures: list[DiscoveredFixture] | None = None,
    qualifying_arbs: int = 0,
    venue_health: dict[str, str] | None = None,
    matched_event_pairs: int = 0,
    matched_market_pairs: int = 0,
    universe_generation_id: int | None = None,
    resume_cursor: str | None = None,
) -> CollectionReport:
    fixtures = fixtures or []
    leftover = sum(
        1 for item in fixtures if item.market_evaluation_state == "not_evaluated_scan_deadline"
    )
    evaluated = sum(1 for item in fixtures if item.market_evaluation_state == "evaluated")
    finished = completed_at or started_at + timedelta(seconds=2)
    return CollectionReport(
        started_at=started_at,
        completed_at=finished,
        scan_lane=scan_lane,
        paper_decisions=paper_decisions or [],
        discovered_fixtures=fixtures,
        qualifying_arbs=qualifying_arbs,
        venue_health=venue_health or {"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"},
        matched_event_pairs=matched_event_pairs,
        matched_market_pairs=matched_market_pairs,
        resume_cursor=resume_cursor,
        scan_diagnostics={
            "evaluated_count": evaluated,
            "not_evaluated_count": leftover,
            "universe_generation_id": universe_generation_id,
            "resume_cursor": resume_cursor,
            "completeness": "complete" if leftover == 0 else "deadline_leftover",
            "generation_resume": False,
        },
    )


def _persist(report: CollectionReport, audit: SqlitePaperScanRepository, monkeypatch) -> None:
    monkeypatch.setattr(paper_api, "get_paper_operations_service", lambda *args, **kwargs: object())
    monkeypatch.setattr(paper_api, "_run_paper_position_management", lambda *args, **kwargs: [])
    paper_api._persist_collection_report(
        report,
        service=object(),
        audit=audit,
        watchlist=object(),
        scan_lane=report.scan_lane,
    )


def test_zero_decision_cycle_appends_one_row_without_market_rows(tmp_path: Path, monkeypatch) -> None:
    audit = SqlitePaperScanRepository(tmp_path / "cycles.sqlite")
    try:
        report = _report(started_at=NOW, fixtures=[_fixture("hot-empty")])
        assert report.paper_decisions == []
        _persist(report, audit, monkeypatch)
        cycles = audit.list_cycles(limit=100)
        assert len(cycles) == 1
        row = cycles[0]
        assert row.scan_lane == "hot"
        assert row.paper_decision_count == 0
        assert row.qualifying_arb_count == 0
        assert row.fixture_count == 1
        assert row.evaluated_count == 1
        assert row.matched_event_pairs == 0
        assert row.venue_health["kalshi"] == "ok"
        assert audit.list_scans(limit=100) == []
    finally:
        audit.close()


def test_hot_and_universe_cycles_both_append(tmp_path: Path, monkeypatch) -> None:
    audit = SqlitePaperScanRepository(tmp_path / "lanes.sqlite")
    try:
        hot = _report(
            started_at=NOW,
            scan_lane=ScanLane.HOT.value,
            fixtures=[_fixture("hot-1")],
            matched_event_pairs=1,
            matched_market_pairs=2,
        )
        universe = _report(
            started_at=NOW + timedelta(seconds=30),
            scan_lane=ScanLane.UNIVERSE.value,
            fixtures=[
                _fixture("uni-1"),
                _fixture("uni-2", evaluation="not_evaluated_scan_deadline"),
            ],
            universe_generation_id=7,
            resume_cursor="uni-1",
        )
        _persist(hot, audit, monkeypatch)
        _persist(universe, audit, monkeypatch)
        rows = audit.list_cycles(limit=100)
        assert [row.scan_lane for row in rows] == ["universe", "hot"]
        assert rows[0].universe_generation_id == 7
        assert rows[0].resume_cursor == "uni-1"
        assert rows[0].not_evaluated_count == 1
        assert rows[1].scan_lane == "hot"
        assert rows[1].matched_market_pairs == 2
    finally:
        audit.close()


def test_retry_of_same_completed_cycle_does_not_duplicate(tmp_path: Path, monkeypatch) -> None:
    audit = SqlitePaperScanRepository(tmp_path / "retry.sqlite")
    try:
        report = _report(started_at=NOW, fixtures=[_fixture("hot-retry")])
        _persist(report, audit, monkeypatch)
        _persist(report, audit, monkeypatch)
        audit.append_cycle(build_paper_scan_cycle_record(report, scan_lane=ScanLane.HOT))
        assert len(audit.list_cycles(limit=100)) == 1
        assert len(audit.list_scans(limit=100)) == 0
    finally:
        audit.close()


def test_latest_100_is_newest_first_and_bounded(tmp_path: Path) -> None:
    audit = SqlitePaperScanRepository(tmp_path / "window.sqlite")
    try:
        for index in range(105):
            started = NOW + timedelta(seconds=index)
            report = _report(
                started_at=started,
                completed_at=started + timedelta(seconds=1),
                scan_lane=ScanLane.HOT.value if index % 2 == 0 else ScanLane.UNIVERSE.value,
            )
            audit.append_cycle(build_paper_scan_cycle_record(report, scan_lane=report.scan_lane))
        window = audit.list_cycles(limit=100)
        assert len(window) == 100
        newest = NOW + timedelta(seconds=104)
        oldest_in_window = NOW + timedelta(seconds=5)
        assert window[0].started_at == newest
        assert window[-1].started_at == oldest_in_window
        assert all(
            window[index].started_at >= window[index + 1].started_at
            for index in range(len(window) - 1)
        )
    finally:
        audit.close()


def test_scan_cycle_endpoint_and_live_refresh_poll_see_new_row(
    tmp_path: Path, monkeypatch
) -> None:
    audit = SqlitePaperScanRepository(tmp_path / "api-cycles.sqlite")
    app.dependency_overrides[get_paper_audit_repository] = lambda: audit
    client = TestClient(app)
    try:
        empty = client.get("/paper/scan-cycles")
        assert empty.status_code == 200
        assert empty.json() == []
        report = _report(
            started_at=NOW,
            fixtures=[_fixture("hot-api")],
            matched_event_pairs=3,
            matched_market_pairs=4,
        )
        _persist(report, audit, monkeypatch)
        listed = client.get("/paper/scan-cycles", params={"limit": 100})
        assert listed.status_code == 200
        body = listed.json()
        assert len(body) == 1
        assert body[0]["scan_lane"] == "hot"
        assert body[0]["paper_decision_count"] == 0
        assert body[0]["matched_event_pairs"] == 3
        status = client.get("/paper/live-refresh")
        assert status.status_code == 200
        assert "recent_scan_cycles" not in status.json()
        cycles = client.get("/paper/scan-cycles", params={"limit": 50}).json()
        assert len(cycles) == 1
        assert cycles[0]["cycle_id"] == body[0]["cycle_id"]
        assert client.get("/paper/scans").json() == []
    finally:
        app.dependency_overrides.clear()
        audit.close()


def test_provider_degraded_cycle_is_not_a_scanner_failure_row(tmp_path: Path) -> None:
    report = _report(
        started_at=NOW,
        venue_health={"matchbook": "ok", "polymarket": "ok", "kalshi": "unavailable"},
        fixtures=[_fixture("hot-degraded")],
    )
    row = build_paper_scan_cycle_record(report, scan_lane=ScanLane.HOT)
    assert row.degraded is True
    assert row.last_error is None
    assert row.paper_decision_count == 0
    assert row.venue_health["kalshi"] == "unavailable"
