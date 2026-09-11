from __future__ import annotations

from decimal import Decimal

from sports_hedge.research.value.contracts import VenueQuote


def net_back_odds(displayed_decimal_odds: Decimal, commission_rate: Decimal) -> Decimal:
    """Effective decimal odds after a known profit commission.

    Does not invent fees: callers must only pass a supplied commission rate.
    """

    if displayed_decimal_odds <= 1:
        raise ValueError("displayed_decimal_odds must exceed 1")
    if commission_rate < 0 or commission_rate >= 1:
        raise ValueError("commission_rate must be in [0, 1)")
    profit = displayed_decimal_odds - Decimal("1")
    return Decimal("1") + profit * (Decimal("1") - commission_rate)


def raw_implied_probability(displayed_decimal_odds: Decimal) -> Decimal:
    if displayed_decimal_odds <= 1:
        raise ValueError("displayed_decimal_odds must exceed 1")
    return Decimal("1") / displayed_decimal_odds


def cost_adjusted_implied_probability(net_odds: Decimal) -> Decimal:
    if net_odds <= 1:
        raise ValueError("net_odds must exceed 1")
    return Decimal("1") / net_odds


def expected_profit_per_unit(model_probability: Decimal, net_odds: Decimal) -> Decimal:
    """EV of staking 1 at net back/buy odds.

    ``p * net_win_if_success - (1 - p) * 1`` equals ``p * net_odds - 1``.
    """

    return model_probability * net_odds - Decimal("1")


def probability_edge_percentage_points(
    model_probability: Decimal,
    cost_adjusted_market_probability: Decimal,
) -> Decimal:
    return (model_probability - cost_adjusted_market_probability) * Decimal("100")


def quote_net_odds(quote: VenueQuote) -> Decimal:
    if quote.commission_rate is None:
        raise ValueError("commission_rate is required to compute net odds")
    return net_back_odds(quote.displayed_decimal_odds, quote.commission_rate)
