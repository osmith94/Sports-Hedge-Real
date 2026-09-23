"""Date-stable FX fixtures for tests that evaluate against wall-clock now()."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sports_hedge.fx.models import PublishedFxClose

LONDON = ZoneInfo("Europe/London")


def fresh_usd_ecb_close(
    gbp_per_unit: Decimal = Decimal("0.75000000"),
    *,
    as_of: datetime | None = None,
    age_days: int = 1,
) -> PublishedFxClose:
    """Published USD close still inside the 7-day scanner stale window."""

    as_of = as_of or datetime.now(UTC)
    london_date = as_of.astimezone(LONDON).date()
    source_date = london_date - timedelta(days=max(0, age_days))
    retrieved = datetime(source_date.year, source_date.month, source_date.day, 16, tzinfo=UTC)
    retrieved = min(retrieved, as_of)
    return PublishedFxClose(
        currency="USD",
        gbp_per_unit=gbp_per_unit,
        source_date=source_date,
        retrieved_at=retrieved,
        source="ecb_eurofxref",
        source_id=f"ecb:{source_date.isoformat()}:USD",
    )
