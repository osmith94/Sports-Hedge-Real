from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from sports_hedge.facts.catalog import CompetitionCode
from sports_hedge.facts.identity import (
    CanonicalMatchRef,
    NaiveKickoffError,
    build_match_ref,
    canonical_match_id,
    canonical_team_id,
    season_for_kickoff,
)

AWARE = datetime(2025, 8, 16, 17, 30, tzinfo=UTC)
NAIVE = datetime(2025, 8, 16, 17, 30)  # noqa: DTZ001


def test_canonical_match_id_rejects_naive_kickoff() -> None:
    with pytest.raises(NaiveKickoffError, match="naive kickoff_utc"):
        canonical_match_id(
            competition_code="premier_league",
            season="2025/26",
            home_team="Arsenal",
            away_team="Chelsea",
            kickoff_utc=NAIVE,
        )


def test_season_for_kickoff_rejects_naive_datetime() -> None:
    with pytest.raises(NaiveKickoffError, match="naive kickoff_utc"):
        season_for_kickoff(NAIVE)


def test_build_match_ref_rejects_naive_kickoff() -> None:
    with pytest.raises(NaiveKickoffError, match="naive kickoff_utc"):
        build_match_ref(
            competition="Premier League",
            home_team="Arsenal",
            away_team="Chelsea",
            kickoff_utc=NAIVE,
        )


def test_canonical_match_ref_rejects_naive_kickoff() -> None:
    with pytest.raises(ValidationError, match="naive kickoff_utc"):
        CanonicalMatchRef(
            canonical_match_id="match:placeholder",
            competition_code=CompetitionCode.PREMIER_LEAGUE,
            season="2025/26",
            home_team="arsenal",
            away_team="chelsea",
            home_team_id=canonical_team_id("Arsenal"),
            away_team_id=canonical_team_id("Chelsea"),
            kickoff_utc=NAIVE,
        )


def test_aware_kickoff_is_accepted_without_inventing_timezone() -> None:
    match_id = canonical_match_id(
        competition_code="premier_league",
        season="2025/26",
        home_team="Arsenal",
        away_team="Chelsea",
        kickoff_utc=AWARE,
    )
    assert match_id.startswith("match:")
    assert season_for_kickoff(AWARE) == "2025/26"
    ref = build_match_ref(
        competition="Premier League",
        home_team="Arsenal",
        away_team="Chelsea",
        kickoff_utc=AWARE,
        season="2025/26",
    )
    assert ref.canonical_match_id == match_id
    assert ref.kickoff_utc.tzinfo is not None
