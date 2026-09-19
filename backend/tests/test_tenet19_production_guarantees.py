"""Tenet 19 production-path guarantees after the architect audit.

Deterministic fakes. Not owner-live evidence. Drives the real collector
callback path rather than calling coordinator progress helpers alone.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from time import monotonic
from typing import Any

import httpx
import pytest

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.fixture_clusters import cluster_canonical_event_id
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.hot_identity import hot_scheduling_key
from sports_hedge.application.live_refresh import LiveRefreshCoordinator, _merge_top_level_venue_health
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.provider_access import ProviderAccessLayer, reset_shared_provider_access
from sports_hedge.application.provider_runtime import (
    SharedProviderRuntime,
    aclose_shared_provider_runtime,
    get_shared_provider_runtime,
    reset_shared_provider_runtime,
    set_shared_provider_runtime,
)
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.application.universe_checkpoint import (
    SWEEP_EVALUATED,
    SWEEP_OK,
    SWEEP_RETRY_WAIT,
    series_work_key,
)
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.persistence.universe_checkpoint import SqliteUniverseCheckpointStore
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.polymarket import PolymarketClient
from sports_hedge.venues.rate_limit import ProviderRateLimitedError
from test_concurrent_hot_universe_workers import _named_fixture, _universe_fixture
from test_dual_cadence_scheduler import NOW, FakeClock, _report
from test_issue200_universe_hot_promotion import _market_row

KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
REGULATION = "Resolves based on 90 minutes of regulation time."


@pytest.fixture(autouse=True)
async def _reset_shared() -> Any:
    reset_shared_provider_access()
    await reset_shared_provider_runtime()
    yield
    await reset_shared_provider_runtime()
    reset_shared_provider_access()


def _paper() -> PaperScanService:
    return PaperScanService(MarketIntelligenceService(SqliteMarketIntelligenceRepository()))


class _NoSeriesSettings:
    kalshi_series_tickers: list[str] = []
    kalshi_event_page_limit = 100
    kalshi_event_max_pages = 1
    polymarket_gamma_page_limit = 50
    polymarket_gamma_max_pages_per_series = 1

    def resolved_polymarket_series_ids(self) -> list[str]:
        return []

    def resolved_kalshi_base_url(self) -> str:
        return "https://example.test"


def _mb_event(
    event_id: int,
    home: str,
    away: str,
    *,
    kickoff: datetime = KICKOFF,
    competition: str = "Premier League",
) -> dict[str, Any]:
    return {
        "id": event_id,
        "name": f"{home} vs {away}",
        "start": kickoff.isoformat(),
        "competition-name": competition,
    }


def _pm_event(
    event_id: str,
    home: str,
    away: str,
    *,
    kickoff: datetime = KICKOFF,
    competition: str = "Premier League",
) -> dict[str, Any]:
    return {
        "id": event_id,
        "title": f"{home} vs {away}",
        "startTime": kickoff.isoformat(),
        "competition": competition,
    }


def _k_event(
    ticker: str,
    home: str,
    away: str,
    *,
    series: str = "KXEPLGAME",
    competition: str | None = None,
) -> dict[str, Any]:
    payload = {
        "event_ticker": ticker,
        "series_ticker": series,
        "title": f"{home} vs {away}",
        "category": "Sports",
        "strike_date": KICKOFF.isoformat(),
        "markets": [
            {
                "ticker": f"{ticker}-BTTS",
                "event_ticker": ticker,
                "title": "Both Teams To Score",
                "yes_sub_title": "Yes",
                "rules_primary": REGULATION,
            }
        ],
    }
    if competition:
        payload["competition"] = competition
    return payload


class ScriptedMatchbook:
    def __init__(self, events: list[dict[str, Any]], *, hold_ids: set[str] | None = None) -> None:
        self.events = events
        self.hold_ids = hold_ids or set()
        self.hold = asyncio.Event()
        self.started: dict[str, asyncio.Event] = {item: asyncio.Event() for item in self.hold_ids}
        self.list_events_calls = 0
        self.inflight = 0
        self.peak = 0

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_events_calls += 1
        return {"events": list(self.events)}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        key = str(event_id)
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            if key in self.hold_ids:
                self.started[key].set()
                await self.hold.wait()
            return {"markets": []}
        finally:
            self.inflight -= 1


class ScriptedPolymarket:
    def __init__(self, events: list[dict[str, Any]]) -> None:
        self.events = events
        self.list_events_calls = 0
        self.settings = _NoSeriesSettings()

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        self.list_events_calls += 1
        return list(self.events)

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return []

    async def get_order_book(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {}


class ScriptedKalshi:
    def __init__(
        self,
        events_by_series: dict[str, list[dict[str, Any]]],
        *,
        fail_once: str | None = None,
        fail_times: dict[str, int] | None = None,
        fail_always: set[str] | None = None,
    ) -> None:
        self.events_by_series = events_by_series
        self.fail_once = fail_once
        self.failures = 0
        self.fail_counts: dict[str, int] = {}
        self.fail_budget = dict(fail_times or {})
        if fail_once:
            self.fail_budget.setdefault(fail_once, 1)
        self.fail_always = set(fail_always or [])
        self.series_calls: dict[str, int] = {}
        settings = _NoSeriesSettings()
        settings.kalshi_series_tickers = list(events_by_series)
        self.settings = settings
        self.last_series_report: list[dict[str, Any]] = []

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        ticker = str(filters.get("series_ticker") or "").strip()
        if ticker:
            self.series_calls[ticker] = self.series_calls.get(ticker, 0) + 1
            if ticker in self.fail_always:
                raise TimeoutError("kalshi series timed out")
            budget = self.fail_budget.get(ticker, 0)
            failed = self.fail_counts.get(ticker, 0)
            if failed < budget:
                self.fail_counts[ticker] = failed + 1
                if ticker == self.fail_once:
                    self.failures += 1
                raise TimeoutError("kalshi series timed out")
            events = list(self.events_by_series.get(ticker) or [])
            return {"events": events, "milestones": []}
        events = [item for rows in self.events_by_series.values() for item in rows]
        return {"events": events, "milestones": []}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": []}

    async def get_order_book(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {"orderbook_fp": {"yes_dollars": [["0.40", "100"]], "no_dollars": [["0.49", "200"]]}}

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        return {"ticker": series_ticker, "title": "EPL", "category": "Sports", "fee_type": "quadratic_with_maker_fees"}


def _cluster_home(cluster: Any) -> str:
    return str(cluster.anchor.canonical.home_team)


def _series_unit(coordinator: LiveRefreshCoordinator, venue: str, series: str) -> Any:
    return coordinator._universe_series_work[series_work_key(venue, series)]


def _kalshi_collector(
    kalshi: ScriptedKalshi,
    *,
    matchbook: ScriptedMatchbook | None = None,
    polymarket: ScriptedPolymarket | None = None,
) -> ReadOnlyCrossVenueCollector:
    return ReadOnlyCrossVenueCollector(
        matchbook=matchbook or ScriptedMatchbook([]),
        polymarket=polymarket or ScriptedPolymarket([]),
        kalshi=kalshi,
        paper_scan=_paper(),
        cycle_timeout_seconds=None,
    )


async def _run_universe(
    coordinator: LiveRefreshCoordinator,
    collector: ReadOnlyCrossVenueCollector,
    **kwargs: Any,
) -> Any:
    enabled = kwargs.pop("enabled_venues", [VenueName.KALSHI])

    async def runner() -> Any:
        on_discovery, on_fixture, on_work = coordinator.universe_collect_callbacks()
        return await collector.collect_and_scan(
            scan_lane=ScanLane.UNIVERSE.value,
            unbounded_cycle=True,
            max_event_pairs=8,
            enabled_venues=enabled,
            on_discovery_complete=on_discovery,
            on_fixture_evaluated=on_fixture,
            on_canonical_work_set=on_work,
            **kwargs,
        )

    return await coordinator.run_cycle(runner, timeout_seconds=None, scan_lane=ScanLane.UNIVERSE)


def _force_evaluated(collector: ReadOnlyCrossVenueCollector) -> ReadOnlyCrossVenueCollector:
    original = collector._scan_cluster

    async def gated(cluster: Any, **kwargs: Any) -> Any:
        row = await original(cluster, **kwargs)
        fixture, decisions, inventory, counts, fetched, pairs = row
        fixture = fixture.model_copy(
            update={"market_evaluation_state": "evaluated", "last_scanned_at": NOW}
        )
        return fixture, decisions, inventory, counts, fetched, pairs

    collector._scan_cluster = gated  # type: ignore[method-assign]
    return collector


def _enrich_surveillance(row: Any) -> Any:
    fixture, decisions, inventory, counts, fetched, pairs = row
    fixture = fixture.model_copy(
        update={
            "market_evaluation_state": "evaluated",
            "current_net_edge": Decimal("0.004"),
            "matched_equivalent_count": 1,
            "opportunity_state": "near",
            "solver_is_arbitrage": False,
            "last_scanned_at": NOW,
        }
    )
    return (
        fixture,
        decisions,
        [_market_row(edge=Decimal("0.004"), arb=False, trigger=Decimal("0.01"))],
        counts,
        fetched,
        pairs,
    )


@pytest.mark.asyncio
async def test_fixture_streams_before_collect_returns(tmp_path: Path) -> None:
    matchbook = ScriptedMatchbook(
        [
            _mb_event(1001, "Real Betis", "Getafe", competition="La Liga"),
            _mb_event(1002, "Arsenal", "Liverpool"),
        ],
        hold_ids={"1002"},
    )
    polymarket = ScriptedPolymarket(
        [
            _pm_event("pm-a", "Real Betis", "Getafe", competition="La Liga"),
            _pm_event("pm-b", "Arsenal", "Liverpool"),
        ]
    )
    store = SqliteUniverseCheckpointStore(tmp_path / "stream.sqlite")
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    coordinator.configure_from_settings()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        paper_scan=_paper(),
        cluster_concurrency=2,
        cycle_timeout_seconds=None,
    )
    original = collector._scan_cluster
    a_emitted = asyncio.Event()
    streamed: list[str] = []

    async def gated(cluster: Any, **kwargs: Any) -> Any:
        home = _cluster_home(cluster)
        if "Arsenal" in home:
            matchbook.started["1002"].set()
            await matchbook.hold.wait()
        row = await original(cluster, **kwargs)
        if "Betis" in home or "Getafe" in home:
            row = _enrich_surveillance(row)
            streamed.append(cluster_canonical_event_id(cluster))
            a_emitted.set()
        return row

    collector._scan_cluster = gated  # type: ignore[method-assign]

    async def runner() -> Any:
        on_discovery, on_fixture, on_work = coordinator.universe_collect_callbacks()
        return await collector.collect_and_scan(
            scan_lane=ScanLane.UNIVERSE.value,
            unbounded_cycle=True,
            max_event_pairs=8,
            on_discovery_complete=on_discovery,
            on_fixture_evaluated=on_fixture,
            on_canonical_work_set=on_work,
        )

    task = asyncio.create_task(
        coordinator.run_cycle(runner, timeout_seconds=None, scan_lane=ScanLane.UNIVERSE)
    )
    await asyncio.wait_for(a_emitted.wait(), timeout=5)
    await asyncio.wait_for(matchbook.started["1002"].wait(), timeout=5)
    assert task.done() is False
    assert coordinator._universe_in_progress is True
    assert streamed
    early_id = streamed[0]
    assert coordinator.fixture_current_state().detail(early_id, now=NOW) is not None
    assert early_id in coordinator._universe_evaluated_ids
    payload = store.load()
    assert payload is not None
    work = payload.get("work_units") or {}
    assert work[early_id]["state"] == SWEEP_EVALUATED
    assert early_id in coordinator.fixture_current_state().hot_identity_scope(NOW)
    assert coordinator._universe_sweep_is_complete_unlocked() is False
    matchbook.hold.set()
    report = await asyncio.wait_for(task, timeout=5)
    assert any(item.canonical_event_id for item in report.discovered_fixtures)


@pytest.mark.asyncio
async def test_kalshi_series_failure_is_isolated() -> None:
    settings = _NoSeriesSettings()
    settings.kalshi_series_tickers = ["good", "bad", "also-good"]
    http = httpx.AsyncClient()
    client = KalshiClient(settings, client=http)  # type: ignore[arg-type]

    async def fake_paginate(path: str, *, params: dict[str, Any], **kwargs: Any) -> dict[str, Any]:
        del path, kwargs
        ticker = str(params.get("series_ticker") or "")
        if ticker == "bad":
            raise TimeoutError("timed out")
        return {
            "events": [{"event_ticker": f"{ticker}-1", "title": "Real Betis vs Getafe"}],
            "milestones": [],
        }

    client._paginate = fake_paginate  # type: ignore[method-assign]
    payload = await client.list_events()
    await http.aclose()
    assert len(payload["events"]) == 2
    assert payload["partial"] is True
    statuses = {row["series"]: row["status"] for row in payload["series_results"]}
    assert statuses["good"] == "ok"
    assert statuses["also-good"] == "ok"
    assert statuses["bad"] == "discovery_timeout"
    assert statuses["good"] != statuses["bad"]


@pytest.mark.asyncio
async def test_polymarket_series_failure_is_isolated() -> None:
    settings = _NoSeriesSettings()
    settings.resolved_polymarket_series_ids = lambda: ["101", "202", "303"]  # type: ignore[method-assign]
    http = httpx.AsyncClient()
    client = PolymarketClient(settings, client=http)  # type: ignore[arg-type]

    async def fake_series(series_id: str, base_params: dict[str, Any]) -> list[dict[str, Any]]:
        del base_params
        if series_id == "202":
            raise TimeoutError("timed out")
        return [{"id": f"pm-{series_id}", "title": "Real Betis vs Getafe"}]

    client._list_series_events = fake_series  # type: ignore[method-assign]
    events = await client.list_events()
    await http.aclose()
    assert {item["id"] for item in events} == {"pm-101", "pm-303"}
    statuses = {row["series"]: row["status"] for row in client.last_series_report}
    assert statuses["101"] == "ok"
    assert statuses["303"] == "ok"
    assert statuses["202"] == "discovery_timeout"


@pytest.mark.asyncio
async def test_collector_keeps_successful_series_when_one_times_out() -> None:
    kalshi = ScriptedKalshi(
        {
            "KXOK": [_k_event("ok-1", "Real Betis", "Getafe", series="KXOK", competition="La Liga")],
            "KXBAD": [_k_event("bad-1", "Arsenal", "Liverpool", series="KXBAD", competition="Premier League")],
        },
        fail_once="KXBAD",
    )
    collector = ReadOnlyCrossVenueCollector(
        matchbook=ScriptedMatchbook([]),
        polymarket=ScriptedPolymarket([]),
        kalshi=kalshi,
        paper_scan=_paper(),
        cycle_timeout_seconds=None,
    )
    report = await collector.collect_and_scan(
        scan_lane=ScanLane.UNIVERSE.value,
        unbounded_cycle=True,
        max_event_pairs=8,
        enabled_venues=[VenueName.KALSHI],
    )
    series = {row["series"]: row for row in report.series_results.get("kalshi", [])}
    assert series["KXOK"]["status"] == "ok"
    assert series["KXOK"]["event_count"] >= 1
    assert series["KXBAD"]["status"] == "discovery_timeout"
    assert report.raw_kalshi_events >= 1
    assert report.venue_health["kalshi"] == "degraded"


def test_retryable_failure_then_success_does_not_complete_early() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator.record_universe_work_set(["fix-a", "fix-b"])
    coordinator.record_universe_fixture_progress(None, _universe_fixture("fix-a"), [], [])
    coordinator.record_universe_fixture_progress(
        None,
        _universe_fixture("fix-b", evaluation="market_fetch_unavailable"),
        [],
        [],
    )
    assert coordinator._universe_work["fix-b"].state == SWEEP_RETRY_WAIT
    assert coordinator._universe_sweep_is_complete_unlocked() is False
    assert coordinator.status.universe.canonical_remaining >= 1
    coordinator._universe_in_progress = False
    idle = coordinator.plan_universe_tick(now=clock.now)
    assert idle.reason == "universe_retry_wait"
    clock.advance(3)
    plan = coordinator.plan_universe_tick(now=clock.now)
    assert plan.lane == "universe"
    assert "fix-a" in plan.skip_event_ids
    assert "fix-b" not in plan.skip_event_ids
    coordinator.record_universe_fixture_progress(None, _universe_fixture("fix-b"), [], [])
    assert coordinator._universe_work["fix-b"].state == SWEEP_EVALUATED
    assert coordinator._universe_sweep_is_complete_unlocked() is True
    coordinator._charge_successful_universe_work(0.0, clock.now, leftover_n=0, completeness=None)
    assert coordinator._universe_generation_started_at is None


def test_transient_unavailable_stays_retry_wait_beyond_max_attempts_then_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "sports_hedge.application.live_refresh.get_settings",
        lambda: Settings(paper_universe_work_max_attempts=3),
    )
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator.record_universe_work_set(["only"])
    for _ in range(5):
        coordinator.record_universe_fixture_progress(
            None,
            _universe_fixture("only", evaluation="market_fetch_unavailable"),
            [],
            [],
        )
        clock.advance(20)
    unit = coordinator._universe_work["only"]
    assert unit.state == SWEEP_RETRY_WAIT
    assert unit.attempt_count == 5
    assert unit.retryable is True
    assert coordinator._universe_failed_ids == {}
    assert coordinator._universe_sweep_is_complete_unlocked() is False
    coordinator.record_universe_fixture_progress(None, _universe_fixture("only"), [], [])
    assert coordinator._universe_work["only"].state == SWEEP_EVALUATED
    assert coordinator._universe_sweep_is_complete_unlocked() is True
    coordinator._charge_successful_universe_work(0.0, clock.now, leftover_n=0, completeness=None)
    assert coordinator._universe_generation_started_at is None


def test_fixture_10_failure_does_not_repeat_or_drop_later_fixtures() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    ids = [f"fix-{index:02d}" for index in range(1, 13)]
    coordinator.record_universe_work_set(ids)
    for item in ids:
        if item == "fix-10":
            coordinator.record_universe_fixture_progress(
                None,
                _universe_fixture(item, evaluation="market_fetch_unavailable"),
                [],
                [],
            )
        else:
            coordinator.record_universe_fixture_progress(None, _universe_fixture(item), [], [])
    assert coordinator._universe_work["fix-10"].state == SWEEP_RETRY_WAIT
    assert all(coordinator._universe_work[item].state == SWEEP_EVALUATED for item in ids if item != "fix-10")
    coordinator._universe_in_progress = False
    clock.advance(3)
    plan = coordinator.plan_universe_tick(now=clock.now)
    assert plan.skip_event_ids.count("fix-11") == 1
    assert plan.skip_event_ids.count("fix-12") == 1
    assert "fix-10" not in plan.skip_event_ids
    assert set(plan.skip_event_ids) == set(ids) - {"fix-10"}


@pytest.mark.asyncio
async def test_three_venues_one_canonical_work_unit() -> None:
    seen: list[list[str]] = []
    matchbook = ScriptedMatchbook(
        [_mb_event(1001, "Real Betis", "Getafe", competition="La Liga")]
    )
    polymarket = ScriptedPolymarket(
        [_pm_event("pm-betis", "Real Betis", "Getafe", competition="La Liga")]
    )
    kalshi = ScriptedKalshi(
        {"KXLALIGAGAME": [_k_event("k-betis", "Real Betis", "Getafe", series="KXLALIGAGAME")]}
    )
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        paper_scan=_paper(),
        cycle_timeout_seconds=None,
    )

    def capture(ids: list[str]) -> None:
        seen.append(list(ids))

    report = await collector.collect_and_scan(
        scan_lane=ScanLane.UNIVERSE.value,
        unbounded_cycle=True,
        max_event_pairs=8,
        on_canonical_work_set=capture,
    )
    assert report.raw_matchbook_events == 1
    assert report.raw_polymarket_events == 1
    assert report.raw_kalshi_events == 1
    assert report.scan_diagnostics["canonical_work_total"] == 1
    assert seen
    assert len(seen[0]) == 1
    coordinator = LiveRefreshCoordinator()
    coordinator.record_universe_discovery_snapshot(
        {
            "matchbook": matchbook.events,
            "polymarket": polymarket.events,
            "kalshi": kalshi.events_by_series["KXLALIGAGAME"],
        }
    )
    coordinator.record_universe_work_set(seen[0])
    coordinator.record_universe_fixture_progress(None, _universe_fixture(seen[0][0]), [], [])
    counts = coordinator._canonical_counts_unlocked()
    assert counts["canonical_work_total"] == 1
    assert counts["canonical_evaluated"] == 1
    assert counts["canonical_remaining"] == 0
    assert counts["discovered_total"] == 1
    coordinator._charge_successful_universe_work(0.0, NOW, leftover_n=0, completeness=None)
    assert coordinator._universe_generation_started_at is None


@pytest.mark.asyncio
async def test_shared_runtime_session_and_cooldown_are_process_wide() -> None:
    settings = Settings()
    runtime = SharedProviderRuntime(settings)
    set_shared_provider_runtime(runtime)
    other = get_shared_provider_runtime(settings)
    assert other is runtime
    assert other.polymarket is runtime.polymarket
    assert other.kalshi is runtime.kalshi
    assert other.http_client(VenueName.POLYMARKET) is runtime.polymarket._client
    assert other.http_client(VenueName.KALSHI) is runtime.kalshi._client
    runtime.cooldown(VenueName.POLYMARKET).enter(30)
    with pytest.raises(ProviderRateLimitedError):
        runtime.polymarket._cooldown.raise_if_active()
    with pytest.raises(ProviderRateLimitedError):
        other.cooldown(VenueName.POLYMARKET).raise_if_active()
    await runtime.aclose()
    await runtime.aclose()
    await aclose_shared_provider_runtime()
    assert runtime.close_count == 1
    assert runtime.closed is True


@pytest.mark.asyncio
async def test_shared_runtime_priority_concurrency_and_no_starvation() -> None:
    layer = ProviderAccessLayer({VenueName.POLYMARKET: 1}, starvation_hot_grants=2)
    runtime = SharedProviderRuntime(Settings(), access=layer)
    peak = 0
    current = 0
    lock = asyncio.Lock()
    grants: list[str] = []

    async def occupy(lane: str) -> None:
        nonlocal peak, current
        async with runtime.access.acquire(VenueName.POLYMARKET, lane=lane):
            async with lock:
                current += 1
                peak = max(peak, current)
                grants.append(lane)
            await asyncio.sleep(0.02)
            async with lock:
                current -= 1

    await asyncio.gather(occupy("universe"), occupy("hot"), occupy("universe"))
    assert peak == 1
    assert "hot" in grants
    assert grants.count("universe") == 2
    stop = asyncio.Event()

    async def hot_loop() -> None:
        while not stop.is_set():
            async with runtime.access.acquire(VenueName.POLYMARKET, lane="hot"):
                grants.append("hot")
                await asyncio.sleep(0.005)

    async def universe_once() -> None:
        async with runtime.access.acquire(VenueName.POLYMARKET, lane="universe"):
            grants.append("universe-resume")
            stop.set()

    hot_task = asyncio.create_task(hot_loop())
    await asyncio.wait_for(universe_once(), timeout=1.0)
    hot_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await hot_task
    assert "universe-resume" in grants


def test_scheduling_key_collision_does_not_absorb_canonical_identity() -> None:
    store = FixtureCurrentStateStore()
    left = _named_fixture("betis-src-a", home="Real Betis", away="Getafe")
    right = _named_fixture("betis-src-b", home="Getafe", away="Real Betis")
    assert hot_scheduling_key(left) == hot_scheduling_key(right)
    store.upsert_from_report(_report([left], when=NOW, scan_lane=ScanLane.UNIVERSE.value), scan_lane=ScanLane.UNIVERSE, now=NOW)
    store.upsert_from_report(_report([right], when=NOW, scan_lane=ScanLane.UNIVERSE.value), scan_lane=ScanLane.UNIVERSE, now=NOW)
    assert set(store._rows) == {"betis-src-a", "betis-src-b"}
    assert store._aliases.get("betis-src-a") != "betis-src-b"
    assert store._aliases.get("betis-src-b") != "betis-src-a"
    near_left = left.model_copy(update={"kickoff_utc": NOW + timedelta(minutes=20)})
    near_right = right.model_copy(update={"kickoff_utc": NOW + timedelta(minutes=20)})
    store.clear()
    store.upsert_from_report(
        _report([near_left, near_right], when=NOW, scan_lane=ScanLane.UNIVERSE.value),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    unique, _lifecycle, _promoted = store.hot_membership_breakdown(NOW)
    assert unique == 1
    assert len(store._rows) == 2


def test_trusted_alias_still_merges_one_hot_unit() -> None:
    store = FixtureCurrentStateStore()
    first = _named_fixture("betis-a", home="Real Betis Balompié", away="Getafe CF")
    first = first.model_copy(update={"kickoff_utc": NOW + timedelta(minutes=20)})
    second = _named_fixture("betis-b", home="Real Betis", away="Getafe")
    second = second.model_copy(update={"kickoff_utc": NOW + timedelta(minutes=20)})
    report = _report([first], when=NOW, scan_lane=ScanLane.UNIVERSE.value)
    report = report.model_copy(
        update={
            "fixture_identity_aliases": {"betis-a": "betis-a", "mb-1": "betis-a"},
            "fixture_source_events": {
                "betis-a": [{"venue": "matchbook", "source_event_id": "mb-1", "raw": {"id": "mb-1"}}]
            },
        }
    )
    store.upsert_from_report(report, scan_lane=ScanLane.UNIVERSE, now=NOW)
    later = _report([second], when=NOW, scan_lane=ScanLane.UNIVERSE.value)
    later = later.model_copy(
        update={
            "fixture_identity_aliases": {"betis-b": "betis-b", "mb-1": "betis-b", "betis-a": "betis-b"},
            "fixture_source_events": {
                "betis-b": [{"venue": "matchbook", "source_event_id": "mb-1", "raw": {"id": "mb-1"}}]
            },
        }
    )
    store.upsert_from_report(later, scan_lane=ScanLane.UNIVERSE, now=NOW)
    assert len(store.hot_identity_scope(NOW)) == 1
    assert len(store._rows) == 1


def test_health_merge_is_never_false_green() -> None:
    hot = {"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"}
    assert _merge_top_level_venue_health(hot, {"matchbook": "discovery_timeout"})["matchbook"] == "degraded"
    assert _merge_top_level_venue_health(hot, {"kalshi": "unavailable"})["kalshi"] == "degraded"
    assert _merge_top_level_venue_health(hot, {"kalshi": "auth_failure"})["kalshi"] == "degraded"
    assert _merge_top_level_venue_health(hot, {"polymarket": "retry_wait"})["polymarket"] == "degraded"
    assert _merge_top_level_venue_health(hot, {"polymarket": "partial"})["polymarket"] == "degraded"
    assert _merge_top_level_venue_health(hot, {"matchbook": "waiting"})["matchbook"] == "ok"


@pytest.mark.asyncio
async def test_integrated_concurrent_streaming_pipeline(tmp_path: Path) -> None:
    trace: list[tuple[float, str]] = []
    origin = monotonic()
    matchbook = ScriptedMatchbook(
        [
            _mb_event(1001, "Real Betis", "Getafe", competition="La Liga"),
            _mb_event(1002, "Arsenal", "Liverpool"),
            _mb_event(1003, "Newcastle United", "Chelsea"),
        ],
        hold_ids={"1002"},
    )
    polymarket = ScriptedPolymarket(
        [
            _pm_event("pm-a", "Real Betis", "Getafe", competition="La Liga"),
            _pm_event("pm-b", "Arsenal", "Liverpool"),
            _pm_event("pm-c", "Newcastle United", "Chelsea"),
        ]
    )
    kalshi = ScriptedKalshi(
        {
            "KXOK": [_k_event("ok-1", "Real Betis", "Getafe", series="KXOK")],
            "KXBAD": [],
        },
        fail_once="KXBAD",
    )
    store = SqliteUniverseCheckpointStore(tmp_path / "integrated.sqlite")
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    coordinator.configure_from_settings()
    access = ProviderAccessLayer({VenueName.MATCHBOOK: 1}, starvation_hot_grants=2)
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        paper_scan=_paper(),
        cluster_concurrency=3,
        provider_access=access,
        cycle_timeout_seconds=None,
    )
    original = collector._scan_cluster
    a_done = asyncio.Event()
    c_attempts = 0
    early_id = ""

    async def gated(cluster: Any, **kwargs: Any) -> Any:
        nonlocal c_attempts, early_id
        home = _cluster_home(cluster)
        canonical = cluster_canonical_event_id(cluster)
        if "Arsenal" in home:
            matchbook.started["1002"].set()
            await matchbook.hold.wait()
        if "Newcastle" in home and c_attempts < 1:
            c_attempts += 1
            return collector._failed_cluster_result(
                cluster,
                seen_at=NOW,
                polymarket_events=[],
                queried_series_ids=None,
                detail="list_markets_unavailable",
            )
        row = await original(cluster, **kwargs)
        if "Betis" in home or "Getafe" in home:
            row = _enrich_surveillance(row)
            early_id = canonical
            a_done.set()
            trace.append((monotonic() - origin, "fixture_a_streamed"))
        return row

    collector._scan_cluster = gated  # type: ignore[method-assign]

    async def universe_runner() -> Any:
        on_discovery, on_fixture, on_work = coordinator.universe_collect_callbacks()
        trace.append((monotonic() - origin, "universe_start"))
        return await collector.collect_and_scan(
            scan_lane=ScanLane.UNIVERSE.value,
            unbounded_cycle=True,
            max_event_pairs=8,
            on_discovery_complete=on_discovery,
            on_fixture_evaluated=on_fixture,
            on_canonical_work_set=on_work,
        )

    universe_task = asyncio.create_task(
        coordinator.run_cycle(universe_runner, timeout_seconds=None, scan_lane=ScanLane.UNIVERSE)
    )
    await asyncio.wait_for(a_done.wait(), timeout=5)
    assert universe_task.done() is False
    assert coordinator.fixture_current_state().detail(early_id, now=NOW) is not None
    assert early_id in coordinator.fixture_current_state().hot_identity_scope(NOW)
    cursor_before_hot = coordinator._universe_cursor
    evaluated_before_hot = set(coordinator._universe_evaluated_ids)
    counts = coordinator._canonical_counts_unlocked()
    assert counts["canonical_work_total"] == 3
    assert counts["canonical_evaluated"] == 1
    assert counts["canonical_retryable"] == 1
    assert counts["canonical_remaining"] == 2
    assert coordinator._universe_sweep_is_complete_unlocked() is False
    assert matchbook.peak <= 1

    async def hot_runner() -> Any:
        trace.append((monotonic() - origin, "hot_start"))
        fixture = coordinator.fixture_current_state().detail(early_id, now=NOW)
        assert fixture is not None
        report = _report(
            [fixture.fixture],
            when=NOW,
            scan_lane=ScanLane.HOT.value,
        )
        trace.append((monotonic() - origin, "hot_complete"))
        return report

    await coordinator.run_cycle(hot_runner, timeout_seconds=2.0, scan_lane=ScanLane.HOT)
    assert coordinator._universe_in_progress is True
    assert coordinator._universe_cursor == cursor_before_hot
    assert coordinator._universe_evaluated_ids == evaluated_before_hot
    matchbook.hold.set()
    first = await asyncio.wait_for(universe_task, timeout=5)
    assert first.venue_health.get("kalshi") == "degraded"
    assert coordinator._universe_sweep_is_complete_unlocked() is False
    retry_plan = coordinator.plan_universe_tick(now=NOW + timedelta(seconds=3))
    assert retry_plan.lane == "universe"
    assert retry_plan.reuse_discovery is True
    kalshi.fail_once = None

    async def retry_runner() -> Any:
        on_discovery, on_fixture, on_work = coordinator.universe_collect_callbacks()
        return await collector.collect_and_scan(
            scan_lane=ScanLane.UNIVERSE.value,
            unbounded_cycle=True,
            reuse_discovery=True,
            discovery_snapshot=retry_plan.discovery_snapshot,
            retry_series=retry_plan.retry_series,
            skip_event_ids=retry_plan.skip_event_ids,
            generation_resume=True,
            max_event_pairs=8,
            on_discovery_complete=on_discovery,
            on_fixture_evaluated=on_fixture,
            on_canonical_work_set=on_work,
        )

    await coordinator.run_cycle(retry_runner, timeout_seconds=None, scan_lane=ScanLane.UNIVERSE)
    assert coordinator.status.universe.worker_state == "complete"
    assert coordinator.status.universe.canonical_remaining == 0
    unique, _lifecycle, _promoted = coordinator.fixture_current_state().hot_membership_breakdown(NOW)
    assert unique == 1
    assert len(coordinator.fixture_current_state()._rows) >= 1
    labels = [item[1] for item in trace]
    assert labels.index("universe_start") < labels.index("fixture_a_streamed")
    assert labels.index("fixture_a_streamed") < labels.index("hot_start")
    assert labels.index("hot_start") < labels.index("hot_complete")
    print("INTEGRATED_TRACE")
    for offset, label in trace:
        print(f"{offset:06.3f} {label}")


def test_restart_preserves_retryable_and_completed_work(tmp_path: Path) -> None:
    database = tmp_path / "resume.sqlite"
    store = SqliteUniverseCheckpointStore(database)
    first = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    first.configure_from_settings()
    first.record_universe_discovery_snapshot(
        {
            "matchbook": [_mb_event(1, "A", "B")],
            "polymarket": [_pm_event("pm-1", "A", "B")],
            "kalshi": [],
            "series_results": {
                "kalshi": [
                    {"series": "KXOK", "status": "ok", "retryable": False, "event_count": 1},
                    {"series": "KXBAD", "status": "discovery_timeout", "retryable": True, "event_count": 0},
                ]
            },
        }
    )
    first.record_universe_work_set(["done-a", "retry-b", "pending-c"])
    first.record_universe_fixture_progress(None, _universe_fixture("done-a"), [], [])
    first.record_universe_fixture_progress(
        None,
        _universe_fixture("retry-b", evaluation="market_fetch_unavailable"),
        [],
        [],
    )
    sweep_id = first._universe_sweep_id
    restarted = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    restarted.configure_from_settings()
    assert restarted._universe_sweep_id == sweep_id
    assert restarted._universe_work["done-a"].state == SWEEP_EVALUATED
    assert restarted._universe_work["retry-b"].state == SWEEP_RETRY_WAIT
    assert restarted._universe_work["pending-c"].state == "pending"
    assert restarted._universe_discovery_snapshot is None
    assert restarted._universe_series_work
    assert _series_unit(restarted, "kalshi", "KXOK").state == SWEEP_OK
    bad = _series_unit(restarted, "kalshi", "KXBAD")
    assert bad.state == SWEEP_RETRY_WAIT
    assert bad.attempt_count == 1
    assert bad.next_retry_at is not None
    assert restarted._universe_sweep_is_complete_unlocked() is False
    waiting = restarted.plan_universe_tick(now=NOW)
    assert waiting.generation_resume is True
    assert waiting.reuse_discovery is False
    assert "done-a" in waiting.skip_event_ids
    assert "retry-b" in waiting.skip_event_ids
    assert "KXBAD" not in waiting.retry_series.get("kalshi", [])
    plan = restarted.plan_universe_tick(now=NOW + timedelta(seconds=3))
    assert plan.retry_series.get("kalshi") == ["KXBAD"]
    assert "KXOK" not in plan.retry_series.get("kalshi", [])
    assert restarted._universe_generation_started_at is not None


def _betis_matchbook() -> ScriptedMatchbook:
    return ScriptedMatchbook(
        [_mb_event(1001, "Real Betis", "Getafe", competition="La Liga")]
    )


_MB_K = [VenueName.MATCHBOOK, VenueName.KALSHI]


@pytest.mark.asyncio
async def test_retryable_series_keeps_sweep_open_after_fixture_success() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator.configure_from_settings()
    kalshi = ScriptedKalshi(
        {
            "KXOK": [_k_event("ok-1", "Real Betis", "Getafe", series="KXOK", competition="La Liga")],
            "KXBAD": [_k_event("bad-1", "Arsenal", "Liverpool", series="KXBAD", competition="Premier League")],
        },
        fail_once="KXBAD",
    )
    await _run_universe(
        coordinator,
        _force_evaluated(_kalshi_collector(kalshi, matchbook=_betis_matchbook())),
        enabled_venues=_MB_K,
    )
    assert _series_unit(coordinator, "kalshi", "KXOK").state == SWEEP_OK
    bad = _series_unit(coordinator, "kalshi", "KXBAD")
    assert bad.state == SWEEP_RETRY_WAIT
    assert bad.retryable is True
    assert coordinator._universe_work
    assert all(unit.state == SWEEP_EVALUATED for unit in coordinator._universe_work.values())
    assert coordinator._universe_sweep_is_complete_unlocked() is False
    assert coordinator._universe_generation_started_at is not None
    assert coordinator.status.universe.worker_state != "complete"
    assert coordinator.status.universe.series_retryable == 1


@pytest.mark.asyncio
async def test_series_retry_appends_new_canonical_work_before_complete() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator.configure_from_settings()
    kalshi = ScriptedKalshi(
        {
            "KXOK": [_k_event("ok-1", "Real Betis", "Getafe", series="KXOK", competition="La Liga")],
            "KXBAD": [_k_event("bad-1", "Arsenal", "Liverpool", series="KXBAD", competition="Premier League")],
        },
        fail_once="KXBAD",
    )
    collector = _force_evaluated(_kalshi_collector(kalshi, matchbook=_betis_matchbook()))
    await _run_universe(coordinator, collector, enabled_venues=_MB_K)
    before_total = coordinator.status.universe.canonical_work_total
    assert before_total >= 1
    assert coordinator._universe_sweep_is_complete_unlocked() is False
    clock.advance(20)
    plan = coordinator.plan_universe_tick(now=clock.now)
    assert plan.lane == "universe"
    assert plan.retry_series.get("kalshi") == ["KXBAD"]
    await _run_universe(
        coordinator,
        collector,
        enabled_venues=_MB_K,
        reuse_discovery=True,
        discovery_snapshot=plan.discovery_snapshot,
        retry_series=plan.retry_series,
        skip_event_ids=plan.skip_event_ids,
        generation_resume=True,
    )
    diagnostics = coordinator.status.universe.last_diagnostics or {}
    assert coordinator.status.universe.canonical_work_total > before_total
    assert diagnostics["series_work"][series_work_key("kalshi", "KXBAD")]["state"] == SWEEP_OK
    assert coordinator._universe_generation_started_at is None
    assert coordinator.status.universe.worker_state == "complete"


@pytest.mark.asyncio
async def test_all_series_fail_does_not_empty_universe_complete() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator.configure_from_settings()
    kalshi = ScriptedKalshi(
        {"KXBAD1": [], "KXBAD2": []},
        fail_always={"KXBAD1", "KXBAD2"},
    )
    await _run_universe(coordinator, _kalshi_collector(kalshi))
    assert not coordinator._universe_work
    assert _series_unit(coordinator, "kalshi", "KXBAD1").state == SWEEP_RETRY_WAIT
    assert _series_unit(coordinator, "kalshi", "KXBAD2").state == SWEEP_RETRY_WAIT
    assert coordinator._universe_sweep_is_complete_unlocked() is False
    assert coordinator._universe_generation_started_at is not None
    assert coordinator.status.universe.worker_state != "complete"
    assert coordinator.status.universe.series_retryable == 2


@pytest.mark.asyncio
async def test_series_transient_timeout_stays_retryable_beyond_max_attempts_then_recovers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "sports_hedge.application.live_refresh.get_settings",
        lambda: Settings(paper_universe_work_max_attempts=3),
    )
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator.configure_from_settings()
    kalshi = ScriptedKalshi(
        {
            "KXOK": [_k_event("ok-1", "Real Betis", "Getafe", series="KXOK")],
            "KXPERM": [],
        },
        fail_times={"KXPERM": 4},
    )
    collector = _force_evaluated(_kalshi_collector(kalshi, matchbook=_betis_matchbook()))
    await _run_universe(coordinator, collector, enabled_venues=_MB_K)
    for _ in range(3):
        assert _series_unit(coordinator, "kalshi", "KXPERM").state == SWEEP_RETRY_WAIT
        assert coordinator._universe_sweep_is_complete_unlocked() is False
        clock.advance(20)
        plan = coordinator.plan_universe_tick(now=clock.now)
        await _run_universe(
            coordinator,
            collector,
            enabled_venues=_MB_K,
            reuse_discovery=True,
            discovery_snapshot=plan.discovery_snapshot,
            retry_series=plan.retry_series,
            skip_event_ids=plan.skip_event_ids,
            generation_resume=True,
        )
    diagnostics = coordinator.status.universe.last_diagnostics or {}
    perm = diagnostics["series_work"][series_work_key("kalshi", "KXPERM")]
    assert perm["state"] == SWEEP_RETRY_WAIT
    assert perm["attempt_count"] == 4
    assert perm["retryable"] is True
    assert coordinator.status.universe.series_final_failed == 0
    assert coordinator._universe_generation_started_at is not None
    clock.advance(20)
    plan = coordinator.plan_universe_tick(now=clock.now)
    await _run_universe(
        coordinator,
        collector,
        enabled_venues=_MB_K,
        reuse_discovery=True,
        discovery_snapshot=plan.discovery_snapshot,
        retry_series=plan.retry_series,
        skip_event_ids=plan.skip_event_ids,
        generation_resume=True,
    )
    recovered = (coordinator.status.universe.last_diagnostics or {})["series_work"][
        series_work_key("kalshi", "KXPERM")
    ]
    assert recovered["state"] == SWEEP_OK
    assert recovered["retryable"] is False
    assert coordinator._universe_generation_started_at is None
    assert coordinator.status.universe.worker_state == "complete"


@pytest.mark.asyncio
async def test_restart_preserves_series_retry_and_skips_successful_series(
    tmp_path: Path,
) -> None:
    clock = FakeClock(NOW)
    store = SqliteUniverseCheckpointStore(tmp_path / "series-resume.sqlite")
    first = LiveRefreshCoordinator(clock=clock, universe_checkpoint_store=store)
    first._clock = clock
    first.configure_from_settings()
    kalshi = ScriptedKalshi(
        {
            "KXOK": [_k_event("ok-1", "Real Betis", "Getafe", series="KXOK", competition="La Liga")],
            "KXBAD": [_k_event("bad-1", "Arsenal", "Liverpool", series="KXBAD", competition="Premier League")],
        },
        fail_once="KXBAD",
    )
    collector = _force_evaluated(_kalshi_collector(kalshi, matchbook=_betis_matchbook()))
    await _run_universe(first, collector, enabled_venues=_MB_K)
    assert kalshi.series_calls == {"KXOK": 1, "KXBAD": 1}
    assert first._universe_generation_started_at is not None
    payload = store.load()
    assert payload is not None
    assert payload["series_work"][series_work_key("kalshi", "KXBAD")]["attempt_count"] == 1
    assert payload["series_work"][series_work_key("kalshi", "KXOK")]["state"] == SWEEP_OK
    restarted = LiveRefreshCoordinator(clock=clock, universe_checkpoint_store=store)
    restarted._clock = clock
    restarted.configure_from_settings()
    bad = _series_unit(restarted, "kalshi", "KXBAD")
    assert bad.state == SWEEP_RETRY_WAIT
    assert bad.attempt_count == 1
    assert bad.next_retry_at is not None
    assert _series_unit(restarted, "kalshi", "KXOK").state == SWEEP_OK
    clock.advance(20)
    plan = restarted.plan_universe_tick(now=clock.now)
    assert plan.retry_series.get("kalshi") == ["KXBAD"]
    assert "KXOK" not in plan.retry_series.get("kalshi", [])
    await _run_universe(
        restarted,
        collector,
        enabled_venues=_MB_K,
        reuse_discovery=plan.reuse_discovery,
        discovery_snapshot=plan.discovery_snapshot,
        retry_series=plan.retry_series,
        skip_event_ids=plan.skip_event_ids,
        generation_resume=True,
    )
    assert kalshi.series_calls["KXBAD"] == 2
    diagnostics = restarted.status.universe.last_diagnostics or {}
    assert diagnostics["series_work"][series_work_key("kalshi", "KXBAD")]["state"] == SWEEP_OK
    assert restarted.status.universe.worker_state == "complete"


@pytest.mark.asyncio
async def test_hot_overlap_while_universe_retries_series() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator.configure_from_settings()
    kalshi = ScriptedKalshi(
        {
            "KXOK": [_k_event("ok-1", "Real Betis", "Getafe", series="KXOK")],
            "KXBAD": [],
        },
        fail_once="KXBAD",
    )
    await _run_universe(
        coordinator,
        _force_evaluated(_kalshi_collector(kalshi, matchbook=_betis_matchbook())),
        enabled_venues=_MB_K,
    )
    assert _series_unit(coordinator, "kalshi", "KXBAD").state == SWEEP_RETRY_WAIT
    generation = coordinator._universe_generation_started_at
    sweep_id = coordinator._universe_sweep_id
    evaluated = set(coordinator._universe_evaluated_ids)
    assert generation is not None
    assert evaluated

    async def hot_runner() -> Any:
        return _report(
            [_universe_fixture(next(iter(evaluated)))],
            when=clock.now,
            scan_lane=ScanLane.HOT.value,
        )

    await coordinator.run_cycle(hot_runner, timeout_seconds=2.0, scan_lane=ScanLane.HOT)
    assert coordinator._universe_generation_started_at == generation
    assert coordinator._universe_sweep_id == sweep_id
    assert coordinator._universe_evaluated_ids == evaluated
    assert _series_unit(coordinator, "kalshi", "KXBAD").state == SWEEP_RETRY_WAIT
    assert coordinator.status.universe.worker_state != "complete"
    clock.advance(20)
    plan = coordinator.plan_universe_tick(now=clock.now)
    assert plan.lane == "universe"
    assert plan.retry_series.get("kalshi") == ["KXBAD"]
