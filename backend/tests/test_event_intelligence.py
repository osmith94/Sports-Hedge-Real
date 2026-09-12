from __future__ import annotations

import ast
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from sports_hedge.api.event_intelligence import get_event_intelligence_service
from sports_hedge.api.main import app
from sports_hedge.arbitrage.dislocations.service import EventDrivenDislocationService
from sports_hedge.arbitrage.watchlist.models import (
    NearOpportunity,
    OpportunityClassification,
    OpportunityStatus,
)
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.config import Settings
from sports_hedge.event_intelligence.adapters import (
    ProviderNotConfiguredError,
    UnconfiguredApprovedSourceFeed,
)
from sports_hedge.event_intelligence.fixtures import (
    CANONICAL_EVENT_ID,
    FIXTURE_KICKOFF,
    OTHER_CANONICAL_EVENT_ID,
    fixture_timeline,
    newcastle_arsenal_fixture_feed,
)
from sports_hedge.event_intelligence.models import (
    EventIntelligenceFact,
    EventIntelligenceType,
    EventSubject,
    ProvenanceClass,
    SourceKind,
    VerificationStatus,
)
from sports_hedge.event_intelligence.repository import SqliteEventIntelligenceRepository
from sports_hedge.event_intelligence.service import EventIntelligenceService

PACKAGE_ROOT = Path(__file__).resolve().parents[1] / "src" / "sports_hedge" / "event_intelligence"


def make_fact(**overrides: object) -> EventIntelligenceFact:
    payload = {
        "source_name": "fixture_test",
        "source_event_id": "src-1",
        "event_type": "TEAM_SHEET_RELEASED",
        "canonical_event_id": CANONICAL_EVENT_ID,
        "published_at": datetime(2026, 9, 12, 15, 0, tzinfo=UTC),
        "ingested_at": datetime(2026, 9, 12, 19, 0, tzinfo=UTC),
        "provenance_class": ProvenanceClass.FIXTURE_TEST,
        "title": "Starting XI announced",
        "source_url": "https://example.test/team-sheet",
        "source_reference": "ref-1",
        "subject": EventSubject(team_label="Newcastle United"),
        "payload": {"formation": "4-3-3"},
        "raw_payload": {"raw": True},
    }
    payload.update(overrides)
    return EventIntelligenceFact.model_validate(payload)


def _service() -> tuple[EventIntelligenceService, SqliteEventIntelligenceRepository]:
    repository = SqliteEventIntelligenceRepository()
    return EventIntelligenceService(repository), repository


def test_fixture_timeline_persists_types_in_chronological_source_order() -> None:
    service, repository = _service()
    results = service.ingest_feed(newcastle_arsenal_fixture_feed())
    timeline = service.timeline(CANONICAL_EVENT_ID)

    assert {item.status for item in results} == {"created"}
    assert [item.event_type for item in timeline.records] == [
        EventIntelligenceType.TEAM_SHEET_RELEASED,
        EventIntelligenceType.PLAYER_OUT,
        EventIntelligenceType.INJURY_NEWS,
        EventIntelligenceType.MANAGER_NEWS,
        EventIntelligenceType.LINEUP_CHANGE,
        EventIntelligenceType.PLAYER_IN,
    ]
    assert timeline.records[0].published_at == FIXTURE_KICKOFF - timedelta(hours=1)
    assert timeline.records[-1].published_at == FIXTURE_KICKOFF - timedelta(minutes=8)
    assert timeline.data_class == ProvenanceClass.FIXTURE_TEST.value
    assert timeline.causal_claim is False
    assert timeline.temporal_context_only is True
    repository.close()


def test_published_time_remains_distinct_from_ingestion_time() -> None:
    service, repository = _service()
    published = datetime(2026, 9, 12, 14, 0, tzinfo=UTC)
    ingested = datetime(2026, 9, 12, 18, 30, tzinfo=UTC)
    result = service.ingest(make_fact(published_at=published, ingested_at=ingested))

    assert result.status == "created"
    assert result.record is not None
    assert result.record.published_at == published
    assert result.record.ingested_at == ingested
    assert result.record.published_at != result.record.ingested_at
    stored = repository.list_timeline(CANONICAL_EVENT_ID)
    assert stored[0].published_at == published
    assert stored[0].ingested_at == ingested
    repository.close()


def test_duplicate_source_fact_is_idempotent() -> None:
    service, repository = _service()
    first = service.ingest(make_fact())
    second = service.ingest(make_fact(ingested_at=datetime(2026, 9, 12, 21, 0, tzinfo=UTC)))
    stored = repository.list_timeline(CANONICAL_EVENT_ID)

    assert first.status == "created"
    assert second.status == "duplicate"
    assert second.record is not None
    assert first.record is not None
    assert second.record.event_intelligence_id == first.record.event_intelligence_id
    assert len(stored) == 1
    repository.close()


def test_identity_conflict_fails_closed_and_does_not_reattach_fixture() -> None:
    service, repository = _service()
    created = service.ingest(make_fact())
    conflict = service.ingest(
        make_fact(
            canonical_event_id=OTHER_CANONICAL_EVENT_ID,
            title="Starting XI announced",
        )
    )

    assert created.status == "created"
    assert conflict.status == "conflict"
    assert conflict.record is None
    assert conflict.rejection is not None
    assert conflict.rejection.field == "identity"
    assert repository.list_timeline(OTHER_CANONICAL_EVENT_ID) == []
    assert len(repository.list_timeline(CANONICAL_EVENT_ID)) == 1
    repository.close()


def test_conflicting_player_identity_fails_closed() -> None:
    service, repository = _service()
    first = service.ingest(
        make_fact(
            event_type="PLAYER_OUT",
            source_event_id="player-out-1",
            title="Player omitted",
            subject=EventSubject(player_label="Alexander Isak", team_label="Newcastle United"),
        )
    )
    conflict = service.ingest(
        make_fact(
            event_type="PLAYER_OUT",
            source_event_id="player-out-1",
            title="Player omitted",
            subject=EventSubject(player_label="Callum Wilson", team_label="Newcastle United"),
        )
    )

    assert first.status == "created"
    assert conflict.status == "conflict"
    assert conflict.record is None
    stored = repository.list_timeline(CANONICAL_EVENT_ID)
    assert len(stored) == 1
    assert stored[0].subject.player_label == "Alexander Isak"
    repository.close()


def test_payload_revision_appends_without_mutating_prior() -> None:
    service, repository = _service()
    first = service.ingest(make_fact(payload={"formation": "4-3-3"}))
    revision = service.ingest(
        make_fact(
            payload={"formation": "4-2-3-1"},
            ingested_at=datetime(2026, 9, 12, 20, 0, tzinfo=UTC),
        )
    )
    stored = repository.list_timeline(CANONICAL_EVENT_ID)

    assert first.status == "created"
    assert revision.status == "conflict"
    assert revision.record is not None
    assert first.record is not None
    assert revision.record.event_intelligence_id != first.record.event_intelligence_id
    assert revision.record.revision_of_id == first.record.event_intelligence_id
    assert [item.payload["formation"] for item in stored] == ["4-3-3", "4-2-3-1"]
    repository.close()


def test_missing_canonical_event_id_is_rejected_not_guessed() -> None:
    service, repository = _service()
    result = service.ingest(make_fact(canonical_event_id=None))
    assert result.status == "rejected"
    assert result.rejection is not None
    assert result.rejection.field == "canonical_event_id"
    assert repository.list_timeline(CANONICAL_EVENT_ID) == []
    repository.close()


def test_ambiguous_and_in_play_types_are_rejected() -> None:
    service, repository = _service()
    lineup = service.ingest(make_fact(event_type="lineup", source_event_id="amb-lineup"))
    goal = service.ingest(make_fact(event_type="GOAL", source_event_id="goal-1"))
    player_without_subject = service.ingest(
        make_fact(
            event_type="PLAYER_OUT",
            source_event_id="no-player",
            subject=EventSubject(team_label="Newcastle United"),
        )
    )
    ambiguous_player = service.ingest(
        make_fact(
            event_type="PLAYER_OUT",
            source_event_id="amb-player",
            subject=EventSubject(player_label="Isak or Wilson"),
        )
    )

    assert lineup.status == "rejected"
    assert lineup.rejection is not None
    assert lineup.rejection.reason == "ambiguous"
    assert goal.status == "rejected"
    assert goal.rejection is not None
    assert goal.rejection.reason == "rejected_in_play"
    assert player_without_subject.status == "rejected"
    assert ambiguous_player.status == "rejected"
    assert repository.list_timeline(CANONICAL_EVENT_ID) == []
    repository.close()


def test_label_only_player_stays_unresolved_without_invented_id() -> None:
    service, repository = _service()
    result = service.ingest(
        make_fact(
            event_type="INJURY_NEWS",
            source_event_id="injury-1",
            title="Injury news",
            subject=EventSubject(player_label="Bukayo Saka", team_label="Arsenal"),
        )
    )
    assert result.status == "created"
    assert result.record is not None
    assert result.record.subject.player_id is None
    assert result.record.verification_status is VerificationStatus.UNRESOLVED
    repository.close()


def test_naive_timestamps_are_refused() -> None:
    with pytest.raises(ValidationError, match="timezone-aware"):
        make_fact(published_at=datetime(2026, 9, 12, 15, 0))


def test_unconfigured_provider_does_not_scrape() -> None:
    feed = UnconfiguredApprovedSourceFeed("official_sports_data")
    with pytest.raises(ProviderNotConfiguredError, match="adapter seam"):
        feed.fetch()


def test_timeline_api_returns_provenance_and_is_read_only() -> None:
    service, repository = _service()
    service.ingest_many(fixture_timeline())
    app.dependency_overrides[get_event_intelligence_service] = lambda: service
    client = TestClient(app)
    try:
        response = client.get(
            f"/research/event-intelligence/fixtures/{CANONICAL_EVENT_ID}/timeline"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["data_class"] == "fixture_test"
        assert body["causal_claim"] is False
        assert len(body["records"]) == 6
        first = body["records"][0]
        assert first["provenance_class"] == "fixture_test"
        assert first["source_kind"] == SourceKind.FIXTURE_TEST.value
        assert first["published_at"] != first["ingested_at"]
        assert first["verification_status"] in {
            "verified",
            "unverified",
            "unresolved",
        }
        types = client.get("/research/event-intelligence/event-types")
        assert types.status_code == 200
        assert "team_sheet_released" in types.json()
        filtered = client.get(
            f"/research/event-intelligence/fixtures/{CANONICAL_EVENT_ID}/timeline",
            params={"event_type": "player_out"},
        )
        assert [item["event_type"] for item in filtered.json()["records"]] == ["player_out"]
        posted = client.post(
            f"/research/event-intelligence/fixtures/{CANONICAL_EVENT_ID}/timeline",
            json={},
        )
        assert posted.status_code == 405
        empty = client.get("/research/event-intelligence/fixtures/evt:unknown/timeline")
        assert empty.json()["data_class"] == "EMPTY"
        assert empty.json()["records"] == []
        health = client.get("/health")
        assert health.json()["execution_enabled"] is False
        assert health.json()["mode"] == "paper"
    finally:
        app.dependency_overrides.clear()
        repository.close()


def test_event_intelligence_does_not_change_arb_or_watchlist_state() -> None:
    watchlist = SqliteWatchlistRepository()
    seen = datetime(2026, 9, 12, 12, 0, tzinfo=UTC)
    opportunity = NearOpportunity(
        opportunity_id="opp-1",
        canonical_event_id=CANONICAL_EVENT_ID,
        canonical_market_id="mkt:match_result",
        status=OpportunityStatus.WATCHING,
        classification=OpportunityClassification.WATCH_CANDIDATE,
        is_arbitrage=False,
        trigger_net_edge=Decimal("0.01"),
        current_net_edge=Decimal("0.004"),
        first_seen_at=seen,
        last_seen_at=seen,
    )
    watchlist.upsert_opportunity(opportunity)
    before = [item.model_dump() for item in watchlist.list_opportunities()]
    dislocation = EventDrivenDislocationService(settings=Settings())
    before_states = dict(dislocation.tracker._states)

    service, repository = _service()
    results = service.ingest_many(fixture_timeline())
    after = [item.model_dump() for item in watchlist.list_opportunities()]

    assert {item.status for item in results} == {"created"}
    assert after == before
    assert after[0]["classification"] == "watch_candidate"
    assert after[0]["is_arbitrage"] is False
    assert dislocation.tracker._states == before_states
    watchlist.close()
    repository.close()


def test_no_live_execution_capability_in_event_intelligence_package() -> None:
    forbidden_defs = ("place_order", "cancel_order", "place_bet", "sign_wallet", "submit_order")
    forbidden_imports = {
        "sports_hedge.arbitrage",
        "sports_hedge.paper",
        "sports_hedge.api.paper",
        "sports_hedge.api.watchlist",
    }
    joined: list[str] = []
    for path in sorted(PACKAGE_ROOT.glob("*.py")):
        text = path.read_text(encoding="utf-8")
        joined.append(text)
        tree = ast.parse(text)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                root = node.module
                for blocked in forbidden_imports:
                    assert not root.startswith(blocked), f"{path} imports {root}"
            if isinstance(node, ast.FunctionDef):
                assert node.name not in forbidden_defs, f"{node.name} in {path}"
    blob = "\n".join(joined)
    for token in ("scrapy", "playwright", "selenium", "api_key", "password"):
        assert token not in blob
    service, repository = _service()
    for name in forbidden_defs:
        assert not hasattr(service, name)
        assert not hasattr(service.repository, name)
    assert Settings().sports_hedge_execution_enabled is False
    repository.close()
