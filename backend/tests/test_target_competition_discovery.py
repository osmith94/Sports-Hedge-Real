from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.target_competitions import (
    UNMATCHED_POLYMARKET_COVERAGE,
    TargetCompetitionCode,
    resolve_target_competition,
    scope_matchbook_event,
)
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import matchbook_polymarket_costs


KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)


VARIANT_LABELS = {
    TargetCompetitionCode.PREMIER_LEAGUE: (
        "Premier League",
        "English Premier League",
        "EPL",
        "Barclays Premier League",
    ),
    TargetCompetitionCode.CHAMPIONSHIP: (
        "Championship",
        "EFL Championship",
        "Sky Bet Championship",
        "English Championship",
    ),
    TargetCompetitionCode.LA_LIGA: (
        "La Liga",
        "LaLiga",
        "Primera Division",
        "Primera División",
    ),
}

REJECTED_LABELS = (
    "Women's Cricket",
    "Rugby Union",
    "3. Liga",
    "Germany 3. Liga",
    "Bundesliga",
    "USL Championship",
    "LaLiga2",
    "EFL CUP",
    "Champions League",
    "Serie A",
    "",
)


@pytest.mark.parametrize(
    ("label", "code"),
    [
        (label, code)
        for code, labels in VARIANT_LABELS.items()
        for label in labels
    ],
)
def test_target_competition_aliases_are_accepted(label: str, code: TargetCompetitionCode) -> None:
    resolved = resolve_target_competition(label)
    assert resolved is not None
    assert resolved.code == code


@pytest.mark.parametrize("label", REJECTED_LABELS)
def test_unknown_or_adjacent_competitions_fail_closed(label: str) -> None:
    assert resolve_target_competition(label) is None


def test_matchbook_scope_excludes_unrelated_sport_even_if_label_looks_plausible() -> None:
    cricket = scope_matchbook_event(
        {
            "id": 1,
            "name": "England Women vs India Women",
            "sport-name": "Cricket",
            "competition-name": "Premier League",
        }
    )
    assert cricket.allowed is False
    assert cricket.reason == "non_football_sport"


def _event(
    event_id: int,
    name: str,
    competition: str,
    *,
    sport: str | None = "Football",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": event_id,
        "name": name,
        "start": KICKOFF.isoformat(),
        "competition-name": competition,
        "status": "open",
    }
    if sport is not None:
        payload["sport-name"] = sport
    return payload


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


class ScopedMatchbook:
    def __init__(self) -> None:
        self.list_markets_calls: list[str] = []

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {
            "events": [
                _event(1001, "Newcastle United vs Chelsea", "Premier League"),
                _event(2001, "Leeds United vs Leicester City", "EFL Championship"),
                _event(3001, "Real Madrid vs Barcelona", "La Liga"),
                _event(4001, "England Women vs India Women", "Women's Cricket", sport="Cricket"),
                _event(4002, "England vs France", "Rugby Union", sport="Rugby Union"),
                _event(4003, "Aachen vs Essen", "3. Liga"),
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_markets_calls.append(str(event_id))
        return {"markets": [_btts_market(int(event_id))]}


class PartialPolymarket:
    """EPL coverage only. Championship/La Liga stay unmatched without invented markets."""

    def __init__(self) -> None:
        self.list_events_filters: list[dict[str, Any]] = []
        self.book_calls: list[str] = []

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        self.list_events_filters.append(dict(filters))
        return [
            {
                "id": "pm-epl-1",
                "title": "Newcastle United vs Chelsea",
                "startTime": KICKOFF.isoformat(),
                "competition": "Premier League",
                "series": [{"title": "Premier League"}],
            }
        ]

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return [
            {
                "id": "pm-market-1",
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
        self.book_calls.append(token)
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
async def test_collector_scopes_discovery_and_keeps_unmatched_coverage_truthful() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    matchbook = ScopedMatchbook()
    polymarket = PartialPolymarket()
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
        ids = {item.source_event_id: item for item in report.discovered_fixtures}
        assert set(ids) == {"1001", "2001", "3001"}
        assert "4001" not in ids
        assert "4002" not in ids
        assert "4003" not in ids
        assert any(issue.stage == "target_competition" for issue in report.issues)

        epl = ids["1001"]
        assert epl.polymarket_matched is True
        assert epl.target_competition_code == "premier_league"
        assert epl.matched_market_count == 1
        assert epl.market_family == "both_teams_to_score"
        assert epl.outcome_context == "yes/no"
        assert epl.best_matchbook_price is not None
        assert epl.best_polymarket_price is not None
        assert epl.current_net_edge is not None
        assert epl.trigger_net_edge == Decimal("0.005")
        assert epl.distance_to_trigger_pp is not None
        assert epl.quote_age_ms is not None
        assert epl.no_comparison_reason is None
        assert epl.solver_is_arbitrage is True

        championship = ids["2001"]
        assert championship.target_competition_code == "championship"
        assert championship.polymarket_matched is False
        assert championship.no_comparison_reason == UNMATCHED_POLYMARKET_COVERAGE
        assert championship.current_net_edge is None
        assert championship.solver_is_arbitrage is False

        la_liga = ids["3001"]
        assert la_liga.target_competition_code == "la_liga"
        assert la_liga.polymarket_matched is False
        assert la_liga.no_comparison_reason == UNMATCHED_POLYMARKET_COVERAGE
        assert matchbook.list_markets_calls == ["1001"]
    finally:
        repository.close()
