"""Cross-venue arb viability, distinct from fixture lifecycle.

Lifecycle (kickoff, in-play, explicit terminal, 3h unknown window) stays in
``scan_lanes.classify_scan_lane``. This module answers a different question:

    Are there currently at least two viable venues for a PAPER cross-venue arb?

A Matchbook close/drop while Kalshi remains listed is not match completion.
It is ``cross_venue_unavailable`` / ``no_cross_venue_candidate`` — honest
current-state, not ``finished`` from elapsed time.

Process-memory only. PAPER / read-only. No provider writes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from threading import Lock
from typing import Any

from sports_hedge.application.fixture_state import matchbook_fixture_state
from sports_hedge.application.scan_lanes import TERMINAL_STATUSES
from sports_hedge.domain.models import VenueName

CROSS_VENUE_UNAVAILABLE = "cross_venue_unavailable"
NO_CROSS_VENUE_CANDIDATE = "no_cross_venue_candidate"
ACTIVE_TRADE_OVERRIDE = "active_trade_override"
UPPER_BOUND_BELOW_MIN_NET = "upper_bound_below_min_net"

VENUE_VIABLE = "viable"
VENUE_TERMINAL = "terminal"
VENUE_UNAVAILABLE = "unavailable"


class VenueViability(StrEnum):
    VIABLE = VENUE_VIABLE
    TERMINAL = VENUE_TERMINAL
    UNAVAILABLE = VENUE_UNAVAILABLE


@dataclass(frozen=True)
class ViabilityAssessment:
    """Arb-viability snapshot. Does not rewrite fixture_status or in_running."""

    viable_venues: tuple[VenueName, ...]
    reason: str | None
    skip_expensive_work: bool
    active_trade_override: bool = False

    @property
    def viable_venue_count(self) -> int:
        return len(self.viable_venues)


@dataclass
class OpportunityViabilityCache:
    """Remember explicit venue terminal/unavailable observations.

    Keyed by canonical fixture id. A later non-terminal observation restores
    that venue. Operator Clear & update / engine restart discards the cache.
    """

    _venues: dict[str, dict[VenueName, VenueViability]] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def mark(
        self,
        canonical_event_id: str,
        venue: VenueName,
        state: VenueViability,
    ) -> None:
        ident = str(canonical_event_id or "").strip()
        if not ident:
            return
        with self._lock:
            row = self._venues.setdefault(ident, {})
            row[venue] = state

    def mark_terminal(self, canonical_event_id: str, venue: VenueName) -> None:
        self.mark(canonical_event_id, venue, VenueViability.TERMINAL)

    def mark_unavailable(self, canonical_event_id: str, venue: VenueName) -> None:
        self.mark(canonical_event_id, venue, VenueViability.UNAVAILABLE)

    def mark_viable(self, canonical_event_id: str, venue: VenueName) -> None:
        self.mark(canonical_event_id, venue, VenueViability.VIABLE)

    def state(self, canonical_event_id: str, venue: VenueName) -> VenueViability | None:
        ident = str(canonical_event_id or "").strip()
        if not ident:
            return None
        with self._lock:
            row = self._venues.get(ident) or {}
            return row.get(venue)

    def is_blocked(self, canonical_event_id: str, venue: VenueName) -> bool:
        current = self.state(canonical_event_id, venue)
        return current in {VenueViability.TERMINAL, VenueViability.UNAVAILABLE}

    def clear(self) -> None:
        with self._lock:
            self._venues.clear()


_CACHE = OpportunityViabilityCache()
_CACHE_LOCK = Lock()


def get_opportunity_viability_cache() -> OpportunityViabilityCache:
    return _CACHE


def reset_opportunity_viability_cache() -> None:
    with _CACHE_LOCK:
        _CACHE.clear()


def raw_status_is_terminal(raw: Any) -> bool:
    if not isinstance(raw, dict):
        return False
    status = str(raw.get("status") or raw.get("state") or "").strip().casefold()
    return status in TERMINAL_STATUSES


def venue_event_is_currently_viable(
    event: Any,
    *,
    canonical_event_id: str,
    cache: OpportunityViabilityCache | None = None,
) -> bool:
    """True when this listing can still participate in a cross-venue arb."""

    if event is None:
        return False
    venue = getattr(event, "venue", None)
    store = cache if cache is not None else get_opportunity_viability_cache()
    if venue is not None and store.is_blocked(canonical_event_id, venue):
        return False
    raw = getattr(event, "raw", None)
    if venue is VenueName.MATCHBOOK and isinstance(raw, dict):
        state = matchbook_fixture_state(raw)
        if state.venue_status in TERMINAL_STATUSES:
            return False
    if raw_status_is_terminal(raw):
        return False
    return True


def currently_viable_venues(
    cluster: Any,
    *,
    canonical_event_id: str,
    cache: OpportunityViabilityCache | None = None,
    enabled_venues: frozenset[VenueName] | None = None,
) -> tuple[VenueName, ...]:
    present: list[VenueName] = []
    for venue in (VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI):
        if enabled_venues is not None and venue not in enabled_venues:
            continue
        events = cluster.events_for(venue) if hasattr(cluster, "events_for") else []
        if not events:
            continue
        if any(
            venue_event_is_currently_viable(
                item, canonical_event_id=canonical_event_id, cache=cache
            )
            for item in events
        ):
            present.append(venue)
    return tuple(present)


def catalogue_ready_venues(identity: Any) -> tuple[VenueName, ...]:
    ready: list[VenueName] = []
    if getattr(identity, "matchbook_event_id", None) and getattr(
        identity, "matchbook_market_id", None
    ):
        ready.append(VenueName.MATCHBOOK)
    if getattr(identity, "kalshi_event_ticker", None) and (
        list(getattr(identity, "kalshi_market_tickers", None) or [])
        or list(getattr(identity, "kalshi_outcome_ids", None) or [])
    ):
        ready.append(VenueName.KALSHI)
    tokens = list(getattr(identity, "polymarket_token_ids", None) or [])
    if (
        getattr(identity, "polymarket_event_id", None)
        and getattr(identity, "polymarket_market_id", None)
        and tokens
    ):
        ready.append(VenueName.POLYMARKET)
    return tuple(ready)


def assess_identity_viability(
    identity: Any,
    *,
    cache: OpportunityViabilityCache | None = None,
    active_event_ids: frozenset[str] | None = None,
    active_trade_lane: bool = False,
) -> ViabilityAssessment:
    canonical_id = str(getattr(identity, "canonical_event_id", "") or "")
    override = bool(active_trade_lane) or canonical_id in (active_event_ids or frozenset())
    store = cache if cache is not None else get_opportunity_viability_cache()
    ready = catalogue_ready_venues(identity)
    viable = tuple(venue for venue in ready if not store.is_blocked(canonical_id, venue))
    if override:
        return ViabilityAssessment(
            viable_venues=viable,
            reason=ACTIVE_TRADE_OVERRIDE,
            skip_expensive_work=False,
            active_trade_override=True,
        )
    if len(viable) >= 2:
        return ViabilityAssessment(
            viable_venues=viable, reason=None, skip_expensive_work=False
        )
    if len(ready) < 2:
        reason = NO_CROSS_VENUE_CANDIDATE
    else:
        reason = CROSS_VENUE_UNAVAILABLE
    return ViabilityAssessment(
        viable_venues=viable, reason=reason, skip_expensive_work=True
    )


def assess_cluster_viability(
    cluster: Any,
    *,
    canonical_event_id: str,
    cache: OpportunityViabilityCache | None = None,
    enabled_venues: frozenset[VenueName] | None = None,
    active_event_ids: frozenset[str] | None = None,
    allow_one_sided: bool = False,
) -> ViabilityAssessment:
    override = str(canonical_event_id) in (active_event_ids or frozenset())
    if allow_one_sided:
        venues = currently_viable_venues(
            cluster,
            canonical_event_id=canonical_event_id,
            cache=cache,
            enabled_venues=enabled_venues,
        )
        return ViabilityAssessment(
            viable_venues=venues,
            reason=None,
            skip_expensive_work=False,
            active_trade_override=override,
        )
    venues = currently_viable_venues(
        cluster,
        canonical_event_id=canonical_event_id,
        cache=cache,
        enabled_venues=enabled_venues,
    )
    if override:
        return ViabilityAssessment(
            viable_venues=venues,
            reason=ACTIVE_TRADE_OVERRIDE,
            skip_expensive_work=False,
            active_trade_override=True,
        )
    listed = int(getattr(cluster, "venue_count", 0) or 0)
    if len(venues) >= 2:
        return ViabilityAssessment(
            viable_venues=venues, reason=None, skip_expensive_work=False
        )
    reason = (
        NO_CROSS_VENUE_CANDIDATE if listed < 2 else CROSS_VENUE_UNAVAILABLE
    )
    return ViabilityAssessment(
        viable_venues=venues, reason=reason, skip_expensive_work=True
    )
