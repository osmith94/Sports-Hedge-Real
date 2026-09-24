"""MLB half-run lines and rejected period/prop/pitcher gates."""

from __future__ import annotations

from decimal import Decimal
import re

from sports_hedge.domain.football import FootballPeriod, line_push_possible
from sports_hedge.normalization.text import normalize_text

_REJECT_TOKENS = (
    "first 5",
    "1st 5",
    "first five",
    "first 3",
    "first 7",
    "inning winner",
    "inning total",
    "run line",
    "player prop",
    "world series",
    "futures",
    "outright",
    "listed pitcher",
    " nrfi",
    "yrfi",
    "extra innings only",
)

_OVER_RUNS = re.compile(r"over\s+(\d+(?:\.\d+)?)\s+runs", re.IGNORECASE)
_GAME_NUMBER = re.compile(r"\bgame\s*([12])\b", re.IGNORECASE)


def is_exact_half_line(line: Decimal | None) -> bool:
    if line is None:
        return False
    return line_push_possible(line) is False


def parse_over_runs_line(text: str) -> Decimal | None:
    match = _OVER_RUNS.search(text or "")
    if match is None:
        return None
    line = Decimal(match.group(1))
    if not is_exact_half_line(line):
        return None
    return line


def parse_game_number(*values: object) -> int | None:
    for value in values:
        match = _GAME_NUMBER.search(str(value or ""))
        if match is not None:
            return int(match.group(1))
    return None


def mlb_text_is_rejected_family(value: str) -> bool:
    text = normalize_text(value)
    return any(token in text for token in _REJECT_TOKENS)


def mlb_period_from_text(value: str) -> FootballPeriod | None:
    if mlb_text_is_rejected_family(value):
        return None
    return FootballPeriod.FULL_TIME
