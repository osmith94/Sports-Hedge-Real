from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class CompetitionCode(StrEnum):
    PREMIER_LEAGUE = "premier_league"
    CHAMPIONSHIP = "championship"
    LA_LIGA = "la_liga"
    CHAMPIONS_LEAGUE = "champions_league"


class CompetitionSeason(BaseModel):
    """Catalog entry for a competition/season pair.

    ``expected_league_matches`` is calendar metadata (e.g. 20-team double
    round-robin = 380). It is not a substitute for imported fixture rows.
    Champions League has no fixed league-match count because of qualifying.
    """

    code: CompetitionCode
    display_name: str
    season: str
    in_bounded_universe: bool
    expected_league_matches: int | None = None
    football_data_div: str | None = None
    aliases: tuple[str, ...] = Field(default_factory=tuple)


_COMPETITIONS: tuple[CompetitionSeason, ...] = (
    CompetitionSeason(
        code=CompetitionCode.PREMIER_LEAGUE,
        display_name="Premier League",
        season="2025/26",
        in_bounded_universe=True,
        expected_league_matches=380,
        football_data_div="E0",
        aliases=("premier league", "epl", "english premier league", "e0"),
    ),
    CompetitionSeason(
        code=CompetitionCode.CHAMPIONSHIP,
        display_name="Championship",
        season="2025/26",
        in_bounded_universe=True,
        expected_league_matches=552,
        football_data_div="E1",
        aliases=("efl championship", "english championship", "e1"),
    ),
    CompetitionSeason(
        code=CompetitionCode.LA_LIGA,
        display_name="La Liga",
        season="2025/26",
        in_bounded_universe=True,
        expected_league_matches=380,
        football_data_div="SP1",
        aliases=("la liga", "primera division", "primera división", "sp1"),
    ),
    CompetitionSeason(
        code=CompetitionCode.CHAMPIONS_LEAGUE,
        display_name="UEFA Champions League",
        season="2025/26",
        in_bounded_universe=False,
        expected_league_matches=None,
        football_data_div=None,
        aliases=("champions league", "uefa champions league", "ucl"),
    ),
)


def list_competitions() -> tuple[CompetitionSeason, ...]:
    return _COMPETITIONS


def bounded_universe() -> tuple[CompetitionSeason, ...]:
    return tuple(item for item in _COMPETITIONS if item.in_bounded_universe)


BOUNDED_UNIVERSE = bounded_universe()


def competition_by_code(code: str, season: str = "2025/26") -> CompetitionSeason | None:
    wanted = code.strip().casefold()
    for item in _COMPETITIONS:
        if item.code.value == wanted and item.season == season:
            return item
    return None


def competition_from_label(value: str, season: str = "2025/26") -> CompetitionSeason | None:
    from sports_hedge.normalization.text import normalize_text

    normalized = normalize_text(value)
    for item in _COMPETITIONS:
        if item.season != season:
            continue
        names = {normalize_text(item.display_name), normalize_text(item.code.value), *item.aliases}
        if item.football_data_div:
            names.add(normalize_text(item.football_data_div))
        if normalized in names:
            return item
    return None
