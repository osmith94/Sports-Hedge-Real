from __future__ import annotations

from datetime import timedelta
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status

from sports_hedge.config import get_settings
from sports_hedge.notifications.canonical import PriorityAlertNotificationAdapter
from sports_hedge.notifications.models import (
    InAppPriorityNotification,
    RouteDecision,
)
from sports_hedge.notifications.repository import SqliteNotificationRepository
from sports_hedge.notifications.router import (
    PriorityAlertNotificationRouter,
    default_adapters,
)

router = APIRouter(prefix="/notifications", tags=["notifications"])


@lru_cache
def get_notification_router() -> PriorityAlertNotificationRouter:
    settings = get_settings()
    database = settings.notifications_db_path
    if database != ":memory:":
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
    return PriorityAlertNotificationRouter(
        SqliteNotificationRepository(database),
        default_adapters(),
        cooldown=timedelta(seconds=settings.notification_cooldown_seconds),
    )


@router.post(
    "/priority-alerts",
    response_model=RouteDecision,
    status_code=status.HTTP_200_OK,
)
def route_priority_alert(
    alert: PriorityAlertNotificationAdapter,
    service: PriorityAlertNotificationRouter = Depends(get_notification_router),
) -> RouteDecision:
    return service.route(alert)


@router.get("", response_model=list[InAppPriorityNotification])
def list_notifications(
    limit: int = Query(default=50, ge=1, le=500),
    unread_only: bool = False,
    service: PriorityAlertNotificationRouter = Depends(get_notification_router),
) -> list[InAppPriorityNotification]:
    return service.list_recent(limit=limit, unread_only=unread_only)


@router.get("/unread-count")
def unread_count(
    service: PriorityAlertNotificationRouter = Depends(get_notification_router),
) -> dict[str, int]:
    return {"unread_count": service.unread_count()}


@router.post(
    "/{notification_id}/read",
    response_model=InAppPriorityNotification,
)
def mark_read(
    notification_id: str,
    service: PriorityAlertNotificationRouter = Depends(get_notification_router),
) -> InAppPriorityNotification:
    notification = service.mark_read(notification_id)
    if notification is None:
        raise HTTPException(status_code=404, detail="notification not found")
    return notification
