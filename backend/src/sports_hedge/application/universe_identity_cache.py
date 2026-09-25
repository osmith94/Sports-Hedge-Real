"""UNIVERSE fixture-identity caches.

Two scopes share this module:

* ``GenerationIdentityCache`` — process-local, keyed by one UNIVERSE generation.
  Proven-negative single-venue work and clustering resume live here. A fresh
  generation id, generation close, or operator Clear & update discards it.
* ``CrossGenerationIdentityCache`` — compact versioned fingerprints and pairwise
  identity scores that may be reused on the *next* generation. Reuse is allowed
  only when the identity-relevant fingerprint and the semantic matcher / alias /
  competition-registry version are unchanged. Clear & update discards it.
  Generation close does not.

Neither cache is a second canonical identity system. EventMatcher remains the
oracle; clean full recomputation must equal incremental clustering for the same
source snapshot. Records are compact identity/fingerprint metadata only: no raw
venue bodies, credentials, or secrets. The schema is generic over venue string
plus source event id so a new provider does not require redesign.

Data class: live UNIVERSE observations motivate the cache; tests in this wave
use synthetic/fixture events unless a soak notes otherwise. PAPER / read-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
from hashlib import sha256
from threading import Lock
from typing import Any

from sports_hedge.domain.models import VenueName
from sports_hedge.matching.identity_graph import (
    DEFAULT_ASSIGNMENT_MARGIN,
    ScoredIdentityPair,
)
from sports_hedge.matching.learned_rules import SECRET_KEY_FRAGMENTS, squad_category_fingerprint

# Bump when fingerprint fields or reuse rules change. Matcher threshold,
# kickoff window, assignment margin, competition registry, alias catalog, and
# learned-rule versions are hashed separately into the semantic version.
# 2: MLB scheduled-game identity uses ordinals plus kickoff tolerance, not
# exact minute-key equality. Stale negative pair cache must not survive.
IDENTITY_CACHE_SEMANTIC_VERSION = 2


def event_cache_key(item: Any) -> tuple[str, str]:
    venue = item.venue.value if hasattr(item.venue, "value") else str(item.venue)
    return (str(venue), str(item.source_event_id))


def _venue_value(item: Any) -> str:
    venue = getattr(item, "venue", "")
    return venue.value if hasattr(venue, "value") else str(venue)


def event_identity_fingerprint_payload(item: Any) -> str:
    """Stable identity-relevant fields. Never includes raw venue payloads."""

    canonical = item.canonical
    kickoff = getattr(canonical, "kickoff_utc", None)
    kickoff_text = kickoff.isoformat() if kickoff is not None else ""
    competition = str(getattr(canonical, "competition", "") or "")
    home = str(getattr(canonical, "home_team", "") or "")
    away = str(getattr(canonical, "away_team", "") or "")
    sport = str(getattr(canonical, "sport", "") or "")
    from sports_hedge.matching.events import target_competition_code

    competition_code = target_competition_code(competition) or ""
    squad_home = ",".join(sorted(squad_category_fingerprint(home)))
    squad_away = ",".join(sorted(squad_category_fingerprint(away)))
    return "\n".join(
        (
            f"venue={_venue_value(item)}",
            f"source_event_id={item.source_event_id}",
            f"sport={sport}",
            f"competition={competition}",
            f"competition_code={competition_code}",
            f"home_team={home}",
            f"away_team={away}",
            f"kickoff_utc={kickoff_text}",
            f"squad_home={squad_home}",
            f"squad_away={squad_away}",
        )
    )


def event_identity_fingerprint(item: Any) -> str:
    """Compact digest over identity-relevant fields. Not a second canonical id."""

    return sha256(event_identity_fingerprint_payload(item).encode()).hexdigest()


@lru_cache(maxsize=1)
def alias_catalog_digest() -> str:
    """Digest of curated team aliases. Registry edits bump the semantic version."""

    from sports_hedge.facts.team_registry import alias_pairs

    payload = "\n".join(f"{alias}\t{canonical}" for alias, canonical in alias_pairs())
    return sha256(payload.encode()).hexdigest()[:16]


def learned_rules_digest(applicator: Any | None) -> str:
    if applicator is None:
        return "none"
    enabled = getattr(applicator, "enabled_rules", None)
    rules = list(enabled()) if callable(enabled) else []
    if not rules:
        return "none"
    parts = sorted(
        f"{getattr(rule, 'rule_id', '')}:{getattr(rule, 'version', '')}" for rule in rules
    )
    return sha256("\n".join(parts).encode()).hexdigest()[:16]


def identity_cache_semantic_version(matcher: Any | None = None) -> str:
    """Cache contract version: matcher + alias catalog + competition registry."""

    from sports_hedge.application.target_competitions import (
        OPERATOR_COMPETITION_REGISTRY_VERSION,
    )

    threshold = float(getattr(matcher, "threshold", 0.92)) if matcher is not None else 0.92
    kickoff = getattr(matcher, "kickoff_tolerance", None)
    kickoff_seconds = int(kickoff.total_seconds()) if kickoff is not None else 300
    learned = learned_rules_digest(
        getattr(matcher, "learned_applicator", None) if matcher is not None else None
    )
    return "|".join(
        (
            f"identity-cache/{IDENTITY_CACHE_SEMANTIC_VERSION}",
            f"matcher_threshold={threshold:.4f}",
            f"kickoff_seconds={kickoff_seconds}",
            f"assignment_margin={float(DEFAULT_ASSIGNMENT_MARGIN):.4f}",
            f"competition_registry={int(OPERATOR_COMPETITION_REGISTRY_VERSION)}",
            f"alias_catalog={alias_catalog_digest()}",
            f"learned_rules={learned}",
        )
    )


def _pair_store_key(left: tuple[str, str], right: tuple[str, str]) -> frozenset[tuple[str, str]]:
    return frozenset((left, right))


@dataclass(frozen=True)
class CachedEventIdentity:
    """Compact per-source-event identity metadata. No raw secrets or bodies."""

    venue: str
    source_event_id: str
    fingerprint: str

    def cache_key(self) -> tuple[str, str]:
        return (self.venue, self.source_event_id)

    def as_record(self) -> dict[str, str]:
        return {
            "venue": self.venue,
            "source_event_id": self.source_event_id,
            "fingerprint": self.fingerprint,
        }


@dataclass(frozen=True)
class CachedPairScore:
    """Compact EventMatcher evidence for one undirected source-event pair."""

    left_venue: str
    left_source_event_id: str
    right_venue: str
    right_source_event_id: str
    left_fingerprint: str
    right_fingerprint: str
    confidence: float
    reasons: tuple[str, ...]
    matched: bool
    veto: bool

    def store_key(self) -> frozenset[tuple[str, str]]:
        return _pair_store_key(
            (self.left_venue, self.left_source_event_id),
            (self.right_venue, self.right_source_event_id),
        )

    def as_record(self) -> dict[str, object]:
        return {
            "left_venue": self.left_venue,
            "left_source_event_id": self.left_source_event_id,
            "right_venue": self.right_venue,
            "right_source_event_id": self.right_source_event_id,
            "left_fingerprint": self.left_fingerprint,
            "right_fingerprint": self.right_fingerprint,
            "confidence": self.confidence,
            "reasons": list(self.reasons),
            "matched": self.matched,
            "veto": self.veto,
        }

    def to_scored_pair(self) -> ScoredIdentityPair:
        return ScoredIdentityPair.from_endpoints(
            (VenueName(self.left_venue), self.left_source_event_id),
            (VenueName(self.right_venue), self.right_source_event_id),
            confidence=self.confidence,
            reasons=self.reasons,
            matched=self.matched,
            veto=self.veto,
        )

    @classmethod
    def from_scored_pair(
        cls,
        pair: ScoredIdentityPair,
        fingerprints: dict[tuple[str, str], str],
    ) -> CachedPairScore:
        left_key = (pair.left[0].value, pair.left[1])
        right_key = (pair.right[0].value, pair.right[1])
        return cls(
            left_venue=left_key[0],
            left_source_event_id=left_key[1],
            right_venue=right_key[0],
            right_source_event_id=right_key[1],
            left_fingerprint=fingerprints[left_key],
            right_fingerprint=fingerprints[right_key],
            confidence=float(pair.confidence),
            reasons=tuple(pair.reasons),
            matched=bool(pair.matched),
            veto=bool(pair.veto),
        )


@dataclass
class IncrementalIdentityDiagnostics:
    discovered: int = 0
    unchanged_reused: int = 0
    changed_recomputed: int = 0
    new: int = 0
    removed: int = 0
    cache_hit_pct: float = 0.0
    saved_candidate_comparisons: int = 0
    semantic_version: str = ""
    semantic_version_mismatch: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "identity_events_discovered": self.discovered,
            "identity_unchanged_reused": self.unchanged_reused,
            "identity_changed_recomputed": self.changed_recomputed,
            "identity_events_new": self.new,
            "identity_events_removed": self.removed,
            "identity_cache_hit_pct": self.cache_hit_pct,
            "identity_saved_candidate_comparisons": self.saved_candidate_comparisons,
            "identity_cache_semantic_version": self.semantic_version,
            "identity_cache_semantic_version_mismatch": self.semantic_version_mismatch,
        }


@dataclass
class ClusteringResumeState:
    """In-memory clustering checkpoint for one generation's identity pass."""

    items_signature: str
    cursor: int
    parent: dict[tuple[VenueName, str], tuple[VenueName, str]]
    match_confidence: dict[tuple[VenueName, str], float]
    pair_kinds: dict[tuple[VenueName, str], set[str]]
    scored_pairs: list[Any] = field(default_factory=list)
    candidate_keys: list[tuple[tuple[str, str], tuple[str, str]]] = field(
        default_factory=list
    )
    candidate_order_version: int = 2
    index_diagnostics: dict[str, Any] = field(default_factory=dict)


@dataclass
class GenerationIdentityCache:
    """Process-local cache keyed by UNIVERSE generation id."""

    generation_id: int | None = None
    no_cross_venue: dict[tuple[str, str], str] = field(default_factory=dict)
    known_other_keys: dict[tuple[str, str], frozenset[tuple[str, str]]] = field(
        default_factory=dict
    )
    clustering_resume: ClusteringResumeState | None = None
    # Resume/candidate lists partitioned by identity shard. A change in one
    # competition must not drop cursors for the others. ``clustering_resume``
    # stays the single-pass checkpoint used by direct ClusterPass callers.
    shard_resumes: dict[str, ClusteringResumeState] = field(default_factory=dict)
    discovery_signature: str | None = None

    def bind(self, generation_id: int | None) -> None:
        if generation_id is None:
            self.clear()
            return
        if self.generation_id != generation_id:
            self.clear()
            self.generation_id = int(generation_id)

    def clear(self) -> None:
        self.generation_id = None
        self.no_cross_venue.clear()
        self.known_other_keys.clear()
        self.clustering_resume = None
        self.shard_resumes.clear()
        self.discovery_signature = None

    def skip_cross_venue_against(self, item: Any, other: Any) -> bool:
        """True when this pair was already a proven non-candidate this generation."""

        if item.venue is other.venue:
            return False
        key = event_cache_key(item)
        cached_fingerprint = self.no_cross_venue.get(key)
        if cached_fingerprint is None:
            return False
        if cached_fingerprint != event_identity_fingerprint(item):
            return False
        known = self.known_other_keys.get(key)
        if not known:
            return False
        return event_cache_key(other) in known

    def record_single_venue(
        self,
        item: Any,
        *,
        other_venue_keys: frozenset[tuple[str, str]],
    ) -> None:
        key = event_cache_key(item)
        self.no_cross_venue[key] = event_identity_fingerprint(item)
        self.known_other_keys[key] = other_venue_keys

    def items_signature(self, items: list[Any]) -> str:
        """Order-independent snapshot of the current source-event set."""

        return "\n".join(sorted(event_identity_fingerprint(item) for item in items))

    def build_clustering_resume(
        self,
        *,
        items: list[Any],
        cursor: int,
        parent: dict[tuple[VenueName, str], tuple[VenueName, str]],
        match_confidence: dict[tuple[VenueName, str], float],
        pair_kinds: dict[tuple[VenueName, str], set[str]],
        scored_pairs: list[Any] | None = None,
        candidate_keys: list[tuple[tuple[str, str], tuple[str, str]]] | None = None,
        candidate_order_version: int = 2,
        index_diagnostics: dict[str, Any] | None = None,
    ) -> ClusteringResumeState:
        return ClusteringResumeState(
            items_signature=self.items_signature(items),
            cursor=max(0, int(cursor)),
            parent=dict(parent),
            match_confidence=dict(match_confidence),
            pair_kinds={key: set(value) for key, value in pair_kinds.items()},
            scored_pairs=list(scored_pairs or ()),
            candidate_keys=list(candidate_keys or ()),
            candidate_order_version=int(candidate_order_version),
            index_diagnostics=dict(index_diagnostics or {}),
        )

    def store_clustering_resume(
        self,
        *,
        items: list[Any],
        cursor: int,
        parent: dict[tuple[VenueName, str], tuple[VenueName, str]],
        match_confidence: dict[tuple[VenueName, str], float],
        pair_kinds: dict[tuple[VenueName, str], set[str]],
        scored_pairs: list[Any] | None = None,
        candidate_keys: list[tuple[tuple[str, str], tuple[str, str]]] | None = None,
        candidate_order_version: int = 2,
        index_diagnostics: dict[str, Any] | None = None,
    ) -> None:
        self.clustering_resume = self.build_clustering_resume(
            items=items,
            cursor=cursor,
            parent=parent,
            match_confidence=match_confidence,
            pair_kinds=pair_kinds,
            scored_pairs=scored_pairs,
            candidate_keys=candidate_keys,
            candidate_order_version=candidate_order_version,
            index_diagnostics=index_diagnostics,
        )

    def store_shard_resume(self, shard_id: str, **kwargs: Any) -> None:
        """Checkpoint one competition shard without touching any other shard."""

        self.shard_resumes[str(shard_id)] = self.build_clustering_resume(**kwargs)

    def take_clustering_resume(self, items: list[Any]) -> ClusteringResumeState | None:
        snapshot = self.clustering_resume
        if snapshot is None:
            return None
        if snapshot.items_signature != self.items_signature(items):
            self.clustering_resume = None
            return None
        return snapshot


class ShardResumeCache:
    """GenerationIdentityCache view whose resume cursor is one shard.

    Negative-pair memory stays on the parent (it is keyed by source event).
    Candidate lists and cursors do not.
    """

    def __init__(self, parent: GenerationIdentityCache, shard_id: str) -> None:
        self._parent = parent
        self.shard_id = str(shard_id)

    @property
    def generation_id(self) -> int | None:
        return self._parent.generation_id

    @property
    def clustering_resume(self) -> ClusteringResumeState | None:
        return self._parent.shard_resumes.get(self.shard_id)

    @clustering_resume.setter
    def clustering_resume(self, value: ClusteringResumeState | None) -> None:
        if value is None:
            self._parent.shard_resumes.pop(self.shard_id, None)
        else:
            self._parent.shard_resumes[self.shard_id] = value

    def items_signature(self, items: list[Any]) -> str:
        return self._parent.items_signature(items)

    def take_clustering_resume(self, items: list[Any]) -> ClusteringResumeState | None:
        snapshot = self.clustering_resume
        if snapshot is None:
            return None
        if snapshot.items_signature != self.items_signature(items):
            self.clustering_resume = None
            return None
        return snapshot

    def store_clustering_resume(self, **kwargs: Any) -> None:
        self._parent.store_shard_resume(self.shard_id, **kwargs)

    def skip_cross_venue_against(self, item: Any, other: Any) -> bool:
        return self._parent.skip_cross_venue_against(item, other)

    def record_single_venue(
        self,
        item: Any,
        *,
        other_venue_keys: frozenset[tuple[str, str]],
    ) -> None:
        self._parent.record_single_venue(item, other_venue_keys=other_venue_keys)


@dataclass
class CrossGenerationIdentityCache:
    """Versioned fingerprints + pairwise scores reusable across UNIVERSE generations.

    Clean full recomputation remains the correctness oracle. Pairwise EventMatcher
    evidence is reused only when both endpoints' fingerprints and the semantic
    version match. Removed source events are dropped on the next committed
    snapshot. Incomplete/truncated clustering must not commit.
    """

    semantic_version: str | None = None
    events: dict[tuple[str, str], CachedEventIdentity] = field(default_factory=dict)
    pairs: dict[frozenset[tuple[str, str]], CachedPairScore] = field(default_factory=dict)

    def clear(self) -> None:
        self.semantic_version = None
        self.events.clear()
        self.pairs.clear()

    def classify(
        self,
        items: list[Any],
        semantic_version: str,
    ) -> IncrementalIdentityDiagnostics:
        current_keys = {event_cache_key(item) for item in items}
        previous_keys = set(self.events)
        version_mismatch = (
            self.semantic_version is not None and self.semantic_version != semantic_version
        )
        diagnostics = IncrementalIdentityDiagnostics(
            discovered=len(items),
            semantic_version=semantic_version,
            semantic_version_mismatch=version_mismatch,
        )
        if not self.events:
            diagnostics.new = len(items)
            diagnostics.removed = 0
            return diagnostics
        if version_mismatch:
            diagnostics.unchanged_reused = 0
            diagnostics.changed_recomputed = len(current_keys & previous_keys)
            diagnostics.new = len(current_keys - previous_keys)
            diagnostics.removed = len(previous_keys - current_keys)
            return diagnostics
        for item in items:
            key = event_cache_key(item)
            cached = self.events.get(key)
            if cached is None:
                diagnostics.new += 1
            elif cached.fingerprint == event_identity_fingerprint(item):
                diagnostics.unchanged_reused += 1
            else:
                diagnostics.changed_recomputed += 1
        diagnostics.removed = len(previous_keys - current_keys)
        return diagnostics

    def pair_reuse_enabled(self, semantic_version: str) -> bool:
        return bool(self.pairs) and self.semantic_version == semantic_version

    def reuse_pair(
        self,
        left: Any,
        right: Any,
        fingerprints: dict[tuple[str, str], str],
        semantic_version: str,
    ) -> ScoredIdentityPair | None:
        if not self.pair_reuse_enabled(semantic_version):
            return None
        left_key = event_cache_key(left)
        right_key = event_cache_key(right)
        cached = self.pairs.get(_pair_store_key(left_key, right_key))
        if cached is None:
            return None
        expected = {
            (cached.left_venue, cached.left_source_event_id): cached.left_fingerprint,
            (cached.right_venue, cached.right_source_event_id): cached.right_fingerprint,
        }
        if fingerprints.get(left_key) != expected.get(left_key):
            return None
        if fingerprints.get(right_key) != expected.get(right_key):
            return None
        return cached.to_scored_pair()

    def commit_snapshot(
        self,
        items: list[Any],
        scored_pairs: list[ScoredIdentityPair],
        semantic_version: str,
    ) -> None:
        fingerprints = {
            event_cache_key(item): event_identity_fingerprint(item) for item in items
        }
        self.semantic_version = semantic_version
        self.events = {
            key: CachedEventIdentity(
                venue=key[0],
                source_event_id=key[1],
                fingerprint=fingerprint,
            )
            for key, fingerprint in fingerprints.items()
        }
        next_pairs: dict[frozenset[tuple[str, str]], CachedPairScore] = {}
        for pair in scored_pairs:
            left_key = (pair.left[0].value, pair.left[1])
            right_key = (pair.right[0].value, pair.right[1])
            if left_key not in fingerprints or right_key not in fingerprints:
                continue
            cached = CachedPairScore.from_scored_pair(pair, fingerprints)
            next_pairs[cached.store_key()] = cached
        self.pairs = next_pairs

    def compact_payload(self) -> dict[str, Any]:
        """Safe identity/fingerprint metadata only. No raw venue bodies."""

        return {
            "semantic_version": self.semantic_version,
            "events": [record.as_record() for record in sorted(
                self.events.values(),
                key=lambda item: (item.venue, item.source_event_id),
            )],
            "pairs": [record.as_record() for record in sorted(
                self.pairs.values(),
                key=lambda item: (
                    item.left_venue,
                    item.left_source_event_id,
                    item.right_venue,
                    item.right_source_event_id,
                ),
            )],
        }


def compact_payload_contains_secret(payload: Any) -> bool:
    """True when compact metadata accidentally retained a credential-like token."""

    text = str(payload).casefold()
    return any(fragment in text for fragment in SECRET_KEY_FRAGMENTS)


_STORE = GenerationIdentityCache()
_CROSS_GENERATION = CrossGenerationIdentityCache()
_LOCK = Lock()


def get_universe_identity_cache() -> GenerationIdentityCache:
    return _STORE


def get_cross_generation_identity_cache() -> CrossGenerationIdentityCache:
    return _CROSS_GENERATION


def reset_generation_scoped_identity_cache() -> None:
    """Discard intra-generation resume/negatives. Cross-generation fingerprints stay."""

    with _LOCK:
        _STORE.clear()


def reset_universe_identity_cache() -> None:
    """Discard generation-scoped and cross-generation identity work.

    Called from operator Clear & update. Generation close must not use this.
    """

    with _LOCK:
        _STORE.clear()
        _CROSS_GENERATION.clear()


def bind_universe_identity_cache(generation_id: int | None) -> GenerationIdentityCache:
    with _LOCK:
        _STORE.bind(generation_id)
        return _STORE


def cache_payload_summary(
    cache: GenerationIdentityCache,
    incremental: CrossGenerationIdentityCache | None = None,
) -> dict[str, Any]:
    incremental = incremental if incremental is not None else _CROSS_GENERATION
    return {
        "generation_id": cache.generation_id,
        "no_cross_venue_cached": len(cache.no_cross_venue),
        "clustering_resume_cursor": (
            None if cache.clustering_resume is None else cache.clustering_resume.cursor
        ),
        "shard_resume_count": len(cache.shard_resumes),
        "discovery_signature_set": cache.discovery_signature is not None,
        "cross_generation_events": len(incremental.events),
        "cross_generation_pairs": len(incremental.pairs),
        "cross_generation_semantic_version": incremental.semantic_version,
    }
