"""#466 UNIVERSE indexed clustering and cross-venue-first evaluation.

Data class: fixture/demo synthetic events. Not live, historical, or modelled quotes.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from sports_hedge.application.collector import (
    MarketEvaluationState,
    ReadOnlyCrossVenueCollector,
    clustering_market_eval_reserve_seconds,
)
from sports_hedge.application.fixture_clusters import (
    ClusterPass,
    VenueEvent,
    build_indexed_candidates,
    cluster_venue_events,
    naive_pair_space,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.application.universe_identity_cache import (
    CachedEventIdentity,
    GenerationIdentityCache,
    get_cross_generation_identity_cache,
    get_universe_identity_cache,
    reset_universe_identity_cache,
)
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.events import EventMatcher
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from registered_kalshi import FakeKalshiBTTS
from test_issue_147_market_evaluation_state import THREE_LEAGUE_FIXTURES
from test_read_only_collector import FakeMatchbook, FakePolymarket, KICKOFF as COLLECTOR_KICKOFF
from venue_cost_helpers import matchbook_kalshi_costs


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


def _event(
    venue: VenueName,
    source_event_id: str,
    *,
    home: str,
    away: str,
    competition: str = "Premier League",
    kickoff: datetime = KICKOFF,
    sport: str = "football",
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
        raw={"id": source_event_id, "title": f"{home} vs {away}"},
        canonical=canonical,
        source_event_id=source_event_id,
    )


def test_calatayud_baztan_pm_kalshi_clusters_without_matchbook() -> None:
    matcher = EventMatcher()
    assert matcher.threshold == 0.92
    polymarket = [
        _event(
            VenueName.POLYMARKET,
            "pm-calatayud",
            home="Atlético Calatayud CF",
            away="CD Baztán",
            competition="Copa del Rey",
        )
    ]
    kalshi = [
        _event(
            VenueName.KALSHI,
            "k-calatayud",
            home="Atletico Calatayud",
            away="CD Baztan",
            competition="Copa del Rey",
        )
    ]
    pm = polymarket[0].canonical
    k = kalshi[0].canonical
    result = matcher.match(pm, k)
    assert result.matched is True
    assert result.confidence >= 0.92
    clusters, counts = cluster_venue_events(
        matchbook=[],
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=matcher,
        max_event_pairs=8,
    )
    assert counts["polymarket_kalshi"] == 1
    assert len(clusters) == 1
    assert clusters[0].matchbook is None
    assert {item.source_event_id for item in clusters[0].polymarket_events} == {"pm-calatayud"}
    assert {item.source_event_id for item in clusters[0].kalshi_events} == {"k-calatayud"}


def test_kickoff_bucket_boundary_is_not_a_false_negative() -> None:
    """Events exactly 5 minutes apart must remain candidates across bucket edges."""

    window = timedelta(minutes=5)
    # Choose a timestamp just before a 300s bucket boundary.
    base = datetime(2026, 9, 21, 15, 4, 59, tzinfo=UTC)
    later = base + window
    left = _event(
        VenueName.POLYMARKET,
        "pm-boundary",
        home="Leeds United",
        away="Leicester City",
        kickoff=base,
    )
    right = _event(
        VenueName.KALSHI,
        "k-boundary",
        home="Leeds United",
        away="Leicester City",
        kickoff=later,
    )
    candidates, _diag = build_indexed_candidates(
        [left, right], kickoff_tolerance=window, cache=None
    )
    assert (left, right) in candidates or (right, left) in candidates
    matcher = EventMatcher(kickoff_tolerance=window)
    assert matcher.could_match(left.canonical, right.canonical) is True
    clusters, counts = cluster_venue_events(
        matchbook=[],
        polymarket=[left],
        kalshi=[right],
        matcher=matcher,
        max_event_pairs=4,
    )
    assert counts["polymarket_kalshi"] == 1
    assert len(clusters) == 1


def _large_universe_items(count: int = 1600) -> tuple[
    list[VenueEvent],
    list[VenueEvent],
    list[VenueEvent],
    set[tuple[str, str]],
]:
    """Sparse but realistic mix: many competitions/kickoffs plus known true matches."""

    matchbook: list[VenueEvent] = []
    polymarket: list[VenueEvent] = []
    kalshi: list[VenueEvent] = []
    expected_cross: set[tuple[str, str]] = set()
    identities = THREE_LEAGUE_FIXTURES
    true_match_n = 40
    index = 0
    while True:
        total = len(matchbook) + len(polymarket) + len(kalshi)
        if total >= count:
            break
        competition, home, away = identities[index % len(identities)]
        day = index // 80
        slot = index % 16
        kickoff = KICKOFF + timedelta(days=day, hours=slot)
        remaining = count - total
        if index < true_match_n and remaining >= 3:
            mb_id = f"mb-{index}"
            pm_id = f"pm-{index}"
            k_id = f"k-{index}"
            matchbook.append(
                _event(
                    VenueName.MATCHBOOK,
                    mb_id,
                    home=home,
                    away=away,
                    competition=competition,
                    kickoff=kickoff,
                )
            )
            polymarket.append(
                _event(
                    VenueName.POLYMARKET,
                    pm_id,
                    home=home,
                    away=away,
                    competition=competition,
                    kickoff=kickoff,
                )
            )
            kalshi.append(
                _event(
                    VenueName.KALSHI,
                    k_id,
                    home=home,
                    away=away,
                    competition=competition,
                    kickoff=kickoff,
                )
            )
            expected_cross.add((home, away))
            index += 1
            continue
        venue = (VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI)[index % 3]
        suffix = f"{index}-{venue.value}"
        item = _event(
            venue,
            suffix,
            home=f"{home} {index}",
            away=f"{away} {index}",
            competition=COMPETITIONS[index % len(COMPETITIONS)],
            kickoff=kickoff,
        )
        if venue is VenueName.MATCHBOOK:
            matchbook.append(item)
        elif venue is VenueName.POLYMARKET:
            polymarket.append(item)
        else:
            kalshi.append(item)
        index += 1
    return matchbook, polymarket, kalshi, expected_cross


def test_large_universe_candidate_space_is_far_below_naive_n_squared() -> None:
    matchbook, polymarket, kalshi, expected_cross = _large_universe_items(1600)
    items = [*matchbook, *polymarket, *kalshi]
    naive = naive_pair_space(len(items))
    candidates, diagnostics = build_indexed_candidates(
        items, kickoff_tolerance=timedelta(minutes=5)
    )
    generated = diagnostics["candidate_pairs_generated"]
    reduction = 100.0 * (naive - generated) / naive
    print(
        "issue466_large_universe_pair_counts "
        f"n={len(items)} naive={naive} candidates={generated} "
        f"pruned={diagnostics['pairs_pruned_by_index']} reduction_pct={reduction:.4f}"
    )
    assert naive == 1600 * 1599 // 2
    assert generated <= int(naive * 0.05)
    assert generated < naive
    matcher = EventMatcher()
    clusters, counts = cluster_venue_events(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=matcher,
        max_event_pairs=100,
    )
    found = {
        (cluster.anchor.canonical.home_team, cluster.anchor.canonical.away_team)
        for cluster in clusters
        if cluster.venue_count >= 2
    }
    assert expected_cross <= found
    assert counts["matchbook_polymarket"] >= 40
    assert counts["polymarket_kalshi"] >= 40
    assert any(cluster.venue_count >= 2 for cluster in clusters)
    assert clusters[0].venue_count >= clusters[-1].venue_count


def test_indexed_candidates_are_a_superset_of_matcher_hits() -> None:
    matchbook, polymarket, kalshi, _expected = _large_universe_items(120)
    items = [*matchbook, *polymarket, *kalshi]
    candidates, _diag = build_indexed_candidates(
        items, kickoff_tolerance=timedelta(minutes=5)
    )
    candidate_keys = {
        frozenset(
            ((left.venue.value, left.source_event_id), (right.venue.value, right.source_event_id))
        )
        for left, right in candidates
    }
    matcher = EventMatcher()
    for left_index, left in enumerate(items):
        for right in items[left_index + 1 :]:
            if matcher.match(left.canonical, right.canonical).matched:
                key = frozenset(
                    (
                        (left.venue.value, left.source_event_id),
                        (right.venue.value, right.source_event_id),
                    )
                )
                assert key in candidate_keys


def test_generation_cache_skips_proven_single_venue_pairs() -> None:
    reset_universe_identity_cache()
    cache = GenerationIdentityCache()
    cache.bind(7)
    matchbook = [
        _event(VenueName.MATCHBOOK, "mb-a", home="Arsenal", away="Chelsea"),
    ]
    polymarket = [
        _event(
            VenueName.POLYMARKET,
            "pm-other",
            home="Liverpool",
            away="Manchester City",
        )
    ]
    first, _counts = cluster_venue_events(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=[],
        matcher=EventMatcher(),
        max_event_pairs=8,
        identity_cache=cache,
    )
    assert all(cluster.venue_count == 1 for cluster in first)
    second_pass = ClusterPass(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=[],
        matcher=EventMatcher(),
        max_event_pairs=8,
        identity_cache=cache,
    )
    assert second_pass.index_diagnostics["pairs_skipped_by_generation_cache"] > 0
    cache.clear()
    _rebuilt, diag = build_indexed_candidates(
        [*matchbook, *polymarket],
        kickoff_tolerance=timedelta(minutes=5),
        cache=cache,
    )
    assert diag["pairs_skipped_by_generation_cache"] == 0


def test_clear_and_update_invalidates_generation_identity_cache() -> None:
    reset_universe_identity_cache()
    cache = get_universe_identity_cache()
    cache.bind(3)
    cache.no_cross_venue[("polymarket", "pm-1")] = "stale"
    incremental = get_cross_generation_identity_cache()
    incremental.semantic_version = "stale-version"
    incremental.events[("matchbook", "mb-1")] = CachedEventIdentity(
        venue="matchbook",
        source_event_id="mb-1",
        fingerprint="stale",
    )
    coordinator = LiveRefreshCoordinator()
    coordinator.reset()
    assert get_universe_identity_cache().no_cross_venue == {}
    assert get_universe_identity_cache().generation_id is None
    assert get_cross_generation_identity_cache().events == {}
    assert get_cross_generation_identity_cache().semantic_version is None


def test_clustering_reserve_holds_market_eval_slice_on_150s_chunk() -> None:
    assert clustering_market_eval_reserve_seconds(None) == 0.0
    assert clustering_market_eval_reserve_seconds(2.0) == 0.0
    reserve = clustering_market_eval_reserve_seconds(140.0)
    assert reserve >= 40.0
    assert reserve <= 45.0


@pytest.mark.asyncio
async def test_universe_evaluates_multi_venue_before_cheap_single_venue_rows() -> None:
    reset_universe_identity_cache()
    matchbook = FakeMatchbook()
    polymarket = FakePolymarket()
    kalshi = FakeKalshiBTTS(
        [("Premier League", "Newcastle United", "Chelsea", COLLECTOR_KICKOFF)],
        arb=True,
    )
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        cycle_timeout_seconds=8.0,
    )
    try:
        report = await collector.collect_and_scan(
            scan_lane=ScanLane.UNIVERSE.value,
            enabled_venues=[VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI],
            cycle_timeout_seconds=8.0,
            universe_generation_id=12,
            venue_costs=matchbook_kalshi_costs(),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"))],
            maximum_execution_risk=100,
        )
        diagnostics = report.scan_diagnostics
        assert diagnostics["clustering_truncated"] is False
        assert diagnostics["candidate_pairs_generated"] <= diagnostics["naive_pair_space"]
        singles = [
            item
            for item in report.discovered_fixtures
            if item.market_evaluation_state
            == MarketEvaluationState.SINGLE_VENUE_NO_CROSS_VENUE_CANDIDATE.value
        ]
        evaluated = [
            item
            for item in report.discovered_fixtures
            if item.market_evaluation_state == MarketEvaluationState.EVALUATED.value
        ]
        assert singles
        assert "pm-event-2" not in polymarket.list_markets_calls
        assert evaluated
        assert any(item.matchbook_matched and item.polymarket_matched for item in evaluated)
        first_multi = next(
            index
            for index, item in enumerate(report.discovered_fixtures)
            if bool(item.matchbook_matched) and bool(item.polymarket_matched)
        )
        first_single = next(
            index
            for index, item in enumerate(report.discovered_fixtures)
            if item.market_evaluation_state
            == MarketEvaluationState.SINGLE_VENUE_NO_CROSS_VENUE_CANDIDATE.value
        )
        assert first_multi < first_single
    finally:
        repository.close()
        reset_universe_identity_cache()
