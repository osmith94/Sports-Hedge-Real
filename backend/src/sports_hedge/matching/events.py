from __future__ import annotations

from datetime import timedelta
from difflib import SequenceMatcher
from functools import lru_cache

from pydantic import BaseModel, Field

from sports_hedge.domain.football import CanonicalEvent, CanonicalMarket
from sports_hedge.facts.aliases import resolve_team_name
from sports_hedge.matching.learned_rules import (
    AppliedLearnedRule,
    LearnedMappingApplicator,
    MappingProvenance,
    provenance_from_applied,
    squad_categories_compatible,
)


@lru_cache(maxsize=4096)
def _resolve_static_team_name(value: str) -> str:
    """Cache immutable curated aliases used repeatedly during bulk clustering."""

    return resolve_team_name(value)


class EventMatchResult(BaseModel):
    matched: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str]
    provenance: MappingProvenance = Field(default_factory=MappingProvenance)


class EventMatcher:
    def __init__(
        self,
        *,
        kickoff_tolerance: timedelta = timedelta(minutes=5),
        threshold: float = 0.92,
        learned_applicator: LearnedMappingApplicator | None = None,
    ) -> None:
        self.kickoff_tolerance = kickoff_tolerance
        self.threshold = threshold
        self.learned_applicator = learned_applicator

    def bulk_snapshot(self) -> EventMatcher:
        """Freeze enabled learned rules once for one deterministic bulk pass."""

        if type(self) is not EventMatcher:
            return self
        if self.learned_applicator is None:
            return self
        rules = self.learned_applicator.enabled_rules()
        return EventMatcher(
            kickoff_tolerance=self.kickoff_tolerance,
            threshold=self.threshold,
            learned_applicator=LearnedMappingApplicator(rules=rules) if rules else None,
        )

    @staticmethod
    def _similarity(left: str, right: str) -> float:
        if left == right:
            return 1.0
        return SequenceMatcher(a=left, b=right).ratio()

    def could_match(
        self,
        left: CanonicalEvent,
        right: CanonicalEvent,
        *,
        left_market: CanonicalMarket | None = None,
        right_market: CanonicalMarket | None = None,
    ) -> bool:
        """Cheap conservative prefilter for bulk clustering.

        ``SequenceMatcher.quick_ratio`` is an upper bound on ``ratio``. Using
        the maximum possible competition score means ``False`` cannot exclude
        a pair that could reach this matcher's unchanged confidence threshold.
        """

        if left.sport != right.sport:
            return False
        if not squad_categories_compatible(left.home_team, right.home_team) or not squad_categories_compatible(
            left.away_team, right.away_team
        ):
            return False
        kickoff_delta = abs(left.kickoff_utc - right.kickoff_utc)
        if kickoff_delta > self.kickoff_tolerance:
            return False
        left_home, left_away, _ = self._resolved_teams(
            left, right, market=left_market, counterpart_market=right_market
        )
        right_home, right_away, _ = self._resolved_teams(
            right, left, market=right_market, counterpart_market=left_market
        )
        home_upper = SequenceMatcher(a=left_home, b=right_home).quick_ratio()
        away_upper = SequenceMatcher(a=left_away, b=right_away).quick_ratio()
        tolerance_seconds = self.kickoff_tolerance.total_seconds()
        kickoff_score = (
            1.0
            if tolerance_seconds <= 0
            else 1.0 - (kickoff_delta.total_seconds() / tolerance_seconds)
        )
        confidence_upper = (
            0.35 * home_upper
            + 0.35 * away_upper
            + 0.10
            + 0.20 * kickoff_score
        )
        return confidence_upper >= self.threshold

    def match(
        self,
        left: CanonicalEvent,
        right: CanonicalEvent,
        *,
        left_market: CanonicalMarket | None = None,
        right_market: CanonicalMarket | None = None,
    ) -> EventMatchResult:
        reasons: list[str] = []

        if left.sport != right.sport:
            return EventMatchResult(matched=False, confidence=0.0, reasons=["sport_mismatch"])

        if not squad_categories_compatible(left.home_team, right.home_team) or not squad_categories_compatible(
            left.away_team, right.away_team
        ):
            return EventMatchResult(
                matched=False,
                confidence=0.0,
                reasons=["participant_squad_category_mismatch"],
            )

        kickoff_delta = abs(left.kickoff_utc - right.kickoff_utc)
        if kickoff_delta > self.kickoff_tolerance:
            return EventMatchResult(
                matched=False,
                confidence=0.0,
                reasons=["kickoff_outside_tolerance"],
            )

        left_home, left_away, left_applied = self._resolved_teams(
            left, right, market=left_market, counterpart_market=right_market
        )
        right_home, right_away, right_applied = self._resolved_teams(
            right, left, market=right_market, counterpart_market=left_market
        )
        applied = [*left_applied, *right_applied]
        provenance = provenance_from_applied(applied)

        home_score = self._similarity(left_home, right_home)
        away_score = self._similarity(left_away, right_away)
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
        if applied:
            reasons.append("operator_verified_learned_alias")
            if provenance.rule_id:
                reasons.append(f"learned_rule:{provenance.rule_id}:v{provenance.rule_version}")

        return EventMatchResult(
            matched=confidence >= self.threshold,
            confidence=round(confidence, 6),
            reasons=reasons,
            provenance=provenance,
        )

    def _resolved_teams(
        self,
        event: CanonicalEvent,
        counterpart: CanonicalEvent,
        *,
        market: CanonicalMarket | None,
        counterpart_market: CanonicalMarket | None,
    ) -> tuple[str, str, list[AppliedLearnedRule]]:
        if self.learned_applicator is None:
            return (
                _resolve_static_team_name(event.home_team),
                _resolve_static_team_name(event.away_team),
                [],
            )
        return self.learned_applicator.resolve_teams(
            event,
            counterpart,
            market=market,
            counterpart_market=counterpart_market,
        )

    @staticmethod
    def _competition_score(left: str, right: str) -> float:
        from sports_hedge.application.target_competitions import resolve_target_competition

        left_target = resolve_target_competition(left)
        right_target = resolve_target_competition(right)
        if left_target is not None and right_target is not None:
            return 1.0 if left_target.code == right_target.code else 0.0
        return EventMatcher._similarity(left, right)
