"""Cross-venue arb viability, distinct from fixture lifecycle.

Lifecycle (kickoff, in-play, explicit terminal, 3h unknown window) stays in
``scan_lanes.classify_scan_lane``. This module answers a different question:

    Are there currently at least two viable venues for a PAPER cross-venue arb?

A Matchbook close/drop while Kalshi remains listed is not match completion.
It is ``cross_venue_unavailable`` / ``no_cross_venue_candidate`` — honest
current-state, not ``finished`` from elapsed time.

Process-memory only. PAPER / read-only. No provider writes.

Evidence scope is not widened. A market 404 or a closed market payload
invalidates that native market only. Provider timeout, rate limit, and
auth failure are provider health, not fixture unavailability. Unknown
work stays unknown. Only an authoritative event observation may block or
restore canonical fixture + venue.
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


EVIDENCE_SCOPE_EVENT = "event"
EVIDENCE_SCOPE_MARKET = "market"
EVIDENCE_SCOPE_PROVIDER = "provider"
EVENT_TERMINAL_REASON = "event_terminal"
EVENT_CURRENT_REASON = "event_current"
EVENT_UNAVAILABLE_REASON = "event_unavailable"
MARKET_GONE_REASON = "market_gone"
MARKET_TERMINAL_REASON = "market_terminal"


@dataclass(frozen=True)
class VenueEvidence:
    """Event-scoped venue observation. Market failures do not belong here."""

    state: VenueViability
    scope: str = EVIDENCE_SCOPE_EVENT
    reason: str = ""
    source_event_id: str | None = None


@dataclass(frozen=True)
class MarketEvidence:
    """One native market. A 404 here must not block sibling markets."""

    state: VenueViability
    native_market_id: str
    scope: str = EVIDENCE_SCOPE_MARKET
    reason: str = ""


@dataclass
class OpportunityViabilityCache:
    """Process-memory viability evidence with an explicit scope boundary.

    Event state is keyed by canonical fixture + venue and may be written only
    from event-level evidence. A later authoritative non-terminal event
    observation restores that venue. Market 404/closed state is keyed by
    canonical fixture + venue + native market id. Provider failures are
    recorded separately and never mark a fixture unavailable.

    Operator Clear & update / engine restart discards the cache.
    """

    _venues: dict[str, dict[VenueName, VenueEvidence]] = field(default_factory=dict)
    _markets: dict[tuple[str, VenueName, str], MarketEvidence] = field(default_factory=dict)
    _provider_issues: dict[VenueName, str] = field(default_factory=dict)
    _lock: Lock = field(default_factory=Lock)

    def mark(
        self,
        canonical_event_id: str,
        venue: VenueName,
        state: VenueViability,
        *,
        reason: str | None = None,
        source_event_id: str | None = None,
    ) -> None:
        """Record event-level venue evidence. Does not touch market records."""

        ident = str(canonical_event_id or "").strip()
        if not ident:
            return
        resolved_reason = reason
        if not resolved_reason:
            if state is VenueViability.TERMINAL:
                resolved_reason = EVENT_TERMINAL_REASON
            elif state is VenueViability.UNAVAILABLE:
                resolved_reason = EVENT_UNAVAILABLE_REASON
            else:
                resolved_reason = EVENT_CURRENT_REASON
        source = str(source_event_id or "").strip() or None
        with self._lock:
            row = self._venues.setdefault(ident, {})
            row[venue] = VenueEvidence(
                state=state,
                reason=resolved_reason,
                source_event_id=source,
            )

    def mark_terminal(
        self,
        canonical_event_id: str,
        venue: VenueName,
        *,
        reason: str = EVENT_TERMINAL_REASON,
        source_event_id: str | None = None,
    ) -> None:
        self.mark(
            canonical_event_id,
            venue,
            VenueViability.TERMINAL,
            reason=reason,
            source_event_id=source_event_id,
        )

    def mark_unavailable(
        self,
        canonical_event_id: str,
        venue: VenueName,
        *,
        reason: str = EVENT_UNAVAILABLE_REASON,
        source_event_id: str | None = None,
    ) -> None:
        """Event-level absence only. A market 404 must use ``mark_market_unavailable``."""

        self.mark(
            canonical_event_id,
            venue,
            VenueViability.UNAVAILABLE,
            reason=reason,
            source_event_id=source_event_id,
        )

    def mark_viable(
        self,
        canonical_event_id: str,
        venue: VenueName,
        *,
        reason: str = EVENT_CURRENT_REASON,
        source_event_id: str | None = None,
    ) -> None:
        """Restore event-level viability. Market-level gone rows stay gone."""

        self.mark(
            canonical_event_id,
            venue,
            VenueViability.VIABLE,
            reason=reason,
            source_event_id=source_event_id,
        )

    def mark_market_unavailable(
        self,
        canonical_event_id: str,
        venue: VenueName,
        native_market_id: str,
        *,
        reason: str = MARKET_GONE_REASON,
    ) -> None:
        self._mark_market(
            canonical_event_id,
            venue,
            native_market_id,
            VenueViability.UNAVAILABLE,
            reason=reason,
        )

    def mark_market_terminal(
        self,
        canonical_event_id: str,
        venue: VenueName,
        native_market_id: str,
        *,
        reason: str = MARKET_TERMINAL_REASON,
    ) -> None:
        self._mark_market(
            canonical_event_id,
            venue,
            native_market_id,
            VenueViability.TERMINAL,
            reason=reason,
        )

    def clear_market(
        self,
        canonical_event_id: str,
        venue: VenueName,
        native_market_id: str,
    ) -> None:
        ident = str(canonical_event_id or "").strip()
        market_id = str(native_market_id or "").strip()
        if not ident or not market_id:
            return
        with self._lock:
            self._markets.pop((ident, venue, market_id), None)

    def note_provider_issue(self, venue: VenueName, reason: str) -> None:
        """Provider health only. Does not change fixture or market viability."""

        text = str(reason or "").strip() or "provider_issue"
        with self._lock:
            self._provider_issues[venue] = text

    def provider_issue(self, venue: VenueName) -> str | None:
        with self._lock:
            return self._provider_issues.get(venue)

    def _mark_market(
        self,
        canonical_event_id: str,
        venue: VenueName,
        native_market_id: str,
        state: VenueViability,
        *,
        reason: str,
    ) -> None:
        ident = str(canonical_event_id or "").strip()
        market_id = str(native_market_id or "").strip()
        if not ident or not market_id:
            return
        with self._lock:
            self._markets[(ident, venue, market_id)] = MarketEvidence(
                state=state,
                native_market_id=market_id,
                reason=reason,
            )

    def state(self, canonical_event_id: str, venue: VenueName) -> VenueViability | None:
        evidence = self.event_evidence(canonical_event_id, venue)
        if evidence is None:
            return None
        return evidence.state

    def event_evidence(
        self, canonical_event_id: str, venue: VenueName
    ) -> VenueEvidence | None:
        ident = str(canonical_event_id or "").strip()
        if not ident:
            return None
        with self._lock:
            row = self._venues.get(ident) or {}
            return row.get(venue)

    def market_state(
        self,
        canonical_event_id: str,
        venue: VenueName,
        native_market_id: str,
    ) -> VenueViability | None:
        ident = str(canonical_event_id or "").strip()
        market_id = str(native_market_id or "").strip()
        if not ident or not market_id:
            return None
        with self._lock:
            evidence = self._markets.get((ident, venue, market_id))
            return None if evidence is None else evidence.state

    def is_blocked(self, canonical_event_id: str, venue: VenueName) -> bool:
        current = self.state(canonical_event_id, venue)
        return current in {VenueViability.TERMINAL, VenueViability.UNAVAILABLE}

    def market_is_blocked(
        self,
        canonical_event_id: str,
        venue: VenueName,
        native_market_id: str,
    ) -> bool:
        current = self.market_state(canonical_event_id, venue, native_market_id)
        return current in {VenueViability.TERMINAL, VenueViability.UNAVAILABLE}

    def market_records_for(
        self, canonical_event_id: str
    ) -> tuple[tuple[VenueName, MarketEvidence], ...]:
        ident = str(canonical_event_id or "").strip()
        if not ident:
            return ()
        with self._lock:
            rows = [
                (venue, item)
                for (event_id, venue, _market_id), item in self._markets.items()
                if event_id == ident
            ]
        return tuple(rows)

    def clear(self) -> None:
        with self._lock:
            self._venues.clear()
            self._markets.clear()
            self._provider_issues.clear()


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
    raw = getattr(event, "raw", None)
    source_event_id = str(getattr(event, "source_event_id", "") or "") or None
    terminal = raw_status_is_terminal(raw)
    if venue is VenueName.MATCHBOOK and isinstance(raw, dict):
        observed = matchbook_fixture_state(raw)
        if observed.venue_status in TERMINAL_STATUSES:
            terminal = True
    if venue is None:
        return not terminal
    if terminal:
        store.mark_terminal(
            canonical_event_id,
            venue,
            reason=EVENT_TERMINAL_REASON,
            source_event_id=source_event_id,
        )
        return False
    store.mark_viable(
        canonical_event_id,
        venue,
        reason=EVENT_CURRENT_REASON,
        source_event_id=source_event_id,
    )
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


def identity_market_ids(identity: Any, venue: VenueName) -> tuple[str, ...]:
    """Native market ids that belong to one catalogue relationship."""

    if venue is VenueName.MATCHBOOK:
        market_id = getattr(identity, "matchbook_market_id", None)
        return (str(market_id),) if market_id else ()
    if venue is VenueName.KALSHI:
        tickers = list(getattr(identity, "kalshi_market_tickers", None) or [])
        return tuple(str(item) for item in tickers if str(item).strip())
    if venue is VenueName.POLYMARKET:
        market_id = getattr(identity, "polymarket_market_id", None)
        return (str(market_id),) if market_id else ()
    return ()


def venue_blocked_for_identity(
    cache: OpportunityViabilityCache,
    canonical_event_id: str,
    venue: VenueName,
    identity: Any,
) -> bool:
    """Event block rejects the venue. A market block rejects only that relationship."""

    if cache.is_blocked(canonical_event_id, venue):
        return True
    return any(
        cache.market_is_blocked(canonical_event_id, venue, market_id)
        for market_id in identity_market_ids(identity, venue)
    )


def build_viability_evidence(
    canonical_event_id: str,
    *,
    cache: OpportunityViabilityCache | None = None,
    venues_present: list[str] | None = None,
    final_reason: str | None = None,
    discovered_archetypes: list[str] | None = None,
    attempted_relationships: list[str] | None = None,
    registered_relationships: list[str] | None = None,
    structural_rejections: list[str] | None = None,
) -> dict[str, Any]:
    """Operator diagnostic. Distinguishes event, market, and provider evidence."""

    store = cache if cache is not None else get_opportunity_viability_cache()
    ident = str(canonical_event_id or "").strip()
    event_viability: dict[str, dict[str, Any]] = {}
    for venue in (VenueName.MATCHBOOK, VenueName.KALSHI, VenueName.POLYMARKET):
        evidence = store.event_evidence(ident, venue)
        if evidence is None:
            event_viability[venue.value] = {
                "state": "unknown",
                "evidence_scope": EVIDENCE_SCOPE_EVENT,
                "evidence_reason": None,
                "source_event_id": None,
            }
            continue
        event_viability[venue.value] = {
            "state": evidence.state.value,
            "evidence_scope": evidence.scope,
            "evidence_reason": evidence.reason,
            "source_event_id": evidence.source_event_id,
        }
    gone = []
    for venue, evidence in store.market_records_for(ident):
        gone.append(
            {
                "venue": venue.value,
                "native_market_id": evidence.native_market_id,
                "state": evidence.state.value,
                "evidence_scope": evidence.scope,
                "evidence_reason": evidence.reason,
            }
        )
    gone.sort(key=lambda item: (item["venue"], item["native_market_id"]))
    provider = {
        venue.value: store.provider_issue(venue)
        for venue in (VenueName.MATCHBOOK, VenueName.KALSHI, VenueName.POLYMARKET)
        if store.provider_issue(venue)
    }
    return {
        "canonical_event_id": ident,
        "venues_present": list(venues_present or []),
        "event_viability": event_viability,
        "market_gone": gone,
        "provider_issues": provider,
        "discovered_archetypes": list(discovered_archetypes or []),
        "attempted_relationships": list(attempted_relationships or []),
        "registered_relationships": list(registered_relationships or []),
        "structural_rejections": list(structural_rejections or []),
        "final_reason": final_reason,
    }


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
    viable = tuple(
        venue
        for venue in ready
        if not venue_blocked_for_identity(store, canonical_id, venue, identity)
    )
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
