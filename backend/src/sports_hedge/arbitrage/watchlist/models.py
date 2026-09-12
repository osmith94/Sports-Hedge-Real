from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName


class OpportunityStatus(StrEnum):
    WATCHING = "WATCHING"
    APPROACHING = "APPROACHING"
    TRIGGERED = "TRIGGERED"
    PAPER_FILLING = "PAPER_FILLING"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CLOSED = "CLOSED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class OpportunityClassification(StrEnum):
    """Operator-facing label. Below-threshold items are never called arbitrage."""

    WATCH_CANDIDATE = "watch_candidate"
    NEAR_OPPORTUNITY = "near_opportunity"
    TRIGGERED_OPPORTUNITY = "triggered_opportunity"
    REJECTED = "rejected"
    PAPER_FILL = "paper_fill"
    CLOSED = "closed"
    EXPIRED = "expired"


class LifecycleEventType(StrEnum):
    CANDIDATE_FIRST_SEEN = "candidate_first_seen"
    MOVED_CLOSER_TO_TRIGGER = "moved_closer_to_trigger"
    MOVED_FURTHER_FROM_TRIGGER = "moved_further_from_trigger"
    TRIGGER_CROSSED = "trigger_crossed"
    TRIGGER_LOST_BEFORE_FILL = "trigger_lost_before_fill"
    PAPER_FILL_ATTEMPTED = "paper_fill_attempted"
    PAPER_FILL_PARTIAL = "paper_fill_partial"
    PAPER_FILL_COMPLETE = "paper_fill_complete"
    REJECTED_STALE_QUOTE = "rejected_stale_quote"
    REJECTED_INSUFFICIENT_DEPTH = "rejected_insufficient_depth"
    REJECTED_SEMANTICS = "rejected_semantics"
    REJECTED_MISSING_COSTS = "rejected_missing_costs"
    REJECTED_EXECUTION_RISK = "rejected_execution_risk"
    CLOSED = "closed"
    EXPIRED = "expired"


class WatchLeg(BaseModel):
    """One venue/outcome leg with native and GBP amounts kept distinct."""

    outcome: str
    venue: VenueName
    source_market_id: str
    currency: str
    native_stake: Decimal | None = Field(default=None, ge=0)
    gbp_per_unit: Decimal | None = Field(default=None, gt=0)
    gbp_stake: Decimal | None = Field(default=None, ge=0)
    net_decimal_odds: Decimal | None = Field(default=None, gt=1)
    cumulative_depth_gbp: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def normalize_currency(self) -> WatchLeg:
        self.currency = self.currency.upper()
        return self


class WatchObservation(BaseModel):
    """Ingest snapshot for the watchlist. Does not place orders."""

    observed_at: datetime
    canonical_event_id: str
    canonical_market_id: str
    settlement_key: str | None = None
    competition: str | None = None
    home_team: str | None = None
    away_team: str | None = None
    market_family: MarketFamily | None = None
    period: FootballPeriod | None = None
    venues: list[VenueName] = Field(default_factory=list)
    legs: list[WatchLeg] = Field(default_factory=list)
    trigger_net_edge: Decimal = Field(ge=0)
    current_net_edge: Decimal | None = None
    implied_probability_sum: Decimal | None = Field(default=None, gt=0)
    solver_is_arbitrage: bool = False
    eligible_for_paper_simulation: bool = False
    rejection_reasons: list[str] = Field(default_factory=list)
    execution_risk_score: int | None = Field(default=None, ge=0, le=100)
    quote_age_ms: int | None = Field(default=None, ge=0)
    limiting_depth_gbp: Decimal | None = Field(default=None, ge=0)
    limiting_leg_outcome: str | None = None
    capital_required_gbp: Decimal | None = Field(default=None, ge=0)
    guaranteed_profit_gbp: Decimal | None = None
    expected_lock_minutes: Decimal | None = Field(default=None, ge=0)
    kickoff_utc: datetime | None = None

    @model_validator(mode="after")
    def ensure_timezone(self) -> WatchObservation:
        if self.observed_at.tzinfo is None:
            self.observed_at = self.observed_at.replace(tzinfo=UTC)
        if self.kickoff_utc is not None and self.kickoff_utc.tzinfo is None:
            self.kickoff_utc = self.kickoff_utc.replace(tzinfo=UTC)
        if not self.venues:
            self.venues = list(dict.fromkeys(leg.venue for leg in self.legs))
        return self


class NearOpportunity(BaseModel):
    opportunity_id: str
    canonical_event_id: str
    canonical_market_id: str
    settlement_key: str | None = None
    competition: str | None = None
    home_team: str | None = None
    away_team: str | None = None
    market_family: MarketFamily | None = None
    period: FootballPeriod | None = None
    venues: list[VenueName] = Field(default_factory=list)
    legs: list[WatchLeg] = Field(default_factory=list)
    status: OpportunityStatus
    classification: OpportunityClassification
    is_arbitrage: bool = False
    trigger_net_edge: Decimal
    current_net_edge: Decimal | None = None
    distance_to_trigger_pp: Decimal | None = None
    implied_probability_sum: Decimal | None = None
    quote_age_ms: int | None = Field(default=None, ge=0)
    limiting_depth_gbp: Decimal | None = None
    limiting_leg_outcome: str | None = None
    capital_required_gbp: Decimal | None = None
    guaranteed_profit_gbp: Decimal | None = None
    execution_risk_score: int | None = None
    expected_lock_minutes: Decimal | None = None
    first_seen_at: datetime
    last_seen_at: datetime
    rejection_reasons: list[str] = Field(default_factory=list)
    insufficiency_reasons: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def enforce_non_arbitrage_labelling(self) -> NearOpportunity:
        if self.status != OpportunityStatus.TRIGGERED:
            self.is_arbitrage = False
            self.guaranteed_profit_gbp = None
        if self.first_seen_at.tzinfo is None:
            self.first_seen_at = self.first_seen_at.replace(tzinfo=UTC)
        if self.last_seen_at.tzinfo is None:
            self.last_seen_at = self.last_seen_at.replace(tzinfo=UTC)
        return self


class OpportunityLifecycleEvent(BaseModel):
    event_id: str = Field(default_factory=lambda: str(uuid4()))
    opportunity_id: str
    occurred_at: datetime
    event_type: LifecycleEventType
    status: OpportunityStatus
    current_net_edge: Decimal | None = None
    distance_to_trigger_pp: Decimal | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def ensure_timezone(self) -> OpportunityLifecycleEvent:
        if self.occurred_at.tzinfo is None:
            self.occurred_at = self.occurred_at.replace(tzinfo=UTC)
        return self
