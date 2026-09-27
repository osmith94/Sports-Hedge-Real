"""Strict current 32-team NFL registry with evidence-backed aliases.

Generic same-city labels fail closed. Historical franchise names are not
current aliases.
"""

from __future__ import annotations

import re
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


def _is_short_code(value: str) -> bool:
    token = value.strip()
    return bool(token) and " " not in token and token.isalpha() and token.upper() == token and len(token) <= 4


def _provider_nicknames(canonical: str, aliases: tuple[str, ...]) -> tuple[str, ...]:
    """Nicknames used beside a provider abbreviation, such as ``Chiefs`` or ``49ers``."""

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
    for franchise in _FRANCHISES:
        canonical_norm = normalize_text(franchise.canonical)
        canonicals.add(canonical_norm)
        abbrev_by_canonical[canonical_norm] = franchise.abbreviation
        registry.add(franchise.canonical, franchise.canonical)
        registry.add(franchise.abbreviation, franchise.canonical)
        for alias in franchise.aliases:
            registry.add(alias, franchise.canonical)
        # Deterministic provider compounds: "KC Chiefs", "JAC Jaguars", "SF 49ers".
        for code in _provider_codes(franchise.abbreviation, franchise.aliases):
            for nickname in _provider_nicknames(franchise.canonical, franchise.aliases):
                registry.add(f"{code} {nickname}", franchise.canonical)
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


def nfl_team_codes() -> tuple[str, ...]:
    codes: list[str] = []
    for franchise in _FRANCHISES:
        for code in _provider_codes(franchise.abbreviation, franchise.aliases):
            if code not in codes:
                codes.append(code)
    return tuple(codes)


def split_concatenated_nfl_codes(blob: str) -> tuple[str, str] | None:
    """Split ``KCMIA`` into ``(KC, MIA)`` only when exactly one code pair fits."""

    codes = set(nfl_team_codes())
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


_NFL_EVENT_TEAMS = re.compile(
    r"^(?:KXNFLGAME|KXNFLSPREAD|KXNFLTOTAL)-(\d{2}[A-Z]{3}\d{2})([A-Z]{4,8})$"
)


def nfl_away_home_from_event_ticker(ticker: str | None) -> tuple[str, str] | None:
    """Away then home from an approved NFL event ticker, or None.

    Captured Kalshi event tickers put the away abbreviation before home:
    ``KXNFLGAME-26SEP20INDKC`` is Indianapolis at Kansas City.
    """

    match = _NFL_EVENT_TEAMS.match(str(ticker or "").strip().upper())
    if match is None:
        return None
    pair = split_concatenated_nfl_codes(match.group(2))
    if pair is None:
        return None
    away = resolve_nfl_team(pair[0])
    home = resolve_nfl_team(pair[1])
    if not away.ok or not home.ok or not away.canonical or not home.canonical:
        return None
    if away.canonical == home.canonical:
        return None
    return away.canonical, home.canonical


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
