from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from sports_hedge.application.quote_freshness import require_aware_instant
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.effective import profit_commission_net_odds

ZeroRateBasis = Literal["verified_zero", "assumed_zero"]


class FeeSnapshot(BaseModel):
    """Deterministic fee assumption captured with a paper decision.

    Phase 1 uses an explicit profit-haircut representation rather than silently
    embedding venue costs in quoted prices. A missing snapshot is unknown cost.
    An explicit zero must declare ``verified_zero`` or ``assumed_zero``.
    """

    venue: VenueName
    profit_haircut_rate: Decimal = Field(ge=0, lt=1)
    zero_rate_basis: ZeroRateBasis | None = None
    source: str = Field(default="paper_assumption", min_length=1)
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    detail: str | None = None

    @model_validator(mode="after")
    def validate_fee_snapshot(self) -> "FeeSnapshot":
        self.captured_at = require_aware_instant(self.captured_at, "captured_at")
        if self.profit_haircut_rate == 0:
            if self.zero_rate_basis is None:
                raise ValueError(
                    "zero profit_haircut_rate requires zero_rate_basis "
                    "verified_zero or assumed_zero; a missing rate is not zero"
                )
        elif self.zero_rate_basis is not None:
            raise ValueError("zero_rate_basis is only valid when profit_haircut_rate is 0")
        return self

    def apply_to_decimal_odds(self, decimal_odds: Decimal) -> Decimal:
        return profit_commission_net_odds(decimal_odds, self.profit_haircut_rate)
