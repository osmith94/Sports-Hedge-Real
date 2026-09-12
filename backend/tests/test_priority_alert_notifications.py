from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

import sports_hedge.notifications as notifications
from sports_hedge.api.main import app
from sports_hedge.api.notifications import get_notification_router
from sports_hedge.arbitrage.priority_alerts.models import (
    OpportunitySurvivability,
    OperatorAction,
    PriorityAlertState,
    PrioritySeverity,
    SurvivabilityConfidence,
    VolatilityRegime,
)
from sports_hedge.config import Settings
from sports_hedge.notifications.adapters import RecordingAdapter
from sports_hedge.notifications.canonical import (
    CanonicalHedgeRevalidationAdapter,
    CanonicalRecommendationAdapter,
    PriorityAlertNotificationAdapter,
    notification_adapter_from_priority_alert,
    priority_alert_deep_link,
)
from sports_hedge.notifications.models import (
    NotificationChannel,
    NotificationEventType,
)
from sports_hedge.notifications.repository import SqliteNotificationRepository
from sports_hedge.notifications.router import PriorityAlertNotificationRouter
from sports_hedge.venues.base import ReadOnlyVenue


NOW = datetime(2026, 9, 11, 21, 0, tzinfo=UTC)


def _alert(
    *,
    alert_id: str = "alert-1",
    opportunity_id: str = "opp:newcastle-chelsea-match-result",
    severity: str = "PRIORITY",
    opened_at: datetime = NOW,
    expired_at: datetime | None = None,
    edge: str = "0.098",
    size: str = "475",
    profit: str = "46.55",
    freshness_ms: int = 180,
    lifecycle_state: str = "OPEN",
    operator_action: str = "PREPARE_MANUAL_OVERRIDE",
    hedge_revalidation: CanonicalHedgeRevalidationAdapter | None = None,
    survivability: OpportunitySurvivability | None = None,
) -> PriorityAlertNotificationAdapter:
    return PriorityAlertNotificationAdapter(
        alert_id=alert_id,
        opportunity_id=opportunity_id,
        canonical_event_id="evt:newcastle-chelsea-2026-09-20",
        canonical_market_id="evt:newcastle-chelsea-2026-09-20:match_result",
        severity=PrioritySeverity(severity),
        lifecycle_state=PriorityAlertState(lifecycle_state),
        operator_action=OperatorAction(operator_action),
        net_guaranteed_edge=Decimal(edge),
        recommendation=CanonicalRecommendationAdapter(
            recommended_size=Decimal(size),
            guaranteed_profit=Decimal(profit),
            limiting_leg_outcome="away",
            limiting_leg_venue="smarkets",
            quote_age_ms=freshness_ms,
        ),
        opened_at=opened_at,
        expired_at=expired_at or NOW + timedelta(minutes=5),
        event_summary="Newcastle United vs Chelsea",
        market_summary="Match result (regulation time)",
        hedge_revalidation=hedge_revalidation,
        survivability=survivability,
    )


def _router(
    email: RecordingAdapter | None = None,
) -> tuple[PriorityAlertNotificationRouter, RecordingAdapter]:
    adapter = email or RecordingAdapter(NotificationChannel.EMAIL)
    service = PriorityAlertNotificationRouter(
        SqliteNotificationRepository(),
        {NotificationChannel.EMAIL: adapter},
        cooldown=timedelta(minutes=15),
    )
    return service, adapter


def test_first_priority_alert_produces_in_app_and_email_notification() -> None:
    service, email = _router()
    decision = service.route(_alert(), now=NOW)

    assert decision.event == NotificationEventType.PRIORITY_ALERT_OPENED
    assert decision.notify_outbound is True
    assert decision.notification is not None
    assert decision.notification.unread is True
    assert decision.unread_count == 1
    assert len(email.sent) == 1
    assert email.sent[0].payload.priority_alert_id == "alert-1"
    assert email.sent[0].payload.net_guaranteed_edge == Decimal("0.098")
    assert email.sent[0].payload.recommended_size == Decimal("475")
    assert email.sent[0].payload.expected_guaranteed_profit == Decimal("46.55")
    assert email.sent[0].payload.limiting_venue == "smarkets"
    assert email.sent[0].payload.quote_freshness_ms == 180


def test_duplicate_alert_inside_cooldown_does_not_resend() -> None:
    service, email = _router()
    first = service.route(_alert(alert_id="alert-1", size="475"), now=NOW)
    duplicate = service.route(
        _alert(alert_id="alert-2", size="480", freshness_ms=220),
        now=NOW + timedelta(minutes=2),
    )

    assert first.notify_outbound is True
    assert first.notification is not None
    assert duplicate.notify_outbound is False
    assert duplicate.suppress_reason == "cooldown"
    assert duplicate.event == NotificationEventType.PRIORITY_ALERT_UPDATED
    assert duplicate.notification is not None
    assert duplicate.notification.recommended_size == Decimal("480")
    assert duplicate.notification.quote_freshness_ms == 220
    assert duplicate.notification.notification_id == first.notification.notification_id
    assert len(email.sent) == 1


def test_severity_upgrade_can_resend() -> None:
    service, email = _router()
    service.route(_alert(severity="PRIORITY"), now=NOW)
    upgrade = service.route(
        _alert(alert_id="alert-critical", severity="CRITICAL", profit="90"),
        now=NOW + timedelta(minutes=1),
    )

    assert upgrade.event == NotificationEventType.PRIORITY_ALERT_UPGRADED
    assert upgrade.notify_outbound is True
    assert upgrade.notification is not None
    assert upgrade.notification.severity == PrioritySeverity.CRITICAL
    assert len(email.sent) == 2
    assert email.sent[1].event == NotificationEventType.PRIORITY_ALERT_UPGRADED


def test_deep_link_matches_integrated_ui_route() -> None:
    alert = _alert(alert_id="pa-42")
    payload_path = "/arbitrage/priority-alerts/pa-42"
    assert priority_alert_deep_link("pa-42") == payload_path
    assert priority_alert_deep_link("alert-1") == "/arbitrage/priority-alerts/alert-1"
    assert priority_alert_deep_link("alert-1") == priority_alert_deep_link("alert-1")

    service, email = _router()
    decision = service.route(alert, now=NOW)
    assert decision.notification is not None
    assert decision.notification.deep_link_path == payload_path
    assert email.sent[0].payload.deep_link_path == payload_path


def test_expired_alert_does_not_send_stale_notification() -> None:
    service, email = _router()
    expired = service.route(
        _alert(expired_at=NOW - timedelta(seconds=1)),
        now=NOW,
    )
    assert expired.notify_outbound is False
    assert expired.notification is None
    assert email.sent == []
    assert service.list_recent() == []

    opened = service.route(_alert(alert_id="live"), now=NOW)
    stale = service.route(
        _alert(alert_id="stale", expired_at=NOW + timedelta(minutes=1)),
        now=NOW + timedelta(minutes=2),
    )
    assert opened.notify_outbound is True
    assert stale.event == NotificationEventType.PRIORITY_ALERT_EXPIRED
    assert stale.notify_outbound is False
    assert stale.notification is not None
    assert stale.notification.status.value == "EXPIRED"
    assert len(email.sent) == 1


def test_external_manual_lifecycle_requires_operator_confirmation() -> None:
    service, email = _router()
    decision = service.route(
        _alert(
            lifecycle_state="AWAITING_EXTERNAL_LEG_CONFIRMATION",
            operator_action="PREPARE_PROCEED_WITH_EXTERNAL_COUNTERPARTY",
        ),
        now=NOW,
    )
    assert decision.notification is not None
    assert decision.notification.requires_operator_confirmation is True
    assert decision.notification.actionability == "REQUIRES_OPERATOR_CONFIRMATION"
    assert decision.notification.lifecycle_state == PriorityAlertState.AWAITING_EXTERNAL_LEG_CONFIRMATION
    assert "not fully actionable or filled" in email.sent[0].body
    assert "PREPARE_PROCEED_WITH_EXTERNAL_COUNTERPARTY" in email.sent[0].body


def test_revalidation_failed_is_not_treated_as_filled() -> None:
    service, email = _router()
    decision = service.route(
        _alert(
            lifecycle_state="HEDGE_REVALIDATION_FAILED",
            hedge_revalidation=CanonicalHedgeRevalidationAdapter(
                accepted=False,
                lifecycle_state=PriorityAlertState.HEDGE_REVALIDATION_FAILED,
                reasons=["fresh_hedge_capacity_insufficient"],
            ),
        ),
        now=NOW,
    )
    assert decision.notification is not None
    assert decision.notification.revalidation_failed is True
    assert decision.notification.actionability == "NOT_FULLY_ACTIONABLE"
    assert "fail closed" in email.sent[0].body
    assert "Do not treat this as filled" in email.sent[0].body


def test_revalidation_confirmed_stays_paper_only() -> None:
    service, email = _router()
    decision = service.route(
        _alert(
            lifecycle_state="HEDGE_REVALIDATED",
            hedge_revalidation=CanonicalHedgeRevalidationAdapter(
                accepted=True,
                lifecycle_state=PriorityAlertState.HEDGE_REVALIDATED,
            ),
        ),
        now=NOW,
    )
    assert decision.notification is not None
    assert decision.notification.revalidation_confirmed is True
    assert "no venue orders were placed" in email.sent[0].body
    assert decision.notification.payload().paper_mode is True


def test_adapter_maps_1_1_from_canonical_priority_alert_object() -> None:
    canonical = SimpleNamespace(
        alert_id="alert-60",
        opportunity_id="opp-60",
        canonical_event_id="evt-60",
        canonical_market_id="mkt-60",
        severity=SimpleNamespace(value="HIGH_PRIORITY"),
        lifecycle_state=SimpleNamespace(value="AWAITING_EXTERNAL_LEG_CONFIRMATION"),
        operator_action=SimpleNamespace(value="PREPARE_PROCEED_WITH_EXTERNAL_COUNTERPARTY"),
        net_guaranteed_edge=Decimal("0.04"),
        recommendation=SimpleNamespace(
            recommended_size=Decimal("200"),
            guaranteed_profit=Decimal("8"),
            limiting_leg_outcome="home",
            limiting_leg_venue="matchbook",
            quote_age_ms=90,
        ),
        opened_at=NOW,
        expired_at=NOW + timedelta(minutes=3),
        event_summary=None,
        market_summary=None,
        paper_mode=True,
        places_orders=False,
        commits_automated_legs=False,
        hedge_revalidation=None,
    )
    adapter = notification_adapter_from_priority_alert(canonical)
    assert adapter.alert_id == "alert-60"
    assert adapter.opportunity_id == "opp-60"
    assert adapter.severity == "HIGH_PRIORITY"
    assert adapter.lifecycle_state == "AWAITING_EXTERNAL_LEG_CONFIRMATION"
    service, email = _router()
    decision = service.route(canonical, now=NOW)
    assert decision.notification is not None
    assert decision.notification.priority_alert_id == "alert-60"
    assert decision.notification.deep_link_path == "/arbitrage/priority-alerts/alert-60"
    assert decision.notification.requires_operator_confirmation is True
    assert email.sent[0].payload.opportunity_id == "opp-60"


def test_notifications_package_does_not_define_a_second_priority_alert_model() -> None:
    assert not hasattr(notifications, "PriorityAlert")
    assert not hasattr(notifications, "AlertSeverity")
    from sports_hedge.notifications import models as notification_models

    assert not hasattr(notification_models, "PriorityAlert")
    assert not hasattr(notification_models, "AlertSeverity")


def test_naive_timestamps_fail_closed() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        _alert(opened_at=datetime(2026, 9, 11, 21, 0))
    service, _email = _router()
    with pytest.raises(ValueError, match="timezone-aware"):
        service.route(_alert(), now=datetime(2026, 9, 11, 21, 0))


def test_outbound_adapter_failure_does_not_drop_in_app_notification() -> None:
    class ExplodingAdapter:
        channel = NotificationChannel.EMAIL

        def send(self, message: object) -> None:
            raise RuntimeError("provider unavailable")

    exploding = ExplodingAdapter()
    service = PriorityAlertNotificationRouter(
        SqliteNotificationRepository(),
        {NotificationChannel.EMAIL: exploding},
    )
    decision = service.route(_alert(), now=NOW)
    assert decision.notification is not None
    assert decision.notification.unread is True
    assert any(item.reason.startswith("adapter_failed:") for item in decision.dispatches)
    assert service.unread_count() == 1


def test_notification_routing_has_no_betting_or_execution_side_effects() -> None:
    service, _email = _router()
    service.route(_alert(), now=NOW)

    assert Settings().sports_hedge_execution_enabled is False
    assert not hasattr(ReadOnlyVenue, "place_order")
    assert not hasattr(service, "place_order")
    assert not hasattr(service, "cancel_order")
    venue_like = SimpleNamespace(place_order=None)
    assert venue_like is not service
    assert "sports_hedge.venues" not in notifications.__dict__
    assert "sports_hedge.arbitrage" not in notifications.__dict__
    assert "sports_hedge.paper" not in notifications.__dict__


def test_read_model_lists_unread_and_mark_read() -> None:
    service, _email = _router()
    decision = service.route(_alert(), now=NOW)
    assert decision.notification is not None
    unread = service.list_recent(unread_only=True)
    assert len(unread) == 1
    marked = service.mark_read(decision.notification.notification_id)
    assert marked is not None
    assert marked.unread is False
    assert service.unread_count() == 0
    assert service.list_recent(unread_only=True) == []


def test_notifications_api_exposes_recent_unread_and_mark_read() -> None:
    service, _email = _router()
    app.dependency_overrides[get_notification_router] = lambda: service
    client = TestClient(app)
    live_now = datetime.now(UTC)
    payload = _alert(
        opened_at=live_now,
        expired_at=live_now + timedelta(minutes=10),
    ).model_dump(mode="json")
    try:
        opened = client.post("/notifications/priority-alerts", json=payload)
        assert opened.status_code == 200
        body = opened.json()
        assert body["notify_outbound"] is True
        notification_id = body["notification"]["notification_id"]
        assert body["notification"]["deep_link_path"] == "/arbitrage/priority-alerts/alert-1"

        listed = client.get("/notifications?unread_only=true")
        assert listed.status_code == 200
        rows = listed.json()
        assert len(rows) == 1
        assert rows[0]["priority_alert_id"] == "alert-1"

        count = client.get("/notifications/unread-count")
        assert count.json() == {"unread_count": 1}

        read = client.post(f"/notifications/{notification_id}/read")
        assert read.status_code == 200
        assert read.json()["unread"] is False
        assert client.get("/notifications/unread-count").json() == {"unread_count": 0}
    finally:
        app.dependency_overrides.clear()


def test_optional_survivability_is_carried_when_present_and_null_when_absent() -> None:
    service, email = _router()
    absent = service.route(_alert(), now=NOW)
    assert absent.notification is not None
    assert absent.notification.survivability is None
    assert "Survivability score" not in email.sent[0].body

    supplied = OpportunitySurvivability(
        survivability_score=42,
        survival_probability_at_required_latency=Decimal("0.61"),
        required_action_latency_seconds=Decimal("12"),
        expected_external_confirmation_latency_seconds=Decimal("25"),
        volatility_regime=VolatilityRegime.ELEVATED,
        survivability_confidence=SurvivabilityConfidence.MEDIUM,
        reasons=["quote_age"],
        data_insufficient=False,
    )
    other = PriorityAlertNotificationRouter(
        SqliteNotificationRepository(),
        {NotificationChannel.EMAIL: RecordingAdapter(NotificationChannel.EMAIL)},
    )
    present = other.route(
        _alert(opportunity_id="opp-survivability", survivability=supplied),
        now=NOW,
    )
    assert present.notification is not None
    assert present.notification.survivability is not None
    assert present.notification.survivability.survivability_score == 42
    assert present.notification.survivability.survival_probability_at_required_latency == Decimal(
        "0.61"
    )
    assert present.notification.survivability.required_action_latency_seconds == Decimal("12")
    assert present.notification.survivability.expected_external_confirmation_latency_seconds == Decimal(
        "25"
    )
    assert present.notification.survivability.volatility_regime == VolatilityRegime.ELEVATED
    assert present.notification.survivability.survivability_confidence == SurvivabilityConfidence.MEDIUM
    assert present.notification.survivability.reasons == ["quote_age"]


def test_routes_merged_canonical_priority_alert_without_inventing_survivability() -> None:
    import test_priority_alerts as pa

    alert = pa._service().ingest(pa._candidate())
    assert alert is not None
    service, email = _router()
    decision = service.route(alert, now=alert.opened_at)
    assert decision.notify_outbound is True
    assert decision.notification is not None
    assert decision.notification.priority_alert_id == alert.alert_id
    assert decision.notification.deep_link_path == f"/arbitrage/priority-alerts/{alert.alert_id}"
    assert decision.notification.survivability is not None
    assert decision.notification.survivability.survivability_score is None
    assert decision.notification.survivability.data_insufficient is True
    assert "Survivability score" not in email.sent[0].body
    assert alert.places_orders is False
    assert alert.commits_automated_legs is False

