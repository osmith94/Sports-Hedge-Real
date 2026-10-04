"""Polymarket V2 execution. No venue writes. Price-2 is not recomputed."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from eth_account import Account

from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.arbitrage.watchlist.economics import classification_for
from sports_hedge.arbitrage.watchlist.models import NearOpportunity, OpportunityStatus
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.models import MarketSide, VenueName
from sports_hedge.execution.clients import (
    DeterministicExecutionTransport,
    MatchbookExecutionClient,
    PolymarketExecutionClient,
)
from sports_hedge.execution.models import VenueOrderRequest, VenueOrderStatus
from sports_hedge.execution.package import LivePackageOutcome, execute_live_package
from sports_hedge.execution.polymarket_geoblock import fetch_geoblock, parse_geoblock
from sports_hedge.execution.polymarket_http import (
    POLYMARKET_OPENING_LAY_NOT_APPROVED,
    PolymarketAmbiguous,
    PolymarketConstraints,
    PolymarketHttpExecutionTransport,
    PolymarketSubmission,
    SdkPolymarketBroker,
)
from sports_hedge.execution.polymarket_preflight import (
    collect_polymarket_preflight,
    format_polymarket_preflight,
)
from sports_hedge.execution.polymarket_sdk import (
    POLYMARKET_CLIENT_VERSION,
    PolymarketClientError,
    open_polymarket_client,
)
from sports_hedge.execution.runtime import ExecutionRuntime
from sports_hedge.execution.translate import (
    POLYMARKET_ORDER_TYPE,
    matchbook_limit_odds,
    matchbook_stake,
    polymarket_native_order,
    polymarket_payoff,
)
from sports_hedge.fees import polymarket as polymarket_fees
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.chain import PaperFillPlan
from sports_hedge.paper.fills import PaperOpportunityLeg
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.paper.trades import OPENING_TRANCHE_ID
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
TOKEN = "9" * 76
SECRET = "not-a-real-polymarket-secret"
PASSPHRASE = "not-a-real-passphrase"


class _Broker:
    def __init__(self, submission: PolymarketSubmission | None = None) -> None:
        self.submission = submission or PolymarketSubmission(
            state="matched",
            order_id="0xorder",
            making_amount=Decimal("2.50"),
            taking_amount=Decimal("5"),
            fee_rate_bps=Decimal("50"),
        )
        self.posts = 0
        self.cancels = 0
        self.constraints_calls = 0
        self.readiness_calls = 0
        self.readiness_spend: Decimal | None = None
        self.raise_on_post: Exception | None = None

    def constraints(self, token_id: str) -> PolymarketConstraints:
        self.constraints_calls += 1
        return PolymarketConstraints(tick_size="0.01", minimum_shares=Decimal("5"), token_id=token_id)

    def readiness(self, spend: Decimal) -> str | None:
        self.readiness_calls += 1
        self.readiness_spend = spend
        return None

    def post_fak(self, order) -> PolymarketSubmission:
        del order
        self.posts += 1
        if self.raise_on_post is not None:
            raise self.raise_on_post
        return self.submission

    def cancel(self, order_id: str) -> str:
        del order_id
        self.cancels += 1
        return "cancelled"

    def account_snapshot(self) -> dict[str, bool | str]:
        return {
            "authenticated_read": True,
            "balance_readable": True,
            "trading_ready": True,
            "open_orders_readable": True,
        }


def _settings(*, execution: bool = True) -> Settings:
    return Settings(
        sports_hedge_mode="real",
        sports_hedge_execution_enabled=execution,
    )


def _request(
    *,
    stake: str = "2.50",
    odds: str = "2",
    runner: str = TOKEN,
    side: MarketSide = MarketSide.BACK,
    currency: str = "USD",
) -> VenueOrderRequest:
    return VenueOrderRequest(
        venue=VenueName.POLYMARKET,
        trade_id="trade-1",
        tranche_id="opening",
        native_event_id="evt-1",
        native_market_id="condition-1",
        native_runner_id=runner,
        side=side,
        currency=currency,
        requested_price=Decimal(odds),
        requested_size=Decimal(stake),
        client_order_id="sh-" + "ab" * 16,
    )


def _transport(
    broker: _Broker | None = None,
    *,
    execution: bool = True,
    eligibility: dict | None = None,
) -> tuple[PolymarketHttpExecutionTransport, _Broker]:
    used = broker or _Broker()
    body = eligibility or {"blocked": False, "country": "AR", "region": "C"}

    async def _geo():
        return parse_geoblock(body)

    return (
        PolymarketHttpExecutionTransport(
            _settings(execution=execution),
            broker=used,
            geoblock=_geo,
            clock=lambda: NOW,
        ),
        used,
    )


def _permitted() -> dict:
    return {"blocked": False, "country": "AR", "region": "C"}


async def test_v2_client_initializes_with_fake_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("client initialization performed HTTP")

    monkeypatch.setattr(httpx.Client, "request", refuse)
    monkeypatch.setattr(
        "polymarket.clients.secure._credentials_are_active_sync",
        lambda **_kwargs: True,
    )
    account = Account.create()
    key_file = tmp_path / "polymarket-private-key"
    key_file.write_text(account.key.hex(), encoding="utf-8")
    settings = Settings(
        polymarket_private_key_path=str(key_file),
        polymarket_signature_type=0,
        polymarket_api_key="fake-key",
        polymarket_api_secret=SECRET,
        polymarket_api_passphrase=PASSPHRASE,
    )
    client = open_polymarket_client(settings, derive_credentials=False)
    try:
        assert client._ctx.wallet_type == "EOA"
        assert POLYMARKET_CLIENT_VERSION == "0.12.0"
        assert SECRET not in repr(client._ctx.credentials)
    finally:
        client.close()


def _wallet_settings(tmp_path: Path) -> Settings:
    account = Account.create()
    key_file = tmp_path / "polymarket-private-key"
    key_file.write_text(account.key.hex(), encoding="utf-8")
    return Settings(
        polymarket_private_key_path=str(key_file),
        polymarket_signature_type=0,
        polymarket_api_key="fake-key",
        polymarket_api_secret=SECRET,
        polymarket_api_passphrase=PASSPHRASE,
    )


def test_unsupported_sdk_shape_does_not_call_create(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from polymarket.clients.secure import SecureClient

    def refuse_create(cls, **_kwargs: object) -> None:
        raise AssertionError("SecureClient.create was called")

    def narrow(cls, *, private_key: str) -> None:
        del cls, private_key
        raise AssertionError("unsupported _create was called")

    monkeypatch.setattr(SecureClient, "create", classmethod(refuse_create))
    monkeypatch.setattr(SecureClient, "_create", classmethod(narrow))
    with pytest.raises(PolymarketClientError, match="signature is unsupported"):
        open_polymarket_client(_wallet_settings(tmp_path), derive_credentials=False)

    monkeypatch.delattr(SecureClient, "_create")
    with pytest.raises(PolymarketClientError, match="_create is unavailable"):
        open_polymarket_client(_wallet_settings(tmp_path), derive_credentials=False)

    monkeypatch.setattr("importlib.metadata.version", lambda _name: "9.9.9")
    with pytest.raises(PolymarketClientError, match="is not 0.12.0"):
        open_polymarket_client(_wallet_settings(tmp_path), derive_credentials=False)


async def test_execution_disabled_does_not_write() -> None:
    transport, broker = _transport(execution=False)
    result = await transport.dispatch(_request())
    assert result.status is VenueOrderStatus.FAILED
    assert result.filled_size == 0
    assert broker.posts == 0
    assert broker.constraints_calls == 0
    assert broker.readiness_calls == 0


async def test_exact_token_id_is_required() -> None:
    transport, broker = _transport()
    result = await transport.dispatch(_request(runner="condition-1:0"))
    assert result.filled_size == 0
    assert broker.posts == 0


async def test_polymarket_opening_lay_is_not_submitted() -> None:
    transport, broker = _transport()
    result = await transport.dispatch(_request(side=MarketSide.LAY))
    assert result.status is VenueOrderStatus.FAILED
    assert result.filled_size == 0
    assert result.note == POLYMARKET_OPENING_LAY_NOT_APPROVED
    assert broker.posts == 0
    assert broker.constraints_calls == 0
    assert broker.readiness_calls == 0
    assert broker.cancels == 0


def test_buy_and_sell_payoffs_match_the_approved_leg() -> None:
    buy = polymarket_native_order(
        native_runner_id=TOKEN,
        native_market_id="condition-1",
        side=MarketSide.BACK,
        decimal_odds=Decimal("2"),
        requested_stake=Decimal("2.50"),
        tick_size="0.01",
        minimum_shares=Decimal("5"),
    )
    assert buy.side.value == "BUY"
    assert buy.price == Decimal("0.50")
    assert buy.order_type == POLYMARKET_ORDER_TYPE
    for wins in (True, False):
        hedge = buy.stake * (buy.decimal_odds - 1) if wins else -buy.stake
        assert polymarket_payoff(buy, outcome_wins=wins) == hedge
    sell = polymarket_native_order(
        native_runner_id=TOKEN,
        native_market_id="condition-1",
        side=MarketSide.LAY,
        decimal_odds=Decimal("2"),
        requested_stake=Decimal("2.50"),
        tick_size="0.01",
        minimum_shares=Decimal("5"),
    )
    assert sell.side.value == "SELL"
    assert sell.token_id == buy.token_id
    for wins in (True, False):
        liability = sell.stake * (sell.decimal_odds - 1)
        hedge = -liability if wins else sell.stake
        assert polymarket_payoff(sell, outcome_wins=wins) == hedge


def test_signed_buy_keeps_the_approved_stake_when_fees_would_shrink_it() -> None:
    from polymarket._internal.actions.orders.market import adjust_buy_amount_for_fees
    from polymarket._internal.actions.orders.market_data import PlatformFeeInfo

    stake = Decimal("10")
    order = polymarket_native_order(
        native_runner_id=TOKEN,
        native_market_id="condition-1",
        side=MarketSide.BACK,
        decimal_odds=Decimal("3.7"),
        requested_stake=stake,
        tick_size="0.01",
        minimum_shares=Decimal("1"),
    )
    shrunk = adjust_buy_amount_for_fees(
        amount=order.amount,
        price=order.price,
        max_spend=order.amount,
        fee=PlatformFeeInfo(rate=Decimal("0.05"), exponent=Decimal(1)),
    )
    assert shrunk < order.amount
    captured: dict[str, object] = {}

    class _Client:
        def create_market_order(self, **kwargs: object) -> object:
            captured.update(kwargs)
            return object()

        def post_order(self, signed: object) -> object:
            del signed
            return type(
                "Response",
                (),
                {
                    "ok": True,
                    "status": "matched",
                    "making_amount": order.amount,
                    "taking_amount": order.shares,
                    "order_id": "0xorder",
                    "trade_ids": (),
                },
            )()

    broker = SdkPolymarketBroker(Settings())
    broker._client = _Client()
    broker.post_fak(order)
    assert captured["amount"] == stake
    assert captured["amount"] == order.amount
    assert captured.get("max_spend") is None
    assert captured["side"] == "BUY"
    assert captured["order_type"] == POLYMARKET_ORDER_TYPE
    assert broker.posts == 1


def _back_package_pnl(matchbook_stake_value: Decimal, matchbook_odds: Decimal, polymarket_stake_value: Decimal, polymarket_odds: Decimal, *, home_wins: bool) -> Decimal:
    matchbook = (
        matchbook_stake_value * (matchbook_odds - 1) if home_wins else -matchbook_stake_value
    )
    polymarket = (
        -polymarket_stake_value
        if home_wins
        else polymarket_stake_value * (polymarket_odds - 1)
    )
    return matchbook + polymarket


def test_non_even_package_payoff_matches_the_approved_stakes() -> None:
    matchbook_odds = Decimal("2.50")
    matchbook_size = Decimal("6")
    polymarket_odds = Decimal("4")
    polymarket_size = Decimal("10")
    sent_odds = matchbook_limit_odds(matchbook_odds, side=MarketSide.BACK)
    sent_stake = matchbook_stake(matchbook_size)
    buy = polymarket_native_order(
        native_runner_id=TOKEN,
        native_market_id="condition-1",
        side=MarketSide.BACK,
        decimal_odds=polymarket_odds,
        requested_stake=polymarket_size,
        tick_size="0.01",
        minimum_shares=Decimal("1"),
    )
    assert sent_odds == matchbook_odds
    assert sent_stake == matchbook_size
    assert buy.amount == polymarket_size
    assert buy.price == Decimal("0.25")
    for home_wins in (True, False):
        approved = _back_package_pnl(
            matchbook_size,
            matchbook_odds,
            polymarket_size,
            polymarket_odds,
            home_wins=home_wins,
        )
        translated = _back_package_pnl(sent_stake, sent_odds, buy.stake, buy.decimal_odds, home_wins=home_wins)
        assert polymarket_payoff(buy, outcome_wins=not home_wins) == (
            -buy.stake if home_wins else buy.stake * (buy.decimal_odds - 1)
        )
        assert translated == approved


def test_tick_rounding_preserves_the_limit_and_never_increases_stake() -> None:
    exact = polymarket_native_order(
        native_runner_id=TOKEN,
        native_market_id="m",
        side=MarketSide.BACK,
        decimal_odds=Decimal("2"),
        requested_stake=Decimal("2.50"),
        tick_size="0.01",
        minimum_shares=Decimal("5"),
    )
    assert exact.price == Decimal("0.50")
    assert exact.decimal_odds >= Decimal("2")
    off = polymarket_native_order(
        native_runner_id=TOKEN,
        native_market_id="m",
        side=MarketSide.BACK,
        decimal_odds=Decimal(1) / Decimal("0.501"),
        requested_stake=Decimal("2.50"),
        tick_size="0.01",
        minimum_shares=Decimal("1"),
    )
    assert off.price == Decimal("0.50")
    assert off.decimal_odds > Decimal(1) / Decimal("0.501")
    assert off.stake <= Decimal("2.50")
    with pytest.raises(ValueError, match="minimum"):
        polymarket_native_order(
            native_runner_id=TOKEN,
            native_market_id="m",
            side=MarketSide.BACK,
            decimal_odds=Decimal("2"),
            requested_stake=Decimal("0.01"),
            tick_size="0.01",
            minimum_shares=Decimal("5"),
        )
    with pytest.raises(ValueError, match="minimum"):
        polymarket_native_order(
            native_runner_id=TOKEN,
            native_market_id="m",
            side=MarketSide.BACK,
            decimal_odds=Decimal("2"),
            requested_stake=Decimal("2.49"),
            tick_size="0.01",
            minimum_shares=Decimal("5"),
        )


async def test_fak_full_partial_and_explicit_zero() -> None:
    full, full_broker = _transport()
    filled = await full.dispatch(_request())
    assert filled.order_type == "FAK"
    assert filled.status is VenueOrderStatus.FILLED
    assert filled.filled_size == Decimal("2.50")
    assert filled.average_fill_price == Decimal("2")
    assert full_broker.posts == 1

    partial_broker = _Broker(
        PolymarketSubmission(
            state="matched",
            order_id="0xpartial",
            making_amount=Decimal("1.00"),
            taking_amount=Decimal("2"),
        )
    )
    partial, _ = _transport(partial_broker)
    partial_result = await partial.dispatch(_request())
    assert partial_result.status is VenueOrderStatus.PARTIAL
    assert partial_result.filled_size == Decimal("1.00")
    assert partial_result.filled_size != partial_result.requested_size

    rejected_broker = _Broker(PolymarketSubmission(state="rejected", code="fak_not_filled"))
    rejected, _ = _transport(rejected_broker)
    rejected_result = await rejected.dispatch(_request())
    assert rejected_result.status is VenueOrderStatus.CANCELLED
    assert rejected_result.filled_size == 0
    assert rejected_result.cancel_result == "venue_fak"


async def test_ambiguous_timeout_does_not_post_again() -> None:
    broker = _Broker()
    broker.raise_on_post = PolymarketAmbiguous("timeout")
    transport, _ = _transport(broker)
    first = await transport.dispatch(_request())
    second = await transport.dispatch(_request())
    assert first.filled_size is None
    assert second.filled_size is None
    assert broker.posts == 1
    assert broker.cancels == 0


async def test_duplicate_attempt_does_not_create_another_order() -> None:
    transport, broker = _transport()
    await transport.dispatch(_request())
    await transport.dispatch(_request())
    assert broker.posts == 1


async def test_resting_remainder_is_cancelled_without_upgrading_the_fill() -> None:
    broker = _Broker(
        PolymarketSubmission(
            state="live",
            order_id="0xlive",
            making_amount=Decimal("1.00"),
            taking_amount=Decimal("2"),
        )
    )
    transport, _ = _transport(broker)
    result = await transport.dispatch(_request())
    assert broker.cancels == 1
    assert result.cancel_result == "cancelled"
    assert result.filled_size == Decimal("1.00")
    assert result.status is VenueOrderStatus.PARTIAL


async def test_geoblock_blocks_and_failures_submit_nothing() -> None:
    blocked, blocked_broker = _transport(eligibility={"blocked": True, "country": "GB", "region": ""})
    blocked_result = await blocked.dispatch(_request())
    assert blocked_result.filled_size == 0
    assert blocked_result.note == "geoblock_blocked"
    assert blocked_result.eligibility is not None
    assert "ip" not in blocked_result.eligibility
    assert blocked_broker.posts == 0

    unavailable, unavailable_broker = _transport(eligibility={"country": "GB"})
    unavailable_result = await unavailable.dispatch(_request())
    assert unavailable_result.filled_size == 0
    assert unavailable_result.note == "geoblock_ambiguous"
    assert unavailable_broker.posts == 0

    allowed, allowed_broker = _transport(eligibility=_permitted())
    allowed_result = await allowed.dispatch(_request())
    assert allowed_result.status is VenueOrderStatus.FILLED
    assert allowed_broker.posts == 1
    assert allowed_result.eligibility is not None
    assert allowed_result.eligibility["country"] == "AR"


async def test_geoblock_http_failure_is_closed() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), trust_env=False)
    try:
        result = await fetch_geoblock("https://polymarket.com/api/geoblock", client=client)
    finally:
        await client.aclose()
    assert result.permitted is False
    assert result.reachable is False


async def test_matchbook_and_polymarket_dispatch_together(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail_fee(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("execution recalculated Polymarket fees")

    monkeypatch.setattr(polymarket_fees, "apply_polymarket_taker", fail_fee)
    matchbook = DeterministicExecutionTransport()
    polymarket_transport, broker = _transport()
    plan = _plan(_matchbook_leg(), _polymarket_leg())
    package = await execute_live_package(
        plan,
        trade_id="trade-1",
        tranche_id=OPENING_TRANCHE_ID,
        settings=_settings(),
        matchbook=MatchbookExecutionClient(matchbook),
        polymarket=PolymarketExecutionClient(polymarket_transport),
    )
    assert package.outcome is LivePackageOutcome.FULLY_FILLED
    assert len(matchbook.calls) == 1
    assert broker.posts == 1
    assert {order.venue for order in package.orders} == {VenueName.MATCHBOOK, VenueName.POLYMARKET}


def test_restart_does_not_resubmit(tmp_path: Path) -> None:
    database = tmp_path / "polymarket.sqlite"
    broker = _Broker()
    ledger = SqlitePaperLedger(database)
    ops = _ops(ledger, broker)
    plan = _plan(_matchbook_leg(), _polymarket_leg())
    ops._plans[plan.opportunity_id] = plan
    ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    assert broker.posts == 1
    ledger._connection.close()

    restarted_broker = _Broker()
    restarted = SqlitePaperLedger(database, auto_seed=False)
    again = _ops(restarted, restarted_broker)
    again._plans[plan.opportunity_id] = plan
    again.capture_opening_fill(plan.opportunity_id, now=NOW)
    assert restarted_broker.posts == 0


def test_reserved_attempt_is_not_resubmitted(tmp_path: Path) -> None:
    broker = _Broker()
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(ledger, broker)
    plan = _plan(_polymarket_leg())
    ops._plans[plan.opportunity_id] = plan
    from sports_hedge.application.paper_operations import paper_trade_id
    from sports_hedge.execution.dispatch import opening_package_id

    trade_id = paper_trade_id(plan.opportunity_id)
    ops._live_attempt_store().reserve(
        {
            "package_id": opening_package_id(trade_id),
            "trade_id": trade_id,
            "tranche_id": OPENING_TRANCHE_ID,
            "opportunity_id": plan.opportunity_id,
            "snapshot_ref": None,
            "snapshot_json": plan.execution_snapshot_json,
            "recovery_json": "{}",
            "created_at": NOW,
        }
    )
    ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    assert broker.posts == 0


class _CollateralClient:
    """Read-only stand-in. Order and approval methods fail the test if called."""

    def __init__(
        self,
        *,
        balance: int = 0,
        allowances: dict[str, int] | None = None,
        approved: bool = True,
        balance_error: Exception | None = None,
        orders_error: Exception | None = None,
        approvals_error: Exception | None = None,
        bind_exchange: bool = True,
    ) -> None:
        self.balance = balance
        self.allowances = {} if allowances is None else allowances
        self.approved = approved
        self.balance_error = balance_error
        self.orders_error = orders_error
        self.approvals_error = approvals_error
        self.balance_calls: list[dict[str, object]] = []
        self.approval_reads = 0
        self.refused: list[str] = []
        self.closed = False
        if bind_exchange:
            from polymarket.environments import PRODUCTION

            self.environment = PRODUCTION

    def get_balance_allowance(self, **kwargs: object) -> object:
        self.balance_calls.append(kwargs)
        if self.balance_error is not None:
            raise self.balance_error
        return type("Balance", (), {"balance": self.balance, "allowances": self.allowances})()

    def get_trading_approvals_state(self) -> object:
        self.approval_reads += 1
        if self.approvals_error is not None:
            raise self.approvals_error
        return type("Approvals", (), {"is_fully_approved": self.approved})()

    def list_open_orders(self) -> object:
        if self.orders_error is not None:
            raise self.orders_error
        return type("Pager", (), {"first_page": lambda self: object()})()

    def close(self) -> None:
        self.closed = True

    def create_market_order(self, **_kwargs: object) -> object:
        self._refuse("create_market_order")

    def place_market_order(self, **_kwargs: object) -> object:
        self._refuse("place_market_order")

    def post_order(self, _signed: object) -> object:
        self._refuse("post_order")

    def cancel_order(self, **_kwargs: object) -> object:
        self._refuse("cancel_order")

    def approve_erc20(self, **_kwargs: object) -> object:
        self._refuse("approve_erc20")

    def approve_erc1155_for_all(self, **_kwargs: object) -> object:
        self._refuse("approve_erc1155_for_all")

    def setup_trading_approvals(self) -> object:
        self._refuse("setup_trading_approvals")

    def _ensure_wallet_ready(self) -> object:
        self._refuse("_ensure_wallet_ready")

    def _deploy_default_deposit_wallet(self) -> object:
        self._refuse("_deploy_default_deposit_wallet")

    def _refuse(self, name: str) -> None:
        self.refused.append(name)
        raise AssertionError(f"{name} was called")


def _require_collateral_string(client: _CollateralClient) -> None:
    assert client.balance_calls == [{"asset_type": "COLLATERAL"}]


def _exchange_v3() -> str:
    from polymarket._internal.environment import PRODUCTION_CONFIG

    return PRODUCTION_CONFIG.exchange_v3


def _other_spender() -> str:
    from polymarket._internal.environment import PRODUCTION_CONFIG

    return PRODUCTION_CONFIG.perps_deposit_contract


async def test_preflight_collateral_read_uses_the_sdk_string(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _CollateralClient(
        balance=2_000_000,
        allowances={_exchange_v3(): 2_000_000, _other_spender(): 1},
    )

    def _open(_settings: Settings, *, derive_credentials: bool) -> _CollateralClient:
        assert derive_credentials is False
        return client

    monkeypatch.setattr(
        "sports_hedge.execution.polymarket_preflight.open_polymarket_client",
        _open,
    )

    async def _geo():
        return parse_geoblock(_permitted())

    report = await collect_polymarket_preflight(
        _wallet_settings(tmp_path),
        geoblock=_geo,
    )
    text = format_polymarket_preflight(report)
    _require_collateral_string(client)
    assert client.closed is True
    assert report["l2_auth_valid"] is True
    assert report["authenticated_read"] is True
    assert report["balance_readable"] is True
    assert report["trading_ready"] is True
    assert report["open_orders_readable"] is True
    assert report["transport_ready"] is True
    assert "L2 auth valid: yes" in text
    assert "authenticated read: PASS" in text
    assert "LIVE ORDER SUBMISSION:" in text
    assert "DISABLED" in text


async def test_preflight_balance_failure_keeps_l2_credentials_valid(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _CollateralClient(balance_error=AttributeError("AssetType.COLLATERAL"))

    monkeypatch.setattr(
        "sports_hedge.execution.polymarket_preflight.open_polymarket_client",
        lambda _settings, *, derive_credentials: client,
    )

    async def _geo():
        return parse_geoblock(_permitted())

    report = await collect_polymarket_preflight(_wallet_settings(tmp_path), geoblock=_geo)
    text = format_polymarket_preflight(report)
    _require_collateral_string(client)
    assert client.closed is True
    assert report["l2_configured"] is True
    assert report["l2_auth_valid"] is True
    assert report["authenticated_read"] is True
    assert report["balance_readable"] is False
    assert report["trading_ready"] is False
    assert report["open_orders_readable"] is True
    assert report["transport_ready"] is False
    assert "L2 auth valid: yes" in text
    assert "authenticated read: PASS" in text
    assert "balance readable: no" in text
    assert "ready: no" in text


async def test_preflight_open_order_failure_stays_distinct_and_fail_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _CollateralClient(
        balance=2_000_000,
        allowances={_exchange_v3(): 2_000_000},
        orders_error=RuntimeError("open orders unavailable"),
        approved=False,
    )
    monkeypatch.setattr(
        "sports_hedge.execution.polymarket_preflight.open_polymarket_client",
        lambda _settings, *, derive_credentials: client,
    )

    async def _geo():
        return parse_geoblock(_permitted())

    report = await collect_polymarket_preflight(_wallet_settings(tmp_path), geoblock=_geo)
    _require_collateral_string(client)
    assert report["authenticated_read"] is True
    assert report["l2_auth_valid"] is True
    assert report["balance_readable"] is True
    assert report["trading_ready"] is True
    assert report["open_orders_readable"] is False
    assert report["transport_ready"] is False
    assert report["platform_wide_approval_diagnostic_not_execution_readiness"] is False
    assert client.closed is True
    assert client.refused == []


def test_live_readiness_uses_collateral_string_and_passes() -> None:
    from polymarket.models.clob import AssetType

    missing = "COLLATERAL"
    with pytest.raises(AttributeError):
        getattr(AssetType, missing)
    client = _CollateralClient(
        balance=1_000_000,
        allowances={_exchange_v3().lower(): 1_000_000, _other_spender(): 9_000_000},
        approvals_error=AssertionError("broad approvals were consulted"),
    )
    broker = SdkPolymarketBroker(Settings())
    broker._client = client
    assert broker.readiness(Decimal(1)) is None
    _require_collateral_string(client)
    assert client.approval_reads == 0
    assert client.refused == []
    assert broker.posts == 0


def test_live_readiness_insufficient_balance_fails_closed() -> None:
    client = _CollateralClient(balance=999_999, allowances={"spender": 5_000_000})
    broker = SdkPolymarketBroker(Settings())
    broker._client = client
    block = broker.readiness(Decimal(1))
    assert block == "POLYMARKET_NOT_READY: collateral balance is below the order"
    _require_collateral_string(client)
    assert broker.posts == 0


def test_live_readiness_insufficient_allowance_fails_closed() -> None:
    client = _CollateralClient(
        balance=5_000_000,
        allowances={_exchange_v3(): 999_999, _other_spender(): 5_000_000},
    )
    broker = SdkPolymarketBroker(Settings())
    broker._client = client
    block = broker.readiness(Decimal(1))
    assert block == "POLYMARKET_NOT_READY: collateral allowance is below the order"
    _require_collateral_string(client)
    assert broker.posts == 0


def test_live_readiness_ignores_other_spenders_and_broad_approvals() -> None:
    client = _CollateralClient(
        balance=5_000_000,
        allowances={_other_spender(): 5_000_000, "0x" + "11" * 20: 5_000_000},
        approved=False,
        approvals_error=AssertionError("broad approvals were consulted"),
    )
    broker = SdkPolymarketBroker(Settings())
    broker._client = client
    block = broker.readiness(Decimal(1))
    assert block == "POLYMARKET_NOT_READY: collateral allowance is below the order"
    assert client.approval_reads == 0
    assert client.refused == []


def test_live_readiness_exact_exchange_v3_allowance_passes() -> None:
    client = _CollateralClient(
        balance=2_500_000,
        allowances={_exchange_v3(): 2_500_000},
        approved=False,
    )
    broker = SdkPolymarketBroker(Settings())
    broker._client = client
    assert broker.readiness(Decimal("2.50")) is None
    assert client.approval_reads == 0


def test_unresolved_exchange_v3_fails_closed_before_balance() -> None:
    client = _CollateralClient(
        balance=5_000_000,
        allowances={_other_spender(): 5_000_000},
        bind_exchange=False,
        approved=True,
    )
    broker = SdkPolymarketBroker(Settings())
    broker._client = client
    block = broker.readiness(Decimal(1))
    assert block == "POLYMARKET_NOT_READY: exchange_v3 spender cannot be resolved"
    assert client.balance_calls == []
    assert client.refused == []


def test_blank_exchange_v3_fails_closed() -> None:
    client = _CollateralClient(balance=5_000_000, allowances={_exchange_v3(): 5_000_000}, bind_exchange=False)
    client._ctx = type("Ctx", (), {"environment_config": type("Cfg", (), {"exchange_v3": "  "})()})()
    broker = SdkPolymarketBroker(Settings())
    broker._client = client
    block = broker.readiness(Decimal(1))
    assert block == "POLYMARKET_NOT_READY: exchange_v3 spender cannot be resolved"


def test_account_snapshot_uses_exchange_v3_not_broad_approval() -> None:
    from sports_hedge.execution.polymarket_buy_readiness import PLATFORM_WIDE_APPROVAL_DIAGNOSTIC

    missing = _CollateralClient(
        balance=2_000_000,
        allowances={_other_spender(): 2_000_000},
        approved=True,
    )
    broker = SdkPolymarketBroker(Settings())
    broker._client = missing
    blocked = broker.account_snapshot()
    assert blocked["authenticated_read"] is True
    assert blocked["balance_readable"] is True
    assert blocked["trading_ready"] is False
    assert blocked["trading_readiness_reason"] == "exchange_v3 collateral allowance is not positive"
    assert blocked[PLATFORM_WIDE_APPROVAL_DIAGNOSTIC] is True

    ready = _CollateralClient(
        balance=1,
        allowances={_exchange_v3(): 1},
        approved=False,
    )
    broker._client = ready
    opened = broker.account_snapshot()
    assert opened["balance_readable"] is True
    assert opened["trading_ready"] is True
    assert "trading_readiness_reason" not in opened
    assert opened[PLATFORM_WIDE_APPROVAL_DIAGNOSTIC] is False
    assert ready.refused == []

    zero = _CollateralClient(
        balance=0,
        allowances={_exchange_v3(): 5_000_000},
        approved=True,
    )
    broker._client = zero
    empty = broker.account_snapshot()
    assert empty["authenticated_read"] is True
    assert empty["balance_readable"] is True
    assert empty["trading_ready"] is False
    assert empty["trading_readiness_reason"] == "collateral balance is not positive"


def test_bound_environment_config_is_the_exchange_v3_authority() -> None:
    """The spender is the client's bound config, not another high allowance."""

    bound = "0x" + "ab" * 20
    client = _CollateralClient(
        balance=1_000_000,
        allowances={bound: 1_000_000, _exchange_v3(): 1},
        bind_exchange=True,
    )
    client._ctx = type(
        "Ctx",
        (),
        {"environment_config": type("Cfg", (), {"exchange_v3": bound})()},
    )()
    broker = SdkPolymarketBroker(Settings())
    broker._client = client
    assert broker.readiness(Decimal(1)) is None

    client.allowances = {_exchange_v3(): 5_000_000, bound: 1}
    assert broker.readiness(Decimal(1)) == "POLYMARKET_NOT_READY: collateral allowance is below the order"


def test_account_snapshot_balance_failure_keeps_authenticated_read() -> None:
    client = _CollateralClient(balance_error=RuntimeError("balance unreadable"))
    broker = SdkPolymarketBroker(Settings())
    broker._client = client
    snapshot = broker.account_snapshot()
    assert snapshot["authenticated_read"] is True
    assert snapshot["balance_readable"] is False
    assert snapshot["trading_ready"] is False
    assert snapshot["open_orders_readable"] is True
    _require_collateral_string(client)
    assert broker.posts == 0


async def test_preflight_transport_ready_ignores_broad_approval_gaps(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sports_hedge.execution.polymarket_buy_readiness import PLATFORM_WIDE_APPROVAL_DIAGNOSTIC

    client = _CollateralClient(
        balance=2_000_000,
        allowances={_exchange_v3(): 1, _other_spender(): 0},
        approved=False,
    )
    monkeypatch.setattr(
        "sports_hedge.execution.polymarket_preflight.open_polymarket_client",
        lambda _settings, *, derive_credentials: client,
    )

    async def _geo():
        return parse_geoblock(_permitted())

    report = await collect_polymarket_preflight(_wallet_settings(tmp_path), geoblock=_geo)
    text = format_polymarket_preflight(report)
    _require_collateral_string(client)
    assert report["authenticated_read"] is True
    assert report["balance_readable"] is True
    assert report["trading_ready"] is True
    assert report["trading_readiness_reason"] is None
    assert report["open_orders_readable"] is True
    assert report["transport_ready"] is True
    assert report[PLATFORM_WIDE_APPROVAL_DIAGNOSTIC] is False
    assert "trading allowance/readiness: READY" in text
    assert "platform-wide approvals (diagnostic, not execution readiness): not fully approved" in text
    assert "Execution transport:" in text
    assert "ready: yes" in text
    assert client.refused == []
    assert client.closed is True


async def test_zero_collateral_balance_is_not_generic_trading_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    client = _CollateralClient(
        balance=0,
        allowances={_exchange_v3(): 5_000_000},
        approved=True,
    )
    monkeypatch.setattr(
        "sports_hedge.execution.polymarket_preflight.open_polymarket_client",
        lambda _settings, *, derive_credentials: client,
    )

    async def _geo():
        return parse_geoblock(_permitted())

    report = await collect_polymarket_preflight(_wallet_settings(tmp_path), geoblock=_geo)
    text = format_polymarket_preflight(report)
    assert report["authenticated_read"] is True
    assert report["balance_readable"] is True
    assert report["trading_ready"] is False
    assert report["trading_readiness_reason"] == "collateral balance is not positive"
    assert report["transport_ready"] is False
    assert "ACTION REQUIRED: collateral balance is not positive" in text
    assert "ready: no" in text
    assert client.refused == []


async def test_broad_approval_failure_does_not_veto_v2_buy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sports_hedge.execution.polymarket_buy_readiness import PLATFORM_WIDE_APPROVAL_DIAGNOSTIC

    async def _geo():
        return parse_geoblock(_permitted())

    for approvals_error, approved, diagnostic in (
        (RuntimeError("approvals unreadable"), True, "unavailable"),
        (None, False, False),
    ):
        client = _CollateralClient(
            balance=2_500_000,
            allowances={_exchange_v3(): 2_500_000, _other_spender(): 0},
            approved=approved,
            approvals_error=approvals_error,
        )

        def _open(
            _settings: Settings,
            *,
            derive_credentials: bool,
            client: _CollateralClient = client,
        ) -> _CollateralClient:
            assert derive_credentials is False
            return client

        monkeypatch.setattr(
            "sports_hedge.execution.polymarket_preflight.open_polymarket_client",
            _open,
        )
        broker = SdkPolymarketBroker(Settings())
        broker._client = client
        assert broker.readiness(Decimal("2.50")) is None
        assert client.approval_reads == 0
        snapshot = broker.account_snapshot()
        assert snapshot["balance_readable"] is True
        assert snapshot["trading_ready"] is True
        assert snapshot[PLATFORM_WIDE_APPROVAL_DIAGNOSTIC] == diagnostic
        report = await collect_polymarket_preflight(_wallet_settings(tmp_path), geoblock=_geo)
        assert report["trading_ready"] is True
        assert report["transport_ready"] is True
        assert report[PLATFORM_WIDE_APPROVAL_DIAGNOSTIC] == diagnostic
        assert client.refused == []


async def test_preflight_names_a_missing_exchange_v3_allowance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from sports_hedge.execution.polymarket_buy_readiness import PLATFORM_WIDE_APPROVAL_DIAGNOSTIC

    client = _CollateralClient(
        balance=2_000_000,
        allowances={_other_spender(): 9_000_000},
        approved=True,
    )
    monkeypatch.setattr(
        "sports_hedge.execution.polymarket_preflight.open_polymarket_client",
        lambda _settings, *, derive_credentials: client,
    )

    async def _geo():
        return parse_geoblock(_permitted())

    report = await collect_polymarket_preflight(_wallet_settings(tmp_path), geoblock=_geo)
    text = format_polymarket_preflight(report)
    assert report["balance_readable"] is True
    assert report["trading_ready"] is False
    assert report["trading_readiness_reason"] == "exchange_v3 collateral allowance is not positive"
    assert report["transport_ready"] is False
    assert report[PLATFORM_WIDE_APPROVAL_DIAGNOSTIC] is True
    assert "ACTION REQUIRED: exchange_v3 collateral allowance is not positive" in text
    assert "ready: no" in text
    assert client.refused == []


async def test_dispatch_readiness_uses_translated_native_stake() -> None:
    broker = _Broker()

    def _readiness(spend: Decimal) -> str:
        broker.readiness_calls += 1
        broker.readiness_spend = spend
        return "POLYMARKET_NOT_READY: stop"

    broker.readiness = _readiness  # type: ignore[method-assign]
    transport, _used = _transport(broker)
    result = await transport.dispatch(_request(stake="2.509", odds="2"))
    native = polymarket_native_order(
        native_runner_id=TOKEN,
        native_market_id="condition-1",
        side=MarketSide.BACK,
        decimal_odds=Decimal("2"),
        requested_stake=Decimal("2.509"),
        tick_size="0.01",
        minimum_shares=Decimal("5"),
    )
    assert native.stake == Decimal("2.50")
    assert broker.readiness_calls == 1
    assert broker.readiness_spend == native.stake
    assert broker.posts == 0
    assert result.note == "POLYMARKET_NOT_READY: stop"


async def test_preflight_is_read_only() -> None:
    broker = _Broker()

    async def _geo():
        return parse_geoblock(_permitted())

    report = await collect_polymarket_preflight(
        _settings(execution=False),
        geoblock=_geo,
        account=broker.account_snapshot,
    )
    text = format_polymarket_preflight(report)
    assert "SPORTS HEDGE — POLYMARKET PREFLIGHT" in text
    assert "Execution enabled: false" in text
    assert "LIVE ORDER SUBMISSION:" in text
    assert "DISABLED" in text
    assert "blocked: no" in text
    assert SECRET not in text
    assert broker.posts == 0
    assert broker.cancels == 0


def _matchbook_leg() -> PaperOpportunityLeg:
    return PaperOpportunityLeg(
        outcome="home",
        venue=VenueName.MATCHBOOK,
        source_market_id="mb-mkt",
        source_runner_id="101",
        source_event_id="evt-mb",
        currency="GBP",
        requested_stake=Decimal("4"),
        displayed_odds=Decimal("2.10"),
    )


def _polymarket_leg() -> PaperOpportunityLeg:
    return PaperOpportunityLeg(
        outcome="away",
        venue=VenueName.POLYMARKET,
        source_market_id="condition-1",
        source_runner_id=TOKEN,
        source_event_id="evt-pm",
        currency="USD",
        requested_stake=Decimal("2.50"),
        displayed_odds=Decimal("2"),
    )


def _plan(*legs: PaperOpportunityLeg) -> PaperFillPlan:
    return PaperFillPlan(
        opportunity_id="watch:mkt",
        canonical_event_id="evt-canonical",
        canonical_market_id="mkt",
        scanned_at=NOW,
        eligible_for_paper_simulation=True,
        settlement_equivalent=True,
        legs=list(legs),
        decision=PaperScanDecision(
            canonical_market_id="mkt",
            canonical_event_id="evt-canonical",
            eligible_for_paper_simulation=True,
            market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=["test"]),
            solver_model="simple_complete_set",
            fill_legs=list(legs),
            scanned_at=NOW,
        ),
        execution_authoritative=True,
        execution_snapshot_json=json.dumps({"snapshot_id": "exec:pm:1", "accepted": True}),
        provenance="live_paper",
    )


def _ops(ledger: SqlitePaperLedger, broker: _Broker) -> PaperOperationsService:
    watchlist = WatchlistService(SqliteWatchlistRepository())
    watchlist.repository.upsert_opportunity(
        NearOpportunity(
            opportunity_id="watch:mkt",
            canonical_event_id="evt-canonical",
            canonical_market_id="mkt",
            status=OpportunityStatus.TRIGGERED,
            classification=classification_for(OpportunityStatus.TRIGGERED),
            is_arbitrage=True,
            first_seen_at=NOW,
            last_seen_at=NOW,
        ),
        force_status=True,
    )
    transport, _ = _transport(broker)
    runtime = ExecutionRuntime(
        matchbook=MatchbookExecutionClient(DeterministicExecutionTransport()),
        polymarket=PolymarketExecutionClient(transport),
    )
    return PaperOperationsService(
        watchlist=watchlist,
        settings=_settings(),
        ledger=ledger,
        execution_runtime=runtime,
    )
