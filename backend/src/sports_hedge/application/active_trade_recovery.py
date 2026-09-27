"""Buy-in partial-fill recovery. Entry/top-up only. Does not change unwind policy."""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any

from sports_hedge.fees.cost import VenueCostSnapshot
from sports_hedge.fees.effective import CostRuleError, apply_venue_costs
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.paper.canonical_results import canonical_result_space
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
    outcomes.update(canonical_result_space(trade).values)
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


# PAPER fills do not remove venue orders. A later HTTP read of the same
# displayed size is not new liquidity. Matchbook last-updated, Kalshi
# orderbook payloads, and Polymarket book bodies are not used as a
# replenishment epoch: none of those clients expose a book version,
# sequence, or revision that proves a price level was replaced. Local
# retrieval time is not proof either. Remaining depth is
# max(0, observed_at_this_odds - previously_consumed_at_this_odds).
# A larger displayed size unlocks only the increment. A smaller one
# clamps at zero. A different price is a different level.
ODDS_QUANTUM = Decimal("0.0001")


def odds_token(odds: Decimal | str) -> str:
    return str(Decimal(str(odds)).quantize(ODDS_QUANTUM))


def native_leg_identity(leg: Any) -> tuple[str, str, str, str]:
    venue = getattr(leg, "venue", "")
    venue_name = venue.value if hasattr(venue, "value") else str(venue)
    market = getattr(leg, "source_market_id", None) or getattr(leg, "native_market_id", "")
    runner = getattr(leg, "source_runner_id", None)
    if runner is None:
        runner = getattr(leg, "native_runner_id", "") or ""
    outcome = getattr(leg, "outcome", "") or ""
    return (venue_name, str(market or ""), str(runner or ""), str(outcome))


def residual_native(observed: Decimal, consumed: Decimal) -> Decimal:
    """Executable stake still available at one exact price. Never negative."""

    left = observed - consumed
    if left <= 0:
        return ZERO
    return left


def _snapshot_levels(
    payload: dict[str, Any],
) -> dict[tuple[str, str, str, str], list[tuple[Decimal, Decimal]]]:
    levels: dict[tuple[str, str, str, str], list[tuple[Decimal, Decimal]]] = {}
    for leg in payload.get("legs") or []:
        if not isinstance(leg, dict):
            continue
        identity = (
            str(leg.get("venue") or ""),
            str(leg.get("native_market_id") or ""),
            str(leg.get("native_runner_id") or ""),
            str(leg.get("outcome") or ""),
        )
        parsed: list[tuple[Decimal, Decimal]] = []
        for level in leg.get("levels") or []:
            if not isinstance(level, dict):
                continue
            parsed.append((Decimal(str(level["odds"])), Decimal(str(level["depth"]))))
        if not parsed and leg.get("displayed_odds") and leg.get("available_depth"):
            parsed.append(
                (Decimal(str(leg["displayed_odds"])), Decimal(str(leg["available_depth"])))
            )
        levels[identity] = parsed
    return levels


def _attribute_fill(
    levels: list[tuple[Decimal, Decimal]],
    filled_stake: Decimal,
) -> dict[str, Decimal]:
    """Replay one fill best-odds-first onto the book that authorised it."""

    remaining = filled_stake
    attributed: dict[str, Decimal] = {}
    ordered = sorted(levels, key=lambda item: item[0], reverse=True)
    for odds, available in ordered:
        if remaining <= 0:
            break
        take = min(available, remaining)
        if take <= 0:
            continue
        token = odds_token(odds)
        attributed[token] = attributed.get(token, ZERO) + take
        remaining -= take
    if remaining > 0 and ordered:
        token = odds_token(ordered[0][0])
        attributed[token] = attributed.get(token, ZERO) + remaining
    return attributed


def consumed_native_by_level(
    trade: PaperTrade | None,
) -> dict[tuple[str, str, str, str, str], Decimal]:
    """Native stake already PAPER-filled, keyed by venue, market, runner, outcome, odds.

    Durable source is the persisted trade: each snapshot-backed tranche is
    replayed onto that snapshot's levels. Fills without a snapshot fall back
    to their filled odds. Process restart does not reset this.
    """

    if trade is None:
        return {}
    from sports_hedge.paper.trades import PaperTradeAuditEventType

    snapshots: dict[str, dict[tuple[str, str, str, str], list[tuple[Decimal, Decimal]]]] = {}
    for event in trade.audit:
        if event.event_type is not PaperTradeAuditEventType.EXECUTION_SNAPSHOT:
            continue
        try:
            payload = json.loads(event.detail or "")
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue
        snapshot_id = payload.get("snapshot_id")
        if snapshot_id:
            snapshots[str(snapshot_id)] = _snapshot_levels(payload)
    by_tranche: dict[str, list[Any]] = {}
    for leg in trade.legs:
        stake = leg.filled_stake or ZERO
        if stake <= 0:
            continue
        by_tranche.setdefault(leg.tranche_id, []).append(leg)
    consumed: dict[tuple[str, str, str, str, str], Decimal] = {}
    seen: set[str] = set()
    for tranche in trade.tranches:
        seen.add(tranche.tranche_id)
        levels_map = snapshots.get(tranche.execution_snapshot_id or "")
        for leg in by_tranche.get(tranche.tranche_id, []):
            _add_consumed(consumed, leg, levels_map)
    for tranche_id, legs in by_tranche.items():
        if tranche_id in seen:
            continue
        for leg in legs:
            _add_consumed(consumed, leg, None)
    return consumed


def _add_consumed(
    consumed: dict[tuple[str, str, str, str, str], Decimal],
    leg: Any,
    levels_map: dict[tuple[str, str, str, str], list[tuple[Decimal, Decimal]]] | None,
) -> None:
    identity = native_leg_identity(leg)
    stake = Decimal(leg.filled_stake or ZERO)
    if stake <= 0:
        return
    levels = None if levels_map is None else levels_map.get(identity)
    if levels:
        attributed = _attribute_fill(levels, stake)
    elif getattr(leg, "filled_odds", None):
        attributed = {odds_token(leg.filled_odds): stake}
    else:
        return
    for token, amount in attributed.items():
        key = (*identity, token)
        consumed[key] = consumed.get(key, ZERO) + amount


def liquidity_rows(
    identity: tuple[str, str, str, str],
    levels: list[tuple[Decimal, Decimal]],
    consumed: dict[tuple[str, str, str, str, str], Decimal],
) -> list[dict[str, str]]:
    buckets: dict[str, Decimal] = {}
    for odds, available in levels:
        token = odds_token(odds)
        buckets[token] = buckets.get(token, ZERO) + available
    rows: list[dict[str, str]] = []
    for token in sorted(buckets, key=Decimal, reverse=True):
        observed = buckets[token]
        used = consumed.get((*identity, token), ZERO)
        incremental = residual_native(observed, used)
        rows.append(
            {
                "venue": identity[0],
                "source_market_id": identity[1],
                "source_runner_id": identity[2],
                "outcome": identity[3],
                "odds": token,
                "observed": str(observed),
                "previously_consumed": str(used),
                "incremental": str(incremental),
            }
        )
    return rows


def liquidity_evidence_for_levels(
    legs: list[tuple[tuple[str, str, str, str], list[tuple[Decimal, Decimal]]]],
    trade: PaperTrade | None,
) -> list[dict[str, str]]:
    consumed = consumed_native_by_level(trade)
    rows: list[dict[str, str]] = []
    for identity, levels in legs:
        rows.extend(liquidity_rows(identity, levels, consumed))
    return rows


def residual_book_levels(
    levels: list[BookLevel],
    identity: tuple[str, str, str, str],
    consumed: dict[tuple[str, str, str, str, str], Decimal],
) -> list[BookLevel]:
    """Still-displayed stake at each price after PAPER consumption at that price."""

    observed = [(level.decimal_odds, level.available_stake) for level in levels]
    residual: list[BookLevel] = []
    buckets: dict[str, Decimal] = {}
    for odds, available in observed:
        token = odds_token(odds)
        buckets[token] = buckets.get(token, ZERO) + available
    for token in sorted(buckets, key=Decimal, reverse=True):
        left = residual_native(buckets[token], consumed.get((*identity, token), ZERO))
        if left > 0:
            residual.append(BookLevel(decimal_odds=Decimal(token), available_stake=left))
    return residual
