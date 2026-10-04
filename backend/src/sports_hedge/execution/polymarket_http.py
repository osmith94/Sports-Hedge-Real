"""Authenticated Polymarket order transport. Not the market-data client.

Orders are signed and posted with ``polymarket-client`` 0.12.0. Token orders
use exchange protocol version 2. ``place_market_order`` is not used: that
helper can approve tokens and post a second time. This transport signs with
``create_market_order`` and calls ``post_order`` once.

FAK is the order type. The venue cancels an unfilled remainder. A resting
``live`` order is cancelled here and the fill quantity is left unchanged.
A delayed or timed-out submit is unknown, not zero, and is not posted again.

Price-2 remains the fee model. This module records a fee rate only when a
trade read returns one. It does not calculate a taker fee.

Fee audit, 2026-10-03, https://docs.polymarket.com/trading/fees :
the published formula is ``C × feeRate × p × (1 - p)``, applied at match time.
Sports is a category rate of 0.05 in that table, not a branch in this file.
``fees/polymarket.py`` still applies a per-market exponent. That matches the
published formula when the exponent is 1. CLOB market info can still return
an exponent. Collateral is pUSD. This transport does not change that model.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_DOWN, Decimal
from typing import Any, Protocol

from sports_hedge.config import Settings
from sports_hedge.domain.models import MarketSide, VenueName
from sports_hedge.execution.models import VenueOrderRequest, VenueOrderResult, VenueOrderStatus
from sports_hedge.execution.package import execution_armed
from sports_hedge.execution.polymarket_geoblock import (
    PolymarketEligibility,
    fetch_geoblock,
)
from sports_hedge.execution.polymarket_sdk import NOT_READY, PolymarketClientError, open_polymarket_client
from sports_hedge.execution.translate import (
    POLYMARKET_ORDER_TYPE,
    PolymarketNativeOrder,
    PolymarketOrderSide,
    TranslationError,
    polymarket_native_order,
)

_BASE = Decimal(10) ** 6
# Opening canary decision: a Polymarket SELL is share-denominated and is not
# an approved cash-funded lay. Dispatch rejects it before any venue call.
POLYMARKET_OPENING_LAY_NOT_APPROVED = "POLYMARKET_OPENING_LAY_NOT_APPROVED"


class PolymarketAmbiguous(Exception):
    """The submit call did not prove whether an order exists."""


@dataclass(frozen=True)
class PolymarketConstraints:
    tick_size: str
    minimum_shares: Decimal
    token_id: str


@dataclass(frozen=True)
class PolymarketSubmission:
    """One venue report. Missing amounts stay None."""

    state: str
    order_id: str | None = None
    making_amount: Decimal | None = None
    taking_amount: Decimal | None = None
    fee_rate_bps: Decimal | None = None
    code: str | None = None


class PolymarketBroker(Protocol):
    def constraints(self, token_id: str) -> PolymarketConstraints: ...

    def readiness(self, spend: Decimal) -> str | None: ...

    def post_fak(self, order: PolymarketNativeOrder) -> PolymarketSubmission: ...

    def cancel(self, order_id: str) -> str: ...

    def account_snapshot(self) -> dict[str, bool | str]: ...


class SdkPolymarketBroker:
    """Official client adapter. Constructing it does not open the client."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._client: Any = None
        self.posts = 0

    def constraints(self, token_id: str) -> PolymarketConstraints:
        book = self._open().get_order_book(token_id=token_id)
        if str(book.asset_id) != token_id:
            raise TranslationError("Polymarket book token does not match the order")
        tick = format(Decimal(str(book.tick_size)), "f")
        minimum = Decimal(str(book.min_order_size))
        if minimum <= 0:
            raise TranslationError("Polymarket minimum size is missing")
        return PolymarketConstraints(tick_size=tick, minimum_shares=minimum, token_id=token_id)

    def readiness(self, spend: Decimal) -> str | None:
        """pUSD readiness for a BUY. This does not check a SELL.

        ``get_balance_allowance`` is called only with ``asset_type="COLLATERAL"``.
        polymarket-client 0.12.0 types that argument as a string literal.
        ``AssetType`` is not an enum, so ``AssetType.COLLATERAL`` raises
        ``AttributeError``. ``get_trading_approvals_state`` is the general
        approval flag. Neither call reads ``CONDITIONAL`` or ``CONDITIONAL-V2``
        balance or allowance.
        A share-denominated SELL needs that conditional-token check. Opening
        LAY/SELL is rejected before this method runs.
        """

        client = self._open()
        state = client.get_trading_approvals_state()
        if not state.is_fully_approved:
            return f"{NOT_READY}: trading allowance is not approved"
        balance = client.get_balance_allowance(asset_type="COLLATERAL")
        required = int((spend * _BASE).to_integral_value(rounding=ROUND_DOWN))
        if balance.balance < required:
            return f"{NOT_READY}: collateral balance is below the order"
        if not any(amount >= required for amount in balance.allowances.values()):
            return f"{NOT_READY}: collateral allowance is below the order"
        return None

    def post_fak(self, order: PolymarketNativeOrder) -> PolymarketSubmission:
        client = self._open()
        if order.side is PolymarketOrderSide.BUY:
            # ``amount`` is the approved Price-2 stake. The live request does
            # not carry a separate approved fee budget, so ``max_spend`` is
            # omitted. Passing the stake as ``max_spend`` would make
            # polymarket-client 0.12.0 shrink the signed maker amount.
            signed = client.create_market_order(
                token_id=order.token_id,
                side="BUY",
                amount=order.amount,
                max_price=order.price,
                order_type=POLYMARKET_ORDER_TYPE,
            )
        else:
            signed = client.create_market_order(
                token_id=order.token_id,
                side="SELL",
                shares=order.shares,
                min_price=order.price,
                order_type=POLYMARKET_ORDER_TYPE,
            )
        self.posts += 1
        try:
            response = client.post_order(signed)
        except Exception as exc:
            raise PolymarketAmbiguous(type(exc).__name__) from exc
        return _submission(response, self._fee_rate(getattr(response, "trade_ids", ())))

    def cancel(self, order_id: str) -> str:
        response = self._open().cancel_order(order_id=order_id)
        if order_id in response.canceled:
            return "cancelled"
        return "cancel_unproven"

    def account_snapshot(self) -> dict[str, bool | str]:
        """Read-only account signals. Opening the client is separate from later reads.

        A balance or open-order failure leaves ``authenticated_read`` true.
        Trading readiness stays false unless collateral balance and allowance
        were both read. This method does not place or cancel an order.
        """

        client = self._open()
        balance = None
        balance_readable = False
        try:
            balance = client.get_balance_allowance(asset_type="COLLATERAL")
            balance_readable = balance is not None
        except Exception:  # noqa: BLE001 — balance failure must not invalidate the open client
            balance_readable = False
        trading_ready = False
        try:
            approvals = client.get_trading_approvals_state()
            trading_ready = bool(
                balance is not None
                and approvals.is_fully_approved
                and any(amount > 0 for amount in balance.allowances.values())
            )
        except Exception:  # noqa: BLE001 — approval failure stays on trading readiness
            trading_ready = False
        open_orders_readable = False
        try:
            page = client.list_open_orders().first_page()
            open_orders_readable = page is not None
        except Exception:  # noqa: BLE001 — open-order failure stays on that signal
            open_orders_readable = False
        return {
            "authenticated_read": True,
            "balance_readable": balance_readable,
            "trading_ready": trading_ready,
            "open_orders_readable": open_orders_readable,
        }

    def _open(self) -> Any:
        if self._client is None:
            self._client = open_polymarket_client(self._settings, derive_credentials=True)
        return self._client

    def _fee_rate(self, trade_ids: tuple[str, ...]) -> Decimal | None:
        if not trade_ids:
            return None
        try:
            page = self._open().list_account_trades(id=trade_ids[0]).first_page()
        except Exception:
            return None
        items = getattr(page, "items", None) or getattr(page, "data", None) or ()
        for trade in items:
            rate = getattr(trade, "fee_rate_bps", None)
            if rate is not None:
                return Decimal(str(rate))
        return None


class PolymarketHttpExecutionTransport:
    """One Polymarket execution client. Market-data reads do not go through here."""

    transport_kind = "polymarket_http"
    test_only = False

    def __init__(
        self,
        settings: Settings,
        *,
        broker: PolymarketBroker | None = None,
        geoblock: Callable[[], Awaitable[PolymarketEligibility]] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._settings = settings
        self._broker = broker if broker is not None else SdkPolymarketBroker(settings)
        self._geoblock = geoblock or (
            lambda: fetch_geoblock(settings.polymarket_geoblock_url)
        )
        self._clock = clock or (lambda: datetime.now(UTC))
        self._results: dict[str, VenueOrderResult] = {}
        self._unknown: set[str] = set()

    async def dispatch(self, request: VenueOrderRequest) -> VenueOrderResult:
        if request.venue is not VenueName.POLYMARKET:
            raise ValueError("Polymarket execution transport received another venue")
        now = self._clock()
        if not execution_armed(self._settings):
            return _result(request, status=VenueOrderStatus.FAILED, at=now, filled_size=Decimal(0))
        if request.side is MarketSide.LAY:
            return _result(
                request,
                status=VenueOrderStatus.FAILED,
                at=now,
                filled_size=Decimal(0),
                note=POLYMARKET_OPENING_LAY_NOT_APPROVED,
            )
        if request.currency.upper() != "USD":
            return _result(request, status=VenueOrderStatus.FAILED, at=now, filled_size=Decimal(0))
        prior = self._results.get(request.client_order_id)
        if prior is not None or request.client_order_id in self._unknown:
            return prior or _result(
                request,
                status=VenueOrderStatus.FAILED,
                at=now,
                filled_size=None,
                note="ambiguous_submit",
            )
        try:
            constraints = await asyncio.to_thread(self._broker.constraints, request.native_runner_id.strip())
            native = polymarket_native_order(
                native_runner_id=request.native_runner_id,
                native_market_id=request.native_market_id,
                side=request.side,
                decimal_odds=request.requested_price,
                requested_stake=request.requested_size,
                tick_size=constraints.tick_size,
                minimum_shares=constraints.minimum_shares,
            )
        except (TranslationError, PolymarketClientError):
            return _result(request, status=VenueOrderStatus.FAILED, at=now, filled_size=Decimal(0))
        try:
            block = await asyncio.to_thread(self._broker.readiness, native.stake)
        except (PolymarketClientError, Exception):
            return _result(
                request,
                status=VenueOrderStatus.FAILED,
                at=self._clock(),
                filled_size=Decimal(0),
                note=f"{NOT_READY}: account readiness could not be read",
                order_type=POLYMARKET_ORDER_TYPE,
            )
        if block:
            return _result(
                request,
                status=VenueOrderStatus.FAILED,
                at=self._clock(),
                filled_size=Decimal(0),
                note=block,
                order_type=POLYMARKET_ORDER_TYPE,
            )
        eligibility = await self._geoblock()
        if not eligibility.permitted:
            return _result(
                request,
                status=VenueOrderStatus.FAILED,
                at=self._clock(),
                filled_size=Decimal(0),
                note=eligibility.reason,
                order_type=POLYMARKET_ORDER_TYPE,
                eligibility=eligibility.audit(),
            )
        try:
            submission = await asyncio.to_thread(self._broker.post_fak, native)
        except PolymarketAmbiguous:
            self._unknown.add(request.client_order_id)
            result = _result(
                request,
                status=VenueOrderStatus.FAILED,
                at=self._clock(),
                filled_size=None,
                note="ambiguous_submit",
                order_type=POLYMARKET_ORDER_TYPE,
                eligibility=eligibility.audit(),
            )
            return result
        except Exception:
            return _result(
                request,
                status=VenueOrderStatus.FAILED,
                at=self._clock(),
                filled_size=Decimal(0),
                note="order_not_submitted",
                order_type=POLYMARKET_ORDER_TYPE,
                eligibility=eligibility.audit(),
            )
        result = _from_submission(request, native, submission, at=self._clock(), eligibility=eligibility)
        if result.note == "ambiguous_submit" or result.filled_size is None:
            self._unknown.add(request.client_order_id)
        else:
            self._results[request.client_order_id] = result
        if result.status is VenueOrderStatus.OPEN and result.venue_order_id:
            return await self._cancel_resting(request, native, result)
        return result

    async def cancel(self, request: VenueOrderRequest) -> VenueOrderResult:
        current = self._results.get(request.client_order_id)
        if current is None or not current.venue_order_id:
            return _result(
                request,
                status=VenueOrderStatus.FAILED,
                at=self._clock(),
                filled_size=None,
                note="cancel_unproven",
            )
        return await self._cancel_resting(request, None, current)

    async def _cancel_resting(
        self,
        request: VenueOrderRequest,
        native: PolymarketNativeOrder | None,
        current: VenueOrderResult,
    ) -> VenueOrderResult:
        try:
            cancel_result = await asyncio.to_thread(self._broker.cancel, current.venue_order_id or "")
        except Exception:
            cancel_result = "cancel_unproven"
        status = current.status
        if cancel_result == "cancelled" and current.filled_size == 0:
            status = VenueOrderStatus.CANCELLED
        elif current.filled_size is not None and current.filled_size > 0:
            status = VenueOrderStatus.PARTIAL
        updated = current.model_copy(
            update={
                "status": status,
                "cancel_result": cancel_result,
                "updated_at": self._clock(),
                "remainder_quantity": Decimal(0) if cancel_result == "cancelled" else current.remainder_quantity,
            }
        )
        if updated.filled_size is not None:
            self._results[request.client_order_id] = updated
        del native
        return updated


def _submission(response: Any, fee_rate: Decimal | None) -> PolymarketSubmission:
    ok = getattr(response, "ok", None)
    if ok is False:
        return PolymarketSubmission(state="rejected", code=str(getattr(response, "code", "unknown")))
    if ok is not True:
        return PolymarketSubmission(state="ambiguous")
    status = str(getattr(response, "status", "") or "")
    making = getattr(response, "making_amount", None)
    taking = getattr(response, "taking_amount", None)
    order_id = str(getattr(response, "order_id", "") or "") or None
    if status == "delayed":
        return PolymarketSubmission(state="delayed", order_id=order_id, fee_rate_bps=fee_rate)
    if status == "live":
        return PolymarketSubmission(
            state="live",
            order_id=order_id,
            making_amount=_decimal(making),
            taking_amount=_decimal(taking),
            fee_rate_bps=fee_rate,
        )
    if status == "matched":
        return PolymarketSubmission(
            state="matched",
            order_id=order_id,
            making_amount=_decimal(making),
            taking_amount=_decimal(taking),
            fee_rate_bps=fee_rate,
        )
    return PolymarketSubmission(state="ambiguous", order_id=order_id)


def _from_submission(
    request: VenueOrderRequest,
    native: PolymarketNativeOrder,
    submission: PolymarketSubmission,
    *,
    at: datetime,
    eligibility: PolymarketEligibility,
) -> VenueOrderResult:
    audit = eligibility.audit()
    if submission.state == "rejected":
        zero_codes = {"fak_not_filled", "fok_not_filled", "unmatched", "not_enough_balance"}
        status = (
            VenueOrderStatus.CANCELLED
            if submission.code in {"fak_not_filled", "fok_not_filled", "unmatched"}
            else VenueOrderStatus.REJECTED
        )
        note = submission.code if submission.code in zero_codes else submission.code or "rejected"
        if submission.code == "not_enough_balance":
            note = f"{NOT_READY}: not enough balance / allowance"
        return _result(
            request,
            status=status,
            at=at,
            filled_size=Decimal(0),
            venue_order_id=None,
            order_type=POLYMARKET_ORDER_TYPE,
            eligibility=audit,
            note=note,
            native_filled=Decimal(0),
            native_remaining=native.shares,
            remainder=native.shares,
            cancel_result="venue_fak" if status is VenueOrderStatus.CANCELLED else None,
        )
    if submission.state in {"ambiguous", "delayed"} or submission.making_amount is None or submission.taking_amount is None:
        return _result(
            request,
            status=VenueOrderStatus.FAILED,
            at=at,
            filled_size=None,
            venue_order_id=submission.order_id,
            order_type=POLYMARKET_ORDER_TYPE,
            eligibility=audit,
            note="ambiguous_submit" if submission.state != "delayed" else "matching_delayed",
            fee_rate=submission.fee_rate_bps,
        )
    filled_size, shares, average = _fill_economics(native, submission.making_amount, submission.taking_amount)
    if filled_size is None or shares is None:
        return _result(
            request,
            status=VenueOrderStatus.FAILED,
            at=at,
            filled_size=None,
            venue_order_id=submission.order_id,
            order_type=POLYMARKET_ORDER_TYPE,
            eligibility=audit,
            note="ambiguous_submit",
            fee_rate=submission.fee_rate_bps,
        )
    if filled_size > request.requested_size:
        filled_size = request.requested_size
    remaining = native.shares - shares
    if remaining < 0:
        remaining = Decimal(0)
    worse = _worse_than_limit(request, native, average)
    complete = (
        not worse
        and shares + Decimal("0.0000001") >= native.shares
        and filled_size + Decimal("0.0000001") >= native.stake
    )
    if submission.state == "live":
        status = VenueOrderStatus.OPEN
        cancel_result = None
    elif shares == 0 and filled_size == 0:
        status = VenueOrderStatus.CANCELLED
        average = None
        cancel_result = "venue_fak"
    elif complete:
        status = VenueOrderStatus.FILLED
        cancel_result = "venue_fak"
    else:
        status = VenueOrderStatus.PARTIAL
        cancel_result = "venue_fak"
    return _result(
        request,
        status=status,
        at=at,
        filled_size=filled_size,
        average_fill_price=average if filled_size > 0 else None,
        venue_order_id=submission.order_id,
        order_type=POLYMARKET_ORDER_TYPE,
        eligibility=audit,
        note=None if status is not VenueOrderStatus.PARTIAL else "partial_fill",
        fee_rate=submission.fee_rate_bps,
        native_filled=shares,
        native_remaining=remaining,
        remainder=remaining,
        cancel_result=cancel_result,
    )


def _fill_economics(
    native: PolymarketNativeOrder,
    making: Decimal,
    taking: Decimal,
) -> tuple[Decimal | None, Decimal | None, Decimal | None]:
    if making < 0 or taking < 0:
        return None, None, None
    if making == 0 and taking == 0:
        return Decimal(0), Decimal(0), None
    if making == 0 or taking == 0:
        return None, None, None
    if native.side is PolymarketOrderSide.BUY:
        collateral = making
        shares = taking
        price = collateral / shares
    else:
        shares = making
        collateral = taking
        price = collateral / shares
    if price <= 0 or price >= 1:
        return None, None, None
    return collateral, shares, Decimal(1) / price


def _worse_than_limit(
    request: VenueOrderRequest,
    native: PolymarketNativeOrder,
    average: Decimal | None,
) -> bool:
    if average is None:
        return True
    if native.side is PolymarketOrderSide.BUY:
        return average + Decimal("0.0000001") < request.requested_price
    return average > request.requested_price + Decimal("0.0000001")


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None


def _result(
    request: VenueOrderRequest,
    *,
    status: VenueOrderStatus,
    at: datetime,
    filled_size: Decimal | None,
    average_fill_price: Decimal | None = None,
    venue_order_id: str | None = None,
    order_type: str | None = None,
    eligibility: dict[str, Any] | None = None,
    note: str | None = None,
    fee_rate: Decimal | None = None,
    native_filled: Decimal | None = None,
    native_remaining: Decimal | None = None,
    remainder: Decimal | None = None,
    cancel_result: str | None = None,
) -> VenueOrderResult:
    return VenueOrderResult(
        venue=request.venue,
        client_order_id=request.client_order_id,
        venue_order_id=venue_order_id,
        status=status,
        requested_size=request.requested_size,
        filled_size=filled_size,
        requested_price=request.requested_price,
        average_fill_price=average_fill_price,
        submitted_at=at,
        updated_at=at,
        native_filled_quantity=native_filled,
        native_remaining_quantity=native_remaining,
        order_type=order_type,
        venue_fee_rate_bps=fee_rate,
        remainder_quantity=remainder,
        cancel_result=cancel_result,
        eligibility=eligibility,
        note=note,
    )
