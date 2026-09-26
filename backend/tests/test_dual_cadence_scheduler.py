from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api import paper as paper_api
from sports_hedge.api.main import app
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.application.collector import (
    CollectionReport,
    DiscoveredFixture,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.live_refresh import (
    ExplicitCollectBusy,
    LiveRefreshCoordinator,
    SCAN_CYCLE_RETURN_GRACE_SECONDS,
    get_live_refresh_coordinator,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane, classify_scan_lane, hot_sort_key
from sports_hedge.arbitrage.watchlist.models import WatchLeg, WatchObservation
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient
from test_read_only_collector import FakePolymarket
from test_scan_timeout import HungMarketsMatchbook

NOW = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)


class FakeClock:
    def __init__(self, now: datetime) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now = self.now + timedelta(seconds=seconds)


def _fixture(
    canonical_id: str,
    *,
    kickoff: datetime,
    in_running: bool | None = None,
    fixture_status: str | None = None,
    evaluation: str = "evaluated",
    opportunity_state: str = "matched",
    last_seen: datetime = NOW,
) -> DiscoveredFixture:
    return DiscoveredFixture(
        source=VenueName.MATCHBOOK,
        source_event_id=canonical_id,
        canonical_event_id=canonical_id,
        home_team="Home",
        away_team="Away",
        competition="Premier League",
        kickoff_utc=kickoff,
        last_seen_at=last_seen,
        in_running=in_running,
        fixture_status=fixture_status,
        market_evaluation_state=evaluation,
        opportunity_state=opportunity_state,
    )


def _decision(event_id: str, market_id: str, *, when: datetime = NOW) -> PaperScanDecision:
    return PaperScanDecision(
        canonical_event_id=event_id,
        canonical_market_id=market_id,
        fixture_canonical_event_id=event_id,
        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=[]),
        scanned_at=when,
        eligible_for_paper_simulation=False,
    )


def _report(
    fixtures: list[DiscoveredFixture],
    *,
    when: datetime = NOW,
    scan_lane: str = ScanLane.UNIVERSE.value,
    decisions: list[PaperScanDecision] | None = None,
    venue_health: dict[str, str] | None = None,
) -> CollectionReport:
    paper = decisions or [
        _decision(item.canonical_event_id, f"mkt-{item.canonical_event_id}", when=when)
        for item in fixtures
        if item.market_evaluation_state == "evaluated"
    ]
    return CollectionReport(
        started_at=when,
        completed_at=when,
        paper_decisions=paper,
        discovered_fixtures=fixtures,
        scan_lane=scan_lane,
        venue_health=venue_health or {"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"},
        operator_summary="test",
        fixture_identity_aliases={
            item.canonical_event_id: item.canonical_event_id for item in fixtures
        },
        fixture_source_events={
            item.canonical_event_id: [
                {
                    "venue": "matchbook",
                    "source_event_id": item.source_event_id,
                    "raw": {"id": item.source_event_id, "name": "Home vs Away"},
                }
            ]
            for item in fixtures
        },
    )


def _observation(event_id: str, market_id: str, *, when: datetime, triggered: bool = False) -> WatchObservation:
    return WatchObservation(
        observed_at=when,
        canonical_event_id=event_id,
        canonical_market_id=market_id,
        competition="Premier League",
        home_team="Home",
        away_team="Away",
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
        current_net_edge=Decimal("0.02") if triggered else Decimal("0.004"),
        implied_probability_sum=Decimal("0.98"),
        solver_is_arbitrage=triggered,
        eligible_for_paper_simulation=triggered,
        guaranteed_profit_gbp=Decimal("1.2") if triggered else None,
        quote_age_ms=80,
        quote_age_basis="source",
        limiting_depth_gbp=Decimal("50"),
        kickoff_utc=NOW + timedelta(days=6),
    )


def test_classifier_matrix_does_not_fabricate_live_or_completed() -> None:
    t6d = _fixture("t6d", kickoff=NOW + timedelta(days=6))
    t4h = _fixture("t4h", kickoff=NOW + timedelta(hours=4))
    t59m = _fixture("t59m", kickoff=NOW + timedelta(minutes=59))
    live = _fixture("live", kickoff=NOW - timedelta(minutes=10), in_running=True)
    unknown = _fixture("unknown", kickoff=NOW - timedelta(hours=1), in_running=None)
    late_unknown = _fixture("late", kickoff=NOW - timedelta(hours=3, minutes=1), in_running=None)
    completed = _fixture(
        "done",
        kickoff=NOW - timedelta(hours=2),
        in_running=None,
        fixture_status="completed",
    )
    assert classify_scan_lane(t6d, NOW) is ScanLane.UNIVERSE
    assert classify_scan_lane(t4h, NOW) is ScanLane.UNIVERSE
    assert classify_scan_lane(t59m, NOW) is ScanLane.HOT
    assert classify_scan_lane(live, NOW) is ScanLane.HOT
    assert live.in_running is True
    assert classify_scan_lane(unknown, NOW) is ScanLane.HOT
    assert unknown.in_running is None
    assert classify_scan_lane(late_unknown, NOW) is ScanLane.DROP
    assert late_unknown.in_running is None
    assert late_unknown.fixture_status is None
    assert classify_scan_lane(completed, NOW) is ScanLane.DROP
    in_play_first = hot_sort_key(live)[0]
    pre_kickoff = hot_sort_key(t59m)[0]
    assert in_play_first < pre_kickoff


def test_t59m_and_in_play_are_hot_without_waiting_for_universe() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator.reset()
    coordinator._clock = clock
    far = _fixture("t6d", kickoff=NOW + timedelta(days=6))
    hot = _fixture("t59m", kickoff=NOW + timedelta(minutes=59))
    live = _fixture("live", kickoff=NOW - timedelta(minutes=1), in_running=True)
    coordinator.record_report(_report([far, hot, live], when=NOW), scan_lane=ScanLane.UNIVERSE)
    clock.advance(30)
    plan = coordinator.plan_tick(now=clock.now)
    assert plan.lane == "hot"
    assert "t59m" in plan.identity_scope
    assert "live" in plan.identity_scope
    assert "t6d" not in plan.identity_scope
    assert plan.collector_timeout_seconds == 25


def test_t6d_is_not_in_hot_identity_scope() -> None:
    """Non-qualifying distant fixtures stay UNIVERSE-only (Issue #200 contrast)."""
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    far = _fixture("t6d", kickoff=NOW + timedelta(days=6))
    near = _fixture("t59m", kickoff=NOW + timedelta(minutes=59))
    coordinator.record_report(_report([far, near], when=NOW), scan_lane=ScanLane.UNIVERSE)
    scope = coordinator.fixture_current_state().hot_identity_scope(NOW)
    assert scope == ["t59m"]
    assert "t6d" not in coordinator.fixture_current_state().known_source_events(scope)
    coordinator.reset()


@pytest.mark.asyncio
async def test_universe_chunk_yields_and_cursor_advances_across_hot_cycles() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    live = _fixture("live", kickoff=NOW - timedelta(minutes=1), in_running=True)
    coordinator.record_report(_report([live], when=NOW), scan_lane=ScanLane.HOT)
    coordinator._next_hot_due = NOW + timedelta(seconds=20)
    coordinator._next_universe_due = NOW
    coordinator._universe_generation_started_at = None
    coordinator._universe_work_used = 0.0
    coordinator._universe_evaluated_ids = set()
    coordinator._universe_cursor = None

    async def runner() -> CollectionReport:
        fixture_a = _fixture("a", kickoff=NOW + timedelta(days=2), evaluation="evaluated")
        leftover_b = _fixture("b", kickoff=NOW + timedelta(days=3), evaluation="not_evaluated_scan_deadline")
        leftover_c = _fixture("c", kickoff=NOW + timedelta(days=4), evaluation="not_evaluated_scan_deadline")
        when = clock.now
        completed = when + timedelta(seconds=3)
        if not coordinator._universe_evaluated_ids:
            report = _report(
                [fixture_a, leftover_b, leftover_c],
                when=when,
                scan_lane=ScanLane.UNIVERSE.value,
            )
        else:
            report = _report(
                [
                    _fixture("b", kickoff=NOW + timedelta(days=3), evaluation="evaluated"),
                    leftover_c,
                ],
                when=when,
                scan_lane=ScanLane.UNIVERSE.value,
            )
        report = report.model_copy(update={"completed_at": completed})
        return report

    first = coordinator.plan_tick(now=clock.now)
    assert first.lane == "universe"
    assert first.collector_timeout_seconds is not None
    assert first.coordinator_timeout_seconds is not None
    assert first.unbounded_cycle is False
    assert first.reason == "universe_sweep"
    await coordinator.run_cycle(
        runner,
        timeout_seconds=first.coordinator_timeout_seconds,
        scan_lane=ScanLane.UNIVERSE,
    )
    assert coordinator._universe_cursor == "a"
    assert "a" in coordinator._universe_evaluated_ids
    work_after_first = coordinator._universe_work_used
    clock.now = NOW + timedelta(seconds=20)
    hot_plan = coordinator.plan_tick(now=clock.now)
    assert hot_plan.lane == "hot"

    async def hot_runner() -> CollectionReport:
        return _report([live], when=clock.now, scan_lane=ScanLane.HOT.value)

    await coordinator.run_cycle(
        hot_runner,
        timeout_seconds=hot_plan.coordinator_timeout_seconds,
        scan_lane=ScanLane.HOT,
    )
    clock.advance(1)
    second = coordinator.plan_tick(now=clock.now)
    assert second.lane == "universe"
    assert "a" in second.skip_event_ids
    await coordinator.run_cycle(
        runner,
        timeout_seconds=second.coordinator_timeout_seconds,
        scan_lane=ScanLane.UNIVERSE,
    )
    assert coordinator._universe_cursor == "b"
    assert coordinator._universe_work_used > work_after_first
    assert coordinator.status.hot.last_completed_at is not None


def test_far_future_triggered_surfaces_in_same_universe_chunk() -> None:
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: NOW)
    far = _fixture("t6d", kickoff=NOW + timedelta(days=6))
    decision = _decision("t6d", "mkt-t6d")
    decision = decision.model_copy(
        update={
            "eligible_for_paper_simulation": True,
            "current_net_edge": None,
        }
    )
    service.observe(_observation("t6d", "mkt-t6d", when=NOW, triggered=True))
    coordinator.record_report(
        _report([far], when=NOW, decisions=[_decision("t6d", "mkt-t6d")]),
        scan_lane=ScanLane.UNIVERSE,
    )
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    try:
        tracked = client.get("/paper/watchlist/tracked").json()
        assert any(row["canonical_market_id"] == "mkt-t6d" for row in tracked)
        row = next(item for item in tracked if item["canonical_market_id"] == "mkt-t6d")
        assert row["freshness_class"] == "executable"
        assert row["scan_lane"] == "universe"
        triggered = client.get("/paper/watchlist/triggered").json()
        assert any(item["canonical_market_id"] == "mkt-t6d" for item in triggered)
    finally:
        app.dependency_overrides.clear()
        coordinator.reset()
        repository.close()


def test_promotion_into_hot_as_clock_advances() -> None:
    fixture = _fixture("promo", kickoff=NOW + timedelta(minutes=61))
    assert classify_scan_lane(fixture, NOW) is ScanLane.UNIVERSE
    later = NOW + timedelta(minutes=2)
    assert classify_scan_lane(fixture, later) is ScanLane.HOT
    coordinator = LiveRefreshCoordinator()
    coordinator.record_report(_report([fixture], when=NOW), scan_lane=ScanLane.UNIVERSE)
    assert "promo" not in coordinator.fixture_current_state().hot_identity_scope(NOW)
    assert coordinator.fixture_current_state().hot_identity_scope(later) == ["promo"]


def test_partial_universe_chunk_retains_previous_evaluation() -> None:
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    first = _report(
        [
            _fixture("healthy", kickoff=NOW + timedelta(days=2)),
            _fixture("rest", kickoff=NOW + timedelta(days=3)),
        ],
        when=NOW,
    )
    coordinator.record_report(first, scan_lane=ScanLane.UNIVERSE)
    leftover = _report(
        [
            _fixture("healthy", kickoff=NOW + timedelta(days=2)),
            _fixture(
                "rest",
                kickoff=NOW + timedelta(days=3),
                evaluation="not_evaluated_scan_deadline",
                opportunity_state="not_evaluated",
            ),
        ],
        when=NOW + timedelta(seconds=10),
    )
    coordinator.record_report(leftover, scan_lane=ScanLane.UNIVERSE)
    store = coordinator.fixture_current_state()
    rest = store.detail("rest")
    assert rest is not None
    assert rest.fixture.market_evaluation_state == "evaluated"
    ids = store.current_tracked_opportunity_ids(NOW + timedelta(seconds=10))
    assert "watch:mkt-healthy" in ids
    assert "watch:mkt-rest" in ids
    coordinator.reset()


def test_same_canonical_ids_no_duplicate_tracked_rows() -> None:
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: NOW)
    fixture = _fixture("same", kickoff=NOW + timedelta(minutes=30))
    service.observe(_observation("same", "mkt-same", when=NOW))
    coordinator.record_report(_report([fixture], when=NOW), scan_lane=ScanLane.UNIVERSE)
    coordinator.record_report(_report([fixture], when=NOW), scan_lane=ScanLane.HOT)
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    try:
        tracked = client.get("/paper/watchlist/tracked").json()
        ids = [row["canonical_market_id"] for row in tracked]
        assert ids.count("mkt-same") == 1
        assert store_aliases_unique(coordinator)
    finally:
        app.dependency_overrides.clear()
        coordinator.reset()
        repository.close()


def store_aliases_unique(coordinator: LiveRefreshCoordinator) -> bool:
    identities = coordinator.fixture_identities("same")
    return "same" in identities


def test_live_refresh_exposes_distinct_hot_and_universe_status() -> None:
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    clock = FakeClock(NOW)
    original_clock = coordinator._clock
    coordinator._clock = clock
    coordinator.record_report(
        _report([_fixture("t6d", kickoff=NOW + timedelta(days=6))], when=NOW),
        scan_lane=ScanLane.UNIVERSE,
    )
    client = TestClient(app)
    try:
        payload = client.get("/paper/live-refresh").json()
        assert payload["hot"]["cadence_seconds"] == 10
        assert payload["hot"]["reprice_after_seconds"] == 30
        assert payload["hot"]["cycle_timeout_seconds"] == 25
        assert payload["universe"]["cadence_seconds"] == 3600
        assert payload["background"]["cadence_seconds"] == 10
        assert payload["background"]["reprice_after_seconds"] == 600
        assert payload["universe"]["generation_budget_seconds"] == 150
        assert payload["interval_seconds"] == payload["hot"]["cadence_seconds"]
        assert "HOT pricing" in (payload["operator_summary"] or "")
        assert "BACKGROUND pricing" in (payload["operator_summary"] or "")
        assert "UNIVERSE discovery" in (payload["operator_summary"] or "")
        assert "Fast scan" not in (payload["operator_summary"] or "")
        assert "Full sweep" not in (payload["operator_summary"] or "")
        assert "discovered_fixtures" not in payload
        catalogue = client.get("/operations/universe-fixtures").json()
        assert catalogue["fixtures"]
        assert catalogue["fixtures"][0]["canonical_event_id"]
    finally:
        coordinator.reset()
        coordinator._clock = original_clock


def test_hot_and_universe_ttl_expiry_omits_tracked_rows() -> None:
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    repository = SqliteWatchlistRepository()
    clock = {"now": NOW}
    service = WatchlistService(repository, clock=lambda: clock["now"])
    live = _fixture("live", kickoff=NOW - timedelta(minutes=5), in_running=True)
    far = _fixture("t6d", kickoff=NOW + timedelta(days=6))
    service.observe(_observation("live", "mkt-live", when=NOW))
    service.observe(_observation("t6d", "mkt-t6d", when=NOW))
    coordinator.record_report(_report([live], when=NOW), scan_lane=ScanLane.HOT)
    coordinator.record_report(_report([far], when=NOW), scan_lane=ScanLane.UNIVERSE)
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    try:
        assert {row["canonical_market_id"] for row in client.get("/paper/watchlist/tracked").json()} == {
            "mkt-live",
            "mkt-t6d",
        }
        clock["now"] = NOW + timedelta(seconds=91)
        ids = {row["canonical_market_id"] for row in client.get("/paper/watchlist/tracked").json()}
        assert "mkt-live" not in ids
        assert "mkt-t6d" in ids
        clock["now"] = NOW + timedelta(seconds=361)
        ids = {row["canonical_market_id"] for row in client.get("/paper/watchlist/tracked").json()}
        assert ids == set()
        assert repository.get("watch:mkt-live") is not None
        assert repository.get("watch:mkt-t6d") is not None
    finally:
        app.dependency_overrides.clear()
        coordinator.reset()
        repository.close()


def test_empty_hot_cycle_does_not_clear_in_ttl_universe_tracked() -> None:
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: NOW)
    far = _fixture("t6d", kickoff=NOW + timedelta(days=6))
    service.observe(_observation("t6d", "mkt-t6d", when=NOW))
    coordinator.record_report(_report([far], when=NOW), scan_lane=ScanLane.UNIVERSE)
    coordinator.record_report(
        _report([], when=NOW + timedelta(seconds=30), scan_lane=ScanLane.HOT.value, decisions=[]),
        scan_lane=ScanLane.HOT,
    )
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    try:
        tracked = client.get("/paper/watchlist/tracked").json()
        assert {row["canonical_market_id"] for row in tracked} == {"mkt-t6d"}
    finally:
        app.dependency_overrides.clear()
        coordinator.reset()
        repository.close()


def test_execution_enabled_false_and_no_place_cancel_sign() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    for venue in (MatchbookClient, PolymarketClient, KalshiClient):
        assert not hasattr(venue, "place_order")
        assert not hasattr(venue, "cancel_order")
        source = inspect.getsource(venue)
        assert "place_order" not in source
        assert "cancel_order" not in source
        assert "sign_order" not in source


def test_t_plus_3h_unknown_leaves_hot_without_fabricating_completed() -> None:
    kickoff = NOW - timedelta(hours=2, minutes=59)
    fixture = _fixture("unk", kickoff=kickoff, in_running=None)
    assert classify_scan_lane(fixture, NOW) is ScanLane.HOT
    assert fixture.in_running is None
    later = NOW + timedelta(minutes=2)
    demoted = classify_scan_lane(fixture, later)
    assert demoted is ScanLane.DROP
    assert fixture.in_running is None
    assert fixture.fixture_status is None


def test_startup_universe_due_immediately_and_tracked_empty() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator.reset()
    coordinator._clock = clock
    assert coordinator.fixture_current_state().has_collection() is False
    client = TestClient(app)
    get_live_refresh_coordinator().reset()
    assert client.get("/paper/watchlist/tracked").json() == []
    assert coordinator.universe_due_immediately()
    assert coordinator._next_universe_due == NOW
    assert coordinator._next_hot_due == NOW
    plan = coordinator.plan_tick(now=NOW)
    assert plan.lane == "universe"
    assert plan.reason == "universe_sweep"
    assert plan.collector_timeout_seconds is not None
    assert plan.unbounded_cycle is False
    assert coordinator._next_hot_due == NOW


@pytest.mark.asyncio
async def test_hot_envelope_stops_inside_30s_and_does_not_overlap() -> None:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=HungMarketsMatchbook(),
        polymarket=FakePolymarket(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        venue_timeout_seconds=0.2,
        provider_call_timeout_seconds=0.2,
        cycle_timeout_seconds=25.0,
    )
    coordinator = LiveRefreshCoordinator()
    in_progress = []

    async def runner() -> CollectionReport:
        in_progress.append(coordinator.status.hot.cycle_in_progress)
        return await collector.collect_and_scan(cycle_timeout_seconds=25)

    started = monotonic()
    report = await coordinator.run_cycle(
        runner,
        timeout_seconds=25 + SCAN_CYCLE_RETURN_GRACE_SECONDS,
        scan_lane=ScanLane.HOT,
    )
    elapsed = monotonic() - started
    assert elapsed < 30
    assert True in in_progress
    assert coordinator.status.hot.cycle_in_progress is False
    assert coordinator.status.hot.last_error is None
    assert coordinator.status.last_error is None
    assert coordinator.status.hot.last_diagnostics is not None
    assert report.discovered_fixtures
    repository.close()


def test_explicit_collect_is_bounded_diagnostic_and_keeps_max_event_pairs() -> None:
    from sports_hedge.api.paper import PaperCollectionRequest
    from sports_hedge.application.collector import DEFAULT_MAX_EVENT_PAIRS

    request = PaperCollectionRequest()
    assert request.max_event_pairs == DEFAULT_MAX_EVENT_PAIRS == 60
    settings = Settings()
    assert settings.paper_scan_cycle_timeout_seconds == 45
    assert settings.paper_scan_manual_diagnostic_timeout_seconds == 20
    assert settings.paper_scan_hot_cycle_timeout_seconds == 25
    assert (
        settings.paper_scan_manual_diagnostic_timeout_seconds
        + SCAN_CYCLE_RETURN_GRACE_SECONDS
        == 25
    )
    assert (
        settings.paper_scan_hot_cycle_timeout_seconds + SCAN_CYCLE_RETURN_GRACE_SECONDS
        == 30
    )


def test_hot_upsert_keeps_161_aliases() -> None:
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    cluster_id = "cluster-1"
    source_id = "mb-1"
    decision_id = "pair-1"
    fixture = _fixture(cluster_id, kickoff=NOW + timedelta(minutes=30))
    fixture = fixture.model_copy(update={"source_event_id": source_id})
    report = _report([fixture], when=NOW)
    report = report.model_copy(
        update={
            "fixture_identity_aliases": {
                cluster_id: cluster_id,
                source_id: cluster_id,
                decision_id: cluster_id,
            },
            "paper_decisions": [
                _decision(decision_id, "mkt-1").model_copy(
                    update={"fixture_canonical_event_id": cluster_id}
                )
            ],
        }
    )
    coordinator.record_report(report, scan_lane=ScanLane.UNIVERSE)
    hot_only = _report(
        [_fixture("other-hot", kickoff=NOW + timedelta(minutes=10), in_running=True)],
        when=NOW,
        scan_lane=ScanLane.HOT.value,
    )
    coordinator.record_report(hot_only, scan_lane=ScanLane.HOT)
    store = coordinator.fixture_current_state()
    assert store.resolve_canonical_id(cluster_id) == cluster_id
    assert store.resolve_canonical_id(source_id) == cluster_id
    assert store.resolve_canonical_id(decision_id) == cluster_id
    coordinator.reset()


def test_later_universe_status_unsticks_stale_hot_live_pin() -> None:
    live_at = NOW
    kickoff = live_at - timedelta(minutes=10)
    later = live_at + timedelta(hours=3, minutes=2)
    coordinator = LiveRefreshCoordinator()
    coordinator.reset()
    live = _fixture("stale-live", kickoff=kickoff, in_running=True)
    coordinator.record_report(_report([live], when=live_at), scan_lane=ScanLane.HOT)
    store = coordinator.fixture_current_state()
    assert store.hot_identity_scope(live_at) == ["stale-live"]
    detail_live = store.detail("stale-live")
    assert detail_live is not None
    assert detail_live.fixture.in_running is True

    unknown = _fixture("stale-live", kickoff=kickoff, in_running=None)
    coordinator.record_report(_report([unknown], when=later), scan_lane=ScanLane.UNIVERSE)
    assert store.hot_identity_scope(later) == []
    hot_count, universe_count = store.membership_counts(later)
    assert hot_count == 0
    assert universe_count == 0
    detail = store.detail("stale-live", now=later)
    assert detail is None
    assert unknown.in_running is None
    assert unknown.fixture_status is None
    inventory = store.inventory(later)
    assert inventory == []
    radar = store.current_radar_rows(later)
    assert radar == []
    coordinator.reset()


@pytest.mark.asyncio
async def test_failed_universe_chunks_do_not_consume_successful_budget_or_starve_hot() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    live = _fixture("live", kickoff=NOW - timedelta(minutes=5), in_running=True)
    far = _fixture("t6d", kickoff=NOW + timedelta(days=6))
    coordinator.record_report(_report([live, far], when=NOW), scan_lane=ScanLane.UNIVERSE)
    coordinator._next_hot_due = NOW + timedelta(seconds=20)
    coordinator._next_universe_due = NOW
    coordinator._universe_generation_started_at = None
    coordinator._universe_work_used = 0.0
    open_generation = None

    async def fail_runner() -> CollectionReport:
        clock.advance(8)
        raise RuntimeError("provider_timeout")

    first = coordinator.plan_tick(now=clock.now)
    assert first.lane == "universe"
    open_generation = first.universe_generation_id
    with pytest.raises(RuntimeError, match="provider_timeout"):
        await coordinator.run_cycle(
            fail_runner,
            timeout_seconds=first.coordinator_timeout_seconds,
            scan_lane=ScanLane.UNIVERSE,
        )
    assert coordinator._universe_work_used == pytest.approx(0.0)
    assert coordinator.status.universe.generation_work_used_s == pytest.approx(0.0)
    assert coordinator.status.universe.degraded is True
    assert coordinator._universe_generation_started_at is not None
    assert coordinator._universe_retry_at is not None
    assert coordinator._universe_retry_at > clock.now
    assert coordinator.plan_tick(now=clock.now).lane == "idle"
    assert coordinator.plan_tick(now=clock.now).reason == "universe_provider_backoff"

    clock.now = NOW + timedelta(seconds=20)
    hot_plan = coordinator.plan_tick(now=clock.now)
    assert hot_plan.lane == "hot"
    assert "live" in hot_plan.identity_scope

    async def hot_runner() -> CollectionReport:
        return _report([live], when=clock.now, scan_lane=ScanLane.HOT.value)

    await coordinator.run_cycle(
        hot_runner,
        timeout_seconds=hot_plan.coordinator_timeout_seconds,
        scan_lane=ScanLane.HOT,
    )
    coordinator._next_hot_due = clock.now + timedelta(seconds=300)
    clock.now = coordinator._universe_retry_at
    resumed = coordinator.plan_tick(now=clock.now)
    assert resumed.lane == "universe"
    assert resumed.universe_generation_id == open_generation
    assert coordinator._universe_work_used == pytest.approx(0.0)


def test_frontend_auto_refresh_does_not_post_collect_when_server_owns_scans() -> None:
    from pathlib import Path

    scan = (Path(__file__).resolve().parents[2] / "frontend" / "components" / "run-paper-scan.tsx").read_text(
        encoding="utf-8"
    )
    demo = (
        Path(__file__).resolve().parents[2] / "frontend" / "components" / "demo-walkthrough.tsx"
    ).read_text(encoding="utf-8")
    assert "useLiveStatus" in scan
    assert "refreshNow" in scan
    assert "pollLiveStatus" not in scan
    assert "getLiveRefreshStatus" not in scan
    assert "void collectRef.current()" not in scan
    assert "server owns HOT / BACKGROUND / UNIVERSE" in scan
    assert scan.count("runPaperCollection") == 2
    assert "AUTO PAPER CAPTURE ON" in scan
    assert "server_loop_enabled" in demo
    assert "runPaperCollection" in demo


@pytest.mark.asyncio
async def test_explicit_collect_does_not_consume_universe_generation_progress() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    live = _fixture("live", kickoff=NOW - timedelta(minutes=5), in_running=True)
    far = _fixture("t6d", kickoff=NOW + timedelta(days=6))
    coordinator.record_report(_report([live, far], when=NOW), scan_lane=ScanLane.UNIVERSE)
    hot_due = NOW + timedelta(seconds=30)
    universe_due = NOW + timedelta(seconds=180)
    coordinator._next_hot_due = hot_due
    coordinator._next_universe_due = universe_due
    coordinator._universe_generation_started_at = NOW
    coordinator._universe_work_used = 41.0
    coordinator._universe_cursor = "a"
    coordinator._universe_evaluated_ids = {"a"}
    coordinator.status = coordinator.status.model_copy(
        update={
            "universe": coordinator.status.universe.model_copy(
                update={"generation_work_used_s": 41.0, "resume_cursor": "a"}
            )
        }
    )

    async def runner() -> CollectionReport:
        extra = _fixture("extra", kickoff=NOW + timedelta(days=2))
        return _report([extra], when=NOW, scan_lane=ScanLane.UNIVERSE.value)

    await coordinator.run_explicit_collect(runner)
    assert coordinator._universe_work_used == 41.0
    assert coordinator._universe_cursor == "a"
    assert coordinator._universe_evaluated_ids == {"a"}
    assert coordinator._next_hot_due == hot_due
    assert coordinator._next_universe_due == universe_due
    assert coordinator.status.universe.generation_work_used_s == 41.0
    assert coordinator.status.universe.resume_cursor == "a"
    assert coordinator.fixture_current_state().resolve_canonical_id("extra") == "extra"


@pytest.mark.asyncio
async def test_explicit_collect_fails_fast_when_scheduled_lane_is_active() -> None:
    coordinator = LiveRefreshCoordinator()
    coordinator.status = coordinator.status.model_copy(update={"cycle_in_progress": True})
    coordinator._universe_in_progress = True

    async def runner() -> CollectionReport:
        raise AssertionError("explicit collect must not start")

    with pytest.raises(ExplicitCollectBusy, match="UNIVERSE scan in progress"):
        await coordinator.run_explicit_collect(runner)

    client = TestClient(app)
    get_live_refresh_coordinator().reset()
    busy = get_live_refresh_coordinator()
    busy.status = busy.status.model_copy(update={"cycle_in_progress": True})
    busy._universe_in_progress = True
    try:
        response = client.post("/paper/collect", json={"maximum_execution_risk": 60})
        assert response.status_code == 409
        assert "UNIVERSE scan in progress" in response.json()["detail"]
    finally:
        busy.reset()


def test_manual_hot_plan_is_the_scheduled_fast_scan_contract() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    hot = _fixture("t59m", kickoff=NOW + timedelta(minutes=59))
    far = _fixture("t6d", kickoff=NOW + timedelta(days=6))
    coordinator.record_explicit_report(_report([hot, far], when=NOW))
    coordinator._next_hot_due = NOW
    coordinator._next_universe_due = NOW + timedelta(minutes=3)

    scheduled = coordinator.plan_tick(now=NOW)
    manual = coordinator.manual_hot_plan(now=NOW)

    assert scheduled.lane == manual.lane == ScanLane.HOT.value
    assert scheduled.identity_scope == manual.identity_scope == ["t59m"]
    assert scheduled.known_source_events == manual.known_source_events
    assert scheduled.enabled_venues == manual.enabled_venues
    assert scheduled.collector_timeout_seconds == manual.collector_timeout_seconds == 25
    assert scheduled.coordinator_timeout_seconds == manual.coordinator_timeout_seconds == 30
    assert "t6d" not in manual.identity_scope
    assert manual.reason == "manual_hot"


@pytest.mark.asyncio
async def test_manual_hot_refresh_does_not_advance_scheduled_progress() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    hot = _fixture("live", kickoff=NOW - timedelta(minutes=5), in_running=True)
    coordinator.record_explicit_report(_report([hot], when=NOW))
    coordinator._next_hot_due = NOW + timedelta(seconds=10)
    coordinator._next_universe_due = NOW + timedelta(seconds=100)
    coordinator._universe_generation_started_at = NOW - timedelta(seconds=40)
    coordinator._universe_work_used = 41.0
    coordinator._universe_cursor = "prior"
    coordinator._universe_evaluated_ids = {"prior"}
    due_before = (coordinator._next_hot_due, coordinator._next_universe_due)

    async def runner() -> CollectionReport:
        return _report([hot], when=NOW, scan_lane=ScanLane.HOT.value)

    report = await coordinator.run_manual_hot(runner, timeout_seconds=0.5)

    assert report.scan_lane == ScanLane.HOT.value
    assert (coordinator._next_hot_due, coordinator._next_universe_due) == due_before
    assert coordinator._universe_work_used == 41.0
    assert coordinator._universe_cursor == "prior"
    assert coordinator._universe_evaluated_ids == {"prior"}
    assert coordinator.status.cycle_in_progress is False
    assert coordinator.status.hot.cycle_in_progress is False


def test_manual_hot_http_reuses_known_events_without_universe_discovery(monkeypatch) -> None:
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    now = coordinator.now()
    hot = _fixture("manual-hot", kickoff=now + timedelta(minutes=30), last_seen=now)
    far = _fixture("manual-far", kickoff=now + timedelta(days=4), last_seen=now)
    coordinator.record_explicit_report(_report([hot, far], when=now))
    captured: dict[str, object] = {}

    async def fake_collect_report(_kwargs, **kwargs):
        captured.update(kwargs)
        return _report([hot], when=now, scan_lane=ScanLane.HOT.value)

    monkeypatch.setattr(paper_api, "_collect_report", fake_collect_report)
    monkeypatch.setattr(paper_api, "_persist_collection_report", lambda *args, **kwargs: None)
    app.dependency_overrides[paper_api.get_paper_scan_service] = lambda: object()
    app.dependency_overrides[paper_api.get_paper_audit_repository] = lambda: object()
    app.dependency_overrides[get_watchlist_service] = lambda: object()
    try:
        response = TestClient(app).post("/paper/collect/hot", json={"maximum_execution_risk": 60})
    finally:
        app.dependency_overrides.clear()
        coordinator.reset()

    assert response.status_code == 200, response.text
    assert response.json()["scan_diagnostics"]["collection_kind"] == "manual_hot_refresh"
    assert response.json()["scan_diagnostics"]["scheduled_fast_full_unchanged"] is True
    assert response.json()["fixture_markets"] == {}
    assert captured["scan_lane"] == ScanLane.HOT.value
    assert captured["identity_scope"] == ["manual-hot"]
    known = captured["known_source_events"]
    assert isinstance(known, dict)
    assert set(known) == {"manual-hot"}
    assert captured["cycle_timeout_seconds"] == 25
    assert captured["enabled_venues"] == list(coordinator.pending_venues_for(ScanLane.HOT))


def test_manual_hot_http_returns_busy_when_scheduled_lane_owns_coordinator() -> None:
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    coordinator._hot_in_progress = True
    try:
        response = TestClient(app).post("/paper/collect/hot", json={})
    finally:
        coordinator.reset()

    assert response.status_code == 409
    assert "HOT scan in progress" in response.json()["detail"]
