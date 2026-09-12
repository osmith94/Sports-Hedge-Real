from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from pathlib import Path
from typing import Any

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator

from sports_hedge.api.market_intelligence import get_market_intelligence_service
from sports_hedge.api.priority_alerts import get_priority_alert_service
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.application.paper_operations import PaperOperationsError, PaperOperationsService
from sports_hedge.application.collector import CollectionReport, ReadOnlyCrossVenueCollector
from sports_hedge.application.live_refresh import (
    LiveRefreshStatus,
    get_live_refresh_coordinator,
)
from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
    VenueMarketObservation,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import VenueCostSnapshot
from sports_hedge.fees.models import FeeSnapshot
from sports_hedge.fees.resolver import VenueCostResolver
from sports_hedge.fx.models import FxRateUnavailable
from sports_hedge.fx.repository import SqliteFxRateRepository
from sports_hedge.fx.service import FxRateService
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.normalization.venues import VenueNormalizationError
from sports_hedge.paper.audit import (
    PaperScanRecord,
    PaperScanSummary,
    build_paper_scan_record,
)
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.paper.chain import SimulatePaperFillRequest, SimulatePaperFillResult
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision
from sports_hedge.paper.liquidity import PaperLiquiditySnapshot
from sports_hedge.paper.trades import (
    PaperSettlementRequest,
    PaperTrade,
    PaperTradeBookSummary,
    PaperTradeDetail,
)
from sports_hedge.paper.unwind.models import PaperClosePlanRequest, UnwindDecision
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from sports_hedge.persistence.liquidity import SqlitePaperLiquidityRepository
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.venues.matchbook import (
    MatchbookAuthError,
    MatchbookClient,
    MatchbookDiscoveryError,
)
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
    quote_age_ms: int | None = Field(default=None, ge=0)


class PaperPairScanRequest(BaseModel):
    left: RawVenueObservationRequest
    right: RawVenueObservationRequest
    fee_snapshots: list[FeeSnapshot] = Field(default_factory=list)
    venue_costs: list[VenueCostSnapshot] = Field(default_factory=list)
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
    capital_limit_gbp: Decimal | None = Field(default=None, gt=0)
    minimum_net_edge: Decimal = Field(default=Decimal("0.005"), ge=0)
    maximum_execution_risk: int = Field(default=60, ge=0, le=100)
    minimum_mapping_confidence: float = Field(default=0.98, ge=0, le=1)
    assumed_latency_ms: int = Field(default=500, ge=0)
    recent_volatility_bps: float = Field(default=0.0, ge=0)
    max_event_pairs: int = Field(default=25, ge=1, le=100)
    max_market_pairs_per_event: int = Field(default=50, ge=1, le=200)

    @model_validator(mode="before")
    @classmethod
    def reject_client_economics(cls, value: Any) -> Any:
        if isinstance(value, dict):
            for key in ("fee_snapshots", "venue_costs", "fx_snapshots"):
                if value.get(key):
                    raise ValueError(
                        "live collection rejects client FX and venue-cost assumptions; "
                        "economics resolve on the backend"
                    )
                value.pop(key, None)
        return value


class EconomicsStatus(BaseModel):
    as_of: datetime
    data_kind: str = "backend_resolved"
    fx: list[dict[str, Any]] = Field(default_factory=list)
    venue_costs: list[dict[str, Any]] = Field(default_factory=list)
    issues: list[str] = Field(default_factory=list)


class PaperPoolUpdate(BaseModel):
    venue: VenueName
    available: Decimal = Field(ge=0)
    locked: Decimal | None = Field(default=None, ge=0)
    transit: Decimal | None = Field(default=None, ge=0)


class PaperLiquidityUpdateRequest(BaseModel):
    pools: list[PaperPoolUpdate] = Field(min_length=1, max_length=3)


@lru_cache
def get_paper_audit_repository() -> SqlitePaperScanRepository:
    settings = get_settings()
    database = settings.paper_audit_db_path
    if database != ":memory:":
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
    return SqlitePaperScanRepository(database)


@lru_cache
def get_fx_rate_repository() -> SqliteFxRateRepository:
    settings = get_settings()
    database = settings.fx_db_path
    if database != ":memory:":
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteFxRateRepository(database)


def get_fx_rate_service() -> FxRateService:
    settings = get_settings()
    return FxRateService(
        get_fx_rate_repository(),
        check_tolerance_bps=Decimal(settings.fx_check_tolerance_bps),
        stale_after_days=settings.fx_stale_after_days,
    )


@lru_cache
def get_accounting_schedule():
    from sports_hedge.fx.schedule import AccountingSchedule

    settings = get_settings()
    return AccountingSchedule(
        get_fx_rate_service(),
        journal=get_paper_journal_holder().journal,
        enabled=settings.accounting_schedule_enabled,
    )


@lru_cache
def get_venue_cost_resolver() -> VenueCostResolver:
    return VenueCostResolver()


@lru_cache
def get_paper_liquidity_repository() -> SqlitePaperLiquidityRepository:
    settings = get_settings()
    database = settings.paper_liquidity_db_path
    if database != ":memory:":
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
    return SqlitePaperLiquidityRepository(
        database,
        matchbook_gbp=Decimal(str(settings.paper_bankroll_gbp)),
        polymarket_usd=Decimal(str(settings.paper_bankroll_usd)),
    )


def get_paper_scan_service(
    intelligence: MarketIntelligenceService = Depends(get_market_intelligence_service),
    fx: FxRateService = Depends(get_fx_rate_service),
    costs: VenueCostResolver = Depends(get_venue_cost_resolver),
    liquidity: SqlitePaperLiquidityRepository = Depends(get_paper_liquidity_repository),
) -> PaperScanService:
    open_trades = []
    try:
        open_trades = get_paper_ledger().trades.list_active()
    except Exception:
        open_trades = []
    return PaperScanService(
        intelligence,
        fx_service=fx,
        cost_resolver=costs,
        liquidity=liquidity,
        open_trades=open_trades,
    )


@lru_cache
def get_paper_ledger() -> SqlitePaperLedger:
    settings = get_settings()
    database = settings.paper_ledger_db_path
    if database != ":memory:":
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
    return SqlitePaperLedger(database)


@lru_cache
def get_paper_journal_holder() -> PaperOperationsService:
    """Process-local paper chain backed by the durable paper ledger."""

    from sports_hedge.api.watchlist import get_watchlist_repository, get_watchlist_service

    return PaperOperationsService(
        watchlist=get_watchlist_service(get_watchlist_repository()),
        alerts=get_priority_alert_service(),
        ledger=get_paper_ledger(),
    )


def get_paper_operations_service(
    watchlist: WatchlistService = Depends(get_watchlist_service),
    alerts: PriorityAlertService = Depends(get_priority_alert_service),
) -> PaperOperationsService:
    holder = get_paper_journal_holder()
    holder.watchlist = watchlist
    holder.alerts = alerts
    return holder


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


@router.get("/liquidity-pools", response_model=PaperLiquiditySnapshot)
def paper_liquidity_pools(
    repository: SqlitePaperLiquidityRepository = Depends(get_paper_liquidity_repository),
    fx: FxRateService = Depends(get_fx_rate_service),
) -> PaperLiquiditySnapshot:
    rates, source = _backend_fx_for_pools(fx)
    return repository.get(gbp_per_unit=rates, fx_source=source)


@router.post("/liquidity-pools", response_model=PaperLiquiditySnapshot)
def update_paper_liquidity_pools(
    request: PaperLiquidityUpdateRequest,
    repository: SqlitePaperLiquidityRepository = Depends(get_paper_liquidity_repository),
    fx: FxRateService = Depends(get_fx_rate_service),
) -> PaperLiquiditySnapshot:
    available = {item.venue: item.available for item in request.pools}
    locked = {item.venue: item.locked for item in request.pools if item.locked is not None}
    transit = {item.venue: item.transit for item in request.pools if item.transit is not None}
    repository.update_available(available, locked=locked or None, transit=transit or None)
    rates, source = _backend_fx_for_pools(fx)
    return repository.get(gbp_per_unit=rates, fx_source=source)


@router.post("/liquidity-pools/reset", response_model=PaperLiquiditySnapshot)
def reset_paper_liquidity_pools(
    repository: SqlitePaperLiquidityRepository = Depends(get_paper_liquidity_repository),
    fx: FxRateService = Depends(get_fx_rate_service),
) -> PaperLiquiditySnapshot:
    repository.reset()
    rates, source = _backend_fx_for_pools(fx)
    return repository.get(gbp_per_unit=rates, fx_source=source)


@router.post(
    "/scan/pair",
    response_model=PaperScanDecision,
    status_code=status.HTTP_200_OK,
)
def scan_pair(
    request: PaperPairScanRequest,
    service: PaperScanService = Depends(get_paper_scan_service),
    audit: SqlitePaperScanRepository = Depends(get_paper_audit_repository),
    watchlist: WatchlistService = Depends(get_watchlist_service),
) -> PaperScanDecision:
    try:
        left = _build_observation(request.left)
        right = _build_observation(request.right)
        decision = service.scan_pair(
            left,
            right,
            fee_snapshots=request.fee_snapshots or None,
            venue_costs=request.venue_costs or None,
            fx_snapshots=request.fx_snapshots or None,
            capital_limit_gbp=request.capital_limit_gbp,
            minimum_net_edge=request.minimum_net_edge,
            maximum_execution_risk=request.maximum_execution_risk,
            minimum_mapping_confidence=request.minimum_mapping_confidence,
            assumed_latency_ms=request.assumed_latency_ms,
            recent_volatility_bps=request.recent_volatility_bps,
        )
        _persist_decision(
            decision,
            service=service,
            audit=audit,
            watchlist=watchlist,
            operations=get_paper_operations_service(watchlist, get_priority_alert_service()),
            quote_age_ms=decision.quote_age_ms,
        )
        return decision
    except (VenueNormalizationError, ValueError) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.get("/economics-status", response_model=EconomicsStatus)
def economics_status(
    fx: FxRateService = Depends(get_fx_rate_service),
    costs: VenueCostResolver = Depends(get_venue_cost_resolver),
) -> EconomicsStatus:
    as_of = datetime.now(UTC)
    issues: list[str] = []
    fx_rows = []
    try:
        for rate in fx.economics_status(as_of=as_of):
            retrieved = rate.retrieved_at or rate.captured_at
            fx_rows.append(
                {
                    "currency": rate.currency,
                    "gbp_per_unit": str(rate.gbp_per_unit),
                    "source_date": rate.source_date.isoformat(),
                    "valuation_date": rate.valuation_date.isoformat(),
                    "retrieved_at": retrieved.isoformat() if retrieved is not None else None,
                    "status": rate.status.value,
                    "primary_source": rate.primary_source,
                    "variance_bps": None if rate.variance_bps is None else str(rate.variance_bps),
                    "check_source": rate.check_source,
                }
            )
            try:
                fx.resolve_for_scanner(rate.currency, as_of=as_of)
            except FxRateUnavailable as exc:
                issues.append(exc.reason)
    except Exception as exc:  # noqa: BLE001
        issues.append(str(exc))
    if not any(row["currency"] == "USD" for row in fx_rows):
        issues.append("missing_fx_rate:USD")
    venue_rows = [
        snapshot.model_dump(mode="json")
        for snapshot in costs.list_status(as_of=as_of)
        if snapshot.market_class in {"both_teams_to_score", "match_result", "player_props"}
    ]
    return EconomicsStatus(as_of=as_of, fx=fx_rows, venue_costs=venue_rows, issues=issues)


@router.get("/live-refresh", response_model=LiveRefreshStatus)
def live_refresh_status() -> LiveRefreshStatus:
    coordinator = get_live_refresh_coordinator()
    coordinator.configure_from_settings()
    return coordinator.status


@router.post(
    "/collect",
    response_model=CollectionReport,
    status_code=status.HTTP_200_OK,
)
async def collect_read_only_market_data(
    request: PaperCollectionRequest,
    service: PaperScanService = Depends(get_paper_scan_service),
    audit: SqlitePaperScanRepository = Depends(get_paper_audit_repository),
    watchlist: WatchlistService = Depends(get_watchlist_service),
) -> CollectionReport:
    """Run one explicit read-only collection/scan cycle and persist watchlist observations."""

    kwargs = request.model_dump()
    coordinator = get_live_refresh_coordinator()
    coordinator.remember_request(kwargs)

    async def runner() -> CollectionReport:
        return await _execute_collection(
            kwargs,
            service=service,
            audit=audit,
            watchlist=watchlist,
        )

    try:
        return await coordinator.run_cycle(runner)
    except (MatchbookAuthError, MatchbookDiscoveryError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"venue market-data request failed: {exc}"
        ) from exc


async def _execute_collection(
    kwargs: dict[str, Any],
    *,
    service: PaperScanService,
    audit: SqlitePaperScanRepository,
    watchlist: WatchlistService,
) -> CollectionReport:
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
            **kwargs,
            polymarket_queried_series_ids=settings.resolved_polymarket_series_ids(),
        )
        for decision in report.paper_decisions:
            _persist_decision(
                decision,
                service=service,
                audit=audit,
                watchlist=watchlist,
                operations=get_paper_operations_service(watchlist, get_priority_alert_service()),
            )
        return report
    finally:
        await matchbook.aclose()
        await polymarket.aclose()


async def server_owned_refresh_tick() -> None:
    """Background tick used when PAPER_LIVE_REFRESH_ENABLED is true."""

    coordinator = get_live_refresh_coordinator()
    service = get_paper_scan_service(
        get_market_intelligence_service(),
        get_fx_rate_service(),
        get_venue_cost_resolver(),
        get_paper_liquidity_repository(),
    )
    audit = get_paper_audit_repository()
    from sports_hedge.api.watchlist import get_watchlist_repository, get_watchlist_service

    watchlist = get_watchlist_service(get_watchlist_repository())
    kwargs = coordinator.last_request() or PaperCollectionRequest().model_dump()

    async def runner() -> CollectionReport:
        return await _execute_collection(
            kwargs,
            service=service,
            audit=audit,
            watchlist=watchlist,
        )

    try:
        await coordinator.run_cycle(runner)
    except (MatchbookAuthError, MatchbookDiscoveryError, httpx.HTTPError):
        return


@router.get("/trades/summary", response_model=PaperTradeBookSummary)
def paper_trade_summary(
    operations: PaperOperationsService = Depends(get_paper_operations_service),
) -> PaperTradeBookSummary:
    return operations.book_summary()


@router.get("/trades/active", response_model=list[PaperTrade])
def active_paper_trades(
    operations: PaperOperationsService = Depends(get_paper_operations_service),
) -> list[PaperTrade]:
    return operations.list_active_trades()


@router.get("/trades/closed", response_model=list[PaperTrade])
def closed_paper_trades(
    operations: PaperOperationsService = Depends(get_paper_operations_service),
) -> list[PaperTrade]:
    return operations.list_closed_trades()


@router.get("/trades/{trade_id}", response_model=PaperTradeDetail)
def paper_trade_detail(
    trade_id: str,
    operations: PaperOperationsService = Depends(get_paper_operations_service),
) -> PaperTradeDetail:
    try:
        return operations.trade_detail(trade_id)
    except PaperOperationsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.post("/trades/{trade_id}/close-plan", response_model=UnwindDecision)
def paper_trade_close_plan(
    trade_id: str,
    request: PaperClosePlanRequest,
    operations: PaperOperationsService = Depends(get_paper_operations_service),
) -> UnwindDecision:
    """PAPER-ONLY close-plan evaluation. Does not release capital or place orders."""

    if request.places_orders:
        raise HTTPException(status_code=422, detail="close-plan evaluation cannot place orders")
    try:
        return operations.evaluate_unwind(
            trade_id,
            quotes=request.quotes,
            fx=request.fx,
            policy=request.policy,
            scarcity=request.scarcity,
        )
    except PaperOperationsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/trades/{trade_id}/settle", response_model=PaperTradeDetail)
def settle_paper_trade(
    trade_id: str,
    request: PaperSettlementRequest,
    operations: PaperOperationsService = Depends(get_paper_operations_service),
) -> PaperTradeDetail:
    """PAPER-ONLY explicit settlement. Does not infer a result from kickoff time."""

    try:
        return operations.settle(trade_id, request)
    except PaperOperationsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/simulate-fill",
    response_model=SimulatePaperFillResult,
    status_code=status.HTTP_200_OK,
)
def simulate_paper_fill(
    request: SimulatePaperFillRequest,
    operations: PaperOperationsService = Depends(get_paper_operations_service),
) -> SimulatePaperFillResult:
    """PAPER-ONLY operator action. Never places a venue order or signs a wallet."""

    try:
        return operations.simulate_fill(
            request.opportunity_id,
            capital_source=request.capital_source,
            confirm_external=request.confirm_external,
            provenance=request.provenance,
            operator_note=request.operator_note,
        )
    except PaperOperationsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


def _persist_decision(
    decision: PaperScanDecision,
    *,
    service: PaperScanService,
    audit: SqlitePaperScanRepository,
    watchlist: WatchlistService,
    operations: PaperOperationsService | None = None,
    quote_age_ms: int | None = None,
) -> None:
    if not decision.canonical_market_id:
        return
    history = service.market_intelligence.market_history(
        canonical_market_id=decision.canonical_market_id,
    )
    if history:
        audit.append_scan(build_paper_scan_record(decision, history))
    watchlist.observe_paper_decision(decision, history, quote_age_ms=quote_age_ms)
    if operations is not None:
        operations.persist_triggered_chain(decision)


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


def _backend_fx_for_pools(fx: FxRateService) -> tuple[dict[str, Decimal], str | None]:
    rates: dict[str, Decimal] = {"GBP": Decimal("1")}
    try:
        snapshots = fx.paper_snapshots({"USD", "GBP"}, as_of=datetime.now(UTC))
    except FxRateUnavailable:
        return rates, None
    source = None
    for snapshot in snapshots:
        rates[snapshot.currency] = snapshot.gbp_per_unit
        if snapshot.currency == "USD":
            source = snapshot.source
    return rates, source
