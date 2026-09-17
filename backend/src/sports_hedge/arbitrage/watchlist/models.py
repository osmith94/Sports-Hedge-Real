from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from sports_hedge.application.quote_freshness import require_aware_instant
from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.learned_rules import MappingProvenance, MappingReviewCandidate


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


ORPHANED_PAPER_FILLING_RECONCILED = "orphaned_paper_filling_reconciled"


class PaperFillAttemptStatus(StrEnum):
    STARTED = "started"
    REJECTED = "rejected"
    COMPLETE = "complete"


class PaperFillAttempt(BaseModel):
    """Durable paper-fill attempt identity. Bound snapshot is explicit, not inferred."""

    attempt_id: str
    opportunity_id: str
    bound_snapshot: bool = False
    status: PaperFillAttemptStatus = PaperFillAttemptStatus.STARTED
    started_at: datetime
    decision_at: datetime | None = None
    finished_at: datetime | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def ensure_timezone(self) -> PaperFillAttempt:
        self.started_at = require_aware_instant(self.started_at, "started_at")
        if self.decision_at is not None:
            self.decision_at = require_aware_instant(self.decision_at, "decision_at")
        if self.finished_at is not None:
            self.finished_at = require_aware_instant(self.finished_at, "finished_at")
        return self


class LifecycleEventType(StrEnum):
    CANDIDATE_FIRST_SEEN = "candidate_first_seen"
    MOVED_CLOSER_TO_TRIGGER = "moved_closer_to_trigger"
    MOVED_FURTHER_FROM_TRIGGER = "moved_further_from_trigger"
    TRIGGER_CROSSED = "trigger_crossed"
    TRIGGER_LOST_BEFORE_FILL = "trigger_lost_before_fill"
    PAPER_FILL_ATTEMPTED = "paper_fill_attempted"
    PAPER_FILL_PARTIAL = "paper_fill_partial"
    PAPER_FILL_COMPLETE = "paper_fill_complete"
    PAPER_FILL_REJECTED = "paper_fill_rejected"
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
    source_runner_id: str | None = None
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
    gross_edge: Decimal | None = None
    implied_probability_sum: Decimal | None = Field(default=None, gt=0)
    solver_is_arbitrage: bool = False
    solver_model: str | None = None
    eligible_for_paper_simulation: bool = False
    rejection_reasons: list[str] = Field(default_factory=list)
    execution_risk_score: int | None = Field(default=None, ge=0, le=100)
    quote_age_ms: int | None = Field(default=None, ge=0)
    quote_age_basis: str | None = None
    limiting_depth_gbp: Decimal | None = Field(default=None, ge=0)
    limiting_leg_outcome: str | None = None
    capital_required_gbp: Decimal | None = Field(default=None, ge=0)
    guaranteed_profit_gbp: Decimal | None = None
    expected_lock_minutes: Decimal | None = Field(default=None, ge=0)
    kickoff_utc: datetime | None = None
    fixture_discovery_source: VenueName | None = None
    fixture_status: str | None = None
    in_running: bool | None = None
    live_score_supported: bool = False
    home_score: int | None = Field(default=None, ge=0)
    away_score: int | None = Field(default=None, ge=0)
    data_kind: str = "live_paper"
    mapping_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    mapping_matched: bool | None = None
    mapping_reasons: list[str] = Field(default_factory=list)
    mapping_provenance: MappingProvenance | None = None
    mapping_review_candidate: MappingReviewCandidate | None = None

    @model_validator(mode="after")
    def ensure_timezone(self) -> WatchObservation:
        self.observed_at = require_aware_instant(self.observed_at, "observed_at")
        if self.kickoff_utc is not None:
            self.kickoff_utc = require_aware_instant(self.kickoff_utc, "kickoff_utc")
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
    gross_edge: Decimal | None = None
    distance_to_trigger_pp: Decimal | None = None
    implied_probability_sum: Decimal | None = None
    quote_age_ms: int | None = Field(default=None, ge=0)
    quote_age_basis: str | None = None
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
    fixture_discovery_source: VenueName | None = None
    fixture_status: str | None = None
    in_running: bool | None = None
    live_score_supported: bool = False
    home_score: int | None = Field(default=None, ge=0)
    away_score: int | None = Field(default=None, ge=0)
    strike_narrative: str | None = None
    previous_net_edge: Decimal | None = None
    previous_distance_to_trigger_pp: Decimal | None = None
    observation_count: int = Field(default=0, ge=0)
    data_kind: str = "live_paper"
    bet_actionable: bool = False
    bet_blocked_reason: str | None = None
    scan_lane: str | None = None
    last_scanned_at: datetime | None = None
    next_due_at: datetime | None = None
    freshness_class: str | None = None
    mapping_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    mapping_matched: bool | None = None
    mapping_reasons: list[str] = Field(default_factory=list)
    mapping_provenance: MappingProvenance | None = None
    mapping_review_candidate: MappingReviewCandidate | None = None

    @model_validator(mode="after")
    def enforce_non_arbitrage_labelling(self) -> NearOpportunity:
        if self.status != OpportunityStatus.TRIGGERED:
            self.is_arbitrage = False
            self.guaranteed_profit_gbp = None
        self.first_seen_at = require_aware_instant(self.first_seen_at, "first_seen_at")
        self.last_seen_at = require_aware_instant(self.last_seen_at, "last_seen_at")
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
        self.occurred_at = require_aware_instant(self.occurred_at, "occurred_at")
        return self


def paper_fill_lifecycle_event_id(
    opportunity_id: str,
    event_type: LifecycleEventType,
    attempt_id: str | None = None,
) -> str:
    """Stable lifecycle id. Attempt-scoped events stay idempotent inside one attempt."""

    if attempt_id:
        return f"{opportunity_id}:{event_type.value}:{attempt_id}"
    return f"{opportunity_id}:{event_type.value}"


class OpportunityObservationPoint(BaseModel):
    """Append-only net-edge sample. Sequence is observational, not causal."""

    opportunity_id: str
    observed_at: datetime
    current_net_edge: Decimal | None = None
    distance_to_trigger_pp: Decimal | None = None
    quote_age_ms: int | None = Field(default=None, ge=0)
    status: OpportunityStatus

    @model_validator(mode="after")
    def ensure_timezone(self) -> OpportunityObservationPoint:
        self.observed_at = require_aware_instant(self.observed_at, "observed_at")
        return self


def strike_distance_narrative(
    points: list[OpportunityObservationPoint],
) -> tuple[str | None, Decimal | None, Decimal | None]:
    """Describe recent distance-to-trigger change without claiming cause."""

    if len(points) < 2:
        return "insufficient_history", None, None
    previous, current = points[-2], points[-1]
    if previous.distance_to_trigger_pp is None or current.distance_to_trigger_pp is None:
        return "insufficient_history", previous.current_net_edge, previous.distance_to_trigger_pp
    if current.distance_to_trigger_pp < previous.distance_to_trigger_pp:
        label = "approaching"
    elif current.distance_to_trigger_pp > previous.distance_to_trigger_pp:
        label = "moving_away"
    else:
        label = "stable"
    return label, previous.current_net_edge, previous.distance_to_trigger_pp
