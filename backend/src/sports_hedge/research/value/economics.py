from __future__ import annotations

from decimal import Decimal

from sports_hedge.fees.effective import (
    CostRuleError,
    EffectiveLegEconomics,
    apply_venue_costs,
    profit_commission_net_odds,
)
from sports_hedge.research.value.contracts import VenueQuote


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


def quote_effective_economics(quote: VenueQuote) -> EffectiveLegEconomics:
    """Net price from the quote's own cost rule, not a generic commission haircut."""

    return apply_venue_costs(
        quote.cost,
        gross_decimal_odds=quote.displayed_decimal_odds,
    )


def quote_net_odds(quote: VenueQuote) -> Decimal:
    return quote_effective_economics(quote).net_decimal_equivalent


def quote_cost_rejection(quote: VenueQuote) -> str | None:
    try:
        quote_effective_economics(quote)
    except CostRuleError as exc:
        return exc.reason
    return None


# Preserved name for profit-commission arithmetic used by tests and FeeSnapshot.
net_back_odds = profit_commission_net_odds
