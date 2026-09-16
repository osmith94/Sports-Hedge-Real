from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field

from sports_hedge.domain.models import VenueName
from sports_hedge.matching.events import EventMatcher
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
    pair_kinds: set[str] = field(default_factory=set)

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

    def events_for(self, venue: VenueName) -> list[VenueEvent]:
        if venue is VenueName.MATCHBOOK:
            return list(self.matchbook_events)
        if venue is VenueName.POLYMARKET:
            return list(self.polymarket_events)
        if venue is VenueName.KALSHI:
            return list(self.kalshi_events)
        return []


def _key(venue: VenueName, source_event_id: str) -> tuple[VenueName, str]:
    return (venue, source_event_id)


def _sort_events(items: list[VenueEvent]) -> list[VenueEvent]:
    return sorted(items, key=lambda item: item.source_event_id)


_PAIR_KIND = {
    (VenueName.MATCHBOOK, VenueName.POLYMARKET): "matchbook_polymarket",
    (VenueName.POLYMARKET, VenueName.MATCHBOOK): "matchbook_polymarket",
    (VenueName.MATCHBOOK, VenueName.KALSHI): "matchbook_kalshi",
    (VenueName.KALSHI, VenueName.MATCHBOOK): "matchbook_kalshi",
    (VenueName.POLYMARKET, VenueName.KALSHI): "polymarket_kalshi",
    (VenueName.KALSHI, VenueName.POLYMARKET): "polymarket_kalshi",
}


class ClusterPass:
    """Deterministic union-find clustering that can yield between comparisons."""

    def __init__(
        self,
        *,
        matchbook: list[VenueEvent],
        polymarket: list[VenueEvent],
        kalshi: list[VenueEvent],
        matcher: EventMatcher,
        max_event_pairs: int,
    ) -> None:
        if max_event_pairs <= 0:
            raise ValueError("max_event_pairs must be positive")
        self.items = [*matchbook, *polymarket, *kalshi]
        self.parent: dict[tuple[VenueName, str], tuple[VenueName, str]] = {}
        self.nodes: dict[tuple[VenueName, str], VenueEvent] = {}
        self.pair_kinds: dict[tuple[VenueName, str], set[str]] = {}
        for item in self.items:
            key = _key(item.venue, item.source_event_id)
            self.nodes[key] = item
            self.parent.setdefault(key, key)
        snapshot_for_bulk = getattr(matcher, "bulk_snapshot", None)
        self.bulk_matcher = snapshot_for_bulk() if callable(snapshot_for_bulk) else matcher
        self.could_match = getattr(self.bulk_matcher, "could_match", None)

    def pairs(self) -> Iterator[tuple[VenueEvent, VenueEvent]]:
        for left_index, left in enumerate(self.items):
            for right in self.items[left_index + 1 :]:
                yield left, right

    def _find(self, key: tuple[VenueName, str]) -> tuple[VenueName, str]:
        parent = self.parent
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def _union(self, left: VenueEvent, right: VenueEvent) -> None:
        a = self._find(_key(left.venue, left.source_event_id))
        b = self._find(_key(right.venue, right.source_event_id))
        if a != b:
            self.parent[b] = a

    def consider(self, left: VenueEvent, right: VenueEvent) -> None:
        could_match = self.could_match
        if callable(could_match) and not could_match(left.canonical, right.canonical):
            return
        match = self.bulk_matcher.match(left.canonical, right.canonical)
        if not match.matched:
            return
        if left.venue is right.venue:
            self._union(left, right)
            return
        kind = _PAIR_KIND.get((left.venue, right.venue))
        if kind is None:
            self._union(left, right)
            return
        self._union(left, right)
        for item in (left, right):
            self.pair_kinds.setdefault(_key(item.venue, item.source_event_id), set()).add(kind)

    def finalize(self) -> tuple[list[FixtureCluster], dict[str, int]]:
        grouped: dict[tuple[VenueName, str], FixtureCluster] = {}
        for key, item in self.nodes.items():
            root = self._find(key)
            cluster = grouped.setdefault(root, FixtureCluster())
            if item.venue is VenueName.MATCHBOOK:
                cluster.matchbook_events.append(item)
            elif item.venue is VenueName.POLYMARKET:
                cluster.polymarket_events.append(item)
            elif item.venue is VenueName.KALSHI:
                cluster.kalshi_events.append(item)
            cluster.pair_kinds.update(self.pair_kinds.get(key, set()))

        clusters: list[FixtureCluster] = []
        for cluster in grouped.values():
            cluster.matchbook_events = _sort_events(cluster.matchbook_events)
            cluster.polymarket_events = _sort_events(cluster.polymarket_events)
            cluster.kalshi_events = _sort_events(cluster.kalshi_events)
            clusters.append(cluster)

        clusters.sort(key=lambda item: -item.venue_count)
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


def cluster_venue_events(
    *,
    matchbook: list[VenueEvent],
    polymarket: list[VenueEvent],
    kalshi: list[VenueEvent],
    matcher: EventMatcher,
    max_event_pairs: int,
) -> tuple[list[FixtureCluster], dict[str, int]]:
    """Cluster independently discovered venue events by canonical fixture identity.

    Pairwise EventMatcher still decides whether two source events are the same
    fixture. Unlike greedy one-to-one pairing, every matching source event for
    the same fixture is unioned — including multiple Polymarket or Kalshi
    events — so a PM↔Kalshi match does not require Matchbook and does not
    consume a sibling BTTS/totals event as if it were a different fixture.

    ``max_event_pairs`` never drops a multi-venue cluster. Unmatched single-venue
    leftovers remain visible so unmatched coverage is not silently dropped.
    """

    cluster_pass = ClusterPass(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        matcher=matcher,
        max_event_pairs=max_event_pairs,
    )
    for left, right in cluster_pass.pairs():
        cluster_pass.consider(left, right)
    return cluster_pass.finalize()


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


def cluster_member_events(cluster: FixtureCluster) -> list[VenueEvent]:
    return [
        *cluster.matchbook_events,
        *cluster.polymarket_events,
        *cluster.kalshi_events,
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
