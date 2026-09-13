from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from sports_hedge.application.live_refresh import get_live_refresh_coordinator
from sports_hedge.arbitrage.watchlist.models import NearOpportunity, OpportunityLifecycleEvent
from sports_hedge.arbitrage.watchlist.ranking import tracked_cohort_opportunity_ids
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.application.paper_operations import PaperOperationsService
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


def get_bet_ticket_operations() -> PaperOperationsService:
    from sports_hedge.api.paper import get_paper_journal_holder

    return get_paper_journal_holder()


@router.get("/near", response_model=list[NearOpportunity])
def top_near_opportunities(
    limit: int = Query(default=25, ge=1, le=200),
    competition: str | None = None,
    venue: VenueName | None = None,
    market_family: MarketFamily | None = None,
    service: WatchlistService = Depends(get_watchlist_service),
    operations: PaperOperationsService = Depends(get_bet_ticket_operations),
) -> list[NearOpportunity]:
    return _read_watchlist(
        service.top_near,
        operations,
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
    operations: PaperOperationsService = Depends(get_bet_ticket_operations),
) -> list[NearOpportunity]:
    return _read_watchlist(
        service.triggered,
        operations,
        limit=limit,
        competition=competition,
        venue=venue,
        market_family=market_family,
    )


@router.get("/tracked", response_model=list[NearOpportunity])
def tracked_markets(
    limit: int = Query(default=100, ge=1, le=500),
    competition: str | None = None,
    venue: VenueName | None = None,
    market_family: MarketFamily | None = None,
    service: WatchlistService = Depends(get_watchlist_service),
    operations: PaperOperationsService = Depends(get_bet_ticket_operations),
) -> list[NearOpportunity]:
    report = get_live_refresh_coordinator().last_report()
    if report is None:
        return []
    cohort_ids = tracked_cohort_opportunity_ids(
        decision.canonical_market_id for decision in report.paper_decisions
    )
    return _read_watchlist(
        service.tracked,
        operations,
        limit=limit,
        competition=competition,
        venue=venue,
        market_family=market_family,
        collection_cohort_ids=cohort_ids,
    )


@router.get("/activity", response_model=list[OpportunityLifecycleEvent])
def recent_lifecycle_activity(
    limit: int = Query(default=100, ge=1, le=1000),
    opportunity_id: str | None = None,
    since: datetime | None = None,
    service: WatchlistService = Depends(get_watchlist_service),
) -> list[OpportunityLifecycleEvent]:
    return service.activity(limit=limit, opportunity_id=opportunity_id, since=since)


def _read_watchlist(reader, operations: PaperOperationsService, **kwargs):
    try:
        items = reader(**kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return operations.annotate_bet_ticket_actions(items)
