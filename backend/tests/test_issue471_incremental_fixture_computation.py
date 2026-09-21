"""#471 Cross-generation incremental fixture computation with versioned fingerprints.

Data class: synthetic/fixture events. Not live, historical, or modelled quotes.
PAPER only. Clean full recomputation is the correctness oracle.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from sports_hedge.application.fixture_clusters import (
    ClusterPass,
    VenueEvent,
    cluster_member_keyset,
    cluster_venue_events,
)
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.universe_identity_cache import (
    CrossGenerationIdentityCache,
    compact_payload_contains_secret,
    event_identity_fingerprint,
    event_identity_fingerprint_payload,
    get_cross_generation_identity_cache,
    get_universe_identity_cache,
    identity_cache_semantic_version,
    reset_generation_scoped_identity_cache,
    reset_universe_identity_cache,
)
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.events import EventMatcher

KICKOFF = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)


class CountingMatcher(EventMatcher):
    """Counts matcher work on the same instance ClusterPass bulk-scores with."""

    def __init__(self, **kwargs: object) -> None:
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self.match_calls = 0
        self.could_match_calls = 0

    def bulk_snapshot(self) -> CountingMatcher:
        return self

    def could_match(self, left, right, **kwargs):  # type: ignore[no-untyped-def]
        self.could_match_calls += 1
        return super().could_match(left, right, **kwargs)

    def match(self, left, right, **kwargs):  # type: ignore[no-untyped-def]
        self.match_calls += 1
        return super().match(left, right, **kwargs)


@pytest.fixture(autouse=True)
def _reset_identity_caches() -> None:
    reset_universe_identity_cache()
    yield
    reset_universe_identity_cache()


def _event(
    venue: VenueName,
    source_event_id: str,
    *,
    home: str,
    away: str,
    competition: str = "Premier League",
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


def _cluster_sets(clusters) -> set[frozenset[tuple[str, str]]]:
    return {cluster_member_keyset(cluster) for cluster in clusters}


def _split(items: list[VenueEvent]) -> tuple[list[VenueEvent], list[VenueEvent], list[VenueEvent], list[VenueEvent]]:
    matchbook = [item for item in items if item.venue is VenueName.MATCHBOOK]
    polymarket = [item for item in items if item.venue is VenueName.POLYMARKET]
    kalshi = [item for item in items if item.venue is VenueName.KALSHI]
    extra = [
        item
        for item in items
        if item.venue not in {VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI}
    ]
    return matchbook, polymarket, kalshi, extra


def _cluster(
    items: list[VenueEvent],
    *,
    matcher: EventMatcher | None = None,
    incremental: CrossGenerationIdentityCache | None = None,
    max_event_pairs: int = 64,
):
    matchbook, polymarket, kalshi, extra = _split(items)
    return cluster_venue_events(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=matcher or EventMatcher(),
        max_event_pairs=max_event_pairs,
        extra=extra or None,
        incremental_cache=incremental,
    )


def _generation_one() -> list[VenueEvent]:
    return [
        _event(VenueName.MATCHBOOK, "mb-ars-che", home="Arsenal", away="Chelsea"),
        _event(VenueName.POLYMARKET, "pm-ars-che", home="Arsenal", away="Chelsea"),
        _event(VenueName.KALSHI, "k-ars-che", home="Arsenal", away="Chelsea"),
        _event(VenueName.MATCHBOOK, "mb-liv-mci", home="Liverpool", away="Manchester City"),
        _event(VenueName.POLYMARKET, "pm-liv-mci", home="Liverpool", away="Manchester City"),
        _event(
            VenueName.SMARKETS,
            "sm-tot-whu",
            home="Tottenham Hotspur",
            away="West Ham United",
        ),
        _event(VenueName.MATCHBOOK, "mb-bre-ful", home="Brentford", away="Fulham"),
    ]


def _generation_two(base: list[VenueEvent]) -> list[VenueEvent]:
    kept = [
        item
        for item in base
        if item.source_event_id not in {"sm-tot-whu"}
    ]
    changed = []
    for item in kept:
        if item.source_event_id == "pm-liv-mci":
            changed.append(
                _event(
                    item.venue,
                    item.source_event_id,
                    home="Liverpool",
                    away="Manchester City",
                    kickoff=KICKOFF + timedelta(minutes=1),
                )
            )
        else:
            changed.append(item)
    changed.extend(
        [
            _event(VenueName.KALSHI, "k-bre-ful", home="Brentford", away="Fulham"),
            _event(
                VenueName.SMARKETS,
                "sm-new-bha",
                home="Newcastle United",
                away="Brighton",
            ),
        ]
    )
    return changed


def test_fingerprint_covers_identity_fields_and_ignores_raw_secrets() -> None:
    clean = _event(VenueName.MATCHBOOK, "mb-1", home="Arsenal", away="Chelsea")
    secret = _event(
        VenueName.MATCHBOOK,
        "mb-1",
        home="Arsenal",
        away="Chelsea",
        raw={
            "id": "mb-1",
            "authorization": "Bearer super-secret-token",
            "api_key": "sk-live-not-for-cache",
        },
    )
    renamed = _event(VenueName.MATCHBOOK, "mb-1", home="Arsenal Women", away="Chelsea Women")
    payload = event_identity_fingerprint_payload(clean)
    assert "venue=matchbook" in payload
    assert "source_event_id=mb-1" in payload
    assert "sport=football" in payload
    assert "competition=Premier League" in payload
    assert "home_team=Arsenal" in payload
    assert "kickoff_utc=" in payload
    assert event_identity_fingerprint(clean) == event_identity_fingerprint(secret)
    assert event_identity_fingerprint(clean) != event_identity_fingerprint(renamed)
    assert "Bearer" not in payload
    assert "api_key" not in payload


def test_identical_snapshot_reuses_matcher_scores_and_matches_full_recompute() -> None:
    items = _generation_one()
    cache = CrossGenerationIdentityCache()
    matcher = CountingMatcher()
    incremental_first, _counts = _cluster(items, matcher=matcher, incremental=cache)
    first_calls = matcher.match_calls + matcher.could_match_calls
    assert first_calls > 0
    assert cache.events
    assert cache.pairs

    matcher_second = CountingMatcher()
    incremental_second, _counts = _cluster(items, matcher=matcher_second, incremental=cache)
    oracle, _counts = _cluster(items, matcher=EventMatcher())
    assert _cluster_sets(incremental_first) == _cluster_sets(oracle)
    assert _cluster_sets(incremental_second) == _cluster_sets(oracle)
    assert matcher_second.match_calls == 0
    assert matcher_second.could_match_calls == 0

    matchbook, polymarket, kalshi, extra = _split(items)
    pass_for_diag = ClusterPass(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=64,
        extra=extra,
        incremental_cache=cache,
    )
    for left, right in pass_for_diag.pairs():
        pass_for_diag.consider(left, right)
    diag = pass_for_diag.clustering_diagnostics(truncated=False)
    assert diag["identity_unchanged_reused"] == len(items)
    assert diag["identity_events_new"] == 0
    assert diag["identity_changed_recomputed"] == 0
    assert diag["identity_events_removed"] == 0
    assert diag["identity_saved_candidate_comparisons"] > 0
    assert diag["identity_cache_hit_pct"] > 0


def test_cross_generation_differential_equals_clean_full_recompute() -> None:
    generation_one = _generation_one()
    cache = CrossGenerationIdentityCache()
    first, _counts = _cluster(generation_one, incremental=cache)
    assert any(
        (VenueName.SMARKETS.value, "sm-tot-whu") in cluster_member_keyset(cluster)
        for cluster in first
    )

    generation_two = _generation_two(generation_one)
    matcher = CountingMatcher()
    incremental, _counts = _cluster(generation_two, matcher=matcher, incremental=cache)
    oracle, _counts = _cluster(generation_two, matcher=EventMatcher())
    assert _cluster_sets(incremental) == _cluster_sets(oracle)

    member_ids = {key for cluster in incremental for key in cluster_member_keyset(cluster)}
    assert ("smarkets", "sm-tot-whu") not in member_ids
    assert ("kalshi", "k-bre-ful") in member_ids
    assert ("smarkets", "sm-new-bha") in member_ids
    brentford = next(
        cluster
        for cluster in incremental
        if ("matchbook", "mb-bre-ful") in cluster_member_keyset(cluster)
    )
    assert ("kalshi", "k-bre-ful") in cluster_member_keyset(brentford)

    diagnostics = cache.classify(
        generation_two, identity_cache_semantic_version(EventMatcher())
    )
    # After commit, generation two is the snapshot; classify against itself.
    assert diagnostics.unchanged_reused == len(generation_two)
    assert diagnostics.removed == 0

    payload = cache.compact_payload()
    assert compact_payload_contains_secret(payload) is False
    venues = {record["venue"] for record in payload["events"]}
    assert "smarkets" in venues
    assert "matchbook" in venues
    for record in payload["events"]:
        assert set(record) == {"venue", "source_event_id", "fingerprint"}


def test_changed_and_new_and_removed_diagnostics_before_commit() -> None:
    generation_one = _generation_one()
    cache = CrossGenerationIdentityCache()
    _cluster(generation_one, incremental=cache)
    generation_two = _generation_two(generation_one)
    cluster_pass = ClusterPass(
        matchbook=_split(generation_two)[0],
        polymarket=_split(generation_two)[1],
        kalshi=_split(generation_two)[2],
        matcher=EventMatcher(),
        max_event_pairs=64,
        extra=_split(generation_two)[3],
        incremental_cache=cache,
    )
    diag = cluster_pass.incremental_diagnostics
    assert diag.discovered == len(generation_two)
    assert diag.unchanged_reused >= 4
    assert diag.changed_recomputed == 1
    assert diag.new == 2
    assert diag.removed == 1
    assert diag.semantic_version_mismatch is False
    for left, right in cluster_pass.pairs():
        cluster_pass.consider(left, right)
    clusters, _counts = cluster_pass.finalize()
    oracle, _oracle_counts = _cluster(generation_two)
    assert _cluster_sets(clusters) == _cluster_sets(oracle)
    assert cluster_pass.incremental_diagnostics.saved_candidate_comparisons > 0


def test_registry_version_bump_forces_re_evaluation(monkeypatch: pytest.MonkeyPatch) -> None:
    items = _generation_one()
    cache = CrossGenerationIdentityCache()
    _cluster(items, incremental=cache)
    monkeypatch.setattr(
        "sports_hedge.application.target_competitions.OPERATOR_COMPETITION_REGISTRY_VERSION",
        9_999,
    )
    probe = ClusterPass(
        matchbook=_split(items)[0],
        polymarket=_split(items)[1],
        kalshi=_split(items)[2],
        matcher=EventMatcher(),
        max_event_pairs=64,
        extra=_split(items)[3],
        incremental_cache=cache,
    )
    assert probe.incremental_diagnostics.semantic_version_mismatch is True
    assert probe.incremental_diagnostics.unchanged_reused == 0
    assert cache.pair_reuse_enabled(probe.semantic_version) is False
    matcher = CountingMatcher()
    incremental, _counts = _cluster(items, matcher=matcher, incremental=cache)
    oracle, _oracle_counts = _cluster(items)
    assert _cluster_sets(incremental) == _cluster_sets(oracle)
    assert matcher.match_calls + matcher.could_match_calls > 0


def test_matcher_threshold_bump_forces_re_evaluation() -> None:
    items = _generation_one()
    cache = CrossGenerationIdentityCache()
    _cluster(items, matcher=EventMatcher(threshold=0.92), incremental=cache)
    probe = ClusterPass(
        matchbook=_split(items)[0],
        polymarket=_split(items)[1],
        kalshi=_split(items)[2],
        matcher=EventMatcher(threshold=0.99),
        max_event_pairs=64,
        extra=_split(items)[3],
        incremental_cache=cache,
    )
    assert probe.incremental_diagnostics.semantic_version_mismatch is True
    assert probe.incremental_diagnostics.unchanged_reused == 0
    matcher = CountingMatcher(threshold=0.99)
    incremental, _counts = _cluster(items, matcher=matcher, incremental=cache)
    oracle, _oracle_counts = _cluster(items, matcher=EventMatcher(threshold=0.99))
    assert _cluster_sets(incremental) == _cluster_sets(oracle)
    assert matcher.match_calls + matcher.could_match_calls > 0


def test_clear_and_update_invalidates_cross_generation_cache() -> None:
    items = _generation_one()
    _cluster(items, incremental=get_cross_generation_identity_cache())
    assert get_cross_generation_identity_cache().events
    coordinator = LiveRefreshCoordinator()
    coordinator.reset()
    assert get_cross_generation_identity_cache().events == {}
    assert get_cross_generation_identity_cache().pairs == {}
    assert get_universe_identity_cache().generation_id is None


def test_generation_close_keeps_cross_generation_fingerprints() -> None:
    items = _generation_one()
    cache = get_cross_generation_identity_cache()
    _cluster(items, incremental=cache)
    generation = get_universe_identity_cache()
    generation.bind(4)
    generation.no_cross_venue[("matchbook", "mb-ars-che")] = "keep-check"
    reset_generation_scoped_identity_cache()
    assert get_universe_identity_cache().no_cross_venue == {}
    assert cache.events
    assert cache.pairs


def test_truncated_clustering_does_not_commit_partial_snapshot() -> None:
    items = _generation_one()
    cache = CrossGenerationIdentityCache()
    _cluster(items, incremental=cache)
    committed = dict(cache.events)
    generation_two = _generation_two(items)
    cluster_pass = ClusterPass(
        matchbook=_split(generation_two)[0],
        polymarket=_split(generation_two)[1],
        kalshi=_split(generation_two)[2],
        matcher=EventMatcher(),
        max_event_pairs=64,
        extra=_split(generation_two)[3],
        incremental_cache=cache,
    )
    left, right = next(iter(cluster_pass.pairs()))
    cluster_pass.consider(left, right)
    cluster_pass.finalize()
    assert cache.events == committed
    assert ("kalshi", "k-bre-ful") not in cache.events
