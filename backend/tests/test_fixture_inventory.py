from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.application.collector import (
    CollectionReport,
    DiscoveredFixture,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.fixture_inventory import (
    FX_STATUS_KNOWN,
    FX_STATUS_MISSING,
    FX_STATUS_NOT_REQUIRED,
    InventoryComparisonStatus,
    InventoryMarket,
    assemble_fixture_inventory,
    solver_eligible_pair,
    _fx_status,
)
from sports_hedge.application.live_refresh import get_live_refresh_coordinator
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.domain.football import (
    CanonicalEvent,
    CanonicalMarket,
    CanonicalOutcome,
    CanonicalRunner,
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.normalization.identity import canonical_source_event_id
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import matchbook_polymarket_costs


KICKOFF = datetime(2026, 9, 12, 18, 45, tzinfo=UTC)
FRONTEND = __import__("pathlib").Path(__file__).resolve().parents[2] / "frontend"


def _event(venue: VenueName, source_id: str) -> CanonicalEvent:
    return CanonicalEvent(
        competition="Premier League",
        home_team="Tottenham",
        away_team="Everton",
        kickoff_utc=KICKOFF,
        source_venue=venue,
        source_event_id=source_id,
    )


def _market(
    venue: VenueName,
    *,
    family: MarketFamily,
    source_id: str,
    extra_time: bool = False,
    outcomes: list[CanonicalOutcome] | None = None,
    line: Decimal | None = None,
) -> CanonicalMarket:
    resolved = outcomes or [CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY]
    if family is MarketFamily.DRAW_NO_BET:
        push_possible = True
    elif line is not None:
        push_possible = line == line.to_integral_value()
    else:
        push_possible = False
    return CanonicalMarket(
        event=_event(venue, f"{venue.value}-event"),
        source_venue=venue,
        source_market_id=source_id,
        family=family,
        period=FootballPeriod.FULL_TIME,
        line=line,
        settlement=SettlementFingerprint(
            scope=SettlementScope.INCLUDING_EXTRA_TIME if extra_time else SettlementScope.REGULATION_TIME,
            period=FootballPeriod.FULL_TIME,
            line=line,
            push_possible=push_possible,
            extra_time_included=extra_time,
            penalties_included=False,
        ),
        runners=[
            CanonicalRunner(source_runner_id=f"{source_id}-{outcome.value}", outcome=outcome, label=outcome.value)
            for outcome in resolved
        ],
    )


def _inventory(market: CanonicalMarket, *, name: str) -> InventoryMarket:
    return InventoryMarket(
        venue=market.source_venue,
        source_event_id=market.event.source_event_id,
        source_market_id=market.source_market_id,
        raw_name=name,
        canonical=market,
    )


def test_inventory_keeps_matched_venue_only_settlement_and_unsupported_rows() -> None:
    match_result = _market(VenueName.MATCHBOOK, family=MarketFamily.MATCH_RESULT, source_id="mb-1x2")
    pm_match_result = _market(VenueName.POLYMARKET, family=MarketFamily.MATCH_RESULT, source_id="pm-1x2")
    totals = _market(
        VenueName.MATCHBOOK,
        family=MarketFamily.TOTAL_GOALS,
        source_id="mb-tg",
        outcomes=[CanonicalOutcome.OVER, CanonicalOutcome.UNDER],
        line=Decimal("2.5"),
    )
    correct = _market(
        VenueName.MATCHBOOK,
        family=MarketFamily.CORRECT_SCORE,
        source_id="mb-cs",
        outcomes=[CanonicalOutcome.OTHER, CanonicalOutcome.OTHER],
    )
    pm_correct = _market(
        VenueName.POLYMARKET,
        family=MarketFamily.CORRECT_SCORE,
        source_id="pm-cs",
        outcomes=[CanonicalOutcome.OTHER, CanonicalOutcome.OTHER],
    )
    ah_mb = _market(
        VenueName.MATCHBOOK,
        family=MarketFamily.ASIAN_HANDICAP,
        source_id="mb-ah",
        outcomes=[CanonicalOutcome.HOME, CanonicalOutcome.AWAY],
        line=Decimal("-0.5"),
    )
    ah_pm = _market(
        VenueName.POLYMARKET,
        family=MarketFamily.ASIAN_HANDICAP,
        source_id="pm-ah",
        extra_time=True,
        outcomes=[CanonicalOutcome.HOME, CanonicalOutcome.AWAY],
        line=Decimal("-0.5"),
    )

    rows = assemble_fixture_inventory(
        [
            _inventory(match_result, name="Match Odds"),
            _inventory(totals, name="Total Goals 2.5"),
            _inventory(correct, name="Correct Score"),
            _inventory(ah_mb, name="Asian Handicap -0.5"),
        ],
        [
            _inventory(pm_match_result, name="Match Result"),
            _inventory(pm_correct, name="Correct Score"),
            _inventory(ah_pm, name="AH -0.5 including extra time"),
        ],
    )
    statuses = {row.display_name: row.comparison_status for row in rows}
    assert InventoryComparisonStatus.MATCHED_EQUIVALENT in statuses.values()
    assert InventoryComparisonStatus.VENUE_ONLY in statuses.values()
    assert InventoryComparisonStatus.SETTLEMENT_MISMATCH in statuses.values()
    assert InventoryComparisonStatus.UNSUPPORTED_OUTCOME_MODEL in statuses.values()
    assert all(not row.entered_solver for row in rows)
    ah_row = next(row for row in rows if row.comparison_status is InventoryComparisonStatus.SETTLEMENT_MISMATCH)
    assert "settlement_mismatch" in ah_row.match_reasons
    matcher = MarketMatcher()
    assert matcher.match(ah_mb, ah_pm).matched is False
    assert "settlement_mismatch" in matcher.match(ah_mb, ah_pm).reasons
    assert solver_eligible_pair(correct, pm_correct, matcher.match(correct, pm_correct)) is False


def test_unnormalized_market_is_unsupported_family_not_hidden() -> None:
    rows = assemble_fixture_inventory(
        [
            InventoryMarket(
                venue=VenueName.MATCHBOOK,
                source_event_id="1001",
                source_market_id="2999",
                raw_name="Novelty unsupported market",
                normalize_error="Unsupported Matchbook market: Novelty unsupported market",
            )
        ],
        [],
    )
    assert len(rows) == 1
    assert rows[0].comparison_status is InventoryComparisonStatus.UNSUPPORTED_FAMILY
    assert rows[0].matchbook is not None
    assert rows[0].matchbook.source_market_id == "2999"


class RichMatchbook:
    def __init__(self, *, in_running: bool = False) -> None:
        self.in_running = in_running
        self.list_markets_calls: list[str] = []

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {
            "events": [
                {
                    "id": 5001,
                    "name": "Tottenham vs Everton",
                    "start": KICKOFF.isoformat(),
                    "competition-name": "Premier League",
                    "status": "open" if not self.in_running else "in-play",
                    "in-running-flag": self.in_running,
                }
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_markets_calls.append(str(event_id))
        return {
            "markets": [
                {
                    "id": 6101,
                    "name": "Match Odds",
                    "runners": [
                        {"id": 1, "name": "Tottenham", "prices": [{"side": "back", "odds": "2.10", "available-amount": "80"}]},
                        {"id": 2, "name": "Draw", "prices": [{"side": "back", "odds": "3.40", "available-amount": "80"}]},
                        {"id": 3, "name": "Everton", "prices": [{"side": "back", "odds": "3.60", "available-amount": "80"}]},
                    ],
                },
                {
                    "id": 6102,
                    "name": "Total Goals 2.5",
                    "runners": [
                        {"id": 11, "name": "Over 2.5", "prices": [{"side": "back", "odds": "1.90", "available-amount": "50"}]},
                        {"id": 12, "name": "Under 2.5", "prices": [{"side": "back", "odds": "1.95", "available-amount": "50"}]},
                    ],
                },
                {
                    "id": 6103,
                    "name": "Correct Score",
                    "runners": [
                        {"id": 21, "name": "1-0", "prices": []},
                        {"id": 22, "name": "2-0", "prices": []},
                    ],
                },
                {
                    "id": 6104,
                    "name": "Asian Handicap -0.5",
                    "line": "-0.5",
                    "runners": [
                        {"id": 31, "name": "Tottenham", "prices": [{"side": "back", "odds": "1.85", "available-amount": "40"}]},
                        {"id": 32, "name": "Everton", "prices": [{"side": "back", "odds": "2.05", "available-amount": "40"}]},
                    ],
                },
            ]
        }


class RichPolymarket:
    def __init__(self) -> None:
        self.book_calls: list[str] = []

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return [
            {
                "id": "pm-tot-eve",
                "title": "Tottenham vs Everton",
                "startTime": KICKOFF.isoformat(),
                "competition": "Premier League",
            }
        ]

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return [
            {
                "id": "pm-1x2",
                "question": "Match result?",
                "sportsMarketType": "moneyline",
                "outcomes": '["Tottenham", "Draw", "Everton"]',
                "clobTokenIds": '["h", "d", "a"]',
                "description": "Resolves based on 90 minutes of regulation time.",
            },
            {
                "id": "pm-cs",
                "question": "Correct score?",
                "sportsMarketType": "correct score",
                "outcomes": '["1-0", "2-0"]',
                "clobTokenIds": '["cs1", "cs2"]',
                "description": "Resolves based on 90 minutes of regulation time.",
            },
            {
                "id": "pm-ah",
                "question": "Asian handicap -0.5",
                "sportsMarketType": "handicap",
                "line": "-0.5",
                "outcomes": '["Tottenham", "Everton"]',
                "clobTokenIds": '["ah-h", "ah-a"]',
                "description": "Resolves including extra time.",
            },
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
        self.book_calls.append(token)
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        return {
            "asset_id": token,
            "timestamp": now_ms - 150,
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.42", "size": "100"}],
        }


@pytest.mark.asyncio
async def test_collector_inventories_all_families_and_keeps_unsupported_out_of_solver() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    matchbook = RichMatchbook()
    polymarket = RichPolymarket()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        paper_scan=PaperScanService(intelligence),
    )
    try:
        report = await collector.collect_and_scan(
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=[
                FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75")),
                FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1")),
            ],
            maximum_execution_risk=100,
        )
        fixture = report.discovered_fixtures[0]
        assert fixture.canonical_event_id.startswith("evt:")
        assert fixture.canonical_event_id == canonical_source_event_id(
            CanonicalEvent(
                competition=fixture.competition,
                home_team=fixture.home_team,
                away_team=fixture.away_team,
                kickoff_utc=fixture.kickoff_utc,
                source_venue=VenueName.MATCHBOOK,
                source_event_id=fixture.source_event_id,
            )
        )
        markets = report.fixture_markets[fixture.canonical_event_id]
        statuses = {row.comparison_status for row in markets}
        assert InventoryComparisonStatus.MATCHED_EQUIVALENT in statuses
        assert InventoryComparisonStatus.VENUE_ONLY in statuses
        assert InventoryComparisonStatus.SETTLEMENT_MISMATCH in statuses
        assert InventoryComparisonStatus.UNSUPPORTED_OUTCOME_MODEL in statuses
        assert fixture.discovered_market_count == len(markets)
        assert fixture.matched_equivalent_count >= 1
        assert all(not row.entered_solver for row in markets if row.comparison_status is InventoryComparisonStatus.UNSUPPORTED_OUTCOME_MODEL)
        assert all(decision.canonical_market_id for decision in report.paper_decisions)
        for decision in report.paper_decisions:
            assert "unsupported_outcome_model" not in decision.rejection_reasons
            assert "noncanonical_outcome_space" not in decision.rejection_reasons
        families = {row.family for row in markets if row.family}
        assert "match_result" in families
        assert "total_goals" in families
        assert "correct_score" in families
        assert "asian_handicap" in families
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_missing_costs_and_fx_fail_closed_on_inventory() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    collector = ReadOnlyCrossVenueCollector(
        matchbook=RichMatchbook(),
        polymarket=RichPolymarket(),
        paper_scan=PaperScanService(intelligence),
    )
    try:
        report = await collector.collect_and_scan(maximum_execution_risk=100)
        fixture = report.discovered_fixtures[0]
        markets = report.fixture_markets[fixture.canonical_event_id]
        equivalent = [
            row
            for row in markets
            if row.comparison_status
            in {
                InventoryComparisonStatus.MATCHED_EQUIVALENT,
                InventoryComparisonStatus.MISSING_COSTS,
                InventoryComparisonStatus.MISSING_FX,
            }
            and row.entered_solver
        ]
        assert equivalent
        assert any(
            row.comparison_status
            in {InventoryComparisonStatus.MISSING_COSTS, InventoryComparisonStatus.MISSING_FX}
            for row in equivalent
        )
        assert all(not row.solver_is_arbitrage for row in equivalent)
        assert any(row.matchbook and row.matchbook.fee_status == "missing" for row in markets)
        assert any(
            row.matchbook and row.matchbook.fx_status == FX_STATUS_NOT_REQUIRED for row in markets
        )
        assert any(
            row.polymarket and row.polymarket.fx_status == FX_STATUS_MISSING for row in markets
        )
    finally:
        repository.close()


def test_gbp_fx_status_is_not_required_regardless_of_snapshots() -> None:
    usd = FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))
    gbp = FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1"))
    assert _fx_status("GBP", None) == FX_STATUS_NOT_REQUIRED
    assert _fx_status("gbp", []) == FX_STATUS_NOT_REQUIRED
    assert _fx_status("GBP", [usd]) == FX_STATUS_NOT_REQUIRED
    assert _fx_status("GBP", [gbp, usd]) == FX_STATUS_NOT_REQUIRED
    assert _fx_status("USD", None) == FX_STATUS_MISSING
    assert _fx_status("USD", []) == FX_STATUS_MISSING
    assert _fx_status("USD", [gbp]) == FX_STATUS_MISSING
    assert _fx_status("USD", [usd]) == FX_STATUS_KNOWN
    assert _fx_status(None, None) is None


@pytest.mark.asyncio
async def test_pre_match_and_in_play_share_the_same_inventory_read_model() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    try:
        pre = await ReadOnlyCrossVenueCollector(
            matchbook=RichMatchbook(in_running=False),
            polymarket=RichPolymarket(),
            paper_scan=PaperScanService(intelligence),
        ).collect_and_scan(maximum_execution_risk=100)
        live = await ReadOnlyCrossVenueCollector(
            matchbook=RichMatchbook(in_running=True),
            polymarket=RichPolymarket(),
            paper_scan=PaperScanService(intelligence),
        ).collect_and_scan(maximum_execution_risk=100)
        pre_fixture = pre.discovered_fixtures[0]
        live_fixture = live.discovered_fixtures[0]
        assert pre_fixture.in_running is False
        assert live_fixture.in_running is True
        assert live_fixture.fixture_status == "in-play"
        assert set(row.comparison_status for row in pre.fixture_markets[pre_fixture.canonical_event_id]) == set(
            row.comparison_status for row in live.fixture_markets[live_fixture.canonical_event_id]
        )
        assert pre_fixture.canonical_event_id == live_fixture.canonical_event_id
    finally:
        repository.close()


def test_operations_fixture_route_uses_canonical_event_id() -> None:
    coordinator = get_live_refresh_coordinator()
    fixture = DiscoveredFixture(
        source_event_id="5001",
        canonical_event_id="evt:fixture-route-test",
        home_team="Tottenham",
        away_team="Everton",
        competition="Premier League",
        kickoff_utc=KICKOFF,
        last_seen_at=KICKOFF,
        discovered_market_count=2,
        matched_equivalent_count=1,
    )
    report = CollectionReport(
        started_at=KICKOFF,
        completed_at=KICKOFF,
        discovered_fixtures=[fixture],
        fixture_markets={"evt:fixture-route-test": []},
    )
    coordinator.record_report(report)
    client = TestClient(app)
    missing = client.get("/operations/fixtures/unknown")
    assert missing.status_code == 404
    found = client.get("/operations/fixtures/evt:fixture-route-test")
    assert found.status_code == 200
    body = found.json()
    assert body["fixture"]["canonical_event_id"] == "evt:fixture-route-test"
    assert body["execution_enabled"] is False
    assert body["paper_mode"] == "paper"
    by_source = client.get("/operations/fixtures/5001")
    assert by_source.status_code == 200
    health = client.get("/health")
    assert health.json()["execution_enabled"] is False
    coordinator.reset()


def test_fixture_ui_routes_by_canonical_id_and_renders_inventory_states() -> None:
    discovered = (FRONTEND / "components" / "discovered-fixtures.tsx").read_text(encoding="utf-8")
    page = (FRONTEND / "app" / "arbitrage" / "fixtures" / "[eventId]" / "page.tsx").read_text(
        encoding="utf-8"
    )
    workspace = (FRONTEND / "components" / "fixture-inventory.tsx").read_text(encoding="utf-8")
    api = (FRONTEND / "lib" / "api.ts").read_text(encoding="utf-8")
    assert "fixtureHref" in discovered
    assert "canonical_event_id" in discovered
    assert "/operations/fixtures/" in api
    assert "getFixtureDetail" in page
    for token in (
        "matched_equivalent",
        "venue_only",
        "settlement_mismatch",
        "unsupported_outcome_model",
        "PAPER MODE",
        "not in solver",
    ):
        assert token in workspace or token in (FRONTEND / "lib" / "fixture-inventory-display.ts").read_text(
            encoding="utf-8"
        )
    assert "place_order" not in (FRONTEND / "app" / "arbitrage" / "fixtures" / "[eventId]" / "page.tsx").read_text(
        encoding="utf-8"
    )


def test_phase1_operations_router_has_no_execution_surface() -> None:
    source = (__import__("pathlib").Path(__file__).resolve().parents[1] / "src" / "sports_hedge" / "api" / "operations.py").read_text(
        encoding="utf-8"
    )
    assert "place_order" not in source
    assert "cancel_order" not in source
    assert "@router.get" in source
    assert "@router.post" not in source
