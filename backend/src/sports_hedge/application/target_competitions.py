from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.normalization.text import normalize_text


class TargetCompetitionCode(StrEnum):
    PREMIER_LEAGUE = "premier_league"
    CHAMPIONSHIP = "championship"
    LA_LIGA = "la_liga"


class TargetCompetition(BaseModel):
    code: TargetCompetitionCode
    display_name: str
    aliases: tuple[str, ...] = Field(default_factory=tuple)
    polymarket_gamma_series_id: str | None = None
    polymarket_gamma_sport: str | None = None


# Public Gamma GET /sports (retrieved 2026-09-12): epl=10188, elc=10355, lal=10193.
# Series IDs are coverage claims for those competitions only. Empty series_id means
# Sports Hedge must not invent Polymarket markets for that competition.
TARGET_COMPETITIONS: tuple[TargetCompetition, ...] = (
    TargetCompetition(
        code=TargetCompetitionCode.PREMIER_LEAGUE,
        display_name="English Premier League",
        aliases=(
            "premier league",
            "english premier league",
            "epl",
            "barclays premier league",
            "premier league england",
        ),
        polymarket_gamma_series_id="10188",
        polymarket_gamma_sport="epl",
    ),
    TargetCompetition(
        code=TargetCompetitionCode.CHAMPIONSHIP,
        display_name="EFL Championship",
        aliases=(
            "championship",
            "efl championship",
            "english championship",
            "sky bet championship",
            "english football league championship",
        ),
        polymarket_gamma_series_id="10355",
        polymarket_gamma_sport="elc",
    ),
    TargetCompetition(
        code=TargetCompetitionCode.LA_LIGA,
        display_name="Spain La Liga",
        aliases=(
            "la liga",
            "laliga",
            "la liga santander",
            "la liga ea sports",
            "primera division",
            "primera división",
            "spanish primera",
            "spanish primera division",
        ),
        polymarket_gamma_series_id="10193",
        polymarket_gamma_sport="lal",
    ),
)

_ALIAS_INDEX: dict[str, TargetCompetition] = {}
for _item in TARGET_COMPETITIONS:
    for _alias in (_item.display_name, _item.code.value, *_item.aliases):
        _ALIAS_INDEX[normalize_text(_alias)] = _item

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


class ScopeDecision(BaseModel):
    allowed: bool
    reason: str | None = None
    competition: TargetCompetition | None = None
    label: str | None = None
    sport: str | None = None


def resolve_target_competition(label: str | None) -> TargetCompetition | None:
    """Exact alias match only. Unknown and ambiguous labels fail closed."""

    if label is None:
        return None
    normalized = normalize_text(label)
    if not normalized:
        return None
    return _ALIAS_INDEX.get(normalized)


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


def _first_str(payload: dict[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
