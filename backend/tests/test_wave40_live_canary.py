"""Wave 40 runtime arming: fake transports only. No venue I/O."""

from __future__ import annotations

import threading
from dataclasses import replace
from datetime import UTC, datetime, timedelta
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
    canary_submit_refusal,
    disarm_live_runtime,
    inspect_durable_unresolved_live_orders,
    live_runtime_armed,
    public_canary_status,
    prime_live_submit_facts,
    reset_live_canary_runtime,
    set_live_canary_trading_facts,
    set_live_runtime_arm,
    trading_permission_reasons,
    unresolved_live_orders_from_durable,
)
from sports_hedge.execution.package import execution_capability, execution_armed
from sports_hedge.execution.runtime import ExecutionRuntime, get_execution_runtime, set_execution_runtime
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
    injected=True,
)

LOCAL_ORIGIN = {"Origin": "http://127.0.0.1:3000"}
FROZEN_SUBMIT = datetime(2026, 10, 11, 13, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _reset_canary(monkeypatch: pytest.MonkeyPatch) -> None:
    set_execution_runtime(None)
    reset_live_canary_runtime()
    set_live_canary_trading_facts(None)
    bind_runtime_operator_scanner_settings_store(None)
    monkeypatch.setattr(
        "sports_hedge.execution.package.wall_clock_utc",
        lambda: FROZEN_SUBMIT,
    )
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
    evaluated = FROZEN_SUBMIT.isoformat()
    payload: dict[str, object] = {
        "snapshot_id": "exec:canary:1",
        "accepted": True,
        "evaluated_at": evaluated,
        "started_at": evaluated,
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


def test_live_pre_submit_refusal_is_durable_and_names_the_exact_gate(tmp_path: Path) -> None:
    """The 17:17 accepted-Price-2/no-attempt gap must not recur silently."""
    settings = _canary_settings(tmp_path)
    runtime, mb, pm = _runtime()
    _arm(settings)
    set_live_canary_trading_facts(
        replace(PERMITTED, polymarket_geoblock_permitted=False,
                polymarket_geoblock_reason="geoblock_blocked")
    )
    ledger = SqlitePaperLedger(tmp_path / "pre-submit-denied.sqlite")
    ops = _ops(settings, runtime, ledger, _plan())
    ops.capture_opening_fill("watch:mkt-canary", now=NOW)

    assert mb.calls == []
    assert pm.calls == []
    assert ledger.live_attempts.list_all() == []
    events = ops.watchlist.activity(limit=20, opportunity_id="watch:mkt-canary")
    refused = [item for item in events if item.event_type.value == "paper_fill_rejected"]
    assert refused
    assert "live_pre_submit:geoblock_blocked" in refused[0].detail
    assert not ops.list_active_trades()


def test_live_price2_expiry_records_reason_not_silent_return(tmp_path: Path) -> None:
    settings = _canary_settings(tmp_path)
    _arm(settings)
    runtime, mb, pm = _runtime()
    frozen = _snapshot(stake="1.50", evaluated_at="2026-10-10T12:00:00+00:00")
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "stale-submit.sqlite"),
               _plan(snapshot=frozen))
    ops.capture_opening_fill("watch:mkt-canary", now=NOW)

    assert mb.calls == [] and pm.calls == []
    events = ops.watchlist.activity(limit=20, opportunity_id="watch:mkt-canary")
    assert any(
        item.event_type.value == "paper_fill_rejected"
        and "live_pre_submit:live_submit_quote_age_elapsed" in item.detail
        for item in events
    )


def test_live_dispatch_never_repeats_remote_preflight_after_price2(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _canary_settings(tmp_path)
    _arm(settings)
    runtime, mb, pm = _runtime()
    def unexpected_preflight(*_args: object, **_kwargs: object):
        raise AssertionError("remote account read after fresh Price-2")
    monkeypatch.setattr(
        "sports_hedge.execution.live_canary.resolve_trading_facts",
        unexpected_preflight,
    )
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "one-read.sqlite"), _plan())
    ops.capture_opening_fill("watch:mkt-canary", now=NOW)
    assert len(mb.calls) == 1
    assert len(pm.calls) == 1


def test_readiness_collected_once_before_price2_and_denial_fails_closed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _canary_settings(tmp_path)
    set_live_runtime_arm(True)
    calls = {"count": 0}

    async def collect(_settings: Settings) -> LiveCanaryTradingFacts:
        calls["count"] += 1
        return replace(PERMITTED, injected=False, collected_at=datetime.now(UTC))

    monkeypatch.setattr(
        "sports_hedge.execution.live_canary.collect_live_canary_facts", collect
    )
    assert prime_live_submit_facts(settings) is not None
    assert prime_live_submit_facts(settings) is not None
    assert calls["count"] == 1

    reset_live_canary_runtime()
    set_live_runtime_arm(True)

    async def inaccessible(_settings: Settings) -> LiveCanaryTradingFacts:
        raise ConnectionError("account response timed out")

    monkeypatch.setattr(
        "sports_hedge.execution.live_canary.collect_live_canary_facts", inaccessible
    )
    failed = prime_live_submit_facts(settings)
    assert failed is not None and not failed.polymarket_geoblock_permitted
    assert "canary_preflight_unavailable" in trading_permission_reasons(settings, failed)


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
    halted = client.post("/live-canary/halt-new-orders", headers=LOCAL_ORIGIN).json()
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
    result = await arm_live_runtime(settings)
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
        json={},
        headers={"origin": "https://evil.example"},
    )
    assert response.status_code == 403
    substring = client.post(
        "/live-canary/arm",
        json={},
        headers={"Origin": "https://localhost.evil.example"},
    )
    assert substring.status_code == 403
    missing = client.post("/live-canary/arm", json={})
    assert missing.status_code == 403
    get_arm = client.get("/live-canary/arm", headers=LOCAL_ORIGIN)
    assert get_arm.status_code == 405
    assert live_runtime_armed() is False
    set_live_canary_trading_facts(
        LiveCanaryTradingFacts(
            polymarket_geoblock_permitted=False,
            polymarket_geoblock_reason="geoblock_blocked",
            injected=True,
        )
    )
    allowed = client.post("/live-canary/arm", json={}, headers=LOCAL_ORIGIN)
    assert allowed.status_code == 200
    assert allowed.json()["runtime_armed"] is True
    assert allowed.json()["trading_permission"] is False
    assert "geoblock_blocked" in allowed.json()["not_ready_reasons"]
    assert live_runtime_armed() is True


def test_default_settings_keep_execution_off() -> None:
    settings = Settings()
    assert settings.sports_hedge_live_canary_enabled is False
    assert settings.sports_hedge_execution_enabled is False
    assert settings.paper_autofill_enabled is False
    assert live_runtime_armed() is False
    assert "session_cap_gbp" not in public_canary_status(settings)
    assert "opening_tranche_gbp" not in public_canary_status(settings)


class _HttpishTransport(DeterministicExecutionTransport):
    test_only = False

    def __init__(self, settings: Settings, **_kwargs: object) -> None:
        super().__init__()
        self._settings = settings


class _FakeMatchbookHttp(_HttpishTransport):
    transport_kind = "matchbook_http"
    constructed = 0

    def __init__(self, settings: Settings, **kwargs: object) -> None:
        type(self).constructed += 1
        super().__init__(settings, **kwargs)


class _FakePolymarketHttp(_HttpishTransport):
    transport_kind = "polymarket_http"
    constructed = 0

    def __init__(self, settings: Settings, **kwargs: object) -> None:
        type(self).constructed += 1
        super().__init__(settings, **kwargs)


def test_health_off_then_arm_binds_transports_only_when_needed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from eth_account import Account

    _FakeMatchbookHttp.constructed = 0
    _FakePolymarketHttp.constructed = 0
    monkeypatch.setattr(
        "sports_hedge.execution.composition.MatchbookHttpExecutionTransport",
        _FakeMatchbookHttp,
    )
    monkeypatch.setattr(
        "sports_hedge.execution.composition.PolymarketHttpExecutionTransport",
        _FakePolymarketHttp,
    )
    key = tmp_path / "pm.key"
    key.write_text(Account.create().key.hex(), encoding="utf-8")
    settings = _canary_settings(
        tmp_path,
        matchbook_username="user",
        matchbook_password="secret",
        polymarket_private_key_path=str(key),
        polymarket_signature_type=0,
    )
    monkeypatch.setattr("sports_hedge.api.live_canary.get_settings", lambda: settings)
    monkeypatch.setattr("sports_hedge.api.main.get_settings", lambda: settings)
    set_live_canary_trading_facts(PERMITTED)
    client = TestClient(app)
    health = client.get("/health").json()
    assert health["live_canary"]["runtime_armed"] is False
    runtime = get_execution_runtime()
    assert runtime.matchbook is None
    assert runtime.polymarket is None
    assert _FakeMatchbookHttp.constructed == 0
    assert _FakePolymarketHttp.constructed == 0
    leftover = client.post(
        "/live-canary/arm",
        json={},
        headers=LOCAL_ORIGIN,
    )
    assert leftover.status_code == 200
    armed = leftover.json()
    assert armed["runtime_armed"] is True
    runtime = get_execution_runtime()
    # The ON switch itself must not construct venue transports.
    assert runtime.matchbook is None
    assert runtime.polymarket is None
    assert _FakeMatchbookHttp.constructed == 0
    assert _FakePolymarketHttp.constructed == 0
    # The real order path binds transports lazily, after the operator turns ON.
    from sports_hedge.execution.composition import bind_process_execution_runtime

    bind_process_execution_runtime(settings)
    assert runtime.explicit is False
    assert isinstance(runtime.matchbook, MatchbookExecutionClient)
    assert isinstance(runtime.polymarket, PolymarketExecutionClient)
    assert runtime.matchbook._transport.transport_kind == "matchbook_http"
    assert runtime.polymarket._transport.transport_kind == "polymarket_http"
    assert _FakeMatchbookHttp.constructed == 1
    assert _FakePolymarketHttp.constructed == 1
    first = _plan("watch:first", stake="1.50")
    ops = PaperOperationsService(
        watchlist=WatchlistService(SqliteWatchlistRepository()),
        settings=settings,
        ledger=SqlitePaperLedger(tmp_path / "prov.sqlite"),
    )
    _register(ops, first)
    ops.capture_opening_fill(first.opportunity_id, now=NOW)
    assert len(runtime.matchbook._transport.calls) == 1
    second = _plan(
        "watch:second",
        stake="1.50",
        snapshot=_snapshot(stake="1.50", snapshot_id="exec:second"),
        canonical_event_id="evt-2",
    )
    _register(ops, second)
    ops.capture_opening_fill(second.opportunity_id, now=NOW)
    assert len(runtime.matchbook._transport.calls) == 2
    client.post("/live-canary/disarm", headers=LOCAL_ORIGIN)
    assert live_runtime_armed() is False
    assert get_execution_runtime().matchbook is None
    assert get_execution_runtime().polymarket is None
    before = _FakeMatchbookHttp.constructed
    try:
        ops.capture_opening_fill(first.opportunity_id, now=NOW)
    except PaperOperationsError:
        pass
    assert _FakeMatchbookHttp.constructed == before
    reset_live_canary_runtime()
    assert live_runtime_armed() is False
    assert get_execution_runtime().matchbook is None


def test_stale_collected_facts_are_not_evergreen(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    stale = LiveCanaryTradingFacts(
        polymarket_geoblock_permitted=True,
        polymarket_geoblock_reason="geoblock_permitted",
        polymarket_credentials_present=True,
        polymarket_wallet_valid=True,
        polymarket_balance_readable=True,
        polymarket_trading_ready=True,
        matchbook_credentials_present=True,
        matchbook_authenticated=True,
        matchbook_free_funds_positive=True,
        collected_at=NOW - timedelta(minutes=10),
        injected=False,
    )
    refreshed = LiveCanaryTradingFacts(
        polymarket_geoblock_permitted=False,
        polymarket_geoblock_reason="geoblock_blocked",
        collected_at=NOW,
        injected=False,
    )

    async def _collect(_settings: Settings) -> LiveCanaryTradingFacts:
        return refreshed

    monkeypatch.setattr(
        "sports_hedge.execution.live_canary.collect_live_canary_facts",
        _collect,
    )
    settings = _canary_settings(tmp_path)
    set_live_canary_trading_facts(stale)
    set_live_runtime_arm(True)
    runtime, mb, _pm = _runtime()
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "stale.sqlite"), _plan())
    ops.capture_opening_fill("watch:mkt-canary", now=NOW)
    assert mb.calls == []


def test_official_matchbook_balance_without_currency_is_not_a_mismatch(tmp_path: Path) -> None:
    settings = _canary_settings(tmp_path)
    official = LiveCanaryTradingFacts(
        polymarket_geoblock_permitted=True,
        polymarket_geoblock_reason="geoblock_permitted",
        polymarket_credentials_present=True,
        polymarket_wallet_valid=True,
        polymarket_balance_readable=True,
        polymarket_trading_ready=True,
        matchbook_credentials_present=True,
        matchbook_authenticated=True,
        matchbook_free_funds_positive=True,
        matchbook_currency_compatible=None,
        injected=True,
    )
    set_live_canary_trading_facts(official)
    set_live_runtime_arm(True)
    reasons = trading_permission_reasons(settings, official)
    assert "matchbook_currency_unknown" not in reasons
    assert "matchbook_currency_mismatch" not in reasons
    assert canary_submit_refusal(_plan(), settings=settings, facts=official) is None
    runtime, mb, _pm = _runtime()
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "cur.sqlite"), _plan())
    ops.capture_opening_fill("watch:mkt-canary", now=NOW)
    assert mb.calls
    set_live_canary_trading_facts(PERMITTED)
    reasons_ok = trading_permission_reasons(settings, PERMITTED)
    assert "matchbook_currency_unknown" not in reasons_ok
    assert "matchbook_currency_mismatch" not in reasons_ok


def test_explicit_matchbook_currency_mismatch_denies_before_submit(tmp_path: Path) -> None:
    settings = _canary_settings(tmp_path)
    mismatch = LiveCanaryTradingFacts(
        polymarket_geoblock_permitted=True,
        polymarket_geoblock_reason="geoblock_permitted",
        polymarket_credentials_present=True,
        polymarket_wallet_valid=True,
        polymarket_balance_readable=True,
        polymarket_trading_ready=True,
        matchbook_credentials_present=True,
        matchbook_authenticated=True,
        matchbook_free_funds_positive=True,
        matchbook_currency_compatible=False,
        injected=True,
    )
    set_live_canary_trading_facts(mismatch)
    set_live_runtime_arm(True)
    reasons = trading_permission_reasons(settings, mismatch)
    assert "matchbook_currency_mismatch" in reasons
    assert canary_submit_refusal(_plan(), settings=settings, facts=mismatch) == "matchbook_currency_mismatch"
    runtime, mb, _pm = _runtime()
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "mismatch.sqlite"), _plan())
    ops.capture_opening_fill("watch:mkt-canary", now=NOW)
    assert mb.calls == []


def test_orphan_inspection_exception_denies(tmp_path: Path) -> None:
    settings = _canary_settings(tmp_path)
    failed = replace(PERMITTED, orphan_inspection_failed=True, outstanding_unknown_orders=True)
    set_live_canary_trading_facts(failed)
    set_live_runtime_arm(True)
    reasons = trading_permission_reasons(settings, failed)
    assert "orphan_inspection_failed" in reasons
    assert "outstanding_unknown_orders" in reasons
    runtime, mb, _pm = _runtime()
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "orph.sqlite"), _plan())
    ops.capture_opening_fill("watch:mkt-canary", now=NOW)
    assert mb.calls == []


def test_durable_unresolved_attempt_blocks_and_inspect_error_is_fail_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reserved = {"status": "reserved", "outcome": None, "package_id": "pkg-reserved"}
    completed_unknown = {"status": "completed", "outcome": "UNKNOWN", "package_id": "pkg-unknown"}
    reconstructed = {"status": "completed", "outcome": "FULLY_FILLED", "package_id": "pkg-ok"}
    assert unresolved_live_orders_from_durable([reserved], []) is True
    assert unresolved_live_orders_from_durable([completed_unknown], []) is True
    assert (
        unresolved_live_orders_from_durable(
            [reconstructed],
            [{"classification": "reconstructed_active", "live_fill_unknown": False}],
        )
        is False
    )
    assert (
        unresolved_live_orders_from_durable(
            [reconstructed],
            [{"classification": "orphaned_unresolved", "reason": "fill_quantity_unknown"}],
        )
        is True
    )

    def _raise_holder() -> object:
        raise RuntimeError("durable reader down")

    monkeypatch.setattr(
        "sports_hedge.api.paper.get_paper_journal_holder",
        _raise_holder,
    )
    outstanding, failed = inspect_durable_unresolved_live_orders()
    assert failed is True
    assert outstanding is True
