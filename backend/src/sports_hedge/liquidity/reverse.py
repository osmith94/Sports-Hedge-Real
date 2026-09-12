"""Reverse-side order-book walks for paper close plans.

These walks never place orders. They price the economically opposite action
to an already-open back/buy using visible depth only.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

from sports_hedge.liquidity.book import BookLevel


class ReverseFill(BaseModel):
    requested_quantity: Decimal
    filled_quantity: Decimal
    remaining_quantity: Decimal
    weighted_average_odds: Decimal | None
    worst_odds: Decimal | None
    fully_filled: bool
    levels_consumed: int = Field(ge=0)
    available_capacity: Decimal
    matched_stake: Decimal = Decimal("0")
    liability: Decimal = Decimal("0")
    proceeds: Decimal = Decimal("0")
    shares_sold: Decimal = Decimal("0")


def walk_lay_to_cover_payout(levels: list[BookLevel], required_payout: Decimal) -> ReverseFill:
    """Consume exchange lay depth to hedge a backer's potential payout.

    Lay odds are never treated as back odds. Best close is the lowest lay
    price. ``available_stake`` is the matched-stake capacity at that lay odds.
    Liability at a level is ``matched * (odds - 1)``.
    """

    if required_payout <= 0:
        raise ValueError("required_payout must be positive")

    remaining = required_payout
    matched = Decimal("0")
    liability = Decimal("0")
    worst: Decimal | None = None
    consumed = 0
    capacity_payout = sum((level.available_stake * level.decimal_odds for level in levels), Decimal("0"))

    for level in sorted(levels, key=lambda item: item.decimal_odds):
        if remaining <= 0:
            break
        if level.decimal_odds <= 1 or level.available_stake <= 0:
            continue
        take = min(level.available_stake, remaining / level.decimal_odds)
        if take <= 0:
            continue
        matched += take
        remaining -= take * level.decimal_odds
        liability += take * (level.decimal_odds - Decimal("1"))
        worst = level.decimal_odds
        consumed += 1

    if remaining < 0 and remaining.copy_abs() < Decimal("0.00000001"):
        remaining = Decimal("0")

    average = None if matched == 0 else (required_payout - remaining) / matched
    return ReverseFill(
        requested_quantity=required_payout,
        filled_quantity=required_payout - remaining,
        remaining_quantity=remaining,
        weighted_average_odds=average,
        worst_odds=worst,
        fully_filled=remaining == 0,
        levels_consumed=consumed,
        available_capacity=capacity_payout,
        matched_stake=matched,
        liability=liability,
    )


def walk_prediction_sell(levels: list[BookLevel], required_shares: Decimal) -> ReverseFill:
    """Sell prediction-market shares into bid depth.

    Levels use the provider-neutral encoding already used on observations:
    ``decimal_odds = 1 / probability`` and ``available_stake`` is native
    notional (``probability * shares``). Best sale is highest probability
    (lowest decimal odds). Lay/back odds are not substituted for each other.
    """

    if required_shares <= 0:
        raise ValueError("required_shares must be positive")

    remaining = required_shares
    sold = Decimal("0")
    proceeds = Decimal("0")
    worst: Decimal | None = None
    consumed = 0
    capacity_shares = sum((level.available_stake * level.decimal_odds for level in levels), Decimal("0"))

    for level in sorted(levels, key=lambda item: item.decimal_odds):
        if remaining <= 0:
            break
        if level.decimal_odds <= 1 or level.available_stake <= 0:
            continue
        shares_at_level = level.available_stake * level.decimal_odds
        take = min(shares_at_level, remaining)
        if take <= 0:
            continue
        probability = Decimal("1") / level.decimal_odds
        sold += take
        remaining -= take
        proceeds += take * probability
        worst = level.decimal_odds
        consumed += 1

    average = None if sold == 0 else sold / proceeds if proceeds > 0 else None
    return ReverseFill(
        requested_quantity=required_shares,
        filled_quantity=sold,
        remaining_quantity=remaining,
        weighted_average_odds=average,
        worst_odds=worst,
        fully_filled=remaining == 0,
        levels_consumed=consumed,
        available_capacity=capacity_shares,
        proceeds=proceeds,
        shares_sold=sold,
    )
