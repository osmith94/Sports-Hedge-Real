from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field


_NON_ALNUM = re.compile(r"[^a-z0-9]+")


def normalize_text(value: str) -> str:
    """Normalize labels without making semantic guesses.

    This deliberately avoids silently dropping football terms such as FC, United,
    City or Athletic. Venue-specific aliases belong in the explicit registry.
    """

    decomposed = unicodedata.normalize("NFKD", value)
    ascii_text = "".join(char for char in decomposed if not unicodedata.combining(char))
    lowered = ascii_text.casefold()
    normalized = _NON_ALNUM.sub(" ", lowered).strip()
    return " ".join(normalized.split())


@dataclass(slots=True)
class AliasRegistry:
    """Explicit aliases for teams/competitions discovered across venues."""

    aliases: dict[str, str] = field(default_factory=dict)

    def add(self, alias: str, canonical: str) -> None:
        self.aliases[normalize_text(alias)] = normalize_text(canonical)

    def resolve(self, value: str) -> str:
        normalized = normalize_text(value)
        return self.aliases.get(normalized, normalized)
