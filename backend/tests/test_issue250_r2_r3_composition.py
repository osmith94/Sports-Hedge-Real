"""Issue #250: R2 scan-cycle history composed with R3 Matchbook session reuse.

Composition-only. Does not change scanner, matching, settlement, thresholds,
cadence, or execution. Characterization against exact accepted heads:

- R1 `4886366eacf53cb253a7120a01f97c3c62302172`
- R2 `bbfe36c5de1cc263fd5dd3a14d7aac50c57cd62e`
- R3 `359bcb35f6d8bc0c894ccf2234624cbcfa35286a`
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from sports_hedge.api import main as main_api
from sports_hedge.api import paper as paper_api
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import (
    HOT_REASON_IN_PLAY,
    ScanLane,
    classify_scan_lane,
    hot_reason_labels,
)
from sports_hedge.application.provider_runtime import (
    SharedProviderRuntime,
    reset_shared_provider_runtime,
    set_shared_provider_runtime,
)
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from sports_hedge.venues.matchbook import (
    MATCHBOOK_SESSION_PATH,
    MatchbookClient,
    get_shared_matchbook_client,
    reset_shared_matchbook_client,
    set_shared_matchbook_client,
)
from test_fixture_lifecycle_eviction import NOW, _fixture, _report
from test_matchbook_session_reliability import (
    FakeMono,
    _EmptyOtherVenue,
    _client_for,
    _events_ok,
    _football_ok,
    _secret_free,
    _settings,
)

REPO_ROOT = Path(__file__).resolve().parents[2]


def _paper_service() -> PaperScanService:
    return PaperScanService(MarketIntelligenceService(SqliteMarketIntelligenceRepository()))


def _stub_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(paper_api, "get_paper_operations_service", lambda *args, **kwargs: object())
    monkeypatch.setattr(paper_api, "_run_paper_position_management", lambda *args, **kwargs: [])


def _wire_shared_collect(
    monkeypatch: pytest.MonkeyPatch,
    settings: Settings,
) -> None:
    monkeypatch.setattr(paper_api, "get_settings", lambda: settings)
    runtime = SharedProviderRuntime(
        settings,
        matchbook=get_shared_matchbook_client(settings),
        polymarket=_EmptyOtherVenue(VenueName.POLYMARKET),
        kalshi=_EmptyOtherVenue(VenueName.KALSHI),
    )
    set_shared_provider_runtime(runtime)


def _wire_health(monkeypatch: pytest.MonkeyPatch, settings: Settings) -> None:
    monkeypatch.setattr(main_api, "get_settings", lambda: settings)


def _persist(report, audit: SqlitePaperScanRepository, monkeypatch: pytest.MonkeyPatch) -> None:
    _stub_persist(monkeypatch)
    paper_api._persist_collection_report(
        report,
        service=object(),
        audit=audit,
        watchlist=object(),
        scan_lane=report.scan_lane,
    )


@pytest.fixture
async def isolated_shared_matchbook() -> Any:
    await reset_shared_matchbook_client()
    await reset_shared_provider_runtime()
    yield
    await reset_shared_provider_runtime()
    await reset_shared_matchbook_client()


@pytest.mark.asyncio
async def test_zero_decision_hot_cycle_persists_one_row_with_shared_session(
    tmp_path: Path,
    isolated_shared_matchbook: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_posts: list[str] = []
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MATCHBOOK_SESSION_PATH:
            session_posts.append(request.method)
            return httpx.Response(200, json={"session-token": "session-hot-zero"})
        if request.url.path == "/edge/rest/lookups/sports":
            return _football_ok()
        if request.url.path == "/edge/rest/events":
            return _events_ok()
        return httpx.Response(404)

    venue, http = await _client_for(handler, settings=settings)
    set_shared_matchbook_client(venue)
    _wire_shared_collect(monkeypatch, settings)
    audit = SqlitePaperScanRepository(tmp_path / "zero-hot.sqlite")
    service = _paper_service()
    try:
        async with http:
            report = await paper_api._collect_report({}, service=service, scan_lane=ScanLane.HOT)
            assert report.paper_decisions == []
            assert report.scan_lane == ScanLane.HOT.value
            _persist(report, audit, monkeypatch)
            assert not venue.closed
            assert venue._session_token == "session-hot-zero"
            assert venue is get_shared_matchbook_client(settings)
        cycles = audit.list_cycles(limit=100)
        assert len(cycles) == 1
        row = cycles[0]
        assert row.scan_lane == "hot"
        assert row.paper_decision_count == 0
        assert row.qualifying_arb_count == 0
        assert audit.list_scans(limit=100) == []
        assert session_posts == ["POST"]
        collect_src = inspect.getsource(paper_api._collect_report)
        persist_src = inspect.getsource(paper_api._persist_collection_report)
        assert "get_shared_provider_runtime(settings)" in collect_src
        assert "_aclose_soon(matchbook" not in collect_src
        assert "audit.append_cycle(" in persist_src
    finally:
        audit.close()


@pytest.mark.asyncio
async def test_hot_and_universe_share_one_login_and_each_cycle_appears_once(
    tmp_path: Path,
    isolated_shared_matchbook: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_posts: list[str] = []
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MATCHBOOK_SESSION_PATH:
            session_posts.append(request.method)
            return httpx.Response(200, json={"session-token": "session-lanes"})
        if request.url.path == "/edge/rest/lookups/sports":
            return _football_ok()
        if request.url.path == "/edge/rest/events":
            return _events_ok()
        return httpx.Response(404)

    venue, http = await _client_for(handler, settings=settings)
    set_shared_matchbook_client(venue)
    _wire_shared_collect(monkeypatch, settings)
    audit = SqlitePaperScanRepository(tmp_path / "lanes.sqlite")
    service = _paper_service()
    try:
        async with http:
            hot = await paper_api._collect_report({}, service=service, scan_lane=ScanLane.HOT)
            universe = await paper_api._collect_report(
                {}, service=service, scan_lane=ScanLane.UNIVERSE
            )
            assert not venue.closed
            _persist(hot, audit, monkeypatch)
            _persist(universe, audit, monkeypatch)
        rows = audit.list_cycles(limit=100)
        assert len(rows) == 2
        lanes = {row.scan_lane for row in rows}
        assert lanes == {"hot", "universe"}
        assert rows[0].cycle_id != rows[1].cycle_id
        assert all(row.paper_decision_count == 0 for row in rows)
        assert audit.list_scans(limit=100) == []
        assert session_posts == ["POST"]
        assert venue._session_token == "session-lanes"
    finally:
        audit.close()


@pytest.mark.asyncio
async def test_venues_health_reuses_session_and_does_not_write_scan_cycles(
    tmp_path: Path,
    isolated_shared_matchbook: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_posts: list[str] = []
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MATCHBOOK_SESSION_PATH:
            session_posts.append(request.method)
            return httpx.Response(200, json={"session-token": "session-health"})
        if request.url.path == "/edge/rest/lookups/sports":
            return _football_ok()
        if request.url.path == "/edge/rest/events":
            return _events_ok()
        return httpx.Response(404)

    venue, http = await _client_for(handler, settings=settings)
    set_shared_matchbook_client(venue)
    _wire_shared_collect(monkeypatch, settings)
    _wire_health(monkeypatch, settings)
    audit = SqlitePaperScanRepository(tmp_path / "health.sqlite")
    service = _paper_service()
    try:
        async with http:
            first = await paper_api._collect_report({}, service=service, scan_lane=ScanLane.HOT)
            _persist(first, audit, monkeypatch)
            health = await main_api.venue_health()
            matchbook = next(item for item in health if item["venue"] == VenueName.MATCHBOOK)
            assert matchbook["ok"] is True
            assert matchbook["authenticated"] is True
            assert not venue.closed
            second = await paper_api._collect_report(
                {}, service=service, scan_lane=ScanLane.UNIVERSE
            )
            _persist(second, audit, monkeypatch)
        rows = audit.list_cycles(limit=100)
        assert len(rows) == 2
        assert {row.scan_lane for row in rows} == {"hot", "universe"}
        assert session_posts == ["POST"]
        health_src = inspect.getsource(main_api.venue_health)
        assert "get_shared_provider_runtime(settings)" in health_src
        assert "MatchbookClient(settings)" not in health_src
        assert "get_shared_provider_runtime(settings)" in health_src
        assert "runtime.matchbook" in health_src
    finally:
        audit.close()


@pytest.mark.asyncio
async def test_429_cooldown_blocks_login_posts_and_cycle_history_is_honest(
    tmp_path: Path,
    isolated_shared_matchbook: None,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_posts: list[str] = []
    mono = FakeMono(10.0)
    settings = _settings()

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == MATCHBOOK_SESSION_PATH:
            session_posts.append(request.method)
            return httpx.Response(429, headers={"Retry-After": "20"}, text="Too Many Requests")
        return httpx.Response(404)

    venue, http = await _client_for(handler, settings=settings, monotonic_clock=mono)
    set_shared_matchbook_client(venue)
    _wire_shared_collect(monkeypatch, settings)
    _wire_health(monkeypatch, settings)
    audit = SqlitePaperScanRepository(tmp_path / "cooldown.sqlite")
    service = _paper_service()
    try:
        async with http:
            hot = await paper_api._collect_report({}, service=service, scan_lane=ScanLane.HOT)
            health = await main_api.venue_health()
            universe = await paper_api._collect_report(
                {}, service=service, scan_lane=ScanLane.UNIVERSE
            )
            _persist(hot, audit, monkeypatch)
            _persist(universe, audit, monkeypatch)
        assert session_posts == ["POST"]
        assert not venue.closed
        matchbook_health = next(item for item in health if item["venue"] == VenueName.MATCHBOOK)
        assert matchbook_health["ok"] is False
        assert matchbook_health["authenticated"] is False
        assert "429" in str(matchbook_health["detail"])
        _secret_free(str(matchbook_health["detail"]))
        rows = audit.list_cycles(limit=100)
        assert len(rows) == 2
        assert {row.scan_lane for row in rows} == {"hot", "universe"}
        for row in rows:
            assert row.venue_health["matchbook"] == "unavailable"
            assert row.degraded is True
            assert row.paper_decision_count == 0
            assert row.last_error is not None
            assert "429" in row.last_error
            assert "SCAN FAILED" not in (row.last_error or "").upper()
            _secret_free(row.last_error)
        assert audit.list_scans(limit=100) == []
    finally:
        audit.close()


def test_hot_roster_and_kalshi_live_data_wording_remain_intact() -> None:
    fixture = _fixture("live", kickoff=NOW - timedelta(minutes=20), in_running=True)
    assert classify_scan_lane(fixture, NOW) is ScanLane.HOT
    assert hot_reason_labels(
        fixture,
        NOW,
        membership=ScanLane.HOT,
        lifecycle=ScanLane.HOT,
        qualifying_promotion=False,
    ) == [HOT_REASON_IN_PLAY]

    store = FixtureCurrentStateStore()
    live = _fixture("live", kickoff=NOW - timedelta(minutes=10), in_running=True)
    distant = _fixture("uni", kickoff=NOW + timedelta(days=4))
    store.upsert_from_report(_report([live, distant]), scan_lane=ScanLane.UNIVERSE, now=NOW)
    inventory = {item.canonical_event_id: item for item in store.inventory(NOW)}
    assert inventory["live"].hot_reasons == [HOT_REASON_IN_PLAY]
    assert inventory["uni"].scan_lane == ScanLane.UNIVERSE.value
    assert inventory["uni"].hot_reasons == []

    page = (REPO_ROOT / "frontend/app/page.tsx").read_text(encoding="utf8")
    roster = (REPO_ROOT / "frontend/lib/hot-fixture-roster-display.ts").read_text(encoding="utf8")
    health = (REPO_ROOT / "frontend/lib/venue-health-display.ts").read_text(encoding="utf8")
    assert 'from "../components/hot-fixtures-panel"' in page
    assert 'from "../components/opportunity-monitor"' in page
    assert 'from "../components/scan-cycle-history-panel"' in page
    assert 'HOT_ROSTER_TITLE = "HOT pricing fixtures"' in roster
    assert "Opportunity Monitor remains the current/near qualifying surface" in roster
    assert 'if (label === "Kalshi") return "Kalshi live data"' in health
    assert "read-only" not in health.casefold()


def test_paper_only_execution_boundary_unchanged() -> None:
    assert not hasattr(MatchbookClient, "place_order")
    assert not hasattr(MatchbookClient, "cancel_order")
    assert not hasattr(MatchbookClient, "sign_order")
    assert MatchbookClient.capabilities.execution_enabled is False
    assert MatchbookClient.capabilities.data_enabled is True
    assert MatchbookClient.capabilities.paper_enabled is True
    paper_src = inspect.getsource(paper_api._collect_report)
    main_src = inspect.getsource(main_api.venue_health)
    lifespan_src = inspect.getsource(main_api.lifespan)
    assert "place_order" not in paper_src
    assert "cancel_order" not in paper_src
    assert "place_order" not in main_src
    assert "aclose_shared_matchbook_client()" in lifespan_src
    assert "get_shared_provider_runtime(settings)" in paper_src
    persist_src = inspect.getsource(paper_api._persist_collection_report)
    status_src = inspect.getsource(paper_api.live_refresh_status)
    helper_src = inspect.getsource(paper_api._status_with_scan_cycles)
    assert "audit.append_cycle(" in persist_src
    assert "_operations_heartbeat(" in status_src
    assert "public_status(" not in status_src
    assert "recent_scan_cycles" in helper_src
