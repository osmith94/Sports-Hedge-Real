"""Provider-neutral Priority Alert notification routing."""

from sports_hedge.notifications.adapters import (
    ChannelAdapter,
    ConsoleEmailAdapter,
    NoOpAdapter,
    RecordingAdapter,
)
from sports_hedge.notifications.canonical import (
    PriorityAlertNotificationAdapter,
    notification_adapter_from_priority_alert,
    priority_alert_deep_link,
)
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
from sports_hedge.notifications.router import PriorityAlertNotificationRouter, default_adapters

__all__ = [
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
    "PriorityAlertNotificationAdapter",
    "PriorityAlertNotificationRouter",
    "RecordingAdapter",
    "RouteDecision",
    "SqliteNotificationRepository",
    "default_adapters",
    "notification_adapter_from_priority_alert",
    "priority_alert_deep_link",
]
