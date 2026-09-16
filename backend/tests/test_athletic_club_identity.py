from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

import pytest

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.target_competitions import (
    EVENT_IDENTITY_MISMATCH,
    SERIES_NOT_QUERIED,
    UNMATCHED_POLYMARKET_COVERAGE,
    polymarket_series_ids_for_targets,
)
from sports_hedge.config import Settings
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.facts.identity import canonical_team_id
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.events import EventMatcher
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import matchbook_polymarket_costs


KICKOFF = datetime(2026, 9, 12, 16, 30, tzinfo=UTC)


def _canonical(
    venue: VenueName,
    home: str,
    away: str,
    *,
    competition: str = "La Liga",
) -> CanonicalEvent:
    return CanonicalEvent(
        competition=competition,
        home_team=home,
        away_team=away,
        kickoff_utc=KICKOFF,
        source_venue=venue,
        source_event_id=f"{venue.value}-athletic",
    )


def test_athletic_bilbao_and_athletic_club_share_canonical_team_identity() -> None:
    assert canonical_team_id("Athletic Bilbao") == canonical_team_id("Athletic Club")
    assert canonical_team_id("Ath Bilbao") == canonical_team_id("Athletic Club")
    assert canonical_team_id("Elche CF") == canonical_team_id("Elche")


def test_event_matcher_keeps_threshold_and_matches_athletic_via_alias() -> None:
    matcher = EventMatcher()
    assert matcher.threshold == 0.92
    raw_home = SequenceMatcher(a="athletic bilbao", b="athletic club").ratio()
    assert raw_home < 0.92

    result = matcher.match(
        _canonical(VenueName.MATCHBOOK, "Athletic Bilbao", "Elche"),
        _canonical(VenueName.POLYMARKET, "Athletic Club", "Elche CF"),
    )
    assert result.matched is True
    assert result.confidence >= 0.92
    assert "home_team_fuzzy" not in result.reasons
    assert "away_team_fuzzy" not in result.reasons


def test_event_matcher_does_not_match_unrelated_la_liga_clubs() -> None:
    result = EventMatcher().match(
        _canonical(VenueName.MATCHBOOK, "Athletic Bilbao", "Elche"),
        _canonical(VenueName.POLYMARKET, "Real Madrid", "Barcelona"),
    )
    assert result.matched is False


def _btts_market(market_id: int) -> dict[str, Any]:
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


class AthleticMatchbook:
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {
            "events": [
                {
                    "id": 8803,
                    "name": "Athletic Bilbao vs Elche",
                    "start": KICKOFF.isoformat(),
                    "sport-name": "Football",
                    "competition-name": "La Liga",
                    "status": "open",
                }
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        return {"markets": [_btts_market(int(event_id))]}


class AthleticPolymarket:
    def __init__(self, *, title: str, competition: str = "La Liga") -> None:
        self.title = title
        self.competition = competition

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return [
            {
                "id": "pm-lal-athletic",
                "title": self.title,
                "startTime": KICKOFF.isoformat(),
                "competition": self.competition,
                "series": [{"id": "10193", "title": "La Liga"}],
            }
        ]

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return [
            {
                "id": "pm-market-athletic",
                "question": "Both teams to score?",
                "sportsMarketType": "both teams to score",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["yes-token", "no-token"]',
                "description": "Resolves based on 90 minutes of regulation time.",
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
        token = str(outcome_id)
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        if token == "yes-token":
            return {
                "asset_id": token,
                "timestamp": now_ms - 200,
                "bids": [{"price": "0.49", "size": "250"}],
                "asks": [{"price": "0.51", "size": "250"}],
            }
        return {
            "asset_id": token,
            "timestamp": now_ms - 180,
            "bids": [{"price": "0.41", "size": "300"}],
            "asks": [{"price": "0.43", "size": "300"}],
        }


@pytest.mark.asyncio
async def test_collector_matches_athletic_bilbao_to_athletic_club_and_compares_markets() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    collector = ReadOnlyCrossVenueCollector(
        matchbook=AthleticMatchbook(),
        polymarket=AthleticPolymarket(title="Athletic Club vs Elche CF"),
        paper_scan=PaperScanService(intelligence),
    )
    try:
        report = await collector.collect_and_scan(
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            capital_limit_gbp=Decimal("100"),
            maximum_execution_risk=100,
            polymarket_queried_series_ids=["10188", "10355", "10193"],
        )
        fixture = report.discovered_fixtures[0]
        assert fixture.home_team == "Athletic Bilbao"
        assert fixture.polymarket_matched is True
        assert fixture.no_comparison_reason is None
        assert fixture.matched_market_count == 1
        assert fixture.market_family == "both_teams_to_score"
        assert fixture.current_net_edge is not None
        assert fixture.solver_is_arbitrage is True
        assert report.matched_event_pairs == 1
        assert report.paper_decisions
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_unmatched_reason_is_identity_when_la_liga_coverage_exists() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    collector = ReadOnlyCrossVenueCollector(
        matchbook=AthleticMatchbook(),
        polymarket=AthleticPolymarket(title="Real Madrid vs Barcelona"),
        paper_scan=PaperScanService(intelligence),
    )
    try:
        report = await collector.collect_and_scan(
            polymarket_queried_series_ids=["10188", "10355", "10193"],
        )
        fixture = report.discovered_fixtures[0]
        assert fixture.polymarket_matched is False
        assert fixture.no_comparison_reason == EVENT_IDENTITY_MISMATCH
        assert fixture.no_comparison_reason != UNMATCHED_POLYMARKET_COVERAGE
    finally:
        repository.close()


class EmptyPolymarket:
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return []

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return []

    async def get_order_book(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise AssertionError("order books should not be fetched without a match")


@pytest.mark.asyncio
async def test_unmatched_reason_is_series_not_queried_for_legacy_epl_override() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    collector = ReadOnlyCrossVenueCollector(
        matchbook=AthleticMatchbook(),
        polymarket=EmptyPolymarket(),
        paper_scan=PaperScanService(intelligence),
    )
    try:
        report = await collector.collect_and_scan(
            polymarket_queried_series_ids=["10188"],
        )
        fixture = report.discovered_fixtures[0]
        assert fixture.polymarket_matched is False
        assert fixture.no_comparison_reason == SERIES_NOT_QUERIED
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_unmatched_reason_is_coverage_when_la_liga_series_was_queried_empty() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    collector = ReadOnlyCrossVenueCollector(
        matchbook=AthleticMatchbook(),
        polymarket=EmptyPolymarket(),
        paper_scan=PaperScanService(intelligence),
    )
    try:
        report = await collector.collect_and_scan(
            polymarket_queried_series_ids=["10188", "10355", "10193"],
        )
        fixture = report.discovered_fixtures[0]
        assert fixture.polymarket_matched is False
        assert fixture.no_comparison_reason == UNMATCHED_POLYMARKET_COVERAGE
    finally:
        repository.close()


def test_legacy_single_series_already_in_targets_is_not_an_operator_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level("WARNING", logger="sports_hedge.config"):
        settings = Settings(polymarket_gamma_series_id="10188")
    assert settings.resolved_polymarket_series_ids() == polymarket_series_ids_for_targets()
    assert settings.polymarket_series_config_warnings() == []
    assert not any("legacy single-series" in record.message for record in caplog.records)
    assert not any("POLYMARKET_GAMMA_SERIES_ID" in record.message for record in caplog.records)
