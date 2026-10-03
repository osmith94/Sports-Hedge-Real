"""Authenticated Kalshi order transport. Not a market-data client.

Create Order V2 (``POST /portfolio/events/orders``) is the order endpoint.
The legacy ``/portfolio/orders`` create path is not used. V2 quotes a YES
price and a book side: ``bid`` buys YES, ``ask`` sells YES. Selling YES is
the same exposure as buying NO at ``1 - price``.

``time_in_force`` is ``immediate_or_cancel``. Unfilled remainder is cancelled
by the venue. ``self_trade_prevention_type`` is ``taker_at_cross``, which is
the value in Kalshi's create-order quick start. ``cancel_order_on_pause`` is
true so a paused exchange cancels a still-resting order.

``client_order_id`` is the Wave 1 digest rendered as a UUID. A repeat submit
of that id is a 409, not a second order. On 409 this transport looks up the
existing order and does not POST again. Lookup uses the 409 body when it
contains an order id, otherwise ``GET /portfolio/orders`` filtered by the
ticker. If that scan does not find the id, the result is failed and no
further create is attempted.

Average fill price on the V2 create response is the YES price. Decimal odds
are ``1 / yes_price`` for a long-YES order and ``1 / (1 - yes_price)`` for a
long-NO order. When the create response has no average and the get-order
payload has separate fill cost and fill count, unit cost is
``(taker_fill_cost + maker_fill_cost) / fill_count``. Fees are not folded in.
If neither source is present, average decimal odds stay empty.

Cancel Order V2 is ``DELETE /portfolio/events/orders/{order_id}`` on the
trade-api host, so the signed path is
``/trade-api/v2/portfolio/events/orders/{order_id}`` with the query omitted.
Auto-routing requires ``market_ticker`` when ``exchange_index`` is omitted or
``-1``. This cancel sends ``market_ticker`` and omits ``exchange_index``. An
order id alone is not sent, because that would default to shard 0.

Signing is RSA-PSS SHA-256 over ``timestamp_ms + METHOD + path``. The path is
the URL path from the host, without the query string. The private key is
never logged.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import httpx

from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.execution.models import VenueOrderRequest, VenueOrderResult, VenueOrderStatus
from sports_hedge.execution.package import execution_armed
from sports_hedge.execution.translate import (
    KALSHI_DEFAULT_PRICE_TICK,
    KalshiNativeLimit,
    TranslationError,
    decimal_odds_from_yes_price,
    kalshi_action,
    kalshi_contract_side,
    kalshi_native_limit,
    kalshi_ticker,
    kalshi_wire_client_order_id,
)

CREATE_PATH = "/portfolio/events/orders"
CANCEL_PATH = "/portfolio/events/orders"
ORDERS_PATH = "/portfolio/orders"
_ORDER_PAGE_LIMIT = 200
_ORDER_PAGE_CAP = 5


class KalshiHttpExecutionTransport:
    """Signed Kalshi order client. Market-data reads do not go through here."""

    transport_kind = "kalshi_http"
    test_only = False

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Callable[[], datetime] | None = None,
        private_key: Any = None,
        price_tick: Decimal = KALSHI_DEFAULT_PRICE_TICK,
    ) -> None:
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owns_client = client is None
        self._price_tick = price_tick
        self._private_key = private_key
        base = settings.resolved_kalshi_base_url().rstrip("/")
        self._base_url = base
        self._client = client or httpx.AsyncClient(
            base_url=base,
            timeout=httpx.Timeout(10.0),
            headers={"Accept": "application/json", "Content-Type": "application/json"},
        )
        self._orders: dict[str, str] = {}
        self._unknown_submit: set[str] = set()
        self._observed_fills: dict[str, VenueOrderResult] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def dispatch(self, request: VenueOrderRequest) -> VenueOrderResult:
        if request.venue is not VenueName.KALSHI:
            raise ValueError("Kalshi execution transport received another venue")
        now = self._clock()
        if not execution_armed(self._settings):
            return _result(request, status=VenueOrderStatus.FAILED, at=now)
        if request.currency.upper() != "USD":
            return _result(request, status=VenueOrderStatus.FAILED, at=now)
        wire_id = _wire_id(request.client_order_id)
        if wire_id is None:
            return _result(request, status=VenueOrderStatus.FAILED, at=now)
        known = self._orders.get(request.client_order_id)
        if known:
            return await self._get_order(request, known, limit=None, at=now)
        if request.client_order_id in self._unknown_submit:
            return _result(request, status=VenueOrderStatus.FAILED, at=now)
        try:
            limit = kalshi_native_limit(
                decimal_odds=request.requested_price,
                requested_stake=request.requested_size,
                contract_side=kalshi_contract_side(request.native_runner_id),
                action=kalshi_action(request.side),
                tick=self._price_tick,
            )
            ticker = kalshi_ticker(request.native_runner_id, request.native_market_id)
        except TranslationError:
            return _result(request, status=VenueOrderStatus.FAILED, at=now)
        body = {
            "ticker": ticker,
            "client_order_id": wire_id,
            "side": limit.book_side,
            "count": f"{limit.contract_count:.2f}",
            "price": format(limit.yes_price.quantize(Decimal("0.0001")), "f"),
            "time_in_force": "immediate_or_cancel",
            "self_trade_prevention_type": "taker_at_cross",
            "post_only": False,
            "cancel_order_on_pause": True,
            "reduce_only": False,
        }
        if not (self._settings.kalshi_api_key_id or "").strip() or self._private_key is None:
            return _result(request, status=VenueOrderStatus.FAILED, at=now)
        self._unknown_submit.add(request.client_order_id)
        try:
            response = await self._send("POST", CREATE_PATH, json_body=body)
        except httpx.HTTPError:
            return _result(request, status=VenueOrderStatus.FAILED, at=self._clock())
        if response.status_code == 409:
            recovered = await self._recover_conflict(request, wire_id, ticker, limit, at=self._clock())
            return recovered
        if response.status_code >= 400:
            self._unknown_submit.discard(request.client_order_id)
            status = VenueOrderStatus.REJECTED if response.status_code < 500 else VenueOrderStatus.FAILED
            return _result(request, status=status, at=self._clock())
        return self._remember(request, response, limit, at=self._clock())

    async def cancel(self, request: VenueOrderRequest) -> VenueOrderResult:
        now = self._clock()
        if not execution_armed(self._settings):
            return _result(request, status=VenueOrderStatus.FAILED, at=now)
        order_id = self._orders.get(request.client_order_id)
        if not order_id:
            return _result(request, status=VenueOrderStatus.FAILED, at=now)
        try:
            ticker = kalshi_ticker(request.native_runner_id, request.native_market_id)
        except TranslationError:
            return _result(request, status=VenueOrderStatus.FAILED, at=now, venue_order_id=order_id)
        try:
            response = await self._send(
                "DELETE",
                f"{CANCEL_PATH}/{order_id}",
                params={"market_ticker": ticker},
            )
        except httpx.HTTPError:
            return _result(request, status=VenueOrderStatus.FAILED, at=self._clock(), venue_order_id=order_id)
        if response.status_code >= 400:
            return _result(request, status=VenueOrderStatus.FAILED, at=self._clock(), venue_order_id=order_id)
        payload = _json_object(response)
        if payload is not None and "reduced_by" in payload and _count(payload, "fill_count", "fill_count_fp") is None:
            return await self._cancel_without_fill(
                request,
                _text(payload.get("order_id")) or order_id,
            )
        order = _order_payload(payload)
        if order is None:
            return await self._get_order(request, order_id, limit=None, at=self._clock())
        return self._note(request.client_order_id, _from_order(request, order, limit=None, at=self._clock()))

    async def _cancel_without_fill(self, request: VenueOrderRequest, order_id: str) -> VenueOrderResult:
        """V2 cancel confirms the remainder is gone. Fill comes from a later read."""

        looked_up = await self._get_order(request, order_id, limit=None, at=self._clock())
        if looked_up.native_filled_quantity is not None:
            status = (
                VenueOrderStatus.CANCELLED
                if looked_up.native_filled_quantity == 0
                else VenueOrderStatus.PARTIAL
            )
            return looked_up.model_copy(
                update={
                    "status": status,
                    "native_remaining_quantity": Decimal(0),
                    "venue_order_id": looked_up.venue_order_id or order_id,
                }
            )
        prior = self._observed_fills.get(request.client_order_id)
        if prior is not None and prior.native_filled_quantity is not None:
            status = (
                VenueOrderStatus.CANCELLED
                if prior.native_filled_quantity == 0
                else VenueOrderStatus.PARTIAL
            )
            return prior.model_copy(
                update={
                    "status": status,
                    "native_remaining_quantity": Decimal(0),
                    "venue_order_id": prior.venue_order_id or order_id,
                    "updated_at": self._clock(),
                }
            )
        return _result(
            request,
            status=VenueOrderStatus.CANCELLED,
            at=self._clock(),
            filled_size=None,
            venue_order_id=order_id,
            native_remaining=Decimal(0),
        )

    def _note(self, client_order_id: str, result: VenueOrderResult) -> VenueOrderResult:
        if result.native_filled_quantity is not None:
            self._observed_fills[client_order_id] = result
        return result

    async def _recover_conflict(
        self,
        request: VenueOrderRequest,
        wire_id: str,
        ticker: str,
        limit: KalshiNativeLimit,
        *,
        at: datetime,
    ) -> VenueOrderResult:
        # The duplicate already exists. Never POST again from this method.
        found = await self._find_client_order(wire_id, ticker)
        if found is None:
            return _result(request, status=VenueOrderStatus.FAILED, at=at)
        order_id = str(found.get("order_id") or "")
        if order_id:
            self._orders[request.client_order_id] = order_id
            self._unknown_submit.discard(request.client_order_id)
        return self._note(request.client_order_id, _from_order(request, found, limit=limit, at=self._clock()))

    async def _find_client_order(self, wire_id: str, ticker: str) -> dict[str, Any] | None:
        cursor = ""
        for _ in range(_ORDER_PAGE_CAP):
            path = f"{ORDERS_PATH}?ticker={ticker}&limit={_ORDER_PAGE_LIMIT}"
            if cursor:
                path = f"{path}&cursor={cursor}"
            try:
                response = await self._send("GET", path)
            except httpx.HTTPError:
                return None
            if response.status_code >= 400:
                return None
            body = _json_object(response)
            if body is None:
                return None
            orders = body.get("orders")
            if isinstance(orders, list):
                for order in orders:
                    if isinstance(order, dict) and str(order.get("client_order_id") or "") == wire_id:
                        return order
            cursor = str(body.get("cursor") or "")
            if not cursor:
                return None
        return None

    async def _get_order(
        self,
        request: VenueOrderRequest,
        order_id: str,
        *,
        limit: KalshiNativeLimit | None,
        at: datetime,
    ) -> VenueOrderResult:
        try:
            response = await self._send("GET", f"{ORDERS_PATH}/{order_id}")
        except httpx.HTTPError:
            return _result(request, status=VenueOrderStatus.FAILED, at=self._clock(), venue_order_id=order_id)
        if response.status_code >= 400:
            return _result(request, status=VenueOrderStatus.FAILED, at=self._clock(), venue_order_id=order_id)
        order = _order_payload(_json_object(response))
        if order is None:
            return _result(request, status=VenueOrderStatus.FAILED, at=at, venue_order_id=order_id)
        return self._note(request.client_order_id, _from_order(request, order, limit=limit, at=self._clock()))

    def _remember(
        self,
        request: VenueOrderRequest,
        response: httpx.Response,
        limit: KalshiNativeLimit,
        *,
        at: datetime,
    ) -> VenueOrderResult:
        payload = _json_object(response)
        if payload is None:
            return _result(request, status=VenueOrderStatus.FAILED, at=at)
        order_id = str(payload.get("order_id") or "")
        if not order_id:
            nested = _order_payload(payload)
            order_id = "" if nested is None else str(nested.get("order_id") or "")
        if order_id:
            self._orders[request.client_order_id] = order_id
            self._unknown_submit.discard(request.client_order_id)
        source = payload if "fill_count" in payload or "fill_count_fp" in payload else _order_payload(payload)
        if source is None:
            return _result(request, status=VenueOrderStatus.FAILED, at=at, venue_order_id=order_id or None)
        return self._note(request.client_order_id, _from_order(request, source, limit=limit, at=at))

    async def _send(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
    ) -> httpx.Response:
        key_id = (self._settings.kalshi_api_key_id or "").strip()
        if not key_id or self._private_key is None:
            raise httpx.HTTPError("Kalshi execution credentials are not configured")
        timestamp = str(int(self._clock().timestamp() * 1000))
        sign_path = kalshi_sign_path(self._base_url, path)
        headers = kalshi_auth_headers(
            key_id=key_id,
            private_key=self._private_key,
            timestamp_ms=timestamp,
            method=method,
            sign_path=sign_path,
        )
        return await self._client.request(method, path, headers=headers, json=json_body, params=params)


def kalshi_sign_path(base_url: str, path: str) -> str:
    """Path from the host, without a query string, as Kalshi requires for signing."""

    relative = path.split("?", 1)[0]
    combined = base_url.rstrip("/") + "/" + relative.lstrip("/")
    return urlsplit(combined).path


def kalshi_auth_headers(
    *,
    key_id: str,
    private_key: Any,
    timestamp_ms: str,
    method: str,
    sign_path: str,
) -> dict[str, str]:
    message = f"{timestamp_ms}{method.upper()}{sign_path}".encode()
    signature = _sign_pss(private_key, message)
    return {
        "KALSHI-ACCESS-KEY": key_id,
        "KALSHI-ACCESS-TIMESTAMP": timestamp_ms,
        "KALSHI-ACCESS-SIGNATURE": signature,
    }


def _sign_pss(private_key: Any, message: bytes) -> str:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding

    signature = private_key.sign(
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    return base64.b64encode(signature).decode("ascii")


def _from_order(
    request: VenueOrderRequest,
    order: dict[str, Any],
    *,
    limit: KalshiNativeLimit | None,
    at: datetime,
) -> VenueOrderResult:
    filled = _count(order, "fill_count", "fill_count_fp")
    remaining = _count(order, "remaining_count", "remaining_count_fp")
    initial = _count(order, "initial_count", "initial_count_fp")
    if initial is None and limit is not None:
        initial = limit.contract_count
    if filled is None:
        return _result(
            request,
            status=VenueOrderStatus.FAILED,
            at=at,
            venue_order_id=_text(order.get("order_id")),
        )
    status_name = str(order.get("status") or "").lower()
    if remaining is None and initial is not None:
        remaining = initial - filled
    if status_name in {"executed"} and initial is not None and filled >= initial:
        order_status = VenueOrderStatus.FILLED
    elif status_name in {"resting"} and filled == 0:
        order_status = VenueOrderStatus.OPEN
    elif filled == 0 and (
        status_name in {"canceled", "cancelled"} or remaining == 0
    ):
        order_status = VenueOrderStatus.CANCELLED
    elif filled == 0 and remaining is not None and remaining > 0:
        order_status = VenueOrderStatus.OPEN
    elif initial is not None and filled >= initial and (remaining or 0) == 0:
        order_status = VenueOrderStatus.FILLED
    elif filled > 0:
        order_status = VenueOrderStatus.PARTIAL
    elif status_name in {"resting"}:
        order_status = VenueOrderStatus.OPEN
    else:
        order_status = VenueOrderStatus.FAILED
    average = _average_decimal(order, limit)
    if (
        order_status is VenueOrderStatus.FILLED
        and average is not None
        and average + Decimal("0.0000001") < request.requested_price
    ):
        order_status = VenueOrderStatus.PARTIAL
    filled_size = _currency_filled(request, filled, initial, order_status)
    return _result(
        request,
        status=order_status,
        at=at,
        filled_size=filled_size,
        average_fill_price=average if filled > 0 else None,
        venue_order_id=_text(order.get("order_id")),
        native_filled=filled,
        native_remaining=remaining,
    )


def _currency_filled(
    request: VenueOrderRequest,
    filled: Decimal,
    initial: Decimal | None,
    status: VenueOrderStatus,
) -> Decimal:
    if filled <= 0 or initial is None or initial <= 0:
        return Decimal(0)
    if status is VenueOrderStatus.FILLED and filled >= initial:
        return request.requested_size
    scaled = request.requested_size * filled / initial
    if scaled > request.requested_size:
        return request.requested_size
    return scaled


def _average_decimal(order: dict[str, Any], limit: KalshiNativeLimit | None) -> Decimal | None:
    yes_price = _decimal(order.get("average_fill_price"))
    if yes_price is None:
        cost = _fill_cost(order)
        count = _count(order, "fill_count", "fill_count_fp")
        if cost is None or count is None or count <= 0:
            return None
        unit_cost = cost / count
        if unit_cost <= 0 or unit_cost >= 1:
            return None
        return Decimal(1) / unit_cost
    if limit is None or yes_price <= 0 or yes_price >= 1:
        return None
    try:
        return decimal_odds_from_yes_price(yes_price, limit)
    except TranslationError:
        return None


def _fill_cost(order: dict[str, Any]) -> Decimal | None:
    taker = _decimal(order.get("taker_fill_cost_dollars"))
    maker = _decimal(order.get("maker_fill_cost_dollars"))
    if taker is None and maker is None:
        return None
    return (taker or Decimal(0)) + (maker or Decimal(0))


def _count(order: dict[str, Any], plain: str, fixed: str) -> Decimal | None:
    if fixed in order and order.get(fixed) is not None:
        return _decimal(order.get(fixed))
    return _decimal(order.get(plain))


def _order_payload(body: dict[str, Any] | None) -> dict[str, Any] | None:
    if body is None:
        return None
    order = body.get("order")
    if isinstance(order, dict):
        return order
    if "order_id" in body or "fill_count" in body or "fill_count_fp" in body:
        return body
    return None


def _json_object(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except json.JSONDecodeError:
        return None
    return body if isinstance(body, dict) else None


def _wire_id(client_order_id: str) -> str | None:
    try:
        return kalshi_wire_client_order_id(client_order_id)
    except TranslationError:
        return None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _result(
    request: VenueOrderRequest,
    *,
    status: VenueOrderStatus,
    at: datetime,
    filled_size: Decimal | None = Decimal(0),
    average_fill_price: Decimal | None = None,
    venue_order_id: str | None = None,
    native_filled: Decimal | None = None,
    native_remaining: Decimal | None = None,
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
    )
