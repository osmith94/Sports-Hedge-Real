from __future__ import annotations

from decimal import Decimal

from sports_hedge.arbitrage.allocation.models import BankrollAllocationPolicy
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName


def default_bankroll_policy() -> BankrollAllocationPolicy:
    return BankrollAllocationPolicy()


def _per_opportunity_limit(settings: Settings) -> Decimal:
    """Operator max-allocated-per-trade is the allocator per-opportunity cap."""

    try:
        from sports_hedge.persistence.operator_scanner_settings import (
            effective_operator_scanner_settings,
        )

        return effective_operator_scanner_settings(settings).max_allocated_per_trade_gbp
    except Exception:
        configured = settings.allocation_per_opportunity_limit_gbp
        if configured is None:
            configured = settings.max_allocated_per_trade_gbp
        return Decimal(str(configured))


def policy_from_settings(settings: Settings) -> BankrollAllocationPolicy:
    venue_limits: dict[VenueName, Decimal] = {}
    if settings.allocation_matchbook_limit_gbp is not None:
        venue_limits[VenueName.MATCHBOOK] = Decimal(str(settings.allocation_matchbook_limit_gbp))
    if settings.allocation_polymarket_limit_usd is not None:
        venue_limits[VenueName.POLYMARKET] = Decimal(str(settings.allocation_polymarket_limit_usd))
    return BankrollAllocationPolicy(
        min_reserve_amount=(
            Decimal(str(settings.allocation_min_reserve_amount))
            if settings.allocation_min_reserve_amount is not None
            else None
        ),
        min_reserve_fraction=Decimal(str(settings.allocation_min_reserve_fraction)),
        max_pool_fraction_per_opportunity=Decimal(
            str(settings.allocation_max_pool_fraction_per_opportunity)
        ),
        max_open_capital_fraction=Decimal(str(settings.allocation_max_open_capital_fraction)),
        max_same_fixture_capital_fraction=Decimal(
            str(settings.allocation_max_same_fixture_fraction)
        ),
        per_opportunity_limit_reporting=_per_opportunity_limit(settings),
        venue_limits_native=venue_limits,
        portfolio_cap_reporting=(
            Decimal(str(settings.max_total_exposure_gbp))
            if settings.max_total_exposure_gbp
            else None
        ),
        safety_haircut=Decimal(str(settings.priority_safety_haircut)),
        external_leg_cap_native=(
            Decimal(str(settings.allocation_external_leg_cap_native))
            if settings.allocation_external_leg_cap_native is not None
            else None
        ),
        operator_recommended_cap_reporting=Decimal(str(settings.priority_operator_manual_cap)),
        risk_limit_reporting=Decimal(str(settings.priority_risk_limit)),
        football_regulation_playing_minutes=Decimal(
            str(settings.allocation_football_regulation_playing_minutes)
        ),
        football_halftime_minutes=Decimal(str(settings.allocation_football_halftime_minutes)),
        football_stoppage_and_settlement_buffer_minutes=Decimal(
            str(settings.allocation_football_stoppage_settlement_buffer_minutes)
        ),
    )
