from __future__ import annotations

from sports_hedge.facts.team_registry import alias_pairs, canonical_team_names
from sports_hedge.normalization.text import AliasRegistry, normalize_text

# Conservative legal-form affixes only. Never drop United/City/Athletic/Turin-style
# identity terms. Italian Calcio/BC are the Serie A equivalent of FC/SC; unknown
# remainders stay unchanged so senior vs youth/women/reserves stay fail-closed.
SAFE_TEAM_AFFIX_TOKENS = frozenset({"fc", "cf", "afc", "sc", "calcio", "bc"})


def _registry() -> AliasRegistry:
    aliases = AliasRegistry()
    for alias, canonical in alias_pairs():
        aliases.add(alias, canonical)
        aliases.add(canonical, canonical)
    return aliases


football_alias_registry = _registry()
_CANONICAL_TEAM_NAMES = canonical_team_names() | frozenset(football_alias_registry.aliases.values())


def _canonical_remainder_after_safe_affixes(normalized: str) -> str | None:
    """Rewrite only when the remainder is already a curated canonical club.

    This is not a global FC/CF strip. Unknown remainder stays unchanged so
    senior vs youth/women/reserves and same-city clubs remain fail-closed.
    """

    tokens = normalized.split()
    if len(tokens) < 2:
        return None
    if tokens[-1] in SAFE_TEAM_AFFIX_TOKENS:
        remainder = " ".join(tokens[:-1])
        if remainder in _CANONICAL_TEAM_NAMES:
            return remainder
    if tokens[0] in SAFE_TEAM_AFFIX_TOKENS:
        remainder = " ".join(tokens[1:])
        if remainder in _CANONICAL_TEAM_NAMES:
            return remainder
    if tokens[0].isdigit() and len(tokens) >= 3 and tokens[1] in SAFE_TEAM_AFFIX_TOKENS:
        remainder = " ".join(tokens[2:])
        if remainder in _CANONICAL_TEAM_NAMES:
            return remainder
    return None


def curated_team_names_conflict(left: str, right: str) -> bool:
    """True when both names are curated canonicals and they are different clubs."""

    if left == right:
        return False
    return left in _CANONICAL_TEAM_NAMES and right in _CANONICAL_TEAM_NAMES


def resolve_team_name(value: str) -> str:
    resolved = football_alias_registry.resolve(value)
    if resolved in _CANONICAL_TEAM_NAMES:
        return resolved
    stripped = _canonical_remainder_after_safe_affixes(resolved)
    return stripped if stripped is not None else resolved


def is_curated_canonical_team(name: str) -> bool:
    return normalize_text(name) in _CANONICAL_TEAM_NAMES
