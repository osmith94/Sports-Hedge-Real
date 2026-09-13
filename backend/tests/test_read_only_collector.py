from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import matchbook_polymarket_costs


KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)


class FakeMatchbook:
    def __init__(self, *, duplicate_event: bool = False) -> None:
        self.duplicate_event = duplicate_event
        self.list_markets_calls: list[str] = []

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        events = [
            {
                "id": 1001,
                "name": "Newcastle United vs Chelsea",
                "start": KICKOFF.isoformat(),
                "competition-name": "Premier League",
            },
            {
                "id": 9999,
                "name": "Outright Premier League winner",
                "start": KICKOFF.isoformat(),
                "competition-name": "Premier League",
            },
        ]
        if self.duplicate_event:
            events.insert(
                1,
                {
                    "id": 1002,
                    "name": "Newcastle United vs Chelsea",
                    "start": KICKOFF.isoformat(),
                    "competition-name": "Premier League",
                },
            )
        return {"events": events}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_markets_calls.append(str(event_id))
        return {
            "markets": [
                {
                    "id": 2001,
                    "name": "Both Teams To Score",
                    "runners": [
                        {
                            "id": 301,
                            "name": "Yes",
                            "prices": [
                                {"side": "back", "odds": "2.20", "available-amount": "100"},
                                {"side": "lay", "odds": "2.22", "available-amount": "100"},
                            ],
                        },
                        {
                            "id": 302,
                            "name": "No",
                            "prices": [
                                {"side": "back", "odds": "1.80", "available-amount": "100"},
                                {"side": "lay", "odds": "1.82", "available-amount": "100"},
                            ],
                        },
                    ],
                },
                {
                    "id": 2999,
                    "name": "Novelty unsupported market",
                    "runners": [{"id": 3999, "name": "Yes", "prices": []}],
                },
            ]
        }


class FakePolymarket:
    def __init__(self) -> None:
        self.list_markets_calls: list[str] = []
        self.book_calls: list[str] = []

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return [
            {
                "id": "pm-event-1",
                "title": "Newcastle United vs Chelsea",
                "startTime": KICKOFF.isoformat(),
                "competition": "Premier League",
            },
            {
                "id": "pm-event-2",
                "title": "Arsenal vs Liverpool",
                "startTime": KICKOFF.isoformat(),
                "competition": "Premier League",
            },
        ]

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del filters
        self.list_markets_calls.append(str(event_id))
        return [
            {
                "id": "pm-market-1",
                "question": "Both teams to score?",
                "sportsMarketType": "both teams to score",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["yes-token", "no-token"]',
                "description": "Resolves based on 90 minutes of regulation time.",
                "feesEnabled": False,
            },
            {
                "id": "pm-market-unsupported",
                "question": "Will there be a pitch invasion?",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["a", "b"]',
                "description": "Resolves based on 90 minutes of regulation time.",
                "feesEnabled": False,
            },
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
        token = str(outcome_id)
        self.book_calls.append(token)
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        if token == "yes-token":
            return {
                "asset_id": token,
                "timestamp": now_ms - 200,
                "bids": [{"price": "0.49", "size": "250"}],
                "asks": [{"price": "0.51", "size": "250"}],
            }
        if token == "no-token":
            return {
                "asset_id": token,
                "timestamp": now_ms - 180,
                "bids": [{"price": "0.41", "size": "300"}],
                "asks": [{"price": "0.43", "size": "300"}],
            }
        raise AssertionError(f"unexpected token: {token}")


@pytest.mark.asyncio
async def test_collector_discovers_matches_fetches_books_and_feeds_paper_pipeline() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    matchbook = FakeMatchbook()
    polymarket = FakePolymarket()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        paper_scan=PaperScanService(intelligence),
    )

    try:
        report = await collector.collect_and_scan(
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            capital_limit_gbp=Decimal("100"),
            maximum_execution_risk=100,
        )

        assert report.discovery_source == VenueName.MATCHBOOK
        assert report.matching_venue == VenueName.POLYMARKET
        assert report.raw_matchbook_events == 2
        assert report.raw_polymarket_events == 2
        assert report.normalized_matchbook_events == 1
        assert report.normalized_polymarket_events == 2
        assert report.matched_event_pairs == 1
        assert report.normalized_matchbook_markets == 1
        assert report.normalized_polymarket_markets >= 1
        assert report.matched_market_pairs == 1
        assert report.discovery_mode == "venue_union"
        assert report.skipped_out_of_scope == 0
        discovered = {item.source_event_id: item for item in report.discovered_fixtures}
        assert "1001" in discovered
        assert discovered["1001"].polymarket_matched is True
        assert discovered["1001"].matchbook_matched is True
        assert "pm-event-2" in discovered
        assert discovered["pm-event-2"].matchbook_matched is False
        assert discovered["pm-event-2"].polymarket_matched is True
        assert report.order_books_fetched >= 2
        assert "yes-token" in polymarket.book_calls
        assert "no-token" in polymarket.book_calls
        assert len(report.paper_decisions) == 1
        assert discovered["1001"].live_score_supported is False
        assert discovered["1001"].home_score is None
        assert discovered["1001"].matched_market_count == 1
        assert discovered["1001"].market_family == "both_teams_to_score"
        assert discovered["1001"].current_net_edge is not None
        assert discovered["1001"].trigger_net_edge is not None
        assert discovered["1001"].distance_to_trigger_pp is not None
        assert discovered["1001"].solver_is_arbitrage is True
        assert discovered["1001"].no_comparison_reason is None
        decision = report.paper_decisions[0]
        assert decision.fixture_discovery_source == VenueName.MATCHBOOK
        assert decision.live_score_supported is False
        assert decision.quote_age_ms is not None
        assert decision.quote_age_ms < 2000
        assert "unknown_quote_age" not in decision.rejection_reasons
        assert decision.depth_scan is not None
        assert decision.depth_scan.solution.is_arbitrage is True
        assert decision.eligible_for_paper_simulation is True
        assert report.paper_eligible_count == 1
        assert any(issue.stage == "normalize_event" for issue in report.issues)
        assert sum(issue.stage == "normalize_market" for issue in report.issues) >= 2

        history = intelligence.market_history(canonical_market_id=decision.canonical_market_id)
        assert len(history) == 4
        assert {snapshot.venue for snapshot in history} == {
            VenueName.MATCHBOOK,
            VenueName.POLYMARKET,
        }
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_collector_pairs_each_event_only_once() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    matchbook = FakeMatchbook(duplicate_event=True)
    polymarket = FakePolymarket()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        paper_scan=PaperScanService(intelligence),
    )

    try:
        report = await collector.collect_and_scan(
            venue_costs=matchbook_polymarket_costs("0.02", "0.02"),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            maximum_execution_risk=100,
        )
        assert report.normalized_matchbook_events == 2
        assert report.matched_event_pairs == 1
        assert len(matchbook.list_markets_calls) == 2
        assert len(polymarket.list_markets_calls) >= 1
    finally:
        repository.close()


class BrokenPolymarket(FakePolymarket):
    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id, outcome_id, filters
        raise RuntimeError("temporary public book failure")


@pytest.mark.asyncio
async def test_collector_reports_book_failure_without_crashing_scan() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    collector = ReadOnlyCrossVenueCollector(
        matchbook=FakeMatchbook(),
        polymarket=BrokenPolymarket(),
        paper_scan=PaperScanService(intelligence),
    )

    try:
        report = await collector.collect_and_scan(maximum_execution_risk=100)
        assert report.matched_event_pairs == 1
        assert report.matched_market_pairs == 1
        assert report.paper_decisions == []
        assert any(
            issue.stage == "order_book" and "temporary public book failure" in issue.detail
            for issue in report.issues
        )
    finally:
        repository.close()


class HangingMatchbook:
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        await asyncio.sleep(30)
        return {"events": []}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        await asyncio.sleep(30)
        return {"markets": []}


@pytest.mark.asyncio
async def test_collector_returns_healthy_venue_fixtures_when_matchbook_hangs() -> None:
    import time

    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    collector = ReadOnlyCrossVenueCollector(
        matchbook=HangingMatchbook(),
        polymarket=FakePolymarket(),
        paper_scan=PaperScanService(intelligence),
        venue_timeout_seconds=0.3,
        provider_call_timeout_seconds=0.3,
        cycle_timeout_seconds=2.0,
    )
    started = time.monotonic()
    try:
        report = await collector.collect_and_scan(maximum_execution_risk=100)
        elapsed = time.monotonic() - started
        assert elapsed < 2.0
        assert report.venue_health[VenueName.MATCHBOOK.value] == "timeout"
        assert report.venue_health[VenueName.POLYMARKET.value] == "ok"
        assert report.raw_matchbook_events == 0
        assert report.raw_polymarket_events == 2
        discovered = {item.source_event_id: item for item in report.discovered_fixtures}
        assert "pm-event-1" in discovered or "pm-event-2" in discovered
        assert any(item.polymarket_matched for item in report.discovered_fixtures)
        assert not any(item.matchbook_matched for item in report.discovered_fixtures)
        assert any(
            issue.stage == "list_events"
            and issue.venue is VenueName.MATCHBOOK
            and "timeout" in issue.detail
            for issue in report.issues
        )
    finally:
        repository.close()
