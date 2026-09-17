from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic
from typing import Any

import pytest

from sports_hedge.application.collector import (
    CollectionReport,
    DEFAULT_MAX_EVENT_PAIRS,
    MarketEvaluationState,
    ReadOnlyCrossVenueCollector,
    SCAN_BUDGET_EXHAUSTED_REASON,
    SCAN_FINALISATION_RESERVE_SECONDS,
    finalisation_reserve_seconds,
)
from sports_hedge.application.live_refresh import (
    LiveRefreshCoordinator,
    SCAN_CYCLE_RETURN_GRACE_SECONDS,
    ScanCycleTimeout,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from test_read_only_collector import FakeMatchbook, FakePolymarket
from test_issue_147_market_evaluation_state import THREE_LEAGUE_FIXTURES
from test_venue_union_discovery import KALSHI_SERIES, NewcastleKalshi, NewcastlePolymarket
from venue_cost_helpers import matchbook_polymarket_costs, profit_commission_cost


class HungMatchbook(FakeMatchbook):
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        await asyncio.sleep(3600)
        return {"events": []}


class HungMarketsMatchbook(FakeMatchbook):
    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        await asyncio.sleep(3600)
        return {"markets": []}


class HungKalshi(NewcastleKalshi):
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        await asyncio.sleep(3600)
        return {"events": []}


class SlowMarketsPolymarket(FakePolymarket):
    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        await asyncio.sleep(30)
        return []


@pytest.mark.asyncio
async def test_hung_matchbook_does_not_block_healthy_polymarket_fixtures() -> None:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=HungMatchbook(),
        polymarket=FakePolymarket(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        venue_timeout_seconds=0.2,
        provider_call_timeout_seconds=0.2,
        cycle_timeout_seconds=2.0,
    )
    try:
        started = monotonic()
        report = await collector.collect_and_scan(
            venue_costs=matchbook_polymarket_costs("0.02", "0.02"),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"))],
            maximum_execution_risk=100,
        )
        elapsed = monotonic() - started
        assert elapsed < 2.5
        assert report.venue_health["matchbook"] == "timeout"
        assert report.venue_health["polymarket"] == "ok"
        assert any(item.polymarket_matched for item in report.discovered_fixtures)
        assert any(issue.stage == "list_events" and "timeout" in issue.detail for issue in report.issues)
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_partial_kalshi_timeout_still_returns_polymarket_kalshi_capable_fixtures() -> None:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=FakeMatchbook(),
        polymarket=NewcastlePolymarket(),
        kalshi=HungKalshi(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        venue_timeout_seconds=0.2,
        provider_call_timeout_seconds=0.2,
        cycle_timeout_seconds=2.0,
    )
    try:
        started = monotonic()
        report = await collector.collect_and_scan(
            venue_costs=[
                matchbook_polymarket_costs("0.02", "0")[0],
                profit_commission_cost(VenueName.POLYMARKET, "0"),
                kalshi_cost_from_series(KALSHI_SERIES),
            ],
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"))],
            maximum_execution_risk=100,
        )
        elapsed = monotonic() - started
        assert elapsed < 2.5
        assert report.venue_health["kalshi"] == "timeout"
        assert report.venue_health["polymarket"] == "ok"
        assert report.venue_health["matchbook"] == "ok"
        assert report.discovered_fixtures
        assert any(item.polymarket_matched or item.matchbook_matched for item in report.discovered_fixtures)
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_hung_list_markets_marks_matchbook_degraded_and_keeps_other_venues() -> None:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=HungMarketsMatchbook(),
        polymarket=FakePolymarket(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        venue_timeout_seconds=1.0,
        provider_call_timeout_seconds=0.2,
        cycle_timeout_seconds=2.0,
    )
    try:
        started = monotonic()
        report = await collector.collect_and_scan(
            venue_costs=matchbook_polymarket_costs("0.02", "0.02"),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"))],
            maximum_execution_risk=100,
        )
        elapsed = monotonic() - started
        assert elapsed < 2.5
        assert report.venue_health["matchbook"] in {"degraded", "timeout"}
        assert report.venue_health["polymarket"] == "ok"
        assert any(item.polymarket_matched for item in report.discovered_fixtures)
        assert any(issue.stage == "list_markets" and "timeout" in issue.detail for issue in report.issues)
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_run_cycle_timeout_clears_in_progress_and_records_error() -> None:
    coordinator = LiveRefreshCoordinator()
    coordinator.reset()

    async def hung() -> None:
        await asyncio.sleep(30)

    started = monotonic()
    with pytest.raises(ScanCycleTimeout, match="scan_cycle_timeout"):
        await coordinator.run_cycle(hung, timeout_seconds=0.2)
    elapsed = monotonic() - started
    assert elapsed < 2.0
    assert coordinator.status.cycle_in_progress is False
    assert coordinator.status.last_error is not None
    assert "timeout" in coordinator.status.last_error
    assert coordinator.status.last_completed_at is not None
    assert coordinator.status.last_started_at is not None

    async def ok() -> CollectionReport:
        now = datetime.now(UTC)
        return CollectionReport(started_at=now, completed_at=now)

    recovered = await coordinator.run_cycle(ok, timeout_seconds=1.0)
    assert recovered.started_at is not None
    assert coordinator.status.cycle_in_progress is False
    assert coordinator.status.last_error is None


@pytest.mark.asyncio
async def test_slow_cluster_markets_still_return_discovered_fixtures() -> None:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=HungMatchbook(),
        polymarket=SlowMarketsPolymarket(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        venue_timeout_seconds=0.2,
        provider_call_timeout_seconds=5.0,
        cycle_timeout_seconds=0.8,
    )
    try:
        started = monotonic()
        report = await collector.collect_and_scan(
            venue_costs=matchbook_polymarket_costs("0.02", "0.02"),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"))],
            maximum_execution_risk=100,
        )
        elapsed = monotonic() - started
        assert elapsed < 2.5
        assert report.venue_health["matchbook"] == "timeout"
        assert report.discovered_fixtures
        assert any(item.polymarket_matched for item in report.discovered_fixtures)
        assert any(
            issue.stage in {"list_markets", "collect", "get_order_book"} for issue in report.issues
        )
    finally:
        repository.close()


# These synthetic fixtures exercise bounded partial collection, not lifecycle expiry.
# Keep them safely pre-kickoff for the lifetime of a test process so calendar time
# cannot change whether /paper/live-refresh is expected to retain them.
_SIXTY_FIXTURE_KICKOFF = datetime.now(UTC).replace(minute=0, second=0, microsecond=0) + timedelta(
    days=1
)


def _sixty_fixtures() -> list[tuple[str, str, str, datetime]]:
    assert len(THREE_LEAGUE_FIXTURES) == 30
    fixtures: list[tuple[str, str, str, datetime]] = []
    for day in (0, 1):
        kickoff = _SIXTY_FIXTURE_KICKOFF + timedelta(days=day)
        for competition, home, away in THREE_LEAGUE_FIXTURES:
            fixtures.append((competition, home, away, kickoff))
    assert len(fixtures) == DEFAULT_MAX_EVENT_PAIRS
    return fixtures


_SERIES_BY_LEAGUE = {
    "Premier League": ("10188", "KXEPLGAME"),
    "Championship": ("10355", "KXEFLCHAMPIONSHIPGAME"),
    "La Liga": ("10193", "KXLALIGAGAME"),
}


class SixtyResponsiveMatchbook:
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {
            "events": [
                {
                    "id": 30000 + index,
                    "name": f"{home} vs {away}",
                    "start": kickoff.isoformat(),
                    "competition-name": competition,
                }
                for index, (competition, home, away, kickoff) in enumerate(_sixty_fixtures())
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        await asyncio.sleep(30)
        return {"markets": []}


class SixtyResponsivePolymarket:
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        events = []
        for index, (competition, home, away, kickoff) in enumerate(_sixty_fixtures()):
            series_id, _kalshi = _SERIES_BY_LEAGUE[competition]
            events.append(
                {
                    "id": f"pm-sixty-{index}",
                    "title": f"{home} vs {away}",
                    "startTime": kickoff.isoformat(),
                    "competition": competition,
                    "series": [{"id": series_id, "title": competition}],
                }
            )
        return events

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        await asyncio.sleep(30)
        return []

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id, outcome_id, filters
        await asyncio.sleep(30)
        return {}


class SixtyResponsiveKalshi:
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        events = []
        for index, (competition, home, away, kickoff) in enumerate(_sixty_fixtures()):
            _series_id, ticker = _SERIES_BY_LEAGUE[competition]
            events.append(
                {
                    "event_ticker": f"{ticker}-{index}",
                    "series_ticker": ticker,
                    "title": f"{home} vs {away}",
                    "category": "Sports",
                    "strike_date": kickoff.isoformat(),
                }
            )
        return {"events": events}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        await asyncio.sleep(30)
        return {"markets": []}

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        del series_ticker
        return {"ticker": "KXEPLGAME", "title": "Premier League"}

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id, outcome_id, filters
        await asyncio.sleep(30)
        return {}


class StickyMarketsPolymarket(FakePolymarket):
    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            await asyncio.sleep(20)
            raise
        return []


@pytest.mark.asyncio
async def test_uncooperative_market_cancel_still_returns_partial_report() -> None:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=FakeMatchbook(),
        polymarket=StickyMarketsPolymarket(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        venue_timeout_seconds=0.2,
        provider_call_timeout_seconds=0.2,
        cycle_timeout_seconds=0.8,
    )
    try:
        started = monotonic()
        report = await collector.collect_and_scan(
            venue_costs=matchbook_polymarket_costs("0.02", "0.02"),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"))],
            maximum_execution_risk=100,
        )
        elapsed = monotonic() - started
        assert elapsed < 2.0
        assert report.discovered_fixtures
        assert report.venue_health["matchbook"] == "ok"
        assert report.venue_health["polymarket"] in {"ok", "degraded", "timeout"}
        assert report.scan_diagnostics["provider_cancels"] >= 1
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_sixty_slow_market_clusters_return_partial_before_hard_timeout() -> None:
    cycle = 2.0
    hard = cycle + SCAN_CYCLE_RETURN_GRACE_SECONDS
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=SixtyResponsiveMatchbook(),
        polymarket=SixtyResponsivePolymarket(),
        kalshi=SixtyResponsiveKalshi(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        venue_timeout_seconds=0.4,
        provider_call_timeout_seconds=8.0,
        cycle_timeout_seconds=cycle,
    )
    try:
        started = monotonic()
        report = await collector.collect_and_scan(
            venue_costs=matchbook_polymarket_costs("0.02", "0.02"),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"))],
            maximum_execution_risk=100,
            max_event_pairs=DEFAULT_MAX_EVENT_PAIRS,
        )
        elapsed = monotonic() - started
        assert elapsed < hard
        assert elapsed >= cycle - finalisation_reserve_seconds(cycle) - 0.5
        assert report.venue_health["matchbook"] in {"ok", "degraded", "timeout"}
        assert report.venue_health["polymarket"] in {"ok", "degraded", "timeout"}
        assert report.venue_health["kalshi"] in {"ok", "degraded", "timeout"}
        assert len(report.discovered_fixtures) == DEFAULT_MAX_EVENT_PAIRS
        leftovers = [
            item
            for item in report.discovered_fixtures
            if item.market_evaluation_state == MarketEvaluationState.NOT_EVALUATED_SCAN_DEADLINE
        ]
        assert leftovers
        assert all(item.matched_equivalent_count is None for item in leftovers)
        assert all(
            item.market_evaluation_reason == SCAN_BUDGET_EXHAUSTED_REASON for item in leftovers
        )
        assert any(issue.detail == "scan_cycle_deadline_reached" for issue in report.issues)
        diagnostics = report.scan_diagnostics
        assert diagnostics["soft_deadline_reached"] is True
        assert diagnostics["clusters_total"] == DEFAULT_MAX_EVENT_PAIRS
        assert diagnostics["clusters_leftover"] == len(leftovers)
        assert "event_discovery_ms" in diagnostics
        assert "cluster_scan_ms" in diagnostics
        assert "assembly_ms" in diagnostics
        assert "partial" in report.operator_summary
        assert SCAN_FINALISATION_RESERVE_SECONDS == 4.0
    finally:
        repository.close()
