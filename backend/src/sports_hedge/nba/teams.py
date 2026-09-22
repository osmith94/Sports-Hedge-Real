"""Strict current 30-team NBA registry with evidence-backed aliases.

Generic same-city labels fail closed. Historical franchise names are not
current aliases.
"""

from __future__ import annotations

from dataclasses import dataclass

from sports_hedge.normalization.text import AliasRegistry, normalize_text

NBA_ABBREVIATIONS: tuple[str, ...] = (
    "ATL",
    "BOS",
    "BKN",
    "CHA",
    "CHI",
    "CLE",
    "DAL",
    "DEN",
    "DET",
    "GSW",
    "HOU",
    "IND",
    "LAC",
    "LAL",
    "MEM",
    "MIA",
    "MIL",
    "MIN",
    "NOP",
    "NYK",
    "OKC",
    "ORL",
    "PHI",
    "PHX",
    "POR",
    "SAC",
    "SAS",
    "TOR",
    "UTA",
    "WAS",
)


@dataclass(frozen=True, slots=True)
class NbaFranchise:
    abbreviation: str
    canonical: str
    aliases: tuple[str, ...]


# Full names, nicknames, and #453 provider short forms only. City names that
# uniquely identify a current franchise are included; New York / Los Angeles
# are not.
_FRANCHISES: tuple[NbaFranchise, ...] = (
    NbaFranchise("ATL", "Atlanta Hawks", ("Atlanta", "Hawks", "ATL")),
    NbaFranchise("BOS", "Boston Celtics", ("Boston", "Celtics", "BOS")),
    NbaFranchise(
        "BKN",
        "Brooklyn Nets",
        ("Brooklyn", "Nets", "BKN", "BRK", "BKN Nets"),
    ),
    NbaFranchise("CHA", "Charlotte Hornets", ("Charlotte", "Hornets", "CHA", "CHO")),
    NbaFranchise("CHI", "Chicago Bulls", ("Chicago", "Bulls", "CHI")),
    NbaFranchise(
        "CLE",
        "Cleveland Cavaliers",
        ("Cleveland", "Cavaliers", "Cavs", "CLE"),
    ),
    NbaFranchise(
        "DAL",
        "Dallas Mavericks",
        ("Dallas", "Mavericks", "Mavs", "DAL"),
    ),
    NbaFranchise("DEN", "Denver Nuggets", ("Denver", "Nuggets", "DEN")),
    NbaFranchise("DET", "Detroit Pistons", ("Detroit", "Pistons", "DET")),
    NbaFranchise(
        "GSW",
        "Golden State Warriors",
        ("Golden State", "Warriors", "GSW", "GS"),
    ),
    NbaFranchise("HOU", "Houston Rockets", ("Houston", "Rockets", "HOU")),
    NbaFranchise("IND", "Indiana Pacers", ("Indiana", "Pacers", "IND")),
    NbaFranchise(
        "LAC",
        "Los Angeles Clippers",
        ("Clippers", "LAC", "LA Clippers", "Los Angeles C", "Los Angeles Clippers"),
    ),
    NbaFranchise(
        "LAL",
        "Los Angeles Lakers",
        ("Lakers", "LAL", "LA Lakers", "Los Angeles L", "Los Angeles Lakers"),
    ),
    NbaFranchise("MEM", "Memphis Grizzlies", ("Memphis", "Grizzlies", "MEM")),
    NbaFranchise("MIA", "Miami Heat", ("Miami", "Heat", "MIA")),
    NbaFranchise("MIL", "Milwaukee Bucks", ("Milwaukee", "Bucks", "MIL")),
    NbaFranchise(
        "MIN",
        "Minnesota Timberwolves",
        ("Minnesota", "Timberwolves", "Wolves", "MIN"),
    ),
    NbaFranchise(
        "NOP",
        "New Orleans Pelicans",
        ("New Orleans", "Pelicans", "NOP", "NO"),
    ),
    NbaFranchise(
        "NYK",
        "New York Knicks",
        ("Knicks", "NYK", "NY Knicks", "New York Knicks"),
    ),
    NbaFranchise(
        "OKC",
        "Oklahoma City Thunder",
        ("Oklahoma City", "Thunder", "OKC"),
    ),
    NbaFranchise("ORL", "Orlando Magic", ("Orlando", "Magic", "ORL")),
    NbaFranchise(
        "PHI",
        "Philadelphia 76ers",
        ("Philadelphia", "76ers", "Sixers", "PHI"),
    ),
    NbaFranchise("PHX", "Phoenix Suns", ("Phoenix", "Suns", "PHX", "PHO")),
    NbaFranchise(
        "POR",
        "Portland Trail Blazers",
        ("Portland", "Trail Blazers", "Blazers", "POR"),
    ),
    NbaFranchise("SAC", "Sacramento Kings", ("Sacramento", "Kings", "SAC")),
    NbaFranchise(
        "SAS",
        "San Antonio Spurs",
        ("San Antonio", "Spurs", "SAS", "SA"),
    ),
    NbaFranchise("TOR", "Toronto Raptors", ("Toronto", "Raptors", "TOR")),
    NbaFranchise("UTA", "Utah Jazz", ("Utah", "Jazz", "UTA", "UTAH")),
    NbaFranchise(
        "WAS",
        "Washington Wizards",
        ("Washington", "Wizards", "WAS", "WSH"),
    ),
)

# Generic same-city labels must never choose Lakers vs Clippers or Knicks vs Nets.
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
        "seattle supersonics",
        "seattle sonics",
        "new jersey nets",
        "vancouver grizzlies",
        "charlotte bobcats",
        "new orleans hornets",
        "new orleans jazz",
        "washington bullets",
        "kansas city kings",
        "san diego clippers",
        "minneapolis lakers",
        "philadelphia warriors",
        "san francisco warriors",
        "buffalo braves",
        "rochester royals",
        "cincinnati royals",
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


nba_alias_registry, _ABBREV_BY_CANONICAL, _CANONICAL_NAMES = _build_registry()


@dataclass(frozen=True, slots=True)
class NbaTeamResolution:
    canonical: str | None
    abbreviation: str | None
    ambiguous: bool
    rejected: bool
    reason: str | None = None

    @property
    def ok(self) -> bool:
        return bool(self.canonical) and not self.ambiguous and not self.rejected


def resolve_nba_team(value: str | None) -> NbaTeamResolution:
    """Resolve a provider label onto a current NBA franchise, or fail closed."""

    normalized = normalize_text(str(value or ""))
    if not normalized:
        return NbaTeamResolution(None, None, False, True, "empty_nba_team_label")
    if normalized in _AMBIGUOUS_GENERIC:
        return NbaTeamResolution(
            None,
            None,
            True,
            False,
            "ambiguous_nba_city_name",
        )
    if normalized in _HISTORICAL_REJECT:
        return NbaTeamResolution(
            None,
            None,
            False,
            True,
            "historical_nba_franchise_name",
        )
    resolved = nba_alias_registry.resolve(value or "")
    if resolved in _CANONICAL_NAMES:
        return NbaTeamResolution(
            resolved,
            _ABBREV_BY_CANONICAL[resolved],
            False,
            False,
        )
    return NbaTeamResolution(None, None, False, False, "unknown_nba_team")


def is_canonical_nba_team(name: str | None) -> bool:
    return normalize_text(str(name or "")) in _CANONICAL_NAMES


def nba_teams_conflict(left: str, right: str) -> bool:
    if left == right:
        return False
    return is_canonical_nba_team(left) and is_canonical_nba_team(right)


def require_resolved_nba_team(value: str | None) -> str:
    resolved = resolve_nba_team(value)
    if not resolved.ok or resolved.canonical is None:
        reason = resolved.reason or "unresolved_nba_team"
        raise ValueError(reason)
    return resolved.canonical


def franchise_short_name(team: str) -> str:
    """Operator nickname from the 30-team registry, original franchise casing."""

    resolved = resolve_nba_team(team)
    if resolved.ok and resolved.abbreviation:
        for franchise in _FRANCHISES:
            if franchise.abbreviation == resolved.abbreviation:
                parts = franchise.canonical.split()
                if franchise.abbreviation == "GSW":
                    return "Warriors"
                if franchise.abbreviation == "NOP":
                    return "Pelicans"
                if franchise.abbreviation == "NYK":
                    return "Knicks"
                if franchise.abbreviation == "OKC":
                    return "Thunder"
                if franchise.abbreviation == "SAS":
                    return "Spurs"
                if franchise.abbreviation == "POR":
                    return "Trail Blazers"
                if franchise.abbreviation in {"LAC", "LAL"}:
                    return parts[-1]
                return parts[-1]
    if is_canonical_nba_team(team):
        return str(team).split()[-1].title()
    parts = str(team or "").strip().split()
    return parts[-1] if parts else str(team or "")
