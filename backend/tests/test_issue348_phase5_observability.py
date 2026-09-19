"""Issue #348 Phase 5: observability behind the critical path.

Clock-injected. Deterministic fixture/demo providers. No live HTTP.
PAPER / read-only. Capture-critical persist_triggered_chain stays on-path.
Audit/history/UI projection are consumers and must not delay pricing.
"""

from __future__ import annotations

import asyncio
import inspect
import threading
import time
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest

from sports_hedge.api import main as main_api
from sports_hedge.api import paper as paper_api
from sports_hedge.application.capture_replay import FORBIDDEN_WRITE_METHODS
from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.price_engine import (
    SCAN_BUDGET_EXHAUSTED_REASON,
    CataloguePriceEngine,
    PriceEnginePriority,
)
from sports_hedge.application.provider_access import (
    HEALTH_CAPACITY_SATURATED,
    HEALTH_MARKET_TIMEOUT,
    HEALTH_OK,
    ProviderAccessLayer,
)
from sports_hedge.application.scanner_observability import ScannerObservabilitySink
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.trades import PaperTradeState
from sports_hedge.persistence.universe_checkpoint import SqliteUniverseCheckpointStore
from test_dual_cadence_scheduler import NOW
from test_issue344_price_engine import (
    DISTANT_KICKOFF,
    FakeKalshi,
    NEAR_KICKOFF,
    _engine,
    _hold_slot,
    _qualifying_decision,
    _row,
)
from test_issue346_item_completion_capture import (
    PerItemScan,
    _audit,
    _bind,
    _item_decision,
    _slice_and_drain,
)
from test_paper_eligible_auto_capture_contract import MB_K
from test_step8f_automatic_paper_entry import _ops_bundle


def _json_walk(value: Any) -> str:
    return repr(value)


@pytest.mark.asyncio
async def test_health_and_build_info_dispatchable_during_hot_background_universe_load(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW)
    coordinator._hot_in_progress = True
    coordinator._background_in_progress = True
    coordinator._universe_in_progress = True
    monkeypatch.setattr(main_api, "get_live_refresh_coordinator", lambda: coordinator)

    stop = asyncio.Event()

    async def occupy() -> None:
        await stop.wait()

    workers = [
        asyncio.create_task(occupy(), name="hot-load"),
        asyncio.create_task(occupy(), name="background-load"),
        asyncio.create_task(occupy(), name="universe-load"),
    ]
    engine = coordinator.price_engine()
    engine.observability.emit(lambda: time.sleep(1.5))

    transport = httpx.ASGITransport(app=main_api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        started = time.monotonic()
        health = await client.get("/health")
        build = await client.get("/build-info")
        elapsed = time.monotonic() - started
        assert health.status_code == 200
        assert build.status_code == 200
        body = health.json()
        assert body["mode"] == "paper"
        assert body["execution_enabled"] is False
        live = body["live_refresh"]
        assert live["hot_in_progress"] is True
        assert live["background_in_progress"] is True
        assert live["universe_in_progress"] is True
        assert elapsed < 0.8

    stop.set()
    await asyncio.gather(*workers)
    await engine.drain_observability()


@pytest.mark.asyncio
async def test_live_refresh_is_read_model_and_never_starts_collector_or_fat_checkpoint(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqliteUniverseCheckpointStore(tmp_path / "ckpt.sqlite")
    coordinator = LiveRefreshCoordinator(
        clock=lambda: NOW, universe_checkpoint_store=store
    )
    calls = {
        "collect_and_scan": 0,
        "collect_report": 0,
        "list_events": 0,
        "list_markets": 0,
        "get_market": 0,
        "get_order_book": 0,
        "load": 0,
    }

    async def boom_collect(*_args: Any, **_kwargs: Any) -> None:
        calls["collect_and_scan"] += 1
        raise AssertionError("live-refresh must not collect_and_scan")

    async def boom_report(*_args: Any, **_kwargs: Any) -> None:
        calls["collect_report"] += 1
        raise AssertionError("live-refresh must not _collect_report")

    original_load = store.load

    def counting_load() -> Any:
        calls["load"] += 1
        return original_load()

    monkeypatch.setattr(ReadOnlyCrossVenueCollector, "collect_and_scan", boom_collect)
    monkeypatch.setattr(paper_api, "_collect_report", boom_report)
    monkeypatch.setattr(store, "load", counting_load)
    monkeypatch.setattr(main_api, "get_live_refresh_coordinator", lambda: coordinator)
    monkeypatch.setattr(paper_api, "get_live_refresh_coordinator", lambda: coordinator)

    source = inspect.getsource(paper_api.live_refresh_status)
    assert "collect_and_scan" not in source
    assert "_collect_report" not in source
    assert "configure_from_settings" not in source
    assert "list_events" not in source
    assert "list_markets" not in source

    transport = httpx.ASGITransport(app=main_api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        before = dict(calls)
        status = await client.get("/paper/live-refresh")
        assert status.status_code == 200
        payload = status.json()
        assert "hot" in payload
        assert "background" in payload
        assert "universe" in payload
        assert "price_engine" in payload
        assert payload["price_engine"]["durable_queue"] is False
        assert SCAN_BUDGET_EXHAUSTED_REASON not in _json_walk(payload["price_engine"])
        assert calls == before


@pytest.mark.asyncio
async def test_price_engine_status_reports_hot_and_background_separately() -> None:
    hot_row = _row(
        suffix="hotstat",
        kickoff=NEAR_KICKOFF,
        matchbook_market_id="316501",
        kalshi_event="KXEPLBTTS-HOTSTAT",
    )
    bg_row = _row(
        suffix="bgstat",
        kickoff=DISTANT_KICKOFF,
        matchbook_market_id="316502",
        kalshi_event="KXEPLBTTS-BGSTAT",
    )
    leftover = _row(
        suffix="left",
        kickoff=DISTANT_KICKOFF,
        matchbook_market_id="316503",
        kalshi_event="KXEPLBTTS-LEFT",
    )
    engine, _mb, _ks, _layer = _engine([hot_row, bg_row, leftover])
    await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
    await engine.run_slice(
        PriceEnginePriority.BACKGROUND, slice_wall_seconds=0, now=NOW
    )
    status = engine.public_status(now=NOW)
    dumped = status.model_dump()
    assert "scan_budget_exhausted" not in dumped
    assert SCAN_BUDGET_EXHAUSTED_REASON not in _json_walk(dumped)
    assert status.durable_queue is False
    assert status.hot.working_set == 1
    assert status.hot.evaluated == 1
    assert status.hot.in_flight == 0
    assert status.background.working_set == 2
    assert status.background.not_started_this_cadence == 2
    assert status.background.due == 0
    assert status.hot.queued == status.hot.due
    assert "current cadence" in status.hot.evaluated_definition.casefold() or (
        "cadence" in status.hot.evaluated_definition.casefold()
    )
    snapshot = engine.snapshot()
    assert snapshot["durable_queue"] is False
    assert SCAN_BUDGET_EXHAUSTED_REASON not in _json_walk(snapshot)


@pytest.mark.asyncio
async def test_price_engine_status_never_emits_scan_budget_exhausted() -> None:
    engine, _mb, _ks, _layer = _engine(
        [_row(suffix="nb", kickoff=NEAR_KICKOFF, matchbook_market_id="316510", kalshi_event="KXEPLBTTS-NB")]
    )
    result = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
    assert result.scan_budget_exhausted is False
    payload = engine.public_status(now=NOW).model_dump()
    assert SCAN_BUDGET_EXHAUSTED_REASON not in _json_walk(payload)
    assert "scan_budget_exhausted" not in payload
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW)
    coordinator.bind_price_engine(engine)
    live = coordinator.public_status()
    assert SCAN_BUDGET_EXHAUSTED_REASON not in _json_walk(live.price_engine.model_dump())


@pytest.mark.asyncio
async def test_four_busy_kalshi_slots_are_kalshi_deferred_not_matchbook_failed() -> None:
    access = ProviderAccessLayer(
        {VenueName.MATCHBOOK: 4, VenueName.KALSHI: 4, VenueName.POLYMARKET: 8}
    )
    gate = asyncio.Event()
    holders = [
        asyncio.create_task(_hold_slot(access, VenueName.KALSHI, gate))
        for _ in range(4)
    ]
    await asyncio.sleep(0.05)
    engine, _mb, _ks, _layer = _engine(
        [_row(suffix="cap", kickoff=NEAR_KICKOFF, matchbook_market_id="316520", kalshi_event="KXEPLBTTS-CAP")],
        access=access,
    )
    result = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
    gate.set()
    await asyncio.gather(*holders)
    assert result.provider_capacity_saturated is True
    assert "amc-cap" in result.deferred
    assert result.scan_budget_exhausted is False
    hot = engine.public_status(now=NOW).hot
    kalshi_ops = hot.operation_health.get(VenueName.KALSHI.value) or {}
    assert kalshi_ops.get("order_book") == HEALTH_CAPACITY_SATURATED
    assert hot.venue_health.get(VenueName.MATCHBOOK.value) != "failed"
    assert hot.venue_health.get(VenueName.MATCHBOOK.value) not in {
        "unavailable",
        "timeout",
        "market_timeout",
        "failed",
    }
    matchbook_ops = hot.operation_health.get(VenueName.MATCHBOOK.value) or {}
    if matchbook_ops:
        assert matchbook_ops.get("get_market") == HEALTH_OK


@pytest.mark.asyncio
async def test_kalshi_order_book_timeout_does_not_mark_matchbook_failed() -> None:
    kalshi = FakeKalshi()
    kalshi.hang.add("KXEPLBTTS-TO-BTTS")
    engine, _mb, _ks, _layer = _engine(
        [
            _row(
                suffix="to",
                kickoff=NEAR_KICKOFF,
                matchbook_market_id="316530",
                kalshi_event="KXEPLBTTS-TO",
            )
        ],
        kalshi=kalshi,
        timeout=0.05,
    )
    result = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
    assert "amc-to" in result.retry_wait
    hot = engine.public_status(now=NOW).hot
    kalshi_ops = hot.operation_health.get(VenueName.KALSHI.value) or {}
    assert kalshi_ops.get("order_book") == HEALTH_MARKET_TIMEOUT
    assert hot.venue_health.get(VenueName.KALSHI.value) == HEALTH_MARKET_TIMEOUT
    assert hot.venue_health.get(VenueName.MATCHBOOK.value) not in {
        "failed",
        "unavailable",
        "timeout",
        "market_timeout",
    }
    matchbook_ops = hot.operation_health.get(VenueName.MATCHBOOK.value) or {}
    assert matchbook_ops.get("get_market") == HEALTH_OK
    issues = " ".join(issue.detail for issue in result.issues).casefold()
    assert "matchbook failed" not in issues
    assert "failed" not in (hot.venue_health.get(VenueName.MATCHBOOK.value) or "")


@pytest.mark.asyncio
async def test_hot_and_background_operation_health_stay_independent() -> None:
    kalshi = FakeKalshi()
    kalshi.hang.add("KXEPLBTTS-HOTFAIL-BTTS")
    hot_row = _row(
        suffix="hotfail",
        kickoff=NEAR_KICKOFF,
        matchbook_market_id="316540",
        kalshi_event="KXEPLBTTS-HOTFAIL",
    )
    bg_row = _row(
        suffix="bgsuccess",
        kickoff=DISTANT_KICKOFF,
        matchbook_market_id="316541",
        kalshi_event="KXEPLBTTS-BGSUCCESS",
    )
    engine, _mb, _ks, _layer = _engine(
        [hot_row, bg_row], kalshi=kalshi, timeout=0.05
    )
    hot_result = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
    bg_result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    assert "amc-hotfail" in hot_result.retry_wait
    assert "amc-bgsuccess" in bg_result.evaluated
    status = engine.public_status(now=NOW)
    hot_k = (status.hot.operation_health.get(VenueName.KALSHI.value) or {}).get("order_book")
    bg_k = (status.background.operation_health.get(VenueName.KALSHI.value) or {}).get("order_book")
    assert hot_k == HEALTH_MARKET_TIMEOUT
    assert bg_k == HEALTH_OK
    assert status.background.venue_health.get(VenueName.KALSHI.value) == HEALTH_OK
    assert status.hot.venue_health.get(VenueName.KALSHI.value) == HEALTH_MARKET_TIMEOUT


@pytest.mark.asyncio
async def test_slow_audit_does_not_delay_next_price_item_or_scan_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    audit = _audit(tmp_path)
    started = threading.Event()
    release = threading.Event()

    def slow_audit(*_args: Any, **_kwargs: Any) -> None:
        started.set()
        assert release.wait(timeout=2.0)

    monkeypatch.setattr(paper_api, "record_price_engine_item_audit", slow_audit)
    try:
        rows = [
            _row(
                suffix=f"aud{index}",
                kickoff=NEAR_KICKOFF + timedelta(minutes=index * 10),
                matchbook_event_id=str(8850 + index),
                matchbook_market_id=str(316550 + index),
                kalshi_event=f"KXEPLBTTS-AUD{index}",
            )
            for index in range(2)
        ]
        paper = PerItemScan(lambda _fixture: _item_decision(scan))
        engine, _mb, _ks, _layer = _engine(rows, paper_scan=paper)
        _bind(engine, scan=scan, watchlist=watchlist, ops=ops, audit=audit, monkeypatch=monkeypatch)
        began = time.monotonic()
        result = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
        elapsed = time.monotonic() - began
        assert set(result.evaluated) == {"amc-aud0", "amc-aud1"}
        assert elapsed < 0.6
        await engine.drain_item_captures()
        assert ops.list_active_trades()
        assert started.wait(timeout=1.0)
        assert engine.observability.lag >= 1
        release.set()
        await engine.drain_observability()
        assert "scan_cycle_timeout" not in (result.issues and result.issues[0].detail or "")
    finally:
        release.set()
        repository.close()
        ledger.close()
        audit.close()


@pytest.mark.asyncio
async def test_slow_ui_projection_does_not_block_pricing_and_may_lag() -> None:
    fixture_state = FixtureCurrentStateStore()
    paper = type("Scan", (), {"scan_pair": lambda self, *a, **k: _qualifying_decision(scanned_at=NOW), "market_matcher": None, "cost_resolver": None, "settings": None})()
    rows = [
        _row(
            suffix=f"proj{index}",
            kickoff=NEAR_KICKOFF + timedelta(minutes=index * 10),
            matchbook_event_id=str(8860 + index),
            matchbook_market_id=str(316560 + index),
            kalshi_event=f"KXEPLBTTS-PROJ{index}",
        )
        for index in range(2)
    ]
    engine, _mb, _ks, _layer = _engine(rows, paper_scan=paper, fixture_state=fixture_state)
    gate = threading.Event()
    original = fixture_state.upsert_from_report

    def slow_upsert(*args: Any, **kwargs: Any) -> None:
        gate.wait(timeout=2.0)
        original(*args, **kwargs)

    fixture_state.upsert_from_report = slow_upsert  # type: ignore[method-assign]
    began = time.monotonic()
    result = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
    elapsed = time.monotonic() - began
    assert set(result.evaluated) == {"amc-proj0", "amc-proj1"}
    assert elapsed < 0.6
    promoted = {rows[0].canonical_event_id, rows[1].canonical_event_id}
    assert promoted <= set(engine._promoted_hot_ids)
    assert rows[0].canonical_event_id not in fixture_state.hot_identity_scope(NOW)
    assert rows[1].canonical_event_id not in fixture_state.hot_identity_scope(NOW)
    gate.set()
    await engine.drain_observability()
    scope = set(fixture_state.hot_identity_scope(NOW))
    assert rows[0].canonical_event_id in scope
    assert rows[1].canonical_event_id in scope


@pytest.mark.asyncio
async def test_delayed_audit_cannot_recapture_phase4_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    audit = _audit(tmp_path)
    try:
        row = _row(
            suffix="once5",
            kickoff=NEAR_KICKOFF,
            matchbook_market_id="316570",
            kalshi_event="KXEPLBTTS-ONCE5",
        )
        paper = PerItemScan(lambda _fixture: _item_decision(scan))
        engine, _mb, _ks, _layer = _engine([row], paper_scan=paper)
        chain_calls = _bind(
            engine, scan=scan, watchlist=watchlist, ops=ops, audit=audit, monkeypatch=monkeypatch
        )
        result = await _slice_and_drain(engine, PriceEnginePriority.HOT, now=NOW)
        assert len(ops.list_active_trades()) == 1
        first = len(chain_calls)
        paper_api.record_price_engine_item_audit(
            result.decisions[0], audit=audit, history=[]
        )
        paper_api.record_price_engine_item_audit(
            result.decisions[0], audit=audit, history=[]
        )
        assert len(chain_calls) == first
        assert len(ops.list_active_trades()) == 1
        assert ops.list_active_trades()[0].state is PaperTradeState.OPEN
        source = inspect.getsource(paper_api.record_price_engine_item_audit)
        assert "persist_triggered_chain" not in source
        assert "append_scan" in source
        capture_src = inspect.getsource(paper_api.persist_price_engine_item_capture)
        assert "append_scan" not in capture_src
        assert "persist_triggered_chain" in inspect.getsource(paper_api._persist_decision)
    finally:
        repository.close()
        ledger.close()
        audit.close()


def test_phase5_does_not_create_durable_queue_or_weaken_paper_boundary() -> None:
    sink_src = inspect.getsource(ScannerObservabilitySink)
    engine_src = inspect.getsource(CataloguePriceEngine)
    paper_src = inspect.getsource(paper_api.live_refresh_status)
    assert "CREATE TABLE" not in sink_src
    assert "CREATE TABLE" not in engine_src
    assert "durable_queue" in inspect.getsource(CataloguePriceEngine.public_status)
    assert "collect_and_scan" not in paper_src
    for token in FORBIDDEN_WRITE_METHODS:
        assert token not in engine_src
        assert token not in sink_src
    assert VenueName.MATCHBOOK in MB_K
