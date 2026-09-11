from __future__ import annotations

import json
from hashlib import sha256
from typing import Any
from uuid import NAMESPACE_URL, uuid5

from sports_hedge.historical.adapters import HistoricalSourceAdapter
from sports_hedge.historical.catalog import HistoricalCatalog, season_for
from sports_hedge.historical.errors import HistoricalMappingError
from sports_hedge.historical.models import (
    DataQualityFlag,
    LineupRecord,
    MatchEventRecord,
    MatchEventType,
    MatchRecord,
    SourceMatchPayload,
    SourceProvenance,
    TeamMatchStatsRecord,
)
from sports_hedge.historical.repository import SqliteHistoricalRepository
from sports_hedge.normalization.text import normalize_text


class HistoricalIngestionService:
    """Idempotent ingestion from source-neutral adapters into the repository."""

    def __init__(
        self,
        repository: SqliteHistoricalRepository,
        catalog: HistoricalCatalog | None = None,
    ) -> None:
        self._repository = repository
        self._catalog = catalog or HistoricalCatalog()
        for competition in self._catalog.competitions():
            self._repository.upsert_competition(competition)
            self._repository.upsert_season(season_for(competition))
        for team in self._catalog.teams():
            self._repository.upsert_team(team)

    def ingest(self, payload: SourceMatchPayload) -> str:
        competition = self._catalog.resolve_competition(payload.competition_name)
        season = self._catalog.resolve_season(competition, payload.season_label)
        home = self._catalog.resolve_team(payload.home_team)
        away = self._catalog.resolve_team(payload.away_team)
        if home.team_id == away.team_id:
            raise HistoricalMappingError("Home and away teams resolved to the same team")

        self._repository.upsert_season(season)
        self._repository.upsert_team(home)
        self._repository.upsert_team(away)

        match_id = self._resolve_match_id(
            payload=payload,
            competition_id=competition.competition_id,
            season_id=season.season_id,
            home_team_id=home.team_id,
            away_team_id=away.team_id,
        )
        match = MatchRecord(
            match_id=match_id,
            competition_id=competition.competition_id,
            season_id=season.season_id,
            kickoff_utc=payload.kickoff_utc,
            home_team_id=home.team_id,
            away_team_id=away.team_id,
            home_team_name=home.canonical_name,
            away_team_name=away.canonical_name,
            competition_name=competition.name,
            season_label=season.label,
            status=payload.status,
            home_ft_goals=payload.home_ft_goals,
            away_ft_goals=payload.away_ft_goals,
            home_ht_goals=payload.home_ht_goals,
            away_ht_goals=payload.away_ht_goals,
            venue_name=payload.venue_name,
        )
        self._repository.upsert_match(match)
        self._repository.upsert_team_stats(
            TeamMatchStatsRecord(
                match_id=match_id,
                team_id=home.team_id,
                team_name=home.canonical_name,
                is_home=True,
                competition_id=competition.competition_id,
                season_id=season.season_id,
                corners=payload.home_corners,
                yellow_cards=payload.home_yellow_cards,
                red_cards=payload.home_red_cards,
                penalties=payload.home_penalties,
            )
        )
        self._repository.upsert_team_stats(
            TeamMatchStatsRecord(
                match_id=match_id,
                team_id=away.team_id,
                team_name=away.canonical_name,
                is_home=False,
                competition_id=competition.competition_id,
                season_id=season.season_id,
                corners=payload.away_corners,
                yellow_cards=payload.away_yellow_cards,
                red_cards=payload.away_red_cards,
                penalties=payload.away_penalties,
            )
        )
        teams = {"home": home, "away": away}
        for event in payload.events:
            self._ingest_event(match_id, payload.source_name, teams, event)
        for lineup in payload.lineups:
            self._ingest_lineup(match_id, payload.source_name, teams, lineup)

        flags = _quality_flags(payload)
        provenance = SourceProvenance(
            source_name=payload.source_name,
            source_match_id=payload.source_match_id,
            source_url=payload.source_url,
            retrieved_at=payload.retrieved_at,
            source_timestamp=payload.source_timestamp,
            raw_payload_hash=_payload_hash(payload),
            raw_payload=payload.raw_payload or payload.model_dump(mode="json"),
            quality_flags=flags,
            confidence=_confidence(flags),
        )
        self._repository.upsert_source_record(match_id, provenance)
        return match_id

    def ingest_many(self, payloads: list[SourceMatchPayload]) -> list[str]:
        return [self.ingest(payload) for payload in payloads]

    def ingest_adapter(
        self,
        adapter: HistoricalSourceAdapter,
        *,
        competition_id: str,
        season_label: str,
    ) -> list[str]:
        return self.ingest_many(
            adapter.fetch_matches(competition_id, season_label)
        )

    def _resolve_match_id(
        self,
        *,
        payload: SourceMatchPayload,
        competition_id: str,
        season_id: str,
        home_team_id: str,
        away_team_id: str,
    ) -> str:
        from_source = self._repository.get_match_id_for_source(
            payload.source_name, payload.source_match_id
        )
        from_identity = self._repository.find_match_id(
            competition_id=competition_id,
            season_id=season_id,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            kickoff_utc=payload.kickoff_utc,
        )
        if from_source and from_identity and from_source != from_identity:
            raise HistoricalMappingError(
                "Source match maps to a different canonical match than identity lookup"
            )
        if from_source:
            return from_source
        if from_identity:
            return from_identity
        seed = (
            f"{competition_id}|{season_id}|{home_team_id}|"
            f"{away_team_id}|{payload.kickoff_utc.isoformat()}"
        )
        return f"hist:{uuid5(NAMESPACE_URL, seed).hex[:24]}"

    def _ingest_event(
        self,
        match_id: str,
        source_name: str,
        teams: dict[str, Any],
        event: dict[str, Any],
    ) -> None:
        source_event_id = str(event.get("source_event_id") or "")
        if not source_event_id:
            raise HistoricalMappingError("Match event is missing source_event_id")
        event_type = MatchEventType(str(event["event_type"]))
        team = _resolve_side(teams, event.get("team"))
        event_id = str(uuid5(NAMESPACE_URL, f"{source_name}:{source_event_id}"))
        self._repository.upsert_event(
            MatchEventRecord(
                event_id=event_id,
                match_id=match_id,
                event_type=event_type,
                minute=event.get("minute"),
                extra_minute=event.get("extra_minute"),
                team_id=None if team is None else team.team_id,
                team_name=None if team is None else team.canonical_name,
                player_name=event.get("player_name"),
                related_player_name=event.get("related_player_name"),
                period=event.get("period"),
                source_name=source_name,
                source_event_id=source_event_id,
            )
        )

    def _ingest_lineup(
        self,
        match_id: str,
        source_name: str,
        teams: dict[str, Any],
        lineup: dict[str, Any],
    ) -> None:
        team = _resolve_side(teams, lineup.get("team"))
        if team is None:
            raise HistoricalMappingError("Lineup row is missing a home/away team side")
        player_name = str(lineup.get("player_name") or "").strip()
        if not player_name:
            raise HistoricalMappingError("Lineup row is missing player_name")
        lineup_id = str(
            uuid5(NAMESPACE_URL, f"{match_id}:{team.team_id}:{normalize_text(player_name)}")
        )
        self._repository.upsert_lineup(
            LineupRecord(
                lineup_id=lineup_id,
                match_id=match_id,
                team_id=team.team_id,
                team_name=team.canonical_name,
                player_name=player_name,
                shirt_number=lineup.get("shirt_number"),
                position=lineup.get("position"),
                is_starter=bool(lineup.get("is_starter", True)),
                source_name=source_name,
            )
        )


def _resolve_side(teams: dict[str, Any], side: Any):
    if side in (None, ""):
        return None
    if side not in teams:
        raise HistoricalMappingError(f"Unknown team side '{side}'")
    return teams[side]


def _payload_hash(payload: SourceMatchPayload) -> str:
    encoded = json.dumps(payload.model_dump(mode="json"), sort_keys=True, separators=(",", ":"))
    return sha256(encoded.encode("utf-8")).hexdigest()


def _quality_flags(payload: SourceMatchPayload) -> list[DataQualityFlag]:
    flags: list[DataQualityFlag] = []
    if payload.home_ft_goals is None or payload.away_ft_goals is None:
        flags.append(DataQualityFlag.MISSING_FT_SCORE)
    if payload.home_ht_goals is None or payload.away_ht_goals is None:
        flags.append(DataQualityFlag.MISSING_HT_SCORE)
    goals = [event for event in payload.events if event.get("event_type") == MatchEventType.GOAL]
    if payload.home_ft_goals is not None and payload.away_ft_goals is not None:
        expected = payload.home_ft_goals + payload.away_ft_goals
        if expected and (not goals or any(event.get("minute") is None for event in goals)):
            flags.append(DataQualityFlag.MISSING_GOAL_TIMESTAMPS)
    if payload.home_corners is None or payload.away_corners is None:
        flags.append(DataQualityFlag.MISSING_CORNERS)
    if payload.home_yellow_cards is None or payload.away_yellow_cards is None:
        flags.append(DataQualityFlag.MISSING_YELLOW_CARDS)
    if payload.home_red_cards is None or payload.away_red_cards is None:
        flags.append(DataQualityFlag.MISSING_RED_CARDS)
    if payload.home_penalties is None or payload.away_penalties is None:
        flags.append(DataQualityFlag.MISSING_PENALTIES)
    substitutions = [
        event for event in payload.events if event.get("event_type") == MatchEventType.SUBSTITUTION
    ]
    if not substitutions:
        flags.append(DataQualityFlag.MISSING_SUBSTITUTIONS)
    if not payload.lineups:
        flags.append(DataQualityFlag.MISSING_LINEUPS)
    elif len(payload.lineups) < 22:
        flags.append(DataQualityFlag.PARTIAL_LINEUPS)
    return flags


def _confidence(flags: list[DataQualityFlag]) -> float:
    if not flags:
        return 1.0
    return max(0.4, round(1.0 - (0.08 * len(flags)), 2))
