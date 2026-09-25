"""MLB scheduled-game compatibility.

``scheduled_game_key`` remains audit evidence: a minute-precision start, plus
``|game-1`` or ``|game-2`` when the provider states an ordinal. The minute
embedded in that key is not a second identity clock. The authoritative
kickoff tolerance lives on ``EventMatcher``.
"""

from __future__ import annotations

import re

from sports_hedge.mlb.constants import MLB_GAME_IDENTITY_AMBIGUOUS, MLB_GAME_IDENTITY_MISMATCH

_EXPLICIT_ORDINAL = re.compile(r"\|game-([12])\Z")


def mlb_explicit_game_ordinal(scheduled_game_key: str | None) -> int | None:
    """Return 1 or 2 when the key states that ordinal. Otherwise None."""

    match = _EXPLICIT_ORDINAL.search(str(scheduled_game_key or "").strip())
    if match is None:
        return None
    return int(match.group(1))


def mlb_scheduled_games_compatible(
    left_key: str | None,
    right_key: str | None,
) -> tuple[bool, str | None]:
    """Ordinal safety only. Kickoff tolerance is applied by the caller.

    Neither side states an ordinal: compatible. The caller still requires the
    approved kickoff window and the same clubs.

    Both state the same ordinal: compatible, subject to that same window.

    Ordinals conflict: hard reject, including Game 1 versus Game 2 a few
    seconds apart.

    One side states an ordinal and the other does not, or a key is missing:
    fail closed. Do not guess which game of a doubleheader it is.
    """

    left = str(left_key or "").strip()
    right = str(right_key or "").strip()
    if not left or not right:
        return False, MLB_GAME_IDENTITY_AMBIGUOUS
    left_ordinal = mlb_explicit_game_ordinal(left)
    right_ordinal = mlb_explicit_game_ordinal(right)
    if "|game-" in left and left_ordinal is None:
        return False, MLB_GAME_IDENTITY_AMBIGUOUS
    if "|game-" in right and right_ordinal is None:
        return False, MLB_GAME_IDENTITY_AMBIGUOUS
    if left_ordinal is None and right_ordinal is None:
        return True, None
    if left_ordinal is None or right_ordinal is None:
        return False, MLB_GAME_IDENTITY_AMBIGUOUS
    if left_ordinal != right_ordinal:
        return False, MLB_GAME_IDENTITY_MISMATCH
    return True, None
