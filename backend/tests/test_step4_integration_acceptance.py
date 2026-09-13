"""Step 4: compose discovery, backend economics and paper liquidity seams.

This is not a live-provider check. Venue clients are fixture/synthetic.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.paper import (
    get_fx_rate_service,
    get_matchbook_account_fee_store,
    get_paper_liquidity_repository,
    get_venue_cost_resolver,
)
from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.target_competitions import UNMATCHED_POLYMARKET_COVERAGE
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.resolver import VenueCostResolver
from sports_hedge.fx.models import PublishedFxClose
from sports_hedge.fx.repository import SqliteFxRateRepository
from sports_hedge.fx.service import FxRateService
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.persistence.liquidity import SqlitePaperLiquidityRepository
from sports_hedge.persistence.matchbook_account_fee import SqliteMatchbookAccountFeeStore
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient
from test_matchbook_event_discovery import (
    FOOTBALL_SPORT_ID,
    MatchbookDiscoveryTransport,
    _settings as matchbook_discovery_settings,
)
from test_paper_scan_pipeline import OBSERVED, matchbook_payloads, polymarket_payloads
from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)


KICKOFF = datetime(2026, 9, 12, 16, 30, tzinfo=UTC)
FRONTEND = Path(__file__).resolve().parents[2] / "frontend"
REPO = Path(__file__).resolve().parents[2]


def _btts_market(market_id: int) -> dict[str, Any]:
    return {
        "id": market_id,
        "name": "Both Teams To Score",
        "runners": [
            {
                "id": market_id * 10 + 1,
                "name": "Yes",
                "prices": [
                    {"side": "back", "odds": "2.20", "available-amount": "100"},
                    {"side": "lay", "odds": "2.22", "available-amount": "100"},
                ],
            },
            {
                "id": market_id * 10 + 2,
                "name": "No",
                "prices": [
                    {"side": "back", "odds": "1.80", "available-amount": "100"},
                    {"side": "lay", "odds": "1.82", "available-amount": "100"},
                ],
            },
        ],
    }


def _mb_event(event_id: int, name: str, *, sport: str, competition: str) -> dict[str, Any]:
    return {
        "id": event_id,
        "name": name,
        "start": KICKOFF.isoformat(),
        "sport-id": FOOTBALL_SPORT_ID if sport == "Football" else 9,
        "sport-name": sport,
        "competition-name": competition,
        "status": "open",
    }


class MixedDiscoveryMatchbook:
    """First 100 mixed-sport rows plus PL / Championship / La Liga football."""

    def __init__(self) -> None:
        self.list_markets_calls: list[str] = []

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        tennis = [
            _mb_event(
                10_000 + index,
                f"Player {index} vs Player B",
                sport="Tennis",
                competition="ATP",
            )
            for index in range(100)
        ]
        football = [
            _mb_event(8801, "Newcastle United vs Chelsea", sport="Football", competition="Premier League"),
            _mb_event(8802, "Leeds United vs Leicester City", sport="Football", competition="EFL Championship"),
            _mb_event(8803, "Athletic Bilbao vs Elche", sport="Football", competition="La Liga"),
        ]
        return {"events": tennis + football, "truncated": False}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_markets_calls.append(str(event_id))
        return {"markets": [_btts_market(int(event_id))]}


class MultiSeriesPolymarket:
    """EPL + La Liga identity coverage. Championship series is queried empty."""

    def __init__(self) -> None:
        self.list_events_filters: list[dict[str, Any]] = []

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        self.list_events_filters.append(dict(filters))
        return [
            {
                "id": "pm-epl-1",
                "title": "Newcastle United vs Chelsea",
                "startTime": KICKOFF.isoformat(),
                "competition": "Premier League",
                "series": [{"id": "10188", "title": "Premier League"}],
            },
            {
                "id": "pm-lal-athletic",
                "title": "Athletic Club vs Elche CF",
                "startTime": KICKOFF.isoformat(),
                "competition": "La Liga",
                "series": [{"id": "10193", "title": "La Liga"}],
            },
        ]

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del filters
        suffix = str(event_id)
        return [
            {
                "id": f"pm-market-{suffix}",
                "question": "Both teams to score?",
                "sportsMarketType": "both teams to score",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["yes-token", "no-token"]',
                "description": "Resolves based on 90 minutes of regulation time.",
                "feesEnabled": False,
            }
        ]

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id, filters
        token = str(outcome_id)
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        if token == "yes-token":
            return {
                "asset_id": token,
                "timestamp": now_ms - 200,
                "bids": [{"price": "0.49", "size": "250"}],
                "asks": [{"price": "0.51", "size": "250"}],
            }
        return {
            "asset_id": token,
            "timestamp": now_ms - 180,
            "bids": [{"price": "0.41", "size": "300"}],
            "asks": [{"price": "0.43", "size": "300"}],
        }


def _backend_fx(gbp_per_usd: Decimal = Decimal("0.50000000")) -> FxRateService:
    fx = FxRateService(SqliteFxRateRepository())
    fx.persist_ecb_closes(
        [
            PublishedFxClose(
                currency="USD",
                gbp_per_unit=gbp_per_usd,
                source_date=date(2026, 9, 11),
                retrieved_at=datetime(2026, 9, 11, 16, tzinfo=UTC),
                source="ecb_eurofxref",
                source_id="ecb:2026-09-11:USD",
            )
        ]
    )
    return fx


@pytest.mark.asyncio
async def test_matchbook_client_still_pages_past_first_100_mixed_events() -> None:
    transport = MatchbookDiscoveryTransport()
    settings = matchbook_discovery_settings()
    import httpx

    mock = httpx.MockTransport(transport.handler)
    async with httpx.AsyncClient(transport=mock, base_url=settings.matchbook_base_url) as http:
        venue = MatchbookClient(settings, client=http, clock=lambda: datetime(2026, 9, 12, 17, tzinfo=UTC))
        payload = await venue.list_events()
    ids = [item["id"] for item in payload["events"]]
    assert transport.event_offsets == [0, 100]
    assert ids[100] == 8801
    assert {8801, 8802, 8803}.issubset(set(ids))
    assert payload["truncated"] is False
    assert not hasattr(venue, "place_order")


@pytest.mark.asyncio
async def test_collector_composes_scoped_discovery_backend_fx_and_native_pools() -> None:
    intelligence = MarketIntelligenceService(SqliteMarketIntelligenceRepository())
    liquidity = SqlitePaperLiquidityRepository()
    liquidity.update_available(
        {
            VenueName.MATCHBOOK: Decimal("5"),
            VenueName.POLYMARKET: Decimal("10"),
            VenueName.SMARKETS: Decimal("5000"),
        }
    )
    matchbook = MixedDiscoveryMatchbook()
    polymarket = MultiSeriesPolymarket()
    service = PaperScanService(
        intelligence,
        settings=Settings(max_slippage_bps=0, fx_spread_bps=0),
        fx_service=_backend_fx(),
        cost_resolver=VenueCostResolver(),
        liquidity=liquidity,
    )
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        paper_scan=service,
    )
    report = await collector.collect_and_scan(
        maximum_execution_risk=100,
        polymarket_queried_series_ids=Settings().resolved_polymarket_series_ids(),
    )

    discovered = {item.source_event_id: item for item in report.discovered_fixtures}
    assert set(discovered) == {"8801", "8802", "8803"}
    assert not any(item.source_event_id.startswith("100") for item in report.discovered_fixtures)
    assert Settings().resolved_polymarket_series_ids() == ["10188", "10355", "10193"]

    epl = discovered["8801"]
    assert epl.polymarket_matched is True
    assert epl.target_competition_code == "premier_league"
    assert epl.no_comparison_reason is None
    assert epl.solver_is_arbitrage is True

    championship = discovered["8802"]
    assert championship.target_competition_code == "championship"
    assert championship.polymarket_matched is False
    assert championship.no_comparison_reason == UNMATCHED_POLYMARKET_COVERAGE

    la_liga = discovered["8803"]
    assert la_liga.target_competition_code == "la_liga"
    assert la_liga.polymarket_matched is True
    assert la_liga.home_team == "Athletic Bilbao"

    assert report.paper_decisions
    for decision in report.paper_decisions:
        usd = next(item for item in decision.fx_snapshots if item.currency == "USD")
        assert usd.source == "ecb_eurofxref"
        assert usd.gbp_per_unit == Decimal("0.50000000")
        assert {item.venue.value for item in decision.venue_costs} == {"matchbook", "polymarket"}
        sources = {item.venue.value: item.source for item in decision.venue_costs}
        assert sources["matchbook"].startswith("venue_cost_registry")
        assert sources["polymarket"].startswith("polymarket_fee_schedule")
        used_pm = sum(
            (
                stake.stake
                for stake in (decision.depth_scan.solution.stakes if decision.depth_scan else [])
                if stake.venue is VenueName.POLYMARKET
            ),
            Decimal("0"),
        )
        assert used_pm <= Decimal("5") + Decimal("0.0000001")
        used_mb = sum(
            (
                stake.stake
                for stake in (decision.depth_scan.solution.stakes if decision.depth_scan else [])
                if stake.venue is VenueName.MATCHBOOK
            ),
            Decimal("0"),
        )
        assert used_mb <= Decimal("5") + Decimal("0.0000001")

    limits = liquidity.get(
        gbp_per_unit={"GBP": Decimal("1"), "USD": Decimal("0.5")},
        fx_source="ecb_eurofxref",
    ).solver_gbp_limits({"GBP": Decimal("1"), "USD": Decimal("0.5")})
    assert VenueName.SMARKETS not in limits
    assert set(matchbook.list_markets_calls) == {"8801", "8802", "8803"}


def test_live_collect_and_economics_status_compose_on_http_surface() -> None:
    fx = _backend_fx()
    liquidity = SqlitePaperLiquidityRepository()
    fee_store = SqliteMatchbookAccountFeeStore()
    costs = VenueCostResolver(matchbook_fee_store=fee_store)
    app.dependency_overrides[get_fx_rate_service] = lambda: fx
    app.dependency_overrides[get_paper_liquidity_repository] = lambda: liquidity
    app.dependency_overrides[get_matchbook_account_fee_store] = lambda: fee_store
    app.dependency_overrides[get_venue_cost_resolver] = lambda: costs
    client = TestClient(app)
    try:
        rejected = client.post(
            "/paper/collect",
            json={
                "fx_snapshots": [{"currency": "USD", "gbp_per_unit": "1", "source": "dashboard_input"}],
                "venue_costs": [{"venue": "matchbook", "profit_haircut_rate": "0"}],
                "fee_snapshots": [{"venue": "matchbook", "profit_haircut_rate": "0.02", "source": "ui"}],
            },
        )
        assert rejected.status_code == 422

        status = client.get("/paper/economics-status")
        assert status.status_code == 200
        body = status.json()
        assert body["data_kind"] == "backend_resolved"
        usd = next(row for row in body["fx"] if row["currency"] == "USD")
        assert usd["primary_source"] == "ecb_eurofxref"
        assert "missing_fx_rate:USD" not in body["issues"]
        assert "stale_fx_rate:USD" not in body["issues"]
        venues = {row["venue"] for row in body["venue_costs"]}
        assert "matchbook" in venues
        assert "polymarket" not in venues
        assert body["matchbook_fee"]["label"] == "2.00% net-profit commission"
        assert body["polymarket_fee_policy"]["catalog_seeded"] is False
        assert body["polymarket_fee_policy"]["resolution"] == "per_market_clob_metadata"

        listed = client.get("/paper/liquidity-pools").json()
        by_venue = {pool["venue"]: pool for pool in listed["pools"]}
        assert by_venue["matchbook"]["native_currency"] == "GBP"
        assert by_venue["polymarket"]["native_currency"] == "USD"
        assert by_venue["smarkets"]["included_in_solver"] is False
        assert "combined_cash" not in str(listed)
        client.post(
            "/paper/liquidity-pools",
            json={"pools": [{"venue": "matchbook", "available": "42"}]},
        )
        again = client.get("/paper/liquidity-pools").json()
        assert Decimal(next(p["available"] for p in again["pools"] if p["venue"] == "matchbook")) == Decimal("42")

        health = client.get("/health")
        assert health.json()["execution_enabled"] is False
        assert health.json()["mode"] == "paper"
    finally:
        app.dependency_overrides.clear()
        liquidity.close()


def test_economics_status_fail_closes_missing_and_stale_required_fx() -> None:
    empty = FxRateService(SqliteFxRateRepository())
    stale = FxRateService(SqliteFxRateRepository())
    stale.persist_ecb_closes(
        [
            PublishedFxClose(
                currency="USD",
                gbp_per_unit=Decimal("0.75"),
                source_date=date(2020, 1, 2),
                retrieved_at=datetime(2020, 1, 2, 16, tzinfo=UTC),
                source="ecb_eurofxref",
                source_id="ecb:2020-01-02:USD",
            )
        ]
    )
    client = TestClient(app)
    try:
        app.dependency_overrides[get_fx_rate_service] = lambda: empty
        missing = client.get("/paper/economics-status").json()
        assert "missing_fx_rate:USD" in missing["issues"]

        app.dependency_overrides[get_fx_rate_service] = lambda: stale
        warned = client.get("/paper/economics-status").json()
        assert any(row["currency"] == "USD" for row in warned["fx"])
        assert "stale_fx_rate:USD" in warned["issues"]
    finally:
        app.dependency_overrides.clear()


def test_paper_scan_fail_closes_stale_fx_on_the_same_path_as_liquidity_sizing() -> None:
    intelligence = MarketIntelligenceService(SqliteMarketIntelligenceRepository())
    liquidity = SqlitePaperLiquidityRepository()
    liquidity.update_available(
        {VenueName.MATCHBOOK: Decimal("100"), VenueName.POLYMARKET: Decimal("100")}
    )
    fx = FxRateService(SqliteFxRateRepository())
    fx.persist_ecb_closes(
        [
            PublishedFxClose(
                currency="USD",
                gbp_per_unit=Decimal("0.75"),
                source_date=date(2020, 1, 2),
                retrieved_at=datetime(2020, 1, 2, 16, tzinfo=UTC),
                source="ecb_eurofxref",
                source_id="ecb:2020-01-02:USD",
            )
        ]
    )
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=OBSERVED, quote_age_ms=120
    )
    polymarket = PolymarketObservationBuilder().build(
        pm_event, pm_market, pm_books, observed_at=OBSERVED, quote_age_ms=180
    )
    decision = PaperScanService(
        intelligence,
        fx_service=fx,
        cost_resolver=VenueCostResolver(),
        liquidity=liquidity,
    ).scan_pair(matchbook, polymarket, maximum_execution_risk=100)
    assert decision.eligible_for_paper_simulation is False
    assert any(reason.startswith("stale_fx_rate") for reason in decision.rejection_reasons)


def test_operator_console_keeps_steps_1_to_3_console_contract() -> None:
    scan = (FRONTEND / "components" / "run-paper-scan.tsx").read_text(encoding="utf-8")
    page = (FRONTEND / "app" / "page.tsx").read_text(encoding="utf-8")
    api = (FRONTEND / "lib" / "api.ts").read_text(encoding="utf-8")
    pools = (FRONTEND / "components" / "liquidity-pools.tsx").read_text(encoding="utf-8")
    collect_type = api.split("export type PaperCollectionRequest")[1].split("export type")[0]
    assert "fx_snapshots" not in collect_type
    assert "fee_snapshots" not in collect_type
    assert "venue_costs" not in collect_type
    assert "USD → GBP" not in scan
    assert "Matchbook commission %" in scan
    assert "operator/account assumption" in scan
    assert "per-market CLOB metadata" in scan
    assert "fx_snapshots" not in collect_type
    assert "Min net arb %" in scan
    assert "Max risk" in scan
    assert "Optional capital limit" in scan
    assert "Advanced · FX / fees / provenance" in scan
    assert "econ-strip" in scan
    assert "getEconomicsStatus" in scan
    assert "PAPER MODE · NO EXECUTION" in scan
    assert "demo-walkthrough" in page
    assert "Demo walkthrough · not live operations" in page
    assert "DEMO_LIQUIDITY_POOLS" not in page
    assert "liveConnected" in page
    assert "excluded from solver" in pools
    assert "Operator demo" not in (FRONTEND / "components" / "sidebar.tsx").read_text(encoding="utf-8")
    assert not hasattr(MatchbookClient, "place_order")
    assert not hasattr(PolymarketClient, "place_order")
    for path in (REPO / "backend" / "src" / "sports_hedge" / "venues").glob("*.py"):
        source = path.read_text(encoding="utf-8")
        for token in ("place_order", "cancel_order", "sign_wallet", "submit_order"):
            assert f"def {token}" not in source
