from __future__ import annotations

import asyncio
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sports_hedge.application.universe_identity_cache import (
    CrossGenerationIdentityCache,
    IncrementalIdentityDiagnostics,
    event_cache_key,
    event_identity_fingerprint,
    identity_cache_semantic_version,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.events import EventMatcher, target_competition_code
from sports_hedge.matching.identity_graph import (
    IdentityAssignmentProvenance,
    ScoredIdentityPair,
    assign_identity_components,
    is_hard_identity_veto,
    node_sort_key,
    pair_kind,
)
from sports_hedge.matching.learned_rules import squad_category_fingerprint
from sports_hedge.normalization.identity import (
    canonical_matched_event_id,
    canonical_source_event_id,
)


@dataclass
class VenueEvent:
    venue: VenueName
    raw: dict
    canonical: object
    source_event_id: str


@dataclass
class FixtureCluster:
    """One canonical fixture, possibly backed by multiple source events per venue.

    Polymarket and Kalshi often split a fixture into separate series/events
    (moneyline, BTTS, totals, FTTS). Those source events share fixture identity
    and must be scanned together so settlement-equivalent families can meet.
    """

    matchbook_events: list[VenueEvent] = field(default_factory=list)
    polymarket_events: list[VenueEvent] = field(default_factory=list)
    kalshi_events: list[VenueEvent] = field(default_factory=list)
    other_venue_events: dict[VenueName, list[VenueEvent]] = field(default_factory=dict)
    pair_kinds: set[str] = field(default_factory=set)
    event_match_confidence: float | None = None
    event_match_threshold: float | None = None
    identity_provenance: IdentityAssignmentProvenance | None = None

    @property
    def matchbook(self) -> VenueEvent | None:
        return self.matchbook_events[0] if self.matchbook_events else None

    @property
    def polymarket(self) -> VenueEvent | None:
        return self.polymarket_events[0] if self.polymarket_events else None

    @property
    def kalshi(self) -> VenueEvent | None:
        return self.kalshi_events[0] if self.kalshi_events else None

    @property
    def venues_present(self) -> list[VenueName]:
        present: list[VenueName] = []
        if self.matchbook_events:
            present.append(VenueName.MATCHBOOK)
        if self.polymarket_events:
            present.append(VenueName.POLYMARKET)
        if self.kalshi_events:
            present.append(VenueName.KALSHI)
        for venue in sorted(self.other_venue_events, key=lambda item: item.value):
            if self.other_venue_events[venue]:
                present.append(venue)
        return present

    @property
    def venue_count(self) -> int:
        return len(self.venues_present)

    @property
    def anchor(self) -> VenueEvent:
        for item in (self.matchbook, self.polymarket, self.kalshi):
            if item is not None:
                return item
        for venue in sorted(self.other_venue_events, key=lambda item: item.value):
            events = self.other_venue_events.get(venue) or []
            if events:
                return events[0]
        raise ValueError("empty fixture cluster")

    def event_for(self, venue: VenueName) -> VenueEvent | None:
        events = self.events_for(venue)
        return events[0] if events else None

    def events_for(self, venue: VenueName) -> list[VenueEvent]:
        if venue is VenueName.MATCHBOOK:
            return list(self.matchbook_events)
        if venue is VenueName.POLYMARKET:
            return list(self.polymarket_events)
        if venue is VenueName.KALSHI:
            return list(self.kalshi_events)
        return list(self.other_venue_events.get(venue, []))


def _key(venue: VenueName, source_event_id: str) -> tuple[VenueName, str]:
    return (venue, source_event_id)


def _sort_events(items: list[VenueEvent]) -> list[VenueEvent]:
    return sorted(items, key=lambda item: item.source_event_id)


def naive_pair_space(item_count: int) -> int:
    if item_count <= 1:
        return 0
    return item_count * (item_count - 1) // 2


def candidate_reduction_pct(*, naive: int, candidates: int) -> float:
    if naive <= 0:
        return 0.0
    pruned = max(0, naive - max(0, candidates))
    return round(100.0 * pruned / naive, 4)


@dataclass(frozen=True)
class _IndexRecord:
    index: int
    item: VenueEvent
    sport: str
    competition_code: str | None
    kickoff_ts: float
    squad_home: frozenset[str]
    squad_away: frozenset[str]


def _kickoff_bucket(timestamp: float, window_seconds: float) -> int:
    return int(timestamp // window_seconds)


def _index_record(index: int, item: VenueEvent) -> _IndexRecord:
    canonical = item.canonical
    kickoff = canonical.kickoff_utc
    timestamp = kickoff.timestamp()
    sport = str(getattr(canonical, "sport", "") or "")
    return _IndexRecord(
        index=index,
        item=item,
        sport=sport,
        competition_code=target_competition_code(canonical.competition),
        kickoff_ts=timestamp,
        squad_home=squad_category_fingerprint(canonical.home_team),
        squad_away=squad_category_fingerprint(canonical.away_team),
    )


def _compatible_index_pair(left: _IndexRecord, right: _IndexRecord, *, window_seconds: float) -> bool:
    if left.sport != right.sport:
        return False
    if abs(left.kickoff_ts - right.kickoff_ts) > window_seconds:
        return False
    if (
        left.competition_code is not None
        and right.competition_code is not None
        and left.competition_code != right.competition_code
    ):
        return False
    from sports_hedge.nfl.constants import NFL_SPORT

    if left.sport != NFL_SPORT and (
        left.squad_home != right.squad_home or left.squad_away != right.squad_away
    ):
        return False
    return True


INDEX_COOP_YIELD_EVERY = 1024


@dataclass
class _IndexedCandidateBuilder:
    """Deterministic indexed pair generation, with an optional cooperative yield."""

    items: list[VenueEvent]
    kickoff_tolerance: timedelta
    cache: Any | None = None

    def __post_init__(self) -> None:
        window = float(self.kickoff_tolerance.total_seconds())
        if window <= 0:
            window = 1.0
        self.window = window
        self.records = [_index_record(index, item) for index, item in enumerate(self.items)]
        self.by_sport_bucket_comp: dict[tuple[str, int, str | None], list[_IndexRecord]] = defaultdict(
            list
        )
        self.by_sport_bucket: dict[tuple[str, int], list[_IndexRecord]] = defaultdict(list)
        for record in self.records:
            bucket = _kickoff_bucket(record.kickoff_ts, window)
            self.by_sport_bucket_comp[(record.sport, bucket, record.competition_code)].append(record)
            self.by_sport_bucket[(record.sport, bucket)].append(record)
        self.seen: set[tuple[int, int]] = set()
        self.generated: list[tuple[VenueEvent, VenueEvent]] = []
        self.cache_skipped = 0

    def consider_pair(self, left: _IndexRecord, right: _IndexRecord) -> bool:
        """Return True when a new pair was generated or cache-skipped (a unit of work)."""

        if left.index >= right.index:
            return False
        key = (left.index, right.index)
        if key in self.seen:
            return False
        if not _compatible_index_pair(left, right, window_seconds=self.window):
            return False
        self.seen.add(key)
        cache = self.cache
        if cache is not None and left.item.venue is not right.item.venue:
            skip_left = cache.skip_cross_venue_against(left.item, right.item)
            skip_right = cache.skip_cross_venue_against(right.item, left.item)
            if skip_left or skip_right:
                self.cache_skipped += 1
                return True
        self.generated.append((left.item, right.item))
        return True

    def neighbour_records(self, record: _IndexRecord) -> Iterator[_IndexRecord]:
        bucket = _kickoff_bucket(record.kickoff_ts, self.window)
        neighbour_buckets = (bucket - 1, bucket, bucket + 1)
        if record.competition_code is None:
            for neighbour in neighbour_buckets:
                yield from self.by_sport_bucket.get((record.sport, neighbour), ())
            return
        for neighbour in neighbour_buckets:
            yield from self.by_sport_bucket_comp.get(
                (record.sport, neighbour, record.competition_code), ()
            )
            yield from self.by_sport_bucket_comp.get((record.sport, neighbour, None), ())

    def finish(self) -> tuple[list[tuple[VenueEvent, VenueEvent]], dict[str, int]]:
        self.generated.sort(
            key=lambda pair: (
                0 if pair[0].venue is not pair[1].venue else 1,
                pair[0].venue.value,
                pair[0].source_event_id,
                pair[1].venue.value,
                pair[1].source_event_id,
            )
        )
        naive = naive_pair_space(len(self.items))
        diagnostics = {
            "naive_pair_space": naive,
            "candidate_pairs_generated": len(self.generated),
            "pairs_pruned_by_index": max(0, naive - len(self.generated) - self.cache_skipped),
            "pairs_skipped_by_generation_cache": self.cache_skipped,
        }
        return self.generated, diagnostics

    def run_sync(self) -> tuple[list[tuple[VenueEvent, VenueEvent]], dict[str, int]]:
        for record in self.records:
            for other in self.neighbour_records(record):
                self.consider_pair(record, other)
        return self.finish()

    async def run_cooperative(
        self, *, yield_every: int = INDEX_COOP_YIELD_EVERY
    ) -> tuple[list[tuple[VenueEvent, VenueEvent]], dict[str, int]]:
        ticks = 0
        pause_every = max(1, int(yield_every))
        for record in self.records:
            for other in self.neighbour_records(record):
                self.consider_pair(record, other)
                ticks += 1
                if ticks % pause_every == 0:
                    await asyncio.sleep(0)
        return self.finish()


def build_indexed_candidates(
    items: list[VenueEvent],
    *,
    kickoff_tolerance: timedelta,
    cache: Any | None = None,
) -> tuple[list[tuple[VenueEvent, VenueEvent]], dict[str, int]]:
    """Correctness-preserving superset of EventMatcher-eligible pairs.

    Blocks on sport, known target competition, overlapping kickoff windows,
    and squad-category compatibility. Unresolved competitions stay in a
    fallback path that pairs against every same-sport window neighbour so
    they are never dropped for speed. Kickoff buckets overlap by ±1 window
    so a pair on a 5-minute boundary is not a false negative.
    """

    return _IndexedCandidateBuilder(
        items=items, kickoff_tolerance=kickoff_tolerance, cache=cache
    ).run_sync()


async def build_indexed_candidates_cooperative(
    items: list[VenueEvent],
    *,
    kickoff_tolerance: timedelta,
    cache: Any | None = None,
    yield_every: int = INDEX_COOP_YIELD_EVERY,
) -> tuple[list[tuple[VenueEvent, VenueEvent]], dict[str, int]]:
    """Same candidate set as ``build_indexed_candidates``, yielding to the event loop."""

    return await _IndexedCandidateBuilder(
        items=items, kickoff_tolerance=kickoff_tolerance, cache=cache
    ).run_cooperative(yield_every=yield_every)


def universe_cluster_sort_key(cluster: FixtureCluster) -> tuple[int, str]:
    """Multi-venue clusters first, then stable canonical id."""

    return (-cluster.venue_count, cluster_canonical_event_id(cluster))


class ClusterPass:
    """Deterministic union-find clustering over an indexed candidate set."""

    def __init__(
        self,
        *,
        matchbook: list[VenueEvent],
        polymarket: list[VenueEvent],
        kalshi: list[VenueEvent],
        matcher: EventMatcher,
        max_event_pairs: int,
        identity_cache: Any | None = None,
        extra: list[VenueEvent] | None = None,
        incremental_cache: CrossGenerationIdentityCache | None = None,
        defer_candidate_build: bool = False,
    ) -> None:
        if max_event_pairs <= 0:
            raise ValueError("max_event_pairs must be positive")
        self.max_event_pairs = max_event_pairs
        self.items = [*matchbook, *polymarket, *kalshi, *(extra or [])]
        self.parent: dict[tuple[VenueName, str], tuple[VenueName, str]] = {}
        self.nodes: dict[tuple[VenueName, str], VenueEvent] = {}
        self.pair_kinds: dict[tuple[VenueName, str], set[str]] = {}
        self.scored_pairs: list[ScoredIdentityPair] = []
        self.graph_diagnostics: dict[str, int] = {}
        for item in self.items:
            key = _key(item.venue, item.source_event_id)
            self.nodes[key] = item
            self.parent.setdefault(key, key)
        snapshot_for_bulk = getattr(matcher, "bulk_snapshot", None)
        self.bulk_matcher = snapshot_for_bulk() if callable(snapshot_for_bulk) else matcher
        self.could_match = getattr(self.bulk_matcher, "could_match", None)
        self.match_confidence: dict[tuple[VenueName, str], float] = {}
        self.identity_cache = identity_cache
        self.incremental_cache = incremental_cache
        self.semantic_version = identity_cache_semantic_version(self.bulk_matcher)
        self._fingerprints = {
            event_cache_key(item): event_identity_fingerprint(item) for item in self.items
        }
        self.incremental_diagnostics = (
            incremental_cache.classify(self.items, self.semantic_version)
            if incremental_cache is not None
            else IncrementalIdentityDiagnostics(
                discovered=len(self.items),
                new=len(self.items),
                semantic_version=self.semantic_version,
            )
        )
        kickoff_tolerance = getattr(
            self.bulk_matcher, "kickoff_tolerance", timedelta(minutes=5)
        )
        self._kickoff_tolerance = kickoff_tolerance
        self._candidates: list[tuple[VenueEvent, VenueEvent]] = []
        self.index_diagnostics: dict[str, int] = {}
        self.candidate_pairs_considered = 0
        self._resume_cursor = 0
        if not defer_candidate_build:
            self._load_candidates_sync()

    def _apply_resume_cursor(self) -> None:
        cache = self.identity_cache
        if cache is None:
            return
        resume = cache.take_clustering_resume(self.items)
        if resume is None:
            return
        self.parent = dict(resume.parent)
        self.match_confidence = dict(resume.match_confidence)
        self.pair_kinds = {key: set(value) for key, value in resume.pair_kinds.items()}
        self.scored_pairs = list(getattr(resume, "scored_pairs", ()) or ())
        self._resume_cursor = min(resume.cursor, len(self._candidates))
        self.candidate_pairs_considered = self._resume_cursor

    def _load_candidates_sync(self) -> None:
        self._candidates, self.index_diagnostics = build_indexed_candidates(
            self.items,
            kickoff_tolerance=self._kickoff_tolerance,
            cache=self.identity_cache,
        )
        self._apply_resume_cursor()

    async def load_candidates_cooperative(
        self, *, yield_every: int = INDEX_COOP_YIELD_EVERY
    ) -> None:
        self._candidates, self.index_diagnostics = await build_indexed_candidates_cooperative(
            self.items,
            kickoff_tolerance=self._kickoff_tolerance,
            cache=self.identity_cache,
            yield_every=yield_every,
        )
        self._apply_resume_cursor()

    def pairs(self) -> Iterator[tuple[VenueEvent, VenueEvent]]:
        for pair in self._candidates[self._resume_cursor :]:
            yield pair

    def clustering_diagnostics(self, *, truncated: bool, duration_ms: int = 0) -> dict[str, Any]:
        naive = int(self.index_diagnostics.get("naive_pair_space") or 0)
        generated = int(self.index_diagnostics.get("candidate_pairs_generated") or 0)
        considered = int(self.candidate_pairs_considered)
        pruned = int(self.index_diagnostics.get("pairs_pruned_by_index") or 0)
        saved = int(self.incremental_diagnostics.saved_candidate_comparisons)
        if generated > 0:
            self.incremental_diagnostics.cache_hit_pct = round(
                100.0 * saved / generated, 4
            )
        else:
            self.incremental_diagnostics.cache_hit_pct = 0.0
        return {
            **self.index_diagnostics,
            "candidate_pairs_considered": considered,
            "pairs_pruned_by_index": pruned,
            "candidate_reduction_pct": candidate_reduction_pct(
                naive=naive, candidates=generated
            ),
            "clustering_truncated": bool(truncated),
            "clustering_duration_ms": max(0, int(duration_ms)),
            **self.graph_diagnostics,
            **self.incremental_diagnostics.as_dict(),
        }

    def checkpoint(self, cursor: int) -> None:
        cache = self.identity_cache
        if cache is None:
            return
        cache.store_clustering_resume(
            items=self.items,
            cursor=cursor,
            parent=self.parent,
            match_confidence=self.match_confidence,
            pair_kinds=self.pair_kinds,
            scored_pairs=list(self.scored_pairs),
        )

    def record_generation_negatives(self, clusters: list[FixtureCluster]) -> None:
        cache = self.identity_cache
        if cache is None:
            return
        venues = {item.venue for item in self.items}
        other_keys_by_venue = {
            venue: frozenset(
                (item.venue.value, item.source_event_id)
                for item in self.items
                if item.venue is not venue
            )
            for venue in venues
        }
        for cluster in clusters:
            if cluster.venue_count >= 2:
                continue
            for item in cluster_member_events(cluster):
                cache.record_single_venue(
                    item,
                    other_venue_keys=other_keys_by_venue.get(item.venue, frozenset()),
                )

    def _find(self, key: tuple[VenueName, str]) -> tuple[VenueName, str]:
        parent = self.parent
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def _union_keys(self, left: tuple[VenueName, str], right: tuple[VenueName, str]) -> None:
        a = self._find(left)
        b = self._find(right)
        if a != b:
            self.parent[b] = a

    def commit_incremental_snapshot(self) -> None:
        cache = self.incremental_cache
        if cache is None:
            return
        cache.commit_snapshot(self.items, self.scored_pairs, self.semantic_version)

    def consider(self, left: VenueEvent, right: VenueEvent) -> None:
        """Score one indexed candidate. Clustering happens in ``finalize``."""

        self.candidate_pairs_considered += 1
        cache = self.incremental_cache
        if cache is not None:
            reused = cache.reuse_pair(
                left,
                right,
                self._fingerprints,
                self.semantic_version,
            )
            if reused is not None:
                self.scored_pairs.append(reused)
                self.incremental_diagnostics.saved_candidate_comparisons += 1
                return
        left_key = _key(left.venue, left.source_event_id)
        right_key = _key(right.venue, right.source_event_id)
        could_match = self.could_match
        if callable(could_match) and not could_match(left.canonical, right.canonical):
            self.scored_pairs.append(
                ScoredIdentityPair.from_endpoints(
                    left_key,
                    right_key,
                    confidence=0.0,
                    reasons=["prefilter_rejected"],
                    matched=False,
                    veto=True,
                )
            )
            return
        match = self.bulk_matcher.match(left.canonical, right.canonical)
        veto = is_hard_identity_veto(
            match.reasons, matched=match.matched, confidence=match.confidence
        )
        self.scored_pairs.append(
            ScoredIdentityPair.from_endpoints(
                left_key,
                right_key,
                confidence=match.confidence,
                reasons=match.reasons,
                matched=match.matched,
                veto=veto,
            )
        )

    def _unscored_identity_nodes(self) -> set[tuple[VenueName, str]]:
        """Endpoints of indexed candidates that have not been scored yet."""

        nodes: set[tuple[VenueName, str]] = set()
        remaining = self._candidates[self.candidate_pairs_considered :]
        for left, right in remaining:
            nodes.add(_key(left.venue, left.source_event_id))
            nodes.add(_key(right.venue, right.source_event_id))
            if len(nodes) >= len(self.nodes):
                break
        return nodes

    def _apply_identity_graph(
        self,
        threshold: float,
        *,
        unscored_nodes: set[tuple[VenueName, str]] | None = None,
    ) -> dict[tuple[VenueName, str], IdentityAssignmentProvenance]:
        result = assign_identity_components(
            list(self.nodes.keys()),
            self.scored_pairs,
            unscored_nodes=(
                self._unscored_identity_nodes() if unscored_nodes is None else unscored_nodes
            ),
            threshold=threshold,
        )
        self.graph_diagnostics = result.diagnostics.as_dict()
        self.parent = {key: key for key in self.nodes}
        self.match_confidence = {}
        self.pair_kinds = {}
        provenance_by_member: dict[tuple[VenueName, str], IdentityAssignmentProvenance] = {}
        for assigned in result.clusters:
            members = sorted(assigned.member_keys, key=node_sort_key)
            for member in members:
                provenance_by_member[member] = assigned.provenance
            for member in members[1:]:
                self._union_keys(members[0], member)
            for edge in assigned.provenance.chosen_edges:
                for key in (edge.left, edge.right):
                    previous = self.match_confidence.get(key)
                    if previous is None or edge.confidence < previous:
                        self.match_confidence[key] = edge.confidence
                if edge.left[0] is edge.right[0]:
                    continue
                kind = pair_kind(edge.left[0], edge.right[0])
                self.pair_kinds.setdefault(edge.left, set()).add(kind)
                self.pair_kinds.setdefault(edge.right, set()).add(kind)
        return provenance_by_member

    def finalize(self) -> tuple[list[FixtureCluster], dict[str, int]]:
        threshold = float(getattr(self.bulk_matcher, "threshold", 0.92))
        provenance_by_member = self._apply_identity_graph(threshold)
        return self._assemble_clusters(threshold, provenance_by_member)

    async def _unscored_identity_nodes_cooperative(
        self, *, yield_every: int = INDEX_COOP_YIELD_EVERY
    ) -> set[tuple[VenueName, str]]:
        nodes: set[tuple[VenueName, str]] = set()
        remaining = self._candidates[self.candidate_pairs_considered :]
        pause_every = max(1, int(yield_every))
        for index, (left, right) in enumerate(remaining):
            if index % pause_every == 0:
                await asyncio.sleep(0)
            nodes.add(_key(left.venue, left.source_event_id))
            nodes.add(_key(right.venue, right.source_event_id))
            if len(nodes) >= len(self.nodes):
                break
        return nodes

    async def finalize_cooperative(
        self, *, yield_every: int = 64
    ) -> tuple[list[FixtureCluster], dict[str, int]]:
        threshold = float(getattr(self.bulk_matcher, "threshold", 0.92))
        unscored_nodes = await self._unscored_identity_nodes_cooperative()
        provenance_by_member = self._apply_identity_graph(
            threshold, unscored_nodes=unscored_nodes
        )
        await asyncio.sleep(0)
        pause_every = max(1, int(yield_every))
        grouped: dict[tuple[VenueName, str], FixtureCluster] = {}
        for index, (key, item) in enumerate(self.nodes.items()):
            if index % pause_every == 0:
                await asyncio.sleep(0)
            root = self._find(key)
            cluster = grouped.setdefault(root, FixtureCluster())
            _append_cluster_event(cluster, item)
            cluster.pair_kinds.update(self.pair_kinds.get(key, set()))
            if cluster.identity_provenance is None:
                cluster.identity_provenance = provenance_by_member.get(key)
        return self._finish_grouped_clusters(threshold, grouped)

    def _assemble_clusters(
        self,
        threshold: float,
        provenance_by_member: dict[tuple[VenueName, str], IdentityAssignmentProvenance],
    ) -> tuple[list[FixtureCluster], dict[str, int]]:
        grouped: dict[tuple[VenueName, str], FixtureCluster] = {}
        for key, item in self.nodes.items():
            root = self._find(key)
            cluster = grouped.setdefault(root, FixtureCluster())
            _append_cluster_event(cluster, item)
            cluster.pair_kinds.update(self.pair_kinds.get(key, set()))
            if cluster.identity_provenance is None:
                cluster.identity_provenance = provenance_by_member.get(key)
        return self._finish_grouped_clusters(threshold, grouped)

    def _finish_grouped_clusters(
        self,
        threshold: float,
        grouped: dict[tuple[VenueName, str], FixtureCluster],
    ) -> tuple[list[FixtureCluster], dict[str, int]]:
        clusters: list[FixtureCluster] = []
        for cluster in grouped.values():
            cluster.matchbook_events = _sort_events(cluster.matchbook_events)
            cluster.polymarket_events = _sort_events(cluster.polymarket_events)
            cluster.kalshi_events = _sort_events(cluster.kalshi_events)
            cluster.other_venue_events = {
                venue: _sort_events(events)
                for venue, events in sorted(
                    cluster.other_venue_events.items(), key=lambda item: item[0].value
                )
            }
            confidences = [
                self.match_confidence[_key(item.venue, item.source_event_id)]
                for item in cluster_member_events(cluster)
                if _key(item.venue, item.source_event_id) in self.match_confidence
            ]
            cluster.event_match_threshold = threshold
            cluster.event_match_confidence = min(confidences) if confidences else None
            clusters.append(cluster)

        counts = {
            "matchbook_polymarket": sum(
                1 for item in clusters if item.matchbook_events and item.polymarket_events
            ),
            "matchbook_kalshi": sum(
                1 for item in clusters if item.matchbook_events and item.kalshi_events
            ),
            "polymarket_kalshi": sum(
                1 for item in clusters if item.polymarket_events and item.kalshi_events
            ),
        }
        return clusters, counts


def _append_cluster_event(cluster: FixtureCluster, item: VenueEvent) -> None:
    if item.venue is VenueName.MATCHBOOK:
        cluster.matchbook_events.append(item)
        return
    if item.venue is VenueName.POLYMARKET:
        cluster.polymarket_events.append(item)
        return
    if item.venue is VenueName.KALSHI:
        cluster.kalshi_events.append(item)
        return
    cluster.other_venue_events.setdefault(item.venue, []).append(item)


def cluster_venue_events(
    *,
    matchbook: list[VenueEvent],
    polymarket: list[VenueEvent],
    kalshi: list[VenueEvent],
    matcher: EventMatcher,
    max_event_pairs: int,
    identity_cache: Any | None = None,
    extra: list[VenueEvent] | None = None,
    incremental_cache: CrossGenerationIdentityCache | None = None,
) -> tuple[list[FixtureCluster], dict[str, int]]:
    """Cluster independently discovered venue events by canonical fixture identity.

    Indexed candidate generation is the fast first stage. EventMatcher hard
    vetoes and thresholds remain the only pairwise identity evidence. Obvious
    connected components still union, including same-venue sibling market-family
    events. Ambiguous components use constrained maximum-weight assignment
    instead of greedy local pair choice. Contradictory or low-margin components
    fail closed with provenance rather than forcing a match.

    The graph is generic over 2+ venues. Matchbook is not required. Unmatched
    single-venue leftovers remain visible.

    When ``incremental_cache`` is supplied, unchanged source events may reuse
    prior EventMatcher scores if fingerprints and the semantic version match.
    Clean full recomputation (this function without the cache) remains the
    correctness oracle.
    """

    cluster_pass = ClusterPass(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=matcher,
        max_event_pairs=max_event_pairs,
        identity_cache=identity_cache,
        extra=extra,
        incremental_cache=incremental_cache,
    )
    for left, right in cluster_pass.pairs():
        cluster_pass.consider(left, right)
    clusters, counts = cluster_pass.finalize()
    cluster_pass.checkpoint(len(cluster_pass._candidates))
    cluster_pass.record_generation_negatives(clusters)
    cluster_pass.commit_incremental_snapshot()
    return clusters, counts


def to_venue_event(normalized: object, venue: VenueName) -> VenueEvent:
    canonical = getattr(normalized, "canonical")
    return VenueEvent(
        venue=venue,
        raw=getattr(normalized, "raw"),
        canonical=canonical,
        source_event_id=str(canonical.source_event_id),
    )


def cluster_canonical_event_id(cluster: FixtureCluster) -> str:
    return canonical_source_event_id(cluster.anchor.canonical)


def cluster_member_keyset(cluster: FixtureCluster) -> frozenset[tuple[str, str]]:
    """Stable membership signature used to compare incremental vs full recompute."""

    return frozenset(
        (item.venue.value, str(item.source_event_id))
        for item in cluster_member_events(cluster)
    )


def cluster_member_events(cluster: FixtureCluster) -> list[VenueEvent]:
    extras = [
        event
        for venue in sorted(cluster.other_venue_events, key=lambda item: item.value)
        for event in cluster.other_venue_events[venue]
    ]
    return [
        *cluster.matchbook_events,
        *cluster.polymarket_events,
        *cluster.kalshi_events,
        *extras,
    ]


def cluster_identity_aliases(cluster: FixtureCluster) -> dict[str, str]:
    """Explicit pair-level and source-id aliases for one collector-cluster fixture.

    Pair paper decisions hash `canonical_matched_event_id` over the two venue
    events in that scan. Cluster rows hash `canonical_source_event_id` of the
    cluster anchor. Those strings are not the same when a pair is a subset of a
    three-venue cluster. Navigation must use this map, never fixture-name fuzzy
    matching.
    """

    canonical_id = cluster_canonical_event_id(cluster)
    aliases = {canonical_id: canonical_id}
    members = cluster_member_events(cluster)
    for item in members:
        source_id = str(item.source_event_id).strip()
        if source_id:
            aliases[source_id] = canonical_id
    for index, left in enumerate(members):
        for right in members[index + 1 :]:
            if left.venue is right.venue:
                continue
            aliases[canonical_matched_event_id([left.canonical, right.canonical])] = canonical_id
    return aliases
