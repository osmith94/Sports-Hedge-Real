"""NBA sport-specific canonical identity, markets, and PAPER equivalence.

Feeds the existing UNIVERSE → BACKGROUND → HOT → PAPER scanner. Soccer and
NFL aliases, recognisers, and register rows are not reused.
"""

from sports_hedge.nba.constants import (
    NBA_COMPETITION,
    NBA_EXCEPTIONAL_SETTLEMENT_CAVEAT,
    NBA_SPORT,
)
from sports_hedge.nba.detect import is_nba_canonical_event, is_nba_kalshi_ticker, is_nba_payload
from sports_hedge.nba.labels import (
    nba_fixture_label,
    nba_operator_market_label,
    nba_operator_side_label,
)
from sports_hedge.nba.teams import (
    NBA_ABBREVIATIONS,
    NbaTeamResolution,
    franchise_short_name,
    is_canonical_nba_team,
    resolve_nba_team,
)

__all__ = [
    "NBA_ABBREVIATIONS",
    "NBA_COMPETITION",
    "NBA_EXCEPTIONAL_SETTLEMENT_CAVEAT",
    "NBA_SPORT",
    "NbaTeamResolution",
    "franchise_short_name",
    "is_canonical_nba_team",
    "is_nba_canonical_event",
    "is_nba_kalshi_ticker",
    "is_nba_payload",
    "nba_fixture_label",
    "nba_operator_market_label",
    "nba_operator_side_label",
    "resolve_nba_team",
]
