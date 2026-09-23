"""Operator-facing fee labels. Provenance stays on the snapshot source fields."""

from __future__ import annotations

from decimal import Decimal

from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import FeeBasis, VenueCostSnapshot
from sports_hedge.fees.kalshi import KALSHI_QUADRATIC_FORMULA
from sports_hedge.fees.polymarket import POLYMARKET_TAKER_FORMULA

MATCHBOOK_OVERRIDE_TIER = "operator_account_override"


def operator_fee_label(snapshot: VenueCostSnapshot | None, *, status: str | None = None) -> str:
    if snapshot is None:
        if status == "missing":
            return "fee missing"
        if status == "unknown":
            return "fee unknown"
        return "fee unknown"
    if not snapshot.is_economically_known():
        if snapshot.venue is VenueName.POLYMARKET:
            return "fee metadata missing · fail closed"
        return "fee unknown"
    assumption = _is_account_assumption(snapshot)
    if snapshot.fee_basis is FeeBasis.PROFIT_COMMISSION and snapshot.rate is not None:
        label = f"{_percent(snapshot.rate)} net-profit commission"
        if assumption:
            return f"{label} · operator/account assumption"
        return label
    if snapshot.fee_basis is FeeBasis.NONE_CONFIRMED:
        if snapshot.venue is VenueName.POLYMARKET:
            return "fee disabled · known zero"
        return "fee disabled · known zero"
    if snapshot.fee_basis is FeeBasis.FORMULA:
        if snapshot.formula_name == POLYMARKET_TAKER_FORMULA:
            rate = snapshot.formula_parameters.get("rate")
            if rate is not None:
                return f"market-specific taker formula (rate {rate})"
            return "market-specific taker formula"
        if snapshot.formula_name == KALSHI_QUADRATIC_FORMULA:
            multiplier = snapshot.formula_parameters.get("fee_multiplier")
            if multiplier is not None:
                return f"Kalshi quadratic taker formula (M={multiplier})"
            return "Kalshi quadratic taker formula"
        name = snapshot.formula_name or "formula"
        return f"market-specific {name.replace('_', ' ')}"
    if snapshot.fee_basis is FeeBasis.PAYOUT and snapshot.rate is not None:
        return f"{_percent(snapshot.rate)} payout fee"
    if snapshot.rate is not None:
        return f"{_percent(snapshot.rate)} {snapshot.fee_basis.value.replace('_', ' ')}"
    return snapshot.fee_basis.value.replace("_", " ")


def cost_assumption_labels_for_snapshots(snapshots: list[VenueCostSnapshot]) -> list[str]:
    labels: list[str] = []
    for snapshot in snapshots:
        labels.append(f"venue_fee:{snapshot.venue.value}:{operator_fee_label(snapshot)}")
        if snapshot.snapshot_id:
            labels.append(f"fee_snapshot:{snapshot.snapshot_id}")
        if snapshot.source:
            labels.append(f"fee_source:{snapshot.source}")
        if _is_account_assumption(snapshot):
            labels.append("matchbook_operator_account_assumption")
    return labels


def _is_account_assumption(snapshot: VenueCostSnapshot) -> bool:
    if snapshot.account_or_fee_tier == MATCHBOOK_OVERRIDE_TIER:
        return True
    return snapshot.source.startswith("operator_account_assumption:")


def _percent(rate: Decimal) -> str:
    return f"{(rate * Decimal('100')).quantize(Decimal('0.01'))}%"
