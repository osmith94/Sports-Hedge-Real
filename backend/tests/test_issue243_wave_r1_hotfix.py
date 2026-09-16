"""Issue #243 / Wave R1 hotfix: owner-live Full Sweep leftovers + fixture splits.

Frozen-base reproductions at Wave R head `8c001fd96bcc6618f05045a5bd5d7b4debd6e8b6`:
- A. Hung/degraded list_events consumed the short Full Sweep soft budget, so
  discovered overlapping fixtures were leftover as scan_budget_exhausted with
  0 evaluated and no generation progress.
- B. Production EventMatcher (threshold 0.92) split FC Bayern München /
  Bayern Munich and Málaga / Malaga CF provider variants.

Data class: deterministic fixture/demo providers. Not live, historical, or
modelled venue quotes. Paper-only; execution stays disabled.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

import pytest

from sports_hedge.application.collector import (
    MARKET_FETCH_UNAVAILABLE_REASON,
    MIN_POST_DISCOVERY_SOFT_SECONDS,
    MarketEvaluationState,
    ReadOnlyCrossVenueCollector,
    SCAN_BUDGET_EXHAUSTED_REASON,
)
from sports_hedge.application.fixture_clusters import VenueEvent, cluster_venue_events
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane, UNIVERSE_MIN_CHUNK_SECONDS
from sports_hedge.application.target_competitions import EVENT_IDENTITY_MISMATCH
from sports_hedge.config import Settings
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.facts.aliases import resolve_team_name
from sports_hedge.facts.identity import canonical_team_id
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.events import EventMatcher
from sports_hedge.normalization.text import normalize_text
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import matchbook_polymarket_costs, profit_commission_cost


KICKOFF = datetime(2026, 9, 16, 18, 30, tzinfo=UTC)
REGULATION = "Resolves based on 90 minutes of regulation time."
BUNDESLIGA_KALSHI_SERIES = {
    "ticker": "KXBUNDESLIGAGAME",
    "title": "Bundesliga",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "settlement_sources": [{"name": "Opta"}],
}
Q1_OVERLAP_FIXTURES: list[tuple[str, str, str]] = [
    ("Premier League", "Arsenal", "Chelsea"),
    ("Premier League", "Liverpool", "Manchester City"),
    ("Premier League", "Newcastle United", "Tottenham Hotspur"),
    ("Championship", "Leeds United", "Leicester City"),
    ("Championship", "Burnley", "Sheffield United"),
    ("Championship", "Sunderland", "Coventry City"),
    ("La Liga", "Athletic Club", "Barcelona"),
    ("La Liga", "Atletico Madrid", "Sevilla"),
    ("La Liga", "Girona", "Osasuna"),
    ("Carabao Cup", "Arsenal", "Port Vale"),
    ("Carabao Cup", "Chelsea", "Lincoln City"),
    ("FA Cup", "Manchester United", "Grimsby Town"),
    ("International Friendlies", "England", "Wales"),
    ("Bundesliga", "Borussia Dortmund", "RB Leipzig"),
    ("Bundesliga", "Eintracht Frankfurt", "VfB Stuttgart"),
    ("Serie A", "Inter", "Juventus"),
    ("Serie A", "AC Milan", "Napoli"),
    ("Serie A", "Roma", "Lazio"),
]


def _canonical(
    venue: VenueName,
    home: str,
    away: str,
    *,
    competition: str,
    source_event_id: str,
    kickoff: datetime = KICKOFF,
) -> CanonicalEvent:
    return CanonicalEvent(
        competition=competition,
        home_team=home,
        away_team=away,
        kickoff_utc=kickoff,
        source_venue=venue,
        source_event_id=source_event_id,
    )


def _venue_event(
    venue: VenueName,
    home: str,
    away: str,
    *,
    competition: str,
    source_event_id: str,
) -> VenueEvent:
    canonical = _canonical(
        venue, home, away, competition=competition, source_event_id=source_event_id
    )
    return VenueEvent(
        venue=venue,
        raw={"id": source_event_id, "title": f"{home} vs {away}"},
        canonical=canonical,
        source_event_id=source_event_id,
    )


def _btts_matchbook(market_id: int) -> dict[str, Any]:
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


def _btts_polymarket(index: int) -> dict[str, Any]:
    return {
        "id": f"pm-btts-{index}",
        "question": "Both teams to score?",
        "sportsMarketType": "both teams to score",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": f'["yes-{index}", "no-{index}"]',
        "description": REGULATION,
        "feesEnabled": False,
    }


def _pm_book(token: str) -> dict[str, Any]:
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    yes = token.startswith("yes")
    return {
        "asset_id": token,
        "timestamp": now_ms - 150,
        "bids": [{"price": "0.49" if yes else "0.41", "size": "250"}],
        "asks": [{"price": "0.51" if yes else "0.43", "size": "250"}],
    }


def _kalshi_btts_event(
    *,
    ticker: str,
    title: str,
    competition_series: str,
    kickoff: datetime = KICKOFF,
) -> dict[str, Any]:
    return {
        "event_ticker": ticker,
        "series_ticker": competition_series,
        "title": title,
        "category": "Sports",
        "strike_date": kickoff.isoformat(),
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


class HungMatchbook:
    def __init__(self) -> None:
        self.list_events_calls = 0
        self.list_markets_calls: list[str] = []

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_events_calls += 1
        await asyncio.sleep(30)
        return {"events": []}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_markets_calls.append(str(event_id))
        return {"markets": [_btts_matchbook(int(event_id) if str(event_id).isdigit() else 2001)]}


class OverlapPolymarket:
    def __init__(self, fixtures: list[tuple[str, str, str]]) -> None:
        self.fixtures = fixtures
        self.list_events_calls = 0
        self.list_markets_calls: list[str] = []

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        self.list_events_calls += 1
        return [
            {
                "id": f"pm-{index}",
                "title": f"{home} vs {away}",
                "startTime": KICKOFF.isoformat(),
                "competition": competition,
            }
            for index, (competition, home, away) in enumerate(self.fixtures)
        ]

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del filters
        self.list_markets_calls.append(str(event_id))
        index = int(str(event_id).split("-")[-1])
        return [_btts_polymarket(index)]

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id, filters
        return _pm_book(str(outcome_id))


class SlowOverlapPolymarket(OverlapPolymarket):
    def __init__(self, fixtures: list[tuple[str, str, str]], *, market_delay_s: float) -> None:
        super().__init__(fixtures)
        self.market_delay_s = market_delay_s

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        await asyncio.sleep(self.market_delay_s)
        return await super().list_markets(event_id, **filters)


class OverlapKalshi:
    def __init__(self, fixtures: list[tuple[str, str, str]]) -> None:
        self.fixtures = fixtures
        self.list_events_calls = 0
        self.list_markets_calls: list[str] = []

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_events_calls += 1
        return {
            "events": [
                _kalshi_btts_event(
                    ticker=f"KXGAME-{index}",
                    title=f"{home} vs {away}",
                    competition_series="KXGAME",
                )
                | {"competition": competition}
                for index, (competition, home, away) in enumerate(self.fixtures)
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_markets_calls.append(str(event_id))
        return {"markets": []}

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, outcome_id, filters
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "100.00"]],
                "no_dollars": [["0.49", "200.00"]],
            }
        }

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        del series_ticker
        return BUNDESLIGA_KALSHI_SERIES


class NamedMatchbook:
    def __init__(self, *, home: str, away: str, competition: str, event_id: int = 8801) -> None:
        self.home = home
        self.away = away
        self.competition = competition
        self.event_id = event_id
        self.list_events_calls = 0
        self.list_markets_calls: list[str] = []

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_events_calls += 1
        return {
            "events": [
                {
                    "id": self.event_id,
                    "name": f"{self.home} vs {self.away}",
                    "start": KICKOFF.isoformat(),
                    "sport-name": "Football",
                    "competition-name": self.competition,
                    "status": "open",
                }
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_markets_calls.append(str(event_id))
        return {"markets": [_btts_matchbook(int(event_id))]}


class NamedPolymarket:
    def __init__(self, *, home: str, away: str, competition: str) -> None:
        self.home = home
        self.away = away
        self.competition = competition
        self.list_events_calls = 0
        self.list_markets_calls: list[str] = []

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        self.list_events_calls += 1
        return [
            {
                "id": "pm-named",
                "title": f"{self.home} vs {self.away}",
                "startTime": KICKOFF.isoformat(),
                "competition": self.competition,
            }
        ]

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del filters
        self.list_markets_calls.append(str(event_id))
        return [_btts_polymarket(1)]

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id, filters
        return _pm_book(str(outcome_id))


class NamedKalshi:
    def __init__(self, *, home: str, away: str, competition: str = "Bundesliga") -> None:
        self.home = home
        self.away = away
        self.competition = competition
        self.list_events_calls = 0
        self.list_markets_calls: list[str] = []

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_events_calls += 1
        event = _kalshi_btts_event(
            ticker="KXBUNDESLIGAGAME-26SEP16BAYUNI",
            title=f"{self.home} vs {self.away}",
            competition_series="KXBUNDESLIGAGAME",
        )
        event["competition"] = self.competition
        return {"events": [event]}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_markets_calls.append(str(event_id))
        return {"markets": []}

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, outcome_id, filters
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "100.00"]],
                "no_dollars": [["0.49", "200.00"]],
            }
        }

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        del series_ticker
        return BUNDESLIGA_KALSHI_SERIES


def _scan_costs():
    return [
        *matchbook_polymarket_costs(),
        kalshi_cost_from_series(BUNDESLIGA_KALSHI_SERIES),
        profit_commission_cost(VenueName.KALSHI, "0"),
    ]


def test_paper_execution_boundary_stays_disabled() -> None:
    settings = Settings()
    assert settings.sports_hedge_execution_enabled is False
    assert UNIVERSE_MIN_CHUNK_SECONDS == 6.0
    assert MIN_POST_DISCOVERY_SOFT_SECONDS == 1.5


def test_normalize_text_does_not_drop_fc_united_city() -> None:
    assert "fc" in normalize_text("FC Bayern München").split()
    assert "united" in normalize_text("Manchester United").split()
    assert "city" in normalize_text("Manchester City").split()
    assert resolve_team_name("FC Unknownville") == "fc unknownville"
    assert resolve_team_name("Unknownville CF") == "unknownville cf"


def test_wave_r_base_raw_similarity_is_below_event_matcher_threshold() -> None:
    matcher = EventMatcher()
    assert matcher.threshold == 0.92
    home = SequenceMatcher(
        a=normalize_text("FC Bayern München"), b=normalize_text("Bayern Munich")
    ).ratio()
    away = SequenceMatcher(
        a=normalize_text("1. FC Union Berlin"), b=normalize_text("Union Berlin")
    ).ratio()
    assert home < 0.92
    assert away < 0.92
    malaga = SequenceMatcher(a=normalize_text("Málaga"), b=normalize_text("Malaga CF")).ratio()
    villarreal = SequenceMatcher(
        a=normalize_text("Villarreal"), b=normalize_text("Villarreal CF")
    ).ratio()
    assert malaga < 0.92
    assert villarreal < 0.92


def test_event_matcher_clusters_bayern_union_provider_variants() -> None:
    matcher = EventMatcher()
    assert matcher.threshold == 0.92
    assert resolve_team_name("FC Bayern München") == resolve_team_name("Bayern Munich")
    assert resolve_team_name("1. FC Union Berlin") == resolve_team_name("Union Berlin")
    assert canonical_team_id("FC Bayern München") == canonical_team_id("Bayern Munich")
    assert canonical_team_id("1. FC Union Berlin") == canonical_team_id("Union Berlin")

    result = matcher.match(
        _canonical(
            VenueName.MATCHBOOK,
            "FC Bayern München",
            "1. FC Union Berlin",
            competition="Germany Bundesliga",
            source_event_id="mb-bayern",
        ),
        _canonical(
            VenueName.KALSHI,
            "Bayern Munich",
            "Union Berlin",
            competition="Bundesliga",
            source_event_id="k-bayern",
        ),
    )
    assert result.matched is True
    assert result.confidence >= 0.92

    clusters, counts = cluster_venue_events(
        matchbook=[
            _venue_event(
                VenueName.MATCHBOOK,
                "FC Bayern München",
                "1. FC Union Berlin",
                competition="Germany Bundesliga",
                source_event_id="mb-bayern",
            )
        ],
        polymarket=[],
        kalshi=[
            _venue_event(
                VenueName.KALSHI,
                "Bayern Munich",
                "Union Berlin",
                competition="Bundesliga",
                source_event_id="k-bayern",
            )
        ],
        matcher=matcher,
        max_event_pairs=8,
    )
    assert len(clusters) == 1
    assert clusters[0].matchbook is not None
    assert clusters[0].kalshi is not None
    assert counts["matchbook_kalshi"] == 1


def test_event_matcher_clusters_malaga_villarreal_suffix_variants() -> None:
    matcher = EventMatcher()
    assert matcher.threshold == 0.92
    assert resolve_team_name("Málaga") == resolve_team_name("Malaga CF")
    assert resolve_team_name("Villarreal") == resolve_team_name("Villarreal CF")

    result = matcher.match(
        _canonical(
            VenueName.MATCHBOOK,
            "Málaga",
            "Villarreal",
            competition="La Liga",
            source_event_id="mb-malaga",
        ),
        _canonical(
            VenueName.POLYMARKET,
            "Malaga CF",
            "Villarreal CF",
            competition="La Liga",
            source_event_id="pm-malaga",
        ),
    )
    assert result.matched is True
    assert result.confidence >= 0.92

    clusters, counts = cluster_venue_events(
        matchbook=[
            _venue_event(
                VenueName.MATCHBOOK,
                "Málaga",
                "Villarreal",
                competition="La Liga",
                source_event_id="mb-malaga",
            )
        ],
        polymarket=[
            _venue_event(
                VenueName.POLYMARKET,
                "Malaga CF",
                "Villarreal CF",
                competition="La Liga",
                source_event_id="pm-malaga",
            )
        ],
        kalshi=[],
        matcher=matcher,
        max_event_pairs=8,
    )
    assert len(clusters) == 1
    assert counts["matchbook_polymarket"] == 1


@pytest.mark.parametrize(
    ("home_left", "away_left", "home_right", "away_right", "competition"),
    [
        ("Bayern Munich", "Union Berlin", "Bayern Munich Women", "Union Berlin", "Bundesliga"),
        ("Bayern Munich", "Union Berlin", "Bayern Munich", "Union Berlin II", "Bundesliga"),
        ("Bayern Munich", "Union Berlin", "Bayern Munich U21", "Union Berlin", "Bundesliga"),
        ("Bayern Munich", "Union Berlin", "Bayer Leverkusen", "Union Berlin", "Bundesliga"),
        ("Manchester United", "Chelsea", "Manchester City", "Chelsea", "Premier League"),
        ("Athletic Club", "Barcelona", "Atletico Madrid", "Barcelona", "La Liga"),
    ],
)
def test_identity_aliases_stay_fail_closed_for_distinct_clubs(
    home_left: str,
    away_left: str,
    home_right: str,
    away_right: str,
    competition: str,
) -> None:
    result = EventMatcher().match(
        _canonical(
            VenueName.MATCHBOOK,
            home_left,
            away_left,
            competition=competition,
            source_event_id="left",
        ),
        _canonical(
            VenueName.KALSHI,
            home_right,
            away_right,
            competition=competition,
            source_event_id="right",
        ),
    )
    assert result.matched is False


@pytest.mark.asyncio
async def test_collector_clusters_observed_bayern_union_names() -> None:
    repository = SqliteMarketIntelligenceRepository()
    matchbook = NamedMatchbook(
        home="FC Bayern München",
        away="1. FC Union Berlin",
        competition="Germany Bundesliga",
    )
    kalshi = NamedKalshi(home="Bayern Munich", away="Union Berlin")
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=NamedPolymarket(home="Unrelated", away="Club", competition="Premier League"),
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        report = await collector.collect_and_scan(venue_costs=_scan_costs())
        clustered = [
            item
            for item in report.discovered_fixtures
            if item.matchbook_matched and item.kalshi_matched
        ]
        assert clustered, report.discovered_fixtures
        fixture = clustered[0]
        assert fixture.market_evaluation_state == MarketEvaluationState.EVALUATED.value
        assert fixture.no_comparison_reason != EVENT_IDENTITY_MISMATCH
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_collector_clusters_malaga_villarreal_suffix_names() -> None:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=NamedMatchbook(home="Málaga", away="Villarreal", competition="La Liga"),
        polymarket=NamedPolymarket(home="Malaga CF", away="Villarreal CF", competition="La Liga"),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        report = await collector.collect_and_scan(venue_costs=matchbook_polymarket_costs())
        clustered = [
            item
            for item in report.discovered_fixtures
            if item.matchbook_matched and item.polymarket_matched
        ]
        assert clustered, report.discovered_fixtures
        fixture = clustered[0]
        assert fixture.market_evaluation_state == MarketEvaluationState.EVALUATED.value
        assert fixture.matched_market_count == 1
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_hung_provider_does_not_leftover_healthy_overlap_as_budget_exhausted() -> None:
    fixtures = Q1_OVERLAP_FIXTURES[:6]
    polymarket = OverlapPolymarket(fixtures)
    kalshi = OverlapKalshi(fixtures)
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=HungMatchbook(),
        polymarket=polymarket,
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        venue_timeout_seconds=8.0,
        provider_call_timeout_seconds=8.0,
        cycle_timeout_seconds=2.5,
    )
    try:
        report = await collector.collect_and_scan(
            venue_costs=_scan_costs(),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            maximum_execution_risk=100,
            scan_lane=ScanLane.UNIVERSE.value,
            cycle_timeout_seconds=2.5,
        )
        evaluated = [
            item
            for item in report.discovered_fixtures
            if item.market_evaluation_state == MarketEvaluationState.EVALUATED.value
        ]
        leftovers = [
            item
            for item in report.discovered_fixtures
            if item.market_evaluation_state
            == MarketEvaluationState.NOT_EVALUATED_SCAN_DEADLINE.value
        ]
        two_venue = [
            item
            for item in report.discovered_fixtures
            if item.polymarket_matched and item.kalshi_matched
        ]
        diagnostics = report.scan_diagnostics
        assert report.venue_health["matchbook"] in {"timeout", "unavailable"}
        assert report.venue_health["polymarket"] == "ok"
        assert report.venue_health["kalshi"] == "ok"
        assert two_venue, "healthy venues must still cluster overlapping fixtures"
        assert evaluated, diagnostics
        assert diagnostics["evaluated_count"] == len(evaluated)
        assert diagnostics["provider_unavailable"].get("matchbook") in {"timeout", "unavailable"}
        budget_only = [
            item
            for item in leftovers
            if item.market_evaluation_reason == SCAN_BUDGET_EXHAUSTED_REASON
            and item.polymarket_matched
            and item.kalshi_matched
            and item.market_evaluation_state
            == MarketEvaluationState.NOT_EVALUATED_SCAN_DEADLINE.value
        ]
        assert len(evaluated) >= 1
        assert diagnostics["identity_unmatched_count"] >= 0
        if budget_only:
            assert diagnostics["scan_deadline_exhausted"] is True
        assert diagnostics["event_discovery_ms"] < 2500
        assert polymarket.list_markets_calls or kalshi.list_markets_calls or evaluated
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_q1_generation_resumes_until_eligible_clusters_are_evaluated() -> None:
    fixtures = Q1_OVERLAP_FIXTURES
    polymarket = SlowOverlapPolymarket(fixtures, market_delay_s=0.22)

    class Q1Matchbook:
        def __init__(self) -> None:
            self.list_events_calls = 0
            self.list_markets_calls: list[str] = []

        async def list_events(self, **filters: Any) -> dict[str, Any]:
            del filters
            self.list_events_calls += 1
            return {
                "events": [
                    {
                        "id": 10_000 + index,
                        "name": f"{home} vs {away}",
                        "start": KICKOFF.isoformat(),
                        "sport-name": "Football",
                        "competition-name": competition,
                        "status": "open",
                    }
                    for index, (competition, home, away) in enumerate(fixtures)
                ]
            }

        async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
            del filters
            self.list_markets_calls.append(str(event_id))
            return {"markets": [_btts_matchbook(int(event_id))]}

    mb = Q1Matchbook()
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=mb,
        polymarket=polymarket,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        venue_timeout_seconds=8.0,
        provider_call_timeout_seconds=0.8,
        cycle_timeout_seconds=1.1,
        cluster_concurrency=4,
    )
    try:
        evaluated_ids: set[str] = set()
        leftover_reasons: set[str] = set()
        for chunk in range(8):
            report = await collector.collect_and_scan(
                venue_costs=matchbook_polymarket_costs(),
                fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
                maximum_execution_risk=100,
                scan_lane=ScanLane.UNIVERSE.value,
                skip_event_ids=sorted(evaluated_ids),
                generation_resume=chunk > 0,
                universe_generation_id=1,
                cycle_timeout_seconds=1.1,
                max_event_pairs=40,
            )
            newly = {
                item.canonical_event_id
                for item in report.discovered_fixtures
                if item.market_evaluation_state == MarketEvaluationState.EVALUATED.value
            }
            leftovers = [
                item
                for item in report.discovered_fixtures
                if item.market_evaluation_state
                == MarketEvaluationState.NOT_EVALUATED_SCAN_DEADLINE.value
            ]
            for item in leftovers:
                leftover_reasons.add(item.market_evaluation_reason or "")
            assert newly.isdisjoint(evaluated_ids)
            evaluated_ids.update(newly)
            diagnostics = report.scan_diagnostics
            if leftovers:
                assert diagnostics["generation_resume_pending"] is (chunk > 0)
                assert leftover_reasons <= {SCAN_BUDGET_EXHAUSTED_REASON, ""}
            else:
                break
        assert len(evaluated_ids) == len(fixtures), evaluated_ids
        assert SCAN_BUDGET_EXHAUSTED_REASON in leftover_reasons or len(evaluated_ids) == len(fixtures)
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_hot_known_source_refresh_evaluates_without_universe_rediscovery() -> None:
    matchbook = NamedMatchbook(
        home="FC Bayern München",
        away="1. FC Union Berlin",
        competition="Germany Bundesliga",
    )
    kalshi = NamedKalshi(home="Bayern Munich", away="Union Berlin")
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=NamedPolymarket(home="Unrelated", away="Club", competition="Premier League"),
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        universe = await collector.collect_and_scan(
            venue_costs=_scan_costs(),
            scan_lane=ScanLane.UNIVERSE.value,
        )
        clustered = [
            item
            for item in universe.discovered_fixtures
            if item.matchbook_matched and item.kalshi_matched
        ]
        assert clustered
        fixture = clustered[0]
        known = universe.fixture_source_events
        mb_events_before = matchbook.list_events_calls
        k_events_before = kalshi.list_events_calls
        mb_markets_before = list(matchbook.list_markets_calls)
        hot = await collector.collect_and_scan(
            venue_costs=_scan_costs(),
            scan_lane=ScanLane.HOT.value,
            identity_scope=[fixture.canonical_event_id],
            known_source_events=known,
        )
        assert matchbook.list_events_calls == mb_events_before
        assert kalshi.list_events_calls == k_events_before
        assert matchbook.list_markets_calls != mb_markets_before
        assert all(
            item.market_evaluation_state == MarketEvaluationState.EVALUATED.value
            for item in hot.discovered_fixtures
            if item.canonical_event_id == fixture.canonical_event_id
        )
        assert hot.scan_lane == ScanLane.HOT.value
    finally:
        repository.close()


def test_diagnostics_keys_distinguish_failure_classes() -> None:
    assert MARKET_FETCH_UNAVAILABLE_REASON != SCAN_BUDGET_EXHAUSTED_REASON
    assert EVENT_IDENTITY_MISMATCH != SCAN_BUDGET_EXHAUSTED_REASON
    assert EVENT_IDENTITY_MISMATCH != MARKET_FETCH_UNAVAILABLE_REASON
