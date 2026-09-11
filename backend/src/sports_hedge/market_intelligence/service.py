from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Sequence

from sports_hedge.domain.football import (
    FootballPeriod,
    MarketFamily,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.analytics import MarketIntelligenceAnalytics
from sports_hedge.market_intelligence.event_reaction import (
    EventReactionAnalysis,
    EventReactionAnalyzer,
)
from sports_hedge.market_intelligence.ingestion.contracts import (
    IngestResult,
    MarketEventFeed,
    ProviderEventRecord,
)
from sports_hedge.market_intelligence.ingestion.pipeline import MarketEventIngestionPipeline
from sports_hedge.market_intelligence.models import (
    AnnotationCategory,
    MarketEventAnnotation,
    MarketSnapshot,
    MovementScore,
    ReversionAnalysis,
)
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.trends import (
    TrendExplorer,
    TrendMetric,
    TrendQuery,
    TrendSummary,
)


class MarketIntelligenceService:
    """Application service for recording and researching historical market behaviour."""

    def __init__(
        self,
        repository: SqliteMarketIntelligenceRepository,
        *,
        trend_minimum_sample_size: int = 8,
    ) -> None:
        self.repository = repository
        self.analytics = MarketIntelligenceAnalytics()
        self.event_reactions = EventReactionAnalyzer()
        self.trends = TrendExplorer(minimum_sample_size=trend_minimum_sample_size)
        self.event_ingestion = MarketEventIngestionPipeline(repository)

    def record_snapshot(self, snapshot: MarketSnapshot) -> None:
        self.repository.append_snapshot(snapshot)

    def record_annotation(self, annotation: MarketEventAnnotation) -> None:
        self.repository.append_annotation(annotation)

    def ingest_market_event(self, record: ProviderEventRecord) -> IngestResult:
        return self.event_ingestion.ingest(record)

    def ingest_market_events(self, records: list[ProviderEventRecord]) -> list[IngestResult]:
        return self.event_ingestion.ingest_many(records)

    def ingest_market_event_feed(
        self,
        feed: MarketEventFeed,
        *,
        since: datetime | None = None,
    ) -> list[IngestResult]:
        return self.event_ingestion.ingest_feed(feed, since=since)

    def market_history(
        self,
        *,
        canonical_event_id: str | None = None,
        canonical_market_id: str | None = None,
        canonical_outcome: str | None = None,
        venue: VenueName | None = None,
        market_family: MarketFamily | None = None,
        period: FootballPeriod | None = None,
        market_line: Decimal | None = None,
        settlement_scope: SettlementScope | None = None,
        competition: str | None = None,
        team: str | None = None,
    ) -> list[MarketSnapshot]:
        return self.repository.list_snapshots(
            canonical_event_id=canonical_event_id,
            canonical_market_id=canonical_market_id,
            canonical_outcome=canonical_outcome,
            venue=venue,
            market_family=market_family,
            period=period,
            market_line=market_line,
            settlement_scope=settlement_scope,
            competition=competition,
            team=team,
        )

    def event_annotations(
        self,
        canonical_event_id: str,
        *,
        category: AnnotationCategory | None = None,
    ) -> list[MarketEventAnnotation]:
        return self.repository.list_annotations(
            canonical_event_id=canonical_event_id,
            category=category,
        )

    def analyze_trend(
        self,
        metric: TrendMetric,
        *,
        query: TrendQuery | None = None,
    ) -> TrendSummary:
        cohort = query or TrendQuery()
        history = self.repository.list_snapshots(
            venue=cohort.venue,
            market_family=cohort.market_family,
            period=cohort.period,
            market_line=cohort.market_line,
            settlement_scope=cohort.settlement_scope,
            competition=cohort.competition,
            team=cohort.team,
            canonical_outcome=cohort.canonical_outcome,
            start_at=cohort.start_at,
            end_at=cohort.end_at,
        )
        return self.trends.analyze(metric, history, query=cohort)

    def score_recent_move(
        self,
        *,
        canonical_market_id: str,
        canonical_outcome: str,
        lookback_minutes: int,
        historical_moves: Sequence[Decimal | float],
        venue: VenueName | None = None,
        minimum_sample_size: int = 8,
    ) -> MovementScore | None:
        history = self.repository.list_snapshots(
            canonical_market_id=canonical_market_id,
            canonical_outcome=canonical_outcome,
            venue=venue,
        )
        move = self.analytics.probability_move(history, lookback_minutes=lookback_minutes)
        if move is None or not history:
            return None
        return self.analytics.score_move(
            move,
            historical_moves,
            minimum_sample_size=minimum_sample_size,
            current_liquidity=history[-1].total_liquidity,
        )

    def analyze_cross_market_event(
        self,
        *,
        annotation: MarketEventAnnotation,
        venue: VenueName | None = None,
        pre_window_minutes: int = 5,
        post_window_minutes: int = 30,
        response_threshold_probability_points: Decimal = Decimal("0.005"),
    ) -> EventReactionAnalysis:
        history = self.repository.list_snapshots(
            canonical_event_id=annotation.canonical_event_id,
            venue=venue,
        )
        return self.event_reactions.analyze(
            annotation,
            history,
            pre_window_minutes=pre_window_minutes,
            post_window_minutes=post_window_minutes,
            response_threshold_probability_points=response_threshold_probability_points,
        )

    def analyze_annotation_reaction(
        self,
        *,
        annotation: MarketEventAnnotation,
        canonical_market_id: str,
        canonical_outcome: str,
        venue: VenueName | None = None,
        shock_window_minutes: int = 5,
        horizons_minutes: Sequence[int] = (1, 5, 15, 30, 60),
    ) -> ReversionAnalysis | None:
        history = self.repository.list_snapshots(
            canonical_event_id=annotation.canonical_event_id,
            canonical_market_id=canonical_market_id,
            canonical_outcome=canonical_outcome,
            venue=venue,
        )
        baseline_candidates = [
            snapshot for snapshot in history if snapshot.observed_at <= annotation.occurred_at
        ]
        after = [
            snapshot
            for snapshot in history
            if annotation.occurred_at < snapshot.observed_at
            <= annotation.occurred_at + timedelta(minutes=shock_window_minutes)
        ]
        if not baseline_candidates or not after:
            return None

        baseline = baseline_candidates[-1]
        baseline_probability = _probability(baseline)
        shock = max(
            after,
            key=lambda snapshot: abs(float(_probability(snapshot) - baseline_probability)),
        )
        if _probability(shock) == baseline_probability:
            return None

        post = [snapshot for snapshot in history if snapshot.observed_at >= shock.observed_at]
        return self.analytics.reversion_analysis(
            baseline_probability=baseline_probability,
            shock_snapshot=shock,
            post_snapshots=post,
            horizons_minutes=horizons_minutes,
        )


def _probability(snapshot: MarketSnapshot) -> Decimal:
    if snapshot.implied_probability is None:
        raise ValueError("Snapshot has no implied probability")
    return snapshot.implied_probability
