"""Issue #277: duplicate canonical fixtures from provider club-name variants.

Owner-live observation on 2026-09-18 (PAPER/read-only, Polymarket off):

- ``AC Monza v Sassuolo Calcio`` (Kalshi-only row) and ``Monza v Sassuolo``
  (Matchbook + Kalshi) are the same Serie A fixture.
- ``Espanyol Barcelona v Elche CF`` (Kalshi-only) and ``Espanyol v Elche CF``
  (Matchbook + Kalshi) are the same La Liga fixture.

Fix is curated aliases / safe deterministic identity. The EventMatcher
threshold stays 0.92. This does not strip AC, Calcio, or Barcelona globally.

Data class: deterministic fixture/demo providers. Not live, historical, or
modelled venue quotes. Paper-only; execution stays disabled.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

import pytest

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.fixture_clusters import (
    VenueEvent,
    cluster_canonical_event_id,
    cluster_identity_aliases,
    cluster_venue_events,
)
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.hot_identity import hot_scheduling_key
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
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
from test_dual_cadence_scheduler import NOW, _report
from venue_cost_helpers import matchbook_polymarket_costs, profit_commission_cost


KICKOFF = datetime(2026, 9, 18, 18, 30, tzinfo=UTC)
REGULATION = "Resolves based on 90 minutes of regulation time."
SERIE_A_KALSHI_SERIES = {
    "ticker": "KXSERIEAGAME",
    "title": "Serie A",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "settlement_sources": [{"name": "Opta"}],
}
LA_LIGA_KALSHI_SERIES = {
    "ticker": "KXLALIGAGAME",
    "title": "La Liga",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "settlement_sources": [{"name": "Opta"}],
}


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


def _kalshi_event(
    *,
    ticker: str,
    title: str,
    competition: str,
    series_ticker: str,
    kickoff: datetime = KICKOFF,
) -> dict[str, Any]:
    return {
        "event_ticker": ticker,
        "series_ticker": series_ticker,
        "title": title,
        "category": "Sports",
        "strike_date": kickoff.isoformat(),
        "competition": competition,
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


class EmptyPolymarket:
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return []

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return []

    async def get_order_book(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        raise AssertionError("Polymarket stays off; order books must not be fetched")


class NamedMatchbook:
    def __init__(
        self,
        *,
        home: str,
        away: str,
        competition: str,
        event_id: int = 27701,
        event_name: str | None = None,
    ) -> None:
        self.home = home
        self.away = away
        self.competition = competition
        self.event_id = event_id
        self.event_name = event_name

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {
            "events": [
                {
                    "id": self.event_id,
                    "name": self.event_name or f"{self.home} vs {self.away}",
                    "start": KICKOFF.isoformat(),
                    "sport-name": "Football",
                    "competition-name": self.competition,
                    "status": "open",
                }
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id
        return {"markets": [_btts_matchbook(self.event_id)]}


class SiblingKalshi:
    """One fixture published as multiple Kalshi market-family events with name variants."""

    def __init__(
        self,
        events: list[dict[str, Any]],
        *,
        competition: str,
        series: dict[str, Any],
    ) -> None:
        self.events = events
        self.competition = competition
        self.series = series

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {"events": list(self.events)}

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
        del event_id, outcome_id, filters
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "100.00"]],
                "no_dollars": [["0.49", "200.00"]],
            }
        }

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        payload = dict(self.series)
        payload["ticker"] = series_ticker or payload["ticker"]
        return payload


def _scan_costs(series: dict[str, Any]) -> list[Any]:
    return [
        *matchbook_polymarket_costs(),
        kalshi_cost_from_series(series),
        profit_commission_cost(VenueName.KALSHI, "0"),
    ]


def _raw_ratio(left: str, right: str) -> float:
    return SequenceMatcher(a=normalize_text(left), b=normalize_text(right)).ratio()


def test_paper_execution_boundary_and_matcher_threshold_stay_unchanged() -> None:
    settings = Settings()
    assert settings.sports_hedge_execution_enabled is False
    assert settings.sports_hedge_mode == "paper"
    matcher = EventMatcher()
    assert matcher.threshold == 0.92
    assert matcher.kickoff_tolerance.total_seconds() == 300


def test_observed_raw_similarity_is_below_event_matcher_threshold() -> None:
    assert _raw_ratio("AC Monza", "Monza") < 0.92
    assert _raw_ratio("Sassuolo Calcio", "Sassuolo") < 0.92
    assert _raw_ratio("Espanyol Barcelona", "Espanyol") < 0.92
    assert _raw_ratio("RCD Espanyol Barcelona", "Espanyol") < 0.92
    assert EventMatcher().threshold == 0.92


def test_curated_aliases_converge_observed_club_name_variants() -> None:
    assert resolve_team_name("AC Monza") == resolve_team_name("Monza") == "monza"
    assert resolve_team_name("Sassuolo Calcio") == resolve_team_name("Sassuolo") == "sassuolo"
    assert resolve_team_name("US Sassuolo") == "sassuolo"
    assert resolve_team_name("Espanyol Barcelona") == resolve_team_name("Espanyol") == "espanyol"
    assert resolve_team_name("RCD Espanyol Barcelona") == "espanyol"
    assert resolve_team_name("RCD Espanyol de Barcelona") == "espanyol"
    assert canonical_team_id("AC Monza") == canonical_team_id("Monza")
    assert canonical_team_id("Sassuolo Calcio") == canonical_team_id("Sassuolo")
    assert canonical_team_id("Espanyol Barcelona") == canonical_team_id("Espanyol")
    assert canonical_team_id("RCD Espanyol Barcelona") == canonical_team_id("Espanyol")


def test_aliases_do_not_conflate_distinct_senior_clubs() -> None:
    assert resolve_team_name("AC Monza") != resolve_team_name("AC Milan")
    assert resolve_team_name("Monza") != resolve_team_name("Milan")
    assert resolve_team_name("Espanyol Barcelona") != resolve_team_name("FC Barcelona")
    assert resolve_team_name("Espanyol Barcelona") != resolve_team_name("Barcelona")
    assert canonical_team_id("AC Monza") != canonical_team_id("AC Milan")
    assert canonical_team_id("Espanyol Barcelona") != canonical_team_id("FC Barcelona")
    assert resolve_team_name("AC Unknownville") == "ac unknownville"
    assert resolve_team_name("Unknownville Calcio") == "unknownville calcio"
    assert resolve_team_name("Unknownville Barcelona") == "unknownville barcelona"


def test_event_matcher_clusters_monza_sassuolo_provider_variants() -> None:
    matcher = EventMatcher()
    result = matcher.match(
        _canonical(
            VenueName.MATCHBOOK,
            "Monza",
            "Sassuolo",
            competition="Serie A",
            source_event_id="mb-monza",
        ),
        _canonical(
            VenueName.KALSHI,
            "AC Monza",
            "Sassuolo Calcio",
            competition="Italian Serie A",
            source_event_id="k-monza",
        ),
    )
    assert result.matched is True
    assert result.confidence >= 0.92
    assert "home_team_fuzzy" not in result.reasons
    assert "away_team_fuzzy" not in result.reasons

    clusters, counts = cluster_venue_events(
        matchbook=[
            _venue_event(
                VenueName.MATCHBOOK,
                "Monza",
                "Sassuolo",
                competition="Serie A",
                source_event_id="mb-monza",
            )
        ],
        polymarket=[],
        kalshi=[
            _venue_event(
                VenueName.KALSHI,
                "AC Monza",
                "Sassuolo Calcio",
                competition="Serie A",
                source_event_id="k-monza-game",
            ),
            _venue_event(
                VenueName.KALSHI,
                "AC Monza",
                "Sassuolo Calcio",
                competition="Serie A",
                source_event_id="k-monza-btts",
            ),
        ],
        matcher=matcher,
        max_event_pairs=8,
    )
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.matchbook is not None
    assert len(cluster.kalshi_events) == 2
    assert counts["matchbook_kalshi"] == 1
    aliases = cluster_identity_aliases(cluster)
    canonical_id = cluster_canonical_event_id(cluster)
    assert aliases["mb-monza"] == canonical_id
    assert aliases["k-monza-game"] == canonical_id
    assert aliases["k-monza-btts"] == canonical_id


def test_event_matcher_clusters_espanyol_elche_provider_variants() -> None:
    matcher = EventMatcher()
    result = matcher.match(
        _canonical(
            VenueName.MATCHBOOK,
            "Espanyol",
            "Elche CF",
            competition="La Liga",
            source_event_id="mb-espanyol",
        ),
        _canonical(
            VenueName.KALSHI,
            "Espanyol Barcelona",
            "Elche CF",
            competition="La Liga",
            source_event_id="k-espanyol",
        ),
    )
    assert result.matched is True
    assert result.confidence >= 0.92
    assert "home_team_fuzzy" not in result.reasons

    rcd = matcher.match(
        _canonical(
            VenueName.MATCHBOOK,
            "Espanyol",
            "Elche CF",
            competition="La Liga",
            source_event_id="mb-espanyol",
        ),
        _canonical(
            VenueName.KALSHI,
            "RCD Espanyol Barcelona",
            "Elche CF",
            competition="La Liga",
            source_event_id="k-rcd",
        ),
    )
    assert rcd.matched is True

    clusters, counts = cluster_venue_events(
        matchbook=[
            _venue_event(
                VenueName.MATCHBOOK,
                "Espanyol",
                "Elche CF",
                competition="La Liga",
                source_event_id="mb-espanyol",
            )
        ],
        polymarket=[],
        kalshi=[
            _venue_event(
                VenueName.KALSHI,
                "Espanyol Barcelona",
                "Elche CF",
                competition="La Liga",
                source_event_id="k-espanyol-game",
            ),
            _venue_event(
                VenueName.KALSHI,
                "RCD Espanyol Barcelona",
                "Elche CF",
                competition="La Liga",
                source_event_id="k-espanyol-btts",
            ),
        ],
        matcher=matcher,
        max_event_pairs=8,
    )
    assert len(clusters) == 1
    assert counts["matchbook_kalshi"] == 1
    assert {item.source_event_id for item in clusters[0].kalshi_events} == {
        "k-espanyol-game",
        "k-espanyol-btts",
    }


@pytest.mark.parametrize(
    ("home_left", "away_left", "home_right", "away_right", "competition"),
    [
        ("AC Monza", "Sassuolo", "AC Milan", "Sassuolo", "Serie A"),
        ("Monza", "Sassuolo", "Milan", "Sassuolo", "Serie A"),
        ("Espanyol Barcelona", "Elche CF", "FC Barcelona", "Elche CF", "La Liga"),
        ("Espanyol", "Elche CF", "Barcelona", "Elche CF", "La Liga"),
        ("AC Monza", "Sassuolo", "AC Monza Women", "Sassuolo", "Serie A"),
        ("AC Monza", "Sassuolo", "AC Monza", "Sassuolo U21", "Serie A"),
        ("Bayern Munich", "Union Berlin", "Bayern Munich", "Union Berlin II", "Bundesliga"),
    ],
)
def test_identity_aliases_stay_fail_closed_for_distinct_or_non_senior_sides(
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


def test_existing_bayern_union_and_betis_getafe_aliases_remain() -> None:
    matcher = EventMatcher()
    bayern = matcher.match(
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
    assert bayern.matched is True
    betis = matcher.match(
        _canonical(
            VenueName.MATCHBOOK,
            "Real Betis",
            "Getafe",
            competition="La Liga",
            source_event_id="mb-betis",
        ),
        _canonical(
            VenueName.KALSHI,
            "Betis",
            "Getafe CF",
            competition="La Liga",
            source_event_id="k-betis",
        ),
    )
    assert betis.matched is True


async def _collect_sibling_fixture(
    *,
    matchbook_home: str,
    matchbook_away: str,
    competition: str,
    kalshi_titles: list[tuple[str, str]],
    series: dict[str, Any],
) -> Any:
    kalshi_events = [
        _kalshi_event(
            ticker=ticker,
            title=title,
            competition=competition,
            series_ticker=str(series["ticker"]),
        )
        for ticker, title in kalshi_titles
    ]
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=NamedMatchbook(
            home=matchbook_home, away=matchbook_away, competition=competition
        ),
        polymarket=EmptyPolymarket(),
        kalshi=SiblingKalshi(kalshi_events, competition=competition, series=series),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        return await collector.collect_and_scan(
            venue_costs=_scan_costs(series),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
            capital_limit_gbp=Decimal("100"),
            maximum_execution_risk=100,
        )
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_collector_unions_monza_kalshi_siblings_with_matchbook() -> None:
    report = await _collect_sibling_fixture(
        matchbook_home="Monza",
        matchbook_away="Sassuolo",
        competition="Serie A",
        kalshi_titles=[
            ("KXSERIEAGAME-MONSAS", "Monza vs Sassuolo"),
            ("KXSERIEABTTS-MONSAS", "AC Monza vs Sassuolo Calcio"),
            ("KXSERIEATOTAL-MONSAS", "AC Monza v Sassuolo Calcio"),
        ],
        series=SERIE_A_KALSHI_SERIES,
    )
    clustered = [
        item for item in report.discovered_fixtures if item.matchbook_matched and item.kalshi_matched
    ]
    assert len(clustered) == 1
    fixture = clustered[0]
    assert fixture.polymarket_matched is False
    kalshi_ids = {
        str(row["source_event_id"])
        for row in report.fixture_source_events.get(fixture.canonical_event_id, [])
        if str(row.get("venue")) == VenueName.KALSHI.value
    }
    assert kalshi_ids == {
        "KXSERIEAGAME-MONSAS",
        "KXSERIEABTTS-MONSAS",
        "KXSERIEATOTAL-MONSAS",
    }
    aliases = report.fixture_identity_aliases
    canonical_id = fixture.canonical_event_id
    assert aliases["27701"] == canonical_id
    for source_id in kalshi_ids:
        assert aliases[source_id] == canonical_id


@pytest.mark.asyncio
async def test_collector_unions_espanyol_kalshi_siblings_with_matchbook() -> None:
    report = await _collect_sibling_fixture(
        matchbook_home="Espanyol",
        matchbook_away="Elche CF",
        competition="La Liga",
        kalshi_titles=[
            ("KXLALIGAGAME-ESPELCHE", "Espanyol vs Elche CF"),
            ("KXLALIGABTTS-ESPELCHE", "Espanyol Barcelona vs Elche CF"),
            ("KXLALIGATOTAL-ESPELCHE", "RCD Espanyol Barcelona v Elche CF"),
        ],
        series=LA_LIGA_KALSHI_SERIES,
    )
    clustered = [
        item for item in report.discovered_fixtures if item.matchbook_matched and item.kalshi_matched
    ]
    assert len(clustered) == 1
    fixture = clustered[0]
    aliases = report.fixture_identity_aliases
    canonical_id = fixture.canonical_event_id
    assert aliases["27701"] == canonical_id
    assert aliases["KXLALIGAGAME-ESPELCHE"] == canonical_id
    assert aliases["KXLALIGABTTS-ESPELCHE"] == canonical_id
    assert aliases["KXLALIGATOTAL-ESPELCHE"] == canonical_id


@pytest.mark.asyncio
async def test_hot_universe_identity_aliases_stay_stable_across_refreshes() -> None:
    report = await _collect_sibling_fixture(
        matchbook_home="Monza",
        matchbook_away="Sassuolo",
        competition="Serie A",
        kalshi_titles=[
            ("KXSERIEAGAME-MONSAS", "Monza vs Sassuolo"),
            ("KXSERIEABTTS-MONSAS", "AC Monza vs Sassuolo Calcio"),
        ],
        series=SERIE_A_KALSHI_SERIES,
    )
    fixture = next(item for item in report.discovered_fixtures if item.matchbook_matched)
    fixture = fixture.model_copy(update={"kickoff_utc": NOW.replace(hour=18, minute=30)})
    first = report.model_copy(
        update={
            "discovered_fixtures": [fixture],
            "scan_lane": ScanLane.UNIVERSE.value,
            "fixture_identity_aliases": dict(report.fixture_identity_aliases),
        }
    )
    store = FixtureCurrentStateStore()
    store.upsert_from_report(first, scan_lane=ScanLane.UNIVERSE, now=NOW)
    assert len(store._rows) == 1
    universe_id = next(iter(store._rows))
    later = _report([fixture], when=NOW, scan_lane=ScanLane.HOT.value)
    later = later.model_copy(
        update={
            "fixture_identity_aliases": dict(report.fixture_identity_aliases),
            "fixture_source_events": dict(report.fixture_source_events),
        }
    )
    store.upsert_from_report(later, scan_lane=ScanLane.HOT, now=NOW)
    assert len(store._rows) == 1
    assert next(iter(store._rows)) == universe_id
    assert store.resolve_canonical_id("27701") == universe_id
    assert store.resolve_canonical_id("KXSERIEAGAME-MONSAS") == universe_id
    assert store.resolve_canonical_id("KXSERIEABTTS-MONSAS") == universe_id
    assert hot_scheduling_key(fixture) is not None
    assert hot_scheduling_key(fixture) == hot_scheduling_key(
        fixture.model_copy(update={"home_team": "AC Monza", "away_team": "Sassuolo Calcio"})
    )
