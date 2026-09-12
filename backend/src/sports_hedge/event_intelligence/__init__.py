"""Fixture-linked event intelligence: pre-match/news context, not an arb trigger."""

from sports_hedge.event_intelligence.adapters import (
    FixtureEventIntelligenceFeed,
    ProviderNotConfiguredError,
    UnconfiguredApprovedSourceFeed,
)
from sports_hedge.event_intelligence.fixtures import (
    CANONICAL_EVENT_ID,
    newcastle_arsenal_fixture_feed,
)
from sports_hedge.event_intelligence.models import (
    EventIntelligenceFact,
    EventIntelligenceRecord,
    EventIntelligenceTimeline,
    EventIntelligenceType,
    IngestResult,
    ProvenanceClass,
)
from sports_hedge.event_intelligence.repository import SqliteEventIntelligenceRepository
from sports_hedge.event_intelligence.service import EventIntelligenceService

__all__ = [
    "CANONICAL_EVENT_ID",
    "EventIntelligenceFact",
    "EventIntelligenceRecord",
    "EventIntelligenceService",
    "EventIntelligenceTimeline",
    "EventIntelligenceType",
    "FixtureEventIntelligenceFeed",
    "IngestResult",
    "ProvenanceClass",
    "ProviderNotConfiguredError",
    "SqliteEventIntelligenceRepository",
    "UnconfiguredApprovedSourceFeed",
    "newcastle_arsenal_fixture_feed",
]
