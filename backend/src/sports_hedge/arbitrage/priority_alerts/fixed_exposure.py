from __future__ import annotations

from decimal import Decimal

from sports_hedge.arbitrage.models import ArbitrageSolution, ArbitrageStake
from sports_hedge.arbitrage.priority_alerts.models import PriorityLeg


def revalidate_fixed_external_exposure(
    *,
    executed_stake_reporting: Decimal,
    executed_price: Decimal,
    external_leg: PriorityLeg,
    hedge_legs: list[PriorityLeg],
) -> ArbitrageSolution:
    """Hedge a already-executed external stake. The confirmed size is not a max-stake cap.

    Ordinary complete-set solving may scale every leg down to the shallowest book. After an
    external fill that is no longer valid: the operator is exposed to the full confirmed amount,
    so remaining internal legs must cover that exact state return or revalidation fails closed.
    """

    if executed_stake_reporting <= 0:
        return ArbitrageSolution(
            is_arbitrage=False,
            implied_probability_sum=Decimal("1"),
            rejection_reason="confirmed_external_size_must_be_positive",
        )
    if executed_price <= 1:
        return ArbitrageSolution(
            is_arbitrage=False,
            implied_probability_sum=Decimal("1"),
            rejection_reason="confirmed_external_price_invalid",
        )
    if not hedge_legs:
        return ArbitrageSolution(
            is_arbitrage=False,
            implied_probability_sum=Decimal("1"),
            rejection_reason="missing_internal_hedge_legs",
        )

    guaranteed_return = executed_stake_reporting * executed_price
    stakes = [
        ArbitrageStake(
            outcome=external_leg.outcome,
            venue=external_leg.venue,
            source_market_id=external_leg.source_market_id,
            stake=executed_stake_reporting,
            net_decimal_odds=executed_price,
            state_return=guaranteed_return,
        )
    ]
    for leg in hedge_legs:
        required = guaranteed_return / leg.net_decimal_odds
        if required > leg.max_stake_reporting:
            return ArbitrageSolution(
                is_arbitrage=False,
                implied_probability_sum=Decimal("1"),
                rejection_reason="confirmed_external_exposure_not_fully_hedgeable",
            )
        stakes.append(
            ArbitrageStake(
                outcome=leg.outcome,
                venue=leg.venue,
                source_market_id=leg.source_market_id,
                stake=required,
                net_decimal_odds=leg.net_decimal_odds,
                state_return=required * leg.net_decimal_odds,
            )
        )

    inverse_prices = [Decimal("1") / executed_price] + [
        Decimal("1") / leg.net_decimal_odds for leg in hedge_legs
    ]
    implied_sum = sum(inverse_prices, Decimal("0"))
    total_stake = sum((item.stake for item in stakes), Decimal("0"))
    guaranteed_profit = guaranteed_return - total_stake
    if implied_sum >= Decimal("1") or guaranteed_profit <= 0:
        return ArbitrageSolution(
            is_arbitrage=False,
            implied_probability_sum=implied_sum,
            total_stake=total_stake,
            guaranteed_return=guaranteed_return,
            guaranteed_profit=guaranteed_profit,
            rejection_reason="non_positive_minimum_payoff",
            stakes=stakes,
        )

    return ArbitrageSolution(
        is_arbitrage=True,
        implied_probability_sum=implied_sum,
        total_stake=total_stake,
        guaranteed_return=guaranteed_return,
        guaranteed_profit=guaranteed_profit,
        roi=guaranteed_profit / total_stake,
        stakes=stakes,
    )
