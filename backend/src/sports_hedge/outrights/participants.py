"""Explicit season-participant ids. Name-only tokens fail closed.

EPL clubs reuse the football team registry. NFL Super Bowl teams use a
dedicated season-team table so generic city labels (New York / Los Angeles)
cannot collapse NYG/NYJ or LAR/LAC. Players stay venue-native until a
registry exists.
"""

from __future__ import annotations

from sports_hedge.domain.outrights import NAME_ONLY_PARTICIPANT_PREFIXES, ParticipantType
from sports_hedge.facts.identity import canonical_team_id
from sports_hedge.normalization.text import normalize_text


class SeasonParticipantError(ValueError):
    """Fail-closed participant identity."""


# Explicit NFL season-team codes. Aliases are exact labels, never generic cities.
NFL_SEASON_TEAMS: dict[str, tuple[str, ...]] = {
    "nfl_team:ari": ("arizona cardinals", "arizona", "ari"),
    "nfl_team:atl": ("atlanta falcons", "atlanta", "atl"),
    "nfl_team:bal": ("baltimore ravens", "baltimore", "bal"),
    "nfl_team:buf": ("buffalo bills", "buffalo", "buf"),
    "nfl_team:car": ("carolina panthers", "carolina", "car"),
    "nfl_team:chi": ("chicago bears", "chicago", "chi"),
    "nfl_team:cin": ("cincinnati bengals", "cincinnati", "cin"),
    "nfl_team:cle": ("cleveland browns", "cleveland", "cle"),
    "nfl_team:dal": ("dallas cowboys", "dallas", "dal"),
    "nfl_team:den": ("denver broncos", "denver", "den"),
    "nfl_team:det": ("detroit lions", "detroit", "det"),
    "nfl_team:gb": ("green bay packers", "green bay", "gb"),
    "nfl_team:hou": ("houston texans", "houston", "hou"),
    "nfl_team:ind": ("indianapolis colts", "indianapolis", "ind"),
    "nfl_team:jax": ("jacksonville jaguars", "jacksonville", "jax"),
    "nfl_team:kc": ("kansas city chiefs", "kansas city", "kc"),
    "nfl_team:lv": ("las vegas raiders", "las vegas", "lv", "raiders"),
    "nfl_team:lac": ("los angeles chargers", "chargers", "lac"),
    "nfl_team:lar": ("los angeles rams", "rams", "lar"),
    "nfl_team:mia": ("miami dolphins", "miami", "mia"),
    "nfl_team:min": ("minnesota vikings", "minnesota", "min"),
    "nfl_team:ne": ("new england patriots", "new england", "ne"),
    "nfl_team:no": ("new orleans saints", "new orleans", "no"),
    "nfl_team:nyg": ("new york giants", "giants", "nyg"),
    "nfl_team:nyj": ("new york jets", "jets", "nyj"),
    "nfl_team:phi": ("philadelphia eagles", "philadelphia", "phi"),
    "nfl_team:pit": ("pittsburgh steelers", "pittsburgh", "pit"),
    "nfl_team:sf": ("san francisco 49ers", "san francisco", "sf"),
    "nfl_team:sea": ("seattle seahawks", "seattle", "sea"),
    "nfl_team:tb": ("tampa bay buccaneers", "tampa bay", "tb"),
    "nfl_team:ten": ("tennessee titans", "tennessee", "ten"),
    "nfl_team:was": ("washington commanders", "washington", "was"),
}

_GENERIC_CITY_VETOES = frozenset({"new york", "ny", "los angeles", "la"})


def _alias_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for canonical, aliases in NFL_SEASON_TEAMS.items():
        for alias in aliases:
            index[normalize_text(alias)] = canonical
        index[normalize_text(canonical)] = canonical
    return index


_NFL_ALIAS_INDEX = _alias_index()


def reject_name_only_participant_id(participant_id: str) -> str:
    token = str(participant_id or "").strip()
    if not token:
        raise SeasonParticipantError("missing_participant_canonical_id")
    if token.casefold().startswith(NAME_ONLY_PARTICIPANT_PREFIXES):
        raise SeasonParticipantError("name_only_participant_id_forbidden")
    return token


def epl_team_participant_id(club_name: str) -> str:
    """Football registry team id. Never a raw display name."""

    name = str(club_name or "").strip()
    if not name:
        raise SeasonParticipantError("missing_epl_team_name")
    return canonical_team_id(name)


def nfl_season_team_id(label: str) -> str:
    """Exact NFL season-team id. Generic NY/LA city tokens fail closed."""

    normalized = normalize_text(label)
    if not normalized:
        raise SeasonParticipantError("missing_nfl_team_label")
    if normalized in _GENERIC_CITY_VETOES:
        raise SeasonParticipantError("generic_city_participant_veto")
    canonical = _NFL_ALIAS_INDEX.get(normalized)
    if canonical is None:
        raise SeasonParticipantError("unknown_nfl_season_team")
    return canonical


def venue_native_player_id(*, venue: str, native_id: str) -> str:
    """Players remain venue-native until a registry exists. Never a display name."""

    venue_token = str(venue or "").strip().casefold()
    native = reject_name_only_participant_id(native_id)
    if not venue_token:
        raise SeasonParticipantError("missing_player_venue")
    if native.casefold().startswith(NAME_ONLY_PARTICIPANT_PREFIXES):
        raise SeasonParticipantError("name_only_participant_id_forbidden")
    return f"venue_native:{venue_token}:{native}"


def participant_namespaces_collide(left_type: ParticipantType, right_type: ParticipantType) -> bool:
    return left_type != right_type
