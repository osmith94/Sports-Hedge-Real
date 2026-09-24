"""Zero-fill PAPER execution miss policy.

A qualifying scanner decision still attempts PAPER execution immediately.
Full and partial fills stay on the ACTIVE / exposure path. A zero-fill caused
by the executable price moving, or the opportunity disappearing, retains HOT
for a bounded sticky window so the row is not dropped straight back to
BACKGROUND. The window is process memory, matching other HOT promotion state.
The audit record is append-only and survives restart.

Phase 1 remains paper-only. This module does not place venue orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from sports_hedge.arbitrage.watchlist.economics import MOVED_BELOW_MIN_NET_ARB

REASON_RECENTLY_QUALIFYING_EXECUTION_MISS = "recently_qualifying_execution_miss"
HOT_REASON_RECENTLY_QUALIFYING_EXECUTION_MISS = "RECENTLY QUALIFYING EXECUTION MISS"
DEFAULT_EXECUTION_MISS_HOT_STICKY = timedelta(minutes=5)

ZERO_FILL_PRICE_MOVEMENT = "price_movement"
ZERO_FILL_STALE_EXECUTABLE_QUOTE = "stale_executable_quote"
ZERO_FILL_PROVIDER_REJECTION = "provider_rejection"
ZERO_FILL_OTHER = "other"

PRICE_MOVEMENT_REASONS = frozenset(
    {
        MOVED_BELOW_MIN_NET_ARB,
        "incomplete_opening_hedge",
        "no_visible_depth",
        "insufficient_depth",
        "price_unavailable_after_slippage",
    }
)
STALE_EXECUTABLE_QUOTE_REASONS = frozenset(
    {
        "snapshot_stale_at_decision",
        "snapshot_stale_at_simulated_arrival",
        "unknown_quote_age",
        "stale_quote",
        "stale_before_fill",
    }
)
RETRYABLE_ZERO_FILL_REASONS = frozenset(PRICE_MOVEMENT_REASONS)


@dataclass(frozen=True)
class ZeroFillExecutionMiss:
    """One zero-exposure PAPER miss. Not an ACTIVE position."""

    opportunity_id: str
    occurred_at: datetime
    reason: str
    zero_fill_fact: str
    retains_hot: bool
    sticky_until: datetime | None
    canonical_event_id: str | None = None
    pricing_lane: str | None = None


def classify_zero_fill_fact(reason: str) -> str:
    """Separate price movement, stale executable quotes, and provider rejection."""

    if reason in STALE_EXECUTABLE_QUOTE_REASONS:
        return ZERO_FILL_STALE_EXECUTABLE_QUOTE
    if reason in PRICE_MOVEMENT_REASONS:
        return ZERO_FILL_PRICE_MOVEMENT
    lowered = reason.casefold()
    if "provider" in lowered or lowered.startswith("venue_reject"):
        return ZERO_FILL_PROVIDER_REJECTION
    return ZERO_FILL_OTHER


def retains_hot_for_zero_fill(reason: str) -> bool:
    """HOT sticky is only for a price move or a disappeared opportunity."""

    return classify_zero_fill_fact(reason) == ZERO_FILL_PRICE_MOVEMENT


def execution_miss_sticky_until(
    occurred_at: datetime,
    *,
    sticky: timedelta = DEFAULT_EXECUTION_MISS_HOT_STICKY,
) -> datetime:
    if sticky <= timedelta(0):
        raise ValueError("execution-miss HOT sticky window must be positive")
    return occurred_at + sticky


def execution_miss_hot_active(now: datetime, sticky_until: datetime | None) -> bool:
    if sticky_until is None:
        return False
    return now < sticky_until


def format_zero_fill_audit_detail(
    *,
    reason: str,
    zero_fill_fact: str,
    retains_hot: bool,
    sticky_until: datetime | None,
) -> str:
    parts = [
        "fill_result=zero",
        f"zero_fill_fact={zero_fill_fact}",
        f"reason={reason}",
    ]
    if retains_hot:
        parts.append(f"hot_promotion_reason={REASON_RECENTLY_QUALIFYING_EXECUTION_MISS}")
        if sticky_until is not None:
            parts.append(f"hot_sticky_until={sticky_until.isoformat()}")
    return "; ".join(parts)


def zero_fill_exposure_gbp(filled_stake: Decimal) -> Decimal:
    """Zero-fill misses have no persisted exposure. Partial fills must not use this path."""

    if filled_stake > 0:
        raise ValueError("partial fill is exposure and must stay on the ACTIVE path")
    return Decimal("0")
