from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta

from sports_hedge.arbitrage.priority_alerts.models import (
    PriorityAlertState,
    SEVERITY_RANK,
    SurvivabilityConfidence,
    VolatilityRegime,
)
from sports_hedge.notifications.adapters import ChannelAdapter
from sports_hedge.notifications.canonical import require_aware_utc
from sports_hedge.notifications.models import (
    InAppNotificationStatus,
    InAppPriorityNotification,
    NotificationChannel,
    NotificationDispatch,
    NotificationEventType,
    NotificationPayload,
    OutboundMessage,
    RouteDecision,
)
from sports_hedge.notifications.repository import SqliteNotificationRepository


class PriorityAlertNotificationRouter:
    """Deduping, rate-limited routing for Priority Arb Alerts.

    Consumes the canonical Priority Alert read model via a 1:1 adapter.
    In-app persistence is always applied. Outbound channels are provider-neutral
    and never place venue orders.
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

    def route(self, alert: object, *, now: datetime | None = None) -> RouteDecision:
        moment = require_aware_utc(now or datetime.now(UTC), field="now")
        payload = NotificationPayload.from_priority_alert(alert)
        existing = self._repository.get_by_opportunity_key(payload.opportunity_key)

        if payload.is_expired(now=moment):
            return self._expire(payload, existing=existing, now=moment)

        if existing is None:
            return self._notify(
                payload,
                existing=None,
                now=moment,
                event=NotificationEventType.PRIORITY_ALERT_OPENED,
                status=InAppNotificationStatus.OPEN,
            )

        previous_rank = SEVERITY_RANK[existing.severity]
        next_rank = SEVERITY_RANK[payload.severity]
        if next_rank > previous_rank:
            return self._notify(
                payload,
                existing=existing,
                now=moment,
                event=NotificationEventType.PRIORITY_ALERT_UPGRADED,
                status=InAppNotificationStatus.UPGRADED,
            )
        if next_rank < previous_rank:
            return self._update_in_app(
                payload,
                existing=existing,
                now=moment,
                event=NotificationEventType.PRIORITY_ALERT_DOWNGRADED,
                status=InAppNotificationStatus.DOWNGRADED,
                suppress_reason="severity_downgrade",
            )

        if self._within_cooldown(existing, now=moment):
            return self._update_in_app(
                payload,
                existing=existing,
                now=moment,
                event=NotificationEventType.PRIORITY_ALERT_UPDATED,
                status=InAppNotificationStatus.UPDATED,
                suppress_reason="cooldown",
            )

        return self._notify(
            payload,
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

    def _expire(
        self,
        payload: NotificationPayload,
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
                "lifecycle_state": PriorityAlertState.EXPIRED,
                "actionability": "NOT_FULLY_ACTIONABLE",
                "updated_at": now,
                "expires_at": payload.expires_at,
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
        payload: NotificationPayload,
        *,
        existing: InAppPriorityNotification | None,
        now: datetime,
        event: NotificationEventType,
        status: InAppNotificationStatus,
    ) -> RouteDecision:
        notification = InAppPriorityNotification.from_payload(
            payload,
            now=now,
            notification_id=None if existing is None else existing.notification_id,
            last_outbound_at=now,
            status=status,
            last_event=event,
            unread=True,
            created_at=now if existing is None else existing.created_at,
        )
        stored = self._repository.upsert(notification)
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
        payload: NotificationPayload,
        *,
        existing: InAppPriorityNotification,
        now: datetime,
        event: NotificationEventType,
        status: InAppNotificationStatus,
        suppress_reason: str,
    ) -> RouteDecision:
        notification = InAppPriorityNotification.from_payload(
            payload,
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
        try:
            adapter.send(message)
        except Exception as exc:  # noqa: BLE001 — keep in-app delivery usable
            return self._record_dispatch(
                notification,
                channel=channel,
                event=event,
                now=now,
                suppressed=True,
                reason=f"adapter_failed:{type(exc).__name__}",
                payload=payload,
            )
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
    lines = [
        f"Net guaranteed edge: {payload.net_guaranteed_edge}",
        f"Recommended size: {payload.recommended_size}",
        f"Expected guaranteed profit: {payload.expected_guaranteed_profit}",
        f"Limiting: {payload.limiting_venue} / {payload.limiting_leg}",
        f"Quote freshness: {payload.quote_freshness_ms}ms",
        f"Lifecycle: {payload.lifecycle_state.value}",
        f"Actionability: {payload.actionability}",
        f"Open: {payload.deep_link_path}",
    ]
    if payload.requires_operator_confirmation:
        lines.append(
            "Requires operator confirmation of an external manual leg; "
            "this opportunity is not fully actionable or filled."
        )
        lines.append(f"Operator action: {payload.operator_action.value}")
    if payload.revalidation_failed:
        lines.append(
            "Hedge revalidation failed after external confirmation; fail closed. "
            "Do not treat this as filled."
        )
    if payload.revalidation_confirmed:
        lines.append(
            "External leg confirmed and remaining hedge revalidated in paper mode; "
            "no venue orders were placed."
        )
    survivability = payload.survivability
    if survivability is not None:
        if survivability.survivability_score is not None:
            lines.append(f"Survivability score: {survivability.survivability_score}")
        if survivability.survival_probability_at_required_latency is not None:
            lines.append(
                "Survival probability at required latency: "
                f"{survivability.survival_probability_at_required_latency}"
            )
        if survivability.required_action_latency_seconds is not None:
            lines.append(
                f"Required action latency: {survivability.required_action_latency_seconds}s"
            )
        if survivability.expected_external_confirmation_latency_seconds is not None:
            lines.append(
                "Expected external-confirmation latency: "
                f"{survivability.expected_external_confirmation_latency_seconds}s"
            )
        if survivability.volatility_regime not in (None, VolatilityRegime.UNKNOWN):
            lines.append(f"Volatility regime: {survivability.volatility_regime.value}")
        if survivability.survivability_confidence not in (
            None,
            SurvivabilityConfidence.UNKNOWN,
        ):
            lines.append(
                f"Survivability confidence: {survivability.survivability_confidence.value}"
            )
        if survivability.reasons:
            lines.append("Survivability reasons: " + ", ".join(survivability.reasons))
    if payload.expires_at is not None:
        lines.append(f"Expires: {payload.expires_at.isoformat()}")
    return "\n".join(lines)


def default_adapters() -> dict[NotificationChannel, ChannelAdapter]:
    from sports_hedge.notifications.adapters import ConsoleEmailAdapter

    return {NotificationChannel.EMAIL: ConsoleEmailAdapter()}


__all__ = ["PriorityAlertNotificationRouter", "default_adapters"]
