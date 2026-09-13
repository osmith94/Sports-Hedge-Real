from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sports_hedge.accounting.paper_journal import NativeCurrencyMixError
from sports_hedge.api.main import app
from sports_hedge.api.paper import get_paper_liquidity_repository
from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.resolver import VenueCostResolver
from sports_hedge.fx.models import PublishedFxClose
from sports_hedge.fx.repository import SqliteFxRateRepository
from sports_hedge.fx.service import FxRateService
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.liquidity import PaperLiquiditySnapshot
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.persistence.liquidity import SqlitePaperLiquidityRepository
from test_paper_scan_pipeline import OBSERVED, matchbook_payloads, polymarket_payloads
from venue_cost_helpers import matchbook_polymarket_costs


def test_liquidity_repository_persists_across_reopen(tmp_path: Path) -> None:
    database = tmp_path / "paper_liquidity.sqlite"
    first = SqlitePaperLiquidityRepository(database)
    first.update_available(
        {
            VenueName.MATCHBOOK: Decimal("123.50"),
            VenueName.POLYMARKET: Decimal("800"),
            VenueName.SMARKETS: Decimal("40"),
        }
    )
    first.close()

    second = SqlitePaperLiquidityRepository(database)
    snapshot = second.get()
    assert snapshot.pool(VenueName.MATCHBOOK).available == Decimal("123.50")
    assert snapshot.pool(VenueName.POLYMARKET).available == Decimal("800")
    assert snapshot.pool(VenueName.SMARKETS).available == Decimal("40")
    assert snapshot.pool(VenueName.SMARKETS).included_in_solver is False
    assert snapshot.pool(VenueName.SMARKETS).connection_status == "not_connected"
    second.close()


def test_native_currencies_stay_separate_and_refuse_combined_cash() -> None:
    repository = SqlitePaperLiquidityRepository()
    snapshot = repository.update_available(
        {
            VenueName.MATCHBOOK: Decimal("100"),
            VenueName.POLYMARKET: Decimal("200"),
            VenueName.KALSHI: Decimal("0"),
            VenueName.SMARKETS: Decimal("50"),
        }
    )
    totals = snapshot.native_totals_by_currency()
    assert totals["GBP"] == Decimal("150")
    assert totals["USD"] == Decimal("200")
    with pytest.raises(NativeCurrencyMixError, match="must not be summed"):
        snapshot.combined_cash_gbp()
    payload = snapshot.model_dump()
    assert "combined_cash_gbp" not in payload
    assert snapshot.pool(VenueName.MATCHBOOK).gbp_carrying_status == "identity"
    assert snapshot.pool(VenueName.POLYMARKET).gbp_carrying_status == "fx_unavailable"
    assert snapshot.pool(VenueName.POLYMARKET).gbp_carrying_value is None
    usd_with_fx = repository.get(gbp_per_unit={"USD": Decimal("0.75")}, fx_source="test_fx")
    assert usd_with_fx.pool(VenueName.POLYMARKET).gbp_carrying_value == Decimal("150.00")
    assert usd_with_fx.pool(VenueName.POLYMARKET).gbp_carrying_status == "fx_converted"
    repository.close()


def test_smarkets_pool_is_excluded_from_solver_limits() -> None:
    snapshot = SqlitePaperLiquidityRepository().get()
    limits = snapshot.solver_gbp_limits({"GBP": Decimal("1"), "USD": Decimal("0.75")})
    assert VenueName.MATCHBOOK in limits
    assert VenueName.POLYMARKET in limits
    assert VenueName.KALSHI in limits
    assert VenueName.SMARKETS not in limits


def test_matchbook_and_polymarket_pools_cap_paper_scan() -> None:
    intelligence = MarketIntelligenceService(SqliteMarketIntelligenceRepository())
    liquidity = SqlitePaperLiquidityRepository()
    liquidity.update_available(
        {
            VenueName.MATCHBOOK: Decimal("5"),
            VenueName.POLYMARKET: Decimal("2"),
            VenueName.SMARKETS: Decimal("5000"),
        }
    )
    service = PaperScanService(intelligence, liquidity=liquidity)
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()
    matchbook = MatchbookObservationBuilder().build(
        mb_event,
        mb_market,
        observed_at=OBSERVED,
        source_latency_ms=80,
        quote_age_ms=120,
    )
    polymarket = PolymarketObservationBuilder().build(
        pm_event,
        pm_market,
        pm_books,
        observed_at=OBSERVED,
        source_latency_ms=110,
        quote_age_ms=180,
    )
    fx = [FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test")]
    uncapped = PaperScanService(intelligence).scan_pair(
        matchbook,
        polymarket,
        venue_costs=matchbook_polymarket_costs(),
        fx_snapshots=fx,
        maximum_execution_risk=100,
    )
    capped = service.scan_pair(
        matchbook,
        polymarket,
        venue_costs=matchbook_polymarket_costs(),
        fx_snapshots=fx,
        maximum_execution_risk=100,
    )
    assert uncapped.depth_scan is not None and uncapped.depth_scan.solution.is_arbitrage
    assert capped.depth_scan is not None and capped.depth_scan.solution.is_arbitrage
    limits = {
        VenueName.MATCHBOOK: Decimal("5"),
        VenueName.POLYMARKET: Decimal("2") * Decimal("0.75"),
    }
    used: dict[VenueName, Decimal] = {}
    for stake in capped.depth_scan.solution.stakes:
        used[stake.venue] = used.get(stake.venue, Decimal("0")) + stake.stake
    for venue, amount in used.items():
        assert amount <= limits[venue] + Decimal("0.0000001")
    assert capped.depth_scan.solution.total_stake < uncapped.depth_scan.solution.total_stake

    liquidity.update_available(
        {VenueName.MATCHBOOK: Decimal("0"), VenueName.POLYMARKET: Decimal("0")}
    )
    rejected = service.scan_pair(
        matchbook,
        polymarket,
        venue_costs=matchbook_polymarket_costs(),
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test")],
        maximum_execution_risk=100,
    )
    assert rejected.eligible_for_paper_simulation is False
    assert "insufficient_venue_capital" in rejected.rejection_reasons


def test_polymarket_pool_uses_backend_fx_not_client_assumption() -> None:
    fx = FxRateService(SqliteFxRateRepository())
    fx.persist_ecb_closes(
        [
            PublishedFxClose(
                currency="USD",
                gbp_per_unit=Decimal("0.50000000"),
                source_date=date(2026, 9, 11),
                retrieved_at=datetime(2026, 9, 11, 16, tzinfo=UTC),
                source="ecb_eurofxref",
                source_id="ecb:2026-09-11:USD",
            )
        ]
    )
    intelligence = MarketIntelligenceService(SqliteMarketIntelligenceRepository())
    liquidity = SqlitePaperLiquidityRepository()
    liquidity.update_available(
        {
            VenueName.MATCHBOOK: Decimal("10000"),
            VenueName.POLYMARKET: Decimal("10"),
            VenueName.SMARKETS: Decimal("0"),
        }
    )
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=OBSERVED, quote_age_ms=120
    )
    polymarket = PolymarketObservationBuilder().build(
        pm_event, pm_market, pm_books, observed_at=OBSERVED, quote_age_ms=180
    )
    service = PaperScanService(
        intelligence,
        settings=Settings(max_slippage_bps=0, fx_spread_bps=0),
        fx_service=fx,
        cost_resolver=VenueCostResolver(),
        liquidity=liquidity,
    )
    decision = service.scan_pair(matchbook, polymarket, maximum_execution_risk=100)
    assert decision.depth_scan is not None and decision.depth_scan.solution.is_arbitrage
    usd = next(item for item in decision.fx_snapshots if item.currency == "USD")
    assert usd.source == "ecb_eurofxref"
    used_pm = sum(
        (
            stake.stake
            for stake in decision.depth_scan.solution.stakes
            if stake.venue is VenueName.POLYMARKET
        ),
        Decimal("0"),
    )
    assert used_pm <= Decimal("5") + Decimal("0.0000001")


def test_liquidity_api_persists_and_resets(tmp_path: Path) -> None:
    repository = SqlitePaperLiquidityRepository(tmp_path / "pools.sqlite")
    app.dependency_overrides[get_paper_liquidity_repository] = lambda: repository
    client = TestClient(app)
    try:
        listed = client.get("/paper/liquidity-pools")
        assert listed.status_code == 200
        body = listed.json()
        assert body["capital_kind"] == "paper_hypothetical"
        venues = {pool["venue"]: pool for pool in body["pools"]}
        assert venues["matchbook"]["native_currency"] == "GBP"
        assert venues["polymarket"]["native_currency"] == "USD"
        assert venues["kalshi"]["native_currency"] == "USD"
        assert venues["smarkets"]["included_in_solver"] is False
        assert "combined_cash" not in str(body)

        updated = client.post(
            "/paper/liquidity-pools",
            json={
                "pools": [
                    {"venue": "matchbook", "available": "77"},
                    {"venue": "polymarket", "available": "88.25"},
                    {"venue": "smarkets", "available": "9"},
                ]
            },
        )
        assert updated.status_code == 200
        again = client.get("/paper/liquidity-pools").json()
        by_venue = {pool["venue"]: pool for pool in again["pools"]}
        assert Decimal(by_venue["matchbook"]["available"]) == Decimal("77")
        assert Decimal(by_venue["polymarket"]["available"]) == Decimal("88.25")
        assert by_venue["smarkets"]["connection_status"] == "not_connected"

        reset = client.post("/paper/liquidity-pools/reset")
        assert reset.status_code == 200
        reset_body = reset.json()
        reset_venues = {pool["venue"]: pool for pool in reset_body["pools"]}
        assert Decimal(reset_venues["matchbook"]["available"]) == Decimal("5000")
        assert Decimal(reset_venues["kalshi"]["available"]) == Decimal("5000")
        assert Decimal(reset_venues["smarkets"]["available"]) == Decimal("0")
    finally:
        app.dependency_overrides.clear()
        repository.close()


def test_liquidity_snapshot_model_round_trip() -> None:
    snapshot = SqlitePaperLiquidityRepository().get()
    restored = PaperLiquiditySnapshot.model_validate(snapshot.model_dump())
    assert restored.pool(VenueName.MATCHBOOK).included_in_solver is True
    assert restored.pool(VenueName.SMARKETS).included_in_solver is False


def test_live_console_hides_demo_pools_and_compacts_empty_states() -> None:
    repo = Path(__file__).resolve().parents[2]
    page = (repo / "frontend" / "app" / "page.tsx").read_text(encoding="utf-8")
    treasury = (repo / "frontend" / "app" / "treasury" / "page.tsx").read_text(encoding="utf-8")
    scan = (repo / "frontend" / "components" / "run-paper-scan.tsx").read_text(encoding="utf-8")
    pools = (repo / "frontend" / "components" / "liquidity-pools.tsx").read_text(encoding="utf-8")
    assert "DEMO_LIQUIDITY_POOLS" not in page
    assert "DEMO_NEAR_ARB" not in page
    assert "DEMO_EXECUTABLE" not in page
    assert "DEMO_ACTIVITY" not in page
    assert "getPaperLiquidityPools" in page
    assert "getPaperTreasury" in page
    assert "empty-live-compact" in page
    assert "demo-walkthrough" in page
    assert "PAPER MODE · NO EXECUTION" in scan
    assert "Advanced · provenance" in scan
    assert "getEconomicsStatus" in scan
    assert "econ-strip" in scan
    assert "USD → GBP" not in scan
    assert "Matchbook fee %" not in scan
    assert "Refresh interval" in scan
    assert "Auto 30s" not in scan
    assert "DEMO_LIQUIDITY_POOLS" not in treasury
    assert "PAPER CAPITAL" in pools
    assert "HYPOTHETICAL" in pools
    assert "excluded from solver" in pools
    assert "Paper Treasury" in pools
