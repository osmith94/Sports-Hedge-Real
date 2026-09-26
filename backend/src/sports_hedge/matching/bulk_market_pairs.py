"""Same-answer market pairing that does not compare every market with every market.

``MarketMatcher.match`` stays the authority. A pair can match only when both
markets have the same single-market Approved Match Register key: every
sport register returns a key only when the two single-market keys are equal
and non-empty. Pairs that fail that test are not sent to the matcher.

Within one key, event identity is shared by many markets. One probe match
per event/venue block decides admission and confidence. Greedy uniqueness
then runs on that block. ``MarketMatcher.match`` is called again only for
the pairs that greedy actually keeps, so reasons stay pair-specific.

Candidate order matches the historical left-major enumeration, so equal
confidence still prefers the earlier market index.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence

from sports_hedge.domain.football import CanonicalMarket
from sports_hedge.matching.approved_register import canonical_key_for_market
from sports_hedge.matching.markets import (
    MarketMatcher,
    MarketMatchResult,
    economic_match_fingerprint,
    memoize_market_matches,
)

PriorityPair = Callable[[CanonicalMarket, CanonicalMarket], bool]


def greedy_unique_market_matches(
    left: Sequence[CanonicalMarket | None],
    right: Sequence[CanonicalMarket | None],
    matcher: MarketMatcher,
    *,
    priority_pair: PriorityPair | None = None,
) -> list[tuple[int, int, MarketMatchResult]]:
    """Greedy one-to-one matches in the same order as a full cartesian scan.

    ``priority_pair`` matches collector baseline ordering. Inventory leaves
    it unset and sorts by matcher confidence only.
    """

    if not left or not right:
        return []
    left_keys = [_register_key(market) for market in left]
    right_keys = [_register_key(market) for market in right]
    left_fp = [_fingerprint(market, key) for market, key in zip(left, left_keys, strict=True)]
    right_fp = [_fingerprint(market, key) for market, key in zip(right, right_keys, strict=True)]
    rights_by_key: dict[str, list[int]] = {}
    for index, key in enumerate(right_keys):
        if key is not None:
            rights_by_key.setdefault(key, []).append(index)
    left_groups = _first_index_by_block(left, left_keys)
    right_groups = _first_index_by_block(right, right_keys)

    selected: list[tuple[int, float, int, int]] = []
    with memoize_market_matches():
        admitted = _admitted_blocks(left, right, matcher, left_groups, right_groups)
        for key, right_indexes in rights_by_key.items():
            left_indexes = [index for index, item in enumerate(left_keys) if item == key]
            if not left_indexes:
                continue
            blocks = _admitted_sides(
                left,
                right,
                key,
                left_indexes,
                right_indexes,
                admitted,
            )
            if not blocks:
                continue
            if (
                len(blocks) == 1
                and _uniform(left_fp, left_indexes)
                and _uniform(right_fp, right_indexes)
            ):
                confidence, side_left, side_right = blocks[0]
                priority = _priority(priority_pair, left[side_left[0]], right[side_right[0]])
                for left_index, right_index in zip(side_left, side_right, strict=False):
                    selected.append((priority, confidence, left_index, right_index))
                continue
            selected.extend(
                _greedy_block(
                    left,
                    right,
                    key,
                    left_indexes,
                    right_indexes,
                    left_fp,
                    right_fp,
                    admitted,
                    priority_pair,
                )
            )
        selected.sort(key=lambda item: (-item[0], -item[1], item[2], item[3]))
        return [
            (left_index, right_index, matcher.match(left[left_index], right[right_index]))
            for _priority_flag, _confidence, left_index, right_index in selected
            if left[left_index] is not None and right[right_index] is not None
        ]


def _register_key(market: CanonicalMarket | None) -> str | None:
    if market is None:
        return None
    return canonical_key_for_market(market)


def _fingerprint(market: CanonicalMarket | None, key: str | None) -> tuple[object, ...] | None:
    if market is None or key is None:
        return None
    return economic_match_fingerprint(market)


def _first_index_by_block(
    markets: Sequence[CanonicalMarket | None],
    keys: Sequence[str | None],
) -> dict[tuple[int, object, str], int]:
    groups: dict[tuple[int, object, str], int] = {}
    for index, (market, key) in enumerate(zip(markets, keys, strict=True)):
        if market is None or key is None:
            continue
        groups.setdefault((id(market.event), market.source_venue, key), index)
    return groups


def _admitted_blocks(
    left: Sequence[CanonicalMarket | None],
    right: Sequence[CanonicalMarket | None],
    matcher: MarketMatcher,
    left_groups: dict[tuple[int, object, str], int],
    right_groups: dict[tuple[int, object, str], int],
) -> dict[tuple[int, int, object, object, str], float]:
    by_key_right: dict[str, list[tuple[int, object, int]]] = {}
    for (event_id, venue, key), index in right_groups.items():
        by_key_right.setdefault(key, []).append((event_id, venue, index))
    admitted: dict[tuple[int, int, object, object, str], float] = {}
    for (left_event_id, left_venue, key), left_index in left_groups.items():
        left_market = left[left_index]
        if left_market is None:
            continue
        for right_event_id, right_venue, right_index in by_key_right.get(key, ()):
            right_market = right[right_index]
            if right_market is None:
                continue
            probe = matcher.match(left_market, right_market)
            if probe.matched:
                admitted[(left_event_id, right_event_id, left_venue, right_venue, key)] = (
                    probe.confidence
                )
    return admitted


def _admitted_sides(
    left: Sequence[CanonicalMarket | None],
    right: Sequence[CanonicalMarket | None],
    key: str,
    left_indexes: list[int],
    right_indexes: list[int],
    admitted: dict[tuple[int, int, object, object, str], float],
) -> list[tuple[float, list[int], list[int]]]:
    left_sides: dict[tuple[int, object], list[int]] = {}
    for index in left_indexes:
        market = left[index]
        if market is None:
            continue
        left_sides.setdefault((id(market.event), market.source_venue), []).append(index)
    right_sides: dict[tuple[int, object], list[int]] = {}
    for index in right_indexes:
        market = right[index]
        if market is None:
            continue
        right_sides.setdefault((id(market.event), market.source_venue), []).append(index)
    blocks: list[tuple[float, list[int], list[int]]] = []
    for (left_event_id, left_venue), side_left in left_sides.items():
        for (right_event_id, right_venue), side_right in right_sides.items():
            confidence = admitted.get((left_event_id, right_event_id, left_venue, right_venue, key))
            if confidence is None:
                continue
            blocks.append((confidence, side_left, side_right))
    return blocks


def _uniform(fingerprints: Sequence[tuple[object, ...] | None], indexes: Sequence[int]) -> bool:
    first = fingerprints[indexes[0]]
    return all(fingerprints[index] == first for index in indexes)


def _priority(
    priority_pair: PriorityPair | None,
    left: CanonicalMarket | None,
    right: CanonicalMarket | None,
) -> int:
    if priority_pair is None or left is None or right is None:
        return 0
    return 1 if priority_pair(left, right) else 0


def _greedy_block(
    left: Sequence[CanonicalMarket | None],
    right: Sequence[CanonicalMarket | None],
    key: str,
    left_indexes: list[int],
    right_indexes: list[int],
    left_fp: Sequence[tuple[object, ...] | None],
    right_fp: Sequence[tuple[object, ...] | None],
    admitted: dict[tuple[int, int, object, object, str], float],
    priority_pair: PriorityPair | None,
) -> list[tuple[int, float, int, int]]:
    priority_cache: dict[tuple[object, object], int] = {}
    edges: list[tuple[int, float, int, int]] = []
    for left_index in left_indexes:
        left_market = left[left_index]
        if left_market is None:
            continue
        for right_index in right_indexes:
            right_market = right[right_index]
            if right_market is None:
                continue
            confidence = admitted.get(
                (
                    id(left_market.event),
                    id(right_market.event),
                    left_market.source_venue,
                    right_market.source_venue,
                    key,
                )
            )
            if confidence is None:
                continue
            if priority_pair is None:
                priority = 0
            else:
                cache_key = (left_fp[left_index], right_fp[right_index])
                cached = priority_cache.get(cache_key)
                if cached is None:
                    cached = _priority(priority_pair, left_market, right_market)
                    priority_cache[cache_key] = cached
                priority = cached
            edges.append((priority, confidence, left_index, right_index))
    edges.sort(key=lambda item: (item[0], item[1]), reverse=True)
    used_left: set[int] = set()
    used_right: set[int] = set()
    chosen: list[tuple[int, float, int, int]] = []
    for priority, confidence, left_index, right_index in edges:
        if left_index in used_left or right_index in used_right:
            continue
        used_left.add(left_index)
        used_right.add(right_index)
        chosen.append((priority, confidence, left_index, right_index))
    return chosen
