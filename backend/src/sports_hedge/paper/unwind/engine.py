"""Paper-only mark-to-market and hold-vs-unwind decision engine.

Prices every required reverse transaction from current marginal depth, fees,
FX, slippage and freshness. Does not post balances, allocate capital, or
place venue orders.

Capital becomes releasable only after a fully validated clean unwind whose
required reverse legs complete, or after actual venue/event settlement
recorded by paper operations/treasury. Predicted match completion is not a
hold-vs-unwind trigger and is not an assumed capital-release time.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sports_hedge.fees.cost import FeeBasis, MarketAction
from sports_hedge.fees.effective import CostRuleError, apply_closing_action_costs
from sports_hedge.liquidity.reverse import walk_lay_to_cover_payout, walk_prediction_sell
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.paper.unwind.mechanics import close_action_for, mechanics_for_venue
from sports_hedge.paper.unwind.models import (
    CapitalPressure,
    CapitalScarcityInput,
    CloseLegPlan,
    ClosePlan,
    DurationDecisionRole,
    EstimatedTimeToRelease,
    IncrementalCloseCapitalStatus,
    OpenPaperLeg,
    OpenPaperPosition,
    RemainingLockSource,
    ReverseQuote,
    UnwindDecision,
    UnwindEvaluationRequest,
    UnwindRecommendation,
    VenueCloseMechanics,
    venue_currency_key,
)
from sports_hedge.paper.unwind.policy import decide_recommendation
from sports_hedge.risk.execution import ExecutionRiskInputs, ExecutionRiskResult, ExecutionRiskScorer


class PaperUnwindEngine:
    """Evaluate an already-open paper position against reverse-side liquidity."""

    def __init__(self, risk: ExecutionRiskScorer | None = None) -> None:
        self.risk = risk or ExecutionRiskScorer()

    def evaluate(self, request: UnwindEvaluationRequest) -> UnwindDecision:
        evaluated_at = request.evaluated_at or datetime.now(UTC)
        position = request.position
        reasons: list[str] = []
        quotes = {
            (item.venue, item.source_market_id, item.source_runner_id, item.canonical_outcome): item
            for item in request.quotes
        }
        fx = {item.currency.upper(): item for item in request.fx}

        legs: list[CloseLegPlan] = []
        for open_leg in position.legs:
            quote = quotes.get(
                (
                    open_leg.venue,
                    open_leg.source_market_id,
                    open_leg.source_runner_id,
                    open_leg.canonical_outcome,
                )
            )
            legs.append(
                self._close_leg(
                    position,
                    open_leg,
                    quote,
                    fx=fx,
                    policy_max_age=request.policy.max_quote_age_ms,
                    evaluated_at=evaluated_at,
                    reasons=reasons,
                )
            )

        fully = bool(legs) and all(leg.executable for leg in legs)
        if reasons:
            fully = False

        if fully:
            commission_fail = _apply_deferred_profit_commission(legs, position, quotes)
            reasons.extend(commission_fail)
            if commission_fail:
                fully = False

        exit_pnl: Decimal | None = None
        if fully:
            exit_pnl = Decimal("0")
            for plan, open_leg in zip(legs, position.legs, strict=True):
                rate = _fx_rate(open_leg.native_currency, fx, reasons)
                if rate is None:
                    fully = False
                    exit_pnl = None
                    break
                plan.gbp_close_pnl = plan.native_close_pnl * rate
                exit_pnl += plan.gbp_close_pnl

        risk = self._risk(position, legs, request) if fully else None
        if fully and risk is not None:
            for plan in legs:
                if plan.fill_confidence is None:
                    plan.fill_confidence = Decimal(str(max(0, 100 - risk.score))) / Decimal("100")

        hold = position.hold_pnl_gbp
        give_up = (hold - exit_pnl) if fully and exit_pnl is not None else None
        recommendation, reason = decide_recommendation(
            fully_executable=fully,
            fail_reasons=list(dict.fromkeys(reasons)),
            hold_pnl_gbp=hold,
            exit_pnl_gbp=exit_pnl if exit_pnl is not None else hold,
            policy=request.policy,
            scarcity=request.scarcity,
            execution_risk_score=None if risk is None else risk.score,
        )
        if not fully:
            recommendation = UnwindRecommendation.UNWIND_NOT_SAFE

        releasable: dict[str, Decimal] = {}
        gross_liability: dict[str, Decimal] = {}
        incremental: dict[str, Decimal] = {}
        incremental_status = IncrementalCloseCapitalStatus.UNKNOWN_NOT_MODELLED
        if fully:
            incremental_status = IncrementalCloseCapitalStatus.KNOWN
            for open_leg, plan in zip(position.legs, legs, strict=True):
                key = venue_currency_key(open_leg.venue, open_leg.native_currency)
                releasable[key] = releasable.get(key, Decimal("0")) + open_leg.filled_size
                if plan.liability > 0:
                    gross_liability[key] = gross_liability.get(key, Decimal("0")) + plan.liability
                _annotate_incremental_close_capital(plan)
                if plan.incremental_close_capital_status is IncrementalCloseCapitalStatus.UNKNOWN_NOT_MODELLED:
                    incremental_status = IncrementalCloseCapitalStatus.UNKNOWN_NOT_MODELLED
                elif plan.incremental_close_capital_native is not None:
                    incremental[key] = incremental.get(key, Decimal("0")) + plan.incremental_close_capital_native
            if incremental_status is IncrementalCloseCapitalStatus.UNKNOWN_NOT_MODELLED:
                incremental = {}
        else:
            for open_leg, plan in zip(position.legs, legs, strict=True):
                key = venue_currency_key(open_leg.venue, open_leg.native_currency)
                if plan.liability > 0:
                    gross_liability[key] = gross_liability.get(key, Decimal("0")) + plan.liability
                _annotate_incremental_close_capital(plan)

        if recommendation is UnwindRecommendation.UNWIND_ELIGIBLE:
            needs_known_incremental = (
                request.policy.require_known_incremental_close_capital
                or request.scarcity.pressure is CapitalPressure.SCARCE
            )
            if (
                needs_known_incremental
                and incremental_status is IncrementalCloseCapitalStatus.UNKNOWN_NOT_MODELLED
            ):
                recommendation = UnwindRecommendation.UNWIND_NOT_SAFE
                reason = "incremental_close_capital_unknown"

        # Advisory passthrough only. Do not invent remaining lock from
        # kickoff, match clock, or expected_settlement_at - evaluated_at.
        estimate = _estimated_time_to_release(position)
        duration_role = _duration_decision_role(estimate, request.scarcity)
        turnover = _opportunity_cost_hint(request.scarcity, estimate)

        return UnwindDecision(
            trade_id=position.trade_id,
            recommendation=recommendation,
            decision_reason=reason,
            hold_pnl_gbp=hold,
            validated_exit_pnl_gbp=exit_pnl if fully else None,
            unwind_cost_gbp=give_up if fully else None,
            profit_give_up_gbp=give_up if fully else None,
            opportunity_cost_gbp=request.scarcity.opportunity_cost_gbp,
            remaining_lock_minutes=estimate.remaining_lock_minutes,
            estimated_time_to_release=estimate,
            duration_decision_role=duration_role,
            capital_turnover_hint=turnover,
            conditionally_releasable_by_venue_currency=releasable,
            gross_close_liability_native=gross_liability,
            incremental_close_capital_status=incremental_status,
            incremental_close_capital_native=incremental,
            close_plan=ClosePlan(
                evaluated_at=evaluated_at,
                fully_executable=fully,
                legs=legs,
                rejection_reasons=list(dict.fromkeys(reasons)),
            ),
            execution_risk=risk,
            capital_pressure=request.scarcity.pressure,
        )

    def _close_leg(
        self,
        position: OpenPaperPosition,
        open_leg: OpenPaperLeg,
        quote: ReverseQuote | None,
        *,
        fx: dict[str, FxRateSnapshot],
        policy_max_age: int,
        evaluated_at: datetime,
        reasons: list[str],
    ) -> CloseLegPlan:
        del fx
        mechanics = mechanics_for_venue(open_leg.venue)
        try:
            close_action = close_action_for(open_leg.opening_action, mechanics)
        except ValueError as exc:
            reasons.append(str(exc))
            return _rejected_leg(open_leg, mechanics, str(exc))

        if quote is None:
            reasons.append("missing_reverse_quote")
            return _rejected_leg(open_leg, mechanics, "missing_reverse_quote", close_action=close_action)
        if quote.native_currency != open_leg.native_currency:
            reasons.append("native_currency_mismatch")
            return _rejected_leg(open_leg, mechanics, "native_currency_mismatch", close_action=close_action)
        if quote.settlement_fingerprint_key != position.settlement_fingerprint_key:
            reasons.append("settlement_identity_mismatch")
            return _rejected_leg(open_leg, mechanics, "settlement_identity_mismatch", close_action=close_action)
        if quote.quote_age_ms is None:
            reasons.append("unknown_quote_age")
            return _rejected_leg(open_leg, mechanics, "unknown_quote_age", close_action=close_action)
        if quote.quote_age_ms > policy_max_age:
            reasons.append("stale_quote")
            return _rejected_leg(open_leg, mechanics, "stale_quote", close_action=close_action)
        if quote.closing_cost.action is not close_action:
            reasons.append("unsupported_exit_fee")
            return _rejected_leg(open_leg, mechanics, "unsupported_exit_fee", close_action=close_action)

        if mechanics is VenueCloseMechanics.EXCHANGE_BACK_LAY:
            required = open_leg.filled_size * open_leg.filled_price
            fill = walk_lay_to_cover_payout(quote.levels, required)
            proceeds = fill.matched_stake
            native_pnl = fill.matched_stake - open_leg.filled_size if fill.fully_filled else Decimal("0")
            top = min((level.decimal_odds for level in quote.levels), default=None)
        elif mechanics is VenueCloseMechanics.PREDICTION_BINARY_BUY_SELL:
            required = open_leg.filled_size * open_leg.filled_price
            fill = walk_prediction_sell(quote.levels, required)
            proceeds = fill.proceeds
            native_pnl = fill.proceeds - open_leg.filled_size if fill.fully_filled else Decimal("0")
            top = min((level.decimal_odds for level in quote.levels), default=None)
        else:
            reasons.append("unsupported_close_mechanics")
            return _rejected_leg(open_leg, mechanics, "unsupported_close_mechanics", close_action=close_action)

        slippage = None
        if fill.weighted_average_odds is not None and top is not None and top > 0:
            slippage = (fill.weighted_average_odds - top) / top

        if not fill.fully_filled:
            reasons.append("insufficient_reverse_depth")
            return CloseLegPlan(
                venue=open_leg.venue,
                canonical_outcome=open_leg.canonical_outcome,
                opening_action=open_leg.opening_action,
                close_action=close_action,
                mechanics=mechanics,
                required_close_quantity=required,
                filled_close_quantity=fill.filled_quantity,
                available_closing_capacity=fill.available_capacity,
                levels_consumed=fill.levels_consumed,
                weighted_closing_price=fill.weighted_average_odds,
                worst_closing_price=fill.worst_odds,
                slippage_vs_top=slippage,
                matched_stake=fill.matched_stake,
                liability=fill.liability,
                proceeds=fill.proceeds,
                quote_age_ms=quote.quote_age_ms,
                quote_age_basis=quote.quote_age_basis,
                fill_confidence=quote.fill_confidence,
                executable=False,
                rejection_reason="insufficient_reverse_depth",
                native_currency=open_leg.native_currency,
            )

        try:
            costs = apply_closing_action_costs(
                quote.closing_cost,
                action=close_action,
                gross_proceeds=proceeds if close_action is MarketAction.SELL else fill.matched_stake,
                matched_stake=fill.matched_stake if close_action is MarketAction.LAY else fill.shares_sold,
                as_of=evaluated_at,
            )
        except CostRuleError as exc:
            reasons.append(exc.reason)
            return _rejected_leg(open_leg, mechanics, exc.reason, close_action=close_action)

        native_pnl = native_pnl if costs.deferred_profit_commission else native_pnl - costs.venue_fee
        return CloseLegPlan(
            venue=open_leg.venue,
            canonical_outcome=open_leg.canonical_outcome,
            opening_action=open_leg.opening_action,
            close_action=close_action,
            mechanics=mechanics,
            required_close_quantity=required,
            filled_close_quantity=fill.filled_quantity,
            available_closing_capacity=fill.available_capacity,
            levels_consumed=fill.levels_consumed,
            weighted_closing_price=fill.weighted_average_odds,
            worst_closing_price=fill.worst_odds,
            slippage_vs_top=slippage,
            matched_stake=fill.matched_stake,
            liability=fill.liability,
            proceeds=fill.proceeds,
            closing_fee=Decimal("0") if costs.deferred_profit_commission else costs.venue_fee,
            native_close_pnl=native_pnl,
            gbp_close_pnl=Decimal("0"),
            native_currency=open_leg.native_currency,
            quote_age_ms=quote.quote_age_ms,
            quote_age_basis=quote.quote_age_basis,
            fill_confidence=quote.fill_confidence,
            executable=True,
            fee_snapshot_id=costs.fee_snapshot_id,
            deferred_profit_commission=costs.deferred_profit_commission,
        )

    def _risk(
        self,
        position: OpenPaperPosition,
        legs: list[CloseLegPlan],
        request: UnwindEvaluationRequest,
    ) -> ExecutionRiskResult | None:
        ages = [leg.quote_age_ms for leg in legs if leg.quote_age_ms is not None]
        if not ages:
            return None
        # Remaining lock (authoritative or modelled) is not kickoff proximity.
        # Leave minutes_to_kickoff unset unless an actual pre-kickoff clock is
        # known. 8D does not have that input and must not invent near_kickoff.
        ratios = []
        for leg in legs:
            if leg.available_closing_capacity <= 0:
                return None
            ratios.append(float(leg.required_close_quantity / leg.available_closing_capacity))
        hedge = 1.0
        if ratios:
            hedge = min((1 / r if r else 1.0) for r in ratios)
        return self.risk.score(
            ExecutionRiskInputs(
                spread_bps=0.0,
                size_to_depth_ratio=max(ratios) if ratios else 0.0,
                quote_age_ms=max(ages),
                recent_volatility_bps=request.recent_volatility_bps,
                leg_count=max(len(legs), 2),
                minutes_to_kickoff=None,
                assumed_latency_ms=request.assumed_latency_ms,
                hedge_liquidity_ratio=hedge,
            )
        )


def _apply_deferred_profit_commission(
    legs: list[CloseLegPlan],
    position: OpenPaperPosition,
    quotes: dict[tuple, ReverseQuote],
) -> list[str]:
    grouped: dict[tuple, list[int]] = {}
    for index, open_leg in enumerate(position.legs):
        quote = quotes.get(
            (open_leg.venue, open_leg.source_market_id, open_leg.source_runner_id, open_leg.canonical_outcome)
        )
        plan = legs[index]
        if quote is None or not plan.deferred_profit_commission:
            continue
        if quote.closing_cost.fee_basis is not FeeBasis.PROFIT_COMMISSION or quote.closing_cost.rate is None:
            return ["unknown_exit_fee"]
        key = (open_leg.venue, open_leg.native_currency, quote.closing_cost.rate)
        grouped.setdefault(key, []).append(index)

    for key, indexes in grouped.items():
        _venue, _currency, rate = key
        gross = sum((legs[index].native_close_pnl for index in indexes), Decimal("0"))
        fee = max(gross, Decimal("0")) * rate
        if fee <= 0:
            continue
        remaining = fee
        for index in indexes:
            take = remaining
            legs[index].closing_fee += take
            legs[index].native_close_pnl -= take
            remaining = Decimal("0")
            break
    return []


def _estimated_time_to_release(position: OpenPaperPosition) -> EstimatedTimeToRelease:
    return EstimatedTimeToRelease(
        remaining_lock_minutes=position.remaining_lock_minutes,
        expected_settlement_at=position.expected_settlement_at,
        basis=position.remaining_lock_basis,
        confidence=position.remaining_lock_confidence,
        detail=position.remaining_lock_detail,
    )


def _duration_decision_role(
    estimate: EstimatedTimeToRelease,
    scarcity: CapitalScarcityInput,
) -> DurationDecisionRole:
    if scarcity.opportunity_cost_gbp is not None:
        return DurationDecisionRole.OPPORTUNITY_COST_COMPARED
    if (
        estimate.remaining_lock_minutes is not None
        or estimate.expected_settlement_at is not None
    ):
        return DurationDecisionRole.RANKING_CONTEXT_ONLY
    return DurationDecisionRole.DURATION_UNKNOWN


def _opportunity_cost_hint(
    scarcity: CapitalScarcityInput,
    estimate: EstimatedTimeToRelease,
) -> str | None:
    """Scarcity / competing opportunities, or advisory lock duration. Never a finish timer."""

    if scarcity.opportunity_cost_gbp is not None:
        return "opportunity_cost_from_current_scarcity"
    if estimate.basis is not RemainingLockSource.UNKNOWN and (
        estimate.remaining_lock_minutes is not None or estimate.expected_settlement_at is not None
    ):
        return "advisory_estimated_time_to_release"
    if scarcity.pressure is CapitalPressure.SCARCE:
        return "scarce_capital_competing_opportunities"
    return None


def _annotate_incremental_close_capital(plan: CloseLegPlan) -> None:
    """Gross LAY liability is not validated extra cash without venue netting.

    Selling already-owned prediction inventory does not consume incremental
    collateral. Exchange BACK→LAY netting is not modelled in 8D.
    """

    if not plan.executable:
        plan.incremental_close_capital_status = IncrementalCloseCapitalStatus.UNKNOWN_NOT_MODELLED
        plan.incremental_close_capital_native = None
        return
    if (
        plan.close_action is MarketAction.SELL
        and plan.mechanics is VenueCloseMechanics.PREDICTION_BINARY_BUY_SELL
    ):
        plan.incremental_close_capital_status = IncrementalCloseCapitalStatus.KNOWN
        plan.incremental_close_capital_native = Decimal("0")
        return
    plan.incremental_close_capital_status = IncrementalCloseCapitalStatus.UNKNOWN_NOT_MODELLED
    plan.incremental_close_capital_native = None


def _fx_rate(
    currency: str,
    fx: dict[str, FxRateSnapshot],
    reasons: list[str],
) -> Decimal | None:
    if currency == "GBP":
        return Decimal("1")
    snap = fx.get(currency)
    if snap is None:
        reasons.append(f"missing_fx_rate:{currency}")
        return None
    return snap.gbp_per_unit


def _rejected_leg(
    open_leg: OpenPaperLeg,
    mechanics: VenueCloseMechanics,
    reason: str,
    *,
    close_action: MarketAction | None = None,
) -> CloseLegPlan:
    action = close_action or (
        MarketAction.LAY if open_leg.opening_action is MarketAction.BACK else MarketAction.SELL
    )
    return CloseLegPlan(
        venue=open_leg.venue,
        canonical_outcome=open_leg.canonical_outcome,
        opening_action=open_leg.opening_action,
        close_action=action,
        mechanics=mechanics,
        required_close_quantity=open_leg.filled_size,
        filled_close_quantity=Decimal("0"),
        available_closing_capacity=Decimal("0"),
        native_currency=open_leg.native_currency,
        executable=False,
        rejection_reason=reason,
    )
