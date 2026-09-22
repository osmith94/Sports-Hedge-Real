"""NBA half-line helpers and period/family rejection gates."""

from __future__ import annotations

import re
from decimal import Decimal

from sports_hedge.domain.football import FootballPeriod, line_push_possible
from sports_hedge.normalization.text import normalize_text

_PERIOD_REJECT_TOKENS = (
    "first half",
    "1st half",
    "second half",
    "2nd half",
    "1st quarter",
    "2nd quarter",
    "3rd quarter",
    "4th quarter",
    "first quarter",
    "second quarter",
    "third quarter",
    "fourth quarter",
    "q1",
    "q2",
    "q3",
    "q4",
    "overtime only",
    "ot only",
)
_WINS_BY_OVER = re.compile(
    r"wins by over\s+(\d+(?:\.\d+)?)",
    re.IGNORECASE,
)
_OVER_POINTS = re.compile(
    r"over\s+(\d+(?:\.\d+)?)\s+points",
    re.IGNORECASE,
)
_SIGNED_LINE = re.compile(r"([+-]?\d+(?:\.\d+)?)")


def is_exact_half_line(line: Decimal | None) -> bool:
    if line is None:
        return False
    return line_push_possible(line) is False


def parse_wins_by_over_line(text: str) -> Decimal | None:
    """Kalshi '{team} wins by over N.5' → covering line -N.5."""

    match = _WINS_BY_OVER.search(text or "")
    if match is None:
        return None
    threshold = Decimal(match.group(1))
    if not is_exact_half_line(threshold):
        return None
    return -threshold


def parse_over_points_line(text: str) -> Decimal | None:
    match = _OVER_POINTS.search(text or "")
    if match is None:
        return None
    line = Decimal(match.group(1))
    if not is_exact_half_line(line):
        return None
    return line


def extract_signed_line(text: str) -> Decimal | None:
    match = _SIGNED_LINE.search(text or "")
    if match is None:
        return None
    try:
        line = Decimal(match.group(1))
    except Exception:
        return None
    return line


def nba_period_from_text(value: str) -> FootballPeriod | None:
    """Full-game only. Period / OT-only text is rejected with None."""

    text = normalize_text(value)
    compact = f" {text} "
    for token in _PERIOD_REJECT_TOKENS:
        if token in text:
            return None
        if f" {token} " in compact:
            return None
    if "quarter" in text and "full" not in text:
        return None
    if text.startswith("ot ") or text.endswith(" ot") or " overtime" in f" {text} ":
        if "full game" not in text and "regulation" not in text:
            return None
    return FootballPeriod.FULL_TIME
