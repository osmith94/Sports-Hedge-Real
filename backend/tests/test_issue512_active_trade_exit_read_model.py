"""Issue #512: active-trade entry arb and current exit % are read-model only."""

from datetime import UTC, datetime
from decimal import Decimal

from sports_hedge.paper.active_trade_read_model import annotate_active_trade_economics
from sports_hedge.paper.position_management.models import PositionManagementSnapshot
from sports_hedge.paper.risk_snapshot import PaperExecutionRiskSnapshot, PaperRiskSnapshotKind
from sports_hedge.paper.trades import PaperTrade, PaperTradeState
from sports_hedge.paper.unwind.models import UnwindRecommendation


def _trade(**overrides: object) -> PaperTrade:
    payload = {
        "trade_id": "ptrade-512",
        "opportunity_id": "opp-512",
        "state": PaperTradeState.OPEN,
        "opened_at": datetime(2026, 9, 15, 12, 0, tzinfo=UTC),
        "last_updated_at": datetime(2026, 9, 15, 12, 0, tzinfo=UTC),
        "capital_locked_gbp": Decimal("100"),
        "entry_risk": PaperExecutionRiskSnapshot(
            kind=PaperRiskSnapshotKind.ENTRY,
            recorded_at=datetime(2026, 9, 15, 12, 0, tzinfo=UTC),
            net_edge=Decimal("0.0182"),
        ),
    }
    payload.update(overrides)
    return PaperTrade(**payload)


def _snapshot(**overrides: object) -> PositionManagementSnapshot:
    payload = {
        "trade_id": "ptrade-512",
        "recommendation": UnwindRecommendation.UNWIND_ELIGIBLE,
        "decision_reason": "give_up_within_abundant_threshold",
        "evaluated_at": datetime(2026, 9, 15, 12, 5, tzinfo=UTC),
        "validated_exit_pnl_gbp": Decimal("0.64"),
        "close_executable": True,
        "hold_pnl_gbp": Decimal("1.82"),
    }
    payload.update(overrides)
    return PositionManagementSnapshot(**payload)


def test_current_exit_pct_is_validated_pnl_over_locked_capital() -> None:
    annotated = annotate_active_trade_economics(_trade(position_management=_snapshot()))
    assert annotated.entry_net_edge == Decimal("0.0182")
    assert annotated.current_exit_pct == Decimal("0.006400")
    assert annotated.current_exit_delta_pp == Decimal("-1.18")
    assert annotated.current_exit_block_reason is None
    assert annotated.current_exit_checked_at == datetime(2026, 9, 15, 12, 5, tzinfo=UTC)


def test_current_exit_pct_is_null_when_close_is_not_fully_executable() -> None:
    annotated = annotate_active_trade_economics(
        _trade(
            position_management=_snapshot(
                recommendation=UnwindRecommendation.UNWIND_NOT_SAFE,
                decision_reason="missing_reverse_quote",
                close_blocker="missing_reverse_quote",
                validated_exit_pnl_gbp=None,
                close_executable=False,
            )
        )
    )
    assert annotated.current_exit_pct is None
    assert annotated.current_exit_delta_pp is None
    assert annotated.current_exit_block_reason == "missing_reverse_quote"
    assert annotated.entry_net_edge == Decimal("0.0182")


def test_entry_net_edge_stays_fixed_when_exit_economics_change() -> None:
    first = annotate_active_trade_economics(_trade(position_management=_snapshot()))
    second = annotate_active_trade_economics(
        _trade(
            position_management=_snapshot(
                validated_exit_pnl_gbp=Decimal("1.10"),
                evaluated_at=datetime(2026, 9, 15, 12, 8, tzinfo=UTC),
            )
        )
    )
    assert first.entry_net_edge == second.entry_net_edge == Decimal("0.0182")
    assert second.current_exit_pct == Decimal("0.011000")
    assert first.current_exit_pct != second.current_exit_pct


def test_missing_position_management_does_not_invent_an_exit_percentage() -> None:
    annotated = annotate_active_trade_economics(_trade(position_management=None))
    assert annotated.current_exit_pct is None
    assert annotated.current_exit_block_reason == "incomplete close plan"
    assert annotated.entry_net_edge == Decimal("0.0182")
