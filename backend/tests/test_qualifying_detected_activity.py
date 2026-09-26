"""Durable QUALIFYING_DETECTED audit. Paper-only. Does not change capture economics.

QUALIFYING is a solver-qualified TRIGGERED episode. PAPER_ELIGIBLE remains the
stronger capture-eligible stay. Operator Activity includes the qualifying
episode even when it never becomes paper eligible and never opens a trade.
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

import asyncio
from unittest.mock import patch

from sports_hedge.api.main import app
from sports_hedge.api.paper import _persist_decision, server_owned_refresh_tick
from sports_hedge.api.watchlist import get_watchlist_service, tracked_markets
from sports_hedge.application.live_refresh import DualCadencePlan, get_live_refresh_coordinator
from sports_hedge.application.collector import CollectionReport, DiscoveredFixture
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.scan_lanes import (
    DEFAULT_HOT_TTL_SECONDS,
    DEFAULT_UNIVERSE_TTL_SECONDS,
    FRESHNESS_RADAR_CURRENT,
    ScanLane,
    freshness_class,
)
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.arbitrage.watchlist.economics import classify_status
from sports_hedge.arbitrage.watchlist.models import (
    OPERATOR_ACTIVITY_EVENT_TYPES,
    LifecycleEventType,
    OpportunityStatus,
    qualifying_lifecycle_event_id,
)
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import (
    WatchlistService,
    _entered_capture_eligible_triggered_episode,
    _entered_qualifying_episode,
)
from sports_hedge.domain.football import MarketFamily
from test_issue395_signal_only_activity_feed import EDGE_080, EDGE_120, OBSERVED, _observation

GROSS = Decimal("0.0492")
EXECUTABLE = Decimal("206.91")
GUARANTEED = Decimal("19.73")


def _qualified(*, eligible: bool = False, **overrides):
    observation = _observation(
        edge=EDGE_120,
        eligible=eligible,
        observed_at=OBSERVED,
        market_id="mkt-mkd",
    )
    observation = observation.model_copy(
        update={
            "canonical_event_id": "evt-mkd-sui",
            "home_team": "North Macedonia",
            "away_team": "Switzerland",
            "market_family": MarketFamily.TOTAL_GOALS,
            "gross_edge": GROSS,
            "limiting_depth_gbp": EXECUTABLE,
            "guaranteed_profit_gbp": GUARANTEED,
            "quote_age_ms": 800,
            "pricing_lane": "background",
            "solver_is_arbitrage": True,
            **overrides,
        }
    )
    return observation


def _qualifying_events(service: WatchlistService, opportunity_id: str):
    return [
        event
        for event in service.activity(opportunity_id=opportunity_id)
        if event.event_type is LifecycleEventType.QUALIFYING_DETECTED
    ]


def test_solver_qualifying_without_paper_eligibility_is_operator_visible() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    watched = service.observe(_qualified())
    assert watched.status is OpportunityStatus.TRIGGERED
    assert watched.capture_eligible is False
    assert watched.is_arbitrage is True
    detected = _qualifying_events(service, watched.opportunity_id)
    assert len(detected) == 1
    event = detected[0]
    assert event.detail == "solver_qualified"
    assert event.capture_eligible is False
    assert event.canonical_event_id == "evt-mkd-sui"
    assert event.canonical_market_id == "mkt-mkd"
    assert event.fixture_label == "North Macedonia v Switzerland"
    assert event.market_family == "total_goals"
    assert event.current_net_edge == EDGE_120
    assert event.trigger_net_edge == Decimal("0.01")
    assert event.gross_edge == GROSS
    assert event.limiting_depth_gbp == EXECUTABLE
    assert event.guaranteed_profit_gbp == GUARANTEED
    assert event.quote_age_ms == 800
    assert event.pricing_lane == "background"
    assert event.venue_pair == "matchbook,kalshi"
    assert event.event_id == qualifying_lifecycle_event_id(
        watched.opportunity_id,
        LifecycleEventType.QUALIFYING_DETECTED,
        1,
    )
    types = {item.event_type for item in service.activity(opportunity_id=watched.opportunity_id)}
    assert LifecycleEventType.PAPER_ELIGIBLE not in types
    assert LifecycleEventType.PAPER_FILL_COMPLETE not in types
    assert [item.event_type for item in service.operator_activity()] == [
        LifecycleEventType.QUALIFYING_DETECTED
    ]


def test_successful_capture_orders_qualifying_before_paper_eligible_and_trade() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    watched = service.observe(_qualified(eligible=True))
    service.record_paper_fill(
        watched.opportunity_id,
        stage=OpportunityStatus.PAPER_FILLING,
        occurred_at=OBSERVED + timedelta(seconds=1),
        detail="paper_fill_attempted_bound_snapshot",
    )
    service.record_paper_fill(
        watched.opportunity_id,
        stage=OpportunityStatus.FILLED,
        occurred_at=OBSERVED + timedelta(seconds=2),
        detail="paper_mode_only",
    )
    chronological = list(reversed(service.activity(opportunity_id=watched.opportunity_id)))
    types = [event.event_type for event in chronological]
    assert types.index(LifecycleEventType.QUALIFYING_DETECTED) < types.index(
        LifecycleEventType.PAPER_ELIGIBLE
    )
    assert types.index(LifecycleEventType.PAPER_ELIGIBLE) < types.index(
        LifecycleEventType.PAPER_FILL_ATTEMPTED
    )
    assert types.index(LifecycleEventType.PAPER_FILL_ATTEMPTED) < types.index(
        LifecycleEventType.PAPER_FILL_COMPLETE
    )
    operator = [event.event_type for event in reversed(service.operator_activity())]
    assert operator == [
        LifecycleEventType.QUALIFYING_DETECTED,
        LifecycleEventType.PAPER_ELIGIBLE,
        LifecycleEventType.PAPER_FILL_COMPLETE,
    ]


def test_repeated_qualifying_observations_are_one_episode_across_restart() -> None:
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository)
    first = service.observe(_qualified())
    service.observe(
        _qualified(observed_at=OBSERVED + timedelta(seconds=5), current_net_edge=Decimal("0.037"))
    )
    restarted = WatchlistService(repository)
    restarted.observe(
        _qualified(observed_at=OBSERVED + timedelta(seconds=30), current_net_edge=Decimal("0.040"))
    )
    detected = _qualifying_events(restarted, first.opportunity_id)
    assert len(detected) == 1
    assert detected[0].event_id.endswith(":qualifying_detected:1")
    assert detected[0].occurred_at == OBSERVED


def test_requalification_after_a_real_gap_is_a_second_episode() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    first = service.observe(_qualified())
    service.observe(
        _qualified(
            observed_at=OBSERVED + timedelta(minutes=1),
            current_net_edge=EDGE_080,
            rejection_reasons=["net_edge_below_threshold"],
            guaranteed_profit_gbp=None,
        )
    )
    again = service.observe(_qualified(observed_at=OBSERVED + timedelta(minutes=5), eligible=False))
    assert again.status is OpportunityStatus.TRIGGERED
    detected = _qualifying_events(service, first.opportunity_id)
    assert [event.occurred_at for event in reversed(detected)] == [
        OBSERVED,
        OBSERVED + timedelta(minutes=5),
    ]
    assert [event.event_id for event in reversed(detected)] == [
        qualifying_lifecycle_event_id(
            first.opportunity_id, LifecycleEventType.QUALIFYING_DETECTED, 1
        ),
        qualifying_lifecycle_event_id(
            first.opportunity_id, LifecycleEventType.QUALIFYING_DETECTED, 2
        ),
    ]
    lost = [
        event
        for event in service.activity(opportunity_id=first.opportunity_id)
        if event.event_type is LifecycleEventType.QUALIFYING_LOST
    ]
    assert len(lost) == 1
    assert lost[0].event_id == qualifying_lifecycle_event_id(
        first.opportunity_id,
        LifecycleEventType.QUALIFYING_LOST,
        1,
    )


def test_economic_miss_keeps_detection_and_explains_the_ending() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    watched = service.observe(_qualified())
    service.observe(
        _qualified(
            observed_at=OBSERVED + timedelta(seconds=20),
            current_net_edge=EDGE_080,
            rejection_reasons=["net_edge_below_threshold"],
            guaranteed_profit_gbp=None,
            quote_age_ms=3500,
        )
    )
    audit = service.activity(opportunity_id=watched.opportunity_id)
    types = [event.event_type for event in audit]
    assert LifecycleEventType.QUALIFYING_DETECTED in types
    assert LifecycleEventType.QUALIFYING_LOST in types
    assert LifecycleEventType.TRIGGER_LOST_BEFORE_FILL in types
    assert LifecycleEventType.PAPER_ELIGIBLE not in types
    assert LifecycleEventType.PAPER_FILL_COMPLETE not in types
    lost = next(event for event in audit if event.event_type is LifecycleEventType.QUALIFYING_LOST)
    assert "net edge below threshold" in (lost.detail or "")
    assert lost.current_net_edge == EDGE_080
    assert lost.quote_age_ms == 3500
    operator = [event.event_type for event in service.operator_activity()]
    assert operator[0] is LifecycleEventType.QUALIFYING_LOST
    assert LifecycleEventType.QUALIFYING_DETECTED in operator
    assert LifecycleEventType.TRIGGER_LOST_BEFORE_FILL not in operator


def test_capture_rejection_keeps_qualifying_detection_and_rejection_audit() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    watched = service.observe(_qualified(eligible=True))
    service.record_paper_fill(
        watched.opportunity_id,
        stage=OpportunityStatus.PAPER_FILLING,
        occurred_at=OBSERVED + timedelta(seconds=1),
    )
    service.record_paper_fill_rejection(
        watched.opportunity_id,
        occurred_at=OBSERVED + timedelta(seconds=2),
        detail="opening_leg_failed:kalshi",
        reject_triggered=True,
    )
    types = {event.event_type for event in service.activity(opportunity_id=watched.opportunity_id)}
    assert LifecycleEventType.QUALIFYING_DETECTED in types
    assert LifecycleEventType.PAPER_ELIGIBLE in types
    assert LifecycleEventType.PAPER_FILL_REJECTED in types
    assert LifecycleEventType.QUALIFYING_LOST not in types
    assert LifecycleEventType.PAPER_FILL_COMPLETE not in types


def test_operator_activity_api_includes_qualifying_detected() -> None:
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    try:
        service.observe(_qualified())
        response = client.get(
            "/paper/watchlist/activity",
            params={"limit": 20, "operator_signal": True},
        )
        assert response.status_code == 200
        payload = response.json()
        assert [item["event_type"] for item in payload] == ["qualifying_detected"]
        event = payload[0]
        assert event["fixture_label"] == "North Macedonia v Switzerland"
        assert event["market_family"] == "total_goals"
        assert event["gross_edge"] == str(GROSS)
        assert event["limiting_depth_gbp"] == str(EXECUTABLE)
        assert event["guaranteed_profit_gbp"] == str(GUARANTEED)
        assert event["pricing_lane"] == "background"
        assert event["venue_pair"] == "matchbook,kalshi"
        assert event["capture_eligible"] is False
        assert event["quote_age_ms"] == 800
    finally:
        app.dependency_overrides.clear()
        repository.close()


def test_paper_eligible_still_requires_capture_eligible_triggered_episode() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    watched = service.observe(_qualified(eligible=False))
    assert _entered_qualifying_episode(None, watched) is True
    assert _entered_capture_eligible_triggered_episode(None, watched) is False
    assert LifecycleEventType.PAPER_ELIGIBLE not in {
        event.event_type for event in service.activity(opportunity_id=watched.opportunity_id)
    }
    classify_src = inspect.getsource(classify_status)
    assert "PAPER_ELIGIBLE" not in classify_src
    assert "QUALIFYING_DETECTED" not in classify_src


def test_non_solver_edge_does_not_open_a_qualifying_episode() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    watched = service.observe(_qualified(solver_is_arbitrage=False))
    assert watched.status is not OpportunityStatus.TRIGGERED
    assert service.operator_activity() == []


def test_qualifying_is_emitted_from_watchlist_observe_before_capture() -> None:
    source = inspect.getsource(_persist_decision)
    observe_at = source.index("observe_paper_decision")
    capture_at = source.index("persist_triggered_chain")
    assert observe_at < capture_at
    assert "pricing_lane=pricing_lane" in source


def test_radar_ttl_expiry_closes_qualifying_episode_without_a_new_observation() -> None:
    """Radar TTL drop is an expiry close, not an economic qualifying_lost.

    Quote age inside the radar TTL stays radar_current and does not close the
    episode. One qualifying_expired is written per open episode. Status stays
    TRIGGERED so a later solver-qualified observation can open a new episode.
    """

    service = WatchlistService(SqliteWatchlistRepository())
    watched = service.observe(_qualified(pricing_lane="hot", quote_age_ms=50_000))
    opportunity_id = watched.opportunity_id
    assert opportunity_id == "watch:mkt-mkd"
    assert watched.status is OpportunityStatus.TRIGGERED
    detected = _qualifying_events(service, opportunity_id)
    assert len(detected) == 1
    original = detected[0]

    inside_ttl = OBSERVED + timedelta(seconds=10)
    assert (
        freshness_class(
            lane=ScanLane.HOT,
            last_scanned_at=watched.last_seen_at,
            now=inside_ttl,
            quote_age_ms=50_000,
            max_quote_age_ms=service.max_quote_age_ms,
        )
        == FRESHNESS_RADAR_CURRENT
    )
    assert (
        service.note_qualifying_radar_expiry(
            inside_ttl,
            hot_ttl_seconds=DEFAULT_HOT_TTL_SECONDS,
            universe_ttl_seconds=DEFAULT_UNIVERSE_TTL_SECONDS,
        )
        == []
    )

    store = FixtureCurrentStateStore()
    kickoff = OBSERVED + timedelta(minutes=30)
    fixture = DiscoveredFixture(
        source_event_id="src-mkd",
        canonical_event_id="evt-mkd-sui",
        home_team="North Macedonia",
        away_team="Switzerland",
        competition="World Cup Qualifying",
        kickoff_utc=kickoff,
        last_seen_at=OBSERVED,
        market_evaluation_state="evaluated",
    )
    report = CollectionReport(
        started_at=OBSERVED,
        completed_at=OBSERVED,
        discovered_fixtures=[fixture],
        paper_decisions=[
            PaperScanDecision(
                canonical_event_id="evt-mkd-sui",
                canonical_market_id="mkt-mkd",
                fixture_canonical_event_id="evt-mkd-sui",
                market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=[]),
                scanned_at=OBSERVED,
                eligible_for_paper_simulation=False,
            )
        ],
        scan_lane=ScanLane.HOT.value,
    )
    store.upsert_from_report(report, scan_lane=ScanLane.HOT, now=OBSERVED)
    assert opportunity_id in store.current_tracked_opportunity_ids(inside_ttl)
    expired_at = OBSERVED + timedelta(seconds=DEFAULT_HOT_TTL_SECONDS)
    assert opportunity_id not in store.current_tracked_opportunity_ids(expired_at)
    persisted = service.repository.get(opportunity_id)
    assert persisted is not None
    assert persisted.status is OpportunityStatus.TRIGGERED
    assert (
        service.repository.count_events(opportunity_id, LifecycleEventType.QUALIFYING_EXPIRED) == 0
    )

    closed = service.note_qualifying_radar_expiry(
        expired_at,
        hot_ttl_seconds=DEFAULT_HOT_TTL_SECONDS,
        universe_ttl_seconds=DEFAULT_UNIVERSE_TTL_SECONDS,
    )
    assert len(closed) == 1
    expiry = closed[0]
    assert expiry.event_type is LifecycleEventType.QUALIFYING_EXPIRED
    assert expiry.detail == "radar expired"
    assert expiry.status is OpportunityStatus.TRIGGERED
    assert expiry.pricing_lane == "hot"
    assert expiry.gross_edge == GROSS
    assert expiry.event_id == qualifying_lifecycle_event_id(
        opportunity_id,
        LifecycleEventType.QUALIFYING_EXPIRED,
        1,
    )
    assert service.repository.get(opportunity_id).status is OpportunityStatus.TRIGGERED
    assert [item.event_type for item in service.operator_activity(opportunity_id=opportunity_id)] == [
        LifecycleEventType.QUALIFYING_EXPIRED,
        LifecycleEventType.QUALIFYING_DETECTED,
    ]
    assert (
        service.note_qualifying_radar_expiry(
            expired_at,
            hot_ttl_seconds=DEFAULT_HOT_TTL_SECONDS,
            universe_ttl_seconds=DEFAULT_UNIVERSE_TTL_SECONDS,
        )
        == []
    )
    still = _qualifying_events(service, opportunity_id)
    assert len(still) == 1
    assert still[0].event_id == original.event_id
    assert still[0].gross_edge == original.gross_edge == GROSS
    assert still[0].limiting_depth_gbp == EXECUTABLE
    assert still[0].guaranteed_profit_gbp == GUARANTEED

    requalified_at = expired_at + timedelta(seconds=5)
    service.observe(
        _qualified(pricing_lane="hot", quote_age_ms=800, observed_at=requalified_at)
    )
    detected_again = _qualifying_events(service, opportunity_id)
    assert len(detected_again) == 2
    assert detected_again[0].event_id == qualifying_lifecycle_event_id(
        opportunity_id,
        LifecycleEventType.QUALIFYING_DETECTED,
        2,
    )
    assert (
        service.note_qualifying_radar_expiry(
            expired_at,
            hot_ttl_seconds=DEFAULT_HOT_TTL_SECONDS,
            universe_ttl_seconds=DEFAULT_UNIVERSE_TTL_SECONDS,
        )
        == []
    )
    assert (
        service.repository.count_events(opportunity_id, LifecycleEventType.QUALIFYING_EXPIRED) == 1
    )
    second_expiry = requalified_at + timedelta(seconds=DEFAULT_HOT_TTL_SECONDS)
    again = service.note_qualifying_radar_expiry(
        second_expiry,
        hot_ttl_seconds=DEFAULT_HOT_TTL_SECONDS,
        universe_ttl_seconds=DEFAULT_UNIVERSE_TTL_SECONDS,
    )
    assert len(again) == 1
    assert again[0].event_id == qualifying_lifecycle_event_id(
        opportunity_id,
        LifecycleEventType.QUALIFYING_EXPIRED,
        2,
    )
    assert (
        service.note_qualifying_radar_expiry(
            second_expiry,
            hot_ttl_seconds=DEFAULT_HOT_TTL_SECONDS,
            universe_ttl_seconds=DEFAULT_UNIVERSE_TTL_SECONDS,
        )
        == []
    )
    assert expiry.occurred_at == expired_at
    tracked_source = inspect.getsource(tracked_markets)
    assert "note_qualifying_radar_expiry" not in tracked_source
    assert "note_expired_qualifying_episodes" not in tracked_source
    tick_source = inspect.getsource(server_owned_refresh_tick)
    note_at = tick_source.index("note_expired_qualifying_episodes_on_tick")
    assert note_at < tick_source.index("run_price_engine_slice")
    assert note_at < tick_source.index("_collect_report(")


def test_server_owned_tick_persists_qualifying_expired_without_tracked_read() -> None:
    """A-F: the refresh tick closes the episode. The tracked read is not involved."""

    asyncio.run(_server_owned_tick_persists_qualifying_expired_without_tracked_read())


async def _server_owned_tick_persists_qualifying_expired_without_tracked_read() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    watched = service.observe(_qualified(pricing_lane="hot", quote_age_ms=50_000))
    plain = service.observe(
        _qualified(
            solver_is_arbitrage=False,
            canonical_market_id="mkt-plain",
            quote_age_ms=50_000,
        )
    )
    coordinator = get_live_refresh_coordinator()
    previous_clock = coordinator._clock
    previous_store = coordinator._catalogue_store
    previous_stopped = coordinator._operator_scanner_stopped
    try:
        coordinator._operator_scanner_stopped = False
        coordinator._catalogue_store = object()
        hot_ttl = int(coordinator.radar_horizon_kwargs()["hot_ttl_seconds"])
        boundary = OBSERVED + timedelta(seconds=hot_ttl)

        async def tick_at(when) -> None:
            coordinator._clock = lambda: when
            with (
                patch(
                    "sports_hedge.api.watchlist.get_watchlist_repository",
                    lambda: service.repository,
                ),
                patch(
                    "sports_hedge.api.watchlist.get_watchlist_service",
                    lambda repository=None: service,
                ),
            ):
                await server_owned_refresh_tick(DualCadencePlan(lane="idle", reason="waiting"))

        await tick_at(OBSERVED + timedelta(seconds=10))
        assert (
            service.repository.count_events(
                watched.opportunity_id, LifecycleEventType.QUALIFYING_EXPIRED
            )
            == 0
        )

        late = boundary + timedelta(seconds=15)
        await tick_at(late)
        closed = [
            event
            for event in service.activity(opportunity_id=watched.opportunity_id)
            if event.event_type is LifecycleEventType.QUALIFYING_EXPIRED
        ]
        assert len(closed) == 1
        assert closed[0].detail == "radar expired"
        assert closed[0].status is OpportunityStatus.TRIGGERED
        assert closed[0].occurred_at == boundary
        assert service.repository.get(watched.opportunity_id).status is OpportunityStatus.TRIGGERED
        assert LifecycleEventType.QUALIFYING_EXPIRED in {
            event.event_type
            for event in service.operator_activity(opportunity_id=watched.opportunity_id)
        }

        await tick_at(late + timedelta(seconds=1))
        assert (
            service.repository.count_events(
                watched.opportunity_id, LifecycleEventType.QUALIFYING_EXPIRED
            )
            == 1
        )

        requalified_at = late + timedelta(seconds=5)
        service.observe(_qualified(pricing_lane="hot", quote_age_ms=800, observed_at=requalified_at))
        assert len(_qualifying_events(service, watched.opportunity_id)) == 2
        await tick_at(late)
        assert (
            service.repository.count_events(
                watched.opportunity_id, LifecycleEventType.QUALIFYING_EXPIRED
            )
            == 1
        )
        second_boundary = requalified_at + timedelta(seconds=hot_ttl)
        await tick_at(second_boundary + timedelta(seconds=1))
        expired = [
            event
            for event in service.activity(opportunity_id=watched.opportunity_id)
            if event.event_type is LifecycleEventType.QUALIFYING_EXPIRED
        ]
        assert [event.occurred_at for event in reversed(expired)] == [boundary, second_boundary]
        await tick_at(second_boundary + timedelta(seconds=30))
        assert (
            service.repository.count_events(
                watched.opportunity_id, LifecycleEventType.QUALIFYING_EXPIRED
            )
            == 2
        )

        plain_types = {
            event.event_type for event in service.activity(opportunity_id=plain.opportunity_id)
        }
        assert LifecycleEventType.QUALIFYING_EXPIRED not in plain_types
        assert LifecycleEventType.EXPIRED not in plain_types
        assert service.operator_activity(opportunity_id=plain.opportunity_id) == []
        assert "note_qualifying_radar_expiry" not in inspect.getsource(tracked_markets)
    finally:
        coordinator._clock = previous_clock
        coordinator._catalogue_store = previous_store
        coordinator._operator_scanner_stopped = previous_stopped


def test_generic_expired_stays_hidden_unless_it_closes_a_qualifying_episode() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    watched = service.observe(
        _qualified(solver_is_arbitrage=False, canonical_market_id="mkt-plain")
    )
    assert watched.status is not OpportunityStatus.TRIGGERED
    service.expire(watched.opportunity_id, occurred_at=OBSERVED, detail="watchlist expired")
    types = [event.event_type for event in service.activity(opportunity_id=watched.opportunity_id)]
    assert LifecycleEventType.EXPIRED in types
    assert LifecycleEventType.QUALIFYING_EXPIRED not in types
    assert service.operator_activity(opportunity_id=watched.opportunity_id) == []
    assert LifecycleEventType.EXPIRED not in OPERATOR_ACTIVITY_EVENT_TYPES
