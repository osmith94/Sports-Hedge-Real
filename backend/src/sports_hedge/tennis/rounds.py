"""Tennis round labels. Different vocabularies are not equated.

Matchbook `R1` is not treated as Kalshi `Round Of 32`. That crosswalk is not
proven for every draw size, so unequal labels fail closed.
"""

from __future__ import annotations

import re

from sports_hedge.normalization.text import normalize_text

_ORDERED_ROUNDS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bround of 128\b"), "round_of_128"),
    (re.compile(r"\bround of 64\b"), "round_of_64"),
    (re.compile(r"\bround of 32\b"), "round_of_32"),
    (re.compile(r"\bround of 16\b"), "round_of_16"),
    (re.compile(r"\bquarter ?finals?\b"), "quarterfinal"),
    (re.compile(r"\bqf\b"), "quarterfinal"),
    (re.compile(r"\bsemi ?finals?\b"), "semifinal"),
    (re.compile(r"\bsf\b"), "semifinal"),
    (re.compile(r"\br\d+\b"), ""),
    (re.compile(r"\bfinal\b"), "final"),
)


def canonical_round_label(*parts: object) -> str:
    text = normalize_text(" ".join(str(part or "") for part in parts))
    if not text:
        return ""
    for pattern, canonical in _ORDERED_ROUNDS:
        match = pattern.search(text)
        if match is None:
            continue
        if canonical:
            return canonical
        return match.group(0).replace(" ", "")
    return ""
