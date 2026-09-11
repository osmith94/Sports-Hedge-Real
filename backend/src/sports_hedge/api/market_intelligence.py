from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query, status

from sports_hedge.config import get_settings
from sports_hedge.domain.football import (
    FootballPeriod,
    MarketFamily,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.event_reaction import EventReactionAnalysis
from sports_hedge.market_intelligence.ingestion.contracts import IngestResult, ProviderEventRecord
from sports_hedge.market_intelligence.ingestion.fixtures import (
    newcastle_arsenal_fixture_feed,
)
from sports_hedge.market_intelligence.models import (
    AnnotationCategory,
    KickoffBucket,
    MarketEventAnnotation,
    MarketSnapshot,
)
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.market_intelligence.trends import (
    TrendMetric,
    TrendQuery,
    TrendSummary,
)

router = APIRouter(prefix="/market-intelligence", tags=["market-intelligence"])


@lru_cache
def get_market_intelligence_service() -> MarketIntelligenceService:
    settings = get_settings()
    database = settings.market_intelligence_db_path
    if database != ":memory:":
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
    repository = SqliteMarketIntelligenceRepository(database)
    return MarketIntelligenceService(
        repository,
        trend_minimum_sample_size=settings.market_intelligence_minimum_sample_size,
    )


@router.post(
    "/snapshots",
    response_model=MarketSnapshot,
    status_code=status.HTTP_201_CREATED,
)
def record_snapshot(
    snapshot: MarketSnapshot,
    service: MarketIntelligenceService = Depends(get_market_intelligence_service),
) -> MarketSnapshot:
    service.record_snapshot(snapshot)
    return snapshot


@router.get("/history", response_model=list[MarketSnapshot])
def market_history(
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
    limit: int = Query(default=2000, ge=1, le=10000),
    service: MarketIntelligenceService = Depends(get_market_intelligence_service),
) -> list[MarketSnapshot]:
    history = service.market_history(
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
    return history[-limit:]


@router.post(
    "/annotations",
    response_model=MarketEventAnnotation,
    status_code=status.HTTP_201_CREATED,
)
def record_annotation(
    annotation: MarketEventAnnotation,
    service: MarketIntelligenceService = Depends(get_market_intelligence_service),
) -> MarketEventAnnotation:
    service.record_annotation(annotation)
    return annotation


@router.post(
    "/events/ingest",
    response_model=list[IngestResult],
    status_code=status.HTTP_200_OK,
)
def ingest_market_events(
    records: list[ProviderEventRecord],
    service: MarketIntelligenceService = Depends(get_market_intelligence_service),
) -> list[IngestResult]:
    """Manual/fixture ingestion of provider-neutral sports and news events.

    Rejected mappings are returned in the batch rather than guessed. Duplicate
    source events are idempotent and do not create a second annotation.
    """

    return service.ingest_market_events(records)


@router.post(
    "/events/ingest/fixtures/newcastle-arsenal",
    response_model=list[IngestResult],
    status_code=status.HTTP_200_OK,
)
def ingest_newcastle_arsenal_fixture(
    service: MarketIntelligenceService = Depends(get_market_intelligence_service),
) -> list[IngestResult]:
    return service.ingest_market_event_feed(newcastle_arsenal_fixture_feed())


@router.get(
    "/events/{canonical_event_id}/annotations",
    response_model=list[MarketEventAnnotation],
)
def event_annotations(
    canonical_event_id: str,
    category: AnnotationCategory | None = None,
    service: MarketIntelligenceService = Depends(get_market_intelligence_service),
) -> list[MarketEventAnnotation]:
    return service.event_annotations(canonical_event_id, category=category)


@router.get(
    "/events/{canonical_event_id}/reactions/{annotation_id}",
    response_model=EventReactionAnalysis,
)
def event_reaction(
    canonical_event_id: str,
    annotation_id: str,
    venue: VenueName | None = None,
    pre_window_minutes: int = Query(default=5, ge=1, le=180),
    post_window_minutes: int = Query(default=30, ge=1, le=1440),
    response_threshold_probability_points: Decimal = Query(
        default=Decimal("0.005"),
        gt=0,
        lt=1,
    ),
    service: MarketIntelligenceService = Depends(get_market_intelligence_service),
) -> EventReactionAnalysis:
    annotation = next(
        (
            item
            for item in service.event_annotations(canonical_event_id)
            if item.annotation_id == annotation_id
        ),
        None,
    )
    if annotation is None:
        raise HTTPException(status_code=404, detail="Market event annotation not found")
    return service.analyze_cross_market_event(
        annotation=annotation,
        venue=venue,
        pre_window_minutes=pre_window_minutes,
        post_window_minutes=post_window_minutes,
        response_threshold_probability_points=response_threshold_probability_points,
    )


@router.get("/trends/metrics", response_model=list[str])
def trend_metrics() -> list[str]:
    return [metric.value for metric in TrendMetric]


@router.get("/trends/{metric}", response_model=TrendSummary)
def trend_summary(
    metric: TrendMetric,
    venue: VenueName | None = None,
    market_family: MarketFamily | None = None,
    period: FootballPeriod | None = None,
    market_line: Decimal | None = None,
    settlement_scope: SettlementScope | None = None,
    competition: str | None = None,
    team: str | None = None,
    canonical_outcome: str | None = None,
    kickoff_bucket: KickoffBucket | None = None,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    minimum_start_probability: float | None = Query(default=None, ge=0.0, le=1.0),
    maximum_start_probability: float | None = Query(default=None, ge=0.0, le=1.0),
    service: MarketIntelligenceService = Depends(get_market_intelligence_service),
) -> TrendSummary:
    if (
        minimum_start_probability is not None
        and maximum_start_probability is not None
        and minimum_start_probability > maximum_start_probability
    ):
        raise HTTPException(
            status_code=422,
            detail="minimum_start_probability cannot exceed maximum_start_probability",
        )
    query = TrendQuery(
        venue=venue,
        market_family=market_family,
        period=period,
        market_line=market_line,
        settlement_scope=settlement_scope,
        competition=competition,
        team=team,
        canonical_outcome=canonical_outcome,
        kickoff_bucket=kickoff_bucket,
        start_at=start_at,
        end_at=end_at,
        minimum_start_probability=minimum_start_probability,
        maximum_start_probability=maximum_start_probability,
    )
    return service.analyze_trend(metric, query=query)
