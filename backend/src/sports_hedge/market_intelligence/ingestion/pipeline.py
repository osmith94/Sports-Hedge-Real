from __future__ import annotations

import json
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
    event caused a market move. Identical source content is stored once;
    a later payload for the same source id is appended as a conflict/revision
    without mutating the earlier observation.
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

        fingerprint = source_content_fingerprint(normalized)
        _attach_fingerprint(normalized, fingerprint)
        annotation = annotation_from_normalized(normalized)
        existing_same_content = self.repository.get_annotation(annotation.annotation_id)
        if existing_same_content is not None:
            return IngestResult(
                status="duplicate",
                provider=normalized.provider,
                source_event_id=normalized.source_event_id,
                annotation=existing_same_content,
            )

        priors = self.repository.list_annotations_for_ingestion_key(normalized.ingestion_key)
        if priors:
            _attach_revision(normalized, priors)
            annotation = annotation_from_normalized(normalized)
            persisted = _append_or_existing(self.repository, annotation)
            return IngestResult(
                status="conflict",
                provider=normalized.provider,
                source_event_id=normalized.source_event_id,
                annotation=persisted,
                prior_annotation=priors[0],
            )

        persisted = _append_or_existing(self.repository, annotation)
        return IngestResult(
            status="created",
            provider=normalized.provider,
            source_event_id=normalized.source_event_id,
            annotation=persisted,
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
    fingerprint = event.metadata.get("ingestion", {}).get("content_fingerprint")
    if not isinstance(fingerprint, str) or not fingerprint:
        fingerprint = source_content_fingerprint(event)
        _attach_fingerprint(event, fingerprint)
    return MarketEventAnnotation(
        annotation_id=_deterministic_annotation_id(event.ingestion_key, fingerprint),
        canonical_event_id=event.canonical_event_id,
        occurred_at=event.source_occurred_at,
        category=event.category,
        source=event.provider,
        confidence=event.confidence,
        title=event.title,
        source_url=event.source_url,
        metadata=event.metadata,
    )


def source_content_fingerprint(event: NormalizedMarketEvent) -> str:
    ingestion = event.metadata.get("ingestion", {})
    payload = {
        "canonical_event_id": event.canonical_event_id,
        "category": event.category.value,
        "confidence": event.confidence,
        "home_team": ingestion.get("home_team"),
        "away_team": ingestion.get("away_team"),
        "player_ref": event.player_ref,
        "provider_payload": event.metadata.get("provider_payload", {}),
        "source_occurred_at": event.source_occurred_at.isoformat(),
        "source_reference": event.source_reference,
        "source_url": event.source_url,
        "team_ref": event.team_ref,
        "title": event.title,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return sha256(blob.encode()).hexdigest()[:32]


def _attach_fingerprint(event: NormalizedMarketEvent, fingerprint: str) -> None:
    ingestion = dict(event.metadata.get("ingestion", {}))
    ingestion["content_fingerprint"] = fingerprint
    event.metadata["ingestion"] = ingestion


def _attach_revision(
    event: NormalizedMarketEvent,
    priors: list[MarketEventAnnotation],
) -> None:
    ingestion = dict(event.metadata.get("ingestion", {}))
    ingestion["revision"] = True
    ingestion["corrects_annotation_id"] = priors[0].annotation_id
    ingestion["prior_annotation_ids"] = [item.annotation_id for item in priors]
    ingestion["prior_content_fingerprints"] = [
        item.metadata.get("ingestion", {}).get("content_fingerprint") for item in priors
    ]
    ingestion["causal_claim"] = False
    ingestion["temporal_context_only"] = True
    event.metadata["ingestion"] = ingestion


def _append_or_existing(
    repository: SqliteMarketIntelligenceRepository,
    annotation: MarketEventAnnotation,
) -> MarketEventAnnotation:
    try:
        repository.append_annotation(annotation)
        return annotation
    except IntegrityError:
        return repository.get_annotation(annotation.annotation_id) or annotation


def _deterministic_annotation_id(key: str, fingerprint: str) -> str:
    digest = sha256(f"{key}|{fingerprint}".encode()).hexdigest()[:24]
    return f"ann:{digest}"
