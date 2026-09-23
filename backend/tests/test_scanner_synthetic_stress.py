from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic
from typing import Any

import pytest

from sports_hedge.application.collector import CollectionReport, ReadOnlyCrossVenueCollector
from sports_hedge.application.hot_market_relationships import relationships_from_fixture_markets
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import matchbook_kalshi_costs
from registered_kalshi import FakeKalshiBTTS


SYNTHETIC_PROVIDER_LATENCY_SECONDS = 0.002
SYNTHETIC_CYCLE_BUDGET_SECONDS = 2.0
SYNTHETIC_WORKLOADS = (1, 4, 16, 50)
SYNTHETIC_SOAK_CYCLES = 12


def _kickoff() -> datetime:
    return datetime.now(UTC) + timedelta(minutes=30)


def _fixtures(count: int) -> list[tuple[str, str, datetime]]:
    kickoff = _kickoff()
    return [
        (
            f"Stress Home {_alpha_label(index)}",
            f"Stress Away {_alpha_label(index)}",
            kickoff + timedelta(hours=index * 2),
        )
        for index in range(count)
    ]


def _alpha_label(index: int) -> str:
    return f"{chr(65 + index // 26)}{chr(65 + index % 26)}"


class SyntheticMatchbook:
    def __init__(self, fixtures: list[tuple[str, str, datetime]]) -> None:
        self.fixtures = fixtures
        self.list_events_calls = 0
        self.list_markets_calls = 0

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_events_calls += 1
        await asyncio.sleep(SYNTHETIC_PROVIDER_LATENCY_SECONDS)
        return {
            "events": [
                {
                    "id": 100_000 + index,
                    "name": f"{home} vs {away}",
                    "start": kickoff.isoformat(),
                    "competition-name": "Premier League",
                }
                for index, (home, away, kickoff) in enumerate(self.fixtures)
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_markets_calls += 1
        await asyncio.sleep(SYNTHETIC_PROVIDER_LATENCY_SECONDS)
        return {
            "markets": [
                {
                    "id": f"mb-market-{event_id}",
                    "name": "Both Teams To Score",
                    "runners": [
                        {
                            "id": f"mb-yes-{event_id}",
                            "name": "Yes",
                            "prices": [
                                {"side": "back", "odds": "1.90", "available-amount": "100"},
                                {"side": "lay", "odds": "1.92", "available-amount": "100"},
                            ],
                        },
                        {
                            "id": f"mb-no-{event_id}",
                            "name": "No",
                            "prices": [
                                {"side": "back", "odds": "1.90", "available-amount": "100"},
                                {"side": "lay", "odds": "1.92", "available-amount": "100"},
                            ],
                        },
                    ],
                }
            ]
        }

    async def get_market(
        self,
        event_id: int | str,
        market_id: int | str,
        **filters: Any,
    ) -> dict[str, Any]:
        del filters
        await asyncio.sleep(SYNTHETIC_PROVIDER_LATENCY_SECONDS)
        payload = {
            "markets": [
                {
                    "id": f"mb-market-{event_id}",
                    "name": "Both Teams To Score",
                    "runners": [
                        {
                            "id": f"mb-yes-{event_id}",
                            "name": "Yes",
                            "prices": [
                                {"side": "back", "odds": "1.90", "available-amount": "100"},
                                {"side": "lay", "odds": "1.92", "available-amount": "100"},
                            ],
                        },
                        {
                            "id": f"mb-no-{event_id}",
                            "name": "No",
                            "prices": [
                                {"side": "back", "odds": "1.90", "available-amount": "100"},
                                {"side": "lay", "odds": "1.92", "available-amount": "100"},
                            ],
                        },
                    ],
                }
            ]
        }
        for market in payload["markets"]:
            if str(market.get("id")) == str(market_id):
                return market
        from sports_hedge.venues.matchbook import MatchbookMarketGoneError

        raise MatchbookMarketGoneError(event_id, market_id, 404)


class SyntheticPolymarket:
    def __init__(self, fixtures: list[tuple[str, str, datetime]]) -> None:
        self.fixtures = fixtures
        self.list_events_calls = 0
        self.list_markets_calls = 0
        self.book_calls = 0

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        self.list_events_calls += 1
        await asyncio.sleep(SYNTHETIC_PROVIDER_LATENCY_SECONDS)
        return [
            {
                "id": f"pm-stress-{index}",
                "title": f"{home} vs {away}",
                "startTime": kickoff.isoformat(),
                "competition": "Premier League",
                "series": [{"id": "10188", "title": "Premier League"}],
            }
            for index, (home, away, kickoff) in enumerate(self.fixtures)
        ]

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del filters
        self.list_markets_calls += 1
        await asyncio.sleep(SYNTHETIC_PROVIDER_LATENCY_SECONDS)
        return [
            {
                "id": f"pm-market-{event_id}",
                "question": "Both teams to score?",
                "sportsMarketType": "both teams to score",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": f'["yes-{event_id}", "no-{event_id}"]',
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
        assert outcome_id is not None
        self.book_calls += 1
        await asyncio.sleep(SYNTHETIC_PROVIDER_LATENCY_SECONDS)
        return {
            "asset_id": str(outcome_id),
            "timestamp": int(datetime.now(UTC).timestamp() * 1000) - 20,
            "bids": [{"price": "0.49", "size": "100"}],
            "asks": [{"price": "0.51", "size": "100"}],
        }


class SyntheticNoMarketPolymarket(SyntheticPolymarket):
    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        self.list_markets_calls += 1
        await asyncio.sleep(SYNTHETIC_PROVIDER_LATENCY_SECONDS)
        return []


def _collector(
    fixture_count: int,
) -> tuple[
    ReadOnlyCrossVenueCollector,
    SqliteMarketIntelligenceRepository,
    SyntheticMatchbook,
    FakeKalshiBTTS,
]:
    fixtures = _fixtures(fixture_count)
    matchbook = SyntheticMatchbook(fixtures)
    kalshi = FakeKalshiBTTS(fixtures, arb=False, latency_s=SYNTHETIC_PROVIDER_LATENCY_SECONDS)
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=SyntheticPolymarket(fixtures),
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        venue_timeout_seconds=0.5,
        provider_call_timeout_seconds=0.5,
        cycle_timeout_seconds=SYNTHETIC_CYCLE_BUDGET_SECONDS,
    )
    return collector, repository, matchbook, kalshi


def _assert_stable_report(
    report: CollectionReport, fixture_count: int, *, hot: bool = False
) -> None:
    diagnostics = report.scan_diagnostics
    assert len(report.discovered_fixtures) == fixture_count
    assert report.matched_event_pairs == fixture_count
    assert diagnostics["clusters_total"] == fixture_count
    assert diagnostics["clusters_evaluated"] == fixture_count
    assert diagnostics["clusters_leftover"] == 0
    assert diagnostics["provider_cancels"] == 0
    assert diagnostics["inflight_orphaned"] == 0
    assert diagnostics["inflight_live"] == 0
    assert diagnostics["timeout_count"] == 0
    assert diagnostics["stages"]["market_discovery"]["calls"] >= fixture_count
    classes = diagnostics["provider_call_classes"]
    if hot:
        assert diagnostics["stages"]["book_depth"]["calls"] >= fixture_count
        assert diagnostics["stages"]["mapping_equivalence"]["calls"] >= fixture_count
        assert diagnostics["stages"]["fees_fx_risk"]["calls"] >= fixture_count
        assert classes["executable_book_depth"] >= fixture_count
    else:
        # UNIVERSE catalogues from metadata. Executable books and solver
        # economics belong to the price engine, not discovery.
        assert diagnostics["stages"]["book_depth"]["calls"] == 0
        assert classes["executable_book_depth"] == 0
        assert classes["identity_metadata"] >= fixture_count
        assert diagnostics["universe_executable_pricing_deferred"] is True
        assert diagnostics["paper_decision_timing"] == "deferred_to_price_engine"
        assert report.paper_decisions == []
    observed = dict(diagnostics["matching_coverage"])
    observed.pop("catalogue_by_archetype", None)
    assert observed == {
        "fixtures": fixture_count,
        "single_venue_clusters": 0,
        "cross_venue_clusters": fixture_count,
        "matched_event_pairs": fixture_count,
        "inventory_cross_venue_fixtures": fixture_count,
        "equivalent_markets": fixture_count,
        "qualifying_arbs": 0,
        "matching_state": "cross_venue_equivalent_present",
        "zero_equivalent_reason_counts": {},
    }


async def _run_universe(
    collector: ReadOnlyCrossVenueCollector,
) -> tuple[CollectionReport, float]:
    started = monotonic()
    report = await collector.collect_and_scan(
        scan_lane=ScanLane.UNIVERSE.value,
        enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
        cycle_timeout_seconds=SYNTHETIC_CYCLE_BUDGET_SECONDS,
        venue_costs=matchbook_kalshi_costs("0"),
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"))],
    )
    return report, monotonic() - started


async def _run_hot(
    collector: ReadOnlyCrossVenueCollector,
    seed: CollectionReport,
) -> tuple[CollectionReport, float]:
    identity_scope = [item.canonical_event_id for item in seed.discovered_fixtures]
    started = monotonic()
    report = await collector.collect_and_scan(
        scan_lane=ScanLane.HOT.value,
        identity_scope=identity_scope,
        known_source_events=seed.fixture_source_events,
        hot_market_relationships=relationships_from_fixture_markets(seed.fixture_markets),
        enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
        cycle_timeout_seconds=SYNTHETIC_CYCLE_BUDGET_SECONDS,
        venue_costs=matchbook_kalshi_costs("0"),
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"))],
    )
    return report, monotonic() - started


@pytest.mark.parametrize("fixture_count", SYNTHETIC_WORKLOADS)
@pytest.mark.asyncio
async def test_synthetic_universe_stress_has_headroom_and_no_orphans(
    fixture_count: int,
) -> None:
    collector, repository, matchbook, kalshi = _collector(fixture_count)
    try:
        report, wall_seconds = await _run_universe(collector)
        _assert_stable_report(report, fixture_count)
        assert wall_seconds < SYNTHETIC_CYCLE_BUDGET_SECONDS * 0.8
        assert report.scan_diagnostics["total_ms"] <= wall_seconds * 1000 + 10
        assert matchbook.list_events_calls == kalshi.list_events_calls == 1
        assert kalshi.book_calls == 0
        print(
            json.dumps(
                {
                    "lane": "universe",
                    "fixtures": fixture_count,
                    "wall_ms": round(wall_seconds * 1000, 3),
                    "providers": report.scan_diagnostics["providers"],
                    "stages": report.scan_diagnostics["stages"],
                    "cancels": report.scan_diagnostics["provider_cancels"],
                    "orphans": report.scan_diagnostics["inflight_orphaned"],
                },
                sort_keys=True,
            )
        )
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_matching_diagnostics_separate_provider_overlap_from_market_equivalence() -> None:
    fixture_count = 4
    fixtures = _fixtures(fixture_count)
    matchbook = SyntheticMatchbook(fixtures)
    polymarket = SyntheticNoMarketPolymarket(fixtures)
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        cycle_timeout_seconds=SYNTHETIC_CYCLE_BUDGET_SECONDS,
    )
    try:
        report = await collector.collect_and_scan(
            scan_lane=ScanLane.UNIVERSE.value,
            enabled_venues=[VenueName.MATCHBOOK, VenueName.POLYMARKET],
            cycle_timeout_seconds=SYNTHETIC_CYCLE_BUDGET_SECONDS,
            venue_costs=matchbook_kalshi_costs("0"),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"))],
        )
        assert report.scan_diagnostics["matching_coverage"]["matching_state"] == (
            "multi_venue_identity_without_settlement_equivalent"
        )
        assert report.scan_diagnostics["matching_coverage"]["equivalent_markets"] == 0
        assert report.scan_diagnostics["matching_coverage"]["zero_equivalent_reason_counts"]
        assert all(
            item.no_comparison_reason == "no_normalized_market_family_overlap"
            for item in report.discovered_fixtures
        )
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_matching_diagnostics_report_no_multi_venue_identity_without_fabrication() -> None:
    fixture_count = 4
    collector, repository, _matchbook, _polymarket = _collector(fixture_count)
    try:
        report = await collector.collect_and_scan(
            scan_lane=ScanLane.UNIVERSE.value,
            enabled_venues=[VenueName.MATCHBOOK],
            cycle_timeout_seconds=SYNTHETIC_CYCLE_BUDGET_SECONDS,
        )
        coverage = report.scan_diagnostics["matching_coverage"]
        assert coverage["fixtures"] == fixture_count
        assert coverage["single_venue_clusters"] == fixture_count
        assert coverage["cross_venue_clusters"] == 0
        assert coverage["matched_event_pairs"] == 0
        assert coverage["equivalent_markets"] == 0
        assert coverage["qualifying_arbs"] == 0
        assert coverage["matching_state"] == "no_multi_venue_identity_match"
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_repeated_universe_cycles_do_not_accumulate_tasks_or_latency() -> None:
    fixture_count = 16
    collector, repository, _matchbook, _polymarket = _collector(fixture_count)
    try:
        wall_samples: list[float] = []
        live_tasks_before = len(
            [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        )
        for _ in range(SYNTHETIC_SOAK_CYCLES):
            report, wall_seconds = await _run_universe(collector)
            _assert_stable_report(report, fixture_count)
            wall_samples.append(wall_seconds)
            assert collector._inflight == set()
        live_tasks_after = len(
            [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        )
        assert live_tasks_after <= live_tasks_before
        assert max(wall_samples) < SYNTHETIC_CYCLE_BUDGET_SECONDS * 0.8
        first_half = sum(wall_samples[:6]) / 6
        second_half = sum(wall_samples[6:]) / 6
        assert second_half <= first_half * 1.75 + 0.02
        print(
            json.dumps(
                {
                    "lane": "universe_soak",
                    "fixtures": fixture_count,
                    "cycles": SYNTHETIC_SOAK_CYCLES,
                    "wall_ms_min": round(min(wall_samples) * 1000, 3),
                    "wall_ms_median": round(
                        sorted(wall_samples)[len(wall_samples) // 2] * 1000,
                        3,
                    ),
                    "wall_ms_max": round(max(wall_samples) * 1000, 3),
                    "task_delta": live_tasks_after - live_tasks_before,
                    "cancels": report.scan_diagnostics["provider_cancels"],
                    "orphans": report.scan_diagnostics["inflight_orphaned"],
                },
                sort_keys=True,
            )
        )
    finally:
        repository.close()


@pytest.mark.parametrize("fixture_count", SYNTHETIC_WORKLOADS)
@pytest.mark.asyncio
async def test_synthetic_hot_stress_skips_discovery_and_has_no_orphans(
    fixture_count: int,
) -> None:
    collector, repository, matchbook, kalshi = _collector(fixture_count)
    try:
        seed, _ = await _run_universe(collector)
        matchbook.list_events_calls = kalshi.list_events_calls = 0
        report, wall_seconds = await _run_hot(collector, seed)
        _assert_stable_report(report, fixture_count, hot=True)
        assert wall_seconds < SYNTHETIC_CYCLE_BUDGET_SECONDS * 0.8
        assert matchbook.list_events_calls == kalshi.list_events_calls == 0
        assert kalshi.book_calls >= fixture_count
        print(
            json.dumps(
                {
                    "lane": "hot",
                    "fixtures": fixture_count,
                    "wall_ms": round(wall_seconds * 1000, 3),
                    "providers": report.scan_diagnostics["providers"],
                    "stages": report.scan_diagnostics["stages"],
                    "cancels": report.scan_diagnostics["provider_cancels"],
                    "orphans": report.scan_diagnostics["inflight_orphaned"],
                },
                sort_keys=True,
            )
        )
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_repeated_hot_cycles_do_not_accumulate_tasks_or_latency() -> None:
    fixture_count = 16
    collector, repository, matchbook, kalshi = _collector(fixture_count)
    try:
        seed, _ = await _run_universe(collector)
        matchbook.list_events_calls = kalshi.list_events_calls = 0
        kalshi.book_calls = 0
        wall_samples: list[float] = []
        live_tasks_before = len(
            [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        )
        for _ in range(SYNTHETIC_SOAK_CYCLES):
            report, wall_seconds = await _run_hot(collector, seed)
            _assert_stable_report(report, fixture_count, hot=True)
            wall_samples.append(wall_seconds)
            assert collector._inflight == set()
        live_tasks_after = len(
            [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
        )
        assert matchbook.list_events_calls == kalshi.list_events_calls == 0
        assert kalshi.book_calls >= fixture_count * SYNTHETIC_SOAK_CYCLES
        assert live_tasks_after <= live_tasks_before
        assert max(wall_samples) < SYNTHETIC_CYCLE_BUDGET_SECONDS * 0.8
        first_half = sum(wall_samples[:6]) / 6
        second_half = sum(wall_samples[6:]) / 6
        assert second_half <= first_half * 1.75 + 0.02
        print(
            json.dumps(
                {
                    "lane": "hot_soak",
                    "fixtures": fixture_count,
                    "cycles": SYNTHETIC_SOAK_CYCLES,
                    "wall_ms_min": round(min(wall_samples) * 1000, 3),
                    "wall_ms_median": round(
                        sorted(wall_samples)[len(wall_samples) // 2] * 1000,
                        3,
                    ),
                    "wall_ms_max": round(max(wall_samples) * 1000, 3),
                    "task_delta": live_tasks_after - live_tasks_before,
                    "cancels": report.scan_diagnostics["provider_cancels"],
                    "orphans": report.scan_diagnostics["inflight_orphaned"],
                },
                sort_keys=True,
            )
        )
    finally:
        repository.close()
