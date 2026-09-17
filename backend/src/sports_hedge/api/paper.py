from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from functools import lru_cache
from logging import getLogger
from pathlib import Path
from time import monotonic
from typing import Any
from uuid import uuid4

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field, model_validator

from sports_hedge.api.market_intelligence import get_market_intelligence_service
from sports_hedge.api.priority_alerts import get_priority_alert_service
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.application.collector import (
    DEFAULT_MAX_EVENT_PAIRS,
    CollectionReport,
    ReadOnlyCrossVenueCollector,
    acknowledge_task_cancellation,
)
from sports_hedge.application.demo_walkthrough import (
    DemoCloseRequest,
    DemoResetRequest,
    DemoWalkthroughService,
    DemoWalkthroughSnapshot,
    FixtureReplayRequest,
    FixtureReplayResult,
)
from sports_hedge.application.lane_venues import (
    LaneVenueParticipation,
)
from sports_hedge.application.live_refresh import (
    ExplicitCollectBusy,
    LiveRefreshStatus,
    ScanCycleTimeout,
    get_live_refresh_coordinator,
)
from sports_hedge.application.market_observation import (
    KalshiObservationBuilder,
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
    VenueMarketObservation,
)
from sports_hedge.application.paper_operations import PaperOperationsError, PaperOperationsService
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_cycle_audit import build_paper_scan_cycle_record
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
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
    PaperScanCycleRecord,
    PaperScanRecord,
    PaperScanSummary,
    build_paper_scan_record,
)
from sports_hedge.paper.bet_ticket import (
    RecommendedPaperDeployment,
    RecommendPaperDeploymentRequest,
)
from sports_hedge.paper.chain import SimulatePaperFillRequest, SimulatePaperFillResult
from sports_hedge.paper.liquidity import PaperLiquiditySnapshot
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision
from sports_hedge.paper.preparation import PreparedPaperDeployment, PreparePaperDeploymentRequest
from sports_hedge.paper.trades import (
    PaperSettlementRequest,
    PaperTrade,
    PaperTradeBookSummary,
    PaperTradeDetail,
)
from sports_hedge.paper.unwind.models import PaperClosePlanRequest, UnwindDecision, UnwindPolicy
from sports_hedge.paper.position_management.models import PositionManagementSnapshot
from sports_hedge.persistence.liquidity import SqlitePaperLiquidityRepository
from sports_hedge.persistence.matchbook_account_fee import (
    MatchbookAccountFeeStatus,
    MatchbookFeeUpdate,
    SqliteMatchbookAccountFeeStore,
)
from sports_hedge.persistence.mapping_rules import get_mapping_rule_store
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.treasury.models import (
    PaperTreasurySnapshot,
    TreasuryLockRequest,
    ValidatedUnwindResult,
)
from sports_hedge.treasury.service import PaperTreasuryError
from sports_hedge.application.provider_runtime import get_shared_provider_runtime
from sports_hedge.venues.matchbook import (
    MatchbookAuthError,
    MatchbookDiscoveryError,
)

router = APIRouter(prefix="/paper", tags=["paper"])
LOGGER = getLogger(__name__)


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
    max_event_pairs: int = Field(default=DEFAULT_MAX_EVENT_PAIRS, ge=1, le=100)
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
    matchbook_fee: MatchbookAccountFeeStatus | None = None
    polymarket_fee_policy: dict[str, Any] = Field(default_factory=dict)
    issues: list[str] = Field(default_factory=list)
    fx_schedule: dict[str, Any] = Field(default_factory=dict)


class LaneVenueFlags(BaseModel):
    matchbook: bool = True
    polymarket: bool = True
    kalshi: bool = True

    def as_venues(self) -> list[VenueName]:
        mapping = (
            (VenueName.MATCHBOOK, self.matchbook),
            (VenueName.POLYMARKET, self.polymarket),
            (VenueName.KALSHI, self.kalshi),
        )
        return [venue for venue, enabled in mapping if enabled]


class LaneVenueParticipationUpdate(BaseModel):
    hot: LaneVenueFlags
    universe: LaneVenueFlags


class PaperPoolUpdate(BaseModel):
    venue: VenueName
    available: Decimal = Field(ge=0)
    locked: Decimal | None = Field(default=None, ge=0)
    transit: Decimal | None = Field(default=None, ge=0)


class PaperTreasuryResetRequest(BaseModel):
    seed_gbp: Decimal | None = Field(default=None, gt=0)
    usd_gbp_per_unit: Decimal | None = Field(default=None, gt=0)
    fx_source: str | None = None
    include_kalshi: bool | None = None
    reason: str = "explicit paper treasury demo reset"


class PaperTreasuryPoolUpdate(BaseModel):
    venue: VenueName
    available: Decimal = Field(ge=0)


class PaperTreasuryAdjustRequest(BaseModel):
    pools: list[PaperTreasuryPoolUpdate] = Field(min_length=1, max_length=3)
    reason: str = "operator paper treasury edit"


class PaperTreasuryFxRequest(BaseModel):
    usd_gbp_per_unit: Decimal = Field(gt=0)
    fx_source: str = Field(min_length=1)
    fx_as_of: datetime | None = None


class PaperLiquidityUpdateRequest(BaseModel):
    pools: list[PaperPoolUpdate] = Field(min_length=1, max_length=4)


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
def get_matchbook_account_fee_store() -> SqliteMatchbookAccountFeeStore:
    settings = get_settings()
    database = settings.paper_account_fees_db_path
    if database != ":memory:":
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
    return SqliteMatchbookAccountFeeStore(database)


@lru_cache
def get_venue_cost_resolver() -> VenueCostResolver:
    return VenueCostResolver(matchbook_fee_store=get_matchbook_account_fee_store())


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
        kalshi_usd=Decimal(str(settings.paper_bankroll_kalshi_usd)),
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
        mapping_rule_store=get_mapping_rule_store(),
        reverse_catalog=get_reverse_book_catalog(),
    )


@lru_cache
def get_reverse_book_catalog():
    from sports_hedge.paper.position_management.quotes import LatestObservationCatalog

    return LatestObservationCatalog()


@lru_cache
def get_paper_position_manager():
    from sports_hedge.paper.position_management import PaperPositionManager

    return PaperPositionManager(
        get_paper_journal_holder(),
        observation_catalog=get_reverse_book_catalog(),
        cost_resolver=get_venue_cost_resolver(),
    )


@lru_cache
def get_paper_ledger() -> SqlitePaperLedger:
    settings = get_settings()
    database = settings.paper_ledger_db_path
    if database != ":memory:":
        path = Path(database)
        path.parent.mkdir(parents=True, exist_ok=True)
    return SqlitePaperLedger(
        database,
        seed_gbp=Decimal(str(settings.paper_treasury_seed_gbp)),
        usd_gbp_per_unit=Decimal(str(settings.paper_treasury_demo_usd_gbp_per_unit)),
        fx_source=settings.paper_treasury_demo_fx_source,
        include_kalshi=settings.paper_treasury_include_kalshi,
    )


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


@lru_cache
def get_demo_walkthrough_service() -> DemoWalkthroughService:
    from sports_hedge.api.watchlist import get_watchlist_repository, get_watchlist_service

    watchlist = get_watchlist_service(get_watchlist_repository())
    operations = get_paper_journal_holder()
    operations.watchlist = watchlist
    operations.alerts = get_priority_alert_service()
    scan = PaperScanService(
        get_market_intelligence_service(),
        fx_service=get_fx_rate_service(),
        cost_resolver=get_venue_cost_resolver(),
        liquidity=get_paper_liquidity_repository(),
    )
    return DemoWalkthroughService(
        operations=operations,
        scan=scan,
        watchlist=watchlist,
        ledger=get_paper_ledger(),
        liquidity=get_paper_liquidity_repository(),
    )


@router.get("/scans", response_model=list[PaperScanRecord])
def recent_scans(
    limit: int = Query(default=100, ge=1, le=1000),
    eligible_only: bool = False,
    arbitrage_only: bool = False,
    since: datetime | None = None,
    repository: SqlitePaperScanRepository = Depends(get_paper_audit_repository),
) -> list[PaperScanRecord]:
    """Newest-first paper scan *audit* window. Not radar current-state."""
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


@router.get("/scan-cycles", response_model=list[PaperScanCycleRecord])
def recent_scan_cycles(
    limit: int = Query(default=100, ge=1, le=1000),
    repository: SqlitePaperScanRepository = Depends(get_paper_audit_repository),
) -> list[PaperScanCycleRecord]:
    """Newest-first completed HOT/UNIVERSE refresh cycles. Not market-decision rows."""
    return repository.list_cycles(limit=limit)


@router.get("/treasury", response_model=PaperTreasurySnapshot)
def paper_treasury(
    ledger: SqlitePaperLedger = Depends(get_paper_ledger),
    limit: int = Query(default=50, ge=1, le=200),
) -> PaperTreasurySnapshot:
    return ledger.treasury.snapshot(event_limit=limit)


@router.get("/ledger/reconciliation")
def paper_ledger_reconciliation(
    ledger: SqlitePaperLedger = Depends(get_paper_ledger),
):
    """Read-only proof that native treasury pools reconstruct from the append-only paper journal."""

    report = ledger.reconcile()
    payload = report.model_dump()
    payload["ok"] = report.ok
    payload["paper_only"] = True
    payload["places_orders"] = False
    payload["execution_enabled"] = False
    return payload


@router.post("/treasury/pools", response_model=PaperTreasurySnapshot)
def adjust_paper_treasury_pools(
    request: PaperTreasuryAdjustRequest,
    ledger: SqlitePaperLedger = Depends(get_paper_ledger),
    liquidity: SqlitePaperLiquidityRepository = Depends(get_paper_liquidity_repository),
    fx: FxRateService = Depends(get_fx_rate_service),
) -> PaperTreasurySnapshot:
    try:
        snapshot = ledger.treasury.set_available_amounts(
            {item.venue: item.available for item in request.pools},
            reason=request.reason,
        )
    except PaperTreasuryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    liquidity.sync_from_treasury_pools(snapshot.pools)
    rates, source = _backend_fx_for_pools(fx)
    liquidity.get(gbp_per_unit=rates, fx_source=source)
    return snapshot


@router.post("/treasury/reset", response_model=PaperTreasurySnapshot)
def reset_paper_treasury(
    request: PaperTreasuryResetRequest,
    ledger: SqlitePaperLedger = Depends(get_paper_ledger),
    fx: FxRateService = Depends(get_fx_rate_service),
) -> PaperTreasurySnapshot:
    settings = get_settings()
    seed_gbp = request.seed_gbp or Decimal(str(settings.paper_treasury_seed_gbp))
    rate = request.usd_gbp_per_unit
    source = request.fx_source
    if rate is None or source is None:
        rates, fx_source = _backend_fx_for_pools(fx)
        usd_rate = rates.get("USD")
        if rate is None:
            rate = usd_rate or Decimal(str(settings.paper_treasury_demo_usd_gbp_per_unit))
        if source is None:
            source = fx_source or settings.paper_treasury_demo_fx_source
    include = (
        settings.paper_treasury_include_kalshi
        if request.include_kalshi is None
        else request.include_kalshi
    )
    try:
        return ledger.treasury.reset_demo_session(
            seed_gbp=seed_gbp,
            usd_gbp_per_unit=rate,
            fx_source=source,
            include_kalshi=include,
            reason=request.reason,
        )
    except PaperTreasuryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.get("/demo/walkthrough", response_model=DemoWalkthroughSnapshot)
def demo_walkthrough(
    service: DemoWalkthroughService = Depends(get_demo_walkthrough_service),
) -> DemoWalkthroughSnapshot:
    return service.snapshot()


@router.post("/demo/reset", response_model=DemoWalkthroughSnapshot)
def demo_reset(
    request: DemoResetRequest,
    service: DemoWalkthroughService = Depends(get_demo_walkthrough_service),
) -> DemoWalkthroughSnapshot:
    try:
        return service.reset(request)
    except PaperOperationsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/demo/fixture-replay", response_model=FixtureReplayResult)
def demo_fixture_replay(
    request: FixtureReplayRequest,
    service: DemoWalkthroughService = Depends(get_demo_walkthrough_service),
) -> FixtureReplayResult:
    try:
        return service.replay(request)
    except PaperOperationsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/demo/trades/{trade_id}/close", response_model=FixtureReplayResult)
def demo_close_open_trade(
    trade_id: str,
    request: DemoCloseRequest,
    service: DemoWalkthroughService = Depends(get_demo_walkthrough_service),
) -> FixtureReplayResult:
    try:
        return service.close_open_trade(trade_id, request)
    except PaperOperationsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post("/treasury/fx-snapshot", response_model=PaperTreasurySnapshot)
def paper_treasury_fx_snapshot(
    request: PaperTreasuryFxRequest,
    ledger: SqlitePaperLedger = Depends(get_paper_ledger),
) -> PaperTreasurySnapshot:
    try:
        return ledger.treasury.apply_fx_snapshot(
            usd_gbp_per_unit=request.usd_gbp_per_unit,
            fx_source=request.fx_source,
            fx_as_of=request.fx_as_of,
        )
    except PaperTreasuryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


@router.post("/treasury/locks", response_model=PaperTreasurySnapshot)
def paper_treasury_locks(
    requests: list[TreasuryLockRequest],
    ledger: SqlitePaperLedger = Depends(get_paper_ledger),
) -> PaperTreasurySnapshot:
    try:
        ledger.treasury.lock_capital(requests)
    except PaperTreasuryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ledger.treasury.snapshot()


@router.post("/treasury/unwind", response_model=PaperTreasurySnapshot)
def paper_treasury_unwind(
    result: ValidatedUnwindResult,
    ledger: SqlitePaperLedger = Depends(get_paper_ledger),
) -> PaperTreasurySnapshot:
    """8D supplies a completed close. conditionally_releasable metadata does not post."""

    try:
        ledger.treasury.post_unwind(result)
    except PaperTreasuryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return ledger.treasury.snapshot()


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
    ledger: SqlitePaperLedger = Depends(get_paper_ledger),
) -> PaperLiquiditySnapshot:
    try:
        ledger.treasury.assert_mutation_safe()
    except PaperTreasuryError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
                    "check_status": rate.status.value,
                    "carried_forward": rate.status.value == "carried_forward",
                    "primary_source": rate.primary_source,
                    "variance_bps": None if rate.variance_bps is None else str(rate.variance_bps),
                    "check_source": rate.check_source,
                    "check_gbp_per_unit": (
                        None if rate.check_gbp_per_unit is None else str(rate.check_gbp_per_unit)
                    ),
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
    schedule = get_accounting_schedule()
    return EconomicsStatus(
        as_of=as_of,
        fx=fx_rows,
        venue_costs=venue_rows,
        matchbook_fee=costs.matchbook_account_status(as_of=as_of),
        polymarket_fee_policy={
            "resolution": "per_market_clob_metadata",
            "catalog_seeded": False,
            "detail": (
                "Polymarket fees resolve from each market's feesEnabled/feeSchedule "
                "(legacy Gamma feeRate when applicability is present). "
                "Fee-disabled markets are known zero. Missing applicability or "
                "parameters fail closed. The venue-cost registry does not seed a global zero."
            ),
        },
        issues=issues,
        fx_schedule=schedule.operator_status(as_of=as_of),
    )


@router.get("/matchbook-fee", response_model=MatchbookAccountFeeStatus)
def get_matchbook_fee(
    costs: VenueCostResolver = Depends(get_venue_cost_resolver),
) -> MatchbookAccountFeeStatus:
    return costs.matchbook_account_status(as_of=datetime.now(UTC))


@router.put("/matchbook-fee", response_model=MatchbookAccountFeeStatus)
def put_matchbook_fee(
    update: MatchbookFeeUpdate,
    store: SqliteMatchbookAccountFeeStore = Depends(get_matchbook_account_fee_store),
    costs: VenueCostResolver = Depends(get_venue_cost_resolver),
) -> MatchbookAccountFeeStatus:
    store.set_override(update.commission_rate)
    return costs.matchbook_account_status(as_of=datetime.now(UTC))


@router.post("/matchbook-fee/reset", response_model=MatchbookAccountFeeStatus)
def reset_matchbook_fee(
    store: SqliteMatchbookAccountFeeStore = Depends(get_matchbook_account_fee_store),
    costs: VenueCostResolver = Depends(get_venue_cost_resolver),
) -> MatchbookAccountFeeStatus:
    store.clear_override()
    return costs.matchbook_account_status(as_of=datetime.now(UTC))


def _cycles_from(repository: Any, limit: int = 100) -> list[PaperScanCycleRecord]:
    """Latest completed cycles. Missing/unreadable store stays empty, never fabricated."""

    list_cycles = getattr(repository, "list_cycles", None)
    if not callable(list_cycles):
        return []
    try:
        return list_cycles(limit=limit)
    except Exception:
        return []


def _status_with_scan_cycles(
    status: LiveRefreshStatus,
    repository: Any,
) -> LiveRefreshStatus:
    return status.model_copy(update={"recent_scan_cycles": _cycles_from(repository)})


@router.get("/live-refresh", response_model=LiveRefreshStatus)
def live_refresh_status(
    repository: SqlitePaperScanRepository = Depends(get_paper_audit_repository),
) -> LiveRefreshStatus:
    coordinator = get_live_refresh_coordinator()
    coordinator.configure_from_settings()
    return _status_with_scan_cycles(coordinator.public_status(), repository)


@router.get("/venue-participation", response_model=LaneVenueParticipation)
def get_venue_participation() -> LaneVenueParticipation:
    coordinator = get_live_refresh_coordinator()
    coordinator.configure_from_settings()
    participation = coordinator.status.venue_participation
    if participation is None:
        coordinator.configure_from_settings()
        participation = coordinator.status.venue_participation
    assert participation is not None
    return participation


@router.put("/venue-participation", response_model=LiveRefreshStatus)
def put_venue_participation(
    update: LaneVenueParticipationUpdate,
    repository: SqlitePaperScanRepository = Depends(get_paper_audit_repository),
) -> LiveRefreshStatus:
    """Persist operator lane venue toggles. Applies at the next safe cycle boundary."""

    coordinator = get_live_refresh_coordinator()
    coordinator.apply_venue_participation(update.hot.as_venues(), update.universe.as_venues())
    return _status_with_scan_cycles(coordinator.public_status(), repository)


@router.post(
    "/collect/hot",
    response_model=CollectionReport,
    status_code=status.HTTP_200_OK,
)
async def refresh_hot_read_only_market_data(
    request: PaperCollectionRequest,
    background_tasks: BackgroundTasks,
    service: PaperScanService = Depends(get_paper_scan_service),
    audit: SqlitePaperScanRepository = Depends(get_paper_audit_repository),
    watchlist: WatchlistService = Depends(get_watchlist_service),
) -> CollectionReport:
    """Refresh the current HOT identity scope with the scheduled Fast Scan contract.

    This primary operator action reuses current known source events, HOT venue
    participation, and the HOT timeout envelope. It never falls through to
    universe discovery. Persistence/auto-capture remains after the HTTP response
    and retains all normal paper qualification gates.
    """

    kwargs = request.model_dump()
    coordinator = get_live_refresh_coordinator()
    coordinator.remember_request(kwargs)
    plan = coordinator.manual_hot_plan()

    async def runner() -> CollectionReport:
        return await _collect_report(
            kwargs,
            service=service,
            scan_lane=plan.lane,
            identity_scope=plan.identity_scope,
            known_source_events=plan.known_source_events,
            cycle_timeout_seconds=plan.collector_timeout_seconds,
            enabled_venues=plan.enabled_venues,
        )

    try:
        report = await coordinator.run_manual_hot(
            runner,
            timeout_seconds=plan.coordinator_timeout_seconds,
        )
    except ExplicitCollectBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ScanCycleTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except (MatchbookAuthError, MatchbookDiscoveryError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"venue market-data request failed: {exc}"
        ) from exc

    diagnostics = dict(report.scan_diagnostics or {})
    diagnostics["collection_kind"] = "manual_hot_refresh"
    diagnostics["scheduled_fast_full_unchanged"] = True
    report.scan_diagnostics = diagnostics
    background_tasks.add_task(
        persist_manual_hot_after_http_response,
        coordinator,
        report,
        service=service,
        audit=audit,
        watchlist=watchlist,
    )
    return report.model_copy(update={"fixture_markets": {}})


@router.post(
    "/collect",
    response_model=CollectionReport,
    status_code=status.HTTP_200_OK,
)
async def collect_read_only_market_data(
    request: PaperCollectionRequest,
    background_tasks: BackgroundTasks,
    service: PaperScanService = Depends(get_paper_scan_service),
    audit: SqlitePaperScanRepository = Depends(get_paper_audit_repository),
    watchlist: WatchlistService = Depends(get_watchlist_service),
) -> CollectionReport:
    """Run one bounded full-universe diagnostic and persist after the HTTP body.

    This is not Fast Scan and not a Full Sweep generation chunk. Scheduled lanes
    keep their own 25s HOT / chunked UNIVERSE budgets. Persistence/auto-capture
    is scheduled after Starlette sends the HTTP body so a slow persist cannot
    hold the browser past PAPER_COLLECTION_TIMEOUT_MS. Persistence remains
    outside the bounded scan envelope: persist failure is live-refresh/persist
    diagnostics, never scan_cycle_timeout. The HTTP body omits fixture_markets;
    drill-down reads current-state inventory instead of this diagnostic payload.
    """

    kwargs = request.model_dump()
    coordinator = get_live_refresh_coordinator()
    coordinator.remember_request(kwargs)
    settings = get_settings()
    diagnostic_timeout = float(settings.paper_scan_manual_diagnostic_timeout_seconds)

    async def runner() -> CollectionReport:
        return await _collect_report(
            kwargs,
            service=service,
            enabled_venues=list(coordinator.running_cycle_venues()),
            cycle_timeout_seconds=diagnostic_timeout,
        )

    try:
        report = await coordinator.run_explicit_collect(runner)
    except ExplicitCollectBusy as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ScanCycleTimeout as exc:
        raise HTTPException(status_code=504, detail=str(exc)) from exc
    except (MatchbookAuthError, MatchbookDiscoveryError) as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    except httpx.HTTPError as exc:
        raise HTTPException(
            status_code=502, detail=f"venue market-data request failed: {exc}"
        ) from exc

    diagnostics = dict(report.scan_diagnostics or {})
    diagnostics["collection_kind"] = "manual_diagnostic"
    diagnostics["scheduled_fast_full_unchanged"] = True
    report.scan_diagnostics = diagnostics
    background_tasks.add_task(
        persist_explicit_collect_after_http_response,
        coordinator,
        report,
        service=service,
        audit=audit,
        watchlist=watchlist,
    )
    return report.model_copy(update={"fixture_markets": {}})


async def _collect_report(
    kwargs: dict[str, Any],
    *,
    service: PaperScanService,
    scan_lane: str | None = None,
    identity_scope: list[str] | None = None,
    resume_cursor: str | None = None,
    skip_event_ids: list[str] | None = None,
    universe_generation_id: int | None = None,
    generation_resume: bool = False,
    known_source_events: dict[str, list[dict[str, Any]]] | None = None,
    cycle_timeout_seconds: float | None = None,
    enabled_venues: list[VenueName] | None = None,
    reuse_discovery: bool = False,
    discovery_snapshot: dict[str, list[dict[str, Any]]] | None = None,
    unbounded_cycle: bool = False,
    sweep_id: str | None = None,
    on_discovery_complete=None,
    on_fixture_evaluated=None,
    on_canonical_work_set=None,
    retry_series: dict[str, list[str]] | None = None,
) -> CollectionReport:
    settings = get_settings()
    runtime = get_shared_provider_runtime(settings)
    matchbook = runtime.matchbook
    polymarket = runtime.polymarket
    kalshi = runtime.kalshi
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        paper_scan=service,
        event_matcher=service.market_matcher.event_matcher,
        market_matcher=service.market_matcher,
        venue_timeout_seconds=settings.paper_scan_venue_timeout_seconds,
        provider_call_timeout_seconds=settings.paper_scan_provider_timeout_seconds,
        cluster_concurrency=settings.paper_scan_cluster_concurrency,
        provider_concurrency={
            VenueName.MATCHBOOK: settings.paper_scan_matchbook_concurrency,
            VenueName.POLYMARKET: settings.paper_scan_polymarket_concurrency,
            VenueName.KALSHI: settings.paper_scan_kalshi_concurrency,
        },
        provider_access=runtime.access,
        cycle_timeout_seconds=(
            settings.paper_scan_cycle_timeout_seconds
            if cycle_timeout_seconds is None
            else float(cycle_timeout_seconds)
        ),
    )
    try:
        return await collector.collect_and_scan(
            **kwargs,
            polymarket_queried_series_ids=settings.resolved_polymarket_series_ids(),
            config_warnings=settings.polymarket_series_config_warnings(),
            scan_lane=scan_lane,
            identity_scope=identity_scope,
            resume_cursor=resume_cursor,
            skip_event_ids=skip_event_ids,
            universe_generation_id=universe_generation_id,
            generation_resume=generation_resume,
            known_source_events=known_source_events,
            cycle_timeout_seconds=cycle_timeout_seconds,
            enabled_venues=enabled_venues,
            reuse_discovery=reuse_discovery,
            discovery_snapshot=discovery_snapshot,
            unbounded_cycle=unbounded_cycle,
            sweep_id=sweep_id,
            on_discovery_complete=on_discovery_complete,
            on_fixture_evaluated=on_fixture_evaluated,
            on_canonical_work_set=on_canonical_work_set,
            retry_series=retry_series,
        )
    finally:
        acknowledge_task_cancellation()
        # SharedProviderRuntime owns Matchbook/Polymarket/Kalshi sessions across
        # concurrent HOT and UNIVERSE workers. Do not close them per collect.


def _run_paper_position_management(
    report: CollectionReport,
    *,
    operations: PaperOperationsService,
    manager=None,
    policy: UnwindPolicy | None = None,
    now=None,
    quotes_by_trade=None,
):
    """Evaluate OPEN paper trades against the just-scanned reverse books.

    Failures here must not roll back scan persist or invent a settlement.
    Automatic close uses two-scan confirmation: cycle N pends UNWIND_ELIGIBLE,
    cycle N+1 may call complete_validated_unwind() only on strictly newer
    exact-ID books. Same-scan catalog facts never mutate treasury.
    """

    try:
        from sports_hedge.paper.position_management.scarcity import competing_from_paper_decisions

        resolved = manager if manager is not None else get_paper_position_manager()
        resolved.operations = operations
        exclude = {
            trade.opportunity_id
            for trade in operations.list_active_trades()
        }
        competing = competing_from_paper_decisions(
            report.paper_decisions,
            exclude_opportunity_ids=exclude,
        )
        return resolved.manage_open_positions(
            competing=competing,
            policy=policy,
            now=now,
            quotes_by_trade=quotes_by_trade,
        )
    except Exception:
        LOGGER.exception("paper position management cycle failed")
        return []


def _persist_collection_report(
    report: CollectionReport,
    *,
    service: PaperScanService,
    audit: SqlitePaperScanRepository,
    watchlist: WatchlistService,
    scan_lane: ScanLane | str | None = None,
) -> None:
    audit.append_cycle(
        build_paper_scan_cycle_record(report, scan_lane=scan_lane or report.scan_lane)
    )
    operations = get_paper_operations_service(watchlist, get_priority_alert_service())
    for decision in report.paper_decisions:
        _persist_decision(
            decision,
            service=service,
            audit=audit,
            watchlist=watchlist,
            operations=operations,
            refreshed_venues=report.enabled_venues,
        )
    _run_paper_position_management(report, operations=operations)


def _annotate_collection_persist(
    report: CollectionReport,
    *,
    ok: bool,
    error: str | None,
    duration_ms: int,
) -> None:
    """Attach persist outcome to the scan report without rewriting scan truth."""

    diagnostics = dict(report.scan_diagnostics or {})
    stages = dict(diagnostics.get("stages") or {})
    stages["persistence"] = {
        "calls": 1,
        "elapsed_ms": max(0, int(duration_ms)),
        "timeouts": 0,
        "cancels": 0,
        "ok": ok,
        "error": error,
    }
    diagnostics["stages"] = stages
    diagnostics["persist_ok"] = ok
    if ok:
        diagnostics.pop("persist_error", None)
    else:
        diagnostics["persist_error"] = error
    report.scan_diagnostics = diagnostics


async def persist_explicit_collect_after_http_response(
    coordinator,
    report: CollectionReport,
    *,
    service: PaperScanService,
    audit: SqlitePaperScanRepository,
    watchlist: WatchlistService,
) -> None:
    """Starlette BackgroundTask: runs after the collect HTTP body is sent.

    Uses the same event-loop-safe persist wrapper as scheduled Fast/Full ticks.
    Failures are recorded on live-refresh persist diagnostics.
    """

    await persist_scheduled_collection_report(
        coordinator,
        report,
        service=service,
        audit=audit,
        watchlist=watchlist,
        scan_lane=ScanLane.UNIVERSE,
    )


async def persist_manual_hot_after_http_response(
    coordinator,
    report: CollectionReport,
    *,
    service: PaperScanService,
    audit: SqlitePaperScanRepository,
    watchlist: WatchlistService,
) -> None:
    """Persist a manual Fast Scan result with HOT provenance after response."""

    await persist_scheduled_collection_report(
        coordinator,
        report,
        service=service,
        audit=audit,
        watchlist=watchlist,
        scan_lane=ScanLane.HOT,
    )


async def persist_scheduled_collection_report(
    coordinator,
    report: CollectionReport,
    *,
    service: PaperScanService,
    audit: SqlitePaperScanRepository,
    watchlist: WatchlistService,
    scan_lane: ScanLane,
) -> None:
    """Persist after the scan envelope without blocking FastAPI's event loop.

    Only synchronous SQLite / auto-capture / position-management work runs in a
    worker thread. Coordinator persist status and report diagnostic mutation stay
    on the event loop after that worker returns. Failures are not
    scan_cycle_timeout.
    """

    started = monotonic()
    try:
        await asyncio.to_thread(
            _persist_collection_report,
            report,
            service=service,
            audit=audit,
            watchlist=watchlist,
            scan_lane=scan_lane,
        )
    except Exception as exc:
        LOGGER.exception("persist/auto-capture failed lane=%s", scan_lane)
        duration_ms = int((monotonic() - started) * 1000)
        coordinator.record_persist_outcome(
            ok=False,
            error=str(exc),
            duration_ms=duration_ms,
            scan_lane=scan_lane,
        )
        _annotate_collection_persist(
            report, ok=False, error=str(exc), duration_ms=duration_ms
        )
        return
    duration_ms = int((monotonic() - started) * 1000)
    coordinator.record_persist_outcome(
        ok=True,
        error=None,
        duration_ms=duration_ms,
        scan_lane=scan_lane,
    )
    _annotate_collection_persist(
        report, ok=True, error=None, duration_ms=duration_ms
    )


async def _execute_collection(
    kwargs: dict[str, Any],
    *,
    service: PaperScanService,
    audit: SqlitePaperScanRepository,
    watchlist: WatchlistService,
    scan_lane: str | None = None,
    identity_scope: list[str] | None = None,
    resume_cursor: str | None = None,
    skip_event_ids: list[str] | None = None,
    universe_generation_id: int | None = None,
    generation_resume: bool = False,
    known_source_events: dict[str, list[dict[str, Any]]] | None = None,
    cycle_timeout_seconds: float | None = None,
    enabled_venues: list[VenueName] | None = None,
) -> CollectionReport:
    return await _collect_report(
        kwargs,
        service=service,
        scan_lane=scan_lane,
        identity_scope=identity_scope,
        resume_cursor=resume_cursor,
        skip_event_ids=skip_event_ids,
        universe_generation_id=universe_generation_id,
        generation_resume=generation_resume,
        known_source_events=known_source_events,
        cycle_timeout_seconds=cycle_timeout_seconds,
        enabled_venues=enabled_venues,
    )


def scheduled_collection_kwargs() -> dict[str, Any]:
    """Stable server-owned collect settings. Manual request kwargs never apply."""

    settings = get_settings()
    return PaperCollectionRequest(
        minimum_net_edge=Decimal(str(settings.min_net_edge)),
        maximum_execution_risk=int(settings.max_execution_risk),
        minimum_mapping_confidence=float(settings.min_mapping_confidence),
        assumed_latency_ms=int(settings.simulated_latency_ms),
    ).model_dump()


async def server_owned_refresh_tick(plan=None) -> None:
    """Background tick used when PAPER_LIVE_REFRESH_ENABLED is true."""

    coordinator = get_live_refresh_coordinator()
    resolved = plan if plan is not None and getattr(plan, "lane", "idle") != "idle" else coordinator.plan_tick()
    if resolved.lane == "idle":
        return
    service = get_paper_scan_service(
        get_market_intelligence_service(),
        get_fx_rate_service(),
        get_venue_cost_resolver(),
        get_paper_liquidity_repository(),
    )
    audit = get_paper_audit_repository()
    from sports_hedge.api.watchlist import get_watchlist_repository, get_watchlist_service

    watchlist = get_watchlist_service(get_watchlist_repository())
    kwargs = scheduled_collection_kwargs()
    on_discovery = on_fixture = on_work_set = None
    if resolved.lane == ScanLane.UNIVERSE.value:
        on_discovery, on_fixture, on_work_set = coordinator.universe_collect_callbacks()

    async def runner() -> CollectionReport:
        return await _collect_report(
            kwargs,
            service=service,
            scan_lane=resolved.lane,
            identity_scope=resolved.identity_scope,
            resume_cursor=resolved.resume_cursor,
            skip_event_ids=resolved.skip_event_ids,
            universe_generation_id=resolved.universe_generation_id,
            generation_resume=resolved.generation_resume,
            known_source_events=resolved.known_source_events,
            cycle_timeout_seconds=resolved.collector_timeout_seconds,
            enabled_venues=list(resolved.enabled_venues),
            reuse_discovery=resolved.reuse_discovery,
            discovery_snapshot=resolved.discovery_snapshot,
            unbounded_cycle=resolved.unbounded_cycle,
            sweep_id=resolved.sweep_id,
            on_discovery_complete=on_discovery,
            on_fixture_evaluated=on_fixture,
            on_canonical_work_set=on_work_set,
            retry_series=resolved.retry_series,
        )

    try:
        report = await coordinator.run_cycle(
            runner,
            timeout_seconds=resolved.coordinator_timeout_seconds,
            scan_lane=ScanLane(resolved.lane),
        )
    except (MatchbookAuthError, MatchbookDiscoveryError, ScanCycleTimeout, httpx.HTTPError):
        return
    await persist_scheduled_collection_report(
        coordinator,
        report,
        service=service,
        audit=audit,
        watchlist=watchlist,
        scan_lane=ScanLane(resolved.lane),
    )


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


@router.get("/trades/position-management", response_model=list[PositionManagementSnapshot])
def paper_position_management_states(
    operations: PaperOperationsService = Depends(get_paper_operations_service),
) -> list[PositionManagementSnapshot]:
    """Reusable #168 seam: latest HOLD / UNWIND ELIGIBLE / NOT SAFE per active trade."""

    return [
        trade.position_management
        for trade in operations.list_active_trades()
        if trade.position_management is not None
    ]


@router.get(
    "/trades/{trade_id}/position-management",
    response_model=PositionManagementSnapshot,
)
def paper_trade_position_management(
    trade_id: str,
    operations: PaperOperationsService = Depends(get_paper_operations_service),
) -> PositionManagementSnapshot:
    try:
        detail = operations.trade_detail(trade_id)
    except PaperOperationsError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if detail.position_management is None:
        raise HTTPException(status_code=404, detail="position_management_unavailable")
    return detail.position_management


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


@router.post("/trades/{trade_id}/unwind", response_model=PaperTradeDetail)
def complete_paper_unwind(
    trade_id: str,
    request: PaperClosePlanRequest,
    operations: PaperOperationsService = Depends(get_paper_operations_service),
) -> PaperTradeDetail:
    """PAPER-ONLY validated unwind. Posts 8E release only after a complete close plan."""

    if request.places_orders:
        raise HTTPException(status_code=422, detail="unwind cannot place orders")
    try:
        return operations.complete_validated_unwind(
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
    "/recommend-deployment",
    response_model=RecommendedPaperDeployment,
    status_code=status.HTTP_200_OK,
)
def recommend_paper_deployment(
    request: RecommendPaperDeploymentRequest,
    operations: PaperOperationsService = Depends(get_paper_operations_service),
) -> RecommendedPaperDeployment:
    """PAPER-ONLY recommended size from the existing allocator. Never locks or OPENs."""

    try:
        return operations.recommend_paper_deployment(request.opportunity_id)
    except PaperOperationsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


@router.post(
    "/prepare-deployment",
    response_model=PreparedPaperDeployment,
    status_code=status.HTTP_200_OK,
)
def prepare_paper_deployment(
    request: PreparePaperDeploymentRequest,
    operations: PaperOperationsService = Depends(get_paper_operations_service),
) -> PreparedPaperDeployment:
    """PAPER-ONLY fixed-size preview. Never locks treasury, OPENs, or places a venue order."""

    try:
        return operations.prepare_fixed_deployment(
            request.opportunity_id,
            request.requested_size_gbp,
            operator_note=request.operator_note,
        )
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
            simulate_external=request.simulate_external,
            prepared_deployment_id=request.prepared_deployment_id,
            requested_size_gbp=request.requested_size_gbp,
        )
    except PaperOperationsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc


async def _aclose_soon(*clients: Any, timeout: float = 0.5) -> None:
    """Drop HTTP clients without letting aclose hang past scan finalisation."""

    tasks: list[asyncio.Task[Any]] = []
    for client in clients:
        closer = getattr(client, "aclose", None)
        if closer is None:
            continue
        tasks.append(asyncio.create_task(closer()))
    if not tasks:
        return
    done, pending = await asyncio.wait(set(tasks), timeout=timeout)
    for task in pending:
        task.cancel()
    for task in done:
        try:
            task.result()
        except Exception as exc:
            LOGGER.warning("venue_client_close_failed error=%s", exc)


def _persist_decision(
    decision: PaperScanDecision,
    *,
    service: PaperScanService,
    audit: SqlitePaperScanRepository,
    watchlist: WatchlistService,
    operations: PaperOperationsService | None = None,
    quote_age_ms: int | None = None,
    refreshed_venues: list[VenueName] | tuple[VenueName, ...] | None = None,
) -> None:
    if not decision.canonical_market_id:
        return
    if not decision.paper_audit_record_id:
        decision.paper_audit_record_id = str(uuid4())
    history = service.market_intelligence.market_history(
        canonical_market_id=decision.canonical_market_id,
    )
    if history:
        audit.append_scan(build_paper_scan_record(decision, history))
    watchlist.observe_paper_decision(decision, history, quote_age_ms=quote_age_ms)
    if operations is not None:
        operations.persist_triggered_chain(decision, refreshed_venues=refreshed_venues)


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
    if request.venue == VenueName.KALSHI:
        payloads = request.market_payload.get("grouped_payloads")
        if not isinstance(payloads, list) or not payloads:
            payloads = [request.market_payload]
        return KalshiObservationBuilder().build(
            request.event_payload,
            payloads,
            request.books_by_token,
            series=request.market_payload.get("series")
            if isinstance(request.market_payload.get("series"), dict)
            else None,
            observed_at=request.observed_at,
            source_latency_ms=request.source_latency_ms,
            quote_age_ms=request.quote_age_ms,
            fee_snapshot=request.market_payload.get("kalshi_fee")
            if isinstance(request.market_payload.get("kalshi_fee"), dict)
            else None,
        )
    raise ValueError(f"Paper scan does not yet support venue: {request.venue.value}")


def _backend_fx_for_pools(fx: FxRateService) -> tuple[dict[str, Decimal], str | None]:
    settings = get_settings()
    rates: dict[str, Decimal] = {"GBP": Decimal(1)}
    source: str | None = None
    try:
        snapshots = fx.paper_snapshots({"USD", "GBP"}, as_of=datetime.now(UTC))
        for snapshot in snapshots:
            rates[snapshot.currency] = snapshot.gbp_per_unit
            if snapshot.currency == "USD":
                source = snapshot.source
    except FxRateUnavailable:
        snapshots = []
    if "USD" not in rates or rates.get("USD") in {None, Decimal(0)}:
        try:
            treasury = get_paper_ledger().treasury.snapshot()
        except Exception:
            treasury = None
        if treasury is not None and treasury.session is not None:
            rates["USD"] = treasury.session.fx_rate_usd_gbp
            source = treasury.session.fx_source
        else:
            rates["USD"] = Decimal(str(settings.paper_treasury_demo_usd_gbp_per_unit))
            source = settings.paper_treasury_demo_fx_source
    return rates, source
