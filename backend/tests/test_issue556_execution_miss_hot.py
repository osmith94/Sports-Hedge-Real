"""Issue #556 — zero-fill execution miss stays HOT for a bounded window.

PAPER / fixture clocks only. No venue orders. Sticky HOT is process memory.
The audit row is append-only SQLite.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

import pytest

from sports_hedge.application.paper_operations import PaperOperationsError
from sports_hedge.application.price_engine import PriceEnginePriority
from sports_hedge.application.scan_lanes import (
    HOT_REASON_RECENTLY_QUALIFYING_EXECUTION_MISS,
    ScanLane,
    hot_reason_labels,
)
from sports_hedge.arbitrage.watchlist.economics import MOVED_BELOW_MIN_NET_ARB
from sports_hedge.arbitrage.watchlist.models import LifecycleEventType
from sports_hedge.lifecycle.execution_miss import (
    DEFAULT_EXECUTION_MISS_HOT_STICKY,
    REASON_RECENTLY_QUALIFYING_EXECUTION_MISS,
    ZERO_FILL_PRICE_MOVEMENT,
    ZERO_FILL_PROVIDER_REJECTION,
    ZERO_FILL_STALE_EXECUTABLE_QUOTE,
    classify_zero_fill_fact,
    execution_miss_hot_active,
    execution_miss_sticky_until,
    retains_hot_for_zero_fill,
    zero_fill_exposure_gbp,
)
from sports_hedge.paper.trades import PaperTradeState
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from test_dual_cadence_scheduler import FakeClock
from test_issue344_price_engine import NOW, StubPaperScan, _engine, _qualifying_decision, _row
from test_issue372_active_trade_journal import _one_sided_simulator
from test_issue395_signal_only_activity_feed import _uninteresting_decision
from test_paper_trade_lifecycle import _ops


def test_zero_fill_facts_are_distinct_and_only_price_movement_retains_hot() -> None:
    assert classify_zero_fill_fact(MOVED_BELOW_MIN_NET_ARB) == ZERO_FILL_PRICE_MOVEMENT
    assert classify_zero_fill_fact("no_visible_depth") == ZERO_FILL_PRICE_MOVEMENT
    assert classify_zero_fill_fact("incomplete_opening_hedge") == ZERO_FILL_PRICE_MOVEMENT
    assert classify_zero_fill_fact("snapshot_stale_at_simulated_arrival") == (
        ZERO_FILL_STALE_EXECUTABLE_QUOTE
    )
    assert classify_zero_fill_fact("provider_rejected") == ZERO_FILL_PROVIDER_REJECTION
    assert retains_hot_for_zero_fill(MOVED_BELOW_MIN_NET_ARB) is True
    assert retains_hot_for_zero_fill("snapshot_stale_at_decision") is False
    assert retains_hot_for_zero_fill("provider_rejected") is False
    assert DEFAULT_EXECUTION_MISS_HOT_STICKY == timedelta(minutes=5)
    until = execution_miss_sticky_until(NOW)
    assert execution_miss_hot_active(NOW + timedelta(minutes=5) - timedelta(seconds=1), until)
    assert execution_miss_hot_active(NOW + timedelta(minutes=5), until) is False
    assert zero_fill_exposure_gbp(Decimal("0")) == Decimal("0")
    with pytest.raises(ValueError):
        zero_fill_exposure_gbp(Decimal("1"))


def test_hot_reason_label_is_not_net_proximity() -> None:
    labels = hot_reason_labels(
        type("Fixture", (), {"in_running": False, "kickoff_utc": NOW + timedelta(days=3)})(),
        NOW,
        membership=ScanLane.HOT,
        lifecycle=ScanLane.UNIVERSE,
        qualifying_promotion=False,
        net_proximity_promotion=False,
        surveillance_promotion=False,
        execution_miss_promotion=True,
    )
    assert labels == [HOT_REASON_RECENTLY_QUALIFYING_EXECUTION_MISS]
    assert REASON_RECENTLY_QUALIFYING_EXECUTION_MISS not in labels


def test_background_full_fill_is_active_and_not_an_execution_miss(tmp_path) -> None:
    _scan, _watchlist, ops, _repository = _ops(
        ledger=SqlitePaperLedger(tmp_path / "full.sqlite"), autofill=False
    )
    try:
        opportunity_id = next(iter(ops._plans))
        ops.simulate_fill(opportunity_id, simulate_external=True, now=NOW)
        assert ops.list_active_trades()[0].state is PaperTradeState.OPEN
        assert ops.consume_execution_miss(opportunity_id) is None
    finally:
        ops.watchlist.repository.close()


def test_partial_fill_stays_active_and_is_not_hot_miss(tmp_path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "partial.sqlite")
    _scan, _watchlist, ops, _repository = _ops(ledger=ledger, autofill=False)
    try:
        opportunity_id = next(iter(ops._plans))
        ops.simulator, _calls = _one_sided_simulator(ops.simulator)
        ops.simulate_fill(opportunity_id, simulate_external=True, now=NOW)
        trade = ops.list_active_trades()[0]
        assert trade.state in {PaperTradeState.PARTIAL, PaperTradeState.OPEN}
        assert any(leg.filled_stake > 0 for leg in trade.legs)
        assert ops.consume_execution_miss(opportunity_id) is None
        events = ops.watchlist.repository.list_events(opportunity_id=opportunity_id, limit=50)
        assert LifecycleEventType.ZERO_FILL_EXECUTION_MISS not in {item.event_type for item in events}
    finally:
        ops.watchlist.repository.close()
        ledger.close()


def test_zero_fill_price_movement_audits_miss_without_active_exposure(tmp_path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "zero.sqlite")
    _scan, watchlist, ops, _repository = _ops(ledger=ledger, autofill=False)
    try:
        opportunity_id = next(iter(ops._plans))
        real = ops.simulator

        class _Empty:
            def simulate(self, *args, **kwargs):
                filled = real.simulate(*args, **kwargs)
                empty = [
                    item.model_copy(
                        update={
                            "filled_stake": Decimal("0"),
                            "remaining_stake": item.requested_stake,
                            "fully_filled": False,
                            "weighted_odds": None,
                            "rejection_reason": "no_visible_depth",
                        }
                    )
                    for item in filled.fills
                ]
                return filled.model_copy(update={"fills": empty})

        ops.simulator = _Empty()
        with pytest.raises(PaperOperationsError, match="no_visible_depth"):
            ops.simulate_fill(opportunity_id, simulate_external=True, now=NOW)
        assert ops.list_active_trades() == []
        miss = ops.consume_execution_miss(opportunity_id)
        assert miss is not None
        assert miss.retains_hot is True
        assert miss.zero_fill_fact == ZERO_FILL_PRICE_MOVEMENT
        assert miss.sticky_until == NOW + DEFAULT_EXECUTION_MISS_HOT_STICKY
        events = watchlist.repository.list_events(opportunity_id=opportunity_id, limit=50)
        typed = [item.event_type for item in events]
        assert LifecycleEventType.PAPER_FILL_ATTEMPTED in typed
        assert LifecycleEventType.PAPER_FILL_REJECTED in typed
        assert LifecycleEventType.ZERO_FILL_EXECUTION_MISS in typed
        audit = next(
            item for item in events if item.event_type is LifecycleEventType.ZERO_FILL_EXECUTION_MISS
        )
        assert "fill_result=zero" in (audit.detail or "")
        assert f"zero_fill_fact={ZERO_FILL_PRICE_MOVEMENT}" in (audit.detail or "")
        assert f"hot_promotion_reason={REASON_RECENTLY_QUALIFYING_EXECUTION_MISS}" in (
            audit.detail or ""
        )
        trade_id = ops.list_active_trades()
        assert trade_id == []
        journal = ops.query_active_trade_events(limit=50)
        no_fill = next(item for item in journal if item.event_type.value == "entry_no_fill")
        assert no_fill.payload["fill_result"] == "zero"
        assert no_fill.payload["zero_fill_fact"] == ZERO_FILL_PRICE_MOVEMENT
        assert no_fill.payload["hot_promotion_reason"] == REASON_RECENTLY_QUALIFYING_EXECUTION_MISS
        before = len(events)
        watchlist.record_paper_fill_rejection(
            opportunity_id,
            occurred_at=NOW,
            detail="no_visible_depth",
            zero_fill_fact=ZERO_FILL_PRICE_MOVEMENT,
            zero_fill_audit_detail=audit.detail,
        )
        again = watchlist.repository.list_events(opportunity_id=opportunity_id, limit=50)
        assert len([item for item in again if item.event_type is LifecycleEventType.ZERO_FILL_EXECUTION_MISS]) == 1
        assert len(again) >= before
    finally:
        watchlist.repository.close()
        ledger.close()


def test_zero_fill_miss_can_requalify_and_attempt_paper_again(tmp_path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "retry.sqlite")
    _scan, _watchlist, ops, _repository = _ops(ledger=ledger, autofill=False)
    try:
        opportunity_id = next(iter(ops._plans))
        plan = ops._plans[opportunity_id]
        real = ops.simulator

        class _Empty:
            def simulate(self, *args, **kwargs):
                filled = real.simulate(*args, **kwargs)
                empty = [
                    item.model_copy(
                        update={
                            "filled_stake": Decimal("0"),
                            "remaining_stake": item.requested_stake,
                            "fully_filled": False,
                            "weighted_odds": None,
                            "rejection_reason": "no_visible_depth",
                        }
                    )
                    for item in filled.fills
                ]
                return filled.model_copy(update={"fills": empty})

        ops.simulator = _Empty()
        with pytest.raises(PaperOperationsError):
            ops.simulate_fill(opportunity_id, simulate_external=True, now=NOW)
        ops.simulator = real
        later = NOW + timedelta(seconds=30)
        decision = plan.decision.model_copy(update={"scanned_at": later})
        ops.persist_triggered_chain(decision, autofill=True, now=later)
        assert ops.list_active_trades()[0].state is PaperTradeState.OPEN
        journal = ops.query_active_trade_events(limit=100)
        assert any(
            item.payload.get("requalification_after") == REASON_RECENTLY_QUALIFYING_EXECUTION_MISS
            for item in journal
        )
    finally:
        ops.watchlist.repository.close()
        ledger.close()


@pytest.mark.asyncio
async def test_execution_miss_hot_is_sticky_then_expires_and_restarts_clear() -> None:
    clock = FakeClock(NOW)
    paper = StubPaperScan(_uninteresting_decision(scanned_at=NOW))
    facts = []
    captures = []
    engine, _mb, _ks, _layer = _engine(
        [_row(suffix="cover", kickoff=NOW + timedelta(days=3), matchbook_market_id="316131", kalshi_event="KXEPLBTTS-COVER")],
        clock=clock,
        paper_scan=paper,
        on_hot_promotion=facts.append,
        on_item_decision=lambda decision, runtime: captures.append(runtime.identity.catalogue_row_id),
        hot_interval=0,
    )
    runtime = engine.item("amc-cover")
    assert runtime is not None
    assert runtime.priority is PriceEnginePriority.BACKGROUND
    until = execution_miss_sticky_until(NOW)
    retained = engine.note_recently_qualifying_execution_miss(
        canonical_event_id=runtime.identity.canonical_event_id,
        catalogue_row_id=runtime.identity.catalogue_row_id,
        content_version=runtime.identity.content_version,
        occurred_at=NOW,
        sticky_until=until,
        zero_fill_reason="no_visible_depth",
        pricing_lane="background",
    )
    assert retained is True
    assert engine.classify_priority(runtime.identity) is PriceEnginePriority.HOT
    assert facts[0].promotion_reason == REASON_RECENTLY_QUALIFYING_EXECUTION_MISS
    assert REASON_RECENTLY_QUALIFYING_EXECUTION_MISS in (facts[0].detail or "")
    clock.advance(30)
    await engine.run_slice(PriceEnginePriority.HOT, now=clock.now)
    assert runtime.identity.canonical_event_id in engine._promoted_hot_ids
    assert runtime.execution_miss_sticky is True
    paper.decision = _qualifying_decision(scanned_at=clock.now)
    clock.advance(10)
    await engine.run_slice(PriceEnginePriority.HOT, now=clock.now)
    assert captures
    assert runtime.execution_miss_sticky is False
    assert engine.classify_priority(runtime.identity) is PriceEnginePriority.HOT
    paper.decision = _uninteresting_decision(scanned_at=clock.now)
    clock.now = until + timedelta(seconds=1)
    await engine.run_slice(PriceEnginePriority.HOT, now=clock.now)
    assert runtime.identity.canonical_event_id not in engine._promoted_hot_ids
    engine.note_recently_qualifying_execution_miss(
        canonical_event_id=runtime.identity.canonical_event_id,
        catalogue_row_id=runtime.identity.catalogue_row_id,
        content_version=runtime.identity.content_version,
        occurred_at=clock.now,
        sticky_until=execution_miss_sticky_until(clock.now),
        zero_fill_reason="no_visible_depth",
    )
    assert runtime.identity.canonical_event_id in engine._promoted_hot_ids
    engine.restart()
    restarted = engine.item("amc-cover")
    assert restarted is not None
    assert restarted.identity.canonical_event_id not in engine._promoted_hot_ids
    assert engine._execution_miss_until == {}
