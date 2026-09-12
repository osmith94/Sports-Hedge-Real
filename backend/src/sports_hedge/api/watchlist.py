from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, Query

from sports_hedge.arbitrage.watchlist.models import NearOpportunity, OpportunityLifecycleEvent
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import get_settings
from sports_hedge.domain.football import MarketFamily
from sports_hedge.domain.models import VenueName

router = APIRouter(prefix="/paper/watchlist", tags=["paper-watchlist"])


@lru_cache
def get_watchlist_repository() -> SqliteWatchlistRepository:
    settings = get_settings()
    database = settings.watchlist_db_path
    if database != ":memory:":
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteWatchlistRepository(database)


def get_watchlist_service(
    repository: SqliteWatchlistRepository = Depends(get_watchlist_repository),
) -> WatchlistService:
    return WatchlistService(repository)


@router.get("/near", response_model=list[NearOpportunity])
def top_near_opportunities(
    limit: int = Query(default=25, ge=1, le=200),
    competition: str | None = None,
    venue: VenueName | None = None,
    market_family: MarketFamily | None = None,
    service: WatchlistService = Depends(get_watchlist_service),
) -> list[NearOpportunity]:
    return service.top_near(
        limit=limit,
        competition=competition,
        venue=venue,
        market_family=market_family,
    )


@router.get("/triggered", response_model=list[NearOpportunity])
def triggered_opportunities(
    limit: int = Query(default=25, ge=1, le=200),
    competition: str | None = None,
    venue: VenueName | None = None,
    market_family: MarketFamily | None = None,
    service: WatchlistService = Depends(get_watchlist_service),
) -> list[NearOpportunity]:
    return service.triggered(
        limit=limit,
        competition=competition,
        venue=venue,
        market_family=market_family,
    )


@router.get("/activity", response_model=list[OpportunityLifecycleEvent])
def recent_lifecycle_activity(
    limit: int = Query(default=100, ge=1, le=1000),
    opportunity_id: str | None = None,
    since: datetime | None = None,
    service: WatchlistService = Depends(get_watchlist_service),
) -> list[OpportunityLifecycleEvent]:
    return service.activity(limit=limit, opportunity_id=opportunity_id, since=since)
