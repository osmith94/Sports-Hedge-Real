"""Buy-in partial-fill recovery. Entry/top-up only. Does not change unwind policy."""

from __future__ import annotations

from decimal import Decimal
from typing import Any

from sports_hedge.paper.fills import PaperOpportunityLeg
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
    """Worst-case native-complete-set P&L in GBP after fees are ignored.

    Used only to size risk-reducing recovery. Not an unwind metric.
    """

    fx = fx_map_from_trade(trade)
    filled = [
        leg
        for leg in trade.legs
        if leg.filled_stake > 0 and (leg.filled_odds or ZERO) > 1
    ]
    if not filled:
        return ZERO
    outcomes = {outcome_key(leg) for leg in trade.legs if outcome_key(leg)}
    if not outcomes:
        outcomes = {outcome_key(leg) for leg in filled}
    worst: Decimal | None = None
    for winner in outcomes:
        pnl = ZERO
        for leg in filled:
            stake_gbp = leg.filled_stake * _fx_rate(leg, fx)
            odds = leg.filled_odds or ZERO
            if outcome_key(leg) == winner:
                pnl += stake_gbp * (odds - 1)
            else:
                pnl -= stake_gbp
        worst = pnl if worst is None else min(worst, pnl)
    return worst if worst is not None else ZERO


def residual_exposure_gbp(trade: PaperTrade) -> Decimal:
    pnl = worst_case_settlement_pnl_gbp(trade)
    if pnl >= 0:
        return ZERO
    return -pnl


def recovery_legs_from_plan(
    trade: PaperTrade,
    plan_legs: list[PaperOpportunityLeg],
    *,
    remaining_gbp: Decimal,
) -> tuple[list[PaperOpportunityLeg], str]:
    """Size missing/underfilled buy legs to minimise worst-case settlement loss.

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
        pnl_if_wins = _pnl_if_outcome_wins(trade, outcome_key(plan_leg), fx)
        if pnl_if_wins >= 0:
            continue
        odds = plan_leg.displayed_odds
        need_gbp = (-pnl_if_wins) / (odds - 1)
        depth = sum((level.available_stake for level in plan_leg.levels), ZERO)
        depth_gbp = depth * _fx_rate(plan_leg, fx)
        take_gbp = min(need_gbp, room, depth_gbp if depth_gbp > 0 else need_gbp)
        if take_gbp <= 0:
            reason = "recovery_depth_bounded" if depth_gbp <= 0 else "recovery_treasury_bounded"
            continue
        if take_gbp < need_gbp:
            reason = "recovery_depth_bounded" if take_gbp >= depth_gbp else "recovery_treasury_bounded"
        stake_native = take_gbp / _fx_rate(plan_leg, fx)
        if stake_native <= 0:
            continue
        needed.append(plan_leg.model_copy(update={"requested_stake": stake_native}))
        room -= take_gbp
        if room <= 0:
            break
    return needed, reason


def _pnl_if_outcome_wins(trade: PaperTrade, winner: str, fx: dict[str, Decimal]) -> Decimal:
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
