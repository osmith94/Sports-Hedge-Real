from __future__ import annotations

from datetime import UTC, datetime
from typing import Protocol

from sports_hedge.historical.models import MatchEventType, MatchStatus, SourceMatchPayload


class HistoricalSourceAdapter(Protocol):
    """Contract for authorised/public historical stat sources."""

    name: str

    def fetch_matches(self, competition_id: str, season_label: str) -> list[SourceMatchPayload]:
        """Return source-neutral match bundles. Must not bypass access controls."""


class SyntheticHistoricalAdapter:
    """Authorised in-process fixtures for repository tests and local backfill."""

    name = "synthetic-fixtures"

    def fetch_matches(self, competition_id: str, season_label: str) -> list[SourceMatchPayload]:
        retrieved = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
        fixtures = {
            "premier-league": [_premier_league_match(retrieved, season_label)],
            "championship": [_championship_match(retrieved, season_label)],
            "la-liga": [_la_liga_match(retrieved, season_label)],
        }
        return list(fixtures.get(competition_id, []))


def _premier_league_match(retrieved: datetime, season_label: str) -> SourceMatchPayload:
    raw = {
        "competition": "Premier League",
        "season": season_label,
        "home": "Arsenal",
        "away": "Chelsea",
        "score": "2-1",
    }
    return SourceMatchPayload(
        source_name="synthetic-fixtures",
        source_match_id="syn-pl-2025-001",
        source_url="https://sports-hedge.local/historical/synthetic/pl-2025-001",
        retrieved_at=retrieved,
        source_timestamp=datetime(2025, 8, 16, 16, 55, tzinfo=UTC),
        competition_name="Premier League",
        season_label=season_label,
        kickoff_utc=datetime(2025, 8, 16, 17, 30, tzinfo=UTC),
        home_team="Arsenal",
        away_team="Chelsea",
        status=MatchStatus.FINISHED,
        home_ft_goals=2,
        away_ft_goals=1,
        home_ht_goals=1,
        away_ht_goals=0,
        venue_name="Emirates Stadium",
        home_corners=7,
        away_corners=4,
        home_yellow_cards=2,
        away_yellow_cards=3,
        home_red_cards=0,
        away_red_cards=1,
        home_penalties=1,
        away_penalties=0,
        events=[
            {
                "source_event_id": "syn-pl-2025-001-g1",
                "event_type": MatchEventType.GOAL.value,
                "minute": 12,
                "team": "home",
                "player_name": "Bukayo Saka",
                "period": "first_half",
            },
            {
                "source_event_id": "syn-pl-2025-001-p1",
                "event_type": MatchEventType.PENALTY.value,
                "minute": 12,
                "team": "home",
                "player_name": "Bukayo Saka",
                "period": "first_half",
            },
            {
                "source_event_id": "syn-pl-2025-001-g2",
                "event_type": MatchEventType.GOAL.value,
                "minute": 67,
                "team": "away",
                "player_name": "Cole Palmer",
                "period": "second_half",
            },
            {
                "source_event_id": "syn-pl-2025-001-g3",
                "event_type": MatchEventType.GOAL.value,
                "minute": 81,
                "team": "home",
                "player_name": "Gabriel Jesus",
                "period": "second_half",
            },
            {
                "source_event_id": "syn-pl-2025-001-y1",
                "event_type": MatchEventType.YELLOW_CARD.value,
                "minute": 40,
                "team": "away",
                "player_name": "Enzo Fernandez",
                "period": "first_half",
            },
            {
                "source_event_id": "syn-pl-2025-001-r1",
                "event_type": MatchEventType.RED_CARD.value,
                "minute": 74,
                "team": "away",
                "player_name": "Moises Caicedo",
                "period": "second_half",
            },
            {
                "source_event_id": "syn-pl-2025-001-s1",
                "event_type": MatchEventType.SUBSTITUTION.value,
                "minute": 62,
                "team": "home",
                "player_name": "Gabriel Jesus",
                "related_player_name": "Kai Havertz",
                "period": "second_half",
            },
        ],
        lineups=[
            {
                "team": "home",
                "player_name": "David Raya",
                "shirt_number": 1,
                "position": "GK",
                "is_starter": True,
            },
            {
                "team": "home",
                "player_name": "Bukayo Saka",
                "shirt_number": 7,
                "position": "RW",
                "is_starter": True,
            },
            {
                "team": "home",
                "player_name": "Kai Havertz",
                "shirt_number": 29,
                "position": "ST",
                "is_starter": True,
            },
            {
                "team": "home",
                "player_name": "Gabriel Jesus",
                "shirt_number": 9,
                "position": "ST",
                "is_starter": False,
            },
            {
                "team": "away",
                "player_name": "Robert Sanchez",
                "shirt_number": 1,
                "position": "GK",
                "is_starter": True,
            },
            {
                "team": "away",
                "player_name": "Cole Palmer",
                "shirt_number": 20,
                "position": "AM",
                "is_starter": True,
            },
        ],
        raw_payload=raw,
    )


def _championship_match(retrieved: datetime, season_label: str) -> SourceMatchPayload:
    return SourceMatchPayload(
        source_name="synthetic-fixtures",
        source_match_id="syn-ch-2025-001",
        source_url="https://sports-hedge.local/historical/synthetic/ch-2025-001",
        retrieved_at=retrieved,
        source_timestamp=datetime(2025, 8, 9, 14, 0, tzinfo=UTC),
        competition_name="Championship",
        season_label=season_label,
        kickoff_utc=datetime(2025, 8, 9, 14, 0, tzinfo=UTC),
        home_team="Leeds United",
        away_team="Leicester City",
        status=MatchStatus.FINISHED,
        home_ft_goals=1,
        away_ft_goals=1,
        home_corners=5,
        away_corners=6,
        home_yellow_cards=1,
        away_yellow_cards=2,
        home_red_cards=0,
        away_red_cards=0,
        events=[
            {
                "source_event_id": "syn-ch-2025-001-g1",
                "event_type": MatchEventType.GOAL.value,
                "minute": None,
                "team": "home",
                "player_name": "Willy Gnonto",
            }
        ],
        raw_payload={"score": "1-1", "ht": None},
    )


def _la_liga_match(retrieved: datetime, season_label: str) -> SourceMatchPayload:
    return SourceMatchPayload(
        source_name="synthetic-fixtures",
        source_match_id="syn-ll-2025-001",
        source_url="https://sports-hedge.local/historical/synthetic/ll-2025-001",
        retrieved_at=retrieved,
        source_timestamp=datetime(2025, 8, 17, 19, 0, tzinfo=UTC),
        competition_name="La Liga",
        season_label=season_label,
        kickoff_utc=datetime(2025, 8, 17, 19, 0, tzinfo=UTC),
        home_team="Real Madrid",
        away_team="Barcelona",
        status=MatchStatus.FINISHED,
        home_ft_goals=3,
        away_ft_goals=2,
        home_ht_goals=2,
        away_ht_goals=1,
        home_yellow_cards=1,
        away_yellow_cards=1,
        home_red_cards=0,
        away_red_cards=0,
        home_penalties=0,
        away_penalties=1,
        events=[
            {
                "source_event_id": "syn-ll-2025-001-g1",
                "event_type": MatchEventType.GOAL.value,
                "minute": 8,
                "team": "home",
                "player_name": "Kylian Mbappe",
            },
            {
                "source_event_id": "syn-ll-2025-001-p1",
                "event_type": MatchEventType.PENALTY.value,
                "minute": 55,
                "team": "away",
                "player_name": "Robert Lewandowski",
            },
        ],
        lineups=[
            {
                "team": "home",
                "player_name": "Thibaut Courtois",
                "shirt_number": 1,
                "position": "GK",
                "is_starter": True,
            },
            {
                "team": "away",
                "player_name": "Marc-Andre ter Stegen",
                "shirt_number": 1,
                "position": "GK",
                "is_starter": True,
            },
        ],
        raw_payload={"score": "3-2", "corners": None},
    )
