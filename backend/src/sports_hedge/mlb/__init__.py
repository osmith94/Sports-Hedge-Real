"""MLB discovery, fixture identity, and owner-approved PAPER structural keys."""

from sports_hedge.mlb.constants import MLB_COMPETITION, MLB_SPORT
from sports_hedge.mlb.detect import is_mlb_canonical_event, is_mlb_payload
from sports_hedge.mlb.teams import resolve_mlb_team

__all__ = [
    "MLB_COMPETITION",
    "MLB_SPORT",
    "is_mlb_canonical_event",
    "is_mlb_payload",
    "resolve_mlb_team",
]
