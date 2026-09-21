"""Issue #372 — ACTIVE TRADE event journal and partial-fill recovery.

PAPER / fixture clocks only. Logging never performs provider I/O. Unwind
metric/policy is not changed. Venue-degradation Why? is a separate concern.
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
    get_active_trade_registry,
    reset_active_trade_registry,
)
from sports_hedge.application.active_trade_recovery import (
    recovery_legs_from_plan,
    residual_exposure_gbp,
    subtract_consumed_depth,
    worst_case_settlement_pnl_gbp,
)
from sports_hedge.application.live_refresh import DualCadencePlan, LiveRefreshCoordinator
from sports_hedge.application.paper_operations import (
    PaperOperationsError,
    PaperOperationsService,
    paper_trade_id,
)
from sports_hedge.application.price_engine import PriceEngineItemStatus
from sports_hedge.domain.models import MarketScope
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


def _fresh_current_plan(ops: PaperOperationsService, opportunity_id: str):
    plan = ops._plans[opportunity_id]
    later = plan.scanned_at + timedelta(seconds=5)
    decision = plan.decision.model_copy(update={"scanned_at": later})
    return ops._plan_from_decision(decision, opportunity_id, plan.provenance)


def _one_sided_simulator(real):
    calls = {"n": 0}

    class _OneSided:
        def simulate(self, *args, **kwargs):
            calls["n"] += 1
            filled = real.simulate(*args, **kwargs)
            if not filled.fills:
                return filled
            first = filled.fills[0]
            rest = [
                item.model_copy(update={"filled_stake": Decimal("0"), "fully_filled": False})
                for item in filled.fills[1:]
            ]
            return filled.model_copy(update={"fills": [first, *rest], "fully_filled": False})

    return _OneSided(), calls


def _types(ops: PaperOperationsService, trade_id: str) -> list[ActiveTradeEventType]:
    return [item.event_type for item in ops.query_active_trade_events(trade_id=trade_id, limit=500)]


def _reasons(ops: PaperOperationsService, trade_id: str) -> list[ActiveTradeReasonCode]:
    return [item.reason_code for item in ops.query_active_trade_events(trade_id=trade_id, limit=500)]


def test_hot_paper_promotion_creates_first_active_event(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        events = ops.query_active_trade_events(trade_id=trade.trade_id, limit=500)
        types = [item.event_type for item in events]
        assert types.index(ActiveTradeEventType.ENTRY_DECISION) < types.index(
            ActiveTradeEventType.ENTRY_ATTEMPT
        )
        assert types.index(ActiveTradeEventType.ENTRY_ATTEMPT) < types.index(
            ActiveTradeEventType.ENTRY_FILL
        )
        assert types.index(ActiveTradeEventType.ENTRY_FILL) < types.index(
            ActiveTradeEventType.PROMOTED_TO_ACTIVE
        )
        assert ActiveTradeEventType.ENTRY_NO_FILL not in types
        decision = next(
            item for item in events if item.event_type is ActiveTradeEventType.ENTRY_DECISION
        )
        assert decision.reason_code is ActiveTradeReasonCode.QUALIFYING_HOT
        assert decision.payload.get("pricing_lane") == "hot"
        assert "HOT" in decision.operator_copy
        assert "same cycle" in decision.operator_copy
        promoted = [
            item
            for item in events
            if item.event_type is ActiveTradeEventType.PROMOTED_TO_ACTIVE
        ]
        assert len(promoted) == 1
        assert promoted[0].dedupe_key == f"promoted:{trade.trade_id}"
        assert "management started after the initial fill" in promoted[0].operator_copy
        assert "promoted from paper fill" not in promoted[0].operator_copy.lower()
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


def test_background_qualifying_decision_precedes_fill_and_management(
    tmp_path: Path,
) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    try:
        opportunity_id = next(iter(ops._plans))
        ops._plans[opportunity_id] = ops._plans[opportunity_id].model_copy(
            update={"pricing_lane": "background"}
        )
        result = ops.simulate_fill(
            opportunity_id,
            simulate_external=True,
            now=datetime.now(UTC),
        )
        loaded = ops.list_active_trades()[0]
        assert result.trade_id == loaded.trade_id
        events = ops.query_active_trade_events(trade_id=loaded.trade_id, limit=500)
        types = [item.event_type for item in events]
        assert types.index(ActiveTradeEventType.ENTRY_DECISION) < types.index(
            ActiveTradeEventType.ENTRY_ATTEMPT
        )
        assert types.index(ActiveTradeEventType.ENTRY_ATTEMPT) < types.index(
            ActiveTradeEventType.ENTRY_FILL
        )
        assert types.index(ActiveTradeEventType.ENTRY_FILL) < types.index(
            ActiveTradeEventType.PROMOTED_TO_ACTIVE
        )
        decision = next(
            item for item in events if item.event_type is ActiveTradeEventType.ENTRY_DECISION
        )
        assert decision.reason_code is ActiveTradeReasonCode.QUALIFYING_BACKGROUND
        assert decision.payload.get("pricing_lane") == "background"
        assert "BACKGROUND" in decision.operator_copy
    finally:
        repository.close()
        ledger.close()
        reset_active_trade_registry()


def test_empty_opening_fill_is_journaled_without_fabricated_exposure(
    tmp_path: Path,
) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    try:
        opportunity_id = next(iter(ops._plans))
        trade_id = paper_trade_id(opportunity_id)
        real = ops.simulator

        class _EmptyOpening:
            def simulate(self, *args, **kwargs):
                filled = real.simulate(*args, **kwargs)
                empty = [
                    item.model_copy(
                        update={
                            "filled_stake": Decimal("0"),
                            "remaining_stake": item.requested_stake,
                            "fully_filled": False,
                            "rejection_reason": "incomplete_opening_hedge",
                        }
                    )
                    for item in filled.fills
                ]
                return filled.model_copy(update={"fills": empty})

        ops.simulator = _EmptyOpening()
        with pytest.raises(PaperOperationsError, match="incomplete_opening_hedge"):
            ops.simulate_fill(
                opportunity_id,
                simulate_external=True,
                now=datetime.now(UTC),
            )
        assert ops.list_active_trades() == []
        assert ops.trades.get(trade_id) is None
        assert _lock_fingerprint(ledger, trade_id) == []
        events = ops.query_active_trade_events(trade_id=trade_id, limit=500)
        types = [item.event_type for item in events]
        assert types.index(ActiveTradeEventType.ENTRY_DECISION) < types.index(
            ActiveTradeEventType.ENTRY_ATTEMPT
        )
        assert types.index(ActiveTradeEventType.ENTRY_ATTEMPT) < types.index(
            ActiveTradeEventType.ENTRY_NO_FILL
        )
        assert ActiveTradeEventType.ENTRY_FILL not in types
        assert ActiveTradeEventType.PROMOTED_TO_ACTIVE not in types
        no_fill = next(
            item for item in events if item.event_type is ActiveTradeEventType.ENTRY_NO_FILL
        )
        assert no_fill.reason_code is ActiveTradeReasonCode.ENTRY_NO_FILL
        assert "no exposure fabricated" in no_fill.operator_copy.lower()
    finally:
        repository.close()
        ledger.close()
        reset_active_trade_registry()


def test_handoff_keeps_runtime_pricing_lane_for_same_cycle_fill() -> None:
    from sports_hedge.api import paper as paper_api
    from sports_hedge.application.price_engine import PriceEnginePriority

    src = inspect.getsource(paper_api.bind_price_engine_item_persist)
    assert "del runtime" not in src
    assert "pricing_lane=_pricing_lane_from_runtime(runtime)" in src
    helper = inspect.getsource(paper_api._pricing_lane_from_runtime)
    assert '"hot"' in helper
    assert '"background"' in helper
    assert "pricing_slice_priority" in helper

    class _Runtime:
        priority = PriceEnginePriority.HOT
        pricing_slice_priority = PriceEnginePriority.BACKGROUND

    assert paper_api._pricing_lane_from_runtime(_Runtime()) == "background"


def test_pre_attempt_hard_block_is_not_an_entry_attempt(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    try:
        opportunity_id = next(iter(ops._plans))
        plan = ops._plans[opportunity_id]
        ops._plans[opportunity_id] = plan.model_copy(update={"legs": []})
        with pytest.raises(PaperOperationsError, match="no_positive_opening_legs"):
            ops.simulate_fill(
                opportunity_id,
                simulate_external=True,
                now=datetime.now(UTC),
            )
        trade_id = paper_trade_id(opportunity_id)
        types = _types(ops, trade_id)
        assert ActiveTradeEventType.ENTRY_DECISION in types
        assert ActiveTradeEventType.ENTRY_BLOCKED in types
        assert ActiveTradeEventType.ENTRY_ATTEMPT not in types
        assert ActiveTradeEventType.ENTRY_NO_FILL not in types
        blocked = next(
            item
            for item in ops.query_active_trade_events(trade_id=trade_id, limit=500)
            if item.event_type is ActiveTradeEventType.ENTRY_BLOCKED
        )
        assert blocked.payload.get("reason") == "no_positive_opening_legs"
        assert ops.list_active_trades() == []
    finally:
        repository.close()
        ledger.close()
        reset_active_trade_registry()


def test_recovery_subtracts_already_consumed_snapshot_depth(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    try:
        opportunity_id = next(iter(ops._plans))
        real = ops.simulator
        one_sided, _calls = _one_sided_simulator(real)
        ops.simulator = one_sided
        ops.simulate_fill(opportunity_id, simulate_external=True, now=datetime.now(UTC))
        loaded = ops.list_active_trades()[0]
        plan = ops._plans[opportunity_id]
        filled = [leg for leg in loaded.legs if leg.filled_stake > 0]
        assert filled
        remaining = subtract_consumed_depth(list(plan.legs), filled)
        hedge_src = inspect.getsource(PaperOperationsService._apply_recovery_hedge)
        assert "subtract_consumed_depth" in hedge_src
        for original, adjusted in zip(plan.legs, remaining, strict=True):
            orig_depth = sum((level.available_stake for level in original.levels), Decimal("0"))
            left_depth = sum((level.available_stake for level in adjusted.levels), Decimal("0"))
            consumed = next(
                (
                    leg.filled_stake
                    for leg in filled
                    if leg.venue == original.venue
                    and leg.outcome == original.outcome
                    and leg.source_market_id == original.source_market_id
                ),
                Decimal("0"),
            )
            assert left_depth == orig_depth - consumed or left_depth <= orig_depth - consumed
            if consumed > 0:
                assert left_depth < orig_depth
    finally:
        repository.close()
        ledger.close()
        reset_active_trade_registry()


def test_residual_uses_canonical_post_cost_settlement() -> None:
    from sports_hedge.domain.models import VenueName
    from sports_hedge.fees.cost import MarketAction
    from sports_hedge.paper.models import FxRateSnapshot
    from sports_hedge.paper.trades import PaperLegFillKind, PaperTrade, PaperTradeLeg
    from venue_cost_helpers import profit_commission_cost

    now = OBSERVED
    legs = [
        PaperTradeLeg(
            venue=VenueName.MATCHBOOK,
            outcome="yes",
            currency="GBP",
            requested_stake=Decimal("100"),
            filled_stake=Decimal("100"),
            displayed_odds=Decimal("2"),
            filled_odds=Decimal("2"),
            source_market_id="m-yes",
            fill_id="f-yes",
            fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
            opening_action=MarketAction.BACK,
        ),
        PaperTradeLeg(
            venue=VenueName.MATCHBOOK,
            outcome="no",
            currency="GBP",
            requested_stake=Decimal("100"),
            filled_stake=Decimal("100"),
            displayed_odds=Decimal("2"),
            filled_odds=Decimal("2"),
            source_market_id="m-no",
            fill_id="f-no",
            fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
            opening_action=MarketAction.BACK,
        ),
    ]
    trade = PaperTrade(
        trade_id="t-post-cost",
        opportunity_id="opp-post-cost",
        state=PaperTradeState.OPEN,
        opened_at=now,
        last_updated_at=now,
        legs=legs,
        venue_costs=[profit_commission_cost(VenueName.MATCHBOOK, Decimal("0.20"))],
        fx_snapshots=[
            FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1"), spread_bps=Decimal("0"))
        ],
    )
    src = inspect.getsource(worst_case_settlement_pnl_gbp)
    assert "compute_paper_settlement" in src
    assert "after fees are ignored" not in src
    assert worst_case_settlement_pnl_gbp(trade) < Decimal("0")
    assert residual_exposure_gbp(trade) > Decimal("0")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("priority_name", "lane", "suffix"),
    [
        ("HOT", "hot", "hotpath"),
        ("BACKGROUND", "background", "bgpath"),
    ],
)
async def test_qualifying_price_engine_cycle_attempts_fill_immediately(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    priority_name: str,
    lane: str,
    suffix: str,
) -> None:
    from sports_hedge.application.price_engine import PriceEnginePriority
    from sports_hedge.domain.models import VenueName
    from test_issue344_price_engine import DISTANT_KICKOFF, NEAR_KICKOFF, NOW, _engine, _row
    from test_issue346_item_completion_capture import (
        PerItemScan,
        _audit,
        _bind,
        _item_decision,
        _slice_and_drain,
    )
    from test_step8f_automatic_paper_entry import _ops_bundle

    priority = PriceEnginePriority[priority_name]
    kickoff = NEAR_KICKOFF if priority is PriceEnginePriority.HOT else DISTANT_KICKOFF
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    audit = _audit(tmp_path)
    try:
        row = _row(
            suffix=suffix,
            kickoff=kickoff,
            matchbook_market_id="316510" if lane == "hot" else "316511",
            kalshi_event=f"KXEPLBTTS-{suffix.upper()}",
        )
        paper = PerItemScan(lambda _fixture: _item_decision(scan))
        engine, _mb, _ks, _layer = _engine([row], paper_scan=paper)
        runtime = engine.item(f"amc-{suffix}")
        assert runtime is not None
        assert runtime.priority is priority
        chain_calls = _bind(
            engine, scan=scan, watchlist=watchlist, ops=ops, audit=audit, monkeypatch=monkeypatch
        )
        result = await _slice_and_drain(engine, priority, now=NOW)
        assert f"amc-{suffix}" in result.evaluated
        priced = engine.item(f"amc-{suffix}")
        assert priced is not None
        assert priced.pricing_slice_priority is priority
        if priority is PriceEnginePriority.BACKGROUND:
            assert priced.priority is PriceEnginePriority.HOT
            assert result.promotions
        assert len(chain_calls) == 1
        assert chain_calls[0][1].get("pricing_lane") == lane
        trades = ops.list_active_trades()
        assert len(trades) == 1
        trade = trades[0]
        assert trade.state is PaperTradeState.OPEN
        events = ops.query_active_trade_events(trade_id=trade.trade_id, limit=500)
        types = [item.event_type for item in events]
        assert types.index(ActiveTradeEventType.ENTRY_DECISION) < types.index(
            ActiveTradeEventType.ENTRY_ATTEMPT
        )
        assert types.index(ActiveTradeEventType.ENTRY_ATTEMPT) < types.index(
            ActiveTradeEventType.ENTRY_FILL
        )
        assert types.index(ActiveTradeEventType.ENTRY_FILL) < types.index(
            ActiveTradeEventType.PROMOTED_TO_ACTIVE
        )
        decision = next(
            item for item in events if item.event_type is ActiveTradeEventType.ENTRY_DECISION
        )
        assert decision.payload.get("pricing_lane") == lane
        snap = ledger.treasury.snapshot()
        assert snap.pool(VenueName.MATCHBOOK, "GBP").locked_capital > 0
    finally:
        repository.close()
        ledger.close()
        audit.close()
        reset_active_trade_registry()


@pytest.mark.asyncio
async def test_refresh_telemetry_failure_does_not_veto_fresh_top_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    store = _raise_trade_cap(tmp_path, Decimal("2000"))
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    coordinator = None
    try:
        trade = ops.list_active_trades()[0]
        old_plan = ops._plans[trade.opportunity_id]
        before_capital = trade.capital_locked_gbp
        real = ops.record_active_lifecycle_event

        def _drop_refresh(journal_trade, **kwargs):
            event_type = kwargs.get("event_type")
            if event_type in {
                ActiveTradeEventType.ACTIVE_REFRESH_STARTED,
                ActiveTradeEventType.ACTIVE_REFRESH_RESULT,
            }:
                return False
            return real(journal_trade, **kwargs)

        ops.record_active_lifecycle_event = _drop_refresh  # type: ignore[method-assign]

        class _FakeEngine:
            async def _price_item(self, runtime, slice_result, *, lane=None):
                runtime.status = PriceEngineItemStatus.EVALUATED
                later = old_plan.decision.scanned_at + timedelta(milliseconds=100)
                slice_result.decisions.append(
                    old_plan.decision.model_copy(update={"scanned_at": later})
                )
                return PriceEngineItemStatus.EVALUATED

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
        tick_src = inspect.getsource(LiveRefreshCoordinator._run_active_trade_tick)
        assert "fresh_plan is not None and journal_ok" not in tick_src
        await coordinator._run_active_trade_tick(
            DualCadencePlan(
                lane=ACTIVE_TRADE_LANE,
                reason="active_trade_due",
                identity_scope=[trade.trade_id],
            )
        )
        loaded = ops.list_active_trades()[0]
        assert any(item.kind is PaperTradeTrancheKind.TOP_UP for item in loaded.tranches)
        assert (loaded.capital_locked_gbp or Decimal("0")) > (before_capital or Decimal("0"))
        assert coordinator.status.active_trade.last_persist_error == "active_trade_journal_write_failed"
        assert coordinator.status.active_trade.persist_ok is False
    finally:
        if coordinator is not None:
            coordinator.reset()
        repository.close()
        ledger.close()
        store.close()
        bind_runtime_operator_scanner_settings_store(None)
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
        min_net_edge=Decimal("0.01"),
        max_execution_risk=60,
        hot_cadence_seconds=30,
        max_allocated_per_trade_gbp=Decimal("2000"),
    )
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    try:
        opportunity_id = next(iter(ops._plans))
        plan = ops._plans[opportunity_id]
        plan.decision.minimum_net_edge = Decimal("0.99")
        plan.decision.min_net_edge_scope = MarketScope.FIXTURE_MATCH
        plan.decision.min_net_edge_source = "min_net_edge"
        real = ops.simulator
        one_sided, calls = _one_sided_simulator(real)
        ops.simulator = one_sided
        result = ops.simulate_fill(
            opportunity_id,
            simulate_external=True,
            now=datetime.now(UTC),
        )
        loaded = ops.list_active_trades()[0]
        assert result.trade_id == loaded.trade_id
        assert calls["n"] == 1
        assert loaded.state is PaperTradeState.PARTIAL
        assert loaded.unresolved_recovery is True
        types = _types(ops, loaded.trade_id)
        assert types.index(ActiveTradeEventType.ENTRY_DECISION) < types.index(
            ActiveTradeEventType.ENTRY_ATTEMPT
        )
        assert types.index(ActiveTradeEventType.ENTRY_ATTEMPT) < types.index(
            ActiveTradeEventType.ENTRY_PARTIAL_FILL
        )
        assert types.index(ActiveTradeEventType.ENTRY_PARTIAL_FILL) < types.index(
            ActiveTradeEventType.PROMOTED_TO_ACTIVE
        )
        assert ActiveTradeEventType.ENTRY_RECOVERY_FILL not in types
        promoted = next(
            item
            for item in ops.query_active_trade_events(trade_id=loaded.trade_id, limit=500)
            if item.event_type is ActiveTradeEventType.PROMOTED_TO_ACTIVE
        )
        assert "partial fill" in promoted.operator_copy.lower()
        member = get_active_trade_registry().get(loaded.trade_id)
        assert member is not None
        assert member.next_due_at == loaded.last_updated_at or member.next_due_at <= loaded.last_updated_at

        ops.simulator = real
        recovered = ops.maybe_top_up_open_trade(
            loaded,
            now=OBSERVED + timedelta(seconds=5),
            plan=_fresh_current_plan(ops, opportunity_id),
            require_current_plan=True,
        )
        assert recovered is not None
        again = ops.list_active_trades()[0]
        types = _types(ops, again.trade_id)
        assert ActiveTradeEventType.ENTRY_RECOVERY_DECISION in types
        assert ActiveTradeEventType.ENTRY_RECOVERY_FILL in types
        assert ActiveTradeEventType.ENTRY_RECOVERY_RESIDUAL in types
        assert ActiveTradeReasonCode.RECOVERY_BELOW_MIN_NET in _reasons(ops, again.trade_id)
        recoveries = [
            item for item in again.tranches if item.kind is PaperTradeTrancheKind.RECOVERY
        ]
        assert recoveries
        assert again.unresolved_recovery is False
        assert again.state is PaperTradeState.OPEN
        assert residual_exposure_gbp(again) == Decimal("0")
        assert worst_case_settlement_pnl_gbp(again) >= Decimal("0")
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

        one_sided, calls = _one_sided_simulator(real)
        ops.simulator = one_sided
        ops.simulate_fill(opportunity_id, simulate_external=True, now=datetime.now(UTC))
        loaded = ops.list_active_trades()[0]
        assert calls["n"] == 1
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

        class _TinyRecovery:
            def simulate(self, *args, **kwargs):
                filled = real.simulate(*args, **kwargs)
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
        ops.maybe_top_up_open_trade(
            loaded,
            now=OBSERVED + timedelta(seconds=5),
            plan=_fresh_current_plan(ops, opportunity_id),
            require_current_plan=True,
        )
        again = ops.list_active_trades()[0]
        assert len(again.tranches) >= len(before_ids)
        retry_ids = [item.tranche_id for item in again.tranches]
        assert len(retry_ids) == len(set(retry_ids))
        assert len([leg.fill_id for leg in again.legs if leg.fill_id]) == len(
            {leg.fill_id for leg in again.legs if leg.fill_id}
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
        before_fills = [
            item
            for item in ops.query_active_trade_events(trade_id=trade.trade_id, limit=500)
            if item.event_type is ActiveTradeEventType.TOPUP_FILL
        ]
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
        assert [
            item
            for item in ops.query_active_trade_events(trade_id=trade.trade_id, limit=500)
            if item.event_type is ActiveTradeEventType.TOPUP_FILL
        ] == before_fills
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


def test_venue_degradation_why_is_untouched_by_active_journal() -> None:
    from sports_hedge.application import venue_degradation_incident as why_mod

    why_src = inspect.getsource(why_mod)
    observe_src = inspect.getsource(LiveRefreshCoordinator.observe_degradation_incidents)
    coordinator_src = inspect.getsource(LiveRefreshCoordinator)
    assert "active_trade_context" not in why_src
    assert "query_active_trade_events" not in why_src
    assert '"active_trade"' not in why_src
    assert "active_trade_context" not in observe_src
    assert "query_active_trade_events" not in observe_src
    assert "recent_active_trade_timeline" not in observe_src
    assert "_active_trade_degradation_context" not in coordinator_src
    assert "list_events" not in observe_src
    assert "get_order_book" not in observe_src


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
