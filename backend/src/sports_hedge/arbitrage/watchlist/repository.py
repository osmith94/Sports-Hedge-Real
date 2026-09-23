from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Collection, Iterator
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

from sports_hedge.arbitrage.watchlist.models import (
    OPERATOR_ACTIVITY_UNCONDITIONAL_EVENT_TYPES,
    LifecycleEventType,
    NearOpportunity,
    OpportunityClassification,
    OpportunityLifecycleEvent,
    OpportunityObservationPoint,
    OpportunityStatus,
    PaperFillAttempt,
    PaperFillAttemptStatus,
    WatchLeg,
    attempt_id_from_lifecycle_event,
)
from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.domain.models import MarketScope, VenueName

_PROTECTED_LIFECYCLE_STATUSES = (
    "PAPER_FILLING",
    "PARTIAL",
    "FILLED",
    "CLOSED",
    "EXPIRED",
)
_OBSERVATION_STATUSES = (
    "WATCHING",
    "APPROACHING",
    "TRIGGERED",
    "REJECTED",
)
_PROTECTED_SQL = ", ".join(f"'{status}'" for status in _PROTECTED_LIFECYCLE_STATUSES)
_OBSERVATION_SQL = ", ".join(f"'{status}'" for status in _OBSERVATION_STATUSES)
_PRESERVE_FILL_LIFECYCLE_SQL = (
    f"watchlist_opportunities.status IN ({_PROTECTED_SQL}) "
    f"AND excluded.status IN ({_OBSERVATION_SQL})"
)


class SqliteWatchlistRepository:
    """Current-opportunity snapshot plus append-only lifecycle events.

    sqlite3.Connection is not safe for concurrent use, even with
    ``check_same_thread=False``. FastAPI paper persist and scan observe share
    this repository from a thread pool, so every connection use is serialized.
    Lifecycle observe/fill read-modify-write also uses ``BEGIN IMMEDIATE`` so
    a second worker connection cannot regress a durable FILLED row.
    """

    def __init__(self, database: str | Path = ":memory:") -> None:
        self._lock = threading.RLock()
        self._tx_depth = 0
        self._connection = sqlite3.connect(
            str(database),
            check_same_thread=False,
            timeout=30.0,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.isolation_level = None
        self._connection.execute("PRAGMA busy_timeout=5000")
        self._create_schema()

    @contextmanager
    def exclusive(self) -> Iterator[None]:
        with self._lock:
            yield

    @contextmanager
    def transaction(self) -> Iterator[None]:
        with self._lock:
            self._tx_depth += 1
            started = self._tx_depth == 1
            if started:
                self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield
                if started:
                    self._connection.commit()
            except Exception:
                if started:
                    self._connection.rollback()
                raise
            finally:
                self._tx_depth -= 1

    def _commit(self) -> None:
        if self._tx_depth == 0:
            self._connection.commit()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS watchlist_opportunities (
                opportunity_id TEXT PRIMARY KEY,
                canonical_event_id TEXT NOT NULL,
                canonical_market_id TEXT NOT NULL,
                settlement_key TEXT,
                competition TEXT,
                home_team TEXT,
                away_team TEXT,
                market_family TEXT,
                period TEXT,
                venues_json TEXT NOT NULL,
                legs_json TEXT NOT NULL,
                status TEXT NOT NULL,
                classification TEXT NOT NULL,
                is_arbitrage INTEGER NOT NULL,
                trigger_net_edge TEXT,
                current_net_edge TEXT,
                distance_to_trigger_pp TEXT,
                implied_probability_sum TEXT,
                quote_age_ms INTEGER,
                limiting_depth_gbp TEXT,
                limiting_leg_outcome TEXT,
                capital_required_gbp TEXT,
                guaranteed_profit_gbp TEXT,
                execution_risk_score INTEGER,
                expected_lock_minutes TEXT,
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                rejection_reasons_json TEXT NOT NULL,
                insufficiency_reasons_json TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS watchlist_lifecycle_events (
                event_id TEXT PRIMARY KEY,
                opportunity_id TEXT NOT NULL,
                occurred_at TEXT NOT NULL,
                event_type TEXT NOT NULL,
                status TEXT NOT NULL,
                current_net_edge TEXT,
                distance_to_trigger_pp TEXT,
                detail TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_watchlist_status
                ON watchlist_opportunities(status, last_seen_at DESC);
            CREATE INDEX IF NOT EXISTS idx_watchlist_market
                ON watchlist_opportunities(canonical_market_id);
            CREATE INDEX IF NOT EXISTS idx_watchlist_events_time
                ON watchlist_lifecycle_events(occurred_at DESC);
            CREATE INDEX IF NOT EXISTS idx_watchlist_events_opportunity
                ON watchlist_lifecycle_events(opportunity_id, occurred_at);

            CREATE TABLE IF NOT EXISTS watchlist_observation_history (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                opportunity_id TEXT NOT NULL,
                observed_at TEXT NOT NULL,
                current_net_edge TEXT,
                distance_to_trigger_pp TEXT,
                quote_age_ms INTEGER,
                status TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS idx_watchlist_observation_history
                ON watchlist_observation_history(opportunity_id, observed_at, id);

            CREATE TABLE IF NOT EXISTS paper_fill_attempts (
                attempt_id TEXT PRIMARY KEY,
                opportunity_id TEXT NOT NULL,
                bound_snapshot INTEGER NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                decision_at TEXT,
                finished_at TEXT,
                detail TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_paper_fill_attempts_opportunity
                ON paper_fill_attempts(opportunity_id, started_at DESC, attempt_id DESC);
            """
        )
        columns = {
            row[1]
            for row in self._connection.execute("PRAGMA table_info(watchlist_opportunities)")
        }
        extras = {
            "gross_edge": "TEXT",
            "fixture_discovery_source": "TEXT",
            "fixture_status": "TEXT",
            "in_running": "INTEGER",
            "live_score_supported": "INTEGER",
            "home_score": "INTEGER",
            "away_score": "INTEGER",
            "strike_narrative": "TEXT",
            "previous_net_edge": "TEXT",
            "previous_distance_to_trigger_pp": "TEXT",
            "observation_count": "INTEGER",
            "quote_age_basis": "TEXT",
            "data_kind": "TEXT",
            "mapping_confidence": "TEXT",
            "mapping_matched": "INTEGER",
            "mapping_reasons_json": "TEXT",
            "mapping_provenance_json": "TEXT",
            "mapping_review_candidate_json": "TEXT",
            "line": "TEXT",
            "capture_eligible": "INTEGER",
            "min_net_edge_scope": "TEXT",
            "min_net_edge_source": "TEXT",
            "min_net_edge_configured": "INTEGER",
        }
        for name, ddl in extras.items():
            if name not in columns:
                self._connection.execute(
                    f"ALTER TABLE watchlist_opportunities ADD COLUMN {name} {ddl}"
                )
        event_columns = {
            row[1]
            for row in self._connection.execute("PRAGMA table_info(watchlist_lifecycle_events)")
        }
        event_extras = {
            "fixture_label": "TEXT",
            "market_family": "TEXT",
            "canonical_event_id": "TEXT",
            "canonical_market_id": "TEXT",
            "capture_eligible": "INTEGER",
            "attempt_id": "TEXT",
            "trigger_net_edge": "TEXT",
            "min_net_edge_scope": "TEXT",
            "min_net_edge_source": "TEXT",
        }
        for name, ddl in event_extras.items():
            if name not in event_columns:
                self._connection.execute(
                    f"ALTER TABLE watchlist_lifecycle_events ADD COLUMN {name} {ddl}"
                )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_watchlist_events_canonical_event
                ON watchlist_lifecycle_events(canonical_event_id, occurred_at)
            """
        )
        self._commit()

    def get(self, opportunity_id: str) -> NearOpportunity | None:
        with self.exclusive():
            row = self._connection.execute(
                "SELECT * FROM watchlist_opportunities WHERE opportunity_id = ?",
                (opportunity_id,),
            ).fetchone()
            return None if row is None else _opportunity_from_row(row)

    def upsert_opportunity(self, opportunity: NearOpportunity, *, force_status: bool = False) -> None:
        with self.exclusive():
            self._upsert_opportunity_locked(opportunity, force_status=force_status)
            self._commit()

    def _upsert_opportunity_locked(
        self, opportunity: NearOpportunity, *, force_status: bool = False
    ) -> None:
        preserve = "0" if force_status else _PRESERVE_FILL_LIFECYCLE_SQL
        self._connection.execute(
            f"""
            INSERT INTO watchlist_opportunities (
                opportunity_id, canonical_event_id, canonical_market_id, settlement_key,
                competition, home_team, away_team, market_family, period, line, venues_json,
                legs_json, status, classification, is_arbitrage, trigger_net_edge,
                current_net_edge, gross_edge, distance_to_trigger_pp, implied_probability_sum,
                quote_age_ms, limiting_depth_gbp, limiting_leg_outcome,
                capital_required_gbp, guaranteed_profit_gbp, execution_risk_score,
                expected_lock_minutes, first_seen_at, last_seen_at,
                rejection_reasons_json, insufficiency_reasons_json,
                fixture_discovery_source, fixture_status, in_running,
                live_score_supported, home_score, away_score, strike_narrative,
                previous_net_edge, previous_distance_to_trigger_pp, observation_count,
                quote_age_basis, data_kind, mapping_confidence, mapping_matched,
                mapping_reasons_json, mapping_provenance_json, mapping_review_candidate_json,
                capture_eligible, min_net_edge_scope, min_net_edge_source, min_net_edge_configured
            ) VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, ?, ?, ?, ?
            )
            ON CONFLICT(opportunity_id) DO UPDATE SET
                canonical_event_id = excluded.canonical_event_id,
                canonical_market_id = excluded.canonical_market_id,
                settlement_key = excluded.settlement_key,
                competition = excluded.competition,
                home_team = excluded.home_team,
                away_team = excluded.away_team,
                market_family = excluded.market_family,
                period = excluded.period,
                line = excluded.line,
                venues_json = excluded.venues_json,
                legs_json = excluded.legs_json,
                status = CASE
                    WHEN {preserve}
                    THEN watchlist_opportunities.status
                    ELSE excluded.status
                END,
                classification = CASE
                    WHEN {preserve}
                    THEN watchlist_opportunities.classification
                    ELSE excluded.classification
                END,
                is_arbitrage = CASE
                    WHEN {preserve}
                    THEN watchlist_opportunities.is_arbitrage
                    ELSE excluded.is_arbitrage
                END,
                trigger_net_edge = excluded.trigger_net_edge,
                current_net_edge = excluded.current_net_edge,
                gross_edge = excluded.gross_edge,
                distance_to_trigger_pp = excluded.distance_to_trigger_pp,
                implied_probability_sum = excluded.implied_probability_sum,
                quote_age_ms = excluded.quote_age_ms,
                limiting_depth_gbp = excluded.limiting_depth_gbp,
                limiting_leg_outcome = excluded.limiting_leg_outcome,
                capital_required_gbp = excluded.capital_required_gbp,
                guaranteed_profit_gbp = excluded.guaranteed_profit_gbp,
                execution_risk_score = excluded.execution_risk_score,
                expected_lock_minutes = excluded.expected_lock_minutes,
                first_seen_at = excluded.first_seen_at,
                last_seen_at = excluded.last_seen_at,
                rejection_reasons_json = excluded.rejection_reasons_json,
                insufficiency_reasons_json = excluded.insufficiency_reasons_json,
                fixture_discovery_source = excluded.fixture_discovery_source,
                fixture_status = excluded.fixture_status,
                in_running = excluded.in_running,
                live_score_supported = excluded.live_score_supported,
                home_score = excluded.home_score,
                away_score = excluded.away_score,
                strike_narrative = excluded.strike_narrative,
                previous_net_edge = excluded.previous_net_edge,
                previous_distance_to_trigger_pp = excluded.previous_distance_to_trigger_pp,
                observation_count = excluded.observation_count,
                quote_age_basis = excluded.quote_age_basis,
                data_kind = excluded.data_kind,
                mapping_confidence = excluded.mapping_confidence,
                mapping_matched = excluded.mapping_matched,
                mapping_reasons_json = excluded.mapping_reasons_json,
                mapping_provenance_json = excluded.mapping_provenance_json,
                mapping_review_candidate_json = excluded.mapping_review_candidate_json,
                capture_eligible = excluded.capture_eligible,
                min_net_edge_scope = excluded.min_net_edge_scope,
                min_net_edge_source = excluded.min_net_edge_source,
                min_net_edge_configured = excluded.min_net_edge_configured
            """,
            (
                opportunity.opportunity_id,
                opportunity.canonical_event_id,
                opportunity.canonical_market_id,
                opportunity.settlement_key,
                opportunity.competition,
                opportunity.home_team,
                opportunity.away_team,
                opportunity.market_family.value if opportunity.market_family else None,
                opportunity.period.value if opportunity.period else None,
                _stringify(opportunity.line),
                json.dumps([venue.value for venue in opportunity.venues]),
                json.dumps([leg.model_dump(mode="json") for leg in opportunity.legs]),
                opportunity.status.value,
                opportunity.classification.value,
                int(opportunity.is_arbitrage),
                _stringify(opportunity.trigger_net_edge),
                _stringify(opportunity.current_net_edge),
                _stringify(opportunity.gross_edge),
                _stringify(opportunity.distance_to_trigger_pp),
                _stringify(opportunity.implied_probability_sum),
                opportunity.quote_age_ms,
                _stringify(opportunity.limiting_depth_gbp),
                opportunity.limiting_leg_outcome,
                _stringify(opportunity.capital_required_gbp),
                _stringify(opportunity.guaranteed_profit_gbp),
                opportunity.execution_risk_score,
                _stringify(opportunity.expected_lock_minutes),
                opportunity.first_seen_at.isoformat(),
                opportunity.last_seen_at.isoformat(),
                json.dumps(opportunity.rejection_reasons),
                json.dumps(opportunity.insufficiency_reasons),
                opportunity.fixture_discovery_source.value
                if opportunity.fixture_discovery_source
                else None,
                opportunity.fixture_status,
                None if opportunity.in_running is None else int(opportunity.in_running),
                int(opportunity.live_score_supported),
                opportunity.home_score,
                opportunity.away_score,
                opportunity.strike_narrative,
                _stringify(opportunity.previous_net_edge),
                _stringify(opportunity.previous_distance_to_trigger_pp),
                opportunity.observation_count,
                opportunity.quote_age_basis,
                opportunity.data_kind,
                _stringify_float(opportunity.mapping_confidence),
                None if opportunity.mapping_matched is None else int(opportunity.mapping_matched),
                json.dumps(opportunity.mapping_reasons),
                json.dumps(opportunity.mapping_provenance.model_dump(mode="json"))
                if opportunity.mapping_provenance is not None
                else None,
                json.dumps(opportunity.mapping_review_candidate.model_dump(mode="json"))
                if opportunity.mapping_review_candidate is not None
                else None,
                int(opportunity.capture_eligible),
                opportunity.min_net_edge_scope.value
                if opportunity.min_net_edge_scope is not None
                else None,
                opportunity.min_net_edge_source,
                None
                if opportunity.min_net_edge_configured is None
                else int(opportunity.min_net_edge_configured),
            ),
        )

    def append_event(self, event: OpportunityLifecycleEvent) -> None:
        with self.exclusive():
            self._connection.execute(
                """
                INSERT OR IGNORE INTO watchlist_lifecycle_events (
                    event_id, opportunity_id, occurred_at, event_type, status,
                    current_net_edge, distance_to_trigger_pp, detail,
                    fixture_label, market_family, canonical_event_id,
                    canonical_market_id, capture_eligible, attempt_id,
                    trigger_net_edge, min_net_edge_scope, min_net_edge_source
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    event.event_id,
                    event.opportunity_id,
                    event.occurred_at.isoformat(),
                    event.event_type.value,
                    event.status.value,
                    _stringify(event.current_net_edge),
                    _stringify(event.distance_to_trigger_pp),
                    event.detail,
                    event.fixture_label,
                    event.market_family,
                    event.canonical_event_id,
                    event.canonical_market_id,
                    None if event.capture_eligible is None else int(event.capture_eligible),
                    event.attempt_id,
                    _stringify(event.trigger_net_edge),
                    event.min_net_edge_scope,
                    event.min_net_edge_source,
                ),
            )
            self._commit()

    def append_observation(self, point: OpportunityObservationPoint) -> None:
        with self.exclusive():
            self._connection.execute(
                """
                INSERT INTO watchlist_observation_history (
                    opportunity_id, observed_at, current_net_edge, distance_to_trigger_pp,
                    quote_age_ms, status
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    point.opportunity_id,
                    point.observed_at.isoformat(),
                    _stringify(point.current_net_edge),
                    _stringify(point.distance_to_trigger_pp),
                    point.quote_age_ms,
                    point.status.value,
                ),
            )
            self._commit()

    def list_observations(
        self,
        opportunity_id: str,
        *,
        limit: int = 50,
    ) -> list[OpportunityObservationPoint]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        with self.exclusive():
            rows = self._connection.execute(
                """
                SELECT * FROM watchlist_observation_history
                WHERE opportunity_id = ?
                ORDER BY observed_at DESC, id DESC
                LIMIT ?
                """,
                (opportunity_id, limit),
            ).fetchall()
            points = [_observation_from_row(row) for row in rows]
            points.reverse()
            return points

    def list_opportunities(self) -> list[NearOpportunity]:
        with self.exclusive():
            rows = self._connection.execute(
                "SELECT * FROM watchlist_opportunities ORDER BY last_seen_at DESC"
            ).fetchall()
            return [_opportunity_from_row(row) for row in rows]

    def list_events(
        self,
        *,
        limit: int = 100,
        opportunity_id: str | None = None,
        canonical_event_id: str | None = None,
        since: datetime | None = None,
        event_types: Collection[LifecycleEventType] | None = None,
        operator_signal: bool = False,
    ) -> list[OpportunityLifecycleEvent]:
        if limit <= 0:
            raise ValueError("limit must be positive")
        clauses: list[str] = []
        parameters: list[Any] = []
        if opportunity_id is not None and canonical_event_id is not None:
            clauses.append("(opportunity_id = ? OR canonical_event_id = ?)")
            parameters.extend([opportunity_id, canonical_event_id])
        elif opportunity_id is not None:
            clauses.append("opportunity_id = ?")
            parameters.append(opportunity_id)
        elif canonical_event_id is not None:
            clauses.append("canonical_event_id = ?")
            parameters.append(canonical_event_id)
        if since is not None:
            clauses.append("occurred_at >= ?")
            parameters.append(since.isoformat())
        if operator_signal:
            unconditional = tuple(
                event.value for event in OPERATOR_ACTIVITY_UNCONDITIONAL_EVENT_TYPES
            )
            placeholders = ", ".join("?" for _ in unconditional)
            clauses.append(
                f"(event_type IN ({placeholders}) OR "
                "(event_type = ? AND capture_eligible = 1))"
            )
            parameters.extend(unconditional)
            parameters.append(LifecycleEventType.TRIGGER_LOST_BEFORE_FILL.value)
        elif event_types:
            types = tuple(event.value for event in event_types)
            placeholders = ", ".join("?" for _ in types)
            clauses.append(f"event_type IN ({placeholders})")
            parameters.extend(types)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        parameters.append(limit)
        with self.exclusive():
            rows = self._connection.execute(
                f"SELECT watchlist_lifecycle_events.*, "  # noqa: S608
                "watchlist_lifecycle_events.rowid AS append_seq "
                f"FROM watchlist_lifecycle_events{where} "
                "ORDER BY occurred_at DESC, append_seq DESC LIMIT ?",
                parameters,
            ).fetchall()
            return [_event_from_row(row) for row in rows]

    def upsert_paper_fill_attempt(self, attempt: PaperFillAttempt) -> None:
        with self.exclusive():
            self._connection.execute(
                """
                INSERT INTO paper_fill_attempts (
                    attempt_id, opportunity_id, bound_snapshot, status,
                    started_at, decision_at, finished_at, detail
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(attempt_id) DO UPDATE SET
                    opportunity_id = excluded.opportunity_id,
                    bound_snapshot = excluded.bound_snapshot,
                    status = excluded.status,
                    started_at = excluded.started_at,
                    decision_at = excluded.decision_at,
                    finished_at = excluded.finished_at,
                    detail = excluded.detail
                """,
                (
                    attempt.attempt_id,
                    attempt.opportunity_id,
                    int(attempt.bound_snapshot),
                    attempt.status.value,
                    attempt.started_at.isoformat(),
                    None if attempt.decision_at is None else attempt.decision_at.isoformat(),
                    None if attempt.finished_at is None else attempt.finished_at.isoformat(),
                    attempt.detail,
                ),
            )
            self._commit()

    def get_paper_fill_attempt(self, attempt_id: str) -> PaperFillAttempt | None:
        with self.exclusive():
            row = self._connection.execute(
                "SELECT * FROM paper_fill_attempts WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()
            return None if row is None else _paper_fill_attempt_from_row(row)

    def list_paper_fill_attempts(self, opportunity_id: str) -> list[PaperFillAttempt]:
        with self.exclusive():
            rows = self._connection.execute(
                """
                SELECT * FROM paper_fill_attempts
                WHERE opportunity_id = ?
                ORDER BY started_at DESC, attempt_id DESC
                """,
                (opportunity_id,),
            ).fetchall()
            return [_paper_fill_attempt_from_row(row) for row in rows]

    def get_started_paper_fill_attempt(self, opportunity_id: str) -> PaperFillAttempt | None:
        with self.exclusive():
            row = self._connection.execute(
                """
                SELECT * FROM paper_fill_attempts
                WHERE opportunity_id = ? AND status = ?
                ORDER BY started_at DESC, attempt_id DESC
                LIMIT 1
                """,
                (opportunity_id, PaperFillAttemptStatus.STARTED.value),
            ).fetchone()
            return None if row is None else _paper_fill_attempt_from_row(row)

    def close(self) -> None:
        with self.exclusive():
            self._connection.close()


def _opportunity_from_row(row: sqlite3.Row) -> NearOpportunity:
    return NearOpportunity(
        opportunity_id=row["opportunity_id"],
        canonical_event_id=row["canonical_event_id"],
        canonical_market_id=row["canonical_market_id"],
        settlement_key=row["settlement_key"],
        competition=row["competition"],
        home_team=row["home_team"],
        away_team=row["away_team"],
        market_family=MarketFamily(row["market_family"]) if row["market_family"] else None,
        period=FootballPeriod(row["period"]) if row["period"] else None,
        line=_decimal(_row_get(row, "line")),
        venues=[VenueName(value) for value in json.loads(row["venues_json"])],
        legs=[WatchLeg.model_validate(item) for item in json.loads(row["legs_json"])],
        status=OpportunityStatus(row["status"]),
        classification=OpportunityClassification(row["classification"]),
        is_arbitrage=bool(row["is_arbitrage"]),
        trigger_net_edge=_decimal(row["trigger_net_edge"]),
        current_net_edge=_decimal(row["current_net_edge"]),
        gross_edge=_decimal(row["gross_edge"]) if "gross_edge" in row.keys() else None,
        distance_to_trigger_pp=_decimal(row["distance_to_trigger_pp"]),
        implied_probability_sum=_decimal(row["implied_probability_sum"]),
        quote_age_ms=None if row["quote_age_ms"] is None else int(row["quote_age_ms"]),
        limiting_depth_gbp=_decimal(row["limiting_depth_gbp"]),
        limiting_leg_outcome=row["limiting_leg_outcome"],
        capital_required_gbp=_decimal(row["capital_required_gbp"]),
        guaranteed_profit_gbp=_decimal(row["guaranteed_profit_gbp"]),
        execution_risk_score=row["execution_risk_score"],
        expected_lock_minutes=_decimal(row["expected_lock_minutes"]),
        first_seen_at=datetime.fromisoformat(row["first_seen_at"]),
        last_seen_at=datetime.fromisoformat(row["last_seen_at"]),
        rejection_reasons=list(json.loads(row["rejection_reasons_json"])),
        insufficiency_reasons=list(json.loads(row["insufficiency_reasons_json"])),
        fixture_discovery_source=_venue(_row_get(row, "fixture_discovery_source")),
        fixture_status=_row_get(row, "fixture_status"),
        in_running=_optional_bool(_row_get(row, "in_running")),
        live_score_supported=bool(_row_get(row, "live_score_supported") or 0),
        home_score=_optional_int(_row_get(row, "home_score")),
        away_score=_optional_int(_row_get(row, "away_score")),
        strike_narrative=_row_get(row, "strike_narrative"),
        previous_net_edge=_decimal(_row_get(row, "previous_net_edge")),
        previous_distance_to_trigger_pp=_decimal(
            _row_get(row, "previous_distance_to_trigger_pp")
        ),
        observation_count=int(_row_get(row, "observation_count") or 0),
        quote_age_basis=_row_get(row, "quote_age_basis"),
        data_kind=_row_get(row, "data_kind") or "live_paper",
        mapping_confidence=_float(_row_get(row, "mapping_confidence")),
        mapping_matched=_optional_bool(_row_get(row, "mapping_matched")),
        mapping_reasons=_json_list(_row_get(row, "mapping_reasons_json")),
        mapping_provenance=_mapping_provenance(_row_get(row, "mapping_provenance_json")),
        mapping_review_candidate=_mapping_candidate(_row_get(row, "mapping_review_candidate_json")),
        capture_eligible=bool(_row_get(row, "capture_eligible") or 0),
        min_net_edge_scope=_market_scope(_row_get(row, "min_net_edge_scope")),
        min_net_edge_source=_row_get(row, "min_net_edge_source"),
        min_net_edge_configured=_optional_bool(_row_get(row, "min_net_edge_configured")),
    )


def _paper_fill_attempt_from_row(row: sqlite3.Row) -> PaperFillAttempt:
    return PaperFillAttempt(
        attempt_id=row["attempt_id"],
        opportunity_id=row["opportunity_id"],
        bound_snapshot=bool(row["bound_snapshot"]),
        status=PaperFillAttemptStatus(row["status"]),
        started_at=datetime.fromisoformat(row["started_at"]),
        decision_at=None
        if row["decision_at"] is None
        else datetime.fromisoformat(row["decision_at"]),
        finished_at=None
        if row["finished_at"] is None
        else datetime.fromisoformat(row["finished_at"]),
        detail=row["detail"],
    )


def _event_from_row(row: sqlite3.Row) -> OpportunityLifecycleEvent:
    event_type = LifecycleEventType(row["event_type"])
    stored_attempt_id = _row_get(row, "attempt_id")
    append_seq = _row_get(row, "append_seq")
    return OpportunityLifecycleEvent(
        event_id=row["event_id"],
        opportunity_id=row["opportunity_id"],
        occurred_at=datetime.fromisoformat(row["occurred_at"]),
        event_type=event_type,
        status=OpportunityStatus(row["status"]),
        current_net_edge=_decimal(row["current_net_edge"]),
        distance_to_trigger_pp=_decimal(row["distance_to_trigger_pp"]),
        detail=row["detail"],
        fixture_label=_row_get(row, "fixture_label"),
        market_family=_row_get(row, "market_family"),
        canonical_event_id=_row_get(row, "canonical_event_id"),
        canonical_market_id=_row_get(row, "canonical_market_id"),
        capture_eligible=_optional_bool(_row_get(row, "capture_eligible")),
        attempt_id=attempt_id_from_lifecycle_event(
            opportunity_id=row["opportunity_id"],
            event_type=event_type,
            event_id=row["event_id"],
            stored_attempt_id=None if stored_attempt_id in (None, "") else str(stored_attempt_id),
        ),
        append_seq=None if append_seq is None else int(append_seq),
        trigger_net_edge=_decimal(_row_get(row, "trigger_net_edge")),
        min_net_edge_scope=_row_get(row, "min_net_edge_scope"),
        min_net_edge_source=_row_get(row, "min_net_edge_source"),
    )


def _stringify(value: Decimal | None) -> str | None:
    return None if value is None else str(value)


def _stringify_float(value: float | None) -> str | None:
    return None if value is None else str(value)


def _float(value: str | None) -> float | None:
    if value is None or value == "":
        return None
    return float(value)


def _decimal(value: str | None) -> Decimal | None:
    return None if value is None else Decimal(value)


def _observation_from_row(row: sqlite3.Row) -> OpportunityObservationPoint:
    return OpportunityObservationPoint(
        opportunity_id=row["opportunity_id"],
        observed_at=datetime.fromisoformat(row["observed_at"]),
        current_net_edge=_decimal(row["current_net_edge"]),
        distance_to_trigger_pp=_decimal(row["distance_to_trigger_pp"]),
        quote_age_ms=None if row["quote_age_ms"] is None else int(row["quote_age_ms"]),
        status=OpportunityStatus(row["status"]),
    )


def _row_get(row: sqlite3.Row, key: str) -> Any:
    if key not in row.keys():
        return None
    return row[key]


def _venue(value: str | None) -> VenueName | None:
    if not value:
        return None
    return VenueName(value)


def _market_scope(value: str | None) -> MarketScope | None:
    if not value:
        return None
    try:
        return MarketScope(value)
    except ValueError:
        return None


def _optional_bool(value: Any) -> bool | None:
    if value is None:
        return None
    return bool(value)


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _json_list(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return []
    if not isinstance(parsed, list):
        return []
    return [str(item) for item in parsed]


def _mapping_provenance(value: str | None):
    if not value:
        return None
    try:
        from sports_hedge.matching.learned_rules import MappingProvenance

        return MappingProvenance.model_validate(json.loads(value))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def _mapping_candidate(value: str | None):
    if not value:
        return None
    try:
        from sports_hedge.matching.learned_rules import MappingReviewCandidate

        candidate = MappingReviewCandidate.model_validate(json.loads(value))
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
    if len(candidate.sides) < 2:
        return None
    return candidate
