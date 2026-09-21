"""Issue #412: durable Paper eligible operator signal + unfiltered history.

PAPER / read-only. No venue writes, extra provider calls, or capture-path changes.
Does not backfill guessed historical paper_eligible rows.
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.arbitrage.watchlist.models import (
    OPERATOR_ACTIVITY_EVENT_TYPES,
    LifecycleEventType,
    OpportunityLifecycleEvent,
    OpportunityStatus,
    attempt_id_from_lifecycle_event,
    canonical_event_id_from_hot_opportunity_id,
)
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import (
    WatchlistService,
    _entered_capture_eligible_triggered_episode,
    _episode_capture_eligible,
)
from sports_hedge.arbitrage.watchlist.economics import classify_status
from test_issue395_signal_only_activity_feed import (
    EDGE_080,
    EDGE_120,
    OBSERVED,
    _observation,
)


def test_first_triggered_capture_eligible_observation_emits_paper_eligible_once() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    first = service.observe(_observation(edge=EDGE_120, eligible=True, observed_at=OBSERVED))
    assert first.status is OpportunityStatus.TRIGGERED
    assert first.capture_eligible is True
    events = service.activity(opportunity_id=first.opportunity_id)
    eligible = [event for event in events if event.event_type is LifecycleEventType.PAPER_ELIGIBLE]
    assert len(eligible) == 1
    event = eligible[0]
    assert event.detail == "capture_eligible_triggered"
    assert event.capture_eligible is True
    assert event.opportunity_id == first.opportunity_id
    assert event.canonical_event_id == "evt-signal"
    assert event.canonical_market_id == "mkt-signal"
    assert event.fixture_label == "Brentford v Chelsea"
    assert event.market_family == "both_teams_to_score"
    assert event.current_net_edge == EDGE_120
    assert event.distance_to_trigger_pp is not None
    assert event.occurred_at == OBSERVED
    assert service.operator_activity()[0].event_type is LifecycleEventType.PAPER_ELIGIBLE


def test_capture_ineligible_trigger_does_not_emit_paper_eligible() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    triggered = service.observe(_observation(edge=EDGE_120, eligible=False, observed_at=OBSERVED))
    assert triggered.status is OpportunityStatus.TRIGGERED
    assert triggered.capture_eligible is False
    types = {event.event_type for event in service.activity(opportunity_id=triggered.opportunity_id)}
    assert LifecycleEventType.TRIGGER_CROSSED in types
    assert LifecycleEventType.PAPER_ELIGIBLE not in types
    assert service.operator_activity() == []


def test_false_to_true_capture_eligibility_while_triggered_emits_once() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    first = service.observe(_observation(edge=EDGE_120, eligible=False, observed_at=OBSERVED))
    assert first.capture_eligible is False
    second = service.observe(
        _observation(edge=EDGE_120, eligible=True, observed_at=OBSERVED + timedelta(seconds=2))
    )
    assert second.status is OpportunityStatus.TRIGGERED
    assert second.capture_eligible is True
    eligible = [
        event
        for event in service.activity(opportunity_id=second.opportunity_id)
        if event.event_type is LifecycleEventType.PAPER_ELIGIBLE
    ]
    assert len(eligible) == 1
    assert eligible[0].occurred_at == OBSERVED + timedelta(seconds=2)
    assert eligible[0].canonical_market_id == "mkt-signal"
    assert [event.event_type for event in service.operator_activity()] == [
        LifecycleEventType.PAPER_ELIGIBLE
    ]


def test_repeated_eligible_reprices_do_not_duplicate_paper_eligible() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    first = service.observe(_observation(edge=EDGE_120, eligible=True, observed_at=OBSERVED))
    service.observe(
        _observation(edge=Decimal("0.013"), eligible=True, observed_at=OBSERVED + timedelta(seconds=3))
    )
    service.observe(
        _observation(edge=EDGE_120, eligible=True, observed_at=OBSERVED + timedelta(seconds=6))
    )
    eligible = [
        event
        for event in service.activity(opportunity_id=first.opportunity_id)
        if event.event_type is LifecycleEventType.PAPER_ELIGIBLE
    ]
    assert len(eligible) == 1
    assert eligible[0].occurred_at == OBSERVED


def test_leave_and_reenter_eligible_episode_can_emit_again() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    first = service.observe(_observation(edge=EDGE_120, eligible=True, observed_at=OBSERVED))
    service.observe(
        _observation(
            edge=EDGE_080,
            rejection_reasons=["net_edge_below_threshold"],
            observed_at=OBSERVED + timedelta(seconds=4),
        )
    )
    reentered = service.observe(
        _observation(edge=EDGE_120, eligible=True, observed_at=OBSERVED + timedelta(seconds=8))
    )
    assert reentered.status is OpportunityStatus.TRIGGERED
    assert reentered.capture_eligible is True
    eligible = [
        event
        for event in service.activity(opportunity_id=first.opportunity_id)
        if event.event_type is LifecycleEventType.PAPER_ELIGIBLE
    ]
    assert [event.occurred_at for event in reversed(eligible)] == [
        OBSERVED,
        OBSERVED + timedelta(seconds=8),
    ]
    operator = [event.event_type for event in service.operator_activity()]
    assert operator.count(LifecycleEventType.PAPER_ELIGIBLE) == 2
    assert LifecycleEventType.TRIGGER_LOST_BEFORE_FILL in operator


def test_operator_activity_includes_paper_eligible_and_hides_trigger_crossed() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    triggered = service.observe(_observation(edge=EDGE_120, eligible=True, observed_at=OBSERVED))
    operator = service.operator_activity()
    assert [event.event_type for event in operator] == [LifecycleEventType.PAPER_ELIGIBLE]
    assert LifecycleEventType.PAPER_ELIGIBLE in OPERATOR_ACTIVITY_EVENT_TYPES
    audit = {event.event_type for event in service.activity(opportunity_id=triggered.opportunity_id)}
    assert LifecycleEventType.TRIGGER_CROSSED in audit
    assert LifecycleEventType.CANDIDATE_FIRST_SEEN in audit
    assert LifecycleEventType.TRIGGER_CROSSED not in {event.event_type for event in operator}


def test_unfiltered_opportunity_history_keeps_hidden_lifecycle_noise() -> None:
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    try:
        first = service.observe(_observation(edge=EDGE_120, eligible=True, observed_at=OBSERVED))
        service.observe(
            _observation(
                edge=EDGE_080,
                rejection_reasons=["net_edge_below_threshold"],
                observed_at=OBSERVED + timedelta(seconds=4),
            )
        )
        history = client.get(
            "/paper/watchlist/activity",
            params={"limit": 50, "opportunity_id": first.opportunity_id},
        )
        assert history.status_code == 200
        types = [item["event_type"] for item in history.json()]
        assert "paper_eligible" in types
        assert "trigger_crossed" in types
        assert "candidate_first_seen" in types
        assert "trigger_lost_before_fill" in types
        operator = client.get(
            "/paper/watchlist/activity",
            params={"limit": 50, "operator_signal": True},
        )
        assert [item["event_type"] for item in operator.json()] == [
            "trigger_lost_before_fill",
            "paper_eligible",
        ]
    finally:
        app.dependency_overrides.clear()
        repository.close()


def test_paper_eligible_transition_helper_is_episode_scoped() -> None:
    watching = WatchlistService(SqliteWatchlistRepository()).observe(
        _observation(edge=EDGE_080, rejection_reasons=["net_edge_below_threshold"])
    )
    triggered_ineligible = watching.model_copy(
        update={"status": OpportunityStatus.TRIGGERED, "capture_eligible": False}
    )
    triggered_eligible = watching.model_copy(
        update={"status": OpportunityStatus.TRIGGERED, "capture_eligible": True}
    )
    assert _entered_capture_eligible_triggered_episode(None, triggered_eligible) is True
    assert _entered_capture_eligible_triggered_episode(None, triggered_ineligible) is False
    assert (
        _entered_capture_eligible_triggered_episode(triggered_ineligible, triggered_eligible)
        is True
    )
    assert (
        _entered_capture_eligible_triggered_episode(triggered_eligible, triggered_eligible)
        is False
    )
    assert _entered_capture_eligible_triggered_episode(watching, triggered_eligible) is True


def test_capture_eligibility_and_observe_path_are_unchanged() -> None:
    observe_src = inspect.getsource(WatchlistService._observe_locked)
    episode_src = inspect.getsource(_episode_capture_eligible)
    append_src = inspect.getsource(WatchlistService._append_lifecycle)
    classify_src = inspect.getsource(classify_status)
    assert "eligible_for_paper_simulation" in episode_src
    assert "place_order" not in observe_src
    assert "cancel_order" not in append_src
    assert "PAPER_ELIGIBLE" in append_src
    assert "capture_eligible_triggered" in append_src
    assert "_entered_capture_eligible_triggered_episode" in append_src
    assert "PAPER_ELIGIBLE" not in classify_src


def test_same_timestamp_first_triggered_eligible_history_is_append_order() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    first = service.observe(_observation(edge=EDGE_120, eligible=True, observed_at=OBSERVED))
    events = service.activity(opportunity_id=first.opportunity_id)
    same_ts = [event for event in events if event.occurred_at == OBSERVED]
    assert [event.event_type for event in same_ts[:3]] == [
        LifecycleEventType.PAPER_ELIGIBLE,
        LifecycleEventType.TRIGGER_CROSSED,
        LifecycleEventType.CANDIDATE_FIRST_SEEN,
    ]
    seqs = [event.append_seq for event in same_ts[:3]]
    assert all(seq is not None for seq in seqs)
    assert seqs == sorted(seqs, reverse=True)
    chronological = list(reversed(same_ts[:3]))
    assert [event.event_type for event in chronological] == [
        LifecycleEventType.CANDIDATE_FIRST_SEEN,
        LifecycleEventType.TRIGGER_CROSSED,
        LifecycleEventType.PAPER_ELIGIBLE,
    ]


def test_list_events_same_timestamp_uses_append_seq_not_event_id() -> None:
    repository = SqliteWatchlistRepository()
    try:
        for event_id, event_type in (
            ("zzz-first-appended", LifecycleEventType.CANDIDATE_FIRST_SEEN),
            ("mmm-second-appended", LifecycleEventType.TRIGGER_CROSSED),
            ("aaa-third-appended", LifecycleEventType.PAPER_ELIGIBLE),
        ):
            repository.append_event(
                OpportunityLifecycleEvent(
                    event_id=event_id,
                    opportunity_id="watch:mkt-1",
                    occurred_at=OBSERVED,
                    event_type=event_type,
                    status=OpportunityStatus.TRIGGERED,
                    canonical_event_id="evt-signal",
                    canonical_market_id="mkt-1",
                    capture_eligible=True,
                )
            )
        events = repository.list_events(opportunity_id="watch:mkt-1")
        assert [event.event_id for event in events] == [
            "aaa-third-appended",
            "mmm-second-appended",
            "zzz-first-appended",
        ]
        lexical_desc = sorted((event.event_id for event in events), reverse=True)
        assert lexical_desc == [
            "zzz-first-appended",
            "mmm-second-appended",
            "aaa-third-appended",
        ]
        assert [event.event_id for event in events] != lexical_desc
        assert [event.append_seq for event in events] == sorted(
            (event.append_seq for event in events), reverse=True
        )
    finally:
        repository.close()


def test_hot_promotion_history_joins_later_market_paper_lifecycle() -> None:
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: OBSERVED)
    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    try:
        hot = service.record_hot_promotion(
            canonical_event_id="evt-signal",
            occurred_at=OBSERVED,
            episode=1,
            fixture_label="Brentford v Chelsea",
            market_family="both_teams_to_score",
            pricing_lane="background",
            current_net_edge=EDGE_080,
            distance_to_trigger_pp=Decimal("0.2000"),
        )
        market = service.observe(
            _observation(edge=EDGE_120, eligible=True, observed_at=OBSERVED + timedelta(seconds=2))
        )
        service.record_paper_fill(
            market.opportunity_id,
            stage=OpportunityStatus.PAPER_FILLING,
            occurred_at=OBSERVED + timedelta(seconds=3),
        )
        service.record_paper_fill_rejection(
            market.opportunity_id,
            occurred_at=OBSERVED + timedelta(seconds=4),
            detail="opening_leg_failed:kalshi",
            reject_triggered=True,
        )
        hot_id = hot.opportunity_id
        assert hot_id == "hot:evt-signal"
        assert canonical_event_id_from_hot_opportunity_id(hot_id) == "evt-signal"
        fixture_only = repository.list_events(opportunity_id=hot_id, limit=50)
        assert {event.event_type for event in fixture_only} == {
            LifecycleEventType.PROMOTED_TO_HOT
        }
        joined = service.activity(opportunity_id=hot_id)
        types = {event.event_type for event in joined}
        assert LifecycleEventType.PROMOTED_TO_HOT in types
        assert LifecycleEventType.PAPER_ELIGIBLE in types
        assert LifecycleEventType.TRIGGER_CROSSED in types
        assert LifecycleEventType.PAPER_FILL_ATTEMPTED in types
        assert LifecycleEventType.PAPER_FILL_REJECTED in types
        eligible = next(
            event for event in joined if event.event_type is LifecycleEventType.PAPER_ELIGIBLE
        )
        assert eligible.opportunity_id == market.opportunity_id
        assert eligible.canonical_event_id == "evt-signal"
        assert eligible.canonical_market_id == "mkt-signal"
        market_history = service.activity(
            opportunity_id=market.opportunity_id,
            canonical_event_id="evt-signal",
        )
        market_types = {event.event_type for event in market_history}
        assert LifecycleEventType.PROMOTED_TO_HOT in market_types
        assert LifecycleEventType.PAPER_ELIGIBLE in market_types
        response = client.get(
            "/paper/watchlist/activity",
            params={
                "limit": 50,
                "opportunity_id": hot_id,
                "canonical_event_id": "evt-signal",
            },
        )
        assert response.status_code == 200
        payload_types = {item["event_type"] for item in response.json()}
        assert "promoted_to_hot" in payload_types
        assert "paper_eligible" in payload_types
        assert "paper_fill_rejected" in payload_types
        rejected = next(
            item for item in response.json() if item["event_type"] == "paper_fill_rejected"
        )
        assert rejected["canonical_event_id"] == "evt-signal"
        assert rejected["canonical_market_id"] == "mkt-signal"
        assert rejected["attempt_id"]
        assert rejected["opportunity_id"] == market.opportunity_id
    finally:
        app.dependency_overrides.clear()
        repository.close()


def test_paper_fill_lifecycle_exposes_attempt_id() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    triggered = service.observe(_observation(edge=EDGE_120, eligible=True, observed_at=OBSERVED))
    service.record_paper_fill(
        triggered.opportunity_id,
        stage=OpportunityStatus.PAPER_FILLING,
        occurred_at=OBSERVED + timedelta(seconds=1),
        detail="paper_fill_attempted_bound_snapshot",
    )
    service.record_paper_fill(
        triggered.opportunity_id,
        stage=OpportunityStatus.FILLED,
        occurred_at=OBSERVED + timedelta(seconds=2),
        detail="paper_mode_only",
    )
    events = service.activity(opportunity_id=triggered.opportunity_id)
    attempted = next(
        event for event in events if event.event_type is LifecycleEventType.PAPER_FILL_ATTEMPTED
    )
    complete = next(
        event for event in events if event.event_type is LifecycleEventType.PAPER_FILL_COMPLETE
    )
    assert attempted.attempt_id
    assert complete.attempt_id == attempted.attempt_id
    assert attempted.event_id == (
        f"{triggered.opportunity_id}:paper_fill_attempted:{attempted.attempt_id}"
    )
    assert complete.event_id == (
        f"{triggered.opportunity_id}:paper_fill_complete:{complete.attempt_id}"
    )
    assert attempted.attempt_id in (attempted.detail or "")
    parsed = attempt_id_from_lifecycle_event(
        opportunity_id=attempted.opportunity_id,
        event_type=attempted.event_type,
        event_id=attempted.event_id,
        stored_attempt_id=None,
    )
    assert parsed == attempted.attempt_id
    other = service.observe(
        _observation(market_id="mkt-reject", edge=EDGE_120, eligible=True, observed_at=OBSERVED)
    )
    service.record_paper_fill(
        other.opportunity_id,
        stage=OpportunityStatus.PAPER_FILLING,
        occurred_at=OBSERVED + timedelta(seconds=1),
    )
    service.record_paper_fill_rejection(
        other.opportunity_id,
        occurred_at=OBSERVED + timedelta(seconds=2),
        detail="opening_leg_failed:kalshi",
        reject_triggered=True,
    )
    rejected = next(
        event
        for event in service.activity(opportunity_id=other.opportunity_id)
        if event.event_type is LifecycleEventType.PAPER_FILL_REJECTED
    )
    assert rejected.attempt_id
    assert rejected.event_id.endswith(f":{rejected.attempt_id}")
    assert attempt_id_from_lifecycle_event(
        opportunity_id="watch:mkt-1",
        event_type=LifecycleEventType.PAPER_ELIGIBLE,
        event_id="uuid-eligible",
        stored_attempt_id=None,
    ) is None
    assert canonical_event_id_from_hot_opportunity_id("watch:mkt-1") is None
