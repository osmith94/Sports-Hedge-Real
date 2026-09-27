"""Reporting-GBP placement room for discretionary PAPER execution.

Operator authority is three hard caps, all in reporting GBP:

- Max Event: aggregate capital currently locked on one canonical event.
  Any non-closed trade with a positive lock counts. A missing lock
  contributes zero. Closed or released capital contributes zero.
- Max Opportunity: capital currently locked on this opportunity's single
  paper trade (opening tranche plus top-ups).
- Max One-Time: capital one accepted Price-2 execution cycle may add.

Closed and released trades are omitted. Remaining room is never negative.

Migration
---------
Existing ``max_allocated_per_trade_gbp`` is not a runtime sizing input.
When a stored settings row has no placement-threshold columns, each of the
three caps is initialised to that legacy value so an upgrade cannot raise
the permitted deployment. Fresh installs with no row inherit the same
legacy default (``max_allocated_per_trade_gbp``, code default £1,000)
unless an explicit threshold is configured.

Recovery
--------
Risk-reducing recovery is not a discretionary accumulation cycle.
``recovery_capital_room_gbp`` is remaining Max Opportunity room only — the
same remaining-trade-room bound recovery used before these thresholds.
Max Event and Max One-Time do not shrink that recovery room. A missing
lock contributes zero. A missing canonical event id still fail-closes,
and a new cycle still fail-closes when its own FX or native balance
evidence is missing.
"""

from __future__ import annotations

from decimal import Decimal

from sports_hedge.paper.trades import PaperTrade, PaperTradeState


class PlacementValuationError(Exception):
    """Required reporting-GBP evidence is missing. Placement must fail closed."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _locked_gbp(trade: PaperTrade) -> Decimal:
    """Positive lock on a non-closed trade. Missing, zero, and closed locks are zero."""

    if trade.state is PaperTradeState.CLOSED:
        return Decimal("0")
    locked = trade.capital_locked_gbp
    if locked is None or locked <= 0:
        return Decimal("0")
    return locked


def current_event_deployment_gbp(trades: list[PaperTrade], canonical_event_id: str) -> Decimal:
    """Locked reporting GBP on one canonical event.

    State labels are not the authority. PENDING, OPEN, PARTIAL and
    AWAITING_MANUAL_EXTERNAL count when they hold a positive lock. A trade
    with no lock contributes zero. CLOSED trades contribute zero even if a
    stale lock figure remains. Each trade is counted once.
    """

    if not str(canonical_event_id or "").strip():
        raise PlacementValuationError("missing_canonical_event_id")
    total = Decimal("0")
    for trade in trades:
        if trade.canonical_event_id != canonical_event_id:
            continue
        total += _locked_gbp(trade)
    return total


def current_opportunity_deployment_gbp(trade: PaperTrade) -> Decimal:
    """Active capital locked by this opportunity's paper trade."""

    return _locked_gbp(trade)


def remaining_room_gbp(limit_gbp: Decimal, deployed_gbp: Decimal) -> Decimal:
    room = limit_gbp - deployed_gbp
    if room < 0:
        return Decimal("0")
    return room


def remaining_event_room_gbp(trades: list[PaperTrade], canonical_event_id: str, limit_gbp: Decimal) -> Decimal:
    return remaining_room_gbp(limit_gbp, current_event_deployment_gbp(trades, canonical_event_id))


def discretionary_cycle_cap_gbp(
    *,
    event_room_gbp: Decimal,
    opportunity_room_gbp: Decimal,
    max_one_time_gbp: Decimal,
) -> Decimal:
    """Upper bound from the three operator thresholds before depth and treasury."""

    capped = min(event_room_gbp, opportunity_room_gbp, max_one_time_gbp)
    if capped < 0:
        return Decimal("0")
    return capped


def recovery_capital_room_gbp(max_opportunity_gbp: Decimal, deployed_gbp: Decimal | None) -> Decimal:
    """GBP room a risk-reducing recovery hedge may still lock.

    This is remaining Max Opportunity room. It replaces the old per-trade
    remaining room and does not apply Max Event or Max One-Time. Missing
    deployed GBP is treated as zero so recovery is not tighter than the
    previous ``remaining_trade_room_gbp`` behaviour.
    """

    locked = deployed_gbp if deployed_gbp is not None else Decimal("0")
    return remaining_room_gbp(max_opportunity_gbp, locked)
