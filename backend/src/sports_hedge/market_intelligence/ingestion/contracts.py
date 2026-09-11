from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, Literal, Protocol

from pydantic import BaseModel, Field, model_validator

from sports_hedge.market_intelligence.models import AnnotationCategory, MarketEventAnnotation


class ProviderEventRecord(BaseModel):
    """Provider-neutral envelope for a timestamped sports or news event.

    ``source_occurred_at`` is the event time from the originating source.
    ``retrieved_at`` is when Sports Hedge obtained the record. Event Reaction
    analytics must align to the source timestamp, not retrieval time.
    """

    provider: str = Field(min_length=1)
    source_event_id: str = Field(min_length=1)
    source_occurred_at: datetime
    retrieved_at: datetime
    category: str = Field(min_length=1)
    canonical_event_id: str | None = None
    team_ref: str | None = None
    player_ref: str | None = None
    home_team: str | None = None
    away_team: str | None = None
    title: str | None = None
    source_url: str | None = None
    source_reference: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    payload: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def ensure_timezones(self) -> ProviderEventRecord:
        if self.source_occurred_at.tzinfo is None:
            self.source_occurred_at = self.source_occurred_at.replace(tzinfo=UTC)
        if self.retrieved_at.tzinfo is None:
            self.retrieved_at = self.retrieved_at.replace(tzinfo=UTC)
        return self


class NormalizedMarketEvent(BaseModel):
    """Mapped event ready to persist as a Market Intelligence annotation."""

    provider: str
    source_event_id: str
    source_occurred_at: datetime
    retrieved_at: datetime
    category: AnnotationCategory
    canonical_event_id: str
    title: str
    confidence: float = Field(ge=0.0, le=1.0)
    team_ref: str | None = None
    player_ref: str | None = None
    source_url: str | None = None
    source_reference: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @property
    def ingestion_key(self) -> str:
        return ingestion_key(self.provider, self.source_event_id)


class MappingIssue(BaseModel):
    field: str
    reason: str
    detail: str
    provider: str
    source_event_id: str


class IngestResult(BaseModel):
    status: Literal["created", "duplicate", "rejected"]
    provider: str
    source_event_id: str
    annotation: MarketEventAnnotation | None = None
    rejection: MappingIssue | None = None

    @property
    def created(self) -> bool:
        return self.status == "created"


class MarketEventFeed(Protocol):
    """Adapter seam for official sports-data and news providers."""

    @property
    def provider(self) -> str: ...

    def fetch(self, *, since: datetime | None = None) -> Sequence[ProviderEventRecord]: ...


def ingestion_key(provider: str, source_event_id: str) -> str:
    return f"{provider.strip().casefold()}:{source_event_id.strip()}"
