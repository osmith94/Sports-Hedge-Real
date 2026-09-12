from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sports_hedge.api.main import app
from sports_hedge.api.market_intelligence import get_market_intelligence_service
from sports_hedge.domain.football import MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.ingestion.adapters import (
    OfficialSportsDataFeed,
    ProviderNotConfiguredError,
)
from sports_hedge.market_intelligence.ingestion.contracts import (
    ProviderEventRecord,
    localize_naive_datetime,
)
from sports_hedge.market_intelligence.ingestion.fixtures import (
    CANONICAL_EVENT_ID,
    FIXTURE_KICKOFF,
    newcastle_arsenal_fixture_feed,
    team_sheet_yellow_red_timeline,
)
from sports_hedge.market_intelligence.ingestion.pipeline import MarketEventIngestionPipeline
from sports_hedge.market_intelligence.models import AnnotationCategory, MarketSnapshot
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService


def make_record(**overrides: object) -> ProviderEventRecord:
    payload = {
        "provider": "fixture_test",
        "source_event_id": "src-1",
        "source_occurred_at": datetime(2026, 9, 12, 15, 0, tzinfo=UTC),
        "retrieved_at": datetime(2026, 9, 12, 19, 0, tzinfo=UTC),
        "category": "TEAM_SHEET",
        "canonical_event_id": CANONICAL_EVENT_ID,
        "title": "Starting XI announced",
        "source_url": "https://example.test/team-sheet",
        "confidence": 0.99,
    }
    payload.update(overrides)
    return ProviderEventRecord.model_validate(payload)


def test_team_sheet_feed_creates_exactly_one_annotation() -> None:
    repository = SqliteMarketIntelligenceRepository()
    pipeline = MarketEventIngestionPipeline(repository)
    record = make_record()

    first = pipeline.ingest(record)
    second = pipeline.ingest(record)
    stored = repository.list_annotations(canonical_event_id=CANONICAL_EVENT_ID)

    assert first.status == "created"
    assert first.annotation is not None
    assert first.annotation.category == AnnotationCategory.TEAM_SHEET
    assert first.annotation.canonical_event_id == CANONICAL_EVENT_ID
    assert len(stored) == 1
    assert stored[0].annotation_id == first.annotation.annotation_id
    assert second.status == "duplicate"
    assert second.annotation is not None
    assert second.annotation.annotation_id == first.annotation.annotation_id
    repository.close()


def test_red_and_yellow_cards_align_to_canonical_event_and_source_time() -> None:
    repository = SqliteMarketIntelligenceRepository()
    service = MarketIntelligenceService(repository)
    results = service.ingest_market_event_feed(newcastle_arsenal_fixture_feed())
    annotations = {item.annotation.category: item.annotation for item in results if item.annotation}

    yellow = annotations[AnnotationCategory.YELLOW_CARD]
    red = annotations[AnnotationCategory.RED_CARD]
    team_sheet = annotations[AnnotationCategory.TEAM_SHEET]

    assert {item.status for item in results} == {"created"}
    assert yellow.canonical_event_id == CANONICAL_EVENT_ID
    assert red.canonical_event_id == CANONICAL_EVENT_ID
    assert yellow.occurred_at == FIXTURE_KICKOFF + timedelta(minutes=12)
    assert red.occurred_at == FIXTURE_KICKOFF + timedelta(minutes=41)
    assert team_sheet.occurred_at == FIXTURE_KICKOFF - timedelta(hours=1)
    assert yellow.metadata["ingestion"]["retrieved_at"] != yellow.occurred_at.isoformat()
    assert red.metadata["ingestion"]["source_occurred_at"] == red.occurred_at.isoformat()
    assert yellow.metadata["ingestion"]["player_ref"] == "Bruno Guimaraes"
    assert red.metadata["ingestion"]["player_ref"] == "William Saliba"
    assert yellow.metadata["ingestion"]["causal_claim"] is False
    repository.close()


def test_duplicate_provider_event_is_rejected_idempotently() -> None:
    repository = SqliteMarketIntelligenceRepository()
    pipeline = MarketEventIngestionPipeline(repository)
    timeline = team_sheet_yellow_red_timeline()

    first_pass = pipeline.ingest_many(timeline)
    second_pass = pipeline.ingest_many(timeline)
    stored = repository.list_annotations(canonical_event_id=CANONICAL_EVENT_ID)

    assert [item.status for item in first_pass] == ["created", "created", "created"]
    assert [item.status for item in second_pass] == ["duplicate", "duplicate", "duplicate"]
    assert [item.category for item in stored] == [
        AnnotationCategory.TEAM_SHEET,
        AnnotationCategory.YELLOW_CARD,
        AnnotationCategory.RED_CARD,
    ]
    repository.close()


def test_ambiguous_and_missing_mappings_are_surfaced_not_guessed() -> None:
    repository = SqliteMarketIntelligenceRepository()
    pipeline = MarketEventIngestionPipeline(repository)

    ambiguous_card = pipeline.ingest(make_record(category="card", source_event_id="amb-card"))
    ambiguous_news = pipeline.ingest(make_record(category="news", source_event_id="amb-news"))
    missing_event = pipeline.ingest(
        make_record(canonical_event_id=None, source_event_id="no-event")
    )
    unknown_category = pipeline.ingest(
        make_record(category="press_conference", source_event_id="unknown-cat")
    )

    assert ambiguous_card.status == "rejected"
    assert ambiguous_card.rejection is not None
    assert ambiguous_card.rejection.reason == "ambiguous"
    assert ambiguous_card.rejection.field == "category"
    assert ambiguous_news.rejection is not None
    assert ambiguous_news.rejection.reason == "ambiguous"
    assert missing_event.status == "rejected"
    assert missing_event.rejection is not None
    assert missing_event.rejection.field == "canonical_event_id"
    assert unknown_category.rejection is not None
    assert unknown_category.rejection.reason == "unmapped"
    assert repository.list_annotations() == []
    repository.close()


def test_source_time_and_retrieval_time_remain_distinct_for_event_reaction() -> None:
    repository = SqliteMarketIntelligenceRepository()
    service = MarketIntelligenceService(repository)
    source_time = datetime(2026, 9, 12, 15, 0, tzinfo=UTC)
    retrieved_at = datetime(2026, 9, 12, 19, 30, tzinfo=UTC)
    result = service.ingest_market_event(
        make_record(source_occurred_at=source_time, retrieved_at=retrieved_at)
    )
    assert result.annotation is not None

    service.record_snapshot(
        MarketSnapshot(
            observed_at=source_time - timedelta(minutes=1),
            venue=VenueName.MATCHBOOK,
            canonical_event_id=CANONICAL_EVENT_ID,
            canonical_market_id="mkt:match_result",
            canonical_outcome="home",
            market_family=MarketFamily.MATCH_RESULT,
            decimal_odds=Decimal("2.50"),
            implied_probability=Decimal("0.40"),
        )
    )
    service.record_snapshot(
        MarketSnapshot(
            observed_at=source_time + timedelta(minutes=2),
            venue=VenueName.MATCHBOOK,
            canonical_event_id=CANONICAL_EVENT_ID,
            canonical_market_id="mkt:match_result",
            canonical_outcome="home",
            market_family=MarketFamily.MATCH_RESULT,
            decimal_odds=Decimal("2.22"),
            implied_probability=Decimal("0.45"),
        )
    )

    analysis = service.analyze_cross_market_event(
        annotation=result.annotation,
        post_window_minutes=10,
        response_threshold_probability_points=Decimal("0.02"),
    )

    assert result.annotation.occurred_at == source_time
    assert result.annotation.metadata["ingestion"]["retrieved_at"] == retrieved_at.isoformat()
    assert result.annotation.occurred_at != retrieved_at
    assert analysis.occurred_at == source_time
    assert analysis.reactions[0].first_response_seconds == 120
    repository.close()


def test_unconfigured_provider_adapter_is_a_seam_not_a_hardcoded_source() -> None:
    feed = OfficialSportsDataFeed()
    with pytest.raises(ProviderNotConfiguredError):
        feed.fetch()


def test_ingest_api_accepts_provider_records_and_rejects_duplicates() -> None:
    repository = SqliteMarketIntelligenceRepository()
    service = MarketIntelligenceService(repository)
    app.dependency_overrides[get_market_intelligence_service] = lambda: service
    client = TestClient(app)
    records = [item.model_dump(mode="json") for item in team_sheet_yellow_red_timeline()]

    try:
        missing_fixture = client.post(
            "/market-intelligence/events/ingest/fixtures/newcastle-arsenal"
        )
        assert missing_fixture.status_code == 404

        created = client.post("/market-intelligence/events/ingest", json=records)
        assert created.status_code == 200
        payload = created.json()
        assert [item["status"] for item in payload] == ["created", "created", "created"]
        assert [item["annotation"]["category"] for item in payload] == [
            "team_sheet",
            "yellow_card",
            "red_card",
        ]

        duplicate = client.post("/market-intelligence/events/ingest", json=records)
        assert [item["status"] for item in duplicate.json()] == [
            "duplicate",
            "duplicate",
            "duplicate",
        ]

        listed = client.get(f"/market-intelligence/events/{CANONICAL_EVENT_ID}/annotations")
        assert listed.status_code == 200
        assert len(listed.json()) == 3
    finally:
        app.dependency_overrides.clear()
        repository.close()


def test_changed_source_content_is_a_conflict_revision_not_a_silent_duplicate() -> None:
    repository = SqliteMarketIntelligenceRepository()
    pipeline = MarketEventIngestionPipeline(repository)
    original = make_record(
        source_event_id="src-correction",
        title="Yellow card: Bruno Guimaraes",
        category="YELLOW_CARD",
        player_ref="Bruno Guimaraes",
        source_occurred_at=datetime(2026, 9, 12, 16, 12, tzinfo=UTC),
    )
    correction = make_record(
        source_event_id="src-correction",
        title="Red card: Bruno Guimaraes",
        category="RED_CARD",
        player_ref="Bruno Guimaraes",
        source_occurred_at=datetime(2026, 9, 12, 16, 18, tzinfo=UTC),
        retrieved_at=datetime(2026, 9, 12, 19, 45, tzinfo=UTC),
        payload={"minute": 18, "corrected": True},
    )

    first = pipeline.ingest(original)
    second = pipeline.ingest(correction)
    third = pipeline.ingest(correction)
    stored = repository.list_annotations(canonical_event_id=CANONICAL_EVENT_ID)

    assert first.status == "created"
    assert first.annotation is not None
    assert second.status == "conflict"
    assert second.annotation is not None
    assert second.prior_annotation is not None
    assert second.prior_annotation.annotation_id == first.annotation.annotation_id
    assert second.annotation.annotation_id != first.annotation.annotation_id
    assert second.annotation.category == AnnotationCategory.RED_CARD
    assert second.annotation.occurred_at == datetime(2026, 9, 12, 16, 18, tzinfo=UTC)
    assert first.annotation.category == AnnotationCategory.YELLOW_CARD
    assert stored[0].annotation_id == first.annotation.annotation_id
    assert stored[0].title == "Yellow card: Bruno Guimaraes"
    assert stored[1].metadata["ingestion"]["revision"] is True
    assert stored[1].metadata["ingestion"]["corrects_annotation_id"] == first.annotation.annotation_id
    assert stored[1].metadata["ingestion"]["causal_claim"] is False
    assert len(stored) == 2
    assert third.status == "duplicate"
    assert third.annotation is not None
    assert third.annotation.annotation_id == second.annotation.annotation_id
    repository.close()


def test_retrieval_time_change_alone_is_still_a_duplicate() -> None:
    repository = SqliteMarketIntelligenceRepository()
    pipeline = MarketEventIngestionPipeline(repository)
    first = pipeline.ingest(make_record())
    second = pipeline.ingest(
        make_record(retrieved_at=datetime(2026, 9, 12, 21, 0, tzinfo=UTC))
    )
    stored = repository.list_annotations(canonical_event_id=CANONICAL_EVENT_ID)

    assert first.status == "created"
    assert second.status == "duplicate"
    assert len(stored) == 1
    repository.close()


def test_naive_source_and_retrieval_timestamps_fail_closed() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        make_record(source_occurred_at=datetime(2026, 9, 12, 15, 0))  # noqa: DTZ001
    with pytest.raises(ValidationError, match="timezone-aware"):
        make_record(retrieved_at=datetime(2026, 9, 12, 19, 0))  # noqa: DTZ001

    localized = localize_naive_datetime(datetime(2026, 9, 12, 15, 0), UTC)  # noqa: DTZ001
    record = make_record(source_occurred_at=localized)
    assert record.source_occurred_at.tzinfo is not None

    client_repository = SqliteMarketIntelligenceRepository()
    service = MarketIntelligenceService(client_repository)
    app.dependency_overrides[get_market_intelligence_service] = lambda: service
    client = TestClient(app)
    try:
        response = client.post(
            "/market-intelligence/events/ingest",
            json=[
                {
                    "provider": "fixture_test",
                    "source_event_id": "naive",
                    "source_occurred_at": "2026-09-12T15:00:00",
                    "retrieved_at": "2026-09-12T19:00:00Z",
                    "category": "TEAM_SHEET",
                    "canonical_event_id": CANONICAL_EVENT_ID,
                    "title": "Starting XI announced",
                }
            ],
        )
        assert response.status_code == 422
    finally:
        app.dependency_overrides.clear()
        client_repository.close()
