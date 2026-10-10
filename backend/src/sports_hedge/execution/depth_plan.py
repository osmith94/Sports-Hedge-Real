"""One Price-2 depth plan: expected blend vs worst native limit.

The book-walker and prefix-depth scanner remain the economics authorities.
This module does not solve a second arb. It records, for an approved native
stake, the levels actually admitted, the stake-weighted average, and the
worst BACK/BUY price that a single bounded order must be allowed to reach.

STREAM Phase-2 (#32 / #37) and live iterative accumulation (#36) must reuse
this translator. Do not add a paper-only solver or loosen the limit to a
market order.

A blended average is a snapshot prediction, not a fill guarantee. Native
submit uses the worst admitted BACK/BUY limit. Conservative qualification
treats the whole size as filling at that limit after fees and FX.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from sports_hedge.arbitrage.models import ExecutableQuote
from sports_hedge.arbitrage.payoff_solver import back_payoff_per_unit
from sports_hedge.arbitrage.solver import CompleteSetArbitrageSolver
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import VenueCostSnapshot
from sports_hedge.fees.effective import CostRuleError, apply_venue_costs
from sports_hedge.liquidity.book import BookLevel, OrderBookWalker
from sports_hedge.paper.fills import PaperOpportunityLeg, apply_odds_haircut

_WALKER = OrderBookWalker()


@dataclass(frozen=True)
class NativeLevelFill:
    decimal_odds: Decimal
    stake: Decimal


@dataclass(frozen=True)
class NativeDepthPlan:
    """Approved quantity expressed as both blend and worst executable limit."""

    requested_stake: Decimal
    filled_stake: Decimal
    remaining_stake: Decimal
    expected_average_odds: Decimal | None
    worst_limit_odds: Decimal | None
    levels: tuple[NativeLevelFill, ...]
    fully_filled: bool

    def as_admitted_pairs(self) -> tuple[tuple[str, str], ...]:
        return tuple((format(item.decimal_odds, "f"), format(item.stake, "f")) for item in self.levels)


def sweep_back_levels(levels: Sequence[BookLevel], requested_stake: Decimal) -> NativeDepthPlan:
    """Walk best-to-worst BACK/BUY depth for one native stake.

    ``worst_limit_odds`` is the lowest decimal odds reached (the most
    aggressive BACK limit required). ``expected_average_odds`` is the
    stake-weighted blend on this snapshot only.
    """

    if requested_stake <= 0:
        raise ValueError("requested_stake must be positive")
    fill = _WALKER.fill(list(levels), requested_stake)
    admitted: list[NativeLevelFill] = []
    remaining = requested_stake
    for level in sorted(levels, key=lambda item: item.decimal_odds, reverse=True):
        if remaining <= 0:
            break
        take = min(level.available_stake, remaining)
        if take <= 0:
            continue
        admitted.append(NativeLevelFill(decimal_odds=level.decimal_odds, stake=take))
        remaining -= take
    return NativeDepthPlan(
        requested_stake=requested_stake,
        filled_stake=fill.filled_stake,
        remaining_stake=fill.remaining_stake,
        expected_average_odds=fill.weighted_average_odds,
        worst_limit_odds=fill.worst_odds,
        levels=tuple(admitted),
        fully_filled=fill.fully_filled,
    )


def approved_back_limit_odds(leg: Any) -> Decimal:
    """Worst admitted BACK odds for the approved quantity. Never the best touch.

    Matchbook BACK must be submitted at this decimal (or an equal ladder snap)
    so deeper levels are reachable. Polymarket BUY ``max_price`` is ``1 / this``.
    """

    stamped = getattr(leg, "approved_depth_worst_limit", None)
    if stamped is not None:
        value = stamped if isinstance(stamped, Decimal) else Decimal(str(stamped))
        if value > 1:
            return value
    levels = list(getattr(leg, "levels", None) or [])
    stake = getattr(leg, "requested_stake", None)
    if levels and stake is not None and stake > 0:
        plan = sweep_back_levels(levels, stake)
        if plan.remaining_stake > 0 or plan.worst_limit_odds is None:
            raise DepthPlanError("approved_stake_exceeds_executable_depth")
        return plan.worst_limit_odds
    displayed = getattr(leg, "displayed_odds", None)
    if displayed is None:
        raise DepthPlanError("missing_approved_back_limit")
    value = displayed if isinstance(displayed, Decimal) else Decimal(str(displayed))
    if value <= 1:
        raise DepthPlanError("missing_approved_back_limit")
    return value


def expected_average_odds(leg: Any) -> Decimal:
    stamped = getattr(leg, "expected_average_odds", None)
    if stamped is not None:
        value = stamped if isinstance(stamped, Decimal) else Decimal(str(stamped))
        if value > 1:
            return value
    levels = list(getattr(leg, "levels", None) or [])
    stake = getattr(leg, "requested_stake", None)
    if levels and stake is not None and stake > 0:
        plan = sweep_back_levels(levels, stake)
        if plan.expected_average_odds is not None:
            return plan.expected_average_odds
    displayed = getattr(leg, "displayed_odds", None)
    if displayed is None:
        raise DepthPlanError("missing_expected_average")
    return displayed if isinstance(displayed, Decimal) else Decimal(str(displayed))


class DepthPlanError(ValueError):
    """The approved quantity cannot be expressed as a bounded native sweep."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def stamp_fill_leg_depth_plan(leg: PaperOpportunityLeg) -> PaperOpportunityLeg:
    """Attach worst-limit / blend fields. Does not change requested stake."""

    if not leg.levels:
        return leg.model_copy(
            update={
                "expected_average_odds": leg.displayed_odds,
                "approved_depth_worst_limit": leg.displayed_odds,
                "admitted_levels": [],
            }
        )
    plan = sweep_back_levels(leg.levels, leg.requested_stake)
    return leg.model_copy(
        update={
            "expected_average_odds": plan.expected_average_odds or leg.displayed_odds,
            "approved_depth_worst_limit": plan.worst_limit_odds or leg.displayed_odds,
            "admitted_levels": [
                BookLevel(decimal_odds=item.decimal_odds, available_stake=item.stake)
                for item in plan.levels
            ],
        }
    )


def conservative_net_odds(
    cost: VenueCostSnapshot,
    *,
    gross_odds: Decimal,
    configured_slippage_bps: Decimal | None = None,
) -> Decimal:
    economics = apply_venue_costs(cost, gross_decimal_odds=gross_odds, require_gbp=False)
    net = economics.net_decimal_equivalent
    if configured_slippage_bps:
        net = apply_odds_haircut(net, configured_slippage_bps)
    return net


def exact_frozen_settlement(
    legs: Sequence[Any],
    *,
    costs: Mapping[VenueName, VenueCostSnapshot],
    gbp_per_unit: Mapping[str, Decimal],
    configured_slippage_bps: Decimal | None = None,
    state_model: Any | None = None,
) -> tuple[Decimal, Decimal, dict[str, Decimal]]:
    """Minimum GBP P&L of the *submitted* native stake pair after fees and FX.

    Does not re-solve stake ratios. Missing costs/FX fail closed. Generalized
    models reuse the existing win/refund state mapping; otherwise each listed
    outcome is a mutually exclusive complete-set state.
    """

    prepared: list[tuple[Any, Decimal, Decimal, Decimal]] = []
    for leg in legs:
        venue = getattr(leg, "venue", None)
        if venue is None:
            raise DepthPlanError("missing_leg_venue")
        cost = costs.get(venue)
        if cost is None:
            raise DepthPlanError("missing_venue_cost")
        currency = str(getattr(leg, "currency", "") or "").upper()
        rate = gbp_per_unit.get(currency)
        if currency == "GBP":
            rate = Decimal(1)
        if rate is None or rate <= 0:
            raise DepthPlanError("missing_fx")
        stake = getattr(leg, "requested_stake", None)
        if stake is None or stake <= 0:
            continue
        try:
            limit = approved_back_limit_odds(leg)
            net = conservative_net_odds(
                cost,
                gross_odds=limit,
                configured_slippage_bps=configured_slippage_bps,
            )
        except (CostRuleError, DepthPlanError) as exc:
            reason = getattr(exc, "reason", str(exc))
            raise DepthPlanError(reason) from exc
        if net <= 1:
            raise DepthPlanError("conservative_odds_not_executable")
        prepared.append((leg, stake, rate, net))
    if len(prepared) < 2:
        raise DepthPlanError("incomplete_outcome_set")
    outcomes = [str(getattr(leg, "outcome", "") or "") for leg, _stake, _rate, _net in prepared]
    states, mapping = _settlement_states(outcomes, state_model=state_model)
    state_pnl = {state: Decimal(0) for state in states}
    total_gbp = Decimal(0)
    refunds_by_leg: list[set[str]] = []
    for leg, stake, rate, net in prepared:
        outcome = str(getattr(leg, "outcome", "") or "")
        win_states, refund_states = mapping.get(outcome, ((outcome,), ()))
        refunds_by_leg.append(set(refund_states))
        payoffs = back_payoff_per_unit(
            states=states,
            win_states=win_states,
            refund_states=refund_states,
            net_decimal_odds=net,
        )
        gbp_stake = stake * rate
        total_gbp += gbp_stake
        for state in states:
            state_pnl[state] += gbp_stake * payoffs[state]
    if total_gbp <= 0:
        raise DepthPlanError("incomplete_outcome_set")
    refund_only = set(states)
    for refunds in refunds_by_leg:
        refund_only &= refunds
    decisive = [state_pnl[state] for state in states if state not in refund_only]
    minimum = min(decisive) if decisive else min(state_pnl.values())
    return total_gbp, minimum, state_pnl


def _settlement_states(
    outcomes: Sequence[str],
    *,
    state_model: Any | None,
) -> tuple[tuple[str, ...], dict[str, tuple[tuple[str, ...], tuple[str, ...]]]]:
    if state_model is not None:
        from sports_hedge.arbitrage.payoff_scan import _state_mapping

        return _state_mapping(state_model)
    unique = tuple(dict.fromkeys(item for item in outcomes if item))
    if len(unique) < 2:
        raise DepthPlanError("incomplete_outcome_set")
    mapping = {outcome: ((outcome,), ()) for outcome in unique}
    return unique, mapping


def conservative_complete_set_solution(
    legs: Sequence[Any],
    *,
    costs: Mapping[VenueName, VenueCostSnapshot],
    gbp_per_unit: Mapping[str, Decimal],
    configured_slippage_bps: Decimal | None = None,
    capital_limit: Decimal | None = None,
):
    """Whole-hedge ROI if every leg fills entirely at its submitted worst limit.

    Mixed currencies are converted with the same FX map Price-1 used. Missing
    FX or costs fail closed.
    """

    quotes: list[ExecutableQuote] = []
    for leg in legs:
        venue = getattr(leg, "venue", None)
        if venue is None:
            raise DepthPlanError("missing_leg_venue")
        cost = costs.get(venue)
        if cost is None:
            raise DepthPlanError("missing_venue_cost")
        currency = str(getattr(leg, "currency", "") or "").upper()
        rate = gbp_per_unit.get(currency)
        if rate is None or rate <= 0:
            raise DepthPlanError("missing_fx")
        stake = getattr(leg, "requested_stake", None)
        if stake is None or stake <= 0:
            continue
        try:
            limit = approved_back_limit_odds(leg)
            net = conservative_net_odds(
                cost,
                gross_odds=limit,
                configured_slippage_bps=configured_slippage_bps,
            )
        except (CostRuleError, DepthPlanError) as exc:
            reason = getattr(exc, "reason", str(exc))
            raise DepthPlanError(reason) from exc
        if net <= 1:
            raise DepthPlanError("conservative_odds_not_executable")
        quotes.append(
            ExecutableQuote(
                outcome=str(getattr(leg, "outcome", "")),
                venue=venue,
                source_market_id=str(getattr(leg, "source_market_id", "")),
                net_decimal_odds=net,
                max_stake=stake * rate,
            )
        )
    if len(quotes) < 2:
        raise DepthPlanError("incomplete_outcome_set")
    return CompleteSetArbitrageSolver().solve(quotes, capital_limit=capital_limit)


def qualifies_at_conservative_limits(
    legs: Sequence[Any],
    *,
    costs: Mapping[VenueName, VenueCostSnapshot],
    gbp_per_unit: Mapping[str, Decimal],
    minimum_net_edge: Decimal,
    configured_slippage_bps: Decimal | None = None,
    state_model: Any | None = None,
) -> bool:
    """True only when the exact frozen native quantities still clear min-net.

    A differently sized theoretical complete-set solution is not proof.
    """

    try:
        total_gbp, minimum_pnl, state_pnl = exact_frozen_settlement(
            legs,
            costs=costs,
            gbp_per_unit=gbp_per_unit,
            configured_slippage_bps=configured_slippage_bps,
            state_model=state_model,
        )
    except DepthPlanError:
        return False
    if total_gbp <= 0 or minimum_pnl <= 0:
        return False
    if any(pnl < 0 for pnl in state_pnl.values()):
        return False
    return (minimum_pnl / total_gbp) >= minimum_net_edge
