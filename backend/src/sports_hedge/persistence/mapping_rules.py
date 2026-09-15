"""Additive SQLite store for operator-verified mapping rules and review audit.

Rules are versioned and disable/revoke without rewriting historical scan rows.
Reviews persist even when the verdict does not activate a rule.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path
from typing import Any, Iterator
from uuid import uuid4

from sports_hedge.config import get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.learned_rules import (
    MappingFieldScope,
    MappingGuardrails,
    MappingRule,
    MappingRuleSource,
    MappingRuleType,
    MappingVerdict,
)

_CREATE_SQL = """
CREATE TABLE IF NOT EXISTS mapping_rules (
    rule_id TEXT PRIMARY KEY,
    version INTEGER NOT NULL,
    enabled INTEGER NOT NULL,
    revoked INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    operator TEXT NOT NULL,
    source TEXT NOT NULL,
    rule_type TEXT NOT NULL,
    venue TEXT NOT NULL,
    field_scope TEXT NOT NULL,
    raw_pattern TEXT NOT NULL,
    canonical_transformation TEXT NOT NULL,
    guardrails_json TEXT NOT NULL,
    evidence TEXT NOT NULL,
    evidence_raw_json TEXT NOT NULL,
    review_id TEXT,
    notes TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS mapping_rule_audit (
    audit_id INTEGER PRIMARY KEY AUTOINCREMENT,
    rule_id TEXT NOT NULL,
    version INTEGER NOT NULL,
    action TEXT NOT NULL,
    at TEXT NOT NULL,
    operator TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS mapping_reviews (
    review_id TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    operator TEXT NOT NULL,
    source TEXT NOT NULL,
    verdict TEXT,
    prompt_text TEXT NOT NULL,
    chatgpt_text TEXT,
    proposed_rule_json TEXT,
    confirmed_at TEXT,
    activated_rule_id TEXT,
    activation_blocked_reason TEXT,
    evidence_json TEXT NOT NULL
);
"""


class SqliteMappingRuleStore:
    """File-backed additive mapping-review / learned-rule store."""

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
        connection.executescript(_CREATE_SQL)
        columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(mapping_reviews)")
        }
        if "activation_blocked_reason" not in columns:
            connection.execute(
                "ALTER TABLE mapping_reviews ADD COLUMN activation_blocked_reason TEXT"
            )

    def list_enabled(self) -> list[MappingRule]:
        with self._connect() as connection:
            rows = connection.execute(
                """
                SELECT * FROM mapping_rules
                WHERE enabled = 1 AND revoked = 0
                ORDER BY created_at ASC, rule_id ASC
                """
            ).fetchall()
        return [_rule_from_row(row) for row in rows]

    def list_rules(self, *, include_disabled: bool = True) -> list[MappingRule]:
        sql = "SELECT * FROM mapping_rules ORDER BY created_at ASC, rule_id ASC"
        if not include_disabled:
            sql = """
                SELECT * FROM mapping_rules
                WHERE enabled = 1 AND revoked = 0
                ORDER BY created_at ASC, rule_id ASC
                """
        with self._connect() as connection:
            rows = connection.execute(sql).fetchall()
        return [_rule_from_row(row) for row in rows]

    def get_rule(self, rule_id: str) -> MappingRule | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM mapping_rules WHERE rule_id = ?",
                (rule_id,),
            ).fetchone()
        if row is None:
            return None
        return _rule_from_row(row)

    def save_rule(self, rule: MappingRule, *, action: str = "created") -> MappingRule:
        existing = self.get_rule(rule.rule_id)
        now = datetime.now(UTC)
        if existing is not None:
            rule = rule.model_copy(
                update={
                    "version": existing.version + 1,
                    "created_at": existing.created_at,
                    "updated_at": now,
                }
            )
            action = "version_bumped" if action == "created" else action
        elif rule.updated_at is None:
            rule = rule.model_copy(update={"updated_at": now})
        payload = _rule_to_row(rule)
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO mapping_rules (
                    rule_id, version, enabled, revoked, created_at, updated_at,
                    operator, source, rule_type, venue, field_scope, raw_pattern,
                    canonical_transformation, guardrails_json, evidence,
                    evidence_raw_json, review_id, notes
                ) VALUES (
                    :rule_id, :version, :enabled, :revoked, :created_at, :updated_at,
                    :operator, :source, :rule_type, :venue, :field_scope, :raw_pattern,
                    :canonical_transformation, :guardrails_json, :evidence,
                    :evidence_raw_json, :review_id, :notes
                )
                ON CONFLICT(rule_id) DO UPDATE SET
                    version = excluded.version,
                    enabled = excluded.enabled,
                    revoked = excluded.revoked,
                    updated_at = excluded.updated_at,
                    operator = excluded.operator,
                    source = excluded.source,
                    rule_type = excluded.rule_type,
                    venue = excluded.venue,
                    field_scope = excluded.field_scope,
                    raw_pattern = excluded.raw_pattern,
                    canonical_transformation = excluded.canonical_transformation,
                    guardrails_json = excluded.guardrails_json,
                    evidence = excluded.evidence,
                    evidence_raw_json = excluded.evidence_raw_json,
                    review_id = excluded.review_id,
                    notes = excluded.notes
                """,
                payload,
            )
            connection.execute(
                """
                INSERT INTO mapping_rule_audit (
                    rule_id, version, action, at, operator, payload_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    rule.rule_id,
                    rule.version,
                    action,
                    now.isoformat(),
                    rule.operator,
                    json.dumps(payload, default=str),
                ),
            )
        return rule

    def disable_rule(self, rule_id: str, *, operator: str, revoke: bool = False) -> MappingRule:
        rule = self.get_rule(rule_id)
        if rule is None:
            raise KeyError(rule_id)
        updated = rule.model_copy(
            update={
                "enabled": False,
                "revoked": bool(revoke or rule.revoked),
                "updated_at": datetime.now(UTC),
                "operator": operator,
            }
        )
        action = "revoked" if updated.revoked else "disabled"
        return self.save_rule(updated, action=action)

    def list_audit(self, rule_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT * FROM mapping_rule_audit ORDER BY audit_id ASC"
        params: tuple[Any, ...] = ()
        if rule_id is not None:
            sql = "SELECT * FROM mapping_rule_audit WHERE rule_id = ? ORDER BY audit_id ASC"
            params = (rule_id,)
        with self._connect() as connection:
            rows = connection.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def save_review(
        self,
        *,
        operator: str,
        source: MappingRuleSource,
        prompt_text: str,
        evidence: dict[str, Any],
        verdict: MappingVerdict | None = None,
        chatgpt_text: str | None = None,
        proposed_rule: MappingRule | None = None,
        confirmed_at: datetime | None = None,
        activated_rule_id: str | None = None,
        review_id: str | None = None,
        activation_blocked_reason: str | None = None,
    ) -> dict[str, Any]:
        review_id = review_id or f"maprev:{uuid4().hex[:16]}"
        created = datetime.now(UTC)
        row = {
            "review_id": review_id,
            "created_at": created.isoformat(),
            "operator": operator,
            "source": source.value,
            "verdict": None if verdict is None else verdict.value,
            "prompt_text": prompt_text,
            "chatgpt_text": chatgpt_text,
            "proposed_rule_json": None
            if proposed_rule is None
            else proposed_rule.model_dump_json(),
            "confirmed_at": None if confirmed_at is None else confirmed_at.isoformat(),
            "activated_rule_id": activated_rule_id,
            "activation_blocked_reason": activation_blocked_reason,
            "evidence_json": json.dumps(evidence, default=str),
        }
        with self._connect() as connection:
            existing = connection.execute(
                "SELECT review_id FROM mapping_reviews WHERE review_id = ?",
                (review_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    """
                    INSERT INTO mapping_reviews (
                        review_id, created_at, operator, source, verdict, prompt_text,
                        chatgpt_text, proposed_rule_json, confirmed_at, activated_rule_id,
                        activation_blocked_reason, evidence_json
                    ) VALUES (
                        :review_id, :created_at, :operator, :source, :verdict, :prompt_text,
                        :chatgpt_text, :proposed_rule_json, :confirmed_at, :activated_rule_id,
                        :activation_blocked_reason, :evidence_json
                    )
                    """,
                    row,
                )
            else:
                connection.execute(
                    """
                    UPDATE mapping_reviews SET
                        operator = :operator,
                        source = :source,
                        verdict = :verdict,
                        prompt_text = :prompt_text,
                        chatgpt_text = :chatgpt_text,
                        proposed_rule_json = :proposed_rule_json,
                        confirmed_at = :confirmed_at,
                        activated_rule_id = :activated_rule_id,
                        activation_blocked_reason = :activation_blocked_reason,
                        evidence_json = :evidence_json
                    WHERE review_id = :review_id
                    """,
                    row,
                )
        return row

    def get_review(self, review_id: str) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM mapping_reviews WHERE review_id = ?",
                (review_id,),
            ).fetchone()
        if row is None:
            return None
        return dict(row)

    def close(self) -> None:
        with self._lock:
            if self._shared_connection is not None:
                self._shared_connection.close()
                self._shared_connection = None


def _rule_to_row(rule: MappingRule) -> dict[str, Any]:
    updated = rule.updated_at or rule.created_at
    return {
        "rule_id": rule.rule_id,
        "version": rule.version,
        "enabled": 1 if rule.enabled else 0,
        "revoked": 1 if rule.revoked else 0,
        "created_at": rule.created_at.isoformat(),
        "updated_at": updated.isoformat(),
        "operator": rule.operator,
        "source": rule.source.value,
        "rule_type": rule.rule_type.value,
        "venue": rule.venue.value,
        "field_scope": rule.field_scope.value,
        "raw_pattern": rule.raw_pattern,
        "canonical_transformation": rule.canonical_transformation,
        "guardrails_json": rule.guardrails.model_dump_json(),
        "evidence": rule.evidence,
        "evidence_raw_json": json.dumps(rule.evidence_raw, default=str),
        "review_id": rule.review_id,
        "notes": rule.notes,
    }


def _rule_from_row(row: sqlite3.Row) -> MappingRule:
    guardrails = MappingGuardrails.model_validate_json(row["guardrails_json"])
    evidence_raw = json.loads(row["evidence_raw_json"] or "{}")
    return MappingRule(
        rule_id=row["rule_id"],
        version=int(row["version"]),
        enabled=bool(row["enabled"]),
        revoked=bool(row["revoked"]),
        created_at=datetime.fromisoformat(row["created_at"]),
        updated_at=datetime.fromisoformat(row["updated_at"]),
        operator=row["operator"],
        source=MappingRuleSource(row["source"]),
        rule_type=MappingRuleType(row["rule_type"]),
        venue=VenueName(row["venue"]),
        field_scope=MappingFieldScope(row["field_scope"]),
        raw_pattern=row["raw_pattern"],
        canonical_transformation=row["canonical_transformation"],
        guardrails=guardrails,
        evidence=row["evidence"],
        evidence_raw=evidence_raw,
        review_id=row["review_id"],
        notes=row["notes"] or "",
    )


@lru_cache
def get_mapping_rule_store() -> SqliteMappingRuleStore:
    settings = get_settings()
    database = settings.mapping_rules_db_path
    if database != ":memory:":
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteMappingRuleStore(database)
