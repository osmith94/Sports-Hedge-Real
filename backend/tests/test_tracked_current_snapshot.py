from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.application.collector import CollectionReport, DiscoveredFixture
from sports_hedge.application.live_refresh import get_live_refresh_coordinator
from sports_hedge.application.scan_lanes import ScanLane
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
FAR_KICKOFF = OBSERVED + timedelta(days=6)


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
    event_id: str | None = None,
) -> WatchObservation:
    return WatchObservation(
        observed_at=observed_at,
        canonical_event_id=event_id or f"evt-{market_id}",
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
        kickoff_utc=FAR_KICKOFF,
    )


def _decision(market_id: str) -> PaperScanDecision:
    return PaperScanDecision(
        canonical_event_id=f"evt-{market_id}",
        canonical_market_id=market_id,
        fixture_canonical_event_id=f"evt-{market_id}",
        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=[]),
        scanned_at=OBSERVED,
    )


def _fixture(market_id: str, *, evaluation: str = "evaluated") -> DiscoveredFixture:
    return DiscoveredFixture(
        source=VenueName.MATCHBOOK,
        source_event_id=f"evt-{market_id}",
        canonical_event_id=f"evt-{market_id}",
        home_team="Arsenal",
        away_team="Fulham",
        competition="Premier League",
        kickoff_utc=FAR_KICKOFF,
        last_seen_at=OBSERVED,
        market_evaluation_state=evaluation,
        opportunity_state="matched" if evaluation == "evaluated" else "not_evaluated",
    )


def _report(
    *market_ids: str,
    venue_health: dict[str, str] | None = None,
    extra_decisions: list[PaperScanDecision] | None = None,
    fixtures: list[DiscoveredFixture] | None = None,
    when: datetime = OBSERVED,
    leftover_ids: list[str] | None = None,
) -> CollectionReport:
    decisions = [_decision(market_id) for market_id in market_ids]
    if extra_decisions:
        decisions.extend(extra_decisions)
    discovered = fixtures
    if discovered is None:
        discovered = [_fixture(market_id) for market_id in market_ids]
        for leftover in leftover_ids or []:
            discovered.append(_fixture(leftover, evaluation="not_evaluated_scan_deadline"))
    return CollectionReport(
        started_at=when,
        completed_at=when,
        paper_decisions=decisions,
        discovered_fixtures=discovered,
        venue_health=venue_health or {},
        operator_summary="latest completed live collection",
        scan_lane=ScanLane.UNIVERSE.value,
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


def test_tracked_merges_universe_rows_across_later_hot_cycles() -> None:
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
        coordinator.record_report(_report("mkt-a-only", "mkt-shared"), scan_lane=ScanLane.UNIVERSE)
        first = client.get("/paper/watchlist/tracked").json()
        assert {row["canonical_market_id"] for row in first} == {"mkt-a-only", "mkt-shared"}

        clock["now"] = OBSERVED + timedelta(seconds=30)
        service.observe(
            _observation(
                market_id="mkt-b-only",
                edge=Decimal("0.009"),
                observed_at=clock["now"],
                event_id="evt-hot-b",
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
        hot_fixture = DiscoveredFixture(
            source=VenueName.MATCHBOOK,
            source_event_id="evt-hot-b",
            canonical_event_id="evt-hot-b",
            home_team="Arsenal",
            away_team="Fulham",
            competition="Premier League",
            kickoff_utc=clock["now"] - timedelta(minutes=5),
            last_seen_at=clock["now"],
            in_running=True,
            market_evaluation_state="evaluated",
            opportunity_state="matched",
        )
        coordinator.record_report(
            CollectionReport(
                started_at=clock["now"],
                completed_at=clock["now"],
                paper_decisions=[
                    PaperScanDecision(
                        canonical_event_id="evt-hot-b",
                        canonical_market_id="mkt-b-only",
                        fixture_canonical_event_id="evt-hot-b",
                        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=[]),
                        scanned_at=clock["now"],
                    ),
                    PaperScanDecision(
                        canonical_event_id="evt-mkt-shared",
                        canonical_market_id="mkt-shared",
                        fixture_canonical_event_id="evt-mkt-shared",
                        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=[]),
                        scanned_at=clock["now"],
                    ),
                ],
                discovered_fixtures=[
                    hot_fixture,
                    _fixture("mkt-shared"),
                ],
                scan_lane=ScanLane.HOT.value,
            ),
            scan_lane=ScanLane.HOT,
        )

        second = client.get("/paper/watchlist/tracked").json()
        ids = {row["canonical_market_id"] for row in second}
        assert "mkt-a-only" in ids
        assert "mkt-b-only" in ids
        assert "mkt-shared" in ids

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


def test_degraded_universe_chunk_keeps_previous_valid_current_state() -> None:
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    service.observe(_observation(market_id="mkt-previous", edge=Decimal("0.008")))
    service.observe(_observation(market_id="mkt-healthy", edge=Decimal("0.007")))
    coordinator.record_report(_report("mkt-previous", "mkt-healthy"), scan_lane=ScanLane.UNIVERSE)
    coordinator.record_report(
        _report(
            "mkt-healthy",
            leftover_ids=["mkt-previous"],
            venue_health={"matchbook": "ok", "polymarket": "unavailable", "kalshi": "ok"},
        ),
        scan_lane=ScanLane.UNIVERSE,
    )
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    try:
        tracked = client.get("/paper/watchlist/tracked").json()
        assert {row["canonical_market_id"] for row in tracked} == {"mkt-healthy", "mkt-previous"}
        status = client.get("/paper/live-refresh").json()
        assert status["venue_health"]["polymarket"] == "unavailable"
        assert status["venue_health"]["matchbook"] == "ok"
        assert "hot" in status and "universe" in status
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


def test_tracked_snapshot_keeps_newest_observations_inside_the_limit() -> None:
    """A stronger older edge must not crowd a newer row out of the snapshot."""

    repository = SqliteWatchlistRepository()
    latest = OBSERVED + timedelta(seconds=24)
    service = WatchlistService(repository, clock=lambda: latest)
    for index in range(25):
        service.observe(
            _observation(
                market_id=f"mkt-{index:02d}",
                edge=Decimal("0.05") if index == 0 else Decimal("0.001"),
                observed_at=OBSERVED + timedelta(seconds=index),
            )
        )
    newest = service.tracked(limit=20)
    assert len(newest) == 20
    assert [item.canonical_market_id for item in newest[:3]] == ["mkt-24", "mkt-23", "mkt-22"]
    assert "mkt-00" not in {item.canonical_market_id for item in newest}
    assert newest[0].current_net_edge == Decimal("0.001")
    assert newest[0].status not in {OpportunityStatus.CLOSED, OpportunityStatus.EXPIRED}
    widened = service.tracked(limit=50)
    assert len(widened) == 25
    oldest = widened[-1]
    assert oldest.canonical_market_id == "mkt-00"
    assert oldest.current_net_edge == Decimal("0.05")
    assert service.tracked() == newest
    repository.close()


def test_tracked_route_defaults_to_twenty_and_allows_fifty() -> None:
    from sports_hedge.api.watchlist import tracked_markets
    import inspect

    limit = inspect.signature(tracked_markets).parameters["limit"].default
    bounds = {type(item).__name__: item for item in limit.metadata}
    assert limit.default == 20
    assert bounds["Ge"].ge == 1
    assert bounds["Le"].le == 500
