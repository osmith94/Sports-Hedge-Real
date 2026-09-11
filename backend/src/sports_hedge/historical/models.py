from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class CompetitionType(StrEnum):
    LEAGUE = "league"
    CUP = "cup"
    CONTINENTAL = "continental"


class MatchStatus(StrEnum):
    SCHEDULED = "scheduled"
    FINISHED = "finished"
    POSTPONED = "postponed"
    ABANDONED = "abandoned"


class MatchEventType(StrEnum):
    GOAL = "goal"
    YELLOW_CARD = "yellow_card"
    RED_CARD = "red_card"
    PENALTY = "penalty"
    SUBSTITUTION = "substitution"


class DataQualityFlag(StrEnum):
    MISSING_HT_SCORE = "missing_ht_score"
    MISSING_FT_SCORE = "missing_ft_score"
    MISSING_GOAL_TIMESTAMPS = "missing_goal_timestamps"
    MISSING_CORNERS = "missing_corners"
    MISSING_YELLOW_CARDS = "missing_yellow_cards"
    MISSING_RED_CARDS = "missing_red_cards"
    MISSING_PENALTIES = "missing_penalties"
    MISSING_SUBSTITUTIONS = "missing_substitutions"
    MISSING_LINEUPS = "missing_lineups"
    PARTIAL_LINEUPS = "partial_lineups"


class Competition(BaseModel):
    competition_id: str
    name: str
    country: str | None = None
    competition_type: CompetitionType = CompetitionType.LEAGUE


class Season(BaseModel):
    season_id: str
    competition_id: str
    label: str
    start_year: int
    end_year: int


class Team(BaseModel):
    team_id: str
    canonical_name: str
    country: str | None = None


class SourceProvenance(BaseModel):
    source_name: str
    source_match_id: str
    source_url: str | None = None
    retrieved_at: datetime
    source_timestamp: datetime | None = None
    raw_payload_hash: str
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    quality_flags: list[DataQualityFlag] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)


class MatchRecord(BaseModel):
    match_id: str
    competition_id: str
    season_id: str
    kickoff_utc: datetime
    home_team_id: str
    away_team_id: str
    home_team_name: str
    away_team_name: str
    competition_name: str
    season_label: str
    status: MatchStatus = MatchStatus.FINISHED
    home_ft_goals: int | None = None
    away_ft_goals: int | None = None
    home_ht_goals: int | None = None
    away_ht_goals: int | None = None
    venue_name: str | None = None


class MatchEventRecord(BaseModel):
    event_id: str
    match_id: str
    event_type: MatchEventType
    minute: int | None = None
    extra_minute: int | None = None
    team_id: str | None = None
    team_name: str | None = None
    player_name: str | None = None
    related_player_name: str | None = None
    period: str | None = None
    source_name: str
    source_event_id: str


class TeamMatchStatsRecord(BaseModel):
    match_id: str
    team_id: str
    team_name: str
    is_home: bool
    competition_id: str
    season_id: str
    corners: int | None = None
    yellow_cards: int | None = None
    red_cards: int | None = None
    penalties: int | None = None


class LineupRecord(BaseModel):
    lineup_id: str
    match_id: str
    team_id: str
    team_name: str
    player_name: str
    shirt_number: int | None = None
    position: str | None = None
    is_starter: bool = True
    source_name: str


class CoverageCell(BaseModel):
    competition_id: str
    competition_name: str
    season_id: str
    season_label: str
    field_name: str
    matches_total: int
    matches_present: int
    coverage_ratio: float
    missing_match_ids: list[str] = Field(default_factory=list)


class SourceMatchPayload(BaseModel):
    """Source-neutral fixture bundle supplied by a historical adapter."""

    source_name: str
    source_match_id: str
    source_url: str | None = None
    retrieved_at: datetime
    source_timestamp: datetime | None = None
    competition_name: str
    season_label: str
    kickoff_utc: datetime
    home_team: str
    away_team: str
    status: MatchStatus = MatchStatus.FINISHED
    home_ft_goals: int | None = None
    away_ft_goals: int | None = None
    home_ht_goals: int | None = None
    away_ht_goals: int | None = None
    venue_name: str | None = None
    home_corners: int | None = None
    away_corners: int | None = None
    home_yellow_cards: int | None = None
    away_yellow_cards: int | None = None
    home_red_cards: int | None = None
    away_red_cards: int | None = None
    home_penalties: int | None = None
    away_penalties: int | None = None
    events: list[dict[str, Any]] = Field(default_factory=list)
    lineups: list[dict[str, Any]] = Field(default_factory=list)
    raw_payload: dict[str, Any] = Field(default_factory=dict)
