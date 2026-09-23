"""Constrained fixture-identity graph for ambiguous multi-venue components.

Indexed candidate generation remains the fast first stage. EventMatcher hard
vetoes and thresholds stay authoritative: this module never invents an edge
the matcher rejected, and never overrides a hard veto.

Obvious connected components stay on the cheap union path. Ambiguous
components (competing cross-venue evidence, 3+ venue consistency) are solved
with constrained maximum-weight assignment. Contradictory or low-margin
components fail closed and return provenance instead of forcing a match.

Same-venue sibling source events (market-family splits of one fixture) are
contracted before cross-venue assignment so they are not consumed as rival
fixtures. The graph is generic over 2+ venues; it does not special-case
Matchbook, Kalshi, or Polymarket.
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict, deque
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from sports_hedge.domain.models import VenueName

NodeKey = tuple[VenueName, str]

# Assignment uniqueness, not an EventMatcher threshold. Matcher 0.92 / PAPER
# 0.80 are unchanged. A thinner global margin fails closed.
DEFAULT_ASSIGNMENT_MARGIN = 0.03
MAX_AMBIGUOUS_SUPERNODES = 8
_ASSIGN_COOP_MAX_SLICE_SECONDS = 0.05

HARD_VETO_REASONS = frozenset(
    {
        "sport_mismatch",
        "kickoff_outside_tolerance",
        "participant_squad_category_mismatch",
        "competition_mismatch",
        "curated_team_mismatch",
        "nfl_team_identity_ambiguous",
        "nba_team_identity_ambiguous",
        "prefilter_rejected",
    }
)

CONSTRAINT_ONE_SUPERNODE_PER_VENUE = "at_most_one_supernode_per_venue_per_cluster"
CONSTRAINT_SIBLINGS_PERMITTED = "same_venue_siblings_permitted"
CONSTRAINT_HARD_VETO = "event_matcher_hard_veto"
CONSTRAINT_CLIQUE = "multi_venue_cluster_requires_pairwise_clique"
CONSTRAINT_MARGIN = "assignment_margin"
CONSTRAINT_ENUMERATION_CAP = "ambiguous_component_enumeration_cap"
CONSTRAINT_INCOMPLETE = "incomplete_candidate_evidence"


def node_sort_key(key: NodeKey) -> tuple[str, str]:
    return (key[0].value, key[1])


def ordered_node_pair(left: NodeKey, right: NodeKey) -> tuple[NodeKey, NodeKey]:
    if node_sort_key(left) <= node_sort_key(right):
        return left, right
    return right, left


def pair_kind(left: VenueName, right: VenueName) -> str:
    """Stable undirected venue-pair label. Known pairs keep historical names."""

    historical = {
        frozenset({VenueName.MATCHBOOK, VenueName.POLYMARKET}): "matchbook_polymarket",
        frozenset({VenueName.MATCHBOOK, VenueName.KALSHI}): "matchbook_kalshi",
        frozenset({VenueName.POLYMARKET, VenueName.KALSHI}): "polymarket_kalshi",
    }
    known = historical.get(frozenset({left, right}))
    if known is not None:
        return known
    first, second = sorted((left.value, right.value))
    return f"{first}_{second}"


def is_hard_identity_veto(reasons: Sequence[str], *, matched: bool, confidence: float) -> bool:
    if matched:
        return False
    if any(
        reason in HARD_VETO_REASONS
        or reason.startswith("nfl_team_identity")
        or reason.startswith("nba_team_identity")
        for reason in reasons
    ):
        return True
    return confidence <= 0.0 and "prefilter_rejected" in reasons


@dataclass(frozen=True)
class ScoredIdentityPair:
    left: NodeKey
    right: NodeKey
    confidence: float
    reasons: tuple[str, ...]
    matched: bool
    veto: bool

    @staticmethod
    def from_endpoints(
        left: NodeKey,
        right: NodeKey,
        *,
        confidence: float,
        reasons: Sequence[str],
        matched: bool,
        veto: bool,
    ) -> ScoredIdentityPair:
        ordered_left, ordered_right = ordered_node_pair(left, right)
        return ScoredIdentityPair(
            left=ordered_left,
            right=ordered_right,
            confidence=round(float(confidence), 6),
            reasons=tuple(reasons),
            matched=bool(matched),
            veto=bool(veto),
        )

    def undirected_key(self) -> frozenset[NodeKey]:
        return frozenset({self.left, self.right})

    def as_record(self) -> dict[str, object]:
        return {
            "left_venue": self.left[0].value,
            "left_source_event_id": self.left[1],
            "right_venue": self.right[0].value,
            "right_source_event_id": self.right[1],
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "matched": self.matched,
            "veto": self.veto,
        }


@dataclass(frozen=True)
class IdentityAssignmentProvenance:
    method: str
    component_id: str
    chosen_edges: tuple[ScoredIdentityPair, ...]
    rejected_competing_edges: tuple[ScoredIdentityPair, ...]
    confidence_margin: float | None
    constraints: tuple[str, ...]
    fail_closed_reason: str | None = None
    greedy_weight: float | None = None
    global_weight: float | None = None

    def as_record(self) -> dict[str, object]:
        return {
            "method": self.method,
            "component_id": self.component_id,
            "chosen_edges": [edge.as_record() for edge in self.chosen_edges],
            "rejected_competing_edges": [edge.as_record() for edge in self.rejected_competing_edges],
            "confidence_margin": self.confidence_margin,
            "constraints": list(self.constraints),
            "fail_closed_reason": self.fail_closed_reason,
            "greedy_weight": self.greedy_weight,
            "global_weight": self.global_weight,
        }


@dataclass(frozen=True)
class AssignedIdentityCluster:
    member_keys: frozenset[NodeKey]
    provenance: IdentityAssignmentProvenance


@dataclass
class IdentityGraphDiagnostics:
    components: int = 0
    obvious_components: int = 0
    ambiguous_components: int = 0
    contradictory_components: int = 0
    fail_closed_components: int = 0
    global_assignments: int = 0
    sibling_groups: int = 0

    def as_dict(self) -> dict[str, int]:
        return {
            "identity_graph_components": self.components,
            "identity_graph_obvious_components": self.obvious_components,
            "identity_graph_ambiguous_components": self.ambiguous_components,
            "identity_graph_contradictory_components": self.contradictory_components,
            "identity_graph_fail_closed_components": self.fail_closed_components,
            "identity_graph_global_assignments": self.global_assignments,
            "identity_graph_sibling_groups": self.sibling_groups,
        }


@dataclass
class IdentityGraphResult:
    clusters: list[AssignedIdentityCluster]
    diagnostics: IdentityGraphDiagnostics = field(default_factory=IdentityGraphDiagnostics)


@dataclass(frozen=True)
class _Supernode:
    root: NodeKey
    venue: VenueName
    members: frozenset[NodeKey]


def greedy_local_pairwise_assignment(
    matching_edges: Sequence[ScoredIdentityPair],
) -> tuple[tuple[ScoredIdentityPair, ...], float]:
    """Local 1:1 pairing: heaviest remaining edge whose endpoints are free.

    Same-venue sibling contraction must already have happened; this is the
    naive competing-pair consumer that global assignment replaces.
    """

    assigned: set[NodeKey] = set()
    chosen: list[ScoredIdentityPair] = []
    ordered = sorted(
        (edge for edge in matching_edges if edge.matched and not edge.veto),
        key=lambda edge: (
            -edge.confidence,
            node_sort_key(edge.left),
            node_sort_key(edge.right),
        ),
    )
    for edge in ordered:
        if edge.left in assigned or edge.right in assigned:
            continue
        assigned.add(edge.left)
        assigned.add(edge.right)
        chosen.append(edge)
    weight = round(sum(edge.confidence for edge in chosen), 6)
    return tuple(chosen), weight


def _component_id(keys: Iterable[NodeKey]) -> str:
    return ",".join(f"{venue.value}:{source_id}" for venue, source_id in sorted(keys, key=node_sort_key))


class _UnionFind:
    def __init__(self, nodes: Iterable[NodeKey]) -> None:
        self.parent = {node: node for node in nodes}

    def find(self, key: NodeKey) -> NodeKey:
        parent = self.parent
        while parent[key] != key:
            parent[key] = parent[parent[key]]
            key = parent[key]
        return key

    def union(self, left: NodeKey, right: NodeKey) -> None:
        a = self.find(left)
        b = self.find(right)
        if a == b:
            return
        if node_sort_key(a) <= node_sort_key(b):
            self.parent[b] = a
        else:
            self.parent[a] = b


def _sibling_supernodes(
    nodes: Sequence[NodeKey],
    scored: Sequence[ScoredIdentityPair],
) -> list[_Supernode]:
    forest = _UnionFind(nodes)
    for pair in scored:
        if pair.left[0] is not pair.right[0]:
            continue
        if pair.matched and not pair.veto:
            forest.union(pair.left, pair.right)
    grouped: dict[NodeKey, list[NodeKey]] = defaultdict(list)
    for node in nodes:
        grouped[forest.find(node)].append(node)
    supernodes: list[_Supernode] = []
    for root, members in grouped.items():
        member_keys = frozenset(members)
        supernodes.append(
            _Supernode(root=root, venue=root[0], members=member_keys)
        )
    supernodes.sort(key=lambda item: node_sort_key(item.root))
    return supernodes


def _index_pairs(
    scored: Sequence[ScoredIdentityPair],
) -> dict[frozenset[NodeKey], ScoredIdentityPair]:
    indexed: dict[frozenset[NodeKey], ScoredIdentityPair] = {}
    for pair in scored:
        indexed[pair.undirected_key()] = pair
    return indexed


def _supernode_relation(
    left: _Supernode,
    right: _Supernode,
    *,
    pair_index: Mapping[frozenset[NodeKey], ScoredIdentityPair],
) -> tuple[float | None, bool, bool]:
    """Return (weight, has_match, has_veto) between two supernodes."""

    if left.root == right.root:
        return None, False, False
    matched_weights: list[float] = []
    saw_miss = False
    saw_veto = False
    for left_member in left.members:
        for right_member in right.members:
            pair = pair_index.get(frozenset({left_member, right_member}))
            if pair is None:
                continue
            if pair.veto:
                saw_veto = True
                continue
            if pair.matched:
                matched_weights.append(pair.confidence)
            else:
                saw_miss = True
    if saw_veto:
        return None, False, True
    if saw_miss:
        return None, False, False
    if not matched_weights:
        return None, False, False
    return min(matched_weights), True, False


def _connected_components(
    supernodes: Sequence[_Supernode],
    adjacency: Mapping[NodeKey, Sequence[NodeKey]],
) -> list[list[_Supernode]]:
    by_root = {item.root: item for item in supernodes}
    remaining = {item.root for item in supernodes}
    components: list[list[_Supernode]] = []
    for start in sorted(remaining, key=node_sort_key):
        if start not in remaining:
            continue
        queue = deque([start])
        remaining.remove(start)
        members = [by_root[start]]
        while queue:
            current = queue.popleft()
            for neighbour in adjacency.get(current, ()):
                if neighbour not in remaining:
                    continue
                remaining.remove(neighbour)
                queue.append(neighbour)
                members.append(by_root[neighbour])
        members.sort(key=lambda item: node_sort_key(item.root))
        components.append(members)
    return components


def _matching_pairs_in_component(
    component: Sequence[_Supernode],
    *,
    pair_index: Mapping[frozenset[NodeKey], ScoredIdentityPair],
) -> list[ScoredIdentityPair]:
    member_nodes = {node for item in component for node in item.members}
    pairs: list[ScoredIdentityPair] = []
    seen: set[frozenset[NodeKey]] = set()
    for left in component:
        for right in component:
            if node_sort_key(left.root) >= node_sort_key(right.root):
                continue
            if left.venue is right.venue:
                continue
            relation_weight, has_match, _veto = _supernode_relation(
                left, right, pair_index=pair_index
            )
            if not has_match or relation_weight is None:
                continue
            for left_member in left.members:
                for right_member in right.members:
                    pair = pair_index.get(frozenset({left_member, right_member}))
                    if pair is None or not pair.matched or pair.veto:
                        continue
                    key = pair.undirected_key()
                    if key in seen:
                        continue
                    if pair.left not in member_nodes or pair.right not in member_nodes:
                        continue
                    seen.add(key)
                    pairs.append(pair)
    pairs.sort(key=lambda edge: (node_sort_key(edge.left), node_sort_key(edge.right)))
    return pairs


def _unscored_endpoint_nodes(unscored: Iterable[frozenset[NodeKey]]) -> set[NodeKey]:
    """Nodes incident to at least one indexed candidate that was not scored."""

    nodes: set[NodeKey] = set()
    for pair in unscored:
        if len(pair) != 2:
            continue
        left, right = tuple(pair)
        nodes.add(left)
        nodes.add(right)
    return nodes


def _component_incomplete(
    component: Sequence[_Supernode],
    *,
    unscored_nodes: set[NodeKey],
) -> bool:
    if not unscored_nodes:
        return False
    return any(
        node in unscored_nodes
        for item in component
        for node in item.members
    )


def _matching_supernode_adjacency(
    supernodes: Sequence[_Supernode],
    *,
    pair_index: Mapping[frozenset[NodeKey], ScoredIdentityPair],
) -> dict[NodeKey, list[NodeKey]]:
    """Connected matching supernodes. Equivalent to all-pairs ``_supernode_relation``.

    All-pairs enumeration is O(N²) in source events and starves the FastAPI
    loop on a ~1,600-fixture UNIVERSE. Matching edges are sparse, so walk the
    scored pair index instead. Veto or miss between supernodes still suppresses
    the edge, matching ``_supernode_relation``.
    """

    node_to_root: dict[NodeKey, NodeKey] = {}
    for item in supernodes:
        for member in item.members:
            node_to_root[member] = item.root

    matched_weights: dict[tuple[NodeKey, NodeKey], list[float]] = defaultdict(list)
    veto_pairs: set[tuple[NodeKey, NodeKey]] = set()
    miss_pairs: set[tuple[NodeKey, NodeKey]] = set()

    for pair in pair_index.values():
        left_root = node_to_root.get(pair.left)
        right_root = node_to_root.get(pair.right)
        if left_root is None or right_root is None or left_root == right_root:
            continue
        key = (
            (left_root, right_root)
            if node_sort_key(left_root) <= node_sort_key(right_root)
            else (right_root, left_root)
        )
        if pair.veto:
            veto_pairs.add(key)
            continue
        if pair.matched:
            matched_weights[key].append(pair.confidence)
        else:
            miss_pairs.add(key)

    adjacency: dict[NodeKey, list[NodeKey]] = {item.root: [] for item in supernodes}
    for key, weights in matched_weights.items():
        if key in veto_pairs or key in miss_pairs or not weights:
            continue
        left_root, right_root = key
        adjacency[left_root].append(right_root)
        adjacency[right_root].append(left_root)
    for neighbours in adjacency.values():
        neighbours.sort(key=node_sort_key)
    return adjacency


def _venues_compete(component: Sequence[_Supernode]) -> bool:
    counts: dict[VenueName, int] = defaultdict(int)
    for item in component:
        counts[item.venue] += 1
    return any(count > 1 for count in counts.values())


def _is_obvious_clique(
    component: Sequence[_Supernode],
    *,
    pair_index: Mapping[frozenset[NodeKey], ScoredIdentityPair],
) -> bool:
    if _venues_compete(component):
        return False
    if len(component) <= 1:
        return True
    for index, left in enumerate(component):
        for right in component[index + 1 :]:
            _weight, has_match, has_veto = _supernode_relation(
                left, right, pair_index=pair_index
            )
            if has_veto or not has_match:
                return False
    return True


def _has_cross_venue_veto_path(
    component: Sequence[_Supernode],
    *,
    pair_index: Mapping[frozenset[NodeKey], ScoredIdentityPair],
) -> bool:
    for index, left in enumerate(component):
        for right in component[index + 1 :]:
            _weight, _has_match, has_veto = _supernode_relation(
                left, right, pair_index=pair_index
            )
            if has_veto and left.venue is not right.venue:
                return True
    return False


def _edge_weight_map(
    component: Sequence[_Supernode],
    *,
    pair_index: Mapping[frozenset[NodeKey], ScoredIdentityPair],
) -> dict[frozenset[int], float]:
    weights: dict[frozenset[int], float] = {}
    for left_index, left in enumerate(component):
        for right_index, right in enumerate(component):
            if left_index >= right_index:
                continue
            weight, has_match, has_veto = _supernode_relation(
                left, right, pair_index=pair_index
            )
            if has_veto or not has_match or weight is None:
                continue
            weights[frozenset({left_index, right_index})] = weight
    return weights


def _veto_index_pairs(
    component: Sequence[_Supernode],
    *,
    pair_index: Mapping[frozenset[NodeKey], ScoredIdentityPair],
) -> set[frozenset[int]]:
    vetoed: set[frozenset[int]] = set()
    for left_index, left in enumerate(component):
        for right_index, right in enumerate(component):
            if left_index >= right_index:
                continue
            _weight, _has_match, has_veto = _supernode_relation(
                left, right, pair_index=pair_index
            )
            if has_veto:
                vetoed.add(frozenset({left_index, right_index}))
    return vetoed


def _partition_is_legal(
    clusters: Sequence[tuple[int, ...]],
    *,
    component: Sequence[_Supernode],
    weights: Mapping[frozenset[int], float],
    vetoed: set[frozenset[int]],
) -> tuple[bool, float]:
    total = 0.0
    for cluster in clusters:
        if len(cluster) <= 1:
            continue
        venues = [component[index].venue for index in cluster]
        if len(venues) != len(set(venues)):
            return False, 0.0
        for left_pos, left in enumerate(cluster):
            for right in cluster[left_pos + 1 :]:
                pair = frozenset({left, right})
                if pair in vetoed:
                    return False, 0.0
                weight = weights.get(pair)
                if weight is None:
                    return False, 0.0
                total += weight
    return True, round(total, 6)


def _best_two_partitions(
    component: Sequence[_Supernode],
    *,
    pair_index: Mapping[frozenset[NodeKey], ScoredIdentityPair],
) -> tuple[tuple[tuple[tuple[int, ...], ...], float] | None, tuple[tuple[tuple[int, ...], ...], float] | None]:
    n = len(component)
    weights = _edge_weight_map(component, pair_index=pair_index)
    vetoed = _veto_index_pairs(component, pair_index=pair_index)
    ranked: list[tuple[float, tuple[tuple[int, ...], ...]]] = []

    def consider(clusters: list[list[int]]) -> None:
        frozen = tuple(tuple(cluster) for cluster in clusters if cluster)
        legal, weight = _partition_is_legal(
            frozen, component=component, weights=weights, vetoed=vetoed
        )
        if not legal:
            return
        ranked.append((weight, frozen))

    def rec(index: int, clusters: list[list[int]]) -> None:
        if index == n:
            consider(clusters)
            return
        venue = component[index].venue
        for cluster in clusters:
            if any(component[member].venue is venue for member in cluster):
                continue
            cluster.append(index)
            rec(index + 1, clusters)
            cluster.pop()
        clusters.append([index])
        rec(index + 1, clusters)
        clusters.pop()

    rec(0, [])
    unique: dict[tuple[tuple[int, ...], ...], float] = {}
    for weight, frozen in ranked:
        canonical = tuple(sorted(frozen, key=lambda cluster: cluster[0] if cluster else -1))
        previous = unique.get(canonical)
        if previous is None or weight > previous:
            unique[canonical] = weight
    ordered = sorted(unique.items(), key=lambda item: (-item[1], item[0]))
    best_pair = None if not ordered else (ordered[0][0], float(ordered[0][1]))
    second_pair = None if len(ordered) < 2 else (ordered[1][0], float(ordered[1][1]))
    return best_pair, second_pair


def _pairs_for_clusters(
    component: Sequence[_Supernode],
    clusters: Sequence[tuple[int, ...]],
    *,
    pair_index: Mapping[frozenset[NodeKey], ScoredIdentityPair],
) -> tuple[tuple[ScoredIdentityPair, ...], tuple[ScoredIdentityPair, ...]]:
    chosen_keys: set[frozenset[NodeKey]] = set()
    chosen: list[ScoredIdentityPair] = []
    member_of: dict[int, int] = {}
    for cluster_index, cluster in enumerate(clusters):
        for node_index in cluster:
            member_of[node_index] = cluster_index
        if len(cluster) < 2:
            continue
        for left_pos, left_index in enumerate(cluster):
            for right_index in cluster[left_pos + 1 :]:
                left = component[left_index]
                right = component[right_index]
                for left_member in left.members:
                    for right_member in right.members:
                        pair = pair_index.get(frozenset({left_member, right_member}))
                        if pair is None or not pair.matched or pair.veto:
                            continue
                        key = pair.undirected_key()
                        if key in chosen_keys:
                            continue
                        chosen_keys.add(key)
                        chosen.append(pair)
    competing: list[ScoredIdentityPair] = []
    seen_competing: set[frozenset[NodeKey]] = set()
    for left_index, left in enumerate(component):
        for right_index, right in enumerate(component):
            if left_index >= right_index or left.venue is right.venue:
                continue
            if member_of.get(left_index) == member_of.get(right_index):
                continue
            for left_member in left.members:
                for right_member in right.members:
                    pair = pair_index.get(frozenset({left_member, right_member}))
                    if pair is None or not pair.matched or pair.veto:
                        continue
                    key = pair.undirected_key()
                    if key in chosen_keys or key in seen_competing:
                        continue
                    seen_competing.add(key)
                    competing.append(pair)
    chosen.sort(key=lambda edge: (node_sort_key(edge.left), node_sort_key(edge.right)))
    competing.sort(key=lambda edge: (node_sort_key(edge.left), node_sort_key(edge.right)))
    return tuple(chosen), tuple(competing)


def _singleton_clusters(
    component: Sequence[_Supernode],
    provenance: IdentityAssignmentProvenance,
) -> list[AssignedIdentityCluster]:
    return [
        AssignedIdentityCluster(member_keys=item.members, provenance=provenance)
        for item in component
    ]


def _clusters_from_partition(
    component: Sequence[_Supernode],
    partition: Sequence[tuple[int, ...]],
    provenance: IdentityAssignmentProvenance,
) -> list[AssignedIdentityCluster]:
    assigned: list[AssignedIdentityCluster] = []
    for cluster in partition:
        members: set[NodeKey] = set()
        for index in cluster:
            members.update(component[index].members)
        assigned.append(
            AssignedIdentityCluster(member_keys=frozenset(members), provenance=provenance)
        )
    return assigned


def _fail_closed_provenance(
    component: Sequence[_Supernode],
    *,
    method: str,
    reason: str,
    constraints: Sequence[str],
    pair_index: Mapping[frozenset[NodeKey], ScoredIdentityPair],
    greedy_weight: float | None = None,
    global_weight: float | None = None,
    margin: float | None = None,
) -> IdentityAssignmentProvenance:
    competing = tuple(_matching_pairs_in_component(component, pair_index=pair_index))
    return IdentityAssignmentProvenance(
        method=method,
        component_id=_component_id(node for item in component for node in item.members),
        chosen_edges=(),
        rejected_competing_edges=competing,
        confidence_margin=margin,
        constraints=tuple(constraints),
        fail_closed_reason=reason,
        greedy_weight=greedy_weight,
        global_weight=global_weight,
    )


def _obvious_provenance(
    component: Sequence[_Supernode],
    *,
    pair_index: Mapping[frozenset[NodeKey], ScoredIdentityPair],
    constraints: Sequence[str],
) -> IdentityAssignmentProvenance:
    chosen = tuple(_matching_pairs_in_component(component, pair_index=pair_index))
    return IdentityAssignmentProvenance(
        method="obvious_union",
        component_id=_component_id(node for item in component for node in item.members),
        chosen_edges=chosen,
        rejected_competing_edges=(),
        confidence_margin=None,
        constraints=tuple(constraints),
        global_weight=round(sum(edge.confidence for edge in chosen), 6) if chosen else 0.0,
    )


@dataclass
class _IdentityAssignmentPrep:
    unique_nodes: list[NodeKey]
    pair_index: dict[frozenset[NodeKey], ScoredIdentityPair]
    incomplete_nodes: set[NodeKey]
    diagnostics: IdentityGraphDiagnostics
    components: list[list[_Supernode]]
    base_constraints: tuple[str, ...]
    margin: float


def _prepare_identity_assignment(
    nodes: Sequence[NodeKey],
    scored_pairs: Sequence[ScoredIdentityPair],
    *,
    unscored_candidate_keys: Iterable[frozenset[NodeKey]] = (),
    unscored_nodes: Iterable[NodeKey] | None = None,
    threshold: float,
    margin: float = DEFAULT_ASSIGNMENT_MARGIN,
) -> _IdentityAssignmentPrep | None:
    unique_nodes = sorted(set(nodes), key=node_sort_key)
    pair_index = _index_pairs(scored_pairs)
    unscored_keys = {key for key in unscored_candidate_keys if len(key) == 2}
    incomplete_nodes = (
        set(unscored_nodes)
        if unscored_nodes is not None
        else _unscored_endpoint_nodes(unscored_keys)
    )
    diagnostics = IdentityGraphDiagnostics()
    if not unique_nodes:
        return None
    supernodes = _sibling_supernodes(unique_nodes, scored_pairs)
    diagnostics.sibling_groups = sum(1 for item in supernodes if len(item.members) > 1)
    adjacency = _matching_supernode_adjacency(supernodes, pair_index=pair_index)
    components = _connected_components(supernodes, adjacency)
    diagnostics.components = len(components)
    return _IdentityAssignmentPrep(
        unique_nodes=unique_nodes,
        pair_index=pair_index,
        incomplete_nodes=incomplete_nodes,
        diagnostics=diagnostics,
        components=components,
        base_constraints=(
            CONSTRAINT_ONE_SUPERNODE_PER_VENUE,
            CONSTRAINT_SIBLINGS_PERMITTED,
            f"event_matcher_threshold={threshold:.2f}",
            f"{CONSTRAINT_MARGIN}={margin:.2f}",
            CONSTRAINT_HARD_VETO,
        ),
        margin=margin,
    )


def _clusters_for_component(
    component: Sequence[_Supernode],
    *,
    prep: _IdentityAssignmentPrep,
) -> list[AssignedIdentityCluster]:
    pair_index = prep.pair_index
    diagnostics = prep.diagnostics
    base_constraints = prep.base_constraints
    # Singletons cannot be incomplete-ambiguous; skip the O(unscored)
    # scan that previously walked every leftover candidate per component.
    incomplete = (
        False
        if len(component) <= 1
        else _component_incomplete(component, unscored_nodes=prep.incomplete_nodes)
    )
    competing = _venues_compete(component)
    obvious = _is_obvious_clique(component, pair_index=pair_index)
    contradictory = _has_cross_venue_veto_path(component, pair_index=pair_index)
    matching_edges = _matching_pairs_in_component(component, pair_index=pair_index)
    _, greedy_weight = greedy_local_pairwise_assignment(matching_edges)

    if obvious and not competing and not contradictory:
        if incomplete and len(component) > 1:
            diagnostics.fail_closed_components += 1
            provenance = _fail_closed_provenance(
                component,
                method="fail_closed",
                reason="incomplete_candidate_evidence",
                constraints=[*base_constraints, CONSTRAINT_INCOMPLETE],
                pair_index=pair_index,
                greedy_weight=greedy_weight,
            )
            return _singleton_clusters(component, provenance)
        diagnostics.obvious_components += 1
        provenance = _obvious_provenance(
            component,
            pair_index=pair_index,
            constraints=[*base_constraints, CONSTRAINT_CLIQUE],
        )
        members = frozenset(node for item in component for node in item.members)
        return [AssignedIdentityCluster(member_keys=members, provenance=provenance)]

    diagnostics.ambiguous_components += 1
    if contradictory:
        diagnostics.contradictory_components += 1
        diagnostics.fail_closed_components += 1
        provenance = _fail_closed_provenance(
            component,
            method="fail_closed",
            reason="contradictory_component",
            constraints=[*base_constraints, "cross_venue_veto_on_matching_path"],
            pair_index=pair_index,
            greedy_weight=greedy_weight,
        )
        return _singleton_clusters(component, provenance)
    if incomplete:
        diagnostics.fail_closed_components += 1
        provenance = _fail_closed_provenance(
            component,
            method="fail_closed",
            reason="incomplete_candidate_evidence",
            constraints=[*base_constraints, CONSTRAINT_INCOMPLETE],
            pair_index=pair_index,
            greedy_weight=greedy_weight,
        )
        return _singleton_clusters(component, provenance)
    if len(component) > MAX_AMBIGUOUS_SUPERNODES:
        diagnostics.fail_closed_components += 1
        provenance = _fail_closed_provenance(
            component,
            method="fail_closed",
            reason="ambiguous_component_too_large",
            constraints=[*base_constraints, CONSTRAINT_ENUMERATION_CAP],
            pair_index=pair_index,
            greedy_weight=greedy_weight,
        )
        return _singleton_clusters(component, provenance)

    best, second = _best_two_partitions(component, pair_index=pair_index)
    if best is None or best[1] <= 0.0:
        diagnostics.fail_closed_components += 1
        provenance = _fail_closed_provenance(
            component,
            method="fail_closed",
            reason="no_legal_global_assignment",
            constraints=base_constraints,
            pair_index=pair_index,
            greedy_weight=greedy_weight,
            global_weight=0.0,
            margin=0.0,
        )
        return _singleton_clusters(component, provenance)

    best_partition, best_weight = best
    second_weight = 0.0 if second is None else float(second[1])
    confidence_margin = round(best_weight - second_weight, 6)
    if confidence_margin < prep.margin:
        diagnostics.fail_closed_components += 1
        provenance = _fail_closed_provenance(
            component,
            method="fail_closed",
            reason="assignment_margin_too_small",
            constraints=base_constraints,
            pair_index=pair_index,
            greedy_weight=greedy_weight,
            global_weight=best_weight,
            margin=confidence_margin,
        )
        return _singleton_clusters(component, provenance)

    chosen, rejected = _pairs_for_clusters(
        component, best_partition, pair_index=pair_index
    )
    diagnostics.global_assignments += 1
    provenance = IdentityAssignmentProvenance(
        method="global_max_weight",
        component_id=_component_id(node for item in component for node in item.members),
        chosen_edges=chosen,
        rejected_competing_edges=rejected,
        confidence_margin=confidence_margin,
        constraints=(*base_constraints, CONSTRAINT_CLIQUE),
        greedy_weight=greedy_weight,
        global_weight=best_weight,
    )
    return _clusters_from_partition(component, best_partition, provenance)


def _finish_identity_assignment(
    prep: _IdentityAssignmentPrep,
    assigned: list[AssignedIdentityCluster],
) -> IdentityGraphResult:
    assigned.sort(
        key=lambda cluster: tuple(sorted(cluster.member_keys, key=node_sort_key))
    )
    covered = {node for cluster in assigned for node in cluster.member_keys}
    if set(prep.unique_nodes) != covered:
        missing = [node for node in prep.unique_nodes if node not in covered]
        raise RuntimeError(f"identity graph dropped nodes: {missing!r}")
    return IdentityGraphResult(clusters=assigned, diagnostics=prep.diagnostics)


def assign_identity_components(
    nodes: Sequence[NodeKey],
    scored_pairs: Sequence[ScoredIdentityPair],
    *,
    unscored_candidate_keys: Iterable[frozenset[NodeKey]] = (),
    unscored_nodes: Iterable[NodeKey] | None = None,
    threshold: float,
    margin: float = DEFAULT_ASSIGNMENT_MARGIN,
) -> IdentityGraphResult:
    """Partition source events into canonical fixture clusters.

    ``nodes`` must include every source event. ``scored_pairs`` are EventMatcher
    outcomes on indexed candidates. Unscored candidates make an otherwise
    ambiguous component fail closed rather than guess from partial evidence.
    """

    prep = _prepare_identity_assignment(
        nodes,
        scored_pairs,
        unscored_candidate_keys=unscored_candidate_keys,
        unscored_nodes=unscored_nodes,
        threshold=threshold,
        margin=margin,
    )
    if prep is None:
        return IdentityGraphResult(clusters=[], diagnostics=IdentityGraphDiagnostics())
    assigned: list[AssignedIdentityCluster] = []
    for component in prep.components:
        assigned.extend(_clusters_for_component(component, prep=prep))
    return _finish_identity_assignment(prep, assigned)


async def assign_identity_components_cooperative(
    nodes: Sequence[NodeKey],
    scored_pairs: Sequence[ScoredIdentityPair],
    *,
    unscored_candidate_keys: Iterable[frozenset[NodeKey]] = (),
    unscored_nodes: Iterable[NodeKey] | None = None,
    threshold: float,
    margin: float = DEFAULT_ASSIGNMENT_MARGIN,
    yield_every: int = 64,
) -> IdentityGraphResult:
    """Same assignment as ``assign_identity_components``, yielding to the loop.

    Dense UNIVERSE finalize must not monopolise asyncio for the 0.25s liveness
    bound. Yield before sync prep so a large node/pair index cannot occupy the
    whole slice, then walk components with the same 0.05s cap as ClusterPass.
    """

    await asyncio.sleep(0)
    prep = _prepare_identity_assignment(
        nodes,
        scored_pairs,
        unscored_candidate_keys=unscored_candidate_keys,
        unscored_nodes=unscored_nodes,
        threshold=threshold,
        margin=margin,
    )
    if prep is None:
        return IdentityGraphResult(clusters=[], diagnostics=IdentityGraphDiagnostics())
    assigned: list[AssignedIdentityCluster] = []
    pause_every = max(1, int(yield_every))
    await asyncio.sleep(0)
    last_yield = time.perf_counter()
    for index, component in enumerate(prep.components, start=1):
        assigned.extend(_clusters_for_component(component, prep=prep))
        if index % pause_every == 0 or (
            time.perf_counter() - last_yield
        ) >= _ASSIGN_COOP_MAX_SLICE_SECONDS:
            await asyncio.sleep(0)
            last_yield = time.perf_counter()
    return _finish_identity_assignment(prep, assigned)
