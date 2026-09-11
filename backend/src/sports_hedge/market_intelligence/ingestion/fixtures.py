from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime, timedelta

from sports_hedge.market_intelligence.ingestion.contracts import ProviderEventRecord

CANONICAL_EVENT_ID = "evt:newcastle-arsenal"
FIXTURE_KICKOFF = datetime(2026, 9, 12, 16, 0, tzinfo=UTC)
FIXTURE_PROVIDER = "phase1_fixture"


class FixtureMarketEventFeed:
    """Manual/synthetic ingestion path for Phase 1 testing."""

    def __init__(self, records: Sequence[ProviderEventRecord], *, provider: str) -> None:
        self._records = list(records)
        self._provider = provider

    @property
    def provider(self) -> str:
        return self._provider

    def fetch(self, *, since: datetime | None = None) -> Sequence[ProviderEventRecord]:
        if since is None:
            return list(self._records)
        return [record for record in self._records if record.source_occurred_at >= since]


def team_sheet_yellow_red_timeline() -> list[ProviderEventRecord]:
    """Representative pre-match to in-play timeline for Newcastle vs Arsenal.

    Times are source event timestamps. Retrieval is delayed on purpose so tests
    can prove Event Reaction uses source time, not ingest time.
    """

    team_sheet_at = FIXTURE_KICKOFF - timedelta(hours=1)
    yellow_at = FIXTURE_KICKOFF + timedelta(minutes=12)
    red_at = FIXTURE_KICKOFF + timedelta(minutes=41)
    retrieved_at = FIXTURE_KICKOFF + timedelta(hours=3)
    return [
        ProviderEventRecord(
            provider=FIXTURE_PROVIDER,
            source_event_id="fixture:ncl-ars:team-sheet",
            source_occurred_at=team_sheet_at,
            retrieved_at=retrieved_at,
            category="TEAM_SHEET",
            canonical_event_id=CANONICAL_EVENT_ID,
            team_ref="Newcastle United",
            home_team="Newcastle United",
            away_team="Arsenal",
            title="Newcastle starting XI published",
            source_url="https://example.test/fixtures/ncl-ars/team-sheet",
            source_reference="club-site:starting-xi",
            confidence=1.0,
            payload={"formation": "4-3-3"},
        ),
        ProviderEventRecord(
            provider=FIXTURE_PROVIDER,
            source_event_id="fixture:ncl-ars:yellow-12",
            source_occurred_at=yellow_at,
            retrieved_at=retrieved_at,
            category="YELLOW_CARD",
            canonical_event_id=CANONICAL_EVENT_ID,
            team_ref="Newcastle United",
            player_ref="Bruno Guimaraes",
            home_team="Newcastle United",
            away_team="Arsenal",
            title="Yellow card: Bruno Guimaraes (Newcastle)",
            source_url="https://example.test/fixtures/ncl-ars/yellow-12",
            source_reference="match-feed:booking:12",
            confidence=1.0,
            payload={"minute": 12},
        ),
        ProviderEventRecord(
            provider=FIXTURE_PROVIDER,
            source_event_id="fixture:ncl-ars:red-41",
            source_occurred_at=red_at,
            retrieved_at=retrieved_at,
            category="RED_CARD",
            canonical_event_id=CANONICAL_EVENT_ID,
            team_ref="Arsenal",
            player_ref="William Saliba",
            home_team="Newcastle United",
            away_team="Arsenal",
            title="Red card: William Saliba (Arsenal)",
            source_url="https://example.test/fixtures/ncl-ars/red-41",
            source_reference="match-feed:dismissal:41",
            confidence=1.0,
            payload={"minute": 41},
        ),
    ]


def newcastle_arsenal_fixture_feed() -> FixtureMarketEventFeed:
    return FixtureMarketEventFeed(
        team_sheet_yellow_red_timeline(),
        provider=FIXTURE_PROVIDER,
    )
