from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from hashlib import sha256

from pydantic import BaseModel, model_validator

from sports_hedge.facts.aliases import resolve_team_name_for_competition
from sports_hedge.facts.catalog import CompetitionCode, competition_from_label
from sports_hedge.normalization.identity import kickoff_bucket
from sports_hedge.normalization.text import normalize_text


class NaiveKickoffError(ValueError):
    """Raised when a timezone-naive kickoff is offered to shared match identity."""


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
    def reject_naive_kickoff(self) -> CanonicalMatchRef:
        require_aware_kickoff(self.kickoff_utc)
        return self


def require_aware_kickoff(kickoff_utc: datetime) -> datetime:
    """Reject naive kickoffs. Adapters must convert local time before this boundary."""

    if kickoff_utc.tzinfo is None:
        raise NaiveKickoffError(
            "naive kickoff_utc is not allowed; convert to a timezone-aware datetime "
            "in the adapter before constructing canonical match identity"
        )
    return kickoff_utc


def canonical_team_id(name: str, competition: str | None = None) -> str:
    resolved = resolve_team_name_for_competition(name, competition)
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

    require_aware_kickoff(kickoff_utc)
    payload = "|".join(
        [
            "football",
            normalize_text(competition_code),
            season.strip(),
            resolve_team_name_for_competition(home_team, competition_code),
            resolve_team_name_for_competition(away_team, competition_code),
            kickoff_bucket(kickoff_utc).isoformat(),
        ]
    )
    return f"match:{sha256(payload.encode('utf-8')).hexdigest()[:24]}"


def season_for_kickoff(kickoff_utc: datetime) -> str:
    utc = require_aware_kickoff(kickoff_utc).astimezone(UTC)
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
    home = resolve_team_name_for_competition(home_team, spec.code.value)
    away = resolve_team_name_for_competition(away_team, spec.code.value)
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
