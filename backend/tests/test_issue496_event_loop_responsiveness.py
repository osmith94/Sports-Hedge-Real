"""#496 Event-loop liveness during UNIVERSE identity / cancel recovery.

Owner-live evidence: port 8000 stayed LISTENING while /build-info, Treasury,
and trade GETs hung, CLOSE_WAIT sockets accumulated, and scanner logs showed
``scan_cancelled_assembling_partial`` then ``scan_cancelled_partial_identity``.
Root cause: ClusterPass.__init__ / finalize() / sync cluster_venue_events
recovery monopolised the asyncio loop.

Data class: synthetic/fixture events. Not live, historical, or modelled quotes.
PAPER / read-only. EventMatcher thresholds are unchanged.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from datetime import UTC, datetime, timedelta

import pytest

from sports_hedge.application.collector import (
    CLUSTER_COMPARISON_YIELD_EVERY,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.fixture_clusters import (
    ClusterPass,
    VenueEvent,
    build_indexed_candidates,
    build_indexed_candidates_cooperative,
    cluster_venue_events,
)
from sports_hedge.application.hot_market_relationships import relationships_from_fixture_markets
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.events import EventMatcher
from test_collector_scan_throughput import SyntheticUniverse, _collector, _scan
from test_issue466_universe_indexed_clustering import _large_universe_items


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
TEAMS = [
    "Arsenal",
    "Chelsea",
    "Liverpool",
    "Manchester City",
    "Tottenham Hotspur",
    "Newcastle United",
    "Aston Villa",
    "Brighton",
    "West Ham",
    "Everton",
    "Fulham",
    "Brentford",
    "Real Madrid",
    "Barcelona",
    "Juventus",
    "Bayern Munich",
    "PSG",
    "Ajax",
    "Benfica",
    "LAFC",
]


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


def _dense_universe(
    fixture_count: int,
    *,
    unresolved_pm: bool = False,
    stagger: bool = False,
) -> tuple[list[VenueEvent], list[VenueEvent], list[VenueEvent]]:
    matchbook: list[VenueEvent] = []
    polymarket: list[VenueEvent] = []
    kalshi: list[VenueEvent] = []
    for index in range(fixture_count):
        home = f"{TEAMS[index % len(TEAMS)]} {index}"
        away = f"{TEAMS[(index + 7) % len(TEAMS)]} {index}"
        competition = COMPETITIONS[index % len(COMPETITIONS)]
        kickoff = KICKOFF
        if stagger:
            kickoff = KICKOFF + timedelta(minutes=(index % 24) * 5, days=index // 80)
        matchbook.append(
            _event(
                VenueName.MATCHBOOK,
                f"mb-{index}",
                home=home,
                away=away,
                competition=competition,
                kickoff=kickoff,
            )
        )
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
        kalshi.append(
            _event(
                VenueName.KALSHI,
                f"k-{index}",
                home=home,
                away=away,
                competition=competition,
                kickoff=kickoff,
            )
        )
    return matchbook, polymarket, kalshi


async def _probe_gaps(stop: asyncio.Event, *, interval: float = 0.01) -> list[float]:
    gaps: list[float] = []
    await asyncio.sleep(0)
    while not stop.is_set():
        started = time.perf_counter()
        await asyncio.sleep(0)
        gaps.append(time.perf_counter() - started)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            continue
    return gaps


@pytest.mark.asyncio
async def test_cooperative_index_matches_sync_and_yields() -> None:
    matchbook, polymarket, kalshi = _dense_universe(80, unresolved_pm=True)
    items = [*matchbook, *polymarket, *kalshi]
    window = timedelta(minutes=5)
    sync_pairs, sync_diag = build_indexed_candidates(items, kickoff_tolerance=window)
    coop_pairs, coop_diag = await build_indexed_candidates_cooperative(
        items, kickoff_tolerance=window, yield_every=64
    )
    assert sync_diag == coop_diag
    assert [(left.source_event_id, right.source_event_id) for left, right in sync_pairs] == [
        (left.source_event_id, right.source_event_id) for left, right in coop_pairs
    ]


@pytest.mark.asyncio
async def test_large_universe_identity_does_not_starve_event_loop() -> None:
    """~1,600-fixture UNIVERSE identity + cancel/finalize must keep the loop alive."""

    matchbook, polymarket, kalshi, _expected = _large_universe_items(1600)
    stop = asyncio.Event()
    probe_task = asyncio.create_task(_probe_gaps(stop))
    await asyncio.sleep(0)

    cluster_pass = ClusterPass(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=10_000_000,
        defer_candidate_build=True,
    )
    await cluster_pass.load_candidates_cooperative()
    for index, (left, right) in enumerate(cluster_pass.pairs()):
        if index % CLUSTER_COMPARISON_YIELD_EVERY == 0:
            await asyncio.sleep(0)
        cluster_pass.consider(left, right)
    clusters, counts = await cluster_pass.finalize_cooperative()
    stop.set()
    gaps = await probe_task

    assert counts["matchbook_polymarket"] >= 40
    assert counts["matchbook_kalshi"] >= 40
    assert len(clusters) >= 40
    assert gaps, "event loop never resumed while identity work ran"
    assert max(gaps) < 0.25, (
        f"event loop starved for {max(gaps):.3f}s during 1600-fixture identity "
        f"(probes={len(gaps)})"
    )


def cluster_canonical_ids(clusters) -> frozenset[str]:
    from sports_hedge.application.fixture_clusters import cluster_canonical_event_id

    return frozenset(cluster_canonical_event_id(cluster) for cluster in clusters)


@pytest.mark.asyncio
async def test_cancel_finalize_of_dense_unresolved_universe_keeps_loop_alive() -> None:
    matchbook, polymarket, kalshi = _dense_universe(400, unresolved_pm=True)
    stop = asyncio.Event()
    probe_task = asyncio.create_task(_probe_gaps(stop))
    await asyncio.sleep(0)

    cluster_pass = ClusterPass(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=10_000_000,
        defer_candidate_build=True,
    )
    await cluster_pass.load_candidates_cooperative(yield_every=256)
    for index, (left, right) in enumerate(cluster_pass.pairs()):
        cluster_pass.consider(left, right)
        if index >= 200:
            break
        if index % CLUSTER_COMPARISON_YIELD_EVERY == 0:
            await asyncio.sleep(0)
    snapshot, _counts = await cluster_pass.finalize_cooperative()
    stop.set()
    gaps = await probe_task
    assert snapshot
    assert gaps
    assert max(gaps) < 0.25, f"event loop starved for {max(gaps):.3f}s during dense finalize"


@pytest.mark.asyncio
async def test_cancelled_hot_scan_still_preserves_known_roster(monkeypatch) -> None:
    universe = SyntheticUniverse(16, latency_s=0)
    collector, repository = _collector(universe)
    try:
        seed = await _scan(collector, max_event_pairs=16, scan_lane=ScanLane.UNIVERSE.value)
        assert len(seed.discovered_fixtures) == 16

        async def _interrupt_clustering(self, *args, **kwargs):
            del self, args, kwargs
            await asyncio.sleep(0)
            raise asyncio.CancelledError

        monkeypatch.setattr(
            ReadOnlyCrossVenueCollector,
            "_cluster_venue_events_cooperative",
            _interrupt_clustering,
        )
        report = await _scan(
            collector,
            max_event_pairs=16,
            scan_lane=ScanLane.HOT.value,
            identity_scope=[item.canonical_event_id for item in seed.discovered_fixtures],
            known_source_events=seed.fixture_source_events,
            hot_market_relationships=relationships_from_fixture_markets(seed.fixture_markets),
        )
        assert report.scan_diagnostics["cancelled"] is True
        assert len(report.discovered_fixtures) == 16
    finally:
        repository.close()


def test_cancel_recovery_does_not_call_sync_cluster_venue_events() -> None:
    recover_src = inspect.getsource(ReadOnlyCrossVenueCollector._recover_clusters_for_cancelled_scan)
    cooperative_src = inspect.getsource(
        ReadOnlyCrossVenueCollector._cluster_venue_events_cooperative
    )
    assert "cluster_venue_events(" not in recover_src
    assert "_cluster_venue_events_cooperative(" in recover_src
    assert "allow_incremental=False" in recover_src
    assert "if index_ready:" in cooperative_src
    assert "defer_candidate_build=True" in cooperative_src
    assert "finalize_cooperative" in cooperative_src
    assert "load_candidates_cooperative" in cooperative_src


@pytest.mark.asyncio
async def test_cooperative_cluster_pass_matches_sync_oracle_on_staggered_universe() -> None:
    matchbook, polymarket, kalshi, expected_cross = _large_universe_items(120)
    oracle, oracle_counts = cluster_venue_events(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=10_000_000,
    )
    cluster_pass = ClusterPass(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=10_000_000,
        defer_candidate_build=True,
    )
    await cluster_pass.load_candidates_cooperative()
    for left, right in cluster_pass.pairs():
        cluster_pass.consider(left, right)
    cooperative, cooperative_counts = await cluster_pass.finalize_cooperative()
    assert cooperative_counts == oracle_counts
    assert cluster_canonical_ids(cooperative) == cluster_canonical_ids(oracle)
    found = {
        (cluster.anchor.canonical.home_team, cluster.anchor.canonical.away_team)
        for cluster in cooperative
        if cluster.venue_count >= 2
    }
    assert expected_cross <= found


def _membership(clusters) -> frozenset[frozenset[tuple[str, str]]]:
    from sports_hedge.application.fixture_clusters import cluster_member_keyset

    return frozenset(cluster_member_keyset(cluster) for cluster in clusters)


def _graph_diag(cluster_pass: ClusterPass) -> dict[str, int]:
    return dict(cluster_pass.graph_diagnostics)


async def _cooperative_clusters(matchbook, polymarket, kalshi, *, limit: int | None = None, cache=None):
    cluster_pass = ClusterPass(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=10_000_000,
        identity_cache=cache,
        defer_candidate_build=True,
    )
    await cluster_pass.load_candidates_cooperative()
    for index, (left, right) in enumerate(cluster_pass.pairs()):
        cluster_pass.consider(left, right)
        if limit is not None and index + 1 >= limit:
            break
    clusters, counts = await cluster_pass.finalize_cooperative()
    return cluster_pass, clusters, counts


def _sync_clusters(matchbook, polymarket, kalshi, *, limit: int | None = None, cache=None):
    cluster_pass = ClusterPass(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=10_000_000,
        identity_cache=cache,
    )
    for index, (left, right) in enumerate(cluster_pass.pairs()):
        cluster_pass.consider(left, right)
        if limit is not None and index + 1 >= limit:
            break
    clusters, counts = cluster_pass.finalize()
    return cluster_pass, clusters, counts


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "label,factory,limit",
    [
        ("staggered_full", lambda: _dense_universe(80, stagger=True), None),
        ("dense_full", lambda: _dense_universe(80), None),
        ("unresolved_full", lambda: _dense_universe(60, unresolved_pm=True), None),
        ("dense_partial", lambda: _dense_universe(80), 250),
        ("unresolved_partial", lambda: _dense_universe(60, unresolved_pm=True), 180),
    ],
)
async def test_cooperative_identity_matches_sync_across_shapes(label, factory, limit) -> None:
    matchbook, polymarket, kalshi = factory()
    sync_pass, sync_clusters, sync_counts = _sync_clusters(
        matchbook, polymarket, kalshi, limit=limit
    )
    coop_pass, coop_clusters, coop_counts = await _cooperative_clusters(
        matchbook, polymarket, kalshi, limit=limit
    )
    assert coop_counts == sync_counts, label
    assert _membership(coop_clusters) == _membership(sync_clusters), label
    assert _graph_diag(coop_pass) == _graph_diag(sync_pass), label
    assert coop_pass.candidate_pairs_considered == sync_pass.candidate_pairs_considered, label


@pytest.mark.asyncio
async def test_resume_cooperative_identity_matches_sync() -> None:
    from sports_hedge.application.universe_identity_cache import GenerationIdentityCache

    matchbook, polymarket, kalshi = _dense_universe(80, stagger=True)
    items = [*matchbook, *polymarket, *kalshi]
    seed_cache = GenerationIdentityCache()
    seed = ClusterPass(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=10_000_000,
        identity_cache=seed_cache,
    )
    for index, (left, right) in enumerate(seed.pairs()):
        seed.consider(left, right)
        if index >= 40:
            break
    seed.checkpoint(seed.candidate_pairs_considered)
    resume = seed_cache.clustering_resume
    assert resume is not None
    assert resume.cursor == 41

    def _fork() -> GenerationIdentityCache:
        forked = GenerationIdentityCache()
        forked.store_clustering_resume(
            items=items,
            cursor=resume.cursor,
            parent=dict(resume.parent),
            match_confidence=dict(resume.match_confidence),
            pair_kinds={key: set(value) for key, value in resume.pair_kinds.items()},
            scored_pairs=list(resume.scored_pairs),
        )
        return forked

    sync_pass, sync_clusters, sync_counts = _sync_clusters(
        matchbook, polymarket, kalshi, cache=_fork()
    )
    coop_pass, coop_clusters, coop_counts = await _cooperative_clusters(
        matchbook, polymarket, kalshi, cache=_fork()
    )
    assert coop_pass._resume_cursor == sync_pass._resume_cursor == 41
    assert coop_counts == sync_counts
    assert _membership(coop_clusters) == _membership(sync_clusters)
    assert _graph_diag(coop_pass) == _graph_diag(sync_pass)


@pytest.mark.asyncio
async def test_cancelled_index_keeps_partial_candidates_not_empty_singletons() -> None:
    matchbook, polymarket, kalshi = _dense_universe(200, unresolved_pm=True)
    cluster_pass = ClusterPass(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=10_000_000,
        defer_candidate_build=True,
    )
    task = asyncio.create_task(cluster_pass.load_candidates_cooperative(yield_every=16))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert cluster_pass._candidates, "cancel during index dropped generated pairs"
    assert cluster_pass.index_diagnostics["candidate_pairs_generated"] == len(cluster_pass._candidates)


@pytest.mark.asyncio
async def test_cancel_during_index_does_not_wipe_generation_resume() -> None:
    from sports_hedge.application.universe_identity_cache import GenerationIdentityCache

    matchbook, polymarket, kalshi = _dense_universe(80, stagger=True)
    cache = GenerationIdentityCache()
    seed = ClusterPass(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=10_000_000,
        identity_cache=cache,
    )
    for index, (left, right) in enumerate(seed.pairs()):
        seed.consider(left, right)
        if index >= 40:
            break
    seed.checkpoint(seed.candidate_pairs_considered)
    resume = cache.clustering_resume
    assert resume is not None
    saved_cursor = resume.cursor
    saved_scored = list(resume.scored_pairs)
    assert saved_cursor == 41
    assert saved_scored

    cluster_pass = ClusterPass(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=10_000_000,
        identity_cache=cache,
        defer_candidate_build=True,
    )
    task = asyncio.create_task(cluster_pass.load_candidates_cooperative(yield_every=16))
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    kept = cache.clustering_resume
    assert kept is not None
    assert kept.cursor == saved_cursor
    assert list(kept.scored_pairs) == saved_scored
    assert cluster_pass.scored_pairs == []
    replay = GenerationIdentityCache()
    replay.store_clustering_resume(
        items=[*matchbook, *polymarket, *kalshi],
        cursor=saved_cursor,
        parent=dict(kept.parent),
        match_confidence=dict(kept.match_confidence),
        pair_kinds={key: set(value) for key, value in kept.pair_kinds.items()},
        scored_pairs=list(saved_scored),
    )
    sync_pass, _sync_clusters_out, _sync_counts = _sync_clusters(
        matchbook, polymarket, kalshi, cache=replay
    )
    assert sync_pass._resume_cursor == saved_cursor
    assert sync_pass.scored_pairs[: len(saved_scored)] == saved_scored


@pytest.mark.asyncio
async def test_cancel_finalize_does_not_commit_incremental_or_negatives() -> None:
    from sports_hedge.application.universe_identity_cache import CrossGenerationIdentityCache

    matchbook, polymarket, kalshi = _dense_universe(40, stagger=True)
    incremental = CrossGenerationIdentityCache()
    cluster_venue_events(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=10_000_000,
        incremental_cache=incremental,
    )
    before_events = dict(incremental.events)
    before_pairs = dict(incremental.pairs)
    cluster_pass = ClusterPass(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=10_000_000,
        incremental_cache=incremental,
        defer_candidate_build=True,
    )
    await cluster_pass.load_candidates_cooperative()
    for index, (left, right) in enumerate(cluster_pass.pairs()):
        cluster_pass.consider(left, right)
        if index >= 8:
            break
    cluster_pass.checkpoint(cluster_pass.candidate_pairs_considered)
    await cluster_pass.finalize_cooperative()
    assert incremental.events == before_events
    assert incremental.pairs == before_pairs


@pytest.mark.asyncio
async def test_universe_cancel_after_index_finalizes_once_uses_partial(
    monkeypatch,
) -> None:
    from sports_hedge.application.universe_identity_cache import (
        get_cross_generation_identity_cache,
        reset_universe_identity_cache,
    )

    reset_universe_identity_cache()
    finalize_calls = {"n": 0}
    original = ClusterPass.finalize_cooperative

    async def counted(self, *args, **kwargs):
        finalize_calls["n"] += 1
        return await original(self, *args, **kwargs)

    hang = asyncio.Event()
    ready = asyncio.Event()
    original_load = ClusterPass.load_candidates_cooperative

    async def load_then_hang(self, *args, **kwargs):
        await original_load(self, *args, **kwargs)
        ready.set()
        await hang.wait()

    monkeypatch.setattr(ClusterPass, "finalize_cooperative", counted)
    monkeypatch.setattr(ClusterPass, "load_candidates_cooperative", load_then_hang)

    universe = SyntheticUniverse(8, latency_s=0)
    collector, repository = _collector(universe)
    try:
        task = asyncio.create_task(
            _scan(
                collector,
                max_event_pairs=8,
                scan_lane=ScanLane.UNIVERSE.value,
                universe_generation_id=9,
            )
        )
        await asyncio.wait_for(ready.wait(), timeout=5)
        task.cancel()
        report = await task
        assert report.scan_diagnostics["cancelled"] is True
        assert finalize_calls["n"] == 1
        assert report.discovered_fixtures
        assert get_cross_generation_identity_cache().events == {}
        assert collector._identity_cache is not None
        assert collector._identity_cache.generation_id == 9
    finally:
        hang.set()
        repository.close()
        reset_universe_identity_cache()


@pytest.mark.asyncio
async def test_last_resort_cancel_recovery_does_not_commit_incremental(
    monkeypatch,
) -> None:
    from sports_hedge.application.universe_identity_cache import (
        get_cross_generation_identity_cache,
        reset_universe_identity_cache,
    )

    reset_universe_identity_cache()
    original = ReadOnlyCrossVenueCollector._cluster_venue_events_cooperative
    calls: list[bool] = []

    async def first_raises(self, *args, **kwargs):
        calls.append(bool(kwargs.get("allow_incremental", True)))
        if len(calls) == 1:
            raise asyncio.CancelledError
        return await original(self, *args, **kwargs)

    monkeypatch.setattr(
        ReadOnlyCrossVenueCollector,
        "_cluster_venue_events_cooperative",
        first_raises,
    )
    universe = SyntheticUniverse(8, latency_s=0)
    collector, repository = _collector(universe)
    try:
        report = await _scan(
            collector,
            max_event_pairs=8,
            scan_lane=ScanLane.UNIVERSE.value,
            universe_generation_id=11,
        )
        assert report.scan_diagnostics["cancelled"] is True
        assert report.discovered_fixtures
        assert calls == [True, False]
        assert get_cross_generation_identity_cache().events == {}
        assert get_cross_generation_identity_cache().pairs == {}
        assert collector._identity_cache is not None
        assert collector._identity_cache.generation_id == 11
    finally:
        repository.close()
        reset_universe_identity_cache()


def test_thresholds_concurrency_and_paper_boundary_unchanged() -> None:
    from sports_hedge.application.collector import DEFAULT_PROVIDER_CONCURRENCY
    from sports_hedge.config import Settings
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


@pytest.mark.asyncio
async def test_checkpoint_and_negatives_ignore_rebound_generation() -> None:
    from sports_hedge.application.universe_identity_cache import GenerationIdentityCache

    matchbook, polymarket, kalshi = _dense_universe(12, stagger=True)
    cache = GenerationIdentityCache()
    cache.bind(9)
    cluster_pass = ClusterPass(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=10_000_000,
        identity_cache=cache,
    )
    for index, (left, right) in enumerate(cluster_pass.pairs()):
        cluster_pass.consider(left, right)
        if index >= 4:
            break
    cache.bind(10)
    cluster_pass.checkpoint(cluster_pass.candidate_pairs_considered)
    clusters, _counts = cluster_pass.finalize()
    cluster_pass.record_generation_negatives(clusters)
    assert cache.generation_id == 10
    assert cache.clustering_resume is None
    assert cache.no_cross_venue == {}
