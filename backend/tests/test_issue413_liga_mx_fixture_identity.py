"""Issue #413: Liga MX Querétaro vs Club León fixture identity.

Live read-only evidence captured 2026-09-20 against owner-live
``588c8b41802690c226ce338e871ba17fcd367eab`` (PAPER / no venue writes):

Matchbook GET /edge/rest/events:
- id ``34343548143100081``
- name ``Querétaro vs Club León``
- start ``2026-09-21T02:05:00.000Z``
- competition meta-tag ``Mexico Liga MX``

Kalshi public Trade API v2 sibling events, same suffix ``26SEP20QUELEO``:
- ``KXLIGAMXGAME-26SEP20QUELEO`` title ``Queretaro vs Leon``
- ``KXLIGAMXBTTS-26SEP20QUELEO`` title ``Queretaro vs Leon: BTTS``
- ``KXLIGAMXTOTAL-26SEP20QUELEO`` title ``Queretaro vs Leon: Total Goals``
- ``KXLIGAMXFTTS-26SEP20QUELEO`` title ``Queretaro FC vs Club Leon: First Team to Score``
- GAME/BTTS/TOTAL milestone ``2026-09-21T02:10:00Z`` (exactly 5 minutes after Matchbook)

Structural aliases are the preferred exact identity fix. Generic city tokens
``Queretaro`` / ``Leon`` resolve only inside Liga MX. PAPER EventMatcher uses
the configurable 0.80 threshold; class/default identity stays 0.92. The
existing 5-minute kickoff window is inclusive (02:05 vs 02:10 collapses;
02:05 vs 02:11 does not).

Data class: deterministic fixture/demo providers. Not live venue quotes.
Paper-only; execution stays disabled.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.fixture_clusters import (
    cluster_canonical_event_id,
    cluster_identity_aliases,
    cluster_venue_events,
)
from sports_hedge.application.hot_identity import same_hot_scheduling_unit
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
from sports_hedge.facts.team_registry import LIGA_MX, LIGA_MX_CLUBS, MLS, clubs_for
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.events import EventMatcher, paper_event_matcher
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.application.capture_replay import DEFAULT_KALSHI_BOOK
from test_issue293_owner_live_overlap import OverlapKalshi, OverlapMatchbook
from test_issue316_catalogue_registry import _DisabledPolymarket
from venue_cost_helpers import matchbook_polymarket_costs

MB_KICKOFF = datetime(2026, 9, 21, 2, 5, tzinfo=UTC)
KALSHI_KICKOFF = datetime(2026, 9, 21, 2, 10, tzinfo=UTC)
OUTSIDE_KICKOFF = datetime(2026, 9, 21, 2, 11, tzinfo=UTC)
MB_EVENT_ID = "34343548143100081"
GAME = "KXLIGAMXGAME-26SEP20QUELEO"
BTTS = "KXLIGAMXBTTS-26SEP20QUELEO"
TOTAL = "KXLIGAMXTOTAL-26SEP20QUELEO"
FTTS = "KXLIGAMXFTTS-26SEP20QUELEO"
LIGA_MX_SERIES = {
    "KXLIGAMXGAME": {
        "ticker": "KXLIGAMXGAME",
        "title": "Liga MX",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
        "settlement_sources": [{"name": "Opta"}],
    },
    "KXLIGAMXBTTS": {
        "ticker": "KXLIGAMXBTTS",
        "title": "Liga MX",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
        "settlement_sources": [{"name": "Opta"}],
    },
    "KXLIGAMXTOTAL": {
        "ticker": "KXLIGAMXTOTAL",
        "title": "Liga MX",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
        "settlement_sources": [{"name": "Opta"}],
    },
    "KXLIGAMXFTTS": {
        "ticker": "KXLIGAMXFTTS",
        "title": "Liga MX",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
        "settlement_sources": [{"name": "Opta"}],
    },
}

LIVE_MATCHBOOK_EVENT = {
    "id": int(MB_EVENT_ID),
    "name": "Querétaro vs Club León",
    "start": "2026-09-21T02:05:00.000Z",
    "status": "open",
    "sport-id": 15,
    "meta-tags": [
        {"id": 15, "name": "Soccer", "type": "SPORT", "url-name": "soccer"},
        {
            "id": 1190793990860099,
            "name": "Mexico Liga MX",
            "type": "COMPETITION",
            "url-name": "mexico-liga-mx",
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
    kickoff: datetime = MB_KICKOFF,
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
    kickoff: datetime = MB_KICKOFF,
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


def _runner(runner_id: int, name: str, odds: str = "2.10") -> dict[str, Any]:
    return {
        "id": runner_id,
        "name": name,
        "prices": [{"side": "back", "odds": odds, "available-amount": "80"}],
    }


def _mb_markets() -> list[dict[str, Any]]:
    return [
        {
            "id": 34343548143110081,
            "name": "Match Odds",
            "runners": [
                _runner(1, "Querétaro", "2.40"),
                _runner(2, "Draw", "3.40"),
                _runner(3, "Club León", "2.90"),
            ],
        },
        {
            "id": 34343548143120081,
            "name": "Both Teams To Score",
            "runners": [_runner(11, "Yes", "1.90"), _runner(12, "No", "1.95")],
        },
        {
            "id": 34343548143130081,
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
    kickoff: datetime = KALSHI_KICKOFF,
) -> dict[str, Any]:
    return {
        "event_ticker": ticker,
        "series_ticker": series,
        "title": title,
        "category": "Sports",
        "strike_date": kickoff.isoformat(),
        "milestone": {"start_date": kickoff.isoformat()},
        "product_metadata": {"competition": "Liga MX", "competition_scope": "Game"},
        "markets": markets,
    }


def _kalshi_siblings() -> list[dict[str, Any]]:
    return [
        _kalshi_event(
            GAME,
            "KXLIGAMXGAME",
            "Queretaro vs Leon",
            [
                {
                    "ticker": f"{GAME}-QUE",
                    "event_ticker": GAME,
                    "title": "Queretaro wins",
                    "yes_sub_title": "Queretaro",
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
                    "ticker": f"{GAME}-LEO",
                    "event_ticker": GAME,
                    "title": "Leon wins",
                    "yes_sub_title": "Leon",
                    "rules_primary": GAMEWIN_TEMPLATE,
                },
            ],
        ),
        _kalshi_event(
            BTTS,
            "KXLIGAMXBTTS",
            "Queretaro vs Leon: BTTS",
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
            "KXLIGAMXTOTAL",
            "Queretaro vs Leon: Total Goals",
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
            "KXLIGAMXFTTS",
            "Queretaro FC vs Club Leon: First Team to Score",
            [
                {
                    "ticker": f"{FTTS}-QUE",
                    "event_ticker": FTTS,
                    "title": "First team to score",
                    "yes_sub_title": "Queretaro FC",
                    "rules_primary": REGULATION,
                },
                {
                    "ticker": f"{FTTS}-LEO",
                    "event_ticker": FTTS,
                    "title": "First team to score",
                    "yes_sub_title": "Club Leon",
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
        ),
    ]


def _books() -> dict[str, dict[str, Any]]:
    tickers = [
        f"{GAME}-QUE",
        f"{GAME}-TIE",
        f"{GAME}-LEO",
        f"{BTTS}-BTTS",
        f"{TOTAL}-3",
        f"{FTTS}-QUE",
        f"{FTTS}-LEO",
        f"{FTTS}-NONE",
    ]
    return {ticker: deepcopy(DEFAULT_KALSHI_BOOK) for ticker in tickers}


def _costs() -> list[Any]:
    captured = MB_KICKOFF
    return [
        *matchbook_polymarket_costs(captured_at=captured),
        *[kalshi_cost_from_series(series, captured_at=captured) for series in LIGA_MX_SERIES.values()],
    ]


def _fx() -> list[FxRateSnapshot]:
    return [
        FxRateSnapshot(
            currency="USD", gbp_per_unit=Decimal("0.75"), source="test_fx", captured_at=MB_KICKOFF
        ),
        FxRateSnapshot(
            currency="GBP",
            gbp_per_unit=Decimal("1"),
            source="functional_currency",
            captured_at=MB_KICKOFF,
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
            series_by_ticker=LIGA_MX_SERIES,
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
            selected_competition_codes=["liga_mx"],
            unbounded_cycle=True,
        )
    finally:
        repository.close()


def test_liga_mx_registry_covers_current_senior_clubs_and_observed_forms() -> None:
    names = {club.canonical_name for club in clubs_for(LIGA_MX)}
    assert "Querétaro FC" in names
    assert "Club León" in names
    assert "Club América" in names
    assert len(LIGA_MX_CLUBS) >= 18
    for alias in ("Querétaro", "Queretaro", "Querétaro FC", "Queretaro FC"):
        assert resolve_team_name_for_competition(alias, LIGA_MX) == "queretaro fc"
    for alias in ("León", "Leon", "Club León", "Club Leon"):
        assert resolve_team_name_for_competition(alias, LIGA_MX) == "club leon"


def test_liga_mx_generic_aliases_fail_closed_outside_competition() -> None:
    assert resolve_team_name("Leon") == "leon"
    assert resolve_team_name("León") == "leon"
    assert resolve_team_name("Queretaro") == "queretaro"
    assert resolve_team_name("Querétaro") == "queretaro"
    assert resolve_team_name("Club León") == "club leon"
    assert resolve_team_name("Queretaro FC") == "queretaro fc"
    assert resolve_team_name_for_competition("Leon", MLS) == "leon"
    assert resolve_team_name_for_competition("Leon", "premier_league") == "leon"
    assert resolve_team_name_for_competition("Queretaro", "la_liga") == "queretaro"
    assert resolve_team_name_for_competition("America", LIGA_MX) == "club america"
    assert resolve_team_name("America") == "america"
    assert resolve_team_name("Inter") == "inter"
    assert resolve_team_name("Brentford FC") == "brentford"


def test_mexico_liga_mx_is_retained_only_when_liga_mx_is_selected() -> None:
    target = resolve_target_competition("Mexico Liga MX")
    assert target is not None
    assert target.code.value == "liga_mx"
    default_scope = scope_matchbook_event(LIVE_MATCHBOOK_EVENT)
    assert default_scope.allowed is False
    assert default_scope.reason == "out_of_scope_competition"
    selected = scope_matchbook_event(LIVE_MATCHBOOK_EVENT, selected_codes=["liga_mx"])
    assert selected.allowed is True
    filtered = filter_in_scope_events(
        [LIVE_MATCHBOOK_EVENT],
        venue=VenueName.MATCHBOOK,
        selected_codes=["liga_mx"],
    )
    assert len(filtered.allowed) == 1
    dropped = filter_in_scope_events([LIVE_MATCHBOOK_EVENT], venue=VenueName.MATCHBOOK)
    assert dropped.allowed == []


def test_queretaro_and_club_leon_collapse_inside_five_minute_kickoff() -> None:
    matcher = EventMatcher()
    assert matcher.threshold == 0.92
    assert matcher.kickoff_tolerance == timedelta(minutes=5)
    result = matcher.match(
        _canonical(
            VenueName.MATCHBOOK,
            "Querétaro",
            "Club León",
            competition="Mexico Liga MX",
            source_event_id=MB_EVENT_ID,
            kickoff=MB_KICKOFF,
        ),
        _canonical(
            VenueName.KALSHI,
            "Queretaro",
            "Leon",
            competition="Liga MX",
            source_event_id=GAME,
            kickoff=KALSHI_KICKOFF,
        ),
    )
    assert result.matched is True
    assert result.confidence >= 0.92
    assert "kickoff_offset" in result.reasons
    outside = matcher.match(
        _canonical(
            VenueName.MATCHBOOK,
            "Querétaro",
            "Club León",
            competition="Mexico Liga MX",
            source_event_id=MB_EVENT_ID,
            kickoff=MB_KICKOFF,
        ),
        _canonical(
            VenueName.KALSHI,
            "Queretaro",
            "Leon",
            competition="Liga MX",
            source_event_id=GAME,
            kickoff=OUTSIDE_KICKOFF,
        ),
    )
    assert outside.matched is False
    assert outside.reasons == ["kickoff_outside_tolerance"]
    paper = paper_event_matcher(Settings())
    paper_result = paper.match(
        _canonical(
            VenueName.MATCHBOOK,
            "Querétaro",
            "Club León",
            competition="Mexico Liga MX",
            source_event_id=MB_EVENT_ID,
            kickoff=MB_KICKOFF,
        ),
        _canonical(
            VenueName.KALSHI,
            "Queretaro FC",
            "Club Leon",
            competition="Liga MX",
            source_event_id=FTTS,
            kickoff=KALSHI_KICKOFF,
        ),
    )
    assert paper.threshold == 0.80
    assert paper_result.matched is True
    assert paper_result.confidence >= 0.80


def test_generic_leon_outside_liga_mx_does_not_match_club_leon() -> None:
    result = EventMatcher().match(
        _canonical(
            VenueName.MATCHBOOK,
            "Leon",
            "Querétaro",
            competition="Premier League",
            source_event_id="mb-pl",
        ),
        _canonical(
            VenueName.KALSHI,
            "Club León",
            "Querétaro FC",
            competition="Premier League",
            source_event_id="k-pl",
        ),
    )
    assert result.matched is False


def test_liga_mx_siblings_cluster_once_across_matchbook_and_kalshi() -> None:
    matcher = EventMatcher()
    clusters, counts = cluster_venue_events(
        matchbook=[
            _venue_event(
                VenueName.MATCHBOOK,
                "Querétaro",
                "Club León",
                competition="Mexico Liga MX",
                source_event_id=MB_EVENT_ID,
                kickoff=MB_KICKOFF,
            )
        ],
        polymarket=[],
        kalshi=[
            _venue_event(
                VenueName.KALSHI,
                "Queretaro",
                "Leon",
                competition="Liga MX",
                source_event_id=GAME,
                kickoff=KALSHI_KICKOFF,
            ),
            _venue_event(
                VenueName.KALSHI,
                "Queretaro",
                "Leon",
                competition="Liga MX",
                source_event_id=BTTS,
                kickoff=KALSHI_KICKOFF,
            ),
            _venue_event(
                VenueName.KALSHI,
                "Queretaro",
                "Leon",
                competition="Liga MX",
                source_event_id=TOTAL,
                kickoff=KALSHI_KICKOFF,
            ),
            _venue_event(
                VenueName.KALSHI,
                "Queretaro FC",
                "Club Leon",
                competition="Liga MX",
                source_event_id=FTTS,
                kickoff=KALSHI_KICKOFF,
            ),
        ],
        matcher=matcher,
        max_event_pairs=16,
    )
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.matchbook is not None
    assert {item.source_event_id for item in cluster.kalshi_events} == {GAME, BTTS, TOTAL, FTTS}
    assert counts["matchbook_kalshi"] == 1
    canonical_id = cluster_canonical_event_id(cluster)
    aliases = cluster_identity_aliases(cluster)
    assert aliases[MB_EVENT_ID] == canonical_id
    for source_id in (GAME, BTTS, TOTAL, FTTS):
        assert aliases[source_id] == canonical_id
    assert cluster.event_match_confidence is not None
    assert cluster.event_match_confidence >= matcher.threshold
    assert cluster.event_match_threshold == matcher.threshold
    assert same_hot_scheduling_unit(
        cluster.matchbook.canonical,
        cluster.kalshi_events[0].canonical,
    )


@pytest.mark.asyncio
async def test_collector_collapses_liga_mx_live_labels_and_reports_confidence() -> None:
    report = await _collect()
    clustered = [
        item
        for item in report.discovered_fixtures
        if item.matchbook_matched and item.kalshi_matched
    ]
    assert len(clustered) == 1
    fixture = clustered[0]
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
    assert fixture.event_match_threshold == 0.80
    assert fixture.event_match_confidence is not None
    assert fixture.event_match_confidence >= 0.80
    assert report.scan_diagnostics["event_match_threshold"] == 0.80
    assert EventMatcher().threshold == 0.92
    assert Settings().paper_event_match_threshold == 0.80
    assert Settings().paper_scan_matchbook_concurrency == 4
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.MATCHBOOK] == 4
    assert report.scan_diagnostics["provider_concurrency"]["matchbook"] == 4
    assert report.scan_diagnostics["provider_concurrency"]["kalshi"] == 4
