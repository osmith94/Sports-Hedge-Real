"""Process-memory price engine over ACTIVE catalogue rows (Issue #344 / #346).

UNIVERSE catalogues. This engine prices every ACTIVE supported row. HOT is a
priority tier inside the engine, not exclusive membership.

In-flight leases and (2, 5, 10)s retry live in process memory. Restart rebuilds
from ACTIVE catalogue rows and resets short backoff. There is no durable
pricing-work queue.

Phase 4 publishes economics at item completion and hands the decision to the
injected ``on_item_decision`` callback (wired by the paper API to the existing
paper capture chain). Capture is started immediately, then waited on outside
the provider-pricing worker and HOT ``run_cycle`` envelope. This module does
not own capture. Quote age uses each exact provider response's retrieval
instant; a multi-constituent item is as old as its oldest required quote.

PAPER / read-only. Exact persisted Matchbook/Kalshi/Polymarket IDs only — never
``list_events`` / ``list_markets`` rediscovery on this path. Polymarket CLOB
books use real token IDs from the catalogue; missing or synthetic tokens are
not executable.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from inspect import isawaitable
from logging import getLogger
from time import monotonic
from typing import Any

from sports_hedge.application.approved_market_catalogue import (
    ApprovedMarketCatalogueRow,
    DerivedPriceEngineItem,
    derived_price_engine_working_set,
    executable_polymarket_token_ids,
    required_outcomes_for_key,
)
from sports_hedge.application.target_competitions import resolve_catalogue_competition_code
from sports_hedge.application.collector import (
    CollectionReport,
    CollectorIssue,
    DiscoveredFixture,
    MarketEvaluationState,
    _inventory_from_observation,
)
from sports_hedge.application.executable_liquidity import (
    decision_is_solver_arbitrage,
    decision_net_edge,
)
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.fixture_inventory import (
    FixtureMarketInventoryRow,
    InventoryComparisonStatus,
    VenueMarketFacts,
    VenueQuoteFact,
    assemble_fixture_inventory,
    inventory_is_comparable_opportunity,
)
from sports_hedge.application.hot_market_relationships import (
    HOT_REVALIDATION_NEEDED_REASON,
    extract_matchbook_market_payload,
    matchbook_payload_is_terminal,
)
from sports_hedge.application.market_observation import (
    KalshiObservationBuilder,
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
    VenueMarketObservation,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.opportunity_viability import (
    CROSS_VENUE_UNAVAILABLE,
    NO_CROSS_VENUE_CANDIDATE,
    UPPER_BOUND_BELOW_MIN_NET,
    VenueViability,
    assess_identity_viability,
    get_opportunity_viability_cache,
    reset_opportunity_viability_cache,
)
from sports_hedge.application.provider_access import (
    HEALTH_CAPACITY_SATURATED,
    HEALTH_DEFERRED,
    HEALTH_MARKET_TIMEOUT,
    HEALTH_OK,
    PRICE_ENGINE_ACTIVE_TRADE_LANE,
    PRICE_ENGINE_BACKGROUND_LANE,
    ProviderAccessLayer,
    ProviderLease,
    get_shared_provider_access,
)
from sports_hedge.arbitrage.arb_upper_bound import (
    implied_from_kalshi_book,
    implied_from_matchbook_market,
    implied_from_observation,
    merge_known_implied,
    optimistic_net_edge_upper_bound,
)
from sports_hedge.application.scanner_observability import (
    PRICE_ENGINE_EVALUATED_DEFINITION,
    PriceEnginePublicStatus,
    PriceEngineTierStatus,
    ScannerObservabilitySink,
    record_operation_health,
    venue_health_from_operation_health,
)
from sports_hedge.application.quote_freshness import (
    QuoteAgeAssessment,
    matchbook_market_quote_age,
    retrieval_quote_age,
)
from sports_hedge.application.scan_lanes import (
    DEFAULT_BACKGROUND_INTERVAL_SECONDS,
    DEFAULT_HOT_INTERVAL_SECONDS,
    ScanLane,
    classify_scan_lane,
)
from sports_hedge.arbitrage.watchlist.economics import (
    distance_to_trigger_pp,
    is_net_proximity_hot,
    qualifies_min_net_arb,
)
from sports_hedge.arbitrage.min_net_threshold import catalogue_market_scope
from sports_hedge.arbitrage.watchlist.models import hot_promotion_opportunity_id
from sports_hedge.config import Settings, get_settings
from sports_hedge.persistence.operator_scanner_settings import (
    effective_operator_scanner_settings,
)
from sports_hedge.domain.football import (
    CanonicalEvent,
    CanonicalMarket,
    CanonicalOutcome,
    CanonicalRunner,
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import VenueCostSnapshot
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore
from sports_hedge.venues.matchbook import MatchbookMarketGoneError

PRICE_ENGINE_RETRY_BACKOFF_SECONDS = (2.0, 5.0, 10.0)
PRICE_ENGINE_ITEM_TIMEOUT_REASON = "order_book_timeout after 8s"
CATALOGUE_REVALIDATION_REASON = HOT_REVALIDATION_NEEDED_REASON
NOT_STARTED_THIS_CADENCE = "not_started_this_cadence"
PROVIDER_CAPACITY_SATURATED = HEALTH_CAPACITY_SATURATED
DEFERRED_STATUS = HEALTH_DEFERRED
SCAN_BUDGET_EXHAUSTED_REASON = "scan_budget_exhausted"
PRICE_ENGINE_PERSIST_STAGE = "persist_capture"

LOGGER = getLogger(__name__)


class PriceEnginePriority(StrEnum):
    HOT = "hot"
    BACKGROUND = "background"


class PriceEngineItemStatus(StrEnum):
    DUE = "due"
    IN_FLIGHT = "in_flight"
    EVALUATED = "evaluated"
    RETRY_WAIT = "retry_wait"
    DEFERRED = "deferred"
    NOT_STARTED = "not_started_this_cadence"
    REVALIDATION_NEEDED = "revalidation_needed"
    SKIPPED = "skipped_not_viable"
    FAILED = "failed"


@dataclass(frozen=True)
class RetrievedVenuePayload:
    """One exact provider response plus the instant that response completed."""

    payload: dict[str, Any]
    retrieved_at: datetime


@dataclass
class PriceEngineRuntimeItem:
    """Process-memory scheduler view of one derived catalogue item."""

    identity: DerivedPriceEngineItem
    priority: PriceEnginePriority = PriceEnginePriority.BACKGROUND
    status: PriceEngineItemStatus = PriceEngineItemStatus.DUE
    in_flight: bool = False
    next_retry_at: datetime | None = None
    retry_attempt: int = 0
    last_error_stage: str | None = None
    last_error_detail: str | None = None
    last_persist_error: str | None = None
    last_priced_at: datetime | None = None
    list_events_calls: int = 0
    list_markets_calls: int = 0
    pricing_slice_priority: PriceEnginePriority | None = None

    @property
    def item_key(self) -> str:
        return f"{self.identity.catalogue_row_id}:{self.identity.content_version}"


@dataclass(frozen=True)
class PriceEngineProjectionEvent:
    """Immutable item-event inputs for lagged UI/current-state projection.

    Projection may lag, but must not reread later ``PriceEngineRuntimeItem``
    mutation for catalogue identity, lane/priority, observations, or decision.
    ``reset_generation`` is the FixtureCurrentStateStore epoch at emit time;
    a pre-reset callback must not commit after coordinator/store reset.
    """

    identity: DerivedPriceEngineItem
    priority: PriceEnginePriority
    matchbook_obs: VenueMarketObservation
    kalshi_obs: VenueMarketObservation
    decision: PaperScanDecision | None
    reset_generation: int = 0


@dataclass(frozen=True)
class HotPromotionFact:
    """One scheduler BACKGROUND→HOT episode. Not inferred from watchlist movement."""

    canonical_event_id: str
    catalogue_row_id: str
    content_version: int
    occurred_at: datetime
    opportunity_id: str
    episode: int
    fixture_label: str | None = None
    market_family: str | None = None
    pricing_lane: str = PriceEnginePriority.BACKGROUND.value
    current_net_edge: Decimal | None = None
    distance_to_trigger_pp: Decimal | None = None


@dataclass
class PriceEngineSliceResult:
    evaluated: list[str] = field(default_factory=list)
    failed: list[str] = field(default_factory=list)
    deferred: list[str] = field(default_factory=list)
    not_started: list[str] = field(default_factory=list)
    retry_wait: list[str] = field(default_factory=list)
    revalidation: list[str] = field(default_factory=list)
    decisions: list[PaperScanDecision] = field(default_factory=list)
    issues: list[CollectorIssue] = field(default_factory=list)
    remaining_soft_used: bool = False
    scan_budget_exhausted: bool = False
    provider_capacity_saturated: bool = False
    promotions: list[str] = field(default_factory=list)
    persist_failures: list[str] = field(default_factory=list)
    operation_health: dict[str, Any] = field(default_factory=dict)
    venue_health: dict[str, str] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    skipped_provider_calls: int = 0
    saved_provider_calls: int = 0
    saved_time_ms: int = 0
    skip_reasons: dict[str, str] = field(default_factory=dict)
    upper_bound_net_edge: str | None = None
    viable_venue_count: int | None = None

    def statuses(self) -> dict[str, str]:
        payload: dict[str, str] = {}
        for key in self.evaluated:
            payload[key] = PriceEngineItemStatus.EVALUATED.value
        for key in self.failed:
            payload[key] = PriceEngineItemStatus.FAILED.value
        for key in self.deferred:
            payload[key] = PROVIDER_CAPACITY_SATURATED
        for key in self.not_started:
            payload[key] = NOT_STARTED_THIS_CADENCE
        for key in self.retry_wait:
            payload[key] = PriceEngineItemStatus.RETRY_WAIT.value
        for key in self.revalidation:
            payload[key] = CATALOGUE_REVALIDATION_REASON
        for key in self.skipped:
            payload[key] = self.skip_reasons.get(key) or PriceEngineItemStatus.SKIPPED.value
        return payload

    def viability_diagnostics(self) -> dict[str, Any]:
        reason = None
        if self.skip_reasons:
            reason = next(iter(self.skip_reasons.values()))
        return {
            "skipped_provider_calls": int(self.skipped_provider_calls),
            "saved_provider_calls": int(self.saved_provider_calls),
            "saved_time_ms": int(self.saved_time_ms),
            "skip_reason": reason,
            "upper_bound_net_edge": self.upper_bound_net_edge,
            "viable_venue_count": self.viable_venue_count,
            "skipped_items": len(self.skipped),
        }


def price_engine_retry_backoff_seconds(attempt_count: int) -> float:
    """Capped isolated-item backoff. Attempt count may grow; delay does not."""

    if attempt_count <= 0:
        return PRICE_ENGINE_RETRY_BACKOFF_SECONDS[0]
    index = min(attempt_count - 1, len(PRICE_ENGINE_RETRY_BACKOFF_SECONDS) - 1)
    return float(PRICE_ENGINE_RETRY_BACKOFF_SECONDS[index])


class CataloguePriceEngine:
    """Derive HOT + BACKGROUND price work from ACTIVE catalogue rows."""

    def __init__(
        self,
        *,
        catalogue_store: SqliteApprovedMarketCatalogueStore | None = None,
        matchbook: Any = None,
        kalshi: Any = None,
        polymarket: Any = None,
        paper_scan: PaperScanService | None = None,
        fixture_state: FixtureCurrentStateStore | None = None,
        provider_access: ProviderAccessLayer | None = None,
        clock: Callable[[], datetime] | None = None,
        settings: Settings | None = None,
        venue_costs: list[VenueCostSnapshot] | None = None,
        fx_snapshots: list[FxRateSnapshot] | None = None,
        provider_timeout_seconds: float | None = None,
        hot_interval_seconds: int | None = None,
        background_interval_seconds: int | None = None,
        on_item_decision: Callable[[PaperScanDecision, PriceEngineRuntimeItem], Any] | None = None,
        on_hot_promotion: Callable[[HotPromotionFact], Any] | None = None,
        observability: ScannerObservabilitySink | None = None,
    ) -> None:
        resolved = settings or get_settings()
        self.catalogue_store = catalogue_store
        self.matchbook = matchbook
        self.kalshi = kalshi
        self.polymarket = polymarket
        self.paper_scan = paper_scan
        self.fixture_state = fixture_state
        self.provider_access = provider_access if provider_access is not None else get_shared_provider_access()
        self._clock = clock or (lambda: datetime.now(UTC))
        self._provider_timeout = float(
            resolved.paper_scan_provider_timeout_seconds
            if provider_timeout_seconds is None
            else provider_timeout_seconds
        )
        self._hot_interval = int(
            resolved.paper_live_refresh_hot_interval_seconds
            if hot_interval_seconds is None
            else hot_interval_seconds
        )
        self._background_interval = int(
            resolved.paper_background_price_interval_seconds
            if background_interval_seconds is None
            else background_interval_seconds
        )
        self.venue_costs = list(venue_costs or [])
        self.fx_snapshots = list(fx_snapshots or [])
        self.on_item_decision = on_item_decision
        self.on_hot_promotion = on_hot_promotion
        self.observability = observability if observability is not None else ScannerObservabilitySink()
        self._items: dict[str, PriceEngineRuntimeItem] = {}
        self._promoted_hot_rows: dict[str, int] = {}
        self._promoted_hot_ids: set[str] = set()
        self._hot_promotion_episodes: dict[str, int] = {}
        self._operation_health: dict[str, dict[str, Any]] = {
            PriceEnginePriority.HOT.value: {},
            PriceEnginePriority.BACKGROUND.value: {},
        }
        self.revalidation_requests: list[dict[str, str]] = []
        self.matchbook_builder = MatchbookObservationBuilder()
        self.kalshi_builder = KalshiObservationBuilder()
        self.polymarket_builder = PolymarketObservationBuilder()
        self._peak_held_slots: dict[str, int] = {
            VenueName.MATCHBOOK.value: 0,
            VenueName.KALSHI.value: 0,
            VenueName.POLYMARKET.value: 0,
        }
        self._slice_remaining: Callable[[], float | None] | None = None
        self._pending_item_captures: set[asyncio.Task[Any]] = set()
        self._last_slice_not_started: dict[str, int] = {
            PriceEnginePriority.HOT.value: 0,
            PriceEnginePriority.BACKGROUND.value: 0,
        }
        self._selected_competition_codes: frozenset[str] | None = None
        self._exempt_event_ids: frozenset[str] = frozenset()
        self.viability_cache = get_opportunity_viability_cache()

    def now(self) -> datetime:
        return self._clock()

    def set_background_interval_seconds(self, seconds: int) -> None:
        """Apply the operator/env BACKGROUND cadence without reconstructing work."""

        self._background_interval = int(seconds)

    def set_operator_scope(
        self,
        selected_codes: list[str] | tuple[str, ...] | frozenset[str] | None,
        *,
        exempt_event_ids: list[str] | tuple[str, ...] | frozenset[str] | None = None,
    ) -> None:
        self._selected_competition_codes = (
            None if selected_codes is None else frozenset(str(code) for code in selected_codes)
        )
        self._exempt_event_ids = frozenset(str(item) for item in (exempt_event_ids or ()))

    def items(self) -> list[PriceEngineRuntimeItem]:
        return list(self._items.values())

    def item(self, catalogue_row_id: str) -> PriceEngineRuntimeItem | None:
        for runtime in self._items.values():
            if runtime.identity.catalogue_row_id == catalogue_row_id:
                return runtime
        return None

    def reconstruct(self, rows: list[ApprovedMarketCatalogueRow] | None = None) -> list[PriceEngineRuntimeItem]:
        """Rebuild working set from ACTIVE rows. Backoff is not durable."""

        if rows is None:
            if self.catalogue_store is None:
                rows = []
            else:
                rows = self.catalogue_store.list_active()
        if self._selected_competition_codes is not None:
            scoped: list[ApprovedMarketCatalogueRow] = []
            for row in rows:
                if row.canonical_event_id in self._exempt_event_ids:
                    scoped.append(row)
                    continue
                code = resolve_catalogue_competition_code(
                    competition=row.competition,
                    kalshi_series_ticker=row.kalshi_series_ticker,
                )
                if code is not None and code in self._selected_competition_codes:
                    scoped.append(row)
            rows = scoped
        derived = derived_price_engine_working_set(list(rows))
        live_keys = {f"{item.catalogue_row_id}:{item.content_version}" for item in derived}
        for key in list(self._items):
            if key not in live_keys:
                del self._items[key]
        rebuilt: list[PriceEngineRuntimeItem] = []
        for identity in derived:
            key = f"{identity.catalogue_row_id}:{identity.content_version}"
            existing = self._items.get(key)
            if existing is None:
                runtime = PriceEngineRuntimeItem(identity=identity)
                self._items[key] = runtime
            else:
                runtime = existing
                runtime.identity = identity
            rebuilt.append(runtime)
        self._refresh_promoted_hot_ids()
        for runtime in rebuilt:
            runtime.priority = self.classify_priority(runtime.identity)
        return rebuilt

    def restart(self) -> list[PriceEngineRuntimeItem]:
        """Process restart: reconstruct ACTIVE work and reset short backoff."""

        self._items.clear()
        self._promoted_hot_rows.clear()
        self._promoted_hot_ids.clear()
        self._hot_promotion_episodes.clear()
        self.revalidation_requests.clear()
        reset_opportunity_viability_cache()
        self.viability_cache = get_opportunity_viability_cache()
        self._operation_health = {
            PriceEnginePriority.HOT.value: {},
            PriceEnginePriority.BACKGROUND.value: {},
        }
        self.observability.reset()
        return self.reconstruct()

    def classify_priority(self, identity: DerivedPriceEngineItem) -> PriceEnginePriority:
        """Scheduler HOT vs BACKGROUND from lifecycle + engine-local promotion.

        UI/current-state projection is not scheduler authority. A lagged
        ``FixtureCurrentStateStore`` upsert cannot grant or revoke priority.
        """

        fixture = _fixture_like(identity)
        lifecycle = classify_scan_lane(fixture, self.now())
        if lifecycle is ScanLane.HOT:
            return PriceEnginePriority.HOT
        if lifecycle is ScanLane.DROP:
            return PriceEnginePriority.BACKGROUND
        if identity.canonical_event_id in self._promoted_hot_ids:
            return PriceEnginePriority.HOT
        return PriceEnginePriority.BACKGROUND

    def due_items(
        self,
        priority: PriceEnginePriority,
        *,
        now: datetime | None = None,
    ) -> list[PriceEngineRuntimeItem]:
        evaluated = now or self.now()
        due: list[PriceEngineRuntimeItem] = []
        for runtime in self._items.values():
            runtime.priority = self.classify_priority(runtime.identity)
            if runtime.priority is not priority:
                continue
            if runtime.in_flight:
                continue
            if runtime.next_retry_at is not None and evaluated < runtime.next_retry_at:
                continue
            if runtime.last_priced_at is not None:
                interval = (
                    self._hot_interval
                    if runtime.priority is PriceEnginePriority.HOT
                    else self._background_interval
                )
                if evaluated < runtime.last_priced_at + timedelta(seconds=interval):
                    continue
            runtime.status = PriceEngineItemStatus.DUE
            due.append(runtime)
        return due

    def _slice_worker_limit(self) -> int:
        """Bound in-slice item workers to provider caps without raising them.

        One worker prices one item sequentially (Matchbook then Kalshi), so a
        pool of Matchbook+Kalshi caps can keep both venues busy. It does not
        reserve a hostage set of slots for one item.
        """

        access = self.provider_access
        if access is None:
            return 4
        matchbook = int(access.limits.get(VenueName.MATCHBOOK, 4))
        kalshi = int(access.limits.get(VenueName.KALSHI, 4))
        return max(1, matchbook + kalshi)

    def _remaining_slice_seconds(self) -> float | None:
        if self._slice_remaining is None:
            return None
        return self._slice_remaining()

    def _slot_wait_seconds(self) -> float:
        """How long an unstarted item may wait for a freed provider slot.

        Fast in-slice work must be allowed to queue locally. The wait is still
        capped by the remaining slice and the 8s provider timeout so hung
        held leases cannot block the cadence forever.
        """

        cap = float(self._provider_timeout)
        remaining = self._remaining_slice_seconds()
        if remaining is None:
            return cap
        return max(0.0, min(cap, remaining))

    async def run_slice(
        self,
        priority: PriceEnginePriority,
        *,
        slice_wall_seconds: float | None = None,
        now: datetime | None = None,
    ) -> PriceEngineSliceResult:
        evaluated = now or self.now()
        self.reconstruct()
        due = self.due_items(priority, now=evaluated)
        result = PriceEngineSliceResult()
        started_mono = monotonic()
        deadline = (
            None
            if slice_wall_seconds is None
            else started_mono + float(slice_wall_seconds)
        )

        def remaining() -> float | None:
            if deadline is None:
                return None
            return deadline - monotonic()

        if remaining() is not None and float(remaining() or 0) <= 0:
            for runtime in due:
                runtime.status = PriceEngineItemStatus.NOT_STARTED
                result.not_started.append(runtime.identity.catalogue_row_id)
            return result

        pending = deque(due)
        self._slice_remaining = remaining
        try:
            async def _worker() -> None:
                while True:
                    rem = remaining()
                    if rem is not None and rem <= 0:
                        return
                    try:
                        runtime = pending.popleft()
                    except IndexError:
                        return
                    rem = remaining()
                    if rem is not None and rem <= 0:
                        runtime.status = PriceEngineItemStatus.NOT_STARTED
                        self._record_outcome(runtime, PriceEngineItemStatus.NOT_STARTED, result)
                        continue
                    outcome = await self._price_item(runtime, result)
                    self._record_outcome(runtime, outcome, result)

            worker_n = min(self._slice_worker_limit(), len(due))
            if worker_n > 0:
                await asyncio.gather(
                    *(asyncio.create_task(_worker()) for _ in range(worker_n)),
                    return_exceptions=True,
                )
            while pending:
                runtime = pending.popleft()
                runtime.status = PriceEngineItemStatus.NOT_STARTED
                result.not_started.append(runtime.identity.catalogue_row_id)
        finally:
            self._slice_remaining = None
        self._last_slice_not_started[priority.value] = len(result.not_started)
        result.operation_health = dict(self._operation_health.get(priority.value) or {})
        result.venue_health = venue_health_from_operation_health(result.operation_health)
        result.scan_budget_exhausted = False
        return result

    async def _price_item(
        self,
        runtime: PriceEngineRuntimeItem,
        result: PriceEngineSliceResult,
        *,
        lane: str | None = None,
    ) -> PriceEngineItemStatus:
        identity = runtime.identity
        runtime.in_flight = True
        runtime.status = PriceEngineItemStatus.IN_FLIGHT
        runtime.list_events_calls = 0
        runtime.list_markets_calls = 0
        runtime.pricing_slice_priority = runtime.priority
        if lane is None:
            lane = (
                ScanLane.HOT.value
                if runtime.priority is PriceEnginePriority.HOT
                else PRICE_ENGINE_BACKGROUND_LANE
            )
        try:
            matchbook_ready = bool(identity.matchbook_event_id and identity.matchbook_market_id)
            kalshi_ready = bool(identity.kalshi_event_ticker and _kalshi_tickers(identity))
            pm_tokens = executable_polymarket_token_ids(
                list(identity.polymarket_token_ids),
                event_id=identity.polymarket_event_id,
                market_id=identity.polymarket_market_id,
                condition_id=identity.polymarket_condition_id,
                required_outcomes=list(identity.required_outcomes)
                or required_outcomes_for_key(identity.register_canonical_key),
            )
            polymarket_ready = bool(pm_tokens)
            if not matchbook_ready and not (kalshi_ready and polymarket_ready):
                return self._request_revalidation(runtime, "missing_matchbook_identity")
            if not kalshi_ready and not (matchbook_ready and polymarket_ready):
                return self._request_revalidation(runtime, "missing_kalshi_identity")
            if not polymarket_ready and not (matchbook_ready and kalshi_ready):
                return self._request_revalidation(runtime, "missing_polymarket_identity")

            active_lane = str(lane).strip().casefold() == PRICE_ENGINE_ACTIVE_TRADE_LANE
            viability = assess_identity_viability(
                identity,
                cache=self.viability_cache,
                active_event_ids=self._exempt_event_ids,
                active_trade_lane=active_lane,
            )
            result.viable_venue_count = viability.viable_venue_count
            if viability.skip_expensive_work:
                saved = _expected_provider_calls(
                    matchbook_ready=matchbook_ready,
                    kalshi_tickers=_kalshi_tickers(identity) if kalshi_ready else [],
                    polymarket_tokens=pm_tokens if polymarket_ready else [],
                )
                return self._skip_item(
                    runtime,
                    result,
                    reason=viability.reason or CROSS_VENUE_UNAVAILABLE,
                    saved_calls=saved,
                    viable_count=viability.viable_venue_count,
                )

            matchbook_payload: RetrievedVenuePayload | None = None
            known_implied: dict[str, Decimal] = {}
            if matchbook_ready:
                matchbook_payload = await self._refresh_matchbook(runtime, lane=lane)
                if isinstance(matchbook_payload, PriceEngineItemStatus):
                    if runtime.last_error_detail and ":gone" in str(runtime.last_error_detail):
                        remaining = _expected_provider_calls(
                            matchbook_ready=False,
                            kalshi_tickers=_kalshi_tickers(identity) if kalshi_ready else [],
                            polymarket_tokens=pm_tokens if polymarket_ready else [],
                        )
                        result.skipped_provider_calls += remaining
                        result.saved_provider_calls += remaining
                        result.viable_venue_count = assess_identity_viability(
                            identity,
                            cache=self.viability_cache,
                            active_event_ids=self._exempt_event_ids,
                            active_trade_lane=active_lane,
                        ).viable_venue_count
                    return self._finalize_provider_status(runtime, matchbook_payload)
                known_implied = merge_known_implied(
                    self._implied_from_matchbook(identity, matchbook_payload)
                )
                if not active_lane and not viability.active_trade_override:
                    post_mb = assess_identity_viability(
                        identity,
                        cache=self.viability_cache,
                        active_event_ids=self._exempt_event_ids,
                        active_trade_lane=False,
                    )
                    result.viable_venue_count = post_mb.viable_venue_count
                    if post_mb.skip_expensive_work:
                        saved = _expected_provider_calls(
                            matchbook_ready=False,
                            kalshi_tickers=_kalshi_tickers(identity) if kalshi_ready else [],
                            polymarket_tokens=pm_tokens if polymarket_ready else [],
                        )
                        return self._skip_item(
                            runtime,
                            result,
                            reason=post_mb.reason or CROSS_VENUE_UNAVAILABLE,
                            saved_calls=saved,
                            viable_count=post_mb.viable_venue_count,
                        )

            kalshi_books: dict[str, RetrievedVenuePayload] | None = None
            if kalshi_ready:
                kalshi_books = await self._refresh_kalshi_constituents(
                    runtime,
                    lane=lane,
                    result=result,
                    known_implied=known_implied,
                    skip_bound=active_lane or viability.active_trade_override,
                )
                if isinstance(kalshi_books, PriceEngineItemStatus):
                    return self._finalize_provider_status(runtime, kalshi_books)
                required = _required_tickers(identity)
                if any(ticker not in kalshi_books for ticker in required):
                    if runtime.status is PriceEngineItemStatus.SKIPPED:
                        return PriceEngineItemStatus.SKIPPED
                    return self._schedule_retry(runtime, PRICE_ENGINE_ITEM_TIMEOUT_REASON)

            polymarket_books: dict[str, RetrievedVenuePayload] | None = None
            if polymarket_ready:
                polymarket_books = await self._refresh_polymarket(runtime, lane=lane, tokens=pm_tokens)
                if isinstance(polymarket_books, PriceEngineItemStatus):
                    if matchbook_payload is not None and kalshi_books is not None:
                        polymarket_books = None
                    else:
                        return self._finalize_provider_status(runtime, polymarket_books)

            if matchbook_payload is not None and kalshi_books is not None:
                status = await self._evaluate_complete_item(
                    runtime,
                    matchbook=matchbook_payload,
                    kalshi_books=kalshi_books,
                    result=result,
                )
                if (
                    status is PriceEngineItemStatus.EVALUATED
                    and polymarket_books
                    and self.paper_scan is not None
                ):
                    await self._evaluate_extra_polymarket_pairs(
                        runtime,
                        matchbook=matchbook_payload,
                        kalshi_books=kalshi_books,
                        polymarket_books=polymarket_books,
                        result=result,
                    )
                return status
            return await self._evaluate_flexible_pairs(
                runtime,
                matchbook=matchbook_payload,
                kalshi_books=kalshi_books,
                polymarket_books=polymarket_books,
                result=result,
            )
        finally:
            runtime.in_flight = False

    async def _refresh_matchbook(
        self,
        runtime: PriceEngineRuntimeItem,
        *,
        lane: str,
    ) -> RetrievedVenuePayload | PriceEngineItemStatus:
        identity = runtime.identity
        getter = getattr(self.matchbook, "get_market", None)
        if not callable(getter):
            return self._request_revalidation(runtime, "matchbook_get_market_unavailable")
        try:
            payload, status = await self._provider_call(
                VenueName.MATCHBOOK,
                lane=lane,
                stage="get_market",
                source_id=str(identity.matchbook_market_id),
                coro=getter(identity.matchbook_event_id, identity.matchbook_market_id),
            )
        except MatchbookMarketGoneError:
            self.viability_cache.mark_unavailable(
                identity.canonical_event_id, VenueName.MATCHBOOK
            )
            return self._request_revalidation(runtime, f"{CATALOGUE_REVALIDATION_REASON}:gone")
        if status is not None:
            if status is PriceEngineItemStatus.RETRY_WAIT:
                runtime.last_error_stage = "get_market"
                runtime.last_error_detail = f"get_market_timeout after {self._provider_timeout:g}s"
            return status
        market = extract_matchbook_market_payload(payload)
        if market is None or matchbook_payload_is_terminal(market):
            state = (
                VenueViability.TERMINAL
                if market is not None and matchbook_payload_is_terminal(market)
                else VenueViability.UNAVAILABLE
            )
            self.viability_cache.mark(identity.canonical_event_id, VenueName.MATCHBOOK, state)
            return self._request_revalidation(runtime, f"{CATALOGUE_REVALIDATION_REASON}:gone")
        if str(market.get("id") or "") != str(identity.matchbook_market_id):
            return self._request_revalidation(runtime, f"{CATALOGUE_REVALIDATION_REASON}:identity")
        self.viability_cache.mark_viable(identity.canonical_event_id, VenueName.MATCHBOOK)
        return RetrievedVenuePayload(payload=market, retrieved_at=self.now())

    async def _refresh_kalshi_constituents(
        self,
        runtime: PriceEngineRuntimeItem,
        *,
        lane: str,
        result: PriceEngineSliceResult | None = None,
        known_implied: Mapping[str, Decimal] | None = None,
        skip_bound: bool = False,
    ) -> dict[str, RetrievedVenuePayload] | PriceEngineItemStatus:
        identity = runtime.identity
        client = self.kalshi
        getter = getattr(client, "get_order_book", None) if client is not None else None
        if not callable(getter):
            return self._request_revalidation(runtime, "kalshi_order_book_unavailable")
        books: dict[str, RetrievedVenuePayload] = {}
        tickers = _kalshi_tickers(identity)
        required_outcomes = list(identity.required_outcomes) or required_outcomes_for_key(
            identity.register_canonical_key
        )
        accumulated = dict(known_implied or {})
        min_net = self._minimum_net_edge(identity)
        for index, ticker in enumerate(tickers):
            if not skip_bound and min_net is not None and required_outcomes:
                remaining = [
                    outcome
                    for outcome in (
                        _ticker_outcome(identity, item) for item in tickers[index:]
                    )
                    if outcome
                ]
                bound = optimistic_net_edge_upper_bound(
                    required_outcomes=required_outcomes,
                    known_implied=accumulated,
                    unknown_outcomes=remaining,
                    minimum_net_edge=min_net,
                )
                if result is not None and bound.upper_bound_net_edge is not None:
                    result.upper_bound_net_edge = str(bound.upper_bound_net_edge)
                if bound.prune:
                    saved = len(tickers) - index
                    return self._skip_item(
                        runtime,
                        result or PriceEngineSliceResult(),
                        reason=UPPER_BOUND_BELOW_MIN_NET,
                        saved_calls=saved,
                        viable_count=result.viable_venue_count if result is not None else None,
                        upper_bound=bound.upper_bound_net_edge,
                    )
            payload, status = await self._provider_call(
                VenueName.KALSHI,
                lane=lane,
                stage="order_book",
                source_id=ticker,
                coro=getter(identity.kalshi_event_ticker, ticker),
            )
            if status is not None:
                if status is PriceEngineItemStatus.RETRY_WAIT:
                    runtime.last_error_stage = "order_book"
                    runtime.last_error_detail = PRICE_ENGINE_ITEM_TIMEOUT_REASON
                return status
            if payload is None:
                runtime.last_error_stage = "order_book"
                runtime.last_error_detail = PRICE_ENGINE_ITEM_TIMEOUT_REASON
                return self._schedule_retry(runtime, PRICE_ENGINE_ITEM_TIMEOUT_REASON)
            books[ticker] = RetrievedVenuePayload(payload=payload, retrieved_at=self.now())
            self.viability_cache.mark_viable(identity.canonical_event_id, VenueName.KALSHI)
            outcome = _ticker_outcome(identity, ticker)
            implied = implied_from_kalshi_book(payload)
            if outcome and implied is not None:
                accumulated = merge_known_implied(accumulated, {outcome: implied})
        return books

    async def _refresh_polymarket(
        self,
        runtime: PriceEngineRuntimeItem,
        *,
        lane: str,
        tokens: list[Any],
    ) -> dict[str, RetrievedVenuePayload] | PriceEngineItemStatus:
        identity = runtime.identity
        getter = getattr(self.polymarket, "get_order_book", None) if self.polymarket is not None else None
        if not callable(getter):
            return self._request_revalidation(runtime, "polymarket_order_book_unavailable")
        books: dict[str, RetrievedVenuePayload] = {}
        for item in tokens:
            token = str(getattr(item, "native_id", item) or "").strip()
            if not token:
                return self._request_revalidation(runtime, "missing_polymarket_clob_token")
            payload, status = await self._provider_call(
                VenueName.POLYMARKET,
                lane=lane,
                stage="order_book",
                source_id=token,
                coro=getter(
                    identity.polymarket_event_id,
                    identity.polymarket_market_id,
                    token,
                ),
            )
            if status is not None:
                if status is PriceEngineItemStatus.RETRY_WAIT:
                    runtime.last_error_stage = "order_book"
                    runtime.last_error_detail = PRICE_ENGINE_ITEM_TIMEOUT_REASON
                return status
            if payload is None:
                runtime.last_error_stage = "order_book"
                runtime.last_error_detail = PRICE_ENGINE_ITEM_TIMEOUT_REASON
                return self._schedule_retry(runtime, PRICE_ENGINE_ITEM_TIMEOUT_REASON)
            books[token] = RetrievedVenuePayload(payload=payload, retrieved_at=self.now())
        return books

    async def _evaluate_complete_item(
        self,
        runtime: PriceEngineRuntimeItem,
        *,
        matchbook: RetrievedVenuePayload,
        kalshi_books: Mapping[str, RetrievedVenuePayload],
        result: PriceEngineSliceResult,
    ) -> PriceEngineItemStatus:
        identity = runtime.identity
        evaluated_at = self.now()
        matchbook_age = matchbook_market_quote_age(
            matchbook.payload,
            retrieved_at=matchbook.retrieved_at,
            evaluated_at=evaluated_at,
        )
        required_tickers = _required_tickers(identity)
        kalshi_age = _oldest_retrieval_age(
            [
                kalshi_books[ticker].retrieved_at
                for ticker in required_tickers
                if ticker in kalshi_books
            ],
            evaluated_at=evaluated_at,
            required=len(required_tickers),
        )
        try:
            matchbook_obs = self.matchbook_builder.build(
                _synthetic_matchbook_event(identity),
                matchbook.payload,
                observed_at=evaluated_at,
                quote_age_ms=matchbook_age.quote_age_ms,
                quote_age_basis=matchbook_age.basis or "retrieval",
                quote_age_reason=matchbook_age.reason,
            )
        except Exception as exc:
            return self._request_revalidation(runtime, f"{CATALOGUE_REVALIDATION_REASON}:{exc}")
        kalshi_market = _canonical_kalshi_market(identity)
        if kalshi_market is None:
            return self._request_revalidation(runtime, f"{CATALOGUE_REVALIDATION_REASON}:kalshi_identity")
        fee_snapshot = self._fee_snapshot_payload(identity)
        kalshi_payloads = {ticker: item.payload for ticker, item in kalshi_books.items()}
        try:
            kalshi_obs = self.kalshi_builder.build_from_canonical(
                kalshi_market,
                kalshi_payloads,
                observed_at=evaluated_at,
                quote_age_ms=kalshi_age.quote_age_ms,
                quote_age_basis=kalshi_age.basis,
                quote_age_reason=kalshi_age.reason,
                fee_snapshot=fee_snapshot,
            )
        except Exception as exc:
            runtime.last_error_stage = "build_observation"
            runtime.last_error_detail = str(exc)
            return self._schedule_retry(runtime, str(exc))

        decision: PaperScanDecision | None = None
        if self.paper_scan is not None:
            scan_kwargs: dict[str, Any] = {
                "venue_costs": self.venue_costs or None,
                "fx_snapshots": self.fx_snapshots or None,
                "fixture_canonical_event_id": identity.canonical_event_id,
            }
            settings = getattr(self.paper_scan, "settings", None)
            if settings is not None:
                operator = effective_operator_scanner_settings(settings)
                scan_kwargs["minimum_net_edge"] = operator.min_net_edge
                scan_kwargs["market_scope"] = catalogue_market_scope(identity)
                scan_kwargs["outright_min_net_edge"] = operator.outright_min_net_edge
                scan_kwargs["maximum_execution_risk"] = operator.max_execution_risk
                scan_kwargs["assumed_latency_ms"] = int(settings.simulated_latency_ms)
            decision = self.paper_scan.scan_pair(matchbook_obs, kalshi_obs, **scan_kwargs)
            result.decisions.append(decision)
        if runtime.pricing_slice_priority is None:
            runtime.pricing_slice_priority = runtime.priority
        self._maybe_promote(runtime, decision, result)
        self._schedule_projection(
            runtime,
            matchbook_obs=matchbook_obs,
            kalshi_obs=kalshi_obs,
            decision=decision,
        )
        await self._handoff_item_decision(runtime, decision, result)
        runtime.last_priced_at = evaluated_at
        runtime.retry_attempt = 0
        runtime.next_retry_at = None
        runtime.last_error_stage = None
        runtime.last_error_detail = None
        runtime.status = PriceEngineItemStatus.EVALUATED
        return PriceEngineItemStatus.EVALUATED

    async def _evaluate_extra_polymarket_pairs(
        self,
        runtime: PriceEngineRuntimeItem,
        *,
        matchbook: RetrievedVenuePayload,
        kalshi_books: Mapping[str, RetrievedVenuePayload],
        polymarket_books: Mapping[str, RetrievedVenuePayload],
        result: PriceEngineSliceResult,
    ) -> None:
        """Scheduled-reprice Polymarket against already-refreshed MB/Kalshi legs."""

        if self.paper_scan is None:
            return
        identity = runtime.identity
        evaluated_at = self.now()
        try:
            matchbook_obs = self._build_matchbook_obs(identity, matchbook, evaluated_at)
            kalshi_obs = self._build_kalshi_obs(identity, kalshi_books, evaluated_at)
            polymarket_obs = self._build_polymarket_obs(identity, polymarket_books, evaluated_at)
        except Exception:
            return
        scan_kwargs = self._scan_kwargs(identity)
        for left, right in (
            (matchbook_obs, polymarket_obs),
            (polymarket_obs, kalshi_obs),
        ):
            try:
                decision = self.paper_scan.scan_pair(left, right, **scan_kwargs)
            except Exception:
                continue
            result.decisions.append(decision)
            if _decision_is_interesting(decision):
                self._maybe_promote(runtime, decision, result)
            await self._handoff_item_decision(runtime, decision, result)

    async def _evaluate_flexible_pairs(
        self,
        runtime: PriceEngineRuntimeItem,
        *,
        matchbook: RetrievedVenuePayload | None,
        kalshi_books: Mapping[str, RetrievedVenuePayload] | None,
        polymarket_books: Mapping[str, RetrievedVenuePayload] | None,
        result: PriceEngineSliceResult,
    ) -> PriceEngineItemStatus:
        identity = runtime.identity
        evaluated_at = self.now()
        observations: dict[VenueName, VenueMarketObservation] = {}
        try:
            if matchbook is not None:
                observations[VenueName.MATCHBOOK] = self._build_matchbook_obs(
                    identity, matchbook, evaluated_at
                )
            if kalshi_books is not None:
                observations[VenueName.KALSHI] = self._build_kalshi_obs(
                    identity, kalshi_books, evaluated_at
                )
            if polymarket_books is not None:
                observations[VenueName.POLYMARKET] = self._build_polymarket_obs(
                    identity, polymarket_books, evaluated_at
                )
        except Exception as exc:
            return self._request_revalidation(runtime, f"{CATALOGUE_REVALIDATION_REASON}:{exc}")
        if len(observations) < 2 or self.paper_scan is None:
            return self._request_revalidation(runtime, f"{CATALOGUE_REVALIDATION_REASON}:incomplete_pair")
        scan_kwargs = self._scan_kwargs(identity)
        venues = list(observations)
        decision = None
        for index, left_venue in enumerate(venues):
            for right_venue in venues[index + 1 :]:
                try:
                    decision = self.paper_scan.scan_pair(
                        observations[left_venue],
                        observations[right_venue],
                        **scan_kwargs,
                    )
                except Exception as exc:
                    runtime.last_error_stage = "build_observation"
                    runtime.last_error_detail = str(exc)
                    continue
                result.decisions.append(decision)
                await self._handoff_item_decision(runtime, decision, result)
        if not result.decisions:
            return self._schedule_retry(runtime, runtime.last_error_detail or "order_book_unavailable")
        if runtime.pricing_slice_priority is None:
            runtime.pricing_slice_priority = runtime.priority
        self._maybe_promote(runtime, decision, result)
        runtime.last_priced_at = evaluated_at
        runtime.retry_attempt = 0
        runtime.next_retry_at = None
        runtime.last_error_stage = None
        runtime.last_error_detail = None
        runtime.status = PriceEngineItemStatus.EVALUATED
        return PriceEngineItemStatus.EVALUATED

    def _scan_kwargs(self, identity: DerivedPriceEngineItem) -> dict[str, Any]:
        scan_kwargs: dict[str, Any] = {
            "venue_costs": self.venue_costs or None,
            "fx_snapshots": self.fx_snapshots or None,
            "fixture_canonical_event_id": identity.canonical_event_id,
        }
        settings = getattr(self.paper_scan, "settings", None)
        if settings is not None:
            operator = effective_operator_scanner_settings(settings)
            scan_kwargs["minimum_net_edge"] = operator.min_net_edge
            scan_kwargs["maximum_execution_risk"] = operator.max_execution_risk
            scan_kwargs["assumed_latency_ms"] = int(settings.simulated_latency_ms)
        return scan_kwargs

    def _build_matchbook_obs(
        self,
        identity: DerivedPriceEngineItem,
        matchbook: RetrievedVenuePayload,
        evaluated_at: datetime,
    ) -> VenueMarketObservation:
        matchbook_age = matchbook_market_quote_age(
            matchbook.payload,
            retrieved_at=matchbook.retrieved_at,
            evaluated_at=evaluated_at,
        )
        return self.matchbook_builder.build(
            _synthetic_matchbook_event(identity),
            matchbook.payload,
            observed_at=evaluated_at,
            quote_age_ms=matchbook_age.quote_age_ms,
            quote_age_basis=matchbook_age.basis or "retrieval",
            quote_age_reason=matchbook_age.reason,
        )

    def _build_kalshi_obs(
        self,
        identity: DerivedPriceEngineItem,
        kalshi_books: Mapping[str, RetrievedVenuePayload],
        evaluated_at: datetime,
    ) -> VenueMarketObservation:
        required_tickers = _required_tickers(identity)
        kalshi_age = _oldest_retrieval_age(
            [
                kalshi_books[ticker].retrieved_at
                for ticker in required_tickers
                if ticker in kalshi_books
            ],
            evaluated_at=evaluated_at,
            required=len(required_tickers),
        )
        kalshi_market = _canonical_kalshi_market(identity)
        if kalshi_market is None:
            raise ValueError("kalshi_identity")
        return self.kalshi_builder.build_from_canonical(
            kalshi_market,
            {ticker: item.payload for ticker, item in kalshi_books.items()},
            observed_at=evaluated_at,
            quote_age_ms=kalshi_age.quote_age_ms,
            quote_age_basis=kalshi_age.basis,
            quote_age_reason=kalshi_age.reason,
            fee_snapshot=self._fee_snapshot_payload(identity),
        )

    def _build_polymarket_obs(
        self,
        identity: DerivedPriceEngineItem,
        polymarket_books: Mapping[str, RetrievedVenuePayload],
        evaluated_at: datetime,
    ) -> VenueMarketObservation:
        market = _canonical_polymarket_market(identity)
        if market is None:
            raise ValueError("polymarket_identity")
        age = _oldest_retrieval_age(
            [item.retrieved_at for item in polymarket_books.values()],
            evaluated_at=evaluated_at,
            required=len(identity.polymarket_token_ids) or 1,
        )
        return self.polymarket_builder.build(
            {"id": identity.polymarket_event_id, "title": f"{identity.home_canonical} vs {identity.away_canonical}"},
            {
                "id": identity.polymarket_market_id,
                "conditionId": identity.polymarket_condition_id,
                "clobTokenIds": [item.native_id for item in identity.polymarket_token_ids],
            },
            {token: item.payload for token, item in polymarket_books.items()},
            canonical=market,
            observed_at=evaluated_at,
            quote_age_ms=age.quote_age_ms,
            quote_age_basis=age.basis,
            quote_age_reason=age.reason,
        )

    def _refresh_promoted_hot_ids(self) -> None:
        """Derive fixture HOT from any live interesting catalogue row.

        Reconstruct / content-version invalidation drops stale row state.
        There is no durable promotion table.
        """

        live_versions = {
            runtime.identity.catalogue_row_id: runtime.identity.content_version
            for runtime in self._items.values()
        }
        for row_id, version in list(self._promoted_hot_rows.items()):
            if live_versions.get(row_id) != version:
                self._promoted_hot_rows.pop(row_id, None)
        self._promoted_hot_ids = {
            runtime.identity.canonical_event_id
            for runtime in self._items.values()
            if self._promoted_hot_rows.get(runtime.identity.catalogue_row_id)
            == runtime.identity.content_version
        }

    def _reclassify_fixture(self, canonical_event_id: str) -> None:
        for runtime in self._items.values():
            if runtime.identity.canonical_event_id == canonical_event_id:
                runtime.priority = self.classify_priority(runtime.identity)

    def _maybe_promote(
        self,
        runtime: PriceEngineRuntimeItem,
        decision: PaperScanDecision | None,
        result: PriceEngineSliceResult,
    ) -> None:
        """HOT promotion/demotion is scheduler truth in process memory.

        Opportunity truth is per catalogue row. Fixture/event promotion is
        the OR of current interesting rows for that canonical_event_id.
        One cooling sibling must not clear another interesting row.
        Lifecycle HOT remains independent of opportunity demotion.
        Lagged UI projection cannot grant or revoke this set.
        """

        row_id = runtime.identity.catalogue_row_id
        version = runtime.identity.content_version
        canonical_id = runtime.identity.canonical_event_id
        if _decision_is_interesting(decision):
            already_fixture = canonical_id in self._promoted_hot_ids
            was_scheduler_hot = (
                already_fixture
                or runtime.priority is PriceEnginePriority.HOT
                or runtime.pricing_slice_priority is PriceEnginePriority.HOT
            )
            self._promoted_hot_rows[row_id] = version
            self._promoted_hot_ids.add(canonical_id)
            if not already_fixture:
                result.promotions.append(canonical_id)
                if not was_scheduler_hot:
                    self._emit_operator_hot_promotion(runtime, decision, result)
        else:
            self._promoted_hot_rows.pop(row_id, None)
            self._refresh_promoted_hot_ids()
        self._reclassify_fixture(canonical_id)

    def _emit_operator_hot_promotion(
        self,
        runtime: PriceEngineRuntimeItem,
        decision: PaperScanDecision | None,
        result: PriceEngineSliceResult,
    ) -> None:
        """Persist one operator-feed event for a real BACKGROUND→HOT episode."""

        identity = runtime.identity
        canonical_id = identity.canonical_event_id
        episode = self._hot_promotion_episodes.get(canonical_id, 0) + 1
        self._hot_promotion_episodes[canonical_id] = episode
        edge = decision_net_edge(decision) if decision is not None else None
        trigger = None if decision is None else decision.minimum_net_edge
        distance = None
        if edge is not None and trigger is not None:
            distance = distance_to_trigger_pp(edge, trigger)
        home = identity.home_canonical
        away = identity.away_canonical
        fixture_label = None
        if home and away:
            fixture_label = f"{home} v {away}"
        elif home or away:
            fixture_label = home or away
        lane = runtime.pricing_slice_priority or runtime.priority or PriceEnginePriority.BACKGROUND
        fact = HotPromotionFact(
            canonical_event_id=canonical_id,
            catalogue_row_id=identity.catalogue_row_id,
            content_version=identity.content_version,
            occurred_at=self.now(),
            opportunity_id=hot_promotion_opportunity_id(canonical_id),
            episode=episode,
            fixture_label=fixture_label,
            market_family=identity.family,
            pricing_lane=getattr(lane, "value", str(lane)),
            current_net_edge=edge,
            distance_to_trigger_pp=distance,
        )
        if self.on_hot_promotion is None:
            return
        try:
            self.on_hot_promotion(fact)
        except Exception as exc:
            LOGGER.exception("hot promotion persist failed for %s", canonical_id)
            result.persist_failures.append(f"hot_promotion:{canonical_id}:{exc}")

    def _schedule_projection(
        self,
        runtime: PriceEngineRuntimeItem,
        *,
        matchbook_obs: VenueMarketObservation,
        kalshi_obs: VenueMarketObservation,
        decision: PaperScanDecision | None,
    ) -> None:
        """UI/current-state projection is a consumer. It must not delay the next item."""

        event = PriceEngineProjectionEvent(
            identity=runtime.identity.model_copy(deep=True),
            priority=runtime.priority,
            matchbook_obs=matchbook_obs.model_copy(deep=True),
            kalshi_obs=kalshi_obs.model_copy(deep=True),
            decision=None if decision is None else decision.model_copy(deep=True),
            reset_generation=(
                0 if self.fixture_state is None else self.fixture_state.reset_generation
            ),
        )
        self.observability.emit(lambda snapshot=event: self._project_item_state(snapshot))

    def _project_item_state(self, event: PriceEngineProjectionEvent) -> None:
        if self.fixture_state is None:
            return
        if event.reset_generation != self.fixture_state.reset_generation:
            return
        identity = event.identity
        matchbook_obs = event.matchbook_obs
        kalshi_obs = event.kalshi_obs
        decision = event.decision
        observed_at = matchbook_obs.observed_at
        fixture = DiscoveredFixture(
            source=VenueName.MATCHBOOK,
            source_event_id=str(identity.matchbook_event_id or identity.canonical_event_id),
            canonical_event_id=identity.canonical_event_id,
            home_team=identity.home_canonical or "Home",
            away_team=identity.away_canonical or "Away",
            competition=identity.competition or "Premier League",
            kickoff_utc=identity.kickoff_utc or observed_at,
            last_seen_at=observed_at,
            last_scanned_at=observed_at,
            matchbook_matched=True,
            kalshi_matched=True,
            market_evaluation_state=MarketEvaluationState.EVALUATED.value,
            opportunity_state="matched",
            scan_lane=(
                ScanLane.HOT.value
                if event.priority is PriceEnginePriority.HOT
                else ScanLane.UNIVERSE.value
            ),
            current_net_edge=None if decision is None else decision_net_edge(decision),
            solver_is_arbitrage=(
                False if decision is None else decision_is_solver_arbitrage(decision)
            ),
        )
        decisions = [] if decision is None else [decision]
        if decision is not None:
            if decision_is_solver_arbitrage(decision):
                fixture.opportunity_state = "qualifying"
            else:
                edge = decision_net_edge(decision)
                if edge is not None and edge > 0:
                    fixture.opportunity_state = "near"
        mb_inv = _inventory_from_observation(matchbook_obs)
        k_inv = _inventory_from_observation(kalshi_obs)
        pair_key = (
            matchbook_obs.market.source_venue.value,
            matchbook_obs.market.source_market_id,
            kalshi_obs.market.source_venue.value,
            kalshi_obs.market.source_market_id,
        )
        rows = assemble_fixture_inventory(
            [mb_inv],
            [],
            kalshi_markets=[k_inv],
            matcher=None if self.paper_scan is None else getattr(self.paper_scan, "market_matcher", None),
            decisions_by_source_ids=(
                {}
                if decision is None
                else {
                    (
                        matchbook_obs.market.source_market_id,
                        kalshi_obs.market.source_market_id,
                    ): decision
                }
            ),
            decisions_by_pair={} if decision is None else {pair_key: decision},
            venue_costs=self.venue_costs or None,
            fx_snapshots=self.fx_snapshots or None,
            cost_resolver=None if self.paper_scan is None else getattr(self.paper_scan, "cost_resolver", None),
        )
        rows = _overlay_decision_inventory(
            rows,
            identity=identity,
            matchbook_obs=matchbook_obs,
            kalshi_obs=kalshi_obs,
            decision=decision,
        )
        if decision is not None:
            edge = decision_net_edge(decision)
            if edge is not None:
                fixture.current_net_edge = edge
        report = CollectionReport(
            started_at=observed_at,
            completed_at=observed_at,
            matching_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
            enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
            paper_decisions=decisions,
            discovered_fixtures=[fixture],
            fixture_markets={identity.canonical_event_id: rows},
            scan_lane=fixture.scan_lane,
            scan_diagnostics={
                "price_engine": True,
                "priority": event.priority.value,
                "catalogue_row_id": identity.catalogue_row_id,
            },
        )
        self.fixture_state.upsert_from_report(
            report,
            scan_lane=ScanLane.UNIVERSE
            if event.priority is PriceEnginePriority.BACKGROUND
            else ScanLane.HOT,
            now=observed_at,
            reset_generation=event.reset_generation,
        )

    async def _handoff_item_decision(
        self,
        runtime: PriceEngineRuntimeItem,
        decision: PaperScanDecision | None,
        result: PriceEngineSliceResult,
    ) -> None:
        """Start capture immediately. Do not hold a pricing worker for persist."""

        if decision is None or self.on_item_decision is None:
            return
        task = asyncio.create_task(self._run_item_capture(runtime, decision, result))
        self._pending_item_captures.add(task)
        task.add_done_callback(self._capture_task_done)
        await asyncio.sleep(0)

    async def _run_item_capture(
        self,
        runtime: PriceEngineRuntimeItem,
        decision: PaperScanDecision,
        result: PriceEngineSliceResult,
    ) -> None:
        row_id = runtime.identity.catalogue_row_id
        try:
            outcome = self.on_item_decision(decision, runtime)
            if isawaitable(outcome):
                await outcome
        except Exception as exc:
            LOGGER.exception(
                "price-engine item persist/capture failed row=%s",
                row_id,
            )
            runtime.last_persist_error = str(exc)
            result.persist_failures.append(row_id)
            result.issues.append(
                CollectorIssue(
                    stage=PRICE_ENGINE_PERSIST_STAGE,
                    source_id=row_id,
                    detail=str(exc),
                )
            )

    def _capture_task_done(self, task: asyncio.Task[Any]) -> None:
        self._pending_item_captures.discard(task)
        if task.cancelled():
            return
        try:
            exc = task.exception()
        except asyncio.CancelledError:
            return
        if exc is not None:
            LOGGER.exception("price-engine capture task crashed", exc_info=exc)

    async def drain_item_captures(self) -> None:
        """Await in-memory item captures outside the HOT pricing envelope."""

        pending = list(self._pending_item_captures)
        if not pending:
            return
        await asyncio.gather(*pending, return_exceptions=True)

    async def drain_observability(self) -> None:
        """Wait for lagged UI/audit consumers. Never called from the pricing worker."""

        await self.observability.drain()

    async def shutdown_observability(self) -> None:
        """Finish accepted consumers then join the worker. Not a pricing await."""

        await self.observability.shutdown()

    def schedule_observability(self, fn: Callable[[], Any]) -> None:
        """Enqueue non-critical audit/history work without delaying capture."""

        self.observability.emit(fn)

    def _record_lane_operation(
        self,
        lane: str,
        venue: VenueName,
        operation: str,
        status: str,
    ) -> None:
        key = (
            PriceEnginePriority.HOT.value
            if str(lane).strip().casefold() == PriceEnginePriority.HOT.value
            else PriceEnginePriority.BACKGROUND.value
        )
        self._operation_health[key] = record_operation_health(
            self._operation_health.get(key),
            venue=venue.value,
            operation=operation,
            status=status,
        )

    async def _provider_call(
        self,
        venue: VenueName,
        *,
        lane: str,
        stage: str,
        source_id: str,
        coro: Any,
    ) -> tuple[Any, PriceEngineItemStatus | None]:
        access = self.provider_access
        timeout = self._provider_timeout
        if access is None:
            try:
                payload = await asyncio.wait_for(coro, timeout=timeout)
                self._record_lane_operation(lane, venue, stage, HEALTH_OK)
                return payload, None
            except TimeoutError:
                self._record_lane_operation(lane, venue, stage, HEALTH_MARKET_TIMEOUT)
                return None, PriceEngineItemStatus.RETRY_WAIT

        async def _close_unused() -> None:
            close = getattr(coro, "close", None)
            if callable(close):
                try:
                    close()
                except (RuntimeError, ValueError):
                    pass

        slot_wait = self._slot_wait_seconds()
        if slot_wait <= 0:
            await _close_unused()
            if access.venue_saturated(venue):
                self._record_lane_operation(lane, venue, stage, PROVIDER_CAPACITY_SATURATED)
                return None, PriceEngineItemStatus.DEFERRED
            return None, PriceEngineItemStatus.NOT_STARTED
        async with access.acquire_wait(
            venue, lane=lane, stage=stage, timeout=slot_wait
        ) as lease:
            if lease is None:
                await _close_unused()
                if access.venue_saturated(venue):
                    self._record_lane_operation(lane, venue, stage, PROVIDER_CAPACITY_SATURATED)
                    return None, PriceEngineItemStatus.DEFERRED
                return None, PriceEngineItemStatus.NOT_STARTED
            inflight = access.snapshot().inflight.get(venue.value, 0)
            self._peak_held_slots[venue.value] = max(
                self._peak_held_slots.get(venue.value, 0), inflight
            )
            task = asyncio.create_task(coro)
            done, _pending = await asyncio.wait({task}, timeout=timeout)
            if task in done:
                try:
                    payload = task.result()
                    self._record_lane_operation(lane, venue, stage, HEALTH_OK)
                    return payload, None
                except MatchbookMarketGoneError:
                    raise
                except Exception:
                    self._record_lane_operation(lane, venue, stage, HEALTH_MARKET_TIMEOUT)
                    return None, PriceEngineItemStatus.RETRY_WAIT
            task.cancel()
            held = False
            if isinstance(lease, ProviderLease):
                held = lease.hold_until_task(task)
            if not held and not task.done():
                task.add_done_callback(lambda done_task: done_task.exception() if done_task.done() else None)
            self._record_lane_operation(lane, venue, stage, HEALTH_MARKET_TIMEOUT)
            return None, PriceEngineItemStatus.RETRY_WAIT

    def _finalize_provider_status(
        self,
        runtime: PriceEngineRuntimeItem,
        status: PriceEngineItemStatus,
    ) -> PriceEngineItemStatus:
        if status is PriceEngineItemStatus.RETRY_WAIT:
            runtime.last_error_detail = runtime.last_error_detail or PRICE_ENGINE_ITEM_TIMEOUT_REASON
            runtime.last_error_stage = runtime.last_error_stage or "order_book"
            return self._schedule_retry(runtime, runtime.last_error_detail)
        return status

    def _schedule_retry(self, runtime: PriceEngineRuntimeItem, detail: str) -> PriceEngineItemStatus:
        runtime.retry_attempt += 1
        delay = price_engine_retry_backoff_seconds(runtime.retry_attempt)
        runtime.next_retry_at = self.now() + timedelta(seconds=delay)
        runtime.last_error_detail = detail
        runtime.status = PriceEngineItemStatus.RETRY_WAIT
        return PriceEngineItemStatus.RETRY_WAIT

    def _request_revalidation(self, runtime: PriceEngineRuntimeItem, reason: str) -> PriceEngineItemStatus:
        runtime.status = PriceEngineItemStatus.REVALIDATION_NEEDED
        runtime.last_error_stage = "catalogue_revalidation"
        runtime.last_error_detail = reason
        self.revalidation_requests.append(
            {
                "catalogue_row_id": runtime.identity.catalogue_row_id,
                "canonical_event_id": runtime.identity.canonical_event_id,
                "reason": reason,
            }
        )
        return PriceEngineItemStatus.REVALIDATION_NEEDED

    def _skip_item(
        self,
        runtime: PriceEngineRuntimeItem,
        result: PriceEngineSliceResult,
        *,
        reason: str,
        saved_calls: int,
        viable_count: int | None,
        upper_bound: Decimal | None = None,
    ) -> PriceEngineItemStatus:
        runtime.status = PriceEngineItemStatus.SKIPPED
        runtime.last_priced_at = self.now()
        runtime.last_error_stage = "opportunity_viability"
        runtime.last_error_detail = reason
        runtime.retry_attempt = 0
        runtime.next_retry_at = None
        result.skipped_provider_calls += max(0, int(saved_calls))
        result.saved_provider_calls += max(0, int(saved_calls))
        if viable_count is not None:
            result.viable_venue_count = viable_count
        if upper_bound is not None:
            result.upper_bound_net_edge = str(upper_bound)
        result.skip_reasons[runtime.identity.catalogue_row_id] = reason
        self._project_skipped_state(runtime, reason=reason)
        return PriceEngineItemStatus.SKIPPED

    def _project_skipped_state(self, runtime: PriceEngineRuntimeItem, *, reason: str) -> None:
        if self.fixture_state is None:
            return
        identity = runtime.identity
        observed_at = self.now()
        if reason == NO_CROSS_VENUE_CANDIDATE:
            evaluation_state = MarketEvaluationState.SINGLE_VENUE_NO_CROSS_VENUE_CANDIDATE.value
        elif reason == UPPER_BOUND_BELOW_MIN_NET:
            evaluation_state = MarketEvaluationState.UPPER_BOUND_BELOW_MIN_NET.value
        else:
            evaluation_state = MarketEvaluationState.CROSS_VENUE_UNAVAILABLE.value
        fixture = DiscoveredFixture(
            source=VenueName.MATCHBOOK,
            source_event_id=str(identity.matchbook_event_id or identity.canonical_event_id),
            canonical_event_id=identity.canonical_event_id,
            home_team=identity.home_canonical or "Home",
            away_team=identity.away_canonical or "Away",
            competition=identity.competition or "Premier League",
            kickoff_utc=identity.kickoff_utc or observed_at,
            last_seen_at=observed_at,
            last_scanned_at=observed_at,
            matchbook_matched=bool(identity.matchbook_event_id),
            kalshi_matched=bool(identity.kalshi_event_ticker),
            polymarket_matched=bool(identity.polymarket_event_id),
            market_evaluation_state=evaluation_state,
            market_evaluation_reason=reason,
            no_comparison_reason=reason,
            opportunity_state="not_evaluated",
            solver_is_arbitrage=False,
            scan_lane=(
                ScanLane.HOT.value
                if runtime.priority is PriceEnginePriority.HOT
                else ScanLane.UNIVERSE.value
            ),
        )
        self.fixture_state.upsert_evaluated_fixture(
            fixture,
            scan_lane=ScanLane.UNIVERSE
            if runtime.priority is PriceEnginePriority.BACKGROUND
            else ScanLane.HOT,
            now=observed_at,
        )

    def _implied_from_matchbook(
        self,
        identity: DerivedPriceEngineItem,
        matchbook: RetrievedVenuePayload,
    ) -> dict[str, Decimal]:
        parsed = implied_from_matchbook_market(matchbook.payload)
        required = {str(item) for item in identity.required_outcomes or []}
        if required:
            parsed = {key: value for key, value in parsed.items() if key in required}
        if parsed:
            return parsed
        try:
            observation = self._build_matchbook_obs(identity, matchbook, self.now())
        except Exception:
            return {}
        from_obs = implied_from_observation(observation)
        if required:
            return {key: value for key, value in from_obs.items() if key in required}
        return from_obs

    def _minimum_net_edge(self, identity: DerivedPriceEngineItem) -> Decimal | None:
        del identity
        resolved = getattr(self.paper_scan, "settings", None) if self.paper_scan is not None else None
        if resolved is None:
            try:
                resolved = get_settings()
            except Exception:
                resolved = None
        try:
            operator = effective_operator_scanner_settings(resolved)
            return Decimal(str(operator.min_net_edge))
        except Exception:
            pass
        if resolved is None:
            return None
        try:
            return Decimal(str(resolved.min_net_edge))
        except (InvalidOperation, ValueError, TypeError, AttributeError):
            return None

    def _record_outcome(
        self,
        runtime: PriceEngineRuntimeItem,
        outcome: PriceEngineItemStatus,
        result: PriceEngineSliceResult,
    ) -> None:
        row_id = runtime.identity.catalogue_row_id
        if outcome is PriceEngineItemStatus.EVALUATED:
            result.evaluated.append(row_id)
            return
        if outcome is PriceEngineItemStatus.RETRY_WAIT:
            result.retry_wait.append(row_id)
            result.issues.append(
                CollectorIssue(
                    stage=runtime.last_error_stage or "order_book",
                    source_id=row_id,
                    detail=runtime.last_error_detail or PRICE_ENGINE_ITEM_TIMEOUT_REASON,
                )
            )
            return
        if outcome is PriceEngineItemStatus.NOT_STARTED:
            runtime.status = PriceEngineItemStatus.NOT_STARTED
            result.not_started.append(row_id)
            return
        if outcome is PriceEngineItemStatus.DEFERRED:
            runtime.status = PriceEngineItemStatus.DEFERRED
            result.deferred.append(row_id)
            result.provider_capacity_saturated = True
            result.issues.append(
                CollectorIssue(
                    stage="provider_capacity",
                    source_id=row_id,
                    detail=PROVIDER_CAPACITY_SATURATED,
                )
            )
            return
        if outcome is PriceEngineItemStatus.REVALIDATION_NEEDED:
            result.revalidation.append(row_id)
            result.issues.append(
                CollectorIssue(
                    stage="catalogue_revalidation",
                    source_id=row_id,
                    detail=runtime.last_error_detail or CATALOGUE_REVALIDATION_REASON,
                )
            )
            return
        if outcome is PriceEngineItemStatus.SKIPPED:
            result.skipped.append(row_id)
            result.skip_reasons[row_id] = runtime.last_error_detail or CROSS_VENUE_UNAVAILABLE
            result.issues.append(
                CollectorIssue(
                    stage="opportunity_viability",
                    source_id=row_id,
                    detail=runtime.last_error_detail or CROSS_VENUE_UNAVAILABLE,
                )
            )
            return
        result.failed.append(row_id)

    def _fee_snapshot_payload(self, identity: DerivedPriceEngineItem) -> dict[str, Any] | None:
        if self.catalogue_store is None or not identity.kalshi_fee_snapshot_id:
            return None
        snapshot = self.catalogue_store.get_fee_snapshot(identity.kalshi_fee_snapshot_id)
        if snapshot is None or not snapshot.is_known():
            return None
        return {
            "fee_type": snapshot.fee_type,
            "fee_multiplier": snapshot.fee_multiplier,
            "fee_provenance": snapshot.fee_provenance,
            "fee_resolution_status": snapshot.fee_resolution_status,
            "snapshot_id": snapshot.snapshot_id,
        }

    def snapshot(self) -> dict[str, Any]:
        public = self.public_status()
        payload = public.model_dump()
        payload.update(
            {
                "item_count": len(self._items),
                "retry_wait": public.hot.retry_wait + public.background.retry_wait,
                "revalidation_requests": list(self.revalidation_requests),
                "peak_held_slots": dict(self._peak_held_slots),
                "durable_queue": False,
            }
        )
        payload.pop("scan_budget_exhausted", None)
        return payload

    def public_status(self, *, now: datetime | None = None) -> PriceEnginePublicStatus:
        """HOT vs BACKGROUND counters from current in-memory items.

        Does not reconstruct from the catalogue, collector leftovers, or a
        durable queue. ``scan_budget_exhausted`` is never emitted.
        """

        evaluated_at = now or self.now()
        return PriceEnginePublicStatus(
            hot=self._tier_status(PriceEnginePriority.HOT, now=evaluated_at),
            background=self._tier_status(PriceEnginePriority.BACKGROUND, now=evaluated_at),
            durable_queue=False,
            observability_lag=self.observability.lag,
            observability_dropped=self.observability.dropped,
            observability_error=self.observability.last_error,
        )

    def _tier_status(
        self,
        priority: PriceEnginePriority,
        *,
        now: datetime,
    ) -> PriceEngineTierStatus:
        interval = self._hot_interval if priority is PriceEnginePriority.HOT else self._background_interval
        items = [
            item
            for item in self._items.values()
            if item.priority is priority
        ]
        due = 0
        in_flight = 0
        evaluated = 0
        retry_wait = 0
        deferred = 0
        not_started = 0
        revalidation = 0
        persist_failures = 0
        last_error: str | None = None
        for runtime in items:
            if runtime.last_persist_error:
                persist_failures += 1
            if runtime.last_error_detail and last_error is None:
                if runtime.last_error_detail != SCAN_BUDGET_EXHAUSTED_REASON:
                    last_error = runtime.last_error_detail
            if runtime.in_flight or runtime.status is PriceEngineItemStatus.IN_FLIGHT:
                in_flight += 1
                continue
            if runtime.status is PriceEngineItemStatus.RETRY_WAIT or (
                runtime.next_retry_at is not None and now < runtime.next_retry_at
            ):
                retry_wait += 1
                continue
            if runtime.status is PriceEngineItemStatus.DEFERRED:
                deferred += 1
                continue
            if runtime.status is PriceEngineItemStatus.NOT_STARTED:
                not_started += 1
                continue
            if runtime.status is PriceEngineItemStatus.REVALIDATION_NEEDED:
                revalidation += 1
                continue
            if runtime.status is PriceEngineItemStatus.SKIPPED:
                if runtime.last_priced_at is None:
                    continue
                if interval <= 0 or now <= runtime.last_priced_at + timedelta(seconds=interval):
                    continue
                due += 1
                continue
            if runtime.status is PriceEngineItemStatus.EVALUATED:
                if runtime.last_priced_at is None:
                    evaluated += 1
                    continue
                if interval <= 0 or now <= runtime.last_priced_at + timedelta(seconds=interval):
                    evaluated += 1
                else:
                    due += 1
                continue
            due += 1
        operations = dict(self._operation_health.get(priority.value) or {})
        return PriceEngineTierStatus(
            working_set=len(items),
            due=due,
            queued=due,
            in_flight=in_flight,
            evaluated=evaluated,
            evaluated_definition=PRICE_ENGINE_EVALUATED_DEFINITION,
            retry_wait=retry_wait,
            deferred=deferred,
            provider_capacity_saturated=deferred,
            not_started_this_cadence=not_started,
            revalidation_needed=revalidation,
            persist_failures=persist_failures,
            last_error=last_error,
            operation_health=operations,
            venue_health=venue_health_from_operation_health(operations),
        )


def _oldest_retrieval_age(
    retrieved_at: list[datetime],
    *,
    evaluated_at: datetime,
    required: int,
) -> QuoteAgeAssessment:
    """Age from the oldest required retrieval instant. Missing times fail closed."""

    if required <= 0:
        return QuoteAgeAssessment(
            quote_age_ms=None,
            basis="unknown",
            reason="missing_quote_timestamp",
        )
    if len(retrieved_at) < required:
        return QuoteAgeAssessment(
            quote_age_ms=None,
            basis="unknown",
            reason="missing_quote_timestamp",
        )
    oldest = min(retrieved_at)
    return retrieval_quote_age(retrieved_at=oldest, evaluated_at=evaluated_at)


def _fixture_like(identity: DerivedPriceEngineItem) -> Any:
    class _Fixture:
        canonical_event_id = identity.canonical_event_id
        kickoff_utc = identity.kickoff_utc
        in_running = None
        fixture_status = None
        fixture_status_source = None

    return _Fixture()


def _kalshi_tickers(identity: DerivedPriceEngineItem) -> list[str]:
    tickers = [item for item in identity.kalshi_market_tickers if str(item).strip()]
    if tickers:
        return list(dict.fromkeys(tickers))
    derived: list[str] = []
    for outcome in identity.kalshi_outcome_ids:
        ticker = str(outcome.native_id).rsplit(":", 1)[0].strip()
        if ticker and ticker not in derived:
            derived.append(ticker)
    return derived


def _ticker_outcome(identity: DerivedPriceEngineItem, ticker: str) -> str | None:
    needle = str(ticker or "").strip()
    if not needle:
        return None
    for item in identity.kalshi_outcome_ids:
        native = str(item.native_id or "")
        if native == needle or native.startswith(f"{needle}:"):
            return str(item.outcome)
    return None


def _expected_provider_calls(
    *,
    matchbook_ready: bool,
    kalshi_tickers: list[str],
    polymarket_tokens: list[Any],
) -> int:
    return (
        (1 if matchbook_ready else 0)
        + len(kalshi_tickers)
        + len(polymarket_tokens)
    )


def _required_tickers(identity: DerivedPriceEngineItem) -> list[str]:
    required = list(identity.required_outcomes) or required_outcomes_for_key(
        identity.register_canonical_key
    )
    if not required:
        return _kalshi_tickers(identity)
    by_outcome = {item.outcome: item.native_id for item in identity.kalshi_outcome_ids}
    tickers: list[str] = []
    for outcome in required:
        native = by_outcome.get(outcome)
        if native:
            ticker = str(native).rsplit(":", 1)[0].strip()
            if ticker and ticker not in tickers:
                tickers.append(ticker)
    return tickers or _kalshi_tickers(identity)


def _synthetic_matchbook_event(identity: DerivedPriceEngineItem) -> dict[str, Any]:
    kickoff = identity.kickoff_utc or datetime.now(UTC)
    return {
        "id": identity.matchbook_event_id,
        "name": f"{identity.home_canonical or 'Home'} vs {identity.away_canonical or 'Away'}",
        "start": kickoff.isoformat(),
        "sport-name": (
            "American Football"
            if str(identity.register_canonical_key or "").startswith("NFL_")
            or str(identity.competition or "").upper() == "NFL"
            else "Football"
        ),
        "competition-name": identity.competition
        or ("NFL" if str(identity.register_canonical_key or "").startswith("NFL_") else "Premier League"),
        "status": "open",
    }


def _canonical_kalshi_market(identity: DerivedPriceEngineItem) -> CanonicalMarket | None:
    if identity.kickoff_utc is None or not identity.kalshi_event_ticker:
        return None
    family = _family_from_key(identity)
    if family is None:
        return None
    line = None if not identity.line else Decimal(str(identity.line))
    runners = [
        CanonicalRunner(
            source_runner_id=item.native_id,
            outcome=CanonicalOutcome(item.outcome),
            label=item.outcome,
        )
        for item in identity.kalshi_outcome_ids
    ]
    if not runners:
        return None
    event = CanonicalEvent(
        sport=_sport_for_identity(identity),
        competition=identity.competition or "Premier League",
        home_team=identity.home_canonical or "Home",
        away_team=identity.away_canonical or "Away",
        kickoff_utc=identity.kickoff_utc,
        source_venue=VenueName.KALSHI,
        source_event_id=identity.kalshi_event_ticker,
    )
    return CanonicalMarket(
        event=event,
        source_venue=VenueName.KALSHI,
        source_market_id=identity.kalshi_market_tickers[0] if identity.kalshi_market_tickers else identity.kalshi_event_ticker,
        family=family,
        period=FootballPeriod.FULL_TIME,
        line=line,
        settlement=SettlementFingerprint(
            scope=SettlementScope.REGULATION_TIME,
            period=FootballPeriod.FULL_TIME,
            line=line,
            push_possible=False,
            penalties_included=False,
            extra_time_included=False,
        ),
        runners=runners,
    )


def _canonical_polymarket_market(identity: DerivedPriceEngineItem) -> CanonicalMarket | None:
    if identity.kickoff_utc is None or not identity.polymarket_event_id or not identity.polymarket_market_id:
        return None
    family = _family_from_key(identity)
    if family is None:
        return None
    tokens = executable_polymarket_token_ids(
        list(identity.polymarket_token_ids),
        event_id=identity.polymarket_event_id,
        market_id=identity.polymarket_market_id,
        condition_id=identity.polymarket_condition_id,
        required_outcomes=list(identity.required_outcomes)
        or required_outcomes_for_key(identity.register_canonical_key),
    )
    if not tokens:
        return None
    line = None if not identity.line else Decimal(str(identity.line))
    runners = [
        CanonicalRunner(
            source_runner_id=item.native_id,
            outcome=CanonicalOutcome(item.outcome),
            label=item.outcome,
        )
        for item in tokens
    ]
    event = CanonicalEvent(
        sport=_sport_for_identity(identity),
        competition=identity.competition or "Premier League",
        home_team=identity.home_canonical or "Home",
        away_team=identity.away_canonical or "Away",
        kickoff_utc=identity.kickoff_utc,
        source_venue=VenueName.POLYMARKET,
        source_event_id=identity.polymarket_event_id,
    )
    return CanonicalMarket(
        event=event,
        source_venue=VenueName.POLYMARKET,
        source_market_id=identity.polymarket_market_id,
        family=family,
        period=FootballPeriod.FULL_TIME,
        line=line,
        settlement=SettlementFingerprint(
            scope=SettlementScope.UNKNOWN,
            period=FootballPeriod.FULL_TIME,
            line=line,
            push_possible=False,
            penalties_included=False,
            extra_time_included=None,
        ),
        runners=runners,
    )


def _sport_for_identity(identity: DerivedPriceEngineItem) -> str:
    if str(identity.register_canonical_key or "").startswith("NFL_"):
        from sports_hedge.nfl.constants import NFL_SPORT

        return NFL_SPORT
    if str(identity.competition or "").upper() == "NFL":
        from sports_hedge.nfl.constants import NFL_SPORT

        return NFL_SPORT
    return "football"


def _family_from_key(identity: DerivedPriceEngineItem) -> MarketFamily | None:
    key = identity.register_canonical_key
    if key == "MATCH_RESULT_FT":
        return MarketFamily.MATCH_RESULT
    if key == "BTTS_FT":
        return MarketFamily.BOTH_TEAMS_TO_SCORE
    if key == "FTTS_FT":
        return MarketFamily.FIRST_TEAM_TO_SCORE
    if key.startswith("TOTAL_GOALS_FT:"):
        return MarketFamily.TOTAL_GOALS
    if key == "NFL_GAME_WINNER_FT":
        return MarketFamily.GAME_WINNER
    if key.startswith("NFL_POINT_SPREAD_FT:"):
        return MarketFamily.POINT_SPREAD
    if key.startswith("NFL_TOTAL_POINTS_FT:"):
        return MarketFamily.TOTAL_POINTS
    if identity.family:
        try:
            return MarketFamily(identity.family)
        except ValueError:
            return None
    return None


def _decision_is_interesting(decision: PaperScanDecision | None) -> bool:
    """Triggered Min Net Arb or 0.50pp net proximity. Broader than paper entry.

    Uses `current_net_edge` (`decision_net_edge`) versus `trigger_net_edge`
    (`decision.minimum_net_edge`). Does not hard-code zero or gross edge.
    """

    if decision is None:
        return False
    edge = decision_net_edge(decision)
    trigger = decision.minimum_net_edge
    if edge is None or trigger is None:
        return False
    if qualifies_min_net_arb(edge, trigger):
        return True
    return is_net_proximity_hot(edge, trigger)


def _overlay_decision_inventory(
    rows: list[FixtureMarketInventoryRow],
    *,
    identity: DerivedPriceEngineItem,
    matchbook_obs: VenueMarketObservation,
    kalshi_obs: VenueMarketObservation,
    decision: PaperScanDecision | None,
) -> list[FixtureMarketInventoryRow]:
    """Keep Issue #200 promotion truth even when greedy pairing is incomplete."""

    if decision is None:
        return rows
    edge = decision_net_edge(decision)
    is_arb = decision_is_solver_arbitrage(decision)
    if edge is None or edge <= 0:
        if not is_arb:
            return rows
        edge = Decimal("0.01")
    comparable = [
        row
        for row in rows
        if inventory_is_comparable_opportunity(row.comparison_status)
        and row.matchbook is not None
        and row.kalshi is not None
    ]
    if comparable:
        for row in comparable:
            row.current_net_edge = edge
            row.trigger_net_edge = decision.minimum_net_edge
            row.solver_is_arbitrage = is_arb or (edge is not None and edge > 0)
            row.entered_solver = True
            if row.matchbook is not None and row.matchbook.quote_age_ms is None:
                row.matchbook.quote_age_ms = matchbook_obs.quote_age_ms or 0
            if row.kalshi is not None and row.kalshi.quote_age_ms is None:
                row.kalshi.quote_age_ms = kalshi_obs.quote_age_ms or 0
        return rows
    return [*rows, _decision_inventory_row(identity, matchbook_obs, kalshi_obs, decision, edge, is_arb)]


def _decision_inventory_row(
    identity: DerivedPriceEngineItem,
    matchbook_obs: VenueMarketObservation,
    kalshi_obs: VenueMarketObservation,
    decision: PaperScanDecision,
    edge: Decimal,
    is_arb: bool,
) -> FixtureMarketInventoryRow:
    family = identity.family or matchbook_obs.market.family.value
    period = identity.period or matchbook_obs.market.period.value

    def _facts(observation: VenueMarketObservation) -> VenueMarketFacts:
        backs = [
            VenueQuoteFact(
                outcome=book.outcome.value,
                decimal_odds=None if book.best_back is None else book.best_back.decimal_odds,
                size_at_touch=None if book.best_back is None else book.best_back.available_stake,
            )
            for book in observation.outcome_books
        ]
        return VenueMarketFacts(
            venue=observation.venue,
            source_event_id=observation.market.event.source_event_id,
            source_market_id=observation.market.source_market_id,
            family=family,
            period=period,
            settlement_key="regulation_time|full_time",
            settlement_complete=True,
            best_backs=backs,
            quote_age_ms=observation.quote_age_ms if observation.quote_age_ms is not None else 0,
            quote_age_basis="retrieval",
            native_currency=observation.native_currency,
        )

    return FixtureMarketInventoryRow(
        display_name=family,
        family=family,
        period=period,
        comparison_status=InventoryComparisonStatus.MATCHED_EQUIVALENT,
        entered_solver=True,
        solver_model=decision.solver_model or "strict_complete_set",
        current_net_edge=edge,
        trigger_net_edge=decision.minimum_net_edge,
        solver_is_arbitrage=is_arb or edge > 0,
        matchbook=_facts(matchbook_obs),
        kalshi=_facts(kalshi_obs),
        last_scanned_at=matchbook_obs.observed_at,
    )


# Imported for tests that assert these constants stay honest.
DEFAULT_HOT_CADENCE_SECONDS = DEFAULT_HOT_INTERVAL_SECONDS
DEFAULT_BACKGROUND_CADENCE_SECONDS = DEFAULT_BACKGROUND_INTERVAL_SECONDS
_ = (InvalidOperation, SCAN_BUDGET_EXHAUSTED_REASON, DEFERRED_STATUS)
