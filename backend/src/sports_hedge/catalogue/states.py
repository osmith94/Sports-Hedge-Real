"""Tenet 20 operational states and Issue #268 census-v1 archetypes."""

from __future__ import annotations

from enum import StrEnum

from sports_hedge.domain.football import CanonicalOutcome, MarketFamily


class CatalogueApprovalState(StrEnum):
    APPROVED_EQUIVALENT = "approved_equivalent"
    PAPER_ASSUMED_EQUIVALENT = "paper_assumed_equivalent"
    APPROVED_PARAMETER_MISMATCH = "approved_parameter_mismatch"
    KNOWN_CONTRADICTION = "known_contradiction"
    REVIEW_REQUIRED = "review_required"
    UNSUPPORTED = "unsupported"


class CatalogueArchetype(StrEnum):
    MATCH_RESULT_1X2 = "match_result_1x2"
    BOTH_TEAMS_TO_SCORE = "both_teams_to_score"
    TOTAL_GOALS_HALF_LINE = "total_goals_half_line"
    TOTAL_GOALS_INTEGER = "total_goals_integer"
    FIRST_TEAM_TO_SCORE = "first_team_to_score"
    TEAM_TOTAL_GOALS = "team_total_goals"
    HANDICAP = "handicap"
    DRAW_NO_BET = "draw_no_bet"
    DOUBLE_CHANCE = "double_chance"
    TEAM_TO_SCORE = "team_to_score"
    TEAM_CLEAN_SHEET = "team_clean_sheet"


CENSUS_V1_FAMILIES: frozenset[MarketFamily] = frozenset(
    {
        MarketFamily.MATCH_RESULT,
        MarketFamily.BOTH_TEAMS_TO_SCORE,
        MarketFamily.TOTAL_GOALS,
        MarketFamily.FIRST_TEAM_TO_SCORE,
        MarketFamily.TEAM_TOTAL,
        MarketFamily.ASIAN_HANDICAP,
        MarketFamily.DRAW_NO_BET,
        MarketFamily.DOUBLE_CHANCE,
    }
)

REQUIRED_OUTCOMES: dict[MarketFamily, frozenset[CanonicalOutcome]] = {
    MarketFamily.MATCH_RESULT: frozenset(
        {CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY}
    ),
    MarketFamily.BOTH_TEAMS_TO_SCORE: frozenset({CanonicalOutcome.YES, CanonicalOutcome.NO}),
    MarketFamily.TOTAL_GOALS: frozenset({CanonicalOutcome.OVER, CanonicalOutcome.UNDER}),
    MarketFamily.FIRST_TEAM_TO_SCORE: frozenset(
        {CanonicalOutcome.HOME, CanonicalOutcome.AWAY, CanonicalOutcome.NO_GOAL}
    ),
    MarketFamily.TEAM_TOTAL: frozenset({CanonicalOutcome.OVER, CanonicalOutcome.UNDER}),
    MarketFamily.ASIAN_HANDICAP: frozenset({CanonicalOutcome.HOME, CanonicalOutcome.AWAY}),
    MarketFamily.DRAW_NO_BET: frozenset({CanonicalOutcome.HOME, CanonicalOutcome.AWAY}),
    MarketFamily.DOUBLE_CHANCE: frozenset(
        {
            CanonicalOutcome.HOME_OR_DRAW,
            CanonicalOutcome.HOME_OR_AWAY,
            CanonicalOutcome.DRAW_OR_AWAY,
        }
    ),
    MarketFamily.GAME_WINNER: frozenset({CanonicalOutcome.HOME, CanonicalOutcome.AWAY}),
    MarketFamily.POINT_SPREAD: frozenset({CanonicalOutcome.HOME, CanonicalOutcome.AWAY}),
    MarketFamily.TOTAL_POINTS: frozenset({CanonicalOutcome.OVER, CanonicalOutcome.UNDER}),
    MarketFamily.TOTAL_RUNS: frozenset({CanonicalOutcome.OVER, CanonicalOutcome.UNDER}),
}

VENUE_PAIR_ORDER: tuple[str, ...] = (
    "matchbook_kalshi",
    "matchbook_polymarket",
    "kalshi_polymarket",
)


def family_to_archetype(
    family: MarketFamily,
    *,
    integer_line: bool | None = None,
) -> CatalogueArchetype | None:
    if family is MarketFamily.MATCH_RESULT:
        return CatalogueArchetype.MATCH_RESULT_1X2
    if family is MarketFamily.BOTH_TEAMS_TO_SCORE:
        return CatalogueArchetype.BOTH_TEAMS_TO_SCORE
    if family is MarketFamily.FIRST_TEAM_TO_SCORE:
        return CatalogueArchetype.FIRST_TEAM_TO_SCORE
    if family is MarketFamily.TOTAL_GOALS:
        if integer_line is True:
            return CatalogueArchetype.TOTAL_GOALS_INTEGER
        if integer_line is False:
            return CatalogueArchetype.TOTAL_GOALS_HALF_LINE
        return CatalogueArchetype.TOTAL_GOALS_HALF_LINE
    if family is MarketFamily.TEAM_TOTAL:
        return CatalogueArchetype.TEAM_TOTAL_GOALS
    if family is MarketFamily.ASIAN_HANDICAP:
        return CatalogueArchetype.HANDICAP
    if family is MarketFamily.DRAW_NO_BET:
        return CatalogueArchetype.DRAW_NO_BET
    if family is MarketFamily.DOUBLE_CHANCE:
        return CatalogueArchetype.DOUBLE_CHANCE
    return None
