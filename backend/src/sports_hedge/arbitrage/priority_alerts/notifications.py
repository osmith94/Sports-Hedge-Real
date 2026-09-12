from __future__ import annotations

from typing import Protocol

from sports_hedge.arbitrage.priority_alerts.models import (
    NotificationChannel,
    PriorityAlert,
    PriorityAlertEvent,
)


class NotificationAdapter(Protocol):
    channel: NotificationChannel

    def notify(self, event: PriorityAlertEvent, alert: PriorityAlert) -> None: ...


class InAppNotificationAdapter:
    """Phase 1 in-app seam. Records notifications without contacting venues."""

    channel = NotificationChannel.IN_APP

    def __init__(self) -> None:
        self.delivered: list[PriorityAlertEvent] = []

    def notify(self, event: PriorityAlertEvent, alert: PriorityAlert) -> None:
        del alert
        self.delivered.append(event)


class EmailNotificationAdapter:
    """Email adapter seam. The test provider records payloads and never sends mail."""

    channel = NotificationChannel.EMAIL

    def __init__(self) -> None:
        self.delivered: list[PriorityAlertEvent] = []

    def notify(self, event: PriorityAlertEvent, alert: PriorityAlert) -> None:
        del alert
        self.delivered.append(event)
