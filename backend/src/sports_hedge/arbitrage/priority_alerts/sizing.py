from __future__ import annotations

from decimal import Decimal

from sports_hedge.accounting.dimensions import CapitalSource
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
) -> RecommendedManualSize:
    if not unconstrained.is_arbitrage or not unconstrained.stakes:
        raise ValueError("recommended size requires a confirmed ordinary arbitrage solution")

    solver = solver or CompleteSetArbitrageSolver()
    limiting_leg = _limiting_leg(legs, unconstrained)
    raw_limiting_depth = limiting_leg.max_stake_reporting
    after_haircut = raw_limiting_depth * (Decimal("1") - thresholds.safety_haircut)

    venue_cap = thresholds.venue_limits.get(limiting_leg.venue)
    caps = [after_haircut, thresholds.operator_manual_cap, thresholds.risk_limit]
    if venue_cap is not None:
        caps.append(venue_cap)
    recommended_limiting = min(caps)
    if recommended_limiting <= 0:
        raise ValueError("recommended size must be positive")

    scale = recommended_limiting / raw_limiting_depth
    capital_limit = unconstrained.total_stake * scale
    sized = solver.solve(
        [leg.as_executable_quote() for leg in legs],
        capital_limit=capital_limit,
    )
    if not sized.is_arbitrage:
        raise ValueError("sized recommendation lost ordinary arbitrage")

    coverage = _depth_coverage(legs, sized)
    fill = score_fill_confidence(
        FillConfidenceInputs(
            depth_coverage_ratio=coverage,
            quote_age_ms=max(leg.quote_age_ms for leg in legs),
            quote_persistence=min(leg.quote_persistence for leg in legs),
            levels_consumed=max(leg.levels_consumed for leg in legs),
            assumed_latency_ms=max(leg.assumed_latency_ms for leg in legs),
            venue_cancellation_rate=_optional_max(
                (leg.venue_cancellation_rate for leg in legs)
            ),
            historical_paper_fill_rate=_optional_min(
                (leg.historical_paper_fill_rate for leg in legs)
            ),
        )
    )
    capital_required = capital_required_by_venue_currency(legs, sized)
    auto_pool_draw, additional = additional_capital_required(
        capital_required,
        automated_pools or [],
        legs,
    )
    return RecommendedManualSize(
        raw_limiting_depth=raw_limiting_depth,
        safety_haircut=thresholds.safety_haircut,
        recommended_size=recommended_limiting,
        maximum_validated_size=raw_limiting_depth,
        limiting_leg_outcome=limiting_leg.outcome,
        limiting_leg_venue=limiting_leg.venue,
        total_stake_reporting=sized.total_stake,
        guaranteed_payoff=sized.guaranteed_return,
        guaranteed_profit=sized.guaranteed_profit,
        guaranteed_roi=sized.roi,
        capital_efficiency=sized.roi,
        stake_plan=sized.stakes,
        capital_required=capital_required,
        additional_capital_required=additional,
        quote_age_ms=max(leg.quote_age_ms for leg in legs),
        fill_confidence=fill,
        execution_risk_score=execution_risk_score,
        reporting_currency="GBP",
        auto_pool_draw=auto_pool_draw,
        has_external_leg=any(
            leg.execution_mode == LegExecutionMode.EXTERNAL_OPERATOR for leg in legs
        ),
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
