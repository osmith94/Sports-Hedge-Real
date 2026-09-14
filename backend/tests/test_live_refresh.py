from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.application.fixture_state import matchbook_fixture_state
from sports_hedge.application.live_refresh import get_live_refresh_coordinator
from sports_hedge.arbitrage.watchlist.models import WatchLeg, WatchObservation
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.api.watchlist import get_watchlist_service


OBSERVED = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)


def _observation(*, market_id: str, net: str, when: datetime) -> WatchObservation:
    return WatchObservation(
        observed_at=when,
        canonical_event_id="evt-live",
        canonical_market_id=market_id,
        competition="Premier League",
        home_team="Arsenal",
        away_team="Fulham",
        market_family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        legs=[
            WatchLeg(
                outcome="home",
                venue=VenueName.MATCHBOOK,
                source_market_id="mb",
                currency="GBP",
                native_stake=Decimal("50"),
                gbp_per_unit=Decimal("1"),
                gbp_stake=Decimal("50"),
                net_decimal_odds=Decimal("2.05"),
                cumulative_depth_gbp=Decimal("50"),
            )
        ],
        trigger_net_edge=Decimal("0.01"),
        current_net_edge=Decimal(net),
        quote_age_ms=120,
        quote_age_basis="source",
        limiting_depth_gbp=Decimal("50"),
        fixture_discovery_source=VenueName.MATCHBOOK,
        fixture_status="open",
        live_score_supported=False,
    )


def test_matchbook_fixture_state_does_not_invent_scores() -> None:
    without_scores = matchbook_fixture_state(
        {
            "id": 1,
            "name": "Arsenal vs Fulham",
            "status": "open",
            "in-running-flag": False,
        }
    )
    assert without_scores.venue_status == "open"
    assert without_scores.in_running is False
    assert without_scores.live_score_supported is False
    assert without_scores.home_score is None
    assert without_scores.away_score is None

    with_scores = matchbook_fixture_state(
        {
            "id": 1,
            "status": "open",
            "in-running-flag": True,
            "home-score": 1,
            "away-score": 0,
        }
    )
    assert with_scores.live_score_supported is True
    assert with_scores.home_score == 1
    assert with_scores.away_score == 0
    assert with_scores.in_running is True


def test_watchlist_observation_history_describes_approach_without_causation() -> None:
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository)
    first = service.observe(_observation(market_id="mkt-seq", net="-0.020", when=OBSERVED))
    assert first.strike_narrative == "insufficient_history"
    assert first.observation_count == 1
    second = service.observe(
        _observation(
            market_id="mkt-seq",
            net="-0.004",
            when=OBSERVED + timedelta(seconds=30),
        )
    )
    assert second.strike_narrative == "approaching"
    assert second.observation_count == 2
    assert second.previous_net_edge == Decimal("-0.020")
    third = service.observe(
        _observation(
            market_id="mkt-seq",
            net="-0.030",
            when=OBSERVED + timedelta(seconds=60),
        )
    )
    assert third.strike_narrative == "moving_away"
    history = repository.list_observations(third.opportunity_id)
    assert len(history) == 3
    repository.close()


def test_live_refresh_status_is_matchbook_primary_and_server_loop_off_by_default() -> None:
    get_live_refresh_coordinator().reset()
    client = TestClient(app)
    health = client.get("/health")
    assert health.status_code == 200
    body = health.json()
    assert body["execution_enabled"] is False
    assert body["live_refresh"]["discovery_source"] == "matchbook"
    assert body["live_refresh"]["matching_venue"] == "polymarket"
    assert body["live_refresh"]["server_loop_enabled"] is False
    assert body["live_refresh"]["interval_seconds"] >= 15

    status = client.get("/paper/live-refresh")
    assert status.status_code == 200
    payload = status.json()
    assert payload["discovery_source"] == "matchbook"
    assert payload["matching_venue"] == "polymarket"
    assert payload["server_loop_enabled"] is False
    assert "unavailable_unless_matchbook_payload_includes_scores" in payload["live_scores"]
    assert payload["discovered_fixtures"] == []
    assert payload["hot"]["cadence_seconds"] == 30
    assert payload["universe"]["cadence_seconds"] == 180


def test_tracked_rows_expose_source_freshness_and_narrative() -> None:
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED + timedelta(seconds=20))
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    service.observe(_observation(market_id="mkt-src", net="-0.012", when=OBSERVED))
    service.observe(
        _observation(market_id="mkt-src", net="-0.006", when=OBSERVED + timedelta(seconds=20))
    )
    from test_tracked_current_snapshot import _report

    coordinator.record_report(_report("mkt-src"))
    try:
        tracked = client.get("/paper/watchlist/tracked")
        assert tracked.status_code == 200
        row = tracked.json()[0]
        assert row["fixture_discovery_source"] == "matchbook"
        assert row["live_score_supported"] is False
        assert row["home_score"] is None
        assert row["strike_narrative"] == "approaching"
        assert row["observation_count"] == 2
        assert row["quote_age_ms"] == 120
        assert row["quote_age_basis"] == "source"
        assert row["last_seen_at"]
    finally:
        app.dependency_overrides.clear()
        coordinator.reset()
        repository.close()


@pytest.mark.asyncio
async def test_live_refresh_records_completion_when_cycle_raises() -> None:
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()

    async def boom() -> None:
        raise RuntimeError("matchbook hung")

    with pytest.raises(RuntimeError, match="hung"):
        await coordinator.run_cycle(boom)
    assert coordinator.status.cycle_in_progress is False
    assert coordinator.status.last_completed_at is not None
    assert coordinator.status.last_error is not None
    assert "hung" in coordinator.status.last_error
    coordinator.reset()
