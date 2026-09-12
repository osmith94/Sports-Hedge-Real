"""Historical market movement and event-reaction analytics."""

from sports_hedge.market_intelligence.analytics import MarketIntelligenceAnalytics
from sports_hedge.market_intelligence.event_reaction import (
    EventReactionAnalysis,
    EventReactionAnalyzer,
    LeadLagResult,
    MarketDependencyGraph,
    MarketReaction,
)
from sports_hedge.market_intelligence.ingestion import (
    IngestResult,
    MarketEventIngestionPipeline,
    ProviderEventRecord,
)
from sports_hedge.market_intelligence.models import (
    AnnotationCategory,
    HistoricalCohortStats,
    MarketEventAnnotation,
    MarketSnapshot,
    MovementScore,
    ReversionAnalysis,
)
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.trends import (
    TrendExplorer,
    TrendMetric,
    TrendObservation,
    TrendQuery,
    TrendSummary,
)

__all__ = [
    "AnnotationCategory",
    "EventReactionAnalysis",
    "EventReactionAnalyzer",
    "HistoricalCohortStats",
    "IngestResult",
    "LeadLagResult",
    "MarketDependencyGraph",
    "MarketEventAnnotation",
    "MarketEventIngestionPipeline",
    "MarketIntelligenceAnalytics",
    "MarketReaction",
    "MarketSnapshot",
    "MovementScore",
    "ProviderEventRecord",
    "ReversionAnalysis",
    "SqliteMarketIntelligenceRepository",
    "TrendExplorer",
    "TrendMetric",
    "TrendObservation",
    "TrendQuery",
    "TrendSummary",
]
