"""NCAAB sport-specific canonical identity, markets, and PAPER gate.

Feeds the existing UNIVERSE → BACKGROUND → HOT → PAPER scanner. Soccer
aliases, NFL recognisers, and 1X2 register rows are not reused. No NCAAB
venue-pair/family cell is PAPER-admitted until NCAAB-specific settlement
evidence exists.
"""

from sports_hedge.ncaab.constants import (
    NCAAB_COMPETITION,
    NCAAB_DIVISION_I_PROGRAM_COUNT,
    NCAAB_EXCEPTIONAL_SETTLEMENT_CAVEAT,
    NCAAB_PAIR_UNAPPROVED_REASON,
    NCAAB_SPORT,
    REJECTED_NON_NCAAB_BASKETBALL,
)
from sports_hedge.ncaab.detect import (
    is_ncaab_canonical_event,
    is_ncaab_kalshi_ticker,
    is_ncaab_payload,
)
from sports_hedge.ncaab.labels import (
    ncaab_fixture_label,
    ncaab_operator_market_label,
    ncaab_operator_side_label,
)
from sports_hedge.ncaab.teams import (
    NCAAB_ABBREVIATIONS,
    NcaabTeamResolution,
    is_canonical_ncaab_team,
    ncaab_program_count,
    ncaab_short_name,
    resolve_ncaab_team,
)

__all__ = [
    "NCAAB_ABBREVIATIONS",
    "NCAAB_COMPETITION",
    "NCAAB_DIVISION_I_PROGRAM_COUNT",
    "NCAAB_EXCEPTIONAL_SETTLEMENT_CAVEAT",
    "NCAAB_PAIR_UNAPPROVED_REASON",
    "NCAAB_SPORT",
    "NcaabTeamResolution",
    "REJECTED_NON_NCAAB_BASKETBALL",
    "is_canonical_ncaab_team",
    "is_ncaab_canonical_event",
    "is_ncaab_kalshi_ticker",
    "is_ncaab_payload",
    "ncaab_fixture_label",
    "ncaab_operator_market_label",
    "ncaab_operator_side_label",
    "ncaab_program_count",
    "ncaab_short_name",
    "resolve_ncaab_team",
]
