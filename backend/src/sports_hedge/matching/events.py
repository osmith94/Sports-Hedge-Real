from __future__ import annotations

from datetime import timedelta
from difflib import SequenceMatcher
from functools import lru_cache

from pydantic import BaseModel, Field

from sports_hedge.domain.football import CanonicalEvent, CanonicalMarket
from sports_hedge.facts.aliases import (
    curated_team_names_conflict,
    is_curated_canonical_team,
    resolve_team_name_for_competition,
)
from sports_hedge.matching.learned_rules import (
    AppliedLearnedRule,
    LearnedMappingApplicator,
    MappingProvenance,
    provenance_from_applied,
    squad_categories_compatible,
)

# Class/default production identity stays conservative. PAPER injects a separate
# runtime threshold via ``paper_event_matcher``; do not reuse deprecated
# ``minimum_mapping_confidence``.
DEFAULT_EVENT_MATCH_THRESHOLD = 0.92
PAPER_EVENT_MATCH_THRESHOLD = 0.80


@lru_cache(maxsize=8192)
def _resolve_static_team_name(value: str, competition: str | None) -> str:
    """Cache immutable curated aliases used repeatedly during bulk clustering."""

    return resolve_team_name_for_competition(value, competition)


def target_competition_code(label: str | None) -> str | None:
    """Registry key such as ``mls`` / ``liga_mx``, or None when unknown."""

    if not label:
        return None
    from sports_hedge.application.target_competitions import resolve_target_competition

    target = resolve_target_competition(label)
    return None if target is None else target.code.value


def known_target_competition_mismatch(left: str | None, right: str | None) -> bool:
    """True when both labels resolve to different TargetCompetitionCode values."""

    left_code = target_competition_code(left)
    right_code = target_competition_code(right)
    return left_code is not None and right_code is not None and left_code != right_code


def paper_event_matcher(
    settings: object | None = None,
    *,
    learned_applicator: LearnedMappingApplicator | None = None,
    kickoff_tolerance: timedelta = timedelta(minutes=5),
) -> "EventMatcher":
    """PAPER collector/scanner EventMatcher using the configurable experiment threshold."""

    if settings is None:
        from sports_hedge.config import get_settings

        settings = get_settings()
    return EventMatcher(
        kickoff_tolerance=kickoff_tolerance,
        threshold=float(getattr(settings, "paper_event_match_threshold")),
        learned_applicator=learned_applicator,
    )


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
        # Default 0.92 is the soccer UNIVERSE/HOT matcher default.
        # PAPER soccer 0.80 injection (#415) is a constructor argument at
        # composition time. NFL Stage 1B must not hardcode 0.92 at call sites
        # or overwrite that injection. Competition-aware aliases stay on
        # learned_applicator.resolve_teams for soccer.
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
        a pair that could reach this matcher's threshold, except for a known
        target-competition mismatch, which is a hard veto.
        """

        if left.sport != right.sport:
            return False
        from sports_hedge.nba.constants import NBA_SPORT
        from sports_hedge.ncaab.detect import is_ncaab_canonical_event
        from sports_hedge.ncaab.teams import ncaab_teams_conflict, resolve_ncaab_team
        from sports_hedge.nfl.constants import NFL_SPORT

        if is_ncaab_canonical_event(left) or is_ncaab_canonical_event(right):
            if not (is_ncaab_canonical_event(left) and is_ncaab_canonical_event(right)):
                return False
            labels = (left.home_team, left.away_team, right.home_team, right.away_team)
            resolved = [resolve_ncaab_team(label) for label in labels]
            if any((item.ambiguous or item.rejected or not item.ok) for item in resolved):
                return False
            if ncaab_teams_conflict(resolved[0].canonical or "", resolved[2].canonical or "") or ncaab_teams_conflict(
                resolved[1].canonical or "", resolved[3].canonical or ""
            ):
                return False
            if (resolved[0].canonical, resolved[1].canonical) != (
                resolved[2].canonical,
                resolved[3].canonical,
            ):
                return False
            kickoff_delta = abs(left.kickoff_utc - right.kickoff_utc)
            return kickoff_delta <= self.kickoff_tolerance

        if left.sport == NFL_SPORT:
            from sports_hedge.nfl.teams import nfl_teams_conflict

            left_home, left_away, _ = self._resolved_teams(
                left, right, market=left_market, counterpart_market=right_market
            )
            right_home, right_away, _ = self._resolved_teams(
                right, left, market=right_market, counterpart_market=left_market
            )
            if nfl_teams_conflict(left_home, right_home) or nfl_teams_conflict(left_away, right_away):
                return False
        elif left.sport == NBA_SPORT:
            from sports_hedge.nba.teams import nba_teams_conflict

            left_home, left_away, _ = self._resolved_teams(
                left, right, market=left_market, counterpart_market=right_market
            )
            right_home, right_away, _ = self._resolved_teams(
                right, left, market=right_market, counterpart_market=left_market
            )
            if nba_teams_conflict(left_home, right_home) or nba_teams_conflict(left_away, right_away):
                return False
        elif not squad_categories_compatible(left.home_team, right.home_team) or not squad_categories_compatible(
            left.away_team, right.away_team
        ):
            return False
        kickoff_delta = abs(left.kickoff_utc - right.kickoff_utc)
        if kickoff_delta > self.kickoff_tolerance:
            return False
        if known_target_competition_mismatch(left.competition, right.competition):
            return False
        left_home, left_away, _ = self._resolved_teams(
            left, right, market=left_market, counterpart_market=right_market
        )
        right_home, right_away, _ = self._resolved_teams(
            right, left, market=right_market, counterpart_market=left_market
        )
        if curated_team_names_conflict(left_home, right_home) or curated_team_names_conflict(
            left_away, right_away
        ):
            return False
        if self._exact_curated_senior_identity(
            left_home=left_home,
            right_home=right_home,
            left_away=left_away,
            right_away=right_away,
            left_competition=left.competition,
            right_competition=right.competition,
            sport=left.sport,
        ):
            # Exact curated identity inside the declared kickoff window must not
            # be excluded by the weighted kickoff penalty. Fuzzy pairs still use
            # the conservative upper-bound formula below.
            return True
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

        from sports_hedge.nba.constants import NBA_SPORT
        from sports_hedge.ncaab.detect import is_ncaab_canonical_event
        from sports_hedge.ncaab.teams import ncaab_teams_conflict, resolve_ncaab_team
        from sports_hedge.nfl.constants import NFL_SPORT

        if is_ncaab_canonical_event(left) or is_ncaab_canonical_event(right):
            if not (is_ncaab_canonical_event(left) and is_ncaab_canonical_event(right)):
                return EventMatchResult(
                    matched=False,
                    confidence=0.0,
                    reasons=["sport_mismatch"],
                )
            for label in (left.home_team, left.away_team, right.home_team, right.away_team):
                resolved = resolve_ncaab_team(label)
                if resolved.ambiguous:
                    return EventMatchResult(
                        matched=False,
                        confidence=0.0,
                        reasons=["ncaab_team_identity_ambiguous"],
                    )
                if resolved.rejected or not resolved.ok:
                    return EventMatchResult(
                        matched=False,
                        confidence=0.0,
                        reasons=[resolved.reason or "ncaab_team_identity_unresolved"],
                    )
        elif left.sport == NFL_SPORT:
            from sports_hedge.nfl.teams import nfl_teams_conflict, resolve_nfl_team

            for label in (left.home_team, left.away_team, right.home_team, right.away_team):
                resolved = resolve_nfl_team(label)
                if resolved.ambiguous:
                    return EventMatchResult(
                        matched=False,
                        confidence=0.0,
                        reasons=["nfl_team_identity_ambiguous"],
                    )
                if resolved.rejected:
                    return EventMatchResult(
                        matched=False,
                        confidence=0.0,
                        reasons=[resolved.reason or "nfl_team_identity_rejected"],
                    )
        elif left.sport == NBA_SPORT:
            from sports_hedge.nba.teams import nba_teams_conflict, resolve_nba_team

            for label in (left.home_team, left.away_team, right.home_team, right.away_team):
                resolved = resolve_nba_team(label)
                if resolved.ambiguous:
                    return EventMatchResult(
                        matched=False,
                        confidence=0.0,
                        reasons=["nba_team_identity_ambiguous"],
                    )
                if resolved.rejected:
                    return EventMatchResult(
                        matched=False,
                        confidence=0.0,
                        reasons=[resolved.reason or "nba_team_identity_rejected"],
                    )
        elif not squad_categories_compatible(left.home_team, right.home_team) or not squad_categories_compatible(
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
        if known_target_competition_mismatch(left.competition, right.competition):
            return EventMatchResult(
                matched=False,
                confidence=0.0,
                reasons=["competition_mismatch"],
            )

        left_home, left_away, left_applied = self._resolved_teams(
            left, right, market=left_market, counterpart_market=right_market
        )
        right_home, right_away, right_applied = self._resolved_teams(
            right, left, market=right_market, counterpart_market=left_market
        )
        applied = [*left_applied, *right_applied]
        provenance = provenance_from_applied(applied)
        if curated_team_names_conflict(left_home, right_home) or curated_team_names_conflict(
            left_away, right_away
        ):
            return EventMatchResult(
                matched=False,
                confidence=0.0,
                reasons=["curated_team_mismatch"],
                provenance=provenance,
            )
        if left.sport == NFL_SPORT and (
            nfl_teams_conflict(left_home, right_home) or nfl_teams_conflict(left_away, right_away)
        ):
            return EventMatchResult(
                matched=False,
                confidence=0.0,
                reasons=["curated_team_mismatch"],
                provenance=provenance,
            )
        if left.sport == NBA_SPORT and (
            nba_teams_conflict(left_home, right_home) or nba_teams_conflict(left_away, right_away)
        ):
            return EventMatchResult(
                matched=False,
                confidence=0.0,
                reasons=["curated_team_mismatch"],
                provenance=provenance,
            )
        if is_ncaab_canonical_event(left) and (
            ncaab_teams_conflict(left_home, right_home) or ncaab_teams_conflict(left_away, right_away)
        ):
            return EventMatchResult(
                matched=False,
                confidence=0.0,
                reasons=["curated_team_mismatch"],
                provenance=provenance,
            )
        if is_ncaab_canonical_event(left):
            exact = (
                left_home == right_home
                and left_away == right_away
                and bool(left_home)
                and bool(left_away)
            )
            kickoff_delta = abs(left.kickoff_utc - right.kickoff_utc)
            if not exact:
                return EventMatchResult(
                    matched=False,
                    confidence=0.0,
                    reasons=["ncaab_team_identity_not_exact"],
                    provenance=provenance,
                )
            reasons: list[str] = []
            if kickoff_delta.total_seconds() > 0:
                reasons.append("kickoff_offset")
            return EventMatchResult(
                matched=True,
                confidence=1.0,
                reasons=reasons,
                provenance=provenance,
            )

        home_score = self._similarity(left_home, right_home)
        away_score = self._similarity(left_away, right_away)
        competition_score = self._competition_score(left.competition, right.competition)
        exact_identity = self._exact_curated_senior_identity(
            left_home=left_home,
            right_home=right_home,
            left_away=left_away,
            right_away=right_away,
            left_competition=left.competition,
            right_competition=right.competition,
            sport=left.sport,
        )
        if exact_identity:
            # Provider sibling events (GAME/BTTS/TOTAL/FTTS) often differ by a
            # few minutes. The 5-minute window is already a hard veto; do not
            # also scale confidence by that offset for exact curated seniors.
            kickoff_score = 1.0
        else:
            kickoff_score = 1.0 - (
                kickoff_delta.total_seconds() / self.kickoff_tolerance.total_seconds()
            )

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
        from sports_hedge.nba.constants import NBA_SPORT
        from sports_hedge.ncaab.detect import is_ncaab_canonical_event
        from sports_hedge.ncaab.teams import resolve_ncaab_team
        from sports_hedge.nfl.constants import NFL_SPORT

        if is_ncaab_canonical_event(event):
            home = resolve_ncaab_team(event.home_team)
            away = resolve_ncaab_team(event.away_team)
            return (
                home.canonical or event.home_team,
                away.canonical or event.away_team,
                [],
            )
        if event.sport == NFL_SPORT:
            from sports_hedge.nfl.teams import resolve_nfl_team

            home = resolve_nfl_team(event.home_team)
            away = resolve_nfl_team(event.away_team)
            competition = target_competition_code(event.competition)
            return (
                home.canonical or _resolve_static_team_name(event.home_team, competition),
                away.canonical or _resolve_static_team_name(event.away_team, competition),
                [],
            )
        if event.sport == NBA_SPORT:
            from sports_hedge.nba.teams import resolve_nba_team

            home = resolve_nba_team(event.home_team)
            away = resolve_nba_team(event.away_team)
            competition = target_competition_code(event.competition)
            return (
                home.canonical or _resolve_static_team_name(event.home_team, competition),
                away.canonical or _resolve_static_team_name(event.away_team, competition),
                [],
            )
        competition = target_competition_code(event.competition)
        if self.learned_applicator is None:
            return (
                _resolve_static_team_name(event.home_team, competition),
                _resolve_static_team_name(event.away_team, competition),
                [],
            )
        return self.learned_applicator.resolve_teams(
            event,
            counterpart,
            market=market,
            counterpart_market=counterpart_market,
        )

    @staticmethod
    def _exact_curated_senior_identity(
        *,
        left_home: str,
        right_home: str,
        left_away: str,
        right_away: str,
        left_competition: str,
        right_competition: str,
        sport: str | None = None,
    ) -> bool:
        """True only for exact curated seniors in the same target competition.

        Unknown, youth, women, and reserve labels stay fail-closed. Fuzzy club
        strings keep the weighted kickoff penalty and this matcher's threshold.
        """

        if left_home != right_home or left_away != right_away:
            return False
        from sports_hedge.nba.constants import NBA_SPORT
        from sports_hedge.nfl.constants import NFL_SPORT

        if sport == NFL_SPORT:
            from sports_hedge.nfl.teams import is_canonical_nfl_team

            if is_canonical_nfl_team(left_home) and is_canonical_nfl_team(left_away):
                return EventMatcher._same_target_competition(left_competition, right_competition)
            return False
        if sport == NBA_SPORT:
            from sports_hedge.nba.teams import is_canonical_nba_team

            if is_canonical_nba_team(left_home) and is_canonical_nba_team(left_away):
                return EventMatcher._same_target_competition(left_competition, right_competition)
            return False
        if not is_curated_canonical_team(left_home) or not is_curated_canonical_team(left_away):
            return False
        return EventMatcher._same_target_competition(left_competition, right_competition)

    @staticmethod
    def _same_target_competition(left: str, right: str) -> bool:
        left_code = target_competition_code(left)
        right_code = target_competition_code(right)
        return left_code is not None and left_code == right_code

    @staticmethod
    def _competition_score(left: str, right: str) -> float:
        left_code = target_competition_code(left)
        right_code = target_competition_code(right)
        if left_code is not None and right_code is not None:
            # Score only. Soccer known-competition mismatch veto (#415) is a
            # hard fail earlier in match()/possible_match and must not be
            # replaced by this 0.0 score when NFL is composed onto that matcher.
            return 1.0 if left_code == right_code else 0.0
        # Fuzzy labels only when one or both competitions are unresolved.
        return EventMatcher._similarity(left, right)
