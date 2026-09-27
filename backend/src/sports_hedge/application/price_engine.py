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

BACKGROUND and HOT schedule those exact-ID reads by provider stage and
coalesce identical requests inside one slice. Each slice binds its own
coalescer. ACTIVE TRADE stays sequential on ``_price_item``.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from inspect import isawaitable
from logging import getLogger
from time import monotonic
from typing import Any

from sports_hedge.application.fixture_sport import resolve_discovered_fixture_sport
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
from sports_hedge.application.cycle_diagnostics import CycleDiagnosticAccumulator
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
    assess_identity_viability,
    build_viability_evidence,
    market_relationship_not_collected,
    catalogue_ready_venues,
    venue_blocked_for_identity,
    get_opportunity_viability_cache,
    reset_opportunity_viability_cache,
)
from sports_hedge.application.adaptive_scheduler import (
    SchedulerWork,
    order_scheduler_work,
)
from sports_hedge.application.coverage_cursor import CoverageCursor
from sports_hedge.application.provider_access import (
    HEALTH_CAPACITY_SATURATED,
    HEALTH_DEFERRED,
    HEALTH_MARKET_TIMEOUT,
    HEALTH_OK,
    HEALTH_RATE_LIMITED,
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
    EVICTION_NO_CURRENT_EQUIVALENT_MARKETS_POST_KICKOFF,
    ScanLane,
    classify_scan_lane,
)
from sports_hedge.arbitrage.watchlist.economics import (
    distance_to_trigger_pp,
    is_net_proximity_hot,
    qualifies_min_net_arb,
)
from sports_hedge.arbitrage.min_net_threshold import catalogue_market_scope
from sports_hedge.arbitrage.watchlist.models import (
    format_hot_promotion_detail,
    hot_promotion_opportunity_id,
)
from sports_hedge.lifecycle.execution_miss import (
    REASON_RECENTLY_QUALIFYING_EXECUTION_MISS,
    execution_miss_hot_active,
)
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
from sports_hedge.venues.rate_limit import ProviderRateLimitedError

PRICE_ENGINE_RETRY_BACKOFF_SECONDS = (2.0, 5.0, 10.0)
PRICE_ENGINE_ITEM_TIMEOUT_REASON = "order_book_timeout after 8s"
CATALOGUE_REVALIDATION_REASON = HOT_REVALIDATION_NEEDED_REASON
NOT_STARTED_THIS_CADENCE = "not_started_this_cadence"
PROVIDER_CAPACITY_SATURATED = HEALTH_CAPACITY_SATURATED
DEFERRED_STATUS = HEALTH_DEFERRED
SCAN_BUDGET_EXHAUSTED_REASON = "scan_budget_exhausted"
PRICE_ENGINE_PERSIST_STAGE = "persist_capture"

LOGGER = getLogger(__name__)

# Scheduler wakes that must not be stored as a normal HOT pricing cycle.
HOT_IDLE_SCHEDULER_DISPOSITIONS = frozenset(
    {
        "pass_waiting_target",
        "retry_waiting",
        "no_hot_roster",
        "no_runnable_hot_work",
    }
)
_SLICE_DIAGNOSTICS: ContextVar[CycleDiagnosticAccumulator | None] = ContextVar(
    "price_engine_slice_diagnostics",
    default=None,
)
# Price-2 records slot wait and I/O here. Child provider tasks share the list.
_EXECUTION_REPRICE_CALLS: ContextVar[list[dict[str, Any]] | None] = ContextVar(
    "execution_reprice_calls",
    default=None,
)


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


@dataclass(slots=True)
class ExactIdPricingPrep:
    """Exact-ID inputs for one catalogue row. No discovery."""

    lane: str
    active_lane: bool
    matchbook_ready: bool
    kalshi_ready: bool
    polymarket_ready: bool
    pm_tokens: list[Any]
    skip_bound: bool


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
    viable_venue_count: int = 0
    skip_expensive_work: bool = False
    viability_reason: str | None = None
    near_threshold: bool = False
    qualifying: bool = False
    execution_miss_sticky: bool = False
    last_priority_decision: dict[str, Any] | None = None
    provider_work_started: bool = False

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
    promotion_reason: str | None = None
    detail: str | None = None


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
    diagnostic: dict[str, Any] | None = None
    # Slice-local exact-ID schedule counters. Duplicate avoidance is
    # ``coalesced_provider_calls`` only. Zero unless this slice used the
    # shared staged planner.
    issued_provider_calls: int = 0
    coalesced_provider_calls: int = 0
    provider_stage_calls: dict[str, int] = field(default_factory=dict)
    pricing_call_shape: str = ""
    hot_coverage: dict[str, Any] | None = None

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
        self._execution_miss_until: dict[str, datetime] = {}
        self._execution_miss_version: dict[str, int] = {}
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
        self._execution_reprice_open: set[str] = set()
        self._last_slice_not_started: dict[str, int] = {
            PriceEnginePriority.HOT.value: 0,
            PriceEnginePriority.BACKGROUND.value: 0,
        }
        self._selected_competition_codes: frozenset[str] | None = None
        self._exempt_event_ids: frozenset[str] = frozenset()
        self.viability_cache = get_opportunity_viability_cache()
        self._coverage: dict[str, CoverageCursor] = {
            PriceEnginePriority.HOT.value: CoverageCursor(lane=PriceEnginePriority.HOT.value),
            PriceEnginePriority.BACKGROUND.value: CoverageCursor(
                lane=PriceEnginePriority.BACKGROUND.value
            ),
        }
        self._coverage_claim_limit = {
            PriceEnginePriority.HOT.value: 64,
            PriceEnginePriority.BACKGROUND.value: 24,
        }
        self._membership_observed = False

    def now(self) -> datetime:
        return self._clock()

    def set_hot_reprice_after_seconds(self, seconds: int) -> None:
        """Apply the HOT per-row reprice age without reconstructing work."""

        self._hot_interval = int(seconds)

    def set_background_interval_seconds(self, seconds: int) -> None:
        """Apply the BACKGROUND per-row reprice age without reconstructing work."""

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
        self._membership_observed = True
        return rebuilt

    def restart(self) -> list[PriceEngineRuntimeItem]:
        """Process restart: reconstruct ACTIVE work and reset short backoff."""

        self._items.clear()
        self._promoted_hot_rows.clear()
        self._promoted_hot_ids.clear()
        self._hot_promotion_episodes.clear()
        self._execution_miss_until.clear()
        self._execution_miss_version.clear()
        if self.fixture_state is not None:
            self.fixture_state.clear_execution_miss_hot()
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
        ``FixtureCurrentStateStore`` upsert cannot grant HOT priority.
        A post-kickoff market-closure tombstone may revoke HOT priority only:
        the fixture is no longer a useful live pricing fixture. Cadence is
        unchanged, and provider ``in_running`` is not rewritten.
        """

        if self._post_kickoff_market_closed(identity.canonical_event_id):
            return PriceEnginePriority.BACKGROUND
        fixture = _fixture_like(identity)
        lifecycle = classify_scan_lane(fixture, self.now())
        if lifecycle is ScanLane.HOT:
            return PriceEnginePriority.HOT
        if lifecycle is ScanLane.DROP:
            return PriceEnginePriority.BACKGROUND
        if identity.canonical_event_id in self._promoted_hot_ids:
            return PriceEnginePriority.HOT
        return PriceEnginePriority.BACKGROUND

    def _post_kickoff_market_closed(self, canonical_event_id: str) -> bool:
        store = self.fixture_state
        if store is None or not canonical_event_id:
            return False
        tombstone = store.tombstone_for(canonical_event_id)
        return (
            tombstone is not None
            and tombstone.reason == EVICTION_NO_CURRENT_EQUIVALENT_MARKETS_POST_KICKOFF
        )

    def coverage_cursor(self, priority: PriceEnginePriority) -> CoverageCursor:
        return self._coverage[priority.value]

    def restore_coverage_cursor(self, priority: PriceEnginePriority, payload: dict | None) -> None:
        self._coverage[priority.value] = CoverageCursor.from_resume(priority.value, payload)

    def due_items(
        self,
        priority: PriceEnginePriority,
        *,
        now: datetime | None = None,
    ) -> list[PriceEngineRuntimeItem]:
        """Claim the next round-robin region. Age gates do not rewind the cursor."""

        evaluated = now or self.now()
        membership, blocked = self._lane_membership(priority, evaluated)
        by_id = {item.identity.catalogue_row_id: item for item in membership}
        cursor = self._coverage[priority.value]
        claimed = cursor.claim(
            [item.identity.catalogue_row_id for item in membership],
            blocked=blocked,
            limit=self._coverage_claim_limit[priority.value],
            now=evaluated,
        )
        due: list[PriceEngineRuntimeItem] = []
        for row_id in claimed:
            runtime = by_id.get(row_id)
            if runtime is None or row_id in blocked:
                continue
            runtime.status = PriceEngineItemStatus.DUE
            due.append(runtime)
        if priority is PriceEnginePriority.HOT:
            self.note_hot_provider_demand(evaluated)
        return due

    def next_runnable_at(
        self,
        priority: PriceEnginePriority,
        *,
        now: datetime | None = None,
    ) -> datetime:
        """When this lane can claim another row.

        A runnable unvisited row is due immediately. If the only remaining
        rows are in retry or in flight, return the earliest retry instant or
        a short bounded delay. That delay is process-local; it is not a queue.
        """

        evaluated = now or self.now()
        cursor = self._coverage[priority.value]
        if cursor.hold_until is not None and evaluated < cursor.hold_until:
            return cursor.hold_until
        membership, blocked = self._lane_membership(priority, evaluated, refresh=False)
        ordered = [item.identity.catalogue_row_id for item in membership]
        if cursor._unvisited_unblocked(ordered, blocked):
            return evaluated
        earliest: datetime | None = None
        in_flight = False
        for runtime in membership:
            row_id = runtime.identity.catalogue_row_id
            if row_id in cursor.visited or row_id not in blocked:
                continue
            if runtime.in_flight:
                in_flight = True
            retry_at = runtime.next_retry_at
            if retry_at is not None and evaluated < retry_at and (
                earliest is None or retry_at < earliest
            ):
                earliest = retry_at
        if earliest is not None:
            return earliest
        if in_flight or blocked:
            return evaluated + timedelta(milliseconds=250)
        if cursor.hold_until is not None:
            return cursor.hold_until
        return evaluated

    def note_hot_provider_demand(self, now: datetime | None = None) -> bool:
        """Publish whether HOT can request a provider slot.

        A HOT roster row is not demand by itself. Retry-wait, in-flight-only
        blockers, and viability skips that will not call a provider leave the
        idle ACTIVE reserve in place. In-flight HOT work and a row that can
        request a provider call count. This walks process memory only.
        """

        active = self._hot_has_provider_demand(now or self.now())
        access = self.provider_access
        if access is not None:
            access.set_hot_provider_demand(active)
        return active

    def _hot_has_provider_demand(self, evaluated: datetime) -> bool:
        if not self._items:
            return False
        membership, blocked = self._lane_membership(
            PriceEnginePriority.HOT, evaluated, refresh=False
        )
        cursor = self.coverage_cursor(PriceEnginePriority.HOT)
        holding = cursor.hold_until is not None and evaluated < cursor.hold_until
        for runtime in membership:
            if runtime.in_flight:
                return True
            if holding:
                continue
            row_id = runtime.identity.catalogue_row_id
            if row_id in blocked or runtime.skip_expensive_work:
                continue
            if row_id in cursor.visited and row_id not in cursor.pending_inserts:
                continue
            return True
        return False

    def peek_hot_idle_reason(self, now: datetime | None = None) -> str | None:
        """Why a HOT wake should not be recorded as a pricing cycle.

        ``None`` means the slice should run. Idle reasons are scheduler
        heartbeats: target wait, retry/block, or an empty roster. Provider
        deferrals are not idle; those slices still run and are recorded.
        """

        evaluated = now or self.now()
        self.note_hot_provider_demand(evaluated)
        if not self._membership_observed:
            return None
        cursor = self.coverage_cursor(PriceEnginePriority.HOT)
        if cursor.hold_until is not None and evaluated < cursor.hold_until:
            return "pass_waiting_target"
        membership, blocked = self._lane_membership(
            PriceEnginePriority.HOT, evaluated, refresh=False
        )
        if not membership:
            return "no_hot_roster"
        claimable = False
        blocked_unvisited = False
        for runtime in membership:
            row_id = runtime.identity.catalogue_row_id
            if row_id in cursor.visited:
                continue
            if row_id in blocked:
                blocked_unvisited = True
                continue
            claimable = True
            break
        if claimable:
            return None
        if blocked_unvisited:
            return "retry_waiting"
        if cursor.hold_until is not None and evaluated >= cursor.hold_until:
            return None
        if cursor.pass_started_at is not None and cursor.hold_until is None:
            return None
        return "no_runnable_hot_work"

    def _lane_membership(
        self,
        priority: PriceEnginePriority,
        evaluated: datetime,
        *,
        refresh: bool = True,
    ) -> tuple[list[PriceEngineRuntimeItem], set[str]]:
        """Current lane rows, including rows that cannot run yet."""

        membership: list[PriceEngineRuntimeItem] = []
        blocked: set[str] = set()
        for runtime in self._items.values():
            runtime.priority = self.classify_priority(runtime.identity)
            if runtime.priority is not priority:
                continue
            row_id = runtime.identity.catalogue_row_id
            waiting = runtime.in_flight or (
                runtime.next_retry_at is not None and evaluated < runtime.next_retry_at
            )
            if waiting:
                blocked.add(row_id)
            elif refresh:
                self._refresh_scheduler_signals(runtime)
            membership.append(runtime)
        if priority is PriceEnginePriority.HOT:
            membership = _interleave_hot_fixtures(membership)
        else:
            membership.sort(key=lambda item: item.identity.catalogue_row_id)
        return membership, blocked

    def _refresh_scheduler_signals(self, runtime: PriceEngineRuntimeItem) -> None:
        identity = runtime.identity
        viability = assess_identity_viability(
            identity,
            cache=self.viability_cache,
            active_event_ids=self._exempt_event_ids,
        )
        runtime.viable_venue_count = viability.viable_venue_count
        runtime.skip_expensive_work = bool(viability.skip_expensive_work)
        runtime.viability_reason = viability.reason

    def _interval_for(self, runtime: PriceEngineRuntimeItem) -> int:
        if runtime.priority is PriceEnginePriority.HOT:
            return self._hot_interval
        return self._background_interval

    def _required_venues(self, runtime: PriceEngineRuntimeItem) -> tuple[VenueName, ...]:
        ready = catalogue_ready_venues(runtime.identity)
        return tuple(
            venue
            for venue in ready
            if not venue_blocked_for_identity(
                self.viability_cache,
                runtime.identity.canonical_event_id,
                venue,
                runtime.identity,
            )
        )

    def scheduler_work_for(
        self,
        runtime: PriceEngineRuntimeItem,
        *,
        lane: str | None = None,
        seq: int = 0,
        now: datetime | None = None,
    ) -> SchedulerWork:
        evaluated = now or self.now()
        interval = max(0, int(self._interval_for(runtime)))
        last = runtime.last_priced_at
        due_at = evaluated if last is None else last + timedelta(seconds=interval)
        deadline_at = due_at + timedelta(seconds=interval if interval > 0 else 0)
        now_ts = evaluated.timestamp()
        resolved_lane = lane or (
            ScanLane.HOT.value
            if runtime.priority is PriceEnginePriority.HOT
            else PRICE_ENGINE_BACKGROUND_LANE
        )
        kickoff = runtime.identity.kickoff_utc
        post_kickoff_hot = (
            runtime.priority is PriceEnginePriority.HOT
            and kickoff is not None
            and kickoff <= evaluated
        )
        return SchedulerWork(
            lane=resolved_lane,
            work_id=runtime.identity.catalogue_row_id,
            viable_venue_count=runtime.viable_venue_count,
            skip_expensive_work=runtime.skip_expensive_work,
            viability_reason=runtime.viability_reason,
            viability_assessed=True,
            near_threshold=runtime.near_threshold,
            qualifying=runtime.qualifying,
            in_play=post_kickoff_hot,
            required_venues=self._required_venues(runtime),
            due_mono=due_at.timestamp(),
            deadline_mono=deadline_at.timestamp(),
            cadence_seconds=float(interval),
            seq=seq,
            wait_age_ms=max(0, int((evaluated - due_at).total_seconds() * 1000))
            if evaluated >= due_at
            else 0,
            now_mono=now_ts,
        )

    def _order_due_items(
        self,
        due: list[PriceEngineRuntimeItem],
        *,
        now: datetime,
    ) -> list[PriceEngineRuntimeItem]:
        if len(due) <= 1:
            return due
        access = self.provider_access
        pressure = access.pressure_by_venue() if access is not None else None
        work_items = [
            self.scheduler_work_for(runtime, seq=index, now=now)
            for index, runtime in enumerate(due)
        ]
        ordered = order_scheduler_work(work_items, pressure_by_venue=pressure)
        by_id = {runtime.identity.catalogue_row_id: runtime for runtime in due}
        ranked: list[PriceEngineRuntimeItem] = []
        for work, decision in ordered:
            runtime = by_id.get(work.work_id)
            if runtime is None:
                continue
            runtime.last_priority_decision = decision.as_dict()
            ranked.append(runtime)
        return ranked

    def _saved_calls_for(self, runtime: PriceEngineRuntimeItem) -> int:
        identity = runtime.identity
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
        return _expected_provider_calls(
            matchbook_ready=matchbook_ready,
            kalshi_tickers=_kalshi_tickers(identity) if kalshi_ready else [],
            polymarket_tokens=pm_tokens if pm_tokens else [],
        )

    def _record_item_deadline_miss(self, runtime: PriceEngineRuntimeItem) -> None:
        access = self.provider_access
        if access is None:
            return
        venues = self._required_venues(runtime) or (VenueName.MATCHBOOK,)
        lane = (
            ScanLane.HOT.value
            if runtime.priority is PriceEnginePriority.HOT
            else PRICE_ENGINE_BACKGROUND_LANE
        )
        access.record_deadline_miss(venues[0], lane=lane)

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
        if priority is PriceEnginePriority.HOT:
            self.note_hot_provider_demand(evaluated)
        hot_roster_rows = 0
        hot_roster_fixtures = 0
        if priority is PriceEnginePriority.HOT:
            roster, _blocked = self._lane_membership(priority, evaluated, refresh=False)
            hot_roster_rows = len(roster)
            hot_roster_fixtures = len(
                {item.identity.canonical_event_id for item in roster}
            )
        due = self.due_items(priority, now=evaluated)
        claimed_ids = [item.identity.catalogue_row_id for item in due]
        result = PriceEngineSliceResult()
        started_mono = monotonic()
        diagnostics = CycleDiagnosticAccumulator()
        diagnostics.note_slice_budget(
            slice_wall_seconds=slice_wall_seconds,
            worker_limit=self._slice_worker_limit(),
        )
        diag_token = _SLICE_DIAGNOSTICS.set(diagnostics)
        deadline = (
            None
            if slice_wall_seconds is None
            else started_mono + float(slice_wall_seconds)
        )

        def remaining() -> float | None:
            if deadline is None:
                return None
            return deadline - monotonic()

        expensive: list[PriceEngineRuntimeItem] = []
        for runtime in due:
            if runtime.skip_expensive_work and len(catalogue_ready_venues(runtime.identity)) >= 2:
                saved = self._saved_calls_for(runtime)
                outcome = self._skip_item(
                    runtime,
                    result,
                    reason=runtime.viability_reason or CROSS_VENUE_UNAVAILABLE,
                    saved_calls=saved,
                    viable_count=runtime.viable_venue_count,
                )
                self._record_outcome(runtime, outcome, result)
            else:
                expensive.append(runtime)

        pending = deque(expensive)
        self._slice_remaining = remaining
        try:
            if remaining() is not None and float(remaining() or 0) <= 0:
                for runtime in expensive:
                    runtime.status = PriceEngineItemStatus.NOT_STARTED
                    result.not_started.append(runtime.identity.catalogue_row_id)
                    diagnostics.note_terminal(
                        "not_started",
                        runtime.identity.catalogue_row_id,
                        NOT_STARTED_THIS_CADENCE,
                    )
                    self._record_item_deadline_miss(runtime)
            elif priority is PriceEnginePriority.BACKGROUND and expensive:
                from sports_hedge.application.background_exact_id_planner import (
                    run_background_exact_id_slice,
                )
                from sports_hedge.application.exact_id_coalesce import (
                    ExactIdSliceCoalescer,
                    bind_exact_id_coalescer,
                )

                coalescer = ExactIdSliceCoalescer()
                with bind_exact_id_coalescer(coalescer):
                    await run_background_exact_id_slice(
                        self,
                        expensive,
                        result,
                        remaining=remaining,
                    )
                result.issued_provider_calls = coalescer.issued_provider_calls
                result.coalesced_provider_calls = coalescer.coalesced_provider_calls
                result.provider_stage_calls = dict(coalescer.provider_stage_calls)
                result.pricing_call_shape = "provider_centric_staged_exact_id"
                diagnostics.note_slice_budget(
                    slice_wall_seconds=slice_wall_seconds,
                    worker_limit=self._slice_worker_limit(),
                )
            elif priority is PriceEnginePriority.HOT and expensive:
                from sports_hedge.application.hot_latency_exact_id import (
                    run_hot_latency_exact_id_slice,
                )

                await run_hot_latency_exact_id_slice(
                    self,
                    expensive,
                    result,
                    remaining=remaining,
                )
                diagnostics.note_slice_budget(
                    slice_wall_seconds=slice_wall_seconds,
                    worker_limit=self._slice_worker_limit(),
                )
            else:
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
                            self._record_outcome(
                                runtime, PriceEngineItemStatus.NOT_STARTED, result
                            )
                            self._record_item_deadline_miss(runtime)
                            continue
                        try:
                            outcome = await self._price_item(runtime, result)
                            self._record_outcome(runtime, outcome, result)
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            diagnostics.note_worker_error(exc)
                            runtime.last_error_stage = "item_exception"
                            runtime.last_error_detail = f"{type(exc).__name__}: {exc}"[:200]
                            raise

                worker_n = min(self._slice_worker_limit(), len(expensive))
                diagnostics.note_slice_budget(
                    slice_wall_seconds=slice_wall_seconds,
                    worker_limit=worker_n,
                )
                if worker_n > 0:
                    gathered = await asyncio.gather(
                        *(asyncio.create_task(_worker()) for _ in range(worker_n)),
                        return_exceptions=True,
                    )
                    for item in gathered:
                        if isinstance(item, BaseException) and not isinstance(
                            item, asyncio.CancelledError
                        ):
                            diagnostics.note_worker_error(item)
                while pending:
                    runtime = pending.popleft()
                    runtime.status = PriceEngineItemStatus.NOT_STARTED
                    result.not_started.append(runtime.identity.catalogue_row_id)
                    diagnostics.note_terminal(
                        "not_started",
                        runtime.identity.catalogue_row_id,
                        NOT_STARTED_THIS_CADENCE,
                    )
                    self._record_item_deadline_miss(runtime)
        finally:
            self._slice_remaining = None
            self._close_slice_diagnostic(
                result,
                diagnostics,
                diag_token,
                priority=priority,
                due=len(due),
                started_mono=started_mono,
            )
            self._release_unstarted_claims(priority, claimed_ids, result)
            self.note_hot_provider_demand(evaluated)
            if priority is PriceEnginePriority.HOT:
                result.hot_coverage = self._hot_coverage_snapshot(
                    claimed_ids=claimed_ids,
                    due_count=len(due),
                    result=result,
                    roster_rows=hot_roster_rows,
                    roster_fixtures=hot_roster_fixtures,
                    slot_wait_ms=diagnostics.slot_wait_ms_sum,
                )
                if isinstance(result.diagnostic, dict):
                    result.diagnostic["hot_coverage"] = dict(result.hot_coverage)
        self._last_slice_not_started[priority.value] = len(result.not_started)
        result.operation_health = dict(self._operation_health.get(priority.value) or {})
        result.venue_health = venue_health_from_operation_health(result.operation_health)
        result.scan_budget_exhausted = False
        return result

    def _release_unstarted_claims(
        self,
        priority: PriceEnginePriority,
        claimed_ids: list[str],
        result: PriceEngineSliceResult,
    ) -> None:
        """Claimed rows that never started stay in the current coverage pass."""

        if not claimed_ids:
            return
        honest = set(result.evaluated)
        honest.update(result.failed)
        honest.update(result.deferred)
        honest.update(result.retry_wait)
        honest.update(result.skipped)
        honest.update(result.revalidation)
        unstarted = [row_id for row_id in claimed_ids if row_id not in honest]
        self._coverage[priority.value].release_unstarted(unstarted)

    def _hot_scheduler_disposition(
        self,
        *,
        claimed_ids: list[str],
        result: PriceEngineSliceResult,
        roster_rows: int,
    ) -> str:
        """Classify a finished HOT slice for cycle history.

        Provider-capacity misses stay visible. A wake that claimed nothing
        and started nothing is a scheduler heartbeat, not a pricing cycle.
        """

        if result.deferred or result.not_started:
            return "provider_capacity"
        if (
            claimed_ids
            or result.evaluated
            or result.failed
            or result.skipped
            or result.revalidation
        ):
            return "hot_pricing"
        now = self.now()
        cursor = self.coverage_cursor(PriceEnginePriority.HOT)
        if cursor.hold_until is not None and now < cursor.hold_until:
            return "pass_waiting_target"
        if roster_rows <= 0:
            return "no_hot_roster"
        _membership, blocked = self._lane_membership(
            PriceEnginePriority.HOT, now, refresh=False
        )
        if blocked:
            return "retry_waiting"
        return "no_runnable_hot_work"

    def _hot_coverage_snapshot(
        self,
        *,
        claimed_ids: list[str],
        due_count: int,
        result: PriceEngineSliceResult,
        roster_rows: int,
        roster_fixtures: int,
        slot_wait_ms: int,
    ) -> dict[str, Any]:
        """Measured HOT cycle counts. Missing facts are omitted, not invented."""

        started_ids = [
            row_id
            for row_id in claimed_ids
            if (runtime := self.item(row_id)) is not None and runtime.provider_work_started
        ]
        evaluated_ids = list(result.evaluated)
        started_fixtures = {
            runtime.identity.canonical_event_id
            for row_id in started_ids
            if (runtime := self.item(row_id)) is not None
        }
        evaluated_fixtures = {
            runtime.identity.canonical_event_id
            for row_id in evaluated_ids
            if (runtime := self.item(row_id)) is not None
        }
        missed_row_ids = list(dict.fromkeys([*result.not_started, *result.deferred]))
        missed_fixtures = {
            runtime.identity.canonical_event_id
            for row_id in missed_row_ids
            if (runtime := self.item(row_id)) is not None
            and runtime.identity.canonical_event_id not in evaluated_fixtures
        }
        cursor = self.coverage_cursor(PriceEnginePriority.HOT).snapshot()
        coverage: dict[str, Any] = {
            "roster_fixtures": int(roster_fixtures),
            "roster_rows": int(roster_rows),
            "due_rows": int(due_count),
            "claimed_rows": len(claimed_ids),
            "started_rows": len(started_ids),
            "evaluated_rows": len(evaluated_ids),
            "skipped_viability": len(result.skipped),
            "deferred_rows": len(result.deferred),
            "retry_wait_rows": len(result.retry_wait),
            "not_started_this_cadence": len(result.not_started),
            "fixtures_touched": len(started_fixtures),
            "fixtures_evaluated": len(evaluated_fixtures),
            "fixtures_missed_capacity": len(missed_fixtures),
            "cursor_pass_number": cursor.get("pass_number"),
            "cursor_position": cursor.get("position"),
            "cursor_total": cursor.get("catalogue_rows"),
            "hot_slot_wait_ms": int(slot_wait_ms),
            "scheduler_disposition": self._hot_scheduler_disposition(
                claimed_ids=claimed_ids,
                result=result,
                roster_rows=roster_rows,
            ),
        }
        missed_rows = coverage["not_started_this_cadence"] + coverage["deferred_rows"]
        coverage["summary"] = (
            f"{coverage['roster_fixtures']} HOT fixtures · "
            f"{coverage['roster_rows']} rows · "
            f"{coverage['fixtures_touched']} fixtures touched · "
            f"{coverage['evaluated_rows']} evaluated · "
            f"{missed_rows} rows deadline/capacity missed"
        )
        return coverage

    def _close_slice_diagnostic(
        self,
        result: PriceEngineSliceResult,
        diagnostics: CycleDiagnosticAccumulator,
        diag_token: Any,
        *,
        priority: PriceEnginePriority,
        due: int,
        started_mono: float,
    ) -> None:
        if result.diagnostic is None:
            diagnostics.note_saved_calls(
                result.saved_provider_calls,
                result.skipped_provider_calls,
            )
            limits = {}
            access = self.provider_access
            if access is not None:
                limits = {venue.value: int(limit) for venue, limit in access.limits.items()}
            result.diagnostic = diagnostics.finish(
                lane=priority.value,
                wall_ms=max(0, int((monotonic() - started_mono) * 1000)),
                due=due,
                evaluated=len(result.evaluated),
                skipped=len(result.skipped),
                revalidation=len(result.revalidation),
                failed=len(result.failed),
                retry_wait=len(result.retry_wait),
                deferred=len(result.deferred),
                not_started=len(result.not_started),
                decisions=len(result.decisions),
                qualifying=sum(
                    1 for decision in result.decisions if decision_is_solver_arbitrage(decision)
                ),
                promotions=len(result.promotions),
                provider_limits=limits,
                coalesced_provider_calls=result.coalesced_provider_calls,
                issued_provider_calls=result.issued_provider_calls,
                pricing_call_shape=result.pricing_call_shape,
            )
        try:
            _SLICE_DIAGNOSTICS.reset(diag_token)
        except ValueError:
            return

    def _mark_price_item_started(
        self,
        runtime: PriceEngineRuntimeItem,
        *,
        lane: str | None = None,
    ) -> str:
        runtime.in_flight = True
        runtime.provider_work_started = False
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
        return lane

    def _prepare_exact_id_pricing(
        self,
        runtime: PriceEngineRuntimeItem,
        result: PriceEngineSliceResult,
        *,
        lane: str,
    ) -> ExactIdPricingPrep | PriceEngineItemStatus:
        """Validate exact catalogue IDs and viability. Does not call providers."""

        identity = runtime.identity
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
        return ExactIdPricingPrep(
            lane=lane,
            active_lane=active_lane,
            matchbook_ready=matchbook_ready,
            kalshi_ready=kalshi_ready,
            polymarket_ready=polymarket_ready,
            pm_tokens=list(pm_tokens),
            skip_bound=active_lane or bool(viability.active_trade_override),
        )

    async def _load_matchbook_stage(
        self,
        runtime: PriceEngineRuntimeItem,
        result: PriceEngineSliceResult,
        *,
        prep: ExactIdPricingPrep,
    ) -> tuple[RetrievedVenuePayload, dict[str, Decimal]] | PriceEngineItemStatus:
        identity = runtime.identity
        matchbook_payload = await self._refresh_matchbook(runtime, lane=prep.lane)
        if isinstance(matchbook_payload, PriceEngineItemStatus):
            if runtime.last_error_detail and ":gone" in str(runtime.last_error_detail):
                remaining = _expected_provider_calls(
                    matchbook_ready=False,
                    kalshi_tickers=_kalshi_tickers(identity) if prep.kalshi_ready else [],
                    polymarket_tokens=prep.pm_tokens if prep.polymarket_ready else [],
                )
                result.skipped_provider_calls += remaining
                result.saved_provider_calls += remaining
                result.viable_venue_count = assess_identity_viability(
                    identity,
                    cache=self.viability_cache,
                    active_event_ids=self._exempt_event_ids,
                    active_trade_lane=prep.active_lane,
                ).viable_venue_count
            return self._finalize_provider_status(runtime, matchbook_payload)
        known_implied = merge_known_implied(
            self._implied_from_matchbook(identity, matchbook_payload)
        )
        if not prep.active_lane and not prep.skip_bound:
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
                    kalshi_tickers=_kalshi_tickers(identity) if prep.kalshi_ready else [],
                    polymarket_tokens=prep.pm_tokens if prep.polymarket_ready else [],
                )
                return self._skip_item(
                    runtime,
                    result,
                    reason=post_mb.reason or CROSS_VENUE_UNAVAILABLE,
                    saved_calls=saved,
                    viable_count=post_mb.viable_venue_count,
                )
        return matchbook_payload, known_implied

    async def _load_kalshi_stage(
        self,
        runtime: PriceEngineRuntimeItem,
        result: PriceEngineSliceResult,
        *,
        prep: ExactIdPricingPrep,
        known_implied: Mapping[str, Decimal],
    ) -> dict[str, RetrievedVenuePayload] | PriceEngineItemStatus:
        kalshi_books = await self._refresh_kalshi_constituents(
            runtime,
            lane=prep.lane,
            result=result,
            known_implied=known_implied,
            skip_bound=prep.skip_bound,
        )
        if isinstance(kalshi_books, PriceEngineItemStatus):
            return self._finalize_provider_status(runtime, kalshi_books)
        required = _required_tickers(runtime.identity)
        if any(ticker not in kalshi_books for ticker in required):
            if runtime.status is PriceEngineItemStatus.SKIPPED:
                return PriceEngineItemStatus.SKIPPED
            return self._schedule_retry(runtime, PRICE_ENGINE_ITEM_TIMEOUT_REASON)
        return kalshi_books

    async def _load_polymarket_stage(
        self,
        runtime: PriceEngineRuntimeItem,
        *,
        prep: ExactIdPricingPrep,
        matchbook: RetrievedVenuePayload | None,
        kalshi_books: Mapping[str, RetrievedVenuePayload] | None,
    ) -> dict[str, RetrievedVenuePayload] | None | PriceEngineItemStatus:
        polymarket_books = await self._refresh_polymarket(
            runtime, lane=prep.lane, tokens=prep.pm_tokens
        )
        if isinstance(polymarket_books, PriceEngineItemStatus):
            if matchbook is not None and kalshi_books is not None:
                return None
            return self._finalize_provider_status(runtime, polymarket_books)
        return polymarket_books

    async def _publish_loaded_books(
        self,
        runtime: PriceEngineRuntimeItem,
        result: PriceEngineSliceResult,
        *,
        matchbook: RetrievedVenuePayload | None,
        kalshi_books: Mapping[str, RetrievedVenuePayload] | None,
        polymarket_books: Mapping[str, RetrievedVenuePayload] | None,
    ) -> PriceEngineItemStatus:
        if matchbook is not None and kalshi_books is not None:
            status = await self._await_local(
                self._evaluate_complete_item(
                    runtime,
                    matchbook=matchbook,
                    kalshi_books=kalshi_books,
                    result=result,
                )
            )
            if (
                status is PriceEngineItemStatus.EVALUATED
                and polymarket_books
                and self.paper_scan is not None
            ):
                await self._await_local(
                    self._evaluate_extra_polymarket_pairs(
                        runtime,
                        matchbook=matchbook,
                        kalshi_books=kalshi_books,
                        polymarket_books=polymarket_books,
                        result=result,
                    )
                )
            return status
        return await self._await_local(
            self._evaluate_flexible_pairs(
                runtime,
                matchbook=matchbook,
                kalshi_books=kalshi_books,
                polymarket_books=polymarket_books,
                result=result,
            )
        )

    async def _price_item(
        self,
        runtime: PriceEngineRuntimeItem,
        result: PriceEngineSliceResult,
        *,
        lane: str | None = None,
    ) -> PriceEngineItemStatus:
        """Sequential exact-ID pricing used by ACTIVE TRADE.

        BACKGROUND and HOT use the shared staged planner. Stage order here
        stays Matchbook, then Kalshi with upper-bound pruning, then Polymarket.
        """

        lane = self._mark_price_item_started(runtime, lane=lane)
        try:
            prepared = self._prepare_exact_id_pricing(runtime, result, lane=lane)
            if isinstance(prepared, PriceEngineItemStatus):
                return prepared
            matchbook_payload: RetrievedVenuePayload | None = None
            known_implied: dict[str, Decimal] = {}
            if prepared.matchbook_ready:
                loaded = await self._load_matchbook_stage(runtime, result, prep=prepared)
                if isinstance(loaded, PriceEngineItemStatus):
                    return loaded
                matchbook_payload, known_implied = loaded
            kalshi_books: dict[str, RetrievedVenuePayload] | None = None
            if prepared.kalshi_ready:
                kalshi_loaded = await self._load_kalshi_stage(
                    runtime,
                    result,
                    prep=prepared,
                    known_implied=known_implied,
                )
                if isinstance(kalshi_loaded, PriceEngineItemStatus):
                    return kalshi_loaded
                kalshi_books = kalshi_loaded
            polymarket_books: dict[str, RetrievedVenuePayload] | None = None
            if prepared.polymarket_ready:
                polymarket_loaded = await self._load_polymarket_stage(
                    runtime,
                    prep=prepared,
                    matchbook=matchbook_payload,
                    kalshi_books=kalshi_books,
                )
                if isinstance(polymarket_loaded, PriceEngineItemStatus):
                    return polymarket_loaded
                polymarket_books = polymarket_loaded
            return await self._publish_loaded_books(
                runtime,
                result,
                matchbook=matchbook_payload,
                kalshi_books=kalshi_books,
                polymarket_books=polymarket_books,
            )
        finally:
            runtime.in_flight = False

    async def _await_local(self, awaitable: Any) -> Any:
        started = monotonic()
        try:
            return await awaitable
        finally:
            diagnostics = _SLICE_DIAGNOSTICS.get()
            if diagnostics is not None:
                diagnostics.note_local_ms(max(0, int((monotonic() - started) * 1000)))

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
                coalesce_parts=(
                    str(identity.matchbook_event_id),
                    str(identity.matchbook_market_id),
                ),
                coro=getter(identity.matchbook_event_id, identity.matchbook_market_id),
                runtime=runtime,
            )
        except MatchbookMarketGoneError:
            self.viability_cache.mark_market_unavailable(
                identity.canonical_event_id,
                VenueName.MATCHBOOK,
                str(identity.matchbook_market_id),
                reason="market_gone",
            )
            return self._request_revalidation(runtime, f"{CATALOGUE_REVALIDATION_REASON}:gone")
        if status is not None:
            if status is PriceEngineItemStatus.RETRY_WAIT:
                runtime.last_error_stage = "get_market"
                runtime.last_error_detail = f"get_market_timeout after {self._provider_timeout:g}s"
                self.viability_cache.note_provider_issue(
                    VenueName.MATCHBOOK, "get_market_timeout"
                )
            return status
        market = extract_matchbook_market_payload(payload)
        if market is None or matchbook_payload_is_terminal(market):
            if market is not None and matchbook_payload_is_terminal(market):
                self.viability_cache.mark_market_terminal(
                    identity.canonical_event_id,
                    VenueName.MATCHBOOK,
                    str(identity.matchbook_market_id),
                    reason="market_terminal",
                )
            else:
                self.viability_cache.mark_market_unavailable(
                    identity.canonical_event_id,
                    VenueName.MATCHBOOK,
                    str(identity.matchbook_market_id),
                    reason="market_payload_missing",
                )
            return self._request_revalidation(runtime, f"{CATALOGUE_REVALIDATION_REASON}:gone")
        if str(market.get("id") or "") != str(identity.matchbook_market_id):
            return self._request_revalidation(runtime, f"{CATALOGUE_REVALIDATION_REASON}:identity")
        self.viability_cache.clear_market(
            identity.canonical_event_id,
            VenueName.MATCHBOOK,
            str(identity.matchbook_market_id),
        )
        self.viability_cache.clear_provider_issue(VenueName.MATCHBOOK)
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
                coalesce_parts=(str(identity.kalshi_event_ticker), str(ticker)),
                coro=getter(identity.kalshi_event_ticker, ticker),
                runtime=runtime,
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
            self.viability_cache.clear_market(
                identity.canonical_event_id, VenueName.KALSHI, ticker
            )
            self.viability_cache.clear_provider_issue(VenueName.KALSHI)
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
                coalesce_parts=(
                    str(identity.polymarket_event_id),
                    str(identity.polymarket_market_id),
                    token,
                ),
                coro=getter(
                    identity.polymarket_event_id,
                    identity.polymarket_market_id,
                    token,
                ),
                runtime=runtime,
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
            self.viability_cache.clear_provider_issue(VenueName.POLYMARKET)
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
            fee_snapshot=self._polymarket_fee_snapshot_payload(identity),
        )

    async def reprice_for_paper_entry(
        self,
        runtime: PriceEngineRuntimeItem,
        *,
        venues: tuple[VenueName, ...] | list[VenueName],
    ) -> Any:
        """Fetch the persisted exact books for one hedge and rescan them once.

        Bypasses the slice coalescer so this is a new read, not the discovery
        snapshot. Does not schedule a HOT/BACKGROUND retry, does not change
        cadence, and does not call list_events or list_markets.
        """

        from sports_hedge.application.execution_reprice import (
            EXECUTION_REPRICE_FAILED,
            ExecutionRepriceResult,
        )

        identity = runtime.identity
        row_id = identity.catalogue_row_id
        if row_id in self._execution_reprice_open:
            return ExecutionRepriceResult(reason=EXECUTION_REPRICE_FAILED)
        required = tuple(dict.fromkeys(venues))
        if len(required) < 2:
            return ExecutionRepriceResult(reason=EXECUTION_REPRICE_FAILED)
        if not self._execution_identity_ready(identity, required):
            return ExecutionRepriceResult(reason=EXECUTION_REPRICE_FAILED)
        self._execution_reprice_open.add(row_id)
        try:
            return await self._reprice_exact_books(runtime, required)
        finally:
            self._execution_reprice_open.discard(row_id)

    def _execution_identity_ready(
        self,
        identity: DerivedPriceEngineItem,
        venues: tuple[VenueName, ...],
    ) -> bool:
        for venue in venues:
            if venue is VenueName.MATCHBOOK:
                if not identity.matchbook_event_id or not identity.matchbook_market_id:
                    return False
            elif venue is VenueName.KALSHI:
                if not identity.kalshi_event_ticker or not _kalshi_tickers(identity):
                    return False
            elif venue is VenueName.POLYMARKET:
                tokens = executable_polymarket_token_ids(
                    list(identity.polymarket_token_ids),
                    event_id=identity.polymarket_event_id,
                    market_id=identity.polymarket_market_id,
                    condition_id=identity.polymarket_condition_id,
                    required_outcomes=list(identity.required_outcomes)
                    or required_outcomes_for_key(identity.register_canonical_key),
                )
                if not tokens:
                    return False
            else:
                return False
        return True

    async def _reprice_exact_books(
        self,
        runtime: PriceEngineRuntimeItem,
        venues: tuple[VenueName, ...],
    ) -> Any:
        from sports_hedge.application.execution_reprice import (
            EXECUTION_REPRICE_FAILED,
            ExecutionRepriceResult,
        )

        identity = runtime.identity
        # Qualifying HOT rank only. This does not enqueue a HOT scan or change cadence.
        started_at = self.now()
        started_mono = monotonic()
        assembly_ms = 0
        calls: list[dict[str, Any]] = []
        timing_token = _EXECUTION_REPRICE_CALLS.set(calls)
        work = self._execution_scheduler_work(runtime, venues)
        matchbook = None
        kalshi_books = None
        polymarket_books = None
        try:
            fetched = await self._execution_fetch_venues(
                identity,
                venues,
                runtime=runtime,
                scheduler_work=work,
            )
            assembly_ms = max(0, int((monotonic() - started_mono) * 1000))
            if fetched is None:
                failed = self._execution_diagnostics(
                    started_at,
                    assembly_ms,
                    calls,
                    {},
                    EXECUTION_REPRICE_FAILED,
                )
                self._log_execution_reprice(identity.catalogue_row_id, failed)
                return ExecutionRepriceResult(
                    reason=EXECUTION_REPRICE_FAILED,
                    diagnostics=failed,
                )
            matchbook = fetched.get(VenueName.MATCHBOOK)
            kalshi_books = fetched.get(VenueName.KALSHI)
            polymarket_books = fetched.get(VenueName.POLYMARKET)
        finally:
            _EXECUTION_REPRICE_CALLS.reset(timing_token)
        # Age the books at evaluation time, after the complete set is in hand.
        # A clock that moves during the fetch must not look future-dated.
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
        except Exception:
            failed = self._execution_diagnostics(
                started_at,
                assembly_ms,
                calls,
                {},
                EXECUTION_REPRICE_FAILED,
            )
            self._log_execution_reprice(identity.catalogue_row_id, failed)
            return ExecutionRepriceResult(reason=EXECUTION_REPRICE_FAILED, diagnostics=failed)
        quote_ages = {
            venue.value: observation.quote_age_ms for venue, observation in observations.items()
        }
        ordered = [
            venue
            for venue in (VenueName.MATCHBOOK, VenueName.KALSHI, VenueName.POLYMARKET)
            if venue in observations
        ]
        if len(ordered) != 2 or self.paper_scan is None:
            failed = self._execution_diagnostics(
                started_at,
                assembly_ms,
                calls,
                quote_ages,
                EXECUTION_REPRICE_FAILED,
            )
            self._log_execution_reprice(identity.catalogue_row_id, failed)
            return ExecutionRepriceResult(reason=EXECUTION_REPRICE_FAILED, diagnostics=failed)
        try:
            decision = self.paper_scan.scan_pair(
                observations[ordered[0]],
                observations[ordered[1]],
                **self._scan_kwargs(identity),
            )
        except Exception:
            failed = self._execution_diagnostics(
                started_at,
                assembly_ms,
                calls,
                quote_ages,
                EXECUTION_REPRICE_FAILED,
            )
            self._log_execution_reprice(identity.catalogue_row_id, failed)
            return ExecutionRepriceResult(reason=EXECUTION_REPRICE_FAILED, diagnostics=failed)
        # Provider age is already measured against the evaluation clock. The
        # paper-entry gate adds capture→decision elapsed, so both instants have
        # to be this entry. Leaving a fake engine clock on the legs would count
        # that gap as quote age and reject a book the provider age already scored.
        entry_at = datetime.now(UTC)
        decision = decision.model_copy(
            update={
                "scanned_at": entry_at,
                "fill_legs": [
                    leg.model_copy(update={"quote_captured_at": entry_at})
                    for leg in decision.fill_legs
                ],
            }
        )
        traced = self._execution_diagnostics(
            started_at,
            assembly_ms,
            calls,
            quote_ages,
            None,
        )
        self._log_execution_reprice(identity.catalogue_row_id, traced)
        return ExecutionRepriceResult(
            decision=decision,
            refreshed_venues=tuple(ordered),
            diagnostics=traced,
        )

    def _execution_scheduler_work(
        self,
        runtime: PriceEngineRuntimeItem,
        venues: tuple[VenueName, ...],
    ) -> SchedulerWork:
        """Rank Price 2 as qualifying HOT work with the paper-entry deadline.

        ``runtime=None`` previously built ordinary HOT work with no deadline, so
        a qualifying reread waited behind routine HOT and BACKGROUND traffic and
        inherited no freshness urgency. The deadline is the existing paper-entry
        quote-age window measured on the provider-access clock. It does not
        change cadence and does not use the active-trade lane.
        """

        access = self.provider_access
        now_mono = access._clock() if access is not None else monotonic()
        settings = getattr(self.paper_scan, "settings", None)
        freshness_ms = 2000
        if settings is not None:
            freshness_ms = int(getattr(settings, "paper_entry_max_quote_age_ms", 2000) or 2000)
        freshness_s = max(0.25, freshness_ms / 1000.0)
        kickoff = runtime.identity.kickoff_utc
        return SchedulerWork(
            lane=ScanLane.HOT.value,
            work_id=f"execution:{runtime.identity.catalogue_row_id}",
            viable_venue_count=max(2, len(venues)),
            viability_assessed=True,
            near_threshold=True,
            qualifying=True,
            in_play=kickoff is not None and kickoff <= self.now(),
            required_venues=tuple(venues),
            due_mono=now_mono,
            deadline_mono=now_mono + freshness_s,
            cadence_seconds=freshness_s,
            seq=0,
            now_mono=now_mono,
        )

    async def _execution_fetch_venues(
        self,
        identity: DerivedPriceEngineItem,
        venues: tuple[VenueName, ...],
        *,
        runtime: PriceEngineRuntimeItem,
        scheduler_work: SchedulerWork,
    ) -> dict[VenueName, Any] | None:
        """Fetch every required venue at once. One failure fails the whole set."""

        lane = ScanLane.HOT.value
        jobs: list[tuple[VenueName, Any]] = []
        if VenueName.MATCHBOOK in venues:
            jobs.append(
                (
                    VenueName.MATCHBOOK,
                    self._execution_fetch_matchbook(
                        identity,
                        lane=lane,
                        runtime=runtime,
                        scheduler_work=scheduler_work,
                    ),
                )
            )
        if VenueName.KALSHI in venues:
            jobs.append(
                (
                    VenueName.KALSHI,
                    self._execution_fetch_kalshi(
                        identity,
                        lane=lane,
                        runtime=runtime,
                        scheduler_work=scheduler_work,
                    ),
                )
            )
        if VenueName.POLYMARKET in venues:
            jobs.append(
                (
                    VenueName.POLYMARKET,
                    self._execution_fetch_polymarket(
                        identity,
                        lane=lane,
                        runtime=runtime,
                        scheduler_work=scheduler_work,
                    ),
                )
            )
        results = await asyncio.gather(*(job for _venue, job in jobs), return_exceptions=True)
        fetched: dict[VenueName, Any] = {}
        for (venue, _job), result in zip(jobs, results, strict=True):
            if isinstance(result, Exception) or result is None:
                return None
            fetched[venue] = result
        return fetched

    async def _execution_provider_call(
        self,
        venue: VenueName,
        *,
        lane: str,
        stage: str,
        source_id: str,
        coro: Any,
        runtime: PriceEngineRuntimeItem,
        scheduler_work: SchedulerWork,
    ) -> tuple[Any, PriceEngineItemStatus | None]:
        """Exact-ID read that queues on qualifying work, not the discovery slice.

        Slot admission uses the provider timeout. A discovery slice that is
        already out of wall time must not turn Price 2 into an immediate
        not-started. The call still takes a normal provider slot.
        """

        return await self._provider_call_execute(
            venue,
            lane=lane,
            stage=stage,
            source_id=source_id,
            coro=coro,
            runtime=runtime,
            scheduler_work=scheduler_work,
            admit_timeout=self._provider_timeout,
        )

    async def _execution_fetch_matchbook(
        self,
        identity: DerivedPriceEngineItem,
        *,
        lane: str,
        runtime: PriceEngineRuntimeItem,
        scheduler_work: SchedulerWork,
    ) -> RetrievedVenuePayload | None:
        getter = getattr(self.matchbook, "get_market", None)
        if not callable(getter):
            return None
        try:
            payload, status = await self._execution_provider_call(
                VenueName.MATCHBOOK,
                lane=lane,
                stage="get_market",
                source_id=str(identity.matchbook_market_id),
                coro=getter(identity.matchbook_event_id, identity.matchbook_market_id),
                runtime=runtime,
                scheduler_work=scheduler_work,
            )
        except MatchbookMarketGoneError:
            return None
        if status is not None or payload is None:
            return None
        market = extract_matchbook_market_payload(payload)
        if market is None or matchbook_payload_is_terminal(market):
            return None
        if str(market.get("id") or "") != str(identity.matchbook_market_id):
            return None
        return RetrievedVenuePayload(payload=market, retrieved_at=self.now())

    async def _execution_fetch_kalshi(
        self,
        identity: DerivedPriceEngineItem,
        *,
        lane: str,
        runtime: PriceEngineRuntimeItem,
        scheduler_work: SchedulerWork,
    ) -> dict[str, RetrievedVenuePayload] | None:
        getter = getattr(self.kalshi, "get_order_book", None) if self.kalshi is not None else None
        if not callable(getter):
            return None
        tickers = _required_tickers(identity)
        if not tickers:
            return None

        async def _one(ticker: str) -> tuple[str, RetrievedVenuePayload] | None:
            payload, status = await self._execution_provider_call(
                VenueName.KALSHI,
                lane=lane,
                stage="order_book",
                source_id=ticker,
                coro=getter(identity.kalshi_event_ticker, ticker),
                runtime=runtime,
                scheduler_work=scheduler_work,
            )
            if status is not None or payload is None:
                return None
            return ticker, RetrievedVenuePayload(payload=payload, retrieved_at=self.now())

        pairs = await asyncio.gather(*(_one(ticker) for ticker in tickers), return_exceptions=True)
        books: dict[str, RetrievedVenuePayload] = {}
        for item in pairs:
            if not isinstance(item, tuple):
                return None
            ticker, book = item
            books[ticker] = book
        if len(books) != len(tickers):
            return None
        return books

    async def _execution_fetch_polymarket(
        self,
        identity: DerivedPriceEngineItem,
        *,
        lane: str,
        runtime: PriceEngineRuntimeItem,
        scheduler_work: SchedulerWork,
    ) -> dict[str, RetrievedVenuePayload] | None:
        getter = (
            getattr(self.polymarket, "get_order_book", None)
            if self.polymarket is not None
            else None
        )
        if not callable(getter):
            return None
        tokens = executable_polymarket_token_ids(
            list(identity.polymarket_token_ids),
            event_id=identity.polymarket_event_id,
            market_id=identity.polymarket_market_id,
            condition_id=identity.polymarket_condition_id,
            required_outcomes=list(identity.required_outcomes)
            or required_outcomes_for_key(identity.register_canonical_key),
        )
        native_ids = [str(getattr(item, "native_id", item) or "").strip() for item in tokens]
        if not tokens or any(not token for token in native_ids):
            return None

        async def _one(token: str) -> tuple[str, RetrievedVenuePayload] | None:
            payload, status = await self._execution_provider_call(
                VenueName.POLYMARKET,
                lane=lane,
                stage="order_book",
                source_id=token,
                coro=getter(
                    identity.polymarket_event_id,
                    identity.polymarket_market_id,
                    token,
                ),
                runtime=runtime,
                scheduler_work=scheduler_work,
            )
            if status is not None or payload is None:
                return None
            return token, RetrievedVenuePayload(payload=payload, retrieved_at=self.now())

        pairs = await asyncio.gather(*(_one(token) for token in native_ids), return_exceptions=True)
        books: dict[str, RetrievedVenuePayload] = {}
        for item in pairs:
            if not isinstance(item, tuple):
                return None
            token, book = item
            books[token] = book
        if len(books) != len(native_ids):
            return None
        return books

    def _execution_diagnostics(
        self,
        started_at: datetime,
        assembly_ms: int,
        calls: list[dict[str, Any]],
        quote_ages: dict[str, int | None],
        reason: str | None,
    ) -> Any:
        from sports_hedge.application.execution_reprice import (
            ExecutionProviderCall,
            ExecutionRepriceDiagnostics,
        )

        recorded = tuple(
            ExecutionProviderCall(
                venue=str(call["venue"]),
                stage=str(call["stage"]),
                source_id=str(call["source_id"]),
                outcome=str(call["outcome"]),
                slot_wait_ms=int(call["slot_wait_ms"]),
                io_ms=int(call["io_ms"]),
            )
            for call in calls
        )
        return ExecutionRepriceDiagnostics(
            started_at=started_at,
            assembly_ms=assembly_ms,
            calls=recorded,
            quote_age_ms=dict(quote_ages),
            reason=reason,
        )

    def _log_execution_reprice(self, row_id: str, diagnostics: Any) -> None:
        LOGGER.info(
            "execution reprice row=%s reason=%s %s",
            row_id,
            diagnostics.reason or "scanned",
            diagnostics.compact(),
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
            self._clear_execution_miss(row_id)
            runtime.execution_miss_sticky = False
            if self.fixture_state is not None and not self._event_execution_miss_active(canonical_id):
                self.fixture_state.clear_execution_miss_hot(canonical_id)
            already_fixture = canonical_id in self._promoted_hot_ids
            was_scheduler_hot = (
                already_fixture
                or runtime.priority is PriceEnginePriority.HOT
                or runtime.pricing_slice_priority is PriceEnginePriority.HOT
            )
            runtime.near_threshold = True
            runtime.qualifying = bool(
                decision is not None
                and decision_net_edge(decision) is not None
                and decision.minimum_net_edge is not None
                and qualifies_min_net_arb(
                    decision_net_edge(decision), decision.minimum_net_edge
                )
            )
            self._promoted_hot_rows[row_id] = version
            self._promoted_hot_ids.add(canonical_id)
            if not already_fixture:
                result.promotions.append(canonical_id)
                if not was_scheduler_hot:
                    self._emit_operator_hot_promotion(runtime, decision, result)
        else:
            runtime.near_threshold = False
            runtime.qualifying = False
            if self._execution_miss_still_hot(row_id, version):
                runtime.execution_miss_sticky = True
                self._promoted_hot_rows[row_id] = version
                self._promoted_hot_ids.add(canonical_id)
            else:
                runtime.execution_miss_sticky = False
                self._clear_execution_miss(row_id)
                self._promoted_hot_rows.pop(row_id, None)
                self._refresh_promoted_hot_ids()
                if self.fixture_state is not None and not self._event_execution_miss_active(canonical_id):
                    self.fixture_state.clear_execution_miss_hot(canonical_id)
        self._reclassify_fixture(canonical_id)

    def note_recently_qualifying_execution_miss(
        self,
        *,
        canonical_event_id: str,
        catalogue_row_id: str,
        content_version: int,
        occurred_at: datetime,
        sticky_until: datetime,
        zero_fill_reason: str,
        pricing_lane: str | None = None,
    ) -> bool:
        """Retain HOT after a zero-fill price-movement miss. Returns whether it stuck.

        Does not open a paper trade. A later qualifying decision clears this
        window and uses the normal HOT lifecycle. Expired windows demote on
        the next uninteresting evaluation.
        """

        if not execution_miss_hot_active(self.now(), sticky_until):
            return False
        self._execution_miss_until[catalogue_row_id] = sticky_until
        self._execution_miss_version[catalogue_row_id] = content_version
        self._promoted_hot_rows[catalogue_row_id] = content_version
        self._promoted_hot_ids.add(canonical_event_id)
        if self.fixture_state is not None:
            self.fixture_state.note_execution_miss_hot(canonical_event_id, until=sticky_until)
        runtime = self._items.get(f"{catalogue_row_id}:{content_version}")
        if runtime is not None:
            runtime.execution_miss_sticky = True
            self._reclassify_fixture(canonical_event_id)
            self._emit_operator_hot_promotion(
                runtime,
                None,
                PriceEngineSliceResult(),
                promotion_reason=REASON_RECENTLY_QUALIFYING_EXECUTION_MISS,
                occurred_at=occurred_at,
                zero_fill_reason=zero_fill_reason,
                pricing_lane=pricing_lane,
            )
        return True

    def _execution_miss_still_hot(self, row_id: str, version: int) -> bool:
        if self._execution_miss_version.get(row_id) != version:
            return False
        return execution_miss_hot_active(self.now(), self._execution_miss_until.get(row_id))

    def _event_execution_miss_active(self, canonical_event_id: str) -> bool:
        for runtime in self._items.values():
            if runtime.identity.canonical_event_id != canonical_event_id:
                continue
            if self._execution_miss_still_hot(
                runtime.identity.catalogue_row_id,
                runtime.identity.content_version,
            ):
                return True
        return False

    def _clear_execution_miss(self, row_id: str) -> None:
        self._execution_miss_until.pop(row_id, None)
        self._execution_miss_version.pop(row_id, None)

    def _emit_operator_hot_promotion(
        self,
        runtime: PriceEngineRuntimeItem,
        decision: PaperScanDecision | None,
        result: PriceEngineSliceResult,
        *,
        promotion_reason: str | None = None,
        occurred_at: datetime | None = None,
        zero_fill_reason: str | None = None,
        pricing_lane: str | None = None,
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
        lane = pricing_lane or (
            runtime.pricing_slice_priority or runtime.priority or PriceEnginePriority.BACKGROUND
        )
        lane_value = getattr(lane, "value", str(lane))
        detail = None
        if promotion_reason:
            detail = format_hot_promotion_detail(
                canonical_event_id=canonical_id,
                fixture_label=fixture_label,
                market_family=identity.family,
                pricing_lane=lane_value,
                current_net_edge=edge,
                distance_to_trigger_pp=distance,
            )
            detail = f"{detail} · {promotion_reason}"
            if zero_fill_reason:
                detail = f"{detail} · zero_fill_reason={zero_fill_reason}"
        fact = HotPromotionFact(
            canonical_event_id=canonical_id,
            catalogue_row_id=identity.catalogue_row_id,
            content_version=identity.content_version,
            occurred_at=occurred_at or self.now(),
            opportunity_id=hot_promotion_opportunity_id(canonical_id),
            episode=episode,
            fixture_label=fixture_label,
            market_family=identity.family,
            pricing_lane=lane_value,
            current_net_edge=edge,
            distance_to_trigger_pp=distance,
            promotion_reason=promotion_reason,
            detail=detail,
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
            sport=_discovered_fixture_sport(identity),
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
        runtime: PriceEngineRuntimeItem | None = None,
        coalesce_parts: tuple[str, ...] | None = None,
    ) -> tuple[Any, PriceEngineItemStatus | None]:
        """Issue one exact-ID read, coalescing identical keys inside a BACKGROUND slice.

        HOT and ACTIVE leave the slice coalescer unset, so they keep one call
        per row. A joined caller closes its unused coroutine and reuses the
        leader's result. The cache does not outlive the slice context.
        """

        from sports_hedge.application.exact_id_coalesce import (
            ExactRequestKey,
            current_exact_id_coalescer,
        )

        coalescer = current_exact_id_coalescer()
        if coalescer is None:
            return await self._provider_call_execute(
                venue,
                lane=lane,
                stage=stage,
                source_id=source_id,
                coro=coro,
                runtime=runtime,
            )
        parts = tuple(str(part) for part in (coalesce_parts or (source_id,)))
        key = ExactRequestKey(venue=str(venue.value), stage=str(stage), parts=parts)

        def _close_unused_coro() -> None:
            close = getattr(coro, "close", None)
            if not callable(close):
                return
            try:
                close()
            except (RuntimeError, ValueError):
                return

        async def _leader() -> tuple[Any, PriceEngineItemStatus | None]:
            return await self._provider_call_execute(
                venue,
                lane=lane,
                stage=stage,
                source_id=source_id,
                coro=coro,
                runtime=runtime,
            )

        return await coalescer.share(key, _leader, on_join=_close_unused_coro)

    async def _provider_call_execute(
        self,
        venue: VenueName,
        *,
        lane: str,
        stage: str,
        source_id: str,
        coro: Any,
        runtime: PriceEngineRuntimeItem | None = None,
        scheduler_work: SchedulerWork | None = None,
        admit_timeout: float | None = None,
    ) -> tuple[Any, PriceEngineItemStatus | None]:
        access = self.provider_access
        timeout = self._provider_timeout
        row_id = None if runtime is None else runtime.identity.catalogue_row_id
        if access is None:
            if runtime is not None:
                runtime.provider_work_started = True
            started = monotonic()
            try:
                payload = await asyncio.wait_for(coro, timeout=timeout)
                self._record_lane_operation(lane, venue, stage, HEALTH_OK)
                self._note_provider_call(
                    venue, stage, "success", monotonic() - started, source_id, row_id
                )
                return payload, None
            except ProviderRateLimitedError:
                self._record_lane_operation(lane, venue, stage, HEALTH_RATE_LIMITED)
                self._note_provider_call(
                    venue, stage, "rate_limit", monotonic() - started, source_id, row_id
                )
                return None, PriceEngineItemStatus.RETRY_WAIT
            except TimeoutError:
                self._record_lane_operation(lane, venue, stage, HEALTH_MARKET_TIMEOUT)
                self._note_provider_call(
                    venue, stage, "timeout", monotonic() - started, source_id, row_id
                )
                return None, PriceEngineItemStatus.RETRY_WAIT

        async def _close_unused() -> None:
            close = getattr(coro, "close", None)
            if callable(close):
                try:
                    close()
                except (RuntimeError, ValueError):
                    pass

        slot_wait = self._slot_wait_seconds() if admit_timeout is None else float(admit_timeout)
        work = scheduler_work
        if work is None and runtime is not None:
            work = self.scheduler_work_for(runtime, lane=lane)
            if work.deadline_mono is not None:
                remaining_deadline = max(0.0, float(work.deadline_mono) - self.now().timestamp())
                work = SchedulerWork(
                    lane=work.lane,
                    work_id=work.work_id,
                    viable_venue_count=work.viable_venue_count,
                    skip_expensive_work=work.skip_expensive_work,
                    viability_reason=work.viability_reason,
                    viability_assessed=work.viability_assessed,
                    near_threshold=work.near_threshold,
                    qualifying=work.qualifying,
                    in_play=work.in_play,
                    required_venues=work.required_venues,
                    due_mono=access._clock(),
                    deadline_mono=access._clock() + remaining_deadline,
                    cadence_seconds=work.cadence_seconds,
                    seq=work.seq,
                    wait_age_ms=work.wait_age_ms,
                    now_mono=access._clock(),
                )
        if slot_wait <= 0:
            await _close_unused()
            if self._background_admission_refused(access, lane):
                self._note_provider_call(
                    venue, stage, "not_started", 0, source_id, row_id, slot_wait_s=0
                )
                return None, PriceEngineItemStatus.NOT_STARTED
            if access.venue_saturated(venue):
                self._record_lane_operation(lane, venue, stage, PROVIDER_CAPACITY_SATURATED)
                self._note_provider_call(
                    venue, stage, "capacity_deferred", 0, source_id, row_id, slot_wait_s=0
                )
                return None, PriceEngineItemStatus.DEFERRED
            self._note_provider_call(
                venue, stage, "not_started", 0, source_id, row_id, slot_wait_s=0
            )
            return None, PriceEngineItemStatus.NOT_STARTED
        slot_started = monotonic()
        async with access.acquire_wait(
            venue, lane=lane, stage=stage, timeout=slot_wait, work=work
        ) as lease:
            slot_wait_s = monotonic() - slot_started
            if lease is None:
                await _close_unused()
                if self._background_admission_refused(access, lane):
                    self._note_provider_call(
                        venue,
                        stage,
                        "not_started",
                        0,
                        source_id,
                        row_id,
                        slot_wait_s=slot_wait_s,
                    )
                    return None, PriceEngineItemStatus.NOT_STARTED
                if access.venue_saturated(venue):
                    self._record_lane_operation(lane, venue, stage, PROVIDER_CAPACITY_SATURATED)
                    self._note_provider_call(
                        venue,
                        stage,
                        "capacity_deferred",
                        0,
                        source_id,
                        row_id,
                        slot_wait_s=slot_wait_s,
                    )
                    return None, PriceEngineItemStatus.DEFERRED
                self._note_provider_call(
                    venue, stage, "not_started", 0, source_id, row_id, slot_wait_s=slot_wait_s
                )
                return None, PriceEngineItemStatus.NOT_STARTED
            if runtime is not None:
                runtime.provider_work_started = True
            inflight = access.snapshot().inflight.get(venue.value, 0)
            self._peak_held_slots[venue.value] = max(
                self._peak_held_slots.get(venue.value, 0), inflight
            )
            diagnostics = _SLICE_DIAGNOSTICS.get()
            if diagnostics is not None:
                diagnostics.note_inflight(venue.value, inflight)
            io_started = monotonic()
            task = asyncio.create_task(coro)
            done, _pending = await asyncio.wait({task}, timeout=timeout)
            io_s = monotonic() - io_started
            if task in done:
                try:
                    payload = task.result()
                    self._record_lane_operation(lane, venue, stage, HEALTH_OK)
                    self._note_provider_call(
                        venue,
                        stage,
                        "success",
                        io_s,
                        source_id,
                        row_id,
                        slot_wait_s=slot_wait_s,
                    )
                    return payload, None
                except MatchbookMarketGoneError:
                    self._note_provider_call(
                        venue, stage, "error", io_s, source_id, row_id, slot_wait_s=slot_wait_s
                    )
                    raise
                except ProviderRateLimitedError as exc:
                    access.observe_rate_limit(venue, exc.retry_after_seconds)
                    self._record_lane_operation(lane, venue, stage, HEALTH_RATE_LIMITED)
                    self._note_provider_call(
                        venue,
                        stage,
                        "rate_limit",
                        io_s,
                        source_id,
                        row_id,
                        slot_wait_s=slot_wait_s,
                    )
                    return None, PriceEngineItemStatus.RETRY_WAIT
                except Exception:
                    self._record_lane_operation(lane, venue, stage, HEALTH_MARKET_TIMEOUT)
                    self._note_provider_call(
                        venue, stage, "error", io_s, source_id, row_id, slot_wait_s=slot_wait_s
                    )
                    return None, PriceEngineItemStatus.RETRY_WAIT
            task.cancel()
            held = False
            if isinstance(lease, ProviderLease):
                held = lease.hold_until_task(task)
            if not held and not task.done():
                task.add_done_callback(lambda done_task: done_task.exception() if done_task.done() else None)
            self._record_lane_operation(lane, venue, stage, HEALTH_MARKET_TIMEOUT)
            self._note_provider_call(
                venue, stage, "timeout", io_s, source_id, row_id, slot_wait_s=slot_wait_s
            )
            return None, PriceEngineItemStatus.RETRY_WAIT

    def _background_admission_refused(self, access: Any, lane: str) -> bool:
        """Operator pause blocks new BACKGROUND calls, including saturated venues.

        A refused row is not started, so coverage can release the claim.
        Deferred would mark it visited and hide it until the next pass.
        """

        return (
            str(lane or "").strip().casefold() == PRICE_ENGINE_BACKGROUND_LANE
            and bool(getattr(access, "background_admission_paused", False))
        )

    def _note_provider_call(
        self,
        venue: VenueName,
        stage: str,
        outcome: str,
        elapsed_s: float,
        source_id: str,
        row_id: str | None,
        *,
        slot_wait_s: float = 0.0,
    ) -> None:
        execution_calls = _EXECUTION_REPRICE_CALLS.get()
        if execution_calls is not None:
            execution_calls.append(
                {
                    "venue": venue.value,
                    "stage": stage,
                    "source_id": source_id,
                    "outcome": outcome,
                    "slot_wait_ms": max(0, int(slot_wait_s * 1000)),
                    "io_ms": max(0, int(elapsed_s * 1000)),
                }
            )
        diagnostics = _SLICE_DIAGNOSTICS.get()
        if diagnostics is None:
            return
        diagnostics.note_provider(
            venue=venue.value,
            stage=stage,
            outcome=outcome,
            elapsed_ms=max(0, int(elapsed_s * 1000)),
            slot_wait_ms=max(0, int(slot_wait_s * 1000)),
            source_id=source_id,
            row_id=row_id,
        )

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
            sport=_discovered_fixture_sport(identity),
            kickoff_utc=identity.kickoff_utc or observed_at,
            last_seen_at=observed_at,
            last_scanned_at=observed_at,
            matchbook_matched=bool(identity.matchbook_event_id),
            kalshi_matched=bool(identity.kalshi_event_ticker),
            polymarket_matched=bool(identity.polymarket_event_id),
            market_evaluation_state=evaluation_state,
            market_evaluation_reason=reason,
            viability_evidence=build_viability_evidence(
                identity.canonical_event_id,
                cache=self.viability_cache,
                venues_present=[
                    venue.value for venue in catalogue_ready_venues(identity)
                ],
                final_reason=reason,
                registered_relationships=[str(identity.register_canonical_key or "")],
                evidence_stage="price_engine_skip",
                relationship_fields_scope="single_catalogue_row_skip_not_universe_search",
                market_relationship_evidence=market_relationship_not_collected(
                    "price_engine_skip_not_universe_catalogue"
                ),
            ),
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
        diagnostics = _SLICE_DIAGNOSTICS.get()
        if outcome is PriceEngineItemStatus.EVALUATED:
            result.evaluated.append(row_id)
            return
        if outcome is PriceEngineItemStatus.RETRY_WAIT:
            if diagnostics is not None:
                diagnostics.note_terminal(
                    "retry_wait", row_id, runtime.last_error_detail
                )
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
            if diagnostics is not None:
                diagnostics.note_terminal("not_started", row_id, NOT_STARTED_THIS_CADENCE)
            result.not_started.append(row_id)
            return
        if outcome is PriceEngineItemStatus.DEFERRED:
            runtime.status = PriceEngineItemStatus.DEFERRED
            if diagnostics is not None:
                diagnostics.note_terminal("deferred", row_id, PROVIDER_CAPACITY_SATURATED)
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
            if diagnostics is not None:
                diagnostics.note_terminal(
                    "revalidation", row_id, runtime.last_error_detail
                )
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
        if diagnostics is not None:
            diagnostics.note_terminal("failed", row_id, runtime.last_error_detail)
        result.failed.append(row_id)

    def _fee_snapshot_payload(self, identity: DerivedPriceEngineItem) -> dict[str, Any] | None:
        if self.catalogue_store is None or not identity.kalshi_fee_snapshot_id:
            return None
        snapshot = self.catalogue_store.get_fee_snapshot(identity.kalshi_fee_snapshot_id)
        if snapshot is None or not snapshot.is_known():
            return None
        return snapshot.observation_metadata()

    def _polymarket_fee_snapshot_payload(
        self, identity: DerivedPriceEngineItem
    ) -> dict[str, Any] | None:
        if self.catalogue_store is None or not identity.polymarket_fee_snapshot_id:
            return None
        snapshot = self.catalogue_store.get_polymarket_fee_snapshot(
            identity.polymarket_fee_snapshot_id
        )
        if snapshot is None:
            return None
        return snapshot.observation_metadata()

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
            pricing_fixtures=len({item.identity.canonical_event_id for item in items}),
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


def _interleave_hot_fixtures(
    membership: list[PriceEngineRuntimeItem],
) -> list[PriceEngineRuntimeItem]:
    """Round-robin catalogue rows across canonical fixtures.

    Row economics stay exact. Only the claim order changes, so one fixture
    with many markets cannot consume the whole provider window before the
    next fixture is offered a row.
    """

    groups: dict[str, list[PriceEngineRuntimeItem]] = {}
    for item in membership:
        fixture_id = item.identity.canonical_event_id or item.identity.catalogue_row_id
        groups.setdefault(fixture_id, []).append(item)
    for rows in groups.values():
        rows.sort(key=lambda item: item.identity.catalogue_row_id)
    ordered_fixtures = sorted(groups)
    interleaved: list[PriceEngineRuntimeItem] = []
    depth = 0
    while True:
        added = False
        for fixture_id in ordered_fixtures:
            rows = groups[fixture_id]
            if depth < len(rows):
                interleaved.append(rows[depth])
                added = True
        if not added:
            return interleaved
        depth += 1


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
    key = str(identity.register_canonical_key or "")
    competition = str(identity.competition or "").upper()
    if key.startswith("MLB_") or competition == "MLB":
        sport_name = "Baseball"
        competition_name = identity.competition or "mlb"
    elif key.startswith("NFL_") or competition == "NFL":
        sport_name = "American Football"
        competition_name = identity.competition or "NFL"
    elif key.startswith("NBA_") or competition == "NBA":
        sport_name = "Basketball"
        competition_name = identity.competition or "NBA"
    else:
        sport_name = "Football"
        competition_name = identity.competition or "Premier League"
    return {
        "id": identity.matchbook_event_id,
        "name": f"{identity.home_canonical or 'Home'} vs {identity.away_canonical or 'Away'}",
        "start": kickoff.isoformat(),
        "sport-name": sport_name,
        "competition-name": competition_name,
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


def _discovered_fixture_sport(identity: DerivedPriceEngineItem) -> str:
    """Read-model sport only. Does not change catalogue identity or pricing."""

    return resolve_discovered_fixture_sport(
        competition=identity.competition,
        register_canonical_key=identity.register_canonical_key,
    )


def _sport_for_identity(identity: DerivedPriceEngineItem) -> str:
    key = str(identity.register_canonical_key or "")
    competition = str(identity.competition or "").upper()
    if key.startswith("MLB_") or competition == "MLB":
        from sports_hedge.mlb.constants import MLB_SPORT

        return MLB_SPORT
    if key.startswith("NFL_") or competition == "NFL":
        from sports_hedge.nfl.constants import NFL_SPORT

        return NFL_SPORT
    if key.startswith("NBA_") or competition == "NBA":
        from sports_hedge.nba.constants import NBA_SPORT

        return NBA_SPORT
    if key.startswith("NCAAB_"):
        from sports_hedge.ncaab.constants import NCAAB_SPORT

        return NCAAB_SPORT
    if key == "TENNIS_MATCH_WINNER" or competition in {"ATP", "WTA"}:
        from sports_hedge.tennis.constants import TENNIS_SPORT

        return TENNIS_SPORT
    from sports_hedge.ncaab.detect import is_ncaab_competition_label

    if is_ncaab_competition_label(str(identity.competition or "")):
        from sports_hedge.ncaab.constants import NCAAB_SPORT

        return NCAAB_SPORT
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
    if key == "MLB_GAME_WINNER_FT":
        return MarketFamily.GAME_WINNER
    if key.startswith("MLB_TOTAL_RUNS_FT:"):
        return MarketFamily.TOTAL_RUNS
    if key == "NFL_GAME_WINNER_FT":
        return MarketFamily.GAME_WINNER
    if key.startswith("NFL_POINT_SPREAD_FT:"):
        return MarketFamily.POINT_SPREAD
    if key.startswith("NFL_TOTAL_POINTS_FT:"):
        return MarketFamily.TOTAL_POINTS
    if key == "NBA_GAME_WINNER_FT":
        return MarketFamily.GAME_WINNER
    if key.startswith("NBA_POINT_SPREAD_FT:"):
        return MarketFamily.POINT_SPREAD
    if key.startswith("NBA_TOTAL_POINTS_FT:"):
        return MarketFamily.TOTAL_POINTS
    if key == "NCAAB_GAME_WINNER_FT":
        return MarketFamily.GAME_WINNER
    if key == "TENNIS_MATCH_WINNER":
        return MarketFamily.GAME_WINNER
    if key.startswith("NCAAB_POINT_SPREAD_FT:"):
        return MarketFamily.POINT_SPREAD
    if key.startswith("NCAAB_TOTAL_POINTS_FT:"):
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
