from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.audit import PaperScanRecord, PaperScanSummary


class SqlitePaperScanRepository:
    """Append-only paper scan audit/read store."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS paper_scan_records (
                record_id TEXT PRIMARY KEY,
                scanned_at TEXT NOT NULL,
                canonical_event_id TEXT NOT NULL,
                canonical_market_id TEXT NOT NULL,
                competition TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                kickoff_utc TEXT NOT NULL,
                market_family TEXT NOT NULL,
                period TEXT NOT NULL,
                line TEXT,
                venues_json TEXT NOT NULL,
                source_market_ids_json TEXT NOT NULL,
                mapping_confidence REAL NOT NULL,
                is_arbitrage INTEGER NOT NULL,
                eligible_for_paper_simulation INTEGER NOT NULL,
                gross_edge TEXT,
                net_edge TEXT,
                executable_stake_gbp TEXT,
                guaranteed_profit_gbp TEXT,
                execution_risk_score INTEGER,
                execution_risk_band TEXT,
                rejection_reasons_json TEXT NOT NULL,
                decision_json TEXT NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_paper_scan_time
                ON paper_scan_records(scanned_at DESC);
            CREATE INDEX IF NOT EXISTS idx_paper_scan_eligible_time
                ON paper_scan_records(eligible_for_paper_simulation, scanned_at DESC);
            CREATE INDEX IF NOT EXISTS idx_paper_scan_event_time
                ON paper_scan_records(canonical_event_id, scanned_at DESC);
            """
        )
        self._connection.commit()

    def append_scan(self, record: PaperScanRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO paper_scan_records (
                record_id, scanned_at, canonical_event_id, canonical_market_id,
                competition, home_team, away_team, kickoff_utc, market_family,
                period, line, venues_json, source_market_ids_json,
                mapping_confidence, is_arbitrage, eligible_for_paper_simulation,
                gross_edge, net_edge, executable_stake_gbp, guaranteed_profit_gbp,
                execution_risk_score, execution_risk_band, rejection_reasons_json,
                decision_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.record_id,
                record.scanned_at.isoformat(),
                record.canonical_event_id,
                record.canonical_market_id,
                record.competition,
                record.home_team,
                record.away_team,
                record.kickoff_utc.isoformat(),
                record.market_family.value,
                record.period.value,
                _stringify_decimal(record.line),
                json.dumps([venue.value for venue in record.venues]),
                json.dumps(record.source_market_ids),
                record.mapping_confidence,
                int(record.is_arbitrage),
                int(record.eligible_for_paper_simulation),
                _stringify_decimal(record.gross_edge),
                _stringify_decimal(record.net_edge),
                _stringify_decimal(record.executable_stake_gbp),
                _stringify_decimal(record.guaranteed_profit_gbp),
                record.execution_risk_score,
                record.execution_risk_band,
                json.dumps(record.rejection_reasons),
                record.decision_json,
            ),
        )
        self._connection.commit()

    def list_scans(
        self,
        *,
        limit: int = 100,
        eligible_only: bool = False,
        arbitrage_only: bool = False,
        since: datetime | None = None,
    ) -> list[PaperScanRecord]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        clauses: list[str] = []
        parameters: list[Any] = []
        if eligible_only:
            clauses.append("eligible_for_paper_simulation = 1")
        if arbitrage_only:
            clauses.append("is_arbitrage = 1")
        if since is not None:
            clauses.append("scanned_at >= ?")
            parameters.append(since.isoformat())
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        rows = self._connection.execute(
            f"SELECT * FROM paper_scan_records{where} ORDER BY scanned_at DESC LIMIT ?",  # noqa: S608
            parameters,
        ).fetchall()
        return [_record_from_row(row) for row in rows]

    def summary(self, *, since: datetime) -> PaperScanSummary:
        rows = self._connection.execute(
            "SELECT * FROM paper_scan_records WHERE scanned_at >= ? ORDER BY scanned_at DESC",
            (since.isoformat(),),
        ).fetchall()
        records = [_record_from_row(row) for row in rows]
        net_edges = [record.net_edge for record in records if record.net_edge is not None]
        profits = [
            record.guaranteed_profit_gbp
            for record in records
            if record.guaranteed_profit_gbp is not None
        ]
        return PaperScanSummary(
            since=since,
            scan_count=len(records),
            arbitrage_count=sum(record.is_arbitrage for record in records),
            eligible_count=sum(record.eligible_for_paper_simulation for record in records),
            rejection_count=sum(bool(record.rejection_reasons) for record in records),
            top_net_edge=max(net_edges) if net_edges else None,
            top_guaranteed_profit_gbp=max(profits) if profits else None,
            latest_scan_at=records[0].scanned_at if records else None,
        )

    def close(self) -> None:
        self._connection.close()


def _record_from_row(row: sqlite3.Row) -> PaperScanRecord:
    return PaperScanRecord(
        record_id=row["record_id"],
        scanned_at=datetime.fromisoformat(row["scanned_at"]),
        canonical_event_id=row["canonical_event_id"],
        canonical_market_id=row["canonical_market_id"],
        competition=row["competition"],
        home_team=row["home_team"],
        away_team=row["away_team"],
        kickoff_utc=datetime.fromisoformat(row["kickoff_utc"]),
        market_family=MarketFamily(row["market_family"]),
        period=FootballPeriod(row["period"]),
        line=_decimal(row["line"]),
        venues=[VenueName(value) for value in json.loads(row["venues_json"])],
        source_market_ids=list(json.loads(row["source_market_ids_json"])),
        mapping_confidence=float(row["mapping_confidence"]),
        is_arbitrage=bool(row["is_arbitrage"]),
        eligible_for_paper_simulation=bool(row["eligible_for_paper_simulation"]),
        gross_edge=_decimal(row["gross_edge"]),
        net_edge=_decimal(row["net_edge"]),
        executable_stake_gbp=_decimal(row["executable_stake_gbp"]),
        guaranteed_profit_gbp=_decimal(row["guaranteed_profit_gbp"]),
        execution_risk_score=row["execution_risk_score"],
        execution_risk_band=row["execution_risk_band"],
        rejection_reasons=list(json.loads(row["rejection_reasons_json"])),
        decision_json=row["decision_json"],
    )


def _stringify_decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)
