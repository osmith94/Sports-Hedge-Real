"""BACKGROUND exact-ID pricing cycles persist into scan-cycle history.

Telemetry only. Uses the already-produced BACKGROUND price-engine result.
No extra discovery/provider call. HOT/UNIVERSE history unchanged.
PAPER / read-only. Clock-injected. Deterministic fixture/demo providers.
"""

from __future__ import annotations

import inspect
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import pytest

from sports_hedge.api import paper as paper_api
from sports_hedge.api import watchlist as watchlist_api
from sports_hedge.application.collector import CollectionReport
from sports_hedge.application.live_refresh import DualCadencePlan, get_live_refresh_coordinator
from sports_hedge.application.price_engine import PriceEngineSliceResult
from sports_hedge.application.provider_access import ProviderAccessLayer
from sports_hedge.application.provider_runtime import (
    SharedProviderRuntime,
    set_shared_provider_runtime,
)
from sports_hedge.application.scan_cycle_audit import (
    BACKGROUND_CYCLE_LANE,
    build_background_price_engine_cycle_report,
    build_paper_scan_cycle_record,
    coerce_cycle_lane,
)
from sports_hedge.application.scan_lanes import OPERATOR_BACKGROUND_PRICING_LABEL, ScanLane
from sports_hedge.domain.models import VenueName
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from test_dual_cadence_scheduler import FakeClock
from test_issue344_price_engine import (
    DISTANT_KICKOFF,
    FakeKalshi,
    FakeMatchbook,
    NOW,
    StubPaperScan,
    _qualifying_decision,
    _row,
)


def _report(
    *,
    started_at: datetime,
    completed_at: datetime | None = None,
    scan_lane: str = ScanLane.HOT.value,
    paper_decisions: list | None = None,
    qualifying_arbs: int = 0,
    venue_health: dict[str, str] | None = None,
    evaluated_count: int = 0,
    fixture_count: int | None = None,
) -> CollectionReport:
    finished = completed_at or started_at + timedelta(seconds=2)
    diagnostics: dict[str, Any] = {"evaluated_count": evaluated_count, "not_evaluated_count": 0}
    if fixture_count is not None:
        diagnostics["fixture_count"] = fixture_count
    return CollectionReport(
        started_at=started_at,
        completed_at=finished,
        scan_lane=scan_lane,
        paper_decisions=paper_decisions or [],
        qualifying_arbs=qualifying_arbs,
        venue_health=venue_health or {"matchbook": "ok", "kalshi": "ok"},
        scan_diagnostics=diagnostics,
    )


def test_coerce_cycle_lane_keeps_background_and_does_not_relabel_hot_universe() -> None:
    assert coerce_cycle_lane("background") == BACKGROUND_CYCLE_LANE
    assert coerce_cycle_lane(BACKGROUND_CYCLE_LANE) == "background"
    assert coerce_cycle_lane(ScanLane.HOT) == "hot"
    assert coerce_cycle_lane(ScanLane.UNIVERSE) == "universe"
    assert coerce_cycle_lane("hot") == "hot"
    assert coerce_cycle_lane("universe") == "universe"


def test_background_price_engine_report_uses_actual_slice_counts_only() -> None:
    decision = _qualifying_decision()
    result = PriceEngineSliceResult(
        evaluated=["amc-bg"],
        not_started=["amc-later"],
        deferred=[],
        decisions=[decision],
        venue_health={"matchbook": "ok", "kalshi": "ok"},
    )
    report = build_background_price_engine_cycle_report(
        result,
        started_at=NOW,
        completed_at=NOW + timedelta(seconds=3),
    )
    assert report.scan_lane == "background"
    assert report.paper_decisions == [decision]
    assert report.qualifying_arbs == 1
    assert report.scan_diagnostics["evaluated_count"] == 1
    assert report.scan_diagnostics["not_evaluated_count"] == 1
    assert report.scan_diagnostics["fixture_count"] == 2
    assert report.scan_diagnostics["price_engine"] is True
    assert report.operator_summary == OPERATOR_BACKGROUND_PRICING_LABEL
    row = build_paper_scan_cycle_record(report, scan_lane="background")
    assert row.scan_lane == "background"
    assert row.fixture_count == 2
    assert row.evaluated_count == 1
    assert row.not_evaluated_count == 1
    assert row.paper_decision_count == 1
    assert row.qualifying_arb_count == 1
    assert row.venue_health["kalshi"] == "ok"
    assert row.matched_event_pairs == 0
    assert row.matched_market_pairs == 0


def test_zero_decision_background_report_still_builds_a_truthful_row() -> None:
    result = PriceEngineSliceResult(venue_health={"matchbook": "ok", "kalshi": "ok"})
    report = build_background_price_engine_cycle_report(
        result, started_at=NOW, completed_at=NOW + timedelta(seconds=1)
    )
    row = build_paper_scan_cycle_record(report, scan_lane="background")
    assert row.scan_lane == "background"
    assert row.paper_decision_count == 0
    assert row.qualifying_arb_count == 0
    assert row.evaluated_count == 0
    assert row.fixture_count == 0
    assert row.venue_health["matchbook"] == "ok"


def test_background_hot_universe_history_is_newest_first(tmp_path: Path) -> None:
    audit = SqlitePaperScanRepository(tmp_path / "order.sqlite")
    try:
        hot = _report(started_at=NOW, scan_lane=ScanLane.HOT.value, evaluated_count=2)
        background = _report(
            started_at=NOW + timedelta(seconds=10),
            scan_lane="background",
            evaluated_count=1,
            fixture_count=1,
        )
        universe = _report(
            started_at=NOW + timedelta(seconds=20),
            scan_lane=ScanLane.UNIVERSE.value,
            evaluated_count=3,
        )
        audit.append_cycle(build_paper_scan_cycle_record(hot, scan_lane=hot.scan_lane))
        audit.append_cycle(
            build_paper_scan_cycle_record(background, scan_lane=background.scan_lane)
        )
        audit.append_cycle(
            build_paper_scan_cycle_record(universe, scan_lane=universe.scan_lane)
        )
        rows = audit.list_cycles(limit=100)
        assert [row.scan_lane for row in rows] == ["universe", "background", "hot"]
        assert all(
            rows[index].started_at >= rows[index + 1].started_at
            for index in range(len(rows) - 1)
        )
        assert rows[1].scan_lane == "background"
        assert rows[1].evaluated_count == 1
        assert rows[0].scan_lane == "universe"
        assert rows[2].scan_lane == "hot"
    finally:
        audit.close()


def _scheduled_background_tick_env(
    monkeypatch,
    tmp_path: Path,
    *,
    with_row: bool,
):
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    if with_row:
        store.upsert_catalogue_row(
            _row(
                suffix="bghist",
                kickoff=DISTANT_KICKOFF,
                matchbook_market_id="316301",
                kalshi_event="KXEPLBTTS-BGHIST",
            )
        )
    matchbook = FakeMatchbook()
    kalshi = FakeKalshi()
    access = ProviderAccessLayer(
        {VenueName.MATCHBOOK: 4, VenueName.KALSHI: 4, VenueName.POLYMARKET: 8}
    )
    matchbook.access = access
    kalshi.access = access
    set_shared_provider_runtime(
        SharedProviderRuntime(matchbook=matchbook, kalshi=kalshi, access=access)
    )
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    coordinator._clock = FakeClock(NOW)
    coordinator._price_engine = None
    coordinator.bind_catalogue_store(store)
    paper = StubPaperScan()
    audit = SqlitePaperScanRepository(tmp_path / "bg-cycles.sqlite")
    collect_calls: list[str] = []
    pm_calls: list[str] = []

    async def forbidden_collect(*args: Any, **kwargs: Any) -> None:
        collect_calls.append("collect")
        raise AssertionError("BACKGROUND telemetry must not trigger discovery")

    monkeypatch.setattr(paper_api, "scheduled_paper_scan_service", lambda: paper)
    monkeypatch.setattr(paper_api, "get_paper_audit_repository", lambda: audit)
    monkeypatch.setattr(watchlist_api, "get_watchlist_repository", lambda: object())
    monkeypatch.setattr(watchlist_api, "get_watchlist_service", lambda repo: object())
    monkeypatch.setattr(paper_api, "persist_price_engine_item_capture", lambda *a, **k: None)
    monkeypatch.setattr(paper_api, "persist_price_engine_item_decision", lambda *a, **k: None)
    monkeypatch.setattr(paper_api, "_collect_report", forbidden_collect)
    monkeypatch.setattr(
        paper_api,
        "_run_paper_position_management",
        lambda *a, **k: pm_calls.append("pm") or [],
    )
    plan = DualCadencePlan(lane="background", reason="background_due")
    return paper_api, coordinator, matchbook, kalshi, paper, plan, audit, collect_calls, pm_calls


@pytest.mark.asyncio
async def test_scheduled_background_tick_persists_one_history_row(tmp_path: Path, monkeypatch) -> None:
    (
        paper_api_mod,
        coordinator,
        matchbook,
        kalshi,
        paper,
        plan,
        audit,
        collect_calls,
        pm_calls,
    ) = _scheduled_background_tick_env(monkeypatch, tmp_path, with_row=True)
    try:
        await paper_api_mod.server_owned_refresh_tick(plan)
        rows = audit.list_cycles(limit=100)
        assert len(rows) == 1
        row = rows[0]
        assert row.scan_lane == "background"
        assert row.paper_decision_count == 1
        assert row.qualifying_arb_count == 1
        assert row.evaluated_count == 1
        assert row.fixture_count == 1
        assert row.matched_event_pairs == 0
        assert paper.calls == 1
        assert matchbook.get_market_calls == [("8801", "316301")]
        assert kalshi.book_calls == ["KXEPLBTTS-BGHIST-BTTS"]
        assert matchbook.list_events_calls == 0
        assert matchbook.list_markets_calls == []
        assert kalshi.list_events_calls == 0
        assert kalshi.list_markets_calls == []
        assert collect_calls == []
        assert pm_calls == []
        assert audit.list_scans(limit=100) == []
    finally:
        coordinator.reset()
        set_shared_provider_runtime(None)
        audit.close()


@pytest.mark.asyncio
async def test_zero_decision_background_cycle_still_appears(tmp_path: Path, monkeypatch) -> None:
    (
        paper_api_mod,
        coordinator,
        matchbook,
        kalshi,
        paper,
        plan,
        audit,
        collect_calls,
        pm_calls,
    ) = _scheduled_background_tick_env(monkeypatch, tmp_path, with_row=False)
    try:
        await paper_api_mod.server_owned_refresh_tick(plan)
        rows = audit.list_cycles(limit=100)
        assert len(rows) == 1
        row = rows[0]
        assert row.scan_lane == "background"
        assert row.paper_decision_count == 0
        assert row.qualifying_arb_count == 0
        assert row.evaluated_count == 0
        assert row.fixture_count == 0
        assert paper.calls == 0
        assert matchbook.get_market_calls == []
        assert kalshi.book_calls == []
        assert matchbook.list_events_calls == 0
        assert kalshi.list_events_calls == 0
        assert collect_calls == []
        assert pm_calls == []
    finally:
        coordinator.reset()
        set_shared_provider_runtime(None)
        audit.close()


@pytest.mark.asyncio
async def test_background_history_persist_does_not_add_provider_or_discovery_calls(
    tmp_path: Path, monkeypatch
) -> None:
    (
        paper_api_mod,
        coordinator,
        matchbook,
        kalshi,
        _paper,
        plan,
        audit,
        collect_calls,
        pm_calls,
    ) = _scheduled_background_tick_env(monkeypatch, tmp_path, with_row=True)
    try:
        await paper_api_mod.server_owned_refresh_tick(plan)
        pricing_gets = list(matchbook.get_market_calls)
        pricing_books = list(kalshi.book_calls)
        result = PriceEngineSliceResult(
            evaluated=["amc-bghist"],
            decisions=[_qualifying_decision()],
            venue_health={"matchbook": "ok", "kalshi": "ok"},
        )
        extra = build_background_price_engine_cycle_report(
            result,
            started_at=NOW + timedelta(seconds=30),
            completed_at=NOW + timedelta(seconds=31),
        )
        await paper_api_mod.persist_background_price_cycle_history(extra, audit=audit)
        assert matchbook.get_market_calls == pricing_gets
        assert kalshi.book_calls == pricing_books
        assert matchbook.list_events_calls == 0
        assert kalshi.list_events_calls == 0
        assert collect_calls == []
        assert pm_calls == []
        rows = audit.list_cycles(limit=100)
        assert [row.scan_lane for row in rows] == ["background", "background"]
        assert rows[0].started_at > rows[1].started_at
    finally:
        coordinator.reset()
        set_shared_provider_runtime(None)
        audit.close()


def test_background_tick_source_persists_without_discovery_or_hot_persist_helper() -> None:
    tick_src = inspect.getsource(paper_api.server_owned_refresh_tick)
    bg_src = tick_src[
        tick_src.index('resolved.lane == "background"') : tick_src.index("ScanLane.HOT.value")
    ]
    assert "run_price_engine_slice" in bg_src
    assert "persist_background_price_cycle_history" in bg_src
    assert "build_background_price_engine_cycle_report" in bg_src
    assert bg_src.index("run_price_engine_slice") < bg_src.index(
        "persist_background_price_cycle_history"
    )
    assert "_collect_report" not in bg_src
    assert "persist_scheduled_collection_report" not in bg_src
    assert "list_events" not in bg_src
    assert "list_markets" not in bg_src
    hot_src = tick_src[tick_src.index("ScanLane.HOT.value") :]
    assert hot_src.index("run_cycle") < hot_src.index("persist_scheduled_collection_report")
    persist_src = inspect.getsource(paper_api.persist_background_price_cycle_history)
    assert "append_cycle" in persist_src
    assert "_persist_collection_report" not in persist_src
    assert "_run_paper_position_management" not in persist_src
    assert "_collect_report" not in persist_src
