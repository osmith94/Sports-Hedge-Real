from __future__ import annotations

from datetime import datetime


def require_aware(value: datetime, field: str) -> datetime:
    """Reject naive datetimes. Never invent UTC."""

    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        raise ValueError(
            f"{field} must be timezone-aware; naive historical timestamps are rejected"
        )
    return value
