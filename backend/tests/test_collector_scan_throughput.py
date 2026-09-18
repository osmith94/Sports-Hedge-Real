"""Deterministic synthetic stress coverage for Wave G scanner throughput.

These tests do not encode a real-provider SLA. They prove that collector wall-clock
scaling is bounded under controlled latency, and that a stalled provider degrades
truthfully instead of blocking the cycle.

Synthetic assumptions (explicit, not a live venue claim):
- Per-call provider latency is a fixed asyncio.sleep, default 40ms.
- Topology per fixture: Matchbook list_markets, Polymarket list_markets,
  then two Polymarket get_order_book calls (Yes/No tokens).
- Serial lower bound is therefore N * (2 * list + 2 * book) plus one-time
  list_events across three venues.
- One stalled-provider case sleeps far beyond the cycle budget.
- HOT repeats reuse known_source_events; list_events must not re-run.

Data class: fixture/demo synthetic providers. Not live, historical, or modelled
venue quotes.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic
from typing import Any

import pytest

from sports_hedge.application.collector import (
    MarketEvaluationState,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.hot_market_relationships import relationships_from_fixture_markets
from sports_hedge.application.mapping_review import MappingReviewService
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from test_issue_147_market_evaluation_state import THREE_LEAGUE_FIXTURES
from venue_cost_helpers import matchbook_polymarket_costs


SYNTHETIC_PROVIDER_LATENCY_S = 0.04
STALLED_PROVIDER_SLEEP_S = 30.0
HOT_CADENCE_S = 30.0
KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
REQUIRED_STAGES = (
    "event_lookup",
    "market_discovery",
    "book_depth",
    "mapping_equivalence",
    "fees_fx_risk",
    "solver_allocation",
    "current_state_finalization",
)

def _synthetic_fixtures(
    fixture_count: int,
) -> list[tuple[str, str, str, datetime]]:
    """Return proven-distinct fixture identities, repeating on a later matchday."""

    return [
        (*THREE_LEAGUE_FIXTURES[index % len(THREE_LEAGUE_FIXTURES)], KICKOFF + timedelta(days=index // 30))
        for index in range(fixture_count)
    ]


def _serial_lower_bound_s(fixture_count: int, *, latency_s: float = SYNTHETIC_PROVIDER_LATENCY_S) -> float:
    """N× serial provider latency for the known per-fixture call topology."""

    discovery = 3 * latency_s
    per_fixture = (2 * latency_s) + (2 * latency_s)
    return discovery + fixture_count * per_fixture


def _btts_matchbook_market(market_id: int) -> dict[str, Any]:
    return {
        "id": market_id,
        "name": "Both Teams To Score",
        "runners": [
            {
                "id": market_id * 10 + 1,
                "name": "Yes",
                "prices": [
                    {"side": "back", "odds": "2.20", "available-amount": "100"},
                    {"side": "lay", "odds": "2.22", "available-amount": "100"},
                ],
            },
            {
                "id": market_id * 10 + 2,
                "name": "No",
                "prices": [
                    {"side": "back", "odds": "1.80", "available-amount": "100"},
                    {"side": "lay", "odds": "1.82", "available-amount": "100"},
                ],
            },
        ],
    }


def _btts_polymarket_market(index: int) -> dict[str, Any]:
    return {
        "id": f"pm-market-{index}",
        "question": "Both teams to score?",
        "sportsMarketType": "both teams to score",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": f'["yes-{index}", "no-{index}"]',
        "description": "Resolves based on 90 minutes of regulation time.",
        "feesEnabled": False,
    }


def _book(token: str) -> dict[str, Any]:
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    yes = token.startswith("yes-")
    return {
        "asset_id": token,
        "timestamp": now_ms - 150,
        "bids": [{"price": "0.49" if yes else "0.41", "size": "250"}],
        "asks": [{"price": "0.51" if yes else "0.43", "size": "250"}],
    }


class SyntheticUniverse:
    """Matchbook + Polymarket universe with controlled per-call latency."""

    def __init__(
        self,
        fixture_count: int,
        *,
        latency_s: float = SYNTHETIC_PROVIDER_LATENCY_S,
        stall_matchbook_markets: bool = False,
        stall_after_calls: int | None = None,
    ) -> None:
        if not 1 <= fixture_count <= 50:
            raise ValueError("fixture_count must be 1..50")
        self.fixture_count = fixture_count
        self.latency_s = latency_s
        self.stall_matchbook_markets = stall_matchbook_markets
        self.stall_after_calls = stall_after_calls
        self.list_events_calls = {"matchbook": 0, "polymarket": 0, "kalshi": 0}
        self.list_markets_calls = {"matchbook": 0, "polymarket": 0}
        self.book_calls = 0
        self.live_provider_calls = 0
        self.peak_live_provider_calls = 0

    def _note_live(self, delta: int) -> None:
        self.live_provider_calls += delta
        if self.live_provider_calls > self.peak_live_provider_calls:
            self.peak_live_provider_calls = self.live_provider_calls

    async def _pace(self, *, stall: bool = False) -> None:
        self._note_live(1)
        try:
            if stall:
                await asyncio.sleep(STALLED_PROVIDER_SLEEP_S)
            elif self.latency_s > 0:
                await asyncio.sleep(self.latency_s)
        finally:
            self._note_live(-1)

    def matchbook(self) -> "SyntheticMatchbook":
        return SyntheticMatchbook(self)

    def polymarket(self) -> "SyntheticPolymarket":
        return SyntheticPolymarket(self)

    def kalshi(self) -> "SyntheticKalshi":
        return SyntheticKalshi(self)


class SyntheticMatchbook:
    def __init__(self, universe: SyntheticUniverse) -> None:
        self._universe = universe

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        self._universe.list_events_calls["matchbook"] += 1
        await self._universe._pace()
        return {
            "events": [
                {
                    "id": 10_000 + index,
                    "name": f"{home} vs {away}",
                    "start": kickoff.isoformat(),
                    "competition-name": competition,
                }
                for index, (competition, home, away, kickoff) in enumerate(
                    _synthetic_fixtures(self._universe.fixture_count)
                )
            ]
        }

    def _markets(self, event_id: int | str) -> list[dict[str, Any]]:
        return [_btts_matchbook_market(20_000 + int(event_id))]

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        self._universe.list_markets_calls["matchbook"] += 1
        stall = self._universe.stall_matchbook_markets
        if self._universe.stall_after_calls is not None:
            stall = stall and self._universe.list_markets_calls["matchbook"] > self._universe.stall_after_calls
        await self._universe._pace(stall=stall)
        return {"markets": self._markets(event_id)}

    async def get_market(
        self,
        event_id: int | str,
        market_id: int | str,
        **filters: Any,
    ) -> dict[str, Any]:
        del filters
        stall = self._universe.stall_matchbook_markets
        await self._universe._pace(stall=stall)
        for market in self._markets(event_id):
            if str(market.get("id")) == str(market_id):
                return market
        from sports_hedge.venues.matchbook import MatchbookMarketGoneError

        raise MatchbookMarketGoneError(event_id, market_id, 404)


class SyntheticPolymarket:
    def __init__(self, universe: SyntheticUniverse) -> None:
        self._universe = universe

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        self._universe.list_events_calls["polymarket"] += 1
        await self._universe._pace()
        return [
            {
                "id": f"pm-event-{index}",
                "title": f"{home} vs {away}",
                "startTime": kickoff.isoformat(),
                "competition": competition,
                "series": [
                    {
                        "id": (
                            "10188"
                            if competition == "Premier League"
                            else "10355"
                            if competition == "Championship"
                            else "10193"
                        ),
                        "title": competition,
                    }
                ],
            }
            for index, (competition, home, away, kickoff) in enumerate(
                _synthetic_fixtures(self._universe.fixture_count)
            )
        ]

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del filters
        self._universe.list_markets_calls["polymarket"] += 1
        await self._universe._pace()
        index = int(str(event_id).rsplit("-", 1)[-1])
        return [_btts_polymarket_market(index)]

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id, filters
        assert outcome_id is not None
        self._universe.book_calls += 1
        await self._universe._pace()
        return _book(str(outcome_id))


class SyntheticKalshi:
    """Present so discovery has a third venue; returns no in-scope events."""

    def __init__(self, universe: SyntheticUniverse) -> None:
        self._universe = universe

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        self._universe.list_events_calls["kalshi"] += 1
        await self._universe._pace()
        return {"events": []}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": []}

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id, outcome_id, filters
        return {}

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        del series_ticker
        return {}


def _collector(universe: SyntheticUniverse) -> tuple[ReadOnlyCrossVenueCollector, SqliteMarketIntelligenceRepository]:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=universe.matchbook(),
        polymarket=universe.polymarket(),
        kalshi=universe.kalshi(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        venue_timeout_seconds=2.0,
        provider_call_timeout_seconds=2.0,
        cycle_timeout_seconds=45.0,
    )
    return collector, repository


async def _scan(collector: ReadOnlyCrossVenueCollector, *, max_event_pairs: int, **kwargs: Any):
    return await collector.collect_and_scan(
        venue_costs=matchbook_polymarket_costs(),
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
        capital_limit_gbp=Decimal("100"),
        maximum_execution_risk=100,
        max_event_pairs=max_event_pairs,
        **kwargs,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("fixture_count", [1, 4, 16, 50])
async def test_synthetic_workload_wall_clock_is_not_serial_n_times_latency(fixture_count: int) -> None:
    universe = SyntheticUniverse(fixture_count)
    collector, repository = _collector(universe)
    try:
        started = monotonic()
        report = await _scan(collector, max_event_pairs=max(fixture_count, 1))
        wall_s = monotonic() - started
        serial = _serial_lower_bound_s(fixture_count)
        diagnostics = report.scan_diagnostics
        evaluated = [
            item
            for item in report.discovered_fixtures
            if item.market_evaluation_state == MarketEvaluationState.EVALUATED.value
        ]
        leftovers = [
            item
            for item in report.discovered_fixtures
            if item.market_evaluation_state == MarketEvaluationState.NOT_EVALUATED_SCAN_DEADLINE.value
        ]
        ids = [item.canonical_event_id for item in report.discovered_fixtures]
        detail = (
            f"n={fixture_count} wall={wall_s:.3f}s serial_bound={serial:.3f}s "
            f"diag={diagnostics} leftover={len(leftovers)}"
        )
        assert len(report.discovered_fixtures) == fixture_count, detail
        assert len(evaluated) == fixture_count, detail
        assert leftovers == [], detail
        assert len(set(ids)) == fixture_count, detail
        assert report.matched_market_pairs == fixture_count, detail
        assert len(report.paper_decisions) == fixture_count, detail
        assert diagnostics["clusters_evaluated"] == fixture_count, detail
        assert diagnostics["inflight_orphaned"] == 0, detail
        assert diagnostics["inflight_live"] == 0, detail
        assert diagnostics["peak_provider_inflight"] >= 1, detail
        assert diagnostics["peak_provider_inflight"] <= sum(
            diagnostics["provider_concurrency"].values()
        ) + 1, detail
        for venue, peak in diagnostics["peak_provider_inflight_by_venue"].items():
            assert peak <= diagnostics["provider_concurrency"][venue], detail
        for stage in REQUIRED_STAGES:
            assert stage in diagnostics["stages"], detail
        assert diagnostics["stages"]["market_discovery"]["calls"] >= fixture_count, detail
        assert diagnostics["stages"]["book_depth"]["calls"] >= fixture_count, detail
        assert diagnostics["cluster_concurrency"] >= 2, detail
        # Do not hide time in budgets: cycle remains 45s, frontend collect remains 60s.
        assert diagnostics["cycle_budget_s"] == 45.0, detail
        assert wall_s < HOT_CADENCE_S, detail
        if fixture_count == 1:
            assert wall_s < 2.0, detail
        else:
            # Material speedup vs N× serial provider latency, with HOT cadence headroom.
            assert wall_s < 0.45 * serial, detail
            assert wall_s < 0.5 * HOT_CADENCE_S, detail
        if fixture_count == 50:
            assert wall_s < 8.0, detail
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_stalled_provider_degrades_truthfully_inside_cycle_budget() -> None:
    universe = SyntheticUniverse(16, stall_matchbook_markets=True)
    collector, repository = _collector(universe)
    collector._cycle_timeout_seconds = 2.5
    try:
        started = monotonic()
        report = await _scan(
            collector,
            max_event_pairs=16,
            cycle_timeout_seconds=2.5,
            provider_call_timeout_seconds=0.8,
        )
        wall_s = monotonic() - started
        leftovers = [
            item
            for item in report.discovered_fixtures
            if item.market_evaluation_state == MarketEvaluationState.NOT_EVALUATED_SCAN_DEADLINE.value
        ]
        unavailable = [
            item
            for item in report.discovered_fixtures
            if item.market_evaluation_state == MarketEvaluationState.MARKET_FETCH_UNAVAILABLE.value
        ]
        fabricated = [item for item in report.discovered_fixtures if item.solver_is_arbitrage]
        assert wall_s < 3.5
        assert wall_s < STALLED_PROVIDER_SLEEP_S
        assert len(report.discovered_fixtures) == 16
        assert leftovers or unavailable or report.scan_diagnostics.get("soft_deadline_reached")
        assert fabricated == []
        assert all(item.current_net_edge is None or item.solver_is_arbitrage is False for item in leftovers)
        assert report.venue_health["matchbook"] in {"timeout", "discovery_timeout", "market_timeout", "degraded"}
        assert report.scan_diagnostics["stages"]["market_discovery"]["timeouts"] >= 1
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_repeated_hot_cycles_do_not_rediscover_or_slow_down() -> None:
    universe = SyntheticUniverse(8)
    collector, repository = _collector(universe)
    try:
        first = await _scan(collector, max_event_pairs=8, scan_lane=ScanLane.UNIVERSE.value)
        known = first.fixture_source_events
        scope = [item.canonical_event_id for item in first.discovered_fixtures]
        assert known
        assert universe.list_events_calls["matchbook"] == 1

        durations: list[float] = []
        orphaned: list[int] = []
        for _ in range(5):
            started = monotonic()
            hot = await _scan(
                collector,
                max_event_pairs=8,
                scan_lane=ScanLane.HOT.value,
                identity_scope=scope,
                known_source_events=known,
                hot_market_relationships=relationships_from_fixture_markets(first.fixture_markets),
            )
            durations.append(monotonic() - started)
            orphaned.append(hot.scan_diagnostics["inflight_orphaned"])
            assert hot.scan_diagnostics["inflight_live"] == 0
            assert len(hot.discovered_fixtures) == 8
            assert hot.scan_lane == ScanLane.HOT.value
            assert all(
                item.market_evaluation_state == MarketEvaluationState.EVALUATED.value
                for item in hot.discovered_fixtures
            )
        assert universe.list_events_calls["matchbook"] == 1
        assert universe.list_events_calls["polymarket"] == 1
        assert max(orphaned) == 0
        assert max(durations) < 0.5 * HOT_CADENCE_S
        # Progressive slowdown / task accumulation would lift later cycles.
        assert durations[-1] < durations[0] * 2.5 + 0.25
        assert max(durations) < min(durations) * 3.0 + 0.25
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_cancelled_concurrent_hot_scan_drains_cluster_and_provider_tasks() -> None:
    universe = SyntheticUniverse(16, latency_s=0)
    collector, repository = _collector(universe)
    try:
        seed = await _scan(collector, max_event_pairs=16, scan_lane=ScanLane.UNIVERSE.value)
        universe.latency_s = 0.5
        task = asyncio.create_task(
            _scan(
                collector,
                max_event_pairs=16,
                scan_lane=ScanLane.HOT.value,
                identity_scope=[item.canonical_event_id for item in seed.discovered_fixtures],
                known_source_events=seed.fixture_source_events,
                hot_market_relationships=relationships_from_fixture_markets(seed.fixture_markets),
            )
        )
        await asyncio.sleep(0.1)
        task.cancel()
        report = await task
        await asyncio.sleep(0)
        assert report.scan_diagnostics["cancelled"] is True
        assert report.scan_diagnostics["inflight_live"] == 0
        assert universe.live_provider_calls == 0
        assert len(report.discovered_fixtures) == 16
        assert all(
            item.market_evaluation_state
            == MarketEvaluationState.NOT_EVALUATED_SCAN_DEADLINE.value
            for item in report.discovered_fixtures
        )
    finally:
        repository.close()


def test_collector_keeps_mapping_prompt_and_openai_off_the_scan_path() -> None:
    collector_src = inspect.getsource(ReadOnlyCrossVenueCollector)
    scan_src = inspect.getsource(ReadOnlyCrossVenueCollector.collect_and_scan)
    cluster_src = inspect.getsource(ReadOnlyCrossVenueCollector._scan_cluster)
    paper_src = inspect.getsource(PaperScanService.scan_pair)
    assert "build_prompt" not in collector_src
    assert "build_prompt" not in paper_src
    assert "openai" not in collector_src.casefold()
    assert "chatgpt" not in scan_src.casefold()
    assert "chatgpt" not in cluster_src.casefold()
    assert "MappingReviewService" not in collector_src
    prompt_src = inspect.getsource(MappingReviewService.build_prompt)
    assert "VERIFIED / NOT VERIFIED / AMBIGUOUS" in prompt_src


def test_frontend_collect_timeout_was_not_raised_to_hide_scan_time() -> None:
    from pathlib import Path

    api_ts = Path(__file__).resolve().parents[2] / "frontend" / "lib" / "api.ts"
    text = api_ts.read_text(encoding="utf-8")
    assert "PAPER_COLLECTION_TIMEOUT_MS = 60_000" in text
