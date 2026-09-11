"""Provider-neutral market event ingestion for Market Intelligence annotations."""

from sports_hedge.market_intelligence.ingestion.adapters import (
    NewsProviderFeed,
    OfficialSportsDataFeed,
    ProviderNotConfiguredError,
    UnconfiguredProviderFeed,
)
from sports_hedge.market_intelligence.ingestion.contracts import (
    IngestResult,
    MappingIssue,
    MarketEventFeed,
    NormalizedMarketEvent,
    ProviderEventRecord,
    ingestion_key,
)
from sports_hedge.market_intelligence.ingestion.fixtures import (
    FixtureMarketEventFeed,
    newcastle_arsenal_fixture_feed,
    team_sheet_yellow_red_timeline,
)
from sports_hedge.market_intelligence.ingestion.mapping import (
    EventMappingError,
    map_event_category,
    normalize_provider_event,
)
from sports_hedge.market_intelligence.ingestion.pipeline import (
    MarketEventIngestionPipeline,
    annotation_from_normalized,
)

__all__ = [
    "EventMappingError",
    "FixtureMarketEventFeed",
    "IngestResult",
    "MappingIssue",
    "MarketEventFeed",
    "MarketEventIngestionPipeline",
    "NewsProviderFeed",
    "NormalizedMarketEvent",
    "OfficialSportsDataFeed",
    "ProviderEventRecord",
    "ProviderNotConfiguredError",
    "UnconfiguredProviderFeed",
    "annotation_from_normalized",
    "ingestion_key",
    "map_event_category",
    "newcastle_arsenal_fixture_feed",
    "normalize_provider_event",
    "team_sheet_yellow_red_timeline",
]
