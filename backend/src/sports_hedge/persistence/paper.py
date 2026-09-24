from __future__ import annotations

import json
import logging
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

from pydantic import ValidationError

from sports_hedge.application.cycle_diagnostics import (
    DIAGNOSTIC_MAX_ROWS,
    DIAGNOSTIC_RETENTION_DAYS,
)
from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.audit import (
    PaperScanCycleRecord,
    PaperScanReadIssue,
    PaperScanRecord,
    PaperScanSummary,
)

LOGGER = logging.getLogger(__name__)

PAPER_SCAN_SCHEMA_VERSION = 1

_CREATE_PAPER_SCAN_CYCLES_SQL = """
CREATE TABLE IF NOT EXISTS paper_scan_cycles (
    cycle_id TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    completed_at TEXT NOT NULL,
    scan_lane TEXT NOT NULL,
    duration_ms INTEGER NOT NULL,
    fixture_count INTEGER NOT NULL,
    evaluated_count INTEGER NOT NULL,
    not_evaluated_count INTEGER NOT NULL,
    matched_event_pairs INTEGER NOT NULL,
    matched_market_pairs INTEGER NOT NULL,
    paper_decision_count INTEGER NOT NULL,
    qualifying_arb_count INTEGER NOT NULL,
    venue_health_json TEXT NOT NULL,
    degraded INTEGER NOT NULL,
    last_error TEXT,
    universe_generation_id INTEGER,
    resume_cursor TEXT,
    completeness TEXT,
    generation_resume INTEGER,
    generation_work_used_s TEXT,
    operator_summary TEXT
);
"""

_CREATE_PAPER_SCAN_RECORDS_SQL = """
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
"""

# Additive-only types for ALTER TABLE. Never NOT NULL without a default: legacy rows
# must remain readable as raw history even when a newly added column is NULL.
_PAPER_SCAN_COLUMN_TYPES: tuple[tuple[str, str], ...] = (
    ("record_id", "TEXT"),
    ("scanned_at", "TEXT"),
    ("canonical_event_id", "TEXT"),
    ("canonical_market_id", "TEXT"),
    ("competition", "TEXT"),
    ("home_team", "TEXT"),
    ("away_team", "TEXT"),
    ("kickoff_utc", "TEXT"),
    ("market_family", "TEXT"),
    ("period", "TEXT"),
    ("line", "TEXT"),
    ("venues_json", "TEXT"),
    ("source_market_ids_json", "TEXT"),
    ("mapping_confidence", "REAL"),
    ("is_arbitrage", "INTEGER"),
    ("eligible_for_paper_simulation", "INTEGER"),
    ("gross_edge", "TEXT"),
    ("net_edge", "TEXT"),
    ("executable_stake_gbp", "TEXT"),
    ("guaranteed_profit_gbp", "TEXT"),
    ("execution_risk_score", "INTEGER"),
    ("execution_risk_band", "TEXT"),
    ("rejection_reasons_json", "TEXT"),
    ("decision_json", "TEXT"),
)

_ROW_DECODE_ERRORS = (
    IndexError,
    KeyError,
    TypeError,
    ValueError,
    AttributeError,
    ArithmeticError,
    json.JSONDecodeError,
    ValidationError,
)


class SqlitePaperScanRepository:
    """Append-only paper scan audit/read store.

    File-backed databases use a connection per operation so FastAPI worker threads
    never share a sqlite3 Connection. In-memory databases keep one connection alive
    (required for ``:memory:``) and serialize access with a lock. Scan rows are
    never deleted; unreadable legacy rows are skipped with diagnostics.
    """

    def __init__(self, database: str | Path = ":memory:") -> None:
        self._database = str(database)
        self._lock = threading.RLock()
        self._shared_connection: sqlite3.Connection | None = None
        if not self._is_file_backed():
            self._shared_connection = self._open_connection(shared=True)
        with self._connect() as connection:
            self._ensure_schema(connection)

    def _is_file_backed(self) -> bool:
        lowered = self._database.lower()
        return self._database != ":memory:" and "mode=memory" not in lowered

    def _open_connection(self, *, shared: bool) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database,
            timeout=30.0,
            check_same_thread=not shared,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout=30000")
        if self._is_file_backed():
            connection.execute("PRAGMA journal_mode=WAL").fetchone()
        return connection

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            shared = self._shared_connection
            if shared is not None:
                try:
                    yield shared
                    shared.commit()
                except Exception:
                    shared.rollback()
                    raise
                return
            connection = self._open_connection(shared=False)
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise
            finally:
                connection.close()

    def _ensure_schema(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            f"""
            {_CREATE_PAPER_SCAN_RECORDS_SQL}
            {_CREATE_PAPER_SCAN_CYCLES_SQL}
            CREATE TABLE IF NOT EXISTS paper_scan_meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            );
            """
        )
        existing = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(paper_scan_records)").fetchall()
        }
        for name, sql_type in _PAPER_SCAN_COLUMN_TYPES:
            if name not in existing:
                connection.execute(
                    f"ALTER TABLE paper_scan_records ADD COLUMN {name} {sql_type}"  # noqa: S608
                )
        connection.executescript(
            """
            CREATE INDEX IF NOT EXISTS idx_paper_scan_time
                ON paper_scan_records(scanned_at DESC);
            CREATE INDEX IF NOT EXISTS idx_paper_scan_eligible_time
                ON paper_scan_records(eligible_for_paper_simulation, scanned_at DESC);
            CREATE INDEX IF NOT EXISTS idx_paper_scan_event_time
                ON paper_scan_records(canonical_event_id, scanned_at DESC);
            CREATE INDEX IF NOT EXISTS idx_paper_scan_cycle_completed
                ON paper_scan_cycles(completed_at DESC, started_at DESC);
            CREATE TABLE IF NOT EXISTS paper_scan_cycle_diagnostics (
                cycle_id TEXT PRIMARY KEY,
                scan_lane TEXT NOT NULL,
                completed_at TEXT NOT NULL,
                report_json TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_paper_scan_cycle_diagnostics_completed
                ON paper_scan_cycle_diagnostics(completed_at);
            """
        )
        current_row = connection.execute(
            "SELECT value FROM paper_scan_meta WHERE key = 'schema_version'"
        ).fetchone()
        current = int(current_row["value"]) if current_row is not None else 0
        if current < PAPER_SCAN_SCHEMA_VERSION:
            connection.execute(
                """
                INSERT INTO paper_scan_meta(key, value)
                VALUES ('schema_version', ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (str(PAPER_SCAN_SCHEMA_VERSION),),
            )

    def schema_version(self) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM paper_scan_meta WHERE key = 'schema_version'"
            ).fetchone()
        if row is None:
            return 0
        return int(row["value"])

    def append_scan(self, record: PaperScanRecord) -> None:
        try:
            with self._connect() as connection:
                connection.execute(
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
        except sqlite3.IntegrityError:
            if self._same_stamped_persist_attempt(record):
                LOGGER.info(
                    "paper_scan_audit_same_persist_attempt record_id=%s",
                    record.record_id,
                )
                return
            raise

    def _same_stamped_persist_attempt(self, record: PaperScanRecord) -> bool:
        """True only when the existing PK row is this stamped persist attempt."""

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT record_id, canonical_event_id, canonical_market_id, scanned_at
                FROM paper_scan_records
                WHERE record_id = ?
                """,
                (record.record_id,),
            ).fetchone()
        if row is None:
            return False
        return (
            str(row["record_id"]) == record.record_id
            and str(row["canonical_event_id"]) == record.canonical_event_id
            and str(row["canonical_market_id"]) == record.canonical_market_id
            and str(row["scanned_at"]) == record.scanned_at.isoformat()
        )

    def append_cycle(self, record: PaperScanCycleRecord) -> None:
        """Insert one completed scan-cycle row. Retry of the same cycle is a no-op."""

        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO paper_scan_cycles (
                        cycle_id, started_at, completed_at, scan_lane, duration_ms,
                        fixture_count, evaluated_count, not_evaluated_count,
                        matched_event_pairs, matched_market_pairs,
                        paper_decision_count, qualifying_arb_count,
                        venue_health_json, degraded, last_error,
                        universe_generation_id, resume_cursor, completeness,
                        generation_resume, generation_work_used_s, operator_summary
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        record.cycle_id,
                        record.started_at.isoformat(),
                        record.completed_at.isoformat(),
                        record.scan_lane,
                        record.duration_ms,
                        record.fixture_count,
                        record.evaluated_count,
                        record.not_evaluated_count,
                        record.matched_event_pairs,
                        record.matched_market_pairs,
                        record.paper_decision_count,
                        record.qualifying_arb_count,
                        json.dumps(record.venue_health),
                        int(record.degraded),
                        record.last_error,
                        record.universe_generation_id,
                        record.resume_cursor,
                        record.completeness,
                        None if record.generation_resume is None else int(record.generation_resume),
                        None
                        if record.generation_work_used_s is None
                        else str(record.generation_work_used_s),
                        record.operator_summary,
                    ),
                )
        except sqlite3.IntegrityError:
            if self._same_cycle_persist_attempt(record):
                LOGGER.info(
                    "paper_scan_cycle_same_persist_attempt cycle_id=%s",
                    record.cycle_id,
                )
                return
            raise

    def append_cycle_diagnostic(
        self,
        *,
        cycle_id: str,
        scan_lane: str,
        completed_at: datetime,
        report: dict[str, Any],
        now: datetime | None = None,
    ) -> None:
        """Insert one bounded diagnostic row, then apply retention."""

        completed = completed_at if completed_at.tzinfo is not None else completed_at.replace(tzinfo=UTC)
        payload = json.dumps(report, separators=(",", ":"), default=str)
        try:
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO paper_scan_cycle_diagnostics (
                        cycle_id, scan_lane, completed_at, report_json
                    ) VALUES (?, ?, ?, ?)
                    """,
                    (cycle_id, scan_lane, completed.isoformat(), payload),
                )
        except sqlite3.IntegrityError:
            LOGGER.info("paper_scan_cycle_diagnostic_same_persist_attempt cycle_id=%s", cycle_id)
        self.purge_cycle_diagnostics(now=now or datetime.now(UTC))

    def get_cycle_diagnostic(self, cycle_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT report_json FROM paper_scan_cycle_diagnostics
                WHERE cycle_id = ?
                """,
                (cycle_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            loaded = json.loads(row["report_json"])
        except json.JSONDecodeError:
            return None
        return loaded if isinstance(loaded, dict) else None

    def purge_cycle_diagnostics(self, *, now: datetime | None = None) -> int:
        """Drop rows older than 7 days, then the oldest rows above the fixed cap."""

        evaluated = now or datetime.now(UTC)
        if evaluated.tzinfo is None:
            evaluated = evaluated.replace(tzinfo=UTC)
        cutoff = (evaluated - timedelta(days=DIAGNOSTIC_RETENTION_DAYS)).isoformat()
        with self._connect() as connection:
            deleted = connection.execute(
                """
                DELETE FROM paper_scan_cycle_diagnostics
                WHERE completed_at < ?
                """,
                (cutoff,),
            ).rowcount
            overflow = connection.execute(
                """
                SELECT COUNT(*) AS n FROM paper_scan_cycle_diagnostics
                """
            ).fetchone()
            extra = int(overflow["n"] if overflow is not None else 0) - DIAGNOSTIC_MAX_ROWS
            if extra > 0:
                connection.execute(
                    """
                    DELETE FROM paper_scan_cycle_diagnostics
                    WHERE cycle_id IN (
                        SELECT cycle_id FROM paper_scan_cycle_diagnostics
                        ORDER BY completed_at ASC, cycle_id ASC
                        LIMIT ?
                    )
                    """,
                    (extra,),
                )
                deleted += extra
        return int(deleted or 0)

    def _same_cycle_persist_attempt(self, record: PaperScanCycleRecord) -> bool:
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT cycle_id, scan_lane, started_at, completed_at
                FROM paper_scan_cycles
                WHERE cycle_id = ?
                """,
                (record.cycle_id,),
            ).fetchone()
        if row is None:
            return False
        return (
            str(row["cycle_id"]) == record.cycle_id
            and str(row["scan_lane"]) == record.scan_lane
            and str(row["started_at"]) == record.started_at.isoformat()
            and str(row["completed_at"]) == record.completed_at.isoformat()
        )

    def list_cycles(self, *, limit: int = 100) -> list[PaperScanCycleRecord]:
        """Return newest-first completed scan cycles for a bounded window."""

        if limit <= 0:
            raise ValueError("limit must be positive")
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM paper_scan_cycles
                ORDER BY completed_at DESC, started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        cycles: list[PaperScanCycleRecord] = []
        for row in rows:
            mapping = _row_mapping(row)
            try:
                cycles.append(_cycle_from_mapping(mapping))
            except _ROW_DECODE_ERRORS as exc:
                LOGGER.warning(
                    "paper scan cycle row could not be reconstructed: cycle_id=%s reason=%s",
                    mapping.get("cycle_id"),
                    f"{type(exc).__name__}: {exc}",
                )
        return cycles

    def list_scans(
        self,
        *,
        limit: int = 100,
        eligible_only: bool = False,
        arbitrage_only: bool = False,
        since: datetime | None = None,
    ) -> list[PaperScanRecord]:
        """Return newest-first audit rows for a bounded window.

        This is not current scanner radar state. ``LIMIT`` is applied in SQL
        before decode, so a readable row older than the window stays stored
        but is omitted from this response. Malformed rows occupy a window
        slot, are skipped from the decoded list, and remain in SQLite.
        """
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
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM paper_scan_records{where} ORDER BY scanned_at DESC LIMIT ?",  # noqa: S608
                parameters,
            ).fetchall()
        records, _issues = _decode_rows(rows)
        return records

    def summary(self, *, since: datetime) -> PaperScanSummary:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM paper_scan_records WHERE scanned_at >= ? ORDER BY scanned_at DESC",
                (since.isoformat(),),
            ).fetchall()
        records, issues = _decode_rows(rows)
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
            malformed_count=len(issues),
            malformed_issues=issues,
        )

    def close(self) -> None:
        with self._lock:
            if self._shared_connection is not None:
                self._shared_connection.close()
                self._shared_connection = None


def _decode_rows(
    rows: list[sqlite3.Row],
) -> tuple[list[PaperScanRecord], list[PaperScanReadIssue]]:
    records: list[PaperScanRecord] = []
    issues: list[PaperScanReadIssue] = []
    for row in rows:
        record, issue = _try_record_from_row(row)
        if record is not None:
            records.append(record)
        if issue is not None:
            issues.append(issue)
            LOGGER.warning(
                "paper scan audit row could not be reconstructed as PaperScanRecord: "
                "record_id=%s reason=%s",
                issue.record_id,
                issue.reason,
            )
    return records, issues


def _try_record_from_row(
    row: sqlite3.Row,
) -> tuple[PaperScanRecord | None, PaperScanReadIssue | None]:
    record_id: str | None = None
    try:
        mapping = _row_mapping(row)
        raw_id = mapping.get("record_id")
        if raw_id is not None:
            record_id = str(raw_id)
        return _record_from_mapping(mapping), None
    except _ROW_DECODE_ERRORS as exc:
        return None, PaperScanReadIssue(
            record_id=record_id,
            reason=f"{type(exc).__name__}: {exc}",
        )


def _row_mapping(row: Any) -> dict[str, Any]:
    if isinstance(row, sqlite3.Row):
        try:
            return {str(key): row[key] for key in row.keys()}
        except IndexError as exc:
            raise ValueError("sqlite row is missing expected columns") from exc
    raise TypeError(f"unexpected sqlite row type {type(row).__name__}")


def _record_from_mapping(mapping: dict[str, Any]) -> PaperScanRecord:
    return PaperScanRecord(
        record_id=_required_str(mapping, "record_id"),
        scanned_at=datetime.fromisoformat(_required_str(mapping, "scanned_at")),
        canonical_event_id=_required_str(mapping, "canonical_event_id"),
        canonical_market_id=_required_str(mapping, "canonical_market_id"),
        competition=_required_str(mapping, "competition"),
        home_team=_required_str(mapping, "home_team"),
        away_team=_required_str(mapping, "away_team"),
        kickoff_utc=datetime.fromisoformat(_required_str(mapping, "kickoff_utc")),
        market_family=MarketFamily(_required_str(mapping, "market_family")),
        period=FootballPeriod(_required_str(mapping, "period")),
        line=_decimal(mapping.get("line")),
        venues=[VenueName(value) for value in _json_list(mapping.get("venues_json"))],
        source_market_ids=[str(value) for value in _json_list(mapping.get("source_market_ids_json"))],
        mapping_confidence=float(_required_value(mapping, "mapping_confidence")),
        is_arbitrage=_required_bool(mapping, "is_arbitrage"),
        eligible_for_paper_simulation=_required_bool(mapping, "eligible_for_paper_simulation"),
        gross_edge=_decimal(mapping.get("gross_edge")),
        net_edge=_decimal(mapping.get("net_edge")),
        executable_stake_gbp=_decimal(mapping.get("executable_stake_gbp")),
        guaranteed_profit_gbp=_decimal(mapping.get("guaranteed_profit_gbp")),
        execution_risk_score=mapping.get("execution_risk_score"),
        execution_risk_band=mapping.get("execution_risk_band"),
        rejection_reasons=[str(value) for value in _json_list(mapping.get("rejection_reasons_json"))],
        decision_json=_required_str(mapping, "decision_json"),
    )


def _cycle_from_mapping(mapping: dict[str, Any]) -> PaperScanCycleRecord:
    generation_resume = mapping.get("generation_resume")
    work_used = mapping.get("generation_work_used_s")
    generation_id = mapping.get("universe_generation_id")
    return PaperScanCycleRecord(
        cycle_id=_required_str(mapping, "cycle_id"),
        started_at=datetime.fromisoformat(_required_str(mapping, "started_at")),
        completed_at=datetime.fromisoformat(_required_str(mapping, "completed_at")),
        scan_lane=_required_str(mapping, "scan_lane"),
        duration_ms=int(_required_value(mapping, "duration_ms")),
        fixture_count=int(_required_value(mapping, "fixture_count")),
        evaluated_count=int(_required_value(mapping, "evaluated_count")),
        not_evaluated_count=int(_required_value(mapping, "not_evaluated_count")),
        matched_event_pairs=int(_required_value(mapping, "matched_event_pairs")),
        matched_market_pairs=int(_required_value(mapping, "matched_market_pairs")),
        paper_decision_count=int(_required_value(mapping, "paper_decision_count")),
        qualifying_arb_count=int(_required_value(mapping, "qualifying_arb_count")),
        venue_health=_json_object(mapping.get("venue_health_json")),
        degraded=_required_bool(mapping, "degraded"),
        last_error=None if mapping.get("last_error") is None else str(mapping.get("last_error")),
        universe_generation_id=None if generation_id is None else int(generation_id),
        resume_cursor=None
        if mapping.get("resume_cursor") is None
        else str(mapping.get("resume_cursor")),
        completeness=None if mapping.get("completeness") is None else str(mapping.get("completeness")),
        generation_resume=None if generation_resume is None else bool(generation_resume),
        generation_work_used_s=None if work_used in (None, "") else float(work_used),
        operator_summary=None
        if mapping.get("operator_summary") is None
        else str(mapping.get("operator_summary")),
    )


def _required_value(mapping: dict[str, Any], key: str) -> Any:
    if key not in mapping:
        raise KeyError(f"missing column {key}")
    value = mapping[key]
    if value is None:
        raise ValueError(f"missing required value for {key}")
    return value


def _required_str(mapping: dict[str, Any], key: str) -> str:
    return str(_required_value(mapping, key))


def _required_bool(mapping: dict[str, Any], key: str) -> bool:
    return bool(_required_value(mapping, key))


def _json_object(value: Any) -> dict[str, str]:
    if value is None:
        return {}
    parsed = json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value
    if not isinstance(parsed, dict):
        raise ValueError("expected JSON object")
    return {str(key): str(item) for key, item in parsed.items()}


def _json_list(value: Any) -> list[Any]:
    if value is None:
        raise ValueError("missing JSON list")
    parsed = json.loads(value) if isinstance(value, (str, bytes, bytearray)) else value
    if not isinstance(parsed, list):
        raise ValueError("expected JSON list")
    return parsed


def _stringify_decimal(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if text == "":
        return None
    try:
        return Decimal(text)
    except InvalidOperation as exc:
        raise ValueError(f"invalid decimal {text!r}") from exc
