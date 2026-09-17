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
    into GBP should set currency to GBP; the simulator does not apply FX or invent
    a reporting-currency total. Arb net edge and guaranteed profit stay on
    `ArbitrageSolution` / Near-Arb observations — this is an execution fill, not a
    second economics truth model.
    """

    outcome: str
    venue: VenueName
    source_market_id: str
    source_runner_id: str
    currency: str = "GBP"
    requested_stake: Decimal = Field(gt=Decimal("0"))
    displayed_odds: Decimal = Field(gt=Decimal("1"))
    levels: list[BookLevel] = Field(default_factory=list)
    quote_age_ms: int | None = Field(default=None, ge=0)
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
    max_quote_age_ms: int | None = Field(default=2000, ge=0)
    slippage_bps: Decimal = Field(default=Decimal("0"), ge=0)
    price_impact_bps: Decimal = Field(default=Decimal("0"), ge=0)
    ms_per_skipped_level: int = Field(default=0, ge=0)

    @property
    def is_ideal(self) -> bool:
        return self.mode is FillMode.IDEAL


class PaperFillRecord(BaseModel):
    """Auditable simulated fill ready for later ledger posting.

    `theoretical_payout` / `realised_payout` are this leg's return if its outcome
    wins. They are not opportunity P&L and must not be summed across mutually
    exclusive arb legs. Solver and Near-Arb observations remain the source of
    complete-set edge and guaranteed profit.
    """

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
    quote_age_ms: int | None = Field(default=None, ge=0)
    quote_captured_at: datetime | None = None

    @model_validator(mode="after")
    def ensure_timezone(self) -> "PaperFillRecord":
        if self.filled_at.tzinfo is None:
            self.filled_at = self.filled_at.replace(tzinfo=UTC)
        if self.quote_captured_at is not None and self.quote_captured_at.tzinfo is None:
            self.quote_captured_at = self.quote_captured_at.replace(tzinfo=UTC)
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


class PaperNativeStakeTotals(BaseModel):
    """Requested/filled residual for one venue+currency. Never mixed across FX."""

    currency: str
    venue: VenueName
    requested_stake: Decimal
    filled_stake: Decimal
    remaining_stake: Decimal

    @model_validator(mode="after")
    def normalize_currency(self) -> "PaperNativeStakeTotals":
        self.currency = self.currency.upper()
        return self


class PaperOpportunityFills(BaseModel):
    """Per-leg fills for one paper opportunity.

    Monetary payout/profit stays on each `PaperFillRecord`. Mutually exclusive
    legs must not be summed into an opportunity-level return; complete-set edge
    and guaranteed profit remain on `ArbitrageSolution` and Near-Arb observations.
    Native stake totals are grouped by venue and currency so GBP and USD are
    never added together.
    """

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
    def fully_filled(self) -> bool:
        return bool(self.fills) and all(fill.fully_filled for fill in self.fills)

    @property
    def max_slippage_bps(self) -> Decimal:
        if not self.fills:
            return Decimal("0")
        return max(fill.slippage_bps for fill in self.fills)

    @property
    def rejection_reasons(self) -> list[str]:
        return [fill.rejection_reason for fill in self.fills if fill.rejection_reason]

    def native_stake_totals(self) -> list[PaperNativeStakeTotals]:
        grouped: dict[tuple[str, VenueName], PaperNativeStakeTotals] = {}
        for fill in self.fills:
            key = (fill.currency, fill.venue)
            current = grouped.get(key)
            if current is None:
                grouped[key] = PaperNativeStakeTotals(
                    currency=fill.currency,
                    venue=fill.venue,
                    requested_stake=fill.requested_stake,
                    filled_stake=fill.filled_stake,
                    remaining_stake=fill.remaining_stake,
                )
            else:
                current.requested_stake += fill.requested_stake
                current.filled_stake += fill.filled_stake
                current.remaining_stake += fill.remaining_stake
        return sorted(grouped.values(), key=lambda item: (item.currency, item.venue.value))


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
