from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.paper import get_matchbook_account_fee_store, get_venue_cost_resolver
from sports_hedge.application.fixture_inventory import assemble_fixture_inventory
from sports_hedge.application.market_observation import MatchbookObservationBuilder, PolymarketObservationBuilder
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.config import Settings
from sports_hedge.domain.football import CanonicalOutcome, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import FeeBasis, MarketAction
from sports_hedge.fees.resolver import (
    MATCHBOOK_OVERRIDE_SOURCE,
    MATCHBOOK_STANDARD_FOOTBALL_COMMISSION,
    VenueCostResolver,
)
from sports_hedge.fx.repository import SqliteFxRateRepository
from sports_hedge.fx.service import FxRateService
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.persistence.matchbook_account_fee import SqliteMatchbookAccountFeeStore
from test_fixture_inventory import _inventory, _market
from test_paper_scan_pipeline import OBSERVED, matchbook_payloads, polymarket_payloads
from fx_test_helpers import fresh_usd_ecb_close


AS_OF = datetime(2026, 9, 13, 17, tzinfo=UTC)


def test_matchbook_override_persists_across_store_restart(tmp_path: Path) -> None:
    database = tmp_path / "paper_account_fees.sqlite"
    store = SqliteMatchbookAccountFeeStore(database)
    store.set_override(Decimal("0.03"))
    store.close()
    restarted = SqliteMatchbookAccountFeeStore(database)
    saved = restarted.get_override()
    assert saved is not None
    assert saved[0] == Decimal("0.03")
    restarted.clear_override()
    assert restarted.get_override() is None


def test_resolver_uses_provider_default_then_override() -> None:
    store = SqliteMatchbookAccountFeeStore()
    resolver = VenueCostResolver(matchbook_fee_store=store)
    default = resolver.resolve(
        venue=VenueName.MATCHBOOK,
        market_class=MarketFamily.BOTH_TEAMS_TO_SCORE,
        action=MarketAction.BACK,
        as_of=AS_OF,
    )
    assert default.rate == MATCHBOOK_STANDARD_FOOTBALL_COMMISSION
    assert default.source.startswith("venue_cost_registry")
    store.set_override(Decimal("0.035"))
    overridden = resolver.resolve(
        venue=VenueName.MATCHBOOK,
        market_class=MarketFamily.BOTH_TEAMS_TO_SCORE,
        action=MarketAction.BACK,
        as_of=AS_OF,
    )
    assert overridden.rate == Decimal("0.035")
    assert overridden.source == MATCHBOOK_OVERRIDE_SOURCE
    assert overridden.account_or_fee_tier == "operator_account_override"
    player = resolver.resolve(
        venue=VenueName.MATCHBOOK,
        market_class=MarketFamily.PLAYER_PROPS,
        action=MarketAction.BACK,
        as_of=AS_OF,
    )
    assert player.rate == Decimal("0.035")
    assert player.source == MATCHBOOK_OVERRIDE_SOURCE
    assert player.account_or_fee_tier == "operator_account_override"
    store.clear_override()
    restored = resolver.resolve(
        venue=VenueName.MATCHBOOK,
        market_class=MarketFamily.BOTH_TEAMS_TO_SCORE,
        action=MarketAction.BACK,
        as_of=AS_OF,
    )
    assert restored.rate == MATCHBOOK_STANDARD_FOOTBALL_COMMISSION


def test_paper_scan_snapshots_applied_matchbook_override() -> None:
    store = SqliteMatchbookAccountFeeStore()
    store.set_override(Decimal("0.03"))
    fx = FxRateService(SqliteFxRateRepository())
    fx.persist_ecb_closes([fresh_usd_ecb_close(Decimal("0.75"))])
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    service = PaperScanService(
        intelligence,
        settings=Settings(max_slippage_bps=0, fx_spread_bps=0),
        fx_service=fx,
        cost_resolver=VenueCostResolver(matchbook_fee_store=store),
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
        mb_cost = next(item for item in decision.venue_costs if item.venue is VenueName.MATCHBOOK)
        assert mb_cost.rate == Decimal("0.03")
        assert mb_cost.fee_basis is FeeBasis.PROFIT_COMMISSION
        assert mb_cost.source == MATCHBOOK_OVERRIDE_SOURCE
        assert "matchbook_operator_account_assumption" in decision.cost_assumption_labels
        assert any("3.00% net-profit commission" in label for label in decision.cost_assumption_labels)
    finally:
        repository.close()


def test_inventory_matchbook_fee_label_includes_rate_and_override() -> None:
    market = _market(
        VenueName.MATCHBOOK,
        family=MarketFamily.BOTH_TEAMS_TO_SCORE,
        source_id="mb-btts",
        outcomes=[CanonicalOutcome.YES, CanonicalOutcome.NO],
    )
    default_rows = assemble_fixture_inventory(
        [_inventory(market, name="Both Teams To Score")],
        [],
        cost_resolver=VenueCostResolver(),
    )
    assert default_rows[0].matchbook is not None
    assert default_rows[0].matchbook.fee_label == "2.00% net-profit commission"
    store = SqliteMatchbookAccountFeeStore()
    store.set_override(Decimal("0.03"))
    override_rows = assemble_fixture_inventory(
        [_inventory(market, name="Both Teams To Score")],
        [],
        cost_resolver=VenueCostResolver(matchbook_fee_store=store),
    )
    facts = override_rows[0].matchbook
    assert facts is not None
    assert facts.fee_label == "3.00% net-profit commission · operator/account assumption"
    assert facts.fee_account_assumption is True
    assert facts.fee_rate == Decimal("0.03")


def test_matchbook_fee_http_save_and_reset(tmp_path: Path) -> None:
    store = SqliteMatchbookAccountFeeStore(tmp_path / "fees.sqlite")
    resolver = VenueCostResolver(matchbook_fee_store=store)
    app.dependency_overrides[get_matchbook_account_fee_store] = lambda: store
    app.dependency_overrides[get_venue_cost_resolver] = lambda: resolver
    client = TestClient(app)
    try:
        status = client.get("/paper/economics-status").json()
        assert status["matchbook_fee"]["effective_rate"] == "0.02"
        assert status["matchbook_fee"]["account_assumption"] is False
        assert status["matchbook_fee"]["label"] == "2.00% net-profit commission"
        assert status["polymarket_fee_policy"]["catalog_seeded"] is False
        saved = client.put("/paper/matchbook-fee", json={"commission_rate": "0.03"}).json()
        assert saved["effective_rate"] == "0.03"
        assert saved["account_assumption"] is True
        again = client.get("/paper/matchbook-fee").json()
        assert again["override_rate"] == "0.03"
        reset = client.post("/paper/matchbook-fee/reset").json()
        assert reset["effective_rate"] == "0.02"
        assert reset["account_assumption"] is False
    finally:
        app.dependency_overrides.clear()
        store.close()
