from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field


class BookLevel(BaseModel):
    decimal_odds: Decimal = Field(gt=Decimal("1"))
    available_stake: Decimal = Field(gt=Decimal("0"))


class BookFill(BaseModel):
    requested_stake: Decimal
    filled_stake: Decimal
    remaining_stake: Decimal
    weighted_average_odds: Decimal | None
    worst_odds: Decimal | None
    fully_filled: bool
    levels_consumed: int


class OrderBookWalker:
    """Consume back-bet liquidity from best odds to worst odds."""

    def fill(self, levels: list[BookLevel], requested_stake: Decimal) -> BookFill:
        if requested_stake <= 0:
            raise ValueError("requested_stake must be positive")

        remaining = requested_stake
        filled = Decimal("0")
        weighted_return = Decimal("0")
        worst_odds: Decimal | None = None
        levels_consumed = 0

        for level in sorted(levels, key=lambda item: item.decimal_odds, reverse=True):
            if remaining <= 0:
                break
            take = min(level.available_stake, remaining)
            if take <= 0:
                continue
            filled += take
            remaining -= take
            weighted_return += take * level.decimal_odds
            worst_odds = level.decimal_odds
            levels_consumed += 1

        average = None if filled == 0 else weighted_return / filled
        return BookFill(
            requested_stake=requested_stake,
            filled_stake=filled,
            remaining_stake=remaining,
            weighted_average_odds=average,
            worst_odds=worst_odds,
            fully_filled=remaining == 0,
            levels_consumed=levels_consumed,
        )
