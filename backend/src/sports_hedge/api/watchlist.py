from __future__ import annotations

from datetime import datetime
from functools import lru_cache
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Query

from sports_hedge.application.live_refresh import get_live_refresh_coordinator
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.application.scan_lanes import (
    FRESHNESS_EXECUTABLE,
    FRESHNESS_RADAR_CURRENT,
)
from sports_hedge.arbitrage.watchlist.models import NearOpportunity, OpportunityLifecycleEvent
from sports_hedge.arbitrage.watchlist.price2_activity import (
    PRICE2_ACTIVITY_DEFAULT_LIMIT,
    PRICE2_ACTIVITY_MAX_LIMIT,
    Price2ActivityObservation,
    normalize_opportunity_ids,
)
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
    limit: int = Query(
        default=20,
        ge=1,
        le=500,
        description=(
            "Opportunity Monitor asks for 20, or 50 when the operator expands "
            "the snapshot. The limit is applied after recency order. Values "
            "above 50 are engineering stress reads, not the ordinary monitor."
        ),
    ),
    competition: str | None = None,
    venue: VenueName | None = None,
    market_family: MarketFamily | None = None,
    service: WatchlistService = Depends(get_watchlist_service),
    operations: PaperOperationsService = Depends(get_bet_ticket_operations),
) -> list[NearOpportunity]:
    coordinator = get_live_refresh_coordinator()
    store = coordinator.fixture_current_state()
    now = service._clock()
    settings = get_settings()
    radar_kwargs = coordinator.radar_horizon_kwargs(settings)
    if not store.has_collection():
        return []
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
    # One catalogue pass for every returned row. Per-row radar and clock scans
    # re-prune the whole fixture store under the same lock.
    annotations = store.tracked_annotations(
        [
            (
                row.canonical_market_id,
                tuple(leg.source_market_id for leg in row.legs if leg.source_market_id),
            )
            for row in rows
        ],
        now,
        **radar_kwargs,
    )
    for row in rows:
        found = annotations.get(row.canonical_market_id)
        if found is None:
            continue
        meta, clock = found
        # Executable is the persisted economics quote, not the price-slot quote.
        # A BACKGROUND/HOT refresh can carry a new 80ms provider quote while
        # the watchlist edge is still the older observation. Unknown stays
        # non-executable. Expired rows never reach this annotation.
        economics_executable = (
            row.quote_age_ms is not None and row.quote_age_ms < service.max_quote_age_ms
        )
        freshness = FRESHNESS_EXECUTABLE if economics_executable else FRESHNESS_RADAR_CURRENT
        executable = freshness == FRESHNESS_EXECUTABLE
        annotated.append(
            row.model_copy(
                update={
                    "scan_lane": meta.observation_lane.value,
                    "last_scanned_at": meta.last_scanned_at,
                    "last_discovered_at": clock.discovered_at,
                    "last_priced_at": clock.priced_at,
                    "price_lane": clock.price_lane,
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


@router.get("/price2-attempts", response_model=list[Price2ActivityObservation])
def price2_attempts(
    opportunity_ids: str = Query(
        ...,
        description="Comma-separated opportunity ids from the visible Activity page (max 100).",
    ),
    since: datetime | None = None,
    limit: int = Query(
        default=PRICE2_ACTIVITY_DEFAULT_LIMIT,
        ge=1,
        le=PRICE2_ACTIVITY_MAX_LIMIT,
    ),
    service: WatchlistService = Depends(get_watchlist_service),
) -> list[Price2ActivityObservation]:
    """Read-only Price-2 audit projection. Requires the visible opportunity set."""

    try:
        ids = normalize_opportunity_ids(opportunity_ids)
        return service.price2_activity(opportunity_ids=ids, since=since, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _read_watchlist(reader, operations: PaperOperationsService, **kwargs):
    try:
        items = reader(**kwargs)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return operations.annotate_bet_ticket_actions(items)
