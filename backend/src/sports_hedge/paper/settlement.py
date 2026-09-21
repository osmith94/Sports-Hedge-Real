"""Paper settlement payoffs from recorded fills, venue costs and FX.

Does not invent a football result. Winning outcome must be supplied by a labelled
source (auto-settlement evidence or operator-selected canonical market result).

Phase-1 openings are normalized at fill time:
Matchbook/Smarkets → BACK, Kalshi/Polymarket → BUY, with ``canonical_state``
equal to the paying runner. Settlement therefore matches ``leg.outcome`` to the
canonical winner and does not invert complement/negative states. LAY/SELL
openings are rejected rather than booked as simple winners. A valid family
result that no filled leg covers (e.g. 1X2 DRAW on a home/away-only trade)
settles every filled leg as a loss from stored entry odds.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, Field

from sports_hedge.fees.cost import MarketAction, VenueCostSnapshot
from sports_hedge.fees.effective import apply_venue_costs
from sports_hedge.paper.canonical_results import is_valid_canonical_settlement_outcome
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.paper.trades import PaperLegFillKind, PaperTrade, PaperTradeLeg


class PaperSettlementError(ValueError):
    """Fail-closed paper settlement."""


class LegSettlement(BaseModel):
    outcome: str
    venue: str
    currency: str
    filled_stake: Decimal
    filled_odds: Decimal | None = None
    fill_id: str | None = None
    won: bool
    gross_payoff: Decimal = Decimal("0")
    venue_fee: Decimal = Decimal("0")
    net_payoff: Decimal = Decimal("0")
    native_pnl: Decimal = Decimal("0")
    gbp_pnl: Decimal = Decimal("0")
    fx_rate_gbp_per_unit: Decimal
    capital_source: str


class PaperSettlementComputation(BaseModel):
    winning_outcome: str
    realised_pnl_gbp: Decimal
    legs: list[LegSettlement] = Field(default_factory=list)


def compute_paper_settlement(
    trade: PaperTrade,
    *,
    winning_outcome: str,
) -> PaperSettlementComputation:
    """Settle from a labelled canonical outcome over the trade's filled legs.

    UNFILLED PARTIAL outcomes are valid winners; P&L uses filled legs only.
    Current quotes are never used. Stored filled/entry odds, stakes and fees
    are the sole economics.

    ``opening_action`` / ``canonical_state``: current paper fills persist BACK
    (exchanges) or BUY (Kalshi/Polymarket) with ``canonical_state == outcome``.
    Winner matching is therefore the paying canonical state, not an exchange
    complement. LAY/SELL openings fail closed so a future lay is not treated
    as a simple back winner.
    """
    if not is_valid_canonical_settlement_outcome(trade, winning_outcome):
        raise PaperSettlementError("settlement_outcome_not_on_trade")
    fx = {item.currency: item for item in trade.fx_snapshots}
    results: list[LegSettlement] = []
    realised = Decimal("0")
    for leg in trade.legs:
        if leg.filled_stake <= 0 or leg.fill_kind is PaperLegFillKind.UNFILLED:
            continue
        if leg.opening_action in {MarketAction.LAY, MarketAction.SELL}:
            raise PaperSettlementError("opening_lay_sell_not_supported")
        rate = _fx_rate(leg.currency, fx)
        won = (leg.canonical_state or leg.outcome) == winning_outcome
        filled_odds = leg.filled_odds or leg.displayed_odds
        if won:
            if filled_odds is None:
                raise PaperSettlementError("missing_filled_odds")
            cost = _cost_for_leg(trade.venue_costs, leg)
            economics = apply_venue_costs(
                cost,
                gross_decimal_odds=filled_odds,
                stake=leg.filled_stake,
                require_gbp=False,
            )
            native_pnl = economics.net_payoff - leg.filled_stake
            settlement = LegSettlement(
                outcome=leg.outcome,
                venue=leg.venue.value,
                currency=leg.currency,
                filled_stake=leg.filled_stake,
                filled_odds=filled_odds,
                fill_id=leg.fill_id,
                won=True,
                gross_payoff=economics.gross_payoff,
                venue_fee=economics.venue_fee,
                net_payoff=economics.net_payoff,
                native_pnl=native_pnl,
                gbp_pnl=native_pnl * rate,
                fx_rate_gbp_per_unit=rate,
                capital_source=leg.capital_source.value,
            )
        else:
            native_pnl = -leg.filled_stake
            settlement = LegSettlement(
                outcome=leg.outcome,
                venue=leg.venue.value,
                currency=leg.currency,
                filled_stake=leg.filled_stake,
                filled_odds=filled_odds,
                fill_id=leg.fill_id,
                won=False,
                gross_payoff=Decimal("0"),
                venue_fee=Decimal("0"),
                net_payoff=Decimal("0"),
                native_pnl=native_pnl,
                gbp_pnl=native_pnl * rate,
                fx_rate_gbp_per_unit=rate,
                capital_source=leg.capital_source.value,
            )
        realised += settlement.gbp_pnl
        results.append(settlement)
    if not results:
        raise PaperSettlementError("no_filled_legs_to_settle")
    return PaperSettlementComputation(winning_outcome=winning_outcome, realised_pnl_gbp=realised, legs=results)


def _fx_rate(currency: str, fx: dict[str, FxRateSnapshot]) -> Decimal:
    if currency == "GBP":
        return Decimal("1")
    snap = fx.get(currency)
    if snap is None:
        raise PaperSettlementError(f"missing_fx_rate:{currency}")
    return snap.gbp_per_unit


def _cost_for_leg(costs: list[VenueCostSnapshot], leg: PaperTradeLeg) -> VenueCostSnapshot:
    matches = [item for item in costs if item.venue is leg.venue]
    if not matches:
        raise PaperSettlementError(f"missing_venue_cost:{leg.venue.value}")
    if len(matches) == 1:
        return matches[0]
    by_market = [item for item in matches if item.source_market_id == leg.source_market_id]
    if len(by_market) == 1:
        return by_market[0]
    raise PaperSettlementError(f"ambiguous_venue_cost:{leg.venue.value}")
