from __future__ import annotations

from datetime import UTC, date, datetime
from decimal import Decimal

from pydantic import BaseModel, Field, model_validator

from sports_hedge.application.quote_freshness import require_aware_instant
from sports_hedge.arbitrage.depth import DepthScanResult
from sports_hedge.paper.fills import PaperOpportunityLeg
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import VenueCostSnapshot
from sports_hedge.fees.models import FeeSnapshot
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.risk.execution import ExecutionRiskResult


BPS_SCALE = Decimal("10000")


class FxRateSnapshot(BaseModel):
    currency: str
    gbp_per_unit: Decimal = Field(gt=0)
    source: str = "paper_assumption"
    captured_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
    spread_bps: Decimal | None = Field(default=None, ge=0)
    conversion_slippage_bps: Decimal | None = Field(default=None, ge=0)
    source_date: date | None = None
    valuation_date: date | None = None
    check_status: str | None = None
    variance_bps: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def normalize(self) -> "FxRateSnapshot":
        self.currency = self.currency.upper()
        self.captured_at = require_aware_instant(self.captured_at, "captured_at")
        return self

    def effective_gbp_per_unit(
        self,
        *,
        configured_spread_bps: Decimal | int = 0,
        configured_conversion_slippage_bps: Decimal | int = 0,
    ) -> tuple[Decimal, list[str]]:
        """Worsen the mid rate by explicit or configured FX spread/slippage.

        Conservative for converting native depth into GBP: fewer pounds per unit.
        GBP functional rate stays 1 with no spread. Labels record assumptions.
        """

        if self.currency == "GBP":
            if self.gbp_per_unit != Decimal("1"):
                raise ValueError("GBP functional-currency rate must equal 1")
            return Decimal("1"), []

        labels: list[str] = []
        spread = self.spread_bps
        if spread is None:
            spread = Decimal(configured_spread_bps)
            labels.append(f"configured_fx_spread_bps:{spread}")
        else:
            labels.append(f"explicit_fx_spread_bps:{spread}")
        slippage = self.conversion_slippage_bps
        if slippage is None:
            slippage = Decimal(configured_conversion_slippage_bps)
            if slippage > 0:
                labels.append(f"configured_fx_conversion_slippage_bps:{slippage}")
        elif slippage > 0:
            labels.append(f"explicit_fx_conversion_slippage_bps:{slippage}")
        haircut = Decimal("1") - (spread + slippage) / BPS_SCALE
        if haircut <= 0:
            raise ValueError("FX spread and conversion slippage consume the entire rate")
        return self.gbp_per_unit * haircut, labels


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
    venue_costs: list[VenueCostSnapshot] = Field(default_factory=list)
    fx_snapshots: list[FxRateSnapshot] = Field(default_factory=list)
    cost_assumption_labels: list[str] = Field(default_factory=list)
    fill_legs: list[PaperOpportunityLeg] = Field(default_factory=list)
    execution_modes: dict[VenueName, str] = Field(default_factory=dict)
    minimum_net_edge: Decimal = Field(default=Decimal("0"), ge=0)
    maximum_execution_risk: int = Field(default=100, ge=0, le=100)
    quote_age_ms: int | None = Field(default=None, ge=0)
    quote_age_basis: str | None = None
    fixture_discovery_source: VenueName | None = None
    fixture_status: str | None = None
    in_running: bool | None = None
    live_score_supported: bool = False
    home_score: int | None = Field(default=None, ge=0)
    away_score: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def ensure_timezone(self) -> "PaperScanDecision":
        if self.scanned_at.tzinfo is None:
            self.scanned_at = self.scanned_at.replace(tzinfo=UTC)
        return self
