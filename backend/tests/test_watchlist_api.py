from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.arbitrage.watchlist.models import WatchLeg, WatchObservation
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName

OBSERVED = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)


def test_watchlist_read_endpoints_expose_near_triggered_and_activity() -> None:
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)

    service.observe(
        WatchObservation(
            observed_at=OBSERVED,
            canonical_event_id="evt-1",
            canonical_market_id="mkt-near",
            competition="Premier League",
            home_team="Newcastle United",
            away_team="Chelsea",
            market_family=MarketFamily.BOTH_TEAMS_TO_SCORE,
            period=FootballPeriod.FULL_TIME,
            legs=[
                WatchLeg(
                    outcome="yes",
                    venue=VenueName.MATCHBOOK,
                    source_market_id="mb",
                    currency="GBP",
                    native_stake=Decimal("40"),
                    gbp_per_unit=Decimal("1"),
                    gbp_stake=Decimal("40"),
                    net_decimal_odds=Decimal("2.2"),
                    cumulative_depth_gbp=Decimal("40"),
                ),
                WatchLeg(
                    outcome="no",
                    venue=VenueName.POLYMARKET,
                    source_market_id="pm",
                    currency="USD",
                    native_stake=Decimal("80"),
                    gbp_per_unit=Decimal("0.75"),
                    gbp_stake=Decimal("60"),
                    net_decimal_odds=Decimal("1.9"),
                    cumulative_depth_gbp=Decimal("60"),
                ),
            ],
            trigger_net_edge=Decimal("0.01"),
            current_net_edge=Decimal("0.008"),
            implied_probability_sum=Decimal("1") / Decimal("1.008"),
            solver_is_arbitrage=True,
            rejection_reasons=["net_edge_below_threshold"],
            quote_age_ms=180,
            quote_age_basis="source",
            limiting_depth_gbp=Decimal("40"),
            capital_required_gbp=Decimal("100"),
        )
    )
    service.observe(
        WatchObservation(
            observed_at=OBSERVED,
            canonical_event_id="evt-2",
            canonical_market_id="mkt-triggered",
            competition="Premier League",
            market_family=MarketFamily.BOTH_TEAMS_TO_SCORE,
            period=FootballPeriod.FULL_TIME,
            trigger_net_edge=Decimal("0.01"),
            current_net_edge=Decimal("0.015"),
            implied_probability_sum=Decimal("1") / Decimal("1.015"),
            solver_is_arbitrage=True,
            eligible_for_paper_simulation=True,
            quote_age_ms=90,
            limiting_depth_gbp=Decimal("70"),
            capital_required_gbp=Decimal("120"),
            guaranteed_profit_gbp=Decimal("1.80"),
            venues=[VenueName.MATCHBOOK, VenueName.POLYMARKET],
        )
    )

    try:
        near = client.get(
            "/paper/watchlist/near",
            params={
                "limit": 10,
                "competition": "Premier League",
            },
        )
        assert near.status_code == 200
        near_body = near.json()
        assert len(near_body) == 1
        assert near_body[0]["canonical_market_id"] == "mkt-near"
        assert near_body[0]["status"] == "APPROACHING"
        assert near_body[0]["is_arbitrage"] is False
        assert near_body[0]["guaranteed_profit_gbp"] is None
        assert float(near_body[0]["distance_to_trigger_pp"]) == 0.2
        assert near_body[0]["quote_age_basis"] == "source"

        triggered = client.get("/paper/watchlist/triggered")
        assert triggered.status_code == 200
        triggered_body = triggered.json()
        assert len(triggered_body) == 1
        assert triggered_body[0]["status"] == "TRIGGERED"
        assert triggered_body[0]["is_arbitrage"] is True

        activity = client.get("/paper/watchlist/activity", params={"limit": 20})
        assert activity.status_code == 200
        event_types = {item["event_type"] for item in activity.json()}
        assert "candidate_first_seen" in event_types
        assert "trigger_crossed" in event_types

        health = client.get("/health")
        assert health.json()["execution_enabled"] is False
        assert health.json()["mode"] == "paper"
    finally:
        app.dependency_overrides.clear()
        repository.close()
