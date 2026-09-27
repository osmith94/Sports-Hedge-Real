"""Closed 30-club MLB registry from 2026-09-24 provider labels.

Generic same-city labels fail closed. Historical franchise names are not
current aliases. This registry is not consulted for soccer or NFL.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from sports_hedge.normalization.text import AliasRegistry, normalize_text

MLB_ABBREVIATIONS: tuple[str, ...] = (
    "ARI",
    "ATL",
    "ATH",
    "BAL",
    "BOS",
    "CHC",
    "CWS",
    "CIN",
    "CLE",
    "COL",
    "DET",
    "HOU",
    "KC",
    "LAA",
    "LAD",
    "MIA",
    "MIL",
    "MIN",
    "NYM",
    "NYY",
    "PHI",
    "PIT",
    "SD",
    "SF",
    "SEA",
    "STL",
    "TB",
    "TEX",
    "TOR",
    "WSH",
)


@dataclass(frozen=True, slots=True)
class MlbClub:
    abbreviation: str
    canonical: str
    aliases: tuple[str, ...]


# Full names plus labels observed on Kalshi titles/subtitles, Polymarket
# teams[].name/abbreviation, and Matchbook "X at Y" names on 2026-09-24.
_CLUBS: tuple[MlbClub, ...] = (
    MlbClub("ARI", "Arizona Diamondbacks", ("Arizona", "Diamondbacks", "D-backs", "AZ", "ARI")),
    MlbClub("ATL", "Atlanta Braves", ("Atlanta", "Braves", "ATL")),
    MlbClub("ATH", "Athletics", ("Athletics", "A's", "ATH")),
    MlbClub("BAL", "Baltimore Orioles", ("Baltimore", "Orioles", "BAL")),
    MlbClub("BOS", "Boston Red Sox", ("Boston", "Red Sox", "BOS")),
    MlbClub("CHC", "Chicago Cubs", ("Chicago C", "Cubs", "CHC")),
    MlbClub("CWS", "Chicago White Sox", ("Chicago WS", "White Sox", "CWS", "CHW")),
    MlbClub("CIN", "Cincinnati Reds", ("Cincinnati", "Reds", "CIN")),
    MlbClub("CLE", "Cleveland Guardians", ("Cleveland", "Guardians", "CLE")),
    MlbClub("COL", "Colorado Rockies", ("Colorado", "Rockies", "COL")),
    MlbClub("DET", "Detroit Tigers", ("Detroit", "Tigers", "DET")),
    MlbClub("HOU", "Houston Astros", ("Houston", "Astros", "HOU")),
    MlbClub("KC", "Kansas City Royals", ("Kansas City", "Royals", "KC")),
    MlbClub("LAA", "Los Angeles Angels", ("Los Angeles A", "Angels", "LAA")),
    MlbClub("LAD", "Los Angeles Dodgers", ("Los Angeles D", "Dodgers", "LAD")),
    MlbClub("MIA", "Miami Marlins", ("Miami", "Marlins", "MIA")),
    MlbClub("MIL", "Milwaukee Brewers", ("Milwaukee", "Brewers", "MIL")),
    MlbClub("MIN", "Minnesota Twins", ("Minnesota", "Twins", "MIN")),
    MlbClub("NYM", "New York Mets", ("New York M", "Mets", "NYM")),
    MlbClub("NYY", "New York Yankees", ("New York Y", "Yankees", "NYY")),
    MlbClub("PHI", "Philadelphia Phillies", ("Philadelphia", "Phillies", "PHI")),
    MlbClub("PIT", "Pittsburgh Pirates", ("Pittsburgh", "Pirates", "PIT")),
    MlbClub("SD", "San Diego Padres", ("San Diego", "Padres", "SD")),
    MlbClub("SF", "San Francisco Giants", ("San Francisco", "Giants", "SF")),
    MlbClub("SEA", "Seattle Mariners", ("Seattle", "Mariners", "SEA")),
    MlbClub("STL", "St. Louis Cardinals", ("St. Louis", "St Louis", "Cardinals", "STL")),
    MlbClub("TB", "Tampa Bay Rays", ("Tampa Bay", "Rays", "TB")),
    MlbClub("TEX", "Texas Rangers", ("Texas", "Rangers", "TEX")),
    MlbClub("TOR", "Toronto Blue Jays", ("Toronto", "Blue Jays", "TOR")),
    MlbClub("WSH", "Washington Nationals", ("Washington", "Nationals", "WSH")),
)

_AMBIGUOUS_GENERIC = frozenset(
    {
        "chicago",
        "new york",
        "ny",
        "los angeles",
        "la",
        "c",
        "ws",
    }
)

_HISTORICAL_REJECT = frozenset(
    {
        "cleveland indians",
        "florida marlins",
        "montreal expos",
        "california angels",
        "anaheim angels",
        "oakland athletics",
        "oakland a s",
        "tampa bay devil rays",
    }
)


def _is_short_code(value: str) -> bool:
    token = value.strip()
    return bool(token) and " " not in token and token.isalpha() and token.upper() == token and len(token) <= 4


def _provider_nicknames(canonical: str, aliases: tuple[str, ...]) -> tuple[str, ...]:
    """Nicknames used beside a provider abbreviation, such as ``Red Sox``."""

    words = canonical.split()
    found: list[str] = []
    for alias in aliases:
        alias_words = alias.split()
        if not alias_words or alias == canonical:
            continue
        if words[-len(alias_words) :] != alias_words:
            continue
        if _is_short_code(alias):
            continue
        found.append(alias)
    for alias in aliases:
        if " " in alias or alias == canonical or _is_short_code(alias):
            continue
        if alias not in found:
            found.append(alias)
    if not found:
        found.append(words[-1])
    unique: list[str] = []
    for item in found:
        if item not in unique:
            unique.append(item)
    return tuple(unique)


def _provider_codes(abbreviation: str, aliases: tuple[str, ...]) -> tuple[str, ...]:
    codes = [abbreviation]
    for alias in aliases:
        if _is_short_code(alias) and alias not in codes:
            codes.append(alias)
    return tuple(codes)


def _build_registry() -> tuple[AliasRegistry, dict[str, str], frozenset[str]]:
    registry = AliasRegistry()
    abbrev_by_canonical: dict[str, str] = {}
    canonicals: set[str] = set()
    for club in _CLUBS:
        canonical_norm = normalize_text(club.canonical)
        canonicals.add(canonical_norm)
        abbrev_by_canonical[canonical_norm] = club.abbreviation
        registry.add(club.canonical, club.canonical)
        registry.add(club.abbreviation, club.canonical)
        for alias in club.aliases:
            registry.add(alias, club.canonical)
        for code in _provider_codes(club.abbreviation, club.aliases):
            for nickname in _provider_nicknames(club.canonical, club.aliases):
                registry.add(f"{code} {nickname}", club.canonical)
    return registry, abbrev_by_canonical, frozenset(canonicals)


mlb_alias_registry, _ABBREV_BY_CANONICAL, _CANONICAL_NAMES = _build_registry()


@dataclass(frozen=True, slots=True)
class MlbTeamResolution:
    canonical: str | None
    abbreviation: str | None
    ambiguous: bool
    rejected: bool
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.canonical) and not self.ambiguous and not self.rejected


def resolve_mlb_team(value: str | None) -> MlbTeamResolution:
    """Resolve a provider label onto a current MLB club, or fail closed."""

    normalized = normalize_text(str(value or ""))
    if not normalized:
        return MlbTeamResolution(None, None, False, True, "empty_mlb_team_label")
    if normalized in _AMBIGUOUS_GENERIC:
        return MlbTeamResolution(None, None, True, False, "ambiguous_mlb_city_name")
    if normalized in _HISTORICAL_REJECT:
        return MlbTeamResolution(None, None, False, True, "historical_mlb_franchise_name")
    resolved = mlb_alias_registry.resolve(value or "")
    if resolved in _CANONICAL_NAMES:
        return MlbTeamResolution(
            resolved,
            _ABBREV_BY_CANONICAL[resolved],
            False,
            False,
        )
    return MlbTeamResolution(None, None, False, False, "unknown_mlb_team")


def is_canonical_mlb_team(name: str | None) -> bool:
    return normalize_text(str(name or "")) in _CANONICAL_NAMES


def mlb_teams_conflict(left: str, right: str) -> bool:
    if left == right:
        return False
    return is_canonical_mlb_team(left) and is_canonical_mlb_team(right)


def require_resolved_mlb_team(value: str | None) -> str:
    resolved = resolve_mlb_team(value)
    if not resolved.ok or resolved.canonical is None:
        reason = resolved.reason or "unresolved_mlb_team"
        raise ValueError(reason)
    return resolved.canonical


def mlb_team_codes() -> tuple[str, ...]:
    codes: list[str] = []
    for club in _CLUBS:
        for code in _provider_codes(club.abbreviation, club.aliases):
            if code not in codes:
                codes.append(code)
    return tuple(codes)


def split_concatenated_mlb_codes(blob: str) -> tuple[str, str] | None:
    """Split ``STLPIT`` into ``(STL, PIT)`` only when exactly one code pair fits."""

    codes = set(mlb_team_codes())
    found: list[tuple[str, str]] = []
    compact = str(blob or "").strip().upper()
    for code in codes:
        if compact.startswith(code):
            rest = compact[len(code) :]
            if rest in codes and rest != code:
                found.append((code, rest))
    if len(found) != 1:
        return None
    return found[0]


_MLB_EVENT_TEAMS = re.compile(
    r"^(?:KXMLBGAME|KXMLBTOTAL)-((?:\d{2}[A-Z]{3}\d{6})|(?:\d{2}[A-Z]{3}\d{2}))([A-Z]{4,8})$"
)


def mlb_away_home_from_event_ticker(ticker: str | None) -> tuple[str, str] | None:
    """Away then home from an approved MLB event ticker, or None.

    Captured tickers are ``KXMLBGAME-26SEP241235STLPIT``: away St. Louis, home Pittsburgh.
    """

    match = _MLB_EVENT_TEAMS.match(str(ticker or "").strip().upper())
    if match is None:
        return None
    pair = split_concatenated_mlb_codes(match.group(2))
    if pair is None:
        return None
    away = resolve_mlb_team(pair[0])
    home = resolve_mlb_team(pair[1])
    if not away.ok or not home.ok or not away.canonical or not home.canonical:
        return None
    if away.canonical == home.canonical:
        return None
    return away.canonical, home.canonical


def franchise_short_name(team: str) -> str:
    resolved = resolve_mlb_team(team)
    if resolved.ok and resolved.abbreviation:
        for club in _CLUBS:
            if club.abbreviation == resolved.abbreviation:
                return club.canonical.split()[-1]
    parts = str(team or "").strip().split()
    return parts[-1] if parts else str(team or "")
