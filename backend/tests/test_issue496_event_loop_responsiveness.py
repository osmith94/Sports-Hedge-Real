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
