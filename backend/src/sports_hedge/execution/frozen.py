"""Freeze the native order Price-2 already proved.

Execution submits these fields. It does not call the order book, a balance
endpoint, or a geoblock endpoint to reconstruct them.

Critical path, conceptual, no measured timings:

Before:
    Price-2 book read
    accept
    execution geoblock
    execution get_order_book (tick and minimum)
    execution get_balance_allowance
    sign and POST

After:
    Price-2 book read
    Price-2 collateral / free-funds read when mode is real
    accept and freeze
    sign and POST

Removed from the execution path:
    Polymarket GET /api/geoblock
    Polymarket get_order_book used only for tick size and minimum size
    Polymarket get_balance_allowance(asset_type="COLLATERAL")

Matchbook execution did not read the book or the balance before POST.
That stays a login plus the offer POST. Real-mode free-funds is read once
inside Price-2, not again at dispatch.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from sports_hedge.application.execution_snapshot import FrozenNativeOrder
from sports_hedge.domain.models import MarketSide, VenueName
from sports_hedge.execution.depth_plan import (
    DepthPlanError,
    NativeDepthPlan,
    approved_back_limit_odds,
    expected_average_odds,
    sweep_back_levels,
)
from sports_hedge.execution.translate import (
    TranslationError,
    matchbook_limit_odds,
    matchbook_stake,
    polymarket_native_order,
)

REMOVED_POLYMARKET_EXECUTION_READS = (
    "GET /api/geoblock",
    "get_order_book",
    "get_balance_allowance COLLATERAL",
)


def _text(value: Any) -> str:
    return format(value, "f") if isinstance(value, Decimal) else str(value)


def _payload(book: Any) -> Mapping[str, Any]:
    payload = getattr(book, "payload", book)
    if isinstance(payload, Mapping):
        return payload
    return {}


def polymarket_constraints_from_book(payload: Mapping[str, Any]) -> tuple[str, Decimal] | None:
    """Tick and minimum already present on the Price-2 CLOB book."""

    tick = payload.get("tick_size", payload.get("tickSize"))
    minimum = payload.get("min_order_size", payload.get("minOrderSize"))
    if tick is None or minimum is None:
        return None
    try:
        tick_size = format(Decimal(str(tick)), "f")
        minimum_shares = Decimal(str(minimum))
    except (InvalidOperation, ValueError):
        return None
    if minimum_shares <= 0:
        return None
    return tick_size, minimum_shares


def freeze_native_orders(
    decision: Any,
    *,
    polymarket_books: Mapping[str, Any] | None = None,
) -> tuple[FrozenNativeOrder, ...]:
    """Local translation of the accepted legs. No network I/O."""

    books = polymarket_books or {}
    frozen: list[FrozenNativeOrder] = []
    for leg in getattr(decision, "fill_legs", ()) or ():
        stake = getattr(leg, "requested_stake", None)
        if stake is None or stake <= 0:
            continue
        venue = getattr(leg, "venue", None)
        venue_name = venue.value if isinstance(venue, VenueName) else str(venue or "")
        if venue_name == VenueName.POLYMARKET.value:
            item = _freeze_polymarket(leg, books)
        elif venue_name == VenueName.MATCHBOOK.value:
            item = _freeze_matchbook(leg)
        else:
            item = None
        if item is not None:
            frozen.append(item)
    return tuple(frozen)


def _freeze_polymarket(leg: Any, books: Mapping[str, Any]) -> FrozenNativeOrder | None:
    runner = str(getattr(leg, "source_runner_id", "") or "").strip()
    market = str(getattr(leg, "source_market_id", "") or "").strip()
    event = str(getattr(leg, "source_event_id", "") or "").strip()
    payload = _payload(books.get(runner))
    constraints = polymarket_constraints_from_book(payload)
    if constraints is None or not runner or not market:
        return None
    tick_size, minimum = constraints
    try:
        plan = _plan_for_leg(leg)
        limit_odds = approved_back_limit_odds(leg)
        native = polymarket_native_order(
            native_runner_id=runner,
            native_market_id=market,
            side=MarketSide.BACK,
            decimal_odds=limit_odds,
            requested_stake=leg.requested_stake,
            tick_size=tick_size,
            minimum_shares=minimum,
        )
        implied = Decimal(1) / limit_odds
        if plan is not None and native.price < implied:
            raise TranslationError("Polymarket tick cannot express the approved worst BUY price")
    except (TranslationError, AttributeError, DepthPlanError):
        return None
    return FrozenNativeOrder(
        venue=VenueName.POLYMARKET.value,
        native_event_id=event,
        native_market_id=market,
        native_runner_id=native.token_id,
        side=MarketSide.BACK.value,
        currency=str(getattr(leg, "currency", "") or "USD"),
        approved_decimal_odds=_text(expected_average_odds(leg)),
        approved_stake=_text(leg.requested_stake),
        order_type=native.order_type,
        tick_size=native.tick_size,
        minimum_order_size=_text(minimum),
        limit_price=_text(native.price),
        native_amount=_text(native.amount),
        native_shares=_text(native.shares),
        approved_depth_worst_limit=_text(limit_odds),
        expected_average=_text(expected_average_odds(leg)),
        requested_total=_text(leg.requested_stake),
        admitted_levels=_admitted_pairs(plan),
    )


def _freeze_matchbook(leg: Any) -> FrozenNativeOrder | None:
    runner = str(getattr(leg, "source_runner_id", "") or "").strip()
    market = str(getattr(leg, "source_market_id", "") or "").strip()
    event = str(getattr(leg, "source_event_id", "") or "").strip()
    if not runner or not market or not event:
        return None
    try:
        plan = _plan_for_leg(leg)
        limit_odds = approved_back_limit_odds(leg)
        odds = matchbook_limit_odds(limit_odds, side=MarketSide.BACK)
        if odds < limit_odds:
            raise TranslationError("Matchbook back limit rounded below the approved worst level")
        if plan is not None and odds > limit_odds:
            raise TranslationError("Matchbook ladder snap would miss the approved worst BACK level")
        stake = matchbook_stake(leg.requested_stake)
        int(runner)
    except (TranslationError, AttributeError, ValueError, DepthPlanError):
        return None
    return FrozenNativeOrder(
        venue=VenueName.MATCHBOOK.value,
        native_event_id=event,
        native_market_id=market,
        native_runner_id=runner,
        side=MarketSide.BACK.value,
        currency=str(getattr(leg, "currency", "") or "GBP"),
        approved_decimal_odds=_text(expected_average_odds(leg)),
        approved_stake=_text(leg.requested_stake),
        order_type="back-lay",
        ladder_odds=_text(odds),
        native_stake=_text(stake),
        native_side="back",
        approved_depth_worst_limit=_text(limit_odds),
        expected_average=_text(expected_average_odds(leg)),
        requested_total=_text(leg.requested_stake),
        admitted_levels=_admitted_pairs(plan),
    )


def _plan_for_leg(leg: Any) -> NativeDepthPlan | None:
    levels = list(getattr(leg, "levels", None) or [])
    stake = getattr(leg, "requested_stake", None)
    if not levels or stake is None or stake <= 0:
        return None
    plan = sweep_back_levels(levels, stake)
    if not plan.fully_filled:
        raise DepthPlanError("approved_stake_exceeds_executable_depth")
    return plan


def _admitted_pairs(plan: NativeDepthPlan | None) -> tuple[tuple[str, str], ...]:
    if plan is None:
        return ()
    return plan.as_admitted_pairs()
