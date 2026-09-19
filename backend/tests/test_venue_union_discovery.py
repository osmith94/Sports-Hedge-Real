from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.target_competitions import polymarket_series_ids_for_targets
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import profit_commission_cost
from sports_hedge.fees.kalshi import kalshi_cost_from_series


KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
REGULATION = "Resolves on 90 minutes of regulation time. Extra time and penalties do not count."
KALSHI_SERIES = {
    "ticker": "KXEPLGAME",
    "title": "Premier League",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "settlement_sources": [{"name": "Opta"}],
}


class FailingMatchbook:
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        raise RuntimeError("MATCHBOOK_USERNAME and MATCHBOOK_PASSWORD are required")

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        raise AssertionError("Matchbook markets must not be required for PM↔Kalshi")


class NewcastlePolymarket:
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return [
            {
                "id": "pm-ncl-che",
                "title": "Newcastle United vs Chelsea",
                "startTime": KICKOFF.isoformat(),
                "competition": "Premier League",
                "series": [{"id": 10188, "title": "Premier League"}],
            }
        ]

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return [
            {
                "id": "pm-btts",
                "question": "Both teams to score?",
                "sportsMarketType": "both teams to score",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["yes-token", "no-token"]',
                "description": REGULATION,
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
                "timestamp": now_ms - 120,
                "bids": [{"price": "0.49", "size": "250"}],
                "asks": [{"price": "0.51", "size": "250"}],
            }
        return {
            "asset_id": token,
            "timestamp": now_ms - 110,
            "bids": [{"price": "0.41", "size": "300"}],
            "asks": [{"price": "0.43", "size": "300"}],
        }


class NewcastleKalshi:
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {
            "events": [
                {
                    "event_ticker": "KXEPLGAME-26SEP20NEWCHE",
                    "series_ticker": "KXEPLGAME",
                    "title": "Newcastle United vs Chelsea",
                    "category": "Sports",
                    "strike_date": KICKOFF.isoformat(),
                    "markets": [
                        {
                            "ticker": "KXEPLGAME-26SEP20NEWCHE-BTTS",
                            "event_ticker": "KXEPLGAME-26SEP20NEWCHE",
                            "title": "Both Teams To Score",
                            "yes_sub_title": "Yes",
                            "rules_primary": REGULATION,
                        }
                    ],
                }
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": []}

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, outcome_id, filters
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "100.00"]],
                "no_dollars": [["0.49", "200.00"]],
            }
        }

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        del series_ticker
        return KALSHI_SERIES


@pytest.mark.asyncio
async def test_polymarket_kalshi_discovery_does_not_require_matchbook() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    collector = ReadOnlyCrossVenueCollector(
        matchbook=FailingMatchbook(),
        polymarket=NewcastlePolymarket(),
        kalshi=NewcastleKalshi(),
        paper_scan=PaperScanService(intelligence),
    )
    try:
        report = await collector.collect_and_scan(
            venue_costs=[
                profit_commission_cost(VenueName.POLYMARKET, "0"),
                kalshi_cost_from_series(KALSHI_SERIES, captured_at=datetime.now(UTC)),
            ],
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"), source="paper_demo_fx_snapshot")],
            maximum_execution_risk=100,
        )
        assert report.venue_health["matchbook"] == "unavailable"
        assert report.venue_health["polymarket"] == "ok"
        assert report.venue_health["kalshi"] == "ok"
        assert report.pair_counts["polymarket_kalshi"] == 1
        assert report.pair_counts["matchbook_polymarket"] == 0
        assert report.matched_event_pairs == 1
        fixture = next(item for item in report.discovered_fixtures if item.kalshi_matched)
        assert fixture.matchbook_matched is False
        assert fixture.polymarket_matched is True
        assert fixture.kalshi_matched is True
        assert all(
            VenueName.MATCHBOOK not in decision.execution_modes
            for decision in report.paper_decisions
        )
    finally:
        repository.close()


class SplitSeriesPolymarket:
    """Same fixture split across moneyline + BTTS source events."""

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return [
            {
                "id": "pm-ncl-che-ml",
                "title": "Newcastle United vs Chelsea",
                "startTime": KICKOFF.isoformat(),
                "competition": "Premier League",
                "series": [{"id": 10188, "title": "Premier League"}],
            },
            {
                "id": "pm-ncl-che-btts",
                "title": "Newcastle United vs Chelsea",
                "startTime": KICKOFF.isoformat(),
                "competition": "Premier League",
                "series": [{"id": 10188, "title": "Premier League"}],
            },
        ]

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del filters
        if str(event_id) == "pm-ncl-che-ml":
            return [
                {
                    "id": "pm-home-win",
                    "question": "Will Newcastle United win on 2026-09-20?",
                    "sportsMarketType": "moneyline",
                    "outcomes": '["Yes", "No"]',
                    "clobTokenIds": '["yes-home", "no-home"]',
                    "description": REGULATION,
                }
            ]
        return [
            {
                "id": "pm-btts",
                "question": "Both teams to score?",
                "sportsMarketType": "both teams to score",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["yes-token", "no-token"]',
                "description": REGULATION,
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
        asks = {"price": "0.51", "size": "250"} if token.startswith("yes") else {"price": "0.43", "size": "300"}
        bids = {"price": "0.49", "size": "250"} if token.startswith("yes") else {"price": "0.41", "size": "300"}
        return {"asset_id": token, "timestamp": now_ms - 120, "bids": [bids], "asks": [asks]}


class SplitSeriesKalshi:
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {
            "events": [
                {
                    "event_ticker": "KXEPLGAME-26SEP20NEWCHE",
                    "series_ticker": "KXEPLGAME",
                    "title": "Newcastle United vs Chelsea",
                    "category": "Sports",
                    "strike_date": KICKOFF.isoformat(),
                    "markets": [
                        {
                            "ticker": "KXEPLGAME-26SEP20NEWCHE-NEW",
                            "event_ticker": "KXEPLGAME-26SEP20NEWCHE",
                            "title": "Newcastle United vs Chelsea",
                            "yes_sub_title": "Newcastle United",
                            "rules_primary": REGULATION,
                        },
                        {
                            "ticker": "KXEPLGAME-26SEP20NEWCHE-DRAW",
                            "event_ticker": "KXEPLGAME-26SEP20NEWCHE",
                            "title": "Newcastle United vs Chelsea",
                            "yes_sub_title": "Draw",
                            "rules_primary": REGULATION,
                        },
                        {
                            "ticker": "KXEPLGAME-26SEP20NEWCHE-CHE",
                            "event_ticker": "KXEPLGAME-26SEP20NEWCHE",
                            "title": "Newcastle United vs Chelsea",
                            "yes_sub_title": "Chelsea",
                            "rules_primary": REGULATION,
                        },
                    ],
                },
                {
                    "event_ticker": "KXEPLBTTS-26SEP20NEWCHE",
                    "series_ticker": "KXEPLBTTS",
                    "title": "Newcastle United vs Chelsea",
                    "category": "Sports",
                    "strike_date": KICKOFF.isoformat(),
                    "markets": [
                        {
                            "ticker": "KXEPLBTTS-26SEP20NEWCHE",
                            "event_ticker": "KXEPLBTTS-26SEP20NEWCHE",
                            "title": "Both Teams To Score",
                            "yes_sub_title": "Yes",
                            "rules_primary": REGULATION,
                        }
                    ],
                },
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": []}

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, outcome_id, filters
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.40", "100.00"]],
                "no_dollars": [["0.49", "200.00"]],
            }
        }

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        return {**KALSHI_SERIES, "ticker": series_ticker}


@pytest.mark.asyncio
async def test_split_pm_kalshi_events_cluster_and_btts_enters_solver() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    collector = ReadOnlyCrossVenueCollector(
        matchbook=FailingMatchbook(),
        polymarket=SplitSeriesPolymarket(),
        kalshi=SplitSeriesKalshi(),
        paper_scan=PaperScanService(intelligence),
    )
    try:
        report = await collector.collect_and_scan(
            venue_costs=[
                profit_commission_cost(VenueName.POLYMARKET, "0"),
                kalshi_cost_from_series(KALSHI_SERIES, captured_at=datetime.now(UTC)),
            ],
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"), source="paper_demo_fx_snapshot")],
            maximum_execution_risk=100,
        )
        assert report.pair_counts["polymarket_kalshi"] == 1
        fixtures = [item for item in report.discovered_fixtures if item.polymarket_matched and item.kalshi_matched]
        assert len(fixtures) == 1
        rows = report.fixture_markets[fixtures[0].canonical_event_id]
        btts = [row for row in rows if row.family == "both_teams_to_score"]
        match_result = [row for row in rows if row.family == "match_result"]
        assert btts
        assert all(not row.entered_solver for row in btts)
        assert all(row.comparison_status.value != "matched_equivalent" for row in btts)
        assert match_result
        assert all(not row.entered_solver for row in match_result)
        reasons = " ".join(
            " ".join(row.match_reasons + row.rejection_reasons + ([row.reason] if row.reason else []))
            for row in btts + match_result
        )
        assert "not_registered" in reasons or "outcome_space_mismatch" in reasons or "incomplete" in reasons
        assert all(not decision.eligible_for_paper_simulation for decision in report.paper_decisions)
    finally:
        repository.close()


def test_legacy_series_id_is_merged_not_replaced() -> None:
    settings = Settings(polymarket_gamma_series_id="10188")
    assert settings.resolved_polymarket_series_ids() == polymarket_series_ids_for_targets()
    assert settings.polymarket_series_config_warnings() == []
