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
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sports_hedge.application.execution_snapshot import (
    NATIVE_ORDER_FREEZE_REASONS,
    FrozenNativeOrder,
    NativeOrderFreezeDiagnostic,
)
from sports_hedge.domain.models import MarketSide, VenueName
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


def _optional_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return _text(value)


def _payload(book: Any) -> Mapping[str, Any]:
    payload = getattr(book, "payload", book)
    if isinstance(payload, Mapping):
        return payload
    return {}


@dataclass(frozen=True)
class NativeOrderFreezePackage:
    """Frozen orders plus per-leg diagnostics. Diagnostics are not a gate."""

    orders: tuple[FrozenNativeOrder, ...]
    diagnostics: tuple[NativeOrderFreezeDiagnostic, ...]


def polymarket_constraints_from_book(payload: Mapping[str, Any]) -> tuple[str, Decimal] | None:
    """Tick and minimum already present on the Price-2 CLOB book."""

    inspected = _inspect_polymarket_constraints(payload)
    if inspected.tick_size is None or inspected.minimum_shares is None:
        return None
    return inspected.tick_size, inspected.minimum_shares


def freeze_native_orders(
    decision: Any,
    *,
    polymarket_books: Mapping[str, Any] | None = None,
) -> tuple[FrozenNativeOrder, ...]:
    """Local translation of the accepted legs. No network I/O."""

    return freeze_native_package(decision, polymarket_books=polymarket_books).orders


def freeze_native_package(
    decision: Any,
    *,
    polymarket_books: Mapping[str, Any] | None = None,
) -> NativeOrderFreezePackage:
    """Same local freeze as ``freeze_native_orders``, with structured evidence."""

    books = polymarket_books or {}
    frozen: list[FrozenNativeOrder] = []
    diagnostics: list[NativeOrderFreezeDiagnostic] = []
    for leg in getattr(decision, "fill_legs", ()) or ():
        stake = getattr(leg, "requested_stake", None)
        if stake is None or stake <= 0:
            continue
        venue = getattr(leg, "venue", None)
        venue_name = venue.value if isinstance(venue, VenueName) else str(venue or "")
        if venue_name == VenueName.POLYMARKET.value:
            item, diagnostic = _freeze_polymarket(leg, books)
        elif venue_name == VenueName.MATCHBOOK.value:
            item, diagnostic = _freeze_matchbook(leg)
        else:
            item, diagnostic = None, None
        if diagnostic is not None:
            diagnostics.append(diagnostic)
        if item is not None:
            frozen.append(item)
    return NativeOrderFreezePackage(orders=tuple(frozen), diagnostics=tuple(diagnostics))


@dataclass(frozen=True)
class _PolymarketConstraintInspection:
    tick_present: bool
    minimum_present: bool
    tick_size: str | None
    minimum_shares: Decimal | None
    invalid_metadata: bool


def _inspect_polymarket_constraints(payload: Mapping[str, Any]) -> _PolymarketConstraintInspection:
    raw_tick = payload.get("tick_size", payload.get("tickSize"))
    raw_minimum = payload.get("min_order_size", payload.get("minOrderSize"))
    tick_present = raw_tick is not None and str(raw_tick) != ""
    minimum_present = raw_minimum is not None and str(raw_minimum) != ""
    tick_size: str | None = None
    minimum_shares: Decimal | None = None
    invalid = False
    if tick_present:
        try:
            tick_size = format(Decimal(str(raw_tick)), "f")
        except (InvalidOperation, ValueError, TypeError):
            invalid = True
    if minimum_present:
        try:
            parsed = Decimal(str(raw_minimum))
        except (InvalidOperation, ValueError, TypeError):
            invalid = True
        else:
            if parsed <= 0:
                invalid = True
            else:
                minimum_shares = parsed
    return _PolymarketConstraintInspection(
        tick_present=tick_present,
        minimum_present=minimum_present,
        tick_size=tick_size,
        minimum_shares=minimum_shares,
        invalid_metadata=invalid,
    )


def _freeze_polymarket(
    leg: Any, books: Mapping[str, Any]
) -> tuple[FrozenNativeOrder | None, NativeOrderFreezeDiagnostic]:
    runner = str(getattr(leg, "source_runner_id", "") or "").strip()
    market = str(getattr(leg, "source_market_id", "") or "").strip()
    event = str(getattr(leg, "source_event_id", "") or "").strip()
    payload = _payload(books.get(runner))
    inspected = _inspect_polymarket_constraints(payload)
    identity = _identity_fields(leg, runner, market)
    if not inspected.tick_present:
        return None, _diagnostic(
            venue=VenueName.POLYMARKET.value,
            frozen=False,
            reason="missing_tick_size",
            **identity,
            observed_tick_size=None,
            observed_minimum_shares=_optional_text(inspected.minimum_shares),
        )
    if not inspected.minimum_present:
        return None, _diagnostic(
            venue=VenueName.POLYMARKET.value,
            frozen=False,
            reason="missing_minimum_order_size",
            **identity,
            observed_tick_size=inspected.tick_size,
            observed_minimum_shares=None,
        )
    if inspected.invalid_metadata or inspected.tick_size is None or inspected.minimum_shares is None:
        return None, _diagnostic(
            venue=VenueName.POLYMARKET.value,
            frozen=False,
            reason="invalid_constraint_metadata",
            **identity,
            observed_tick_size=inspected.tick_size,
            observed_minimum_shares=_optional_text(inspected.minimum_shares),
        )
    if not runner or not market:
        return None, _diagnostic(
            venue=VenueName.POLYMARKET.value,
            frozen=False,
            reason="missing_or_invalid_native_market_or_token",
            **identity,
            observed_tick_size=inspected.tick_size,
            observed_minimum_shares=_optional_text(inspected.minimum_shares),
        )
    tick_size, minimum = inspected.tick_size, inspected.minimum_shares
    try:
        native = polymarket_native_order(
            native_runner_id=runner,
            native_market_id=market,
            side=MarketSide.BACK,
            decimal_odds=leg.displayed_odds,
            requested_stake=leg.requested_stake,
            tick_size=tick_size,
            minimum_shares=minimum,
        )
    except TranslationError as exc:
        return None, _diagnostic(
            venue=VenueName.POLYMARKET.value,
            frozen=False,
            reason=_translation_reason(exc),
            **identity,
            observed_tick_size=tick_size,
            observed_minimum_shares=_text(minimum),
            **_safe_translation_details(
                exc, fallback_stake=_optional_text(getattr(leg, "requested_stake", None))
            ),
        )
    except (AttributeError, TypeError, InvalidOperation, ValueError):
        return None, _diagnostic(
            venue=VenueName.POLYMARKET.value,
            frozen=False,
            reason="native_order_translation_other",
            **identity,
            observed_tick_size=tick_size,
            observed_minimum_shares=_text(minimum),
            intended_native_stake=_optional_text(getattr(leg, "requested_stake", None)),
        )
    return (
        FrozenNativeOrder(
            venue=VenueName.POLYMARKET.value,
            native_event_id=event,
            native_market_id=market,
            native_runner_id=native.token_id,
            side=MarketSide.BACK.value,
            currency=str(getattr(leg, "currency", "") or "USD"),
            approved_decimal_odds=_text(leg.displayed_odds),
            approved_stake=_text(leg.requested_stake),
            order_type=native.order_type,
            tick_size=native.tick_size,
            minimum_order_size=_text(minimum),
            limit_price=_text(native.price),
            native_amount=_text(native.amount),
            native_shares=_text(native.shares),
        ),
        _diagnostic(
            venue=VenueName.POLYMARKET.value,
            frozen=True,
            reason="frozen",
            **identity,
            observed_tick_size=native.tick_size,
            observed_minimum_shares=_text(minimum),
            intended_native_stake=_text(native.amount),
            intended_native_shares=_text(native.shares),
            intended_limit_price=_text(native.price),
        ),
    )


def _freeze_matchbook(leg: Any) -> tuple[FrozenNativeOrder | None, NativeOrderFreezeDiagnostic]:
    runner = str(getattr(leg, "source_runner_id", "") or "").strip()
    market = str(getattr(leg, "source_market_id", "") or "").strip()
    event = str(getattr(leg, "source_event_id", "") or "").strip()
    identity = _identity_fields(leg, runner, market)
    if not runner or not market or not event:
        return None, _diagnostic(
            venue=VenueName.MATCHBOOK.value,
            frozen=False,
            reason="missing_or_invalid_native_market_or_token",
            **identity,
            intended_native_stake=_optional_text(getattr(leg, "requested_stake", None)),
        )
    try:
        odds = matchbook_limit_odds(leg.displayed_odds, side=MarketSide.BACK)
        stake = matchbook_stake(leg.requested_stake)
        int(runner)
    except TranslationError as exc:
        return None, _diagnostic(
            venue=VenueName.MATCHBOOK.value,
            frozen=False,
            reason=_translation_reason(exc),
            **identity,
            **_safe_translation_details(
                exc, fallback_stake=_optional_text(getattr(leg, "requested_stake", None))
            ),
        )
    except (AttributeError, TypeError, ValueError):
        return None, _diagnostic(
            venue=VenueName.MATCHBOOK.value,
            frozen=False,
            reason="missing_or_invalid_native_market_or_token"
            if _looks_like_invalid_runner(runner)
            else "native_order_translation_other",
            **identity,
            intended_native_stake=_optional_text(getattr(leg, "requested_stake", None)),
        )
    return (
        FrozenNativeOrder(
            venue=VenueName.MATCHBOOK.value,
            native_event_id=event,
            native_market_id=market,
            native_runner_id=runner,
            side=MarketSide.BACK.value,
            currency=str(getattr(leg, "currency", "") or "GBP"),
            approved_decimal_odds=_text(leg.displayed_odds),
            approved_stake=_text(leg.requested_stake),
            order_type="back-lay",
            ladder_odds=_text(odds),
            native_stake=_text(stake),
            native_side="back",
        ),
        _diagnostic(
            venue=VenueName.MATCHBOOK.value,
            frozen=True,
            reason="frozen",
            **identity,
            intended_native_stake=_text(stake),
            intended_limit_price=_text(odds),
        ),
    )


def _identity_fields(leg: Any, runner: str, market: str) -> dict[str, str | None]:
    outcome = str(getattr(leg, "outcome", "") or "").strip() or None
    return {
        "outcome": outcome,
        "native_market_id": market or None,
        "native_runner_id": runner or None,
    }


def _looks_like_invalid_runner(runner: str) -> bool:
    if not runner:
        return True
    try:
        int(runner)
    except ValueError:
        return True
    return False


def _translation_reason(exc: TranslationError) -> str:
    code = getattr(exc, "code", None)
    if isinstance(code, str) and code in NATIVE_ORDER_FREEZE_REASONS and code != "frozen":
        return code
    return "native_order_translation_other"


def _safe_translation_details(
    exc: TranslationError,
    *,
    fallback_stake: str | None = None,
) -> dict[str, str | None]:
    raw = getattr(exc, "details", None) or {}
    fields = {
        "intended_native_stake": _numeric_text(raw.get("intended_native_stake")) or fallback_stake,
        "intended_native_shares": _numeric_text(raw.get("intended_native_shares")),
        "intended_limit_price": _numeric_text(raw.get("intended_limit_price")),
    }
    return {key: value for key, value in fields.items() if value is not None}


def _numeric_text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    try:
        return format(Decimal(str(value)), "f")
    except (InvalidOperation, ValueError, TypeError):
        return None


def _diagnostic(
    *,
    venue: str,
    frozen: bool,
    reason: str,
    outcome: str | None = None,
    native_market_id: str | None = None,
    native_runner_id: str | None = None,
    observed_tick_size: str | None = None,
    observed_minimum_shares: str | None = None,
    intended_native_stake: str | None = None,
    intended_native_shares: str | None = None,
    intended_limit_price: str | None = None,
) -> NativeOrderFreezeDiagnostic:
    safe_reason = reason if reason in NATIVE_ORDER_FREEZE_REASONS else "native_order_translation_other"
    if frozen:
        safe_reason = "frozen"
    return NativeOrderFreezeDiagnostic(
        venue=venue,
        frozen=frozen,
        reason=safe_reason,
        outcome=outcome,
        native_market_id=native_market_id,
        native_runner_id=native_runner_id,
        observed_tick_size=observed_tick_size,
        observed_minimum_shares=observed_minimum_shares,
        intended_native_stake=intended_native_stake,
        intended_native_shares=intended_native_shares,
        intended_limit_price=intended_limit_price,
    )
