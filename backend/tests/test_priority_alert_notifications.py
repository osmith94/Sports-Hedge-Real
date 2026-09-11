from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.notifications import get_notification_router
from sports_hedge.config import Settings
from sports_hedge.notifications.adapters import RecordingAdapter
from sports_hedge.notifications.models import (
    AlertSeverity,
    NotificationChannel,
    NotificationEventType,
    PriorityAlert,
    priority_alert_deep_link,
)
from sports_hedge.notifications.repository import SqliteNotificationRepository
from sports_hedge.notifications.router import PriorityAlertNotificationRouter
from sports_hedge.venues.base import ReadOnlyVenue


NOW = datetime(2026, 9, 11, 21, 0, tzinfo=UTC)


def _alert(
    *,
    alert_id: str = "alert-1",
    severity: AlertSeverity = AlertSeverity.PRIORITY,
    created_at: datetime = NOW,
    expires_at: datetime | None = None,
    edge: str = "0.098",
    size: str = "475",
    profit: str = "46.55",
    freshness_ms: int = 180,
) -> PriorityAlert:
    return PriorityAlert(
        priority_alert_id=alert_id,
        severity=severity,
        canonical_event_id="evt:newcastle-chelsea-2026-09-20",
        canonical_market_id="evt:newcastle-chelsea-2026-09-20:match_result",
        event_summary="Newcastle United vs Chelsea",
        market_summary="Match result (regulation time)",
        net_guaranteed_edge=Decimal(edge),
        recommended_size=Decimal(size),
        expected_guaranteed_profit=Decimal(profit),
        limiting_venue="smarkets",
        limiting_leg="away",
        quote_freshness_ms=freshness_ms,
        created_at=created_at,
        expires_at=expires_at or NOW + timedelta(minutes=5),
    )


def _router() -> tuple[PriorityAlertNotificationRouter, RecordingAdapter]:
    email = RecordingAdapter(NotificationChannel.EMAIL)
    service = PriorityAlertNotificationRouter(
        SqliteNotificationRepository(),
        {NotificationChannel.EMAIL: email},
        cooldown=timedelta(minutes=15),
    )
    return service, email


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
    service.route(_alert(severity=AlertSeverity.PRIORITY), now=NOW)
    upgrade = service.route(
        _alert(alert_id="alert-critical", severity=AlertSeverity.CRITICAL, profit="90"),
        now=NOW + timedelta(minutes=1),
    )

    assert upgrade.event == NotificationEventType.PRIORITY_ALERT_UPGRADED
    assert upgrade.notify_outbound is True
    assert upgrade.notification is not None
    assert upgrade.notification.severity == AlertSeverity.CRITICAL
    assert len(email.sent) == 2
    assert email.sent[1].event == NotificationEventType.PRIORITY_ALERT_UPGRADED


def test_deep_link_is_deterministic() -> None:
    alert = _alert(alert_id="pa-42")
    assert alert.deep_link_path == "/priority-alerts/pa-42"
    assert priority_alert_deep_link("alert-1") == "/priority-alerts/alert-1"
    assert priority_alert_deep_link("alert-1") == priority_alert_deep_link("alert-1")

    service, email = _router()
    decision = service.route(alert, now=NOW)
    assert decision.notification is not None
    assert decision.notification.deep_link_path == "/priority-alerts/pa-42"
    assert email.sent[0].payload.deep_link_path == "/priority-alerts/pa-42"


def test_expired_alert_does_not_send_stale_notification() -> None:
    service, email = _router()
    expired = service.route(
        _alert(expires_at=NOW - timedelta(seconds=1)),
        now=NOW,
    )
    assert expired.notify_outbound is False
    assert expired.notification is None
    assert email.sent == []
    assert service.list_recent() == []

    opened = service.route(_alert(alert_id="live"), now=NOW)
    stale = service.route(
        _alert(alert_id="stale", expires_at=NOW + timedelta(minutes=1)),
        now=NOW + timedelta(minutes=2),
    )
    assert opened.notify_outbound is True
    assert stale.event == NotificationEventType.PRIORITY_ALERT_EXPIRED
    assert stale.notify_outbound is False
    assert stale.notification is not None
    assert stale.notification.status.value == "EXPIRED"
    assert len(email.sent) == 1


def test_notification_routing_has_no_betting_or_execution_side_effects() -> None:
    service, _email = _router()
    service.route(_alert(), now=NOW)

    assert Settings().sports_hedge_execution_enabled is False
    assert not hasattr(ReadOnlyVenue, "place_order")
    assert not hasattr(service, "place_order")
    assert not hasattr(service, "cancel_order")
    venue_like = SimpleNamespace(place_order=None)
    assert venue_like is not service

    import sports_hedge.notifications as notifications

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
        created_at=live_now,
        expires_at=live_now + timedelta(minutes=10),
    ).model_dump(mode="json")
    try:
        opened = client.post("/notifications/priority-alerts", json=payload)
        assert opened.status_code == 200
        body = opened.json()
        assert body["notify_outbound"] is True
        notification_id = body["notification"]["notification_id"]

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
