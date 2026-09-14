"""Pure dual-cadence classification, ordering, and radar-TTL helpers.

Clock is injected. Do not import the dislocation burst scheduler.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import StrEnum
from typing import Any

from sports_hedge.application.quote_freshness import require_aware_instant


class ScanLane(StrEnum):
    HOT = "hot"
    UNIVERSE = "universe"
    DROP = "drop"


FRESHNESS_EXECUTABLE = "executable"
FRESHNESS_RADAR_CURRENT = "radar_current"
FRESHNESS_EXPIRED = "expired"

DEFAULT_HOT_HORIZON = timedelta(minutes=60)
DEFAULT_POST_KICKOFF_UNKNOWN_HORIZON = timedelta(hours=3)
DEFAULT_HOT_TTL_SECONDS = 90
DEFAULT_UNIVERSE_TTL_SECONDS = 360
DEFAULT_HOT_INTERVAL_SECONDS = 30
DEFAULT_UNIVERSE_INTERVAL_SECONDS = 180
DEFAULT_EXECUTABLE_QUOTE_AGE_MS = 1000
UNIVERSE_MIN_CHUNK_SECONDS = 6.0

# Explicit provider statuses only. Elapsed time must never fabricate these.
_DROP_STATUSES = frozenset(
    {
        "completed",
        "complete",
        "settled",
        "void",
        "expired",
        "closed",
        "finished",
        "settled-complete",
        "settled_complete",
    }
)


def classify_scan_lane(
    fixture: Any,
    now: datetime,
    *,
    hot_horizon: timedelta = DEFAULT_HOT_HORIZON,
    post_kickoff_unknown_horizon: timedelta = DEFAULT_POST_KICKOFF_UNKNOWN_HORIZON,
) -> ScanLane:
    """Return HOT, UNIVERSE, or DROP. Never labels live/completed from time."""

    evaluated = require_aware_instant(now, "now")
    status = _normalized_status(getattr(fixture, "fixture_status", None))
    if status in _DROP_STATUSES:
        return ScanLane.DROP

    in_running = getattr(fixture, "in_running", None)
    if in_running is True:
        return ScanLane.HOT

    kickoff = getattr(fixture, "kickoff_utc", None)
    if kickoff is None:
        return ScanLane.UNIVERSE
    kickoff_utc = require_aware_instant(kickoff, "kickoff_utc")
    until_kickoff = kickoff_utc - evaluated
    if timedelta(0) < until_kickoff <= hot_horizon:
        return ScanLane.HOT
    if kickoff_utc <= evaluated:
        since_kickoff = evaluated - kickoff_utc
        if since_kickoff <= post_kickoff_unknown_horizon and in_running is not True:
            return ScanLane.HOT
    return ScanLane.UNIVERSE


def hot_sort_key(fixture: Any) -> tuple:
    """Deterministic HOT priority: truthful in-play, nearest kickoff, opportunity, id."""

    in_running = getattr(fixture, "in_running", None)
    kickoff = getattr(fixture, "kickoff_utc", None)
    kickoff_utc = require_aware_instant(kickoff, "kickoff_utc") if kickoff is not None else datetime.min
    canonical_id = str(getattr(fixture, "canonical_event_id", "") or "")
    return (
        0 if in_running is True else 1,
        kickoff_utc,
        -_opportunity_rank(fixture),
        canonical_id,
    )


def radar_ttl_seconds(
    lane: ScanLane | str,
    *,
    hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
    universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
) -> int:
    resolved = ScanLane(lane) if not isinstance(lane, ScanLane) else lane
    if resolved is ScanLane.HOT:
        return hot_ttl_seconds
    return universe_ttl_seconds


def observation_expires_at(
    last_scanned_at: datetime,
    lane: ScanLane | str,
    *,
    hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
    universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
) -> datetime:
    scanned = require_aware_instant(last_scanned_at, "last_scanned_at")
    ttl = radar_ttl_seconds(
        lane,
        hot_ttl_seconds=hot_ttl_seconds,
        universe_ttl_seconds=universe_ttl_seconds,
    )
    return scanned + timedelta(seconds=ttl)


def next_due_at(
    last_scanned_at: datetime,
    lane: ScanLane | str,
    *,
    hot_interval_seconds: int = DEFAULT_HOT_INTERVAL_SECONDS,
    universe_interval_seconds: int = DEFAULT_UNIVERSE_INTERVAL_SECONDS,
) -> datetime:
    scanned = require_aware_instant(last_scanned_at, "last_scanned_at")
    resolved = ScanLane(lane) if not isinstance(lane, ScanLane) else lane
    interval = (
        hot_interval_seconds if resolved is ScanLane.HOT else universe_interval_seconds
    )
    return scanned + timedelta(seconds=interval)


def freshness_class(
    *,
    lane: ScanLane | str,
    last_scanned_at: datetime | None,
    now: datetime,
    quote_age_ms: int | None,
    max_quote_age_ms: int = DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    hot_ttl_seconds: int = DEFAULT_HOT_TTL_SECONDS,
    universe_ttl_seconds: int = DEFAULT_UNIVERSE_TTL_SECONDS,
) -> str:
    """Return executable / radar_current / expired. Expired rows must be omitted."""

    evaluated = require_aware_instant(now, "now")
    if last_scanned_at is None:
        return FRESHNESS_EXPIRED
    expires = observation_expires_at(
        last_scanned_at,
        lane,
        hot_ttl_seconds=hot_ttl_seconds,
        universe_ttl_seconds=universe_ttl_seconds,
    )
    if evaluated >= expires:
        return FRESHNESS_EXPIRED
    if quote_age_ms is not None and quote_age_ms < max_quote_age_ms:
        return FRESHNESS_EXECUTABLE
    return FRESHNESS_RADAR_CURRENT


def universe_chunk_wall_seconds(
    *,
    now: datetime,
    next_hot_due: datetime,
    remaining_generation_budget: float,
    safety_margin_seconds: float = 2.0,
    min_chunk_seconds: float = UNIVERSE_MIN_CHUNK_SECONDS,
) -> float | None:
    """Return the UNIVERSE chunk wall, or None when the slot cannot fit min_chunk."""

    until_hot = (require_aware_instant(next_hot_due, "next_hot_due") - require_aware_instant(now, "now")).total_seconds()
    until_hot -= safety_margin_seconds
    chunk_wall = min(float(remaining_generation_budget), until_hot)
    if chunk_wall < min_chunk_seconds:
        return None
    return chunk_wall


def _opportunity_rank(fixture: Any) -> int:
    if bool(getattr(fixture, "solver_is_arbitrage", False)):
        return 3
    state = str(getattr(fixture, "opportunity_state", "") or "").casefold()
    if state in {"qualifying", "triggered", "arbitrage"}:
        return 3
    if state in {"near", "near_executable", "approaching"}:
        return 2
    if state in {"matched"}:
        return 1
    return 0


def _normalized_status(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip().casefold()
    return text or None
