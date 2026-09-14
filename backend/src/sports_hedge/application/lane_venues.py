"""Lane-specific operator venue participation.

Fast Scan (HOT) and Full Sweep (UNIVERSE) each have an independent enabled
set. This is scan scheduling only: it does not add venue write paths, fork
identity/state stores, or hard-code Polymarket off.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.domain.models import VenueName

OPERATOR_SCAN_VENUES: tuple[VenueName, ...] = (
    VenueName.MATCHBOOK,
    VenueName.POLYMARKET,
    VenueName.KALSHI,
)
MIN_VENUES_FOR_COMPARISON = 2
INSUFFICIENT_VENUES_WARNING = (
    "scanner_configuration: fewer than two venues enabled — "
    "arbitrage comparison is not executable"
)
INSUFFICIENT_VENUES_REASON = "insufficient_enabled_venues"
VENUE_HEALTH_DISABLED = "disabled"
ParticipationSource = Literal["operator", "env_default"]


def coerce_operator_venues(values: Iterable[Any] | None) -> tuple[VenueName, ...]:
    """Keep first-class operator venues only, unique, stable order."""

    allowed = {item.value: item for item in OPERATOR_SCAN_VENUES}
    seen: set[VenueName] = set()
    ordered: list[VenueName] = []
    for raw in values or ():
        if isinstance(raw, VenueName):
            venue = raw
        else:
            text = str(raw or "").strip().casefold()
            venue = allowed.get(text)
            if venue is None:
                continue
        if venue not in allowed.values() or venue in seen:
            continue
        seen.add(venue)
        ordered.append(venue)
    return tuple(ordered)


def default_operator_venues() -> tuple[VenueName, ...]:
    return OPERATOR_SCAN_VENUES


def venues_from_settings(values: Iterable[Any] | None) -> tuple[VenueName, ...]:
    """Env fallback. Empty/invalid lists keep the all-three default."""

    coerced = coerce_operator_venues(values)
    return coerced if coerced else default_operator_venues()


def comparison_allowed(venues: Iterable[VenueName] | None) -> bool:
    return len(tuple(coerce_operator_venues(venues))) >= MIN_VENUES_FOR_COMPARISON


def comparison_warning(venues: Iterable[VenueName] | None) -> str | None:
    if comparison_allowed(venues):
        return None
    return INSUFFICIENT_VENUES_WARNING


def venue_enabled(venues: Iterable[VenueName] | None, venue: VenueName) -> bool:
    return venue in coerce_operator_venues(venues)


class LaneVenueSet(BaseModel):
    enabled: list[VenueName] = Field(default_factory=lambda: list(OPERATOR_SCAN_VENUES))
    pending: list[VenueName] = Field(default_factory=lambda: list(OPERATOR_SCAN_VENUES))
    comparison_ready: bool = True
    warning: str | None = None
    applies_next_cycle: bool = False

    @classmethod
    def from_enabled(
        cls,
        enabled: Iterable[VenueName],
        *,
        pending: Iterable[VenueName] | None = None,
        in_progress: bool = False,
    ) -> LaneVenueSet:
        active = list(coerce_operator_venues(enabled))
        queued = list(coerce_operator_venues(pending if pending is not None else enabled))
        pending_differs = tuple(active) != tuple(queued)
        return cls(
            enabled=active,
            pending=queued,
            comparison_ready=comparison_allowed(queued),
            warning=comparison_warning(queued),
            applies_next_cycle=in_progress and pending_differs,
        )


class LaneVenueParticipation(BaseModel):
    hot: list[VenueName] = Field(default_factory=lambda: list(OPERATOR_SCAN_VENUES))
    universe: list[VenueName] = Field(default_factory=lambda: list(OPERATOR_SCAN_VENUES))
    source: ParticipationSource = "env_default"
    updated_at: datetime | None = None
    hot_warning: str | None = None
    universe_warning: str | None = None

    def venues_for(self, lane: ScanLane | str | None) -> tuple[VenueName, ...]:
        resolved = ScanLane(lane) if isinstance(lane, str) else lane
        if resolved is ScanLane.HOT:
            return tuple(self.hot)
        return tuple(self.universe)

    def with_warnings(self) -> LaneVenueParticipation:
        return self.model_copy(
            update={
                "hot": list(coerce_operator_venues(self.hot)),
                "universe": list(coerce_operator_venues(self.universe)),
                "hot_warning": comparison_warning(self.hot),
                "universe_warning": comparison_warning(self.universe),
            }
        )


def participation_from_lists(
    hot: Iterable[Any] | None,
    universe: Iterable[Any] | None,
    *,
    source: ParticipationSource = "operator",
    updated_at: datetime | None = None,
    allow_empty: bool = True,
) -> LaneVenueParticipation:
    hot_venues = coerce_operator_venues(hot)
    universe_venues = coerce_operator_venues(universe)
    if not allow_empty:
        hot_venues = hot_venues or default_operator_venues()
        universe_venues = universe_venues or default_operator_venues()
    return LaneVenueParticipation(
        hot=list(hot_venues),
        universe=list(universe_venues),
        source=source,
        updated_at=updated_at or datetime.now(UTC),
    ).with_warnings()
