from __future__ import annotations

from decimal import Decimal

from sports_hedge.accounting.dimensions import CapitalSource
from sports_hedge.arbitrage.allocation.adapters import request_from_complete_set
from sports_hedge.arbitrage.allocation.engine import allocate
from sports_hedge.arbitrage.allocation.models import (
    AllocationBalance,
    AllocationResult,
    BankrollAllocationPolicy,
)
from sports_hedge.arbitrage.models import ArbitrageSolution
from sports_hedge.arbitrage.priority_alerts.models import (
    AutomatedPoolBalance,
    FillConfidenceInputs,
    LegExecutionMode,
    PriorityLeg,
    RecommendedManualSize,
    VenueCurrencyAmount,
)
from sports_hedge.arbitrage.priority_alerts.fill_confidence import score_fill_confidence
from sports_hedge.arbitrage.priority_alerts.thresholds import PriorityAlertThresholds
from sports_hedge.arbitrage.solver import CompleteSetArbitrageSolver


def recommend_manual_size(
    legs: list[PriorityLeg],
    unconstrained: ArbitrageSolution,
    *,
    thresholds: PriorityAlertThresholds,
    execution_risk_score: int,
    automated_pools: list[AutomatedPoolBalance] | None = None,
    solver: CompleteSetArbitrageSolver | None = None,
    policy: BankrollAllocationPolicy | None = None,
    open_positions=None,
    canonical_event_id: str | None = None,
    expected_lock_duration_hours: Decimal | None = None,
    expected_lock_basis: str | None = None,
) -> RecommendedManualSize:
    if not unconstrained.is_arbitrage or not unconstrained.stakes:
        raise ValueError("recommended size requires a confirmed ordinary arbitrage solution")

    del solver  # ratios come from the provided solver vector; do not re-solve to fit bankroll
    allocation = allocate_priority_legs(
        legs,
        unconstrained,
        thresholds=thresholds,
        execution_risk_score=execution_risk_score,
        automated_pools=automated_pools,
        policy=policy,
        open_positions=open_positions,
        canonical_event_id=canonical_event_id,
        expected_lock_duration_hours=expected_lock_duration_hours,
        expected_lock_basis=expected_lock_basis,
    )
    if not allocation.accepted:
        raise ValueError(allocation.rejection_reason or "allocation_failed")
    return recommended_from_allocation(
        allocation, legs, unconstrained, thresholds, automated_pools=automated_pools
    )


def allocate_priority_legs(
    legs: list[PriorityLeg],
    unconstrained: ArbitrageSolution,
    *,
    thresholds: PriorityAlertThresholds,
    execution_risk_score: int,
    automated_pools: list[AutomatedPoolBalance] | None = None,
    policy: BankrollAllocationPolicy | None = None,
    open_positions=None,
    canonical_event_id: str | None = None,
    expected_lock_duration_hours: Decimal | None = None,
    expected_lock_basis: str | None = None,
) -> AllocationResult:
    coverage = _depth_coverage(legs, unconstrained)
    fill = score_fill_confidence(
        FillConfidenceInputs(
            depth_coverage_ratio=coverage,
            quote_age_ms=max(leg.quote_age_ms for leg in legs),
            quote_persistence=min(leg.quote_persistence for leg in legs),
            levels_consumed=max(leg.levels_consumed for leg in legs),
            assumed_latency_ms=max(leg.assumed_latency_ms for leg in legs),
            venue_cancellation_rate=_optional_max((leg.venue_cancellation_rate for leg in legs)),
            historical_paper_fill_rate=_optional_min(
                (leg.historical_paper_fill_rate for leg in legs)
            ),
        )
    )
    pools = automated_pools or []
    balances = [
        AllocationBalance(
            venue=item.venue,
            currency=item.currency,
            available=item.amount,
        )
        for item in pools
    ]
    merged = policy or BankrollAllocationPolicy(
        min_reserve_fraction=Decimal("0"),
        min_reserve_amount=None,
        max_pool_fraction_per_opportunity=Decimal("1"),
        max_open_capital_fraction=Decimal("1"),
        max_same_fixture_capital_fraction=Decimal("1"),
        max_concurrent_open_opportunities=None,
        safety_haircut=thresholds.safety_haircut,
        operator_recommended_cap_reporting=thresholds.operator_manual_cap,
        risk_limit_reporting=thresholds.risk_limit,
        venue_limits_native=dict(thresholds.venue_limits),
    )
    if policy is None:
        merged.safety_haircut = thresholds.safety_haircut
        merged.operator_recommended_cap_reporting = thresholds.operator_manual_cap
        merged.risk_limit_reporting = thresholds.risk_limit
        merged.venue_limits_native = dict(thresholds.venue_limits)
    request = request_from_complete_set(
        legs,
        unconstrained,
        policy=merged,
        balances=balances,
        open_positions=open_positions,
        canonical_event_id=canonical_event_id,
        execution_risk_score=execution_risk_score,
        fill_confidence=fill,
        quote_age_ms=max(leg.quote_age_ms for leg in legs),
        require_internal_balances=False,
    )
    if expected_lock_duration_hours is not None:
        request = request.model_copy(
            update={
                "expected_lock_duration_hours": expected_lock_duration_hours,
                "expected_lock_basis": expected_lock_basis,
            }
        )
    return allocate(request)


def recommended_from_allocation(
    allocation: AllocationResult,
    legs: list[PriorityLeg],
    unconstrained: ArbitrageSolution,
    thresholds: PriorityAlertThresholds,
    *,
    automated_pools: list[AutomatedPoolBalance] | None = None,
) -> RecommendedManualSize:
    limiting_leg = _limiting_leg(legs, unconstrained)
    by_outcome = {item.outcome: item for item in allocation.recommended_stakes}
    scaled = []
    for stake in unconstrained.stakes:
        if stake.stake <= 0:
            continue
        allocated = by_outcome[stake.outcome]
        scale = allocated.stake_reporting / stake.stake
        scaled.append(
            stake.model_copy(
                update={
                    "stake": allocated.stake_reporting,
                    "state_return": stake.state_return * scale,
                }
            )
        )
    capital_required = [
        VenueCurrencyAmount.model_validate(item.model_dump())
        for item in allocation.capital_required
    ]
    auto_pool_draw, additional = additional_capital_required(
        capital_required,
        [
            AutomatedPoolBalance(venue=item.venue, currency=item.currency, amount=item.amount)
            for item in (automated_pools or [])
        ],
        legs,
    )
    turnover = None
    if allocation.capital_turnover is not None:
        turnover = allocation.capital_turnover.model_dump(mode="json")
    return RecommendedManualSize(
        raw_limiting_depth=limiting_leg.max_stake_reporting,
        safety_haircut=thresholds.safety_haircut,
        recommended_size=allocation.recommended_limiting_stake,
        maximum_validated_size=allocation.maximum_limiting_stake,
        limiting_leg_outcome=limiting_leg.outcome,
        limiting_leg_venue=limiting_leg.venue,
        total_stake_reporting=allocation.recommended_committed_capital,
        guaranteed_payoff=unconstrained.guaranteed_return * allocation.scale_recommended,
        guaranteed_profit=allocation.guaranteed_profit,
        guaranteed_roi=allocation.guaranteed_roi,
        capital_efficiency=allocation.guaranteed_roi,
        stake_plan=scaled,
        capital_required=capital_required,
        additional_capital_required=additional,
        quote_age_ms=max(leg.quote_age_ms for leg in legs),
        fill_confidence=allocation.fill_confidence or score_fill_confidence(
            FillConfidenceInputs(
                depth_coverage_ratio=Decimal("1"),
                quote_age_ms=0,
                levels_consumed=1,
            )
        ),
        execution_risk_score=allocation.execution_risk_score or 0,
        auto_pool_draw=auto_pool_draw,
        has_external_leg=any(
            leg.execution_mode == LegExecutionMode.EXTERNAL_OPERATOR for leg in legs
        ),
        limiting_constraint=(
            allocation.limiting_constraint.value if allocation.limiting_constraint else None
        ),
        limiting_constraint_detail=allocation.limiting_constraint_detail,
        recommended_committed_capital=allocation.recommended_committed_capital,
        maximum_validated_capital=allocation.maximum_validated_capital,
        reduction_factors=[factor.model_dump(mode="json") for factor in allocation.reduction_factors],
        free_balance_after=[row.model_dump(mode="json") for row in allocation.free_balance_after],
        reserve_remaining=[row.model_dump(mode="json") for row in allocation.reserve_remaining],
        expected_lock_duration_hours=allocation.expected_lock_duration_hours,
        expected_lock_basis=allocation.expected_lock_basis,
        estimated_time_to_release=(
            allocation.estimated_time_to_release.model_dump(mode="json")
            if allocation.estimated_time_to_release is not None
            else None
        ),
        settled_at=allocation.settled_at,
        capital_turnover=turnover,
        survivability=allocation.survivability,
    )


def _limiting_leg(legs: list[PriorityLeg], solution: ArbitrageSolution) -> PriorityLeg:
    by_outcome = {leg.outcome: leg for leg in legs}
    binding = min(
        solution.stakes,
        key=lambda item: (
            by_outcome[item.outcome].max_stake_reporting / item.stake
            if item.stake > 0
            else Decimal("Infinity")
        ),
    )
    return by_outcome[binding.outcome]


def size_for_limiting_stake(
    legs: list[PriorityLeg],
    unconstrained: ArbitrageSolution,
    limiting_stake: Decimal,
    *,
    solver: CompleteSetArbitrageSolver | None = None,
) -> ArbitrageSolution:
    solver = solver or CompleteSetArbitrageSolver()
    limiting_raw = _limiting_leg(legs, unconstrained).max_stake_reporting
    scale = limiting_stake / limiting_raw
    return solver.solve(
        [leg.as_executable_quote() for leg in legs],
        capital_limit=unconstrained.total_stake * scale,
    )


def _depth_coverage(legs: list[PriorityLeg], solution: ArbitrageSolution) -> Decimal:
    by_outcome = {leg.outcome: leg for leg in legs}
    ratios: list[Decimal] = []
    for stake in solution.stakes:
        depth = by_outcome[stake.outcome].max_stake_reporting
        if stake.stake <= 0:
            continue
        ratios.append(depth / stake.stake)
    return min(ratios) if ratios else Decimal("0")


def capital_required_by_venue_currency(
    legs: list[PriorityLeg],
    solution: ArbitrageSolution,
) -> list[VenueCurrencyAmount]:
    by_outcome = {leg.outcome: leg for leg in legs}
    buckets: dict[tuple[str, str, str], VenueCurrencyAmount] = {}
    for stake in solution.stakes:
        if stake.stake <= 0:
            continue
        leg = by_outcome[stake.outcome]
        native = stake.stake / leg.gbp_per_unit
        source = (
            CapitalSource.MANUAL_EXTERNAL
            if leg.execution_mode == LegExecutionMode.EXTERNAL_OPERATOR
            else CapitalSource.MANUAL_OVERRIDE
        )
        key = (leg.venue.value, leg.native_currency, source.value)
        existing = buckets.get(key)
        if existing is None:
            buckets[key] = VenueCurrencyAmount(
                venue=leg.venue,
                currency=leg.native_currency,
                amount=native,
                capital_source=source,
            )
        else:
            buckets[key] = existing.model_copy(update={"amount": existing.amount + native})
    return list(buckets.values())


def additional_capital_required(
    required: list[VenueCurrencyAmount],
    pools: list[AutomatedPoolBalance],
    legs: list[PriorityLeg],
) -> tuple[list[VenueCurrencyAmount], list[VenueCurrencyAmount]]:
    """Split capital into AUTO_POOL draw vs additional. External legs never touch AUTO_POOL."""

    external_venues = {
        leg.venue
        for leg in legs
        if leg.execution_mode == LegExecutionMode.EXTERNAL_OPERATOR
    }
    pool_map = {(item.venue, item.currency.upper()): item.amount for item in pools}
    auto_draw: list[VenueCurrencyAmount] = []
    extra: list[VenueCurrencyAmount] = []
    for item in required:
        if item.capital_source == CapitalSource.MANUAL_EXTERNAL or item.venue in external_venues:
            extra.append(
                item.model_copy(
                    update={"capital_source": CapitalSource.MANUAL_EXTERNAL}
                )
            )
            continue
        available = pool_map.get((item.venue, item.currency), Decimal("0"))
        drawn = min(item.amount, available)
        remainder = max(item.amount - available, Decimal("0"))
        if drawn > 0:
            auto_draw.append(
                VenueCurrencyAmount(
                    venue=item.venue,
                    currency=item.currency,
                    amount=drawn,
                    capital_source=CapitalSource.AUTO_POOL,
                )
            )
        extra.append(
            VenueCurrencyAmount(
                venue=item.venue,
                currency=item.currency,
                amount=remainder,
                capital_source=CapitalSource.MANUAL_OVERRIDE,
            )
        )
    return auto_draw, extra


def _optional_max(values) -> Decimal | None:
    present = [value for value in values if value is not None]
    return max(present) if present else None


def _optional_min(values) -> Decimal | None:
    present = [value for value in values if value is not None]
    return min(present) if present else None
