"""Price-2 is the only pre-trade authority. Execution submits the frozen package.

Fixture books and scripted account reads. No live orders. Execution stays disabled
on the default settings. Call counts are the timing evidence: the removed
Polymarket reads are asserted at zero, not with invented milliseconds.
"""

from __future__ import annotations

import inspect
import json
from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from sports_hedge.application.execution_reprice import (
    ExecutionRepriceDiagnostics,
    ExecutionRepriceResult,
)
from sports_hedge.application.execution_snapshot import (
    ExecutionSnapshot,
    FrozenNativeOrder,
    VenueReadinessEvidence,
)
from sports_hedge.application.price_engine import CataloguePriceEngine
from sports_hedge.application.venue_capital import (
    MATCHBOOK_ORDER_UNPROVEN,
    POLYMARKET_CONSTRAINTS_UNPROVEN,
    VENUE_CAPITAL_INSUFFICIENT,
    VENUE_CAPITAL_UNPROVEN,
    judge_real_package,
)
from sports_hedge.config import Settings
from sports_hedge.domain.models import MarketSide, VenueName
from sports_hedge.execution.clients import MatchbookExecutionClient, PolymarketExecutionClient
from sports_hedge.execution.frozen import REMOVED_POLYMARKET_EXECUTION_READS, freeze_native_orders
from sports_hedge.execution.matchbook_http import (
    BALANCE_PATH,
    SUBMIT_PATH,
    MatchbookHttpExecutionTransport,
)
from sports_hedge.execution.models import VenueOrderRequest
from sports_hedge.execution.package import LivePackageOutcome, execute_live_package
from sports_hedge.execution.polymarket_http import (
    PolymarketHttpExecutionTransport,
    PolymarketSubmission,
)
from sports_hedge.execution.translate import POLYMARKET_ORDER_TYPE
from sports_hedge.fees import polymarket as polymarket_fees
from sports_hedge.paper.chain import PaperFillPlan
from sports_hedge.paper.fills import PaperOpportunityLeg
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.paper.trades import OPENING_TRANCHE_ID
from sports_hedge.venues.matchbook import MATCHBOOK_SESSION_PATH

NOW = datetime(2026, 10, 4, 12, tzinfo=UTC)
TOKEN = "8" * 76


def _leg(
    venue: VenueName,
    *,
    runner: str,
    market: str,
    event: str,
    stake: str,
    odds: str,
    currency: str,
) -> PaperOpportunityLeg:
    return PaperOpportunityLeg(
        outcome="home" if venue is VenueName.MATCHBOOK else "away",
        venue=venue,
        source_market_id=market,
        source_runner_id=runner,
        source_event_id=event,
        currency=currency,
        requested_stake=Decimal(stake),
        displayed_odds=Decimal(odds),
    )


def _decision(*legs: PaperOpportunityLeg) -> PaperScanDecision:
    return PaperScanDecision(
        canonical_market_id="mkt",
        canonical_event_id="evt",
        eligible_for_paper_simulation=True,
        market_match={"matched": True, "confidence": 1.0, "reasons": ["test"]},
        solver_model="simple_complete_set",
        fill_legs=list(legs),
        scanned_at=NOW,
    )


def _pm_leg() -> PaperOpportunityLeg:
    return _leg(
        VenueName.POLYMARKET,
        runner=TOKEN,
        market="condition-1",
        event="evt-pm",
        stake="2.50",
        odds="2",
        currency="USD",
    )


def _mb_leg() -> PaperOpportunityLeg:
    return _leg(
        VenueName.MATCHBOOK,
        runner="101",
        market="mb-mkt",
        event="evt-mb",
        stake="4",
        odds="2.09",
        currency="GBP",
    )


def test_default_execution_remains_disabled() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False


def test_accepted_polymarket_package_contains_the_native_order() -> None:
    frozen = freeze_native_orders(
        _decision(_pm_leg()),
        polymarket_books={TOKEN: {"tick_size": "0.01", "min_order_size": "5", "asks": []}},
    )
    assert len(frozen) == 1
    order = frozen[0]
    assert order.venue == "polymarket"
    assert order.native_runner_id == TOKEN
    assert order.order_type == POLYMARKET_ORDER_TYPE
    assert order.tick_size == "0.01"
    assert order.minimum_order_size == "5"
    assert order.limit_price == "0.5"
    assert order.native_amount == "2.50"
    assert Decimal(order.native_shares) == Decimal(5)
    assert "GET /api/geoblock" in REMOVED_POLYMARKET_EXECUTION_READS


def test_missing_polymarket_book_constraints_do_not_invent_an_order() -> None:
    frozen = freeze_native_orders(
        _decision(_pm_leg()),
        polymarket_books={TOKEN: {"asks": [{"price": "0.5", "size": "100"}]}},
    )
    assert frozen == ()


def test_real_package_rejects_unproven_or_short_polymarket_capital() -> None:
    leg = _pm_leg()
    frozen = freeze_native_orders(
        _decision(leg),
        polymarket_books={TOKEN: {"tick_size": "0.01", "min_order_size": "1"}},
    )
    missing, _ = judge_real_package(mode="real", legs=[leg], frozen_orders=frozen, readiness=())
    assert missing == VENUE_CAPITAL_UNPROVEN
    short, bound = judge_real_package(
        mode="real",
        legs=[leg],
        frozen_orders=frozen,
        readiness=(
            VenueReadinessEvidence(
                venue="polymarket",
                currency="USD",
                proven=True,
                spendable="2.50",
                allowance="1.00",
                required_stake=None,
                source="polymarket_collateral",
            ),
        ),
    )
    assert short == VENUE_CAPITAL_INSUFFICIENT
    assert bound[0].required_stake == "2.50"
    unfrozen, _ = judge_real_package(mode="real", legs=[leg], frozen_orders=(), readiness=())
    assert unfrozen == POLYMARKET_CONSTRAINTS_UNPROVEN


def test_paper_mode_does_not_require_live_capital() -> None:
    leg = _pm_leg()
    block, _ = judge_real_package(mode="paper", legs=[leg], frozen_orders=(), readiness=())
    assert block is None


def test_matchbook_freeze_uses_the_ladder_locally() -> None:
    frozen = freeze_native_orders(_decision(_mb_leg()))
    assert len(frozen) == 1
    assert frozen[0].venue == "matchbook"
    assert frozen[0].native_event_id == "evt-mb"
    assert frozen[0].native_runner_id == "101"
    assert frozen[0].ladder_odds == "2.10"
    assert frozen[0].native_stake == "4"
    assert frozen[0].native_side == "back"
    missing, _ = judge_real_package(
        mode="real",
        legs=[_mb_leg()],
        frozen_orders=(),
        readiness=(),
    )
    assert missing == MATCHBOOK_ORDER_UNPROVEN


class _Posts:
    def __init__(self) -> None:
        self.orders: list[object] = []
        self.posts = 0

    def post_fak(self, order: object) -> PolymarketSubmission:
        self.orders.append(order)
        self.posts += 1
        amount = order.amount  # type: ignore[attr-defined]
        shares = order.shares  # type: ignore[attr-defined]
        return PolymarketSubmission(
            state="matched",
            order_id="0xorder",
            making_amount=amount,
            taking_amount=shares,
        )

    def cancel(self, order_id: str) -> str:
        del order_id
        return "cancelled"

    def constraints(self, token_id: str) -> None:
        raise AssertionError(f"order book read {token_id}")

    def readiness(self, spend: Decimal) -> None:
        raise AssertionError(f"balance read {spend}")

    def account_snapshot(self) -> dict[str, bool]:
        raise AssertionError("account snapshot")


def _pm_request(**overrides: object) -> VenueOrderRequest:
    payload: dict[str, object] = {
        "venue": VenueName.POLYMARKET,
        "trade_id": "trade-1",
        "tranche_id": "opening",
        "native_event_id": "evt-pm",
        "native_market_id": "condition-1",
        "native_runner_id": TOKEN,
        "side": MarketSide.BACK,
        "currency": "USD",
        "requested_price": Decimal("2.04"),
        "requested_size": Decimal("2.50"),
        "client_order_id": "sh-" + "cd" * 16,
        "price2_snapshot_id": "exec:authority:1",
        "frozen_order_type": POLYMARKET_ORDER_TYPE,
        "frozen_limit_price": Decimal("0.47"),
        "frozen_amount": Decimal("2.50"),
        "frozen_shares": Decimal("5.319148936"),
        "frozen_tick_size": "0.01",
        "frozen_minimum_size": Decimal(1),
    }
    payload.update(overrides)
    return VenueOrderRequest(**payload)


async def test_polymarket_dispatch_submits_the_frozen_order_only() -> None:
    broker = _Posts()
    transport = PolymarketHttpExecutionTransport(
        Settings(sports_hedge_mode="real", sports_hedge_execution_enabled=True),
        broker=broker,
        clock=lambda: NOW,
    )
    result = await transport.dispatch(_pm_request())
    assert broker.posts == 1
    sent = broker.orders[0]
    assert sent.token_id == TOKEN
    assert sent.price == Decimal("0.47")
    assert sent.amount == Decimal("2.50")
    assert sent.order_type == POLYMARKET_ORDER_TYPE
    assert result.filled_size == Decimal("2.50")
    source = inspect.getsource(PolymarketHttpExecutionTransport.dispatch)
    broker_source = inspect.getsource(type(transport._broker))
    combined = source + inspect.getsource(PolymarketHttpExecutionTransport)
    assert "fetch_geoblock" not in combined
    assert "get_order_book" not in combined
    assert "get_balance_allowance" not in combined
    assert "constraints(" not in source
    assert "readiness(" not in source
    assert "get_order_book" not in broker_source or "def constraints" not in broker_source


async def test_polymarket_execution_disabled_submits_nothing() -> None:
    broker = _Posts()
    transport = PolymarketHttpExecutionTransport(
        Settings(sports_hedge_mode="real", sports_hedge_execution_enabled=False),
        broker=broker,
        clock=lambda: NOW,
    )
    result = await transport.dispatch(_pm_request())
    assert result.filled_size == 0
    assert broker.posts == 0


async def test_polymarket_duplicate_and_ambiguous_still_post_once() -> None:
    broker = _Posts()
    transport = PolymarketHttpExecutionTransport(
        Settings(sports_hedge_mode="real", sports_hedge_execution_enabled=True),
        broker=broker,
        clock=lambda: NOW,
    )
    request = _pm_request()
    await transport.dispatch(request)
    await transport.dispatch(request)
    assert broker.posts == 1

    from sports_hedge.execution.polymarket_http import PolymarketAmbiguous

    ambiguous = _Posts()

    def fail(_order: object) -> None:
        raise PolymarketAmbiguous("timeout")

    ambiguous.post_fak = fail  # type: ignore[method-assign]
    transport = PolymarketHttpExecutionTransport(
        Settings(sports_hedge_mode="real", sports_hedge_execution_enabled=True),
        broker=ambiguous,
        clock=lambda: NOW,
    )
    first = await transport.dispatch(_pm_request(client_order_id="sh-" + "ef" * 16))
    second = await transport.dispatch(_pm_request(client_order_id="sh-" + "ef" * 16))
    assert first.filled_size is None
    assert second.filled_size is None
    assert ambiguous.posts == 0


def _armed() -> Settings:
    return Settings(
        sports_hedge_mode="real",
        sports_hedge_execution_enabled=True,
        matchbook_username="user",
        matchbook_password="secret",
    )


async def test_matchbook_posts_the_frozen_odds_and_does_not_read_the_account() -> None:
    seen: list[tuple[str, str]] = []
    body_seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == MATCHBOOK_SESSION_PATH:
            return httpx.Response(200, json={"session-token": "tok"})
        if request.method == "POST" and request.url.path == SUBMIT_PATH:
            body_seen.update(json.loads(request.content))
            return httpx.Response(
                200,
                json={
                    "offers": [
                        {
                            "id": 9,
                            "status": "matched",
                            "stake": 4,
                            "remaining": 0,
                            "matched-bets": [{"stake": 4, "decimal-odds": "2.12"}],
                        }
                    ]
                },
            )
        raise AssertionError(f"unexpected {request.method} {request.url.path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://api.matchbook.example")
    transport = MatchbookHttpExecutionTransport(_armed(), client=client, clock=lambda: NOW)
    request = VenueOrderRequest(
        venue=VenueName.MATCHBOOK,
        trade_id="trade-1",
        tranche_id="opening",
        native_event_id="evt-mb",
        native_market_id="mb-mkt",
        native_runner_id="101",
        side=MarketSide.BACK,
        currency="GBP",
        requested_price=Decimal("2.09"),
        requested_size=Decimal(4),
        client_order_id="sh-" + "11" * 16,
        price2_snapshot_id="exec:authority:1",
        frozen_order_type="back",
        frozen_limit_price=Decimal("2.12"),
        frozen_amount=Decimal(4),
    )
    result = await transport.dispatch(request)
    await client.aclose()
    assert result.filled_size == Decimal(4)
    assert seen == [("POST", MATCHBOOK_SESSION_PATH), ("POST", SUBMIT_PATH)]
    assert BALANCE_PATH not in {path for _method, path in seen}
    offer = body_seen["offers"][0]
    assert offer["odds"] == 2.12
    assert offer["stake"] == 4
    assert offer["runner-id"] == 101
    assert offer["side"] == "back"
    assert "BALANCE_PATH" not in inspect.getsource(MatchbookHttpExecutionTransport.dispatch)
    assert "get_order_book" not in inspect.getsource(MatchbookHttpExecutionTransport.dispatch)


async def test_matchbook_execution_disabled_does_not_post() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"disabled execution called {request.url.path}")

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    transport = MatchbookHttpExecutionTransport(
        Settings(sports_hedge_mode="real", sports_hedge_execution_enabled=False),
        client=client,
        clock=lambda: NOW,
    )
    result = await transport.dispatch(
        VenueOrderRequest(
            venue=VenueName.MATCHBOOK,
            trade_id="trade-1",
            tranche_id="opening",
            native_event_id="evt-mb",
            native_market_id="mb-mkt",
            native_runner_id="101",
            side=MarketSide.BACK,
            currency="GBP",
            requested_price=Decimal("2.10"),
            requested_size=Decimal(4),
            client_order_id="sh-" + "22" * 16,
            frozen_limit_price=Decimal("2.10"),
            frozen_amount=Decimal(4),
        )
    )
    await client.aclose()
    assert result.filled_size == 0


def _plan() -> PaperFillPlan:
    pm = _pm_leg()
    mb = _mb_leg()
    frozen = freeze_native_orders(
        _decision(pm, mb),
        polymarket_books={TOKEN: {"tick_size": "0.01", "min_order_size": "1"}},
    )
    # Keep the locally snapped Matchbook odds, then override the posted odds so
    # dispatch must use the frozen package rather than snap 2.09 to 2.10 again.
    replaced = tuple(
        FrozenNativeOrder(
            venue=item.venue,
            native_event_id=item.native_event_id,
            native_market_id=item.native_market_id,
            native_runner_id=item.native_runner_id,
            side=item.side,
            currency=item.currency,
            approved_decimal_odds=item.approved_decimal_odds,
            approved_stake=item.approved_stake,
            order_type=item.order_type,
            tick_size=item.tick_size,
            minimum_order_size=item.minimum_order_size,
            limit_price=item.limit_price,
            native_amount=item.native_amount,
            native_shares=item.native_shares,
            ladder_odds="2.12" if item.venue == "matchbook" else item.ladder_odds,
            native_stake=item.native_stake,
            native_side=item.native_side,
        )
        for item in frozen
    )
    snapshot = ExecutionSnapshot(
        catalogue_row_id="row-1",
        started_at=NOW,
        retrievals=(),
        snapshot_id="exec:authority:pkg",
        accepted=True,
        net_edge="0.02",
        frozen_orders=replaced,
    )
    return PaperFillPlan(
        opportunity_id="watch:mkt",
        canonical_event_id="evt",
        canonical_market_id="mkt",
        scanned_at=NOW,
        eligible_for_paper_simulation=True,
        settlement_equivalent=True,
        legs=[mb, pm],
        decision=_decision(mb, pm),
        execution_authoritative=True,
        execution_snapshot_json=snapshot.to_json(),
        provenance="live_paper",
    )


async def test_package_reaches_dispatch_unchanged_and_does_not_recompute_economics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_fee(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("execution recomputed fees")

    monkeypatch.setattr(polymarket_fees, "apply_polymarket_taker", fail_fee)
    seen: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path))
        if request.url.path == MATCHBOOK_SESSION_PATH:
            return httpx.Response(200, json={"session-token": "tok"})
        if request.method == "POST" and request.url.path == SUBMIT_PATH:
            offer = json.loads(request.content)["offers"][0]
            assert offer["odds"] == 2.12
            assert offer["stake"] == 4
            assert offer["runner-id"] == 101
            return httpx.Response(
                200,
                json={
                    "offers": [
                        {
                            "id": 9,
                            "status": "matched",
                            "stake": 4,
                            "remaining": 0,
                            "matched-bets": [{"stake": 4, "decimal-odds": "2.12"}],
                        }
                    ]
                },
            )
        raise AssertionError(request.url.path)

    client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="https://api.matchbook.example",
    )
    matchbook = MatchbookHttpExecutionTransport(_armed(), client=client, clock=lambda: NOW)
    broker = _Posts()
    polymarket = PolymarketHttpExecutionTransport(_armed(), broker=broker, clock=lambda: NOW)
    package = await execute_live_package(
        _plan(),
        trade_id="trade-1",
        tranche_id=OPENING_TRANCHE_ID,
        settings=_armed(),
        matchbook=MatchbookExecutionClient(matchbook),
        polymarket=PolymarketExecutionClient(polymarket),
    )
    await client.aclose()
    assert package.outcome is LivePackageOutcome.FULLY_FILLED
    assert broker.posts == 1
    assert broker.orders[0].price == Decimal("0.5")
    assert broker.orders[0].amount == Decimal("2.50")
    assert broker.orders[0].token_id == TOKEN
    assert [path for _method, path in seen] == [MATCHBOOK_SESSION_PATH, SUBMIT_PATH]


class _Capital:
    def __init__(self, rows: tuple[VenueReadinessEvidence, ...]) -> None:
        self.rows = rows
        self.calls = 0

    async def read(self, venues: tuple[object, ...]) -> tuple[VenueReadinessEvidence, ...]:
        self.calls += 1
        assert VenueName.POLYMARKET in venues
        return self.rows


async def test_real_price2_rejects_when_live_capital_is_unavailable() -> None:
    engine = CataloguePriceEngine(
        settings=Settings(sports_hedge_mode="real", sports_hedge_execution_enabled=False),
        venue_capital_authority=_Capital(()),
    )
    leg = _pm_leg()
    snapshot = ExecutionSnapshot(
        catalogue_row_id="row-1",
        started_at=NOW,
        retrievals=(),
        snapshot_id="exec:capital",
        accepted=False,
        net_edge="0.02",
        frozen_orders=freeze_native_orders(
            _decision(leg),
            polymarket_books={TOKEN: {"tick_size": "0.01", "min_order_size": "1"}},
        ),
    )
    result = ExecutionRepriceResult(
        decision=_decision(leg),
        snapshot=snapshot,
        pending_real_authority=True,
        diagnostics=ExecutionRepriceDiagnostics(started_at=NOW),
    )
    await engine._prove_real_authority(result, (VenueName.POLYMARKET, VenueName.MATCHBOOK))
    assert engine.venue_capital.calls == 1
    assert result.snapshot is not None
    assert result.snapshot.accepted is False
    assert result.reason == VENUE_CAPITAL_UNPROVEN
    assert result.diagnostics is not None
    assert result.diagnostics.calls[-1].stage == "venue_capital"
    assert engine.settings.sports_hedge_execution_enabled is False


async def test_real_price2_accepts_only_after_live_capital_covers_the_stake() -> None:
    authority = _Capital(
        (
            VenueReadinessEvidence(
                venue="polymarket",
                currency="USD",
                proven=True,
                spendable="100",
                allowance="100",
                required_stake=None,
                source="polymarket_collateral",
            ),
            VenueReadinessEvidence(
                venue="matchbook",
                currency="GBP",
                proven=True,
                spendable="100",
                allowance=None,
                required_stake=None,
                source="matchbook_free_funds",
            ),
        )
    )
    engine = CataloguePriceEngine(
        settings=Settings(sports_hedge_mode="real", sports_hedge_execution_enabled=False),
        venue_capital_authority=authority,
    )
    pm = _pm_leg()
    mb = _mb_leg()
    snapshot = ExecutionSnapshot(
        catalogue_row_id="row-1",
        started_at=NOW,
        retrievals=(),
        snapshot_id="exec:capital:ok",
        net_edge="0.03",
        guaranteed_profit="1.20",
        venue_costs=(),
        fx_rates=(),
        frozen_orders=freeze_native_orders(
            _decision(pm, mb),
            polymarket_books={TOKEN: {"tick_size": "0.01", "min_order_size": "1"}},
        ),
    )
    result = ExecutionRepriceResult(
        decision=_decision(pm, mb),
        snapshot=snapshot,
        pending_real_authority=True,
        diagnostics=ExecutionRepriceDiagnostics(started_at=NOW),
    )
    await engine._prove_real_authority(result, (VenueName.MATCHBOOK, VenueName.POLYMARKET))
    assert result.snapshot is not None
    assert result.snapshot.accepted is True
    assert result.reason is None
    by_venue = {item.venue: item for item in result.snapshot.venue_readiness}
    assert by_venue["polymarket"].required_stake == "2.50"
    assert by_venue["matchbook"].required_stake == "4"
    assert by_venue["polymarket"].source == "polymarket_collateral"
    payload = json.loads(result.snapshot.to_json())
    assert payload["frozen_orders"]
    assert payload["venue_readiness"]
    assert payload["accepted"] is True
