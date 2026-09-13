from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic
from typing import Any

import pytest

from sports_hedge.application.collector import CollectionReport, ReadOnlyCrossVenueCollector
from sports_hedge.application.live_refresh import LiveRefreshCoordinator, ScanCycleTimeout
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from test_read_only_collector import FakeMatchbook, FakePolymarket
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
