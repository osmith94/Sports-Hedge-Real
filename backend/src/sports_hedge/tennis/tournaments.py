"""Evidence-backed ATP/WTA tournament names. Unknown labels fail closed.

Observed together on 2026-09-24 from Kalshi KXATPMATCH/KXWTAMATCH
product_metadata.competition, Matchbook COMPETITION tags, and Polymarket
eventMetadata.league. Challenger, ITF, 125K, doubles and team events are
rejected even when a venue files them beside tour matches.
"""

from __future__ import annotations

import re

from sports_hedge.normalization.text import normalize_text
from sports_hedge.tennis.constants import TENNIS_TOUR_ATP, TENNIS_TOUR_WTA

_YEAR_PREFIX = re.compile(r"^(19|20)\d{2}\s+")

# canonical tournament id -> (tour, aliases including the canonical id)
_ADMITTED: dict[str, tuple[str, tuple[str, ...]]] = {
    "atp hangzhou": (
        TENNIS_TOUR_ATP,
        ("atp hangzhou", "hangzhou open"),
    ),
    "atp chengdu": (
        TENNIS_TOUR_ATP,
        ("atp chengdu", "chengdu open"),
    ),
    "wta singapore": (
        TENNIS_TOUR_WTA,
        ("wta singapore", "singapore open"),
    ),
    "wta seoul": (
        TENNIS_TOUR_WTA,
        ("wta seoul",),
    ),
}

_REJECTED_MARKERS = (
    "challenger",
    "125k",
    "125 k",
    "itf",
    "doubles",
    "mixed",
    "davis cup",
    "united cup",
    "laver cup",
    "billie jean",
    "exhibition",
    "table tennis",
)

_ALIAS_INDEX: dict[str, tuple[str, str]] = {}
for _canonical, (_tour, _aliases) in _ADMITTED.items():
    for _alias in _aliases:
        _ALIAS_INDEX[_alias] = (_tour, _canonical)


def admitted_tournament(label: str | None) -> tuple[str, str] | None:
    """Return (tour, canonical tournament) or None. Does not guess."""

    text = normalize_text(str(label or ""))
    if not text:
        return None
    text = _YEAR_PREFIX.sub("", text).strip()
    if any(marker in text for marker in _REJECTED_MARKERS):
        return None
    return _ALIAS_INDEX.get(text)


def tournament_alias_labels() -> tuple[str, ...]:
    return tuple(sorted(_ALIAS_INDEX))
