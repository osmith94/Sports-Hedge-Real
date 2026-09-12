from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import CostKnownStatus, FeeBasis, FeeScope, MarketAction, OrderRole, VenueCostSnapshot
from sports_hedge.fees.effective import CostRuleError, apply_venue_costs
from sports_hedge.fees.models import FeeSnapshot


CAPTURED = datetime(2026, 9, 12, 7, 0, tzinfo=UTC)


def _snapshot(**overrides: object) -> VenueCostSnapshot:
    payload: dict[str, object] = {
        "venue": VenueName.MATCHBOOK,
        "action": MarketAction.BACK,
        "fee_basis": FeeBasis.PROFIT_COMMISSION,
        "known_status": CostKnownStatus.KNOWN,
        "captured_at": CAPTURED,
        "source": "test",
        "source_market_id": "mb-1",
        "rate": Decimal("0.05"),
        "currency": "GBP",
        "effective_from": CAPTURED,
        "snapshot_id": "snap-1",
    }
    payload.update(overrides)
    return VenueCostSnapshot.model_validate(payload)


def test_profit_commission_shares_formula_with_legacy_fee_snapshot() -> None:
    snapshot = _snapshot()
    economics = apply_venue_costs(snapshot, gross_decimal_odds=Decimal("2.20"))
    legacy = FeeSnapshot(venue=VenueName.MATCHBOOK, profit_haircut_rate=Decimal("0.05"))

    assert economics.net_decimal_equivalent == legacy.apply_to_decimal_odds(Decimal("2.20"))
    assert economics.cost_adjusted_implied_probability == Decimal("1") / economics.net_decimal_equivalent
    assert economics.fee_snapshot_id == "snap-1"


def test_payout_basis_is_not_a_profit_haircut() -> None:
    profit = apply_venue_costs(_snapshot(), gross_decimal_odds=Decimal("2.20"))
    payout = apply_venue_costs(
        _snapshot(fee_basis=FeeBasis.PAYOUT),
        gross_decimal_odds=Decimal("2.20"),
    )

    assert payout.net_decimal_equivalent == Decimal("2.20") * Decimal("0.95")
    assert payout.net_decimal_equivalent < profit.net_decimal_equivalent


def test_unknown_and_formula_bases_fail_closed() -> None:
    unknown = _snapshot(fee_basis=FeeBasis.UNKNOWN, known_status=CostKnownStatus.UNKNOWN, rate=None)
    formula = _snapshot(fee_basis=FeeBasis.FORMULA, rate=None)

    with pytest.raises(CostRuleError, match="unknown"):
        apply_venue_costs(unknown, gross_decimal_odds=Decimal("2.20"))
    with pytest.raises(CostRuleError, match="FORMULA"):
        apply_venue_costs(formula, gross_decimal_odds=Decimal("2.20"))


def test_buy_action_uses_same_back_compatible_rules() -> None:
    economics = apply_venue_costs(
        _snapshot(action=MarketAction.BUY, venue=VenueName.POLYMARKET),
        gross_decimal_odds=Decimal("2.00"),
    )
    assert economics.action is MarketAction.BUY
    assert economics.net_decimal_equivalent > 1


def test_lay_and_sell_are_not_routed_through_back_haircut() -> None:
    with pytest.raises(CostRuleError, match="Lay/sell"):
        apply_venue_costs(
            _snapshot(action=MarketAction.LAY),
            gross_decimal_odds=Decimal("2.20"),
        )
    with pytest.raises(CostRuleError, match="Lay/sell"):
        apply_venue_costs(
            _snapshot(action=MarketAction.SELL, venue=VenueName.POLYMARKET),
            gross_decimal_odds=Decimal("2.20"),
        )


def test_market_net_and_period_netted_scopes_are_unsupported() -> None:
    with pytest.raises(CostRuleError, match="per-quote"):
        apply_venue_costs(
            _snapshot(fee_scope=FeeScope.MARKET_NET_PNL),
            gross_decimal_odds=Decimal("2.20"),
        )
    with pytest.raises(CostRuleError, match="per-quote"):
        apply_venue_costs(
            _snapshot(fee_scope=FeeScope.NETTED_COMMISSION),
            gross_decimal_odds=Decimal("2.20"),
        )


def test_unknown_order_role_fails_closed() -> None:
    with pytest.raises(CostRuleError, match="Order role"):
        apply_venue_costs(
            _snapshot(order_role=OrderRole.UNKNOWN),
            gross_decimal_odds=Decimal("2.20"),
        )
