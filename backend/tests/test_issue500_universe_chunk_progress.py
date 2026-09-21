"""#500 UNIVERSE identity throughput across bounded chunks.

Owner-live soak after #499: API stayed responsive but UNIVERSE lingered around
16/761 evaluated after ~40 minutes. Root cause on 9c25a5c:

* unresolved Polymarket + empty senior squad fingerprints explode same-kickoff
  candidate generation (#468 blocks do not prune senior-v-senior);
* candidates were ordered global cross-venue-first, so almost every node stayed
  in ``unscored_identity_nodes`` until the entire pair list finished;
* truncated finalize therefore fail-closed to singletons and published no
  useful fixture state;
* ``generation_work_used_s`` is cumulative generation work; the 150s figure is
  the chunk wall. ``_pause_universe_generation`` is never called from the
  worker path.

Data class: synthetic/fixture events. Not live, historical, or modelled quotes.
PAPER / read-only. EventMatcher thresholds and vetoes are unchanged.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime, timedelta

import pytest
from test_issue496_event_loop_responsiveness import _probe_gaps

from sports_hedge.application.collector import (
    MarketEvaluationState,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.fixture_clusters import (
    CANDIDATE_ORDER_VERSION,
    ClusterPass,
    VenueEvent,
    build_indexed_candidates,
    cluster_canonical_event_id,
    cluster_member_keyset,
    cluster_venue_events,
)
from sports_hedge.application.universe_identity_cache import GenerationIdentityCache
from sports_hedge.config import Settings
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.events import EventMatcher

KICKOFF = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)
COMPETITIONS = (
    "Premier League",
    "Championship",
    "La Liga",
    "Serie A",
    "Bundesliga",
    "Ligue 1",
    "Eredivisie",
    "Primeira Liga",
    "Copa del Rey",
    "MLS",
)
PREFIXES = (
    "Ajax",
    "Benfica",
    "Celtic",
    "Dinamo",
    "Everton",
    "Fiorentina",
    "Genoa",
    "Hamburg",
    "Inter",
    "Juventus",
    "Köln",
    "Lazio",
    "Monaco",
    "Napoli",
    "Olympiacos",
    "Porto",
    "QPR",
    "Rangers",
    "Sevilla",
    "Torino",
    "Udinese",
    "Valencia",
    "Wolves",
    "Xerez",
    "Young Boys",
    "Zurich",
)


def _event(
    venue: VenueName,
    source_event_id: str,
    *,
    home: str,
    away: str,
    competition: str,
    kickoff: datetime = KICKOFF,
) -> VenueEvent:
    canonical = CanonicalEvent(
        sport="football",
        competition=competition,
        home_team=home,
        away_team=away,
        kickoff_utc=kickoff,
        source_venue=venue,
        source_event_id=source_event_id,
    )
    return VenueEvent(
        venue=venue,
        raw={"id": source_event_id, "title": f"{home} vs {away}"},
        canonical=canonical,
        source_event_id=source_event_id,
    )


def _club(index: int, *, away: bool = False) -> str:
    prefix = PREFIXES[(index + (13 if away else 0)) % len(PREFIXES)]
    role = "Wanderers" if away else "Athletic"
    return f"{prefix} {role} {index}"


def _dense_unresolved(
    fixture_count: int,
    *,
    stagger: bool = True,
    unresolved_pm: bool = True,
) -> tuple[list[VenueEvent], list[VenueEvent], list[VenueEvent]]:
    matchbook: list[VenueEvent] = []
    polymarket: list[VenueEvent] = []
    kalshi: list[VenueEvent] = []
    for index in range(fixture_count):
        home = _club(index)
        away = _club(index, away=True)
        competition = COMPETITIONS[index % len(COMPETITIONS)]
        kickoff = KICKOFF
        if stagger:
            kickoff = KICKOFF + timedelta(minutes=(index % 40) * 5, days=index // 160)
        kwargs = {
            "home": home,
            "away": away,
            "competition": competition,
            "kickoff": kickoff,
        }
        matchbook.append(_event(VenueName.MATCHBOOK, f"mb-{index}", **kwargs))
        polymarket.append(
            _event(
                VenueName.POLYMARKET,
                f"pm-{index}",
                home=home,
                away=away,
                competition="" if unresolved_pm else competition,
                kickoff=kickoff,
            )
        )
        kalshi.append(_event(VenueName.KALSHI, f"k-{index}", **kwargs))
    return matchbook, polymarket, kalshi


def _membership(clusters: list) -> dict[str, frozenset[tuple[str, str]]]:
    return {
        cluster_canonical_event_id(cluster): cluster_member_keyset(cluster)
        for cluster in clusters
        if cluster.venue_count >= 2
    }


def _run_chunk(
    matchbook,
    polymarket,
    kalshi,
    cache: GenerationIdentityCache,
    *,
    pair_budget: int,
) -> ClusterPass:
    cluster_pass = ClusterPass(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=10_000_000,
        identity_cache=cache,
        defer_candidate_build=True,
    )
    return cluster_pass


async def _cooperative_chunk(
    matchbook,
    polymarket,
    kalshi,
    cache: GenerationIdentityCache,
    *,
    pair_budget: int,
) -> tuple[ClusterPass, list, dict]:
    cluster_pass = _run_chunk(
        matchbook, polymarket, kalshi, cache, pair_budget=pair_budget
    )
    await cluster_pass.load_candidates_cooperative()
    for scored, (left, right) in enumerate(cluster_pass.pairs(), start=1):
        cluster_pass.consider(left, right)
        if scored >= pair_budget:
            break
        if scored % 32 == 0:
            await asyncio.sleep(0)
    clusters, counts = await cluster_pass.finalize_cooperative()
    cluster_pass.checkpoint(cluster_pass.candidate_pairs_considered)
    return cluster_pass, clusters, counts


def test_items_signature_ignores_discovery_order() -> None:
    cache = GenerationIdentityCache()
    matchbook, polymarket, kalshi = _dense_unresolved(12, stagger=True)
    items = [*matchbook, *polymarket, *kalshi]
    reversed_items = list(reversed(items))
    assert cache.items_signature(items) == cache.items_signature(reversed_items)


def test_could_match_index_filter_is_still_superset_of_matcher_hits() -> None:
    matchbook, polymarket, kalshi = _dense_unresolved(80, stagger=True)
    items = [*matchbook, *polymarket, *kalshi]
    matcher = EventMatcher()
    candidates, diagnostics = build_indexed_candidates(
        items,
        kickoff_tolerance=timedelta(minutes=5),
        matcher=matcher,
    )
    candidate_keys = {
        frozenset(
            ((left.venue.value, left.source_event_id), (right.venue.value, right.source_event_id))
        )
        for left, right in candidates
    }
    hits = 0
    for left_index, left in enumerate(items):
        for right in items[left_index + 1 :]:
            if matcher.match(left.canonical, right.canonical).matched:
                hits += 1
                key = frozenset(
                    (
                        (left.venue.value, left.source_event_id),
                        (right.venue.value, right.source_event_id),
                    )
                )
                assert key in candidate_keys
    assert hits >= 80
    assert diagnostics["pairs_rejected_by_could_match"] > 0
    assert diagnostics["candidate_pairs_generated"] < diagnostics["naive_pair_space"]


def test_dense_unresolved_same_kickoff_bucket_is_diagnosed() -> None:
    matchbook, polymarket, kalshi = _dense_unresolved(
        120, stagger=False, unresolved_pm=True
    )
    items = [*matchbook, *polymarket, *kalshi]
    _candidates, diagnostics = build_indexed_candidates(
        items,
        kickoff_tolerance=timedelta(minutes=5),
        matcher=EventMatcher(),
    )
    assert diagnostics["unresolved_competition_events"] == 120
    assert diagnostics["max_sport_bucket_size"] == 360
    assert diagnostics["candidate_order_version"] == CANDIDATE_ORDER_VERSION
    assert diagnostics["candidate_pairs_generated"] < diagnostics["naive_pair_space"]


@pytest.mark.asyncio
async def test_dense_unresolved_chunks_progress_without_redoing_pairs() -> None:
    """~800-fixture unresolved UNIVERSE across bounded chunks must advance."""

    matchbook, polymarket, kalshi = _dense_unresolved(800, stagger=True)
    cache = GenerationIdentityCache()
    cache.bind(500)
    pair_budget = 400
    cursors: list[int] = []
    considered_keys: list[frozenset[tuple[str, str]]] = []
    multi_counts: list[int] = []
    stop = asyncio.Event()
    probe_task = asyncio.create_task(_probe_gaps(stop))
    await asyncio.sleep(0)
    try:
        for chunk in range(4):
            cluster_pass, clusters, _counts = await _cooperative_chunk(
                matchbook, polymarket, kalshi, cache, pair_budget=pair_budget
            )
            diag = cluster_pass.clustering_diagnostics(truncated=True)
            if chunk == 0:
                assert diag["clustering_resume_applied"] is False
            else:
                assert diag["clustering_resume_applied"] is True
                assert diag["clustering_resume_signature_mismatch"] is False
                assert diag["clustering_resume_candidates_reused"] is True
            cursor = cluster_pass.candidate_pairs_considered
            if cursors:
                assert cursor > cursors[-1]
            cursors.append(cursor)
            chunk_keys = {
                frozenset(
                    (
                        (left.venue.value, left.source_event_id),
                        (right.venue.value, right.source_event_id),
                    )
                )
                for left, right in cluster_pass._candidates[
                    cluster_pass._resume_cursor : cursor
                ]
            }
            for previous in considered_keys:
                assert previous.isdisjoint(chunk_keys)
            considered_keys.append(chunk_keys)
            multi_counts.append(sum(1 for item in clusters if item.venue_count >= 2))
            assert diag["unscored_identity_nodes"] < len(cluster_pass.nodes)
        assert multi_counts[-1] > multi_counts[0]
        assert multi_counts[-1] > 0
        assert cursors[-1] == 4 * pair_budget
    finally:
        stop.set()
        gaps = await probe_task
    assert gaps
    assert max(gaps) < 1.0


@pytest.mark.asyncio
async def test_truncated_node_local_order_equals_full_recompute() -> None:
    matchbook, polymarket, kalshi = _dense_unresolved(120, stagger=True)
    cache = GenerationIdentityCache()
    cache.bind(7)
    remaining = None
    last_clusters = []
    while remaining is None or remaining > 0:
        cluster_pass, last_clusters, _counts = await _cooperative_chunk(
            matchbook, polymarket, kalshi, cache, pair_budget=250
        )
        remaining = len(cluster_pass._candidates) - cluster_pass.candidate_pairs_considered
        if remaining <= 0:
            cluster_pass.record_generation_negatives(last_clusters)
            break
    full_clusters, _full_counts = cluster_venue_events(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=10_000_000,
    )
    assert _membership(last_clusters) == _membership(full_clusters)


@pytest.mark.asyncio
async def test_resume_survives_discovery_reorder() -> None:
    matchbook, polymarket, kalshi = _dense_unresolved(60, stagger=True)
    cache = GenerationIdentityCache()
    first, _clusters, _counts = await _cooperative_chunk(
        matchbook, polymarket, kalshi, cache, pair_budget=80
    )
    saved = first.candidate_pairs_considered
    assert saved == 80
    reordered_mb = list(reversed(matchbook))
    reordered_pm = list(reversed(polymarket))
    reordered_k = list(reversed(kalshi))
    second = ClusterPass(
        matchbook=reordered_mb,
        polymarket=reordered_pm,
        kalshi=reordered_k,
        matcher=EventMatcher(),
        max_event_pairs=10_000_000,
        identity_cache=cache,
        defer_candidate_build=True,
    )
    await second.load_candidates_cooperative()
    assert second.resume_diagnostics["clustering_resume_applied"] is True
    assert second.resume_diagnostics["clustering_resume_signature_mismatch"] is False
    assert second._resume_cursor == saved


def test_thresholds_concurrency_and_paper_boundary_unchanged() -> None:
    from sports_hedge.application.collector import DEFAULT_PROVIDER_CONCURRENCY
    from sports_hedge.matching.events import (
        DEFAULT_EVENT_MATCH_THRESHOLD,
        PAPER_EVENT_MATCH_THRESHOLD,
    )

    assert DEFAULT_EVENT_MATCH_THRESHOLD == 0.92
    assert PAPER_EVENT_MATCH_THRESHOLD == 0.80
    assert Settings().paper_event_match_threshold == 0.80
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.MATCHBOOK] == 4
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.KALSHI] == 4
    assert Settings().sports_hedge_mode == "paper"
    assert Settings().sports_hedge_execution_enabled is False
    collector_src = inspect.getsource(ReadOnlyCrossVenueCollector)
    assert "place_order" not in collector_src
    assert "cancel_order" not in collector_src
    from sports_hedge.matching.identity_graph import DEFAULT_ASSIGNMENT_MARGIN

    assert DEFAULT_ASSIGNMENT_MARGIN == 0.03


def test_incomplete_single_venue_stays_leftover_while_clustering_truncated() -> None:
    collector = ReadOnlyCrossVenueCollector.__new__(ReadOnlyCrossVenueCollector)
    collector._op_clustering_truncated = True
    collector._op_unscored_identity_nodes = {(VenueName.MATCHBOOK, "mb-1")}
    matchbook, polymarket, kalshi = _dense_unresolved(1, stagger=False)
    cluster_pass = ClusterPass(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=8,
    )
    clusters, _counts = cluster_pass.finalize()
    collector._op_unscored_identity_nodes = set(cluster_pass.nodes)
    assert collector._cluster_identity_incomplete(clusters[0]) is True
    leftover = MarketEvaluationState.NOT_EVALUATED_SCAN_DEADLINE.value
    assert leftover == "not_evaluated_scan_deadline"
