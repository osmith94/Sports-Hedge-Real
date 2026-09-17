"""Open UNIVERSE generation checkpoint contract.

Successful-work accounting, resume cursor and evaluated IDs belong to one
generation until that sweep genuinely completes or an explicit reset/invalid
checkpoint proves it cannot be resumed safely.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from sports_hedge.application.collector import CollectionReport

LOGGER = logging.getLogger(__name__)

UNIVERSE_PROVIDER_BACKOFF_SECONDS = (2.0, 5.0, 10.0, 20.0, 30.0)


class UniverseGenerationCheckpoint(BaseModel):
    generation_id: int = Field(ge=1)
    generation_started_at: datetime
    successful_work_used_s: float = Field(default=0.0, ge=0)
    resume_cursor: str | None = None
    evaluated_ids: list[str] = Field(default_factory=list)
    next_universe_due: datetime | None = None
    provider_failure_count: int = Field(default=0, ge=0)
    retry_at: datetime | None = None
    budget_paused: bool = False
    report: dict[str, Any] | None = None
    updated_at: datetime


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


def checkpoint_from_payload(payload: dict[str, Any] | None) -> UniverseGenerationCheckpoint | None:
    if not payload:
        return None
    try:
        return UniverseGenerationCheckpoint.model_validate(payload)
    except ValidationError:
        LOGGER.warning("universe checkpoint failed validation; not resuming unsafely")
        return None
