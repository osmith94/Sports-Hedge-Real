from __future__ import annotations

from datetime import datetime
from sqlite3 import IntegrityError

from sports_hedge.event_intelligence.adapters import EventIntelligenceFeed
from sports_hedge.event_intelligence.errors import EventIntelligenceMappingError
from sports_hedge.event_intelligence.mapping import identity_tuple, normalize_fact
from sports_hedge.event_intelligence.models import (
    EventIntelligenceFact,
    EventIntelligenceRecord,
    EventIntelligenceTimeline,
    EventIntelligenceType,
    IngestResult,
    MappingIssue,
    ProvenanceClass,
)
from sports_hedge.event_intelligence.repository import SqliteEventIntelligenceRepository


class EventIntelligenceService:
    """Persist and read fixture-linked event intelligence. No arb side effects."""

    def __init__(self, repository: SqliteEventIntelligenceRepository) -> None:
        self.repository = repository

    def ingest(self, fact: EventIntelligenceFact) -> IngestResult:
        try:
            record = normalize_fact(fact)
        except EventIntelligenceMappingError as exc:
            return IngestResult(
                status="rejected",
                source_name=fact.source_name,
                source_event_id=fact.source_event_id,
                rejection=MappingIssue(
                    field=exc.field,
                    reason=exc.reason,
                    detail=exc.detail,
                    source_name=fact.source_name,
                    source_event_id=fact.source_event_id,
                ),
            )

        existing_same = self.repository.get(record.event_intelligence_id)
        if existing_same is not None:
            return IngestResult(
                status="duplicate",
                source_name=record.source_name,
                source_event_id=record.source_event_id,
                record=existing_same,
            )

        priors = self.repository.list_for_source_fact(
            source_name=record.source_name,
            source_event_id=record.source_event_id,
        )
        if priors:
            return self._handle_source_conflict(record, priors)

        persisted = self._append_or_existing(record)
        return IngestResult(
            status="created",
            source_name=record.source_name,
            source_event_id=record.source_event_id,
            record=persisted,
        )

    def ingest_many(self, facts: list[EventIntelligenceFact]) -> list[IngestResult]:
        return [self.ingest(fact) for fact in facts]

    def ingest_feed(
        self,
        feed: EventIntelligenceFeed,
        *,
        since: datetime | None = None,
    ) -> list[IngestResult]:
        return self.ingest_many(list(feed.fetch(since=since)))

    def timeline(
        self,
        canonical_event_id: str,
        *,
        event_type: EventIntelligenceType | None = None,
        source_name: str | None = None,
        provenance_class: ProvenanceClass | None = None,
    ) -> EventIntelligenceTimeline:
        records = self.repository.list_timeline(
            canonical_event_id,
            event_type=event_type,
            source_name=source_name,
            provenance_class=provenance_class,
        )
        data_class = _timeline_data_class(records)
        return EventIntelligenceTimeline(
            canonical_event_id=canonical_event_id,
            data_class=data_class,
            temporal_context_only=True,
            causal_claim=False,
            records=records,
        )

    def _handle_source_conflict(
        self,
        incoming: EventIntelligenceRecord,
        priors: list[EventIntelligenceRecord],
    ) -> IngestResult:
        prior = priors[0]
        incoming_identity = identity_tuple(incoming)
        prior_identities = {identity_tuple(item) for item in priors}
        if incoming_identity not in prior_identities:
            return IngestResult(
                status="conflict",
                source_name=incoming.source_name,
                source_event_id=incoming.source_event_id,
                prior_record=prior,
                rejection=MappingIssue(
                    field="identity",
                    reason="conflict",
                    detail=(
                        "Source fact already attached to a different fixture or subject; "
                        "refusing to reattach or guess identity"
                    ),
                    source_name=incoming.source_name,
                    source_event_id=incoming.source_event_id,
                ),
            )

        incoming.revision_of_id = prior.event_intelligence_id
        persisted = self._append_or_existing(incoming)
        return IngestResult(
            status="conflict",
            source_name=incoming.source_name,
            source_event_id=incoming.source_event_id,
            record=persisted,
            prior_record=prior,
        )

    def _append_or_existing(self, record: EventIntelligenceRecord) -> EventIntelligenceRecord:
        try:
            self.repository.append(record)
            return record
        except IntegrityError:
            return self.repository.get(record.event_intelligence_id) or record


def _timeline_data_class(records: list[EventIntelligenceRecord]) -> str:
    if not records:
        return "EMPTY"
    classes = {item.provenance_class for item in records}
    if classes == {ProvenanceClass.FIXTURE_TEST}:
        return ProvenanceClass.FIXTURE_TEST.value
    if classes == {ProvenanceClass.HISTORICAL_IMPORTED}:
        return ProvenanceClass.HISTORICAL_IMPORTED.value
    if classes == {ProvenanceClass.LIVE_READONLY_EXTERNAL}:
        return ProvenanceClass.LIVE_READONLY_EXTERNAL.value
    return "MIXED"
