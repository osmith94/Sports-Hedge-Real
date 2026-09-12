"""Daily accounting FX snapshots. Distinct from realised conversion FX."""

from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

def require_utc(value: datetime, field_name: str) -> datetime:
    """Require timezone-aware instants and store them as UTC."""

    if value.tzinfo is None:
        raise ValueError(
            f"{field_name} must be timezone-aware UTC; naive datetimes are rejected"
        )
    return value.astimezone(UTC)

RATE_QUANTUM = Decimal("0.00000001")
BPS_QUANTUM = Decimal("0.01")
BPS_SCALE = Decimal("10000")


class FxCheckStatus(StrEnum):
    PENDING_CHECK = "pending_check"
    AGREED = "agreed"
    EXCEPTION = "exception"
    CARRIED_FORWARD = "carried_forward"


class FxRateUnavailable(ValueError):
    """Required FX is missing or stale. Callers must fail closed."""

    def __init__(self, reason: str, detail: str) -> None:
        super().__init__(f"{reason}: {detail}")
        self.reason = reason
        self.detail = detail


class DailyFxRate(BaseModel):
    """One persisted GBP-per-unit daily rate for a currency and valuation date."""

    currency: str
    gbp_per_unit: Decimal = Field(gt=0)
    valuation_date: date
    source_date: date
    retrieved_at: datetime
    primary_source: str
    primary_source_id: str
    raw_primary: dict[str, Any] = Field(default_factory=dict)
    check_source: str | None = None
    check_source_id: str | None = None
    check_gbp_per_unit: Decimal | None = Field(default=None, gt=0)
    raw_check: dict[str, Any] = Field(default_factory=dict)
    variance_bps: Decimal | None = Field(default=None, ge=0)
    status: FxCheckStatus
    captured_at: datetime | None = None

    @field_validator("currency", mode="before")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        return str(value).upper()

    @model_validator(mode="after")
    def normalize(self) -> DailyFxRate:
        self.retrieved_at = require_utc(self.retrieved_at, "retrieved_at")
        if self.captured_at is None:
            self.captured_at = self.retrieved_at
        else:
            self.captured_at = require_utc(self.captured_at, "captured_at")
        if self.currency == "GBP" and self.gbp_per_unit != Decimal("1"):
            raise ValueError("GBP functional-currency rate must equal 1")
        return self


class PublishedFxClose(BaseModel):
    """Official published observation before valuation-date carry-forward."""

    currency: str
    gbp_per_unit: Decimal = Field(gt=0)
    source_date: date
    retrieved_at: datetime
    source: str
    source_id: str
    raw: dict[str, Any] = Field(default_factory=dict)

    @field_validator("currency", mode="before")
    @classmethod
    def uppercase_currency(cls, value: str) -> str:
        return str(value).upper()

    @model_validator(mode="after")
    def aware_retrieved(self) -> PublishedFxClose:
        self.retrieved_at = require_utc(self.retrieved_at, "retrieved_at")
        return self


def variance_bps(primary: Decimal, check: Decimal) -> Decimal:
    if primary <= 0:
        raise ValueError("primary rate must be positive")
    return ((abs(primary - check) / primary) * BPS_SCALE).quantize(BPS_QUANTUM)


def quantize_rate(value: Decimal) -> Decimal:
    return value.quantize(RATE_QUANTUM)


def utcnow() -> datetime:
    return datetime.now(UTC)
