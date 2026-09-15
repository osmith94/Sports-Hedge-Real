from __future__ import annotations

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

    if max_event_pairs <= 0:
        raise ValueError("max_event_pairs must be positive")
    items = [*matchbook, *polymarket, *kalshi]
    parent: dict[tuple[VenueName, str], tuple[VenueName, str]] = {}
    nodes: dict[tuple[VenueName, str], VenueEvent] = {}

    def add_node(item: VenueEvent) -> None:
        key = _key(item.venue, item.source_event_id)
        nodes[key] = item
        parent.setdefault(key, key)

    for item in items:
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

    pair_kind = {
        (VenueName.MATCHBOOK, VenueName.POLYMARKET): "matchbook_polymarket",
        (VenueName.POLYMARKET, VenueName.MATCHBOOK): "matchbook_polymarket",
        (VenueName.MATCHBOOK, VenueName.KALSHI): "matchbook_kalshi",
        (VenueName.KALSHI, VenueName.MATCHBOOK): "matchbook_kalshi",
        (VenueName.POLYMARKET, VenueName.KALSHI): "polymarket_kalshi",
        (VenueName.KALSHI, VenueName.POLYMARKET): "polymarket_kalshi",
    }

    could_match = getattr(matcher, "could_match", None)
    for left_index, left in enumerate(items):
        for right in items[left_index + 1 :]:
            if callable(could_match) and not could_match(left.canonical, right.canonical):
                continue
            match = matcher.match(left.canonical, right.canonical)
            if not match.matched:
                continue
            if left.venue is right.venue:
                union(left, right)
                continue
            kind = pair_kind.get((left.venue, right.venue))
            if kind is None:
                union(left, right)
            else:
                mark_pair(left, right, kind)

    grouped: dict[tuple[VenueName, str], FixtureCluster] = {}
    for key, item in nodes.items():
        root = find(key)
        cluster = grouped.setdefault(root, FixtureCluster())
        if item.venue is VenueName.MATCHBOOK:
            cluster.matchbook_events.append(item)
        elif item.venue is VenueName.POLYMARKET:
            cluster.polymarket_events.append(item)
        elif item.venue is VenueName.KALSHI:
            cluster.kalshi_events.append(item)
        cluster.pair_kinds.update(pair_kinds.get(key, set()))

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
