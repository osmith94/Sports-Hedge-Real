from __future__ import annotations

import asyncio
import time
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
    assign_identity_components_cooperative,
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


def identity_name_block_keys(item: VenueEvent) -> frozenset[str]:
    """Conservative name canopy used only inside hot/unresolved shards.

    Keys are 3-letter prefixes of tokens with at least four letters. Shorter
    names fail open (``*short*``) so abbreviations such as ``PSG`` or ``Man Utd``
    are still paired inside the kickoff window. This is not a second matcher.
    """

    from sports_hedge.normalization.text import normalize_text

    keys: set[str] = set()
    significant = False
    for raw_name in (
        getattr(item.canonical, "home_team", "") or "",
        getattr(item.canonical, "away_team", "") or "",
    ):
        normalized = normalize_text(str(raw_name))
        tokens = [token for token in normalized.split() if token]
        if not tokens:
            keys.add(_SHORT_BLOCK)
            continue
        for token in tokens:
            letters = "".join(char for char in token if char.isalpha())
            if len(letters) < 4:
                keys.add(_SHORT_BLOCK)
                continue
            significant = True
            keys.add(letters[:3])
    if not significant:
        keys.add(_SHORT_BLOCK)
    return frozenset(keys)


def identity_name_block_overlap(left: VenueEvent, right: VenueEvent) -> bool:
    left_keys = identity_name_block_keys(left)
    right_keys = identity_name_block_keys(right)
    if _SHORT_BLOCK in left_keys or _SHORT_BLOCK in right_keys:
        return True
    return bool(left_keys & right_keys)


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
# Count-based yields can still monopolise the loop when each tick is expensive
# after a large pytest/process heap. Cap a cooperative slice well inside the
# 0.25s event-loop liveness bound.
COOP_MAX_SLICE_SECONDS = 0.05
# Node-local then kickoff-local candidate order. Completing one source event's
# incident pairs lets truncated finalize assign that fixture instead of waiting
# for the global cross-venue prefix to finish.
CANDIDATE_ORDER_VERSION = 2
# Secondary name blocking uses the next candidate-list version so a shard that
# flips into the hot path does not reuse an unblocked candidate list.
SECONDARY_BLOCK_ORDER_VERSION = CANDIDATE_ORDER_VERSION + 1
# Intra-component veto pairs are restored up to this size. Larger components
# stay on the secondary-block candidate set and are reported as hot.
SECONDARY_CLIQUE_CAP = 24
_SHORT_BLOCK = "*short*"


def _coop_pause_due(ticks: int, pause_every: int, last_yield: float) -> bool:
    if ticks % max(1, int(pause_every)) == 0:
        return True
    return (time.perf_counter() - last_yield) >= COOP_MAX_SLICE_SECONDS


def _node_sort_tuple(item: VenueEvent) -> tuple[str, str]:
    return (item.venue.value, item.source_event_id)


def _event_locality(item: VenueEvent) -> tuple[float, str, str]:
    """Cheap same-kickoff / same-label group used only to order candidate work.

    Completing every incident pair of the earliest locality lets truncated
    finalize assign that fixture. This is not a second identity system.
    """

    home = str(getattr(item.canonical, "home_team", "") or "").casefold()
    away = str(getattr(item.canonical, "away_team", "") or "").casefold()
    kickoff = item.canonical.kickoff_utc.timestamp()
    return (kickoff, min(home, away), max(home, away))


def _candidate_progress_key(pair: tuple[VenueEvent, VenueEvent]) -> tuple:
    left, right = pair
    loc_left = _event_locality(left)
    loc_right = _event_locality(right)
    first_loc, second_loc = (
        (loc_left, loc_right) if loc_left <= loc_right else (loc_right, loc_left)
    )
    left_node = _node_sort_tuple(left)
    right_node = _node_sort_tuple(right)
    first_node, second_node = (
        (left_node, right_node) if left_node <= right_node else (right_node, left_node)
    )
    return (
        first_loc,
        0 if left.venue is not right.venue else 1,
        second_loc,
        first_node,
        second_node,
    )


@dataclass
class _IndexedCandidateBuilder:
    """Deterministic indexed pair generation, with an optional cooperative yield."""

    items: list[VenueEvent]
    kickoff_tolerance: timedelta
    cache: Any | None = None
    matcher: Any | None = None
    secondary_block: bool = False

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
        self.could_match_rejected = 0
        self.secondary_blocked = 0
        self.max_could_match_component = 0
        self.secondary_clique_capped_components = 0
        self.unresolved_competition_events = sum(
            1 for record in self.records if record.competition_code is None
        )
        self.max_sport_bucket_size = max(
            (len(group) for group in self.by_sport_bucket.values()),
            default=0,
        )
        self.sport_bucket_count = len(self.by_sport_bucket)
        self.could_match = getattr(self.matcher, "could_match", None)

    def _cache_skip(self, left: _IndexRecord, right: _IndexRecord) -> bool:
        cache = self.cache
        if cache is None or left.item.venue is right.item.venue:
            return False
        return bool(
            cache.skip_cross_venue_against(left.item, right.item)
            or cache.skip_cross_venue_against(right.item, left.item)
        )

    def _could_match_pair(self, left: _IndexRecord, right: _IndexRecord) -> bool:
        could_match = self.could_match
        if not callable(could_match):
            return True
        return bool(could_match(left.item.canonical, right.item.canonical))

    def _accepted_compatible_pair(
        self, record: _IndexRecord, other: _IndexRecord
    ) -> tuple[_IndexRecord, _IndexRecord] | None:
        if record.index >= other.index:
            return None
        key = (record.index, other.index)
        if key in self.seen:
            return None
        if not _compatible_index_pair(record, other, window_seconds=self.window):
            return None
        self.seen.add(key)
        return record, other

    def _iter_compatible_pairs(self) -> Iterator[tuple[_IndexRecord, _IndexRecord]]:
        for record in self.records:
            for other in self.neighbour_records(record):
                pair = self._accepted_compatible_pair(record, other)
                if pair is not None:
                    yield pair

    def _new_component_parent(self) -> dict[int, int]:
        return {record.index: record.index for record in self.records}

    def _find_component(self, parent: dict[int, int], index: int) -> int:
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def _union_component(self, parent: dict[int, int], left: int, right: int) -> None:
        root_left = self._find_component(parent, left)
        root_right = self._find_component(parent, right)
        if root_left != root_right:
            parent[root_right] = root_left

    def _emit_component_pairs(
        self,
        compatible: list[tuple[_IndexRecord, _IndexRecord]],
        parent: dict[int, int] | None = None,
    ) -> None:
        """Keep every compatible pair inside a could-match component.

        Dropping a could_match-False pair that sits on a matching path would
        hide veto/miss evidence and can false-cluster through a third venue.
        Isolated events (no could_match edge) generate no candidates.
        """

        if parent is None:
            parent = self._new_component_parent()
            for left, right in compatible:
                if self._could_match_pair(left, right):
                    self._union_component(parent, left.index, right.index)
        sizes: dict[int, int] = defaultdict(int)
        for record in self.records:
            sizes[self._find_component(parent, record.index)] += 1
        if sizes:
            self.max_could_match_component = max(
                self.max_could_match_component, max(sizes.values())
            )
        for left, right in compatible:
            if self._cache_skip(left, right):
                self.cache_skipped += 1
                continue
            root = self._find_component(parent, left.index)
            if root != self._find_component(parent, right.index) or sizes[root] < 2:
                self.could_match_rejected += 1
                continue
            self.generated.append((left.item, right.item))

    async def _emit_component_pairs_cooperative(
        self,
        compatible: list[tuple[_IndexRecord, _IndexRecord]],
        parent: dict[int, int],
        *,
        yield_every: int,
    ) -> None:
        pause_every = max(1, int(yield_every))
        sizes: dict[int, int] = defaultdict(int)
        last_yield = time.perf_counter()
        for ticks, record in enumerate(self.records, start=1):
            sizes[self._find_component(parent, record.index)] += 1
            if _coop_pause_due(ticks, pause_every, last_yield):
                await asyncio.sleep(0)
                last_yield = time.perf_counter()
        if sizes:
            self.max_could_match_component = max(
                self.max_could_match_component, max(sizes.values())
            )
        for ticks, (left, right) in enumerate(compatible, start=1):
            if self._cache_skip(left, right):
                self.cache_skipped += 1
            else:
                root = self._find_component(parent, left.index)
                if root != self._find_component(parent, right.index) or sizes[root] < 2:
                    self.could_match_rejected += 1
                else:
                    self.generated.append((left.item, right.item))
            if _coop_pause_due(ticks, pause_every, last_yield):
                await asyncio.sleep(0)
                last_yield = time.perf_counter()

    def consider_pair(self, left: _IndexRecord, right: _IndexRecord) -> bool:
        """Compatibility helper used by tests that walk the builder directly."""

        if left.index >= right.index:
            return False
        key = (left.index, right.index)
        if key in self.seen:
            return False
        if not _compatible_index_pair(left, right, window_seconds=self.window):
            return False
        self.seen.add(key)
        if self._cache_skip(left, right):
            self.cache_skipped += 1
            return True
        if not self._could_match_pair(left, right):
            self.could_match_rejected += 1
            return True
        self.generated.append((left.item, right.item))
        return True

    def _bucket_neighbours(self, record: _IndexRecord) -> Iterator[_IndexRecord]:
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

    def neighbour_records(self, record: _IndexRecord) -> Iterator[_IndexRecord]:
        for other in self._bucket_neighbours(record):
            if other.index == record.index:
                continue
            if self.secondary_block and not identity_name_block_overlap(record.item, other.item):
                if record.index < other.index:
                    self.secondary_blocked += 1
                continue
            yield other

    def _augment_component_cliques(
        self,
        compatible: list[tuple[_IndexRecord, _IndexRecord]],
        parent: dict[int, int],
    ) -> list[tuple[_IndexRecord, _IndexRecord]]:
        """Put veto pairs back inside small could-match components.

        Secondary blocking can omit a name-dissimilar pair that still sits on a
        matching path. Components up to ``SECONDARY_CLIQUE_CAP`` regain those
        pairs. Larger components are reported and keep the blocked candidate set.
        """

        if not self.secondary_block:
            return compatible
        sizes: dict[int, int] = defaultdict(int)
        members: dict[int, list[_IndexRecord]] = defaultdict(list)
        for record in self.records:
            root = self._find_component(parent, record.index)
            sizes[root] += 1
            members[root].append(record)
        existing = {
            (left.index, right.index) if left.index < right.index else (right.index, left.index)
            for left, right in compatible
        }
        augmented = list(compatible)
        for root, group in members.items():
            if sizes[root] < 2:
                continue
            if sizes[root] > SECONDARY_CLIQUE_CAP:
                self.secondary_clique_capped_components += 1
                continue
            for left_index, left in enumerate(group):
                for right in group[left_index + 1 :]:
                    key = (
                        (left.index, right.index)
                        if left.index < right.index
                        else (right.index, left.index)
                    )
                    if key in existing:
                        continue
                    if not _compatible_index_pair(left, right, window_seconds=self.window):
                        continue
                    existing.add(key)
                    self.secondary_blocked = max(0, self.secondary_blocked - 1)
                    augmented.append((left, right) if left.index < right.index else (right, left))
        return augmented

    def _index_diagnostics(self) -> dict[str, Any]:
        naive = naive_pair_space(len(self.items))
        return {
            "naive_pair_space": naive,
            "candidate_pairs_generated": len(self.generated),
            "pairs_pruned_by_index": max(
                0,
                naive
                - len(self.generated)
                - self.cache_skipped
                - self.could_match_rejected
                - self.secondary_blocked,
            ),
            "pairs_skipped_by_generation_cache": self.cache_skipped,
            "pairs_rejected_by_could_match": self.could_match_rejected,
            "unresolved_competition_events": self.unresolved_competition_events,
            "max_sport_bucket_size": self.max_sport_bucket_size,
            "sport_bucket_count": self.sport_bucket_count,
            "candidate_order_version": (
                SECONDARY_BLOCK_ORDER_VERSION if self.secondary_block else CANDIDATE_ORDER_VERSION
            ),
            "secondary_block": bool(self.secondary_block),
            "pairs_blocked_before_could_match": self.secondary_blocked,
            "max_could_match_component": self.max_could_match_component,
            "secondary_clique_capped_components": self.secondary_clique_capped_components,
        }

    def finish(self) -> tuple[list[tuple[VenueEvent, VenueEvent]], dict[str, Any]]:
        self.generated.sort(key=_candidate_progress_key)
        return self.generated, self._index_diagnostics()

    async def finish_cooperative(
        self, *, yield_every: int = INDEX_COOP_YIELD_EVERY
    ) -> tuple[list[tuple[VenueEvent, VenueEvent]], dict[str, Any]]:
        """Same candidate order as ``finish``, yielding while decorating sort keys."""

        pause_every = max(1, int(yield_every))
        last_yield = time.perf_counter()
        decorated: list[tuple[Any, tuple[VenueEvent, VenueEvent]]] = []
        for ticks, pair in enumerate(self.generated, start=1):
            decorated.append((_candidate_progress_key(pair), pair))
            if _coop_pause_due(ticks, pause_every, last_yield):
                await asyncio.sleep(0)
                last_yield = time.perf_counter()
        decorated.sort(key=lambda item: item[0])
        await asyncio.sleep(0)
        self.generated = [pair for _, pair in decorated]
        return self.generated, self._index_diagnostics()

    def run_sync(self) -> tuple[list[tuple[VenueEvent, VenueEvent]], dict[str, Any]]:
        compatible = list(self._iter_compatible_pairs())
        parent = self._new_component_parent()
        for left, right in compatible:
            if self._could_match_pair(left, right):
                self._union_component(parent, left.index, right.index)
        compatible = self._augment_component_cliques(compatible, parent)
        self._emit_component_pairs(compatible, parent)
        return self.finish()

    async def run_cooperative(
        self, *, yield_every: int = INDEX_COOP_YIELD_EVERY
    ) -> tuple[list[tuple[VenueEvent, VenueEvent]], dict[str, Any]]:
        pause_every = max(1, int(yield_every))
        compatible: list[tuple[_IndexRecord, _IndexRecord]] = []
        parent = self._new_component_parent()
        last_yield = time.perf_counter()
        ticks = 0
        try:
            # Pause on neighbour visits, not only yielded pairs. Same-kickoff
            # unresolved buckets spend most of the walk skipping the upper
            # triangle; counting only emitted pairs lets that drain starve
            # the 0.25s event-loop bound. Secondary name blocking still filters
            # inside neighbour_records; clique augmentation restores veto pairs
            # inside small could-match components afterwards.
            for record in self.records:
                for other in self.neighbour_records(record):
                    ticks += 1
                    pair = self._accepted_compatible_pair(record, other)
                    if pair is not None:
                        compatible.append(pair)
                        if self._could_match_pair(*pair):
                            self._union_component(parent, pair[0].index, pair[1].index)
                    if _coop_pause_due(ticks, pause_every, last_yield):
                        await asyncio.sleep(0)
                        last_yield = time.perf_counter()
            compatible = self._augment_component_cliques(compatible, parent)
            await self._emit_component_pairs_cooperative(
                compatible, parent, yield_every=pause_every
            )
            await asyncio.sleep(0)
        except asyncio.CancelledError:
            if compatible and not self.generated:
                compatible = self._augment_component_cliques(compatible, parent)
                self._emit_component_pairs(compatible, parent)
            raise
        return await self.finish_cooperative(yield_every=pause_every)


def build_indexed_candidates(
    items: list[VenueEvent],
    *,
    kickoff_tolerance: timedelta,
    cache: Any | None = None,
    matcher: Any | None = None,
    secondary_block: bool = False,
) -> tuple[list[tuple[VenueEvent, VenueEvent]], dict[str, Any]]:
    """Correctness-preserving superset of EventMatcher-eligible pairs.

    Blocks on sport, known target competition, overlapping kickoff windows,
    and squad-category compatibility. Unresolved competitions stay in a
    fallback path that pairs against every same-sport window neighbour so
    they are never dropped for speed. Kickoff buckets overlap by ±1 window
    so a pair on a 5-minute boundary is not a false negative.

    When ``matcher`` exposes ``could_match``, index generation first builds
    the could-match graph and then keeps every compatible pair inside a
    connected component. That preserves veto/miss evidence on matching
    paths (no false clusters) while dropping pairs that cannot share a
    component. ``False`` from ``could_match`` still cannot hide a pair that
    sits on a matching path.
    """

    return _IndexedCandidateBuilder(
        items=items,
        kickoff_tolerance=kickoff_tolerance,
        cache=cache,
        matcher=matcher,
        secondary_block=secondary_block,
    ).run_sync()


async def build_indexed_candidates_cooperative(
    items: list[VenueEvent],
    *,
    kickoff_tolerance: timedelta,
    cache: Any | None = None,
    matcher: Any | None = None,
    yield_every: int = INDEX_COOP_YIELD_EVERY,
    secondary_block: bool = False,
) -> tuple[list[tuple[VenueEvent, VenueEvent]], dict[str, Any]]:
    """Same candidate set as ``build_indexed_candidates``, yielding to the event loop."""

    return await _IndexedCandidateBuilder(
        items=items,
        kickoff_tolerance=kickoff_tolerance,
        cache=cache,
        matcher=matcher,
        secondary_block=secondary_block,
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
        secondary_block: bool = False,
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
        self._bound_generation_id = getattr(identity_cache, "generation_id", None)
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
        self.index_diagnostics: dict[str, Any] = {}
        self.candidate_pairs_considered = 0
        self._resume_cursor = 0
        self.secondary_block = bool(secondary_block)
        self._candidate_order_version = (
            SECONDARY_BLOCK_ORDER_VERSION if self.secondary_block else CANDIDATE_ORDER_VERSION
        )
        self.last_unscored_nodes: set[tuple[VenueName, str]] = set()
        self.resume_diagnostics: dict[str, Any] = {
            "clustering_resume_applied": False,
            "clustering_resume_signature_mismatch": False,
            "clustering_resume_candidates_reused": False,
            "clustering_resume_cursor_before": 0,
            "clustering_resume_cursor_after": 0,
        }
        if not defer_candidate_build:
            self._load_candidates_sync()

    def _candidate_keys(self) -> list[tuple[tuple[str, str], tuple[str, str]]]:
        return [
            (
                (left.venue.value, left.source_event_id),
                (right.venue.value, right.source_event_id),
            )
            for left, right in self._candidates
        ]

    def _restore_resume_state(self, resume) -> None:
        self.parent = dict(resume.parent)
        self.match_confidence = dict(resume.match_confidence)
        self.pair_kinds = {key: set(value) for key, value in resume.pair_kinds.items()}
        self.scored_pairs = list(getattr(resume, "scored_pairs", ()) or ())
        self._resume_cursor = min(resume.cursor, len(self._candidates))
        self.candidate_pairs_considered = self._resume_cursor
        self.resume_diagnostics["clustering_resume_applied"] = True
        self.resume_diagnostics["clustering_resume_cursor_before"] = int(resume.cursor)
        self.resume_diagnostics["clustering_resume_cursor_after"] = self._resume_cursor

    def _apply_resume_cursor(self) -> None:
        cache = self.identity_cache
        if cache is None:
            return
        snapshot = cache.clustering_resume
        if snapshot is not None and snapshot.items_signature != cache.items_signature(
            self.items
        ):
            self.resume_diagnostics["clustering_resume_signature_mismatch"] = True
        resume = cache.take_clustering_resume(self.items)
        if resume is None:
            return
        stored_version = int(getattr(resume, "candidate_order_version", 1) or 1)
        if stored_version != self._candidate_order_version:
            cache.clustering_resume = None
            return
        self._restore_resume_state(resume)

    def _try_restore_candidates(self) -> bool:
        cache = self.identity_cache
        if cache is None:
            return False
        snapshot = cache.clustering_resume
        if snapshot is None:
            return False
        if snapshot.items_signature != cache.items_signature(self.items):
            self.resume_diagnostics["clustering_resume_signature_mismatch"] = True
            return False
        stored_version = int(getattr(snapshot, "candidate_order_version", 1) or 1)
        if stored_version != self._candidate_order_version:
            return False
        keys = list(getattr(snapshot, "candidate_keys", ()) or ())
        if not keys:
            return False
        nodes = {
            (item.venue.value, item.source_event_id): item for item in self.items
        }
        restored: list[tuple[VenueEvent, VenueEvent]] = []
        for left_key, right_key in keys:
            left = nodes.get((str(left_key[0]), str(left_key[1])))
            right = nodes.get((str(right_key[0]), str(right_key[1])))
            if left is None or right is None:
                return False
            restored.append((left, right))
        self._candidates = restored
        self.index_diagnostics = dict(getattr(snapshot, "index_diagnostics", {}) or {})
        self.resume_diagnostics["clustering_resume_candidates_reused"] = True
        self._apply_resume_cursor()
        return True

    def _load_candidates_sync(self) -> None:
        if self._try_restore_candidates():
            return
        self._candidates, self.index_diagnostics = build_indexed_candidates(
            self.items,
            kickoff_tolerance=self._kickoff_tolerance,
            cache=self.identity_cache,
            matcher=self.bulk_matcher,
            secondary_block=self.secondary_block,
        )
        self._apply_resume_cursor()

    async def load_candidates_cooperative(
        self, *, yield_every: int = INDEX_COOP_YIELD_EVERY
    ) -> None:
        if self._try_restore_candidates():
            return
        builder = _IndexedCandidateBuilder(
            items=self.items,
            kickoff_tolerance=self._kickoff_tolerance,
            cache=self.identity_cache,
            matcher=self.bulk_matcher,
            secondary_block=self.secondary_block,
        )
        try:
            self._candidates, self.index_diagnostics = await builder.run_cooperative(
                yield_every=yield_every
            )
        except asyncio.CancelledError:
            # Keep generated pairs for leftover finalize. Do not apply resume
            # onto an incomplete index, and do not consume the stored checkpoint.
            self._candidates, self.index_diagnostics = builder.finish()
            raise
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
            "unscored_identity_nodes": len(self.last_unscored_nodes),
            **self.resume_diagnostics,
            **self.graph_diagnostics,
            **self.incremental_diagnostics.as_dict(),
        }

    def _cache_generation_matches(self) -> bool:
        cache = self.identity_cache
        if cache is None:
            return False
        bound = self._bound_generation_id
        if bound is None:
            return True
        return getattr(cache, "generation_id", None) == bound

    def checkpoint(self, cursor: int) -> None:
        cache = self.identity_cache
        if cache is None or not self._cache_generation_matches():
            return
        cache.store_clustering_resume(
            items=self.items,
            cursor=cursor,
            parent=self.parent,
            match_confidence=self.match_confidence,
            pair_kinds=self.pair_kinds,
            scored_pairs=list(self.scored_pairs),
            candidate_keys=self._candidate_keys(),
            candidate_order_version=self._candidate_order_version,
            index_diagnostics=dict(self.index_diagnostics),
        )

    def record_generation_negatives(self, clusters: list[FixtureCluster]) -> None:
        cache = self.identity_cache
        if cache is None or not self._cache_generation_matches():
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
        pending = (
            self._unscored_identity_nodes() if unscored_nodes is None else unscored_nodes
        )
        self.last_unscored_nodes = set(pending)
        result = assign_identity_components(
            list(self.nodes.keys()),
            self.scored_pairs,
            unscored_nodes=pending,
            threshold=threshold,
        )
        return self._ingest_identity_result(result)

    def _ingest_identity_result(
        self, result: Any
    ) -> dict[tuple[VenueName, str], IdentityAssignmentProvenance]:
        self.graph_diagnostics = result.diagnostics.as_dict()
        self.parent = {key: key for key in self.nodes}
        self.match_confidence = {}
        self.pair_kinds = {}
        provenance_by_member: dict[tuple[VenueName, str], IdentityAssignmentProvenance] = {}
        for assigned in result.clusters:
            self._record_assigned_identity_cluster(assigned, provenance_by_member)
        return provenance_by_member

    def _record_assigned_identity_cluster(
        self,
        assigned: Any,
        provenance_by_member: dict[tuple[VenueName, str], IdentityAssignmentProvenance],
    ) -> None:
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

    async def _apply_identity_graph_cooperative(
        self,
        threshold: float,
        *,
        unscored_nodes: set[tuple[VenueName, str]] | None = None,
        yield_every: int = 64,
    ) -> dict[tuple[VenueName, str], IdentityAssignmentProvenance]:
        pending = (
            self._unscored_identity_nodes() if unscored_nodes is None else unscored_nodes
        )
        self.last_unscored_nodes = set(pending)
        result = await assign_identity_components_cooperative(
            list(self.nodes.keys()),
            self.scored_pairs,
            unscored_nodes=pending,
            threshold=threshold,
            yield_every=yield_every,
        )
        self.graph_diagnostics = result.diagnostics.as_dict()
        self.parent = {key: key for key in self.nodes}
        self.match_confidence = {}
        self.pair_kinds = {}
        provenance_by_member: dict[tuple[VenueName, str], IdentityAssignmentProvenance] = {}
        pause_every = max(1, int(yield_every))
        last_yield = time.perf_counter()
        for index, assigned in enumerate(result.clusters, start=1):
            self._record_assigned_identity_cluster(assigned, provenance_by_member)
            if _coop_pause_due(index, pause_every, last_yield):
                await asyncio.sleep(0)
                last_yield = time.perf_counter()
        return provenance_by_member

    def finalize(self) -> tuple[list[FixtureCluster], dict[str, int]]:
        threshold = float(getattr(self.bulk_matcher, "threshold", 0.92))
        provenance_by_member = self._apply_identity_graph(threshold)
        return self._assemble_clusters(threshold, provenance_by_member)

    async def _unscored_identity_nodes_cooperative(
        self, *, yield_every: int = INDEX_COOP_YIELD_EVERY
    ) -> set[tuple[VenueName, str]]:
        nodes: set[tuple[VenueName, str]] = set()
        start = self.candidate_pairs_considered
        pause_every = max(1, int(yield_every))
        last_yield = time.perf_counter()
        for ticks, index in enumerate(range(start, len(self._candidates)), start=1):
            if _coop_pause_due(ticks, pause_every, last_yield):
                await asyncio.sleep(0)
                last_yield = time.perf_counter()
            left, right = self._candidates[index]
            nodes.add(_key(left.venue, left.source_event_id))
            nodes.add(_key(right.venue, right.source_event_id))
            if len(nodes) >= len(self.nodes):
                break
        return nodes

    async def finalize_cooperative(
        self, *, yield_every: int = 64
    ) -> tuple[list[FixtureCluster], dict[str, int]]:
        threshold = float(getattr(self.bulk_matcher, "threshold", 0.92))
        unscored_nodes = await self._unscored_identity_nodes_cooperative(
            yield_every=yield_every
        )
        provenance_by_member = await self._apply_identity_graph_cooperative(
            threshold, unscored_nodes=unscored_nodes, yield_every=yield_every
        )
        await asyncio.sleep(0)
        pause_every = max(1, int(yield_every))
        last_yield = time.perf_counter()
        grouped: dict[tuple[VenueName, str], FixtureCluster] = {}
        for index, (key, item) in enumerate(self.nodes.items(), start=1):
            if _coop_pause_due(index, pause_every, last_yield):
                await asyncio.sleep(0)
                last_yield = time.perf_counter()
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


def cluster_events_from_pass(
    *,
    matchbook: list[VenueEvent],
    polymarket: list[VenueEvent],
    kalshi: list[VenueEvent],
    matcher: EventMatcher,
    max_event_pairs: int,
    identity_cache: Any | None = None,
    extra: list[VenueEvent] | None = None,
    incremental_cache: CrossGenerationIdentityCache | None = None,
    secondary_block: bool = False,
) -> ClusterPass:
    """Deferred ClusterPass used by competition-shard scheduling."""

    return ClusterPass(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=matcher,
        max_event_pairs=max_event_pairs,
        identity_cache=identity_cache,
        extra=extra,
        incremental_cache=incremental_cache,
        defer_candidate_build=True,
        secondary_block=secondary_block,
    )


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
