from __future__ import annotations

from decimal import Decimal

from sports_hedge.arbitrage.allocation.models import (
    AllocationBalance,
    AllocationLeg,
    AllocationRequest,
    BankrollAllocationPolicy,
    OpenPositionExposure,
    VenueNativeAmount,
)
from sports_hedge.arbitrage.models import ArbitrageSolution, PayoffSolution
from sports_hedge.arbitrage.priority_alerts.models import (
    FillConfidenceBreakdown,
    LegExecutionMode,
    OpportunitySurvivability,
    PriorityLeg,
)
from sports_hedge.application.complete_set import SOLVER_MODEL_GENERALIZED, SOLVER_MODEL_SIMPLE
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.liquidity import PaperLiquiditySnapshot
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.paper.trades import PaperTrade, PaperTradeState


def request_from_complete_set(
    legs: list[PriorityLeg],
    solution: ArbitrageSolution,
    *,
    policy: BankrollAllocationPolicy | None = None,
    balances: list[AllocationBalance] | None = None,
    open_positions: list[OpenPositionExposure] | None = None,
    canonical_event_id: str | None = None,
    execution_risk_score: int | None = None,
    fill_confidence: FillConfidenceBreakdown | None = None,
    survivability: OpportunitySurvivability | None = None,
    expected_lock_duration_hours: Decimal | None = None,
    expected_lock_basis: str | None = None,
    quote_age_ms: int | None = None,
    external_confirmation_latency_seconds: Decimal | None = None,
    recent_volatility_bps: Decimal | None = None,
    require_internal_balances: bool = True,
) -> AllocationRequest:
    by_outcome = {leg.outcome: leg for leg in legs}
    allocation_legs: list[AllocationLeg] = []
    for stake in solution.stakes:
        if stake.stake <= 0:
            continue
        leg = by_outcome[stake.outcome]
        allocation_legs.append(
            AllocationLeg(
                leg_id=(
                    f"{leg.venue.value}:{leg.source_market_id}:"
                    f"{leg.source_runner_id or ''}:{leg.outcome}"
                ),
                outcome=leg.outcome,
                venue=leg.venue,
                source_market_id=leg.source_market_id,
                source_runner_id=leg.source_runner_id,
                solver_stake=stake.stake,
                max_stake=leg.max_stake_reporting,
                capital_per_unit=Decimal("1"),
                native_currency=leg.native_currency,
                gbp_per_unit=leg.gbp_per_unit,
                execution_mode=str(leg.execution_mode),
                levels_consumed=leg.levels_consumed,
                quote_age_ms=leg.quote_age_ms,
                quote_persistence=leg.quote_persistence,
                assumed_latency_ms=leg.assumed_latency_ms,
            )
        )
    return AllocationRequest(
        solver_model=SOLVER_MODEL_SIMPLE,
        is_arbitrage=solution.is_arbitrage,
        canonical_event_id=canonical_event_id,
        roi=solution.roi if solution.is_arbitrage else Decimal("0"),
        guaranteed_profit_at_solver_size=solution.guaranteed_profit,
        committed_capital_at_solver_size=solution.total_stake,
        legs=allocation_legs,
        balances=balances or [],
        open_positions=open_positions or [],
        policy=policy or BankrollAllocationPolicy(),
        execution_risk_score=execution_risk_score,
        fill_confidence=fill_confidence,
        survivability=survivability,
        expected_lock_duration_hours=expected_lock_duration_hours,
        expected_lock_basis=expected_lock_basis,
        quote_age_ms=quote_age_ms,
        external_confirmation_latency_seconds=external_confirmation_latency_seconds,
        recent_volatility_bps=recent_volatility_bps,
        require_internal_balances=require_internal_balances,
    )


def request_from_payoff(
    solution: PayoffSolution,
    *,
    max_stake_by_leg: dict[str, Decimal],
    native_currency_by_leg: dict[str, str],
    gbp_per_unit_by_leg: dict[str, Decimal],
    execution_mode_by_venue: dict[VenueName, LegExecutionMode] | None = None,
    levels_consumed_by_leg: dict[str, int] | None = None,
    quote_age_ms: int | None = None,
    payoff_by_leg: dict[str, dict[str, Decimal]] | None = None,
    policy: BankrollAllocationPolicy | None = None,
    balances: list[AllocationBalance] | None = None,
    open_positions: list[OpenPositionExposure] | None = None,
    canonical_event_id: str | None = None,
    execution_risk_score: int | None = None,
    fill_confidence: FillConfidenceBreakdown | None = None,
    survivability: OpportunitySurvivability | None = None,
    expected_lock_duration_hours: Decimal | None = None,
    expected_lock_basis: str | None = None,
    external_confirmation_latency_seconds: Decimal | None = None,
    recent_volatility_bps: Decimal | None = None,
    require_internal_balances: bool = True,
) -> AllocationRequest:
    modes = execution_mode_by_venue or {}
    levels = levels_consumed_by_leg or {}
    payoffs = payoff_by_leg or {}
    allocation_legs: list[AllocationLeg] = []
    for stake in solution.selected_stakes:
        if stake.stake <= 0:
            continue
        currency = native_currency_by_leg[stake.leg_id]
        rate = gbp_per_unit_by_leg[stake.leg_id]
        allocation_legs.append(
            AllocationLeg(
                leg_id=stake.leg_id,
                outcome=stake.runner_outcome or stake.leg_id,
                venue=stake.venue,
                source_market_id=stake.source_market_id,
                source_runner_id=stake.source_runner_id,
                solver_stake=stake.stake,
                max_stake=max_stake_by_leg[stake.leg_id],
                capital_per_unit=stake.capital_per_unit,
                native_currency=currency,
                gbp_per_unit=rate,
                execution_mode=str(modes.get(stake.venue, "INTERNAL")),
                levels_consumed=levels.get(stake.leg_id, 1),
                quote_age_ms=quote_age_ms or 0,
                payoff_per_unit=payoffs.get(stake.leg_id),
            )
        )
    return AllocationRequest(
        solver_model=SOLVER_MODEL_GENERALIZED,
        is_arbitrage=solution.is_arbitrage,
        canonical_event_id=canonical_event_id,
        roi=solution.roi if solution.is_arbitrage else Decimal("0"),
        guaranteed_profit_at_solver_size=solution.minimum_state_pnl,
        committed_capital_at_solver_size=solution.total_capital_used,
        legs=allocation_legs,
        state_pnl_at_solver_size=dict(solution.state_pnl) if solution.state_pnl else None,
        balances=balances or [],
        open_positions=open_positions or [],
        policy=policy or BankrollAllocationPolicy(),
        execution_risk_score=execution_risk_score,
        fill_confidence=fill_confidence,
        survivability=survivability,
        expected_lock_duration_hours=expected_lock_duration_hours,
        expected_lock_basis=expected_lock_basis,
        quote_age_ms=quote_age_ms,
        external_confirmation_latency_seconds=external_confirmation_latency_seconds,
        recent_volatility_bps=recent_volatility_bps,
        require_internal_balances=require_internal_balances,
    )


def balances_from_liquidity(
    snapshot: PaperLiquiditySnapshot,
    *,
    gbp_per_unit: dict[str, Decimal] | None = None,
    conditionally_releasable: dict[tuple[VenueName, str], Decimal] | None = None,
) -> list[AllocationBalance]:
    rates = gbp_per_unit or {}
    extra = conditionally_releasable or {}
    rows: list[AllocationBalance] = []
    for pool in snapshot.pools:
        rate = rates.get(pool.native_currency)
        if pool.native_currency == "GBP":
            rate = Decimal("1")
        rows.append(
            AllocationBalance(
                venue=pool.venue,
                currency=pool.native_currency,
                available=pool.available,
                locked=pool.locked,
                transit=pool.transit,
                conditionally_releasable=extra.get((pool.venue, pool.native_currency), Decimal("0")),
                gbp_per_unit=rate,
            )
        )
    return rows


def exposures_from_trades(trades: list[PaperTrade]) -> list[OpenPositionExposure]:
    open_states = {
        PaperTradeState.PENDING,
        PaperTradeState.PARTIAL,
        PaperTradeState.OPEN,
        PaperTradeState.AWAITING_MANUAL_EXTERNAL,
    }
    exposures: list[OpenPositionExposure] = []
    for trade in trades:
        if trade.state not in open_states:
            continue
        native: dict[tuple[VenueName, str], Decimal] = {}
        for leg in trade.legs:
            amount = leg.filled_stake if leg.filled_stake > 0 else leg.requested_stake
            if amount <= 0:
                continue
            key = (leg.venue, leg.currency.upper())
            native[key] = native.get(key, Decimal("0")) + amount
        exposures.append(
            OpenPositionExposure(
                opportunity_id=trade.opportunity_id,
                canonical_event_id=trade.canonical_event_id,
                capital_native=[
                    VenueNativeAmount(venue=venue, currency=currency, amount=amount)
                    for (venue, currency), amount in native.items()
                ],
                capital_reporting=trade.capital_locked_gbp,
            )
        )
    return exposures


def request_from_paper_decision(
    decision: PaperScanDecision,
    *,
    policy: BankrollAllocationPolicy,
    balances: list[AllocationBalance],
    open_positions: list[OpenPositionExposure] | None = None,
    expected_lock_duration_hours: Decimal | None = None,
    expected_lock_basis: str | None = None,
    recent_volatility_bps: Decimal | None = None,
) -> AllocationRequest | None:
    fx = {item.currency.upper(): item.gbp_per_unit for item in decision.fx_snapshots}
    fx.setdefault("GBP", Decimal("1"))
    currency_by_venue = {leg.venue: leg.currency for leg in decision.fill_legs}
    if decision.depth_scan is not None and decision.depth_scan.solution.is_arbitrage:
        from sports_hedge.arbitrage.priority_alerts.models import PriorityLeg as PLeg

        legs: list[PLeg] = []
        for quote in decision.depth_scan.selected_quotes:
            currency = currency_by_venue.get(quote.venue, "GBP")
            rate = fx.get(currency, Decimal("1"))
            mode = LegExecutionMode(
                decision.execution_modes.get(quote.venue, LegExecutionMode.INTERNAL)
            )
            legs.append(
                PLeg(
                    outcome=quote.outcome,
                    venue=quote.venue,
                    source_market_id=quote.source_market_id,
                    source_runner_id=quote.source_runner_id,
                    net_decimal_odds=quote.net_decimal_odds,
                    max_stake_reporting=quote.cumulative_depth,
                    native_currency=currency,
                    native_max_stake=quote.cumulative_depth / rate,
                    gbp_per_unit=rate,
                    levels_consumed=quote.levels_consumed,
                    quote_age_ms=decision.quote_age_ms or 0,
                    execution_mode=mode,
                )
            )
        return request_from_complete_set(
            legs,
            decision.depth_scan.solution,
            policy=policy,
            balances=balances,
            open_positions=open_positions,
            canonical_event_id=decision.canonical_event_id,
            execution_risk_score=decision.execution_risk.score if decision.execution_risk else None,
            expected_lock_duration_hours=expected_lock_duration_hours,
            expected_lock_basis=expected_lock_basis,
            quote_age_ms=decision.quote_age_ms,
            recent_volatility_bps=recent_volatility_bps,
        )
    if decision.payoff_scan is not None and decision.payoff_scan.solution.is_arbitrage:
        solution = decision.payoff_scan.solution
        max_stake: dict[str, Decimal] = {}
        levels: dict[str, int] = {}
        currency: dict[str, str] = {}
        rates: dict[str, Decimal] = {}
        for quote in decision.payoff_scan.selected_quotes:
            for stake in solution.selected_stakes:
                if (
                    stake.venue is quote.venue
                    and stake.source_market_id == quote.source_market_id
                    and (stake.runner_outcome or "") == quote.outcome
                ):
                    max_stake[stake.leg_id] = quote.cumulative_depth
                    levels[stake.leg_id] = quote.levels_consumed
                    native = currency_by_venue.get(quote.venue, "GBP")
                    currency[stake.leg_id] = native
                    rates[stake.leg_id] = fx.get(native, Decimal("1"))
        for stake in solution.selected_stakes:
            if stake.stake <= 0:
                continue
            native = currency.get(stake.leg_id) or currency_by_venue.get(stake.venue, "GBP")
            currency[stake.leg_id] = native
            rates.setdefault(stake.leg_id, fx.get(native, Decimal("1")))
            max_stake.setdefault(stake.leg_id, stake.stake)
        modes = {
            venue: LegExecutionMode(mode) for venue, mode in decision.execution_modes.items()
        }
        return request_from_payoff(
            solution,
            max_stake_by_leg=max_stake,
            native_currency_by_leg=currency,
            gbp_per_unit_by_leg=rates,
            execution_mode_by_venue=modes,
            levels_consumed_by_leg=levels,
            quote_age_ms=decision.quote_age_ms,
            policy=policy,
            balances=balances,
            open_positions=open_positions,
            canonical_event_id=decision.canonical_event_id,
            execution_risk_score=decision.execution_risk.score if decision.execution_risk else None,
            expected_lock_duration_hours=expected_lock_duration_hours,
            expected_lock_basis=expected_lock_basis,
            recent_volatility_bps=recent_volatility_bps,
        )
    return None
