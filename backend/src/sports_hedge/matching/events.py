from __future__ import annotations

from datetime import timedelta
from difflib import SequenceMatcher

from pydantic import BaseModel, Field

from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.facts.aliases import resolve_team_name


class EventMatchResult(BaseModel):
    matched: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str]


class EventMatcher:
    def __init__(self, *, kickoff_tolerance: timedelta = timedelta(minutes=5), threshold: float = 0.92) -> None:
        self.kickoff_tolerance = kickoff_tolerance
        self.threshold = threshold

    @staticmethod
    def _similarity(left: str, right: str) -> float:
        return SequenceMatcher(a=left, b=right).ratio()

    def match(self, left: CanonicalEvent, right: CanonicalEvent) -> EventMatchResult:
        reasons: list[str] = []

        if left.sport != right.sport:
            return EventMatchResult(matched=False, confidence=0.0, reasons=["sport_mismatch"])

        kickoff_delta = abs(left.kickoff_utc - right.kickoff_utc)
        if kickoff_delta > self.kickoff_tolerance:
            return EventMatchResult(matched=False, confidence=0.0, reasons=["kickoff_outside_tolerance"])

        home_score = self._similarity(
            resolve_team_name(left.home_team),
            resolve_team_name(right.home_team),
        )
        away_score = self._similarity(
            resolve_team_name(left.away_team),
            resolve_team_name(right.away_team),
        )
        competition_score = self._competition_score(left.competition, right.competition)
        kickoff_score = 1.0 - (kickoff_delta.total_seconds() / self.kickoff_tolerance.total_seconds())

        confidence = (
            0.35 * home_score
            + 0.35 * away_score
            + 0.10 * competition_score
            + 0.20 * kickoff_score
        )

        if home_score < 1.0:
            reasons.append("home_team_fuzzy")
        if away_score < 1.0:
            reasons.append("away_team_fuzzy")
        if competition_score < 1.0:
            reasons.append("competition_fuzzy")
        if kickoff_delta.total_seconds() > 0:
            reasons.append("kickoff_offset")

        return EventMatchResult(
            matched=confidence >= self.threshold,
            confidence=round(confidence, 6),
            reasons=reasons,
        )

    @staticmethod
    def _competition_score(left: str, right: str) -> float:
        from sports_hedge.application.target_competitions import resolve_target_competition

        left_target = resolve_target_competition(left)
        right_target = resolve_target_competition(right)
        if left_target is not None and right_target is not None:
            return 1.0 if left_target.code == right_target.code else 0.0
        return EventMatcher._similarity(left, right)
