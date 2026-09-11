from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.market_intelligence import get_market_intelligence_service
from sports_hedge.domain.football import MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.models import (
    AnnotationCategory,
    MarketEventAnnotation,
    MarketSnapshot,
)
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService


BASE = datetime(2026, 9, 20, 17, 0, tzinfo=UTC)


def make_snapshot(
    event_number: int,
    minute: int,
    probability: str,
    *,
    family: MarketFamily = MarketFamily.MATCH_RESULT,
    liquidity: str = "500",
) -> MarketSnapshot:
    implied = Decimal(probability)
    event_id = f"evt:{event_number}"
    return MarketSnapshot(
        observed_at=BASE + timedelta(days=event_number, minutes=minute),
        venue=VenueName.MATCHBOOK,
        canonical_event_id=event_id,
        canonical_market_id=f"{event_id}:{family.value}",
        canonical_outcome="home",
        market_family=family,
        competition="Premier League",
        home_team="Newcastle United",
        away_team="Arsenal",
        kickoff_utc=BASE + timedelta(days=event_number, hours=2),
        decimal_odds=Decimal("1") / implied,
        implied_probability=implied,
        spread_decimal=Decimal("0.02"),
        total_liquidity=Decimal(liquidity),
    )


def post_snapshot(client: TestClient, snapshot: MarketSnapshot) -> None:
    response = client.post(
        "/market-intelligence/snapshots",
        json=snapshot.model_dump(mode="json"),
    )
    assert response.status_code == 201


def test_market_intelligence_api_history_reaction_and_trends() -> None:
    repository = SqliteMarketIntelligenceRepository()
    service = MarketIntelligenceService(repository, trend_minimum_sample_size=2)
    app.dependency_overrides[get_market_intelligence_service] = lambda: service
    client = TestClient(app)

    try:
        event_one_time = BASE + timedelta(days=1)
        snapshots = [
            make_snapshot(1, -1, "0.40"),
            make_snapshot(1, 1, "0.45"),
            make_snapshot(1, 5, "0.43"),
            make_snapshot(1, -1, "0.50", family=MarketFamily.CORNERS),
            make_snapshot(1, 2, "0.54", family=MarketFamily.CORNERS),
            make_snapshot(1, 5, "0.52", family=MarketFamily.CORNERS),
            make_snapshot(2, -1, "0.40"),
            make_snapshot(2, 5, "0.44"),
        ]
        for item in snapshots:
            post_snapshot(client, item)

        history = client.get(
            "/market-intelligence/history",
            params={"team": "Newcastle United", "market_family": "match_result"},
        )
        assert history.status_code == 200
        assert len(history.json()) == 5

        annotation = MarketEventAnnotation(
            canonical_event_id="evt:1",
            occurred_at=event_one_time,
            category=AnnotationCategory.TEAM_SHEET,
            source="club",
            title="Starting XI announced",
        )
        created = client.post(
            "/market-intelligence/annotations",
            json=annotation.model_dump(mode="json"),
        )
        assert created.status_code == 201
        annotation_id = created.json()["annotation_id"]

        reaction = client.get(
            f"/market-intelligence/events/evt:1/reactions/{annotation_id}",
            params={
                "post_window_minutes": 10,
                "response_threshold_probability_points": "0.02",
            },
        )
        assert reaction.status_code == 200
        payload = reaction.json()
        assert payload["category"] == "team_sheet"
        assert {item["market_family"] for item in payload["reactions"]} == {
            "match_result",
            "corners",
        }

        trend = client.get(
            "/market-intelligence/trends/open_to_close_probability",
            params={
                "team": "Newcastle United",
                "market_family": "match_result",
                "venue": "matchbook",
            },
        )
        assert trend.status_code == 200
        trend_payload = trend.json()
        assert trend_payload["sample_size"] == 2
        assert trend_payload["sufficient_sample"] is True
        assert trend_payload["median"] == pytest.approx(0.035)

        metrics = client.get("/market-intelligence/trends/metrics")
        assert metrics.status_code == 200
        assert "peak_retracement" in metrics.json()
    finally:
        app.dependency_overrides.clear()
        repository.close()


def test_trend_endpoint_rejects_inverted_probability_bucket() -> None:
    repository = SqliteMarketIntelligenceRepository()
    service = MarketIntelligenceService(repository, trend_minimum_sample_size=2)
    app.dependency_overrides[get_market_intelligence_service] = lambda: service
    client = TestClient(app)

    try:
        response = client.get(
            "/market-intelligence/trends/open_to_close_probability",
            params={
                "minimum_start_probability": 0.7,
                "maximum_start_probability": 0.3,
            },
        )
        assert response.status_code == 422
        assert "cannot exceed" in response.json()["detail"]
    finally:
        app.dependency_overrides.clear()
        repository.close()
