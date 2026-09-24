"""Closed 30-club MLB registry from 2026-09-24 provider labels.

Generic same-city labels fail closed. Historical franchise names are not
current aliases. This registry is not consulted for soccer or NFL.
"""

from __future__ import annotations

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


def franchise_short_name(team: str) -> str:
    resolved = resolve_mlb_team(team)
    if resolved.ok and resolved.abbreviation:
        for club in _CLUBS:
            if club.abbreviation == resolved.abbreviation:
                return club.canonical.split()[-1]
    parts = str(team or "").strip().split()
    return parts[-1] if parts else str(team or "")
