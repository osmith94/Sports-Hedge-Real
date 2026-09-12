from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.models import AnnotationCategory, MarketEventAnnotation


class ScanTrigger(StrEnum):
    """Market-state scan triggers that are not Market Intelligence event labels.

    Sporting/news event truth uses :class:`AnnotationCategory` from Market
    Intelligence. These values only describe cross-venue scan conditions.
    """

    ABNORMAL_CROSS_VENUE_SPREAD = "abnormal_cross_venue_spread"
    RAPID_PRICE_MOVE = "rapid_price_move"
    VENUE_REOPEN_AFTER_SUSPENSION = "venue_reopen_after_suspension"


class ScanPriority(StrEnum):
    NORMAL = "NORMAL"
    ELEVATED = "ELEVATED"
    BURST = "BURST"
    CRITICAL = "CRITICAL"


PRIORITY_RANK: dict[ScanPriority, int] = {
    ScanPriority.NORMAL: 0,
    ScanPriority.ELEVATED: 1,
    ScanPriority.BURST: 2,
    ScanPriority.CRITICAL: 3,
}

EVENT_MATERIALITY: dict[AnnotationCategory, Decimal] = {
    AnnotationCategory.RED_CARD: Decimal("1.00"),
    AnnotationCategory.GOAL: Decimal("0.90"),
    AnnotationCategory.PENALTY: Decimal("0.85"),
    AnnotationCategory.INJURY_NEWS: Decimal("0.75"),
    AnnotationCategory.SUBSTITUTION: Decimal("0.70"),
    AnnotationCategory.PLAYER_OUT: Decimal("0.65"),
    AnnotationCategory.TEAM_SHEET: Decimal("0.45"),
}

SCAN_TRIGGER_MATERIALITY: dict[ScanTrigger, Decimal] = {
    ScanTrigger.VENUE_REOPEN_AFTER_SUSPENSION: Decimal("0.80"),
    ScanTrigger.ABNORMAL_CROSS_VENUE_SPREAD: Decimal("0.55"),
    ScanTrigger.RAPID_PRICE_MOVE: Decimal("0.50"),
}

MATERIAL_EVENT_CATEGORIES: frozenset[AnnotationCategory] = frozenset(
    {
        AnnotationCategory.RED_CARD,
        AnnotationCategory.GOAL,
        AnnotationCategory.PENALTY,
        AnnotationCategory.INJURY_NEWS,
        AnnotationCategory.SUBSTITUTION,
        AnnotationCategory.PLAYER_OUT,
    }
)

MATERIAL_SCAN_TRIGGERS: frozenset[ScanTrigger] = frozenset(
    {ScanTrigger.VENUE_REOPEN_AFTER_SUSPENSION}
)


def event_category_from_label(label: str) -> AnnotationCategory | None:
    """Resolve a provider-neutral Market Intelligence category label.

    Unknown labels fail closed (return None) rather than being guessed.
    Burst-only scan triggers are not event-ingestion categories.
    """

    token = label.strip().casefold().replace("-", "_").replace(" ", "_")
    try:
        return AnnotationCategory(token)
    except ValueError:
        return None


def event_annotation_from_market_intelligence(
    annotation: MarketEventAnnotation,
    *,
    retrieved_at: datetime,
    source_timestamp: datetime | None = None,
) -> "EventAnnotationInput":
    """Adapt a canonical MI annotation without inventing a second event model."""

    return EventAnnotationInput(
        canonical_event_id=annotation.canonical_event_id,
        category=annotation.category,
        occurred_at=annotation.occurred_at,
        retrieved_at=retrieved_at,
        source_timestamp=source_timestamp,
        source=annotation.source,
        confidence=annotation.confidence,
    )


def require_aware_utc(value: datetime, field_name: str) -> datetime:
    """Reject naive or non-UTC datetimes. Never guess UTC."""

    if value.tzinfo is None:
        raise ValueError(
            f"{field_name} must be timezone-aware UTC; naive datetimes are rejected"
        )
    offset = value.utcoffset()
    if offset is None or offset != timedelta(0):
        raise ValueError(
            f"{field_name} must be UTC; non-UTC offsets are rejected rather than converted"
        )
    return value


class CanonicalEventContext(BaseModel):
    canonical_event_id: str
    canonical_market_id: str
    as_of: datetime
    kickoff_utc: datetime | None = None
    match_state: str | None = None
    economically_equivalent_venues: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def require_utc_timestamps(self) -> "CanonicalEventContext":
        self.as_of = require_aware_utc(self.as_of, "as_of")
        if self.kickoff_utc is not None:
            self.kickoff_utc = require_aware_utc(self.kickoff_utc, "kickoff_utc")
        return self

    @property
    def minutes_to_kickoff(self) -> float | None:
        if self.kickoff_utc is None:
            return None
        return (self.kickoff_utc - self.as_of).total_seconds() / 60.0


class EventAnnotationInput(BaseModel):
    """Typed seam for a Market Intelligence event annotation.

    `occurred_at` is the sporting/source event time. `retrieved_at` is when
    Sports Hedge learned about it. They must remain distinct. Category labels
    are :class:`AnnotationCategory` values, not a second event-truth enum.
    """

    canonical_event_id: str
    category: AnnotationCategory
    occurred_at: datetime
    retrieved_at: datetime
    source_timestamp: datetime | None = None
    source: str = "authorised_public_feed"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    @model_validator(mode="after")
    def require_utc_timestamps(self) -> "EventAnnotationInput":
        self.occurred_at = require_aware_utc(self.occurred_at, "occurred_at")
        self.retrieved_at = require_aware_utc(self.retrieved_at, "retrieved_at")
        if self.source_timestamp is not None:
            self.source_timestamp = require_aware_utc(self.source_timestamp, "source_timestamp")
        return self


class VenueQuoteSnapshot(BaseModel):
    """One venue quote observation. Timestamps are not interchangeable."""

    venue: VenueName
    canonical_event_id: str
    canonical_market_id: str
    canonical_outcome: str
    quote_timestamp: datetime
    retrieved_at: datetime
    source_timestamp: datetime | None = None
    decimal_odds: Decimal = Field(gt=Decimal("1"))
    implied_probability: Decimal | None = Field(default=None, gt=0, lt=1)
    executable_depth: Decimal = Field(default=Decimal("0"), ge=0)
    liquidity: Decimal = Field(default=Decimal("0"), ge=0)
    suspended: bool = False
    settlement_semantics_complete: bool = False
    costs_and_fx_complete: bool = False
    quote_age_ms: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def derive_fields(self) -> "VenueQuoteSnapshot":
        self.quote_timestamp = require_aware_utc(self.quote_timestamp, "quote_timestamp")
        self.retrieved_at = require_aware_utc(self.retrieved_at, "retrieved_at")
        if self.source_timestamp is not None:
            self.source_timestamp = require_aware_utc(self.source_timestamp, "source_timestamp")
        if self.implied_probability is None:
            self.implied_probability = Decimal("1") / self.decimal_odds
        age_ms = (self.retrieved_at - self.quote_timestamp).total_seconds() * 1000
        if age_ms < 0:
            raise ValueError(
                "quote_timestamp later than retrieved_at is inconsistent future data"
            )
        if self.quote_age_ms is None:
            self.quote_age_ms = int(age_ms)
        return self


class NearArbSignal(BaseModel):
    """Typed seam for Agent Z / near-arb watchlist distance. Optional."""

    distance_to_trigger: Decimal = Field(ge=0)
    near_arb_active: bool = False
    near_arb_duration_seconds: float | None = Field(default=None, ge=0)
    validated_arb_active: bool = False
    validated_arb_duration_seconds: float | None = Field(default=None, ge=0)


class RateBudget(BaseModel):
    """Authorised-API scan budget. Burst mode must not exceed this."""

    max_scans: int = Field(ge=0)
    remaining_scans: int | None = Field(default=None, ge=0)
    authorised_public_feeds_only: Literal[True] = True

    @property
    def available_scans(self) -> int:
        if self.remaining_scans is None:
            return self.max_scans
        return min(self.max_scans, self.remaining_scans)


class ScanCandidate(BaseModel):
    """Self-contained input for one canonical event/market scan decision."""

    event: CanonicalEventContext
    quotes: list[VenueQuoteSnapshot] = Field(default_factory=list)
    annotation: EventAnnotationInput | None = None
    scan_trigger: ScanTrigger | None = None
    near_arb: NearArbSignal | None = None
    execution_risk_score: int = Field(default=50, ge=0, le=100)
    market_liquidity: Decimal = Field(default=Decimal("0"), ge=0)
    headline_move: Decimal = Field(default=Decimal("0"), ge=0)
    paper_mode: Literal[True] = True

    @model_validator(mode="after")
    def bind_quotes_to_event(self) -> "ScanCandidate":
        if not self.paper_mode:
            raise ValueError("Event-driven dislocation scanning is paper-only")
        if self.annotation is not None and (
            self.annotation.canonical_event_id != self.event.canonical_event_id
        ):
            raise ValueError("annotation.canonical_event_id must match event.canonical_event_id")
        for quote in self.quotes:
            if quote.canonical_event_id != self.event.canonical_event_id:
                raise ValueError(
                    "quote.canonical_event_id must match event.canonical_event_id"
                )
            if quote.canonical_market_id != self.event.canonical_market_id:
                raise ValueError(
                    "quote.canonical_market_id must match event.canonical_market_id"
                )
        return self


class QuoteEligibility(BaseModel):
    venue: VenueName
    canonical_outcome: str
    executable: bool
    reasons: list[str] = Field(default_factory=list)
    quote_timestamp: datetime
    retrieved_at: datetime


class PriorityFactors(BaseModel):
    event_materiality: Decimal
    cross_venue_dispersion: Decimal
    market_liquidity: Decimal
    quote_freshness: Decimal
    equivalent_venues: Decimal
    near_arb_proximity: Decimal
    executable_depth: Decimal
    execution_quality: Decimal
    rate_budget_headroom: Decimal
    headline_move: Decimal
    headline_move_weight: Decimal = Decimal("0")


class BurstPriorityDecision(BaseModel):
    """Inspectable burst-priority decision. This is not an arbitrage result.

    ``validated_price_dislocation`` means fresh, depth-positive, cost- and
    settlement-complete quotes disagree across venues. Executable arb remains
    the existing complete-set solver / watchlist output.
    """

    canonical_event_id: str
    canonical_market_id: str
    priority: ScanPriority
    composite_score: Decimal
    factors: PriorityFactors
    reasons: list[str] = Field(default_factory=list)
    validated_price_dislocation: bool
    cross_venue_dispersion: Decimal = Field(default=Decimal("0"), ge=0)
    fresh_executable_venues: int = Field(ge=0)
    quote_eligibility: list[QuoteEligibility] = Field(default_factory=list)
    event_occurred_at: datetime | None = None
    evaluated_at: datetime
    paper_mode: Literal[True] = True
    data_class: Literal["modelled"] = "modelled"
    authorised_public_feeds_only: Literal[True] = True

    @property
    def priority_rank(self) -> int:
        return PRIORITY_RANK[self.priority]


class VenueReactionState(BaseModel):
    venue: VenueName
    suspended: bool = False
    suspended_at: datetime | None = None
    reopened_at: datetime | None = None
    first_fresh_post_event_quote_at: datetime | None = None
    first_material_repricing_at: datetime | None = None
    last_quote_timestamp: datetime | None = None
    last_implied_probability: Decimal | None = None


class DislocationState(BaseModel):
    canonical_event_id: str
    canonical_market_id: str
    event_occurred_at: datetime | None = None
    event_retrieved_at: datetime | None = None
    venues: dict[str, VenueReactionState] = Field(default_factory=dict)
    current_dispersion: Decimal | None = None
    dispersion_peak: Decimal | None = None
    dispersion_peak_at: datetime | None = None
    dislocation_started_at: datetime | None = None
    dislocation_ended_at: datetime | None = None
    dislocation_duration_seconds: float | None = None
    near_arb_duration_seconds: float | None = None
    validated_arb_duration_seconds: float | None = None
    causality_claim: None = None
    paper_mode: Literal[True] = True
    data_class: Literal["modelled"] = "modelled"


class ScheduledCandidate(BaseModel):
    rank: int = Field(ge=1)
    selected: bool
    budget_reason: str
    decision: BurstPriorityDecision


class ScanSchedule(BaseModel):
    selected: list[ScheduledCandidate] = Field(default_factory=list)
    deferred: list[ScheduledCandidate] = Field(default_factory=list)
    budget_limit: int = Field(ge=0)
    selected_count: int = Field(ge=0)
    paper_mode: Literal[True] = True
    authorised_public_feeds_only: Literal[True] = True
    ranking_key: str = (
        "composite_score desc, canonical_event_id asc, canonical_market_id asc"
    )
