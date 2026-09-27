from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sports_hedge.application.fixture_inventory import assemble_fixture_inventory
from sports_hedge.application.market_observation import PolymarketObservationBuilder
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.domain.football import CanonicalOutcome, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import CostKnownStatus, FeeBasis, MarketAction
from sports_hedge.fees.effective import apply_venue_costs
from sports_hedge.fees.polymarket import (
    POLYMARKET_TAKER_FORMULA,
    extract_polymarket_fee_metadata,
    polymarket_cost_from_market,
)
from sports_hedge.fees.resolver import UnknownRequiredCostError, VenueCostResolver
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from test_fixture_inventory import _inventory, _market
from test_paper_scan_pipeline import OBSERVED, matchbook_payloads, polymarket_payloads
from sports_hedge.application.market_observation import MatchbookObservationBuilder
from sports_hedge.config import Settings
from sports_hedge.fx.repository import SqliteFxRateRepository
from sports_hedge.fx.service import FxRateService
from fx_test_helpers import fresh_usd_ecb_close


AS_OF = datetime(2026, 9, 13, 12, tzinfo=UTC)


def _enabled_market(**overrides: object) -> dict:
    payload = {
        "id": "pm-fee-enabled",
        "feesEnabled": True,
        "feeSchedule": {
            "rate": "0.03",
            "exponent": 1,
            "takerOnly": True,
            "rebateRate": "0.15",
        },
    }
    payload.update(overrides)
    return payload


def test_polymarket_registry_no_longer_seeds_global_zero() -> None:
    resolver = VenueCostResolver()
    with pytest.raises(UnknownRequiredCostError, match="unknown_required_venue_cost:polymarket"):
        resolver.resolve(
            venue=VenueName.POLYMARKET,
            market_class=MarketFamily.BOTH_TEAMS_TO_SCORE,
            action=MarketAction.BUY,
            as_of=AS_OF,
        )
    assert not any(rule.venue is VenueName.POLYMARKET for rule in resolver.rules)


def test_fee_disabled_market_is_known_zero() -> None:
    snapshot = polymarket_cost_from_market(
        {"id": "pm-off", "feesEnabled": False},
        captured_at=AS_OF,
        source_market_id="pm-off",
    )
    assert snapshot.is_economically_known()
    assert snapshot.fee_basis is FeeBasis.NONE_CONFIRMED
    economics = apply_venue_costs(
        snapshot, gross_decimal_odds=Decimal("2"), stake=Decimal("50"), require_gbp=False
    )
    assert economics.venue_fee == Decimal("0")
    assert economics.net_payoff == Decimal("100")


def test_nested_trading_disabled_is_known_zero() -> None:
    snapshot = polymarket_cost_from_market(
        {"trading": {"fees_enabled": False}},
        captured_at=AS_OF,
        source_market_id="nested",
    )
    assert snapshot.fee_basis is FeeBasis.NONE_CONFIRMED


def test_fee_enabled_uses_taker_formula_not_zero() -> None:
    snapshot = polymarket_cost_from_market(_enabled_market(), captured_at=AS_OF)
    assert snapshot.fee_basis is FeeBasis.FORMULA
    assert snapshot.formula_name == POLYMARKET_TAKER_FORMULA
    assert snapshot.formula_parameters["rate"] == Decimal("0.03")
    # 100 shares at 50¢, stake = 50, odds = 2 → fee = 100 × 0.03 × 0.25 = 0.75
    economics = apply_venue_costs(
        snapshot, gross_decimal_odds=Decimal("2"), stake=Decimal("50"), require_gbp=False
    )
    assert economics.venue_fee == Decimal("0.75")
    assert economics.net_payoff == Decimal("99.25")


def test_legacy_gamma_fee_rate_with_applicability() -> None:
    snapshot = polymarket_cost_from_market(
        {"id": "legacy", "feesEnabled": True, "feeRate": "0.05"},
        captured_at=AS_OF,
    )
    assert snapshot.fee_basis is FeeBasis.FORMULA
    assert snapshot.formula_parameters["rate"] == Decimal("0.05")
    assert snapshot.formula_parameters["exponent"] == Decimal("1")


def test_missing_applicability_fails_closed() -> None:
    snapshot = polymarket_cost_from_market({"id": "bare", "feeRate": "0.05"}, captured_at=AS_OF)
    assert snapshot.known_status is CostKnownStatus.UNKNOWN
    assert snapshot.fee_basis is FeeBasis.UNKNOWN


def test_enabled_without_rate_fails_closed() -> None:
    snapshot = polymarket_cost_from_market(
        {"feesEnabled": True, "feeSchedule": {"exponent": 1, "takerOnly": True}},
        captured_at=AS_OF,
    )
    assert not snapshot.is_economically_known()


def test_extract_preserves_market_id_for_provenance() -> None:
    meta = extract_polymarket_fee_metadata(_enabled_market())
    assert meta["fees_enabled"] is True
    assert meta["source_market_id"] == "pm-fee-enabled"
    assert meta["fee_schedule"]["rate"] == "0.03"


def test_observation_builder_attaches_fee_metadata() -> None:
    event, market, books = polymarket_payloads()
    observation = PolymarketObservationBuilder().build(event, market, books, observed_at=OBSERVED)
    assert "polymarket_fee" in observation.metadata
    assert observation.metadata["polymarket_fee"]["fees_enabled"] is None


def test_paper_scan_does_not_inherit_registry_zero_when_metadata_missing() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    service = PaperScanService(intelligence, cost_resolver=VenueCostResolver())
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=OBSERVED, quote_age_ms=120
    )
    polymarket = PolymarketObservationBuilder().build(
        pm_event, pm_market, pm_books, observed_at=OBSERVED, quote_age_ms=180
    )
    try:
        decision = service.scan_pair(matchbook, polymarket, maximum_execution_risk=100)
        assert decision.market_match.matched is True
        assert decision.eligible_for_paper_simulation is False
        assert any(
            "unknown_required_venue_cost" in reason
            or "unknown_costs" in reason
            or "missing_fx" in reason
            for reason in decision.rejection_reasons
        )
    finally:
        repository.close()


def test_paper_scan_fee_disabled_polymarket_is_known_zero() -> None:
    fx = FxRateService(SqliteFxRateRepository())
    fx.persist_ecb_closes([fresh_usd_ecb_close(Decimal("0.75000000"))])
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    service = PaperScanService(
        intelligence,
        settings=Settings(max_slippage_bps=0, fx_spread_bps=0),
        fx_service=fx,
        cost_resolver=VenueCostResolver(),
    )
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()
    pm_market = {**pm_market, "feesEnabled": False}
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=OBSERVED, quote_age_ms=120
    )
    polymarket = PolymarketObservationBuilder().build(
        pm_event, pm_market, pm_books, observed_at=OBSERVED, quote_age_ms=180
    )
    try:
        decision = service.scan_pair(matchbook, polymarket, maximum_execution_risk=100)
        assert decision.market_match.matched is True
        assert "unknown_required_venue_cost:polymarket" not in decision.rejection_reasons
        assert "not_registered" not in decision.market_match.reasons
    finally:
        repository.close()


def test_inventory_surfaces_polymarket_fee_states() -> None:
    event, market, books = polymarket_payloads()
    canonical = _market(
        VenueName.POLYMARKET,
        family=MarketFamily.BOTH_TEAMS_TO_SCORE,
        source_id="pm-market-1",
        outcomes=[CanonicalOutcome.YES, CanonicalOutcome.NO],
    )
    # Rebuild using builder so metadata is attached; then swap canonical.
    missing = PolymarketObservationBuilder().build(event, market, books, observed_at=OBSERVED)
    disabled = PolymarketObservationBuilder().build(
        event, {**market, "feesEnabled": False}, books, observed_at=OBSERVED
    )
    enabled = PolymarketObservationBuilder().build(
        event, {**market, **_enabled_market(), "id": market["id"]}, books, observed_at=OBSERVED
    )

    def row_for(observation):
        item = _inventory(canonical, name="Both Teams To Score", observation=observation)
        rows = assemble_fixture_inventory([], [item], cost_resolver=VenueCostResolver())
        assert rows[0].polymarket is not None
        return rows[0].polymarket

    unknown = row_for(missing)
    assert unknown.fee_status == "unknown"
    assert unknown.fee_label == "fee metadata missing · fail closed"

    off = row_for(disabled)
    assert off.fee_status == "known"
    assert off.fee_label == "fee disabled · known zero"

    on = row_for(enabled)
    assert on.fee_status == "known"
    assert on.fee_label == "market-specific taker formula (rate 0.03)"
    assert on.fee_formula_name == POLYMARKET_TAKER_FORMULA
