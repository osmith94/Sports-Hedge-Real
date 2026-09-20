"""Issue #413: MLS Inter Miami CF v San Diego FC fixture identity.

Live read-only evidence captured 2026-09-20 against owner-live
``588c8b41802690c226ce338e871ba17fcd367eab`` (PAPER / no venue writes):

Matchbook GET /edge/rest/events (sport-ids=15, states=open,suspended):
- id ``34333851245100081``
- name ``Inter Miami CF vs San Diego FC``
- start ``2026-09-20T23:00:00.000Z``
- meta-tag type=COMPETITION ``US Major League Soccer``
- listed Match Odds, Both Teams To Score, Total 1.5–8.5
- returned inside bounded pagination (103 soccer events, 3 pages; not truncated)

Kalshi public Trade API v2 sibling events, same suffix ``26SEP20MIASD``:
- ``KXMLSGAME-26SEP20MIASD`` title ``Miami vs San Diego FC``
- ``KXMLSBTTS-26SEP20MIASD`` title ``Miami vs San Diego FC: BTTS``
- ``KXMLSTOTAL-26SEP20MIASD`` title ``Miami vs San Diego FC: Total Goals``
- ``KXMLSFTTS-26SEP20MIASD`` title ``Inter Miami CF vs San Diego FC: First Team to Score``
- milestone start_date ``2026-09-20T23:00:00Z``

Polymarket Gamma series 10189:
- id ``983347`` title ``Inter Miami CF vs. San Diego FC`` start ``2026-09-20T23:00:00Z``

Root causes (not a pagination/concurrency change):
1. MLS was a verified target competition with no senior-club registry, so
   ``Miami`` vs ``Inter Miami CF`` stayed at raw/weighted fuzzy ~0.834.
2. Matchbook list_events returned the event, then collector competition-scope
   dropped it because ``US Major League Soccer`` was not an MLS alias.
3. Generic aliases were globally flattened, so ``Miami`` resolved to Inter Miami
   without competition context.

Fix is curated MLS identity plus the observed Matchbook competition alias, with
competition-aware generic aliases. Structural aliases remain the preferred exact
identity fix. The generic/default EventMatcher stays 0.92. PAPER injects a
runtime-configurable event-match threshold of 0.80 for this owner-approved
experiment and persists the actual confidence. Deprecated
``minimum_mapping_confidence`` stays unused. Pagination and provider concurrency
stay bounded/sequential. No capture/economics/settlement change.

Data class: deterministic fixture/demo providers. Not live venue quotes.
Paper-only; execution stays disabled.
"""

from __future__ import annotations

import inspect
from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from difflib import SequenceMatcher
from typing import Any

import pytest

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.fixture_clusters import (
    cluster_canonical_event_id,
    cluster_identity_aliases,
    cluster_venue_events,
)
from sports_hedge.application.fixture_inventory import InventoryComparisonStatus
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.provider_access import DEFAULT_PROVIDER_CONCURRENCY
from sports_hedge.application.target_competitions import (
    filter_in_scope_events,
    resolve_target_competition,
    scope_matchbook_event,
)
from sports_hedge.catalogue.corpus import GAMEWIN_TEMPLATE, REGULATION
from sports_hedge.config import Settings
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.facts.aliases import resolve_team_name, resolve_team_name_for_competition
from sports_hedge.facts.identity import canonical_team_id
from sports_hedge.facts.team_registry import MLS, MLS_CLUBS, clubs_for
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.events import EventMatcher, paper_event_matcher
from sports_hedge.normalization.text import normalize_text
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.application.capture_replay import DEFAULT_KALSHI_BOOK
from test_issue293_owner_live_overlap import OverlapKalshi, OverlapMatchbook
from test_issue316_catalogue_registry import _DisabledPolymarket
from venue_cost_helpers import matchbook_polymarket_costs

KICKOFF = datetime(2026, 9, 20, 23, 0, tzinfo=UTC)
FTTS_KICKOFF = KICKOFF + timedelta(minutes=3)
MB_EVENT_ID = "34333851245100081"
GAME = "KXMLSGAME-26SEP20MIASD"
BTTS = "KXMLSBTTS-26SEP20MIASD"
TOTAL = "KXMLSTOTAL-26SEP20MIASD"
FTTS = "KXMLSFTTS-26SEP20MIASD"
MLS_SERIES = {
    "KXMLSGAME": {
        "ticker": "KXMLSGAME",
        "title": "MLS",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
        "settlement_sources": [{"name": "Opta"}],
    },
    "KXMLSBTTS": {
        "ticker": "KXMLSBTTS",
        "title": "MLS",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
        "settlement_sources": [{"name": "Opta"}],
    },
    "KXMLSTOTAL": {
        "ticker": "KXMLSTOTAL",
        "title": "MLS",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
        "settlement_sources": [{"name": "Opta"}],
    },
    "KXMLSFTTS": {
        "ticker": "KXMLSFTTS",
        "title": "MLS",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
        "settlement_sources": [{"name": "Opta"}],
    },
}

LIVE_MATCHBOOK_EVENT = {
    "id": int(MB_EVENT_ID),
    "name": "Inter Miami CF vs San Diego FC",
    "start": "2026-09-20T23:00:00.000Z",
    "status": "open",
    "sport-id": 15,
    "meta-tags": [
        {"id": 15, "name": "Soccer", "type": "SPORT", "url-name": "soccer"},
        {
            "id": 1190793990860023,
            "name": "US Major League Soccer",
            "type": "COMPETITION",
            "url-name": "us-major-league-soccer",
        },
    ],
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
    kickoff: datetime = KICKOFF,
) -> Any:
    from sports_hedge.application.fixture_clusters import VenueEvent

    return VenueEvent(
        venue=venue,
        raw={"id": source_event_id, "title": f"{home} vs {away}"},
        canonical=_canonical(
            venue,
            home,
            away,
            competition=competition,
            source_event_id=source_event_id,
            kickoff=kickoff,
        ),
        source_event_id=source_event_id,
    )


def _raw_ratio(left: str, right: str) -> float:
    return SequenceMatcher(a=normalize_text(left), b=normalize_text(right)).ratio()


def _weighted_exact_away_competition(home_left: str, home_right: str) -> float:
    home = _raw_ratio(home_left, home_right)
    return 0.35 * home + 0.35 * 1.0 + 0.10 * 1.0 + 0.20 * 1.0


def _runner(runner_id: int, name: str, odds: str = "2.10") -> dict[str, Any]:
    return {
        "id": runner_id,
        "name": name,
        "prices": [{"side": "back", "odds": odds, "available-amount": "80"}],
    }


def _mb_markets() -> list[dict[str, Any]]:
    return [
        {
            "id": 34333851722300081,
            "name": "Match Odds",
            "runners": [
                _runner(1, "Inter Miami CF", "2.40"),
                _runner(2, "Draw", "3.40"),
                _runner(3, "San Diego FC", "2.90"),
            ],
        },
        {
            "id": 34333851942200081,
            "name": "Both Teams To Score",
            "runners": [_runner(11, "Yes", "1.90"), _runner(12, "No", "1.95")],
        },
        {
            "id": 34333852626200081,
            "name": "Total",
            "runners": [_runner(21, "OVER 2.5", "1.85"), _runner(22, "UNDER 2.5", "2.05")],
        },
    ]


def _kalshi_event(
    ticker: str,
    series: str,
    title: str,
    markets: list[dict[str, Any]],
    *,
    kickoff: datetime = KICKOFF,
) -> dict[str, Any]:
    return {
        "event_ticker": ticker,
        "series_ticker": series,
        "title": title,
        "category": "Sports",
        "strike_date": kickoff.isoformat(),
        "milestone": {"start_date": kickoff.isoformat()},
        "product_metadata": {"competition": "MLS", "competition_scope": "Game"},
        "markets": markets,
    }


def _kalshi_siblings() -> list[dict[str, Any]]:
    return [
        _kalshi_event(
            GAME,
            "KXMLSGAME",
            "Miami vs San Diego FC",
            [
                {
                    "ticker": f"{GAME}-MIA",
                    "event_ticker": GAME,
                    "title": "Miami wins",
                    "yes_sub_title": "Miami",
                    "rules_primary": GAMEWIN_TEMPLATE,
                },
                {
                    "ticker": f"{GAME}-TIE",
                    "event_ticker": GAME,
                    "title": "Tie is the result",
                    "yes_sub_title": "Draw",
                    "rules_primary": GAMEWIN_TEMPLATE,
                },
                {
                    "ticker": f"{GAME}-SD",
                    "event_ticker": GAME,
                    "title": "San Diego FC wins",
                    "yes_sub_title": "San Diego FC",
                    "rules_primary": GAMEWIN_TEMPLATE,
                },
            ],
        ),
        _kalshi_event(
            BTTS,
            "KXMLSBTTS",
            "Miami vs San Diego FC: BTTS",
            [
                {
                    "ticker": f"{BTTS}-BTTS",
                    "event_ticker": BTTS,
                    "title": "Both Teams To Score",
                    "yes_sub_title": "Yes",
                    "rules_primary": REGULATION,
                }
            ],
        ),
        _kalshi_event(
            TOTAL,
            "KXMLSTOTAL",
            "Miami vs San Diego FC: Total Goals",
            [
                {
                    "ticker": f"{TOTAL}-3",
                    "event_ticker": TOTAL,
                    "title": "Will over 2.5 goals be scored?",
                    "yes_sub_title": "Over 2.5",
                    "rules_primary": REGULATION,
                    "strike": "2.5",
                }
            ],
        ),
        _kalshi_event(
            FTTS,
            "KXMLSFTTS",
            "Inter Miami CF vs San Diego FC: First Team to Score",
            [
                {
                    "ticker": f"{FTTS}-MIA",
                    "event_ticker": FTTS,
                    "title": "First team to score",
                    "yes_sub_title": "Inter Miami CF",
                    "rules_primary": REGULATION,
                },
                {
                    "ticker": f"{FTTS}-SD",
                    "event_ticker": FTTS,
                    "title": "First team to score",
                    "yes_sub_title": "San Diego FC",
                    "rules_primary": REGULATION,
                },
                {
                    "ticker": f"{FTTS}-NONE",
                    "event_ticker": FTTS,
                    "title": "First team to score",
                    "yes_sub_title": "No Goal",
                    "rules_primary": REGULATION,
                },
            ],
            kickoff=FTTS_KICKOFF,
        ),
    ]


def _books() -> dict[str, dict[str, Any]]:
    tickers = [
        f"{GAME}-MIA",
        f"{GAME}-TIE",
        f"{GAME}-SD",
        f"{BTTS}-BTTS",
        f"{TOTAL}-3",
        f"{FTTS}-MIA",
        f"{FTTS}-SD",
        f"{FTTS}-NONE",
    ]
    return {ticker: deepcopy(DEFAULT_KALSHI_BOOK) for ticker in tickers}


def _costs() -> list[Any]:
    captured = KICKOFF
    return [
        *matchbook_polymarket_costs(captured_at=captured),
        *[
            kalshi_cost_from_series(series, captured_at=captured)
            for series in MLS_SERIES.values()
        ],
    ]


def _fx() -> list[FxRateSnapshot]:
    return [
        FxRateSnapshot(
            currency="USD", gbp_per_unit=Decimal("0.75"), source="test_fx", captured_at=KICKOFF
        ),
        FxRateSnapshot(
            currency="GBP",
            gbp_per_unit=Decimal("1"),
            source="functional_currency",
            captured_at=KICKOFF,
        ),
    ]


async def _collect() -> Any:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=OverlapMatchbook(
            [LIVE_MATCHBOOK_EVENT],
            {MB_EVENT_ID: _mb_markets()},
        ),
        polymarket=_DisabledPolymarket(),
        kalshi=OverlapKalshi(
            _kalshi_siblings(),
            series_by_ticker=MLS_SERIES,
            books=_books(),
        ),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        return await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
            enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
            selected_competition_codes=["mls"],
            unbounded_cycle=True,
        )
    finally:
        repository.close()


def test_paper_execution_boundary_and_matcher_threshold_stay_unchanged() -> None:
    settings = Settings()
    assert settings.sports_hedge_execution_enabled is False
    assert settings.sports_hedge_mode == "paper"
    assert settings.paper_scan_matchbook_concurrency == 4
    assert settings.paper_scan_kalshi_concurrency == 4
    assert settings.matchbook_event_per_page == 100
    assert settings.matchbook_event_max_pages == 10
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.MATCHBOOK] == 4
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.KALSHI] == 4
    matcher = EventMatcher()
    assert matcher.threshold == 0.92
    assert matcher.kickoff_tolerance.total_seconds() == 300
    assert "threshold: float = 0.92" in inspect.getsource(EventMatcher.__init__)
    assert settings.paper_event_match_threshold == 0.80
    paper_matcher = paper_event_matcher(settings)
    assert paper_matcher.threshold == 0.80
    assert paper_matcher.kickoff_tolerance.total_seconds() == 300
    overridden = paper_event_matcher(Settings(paper_event_match_threshold=0.85))
    assert overridden.threshold == 0.85
    scan_source = inspect.getsource(PaperScanService.scan_pair)
    assert "del minimum_mapping_confidence" in scan_source
    assert "self.market_matcher.match" in scan_source


def test_observed_raw_similarity_is_below_default_event_matcher_threshold() -> None:
    assert _raw_ratio("Miami", "Inter Miami CF") < 0.92
    assert _weighted_exact_away_competition("Miami", "Inter Miami CF") < 0.92
    assert round(_weighted_exact_away_competition("Miami", "Inter Miami CF"), 3) == 0.834
    assert EventMatcher().threshold == 0.92
    assert _weighted_exact_away_competition("Miami", "Inter Miami CF") >= 0.80
    assert Settings().paper_event_match_threshold == 0.80


def test_mls_registry_covers_2026_universe_and_observed_miami_sandiego_forms() -> None:
    names = {club.canonical_name for club in clubs_for(MLS)}
    assert "Inter Miami" in names
    assert "San Diego" in names
    assert len(MLS_CLUBS) == 30
    assert resolve_team_name("Inter Miami CF") == "inter miami"
    assert resolve_team_name("Inter Miami") == "inter miami"
    assert resolve_team_name("San Diego FC") == resolve_team_name("San Diego") == "san diego"
    assert resolve_team_name("Miami") == "miami"
    assert resolve_team_name_for_competition("Miami", MLS) == "inter miami"
    assert resolve_team_name_for_competition("Inter Miami CF", MLS) == "inter miami"
    assert canonical_team_id("Miami", MLS) == canonical_team_id("Inter Miami CF", MLS)
    assert canonical_team_id("San Diego FC") == canonical_team_id("San Diego")


def test_unrelated_miami_and_san_diego_clubs_stay_fail_closed() -> None:
    assert resolve_team_name("Inter") != resolve_team_name("Inter Miami")
    assert resolve_team_name("Inter") != resolve_team_name("Miami")
    assert resolve_team_name("AC Milan") != resolve_team_name("Inter Miami")
    assert resolve_team_name("Miami FC") == "miami fc"
    assert resolve_team_name_for_competition("Miami FC", MLS) == "miami fc"
    assert resolve_team_name_for_competition("Miami", "premier_league") == "miami"
    assert resolve_team_name_for_competition("Miami", "serie_a") == "miami"
    assert resolve_team_name_for_competition("Miami", None) == "miami"
    assert resolve_team_name("San Jose") != resolve_team_name("San Diego")
    assert resolve_team_name("New York City") != resolve_team_name("New York Red Bulls")
    assert resolve_team_name("Los Angeles FC") != resolve_team_name("LA Galaxy")
    assert resolve_team_name("Unknownville Miami") == "unknownville miami"


def test_soccer_identity_outside_mls_is_unchanged() -> None:
    assert resolve_team_name("Inter") == "inter"
    assert resolve_team_name("Inter Milan") == "inter"
    assert resolve_team_name("AC Milan") == "ac milan"
    assert resolve_team_name("Milan") == "ac milan"
    assert resolve_team_name("Brentford FC") == "brentford"
    assert resolve_team_name("Chelsea") == "chelsea"
    result = EventMatcher().match(
        _canonical(
            VenueName.MATCHBOOK,
            "Inter",
            "AC Milan",
            competition="Serie A",
            source_event_id="mb-derby",
        ),
        _canonical(
            VenueName.KALSHI,
            "Inter Miami CF",
            "AC Milan",
            competition="Serie A",
            source_event_id="k-wrong",
        ),
    )
    assert result.matched is False
    assert result.reasons == ["curated_team_mismatch"]
    outside = EventMatcher().match(
        _canonical(
            VenueName.MATCHBOOK,
            "Miami",
            "San Diego FC",
            competition="Premier League",
            source_event_id="mb-pl",
        ),
        _canonical(
            VenueName.KALSHI,
            "Inter Miami CF",
            "San Diego FC",
            competition="Premier League",
            source_event_id="k-pl",
        ),
    )
    assert outside.matched is False


def test_matchbook_us_major_league_soccer_is_retained_only_when_mls_is_selected() -> None:
    assert resolve_target_competition("US Major League Soccer") is not None
    assert resolve_target_competition("US Major League Soccer").code.value == "mls"
    default_scope = scope_matchbook_event(LIVE_MATCHBOOK_EVENT)
    assert default_scope.allowed is False
    assert default_scope.reason == "out_of_scope_competition"
    selected = scope_matchbook_event(LIVE_MATCHBOOK_EVENT, selected_codes=["mls"])
    assert selected.allowed is True
    assert selected.competition is not None
    assert selected.competition.code.value == "mls"
    filtered = filter_in_scope_events(
        [LIVE_MATCHBOOK_EVENT],
        venue=VenueName.MATCHBOOK,
        selected_codes=["mls"],
    )
    assert len(filtered.allowed) == 1
    dropped = filter_in_scope_events(
        [LIVE_MATCHBOOK_EVENT],
        venue=VenueName.MATCHBOOK,
    )
    assert dropped.allowed == []
    assert dropped.skipped == 1


def test_miami_and_inter_miami_cf_cluster_once_in_mls() -> None:
    matcher = EventMatcher()
    result = matcher.match(
        _canonical(
            VenueName.MATCHBOOK,
            "Inter Miami CF",
            "San Diego FC",
            competition="US Major League Soccer",
            source_event_id=MB_EVENT_ID,
        ),
        _canonical(
            VenueName.KALSHI,
            "Miami",
            "San Diego FC",
            competition="MLS",
            source_event_id=GAME,
        ),
    )
    assert result.matched is True
    assert result.confidence >= 0.92
    assert "home_team_fuzzy" not in result.reasons
    paper = paper_event_matcher(Settings())
    paper_result = paper.match(
        _canonical(
            VenueName.MATCHBOOK,
            "Inter Miami CF",
            "San Diego FC",
            competition="US Major League Soccer",
            source_event_id=MB_EVENT_ID,
        ),
        _canonical(
            VenueName.KALSHI,
            "Miami",
            "San Diego FC",
            competition="MLS",
            source_event_id=GAME,
        ),
    )
    assert paper.threshold == 0.80
    assert paper_result.matched is True
    assert paper_result.confidence >= 0.80
    assert paper_result.confidence == result.confidence

    clusters, counts = cluster_venue_events(
        matchbook=[
            _venue_event(
                VenueName.MATCHBOOK,
                "Inter Miami CF",
                "San Diego FC",
                competition="US Major League Soccer",
                source_event_id=MB_EVENT_ID,
            )
        ],
        polymarket=[
            _venue_event(
                VenueName.POLYMARKET,
                "Inter Miami CF",
                "San Diego FC",
                competition="MLS",
                source_event_id="983347",
            )
        ],
        kalshi=[
            _venue_event(
                VenueName.KALSHI,
                "Miami",
                "San Diego FC",
                competition="MLS",
                source_event_id=GAME,
            ),
            _venue_event(
                VenueName.KALSHI,
                "Miami",
                "San Diego FC",
                competition="MLS",
                source_event_id=BTTS,
            ),
            _venue_event(
                VenueName.KALSHI,
                "Miami",
                "San Diego FC",
                competition="MLS",
                source_event_id=TOTAL,
            ),
            _venue_event(
                VenueName.KALSHI,
                "Inter Miami CF",
                "San Diego FC",
                competition="MLS",
                source_event_id=FTTS,
                kickoff=FTTS_KICKOFF,
            ),
        ],
        matcher=matcher,
        max_event_pairs=16,
    )
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.matchbook is not None
    assert cluster.matchbook.source_event_id == MB_EVENT_ID
    assert {item.source_event_id for item in cluster.kalshi_events} == {GAME, BTTS, TOTAL, FTTS}
    assert counts["matchbook_kalshi"] == 1
    canonical_id = cluster_canonical_event_id(cluster)
    aliases = cluster_identity_aliases(cluster)
    assert aliases[MB_EVENT_ID] == canonical_id
    assert aliases[GAME] == canonical_id
    assert aliases[BTTS] == canonical_id
    assert aliases[TOTAL] == canonical_id
    assert aliases[FTTS] == canonical_id
    assert aliases["983347"] == canonical_id


@pytest.mark.parametrize(
    ("home_left", "away_left", "home_right", "away_right", "competition"),
    [
        ("Inter Miami CF", "San Diego FC", "Inter", "San Diego FC", "MLS"),
        ("Miami", "San Diego FC", "Inter", "San Diego FC", "Serie A"),
        ("Inter Miami CF", "San Diego FC", "Inter Miami CF", "San Jose Earthquakes", "MLS"),
        ("Miami", "San Diego FC", "Miami FC", "San Diego FC", "MLS"),
        ("Inter Miami CF", "San Diego FC", "Inter Miami Women", "San Diego FC", "MLS"),
        ("Inter Miami CF", "San Diego FC", "Inter Miami CF", "San Diego FC U21", "MLS"),
    ],
)
def test_mls_aliases_do_not_merge_distinct_or_non_senior_sides(
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
async def test_collector_collapses_kalshi_siblings_and_attaches_matchbook_markets() -> None:
    report = await _collect()
    clustered = [
        item
        for item in report.discovered_fixtures
        if item.matchbook_matched and item.kalshi_matched
    ]
    assert len(clustered) == 1
    fixture = clustered[0]
    assert fixture.polymarket_matched is False
    kalshi_ids = {
        str(row["source_event_id"])
        for row in report.fixture_source_events.get(fixture.canonical_event_id, [])
        if str(row.get("venue")) == VenueName.KALSHI.value
    }
    matchbook_ids = {
        str(row["source_event_id"])
        for row in report.fixture_source_events.get(fixture.canonical_event_id, [])
        if str(row.get("venue")) == VenueName.MATCHBOOK.value
    }
    assert kalshi_ids == {GAME, BTTS, TOTAL, FTTS}
    assert matchbook_ids == {MB_EVENT_ID}
    aliases = report.fixture_identity_aliases
    canonical_id = fixture.canonical_event_id
    assert aliases[MB_EVENT_ID] == canonical_id
    for source_id in kalshi_ids:
        assert aliases[source_id] == canonical_id
    rows = report.fixture_markets[canonical_id]
    comparable = {
        row.family: row.comparison_status
        for row in rows
        if row.family
        in {
            "match_result",
            "both_teams_to_score",
            "total_goals",
            "first_team_to_score",
        }
    }
    assert comparable["match_result"] is InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT
    assert comparable["both_teams_to_score"] is InventoryComparisonStatus.MATCHED_EQUIVALENT
    assert comparable["total_goals"] is InventoryComparisonStatus.MATCHED_EQUIVALENT
    # Live Matchbook 2026-09-20 listed Match Odds / BTTS / Total, not FTTS.
    # Kalshi FTTS still belongs on the same clustered fixture.
    assert comparable["first_team_to_score"] is InventoryComparisonStatus.VENUE_ONLY
    assert EventMatcher().threshold == 0.92
    assert Settings().paper_event_match_threshold == 0.80
    assert Settings().paper_scan_matchbook_concurrency == 4
    assert fixture.event_match_threshold == 0.80
    assert fixture.event_match_confidence is not None
    assert fixture.event_match_confidence >= 0.80
    assert report.scan_diagnostics["event_match_threshold"] == 0.80
    assert report.scan_diagnostics["event_match_confidences"][fixture.canonical_event_id] == (
        fixture.event_match_confidence
    )
    assert report.scan_diagnostics["provider_concurrency"]["matchbook"] == 4
    assert report.scan_diagnostics["provider_concurrency"]["kalshi"] == 4
