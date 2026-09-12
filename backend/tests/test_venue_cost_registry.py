from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sports_hedge.domain.football import MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import CostKnownStatus, FeeBasis, MarketAction, OrderRole
from sports_hedge.fees.effective import apply_venue_costs
from sports_hedge.fees.resolver import UnknownRequiredCostError, VenueCostResolver, VenueCostRule, phase1_seed_rules


AS_OF = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)


def test_unknown_required_cost_fails_closed() -> None:
    resolver = VenueCostResolver()
    with pytest.raises(UnknownRequiredCostError, match="unknown_required_venue_cost:smarkets"):
        resolver.resolve(
            venue=VenueName.SMARKETS,
            market_class=MarketFamily.MATCH_RESULT,
            action=MarketAction.BACK,
            as_of=AS_OF,
        )
    with pytest.raises(UnknownRequiredCostError, match="unknown_required_venue_cost:matchbook"):
        resolver.resolve(
            venue=VenueName.MATCHBOOK,
            market_class=MarketFamily.UNKNOWN,
            action=MarketAction.BACK,
            as_of=AS_OF,
        )


def test_seeded_rules_retain_provenance_and_effective_date() -> None:
    resolver = VenueCostResolver()
    snapshot = resolver.resolve(
        venue=VenueName.MATCHBOOK,
        market_class=MarketFamily.BOTH_TEAMS_TO_SCORE,
        action=MarketAction.BACK,
        as_of=AS_OF,
        order_role=OrderRole.TAKER,
    )
    assert snapshot.fee_basis is FeeBasis.PROFIT_COMMISSION
    assert snapshot.rate == Decimal("0.02")
    assert snapshot.source.startswith("venue_cost_registry")
    assert snapshot.effective_from is not None
    assert snapshot.snapshot_id
    assert snapshot.market_class == "both_teams_to_score"


def test_different_fee_structures_change_best_venue() -> None:
    extra = VenueCostRule(
        venue=VenueName.SMARKETS,
        market_class=MarketFamily.MATCH_RESULT.value,
        action=MarketAction.BACK,
        order_role=OrderRole.TAKER,
        fee_basis=FeeBasis.NONE_CONFIRMED,
        known_status=CostKnownStatus.KNOWN,
        currency="GBP",
        source="venue_cost_registry:test_fixture",
        effective_from=datetime(2024, 1, 1, tzinfo=UTC),
        catalog_version="test",
        detail="Fixture cheaper none_confirmed schedule for ranking proof.",
    )
    resolver = VenueCostResolver(phase1_seed_rules() + [extra])
    matchbook = resolver.resolve(
        venue=VenueName.MATCHBOOK,
        market_class=MarketFamily.MATCH_RESULT,
        action=MarketAction.BACK,
        as_of=AS_OF,
    )
    smarkets = resolver.resolve(
        venue=VenueName.SMARKETS,
        market_class=MarketFamily.MATCH_RESULT,
        action=MarketAction.BACK,
        as_of=AS_OF,
    )
    odds = Decimal("2.20")
    matchbook_net = apply_venue_costs(matchbook, gross_decimal_odds=odds).net_decimal_equivalent
    smarkets_net = apply_venue_costs(smarkets, gross_decimal_odds=odds).net_decimal_equivalent
    assert smarkets.fee_basis is FeeBasis.NONE_CONFIRMED
    assert matchbook.fee_basis is FeeBasis.PROFIT_COMMISSION
    assert smarkets_net > matchbook_net
    player = resolver.resolve(
        venue=VenueName.MATCHBOOK,
        market_class=MarketFamily.PLAYER_PROPS,
        action=MarketAction.BACK,
        as_of=AS_OF,
    )
    football = resolver.resolve(
        venue=VenueName.MATCHBOOK,
        market_class=MarketFamily.MATCH_RESULT,
        action=MarketAction.BACK,
        as_of=AS_OF,
    )
    assert player.rate != football.rate
    assert apply_venue_costs(player, gross_decimal_odds=odds).net_decimal_equivalent < matchbook_net
