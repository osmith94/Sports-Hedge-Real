"""NCAAB sport-specific canonical identity, markets, and PAPER gate.

Feeds the existing UNIVERSE → BACKGROUND → HOT → PAPER scanner. Soccer
aliases, NFL recognisers, and 1X2 register rows are not reused. No NCAAB
venue-pair/family cell is PAPER-admitted until NCAAB-specific settlement
evidence exists.

The 362-program team registry lives in ``ncaab.teams`` and must stay lazy:
soccer UNIVERSE clustering imports this package, and building that registry
on every identity pass starves the event loop.
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

_LAZY_ATTRS = {
    "NCAAB_ABBREVIATIONS": ("sports_hedge.ncaab.teams", "NCAAB_ABBREVIATIONS"),
    "NcaabTeamResolution": ("sports_hedge.ncaab.teams", "NcaabTeamResolution"),
    "is_canonical_ncaab_team": ("sports_hedge.ncaab.teams", "is_canonical_ncaab_team"),
    "ncaab_fixture_label": ("sports_hedge.ncaab.labels", "ncaab_fixture_label"),
    "ncaab_operator_market_label": ("sports_hedge.ncaab.labels", "ncaab_operator_market_label"),
    "ncaab_operator_side_label": ("sports_hedge.ncaab.labels", "ncaab_operator_side_label"),
    "ncaab_program_count": ("sports_hedge.ncaab.teams", "ncaab_program_count"),
    "ncaab_short_name": ("sports_hedge.ncaab.teams", "ncaab_short_name"),
    "resolve_ncaab_team": ("sports_hedge.ncaab.teams", "resolve_ncaab_team"),
}


def __getattr__(name: str):
    target = _LAZY_ATTRS.get(name)
    if target is None:
        raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
    module_name, attr = target
    from importlib import import_module

    value = getattr(import_module(module_name), attr)
    globals()[name] = value
    return value
