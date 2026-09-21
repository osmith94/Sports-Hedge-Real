"""Canonical market scope. Fixture identity stays the default.

COMPETITION_SEASON is a sibling of FIXTURE_MATCH, not a widening of
``CanonicalEvent``. Kickoff, home, and away never belong on season scope.
"""

from __future__ import annotations

from enum import StrEnum


class MarketScope(StrEnum):
    """Which identity contract a catalogue/subject row uses."""

    FIXTURE_MATCH = "FIXTURE_MATCH"
    COMPETITION_SEASON = "COMPETITION_SEASON"
