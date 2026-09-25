"""Diagnostic UNIVERSE fixture-matching review.

Built only from evidence production already retained. It does not score new
pairs, call venues, or change identity assignment. Missing evidence is labelled
``unavailable`` rather than omitted.
"""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from sports_hedge.application.fixture_clusters import (
    cluster_canonical_event_id,
    cluster_member_events,
)
from sports_hedge.application.opportunity_viability import build_viability_evidence
from sports_hedge.application.universe_identity_cache import identity_cache_semantic_version
from sports_hedge.matching.identity_graph import DEFAULT_ASSIGNMENT_MARGIN

SCHEMA_VERSION = "universe-matching-review/1"
TOP_NEARBY_CANDIDATES = 3
MAX_STORED_SCOPE_REJECTIONS = 2000
MAX_STORED_NORMALIZATION_REJECTIONS = 500
MAX_REPORT_REJECTED_EDGES = 24

EVIDENCE_RETAINED = "retained"
EVIDENCE_UNAVAILABLE = "unavailable"
EVIDENCE_NOT_APPLICABLE = "not_applicable"
EVIDENCE_NOT_SCORED = "not_scored_by_production"

STAGE_DISCOVERY_NOT_RETURNED = "provider_discovery_not_returned"
STAGE_SCOPE_REJECTED = "competition_scope_rejected"
STAGE_NORMALIZATION_REJECTED = "normalization_rejected"
STAGE_NO_CANDIDATE = "no_candidate_pair_generated"
STAGE_GENERATED_NOT_SCORED = "candidate_generated_not_scored"
STAGE_SCORED_REJECTED = "candidate_scored_and_rejected"
STAGE_ASSIGNED_ELSEWHERE = "eligible_assigned_elsewhere"
STAGE_FAIL_CLOSED = "graph_assignment_fail_closed"
STAGE_UNLINKED = "unlinked_scope_or_normalization"
STAGE_MATCHED = "matched"
STAGE_EVIDENCE_UNAVAILABLE = "evidence_unavailable"

_VENUE_ORDER = {"matchbook": 0, "polymarket": 1, "kalshi": 2}
_LABEL_KEYS = ("title", "name", "question", "event_title", "subtitle")
_HOME_KEYS = ("home", "home_team", "homeTeam")
_AWAY_KEYS = ("away", "away_team", "awayTeam")

_HOW_TO_READ = (
    (
        "Each venue member has a stage_trail. evidence=retained is production state; "
        "evidence=unavailable means production did not retain that fact; "
        "evidence=not_applicable means the stage never ran; "
        "evidence=not_scored_by_production means the pair was not an EventMatcher input."
    ),
    (
        "normalized_identity.identity_rule names the sport's match rule. "
        "Football uses participant identity plus the 5-minute kickoff tolerance. "
        "NFL and basketball use curated clubs plus that tolerance. "
        "MLB uses curated clubs, explicit doubleheader ordinals, and the same "
        "inclusive 5-minute kickoff tolerance. A minute embedded in "
        "scheduled_game_key does not override that window. A missing or "
        "one-sided ordinal stays fail-closed. Game 1 never matches Game 2. "
        "Tennis uses an unordered player pair, tour, admitted tournament and round, "
        "with a 14-day supporting window. ATP/WTA coverage is Hangzhou, Chengdu, "
        "Singapore and Seoul only."
    ),
    "provider_discovery_not_returned: no retained source event on that venue was linked, and raw discovery count is zero.",
    "competition_scope_rejected: a returned event was kept out of normalization by the competition/scope filter.",
    "normalization_rejected: a returned in-scope event failed normalization before clustering.",
    "no_candidate_pair_generated: normalized events existed but the identity index did not emit a pair. For the same normalized teams inside the authoritative sport window this is an index correctness signal, not a different fixture.",
    "candidate_generated_not_scored: a candidate pair was indexed but not scored before the pass stopped.",
    "candidate_scored_and_rejected: EventMatcher scored the pair and did not accept it.",
    "eligible_assigned_elsewhere: a scored pair was eligible and graph assignment placed the other event in a different cluster.",
    "graph_assignment_fail_closed: eligible or contradictory edges were retained and assignment refused the component.",
    "Nearby rows with confidence null were not scored. Do not treat kickoff proximity as a match score.",
    "This document is diagnostic only. Scanner decisions do not read it.",
)


def new_matching_review_slot() -> dict[str, Any]:
    return {
        "identity_evidence": EVIDENCE_UNAVAILABLE,
        "identity_evidence_reason": "clustering_not_finished",
        "nodes": [],
        "clusters": [],
        "scored_pairs": [],
        "generated_pairs": [],
        "unscored_node_keys": [],
        "scope_rejections": [],
        "scope_rejections_truncated": False,
        "normalization_rejections": [],
        "normalization_rejections_truncated": False,
        "matcher_threshold": None,
        "matcher_semantic_version": None,
        "assignment_margin": DEFAULT_ASSIGNMENT_MARGIN,
        "clustering_truncated": False,
    }


def clip_text(value: object, limit: int = 300) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text:
        return None
    return text[:limit]


def raw_display_fields(raw: object) -> dict[str, Any]:
    payload = raw if isinstance(raw, dict) else {}
    label = _first_text(payload, _LABEL_KEYS)
    home = _first_text(payload, _HOME_KEYS)
    away = _first_text(payload, _AWAY_KEYS)
    return {
        "provider_label": label,
        "provider_label_evidence": EVIDENCE_RETAINED if label else EVIDENCE_UNAVAILABLE,
        "raw_home": home,
        "raw_home_evidence": EVIDENCE_RETAINED if home else EVIDENCE_UNAVAILABLE,
        "raw_away": away,
        "raw_away_evidence": EVIDENCE_RETAINED if away else EVIDENCE_UNAVAILABLE,
    }


def _first_text(payload: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        text = clip_text(payload.get(key))
        if text:
            return text
    return None


def _node_ref(venue: object, source_event_id: object) -> tuple[str, str]:
    venue_text = getattr(venue, "value", venue)
    return (str(venue_text), str(source_event_id))


def _iso(value: object) -> str | None:
    if isinstance(value, datetime):
        return value.isoformat()
    text = clip_text(value, limit=80)
    return text


def venue_combination(venues: list[str]) -> str:
    ordered = sorted(set(venues), key=lambda item: (_VENUE_ORDER.get(item, 9), item))
    if len(ordered) >= 2:
        return "+".join(ordered)
    if len(ordered) == 1:
        return f"unmatched:{ordered[0]}"
    return "unmatched:none"


def _kickoff_delta(left: str | None, right: str | None) -> tuple[float | None, str]:
    if not left or not right:
        return None, EVIDENCE_UNAVAILABLE
    try:
        left_dt = datetime.fromisoformat(left)
        right_dt = datetime.fromisoformat(right)
    except ValueError:
        return None, EVIDENCE_UNAVAILABLE
    return abs((left_dt - right_dt).total_seconds()), EVIDENCE_RETAINED


async def capture_identity_review_evidence(
    *,
    runs: list[Any],
    matcher: Any,
    truncated: bool,
    unscored: set[tuple[Any, str]],
) -> dict[str, Any]:
    """Copy identity-graph evidence off the clustering task, yielding the loop."""

    from sports_hedge.application.event_loop_activity import yield_event_loop

    nodes: list[dict[str, Any]] = []
    clusters: list[dict[str, Any]] = []
    scored_pairs: list[dict[str, Any]] = []
    generated_pairs: list[dict[str, Any]] = []
    last_yield = time.perf_counter()
    ticks = 0

    async def _pause() -> None:
        nonlocal last_yield, ticks
        ticks += 1
        if ticks % 128 != 0 and (time.perf_counter() - last_yield) < 0.05:
            return
        await yield_event_loop()
        last_yield = time.perf_counter()

    unscored_keys = {_node_ref(venue, source_id) for venue, source_id in unscored}
    for run in runs:
        shard = run.shard
        loaded = bool(run.loaded)
        for item in shard.events:
            await _pause()
            canonical = item.canonical
            key = _node_ref(item.venue, item.source_event_id)
            if not loaded:
                generation_evidence = EVIDENCE_UNAVAILABLE
                generation_reason = "shard_not_loaded"
            elif key in unscored_keys:
                generation_evidence = "partial"
                generation_reason = "candidates_not_fully_scored"
            else:
                generation_evidence = EVIDENCE_RETAINED
                generation_reason = None
            display = raw_display_fields(item.raw)
            nodes.append(
                {
                    "venue": key[0],
                    "source_event_id": key[1],
                    "shard_id": shard.shard_id,
                    "shard_loaded": loaded,
                    "candidate_generation_evidence": generation_evidence,
                    "candidate_generation_reason": generation_reason,
                    **display,
                    "sport": str(getattr(canonical, "sport", "") or "") or None,
                    "competition": str(getattr(canonical, "competition", "") or "") or None,
                    "home_team": str(getattr(canonical, "home_team", "") or "") or None,
                    "away_team": str(getattr(canonical, "away_team", "") or "") or None,
                    "kickoff_utc": _iso(getattr(canonical, "kickoff_utc", None)),
                    "tournament": str(getattr(canonical, "tournament", "") or "") or None,
                    "round_label": str(getattr(canonical, "round_label", "") or "") or None,
                    "event_type": str(getattr(canonical, "event_type", "") or "") or None,
                    "scheduled_game_key": str(getattr(canonical, "scheduled_game_key", "") or "")
                    or None,
                    "normalization_evidence": EVIDENCE_RETAINED,
                }
            )
        if loaded:
            cluster_pass = run.cluster_pass
            scored_keys: set[frozenset[tuple[str, str]]] = set()
            for pair in cluster_pass.scored_pairs:
                await _pause()
                record = dict(pair.as_record())
                left = _node_ref(record["left_venue"], record["left_source_event_id"])
                right = _node_ref(record["right_venue"], record["right_source_event_id"])
                scored_keys.add(frozenset({left, right}))
                scored_pairs.append(record)
            for left, right in cluster_pass._candidates:
                await _pause()
                left_key = _node_ref(left.venue, left.source_event_id)
                right_key = _node_ref(right.venue, right.source_event_id)
                generated_pairs.append(
                    {
                        "left_venue": left_key[0],
                        "left_source_event_id": left_key[1],
                        "right_venue": right_key[0],
                        "right_source_event_id": right_key[1],
                        "scored": frozenset({left_key, right_key}) in scored_keys,
                    }
                )
        for cluster in run.clusters:
            await _pause()
            members = [
                list(_node_ref(item.venue, item.source_event_id))
                for item in cluster_member_events(cluster)
            ]
            members.sort(key=lambda item: (item[0], item[1]))
            provenance = cluster.identity_provenance
            clusters.append(
                {
                    "canonical_event_id": cluster_canonical_event_id(cluster),
                    "member_keys": members,
                    "venues": [venue.value for venue in cluster.venues_present],
                    "pair_kinds": sorted(cluster.pair_kinds),
                    "event_match_confidence": cluster.event_match_confidence,
                    "event_match_threshold": cluster.event_match_threshold,
                    "provenance": None if provenance is None else provenance.as_record(),
                    "provenance_evidence": (
                        EVIDENCE_RETAINED if provenance is not None else EVIDENCE_UNAVAILABLE
                    ),
                    "shard_id": shard.shard_id,
                    "viability_evidence": build_viability_evidence(
                        cluster_canonical_event_id(cluster),
                        venues_present=[venue.value for venue in cluster.venues_present],
                    ),
                }
            )
    threshold = getattr(matcher, "threshold", None)
    return {
        "identity_evidence": EVIDENCE_RETAINED,
        "identity_evidence_reason": None,
        "nodes": nodes,
        "clusters": clusters,
        "scored_pairs": scored_pairs,
        "generated_pairs": generated_pairs,
        "unscored_node_keys": sorted(
            [list(key) for key in unscored_keys], key=lambda item: (item[0], item[1])
        ),
        "matcher_threshold": None if threshold is None else float(threshold),
        "matcher_semantic_version": identity_cache_semantic_version(matcher),
        "assignment_margin": DEFAULT_ASSIGNMENT_MARGIN,
        "clustering_truncated": bool(truncated),
    }


_ATTACHED: dict[int, dict[str, Any]] = {}


def attach_universe_matching_evidence(report: object, evidence: dict[str, Any]) -> None:
    """Store evidence on the report, with an id-keyed fallback when setattr fails.

    ``take_universe_matching_evidence`` always drops the fallback entry so a
    successful direct attachment cannot leave ``_ATTACHED`` growing forever.
    """

    _ATTACHED[id(report)] = evidence
    try:
        object.__setattr__(report, "_universe_matching_evidence", evidence)
    except (AttributeError, TypeError):
        return


def take_universe_matching_evidence(report: object) -> dict[str, Any] | None:
    fallback = _ATTACHED.pop(id(report), None)
    payload = getattr(report, "_universe_matching_evidence", None)
    if isinstance(payload, dict):
        return payload
    if isinstance(fallback, dict):
        return fallback
    return None


def _stage(status: str, evidence: str, **extra: Any) -> dict[str, Any]:
    row: dict[str, Any] = {"status": status, "evidence": evidence}
    row.update(extra)
    return row


def _enabled_venues(meta: dict[str, Any]) -> list[str]:
    raw = meta.get("enabled_venues")
    if isinstance(raw, list) and raw:
        return [str(item) for item in raw]
    return ["matchbook", "polymarket", "kalshi"]


def _raw_count(meta: dict[str, Any], venue: str) -> int | None:
    counts = meta.get("source_event_counts")
    if not isinstance(counts, dict):
        return None
    raw = counts.get("raw_by_venue")
    if not isinstance(raw, dict) or venue not in raw:
        return None
    try:
        return int(raw[venue])
    except (TypeError, ValueError):
        return None


def _index_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    nodes = [item for item in evidence.get("nodes") or [] if isinstance(item, dict)]
    nodes.sort(key=lambda item: (str(item.get("venue")), str(item.get("source_event_id"))))
    node_index = {
        (str(item.get("venue")), str(item.get("source_event_id"))): item for item in nodes
    }
    scored_by_node: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for pair in evidence.get("scored_pairs") or []:
        if not isinstance(pair, dict):
            continue
        left = (str(pair.get("left_venue")), str(pair.get("left_source_event_id")))
        right = (str(pair.get("right_venue")), str(pair.get("right_source_event_id")))
        scored_by_node.setdefault(left, []).append(pair)
        scored_by_node.setdefault(right, []).append(pair)
    generated_by_node: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for pair in evidence.get("generated_pairs") or []:
        if not isinstance(pair, dict):
            continue
        left = (str(pair.get("left_venue")), str(pair.get("left_source_event_id")))
        right = (str(pair.get("right_venue")), str(pair.get("right_source_event_id")))
        generated_by_node.setdefault(left, []).append(pair)
        generated_by_node.setdefault(right, []).append(pair)
    clusters = [item for item in evidence.get("clusters") or [] if isinstance(item, dict)]
    cluster_by_member: dict[tuple[str, str], dict[str, Any]] = {}
    for cluster in clusters:
        for key in cluster.get("member_keys") or []:
            if isinstance(key, (list, tuple)) and len(key) == 2:
                cluster_by_member[(str(key[0]), str(key[1]))] = cluster
    nodes_by_venue: dict[str, list[dict[str, Any]]] = {}
    for item in nodes:
        nodes_by_venue.setdefault(str(item.get("venue")), []).append(item)
    scope_by_venue: dict[str, list[dict[str, Any]]] = {}
    for item in evidence.get("scope_rejections") or []:
        if isinstance(item, dict):
            scope_by_venue.setdefault(str(item.get("venue")), []).append(item)
    normalization_by_venue: dict[str, list[dict[str, Any]]] = {}
    for item in evidence.get("normalization_rejections") or []:
        if isinstance(item, dict):
            normalization_by_venue.setdefault(str(item.get("venue")), []).append(item)
    return {
        "nodes": nodes,
        "node_index": node_index,
        "scored_by_node": scored_by_node,
        "generated_by_node": generated_by_node,
        "clusters": clusters,
        "cluster_by_member": cluster_by_member,
        "nodes_by_venue": nodes_by_venue,
        "scope_by_venue": scope_by_venue,
        "normalization_by_venue": normalization_by_venue,
    }


def _other_end(pair: dict[str, Any], node: tuple[str, str]) -> tuple[str, str]:
    left = (str(pair.get("left_venue")), str(pair.get("left_source_event_id")))
    right = (str(pair.get("right_venue")), str(pair.get("right_source_event_id")))
    return right if left == node else left


def _pair_rank(pair: dict[str, Any]) -> tuple[float, str, str, str, str]:
    try:
        confidence = float(pair.get("confidence") or 0.0)
    except (TypeError, ValueError):
        confidence = 0.0
    return (
        -confidence,
        str(pair.get("left_venue")),
        str(pair.get("left_source_event_id")),
        str(pair.get("right_venue")),
        str(pair.get("right_source_event_id")),
    )


def _candidate_from_scored(
    pair: dict[str, Any],
    other: tuple[str, str],
    node_index: dict[tuple[str, str], dict[str, Any]],
    *,
    threshold: float | None,
    rejection_reason: str,
) -> dict[str, Any]:
    other_node = node_index.get(other) or {}
    other_kickoff = other_node.get("kickoff_utc")
    # Kickoff delta is filled by the caller when both endpoints are known.
    confidence = pair.get("confidence")
    reasons = pair.get("reasons")
    return {
        "venue": other[0],
        "source_event_id": other[1],
        "provider_label": other_node.get("provider_label"),
        "provider_label_evidence": other_node.get("provider_label_evidence", EVIDENCE_UNAVAILABLE),
        "normalized_home": other_node.get("home_team"),
        "normalized_away": other_node.get("away_team"),
        "normalized_competition": other_node.get("competition"),
        "kickoff_utc": other_kickoff,
        "normalized_identity_evidence": (
            EVIDENCE_RETAINED if other_node else EVIDENCE_UNAVAILABLE
        ),
        "shard_id": other_node.get("shard_id"),
        "confidence": confidence,
        "confidence_evidence": EVIDENCE_RETAINED if confidence is not None else EVIDENCE_UNAVAILABLE,
        "threshold": threshold,
        "threshold_evidence": EVIDENCE_RETAINED if threshold is not None else EVIDENCE_UNAVAILABLE,
        "kickoff_delta_seconds": pair.get("kickoff_delta_seconds"),
        "kickoff_delta_evidence": pair.get("kickoff_delta_evidence", EVIDENCE_UNAVAILABLE),
        "matched": pair.get("matched"),
        "veto": pair.get("veto"),
        "reasons": list(reasons) if isinstance(reasons, list) else None,
        "reasons_evidence": EVIDENCE_RETAINED if isinstance(reasons, list) else EVIDENCE_UNAVAILABLE,
        "rejection_reason": rejection_reason,
        "ranking": "production_score",
        "same_shard": None,
    }


def _fill_kickoff_delta(
    candidate: dict[str, Any],
    left_kickoff: str | None,
    right_kickoff: str | None,
) -> None:
    if candidate.get("kickoff_delta_seconds") is not None:
        candidate["kickoff_delta_evidence"] = candidate.get("kickoff_delta_evidence") or EVIDENCE_RETAINED
        return
    delta, evidence = _kickoff_delta(left_kickoff, right_kickoff)
    candidate["kickoff_delta_seconds"] = delta
    candidate["kickoff_delta_evidence"] = evidence


def _nearby_unscored(
    *,
    origin: dict[str, Any],
    venue: str,
    nodes_by_venue: dict[str, list[dict[str, Any]]],
    exclude: set[tuple[str, str]],
) -> list[dict[str, Any]]:
    origin_key = (str(origin.get("venue")), str(origin.get("source_event_id")))
    origin_shard = origin.get("shard_id")
    origin_kickoff = origin.get("kickoff_utc")
    ranked: list[tuple[tuple, dict[str, Any]]] = []
    for item in nodes_by_venue.get(venue, []):
        key = (str(item.get("venue")), str(item.get("source_event_id")))
        if key == origin_key or key in exclude:
            continue
        delta, evidence = _kickoff_delta(origin_kickoff, item.get("kickoff_utc"))
        same_shard = item.get("shard_id") == origin_shard
        sort = (
            0 if same_shard else 1,
            delta if delta is not None else float("inf"),
            key[0],
            key[1],
        )
        ranked.append(
            (
                sort,
                {
                    "venue": key[0],
                    "source_event_id": key[1],
                    "provider_label": item.get("provider_label"),
                    "provider_label_evidence": item.get(
                        "provider_label_evidence", EVIDENCE_UNAVAILABLE
                    ),
                    "normalized_home": item.get("home_team"),
                    "normalized_away": item.get("away_team"),
                    "normalized_competition": item.get("competition"),
                    "kickoff_utc": item.get("kickoff_utc"),
                    "normalized_identity_evidence": EVIDENCE_RETAINED,
                    "shard_id": item.get("shard_id"),
                    "confidence": None,
                    "confidence_evidence": EVIDENCE_NOT_SCORED,
                    "threshold": None,
                    "threshold_evidence": EVIDENCE_NOT_SCORED,
                    "kickoff_delta_seconds": delta,
                    "kickoff_delta_evidence": evidence,
                    "matched": None,
                    "veto": None,
                    "reasons": None,
                    "reasons_evidence": EVIDENCE_NOT_SCORED,
                    "rejection_reason": "not_scored_by_production",
                    "ranking": "nearest_retained_kickoff_not_a_match_score",
                    "same_shard": same_shard,
                },
            )
        )
    ranked.sort(key=lambda item: item[0])
    return [item[1] for item in ranked[:TOP_NEARBY_CANDIDATES]]


def _rejection_reason_for_scored(pair: dict[str, Any], *, fail_closed: str | None) -> str:
    if fail_closed and pair.get("matched"):
        return f"fail_closed:{fail_closed}"
    if pair.get("veto"):
        reasons = pair.get("reasons") if isinstance(pair.get("reasons"), list) else []
        reason = str(reasons[0]) if reasons else "veto"
        return f"veto:{reason}"
    if pair.get("matched"):
        return "eligible_not_chosen"
    reasons = pair.get("reasons") if isinstance(pair.get("reasons"), list) else []
    if reasons:
        return str(reasons[0])
    return "below_threshold"


def _cross_pairs_for(
    members: list[tuple[str, str]],
    venue: str,
    scored_by_node: dict[tuple[str, str], list[dict[str, Any]]],
    generated_by_node: dict[tuple[str, str], list[dict[str, Any]]],
) -> tuple[list[tuple[tuple[str, str], dict[str, Any]]], list[tuple[str, str]]]:
    scored: list[tuple[tuple[str, str], dict[str, Any]]] = []
    seen: set[frozenset[tuple[str, str]]] = set()
    unscored: list[tuple[str, str]] = []
    seen_unscored: set[tuple[str, str]] = set()
    for member in members:
        for pair in scored_by_node.get(member, []):
            other = _other_end(pair, member)
            if other[0] != venue:
                continue
            key = frozenset({member, other})
            if key in seen:
                continue
            seen.add(key)
            scored.append((other, pair))
        for pair in generated_by_node.get(member, []):
            if pair.get("scored"):
                continue
            other = _other_end(pair, member)
            if other[0] != venue or other in seen_unscored:
                continue
            seen_unscored.add(other)
            unscored.append(other)
    scored.sort(key=lambda item: _pair_rank(item[1]))
    return scored, unscored


def _classify_missing_venue(
    *,
    venue: str,
    members: list[tuple[str, str]],
    cluster: dict[str, Any],
    indexed: dict[str, Any],
    meta: dict[str, Any],
    threshold: float | None,
) -> dict[str, Any]:
    node_index: dict[tuple[str, str], dict[str, Any]] = indexed["node_index"]
    cluster_by_member: dict[tuple[str, str], dict[str, Any]] = indexed["cluster_by_member"]
    scored, unscored = _cross_pairs_for(
        members, venue, indexed["scored_by_node"], indexed["generated_by_node"]
    )
    member_set = set(members)
    provenance = cluster.get("provenance") if isinstance(cluster.get("provenance"), dict) else None
    fail_closed = None if provenance is None else provenance.get("fail_closed_reason")
    assigned_elsewhere: list[tuple[tuple[str, str], dict[str, Any]]] = []
    fail_closed_hits: list[tuple[tuple[str, str], dict[str, Any]]] = []
    rejected: list[tuple[tuple[str, str], dict[str, Any]]] = []
    for other, pair in scored:
        if other in member_set:
            continue
        other_cluster = cluster_by_member.get(other)
        other_provenance = (
            other_cluster.get("provenance")
            if isinstance(other_cluster, dict) and isinstance(other_cluster.get("provenance"), dict)
            else None
        )
        other_fail = None if other_provenance is None else other_provenance.get("fail_closed_reason")
        if pair.get("matched") and (fail_closed or other_fail):
            fail_closed_hits.append((other, pair))
        elif pair.get("matched"):
            assigned_elsewhere.append((other, pair))
        else:
            rejected.append((other, pair))
    normalized_elsewhere = [
        item
        for item in indexed["nodes_by_venue"].get(venue, [])
        if (str(item.get("venue")), str(item.get("source_event_id"))) not in member_set
    ]
    scope_rows = indexed["scope_by_venue"].get(venue, [])
    normalization_rows = indexed["normalization_by_venue"].get(venue, [])
    raw_count = _raw_count(meta, venue)
    checks = {
        "scored_candidate_count": len(scored),
        "scored_candidate_evidence": (
            EVIDENCE_RETAINED
            if meta.get("identity_evidence", EVIDENCE_RETAINED) == EVIDENCE_RETAINED
            else EVIDENCE_UNAVAILABLE
        ),
        "generated_unscored_count": len(unscored),
        "normalized_events_on_venue": len(normalized_elsewhere),
        "normalization_rejections_on_venue": len(normalization_rows),
        "scope_rejections_on_venue": len(scope_rows),
        "raw_events_on_venue": raw_count,
        "raw_events_evidence": EVIDENCE_RETAINED if raw_count is not None else EVIDENCE_UNAVAILABLE,
        "scope_rejection_link": "retained_unlinked" if scope_rows else "none",
    }
    top: list[dict[str, Any]] = []
    omitted = 0
    if assigned_elsewhere:
        stage = STAGE_ASSIGNED_ELSEWHERE
        pool = assigned_elsewhere
    elif fail_closed_hits:
        stage = STAGE_FAIL_CLOSED
        pool = fail_closed_hits
    elif rejected:
        stage = STAGE_SCORED_REJECTED
        pool = rejected
    elif unscored:
        stage = STAGE_GENERATED_NOT_SCORED
        pool = []
    elif normalized_elsewhere:
        stage = STAGE_NO_CANDIDATE
        pool = []
    elif raw_count == 0:
        stage = STAGE_DISCOVERY_NOT_RETURNED
        pool = []
    elif normalization_rows and not scope_rows:
        stage = STAGE_NORMALIZATION_REJECTED
        pool = []
    elif scope_rows and not normalization_rows:
        stage = STAGE_SCOPE_REJECTED
        pool = []
    elif scope_rows and normalization_rows:
        stage = STAGE_UNLINKED
        pool = []
    elif raw_count is None:
        stage = STAGE_EVIDENCE_UNAVAILABLE
        pool = []
    else:
        stage = STAGE_EVIDENCE_UNAVAILABLE
        pool = []
    if pool:
        omitted = max(0, len(pool) - TOP_NEARBY_CANDIDATES)
        for other, pair in pool[:TOP_NEARBY_CANDIDATES]:
            reason = _rejection_reason_for_scored(
                pair, fail_closed=str(fail_closed or "") or None
            )
            if stage == STAGE_ASSIGNED_ELSEWHERE:
                reason = "assigned_elsewhere"
            candidate = _candidate_from_scored(
                pair, other, node_index, threshold=threshold, rejection_reason=reason
            )
            origin = node_index.get(members[0]) if members else None
            _fill_kickoff_delta(
                candidate,
                None if origin is None else origin.get("kickoff_utc"),
                candidate.get("kickoff_utc"),
            )
            if origin is not None:
                candidate["same_shard"] = candidate.get("shard_id") == origin.get("shard_id")
            top.append(candidate)
    elif stage in {STAGE_NO_CANDIDATE, STAGE_GENERATED_NOT_SCORED} and members:
        origin = node_index.get(members[0])
        if origin is not None:
            top = _nearby_unscored(
                origin=origin,
                venue=venue,
                nodes_by_venue=indexed["nodes_by_venue"],
                exclude=member_set,
            )
    return {
        "venue": venue,
        "stage": stage,
        "checks": checks,
        "top_nearby_candidates": top,
        "omitted_candidate_count": omitted,
        "scope_rejections_unlinked": stage == STAGE_SCOPE_REJECTED,
        "note": (
            "Scope-rejected and normalization-rejected events are listed separately and "
            "were not scored against this fixture."
            if stage in {STAGE_SCOPE_REJECTED, STAGE_NORMALIZATION_REJECTED, STAGE_UNLINKED}
            else None
        ),
    }


def _member_candidate_summary(
    member: tuple[str, str],
    indexed: dict[str, Any],
) -> dict[str, Any]:
    node = indexed["node_index"].get(member) or {}
    generated = indexed["generated_by_node"].get(member, [])
    scored = indexed["scored_by_node"].get(member, [])
    generation_evidence = node.get("candidate_generation_evidence", EVIDENCE_UNAVAILABLE)
    if generation_evidence == EVIDENCE_UNAVAILABLE and not generated and not scored:
        status = "not_run"
        evidence = EVIDENCE_UNAVAILABLE
    elif generation_evidence == "partial":
        status = "partial"
        evidence = "partial"
    elif generated or scored:
        status = "generated"
        evidence = EVIDENCE_RETAINED
    else:
        status = "none"
        evidence = EVIDENCE_RETAINED
    cross_generated = 0
    same_generated = 0
    for pair in generated:
        other = _other_end(pair, member)
        if other[0] == member[0]:
            same_generated += 1
        else:
            cross_generated += 1
    cross_scored = 0
    for pair in scored:
        other = _other_end(pair, member)
        if other[0] != member[0]:
            cross_scored += 1
    return _stage(
        status,
        evidence,
        generated_count=len(generated),
        scored_count=len(scored),
        unscored_count=max(0, len(generated) - len({
            frozenset({member, _other_end(pair, member)})
            for pair in scored
        })),
        cross_venue_generated_count=cross_generated,
        cross_venue_scored_count=cross_scored,
        same_venue_generated_count=same_generated,
        reason=node.get("candidate_generation_reason"),
    )


def _graph_block(cluster: dict[str, Any], member: tuple[str, str]) -> dict[str, Any]:
    if cluster.get("provenance_evidence") != EVIDENCE_RETAINED:
        return {
            "evidence": EVIDENCE_UNAVAILABLE,
            "reason": "production_did_not_retain_provenance",
            "method": None,
            "component_id": None,
            "decision": None,
            "fail_closed_reason": None,
            "confidence_margin": None,
            "constraints": None,
            "chosen_edges": None,
            "rejected_competing_edges": None,
            "omitted_rejected_edge_count": None,
        }
    provenance = cluster.get("provenance") if isinstance(cluster.get("provenance"), dict) else {}
    chosen = [
        edge
        for edge in provenance.get("chosen_edges") or []
        if isinstance(edge, dict)
        and member in {
            (str(edge.get("left_venue")), str(edge.get("left_source_event_id"))),
            (str(edge.get("right_venue")), str(edge.get("right_source_event_id"))),
        }
    ]
    rejected = [
        edge
        for edge in provenance.get("rejected_competing_edges") or []
        if isinstance(edge, dict)
        and member in {
            (str(edge.get("left_venue")), str(edge.get("left_source_event_id"))),
            (str(edge.get("right_venue")), str(edge.get("right_source_event_id"))),
        }
    ]
    omitted = max(0, len(rejected) - MAX_REPORT_REJECTED_EDGES)
    fail_closed = provenance.get("fail_closed_reason")
    venues = [str(item) for item in cluster.get("venues") or []]
    if fail_closed:
        decision = "fail_closed"
    elif len(venues) >= 2:
        decision = "matched"
    else:
        decision = "unmatched"
    return {
        "evidence": EVIDENCE_RETAINED,
        "reason": None,
        "method": provenance.get("method"),
        "component_id": provenance.get("component_id"),
        "decision": decision,
        "fail_closed_reason": fail_closed,
        "confidence_margin": provenance.get("confidence_margin"),
        "constraints": provenance.get("constraints"),
        "chosen_edges": chosen,
        "rejected_competing_edges": rejected[:MAX_REPORT_REJECTED_EDGES],
        "omitted_rejected_edge_count": omitted,
        "greedy_weight": provenance.get("greedy_weight"),
        "global_weight": provenance.get("global_weight"),
    }


def _top_rejected_for_member(
    member: tuple[str, str],
    cluster: dict[str, Any],
    indexed: dict[str, Any],
    *,
    threshold: float | None,
) -> tuple[list[dict[str, Any]], int]:
    member_set = {
        (str(key[0]), str(key[1]))
        for key in cluster.get("member_keys") or []
        if isinstance(key, (list, tuple)) and len(key) == 2
    }
    provenance = cluster.get("provenance") if isinstance(cluster.get("provenance"), dict) else {}
    fail_closed = None if not provenance else provenance.get("fail_closed_reason")
    pool: list[tuple[tuple[str, str], dict[str, Any]]] = []
    seen: set[frozenset[tuple[str, str]]] = set()
    for pair in indexed["scored_by_node"].get(member, []):
        other = _other_end(pair, member)
        if other[0] == member[0] or other in member_set:
            continue
        key = frozenset({member, other})
        if key in seen:
            continue
        seen.add(key)
        if pair.get("matched") and not fail_closed and len(cluster.get("venues") or []) >= 2:
            continue
        pool.append((other, pair))
    pool.sort(key=lambda item: _pair_rank(item[1]))
    origin = indexed["node_index"].get(member) or {}
    top: list[dict[str, Any]] = []
    for other, pair in pool[:TOP_NEARBY_CANDIDATES]:
        candidate = _candidate_from_scored(
            pair,
            other,
            indexed["node_index"],
            threshold=threshold,
            rejection_reason=_rejection_reason_for_scored(
                pair, fail_closed=str(fail_closed) if fail_closed else None
            ),
        )
        _fill_kickoff_delta(candidate, origin.get("kickoff_utc"), candidate.get("kickoff_utc"))
        candidate["same_shard"] = candidate.get("shard_id") == origin.get("shard_id")
        top.append(candidate)
    if not top and len(cluster.get("venues") or []) < 2:
        gathered: list[dict[str, Any]] = []
        for venue in ("matchbook", "polymarket", "kalshi"):
            if venue == member[0]:
                continue
            gathered.extend(
                _nearby_unscored(
                    origin=origin,
                    venue=venue,
                    nodes_by_venue=indexed["nodes_by_venue"],
                    exclude=member_set,
                )
            )
        gathered.sort(
            key=lambda item: (
                0 if item.get("same_shard") else 1,
                item.get("kickoff_delta_seconds")
                if item.get("kickoff_delta_seconds") is not None
                else float("inf"),
                str(item.get("venue")),
                str(item.get("source_event_id")),
            )
        )
        top = gathered[:TOP_NEARBY_CANDIDATES]
    return top, max(0, len(pool) - TOP_NEARBY_CANDIDATES)


def identity_rule_for_sport(sport: str | None) -> str:
    """Name the identity rule the matcher actually used for this sport."""

    if sport == "tennis":
        return "tennis_player_pair_tour_tournament_round_14d"
    if sport == "baseball":
        return "mlb_curated_clubs_ordinal_kickoff_5m"
    if sport == "american_football":
        return "nfl_curated_clubs_kickoff_5m"
    if sport == "basketball":
        return "basketball_curated_clubs_kickoff_5m"
    if sport == "football":
        return "football_participants_kickoff_5m"
    return "unspecified"


def _identity_fields(node: dict[str, Any] | None) -> dict[str, Any]:
    sport = None if not node else node.get("sport")
    return {
        "sport": sport,
        "identity_rule": identity_rule_for_sport(sport if isinstance(sport, str) else None),
        "competition": None if not node else node.get("competition"),
        "home_team": None if not node else node.get("home_team"),
        "away_team": None if not node else node.get("away_team"),
        "kickoff_utc": None if not node else node.get("kickoff_utc"),
        "tournament": None if not node else node.get("tournament"),
        "round_label": None if not node else node.get("round_label"),
        "event_type": None if not node else node.get("event_type"),
        "scheduled_game_key": None if not node else node.get("scheduled_game_key"),
    }


def _normalized_identity(node: dict[str, Any] | None) -> dict[str, Any]:
    if not node:
        return {
            "evidence": EVIDENCE_UNAVAILABLE,
            "reason": "production_did_not_retain_normalized_identity",
            **_identity_fields(None),
        }
    return {
        "evidence": node.get("normalization_evidence", EVIDENCE_RETAINED),
        "reason": None,
        **_identity_fields(node),
    }


def _fixture_row(
    cluster: dict[str, Any],
    indexed: dict[str, Any],
    meta: dict[str, Any],
    *,
    threshold: float | None,
) -> dict[str, Any]:
    members = [
        (str(key[0]), str(key[1]))
        for key in cluster.get("member_keys") or []
        if isinstance(key, (list, tuple)) and len(key) == 2
    ]
    members.sort()
    venues = [str(item) for item in cluster.get("venues") or []]
    combination = venue_combination(venues)
    matched = len(set(venues)) >= 2
    node_index = indexed["node_index"]
    anchor = node_index.get(members[0]) if members else None
    member_rows = []
    for member in members:
        node = node_index.get(member) or {}
        top, omitted = _top_rejected_for_member(
            member, cluster, indexed, threshold=threshold
        )
        graph = _graph_block(cluster, member)
        member_rows.append(
            {
                "venue": member[0],
                "source_event_id": member[1],
                "provider_label": node.get("provider_label"),
                "provider_label_evidence": node.get(
                    "provider_label_evidence", EVIDENCE_UNAVAILABLE
                ),
                "raw_home": node.get("raw_home"),
                "raw_home_evidence": node.get("raw_home_evidence", EVIDENCE_UNAVAILABLE),
                "raw_away": node.get("raw_away"),
                "raw_away_evidence": node.get("raw_away_evidence", EVIDENCE_UNAVAILABLE),
                "shard_id": node.get("shard_id", cluster.get("shard_id")),
                "stage_trail": {
                    "provider_discovery": _stage(
                        "returned",
                        EVIDENCE_RETAINED,
                        reason=None,
                    ),
                    "competition_scope": _stage(
                        "accepted",
                        EVIDENCE_RETAINED,
                        reason=None,
                    ),
                    "normalization": _stage("accepted", EVIDENCE_RETAINED, reason=None),
                    "normalized_identity": _normalized_identity(node or None),
                    "candidate_pairs": _member_candidate_summary(member, indexed),
                    "scored_candidates": {
                        "evidence": EVIDENCE_RETAINED
                        if indexed["scored_by_node"].get(member)
                        or node.get("candidate_generation_evidence") == EVIDENCE_RETAINED
                        else node.get("candidate_generation_evidence", EVIDENCE_UNAVAILABLE),
                        "top_nearby_candidates": top,
                        "omitted_candidate_count": omitted,
                        "threshold": threshold,
                        "threshold_evidence": (
                            EVIDENCE_RETAINED if threshold is not None else EVIDENCE_UNAVAILABLE
                        ),
                    },
                    "graph_assignment": graph,
                    "final_venue_combination": {
                        "status": combination,
                        "evidence": EVIDENCE_RETAINED,
                        "matched": matched,
                    },
                },
            }
        )
    present = set(venues)
    missing = []
    if meta.get("identity_evidence") == EVIDENCE_RETAINED:
        for venue in _enabled_venues(meta):
            if venue in present:
                continue
            missing.append(
                _classify_missing_venue(
                    venue=venue,
                    members=members,
                    cluster=cluster,
                    indexed=indexed,
                    meta=meta,
                    threshold=threshold,
                )
            )
    else:
        for venue in _enabled_venues(meta):
            if venue in present:
                continue
            missing.append(
                {
                    "venue": venue,
                    "stage": STAGE_EVIDENCE_UNAVAILABLE,
                    "checks": {"identity_evidence": EVIDENCE_UNAVAILABLE},
                    "top_nearby_candidates": [],
                    "omitted_candidate_count": 0,
                    "note": "Identity clustering evidence was not retained for this generation.",
                }
            )
    return {
        "canonical_event_id": cluster.get("canonical_event_id"),
        "sport": None if anchor is None else anchor.get("sport"),
        "competition": None if anchor is None else anchor.get("competition"),
        "kickoff_utc": None if anchor is None else anchor.get("kickoff_utc"),
        "shard_id": cluster.get("shard_id"),
        "matched": matched,
        "venue_combination": combination,
        "event_match_confidence": cluster.get("event_match_confidence"),
        "event_match_threshold": cluster.get("event_match_threshold")
        if cluster.get("event_match_threshold") is not None
        else threshold,
        "event_match_threshold_evidence": (
            EVIDENCE_RETAINED
            if cluster.get("event_match_threshold") is not None or threshold is not None
            else EVIDENCE_UNAVAILABLE
        ),
        "pair_kinds": list(cluster.get("pair_kinds") or []),
        "provenance_evidence": cluster.get("provenance_evidence", EVIDENCE_UNAVAILABLE),
        "members": member_rows,
        "missing_venues": missing,
        "viability_evidence": cluster.get("viability_evidence"),
    }


def _scope_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "venue": item.get("venue"),
        "source_event_id": item.get("source_event_id"),
        "provider_label": item.get("provider_label"),
        "provider_label_evidence": item.get("provider_label_evidence", EVIDENCE_UNAVAILABLE),
        "competition_label": item.get("competition_label"),
        "stage_trail": {
            "provider_discovery": _stage("returned", EVIDENCE_RETAINED, reason=None),
            "competition_scope": _stage(
                "rejected",
                EVIDENCE_RETAINED,
                reason=item.get("scope_reason"),
            ),
            "normalization": _stage(
                "not_run",
                EVIDENCE_NOT_APPLICABLE,
                reason="rejected_before_normalization",
            ),
            "normalized_identity": {
                "evidence": EVIDENCE_UNAVAILABLE,
                "reason": "not_normalized",
                "sport": item.get("sport"),
                "identity_rule": identity_rule_for_sport(
                    item.get("sport") if isinstance(item.get("sport"), str) else None
                ),
                "competition": item.get("competition_label"),
                "home_team": None,
                "away_team": None,
                "kickoff_utc": None,
                "tournament": None,
                "round_label": None,
                "event_type": None,
                "scheduled_game_key": None,
            },
            "candidate_pairs": _stage(
                "not_generated",
                EVIDENCE_NOT_APPLICABLE,
                reason="not_passed_to_matcher",
                generated_count=0,
                scored_count=0,
            ),
            "scored_candidates": {
                "evidence": EVIDENCE_NOT_APPLICABLE,
                "reason": "not_passed_to_matcher",
                "top_nearby_candidates": [],
                "omitted_candidate_count": 0,
                "threshold": None,
                "threshold_evidence": EVIDENCE_NOT_APPLICABLE,
            },
            "graph_assignment": {
                "evidence": EVIDENCE_NOT_APPLICABLE,
                "reason": "not_passed_to_matcher",
                "decision": None,
                "chosen_edges": [],
                "rejected_competing_edges": [],
            },
            "final_venue_combination": {
                "status": "unmatched:scope_rejected",
                "evidence": EVIDENCE_RETAINED,
                "matched": False,
            },
        },
    }


def _normalization_row(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "venue": item.get("venue"),
        "source_event_id": item.get("source_event_id"),
        "provider_label": item.get("provider_label"),
        "provider_label_evidence": item.get("provider_label_evidence", EVIDENCE_UNAVAILABLE),
        "stage_trail": {
            "provider_discovery": _stage("returned", EVIDENCE_RETAINED, reason=None),
            "competition_scope": _stage("accepted", EVIDENCE_RETAINED, reason=None),
            "normalization": _stage(
                "rejected",
                EVIDENCE_RETAINED,
                reason=item.get("reason"),
            ),
            "normalized_identity": {
                "evidence": EVIDENCE_UNAVAILABLE,
                "reason": "normalization_failed",
                **_identity_fields(None),
            },
            "candidate_pairs": _stage(
                "not_generated",
                EVIDENCE_NOT_APPLICABLE,
                reason="not_passed_to_matcher",
                generated_count=0,
                scored_count=0,
            ),
            "scored_candidates": {
                "evidence": EVIDENCE_NOT_APPLICABLE,
                "reason": "not_passed_to_matcher",
                "top_nearby_candidates": [],
                "omitted_candidate_count": 0,
                "threshold": None,
                "threshold_evidence": EVIDENCE_NOT_APPLICABLE,
            },
            "graph_assignment": {
                "evidence": EVIDENCE_NOT_APPLICABLE,
                "reason": "not_passed_to_matcher",
                "decision": None,
                "chosen_edges": [],
                "rejected_competing_edges": [],
            },
            "final_venue_combination": {
                "status": "unmatched:normalization_rejected",
                "evidence": EVIDENCE_RETAINED,
                "matched": False,
            },
        },
    }


def _generation_state(meta: dict[str, Any], *, clustering_truncated: bool) -> str:
    completeness = str(meta.get("completeness") or "")
    partial = bool(meta.get("partial")) or clustering_truncated
    if partial or completeness in {"deadline_leftover", "stale_generation_state"}:
        return "partial"
    if completeness in {"", "complete", "empty_universe"}:
        return "complete"
    return "partial"


def build_universe_matching_report(evidence: dict[str, Any]) -> dict[str, Any]:
    """Deterministic JSON document for an external matching review."""

    meta_in = evidence.get("meta") if isinstance(evidence.get("meta"), dict) else {}
    indexed = _index_evidence(evidence)
    threshold = evidence.get("matcher_threshold")
    if threshold is None:
        threshold = meta_in.get("matcher_threshold")
    threshold_value = None if threshold is None else float(threshold)
    clustering_truncated = bool(
        evidence.get("clustering_truncated") or meta_in.get("clustering_truncated")
    )
    identity_evidence = evidence.get("identity_evidence", EVIDENCE_UNAVAILABLE)
    fixtures = [
        _fixture_row(cluster, indexed, {**meta_in, "identity_evidence": identity_evidence}, threshold=threshold_value)
        for cluster in sorted(
            indexed["clusters"],
            key=lambda item: (
                str(item.get("kickoff_utc") or ""),
                str(item.get("canonical_event_id") or ""),
            ),
        )
    ]
    matched = sum(1 for item in fixtures if item["matched"])
    scope_rows = [
        _scope_row(item)
        for item in evidence.get("scope_rejections") or []
        if isinstance(item, dict)
    ]
    scope_rows.sort(
        key=lambda item: (str(item.get("venue")), str(item.get("source_event_id") or ""), str(item.get("provider_label") or ""))
    )
    normalization_rows = [
        _normalization_row(item)
        for item in evidence.get("normalization_rejections") or []
        if isinstance(item, dict)
    ]
    normalization_rows.sort(
        key=lambda item: (str(item.get("venue")), str(item.get("source_event_id") or ""))
    )
    counts_in = meta_in.get("source_event_counts") if isinstance(meta_in.get("source_event_counts"), dict) else {}
    report = {
        "schema": SCHEMA_VERSION,
        "diagnostic_only": True,
        "scanner_decisions_depend_on_report": False,
        "data_class": "retained_universe_identity_evidence",
        "how_to_read": list(_HOW_TO_READ),
        "meta": {
            "schema": SCHEMA_VERSION,
            "generated_at": meta_in.get("generated_at"),
            "universe_generation_id": meta_in.get("universe_generation_id"),
            "generation_state": _generation_state(meta_in, clustering_truncated=clustering_truncated),
            "completeness": meta_in.get("completeness"),
            "partial": bool(meta_in.get("partial")) or clustering_truncated,
            "clustering_truncated": clustering_truncated,
            "selected_competition_codes": list(meta_in.get("selected_competition_codes") or []),
            "selected_season_scope_codes": list(meta_in.get("selected_season_scope_codes") or []),
            "matcher_threshold": threshold_value,
            "matcher_threshold_evidence": (
                EVIDENCE_RETAINED if threshold_value is not None else EVIDENCE_UNAVAILABLE
            ),
            "matcher_semantic_version": evidence.get("matcher_semantic_version")
            or meta_in.get("matcher_semantic_version"),
            "matcher_semantic_version_evidence": (
                EVIDENCE_RETAINED
                if evidence.get("matcher_semantic_version") or meta_in.get("matcher_semantic_version")
                else EVIDENCE_UNAVAILABLE
            ),
            "assignment_margin": evidence.get("assignment_margin", DEFAULT_ASSIGNMENT_MARGIN),
            "identity_evidence": identity_evidence,
            "identity_evidence_reason": evidence.get("identity_evidence_reason"),
            "enabled_venues": _enabled_venues(meta_in),
            "venue_health": dict(meta_in.get("venue_health") or {}),
            "top_nearby_candidate_limit": TOP_NEARBY_CANDIDATES,
            "scope_rejections_truncated": bool(evidence.get("scope_rejections_truncated")),
            "normalization_rejections_truncated": bool(
                evidence.get("normalization_rejections_truncated")
            ),
        },
        "counts": {
            "raw_by_venue": dict(counts_in.get("raw_by_venue") or {}),
            "normalized_by_venue": dict(counts_in.get("normalized_by_venue") or {}),
            "skipped_out_of_scope": counts_in.get("skipped_out_of_scope"),
            "skipped_by_reason": dict(counts_in.get("skipped_by_reason") or {}),
            "clusters": len(fixtures),
            "matched_clusters": matched,
            "unmatched_clusters": len(fixtures) - matched,
            "scope_rejections": len(scope_rows),
            "normalization_rejections": len(normalization_rows),
        },
        "fixtures": fixtures,
        "scope_rejections": scope_rows,
        "normalization_rejections": normalization_rows,
    }
    return report
