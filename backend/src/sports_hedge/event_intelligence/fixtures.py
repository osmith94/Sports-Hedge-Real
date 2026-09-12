from __future__ import annotations

from datetime import UTC, datetime, timedelta

from sports_hedge.event_intelligence.adapters import FixtureEventIntelligenceFeed
from sports_hedge.event_intelligence.models import (
    EventIntelligenceFact,
    EventSubject,
    ProvenanceClass,
    SourceKind,
)

CANONICAL_EVENT_ID = "evt:newcastle-arsenal"
OTHER_CANONICAL_EVENT_ID = "evt:chelsea-fulham"
FIXTURE_KICKOFF = datetime(2026, 9, 12, 16, 0, tzinfo=UTC)
FIXTURE_PROVIDER = "phase1_fixture"


def fixture_timeline() -> list[EventIntelligenceFact]:
    """Several pre-match types against one canonical fixture.

    Source published times are earlier than ingestion on purpose.
    """

    ingested_at = FIXTURE_KICKOFF + timedelta(hours=4)
    team_sheet_at = FIXTURE_KICKOFF - timedelta(hours=1)
    player_out_at = FIXTURE_KICKOFF - timedelta(minutes=50)
    injury_at = FIXTURE_KICKOFF - timedelta(minutes=40)
    manager_at = FIXTURE_KICKOFF - timedelta(minutes=25)
    lineup_change_at = FIXTURE_KICKOFF - timedelta(minutes=10)
    player_in_at = FIXTURE_KICKOFF - timedelta(minutes=8)
    return [
        EventIntelligenceFact(
            source_name=FIXTURE_PROVIDER,
            source_event_id="src-team-sheet",
            event_type="TEAM_SHEET_RELEASED",
            canonical_event_id=CANONICAL_EVENT_ID,
            published_at=team_sheet_at,
            ingested_at=ingested_at,
            provenance_class=ProvenanceClass.FIXTURE_TEST,
            source_kind=SourceKind.FIXTURE_TEST,
            title="Newcastle starting XI released",
            source_url="https://example.test/team-sheet",
            source_reference="fixture:team-sheet",
            subject=EventSubject(team_label="Newcastle United"),
            payload={"formation": "4-3-3"},
            raw_payload={"fixture": True, "kind": "team_sheet"},
        ),
        EventIntelligenceFact(
            source_name=FIXTURE_PROVIDER,
            source_event_id="src-player-out",
            event_type="PLAYER_OUT",
            canonical_event_id=CANONICAL_EVENT_ID,
            published_at=player_out_at,
            ingested_at=ingested_at,
            provenance_class=ProvenanceClass.FIXTURE_TEST,
            title="Isak omitted from squad",
            subject=EventSubject(team_label="Newcastle United", player_label="Alexander Isak"),
            payload={"reason": "not_in_squad"},
        ),
        EventIntelligenceFact(
            source_name=FIXTURE_PROVIDER,
            source_event_id="src-injury",
            event_type="INJURY_NEWS",
            canonical_event_id=CANONICAL_EVENT_ID,
            published_at=injury_at,
            ingested_at=ingested_at,
            provenance_class=ProvenanceClass.FIXTURE_TEST,
            title="Injury concern reported",
            subject=EventSubject(team_label="Arsenal", player_label="Bukayo Saka"),
            payload={"status": "doubt"},
        ),
        EventIntelligenceFact(
            source_name=FIXTURE_PROVIDER,
            source_event_id="src-manager",
            event_type="MANAGER_NEWS",
            canonical_event_id=CANONICAL_EVENT_ID,
            published_at=manager_at,
            ingested_at=ingested_at,
            provenance_class=ProvenanceClass.FIXTURE_TEST,
            title="Manager comments on selection",
            subject=EventSubject(team_label="Newcastle United", manager_label="Eddie Howe"),
        ),
        EventIntelligenceFact(
            source_name=FIXTURE_PROVIDER,
            source_event_id="src-lineup-change",
            event_type="LINEUP_CHANGE",
            canonical_event_id=CANONICAL_EVENT_ID,
            published_at=lineup_change_at,
            ingested_at=ingested_at,
            provenance_class=ProvenanceClass.FIXTURE_TEST,
            title="Late lineup change",
            subject=EventSubject(team_label="Arsenal"),
            payload={"change": "saka_out_trossard_in"},
        ),
        EventIntelligenceFact(
            source_name=FIXTURE_PROVIDER,
            source_event_id="src-player-in",
            event_type="PLAYER_IN",
            canonical_event_id=CANONICAL_EVENT_ID,
            published_at=player_in_at,
            ingested_at=ingested_at,
            provenance_class=ProvenanceClass.FIXTURE_TEST,
            title="Trossard added to starting XI",
            subject=EventSubject(team_label="Arsenal", player_label="Leandro Trossard"),
        ),
    ]


def newcastle_arsenal_fixture_feed() -> FixtureEventIntelligenceFeed:
    return FixtureEventIntelligenceFeed(fixture_timeline(), provider=FIXTURE_PROVIDER)
