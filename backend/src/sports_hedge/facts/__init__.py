"""Football facts identity surface owned by the historical stats repository.

PR #35 owns this contract. The canonical match namespace is ``match:<sha256…>``.
Historical odds and other modules must consume these helpers rather than
minting competing IDs.
"""

from sports_hedge.facts.catalog import (
    BOUNDED_UNIVERSE,
    CompetitionSeason,
    bounded_universe,
    competition_by_code,
    list_competitions,
)
from sports_hedge.facts.identity import (
    CanonicalMatchRef,
    NaiveKickoffError,
    canonical_match_id,
    canonical_team_id,
    season_for_kickoff,
)

__all__ = [
    "BOUNDED_UNIVERSE",
    "CanonicalMatchRef",
    "CompetitionSeason",
    "NaiveKickoffError",
    "bounded_universe",
    "canonical_match_id",
    "canonical_team_id",
    "competition_by_code",
    "list_competitions",
    "season_for_kickoff",
]
