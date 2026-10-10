"""Wave 40 runtime arming: fake transports only. No venue I/O."""

from __future__ import annotations

import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.application.paper_operations import (
    PaperOperationsError,
    PaperOperationsService,
    paper_trade_id,
)
from sports_hedge.arbitrage.watchlist.economics import classification_for
from sports_hedge.arbitrage.watchlist.models import NearOpportunity, OpportunityStatus
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.execution.clients import (
    DeterministicExecutionTransport,
    MatchbookExecutionClient,
    PolymarketExecutionClient,
)
from sports_hedge.execution.live_canary import (
    LiveCanaryTradingFacts,
    arm_live_runtime,
    disarm_live_runtime,
    live_runtime_armed,
    public_canary_status,
    reset_live_canary_runtime,
    set_live_canary_trading_facts,
    set_live_runtime_arm,
)
from sports_hedge.execution.package import execution_capability, execution_armed
from sports_hedge.execution.runtime import ExecutionRuntime, set_execution_runtime
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.chain import PaperFillPlan
from sports_hedge.paper.fills import PaperOpportunityLeg
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision
from sports_hedge.persistence.operator_scanner_settings import (
    SqliteOperatorScannerSettingsStore,
    bind_runtime_operator_scanner_settings_store,
)
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger

NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
PERMITTED = LiveCanaryTradingFacts(
    polymarket_geoblock_permitted=True,
    polymarket_geoblock_reason="geoblock_permitted",
    polymarket_credentials_present=True,
    polymarket_wallet_valid=True,
    polymarket_balance_readable=True,
    polymarket_trading_ready=True,
    matchbook_credentials_present=True,
    matchbook_authenticated=True,
    matchbook_free_funds_positive=True,
    matchbook_currency_compatible=True,
    fee_fx_known=True,
)


@pytest.fixture(autouse=True)
def _reset_canary() -> None:
    set_execution_runtime(None)
    reset_live_canary_runtime()
    set_live_canary_trading_facts(None)
    bind_runtime_operator_scanner_settings_store(None)
    yield
    set_execution_runtime(None)
    reset_live_canary_runtime()
    set_live_canary_trading_facts(None)
    bind_runtime_operator_scanner_settings_store(None)


def _canary_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "sports_hedge_mode": "real",
        "sports_hedge_execution_enabled": False,
        "paper_autofill_enabled": False,
        "min_net_edge": 0.002,
        "max_event_gbp": 7,
        "max_opportunity_gbp": 7,
        "max_one_time_gbp": 5,
        "paper_settings_db_path": str(tmp_path / "operator.sqlite"),
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


def _arm(settings: Settings) -> None:
    set_live_canary_trading_facts(PERMITTED)
    set_live_runtime_arm(True)
    assert execution_armed(settings) is True


def _snapshot(*, stake: str, extra: dict | None = None, **fields: object) -> str:
    payload: dict[str, object] = {
        "snapshot_id": "exec:canary:1",
        "accepted": True,
        "skew_ms": 12,
        "max_skew_ms": 500,
        "oldest_quote_age_ms": 80,
        "max_quote_age_ms": 2000,
        "net_edge": "0.0025",
        "fee_snapshots": [{"venue": "matchbook", "known": True}],
        "venue_costs": [{"venue": "matchbook", "known": True}],
        "fx_rates": [{"currency": "GBP", "gbp_per_unit": "1"}],
        "frozen_orders": [
            {
                "venue": "matchbook",
                "native_event_id": "evt-mb",
                "native_market_id": "mb-mkt",
                "native_runner_id": "101",
                "native_side": "back",
                "ladder_odds": "2.10",
                "native_stake": stake,
                "currency": "GBP",
            },
            {
                "venue": "polymarket",
                "native_event_id": "evt-pm",
                "native_market_id": "pm-mkt",
                "native_runner_id": "token-yes",
                "order_type": "FAK",
                "limit_price": "0.55",
                "native_amount": stake,
                "native_shares": stake,
                "tick_size": "0.01",
                "minimum_order_size": "0.01",
                "currency": "GBP",
            },
        ],
    }
    payload.update(fields)
    if extra:
        payload.update(extra)
    import json

    return json.dumps(payload)


def _leg(venue: VenueName, *, runner: str, stake: str, event: str, market: str) -> PaperOpportunityLeg:
    return PaperOpportunityLeg(
        outcome="home" if venue is VenueName.MATCHBOOK else "away",
        venue=venue,
        source_market_id=market,
        source_runner_id=runner,
        source_event_id=event,
        currency="GBP",
        requested_stake=Decimal(stake),
        displayed_odds=Decimal("2.10") if venue is VenueName.MATCHBOOK else Decimal("1.90"),
    )


def _plan(
    opportunity_id: str = "watch:mkt-canary",
    *,
    stake: str = "1.50",
    snapshot: str | None = None,
    canonical_event_id: str = "evt-canonical",
) -> PaperFillPlan:
    legs = [
        _leg(VenueName.MATCHBOOK, runner="101", stake=stake, event="evt-mb", market="mb-mkt"),
        _leg(VenueName.POLYMARKET, runner="token-yes", stake=stake, event="evt-pm", market="pm-mkt"),
    ]
    return PaperFillPlan(
        opportunity_id=opportunity_id,
        canonical_event_id=canonical_event_id,
        canonical_market_id=opportunity_id.split(":", 1)[-1],
        scanned_at=NOW,
        eligible_for_paper_simulation=True,
        settlement_equivalent=True,
        legs=legs,
        fx_snapshots=[
            FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1"), source="test", captured_at=NOW)
        ],
        decision=PaperScanDecision(
            canonical_market_id=opportunity_id.split(":", 1)[-1],
            canonical_event_id=canonical_event_id,
            eligible_for_paper_simulation=True,
            market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=["test"]),
            solver_model="simple_complete_set",
            fill_legs=legs,
            scanned_at=NOW,
        ),
        execution_authoritative=True,
        execution_snapshot_json=snapshot or _snapshot(stake=stake),
        provenance="live_paper",
    )


def _register(ops: PaperOperationsService, plan: PaperFillPlan) -> None:
    ops.watchlist.repository.upsert_opportunity(
        NearOpportunity(
            opportunity_id=plan.opportunity_id,
            canonical_event_id=plan.canonical_event_id or "evt-canonical",
            canonical_market_id=plan.canonical_market_id or "mkt-canary",
            status=OpportunityStatus.TRIGGERED,
            classification=classification_for(OpportunityStatus.TRIGGERED),
            is_arbitrage=True,
            first_seen_at=NOW,
            last_seen_at=NOW,
        ),
        force_status=True,
    )
    ops._plans[plan.opportunity_id] = plan


def _ops(
    settings: Settings,
    runtime: ExecutionRuntime,
    ledger: SqlitePaperLedger,
    plan: PaperFillPlan,
) -> PaperOperationsService:
    watchlist = WatchlistService(SqliteWatchlistRepository())
    ops = PaperOperationsService(
        watchlist=watchlist,
        settings=settings,
        ledger=ledger,
        execution_runtime=runtime,
    )
    _register(ops, plan)
    return ops


def _runtime(
    mb: DeterministicExecutionTransport | None = None,
    pm: DeterministicExecutionTransport | None = None,
) -> tuple[ExecutionRuntime, DeterministicExecutionTransport, DeterministicExecutionTransport]:
    matchbook_t = mb or DeterministicExecutionTransport()
    polymarket_t = pm or DeterministicExecutionTransport()
    runtime = ExecutionRuntime(
        matchbook=MatchbookExecutionClient(matchbook_t),
        polymarket=PolymarketExecutionClient(polymarket_t),
    )
    set_execution_runtime(runtime)
    return runtime, matchbook_t, polymarket_t


def test_unarmed_and_real_dry_run_submit_nothing(tmp_path: Path) -> None:
    runtime, mb, pm = _runtime()
    dry = Settings(sports_hedge_mode="real", sports_hedge_execution_enabled=False, paper_autofill_enabled=False)
    ledger = SqlitePaperLedger(tmp_path / "dry.sqlite")
    plan = _plan()
    ops = _ops(dry, runtime, ledger, plan)
    try:
        ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    except PaperOperationsError:
        pass
    assert mb.calls == []
    assert pm.calls == []
    armed_only = Settings(sports_hedge_mode="real", sports_hedge_execution_enabled=True, paper_autofill_enabled=False)
    assert execution_armed(armed_only) is True
    ops2 = _ops(armed_only, runtime, SqlitePaperLedger(tmp_path / "armed.sqlite"), _plan("watch:other"))
    assert ops2._should_autofill(autofill=None, provenance=plan.provenance) is False


def test_two_opportunities_and_top_up_use_operator_limits(tmp_path: Path) -> None:
    settings = _canary_settings(tmp_path)
    _arm(settings)
    status = public_canary_status(settings)
    assert Decimal(status["max_event_gbp"]) == Decimal("7")
    assert Decimal(status["max_opportunity_gbp"]) == Decimal("7")
    assert Decimal(status["max_one_time_gbp"]) == Decimal("5")
    assert Decimal(status["min_net_edge"]) == Decimal("0.002")
    assert status["no_session_cap"] is True
    runtime, mb, pm = _runtime()
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    opening = _plan(stake="1.50")
    ops = _ops(settings, runtime, ledger, opening)
    ops.capture_opening_fill(opening.opportunity_id, now=NOW)
    assert len(mb.calls) == 1
    assert len(pm.calls) == 1
    topup = _plan(stake="1.00", snapshot=_snapshot(stake="1.00", snapshot_id="exec:canary:2"))
    ops._plans[opening.opportunity_id] = topup
    ops._dispatch_live_tranche(
        topup,
        trade_id=paper_trade_id(opening.opportunity_id),
        tranche_id="topup:exec:canary:2",
        now=NOW,
    )
    assert len(mb.calls) == 2
    assert len(pm.calls) == 2
    second = _plan(
        "watch:second",
        stake="1.50",
        snapshot=_snapshot(stake="1.50", snapshot_id="exec:other"),
        canonical_event_id="evt-2",
    )
    _register(ops, second)
    ops.capture_opening_fill(second.opportunity_id, now=NOW)
    assert len(mb.calls) == 3
    assert len(pm.calls) == 3


def test_persisted_operator_one_time_cap_is_authoritative(tmp_path: Path) -> None:
    store = SqliteOperatorScannerSettingsStore(tmp_path / "ops.sqlite")
    store.save_settings(
        min_net_edge=Decimal("0.002"),
        max_execution_risk=60,
        max_event_gbp=Decimal("10"),
        max_opportunity_gbp=Decimal("10"),
        max_one_time_gbp=Decimal("2"),
    )
    bind_runtime_operator_scanner_settings_store(store)
    settings = _canary_settings(tmp_path, max_event_gbp=7, max_opportunity_gbp=7, max_one_time_gbp=5)
    _arm(settings)
    assert Decimal(public_canary_status(settings)["max_one_time_gbp"]) == Decimal("2")
    runtime, mb, _pm = _runtime()
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "cap.sqlite"), _plan(stake="1.50"))
    ops.capture_opening_fill("watch:mkt-canary", now=NOW)
    assert mb.calls == []


def test_restart_disarms_but_same_package_does_not_duplicate(tmp_path: Path) -> None:
    settings = _canary_settings(tmp_path)
    _arm(settings)
    runtime, mb, _pm = _runtime()
    ledger_path = tmp_path / "p.sqlite"
    ops = _ops(settings, runtime, SqlitePaperLedger(ledger_path), _plan())
    ops.capture_opening_fill("watch:mkt-canary", now=NOW)
    first = len(mb.calls)
    assert first == 1
    reset_live_canary_runtime()
    assert live_runtime_armed() is False
    assert execution_armed(settings) is False
    restarted = _ops(settings, runtime, SqlitePaperLedger(ledger_path), _plan())
    try:
        restarted.capture_opening_fill("watch:mkt-canary", now=NOW)
    except PaperOperationsError:
        pass
    assert len(mb.calls) == first
    set_live_canary_trading_facts(PERMITTED)
    set_live_runtime_arm(True)
    restarted.capture_opening_fill("watch:mkt-canary", now=NOW)
    assert len(mb.calls) == first


def test_race_same_package_once_two_opportunities_both_run(tmp_path: Path) -> None:
    settings = _canary_settings(tmp_path)
    _arm(settings)
    mb = DeterministicExecutionTransport(delay_seconds=0.05)
    pm = DeterministicExecutionTransport(delay_seconds=0.05)
    runtime, _, _ = _runtime(mb, pm)
    plan = _plan("watch:same")
    ledger = SqlitePaperLedger(tmp_path / "same.sqlite")
    ops = _ops(settings, runtime, ledger, plan)
    threads = [
        threading.Thread(target=ops.capture_opening_fill, args=("watch:same",), kwargs={"now": NOW}),
        threading.Thread(target=ops.capture_opening_fill, args=("watch:same",), kwargs={"now": NOW}),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(mb.calls) == 1
    other = _plan("watch:b", snapshot=_snapshot(stake="1.50", snapshot_id="exec:b"), canonical_event_id="evt-b")
    _register(ops, other)
    ops.capture_opening_fill("watch:b", now=NOW)
    assert len(mb.calls) == 2


def test_unknown_ack_blocks_retry_after_restart(tmp_path: Path) -> None:
    settings = _canary_settings(tmp_path)
    _arm(settings)
    unknown = DeterministicExecutionTransport(unknown_ack=True)
    runtime, mb, _pm = _runtime(unknown, DeterministicExecutionTransport(unknown_ack=True))
    ledger_path = tmp_path / "u.sqlite"
    ops = _ops(settings, runtime, SqlitePaperLedger(ledger_path), _plan())
    ops.capture_opening_fill("watch:mkt-canary", now=NOW)
    assert mb.calls
    first = len(mb.calls)
    reset_live_canary_runtime()
    set_live_canary_trading_facts(PERMITTED)
    set_live_runtime_arm(True)
    restarted = _ops(settings, runtime, SqlitePaperLedger(ledger_path), _plan())
    restarted.capture_opening_fill("watch:mkt-canary", now=NOW)
    assert len(mb.calls) == first


def test_geoblock_and_missing_allowance_and_min_order(tmp_path: Path) -> None:
    runtime, mb, pm = _runtime()
    settings = _canary_settings(tmp_path)
    set_live_canary_trading_facts(
        LiveCanaryTradingFacts(
            polymarket_geoblock_permitted=False,
            polymarket_geoblock_reason="geoblock_blocked",
            polymarket_credentials_present=True,
            polymarket_wallet_valid=True,
            polymarket_balance_readable=True,
            polymarket_trading_ready=True,
            matchbook_credentials_present=True,
            matchbook_authenticated=True,
            matchbook_free_funds_positive=True,
        )
    )
    set_live_runtime_arm(True)
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "g.sqlite"), _plan())
    ops.capture_opening_fill("watch:mkt-canary", now=NOW)
    assert mb.calls == []
    set_live_canary_trading_facts(
        LiveCanaryTradingFacts(
            polymarket_geoblock_permitted=True,
            polymarket_geoblock_reason="geoblock_permitted",
            polymarket_credentials_present=True,
            polymarket_wallet_valid=True,
            polymarket_balance_readable=True,
            polymarket_trading_ready=False,
            polymarket_trading_reason="missing_exchange_v3_allowance",
            matchbook_credentials_present=True,
            matchbook_authenticated=True,
            matchbook_free_funds_positive=True,
        )
    )
    ops.capture_opening_fill("watch:mkt-canary", now=NOW)
    assert mb.calls == []
    set_live_canary_trading_facts(PERMITTED)
    heavy = _plan(
        snapshot=_snapshot(
            stake="1.50",
            extra={
                "frozen_orders": [
                    {
                        "venue": "matchbook",
                        "native_event_id": "evt-mb",
                        "native_market_id": "mb-mkt",
                        "native_runner_id": "101",
                        "native_side": "back",
                        "ladder_odds": "2.10",
                        "native_stake": "1.50",
                        "minimum_order_size": "8.00",
                        "currency": "GBP",
                    },
                    {
                        "venue": "polymarket",
                        "native_event_id": "evt-pm",
                        "native_market_id": "pm-mkt",
                        "native_runner_id": "token-yes",
                        "order_type": "FAK",
                        "limit_price": "0.55",
                        "native_amount": "1.50",
                        "native_shares": "1.50",
                        "tick_size": "0.01",
                        "minimum_order_size": "8.00",
                        "currency": "GBP",
                    },
                ]
            },
        )
    )
    ops._plans[heavy.opportunity_id] = heavy
    ops.capture_opening_fill(heavy.opportunity_id, now=NOW)
    assert mb.calls == []
    assert pm.calls == []


def test_stale_or_skewed_price2_does_not_submit(tmp_path: Path) -> None:
    settings = _canary_settings(tmp_path)
    _arm(settings)
    runtime, mb, _pm = _runtime()
    stale = _plan(snapshot=_snapshot(stake="1.50", oldest_quote_age_ms=5000))
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "s.sqlite"), stale)
    ops.capture_opening_fill(stale.opportunity_id, now=NOW)
    assert mb.calls == []
    skewed = _plan(snapshot=_snapshot(stake="1.50", skew_ms=800, snapshot_id="exec:skew"))
    ops._plans[skewed.opportunity_id] = skewed
    ops.capture_opening_fill(skewed.opportunity_id, now=NOW)
    assert mb.calls == []


def test_halt_and_explicit_stop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _canary_settings(tmp_path)
    _arm(settings)
    runtime, mb, _pm = _runtime()
    disarm_live_runtime(settings)
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "h.sqlite"), _plan())
    try:
        ops.capture_opening_fill("watch:mkt-canary", now=NOW)
    except PaperOperationsError:
        pass
    assert mb.calls == []
    monkeypatch.setattr("sports_hedge.api.live_canary.get_settings", lambda: settings)
    monkeypatch.setattr("sports_hedge.api.main.get_settings", lambda: settings)
    client = TestClient(app)
    halted = client.post("/live-canary/halt-new-orders").json()
    assert halted["halt_new_orders"] is True
    assert halted["halt_does_not_cancel_existing"] is True
    health = client.get("/health").json()
    assert health["paper_autofill_enabled"] is False
    assert health["live_canary"]["live_execution_ready_is_not_permission"] is True
    assert health["live_canary"]["runtime_armed"] is False
    ready = execution_capability(settings)
    assert ready["live_execution_ready"] is False or health["live_canary"]["trading_permission"] is False
    assert public_canary_status(settings)["trading_permission"] is False


def test_live_execution_ready_is_not_permission(tmp_path: Path) -> None:
    settings = _canary_settings(tmp_path)
    body = public_canary_status(settings)
    assert body["live_execution_ready_is_not_permission"] is True
    assert body["trading_permission"] is False
    assert "canary_unarmed" in body["not_ready_reasons"] or "execution_disabled" in body["not_ready_reasons"]


@pytest.mark.asyncio
async def test_runtime_arm_without_startup_execution_flag(tmp_path: Path) -> None:
    set_live_canary_trading_facts(PERMITTED)
    settings = Settings(
        sports_hedge_mode="real",
        sports_hedge_execution_enabled=False,
        paper_autofill_enabled=False,
        min_net_edge=0.002,
        max_event_gbp=7,
        max_opportunity_gbp=7,
        max_one_time_gbp=5,
    )
    assert execution_armed(settings) is False
    assert live_runtime_armed() is False
    refused = await arm_live_runtime(settings, phrase="nope")
    assert refused["runtime_armed"] is False
    result = await arm_live_runtime(settings, phrase="ARM LIVE CANARY")
    assert result["runtime_armed"] is True
    assert Decimal(str(result["max_one_time_gbp"])) == Decimal("5")
    assert execution_armed(settings) is True
    runtime, mb, pm = _runtime()
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "rt.sqlite"), _plan())
    ops.capture_opening_fill("watch:mkt-canary", now=NOW)
    assert mb.calls and pm.calls
    disarm_live_runtime(settings)
    assert execution_armed(settings) is False
    before = len(mb.calls)
    try:
        ops.capture_opening_fill("watch:mkt-canary", now=NOW)
    except PaperOperationsError:
        pass
    assert len(mb.calls) == before


def test_arm_rejects_nonlocal_origin(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    settings = _canary_settings(tmp_path)
    monkeypatch.setattr("sports_hedge.api.live_canary.get_settings", lambda: settings)
    client = TestClient(app)
    response = client.post(
        "/live-canary/arm",
        json={"phrase": "ARM LIVE CANARY"},
        headers={"origin": "https://evil.example"},
    )
    assert response.status_code == 403


def test_default_settings_keep_execution_off() -> None:
    settings = Settings()
    assert settings.sports_hedge_live_canary_enabled is False
    assert settings.sports_hedge_execution_enabled is False
    assert settings.paper_autofill_enabled is False
    assert live_runtime_armed() is False
    assert "session_cap_gbp" not in public_canary_status(settings)
    assert "opening_tranche_gbp" not in public_canary_status(settings)
