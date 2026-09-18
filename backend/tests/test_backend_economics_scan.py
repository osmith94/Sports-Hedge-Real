from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.config import Settings
from sports_hedge.fees.resolver import VenueCostResolver
from sports_hedge.fx.repository import SqliteFxRateRepository
from sports_hedge.fx.service import FxRateService
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from fx_test_helpers import fresh_usd_ecb_close
from test_paper_scan_pipeline import matchbook_payloads, polymarket_payloads


OBSERVED = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)


def test_paper_scan_resolves_backend_fx_and_registry_costs() -> None:
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
        assert decision.eligible_for_paper_simulation is True
        usd = next(item for item in decision.fx_snapshots if item.currency == "USD")
        assert usd.source == "ecb_eurofxref"
        assert usd.gbp_per_unit == Decimal("0.75000000")
        assert {item.venue.value for item in decision.venue_costs} == {"matchbook", "polymarket"}
        sources = {item.venue.value: item.source for item in decision.venue_costs}
        assert sources["matchbook"].startswith("venue_cost_registry")
        assert sources["polymarket"].startswith("polymarket_fee_schedule:market:disabled")
    finally:
        repository.close()


def test_paper_scan_fails_closed_without_required_fx() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    service = PaperScanService(
        intelligence,
        fx_service=FxRateService(SqliteFxRateRepository()),
        cost_resolver=VenueCostResolver(),
    )
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
        assert decision.eligible_for_paper_simulation is False
        assert any(reason.startswith("missing_fx_rate") for reason in decision.rejection_reasons)
    finally:
        repository.close()
