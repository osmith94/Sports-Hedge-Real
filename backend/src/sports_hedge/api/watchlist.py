from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from sports_hedge.application.live_refresh import get_live_refresh_coordinator
from sports_hedge.application.scan_lanes import FRESHNESS_EXECUTABLE
from sports_hedge.arbitrage.watchlist.models import NearOpportunity, OpportunityLifecycleEvent
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
    return WatchlistService(
        repository,
        max_quote_age_ms=get_settings().paper_entry_max_quote_age_ms,
    )


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
    coordinator = get_live_refresh_coordinator()
    store = coordinator.fixture_current_state()
    if not store.has_collection():
        return []
    now = service._clock()
    settings = get_settings()
    radar_kwargs = coordinator.radar_horizon_kwargs(settings)
    cohort_ids = store.current_tracked_opportunity_ids(now, **radar_kwargs)
    rows = _read_watchlist(
        service.tracked,
        operations,
        limit=limit,
        competition=competition,
        venue=venue,
        market_family=market_family,
        collection_cohort_ids=cohort_ids,
        as_of=now,
    )
    annotated: list[NearOpportunity] = []
    for row in rows:
        meta = store.radar_meta_for_market(row.canonical_market_id, now, **radar_kwargs)
        if meta is None:
            continue
        freshness = meta.freshness
        if row.quote_age_ms is not None and row.quote_age_ms < service.max_quote_age_ms:
            freshness = FRESHNESS_EXECUTABLE
        executable = freshness == FRESHNESS_EXECUTABLE
        annotated.append(
            row.model_copy(
                update={
                    "scan_lane": meta.observation_lane.value,
                    "last_scanned_at": meta.last_scanned_at,
                    "next_due_at": meta.next_due_at,
                    "freshness_class": freshness,
                    "bet_actionable": bool(row.bet_actionable) and executable,
                    "bet_blocked_reason": (
                        row.bet_blocked_reason
                        if executable
                        else (row.bet_blocked_reason or "radar_current_not_executable")
                    ),
                }
            )
        )
    return annotated


@router.get("/activity", response_model=list[OpportunityLifecycleEvent])
def recent_lifecycle_activity(
    limit: int = Query(default=100, ge=1, le=1000),
    opportunity_id: str | None = None,
    canonical_event_id: str | None = None,
    since: datetime | None = None,
    operator_signal: bool = Query(
        default=False,
        description="Primary operator Activity feed only. Unfiltered remains the audit trail.",
    ),
    service: WatchlistService = Depends(get_watchlist_service),
) -> list[OpportunityLifecycleEvent]:
    return service.activity(
        limit=limit,
        opportunity_id=opportunity_id,
        canonical_event_id=canonical_event_id,
        since=since,
        operator_signal=operator_signal,
    )


def _read_watchlist(reader, operations: PaperOperationsService, **kwargs):
    try:
        items = reader(**kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return operations.annotate_bet_ticket_actions(items)
