"""Curated tennis player identity. Surname-only labels fail closed.

Aliases are limited to the same fixture observed on more than one venue.
They are not surname heuristics.
"""

from __future__ import annotations

from dataclasses import dataclass

from sports_hedge.normalization.text import normalize_text
from sports_hedge.tennis.constants import (
    TENNIS_PLAYER_IDENTITY_AMBIGUOUS,
    TENNIS_PLAYER_IDENTITY_UNRESOLVED,
)

# 2026-09-24 ATP Hangzhou, Jie Cui vs Adolfo Vallejo.
# Polymarket moneyline outcome "Adolfo Vallejo"; Matchbook runner
# "Adolfo Daniel Vallejo". Same opponent, tournament and start window.
_CURATED_PLAYER_ALIASES: dict[str, str] = {
    "adolfo daniel vallejo": "adolfo vallejo",
}

_DOUBLES_MARKERS = ("/", " / ", " and ", " & ")


@dataclass(frozen=True)
class PlayerResolution:
    ok: bool
    canonical: str | None = None
    ambiguous: bool = False
    rejected: bool = False
    reason: str | None = None


def resolve_tennis_player(value: str | None) -> PlayerResolution:
    raw = str(value or "").strip()
    if not raw:
        return PlayerResolution(
            ok=False,
            rejected=True,
            reason=TENNIS_PLAYER_IDENTITY_UNRESOLVED,
        )
    lowered = raw.casefold()
    if any(marker in lowered for marker in _DOUBLES_MARKERS):
        return PlayerResolution(
            ok=False,
            rejected=True,
            reason="tennis_doubles_participant",
        )
    text = normalize_text(raw)
    if not text:
        return PlayerResolution(
            ok=False,
            rejected=True,
            reason=TENNIS_PLAYER_IDENTITY_UNRESOLVED,
        )
    canonical = _CURATED_PLAYER_ALIASES.get(text, text)
    tokens = canonical.split()
    if len(tokens) < 2:
        return PlayerResolution(
            ok=False,
            ambiguous=True,
            rejected=True,
            reason=TENNIS_PLAYER_IDENTITY_AMBIGUOUS,
        )
    return PlayerResolution(ok=True, canonical=canonical)


def require_tennis_player(value: str | None) -> str:
    resolved = resolve_tennis_player(value)
    if not resolved.ok or not resolved.canonical:
        raise ValueError(resolved.reason or TENNIS_PLAYER_IDENTITY_UNRESOLVED)
    return resolved.canonical


def orient_players(left: str, right: str) -> tuple[str, str]:
    """Canonical display order. Participant order is not fixture identity."""

    if left == right:
        raise ValueError("tennis_players_not_distinct")
    ordered = tuple(sorted((left, right)))
    return ordered[0], ordered[1]


def same_player_pair(left_home: str, left_away: str, right_home: str, right_away: str) -> bool:
    return {left_home, left_away} == {right_home, right_away} and len({left_home, left_away}) == 2
