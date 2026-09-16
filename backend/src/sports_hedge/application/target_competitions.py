from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.domain.models import VenueName
from sports_hedge.normalization.text import normalize_text


class TargetCompetitionCode(StrEnum):
    PREMIER_LEAGUE = "premier_league"
    CHAMPIONSHIP = "championship"
    LA_LIGA = "la_liga"
    CARABAO_CUP = "carabao_cup"
    FA_CUP = "fa_cup"
    INTERNATIONAL_FRIENDLIES = "international_friendlies"
    BUNDESLIGA = "bundesliga"
    SERIE_A = "serie_a"


class TargetCompetition(BaseModel):
    code: TargetCompetitionCode
    display_name: str
    aliases: tuple[str, ...] = Field(default_factory=tuple)
    polymarket_gamma_series_id: str | None = None
    polymarket_gamma_sport: str | None = None
    kalshi_series_prefixes: tuple[str, ...] = Field(default_factory=tuple)


# Provider coverage is claimed only from read-only metadata. Empty series_id /
# empty Kalshi prefixes means Sports Hedge must not invent that venue's markets.
#
# Public Gamma GET /sports (retrieved 2026-09-16):
#   epl=10188, elc=10355, lal=10193, efl=10329 (EFL CUP), efa=10307 (FA Cup),
#   fif=10238 (FIFA Friendlies), bun=10194 (Bundesliga), sea=10203 (Serie A).
# Near-neighbor Gamma series left unmatched: bl2=10670 (2. Bundesliga),
# itsb=10676 (Serie B), clf=12410 (Club Friendlies), ecu1=11863 (LigaPro Serie A).
#
# Public Kalshi GET /series (retrieved 2026-09-16): KXEFLCUP*, KXFACUP*,
# KXINTLFRIENDLY*, plus match-level Bundesliga/Serie A GAME/BTTS/TOTAL/FTTS.
# Short KXBUNDESLIGA/KXSERIEA prefixes are not used: they would also match
# KXBUNDESLIGA2GAME (2. Bundesliga) and KXSERIEAWGAME (Serie A Femminile).
#
# Aliases include observed Matchbook / Gamma / Kalshi label shapes. Matching is
# exact after normalize_text; unknown labels fail closed.
TARGET_COMPETITIONS: tuple[TargetCompetition, ...] = (
    TargetCompetition(
        code=TargetCompetitionCode.PREMIER_LEAGUE,
        display_name="English Premier League",
        aliases=(
            "premier league",
            "english premier league",
            "england premier league",
            "eng premier league",
            "the premier league",
            "epl",
            "barclays premier league",
            "barclays premiership",
            "premier league england",
            "premier league 2025/26",
            "premier league 2026/27",
            "premier league 2026",
            "english premier league 2025/26",
            "english premier league 2026/27",
        ),
        polymarket_gamma_series_id="10188",
        polymarket_gamma_sport="epl",
        kalshi_series_prefixes=("KXEPL",),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.CHAMPIONSHIP,
        display_name="EFL Championship",
        aliases=(
            "championship",
            "the championship",
            "efl championship",
            "english championship",
            "england championship",
            "sky bet championship",
            "skybet championship",
            "english football league championship",
            "efl champ",
            "championship 2025/26",
            "efl championship 2025/26",
            "efl championship 2026/27",
        ),
        polymarket_gamma_series_id="10355",
        polymarket_gamma_sport="elc",
        kalshi_series_prefixes=("KXEFLCHAMPIONSHIP",),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.LA_LIGA,
        display_name="Spain La Liga",
        aliases=(
            "la liga",
            "laliga",
            "la liga santander",
            "la liga ea sports",
            "laliga ea sports",
            "primera division",
            "primera división",
            "spanish primera",
            "spanish primera division",
            "spanish la liga",
            "spain la liga",
            "spain primera division",
            "primera división de españa",
            "la liga 2025/26",
            "la liga 2026/27",
        ),
        polymarket_gamma_series_id="10193",
        polymarket_gamma_sport="lal",
        kalshi_series_prefixes=("KXLALIGA",),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.CARABAO_CUP,
        display_name="Carabao Cup",
        aliases=(
            "carabao cup",
            "the carabao cup",
            "efl cup",
            "the efl cup",
            "league cup",
            "the league cup",
            "english league cup",
            "england league cup",
            "football league cup",
            "english football league cup",
            "carabao cup 2026/27",
            "efl cup 2026/27",
            "league cup 2026/27",
        ),
        polymarket_gamma_series_id="10329",
        polymarket_gamma_sport="efl",
        kalshi_series_prefixes=("KXEFLCUP",),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.FA_CUP,
        display_name="FA Cup",
        aliases=(
            "fa cup",
            "the fa cup",
            "emirates fa cup",
            "the emirates fa cup",
            "english fa cup",
            "england fa cup",
            "fa cup 2026/27",
            "emirates fa cup 2026/27",
        ),
        polymarket_gamma_series_id="10307",
        polymarket_gamma_sport="efa",
        kalshi_series_prefixes=("KXFACUP",),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.INTERNATIONAL_FRIENDLIES,
        display_name="International Friendlies",
        aliases=(
            "international friendlies",
            "international friendly",
            "fifa friendlies",
            "fifa friendly",
            "senior international friendlies",
            "senior international friendly",
            "mens international friendlies",
            "men's international friendlies",
            "senior mens international friendlies",
            "senior men's international friendlies",
            "international friendly matches",
        ),
        polymarket_gamma_series_id="10238",
        polymarket_gamma_sport="fif",
        kalshi_series_prefixes=("KXINTLFRIENDLY",),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.BUNDESLIGA,
        display_name="Bundesliga",
        aliases=(
            "bundesliga",
            "german bundesliga",
            "germany bundesliga",
            "1 bundesliga",
            "bundesliga 1",
            "fussball bundesliga",
            "fußball-bundesliga",
            "bundesliga 2026/27",
            "german bundesliga 2026/27",
        ),
        polymarket_gamma_series_id="10194",
        polymarket_gamma_sport="bun",
        # Match-level prefixes only. Short KXBUNDESLIGA also matches 2. Bundesliga.
        kalshi_series_prefixes=(
            "KXBUNDESLIGAGAME",
            "KXBUNDESLIGABTTS",
            "KXBUNDESLIGATOTAL",
            "KXBUNDESLIGAFTTS",
        ),
    ),
    TargetCompetition(
        code=TargetCompetitionCode.SERIE_A,
        display_name="Serie A",
        aliases=(
            "serie a",
            "italian serie a",
            "italy serie a",
            "serie a italy",
            "serie a tim",
            "serie a enilive",
            "lega serie a",
            "serie a 2026/27",
            "italian serie a 2026/27",
        ),
        polymarket_gamma_series_id="10203",
        polymarket_gamma_sport="sea",
        # Match-level prefixes only. Short KXSERIEA also matches Serie A Femminile.
        kalshi_series_prefixes=(
            "KXSERIEAGAME",
            "KXSERIEABTTS",
            "KXSERIEATOTAL",
            "KXSERIEAFTTS",
        ),
    ),
)

_ALIAS_INDEX: dict[str, TargetCompetition] = {}
for _item in TARGET_COMPETITIONS:
    for _alias in (_item.display_name, _item.code.value, *_item.aliases):
        _ALIAS_INDEX[normalize_text(_alias)] = _item

_SERIES_INDEX: dict[str, TargetCompetition] = {
    item.polymarket_gamma_series_id: item
    for item in TARGET_COMPETITIONS
    if item.polymarket_gamma_series_id
}

_NON_FOOTBALL_SPORTS = {
    "cricket",
    "rugby",
    "rugby union",
    "rugby league",
    "tennis",
    "golf",
    "horse racing",
    "horseracing",
    "greyhound",
    "american football",
    "nfl",
    "nba",
    "nba basketball",
    "nhl",
    "mlb",
    "baseball",
    "ice hockey",
    "snooker",
    "darts",
    "boxing",
    "mma",
    "ufc",
}

_FOOTBALL_SPORTS = {"football", "soccer", "association football", "soccer football"}

UNMATCHED_POLYMARKET_COVERAGE = "unmatched / no supported Polymarket coverage"
EVENT_IDENTITY_MISMATCH = "event_identity_mismatch"
SERIES_NOT_QUERIED = "series_not_queried"
UNKNOWN_COMPETITION = "unknown_or_ambiguous_competition"
NON_FOOTBALL_SPORT = "non_football_sport"
OUT_OF_SCOPE_COMPETITION = "out_of_scope_competition"


class ScopeDecision(BaseModel):
    allowed: bool
    reason: str | None = None
    competition: TargetCompetition | None = None
    label: str | None = None
    sport: str | None = None


class ScopeFilterResult(BaseModel):
    allowed: list[dict[str, Any]] = Field(default_factory=list)
    skipped: int = 0
    skipped_by_reason: dict[str, int] = Field(default_factory=dict)
    rejected_labels: list[str] = Field(default_factory=list)


def resolve_target_competition(label: str | None) -> TargetCompetition | None:
    """Exact alias match only. Unknown and ambiguous labels fail closed."""

    if label is None:
        return None
    normalized = normalize_text(label)
    if not normalized:
        return None
    direct = _ALIAS_INDEX.get(normalized)
    if direct is not None:
        return direct
    stripped = _strip_season_suffix(normalized)
    if stripped != normalized:
        return _ALIAS_INDEX.get(stripped)
    return None


def resolve_target_competition_from_series_id(series_id: str | None) -> TargetCompetition | None:
    if not series_id:
        return None
    return _SERIES_INDEX.get(str(series_id).strip())


def resolve_target_competition_from_kalshi_ticker(series_ticker: str | None) -> TargetCompetition | None:
    ticker = str(series_ticker or "").strip().upper()
    if not ticker:
        return None
    matches: list[tuple[int, TargetCompetition]] = []
    for item in TARGET_COMPETITIONS:
        for prefix in item.kalshi_series_prefixes:
            if ticker.startswith(prefix):
                matches.append((len(prefix), item))
                break
    if not matches:
        return None
    matches.sort(key=lambda pair: pair[0], reverse=True)
    return matches[0][1]


def polymarket_series_ids_for_targets() -> list[str]:
    return [
        item.polymarket_gamma_series_id
        for item in TARGET_COMPETITIONS
        if item.polymarket_gamma_series_id
    ]


def matchbook_competition_label(payload: dict[str, Any]) -> str | None:
    direct = _first_str(payload, "competition-name", "competition_name", "competition")
    if direct:
        return direct
    for tag in payload.get("meta-tags", payload.get("meta_tags", [])) or []:
        if not isinstance(tag, dict):
            continue
        tag_type = normalize_text(str(tag.get("type", "")))
        name = str(tag.get("name", "")).strip()
        if name and tag_type in {"competition", "league", "tournament"}:
            return name
    return None


def matchbook_sport_label(payload: dict[str, Any]) -> str | None:
    direct = _first_str(payload, "sport-name", "sport_name", "sport")
    if direct:
        return direct
    for tag in payload.get("meta-tags", payload.get("meta_tags", [])) or []:
        if not isinstance(tag, dict):
            continue
        tag_type = normalize_text(str(tag.get("type", "")))
        name = str(tag.get("name", "")).strip()
        if name and tag_type in {"sport"}:
            return name
    return None


def scope_matchbook_event(payload: dict[str, Any]) -> ScopeDecision:
    """Collector-boundary gate. Provider filters must not be the only check."""

    sport = matchbook_sport_label(payload)
    if sport:
        sport_norm = normalize_text(sport)
        if sport_norm in _NON_FOOTBALL_SPORTS:
            return ScopeDecision(
                allowed=False,
                reason=NON_FOOTBALL_SPORT,
                label=matchbook_competition_label(payload),
                sport=sport,
            )
        if sport_norm not in _FOOTBALL_SPORTS:
            return ScopeDecision(
                allowed=False,
                reason=NON_FOOTBALL_SPORT,
                label=matchbook_competition_label(payload),
                sport=sport,
            )

    label = matchbook_competition_label(payload)
    resolved = resolve_target_competition(label)
    if resolved is None:
        return ScopeDecision(
            allowed=False,
            reason=UNKNOWN_COMPETITION,
            label=label,
            sport=sport,
        )
    return ScopeDecision(allowed=True, competition=resolved, label=label, sport=sport)


def scope_polymarket_event(payload: dict[str, Any]) -> ScopeDecision:
    series_target = _polymarket_series_target(payload)
    label = _first_str(payload, "competition", "league", "seriesTitle", "series_title")
    if series_target is not None:
        return ScopeDecision(
            allowed=True,
            competition=series_target,
            label=label or series_target.display_name,
            sport="football",
        )
    resolved = resolve_target_competition(label)
    if resolved is None:
        series_title = _polymarket_series_title(payload)
        resolved = resolve_target_competition(series_title)
        label = label or series_title
    if resolved is None:
        return ScopeDecision(
            allowed=False,
            reason=UNKNOWN_COMPETITION,
            label=label,
            sport="football",
        )
    return ScopeDecision(allowed=True, competition=resolved, label=label, sport="football")


def scope_kalshi_event(payload: dict[str, Any]) -> ScopeDecision:
    ticker = str(payload.get("series_ticker") or payload.get("ticker") or "").strip()
    series_target = resolve_target_competition_from_kalshi_ticker(ticker)
    if series_target is None:
        nested = payload.get("series")
        if isinstance(nested, dict):
            series_target = resolve_target_competition_from_kalshi_ticker(
                str(nested.get("ticker") or "")
            )
    label = _first_str(payload, "competition", "league", "title")
    if series_target is not None:
        return ScopeDecision(
            allowed=True,
            competition=series_target,
            label=label or series_target.display_name,
            sport="football",
        )
    resolved = resolve_target_competition(label)
    if resolved is None:
        return ScopeDecision(
            allowed=False,
            reason=UNKNOWN_COMPETITION,
            label=label or ticker or None,
            sport="football",
        )
    return ScopeDecision(allowed=True, competition=resolved, label=label, sport="football")


def filter_in_scope_events(
    payloads: list[dict[str, Any]],
    *,
    venue: VenueName,
) -> ScopeFilterResult:
    """Keep target-competition football only. Out-of-scope is skipped, not an issue."""

    allowed: list[dict[str, Any]] = []
    skipped_by_reason: dict[str, int] = {}
    rejected: list[str] = []
    seen_labels: set[str] = set()
    skipped = 0
    for payload in payloads:
        decision = _scope_for_venue(payload, venue)
        if decision.allowed:
            allowed.append(payload)
            continue
        skipped += 1
        reason = decision.reason or OUT_OF_SCOPE_COMPETITION
        skipped_by_reason[reason] = skipped_by_reason.get(reason, 0) + 1
        label = (decision.label or "").strip()
        if label and label not in seen_labels:
            seen_labels.add(label)
            rejected.append(label)
    return ScopeFilterResult(
        allowed=allowed,
        skipped=skipped,
        skipped_by_reason=skipped_by_reason,
        rejected_labels=rejected[:50],
    )


def _scope_for_venue(payload: dict[str, Any], venue: VenueName) -> ScopeDecision:
    if venue is VenueName.MATCHBOOK:
        return scope_matchbook_event(payload)
    if venue is VenueName.POLYMARKET:
        return scope_polymarket_event(payload)
    if venue is VenueName.KALSHI:
        return scope_kalshi_event(payload)
    return ScopeDecision(allowed=False, reason=UNKNOWN_COMPETITION)


def _polymarket_series_target(payload: dict[str, Any]) -> TargetCompetition | None:
    series_id = payload.get("series_id") or payload.get("seriesId")
    if isinstance(series_id, (int, str)):
        resolved = resolve_target_competition_from_series_id(str(series_id))
        if resolved is not None:
            return resolved
    series_items = payload.get("series")
    if isinstance(series_items, dict):
        series_items = [series_items]
    if isinstance(series_items, list):
        for series in series_items:
            if not isinstance(series, dict):
                continue
            resolved = resolve_target_competition_from_series_id(
                str(series.get("id", series.get("series_id", ""))).strip()
            )
            if resolved is not None:
                return resolved
            title = series.get("title") or series.get("name")
            resolved = resolve_target_competition(str(title) if title else None)
            if resolved is not None:
                return resolved
    return None


def _polymarket_series_title(payload: dict[str, Any]) -> str | None:
    series_items = payload.get("series")
    if isinstance(series_items, dict):
        title = series_items.get("title") or series_items.get("name")
        return str(title).strip() if title else None
    if isinstance(series_items, list):
        for series in series_items:
            if not isinstance(series, dict):
                continue
            title = series.get("title") or series.get("name")
            if title:
                return str(title).strip()
    return None


def _strip_season_suffix(normalized: str) -> str:
    parts = normalized.split()
    if len(parts) >= 2 and "/" in parts[-1] and parts[-1].replace("/", "").isdigit():
        return " ".join(parts[:-1])
    if len(parts) >= 2 and parts[-1].isdigit() and len(parts[-1]) == 4:
        return " ".join(parts[:-1])
    return normalized


def _first_str(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
