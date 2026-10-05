"""Explicit Price-2 to venue-native price and size translation.

``requested_price`` is decimal odds. ``requested_size`` is the backer's stake
in the leg currency: the amount at risk, not the payout. A backer is worse off
at lower decimal odds. Rounding therefore only moves to a price that is equal
or better than the approved limit.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_CEILING, ROUND_DOWN, Decimal
from enum import StrEnum

from sports_hedge.domain.models import MarketSide


# Published Matchbook decimal ladder, August 2021.
# https://developers.matchbook.com/docs/new-odds-ladder-target-date-of-july-2021
# Invalid submitted odds are moved by Matchbook itself (up for back, down for
# lay). We snap first so the request is already on that ladder.
def _inclusive_range(start: Decimal, stop: Decimal, step: Decimal) -> tuple[Decimal, ...]:
    values: list[Decimal] = []
    current = start
    while current <= stop:
        values.append(current)
        current += step
    return tuple(values)


_LADDER_BANDS: tuple[tuple[str, str, str], ...] = (
    ("1.01", "2.00", "0.01"),
    ("2.02", "3.00", "0.02"),
    ("3.05", "4.00", "0.05"),
    ("4.10", "6.00", "0.10"),
    ("6.20", "10.00", "0.20"),
    ("10.50", "20.00", "0.50"),
    ("21", "30", "1"),
    ("32", "50", "2"),
    ("55", "100", "5"),
    ("110", "1000", "10"),
)
MATCHBOOK_DECIMAL_LADDER: tuple[Decimal, ...] = tuple(
    price
    for start, stop, step in _LADDER_BANDS
    for price in _inclusive_range(Decimal(start), Decimal(stop), Decimal(step))
)
# Stake increment is not published on Submit Offers. Minor currency units are
# the smallest increment this adapter will send.
MATCHBOOK_STAKE_QUANTUM = Decimal("0.01")
# Kalshi fixed-point contracts: responses use 2 decimals and the minimum
# granularity documented for fractional counts is 0.01.
KALSHI_CONTRACT_QUANTUM = Decimal("0.01")
# linear_cent. Finer documented grids use steps that divide $0.01, so a cent
# quote is still on those grids. It can be one cent more conservative.
KALSHI_DEFAULT_PRICE_TICK = Decimal("0.01")
# A translation that cannot place at least 99% of the approved stake fails.
# Smaller dust is the venue increment, not a redesigned hedge.
MATERIAL_STAKE_SHORTFALL = Decimal("0.01")


class KalshiContractSide(StrEnum):
    YES = "yes"
    NO = "no"


class KalshiAction(StrEnum):
    BUY = "buy"
    SELL = "sell"


class TranslationError(ValueError):
    """The approved price or size cannot be represented without worsening it."""


@dataclass(frozen=True)
class KalshiNativeLimit:
    """V2 order fields plus the legacy YES/NO and BUY/SELL names they encode.

    Create Order V2 quotes the YES price. ``bid`` is buy YES. ``ask`` is sell
    YES, which is the same exposure as buy NO. The legacy names are retained
    so the adapter does not collapse that into a generic back.
    """

    contract_side: KalshiContractSide
    action: KalshiAction
    book_side: str
    yes_price: Decimal
    unit_cost: Decimal
    contract_count: Decimal


def matchbook_limit_odds(decimal_odds: Decimal, *, side: MarketSide) -> Decimal:
    """Snap decimal odds onto the Matchbook ladder without worsening the back/lay."""

    if side not in {MarketSide.BACK, MarketSide.LAY}:
        raise TranslationError("Matchbook binary win/lose submissions are not supported")
    snapped = _ceil_ladder(decimal_odds) if side is MarketSide.BACK else _floor_ladder(decimal_odds)
    _require_one_step(decimal_odds, snapped, side=side)
    return snapped


def matchbook_stake(requested_stake: Decimal) -> Decimal:
    """Floor the back stake to the currency quantum. Never increase it."""

    if requested_stake <= 0:
        raise TranslationError("Matchbook stake must be positive")
    floored = _floor_to(requested_stake, MATCHBOOK_STAKE_QUANTUM)
    _require_stake_coverage(requested_stake, floored)
    return floored


def kalshi_contract_side(native_runner_id: str) -> KalshiContractSide:
    """Read the canonical runner suffix. A bare ticker is not assumed to be YES."""

    runner = native_runner_id.strip()
    if runner.endswith(":YES"):
        return KalshiContractSide.YES
    if runner.endswith(":NO"):
        return KalshiContractSide.NO
    raise TranslationError("Kalshi runner id must end in :YES or :NO")


def kalshi_ticker(native_runner_id: str, native_market_id: str) -> str:
    runner = native_runner_id.strip()
    suffix = ":YES" if runner.endswith(":YES") else ":NO" if runner.endswith(":NO") else ""
    if not suffix:
        raise TranslationError("Kalshi runner id must end in :YES or :NO")
    ticker = runner[: -len(suffix)]
    market = native_market_id.strip()
    if not ticker or ticker != market:
        raise TranslationError("Kalshi market id must be the ticker on the runner id")
    return ticker


def kalshi_action(side: MarketSide) -> KalshiAction:
    if side is MarketSide.BACK:
        return KalshiAction.BUY
    if side is MarketSide.LAY:
        return KalshiAction.SELL
    raise TranslationError("Kalshi execution only maps back to buy and lay to sell")


def kalshi_native_limit(
    *,
    decimal_odds: Decimal,
    requested_stake: Decimal,
    contract_side: KalshiContractSide,
    action: KalshiAction,
    tick: Decimal = KALSHI_DEFAULT_PRICE_TICK,
) -> KalshiNativeLimit:
    """Convert one approved decimal-odds limit into a YES-priced V2 order.

    Buy limits are floored so the premium cannot exceed ``1 / decimal_odds``.
    Sell limits are ceiled so the premium received cannot be lower.
    """

    if tick <= 0 or tick >= 1:
        raise TranslationError("Kalshi price tick must be inside (0, 1)")
    yes_price, unit_cost, book_side = _kalshi_yes_price(
        decimal_odds=decimal_odds,
        contract_side=contract_side,
        action=action,
        tick=tick,
    )
    contracts = _floor_to(requested_stake / unit_cost, KALSHI_CONTRACT_QUANTUM)
    if contracts < KALSHI_CONTRACT_QUANTUM:
        raise TranslationError("Kalshi stake is below one contract increment")
    _require_stake_coverage(requested_stake, contracts * unit_cost)
    return KalshiNativeLimit(
        contract_side=contract_side,
        action=action,
        book_side=book_side,
        yes_price=yes_price,
        unit_cost=unit_cost,
        contract_count=contracts,
    )


def decimal_odds_from_yes_price(yes_price: Decimal, limit: KalshiNativeLimit) -> Decimal:
    """Decimal odds of the exposure created by this limit, from a YES fill price."""

    if yes_price <= 0 or yes_price >= 1:
        raise TranslationError("Kalshi fill price must be inside (0, 1)")
    cost = yes_price if limit.book_side == "bid" else Decimal(1) - yes_price
    if cost <= 0:
        raise TranslationError("Kalshi fill price has no positive cost")
    return Decimal(1) / cost


def kalshi_wire_client_order_id(client_order_id: str) -> str:
    """Stable UUID-shaped id. The same Wave 1 id always produces the same value."""

    body = client_order_id.removeprefix("sh-")
    if len(body) != 32 or any(character not in "0123456789abcdef" for character in body):
        raise TranslationError("Kalshi client order id is not the Wave 1 digest")
    return f"{body[:8]}-{body[8:12]}-{body[12:16]}-{body[16:20]}-{body[20:]}"


def _kalshi_yes_price(
    *,
    decimal_odds: Decimal,
    contract_side: KalshiContractSide,
    action: KalshiAction,
    tick: Decimal,
) -> tuple[Decimal, Decimal, str]:
    if decimal_odds <= 1:
        raise TranslationError("decimal odds must be greater than 1")
    implied = Decimal(1) / decimal_odds
    # V2 price is the YES price. bid is long YES; ask is long NO.
    if contract_side is KalshiContractSide.YES and action is KalshiAction.BUY:
        yes_price = _floor_to(implied, tick)
        unit_cost = yes_price
        book_side = "bid"
    elif contract_side is KalshiContractSide.NO and action is KalshiAction.SELL:
        # Sell NO is long YES. Demand at least the implied NO premium, which
        # lowers the YES price. Never bid above that complementary price.
        no_premium = _ceil_to(implied, tick)
        yes_price = Decimal(1) - no_premium
        unit_cost = yes_price
        book_side = "bid"
    elif contract_side is KalshiContractSide.NO and action is KalshiAction.BUY:
        no_cost = _floor_to(implied, tick)
        yes_price = Decimal(1) - no_cost
        unit_cost = no_cost
        book_side = "ask"
    elif contract_side is KalshiContractSide.YES and action is KalshiAction.SELL:
        # Never sell YES below the implied price. Risk per contract is the complement.
        yes_price = _ceil_to(implied, tick)
        unit_cost = Decimal(1) - yes_price
        book_side = "ask"
    else:
        raise TranslationError("unsupported Kalshi side")
    if yes_price <= 0 or yes_price >= 1 or unit_cost <= 0:
        raise TranslationError("Kalshi limit is outside the tradable price range")
    if action is KalshiAction.BUY and Decimal(1) / unit_cost < decimal_odds:
        raise TranslationError("Kalshi price rounding would worsen the approved odds")
    if action is KalshiAction.SELL and unit_cost <= 0:
        raise TranslationError("Kalshi sell limit has no remaining risk")
    return yes_price, unit_cost, book_side


def _require_stake_coverage(requested: Decimal, represented: Decimal) -> None:
    if represented <= 0 or represented > requested:
        raise TranslationError("translated size would increase the approved stake")
    shortfall = requested - represented
    if shortfall > requested * MATERIAL_STAKE_SHORTFALL:
        raise TranslationError("size rounding would drop more than 1% of the approved stake")


def _require_one_step(requested: Decimal, snapped: Decimal, *, side: MarketSide) -> None:
    if side is MarketSide.BACK and snapped < requested:
        raise TranslationError("Matchbook back odds rounded to a worse price")
    if side is MarketSide.LAY and snapped > requested:
        raise TranslationError("Matchbook lay odds rounded to a worse price")
    index = MATCHBOOK_DECIMAL_LADDER.index(snapped)
    neighbour = MATCHBOOK_DECIMAL_LADDER[index - 1] if side is MarketSide.BACK else None
    if side is MarketSide.BACK:
        if index == 0 and snapped - requested > Decimal("0.01"):
            raise TranslationError("decimal odds are below the Matchbook ladder")
        if neighbour is not None and requested <= neighbour:
            raise TranslationError("Matchbook snap skipped a ladder step")
        return
    if index + 1 == len(MATCHBOOK_DECIMAL_LADDER) and requested - snapped > Decimal(10):
        raise TranslationError("decimal odds are above the Matchbook ladder")
    if index + 1 < len(MATCHBOOK_DECIMAL_LADDER):
        higher = MATCHBOOK_DECIMAL_LADDER[index + 1]
        if requested >= higher:
            raise TranslationError("Matchbook snap skipped a ladder step")


def _ceil_ladder(decimal_odds: Decimal) -> Decimal:
    for price in MATCHBOOK_DECIMAL_LADDER:
        if price >= decimal_odds:
            return price
    raise TranslationError("decimal odds are above the Matchbook ladder")


def _floor_ladder(decimal_odds: Decimal) -> Decimal:
    chosen: Decimal | None = None
    for price in MATCHBOOK_DECIMAL_LADDER:
        if price <= decimal_odds:
            chosen = price
            continue
        break
    if chosen is None:
        raise TranslationError("decimal odds are below the Matchbook ladder")
    return chosen


def _floor_to(value: Decimal, quantum: Decimal) -> Decimal:
    units = (value / quantum).to_integral_value(rounding=ROUND_DOWN)
    return units * quantum


def _ceil_to(value: Decimal, quantum: Decimal) -> Decimal:
    units = (value / quantum).to_integral_value(rounding=ROUND_CEILING)
    return units * quantum


# py-clob / polymarket-client 0.12.0 market-order rounding table.
# https://docs.polymarket.com/trading/orders/create
# Price decimals, size decimals. Amount decimals are applied by the SDK.
_POLYMARKET_TICKS: dict[str, int] = {
    "0.1": 2,
    "0.01": 2,
    "0.005": 2,
    "0.0025": 2,
    "0.001": 2,
    "0.0001": 2,
}
# Official immediate type. FAK fills what is available and cancels the rest.
# FOK would turn a partial book into a zero fill. The package already records
# PARTIAL from the actual quantity, so FAK is the IOC equivalent.
POLYMARKET_ORDER_TYPE = "FAK"


class PolymarketOrderSide(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


@dataclass(frozen=True)
class PolymarketNativeOrder:
    """One V2 token order with the same payoff direction as the approved leg.

    ``amount`` is pUSD to spend on a BUY. ``shares`` is the conditional-token
    size. A SELL's stake is the pUSD received if the token loses, which is the
    lay stake. Neither quantity is larger than the approved stake.
    """

    token_id: str
    side: PolymarketOrderSide
    price: Decimal
    amount: Decimal
    shares: Decimal
    stake: Decimal
    decimal_odds: Decimal
    tick_size: str
    order_type: str


def polymarket_token_id(native_runner_id: str) -> str:
    """The Price-2 runner id is the CLOB token id. A synthetic index is not tradable."""

    token = native_runner_id.strip()
    if not token.isdigit() or len(token) < 10 or ":" in native_runner_id:
        raise TranslationError("Polymarket execution requires the exact CLOB token id")
    return token


def polymarket_native_order(
    *,
    native_runner_id: str,
    native_market_id: str,
    side: MarketSide,
    decimal_odds: Decimal,
    requested_stake: Decimal,
    tick_size: str,
    minimum_shares: Decimal,
) -> PolymarketNativeOrder:
    """Translate one approved decimal-odds leg into a V2 BUY or SELL.

    BACK buys the selected token. LAY sells that same token. The price is
    snapped to the venue tick in the direction that does not worsen the
    approved odds. Size is floored. A stake below the venue minimum fails.
    """

    if not native_market_id.strip():
        raise TranslationError("Polymarket market id is required")
    if decimal_odds <= 1:
        raise TranslationError("decimal odds must be greater than 1")
    if requested_stake <= 0:
        raise TranslationError("Polymarket stake must be positive")
    if tick_size not in _POLYMARKET_TICKS:
        raise TranslationError("Polymarket tick size is not a current V2 increment")
    if minimum_shares <= 0:
        raise TranslationError("Polymarket minimum size is missing")
    token = polymarket_token_id(native_runner_id)
    tick = Decimal(tick_size)
    quantum = Decimal(10) ** -_POLYMARKET_TICKS[tick_size]
    implied = Decimal(1) / decimal_odds
    if side is MarketSide.BACK:
        price = _floor_to(implied, tick)
        if price <= 0 or price >= 1:
            raise TranslationError("Polymarket buy limit is outside the tradable price range")
        odds = Decimal(1) / price
        if odds < decimal_odds:
            raise TranslationError("Polymarket price rounding would worsen the approved odds")
        amount = _floor_to(requested_stake, quantum)
        if amount <= 0:
            raise TranslationError("Polymarket stake is below the size increment")
        _require_stake_coverage(requested_stake, amount)
        shares = amount / price
        if shares < minimum_shares:
            raise TranslationError("Polymarket stake is below the minimum order size")
        return PolymarketNativeOrder(
            token_id=token,
            side=PolymarketOrderSide.BUY,
            price=price,
            amount=amount,
            shares=shares,
            stake=amount,
            decimal_odds=odds,
            tick_size=tick_size,
            order_type=POLYMARKET_ORDER_TYPE,
        )
    if side is MarketSide.LAY:
        price = _ceil_to(implied, tick)
        if price <= 0 or price >= 1:
            raise TranslationError("Polymarket sell limit is outside the tradable price range")
        odds = Decimal(1) / price
        if odds > decimal_odds:
            raise TranslationError("Polymarket price rounding would worsen the approved lay")
        shares = _floor_to(requested_stake / price, quantum)
        if shares <= 0:
            raise TranslationError("Polymarket stake is below the size increment")
        received = price * shares
        if received > requested_stake:
            raise TranslationError("translated size would increase the approved stake")
        _require_stake_coverage(requested_stake, received)
        if shares < minimum_shares:
            raise TranslationError("Polymarket stake is below the minimum order size")
        return PolymarketNativeOrder(
            token_id=token,
            side=PolymarketOrderSide.SELL,
            price=price,
            amount=received,
            shares=shares,
            stake=received,
            decimal_odds=odds,
            tick_size=tick_size,
            order_type=POLYMARKET_ORDER_TYPE,
        )
    raise TranslationError("Polymarket execution only maps back to buy and lay to sell")


def polymarket_payoff(order: PolymarketNativeOrder, *, outcome_wins: bool) -> Decimal:
    """Payout minus stake for the token the order trades.

    A BUY wins ``shares - stake`` when the token pays 1. A SELL wins ``stake``
    when the token pays 0 and loses ``(1 - price) * shares`` when it pays 1.
    """

    if order.side is PolymarketOrderSide.BUY:
        return order.shares - order.stake if outcome_wins else -order.stake
    liability = (Decimal(1) - order.price) * order.shares
    return -liability if outcome_wins else order.stake
