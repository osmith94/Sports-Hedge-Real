from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sports_hedge.notifications.models import (
    AlertSeverity,
    InAppNotificationStatus,
    InAppPriorityNotification,
    NotificationChannel,
    NotificationDispatch,
    NotificationEventType,
    NotificationPayload,
)


class SqliteNotificationRepository:
    """Durable in-app Priority Alert notification read model."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS priority_notifications (
                notification_id TEXT PRIMARY KEY,
                opportunity_key TEXT NOT NULL UNIQUE,
                canonical_event_id TEXT NOT NULL,
                canonical_market_id TEXT NOT NULL,
                priority_alert_id TEXT NOT NULL,
                severity TEXT NOT NULL,
                status TEXT NOT NULL,
                last_event TEXT NOT NULL,
                event_summary TEXT NOT NULL,
                market_summary TEXT NOT NULL,
                net_guaranteed_edge TEXT NOT NULL,
                recommended_size TEXT NOT NULL,
                expected_guaranteed_profit TEXT NOT NULL,
                limiting_venue TEXT NOT NULL,
                limiting_leg TEXT NOT NULL,
                quote_freshness_ms INTEGER NOT NULL,
                deep_link_path TEXT NOT NULL,
                alert_created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                last_outbound_at TEXT,
                unread INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_priority_notifications_updated
                ON priority_notifications(updated_at DESC);
            CREATE INDEX IF NOT EXISTS idx_priority_notifications_unread
                ON priority_notifications(unread, updated_at DESC);

            CREATE TABLE IF NOT EXISTS notification_dispatches (
                dispatch_id TEXT PRIMARY KEY,
                notification_id TEXT NOT NULL,
                channel TEXT NOT NULL,
                event TEXT NOT NULL,
                suppressed INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                payload_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_notification_dispatches_notification
                ON notification_dispatches(notification_id, created_at DESC);
            """
        )
        self._connection.commit()

    def get(self, notification_id: str) -> InAppPriorityNotification | None:
        row = self._connection.execute(
            "SELECT * FROM priority_notifications WHERE notification_id = ?",
            (notification_id,),
        ).fetchone()
        return None if row is None else _notification_from_row(row)

    def get_by_opportunity_key(self, opportunity_key: str) -> InAppPriorityNotification | None:
        row = self._connection.execute(
            "SELECT * FROM priority_notifications WHERE opportunity_key = ?",
            (opportunity_key,),
        ).fetchone()
        return None if row is None else _notification_from_row(row)

    def upsert(self, notification: InAppPriorityNotification) -> InAppPriorityNotification:
        self._connection.execute(
            """
            INSERT INTO priority_notifications (
                notification_id, opportunity_key, canonical_event_id, canonical_market_id,
                priority_alert_id, severity, status, last_event, event_summary, market_summary,
                net_guaranteed_edge, recommended_size, expected_guaranteed_profit,
                limiting_venue, limiting_leg, quote_freshness_ms, deep_link_path,
                alert_created_at, expires_at, created_at, updated_at, last_outbound_at, unread
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(opportunity_key) DO UPDATE SET
                notification_id = excluded.notification_id,
                canonical_event_id = excluded.canonical_event_id,
                canonical_market_id = excluded.canonical_market_id,
                priority_alert_id = excluded.priority_alert_id,
                severity = excluded.severity,
                status = excluded.status,
                last_event = excluded.last_event,
                event_summary = excluded.event_summary,
                market_summary = excluded.market_summary,
                net_guaranteed_edge = excluded.net_guaranteed_edge,
                recommended_size = excluded.recommended_size,
                expected_guaranteed_profit = excluded.expected_guaranteed_profit,
                limiting_venue = excluded.limiting_venue,
                limiting_leg = excluded.limiting_leg,
                quote_freshness_ms = excluded.quote_freshness_ms,
                deep_link_path = excluded.deep_link_path,
                alert_created_at = excluded.alert_created_at,
                expires_at = excluded.expires_at,
                created_at = excluded.created_at,
                updated_at = excluded.updated_at,
                last_outbound_at = excluded.last_outbound_at,
                unread = excluded.unread
            """,
            (
                notification.notification_id,
                notification.opportunity_key,
                notification.canonical_event_id,
                notification.canonical_market_id,
                notification.priority_alert_id,
                notification.severity.value,
                notification.status.value,
                notification.last_event.value,
                notification.event_summary,
                notification.market_summary,
                str(notification.net_guaranteed_edge),
                str(notification.recommended_size),
                str(notification.expected_guaranteed_profit),
                notification.limiting_venue,
                notification.limiting_leg,
                notification.quote_freshness_ms,
                notification.deep_link_path,
                notification.alert_created_at.isoformat(),
                notification.expires_at.isoformat(),
                notification.created_at.isoformat(),
                notification.updated_at.isoformat(),
                None
                if notification.last_outbound_at is None
                else notification.last_outbound_at.isoformat(),
                int(notification.unread),
            ),
        )
        self._connection.commit()
        stored = self.get(notification.notification_id)
        if stored is None:
            raise RuntimeError("failed to persist priority notification")
        return stored

    def list_recent(
        self,
        *,
        limit: int = 50,
        unread_only: bool = False,
    ) -> list[InAppPriorityNotification]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        clauses: list[str] = []
        parameters: list[Any] = []
        if unread_only:
            clauses.append("unread = 1")
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        rows = self._connection.execute(
            f"SELECT * FROM priority_notifications{where} ORDER BY updated_at DESC LIMIT ?",  # noqa: S608
            parameters,
        ).fetchall()
        return [_notification_from_row(row) for row in rows]

    def unread_count(self) -> int:
        row = self._connection.execute(
            "SELECT COUNT(*) AS n FROM priority_notifications WHERE unread = 1"
        ).fetchone()
        return int(row["n"])

    def mark_read(self, notification_id: str) -> InAppPriorityNotification | None:
        notification = self.get(notification_id)
        if notification is None:
            return None
        notification.unread = False
        return self.upsert(notification)

    def append_dispatch(self, dispatch: NotificationDispatch) -> NotificationDispatch:
        self._connection.execute(
            """
            INSERT INTO notification_dispatches (
                dispatch_id, notification_id, channel, event, suppressed, reason,
                created_at, payload_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                dispatch.dispatch_id,
                dispatch.notification_id,
                dispatch.channel.value,
                dispatch.event.value,
                int(dispatch.suppressed),
                dispatch.reason,
                dispatch.created_at.isoformat(),
                dispatch.payload.model_dump_json(),
            ),
        )
        self._connection.commit()
        return dispatch

    def list_dispatches(self, notification_id: str) -> list[NotificationDispatch]:
        rows = self._connection.execute(
            """
            SELECT * FROM notification_dispatches
            WHERE notification_id = ?
            ORDER BY created_at DESC
            """,
            (notification_id,),
        ).fetchall()
        return [_dispatch_from_row(row) for row in rows]

    def close(self) -> None:
        self._connection.close()


def _notification_from_row(row: sqlite3.Row) -> InAppPriorityNotification:
    last_outbound = row["last_outbound_at"]
    return InAppPriorityNotification(
        notification_id=row["notification_id"],
        opportunity_key=row["opportunity_key"],
        canonical_event_id=row["canonical_event_id"],
        canonical_market_id=row["canonical_market_id"],
        priority_alert_id=row["priority_alert_id"],
        severity=AlertSeverity(row["severity"]),
        status=InAppNotificationStatus(row["status"]),
        last_event=NotificationEventType(row["last_event"]),
        event_summary=row["event_summary"],
        market_summary=row["market_summary"],
        net_guaranteed_edge=Decimal(row["net_guaranteed_edge"]),
        recommended_size=Decimal(row["recommended_size"]),
        expected_guaranteed_profit=Decimal(row["expected_guaranteed_profit"]),
        limiting_venue=row["limiting_venue"],
        limiting_leg=row["limiting_leg"],
        quote_freshness_ms=int(row["quote_freshness_ms"]),
        deep_link_path=row["deep_link_path"],
        alert_created_at=datetime.fromisoformat(row["alert_created_at"]),
        expires_at=datetime.fromisoformat(row["expires_at"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        last_outbound_at=None if last_outbound is None else datetime.fromisoformat(last_outbound),
        unread=bool(row["unread"]),
    )


def _dispatch_from_row(row: sqlite3.Row) -> NotificationDispatch:
    return NotificationDispatch(
        dispatch_id=row["dispatch_id"],
        notification_id=row["notification_id"],
        channel=NotificationChannel(row["channel"]),
        event=NotificationEventType(row["event"]),
        suppressed=bool(row["suppressed"]),
        reason=row["reason"],
        created_at=datetime.fromisoformat(row["created_at"]),
        payload=NotificationPayload.model_validate(json.loads(row["payload_json"])),
    )
