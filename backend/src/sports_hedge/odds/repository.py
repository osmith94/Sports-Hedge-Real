from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.domain.models import MarketSide
from sports_hedge.facts.identity import KickoffPrecision
from sports_hedge.odds.models import (
    CanonicalMatchFact,
    MappingException,
    OddsObservation,
    QualityTier,
    QuoteType,
    VenueKind,
)
from sports_hedge.odds.movement import settlement_key_without_line


_CURRENT_REVISIONS = """
candidates AS (
    SELECT *
    FROM odds_observations
    WHERE quote_type IN ('opening', 'closing')
),
ranked_revisions AS (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY
                   source,
                   IFNULL(source_market_id, ''),
                   IFNULL(source_reference, ''),
                   quote_type,
                   selection,
                   IFNULL(side, ''),
                   IFNULL(observed_at, '')
               ORDER BY retrieved_at DESC, observation_id DESC
           ) AS revision_rn
    FROM candidates
),
current_revisions AS (
    SELECT * FROM ranked_revisions WHERE revision_rn = 1
),
eligible AS (
    SELECT * FROM current_revisions
    WHERE decimal_odds IS NOT NULL
      AND semantics_complete = 1
      AND settlement_key IS NOT NULL
      AND settlement_key != ''
)
"""

_OPEN_CLOSE_CURRENT = f"""
{_CURRENT_REVISIONS},
ranked_pairs AS (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY
                   quote_type,
                   canonical_match_id,
                   market_family,
                   period,
                   IFNULL(line, ''),
                   settlement_key,
                   selection,
                   source,
                   IFNULL(bookmaker, ''),
                   IFNULL(venue, ''),
                   IFNULL(side, '')
               ORDER BY retrieved_at DESC, observation_id DESC
           ) AS pair_rn
    FROM eligible
),
current_quotes AS (
    SELECT * FROM ranked_pairs WHERE pair_rn = 1
)
"""

_OPEN_CLOSE_IDENTITY_CURRENT = f"""
{_CURRENT_REVISIONS},
ranked_identity AS (
    SELECT *,
           ROW_NUMBER() OVER (
               PARTITION BY
                   quote_type,
                   canonical_match_id,
                   market_family,
                   period,
                   selection,
                   source,
                   IFNULL(bookmaker, ''),
                   IFNULL(venue, ''),
                   IFNULL(side, ''),
                   settlement_key_without_line(settlement_key)
               ORDER BY retrieved_at DESC, observation_id DESC
           ) AS identity_rn
    FROM eligible
),
current_identity AS (
    SELECT * FROM ranked_identity WHERE identity_rn = 1
)
"""


class SqliteOddsRepository:
    """Normalized historical odds store. SQLite now; PostgreSQL later."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.create_function(
            "settlement_key_without_line",
            1,
            settlement_key_without_line,
        )
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS odds_match_index (
                canonical_match_id TEXT PRIMARY KEY,
                competition_id TEXT NOT NULL,
                season TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                kickoff_utc TEXT NOT NULL,
                kickoff_precision TEXT NOT NULL,
                source TEXT NOT NULL,
                source_match_id TEXT,
                retrieved_at TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS odds_observations (
                observation_id TEXT PRIMARY KEY,
                canonical_match_id TEXT NOT NULL,
                source TEXT NOT NULL,
                source_market_id TEXT,
                source_reference TEXT,
                venue TEXT,
                bookmaker TEXT,
                venue_kind TEXT NOT NULL,
                market_family TEXT NOT NULL,
                period TEXT NOT NULL,
                line TEXT,
                selection TEXT NOT NULL,
                side TEXT,
                decimal_odds TEXT,
                observed_at TEXT,
                quote_type TEXT NOT NULL,
                spread TEXT,
                liquidity TEXT,
                commission_known INTEGER NOT NULL,
                source_url TEXT,
                retrieved_at TEXT NOT NULL,
                raw_payload_hash TEXT,
                source_observation_key TEXT NOT NULL,
                quality_tier TEXT NOT NULL,
                confidence REAL NOT NULL,
                semantics_complete INTEGER NOT NULL,
                settlement_key TEXT,
                competition_id TEXT NOT NULL,
                season TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                kickoff_utc TEXT NOT NULL,
                kickoff_precision TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS mapping_exceptions (
                exception_id TEXT PRIMARY KEY,
                source TEXT NOT NULL,
                source_reference TEXT NOT NULL,
                reason TEXT NOT NULL,
                detail TEXT NOT NULL,
                field TEXT NOT NULL,
                retrieved_at TEXT NOT NULL,
                raw_payload_hash TEXT
            );

            CREATE TABLE IF NOT EXISTS ingestion_checkpoints (
                source TEXT PRIMARY KEY,
                cursor_value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_odds_match
                ON odds_observations(canonical_match_id, market_family, quote_type);
            CREATE INDEX IF NOT EXISTS idx_odds_coverage
                ON odds_observations(competition_id, season, source, market_family);
            CREATE INDEX IF NOT EXISTS idx_odds_source_key
                ON odds_observations(source_observation_key);
            CREATE INDEX IF NOT EXISTS idx_odds_movement_path
                ON odds_observations(canonical_match_id, selection, observed_at);
            CREATE INDEX IF NOT EXISTS idx_matches_universe
                ON odds_match_index(competition_id, season);
            """
        )
        self._connection.commit()

    def upsert_match(self, match: CanonicalMatchFact) -> bool:
        existing = self._connection.execute(
            "SELECT canonical_match_id FROM odds_match_index WHERE canonical_match_id = ?",
            (match.canonical_match_id,),
        ).fetchone()
        self._connection.execute(
            """
            INSERT INTO odds_match_index (
                canonical_match_id, competition_id, season, home_team, away_team,
                kickoff_utc, kickoff_precision, source, source_match_id,
                retrieved_at, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(canonical_match_id) DO UPDATE SET
                source_match_id = COALESCE(excluded.source_match_id, odds_match_index.source_match_id)
            """,
            (
                match.canonical_match_id,
                match.competition_code,
                match.season,
                match.home_team,
                match.away_team,
                match.kickoff_utc.isoformat(),
                match.kickoff_precision.value,
                match.source,
                match.source_match_id,
                match.retrieved_at.isoformat(),
                json.dumps(match.metadata),
            ),
        )
        self._connection.commit()
        return existing is None

    def insert_observation(self, observation: OddsObservation) -> bool:
        """Insert idempotently. Returns True when a new row was written."""

        existing = self._connection.execute(
            "SELECT observation_id FROM odds_observations WHERE observation_id = ?",
            (observation.observation_id,),
        ).fetchone()
        if existing is not None:
            return False
        self._connection.execute(
            """
            INSERT INTO odds_observations (
                observation_id, canonical_match_id, source, source_market_id, source_reference,
                venue, bookmaker, venue_kind, market_family, period, line, selection, side,
                decimal_odds, observed_at, quote_type, spread, liquidity, commission_known,
                source_url, retrieved_at, raw_payload_hash, source_observation_key, quality_tier,
                confidence, semantics_complete, settlement_key, competition_id, season,
                home_team, away_team, kickoff_utc, kickoff_precision, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            self._observation_row(observation),
        )
        self._connection.commit()
        return True

    def insert_exception(self, exception: MappingException) -> bool:
        existing = self._connection.execute(
            "SELECT exception_id FROM mapping_exceptions WHERE exception_id = ?",
            (exception.exception_id,),
        ).fetchone()
        if existing is not None:
            return False
        self._connection.execute(
            """
            INSERT INTO mapping_exceptions (
                exception_id, source, source_reference, reason, detail, field,
                retrieved_at, raw_payload_hash
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                exception.exception_id,
                exception.source,
                exception.source_reference,
                exception.reason,
                exception.detail,
                exception.field,
                exception.retrieved_at.isoformat(),
                exception.raw_payload_hash,
            ),
        )
        self._connection.commit()
        return True

    def set_checkpoint(self, source: str, cursor_value: str, updated_at: datetime) -> None:
        self._connection.execute(
            """
            INSERT INTO ingestion_checkpoints (source, cursor_value, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(source) DO UPDATE SET
                cursor_value = excluded.cursor_value,
                updated_at = excluded.updated_at
            """,
            (source, cursor_value, updated_at.isoformat()),
        )
        self._connection.commit()

    def get_checkpoint(self, source: str) -> str | None:
        row = self._connection.execute(
            "SELECT cursor_value FROM ingestion_checkpoints WHERE source = ?",
            (source,),
        ).fetchone()
        return None if row is None else str(row["cursor_value"])

    def list_observations(self) -> list[OddsObservation]:
        rows = self._connection.execute(
            "SELECT * FROM odds_observations ORDER BY retrieved_at, observation_id"
        ).fetchall()
        return [self._observation_from_row(row) for row in rows]

    def list_matches(self) -> list[CanonicalMatchFact]:
        rows = self._connection.execute(
            "SELECT * FROM odds_match_index ORDER BY kickoff_utc, canonical_match_id"
        ).fetchall()
        return [self._match_from_row(row) for row in rows]

    def list_exceptions(self) -> list[MappingException]:
        rows = self._connection.execute(
            "SELECT * FROM mapping_exceptions ORDER BY retrieved_at, exception_id"
        ).fetchall()
        return [self._exception_from_row(row) for row in rows]

    def close(self) -> None:
        self._connection.close()

    def count_matches(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS n FROM odds_match_index").fetchone()
        return int(row["n"]) if row is not None else 0

    def count_observations(self) -> int:
        row = self._connection.execute("SELECT COUNT(*) AS n FROM odds_observations").fetchone()
        return int(row["n"]) if row is not None else 0

    def count_same_line_opening_closing_pairs(self) -> int:
        """Count current opening→closing price pairs using classify_open_close semantics.

        Current revision per stable source identity is selected before line pairing.
        """

        row = self._connection.execute(
            f"""
            WITH {_OPEN_CLOSE_CURRENT}
            SELECT COUNT(*) AS n
            FROM current_quotes o
            JOIN current_quotes c
              ON o.quote_type = 'opening'
             AND c.quote_type = 'closing'
             AND o.canonical_match_id = c.canonical_match_id
             AND o.market_family = c.market_family
             AND o.period = c.period
             AND IFNULL(o.line, '') = IFNULL(c.line, '')
             AND o.settlement_key = c.settlement_key
             AND o.selection = c.selection
             AND o.source = c.source
             AND IFNULL(o.bookmaker, '') = IFNULL(c.bookmaker, '')
             AND IFNULL(o.venue, '') = IFNULL(c.venue, '')
             AND IFNULL(o.side, '') = IFNULL(c.side, '')
            """
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    def count_asian_handicap_line_shifts(self) -> int:
        """Count current AH structural line changes using classify_open_close semantics.

        Current revision per stable source identity is selected before line comparison.
        """

        row = self._connection.execute(
            f"""
            WITH {_OPEN_CLOSE_IDENTITY_CURRENT}
            SELECT COUNT(*) AS n
            FROM current_identity o
            JOIN current_identity c
              ON o.quote_type = 'opening'
             AND c.quote_type = 'closing'
             AND o.market_family = 'asian_handicap'
             AND c.market_family = 'asian_handicap'
             AND o.canonical_match_id = c.canonical_match_id
             AND o.period = c.period
             AND o.selection = c.selection
             AND o.source = c.source
             AND IFNULL(o.bookmaker, '') = IFNULL(c.bookmaker, '')
             AND IFNULL(o.venue, '') = IFNULL(c.venue, '')
             AND IFNULL(o.side, '') = IFNULL(c.side, '')
             AND settlement_key_without_line(o.settlement_key)
               = settlement_key_without_line(c.settlement_key)
             AND IFNULL(o.line, '') != IFNULL(c.line, '')
            """
        ).fetchone()
        return int(row["n"]) if row is not None else 0

    def _observation_row(self, observation: OddsObservation) -> tuple[Any, ...]:
        return (
            observation.observation_id,
            observation.canonical_match_id,
            observation.source,
            observation.source_market_id,
            observation.source_reference,
            observation.venue,
            observation.bookmaker,
            observation.venue_kind.value,
            observation.market_family.value,
            observation.period.value,
            None if observation.line is None else format(observation.line, "f"),
            observation.selection,
            None if observation.side is None else observation.side.value,
            None if observation.decimal_odds is None else format(observation.decimal_odds, "f"),
            None if observation.observed_at is None else observation.observed_at.isoformat(),
            observation.quote_type.value,
            None if observation.spread is None else format(observation.spread, "f"),
            None if observation.liquidity is None else format(observation.liquidity, "f"),
            int(observation.commission_known),
            observation.source_url,
            observation.retrieved_at.isoformat(),
            observation.raw_payload_hash,
            observation.source_observation_key,
            observation.quality_tier.value,
            observation.confidence,
            int(observation.semantics_complete),
            observation.settlement_key,
            observation.competition_code,
            observation.season,
            observation.home_team,
            observation.away_team,
            observation.kickoff_utc.isoformat(),
            observation.kickoff_precision.value,
            json.dumps(observation.metadata),
        )

    def _observation_from_row(self, row: sqlite3.Row) -> OddsObservation:
        return OddsObservation(
            observation_id=row["observation_id"],
            canonical_match_id=row["canonical_match_id"],
            source=row["source"],
            source_market_id=row["source_market_id"],
            source_reference=row["source_reference"],
            venue=row["venue"],
            bookmaker=row["bookmaker"],
            venue_kind=VenueKind(row["venue_kind"]),
            market_family=MarketFamily(row["market_family"]),
            period=FootballPeriod(row["period"]),
            line=None if row["line"] is None else Decimal(row["line"]),
            selection=row["selection"],
            side=None if row["side"] is None else MarketSide(row["side"]),
            decimal_odds=None if row["decimal_odds"] is None else Decimal(row["decimal_odds"]),
            observed_at=None if row["observed_at"] is None else datetime.fromisoformat(row["observed_at"]),
            quote_type=QuoteType(row["quote_type"]),
            spread=None if row["spread"] is None else Decimal(row["spread"]),
            liquidity=None if row["liquidity"] is None else Decimal(row["liquidity"]),
            commission_known=bool(row["commission_known"]),
            source_url=row["source_url"],
            retrieved_at=datetime.fromisoformat(row["retrieved_at"]),
            raw_payload_hash=row["raw_payload_hash"],
            source_observation_key=row["source_observation_key"],
            quality_tier=QualityTier(row["quality_tier"]),
            confidence=row["confidence"],
            semantics_complete=bool(row["semantics_complete"]),
            settlement_key=row["settlement_key"],
            competition_code=row["competition_id"],
            season=row["season"],
            home_team=row["home_team"],
            away_team=row["away_team"],
            kickoff_utc=datetime.fromisoformat(row["kickoff_utc"]),
            kickoff_precision=KickoffPrecision(row["kickoff_precision"]),
            metadata=json.loads(row["metadata_json"]),
        )

    def _match_from_row(self, row: sqlite3.Row) -> CanonicalMatchFact:
        return CanonicalMatchFact(
            canonical_match_id=row["canonical_match_id"],
            competition_code=row["competition_id"],
            season=row["season"],
            home_team=row["home_team"],
            away_team=row["away_team"],
            kickoff_utc=datetime.fromisoformat(row["kickoff_utc"]),
            kickoff_precision=KickoffPrecision(row["kickoff_precision"]),
            source=row["source"],
            source_match_id=row["source_match_id"],
            retrieved_at=datetime.fromisoformat(row["retrieved_at"]),
            metadata=json.loads(row["metadata_json"]),
        )

    def _exception_from_row(self, row: sqlite3.Row) -> MappingException:
        return MappingException(
            exception_id=row["exception_id"],
            source=row["source"],
            source_reference=row["source_reference"],
            reason=row["reason"],
            detail=row["detail"],
            field=row["field"],
            retrieved_at=datetime.fromisoformat(row["retrieved_at"]),
            raw_payload_hash=row["raw_payload_hash"],
        )
