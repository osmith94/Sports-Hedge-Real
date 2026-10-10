"""Wave 40 live canary: fake transports only. No venue I/O."""

from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.application.paper_operations import PaperOperationsService, paper_trade_id
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
    OPENING_TRANCHE_GBP,
    SESSION_CAP_GBP,
    get_live_canary_store,
    public_canary_status,
    reset_live_canary_runtime,
    set_live_canary_trading_facts,
)
from sports_hedge.execution.package import execution_capability, execution_armed
from sports_hedge.execution.runtime import ExecutionRuntime, set_execution_runtime
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.chain import PaperFillPlan
from sports_hedge.paper.fills import PaperOpportunityLeg
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision
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
    yield
    set_execution_runtime(None)
    reset_live_canary_runtime()
    set_live_canary_trading_facts(None)


def _auth(tmp_path: Path, session_id: str = "canary-session") -> Path:
    path = tmp_path / "live-canary-authorization.json"
    path.write_text(
        json.dumps({"session_id": session_id, "phrase": "ARM LIVE CANARY", "confirmed_at": NOW.isoformat()}),
        encoding="utf-8",
    )
    return path


def _canary_settings(tmp_path: Path, **overrides: object) -> Settings:
    session_id = str(overrides.pop("sports_hedge_live_canary_session_id", "canary-session"))
    auth = _auth(tmp_path, session_id)
    values: dict[str, object] = {
        "sports_hedge_mode": "real",
        "sports_hedge_execution_enabled": True,
        "sports_hedge_live_canary_enabled": True,
        "sports_hedge_live_canary_operator_confirmed": True,
        "sports_hedge_live_canary_session_id": session_id,
        "sports_hedge_live_canary_authorization_path": str(auth),
        "sports_hedge_live_canary_db_path": str(tmp_path / "live_canary.sqlite"),
        "paper_autofill_enabled": False,
    }
    values.update(overrides)
    return Settings(**values)  # type: ignore[arg-type]


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
) -> PaperFillPlan:
    legs = [
        _leg(VenueName.MATCHBOOK, runner="101", stake=stake, event="evt-mb", market="mb-mkt"),
        _leg(VenueName.POLYMARKET, runner="token-yes", stake=stake, event="evt-pm", market="pm-mkt"),
    ]
    return PaperFillPlan(
        opportunity_id=opportunity_id,
        canonical_event_id="evt-canonical",
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
            canonical_event_id="evt-canonical",
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


def _ops(
    settings: Settings,
    runtime: ExecutionRuntime,
    ledger: SqlitePaperLedger,
    plan: PaperFillPlan,
) -> PaperOperationsService:
    watchlist = WatchlistService(SqliteWatchlistRepository())
    watchlist.repository.upsert_opportunity(
        NearOpportunity(
            opportunity_id=plan.opportunity_id,
            canonical_event_id="evt-canonical",
            canonical_market_id=plan.canonical_market_id or "mkt-canary",
            status=OpportunityStatus.TRIGGERED,
            classification=classification_for(OpportunityStatus.TRIGGERED),
            is_arbitrage=True,
            first_seen_at=NOW,
            last_seen_at=NOW,
        ),
        force_status=True,
    )
    ops = PaperOperationsService(
        watchlist=watchlist,
        settings=settings,
        ledger=ledger,
        execution_runtime=runtime,
    )
    ops._plans[plan.opportunity_id] = plan
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
    ops.capture_opening_fill(plan.opportunity_id, now=NOW)
    assert mb.calls == []
    assert pm.calls == []
    armed_only = Settings(sports_hedge_mode="real", sports_hedge_execution_enabled=True, paper_autofill_enabled=False)
    assert execution_armed(armed_only) is True
    ops2 = _ops(armed_only, runtime, SqlitePaperLedger(tmp_path / "armed.sqlite"), _plan("watch:other"))
    assert ops2._should_autofill(autofill=None, provenance=plan.provenance) is False


def test_opening_three_pounds_and_two_pound_top_up(tmp_path: Path) -> None:
    set_live_canary_trading_facts(PERMITTED)
    settings = _canary_settings(tmp_path)
    runtime, mb, pm = _runtime()
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    opening = _plan(stake="1.50")
    ops = _ops(settings, runtime, ledger, opening)
    ops.capture_opening_fill(opening.opportunity_id, now=NOW)
    assert len(mb.calls) == 1
    assert len(pm.calls) == 1
    assert sum(call.requested_size for call in mb.calls + pm.calls) == Decimal("3.00")
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
    snapshot = get_live_canary_store(settings).session_snapshot("canary-session")
    assert Decimal(snapshot["reserved_gbp"]) == SESSION_CAP_GBP
    second = _plan("watch:second", stake="1.50", snapshot=_snapshot(stake="1.50", snapshot_id="exec:other"))
    ops._plans[second.opportunity_id] = second
    ops.watchlist.repository.upsert_opportunity(
        NearOpportunity(
            opportunity_id=second.opportunity_id,
            canonical_event_id="evt-2",
            canonical_market_id="second",
            status=OpportunityStatus.TRIGGERED,
            classification=classification_for(OpportunityStatus.TRIGGERED),
            is_arbitrage=True,
            first_seen_at=NOW,
            last_seen_at=NOW,
        ),
        force_status=True,
    )
    before = len(mb.calls)
    ops.capture_opening_fill(second.opportunity_id, now=NOW)
    assert len(mb.calls) == before


def test_durable_cap_survives_new_store(tmp_path: Path) -> None:
    set_live_canary_trading_facts(PERMITTED)
    settings = _canary_settings(tmp_path)
    runtime, mb, _pm = _runtime()
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "p.sqlite"), _plan())
    ops.capture_opening_fill("watch:mkt-canary", now=NOW)
    assert mb.calls
    reset_live_canary_runtime()
    set_live_canary_trading_facts(PERMITTED)
    again = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "p2.sqlite"), _plan("watch:other"))
    before = len(mb.calls)
    again.capture_opening_fill("watch:other", now=NOW)
    assert len(mb.calls) == before


def test_race_single_opening_reservation(tmp_path: Path) -> None:
    set_live_canary_trading_facts(PERMITTED)
    settings = _canary_settings(tmp_path)
    mb = DeterministicExecutionTransport(delay_seconds=0.05)
    pm = DeterministicExecutionTransport(delay_seconds=0.05)
    runtime, _, _ = _runtime(mb, pm)
    plan_a = _plan("watch:a")
    plan_b = _plan("watch:b", snapshot=_snapshot(stake="1.50", snapshot_id="exec:b"))
    ops_a = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "a.sqlite"), plan_a)
    ops_b = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "b.sqlite"), plan_b)
    threads = [
        threading.Thread(target=ops_a.capture_opening_fill, args=("watch:a",), kwargs={"now": NOW}),
        threading.Thread(target=ops_b.capture_opening_fill, args=("watch:b",), kwargs={"now": NOW}),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert len(mb.calls) == 1
    assert len(pm.calls) == 1


def test_unknown_ack_blocks_retry_after_restart(tmp_path: Path) -> None:
    set_live_canary_trading_facts(PERMITTED)
    settings = _canary_settings(tmp_path)
    unknown = DeterministicExecutionTransport(unknown_ack=True)
    runtime, mb, pm = _runtime(unknown, DeterministicExecutionTransport(unknown_ack=True))
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "u.sqlite"), _plan())
    ops.capture_opening_fill("watch:mkt-canary", now=NOW)
    assert mb.calls
    first = len(mb.calls)
    reset_live_canary_runtime()
    set_live_canary_trading_facts(PERMITTED)
    restarted = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "u2.sqlite"), _plan())
    restarted.capture_opening_fill("watch:mkt-canary", now=NOW)
    assert len(mb.calls) == first
    snapshot = get_live_canary_store(settings).session_snapshot("canary-session")
    assert snapshot["blocked_reason"] == "canary_unknown_blocks_new"


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
                        "minimum_order_size": "4.00",
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
                        "minimum_order_size": "4.00",
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
    set_live_canary_trading_facts(PERMITTED)
    settings = _canary_settings(tmp_path)
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
    set_live_canary_trading_facts(PERMITTED)
    settings = _canary_settings(tmp_path)
    runtime, mb, _pm = _runtime()
    get_live_canary_store(settings).halt_new_orders("canary-session", reason="operator_halt_new_orders")
    ops = _ops(settings, runtime, SqlitePaperLedger(tmp_path / "h.sqlite"), _plan())
    ops.capture_opening_fill("watch:mkt-canary", now=NOW)
    assert mb.calls == []
    monkeypatch.setattr("sports_hedge.api.live_canary.get_settings", lambda: settings)
    monkeypatch.setattr("sports_hedge.api.main.get_settings", lambda: settings)
    client = TestClient(app)
    halted = client.post("/live-canary/halt-new-orders").json()
    assert halted["halt_new_orders"] is True
    assert halted["halt_does_not_cancel_existing"] is True
    health = client.get("/health").json()
    assert health["live_canary"]["enabled"] is True
    assert health["paper_autofill_enabled"] is False
    assert health["live_canary"]["live_execution_ready_is_not_permission"] is True
    ready = execution_capability(settings)
    assert ready["live_execution_ready"] is False or health["live_canary"]["trading_permission"] is False
    assert public_canary_status(settings)["trading_permission"] is False


def test_live_execution_ready_is_not_permission(tmp_path: Path) -> None:
    settings = _canary_settings(tmp_path, sports_hedge_live_canary_operator_confirmed=False)
    body = public_canary_status(settings)
    assert body["live_execution_ready_is_not_permission"] is True
    assert body["trading_permission"] is False
    assert "canary_operator_unconfirmed" in body["not_ready_reasons"]


def test_default_settings_keep_canary_off() -> None:
    settings = Settings()
    assert settings.sports_hedge_live_canary_enabled is False
    assert settings.sports_hedge_execution_enabled is False
    assert settings.paper_autofill_enabled is False
    assert OPENING_TRANCHE_GBP == Decimal("3.00")
    assert SESSION_CAP_GBP == Decimal("5.00")
