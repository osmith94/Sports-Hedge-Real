"""Kalshi fee snapshots from official event/series metadata.

Quadratic taker: round_up(M × 0.07 × C × P × (1 − P))
Maker (only when fee_type is quadratic_with_maker_fees):
round_up(M × 0.0175 × C × P × (1 − P))

Effective fee precedence (Core Tenet 15):
- a complete event override (`fee_type_override` + `fee_multiplier_override`)
  replaces series `fee_type` / `fee_multiplier`;
- otherwise the series values are used;
- partial or invalid override combinations fail closed and never silently
  fall back to series economics.

Flat / unknown fee_type fail closed. Sports Hedge does not hard-code a global
haircut and does not treat missing fee metadata as zero.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import ROUND_CEILING, Decimal
from typing import Any

from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import (
    CostKnownStatus,
    FeeBasis,
    FeeScope,
    MarketAction,
    OrderRole,
    VenueCostSnapshot,
)

KALSHI_QUADRATIC_FORMULA = "kalshi_quadratic"
KALSHI_TAKER_COEFFICIENT = Decimal("0.07")
KALSHI_MAKER_COEFFICIENT = Decimal("0.0175")
KALSHI_CENTICENT = Decimal("0.0001")
FEE_PROVENANCE_SERIES = "series"
FEE_PROVENANCE_EVENT_OVERRIDE = "event_override"


def resolve_kalshi_fee_metadata(
    *,
    event: dict[str, Any] | None = None,
    series: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Resolve event-override vs series fee fields without guessing.

    A valid override requires both event fields. One-sided or empty-on-one-side
    combinations are `partial_event_fee_override` and must not fall back.
    """

    event_payload = event or {}
    series_payload = series or {}
    type_override = event_payload.get("fee_type_override")
    multiplier_override = event_payload.get("fee_multiplier_override")
    type_present = _override_present(type_override)
    multiplier_present = _override_present(multiplier_override)
    base = {
        "fee_type_override": type_override,
        "fee_multiplier_override": multiplier_override,
        "series_fee_type": series_payload.get("fee_type"),
        "series_fee_multiplier": series_payload.get("fee_multiplier"),
    }
    if type_present ^ multiplier_present:
        return {
            **base,
            "fee_type": None,
            "fee_multiplier": None,
            "fee_provenance": FEE_PROVENANCE_EVENT_OVERRIDE,
            "fee_resolution_error": "partial_event_fee_override",
        }
    if type_present and multiplier_present:
        return {
            **base,
            "fee_type": type_override,
            "fee_multiplier": multiplier_override,
            "fee_provenance": FEE_PROVENANCE_EVENT_OVERRIDE,
        }
    return {
        **base,
        "fee_type": series_payload.get("fee_type"),
        "fee_multiplier": series_payload.get("fee_multiplier"),
        "fee_provenance": FEE_PROVENANCE_SERIES,
    }


def kalshi_cost_from_series(
    series: dict[str, Any] | None,
    *,
    event: dict[str, Any] | None = None,
    action: MarketAction = MarketAction.BUY,
    order_role: OrderRole = OrderRole.TAKER,
    captured_at: datetime | None = None,
    source_market_id: str | None = None,
) -> VenueCostSnapshot:
    captured = captured_at or datetime.now(UTC)
    payload = series or {}
    if "fee_provenance" in payload or "fee_resolution_error" in payload:
        metadata = payload
    else:
        metadata = resolve_kalshi_fee_metadata(event=event, series=series)
    provenance = str(metadata.get("fee_provenance") or FEE_PROVENANCE_SERIES)
    resolution_error = str(metadata.get("fee_resolution_error") or "").strip()
    if resolution_error:
        return _unknown(
            captured_at=captured,
            source_market_id=source_market_id,
            action=action,
            detail=f"Kalshi fee metadata unresolved ({resolution_error}); no series fallback",
            order_role=order_role,
            provenance=provenance,
        )
    fee_type = str(metadata.get("fee_type") or "").strip().casefold()
    multiplier = _decimal_or_none(metadata.get("fee_multiplier"))
    if action not in {MarketAction.BUY, MarketAction.SELL}:
        return _unknown(
            captured_at=captured,
            source_market_id=source_market_id,
            action=action,
            detail=f"Kalshi fee snapshot requires BUY or SELL; got {action.value}",
            order_role=order_role,
            provenance=provenance,
        )
    if not fee_type:
        return _unknown(
            captured_at=captured,
            source_market_id=source_market_id,
            action=action,
            detail=f"Kalshi {provenance} fee_type missing",
            order_role=order_role,
            provenance=provenance,
        )
    if fee_type == "flat":
        return _unknown(
            captured_at=captured,
            source_market_id=source_market_id,
            action=action,
            detail=(
                f"Kalshi {provenance} flat fee_type is not modelled; "
                "Specific Trading Fees Table is required"
            ),
            order_role=order_role,
            provenance=provenance,
        )
    if fee_type not in {"quadratic", "quadratic_with_maker_fees"}:
        return _unknown(
            captured_at=captured,
            source_market_id=source_market_id,
            action=action,
            detail=f"Unsupported Kalshi {provenance} fee_type {fee_type}",
            order_role=order_role,
            provenance=provenance,
        )
    if multiplier is None:
        return _unknown(
            captured_at=captured,
            source_market_id=source_market_id,
            action=action,
            detail=f"Kalshi {provenance} fee_multiplier missing",
            order_role=order_role,
            provenance=provenance,
        )
    if order_role is OrderRole.UNKNOWN:
        return _unknown(
            captured_at=captured,
            source_market_id=source_market_id,
            action=action,
            detail="Kalshi order role unknown",
            order_role=order_role,
            provenance=provenance,
        )
    if order_role is OrderRole.MAKER and fee_type == "quadratic":
        return VenueCostSnapshot(
            venue=VenueName.KALSHI,
            action=action,
            fee_basis=FeeBasis.NONE_CONFIRMED,
            known_status=CostKnownStatus.KNOWN,
            captured_at=captured,
            source=_schedule_source(provenance, "quadratic"),
            source_market_id=source_market_id,
            order_role=order_role,
            fee_scope=FeeScope.PER_QUOTE,
            currency="USD",
            formula_name=None,
            formula_parameters={"fee_multiplier": multiplier, "fee_type_quadratic": Decimal("1")},
            snapshot_id=f"kalshi:{provenance}:quadratic:maker_none:{source_market_id or 'series'}",
            detail=(
                f"Kalshi quadratic {provenance}: resting maker orders are not charged the general "
                "trading fee unless the series is quadratic_with_maker_fees."
            ),
        )
    coefficient = (
        KALSHI_MAKER_COEFFICIENT if order_role is OrderRole.MAKER else KALSHI_TAKER_COEFFICIENT
    )
    if order_role is OrderRole.MAKER and fee_type != "quadratic_with_maker_fees":
        return _unknown(
            captured_at=captured,
            source_market_id=source_market_id,
            action=action,
            detail="Maker fee formula not authorised for this Kalshi fee_type",
            order_role=order_role,
            provenance=provenance,
        )
    kind = "quadratic_with_maker_fees" if fee_type == "quadratic_with_maker_fees" else "quadratic"
    return VenueCostSnapshot(
        venue=VenueName.KALSHI,
        action=action,
        fee_basis=FeeBasis.FORMULA,
        known_status=CostKnownStatus.KNOWN,
        captured_at=captured,
        source=_schedule_source(provenance, kind),
        source_market_id=source_market_id,
        order_role=order_role,
        fee_scope=FeeScope.PER_QUOTE,
        currency="USD",
        formula_name=KALSHI_QUADRATIC_FORMULA,
        formula_parameters={
            "coefficient": coefficient,
            "fee_multiplier": multiplier,
            "rounding_increment": KALSHI_CENTICENT,
        },
        snapshot_id=(
            f"kalshi:{provenance}:{fee_type}:{action.value}:{order_role.value}:"
            f"{source_market_id or 'series'}"
        ),
        detail=(
            "Official Kalshi general trading fee "
            "round_up(M × coefficient × C × P × (1 − P)); "
            f"fee_type={fee_type}; M={multiplier}; coefficient={coefficient}; "
            f"provenance={provenance}."
        ),
    )


def kalshi_closing_cost_from_series(
    series: dict[str, Any] | None,
    *,
    event: dict[str, Any] | None = None,
    order_role: OrderRole = OrderRole.TAKER,
    captured_at: datetime | None = None,
    source_market_id: str | None = None,
) -> VenueCostSnapshot:
    """Explicit SELL-close snapshot. Never silently reuses a BUY-only cost row."""

    return kalshi_cost_from_series(
        series,
        event=event,
        action=MarketAction.SELL,
        order_role=order_role,
        captured_at=captured_at,
        source_market_id=source_market_id,
    )


def apply_kalshi_quadratic(
    snapshot: VenueCostSnapshot,
    *,
    gross_decimal_odds: Decimal,
    stake: Decimal,
) -> tuple[Decimal, Decimal]:
    params = snapshot.formula_parameters
    coefficient = params.get("coefficient")
    multiplier = params.get("fee_multiplier")
    increment = params.get("rounding_increment") or KALSHI_CENTICENT
    if coefficient is None or multiplier is None:
        raise ValueError("Kalshi quadratic formula requires coefficient and fee_multiplier")
    probability = Decimal("1") / gross_decimal_odds
    if probability <= 0 or probability >= 1:
        raise ValueError("Kalshi contract price P must be in (0, 1)")
    contracts = stake / probability
    fee_raw = multiplier * coefficient * contracts * probability * (Decimal("1") - probability)
    fee = _round_up_fee_plus_position(fee_raw, position_cost=stake, increment=increment)
    return fee, stake * gross_decimal_odds - fee


def _round_up_fee_plus_position(
    fee: Decimal,
    *,
    position_cost: Decimal,
    increment: Decimal,
) -> Decimal:
    """Official rule: round up so fee + positionCost lands on a centicent."""

    if increment <= 0:
        raise ValueError("Kalshi rounding increment must be positive")
    total = fee + position_cost
    units = (total / increment).to_integral_value(rounding=ROUND_CEILING)
    rounded_total = units * increment
    rounded_fee = rounded_total - position_cost
    return max(rounded_fee, Decimal("0"))


def _unknown(
    *,
    captured_at: datetime,
    source_market_id: str | None,
    detail: str,
    order_role: OrderRole,
    provenance: str = FEE_PROVENANCE_SERIES,
    action: MarketAction = MarketAction.BUY,
) -> VenueCostSnapshot:
    return VenueCostSnapshot(
        venue=VenueName.KALSHI,
        action=action,
        fee_basis=FeeBasis.UNKNOWN,
        known_status=CostKnownStatus.UNKNOWN,
        captured_at=captured_at,
        source=_schedule_source(provenance, "unknown"),
        source_market_id=source_market_id,
        order_role=order_role,
        fee_scope=FeeScope.PER_QUOTE,
        currency="USD",
        snapshot_id=f"kalshi:{provenance}:unknown:{action.value}:{source_market_id or 'series'}",
        detail=detail,
    )


def _schedule_source(provenance: str, kind: str) -> str:
    return f"kalshi_fee_schedule:{provenance}:{kind}"


def _override_present(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str) and not value.strip():
        return False
    return True


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except Exception:
        return None
