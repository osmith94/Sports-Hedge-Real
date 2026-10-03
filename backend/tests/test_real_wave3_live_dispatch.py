"""Wave 3: accepted Price-2 opens the existing live package. No venue I/O."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sports_hedge.application.paper_operations import PaperOperationsService, paper_trade_id
from sports_hedge.arbitrage.watchlist.economics import classification_for
from sports_hedge.arbitrage.watchlist.models import NearOpportunity, OpportunityStatus
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.execution.clients import (
    DeterministicExecutionTransport,
    KalshiExecutionClient,
    MatchbookExecutionClient,
)
from sports_hedge.execution.models import VenueOrderResult, VenueOrderStatus
from sports_hedge.execution.package import execution_capability
from sports_hedge.execution.runtime import ExecutionRuntime, set_execution_runtime
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.chain import PaperFillPlan
from sports_hedge.paper.fills import PaperOpportunityLeg
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.paper.trades import (
    OPENING_TRANCHE_ID,
    PaperActiveTradePhase,
    PaperLegFillKind,
    PaperTradeState,
)
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
SECRET = "mb-exec-secret"
MARKET = "mkt-wave3"


def _leg(
    venue: VenueName,
    *,
    event_id: str,
    market_id: str,
    runner_id: str,
    stake: str = "4",
    odds: str = "2.10",
    outcome: str = "home",
) -> PaperOpportunityLeg:
    return PaperOpportunityLeg(
        outcome=outcome,
        venue=venue,
        source_market_id=market_id,
        source_runner_id=runner_id,
        source_event_id=event_id,
        currency="GBP",
        requested_stake=Decimal(stake),
        displayed_odds=Decimal(odds),
    )


def _hedge_legs() -> list[PaperOpportunityLeg]:
    return [
        _leg(
            VenueName.MATCHBOOK,
            event_id="evt-mb",
            market_id="mb-mkt",
            runner_id="101",
            stake="4",
            odds="2.10",
            outcome="home",
        ),
        _leg(
            VenueName.KALSHI,
            event_id="evt-kx",
            market_id="KX-MKT",
            runner_id="KX-MKT",
            stake="5",
            odds="2.40",
            outcome="away",
        ),
    ]


def _plan(*legs: PaperOpportunityLeg, accepted: bool = True) -> PaperFillPlan:
    snapshot = {
        "snapshot_id": "exec:wave3:1",
        "accepted": accepted,
        "password": SECRET,
    }
    return PaperFillPlan(
        opportunity_id=f"watch:{MARKET}",
        canonical_event_id="evt-canonical",
        canonical_market_id=MARKET,
        scanned_at=NOW,
        eligible_for_paper_simulation=True,
        settlement_equivalent=True,
        legs=list(legs),
        decision=PaperScanDecision(
            canonical_market_id=MARKET,
            canonical_event_id="evt-canonical",
            eligible_for_paper_simulation=True,
            market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=["test"]),
            solver_model="simple_complete_set",
            fill_legs=list(legs),
            scanned_at=NOW,
        ),
        execution_authoritative=True,
        execution_snapshot_json=json.dumps(snapshot),
        provenance="live_paper",
    )


def _watch(service: WatchlistService) -> None:
    service.repository.upsert_opportunity(
        NearOpportunity(
            opportunity_id=f"watch:{MARKET}",
            canonical_event_id="evt-canonical",
            canonical_market_id=MARKET,
            status=OpportunityStatus.TRIGGERED,
            classification=classification_for(OpportunityStatus.TRIGGERED),
            is_arbitrage=True,
            first_seen_at=NOW,
            last_seen_at=NOW,
        ),
        force_status=True,
    )


def _ops(
    settings: Settings,
    *,
    runtime: ExecutionRuntime | None = None,
    ledger: SqlitePaperLedger | None = None,
) -> PaperOperationsService:
    watchlist = WatchlistService(SqliteWatchlistRepository())
    _watch(watchlist)
    return PaperOperationsService(
        watchlist=watchlist,
        settings=settings,
        ledger=ledger,
        execution_runtime=runtime or ExecutionRuntime(),
    )


def _armed() -> Settings:
    return Settings(sports_hedge_mode="real", sports_hedge_execution_enabled=True)


def _clients(
    matchbook: object | None = None,
    kalshi: object | None = None,
) -> ExecutionRuntime:
    return ExecutionRuntime(
        matchbook=None if matchbook is None else MatchbookExecutionClient(matchbook),
        kalshi=None if kalshi is None else KalshiExecutionClient(kalshi),
    )


class _Quote:
    def __init__(self, price: Decimal) -> None:
        self.price = price
        self.calls: list[object] = []
        self.cancel_calls: list[object] = []
        self.test_only = True
        self.transport_kind = "scripted"

    async def dispatch(self, request):
        self.calls.append(request)
        filled = request.requested_size
        return VenueOrderResult(
            venue=request.venue,
            client_order_id=request.client_order_id,
            venue_order_id=f"venue:{request.client_order_id}",
            status=VenueOrderStatus.FILLED,
            requested_size=request.requested_size,
            filled_size=filled,
            requested_price=request.requested_price,
            average_fill_price=self.price,
            submitted_at=NOW,
            updated_at=NOW,
        )


class _Unknown:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.test_only = True
        self.transport_kind = "scripted"

    async def dispatch(self, request):
        self.calls.append(request)
        return VenueOrderResult(
            venue=request.venue,
            client_order_id=request.client_order_id,
            venue_order_id=None,
            status=VenueOrderStatus.FAILED,
            requested_size=request.requested_size,
            filled_size=None,
            requested_price=request.requested_price,
            average_fill_price=None,
            submitted_at=NOW,
            updated_at=NOW,
        )


class _MatchbookRemainder:
    def __init__(self) -> None:
        self.calls: list[object] = []
        self.cancel_calls: list[object] = []
        self.test_only = True
        self.transport_kind = "scripted"

    async def dispatch(self, request):
        self.calls.append(request)
        if request.venue is VenueName.MATCHBOOK:
            return VenueOrderResult(
                venue=request.venue,
                client_order_id=request.client_order_id,
                venue_order_id="offer-77",
                status=VenueOrderStatus.OPEN,
                requested_size=request.requested_size,
                filled_size=Decimal("1.25"),
                requested_price=request.requested_price,
                average_fill_price=Decimal("2.20"),
                submitted_at=NOW,
                updated_at=NOW,
            )
        return VenueOrderResult(
            venue=request.venue,
            client_order_id=request.client_order_id,
            venue_order_id="kx-1",
            status=VenueOrderStatus.FILLED,
            requested_size=request.requested_size,
            filled_size=request.requested_size,
            requested_price=request.requested_price,
            average_fill_price=Decimal("2.55"),
            submitted_at=NOW,
            updated_at=NOW,
        )

    async def cancel(self, request):
        self.cancel_calls.append(request)
        return VenueOrderResult(
            venue=request.venue,
            client_order_id=request.client_order_id,
            venue_order_id="offer-77",
            status=VenueOrderStatus.CANCELLED,
            requested_size=request.requested_size,
            filled_size=Decimal("1.25"),
            requested_price=request.requested_price,
            average_fill_price=Decimal("2.20"),
            submitted_at=NOW,
            updated_at=NOW,
            native_remaining_quantity=Decimal(0),
        )


def _install_solver(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        "sports_hedge.application.paper_operations._solver_is_arbitrage",
        lambda _decision: True,
    )
    monkeypatch.setattr(
        "sports_hedge.application.paper_operations.attach_ftts_ordinary_depth",
        lambda decision: decision,
    )


def _persist(ops: PaperOperationsService, plan: PaperFillPlan) -> None:
    ops.persist_triggered_chain(
        plan.decision,
        autofill=True,
        now=NOW,
        execution_authoritative=plan.execution_authoritative,
        execution_snapshot_json=plan.execution_snapshot_json,
    )


def test_paper_mode_simulates_and_does_not_dispatch(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_solver(monkeypatch)
    transport = DeterministicExecutionTransport()
    ops = _ops(Settings(), runtime=_clients(transport, DeterministicExecutionTransport()))
    seen: list[str] = []

    def _simulate(*_args, **_kwargs):
        seen.append("simulate")

    ops.simulate_fill = _simulate  # type: ignore[method-assign]
    _persist(ops, _plan(*_hedge_legs()))
    assert seen == ["simulate"]
    assert transport.calls == []


def test_real_mode_with_execution_disabled_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_solver(monkeypatch)
    transport = DeterministicExecutionTransport()
    ops = _ops(
        Settings(sports_hedge_mode="real"),
        runtime=_clients(transport, DeterministicExecutionTransport()),
    )
    seen: list[str] = []
    ops.simulate_fill = lambda *_a, **_k: seen.append("simulate")  # type: ignore[method-assign]
    _persist(ops, _plan(*_hedge_legs()))
    assert seen == ["simulate"]
    assert transport.calls == []


def test_real_mode_without_transport_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_solver(monkeypatch)
    calls: list[int] = []

    async def _blocked(*_args, **_kwargs):
        calls.append(1)
        raise AssertionError("live package must not run without a transport")

    monkeypatch.setattr("sports_hedge.execution.dispatch.execute_live_package", _blocked)
    ops = _ops(_armed(), runtime=ExecutionRuntime())
    seen: list[str] = []
    ops.simulate_fill = lambda *_a, **_k: seen.append("simulate")  # type: ignore[method-assign]
    _persist(ops, _plan(*_hedge_legs()))
    assert calls == []
    assert seen == []


def test_rejected_price2_sends_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_solver(monkeypatch)
    transport = DeterministicExecutionTransport()
    calls: list[int] = []

    async def _blocked(*_args, **_kwargs):
        calls.append(1)
        raise AssertionError("rejected Price-2 must not dispatch")

    monkeypatch.setattr("sports_hedge.execution.dispatch.execute_live_package", _blocked)
    ops = _ops(_armed(), runtime=_clients(transport, DeterministicExecutionTransport()))
    ops.simulate_fill = lambda *_a, **_k: calls.append(2)  # type: ignore[method-assign]
    _persist(ops, _plan(*_hedge_legs(), accepted=False))
    assert calls == []
    assert transport.calls == []


@pytest.mark.asyncio
async def test_accepted_price2_dispatches_the_package_once(monkeypatch: pytest.MonkeyPatch) -> None:
    _install_solver(monkeypatch)
    from sports_hedge.execution.dispatch import execute_live_package

    calls: list[int] = []

    async def _once(*args, **kwargs):
        calls.append(1)
        return await execute_live_package(*args, **kwargs)

    monkeypatch.setattr("sports_hedge.execution.dispatch.execute_live_package", _once)
    matchbook = DeterministicExecutionTransport()
    kalshi = DeterministicExecutionTransport()
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(_armed(), runtime=_clients(matchbook, kalshi), ledger=ledger)
    _persist(ops, _plan(*_hedge_legs()))
    assert calls == [1]
    assert len(matchbook.calls) == 1
    assert len(kalshi.calls) == 1
    matchbook_order = matchbook.calls[0]
    assert matchbook_order.native_event_id == "evt-mb"
    assert matchbook_order.native_market_id == "mb-mkt"
    assert matchbook_order.native_runner_id == "101"
    assert matchbook_order.requested_price == Decimal("2.10")
    assert matchbook_order.requested_size == Decimal(4)
    assert kalshi.calls[0].requested_price == Decimal("2.40")
    assert kalshi.calls[0].requested_size == Decimal(5)
    assert kalshi.calls[0].native_market_id == "KX-MKT"


def test_fully_filled_package_uses_actual_prices_and_becomes_active() -> None:
    matchbook = _Quote(Decimal("2.30"))
    kalshi = _Quote(Decimal("2.70"))
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(_armed(), runtime=_clients(matchbook, kalshi), ledger=ledger)
    plan = _plan(*_hedge_legs())
    ops._plans[plan.opportunity_id] = plan
    ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    trade = ledger.trades.get_by_opportunity(plan.opportunity_id)
    assert trade is not None
    assert trade.state is PaperTradeState.OPEN
    assert trade.paper_only is False
    assert trade.places_orders is True
    assert trade.live_fill_unknown is False
    assert trade.guaranteed_profit_gbp_at_open is None
    by_venue = {leg.venue: leg for leg in trade.legs}
    assert by_venue[VenueName.MATCHBOOK].filled_stake == Decimal(4)
    assert by_venue[VenueName.MATCHBOOK].filled_odds == Decimal("2.30")
    assert by_venue[VenueName.MATCHBOOK].displayed_odds == Decimal("2.10")
    assert by_venue[VenueName.MATCHBOOK].fill_kind is PaperLegFillKind.LIVE_VENUE
    assert by_venue[VenueName.KALSHI].filled_odds == Decimal("2.70")
    assert by_venue[VenueName.KALSHI].filled_stake == Decimal(5)
    reloaded = ledger.trades.get(trade.trade_id)
    assert reloaded is not None and reloaded.state is PaperTradeState.OPEN
    assert reloaded.active_trade_phase is PaperActiveTradePhase.ACCUMULATING


def test_partial_package_is_not_stored_as_fully_hedged() -> None:
    matchbook = DeterministicExecutionTransport(filled_fraction=Decimal("0.5"))
    kalshi = DeterministicExecutionTransport(filled_fraction=Decimal("0.5"))
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(_armed(), runtime=_clients(matchbook, kalshi), ledger=ledger)
    plan = _plan(*_hedge_legs())
    ops._plans[plan.opportunity_id] = plan
    ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    trade = ledger.trades.get_by_opportunity(plan.opportunity_id)
    assert trade is not None
    assert trade.state is PaperTradeState.PARTIAL
    assert trade.state is not PaperTradeState.OPEN
    assert trade.unresolved_recovery is False
    assert trade.active_trade_phase is PaperActiveTradePhase.LIVE_PARTIAL_EXPOSURE
    assert trade.guaranteed_profit_gbp_at_open is None
    assert all(leg.filled_stake < leg.requested_stake for leg in trade.legs)
    assert all(leg.filled_stake > 0 for leg in trade.legs)

    class _Simulator:
        def simulate(self, *_args, **_kwargs):
            raise AssertionError("partial live exposure must not paper-simulate a hedge")

    ops.simulator = _Simulator()  # type: ignore[assignment]
    ops._maybe_top_up_open_trade_locked(trade, now=NOW, plan=plan)


def test_unknown_fill_size_is_not_stored_as_zero() -> None:
    unknown = _Unknown()
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(_armed(), runtime=_clients(unknown, _Unknown()), ledger=ledger)
    plan = _plan(*_hedge_legs())
    ops._plans[plan.opportunity_id] = plan
    ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    trade = ledger.trades.get_by_opportunity(plan.opportunity_id)
    assert trade is not None
    assert trade.state is PaperTradeState.PENDING
    assert trade.live_fill_unknown is True
    assert trade.active_trade_phase is None
    assert all(leg.fill_quantity_known is False for leg in trade.legs)
    attempt = ledger.live_attempts.get(f"{paper_trade_id(plan.opportunity_id)}:{OPENING_TRANCHE_ID}")
    assert attempt is not None
    orders = json.loads(attempt["orders_json"])
    assert orders
    assert all(order["filled_size"] is None for order in orders)
    assert "0" not in {order["filled_size"] for order in orders}


def test_failed_zero_fill_does_not_open_an_active_hedge() -> None:
    matchbook = DeterministicExecutionTransport(filled_fraction=Decimal(0))
    kalshi = DeterministicExecutionTransport(filled_fraction=Decimal(0))
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(_armed(), runtime=_clients(matchbook, kalshi), ledger=ledger)
    plan = _plan(*_hedge_legs())
    ops._plans[plan.opportunity_id] = plan
    ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    trade = ledger.trades.get_by_opportunity(plan.opportunity_id)
    assert trade is not None
    assert trade.state is PaperTradeState.PENDING
    assert trade.active_trade_phase is None
    assert trade.live_fill_unknown is False
    assert all(leg.fill_quantity_known is True for leg in trade.legs)
    assert all(leg.filled_stake == 0 for leg in trade.legs)
    assert all(leg.fill_kind is PaperLegFillKind.UNFILLED for leg in trade.legs)


def test_duplicate_observation_does_not_submit_twice(tmp_path: Path) -> None:
    database = tmp_path / "wave3.sqlite"
    matchbook = DeterministicExecutionTransport()
    kalshi = DeterministicExecutionTransport()
    ledger = SqlitePaperLedger(database)
    ops = _ops(_armed(), runtime=_clients(matchbook, kalshi), ledger=ledger)
    plan = _plan(*_hedge_legs())
    ops._plans[plan.opportunity_id] = plan
    ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    assert len(matchbook.calls) == 1
    assert len(kalshi.calls) == 1
    ledger._connection.close()

    restarted_matchbook = DeterministicExecutionTransport()
    restarted_kalshi = DeterministicExecutionTransport()
    restarted = SqlitePaperLedger(database, auto_seed=False)
    again = _ops(
        _armed(),
        runtime=_clients(restarted_matchbook, restarted_kalshi),
        ledger=restarted,
    )
    again._plans[plan.opportunity_id] = plan
    again.capture_opening_fill(plan.opportunity_id, now=NOW)
    assert restarted_matchbook.calls == []
    assert restarted_kalshi.calls == []


def test_matchbook_resting_remainder_is_cancelled() -> None:
    matchbook = _MatchbookRemainder()
    kalshi = _MatchbookRemainder()
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(_armed(), runtime=_clients(matchbook, kalshi), ledger=ledger)
    plan = _plan(*_hedge_legs())
    ops._plans[plan.opportunity_id] = plan
    ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    assert len(matchbook.cancel_calls) == 1
    assert kalshi.cancel_calls == []
    trade = ledger.trades.get_by_opportunity(plan.opportunity_id)
    assert trade is not None
    assert trade.state is PaperTradeState.PARTIAL
    matchbook_leg = next(leg for leg in trade.legs if leg.venue is VenueName.MATCHBOOK)
    assert matchbook_leg.filled_stake == Decimal("1.25")
    assert matchbook_leg.filled_odds == Decimal("2.20")
    assert matchbook_leg.filled_stake != matchbook_leg.requested_stake
    attempt = ledger.live_attempts.get(f"{trade.trade_id}:{OPENING_TRANCHE_ID}")
    assert attempt is not None
    assert attempt["detail"] == "cancelled"
    orders = json.loads(attempt["orders_json"])
    resting = [order for order in orders if order["venue_status"] == "open"]
    assert resting == []


def test_audit_records_execution_facts_without_secrets() -> None:
    matchbook = _Quote(Decimal("2.30"))
    kalshi = _Quote(Decimal("2.70"))
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(_armed(), runtime=_clients(matchbook, kalshi), ledger=ledger)
    plan = _plan(*_hedge_legs())
    ops._plans[plan.opportunity_id] = plan
    ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    trade = ledger.trades.get_by_opportunity(plan.opportunity_id)
    assert trade is not None
    attempt = ledger.live_attempts.get(f"{trade.trade_id}:{OPENING_TRANCHE_ID}")
    assert attempt is not None
    blob = json.dumps(attempt)
    detail = next(
        event.detail
        for event in trade.audit
        if event.detail and "package_outcome" in event.detail
    )
    combined = blob + detail
    assert SECRET not in combined
    assert "password" not in combined.casefold()
    payload = json.loads(detail)
    assert payload["trade_id"] == trade.trade_id
    assert payload["tranche_id"] == OPENING_TRANCHE_ID
    assert payload["snapshot_ref"] == "exec:wave3:1"
    assert payload["package_id"] == f"{trade.trade_id}:{OPENING_TRANCHE_ID}"
    assert payload["package_outcome"] == "FULLY_FILLED"
    order = payload["orders"][0]
    for field in (
        "venue",
        "native_event_id",
        "native_market_id",
        "native_runner_id",
        "client_order_id",
        "submitted_at",
        "requested_price",
        "requested_size",
        "venue_order_id",
        "venue_status",
        "filled_size",
        "average_fill_price",
        "updated_at",
    ):
        assert field in order
        assert order[field] not in (None, "")


def test_health_reports_live_scanner_only_when_transports_are_armed() -> None:
    set_execution_runtime(None)
    try:
        configured = Settings(
            sports_hedge_mode="real",
            sports_hedge_execution_enabled=True,
            matchbook_username="user",
            matchbook_password=SECRET,
            kalshi_api_key_id="key-id",
            kalshi_private_key_path="C:/unused/kalshi.pem",
        )
        blocked = execution_capability(configured)
        assert blocked["live_execution_ready"] is False
        assert blocked["scanner_execution"] == "paper"
        assert blocked["matchbook_execution_configured"] is True
        assert SECRET not in json.dumps(blocked)

        class _Http:
            test_only = False

            def __init__(self, kind: str) -> None:
                self.transport_kind = kind

        class _Client:
            def __init__(self, kind: str) -> None:
                self._transport = _Http(kind)

        set_execution_runtime(
            ExecutionRuntime(
                matchbook=_Client("matchbook_http"),  # type: ignore[arg-type]
                kalshi=_Client("kalshi_http"),  # type: ignore[arg-type]
            )
        )
        ready = execution_capability(configured)
        assert ready["live_execution_ready"] is True
        assert ready["execution_transport"] == "armed"
        assert ready["scanner_execution"] == "live"
        assert execution_capability(Settings())["live_execution_ready"] is False
        set_execution_runtime(
            _clients(DeterministicExecutionTransport(), DeterministicExecutionTransport())
        )
        assert execution_capability(configured)["live_execution_ready"] is False
    finally:
        set_execution_runtime(None)
