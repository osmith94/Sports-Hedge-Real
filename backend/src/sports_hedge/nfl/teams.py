"""Strict current 32-team NFL registry with evidence-backed aliases.

Generic same-city labels fail closed. Historical franchise names are not
current aliases.
"""

from __future__ import annotations

from dataclasses import dataclass

from sports_hedge.normalization.text import AliasRegistry, normalize_text

NFL_ABBREVIATIONS: tuple[str, ...] = (
    "ARI",
    "ATL",
    "BAL",
    "BUF",
    "CAR",
    "CHI",
    "CIN",
    "CLE",
    "DAL",
    "DEN",
    "DET",
    "GB",
    "HOU",
    "IND",
    "JAX",
    "KC",
    "LV",
    "LAC",
    "LAR",
    "MIA",
    "MIN",
    "NE",
    "NO",
    "NYG",
    "NYJ",
    "PHI",
    "PIT",
    "SEA",
    "SF",
    "TB",
    "TEN",
    "WAS",
)


@dataclass(frozen=True, slots=True)
class NflFranchise:
    abbreviation: str
    canonical: str
    aliases: tuple[str, ...]


# Full names, nicknames, and Phase-1 provider short forms only. City names that
# uniquely identify a current franchise are included; New York / Los Angeles
# are not.
_FRANCHISES: tuple[NflFranchise, ...] = (
    NflFranchise("ARI", "Arizona Cardinals", ("Arizona", "Cardinals", "ARI")),
    NflFranchise("ATL", "Atlanta Falcons", ("Atlanta", "Falcons", "ATL")),
    NflFranchise("BAL", "Baltimore Ravens", ("Baltimore", "Ravens", "BAL")),
    NflFranchise("BUF", "Buffalo Bills", ("Buffalo", "Bills", "BUF")),
    NflFranchise("CAR", "Carolina Panthers", ("Carolina", "Panthers", "CAR")),
    NflFranchise("CHI", "Chicago Bears", ("Chicago", "Bears", "CHI")),
    NflFranchise("CIN", "Cincinnati Bengals", ("Cincinnati", "Bengals", "CIN")),
    NflFranchise("CLE", "Cleveland Browns", ("Cleveland", "Browns", "CLE")),
    NflFranchise("DAL", "Dallas Cowboys", ("Dallas", "Cowboys", "DAL")),
    NflFranchise("DEN", "Denver Broncos", ("Denver", "Broncos", "DEN")),
    NflFranchise("DET", "Detroit Lions", ("Detroit", "Lions", "DET")),
    NflFranchise("GB", "Green Bay Packers", ("Green Bay", "Packers", "GB")),
    NflFranchise("HOU", "Houston Texans", ("Houston", "Texans", "HOU")),
    NflFranchise("IND", "Indianapolis Colts", ("Indianapolis", "Colts", "IND")),
    NflFranchise("JAX", "Jacksonville Jaguars", ("Jacksonville", "Jaguars", "JAX", "JAC")),
    NflFranchise("KC", "Kansas City Chiefs", ("Kansas City", "Chiefs", "KC")),
    NflFranchise("LV", "Las Vegas Raiders", ("Las Vegas", "Raiders", "LV")),
    NflFranchise(
        "LAC",
        "Los Angeles Chargers",
        ("Chargers", "LAC", "LA Chargers", "Los Angeles C"),
    ),
    NflFranchise(
        "LAR",
        "Los Angeles Rams",
        ("Rams", "LAR", "LA Rams", "Los Angeles R"),
    ),
    NflFranchise("MIA", "Miami Dolphins", ("Miami", "Dolphins", "MIA")),
    NflFranchise("MIN", "Minnesota Vikings", ("Minnesota", "Vikings", "MIN")),
    NflFranchise("NE", "New England Patriots", ("New England", "Patriots", "NE")),
    NflFranchise("NO", "New Orleans Saints", ("New Orleans", "Saints", "NO")),
    NflFranchise(
        "NYG",
        "New York Giants",
        ("Giants", "NYG", "NY Giants", "New York G"),
    ),
    NflFranchise(
        "NYJ",
        "New York Jets",
        ("Jets", "NYJ", "NY Jets", "New York J"),
    ),
    NflFranchise("PHI", "Philadelphia Eagles", ("Philadelphia", "Eagles", "PHI")),
    NflFranchise("PIT", "Pittsburgh Steelers", ("Pittsburgh", "Steelers", "PIT")),
    NflFranchise("SEA", "Seattle Seahawks", ("Seattle", "Seahawks", "SEA")),
    NflFranchise(
        "SF",
        "San Francisco 49ers",
        ("San Francisco", "49ers", "SF", "Forty Niners", "Niners"),
    ),
    NflFranchise(
        "TB",
        "Tampa Bay Buccaneers",
        ("Tampa Bay", "Buccaneers", "Bucs", "TB"),
    ),
    NflFranchise("TEN", "Tennessee Titans", ("Tennessee", "Titans", "TEN")),
    NflFranchise(
        "WAS",
        "Washington Commanders",
        ("Washington", "Commanders", "WAS", "WSH"),
    ),
)

# Generic same-city labels must never choose NYG vs NYJ or LAC vs LAR.
_AMBIGUOUS_GENERIC = frozenset(
    {
        "new york",
        "ny",
        "los angeles",
        "la",
    }
)

# Historical names are not current aliases. Fail closed rather than remap.
_HISTORICAL_REJECT = frozenset(
    {
        "oakland raiders",
        "los angeles raiders",
        "san diego chargers",
        "st louis rams",
        "saint louis rams",
        "houston oilers",
        "tennessee oilers",
        "washington redskins",
        "washington football team",
        "boston patriots",
        "phoenix cardinals",
        "st louis cardinals",
    }
)


def _build_registry() -> tuple[AliasRegistry, dict[str, str], frozenset[str]]:
    registry = AliasRegistry()
    abbrev_by_canonical: dict[str, str] = {}
    canonicals: set[str] = set()
    for franchise in _FRANCHISES:
        canonical_norm = normalize_text(franchise.canonical)
        canonicals.add(canonical_norm)
        abbrev_by_canonical[canonical_norm] = franchise.abbreviation
        registry.add(franchise.canonical, franchise.canonical)
        registry.add(franchise.abbreviation, franchise.canonical)
        for alias in franchise.aliases:
            registry.add(alias, franchise.canonical)
    return registry, abbrev_by_canonical, frozenset(canonicals)


nfl_alias_registry, _ABBREV_BY_CANONICAL, _CANONICAL_NAMES = _build_registry()


@dataclass(frozen=True, slots=True)
class NflTeamResolution:
    canonical: str | None
    abbreviation: str | None
    ambiguous: bool
    rejected: bool
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.canonical) and not self.ambiguous and not self.rejected


def resolve_nfl_team(value: str | None) -> NflTeamResolution:
    """Resolve a provider label onto a current NFL franchise, or fail closed."""

    normalized = normalize_text(str(value or ""))
    if not normalized:
        return NflTeamResolution(None, None, False, True, "empty_nfl_team_label")
    if normalized in _AMBIGUOUS_GENERIC:
        return NflTeamResolution(
            None,
            None,
            True,
            False,
            "ambiguous_nfl_city_name",
        )
    if normalized in _HISTORICAL_REJECT:
        return NflTeamResolution(
            None,
            None,
            False,
            True,
            "historical_nfl_franchise_name",
        )
    resolved = nfl_alias_registry.resolve(value or "")
    if resolved in _CANONICAL_NAMES:
        return NflTeamResolution(
            resolved,
            _ABBREV_BY_CANONICAL[resolved],
            False,
            False,
        )
    return NflTeamResolution(None, None, False, False, "unknown_nfl_team")


def is_canonical_nfl_team(name: str | None) -> bool:
    return normalize_text(str(name or "")) in _CANONICAL_NAMES


def nfl_teams_conflict(left: str, right: str) -> bool:
    if left == right:
        return False
    return is_canonical_nfl_team(left) and is_canonical_nfl_team(right)


def require_resolved_nfl_team(value: str | None) -> str:
    resolved = resolve_nfl_team(value)
    if not resolved.ok or resolved.canonical is None:
        reason = resolved.reason or "unresolved_nfl_team"
        raise ValueError(reason)
    return resolved.canonical


def franchise_short_name(team: str) -> str:
    """Operator nickname from the 32-team registry, original franchise casing."""

    resolved = resolve_nfl_team(team)
    if resolved.ok and resolved.abbreviation:
        for franchise in _FRANCHISES:
            if franchise.abbreviation == resolved.abbreviation:
                return franchise.canonical.split()[-1]
    if is_canonical_nfl_team(team):
        return str(team).split()[-1].title()
    parts = str(team or "").strip().split()
    return parts[-1] if parts else str(team or "")
