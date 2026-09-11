"""Provider-neutral Priority Alert notification routing."""

from sports_hedge.notifications.adapters import (
    ChannelAdapter,
    ConsoleEmailAdapter,
    NoOpAdapter,
    RecordingAdapter,
)
from sports_hedge.notifications.models import (
    AlertSeverity,
    InAppNotificationStatus,
    InAppPriorityNotification,
    NotificationChannel,
    NotificationDispatch,
    NotificationEventType,
    NotificationPayload,
    OutboundMessage,
    PriorityAlert,
    RouteDecision,
    priority_alert_deep_link,
)
from sports_hedge.notifications.repository import SqliteNotificationRepository
from sports_hedge.notifications.router import PriorityAlertNotificationRouter, default_adapters

__all__ = [
    "AlertSeverity",
    "ChannelAdapter",
    "ConsoleEmailAdapter",
    "InAppNotificationStatus",
    "InAppPriorityNotification",
    "NoOpAdapter",
    "NotificationChannel",
    "NotificationDispatch",
    "NotificationEventType",
    "NotificationPayload",
    "OutboundMessage",
    "PriorityAlert",
    "PriorityAlertNotificationRouter",
    "RecordingAdapter",
    "RouteDecision",
    "SqliteNotificationRepository",
    "default_adapters",
    "priority_alert_deep_link",
]
