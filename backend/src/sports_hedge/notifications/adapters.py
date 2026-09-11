from __future__ import annotations

import logging
from typing import Protocol, runtime_checkable

from sports_hedge.notifications.models import NotificationChannel, OutboundMessage

logger = logging.getLogger(__name__)


@runtime_checkable
class ChannelAdapter(Protocol):
    """Provider-neutral outbound notification seam.

    Phase 1 implementations must not place bets, sign wallets, or call venue orders.
    """

    channel: NotificationChannel

    def send(self, message: OutboundMessage) -> None: ...


class RecordingAdapter:
    """In-memory adapter used by tests and local dry-runs."""

    def __init__(self, channel: NotificationChannel) -> None:
        self.channel = channel
        self.sent: list[OutboundMessage] = []

    def send(self, message: OutboundMessage) -> None:
        if message.channel != self.channel:
            raise ValueError(
                f"adapter {self.channel.value} cannot send {message.channel.value} messages"
            )
        self.sent.append(message)


class ConsoleEmailAdapter:
    """Phase 1 email seam: logs the payload instead of calling a paid provider."""

    channel = NotificationChannel.EMAIL

    def send(self, message: OutboundMessage) -> None:
        logger.info(
            "priority-alert email [%s] %s -> %s",
            message.event.value,
            message.subject,
            message.payload.deep_link_path,
        )


class NoOpAdapter:
    """Explicit unused-channel seam (PUSH/SMS until a provider is chosen)."""

    def __init__(self, channel: NotificationChannel) -> None:
        self.channel = channel
        self.sent: list[OutboundMessage] = []

    def send(self, message: OutboundMessage) -> None:
        raise RuntimeError(
            f"{self.channel.value} notifications are not enabled in Phase 1; "
            "wire a provider adapter before enabling this channel"
        )
