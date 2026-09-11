from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from sports_hedge.arbitrage.depth import DepthScanResult
from sports_hedge.fees.models import FeeSnapshot
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.risk.execution import ExecutionRiskResult


class FxRateSnapshot(BaseModel):
    currency: str
    gbp_per_unit: Decimal = Field(gt=0)
    source: str = "paper_assumption"
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @model_validator(mode="after")
    def normalize(self) -> "FxRateSnapshot":
        self.currency = self.currency.upper()
        if self.captured_at.tzinfo is None:
            self.captured_at = self.captured_at.replace(tzinfo=UTC)
        return self


class PaperScanDecision(BaseModel):
    scanned_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    market_match: MarketMatchResult
    canonical_event_id: str | None = None
    canonical_market_id: str | None = None
    snapshots_recorded: int = Field(default=0, ge=0)
    depth_scan: DepthScanResult | None = None
    execution_risk: ExecutionRiskResult | None = None
    eligible_for_paper_simulation: bool = False
    rejection_reasons: list[str] = Field(default_factory=list)
    fee_snapshots: list[FeeSnapshot] = Field(default_factory=list)
    fx_snapshots: list[FxRateSnapshot] = Field(default_factory=list)
    minimum_net_edge: Decimal = Field(default=Decimal("0"), ge=0)
    maximum_execution_risk: int = Field(default=100, ge=0, le=100)
    quote_age_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def ensure_timezone(self) -> "PaperScanDecision":
        if self.scanned_at.tzinfo is None:
            self.scanned_at = self.scanned_at.replace(tzinfo=UTC)
        return self
