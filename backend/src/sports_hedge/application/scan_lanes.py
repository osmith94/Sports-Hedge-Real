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
TERMINAL_STATUSES = frozenset(
    {
        "completed",
        "complete",
        "settled",
        "void",
        "voided",
        "expired",
        "closed",
        "graded",
        "finished",
        "final",
        "settled-complete",
        "cancelled",
        "canceled",
        "abandoned",
        "paid",
    }
)
# Official Matchbook GET /events states: open, suspended, closed, graded.
MATCHBOOK_TERMINAL_EVENT_STATES = frozenset({"closed", "graded"})
SCHEDULE_EXCEPTION_STATUSES = frozenset(
    {
        "postponed",
        "delayed",
        "rescheduled",
        "abandoned-postponed",
    }
)
# Later Matchbook-supplied statuses that may restore a terminal tombstone.
MATCHBOOK_TRUSTED_CORRECTION_STATUSES = frozenset(
    {"open", "in-play", "inplay", "suspended", "rescheduled"}
)
MATCHBOOK_LIFECYCLE_SOURCE = "matchbook"
EVICTION_TERMINAL_FROM_MATCHBOOK = "terminal_status_from_matchbook"
EVICTION_TERMINAL = "terminal_status"


def classify_scan_lane(
    fixture: Any,
    now: datetime,
    *,
    hot_horizon: timedelta = DEFAULT_HOT_HORIZON,
    post_kickoff_unknown_horizon: timedelta = DEFAULT_POST_KICKOFF_UNKNOWN_HORIZON,
) -> ScanLane:
    """Return HOT, UNIVERSE, or DROP. Never labels live/completed from time.

    This is the *lifecycle* classifier only: in-play, pre-kickoff horizon,
    bounded post-kickoff unknown, schedule exception, and explicit terminal.
    Qualifying-opportunity promotion into HOT identity is applied separately
    by `FixtureCurrentStateStore` from merged current-state economics
    (Issue #200). Do not fold solver/UI labels into this function.

    Issue #164: explicit terminal provider status leaves current radar immediately.
    Kickoff-passed + unknown may stay HOT only inside the bounded uncertainty
    window; after that it leaves current radar without fabricating a status.
    Postponed/delayed/rescheduled follows explicit provider truth, not kickoff
    arithmetic. The 3h window is never applied to explicit terminal state.
    """

    evaluated = require_aware_instant(now, "now")
    status = fixture_status_value(fixture)
    if status in TERMINAL_STATUSES:
        return ScanLane.DROP
    if status in SCHEDULE_EXCEPTION_STATUSES:
        return ScanLane.UNIVERSE

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
        if since_kickoff <= post_kickoff_unknown_horizon:
            return ScanLane.HOT
        # Unknown beyond the bounded window: leave current radar. Do not
        # rewrite fixture_status or in_running from elapsed time.
        return ScanLane.DROP
    return ScanLane.UNIVERSE


def fixture_status_value(fixture: Any) -> str | None:
    return _normalized_status(getattr(fixture, "fixture_status", None))


def is_explicit_terminal(fixture: Any) -> bool:
    return fixture_status_value(fixture) in TERMINAL_STATUSES


def is_schedule_exception(fixture: Any) -> bool:
    return fixture_status_value(fixture) in SCHEDULE_EXCEPTION_STATUSES


def lifecycle_status_source(fixture: Any) -> str | None:
    """Venue that actually supplied fixture_status, not cluster coverage."""

    raw = getattr(fixture, "fixture_status_source", None)
    if raw is None:
        return None
    text = str(raw).strip().casefold()
    return text or None


def is_matchbook_lifecycle_status(fixture: Any) -> bool:
    """True only when Matchbook supplied the lifecycle status being evaluated.

    `matchbook_matched` or a Matchbook cluster anchor is not enough: a
    Polymarket/Kalshi status on a Matchbook-matched fixture is not
    Matchbook-confirmed.
    """

    return lifecycle_status_source(fixture) == MATCHBOOK_LIFECYCLE_SOURCE


def is_matchbook_confirmed_tombstone(
    *,
    reason: str | None = None,
    source: str | None = None,
) -> bool:
    if reason == EVICTION_TERMINAL_FROM_MATCHBOOK:
        return True
    if source is None:
        return False
    return str(source).strip().casefold() == MATCHBOOK_LIFECYCLE_SOURCE


def terminal_eviction_reason(fixture: Any) -> str:
    if is_matchbook_lifecycle_status(fixture):
        return EVICTION_TERMINAL_FROM_MATCHBOOK
    return EVICTION_TERMINAL


def should_skip_market_work(fixture: Any, now: datetime, **kwargs: Any) -> bool:
    """Skip market-list / book / economics once explicit terminal status is known.

    The 3h unknown window still evicts current radar on ingest, but market work
    stops immediately only for explicit provider terminal truth (#164).
    """

    del now, kwargs
    return is_explicit_terminal(fixture)


def is_trusted_lifecycle_correction(
    fixture: Any,
    *,
    observed_at: datetime,
    tombstone_observed_at: datetime,
    tombstone_reason: str | None = None,
    tombstone_source: str | None = None,
) -> bool:
    """Return True when a later observation may restore a tombstoned fixture.

    Lifecycle authority is provenance-specific. A Matchbook-confirmed
    terminal tombstone is not cleared by a later Polymarket/Kalshi unknown
    or postponed/delayed/rescheduled observation. Only a later Matchbook-supplied
    open / in-play / suspended / rescheduled status (or Matchbook in_running
    True with a non-terminal status) may restore current radar.
    """

    scanned = require_aware_instant(observed_at, "observed_at")
    stamped = require_aware_instant(tombstone_observed_at, "tombstone_observed_at")
    if scanned < stamped:
        return False
    if not is_matchbook_lifecycle_status(fixture):
        return False
    del tombstone_reason, tombstone_source
    status = fixture_status_value(fixture)
    if status in MATCHBOOK_TRUSTED_CORRECTION_STATUSES:
        return True
    return getattr(fixture, "in_running", None) is True and status not in TERMINAL_STATUSES


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
    text = str(value).strip().casefold().replace("_", "-")
    return text or None
