"""Polymarket football UNIVERSE discovery: isolation, horizon, and honesty.

Provider payloads here are fixtures. No owner-live Gamma call.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from sports_hedge.api import paper as paper_api
from sports_hedge.application.collector import ReadOnlyCrossVenueCollector, _merge_series_rows
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.opportunity_viability import get_opportunity_viability_cache
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.polymarket_fixture_discovery import (
    classify_polymarket_discovery_event,
)
from sports_hedge.application.scan_lanes import DEFAULT_POST_KICKOFF_CURRENT_RADAR_CEILING
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.venues.rate_limit import ProviderRateLimitedError

NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)
EPL = "10188"
LALIGA = "10193"
BUNDESLIGA = "10194"
FA_CUP = "10314"
CHAMPIONSHIP = "10355"
SELECTED = ("premier_league", "la_liga", "bundesliga")
OWNER_SELECTED = (
    "premier_league",
    "championship",
    "la_liga",
    "carabao_cup",
    "fa_cup",
    "international_friendlies",
    "bundesliga",
    "serie_a",
)


def _event(event_id: str, title: str, start: datetime | None, **extra: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {"id": event_id, "title": title, **extra}
    if start is not None:
        payload["startTime"] = start.isoformat().replace("+00:00", "Z")
    return payload


class ScriptedPolymarket:
    def __init__(
        self,
        payloads: dict[str, list[dict[str, Any]]],
        errors: dict[str, BaseException] | None = None,
    ) -> None:
        self.payloads = payloads
        self.errors = errors or {}
        self.calls: list[str] = []
        self.last_pages_attempted = 0

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        series = str(filters.get("series_id") or "")
        self.calls.append(series)
        self.last_pages_attempted = 2 if series == FA_CUP else 1
        error = self.errors.get(series)
        if error is not None:
            raise error
        return list(self.payloads.get(series, []))

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return []

    async def get_order_book(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {"bids": [], "asks": []}


def _collector(client: ScriptedPolymarket) -> tuple[SqliteMarketIntelligenceRepository, ReadOnlyCrossVenueCollector]:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=None,
        polymarket=client,
        kalshi=None,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    return repository, collector


async def _scan(
    client: ScriptedPolymarket,
    codes: tuple[str, ...] | list[str],
    **kwargs: Any,
):
    repository, collector = _collector(client)
    try:
        report = await collector.collect_and_scan(
            selected_competition_codes=list(codes),
            enabled_venues=[VenueName.POLYMARKET],
            unbounded_cycle=True,
            polymarket_discovery_now=NOW,
            scan_lane="universe",
            **kwargs,
        )
    finally:
        repository.close()
    return report


def _rows(report) -> dict[str, dict[str, Any]]:
    diagnostics = report.scan_diagnostics or {}
    rows = diagnostics.get("polymarket_series_results") or report.series_results.get("polymarket") or []
    return {str(row["series"]): row for row in rows}


def test_horizon_keeps_live_fixtures_and_outrights() -> None:
    future = _event("future", "Arsenal vs Chelsea", NOW + timedelta(days=2))
    current = _event("current", "Arsenal vs Chelsea", NOW - timedelta(hours=1))
    stale = _event("stale", "Arsenal vs Chelsea", NOW - DEFAULT_POST_KICKOFF_CURRENT_RADAR_CEILING - timedelta(minutes=1))
    unknown = _event("unknown", "Arsenal vs Chelsea", None)
    outright = _event("outright", "FA Cup Winner", NOW - timedelta(days=40))
    postponed = _event(
        "postponed",
        "Arsenal vs Chelsea",
        NOW - timedelta(days=3),
        gameStatus="postponed",
    )
    assert classify_polymarket_discovery_event(future, now=NOW) == "future"
    assert classify_polymarket_discovery_event(current, now=NOW) == "current"
    assert classify_polymarket_discovery_event(stale, now=NOW) == "stale_fixture"
    assert classify_polymarket_discovery_event(unknown, now=NOW) == "unknown_start"
    assert classify_polymarket_discovery_event(outright, now=NOW) == "outright_exempt"
    assert classify_polymarket_discovery_event(postponed, now=NOW) == "schedule_exception"


@pytest.mark.asyncio
async def test_multi_series_success_retains_each_competition() -> None:
    kick = NOW + timedelta(days=1)
    client = ScriptedPolymarket(
        {
            EPL: [_event("epl-1", "Arsenal vs Chelsea", kick)],
            LALIGA: [_event("laliga-1", "Real Madrid vs Barcelona", kick)],
            BUNDESLIGA: [_event("bundes-1", "Bayern Munich vs Dortmund", kick)],
        }
    )
    report = await _scan(client, SELECTED)
    assert client.calls == [EPL, LALIGA, BUNDESLIGA]
    rows = _rows(report)
    assert rows[EPL]["status"] == "ok"
    assert rows[EPL]["competition"] == "premier_league"
    assert rows[EPL]["http_attempted"] is True
    assert rows[EPL]["retained_event_count"] == 1
    assert rows[LALIGA]["retained_event_count"] == 1
    assert rows[BUNDESLIGA]["retained_event_count"] == 1
    assert report.raw_polymarket_events == 3
    assert report.venue_health["polymarket"] == "ok"


@pytest.mark.asyncio
async def test_one_series_empty_is_not_provider_unavailable() -> None:
    kick = NOW + timedelta(days=1)
    client = ScriptedPolymarket(
        {
            EPL: [_event("epl-1", "Arsenal vs Chelsea", kick)],
            LALIGA: [],
            BUNDESLIGA: [_event("bundes-1", "Bayern Munich vs Dortmund", kick)],
        }
    )
    report = await _scan(client, SELECTED)
    rows = _rows(report)
    assert rows[LALIGA]["status"] == "ok"
    assert rows[LALIGA]["empty"] is True
    assert rows[LALIGA]["raw_event_count"] == 0
    assert rows[EPL]["retained_event_count"] == 1
    assert rows[BUNDESLIGA]["retained_event_count"] == 1
    assert report.venue_health["polymarket"] == "ok"
    assert report.raw_polymarket_events == 2


@pytest.mark.asyncio
async def test_middle_series_timeout_does_not_block_later_series() -> None:
    kick = NOW + timedelta(days=1)
    client = ScriptedPolymarket(
        {
            EPL: [_event("epl-1", "Arsenal vs Chelsea", kick)],
            BUNDESLIGA: [_event("bundes-1", "Bayern Munich vs Dortmund", kick)],
        },
        errors={LALIGA: TimeoutError("timed out")},
    )
    report = await _scan(client, SELECTED)
    assert client.calls == [EPL, LALIGA, BUNDESLIGA]
    rows = _rows(report)
    assert rows[EPL]["status"] == "ok"
    assert rows[LALIGA]["status"] == "discovery_timeout"
    assert rows[LALIGA]["retryable"] is True
    assert rows[LALIGA]["http_attempted"] is True
    assert rows[BUNDESLIGA]["status"] == "ok"
    assert rows[BUNDESLIGA]["retained_event_count"] == 1
    assert report.venue_health["polymarket"] == "degraded"
    assert report.scan_diagnostics["canonical_work_set_authoritative"] is False


@pytest.mark.asyncio
async def test_middle_series_rate_limit_does_not_block_later_series() -> None:
    kick = NOW + timedelta(days=1)
    client = ScriptedPolymarket(
        {
            EPL: [_event("epl-1", "Arsenal vs Chelsea", kick)],
            BUNDESLIGA: [_event("bundes-1", "Bayern Munich vs Dortmund", kick)],
        },
        errors={LALIGA: ProviderRateLimitedError(5, provider="polymarket")},
    )
    report = await _scan(client, SELECTED)
    rows = _rows(report)
    assert rows[LALIGA]["status"] == "rate_limited"
    assert rows[LALIGA]["retryable"] is True
    assert rows[BUNDESLIGA]["retained_event_count"] == 1
    assert report.venue_health["polymarket"] == "degraded"
    assert report.raw_polymarket_events == 2


@pytest.mark.asyncio
async def test_stale_fa_cup_does_not_crowd_out_current_fixtures() -> None:
    kick = NOW + timedelta(days=1)
    fa_cup_payloads = [
        _event(f"fa-old-{index}", "Arsenal vs Chelsea", NOW - timedelta(days=30 + index))
        for index in range(40)
    ]
    fa_cup_payloads.append(_event("fa-live", "Arsenal vs Chelsea", NOW - timedelta(hours=1)))
    fa_cup_payloads.append(_event("fa-next", "Manchester City vs Liverpool", kick))
    fa_cup_payloads.append(_event("fa-winner", "FA Cup Winner", NOW - timedelta(days=20)))
    client = ScriptedPolymarket(
        {
            EPL: [_event("epl-1", "Newcastle United vs Chelsea", kick)],
            LALIGA: [_event("laliga-1", "Real Madrid vs Barcelona", kick)],
            BUNDESLIGA: [_event("bundes-1", "Bayern Munich vs Dortmund", kick)],
            FA_CUP: fa_cup_payloads,
        }
    )
    codes = ("premier_league", "fa_cup", "la_liga", "bundesliga")
    report = await _scan(client, codes)
    assert client.calls == [EPL, FA_CUP, LALIGA, BUNDESLIGA]
    rows = _rows(report)
    assert rows[FA_CUP]["status"] == "ok"
    assert rows[FA_CUP]["raw_event_count"] == 43
    assert rows[FA_CUP]["stale_fixture_rejections"] == 40
    assert rows[FA_CUP]["retained_event_count"] == 3
    assert rows[FA_CUP]["future_fixture_count"] == 1
    assert rows[FA_CUP]["current_fixture_count"] == 1
    assert rows[FA_CUP]["outright_retained_count"] == 1
    assert len(rows[FA_CUP]["stale_fixture_sample_ids"]) <= 3
    assert rows[EPL]["retained_event_count"] == 1
    assert rows[LALIGA]["retained_event_count"] == 1
    assert rows[BUNDESLIGA]["retained_event_count"] == 1
    assert report.raw_polymarket_events == 6
    evidence = report.scan_diagnostics["polymarket_series_results"]
    assert any(row["series"] == FA_CUP and row["stale_fixture_rejections"] == 40 for row in evidence)
    cache = get_opportunity_viability_cache()
    assert all("fa-old-" not in key[2] for key in cache._source_events)


@pytest.mark.asyncio
async def test_budget_exhausted_series_are_not_started_rather_than_empty() -> None:
    kick = NOW + timedelta(days=1)
    client = ScriptedPolymarket(
        {
            EPL: [_event("epl-1", "Arsenal vs Chelsea", kick)],
            LALIGA: [_event("laliga-1", "Real Madrid vs Barcelona", kick)],
            BUNDESLIGA: [_event("bundes-1", "Bayern Munich vs Dortmund", kick)],
        }
    )
    repository, collector = _collector(client)
    budgets = iter((5.0, 0.0, 0.0))
    collector._discovery_timeout_budget = lambda requested: next(budgets)  # type: ignore[method-assign]
    try:
        report = await collector.collect_and_scan(
            selected_competition_codes=list(SELECTED),
            enabled_venues=[VenueName.POLYMARKET],
            unbounded_cycle=True,
            polymarket_discovery_now=NOW,
            scan_lane="universe",
        )
    finally:
        repository.close()
    assert client.calls == [EPL]
    rows = _rows(report)
    assert rows[EPL]["status"] == "ok"
    assert rows[LALIGA]["status"] == "not_started"
    assert rows[LALIGA]["http_attempted"] is False
    assert rows[LALIGA]["retryable"] is True
    assert rows[LALIGA]["empty"] is False
    assert rows[BUNDESLIGA]["status"] == "not_started"
    assert report.venue_health["polymarket"] == "degraded"
    assert report.scan_diagnostics["canonical_work_set_authoritative"] is False


@pytest.mark.asyncio
async def test_incomplete_snapshot_reuse_is_not_authoritative() -> None:
    kick = NOW + timedelta(days=1)
    client = ScriptedPolymarket({EPL: [_event("epl-1", "Arsenal vs Chelsea", kick)]})
    repository, collector = _collector(client)
    budgets = iter((5.0, 0.0))
    collector._discovery_timeout_budget = lambda requested: next(budgets)  # type: ignore[method-assign]
    captured: dict[str, Any] = {}

    def on_discovery(snapshot: dict[str, Any]) -> None:
        captured["snapshot"] = snapshot

    try:
        first = await collector.collect_and_scan(
            selected_competition_codes=["premier_league", "bundesliga"],
            enabled_venues=[VenueName.POLYMARKET],
            unbounded_cycle=True,
            polymarket_discovery_now=NOW,
            scan_lane="universe",
            on_discovery_complete=on_discovery,
        )
        assert _rows(first)[BUNDESLIGA]["status"] == "not_started"
        assert first.scan_diagnostics["canonical_work_set_authoritative"] is False
        snapshot = {
            "matchbook": [],
            "polymarket": list(captured["snapshot"]["polymarket"]),
            "kalshi": [],
        }
        resumed = await collector.collect_and_scan(
            selected_competition_codes=["premier_league", "bundesliga"],
            enabled_venues=[VenueName.POLYMARKET],
            unbounded_cycle=True,
            polymarket_discovery_now=NOW,
            scan_lane="universe",
            reuse_discovery=True,
            discovery_snapshot=snapshot,
            generation_resume=True,
            prior_series_results=first.series_results,
        )
    finally:
        repository.close()
    assert client.calls == [EPL]
    assert resumed.raw_polymarket_events == 1
    assert _rows(resumed)[BUNDESLIGA]["status"] == "not_started"
    assert resumed.venue_health["polymarket"] == "degraded"
    assert resumed.scan_diagnostics["canonical_work_set_authoritative"] is False


def test_fresh_ok_series_replaces_not_started_diagnostic() -> None:
    merged = _merge_series_rows(
        [{"series": EPL, "status": "not_started", "retryable": True, "event_count": 0}],
        [{"series": EPL, "status": "ok", "retryable": False, "event_count": 4}],
    )
    assert merged == [{"series": EPL, "status": "ok", "retryable": False, "event_count": 4}]


def test_clear_and_update_drops_incomplete_discovery_snapshot() -> None:
    coordinator = LiveRefreshCoordinator()
    coordinator._universe_generation_id = 4
    coordinator._universe_generation_started_at = NOW
    coordinator._universe_discovery_snapshot = {"polymarket": [{"id": "partial"}]}
    coordinator._universe_series_results = {
        "polymarket": [{"series": EPL, "status": "not_started", "retryable": True}]
    }
    audit = coordinator.clear_universe_working_set(run_after=True)
    assert coordinator._universe_discovery_snapshot is None
    assert coordinator._universe_series_results == {}
    assert audit["reuse_discovery"] is False
    assert audit["generation_resume"] is False


def test_update_existing_keeps_open_generation_snapshot() -> None:
    coordinator = LiveRefreshCoordinator()
    coordinator._universe_generation_id = 4
    coordinator._universe_generation_started_at = NOW
    coordinator._universe_discovery_snapshot = {"polymarket": [{"id": "keep"}]}
    coordinator.request_universe_run_now()
    assert coordinator._universe_discovery_snapshot == {"polymarket": [{"id": "keep"}]}


def test_scheduled_universe_does_not_inherit_singular_series_id() -> None:
    scheduled = paper_api.scheduled_collection_kwargs()
    filters = scheduled["polymarket_event_filters"]
    assert "series_id" not in filters
    assert filters == {}


@pytest.mark.asyncio
async def test_explicit_singular_series_id_does_not_apply_to_operator_multi_series_unless_set() -> None:
    kick = NOW + timedelta(days=1)
    client = ScriptedPolymarket(
        {
            EPL: [_event("epl-1", "Arsenal vs Chelsea", kick)],
            LALIGA: [_event("laliga-1", "Real Madrid vs Barcelona", kick)],
            FA_CUP: [_event("fa-1", "Arsenal vs Chelsea", kick)],
        }
    )
    plural = await _scan(client, ("premier_league", "la_liga"))
    assert client.calls == [EPL, LALIGA]
    assert plural.raw_polymarket_events == 2
    singular_client = ScriptedPolymarket(
        {FA_CUP: [_event("fa-1", "Arsenal vs Chelsea", kick)]}
    )
    singular = await _scan(
        singular_client,
        ("premier_league", "la_liga"),
        polymarket_event_filters={"series_id": FA_CUP},
    )
    assert singular_client.calls == [FA_CUP]
    assert singular.raw_polymarket_events == 1


@pytest.mark.asyncio
async def test_owner_live_shape_keeps_league_fixtures_beside_stale_fa_cup() -> None:
    kick = NOW + timedelta(days=2)
    stale = [
        _event(f"fa-old-{index}", "Arsenal vs Chelsea", NOW - timedelta(days=40))
        for index in range(80)
    ]
    payloads = {
        EPL: [_event("epl-1", "Arsenal vs Chelsea", kick)],
        CHAMPIONSHIP: [_event("champ-1", "Leeds United vs Leicester City", kick)],
        LALIGA: [_event("laliga-1", "Real Madrid vs Barcelona", kick)],
        "10329": [],
        FA_CUP: [
            *stale,
            _event("fa-next", "Manchester City vs Liverpool", kick),
        ],
        "10238": [_event("friendly-1", "England vs France", kick)],
        BUNDESLIGA: [_event("bundes-1", "Bayern Munich vs Dortmund", kick)],
        "10203": [_event("serie-1", "Inter vs Milan", kick)],
    }
    client = ScriptedPolymarket(
        payloads,
        errors={CHAMPIONSHIP: TimeoutError("timed out")},
    )
    report = await _scan(client, OWNER_SELECTED)
    rows = _rows(report)
    assert [row["status"] for row in report.series_results["polymarket"]][1] == "discovery_timeout"
    assert rows[EPL]["status"] == "ok" and rows[EPL]["retained_event_count"] == 1
    assert rows[LALIGA]["retained_event_count"] == 1
    assert rows[BUNDESLIGA]["retained_event_count"] == 1
    assert rows["10203"]["retained_event_count"] == 1
    assert rows[FA_CUP]["raw_event_count"] == 81
    assert rows[FA_CUP]["stale_fixture_rejections"] == 80
    assert rows[FA_CUP]["retained_event_count"] == 1
    assert rows["10329"]["status"] == "ok" and rows["10329"]["empty"] is True
    assert rows[CHAMPIONSHIP]["status"] == "discovery_timeout"
    assert report.venue_health["polymarket"] == "degraded"
    assert report.raw_polymarket_events == 6
    assert report.scan_diagnostics["canonical_work_set_authoritative"] is False
    counts = report.scan_diagnostics["polymarket_series_results"]
    assert len(counts) == 8
