"""Pre-canary blockers: one runtime, unknown Matchbook fills, orphaned attempts."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from sports_hedge.api.main import _orphaned_live_executions, app, health
from sports_hedge.api.paper import get_paper_journal_holder
from sports_hedge.application.paper_operations import PaperOperationsService, paper_trade_id
from sports_hedge.arbitrage.watchlist.economics import classification_for
from sports_hedge.arbitrage.watchlist.models import NearOpportunity, OpportunityStatus
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.models import MarketSide, VenueName
from sports_hedge.execution.clients import (
    DeterministicExecutionTransport,
    KalshiExecutionClient,
    MatchbookExecutionClient,
)
from sports_hedge.execution.matchbook_http import MatchbookHttpExecutionTransport
from sports_hedge.execution.models import VenueOrderRequest, VenueOrderStatus
from sports_hedge.execution.orphans import clear_orphan_reports, orphaned_live_executions
from sports_hedge.execution.package import execution_capability
from sports_hedge.execution.runtime import (
    ExecutionRuntime,
    get_execution_runtime,
    set_execution_runtime,
)
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.chain import PaperFillPlan
from sports_hedge.paper.fills import PaperOpportunityLeg
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.paper.trades import (
    OPENING_TRANCHE_ID,
    PaperActiveTradePhase,
    PaperTradeAuditEventType,
    PaperTradeState,
)
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger

NOW = datetime(2026, 10, 3, 12, tzinfo=UTC)
MARKET = "mkt-canary"


@pytest.fixture(autouse=True)
def _reset_process_execution():
    set_execution_runtime(None)
    clear_orphan_reports()
    yield
    set_execution_runtime(None)
    clear_orphan_reports()


def _leg(venue: VenueName, *, runner: str, stake: str = "4", odds: str = "2.10", outcome: str = "home") -> PaperOpportunityLeg:
    return PaperOpportunityLeg(
        outcome=outcome,
        venue=venue,
        source_market_id="mb-mkt" if venue is VenueName.MATCHBOOK else "KX-MKT",
        source_runner_id=runner,
        source_event_id="evt-mb" if venue is VenueName.MATCHBOOK else "evt-kx",
        currency="GBP",
        requested_stake=Decimal(stake),
        displayed_odds=Decimal(odds),
    )


def _plan() -> PaperFillPlan:
    legs = [
        _leg(VenueName.MATCHBOOK, runner="101", outcome="home"),
        _leg(VenueName.KALSHI, runner="KX-MKT", stake="5", odds="2.40", outcome="away"),
    ]
    return PaperFillPlan(
        opportunity_id=f"watch:{MARKET}",
        canonical_event_id="evt-canonical",
        canonical_market_id=MARKET,
        scanned_at=NOW,
        eligible_for_paper_simulation=True,
        settlement_equivalent=True,
        legs=legs,
        decision=PaperScanDecision(
            canonical_market_id=MARKET,
            canonical_event_id="evt-canonical",
            eligible_for_paper_simulation=True,
            market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=["test"]),
            solver_model="simple_complete_set",
            fill_legs=legs,
            scanned_at=NOW,
        ),
        execution_authoritative=True,
        execution_snapshot_json=json.dumps({"snapshot_id": "exec:canary:1", "accepted": True}),
        provenance="live_paper",
    )


def _ops(settings: Settings, runtime: ExecutionRuntime, ledger: SqlitePaperLedger) -> PaperOperationsService:
    watchlist = WatchlistService(SqliteWatchlistRepository())
    watchlist.repository.upsert_opportunity(
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
    return PaperOperationsService(
        watchlist=watchlist,
        settings=settings,
        ledger=ledger,
        execution_runtime=runtime,
    )


def _armed() -> Settings:
    return Settings(sports_hedge_mode="real", sports_hedge_execution_enabled=True)


class _Http:
    test_only = False

    def __init__(self, kind: str) -> None:
        self.transport_kind = kind


class _Client:
    def __init__(self, kind: str) -> None:
        self._transport = _Http(kind)


def test_health_and_scanner_share_one_execution_runtime(monkeypatch: pytest.MonkeyPatch) -> None:
    matchbook = _Client("matchbook_http")
    kalshi = _Client("kalshi_http")
    set_execution_runtime(
        ExecutionRuntime(
            matchbook=matchbook,  # type: ignore[arg-type]
            kalshi=kalshi,  # type: ignore[arg-type]
            polymarket=_Client("polymarket_http"),  # type: ignore[arg-type]
        )
    )
    shared = get_execution_runtime()
    settings = _armed()
    ops = PaperOperationsService(
        watchlist=WatchlistService(SqliteWatchlistRepository()),
        settings=settings,
    )
    assert ops.execution_runtime is shared
    assert ops.execution_runtime.matchbook is matchbook
    assert ops.execution_runtime.kalshi is kalshi
    assert execution_capability(settings)["live_execution_ready"] is True
    assert get_execution_runtime() is shared

    monkeypatch.setattr("sports_hedge.api.main.get_settings", lambda: settings)
    monkeypatch.setattr(
        get_paper_journal_holder,
        "cache_info",
        lambda: type("Cache", (), {"currsize": 0})(),
    )
    body = TestClient(app).get("/health").json()
    assert body["execution"]["live_execution_ready"] is True
    assert body["execution"]["scanner_execution"] == "live"
    assert get_execution_runtime() is shared
    assert PaperOperationsService(
        watchlist=WatchlistService(SqliteWatchlistRepository()),
        settings=settings,
    ).execution_runtime is shared
    assert execution_capability(Settings())["live_execution_ready"] is False
    assert get_execution_runtime().matchbook is matchbook


def test_bind_constructs_http_transports_without_submitting(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    posts: list[object] = []

    def refuse(*_args: object, **_kwargs: object) -> None:
        posts.append(True)
        raise AssertionError("constructing execution transports submitted an order")

    monkeypatch.setattr(httpx.AsyncClient, "post", refuse)
    monkeypatch.setattr(httpx.AsyncClient, "request", refuse)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pem = tmp_path / "kalshi.pem"
    pem.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    paper = execution_capability(Settings())
    assert paper["live_execution_ready"] is False
    assert get_execution_runtime().matchbook is None
    assert get_execution_runtime().kalshi is None

    missing_key = Settings(
        sports_hedge_mode="real",
        sports_hedge_execution_enabled=True,
        matchbook_username="user",
        matchbook_password="secret",
        kalshi_api_key_id="key-id",
        kalshi_private_key_path="C:/unused/kalshi.pem",
    )
    blocked = execution_capability(missing_key)
    assert blocked["matchbook_execution_configured"] is True
    assert blocked["kalshi_execution_configured"] is True
    assert blocked["live_execution_ready"] is False
    assert posts == []

    set_execution_runtime(None)
    from eth_account import Account

    polymarket_key = tmp_path / "polymarket-private-key"
    polymarket_key.write_text(Account.create().key.hex(), encoding="utf-8")
    armed = Settings(
        sports_hedge_mode="real",
        sports_hedge_execution_enabled=True,
        matchbook_username="user",
        matchbook_password="secret",
        kalshi_api_key_id="key-id",
        kalshi_private_key_path=str(pem),
        polymarket_private_key_path=str(polymarket_key),
        polymarket_signature_type=0,
    )
    ready = execution_capability(armed)
    runtime = get_execution_runtime()
    assert ready["live_execution_ready"] is True
    assert ready["scanner_execution"] == "live"
    assert isinstance(runtime.matchbook, MatchbookExecutionClient)
    assert isinstance(runtime.kalshi, KalshiExecutionClient)
    assert runtime.polymarket is not None
    assert runtime.polymarket._transport.transport_kind == "polymarket_http"  # type: ignore[union-attr]
    assert runtime.matchbook._transport.transport_kind == "matchbook_http"  # type: ignore[union-attr]
    assert runtime.kalshi._transport.transport_kind == "kalshi_http"  # type: ignore[union-attr]
    assert posts == []
    ops = PaperOperationsService(
        watchlist=WatchlistService(SqliteWatchlistRepository()),
        settings=armed,
    )
    assert ops.execution_runtime is runtime
    assert execution_capability(armed)["live_execution_ready"] is True
    assert get_execution_runtime().matchbook is runtime.matchbook
    assert get_execution_runtime().kalshi is runtime.kalshi


def _matchbook_request() -> VenueOrderRequest:
    return VenueOrderRequest(
        venue=VenueName.MATCHBOOK,
        trade_id="trade-1",
        tranche_id="opening",
        native_event_id="100",
        native_market_id="200",
        native_runner_id="401525949430009",
        side=MarketSide.BACK,
        currency="GBP",
        requested_price=Decimal("2.50"),
        requested_size=Decimal(10),
        client_order_id="mb-order-1",
    )


def _matchbook_settings() -> Settings:
    return Settings(
        sports_hedge_mode="real",
        sports_hedge_execution_enabled=True,
        matchbook_username="mb-user",
        matchbook_password="secret",
    )


@pytest.mark.asyncio
async def test_matchbook_explicit_zero_stays_known_zero() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/security/session"):
            return httpx.Response(200, json={"session-token": "tok"})
        return httpx.Response(
            200,
            json={"offers": [{"id": 3, "status": "failed", "stake": 10, "remaining": 10}]},
        )

    transport = MatchbookHttpExecutionTransport(
        _matchbook_settings(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://venue.test"),
        clock=lambda: NOW,
    )
    result = await transport.dispatch(_matchbook_request())
    assert result.status is VenueOrderStatus.FAILED
    assert result.filled_size == Decimal(0)


@pytest.mark.asyncio
async def test_matchbook_lost_submit_is_unknown_and_is_not_posted_again() -> None:
    posts = {"offers": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/security/session"):
            return httpx.Response(200, json={"session-token": "tok"})
        posts["offers"] += 1
        raise httpx.ReadError("response lost")

    transport = MatchbookHttpExecutionTransport(
        _matchbook_settings(),
        client=httpx.AsyncClient(transport=httpx.MockTransport(handler), base_url="https://venue.test"),
        clock=lambda: NOW,
    )
    request = _matchbook_request()
    lost = await transport.dispatch(request)
    assert lost.filled_size is None
    assert lost.status is VenueOrderStatus.FAILED
    again = await transport.dispatch(request)
    assert again.filled_size is None
    assert posts["offers"] == 1


def test_completed_attempt_rebuilds_active_trade_without_resubmit() -> None:
    matchbook = DeterministicExecutionTransport()
    kalshi = DeterministicExecutionTransport()
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(
        _armed(),
        ExecutionRuntime(
            matchbook=MatchbookExecutionClient(matchbook),
            kalshi=KalshiExecutionClient(kalshi),
        ),
        ledger,
    )
    plan = _plan()
    ops._plans[plan.opportunity_id] = plan
    ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    assert len(matchbook.calls) == 1
    assert len(kalshi.calls) == 1
    trade_id = paper_trade_id(plan.opportunity_id)
    ledger._connection.execute("DELETE FROM paper_trade_events WHERE trade_id = ?", (trade_id,))
    ledger._connection.execute("DELETE FROM paper_trades WHERE trade_id = ?", (trade_id,))
    assert ledger.trades.get(trade_id) is None
    reports = ops.recover_orphaned_live_executions()
    rebuilt = ledger.trades.get(trade_id)
    assert rebuilt is not None
    assert rebuilt.state is PaperTradeState.OPEN
    assert rebuilt.places_orders is True
    assert rebuilt.live_fill_unknown is False
    assert rebuilt.active_trade_phase is PaperActiveTradePhase.ACCUMULATING
    assert reports[0]["classification"] == "reconstructed_active"
    assert reports[0]["recovered_from_persisted_attempt"] is True
    assert reports[0]["orders"]
    attempt = ledger.live_attempts.get(f"{trade_id}:{OPENING_TRANCHE_ID}")
    authority = json.loads(attempt["recovery_json"])["authority"]
    assert authority["market_match"]["reasons"] == ["test"]
    assert authority["solver_model"] == "simple_complete_set"
    assert authority["settlement_equivalent"] is True
    assert authority["execution_authoritative"] is True
    recorded = [
        event
        for event in rebuilt.audit
        if event.event_type is PaperTradeAuditEventType.LIVE_PACKAGE_RECORDED
    ]
    assert len(recorded) == 1
    assert json.loads(recorded[0].detail)["recovered_from_persisted_attempt"] is True
    assert "live_attempt_recovery" not in recorded[0].detail
    ops.recover_orphaned_live_executions()
    again = ledger.trades.get(trade_id)
    assert again is not None
    assert [
        event.event_id
        for event in again.audit
        if event.event_type is PaperTradeAuditEventType.LIVE_PACKAGE_RECORDED
    ] == [recorded[0].event_id]
    ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    assert len(matchbook.calls) == 1
    assert len(kalshi.calls) == 1


def test_reserved_attempt_is_surfaced_and_not_resubmitted() -> None:
    matchbook = DeterministicExecutionTransport()
    kalshi = DeterministicExecutionTransport()
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(
        _armed(),
        ExecutionRuntime(
            matchbook=MatchbookExecutionClient(matchbook),
            kalshi=KalshiExecutionClient(kalshi),
        ),
        ledger,
    )
    plan = _plan()
    ops._plans[plan.opportunity_id] = plan
    trade_id = paper_trade_id(plan.opportunity_id)
    from sports_hedge.execution.dispatch import opening_package_id, recovery_context

    reserved = ledger.live_attempts.reserve(
        {
            "package_id": opening_package_id(trade_id),
            "trade_id": trade_id,
            "tranche_id": OPENING_TRANCHE_ID,
            "opportunity_id": plan.opportunity_id,
            "snapshot_ref": "exec:canary:1",
            "snapshot_json": plan.execution_snapshot_json,
            "recovery_json": recovery_context(plan, trade_id),
            "created_at": NOW,
        }
    )
    assert reserved is True
    reports = ops.recover_orphaned_live_executions()
    assert ledger.trades.get(trade_id) is None
    assert reports[0]["classification"] == "orphaned_unresolved"
    assert reports[0]["reason"] == "attempt_reserved_or_incomplete"
    assert reports[0]["orders"] == []
    ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    assert matchbook.calls == []
    assert kalshi.calls == []
    assert orphaned_live_executions()[0]["package_id"] == opening_package_id(trade_id)


def test_unknown_fill_is_not_reconstructed_as_a_hedge() -> None:
    class _Unknown:
        def __init__(self) -> None:
            self.calls: list[object] = []

        async def dispatch(self, request):
            self.calls.append(request)
            from sports_hedge.execution.models import VenueOrderResult

            return VenueOrderResult(
                venue=request.venue,
                client_order_id=request.client_order_id,
                venue_order_id="offer-9",
                status=VenueOrderStatus.FAILED,
                requested_size=request.requested_size,
                filled_size=None,
                requested_price=request.requested_price,
                average_fill_price=None,
                submitted_at=NOW,
                updated_at=NOW,
            )

    matchbook = _Unknown()
    kalshi = _Unknown()
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(
        _armed(),
        ExecutionRuntime(
            matchbook=MatchbookExecutionClient(matchbook),
            kalshi=KalshiExecutionClient(kalshi),
        ),
        ledger,
    )
    plan = _plan()
    ops._plans[plan.opportunity_id] = plan
    ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    trade_id = paper_trade_id(plan.opportunity_id)
    ledger._connection.execute("DELETE FROM paper_trade_events WHERE trade_id = ?", (trade_id,))
    ledger._connection.execute("DELETE FROM paper_trades WHERE trade_id = ?", (trade_id,))
    reports = ops.recover_orphaned_live_executions()
    assert ledger.trades.get(trade_id) is None
    assert reports[0]["classification"] == "orphaned_unresolved"
    assert reports[0]["reason"] == "fill_quantity_unknown"
    assert reports[0]["orders"][0]["venue_order_id"] == "offer-9"
    assert reports[0]["orders"][0]["filled_size"] is None
    ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    assert len(matchbook.calls) == 1
    assert len(kalshi.calls) == 1


class _Calls:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def dispatch(self, request):
        self.calls.append(request)
        raise AssertionError("recovery must not submit a venue order")


class _UnknownFill:
    def __init__(self) -> None:
        self.calls: list[object] = []

    async def dispatch(self, request):
        self.calls.append(request)
        from sports_hedge.execution.models import VenueOrderResult

        return VenueOrderResult(
            venue=request.venue,
            client_order_id=request.client_order_id,
            venue_order_id="offer-9",
            status=VenueOrderStatus.FAILED,
            requested_size=request.requested_size,
            filled_size=None,
            requested_price=request.requested_price,
            average_fill_price=None,
            submitted_at=NOW,
            updated_at=NOW,
        )


def _erase_trade(ledger: SqlitePaperLedger, trade_id: str) -> None:
    ledger._connection.execute("DELETE FROM paper_trade_events WHERE trade_id = ?", (trade_id,))
    ledger._connection.execute("DELETE FROM paper_trades WHERE trade_id = ?", (trade_id,))


def test_health_read_does_not_reconstruct_during_live_persist(monkeypatch: pytest.MonkeyPatch) -> None:
    matchbook = DeterministicExecutionTransport()
    kalshi = DeterministicExecutionTransport()
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(
        _armed(),
        ExecutionRuntime(
            matchbook=MatchbookExecutionClient(matchbook),
            kalshi=KalshiExecutionClient(kalshi),
        ),
        ledger,
    )
    plan = _plan()
    ops._plans[plan.opportunity_id] = plan
    entered = threading.Event()
    release = threading.Event()
    original = ops._persist_live_package

    def paused(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    ops._persist_live_package = paused
    errors: list[BaseException] = []

    def run() -> None:
        try:
            ops.capture_opening_fill(plan.opportunity_id, now=NOW)
        except BaseException as exc:  # noqa: BLE001 — surface the worker failure
            errors.append(exc)

    worker = threading.Thread(target=run)
    worker.start()
    assert entered.wait(5)
    trade_id = paper_trade_id(plan.opportunity_id)
    assert ledger.trades.get(trade_id) is None

    def factory():
        return ops

    factory.cache_info = lambda: type("Cache", (), {"currsize": 1})()
    monkeypatch.setattr("sports_hedge.api.paper.get_paper_journal_holder", factory)
    import asyncio

    body = asyncio.run(health())
    assert ledger.trades.get(trade_id) is None
    assert body["orphaned_live_executions"][0]["classification"] == "orphaned_unresolved"
    assert body["orphaned_live_executions"][0]["reason"] == "awaiting_explicit_recovery"
    assert _orphaned_live_executions()[0]["package_id"] == f"{trade_id}:{OPENING_TRANCHE_ID}"
    blocked = ops.recover_orphaned_live_executions()
    assert ledger.trades.get(trade_id) is None
    assert blocked[0]["classification"] == "orphaned_unresolved"
    release.set()
    worker.join(5)
    assert errors == []
    saved = ledger.trades.get(trade_id)
    assert saved is not None
    assert saved.state is PaperTradeState.OPEN
    assert len(matchbook.calls) == 1
    assert len(kalshi.calls) == 1
    recorded = [
        event
        for event in saved.audit
        if event.event_type is PaperTradeAuditEventType.LIVE_PACKAGE_RECORDED
    ]
    assert len(recorded) == 1


def test_file_restart_recovers_one_trade_without_a_venue_call(tmp_path: Path) -> None:
    database = tmp_path / "paper.sqlite"
    ledger = SqlitePaperLedger(database)
    ops = _ops(
        _armed(),
        ExecutionRuntime(
            matchbook=MatchbookExecutionClient(DeterministicExecutionTransport()),
            kalshi=KalshiExecutionClient(DeterministicExecutionTransport()),
        ),
        ledger,
    )
    plan = _plan()
    ops._plans[plan.opportunity_id] = plan
    ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    trade_id = paper_trade_id(plan.opportunity_id)
    _erase_trade(ledger, trade_id)
    ledger._connection.close()
    set_execution_runtime(None)
    clear_orphan_reports()

    restarted_ledger = SqlitePaperLedger(database, auto_seed=False)
    quiet = _Calls()
    restarted = _ops(
        _armed(),
        ExecutionRuntime(
            matchbook=MatchbookExecutionClient(quiet),
            kalshi=KalshiExecutionClient(quiet),
        ),
        restarted_ledger,
    )
    reports = restarted.recover_orphaned_live_executions()
    rebuilt = restarted_ledger.trades.get(trade_id)
    assert rebuilt is not None
    assert rebuilt.state is PaperTradeState.OPEN
    assert rebuilt.places_orders is True
    assert reports[0]["classification"] == "reconstructed_active"
    assert quiet.calls == []
    restarted.recover_orphaned_live_executions()
    assert quiet.calls == []
    recorded = [
        event
        for event in restarted_ledger.trades.get(trade_id).audit
        if event.event_type is PaperTradeAuditEventType.LIVE_PACKAGE_RECORDED
    ]
    assert len(recorded) == 1
    assert restarted_ledger._connection.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 1


def test_file_restart_leaves_unknown_fill_unresolved(tmp_path: Path) -> None:
    database = tmp_path / "unknown.sqlite"
    ledger = SqlitePaperLedger(database)
    ops = _ops(
        _armed(),
        ExecutionRuntime(
            matchbook=MatchbookExecutionClient(_UnknownFill()),
            kalshi=KalshiExecutionClient(_UnknownFill()),
        ),
        ledger,
    )
    plan = _plan()
    ops._plans[plan.opportunity_id] = plan
    ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    trade_id = paper_trade_id(plan.opportunity_id)
    _erase_trade(ledger, trade_id)
    ledger._connection.close()
    set_execution_runtime(None)
    clear_orphan_reports()

    restarted_ledger = SqlitePaperLedger(database, auto_seed=False)
    quiet = _Calls()
    restarted = _ops(
        _armed(),
        ExecutionRuntime(
            matchbook=MatchbookExecutionClient(quiet),
            kalshi=KalshiExecutionClient(quiet),
        ),
        restarted_ledger,
    )
    reports = restarted.recover_orphaned_live_executions()
    assert restarted_ledger.trades.get(trade_id) is None
    assert reports[0]["classification"] == "orphaned_unresolved"
    assert reports[0]["reason"] == "fill_quantity_unknown"
    assert reports[0]["orders"][0]["filled_size"] is None
    restarted.recover_orphaned_live_executions()
    assert restarted_ledger.trades.get(trade_id) is None
    assert quiet.calls == []
    assert restarted_ledger._connection.execute("SELECT COUNT(*) FROM paper_trades").fetchone()[0] == 0
