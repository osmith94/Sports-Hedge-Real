from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sports_hedge.paper.risk_snapshot import PaperExecutionRiskSnapshot, PaperRiskSnapshotKind
from sports_hedge.paper.trades import PaperTradeAuditEventType, PaperTradeState
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.risk.execution import ExecutionRiskInputs, ExecutionRiskResult

from test_paper_trade_lifecycle import _ops


NOW = datetime(2026, 9, 13, 12, 0, tzinfo=UTC)


def _snapshot(*, score: int, kind: PaperRiskSnapshotKind = PaperRiskSnapshotKind.ENTRY) -> PaperExecutionRiskSnapshot:
    return PaperExecutionRiskSnapshot(
        kind=kind,
        recorded_at=NOW,
        opportunity_id="opp-risk",
        trade_id="ptrade-risk",
        score=score,
        band="low",
        reasons=["wide_spread"],
        maximum_execution_risk=60,
        quote_age_ms=120,
        quote_age_basis="book_timestamp",
        size_to_depth_ratio=0.2,
        hedge_liquidity_ratio=1.0,
        spread_bps=8.0,
        recent_volatility_bps=5.0,
        assumed_latency_ms=40,
        fill_confidence_score=0.9,
        fill_confidence_reasons=["fresh_taker_book"],
        net_edge=Decimal("0.012"),
        trigger_net_edge=Decimal("0.005"),
        solver_model="simple_complete_set",
        eligible_for_paper_simulation=True,
    )


def test_open_trade_records_immutable_entry_risk_snapshot(tmp_path: Path) -> None:
    path = tmp_path / "paper.sqlite"
    ledger = SqlitePaperLedger(path)
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trades = ops.list_active_trades()
        assert len(trades) == 1
        trade = trades[0]
        assert trade.state is PaperTradeState.OPEN
        assert trade.entry_risk is not None
        assert trade.entry_risk.kind is PaperRiskSnapshotKind.ENTRY
        assert trade.entry_risk.score is not None
        assert trade.entry_risk.band
        assert trade.entry_risk.maximum_execution_risk == 100
        assert trade.entry_risk.quote_age_ms is not None
        assert trade.entry_risk.net_edge is not None
        assert trade.entry_risk.trigger_net_edge is not None
        assert any(
            event.event_type is PaperTradeAuditEventType.ENTRY_RISK_RECORDED for event in trade.audit
        )
        original_score = trade.entry_risk.score
        original_dump = trade.entry_risk.model_dump(mode="json")
        trade.entry_risk = trade.entry_risk.model_copy(update={"score": 99, "band": "extreme"})
        ops.trades.save(trade)
        reloaded = ops.trades.get(trade.trade_id)
        assert reloaded is not None
        assert reloaded.entry_risk is not None
        assert reloaded.entry_risk.score == original_score
        assert reloaded.entry_risk.score != 99
        assert reloaded.entry_risk.model_dump(mode="json") == original_dump
    finally:
        repository.close()
        ledger.close()


def test_entry_risk_survives_ledger_reload_and_close_snapshot_is_separate(tmp_path: Path) -> None:
    path = tmp_path / "paper-reload.sqlite"
    ledger = SqlitePaperLedger(path)
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    trade_id = None
    original = None
    try:
        trade = ops.list_active_trades()[0]
        trade_id = trade.trade_id
        original = trade.entry_risk.model_dump(mode="json") if trade.entry_risk else None
        assert original is not None
        close = _snapshot(score=42, kind=PaperRiskSnapshotKind.UNWIND).model_copy(
            update={"trade_id": trade.trade_id, "opportunity_id": trade.opportunity_id}
        )
        trade.close_risks.append(close)
        ops.trades.save(trade)
    finally:
        repository.close()
        ledger.close()

    reopened = SqlitePaperLedger(path, auto_seed=False)
    try:
        loaded = reopened.trades.get(trade_id)
        assert loaded is not None
        assert loaded.entry_risk is not None
        assert loaded.entry_risk.model_dump(mode="json") == original
        assert len(loaded.close_risks) == 1
        assert loaded.close_risks[0].kind is PaperRiskSnapshotKind.UNWIND
        assert loaded.close_risks[0].score == 42
        tampered = loaded.entry_risk.model_copy(update={"score": 1, "band": "extreme"})
        loaded.entry_risk = tampered
        reopened.trades.save(loaded)
        again = reopened.trades.get(trade_id)
        assert again is not None
        assert again.entry_risk is not None
        assert again.entry_risk.model_dump(mode="json") == original
        assert len(again.close_risks) == 1
    finally:
        reopened.close()


def test_risk_inputs_are_retained_on_scored_result() -> None:
    inputs = ExecutionRiskInputs(
        spread_bps=10,
        size_to_depth_ratio=0.2,
        quote_age_ms=80,
        recent_volatility_bps=4,
        leg_count=2,
        assumed_latency_ms=25,
        hedge_liquidity_ratio=1.0,
    )
    from sports_hedge.risk.execution import ExecutionRiskScorer

    result = ExecutionRiskScorer().score(inputs)
    assert isinstance(result, ExecutionRiskResult)
    assert result.inputs is not None
    assert result.inputs.quote_age_ms == 80
    assert result.inputs.size_to_depth_ratio == 0.2
