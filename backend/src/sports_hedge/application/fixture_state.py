from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

from sports_hedge.domain.models import VenueName


class MatchbookFixtureState(BaseModel):
    """Fixture fields actually present on a Matchbook event payload.

    Live scores are preserved only when the payload contains explicit numeric
    home/away score keys. Missing score fields stay unavailable; they are never
    inferred from market prices or a third-party score provider.
    """

    source: Literal[VenueName.MATCHBOOK] = VenueName.MATCHBOOK
    venue_status: str | None = None
    in_running: bool | None = None
    live_score_supported: bool = False
    home_score: int | None = Field(default=None, ge=0)
    away_score: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def reject_partial_invented_scores(self) -> "MatchbookFixtureState":
        if not self.live_score_supported:
            self.home_score = None
            self.away_score = None
        return self


def matchbook_fixture_state(payload: dict[str, Any]) -> MatchbookFixtureState:
    status_raw = _first_scalar(payload, "status", "state")
    venue_status = str(status_raw).strip().casefold() if status_raw is not None else None
    if venue_status == "":
        venue_status = None
    in_running = _optional_bool(
        _first_scalar(payload, "in-running-flag", "in_running_flag", "in-play", "in_play")
    )
    home_score, away_score, supported = _explicit_scores(payload)
    return MatchbookFixtureState(
        venue_status=venue_status,
        in_running=in_running,
        live_score_supported=supported,
        home_score=home_score,
        away_score=away_score,
    )


def _explicit_scores(payload: dict[str, Any]) -> tuple[int | None, int | None, bool]:
    home = _optional_non_negative_int(
        _first_scalar(payload, "home-score", "home_score", "homeScore")
    )
    away = _optional_non_negative_int(
        _first_scalar(payload, "away-score", "away_score", "awayScore")
    )
    nested = payload.get("score")
    if isinstance(nested, dict):
        if home is None:
            home = _optional_non_negative_int(
                _first_scalar(nested, "home", "home-score", "home_score")
            )
        if away is None:
            away = _optional_non_negative_int(
                _first_scalar(nested, "away", "away-score", "away_score")
            )
    if home is None or away is None:
        return None, None, False
    return home, away, True


def _first_scalar(payload: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in payload and payload[key] is not None:
            return payload[key]
    return None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    text = str(value).strip().casefold()
    if text in {"true", "1", "yes", "y"}:
        return True
    if text in {"false", "0", "no", "n"}:
        return False
    return None


def _optional_non_negative_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        parsed = int(str(value).strip())
    except (TypeError, ValueError):
        return None
    if parsed < 0:
        return None
    return parsed
