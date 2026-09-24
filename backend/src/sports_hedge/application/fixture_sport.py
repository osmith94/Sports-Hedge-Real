"""Canonical sport for the discovered-fixture read model.

This is presentation metadata. It is not a fixture identity key, catalogue key,
or scanner-scope input. Unknown labels stay unknown. Competition names are not
guessed into a sport.
"""

from __future__ import annotations

from typing import Any

from sports_hedge.application.target_competitions import (
    TargetCompetition,
    TargetCompetitionCode,
    competition_by_code,
    matchbook_sport_label,
    resolve_target_competition,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.normalization.text import normalize_text

UNKNOWN_SPORT = "unknown"

CANONICAL_SPORT_LABELS: dict[str, str] = {
    "football": "Football",
    "american_football": "American Football",
    "basketball": "Basketball",
    "baseball": "Baseball",
    "tennis": "Tennis",
    "boxing": "Boxing",
    "mma": "MMA",
    "motorsport": "Motorsport",
    "ice_hockey": "Ice Hockey",
    UNKNOWN_SPORT: "Unknown",
}

_PROVIDER_SPORT_SYNONYMS: dict[str, str] = {
    "soccer": "football",
    "association football": "football",
    "soccer football": "football",
    "american football": "american_football",
    "nfl": "american_football",
    "basketball": "basketball",
    "nba": "basketball",
    "nba basketball": "basketball",
    "college basketball": "basketball",
    "ncaab": "basketball",
    "baseball": "baseball",
    "mlb": "baseball",
    "tennis": "tennis",
    "boxing": "boxing",
    "mma": "mma",
    "ufc": "mma",
    "mixed martial arts": "mma",
    "motorsport": "motorsport",
    "motor sport": "motorsport",
    "formula 1": "motorsport",
    "formula one": "motorsport",
    "ice hockey": "ice_hockey",
    "nhl": "ice_hockey",
    "football": "football",
}


def resolve_discovered_fixture_sport(
    *,
    competition: str | None = None,
    target_competition_code: str | None = None,
    provider_sport: str | None = None,
    canonical_sport: str | None = None,
    register_canonical_key: str | None = None,
) -> str:
    """Sport token for a discovered fixture.

    Catalogue competitions and explicit provider/canonical sport tokens win.
    The CanonicalEvent football default is not evidence on its own. An
    unrecognized competition label is never mapped to a sport.
    """

    catalogue = _catalogue_competition(
        competition=competition,
        target_competition_code=target_competition_code,
    )
    if catalogue is not None:
        return _sport_for_catalogue(catalogue)
    keyed = _sport_for_register_key(register_canonical_key)
    if keyed is not None:
        return keyed
    provided = _sport_for_provider_label(provider_sport)
    if provided is not None:
        return provided
    explicit = _explicit_canonical_sport(canonical_sport)
    if explicit is not None:
        return explicit
    return UNKNOWN_SPORT


def provider_sport_label(venue: VenueName, raw: dict[str, Any] | None) -> str | None:
    """Provider sport field only. Does not read competition or event titles."""

    if not isinstance(raw, dict):
        return None
    if venue is VenueName.MATCHBOOK:
        return matchbook_sport_label(raw)
    sport = raw.get("sport")
    if isinstance(sport, dict):
        name = str(sport.get("name") or sport.get("sport") or "").strip()
        return name or None
    if isinstance(sport, str) and sport.strip():
        return sport.strip()
    return None


def _catalogue_competition(
    *,
    competition: str | None,
    target_competition_code: str | None,
) -> TargetCompetition | None:
    coded = competition_by_code(target_competition_code)
    if coded is not None:
        return coded
    return resolve_target_competition(competition)


def _sport_for_catalogue(item: TargetCompetition) -> str:
    if item.code is TargetCompetitionCode.NFL:
        return "american_football"
    if item.code in {TargetCompetitionCode.NBA, TargetCompetitionCode.NCAAB}:
        return "basketball"
    if item.code is TargetCompetitionCode.MLB:
        return "baseball"
    if item.code in {TargetCompetitionCode.ATP, TargetCompetitionCode.WTA}:
        return "tennis"
    return "football"


def _sport_for_register_key(key: str | None) -> str | None:
    text = str(key or "").strip().upper()
    if not text:
        return None
    if text.startswith("NFL_"):
        return "american_football"
    if text.startswith("NBA_") or text.startswith("NCAAB_"):
        return "basketball"
    if text.startswith("MLB_"):
        return "baseball"
    if text.startswith("TENNIS_"):
        return "tennis"
    if text in {"MATCH_RESULT_FT", "BTTS_FT", "FTTS_FT"} or text.startswith("TOTAL_GOALS_FT:"):
        return "football"
    return None


def _sport_for_provider_label(label: str | None) -> str | None:
    if label is None:
        return None
    normalized = normalize_text(str(label))
    if not normalized:
        return None
    return _PROVIDER_SPORT_SYNONYMS.get(normalized)


def _explicit_canonical_sport(label: str | None) -> str | None:
    """Accept an explicit non-default canonical sport token.

    ``football`` is the CanonicalEvent default, so it is not treated as
    evidence when the catalogue and the provider sport field are both absent.
    """

    mapped = _sport_for_provider_label(label)
    if mapped is None or mapped == "football":
        token = normalize_text(str(label or "")).replace(" ", "_")
        if token in CANONICAL_SPORT_LABELS and token not in {"football", UNKNOWN_SPORT}:
            return token
        return None
    return mapped
