from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from sports_hedge.domain.models import VenueName


class FootballPeriod(StrEnum):
    FULL_TIME = "full_time"
    FIRST_HALF = "first_half"
    SECOND_HALF = "second_half"
    EXTRA_TIME = "extra_time"
    UNKNOWN = "unknown"


class MarketFamily(StrEnum):
    MATCH_RESULT = "match_result"
    DRAW_NO_BET = "draw_no_bet"
    DOUBLE_CHANCE = "double_chance"
    BOTH_TEAMS_TO_SCORE = "both_teams_to_score"
    TOTAL_GOALS = "total_goals"
    ASIAN_HANDICAP = "asian_handicap"
    TEAM_TOTAL = "team_total"
    CORRECT_SCORE = "correct_score"
    HALF_TIME_FULL_TIME = "half_time_full_time"
    TO_QUALIFY = "to_qualify"
    NEXT_GOAL = "next_goal"
    CORNERS = "corners"
    CARDS = "cards"
    PLAYER_PROPS = "player_props"
    UNKNOWN = "unknown"


class SettlementScope(StrEnum):
    REGULATION_TIME = "regulation_time"
    INCLUDING_EXTRA_TIME = "including_extra_time"
    INCLUDING_PENALTIES = "including_penalties"
    PERIOD_ONLY = "period_only"
    UNKNOWN = "unknown"


class CanonicalOutcome(StrEnum):
    HOME = "home"
    DRAW = "draw"
    AWAY = "away"
    YES = "yes"
    NO = "no"
    OVER = "over"
    UNDER = "under"
    HOME_OR_DRAW = "home_or_draw"
    HOME_OR_AWAY = "home_or_away"
    DRAW_OR_AWAY = "draw_or_away"
    HOME_QUALIFY = "home_qualify"
    AWAY_QUALIFY = "away_qualify"
    OTHER = "other"


class SettlementFingerprint(BaseModel):
    """Economic resolution rules that must agree before markets can be matched."""

    scope: SettlementScope = SettlementScope.UNKNOWN
    period: FootballPeriod = FootballPeriod.UNKNOWN
    line: Decimal | None = None
    push_possible: bool | None = None
    penalties_included: bool | None = None
    extra_time_included: bool | None = None
    abandonment_rule: str | None = None
    postponement_rule: str | None = None
    source_rule_version: str | None = None

    def deterministic_key(self) -> str:
        """Return only economically relevant settlement semantics.

        ``source_rule_version`` is retained on the model for provenance/audit but is
        deliberately excluded here. A venue-specific rule document identifier is
        not itself an economic difference and must not prevent two otherwise
        equivalent markets from matching.
        """

        values = (
            self.scope,
            self.period,
            "" if self.line is None else format(self.line, "f"),
            self.push_possible,
            self.penalties_included,
            self.extra_time_included,
            self.abandonment_rule or "",
            self.postponement_rule or "",
        )
        return "|".join(str(value) for value in values)

    def is_economically_complete(self) -> bool:
        """Required settlement evidence must be known before markets can compare.

        Incomplete fingerprints are not equivalent merely because unknown fields
        match. ``source_rule_version`` remains provenance-only.
        """

        if self.scope is SettlementScope.UNKNOWN:
            return False
        if self.period is FootballPeriod.UNKNOWN:
            return False
        if self.extra_time_included is None:
            return False
        if self.penalties_included is None:
            return False
        return True


def line_push_possible(line: Decimal | None) -> bool | None:
    """Integer lines can push/void; half-lines cannot; quarter-lines are unknown."""

    if line is None:
        return None
    twice = line * Decimal("2")
    if twice != twice.to_integral_value():
        return None
    return line == line.to_integral_value()


class CanonicalEvent(BaseModel):
    sport: str = "football"
    competition: str
    home_team: str
    away_team: str
    kickoff_utc: datetime
    source_venue: VenueName
    source_event_id: str
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    def identity_tuple(self) -> tuple[str, str, str, str, datetime]:
        return (
            self.sport,
            self.competition,
            self.home_team,
            self.away_team,
            self.kickoff_utc,
        )


class CanonicalRunner(BaseModel):
    source_runner_id: str
    outcome: CanonicalOutcome
    label: str


class CanonicalMarket(BaseModel):
    event: CanonicalEvent
    source_venue: VenueName
    source_market_id: str
    family: MarketFamily
    period: FootballPeriod
    line: Decimal | None = None
    settlement: SettlementFingerprint
    runners: list[CanonicalRunner] = Field(default_factory=list)
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)

    def economic_key(self) -> tuple[str, FootballPeriod, Decimal | None, str]:
        return (
            self.family.value,
            self.period,
            self.line,
            self.settlement.deterministic_key(),
        )
