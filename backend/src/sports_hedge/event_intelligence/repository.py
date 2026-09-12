from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

from sports_hedge.event_intelligence.models import (
    EventIntelligenceQualityFlag,
    EventIntelligenceRecord,
    EventIntelligenceType,
    EventPhase,
    EventSubject,
    ProvenanceClass,
    SourceKind,
    VerificationStatus,
)


class SqliteEventIntelligenceRepository:
    """Append-only fixture-linked event-intelligence store. Research context only."""

    def __init__(self, database: str | Path = ":memory:") -> None:
        self._connection = sqlite3.connect(str(database), check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        self._connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS event_intelligence_records (
                event_intelligence_id TEXT PRIMARY KEY,
                canonical_event_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                phase TEXT NOT NULL,
                published_at TEXT NOT NULL,
                ingested_at TEXT NOT NULL,
                source_name TEXT NOT NULL,
                source_kind TEXT NOT NULL,
                source_event_id TEXT NOT NULL,
                source_url TEXT,
                source_reference TEXT,
                title TEXT NOT NULL,
                subject_json TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                provenance_class TEXT NOT NULL,
                verification_status TEXT NOT NULL,
                quality_flags_json TEXT NOT NULL,
                confidence REAL NOT NULL,
                raw_payload_hash TEXT NOT NULL,
                raw_payload_json TEXT NOT NULL,
                content_fingerprint TEXT NOT NULL,
                causal_claim INTEGER NOT NULL DEFAULT 0,
                temporal_context_only INTEGER NOT NULL DEFAULT 1,
                revision_of_id TEXT,
                UNIQUE (source_name, source_event_id, content_fingerprint)
            );

            CREATE INDEX IF NOT EXISTS idx_ei_fixture_published
                ON event_intelligence_records(canonical_event_id, published_at, ingested_at);
            CREATE INDEX IF NOT EXISTS idx_ei_source_fact
                ON event_intelligence_records(source_name, source_event_id);
            CREATE INDEX IF NOT EXISTS idx_ei_type_published
                ON event_intelligence_records(event_type, published_at);
            """
        )
        self._connection.commit()

    def append(self, record: EventIntelligenceRecord) -> None:
        self._connection.execute(
            """
            INSERT INTO event_intelligence_records (
                event_intelligence_id, canonical_event_id, event_type, phase,
                published_at, ingested_at, source_name, source_kind, source_event_id,
                source_url, source_reference, title, subject_json, payload_json,
                provenance_class, verification_status, quality_flags_json, confidence,
                raw_payload_hash, raw_payload_json, content_fingerprint, causal_claim,
                temporal_context_only, revision_of_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                record.event_intelligence_id,
                record.canonical_event_id,
                record.event_type.value,
                record.phase.value,
                record.published_at.isoformat(),
                record.ingested_at.isoformat(),
                record.source_name,
                record.source_kind.value,
                record.source_event_id,
                record.source_url,
                record.source_reference,
                record.title,
                json.dumps(record.subject.model_dump(), separators=(",", ":")),
                json.dumps(record.payload, default=str, separators=(",", ":")),
                record.provenance_class.value,
                record.verification_status.value,
                json.dumps([flag.value for flag in record.quality_flags]),
                record.confidence,
                record.raw_payload_hash,
                json.dumps(record.raw_payload, default=str, separators=(",", ":")),
                record.content_fingerprint,
                1 if record.causal_claim else 0,
                1 if record.temporal_context_only else 0,
                record.revision_of_id,
            ),
        )
        self._connection.commit()

    def get(self, event_intelligence_id: str) -> EventIntelligenceRecord | None:
        row = self._connection.execute(
            "SELECT * FROM event_intelligence_records WHERE event_intelligence_id = ?",
            (event_intelligence_id,),
        ).fetchone()
        return None if row is None else _record_from_row(row)

    def list_for_source_fact(
        self, *, source_name: str, source_event_id: str
    ) -> list[EventIntelligenceRecord]:
        rows = self._connection.execute(
            """
            SELECT * FROM event_intelligence_records
            WHERE lower(source_name) = lower(?) AND source_event_id = ?
            ORDER BY ingested_at ASC, event_intelligence_id ASC
            """,
            (source_name, source_event_id),
        ).fetchall()
        return [_record_from_row(row) for row in rows]

    def list_timeline(
        self,
        canonical_event_id: str,
        *,
        event_type: EventIntelligenceType | None = None,
        source_name: str | None = None,
        provenance_class: ProvenanceClass | None = None,
    ) -> list[EventIntelligenceRecord]:
        clauses = ["canonical_event_id = ?"]
        parameters: list[Any] = [canonical_event_id]
        if event_type is not None:
            clauses.append("event_type = ?")
            parameters.append(event_type.value)
        if source_name is not None:
            clauses.append("lower(source_name) = lower(?)")
            parameters.append(source_name)
        if provenance_class is not None:
            clauses.append("provenance_class = ?")
            parameters.append(provenance_class.value)
        where = " AND ".join(clauses)
        rows = self._connection.execute(
            f"""
            SELECT * FROM event_intelligence_records
            WHERE {where}
            ORDER BY published_at ASC, ingested_at ASC, event_intelligence_id ASC
            """,  # noqa: S608
            parameters,
        ).fetchall()
        return [_record_from_row(row) for row in rows]

    def close(self) -> None:
        self._connection.close()


def _record_from_row(row: sqlite3.Row) -> EventIntelligenceRecord:
    quality_raw = json.loads(row["quality_flags_json"])
    return EventIntelligenceRecord(
        event_intelligence_id=row["event_intelligence_id"],
        canonical_event_id=row["canonical_event_id"],
        event_type=EventIntelligenceType(row["event_type"]),
        phase=EventPhase(row["phase"]),
        published_at=datetime.fromisoformat(row["published_at"]),
        ingested_at=datetime.fromisoformat(row["ingested_at"]),
        source_name=row["source_name"],
        source_kind=SourceKind(row["source_kind"]),
        source_event_id=row["source_event_id"],
        source_url=row["source_url"],
        source_reference=row["source_reference"],
        title=row["title"],
        subject=EventSubject.model_validate(json.loads(row["subject_json"])),
        payload=json.loads(row["payload_json"]),
        provenance_class=ProvenanceClass(row["provenance_class"]),
        verification_status=VerificationStatus(row["verification_status"]),
        quality_flags=[EventIntelligenceQualityFlag(item) for item in quality_raw],
        confidence=float(row["confidence"]),
        raw_payload_hash=row["raw_payload_hash"],
        raw_payload=json.loads(row["raw_payload_json"]),
        content_fingerprint=row["content_fingerprint"],
        causal_claim=bool(row["causal_claim"]),
        temporal_context_only=bool(row["temporal_context_only"]),
        revision_of_id=row["revision_of_id"],
    )
