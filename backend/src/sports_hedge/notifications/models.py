from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field

from sports_hedge.arbitrage.priority_alerts.models import (
    OpportunitySurvivability,
    OperatorAction,
    PriorityAlertState,
    PrioritySeverity,
)
from sports_hedge.notifications.canonical import (
    CanonicalHedgeRevalidationAdapter,
    PriorityAlertNotificationAdapter,
    notification_adapter_from_priority_alert,
    priority_alert_deep_link,
    require_aware_utc,
)


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


class NotificationPayload(BaseModel):
    """Provider-neutral notification payload projected from a canonical Priority Alert."""

    priority_alert_id: str
    opportunity_id: str
    severity: PrioritySeverity
    lifecycle_state: PriorityAlertState
    operator_action: OperatorAction
    requires_operator_confirmation: bool
    revalidation_failed: bool
    revalidation_confirmed: bool
    actionability: str
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
    expires_at: datetime | None = None
    opportunity_key: str
    canonical_event_id: str
    canonical_market_id: str
    paper_mode: bool = True
    hedge_revalidation: CanonicalHedgeRevalidationAdapter | None = None
    survivability: OpportunitySurvivability | None = None

    @classmethod
    def from_priority_alert(cls, alert: object) -> "NotificationPayload":
        adapter = notification_adapter_from_priority_alert(alert)
        return cls.from_adapter(adapter)

    @classmethod
    def from_adapter(cls, adapter: PriorityAlertNotificationAdapter) -> "NotificationPayload":
        lifecycle = adapter.lifecycle_state
        if adapter.hedge_revalidation is not None:
            lifecycle = adapter.hedge_revalidation.lifecycle_state
        requires_confirmation = (
            lifecycle == PriorityAlertState.AWAITING_EXTERNAL_LEG_CONFIRMATION
            or adapter.operator_action
            == OperatorAction.PREPARE_PROCEED_WITH_EXTERNAL_COUNTERPARTY
        )
        revalidation_failed = lifecycle == PriorityAlertState.HEDGE_REVALIDATION_FAILED or (
            adapter.hedge_revalidation is not None and not adapter.hedge_revalidation.accepted
        )
        revalidation_confirmed = lifecycle == PriorityAlertState.HEDGE_REVALIDATED
        if requires_confirmation:
            actionability = "REQUIRES_OPERATOR_CONFIRMATION"
        elif revalidation_failed or lifecycle == PriorityAlertState.EXPIRED:
            actionability = "NOT_FULLY_ACTIONABLE"
        else:
            actionability = "PAPER_REVIEW_ONLY"
        event_id = adapter.canonical_event_id or adapter.opportunity_id
        market_id = adapter.canonical_market_id or adapter.opportunity_id
        return cls(
            priority_alert_id=adapter.alert_id,
            opportunity_id=adapter.opportunity_id,
            severity=adapter.severity,
            lifecycle_state=lifecycle,
            operator_action=adapter.operator_action,
            requires_operator_confirmation=requires_confirmation,
            revalidation_failed=revalidation_failed,
            revalidation_confirmed=revalidation_confirmed,
            actionability=actionability,
            event_summary=adapter.event_summary or adapter.opportunity_id,
            market_summary=adapter.market_summary or market_id,
            net_guaranteed_edge=adapter.net_guaranteed_edge,
            recommended_size=adapter.recommendation.recommended_size,
            expected_guaranteed_profit=adapter.recommendation.guaranteed_profit,
            limiting_venue=str(adapter.recommendation.limiting_leg_venue),
            limiting_leg=adapter.recommendation.limiting_leg_outcome,
            quote_freshness_ms=adapter.recommendation.quote_age_ms,
            deep_link_path=priority_alert_deep_link(adapter.alert_id),
            created_at=adapter.opened_at,
            expires_at=adapter.expired_at,
            opportunity_key=adapter.opportunity_id,
            canonical_event_id=event_id,
            canonical_market_id=market_id,
            paper_mode=adapter.paper_mode,
            hedge_revalidation=adapter.hedge_revalidation,
            survivability=adapter.survivability,
        )

    def is_expired(self, *, now: datetime) -> bool:
        moment = require_aware_utc(now, field="now")
        if self.lifecycle_state == PriorityAlertState.EXPIRED:
            return True
        return self.expires_at is not None and self.expires_at <= moment


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
    opportunity_id: str
    severity: PrioritySeverity
    lifecycle_state: PriorityAlertState
    operator_action: OperatorAction
    requires_operator_confirmation: bool
    revalidation_failed: bool
    revalidation_confirmed: bool
    actionability: str
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
    expires_at: datetime | None = None
    created_at: datetime
    updated_at: datetime
    last_outbound_at: datetime | None = None
    unread: bool = True
    survivability: OpportunitySurvivability | None = None

    @classmethod
    def from_payload(
        cls,
        payload: NotificationPayload,
        *,
        now: datetime,
        notification_id: str | None = None,
        last_outbound_at: datetime | None = None,
        status: InAppNotificationStatus = InAppNotificationStatus.OPEN,
        last_event: NotificationEventType = NotificationEventType.PRIORITY_ALERT_OPENED,
        unread: bool = True,
        created_at: datetime | None = None,
    ) -> "InAppPriorityNotification":
        return cls(
            notification_id=notification_id or str(uuid4()),
            opportunity_key=payload.opportunity_key,
            canonical_event_id=payload.canonical_event_id,
            canonical_market_id=payload.canonical_market_id,
            priority_alert_id=payload.priority_alert_id,
            opportunity_id=payload.opportunity_id,
            severity=payload.severity,
            lifecycle_state=payload.lifecycle_state,
            operator_action=payload.operator_action,
            requires_operator_confirmation=payload.requires_operator_confirmation,
            revalidation_failed=payload.revalidation_failed,
            revalidation_confirmed=payload.revalidation_confirmed,
            actionability=payload.actionability,
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
            survivability=payload.survivability,
        )

    def payload(self) -> NotificationPayload:
        return NotificationPayload(
            priority_alert_id=self.priority_alert_id,
            opportunity_id=self.opportunity_id,
            severity=self.severity,
            lifecycle_state=self.lifecycle_state,
            operator_action=self.operator_action,
            requires_operator_confirmation=self.requires_operator_confirmation,
            revalidation_failed=self.revalidation_failed,
            revalidation_confirmed=self.revalidation_confirmed,
            actionability=self.actionability,
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
            paper_mode=True,
            survivability=self.survivability,
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
