from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from sports_hedge.config import get_settings
from sports_hedge.event_intelligence.models import (
    EventIntelligenceTimeline,
    EventIntelligenceType,
    ProvenanceClass,
)
from sports_hedge.event_intelligence.repository import SqliteEventIntelligenceRepository
from sports_hedge.event_intelligence.service import EventIntelligenceService

router = APIRouter(prefix="/research/event-intelligence", tags=["event-intelligence"])


@lru_cache
def get_event_intelligence_service() -> EventIntelligenceService:
    settings = get_settings()
    database = settings.event_intelligence_db_path
    if database != ":memory:":
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
    repository = SqliteEventIntelligenceRepository(database)
    return EventIntelligenceService(repository)


@router.get("/event-types", response_model=list[str])
def event_types() -> list[str]:
    return [item.value for item in EventIntelligenceType]


@router.get(
    "/fixtures/{canonical_event_id}/timeline",
    response_model=EventIntelligenceTimeline,
)
def fixture_timeline(
    canonical_event_id: str,
    event_type: EventIntelligenceType | None = None,
    source_name: str | None = Query(default=None),
    provenance_class: ProvenanceClass | None = None,
    service: EventIntelligenceService = Depends(get_event_intelligence_service),
) -> EventIntelligenceTimeline:
    """Read-only chronological context for one canonical fixture. No betting actions."""

    return service.timeline(
        canonical_event_id,
        event_type=event_type,
        source_name=source_name,
        provenance_class=provenance_class,
    )
