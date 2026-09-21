"""#472 Global fixture identity graph for ambiguous multi-venue components.

Data class: synthetic/fixture events and scripted pairwise evidence.
Not live, historical, or modelled quotes. PAPER only. EventMatcher
thresholds are not changed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from sports_hedge.application.fixture_clusters import (
    ClusterPass,
    VenueEvent,
    cluster_venue_events,
)
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.events import (
    DEFAULT_EVENT_MATCH_THRESHOLD,
    PAPER_EVENT_MATCH_THRESHOLD,
    EventMatcher,
    EventMatchResult,
    paper_event_matcher,
)
from sports_hedge.matching.identity_graph import (
    DEFAULT_ASSIGNMENT_MARGIN,
    HARD_VETO_REASONS,
    NodeKey,
    ScoredIdentityPair,
    assign_identity_components,
    greedy_local_pairwise_assignment,
    pair_kind,
)

KICKOFF = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)


def _event(
    venue: VenueName,
    source_event_id: str,
    *,
    home: str = "Alpha FC",
    away: str = "Beta FC",
    competition: str = "Premier League",
    kickoff: datetime = KICKOFF,
) -> VenueEvent:
    canonical = CanonicalEvent(
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


def _key(venue: VenueName, source_event_id: str) -> NodeKey:
    return (venue, source_event_id)


def _pair(
    left: NodeKey,
    right: NodeKey,
    confidence: float,
    *,
    matched: bool | None = None,
    veto: bool = False,
    reasons: tuple[str, ...] = ("scripted_pairwise_evidence",),
) -> ScoredIdentityPair:
    hit = confidence >= 0.80 if matched is None else matched
    return ScoredIdentityPair.from_endpoints(
        left,
        right,
        confidence=confidence,
        reasons=("curated_team_mismatch",) if veto else reasons,
        matched=False if veto else hit,
        veto=veto,
    )


class ScriptedIdentityMatcher:
    """Explicit pairwise oracle for assignment fixtures. Not a production matcher."""

    def __init__(
        self,
        scores: dict[frozenset[str], float],
        *,
        vetoes: dict[frozenset[str], str] | None = None,
        threshold: float = 0.80,
    ) -> None:
        self.threshold = threshold
        self.kickoff_tolerance = timedelta(minutes=5)
        self._scores = scores
        self._vetoes = vetoes or {}

    def bulk_snapshot(self) -> ScriptedIdentityMatcher:
        return self

    def _ids(self, left: CanonicalEvent, right: CanonicalEvent) -> frozenset[str]:
        return frozenset({left.source_event_id, right.source_event_id})

    def could_match(self, left: CanonicalEvent, right: CanonicalEvent) -> bool:
        return self._ids(left, right) not in self._vetoes

    def match(self, left: CanonicalEvent, right: CanonicalEvent) -> EventMatchResult:
        key = self._ids(left, right)
        if key in self._vetoes:
            return EventMatchResult(
                matched=False,
                confidence=0.0,
                reasons=[self._vetoes[key]],
            )
        confidence = self._scores.get(key)
        if confidence is None:
            return EventMatchResult(
                matched=False,
                confidence=0.0,
                reasons=["no_scripted_edge"],
            )
        return EventMatchResult(
            matched=confidence >= self.threshold,
            confidence=round(float(confidence), 6),
            reasons=["scripted_pairwise_evidence"],
        )


def _ids(*source_event_ids: str) -> frozenset[str]:
    return frozenset(source_event_ids)


def test_event_matcher_thresholds_are_unchanged() -> None:
    assert DEFAULT_EVENT_MATCH_THRESHOLD == 0.92
    assert PAPER_EVENT_MATCH_THRESHOLD == 0.80
    assert EventMatcher().threshold == 0.92
    assert paper_event_matcher(SimpleNamespace(paper_event_match_threshold=0.80)).threshold == 0.80
    assert DEFAULT_ASSIGNMENT_MARGIN == 0.03
    assert "curated_team_mismatch" in HARD_VETO_REASONS
    assert "competition_mismatch" in HARD_VETO_REASONS


def test_pair_kind_is_generic_and_keeps_historical_names() -> None:
    assert pair_kind(VenueName.MATCHBOOK, VenueName.POLYMARKET) == "matchbook_polymarket"
    assert pair_kind(VenueName.KALSHI, VenueName.POLYMARKET) == "polymarket_kalshi"
    assert pair_kind(VenueName.SMARKETS, VenueName.KALSHI) == "kalshi_smarkets"
    assert pair_kind(VenueName.SMARKETS, VenueName.MATCHBOOK) == "matchbook_smarkets"


def test_greedy_local_pairing_is_suboptimal_global_assignment_is_coherent() -> None:
    """Classic 2x2 trap: locally heaviest edge blocks the better global pairing.

    A1-B1 0.90 taken first leaves A2 and B2 unmatched (no A2-B2 edge).
    Global assignment takes A1-B2 0.85 + A2-B1 0.88.
    """

    a1 = _key(VenueName.MATCHBOOK, "a1")
    a2 = _key(VenueName.MATCHBOOK, "a2")
    b1 = _key(VenueName.POLYMARKET, "b1")
    b2 = _key(VenueName.POLYMARKET, "b2")
    edges = [
        _pair(a1, b1, 0.90),
        _pair(a1, b2, 0.85),
        _pair(a2, b1, 0.88),
    ]
    greedy_chosen, greedy_weight = greedy_local_pairwise_assignment(edges)
    assert greedy_weight == 0.90
    assert [edge.left[1] + "-" + edge.right[1] for edge in greedy_chosen] == ["a1-b1"]

    result = assign_identity_components(
        [a1, a2, b1, b2],
        edges,
        threshold=0.80,
    )
    assert result.diagnostics.global_assignments == 1
    assert result.diagnostics.fail_closed_components == 0
    clustered = {
        frozenset(member[1] for member in cluster.member_keys)
        for cluster in result.clusters
        if len(cluster.member_keys) > 1
    }
    assert clustered == {frozenset({"a1", "b2"}), frozenset({"a2", "b1"})}
    provenance = next(
        cluster.provenance for cluster in result.clusters if len(cluster.member_keys) > 1
    )
    assert provenance.method == "global_max_weight"
    assert provenance.greedy_weight == 0.90
    assert provenance.global_weight == 1.73
    assert provenance.confidence_margin == 0.83
    rejected_ids = {
        frozenset({edge.left[1], edge.right[1]}) for edge in provenance.rejected_competing_edges
    }
    assert _ids("a1", "b1") in rejected_ids
    assert "at_most_one_supernode_per_venue_per_cluster" in provenance.constraints


def test_low_margin_ambiguous_component_fails_closed() -> None:
    a1 = _key(VenueName.MATCHBOOK, "a1")
    a2 = _key(VenueName.MATCHBOOK, "a2")
    b1 = _key(VenueName.KALSHI, "b1")
    b2 = _key(VenueName.KALSHI, "b2")
    edges = [
        _pair(a1, b1, 0.90),
        _pair(a2, b2, 0.80),
        _pair(a1, b2, 0.88),
        _pair(a2, b1, 0.81),
    ]
    result = assign_identity_components([a1, a2, b1, b2], edges, threshold=0.80)
    assert result.diagnostics.fail_closed_components == 1
    assert all(len(cluster.member_keys) == 1 for cluster in result.clusters)
    provenance = result.clusters[0].provenance
    assert provenance.method == "fail_closed"
    assert provenance.fail_closed_reason == "assignment_margin_too_small"
    assert provenance.confidence_margin is not None
    assert provenance.confidence_margin < DEFAULT_ASSIGNMENT_MARGIN
    assert provenance.rejected_competing_edges


def test_three_venue_veto_path_fails_closed_instead_of_forcing() -> None:
    mb = _key(VenueName.MATCHBOOK, "mb")
    pm = _key(VenueName.POLYMARKET, "pm")
    kalshi = _key(VenueName.KALSHI, "k")
    edges = [
        _pair(mb, pm, 0.95),
        _pair(pm, kalshi, 0.93),
        _pair(mb, kalshi, 0.0, veto=True, reasons=("curated_team_mismatch",)),
    ]
    result = assign_identity_components([mb, pm, kalshi], edges, threshold=0.80)
    assert result.diagnostics.contradictory_components == 1
    assert result.diagnostics.fail_closed_components == 1
    assert all(len(cluster.member_keys) == 1 for cluster in result.clusters)
    provenance = result.clusters[0].provenance
    assert provenance.fail_closed_reason == "contradictory_component"
    assert {frozenset({edge.left[1], edge.right[1]}) for edge in provenance.rejected_competing_edges} == {
        _ids("mb", "pm"),
        _ids("pm", "k"),
    }


def test_same_venue_siblings_contract_before_assignment() -> None:
    pm_ml = _key(VenueName.POLYMARKET, "pm-ml")
    pm_btts = _key(VenueName.POLYMARKET, "pm-btts")
    k_game = _key(VenueName.KALSHI, "k-game")
    k_btts = _key(VenueName.KALSHI, "k-btts")
    edges = [
        _pair(pm_ml, pm_btts, 0.99),
        _pair(k_game, k_btts, 0.99),
        _pair(pm_ml, k_game, 0.96),
        _pair(pm_btts, k_btts, 0.95),
        _pair(pm_ml, k_btts, 0.94),
        _pair(pm_btts, k_game, 0.94),
    ]
    result = assign_identity_components(
        [pm_ml, pm_btts, k_game, k_btts],
        edges,
        threshold=0.92,
    )
    assert result.diagnostics.obvious_components == 1
    assert result.diagnostics.global_assignments == 0
    assert len(result.clusters) == 1
    assert result.clusters[0].member_keys == {pm_ml, pm_btts, k_game, k_btts}
    assert result.clusters[0].provenance.method == "obvious_union"


def test_assignment_is_deterministic() -> None:
    a1 = _key(VenueName.MATCHBOOK, "a1")
    a2 = _key(VenueName.MATCHBOOK, "a2")
    b1 = _key(VenueName.POLYMARKET, "b1")
    b2 = _key(VenueName.POLYMARKET, "b2")
    edges = [
        _pair(a2, b1, 0.88),
        _pair(a1, b1, 0.90),
        _pair(a1, b2, 0.85),
    ]
    first = assign_identity_components([b2, a2, b1, a1], list(reversed(edges)), threshold=0.80)
    second = assign_identity_components([a1, a2, b1, b2], edges, threshold=0.80)
    assert [frozenset(cluster.member_keys) for cluster in first.clusters] == [
        frozenset(cluster.member_keys) for cluster in second.clusters
    ]
    assert first.clusters[0].provenance.as_record() == second.clusters[0].provenance.as_record()


def test_four_venue_obvious_clique_including_smarkets() -> None:
    matchbook = [_event(VenueName.MATCHBOOK, "mb-leeds", home="Leeds United", away="Leicester City")]
    polymarket = [_event(VenueName.POLYMARKET, "pm-leeds", home="Leeds United", away="Leicester City")]
    kalshi = [_event(VenueName.KALSHI, "k-leeds", home="Leeds United", away="Leicester City")]
    smarkets = [_event(VenueName.SMARKETS, "sm-leeds", home="Leeds United", away="Leicester City")]
    clusters, counts = cluster_venue_events(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=16,
        extra=smarkets,
    )
    assert len(clusters) == 1
    cluster = clusters[0]
    assert cluster.venue_count == 4
    assert VenueName.SMARKETS in cluster.venues_present
    assert cluster.events_for(VenueName.SMARKETS)[0].source_event_id == "sm-leeds"
    assert cluster.identity_provenance is not None
    assert cluster.identity_provenance.method == "obvious_union"
    assert "matchbook_smarkets" in cluster.pair_kinds or any(
        "smarkets" in kind for kind in cluster.pair_kinds
    )
    assert counts["matchbook_polymarket"] == 1
    assert counts["polymarket_kalshi"] == 1


def test_cluster_pass_uses_global_assignment_on_greedy_trap() -> None:
    matcher = ScriptedIdentityMatcher(
        {
            _ids("mb-x", "pm-x"): 0.90,
            _ids("mb-x", "pm-y"): 0.85,
            _ids("mb-y", "pm-x"): 0.88,
        }
    )
    matchbook = [
        _event(VenueName.MATCHBOOK, "mb-x"),
        _event(VenueName.MATCHBOOK, "mb-y"),
    ]
    polymarket = [
        _event(VenueName.POLYMARKET, "pm-x"),
        _event(VenueName.POLYMARKET, "pm-y"),
    ]
    clusters, counts = cluster_venue_events(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=[],
        matcher=matcher,  # type: ignore[arg-type]
        max_event_pairs=16,
    )
    grouped = {
        frozenset(item.source_event_id for item in _members(cluster))
        for cluster in clusters
        if cluster.venue_count >= 2
    }
    assert grouped == {frozenset({"mb-x", "pm-y"}), frozenset({"mb-y", "pm-x"})}
    assert counts["matchbook_polymarket"] == 2
    for cluster in clusters:
        if cluster.venue_count < 2:
            continue
        provenance = cluster.identity_provenance
        assert provenance is not None
        assert provenance.method == "global_max_weight"
        assert provenance.global_weight == 1.73
        assert provenance.greedy_weight == 0.90
        assert provenance.rejected_competing_edges


def test_real_event_matcher_hard_veto_is_not_overridden() -> None:
    matchbook = [
        _event(
            VenueName.MATCHBOOK,
            "mb-city",
            home="Manchester City",
            away="Liverpool",
        )
    ]
    polymarket = [
        _event(
            VenueName.POLYMARKET,
            "pm-united",
            home="Manchester United",
            away="Liverpool",
        )
    ]
    matcher = EventMatcher(threshold=0.80)
    result = matcher.match(matchbook[0].canonical, polymarket[0].canonical)
    assert result.matched is False
    assert "curated_team_mismatch" in result.reasons
    clusters, counts = cluster_venue_events(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=[],
        matcher=matcher,
        max_event_pairs=8,
    )
    assert counts["matchbook_polymarket"] == 0
    assert all(cluster.venue_count == 1 for cluster in clusters)


def test_three_venue_scripted_contradiction_does_not_cluster() -> None:
    matcher = ScriptedIdentityMatcher(
        {
            _ids("mb", "pm"): 0.95,
            _ids("pm", "k"): 0.93,
        },
        vetoes={_ids("mb", "k"): "curated_team_mismatch"},
    )
    clusters, counts = cluster_venue_events(
        matchbook=[_event(VenueName.MATCHBOOK, "mb")],
        polymarket=[_event(VenueName.POLYMARKET, "pm")],
        kalshi=[_event(VenueName.KALSHI, "k")],
        matcher=matcher,  # type: ignore[arg-type]
        max_event_pairs=8,
    )
    assert counts["matchbook_polymarket"] == 0
    assert counts["polymarket_kalshi"] == 0
    assert all(cluster.venue_count == 1 for cluster in clusters)
    reasons = {cluster.identity_provenance.fail_closed_reason for cluster in clusters if cluster.identity_provenance}
    assert "contradictory_component" in reasons


def test_incomplete_ambiguous_component_fails_closed() -> None:
    matcher = ScriptedIdentityMatcher(
        {
            _ids("mb-x", "pm-x"): 0.90,
            _ids("mb-x", "pm-y"): 0.85,
            _ids("mb-y", "pm-x"): 0.88,
        }
    )
    cluster_pass = ClusterPass(
        matchbook=[
            _event(VenueName.MATCHBOOK, "mb-x"),
            _event(VenueName.MATCHBOOK, "mb-y"),
        ],
        polymarket=[
            _event(VenueName.POLYMARKET, "pm-x"),
            _event(VenueName.POLYMARKET, "pm-y"),
        ],
        kalshi=[],
        matcher=matcher,  # type: ignore[arg-type]
        max_event_pairs=16,
    )
    first_left, first_right = next(iter(cluster_pass.pairs()))
    cluster_pass.consider(first_left, first_right)
    clusters, counts = cluster_pass.finalize()
    assert counts["matchbook_polymarket"] == 0
    assert all(cluster.venue_count == 1 for cluster in clusters)
    assert cluster_pass.graph_diagnostics["identity_graph_fail_closed_components"] >= 1


def test_obvious_unique_pair_still_unions_without_global_search() -> None:
    clusters, counts = cluster_venue_events(
        matchbook=[_event(VenueName.MATCHBOOK, "mb-leeds", home="Leeds United", away="Leicester City")],
        polymarket=[_event(VenueName.POLYMARKET, "pm-leeds", home="Leeds United", away="Leicester City")],
        kalshi=[],
        matcher=EventMatcher(),
        max_event_pairs=4,
    )
    assert counts["matchbook_polymarket"] == 1
    assert len(clusters) == 1
    assert clusters[0].identity_provenance is not None
    assert clusters[0].identity_provenance.method == "obvious_union"
    assert clusters[0].identity_provenance.rejected_competing_edges == ()


def test_three_venue_plus_fourth_generic_assignment_trap() -> None:
    """Greedy 1:1 across three venues still loses to global clique assignment.

    Two canonical fixtures, three venues. The locally heaviest edges attach
    fixture X's Matchbook event to fixture Y's Polymarket event and leave a
    broken remainder. Global assignment recovers both coherent triples.
    """

    matcher = ScriptedIdentityMatcher(
        {
            _ids("mb-x", "pm-y"): 0.91,
            _ids("mb-x", "pm-x"): 0.86,
            _ids("mb-y", "pm-x"): 0.85,
            _ids("mb-y", "pm-y"): 0.84,
            _ids("mb-x", "k-x"): 0.89,
            _ids("pm-x", "k-x"): 0.88,
            _ids("mb-y", "k-y"): 0.87,
            _ids("pm-y", "k-y"): 0.86,
            _ids("mb-x", "k-y"): 0.81,
            _ids("pm-x", "k-y"): 0.81,
            _ids("mb-y", "k-x"): 0.81,
            _ids("pm-y", "k-x"): 0.81,
        }
    )
    clusters, _counts = cluster_venue_events(
        matchbook=[_event(VenueName.MATCHBOOK, "mb-x"), _event(VenueName.MATCHBOOK, "mb-y")],
        polymarket=[_event(VenueName.POLYMARKET, "pm-x"), _event(VenueName.POLYMARKET, "pm-y")],
        kalshi=[_event(VenueName.KALSHI, "k-x"), _event(VenueName.KALSHI, "k-y")],
        matcher=matcher,  # type: ignore[arg-type]
        max_event_pairs=32,
    )
    grouped = {
        frozenset(item.source_event_id for item in _members(cluster))
        for cluster in clusters
        if cluster.venue_count >= 2
    }
    assert frozenset({"mb-x", "pm-x", "k-x"}) in grouped
    assert frozenset({"mb-y", "pm-y", "k-y"}) in grouped
    for cluster in clusters:
        if cluster.venue_count < 2:
            continue
        provenance = cluster.identity_provenance
        assert provenance is not None
        assert provenance.method == "global_max_weight"
        assert provenance.global_weight is not None
        assert provenance.greedy_weight is not None
        assert provenance.global_weight > provenance.greedy_weight


def _members(cluster) -> list[VenueEvent]:
    from sports_hedge.application.fixture_clusters import cluster_member_events

    return cluster_member_events(cluster)
