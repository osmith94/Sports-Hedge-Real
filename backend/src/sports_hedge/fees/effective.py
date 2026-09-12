from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel

from sports_hedge.fees.cost import (
    CostKnownStatus,
    FeeBasis,
    FeeScope,
    MarketAction,
    OrderRole,
    VenueCostSnapshot,
    require_aware_utc,
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
    as_of: datetime | None = None,
    quoted_at: datetime | None = None,
    require_gbp: bool = True,
) -> EffectiveLegEconomics:
    """Apply the snapshot's own cost rule. Does not coerce bases together."""

    if snapshot.known_status is CostKnownStatus.UNKNOWN or snapshot.fee_basis is FeeBasis.UNKNOWN:
        raise CostRuleError("unknown_costs", "Required venue costs are unknown")
    if snapshot.fee_scope is FeeScope.UNKNOWN:
        raise CostRuleError("unknown_fee_scope", "Required fee scope is unknown")
    if snapshot.fee_scope is not FeeScope.PER_QUOTE:
        raise CostRuleError(
            "unsupported_fee_scope",
            "Market-net, account-period and netted commission schemes are not modelled by per-quote effective price",
        )
    if snapshot.order_role is OrderRole.UNKNOWN:
        raise CostRuleError(
            "unknown_order_role",
            "Order role must be explicit (maker/taker/not_applicable); unknown is fail-closed",
        )
    if snapshot.action not in _BACK_BUY:
        raise CostRuleError(
            "unsupported_action",
            "Lay/sell/synthetic legs require state-payoff economics, not back net odds",
        )
    if snapshot.currency != "GBP":
        if require_gbp:
            raise CostRuleError(
                "unconverted_currency",
                "Non-GBP venue costs must be converted before GBP value ranking",
            )
        if not snapshot.is_rate_only():
            raise CostRuleError(
                "unconverted_currency",
                "Fixed native fees cannot be applied to GBP payoff without an FX conversion of the fee",
            )
    if gross_decimal_odds <= 1:
        raise CostRuleError("invalid_gross_odds", "gross_decimal_odds must exceed 1")
    if stake <= 0:
        raise CostRuleError("invalid_stake", "stake must be positive")
    _reject_inapplicable_snapshot(snapshot, as_of=as_of, quoted_at=quoted_at)

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


def _reject_inapplicable_snapshot(
    snapshot: VenueCostSnapshot,
    *,
    as_of: datetime | None,
    quoted_at: datetime | None,
) -> None:
    if as_of is not None:
        require_aware_utc(as_of, "as_of")
        if snapshot.captured_at > as_of:
            raise CostRuleError(
                "future_captured_cost",
                "Fee snapshot captured_at is after evaluation time",
            )
        if snapshot.effective_from is not None and snapshot.effective_from > as_of:
            raise CostRuleError(
                "future_effective_cost",
                "Fee snapshot effective_from is after evaluation time",
            )
    if quoted_at is not None:
        require_aware_utc(quoted_at, "quoted_at")
        if snapshot.effective_from is not None and snapshot.effective_from > quoted_at:
            raise CostRuleError(
                "future_effective_cost",
                "Fee snapshot effective_from is after the quote time",
            )


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
