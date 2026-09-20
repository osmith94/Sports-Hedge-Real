"""Open UNIVERSE generation checkpoint contract.

Successful-work accounting, resume cursor and evaluated IDs belong to one
generation until that sweep genuinely completes or an explicit reset/invalid
checkpoint proves it cannot be resumed safely.

Durable payloads are compact resume state only. Raw venue event bodies and
full CollectionReport snapshots stay in-memory for the live process and are
never written to SQLite.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from sports_hedge.application.collector import CollectionReport

LOGGER = logging.getLogger(__name__)

UNIVERSE_PROVIDER_BACKOFF_SECONDS = (2.0, 5.0, 10.0, 20.0, 30.0)
UNIVERSE_WORK_RETRY_BACKOFF_SECONDS = (2.0, 5.0, 10.0)

# Compact durable resume contract. Bump UNIVERSE_CHECKPOINT_SEMANTICS_VERSION
# when skip IDs, work-unit resume, or the persisted field set would mis-handle
# an older checkpoint. Unversioned (#304) and v1 fat payloads parse as the
# legacy constant so a 1→2 bump fail-closes them instead of restoring 50MB
# venue/report blobs onto the event loop.
LEGACY_UNVERSIONED_CHECKPOINT_SEMANTICS_VERSION = 1
UNIVERSE_CHECKPOINT_SEMANTICS_VERSION = 2

# Hard cap for encoded durable JSON. Realistic synthetic work-sets of a few
# hundred compact SweepWorkUnit/SeriesWorkUnit rows stay well under this;
# raw venue bodies and CollectionReport dumps do not.
UNIVERSE_CHECKPOINT_MAX_ENCODED_BYTES = 256 * 1024
FAT_CHECKPOINT_PAYLOAD_KEYS = frozenset({"report", "discovery_snapshot"})

SWEEP_PENDING = "pending"
SWEEP_RUNNING = "running"
SWEEP_EVALUATED = "evaluated"
SWEEP_OK = "ok"
SWEEP_RETRY_WAIT = "retry_wait"
SWEEP_FINAL_FAILED = "final_failed"
SWEEP_SKIPPED_UNSUPPORTED = "skipped_unsupported"
SWEEP_STALE_ORPHAN = "stale_orphan"
STALE_ORPHAN_REASON = "absent_from_authoritative_cluster_set"
SWEEP_TERMINAL_STATES = frozenset(
    {
        SWEEP_EVALUATED,
        SWEEP_FINAL_FAILED,
        SWEEP_SKIPPED_UNSUPPORTED,
        SWEEP_STALE_ORPHAN,
    }
)
SERIES_TERMINAL_STATES = frozenset(
    {SWEEP_OK, SWEEP_FINAL_FAILED, SWEEP_SKIPPED_UNSUPPORTED}
)


class SweepWorkUnit(BaseModel):
    canonical_id: str
    state: str = SWEEP_PENDING
    reason: str | None = None
    attempt_count: int = 0
    last_attempted_at: datetime | None = None
    next_retry_at: datetime | None = None
    retryable: bool = False
    provider: str | None = None
    series: str | None = None


class SeriesWorkUnit(BaseModel):
    venue: str
    series: str
    state: str = SWEEP_PENDING
    reason: str | None = None
    attempt_count: int = 0
    last_attempted_at: datetime | None = None
    next_retry_at: datetime | None = None
    retryable: bool = False
    event_count: int = 0


def series_work_key(venue: str, series: str) -> str:
    return f"{venue}:{series}"


class UniverseCheckpointTooLarge(ValueError):
    """Encoded durable checkpoint exceeded the compact size cap."""


class UniverseGenerationCheckpoint(BaseModel):
    """Resume-critical UNIVERSE generation state. Compact by contract."""

    model_config = ConfigDict(extra="ignore")

    generation_id: int = Field(ge=1)
    generation_started_at: datetime
    successful_work_used_s: float = Field(default=0.0, ge=0)
    resume_cursor: str | None = None
    evaluated_ids: list[str] = Field(default_factory=list)
    next_universe_due: datetime | None = None
    provider_failure_count: int = Field(default=0, ge=0)
    retry_at: datetime | None = None
    budget_paused: bool = False
    updated_at: datetime
    sweep_id: str | None = None
    discovered_total: int = Field(default=0, ge=0)
    failed_ids: dict[str, str] = Field(default_factory=dict)
    skipped_ids: dict[str, str] = Field(default_factory=dict)
    last_successful_fixture: str | None = None
    matched_fixtures: int = Field(default=0, ge=0)
    equivalent_markets: int = Field(default=0, ge=0)
    near_count: int = Field(default=0, ge=0)
    positive_count: int = Field(default=0, ge=0)
    qualifying_count: int = Field(default=0, ge=0)
    hot_promotions: int = Field(default=0, ge=0)
    raw_events_by_venue: dict[str, int] = Field(default_factory=dict)
    work_units: dict[str, SweepWorkUnit] = Field(default_factory=dict)
    series_work: dict[str, SeriesWorkUnit] = Field(default_factory=dict)
    semantics_version: int = Field(default=LEGACY_UNVERSIONED_CHECKPOINT_SEMANTICS_VERSION)
    selected_competition_codes: list[str] = Field(default_factory=list)
    scope_version: int = Field(default=0, ge=0)
    superseded: bool = False


_VENUE_SNAPSHOT_KEYS = frozenset({"matchbook", "polymarket", "kalshi"})


def discovery_event_snapshot(snapshot: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Keep only venue event lists. Series diagnostics are not events."""

    cleaned: dict[str, list[dict[str, Any]]] = {}
    for key in _VENUE_SNAPSHOT_KEYS:
        rows = snapshot.get(key) or []
        if isinstance(rows, list):
            cleaned[key] = [item for item in rows if isinstance(item, dict)]
        else:
            cleaned[key] = []
    return cleaned


def merge_series_reports(
    existing: dict[str, list[dict[str, Any]]] | None,
    incoming: dict[str, list[dict[str, Any]]] | None,
) -> dict[str, list[dict[str, Any]]]:
    """Replace per-series rows without discarding earlier successful series."""

    merged = {key: list(value) for key, value in (existing or {}).items()}
    for venue, rows in (incoming or {}).items():
        by_series: dict[str, dict[str, Any]] = {}
        for item in merged.get(venue, []):
            series = str(item.get("series") or "").strip()
            if series:
                by_series[series] = item
        for item in rows or []:
            series = str(item.get("series") or "").strip()
            if series:
                by_series[series] = item
        merged[venue] = list(by_series.values()) if by_series else list(rows or [])
    return merged


def universe_work_retry_backoff_seconds(attempt_count: int) -> float:
    """Capped isolated-work backoff. Attempt count may grow; delay does not."""

    if attempt_count <= 0:
        return UNIVERSE_WORK_RETRY_BACKOFF_SECONDS[0]
    index = min(attempt_count - 1, len(UNIVERSE_WORK_RETRY_BACKOFF_SECONDS) - 1)
    return float(UNIVERSE_WORK_RETRY_BACKOFF_SECONDS[index])


def universe_provider_backoff_seconds(failure_count: int) -> float:
    if failure_count <= 0:
        return UNIVERSE_PROVIDER_BACKOFF_SECONDS[0]
    index = min(failure_count - 1, len(UNIVERSE_PROVIDER_BACKOFF_SECONDS) - 1)
    return float(UNIVERSE_PROVIDER_BACKOFF_SECONDS[index])


def collection_report_snapshot(report: CollectionReport) -> dict[str, Any]:
    return report.model_dump(mode="json")


def collection_report_from_snapshot(payload: dict[str, Any] | None) -> CollectionReport | None:
    if not payload:
        return None
    try:
        return CollectionReport.model_validate(payload)
    except ValidationError:
        LOGGER.warning("universe checkpoint report failed validation; not restoring roster")
        return None


def durable_checkpoint_has_fat_payload(payload: dict[str, Any] | None) -> bool:
    """True when a payload still carries v1 report/raw-event blobs."""

    if not payload:
        return False
    for key in FAT_CHECKPOINT_PAYLOAD_KEYS:
        value = payload.get(key)
        if value:
            return True
    return False


def encode_durable_universe_checkpoint(payload: dict[str, Any]) -> str:
    """JSON-encode a compact checkpoint and fail closed over the size cap."""

    encoded = json.dumps(payload, separators=(",", ":"), default=str)
    size = len(encoded.encode("utf-8"))
    if size > UNIVERSE_CHECKPOINT_MAX_ENCODED_BYTES:
        raise UniverseCheckpointTooLarge(
            f"universe checkpoint encoded size {size} exceeds "
            f"{UNIVERSE_CHECKPOINT_MAX_ENCODED_BYTES} byte compact cap"
        )
    return encoded


def checkpoint_from_payload(payload: dict[str, Any] | None) -> UniverseGenerationCheckpoint | None:
    if not payload:
        return None
    if durable_checkpoint_has_fat_payload(payload):
        LOGGER.warning("universe checkpoint has fat report/discovery payload; not resuming unsafely")
        return None
    try:
        checkpoint = UniverseGenerationCheckpoint.model_validate(payload)
    except ValidationError:
        LOGGER.warning("universe checkpoint failed validation; not resuming unsafely")
        return None
    if checkpoint.semantics_version != UNIVERSE_CHECKPOINT_SEMANTICS_VERSION:
        LOGGER.warning(
            "universe checkpoint semantics_version=%s incompatible with runtime %s; "
            "not resuming unsafely",
            checkpoint.semantics_version,
            UNIVERSE_CHECKPOINT_SEMANTICS_VERSION,
        )
        return None
    return checkpoint
