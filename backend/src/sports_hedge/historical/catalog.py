from __future__ import annotations

from sports_hedge.facts.identity import canonical_team_id
from sports_hedge.historical.errors import HistoricalMappingError
from sports_hedge.historical.models import Competition, CompetitionType, Season, Team
from sports_hedge.normalization.text import normalize_text

PREMIER_LEAGUE = Competition(
    competition_id="premier-league",
    name="Premier League",
    country="England",
    competition_type=CompetitionType.LEAGUE,
)
CHAMPIONSHIP = Competition(
    competition_id="championship",
    name="Championship",
    country="England",
    competition_type=CompetitionType.LEAGUE,
)
LA_LIGA = Competition(
    competition_id="la-liga",
    name="La Liga",
    country="Spain",
    competition_type=CompetitionType.LEAGUE,
)
CHAMPIONS_LEAGUE = Competition(
    competition_id="champions-league",
    name="UEFA Champions League",
    country=None,
    competition_type=CompetitionType.CONTINENTAL,
)

KNOWN_COMPETITIONS: tuple[Competition, ...] = (
    PREMIER_LEAGUE,
    CHAMPIONSHIP,
    LA_LIGA,
    CHAMPIONS_LEAGUE,
)

SUPPORTED_SEASON_LABEL = "2025/26"


def season_for(competition: Competition, label: str = SUPPORTED_SEASON_LABEL) -> Season:
    start_year, end_year = _parse_season_years(label)
    return Season(
        season_id=f"{competition.competition_id}:{label.replace('/', '-')}",
        competition_id=competition.competition_id,
        label=label,
        start_year=start_year,
        end_year=end_year,
    )


class HistoricalCatalog:
    """Explicit competition/team mapping. Ambiguous names fail closed."""

    def __init__(self) -> None:
        self._competitions: dict[str, Competition] = {}
        self._competition_aliases: dict[str, str] = {}
        self._teams: dict[str, Team] = {}
        self._team_aliases: dict[str, set[str]] = {}
        for competition in KNOWN_COMPETITIONS:
            self.register_competition(competition)
        self.add_competition_alias("English Premier League", PREMIER_LEAGUE.competition_id)
        self.add_competition_alias("EFL Championship", CHAMPIONSHIP.competition_id)
        self.add_competition_alias("LaLiga", LA_LIGA.competition_id)
        self.add_competition_alias("Primera Division", LA_LIGA.competition_id)
        self.add_competition_alias("Champions League", CHAMPIONS_LEAGUE.competition_id)
        self.add_competition_alias("UEFA Champions League", CHAMPIONS_LEAGUE.competition_id)
        self._seed_initial_teams()

    def register_competition(self, competition: Competition) -> None:
        self._competitions[competition.competition_id] = competition
        self.add_competition_alias(competition.name, competition.competition_id)
        self.add_competition_alias(competition.competition_id, competition.competition_id)

    def add_competition_alias(self, alias: str, competition_id: str) -> None:
        key = normalize_text(alias)
        existing = self._competition_aliases.get(key)
        if existing is not None and existing != competition_id:
            raise HistoricalMappingError(f"Ambiguous competition alias '{alias}'")
        self._competition_aliases[key] = competition_id

    def register_team(self, team: Team, aliases: tuple[str, ...] = ()) -> None:
        self._teams[team.team_id] = team
        names = (team.canonical_name, team.team_id, *aliases)
        for alias in names:
            self.add_team_alias(alias, team.team_id)

    def add_team_alias(self, alias: str, team_id: str) -> None:
        key = normalize_text(alias)
        mapped = self._team_aliases.setdefault(key, set())
        mapped.add(team_id)

    def resolve_competition(self, name: str) -> Competition:
        key = normalize_text(name)
        competition_id = self._competition_aliases.get(key)
        if competition_id is None:
            raise HistoricalMappingError(f"Unknown competition '{name}'")
        return self._competitions[competition_id]

    def resolve_season(self, competition: Competition, label: str) -> Season:
        normalized = _normalize_season_label(label)
        return season_for(competition, normalized)

    def resolve_team(self, name: str) -> Team:
        key = normalize_text(name)
        if not key:
            raise HistoricalMappingError("Team name is empty")
        candidates = self._team_aliases.get(key, set())
        if not candidates:
            raise HistoricalMappingError(f"Unknown team '{name}'")
        if len(candidates) != 1:
            names = sorted(self._teams[item].canonical_name for item in candidates)
            raise HistoricalMappingError(f"Ambiguous team '{name}' maps to {names}")
        return self._teams[next(iter(candidates))]

    def competitions(self) -> list[Competition]:
        return [self._competitions[key] for key in sorted(self._competitions)]

    def teams(self) -> list[Team]:
        return [self._teams[key] for key in sorted(self._teams)]

    def _seed_initial_teams(self) -> None:
        seeds = (
            Team(
                team_id=canonical_team_id("Arsenal"),
                canonical_name="Arsenal",
                country="England",
            ),
            Team(
                team_id=canonical_team_id("Chelsea"),
                canonical_name="Chelsea",
                country="England",
            ),
            Team(
                team_id=canonical_team_id("Leeds United"),
                canonical_name="Leeds United",
                country="England",
            ),
            Team(
                team_id=canonical_team_id("Leicester City"),
                canonical_name="Leicester City",
                country="England",
            ),
            Team(
                team_id=canonical_team_id("Real Madrid"),
                canonical_name="Real Madrid",
                country="Spain",
            ),
            Team(
                team_id=canonical_team_id("Barcelona"),
                canonical_name="Barcelona",
                country="Spain",
            ),
        )
        for team in seeds:
            self.register_team(team)
        self.add_team_alias("Barca", canonical_team_id("Barcelona"))
        self.add_team_alias("Real Madrid CF", canonical_team_id("Real Madrid"))


def _normalize_season_label(label: str) -> str:
    compact = label.strip().replace("–", "/").replace("-", "/")
    parts = [part for part in compact.split("/") if part]
    if len(parts) != 2:
        raise HistoricalMappingError(f"Unsupported season label '{label}'")
    try:
        start = int(parts[0])
        end = int(parts[1])
    except ValueError as exc:
        raise HistoricalMappingError(f"Unsupported season label '{label}'") from exc
    if end < 100:
        end = (start // 100) * 100 + end
    if end != start + 1:
        raise HistoricalMappingError(f"Unsupported season label '{label}'")
    return f"{start}/{str(end)[2:]}"


def _parse_season_years(label: str) -> tuple[int, int]:
    normalized = _normalize_season_label(label)
    start = int(normalized.split("/")[0])
    return start, start + 1
