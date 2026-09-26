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

from sports_hedge.api.main import app
from sports_hedge.api.paper import _persist_decision
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.arbitrage.watchlist.economics import classify_status
from sports_hedge.arbitrage.watchlist.models import (
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
