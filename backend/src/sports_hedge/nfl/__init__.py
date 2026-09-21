"""NFL sport-specific canonical identity, markets, and PAPER equivalence.

Feeds the existing UNIVERSE → BACKGROUND → HOT → PAPER scanner. Soccer
aliases, recognisers, and 1X2 register rows are not reused.
"""

from sports_hedge.nfl.constants import (
    NFL_COMPETITION,
    NFL_EXCEPTIONAL_SETTLEMENT_CAVEAT,
    NFL_SPORT,
)
from sports_hedge.nfl.detect import is_nfl_canonical_event, is_nfl_kalshi_ticker, is_nfl_payload
from sports_hedge.nfl.labels import (
    nfl_fixture_label,
    nfl_operator_market_label,
    nfl_operator_side_label,
)
from sports_hedge.nfl.teams import (
    NFL_ABBREVIATIONS,
    NflTeamResolution,
    franchise_short_name,
    is_canonical_nfl_team,
    resolve_nfl_team,
)

__all__ = [
    "NFL_ABBREVIATIONS",
    "NFL_COMPETITION",
    "NFL_EXCEPTIONAL_SETTLEMENT_CAVEAT",
    "NFL_SPORT",
    "NflTeamResolution",
    "franchise_short_name",
    "is_canonical_nfl_team",
    "is_nfl_canonical_event",
    "is_nfl_kalshi_ticker",
    "is_nfl_payload",
    "nfl_fixture_label",
    "nfl_operator_market_label",
    "nfl_operator_side_label",
    "resolve_nfl_team",
]
