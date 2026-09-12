from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, getcontext

from sports_hedge.arbitrage.models import ArbitrageSolution, ArbitrageStake, ExecutableQuote
from sports_hedge.domain.models import VenueName


getcontext().prec = 28


class CompleteSetArbitrageSolver:
    """Solve mutually exclusive and collectively exhaustive outcome sets.

    This covers conventional 2-way and 3-way football arbitrage. The solver takes
    already-normalized effective odds and depth limits, keeping venue fee/FX logic
    outside the mathematical core.
    """

    def solve(
        self,
        quotes: list[ExecutableQuote],
        *,
        capital_limit: Decimal | None = None,
        venue_capital_limits: Mapping[VenueName, Decimal] | None = None,
    ) -> ArbitrageSolution:
        if len(quotes) < 2:
            return ArbitrageSolution(
                is_arbitrage=False,
                implied_probability_sum=Decimal("1"),
                rejection_reason="incomplete_outcome_set",
            )

        outcomes = [quote.outcome for quote in quotes]
        if len(set(outcomes)) != len(outcomes):
            return ArbitrageSolution(
                is_arbitrage=False,
                implied_probability_sum=Decimal("1"),
                rejection_reason="duplicate_outcome",
            )

        inverse_prices = [Decimal("1") / quote.net_decimal_odds for quote in quotes]
        implied_sum = sum(inverse_prices, Decimal("0"))
        if implied_sum >= Decimal("1"):
            return ArbitrageSolution(
                is_arbitrage=False,
                implied_probability_sum=implied_sum,
                rejection_reason="no_positive_edge",
            )

        # For equal state return R, stake_i = R / odds_i. With total stake B:
        # B = R * sum(1 / odds_i), therefore R = B / implied_sum.
        # Each quote's depth caps B to max_stake_i * odds_i * implied_sum.
        max_total_stakes = [
            quote.max_stake * quote.net_decimal_odds * implied_sum for quote in quotes
        ]
        total_stake = min(max_total_stakes)
        if capital_limit is not None:
            if capital_limit <= 0:
                raise ValueError("capital_limit must be positive")
            total_stake = min(total_stake, capital_limit)

        if any(quote.venue is VenueName.SMARKETS for quote in quotes):
            return ArbitrageSolution(
                is_arbitrage=False,
                implied_probability_sum=implied_sum,
                rejection_reason="smarkets_excluded_from_solver",
            )

        if venue_capital_limits:
            implied_by_venue: dict[VenueName, Decimal] = {}
            for quote in quotes:
                implied_by_venue[quote.venue] = implied_by_venue.get(quote.venue, Decimal("0")) + (
                    Decimal("1") / quote.net_decimal_odds
                )
            for venue, venue_implied in implied_by_venue.items():
                if venue is VenueName.SMARKETS:
                    continue
                if venue not in venue_capital_limits:
                    continue
                venue_cap = venue_capital_limits[venue]
                if venue_cap < 0:
                    raise ValueError("venue_capital_limits must be non-negative")
                if venue_cap == 0 or venue_implied <= 0:
                    return ArbitrageSolution(
                        is_arbitrage=False,
                        implied_probability_sum=implied_sum,
                        rejection_reason="insufficient_venue_capital",
                    )
                total_stake = min(total_stake, venue_cap * implied_sum / venue_implied)

        if total_stake <= 0:
            return ArbitrageSolution(
                is_arbitrage=False,
                implied_probability_sum=implied_sum,
                rejection_reason="insufficient_venue_capital",
            )

        guaranteed_return = total_stake / implied_sum
        guaranteed_profit = guaranteed_return - total_stake
        roi = guaranteed_profit / total_stake

        stakes: list[ArbitrageStake] = []
        for quote in quotes:
            stake = guaranteed_return / quote.net_decimal_odds
            stakes.append(
                ArbitrageStake(
                    outcome=quote.outcome,
                    venue=quote.venue,
                    source_market_id=quote.source_market_id,
                    stake=stake,
                    net_decimal_odds=quote.net_decimal_odds,
                    state_return=stake * quote.net_decimal_odds,
                )
            )

        return ArbitrageSolution(
            is_arbitrage=True,
            implied_probability_sum=implied_sum,
            total_stake=total_stake,
            guaranteed_return=guaranteed_return,
            guaranteed_profit=guaranteed_profit,
            roi=roi,
            stakes=stakes,
        )
