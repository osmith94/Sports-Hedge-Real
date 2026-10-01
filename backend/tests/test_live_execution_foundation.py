"""Live-execution seam beside paper fills. Transports are in-process only."""

from __future__ import annotations

import ast
import asyncio
import inspect
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest
from pydantic import ValidationError

from sports_hedge.config import Settings
from sports_hedge.domain.models import MarketSide, VenueName
from sports_hedge.execution.clients import (
    DeterministicExecutionTransport,
    KalshiExecutionClient,
    MatchbookExecutionClient,
)
from sports_hedge.execution.models import LivePackageOutcome, VenueOrderResult, VenueOrderStatus
from sports_hedge.execution.package import (
    LIVE_EXECUTION_TRANSPORT_UNAVAILABLE,
    execute_live_package,
    execution_capability,
)
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.chain import PaperFillPlan
from sports_hedge.paper.fills import PaperOpportunityLeg
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient

NOW = datetime(2026, 10, 1, 12, tzinfo=UTC)
REPO = Path(__file__).resolve().parents[2]
VENUES = REPO / "backend" / "src" / "sports_hedge" / "venues"
EXECUTION = REPO / "backend" / "src" / "sports_hedge" / "execution"
PAPER_OPS = REPO / "backend" / "src" / "sports_hedge" / "application" / "paper_operations.py"


def _leg(
    venue: VenueName,
    *,
    event_id: str = "evt-1",
    market_id: str = "mkt-1",
    runner_id: str = "runner-1",
    stake: str = "4",
    odds: str = "2.10",
) -> PaperOpportunityLeg:
    return PaperOpportunityLeg(
        outcome="home",
        venue=venue,
        source_market_id=market_id,
        source_runner_id=runner_id,
        source_event_id=event_id,
        currency="GBP",
        requested_stake=Decimal(stake),
        displayed_odds=Decimal(odds),
    )


def _plan(*legs: PaperOpportunityLeg, accepted: bool = True, authoritative: bool = True) -> PaperFillPlan:
    return PaperFillPlan(
        opportunity_id="opp-live-1",
        canonical_event_id="evt-1",
        canonical_market_id="mkt-1",
        scanned_at=NOW,
        eligible_for_paper_simulation=True,
        settlement_equivalent=True,
        legs=list(legs),
        decision=PaperScanDecision(
            market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=["test"]),
            solver_model="simple_complete_set",
        ),
        execution_authoritative=authoritative,
        execution_snapshot_json=(
            '{"snapshot_id":"exec:test:1","accepted":true}'
            if accepted
            else '{"snapshot_id":"exec:test:1","accepted":false}'
        ),
    )


def _armed() -> Settings:
    return Settings(sports_hedge_mode="real", sports_hedge_execution_enabled=True)


def _clients(
    matchbook: DeterministicExecutionTransport | None = None,
    kalshi: DeterministicExecutionTransport | None = None,
) -> tuple[MatchbookExecutionClient, KalshiExecutionClient]:
    return (
        MatchbookExecutionClient(matchbook or DeterministicExecutionTransport()),
        KalshiExecutionClient(kalshi or DeterministicExecutionTransport()),
    )


def test_execution_is_disabled_by_default() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    real = Settings(sports_hedge_mode="real")
    assert real.sports_hedge_execution_enabled is False
    with pytest.raises(ValidationError, match="Live execution is intentionally unavailable"):
        Settings(sports_hedge_execution_enabled=True)


def test_real_mode_can_arm_execution_without_changing_the_default() -> None:
    armed = _armed()
    assert armed.sports_hedge_mode == "real"
    assert armed.sports_hedge_execution_enabled is True
    assert Settings().sports_hedge_execution_enabled is False


def test_read_only_clients_have_no_order_methods() -> None:
    for venue_cls in (MatchbookClient, KalshiClient):
        assert not hasattr(venue_cls, "place_order")
        assert not hasattr(venue_cls, "cancel_order")
        assert not hasattr(venue_cls, "dispatch")
        assert venue_cls.capabilities.execution_enabled is False
    assert MatchbookExecutionClient is not MatchbookClient
    assert KalshiExecutionClient is not KalshiClient
    assert not issubclass(MatchbookExecutionClient, MatchbookClient)
    assert not issubclass(KalshiExecutionClient, KalshiClient)
    matchbook_source = (VENUES / "matchbook.py").read_text(encoding="utf-8")
    kalshi_source = (VENUES / "kalshi.py").read_text(encoding="utf-8")
    for token in ("place_order", "cancel_order", "submit_order", "class MatchbookExecutionClient"):
        assert token not in matchbook_source
    for token in ("place_order", "cancel_order", "submit_order", "class KalshiExecutionClient"):
        assert token not in kalshi_source


def test_live_clients_are_separate_from_market_data_clients() -> None:
    matchbook_transport = DeterministicExecutionTransport()
    kalshi_transport = DeterministicExecutionTransport()
    matchbook = MatchbookExecutionClient(matchbook_transport)
    kalshi = KalshiExecutionClient(kalshi_transport)
    assert matchbook.venue is VenueName.MATCHBOOK
    assert kalshi.venue is VenueName.KALSHI
    assert inspect.getfile(MatchbookExecutionClient) != inspect.getfile(MatchbookClient)
    assert inspect.getfile(KalshiExecutionClient) != inspect.getfile(KalshiClient)


@pytest.mark.asyncio
async def test_disabled_execution_does_not_dispatch() -> None:
    matchbook_transport = DeterministicExecutionTransport()
    kalshi_transport = DeterministicExecutionTransport()
    matchbook, kalshi = _clients(matchbook_transport, kalshi_transport)
    plan = _plan(
        _leg(VenueName.MATCHBOOK, market_id="mb-m", runner_id="mb-r"),
        _leg(VenueName.KALSHI, market_id="kx-m", runner_id="kx-r", odds="1.90"),
    )
    result = await execute_live_package(
        plan,
        trade_id="trade-1",
        tranche_id="tranche-1",
        settings=Settings(),
        matchbook=matchbook,
        kalshi=kalshi,
    )
    assert result.outcome is LivePackageOutcome.FAILED
    assert result.detail == "execution_disabled"
    assert result.orders == []
    assert matchbook_transport.calls == []
    assert kalshi_transport.calls == []

    named = await execute_live_package(
        plan,
        trade_id="trade-1",
        tranche_id="tranche-1",
        settings=Settings(sports_hedge_mode="real"),
        matchbook=matchbook,
        kalshi=kalshi,
    )
    assert named.detail == "execution_disabled"
    assert matchbook_transport.calls == []


@pytest.mark.asyncio
async def test_unaccepted_plan_is_not_dispatched() -> None:
    transport = DeterministicExecutionTransport()
    matchbook, kalshi = _clients(transport, transport)
    result = await execute_live_package(
        _plan(_leg(VenueName.MATCHBOOK), accepted=False),
        trade_id="trade-1",
        tranche_id="tranche-1",
        settings=_armed(),
        matchbook=matchbook,
        kalshi=kalshi,
    )
    assert result.outcome is LivePackageOutcome.FAILED
    assert result.detail == "decision_not_accepted"
    assert transport.calls == []


@pytest.mark.asyncio
async def test_package_legs_dispatch_concurrently() -> None:
    release = asyncio.Event()
    entered = 0
    calls: list[str] = []

    class BarrierTransport:
        async def dispatch(self, request):
            nonlocal entered
            entered += 1
            calls.append(request.client_order_id)
            if entered >= 2:
                release.set()
            await asyncio.wait_for(release.wait(), timeout=1)
            now = datetime.now(UTC)
            return VenueOrderResult(
                venue=request.venue,
                client_order_id=request.client_order_id,
                venue_order_id=f"mock:{request.client_order_id}",
                status=VenueOrderStatus.FILLED,
                requested_size=request.requested_size,
                filled_size=request.requested_size,
                requested_price=request.requested_price,
                average_fill_price=request.requested_price,
                submitted_at=now,
                updated_at=now,
            )

    transport = BarrierTransport()
    result = await execute_live_package(
        _plan(
            _leg(VenueName.MATCHBOOK, market_id="mb-m", runner_id="mb-r"),
            _leg(VenueName.KALSHI, market_id="kx-m", runner_id="kx-r"),
        ),
        trade_id="trade-1",
        tranche_id="tranche-1",
        settings=_armed(),
        matchbook=MatchbookExecutionClient(transport),
        kalshi=KalshiExecutionClient(transport),
    )
    assert entered == 2
    assert len(calls) == 2
    assert result.outcome is LivePackageOutcome.FULLY_FILLED
    assert all(order.status is VenueOrderStatus.FILLED for order in result.orders)


@pytest.mark.asyncio
async def test_full_partial_and_failed_packages() -> None:
    plan = _plan(
        _leg(VenueName.MATCHBOOK, market_id="mb-m", runner_id="mb-r", stake="5", odds="2.20"),
        _leg(VenueName.KALSHI, market_id="kx-m", runner_id="kx-r", stake="6", odds="1.80"),
    )

    async def run(matchbook_fill: str, kalshi_fill: str, *, fail_kalshi: bool = False):
        matchbook, kalshi = _clients(
            DeterministicExecutionTransport(filled_fraction=Decimal(matchbook_fill)),
            DeterministicExecutionTransport(
                filled_fraction=Decimal(kalshi_fill),
                fail=fail_kalshi,
            ),
        )
        return await execute_live_package(
            plan,
            trade_id="trade-9",
            tranche_id="tranche-2",
            settings=_armed(),
            matchbook=matchbook,
            kalshi=kalshi,
        )

    full = await run("1", "1")
    assert full.outcome is LivePackageOutcome.FULLY_FILLED
    assert [order.filled_size for order in full.orders] == [Decimal(5), Decimal(6)]
    assert full.orders[0].requested_price == Decimal("2.20")
    assert full.orders[0].venue is VenueName.MATCHBOOK
    assert full.orders[1].venue is VenueName.KALSHI
    assert full.orders[0].average_fill_price == Decimal("2.20")
    assert str(full.orders[0].venue_order_id).startswith("mock:")

    partial = await run("1", "0.5")
    assert partial.outcome is LivePackageOutcome.PARTIAL
    assert partial.orders[0].status is VenueOrderStatus.FILLED
    assert partial.orders[1].status is VenueOrderStatus.PARTIAL
    assert partial.orders[1].filled_size == Decimal("3.0")

    one_sided = await run("1", "0", fail_kalshi=True)
    assert one_sided.outcome is LivePackageOutcome.PARTIAL
    assert one_sided.orders[1].status is VenueOrderStatus.FAILED
    assert one_sided.orders[1].filled_size == Decimal(0)
    assert one_sided.orders[1].venue_order_id is None

    matchbook_zero, kalshi_zero = _clients(
        DeterministicExecutionTransport(fail=True),
        DeterministicExecutionTransport(fail=True),
    )
    failed = await execute_live_package(
        plan,
        trade_id="trade-9",
        tranche_id="tranche-2",
        settings=_armed(),
        matchbook=matchbook_zero,
        kalshi=kalshi_zero,
    )
    assert failed.outcome is LivePackageOutcome.FAILED
    assert all(order.filled_size == 0 for order in failed.orders)


@pytest.mark.asyncio
async def test_requests_keep_native_ids_side_and_stable_client_order_id() -> None:
    seen: list = []

    class Capture:
        async def dispatch(self, request):
            seen.append(request)
            now = datetime.now(UTC)
            return VenueOrderResult(
                venue=request.venue,
                client_order_id=request.client_order_id,
                venue_order_id="mock:1",
                status=VenueOrderStatus.FILLED,
                requested_size=request.requested_size,
                filled_size=request.requested_size,
                requested_price=request.requested_price,
                average_fill_price=request.requested_price,
                submitted_at=now,
                updated_at=now,
            )

    plan = _plan(_leg(VenueName.MATCHBOOK, event_id="mb-evt", market_id="mb-mkt", runner_id="mb-run"))
    transport = Capture()
    client = MatchbookExecutionClient(transport)
    first = await execute_live_package(
        plan,
        trade_id="trade-1",
        tranche_id="tranche-1",
        settings=_armed(),
        matchbook=client,
    )
    second = await execute_live_package(
        plan,
        trade_id="trade-1",
        tranche_id="tranche-1",
        settings=_armed(),
        matchbook=client,
    )
    request = seen[0]
    assert request.native_event_id == "mb-evt"
    assert request.native_market_id == "mb-mkt"
    assert request.native_runner_id == "mb-run"
    assert request.side is MarketSide.BACK
    assert request.requested_price == Decimal("2.10")
    assert request.requested_size == Decimal(4)
    assert request.client_order_id == seen[1].client_order_id
    assert first.orders[0].client_order_id == second.orders[0].client_order_id


@pytest.mark.asyncio
async def test_no_external_write_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    def refuse(*_args, **_kwargs):
        raise AssertionError("execution test opened an HTTP client")

    monkeypatch.setattr(httpx.AsyncClient, "__init__", refuse)
    monkeypatch.setattr(httpx.Client, "__init__", refuse)
    matchbook, kalshi = _clients()
    result = await execute_live_package(
        _plan(
            _leg(VenueName.MATCHBOOK, market_id="mb-m", runner_id="mb-r"),
            _leg(VenueName.KALSHI, market_id="kx-m", runner_id="kx-r"),
        ),
        trade_id="trade-1",
        tranche_id="tranche-1",
        settings=_armed(),
        matchbook=matchbook,
        kalshi=kalshi,
    )
    assert result.outcome is LivePackageOutcome.FULLY_FILLED
    for path in EXECUTION.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported = [
            alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.Import)
            for alias in node.names
        ]
        imported.extend(
            node.module or ""
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom)
        )
        assert "httpx" not in imported
        assert "requests" not in imported


@pytest.mark.asyncio
async def test_armed_real_execution_without_injected_transport_fails_closed() -> None:
    armed = _armed()
    plan = _plan(
        _leg(VenueName.MATCHBOOK, market_id="mb-m", runner_id="mb-r"),
        _leg(VenueName.KALSHI, market_id="kx-m", runner_id="kx-r"),
    )
    result = await execute_live_package(
        plan,
        trade_id="trade-1",
        tranche_id="tranche-1",
        settings=armed,
    )
    assert result.outcome is LivePackageOutcome.FAILED
    assert result.detail == LIVE_EXECUTION_TRANSPORT_UNAVAILABLE
    assert result.orders == []
    assert execution_capability(armed) == {
        "configured_execution_enabled": True,
        "live_execution_ready": False,
        "execution_transport": "unavailable",
        "scanner_execution": "paper",
    }
    assert "DeterministicExecutionTransport" not in (EXECUTION / "package.py").read_text(encoding="utf-8")
    assert inspect.signature(MatchbookExecutionClient.__init__).parameters["transport"].default is (
        inspect.Parameter.empty
    )
    assert inspect.signature(KalshiExecutionClient.__init__).parameters["transport"].default is (
        inspect.Parameter.empty
    )


def test_health_separates_configured_execution_from_live_capability(monkeypatch: pytest.MonkeyPatch) -> None:
    from fastapi.testclient import TestClient

    from sports_hedge.api.main import app

    monkeypatch.setattr("sports_hedge.api.main.get_settings", lambda: Settings())
    paper = TestClient(app).get("/health").json()
    assert paper["execution_enabled"] is False
    assert paper["execution"]["configured_execution_enabled"] is False
    assert paper["execution"]["live_execution_ready"] is False
    assert paper["execution"]["scanner_execution"] == "paper"

    monkeypatch.setattr(
        "sports_hedge.api.main.get_settings",
        lambda: Settings(sports_hedge_mode="real", sports_hedge_execution_enabled=True),
    )
    body = TestClient(app).get("/health").json()
    assert body["mode"] == "real"
    assert body["execution_enabled"] is True
    assert body["execution"] == {
        "configured_execution_enabled": True,
        "live_execution_ready": False,
        "execution_transport": "unavailable",
        "scanner_execution": "paper",
    }


def test_paper_fill_path_is_not_wired_to_live_dispatch() -> None:
    source = PAPER_OPS.read_text(encoding="utf-8")
    assert "def simulate_fill(" in source
    assert "execute_live_package" not in source
    assert "MatchbookExecutionClient" not in source
    assert "KalshiExecutionClient" not in source
