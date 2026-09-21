from __future__ import annotations

from sports_hedge.facts.team_registry import (
    CLUBS_BY_COMPETITION,
    alias_pairs,
    canonical_team_names,
    generic_aliases_for,
)
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


def _generic_registries() -> dict[str, AliasRegistry]:
    by_code: dict[str, AliasRegistry] = {}
    for competition in CLUBS_BY_COMPETITION:
        registry = AliasRegistry()
        for alias, canonical in generic_aliases_for(competition):
            registry.add(alias, canonical)
        if registry.aliases:
            by_code[competition] = registry
    return by_code


football_alias_registry = _registry()
_GENERIC_ALIAS_REGISTRIES = _generic_registries()
_CANONICAL_TEAM_NAMES = canonical_team_names() | frozenset(football_alias_registry.aliases.values())
_GENERIC_ALIAS_TOKENS = frozenset(
    token
    for registry in _GENERIC_ALIAS_REGISTRIES.values()
    for token in registry.aliases
)


def _canonical_remainder_after_safe_affixes(normalized: str) -> str | None:
    """Rewrite only when the remainder is already a curated canonical club.

    This is not a global FC/CF strip. Unknown remainder stays unchanged so
    senior vs youth/women/reserves and same-city clubs remain fail-closed.
    Generic city tokens such as Miami/Leon are not canonical remainders.
    """

    tokens = normalized.split()
    if len(tokens) < 2:
        return None
    remainders: list[str] = []
    if tokens[-1] in SAFE_TEAM_AFFIX_TOKENS:
        remainders.append(" ".join(tokens[:-1]))
    if tokens[0] in SAFE_TEAM_AFFIX_TOKENS:
        remainders.append(" ".join(tokens[1:]))
    if tokens[0].isdigit() and len(tokens) >= 3 and tokens[1] in SAFE_TEAM_AFFIX_TOKENS:
        remainders.append(" ".join(tokens[2:]))
    for remainder in remainders:
        if remainder in _CANONICAL_TEAM_NAMES and remainder not in _GENERIC_ALIAS_TOKENS:
            return remainder
    return None


def curated_team_names_conflict(left: str, right: str) -> bool:
    """True when both names are curated canonicals and they are different clubs."""

    if left == right:
        return False
    return left in _CANONICAL_TEAM_NAMES and right in _CANONICAL_TEAM_NAMES


def resolve_team_name(value: str) -> str:
    """Unique aliases only. Generic city tokens stay unchanged without competition."""

    resolved = football_alias_registry.resolve(value)
    if resolved in _CANONICAL_TEAM_NAMES:
        return resolved
    stripped = _canonical_remainder_after_safe_affixes(resolved)
    return stripped if stripped is not None else resolved


def resolve_team_name_for_competition(value: str, competition: str | None) -> str:
    """Apply unique aliases plus competition-scoped generic aliases.

    ``competition`` must be a registry key such as ``mls`` or ``liga_mx``.
    Unknown or missing competitions fail closed to unique-alias resolution.
    """

    code = str(competition or "").strip()
    generic = _GENERIC_ALIAS_REGISTRIES.get(code)
    if generic is not None:
        mapped = generic.resolve(value)
        if mapped != normalize_text(value) or mapped in generic.aliases:
            canonical = football_alias_registry.resolve(mapped)
            if canonical in _CANONICAL_TEAM_NAMES:
                return canonical
            if mapped in _CANONICAL_TEAM_NAMES:
                return mapped
    return resolve_team_name(value)


def is_curated_canonical_team(name: str) -> bool:
    return normalize_text(name) in _CANONICAL_TEAM_NAMES
