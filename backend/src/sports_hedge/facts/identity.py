from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256

from pydantic import BaseModel, model_validator

from sports_hedge.facts.aliases import football_alias_registry
from sports_hedge.facts.catalog import CompetitionCode, competition_from_label
from sports_hedge.normalization.identity import kickoff_bucket
from sports_hedge.normalization.text import normalize_text


class KickoffPrecision(StrEnum):
    MINUTE = "minute"
    DATE = "date"
    UNKNOWN = "unknown"


class CanonicalMatchRef(BaseModel):
    """Source-independent match identity shared by facts and odds."""

    canonical_match_id: str
    competition_code: CompetitionCode
    season: str
    home_team: str
    away_team: str
    home_team_id: str
    away_team_id: str
    kickoff_utc: datetime
    kickoff_precision: KickoffPrecision = KickoffPrecision.MINUTE

    @model_validator(mode="after")
    def ensure_timezone(self) -> CanonicalMatchRef:
        if self.kickoff_utc.tzinfo is None:
            self.kickoff_utc = self.kickoff_utc.replace(tzinfo=UTC)
        return self


def canonical_team_id(name: str) -> str:
    resolved = football_alias_registry.resolve(name)
    digest = sha256(f"football|team|{resolved}".encode()).hexdigest()[:24]
    return f"team:{digest}"


def canonical_match_id(
    *,
    competition_code: str,
    season: str,
    home_team: str,
    away_team: str,
    kickoff_utc: datetime,
) -> str:
    """Deterministic match key. Source IDs are never part of this key."""

    payload = "|".join(
        [
            "football",
            normalize_text(competition_code),
            season.strip(),
            football_alias_registry.resolve(home_team),
            football_alias_registry.resolve(away_team),
            kickoff_bucket(kickoff_utc).isoformat(),
        ]
    )
    return f"match:{sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def season_for_kickoff(kickoff_utc: datetime) -> str:
    aware = kickoff_utc if kickoff_utc.tzinfo is not None else kickoff_utc.replace(tzinfo=UTC)
    utc = aware.astimezone(UTC)
    if utc.month >= 7:
        return f"{utc.year}/{str(utc.year + 1)[2:]}"
    return f"{utc.year - 1}/{str(utc.year)[2:]}"


def build_match_ref(
    *,
    competition: str,
    home_team: str,
    away_team: str,
    kickoff_utc: datetime,
    season: str | None = None,
    kickoff_precision: KickoffPrecision = KickoffPrecision.MINUTE,
) -> CanonicalMatchRef:
    resolved_season = season or season_for_kickoff(kickoff_utc)
    spec = competition_from_label(competition, resolved_season)
    if spec is None:
        spec = competition_from_label(competition)
    if spec is None:
        raise ValueError(f"unknown competition label: {competition!r}")
    home = football_alias_registry.resolve(home_team)
    away = football_alias_registry.resolve(away_team)
    return CanonicalMatchRef(
        canonical_match_id=canonical_match_id(
            competition_code=spec.code.value,
            season=spec.season if season is None else resolved_season,
            home_team=home,
            away_team=away,
            kickoff_utc=kickoff_utc,
        ),
        competition_code=spec.code,
        season=spec.season if season is None else resolved_season,
        home_team=home,
        away_team=away,
        home_team_id=canonical_team_id(home),
        away_team_id=canonical_team_id(away),
        kickoff_utc=kickoff_utc,
        kickoff_precision=kickoff_precision,
    )
