from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel

from sports_hedge.fees.cost import (
    CostKnownStatus,
    FeeBasis,
    MarketAction,
    VenueCostSnapshot,
)

_BACK_BUY = {MarketAction.BACK, MarketAction.BUY}


class CostRuleError(ValueError):
    """Fail-closed cost application. Never invent a substitute formula."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(detail)
        self.reason = reason
        self.detail = detail


class EffectiveLegEconomics(BaseModel):
    """Net outcome economics for a unit (or supplied) back/buy stake."""

    gross_odds: Decimal
    stake: Decimal
    venue_fee: Decimal
    other_known_leg_costs: Decimal
    gross_payoff: Decimal
    net_payoff: Decimal
    net_decimal_equivalent: Decimal
    cost_adjusted_implied_probability: Decimal
    fee_snapshot_id: str | None
    fee_basis: FeeBasis
    action: MarketAction


def apply_venue_costs(
    snapshot: VenueCostSnapshot,
    *,
    gross_decimal_odds: Decimal,
    stake: Decimal = Decimal("1"),
) -> EffectiveLegEconomics:
    """Apply the snapshot's own cost rule. Does not coerce bases together."""

    if snapshot.known_status is CostKnownStatus.UNKNOWN or snapshot.fee_basis is FeeBasis.UNKNOWN:
        raise CostRuleError("unknown_costs", "Required venue costs are unknown")
    if snapshot.action not in _BACK_BUY:
        raise CostRuleError(
            "unsupported_action",
            "Lay/sell/synthetic legs require state-payoff economics, not back net odds",
        )
    if snapshot.currency != "GBP":
        raise CostRuleError(
            "unconverted_currency",
            "Non-GBP venue costs must be converted before GBP value ranking",
        )
    if gross_decimal_odds <= 1:
        raise CostRuleError("invalid_gross_odds", "gross_decimal_odds must exceed 1")
    if stake <= 0:
        raise CostRuleError("invalid_stake", "stake must be positive")

    gross_payoff = stake * gross_decimal_odds
    other_known = Decimal("0")
    fee, net_payoff = _net_win_payoff(snapshot, gross_decimal_odds=gross_decimal_odds, stake=stake)
    net_odds = net_payoff / stake
    if net_odds <= 1:
        raise CostRuleError("costs_consume_payout", "Applicable costs leave no positive payout")

    return EffectiveLegEconomics(
        gross_odds=gross_decimal_odds,
        stake=stake,
        venue_fee=fee,
        other_known_leg_costs=other_known,
        gross_payoff=gross_payoff,
        net_payoff=net_payoff,
        net_decimal_equivalent=net_odds,
        cost_adjusted_implied_probability=Decimal("1") / net_odds,
        fee_snapshot_id=snapshot.snapshot_id,
        fee_basis=snapshot.fee_basis,
        action=snapshot.action,
    )


def profit_commission_net_odds(gross_decimal_odds: Decimal, rate: Decimal) -> Decimal:
    """Shared profit-commission rule used by Research and FeeSnapshot."""

    if gross_decimal_odds <= 1:
        raise ValueError("decimal_odds must exceed 1")
    if rate < 0 or rate >= 1:
        raise ValueError("profit commission rate must be in [0, 1)")
    profit = gross_decimal_odds - Decimal("1")
    return Decimal("1") + profit * (Decimal("1") - rate)


def _net_win_payoff(
    snapshot: VenueCostSnapshot,
    *,
    gross_decimal_odds: Decimal,
    stake: Decimal,
) -> tuple[Decimal, Decimal]:
    basis = snapshot.fee_basis
    if basis is FeeBasis.NONE_CONFIRMED:
        return Decimal("0"), stake * gross_decimal_odds
    if basis is FeeBasis.PROFIT_COMMISSION:
        assert snapshot.rate is not None
        net_odds = profit_commission_net_odds(gross_decimal_odds, snapshot.rate)
        net_payoff = stake * net_odds
        return stake * gross_decimal_odds - net_payoff, net_payoff
    if basis is FeeBasis.PAYOUT:
        assert snapshot.rate is not None
        fee = stake * gross_decimal_odds * snapshot.rate
        return fee, stake * gross_decimal_odds - fee
    if basis is FeeBasis.STAKE_OR_NOTIONAL:
        assert snapshot.rate is not None
        fee = stake * snapshot.rate
        return fee, stake * gross_decimal_odds - fee
    if basis is FeeBasis.TRANSACTION:
        assert snapshot.rate is not None
        fee = stake * snapshot.rate + (snapshot.fixed_amount or Decimal("0"))
        return fee, stake * gross_decimal_odds - fee
    if basis is FeeBasis.FIXED:
        assert snapshot.fixed_amount is not None
        return snapshot.fixed_amount, stake * gross_decimal_odds - snapshot.fixed_amount
    if basis is FeeBasis.FORMULA:
        raise CostRuleError(
            "unsupported_fee_basis",
            "FORMULA fee basis has no authorised rule registered for this snapshot",
        )
    raise CostRuleError("unsupported_fee_basis", f"No cost rule for fee basis {basis}")
