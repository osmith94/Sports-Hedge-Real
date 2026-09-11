from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, computed_field, model_validator


class AlertSeverity(StrEnum):
    PRIORITY = "PRIORITY"
    HIGH_PRIORITY = "HIGH_PRIORITY"
    CRITICAL = "CRITICAL"


SEVERITY_RANK: dict[AlertSeverity, int] = {
    AlertSeverity.PRIORITY: 1,
    AlertSeverity.HIGH_PRIORITY: 2,
    AlertSeverity.CRITICAL: 3,
}


class NotificationChannel(StrEnum):
    IN_APP = "IN_APP"
    EMAIL = "EMAIL"
    PUSH = "PUSH"
    SMS = "SMS"


class NotificationEventType(StrEnum):
    PRIORITY_ALERT_OPENED = "PRIORITY_ALERT_OPENED"
    PRIORITY_ALERT_UPGRADED = "PRIORITY_ALERT_UPGRADED"
    PRIORITY_ALERT_DOWNGRADED = "PRIORITY_ALERT_DOWNGRADED"
    PRIORITY_ALERT_EXPIRED = "PRIORITY_ALERT_EXPIRED"
    PRIORITY_ALERT_UPDATED = "PRIORITY_ALERT_UPDATED"


class InAppNotificationStatus(StrEnum):
    OPEN = "OPEN"
    UPDATED = "UPDATED"
    UPGRADED = "UPGRADED"
    DOWNGRADED = "DOWNGRADED"
    EXPIRED = "EXPIRED"


def priority_alert_deep_link(priority_alert_id: str) -> str:
    """Deterministic operator UI path for a Priority Alert."""

    return f"/priority-alerts/{priority_alert_id}"


class PriorityAlert(BaseModel):
    """Provider-neutral Priority Alert snapshot used for notification routing."""

    priority_alert_id: str
    severity: AlertSeverity
    canonical_event_id: str
    canonical_market_id: str
    event_summary: str
    market_summary: str
    net_guaranteed_edge: Decimal
    recommended_size: Decimal
    expected_guaranteed_profit: Decimal
    limiting_venue: str
    limiting_leg: str
    quote_freshness_ms: int = Field(ge=0)
    created_at: datetime
    expires_at: datetime
    opportunity_key: str | None = None

    @model_validator(mode="after")
    def normalize(self) -> "PriorityAlert":
        if self.created_at.tzinfo is None:
            self.created_at = self.created_at.replace(tzinfo=UTC)
        if self.expires_at.tzinfo is None:
            self.expires_at = self.expires_at.replace(tzinfo=UTC)
        if not self.opportunity_key:
            self.opportunity_key = f"{self.canonical_event_id}:{self.canonical_market_id}"
        return self

    @computed_field
    @property
    def deep_link_path(self) -> str:
        return priority_alert_deep_link(self.priority_alert_id)

    def is_expired(self, *, now: datetime) -> bool:
        return self.expires_at <= now


class NotificationPayload(BaseModel):
    priority_alert_id: str
    severity: AlertSeverity
    event_summary: str
    market_summary: str
    net_guaranteed_edge: Decimal
    recommended_size: Decimal
    expected_guaranteed_profit: Decimal
    limiting_venue: str
    limiting_leg: str
    quote_freshness_ms: int
    deep_link_path: str
    created_at: datetime
    expires_at: datetime
    opportunity_key: str
    canonical_event_id: str
    canonical_market_id: str

    @classmethod
    def from_alert(cls, alert: PriorityAlert) -> "NotificationPayload":
        assert alert.opportunity_key is not None
        return cls(
            priority_alert_id=alert.priority_alert_id,
            severity=alert.severity,
            event_summary=alert.event_summary,
            market_summary=alert.market_summary,
            net_guaranteed_edge=alert.net_guaranteed_edge,
            recommended_size=alert.recommended_size,
            expected_guaranteed_profit=alert.expected_guaranteed_profit,
            limiting_venue=alert.limiting_venue,
            limiting_leg=alert.limiting_leg,
            quote_freshness_ms=alert.quote_freshness_ms,
            deep_link_path=alert.deep_link_path,
            created_at=alert.created_at,
            expires_at=alert.expires_at,
            opportunity_key=alert.opportunity_key,
            canonical_event_id=alert.canonical_event_id,
            canonical_market_id=alert.canonical_market_id,
        )


class OutboundMessage(BaseModel):
    event: NotificationEventType
    channel: NotificationChannel
    payload: NotificationPayload
    subject: str
    body: str


class InAppPriorityNotification(BaseModel):
    notification_id: str = Field(default_factory=lambda: str(uuid4()))
    opportunity_key: str
    canonical_event_id: str
    canonical_market_id: str
    priority_alert_id: str
    severity: AlertSeverity
    status: InAppNotificationStatus = InAppNotificationStatus.OPEN
    last_event: NotificationEventType = NotificationEventType.PRIORITY_ALERT_OPENED
    event_summary: str
    market_summary: str
    net_guaranteed_edge: Decimal
    recommended_size: Decimal
    expected_guaranteed_profit: Decimal
    limiting_venue: str
    limiting_leg: str
    quote_freshness_ms: int
    deep_link_path: str
    alert_created_at: datetime
    expires_at: datetime
    created_at: datetime
    updated_at: datetime
    last_outbound_at: datetime | None = None
    unread: bool = True

    @classmethod
    def from_alert(
        cls,
        alert: PriorityAlert,
        *,
        now: datetime,
        notification_id: str | None = None,
        last_outbound_at: datetime | None = None,
        status: InAppNotificationStatus = InAppNotificationStatus.OPEN,
        last_event: NotificationEventType = NotificationEventType.PRIORITY_ALERT_OPENED,
        unread: bool = True,
        created_at: datetime | None = None,
    ) -> "InAppPriorityNotification":
        payload = NotificationPayload.from_alert(alert)
        return cls(
            notification_id=notification_id or str(uuid4()),
            opportunity_key=payload.opportunity_key,
            canonical_event_id=payload.canonical_event_id,
            canonical_market_id=payload.canonical_market_id,
            priority_alert_id=payload.priority_alert_id,
            severity=payload.severity,
            status=status,
            last_event=last_event,
            event_summary=payload.event_summary,
            market_summary=payload.market_summary,
            net_guaranteed_edge=payload.net_guaranteed_edge,
            recommended_size=payload.recommended_size,
            expected_guaranteed_profit=payload.expected_guaranteed_profit,
            limiting_venue=payload.limiting_venue,
            limiting_leg=payload.limiting_leg,
            quote_freshness_ms=payload.quote_freshness_ms,
            deep_link_path=payload.deep_link_path,
            alert_created_at=payload.created_at,
            expires_at=payload.expires_at,
            created_at=created_at or now,
            updated_at=now,
            last_outbound_at=last_outbound_at,
            unread=unread,
        )

    def payload(self) -> NotificationPayload:
        return NotificationPayload(
            priority_alert_id=self.priority_alert_id,
            severity=self.severity,
            event_summary=self.event_summary,
            market_summary=self.market_summary,
            net_guaranteed_edge=self.net_guaranteed_edge,
            recommended_size=self.recommended_size,
            expected_guaranteed_profit=self.expected_guaranteed_profit,
            limiting_venue=self.limiting_venue,
            limiting_leg=self.limiting_leg,
            quote_freshness_ms=self.quote_freshness_ms,
            deep_link_path=self.deep_link_path,
            created_at=self.alert_created_at,
            expires_at=self.expires_at,
            opportunity_key=self.opportunity_key,
            canonical_event_id=self.canonical_event_id,
            canonical_market_id=self.canonical_market_id,
        )


class NotificationDispatch(BaseModel):
    dispatch_id: str = Field(default_factory=lambda: str(uuid4()))
    notification_id: str
    channel: NotificationChannel
    event: NotificationEventType
    suppressed: bool
    reason: str
    created_at: datetime
    payload: NotificationPayload


class RouteDecision(BaseModel):
    event: NotificationEventType
    notify_outbound: bool
    suppress_reason: str | None = None
    notification: InAppPriorityNotification | None = None
    dispatches: list[NotificationDispatch] = Field(default_factory=list)
    unread_count: int = 0
