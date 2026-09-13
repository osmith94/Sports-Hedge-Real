"""Polymarket fee snapshots from authoritative per-market CLOB/Gamma metadata.

Taker formula from current Polymarket docs:

    fee = C × rate × (p × (1 − p)) ** exponent

C is shares traded, p is the share price. Makers are not charged when the
schedule is taker-only. Fees round to 5 decimal places; amounts below
0.00001 USDC are zero.

Applicability is market-specific (`feesEnabled` / `fees_enabled`). Category
tables are not used as a global substitute. Missing applicability or
parameters fail closed. `feesEnabled: false` is known zero.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any, Mapping

from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import (
    CostKnownStatus,
    FeeBasis,
    FeeScope,
    MarketAction,
    OrderRole,
    VenueCostSnapshot,
)

POLYMARKET_TAKER_FORMULA = "polymarket_taker_fee"
POLYMARKET_FEE_INCREMENT = Decimal("0.00001")
_BACK_BUY = {MarketAction.BUY, MarketAction.SELL}


def extract_polymarket_fee_metadata(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    """Normalize Gamma/CLOB/SDK fee fields without guessing applicability."""

    raw = dict(payload or {})
    trading = raw.get("trading") if isinstance(raw.get("trading"), dict) else {}
    fees_enabled = _bool_or_none(
        _first_value(trading, "feesEnabled", "fees_enabled", default=_first_value(raw, "feesEnabled", "fees_enabled"))
    )
    schedule_raw = _first_value(trading, "feeSchedule", "fee_schedule", default=_first_value(raw, "feeSchedule", "fee_schedule"))
    schedule = _normalize_schedule(schedule_raw)
    legacy_rate = _decimal_or_none(
        _first_value(trading, "feeRate", "fee_rate", default=_first_value(raw, "feeRate", "fee_rate"))
    )
    return {
        "fees_enabled": fees_enabled,
        "fee_schedule": schedule,
        "legacy_fee_rate": str(legacy_rate) if legacy_rate is not None else None,
        "source_market_id": _market_id(raw),
    }


def polymarket_cost_from_market(
    payload: Mapping[str, Any] | None,
    *,
    action: MarketAction = MarketAction.BUY,
    order_role: OrderRole = OrderRole.TAKER,
    captured_at: datetime | None = None,
    source_market_id: str | None = None,
) -> VenueCostSnapshot:
    captured = captured_at or datetime.now(UTC)
    metadata = (
        dict(payload)
        if payload is not None and ("fees_enabled" in payload or "fee_schedule" in payload)
        else extract_polymarket_fee_metadata(payload)
    )
    market_id = source_market_id or metadata.get("source_market_id")
    if action not in _BACK_BUY:
        return _unknown(
            captured_at=captured,
            source_market_id=market_id,
            action=action,
            order_role=order_role,
            detail=f"Polymarket fee snapshot requires BUY or SELL; got {action.value}",
        )
    fees_enabled = metadata.get("fees_enabled")
    if not isinstance(fees_enabled, bool):
        return _unknown(
            captured_at=captured,
            source_market_id=market_id,
            action=action,
            order_role=order_role,
            detail="Polymarket feesEnabled missing; per-market applicability cannot be assumed",
        )
    if fees_enabled is False:
        return VenueCostSnapshot(
            venue=VenueName.POLYMARKET,
            action=action,
            fee_basis=FeeBasis.NONE_CONFIRMED,
            known_status=CostKnownStatus.KNOWN,
            captured_at=captured,
            source="polymarket_fee_schedule:market:disabled",
            source_market_id=market_id,
            order_role=order_role,
            fee_scope=FeeScope.PER_QUOTE,
            currency="USD",
            snapshot_id=f"polymarket:market:disabled:{order_role.value}:{market_id or 'unknown'}",
            detail="Per-market CLOB/Gamma feesEnabled=false; known zero for this market.",
        )
    schedule = metadata.get("fee_schedule") if isinstance(metadata.get("fee_schedule"), dict) else {}
    rate = _decimal_or_none(schedule.get("rate"))
    exponent = _decimal_or_none(schedule.get("exponent"))
    taker_only = _bool_or_none(schedule.get("taker_only"))
    if rate is None:
        rate = _decimal_or_none(metadata.get("legacy_fee_rate"))
        if rate is not None and exponent is None:
            exponent = Decimal("1")
        if taker_only is None and rate is not None:
            taker_only = True
    if rate is None:
        return _unknown(
            captured_at=captured,
            source_market_id=market_id,
            action=action,
            order_role=order_role,
            detail="Polymarket feesEnabled=true but fee rate is missing; fail closed",
        )
    if exponent is None:
        return _unknown(
            captured_at=captured,
            source_market_id=market_id,
            action=action,
            order_role=order_role,
            detail="Polymarket fee schedule exponent missing; fail closed",
        )
    if taker_only is None:
        taker_only = True
    if order_role is OrderRole.UNKNOWN:
        return _unknown(
            captured_at=captured,
            source_market_id=market_id,
            action=action,
            order_role=order_role,
            detail="Polymarket order role unknown",
        )
    if order_role is OrderRole.MAKER and taker_only:
        return VenueCostSnapshot(
            venue=VenueName.POLYMARKET,
            action=action,
            fee_basis=FeeBasis.NONE_CONFIRMED,
            known_status=CostKnownStatus.KNOWN,
            captured_at=captured,
            source="polymarket_fee_schedule:market:maker_none",
            source_market_id=market_id,
            order_role=order_role,
            fee_scope=FeeScope.PER_QUOTE,
            currency="USD",
            formula_parameters={"rate": rate, "exponent": exponent},
            snapshot_id=f"polymarket:market:maker_none:{market_id or 'unknown'}",
            detail="Per-market Polymarket schedule is taker-only; makers are not charged.",
        )
    rebate = _decimal_or_none(schedule.get("rebate_rate"))
    parameters = {"rate": rate, "exponent": exponent}
    if rebate is not None:
        parameters["rebate_rate"] = rebate
    parameters["taker_only"] = Decimal("1") if taker_only else Decimal("0")
    return VenueCostSnapshot(
        venue=VenueName.POLYMARKET,
        action=action,
        fee_basis=FeeBasis.FORMULA,
        known_status=CostKnownStatus.KNOWN,
        captured_at=captured,
        source="polymarket_fee_schedule:market:taker_formula",
        source_market_id=market_id,
        order_role=order_role,
        fee_scope=FeeScope.PER_QUOTE,
        currency="USD",
        formula_name=POLYMARKET_TAKER_FORMULA,
        formula_parameters=parameters,
        snapshot_id=f"polymarket:market:taker_formula:{order_role.value}:{market_id or 'unknown'}",
        detail=(
            "Official Polymarket taker fee C × rate × (p × (1 − p))**exponent; "
            f"rate={rate}; exponent={exponent}; taker_only={taker_only}."
        ),
    )


def apply_polymarket_taker(
    snapshot: VenueCostSnapshot,
    *,
    gross_decimal_odds: Decimal,
    stake: Decimal,
) -> tuple[Decimal, Decimal]:
    params = snapshot.formula_parameters
    rate = params.get("rate")
    exponent = params.get("exponent")
    if rate is None or exponent is None:
        raise ValueError("Polymarket taker formula requires rate and exponent")
    probability = Decimal("1") / gross_decimal_odds
    if probability <= 0 or probability >= 1:
        raise ValueError("Polymarket contract price p must be in (0, 1)")
    contracts = stake / probability
    curve = probability * (Decimal("1") - probability)
    if exponent != 1:
        curve = curve**exponent
    fee_raw = contracts * rate * curve
    fee = fee_raw.quantize(POLYMARKET_FEE_INCREMENT, rounding=ROUND_HALF_UP)
    if fee < POLYMARKET_FEE_INCREMENT:
        fee = Decimal("0")
    return fee, stake * gross_decimal_odds - fee


def _normalize_schedule(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {
        "rate": _stringify(_decimal_or_none(_first_value(value, "rate"))),
        "exponent": _stringify(_decimal_or_none(_first_value(value, "exponent"))),
        "taker_only": _bool_or_none(_first_value(value, "takerOnly", "taker_only")),
        "rebate_rate": _stringify(_decimal_or_none(_first_value(value, "rebateRate", "rebate_rate"))),
    }


def _unknown(
    *,
    captured_at: datetime,
    source_market_id: str | None,
    detail: str,
    action: MarketAction,
    order_role: OrderRole,
) -> VenueCostSnapshot:
    return VenueCostSnapshot(
        venue=VenueName.POLYMARKET,
        action=action,
        fee_basis=FeeBasis.UNKNOWN,
        known_status=CostKnownStatus.UNKNOWN,
        captured_at=captured_at,
        source="polymarket_fee_schedule:market:unknown",
        source_market_id=source_market_id,
        order_role=order_role,
        fee_scope=FeeScope.PER_QUOTE,
        currency="USD",
        snapshot_id=f"polymarket:market:unknown:{source_market_id or 'unknown'}",
        detail=detail,
    )


def _market_id(payload: Mapping[str, Any]) -> str | None:
    for key in ("id", "condition_id", "conditionId", "market_id", "marketId"):
        value = payload.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def _first_value(payload: Mapping[str, Any], *keys: str, default: Any = None) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None and payload[key] != "":
            return payload[key]
    return default


def _stringify(value: Decimal | None) -> str | None:
    return str(value) if value is not None else None


def _bool_or_none(value: Any) -> bool | None:
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, Decimal)) and value in (0, 1):
        return bool(int(value))
    if isinstance(value, str):
        folded = value.strip().casefold()
        if folded in {"true", "yes", "1"}:
            return True
        if folded in {"false", "no", "0"}:
            return False
    return None


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
