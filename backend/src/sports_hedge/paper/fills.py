from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from sports_hedge.domain.models import VenueName
from sports_hedge.liquidity.book import BookLevel


BPS_SCALE = Decimal("10000")


class FillMode(StrEnum):
    IDEAL = "ideal"
    REALISTIC = "realistic"


class PaperOpportunityLeg(BaseModel):
    """One canonical opportunity leg plus the visible back/buy book used to fill it.

    Stakes are denominated in `currency`. Callers that have already converted depth
    into GBP should set currency to GBP; the simulator does not apply FX.
    """

    outcome: str
    venue: VenueName
    source_market_id: str
    source_runner_id: str
    currency: str = "GBP"
    requested_stake: Decimal = Field(gt=Decimal("0"))
    displayed_odds: Decimal = Field(gt=Decimal("1"))
    levels: list[BookLevel] = Field(default_factory=list)
    quote_age_ms: int = Field(default=0, ge=0)
    quote_captured_at: datetime | None = None

    @model_validator(mode="after")
    def normalize(self) -> "PaperOpportunityLeg":
        self.currency = self.currency.upper()
        if self.quote_captured_at is not None and self.quote_captured_at.tzinfo is None:
            self.quote_captured_at = self.quote_captured_at.replace(tzinfo=UTC)
        return self


class PaperFillConfig(BaseModel):
    """Explicit simulation assumptions. Defaults are conservative and deterministic."""

    mode: FillMode = FillMode.REALISTIC
    assumed_latency_ms: int = Field(default=0, ge=0)
    max_quote_age_ms: int | None = Field(default=1000, ge=0)
    slippage_bps: Decimal = Field(default=Decimal("0"), ge=0)
    price_impact_bps: Decimal = Field(default=Decimal("0"), ge=0)
    ms_per_skipped_level: int = Field(default=0, ge=0)

    @property
    def is_ideal(self) -> bool:
        return self.mode is FillMode.IDEAL


class PaperFillRecord(BaseModel):
    """Auditable simulated fill ready for later ledger posting."""

    fill_id: str = Field(default_factory=lambda: str(uuid4()))
    mode: FillMode
    filled_at: datetime
    venue: VenueName
    currency: str
    source_market_id: str
    source_runner_id: str
    outcome: str
    requested_stake: Decimal
    filled_stake: Decimal
    remaining_stake: Decimal
    displayed_odds: Decimal
    weighted_odds: Decimal | None = None
    worst_odds: Decimal | None = None
    slippage_bps: Decimal = Decimal("0")
    levels_consumed: int = Field(default=0, ge=0)
    fully_filled: bool
    rejection_reason: str | None = None
    assumed_latency_ms: int = Field(default=0, ge=0)
    quote_age_ms: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def ensure_timezone(self) -> "PaperFillRecord":
        if self.filled_at.tzinfo is None:
            self.filled_at = self.filled_at.replace(tzinfo=UTC)
        self.currency = self.currency.upper()
        return self

    @property
    def theoretical_payout(self) -> Decimal:
        return self.requested_stake * self.displayed_odds

    @property
    def realised_payout(self) -> Decimal:
        if self.weighted_odds is None or self.filled_stake <= 0:
            return Decimal("0")
        return self.filled_stake * self.weighted_odds


class PaperOpportunityFills(BaseModel):
    """Bundle of per-leg fills for one paper opportunity."""

    opportunity_id: str
    mode: FillMode
    simulated_at: datetime
    fills: list[PaperFillRecord] = Field(default_factory=list)

    @model_validator(mode="after")
    def ensure_timezone(self) -> "PaperOpportunityFills":
        if self.simulated_at.tzinfo is None:
            self.simulated_at = self.simulated_at.replace(tzinfo=UTC)
        return self

    @property
    def requested_stake(self) -> Decimal:
        return sum((fill.requested_stake for fill in self.fills), Decimal("0"))

    @property
    def filled_stake(self) -> Decimal:
        return sum((fill.filled_stake for fill in self.fills), Decimal("0"))

    @property
    def remaining_stake(self) -> Decimal:
        return sum((fill.remaining_stake for fill in self.fills), Decimal("0"))

    @property
    def theoretical_payout(self) -> Decimal:
        return sum((fill.theoretical_payout for fill in self.fills), Decimal("0"))

    @property
    def realised_payout(self) -> Decimal:
        return sum((fill.realised_payout for fill in self.fills), Decimal("0"))

    @property
    def theoretical_profit(self) -> Decimal:
        return self.theoretical_payout - self.requested_stake

    @property
    def realised_profit(self) -> Decimal:
        return self.realised_payout - self.filled_stake

    @property
    def rejection_reasons(self) -> list[str]:
        return [fill.rejection_reason for fill in self.fills if fill.rejection_reason]


def apply_odds_haircut(odds: Decimal, haircut_bps: Decimal) -> Decimal:
    """Worsen back/buy odds by a basis-point haircut. Never invent a better price."""

    if odds <= 1:
        raise ValueError("odds must exceed 1")
    if haircut_bps < 0:
        raise ValueError("haircut_bps must be non-negative")
    slipped = odds * (Decimal("1") - haircut_bps / BPS_SCALE)
    return slipped


def realised_slippage_bps(displayed_odds: Decimal, filled_odds: Decimal) -> Decimal:
    if displayed_odds <= 0:
        raise ValueError("displayed_odds must be positive")
    return (displayed_odds - filled_odds) / displayed_odds * BPS_SCALE
