"""Read-only active-trade economics for the operator console.

Entry arb is the stored opening snapshot. Current exit % is validated
full-close P&L divided by capital locked at open. Neither value is
recomputed from headline quotes, and a percentage is omitted when a
complete executable close cannot be proven.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sports_hedge.paper.risk_snapshot import PaperRiskSnapshotKind
from sports_hedge.paper.trades import PaperTrade

_EXIT_QUANTUM = Decimal("0.000001")
_DELTA_QUANTUM = Decimal("0.01")


def annotate_active_trade_economics(trade: PaperTrade) -> PaperTrade:
    """Return a copy with console read fields. Does not persist or reprice."""

    entry = _entry_net_edge(trade)
    exit_pct, checked_at, block_reason = _current_exit(trade)
    delta = _delta_pp(entry, exit_pct)
    return trade.model_copy(
        update={
            "entry_net_edge": entry,
            "current_exit_pct": exit_pct,
            "current_exit_delta_pp": delta,
            "current_exit_checked_at": checked_at,
            "current_exit_block_reason": block_reason,
        }
    )


def _entry_net_edge(trade: PaperTrade) -> Decimal | None:
    snapshot = trade.entry_risk
    if snapshot is None or snapshot.kind is not PaperRiskSnapshotKind.ENTRY:
        return None
    return snapshot.net_edge


def _current_exit(trade: PaperTrade) -> tuple[Decimal | None, datetime | None, str | None]:
    snapshot = trade.position_management
    if snapshot is None:
        return None, None, "incomplete close plan"
    checked_at = getattr(snapshot, "evaluated_at", None)
    validated = getattr(snapshot, "validated_exit_pnl_gbp", None)
    executable = bool(getattr(snapshot, "close_executable", False))
    if validated is None or not executable:
        return None, checked_at, _block_reason(snapshot)
    capital = trade.capital_locked_gbp
    if capital is None or capital <= 0:
        return None, checked_at, "missing deployed capital"
    pct = (Decimal(validated) / Decimal(capital)).quantize(_EXIT_QUANTUM)
    return pct, checked_at, None


def _block_reason(snapshot: object) -> str:
    blocker = getattr(snapshot, "close_blocker", None)
    if blocker:
        return str(blocker)
    reason = getattr(snapshot, "decision_reason", None)
    if reason:
        return str(reason)
    return "incomplete close plan"


def _delta_pp(entry: Decimal | None, exit_pct: Decimal | None) -> Decimal | None:
    if entry is None or exit_pct is None:
        return None
    return ((exit_pct - entry) * Decimal(100)).quantize(_DELTA_QUANTUM)
