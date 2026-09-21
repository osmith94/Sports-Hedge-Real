"""Issue #465: operator Clear universe / Update existing / Clear & update.

PAPER / read-only. Deterministic coordinator fakes. Not owner-live quotes.
Clear itself performs no provider I/O and does not mutate catalogue, PAPER
trades, Treasury, saved scope, or HOT/BACKGROUND/ACTIVE membership.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api import paper as paper_api
from sports_hedge.api.main import app
from sports_hedge.application.approved_market_catalogue import (
    ApprovedMarketCatalogueRow,
    CatalogueRowState,
    OutcomeNativeId,
    required_outcomes_for_key,
)
from sports_hedge.application.live_refresh import (
    UNIVERSE_LIVE_WORKING_SET_CLEAR_COPY,
    UNIVERSE_OPERATOR_CLEAR_COMPLETENESS,
    UNIVERSE_RUN_MODE_CLEAR_UPDATE,
    UNIVERSE_RUN_MODE_UPDATE,
    LiveRefreshCoordinator,
)
from sports_hedge.application.price_engine import CataloguePriceEngine
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.matching.approved_register import CANONICAL_MATCH_RESULT_FT
from sports_hedge.paper.trades import PaperTradeState
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from sports_hedge.persistence.universe_checkpoint import SqliteUniverseCheckpointStore
from test_dual_cadence_scheduler import NOW, FakeClock, _fixture, _report
from test_operator_universe_scope import _bind_scope, _unbind


def _universe_fixture(canonical_id: str, *, when=NOW):
    return _fixture(canonical_id, kickoff=when + timedelta(days=2), last_seen=when)


def _hot_live_fixture(*, when=NOW):
    return _fixture(
        "live-hot",
        kickoff=when - timedelta(minutes=1),
        in_running=True,
        last_seen=when,
    )


def _catalogue_row(event_id: str) -> ApprovedMarketCatalogueRow:
    key = CANONICAL_MATCH_RESULT_FT
    return ApprovedMarketCatalogueRow(
        catalogue_row_id=f"amc-{event_id}",
        register_canonical_key=key,
        canonical_event_id=event_id,
        competition="Premier League",
        home_canonical="Home",
        away_canonical="Away",
        kickoff_utc=NOW + timedelta(hours=12),
        matchbook_event_id="8801",
        matchbook_market_id="316001",
        matchbook_runner_ids=[
            OutcomeNativeId(outcome=outcome, native_id=f"mb-{event_id}-{outcome}")
            for outcome in required_outcomes_for_key(key)
        ],
        kalshi_event_ticker=f"KX-{event_id}",
        kalshi_market_tickers=[f"KX-{event_id}-GAME"],
        kalshi_outcome_ids=[
            OutcomeNativeId(outcome=outcome, native_id=f"ks-{event_id}-{outcome}")
            for outcome in required_outcomes_for_key(key)
        ],
        family="match_result",
        period="full_time",
        required_outcomes=required_outcomes_for_key(key),
        row_state=CatalogueRowState.ACTIVE,
        first_catalogued_at=NOW,
        last_confirmed_at=NOW,
        content_version=1,
    )


def _inventory_ids(coordinator: LiveRefreshCoordinator) -> set[str]:
    return {item.canonical_event_id for item in coordinator.public_status().discovered_fixtures}


def test_clear_removes_live_universe_rows_generation_and_checkpoint(tmp_path) -> None:
    store = SqliteUniverseCheckpointStore(tmp_path / "universe-clear.sqlite")
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock, universe_checkpoint_store=store)
    coordinator._clock = clock
    leftover = _fixture(
        "ev-left",
        kickoff=NOW + timedelta(days=3),
        evaluation="not_evaluated_scan_deadline",
    )
    coordinator.record_report(
        _report([_universe_fixture("ev-old"), leftover], when=NOW),
        scan_lane=ScanLane.UNIVERSE,
    )
    coordinator.flush_universe_checkpoint()
    assert store.load() is not None
    assert coordinator._universe_generation_started_at is not None
    assert "ev-old" in _inventory_ids(coordinator)

    audit = coordinator.clear_universe_working_set()
    coordinator.flush_universe_checkpoint()
    assert store.load() is None
    assert coordinator._universe_generation_started_at is None
    assert coordinator._universe_evaluated_ids == set()
    assert coordinator._universe_cursor is None
    assert coordinator._universe_discovery_snapshot is None
    assert coordinator._universe_work == {}
    assert coordinator._universe_series_work == {}
    assert "ev-old" not in _inventory_ids(coordinator)
    status = coordinator.public_status()
    assert status.universe.fixture_count == 0
    assert status.universe.evaluated_count == 0
    assert status.universe.resume_cursor is None
    assert audit["mode"] == "clear"
    assert audit["completeness"] == UNIVERSE_OPERATOR_CLEAR_COMPLETENESS
    plan = coordinator.plan_universe_tick(now=clock.now)
    assert plan.generation_resume is False
    assert plan.reuse_discovery is False
    assert plan.skip_event_ids == []
    assert plan.discovery_snapshot is None


def test_clear_does_not_remove_catalogue_history_trades_or_treasury(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalogue = SqliteApprovedMarketCatalogueStore(tmp_path / "catalogue.sqlite")
    catalogue.upsert_catalogue_row(_catalogue_row("evt-epl"))
    engine = CataloguePriceEngine(catalogue_store=catalogue)
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(
        clock=clock, catalogue_store=catalogue, price_engine=engine
    )
    coordinator._clock = clock
    trade = SimpleNamespace(
        trade_id="paper-1",
        opportunity_id="opp-1",
        active_trade_phase=None,
        state=PaperTradeState.OPEN,
        canonical_event_id="evt-open-trade",
    )

    class _Ops:
        def list_active_trades(self) -> list[SimpleNamespace]:
            return [trade]

    monkeypatch.setattr(paper_api, "get_paper_operations_service", lambda *args, **kwargs: _Ops())
    coordinator._active_trades.promote(trade, now=NOW, cadence_seconds=5)
    coordinator.record_report(
        _report([_universe_fixture("ev-board")], when=NOW),
        scan_lane=ScanLane.UNIVERSE,
    )
    cadence_before = coordinator.status.universe.cadence_seconds
    scope_before = list(coordinator.effective_universe_scope().selected_competition_codes)

    coordinator.clear_universe_working_set()

    loaded = catalogue.get_row("amc-evt-epl")
    assert loaded is not None
    assert loaded.canonical_event_id == "evt-epl"
    assert catalogue.list_rows_for_event("evt-epl")
    assert coordinator._active_trades.get("paper-1") is not None
    assert coordinator._open_paper_event_ids() == frozenset({"evt-open-trade"})
    assert list(coordinator.effective_universe_scope().selected_competition_codes) == scope_before
    assert coordinator.status.universe.cadence_seconds == cadence_before
    assert coordinator._hot_in_progress is False
    assert coordinator._background_in_progress is False
    assert coordinator._active_trade_in_progress is False
    src = inspect.getsource(coordinator.clear_universe_working_set)
    assert "reset_active_trade_registry" not in src
    assert "treasury" not in src.casefold()
    catalogue.close()


def test_open_trade_remains_managed_by_active(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    trade = SimpleNamespace(
        trade_id="paper-open",
        opportunity_id="opp-open",
        active_trade_phase=None,
        state=PaperTradeState.OPEN,
        canonical_event_id="evt-open-trade",
    )

    class _Ops:
        def list_active_trades(self) -> list[SimpleNamespace]:
            return [trade]

    monkeypatch.setattr(paper_api, "get_paper_operations_service", lambda *args, **kwargs: _Ops())
    coordinator._active_trades.promote(trade, now=NOW, cadence_seconds=5)
    coordinator.record_report(
        _report([_universe_fixture("ev-unrelated")], when=NOW),
        scan_lane=ScanLane.UNIVERSE,
    )
    coordinator.clear_universe_working_set()
    member = coordinator._active_trades.get("paper-open")
    assert member is not None
    assert member.opportunity_id == "opp-open"
    due = coordinator._active_trades.due_members(NOW)
    assert any(item.trade_id == "paper-open" for item in due)


def test_clear_keeps_hot_live_fixture() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator.record_report(
        _report([_hot_live_fixture()], when=NOW, scan_lane=ScanLane.HOT.value),
        scan_lane=ScanLane.HOT,
    )
    coordinator.record_report(
        _report([_universe_fixture("ev-board")], when=NOW),
        scan_lane=ScanLane.UNIVERSE,
    )
    ids = _inventory_ids(coordinator)
    assert "live-hot" in ids
    assert "ev-board" in ids
    coordinator.clear_universe_working_set()
    ids = _inventory_ids(coordinator)
    assert "live-hot" in ids
    assert "ev-board" not in ids
    hot_count, universe_count = coordinator.fixture_current_state().membership_counts(NOW)
    assert hot_count >= 1
    assert universe_count == 0


def test_clear_performs_no_provider_calls() -> None:
    clear_src = inspect.getsource(LiveRefreshCoordinator.clear_universe_working_set)
    api_clear = inspect.getsource(paper_api.clear_universe_working_set)
    api_run = inspect.getsource(paper_api.run_universe_with_mode)
    for src in (clear_src, api_clear):
        assert "collect_and_scan" not in src
        assert "get_shared_provider_runtime" not in src
        assert "list_events" not in src
        assert "list_markets" not in src
        assert "ReadOnlyCrossVenueCollector" not in src
    assert "clear_universe_working_set" in api_run
    assert UNIVERSE_LIVE_WORKING_SET_CLEAR_COPY in inspect.getsource(
        paper_api.clear_universe_working_set
    )


def test_update_existing_preserves_current_behavior(tmp_path) -> None:
    coordinator, scope_store, settings_store = _bind_scope(tmp_path)
    try:
        coordinator.record_report(
            _report([_universe_fixture("ev-keep")], when=NOW),
            scan_lane=ScanLane.UNIVERSE,
        )
        before = _inventory_ids(coordinator)
        assert "ev-keep" in before
        client = TestClient(app)
        response = client.post("/paper/universe/run", json={"mode": UNIVERSE_RUN_MODE_UPDATE})
        assert response.status_code == 200
        assert "ev-keep" in _inventory_ids(coordinator)
        legacy = client.post("/paper/collect/universe")
        assert legacy.status_code == 200
        assert "ev-keep" in _inventory_ids(coordinator)
    finally:
        _unbind(coordinator, scope_store, settings_store)


def test_clear_and_update_starts_generation_fresh(tmp_path) -> None:
    store = SqliteUniverseCheckpointStore(tmp_path / "fresh.sqlite")
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock, universe_checkpoint_store=store)
    coordinator._clock = clock
    coordinator.record_report(
        _report(
            [
                _universe_fixture("ev-a"),
                _fixture(
                    "ev-b",
                    kickoff=NOW + timedelta(days=3),
                    evaluation="not_evaluated_scan_deadline",
                ),
            ],
            when=NOW,
        ),
        scan_lane=ScanLane.UNIVERSE,
    )
    coordinator._universe_discovery_snapshot = {"matchbook": [{"id": "old"}]}
    coordinator._universe_cursor = "ev-a"
    coordinator._universe_evaluated_ids = {"ev-a"}
    coordinator._next_hot_due = NOW + timedelta(seconds=1_000)
    audit = coordinator.clear_universe_working_set(run_after=True)
    assert audit["mode"] == UNIVERSE_RUN_MODE_CLEAR_UPDATE
    assert coordinator._universe_discovery_snapshot is None
    assert coordinator._universe_cursor is None
    assert coordinator._universe_evaluated_ids == set()
    plan = coordinator.plan_universe_tick(now=clock.now)
    assert plan.lane == "universe"
    assert plan.generation_resume is False
    assert plan.reuse_discovery is False
    assert plan.skip_event_ids == []
    assert plan.discovery_snapshot is None
    assert plan.resume_cursor is None


@pytest.mark.asyncio
async def test_stale_in_flight_generation_cannot_repopulate_after_clear() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator.record_report(
        _report([_universe_fixture("ev-old")], when=NOW),
        scan_lane=ScanLane.UNIVERSE,
    )
    started = asyncio.Event()
    finish = asyncio.Event()

    async def runner():
        on_discovery, on_fixture, on_work = coordinator.universe_collect_callbacks()
        started.set()
        await finish.wait()
        on_work(["ev-stale"], authoritative=True)
        on_fixture(None, _universe_fixture("ev-stale"), [], [])
        on_discovery({"matchbook": [{"id": "stale-event"}]})
        return _report([_universe_fixture("ev-stale")], when=clock.now)

    task = asyncio.create_task(
        coordinator.run_cycle(runner, timeout_seconds=5, scan_lane=ScanLane.UNIVERSE)
    )
    await asyncio.wait_for(started.wait(), timeout=1)
    coordinator.clear_universe_working_set()
    assert "ev-old" not in _inventory_ids(coordinator)
    finish.set()
    await asyncio.wait_for(task, timeout=2)
    ids = _inventory_ids(coordinator)
    assert "ev-stale" not in ids
    assert "ev-old" not in ids
    assert coordinator._universe_stale_callback_count >= 1
    assert "ev-stale" not in coordinator._universe_work
    assert coordinator._universe_discovery_snapshot is None


def test_api_clear_and_run_modes_and_audit(tmp_path, monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator, scope_store, settings_store = _bind_scope(tmp_path)
    audit_repo = SqlitePaperScanRepository(tmp_path / "paper-audit.sqlite")
    monkeypatch.setattr(paper_api, "get_paper_audit_repository", lambda: audit_repo)
    try:
        coordinator.record_report(
            _report([_universe_fixture("ev-board")], when=NOW),
            scan_lane=ScanLane.UNIVERSE,
        )
        client = TestClient(app)
        cleared = client.post("/paper/universe/clear")
        assert cleared.status_code == 200
        body = cleared.json()
        assert body["universe"]["fixture_count"] == 0
        cycles = audit_repo.list_cycles(limit=10)
        assert cycles
        assert cycles[0].completeness == UNIVERSE_OPERATOR_CLEAR_COMPLETENESS
        assert "preserved" in (cycles[0].operator_summary or "").casefold()
        coordinator.record_report(
            _report([_universe_fixture("ev-again")], when=NOW),
            scan_lane=ScanLane.UNIVERSE,
        )
        updated = client.post(
            "/paper/universe/run", json={"mode": UNIVERSE_RUN_MODE_CLEAR_UPDATE}
        )
        assert updated.status_code == 200
        assert "ev-again" not in _inventory_ids(coordinator)
        plan = coordinator.plan_universe_tick(now=coordinator.now())
        assert plan.generation_resume is False
        assert plan.reuse_discovery is False
    finally:
        _unbind(coordinator, scope_store, settings_store)
        audit_repo.close()


def test_clear_allowed_when_scanner_stopped_run_is_not(tmp_path) -> None:
    coordinator, scope_store, settings_store = _bind_scope(tmp_path)
    try:
        coordinator.apply_operator_scanner_stopped(True)
        client = TestClient(app)
        cleared = client.post("/paper/universe/clear")
        assert cleared.status_code == 200
        blocked = client.post("/paper/universe/run", json={"mode": UNIVERSE_RUN_MODE_UPDATE})
        assert blocked.status_code == 409
        blocked_clear_update = client.post(
            "/paper/universe/run", json={"mode": UNIVERSE_RUN_MODE_CLEAR_UPDATE}
        )
        assert blocked_clear_update.status_code == 409
    finally:
        coordinator.apply_operator_scanner_stopped(False)
        _unbind(coordinator, scope_store, settings_store)


def test_frontend_operator_controls_copy() -> None:
    from pathlib import Path

    frontend = Path(__file__).resolve().parents[2] / "frontend"
    scan = (frontend / "components" / "run-paper-scan.tsx").read_text(encoding="utf-8")
    api = (frontend / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "Clear universe" in scan
    assert "Update existing" in scan
    assert "Clear & update" in scan
    assert UNIVERSE_LIVE_WORKING_SET_CLEAR_COPY in scan
    assert "window.confirm" in scan
    assert "universeActionBusy" in scan
    assert "/paper/universe/clear" in api
    assert "/paper/universe/run" in api
    assert "clear_update" in api
