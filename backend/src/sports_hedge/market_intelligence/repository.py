from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sports_hedge.domain.football import (
    FootballPeriod,
    MarketFamily,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.models import (
    AnnotationCategory,
    MarketEventAnnotation,
    MarketSnapshot,
)


class SqliteMarketIntelligenceRepository:
    """Append-only time-series store for Phase 1 research."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS market_snapshots (
                snapshot_id TEXT PRIMARY KEY,
                observed_at TEXT NOT NULL,
                venue TEXT NOT NULL,
                canonical_event_id TEXT NOT NULL,
                canonical_market_id TEXT NOT NULL,
                canonical_outcome TEXT NOT NULL,
                market_family TEXT NOT NULL,
                period TEXT NOT NULL DEFAULT 'unknown',
                market_line TEXT,
                settlement_scope TEXT NOT NULL DEFAULT 'unknown',
                settlement_key TEXT,
                competition TEXT,
                home_team TEXT,
                away_team TEXT,
                source_event_id TEXT,
                source_market_id TEXT,
                source_outcome_id TEXT,
                kickoff_utc TEXT,
                decimal_odds TEXT NOT NULL,
                implied_probability TEXT NOT NULL,
                best_back_odds TEXT,
                best_lay_odds TEXT,
                back_size TEXT,
                lay_size TEXT,
                spread_decimal TEXT,
                total_liquidity TEXT,
                source_latency_ms INTEGER,
                order_book_json TEXT NOT NULL,
                metadata_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_snapshot_market_time
                ON market_snapshots(
                    canonical_event_id,
                    canonical_market_id,
                    canonical_outcome,
                    observed_at
                );
            CREATE INDEX IF NOT EXISTS idx_snapshot_family_time
                ON market_snapshots(market_family, observed_at);
            CREATE INDEX IF NOT EXISTS idx_snapshot_team_time
                ON market_snapshots(home_team, away_team, observed_at);
            CREATE INDEX IF NOT EXISTS idx_snapshot_competition_time
                ON market_snapshots(competition, observed_at);

            CREATE TABLE IF NOT EXISTS market_event_annotations (
                annotation_id TEXT PRIMARY KEY,
                canonical_event_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                category TEXT NOT NULL,
                source TEXT NOT NULL,
                confidence REAL NOT NULL,
                title TEXT NOT NULL,
                source_url TEXT,
                metadata_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_annotation_event_time
                ON market_event_annotations(canonical_event_id, occurred_at);
            CREATE INDEX IF NOT EXISTS idx_annotation_category_time
                ON market_event_annotations(category, occurred_at);
            """
        )
        self._ensure_snapshot_columns()
        self._connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_snapshot_dimensions_time
                ON market_snapshots(market_family, period, market_line, settlement_scope, observed_at);
            """
        )
        self._connection.commit()

    def _ensure_snapshot_columns(self) -> None:
        existing = {
            row["name"]
            for row in self._connection.execute("PRAGMA table_info(market_snapshots)").fetchall()
        }
        migrations = {
            "period": "ALTER TABLE market_snapshots ADD COLUMN period TEXT NOT NULL DEFAULT 'unknown'",
            "market_line": "ALTER TABLE market_snapshots ADD COLUMN market_line TEXT",
            "settlement_scope": (
                "ALTER TABLE market_snapshots ADD COLUMN settlement_scope "
                "TEXT NOT NULL DEFAULT 'unknown'"
            ),
            "settlement_key": "ALTER TABLE market_snapshots ADD COLUMN settlement_key TEXT",
        }
        for column, sql in migrations.items():
            if column not in existing:
                self._connection.execute(sql)

    def append_snapshot(self, snapshot: MarketSnapshot) -> None:
        self._connection.execute(
            """
            INSERT INTO market_snapshots (
                snapshot_id, observed_at, venue, canonical_event_id, canonical_market_id,
                canonical_outcome, market_family, period, market_line, settlement_scope,
                settlement_key, competition, home_team, away_team, source_event_id,
                source_market_id, source_outcome_id, kickoff_utc, decimal_odds,
                implied_probability, best_back_odds, best_lay_odds, back_size, lay_size,
                spread_decimal, total_liquidity, source_latency_ms, order_book_json,
                metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                snapshot.snapshot_id,
                snapshot.observed_at.isoformat(),
                snapshot.venue.value,
                snapshot.canonical_event_id,
                snapshot.canonical_market_id,
                snapshot.canonical_outcome,
                snapshot.market_family.value,
                snapshot.period.value,
                _stringify_decimal(snapshot.market_line),
                snapshot.settlement_scope.value,
                snapshot.settlement_key,
                snapshot.competition,
                snapshot.home_team,
                snapshot.away_team,
                snapshot.source_event_id,
                snapshot.source_market_id,
                snapshot.source_outcome_id,
                snapshot.kickoff_utc.isoformat() if snapshot.kickoff_utc else None,
                str(snapshot.decimal_odds),
                str(snapshot.implied_probability),
                _stringify_decimal(snapshot.best_back_odds),
                _stringify_decimal(snapshot.best_lay_odds),
                _stringify_decimal(snapshot.back_size),
                _stringify_decimal(snapshot.lay_size),
                _stringify_decimal(snapshot.spread_decimal),
                _stringify_decimal(snapshot.total_liquidity),
                snapshot.source_latency_ms,
                json.dumps(snapshot.order_book, default=str, separators=(",", ":")),
                json.dumps(snapshot.metadata, default=str, separators=(",", ":")),
            ),
        )
        self._connection.commit()

    def append_annotation(self, annotation: MarketEventAnnotation) -> None:
        self._connection.execute(
            """
            INSERT INTO market_event_annotations (
                annotation_id, canonical_event_id, occurred_at, category, source,
                confidence, title, source_url, metadata_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                annotation.annotation_id,
                annotation.canonical_event_id,
                annotation.occurred_at.isoformat(),
                annotation.category.value,
                annotation.source,
                annotation.confidence,
                annotation.title,
                annotation.source_url,
                json.dumps(annotation.metadata, default=str, separators=(",", ":")),
            ),
        )
        self._connection.commit()

    def list_snapshots(
        self,
        *,
        canonical_event_id: str | None = None,
        canonical_market_id: str | None = None,
        canonical_outcome: str | None = None,
        venue: VenueName | None = None,
        market_family: MarketFamily | None = None,
        period: FootballPeriod | None = None,
        market_line: Decimal | None = None,
        settlement_scope: SettlementScope | None = None,
        competition: str | None = None,
        team: str | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> list[MarketSnapshot]:
        clauses: list[str] = []
        parameters: list[Any] = []

        def add_clause(sql: str, value: Any) -> None:
            clauses.append(sql)
            parameters.append(value)

        if canonical_event_id is not None:
            add_clause("canonical_event_id = ?", canonical_event_id)
        if canonical_market_id is not None:
            add_clause("canonical_market_id = ?", canonical_market_id)
        if canonical_outcome is not None:
            add_clause("canonical_outcome = ?", canonical_outcome)
        if venue is not None:
            add_clause("venue = ?", venue.value)
        if market_family is not None:
            add_clause("market_family = ?", market_family.value)
        if period is not None:
            add_clause("period = ?", period.value)
        if market_line is not None:
            add_clause("market_line = ?", str(market_line))
        if settlement_scope is not None:
            add_clause("settlement_scope = ?", settlement_scope.value)
        if competition is not None:
            add_clause("competition = ?", competition)
        if team is not None:
            clauses.append("(home_team = ? OR away_team = ?)")
            parameters.extend((team, team))
        if start_at is not None:
            add_clause("observed_at >= ?", start_at.isoformat())
        if end_at is not None:
            add_clause("observed_at <= ?", end_at.isoformat())

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM market_snapshots{where} ORDER BY observed_at ASC",  # noqa: S608
            parameters,
        ).fetchall()
        return [_snapshot_from_row(row) for row in rows]

    def list_annotations(
        self,
        *,
        canonical_event_id: str | None = None,
        category: AnnotationCategory | None = None,
        start_at: datetime | None = None,
        end_at: datetime | None = None,
    ) -> list[MarketEventAnnotation]:
        clauses: list[str] = []
        parameters: list[Any] = []

        if canonical_event_id is not None:
            clauses.append("canonical_event_id = ?")
            parameters.append(canonical_event_id)
        if category is not None:
            clauses.append("category = ?")
            parameters.append(category.value)
        if start_at is not None:
            clauses.append("occurred_at >= ?")
            parameters.append(start_at.isoformat())
        if end_at is not None:
            clauses.append("occurred_at <= ?")
            parameters.append(end_at.isoformat())

        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self._connection.execute(
            f"SELECT * FROM market_event_annotations{where} ORDER BY occurred_at ASC",  # noqa: S608
            parameters,
        ).fetchall()
        return [_annotation_from_row(row) for row in rows]

    def get_annotation(self, annotation_id: str) -> MarketEventAnnotation | None:
        row = self._connection.execute(
            "SELECT * FROM market_event_annotations WHERE annotation_id = ?",
            (annotation_id,),
        ).fetchone()
        if row is None:
            return None
        return _annotation_from_row(row)

    def close(self) -> None:
        self._connection.close()


def _stringify_decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _snapshot_from_row(row: sqlite3.Row) -> MarketSnapshot:
    return MarketSnapshot(
        snapshot_id=row["snapshot_id"],
        observed_at=datetime.fromisoformat(row["observed_at"]),
        venue=VenueName(row["venue"]),
        canonical_event_id=row["canonical_event_id"],
        canonical_market_id=row["canonical_market_id"],
        canonical_outcome=row["canonical_outcome"],
        market_family=MarketFamily(row["market_family"]),
        period=FootballPeriod(row["period"]),
        market_line=_decimal(row["market_line"]),
        settlement_scope=SettlementScope(row["settlement_scope"]),
        settlement_key=row["settlement_key"],
        competition=row["competition"],
        home_team=row["home_team"],
        away_team=row["away_team"],
        source_event_id=row["source_event_id"],
        source_market_id=row["source_market_id"],
        source_outcome_id=row["source_outcome_id"],
        kickoff_utc=datetime.fromisoformat(row["kickoff_utc"]) if row["kickoff_utc"] else None,
        decimal_odds=Decimal(row["decimal_odds"]),
        implied_probability=Decimal(row["implied_probability"]),
        best_back_odds=_decimal(row["best_back_odds"]),
        best_lay_odds=_decimal(row["best_lay_odds"]),
        back_size=_decimal(row["back_size"]),
        lay_size=_decimal(row["lay_size"]),
        spread_decimal=_decimal(row["spread_decimal"]),
        total_liquidity=_decimal(row["total_liquidity"]),
        source_latency_ms=row["source_latency_ms"],
        order_book=json.loads(row["order_book_json"]),
        metadata=json.loads(row["metadata_json"]),
    )


def _annotation_from_row(row: sqlite3.Row) -> MarketEventAnnotation:
    return MarketEventAnnotation(
        annotation_id=row["annotation_id"],
        canonical_event_id=row["canonical_event_id"],
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        category=AnnotationCategory(row["category"]),
        source=row["source"],
        confidence=float(row["confidence"]),
        title=row["title"],
        source_url=row["source_url"],
        metadata=json.loads(row["metadata_json"]),
    )
