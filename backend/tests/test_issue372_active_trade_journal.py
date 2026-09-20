"""Issue #372 — ACTIVE TRADE event journal, Why? correlation, partial-fill recovery.

PAPER / fixture clocks only. Logging never performs provider I/O. Unwind
metric/policy is not changed.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.paper import get_paper_operations_service
from sports_hedge.application.active_trade_lane import (
    ACTIVE_TRADE_LANE,
    reset_active_trade_registry,
)
from sports_hedge.application.active_trade_recovery import (
    recovery_legs_from_plan,
    residual_exposure_gbp,
    worst_case_settlement_pnl_gbp,
)
from sports_hedge.application.live_refresh import DualCadencePlan, LiveRefreshCoordinator
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.application.price_engine import PriceEngineItemStatus
from sports_hedge.application.provider_access import HEALTH_MARKET_TIMEOUT
from sports_hedge.application.venue_degradation_incident import build_venue_degradation_incident
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.active_trade_journal import ActiveTradeEventType, ActiveTradeReasonCode
from sports_hedge.paper.trades import (
    PaperActiveTradePhase,
    PaperSettlementRequest,
    PaperTradeState,
    PaperTradeTrancheKind,
)
from sports_hedge.paper.unwind import PaperUnwindEngine
from sports_hedge.persistence.operator_scanner_settings import (
    bind_runtime_operator_scanner_settings_store,
)
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from test_issue372_active_trade_lane import (
    _journal_keys,
    _lock_fingerprint,
    _raise_trade_cap,
)
from test_paper_trade_lifecycle import OBSERVED, _ops


def _types(ops: PaperOperationsService, trade_id: str) -> list[ActiveTradeEventType]:
    return [item.event_type for item in ops.query_active_trade_events(trade_id=trade_id, limit=500)]


def _reasons(ops: PaperOperationsService, trade_id: str) -> list[ActiveTradeReasonCode]:
    return [item.reason_code for item in ops.query_active_trade_events(trade_id=trade_id, limit=500)]


def test_hot_paper_promotion_creates_first_active_event(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        types = _types(ops, trade.trade_id)
        assert ActiveTradeEventType.PROMOTED_TO_ACTIVE in types
        assert ActiveTradeEventType.ENTRY_DECISION in types
        assert ActiveTradeEventType.ENTRY_FILL in types
        promoted = [
            item
            for item in ops.query_active_trade_events(trade_id=trade.trade_id)
            if item.event_type is ActiveTradeEventType.PROMOTED_TO_ACTIVE
        ]
        assert len(promoted) == 1
        assert promoted[0].dedupe_key == f"promoted:{trade.trade_id}"
        ops.record_active_lifecycle_event(
            trade,
            event_type=ActiveTradeEventType.PROMOTED_TO_ACTIVE,
            reason_code=ActiveTradeReasonCode.PROMOTED,
            operator_copy="duplicate promote",
            occurred_at=OBSERVED,
            dedupe_key=f"promoted:{trade.trade_id}",
        )
        assert (
            len(
                [
                    item
                    for item in ops.query_active_trade_events(trade_id=trade.trade_id)
                    if item.event_type is ActiveTradeEventType.PROMOTED_TO_ACTIVE
                ]
            )
            == 1
        )
    finally:
        repository.close()
        ledger.close()
        reset_active_trade_registry()


@pytest.mark.asyncio
async def test_two_successive_active_cycles_are_independently_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _raise_trade_cap(tmp_path, Decimal("2000"))
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    coordinator = None
    try:
        trade = ops.list_active_trades()[0]

        class _FakeEngine:
            async def _price_item(self, runtime, slice_result, *, lane=None):
                runtime.status = PriceEngineItemStatus.RETRY_WAIT
                return PriceEngineItemStatus.RETRY_WAIT

            async def drain_item_captures(self) -> None:
                return None

            def set_background_interval_seconds(self, _cadence: int) -> None:
                return None

            def restart(self) -> None:
                return None

        monkeypatch.setattr(
            "sports_hedge.api.paper.get_paper_operations_service",
            lambda *_args, **_kwargs: ops,
        )
        clock = {"now": OBSERVED}

        def _now() -> datetime:
            return clock["now"]

        coordinator = LiveRefreshCoordinator(clock=_now, price_engine=_FakeEngine())
        tick = DualCadencePlan(
            lane=ACTIVE_TRADE_LANE,
            reason="active_trade_due",
            identity_scope=[trade.trade_id],
        )
        await coordinator._run_active_trade_tick(tick)
        clock["now"] = OBSERVED + timedelta(seconds=5)
        await coordinator._run_active_trade_tick(tick)
        results = [
            item
            for item in ops.query_active_trade_events(trade_id=trade.trade_id, limit=500)
            if item.event_type is ActiveTradeEventType.ACTIVE_REFRESH_RESULT
        ]
        no_action = [
            item
            for item in ops.query_active_trade_events(
                trade_id=trade.trade_id,
                event_type=ActiveTradeEventType.NO_ACTION.value,
                limit=500,
            )
        ]
        assert len(results) == 2
        assert results[0].cycle_id != results[1].cycle_id
        assert {item.reason_code for item in results} == {ActiveTradeReasonCode.REFRESH_RETRY_WAIT}
        assert len(no_action) >= 2
        assert all(item.dedupe_key != no_action[0].dedupe_key for item in no_action[1:])
    finally:
        if coordinator is not None:
            coordinator.reset()
        repository.close()
        ledger.close()
        store.close()
        bind_runtime_operator_scanner_settings_store(None)
        reset_active_trade_registry()


@pytest.mark.asyncio
async def test_retry_wait_is_logged_and_cannot_trigger_stale_plan_buy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _raise_trade_cap(tmp_path, Decimal("2000"))
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    coordinator = None
    try:
        trade = ops.list_active_trades()[0]
        before_tranches = [item.tranche_id for item in trade.tranches]
        before_locks = _lock_fingerprint(ledger, trade.trade_id)
        before_capital = trade.capital_locked_gbp

        class _FakeEngine:
            async def _price_item(self, runtime, slice_result, *, lane=None):
                runtime.status = PriceEngineItemStatus.RETRY_WAIT
                return PriceEngineItemStatus.RETRY_WAIT

            async def drain_item_captures(self) -> None:
                return None

            def set_background_interval_seconds(self, _cadence: int) -> None:
                return None

            def restart(self) -> None:
                return None

        monkeypatch.setattr(
            "sports_hedge.api.paper.get_paper_operations_service",
            lambda *_args, **_kwargs: ops,
        )
        coordinator = LiveRefreshCoordinator(clock=lambda: OBSERVED, price_engine=_FakeEngine())
        await coordinator._run_active_trade_tick(
            DualCadencePlan(
                lane=ACTIVE_TRADE_LANE,
                reason="active_trade_due",
                identity_scope=[trade.trade_id],
            )
        )
        loaded = ops.list_active_trades()[0]
        assert [item.tranche_id for item in loaded.tranches] == before_tranches
        assert _lock_fingerprint(ledger, loaded.trade_id) == before_locks
        assert loaded.capital_locked_gbp == before_capital
        reasons = _reasons(ops, loaded.trade_id)
        assert ActiveTradeReasonCode.REFRESH_RETRY_WAIT in reasons
        assert ActiveTradeReasonCode.NO_ACTION_STALE_REFRESH in reasons
        assert ActiveTradeEventType.TOPUP_FILL not in _types(ops, loaded.trade_id)
    finally:
        if coordinator is not None:
            coordinator.reset()
        repository.close()
        ledger.close()
        store.close()
        bind_runtime_operator_scanner_settings_store(None)
        reset_active_trade_registry()


def test_successful_top_up_logs_decision_and_committed_fill(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    store = _raise_trade_cap(tmp_path, Decimal("2000"))
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        before_locks = _lock_fingerprint(ledger, trade.trade_id)
        result = ops.maybe_top_up_open_trade(trade, now=OBSERVED)
        assert result is not None
        loaded = ops.list_active_trades()[0]
        topups = [item for item in loaded.tranches if item.kind is PaperTradeTrancheKind.TOP_UP]
        assert len(topups) == 1
        types = _types(ops, loaded.trade_id)
        assert ActiveTradeEventType.TOPUP_DECISION in types
        assert ActiveTradeEventType.TOPUP_ATTEMPT in types
        assert ActiveTradeEventType.TOPUP_FILL in types
        fills = [
            item
            for item in ops.query_active_trade_events(trade_id=loaded.trade_id, limit=500)
            if item.event_type is ActiveTradeEventType.TOPUP_FILL
        ]
        assert fills
        assert {item.tranche_id for item in fills} == {topups[0].tranche_id}
        assert set(topups[0].fill_ids) <= {item.fill_id for item in fills}
        assert len(_lock_fingerprint(ledger, loaded.trade_id)) > len(before_locks)
        for item in fills:
            assert "treasury_after_gbp" in item.payload
    finally:
        repository.close()
        ledger.close()
        store.close()
        bind_runtime_operator_scanner_settings_store(None)
        reset_active_trade_registry()


def test_partial_entry_recovers_even_below_min_net_arb(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    store = _raise_trade_cap(tmp_path, Decimal("2000"))
    store.save_settings(
        min_net_edge=Decimal("0.99"),
        max_execution_risk=60,
        hot_cadence_seconds=30,
        max_allocated_per_trade_gbp=Decimal("2000"),
    )
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    try:
        opportunity_id = next(iter(ops._plans))
        real = ops.simulator
        calls = {"n": 0}

        class _OpeningOneSidedThenRecover:
            def simulate(self, *args, **kwargs):
                calls["n"] += 1
                filled = real.simulate(*args, **kwargs)
                if calls["n"] == 1 and filled.fills:
                    first = filled.fills[0]
                    rest = [
                        item.model_copy(update={"filled_stake": Decimal("0"), "fully_filled": False})
                        for item in filled.fills[1:]
                    ]
                    return filled.model_copy(
                        update={"fills": [first, *rest], "fully_filled": False}
                    )
                return filled

        ops.simulator = _OpeningOneSidedThenRecover()
        result = ops.simulate_fill(
            opportunity_id,
            simulate_external=True,
            now=OBSERVED,
        )
        loaded = ops.list_active_trades()[0]
        assert result.trade_id == loaded.trade_id
        types = _types(ops, loaded.trade_id)
        assert ActiveTradeEventType.ENTRY_PARTIAL_FILL in types
        assert ActiveTradeEventType.ENTRY_RECOVERY_DECISION in types
        assert ActiveTradeEventType.ENTRY_RECOVERY_FILL in types
        assert ActiveTradeEventType.ENTRY_RECOVERY_RESIDUAL in types
        assert ActiveTradeReasonCode.RECOVERY_BELOW_MIN_NET in _reasons(ops, loaded.trade_id)
        recoveries = [
            item for item in loaded.tranches if item.kind is PaperTradeTrancheKind.RECOVERY
        ]
        assert recoveries
        assert loaded.unresolved_recovery is False
        assert loaded.state is PaperTradeState.OPEN
        assert residual_exposure_gbp(loaded) == Decimal("0")
        assert worst_case_settlement_pnl_gbp(loaded) >= Decimal("0")
    finally:
        repository.close()
        ledger.close()
        store.close()
        bind_runtime_operator_scanner_settings_store(None)
        reset_active_trade_registry()


def test_bounded_recovery_leaves_durable_residual_and_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    store = _raise_trade_cap(tmp_path, Decimal("2000"))
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    try:
        opportunity_id = next(iter(ops._plans))
        real = ops.simulator
        calls = {"n": 0}

        class _TinyRecovery:
            def simulate(self, *args, **kwargs):
                calls["n"] += 1
                filled = real.simulate(*args, **kwargs)
                if calls["n"] == 1 and filled.fills:
                    first = filled.fills[0]
                    rest = [
                        item.model_copy(update={"filled_stake": Decimal("0"), "fully_filled": False})
                        for item in filled.fills[1:]
                    ]
                    return filled.model_copy(
                        update={"fills": [first, *rest], "fully_filled": False}
                    )
                tiny = [
                    item.model_copy(
                        update={
                            "filled_stake": max(item.filled_stake * Decimal("0.01"), Decimal("0.01")),
                            "fully_filled": False,
                        }
                    )
                    if item.filled_stake > 0
                    else item
                    for item in filled.fills
                ]
                return filled.model_copy(update={"fills": tiny, "fully_filled": False})

        ops.simulator = _TinyRecovery()
        ops.simulate_fill(opportunity_id, simulate_external=True, now=OBSERVED)
        loaded = ops.list_active_trades()[0]
        assert loaded.unresolved_recovery is True
        assert loaded.active_trade_phase is PaperActiveTradePhase.RECOVERING_PARTIAL_ENTRY
        assert (loaded.residual_exposure_gbp or Decimal("0")) > 0
        before_ids = [item.tranche_id for item in loaded.tranches]
        before_fills = [leg.fill_id for leg in loaded.legs]
        before_locks = _lock_fingerprint(ledger, loaded.trade_id)
        before_events = [
            (item.event_type, item.dedupe_key)
            for item in ops.query_active_trade_events(trade_id=loaded.trade_id, limit=500)
        ]
        ops.simulator = real
        ops.maybe_top_up_open_trade(
            loaded,
            now=OBSERVED + timedelta(seconds=5),
            plan=ops._plans[opportunity_id],
            require_current_plan=True,
        )
        again = ops.list_active_trades()[0]
        assert len(again.tranches) >= len(before_ids)
        retry_ids = [item.tranche_id for item in again.tranches]
        assert len(retry_ids) == len(set(retry_ids))
        assert len([leg.fill_id for leg in again.legs]) == len(
            set(leg.fill_id for leg in again.legs if leg.fill_id)
        )
        after_locks = _lock_fingerprint(ledger, again.trade_id)
        assert len(after_locks) >= len(before_locks)
        assert len({row[0] for row in after_locks}) == len(after_locks)
        events = ops.query_active_trade_events(trade_id=again.trade_id, limit=500)
        assert len({item.dedupe_key for item in events}) == len(events)
        assert before_fills
        assert before_events
    finally:
        repository.close()
        ledger.close()
        store.close()
        bind_runtime_operator_scanner_settings_store(None)
        reset_active_trade_registry()


def test_injected_journal_failure_rolls_back_finance_and_event(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    store = _raise_trade_cap(tmp_path, Decimal("2000"))
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        before_locks = _lock_fingerprint(ledger, trade.trade_id)
        before_journals = _journal_keys(ops)
        before_tranches = [item.tranche_id for item in trade.tranches]
        before_events = len(ops.query_active_trade_events(trade_id=trade.trade_id, limit=500))
        original = ops.ledger.active_trade_events.append

        def _boom(event):
            if event.event_type is ActiveTradeEventType.TOPUP_FILL:
                raise RuntimeError("injected_journal_failure")
            return original(event)

        ops.ledger.active_trade_events.append = _boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="injected_journal_failure"):
            ops.maybe_top_up_open_trade(trade, now=OBSERVED)
        ops.ledger.active_trade_events.append = original  # type: ignore[method-assign]
        rolled = ops.trades.get(trade.trade_id)
        assert rolled is not None
        assert [item.tranche_id for item in rolled.tranches] == before_tranches
        assert _lock_fingerprint(ledger, trade.trade_id) == before_locks
        assert _journal_keys(ops) == before_journals
        assert ActiveTradeEventType.TOPUP_FILL not in _types(ops, trade.trade_id)
        assert len(ops.query_active_trade_events(trade_id=trade.trade_id, limit=500)) == before_events
        retry = ops.maybe_top_up_open_trade(rolled, now=OBSERVED)
        assert retry is not None
        loaded = ops.list_active_trades()[0]
        assert any(item.kind is PaperTradeTrancheKind.TOP_UP for item in loaded.tranches)
        assert ActiveTradeEventType.TOPUP_FILL in _types(ops, loaded.trade_id)
        assert len(_lock_fingerprint(ledger, loaded.trade_id)) > len(before_locks)
    finally:
        repository.close()
        ledger.close()
        store.close()
        bind_runtime_operator_scanner_settings_store(None)
        reset_active_trade_registry()


def test_settlement_writes_terminal_event_with_final_economics(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        winning = next(leg.outcome for leg in trade.legs if leg.filled_stake > 0)
        settled = ops.settle(
            trade.trade_id,
            PaperSettlementRequest(
                winning_outcome=winning,
                source="fixture_test",
                source_id="active-journal-settle",
                settled_at=OBSERVED,
            ),
        )
        assert settled.state is PaperTradeState.CLOSED
        types = _types(ops, trade.trade_id)
        assert ActiveTradeEventType.SETTLED in types
        assert ActiveTradeEventType.CLOSED in types
        terminal = [
            item
            for item in ops.query_active_trade_events(trade_id=trade.trade_id, limit=500)
            if item.event_type is ActiveTradeEventType.SETTLED
        ][0]
        assert terminal.payload.get("realised_pnl_gbp") is not None
        assert terminal.payload.get("settlement_outcome") == winning
        again = ops.settle(
            trade.trade_id,
            PaperSettlementRequest(
                winning_outcome=winning,
                source="fixture_test",
                source_id="active-journal-settle",
                settled_at=OBSERVED,
            ),
        )
        assert again.state is PaperTradeState.CLOSED
        settled_events = [
            item
            for item in ops.query_active_trade_events(trade_id=trade.trade_id, limit=500)
            if item.event_type is ActiveTradeEventType.SETTLED
        ]
        assert len(settled_events) == 1
    finally:
        repository.close()
        ledger.close()
        reset_active_trade_registry()


def test_venue_degradation_why_captures_active_context_with_zero_provider_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        ops.record_active_lifecycle_event(
            trade,
            event_type=ActiveTradeEventType.ACTIVE_REFRESH_RESULT,
            reason_code=ActiveTradeReasonCode.REFRESH_RETRY_WAIT,
            operator_copy="ACTIVE refresh retry-wait",
            occurred_at=OBSERVED,
            dedupe_key=f"refresh-result:{trade.trade_id}:why",
            cycle_id=f"{trade.trade_id}:why",
            venue=VenueName.KALSHI.value,
        )
        provider_calls = {"n": 0}

        def _forbidden(*_args, **_kwargs):
            provider_calls["n"] += 1
            raise AssertionError("provider I/O during Why?")

        monkeypatch.setattr(
            "sports_hedge.api.paper.get_paper_operations_service",
            lambda *_args, **_kwargs: ops,
        )
        monkeypatch.setattr("sports_hedge.venues.kalshi.KalshiClient.get_event", _forbidden)
        monkeypatch.setattr("sports_hedge.venues.matchbook.MatchbookClient.get_market", _forbidden)
        coordinator = LiveRefreshCoordinator(clock=lambda: OBSERVED)
        coordinator.status = coordinator.status.model_copy(
            update={
                "venue_health": {"matchbook": "ok", "kalshi": HEALTH_MARKET_TIMEOUT, "polymarket": "ok"},
                "active_trade": coordinator.status.active_trade.model_copy(
                    update={
                        "venue_health": {"kalshi": HEALTH_MARKET_TIMEOUT, "matchbook": "ok"},
                        "operation_health": {"kalshi": {"order_book": HEALTH_MARKET_TIMEOUT}},
                        "worker_state": "degraded",
                        "degraded": True,
                    }
                ),
            }
        )
        refs = coordinator.observe_degradation_incidents(coordinator.status)
        assert "kalshi" in refs
        incident = coordinator.degradation_incident("kalshi")
        assert incident is not None
        assert "active_trade" in incident
        context = incident["active_trade_context"]
        assert context["provider_calls"] == 0
        assert trade.trade_id in context["trade_ids"]
        assert any(trade.trade_id in str(event_id) for event_id in context["event_ids"])
        assert provider_calls["n"] == 0
        why_src = inspect.getsource(LiveRefreshCoordinator._active_trade_degradation_context)
        assert ".list_events(" not in why_src
        assert ".list_markets(" not in why_src
        assert ".get_market(" not in why_src
        snapshot = build_venue_degradation_incident(
            coordinator.status,
            "kalshi",
            previous_health="ok",
            new_health=HEALTH_MARKET_TIMEOUT,
            active_trade_context=context,
        )
        assert snapshot["active_trade_context"]["provider_calls"] == 0
        observe_src = inspect.getsource(LiveRefreshCoordinator.observe_degradation_incidents)
        assert "list_events" not in observe_src
        assert "get_order_book" not in observe_src
    finally:
        repository.close()
        ledger.close()
        reset_active_trade_registry()


def test_query_endpoint_filters_and_is_read_only(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    client = TestClient(app)
    try:
        trade = ops.list_active_trades()[0]
        app.dependency_overrides[get_paper_operations_service] = lambda: ops
        listed = client.get(
            "/paper/active-trade-events",
            params={"trade_id": trade.trade_id, "limit": 50},
        )
        assert listed.status_code == 200
        body = listed.json()
        assert body
        assert all(item["trade_id"] == trade.trade_id for item in body)
        by_type = client.get(
            "/paper/active-trade-events",
            params={
                "trade_id": trade.trade_id,
                "event_type": ActiveTradeEventType.PROMOTED_TO_ACTIVE.value,
            },
        )
        assert by_type.status_code == 200
        assert by_type.json()
        assert all(item["event_type"] == "promoted_to_active" for item in by_type.json())
        by_reason = client.get(
            "/paper/active-trade-events",
            params={"reason_code": ActiveTradeReasonCode.PROMOTED.value},
        )
        assert by_reason.status_code == 200
        assert all(item["reason_code"] == "promoted" for item in by_reason.json())
        missing = client.get(
            "/paper/active-trade-events",
            params={"trade_id": "no-such-trade"},
        )
        assert missing.status_code == 200
        assert missing.json() == []
        posted = client.post("/paper/active-trade-events", json={})
        assert posted.status_code in {405, 422}
        endpoint_src = inspect.getsource(
            __import__("sports_hedge.api.paper", fromlist=["list_active_trade_events"]).list_active_trade_events
        )
        assert "list_events" not in endpoint_src
        assert "get_market" not in endpoint_src
        assert endpoint_src.count("@router.get") >= 0
    finally:
        app.dependency_overrides.pop(get_paper_operations_service, None)
        repository.close()
        ledger.close()
        reset_active_trade_registry()


def test_unwind_metric_and_policy_untouched() -> None:
    recovery_src = inspect.getsource(recovery_legs_from_plan)
    module_src = inspect.getsource(
        __import__("sports_hedge.application.active_trade_recovery", fromlist=["residual_exposure_gbp"])
    )
    tick_src = inspect.getsource(LiveRefreshCoordinator._run_active_trade_tick)
    engine_src = inspect.getsource(PaperUnwindEngine)
    for source in (recovery_src, module_src):
        assert "PaperUnwindEngine" not in source
        assert "evaluate_unwind" not in source
        assert "UnwindPolicy" not in source
    assert "evaluate_unwind" not in tick_src
    assert "ActiveTradeEvent" not in engine_src
    journal_src = inspect.getsource(
        __import__("sports_hedge.paper.active_trade_journal", fromlist=["ActiveTradeEvent"])
    )
    persist_src = inspect.getsource(
        __import__(
            "sports_hedge.persistence.active_trade_event_journal",
            fromlist=["SqliteActiveTradeEventJournal"],
        )
    )
    for source in (journal_src, persist_src):
        lowered = source.casefold()
        assert "list_events" not in lowered
        assert "get_market" not in lowered
        assert "get_order_book" not in lowered


def test_logging_never_adds_provider_io() -> None:
    tick_src = inspect.getsource(LiveRefreshCoordinator._run_active_trade_tick)
    record_src = inspect.getsource(PaperOperationsService.record_active_lifecycle_event)
    for source in (tick_src, record_src):
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith(("#", '"', "'"))
        )
        lowered = code.casefold()
        assert ".list_events(" not in lowered
        assert ".list_markets(" not in lowered
        assert "marketmatcher" not in lowered
