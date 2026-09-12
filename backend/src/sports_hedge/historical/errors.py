from __future__ import annotations


class HistoricalMappingError(ValueError):
    """Raised when a team, match, competition or season cannot be mapped safely."""


class HistoricalConflictError(ValueError):
    """Raised when ingested facts contradict an existing normalized record."""
