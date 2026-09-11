from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, model_validator

from sports_hedge.domain.football import (
    FootballPeriod,
    MarketFamily,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName


class AnnotationCategory(StrEnum):
    TEAM_SHEET = "team_sheet"
    PLAYER_OUT = "player_out"
    PLAYER_IN = "player_in"
    INJURY_NEWS = "injury_news"
    MANAGER_NEWS = "manager_news"
    CLUB_ANNOUNCEMENT = "club_announcement"
    JOURNALIST_REPORT = "journalist_report"
    NEWS_ARTICLE = "news_article"
    WEATHER = "weather"
    KICKOFF = "kickoff"
    GOAL = "goal"
    RED_CARD = "red_card"
    YELLOW_CARD = "yellow_card"
    PENALTY = "penalty"
    SUBSTITUTION = "substitution"
    HALF_TIME = "half_time"
    FULL_TIME = "full_time"
    MANUAL_NOTE = "manual_note"
    OTHER = "other"


class KickoffBucket(StrEnum):
    OVER_24H = "over_24h"
    H24_TO_H3 = "24h_to_3h"
    H3_TO_H2 = "3h_to_2h"
    H2_TO_M45 = "2h_to_45m"
    M45_TO_KICKOFF = "45m_to_kickoff"
    IN_PLAY = "in_play"
    UNKNOWN = "unknown"


class MarketSnapshot(BaseModel):
    """Append-only canonical observation of one market outcome at one venue."""

    snapshot_id: str = Field(default_factory=lambda: str(uuid4()))
    observed_at: datetime
    venue: VenueName
    canonical_event_id: str
    canonical_market_id: str
    canonical_outcome: str
    market_family: MarketFamily
    period: FootballPeriod = FootballPeriod.UNKNOWN
    market_line: Decimal | None = None
    settlement_scope: SettlementScope = SettlementScope.UNKNOWN
    settlement_key: str | None = None
    competition: str | None = None
    home_team: str | None = None
    away_team: str | None = None
    source_event_id: str | None = None
    source_market_id: str | None = None
    source_outcome_id: str | None = None
    kickoff_utc: datetime | None = None
    decimal_odds: Decimal = Field(gt=Decimal("1"))
    implied_probability: Decimal | None = Field(default=None, gt=0, lt=1)
    best_back_odds: Decimal | None = Field(default=None, gt=1)
    best_lay_odds: Decimal | None = Field(default=None, gt=1)
    back_size: Decimal | None = Field(default=None, ge=0)
    lay_size: Decimal | None = Field(default=None, ge=0)
    spread_decimal: Decimal | None = Field(default=None, ge=0)
    total_liquidity: Decimal | None = Field(default=None, ge=0)
    source_latency_ms: int | None = Field(default=None, ge=0)
    order_book: list[dict[str, Any]] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def derive_market_fields(self) -> "MarketSnapshot":
        if self.observed_at.tzinfo is None:
            self.observed_at = self.observed_at.replace(tzinfo=UTC)
        if self.kickoff_utc is not None and self.kickoff_utc.tzinfo is None:
            self.kickoff_utc = self.kickoff_utc.replace(tzinfo=UTC)

        if self.implied_probability is None:
            self.implied_probability = Decimal("1") / self.decimal_odds
        if (
            self.spread_decimal is None
            and self.best_back_odds is not None
            and self.best_lay_odds is not None
            and self.best_lay_odds >= self.best_back_odds
        ):
            self.spread_decimal = self.best_lay_odds - self.best_back_odds
        return self

    @property
    def minutes_to_kickoff(self) -> float | None:
        if self.kickoff_utc is None:
            return None
        return (self.kickoff_utc - self.observed_at).total_seconds() / 60.0


class MarketEventAnnotation(BaseModel):
    annotation_id: str = Field(default_factory=lambda: str(uuid4()))
    canonical_event_id: str
    occurred_at: datetime
    category: AnnotationCategory
    source: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    title: str
    source_url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def ensure_timezone(self) -> "MarketEventAnnotation":
        if self.occurred_at.tzinfo is None:
            self.occurred_at = self.occurred_at.replace(tzinfo=UTC)
        return self


class MovementScore(BaseModel):
    sample_size: int = Field(ge=0)
    move_probability_points: Decimal
    percentile: float | None = Field(default=None, ge=0.0, le=100.0)
    robust_z_score: float | None = None
    sufficient_sample: bool
    thin_liquidity: bool = False


class ReversionPoint(BaseModel):
    horizon_minutes: int = Field(gt=0)
    observed_probability: Decimal | None = None
    retracement_fraction: float | None = None
    classification: str = "unavailable"


class ReversionAnalysis(BaseModel):
    baseline_probability: Decimal
    shock_probability: Decimal
    shock_time: datetime
    initial_move_probability_points: Decimal
    points: list[ReversionPoint] = Field(default_factory=list)


class HistoricalCohortStats(BaseModel):
    sample_size: int = Field(ge=0)
    median: float | None = None
    p25: float | None = None
    p75: float | None = None
    minimum: float | None = None
    maximum: float | None = None
    sufficient_sample: bool
