"""Buy-in partial-fill recovery. Entry/top-up only. Does not change unwind policy."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sports_hedge.fees.cost import VenueCostSnapshot
from sports_hedge.fees.effective import CostRuleError, apply_venue_costs
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.paper.fills import PaperOpportunityLeg
from sports_hedge.paper.settlement import PaperSettlementError, compute_paper_settlement
from sports_hedge.paper.trades import PaperTrade, PaperTradeLeg

ZERO = Decimal("0")


def _fx_rate(leg: PaperTradeLeg | PaperOpportunityLeg, fx: dict[str, Decimal]) -> Decimal:
    currency = str(getattr(leg, "currency", "GBP") or "GBP").upper()
    if currency == "GBP":
        return Decimal("1")
    return fx.get(currency, Decimal("1"))


def fx_map_from_trade(trade: PaperTrade) -> dict[str, Decimal]:
    fx = {item.currency.upper(): item.gbp_per_unit for item in trade.fx_snapshots}
    fx.setdefault("GBP", Decimal("1"))
    return fx


def outcome_key(leg: Any) -> str:
    return str(getattr(leg, "canonical_state", None) or getattr(leg, "outcome", "") or "")


def worst_case_settlement_pnl_gbp(trade: PaperTrade) -> Decimal:
    """Worst-case PAPER settlement P&L in GBP after canonical venue costs and FX.

    Uses ``compute_paper_settlement`` so “recovered” means the post-cost payoff
    floor is cleared. Used only to size risk-reducing recovery. Not an unwind metric.
    """

    outcomes = {leg.outcome for leg in trade.legs if leg.outcome}
    filled = [leg for leg in trade.legs if leg.filled_stake > 0]
    if not outcomes or not filled:
        return ZERO
    computed: list[Decimal] = []
    for winner in outcomes:
        try:
            result = compute_paper_settlement(trade, winning_outcome=winner)
            computed.append(result.realised_pnl_gbp)
        except (PaperSettlementError, CostRuleError):
            fx = fx_map_from_trade(trade)
            filled_gbp = sum((leg.filled_stake * _fx_rate(leg, fx) for leg in filled), ZERO)
            computed.append(-filled_gbp)
    return min(computed) if computed else ZERO


def residual_exposure_gbp(trade: PaperTrade) -> Decimal:
    pnl = worst_case_settlement_pnl_gbp(trade)
    if pnl >= 0:
        return ZERO
    return -pnl


def remaining_book_levels(levels: list[BookLevel], consume: Decimal) -> list[BookLevel]:
    """Return still-available taker depth after ``consume`` has been filled."""

    leftover: list[BookLevel] = []
    remaining = consume if consume > 0 else ZERO
    for level in sorted(levels, key=lambda item: item.decimal_odds, reverse=True):
        if remaining <= 0:
            leftover.append(level)
            continue
        take = min(level.available_stake, remaining)
        remaining -= take
        left = level.available_stake - take
        if left > 0:
            leftover.append(level.model_copy(update={"available_stake": left}))
    return leftover


def subtract_consumed_depth(
    plan_legs: list[PaperOpportunityLeg],
    fills: list[Any] | None,
) -> list[PaperOpportunityLeg]:
    """Decrement already-consumed snapshot depth. Never reuse unadjusted pre-fill levels."""

    consumed: dict[tuple[Any, ...], Decimal] = {}
    for fill in fills or []:
        stake = getattr(fill, "filled_stake", ZERO) or ZERO
        if stake <= 0:
            continue
        key = (
            getattr(fill, "venue", None),
            getattr(fill, "outcome", None),
            getattr(fill, "source_market_id", None),
            getattr(fill, "source_runner_id", None),
        )
        consumed[key] = consumed.get(key, ZERO) + stake
    adjusted: list[PaperOpportunityLeg] = []
    for leg in plan_legs:
        key = (leg.venue, leg.outcome, leg.source_market_id, leg.source_runner_id)
        take = consumed.get(key, ZERO)
        if take <= 0:
            adjusted.append(leg)
            continue
        adjusted.append(leg.model_copy(update={"levels": remaining_book_levels(leg.levels, take)}))
    return adjusted


def recovery_legs_from_plan(
    trade: PaperTrade,
    plan_legs: list[PaperOpportunityLeg],
    *,
    remaining_gbp: Decimal,
) -> tuple[list[PaperOpportunityLeg], str]:
    """Size missing/underfilled buy legs to minimise post-cost worst-case settlement loss.

    Below-Min-Net is allowed. Depth and remaining treasury room bound the size.
    """

    if remaining_gbp <= 0 or not plan_legs:
        return [], "recovery_treasury_bounded"
    fx = fx_map_from_trade(trade)
    reason = "recovery_committed"
    needed: list[PaperOpportunityLeg] = []
    room = remaining_gbp
    for plan_leg in plan_legs:
        if plan_leg.requested_stake <= 0 or (plan_leg.displayed_odds or ZERO) <= 1:
            continue
        pnl_if_wins = _pnl_if_outcome_wins(trade, outcome_key(plan_leg))
        if pnl_if_wins >= 0:
            continue
        need_gbp = -pnl_if_wins
        depth = sum((level.available_stake for level in plan_leg.levels), ZERO)
        if depth <= 0:
            reason = "recovery_depth_bounded"
            continue
        fx_rate = _fx_rate(plan_leg, fx)
        max_native = min(depth, room / fx_rate if fx_rate > 0 else ZERO)
        if max_native <= 0:
            reason = "recovery_treasury_bounded"
            continue
        stake_native = _stake_to_cover_post_cost_loss(trade, plan_leg, need_gbp, max_native)
        if stake_native <= 0:
            reason = "recovery_depth_bounded"
            continue
        take_gbp = stake_native * fx_rate
        if take_gbp + Decimal("0.0001") < need_gbp:
            profit = _net_profit_gbp(trade, plan_leg, stake_native)
            if profit is None or profit + Decimal("0.0001") < need_gbp:
                reason = (
                    "recovery_depth_bounded"
                    if stake_native + Decimal("0.0001") >= depth
                    else "recovery_treasury_bounded"
                )
        needed.append(plan_leg.model_copy(update={"requested_stake": stake_native}))
        room -= take_gbp
        if room <= 0:
            break
    return needed, reason


def _pnl_if_outcome_wins(trade: PaperTrade, winner: str) -> Decimal:
    try:
        return compute_paper_settlement(trade, winning_outcome=winner).realised_pnl_gbp
    except (PaperSettlementError, CostRuleError):
        fx = fx_map_from_trade(trade)
        pnl = ZERO
        for leg in trade.legs:
            if leg.filled_stake <= 0 or (leg.filled_odds or ZERO) <= 1:
                continue
            stake_gbp = leg.filled_stake * _fx_rate(leg, fx)
            odds = leg.filled_odds or ZERO
            if outcome_key(leg) == winner:
                pnl += stake_gbp * (odds - 1)
            else:
                pnl -= stake_gbp
        return pnl


def _cost_for_leg(costs: list[VenueCostSnapshot], leg: PaperOpportunityLeg) -> VenueCostSnapshot:
    matches = [item for item in costs if item.venue is leg.venue]
    if not matches:
        raise PaperSettlementError(f"missing_venue_cost:{leg.venue.value}")
    if len(matches) == 1:
        return matches[0]
    by_market = [item for item in matches if item.source_market_id == leg.source_market_id]
    if len(by_market) == 1:
        return by_market[0]
    raise PaperSettlementError(f"ambiguous_venue_cost:{leg.venue.value}")


def _net_profit_gbp(
    trade: PaperTrade,
    plan_leg: PaperOpportunityLeg,
    stake_native: Decimal,
) -> Decimal | None:
    if stake_native <= 0:
        return ZERO
    try:
        cost = _cost_for_leg(list(trade.venue_costs), plan_leg)
        economics = apply_venue_costs(
            cost,
            gross_decimal_odds=plan_leg.displayed_odds,
            stake=stake_native,
            require_gbp=False,
        )
    except (PaperSettlementError, CostRuleError):
        return None
    fx = fx_map_from_trade(trade)
    return (economics.net_payoff - stake_native) * _fx_rate(plan_leg, fx)


def _stake_to_cover_post_cost_loss(
    trade: PaperTrade,
    plan_leg: PaperOpportunityLeg,
    need_gbp: Decimal,
    max_native: Decimal,
) -> Decimal:
    """Smallest executable stake whose post-cost net profit covers ``need_gbp``."""

    if need_gbp <= 0 or max_native <= 0:
        return ZERO
    hi_profit = _net_profit_gbp(trade, plan_leg, max_native)
    if hi_profit is None:
        return ZERO
    if hi_profit <= 0:
        return ZERO
    lo = ZERO
    hi = max_native
    best = max_native
    for _ in range(32):
        mid = (lo + hi) / 2
        if mid <= 0:
            break
        profit = _net_profit_gbp(trade, plan_leg, mid)
        if profit is None:
            return ZERO
        if profit >= need_gbp:
            best = mid
            hi = mid
        else:
            lo = mid
    return best
