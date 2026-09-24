"""Issue #551 BACKGROUND throughput diagnosis and bounded cycle reports.

Synthetic providers only. No live HTTP. Caps and the 8s timeout stay put.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import UTC, datetime, timedelta

import pytest

from sports_hedge.api import paper as paper_api
from sports_hedge.application.cycle_diagnostics import (
    DIAGNOSTIC_MAX_REPORT_BYTES,
    DIAGNOSTIC_MAX_ROWS,
    DIAGNOSTIC_RETENTION_DAYS,
    price_engine_throughput_ceiling,
)
from sports_hedge.application.price_engine import PriceEnginePriority
from sports_hedge.application.scan_cycle_audit import (
    build_background_price_engine_cycle_report,
    build_paper_scan_cycle_record,
)
from sports_hedge.config import Settings
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from test_issue344_price_engine import FakeMatchbook, StubPaperScan, _engine, _row

NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def test_operator_shape_matches_sequential_near_timeout_ceiling() -> None:
    """~4–7 evaluations in 35s is the steady-state ceiling for 4 sequential 8s calls.

    Match result is 1 Matchbook get_market plus 3 Kalshi order books, still
    inside MB4/K4. BACKGROUND has no slice wall, so this is a rate ceiling,
    not a cycle cutoff.
    """

    settings = Settings()
    assert settings.paper_scan_provider_timeout_seconds == 8
    assert settings.paper_scan_matchbook_concurrency == 4
    assert settings.paper_scan_kalshi_concurrency == 4
    assert settings.paper_scan_polymarket_concurrency == 8
    match_result = price_engine_throughput_ceiling(
        matchbook_calls=1,
        kalshi_calls=3,
        polymarket_calls=0,
        seconds_per_call=8.0,
        window_seconds=35.0,
    )
    assert match_result["binding_limit"] == "kalshi_slots"
    assert 4 <= match_result["evaluations_in_window"] <= 8
    assert match_result["provider_slots"] == {
        "matchbook": 4,
        "kalshi": 4,
        "polymarket": 8,
    }
    btts = price_engine_throughput_ceiling(
        matchbook_calls=1,
        kalshi_calls=1,
        seconds_per_call=8.0,
        window_seconds=35.0,
    )
    assert btts["evaluations_in_window"] > 12
    fast = price_engine_throughput_ceiling(
        matchbook_calls=1,
        kalshi_calls=3,
        seconds_per_call=1.0,
        window_seconds=35.0,
    )
    assert fast["evaluations_in_window"] > match_result["evaluations_in_window"] * 5


def test_background_tick_has_no_slice_wall_and_no_discovery() -> None:
    source = inspect.getsource(paper_api.server_owned_refresh_tick)
    background = source.split('resolved.lane == "background"', 1)[1].split(
        "ScanLane.HOT.value", 1
    )[0]
    assert "slice_wall_seconds" not in background
    assert "list_events" not in background
    assert "list_markets" not in background
    hot = source.split("PriceEnginePriority.HOT", 1)[1]
    assert "slice_wall_seconds=hot_wall" in hot


@pytest.mark.asyncio
async def test_zero_wall_marks_every_due_row_not_started_without_calls() -> None:
    rows = [_row(suffix=f"wall{index}", matchbook_market_id=str(7000 + index)) for index in range(12)]
    engine, matchbook, kalshi, _layer = _engine(rows, timeout=0.05, background_interval=0)
    result = await engine.run_slice(PriceEnginePriority.BACKGROUND, slice_wall_seconds=0, now=NOW)
    assert result.evaluated == []
    assert len(result.not_started) == 12
    assert result.retry_wait == []
    assert result.deferred == []
    assert matchbook.get_market_calls == []
    assert kalshi.book_calls == []
    report = result.diagnostic or {}
    assert report["terminals"]["not_started"] == 12
    assert report["leftover_collapsed"] == 12
    assert report["call_shape"]["explicit_slice_wall"] is True
    assert report["call_shape"]["worker_limit"] == 8
    assert "yes_dollars" not in json.dumps(report)


@pytest.mark.asyncio
async def test_timeout_repeat_and_worker_error_are_split() -> None:
    rows = [
        _row(suffix="a", matchbook_market_id="88001", kalshi_event="KX-A", kalshi_tickers=["KX-A-BTTS"]),
        _row(suffix="b", matchbook_market_id="88001", kalshi_event="KX-A", kalshi_tickers=["KX-A-BTTS"]),
        _row(suffix="c", matchbook_market_id="88002", kalshi_event="KX-C", kalshi_tickers=["KX-C-BTTS"]),
    ]

    class RaisingScan(StubPaperScan):
        def scan_pair(self, *args, **kwargs):
            self.calls += 1
            if self.calls == 1:
                raise RuntimeError("economics blew up")
            return super().scan_pair(*args, **kwargs)

    class SlowMatchbook(FakeMatchbook):
        async def get_market(self, event_id, market_id, **filters):
            if str(market_id) == "88002":
                await asyncio.sleep(0.2)
            return await super().get_market(event_id, market_id, **filters)

    engine, _matchbook, _kalshi, _layer = _engine(
        rows,
        matchbook=SlowMatchbook(),
        timeout=0.05,
        background_interval=0,
        paper_scan=RaisingScan(),
    )
    result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    report = result.diagnostic or {}
    assert result.failed
    assert len(result.evaluated) + len(result.failed) + len(result.retry_wait) == 3
    assert result.not_started == []
    assert report["worker_errors"]
    assert report["worker_errors"][0]["type"] == "RuntimeError"
    assert report["terminals"]["failed"] == len(result.failed)
    assert report["terminals"]["retry_wait"] == len(result.retry_wait)
    assert report["repeated_exact_id_calls"] >= 1
    assert report["coalesced_provider_calls"] == 0
    assert report["call_shape"]["explicit_slice_wall"] is False
    assert report["call_shape"]["sequential_within_item"] is True
    stages = {(item["venue"], item["stage"]): item for item in report["stages"]}
    assert stages[("matchbook", "get_market")]["timeout"] >= 1 or stages[("matchbook", "get_market")]["success"] >= 1
    encoded = json.dumps(report)
    assert "yes_dollars" not in encoded
    assert "runners" not in encoded
    assert len(encoded.encode("utf-8")) <= DIAGNOSTIC_MAX_REPORT_BYTES
    cycle = build_background_price_engine_cycle_report(
        result, started_at=NOW, completed_at=NOW + timedelta(seconds=1)
    )
    assert cycle.scan_diagnostics["cycle_diagnostic"]["lane"] == "background"
    assert cycle.scan_diagnostics["not_evaluated_count"] == (
        len(result.not_started) + len(result.deferred) + len(result.retry_wait)
    )


def test_diagnostic_retention_is_seven_days_and_row_capped(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("sports_hedge.persistence.paper.DIAGNOSTIC_MAX_ROWS", 5)
    audit = SqlitePaperScanRepository(tmp_path / "diag.sqlite")
    try:
        old = NOW - timedelta(days=DIAGNOSTIC_RETENTION_DAYS, seconds=1)
        audit.append_cycle_diagnostic(
            cycle_id="old",
            scan_lane="background",
            completed_at=old,
            report={"lane": "background", "note": "stale"},
            now=NOW,
        )
        assert audit.get_cycle_diagnostic("old") is None
        for index in range(8):
            audit.append_cycle_diagnostic(
                cycle_id=f"row-{index}",
                scan_lane="hot",
                completed_at=NOW + timedelta(seconds=index),
                report={"lane": "hot", "due": index},
                now=NOW + timedelta(days=1),
            )
        with audit._connect() as connection:
            count = connection.execute(
                "SELECT COUNT(*) AS n FROM paper_scan_cycle_diagnostics"
            ).fetchone()["n"]
        assert count == 5
        assert audit.get_cycle_diagnostic("row-0") is None
        assert audit.get_cycle_diagnostic("row-7") is not None
        again = audit.get_cycle_diagnostic("row-7")
        audit.append_cycle_diagnostic(
            cycle_id="row-7",
            scan_lane="hot",
            completed_at=NOW,
            report={"lane": "hot", "replaced": True},
            now=NOW + timedelta(days=1),
        )
        assert audit.get_cycle_diagnostic("row-7") == again
        assert DIAGNOSTIC_MAX_ROWS == 8000
    finally:
        audit.close()


@pytest.mark.asyncio
async def test_background_persist_stores_report_without_a_second_provider_pass(tmp_path) -> None:
    rows = [_row(suffix="persist", matchbook_market_id="42")]
    engine, matchbook, kalshi, _layer = _engine(rows, timeout=0.2, background_interval=0)
    result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    calls_before = (list(matchbook.get_market_calls), list(kalshi.book_calls))
    report = build_background_price_engine_cycle_report(
        result, started_at=NOW, completed_at=NOW + timedelta(seconds=2)
    )
    audit = SqlitePaperScanRepository(tmp_path / "cycle.sqlite")
    try:
        await paper_api.persist_background_price_cycle_history(report, audit=audit)
        assert matchbook.get_market_calls == calls_before[0]
        assert kalshi.book_calls == calls_before[1]
        record = build_paper_scan_cycle_record(report, scan_lane="background")
        stored = audit.get_cycle_diagnostic(record.cycle_id)
        assert stored is not None
        assert stored["terminals"]["evaluated"] == len(result.evaluated)
        assert stored["data_kind"] == "scan_cycle_diagnostic"
        assert "Not live quotes" in stored["note"]
    finally:
        audit.close()
