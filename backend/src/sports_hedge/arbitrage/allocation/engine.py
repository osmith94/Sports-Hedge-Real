from __future__ import annotations

from collections import defaultdict
from decimal import Decimal

from sports_hedge.accounting.dimensions import CapitalSource
from sports_hedge.arbitrage.allocation.models import (
    AllocatedStake,
    AllocationBalance,
    AllocationConstraintKind,
    AllocationLeg,
    AllocationRequest,
    AllocationResult,
    BankrollAllocationPolicy,
    CapitalTurnoverMetric,
    ConstraintBinding,
    EstimateConfidence,
    EstimatedTimeToRelease,
    NativeBalanceAfter,
    OpenPositionExposure,
    ReductionFactor,
    ReductionInputStatus,
    VenueNativeAmount,
)
from sports_hedge.domain.models import VenueName

EXTERNAL_OPERATOR = "EXTERNAL_OPERATOR"
KICKOFF_ONLY_BASES = frozenset({"time_to_kickoff", "kickoff", "time_until_kickoff"})
OPTIMISTIC_ELAPSED_BASES = frozenset(
    {
        "kickoff_plus_regulation_plus_settlement_buffer",
        "kickoff_plus_first_half_plus_settlement_buffer",
    }
)
ADVISORY_ESTIMATE_BASES = frozenset(
    {
        "kickoff_plus_elapsed_regulation_halftime_stoppage_settlement_buffer",
        "modelled_remaining_elapsed_regulation_halftime_stoppage_settlement_buffer",
        "kickoff_plus_elapsed_first_half_stoppage_settlement_buffer",
        "provider_live_match_clock",
    }
)


def _advisory_time_to_release(request: AllocationRequest) -> EstimatedTimeToRelease | None:
    """Modelled ranking input only. Never a settlement clock or cash-release trigger."""

    hours = request.estimated_time_to_release_hours
    basis = request.estimate_basis
    if hours is None or hours <= 0 or not basis:
        return None
    if basis in KICKOFF_ONLY_BASES or basis in OPTIMISTIC_ELAPSED_BASES:
        return None
    if basis not in ADVISORY_ESTIMATE_BASES:
        return None
    confidence = request.estimate_confidence
    if confidence is None:
        confidence = (
            EstimateConfidence.PROVIDER_LIVE
            if basis == "provider_live_match_clock"
            else EstimateConfidence.MODELLED
        )
    return EstimatedTimeToRelease(
        hours=hours,
        estimate_basis=basis,
        estimate_confidence=confidence,
    )


def allocate(request: AllocationRequest) -> AllocationResult:
    """Scale a solver-valid stake vector. Never changes ratios to fit a bankroll."""

    if not request.is_arbitrage:
        return _rejected(request, AllocationConstraintKind.SOLVER_NOT_ARBITRAGE, "solver_not_arbitrage")
    if not request.legs:
        return _rejected(
            request,
            AllocationConstraintKind.ZERO_STAKE_EXCLUDED,
            "no_positive_solver_stakes",
        )
    if request.committed_capital_at_solver_size <= 0:
        return _rejected(request, AllocationConstraintKind.SOLVER_CAPITAL, "non_positive_solver_capital")

    try:
        _assert_resize_safe(request)
    except ValueError as exc:
        return _rejected(request, AllocationConstraintKind.CANNOT_RESIZE, str(exc))

    bindings = _hard_constraints(request)
    missing = next(
        (item for item in bindings if item.kind is AllocationConstraintKind.MISSING_BALANCE_DATA),
        None,
    )
    if missing is not None:
        return _rejected(request, missing.kind, missing.detail, hard_constraints=bindings)

    scale_max = min((item.scale for item in bindings), default=Decimal("1"))
    if scale_max < 0:
        scale_max = Decimal("0")
    binding = min(bindings, key=lambda item: (item.scale, _constraint_priority(item.kind)))

    if scale_max <= 0:
        after = _balances_after(request, Decimal("0"))
        return AllocationResult(
            accepted=False,
            solver_model=request.solver_model,
            reporting_currency=request.reporting_currency,
            limiting_constraint=binding.kind,
            limiting_constraint_detail=binding.detail,
            hard_constraints=bindings,
            free_balance_after=after,
            reserve_remaining=after,
            rejection_reason=binding.detail,
            fill_confidence=request.fill_confidence,
            execution_risk_score=request.execution_risk_score,
            survivability=request.survivability,
            estimated_time_to_release=_advisory_time_to_release(request),
            settled_at=None,
            scale_maximum=Decimal("0"),
            scale_recommended=Decimal("0"),
        )

    reductions = _recommendation_reductions(request, scale_max)
    quality = Decimal("1")
    for factor in reductions:
        quality *= Decimal("1") - factor.amount
        if quality <= 0:
            quality = Decimal("0")
            break
    scale_recommended = scale_max * quality
    scale_recommended = min(scale_recommended, scale_max)
    if scale_recommended <= 0:
        scale_recommended = scale_max * (Decimal("1") - request.policy.safety_haircut)
        if scale_recommended <= 0:
            return _rejected(
                request,
                AllocationConstraintKind.CANNOT_RESIZE,
                "recommended_scale_non_positive",
                hard_constraints=bindings,
            )

    try:
        rec_plan = _plan_at_scale(request, scale_recommended)
        _validate_scaled_payoff(request, scale_max)
        _validate_scaled_payoff(request, scale_recommended)
    except ValueError as exc:
        return _rejected(request, AllocationConstraintKind.CANNOT_RESIZE, str(exc), hard_constraints=bindings)

    max_capital = request.committed_capital_at_solver_size * scale_max
    rec_capital = request.committed_capital_at_solver_size * scale_recommended
    profit = request.guaranteed_profit_at_solver_size * scale_recommended
    after = _balances_after(request, scale_recommended)
    limiting_leg = _limiting_depth_leg(request.legs)
    estimate = _advisory_time_to_release(request)
    turnover = None
    if estimate is not None and rec_capital > 0:
        turnover = CapitalTurnoverMetric(
            metric=profit / rec_capital / estimate.hours,
            estimated_time_to_release_hours=estimate.hours,
            estimate_basis=estimate.estimate_basis,
            estimate_confidence=estimate.estimate_confidence,
            expected_lock_duration_hours=estimate.hours,
            expected_lock_basis=estimate.estimate_basis,
            guaranteed_profit=profit,
            committed_capital=rec_capital,
        )

    return AllocationResult(
        accepted=True,
        solver_model=request.solver_model,
        reporting_currency=request.reporting_currency,
        maximum_validated_capital=max_capital,
        recommended_committed_capital=rec_capital,
        maximum_validated_size=max_capital,
        recommended_size=rec_capital,
        maximum_limiting_stake=limiting_leg.solver_stake * scale_max,
        recommended_limiting_stake=limiting_leg.solver_stake * scale_recommended,
        recommended_stakes=rec_plan,
        capital_required=_capital_required(rec_plan),
        guaranteed_profit=profit,
        guaranteed_roi=request.roi,
        free_balance_after=after,
        reserve_remaining=after,
        limiting_constraint=binding.kind,
        limiting_constraint_detail=binding.detail,
        hard_constraints=bindings,
        reduction_factors=reductions,
        expected_lock_duration_hours=estimate.hours if estimate else None,
        expected_lock_basis=estimate.estimate_basis if estimate else None,
        estimated_time_to_release=estimate,
        settled_at=None,
        capital_turnover=turnover,
        fill_confidence=request.fill_confidence,
        execution_risk_score=request.execution_risk_score,
        survivability=request.survivability,
        scale_maximum=scale_max,
        scale_recommended=scale_recommended,
    )


def _constraint_priority(kind: AllocationConstraintKind) -> int:
    order = [
        AllocationConstraintKind.MISSING_BALANCE_DATA,
        AllocationConstraintKind.EXECUTABLE_DEPTH,
        AllocationConstraintKind.NATIVE_VENUE_BALANCE,
        AllocationConstraintKind.MIN_FREE_RESERVE,
        AllocationConstraintKind.MAX_POOL_FRACTION,
        AllocationConstraintKind.VENUE_LIMIT,
        AllocationConstraintKind.PER_OPPORTUNITY_LIMIT,
        AllocationConstraintKind.FIXTURE_CONCENTRATION,
        AllocationConstraintKind.PORTFOLIO_CAP,
        AllocationConstraintKind.CONCURRENCY,
        AllocationConstraintKind.EXTERNAL_LEG_CAP,
        AllocationConstraintKind.SOLVER_CAPITAL,
    ]
    try:
        return order.index(kind)
    except ValueError:
        return 99


def _assert_resize_safe(request: AllocationRequest) -> None:
    if request.state_pnl_at_solver_size:
        if any(pnl <= 0 for pnl in request.state_pnl_at_solver_size.values()):
            raise ValueError("state_payoff_not_strictly_positive")
    ratios = None
    for leg in request.legs:
        if leg.solver_stake <= 0:
            raise ValueError("zero-stake legs must be excluded")
        if ratios is None:
            continue
    _ = ratios


def _validate_scaled_payoff(request: AllocationRequest, scale: Decimal) -> None:
    if scale <= 0:
        raise ValueError("non_positive_scale")
    for leg in request.legs:
        stake = leg.solver_stake * scale
        if stake > leg.max_stake:
            raise ValueError("scaled_stake_exceeds_executable_depth")
    if request.state_pnl_at_solver_size:
        for state, pnl in request.state_pnl_at_solver_size.items():
            if pnl * scale <= 0:
                raise ValueError(f"state_payoff_invalid:{state}")


def _hard_constraints(request: AllocationRequest) -> list[ConstraintBinding]:
    policy = request.policy
    bindings: list[ConstraintBinding] = [
        ConstraintBinding(
            kind=AllocationConstraintKind.EXECUTABLE_DEPTH,
            scale=_depth_scale(request.legs),
            detail="limiting executable marginal depth",
        ),
        ConstraintBinding(
            kind=AllocationConstraintKind.SOLVER_CAPITAL,
            scale=Decimal("1"),
            detail="solver stake vector at provided capital_per_unit",
        ),
    ]

    by_key = {(item.venue, item.currency): item for item in request.balances}
    native_need = _native_need_all(request.legs)
    for (venue, currency), need in native_need.items():
        balance = by_key.get((venue, currency))
        if balance is None:
            if request.require_internal_balances:
                bindings.append(
                    ConstraintBinding(
                        kind=AllocationConstraintKind.MISSING_BALANCE_DATA,
                        scale=Decimal("0"),
                        detail=f"missing_balance:{venue.value}:{currency}",
                        venue=venue,
                        currency=currency,
                    )
                )
            continue
        if not request.require_internal_balances:
            continue
        bindings.append(
            ConstraintBinding(
                kind=AllocationConstraintKind.NATIVE_VENUE_BALANCE,
                scale=_scale_for_budget(need, balance.spendable),
                detail=f"native available {venue.value} {currency}",
                venue=venue,
                currency=currency,
            )
        )
        reserve = _reserve_required(policy, balance)
        remaining_after_reserve = balance.spendable - reserve
        if remaining_after_reserve < 0:
            remaining_after_reserve = Decimal("0")
        bindings.append(
            ConstraintBinding(
                kind=AllocationConstraintKind.MIN_FREE_RESERVE,
                scale=_scale_for_budget(need, remaining_after_reserve),
                detail=f"reserve {venue.value} {currency}",
                venue=venue,
                currency=currency,
            )
        )
        pool_cap = balance.pool_total * policy.max_pool_fraction_per_opportunity
        bindings.append(
            ConstraintBinding(
                kind=AllocationConstraintKind.MAX_POOL_FRACTION,
                scale=_scale_for_budget(need, pool_cap),
                detail=f"max {policy.max_pool_fraction_per_opportunity} of {venue.value} pool",
                venue=venue,
                currency=currency,
            )
        )
        venue_cap = policy.venue_limits_native.get(venue)
        if venue_cap is not None:
            bindings.append(
                ConstraintBinding(
                    kind=AllocationConstraintKind.VENUE_LIMIT,
                    scale=_scale_for_budget(need, venue_cap),
                    detail=f"configured venue limit {venue.value}",
                    venue=venue,
                    currency=currency,
                )
            )
        open_native = _open_native(request.open_positions, venue, currency)
        open_room = balance.pool_total * policy.max_open_capital_fraction - open_native
        if open_room < 0:
            open_room = Decimal("0")
        bindings.append(
            ConstraintBinding(
                kind=AllocationConstraintKind.PORTFOLIO_CAP,
                scale=_scale_for_budget(need, open_room),
                detail=f"max open fraction {venue.value} {currency}",
                venue=venue,
                currency=currency,
            )
        )
        fixture_native = _fixture_native(
            request.open_positions, request.canonical_event_id, venue, currency
        )
        fixture_room = balance.pool_total * policy.max_same_fixture_capital_fraction - fixture_native
        if fixture_room < 0:
            fixture_room = Decimal("0")
        bindings.append(
            ConstraintBinding(
                kind=AllocationConstraintKind.FIXTURE_CONCENTRATION,
                scale=_scale_for_budget(need, fixture_room),
                detail=f"same-fixture cap {venue.value} {currency}",
                venue=venue,
                currency=currency,
            )
        )

    if policy.per_opportunity_limit_reporting is not None:
        bindings.append(
            ConstraintBinding(
                kind=AllocationConstraintKind.PER_OPPORTUNITY_LIMIT,
                scale=_scale_for_budget(
                    request.committed_capital_at_solver_size,
                    policy.per_opportunity_limit_reporting,
                ),
                detail="per-opportunity reporting-currency cap",
            )
        )
    if policy.portfolio_cap_reporting is not None:
        open_reporting = sum(
            (position.capital_reporting or Decimal("0") for position in request.open_positions),
            Decimal("0"),
        )
        room = policy.portfolio_cap_reporting - open_reporting
        if room < 0:
            room = Decimal("0")
        bindings.append(
            ConstraintBinding(
                kind=AllocationConstraintKind.PORTFOLIO_CAP,
                scale=_scale_for_budget(request.committed_capital_at_solver_size, room),
                detail="total open-paper reporting cap",
            )
        )
    if policy.max_concurrent_open_opportunities is not None:
        open_count = len(request.open_positions)
        if open_count >= policy.max_concurrent_open_opportunities:
            bindings.append(
                ConstraintBinding(
                    kind=AllocationConstraintKind.CONCURRENCY,
                    scale=Decimal("0"),
                    detail="maximum concurrent open opportunities",
                )
            )
        else:
            bindings.append(
                ConstraintBinding(
                    kind=AllocationConstraintKind.CONCURRENCY,
                    scale=Decimal("1"),
                    detail="concurrent open opportunities within cap",
                )
            )

    external_need = _native_need_external(request.legs)
    if policy.external_leg_cap_native is not None:
        for (venue, currency), native_need in external_need.items():
            bindings.append(
                ConstraintBinding(
                    kind=AllocationConstraintKind.EXTERNAL_LEG_CAP,
                    scale=_scale_for_budget(native_need, policy.external_leg_cap_native),
                    detail=f"manual/external cap {venue.value} {currency}",
                    venue=venue,
                    currency=currency,
                )
            )
    return bindings


def _depth_scale(legs: list[AllocationLeg]) -> Decimal:
    raw = min((leg.max_stake / leg.solver_stake for leg in legs), default=Decimal("1"))
    return Decimal("1") if raw > 1 else raw


def _scale_for_budget(need: Decimal, budget: Decimal) -> Decimal:
    if need <= 0:
        return Decimal("1")
    if budget <= 0:
        return Decimal("0")
    scale = budget / need
    if scale > 1:
        return Decimal("1")
    return scale


def _reserve_required(policy: BankrollAllocationPolicy, balance: AllocationBalance) -> Decimal:
    fractional = balance.pool_total * policy.min_reserve_fraction
    if policy.min_reserve_amount is None:
        return fractional
    return max(policy.min_reserve_amount, fractional)


def _native_need_all(legs: list[AllocationLeg]) -> dict[tuple[VenueName, str], Decimal]:
    need: dict[tuple[VenueName, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for leg in legs:
        need[(leg.venue, leg.native_currency)] += leg.capital_native
    return dict(need)


def _native_need_internal(legs: list[AllocationLeg]) -> dict[tuple[VenueName, str], Decimal]:
    need: dict[tuple[VenueName, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for leg in legs:
        if leg.execution_mode == EXTERNAL_OPERATOR:
            continue
        need[(leg.venue, leg.native_currency)] += leg.capital_native
    return dict(need)


def _native_need_external(legs: list[AllocationLeg]) -> dict[tuple[VenueName, str], Decimal]:
    need: dict[tuple[VenueName, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for leg in legs:
        if leg.execution_mode != EXTERNAL_OPERATOR:
            continue
        need[(leg.venue, leg.native_currency)] += leg.capital_native
    return dict(need)


def _open_native(
    positions: list[OpenPositionExposure], venue: VenueName, currency: str
) -> Decimal:
    total = Decimal("0")
    for position in positions:
        for item in position.capital_native:
            if item.venue is venue and item.currency == currency:
                total += item.amount
    return total


def _fixture_native(
    positions: list[OpenPositionExposure],
    canonical_event_id: str | None,
    venue: VenueName,
    currency: str,
) -> Decimal:
    if not canonical_event_id:
        return Decimal("0")
    total = Decimal("0")
    for position in positions:
        if position.canonical_event_id != canonical_event_id:
            continue
        for item in position.capital_native:
            if item.venue is venue and item.currency == currency:
                total += item.amount
    return total


def _plan_at_scale(request: AllocationRequest, scale: Decimal) -> list[AllocatedStake]:
    stakes: list[AllocatedStake] = []
    for leg in request.legs:
        stake_reporting = leg.solver_stake * scale
        capital_reporting = stake_reporting * leg.capital_per_unit
        stake_native = stake_reporting / leg.gbp_per_unit
        capital_native = capital_reporting / leg.gbp_per_unit
        source = (
            CapitalSource.MANUAL_EXTERNAL
            if leg.execution_mode == EXTERNAL_OPERATOR
            else CapitalSource.AUTO_POOL
        )
        stakes.append(
            AllocatedStake(
                leg_id=leg.leg_id,
                outcome=leg.outcome,
                venue=leg.venue,
                source_market_id=leg.source_market_id,
                source_runner_id=leg.source_runner_id,
                stake_reporting=stake_reporting,
                stake_native=stake_native,
                capital_reporting=capital_reporting,
                capital_native=capital_native,
                native_currency=leg.native_currency,
                capital_source=source,
                execution_mode=leg.execution_mode,
            )
        )
    return stakes


def _capital_required(stakes: list[AllocatedStake]) -> list[VenueNativeAmount]:
    buckets: dict[tuple[str, str, str], VenueNativeAmount] = {}
    for stake in stakes:
        key = (stake.venue.value, stake.native_currency, stake.capital_source.value)
        existing = buckets.get(key)
        if existing is None:
            buckets[key] = VenueNativeAmount(
                venue=stake.venue,
                currency=stake.native_currency,
                amount=stake.capital_native,
                capital_source=stake.capital_source,
            )
        else:
            buckets[key] = existing.model_copy(update={"amount": existing.amount + stake.capital_native})
    return list(buckets.values())


def _balances_after(request: AllocationRequest, scale: Decimal) -> list[NativeBalanceAfter]:
    allocated: dict[tuple[VenueName, str], Decimal] = defaultdict(lambda: Decimal("0"))
    for leg in request.legs:
        allocated[(leg.venue, leg.native_currency)] += leg.capital_native * scale
    rows: list[NativeBalanceAfter] = []
    for balance in request.balances:
        used = allocated.get((balance.venue, balance.currency), Decimal("0"))
        free = balance.spendable - used
        if free < 0:
            if request.require_internal_balances:
                raise ValueError("balance_would_go_negative")
            free = Decimal("0")
        reserve = _reserve_required(request.policy, balance)
        remaining_reserve = min(free, reserve) if free < reserve else reserve
        # Reserve remaining is unallocated cash earmarked as reserve, capped by free cash.
        if free >= reserve:
            remaining_reserve = reserve
        else:
            remaining_reserve = free
        rows.append(
            NativeBalanceAfter(
                venue=balance.venue,
                currency=balance.currency,
                free_balance=free,
                reserve_required=reserve,
                reserve_remaining=remaining_reserve,
                pool_total=balance.pool_total,
                locked=balance.locked,
                transit=balance.transit,
                conditionally_releasable=balance.conditionally_releasable,
                allocated_native=used,
            )
        )
    return rows


def _limiting_depth_leg(legs: list[AllocationLeg]) -> AllocationLeg:
    return min(legs, key=lambda leg: leg.max_stake / leg.solver_stake)


def _recommendation_reductions(
    request: AllocationRequest, scale_max: Decimal
) -> list[ReductionFactor]:
    policy = request.policy
    factors: list[ReductionFactor] = []
    if policy.safety_haircut > 0:
        factors.append(
            ReductionFactor(
                name="safety_haircut",
                amount=policy.safety_haircut,
                reason="configured safety haircut below hard maximum",
                input_status=ReductionInputStatus.DEFAULT_POLICY,
            )
        )
    quote_age = request.quote_age_ms
    if quote_age is None:
        quote_age = max((leg.quote_age_ms for leg in request.legs), default=None)
    if quote_age is None:
        factors.append(
            ReductionFactor(
                name="quote_age",
                amount=policy.max_quote_age_reduction / 2,
                reason="quote age unknown; conservative default",
                input_status=ReductionInputStatus.UNKNOWN,
            )
        )
    elif quote_age > policy.quote_age_reduction_start_ms:
        span = max(policy.quote_age_reduction_full_ms - policy.quote_age_reduction_start_ms, 1)
        t = min(Decimal(quote_age - policy.quote_age_reduction_start_ms) / Decimal(span), Decimal("1"))
        amount = policy.max_quote_age_reduction * t
        if amount > 0:
            factors.append(
                ReductionFactor(
                    name="quote_age",
                    amount=amount,
                    reason=f"quote_age_ms={quote_age}",
                    input_status=ReductionInputStatus.KNOWN,
                )
            )
    persistence = min((leg.quote_persistence for leg in request.legs), default=Decimal("1"))
    if persistence < Decimal("1"):
        amount = min(Decimal("1") - persistence, Decimal("0.20"))
        if amount > 0:
            factors.append(
                ReductionFactor(
                    name="quote_persistence",
                    amount=amount,
                    reason=f"min_quote_persistence={persistence}",
                    input_status=ReductionInputStatus.KNOWN,
                )
            )
    if request.execution_risk_score is None:
        factors.append(
            ReductionFactor(
                name="execution_risk",
                amount=policy.max_execution_risk_reduction / 5,
                reason="execution risk unknown; conservative default",
                input_status=ReductionInputStatus.UNKNOWN,
            )
        )
    elif request.execution_risk_score > policy.execution_risk_reduction_start:
        span = max(
            policy.execution_risk_reduction_full - policy.execution_risk_reduction_start, 1
        )
        t = min(
            Decimal(request.execution_risk_score - policy.execution_risk_reduction_start)
            / Decimal(span),
            Decimal("1"),
        )
        amount = policy.max_execution_risk_reduction * t
        if amount > 0:
            factors.append(
                ReductionFactor(
                    name="execution_risk",
                    amount=amount,
                    reason=f"execution_risk_score={request.execution_risk_score}",
                    input_status=ReductionInputStatus.KNOWN,
                )
            )
    fill = request.fill_confidence
    band = getattr(fill, "band", None)
    band_value = getattr(band, "value", band)
    if fill is None:
        factors.append(
            ReductionFactor(
                name="fill_confidence",
                amount=policy.fill_confidence_medium_reduction,
                reason="fill confidence unknown; conservative default",
                input_status=ReductionInputStatus.UNKNOWN,
            )
        )
    elif band_value == "LOW":
        factors.append(
            ReductionFactor(
                name="fill_confidence",
                amount=policy.fill_confidence_low_reduction,
                reason="fill_confidence=LOW estimate_not_guarantee",
                input_status=ReductionInputStatus.KNOWN,
            )
        )
    elif band_value == "MEDIUM":
        factors.append(
            ReductionFactor(
                name="fill_confidence",
                amount=policy.fill_confidence_medium_reduction,
                reason="fill_confidence=MEDIUM estimate_not_guarantee",
                input_status=ReductionInputStatus.KNOWN,
            )
        )
    extra_levels = max(max(leg.levels_consumed for leg in request.legs) - 1, 0)
    if extra_levels:
        amount = min(policy.extra_level_reduction * extra_levels, policy.max_levels_reduction)
        factors.append(
            ReductionFactor(
                name="book_levels",
                amount=amount,
                reason=f"levels_consumed_beyond_touch={extra_levels}",
                input_status=ReductionInputStatus.KNOWN,
            )
        )
    survivability = request.survivability
    if survivability is None or survivability.data_insufficient or survivability.survivability_score is None:
        factors.append(
            ReductionFactor(
                name="survivability",
                amount=policy.unknown_survivability_reduction,
                reason="survivability unknown; no invented probability",
                input_status=ReductionInputStatus.UNKNOWN,
            )
        )
    elif survivability.low_survivability_warning or (
        survivability.survivability_score is not None and survivability.survivability_score < 40
    ):
        factors.append(
            ReductionFactor(
                name="survivability",
                amount=policy.low_survivability_reduction,
                reason="low survivability estimate_not_guarantee",
                input_status=ReductionInputStatus.KNOWN,
            )
        )
    if any(leg.execution_mode == EXTERNAL_OPERATOR for leg in request.legs):
        if request.external_confirmation_latency_seconds is None:
            factors.append(
                ReductionFactor(
                    name="external_confirmation_latency",
                    amount=policy.external_latency_reduction,
                    reason="external confirmation latency unknown; conservative default",
                    input_status=ReductionInputStatus.UNKNOWN,
                )
            )
        elif request.external_confirmation_latency_seconds > 0:
            factors.append(
                ReductionFactor(
                    name="external_confirmation_latency",
                    amount=policy.external_latency_reduction,
                    reason="manual/external confirmation latency",
                    input_status=ReductionInputStatus.KNOWN,
                )
            )
    if request.recent_volatility_bps is None:
        factors.append(
            ReductionFactor(
                name="volatility",
                amount=policy.unknown_volatility_reduction,
                reason="volatility unknown; conservative default",
                input_status=ReductionInputStatus.UNKNOWN,
            )
        )
    elif request.recent_volatility_bps >= policy.elevated_volatility_bps:
        factors.append(
            ReductionFactor(
                name="volatility",
                amount=policy.elevated_volatility_reduction,
                reason=f"recent_volatility_bps={request.recent_volatility_bps}",
                input_status=ReductionInputStatus.KNOWN,
            )
        )
    estimate = _advisory_time_to_release(request)
    if estimate is not None and estimate.hours > policy.long_lock_hours:
        t = min(estimate.hours / (policy.long_lock_hours * 3), Decimal("1"))
        amount = policy.max_lock_duration_reduction * t
        if amount > 0:
            factors.append(
                ReductionFactor(
                    name="lock_duration",
                    amount=amount,
                    reason=(
                        f"advisory {estimate.estimate_basis}={estimate.hours}h; "
                        "does not release capital or settle"
                    ),
                    input_status=ReductionInputStatus.KNOWN,
                )
            )
    open_count = len(request.open_positions)
    if open_count:
        amount = min(
            policy.concurrency_reduction_per_open * open_count,
            policy.max_concurrency_reduction,
        )
        factors.append(
            ReductionFactor(
                name="open_concurrency",
                amount=amount,
                reason=f"open_positions={open_count}",
                input_status=ReductionInputStatus.KNOWN,
            )
        )
    scarcity = _scarcity_reduction(request, scale_max, policy)
    if scarcity is not None:
        factors.append(scarcity)
    if policy.operator_recommended_cap_reporting is not None:
        cap_scale = _scale_for_budget(
            request.committed_capital_at_solver_size * scale_max,
            policy.operator_recommended_cap_reporting,
        )
        if cap_scale < 1:
            factors.append(
                ReductionFactor(
                    name="operator_recommended_cap",
                    amount=Decimal("1") - cap_scale,
                    reason="operator recommended cap below hard maximum",
                    input_status=ReductionInputStatus.DEFAULT_POLICY,
                )
            )
    if policy.risk_limit_reporting is not None:
        cap_scale = _scale_for_budget(
            request.committed_capital_at_solver_size * scale_max,
            policy.risk_limit_reporting,
        )
        if cap_scale < 1:
            factors.append(
                ReductionFactor(
                    name="risk_limit",
                    amount=Decimal("1") - cap_scale,
                    reason="risk limit below hard maximum",
                    input_status=ReductionInputStatus.DEFAULT_POLICY,
                )
            )
    return [factor for factor in factors if factor.amount > 0]


def _scarcity_reduction(
    request: AllocationRequest,
    scale_max: Decimal,
    policy: BankrollAllocationPolicy,
) -> ReductionFactor | None:
    worst = Decimal("1")
    known = False
    for balance in request.balances:
        need = _native_need_all(request.legs).get((balance.venue, balance.currency))
        if not need:
            continue
        known = True
        used = need * scale_max
        free_after = balance.spendable - used
        if balance.pool_total <= 0:
            continue
        free_fraction = free_after / balance.pool_total
        if free_fraction < policy.scarcity_start_free_fraction:
            t = min(
                (policy.scarcity_start_free_fraction - free_fraction)
                / policy.scarcity_start_free_fraction,
                Decimal("1"),
            )
            worst = min(worst, Decimal("1") - policy.max_scarcity_reduction * t)
    if not known or worst >= 1:
        return None
    return ReductionFactor(
        name="free_capital_scarcity",
        amount=Decimal("1") - worst,
        reason="free cash after maximum allocation is scarce",
        input_status=ReductionInputStatus.KNOWN,
    )


def _rejected(
    request: AllocationRequest,
    kind: AllocationConstraintKind,
    detail: str,
    *,
    hard_constraints: list[ConstraintBinding] | None = None,
) -> AllocationResult:
    return AllocationResult(
        accepted=False,
        solver_model=request.solver_model,
        reporting_currency=request.reporting_currency,
        limiting_constraint=kind,
        limiting_constraint_detail=detail,
        hard_constraints=hard_constraints or [],
        rejection_reason=detail,
        fill_confidence=request.fill_confidence,
        execution_risk_score=request.execution_risk_score,
        survivability=request.survivability,
        estimated_time_to_release=_advisory_time_to_release(request),
        settled_at=None,
        guaranteed_roi=request.roi,
    )
