from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field

from sports_hedge.api.market_intelligence import get_market_intelligence_service
from sports_hedge.application.collector import CollectionReport, ReadOnlyCrossVenueCollector
from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
    VenueMarketObservation,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.config import get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.models import FeeSnapshot
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.normalization.venues import VenueNormalizationError
from sports_hedge.paper.audit import (
    PaperScanRecord,
    PaperScanSummary,
    build_paper_scan_record,
)
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from sports_hedge.venues.matchbook import MatchbookAuthError, MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient


router = APIRouter(prefix="/paper", tags=["paper"])


class RawVenueObservationRequest(BaseModel):
    venue: VenueName
    event_payload: dict[str, Any]
    market_payload: dict[str, Any]
    books_by_token: dict[str, dict[str, Any]] = Field(default_factory=dict)
    observed_at: datetime | None = None
    native_currency: str | None = None
    source_latency_ms: int = Field(default=0, ge=0)
    quote_age_ms: int = Field(default=0, ge=0)


class PaperPairScanRequest(BaseModel):
    left: RawVenueObservationRequest
    right: RawVenueObservationRequest
    fee_snapshots: list[FeeSnapshot] = Field(default_factory=list)
    fx_snapshots: list[FxRateSnapshot] = Field(default_factory=list)
    capital_limit_gbp: Decimal | None = Field(default=None, gt=0)
    minimum_net_edge: Decimal = Field(default=Decimal("0.005"), ge=0)
    maximum_execution_risk: int = Field(default=60, ge=0, le=100)
    minimum_mapping_confidence: float = Field(default=0.98, ge=0, le=1)
    assumed_latency_ms: int = Field(default=500, ge=0)
    recent_volatility_bps: float = Field(default=0.0, ge=0)


class PaperCollectionRequest(BaseModel):
    matchbook_event_filters: dict[str, Any] = Field(default_factory=dict)
    polymarket_event_filters: dict[str, Any] = Field(default_factory=dict)
    matchbook_market_filters: dict[str, Any] = Field(default_factory=dict)
    polymarket_market_filters: dict[str, Any] = Field(default_factory=dict)
    fee_snapshots: list[FeeSnapshot] = Field(default_factory=list)
    fx_snapshots: list[FxRateSnapshot] = Field(default_factory=list)
    capital_limit_gbp: Decimal | None = Field(default=None, gt=0)
    minimum_net_edge: Decimal = Field(default=Decimal("0.005"), ge=0)
    maximum_execution_risk: int = Field(default=60, ge=0, le=100)
    minimum_mapping_confidence: float = Field(default=0.98, ge=0, le=1)
    assumed_latency_ms: int = Field(default=500, ge=0)
    recent_volatility_bps: float = Field(default=0.0, ge=0)
    max_event_pairs: int = Field(default=25, ge=1, le=100)
    max_market_pairs_per_event: int = Field(default=50, ge=1, le=200)


@lru_cache
def get_paper_audit_repository() -> SqlitePaperScanRepository:
    settings = get_settings()
    database = settings.paper_audit_db_path
    if database != ":memory:":
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
    return SqlitePaperScanRepository(database)


def get_paper_scan_service(
    intelligence: MarketIntelligenceService = Depends(get_market_intelligence_service),
) -> PaperScanService:
    return PaperScanService(intelligence)


@router.get("/scans", response_model=list[PaperScanRecord])
def recent_scans(
    limit: int = Query(default=100, ge=1, le=1000),
    eligible_only: bool = False,
    arbitrage_only: bool = False,
    since: datetime | None = None,
    repository: SqlitePaperScanRepository = Depends(get_paper_audit_repository),
) -> list[PaperScanRecord]:
    return repository.list_scans(
        limit=limit,
        eligible_only=eligible_only,
        arbitrage_only=arbitrage_only,
        since=since,
    )


@router.get("/scans/summary", response_model=PaperScanSummary)
def scan_summary(
    since: datetime | None = None,
    repository: SqlitePaperScanRepository = Depends(get_paper_audit_repository),
) -> PaperScanSummary:
    resolved_since = since or datetime.now(UTC).replace(hour=0, minute=0, second=0, microsecond=0)
    return repository.summary(since=resolved_since)


@router.post(
    "/scan/pair",
    response_model=PaperScanDecision,
    status_code=status.HTTP_200_OK,
)
def scan_pair(
    request: PaperPairScanRequest,
    service: PaperScanService = Depends(get_paper_scan_service),
    audit: SqlitePaperScanRepository = Depends(get_paper_audit_repository),
) -> PaperScanDecision:
    try:
        left = _build_observation(request.left)
        right = _build_observation(request.right)
        decision = service.scan_pair(
            left,
            right,
            fee_snapshots=request.fee_snapshots,
            fx_snapshots=request.fx_snapshots,
            capital_limit_gbp=request.capital_limit_gbp,
            minimum_net_edge=request.minimum_net_edge,
            maximum_execution_risk=request.maximum_execution_risk,
            minimum_mapping_confidence=request.minimum_mapping_confidence,
            assumed_latency_ms=request.assumed_latency_ms,
            recent_volatility_bps=request.recent_volatility_bps,
        )
        _persist_decision(decision, service=service, audit=audit)
        return decision
    except (VenueNormalizationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/collect",
    response_model=CollectionReport,
    status_code=status.HTTP_200_OK,
)
async def collect_read_only_market_data(
    request: PaperCollectionRequest,
    service: PaperScanService = Depends(get_paper_scan_service),
    audit: SqlitePaperScanRepository = Depends(get_paper_audit_repository),
) -> CollectionReport:
    """Run one explicit read-only collection/scan cycle."""

    settings = get_settings()
    matchbook = MatchbookClient(settings)
    polymarket = PolymarketClient(settings)
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        paper_scan=service,
    )
    try:
        report = await collector.collect_and_scan(
            matchbook_event_filters=request.matchbook_event_filters,
            polymarket_event_filters=request.polymarket_event_filters,
            matchbook_market_filters=request.matchbook_market_filters,
            polymarket_market_filters=request.polymarket_market_filters,
            fee_snapshots=request.fee_snapshots,
            fx_snapshots=request.fx_snapshots,
            capital_limit_gbp=request.capital_limit_gbp,
            minimum_net_edge=request.minimum_net_edge,
            maximum_execution_risk=request.maximum_execution_risk,
            minimum_mapping_confidence=request.minimum_mapping_confidence,
            assumed_latency_ms=request.assumed_latency_ms,
            recent_volatility_bps=request.recent_volatility_bps,
            max_event_pairs=request.max_event_pairs,
            max_market_pairs_per_event=request.max_market_pairs_per_event,
        )
        for decision in report.paper_decisions:
            _persist_decision(decision, service=service, audit=audit)
        return report
    except MatchbookAuthError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(status_code=502, detail=f"venue market-data request failed: {exc}") from exc
    finally:
        await matchbook.aclose()
        await polymarket.aclose()


def _persist_decision(
    decision: PaperScanDecision,
    *,
    service: PaperScanService,
    audit: SqlitePaperScanRepository,
) -> None:
    if not decision.canonical_market_id:
        return
    history = service.market_intelligence.market_history(
        canonical_market_id=decision.canonical_market_id,
    )
    if not history:
        return
    audit.append_scan(build_paper_scan_record(decision, history))


def _build_observation(request: RawVenueObservationRequest) -> VenueMarketObservation:
    if request.venue == VenueName.MATCHBOOK:
        return MatchbookObservationBuilder().build(
            request.event_payload,
            request.market_payload,
            observed_at=request.observed_at,
            native_currency=request.native_currency or "GBP",
            source_latency_ms=request.source_latency_ms,
            quote_age_ms=request.quote_age_ms,
        )
    if request.venue == VenueName.POLYMARKET:
        return PolymarketObservationBuilder().build(
            request.event_payload,
            request.market_payload,
            request.books_by_token,
            observed_at=request.observed_at,
            source_latency_ms=request.source_latency_ms,
            quote_age_ms=request.quote_age_ms,
        )
    raise ValueError(f"Paper scan does not yet support venue: {request.venue.value}")
