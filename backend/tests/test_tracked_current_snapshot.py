from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.application.collector import CollectionReport
from sports_hedge.application.live_refresh import get_live_refresh_coordinator
from sports_hedge.arbitrage.watchlist.models import OpportunityStatus, WatchLeg, WatchObservation
from sports_hedge.arbitrage.watchlist.ranking import (
    opportunity_id_for_canonical_market,
    rank_tracked_opportunities,
    tracked_cohort_opportunity_ids,
)
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.models import PaperScanDecision

OBSERVED = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)


def _leg() -> WatchLeg:
    return WatchLeg(
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


def _observation(
    *,
    market_id: str,
    edge: Decimal | None,
    status_edge_eligible: bool = False,
    rejection_reasons: list[str] | None = None,
    quote_age_ms: int | None = 80,
    observed_at: datetime = OBSERVED,
) -> WatchObservation:
    return WatchObservation(
        observed_at=observed_at,
        canonical_event_id=f"evt-{market_id}",
        canonical_market_id=market_id,
        settlement_key="regulation_time|full_time",
        competition="Premier League",
        home_team="Arsenal",
        away_team="Fulham",
        market_family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        legs=[_leg()],
        trigger_net_edge=Decimal("0.01"),
        current_net_edge=edge,
        implied_probability_sum=None if edge is None else Decimal("1") / (Decimal("1") + edge),
        solver_is_arbitrage=status_edge_eligible,
        eligible_for_paper_simulation=status_edge_eligible,
        rejection_reasons=rejection_reasons or [],
        quote_age_ms=quote_age_ms,
        quote_age_basis="source",
        limiting_depth_gbp=Decimal("50"),
        guaranteed_profit_gbp=Decimal("1.50") if status_edge_eligible else None,
    )


def _decision(market_id: str) -> PaperScanDecision:
    return PaperScanDecision(
        canonical_event_id=f"evt-{market_id}",
        canonical_market_id=market_id,
        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=[]),
    )


def _report(
    *market_ids: str,
    venue_health: dict[str, str] | None = None,
    extra_decisions: list[PaperScanDecision] | None = None,
) -> CollectionReport:
    now = datetime.now(UTC)
    decisions = [_decision(market_id) for market_id in market_ids]
    if extra_decisions:
        decisions.extend(extra_decisions)
    return CollectionReport(
        started_at=now,
        completed_at=now,
        paper_decisions=decisions,
        venue_health=venue_health or {},
        operator_summary="latest completed live collection",
    )


def test_tracked_is_empty_until_a_live_collection_completes() -> None:
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    service.observe(
        _observation(market_id="mkt-stale-history", edge=Decimal("0.012"), status_edge_eligible=True)
    )
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    try:
        tracked = client.get("/paper/watchlist/tracked")
        assert tracked.status_code == 200
        assert tracked.json() == []
        activity = client.get("/paper/watchlist/activity").json()
        assert activity
        assert any(event["opportunity_id"] == "watch:mkt-stale-history" for event in activity)
        assert repository.get("watch:mkt-stale-history") is not None
    finally:
        app.dependency_overrides.clear()
        coordinator.reset()
        repository.close()


def test_tracked_follows_latest_completed_collection_and_keeps_history() -> None:
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    repository = SqliteWatchlistRepository()
    clock = {"now": OBSERVED}
    service = WatchlistService(repository, clock=lambda: clock["now"])
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    try:
        service.observe(_observation(market_id="mkt-a-only", edge=Decimal("0.008")))
        service.observe(
            _observation(
                market_id="mkt-shared",
                edge=Decimal("0.015"),
                status_edge_eligible=True,
            )
        )
        coordinator.record_report(_report("mkt-a-only", "mkt-shared"))
        first = client.get("/paper/watchlist/tracked").json()
        assert {row["canonical_market_id"] for row in first} == {"mkt-a-only", "mkt-shared"}

        clock["now"] = OBSERVED + timedelta(seconds=30)
        service.observe(
            _observation(
                market_id="mkt-b-only",
                edge=Decimal("0.009"),
                observed_at=clock["now"],
            )
        )
        service.observe(
            _observation(
                market_id="mkt-shared",
                edge=Decimal("0.016"),
                status_edge_eligible=True,
                observed_at=clock["now"],
            )
        )
        coordinator.record_report(_report("mkt-b-only", "mkt-shared"))

        second = client.get("/paper/watchlist/tracked").json()
        ids = [row["canonical_market_id"] for row in second]
        assert "mkt-a-only" not in ids
        assert set(ids) == {"mkt-b-only", "mkt-shared"}

        persisted_a = repository.get("watch:mkt-a-only")
        assert persisted_a is not None
        history_a = repository.list_observations("watch:mkt-a-only")
        assert history_a
        activity = client.get("/paper/watchlist/activity", params={"limit": 100}).json()
        activity_ids = {event["opportunity_id"] for event in activity}
        assert "watch:mkt-a-only" in activity_ids
        assert "watch:mkt-b-only" in activity_ids
        assert "watch:mkt-shared" in activity_ids
    finally:
        app.dependency_overrides.clear()
        coordinator.reset()
        repository.close()


def test_degraded_completed_refresh_shows_only_that_cohort() -> None:
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    service.observe(_observation(market_id="mkt-previous", edge=Decimal("0.008")))
    service.observe(_observation(market_id="mkt-healthy", edge=Decimal("0.007")))
    coordinator.record_report(
        _report(
            "mkt-healthy",
            venue_health={"matchbook": "ok", "polymarket": "unavailable", "kalshi": "ok"},
        )
    )
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    try:
        tracked = client.get("/paper/watchlist/tracked").json()
        assert [row["canonical_market_id"] for row in tracked] == ["mkt-healthy"]
        status = client.get("/paper/live-refresh").json()
        assert status["venue_health"]["polymarket"] == "unavailable"
        assert status["venue_health"]["matchbook"] == "ok"
    finally:
        app.dependency_overrides.clear()
        coordinator.reset()
        repository.close()


def test_cohort_dedupes_same_market_but_keeps_distinct_markets() -> None:
    ids = tracked_cohort_opportunity_ids(["mkt-1x2", "mkt-btts", "mkt-1x2", None, ""])
    assert ids == {
        opportunity_id_for_canonical_market("mkt-1x2"),
        opportunity_id_for_canonical_market("mkt-btts"),
    }


def test_default_tracked_ranking_puts_strongest_actionable_first() -> None:
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    rejected = service.observe(
        _observation(
            market_id="mkt-rejected",
            edge=Decimal("0.04"),
            rejection_reasons=["missing_fx_rate:USD"],
        )
    )
    watching_far = service.observe(_observation(market_id="mkt-watch-far", edge=Decimal("0.001")))
    watching_near = service.observe(_observation(market_id="mkt-watch-near", edge=Decimal("0.009")))
    triggered_weak = service.observe(
        _observation(market_id="mkt-trig-weak", edge=Decimal("0.011"), status_edge_eligible=True)
    )
    triggered_strong = service.observe(
        _observation(market_id="mkt-trig-strong", edge=Decimal("0.03"), status_edge_eligible=True)
    )
    assert rejected.status == OpportunityStatus.REJECTED
    assert watching_far.status in {OpportunityStatus.WATCHING, OpportunityStatus.APPROACHING}
    assert watching_near.status in {OpportunityStatus.WATCHING, OpportunityStatus.APPROACHING}
    assert triggered_weak.status == OpportunityStatus.TRIGGERED
    assert triggered_strong.status == OpportunityStatus.TRIGGERED

    ranked = rank_tracked_opportunities(service.repository.list_opportunities(), limit=10)
    assert [item.canonical_market_id for item in ranked] == [
        "mkt-trig-strong",
        "mkt-trig-weak",
        "mkt-watch-near",
        "mkt-watch-far",
        "mkt-rejected",
    ]
    assert ranked[0].current_net_edge == Decimal("0.03")
    assert ranked[-1].status == OpportunityStatus.REJECTED
    repository.close()
