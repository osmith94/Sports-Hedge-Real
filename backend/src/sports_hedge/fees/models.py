from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from sports_hedge.domain.models import VenueName


class FeeSnapshot(BaseModel):
    """Deterministic fee assumption captured with a paper decision.

    Phase 1 uses an explicit profit-haircut representation rather than silently
    embedding venue costs in quoted prices. A zero rate means "no deterministic
    fee assumption supplied", not a claim that the venue is permanently fee-free.
    """

    venue: VenueName
    profit_haircut_rate: Decimal = Field(default=Decimal("0"), ge=0, lt=1)
    source: str = "paper_assumption"
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    detail: str | None = None

    @model_validator(mode="after")
    def ensure_timezone(self) -> "FeeSnapshot":
        if self.captured_at.tzinfo is None:
            self.captured_at = self.captured_at.replace(tzinfo=UTC)
        return self

    def apply_to_decimal_odds(self, decimal_odds: Decimal) -> Decimal:
        if decimal_odds <= 1:
            raise ValueError("decimal_odds must exceed 1")
        profit = decimal_odds - Decimal("1")
        return Decimal("1") + profit * (Decimal("1") - self.profit_haircut_rate)
