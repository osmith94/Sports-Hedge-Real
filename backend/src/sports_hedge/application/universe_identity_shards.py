"""Competition-scoped UNIVERSE identity shards.

One dense competition must not share a candidate list or resume cursor with
every other selected sport. Shards are keyed by ``(sport, target competition)``.
Exact provider-scope provenance (verified Gamma series id or Kalshi series
ticker) supplies the competition when the canonical label is sparse. Events
whose competition still cannot be proven stay in an explicit unresolved shard.

EventMatcher remains the identity oracle. Secondary name blocking runs only
inside hot or unresolved shards and is a fail-open canopy, not a new threshold.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict, deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any

from sports_hedge.application.fixture_clusters import (
    SECONDARY_BLOCK_ORDER_VERSION,
    FixtureCluster,
    VenueEvent,
    _append_cluster_event,
    cluster_events_from_pass,
    identity_name_block_overlap,
)
from sports_hedge.application.universe_identity_cache import (
    GenerationIdentityCache,
    IncrementalIdentityDiagnostics,
    ShardResumeCache,
    identity_cache_semantic_version,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.events import EventMatcher, target_competition_code

LOGGER = logging.getLogger(__name__)

UNRESOLVED_COMPETITION = "*unresolved*"
HOT_SHARD_EVENT_COUNT = 64
HOT_BUCKET_SIZE = 40
NORMAL_SLICE_PAIRS = 256
HOT_SLICE_PAIRS = 48
CONSIDER_YIELD_EVERY = 8

_GRAPH_KEYS = (
    "identity_graph_components",
    "identity_graph_obvious_components",
    "identity_graph_ambiguous_components",
    "identity_graph_contradictory_components",
    "identity_graph_fail_closed_components",
    "identity_graph_global_assignments",
    "identity_graph_sibling_groups",
)


def _sport_of(item: VenueEvent) -> str:
    return str(getattr(item.canonical, "sport", "") or "") or "football"


def _kickoff_ts(item: VenueEvent) -> float:
    kickoff = item.canonical.kickoff_utc
    return float(kickoff.timestamp())


def provenance_from_raw(payload: dict[str, Any] | None, venue: VenueName) -> tuple[str | None, str]:
    """Exact registry mapping for one raw venue payload. Unknown stays unresolved."""

    from sports_hedge.application.target_competitions import (
        _polymarket_series_target,
        matchbook_competition_label,
        resolve_target_competition,
        resolve_target_competition_from_kalshi_ticker,
    )

    raw = payload if isinstance(payload, dict) else {}
    if venue is VenueName.POLYMARKET:
        target = _polymarket_series_target(raw)
        if target is not None:
            return target.code.value, "polymarket_series"
    elif venue is VenueName.KALSHI:
        ticker = str(raw.get("series_ticker") or raw.get("event_ticker") or raw.get("ticker") or "")
        target = resolve_target_competition_from_kalshi_ticker(ticker)
        if target is None:
            nested = raw.get("series")
            if isinstance(nested, dict):
                target = resolve_target_competition_from_kalshi_ticker(
                    str(nested.get("ticker") or nested.get("series_ticker") or "")
                )
        if target is not None:
            return target.code.value, "kalshi_ticker"
    elif venue is VenueName.MATCHBOOK:
        label = matchbook_competition_label(raw)
        target = resolve_target_competition(label)
        if target is not None:
            return target.code.value, "matchbook_label"
    for key in ("competition", "league", "seriesTitle", "series_title", "title"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            target = resolve_target_competition(value)
            if target is not None:
                return target.code.value, "raw_label"
    return None, "unresolved"


def stamp_provenance_competition(item: VenueEvent, code: str) -> bool:
    """Record an exact registry competition when the canonical label is sparse.

    The display name is the verified mapping for that series id or ticker.
    EventMatcher still scores the pair; this does not widen thresholds.
    """

    from sports_hedge.application.target_competitions import competition_by_code

    target = competition_by_code(code)
    if target is None:
        return False
    current = str(getattr(item.canonical, "competition", "") or "")
    if target_competition_code(current) is not None:
        return False
    item.canonical.competition = target.display_name
    return True


def provenance_for_event(item: VenueEvent) -> tuple[str | None, str]:
    """Competition shard key source for one normalised venue event.

    A resolved canonical label wins, because that is what EventMatcher scores.
    Otherwise an exact verified series id or ticker is kept. Nothing is inferred
    past the registry.
    """

    label_code = target_competition_code(getattr(item.canonical, "competition", None))
    raw_code, raw_source = provenance_from_raw(
        item.raw if isinstance(item.raw, dict) else None,
        item.venue,
    )
    if label_code is not None:
        return label_code, "label"
    if raw_code is not None:
        return raw_code, raw_source
    return None, "unresolved"


def count_raw_events_by_competition(
    payloads: list[dict[str, Any]],
    *,
    venue: VenueName,
) -> dict[str, int]:
    """In-scope raw payloads by registry competition. Not a coverage reduction."""

    counts: dict[str, int] = defaultdict(int)
    for payload in payloads:
        if not isinstance(payload, dict):
            continue
        code, _source = provenance_from_raw(payload, venue)
        counts[code or UNRESOLVED_COMPETITION] += 1
    return dict(counts)


@dataclass
class IdentityShard:
    sport: str
    competition_code: str
    events: list[VenueEvent] = field(default_factory=list)
    unresolved_attached: int = 0
    provenance_events: int = 0

    @property
    def shard_id(self) -> str:
        return f"{self.sport}/{self.competition_code}"

    @property
    def unresolved(self) -> bool:
        return self.competition_code == UNRESOLVED_COMPETITION

    @property
    def venue_count(self) -> int:
        return len({item.venue for item in self.events})

    @property
    def hot(self) -> bool:
        return self.unresolved or len(self.events) >= HOT_SHARD_EVENT_COUNT


@dataclass
class ShardPartition:
    shards: list[IdentityShard]
    provenance_counts: dict[str, int]
    provenance_conflicts: int
    unresolved_attached: int
    unresolved_bridge_ambiguous: int
    unresolved_events_by_venue: dict[str, int]


def partition_identity_shards(
    items: list[VenueEvent],
    *,
    kickoff_tolerance: timedelta,
) -> ShardPartition:
    """Group events before the expensive identity graph.

    An unresolved event is inlined into a proven shard only when name-block and
    kickoff evidence select exactly one shard. Several shards, or none, leave
    it in the unresolved shard rather than guessing a competition.
    """

    grouped: dict[tuple[str, str], IdentityShard] = {}
    unresolved: list[VenueEvent] = []
    provenance_counts: dict[str, int] = defaultdict(int)
    conflicts = 0
    unresolved_by_venue: dict[str, int] = defaultdict(int)
    for item in items:
        label_code = target_competition_code(getattr(item.canonical, "competition", None))
        raw_code, raw_source = provenance_from_raw(
            item.raw if isinstance(item.raw, dict) else None,
            item.venue,
        )
        if label_code is not None and raw_code is not None and label_code != raw_code:
            conflicts += 1
        code, source = provenance_for_event(item)
        provenance_counts[source] += 1
        if code is not None and label_code is None and source != "label":
            stamp_provenance_competition(item, code)
        if code is None:
            unresolved.append(item)
            unresolved_by_venue[item.venue.value] += 1
            continue
        key = (_sport_of(item), code)
        shard = grouped.get(key)
        if shard is None:
            shard = IdentityShard(sport=key[0], competition_code=key[1])
            grouped[key] = shard
        shard.events.append(item)
        if source != "label":
            shard.provenance_events += 1

    tolerance = float(kickoff_tolerance.total_seconds())
    if tolerance <= 0:
        tolerance = 1.0
    snapshots = [(shard, list(shard.events)) for shard in grouped.values()]
    attached = 0
    ambiguous = 0
    still_unresolved: list[VenueEvent] = []
    for item in unresolved:
        sport = _sport_of(item)
        stamp = _kickoff_ts(item)
        hits: list[IdentityShard] = []
        for shard, proven in snapshots:
            if shard.sport != sport:
                continue
            for other in proven:
                if abs(_kickoff_ts(other) - stamp) > tolerance:
                    continue
                if identity_name_block_overlap(item, other):
                    hits.append(shard)
                    break
        if len(hits) == 1:
            hits[0].events.append(item)
            hits[0].unresolved_attached += 1
            attached += 1
        else:
            still_unresolved.append(item)
            if len(hits) > 1:
                ambiguous += 1
    if still_unresolved:
        by_sport: dict[str, list[VenueEvent]] = defaultdict(list)
        for item in still_unresolved:
            by_sport[_sport_of(item)].append(item)
        for sport, events in by_sport.items():
            grouped[(sport, UNRESOLVED_COMPETITION)] = IdentityShard(
                sport=sport,
                competition_code=UNRESOLVED_COMPETITION,
                events=events,
            )
    shards = sorted(grouped.values(), key=lambda shard: shard.shard_id)
    return ShardPartition(
        shards=shards,
        provenance_counts=dict(provenance_counts),
        provenance_conflicts=conflicts,
        unresolved_attached=attached,
        unresolved_bridge_ambiguous=ambiguous,
        unresolved_events_by_venue=dict(unresolved_by_venue),
    )


def _split_venues(
    events: list[VenueEvent],
) -> tuple[list[VenueEvent], list[VenueEvent], list[VenueEvent], list[VenueEvent]]:
    matchbook: list[VenueEvent] = []
    polymarket: list[VenueEvent] = []
    kalshi: list[VenueEvent] = []
    extra: list[VenueEvent] = []
    for item in events:
        if item.venue is VenueName.MATCHBOOK:
            matchbook.append(item)
        elif item.venue is VenueName.POLYMARKET:
            polymarket.append(item)
        elif item.venue is VenueName.KALSHI:
            kalshi.append(item)
        else:
            extra.append(item)
    return matchbook, polymarket, kalshi, extra


def _singleton_clusters(events: list[VenueEvent]) -> list[FixtureCluster]:
    clusters: list[FixtureCluster] = []
    for item in events:
        cluster = FixtureCluster()
        _append_cluster_event(cluster, item)
        clusters.append(cluster)
    return clusters


def _venue_counts(events: list[VenueEvent]) -> dict[str, int]:
    counts = {
        VenueName.MATCHBOOK.value: 0,
        VenueName.POLYMARKET.value: 0,
        VenueName.KALSHI.value: 0,
        "other": 0,
    }
    for item in events:
        key = item.venue.value
        if key not in counts:
            counts["other"] += 1
        else:
            counts[key] += 1
    counts["events"] = len(events)
    return counts


@dataclass
class _ShardRun:
    shard: IdentityShard
    secondary_block: bool
    cluster_pass: Any
    loaded: bool = False
    finalized: bool = False
    complete: bool = False
    index_ms: int = 0
    consider_ms: int = 0
    finalize_ms: int = 0
    force_hot_slice: bool = False
    clusters: list[FixtureCluster] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)

    @property
    def status(self) -> str:
        if self.complete:
            return "complete"
        if not self.loaded:
            return "not_started"
        if self.shard.hot:
            return "hot_partial"
        return "partial"


def _empty_counts() -> dict[str, int]:
    return {"matchbook_polymarket": 0, "matchbook_kalshi": 0, "polymarket_kalshi": 0}


async def _consider_slice(cluster_pass: Any, limit: int, stop: Callable[[], bool]) -> int:
    taken = 0
    for left, right in cluster_pass.pairs():
        if taken >= limit:
            break
        if taken % CONSIDER_YIELD_EVERY == 0:
            await asyncio.sleep(0)
            if stop():
                break
        cluster_pass.consider(left, right)
        taken += 1
    cluster_pass._resume_cursor = cluster_pass.candidate_pairs_considered
    cluster_pass.checkpoint(cluster_pass.candidate_pairs_considered)
    return taken


def _run_finished(cluster_pass: Any) -> bool:
    return cluster_pass.candidate_pairs_considered >= len(cluster_pass._candidates)


async def cluster_events_sharded(
    *,
    matchbook: list[VenueEvent],
    polymarket: list[VenueEvent],
    kalshi: list[VenueEvent],
    matcher: EventMatcher,
    max_event_pairs: int,
    identity_cache: GenerationIdentityCache | None = None,
    incremental_cache: Any | None = None,
    stop: Callable[[], bool] | None = None,
    on_shard_complete: Callable[[str, list[FixtureCluster]], None] | None = None,
    on_partial: Callable[[list[FixtureCluster]], None] | None = None,
    hot_pair_budget: int | None = None,
) -> tuple[list[FixtureCluster], dict[str, int], bool, dict[str, Any], set[tuple[VenueName, str]]]:
    """Cluster by competition shard, round-robin, and publish finished shards.

    Non-hot multi-venue shards run before single-venue shards. Hot and
    unresolved shards take a smaller slice and cannot start until the earlier
    phases have finished or the chunk deadline hits. ``hot_pair_budget`` limits
    how many hot-shard pairs this call may score (tests and soak caps).
    """

    started = time.perf_counter()
    should_stop = stop or (lambda: False)
    items = [*matchbook, *polymarket, *kalshi]
    tolerance = getattr(matcher, "kickoff_tolerance", timedelta(minutes=5))
    partition = partition_identity_shards(items, kickoff_tolerance=tolerance)
    discovery_changed = False
    if identity_cache is not None:
        signature = identity_cache.items_signature(items)
        previous = identity_cache.discovery_signature
        discovery_changed = previous is not None and previous != signature
        identity_cache.discovery_signature = signature

    runs: list[_ShardRun] = []
    for shard in partition.shards:
        if not shard.events:
            continue
        secondary = shard.hot
        mb, pm, k, extra = _split_venues(shard.events)
        cache_view = (
            None if identity_cache is None else ShardResumeCache(identity_cache, shard.shard_id)
        )
        cluster_pass = cluster_events_from_pass(
            matchbook=mb,
            polymarket=pm,
            kalshi=k,
            extra=extra,
            matcher=matcher,
            max_event_pairs=max_event_pairs,
            identity_cache=cache_view,
            incremental_cache=incremental_cache,
            secondary_block=secondary,
        )
        runs.append(_ShardRun(shard=shard, secondary_block=secondary, cluster_pass=cluster_pass))

    def _publish(extra_clusters: list[FixtureCluster] | None = None) -> None:
        if on_partial is None:
            return
        assembled = [cluster for run in runs for cluster in run.clusters]
        if extra_clusters:
            assembled.extend(extra_clusters)
        on_partial(assembled)

    async def _finalize(run: _ShardRun, *, complete: bool) -> None:
        if run.finalized:
            return
        finalize_started = time.perf_counter()
        clusters, counts = await run.cluster_pass.finalize_cooperative()
        run.finalize_ms += max(0, int((time.perf_counter() - finalize_started) * 1000))
        run.clusters = clusters
        run.counts = dict(counts)
        run.finalized = True
        run.complete = complete and _run_finished(run.cluster_pass)
        if run.complete:
            run.cluster_pass.record_generation_negatives(clusters)
            if on_shard_complete is not None:
                on_shard_complete(run.shard.shard_id, clusters)
        _publish()

    async def _ensure_loaded(run: _ShardRun) -> None:
        if run.loaded:
            return
        index_started = time.perf_counter()
        await run.cluster_pass.load_candidates_cooperative()
        run.index_ms += max(0, int((time.perf_counter() - index_started) * 1000))
        run.loaded = True
        run.cluster_pass.checkpoint(run.cluster_pass.candidate_pairs_considered)
        bucket = int(run.cluster_pass.index_diagnostics.get("max_sport_bucket_size") or 0)
        if bucket >= HOT_BUCKET_SIZE:
            run.force_hot_slice = True

    truncated = False
    hot_considered = 0

    async def _run_phase(phase: list[_ShardRun], *, hot_phase: bool) -> None:
        nonlocal truncated, hot_considered
        if hot_phase and hot_pair_budget == 0:
            truncated = truncated or bool(phase)
            return
        pending: deque[_ShardRun] = deque(phase)
        slice_pairs = HOT_SLICE_PAIRS if hot_phase else NORMAL_SLICE_PAIRS
        while pending:
            if should_stop():
                truncated = True
                return
            if hot_phase and hot_pair_budget is not None and hot_considered >= hot_pair_budget:
                truncated = True
                return
            run = pending.popleft()
            try:
                await _ensure_loaded(run)
            except asyncio.CancelledError:
                if run.loaded:
                    run.cluster_pass.checkpoint(run.cluster_pass.candidate_pairs_considered)
                raise
            if should_stop():
                truncated = True
                pending.appendleft(run)
                return
            if hot_phase and hot_pair_budget is not None and hot_considered >= hot_pair_budget:
                truncated = True
                pending.appendleft(run)
                return
            consider_started = time.perf_counter()
            pair_limit = HOT_SLICE_PAIRS if run.force_hot_slice else slice_pairs
            taken = await _consider_slice(run.cluster_pass, pair_limit, should_stop)
            run.consider_ms += max(0, int((time.perf_counter() - consider_started) * 1000))
            if hot_phase:
                hot_considered += taken
            if _run_finished(run.cluster_pass):
                await _finalize(run, complete=True)
                continue
            pending.append(run)
            if should_stop() or (
                hot_phase and hot_pair_budget is not None and hot_considered >= hot_pair_budget
            ):
                truncated = True
                return

    non_hot_multi = [run for run in runs if not run.shard.hot and run.shard.venue_count >= 2]
    non_hot_single = [run for run in runs if not run.shard.hot and run.shard.venue_count < 2]
    hot_runs = sorted(
        [run for run in runs if run.shard.hot],
        key=lambda run: (
            1 if run.shard.unresolved else 0,
            0 if run.shard.venue_count >= 2 else 1,
            run.shard.shard_id,
        ),
    )
    try:
        await _run_phase(non_hot_multi, hot_phase=False)
        if not truncated and not should_stop():
            await _run_phase(non_hot_single, hot_phase=False)
        else:
            truncated = True
        if not truncated and not should_stop():
            await _run_phase(hot_runs, hot_phase=True)
        elif hot_runs:
            truncated = True
        for run in runs:
            if run.finalized:
                continue
            if run.loaded:
                await _finalize(run, complete=_run_finished(run.cluster_pass))
                if not run.complete:
                    truncated = True
            else:
                run.clusters = _singleton_clusters(run.shard.events)
                run.finalized = True
                run.complete = False
                truncated = True
    except asyncio.CancelledError:
        # A cancel inside the load wrapper can arrive after candidates exist
        # and before ``run.loaded`` is set. Snapshot those shards once so
        # leftover assembly still sees the partial identity. Do not checkpoint
        # a load that has not returned; that would replace a prior resume
        # with an unfinished cursor.
        for run in runs:
            index_ready = run.loaded or bool(run.cluster_pass.index_diagnostics)
            if index_ready and not run.finalized:
                try:
                    if run.loaded:
                        run.cluster_pass.checkpoint(run.cluster_pass.candidate_pairs_considered)
                    await _finalize(run, complete=False)
                except asyncio.CancelledError:
                    if not run.clusters:
                        run.clusters = _singleton_clusters(run.shard.events)
            elif not run.clusters:
                run.clusters = _singleton_clusters(run.shard.events)
        _publish()
        raise

    clusters = [cluster for run in runs for cluster in run.clusters]
    counts = _empty_counts()
    for run in runs:
        for key, value in run.counts.items():
            counts[key] = counts.get(key, 0) + int(value)
    unscored: set[tuple[VenueName, str]] = set()
    for run in runs:
        if run.complete:
            continue
        if run.loaded:
            unscored.update(run.cluster_pass.last_unscored_nodes)
            if not run.cluster_pass.last_unscored_nodes and not _run_finished(run.cluster_pass):
                unscored.update(run.cluster_pass.nodes)
        else:
            unscored.update((item.venue, item.source_event_id) for item in run.shard.events)
    if not truncated and items and incremental_cache is not None:
        scored: list[Any] = []
        semantic = identity_cache_semantic_version(matcher)
        for run in runs:
            scored.extend(run.cluster_pass.scored_pairs)
            semantic = run.cluster_pass.semantic_version
        incremental_cache.commit_snapshot(items, scored, semantic)
    diagnostics = _diagnostics(
        runs=runs,
        items=items,
        partition=partition,
        truncated=truncated,
        duration_ms=max(0, int((time.perf_counter() - started) * 1000)),
        discovery_changed=discovery_changed,
        incremental_cache=incremental_cache,
        matcher=matcher,
        unscored_count=len(unscored),
    )
    blocking = diagnostics.get("blocking_shard_key")
    LOGGER.info(
        "identity_shards total=%s complete=%s hot=%s blocking=%s truncated=%s",
        diagnostics.get("identity_shard_count"),
        diagnostics.get("identity_shards_completed"),
        diagnostics.get("identity_hot_shards"),
        blocking,
        truncated,
    )
    _publish()
    return clusters, counts, truncated, diagnostics, unscored


def _diagnostics(
    *,
    runs: list[_ShardRun],
    items: list[VenueEvent],
    partition: ShardPartition,
    truncated: bool,
    duration_ms: int,
    discovery_changed: bool,
    incremental_cache: Any | None,
    matcher: EventMatcher,
    unscored_count: int,
) -> dict[str, Any]:
    naive = generated = considered = pruned = skipped = rejected = 0
    secondary_blocked = 0
    max_bucket = 0
    bucket_count = 0
    index_ms = consider_ms = finalize_ms = 0
    resume_applied = False
    resume_mismatch = False
    resume_reused = False
    graph = {key: 0 for key in _GRAPH_KEYS}
    saved = 0
    rows: list[dict[str, Any]] = []
    events_by_shard: dict[str, dict[str, int]] = {}
    for run in runs:
        diag = run.cluster_pass.clustering_diagnostics(
            truncated=not run.complete,
            duration_ms=run.index_ms + run.consider_ms + run.finalize_ms,
        )
        naive += int(diag.get("naive_pair_space") or 0)
        generated += int(diag.get("candidate_pairs_generated") or 0)
        considered += int(diag.get("candidate_pairs_considered") or 0)
        pruned += int(diag.get("pairs_pruned_by_index") or 0)
        skipped += int(diag.get("pairs_skipped_by_generation_cache") or 0)
        rejected += int(diag.get("pairs_rejected_by_could_match") or 0)
        secondary_blocked += int(diag.get("pairs_blocked_before_could_match") or 0)
        max_bucket = max(max_bucket, int(diag.get("max_sport_bucket_size") or 0))
        bucket_count += int(diag.get("sport_bucket_count") or 0)
        index_ms += run.index_ms
        consider_ms += run.consider_ms
        finalize_ms += run.finalize_ms
        resume_applied = resume_applied or bool(diag.get("clustering_resume_applied"))
        resume_mismatch = resume_mismatch or bool(diag.get("clustering_resume_signature_mismatch"))
        resume_reused = resume_reused or bool(diag.get("clustering_resume_candidates_reused"))
        saved += int(diag.get("identity_saved_candidate_comparisons") or 0)
        for key in _GRAPH_KEYS:
            graph[key] += int(diag.get(key) or 0)
        remaining = 0
        if run.loaded:
            remaining = max(
                0, len(run.cluster_pass._candidates) - run.cluster_pass.candidate_pairs_considered
            )
        else:
            remaining = len(run.shard.events)
        venue_counts = _venue_counts(run.shard.events)
        events_by_shard[run.shard.shard_id] = venue_counts
        rows.append(
            {
                "shard_id": run.shard.shard_id,
                "sport": run.shard.sport,
                "competition_code": run.shard.competition_code,
                "events": len(run.shard.events),
                "venues": run.shard.venue_count,
                "hot": run.shard.hot,
                "unresolved": run.shard.unresolved,
                "secondary_block": run.secondary_block,
                "status": run.status,
                "candidates": int(diag.get("candidate_pairs_generated") or 0),
                "considered": int(diag.get("candidate_pairs_considered") or 0),
                "remaining_candidates": remaining,
                "max_sport_bucket_size": int(diag.get("max_sport_bucket_size") or 0),
                "max_could_match_component": int(diag.get("max_could_match_component") or 0),
                "index_ms": run.index_ms,
                "consider_ms": run.consider_ms,
                "finalize_ms": run.finalize_ms,
                "resume_reused": bool(diag.get("clustering_resume_candidates_reused")),
                "resume_invalidated": bool(diag.get("clustering_resume_signature_mismatch")),
                "resume_cursor_before": int(diag.get("clustering_resume_cursor_before") or 0),
                "resume_cursor_after": int(diag.get("clustering_resume_cursor_after") or 0),
                "provenance_events": run.shard.provenance_events,
                "unresolved_attached": run.shard.unresolved_attached,
                "candidate_order_version": int(
                    diag.get("candidate_order_version")
                    or (SECONDARY_BLOCK_ORDER_VERSION if run.secondary_block else 2)
                ),
            }
        )
    incomplete = [row for row in rows if row["status"] != "complete"]
    blocking = None
    if incomplete:
        blocking = max(
            incomplete,
            key=lambda row: (
                int(row["remaining_candidates"]),
                int(row["events"]),
                str(row["shard_id"]),
            ),
        )
    completed = sum(1 for row in rows if row["status"] == "complete")
    semantic = identity_cache_semantic_version(matcher)
    if incremental_cache is not None:
        incremental = incremental_cache.classify(items, semantic)
    else:
        incremental = IncrementalIdentityDiagnostics(
            discovered=len(items),
            new=len(items),
            semantic_version=semantic,
        )
    incremental.saved_candidate_comparisons = saved
    if generated > 0:
        incremental.cache_hit_pct = round(100.0 * saved / generated, 4)
    from sports_hedge.application.fixture_clusters import candidate_reduction_pct

    ordered_rows = sorted(rows, key=lambda row: (-int(row["events"]), str(row["shard_id"])))
    return {
        "naive_pair_space": naive,
        "candidate_pairs_generated": generated,
        "candidate_pairs_considered": considered,
        "pairs_pruned_by_index": pruned,
        "pairs_skipped_by_generation_cache": skipped,
        "pairs_rejected_by_could_match": rejected,
        "pairs_blocked_before_could_match": secondary_blocked,
        "unresolved_competition_events": sum(partition.unresolved_events_by_venue.values()),
        "unresolved_events_by_venue": dict(partition.unresolved_events_by_venue),
        "max_sport_bucket_size": max_bucket,
        "sport_bucket_count": bucket_count,
        "candidate_reduction_pct": candidate_reduction_pct(naive=naive, candidates=generated),
        "clustering_truncated": truncated,
        "clustering_duration_ms": duration_ms,
        "cluster_index_ms": index_ms,
        "cluster_consider_ms": consider_ms,
        "cluster_finalize_ms": finalize_ms,
        "unscored_identity_nodes": unscored_count,
        "clustering_resume_applied": resume_applied,
        "clustering_resume_signature_mismatch": resume_mismatch,
        "clustering_resume_candidates_reused": resume_reused,
        "clustering_resume_cursor_before": int((blocking or {}).get("resume_cursor_before") or 0),
        "clustering_resume_cursor_after": int((blocking or {}).get("resume_cursor_after") or 0),
        "discovery_snapshot_changed": discovery_changed,
        "global_resume_invalidated_by_discovery": False,
        "shard_resumes_kept_across_discovery_change": bool(discovery_changed and resume_reused),
        "identity_shard_count": len(rows),
        "identity_shards_completed": completed,
        "identity_shards_pending": len(rows) - completed,
        "identity_hot_shards": sum(1 for row in rows if row["hot"]),
        "largest_shard_key": ordered_rows[0]["shard_id"] if ordered_rows else None,
        "largest_shard_events": int(ordered_rows[0]["events"]) if ordered_rows else 0,
        "largest_shard_candidates": int(ordered_rows[0]["candidates"]) if ordered_rows else 0,
        "blocking_shard_key": None if blocking is None else blocking["shard_id"],
        "blocking_shard_status": None if blocking is None else blocking["status"],
        "blocking_shard_events": 0 if blocking is None else int(blocking["events"]),
        "blocking_shard_candidates": 0 if blocking is None else int(blocking["candidates"]),
        "blocking_shard_considered": 0 if blocking is None else int(blocking["considered"]),
        "blocking_shard_remaining_candidates": (
            0 if blocking is None else int(blocking["remaining_candidates"])
        ),
        "provenance_counts": dict(partition.provenance_counts),
        "provenance_conflicts": partition.provenance_conflicts,
        "unresolved_attached": partition.unresolved_attached,
        "unresolved_bridge_ambiguous": partition.unresolved_bridge_ambiguous,
        "events_by_venue_competition": events_by_shard,
        "identity_shards": ordered_rows[:48],
        **graph,
        **incremental.as_dict(),
    }
