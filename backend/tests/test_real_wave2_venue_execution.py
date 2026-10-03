"""Mocked Matchbook and Kalshi execution transports. No venue is contacted."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from sports_hedge.config import Settings
from sports_hedge.domain.models import MarketSide, VenueName
from sports_hedge.execution.clients import KalshiExecutionClient, MatchbookExecutionClient
from sports_hedge.execution.kalshi_http import (
    KalshiHttpExecutionTransport,
    kalshi_auth_headers,
    kalshi_sign_path,
)
from sports_hedge.execution.matchbook_http import MatchbookHttpExecutionTransport
from sports_hedge.execution.models import LivePackageOutcome, VenueOrderRequest, VenueOrderStatus
from sports_hedge.execution.package import execute_live_package
from sports_hedge.execution.translate import (
    KalshiAction,
    KalshiContractSide,
    TranslationError,
    kalshi_native_limit,
    kalshi_wire_client_order_id,
    matchbook_limit_odds,
    matchbook_stake,
)
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.chain import PaperFillPlan
from sports_hedge.paper.fills import PaperOpportunityLeg
from sports_hedge.paper.models import PaperScanDecision

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
PASSWORD = "matchbook-password-should-not-leak"
KALSHI_ID = "sh-" + ("ab" * 16)
KEY_ID = "kalshi-key-id"


def _armed(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "sports_hedge_mode": "real",
        "sports_hedge_execution_enabled": True,
        "matchbook_username": "mb-user",
        "matchbook_password": PASSWORD,
        "kalshi_api_key_id": KEY_ID,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _request(venue: VenueName, **overrides: object) -> VenueOrderRequest:
    values: dict[str, object] = {
        "venue": venue,
        "trade_id": "trade-1",
        "tranche_id": "tranche-1",
        "native_event_id": "100",
        "native_market_id": "200" if venue is VenueName.MATCHBOOK else "KXTEST",
        "native_runner_id": "401525949430009" if venue is VenueName.MATCHBOOK else "KXTEST:YES",
        "side": MarketSide.BACK,
        "currency": "GBP" if venue is VenueName.MATCHBOOK else "USD",
        "requested_price": Decimal("2.50"),
        "requested_size": Decimal(10),
        "client_order_id": "mb-order-1" if venue is VenueName.MATCHBOOK else KALSHI_ID,
    }
    values.update(overrides)
    return VenueOrderRequest(**values)  # type: ignore[arg-type]


def _plan(*legs: PaperOpportunityLeg) -> PaperFillPlan:
    return PaperFillPlan(
        opportunity_id="opp-1",
        canonical_event_id="evt",
        canonical_market_id="mkt",
        scanned_at=NOW,
        eligible_for_paper_simulation=True,
        settlement_equivalent=True,
        legs=list(legs),
        decision=PaperScanDecision(
            market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=["test"]),
            solver_model="simple_complete_set",
        ),
        execution_authoritative=True,
        execution_snapshot_json='{"accepted":true}',
    )


def _leg(venue: VenueName, *, runner: str, market: str, currency: str, odds: str, stake: str) -> PaperOpportunityLeg:
    return PaperOpportunityLeg(
        outcome="home",
        venue=venue,
        source_event_id="evt-native",
        source_market_id=market,
        source_runner_id=runner,
        currency=currency,
        requested_stake=Decimal(stake),
        displayed_odds=Decimal(odds),
    )


class _FakeKey:
    def sign(self, _message: bytes, _padding: object, _algorithm: object) -> bytes:
        return b"signed-request"


def _client(handler) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://venue.test")


def test_matchbook_back_odds_never_round_down() -> None:
    assert matchbook_limit_odds(Decimal("2.10"), side=MarketSide.BACK) == Decimal("2.10")
    assert matchbook_limit_odds(Decimal("2.51"), side=MarketSide.BACK) == Decimal("2.52")
    assert matchbook_limit_odds(Decimal("2.51"), side=MarketSide.LAY) == Decimal("2.50")
    assert matchbook_stake(Decimal(10)) == Decimal("10.00")
    assert matchbook_stake(Decimal("10.009")) == Decimal("10.00")
    with pytest.raises(TranslationError):
        matchbook_limit_odds(Decimal("0.5"), side=MarketSide.BACK)
    with pytest.raises(TranslationError):
        matchbook_stake(Decimal("0.015"))


def test_kalshi_price_size_and_side_mapping() -> None:
    yes_buy = kalshi_native_limit(
        decimal_odds=Decimal("2.50"),
        requested_stake=Decimal(10),
        contract_side=KalshiContractSide.YES,
        action=KalshiAction.BUY,
    )
    assert yes_buy.book_side == "bid"
    assert yes_buy.yes_price == Decimal("0.40")
    assert yes_buy.contract_count == Decimal("25.00")
    assert Decimal(1) / yes_buy.unit_cost == Decimal("2.5")

    off_tick = kalshi_native_limit(
        decimal_odds=Decimal("2.52"),
        requested_stake=Decimal(10),
        contract_side=KalshiContractSide.YES,
        action=KalshiAction.BUY,
    )
    assert off_tick.yes_price == Decimal("0.39")
    assert Decimal(1) / off_tick.unit_cost > Decimal("2.52")

    no_buy = kalshi_native_limit(
        decimal_odds=Decimal("2.50"),
        requested_stake=Decimal(10),
        contract_side=KalshiContractSide.NO,
        action=KalshiAction.BUY,
    )
    assert no_buy.book_side == "ask"
    assert no_buy.yes_price == Decimal("0.60")
    assert no_buy.unit_cost == Decimal("0.40")

    yes_sell = kalshi_native_limit(
        decimal_odds=Decimal("2.52"),
        requested_stake=Decimal(10),
        contract_side=KalshiContractSide.YES,
        action=KalshiAction.SELL,
    )
    assert yes_sell.book_side == "ask"
    assert yes_sell.yes_price == Decimal("0.40")

    no_sell = kalshi_native_limit(
        decimal_odds=Decimal("2.52"),
        requested_stake=Decimal(10),
        contract_side=KalshiContractSide.NO,
        action=KalshiAction.SELL,
    )
    assert no_sell.book_side == "bid"
    assert no_sell.yes_price == Decimal("0.60")
    assert kalshi_wire_client_order_id(KALSHI_ID) == "abababab-abab-abab-abab-abababababab"
    with pytest.raises(TranslationError):
        kalshi_native_limit(
            decimal_odds=Decimal("2.50"),
            requested_stake=Decimal("0.001"),
            contract_side=KalshiContractSide.YES,
            action=KalshiAction.BUY,
        )


def test_kalshi_signature_covers_method_and_path_without_the_query() -> None:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    path = kalshi_sign_path(
        "https://external-api.kalshi.com/trade-api/v2",
        "/portfolio/orders?limit=5",
    )
    assert path == "/trade-api/v2/portfolio/orders"
    headers = kalshi_auth_headers(
        key_id=KEY_ID,
        private_key=key,
        timestamp_ms="1703123456789",
        method="GET",
        sign_path=path,
    )
    message = b"1703123456789GET/trade-api/v2/portfolio/orders"
    key.public_key().verify(
        base64_decode(headers["KALSHI-ACCESS-SIGNATURE"]),
        message,
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
    pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    assert pem not in headers["KALSHI-ACCESS-SIGNATURE"]
    assert KEY_ID == headers["KALSHI-ACCESS-KEY"]


def base64_decode(value: str) -> bytes:
    import base64

    return base64.b64decode(value)


@pytest.mark.asyncio
async def test_matchbook_submit_reads_status_and_does_not_leak_the_password() -> None:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path.endswith("/security/session"):
            return httpx.Response(200, json={"session-token": "session-secret"})
        body = json.loads(request.content.decode())
        assert body["odds-type"] == "DECIMAL"
        offer = body["offers"][0]
        assert offer == {
            "runner-id": 401525949430009,
            "side": "back",
            "odds": 2.52,
            "stake": 10.0,
            "keep-in-play": False,
        }
        assert PASSWORD not in request.content.decode()
        return httpx.Response(
            200,
            json={
                "offers": [
                    {
                        "id": 99,
                        "status": "open",
                        "stake": 10,
                        "remaining": 10,
                        "decimal-odds": 2.52,
                    }
                ]
            },
        )

    transport = MatchbookHttpExecutionTransport(_armed(), client=_client(handler), clock=lambda: NOW)
    result = await transport.dispatch(_request(VenueName.MATCHBOOK, requested_price=Decimal("2.51")))
    assert result.status is VenueOrderStatus.OPEN
    assert result.filled_size == 0
    assert result.average_fill_price is None
    assert result.venue_order_id == "99"
    assert result.native_remaining_quantity == Decimal(10)
    assert PASSWORD not in str(result)
    assert "session-secret" not in str(result)
    assert seen[0].url.path.endswith("/security/session")
    assert seen[1].method == "POST"


@pytest.mark.asyncio
async def test_matchbook_full_partial_failed_and_idempotent_reread() -> None:
    calls = {"posts": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/security/session"):
            return httpx.Response(200, json={"session-token": "tok"})
        if request.method == "POST":
            calls["posts"] += 1
            return httpx.Response(
                200,
                json={
                    "offers": [
                        {
                            "id": 7,
                            "status": "matched",
                            "stake": 10,
                            "remaining": 0,
                            "matched-bets": [
                                {"stake": 6, "decimal-odds": 2.6},
                                {"stake": 4, "decimal-odds": 2.5},
                            ],
                        }
                    ]
                },
            )
        return httpx.Response(
            200,
            json={
                "offers": [
                    {
                        "id": 7,
                        "status": "open",
                        "stake": 10,
                        "remaining": 4,
                        "matched-bets": [{"stake": 6, "decimal-odds": 2.6}],
                    }
                ]
            },
        )

    transport = MatchbookHttpExecutionTransport(_armed(), client=_client(handler), clock=lambda: NOW)
    request = _request(VenueName.MATCHBOOK)
    full = await transport.dispatch(request)
    assert full.status is VenueOrderStatus.FILLED
    assert full.filled_size == Decimal(10)
    assert full.average_fill_price == Decimal("2.56")
    again = await transport.dispatch(request)
    assert calls["posts"] == 1
    assert again.status is VenueOrderStatus.PARTIAL
    assert again.filled_size == Decimal(6)
    assert again.native_remaining_quantity == Decimal(4)


@pytest.mark.asyncio
async def test_matchbook_rejection_malformed_response_and_cancel() -> None:
    mode = {"kind": "reject"}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/security/session"):
            return httpx.Response(200, json={"session-token": "tok"})
        if mode["kind"] == "reject":
            return httpx.Response(400, json={"errors": [{"messages": [PASSWORD]}]})
        if mode["kind"] == "malformed":
            return httpx.Response(200, content=b"{}")
        if mode["kind"] == "failed":
            return httpx.Response(200, json={"offers": [{"id": 3, "status": "failed", "stake": 10}]})
        if request.method == "DELETE":
            assert request.url.path == "/edge/rest/v2/offers"
            assert request.url.params["offer-ids"] == "3"
            assert request.headers["session-token"] == "tok"
            return httpx.Response(
                200,
                json={"offers": [{"id": 3, "status": "cancelled", "stake": 10, "remaining": 10}]},
            )
        return httpx.Response(
            200,
            json={"offers": [{"id": 3, "status": "open", "stake": 10, "remaining": 10}]},
        )

    transport = MatchbookHttpExecutionTransport(_armed(), client=_client(handler), clock=lambda: NOW)
    request = _request(VenueName.MATCHBOOK)
    rejected = await transport.dispatch(request)
    assert rejected.status is VenueOrderStatus.REJECTED
    assert PASSWORD not in str(rejected)
    mode["kind"] = "malformed"
    assert (await transport.dispatch(request)).status is VenueOrderStatus.FAILED
    mode["kind"] = "failed"
    failed = await transport.dispatch(_request(VenueName.MATCHBOOK, client_order_id="other"))
    assert failed.status is VenueOrderStatus.FAILED
    assert failed.filled_size == 0
    mode["kind"] = "open"
    opened = await transport.dispatch(_request(VenueName.MATCHBOOK, client_order_id="live"))
    assert opened.status is VenueOrderStatus.OPEN
    cancelled = await transport.cancel(_request(VenueName.MATCHBOOK, client_order_id="live"))
    assert cancelled.status is VenueOrderStatus.CANCELLED
    assert cancelled.venue_order_id == "3"


@pytest.mark.asyncio
async def test_kalshi_order_lifecycle_uses_yes_price_and_client_order_id() -> None:
    posts: list[dict[str, object]] = []
    deletes = {"count": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        assert KEY_ID == request.headers["KALSHI-ACCESS-KEY"]
        assert "BEGIN PRIVATE" not in request.content.decode()
        if request.method == "POST":
            posts.append(json.loads(request.content.decode()))
            return httpx.Response(
                201,
                json={
                    "order_id": "ord-1",
                    "client_order_id": posts[-1]["client_order_id"],
                    "fill_count": "25.00",
                    "remaining_count": "0.00",
                    "average_fill_price": "0.4000",
                },
            )
        if request.method == "DELETE":
            deletes["count"] += 1
            assert request.url.path == "/portfolio/events/orders/ord-1"
            assert request.url.params["market_ticker"] == "KXTEST"
            assert "exchange_index" not in request.url.params
            signed = kalshi_sign_path(
                "https://external-api.kalshi.com/trade-api/v2",
                f"{request.url.path}?{request.url.query.decode()}",
            )
            assert signed == "/trade-api/v2/portfolio/events/orders/ord-1"
            return httpx.Response(
                200,
                json={
                    "order_id": "ord-1",
                    "client_order_id": "abababab-abab-abab-abab-abababababab",
                    "reduced_by": "25.00",
                    "ts_ms": 1715793660456,
                },
            )
        return httpx.Response(
            200,
            json={
                "order": {
                    "order_id": "ord-1",
                    "status": "resting",
                    "fill_count_fp": "0.00",
                    "remaining_count_fp": "25.00",
                    "initial_count_fp": "25.00",
                }
            },
        )

    transport = KalshiHttpExecutionTransport(
        _armed(),
        client=_client(handler),
        clock=lambda: NOW,
        private_key=_FakeKey(),
    )
    request = _request(VenueName.KALSHI)
    filled = await transport.dispatch(request)
    assert posts[0]["side"] == "bid"
    assert posts[0]["price"] == "0.4000"
    assert posts[0]["count"] == "25.00"
    assert posts[0]["time_in_force"] == "immediate_or_cancel"
    assert posts[0]["client_order_id"] == "abababab-abab-abab-abab-abababababab"
    assert filled.status is VenueOrderStatus.FILLED
    assert filled.filled_size == Decimal(10)
    assert filled.average_fill_price == Decimal("2.5")
    assert filled.native_filled_quantity == Decimal("25.00")
    reread = await transport.dispatch(request)
    assert reread.status is VenueOrderStatus.OPEN
    assert reread.filled_size == 0
    assert len(posts) == 1
    cancelled = await transport.cancel(request)
    assert cancelled.status is VenueOrderStatus.CANCELLED
    without_ticker = await transport.cancel(
        _request(VenueName.KALSHI, native_runner_id="KXTEST", native_market_id="KXTEST")
    )
    assert without_ticker.status is VenueOrderStatus.FAILED
    assert deletes["count"] == 1


@pytest.mark.asyncio
async def test_cancel_does_not_erase_a_partial_fill() -> None:
    def matchbook_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/security/session"):
            return httpx.Response(200, json={"session-token": "tok"})
        if request.method == "DELETE":
            assert request.url.params["offer-ids"] == "3"
            return httpx.Response(200, json={"offers": [{"id": 3, "status": "cancelled"}]})
        return httpx.Response(
            200,
            json={
                "offers": [
                    {
                        "id": 3,
                        "status": "open",
                        "stake": 10,
                        "remaining": 6,
                        "matched-bets": [{"stake": 4, "decimal-odds": 2.5}],
                    }
                ]
            },
        )

    matchbook = MatchbookHttpExecutionTransport(_armed(), client=_client(matchbook_handler), clock=lambda: NOW)
    request = _request(VenueName.MATCHBOOK)
    opened = await matchbook.dispatch(request)
    assert opened.filled_size == Decimal(4)
    cancelled = await matchbook.cancel(request)
    assert cancelled.filled_size == Decimal(4)
    assert cancelled.native_filled_quantity == Decimal(4)
    assert cancelled.status is VenueOrderStatus.PARTIAL

    def kalshi_handler(request: httpx.Request) -> httpx.Response:
        if request.method == "POST":
            return httpx.Response(
                201,
                json={
                    "order_id": "ord-partial",
                    "fill_count": "10.00",
                    "remaining_count": "15.00",
                    "initial_count": "25.00",
                    "average_fill_price": "0.4000",
                },
            )
        if request.method == "DELETE":
            assert request.url.path == "/portfolio/events/orders/ord-partial"
            assert request.url.params["market_ticker"] == "KXTEST"
            return httpx.Response(
                200,
                json={"order_id": "ord-partial", "reduced_by": "15.00", "ts_ms": 1715793660456},
            )
        assert request.url.path == "/portfolio/orders/ord-partial"
        return httpx.Response(
            200,
            json={
                "order": {
                    "order_id": "ord-partial",
                    "status": "canceled",
                    "fill_count_fp": "10.00",
                    "remaining_count_fp": "0.00",
                    "initial_count_fp": "25.00",
                    "taker_fill_cost_dollars": "4.00",
                    "maker_fill_cost_dollars": "0.00",
                }
            },
        )

    kalshi = KalshiHttpExecutionTransport(
        _armed(),
        client=_client(kalshi_handler),
        clock=lambda: NOW,
        private_key=_FakeKey(),
    )
    kalshi_request = _request(VenueName.KALSHI)
    partial = await kalshi.dispatch(kalshi_request)
    assert partial.filled_size == Decimal(4)
    after_cancel = await kalshi.cancel(kalshi_request)
    assert after_cancel.filled_size == Decimal(4)
    assert after_cancel.native_filled_quantity == Decimal("10.00")
    assert after_cancel.native_remaining_quantity == Decimal(0)
    assert after_cancel.status is VenueOrderStatus.PARTIAL


@pytest.mark.asyncio
async def test_kalshi_partial_conflict_and_malformed_payloads() -> None:
    stage = {"value": "partial"}

    def handler(request: httpx.Request) -> httpx.Response:
        if stage["value"] == "partial":
            return httpx.Response(
                201,
                json={
                    "order_id": "ord-2",
                    "fill_count": "10.00",
                    "remaining_count": "0.00",
                    "initial_count": "25.00",
                    "average_fill_price": "0.3900",
                },
            )
        if stage["value"] == "conflict":
            if request.method == "POST":
                return httpx.Response(409, json={"error": "duplicate"})
            body = json.loads(request.content.decode() or b"{}") if request.content else {}
            assert body == {}
            return httpx.Response(
                200,
                json={
                    "orders": [
                        {
                            "order_id": "ord-existing",
                            "client_order_id": "cdcdcdcd-cdcd-cdcd-cdcd-cdcdcdcdcdcd",
                            "status": "executed",
                            "fill_count_fp": "25.00",
                            "remaining_count_fp": "0.00",
                            "initial_count_fp": "25.00",
                            "taker_fill_cost_dollars": "10.00",
                            "maker_fill_cost_dollars": "0.00",
                        }
                    ]
                },
            )
        if stage["value"] == "bad":
            return httpx.Response(201, content=b"not-json")
        return httpx.Response(400, json={"error": KEY_ID})

    transport = KalshiHttpExecutionTransport(
        _armed(),
        client=_client(handler),
        clock=lambda: NOW,
        private_key=_FakeKey(),
    )
    partial = await transport.dispatch(
        _request(VenueName.KALSHI, requested_price=Decimal("2.52"), client_order_id=KALSHI_ID)
    )
    assert partial.status is VenueOrderStatus.PARTIAL
    assert partial.native_filled_quantity == Decimal("10.00")
    assert partial.average_fill_price == Decimal(1) / Decimal("0.39")
    assert KEY_ID not in str(partial)

    stage["value"] = "conflict"
    conflict = await transport.dispatch(_request(VenueName.KALSHI, client_order_id="sh-" + ("cd" * 16)))
    assert conflict.status is VenueOrderStatus.FILLED
    assert conflict.venue_order_id == "ord-existing"
    assert conflict.average_fill_price == Decimal("2.5")

    stage["value"] = "bad"
    malformed = await transport.dispatch(_request(VenueName.KALSHI, client_order_id="sh-" + ("ef" * 16)))
    assert malformed.status is VenueOrderStatus.FAILED
    stage["value"] = "reject"
    rejected = await transport.dispatch(_request(VenueName.KALSHI, client_order_id="sh-" + ("11" * 16)))
    assert rejected.status is VenueOrderStatus.REJECTED
    assert KEY_ID not in str(rejected)


@pytest.mark.asyncio
async def test_no_buy_request_is_an_ask_at_the_complementary_yes_price() -> None:
    captured: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured.update(json.loads(request.content.decode()))
        return httpx.Response(
            201,
            json={"order_id": "ord-no", "fill_count": "0.00", "remaining_count": "0.00"},
        )

    transport = KalshiHttpExecutionTransport(
        _armed(),
        client=_client(handler),
        clock=lambda: NOW,
        private_key=_FakeKey(),
    )
    result = await transport.dispatch(
        _request(VenueName.KALSHI, native_runner_id="KXTEST:NO", native_market_id="KXTEST")
    )
    assert captured["side"] == "ask"
    assert captured["price"] == "0.6000"
    assert result.status is VenueOrderStatus.CANCELLED
    assert result.filled_size == 0


@pytest.mark.asyncio
async def test_live_package_accepts_real_transports_and_keeps_scanner_on_paper() -> None:
    entered = 0
    release = asyncio.Event()

    async def gated(request: httpx.Request) -> httpx.Response:
        nonlocal entered
        if request.url.path.endswith("/security/session"):
            return httpx.Response(200, json={"session-token": "tok"})
        entered += 1
        if entered >= 2:
            release.set()
        await release.wait()
        if request.url.path.endswith("/offers"):
            return httpx.Response(
                200,
                json={
                    "offers": [
                        {
                            "id": 1,
                            "status": "matched",
                            "stake": 4,
                            "remaining": 0,
                            "decimal-odds": 2.1,
                            "matched-bets": [{"stake": 4, "decimal-odds": 2.1}],
                        }
                    ]
                },
            )
        return httpx.Response(
            201,
            json={
                "order_id": "kx-1",
                "fill_count": "8.40",
                "remaining_count": "0.00",
                "average_fill_price": "0.5000",
            },
        )

    matchbook = MatchbookExecutionClient(
        MatchbookHttpExecutionTransport(_armed(), client=_client(gated), clock=lambda: NOW)
    )
    kalshi = KalshiExecutionClient(
        KalshiHttpExecutionTransport(
            _armed(),
            client=_client(gated),
            clock=lambda: NOW,
            private_key=_FakeKey(),
        )
    )
    plan = _plan(
        _leg(
            VenueName.MATCHBOOK,
            runner="55",
            market="66",
            currency="GBP",
            odds="2.10",
            stake="4",
        ),
        _leg(
            VenueName.KALSHI,
            runner="KXTEST:YES",
            market="KXTEST",
            currency="USD",
            odds="2.00",
            stake="4.20",
        ),
    )
    result = await execute_live_package(
        plan,
        trade_id="trade-1",
        tranche_id="tranche-1",
        settings=_armed(),
        matchbook=matchbook,
        kalshi=kalshi,
    )
    assert entered == 2
    assert result.outcome is LivePackageOutcome.FULLY_FILLED

    async def partial_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/security/session"):
            return httpx.Response(200, json={"session-token": "tok"})
        if "offers" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "offers": [
                        {
                            "id": 2,
                            "status": "matched",
                            "stake": 4,
                            "remaining": 0,
                            "matched-bets": [{"stake": 4, "decimal-odds": 2.1}],
                        }
                    ]
                },
            )
        return httpx.Response(
            201,
            json={"order_id": "kx-2", "fill_count": "4.20", "remaining_count": "0.00", "average_fill_price": "0.5000"},
        )

    partial = await execute_live_package(
        plan,
        trade_id="trade-2",
        tranche_id="tranche-1",
        settings=_armed(),
        matchbook=MatchbookExecutionClient(
            MatchbookHttpExecutionTransport(_armed(), client=_client(partial_handler), clock=lambda: NOW)
        ),
        kalshi=KalshiExecutionClient(
            KalshiHttpExecutionTransport(
                _armed(), client=_client(partial_handler), clock=lambda: NOW, private_key=_FakeKey()
            )
        ),
    )
    assert partial.outcome is LivePackageOutcome.PARTIAL

    async def failed_handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/security/session"):
            return httpx.Response(200, json={"session-token": "tok"})
        if "offers" in request.url.path:
            return httpx.Response(400, json={"errors": []})
        return httpx.Response(400, json={"error": "no"})

    failed = await execute_live_package(
        plan,
        trade_id="trade-3",
        tranche_id="tranche-1",
        settings=_armed(),
        matchbook=MatchbookExecutionClient(
            MatchbookHttpExecutionTransport(_armed(), client=_client(failed_handler), clock=lambda: NOW)
        ),
        kalshi=KalshiExecutionClient(
            KalshiHttpExecutionTransport(
                _armed(), client=_client(failed_handler), clock=lambda: NOW, private_key=_FakeKey()
            )
        ),
    )
    assert failed.outcome is LivePackageOutcome.FAILED
    assert all(order.filled_size == 0 for order in failed.orders)

    async def one_sided(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/security/session"):
            return httpx.Response(200, json={"session-token": "tok"})
        if "offers" in request.url.path:
            return httpx.Response(
                200,
                json={
                    "offers": [
                        {
                            "id": 4,
                            "status": "matched",
                            "stake": 4,
                            "remaining": 0,
                            "matched-bets": [{"stake": 4, "decimal-odds": 2.1}],
                        }
                    ]
                },
            )
        return httpx.Response(500, json={"error": "down"})

    one_sided_result = await execute_live_package(
        plan,
        trade_id="trade-5",
        tranche_id="tranche-1",
        settings=_armed(),
        matchbook=MatchbookExecutionClient(
            MatchbookHttpExecutionTransport(_armed(), client=_client(one_sided), clock=lambda: NOW)
        ),
        kalshi=KalshiExecutionClient(
            KalshiHttpExecutionTransport(
                _armed(), client=_client(one_sided), clock=lambda: NOW, private_key=_FakeKey()
            )
        ),
    )
    assert one_sided_result.outcome is LivePackageOutcome.PARTIAL
    assert one_sided_result.orders[0].status is VenueOrderStatus.FILLED
    assert one_sided_result.orders[1].filled_size == 0

    disabled = await execute_live_package(
        plan,
        trade_id="trade-4",
        tranche_id="tranche-1",
        settings=Settings(),
        matchbook=matchbook,
        kalshi=kalshi,
    )
    assert disabled.detail == "execution_disabled"
