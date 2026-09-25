"""Polymarket football UNIVERSE discovery: isolation, horizon, and honesty.

Provider payloads here are fixtures. No owner-live Gamma call.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from time import monotonic
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
from sports_hedge.application.target_competitions import (
    competition_by_code,
    polymarket_series_ids_for_codes,
)
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


NON_FOOTBALL_CODES = ("nfl", "nba", "ncaab", "mlb", "atp", "wta")


@pytest.mark.asyncio
async def test_football_horizon_does_not_drop_non_football_polymarket_events() -> None:
    old = NOW - timedelta(days=10)
    payloads: dict[str, list[dict[str, Any]]] = {}
    expected_ids: list[str] = []
    for code in NON_FOOTBALL_CODES:
        competition = competition_by_code(code)
        assert competition is not None
        series_id = str(competition.polymarket_gamma_series_id)
        expected_ids.append(series_id)
        payloads[series_id] = [
            _event(
                f"{code}-old",
                f"{competition.display_name} home vs away",
                old,
                series_id=series_id,
            )
        ]
    payloads[EPL] = [
        _event("epl-old", "Arsenal vs Chelsea", old, series_id=EPL),
    ]
    client = ScriptedPolymarket(payloads)
    report = await _scan(client, (*NON_FOOTBALL_CODES, "premier_league"))
    rows = _rows(report)
    for series_id in expected_ids:
        assert rows[series_id]["status"] == "ok"
        assert rows[series_id]["retained_event_count"] == 1
        assert rows[series_id]["stale_fixture_rejections"] == 0
        assert rows[series_id]["raw_event_count"] == 1
    assert rows[EPL]["stale_fixture_rejections"] == 1
    assert rows[EPL]["retained_event_count"] == 0
    assert report.raw_polymarket_events == len(NON_FOOTBALL_CODES)


class _PageSettings:
    polymarket_gamma_page_limit = 100
    polymarket_gamma_max_pages_per_series = 5


class FairPagedPolymarket:
    """One Gamma page per call. Each call advances a simulated discovery clock."""

    def __init__(
        self,
        pages: dict[str, list[list[dict[str, Any]]]],
        *,
        clock: dict[str, float | None],
        simulated_seconds: float,
    ) -> None:
        self.pages = pages
        self.clock = clock
        self.simulated_seconds = simulated_seconds
        self.calls: list[tuple[str, int]] = []
        self.last_pages_attempted = 0
        self.settings = _PageSettings()

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        series = str(filters.get("series_id") or "")
        page_index = int(filters.get("discovery_page_index") or 0)
        await asyncio.sleep(0.01)
        if self.clock["now"] is None:
            self.clock["now"] = monotonic()
        self.clock["now"] = float(self.clock["now"]) + self.simulated_seconds
        self.calls.append((series, page_index))
        self.last_pages_attempted = 1
        series_pages = self.pages.get(series, [])
        if page_index >= len(series_pages):
            return []
        return list(series_pages[page_index])

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return []

    async def get_order_book(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {"bids": [], "asks": []}


@pytest.mark.asyncio
async def test_football_series_share_the_first_page_before_later_pages(monkeypatch: pytest.MonkeyPatch) -> None:
    series_ids = polymarket_series_ids_for_codes(list(OWNER_SELECTED))
    assert len(series_ids) == 8
    kick = NOW + timedelta(days=1)
    stale_start = NOW - timedelta(days=40)
    pages: dict[str, list[list[dict[str, Any]]]] = {}
    for series_id in series_ids:
        if series_id == FA_CUP:
            pages[series_id] = [
                [
                    _event(f"fa-old-{page}-{index}", "Arsenal vs Chelsea", stale_start)
                    for index in range(100)
                ]
                for page in range(5)
            ]
        else:
            pages[series_id] = [[_event(f"{series_id}-next", "Home vs Away", kick)]]
    clock: dict[str, float | None] = {"now": None}

    def simulated_monotonic() -> float:
        if clock["now"] is None:
            clock["now"] = monotonic()
        return float(clock["now"])

    monkeypatch.setattr("sports_hedge.application.collector.monotonic", simulated_monotonic)
    client = FairPagedPolymarket(pages, clock=clock, simulated_seconds=0.5)
    repository, collector = _collector(client)
    try:
        report = await collector.collect_and_scan(
            selected_competition_codes=list(OWNER_SELECTED),
            enabled_venues=[VenueName.POLYMARKET],
            unbounded_cycle=False,
            cycle_timeout_seconds=4.8,
            polymarket_discovery_now=NOW,
            scan_lane="universe",
        )
    finally:
        repository.close()
    assert [series for series, page in client.calls[:8]] == series_ids
    assert [page for _, page in client.calls[:8]] == [0] * 8
    assert all(page == 0 for _, page in client.calls[:8])
    assert all(index >= 8 for index, (_, page) in enumerate(client.calls) if page > 0)
    rows = _rows(report)
    for series_id in series_ids:
        if series_id == FA_CUP:
            continue
        assert rows[series_id]["status"] == "ok"
        assert rows[series_id]["retained_event_count"] == 1
        assert rows[series_id]["http_attempted"] is True
    fa_row = rows[FA_CUP]
    assert fa_row["http_attempted"] is True
    assert fa_row["pages_attempted"] == 1
    assert fa_row["raw_event_count"] == 100
    assert fa_row["stale_fixture_rejections"] == 100
    assert fa_row["status"] == "incomplete"
    assert fa_row["retryable"] is True
    assert fa_row["empty"] is False
    assert report.scan_diagnostics["canonical_work_set_authoritative"] is False
    assert report.raw_polymarket_events == 7
