from __future__ import annotations

from decimal import Decimal

from sports_hedge.arbitrage.allocation.models import BankrollAllocationPolicy
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName


def default_bankroll_policy() -> BankrollAllocationPolicy:
    return BankrollAllocationPolicy()


def _operator_placement(settings: Settings):
    """Three reporting-GBP caps. The legacy per-trade field is not authority."""

    from sports_hedge.persistence.operator_scanner_settings import (
        effective_operator_scanner_settings,
    )

    operator = effective_operator_scanner_settings(settings)
    return (
        operator.max_event_gbp,
        operator.max_opportunity_gbp,
        operator.max_one_time_gbp,
    )


def policy_from_settings(settings: Settings) -> BankrollAllocationPolicy:
    venue_limits: dict[VenueName, Decimal] = {}
    if settings.allocation_matchbook_limit_gbp is not None:
        venue_limits[VenueName.MATCHBOOK] = Decimal(str(settings.allocation_matchbook_limit_gbp))
    if settings.allocation_polymarket_limit_usd is not None:
        venue_limits[VenueName.POLYMARKET] = Decimal(str(settings.allocation_polymarket_limit_usd))
    max_event, max_opportunity, max_one_time = _operator_placement(settings)
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
        per_opportunity_limit_reporting=None,
        max_event_reporting=max_event,
        max_opportunity_reporting=max_opportunity,
        max_one_time_reporting=max_one_time,
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
