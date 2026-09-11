from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from sports_hedge.notifications.adapters import ChannelAdapter
from sports_hedge.notifications.models import (
    SEVERITY_RANK,
    InAppNotificationStatus,
    InAppPriorityNotification,
    NotificationChannel,
    NotificationDispatch,
    NotificationEventType,
    NotificationPayload,
    OutboundMessage,
    PriorityAlert,
    RouteDecision,
)
from sports_hedge.notifications.repository import SqliteNotificationRepository


class PriorityAlertNotificationRouter:
    """Deduping, rate-limited routing for Priority Arb Alerts.

    In-app persistence is always applied. Outbound channels (email/push/SMS)
    are provider-neutral adapters and are never used to place venue orders.
    """

    def __init__(
        self,
        repository: SqliteNotificationRepository,
        adapters: Mapping[NotificationChannel, ChannelAdapter],
        *,
        cooldown: timedelta = timedelta(minutes=15),
        outbound_channels: tuple[NotificationChannel, ...] = (NotificationChannel.EMAIL,),
    ) -> None:
        self._repository = repository
        self._adapters = dict(adapters)
        self._cooldown = cooldown
        self._outbound_channels = outbound_channels

    def route(self, alert: PriorityAlert, *, now: datetime | None = None) -> RouteDecision:
        moment = now or datetime.now(UTC)
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)

        existing = self._repository.get_by_opportunity_key(
            alert.opportunity_key or f"{alert.canonical_event_id}:{alert.canonical_market_id}"
        )

        if alert.is_expired(now=moment):
            return self._expire(alert, existing=existing, now=moment)

        if existing is None:
            return self._open(alert, now=moment)

        previous_rank = SEVERITY_RANK[existing.severity]
        next_rank = SEVERITY_RANK[alert.severity]
        if next_rank > previous_rank:
            return self._notify(
                alert,
                existing=existing,
                now=moment,
                event=NotificationEventType.PRIORITY_ALERT_UPGRADED,
                status=InAppNotificationStatus.UPGRADED,
            )
        if next_rank < previous_rank:
            return self._update_in_app(
                alert,
                existing=existing,
                now=moment,
                event=NotificationEventType.PRIORITY_ALERT_DOWNGRADED,
                status=InAppNotificationStatus.DOWNGRADED,
                suppress_reason="severity_downgrade",
            )

        if self._within_cooldown(existing, now=moment):
            return self._update_in_app(
                alert,
                existing=existing,
                now=moment,
                event=NotificationEventType.PRIORITY_ALERT_UPDATED,
                status=InAppNotificationStatus.UPDATED,
                suppress_reason="cooldown",
            )

        return self._notify(
            alert,
            existing=existing,
            now=moment,
            event=NotificationEventType.PRIORITY_ALERT_OPENED,
            status=InAppNotificationStatus.UPDATED,
        )

    def list_recent(
        self,
        *,
        limit: int = 50,
        unread_only: bool = False,
    ) -> list[InAppPriorityNotification]:
        return self._repository.list_recent(limit=limit, unread_only=unread_only)

    def unread_count(self) -> int:
        return self._repository.unread_count()

    def mark_read(self, notification_id: str) -> InAppPriorityNotification | None:
        return self._repository.mark_read(notification_id)

    def _open(self, alert: PriorityAlert, *, now: datetime) -> RouteDecision:
        return self._notify(
            alert,
            existing=None,
            now=now,
            event=NotificationEventType.PRIORITY_ALERT_OPENED,
            status=InAppNotificationStatus.OPEN,
        )

    def _expire(
        self,
        alert: PriorityAlert,
        *,
        existing: InAppPriorityNotification | None,
        now: datetime,
    ) -> RouteDecision:
        if existing is None:
            return RouteDecision(
                event=NotificationEventType.PRIORITY_ALERT_EXPIRED,
                notify_outbound=False,
                suppress_reason="expired_alert",
            )
        notification = existing.model_copy(
            update={
                "status": InAppNotificationStatus.EXPIRED,
                "last_event": NotificationEventType.PRIORITY_ALERT_EXPIRED,
                "updated_at": now,
                "expires_at": alert.expires_at,
            }
        )
        stored = self._repository.upsert(notification)
        dispatch = self._record_suppressed(
            stored,
            now=now,
            event=NotificationEventType.PRIORITY_ALERT_EXPIRED,
            reason="expired_alert",
        )
        return RouteDecision(
            event=NotificationEventType.PRIORITY_ALERT_EXPIRED,
            notify_outbound=False,
            suppress_reason="expired_alert",
            notification=stored,
            dispatches=[dispatch],
            unread_count=self._repository.unread_count(),
        )

    def _notify(
        self,
        alert: PriorityAlert,
        *,
        existing: InAppPriorityNotification | None,
        now: datetime,
        event: NotificationEventType,
        status: InAppNotificationStatus,
    ) -> RouteDecision:
        notification = InAppPriorityNotification.from_alert(
            alert,
            now=now,
            notification_id=None if existing is None else existing.notification_id,
            last_outbound_at=now,
            status=status,
            last_event=event,
            unread=True,
            created_at=now if existing is None else existing.created_at,
        )
        stored = self._repository.upsert(notification)
        payload = NotificationPayload.from_alert(alert)
        self._record_dispatch(
            stored,
            channel=NotificationChannel.IN_APP,
            event=event,
            now=now,
            suppressed=False,
            reason="in_app",
            payload=payload,
        )
        dispatches = [
            self._send_outbound(stored, payload=payload, event=event, now=now, channel=channel)
            for channel in self._outbound_channels
        ]
        return RouteDecision(
            event=event,
            notify_outbound=True,
            notification=stored,
            dispatches=dispatches,
            unread_count=self._repository.unread_count(),
        )

    def _update_in_app(
        self,
        alert: PriorityAlert,
        *,
        existing: InAppPriorityNotification,
        now: datetime,
        event: NotificationEventType,
        status: InAppNotificationStatus,
        suppress_reason: str,
    ) -> RouteDecision:
        notification = InAppPriorityNotification.from_alert(
            alert,
            now=now,
            notification_id=existing.notification_id,
            last_outbound_at=existing.last_outbound_at,
            status=status,
            last_event=event,
            unread=existing.unread,
            created_at=existing.created_at,
        )
        stored = self._repository.upsert(notification)
        dispatch = self._record_suppressed(
            stored,
            now=now,
            event=event,
            reason=suppress_reason,
        )
        return RouteDecision(
            event=event,
            notify_outbound=False,
            suppress_reason=suppress_reason,
            notification=stored,
            dispatches=[dispatch],
            unread_count=self._repository.unread_count(),
        )

    def _within_cooldown(self, existing: InAppPriorityNotification, *, now: datetime) -> bool:
        if existing.last_outbound_at is None:
            return False
        return now - existing.last_outbound_at < self._cooldown

    def _send_outbound(
        self,
        notification: InAppPriorityNotification,
        *,
        payload: NotificationPayload,
        event: NotificationEventType,
        now: datetime,
        channel: NotificationChannel,
    ) -> NotificationDispatch:
        adapter = self._adapters.get(channel)
        if adapter is None:
            return self._record_suppressed(
                notification,
                now=now,
                event=event,
                reason=f"adapter_missing:{channel.value}",
                channel=channel,
            )
        message = OutboundMessage(
            event=event,
            channel=channel,
            payload=payload,
            subject=_subject(event, payload),
            body=_body(payload),
        )
        adapter.send(message)
        return self._record_dispatch(
            notification,
            channel=channel,
            event=event,
            now=now,
            suppressed=False,
            reason="sent",
            payload=payload,
        )

    def _record_suppressed(
        self,
        notification: InAppPriorityNotification,
        *,
        now: datetime,
        event: NotificationEventType,
        reason: str,
        channel: NotificationChannel = NotificationChannel.EMAIL,
    ) -> NotificationDispatch:
        return self._record_dispatch(
            notification,
            channel=channel,
            event=event,
            now=now,
            suppressed=True,
            reason=reason,
            payload=notification.payload(),
        )

    def _record_dispatch(
        self,
        notification: InAppPriorityNotification,
        *,
        channel: NotificationChannel,
        event: NotificationEventType,
        now: datetime,
        suppressed: bool,
        reason: str,
        payload: NotificationPayload,
    ) -> NotificationDispatch:
        return self._repository.append_dispatch(
            NotificationDispatch(
                notification_id=notification.notification_id,
                channel=channel,
                event=event,
                suppressed=suppressed,
                reason=reason,
                created_at=now,
                payload=payload,
            )
        )


def _subject(event: NotificationEventType, payload: NotificationPayload) -> str:
    return (
        f"{event.value}: {payload.severity.value} "
        f"{payload.event_summary} / {payload.market_summary}"
    )


def _body(payload: NotificationPayload) -> str:
    return (
        f"Net guaranteed edge: {payload.net_guaranteed_edge}\n"
        f"Recommended size: {payload.recommended_size}\n"
        f"Expected guaranteed profit: {payload.expected_guaranteed_profit}\n"
        f"Limiting: {payload.limiting_venue} / {payload.limiting_leg}\n"
        f"Quote freshness: {payload.quote_freshness_ms}ms\n"
        f"Open: {payload.deep_link_path}\n"
        f"Expires: {payload.expires_at.isoformat()}"
    )


def default_adapters() -> dict[NotificationChannel, ChannelAdapter]:
    from sports_hedge.notifications.adapters import ConsoleEmailAdapter

    return {NotificationChannel.EMAIL: ConsoleEmailAdapter()}


__all__ = ["PriorityAlertNotificationRouter", "default_adapters"]
