"""Deterministic scanner stress/soak validation.

These controlled-latency workloads validate architecture headroom only. They do
not establish an SLA for credentialed or public real-provider calls.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from statistics import median
from time import monotonic
from typing import Any

import pytest

from sports_hedge.api import mapping_reviews
from sports_hedge.application import collector as collector_module
from sports_hedge.application import paper_scan as paper_scan_module
from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.mapping_review import MappingReviewService
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import matchbook_polymarket_costs


CONTROLLED_PROVIDER_LATENCY_S = 0.0015
SYNTHETIC_CYCLE_BUDGET_S = 25.0
KICKOFF = datetime.now(UTC) + timedelta(minutes=30)


class CallTracker:
    def __init__(self, latency_s: float) -> None:
        self.latency_s = latency_s
        self.active = 0
        self.peak = 0
        self.calls: dict[str, int] = {}

    async def wait(self, label: str) -> None:
        self.active += 1
        self.peak = max(self.peak, self.active)
        self.calls[label] = self.calls.get(label, 0) + 1
        try:
            if self.latency_s:
                await asyncio.sleep(self.latency_s)
        finally:
            self.active -= 1

    def reset(self, *, latency_s: float | None = None) -> None:
        if latency_s is not None:
            self.latency_s = latency_s
        self.active = 0
        self.peak = 0
        self.calls = {}


def _teams(index: int) -> tuple[str, str]:
    return f"Alpha {index:02d} United", f"Beta {index:02d} City"


def _kickoff(index: int) -> datetime:
    """Keep generated fixtures outside EventMatcher's five-minute tolerance."""

    return KICKOFF + timedelta(minutes=index * 10)


def _matchbook_event(index: int) -> dict[str, Any]:
    home, away = _teams(index)
    return {
        "id": 10_000 + index,
        "name": f"{home} vs {away}",
        "start": _kickoff(index).isoformat(),
        "competition-name": "Premier League",
        "sport-name": "Football",
        "status": "open",
    }


def _polymarket_event(index: int) -> dict[str, Any]:
    home, away = _teams(index)
    return {
        "id": f"pm-event-{index}",
        "title": f"{home} vs {away}",
        "startTime": _kickoff(index).isoformat(),
        "competition": "Premier League",
        "series": [{"title": "Premier League"}],
    }


class SyntheticMatchbook:
    def __init__(self, fixture_count: int, tracker: CallTracker) -> None:
        self.fixture_count = fixture_count
        self.tracker = tracker

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        await self.tracker.wait("matchbook.list_events")
        return {"events": [_matchbook_event(index) for index in range(self.fixture_count)]}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        await self.tracker.wait("matchbook.list_markets")
        numeric = int(event_id)
        market_id = numeric * 10
        return {
            "markets": [
                {
                    "id": market_id,
                    "name": "Both Teams To Score",
                    "runners": [
                        {
                            "id": market_id + 1,
                            "name": "Yes",
                            "prices": [
                                {
                                    "side": "back",
                                    "odds": "2.20",
                                    "available-amount": "100",
                                }
                            ],
                        },
                        {
                            "id": market_id + 2,
                            "name": "No",
                            "prices": [
                                {
                                    "side": "back",
                                    "odds": "1.80",
                                    "available-amount": "100",
                                }
                            ],
                        },
                    ],
                }
            ]
        }


class SyntheticPolymarket:
    def __init__(self, fixture_count: int, tracker: CallTracker) -> None:
        self.fixture_count = fixture_count
        self.tracker = tracker

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        await self.tracker.wait("polymarket.list_events")
        return [_polymarket_event(index) for index in range(self.fixture_count)]

    async def list_markets(
        self,
        event_id: int | str,
        **filters: Any,
    ) -> list[dict[str, Any]]:
        del filters
        await self.tracker.wait("polymarket.list_markets")
        index = int(str(event_id).rsplit("-", 1)[-1])
        return [
            {
                "id": f"pm-market-{index}",
                "question": "Both teams to score?",
                "sportsMarketType": "both teams to score",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": json.dumps([f"yes-{index}", f"no-{index}"]),
                "description": "Resolves based on 90 minutes of regulation time.",
                "feesEnabled": False,
            }
        ]

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id, filters
        await self.tracker.wait("polymarket.get_order_book")
        token = str(outcome_id)
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        return {
            "asset_id": token,
            "timestamp": now_ms - 50,
            "bids": [{"price": "0.44", "size": "250"}],
            "asks": [{"price": "0.46", "size": "250"}],
        }


def _collector(
    fixture_count: int,
    tracker: CallTracker,
    repository: SqliteMarketIntelligenceRepository,
) -> ReadOnlyCrossVenueCollector:
    return ReadOnlyCrossVenueCollector(
        matchbook=SyntheticMatchbook(fixture_count, tracker),
        polymarket=SyntheticPolymarket(fixture_count, tracker),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        venue_timeout_seconds=2.0,
        provider_call_timeout_seconds=2.0,
        cycle_timeout_seconds=SYNTHETIC_CYCLE_BUDGET_S,
    )


async def _run(
    *,
    fixture_count: int,
    lane: ScanLane,
    tracker: CallTracker,
    repository: SqliteMarketIntelligenceRepository,
    identity_scope: list[str] | None = None,
    known_source_events: dict[str, list[dict[str, Any]]] | None = None,
) -> tuple[dict[str, Any], Any]:
    tracker.reset(latency_s=CONTROLLED_PROVIDER_LATENCY_S)
    started = monotonic()
    report = await _collector(fixture_count, tracker, repository).collect_and_scan(
        enabled_venues=[VenueName.MATCHBOOK, VenueName.POLYMARKET],
        venue_costs=matchbook_polymarket_costs(),
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
        maximum_execution_risk=100,
        max_event_pairs=fixture_count,
        cycle_timeout_seconds=SYNTHETIC_CYCLE_BUDGET_S,
        scan_lane=lane.value,
        identity_scope=identity_scope,
        known_source_events=known_source_events,
    )
    wall_ms = round((monotonic() - started) * 1000, 3)
    diagnostics = report.scan_diagnostics
    row = {
        "lane": lane.value,
        "fixtures": fixture_count,
        "controlled_provider_latency_ms": CONTROLLED_PROVIDER_LATENCY_S * 1000,
        "wall_ms": wall_ms,
        "event_lookup_ms": diagnostics["stages"]["event_lookup"]["elapsed_ms"],
        "market_discovery_ms": diagnostics["stages"]["market_discovery"]["elapsed_ms"],
        "book_depth_ms": diagnostics["stages"]["book_depth"]["elapsed_ms"],
        "mapping_equivalence_ms": diagnostics["stages"]["mapping_equivalence"]["elapsed_ms"],
        "fees_fx_risk_ms": diagnostics["stages"]["fees_fx_risk"]["elapsed_ms"],
        "solver_allocation_ms": diagnostics["stages"]["solver_allocation"]["elapsed_ms"],
        "provider_calls": diagnostics["provider_calls"],
        "provider_cancels": diagnostics["provider_cancels"],
        "inflight_orphaned": diagnostics["inflight_orphaned"],
        "inflight_live": diagnostics["inflight_live"],
    }
    assert len(report.discovered_fixtures) == fixture_count
    assert diagnostics["evaluated_count"] == fixture_count
    assert diagnostics["not_evaluated_count"] == 0
    assert diagnostics["provider_cancels"] == 0
    assert diagnostics["inflight_orphaned"] == 0
    assert diagnostics["inflight_live"] == 0
    assert tracker.active == 0
    # Synthetic guardrail only: deliberately not a real-provider SLA.
    assert wall_ms < max(1000, fixture_count * 100)
    return row, report


async def _seed_hot_scope(
    fixture_count: int,
    tracker: CallTracker,
    repository: SqliteMarketIntelligenceRepository,
) -> tuple[list[str], dict[str, list[dict[str, Any]]]]:
    tracker.reset(latency_s=0)
    report = await _collector(fixture_count, tracker, repository).collect_and_scan(
        enabled_venues=[VenueName.MATCHBOOK, VenueName.POLYMARKET],
        max_event_pairs=fixture_count,
        scan_lane=ScanLane.UNIVERSE.value,
        cycle_timeout_seconds=SYNTHETIC_CYCLE_BUDGET_S,
    )
    return (
        [item.canonical_event_id for item in report.discovered_fixtures],
        report.fixture_source_events,
    )


@pytest.mark.parametrize("fixture_count", [1, 4, 16, 50])
@pytest.mark.parametrize("lane", [ScanLane.HOT, ScanLane.UNIVERSE])
@pytest.mark.asyncio
async def test_controlled_latency_workloads_have_headroom_and_no_orphans(
    fixture_count: int,
    lane: ScanLane,
) -> None:
    repository = SqliteMarketIntelligenceRepository()
    tracker = CallTracker(CONTROLLED_PROVIDER_LATENCY_S)
    try:
        identity_scope = None
        known_source_events = None
        if lane is ScanLane.HOT:
            identity_scope, known_source_events = await _seed_hot_scope(
                fixture_count,
                tracker,
                repository,
            )
        row, _report = await _run(
            fixture_count=fixture_count,
            lane=lane,
            tracker=tracker,
            repository=repository,
            identity_scope=identity_scope,
            known_source_events=known_source_events,
        )
        if lane is ScanLane.HOT:
            assert tracker.calls.get("matchbook.list_events", 0) == 0
            assert tracker.calls.get("polymarket.list_events", 0) == 0
            assert row["event_lookup_ms"] <= 5
        print("SYNTHETIC_SCANNER_BENCHMARK " + json.dumps(row, sort_keys=True))
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_repeated_hot_and_universe_cycles_are_stable_without_task_accumulation() -> None:
    fixture_count = 16
    repository = SqliteMarketIntelligenceRepository()
    tracker = CallTracker(CONTROLLED_PROVIDER_LATENCY_S)
    current = asyncio.current_task()
    tasks_before = {id(task) for task in asyncio.all_tasks() if task is not current and not task.done()}
    rows: dict[ScanLane, list[float]] = {ScanLane.HOT: [], ScanLane.UNIVERSE: []}
    try:
        identity_scope, known_source_events = await _seed_hot_scope(
            fixture_count,
            tracker,
            repository,
        )
        for _cycle in range(8):
            for lane in (ScanLane.HOT, ScanLane.UNIVERSE):
                row, _report = await _run(
                    fixture_count=fixture_count,
                    lane=lane,
                    tracker=tracker,
                    repository=repository,
                    identity_scope=identity_scope if lane is ScanLane.HOT else None,
                    known_source_events=known_source_events if lane is ScanLane.HOT else None,
                )
                rows[lane].append(float(row["wall_ms"]))
                await asyncio.sleep(0)
                live = {
                    id(task)
                    for task in asyncio.all_tasks()
                    if task is not current and not task.done()
                }
                assert live == tasks_before
                assert tracker.active == 0

        for lane, timings in rows.items():
            first_half = median(timings[:4])
            second_half = median(timings[4:])
            assert second_half <= first_half * 1.5 + 25
            print(
                "SYNTHETIC_SCANNER_SOAK "
                + json.dumps(
                    {
                        "lane": lane.value,
                        "cycles": len(timings),
                        "fixtures": fixture_count,
                        "first_half_median_ms": round(first_half, 3),
                        "second_half_median_ms": round(second_half, 3),
                        "minimum_ms": min(timings),
                        "maximum_ms": max(timings),
                        "task_delta": 0,
                        "provider_cancels": 0,
                        "inflight_orphaned": 0,
                    },
                    sort_keys=True,
                )
            )
    finally:
        repository.close()


def test_mapping_verify_prompt_is_operator_only_and_off_scan_critical_path() -> None:
    collector_source = inspect.getsource(collector_module)
    paper_scan_source = inspect.getsource(paper_scan_module)
    prompt_source = inspect.getsource(MappingReviewService.build_prompt)
    endpoint_source = inspect.getsource(mapping_reviews.build_mapping_prompt)

    for source in (collector_source, paper_scan_source):
        assert "api.mapping_reviews" not in source
        assert "MappingReviewService" not in source
        assert "openai" not in source.casefold()
    assert "httpx" not in prompt_source
    assert "openai" not in prompt_source.casefold()
    assert "service.build_prompt" in endpoint_source
