from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field

from sports_hedge.api.market_intelligence import get_market_intelligence_service
from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
    VenueMarketObservation,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.models import FeeSnapshot
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.normalization.venues import VenueNormalizationError
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision


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


def get_paper_scan_service(
    intelligence: MarketIntelligenceService = Depends(get_market_intelligence_service),
) -> PaperScanService:
    return PaperScanService(intelligence)


@router.post(
    "/scan/pair",
    response_model=PaperScanDecision,
    status_code=status.HTTP_200_OK,
)
def scan_pair(
    request: PaperPairScanRequest,
    service: PaperScanService = Depends(get_paper_scan_service),
) -> PaperScanDecision:
    try:
        left = _build_observation(request.left)
        right = _build_observation(request.right)
        return service.scan_pair(
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
    except (VenueNormalizationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


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
