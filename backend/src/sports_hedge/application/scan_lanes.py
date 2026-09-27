"""Pure dual-cadence classification, ordering, and radar-TTL helpers.

Clock is injected. Do not import the dislocation burst scheduler.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Any

from sports_hedge.application.quote_freshness import require_aware_instant
from sports_hedge.arbitrage.watchlist.economics import net_proximity_reason_label


class ScanLane(StrEnum):
    HOT = "hot"
    UNIVERSE = "universe"
    DROP = "drop"


FRESHNESS_EXECUTABLE = "executable"
FRESHNESS_RADAR_CURRENT = "radar_current"
FRESHNESS_EXPIRED = "expired"

DEFAULT_HOT_HORIZON = timedelta(minutes=60)
DEFAULT_POST_KICKOFF_UNKNOWN_HORIZON = timedelta(hours=3)
# Hard football current-radar/HOT membership ceiling after effective kickoff.
# Covers extra time, penalties and ordinary stoppage/delay. Scanner membership
# only: elapsed time must not fabricate completed/closed/in_running.
DEFAULT_POST_KICKOFF_CURRENT_RADAR_CEILING = timedelta(hours=4)
DEFAULT_HOT_TTL_SECONDS = 90
DEFAULT_UNIVERSE_TTL_SECONDS = 360
DEFAULT_HOT_INTERVAL_SECONDS = 30
DEFAULT_HOT_SCAN_INTERVAL_SECONDS = 10
# Fixture radar / membership TTL helper. Not BACKGROUND pricing and not the
# post-completion UNIVERSE discovery refresh.
DEFAULT_UNIVERSE_INTERVAL_SECONDS = 180
DEFAULT_BACKGROUND_INTERVAL_SECONDS = 600
DEFAULT_BACKGROUND_SCAN_INTERVAL_SECONDS = 10
DEFAULT_UNIVERSE_DISCOVERY_INTERVAL_SECONDS = 3600
DEFAULT_EXECUTABLE_QUOTE_AGE_MS = 1000
UNIVERSE_MIN_CHUNK_SECONDS = 6.0

# Canonical operator-facing lane names. JSON fields stay hot/background/universe/active_trade.
OPERATOR_ACTIVE_TRADE_LABEL = "ACTIVE TRADE"
OPERATOR_HOT_PRICING_LABEL = "HOT pricing"
OPERATOR_BACKGROUND_PRICING_LABEL = "BACKGROUND pricing"
OPERATOR_UNIVERSE_DISCOVERY_LABEL = "UNIVERSE discovery"

# Process-startup HOT/BACKGROUND pricing barrier (not a runtime scan lock).
STARTUP_UNIVERSE_PENDING = "startup_universe_pending"
STARTUP_PHASE_UNIVERSE_INITIALISING = "UNIVERSE_INITIALISING"
STARTUP_PHASE_RUNNING = "RUNNING"
STARTUP_PRICING_GATED_SLEEP_SECONDS = 2.0
STARTUP_READINESS_COLD_UNREADY = "COLD_UNREADY"
STARTUP_READINESS_WARM_CATALOGUE_READY = "WARM_CATALOGUE_READY"
STARTUP_READINESS_FRESH_GENERATION_READY = "FRESH_GENERATION_READY"

# Operator-facing HOT membership reasons. Read-model only; not scheduler input.
HOT_REASON_IN_PLAY = "IN PLAY"
HOT_REASON_POST_KICKOFF_STATUS_PENDING = "POST-KICKOFF STATUS PENDING"
HOT_REASON_ARB_PROMOTION = "ARB PROMOTION"
HOT_REASON_SURVEILLANCE = "SURVEILLANCE"
HOT_REASON_NET_PROXIMITY_PREFIX = "NET PROXIMITY"
HOT_REASON_RECENTLY_QUALIFYING_EXECUTION_MISS = "RECENTLY QUALIFYING EXECUTION MISS"

WORKER_IDLE = "idle"
# RUNNING means this lane's pricing slice is in progress.
# A live worker between slices is WAITING, not RUNNING.
WORKER_RUNNING = "running"
WORKER_WAITING = "waiting"
WORKER_DEGRADED = "degraded"
WORKER_COMPLETE = "complete"

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
EVICTION_CLOCK_EXPIRED_CURRENT_RADAR = "clock_expired_current_radar"
# Successful post-kickoff evaluation found no ApprovedEquivalent markets.
# Current-radar closure only. Not a fabricated completed/in_running status.
EVICTION_NO_CURRENT_EQUIVALENT_MARKETS_POST_KICKOFF = (
    "no_current_equivalent_markets_post_kickoff"
)
SUCCESSFUL_MARKET_EVALUATION_STATE = "evaluated"
# Incomplete work must not be read as "equivalents no longer exist".
INCOMPLETE_MARKET_EVALUATION_REASONS = frozenset(
    {
        "not_evaluated_scan_deadline",
        "scan_budget_exhausted",
        "list_markets_unavailable",
        "market_fetch_unavailable",
        "hot_relationship_missing",
        "single_venue_no_cross_venue_candidate",
        "cross_venue_unavailable",
        "upper_bound_below_min_net",
        "hot_revalidation_needed",
        "retry_wait",
        "provider_timeout",
        "timeout",
        "degraded",
        "deferred",
        "partial",
        "provider_capacity_exhausted",
        "failed_exact_id_refresh",
    }
)


def classify_scan_lane(
    fixture: Any,
    now: datetime,
    *,
    hot_horizon: timedelta = DEFAULT_HOT_HORIZON,
    post_kickoff_unknown_horizon: timedelta = DEFAULT_POST_KICKOFF_UNKNOWN_HORIZON,
    post_kickoff_current_radar_ceiling: timedelta = DEFAULT_POST_KICKOFF_CURRENT_RADAR_CEILING,
) -> ScanLane:
    """Return HOT, UNIVERSE, or DROP. Never labels live/completed from time.

    This is the *lifecycle* classifier only: in-play, pre-kickoff horizon,
    bounded post-kickoff unknown, hard post-kickoff current-radar ceiling,
    schedule exception, and explicit terminal.
    Qualifying-opportunity promotion into HOT identity is applied separately
    by `FixtureCurrentStateStore` from merged current-state economics
    (Issue #200). Do not fold solver/UI labels into this function.

    Issue #164: explicit terminal provider status leaves current radar immediately.
    Kickoff-passed + unknown may stay HOT only inside the bounded uncertainty
    window; after that it leaves current radar without fabricating a status.
    Postponed/delayed/rescheduled follows explicit provider truth, not kickoff
    arithmetic. The 3h window is never applied to explicit terminal state.

    Issue #275: a stale provider ``in_running=true`` / ``open`` flag cannot keep
    a football fixture on current HOT/radar after the hard 4h post-kickoff
    ceiling. That drop is clock-expired current-radar membership only.
    """

    evaluated = require_aware_instant(now, "now")
    status = fixture_status_value(fixture)
    if status in TERMINAL_STATUSES:
        return ScanLane.DROP
    if status in SCHEDULE_EXCEPTION_STATUSES:
        return ScanLane.UNIVERSE

    kickoff = getattr(fixture, "kickoff_utc", None)
    kickoff_utc = require_aware_instant(kickoff, "kickoff_utc") if kickoff is not None else None
    if kickoff_utc is not None and evaluated - kickoff_utc > post_kickoff_current_radar_ceiling:
        # Hard current-radar ceiling. Do not rewrite fixture_status or in_running.
        return ScanLane.DROP

    in_running = getattr(fixture, "in_running", None)
    if in_running is True:
        return ScanLane.HOT

    if kickoff_utc is None:
        return ScanLane.UNIVERSE
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


def current_radar_eviction_reason(
    fixture: Any,
    now: datetime,
    *,
    hot_horizon: timedelta = DEFAULT_HOT_HORIZON,
    post_kickoff_unknown_horizon: timedelta = DEFAULT_POST_KICKOFF_UNKNOWN_HORIZON,
    post_kickoff_current_radar_ceiling: timedelta = DEFAULT_POST_KICKOFF_CURRENT_RADAR_CEILING,
) -> str | None:
    """Return why the fixture leaves current radar, or None if it remains.

    Elapsed-time expiry is ``clock_expired_current_radar``, never completed.
    Explicit terminal still uses the provider-terminal eviction reason.
    A successful post-kickoff evaluation with zero matched equivalents is
    ``no_current_equivalent_markets_post_kickoff``. Incomplete scans are not.
    """

    classify_kwargs = {
        "hot_horizon": hot_horizon,
        "post_kickoff_unknown_horizon": post_kickoff_unknown_horizon,
        "post_kickoff_current_radar_ceiling": post_kickoff_current_radar_ceiling,
    }
    evaluated = require_aware_instant(now, "now")
    lane = classify_scan_lane(fixture, evaluated, **classify_kwargs)
    if lane is not ScanLane.DROP:
        if authoritative_post_kickoff_zero_equivalents(fixture, evaluated):
            return EVICTION_NO_CURRENT_EQUIVALENT_MARKETS_POST_KICKOFF
        return None
    if is_explicit_terminal(fixture):
        return terminal_eviction_reason(fixture)
    kickoff = getattr(fixture, "kickoff_utc", None)
    if kickoff is None:
        return None
    kickoff_utc = require_aware_instant(kickoff, "kickoff_utc")
    if evaluated - kickoff_utc > post_kickoff_current_radar_ceiling:
        return EVICTION_CLOCK_EXPIRED_CURRENT_RADAR
    if authoritative_post_kickoff_zero_equivalents(fixture, evaluated):
        return EVICTION_NO_CURRENT_EQUIVALENT_MARKETS_POST_KICKOFF
    return None


def explicit_matched_equivalent_count(fixture: Any) -> int | None:
    """Return a stated equivalent count. ``None`` is unknown, never zero."""

    raw = getattr(fixture, "matched_equivalent_count", None)
    if raw is None or isinstance(raw, bool):
        return None
    if isinstance(raw, int):
        return raw
    return None


def market_evaluation_reason_is_incomplete(fixture: Any) -> bool:
    raw = getattr(fixture, "market_evaluation_reason", None)
    if raw is None:
        return False
    text = str(raw).strip().casefold()
    if not text:
        return False
    return text in INCOMPLETE_MARKET_EVALUATION_REASONS


def successful_complete_market_evaluation(fixture: Any) -> bool:
    """True only when this observation finished a current market evaluation.

    ``evaluated`` is the collector/price-engine success state. Timeout,
    scan-budget leftover, degraded venues, retry-wait and other incomplete
    reasons are not success, even if a count field happens to be present.
    """

    state = str(getattr(fixture, "market_evaluation_state", "") or "").strip().casefold()
    if state != SUCCESSFUL_MARKET_EVALUATION_STATE:
        return False
    return not market_evaluation_reason_is_incomplete(fixture)


def kickoff_has_passed(fixture: Any, now: datetime) -> bool:
    kickoff = getattr(fixture, "kickoff_utc", None)
    if kickoff is None:
        return False
    evaluated = require_aware_instant(now, "now")
    kickoff_utc = require_aware_instant(kickoff, "kickoff_utc")
    return kickoff_utc <= evaluated


def operator_post_kickoff_in_play(fixture: Any, now: datetime) -> bool:
    """Operator IN PLAY. Does not read or write provider ``in_running``."""

    if not kickoff_has_passed(fixture, now):
        return False
    if str(getattr(fixture, "market_evaluation_state", "") or "").strip().casefold() != (
        SUCCESSFUL_MARKET_EVALUATION_STATE
    ):
        return False
    count = explicit_matched_equivalent_count(fixture)
    return count is not None and count > 0


def authoritative_post_kickoff_zero_equivalents(fixture: Any, now: datetime) -> bool:
    """True when a finished post-kickoff evaluation confirms zero equivalents.

    Provider ``in_running=True``, explicit terminal status, schedule
    exceptions, pre-kickoff fixtures and incomplete scans are not this signal.
    """

    if getattr(fixture, "in_running", None) is True:
        return False
    if is_explicit_terminal(fixture) or is_schedule_exception(fixture):
        return False
    if not kickoff_has_passed(fixture, now):
        return False
    if not successful_complete_market_evaluation(fixture):
        return False
    return explicit_matched_equivalent_count(fixture) == 0


def kickoff_horizon_reason_label(hot_horizon: timedelta = DEFAULT_HOT_HORIZON) -> str:
    minutes = max(1, int(hot_horizon.total_seconds() // 60))
    return f"KICKOFF < {minutes}M"


def hot_reason_labels(
    fixture: Any,
    now: datetime,
    *,
    membership: ScanLane | str,
    lifecycle: ScanLane | str,
    qualifying_promotion: bool,
    surveillance_promotion: bool = False,
    net_proximity_promotion: bool = False,
    net_proximity_distance_pp: Decimal | None = None,
    execution_miss_promotion: bool = False,
    hot_horizon: timedelta = DEFAULT_HOT_HORIZON,
) -> list[str]:
    """Return truthful current-state HOT reasons. Empty when membership is not HOT.

    Does not re-decide HOT membership. Callers pass the store's lifecycle
    classification and whether current-state economics promoted a UNIVERSE
    fixture. ARB PROMOTION is only labelled when lifecycle would otherwise be
    UNIVERSE and the row still proves a qualifying executable arb.
    NET PROXIMITY is below Min Net Arb but within 0.50pp of the operator
    trigger. SURVEILLANCE remains already-triggered economics that are not
    executable-fresh. Elapsed time never fabricates live or completed status.
    Provider ``in_running`` is unchanged. Post-kickoff IN PLAY is also the
    operator read-model when a successful current evaluation still has
    matched equivalents.
    """

    resolved_membership = ScanLane(membership) if not isinstance(membership, ScanLane) else membership
    if resolved_membership is not ScanLane.HOT:
        return []
    resolved_lifecycle = ScanLane(lifecycle) if not isinstance(lifecycle, ScanLane) else lifecycle
    labels: list[str] = []
    in_running = getattr(fixture, "in_running", None) is True
    if in_running:
        labels.append(HOT_REASON_IN_PLAY)
    else:
        kickoff = getattr(fixture, "kickoff_utc", None)
        if kickoff is not None:
            evaluated = require_aware_instant(now, "now")
            kickoff_utc = require_aware_instant(kickoff, "kickoff_utc")
            until_kickoff = kickoff_utc - evaluated
            if timedelta(0) < until_kickoff <= hot_horizon:
                labels.append(kickoff_horizon_reason_label(hot_horizon))
            elif kickoff_utc <= evaluated and resolved_lifecycle is ScanLane.HOT:
                if operator_post_kickoff_in_play(fixture, evaluated):
                    labels.append(HOT_REASON_IN_PLAY)
                else:
                    labels.append(HOT_REASON_POST_KICKOFF_STATUS_PENDING)
    if resolved_lifecycle is ScanLane.UNIVERSE:
        if qualifying_promotion:
            labels.append(HOT_REASON_ARB_PROMOTION)
        elif net_proximity_promotion and net_proximity_distance_pp is not None:
            labels.append(net_proximity_reason_label(net_proximity_distance_pp))
        elif surveillance_promotion:
            labels.append(HOT_REASON_SURVEILLANCE)
        elif execution_miss_promotion:
            labels.append(HOT_REASON_RECENTLY_QUALIFYING_EXECUTION_MISS)
    return labels


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
    next_hot_due: datetime | None = None,
    remaining_generation_budget: float,
    safety_margin_seconds: float = 2.0,
    min_chunk_seconds: float = UNIVERSE_MIN_CHUNK_SECONDS,
) -> float | None:
    """Return the scheduled UNIVERSE chunk watchdog wall, or None if too small.

    This bounds one scheduled chunk/cycle so a hung provider cannot wedge the
    worker forever. It is not a generation lifetime: UNIVERSE generations stay
    resumable and unbounded (Tenet 19).

    `next_hot_due` is accepted for call-site compatibility and must not shrink
    the chunk. HOT remains independently schedulable and must not time-slice
    UNIVERSE into leftover-until-HOT slots.
    """

    del now, next_hot_due
    chunk_wall = float(remaining_generation_budget) - float(safety_margin_seconds)
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
