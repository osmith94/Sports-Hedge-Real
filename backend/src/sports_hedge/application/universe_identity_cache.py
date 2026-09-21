"""Generation-scoped fixture-identity cache for UNIVERSE clustering.

Negative "no cross-venue candidate" results and resumable clustering progress
are valid only for one UNIVERSE generation. A fresh generation must re-check
because another venue may list the fixture later. Operator Clear & update
(`LiveRefreshCoordinator.reset`) discards the cache.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from threading import Lock
from typing import Any

from sports_hedge.domain.models import VenueName


def event_identity_fingerprint(item: Any) -> str:
    """Cheap metadata fingerprint. Not a second canonical identity."""

    canonical = item.canonical
    kickoff = getattr(canonical, "kickoff_utc", None)
    kickoff_text = kickoff.isoformat() if kickoff is not None else ""
    return "|".join(
        (
            item.venue.value,
            str(item.source_event_id),
            str(getattr(canonical, "sport", "") or ""),
            str(getattr(canonical, "competition", "") or ""),
            str(getattr(canonical, "home_team", "") or ""),
            str(getattr(canonical, "away_team", "") or ""),
            kickoff_text,
        )
    )


def event_cache_key(item: Any) -> tuple[str, str]:
    return (item.venue.value, str(item.source_event_id))


@dataclass
class ClusteringResumeState:
    """In-memory clustering checkpoint for one generation's identity pass."""

    items_signature: str
    cursor: int
    parent: dict[tuple[VenueName, str], tuple[VenueName, str]]
    match_confidence: dict[tuple[VenueName, str], float]
    pair_kinds: dict[tuple[VenueName, str], set[str]]
    scored_pairs: list[Any] = field(default_factory=list)


@dataclass
class GenerationIdentityCache:
    """Process-local cache keyed by UNIVERSE generation id."""

    generation_id: int | None = None
    no_cross_venue: dict[tuple[str, str], str] = field(default_factory=dict)
    known_other_keys: dict[tuple[str, str], frozenset[tuple[str, str]]] = field(
        default_factory=dict
    )
    clustering_resume: ClusteringResumeState | None = None

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
        return "\n".join(event_identity_fingerprint(item) for item in items)

    def store_clustering_resume(
        self,
        *,
        items: list[Any],
        cursor: int,
        parent: dict[tuple[VenueName, str], tuple[VenueName, str]],
        match_confidence: dict[tuple[VenueName, str], float],
        pair_kinds: dict[tuple[VenueName, str], set[str]],
        scored_pairs: list[Any] | None = None,
    ) -> None:
        self.clustering_resume = ClusteringResumeState(
            items_signature=self.items_signature(items),
            cursor=max(0, int(cursor)),
            parent=dict(parent),
            match_confidence=dict(match_confidence),
            pair_kinds={key: set(value) for key, value in pair_kinds.items()},
            scored_pairs=list(scored_pairs or ()),
        )

    def take_clustering_resume(self, items: list[Any]) -> ClusteringResumeState | None:
        snapshot = self.clustering_resume
        if snapshot is None:
            return None
        if snapshot.items_signature != self.items_signature(items):
            self.clustering_resume = None
            return None
        return snapshot


_STORE = GenerationIdentityCache()
_LOCK = Lock()


def get_universe_identity_cache() -> GenerationIdentityCache:
    return _STORE


def reset_universe_identity_cache() -> None:
    """Discard generation-scoped identity work. Called from Clear & update."""

    with _LOCK:
        _STORE.clear()


def bind_universe_identity_cache(generation_id: int | None) -> GenerationIdentityCache:
    with _LOCK:
        _STORE.bind(generation_id)
        return _STORE


def cache_payload_summary(cache: GenerationIdentityCache) -> dict[str, Any]:
    return {
        "generation_id": cache.generation_id,
        "no_cross_venue_cached": len(cache.no_cross_venue),
        "clustering_resume_cursor": (
            None if cache.clustering_resume is None else cache.clustering_resume.cursor
        ),
    }
