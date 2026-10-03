"""Authenticated Matchbook offer transport. Not a market-data client.

Official semantics used here, from developers.matchbook.com:

- Login is ``POST /bpapi/rest/security/session``. The session token is kept on
  this client only. The read-only ``MatchbookClient`` is not called.
- Submit is ``POST /edge/rest/v2/offers`` with ``odds-type=DECIMAL`` and
  ``exchange-type=back-lay``. Fields are runner-id, side, odds, stake, and
  keep-in-play. Binary win/lose submission is rejected by this adapter because
  Matchbook has stopped supporting binary mode for new offers.
- There is no documented client-order-id idempotency key on submit. ``temp-id``
  appears on some offer payloads but is not a documented submit idempotency
  field, so it is not sent. Retry safety is process-local: a second dispatch
  of a client order id that already has an offer id reads that offer and does
  not POST again. A retry after a lost response, or after process restart,
  can create a second offer. That limitation is intentional.
- Status is ``GET /edge/rest/v2/offers/{offer_id}``.
- Cancel is ``DELETE /edge/rest/v2/offers?offer-ids=<offer_id>``. The session
  token authenticates that request. There is no IOC or FOK.
  ``keep-in-play`` is false, so an unmatched offer is flushed when the event
  goes in play, but a pre-match offer still rests until it is matched,
  cancelled, or flushed. ``dispatch`` reports that resting state as ``open``.
  It does not cancel automatically.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

import httpx

from sports_hedge.config import Settings
from sports_hedge.domain.models import MarketSide, VenueName
from sports_hedge.execution.models import VenueOrderRequest, VenueOrderResult, VenueOrderStatus
from sports_hedge.execution.package import execution_armed
from sports_hedge.execution.translate import TranslationError, matchbook_limit_odds, matchbook_stake
from sports_hedge.venues.matchbook import MATCHBOOK_SESSION_PATH

SUBMIT_PATH = "/edge/rest/v2/offers"


class MatchbookHttpExecutionTransport:
    """One execution session. It does not share tokens with market-data clients."""

    transport_kind = "matchbook_http"
    test_only = False

    def __init__(
        self,
        settings: Settings,
        *,
        client: httpx.AsyncClient | None = None,
        clock: Any = None,
    ) -> None:
        self._settings = settings
        self._clock = clock or (lambda: datetime.now(UTC))
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(
            base_url=settings.matchbook_base_url.rstrip("/"),
            timeout=httpx.Timeout(10.0),
            headers={
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "sports-hedge/0.1 execution",
            },
        )
        self._session_token: str | None = None
        self._offer_ids: dict[str, str] = {}
        self._unknown_submit: set[str] = set()
        self._known_matched: dict[str, Decimal] = {}

    async def aclose(self) -> None:
        if self._owns_client:
            await self._client.aclose()

    async def dispatch(self, request: VenueOrderRequest) -> VenueOrderResult:
        if request.venue is not VenueName.MATCHBOOK:
            raise ValueError("Matchbook execution transport received another venue")
        now = self._clock()
        if not execution_armed(self._settings):
            return _result(request, status=VenueOrderStatus.FAILED, at=now)
        if request.client_order_id in self._offer_ids:
            return await self._read(request, self._offer_ids[request.client_order_id], at=now)
        if request.client_order_id in self._unknown_submit:
            return _result(request, status=VenueOrderStatus.FAILED, at=now)
        try:
            odds = matchbook_limit_odds(request.requested_price, side=request.side)
            stake = matchbook_stake(request.requested_size)
            runner_id = int(request.native_runner_id)
        except (TranslationError, ValueError):
            return _result(request, status=VenueOrderStatus.FAILED, at=now)
        if request.side is MarketSide.BACK:
            native_side = "back"
        elif request.side is MarketSide.LAY:
            native_side = "lay"
        else:
            return _result(request, status=VenueOrderStatus.FAILED, at=now)
        if (request.native_event_id or "").strip() == "":
            return _result(request, status=VenueOrderStatus.FAILED, at=now)
        if request.currency.upper() != self._settings.matchbook_currency:
            return _result(request, status=VenueOrderStatus.FAILED, at=now)
        try:
            await self._login()
        except httpx.HTTPError:
            return _result(request, status=VenueOrderStatus.FAILED, at=self._clock())
        self._unknown_submit.add(request.client_order_id)
        try:
            response = await self._client.post(
                SUBMIT_PATH,
                content=_dumps(
                    {
                        "odds-type": "DECIMAL",
                        "exchange-type": "back-lay",
                        "currency": self._settings.matchbook_currency,
                        "offers": [
                            {
                                "runner-id": runner_id,
                                "side": native_side,
                                "odds": odds,
                                "stake": stake,
                                "keep-in-play": False,
                            }
                        ],
                    }
                ),
            )
        except httpx.HTTPError:
            return _result(request, status=VenueOrderStatus.FAILED, at=self._clock())
        if response.status_code >= 400:
            self._unknown_submit.discard(request.client_order_id)
            status = VenueOrderStatus.REJECTED if response.status_code < 500 else VenueOrderStatus.FAILED
            return _result(request, status=status, at=self._clock())
        offer = _first_offer(response)
        if offer is None:
            return _result(request, status=VenueOrderStatus.FAILED, at=self._clock())
        offer_id = offer.get("id")
        if offer_id is None:
            return _result(request, status=VenueOrderStatus.FAILED, at=self._clock())
        self._offer_ids[request.client_order_id] = str(offer_id)
        self._unknown_submit.discard(request.client_order_id)
        return self._remember_match(request, _from_offer(request, offer, submitted_stake=stake, at=self._clock()))

    async def cancel(self, request: VenueOrderRequest) -> VenueOrderResult:
        now = self._clock()
        if not execution_armed(self._settings):
            return _result(request, status=VenueOrderStatus.FAILED, at=now)
        offer_id = self._offer_ids.get(request.client_order_id)
        if offer_id is None:
            return _result(request, status=VenueOrderStatus.FAILED, at=now)
        try:
            await self._login()
            response = await self._client.delete(SUBMIT_PATH, params={"offer-ids": offer_id})
        except httpx.HTTPError:
            return _result(request, status=VenueOrderStatus.FAILED, at=self._clock(), venue_order_id=offer_id)
        if response.status_code >= 400:
            return _result(request, status=VenueOrderStatus.FAILED, at=self._clock(), venue_order_id=offer_id)
        offer = _first_offer(response) or _object_offer(response)
        if offer is None:
            return await self._read(request, offer_id, at=self._clock())
        parsed = _from_offer(request, offer, submitted_stake=request.requested_size, at=self._clock())
        if parsed.native_filled_quantity is None:
            return self._keep_known_match(request, parsed, offer_id)
        return self._remember_match(request, parsed)

    async def _read(self, request: VenueOrderRequest, offer_id: str, *, at: datetime) -> VenueOrderResult:
        try:
            await self._login()
            response = await self._client.get(f"{SUBMIT_PATH}/{offer_id}")
        except httpx.HTTPError:
            return _result(request, status=VenueOrderStatus.FAILED, at=self._clock(), venue_order_id=offer_id)
        if response.status_code >= 400:
            return _result(request, status=VenueOrderStatus.FAILED, at=self._clock(), venue_order_id=offer_id)
        offer = _first_offer(response) or _object_offer(response)
        if offer is None:
            return _result(request, status=VenueOrderStatus.FAILED, at=at, venue_order_id=offer_id)
        return self._remember_match(
            request,
            _from_offer(request, offer, submitted_stake=request.requested_size, at=self._clock()),
        )

    def _remember_match(self, request: VenueOrderRequest, result: VenueOrderResult) -> VenueOrderResult:
        if result.native_filled_quantity is not None:
            self._known_matched[request.client_order_id] = result.native_filled_quantity
        return result

    def _keep_known_match(
        self,
        request: VenueOrderRequest,
        result: VenueOrderResult,
        offer_id: str,
    ) -> VenueOrderResult:
        known = self._known_matched.get(request.client_order_id)
        if known is None:
            return result.model_copy(update={"filled_size": None, "native_filled_quantity": None})
        status = VenueOrderStatus.PARTIAL if known > 0 else VenueOrderStatus.CANCELLED
        return result.model_copy(
            update={
                "status": status,
                "filled_size": known,
                "native_filled_quantity": known,
                "native_remaining_quantity": Decimal(0),
                "venue_order_id": result.venue_order_id or offer_id,
            }
        )

    async def _login(self) -> None:
        if self._session_token:
            return
        username = (self._settings.matchbook_username or "").strip()
        password = self._settings.matchbook_password or ""
        if not username or not password:
            raise httpx.HTTPError("Matchbook execution credentials are not configured")
        payload: dict[str, str] = {"username": username, "password": password}
        mfa = (self._settings.matchbook_mfa_code or "").strip()
        if mfa:
            payload["mfa-code"] = mfa
        response = await self._client.post(MATCHBOOK_SESSION_PATH, json=payload)
        if response.status_code >= 400:
            raise httpx.HTTPError("Matchbook execution login failed")
        try:
            body = response.json()
        except json.JSONDecodeError as exc:
            raise httpx.HTTPError("Matchbook execution login returned no token") from exc
        token = ""
        if isinstance(body, dict):
            token = str(body.get("session-token") or body.get("session_token") or "")
        if not token:
            token = str(response.cookies.get("session-token") or "")
        if not token:
            raise httpx.HTTPError("Matchbook execution login returned no token")
        self._session_token = token
        self._client.headers["session-token"] = token


def _from_offer(
    request: VenueOrderRequest,
    offer: dict[str, Any],
    *,
    submitted_stake: Decimal,
    at: datetime,
) -> VenueOrderResult:
    status_name = str(offer.get("status") or "").lower()
    stake = _decimal(offer.get("stake"))
    remaining = _decimal(offer.get("remaining"))
    matched = _matched_stake(offer)
    if matched is None and stake is not None and remaining is not None:
        matched = stake - remaining
    if matched is None and status_name in {"cancelled", "flushed"}:
        offer_id = offer.get("id")
        return _result(
            request,
            status=VenueOrderStatus.CANCELLED,
            at=at,
            filled_size=None,
            venue_order_id=None if offer_id is None else str(offer_id),
            native_remaining=remaining,
        )
    matched = matched if matched is not None and matched > 0 else Decimal(0)
    if remaining is None and stake is not None:
        remaining = stake - matched
    average = _average_decimal_odds(offer)
    if status_name == "failed":
        order_status = VenueOrderStatus.FAILED
        matched = Decimal(0)
    elif matched == 0 and status_name in {"cancelled", "flushed"}:
        order_status = VenueOrderStatus.CANCELLED
    elif matched <= 0 and status_name in {"open", "edited", "delayed"}:
        order_status = VenueOrderStatus.OPEN
    elif stake is not None and remaining == 0 and matched > 0 and status_name == "matched":
        order_status = VenueOrderStatus.FILLED
    elif matched > 0:
        order_status = VenueOrderStatus.PARTIAL
    elif status_name in {"cancelled", "flushed"}:
        order_status = VenueOrderStatus.CANCELLED
    else:
        order_status = VenueOrderStatus.FAILED
    filled_size = matched
    if (
        order_status is VenueOrderStatus.FILLED
        and submitted_stake == request.requested_size
        and average is not None
        and average + Decimal("0.0000001") < request.requested_price
    ):
        order_status = VenueOrderStatus.PARTIAL
    offer_id = offer.get("id")
    return _result(
        request,
        status=order_status,
        at=at,
        filled_size=filled_size,
        average_fill_price=average if matched > 0 else None,
        venue_order_id=None if offer_id is None else str(offer_id),
        native_filled=matched if matched > 0 else Decimal(0),
        native_remaining=remaining,
    )


def _matched_stake(offer: dict[str, Any]) -> Decimal | None:
    bets = offer.get("matched-bets")
    if not isinstance(bets, list) or not bets:
        return None
    total = Decimal(0)
    for bet in bets:
        if not isinstance(bet, dict):
            return None
        stake = _decimal(bet.get("stake"))
        if stake is None:
            return None
        total += stake
    return total


def _average_decimal_odds(offer: dict[str, Any]) -> Decimal | None:
    bets = offer.get("matched-bets")
    if isinstance(bets, list) and bets:
        total_stake = Decimal(0)
        weighted = Decimal(0)
        for bet in bets:
            if not isinstance(bet, dict):
                return None
            stake = _decimal(bet.get("stake"))
            odds = _decimal(bet.get("decimal-odds"))
            if odds is None:
                odds = _decimal(bet.get("odds"))
            if stake is None or odds is None or stake <= 0:
                return None
            total_stake += stake
            weighted += stake * odds
        if total_stake <= 0:
            return None
        return weighted / total_stake
    if str(offer.get("status") or "").lower() == "matched":
        return _decimal(offer.get("decimal-odds")) or _decimal(offer.get("odds"))
    return None


def _first_offer(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except json.JSONDecodeError:
        return None
    if not isinstance(body, dict):
        return None
    offers = body.get("offers")
    if not isinstance(offers, list) or not offers or not isinstance(offers[0], dict):
        return None
    return offers[0]


def _object_offer(response: httpx.Response) -> dict[str, Any] | None:
    try:
        body = response.json()
    except json.JSONDecodeError:
        return None
    if isinstance(body, dict) and "id" in body and "offers" not in body:
        return body
    return None


def _decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


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


def _dumps(payload: dict[str, Any]) -> bytes:
    encoded = json.dumps(payload, default=_encode_decimal)
    return encoded.replace('"__DECIMAL__', "").replace('__DECIMAL__"', "").encode("utf-8")


def _encode_decimal(value: Any) -> str:
    if isinstance(value, Decimal):
        return f"__DECIMAL__{format(value, 'f')}__DECIMAL__"
    raise TypeError
