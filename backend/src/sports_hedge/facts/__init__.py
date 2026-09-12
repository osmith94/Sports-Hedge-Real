"""Football facts identity surface shared with historical odds.

Agent Q's facts repository and Agent R's odds repository both map onto the
canonical match, team and competition identities defined here. Odds never
invent a competing identity scheme.
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
    canonical_match_id,
    canonical_team_id,
    season_for_kickoff,
)

__all__ = [
    "BOUNDED_UNIVERSE",
    "CanonicalMatchRef",
    "CompetitionSeason",
    "bounded_universe",
    "canonical_match_id",
    "canonical_team_id",
    "competition_by_code",
    "list_competitions",
    "season_for_kickoff",
]
