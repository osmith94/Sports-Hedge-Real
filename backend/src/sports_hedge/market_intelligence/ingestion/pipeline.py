from __future__ import annotations

from datetime import datetime
from hashlib import sha256
from sqlite3 import IntegrityError

from sports_hedge.market_intelligence.ingestion.contracts import (
    IngestResult,
    MarketEventFeed,
    NormalizedMarketEvent,
    ProviderEventRecord,
)
from sports_hedge.market_intelligence.ingestion.mapping import (
    EventMappingError,
    normalize_provider_event,
)
from sports_hedge.market_intelligence.models import MarketEventAnnotation
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository


class MarketEventIngestionPipeline:
    """Idempotent mapping from provider-neutral events to timeline annotations.

    Annotations are temporal context only. This pipeline never infers that an
    event caused a market move.
    """

    def __init__(self, repository: SqliteMarketIntelligenceRepository) -> None:
        self.repository = repository

    def ingest(self, record: ProviderEventRecord) -> IngestResult:
        try:
            normalized = normalize_provider_event(record)
        except EventMappingError as exc:
            return IngestResult(
                status="rejected",
                provider=record.provider,
                source_event_id=record.source_event_id,
                rejection=exc.issue,
            )

        annotation = annotation_from_normalized(normalized)
        existing = self.repository.get_annotation(annotation.annotation_id)
        if existing is not None:
            return IngestResult(
                status="duplicate",
                provider=normalized.provider,
                source_event_id=normalized.source_event_id,
                annotation=existing,
            )

        try:
            self.repository.append_annotation(annotation)
        except IntegrityError:
            duplicate = self.repository.get_annotation(annotation.annotation_id)
            return IngestResult(
                status="duplicate",
                provider=normalized.provider,
                source_event_id=normalized.source_event_id,
                annotation=duplicate or annotation,
            )

        return IngestResult(
            status="created",
            provider=normalized.provider,
            source_event_id=normalized.source_event_id,
            annotation=annotation,
        )

    def ingest_many(self, records: list[ProviderEventRecord]) -> list[IngestResult]:
        return [self.ingest(record) for record in records]

    def ingest_feed(
        self,
        feed: MarketEventFeed,
        *,
        since: datetime | None = None,
    ) -> list[IngestResult]:
        return self.ingest_many(list(feed.fetch(since=since)))


def annotation_from_normalized(event: NormalizedMarketEvent) -> MarketEventAnnotation:
    return MarketEventAnnotation(
        annotation_id=_deterministic_annotation_id(event.ingestion_key),
        canonical_event_id=event.canonical_event_id,
        occurred_at=event.source_occurred_at,
        category=event.category,
        source=event.provider,
        confidence=event.confidence,
        title=event.title,
        source_url=event.source_url,
        metadata=event.metadata,
    )


def _deterministic_annotation_id(key: str) -> str:
    digest = sha256(key.encode("utf-8")).hexdigest()[:24]
    return f"ann:{digest}"
