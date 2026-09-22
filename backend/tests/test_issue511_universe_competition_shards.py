"""#511 competition shards so one dense bucket cannot stall UNIVERSE identity.

Data class: synthetic/fixture events. Not live, historical, or modelled quotes.
PAPER / read-only. EventMatcher thresholds, assignment margin and provider
concurrency are unchanged.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta

import pytest

from sports_hedge.application.collector import (
    DEFAULT_PROVIDER_CONCURRENCY,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.fixture_clusters import (
    VenueEvent,
    build_indexed_candidates,
    cluster_member_keyset,
    cluster_venue_events,
)
from sports_hedge.application.target_competitions import TARGET_COMPETITIONS
from sports_hedge.application.universe_identity_cache import GenerationIdentityCache
from sports_hedge.application.universe_identity_shards import (
    UNRESOLVED_COMPETITION,
    cluster_events_sharded,
    provenance_for_event,
)
from sports_hedge.config import Settings
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.events import (
    DEFAULT_EVENT_MATCH_THRESHOLD,
    PAPER_EVENT_MATCH_THRESHOLD,
    EventMatcher,
)
from sports_hedge.matching.identity_graph import DEFAULT_ASSIGNMENT_MARGIN

KICKOFF = datetime(2026, 10, 4, 15, 0, tzinfo=UTC)
FAR_KICKOFF = datetime(2031, 1, 2, 18, 0, tzinfo=UTC)


def _event(
    venue: VenueName,
    source_event_id: str,
    *,
    home: str,
    away: str,
    competition: str,
    kickoff: datetime = KICKOFF,
    sport: str = "football",
    raw: dict | None = None,
) -> VenueEvent:
    canonical = CanonicalEvent(
        sport=sport,
        competition=competition,
        home_team=home,
        away_team=away,
        kickoff_utc=kickoff,
        source_venue=venue,
        source_event_id=source_event_id,
    )
    return VenueEvent(
        venue=venue,
        raw=raw if raw is not None else {"id": source_event_id, "title": f"{home} vs {away}"},
        canonical=canonical,
        source_event_id=source_event_id,
    )


def _competition_fixtures(
    competitions: list,
    *,
    per_competition: int = 1,
    same_kickoff: bool = False,
) -> tuple[list[VenueEvent], list[VenueEvent], list[VenueEvent]]:
    matchbook: list[VenueEvent] = []
    polymarket: list[VenueEvent] = []
    kalshi: list[VenueEvent] = []
    # Distinct stems so a hot same-kickoff shard does not share a 3-letter token.
    stems = (
        "alpha",
        "bravo",
        "charlie",
        "delta",
        "echo",
        "foxtrot",
        "golf",
        "hotel",
        "india",
        "juliet",
        "kilo",
        "lima",
        "mike",
        "november",
        "oscar",
        "papa",
        "quebec",
        "romeo",
        "sierra",
        "tango",
        "uniform",
        "victor",
        "whiskey",
        "xray",
        "yankee",
        "zulu",
    )
    for comp_index, competition in enumerate(competitions):
        code = competition.code.value
        label = competition.aliases[0]
        for fixture_index in range(per_competition):
            if per_competition == 1:
                home = f"{code} home {fixture_index} united"
                away = f"{code} away {fixture_index} rovers"
            else:
                stem = stems[fixture_index % len(stems)]
                home = f"{stem}{fixture_index}aa"
                away = f"{stem}{fixture_index}bb"
            kickoff = KICKOFF if same_kickoff else KICKOFF + timedelta(days=comp_index)
            kwargs = {
                "home": home,
                "away": away,
                "competition": label,
                "kickoff": kickoff,
                "sport": "football",
            }
            suffix = f"{code}-{fixture_index}"
            matchbook.append(_event(VenueName.MATCHBOOK, f"mb-{suffix}", **kwargs))
            polymarket.append(_event(VenueName.POLYMARKET, f"pm-{suffix}", **kwargs))
            kalshi.append(_event(VenueName.KALSHI, f"k-{suffix}", **kwargs))
    return matchbook, polymarket, kalshi


def _dense_unresolved(count: int) -> tuple[list[VenueEvent], list[VenueEvent], list[VenueEvent]]:
    matchbook: list[VenueEvent] = []
    polymarket: list[VenueEvent] = []
    kalshi: list[VenueEvent] = []
    for index in range(count):
        home = f"Unresolved home {index} athletic"
        away = f"Unresolved away {index} wanderers"
        kwargs = {
            "home": home,
            "away": away,
            "competition": "",
            "kickoff": FAR_KICKOFF,
            "raw": {"id": f"raw-{index}", "title": f"{home} vs {away}"},
        }
        matchbook.append(_event(VenueName.MATCHBOOK, f"umb-{index}", **kwargs))
        polymarket.append(_event(VenueName.POLYMARKET, f"upm-{index}", **kwargs))
        kalshi.append(_event(VenueName.KALSHI, f"uk-{index}", **kwargs))
    return matchbook, polymarket, kalshi


def _membership(clusters: list) -> set[frozenset[tuple[str, str]]]:
    return {cluster_member_keyset(cluster) for cluster in clusters if cluster.venue_count >= 2}


def test_exact_series_provenance_scopes_sparse_polymarket_and_kalshi_labels() -> None:
    code, source = provenance_for_event(
        _event(
            VenueName.POLYMARKET,
            "pm-epl",
            home="Arsenal",
            away="Chelsea",
            competition="",
            raw={"id": "pm-epl", "series_id": "10188", "title": "Arsenal vs Chelsea"},
        )
    )
    assert (code, source) == ("premier_league", "polymarket_series")
    code, source = provenance_for_event(
        _event(
            VenueName.KALSHI,
            "k-epl",
            home="Arsenal",
            away="Chelsea",
            competition="",
            raw={"event_ticker": "KXEPLGAME-26OCT04ARSCHE", "title": "Arsenal vs Chelsea"},
        )
    )
    assert (code, source) == ("premier_league", "kalshi_ticker")
    code, source = provenance_for_event(
        _event(
            VenueName.POLYMARKET,
            "pm-unknown",
            home="Arsenal",
            away="Chelsea",
            competition="",
            raw={"id": "pm-unknown", "title": "Arsenal vs Chelsea"},
        )
    )
    assert code is None
    assert source == "unresolved"


@pytest.mark.asyncio
async def test_series_provenance_matches_its_competition_and_not_a_neighbour() -> None:
    kickoff = KICKOFF
    matchbook = [
        _event(
            VenueName.MATCHBOOK,
            "mb-epl",
            home="Arsenal",
            away="Chelsea",
            competition="Premier League",
            kickoff=kickoff,
        ),
        _event(
            VenueName.MATCHBOOK,
            "mb-liga",
            home="Barcelona",
            away="Sevilla",
            competition="La Liga",
            kickoff=kickoff,
        ),
    ]
    polymarket = [
        _event(
            VenueName.POLYMARKET,
            "pm-epl",
            home="Arsenal",
            away="Chelsea",
            competition="",
            kickoff=kickoff,
            raw={"id": "pm-epl", "series_id": "10188", "title": "Arsenal vs Chelsea"},
        )
    ]
    kalshi = [
        _event(
            VenueName.KALSHI,
            "k-epl",
            home="Arsenal",
            away="Chelsea",
            competition="",
            kickoff=kickoff,
            raw={"event_ticker": "KXEPLGAME-26OCT04ARSCHE"},
        )
    ]
    clusters, _counts, truncated, diagnostics, _unscored = await cluster_events_sharded(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=10_000,
        identity_cache=GenerationIdentityCache(),
    )
    assert truncated is False
    assert diagnostics["provenance_counts"]["polymarket_series"] == 1
    assert diagnostics["provenance_counts"]["kalshi_ticker"] == 1
    multi = _membership(clusters)
    epl = frozenset({("matchbook", "mb-epl"), ("polymarket", "pm-epl"), ("kalshi", "k-epl")})
    assert epl in multi
    assert all(
        ("polymarket", "pm-epl") not in group or ("matchbook", "mb-liga") not in group
        for group in multi
    )
    assert polymarket[0].canonical.competition == "English Premier League"


@pytest.mark.asyncio
async def test_all_sports_progress_while_dense_unresolved_shard_is_capped() -> None:
    assert len(TARGET_COMPETITIONS) >= 30
    matchbook, polymarket, kalshi = _competition_fixtures(list(TARGET_COMPETITIONS))
    dense_mb, dense_pm, dense_k = _dense_unresolved(40)
    completed: list[str] = []
    cache = GenerationIdentityCache()
    cache.bind(511)
    clusters, _counts, truncated, diagnostics, unscored = await cluster_events_sharded(
        matchbook=[*matchbook, *dense_mb],
        polymarket=[*polymarket, *dense_pm],
        kalshi=[*kalshi, *dense_k],
        matcher=EventMatcher(),
        max_event_pairs=10_000_000,
        identity_cache=cache,
        hot_pair_budget=0,
        on_shard_complete=lambda shard_id, _clusters: completed.append(shard_id),
    )
    assert truncated is True
    assert diagnostics["identity_shards_completed"] >= 30
    assert diagnostics["identity_hot_shards"] >= 1
    assert diagnostics["blocking_shard_key"] is not None
    assert UNRESOLVED_COMPETITION in str(diagnostics["blocking_shard_key"])
    assert diagnostics["blocking_shard_status"] == "not_started"
    assert diagnostics["global_resume_invalidated_by_discovery"] is False
    assert len(_membership(clusters)) >= 30
    assert completed
    assert all(UNRESOLVED_COMPETITION not in shard_id for shard_id in completed)
    assert any(
        item.source_event_id.startswith("umb-")
        for item in (event for cluster in clusters for event in (cluster.matchbook_events or []))
    )
    assert any((item.venue, item.source_event_id) in unscored for item in dense_mb)
    premier = next(
        cluster
        for cluster in clusters
        if any(event.source_event_id == "mb-premier_league-0" for event in cluster.matchbook_events)
    )
    premier_event = premier.matchbook_events[0]
    assert (premier_event.venue, premier_event.source_event_id) not in unscored


@pytest.mark.asyncio
async def test_shard_resume_is_local_when_another_competition_changes() -> None:
    competitions = list(TARGET_COMPETITIONS[:8])
    matchbook, polymarket, kalshi = _competition_fixtures(competitions)
    cache = GenerationIdentityCache()
    cache.bind(511)
    await cluster_events_sharded(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=10_000,
        identity_cache=cache,
    )
    la_liga_index = next(
        index for index, item in enumerate(competitions) if item.code.value == "la_liga"
    )
    changed = _event(
        VenueName.MATCHBOOK,
        "mb-la_liga-0",
        home="la_liga home 0 united renamed",
        away="la_liga away 0 rovers",
        competition="la liga",
        kickoff=KICKOFF + timedelta(days=la_liga_index),
        sport="football",
    )
    matchbook = [changed if item.source_event_id == "mb-la_liga-0" else item for item in matchbook]
    _clusters, _counts, _truncated, diagnostics, _unscored = await cluster_events_sharded(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=10_000,
        identity_cache=cache,
    )
    rows = {row["shard_id"]: row for row in diagnostics["identity_shards"]}
    assert rows["football/premier_league"]["resume_reused"] is True
    assert rows["football/la_liga"]["resume_invalidated"] is True
    assert diagnostics["discovery_snapshot_changed"] is True
    assert diagnostics["global_resume_invalidated_by_discovery"] is False
    assert diagnostics["shard_resumes_kept_across_discovery_change"] is True


@pytest.mark.asyncio
async def test_sharded_result_matches_full_recompute_oracle() -> None:
    matchbook, polymarket, kalshi = _competition_fixtures(list(TARGET_COMPETITIONS[:8]))
    sharded, _counts, truncated, _diagnostics, _unscored = await cluster_events_sharded(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=10_000,
        identity_cache=GenerationIdentityCache(),
    )
    full, _full_counts = cluster_venue_events(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=10_000,
    )
    assert truncated is False
    assert _membership(sharded) == _membership(full)


@pytest.mark.asyncio
async def test_hot_secondary_block_preserves_matcher_hits() -> None:
    competitions = [TARGET_COMPETITIONS[0]]
    matchbook, polymarket, kalshi = _competition_fixtures(
        competitions, per_competition=22, same_kickoff=True
    )
    items = [*matchbook, *polymarket, *kalshi]
    assert len(items) >= 64
    matcher = EventMatcher()
    blocked, blocked_diag = build_indexed_candidates(
        items,
        kickoff_tolerance=timedelta(minutes=5),
        matcher=matcher,
        secondary_block=True,
    )
    blocked_keys = {
        frozenset(
            ((left.venue.value, left.source_event_id), (right.venue.value, right.source_event_id))
        )
        for left, right in blocked
    }
    hits = 0
    for left_index, left in enumerate(items):
        for right in items[left_index + 1 :]:
            if not matcher.match(left.canonical, right.canonical).matched:
                continue
            hits += 1
            key = frozenset(
                (
                    (left.venue.value, left.source_event_id),
                    (right.venue.value, right.source_event_id),
                )
            )
            assert key in blocked_keys
    assert hits >= 22
    assert blocked_diag["secondary_block"] is True
    assert blocked_diag["pairs_blocked_before_could_match"] > 0
    sharded, _counts, truncated, diagnostics, _unscored = await cluster_events_sharded(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=matcher,
        max_event_pairs=10_000_000,
        identity_cache=GenerationIdentityCache(),
    )
    full, _full_counts = cluster_venue_events(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=matcher,
        max_event_pairs=10_000_000,
    )
    assert truncated is False
    assert diagnostics["identity_hot_shards"] == 1
    assert _membership(sharded) == _membership(full)


def test_thresholds_concurrency_and_paper_boundary_unchanged() -> None:
    assert DEFAULT_EVENT_MATCH_THRESHOLD == 0.92
    assert PAPER_EVENT_MATCH_THRESHOLD == 0.80
    assert DEFAULT_ASSIGNMENT_MARGIN == 0.03
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.MATCHBOOK] == 4
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.POLYMARKET] == 8
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.KALSHI] == 4
    assert Settings().sports_hedge_mode == "paper"
    assert Settings().sports_hedge_execution_enabled is False
    source = inspect.getsource(ReadOnlyCrossVenueCollector)
    assert "place_order" not in source
    shard_source = inspect.getsource(
        __import__(
            "sports_hedge.application.universe_identity_shards",
            fromlist=["cluster_events_sharded"],
        )
    )
    assert "place_order" not in shard_source


@pytest.mark.asyncio
async def test_collector_reports_blocking_shard_diagnostics() -> None:
    collector = ReadOnlyCrossVenueCollector.__new__(ReadOnlyCrossVenueCollector)
    collector.event_matcher = EventMatcher()
    collector._identity_cache = GenerationIdentityCache()
    collector._identity_cache.bind(511)
    collector._incremental_cache = None
    collector._op_deadline = None
    collector._op_soft_deadline = None
    collector._op_partial_clusters = []
    collector._op_clustering_truncated = False
    collector._op_unscored_identity_nodes = set()
    matchbook, polymarket, kalshi = _competition_fixtures(list(TARGET_COMPETITIONS[:4]))
    clusters, _counts, truncated, diagnostics = await collector._cluster_venue_events_cooperative(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        max_event_pairs=10_000,
    )
    assert truncated is False
    assert diagnostics["identity_shards_completed"] == 4
    assert diagnostics["blocking_shard_key"] is None
    assert diagnostics["global_resume_invalidated_by_discovery"] is False
    assert "events_by_venue_competition" in diagnostics
    assert collector._op_clustering_truncated is False
    assert collector._op_unscored_identity_nodes == set()
    assert len(_membership(clusters)) == 4
