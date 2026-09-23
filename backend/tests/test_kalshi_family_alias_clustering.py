"""Kalshi GAME/BTTS/TOTAL/FTTS sibling clustering for provider club-name variants.

Owner-live observation on integrated `owner-live` at
``0b74398eb342abe9cd92065314ae71251edb24d2`` during PAPER soak:

- ``Frosinone v Como`` (Matchbook + Kalshi GAME) vs
  ``Frosinone Calcio v Como 1907`` (Kalshi FTTS/TOTAL siblings)
- ``AFC Bournemouth v Liverpool`` vs ``Bournemouth v Liverpool``
- ``Juventus v Atalanta`` vs ``Juventus Turin v Atalanta BC``

Root cause (logical trace, not fuzzy-threshold widening):

- EventMatcher still requires confidence >= 0.92. Raw SequenceMatcher on the
  observed legal-name variants is below that.
- ``Como 1907`` and ``AFC Bournemouth`` were already curated aliases, so those
  sides already collapsed. ``Frosinone`` was not a senior-club canonical, so
  ``Frosinone Calcio`` could not use the fail-closed remainder strip or the
  exact-identity kickoff exemption. ``Juventus Turin`` / ``Atalanta BC`` were
  missing aliases, and fuzzy scores on *both* sides dropped the pair below 0.92.
- ClusterPass therefore emitted a Matchbook/Kalshi GAME row plus orphan
  Kalshi-only FTTS/TOTAL rows that shared no source/canonical alias.

Fix is curated aliases plus the existing fail-closed legal-form remainder strip
(``calcio`` / ``bc`` as the Italian equivalent of FC/SC). The EventMatcher
threshold stays 0.92. City qualifiers such as Turin are explicit aliases only.
Unknown remainders, youth/women/reserves, and distinct clubs stay fail-closed.

Data class: deterministic fixture/demo providers. Not live, historical, or
modelled venue quotes. Paper-only; execution stays disabled.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

import pytest
from test_issue277_club_name_variants import _collect_sibling_fixture

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
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.config import Settings
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.facts.aliases import SAFE_TEAM_AFFIX_TOKENS, resolve_team_name
from sports_hedge.facts.identity import canonical_team_id
from sports_hedge.matching.events import EventMatcher
from sports_hedge.normalization.text import normalize_text

KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
FTTS_KICKOFF = KICKOFF + timedelta(minutes=3)
SERIE_A_KALSHI_SERIES = {
    "ticker": "KXSERIEAGAME",
    "title": "Serie A",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "settlement_sources": [{"name": "Opta"}],
}
EPL_KALSHI_SERIES = {
    "ticker": "KXEPLGAME",
    "title": "Premier League",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "settlement_sources": [{"name": "Opta"}],
}

FROCOM = {
    "mb": "27701",
    "game": "KXSERIEAGAME-26SEP20FROCOM",
    "btts": "KXSERIEABTTS-26SEP20FROCOM",
    "total": "KXSERIEATOTAL-26SEP20FROCOM",
    "ftts": "KXSERIEAFTTS-26SEP20FROCOM",
}
JUVATA = {
    "mb": "27701",
    "game": "KXSERIEAGAME-26SEP20JUVATA",
    "btts": "KXSERIEABTTS-26SEP20JUVATA",
    "total": "KXSERIEATOTAL-26SEP20JUVATA",
    "ftts": "KXSERIEAFTTS-26SEP20JUVATA",
}
BOULIV = {
    "mb": "27701",
    "game": "KXEPLGAME-26SEP20BOULIV",
    "btts": "KXEPLBTTS-26SEP20BOULIV",
    "total": "KXEPLTOTAL-26SEP20BOULIV",
    "ftts": "KXEPLFTTS-26SEP20BOULIV",
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


def _cluster(
    *,
    matchbook_home: str,
    matchbook_away: str,
    competition: str,
    kalshi: list[tuple[str, str, str, datetime]],
    matchbook_id: str = "mb-1",
) -> tuple[list[Any], dict[str, int]]:
    return cluster_venue_events(
        matchbook=[
            _venue_event(
                VenueName.MATCHBOOK,
                matchbook_home,
                matchbook_away,
                competition=competition,
                source_event_id=matchbook_id,
            )
        ],
        polymarket=[],
        kalshi=[
            _venue_event(
                VenueName.KALSHI,
                home,
                away,
                competition=competition,
                source_event_id=source_id,
                kickoff=kickoff,
            )
            for source_id, home, away, kickoff in kalshi
        ],
        matcher=EventMatcher(),
        max_event_pairs=16,
    )


def _fixture_from_cluster(cluster: Any, *, competition: str, kickoff: datetime = KICKOFF) -> DiscoveredFixture:
    canonical_id = cluster_canonical_event_id(cluster)
    return DiscoveredFixture(
        source=cluster.anchor.venue,
        source_event_id=cluster.anchor.source_event_id,
        canonical_event_id=canonical_id,
        home_team=cluster.anchor.canonical.home_team,
        away_team=cluster.anchor.canonical.away_team,
        competition=competition,
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
    assert "calcio" in SAFE_TEAM_AFFIX_TOKENS
    assert "bc" in SAFE_TEAM_AFFIX_TOKENS
    assert "turin" not in SAFE_TEAM_AFFIX_TOKENS


def test_observed_raw_similarity_is_below_event_matcher_threshold() -> None:
    assert _raw_ratio("Frosinone Calcio", "Frosinone") < 0.92
    assert _raw_ratio("Como 1907", "Como") < 0.92
    assert _raw_ratio("Juventus Turin", "Juventus") < 0.92
    assert _raw_ratio("Atalanta BC", "Atalanta") < 0.92
    assert _raw_ratio("AFC Bournemouth", "Bournemouth") < 0.92
    assert EventMatcher().threshold == 0.92


def test_curated_identity_converges_observed_family_name_variants() -> None:
    assert resolve_team_name("Frosinone Calcio") == resolve_team_name("Frosinone") == "frosinone"
    assert resolve_team_name("Como 1907") == resolve_team_name("Como") == "como"
    assert resolve_team_name("Juventus Turin") == resolve_team_name("Juventus") == "juventus"
    assert resolve_team_name("Atalanta BC") == resolve_team_name("Atalanta") == "atalanta"
    assert resolve_team_name("Atalanta B.C.") == "atalanta"
    assert resolve_team_name("AFC Bournemouth") == resolve_team_name("Bournemouth") == "bournemouth"
    assert resolve_team_name("Parma Calcio") == resolve_team_name("Parma") == "parma"
    assert canonical_team_id("Frosinone Calcio") == canonical_team_id("Frosinone")
    assert canonical_team_id("Juventus Turin") == canonical_team_id("Juventus")
    assert canonical_team_id("Atalanta BC") == canonical_team_id("Atalanta")
    assert canonical_team_id("AFC Bournemouth") == canonical_team_id("Bournemouth")


def test_ambiguous_and_unknown_remainders_stay_fail_closed() -> None:
    assert resolve_team_name("Unknownville Calcio") == "unknownville calcio"
    assert resolve_team_name("Unknownville BC") == "unknownville bc"
    assert resolve_team_name("Juventus Turin") != resolve_team_name("Torino")
    assert resolve_team_name("Atalanta BC") != resolve_team_name("Atalanta U21")
    assert canonical_team_id("Juventus") != canonical_team_id("Torino")
    assert canonical_team_id("Frosinone") != canonical_team_id("Como")


def _assert_one_cluster(
    clusters: list[Any],
    counts: dict[str, int],
    *,
    matchbook_id: str,
    kalshi_ids: set[str],
) -> Any:
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.matchbook is not None
    assert cluster.matchbook.source_event_id == matchbook_id
    assert {item.source_event_id for item in cluster.kalshi_events} == kalshi_ids
    assert counts["matchbook_kalshi"] == 1
    aliases = cluster_identity_aliases(cluster)
    canonical_id = cluster_canonical_event_id(cluster)
    assert aliases[matchbook_id] == canonical_id
    for source_id in kalshi_ids:
        assert aliases[source_id] == canonical_id
    return cluster


def test_event_matcher_clusters_frosinone_como_kalshi_family_siblings() -> None:
    matcher = EventMatcher()
    result = matcher.match(
        _canonical(
            VenueName.MATCHBOOK,
            "Frosinone",
            "Como",
            competition="Serie A",
            source_event_id=FROCOM["mb"],
        ),
        _canonical(
            VenueName.KALSHI,
            "Frosinone Calcio",
            "Como 1907",
            competition="Serie A",
            source_event_id=FROCOM["ftts"],
            kickoff=FTTS_KICKOFF,
        ),
    )
    assert result.matched is True
    assert result.confidence >= 0.92
    assert "home_team_fuzzy" not in result.reasons
    assert "away_team_fuzzy" not in result.reasons

    clusters, counts = _cluster(
        matchbook_home="Frosinone",
        matchbook_away="Como",
        competition="Serie A",
        matchbook_id=FROCOM["mb"],
        kalshi=[
            (FROCOM["game"], "Frosinone", "Como", KICKOFF),
            (FROCOM["btts"], "Frosinone Calcio", "Como 1907", KICKOFF),
            (FROCOM["total"], "Frosinone Calcio", "Como 1907", KICKOFF),
            (FROCOM["ftts"], "Frosinone Calcio", "Como 1907", FTTS_KICKOFF),
        ],
    )
    _assert_one_cluster(
        clusters,
        counts,
        matchbook_id=FROCOM["mb"],
        kalshi_ids={FROCOM["game"], FROCOM["btts"], FROCOM["total"], FROCOM["ftts"]},
    )


def test_event_matcher_clusters_juventus_atalanta_city_and_bc_variants() -> None:
    matcher = EventMatcher()
    result = matcher.match(
        _canonical(
            VenueName.MATCHBOOK,
            "Juventus",
            "Atalanta",
            competition="Serie A",
            source_event_id=JUVATA["mb"],
        ),
        _canonical(
            VenueName.KALSHI,
            "Juventus Turin",
            "Atalanta BC",
            competition="Serie A",
            source_event_id=JUVATA["ftts"],
            kickoff=FTTS_KICKOFF,
        ),
    )
    assert result.matched is True
    assert result.confidence >= 0.92
    assert "home_team_fuzzy" not in result.reasons
    assert "away_team_fuzzy" not in result.reasons

    clusters, counts = _cluster(
        matchbook_home="Juventus",
        matchbook_away="Atalanta",
        competition="Serie A",
        matchbook_id=JUVATA["mb"],
        kalshi=[
            (JUVATA["game"], "Juventus", "Atalanta", KICKOFF),
            (JUVATA["btts"], "Juventus Turin", "Atalanta BC", KICKOFF),
            (JUVATA["total"], "Juventus Turin", "Atalanta BC", KICKOFF),
            (JUVATA["ftts"], "Juventus Turin", "Atalanta BC", FTTS_KICKOFF),
        ],
    )
    _assert_one_cluster(
        clusters,
        counts,
        matchbook_id=JUVATA["mb"],
        kalshi_ids={JUVATA["game"], JUVATA["btts"], JUVATA["total"], JUVATA["ftts"]},
    )


def test_event_matcher_clusters_afc_bournemouth_liverpool_family_siblings() -> None:
    result = EventMatcher().match(
        _canonical(
            VenueName.MATCHBOOK,
            "AFC Bournemouth",
            "Liverpool",
            competition="Premier League",
            source_event_id=BOULIV["mb"],
        ),
        _canonical(
            VenueName.KALSHI,
            "Bournemouth",
            "Liverpool",
            competition="Premier League",
            source_event_id=BOULIV["ftts"],
            kickoff=FTTS_KICKOFF,
        ),
    )
    assert result.matched is True
    assert result.confidence >= 0.92

    clusters, counts = _cluster(
        matchbook_home="AFC Bournemouth",
        matchbook_away="Liverpool",
        competition="Premier League",
        matchbook_id=BOULIV["mb"],
        kalshi=[
            (BOULIV["game"], "AFC Bournemouth", "Liverpool", KICKOFF),
            (BOULIV["btts"], "Bournemouth", "Liverpool", KICKOFF),
            (BOULIV["total"], "Bournemouth", "Liverpool", KICKOFF),
            (BOULIV["ftts"], "Bournemouth", "Liverpool", FTTS_KICKOFF),
        ],
    )
    _assert_one_cluster(
        clusters,
        counts,
        matchbook_id=BOULIV["mb"],
        kalshi_ids={BOULIV["game"], BOULIV["btts"], BOULIV["total"], BOULIV["ftts"]},
    )


def test_similar_names_with_different_opponent_never_merge() -> None:
    result = EventMatcher().match(
        _canonical(
            VenueName.MATCHBOOK,
            "Frosinone",
            "Como",
            competition="Serie A",
            source_event_id="left",
        ),
        _canonical(
            VenueName.KALSHI,
            "Frosinone Calcio",
            "Lazio",
            competition="Serie A",
            source_event_id="right",
        ),
    )
    assert result.matched is False
    clusters, _ = cluster_venue_events(
        matchbook=[
            _venue_event(
                VenueName.MATCHBOOK,
                "Juventus",
                "Atalanta",
                competition="Serie A",
                source_event_id="mb-juvata",
            )
        ],
        polymarket=[],
        kalshi=[
            _venue_event(
                VenueName.KALSHI,
                "Juventus Turin",
                "Inter",
                competition="Serie A",
                source_event_id="k-juvint",
            )
        ],
        matcher=EventMatcher(),
        max_event_pairs=8,
    )
    assert len(clusters) == 2


def test_city_qualifier_does_not_collapse_juventus_into_torino() -> None:
    result = EventMatcher().match(
        _canonical(
            VenueName.MATCHBOOK,
            "Juventus",
            "Atalanta",
            competition="Serie A",
            source_event_id="left",
        ),
        _canonical(
            VenueName.KALSHI,
            "Torino",
            "Atalanta BC",
            competition="Serie A",
            source_event_id="right",
        ),
    )
    assert result.matched is False
    assert result.reasons == ["curated_team_mismatch"]


def test_same_teams_outside_kickoff_tolerance_never_merge() -> None:
    outside = KICKOFF + timedelta(minutes=6)
    result = EventMatcher().match(
        _canonical(
            VenueName.MATCHBOOK,
            "Frosinone",
            "Como",
            competition="Serie A",
            source_event_id="left",
            kickoff=KICKOFF,
        ),
        _canonical(
            VenueName.KALSHI,
            "Frosinone Calcio",
            "Como 1907",
            competition="Serie A",
            source_event_id="right",
            kickoff=outside,
        ),
    )
    assert result.matched is False
    assert result.reasons == ["kickoff_outside_tolerance"]


@pytest.mark.parametrize(
    ("home_left", "away_left", "home_right", "away_right"),
    [
        ("Frosinone", "Como", "Frosinone U21", "Como"),
        ("Juventus", "Atalanta", "Juventus Turin", "Atalanta Women"),
        ("Juventus", "Atalanta BC", "Juventus", "Atalanta Reserves"),
        ("AFC Bournemouth", "Liverpool", "Bournemouth", "Liverpool U21"),
    ],
)
def test_youth_women_reserve_category_conflict_never_merge(
    home_left: str,
    away_left: str,
    home_right: str,
    away_right: str,
) -> None:
    competition = "Premier League" if "Bournemouth" in home_left else "Serie A"
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


def test_current_state_absorbs_orphan_kalshi_ftts_row() -> None:
    ftts_only, _ = cluster_venue_events(
        matchbook=[],
        polymarket=[],
        kalshi=[
            _venue_event(
                VenueName.KALSHI,
                "Frosinone Calcio",
                "Como 1907",
                competition="Serie A",
                source_event_id=FROCOM["ftts"],
                kickoff=FTTS_KICKOFF,
            )
        ],
        matcher=EventMatcher(),
        max_event_pairs=4,
    )
    unified, _ = _cluster(
        matchbook_home="Frosinone",
        matchbook_away="Como",
        competition="Serie A",
        matchbook_id=FROCOM["mb"],
        kalshi=[
            (FROCOM["game"], "Frosinone", "Como", KICKOFF),
            (FROCOM["btts"], "Frosinone Calcio", "Como 1907", KICKOFF),
            (FROCOM["total"], "Frosinone Calcio", "Como 1907", KICKOFF),
            (FROCOM["ftts"], "Frosinone Calcio", "Como 1907", FTTS_KICKOFF),
        ],
    )
    assert len(ftts_only) == 1
    assert len(unified) == 1
    first_id = cluster_canonical_event_id(ftts_only[0])
    unified_id = cluster_canonical_event_id(unified[0])
    store = FixtureCurrentStateStore()
    scanned = KICKOFF - timedelta(hours=7)
    store.upsert_from_report(
        CollectionReport(
            started_at=scanned,
            completed_at=scanned,
            discovered_fixtures=[_fixture_from_cluster(ftts_only[0], competition="Serie A")],
            fixture_markets={first_id: [_inventory("first_team_to_score", "First Team To Score")]},
            fixture_identity_aliases=cluster_identity_aliases(ftts_only[0]),
            fixture_source_events={
                first_id: [
                    {
                        "venue": VenueName.KALSHI.value,
                        "source_event_id": FROCOM["ftts"],
                        "raw": {"id": FROCOM["ftts"]},
                    }
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
            discovered_fixtures=[_fixture_from_cluster(unified[0], competition="Serie A")],
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
    assert store.resolve_canonical_id(FROCOM["ftts"]) == survivor
    assert store.resolve_canonical_id(FROCOM["mb"]) == survivor
    radar = store.current_radar_rows(scanned)
    assert len(radar) == 1
    assert radar[0].fixture.matchbook_matched is True
    assert radar[0].fixture.kalshi_matched is True
    families = {row.family for row in radar[0].markets}
    assert families >= {"first_team_to_score", "btts"}


async def _assert_collector_unions(
    *,
    matchbook_home: str,
    matchbook_away: str,
    competition: str,
    kalshi_titles: list[tuple[str, str]],
    series: dict[str, Any],
    kalshi_ids: set[str],
) -> None:
    report = await _collect_sibling_fixture(
        matchbook_home=matchbook_home,
        matchbook_away=matchbook_away,
        competition=competition,
        kalshi_titles=kalshi_titles,
        series=series,
    )
    clustered = [
        item for item in report.discovered_fixtures if item.matchbook_matched and item.kalshi_matched
    ]
    assert len(clustered) == 1
    fixture = clustered[0]
    assert fixture.polymarket_matched is False
    observed = {
        str(row["source_event_id"])
        for row in report.fixture_source_events.get(fixture.canonical_event_id, [])
        if str(row.get("venue")) == VenueName.KALSHI.value
    }
    assert observed == kalshi_ids
    aliases = report.fixture_identity_aliases
    canonical_id = fixture.canonical_event_id
    assert aliases["27701"] == canonical_id
    for source_id in kalshi_ids:
        assert aliases[source_id] == canonical_id
    orphans = [
        item
        for item in report.discovered_fixtures
        if item.canonical_event_id != canonical_id and item.kalshi_matched and not item.matchbook_matched
    ]
    assert orphans == []


@pytest.mark.asyncio
async def test_collector_unions_frosinone_kalshi_family_siblings_with_matchbook() -> None:
    await _assert_collector_unions(
        matchbook_home="Frosinone",
        matchbook_away="Como",
        competition="Serie A",
        kalshi_titles=[
            (FROCOM["game"], "Frosinone vs Como"),
            (FROCOM["btts"], "Frosinone Calcio vs Como 1907"),
            (FROCOM["total"], "Frosinone Calcio v Como 1907"),
            (FROCOM["ftts"], "Frosinone Calcio vs Como 1907"),
        ],
        series=SERIE_A_KALSHI_SERIES,
        kalshi_ids={FROCOM["game"], FROCOM["btts"], FROCOM["total"], FROCOM["ftts"]},
    )


@pytest.mark.asyncio
async def test_collector_unions_juventus_kalshi_family_siblings_with_matchbook() -> None:
    await _assert_collector_unions(
        matchbook_home="Juventus",
        matchbook_away="Atalanta",
        competition="Serie A",
        kalshi_titles=[
            (JUVATA["game"], "Juventus vs Atalanta"),
            (JUVATA["btts"], "Juventus Turin vs Atalanta BC"),
            (JUVATA["total"], "Juventus Turin v Atalanta BC"),
            (JUVATA["ftts"], "Juventus Turin vs Atalanta BC"),
        ],
        series=SERIE_A_KALSHI_SERIES,
        kalshi_ids={JUVATA["game"], JUVATA["btts"], JUVATA["total"], JUVATA["ftts"]},
    )


@pytest.mark.asyncio
async def test_collector_unions_bournemouth_kalshi_family_siblings_with_matchbook() -> None:
    await _assert_collector_unions(
        matchbook_home="AFC Bournemouth",
        matchbook_away="Liverpool",
        competition="Premier League",
        kalshi_titles=[
            (BOULIV["game"], "AFC Bournemouth vs Liverpool"),
            (BOULIV["btts"], "Bournemouth vs Liverpool"),
            (BOULIV["total"], "Bournemouth v Liverpool"),
            (BOULIV["ftts"], "Bournemouth vs Liverpool"),
        ],
        series=EPL_KALSHI_SERIES,
        kalshi_ids={BOULIV["game"], BOULIV["btts"], BOULIV["total"], BOULIV["ftts"]},
    )
