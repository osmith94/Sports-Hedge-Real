from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from sports_hedge.config import get_settings
from sports_hedge.historical.repository import SqliteHistoricalRepository
from sports_hedge.odds.repository import SqliteOddsRepository

router = APIRouter(prefix="/research/historical", tags=["historical-read"])


class CompetitionSeasonCoverage(BaseModel):
    competition: str
    season: str
    matches: int = Field(ge=0)


class HistoricalCoverageReadModel(BaseModel):
    """Repository-derived coverage. Counts are never hardcoded in the API."""

    data_class: str
    facts_available: bool
    odds_available: bool
    analogue_model: str
    analogue_note: str
    movement_semantics: str
    match_count: int | None = None
    stored_observation_count: int | None = None
    same_line_opening_closing_pairs: int | None = None
    asian_handicap_line_shifts: int | None = None
    competitions: list[CompetitionSeasonCoverage] = Field(default_factory=list)
    unavailable_reason: str | None = None


@lru_cache
def get_facts_repository() -> SqliteHistoricalRepository | None:
    settings = get_settings()
    path = Path(settings.historical_db_path)
    if settings.historical_db_path == ":memory:" or not path.exists():
        return None
    return SqliteHistoricalRepository(path)


@lru_cache
def get_odds_repository() -> SqliteOddsRepository | None:
    settings = get_settings()
    path = Path(settings.historical_odds_db_path)
    if settings.historical_odds_db_path == ":memory:" or not path.exists():
        return None
    return SqliteOddsRepository(path)


def _analogue_note() -> str:
    return (
        "No historical analogue/read API is wired. Historical movement context is "
        "correlation/context only, never causation. Direct price movement requires an "
        "equivalent proposition and line; Asian handicap line changes are structural "
        "line shifts, not pure price movement. Weak/no relationship and no comparable "
        "precedent remain valid later outcomes."
    )


@router.get("/coverage", response_model=HistoricalCoverageReadModel)
def historical_coverage(
    facts: SqliteHistoricalRepository | None = Depends(get_facts_repository),
    odds: SqliteOddsRepository | None = Depends(get_odds_repository),
) -> HistoricalCoverageReadModel:
    if facts is None and odds is None:
        return HistoricalCoverageReadModel(
            data_class="UNAVAILABLE",
            facts_available=False,
            odds_available=False,
            analogue_model="UNAVAILABLE",
            analogue_note=_analogue_note(),
            movement_semantics=(
                "same-line opening→closing pairs are price movement; "
                "AH line changes are structural line shifts"
            ),
            unavailable_reason="Historical SQLite files are not present in this environment.",
        )

    competitions: list[CompetitionSeasonCoverage] = []
    match_count = None
    if facts is not None:
        match_count = facts.count_matches()
        competitions = [
            CompetitionSeasonCoverage(competition=name, season=season, matches=count)
            for name, season, count in facts.match_counts_by_competition_season()
        ]

    observation_count = None
    same_line_pairs = None
    ah_shifts = None
    if odds is not None:
        observation_count = odds.count_observations()
        same_line_pairs = odds.count_same_line_opening_closing_pairs()
        ah_shifts = odds.count_asian_handicap_line_shifts()

    available = (match_count or 0) > 0 or (observation_count or 0) > 0
    return HistoricalCoverageReadModel(
        data_class="REAL_HISTORICAL" if available else "UNAVAILABLE",
        facts_available=facts is not None,
        odds_available=odds is not None,
        analogue_model="UNAVAILABLE",
        analogue_note=_analogue_note(),
        movement_semantics=(
            "same-line opening→closing pairs are price movement; "
            "AH line changes are structural line shifts"
        ),
        match_count=match_count,
        stored_observation_count=observation_count,
        same_line_opening_closing_pairs=same_line_pairs,
        asian_handicap_line_shifts=ah_shifts,
        competitions=competitions,
        unavailable_reason=None
        if available
        else "Historical databases exist but contain no repository rows yet.",
    )
