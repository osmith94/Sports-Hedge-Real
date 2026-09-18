"""Issue #309: duplicate canonical fixtures from FC-suffix club-name variants.

Owner-live observation on exact #306 head (PAPER/read-only, Polymarket off):

- ``Brentford FC v Chelsea FC`` (Matchbook + Kalshi GAME) and
  ``Brentford v Chelsea`` (Kalshi BTTS/TOTAL siblings) are the same Premier
  League fixture at the same kickoff.
- Raw SequenceMatcher on the FC suffix is below EventMatcher 0.92, so
  ClusterPass emitted two current rows. FixtureCurrentStateStore then kept
  both because they shared no source/canonical alias.

Fix is the existing curated self-alias + fail-closed FC/CF/AFC/SC remainder
strip. The EventMatcher threshold stays 0.92. This does not strip FC globally
and does not change ``fixture_current_state.py`` merge policy.

Data class: deterministic fixture/demo providers. Not live, historical, or
modelled venue quotes. Paper-only; execution stays disabled.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

import pytest

from sports_hedge.application.collector import CollectionReport, DiscoveredFixture
from sports_hedge.application.fixture_clusters import (
    VenueEvent,
    cluster_canonical_event_id,
    cluster_identity_aliases,
    cluster_venue_events,
)
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.fixture_inventory import (
    FixtureMarketInventoryRow,
    InventoryComparisonStatus,
)
from sports_hedge.application.hot_identity import hot_scheduling_key
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.config import Settings
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.facts.aliases import resolve_team_name
from sports_hedge.facts.identity import canonical_team_id
from sports_hedge.matching.events import EventMatcher
from sports_hedge.normalization.text import normalize_text
from test_dual_cadence_scheduler import NOW, _report
from test_issue277_club_name_variants import _collect_sibling_fixture


KICKOFF = datetime(2026, 9, 18, 19, 0, tzinfo=UTC)
EPL_KALSHI_SERIES = {
    "ticker": "KXEPLGAME",
    "title": "Premier League",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "settlement_sources": [{"name": "Opta"}],
}
MB_EVENT_ID = "30901"
K_GAME = "KXEPLGAME-26SEP18BRECFC"
K_BTTS = "KXEPLBTTS-26SEP18BRECFC"
K_TOTAL = "KXEPLTOTAL-26SEP18BRECFC"


def _canonical(
    venue: VenueName,
    home: str,
    away: str,
    *,
    competition: str = "Premier League",
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
    source_event_id: str,
    competition: str = "Premier League",
    kickoff: datetime = KICKOFF,
) -> VenueEvent:
    canonical = _canonical(
        venue,
        home,
        away,
        competition=competition,
        source_event_id=source_event_id,
        kickoff=kickoff,
    )
    return VenueEvent(
        venue=venue,
        raw={"id": source_event_id, "title": f"{home} vs {away}"},
        canonical=canonical,
        source_event_id=source_event_id,
    )


def _owner_clusters(*, kickoff: datetime = KICKOFF) -> tuple[list[Any], dict[str, int]]:
    return cluster_venue_events(
        matchbook=[
            _venue_event(
                VenueName.MATCHBOOK,
                "Brentford FC",
                "Chelsea FC",
                source_event_id=MB_EVENT_ID,
                kickoff=kickoff,
            )
        ],
        polymarket=[],
        kalshi=[
            _venue_event(
                VenueName.KALSHI,
                "Brentford FC",
                "Chelsea FC",
                source_event_id=K_GAME,
                kickoff=kickoff,
            ),
            _venue_event(
                VenueName.KALSHI,
                "Brentford",
                "Chelsea",
                source_event_id=K_BTTS,
                kickoff=kickoff,
            ),
            _venue_event(
                VenueName.KALSHI,
                "Brentford",
                "Chelsea",
                source_event_id=K_TOTAL,
                kickoff=kickoff,
            ),
        ],
        matcher=EventMatcher(),
        max_event_pairs=16,
    )


def _fixture_from_cluster(cluster: Any, *, kickoff: datetime = KICKOFF) -> DiscoveredFixture:
    canonical_id = cluster_canonical_event_id(cluster)
    return DiscoveredFixture(
        source=cluster.anchor.venue,
        source_event_id=cluster.anchor.source_event_id,
        canonical_event_id=canonical_id,
        home_team=cluster.anchor.canonical.home_team,
        away_team=cluster.anchor.canonical.away_team,
        competition="Premier League",
        kickoff_utc=kickoff,
        last_seen_at=kickoff,
        last_scanned_at=kickoff,
        matchbook_matched=cluster.matchbook is not None,
        kalshi_matched=bool(cluster.kalshi_events),
        polymarket_matched=False,
        market_evaluation_state="evaluated",
        opportunity_state="matched",
    )


def _inventory(family: str, display_name: str) -> FixtureMarketInventoryRow:
    return FixtureMarketInventoryRow(
        display_name=display_name,
        family=family,
        period="full_time",
        comparison_status=InventoryComparisonStatus.VENUE_ONLY,
    )


def _raw_ratio(left: str, right: str) -> float:
    return SequenceMatcher(a=normalize_text(left), b=normalize_text(right)).ratio()


def test_paper_execution_boundary_and_matcher_threshold_stay_unchanged() -> None:
    settings = Settings()
    assert settings.sports_hedge_execution_enabled is False
    assert settings.sports_hedge_mode == "paper"
    matcher = EventMatcher()
    assert matcher.threshold == 0.92
    assert matcher.kickoff_tolerance.total_seconds() == 300


def test_observed_raw_fc_suffix_similarity_is_below_event_matcher_threshold() -> None:
    assert _raw_ratio("Brentford FC", "Brentford") < 0.92
    assert _raw_ratio("Chelsea FC", "Chelsea") < 0.92
    assert EventMatcher().threshold == 0.92


def test_curated_identity_converges_brentford_chelsea_fc_suffixes() -> None:
    assert resolve_team_name("Brentford FC") == resolve_team_name("Brentford") == "brentford"
    assert resolve_team_name("Chelsea FC") == resolve_team_name("Chelsea") == "chelsea"
    assert canonical_team_id("Brentford FC") == canonical_team_id("Brentford")
    assert canonical_team_id("Chelsea FC") == canonical_team_id("Chelsea")
    assert resolve_team_name("FC Unknownville") == "fc unknownville"
    assert resolve_team_name("Unknownville FC") == "unknownville fc"


def test_event_matcher_clusters_brentford_fc_and_short_kalshi_siblings() -> None:
    matcher = EventMatcher()
    result = matcher.match(
        _canonical(VenueName.MATCHBOOK, "Brentford FC", "Chelsea FC", source_event_id=MB_EVENT_ID),
        _canonical(VenueName.KALSHI, "Brentford", "Chelsea", source_event_id=K_BTTS),
    )
    assert result.matched is True
    assert result.confidence >= 0.92
    assert "home_team_fuzzy" not in result.reasons
    assert "away_team_fuzzy" not in result.reasons

    clusters, counts = _owner_clusters()
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.matchbook is not None
    assert {item.source_event_id for item in cluster.kalshi_events} == {K_GAME, K_BTTS, K_TOTAL}
    assert counts["matchbook_kalshi"] == 1
    aliases = cluster_identity_aliases(cluster)
    canonical_id = cluster_canonical_event_id(cluster)
    for source_id in (MB_EVENT_ID, K_GAME, K_BTTS, K_TOTAL):
        assert aliases[source_id] == canonical_id


def test_similar_names_with_different_opponent_never_merge() -> None:
    result = EventMatcher().match(
        _canonical(VenueName.MATCHBOOK, "Brentford FC", "Chelsea FC", source_event_id="left"),
        _canonical(VenueName.KALSHI, "Brentford", "Arsenal", source_event_id="right"),
    )
    assert result.matched is False
    clusters, _ = cluster_venue_events(
        matchbook=[
            _venue_event(
                VenueName.MATCHBOOK,
                "Brentford FC",
                "Chelsea FC",
                source_event_id="mb-breche",
            )
        ],
        polymarket=[],
        kalshi=[
            _venue_event(
                VenueName.KALSHI,
                "Brentford",
                "Arsenal",
                source_event_id="k-brears",
            )
        ],
        matcher=EventMatcher(),
        max_event_pairs=8,
    )
    assert len(clusters) == 2


def test_same_teams_outside_kickoff_tolerance_never_merge() -> None:
    outside = KICKOFF + timedelta(minutes=6)
    result = EventMatcher().match(
        _canonical(
            VenueName.MATCHBOOK,
            "Brentford FC",
            "Chelsea FC",
            source_event_id="left",
            kickoff=KICKOFF,
        ),
        _canonical(
            VenueName.KALSHI,
            "Brentford",
            "Chelsea",
            source_event_id="right",
            kickoff=outside,
        ),
    )
    assert result.matched is False
    assert result.reasons == ["kickoff_outside_tolerance"]


@pytest.mark.parametrize(
    ("home_left", "away_left", "home_right", "away_right"),
    [
        ("Brentford FC", "Chelsea FC", "Brentford U21", "Chelsea"),
        ("Brentford", "Chelsea FC", "Brentford", "Chelsea Women"),
        ("Brentford FC", "Chelsea FC", "Brentford", "Chelsea Reserves"),
    ],
)
def test_youth_women_reserve_category_conflict_never_merge(
    home_left: str,
    away_left: str,
    home_right: str,
    away_right: str,
) -> None:
    result = EventMatcher().match(
        _canonical(VenueName.MATCHBOOK, home_left, away_left, source_event_id="left"),
        _canonical(VenueName.KALSHI, home_right, away_right, source_event_id="right"),
    )
    assert result.matched is False


def test_current_state_has_one_radar_row_and_source_aliases() -> None:
    clusters, _ = _owner_clusters()
    assert len(clusters) == 1
    cluster = clusters[0]
    fixture = _fixture_from_cluster(cluster)
    aliases = cluster_identity_aliases(cluster)
    canonical_id = cluster_canonical_event_id(cluster)
    sources = {
        canonical_id: [
            {"venue": item.venue.value, "source_event_id": item.source_event_id, "raw": dict(item.raw)}
            for item in [*cluster.matchbook_events, *cluster.kalshi_events]
        ]
    }
    store = FixtureCurrentStateStore()
    scanned = KICKOFF - timedelta(hours=7)
    store.upsert_from_report(
        CollectionReport(
            started_at=scanned,
            completed_at=scanned,
            discovered_fixtures=[fixture],
            fixture_markets={canonical_id: [_inventory("btts", "Both Teams To Score")]},
            fixture_identity_aliases=aliases,
            fixture_source_events=sources,
            scan_lane=ScanLane.UNIVERSE.value,
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=scanned,
    )
    assert len(store._rows) == 1
    survivor = next(iter(store._rows))
    for identity in (canonical_id, MB_EVENT_ID, K_GAME, K_BTTS, K_TOTAL):
        assert store.resolve_canonical_id(identity) == survivor
    radar = store.current_radar_rows(scanned)
    assert len(radar) == 1
    assert radar[0].fixture.matchbook_matched is True
    assert radar[0].fixture.kalshi_matched is True


def test_absorbed_market_inventory_is_preserved() -> None:
    game_only, _ = cluster_venue_events(
        matchbook=[],
        polymarket=[],
        kalshi=[
            _venue_event(
                VenueName.KALSHI,
                "Brentford FC",
                "Chelsea FC",
                source_event_id=K_GAME,
            )
        ],
        matcher=EventMatcher(),
        max_event_pairs=4,
    )
    unified, _ = _owner_clusters()
    assert len(game_only) == 1
    assert len(unified) == 1
    first_id = cluster_canonical_event_id(game_only[0])
    unified_id = cluster_canonical_event_id(unified[0])
    store = FixtureCurrentStateStore()
    scanned = KICKOFF - timedelta(hours=7)
    store.upsert_from_report(
        CollectionReport(
            started_at=scanned,
            completed_at=scanned,
            discovered_fixtures=[_fixture_from_cluster(game_only[0])],
            fixture_markets={first_id: [_inventory("match_result", "Match Result")]},
            fixture_identity_aliases=cluster_identity_aliases(game_only[0]),
            fixture_source_events={
                first_id: [
                    {"venue": VenueName.KALSHI.value, "source_event_id": K_GAME, "raw": {"id": K_GAME}}
                ]
            },
            scan_lane=ScanLane.UNIVERSE.value,
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=scanned,
    )
    store.upsert_from_report(
        CollectionReport(
            started_at=scanned,
            completed_at=scanned,
            discovered_fixtures=[_fixture_from_cluster(unified[0])],
            fixture_markets={unified_id: [_inventory("btts", "Both Teams To Score")]},
            fixture_identity_aliases=cluster_identity_aliases(unified[0]),
            fixture_source_events={
                unified_id: [
                    {
                        "venue": item.venue.value,
                        "source_event_id": item.source_event_id,
                        "raw": dict(item.raw),
                    }
                    for item in [*unified[0].matchbook_events, *unified[0].kalshi_events]
                ]
            },
            scan_lane=ScanLane.UNIVERSE.value,
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=scanned,
    )
    assert len(store._rows) == 1
    survivor = next(iter(store._rows))
    assert store.resolve_canonical_id(K_GAME) == survivor
    assert store.resolve_canonical_id(MB_EVENT_ID) == survivor
    radar = store.current_radar_rows(scanned)
    assert len(radar) == 1
    families = {row.family for row in radar[0].markets}
    assert families >= {"match_result", "btts"}


def test_hot_scheduling_is_one_unit_for_fc_and_short_names() -> None:
    clusters, _ = _owner_clusters()
    cluster = clusters[0]
    fixture = _fixture_from_cluster(cluster, kickoff=NOW.replace(hour=15, minute=30))
    short = fixture.model_copy(update={"home_team": "Brentford", "away_team": "Chelsea"})
    fc = fixture.model_copy(update={"home_team": "Brentford FC", "away_team": "Chelsea FC"})
    assert hot_scheduling_key(short) is not None
    assert hot_scheduling_key(short) == hot_scheduling_key(fc)
    store = FixtureCurrentStateStore()
    store.upsert_from_report(
        _report(
            [fixture],
            when=NOW,
            scan_lane=ScanLane.UNIVERSE.value,
        ).model_copy(
            update={
                "fixture_identity_aliases": cluster_identity_aliases(cluster),
                "fixture_source_events": {
                    fixture.canonical_event_id: [
                        {
                            "venue": item.venue.value,
                            "source_event_id": item.source_event_id,
                            "raw": dict(item.raw),
                        }
                        for item in [*cluster.matchbook_events, *cluster.kalshi_events]
                    ]
                },
            }
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    unique, _lifecycle, _promoted = store.hot_membership_breakdown(NOW)
    assert unique == 1
    assert len(store._rows) == 1


@pytest.mark.asyncio
async def test_collector_unions_brentford_kalshi_siblings_with_matchbook() -> None:
    report = await _collect_sibling_fixture(
        matchbook_home="Brentford FC",
        matchbook_away="Chelsea FC",
        competition="Premier League",
        kalshi_titles=[
            (K_GAME, "Brentford FC vs Chelsea FC"),
            (K_BTTS, "Brentford vs Chelsea"),
            (K_TOTAL, "Brentford v Chelsea"),
        ],
        series=EPL_KALSHI_SERIES,
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
    assert kalshi_ids == {K_GAME, K_BTTS, K_TOTAL}
    aliases = report.fixture_identity_aliases
    canonical_id = fixture.canonical_event_id
    assert aliases["27701"] == canonical_id
    for source_id in kalshi_ids:
        assert aliases[source_id] == canonical_id


@pytest.mark.asyncio
async def test_issue277_monza_regression_still_unions() -> None:
    from test_issue277_club_name_variants import SERIE_A_KALSHI_SERIES

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
