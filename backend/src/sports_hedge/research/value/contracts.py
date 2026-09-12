from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field, model_validator

from sports_hedge.domain.football import FootballPeriod, MarketFamily, SettlementFingerprint
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import MarketAction, VenueCostSnapshot


class ValueStatus(StrEnum):
    """Classification of a directional scenario-value evaluation.

    Distinct from arbitrage outcomes: VALUE is a probabilistic edge estimate,
    not a guaranteed-profit settlement-state result.
    """

    VALUE = "VALUE"
    NO_VALUE = "NO_VALUE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    STALE_QUOTE = "STALE_QUOTE"
    INSUFFICIENT_LIQUIDITY = "INSUFFICIENT_LIQUIDITY"
    MISSING_COSTS = "MISSING_COSTS"
    SEMANTICS_MISMATCH = "SEMANTICS_MISMATCH"


class DataQuality(StrEnum):
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    UNKNOWN = "unknown"


class CanonicalProposition(BaseModel):
    """The economically identified market claim being valued.

    Settlement fingerprint, family, period and line are the matching keys.
    Venue identity is intentionally absent: the analysis model is provider-neutral.
    """

    event_id: str
    team: str
    opponent: str
    scenario_id: str
    metric: str
    response_window: str
    market_family: MarketFamily
    period: FootballPeriod
    settlement: SettlementFingerprint
    outcome: str
    line: Decimal | None = None


class ScenarioEvidence(BaseModel):
    """Scenario Response Profile outputs plus a model probability for the claim."""

    src: Decimal
    sample_size: int = Field(ge=0)
    confidence: Decimal = Field(ge=0, le=1)
    stability: Decimal = Field(ge=0, le=1)
    data_quality: DataQuality = DataQuality.UNKNOWN
    regime_relevance: Decimal = Field(default=Decimal("1"), ge=0, le=1)
    model_probability: Decimal = Field(gt=0, lt=1)
    model_probability_low: Decimal | None = Field(default=None, ge=0, le=1)
    model_probability_high: Decimal | None = Field(default=None, ge=0, le=1)

    @model_validator(mode="after")
    def validate_interval(self) -> ScenarioEvidence:
        low = self.model_probability_low
        high = self.model_probability_high
        if (low is None) != (high is None):
            raise ValueError("model probability bounds must be supplied together")
        if low is not None and high is not None:
            if low > self.model_probability or high < self.model_probability:
                raise ValueError("model_probability must lie within the supplied interval")
            if low > high:
                raise ValueError("model_probability_low must not exceed model_probability_high")
        return self

    def conservative_probability(self) -> Decimal:
        """Lower posterior/CI bound when present; otherwise the point estimate."""

        if self.model_probability_low is not None:
            return self.model_probability_low
        return self.model_probability

    def interval_width(self) -> Decimal | None:
        if self.model_probability_low is None or self.model_probability_high is None:
            return None
        return self.model_probability_high - self.model_probability_low


class VenueQuote(BaseModel):
    """Provider-neutral quote for one canonical proposition.

    Headline ``displayed_decimal_odds`` are never ranked directly. Net economics
    come from ``cost`` via the shared venue-cost rule for this side/action.
    """

    venue: VenueName
    source_market_id: str
    action: MarketAction
    displayed_decimal_odds: Decimal = Field(gt=Decimal("1"))
    quoted_at: datetime
    settlement: SettlementFingerprint
    market_family: MarketFamily
    period: FootballPeriod
    cost: VenueCostSnapshot
    line: Decimal | None = None
    available_depth: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def cost_must_match_quote(self) -> VenueQuote:
        if self.cost.venue != self.venue:
            raise ValueError("cost snapshot venue must match the quote venue")
        if self.cost.action != self.action:
            raise ValueError("cost snapshot action must match the quote action")
        if (
            self.cost.source_market_id is not None
            and self.cost.source_market_id != self.source_market_id
        ):
            raise ValueError("cost snapshot source_market_id must match the quote")
        return self


class ValueEnginePolicy(BaseModel):
    """Fail-closed thresholds for a paper/research evaluation. Not execution limits."""

    max_quote_age_seconds: Decimal = Field(default=Decimal("60"), gt=0)
    min_available_depth: Decimal = Field(default=Decimal("10"), gt=0)
    min_sample_size: int = Field(default=20, ge=1)
    min_confidence: Decimal = Field(default=Decimal("0.50"), ge=0, le=1)
    min_stability: Decimal = Field(default=Decimal("0.40"), ge=0, le=1)
    min_regime_relevance: Decimal = Field(default=Decimal("0.40"), ge=0, le=1)
    max_probability_interval_width: Decimal = Field(default=Decimal("0.20"), gt=0, le=1)
    reference_venue: VenueName = VenueName.MATCHBOOK


class ScoreComponents(BaseModel):
    """Visible ingredients of the Value Signal Score. Nothing is hidden behind the score."""

    economic_edge: Decimal
    sample_size: Decimal
    confidence: Decimal
    stability: Decimal
    regime_relevance: Decimal
    data_quality: Decimal
    quote_freshness: Decimal
    liquidity: Decimal


class ScenarioValueResult(BaseModel):
    status: ValueStatus
    model_probability: Decimal
    conservative_model_probability: Decimal
    src: Decimal
    sample_size: int
    confidence: Decimal
    stability: Decimal
    data_quality: DataQuality
    regime_relevance: Decimal
    raw_market_implied_probability: Decimal | None = None
    cost_adjusted_market_probability: Decimal | None = None
    probability_edge_pp: Decimal | None = None
    expected_profit_per_unit: Decimal | None = None
    expected_roi: Decimal | None = None
    best_price: Decimal | None = None
    best_net_price: Decimal | None = None
    best_venue: VenueName | None = None
    reference_price: Decimal | None = None
    reference_venue: VenueName | None = None
    quote_age_seconds: Decimal | None = None
    available_depth: Decimal | None = None
    value_signal_score: Decimal | None = None
    score_components: ScoreComponents | None = None
    fee_basis: str | None = None
    fee_snapshot_id: str | None = None
    rejection_reason: str | None = None
    paper_research_only: bool = True
