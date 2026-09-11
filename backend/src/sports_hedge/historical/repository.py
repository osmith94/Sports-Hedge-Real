from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from sports_hedge.historical.errors import HistoricalConflictError
from sports_hedge.historical.models import (
    Competition,
    DataQualityFlag,
    LineupRecord,
    MatchEventRecord,
    MatchEventType,
    MatchRecord,
    MatchStatus,
    Season,
    SourceProvenance,
    Team,
    TeamMatchStatsRecord,
)


class SqliteHistoricalRepository:
    """Normalized historical football facts store. Database is the source of truth."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS competitions (
                competition_id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                country TEXT,
                competition_type TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS seasons (
                season_id TEXT PRIMARY KEY,
                competition_id TEXT NOT NULL,
                label TEXT NOT NULL,
                start_year INTEGER NOT NULL,
                end_year INTEGER NOT NULL,
                UNIQUE (competition_id, label),
                FOREIGN KEY (competition_id) REFERENCES competitions(competition_id)
            );

            CREATE TABLE IF NOT EXISTS teams (
                team_id TEXT PRIMARY KEY,
                canonical_name TEXT NOT NULL,
                country TEXT
            );

            CREATE TABLE IF NOT EXISTS matches (
                match_id TEXT PRIMARY KEY,
                competition_id TEXT NOT NULL,
                season_id TEXT NOT NULL,
                kickoff_utc TEXT NOT NULL,
                home_team_id TEXT NOT NULL,
                away_team_id TEXT NOT NULL,
                status TEXT NOT NULL,
                home_ft_goals INTEGER,
                away_ft_goals INTEGER,
                home_ht_goals INTEGER,
                away_ht_goals INTEGER,
                venue_name TEXT,
                UNIQUE (competition_id, season_id, home_team_id, away_team_id, kickoff_utc),
                FOREIGN KEY (competition_id) REFERENCES competitions(competition_id),
                FOREIGN KEY (season_id) REFERENCES seasons(season_id),
                FOREIGN KEY (home_team_id) REFERENCES teams(team_id),
                FOREIGN KEY (away_team_id) REFERENCES teams(team_id)
            );

            CREATE TABLE IF NOT EXISTS match_events (
                event_id TEXT PRIMARY KEY,
                match_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                minute INTEGER,
                extra_minute INTEGER,
                team_id TEXT,
                player_name TEXT,
                related_player_name TEXT,
                period TEXT,
                source_name TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                UNIQUE (source_name, source_event_id),
                FOREIGN KEY (match_id) REFERENCES matches(match_id)
            );

            CREATE TABLE IF NOT EXISTS team_match_stats (
                match_id TEXT NOT NULL,
                team_id TEXT NOT NULL,
                corners INTEGER,
                yellow_cards INTEGER,
                red_cards INTEGER,
                penalties INTEGER,
                PRIMARY KEY (match_id, team_id),
                FOREIGN KEY (match_id) REFERENCES matches(match_id),
                FOREIGN KEY (team_id) REFERENCES teams(team_id)
            );

            CREATE TABLE IF NOT EXISTS lineups (
                lineup_id TEXT PRIMARY KEY,
                match_id TEXT NOT NULL,
                team_id TEXT NOT NULL,
                player_name TEXT NOT NULL,
                shirt_number INTEGER,
                position TEXT,
                is_starter INTEGER NOT NULL,
                source_name TEXT NOT NULL,
                UNIQUE (match_id, team_id, player_name),
                FOREIGN KEY (match_id) REFERENCES matches(match_id),
                FOREIGN KEY (team_id) REFERENCES teams(team_id)
            );

            CREATE TABLE IF NOT EXISTS source_records (
                source_record_id TEXT PRIMARY KEY,
                source_name TEXT NOT NULL,
                source_match_id TEXT NOT NULL,
                match_id TEXT NOT NULL,
                source_url TEXT,
                retrieved_at TEXT NOT NULL,
                source_timestamp TEXT,
                raw_payload_hash TEXT NOT NULL,
                raw_payload_json TEXT NOT NULL,
                quality_flags_json TEXT NOT NULL,
                confidence REAL NOT NULL,
                UNIQUE (source_name, source_match_id),
                FOREIGN KEY (match_id) REFERENCES matches(match_id)
            );

            CREATE INDEX IF NOT EXISTS idx_matches_competition_season
                ON matches(competition_id, season_id, kickoff_utc);
            CREATE INDEX IF NOT EXISTS idx_matches_teams
                ON matches(home_team_id, away_team_id, kickoff_utc);
            CREATE UNIQUE INDEX IF NOT EXISTS idx_match_events_natural
                ON match_events(
                    match_id,
                    event_type,
                    IFNULL(minute, -1),
                    IFNULL(team_id, ''),
                    IFNULL(player_name, '')
                );
            CREATE INDEX IF NOT EXISTS idx_events_match
                ON match_events(match_id, event_type);
            CREATE INDEX IF NOT EXISTS idx_stats_team
                ON team_match_stats(team_id);
            CREATE INDEX IF NOT EXISTS idx_source_match
                ON source_records(match_id, source_name);
            """
        )
        self._connection.commit()

    def upsert_competition(self, competition: Competition) -> None:
        self._connection.execute(
            """
            INSERT INTO competitions (competition_id, name, country, competition_type)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(competition_id) DO UPDATE SET
                name = excluded.name,
                country = excluded.country,
                competition_type = excluded.competition_type
            """,
            (
                competition.competition_id,
                competition.name,
                competition.country,
                competition.competition_type.value,
            ),
        )
        self._connection.commit()

    def upsert_season(self, season: Season) -> None:
        self._connection.execute(
            """
            INSERT INTO seasons (season_id, competition_id, label, start_year, end_year)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(season_id) DO UPDATE SET
                competition_id = excluded.competition_id,
                label = excluded.label,
                start_year = excluded.start_year,
                end_year = excluded.end_year
            """,
            (
                season.season_id,
                season.competition_id,
                season.label,
                season.start_year,
                season.end_year,
            ),
        )
        self._connection.commit()

    def upsert_team(self, team: Team) -> None:
        self._connection.execute(
            """
            INSERT INTO teams (team_id, canonical_name, country)
            VALUES (?, ?, ?)
            ON CONFLICT(team_id) DO UPDATE SET
                canonical_name = excluded.canonical_name,
                country = excluded.country
            """,
            (team.team_id, team.canonical_name, team.country),
        )
        self._connection.commit()

    def get_match_id_for_source(self, source_name: str, source_match_id: str) -> str | None:
        row = self._connection.execute(
            """
            SELECT match_id FROM source_records
            WHERE source_name = ? AND source_match_id = ?
            """,
            (source_name, source_match_id),
        ).fetchone()
        return None if row is None else str(row["match_id"])

    def find_match_id(
        self,
        *,
        competition_id: str,
        season_id: str,
        home_team_id: str,
        away_team_id: str,
        kickoff_utc: datetime,
    ) -> str | None:
        row = self._connection.execute(
            """
            SELECT match_id FROM matches
            WHERE competition_id = ? AND season_id = ?
              AND home_team_id = ? AND away_team_id = ? AND kickoff_utc = ?
            """,
            (
                competition_id,
                season_id,
                home_team_id,
                away_team_id,
                kickoff_utc.isoformat(),
            ),
        ).fetchone()
        return None if row is None else str(row["match_id"])

    def upsert_match(self, match: MatchRecord) -> None:
        existing = self.get_match(match.match_id)
        if existing is not None:
            _assert_identity_match(existing, match)
            _assert_score_compatible(existing, match)
        self._connection.execute(
            """
            INSERT INTO matches (
                match_id, competition_id, season_id, kickoff_utc, home_team_id,
                away_team_id, status, home_ft_goals, away_ft_goals, home_ht_goals,
                away_ht_goals, venue_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(match_id) DO UPDATE SET
                status = excluded.status,
                home_ft_goals = COALESCE(excluded.home_ft_goals, matches.home_ft_goals),
                away_ft_goals = COALESCE(excluded.away_ft_goals, matches.away_ft_goals),
                home_ht_goals = COALESCE(excluded.home_ht_goals, matches.home_ht_goals),
                away_ht_goals = COALESCE(excluded.away_ht_goals, matches.away_ht_goals),
                venue_name = COALESCE(excluded.venue_name, matches.venue_name)
            """,
            (
                match.match_id,
                match.competition_id,
                match.season_id,
                match.kickoff_utc.isoformat(),
                match.home_team_id,
                match.away_team_id,
                match.status.value,
                match.home_ft_goals,
                match.away_ft_goals,
                match.home_ht_goals,
                match.away_ht_goals,
                match.venue_name,
            ),
        )
        self._connection.commit()

    def upsert_team_stats(self, stats: TeamMatchStatsRecord) -> None:
        existing = self._connection.execute(
            """
            SELECT * FROM team_match_stats WHERE match_id = ? AND team_id = ?
            """,
            (stats.match_id, stats.team_id),
        ).fetchone()
        if existing is not None:
            for field in ("corners", "yellow_cards", "red_cards", "penalties"):
                previous = existing[field]
                incoming = getattr(stats, field)
                if previous is not None and incoming is not None and previous != incoming:
                    raise HistoricalConflictError(
                        f"Conflicting {field} for match {stats.match_id} team {stats.team_id}"
                    )
        self._connection.execute(
            """
            INSERT INTO team_match_stats (
                match_id, team_id, corners, yellow_cards, red_cards, penalties
            ) VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(match_id, team_id) DO UPDATE SET
                corners = COALESCE(excluded.corners, team_match_stats.corners),
                yellow_cards = COALESCE(excluded.yellow_cards, team_match_stats.yellow_cards),
                red_cards = COALESCE(excluded.red_cards, team_match_stats.red_cards),
                penalties = COALESCE(excluded.penalties, team_match_stats.penalties)
            """,
            (
                stats.match_id,
                stats.team_id,
                stats.corners,
                stats.yellow_cards,
                stats.red_cards,
                stats.penalties,
            ),
        )
        self._connection.commit()

    def upsert_event(self, event: MatchEventRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO match_events (
                event_id, match_id, event_type, minute, extra_minute, team_id,
                player_name, related_player_name, period, source_name, source_event_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_name, source_event_id) DO UPDATE SET
                match_id = excluded.match_id,
                event_type = excluded.event_type,
                minute = excluded.minute,
                extra_minute = excluded.extra_minute,
                team_id = excluded.team_id,
                player_name = excluded.player_name,
                related_player_name = excluded.related_player_name,
                period = excluded.period
            """,
            (
                event.event_id,
                event.match_id,
                event.event_type.value,
                event.minute,
                event.extra_minute,
                event.team_id,
                event.player_name,
                event.related_player_name,
                event.period,
                event.source_name,
                event.source_event_id,
            ),
        )
        self._connection.commit()

    def upsert_lineup(self, lineup: LineupRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO lineups (
                lineup_id, match_id, team_id, player_name, shirt_number,
                position, is_starter, source_name
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(match_id, team_id, player_name) DO UPDATE SET
                shirt_number = COALESCE(excluded.shirt_number, lineups.shirt_number),
                position = COALESCE(excluded.position, lineups.position),
                is_starter = excluded.is_starter,
                source_name = excluded.source_name
            """,
            (
                lineup.lineup_id,
                lineup.match_id,
                lineup.team_id,
                lineup.player_name,
                lineup.shirt_number,
                lineup.position,
                1 if lineup.is_starter else 0,
                lineup.source_name,
            ),
        )
        self._connection.commit()

    def upsert_source_record(self, match_id: str, provenance: SourceProvenance) -> None:
        source_record_id = f"{provenance.source_name}:{provenance.source_match_id}"
        self._connection.execute(
            """
            INSERT INTO source_records (
                source_record_id, source_name, source_match_id, match_id, source_url,
                retrieved_at, source_timestamp, raw_payload_hash, raw_payload_json,
                quality_flags_json, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_name, source_match_id) DO UPDATE SET
                match_id = excluded.match_id,
                source_url = excluded.source_url,
                retrieved_at = excluded.retrieved_at,
                source_timestamp = excluded.source_timestamp,
                raw_payload_hash = excluded.raw_payload_hash,
                raw_payload_json = excluded.raw_payload_json,
                quality_flags_json = excluded.quality_flags_json,
                confidence = excluded.confidence
            """,
            (
                source_record_id,
                provenance.source_name,
                provenance.source_match_id,
                match_id,
                provenance.source_url,
                provenance.retrieved_at.isoformat(),
                provenance.source_timestamp.isoformat() if provenance.source_timestamp else None,
                provenance.raw_payload_hash,
                json.dumps(provenance.raw_payload, sort_keys=True, separators=(",", ":")),
                json.dumps([flag.value for flag in provenance.quality_flags]),
                provenance.confidence,
            ),
        )
        self._connection.commit()

    def get_match(self, match_id: str) -> MatchRecord | None:
        row = self._connection.execute(
            """
            SELECT m.*,
                   home.canonical_name AS home_team_name,
                   away.canonical_name AS away_team_name,
                   c.name AS competition_name,
                   s.label AS season_label
            FROM matches m
            JOIN teams home ON home.team_id = m.home_team_id
            JOIN teams away ON away.team_id = m.away_team_id
            JOIN competitions c ON c.competition_id = m.competition_id
            JOIN seasons s ON s.season_id = m.season_id
            WHERE m.match_id = ?
            """,
            (match_id,),
        ).fetchone()
        return None if row is None else _match_from_row(row)

    def list_matches(
        self,
        *,
        competition_id: str | None = None,
        season_id: str | None = None,
        team_id: str | None = None,
        team_name: str | None = None,
    ) -> list[MatchRecord]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if competition_id is not None:
            clauses.append("m.competition_id = ?")
            parameters.append(competition_id)
        if season_id is not None:
            clauses.append("m.season_id = ?")
            parameters.append(season_id)
        if team_id is not None:
            clauses.append("(m.home_team_id = ? OR m.away_team_id = ?)")
            parameters.extend((team_id, team_id))
        if team_name is not None:
            clauses.append("(home.canonical_name = ? OR away.canonical_name = ?)")
            parameters.extend((team_name, team_name))
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"""
            SELECT m.*,
                   home.canonical_name AS home_team_name,
                   away.canonical_name AS away_team_name,
                   c.name AS competition_name,
                   s.label AS season_label
            FROM matches m
            JOIN teams home ON home.team_id = m.home_team_id
            JOIN teams away ON away.team_id = m.away_team_id
            JOIN competitions c ON c.competition_id = m.competition_id
            JOIN seasons s ON s.season_id = m.season_id
            {where}
            ORDER BY m.kickoff_utc ASC, m.match_id ASC
            """,
            parameters,
        ).fetchall()
        return [_match_from_row(row) for row in rows]

    def list_team_stats(
        self,
        *,
        match_id: str | None = None,
        team_id: str | None = None,
        competition_id: str | None = None,
        season_id: str | None = None,
    ) -> list[TeamMatchStatsRecord]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if match_id is not None:
            clauses.append("stats.match_id = ?")
            parameters.append(match_id)
        if team_id is not None:
            clauses.append("stats.team_id = ?")
            parameters.append(team_id)
        if competition_id is not None:
            clauses.append("m.competition_id = ?")
            parameters.append(competition_id)
        if season_id is not None:
            clauses.append("m.season_id = ?")
            parameters.append(season_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"""
            SELECT stats.*, t.canonical_name AS team_name,
                   m.competition_id, m.season_id,
                   CASE WHEN stats.team_id = m.home_team_id THEN 1 ELSE 0 END AS is_home
            FROM team_match_stats stats
            JOIN matches m ON m.match_id = stats.match_id
            JOIN teams t ON t.team_id = stats.team_id
            {where}
            ORDER BY stats.match_id ASC, is_home DESC, stats.team_id ASC
            """,
            parameters,
        ).fetchall()
        return [_stats_from_row(row) for row in rows]

    def list_events(
        self,
        *,
        match_id: str | None = None,
        event_type: MatchEventType | None = None,
        team_id: str | None = None,
    ) -> list[MatchEventRecord]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if match_id is not None:
            clauses.append("e.match_id = ?")
            parameters.append(match_id)
        if event_type is not None:
            clauses.append("e.event_type = ?")
            parameters.append(event_type.value)
        if team_id is not None:
            clauses.append("e.team_id = ?")
            parameters.append(team_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"""
            SELECT e.*, t.canonical_name AS team_name
            FROM match_events e
            LEFT JOIN teams t ON t.team_id = e.team_id
            {where}
            ORDER BY e.match_id ASC, IFNULL(e.minute, 999), e.event_type ASC, e.event_id ASC
            """,
            parameters,
        ).fetchall()
        return [_event_from_row(row) for row in rows]

    def list_lineups(self, *, match_id: str | None = None) -> list[LineupRecord]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if match_id is not None:
            clauses.append("l.match_id = ?")
            parameters.append(match_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"""
            SELECT l.*, t.canonical_name AS team_name
            FROM lineups l
            JOIN teams t ON t.team_id = l.team_id
            {where}
            ORDER BY l.match_id ASC, l.team_id ASC, l.is_starter DESC, l.player_name ASC
            """,
            parameters,
        ).fetchall()
        return [_lineup_from_row(row) for row in rows]

    def list_source_records(self, *, match_id: str | None = None) -> list[SourceProvenance]:
        clauses: list[str] = []
        parameters: list[Any] = []
        if match_id is not None:
            clauses.append("match_id = ?")
            parameters.append(match_id)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"""
            SELECT * FROM source_records
            {where}
            ORDER BY source_name ASC, source_match_id ASC
            """,
            parameters,
        ).fetchall()
        return [_source_from_row(row) for row in rows]

    def count_matches(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS n FROM matches").fetchone()
        return int(row["n"])

    def count_events(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS n FROM match_events").fetchone()
        return int(row["n"])

    def close(self) -> None:
        self._connection.close()


def _match_from_row(row: sqlite3.Row) -> MatchRecord:
    return MatchRecord(
        match_id=row["match_id"],
        competition_id=row["competition_id"],
        season_id=row["season_id"],
        kickoff_utc=datetime.fromisoformat(row["kickoff_utc"]),
        home_team_id=row["home_team_id"],
        away_team_id=row["away_team_id"],
        home_team_name=row["home_team_name"],
        away_team_name=row["away_team_name"],
        competition_name=row["competition_name"],
        season_label=row["season_label"],
        status=MatchStatus(row["status"]),
        home_ft_goals=row["home_ft_goals"],
        away_ft_goals=row["away_ft_goals"],
        home_ht_goals=row["home_ht_goals"],
        away_ht_goals=row["away_ht_goals"],
        venue_name=row["venue_name"],
    )


def _stats_from_row(row: sqlite3.Row) -> TeamMatchStatsRecord:
    return TeamMatchStatsRecord(
        match_id=row["match_id"],
        team_id=row["team_id"],
        team_name=row["team_name"],
        is_home=bool(row["is_home"]),
        competition_id=row["competition_id"],
        season_id=row["season_id"],
        corners=row["corners"],
        yellow_cards=row["yellow_cards"],
        red_cards=row["red_cards"],
        penalties=row["penalties"],
    )


def _event_from_row(row: sqlite3.Row) -> MatchEventRecord:
    return MatchEventRecord(
        event_id=row["event_id"],
        match_id=row["match_id"],
        event_type=MatchEventType(row["event_type"]),
        minute=row["minute"],
        extra_minute=row["extra_minute"],
        team_id=row["team_id"],
        team_name=row["team_name"],
        player_name=row["player_name"],
        related_player_name=row["related_player_name"],
        period=row["period"],
        source_name=row["source_name"],
        source_event_id=row["source_event_id"],
    )


def _lineup_from_row(row: sqlite3.Row) -> LineupRecord:
    return LineupRecord(
        lineup_id=row["lineup_id"],
        match_id=row["match_id"],
        team_id=row["team_id"],
        team_name=row["team_name"],
        player_name=row["player_name"],
        shirt_number=row["shirt_number"],
        position=row["position"],
        is_starter=bool(row["is_starter"]),
        source_name=row["source_name"],
    )


def _source_from_row(row: sqlite3.Row) -> SourceProvenance:
    return SourceProvenance(
        source_name=row["source_name"],
        source_match_id=row["source_match_id"],
        source_url=row["source_url"],
        retrieved_at=datetime.fromisoformat(row["retrieved_at"]),
        source_timestamp=(
            datetime.fromisoformat(row["source_timestamp"]) if row["source_timestamp"] else None
        ),
        raw_payload_hash=row["raw_payload_hash"],
        raw_payload=json.loads(row["raw_payload_json"]),
        quality_flags=[DataQualityFlag(flag) for flag in json.loads(row["quality_flags_json"])],
        confidence=float(row["confidence"]),
    )


def _assert_identity_match(existing: MatchRecord, incoming: MatchRecord) -> None:
    if (
        existing.competition_id != incoming.competition_id
        or existing.season_id != incoming.season_id
        or existing.home_team_id != incoming.home_team_id
        or existing.away_team_id != incoming.away_team_id
        or existing.kickoff_utc != incoming.kickoff_utc
    ):
        raise HistoricalConflictError(
            f"Match {match_id_label(existing, incoming)} identity conflict"
        )


def match_id_label(existing: MatchRecord, incoming: MatchRecord) -> str:
    return existing.match_id or incoming.match_id


def _assert_score_compatible(existing: MatchRecord, incoming: MatchRecord) -> None:
    pairs = (
        ("home_ft_goals", existing.home_ft_goals, incoming.home_ft_goals),
        ("away_ft_goals", existing.away_ft_goals, incoming.away_ft_goals),
        ("home_ht_goals", existing.home_ht_goals, incoming.home_ht_goals),
        ("away_ht_goals", existing.away_ht_goals, incoming.away_ht_goals),
    )
    for field, previous, new in pairs:
        if previous is not None and incoming_value_conflicts(previous, new):
            raise HistoricalConflictError(f"Conflicting {field} for match {existing.match_id}")


def incoming_value_conflicts(previous: int, new: int | None) -> bool:
    return new is not None and previous != new
