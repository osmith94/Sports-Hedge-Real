from __future__ import annotations

from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator, model_validator


class EventIntelligenceType(StrEnum):
    """Conservative pre-match / news taxonomy for fixture-linked context.

    In-play score events (goals, cards, penalties) are not ingested here.
    Those remain historical match facts / market-intelligence annotations.
    """

    TEAM_SHEET_RELEASED = "team_sheet_released"
    PLAYER_OUT = "player_out"
    PLAYER_IN = "player_in"
    INJURY_NEWS = "injury_news"
    MANAGER_NEWS = "manager_news"
    LINEUP_CHANGE = "lineup_change"
    JOURNALIST_REPORT = "journalist_report"
    NEWS_ARTICLE = "news_article"
    CLUB_ANNOUNCEMENT = "club_announcement"


class EventPhase(StrEnum):
    """Separates pre-match context from in-play match facts.

    Step 9A persists only ``PRE_MATCH`` records through this seam.
    """

    PRE_MATCH = "pre_match"
    IN_PLAY = "in_play"
    POST_MATCH = "post_match"


class ProvenanceClass(StrEnum):
    FIXTURE_TEST = "fixture_test"
    HISTORICAL_IMPORTED = "historical_imported"
    LIVE_READONLY_EXTERNAL = "live_readonly_external"


class SourceKind(StrEnum):
    FIXTURE_TEST = "fixture_test"
    HISTORICAL_IMPORT = "historical_import"
    LIVE_READONLY_EXTERNAL = "live_readonly_external"


class VerificationStatus(StrEnum):
    VERIFIED = "verified"
    UNVERIFIED = "unverified"
    UNRESOLVED = "unresolved"
    REJECTED = "rejected"


class EventIntelligenceQualityFlag(StrEnum):
    MISSING_TEAM_IDENTITY = "missing_team_identity"
    MISSING_PLAYER_IDENTITY = "missing_player_identity"
    LABEL_ONLY_SUBJECT = "label_only_subject"
    AMBIGUOUS_SUBJECT = "ambiguous_subject"


class EventSubject(BaseModel):
    """Optional subject identifiers. Missing values stay missing; never inferred."""

    team_id: str | None = None
    team_label: str | None = None
    player_id: str | None = None
    player_label: str | None = None
    manager_id: str | None = None
    manager_label: str | None = None

    @model_validator(mode="after")
    def strip_empty(self) -> "EventSubject":
        for field_name in type(self).model_fields:
            value = getattr(self, field_name)
            if isinstance(value, str):
                stripped = value.strip()
                setattr(self, field_name, stripped or None)
        return self


class EventIntelligenceFact(BaseModel):
    """Provider-neutral ingest envelope. Tests and later approved adapters use this."""

    source_name: str = Field(min_length=1)
    source_event_id: str = Field(min_length=1)
    event_type: str = Field(min_length=1)
    canonical_event_id: str | None = None
    published_at: datetime
    ingested_at: datetime
    provenance_class: ProvenanceClass
    source_kind: SourceKind | None = None
    title: str | None = None
    source_url: str | None = None
    source_reference: str | None = None
    subject: EventSubject = Field(default_factory=EventSubject)
    payload: dict[str, Any] = Field(default_factory=dict)
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)

    @field_validator("published_at", "ingested_at")
    @classmethod
    def require_aware_timestamps(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            raise ValueError(
                "timezone-aware datetime required; refuse to assume UTC. "
                "Normalize at the source adapter with an explicit timezone"
            )
        return value.astimezone(UTC)


class EventIntelligenceRecord(BaseModel):
    """Canonical fixture-linked event-intelligence row. Temporal context only."""

    event_intelligence_id: str
    canonical_event_id: str
    event_type: EventIntelligenceType
    phase: EventPhase = EventPhase.PRE_MATCH
    published_at: datetime
    ingested_at: datetime
    source_name: str
    source_kind: SourceKind
    source_event_id: str
    source_url: str | None = None
    source_reference: str | None = None
    title: str
    subject: EventSubject = Field(default_factory=EventSubject)
    payload: dict[str, Any] = Field(default_factory=dict)
    provenance_class: ProvenanceClass
    verification_status: VerificationStatus
    quality_flags: list[EventIntelligenceQualityFlag] = Field(default_factory=list)
    confidence: float = Field(ge=0.0, le=1.0)
    raw_payload_hash: str
    raw_payload: dict[str, Any] = Field(default_factory=dict)
    content_fingerprint: str
    causal_claim: bool = False
    temporal_context_only: bool = True
    revision_of_id: str | None = None

    @model_validator(mode="after")
    def enforce_honesty_and_phase(self) -> "EventIntelligenceRecord":
        if self.published_at.tzinfo is None:
            self.published_at = self.published_at.replace(tzinfo=UTC)
        if self.ingested_at.tzinfo is None:
            self.ingested_at = self.ingested_at.replace(tzinfo=UTC)
        if self.causal_claim:
            raise ValueError("Event intelligence is temporal context; causal_claim must stay false")
        if not self.temporal_context_only:
            raise ValueError("Event intelligence records are temporal context only")
        if self.phase is not EventPhase.PRE_MATCH:
            raise ValueError("This seam stores pre-match/news context only, not in-play scores")
        return self


class MappingIssue(BaseModel):
    field: str
    reason: str
    detail: str
    source_name: str
    source_event_id: str


class IngestResult(BaseModel):
    status: Literal["created", "duplicate", "rejected", "conflict"]
    source_name: str
    source_event_id: str
    record: EventIntelligenceRecord | None = None
    prior_record: EventIntelligenceRecord | None = None
    rejection: MappingIssue | None = None


class EventIntelligenceTimeline(BaseModel):
    canonical_event_id: str
    data_class: str
    temporal_context_only: bool = True
    causal_claim: bool = False
    records: list[EventIntelligenceRecord] = Field(default_factory=list)
