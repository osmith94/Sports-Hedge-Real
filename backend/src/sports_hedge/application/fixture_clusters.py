from __future__ import annotations

from dataclasses import dataclass, field

from sports_hedge.domain.models import VenueName
from sports_hedge.matching.events import EventMatcher
from sports_hedge.normalization.identity import canonical_source_event_id


@dataclass
class VenueEvent:
    venue: VenueName
    raw: dict
    canonical: object
    source_event_id: str


@dataclass
class FixtureCluster:
    matchbook: VenueEvent | None = None
    polymarket: VenueEvent | None = None
    kalshi: VenueEvent | None = None
    pair_kinds: set[str] = field(default_factory=set)

    @property
    def venues_present(self) -> list[VenueName]:
        present: list[VenueName] = []
        if self.matchbook is not None:
            present.append(VenueName.MATCHBOOK)
        if self.polymarket is not None:
            present.append(VenueName.POLYMARKET)
        if self.kalshi is not None:
            present.append(VenueName.KALSHI)
        return present

    @property
    def venue_count(self) -> int:
        return len(self.venues_present)

    @property
    def anchor(self) -> VenueEvent:
        for item in (self.matchbook, self.polymarket, self.kalshi):
            if item is not None:
                return item
        raise ValueError("empty fixture cluster")

    def event_for(self, venue: VenueName) -> VenueEvent | None:
        if venue is VenueName.MATCHBOOK:
            return self.matchbook
        if venue is VenueName.POLYMARKET:
            return self.polymarket
        if venue is VenueName.KALSHI:
            return self.kalshi
        return None


def _key(venue: VenueName, source_event_id: str) -> tuple[VenueName, str]:
    return (venue, source_event_id)


def cluster_venue_events(
    *,
    matchbook: list[VenueEvent],
    polymarket: list[VenueEvent],
    kalshi: list[VenueEvent],
    matcher: EventMatcher,
    max_event_pairs: int,
) -> tuple[list[FixtureCluster], dict[str, int]]:
    """Union-find clusters across independently discovered venue events.

    Pairwise matching is still greedy one-to-one inside each venue pair. A
    Polymarket↔Kalshi match does not require a Matchbook event.
    """

    mb_pm = _greedy_pairs(matchbook, polymarket, matcher)[:max_event_pairs]
    mb_k = _greedy_pairs(matchbook, kalshi, matcher)[:max_event_pairs]
    pm_k = _greedy_pairs(polymarket, kalshi, matcher)[:max_event_pairs]

    parent: dict[tuple[VenueName, str], tuple[VenueName, str]] = {}
    nodes: dict[tuple[VenueName, str], VenueEvent] = {}

    def add_node(item: VenueEvent) -> None:
        key = _key(item.venue, item.source_event_id)
        nodes[key] = item
        parent.setdefault(key, key)

    for item in (*matchbook, *polymarket, *kalshi):
        add_node(item)

    def find(key: tuple[VenueName, str]) -> tuple[VenueName, str]:
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(left: VenueEvent, right: VenueEvent) -> None:
        a = find(_key(left.venue, left.source_event_id))
        b = find(_key(right.venue, right.source_event_id))
        if a != b:
            parent[b] = a

    pair_kinds: dict[tuple[VenueName, str], set[str]] = {}

    def mark_pair(left: VenueEvent, right: VenueEvent, kind: str) -> None:
        union(left, right)
        for item in (left, right):
            pair_kinds.setdefault(_key(item.venue, item.source_event_id), set()).add(kind)

    for left, right in mb_pm:
        mark_pair(left, right, "matchbook_polymarket")
    for left, right in mb_k:
        mark_pair(left, right, "matchbook_kalshi")
    for left, right in pm_k:
        mark_pair(left, right, "polymarket_kalshi")

    grouped: dict[tuple[VenueName, str], FixtureCluster] = {}
    for key, item in nodes.items():
        root = find(key)
        cluster = grouped.setdefault(root, FixtureCluster())
        if item.venue is VenueName.MATCHBOOK:
            cluster.matchbook = item
        elif item.venue is VenueName.POLYMARKET:
            cluster.polymarket = item
        elif item.venue is VenueName.KALSHI:
            cluster.kalshi = item
        cluster.pair_kinds.update(pair_kinds.get(key, set()))

    clusters = list(grouped.values())
    counts = {
        "matchbook_polymarket": len(mb_pm),
        "matchbook_kalshi": len(mb_k),
        "polymarket_kalshi": len(pm_k),
    }
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


def _greedy_pairs(
    left: list[VenueEvent],
    right: list[VenueEvent],
    matcher: EventMatcher,
) -> list[tuple[VenueEvent, VenueEvent]]:
    candidates: list[tuple[float, int, int]] = []
    for left_index, left_item in enumerate(left):
        for right_index, right_item in enumerate(right):
            match = matcher.match(left_item.canonical, right_item.canonical)
            if match.matched:
                candidates.append((match.confidence, left_index, right_index))
    candidates.sort(key=lambda item: item[0], reverse=True)
    used_left: set[int] = set()
    used_right: set[int] = set()
    result: list[tuple[VenueEvent, VenueEvent]] = []
    for _, left_index, right_index in candidates:
        if left_index in used_left or right_index in used_right:
            continue
        used_left.add(left_index)
        used_right.add(right_index)
        result.append((left[left_index], right[right_index]))
    return result
