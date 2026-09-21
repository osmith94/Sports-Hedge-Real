from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager, contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from logging import getLogger
from time import monotonic, perf_counter
from typing import Any, Protocol

from pydantic import BaseModel, Field

from sports_hedge.application.executable_liquidity import (
    NO_EXECUTABLE_ARB,
    HeadlineBand,
    candidate_from_decision,
    headline_band_for,
    select_fixture_headline,
)
from sports_hedge.application.fixture_clusters import (
    ClusterPass,
    FixtureCluster,
    cluster_canonical_event_id,
    cluster_identity_aliases,
    cluster_member_events,
    to_venue_event,
    universe_cluster_sort_key,
)
from sports_hedge.application.universe_identity_cache import (
    GenerationIdentityCache,
    bind_universe_identity_cache,
)
from sports_hedge.application.equivalence_diagnostics import (
    zero_equivalent_reason_counts,
    zero_equivalent_reason_from_inventory,
)
from sports_hedge.application.fixture_inventory import (
    FixtureMarketInventoryRow,
    InventoryMarket,
    assemble_fixture_inventory,
    inventory_is_comparable_opportunity,
    inventory_summary,
    raw_market_id,
    raw_market_name,
    raw_market_type,
    raw_runner_labels,
    scan_eligible_pair,
)
from sports_hedge.application.hot_market_relationships import (
    HOT_RELATIONSHIP_MISSING_REASON,
    HOT_REVALIDATION_NEEDED_REASON,
    HOT_RELATIONSHIP_UNAVAILABLE_REASON,
    HotMarketRelationship,
    HotVenueLeg,
    canonical_market_from_leg,
    extract_matchbook_market_payload,
    fail_closed_inventory_row,
    hot_identity_matches_market,
    index_hot_relationships,
    kalshi_tickers_for_leg,
    matchbook_payload_is_terminal,
)
from sports_hedge.application.fixture_state import matchbook_fixture_state
from sports_hedge.application.lane_venues import (
    INSUFFICIENT_VENUES_REASON,
    OPERATOR_SCAN_VENUES,
    VENUE_HEALTH_DISABLED,
    coerce_operator_venues,
    comparison_allowed,
    comparison_warning,
    default_operator_venues,
)
from sports_hedge.application.market_observation import (
    KalshiObservationBuilder,
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
    VenueMarketObservation,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.provider_access import (
    HEALTH_AUTH_FAILURE,
    HEALTH_DISCOVERY_TIMEOUT,
    HEALTH_MARKET_TIMEOUT,
    HEALTH_OK,
    HEALTH_TIMEOUT,
    HEALTH_UNAVAILABLE,
    ProviderAccessLayer,
    ProviderLease,
    merge_lane_operation_health,
    operation_health_from_stage,
)
from sports_hedge.application.quote_freshness import (
    matchbook_market_quote_age,
    polymarket_books_quote_age,
    retrieval_quote_age,
)
from sports_hedge.application.approved_market_catalogue import FEE_SOURCE_GET_SERIES
from sports_hedge.application.catalogue_maintenance import (
    FamilyDiscoveryCompleteness,
    family_key_from_kalshi_series,
    pair_identity_from_markets,
    persist_universe_catalogue_pass_offloop,
)
from sports_hedge.application.scan_lanes import ScanLane, hot_sort_key, should_skip_market_work
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore
from sports_hedge.venues.matchbook import MatchbookMarketGoneError
from sports_hedge.catalogue.classify import classify_pair
from sports_hedge.catalogue.coverage_rows import (
    FixtureCatalogueCoverage,
    aggregate_coverage_by_archetype,
    fixture_catalogue_coverage,
)
from sports_hedge.catalogue.registry import (
    family_is_phase1_expensive_work,
    operational_kalshi_families,
)
from sports_hedge.catalogue.states import CatalogueApprovalState
from sports_hedge.application.target_competitions import (
    EVENT_IDENTITY_MISMATCH,
    KALSHI_SERIES_TICKERS_BY_CODE,
    SERIES_NOT_QUERIED,
    TARGET_COMPETITIONS,
    UNMATCHED_POLYMARKET_COVERAGE,
    TargetCompetition,
    TargetCompetitionCode,
    filter_in_scope_events,
    kalshi_series_tickers_for_codes,
    polymarket_series_ids_for_codes,
    resolve_target_competition,
    resolve_target_competition_from_kalshi_ticker,
    scope_matchbook_event,
    selected_includes_nfl,
    selected_includes_soccer,
)
from sports_hedge.outrights.universe_scopes import (
    kalshi_series_tickers_for_season_scopes,
    observe_selected_season_kalshi_events,
    partition_kalshi_season_events,
)
from sports_hedge.arbitrage.watchlist.economics import (
    distance_to_trigger_pp,
    net_edge_from_implied_sum,
    quantized_edge,
)
from sports_hedge.domain.football import (
    CanonicalEvent,
    CanonicalMarket,
    CanonicalOutcome,
    FootballPeriod,
    MarketFamily,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import VenueCostSnapshot
from sports_hedge.fees.kalshi import resolve_kalshi_fee_metadata
from sports_hedge.fees.models import FeeSnapshot
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.markets import MarketMatcher, MarketMatchResult
from sports_hedge.matching.ordinary_1x2 import (
    allow_unknown_settlement_for_ordinary_1x2,
    is_ordinary_full_time_1x2,
)
from sports_hedge.normalization.venues import (
    KALSHI_CONTRACT_RULE_KEYS,
    KALSHI_RULE_DIAGNOSTIC_CAP,
    KALSHI_UNMODELLED_CANCEL_RESCHEDULE_FAIR_PRICE_REASON,
    MATCHBOOK_NON_UNIQUE_CANONICAL_REASON,
    KalshiNormalizer,
    MatchbookNormalizer,
    PolymarketNormalizer,
    VenueNormalizationError,
    matchbook_unsupported_market_detail,
    merge_kalshi_contract_rules,
    promote_polymarket_complete_match_result,
    safe_kalshi_match_result_rule_layers,
)
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision
from sports_hedge.paper.preparation import PreparablePaperOpportunity

LOGGER = getLogger(__name__)


def _consume_orphaned_provider_task(task: asyncio.Task[Any]) -> None:
    """Retrieve a detached provider task result so exceptions are not left unconsumed."""

    if not task.done():
        return
    try:
        task.exception()
    except asyncio.CancelledError:
        return


class _NullCapacityLease:
    def hold_until_task(self, task: asyncio.Task[Any]) -> bool:
        del task
        return False

    async def release(self) -> None:
        return None


class _SemaphoreCapacityLease:
    """Hold a collector semaphore until the live provider task finishes."""

    def __init__(self, semaphore: asyncio.Semaphore) -> None:
        self._semaphore = semaphore
        self._owned = True

    def hold_until_task(self, task: asyncio.Task[Any]) -> bool:
        if not self._owned or task.done():
            return False
        self._owned = False

        def _done(done: asyncio.Task[Any]) -> None:
            _consume_orphaned_provider_task(done)
            try:
                self._semaphore.release()
            except ValueError:
                return

        task.add_done_callback(_done)
        return True

    async def release(self) -> None:
        if not self._owned:
            return
        self._owned = False
        self._semaphore.release()


def _new_kalshi_rule_enrichment() -> dict[str, int]:
    return {
        "attempted": 0,
        "applied": 0,
        "empty": 0,
        "rules_empty": 0,
        "unchanged_existing": 0,
        "failed": 0,
        "skipped_complete": 0,
    }


class MatchbookReadClient(Protocol):
    async def list_events(self, **filters: Any) -> dict[str, Any]: ...

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]: ...

    async def get_market(
        self,
        event_id: int | str,
        market_id: int | str,
        **filters: Any,
    ) -> dict[str, Any]: ...


class KalshiReadClient(Protocol):
    async def list_events(self, **filters: Any) -> dict[str, Any]: ...

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]: ...

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]: ...

    async def get_series(self, series_ticker: str) -> dict[str, Any]: ...


class PolymarketReadClient(Protocol):
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]: ...

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]: ...

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]: ...


class CollectorIssue(BaseModel):
    stage: str
    venue: VenueName | None = None
    source_id: str | None = None
    detail: str


class MarketEvaluationState(StrEnum):
    """Whether this fixture's markets were actually compared this cycle."""

    EVALUATED = "evaluated"
    NOT_EVALUATED_SCAN_DEADLINE = "not_evaluated_scan_deadline"
    MARKET_FETCH_UNAVAILABLE = "market_fetch_unavailable"
    HOT_RELATIONSHIP_MISSING = "hot_relationship_missing"
    SINGLE_VENUE_NO_CROSS_VENUE_CANDIDATE = "single_venue_no_cross_venue_candidate"


NOT_EVALUATED_SCAN_DEADLINE_REASON = "not_evaluated_scan_deadline"
SCAN_BUDGET_EXHAUSTED_REASON = "scan_budget_exhausted"
SINGLE_VENUE_NO_CROSS_VENUE_REASON = "single_venue_no_cross_venue_candidate"
MARKET_FETCH_UNAVAILABLE_REASON = "list_markets_unavailable"
UNIVERSE_COMPLETENESS_COMPLETE = "complete"
UNIVERSE_COMPLETENESS_DEADLINE_LEFTOVER = "deadline_leftover"
UNIVERSE_COMPLETENESS_EMPTY_UNIVERSE = "empty_universe"
UNIVERSE_COMPLETENESS_STALE_GENERATION_STATE = "stale_generation_state"
CANONICAL_WORK_SET_PARTIAL_REASONS = frozenset(
    {
        "clustering_truncated",
        "deadline_truncation",
        "retry_series_partial",
        "provider_failure",
        "auth_failure",
        "incomplete_discovery",
    }
)
_PROVIDER_FAILURE_VENUE_HEALTH = frozenset(
    {
        HEALTH_UNAVAILABLE,
        HEALTH_TIMEOUT,
        HEALTH_DISCOVERY_TIMEOUT,
        HEALTH_MARKET_TIMEOUT,
        "timeout",
        "unavailable",
    }
)
_RETRYABLE_SERIES_STATUSES = frozenset(
    {
        HEALTH_DISCOVERY_TIMEOUT,
        HEALTH_MARKET_TIMEOUT,
        HEALTH_UNAVAILABLE,
        HEALTH_TIMEOUT,
        "rate_limited",
        "timeout",
        "retry_wait",
        "pending",
    }
)
DEFAULT_MAX_EVENT_PAIRS = 60
# Keep ~4s of the 45s operator cycle for leftover assembly before coordinator grace.
SCAN_FINALISATION_RESERVE_SECONDS = 4.0
MIN_PROVIDER_WAIT_SECONDS = 0.05
# Cancel/orphan drain only. Must not become the provider-call timeout when the
# collector has no hard cycle deadline (unbounded UNIVERSE).
PROVIDER_CANCEL_DRAIN_SECONDS = 0.05
# list_events of one hung venue must not consume the entire soft budget before
# clustering/evaluation of fixtures already returned by healthy venues.
MIN_POST_DISCOVERY_SOFT_SECONDS = 1.5

DIAGNOSTIC_PROVIDERS = ("matchbook", "polymarket", "kalshi")
DIAGNOSTIC_STAGES = (
    "event_lookup",
    "market_discovery",
    "book_depth",
    "mapping_equivalence",
    "fees_fx_risk",
    "solver_allocation",
    "current_state_finalization",
    "persistence",
)
_PROVIDER_CALL_STAGE = {
    "list_events": "event_lookup",
    "list_markets": "market_discovery",
    "get_series": "market_discovery",
    "get_market": "market_discovery",
    "get_contract_terms": "market_discovery",
    "get_order_book": "book_depth",
    "order_book": "book_depth",
}
DEFAULT_CLUSTER_CONCURRENCY = 8
CLUSTER_COMPARISON_YIELD_EVERY = 8
NORMALIZE_EVENT_YIELD_EVERY = 8
# Hold back a meaningful slice of a 150s UNIVERSE chunk for market evaluation
# once identity candidates exist. Tiny cycle budgets (tests / leftover seconds)
# keep clustering unbounded up to the existing soft deadline so indexed
# identity work still finishes.
CLUSTERING_MARKET_EVAL_RESERVE_SECONDS = 45.0
CLUSTERING_MARKET_EVAL_RESERVE_FRACTION = 0.30
MIN_CLUSTERING_STAGE_SECONDS = 0.05
TINY_CYCLE_FOR_CLUSTERING_RESERVE_SECONDS = 5.0
DEFAULT_PROVIDER_CONCURRENCY = {
    VenueName.MATCHBOOK: 4,
    VenueName.POLYMARKET: 8,
    VenueName.KALSHI: 4,
}
_WALL_STAGE_NAME = {
    "normalize_match": "mapping_equivalence",
}


def _empty_timing() -> dict[str, int]:
    return {"calls": 0, "elapsed_ms": 0, "timeouts": 0, "cancels": 0}


class ScanAttribution:
    """Wave-A Item 2: provider/stage elapsed, timeouts and cancels."""

    def __init__(self) -> None:
        self.providers = {name: _empty_timing() for name in DIAGNOSTIC_PROVIDERS}
        self.stages = {name: _empty_timing() for name in DIAGNOSTIC_STAGES}

    def add(
        self,
        *,
        venue: str | None = None,
        stage: str | None = None,
        elapsed_ms: int = 0,
        timed_out: bool = False,
        cancelled: bool = False,
        calls: int = 1,
    ) -> None:
        elapsed_ms = max(0, int(elapsed_ms))
        mapped = _PROVIDER_CALL_STAGE.get(stage or "", stage)
        for bucket in self._buckets(venue, mapped):
            bucket["calls"] += calls
            bucket["elapsed_ms"] += elapsed_ms
            if timed_out:
                bucket["timeouts"] += 1
            if cancelled:
                bucket["cancels"] += 1

    def _buckets(self, venue: str | None, stage: str | None) -> list[dict[str, int]]:
        found: list[dict[str, int]] = []
        if venue in self.providers:
            found.append(self.providers[venue])
        if stage in self.stages:
            found.append(self.stages[stage])
        return found

    def snapshot(self) -> dict[str, Any]:
        return {
            "providers": {name: dict(bucket) for name, bucket in self.providers.items()},
            "stages": {name: dict(bucket) for name, bucket in self.stages.items()},
        }


def acknowledge_task_cancellation() -> None:
    """Clear a swallowed CancelledError so later awaits (HTTP aclose) can run.

    Python 3.11+ keeps Task.cancelling() > 0 after catching CancelledError.
    The next await then re-raises, which would discard a leftover CollectionReport
    and surface as scan_cycle_timeout.
    """

    task = asyncio.current_task()
    if task is None or not hasattr(task, "uncancel"):
        return
    while task.cancelling() > 0:
        task.uncancel()


class DiscoveredFixture(BaseModel):
    source: VenueName = VenueName.MATCHBOOK
    source_event_id: str
    canonical_event_id: str
    home_team: str
    away_team: str
    competition: str
    target_competition_code: str | None = None
    kickoff_utc: datetime
    matchbook_matched: bool = False
    polymarket_matched: bool = False
    kalshi_matched: bool = False
    fixture_status: str | None = None
    fixture_status_source: VenueName | None = None
    in_running: bool | None = None
    live_score_supported: bool = False
    home_score: int | None = None
    away_score: int | None = None
    last_seen_at: datetime
    matched_market_count: int = Field(default=0, ge=0)
    discovered_market_count: int = Field(default=0, ge=0)
    matched_equivalent_count: int | None = Field(default=None, ge=0)
    qualifying_market_count: int = Field(default=0, ge=0)
    near_executable_market_count: int = Field(default=0, ge=0)
    market_family: str | None = None
    outcome_context: str | None = None
    best_arb_market: str | None = None
    headline_band: str | None = None
    best_matchbook_price: Decimal | None = None
    best_polymarket_price: Decimal | None = None
    best_kalshi_price: Decimal | None = None
    current_net_edge: Decimal | None = None
    trigger_net_edge: Decimal | None = None
    distance_to_trigger_pp: Decimal | None = None
    quote_age_ms: int | None = Field(default=None, ge=0)
    quote_age_basis: str | None = None
    execution_risk_score: int | None = Field(default=None, ge=0, le=100)
    execution_risk_band: str | None = None
    execution_risk_reasons: list[str] = Field(default_factory=list)
    no_comparison_reason: str | None = None
    solver_is_arbitrage: bool = False
    opportunity_state: str = "unmatched"
    market_evaluation_state: str = MarketEvaluationState.NOT_EVALUATED_SCAN_DEADLINE.value
    market_evaluation_reason: str | None = None
    scan_lane: str | None = None
    last_scanned_at: datetime | None = None
    next_due_at: datetime | None = None
    hot_reasons: list[str] = Field(default_factory=list)
    catalogue_coverage: FixtureCatalogueCoverage | None = None
    event_match_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    event_match_threshold: float | None = Field(default=None, ge=0.0, le=1.0)


class FixturePaperEntry(BaseModel):
    opportunity_id: str
    trade_id: str
    state: str
    solver_model: str | None = None
    fill_kinds: list[str] = Field(default_factory=list)
    guaranteed_profit_gbp_at_open: Decimal | None = None
    paper_only: bool = True
    places_orders: bool = False
    rejection_reason: str | None = None


class FixtureDetailReadModel(BaseModel):
    fixture: DiscoveredFixture
    markets: list[FixtureMarketInventoryRow] = Field(default_factory=list)
    paper_entries: list[FixturePaperEntry] = Field(default_factory=list)
    preparable_opportunities: list[PreparablePaperOpportunity] = Field(default_factory=list)
    data_class: str = "live_paper_when_collected"
    paper_mode: str = "paper"
    execution_enabled: bool = False


class CollectionReport(BaseModel):
    started_at: datetime
    completed_at: datetime
    discovery_source: VenueName = VenueName.MATCHBOOK
    discovery_mode: str = "venue_union"
    matching_venue: VenueName | None = None
    matching_venues: list[VenueName] = Field(default_factory=list)
    enabled_venues: list[VenueName] = Field(
        default_factory=lambda: list(OPERATOR_SCAN_VENUES)
    )
    raw_matchbook_events: int = Field(default=0, ge=0)
    raw_polymarket_events: int = Field(default=0, ge=0)
    raw_kalshi_events: int = Field(default=0, ge=0)
    normalized_matchbook_events: int = Field(default=0, ge=0)
    normalized_polymarket_events: int = Field(default=0, ge=0)
    normalized_kalshi_events: int = Field(default=0, ge=0)
    matched_event_pairs: int = Field(default=0, ge=0)
    pair_counts: dict[str, int] = Field(default_factory=dict)
    normalized_matchbook_markets: int = Field(default=0, ge=0)
    normalized_polymarket_markets: int = Field(default=0, ge=0)
    normalized_kalshi_markets: int = Field(default=0, ge=0)
    matched_market_pairs: int = Field(default=0, ge=0)
    order_books_fetched: int = Field(default=0, ge=0)
    skipped_out_of_scope: int = Field(default=0, ge=0)
    skipped_by_reason: dict[str, int] = Field(default_factory=dict)
    rejected_competition_labels: list[str] = Field(default_factory=list)
    target_coverage: dict[str, dict[str, int]] = Field(default_factory=dict)
    config_warnings: list[str] = Field(default_factory=list)
    operator_summary: str = ""
    venue_health: dict[str, str] = Field(default_factory=dict)
    operation_health: dict[str, Any] = Field(default_factory=dict)
    discovery_reused: bool = False
    sweep_id: str | None = None
    qualifying_arbs: int = Field(default=0, ge=0)
    paper_decisions: list[PaperScanDecision] = Field(default_factory=list)
    discovered_fixtures: list[DiscoveredFixture] = Field(default_factory=list)
    fixture_markets: dict[str, list[FixtureMarketInventoryRow]] = Field(default_factory=dict)
    fixture_identity_aliases: dict[str, str] = Field(default_factory=dict)
    fixture_source_events: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    issues: list[CollectorIssue] = Field(default_factory=list)
    scan_diagnostics: dict[str, Any] = Field(default_factory=dict)
    series_results: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    scan_lane: str | None = None
    resume_cursor: str | None = None

    @property
    def paper_eligible_count(self) -> int:
        return sum(decision.eligible_for_paper_simulation for decision in self.paper_decisions)


class _NormalizedEvent:
    def __init__(self, raw: dict[str, Any], canonical: CanonicalEvent) -> None:
        self.raw = raw
        self.canonical = canonical


class _NormalizedMarket:
    def __init__(self, raw: dict[str, Any], canonical: CanonicalMarket) -> None:
        self.raw = raw
        self.canonical = canonical


class _VenueSideFetch:
    """Mutable collector of one venue's markets/books for a single cluster."""

    def __init__(self) -> None:
        self.markets: list[_NormalizedMarket] = []
        self.inventory: list[InventoryMarket] = []
        self.observations: dict[str, VenueMarketObservation] = {}
        self.listed = False
        self.failed = False
        self.listing_complete = False
        self.books = 0
        self.latency_ms = 0
        self.retrieved_at: datetime | None = None
        self.primary_event: _NormalizedEvent | None = None
        self.kalshi_incomplete_family_keys: set[str] = set()


class _HotLegRefresh:
    """One persisted HOT venue leg after a targeted quote/depth read."""

    def __init__(self) -> None:
        self.inventory: InventoryMarket | None = None
        self.observation: VenueMarketObservation | None = None
        self.gone = False
        self.identity_changed = False
        self.unavailable = False
        self.books = 0


class ReadOnlyCrossVenueCollector:
    """Discover, match and paper-scan first-class venue markets.

    Phase 1 venue clients remain market-data/read methods only because Sports Hedge
    has real execution disabled globally. Kalshi is a first-class venue like
    Matchbook (INTERNAL paper lifecycle), not MANUAL_EXTERNAL. Unsupported payloads
    are reported and skipped. Source events that share canonical fixture identity are
    clustered together (including split Polymarket/Kalshi series). Market pairing
    remains one-to-one so a single venue market cannot silently participate in
    multiple cross-venue matches in one pass. Every settlement-equivalent venue
    pair is scanned independently.
    """

    def __init__(
        self,
        *,
        matchbook: MatchbookReadClient,
        polymarket: PolymarketReadClient,
        paper_scan: PaperScanService,
        kalshi: KalshiReadClient | None = None,
        event_matcher: EventMatcher | None = None,
        market_matcher: MarketMatcher | None = None,
        matchbook_normalizer: MatchbookNormalizer | None = None,
        polymarket_normalizer: PolymarketNormalizer | None = None,
        kalshi_normalizer: KalshiNormalizer | None = None,
        venue_timeout_seconds: float = 15.0,
        provider_call_timeout_seconds: float = 8.0,
        cycle_timeout_seconds: float | None = 45.0,
        cluster_concurrency: int = DEFAULT_CLUSTER_CONCURRENCY,
        provider_concurrency: dict[VenueName, int] | None = None,
        provider_access: ProviderAccessLayer | None = None,
        catalogue_store: SqliteApprovedMarketCatalogueStore | None = None,
    ) -> None:
        self.matchbook = matchbook
        self.polymarket = polymarket
        self.kalshi = kalshi
        self.paper_scan = paper_scan
        self.event_matcher = event_matcher or paper_scan.market_matcher.event_matcher
        self.market_matcher = market_matcher or MarketMatcher(self.event_matcher)
        self.matchbook_normalizer = matchbook_normalizer or MatchbookNormalizer()
        self.polymarket_normalizer = polymarket_normalizer or PolymarketNormalizer()
        self.kalshi_normalizer = kalshi_normalizer or KalshiNormalizer()
        self.matchbook_builder = MatchbookObservationBuilder(self.matchbook_normalizer)
        self.polymarket_builder = PolymarketObservationBuilder(self.polymarket_normalizer)
        self.kalshi_builder = KalshiObservationBuilder(self.kalshi_normalizer)
        self._venue_timeout_seconds = venue_timeout_seconds
        self._provider_call_timeout_seconds = provider_call_timeout_seconds
        self._cycle_timeout_seconds = cycle_timeout_seconds
        self._cluster_concurrency_limit = max(1, int(cluster_concurrency))
        limits = provider_concurrency or DEFAULT_PROVIDER_CONCURRENCY
        self._provider_concurrency_limits = {
            venue: max(1, int(limits.get(venue, DEFAULT_PROVIDER_CONCURRENCY[venue])))
            for venue in DEFAULT_PROVIDER_CONCURRENCY
        }
        self._provider_access = provider_access
        self.catalogue_store = catalogue_store
        self._catalogue_persist_sema = asyncio.Semaphore(1)
        self._op_selected_season_scope_codes: tuple[str, ...] | None = None
        self._op_universe_generation_id: int | None = None
        self._cluster_sema: asyncio.Semaphore | None = None
        self._provider_semaphores: dict[VenueName, asyncio.Semaphore] = {}
        self._op_request_lane: str | None = None
        self._op_operation_health: dict[str, Any] = {}
        self._on_fixture_evaluated: Callable[..., Any] | None = None
        self._on_discovery_complete: Callable[..., Any] | None = None
        self._on_canonical_work_set: Callable[..., Any] | None = None
        self._op_series_results: dict[str, list[dict[str, Any]]] = {}
        self._peak_inflight = 0
        self._provider_inflight = {venue: 0 for venue in DEFAULT_PROVIDER_CONCURRENCY}
        self._provider_peak_inflight = {venue: 0 for venue in DEFAULT_PROVIDER_CONCURRENCY}
        self._op_issues: list[CollectorIssue] = []
        self._op_venue_health: dict[str, str] = {}
        self._op_enabled_venues: frozenset[VenueName] = frozenset(OPERATOR_SCAN_VENUES)
        self._op_venue_timeout = venue_timeout_seconds
        self._op_provider_timeout = provider_call_timeout_seconds
        self._op_deadline: float | None = None
        self._op_soft_deadline: float | None = None
        self._op_started_mono: float | None = None
        self._inflight: set[asyncio.Task[Any]] = set()
        self._stage_ms: dict[str, int] = {}
        self._timeouts_by_stage: dict[str, int] = {}
        self._provider_cancels = 0
        self._provider_calls = 0
        self._inflight_orphaned = 0
        self._attribution = ScanAttribution()
        self._kalshi_rule_enrichment = _new_kalshi_rule_enrichment()
        self._kalshi_rule_layer_diagnostics: list[dict[str, Any]] = []
        self._kalshi_contract_terms_cache: dict[str, dict[str, Any]] = {}
        self._kalshi_series_ok: dict[tuple[str, bool], dict[str, Any]] = {}
        self._kalshi_series_inflight: dict[
            tuple[str, bool], asyncio.Future[dict[str, Any] | None]
        ] = {}
        self._kalshi_books_eligible = 0
        self._kalshi_books_skipped_unapproved = 0
        self._kalshi_get_market_failed_tickers: set[str] = set()
        self._identity_cache: GenerationIdentityCache | None = None

    async def collect_and_scan(
        self,
        *,
        matchbook_event_filters: dict[str, Any] | None = None,
        polymarket_event_filters: dict[str, Any] | None = None,
        matchbook_market_filters: dict[str, Any] | None = None,
        polymarket_market_filters: dict[str, Any] | None = None,
        polymarket_queried_series_ids: list[str] | None = None,
        fee_snapshots: list[FeeSnapshot] | None = None,
        venue_costs: list[VenueCostSnapshot] | None = None,
        fx_snapshots: list[FxRateSnapshot] | None = None,
        capital_limit_gbp: Decimal | None = None,
        minimum_net_edge: Decimal = Decimal("0.005"),
        maximum_execution_risk: int = 60,
        minimum_mapping_confidence: float = 0.98,
        assumed_latency_ms: int = 500,
        recent_volatility_bps: float = 0.0,
        max_event_pairs: int = DEFAULT_MAX_EVENT_PAIRS,
        max_market_pairs_per_event: int = 50,
        config_warnings: list[str] | None = None,
        venue_timeout_seconds: float | None = None,
        provider_call_timeout_seconds: float | None = None,
        cycle_timeout_seconds: float | None = None,
        scan_lane: str | None = None,
        identity_scope: list[str] | None = None,
        resume_cursor: str | None = None,
        skip_event_ids: list[str] | None = None,
        universe_generation_id: int | None = None,
        selected_competition_codes: list[str] | tuple[str, ...] | None = None,
        selected_season_scope_codes: list[str] | tuple[str, ...] | None = None,
        generation_scope_version: int | None = None,
        generation_superseded: bool = False,
        generation_resume: bool = False,
        known_source_events: dict[str, list[dict[str, Any]]] | None = None,
        enabled_venues: list[VenueName] | tuple[VenueName, ...] | None = None,
        reuse_discovery: bool = False,
        discovery_snapshot: dict[str, list[dict[str, Any]]] | None = None,
        unbounded_cycle: bool = False,
        sweep_id: str | None = None,
        on_discovery_complete: Callable[..., Any] | None = None,
        on_fixture_evaluated: Callable[..., Any] | None = None,
        on_canonical_work_set: Callable[..., Any] | None = None,
        retry_series: dict[str, list[str]] | None = None,
        hot_market_relationships: dict[str, list[HotMarketRelationship]] | None = None,
    ) -> CollectionReport:
        if max_event_pairs <= 0 or max_market_pairs_per_event <= 0:
            raise ValueError("collector pair limits must be positive")
        started_at = datetime.now(UTC)
        issues: list[CollectorIssue] = []
        enabled = frozenset(
            coerce_operator_venues(enabled_venues)
            if enabled_venues is not None
            else default_operator_venues()
        )
        self._op_enabled_venues = enabled
        self._op_request_lane = (scan_lane or "").strip().casefold() or None
        self._op_universe_generation_id = universe_generation_id
        if universe_generation_id is not None:
            self._identity_cache = bind_universe_identity_cache(universe_generation_id)
        else:
            self._identity_cache = GenerationIdentityCache()
        self._op_selected_competition_codes = (
            tuple(selected_competition_codes)
            if selected_competition_codes is not None
            else None
        )
        self._op_selected_season_scope_codes = (
            tuple(selected_season_scope_codes)
            if selected_season_scope_codes is not None
            else None
        )
        self._op_generation_scope_version = generation_scope_version
        self._op_generation_superseded = bool(generation_superseded)
        self._op_operation_health = {}
        self._on_discovery_complete = on_discovery_complete
        self._on_fixture_evaluated = on_fixture_evaluated
        self._on_canonical_work_set = on_canonical_work_set
        self._op_series_results = {}
        self._op_canonical_work_set_authoritative = False
        self._op_canonical_work_set_partial_reason: str | None = "incomplete_discovery"
        venue_health: dict[str, str] = {
            VenueName.MATCHBOOK.value: "unknown",
            VenueName.POLYMARKET.value: "unknown",
            VenueName.KALSHI.value: "unavailable" if self.kalshi is None else "unknown",
        }
        for venue in OPERATOR_SCAN_VENUES:
            if venue not in enabled:
                venue_health[venue.value] = VENUE_HEALTH_DISABLED
        self._op_issues = issues
        self._op_venue_health = venue_health
        self._op_venue_timeout = (
            self._venue_timeout_seconds if venue_timeout_seconds is None else venue_timeout_seconds
        )
        self._op_provider_timeout = (
            self._provider_call_timeout_seconds
            if provider_call_timeout_seconds is None
            else provider_call_timeout_seconds
        )
        if unbounded_cycle:
            cycle_budget = None
        else:
            cycle_budget = (
                self._cycle_timeout_seconds if cycle_timeout_seconds is None else cycle_timeout_seconds
            )
        started_mono = monotonic()
        self._op_started_mono = started_mono
        self._stage_ms = {}
        self._timeouts_by_stage = {}
        self._provider_cancels = 0
        self._provider_calls = 0
        self._inflight_orphaned = 0
        self._peak_inflight = 0
        self._provider_inflight = {venue: 0 for venue in DEFAULT_PROVIDER_CONCURRENCY}
        self._provider_peak_inflight = {venue: 0 for venue in DEFAULT_PROVIDER_CONCURRENCY}
        self._attribution = ScanAttribution()
        self._kalshi_rule_enrichment = _new_kalshi_rule_enrichment()
        self._kalshi_rule_layer_diagnostics: list[dict[str, Any]] = []
        self._kalshi_contract_terms_cache: dict[str, dict[str, Any]] = {}
        self._kalshi_series_inflight = {}
        self._kalshi_books_eligible = 0
        self._kalshi_books_skipped_unapproved = 0
        self._kalshi_get_market_failed_tickers = set()
        self._inflight = set()
        self._cluster_sema = asyncio.Semaphore(self._cluster_concurrency_limit)
        self._provider_semaphores = {
            venue: asyncio.Semaphore(limit)
            for venue, limit in self._provider_concurrency_limits.items()
        }
        if cycle_budget is None:
            reserve = 0.0
            self._op_deadline = None
            self._op_soft_deadline = None
        else:
            budget = float(cycle_budget)
            reserve = finalisation_reserve_seconds(budget)
            self._op_deadline = started_mono + budget
            self._op_soft_deadline = self._op_deadline - reserve

        raw_matchbook_events: list[dict[str, Any]] = []
        raw_polymarket_events: list[dict[str, Any]] = []
        raw_kalshi_events: list[dict[str, Any]] = []
        matchbook_events: list[_NormalizedEvent] = []
        polymarket_events: list[_NormalizedEvent] = []
        kalshi_events: list[_NormalizedEvent] = []
        queried_series_ids: list[str] | None = None
        skipped_by_reason: dict[str, int] = {}
        rejected_labels: list[str] = []
        skipped_out_of_scope = 0
        clusters: list[FixtureCluster] = []
        pair_counts: dict[str, int] = {}
        decisions: list[PaperScanDecision] = []
        normalized_matchbook_markets = 0
        normalized_polymarket_markets = 0
        normalized_kalshi_markets = 0
        matched_market_pairs = 0
        order_books_fetched = 0
        fixture_markets: dict[str, list[FixtureMarketInventoryRow]] = {}
        discovered_fixtures: list[DiscoveredFixture] = []
        cancelled = False
        clustering_truncated = False
        clustering_diagnostics: dict[str, Any] = {}
        resolved_lane = (scan_lane or "").strip().casefold() or None
        hot_scope = {item.strip() for item in (identity_scope or []) if item and item.strip()}
        skip_ids = {item.strip() for item in (skip_event_ids or []) if item and item.strip()}
        clusters_before_resume = 0
        skipped_by_resume = 0
        stale_generation_state_ignored = False
        (
            self._op_hot_relationships,
            self._op_hot_relationships_by_source,
        ) = index_hot_relationships(hot_market_relationships)
        self._op_hot_stats = {
            "targeted": resolved_lane == ScanLane.HOT.value,
            "refreshed": 0,
            "missing": 0,
            "revalidation": 0,
            "matchbook_get_market": 0,
            "kalshi_order_books": 0,
        }
        # HOT skips list_events and reuses stored source-event payloads.
        # UNIVERSE reuses the sweep discovery snapshot after the first success.
        # HOT market work is a quote/depth refresh of persisted ApprovedEquivalent
        # relationships. It must not re-run list_markets / get_series rediscovery.
        reuse_snapshot = bool(reuse_discovery and discovery_snapshot)
        skip_discovery = (
            (resolved_lane == ScanLane.HOT.value and identity_scope is not None)
            or reuse_snapshot
            or (reuse_discovery and bool(known_source_events))
        )
        discovery_reused = skip_discovery
        try:
            with self._stage("event_discovery"):
                filtered_known = _filter_known_source_events(known_source_events or {}, enabled)
                if skip_discovery:
                    matchbook_payload: dict[str, Any] = {}
                    if reuse_snapshot:
                        (
                            raw_matchbook_events,
                            raw_polymarket_events,
                            raw_kalshi_events,
                        ) = _raw_events_from_discovery_snapshot(discovery_snapshot or {})
                    else:
                        (
                            raw_matchbook_events,
                            raw_polymarket_events,
                            raw_kalshi_events,
                        ) = _raw_events_from_known_source_events(filtered_known)
                    for venue_name, has_events in (
                        (VenueName.MATCHBOOK, bool(raw_matchbook_events)),
                        (VenueName.POLYMARKET, bool(raw_polymarket_events)),
                        (VenueName.KALSHI, bool(raw_kalshi_events)),
                    ):
                        if venue_name not in enabled:
                            venue_health[venue_name.value] = VENUE_HEALTH_DISABLED
                        elif reuse_snapshot or has_events:
                            # A reused complete snapshot is healthy even when that
                            # venue's event list is empty; unknown must not leak
                            # into reconciliation authority.
                            venue_health[venue_name.value] = HEALTH_OK
                    if self.kalshi is None and VenueName.KALSHI in enabled:
                        venue_health[VenueName.KALSHI.value] = "unavailable"
                    if retry_series:
                        (
                            raw_matchbook_events,
                            raw_polymarket_events,
                            raw_kalshi_events,
                        ) = await self._merge_retry_series(
                            raw_matchbook_events,
                            raw_polymarket_events,
                            raw_kalshi_events,
                            retry_series=retry_series,
                            enabled=enabled,
                            issues=issues,
                            venue_health=venue_health,
                        )
                else:
                    mb_task = self._discovery_task(
                        self.matchbook,
                        venue=VenueName.MATCHBOOK,
                        enabled=enabled,
                        filters=matchbook_event_filters or {},
                        issues=issues,
                        venue_health=venue_health,
                    )
                    pm_filters = dict(polymarket_event_filters or {})
                    k_filters: dict[str, Any] = {}
                    if self._op_selected_competition_codes is not None:
                        selected_pm_ids = polymarket_series_ids_for_codes(
                            self._op_selected_competition_codes
                        )
                        selected_k_tickers = list(
                            dict.fromkeys(
                                [
                                    *kalshi_series_tickers_for_codes(
                                        self._op_selected_competition_codes
                                    ),
                                    *kalshi_series_tickers_for_season_scopes(
                                        self._op_selected_season_scope_codes
                                    ),
                                ]
                            )
                        )
                        if "series_id" not in pm_filters:
                            pm_filters["series_ids"] = selected_pm_ids
                        if selected_k_tickers:
                            k_filters["series_tickers"] = selected_k_tickers
                    pm_task = self._discovery_task(
                        self.polymarket,
                        venue=VenueName.POLYMARKET,
                        enabled=enabled,
                        filters=pm_filters,
                        issues=issues,
                        venue_health=venue_health,
                    )
                    k_task = self._discovery_task(
                        self.kalshi,
                        venue=VenueName.KALSHI,
                        enabled=enabled,
                        filters=k_filters,
                        issues=issues,
                        venue_health=venue_health,
                    )
                    (
                        (raw_matchbook_events, matchbook_payload),
                        (raw_polymarket_events, _),
                        (raw_kalshi_events, _),
                    ) = await self._gather_bounded(
                        mb_task,
                        pm_task,
                        k_task,
                        default=(([], {}), ([], {}), ([], {})),
                    )
                    if self.kalshi is None and VenueName.KALSHI in enabled:
                        raw_kalshi_events = []
                        venue_health[VenueName.KALSHI.value] = "unavailable"
                    issues.extend(_matchbook_discovery_issues(matchbook_payload))
                self._emit_discovery_complete(
                    raw_matchbook_events,
                    raw_polymarket_events,
                    raw_kalshi_events,
                )

            with self._stage("normalize_match"):
                raw_kalshi_events, season_kalshi_events = partition_kalshi_season_events(
                    raw_kalshi_events,
                    selected_season_scope_codes=self._op_selected_season_scope_codes,
                )
                if (
                    resolved_lane == ScanLane.UNIVERSE.value
                    and season_kalshi_events
                    and self.catalogue_store is not None
                ):
                    observe_selected_season_kalshi_events(
                        self.catalogue_store,
                        season_kalshi_events,
                        now=started_at,
                        generation_id=(
                            None
                            if self._op_universe_generation_id is None
                            else str(self._op_universe_generation_id)
                        ),
                    )
                mb_scope = filter_in_scope_events(
                    raw_matchbook_events,
                    venue=VenueName.MATCHBOOK,
                    selected_codes=self._op_selected_competition_codes,
                )
                pm_scope = filter_in_scope_events(
                    raw_polymarket_events,
                    venue=VenueName.POLYMARKET,
                    selected_codes=self._op_selected_competition_codes,
                )
                k_scope = filter_in_scope_events(
                    raw_kalshi_events,
                    venue=VenueName.KALSHI,
                    selected_codes=self._op_selected_competition_codes,
                )
                skipped_by_reason = _merge_counts(
                    mb_scope.skipped_by_reason,
                    pm_scope.skipped_by_reason,
                    k_scope.skipped_by_reason,
                )
                rejected_labels = _unique_cap(
                    [*mb_scope.rejected_labels, *pm_scope.rejected_labels, *k_scope.rejected_labels],
                    50,
                )
                skipped_out_of_scope = mb_scope.skipped + pm_scope.skipped + k_scope.skipped
                matchbook_events = await self._normalize_events(
                    mb_scope.allowed, venue=VenueName.MATCHBOOK, issues=issues
                )
                polymarket_events = await self._normalize_events(
                    pm_scope.allowed, venue=VenueName.POLYMARKET, issues=issues
                )
                kalshi_events = await self._normalize_events(
                    k_scope.allowed, venue=VenueName.KALSHI, issues=issues
                )
                queried_series_ids = _resolved_queried_series_ids(
                    polymarket_event_filters,
                    (
                        polymarket_series_ids_for_codes(self._op_selected_competition_codes)
                        if self._op_selected_competition_codes is not None
                        and polymarket_queried_series_ids is None
                        else polymarket_queried_series_ids
                    ),
                )
                mb_items = [
                    to_venue_event(event, VenueName.MATCHBOOK) for event in matchbook_events
                ]
                pm_items = [
                    to_venue_event(event, VenueName.POLYMARKET) for event in polymarket_events
                ]
                k_items = [to_venue_event(event, VenueName.KALSHI) for event in kalshi_events]
                clusters, pair_counts, clustering_truncated, clustering_diagnostics = (
                    await self._cluster_venue_events_cooperative(
                        matchbook=mb_items,
                        polymarket=pm_items,
                        kalshi=k_items,
                        max_event_pairs=max_event_pairs,
                    )
                )
                if clustering_truncated and self._hard_deadline_reached() and not any(
                    issue.detail == "scan_cycle_deadline_reached" for issue in issues
                ):
                    issues.append(
                        CollectorIssue(stage="normalize_match", detail="scan_cycle_deadline_reached")
                    )
                clusters_before_resume = len(clusters)
                if resolved_lane != ScanLane.HOT.value:
                    cluster_ids = [
                        cluster_canonical_event_id(cluster)
                        for cluster in clusters
                        if cluster_canonical_event_id(cluster)
                    ]
                    authoritative, partial_reason = canonical_work_set_authority(
                        cluster_ids=cluster_ids,
                        clustering_truncated=clustering_truncated,
                        retry_series=retry_series,
                        venue_health=venue_health,
                        enabled=enabled,
                        issues=issues,
                        series_results=self._op_series_results,
                        provider_cancels=self._provider_cancels,
                    )
                    self._op_canonical_work_set_authoritative = authoritative
                    self._op_canonical_work_set_partial_reason = partial_reason
                    self._emit_canonical_work_set(
                        cluster_ids,
                        authoritative=authoritative,
                        partial_reason=partial_reason,
                    )
                effective_skip = skip_ids
                effective_cursor = resume_cursor
                if (
                    resolved_lane != ScanLane.HOT.value
                    and (skip_ids or (resume_cursor or "").strip())
                    and not generation_resume
                ):
                    # Skip/cursor from a closed generation must never empty a new sweep.
                    stale_generation_state_ignored = True
                    effective_skip = set()
                    effective_cursor = None
                if (
                    identity_scope is not None
                    or effective_skip
                    or effective_cursor
                    or resolved_lane == ScanLane.HOT.value
                ):
                    clusters = _select_lane_clusters(
                        clusters,
                        identity_scope=None if identity_scope is None else hot_scope,
                        skip_event_ids=effective_skip,
                        resume_cursor=effective_cursor,
                        scan_lane=resolved_lane,
                        seen_at=started_at,
                        polymarket_events=polymarket_events,
                        queried_series_ids=queried_series_ids,
                    )
                skipped_by_resume = 0
                if resolved_lane != ScanLane.HOT.value:
                    skipped_by_resume = max(0, clusters_before_resume - len(clusters))

            scan_kwargs = {
                "fee_snapshots": fee_snapshots,
                "venue_costs": venue_costs,
                "fx_snapshots": fx_snapshots,
                "capital_limit_gbp": capital_limit_gbp,
                "minimum_net_edge": minimum_net_edge,
                "maximum_execution_risk": maximum_execution_risk,
                "minimum_mapping_confidence": minimum_mapping_confidence,
                "assumed_latency_ms": assumed_latency_ms,
                "recent_volatility_bps": recent_volatility_bps,
            }
            # Truncated clustering still scans whatever union-find completed.
            # Skipping scan here leftovered the whole discovered universe as
            # scan_budget_exhausted even when healthy venues had already returned.
            with self._stage("cluster_scan"):
                (
                    cluster_rows,
                    decisions,
                    fixture_markets,
                    normalized_matchbook_markets,
                    normalized_polymarket_markets,
                    normalized_kalshi_markets,
                    matched_market_pairs,
                    order_books_fetched,
                ) = await self._scan_clusters_bounded(
                    clusters,
                    seen_at=started_at,
                    polymarket_events=polymarket_events,
                    queried_series_ids=queried_series_ids,
                    matchbook_market_filters=matchbook_market_filters or {},
                    polymarket_market_filters=polymarket_market_filters or {},
                    max_market_pairs_per_event=max_market_pairs_per_event,
                    scan_kwargs=scan_kwargs,
                    issues=issues,
                )
                discovered_fixtures.extend(cluster_rows)
        except asyncio.CancelledError:
            cancelled = True
            acknowledge_task_cancellation()
            await self._cancel_inflight()
            LOGGER.warning(
                "scan_cancelled_assembling_partial fixtures=%s inflight=%s",
                len(discovered_fixtures),
                len(self._inflight),
            )
            _append_deadline_leftovers(
                clusters,
                discovered_fixtures=discovered_fixtures,
                issues=issues,
                started_at=started_at,
                polymarket_events=polymarket_events,
                queried_series_ids=queried_series_ids,
            )

        return self._finish_report(
            started_at=started_at,
            started_mono=started_mono,
            issues=issues,
            venue_health=venue_health,
            raw_matchbook_events=raw_matchbook_events,
            raw_polymarket_events=raw_polymarket_events,
            raw_kalshi_events=raw_kalshi_events,
            matchbook_events=matchbook_events,
            polymarket_events=polymarket_events,
            kalshi_events=kalshi_events,
            skipped_out_of_scope=skipped_out_of_scope,
            skipped_by_reason=skipped_by_reason,
            rejected_labels=rejected_labels,
            clusters=clusters,
            pair_counts=pair_counts,
            normalized_matchbook_markets=normalized_matchbook_markets,
            normalized_polymarket_markets=normalized_polymarket_markets,
            normalized_kalshi_markets=normalized_kalshi_markets,
            matched_market_pairs=matched_market_pairs,
            order_books_fetched=order_books_fetched,
            config_warnings=list(config_warnings or []),
            decisions=decisions,
            discovered_fixtures=discovered_fixtures,
            fixture_markets=fixture_markets,
            cycle_budget=None if cycle_budget is None else float(cycle_budget),
            reserve=reserve,
            cancelled=cancelled,
            scan_lane=resolved_lane,
            resume_cursor=resume_cursor,
            universe_generation_id=universe_generation_id,
            generation_resume=generation_resume,
            clusters_before_resume=clusters_before_resume,
            skipped_by_resume=skipped_by_resume,
            stale_generation_state_ignored=stale_generation_state_ignored,
            discovery_reused=discovery_reused,
            sweep_id=sweep_id,
            clustering_truncated=clustering_truncated,
            clustering_diagnostics=clustering_diagnostics,
        )

    def _deadline_reached(self) -> bool:
        return self._op_soft_deadline is not None and monotonic() >= self._op_soft_deadline

    def _hard_deadline_reached(self) -> bool:
        return self._op_deadline is not None and monotonic() >= self._op_deadline

    def _provider_budget_exhausted(self) -> bool:
        remaining = self._remaining_soft()
        return remaining is not None and remaining < MIN_PROVIDER_WAIT_SECONDS

    def _remaining_soft(self) -> float | None:
        if self._op_soft_deadline is None:
            return None
        return self._op_soft_deadline - monotonic()

    def _remaining_assembly(self) -> float | None:
        """Seconds until the collector hard deadline, or None when unbounded.

        Unbounded UNIVERSE has no hard cycle envelope. Individual provider
        calls still use their configured finite venue/provider timeout.
        ``PROVIDER_CANCEL_DRAIN_SECONDS`` is only a cancel-drain allowance.
        """

        if self._op_deadline is None:
            return None
        return max(0.0, self._op_deadline - monotonic())

    def _cancel_drain_seconds(self) -> float:
        remaining = self._remaining_assembly()
        if remaining is None:
            return PROVIDER_CANCEL_DRAIN_SECONDS
        return min(PROVIDER_CANCEL_DRAIN_SECONDS, remaining)

    def _timeout_budget(self, requested: float) -> float:
        """Cap a provider wait to remaining soft budget and the hard collector deadline."""

        remaining = self._remaining_soft()
        if remaining is None:
            remaining = requested
        elif remaining < MIN_PROVIDER_WAIT_SECONDS:
            return 0.0
        hard = self._remaining_assembly()
        capped = min(requested, remaining) if hard is None else min(requested, remaining, hard)
        if capped < MIN_PROVIDER_WAIT_SECONDS:
            return 0.0
        return capped

    def _discovery_timeout_budget(self, requested: float) -> float:
        """Cap list_events so clustering and evaluation keep a reserved soft slice.

        Long cycles still use the configured venue timeout. Short UNIVERSE discovery
        chunks reserve ``MIN_POST_DISCOVERY_SOFT_SECONDS`` so a hung/degraded
        venue cannot leftover the whole discovered universe unevaluated.
        Unbounded UNIVERSE has no assembly deadline, so the configured venue
        timeout remains the finite per-call bound.
        """

        remaining_soft = self._remaining_soft()
        hard = self._remaining_assembly()
        if remaining_soft is None:
            capped = requested if hard is None else min(requested, hard)
            return 0.0 if capped < MIN_PROVIDER_WAIT_SECONDS else capped
        reserved = MIN_POST_DISCOVERY_SOFT_SECONDS
        if remaining_soft <= reserved + MIN_PROVIDER_WAIT_SECONDS:
            available = remaining_soft * 0.5
        else:
            available = remaining_soft - reserved
        capped = (
            min(requested, max(0.0, available))
            if hard is None
            else min(requested, max(0.0, available), hard)
        )
        if capped < MIN_PROVIDER_WAIT_SECONDS:
            return 0.0
        return capped

    @contextmanager
    def _stage(self, name: str) -> Iterator[None]:
        started = monotonic()
        try:
            yield
        finally:
            duration_ms = max(0, int((monotonic() - started) * 1000))
            self._stage_ms[name] = duration_ms
            mapped = _WALL_STAGE_NAME.get(name)
            if mapped is not None:
                self._attribution.add(stage=mapped, elapsed_ms=duration_ms, calls=1)
            LOGGER.info("scan_stage %s duration_ms=%s", name, duration_ms)

    def _request_cancel(self, task: asyncio.Task[Any]) -> None:
        if task.done():
            return
        task.cancel()
        self._provider_cancels += 1

    def _count_orphan_after_drain(self, task: asyncio.Task[Any]) -> None:
        """Orphans are tasks still pending after drain, not every cancel request."""

        if not task.done():
            self._inflight_orphaned += 1

    def _detach_provider_task(self, task: asyncio.Task[Any], *, count_orphan: bool) -> None:
        """Drop a provider task from inflight and consume a later result/exception.

        Bounded cancel must not wait forever. If the coroutine ignores cancel
        and later raises, retrieve that exception so the event loop does not
        emit ``Task exception was never retrieved``. Prefer
        ``_bind_live_capacity`` when a permit must stay occupied until the
        underlying call actually finishes.
        """

        if count_orphan:
            self._count_orphan_after_drain(task)
        self._inflight.discard(task)
        if task.done():
            _consume_orphaned_provider_task(task)
            return
        task.add_done_callback(_consume_orphaned_provider_task)

    def _bind_live_capacity(
        self,
        lease: ProviderLease | _SemaphoreCapacityLease | _NullCapacityLease,
        task: asyncio.Task[Any],
        venue: VenueName,
        *,
        count_logical_inflight: bool,
    ) -> bool:
        """Keep collector and provider capacity occupied until ``task`` finishes."""

        self._count_orphan_after_drain(task)
        if not lease.hold_until_task(task):
            if task.done():
                _consume_orphaned_provider_task(task)
            return False

        def _done(done: asyncio.Task[Any]) -> None:
            self._inflight.discard(done)
            if count_logical_inflight:
                self._provider_inflight[venue] = max(0, self._provider_inflight[venue] - 1)
            _consume_orphaned_provider_task(done)

        task.add_done_callback(_done)
        return True

    @asynccontextmanager
    async def _provider_capacity(
        self,
        venue: VenueName,
        *,
        stage: str,
    ) -> AsyncIterator[ProviderLease | _SemaphoreCapacityLease | _NullCapacityLease]:
        access = self._provider_access
        if access is not None:
            async with access.acquire(
                venue, lane=self._op_request_lane, stage=stage
            ) as lease:
                yield lease
            return
        sem = self._provider_semaphores.get(venue)
        if sem is None:
            yield _NullCapacityLease()
            return
        await sem.acquire()
        lease = _SemaphoreCapacityLease(sem)
        try:
            yield lease
        finally:
            await lease.release()

    async def _await_bounded_with_capacity(
        self,
        coro: Any,
        timeout: float,
        *,
        venue: VenueName,
        lease: ProviderLease | _SemaphoreCapacityLease | _NullCapacityLease,
        count_logical_inflight: bool = False,
    ) -> tuple[Any, bool]:
        held = False

        def _on_still_running(task: asyncio.Task[Any]) -> None:
            nonlocal held
            held = self._bind_live_capacity(
                lease,
                task,
                venue,
                count_logical_inflight=count_logical_inflight,
            )

        if count_logical_inflight:
            self._provider_inflight[venue] += 1
            self._provider_peak_inflight[venue] = max(
                self._provider_peak_inflight[venue],
                self._provider_inflight[venue],
            )
        try:
            return await self._await_bounded(
                coro, timeout, on_still_running=_on_still_running
            )
        finally:
            if count_logical_inflight and not held:
                self._provider_inflight[venue] -= 1

    async def _cancel_inflight(self) -> None:
        pending: list[asyncio.Task[Any]] = []
        for task in list(self._inflight):
            if not task.done():
                self._request_cancel(task)
                pending.append(task)
            else:
                self._inflight.discard(task)
                _consume_orphaned_provider_task(task)
        if pending:
            drain = self._cancel_drain_seconds()
            if drain > 0:
                await asyncio.wait(set(pending), timeout=drain)
        for task in pending:
            self._detach_provider_task(task, count_orphan=True)

    async def _await_bounded(
        self,
        coro: Any,
        timeout: float,
        *,
        on_still_running: Callable[[asyncio.Task[Any]], None] | None = None,
    ) -> tuple[Any, bool]:
        """Wait up to timeout, then cancel without blocking on uncooperative providers.

        When ``on_still_running`` is provided and the task is still alive after
        the drain, capacity accounting is transferred to that callback instead
        of dropping the permit.
        """

        if timeout <= 0:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            return None, True
        remaining_hard = self._remaining_assembly()
        if remaining_hard is not None:
            timeout = min(timeout, remaining_hard)
        if timeout <= 0:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            return None, True
        task = asyncio.create_task(coro)
        self._inflight.add(task)
        self._provider_calls += 1
        if len(self._inflight) > self._peak_inflight:
            self._peak_inflight = len(self._inflight)
        try:
            done, _pending = await asyncio.wait({task}, timeout=timeout)
            if task in done:
                self._inflight.discard(task)
                try:
                    return task.result(), False
                except asyncio.CancelledError:
                    return None, True
            self._request_cancel(task)
            drain = self._cancel_drain_seconds()
            if drain > 0:
                await asyncio.wait({task}, timeout=drain)
            if task.done() and not task.cancelled():
                self._inflight.discard(task)
                try:
                    return task.result(), False
                except asyncio.CancelledError:
                    return None, True
            if not task.done() and on_still_running is not None:
                on_still_running(task)
            else:
                self._detach_provider_task(task, count_orphan=True)
            return None, True
        except asyncio.CancelledError:
            self._request_cancel(task)
            drain = self._cancel_drain_seconds()
            if drain > 0 and not task.done():
                await asyncio.wait({task}, timeout=drain)
            if not task.done() and on_still_running is not None:
                on_still_running(task)
            else:
                self._detach_provider_task(task, count_orphan=True)
            raise
        finally:
            if task.done():
                self._inflight.discard(task)

    async def _gather_bounded(self, *coros: Any, default: Any) -> Any:
        remaining = self._remaining_soft()
        if remaining is None:
            return await asyncio.gather(*coros)
        if remaining <= 0:
            for coro in coros:
                close = getattr(coro, "close", None)
                if callable(close):
                    close()
            return default
        # Wrapper tasks only; inner `_await_bounded` owns provider call/cancel/orphan counts.
        # Inner list_events already uses the discovery budget, so wait the remaining
        # soft slice here and let timed-out venues record health before returning.
        tasks = [asyncio.create_task(coro) for coro in coros]
        try:
            _done, pending = await asyncio.wait(tasks, timeout=remaining)
        except asyncio.CancelledError:
            still_pending = [task for task in tasks if not task.done()]
            for task in still_pending:
                task.cancel()
            if still_pending:
                drain = self._cancel_drain_seconds()
                if drain > 0:
                    await asyncio.wait(set(still_pending), timeout=drain)
            for task in tasks:
                if not task.done():
                    task.add_done_callback(_consume_orphaned_provider_task)
                else:
                    _consume_orphaned_provider_task(task)
            raise
        for task in pending:
            task.cancel()
        if pending:
            drain = self._cancel_drain_seconds()
            if drain > 0:
                await asyncio.wait(pending, timeout=drain)
        for task in pending:
            if not task.done():
                task.add_done_callback(_consume_orphaned_provider_task)
            else:
                _consume_orphaned_provider_task(task)
        results: list[Any] = []
        defaults = default if isinstance(default, tuple) and len(default) == len(tasks) else None
        for index, task in enumerate(tasks):
            if task.done() and not task.cancelled() and task.exception() is None:
                results.append(task.result())
                continue
            results.append(defaults[index] if defaults is not None else None)
        return tuple(results) if len(results) != 1 else results[0]

    def _record_timeout(
        self,
        stage: str,
        venue: VenueName,
        source_id: str | None = None,
        *,
        waited_seconds: float | None = None,
    ) -> None:
        health = self._op_venue_health
        current = health.get(venue.value)
        if current == VENUE_HEALTH_DISABLED:
            return
        configured = (
            self._op_venue_timeout if stage == "list_events" else self._op_provider_timeout
        )
        waited = configured if waited_seconds is None else max(0.0, float(waited_seconds))
        self._timeouts_by_stage[stage] = self._timeouts_by_stage.get(stage, 0) + 1
        if waited_seconds is None or abs(waited - configured) < 1e-9:
            detail = f"{stage}_timeout after {configured:g}s"
        else:
            remaining = self._remaining_soft()
            extra = ""
            if remaining is not None:
                extra = f", remaining_soft={max(0.0, remaining):.3f}s"
            detail = (
                f"{stage}_timeout after {waited:g}s "
                f"(configured_timeout={configured:g}s{extra})"
            )
        self._op_issues.append(
            CollectorIssue(
                stage=stage,
                venue=venue,
                source_id=source_id,
                detail=detail,
            )
        )
        status = operation_health_from_stage(stage, timed_out=True)
        self._op_operation_health = merge_lane_operation_health(
            self._op_operation_health,
            venue=venue.value,
            operation=stage,
            status=status,
        )
        if stage == "list_events":
            health[venue.value] = HEALTH_DISCOVERY_TIMEOUT
        elif current == "ok":
            health[venue.value] = "degraded"
        elif current in {None, "unknown"}:
            health[venue.value] = HEALTH_MARKET_TIMEOUT

    async def _wait_provider(
        self,
        coro: Any,
        *,
        stage: str,
        venue: VenueName,
        source_id: str | None = None,
        default: Any,
    ) -> tuple[Any, bool]:
        try:
            async with self._provider_capacity(venue, stage=stage) as lease:
                return await self._wait_provider_unlocked(
                    coro,
                    stage=stage,
                    venue=venue,
                    source_id=source_id,
                    default=default,
                    lease=lease,
                )
        except asyncio.CancelledError:
            close = getattr(coro, "close", None)
            if callable(close):
                try:
                    close()
                except (RuntimeError, ValueError):
                    pass
            raise

    async def _wait_provider_unlocked(
        self,
        coro: Any,
        *,
        stage: str,
        venue: VenueName,
        source_id: str | None = None,
        default: Any,
        lease: ProviderLease | _SemaphoreCapacityLease | _NullCapacityLease | None = None,
    ) -> tuple[Any, bool]:
        timeout = self._timeout_budget(self._op_provider_timeout)
        started = monotonic()
        if timeout <= 0:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            self._record_timeout(stage, venue, source_id, waited_seconds=0.0)
            self._attribution.add(
                venue=venue.value,
                stage=stage,
                elapsed_ms=0,
                timed_out=True,
            )
            return default, True
        capacity = lease if lease is not None else _NullCapacityLease()
        try:
            payload, timed_out = await self._await_bounded_with_capacity(
                coro,
                timeout,
                venue=venue,
                lease=capacity,
                count_logical_inflight=True,
            )
            self._attribution.add(
                venue=venue.value,
                stage=stage,
                elapsed_ms=max(0, int((monotonic() - started) * 1000)),
                timed_out=timed_out,
            )
            if timed_out:
                self._record_timeout(stage, venue, source_id, waited_seconds=timeout)
                return default, True
            return payload, False
        except asyncio.CancelledError:
            self._attribution.add(
                venue=venue.value,
                stage=stage,
                elapsed_ms=max(0, int((monotonic() - started) * 1000)),
                cancelled=True,
            )
            raise
        except MatchbookMarketGoneError:
            raise
        except Exception as exc:
            self._op_issues.append(
                CollectorIssue(stage=stage, venue=venue, source_id=source_id, detail=str(exc))
            )
            current = self._op_venue_health.get(venue.value)
            if current == VENUE_HEALTH_DISABLED:
                return default, True
            if current == "ok":
                self._op_venue_health[venue.value] = "degraded"
            return default, True

    async def _matchbook_sport_ids_for_scope(self) -> str | None:
        """Extra Matchbook sport-ids only when NFL is in operator scope.

        Soccer-only discovery keeps the historical unfiltered list_events path
        so login/429 handling stays inside that call. NFL adds American Football
        (and soccer when both are selected) without raising provider concurrency.
        """

        client = self.matchbook
        if client is None:
            return None
        codes = self._op_selected_competition_codes
        if not selected_includes_nfl(codes):
            return None
        ids: list[str] = []
        if selected_includes_soccer(codes):
            resolver = getattr(client, "resolve_football_sport_id", None)
            if callable(resolver):
                ids.append(str(await resolver()))
        resolver = getattr(client, "resolve_american_football_sport_id", None)
        if callable(resolver):
            ids.append(str(await resolver()))
        return ",".join(ids) if ids else None

    async def _discovery_task(
        self,
        client: Any,
        *,
        venue: VenueName,
        enabled: frozenset[VenueName],
        filters: dict[str, Any],
        issues: list[CollectorIssue],
        venue_health: dict[str, str],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        if venue not in enabled:
            venue_health[venue.value] = VENUE_HEALTH_DISABLED
            return [], {}
        if client is None:
            venue_health[venue.value] = "unavailable"
            return [], {}
        return await self._list_raw_events(
            client,
            venue=venue,
            filters=filters,
            issues=issues,
            venue_health=venue_health,
        )

    async def _list_raw_events(
        self,
        client: Any,
        *,
        venue: VenueName,
        filters: dict[str, Any],
        issues: list[CollectorIssue],
        venue_health: dict[str, str],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        series_ids = _discovery_series_ids(venue, client, filters)
        if series_ids:
            return await self._list_raw_events_by_series(
                client,
                venue=venue,
                filters=filters,
                series_ids=series_ids,
                issues=issues,
                venue_health=venue_health,
            )
        timeout = self._discovery_timeout_budget(self._op_venue_timeout)
        started = monotonic()
        if timeout <= 0:
            self._record_timeout("list_events", venue)
            self._attribution.add(
                venue=venue.value,
                stage="list_events",
                elapsed_ms=0,
                timed_out=True,
            )
            return [], {}

        list_filters = dict(filters)
        if venue is VenueName.MATCHBOOK and "sport-ids" not in list_filters:
            try:
                sport_ids = await self._matchbook_sport_ids_for_scope()
            except Exception as exc:
                issues.append(CollectorIssue(stage="list_events", venue=venue, detail=str(exc)))
                if venue_health.get(venue.value) != VENUE_HEALTH_DISABLED:
                    venue_health[venue.value] = "unavailable"
                return [], {}
            if sport_ids:
                list_filters["sport-ids"] = sport_ids

        try:
            async with self._provider_capacity(venue, stage="list_events") as lease:
                payload, timed_out = await self._await_bounded_with_capacity(
                    client.list_events(**list_filters),
                    timeout,
                    venue=venue,
                    lease=lease,
                )
            self._attribution.add(
                venue=venue.value,
                stage="list_events",
                elapsed_ms=max(0, int((monotonic() - started) * 1000)),
                timed_out=timed_out,
            )
            if timed_out:
                self._record_timeout("list_events", venue)
                return [], {}
        except asyncio.CancelledError:
            self._attribution.add(
                venue=venue.value,
                stage="list_events",
                elapsed_ms=max(0, int((monotonic() - started) * 1000)),
                cancelled=True,
            )
            raise
        except Exception as exc:
            issues.append(CollectorIssue(stage="list_events", venue=venue, detail=str(exc)))
            if venue_health.get(venue.value) != VENUE_HEALTH_DISABLED:
                venue_health[venue.value] = "unavailable"
            return [], {}
        events, extra = _payload_events(payload, venue)
        client_report = list(getattr(client, "last_series_report", None) or [])
        if extra.get("series_results"):
            client_report = list(extra.get("series_results") or [])
        if not client_report and venue is VenueName.KALSHI and events:
            client_report = _combined_kalshi_series_ok_rows(events)
        if client_report:
            self._op_series_results[venue.value] = client_report
            if extra.get("partial") or any(item.get("status") != "ok" for item in client_report):
                venue_health[venue.value] = "degraded"
            else:
                venue_health[venue.value] = "ok"
        else:
            venue_health[venue.value] = "ok"
        return events, extra if extra else ({"events": events} if events else {})

    async def _list_raw_events_by_series(
        self,
        client: Any,
        *,
        venue: VenueName,
        filters: dict[str, Any],
        series_ids: list[str],
        issues: list[CollectorIssue],
        venue_health: dict[str, str],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        events: list[dict[str, Any]] = []
        seen: set[str] = set()
        series_results: list[dict[str, Any]] = []
        extra: dict[str, Any] = {}
        for series_id in series_ids:
            series_filters = dict(filters)
            if venue is VenueName.KALSHI:
                series_filters["series_ticker"] = series_id
                series_filters.pop("series_tickers", None)
            else:
                series_filters["series_id"] = series_id
            timeout = self._discovery_timeout_budget(self._op_venue_timeout)
            started = monotonic()
            if timeout <= 0:
                series_results.append(
                    {
                        "series": series_id,
                        "status": HEALTH_DISCOVERY_TIMEOUT,
                        "retryable": True,
                        "event_count": 0,
                        "reason": "discovery_timeout",
                    }
                )
                continue
            try:
                async with self._provider_capacity(venue, stage="list_events") as lease:
                    payload, timed_out = await self._await_bounded_with_capacity(
                        client.list_events(**series_filters),
                        timeout,
                        venue=venue,
                        lease=lease,
                    )
                self._attribution.add(
                    venue=venue.value,
                    stage="list_events",
                    elapsed_ms=max(0, int((monotonic() - started) * 1000)),
                    timed_out=timed_out,
                )
                if timed_out:
                    series_results.append(
                        {
                            "series": series_id,
                            "status": HEALTH_DISCOVERY_TIMEOUT,
                            "retryable": True,
                            "event_count": 0,
                            "reason": "discovery_timeout",
                        }
                    )
                    continue
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                status, retryable = _collector_series_failure_kind(exc)
                issues.append(
                    CollectorIssue(
                        stage="list_events",
                        venue=venue,
                        source_id=series_id,
                        detail=str(exc),
                    )
                )
                series_results.append(
                    {
                        "series": series_id,
                        "status": status,
                        "retryable": retryable,
                        "event_count": 0,
                        "reason": str(exc),
                    }
                )
                continue
            page_events, page_extra = _payload_events(payload, venue)
            if page_extra:
                extra.update(page_extra)
            retained = 0
            for item in page_events:
                event_id = _discovery_event_id(item, venue)
                if event_id and event_id in seen:
                    continue
                if event_id:
                    seen.add(event_id)
                events.append(item)
                retained += 1
            series_results.append(
                {
                    "series": series_id,
                    "status": "ok",
                    "retryable": False,
                    "event_count": retained,
                    "reason": None,
                }
            )
        self._op_series_results[venue.value] = series_results
        extra["series_results"] = series_results
        extra["events"] = events
        any_ok = any(item["status"] == "ok" for item in series_results)
        any_fail = any(item["status"] != "ok" for item in series_results)
        if any_ok and any_fail:
            venue_health[venue.value] = "degraded"
        elif any_ok:
            venue_health[venue.value] = "ok"
        elif any(item.get("status") == HEALTH_DISCOVERY_TIMEOUT for item in series_results):
            venue_health[venue.value] = HEALTH_DISCOVERY_TIMEOUT
        elif series_results:
            venue_health[venue.value] = str(series_results[0].get("status") or "unavailable")
        else:
            venue_health[venue.value] = "unavailable"
        return events, extra

    async def _merge_retry_series(
        self,
        raw_matchbook_events: list[dict[str, Any]],
        raw_polymarket_events: list[dict[str, Any]],
        raw_kalshi_events: list[dict[str, Any]],
        *,
        retry_series: dict[str, list[str]],
        enabled: frozenset[VenueName],
        issues: list[CollectorIssue],
        venue_health: dict[str, str],
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
        buckets = {
            VenueName.MATCHBOOK: list(raw_matchbook_events),
            VenueName.POLYMARKET: list(raw_polymarket_events),
            VenueName.KALSHI: list(raw_kalshi_events),
        }
        clients = {
            VenueName.MATCHBOOK: self.matchbook,
            VenueName.POLYMARKET: self.polymarket,
            VenueName.KALSHI: self.kalshi,
        }
        for venue in (VenueName.POLYMARKET, VenueName.KALSHI):
            series_ids = [str(item).strip() for item in retry_series.get(venue.value, []) if str(item).strip()]
            if venue not in enabled or not series_ids or clients[venue] is None:
                continue
            previous_series = list(self._op_series_results.get(venue.value) or [])
            events, _extra = await self._list_raw_events_by_series(
                clients[venue],
                venue=venue,
                filters={},
                series_ids=series_ids,
                issues=issues,
                venue_health=venue_health,
            )
            incoming_series = list(self._op_series_results.get(venue.value) or [])
            self._op_series_results[venue.value] = _merge_series_rows(
                previous_series, incoming_series
            )
            seen = {
                _discovery_event_id(item, venue)
                for item in buckets[venue]
                if _discovery_event_id(item, venue)
            }
            for item in events:
                event_id = _discovery_event_id(item, venue)
                if event_id and event_id in seen:
                    continue
                if event_id:
                    seen.add(event_id)
                buckets[venue].append(item)
        return (
            buckets[VenueName.MATCHBOOK],
            buckets[VenueName.POLYMARKET],
            buckets[VenueName.KALSHI],
        )

    def _finish_report(
        self,
        *,
        started_at: datetime,
        started_mono: float,
        issues: list[CollectorIssue],
        venue_health: dict[str, str],
        raw_matchbook_events: list[dict[str, Any]],
        raw_polymarket_events: list[dict[str, Any]],
        raw_kalshi_events: list[dict[str, Any]],
        matchbook_events: list[_NormalizedEvent],
        polymarket_events: list[_NormalizedEvent],
        kalshi_events: list[_NormalizedEvent],
        skipped_out_of_scope: int,
        skipped_by_reason: dict[str, int],
        rejected_labels: list[str],
        clusters: list[FixtureCluster],
        pair_counts: dict[str, int],
        normalized_matchbook_markets: int,
        normalized_polymarket_markets: int,
        normalized_kalshi_markets: int,
        matched_market_pairs: int,
        order_books_fetched: int,
        config_warnings: list[str],
        decisions: list[PaperScanDecision],
        discovered_fixtures: list[DiscoveredFixture],
        fixture_markets: dict[str, list[FixtureMarketInventoryRow]],
        cycle_budget: float | None,
        reserve: float,
        cancelled: bool,
        scan_lane: str | None = None,
        resume_cursor: str | None = None,
        universe_generation_id: int | None = None,
        generation_resume: bool = False,
        clusters_before_resume: int = 0,
        skipped_by_resume: int = 0,
        stale_generation_state_ignored: bool = False,
        discovery_reused: bool = False,
        sweep_id: str | None = None,
        clustering_truncated: bool = False,
        clustering_diagnostics: dict[str, Any] | None = None,
    ) -> CollectionReport:
        assembly_started = monotonic()
        completed_at = datetime.now(UTC)
        leftover_n = sum(
            1
            for item in discovered_fixtures
            if item.market_evaluation_state
            == MarketEvaluationState.NOT_EVALUATED_SCAN_DEADLINE.value
        )
        if leftover_n > 0 and not any(
            issue.detail == "scan_cycle_deadline_reached" for issue in issues
        ):
            issues.append(CollectorIssue(stage="collect", detail="scan_cycle_deadline_reached"))
        evaluated_n = sum(
            1
            for item in discovered_fixtures
            if item.market_evaluation_state == MarketEvaluationState.EVALUATED.value
        )
        identity_unmatched_n = sum(
            1
            for item in discovered_fixtures
            if item.market_evaluation_state == MarketEvaluationState.EVALUATED.value
            and item.no_comparison_reason == EVENT_IDENTITY_MISMATCH
        )
        no_comparable_n = sum(
            1
            for item in discovered_fixtures
            if item.market_evaluation_state == MarketEvaluationState.EVALUATED.value
            and (item.matched_equivalent_count or 0) == 0
        )
        fetch_unavailable_n = sum(
            1
            for item in discovered_fixtures
            if item.market_evaluation_state
            == MarketEvaluationState.MARKET_FETCH_UNAVAILABLE.value
        )
        qualifying = sum(1 for item in discovered_fixtures if item.solver_is_arbitrage)
        equivalent = sum(item.matched_equivalent_count or 0 for item in discovered_fixtures)
        matching_coverage = _matching_coverage(
            clusters,
            discovered_fixtures,
            pair_counts=pair_counts,
            equivalent=equivalent,
            qualifying=qualifying,
            fixture_markets=fixture_markets,
        )
        coverage = _target_coverage(clusters)
        deadline_hit = leftover_n > 0 or cancelled or any(
            issue.detail == "scan_cycle_deadline_reached" for issue in issues
        ) or self._provider_cancels > 0 or clustering_truncated
        universe_lane = (scan_lane or "").strip().casefold() != ScanLane.HOT.value
        completeness = (
            universe_sweep_completeness(
                leftover_n=leftover_n,
                evaluated_n=evaluated_n,
                clusters_before_resume=clusters_before_resume,
                skipped_by_resume=skipped_by_resume,
                deadline_hit=deadline_hit,
                generation_resume=generation_resume,
                clustering_truncated=clustering_truncated,
            )
            if universe_lane
            else None
        )
        operator_summary = (
            f"{len(discovered_fixtures)} fixtures discovered · "
            f"MB {len(matchbook_events)} · PM {len(polymarket_events)} · "
            f"K {len(kalshi_events)} · {sum(pair_counts.values())} cross-venue matches · "
            f"{equivalent} equivalent markets · {qualifying} qualifying arbs · "
            f"{skipped_out_of_scope} out-of-scope skipped"
        )
        if deadline_hit:
            operator_summary += f" · partial ({leftover_n} not evaluated)"
        elif completeness == UNIVERSE_COMPLETENESS_STALE_GENERATION_STATE:
            operator_summary += " · partial (stale generation state)"
        enabled_list = [
            venue for venue in OPERATOR_SCAN_VENUES if venue in self._op_enabled_venues
        ]
        matching_venues = [
            venue
            for venue in OPERATOR_SCAN_VENUES
            if venue in self._op_enabled_venues and venue is not VenueName.MATCHBOOK
        ]
        if self.kalshi is None:
            matching_venues = [
                venue for venue in matching_venues if venue is not VenueName.KALSHI
            ]
        if not matching_venues:
            matching_venues = list(enabled_list)
        matching_venue = matching_venues[0] if matching_venues else None
        comparison_note = comparison_warning(self._op_enabled_venues)
        if comparison_note and comparison_note not in config_warnings:
            config_warnings = [*config_warnings, comparison_note]
        total_ms = max(0, int((monotonic() - started_mono) * 1000))
        assembly_ms = max(0, int((monotonic() - assembly_started) * 1000))
        self._attribution.add(
            stage="current_state_finalization",
            elapsed_ms=assembly_ms,
            calls=1,
        )
        attributed = self._attribution.snapshot()
        timeout_count = sum(bucket["timeouts"] for bucket in attributed["providers"].values())
        diagnostics = {
            "event_discovery_ms": self._stage_ms.get("event_discovery", 0),
            "normalize_match_ms": self._stage_ms.get("normalize_match", 0),
            "cluster_scan_ms": self._stage_ms.get("cluster_scan", 0),
            "assembly_ms": assembly_ms,
            "total_ms": total_ms,
            "cycle_budget_s": cycle_budget,
            "finalisation_reserve_s": reserve,
            # A timed-out provider call requests cancellation even when the
            # collector's own soft deadline has not elapsed. Keep this signal
            # truthful for callers that must present a partial/degraded cycle.
            "soft_deadline_reached": deadline_hit,
            "cancelled": cancelled,
            "clusters_total": len(clusters),
            "clusters_evaluated": evaluated_n,
            "clusters_leftover": leftover_n,
            "evaluated_count": evaluated_n,
            "not_evaluated_count": leftover_n,
            "identity_unmatched_count": identity_unmatched_n,
            "no_comparable_markets_count": no_comparable_n,
            "market_fetch_unavailable_count": fetch_unavailable_n,
            "generation_resume_pending": bool(leftover_n > 0 and generation_resume),
            "provider_unavailable": {
                venue: health
                for venue, health in venue_health.items()
                if health
                in {
                    "unavailable",
                    "timeout",
                    HEALTH_DISCOVERY_TIMEOUT,
                    HEALTH_MARKET_TIMEOUT,
                    VENUE_HEALTH_DISABLED,
                }
            },
            "operation_health": dict(self._op_operation_health),
            "discovery_reused": discovery_reused,
            "sweep_id": sweep_id,
            "scan_deadline_exhausted": leftover_n > 0 or self._deadline_reached() or cancelled,
            "timeout_count": timeout_count,
            "cancel_count": self._provider_cancels,
            "providers": attributed["providers"],
            "stages": attributed["stages"],
            "provider_timeouts": dict(self._timeouts_by_stage),
            "provider_cancels": self._provider_cancels,
            "provider_calls": self._provider_calls,
            "inflight_orphaned": self._inflight_orphaned,
            "inflight_live": len(self._inflight),
            "peak_provider_inflight": self._peak_inflight,
            "peak_provider_inflight_by_venue": {
                venue.value: peak
                for venue, peak in self._provider_peak_inflight.items()
            },
            "cluster_concurrency": self._cluster_concurrency_limit,
            "provider_concurrency": {
                venue.value: limit
                for venue, limit in self._provider_concurrency_limits.items()
            },
            "scan_lane": scan_lane,
            "resume_cursor": resume_cursor,
            "universe_generation_id": universe_generation_id,
            "generation_resume": generation_resume,
            "clusters_before_resume": clusters_before_resume,
            "skipped_by_resume_count": skipped_by_resume,
            "stale_generation_state_ignored": stale_generation_state_ignored,
            "completeness": completeness,
            "partial": bool(
                deadline_hit
                or clustering_truncated
                or completeness == UNIVERSE_COMPLETENESS_STALE_GENERATION_STATE
            ),
            "enabled_venues": [item.value for item in self._op_enabled_venues],
            "kalshi_match_result_rule_enrichment": dict(self._kalshi_rule_enrichment),
            "kalshi_match_result_rule_layers": list(self._kalshi_rule_layer_diagnostics),
            "kalshi_order_book_policy": {
                "eligible_markets": self._kalshi_books_eligible,
                "skipped_unapproved": self._kalshi_books_skipped_unapproved,
            },
            "hot_targeted_refresh": dict(getattr(self, "_op_hot_stats", {})),
            "identity_scope": [
                item.canonical_event_id for item in discovered_fixtures
            ],
            "matching_coverage": matching_coverage,
            "series_results": dict(self._op_series_results),
            "canonical_work_total": clusters_before_resume or len(clusters),
            "canonical_work_set_authoritative": self._op_canonical_work_set_authoritative,
            "canonical_work_set_partial_reason": self._op_canonical_work_set_partial_reason,
            "raw_events_by_venue": {
                VenueName.MATCHBOOK.value: len(raw_matchbook_events),
                VenueName.POLYMARKET.value: len(raw_polymarket_events),
                VenueName.KALSHI.value: len(raw_kalshi_events),
            },
            "event_match_threshold": self.event_matcher.threshold,
            "event_match_confidences": {
                item.canonical_event_id: item.event_match_confidence
                for item in discovered_fixtures
                if item.event_match_confidence is not None
            },
            "naive_pair_space": int((clustering_diagnostics or {}).get("naive_pair_space") or 0),
            "candidate_pairs_generated": int(
                (clustering_diagnostics or {}).get("candidate_pairs_generated") or 0
            ),
            "candidate_pairs_considered": int(
                (clustering_diagnostics or {}).get("candidate_pairs_considered") or 0
            ),
            "pairs_pruned_by_index": int(
                (clustering_diagnostics or {}).get("pairs_pruned_by_index") or 0
            ),
            "pairs_skipped_by_generation_cache": int(
                (clustering_diagnostics or {}).get("pairs_skipped_by_generation_cache") or 0
            ),
            "candidate_reduction_pct": float(
                (clustering_diagnostics or {}).get("candidate_reduction_pct") or 0.0
            ),
            "cross_venue_clusters": int(matching_coverage.get("cross_venue_clusters") or 0),
            "single_venue_clusters": int(matching_coverage.get("single_venue_clusters") or 0),
            "clustering_duration_ms": int(
                (clustering_diagnostics or {}).get("clustering_duration_ms") or 0
            ),
            "clustering_truncated": bool(
                clustering_truncated
                or (clustering_diagnostics or {}).get("clustering_truncated")
            ),
            "single_venue_deferred_count": sum(
                1
                for item in discovered_fixtures
                if item.market_evaluation_state
                == MarketEvaluationState.SINGLE_VENUE_NO_CROSS_VENUE_CANDIDATE.value
            ),
        }
        LOGGER.info("scan_diagnostics %s", diagnostics)
        return CollectionReport(
            started_at=started_at,
            completed_at=completed_at,
            discovery_source=VenueName.MATCHBOOK,
            discovery_mode="venue_union",
            matching_venue=matching_venue,
            matching_venues=matching_venues,
            enabled_venues=enabled_list,
            raw_matchbook_events=len(raw_matchbook_events),
            raw_polymarket_events=len(raw_polymarket_events),
            raw_kalshi_events=len(raw_kalshi_events),
            normalized_matchbook_events=len(matchbook_events),
            normalized_polymarket_events=len(polymarket_events),
            normalized_kalshi_events=len(kalshi_events),
            matched_event_pairs=sum(pair_counts.values()),
            pair_counts=pair_counts,
            normalized_matchbook_markets=normalized_matchbook_markets,
            normalized_polymarket_markets=normalized_polymarket_markets,
            normalized_kalshi_markets=normalized_kalshi_markets,
            matched_market_pairs=matched_market_pairs,
            order_books_fetched=order_books_fetched,
            skipped_out_of_scope=skipped_out_of_scope,
            skipped_by_reason=skipped_by_reason,
            rejected_competition_labels=rejected_labels,
            target_coverage=coverage,
            config_warnings=config_warnings,
            operator_summary=operator_summary,
            venue_health=venue_health,
            operation_health=dict(self._op_operation_health),
            discovery_reused=discovery_reused,
            sweep_id=sweep_id,
            qualifying_arbs=qualifying,
            paper_decisions=decisions,
            discovered_fixtures=[
                item.model_copy(
                    update={
                        "last_seen_at": completed_at,
                        "scan_lane": scan_lane or item.scan_lane,
                        "last_scanned_at": item.last_scanned_at or completed_at,
                    }
                )
                for item in discovered_fixtures
            ],
            fixture_markets=fixture_markets,
            fixture_identity_aliases=_fixture_identity_aliases(
                clusters, discovered_fixtures, decisions
            ),
            fixture_source_events=_fixture_source_events(clusters, discovered_fixtures),
            issues=issues,
            scan_diagnostics=diagnostics,
            series_results=dict(self._op_series_results),
            scan_lane=scan_lane,
            resume_cursor=resume_cursor,
        )

    def _single_venue_cluster_result(
        self,
        cluster: FixtureCluster,
        *,
        seen_at: datetime,
        polymarket_events: list[_NormalizedEvent],
        queried_series_ids: list[str] | None,
    ) -> tuple[
        DiscoveredFixture,
        list[PaperScanDecision],
        list[FixtureMarketInventoryRow],
        dict[VenueName, int],
        int,
        int,
    ]:
        fixture = _fixture_from_cluster(
            cluster,
            seen_at=seen_at,
            polymarket_events=polymarket_events,
            queried_series_ids=queried_series_ids,
            single_venue_deferred=True,
        )
        return fixture, [], [], {}, 0, 0

    def _leftover_cluster_result(
        self,
        cluster: FixtureCluster,
        *,
        seen_at: datetime,
        polymarket_events: list[_NormalizedEvent],
        queried_series_ids: list[str] | None,
    ) -> tuple[
        DiscoveredFixture,
        list[PaperScanDecision],
        list[FixtureMarketInventoryRow],
        dict[VenueName, int],
        int,
        int,
    ]:
        leftover = _fixture_from_cluster(
            cluster,
            seen_at=seen_at,
            polymarket_events=polymarket_events,
            queried_series_ids=queried_series_ids,
            leftover=True,
        )
        return leftover, [], [], {}, 0, 0

    def _failed_cluster_result(
        self,
        cluster: FixtureCluster,
        *,
        seen_at: datetime,
        polymarket_events: list[_NormalizedEvent],
        queried_series_ids: list[str] | None,
        detail: str,
    ) -> tuple[
        DiscoveredFixture,
        list[PaperScanDecision],
        list[FixtureMarketInventoryRow],
        dict[VenueName, int],
        int,
        int,
    ]:
        fixture = _fixture_from_cluster(
            cluster,
            seen_at=seen_at,
            polymarket_events=polymarket_events,
            queried_series_ids=queried_series_ids,
        )
        fixture.market_evaluation_state = MarketEvaluationState.MARKET_FETCH_UNAVAILABLE.value
        fixture.market_evaluation_reason = detail or MARKET_FETCH_UNAVAILABLE_REASON
        fixture.no_comparison_reason = fixture.no_comparison_reason or MARKET_FETCH_UNAVAILABLE_REASON
        return fixture, [], [], {}, 0, 0

    def _emit_discovery_complete(
        self,
        raw_matchbook_events: list[dict[str, Any]],
        raw_polymarket_events: list[dict[str, Any]],
        raw_kalshi_events: list[dict[str, Any]],
    ) -> None:
        callback = self._on_discovery_complete
        if callback is None:
            return
        try:
            payload: dict[str, Any] = {
                VenueName.MATCHBOOK.value: list(raw_matchbook_events),
                VenueName.POLYMARKET.value: list(raw_polymarket_events),
                VenueName.KALSHI.value: list(raw_kalshi_events),
            }
            if self._op_series_results:
                payload["series_results"] = dict(self._op_series_results)
            callback(payload)
        except Exception:
            LOGGER.warning("on_discovery_complete callback failed", exc_info=True)

    def _emit_canonical_work_set(
        self,
        canonical_ids: list[str],
        *,
        authoritative: bool,
        partial_reason: str | None,
    ) -> None:
        callback = self._on_canonical_work_set
        if callback is None:
            return
        try:
            try:
                callback(
                    list(canonical_ids),
                    authoritative=authoritative,
                    partial_reason=partial_reason,
                )
            except TypeError:
                callback(list(canonical_ids))
        except Exception:
            LOGGER.warning("on_canonical_work_set callback failed", exc_info=True)

    def _emit_fixture_evaluated(
        self,
        cluster: FixtureCluster,
        fixture: DiscoveredFixture,
        decisions: list[PaperScanDecision],
        inventory: list[FixtureMarketInventoryRow],
    ) -> None:
        callback = self._on_fixture_evaluated
        if callback is None:
            return
        if fixture.market_evaluation_state not in {
            MarketEvaluationState.EVALUATED.value,
            MarketEvaluationState.MARKET_FETCH_UNAVAILABLE.value,
            MarketEvaluationState.SINGLE_VENUE_NO_CROSS_VENUE_CANDIDATE.value,
        }:
            return
        try:
            callback(cluster, fixture, decisions, inventory)
        except Exception:
            LOGGER.warning("on_fixture_evaluated callback failed", exc_info=True)

    async def _scan_clusters_bounded(
        self,
        clusters: list[FixtureCluster],
        *,
        seen_at: datetime,
        polymarket_events: list[_NormalizedEvent],
        queried_series_ids: list[str] | None,
        matchbook_market_filters: dict[str, Any],
        polymarket_market_filters: dict[str, Any],
        max_market_pairs_per_event: int,
        scan_kwargs: dict[str, Any],
        issues: list[CollectorIssue],
    ) -> tuple[
        list[DiscoveredFixture],
        list[PaperScanDecision],
        dict[str, list[FixtureMarketInventoryRow]],
        int,
        int,
        int,
        int,
        int,
    ]:
        results: list[
            tuple[
                DiscoveredFixture,
                list[PaperScanDecision],
                list[FixtureMarketInventoryRow],
                dict[VenueName, int],
                int,
                int,
            ]
            | None
        ] = [None] * len(clusters)

        def accept(index: int, cluster: FixtureCluster, row: Any) -> None:
            results[index] = row
            if row is None:
                return
            fixture, cluster_decisions, inventory, _counts, _fetched, _pairs = row
            self._emit_fixture_evaluated(
                cluster, fixture, cluster_decisions, inventory
            )

        async def run(index: int, cluster: FixtureCluster) -> None:
            try:
                if self._deadline_reached() or self._hard_deadline_reached():
                    accept(
                        index,
                        cluster,
                        self._leftover_cluster_result(
                            cluster,
                            seen_at=seen_at,
                            polymarket_events=polymarket_events,
                            queried_series_ids=queried_series_ids,
                        ),
                    )
                    return
                accept(
                    index,
                    cluster,
                    await self._scan_cluster(
                        cluster,
                        seen_at=seen_at,
                        polymarket_events=polymarket_events,
                        queried_series_ids=queried_series_ids,
                        matchbook_market_filters=matchbook_market_filters,
                        polymarket_market_filters=polymarket_market_filters,
                        max_market_pairs_per_event=max_market_pairs_per_event,
                        scan_kwargs=scan_kwargs,
                        issues=issues,
                    ),
                )
            except asyncio.CancelledError:
                accept(
                    index,
                    cluster,
                    self._leftover_cluster_result(
                        cluster,
                        seen_at=seen_at,
                        polymarket_events=polymarket_events,
                        queried_series_ids=queried_series_ids,
                    ),
                )
            except Exception as exc:
                accept(
                    index,
                    cluster,
                    self._failed_cluster_result(
                        cluster,
                        seen_at=seen_at,
                        polymarket_events=polymarket_events,
                        queried_series_ids=queried_series_ids,
                        detail=str(exc),
                    ),
                )
                issues.append(
                    CollectorIssue(
                        stage="cluster_scan",
                        source_id=cluster_canonical_event_id(cluster),
                        detail=str(exc),
                    )
                )

        pending: dict[int, asyncio.Task[None]] = {}
        next_work = 0
        concurrency = self._cluster_concurrency_limit
        universe_lane = self._op_request_lane == ScanLane.UNIVERSE.value
        work_indexes = [
            index
            for index, cluster in enumerate(clusters)
            if not (universe_lane and cluster.venue_count < 2)
        ]
        for index, cluster in enumerate(clusters):
            if universe_lane and cluster.venue_count < 2:
                accept(
                    index,
                    cluster,
                    self._single_venue_cluster_result(
                        cluster,
                        seen_at=seen_at,
                        polymarket_events=polymarket_events,
                        queried_series_ids=queried_series_ids,
                    ),
                )

        async def cancel_pending() -> None:
            await self._cancel_inflight()
            for task in pending.values():
                if not task.done():
                    task.cancel()
            drain = self._cancel_drain_seconds()
            if drain > 0 and pending:
                await asyncio.wait(set(pending.values()), timeout=drain)

        try:
            while next_work < len(work_indexes) or pending:
                while next_work < len(work_indexes) and len(pending) < concurrency:
                    if self._deadline_reached() or self._hard_deadline_reached():
                        break
                    index = work_indexes[next_work]
                    cluster = clusters[index]
                    pending[index] = asyncio.create_task(run(index, cluster))
                    next_work += 1
                    await asyncio.sleep(0)
                if not pending:
                    break
                remaining = self._remaining_soft()
                timeout = None if remaining is None else max(0.0, remaining)
                done_tasks, _still = await asyncio.wait(
                    set(pending.values()),
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for index, task in list(pending.items()):
                    if task in done_tasks or task.done():
                        pending.pop(index, None)
                        if (
                            task.done()
                            and not task.cancelled()
                            and task.exception() is not None
                        ):
                            exc = task.exception()
                            if results[index] is None:
                                accept(
                                    index,
                                    clusters[index],
                                    self._failed_cluster_result(
                                        clusters[index],
                                        seen_at=seen_at,
                                        polymarket_events=polymarket_events,
                                        queried_series_ids=queried_series_ids,
                                        detail=str(exc),
                                    ),
                                )
                                issues.append(
                                    CollectorIssue(
                                        stage="cluster_scan",
                                        source_id=cluster_canonical_event_id(
                                            clusters[index]
                                        ),
                                        detail=str(exc),
                                    )
                                )
                if timeout is not None and not done_tasks:
                    break
                if remaining is not None and remaining <= 0:
                    break
                if self._deadline_reached() or self._hard_deadline_reached():
                    break
        except asyncio.CancelledError:
            await cancel_pending()
            raise

        if pending:
            await cancel_pending()

        discovered: list[DiscoveredFixture] = []
        decisions: list[PaperScanDecision] = []
        fixture_markets: dict[str, list[FixtureMarketInventoryRow]] = {}
        mb_markets = 0
        pm_markets = 0
        k_markets = 0
        matched_pairs = 0
        books = 0
        leftover_needed: list[FixtureCluster] = []
        for index, cluster in enumerate(clusters):
            row = results[index]
            if row is None:
                leftover_needed.append(cluster)
                continue
            fixture, cluster_decisions, inventory, market_counts, fetched, pairs = row
            discovered.append(fixture)
            decisions.extend(cluster_decisions)
            mb_markets += market_counts.get(VenueName.MATCHBOOK, 0)
            pm_markets += market_counts.get(VenueName.POLYMARKET, 0)
            k_markets += market_counts.get(VenueName.KALSHI, 0)
            matched_pairs += pairs
            books += fetched
            if inventory:
                fixture_markets[fixture.canonical_event_id] = inventory
        if leftover_needed:
            _append_deadline_leftovers(
                leftover_needed,
                discovered_fixtures=discovered,
                issues=issues,
                started_at=seen_at,
                polymarket_events=polymarket_events,
                queried_series_ids=queried_series_ids,
            )
        return (
            discovered,
            decisions,
            fixture_markets,
            mb_markets,
            pm_markets,
            k_markets,
            matched_pairs,
            books,
        )

    async def _scan_cluster(
        self,
        cluster: FixtureCluster,
        *,
        seen_at: datetime,
        polymarket_events: list[_NormalizedEvent],
        queried_series_ids: list[str] | None,
        matchbook_market_filters: dict[str, Any],
        polymarket_market_filters: dict[str, Any],
        max_market_pairs_per_event: int,
        scan_kwargs: dict[str, Any],
        issues: list[CollectorIssue],
    ) -> tuple[
        DiscoveredFixture,
        list[PaperScanDecision],
        list[FixtureMarketInventoryRow],
        dict[VenueName, int],
        int,
        int,
    ]:
        if self._hard_deadline_reached() or self._provider_budget_exhausted():
            leftover = _fixture_from_cluster(
                cluster,
                seen_at=seen_at,
                polymarket_events=polymarket_events,
                queried_series_ids=queried_series_ids,
                leftover=True,
            )
            return leftover, [], [], {}, 0, 0
        if (
            self._op_request_lane == ScanLane.UNIVERSE.value
            and cluster.venue_count < 2
            and not cluster_needs_one_sided_catalogue_markets(cluster)
        ):
            return self._single_venue_cluster_result(
                cluster,
                seen_at=seen_at,
                polymarket_events=polymarket_events,
                queried_series_ids=queried_series_ids,
            )
        fixture = _fixture_from_cluster(
            cluster,
            seen_at=seen_at,
            polymarket_events=polymarket_events,
            queried_series_ids=queried_series_ids,
        )
        if should_skip_market_work(fixture, seen_at):
            await self._mark_universe_catalogue_terminal(fixture)
            return fixture, [], [], {}, 0, 0
        if self._op_request_lane == ScanLane.HOT.value:
            return await self._refresh_hot_cluster(
                cluster,
                fixture=fixture,
                seen_at=seen_at,
                polymarket_events=polymarket_events,
                queried_series_ids=queried_series_ids,
                max_market_pairs_per_event=max_market_pairs_per_event,
                scan_kwargs=scan_kwargs,
                issues=issues,
            )
        mb_events = [_as_normalized(item) for item in cluster.events_for(VenueName.MATCHBOOK)]
        pm_events = [_as_normalized(item) for item in cluster.events_for(VenueName.POLYMARKET)]
        k_events = [_as_normalized(item) for item in cluster.events_for(VenueName.KALSHI)]
        enabled = self._op_enabled_venues
        if VenueName.MATCHBOOK not in enabled:
            mb_events = []
        if VenueName.POLYMARKET not in enabled:
            pm_events = []
        if VenueName.KALSHI not in enabled:
            k_events = []
        mb_events = [item for item in mb_events if item is not None]
        pm_events = [item for item in pm_events if item is not None]
        k_events = [item for item in k_events if item is not None]
        mb_event = mb_events[0] if mb_events else None
        matchbook_side = _VenueSideFetch()
        polymarket_side = _VenueSideFetch()
        kalshi_side = _VenueSideFetch()
        observations_by_market_id: dict[tuple[VenueName, str], VenueMarketObservation] = {}
        market_counts: dict[VenueName, int] = {}
        order_books_fetched = 0
        listed_venues: set[VenueName] = set()
        fetch_unavailable = False
        await asyncio.gather(
            self._fetch_matchbook_cluster_side(
                mb_events,
                side=matchbook_side,
                matchbook_market_filters=matchbook_market_filters,
                issues=issues,
                fixture=fixture,
            ),
            self._fetch_polymarket_cluster_side(
                pm_events,
                side=polymarket_side,
                polymarket_market_filters=polymarket_market_filters,
                issues=issues,
                fixture=fixture,
            ),
            self._fetch_kalshi_cluster_side(
                k_events,
                side=kalshi_side,
                issues=issues,
                fixture=fixture,
            ),
        )
        matchbook_markets = matchbook_side.markets
        matchbook_inventory = matchbook_side.inventory
        polymarket_markets = polymarket_side.markets
        polymarket_inventory = polymarket_side.inventory
        kalshi_markets = kalshi_side.markets
        kalshi_inventory = kalshi_side.inventory
        if matchbook_side.primary_event is not None:
            mb_event = matchbook_side.primary_event
        if matchbook_side.listed:
            listed_venues.add(VenueName.MATCHBOOK)
        if polymarket_side.listed:
            listed_venues.add(VenueName.POLYMARKET)
        if kalshi_side.listed:
            listed_venues.add(VenueName.KALSHI)
        fetch_unavailable = matchbook_side.failed or polymarket_side.failed or kalshi_side.failed
        order_books_fetched = (
            matchbook_side.books + polymarket_side.books + kalshi_side.books
        )
        for source_id, observation in matchbook_side.observations.items():
            observations_by_market_id[(VenueName.MATCHBOOK, source_id)] = observation
        for source_id, observation in polymarket_side.observations.items():
            observations_by_market_id[(VenueName.POLYMARKET, source_id)] = observation
        for source_id, observation in kalshi_side.observations.items():
            observations_by_market_id[(VenueName.KALSHI, source_id)] = observation
        if matchbook_markets:
            market_counts[VenueName.MATCHBOOK] = len(matchbook_markets)
        if polymarket_markets:
            market_counts[VenueName.POLYMARKET] = len(polymarket_markets)
        if kalshi_markets:
            market_counts[VenueName.KALSHI] = len(kalshi_markets)

        venue_markets = {
            VenueName.MATCHBOOK: matchbook_markets,
            VenueName.POLYMARKET: polymarket_markets,
            VenueName.KALSHI: kalshi_markets,
        }
        compared_enough_venues = len(listed_venues) >= 2 or cluster.venue_count < 2
        if self._provider_budget_exhausted() and not compared_enough_venues:
            leftover = _fixture_from_cluster(
                cluster,
                seen_at=seen_at,
                polymarket_events=polymarket_events,
                queried_series_ids=queried_series_ids,
                leftover=True,
            )
            leftover.discovered_market_count = (
                len(matchbook_inventory) + len(polymarket_inventory) + len(kalshi_inventory)
            )
            return leftover, [], [], market_counts, order_books_fetched, 0
        pair_specs = (
            (VenueName.MATCHBOOK, VenueName.POLYMARKET),
            (VenueName.MATCHBOOK, VenueName.KALSHI),
            (VenueName.POLYMARKET, VenueName.KALSHI),
        )
        if not comparison_allowed(enabled):
            pair_specs = ()
            if fixture.no_comparison_reason is None:
                fixture.no_comparison_reason = INSUFFICIENT_VENUES_REASON
        else:
            pair_specs = tuple(
                pair for pair in pair_specs if pair[0] in enabled and pair[1] in enabled
            )
        await self._enrich_kalshi_catalogue_candidate_settlement(
            k_events,
            venue_markets=venue_markets,
            kalshi_side=kalshi_side,
            pair_specs=pair_specs,
            issues=issues,
        )
        kalshi_markets = kalshi_side.markets
        venue_markets[VenueName.KALSHI] = kalshi_markets
        selected_pairs: list[
            tuple[VenueName, VenueName, _NormalizedMarket, _NormalizedMarket, MarketMatchResult]
        ] = []
        matched_market_pairs = 0
        for left_venue, right_venue in pair_specs:
            left_markets = venue_markets[left_venue]
            right_markets = venue_markets[right_venue]
            if not left_markets or not right_markets:
                continue
            market_pairs = _select_prioritized_market_pairs(
                _greedy_unique_market_pairs(
                    left_markets, right_markets, matcher=self.market_matcher
                ),
                max_market_pairs_per_event,
            )
            matched_market_pairs += len(market_pairs)
            selected_pairs.extend(
                (left_venue, right_venue, left_market, right_market, match)
                for left_market, right_market, match in market_pairs
            )
        eligible_pairs = [
            item
            for item in selected_pairs
            if scan_eligible_pair(item[2].canonical, item[3].canonical, item[4])
        ]
        kalshi_depth_markets = [
            market
            for market in _kalshi_markets_needing_depth(eligible_pairs)
            if not _kalshi_market_has_failed_ticker(
                market, self._kalshi_get_market_failed_tickers
            )
        ]
        eligible_ids = {item.canonical.source_market_id for item in kalshi_depth_markets}
        self._kalshi_books_eligible += len(kalshi_depth_markets)
        leftover_kalshi = [
            market
            for market in kalshi_markets
            if market.canonical.source_market_id not in eligible_ids
        ]
        # Count every listed Kalshi market that is not a scan-eligible /
        # Approved Market Catalogue pair leg. Do not infer this from missing
        # observations after a leftover depth attempt — leftover books are
        # never fetched.
        self._kalshi_books_skipped_unapproved += len(leftover_kalshi)
        await self._persist_universe_catalogue_from_pairs(
            fixture=fixture,
            eligible_pairs=eligible_pairs,
            k_events=k_events,
            family_discovery=FamilyDiscoveryCompleteness(
                matchbook_listing_complete=matchbook_side.listing_complete,
                kalshi_series_results=tuple(
                    self._op_series_results.get(VenueName.KALSHI.value) or ()
                ),
                kalshi_incomplete_family_keys=frozenset(
                    kalshi_side.kalshi_incomplete_family_keys
                ),
                target_competition_code=fixture.target_competition_code,
            ),
            issues=issues,
        )
        if kalshi_depth_markets:
            await self._fetch_kalshi_depth_for_markets(
                k_events,
                kalshi_depth_markets,
                side=kalshi_side,
                issues=issues,
            )
        order_books_fetched = (
            matchbook_side.books + polymarket_side.books + kalshi_side.books
        )
        for source_id, observation in kalshi_side.observations.items():
            observations_by_market_id[(VenueName.KALSHI, source_id)] = observation
        decisions: list[PaperScanDecision] = []
        decisions_by_source_ids: dict[tuple[str, str], PaperScanDecision] = {}
        decisions_by_pair: dict[tuple[str, str, str, str], PaperScanDecision] = {}
        headline_applies: list[tuple] = []
        for left_venue, right_venue, left_market, right_market, _match in eligible_pairs:
            left_obs = observations_by_market_id.get(
                (left_venue, left_market.canonical.source_market_id)
            )
            right_obs = observations_by_market_id.get(
                (right_venue, right_market.canonical.source_market_id)
            )
            if left_obs is None or right_obs is None:
                if fixture.current_net_edge is None:
                    fixture.no_comparison_reason = "order_book_unavailable"
                continue
            stored = self.paper_scan.scan_pair(
                left_obs,
                right_obs,
                fixture_canonical_event_id=fixture.canonical_event_id,
                **scan_kwargs,
            )
            phases = getattr(self.paper_scan, "last_scan_phase_ms", None) or {}
            self._attribution.add(
                stage="mapping_equivalence",
                elapsed_ms=int(phases.get("mapping_equivalence", 0)),
                calls=1,
            )
            self._attribution.add(
                stage="fees_fx_risk",
                elapsed_ms=int(phases.get("fees_fx_risk", 0)),
                calls=1,
            )
            self._attribution.add(
                stage="solver_allocation",
                elapsed_ms=int(phases.get("solver_allocation", 0)),
                calls=1,
            )
            if mb_event is not None:
                state = matchbook_fixture_state(mb_event.raw)
                stored = stored.model_copy(
                    update={
                        "fixture_discovery_source": cluster.anchor.venue,
                        "fixture_status": state.venue_status,
                        "in_running": state.in_running,
                        "live_score_supported": state.live_score_supported,
                        "home_score": state.home_score,
                        "away_score": state.away_score,
                    }
                )
            else:
                stored = stored.model_copy(
                    update={"fixture_discovery_source": cluster.anchor.venue}
                )
            decisions.append(stored)
            decisions_by_source_ids[
                (left_market.canonical.source_market_id, right_market.canonical.source_market_id)
            ] = stored
            decisions_by_pair[
                (
                    left_obs.venue.value,
                    left_market.canonical.source_market_id,
                    right_obs.venue.value,
                    right_market.canonical.source_market_id,
                )
            ] = stored
            headline_applies.append((left_market, left_obs, right_obs, stored))

        inventory_rows = assemble_fixture_inventory(
            matchbook_inventory,
            polymarket_inventory,
            kalshi_markets=kalshi_inventory,
            matcher=self.market_matcher,
            decisions_by_source_ids=decisions_by_source_ids,
            decisions_by_pair=decisions_by_pair,
            venue_costs=scan_kwargs.get("venue_costs"),
            fx_snapshots=scan_kwargs.get("fx_snapshots"),
            cost_resolver=self.paper_scan.cost_resolver,
        )
        discovered_count, equivalent_count, _observed_edge = inventory_summary(inventory_rows)
        fixture.discovered_market_count = discovered_count
        fixture.matched_market_count = matched_market_pairs
        compared_enough_venues = len(listed_venues) >= 2 or cluster.venue_count < 2
        if self._provider_budget_exhausted() and not compared_enough_venues:
            leftover = _fixture_from_cluster(
                cluster,
                seen_at=seen_at,
                polymarket_events=polymarket_events,
                queried_series_ids=queried_series_ids,
                leftover=True,
            )
            leftover.discovered_market_count = discovered_count
            leftover.catalogue_coverage = fixture_catalogue_coverage(
                inventory_rows,
                matchbook_matched=bool(leftover.matchbook_matched),
                kalshi_matched=bool(leftover.kalshi_matched),
                polymarket_matched=bool(leftover.polymarket_matched),
                target_competition_code=leftover.target_competition_code,
            )
            return leftover, decisions, inventory_rows, market_counts, order_books_fetched, 0
        if fetch_unavailable and not compared_enough_venues:
            fixture.market_evaluation_state = MarketEvaluationState.MARKET_FETCH_UNAVAILABLE.value
            fixture.market_evaluation_reason = MARKET_FETCH_UNAVAILABLE_REASON
            fixture.matched_equivalent_count = None
            fixture.no_comparison_reason = MARKET_FETCH_UNAVAILABLE_REASON
        else:
            fixture.market_evaluation_state = MarketEvaluationState.EVALUATED.value
            fixture.market_evaluation_reason = None
            fixture.matched_equivalent_count = equivalent_count
            _apply_fixture_headline(fixture, headline_applies)
            if cluster.venue_count >= 2 and equivalent_count == 0:
                if fetch_unavailable and fixture.no_comparison_reason is None:
                    fixture.no_comparison_reason = MARKET_FETCH_UNAVAILABLE_REASON
                else:
                    fixture.no_comparison_reason = zero_equivalent_reason_from_inventory(
                        inventory_rows,
                        existing_reason=fixture.no_comparison_reason,
                    )
        fixture.opportunity_state = _opportunity_state(fixture)
        fixture.catalogue_coverage = fixture_catalogue_coverage(
            inventory_rows,
            matchbook_matched=bool(fixture.matchbook_matched),
            kalshi_matched=bool(fixture.kalshi_matched),
            polymarket_matched=bool(fixture.polymarket_matched),
            target_competition_code=fixture.target_competition_code,
        )
        return (
            fixture,
            decisions,
            inventory_rows,
            market_counts,
            order_books_fetched,
            matched_market_pairs,
        )

    async def _mark_universe_catalogue_terminal(self, fixture: DiscoveredFixture) -> None:
        store = self.catalogue_store
        if store is None or self._op_request_lane == ScanLane.HOT.value:
            return
        generation = self._op_universe_generation_id
        async with self._catalogue_persist_sema:
            await persist_universe_catalogue_pass_offloop(
                store,
                canonical_event_id=fixture.canonical_event_id,
                competition=fixture.target_competition_code or fixture.competition,
                home_canonical=fixture.home_team,
                away_canonical=fixture.away_team,
                kickoff_utc=fixture.kickoff_utc,
                pairs=[],
                now=datetime.now(UTC),
                generation_id=None if generation is None else str(generation),
                family_discovery=FamilyDiscoveryCompleteness(),
                terminal=True,
                generation_selected_codes=self._op_selected_competition_codes,
                allow_disappearance=not self._op_generation_superseded,
            )

    async def _persist_universe_catalogue_from_pairs(
        self,
        *,
        fixture: DiscoveredFixture,
        eligible_pairs: list[
            tuple[VenueName, VenueName, _NormalizedMarket, _NormalizedMarket, MarketMatchResult]
        ],
        k_events: list[_NormalizedEvent],
        family_discovery: FamilyDiscoveryCompleteness,
        issues: list[CollectorIssue],
    ) -> None:
        """Persist approved-family identity before depth/solver (Tenet 19 / #341)."""

        store = self.catalogue_store
        if store is None or self._op_request_lane == ScanLane.HOT.value:
            return
        identities = []
        series_by_event: dict[str, dict[str, Any] | None] = {}
        for left_venue, right_venue, left_market, right_market, _match in eligible_pairs:
            venues = {left_venue, right_venue}
            if VenueName.KALSHI in venues:
                kalshi_market = left_market if left_venue is VenueName.KALSHI else right_market
                k_event = _event_for_source(k_events, kalshi_market.canonical.event.source_event_id)
                event_payload = k_event.raw if k_event is not None and isinstance(k_event.raw, dict) else {}
                source_event_id = str(kalshi_market.canonical.event.source_event_id)
                if source_event_id not in series_by_event:
                    series = None
                    if isinstance(kalshi_market.raw, dict) and isinstance(kalshi_market.raw.get("series"), dict):
                        series = kalshi_market.raw.get("series")
                    elif k_event is not None:
                        series = await self._fetch_kalshi_series_metadata(
                            k_event,
                            issues=issues,
                            attach_contract_family=False,
                        )
                    series_by_event[source_event_id] = series if isinstance(series, dict) else None
                series_payload = series_by_event.get(source_event_id)
            else:
                event_payload = {}
                series_payload = None
            identity = pair_identity_from_markets(
                left_market.canonical,
                right_market.canonical,
                kalshi_event_payload=event_payload,
                kalshi_series_payload=series_payload,
                fee_source=FEE_SOURCE_GET_SERIES,
            )
            if identity is not None:
                identities.append(identity)
        generation = self._op_universe_generation_id
        async with self._catalogue_persist_sema:
            await persist_universe_catalogue_pass_offloop(
                store,
                canonical_event_id=fixture.canonical_event_id,
                competition=fixture.target_competition_code or fixture.competition,
                home_canonical=fixture.home_team,
                away_canonical=fixture.away_team,
                kickoff_utc=fixture.kickoff_utc,
                pairs=identities,
                now=datetime.now(UTC),
                generation_id=None if generation is None else str(generation),
                family_discovery=family_discovery,
                terminal=False,
                generation_selected_codes=self._op_selected_competition_codes,
                allow_disappearance=not self._op_generation_superseded,
            )

    async def _refresh_hot_cluster(
        self,
        cluster: FixtureCluster,
        *,
        fixture: DiscoveredFixture,
        seen_at: datetime,
        polymarket_events: list[_NormalizedEvent],
        queried_series_ids: list[str] | None,
        max_market_pairs_per_event: int,
        scan_kwargs: dict[str, Any],
        issues: list[CollectorIssue],
    ) -> tuple[
        DiscoveredFixture,
        list[PaperScanDecision],
        list[FixtureMarketInventoryRow],
        dict[VenueName, int],
        int,
        int,
    ]:
        del max_market_pairs_per_event
        relationships = self._hot_relationships_for_cluster(cluster, fixture)
        if not relationships:
            self._bump_hot_stat("missing")
            issues.append(
                CollectorIssue(
                    stage="hot_refresh",
                    source_id=fixture.canonical_event_id,
                    detail=HOT_RELATIONSHIP_MISSING_REASON,
                )
            )
            fixture.market_evaluation_state = MarketEvaluationState.HOT_RELATIONSHIP_MISSING.value
            fixture.market_evaluation_reason = HOT_RELATIONSHIP_MISSING_REASON
            fixture.no_comparison_reason = HOT_RELATIONSHIP_MISSING_REASON
            fixture.matched_equivalent_count = None
            fixture.opportunity_state = _opportunity_state(fixture)
            return fixture, [], [], {}, 0, 0

        mb_events = [_as_normalized(item) for item in cluster.events_for(VenueName.MATCHBOOK)]
        pm_events = [_as_normalized(item) for item in cluster.events_for(VenueName.POLYMARKET)]
        k_events = [_as_normalized(item) for item in cluster.events_for(VenueName.KALSHI)]
        enabled = self._op_enabled_venues
        if VenueName.MATCHBOOK not in enabled:
            mb_events = []
        if VenueName.POLYMARKET not in enabled:
            pm_events = []
        if VenueName.KALSHI not in enabled:
            k_events = []
        mb_events = [item for item in mb_events if item is not None]
        pm_events = [item for item in pm_events if item is not None]
        k_events = [item for item in k_events if item is not None]
        mb_event = mb_events[0] if mb_events else None

        inventory_rows: list[FixtureMarketInventoryRow] = []
        decisions: list[PaperScanDecision] = []
        headline_applies: list[tuple] = []
        market_counts: dict[VenueName, int] = {}
        order_books_fetched = 0
        matched_market_pairs = 0
        revalidation = 0
        timeouts = 0
        refreshed = 0

        for relationship in relationships:
            if self._provider_budget_exhausted():
                break
            row, pair_decisions, pair_headlines, books, pair_matched, outcome = (
                await self._refresh_hot_relationship(
                    relationship,
                    fixture=fixture,
                    mb_events=mb_events,
                    pm_events=pm_events,
                    k_events=k_events,
                    scan_kwargs=scan_kwargs,
                    issues=issues,
                )
            )
            order_books_fetched += books
            if pair_matched:
                matched_market_pairs += pair_matched
            if outcome == "refreshed":
                refreshed += 1
            elif outcome == "revalidation":
                revalidation += 1
            elif outcome == "unavailable":
                timeouts += 1
            if row is not None:
                inventory_rows.append(row)
                for venue in (VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI):
                    facts = getattr(row, venue.value)
                    if facts is None:
                        continue
                    market_counts[venue] = market_counts.get(venue, 0) + 1
            for stored in pair_decisions:
                if mb_event is not None:
                    state = matchbook_fixture_state(mb_event.raw)
                    stored = stored.model_copy(
                        update={
                            "fixture_discovery_source": cluster.anchor.venue,
                            "fixture_status": state.venue_status,
                            "in_running": state.in_running,
                            "live_score_supported": state.live_score_supported,
                            "home_score": state.home_score,
                            "away_score": state.away_score,
                        }
                    )
                else:
                    stored = stored.model_copy(
                        update={"fixture_discovery_source": cluster.anchor.venue}
                    )
                decisions.append(stored)
            headline_applies.extend(pair_headlines)

        self._bump_hot_stat("refreshed", refreshed)
        self._bump_hot_stat("revalidation", revalidation)
        discovered_count, equivalent_count, _observed_edge = inventory_summary(inventory_rows)
        fixture.discovered_market_count = discovered_count
        fixture.matched_market_count = matched_market_pairs
        if not inventory_rows and timeouts and not refreshed and not revalidation:
            fixture.market_evaluation_state = MarketEvaluationState.MARKET_FETCH_UNAVAILABLE.value
            fixture.market_evaluation_reason = HOT_RELATIONSHIP_UNAVAILABLE_REASON
            fixture.matched_equivalent_count = None
            fixture.no_comparison_reason = HOT_RELATIONSHIP_UNAVAILABLE_REASON
        else:
            fixture.market_evaluation_state = MarketEvaluationState.EVALUATED.value
            fixture.market_evaluation_reason = (
                HOT_REVALIDATION_NEEDED_REASON if revalidation and not refreshed else None
            )
            fixture.matched_equivalent_count = equivalent_count
            _apply_fixture_headline(fixture, headline_applies)
            if cluster.venue_count >= 2 and equivalent_count == 0:
                fixture.no_comparison_reason = zero_equivalent_reason_from_inventory(
                    inventory_rows,
                    existing_reason=fixture.no_comparison_reason,
                )
        fixture.opportunity_state = _opportunity_state(fixture)
        fixture.catalogue_coverage = fixture_catalogue_coverage(
            inventory_rows,
            matchbook_matched=bool(fixture.matchbook_matched),
            kalshi_matched=bool(fixture.kalshi_matched),
            polymarket_matched=bool(fixture.polymarket_matched),
            target_competition_code=fixture.target_competition_code,
        )
        return (
            fixture,
            decisions,
            inventory_rows,
            market_counts,
            order_books_fetched,
            matched_market_pairs,
        )

    def _hot_relationships_for_cluster(
        self,
        cluster: FixtureCluster,
        fixture: DiscoveredFixture,
    ) -> list[HotMarketRelationship]:
        by_canonical = getattr(self, "_op_hot_relationships", {}) or {}
        by_source = getattr(self, "_op_hot_relationships_by_source", {}) or {}
        found: list[HotMarketRelationship] = []
        seen: set[str] = set()
        identities = [
            fixture.canonical_event_id,
            cluster_canonical_event_id(cluster),
            *cluster_identity_aliases(cluster).keys(),
        ]
        for ident in identities:
            for relationship in by_canonical.get(str(ident), []):
                if relationship.market_key in seen:
                    continue
                seen.add(relationship.market_key)
                found.append(relationship)
        for event in cluster_member_events(cluster):
            key = f"{event.venue.value}:{event.source_event_id}"
            for relationship in by_source.get(key, []):
                if relationship.market_key in seen:
                    continue
                seen.add(relationship.market_key)
                found.append(relationship)
        return found

    def _bump_hot_stat(self, key: str, amount: int = 1) -> None:
        stats = getattr(self, "_op_hot_stats", None)
        if not isinstance(stats, dict) or amount == 0:
            return
        stats[key] = int(stats.get(key) or 0) + int(amount)

    async def _refresh_hot_relationship(
        self,
        relationship: HotMarketRelationship,
        *,
        fixture: DiscoveredFixture,
        mb_events: list[_NormalizedEvent],
        pm_events: list[_NormalizedEvent],
        k_events: list[_NormalizedEvent],
        scan_kwargs: dict[str, Any],
        issues: list[CollectorIssue],
    ) -> tuple[
        FixtureMarketInventoryRow | None,
        list[PaperScanDecision],
        list[tuple],
        int,
        int,
        str,
    ]:
        legs: list[tuple[VenueName, HotVenueLeg, list[_NormalizedEvent]]] = []
        if relationship.matchbook is not None:
            legs.append((VenueName.MATCHBOOK, relationship.matchbook, mb_events))
        if relationship.polymarket is not None:
            legs.append((VenueName.POLYMARKET, relationship.polymarket, pm_events))
        if relationship.kalshi is not None:
            legs.append((VenueName.KALSHI, relationship.kalshi, k_events))
        refreshed: dict[VenueName, _HotLegRefresh] = {}
        books = 0
        for venue, leg, events in legs:
            result = await self._refresh_hot_venue_leg(
                venue,
                leg,
                relationship=relationship,
                events=events,
                issues=issues,
            )
            refreshed[venue] = result
            books += result.books
            if result.gone or result.identity_changed:
                self._note_hot_revalidation(issues, relationship, venue, result)
                return (
                    fail_closed_inventory_row(
                        relationship, reason=HOT_REVALIDATION_NEEDED_REASON
                    ),
                    [],
                    [],
                    books,
                    0,
                    "revalidation",
                )
        if any(item.unavailable or item.inventory is None for item in refreshed.values()):
            return None, [], [], books, 0, "unavailable"
        inventories = [item.inventory for item in refreshed.values() if item.inventory is not None]
        observations = {
            venue: item.observation
            for venue, item in refreshed.items()
            if item.observation is not None
        }
        pair_venues = [
            (left, right)
            for left, right in (
                (VenueName.MATCHBOOK, VenueName.POLYMARKET),
                (VenueName.MATCHBOOK, VenueName.KALSHI),
                (VenueName.POLYMARKET, VenueName.KALSHI),
            )
            if left in refreshed and right in refreshed
        ]
        if not pair_venues:
            return None, [], [], books, 0, "unavailable"
        decisions: list[PaperScanDecision] = []
        headlines: list[tuple] = []
        decisions_by_source_ids: dict[tuple[str, str], PaperScanDecision] = {}
        decisions_by_pair: dict[tuple[str, str, str, str], PaperScanDecision] = {}
        matched_pairs = 0
        for left_venue, right_venue in pair_venues:
            left_inv = refreshed[left_venue].inventory
            right_inv = refreshed[right_venue].inventory
            left_obs = observations.get(left_venue)
            right_obs = observations.get(right_venue)
            if (
                left_inv is None
                or right_inv is None
                or left_inv.canonical is None
                or right_inv.canonical is None
                or left_obs is None
                or right_obs is None
            ):
                continue
            match = self.market_matcher.match(left_inv.canonical, right_inv.canonical)
            if not scan_eligible_pair(left_inv.canonical, right_inv.canonical, match):
                issues.append(
                    CollectorIssue(
                        stage="hot_refresh",
                        source_id=relationship.market_key,
                        detail=HOT_REVALIDATION_NEEDED_REASON,
                    )
                )
                return (
                    fail_closed_inventory_row(
                        relationship, reason=HOT_REVALIDATION_NEEDED_REASON
                    ),
                    [],
                    [],
                    books,
                    0,
                    "revalidation",
                )
            stored = self.paper_scan.scan_pair(
                left_obs,
                right_obs,
                fixture_canonical_event_id=fixture.canonical_event_id,
                **scan_kwargs,
            )
            phases = getattr(self.paper_scan, "last_scan_phase_ms", None) or {}
            self._attribution.add(
                stage="mapping_equivalence",
                elapsed_ms=int(phases.get("mapping_equivalence", 0)),
                calls=1,
            )
            self._attribution.add(
                stage="fees_fx_risk",
                elapsed_ms=int(phases.get("fees_fx_risk", 0)),
                calls=1,
            )
            self._attribution.add(
                stage="solver_allocation",
                elapsed_ms=int(phases.get("solver_allocation", 0)),
                calls=1,
            )
            decisions.append(stored)
            decisions_by_source_ids[
                (left_inv.canonical.source_market_id, right_inv.canonical.source_market_id)
            ] = stored
            decisions_by_pair[
                (
                    left_obs.venue.value,
                    left_inv.canonical.source_market_id,
                    right_obs.venue.value,
                    right_inv.canonical.source_market_id,
                )
            ] = stored
            headlines.append(
                (
                    _NormalizedMarket(left_inv.canonical.model_dump(mode="json"), left_inv.canonical),
                    left_obs,
                    right_obs,
                    stored,
                )
            )
            matched_pairs += 1
        if not matched_pairs:
            return None, [], [], books, 0, "unavailable"
        matchbook_inv = [item for item in inventories if item.venue is VenueName.MATCHBOOK]
        polymarket_inv = [item for item in inventories if item.venue is VenueName.POLYMARKET]
        kalshi_inv = [item for item in inventories if item.venue is VenueName.KALSHI]
        rows = assemble_fixture_inventory(
            matchbook_inv,
            polymarket_inv,
            kalshi_markets=kalshi_inv,
            matcher=self.market_matcher,
            decisions_by_source_ids=decisions_by_source_ids,
            decisions_by_pair=decisions_by_pair,
            venue_costs=scan_kwargs.get("venue_costs"),
            fx_snapshots=scan_kwargs.get("fx_snapshots"),
            cost_resolver=self.paper_scan.cost_resolver,
        )
        matched = [
            row
            for row in rows
            if inventory_is_comparable_opportunity(row.comparison_status)
        ]
        chosen = matched[0] if matched else (rows[0] if rows else None)
        if chosen is None:
            return None, [], [], books, 0, "unavailable"
        return chosen, decisions, headlines, books, matched_pairs, "refreshed"

    def _note_hot_revalidation(
        self,
        issues: list[CollectorIssue],
        relationship: HotMarketRelationship,
        venue: VenueName,
        result: _HotLegRefresh,
    ) -> None:
        detail = HOT_REVALIDATION_NEEDED_REASON
        if result.gone:
            detail = f"{HOT_REVALIDATION_NEEDED_REASON}:gone"
        elif result.identity_changed:
            detail = f"{HOT_REVALIDATION_NEEDED_REASON}:identity"
        issues.append(
            CollectorIssue(
                stage="hot_refresh",
                venue=venue,
                source_id=relationship.market_key,
                detail=detail,
            )
        )

    async def _refresh_hot_venue_leg(
        self,
        venue: VenueName,
        leg: HotVenueLeg,
        *,
        relationship: HotMarketRelationship,
        events: list[_NormalizedEvent],
        issues: list[CollectorIssue],
    ) -> _HotLegRefresh:
        if venue is VenueName.MATCHBOOK:
            return await self._refresh_hot_matchbook_leg(
                leg, relationship=relationship, events=events, issues=issues
            )
        if venue is VenueName.KALSHI:
            return await self._refresh_hot_kalshi_leg(
                leg, relationship=relationship, events=events, issues=issues
            )
        return await self._refresh_hot_polymarket_leg(
            leg, relationship=relationship, events=events, issues=issues
        )

    async def _refresh_hot_matchbook_leg(
        self,
        leg: HotVenueLeg,
        *,
        relationship: HotMarketRelationship,
        events: list[_NormalizedEvent],
        issues: list[CollectorIssue],
    ) -> _HotLegRefresh:
        result = _HotLegRefresh()
        getter = getattr(self.matchbook, "get_market", None)
        event = _event_for_source(events, leg.source_event_id)
        if not callable(getter) or event is None:
            result.unavailable = True
            return result
        try:
            started = perf_counter()
            payload, failed = await self._wait_provider(
                getter(leg.source_event_id, leg.source_market_id),
                stage="get_market",
                venue=VenueName.MATCHBOOK,
                source_id=leg.source_market_id,
                default=None,
            )
        except MatchbookMarketGoneError:
            result.gone = True
            self._bump_hot_stat("matchbook_get_market")
            return result
        self._bump_hot_stat("matchbook_get_market")
        if failed or payload is None:
            result.unavailable = True
            return result
        market_payload = extract_matchbook_market_payload(payload)
        if market_payload is None:
            result.gone = True
            return result
        if matchbook_payload_is_terminal(market_payload):
            result.gone = True
            return result
        markets, inventory = self._inventory_markets(
            event, [market_payload], venue=VenueName.MATCHBOOK, issues=issues
        )
        if not markets:
            result.gone = True
            return result
        market = markets[0]
        if not hot_identity_matches_market(
            family=market.canonical.family.value if market.canonical.family else None,
            period=market.canonical.period.value if market.canonical.period else None,
            line=market.canonical.line,
            settlement_key=(
                market.canonical.settlement.deterministic_key()
                if market.canonical.settlement
                else None
            ),
            source_market_id=market.canonical.source_market_id,
            expected=leg,
        ):
            result.identity_changed = True
            return result
        observation = self._try_matchbook_observation(
            event,
            market,
            retrieved_at=datetime.now(UTC),
            latency_ms=_elapsed_ms(started),
            issues=issues,
        )
        if observation is None:
            result.unavailable = True
            return result
        item = inventory[0] if inventory else _inventory_from_observation(observation)
        item.observation = observation
        result.inventory = item
        result.observation = observation
        result.books = 1
        return result

    async def _refresh_hot_kalshi_leg(
        self,
        leg: HotVenueLeg,
        *,
        relationship: HotMarketRelationship,
        events: list[_NormalizedEvent],
        issues: list[CollectorIssue],
    ) -> _HotLegRefresh:
        del relationship
        result = _HotLegRefresh()
        if self.kalshi is None:
            result.unavailable = True
            return result
        event = _event_for_source(events, leg.source_event_id)
        canonical = canonical_market_from_leg(leg)
        tickers = kalshi_tickers_for_leg(leg)
        client = self.kalshi
        if event is None or canonical is None or not tickers or client is None:
            result.unavailable = True
            return result
        books_by_ticker: dict[str, dict[str, Any]] = {}
        fetched = 0
        latency_ms = 0
        failed = False

        async def _one(ticker: str) -> None:
            nonlocal fetched, latency_ms, failed
            if failed or not ticker:
                return
            try:
                started = perf_counter()
                raw_book, book_failed = await self._wait_provider(
                    client.get_order_book(
                        event.canonical.source_event_id,
                        ticker,
                    ),
                    stage="order_book",
                    venue=VenueName.KALSHI,
                    source_id=ticker,
                    default=None,
                )
                if book_failed or raw_book is None:
                    failed = True
                    return
                latency_ms += _elapsed_ms(started)
                books_by_ticker[ticker] = raw_book
                fetched += 1
            except Exception as exc:
                issues.append(
                    CollectorIssue(
                        stage="order_book",
                        venue=VenueName.KALSHI,
                        source_id=ticker,
                        detail=str(exc),
                    )
                )
                failed = True

        await asyncio.gather(*[_one(ticker) for ticker in tickers])
        self._bump_hot_stat("kalshi_order_books", fetched)
        result.books = fetched
        if failed:
            result.unavailable = True
            return result
        evaluated_at = datetime.now(UTC)
        age = retrieval_quote_age(retrieved_at=evaluated_at, evaluated_at=evaluated_at)
        fee_snapshot = leg.fee_snapshot
        if not fee_snapshot:
            # Persisted UNIVERSE event payload may already carry fee overrides.
            # This is not Get Series / Get Market contract proof.
            fee_snapshot = resolve_kalshi_fee_metadata(
                event=event.raw if isinstance(event.raw, dict) else None,
                series=event.raw.get("series") if isinstance(event.raw, dict) else None,
            )
        try:
            observation = self.kalshi_builder.build_from_canonical(
                canonical,
                books_by_ticker,
                observed_at=evaluated_at,
                source_latency_ms=latency_ms,
                quote_age_ms=age.quote_age_ms,
                quote_age_basis=age.basis,
                quote_age_reason=age.reason,
                fee_snapshot=fee_snapshot,
            )
        except (VenueNormalizationError, ValueError) as exc:
            issues.append(
                CollectorIssue(
                    stage="build_observation",
                    venue=VenueName.KALSHI,
                    source_id=leg.source_market_id,
                    detail=str(exc),
                )
            )
            result.unavailable = True
            return result
        result.observation = observation
        result.inventory = _inventory_from_observation(observation)
        return result

    async def _refresh_hot_polymarket_leg(
        self,
        leg: HotVenueLeg,
        *,
        relationship: HotMarketRelationship,
        events: list[_NormalizedEvent],
        issues: list[CollectorIssue],
    ) -> _HotLegRefresh:
        del relationship
        result = _HotLegRefresh()
        event = _event_for_source(events, leg.source_event_id)
        tokens = [item for item in leg.source_runner_ids if str(item).strip()]
        if event is None or not tokens:
            result.unavailable = True
            return result
        books_by_token, latency_ms, fetched, failed = await self._fetch_polymarket_token_books(
            event,
            tokens,
            market_id=leg.source_market_id,
            issues=issues,
        )
        result.books = fetched
        if failed:
            result.unavailable = True
            return result
        canonical = canonical_market_from_leg(leg)
        if canonical is None:
            result.unavailable = True
            return result
        market = _NormalizedMarket(
            {"id": leg.source_market_id, "question": canonical.family.value},
            canonical,
        )
        observation = self._try_polymarket_observation(
            event,
            market,
            books_by_token,
            latency_ms=latency_ms,
            issues=issues,
        )
        if observation is None:
            result.unavailable = True
            return result
        if isinstance(leg.fee_snapshot, dict) and leg.fee_snapshot:
            observation.metadata["polymarket_fee"] = dict(leg.fee_snapshot)
        result.observation = observation
        result.inventory = _inventory_from_observation(observation)
        return result

    async def _fetch_matchbook_cluster_side(
        self,
        mb_events: list[_NormalizedEvent],
        *,
        side: _VenueSideFetch,
        matchbook_market_filters: dict[str, Any],
        issues: list[CollectorIssue],
        fixture: DiscoveredFixture,
    ) -> None:
        if not mb_events:
            side.listing_complete = False
            return
        side.primary_event = mb_events[0]
        processed = 0
        truncated = False
        for mb_event in mb_events:
            if self._provider_budget_exhausted():
                truncated = True
                break
            mb_listed = True
            try:
                mb_started = perf_counter()
                mb_market_payload, mb_failed = await self._wait_provider(
                    self.matchbook.list_markets(
                        mb_event.canonical.source_event_id,
                        **matchbook_market_filters,
                    ),
                    stage="list_markets",
                    venue=VenueName.MATCHBOOK,
                    source_id=mb_event.canonical.source_event_id,
                    default={"markets": []},
                )
                side.latency_ms = _elapsed_ms(mb_started)
                side.retrieved_at = datetime.now(UTC)
                if mb_failed:
                    side.failed = True
                    mb_listed = False
                    if fixture.no_comparison_reason is None:
                        fixture.no_comparison_reason = MARKET_FETCH_UNAVAILABLE_REASON
                    mb_market_payload = {"markets": []}
            except Exception as exc:
                issues.append(
                    CollectorIssue(
                        stage="list_markets",
                        venue=VenueName.MATCHBOOK,
                        source_id=mb_event.canonical.source_event_id,
                        detail=str(exc),
                    )
                )
                fixture.no_comparison_reason = MARKET_FETCH_UNAVAILABLE_REASON
                side.failed = True
                mb_listed = False
                mb_market_payload = {"markets": []}
            if mb_listed:
                side.listed = True
                processed += 1
            if isinstance(mb_market_payload, dict) and mb_market_payload.get("truncated"):
                truncated = True
                issues.extend(_matchbook_market_listing_issues(mb_market_payload))
            raw_matchbook = _extract_matchbook_items(mb_market_payload, "markets")
            markets, inventory = self._inventory_markets(
                mb_event, raw_matchbook, venue=VenueName.MATCHBOOK, issues=issues
            )
            side.markets.extend(markets)
            side.inventory.extend(inventory)
            retrieved_at = side.retrieved_at or datetime.now(UTC)
            for left_market in markets:
                observation = self._try_matchbook_observation(
                    mb_event,
                    left_market,
                    retrieved_at=retrieved_at,
                    latency_ms=side.latency_ms,
                    issues=issues,
                )
                if observation is None:
                    continue
                side.observations[left_market.canonical.source_market_id] = observation
                for item in side.inventory:
                    if item.source_market_id == left_market.canonical.source_market_id:
                        item.observation = observation
        # "At least one listed" is not fixture listing complete. A budget
        # break before later source events must leave the fixture incomplete.
        side.listing_complete = (
            not truncated and not side.failed and processed == len(mb_events)
        )

    async def _fetch_polymarket_cluster_side(
        self,
        pm_events: list[_NormalizedEvent],
        *,
        side: _VenueSideFetch,
        polymarket_market_filters: dict[str, Any],
        issues: list[CollectorIssue],
        fixture: DiscoveredFixture,
    ) -> None:
        if not pm_events:
            return
        pm_event = pm_events[0]
        for event in pm_events:
            if self._provider_budget_exhausted():
                break
            pm_listed = True
            try:
                pm_started = perf_counter()
                pm_market_payload, pm_failed = await self._wait_provider(
                    self.polymarket.list_markets(
                        event.canonical.source_event_id,
                        **polymarket_market_filters,
                    ),
                    stage="list_markets",
                    venue=VenueName.POLYMARKET,
                    source_id=event.canonical.source_event_id,
                    default=[],
                )
                side.latency_ms += _elapsed_ms(pm_started)
                if pm_failed:
                    side.failed = True
                    pm_listed = False
                    if fixture.no_comparison_reason is None:
                        fixture.no_comparison_reason = MARKET_FETCH_UNAVAILABLE_REASON
                    pm_market_payload = []
            except Exception as exc:
                issues.append(
                    CollectorIssue(
                        stage="list_markets",
                        venue=VenueName.POLYMARKET,
                        source_id=event.canonical.source_event_id,
                        detail=str(exc),
                    )
                )
                if fixture.no_comparison_reason is None:
                    fixture.no_comparison_reason = MARKET_FETCH_UNAVAILABLE_REASON
                side.failed = True
                pm_listed = False
                pm_market_payload = []
            if pm_listed:
                side.listed = True
            markets, inventory = self._inventory_markets(
                event,
                [item for item in pm_market_payload if isinstance(item, dict)],
                venue=VenueName.POLYMARKET,
                issues=issues,
            )
            side.markets.extend(markets)
            side.inventory.extend(inventory)
        side.markets, side.inventory = _promote_polymarket_cluster_markets(
            side.markets, side.inventory
        )

        async def _one_market(right_market: _NormalizedMarket) -> None:
            if self._provider_budget_exhausted():
                return
            book_event = _event_for_source(
                pm_events, right_market.canonical.event.source_event_id
            ) or pm_event
            if book_event is None:
                return
            books_by_token, poly_book_latency_ms, fetched, book_failed = (
                await self._fetch_polymarket_books(book_event, right_market, issues=issues)
            )
            side.books += fetched
            if book_failed:
                return
            observation = self._try_polymarket_observation(
                book_event,
                right_market,
                books_by_token,
                latency_ms=side.latency_ms + poly_book_latency_ms,
                issues=issues,
            )
            if observation is None:
                return
            side.observations[right_market.canonical.source_market_id] = observation
            for item in side.inventory:
                if item.source_market_id == right_market.canonical.source_market_id:
                    item.observation = observation

        prioritized = _prioritize_baseline_markets(side.markets)
        if prioritized:
            await asyncio.gather(*[_one_market(market) for market in prioritized])

    async def _fetch_kalshi_cluster_side(
        self,
        k_events: list[_NormalizedEvent],
        *,
        side: _VenueSideFetch,
        issues: list[CollectorIssue],
        fixture: DiscoveredFixture,
    ) -> None:
        if self.kalshi is None or not k_events:
            return
        remaining = list(k_events)
        while remaining:
            k_event = remaining[0]
            family_key = _kalshi_family_key_from_event(k_event)
            if self._provider_budget_exhausted():
                _mark_unprocessed_kalshi_families_incomplete(side, remaining)
                break
            remaining = remaining[1:]
            markets, inventory, _series, fetched, kalshi_failed = await self._load_kalshi_markets(
                k_event, issues=issues
            )
            if kalshi_failed:
                side.failed = True
                if family_key:
                    side.kalshi_incomplete_family_keys.add(family_key)
                if fixture.no_comparison_reason is None:
                    fixture.no_comparison_reason = MARKET_FETCH_UNAVAILABLE_REASON
            else:
                side.listed = True
            side.books += fetched
            side.markets.extend(markets)
            side.inventory.extend(inventory)
        # Live get_order_book is later, and only for Kalshi legs that
        # participate in scan-eligible / Approved Market Catalogue pairs.
        # Cheap nested/list inventory stays here. Series metadata, Get Market
        # rule enrichment, and contract-terms fetches wait for catalogue
        # candidates. Fetching nested unapproved books starves approved BTTS
        # under the 4-slot Kalshi limiter.

    async def _fetch_kalshi_depth_for_markets(
        self,
        k_events: list[_NormalizedEvent],
        markets: list[_NormalizedMarket],
        *,
        side: _VenueSideFetch,
        issues: list[CollectorIssue],
    ) -> None:
        """Fetch live Kalshi order books for approved catalogue pair legs only.

        Callers must pass scan-eligible / APPROVED_EQUIVALENT Kalshi legs.
        Unapproved leftover markets stay metadata/inventory only. Queue wait
        is not a live quote. Missing books stay fail-closed; cached prices
        are never used. A genuine miss still records a precise timeout.
        """

        if self.kalshi is None or not markets:
            return
        prioritized = _prioritize_catalogue_depth_markets(markets)

        async def _one(market: _NormalizedMarket) -> None:
            book_event = _event_for_source(
                k_events, market.canonical.event.source_event_id
            )
            if book_event is None:
                return
            series = market.raw.get("series") if isinstance(market.raw, dict) else None
            if not isinstance(series, dict):
                series = await self._fetch_kalshi_series_metadata(
                    book_event,
                    issues=issues,
                    attach_contract_family=False,
                )
                if isinstance(series, dict) and isinstance(market.raw, dict):
                    market.raw["series"] = series
            observation, book_fetched = await self._try_kalshi_observation(
                book_event,
                market,
                series=series if isinstance(series, dict) else None,
                issues=issues,
            )
            side.books += book_fetched
            if observation is None:
                return
            side.observations[market.canonical.source_market_id] = observation
            for item in side.inventory:
                if item.source_market_id == market.canonical.source_market_id:
                    item.observation = observation

        await asyncio.gather(*[_one(market) for market in prioritized])

    async def _cluster_venue_events_cooperative(
        self,
        *,
        matchbook: list[Any],
        polymarket: list[Any],
        kalshi: list[Any],
        max_event_pairs: int,
    ) -> tuple[list[FixtureCluster], dict[str, int], bool, dict[str, Any]]:
        cluster_pass = ClusterPass(
            matchbook=matchbook,
            polymarket=polymarket,
            kalshi=kalshi,
            matcher=self.event_matcher,
            max_event_pairs=max_event_pairs,
            identity_cache=self._identity_cache,
        )
        truncated = False
        started = monotonic()
        stage_deadline = clustering_stage_deadline_mono(
            remaining_soft=self._remaining_soft(),
            hard_deadline=self._op_deadline,
        )
        absolute_index = cluster_pass._resume_cursor
        for index, (left, right) in enumerate(cluster_pass.pairs()):
            if index % CLUSTER_COMPARISON_YIELD_EVERY == 0:
                await asyncio.sleep(0)
                if self._hard_deadline_reached():
                    truncated = True
                    break
                if stage_deadline is not None and monotonic() >= stage_deadline:
                    truncated = True
                    break
            cluster_pass.consider(left, right)
            absolute_index = cluster_pass._resume_cursor + index + 1
        duration_ms = max(0, int((monotonic() - started) * 1000))
        if truncated:
            cluster_pass.checkpoint(absolute_index)
        else:
            cluster_pass.checkpoint(len(cluster_pass._candidates))
        clusters, counts = cluster_pass.finalize()
        if not truncated:
            cluster_pass.record_generation_negatives(clusters)
        diagnostics = cluster_pass.clustering_diagnostics(
            truncated=truncated, duration_ms=duration_ms
        )
        return clusters, counts, truncated, diagnostics

    async def _normalize_events(
        self,
        payloads: list[dict[str, Any]],
        *,
        venue: VenueName,
        issues: list[CollectorIssue],
    ) -> list[_NormalizedEvent]:
        result: list[_NormalizedEvent] = []
        normalizer = (
            self.matchbook_normalizer
            if venue == VenueName.MATCHBOOK
            else self.polymarket_normalizer
            if venue == VenueName.POLYMARKET
            else self.kalshi_normalizer
        )
        for index, payload in enumerate(payloads):
            if index % NORMALIZE_EVENT_YIELD_EVERY == 0:
                await asyncio.sleep(0)
            source_id = str(
                payload.get("id") or payload.get("event_ticker") or payload.get("ticker") or ""
            ) or None
            try:
                result.append(_NormalizedEvent(payload, normalizer.normalize_event(payload)))
            except (VenueNormalizationError, ValueError) as exc:
                issues.append(
                    CollectorIssue(
                        stage="normalize_event",
                        venue=venue,
                        source_id=source_id,
                        detail=str(exc),
                    )
                )
        return result

    def _inventory_markets(
        self,
        event: _NormalizedEvent,
        payloads: list[dict[str, Any]],
        *,
        venue: VenueName,
        issues: list[CollectorIssue],
    ) -> tuple[list[_NormalizedMarket], list[InventoryMarket]]:
        normalized: list[_NormalizedMarket] = []
        inventory: list[InventoryMarket] = []
        normalizer = (
            self.matchbook_normalizer
            if venue == VenueName.MATCHBOOK
            else self.polymarket_normalizer
            if venue == VenueName.POLYMARKET
            else self.kalshi_normalizer
        )
        for payload in payloads:
            source_id = raw_market_id(payload, venue) or None
            name = raw_market_name(payload, venue)
            try:
                market = _NormalizedMarket(
                    payload,
                    normalizer.normalize_market(event.canonical, payload),
                )
            except (VenueNormalizationError, ValueError) as exc:
                issues.append(
                    CollectorIssue(
                        stage="normalize_market",
                        venue=venue,
                        source_id=source_id,
                        detail=str(exc),
                    )
                )
                inventory.append(
                    InventoryMarket(
                        venue=venue,
                        source_event_id=event.canonical.source_event_id,
                        source_market_id=source_id or name,
                        raw_name=name,
                        raw_market_type=raw_market_type(payload, venue),
                        raw_runner_labels=raw_runner_labels(payload, venue),
                        normalize_error=str(exc),
                    )
                )
                continue
            if not family_is_phase1_expensive_work(market.canonical.family):
                uniqueness = _cheap_non_unique_canonical_detail(venue, market, name, payload)
                if uniqueness is not None:
                    issues.append(
                        CollectorIssue(
                            stage="normalize_market",
                            venue=venue,
                            source_id=source_id,
                            detail=uniqueness,
                        )
                    )
                inventory.append(
                    InventoryMarket(
                        venue=venue,
                        source_event_id=event.canonical.source_event_id,
                        source_market_id=market.canonical.source_market_id,
                        raw_name=name,
                        raw_market_type=raw_market_type(payload, venue),
                        raw_runner_labels=raw_runner_labels(payload, venue),
                        canonical=market.canonical,
                        normalize_error=uniqueness or "phase1_non_target_family_discarded",
                    )
                )
                continue
            normalized.append(market)
            inventory.append(
                InventoryMarket(
                    venue=venue,
                    source_event_id=event.canonical.source_event_id,
                    source_market_id=market.canonical.source_market_id,
                    raw_name=name,
                    raw_market_type=raw_market_type(payload, venue),
                    raw_runner_labels=raw_runner_labels(payload, venue),
                    canonical=market.canonical,
                )
            )
        return normalized, inventory

    def _try_matchbook_observation(
        self,
        event: _NormalizedEvent,
        market: _NormalizedMarket,
        *,
        retrieved_at: datetime,
        latency_ms: int,
        issues: list[CollectorIssue],
    ) -> VenueMarketObservation | None:
        evaluated_at = datetime.now(UTC)
        age = matchbook_market_quote_age(
            market.raw,
            retrieved_at=retrieved_at,
            evaluated_at=evaluated_at,
        )
        if age.reason:
            issues.append(
                CollectorIssue(
                    stage="quote_age",
                    venue=VenueName.MATCHBOOK,
                    source_id=market.canonical.source_market_id,
                    detail=age.reason,
                )
            )
        try:
            return self.matchbook_builder.build(
                event.raw,
                market.raw,
                observed_at=evaluated_at,
                source_latency_ms=latency_ms,
                quote_age_ms=age.quote_age_ms,
                quote_age_basis=age.basis,
                quote_age_reason=age.reason,
            )
        except (VenueNormalizationError, ValueError) as exc:
            issues.append(
                CollectorIssue(
                    stage="build_observation",
                    venue=VenueName.MATCHBOOK,
                    source_id=market.canonical.source_market_id,
                    detail=str(exc),
                )
            )
            return None

    async def _fetch_polymarket_books(
        self,
        event: _NormalizedEvent,
        market: _NormalizedMarket,
        *,
        issues: list[CollectorIssue],
    ) -> tuple[dict[str, dict[str, Any]], int, int, bool]:
        tokens = [runner.source_runner_id for runner in market.canonical.runners]
        return await self._fetch_polymarket_token_books(
            event,
            tokens,
            market_id=market.canonical.source_market_id,
            issues=issues,
        )

    async def _fetch_polymarket_token_books(
        self,
        event: _NormalizedEvent,
        tokens: list[str],
        *,
        market_id: str,
        issues: list[CollectorIssue],
    ) -> tuple[dict[str, dict[str, Any]], int, int, bool]:
        books_by_token: dict[str, dict[str, Any]] = {}
        latency_ms = 0
        fetched = 0
        failed = False

        async def _one(token: str) -> None:
            nonlocal latency_ms, fetched, failed
            if failed or self._provider_budget_exhausted() or not token:
                failed = True
                return
            try:
                started = perf_counter()
                raw_book, book_timed_out = await self._wait_provider(
                    self.polymarket.get_order_book(
                        event.canonical.source_event_id,
                        market_id,
                        token,
                    ),
                    stage="order_book",
                    venue=VenueName.POLYMARKET,
                    source_id=token,
                    default=None,
                )
                if book_timed_out or raw_book is None:
                    failed = True
                    return
                latency_ms += _elapsed_ms(started)
                books_by_token[token] = raw_book
                fetched += 1
            except Exception as exc:
                issues.append(
                    CollectorIssue(
                        stage="order_book",
                        venue=VenueName.POLYMARKET,
                        source_id=token,
                        detail=str(exc),
                    )
                )
                failed = True

        if tokens:
            await asyncio.gather(*[_one(token) for token in tokens])
        return books_by_token, latency_ms, fetched, failed

    def _try_polymarket_observation(
        self,
        event: _NormalizedEvent,
        market: _NormalizedMarket,
        books_by_token: dict[str, dict[str, Any]],
        *,
        latency_ms: int,
        issues: list[CollectorIssue],
    ) -> VenueMarketObservation | None:
        evaluated_at = datetime.now(UTC)
        required_tokens = [runner.source_runner_id for runner in market.canonical.runners]
        age = polymarket_books_quote_age(
            books_by_token,
            required_tokens=required_tokens,
            evaluated_at=evaluated_at,
        )
        if age.reason:
            issues.append(
                CollectorIssue(
                    stage="quote_age",
                    venue=VenueName.POLYMARKET,
                    source_id=market.canonical.source_market_id,
                    detail=age.reason,
                )
            )
        try:
            return self.polymarket_builder.build(
                event.raw,
                market.raw,
                books_by_token,
                canonical=market.canonical,
                observed_at=evaluated_at,
                source_latency_ms=latency_ms,
                quote_age_ms=age.quote_age_ms,
                quote_age_basis=age.basis,
                quote_age_reason=age.reason,
            )
        except (VenueNormalizationError, ValueError) as exc:
            issues.append(
                CollectorIssue(
                    stage="build_observation",
                    venue=VenueName.POLYMARKET,
                    source_id=market.canonical.source_market_id,
                    detail=str(exc),
                )
            )
            return None


    async def _attach_kalshi_contract_family(
        self,
        series: dict[str, Any],
        *,
        issues: list[CollectorIssue],
    ) -> dict[str, Any]:
        """Fetch allowlisted contract_terms_url once per series and attach SAFE family metadata.

        Does not parse PDF text on the hot path. Hash/URL lookup against the
        cached contract-family catalog. SOCCERGAMEWIN has no default result scope.
        """

        from sports_hedge.normalization.kalshi_contract_terms import (
            kalshi_contract_terms_url_is_allowlisted,
            lookup_kalshi_contract_family,
        )

        url = str(series.get("contract_terms_url") or "").strip()
        fetch_status = "not_called"
        sha256: str | None = None
        if not url:
            fetch_status = "absent"
        elif not kalshi_contract_terms_url_is_allowlisted(url):
            fetch_status = "host_not_allowlisted"
        else:
            cached = self._kalshi_contract_terms_cache.get(url)
            getter = getattr(self.kalshi, "get_contract_terms_document", None)
            if cached is not None:
                sha256 = str(cached.get("sha256") or "") or None
                fetch_status = str(cached.get("fetch_status") or "ok")
            elif getter is None:
                fetch_status = "not_called_no_client"
            else:
                try:
                    document, failed = await self._wait_provider(
                        getter(url),
                        stage="get_contract_terms",
                        venue=VenueName.KALSHI,
                        source_id=str(series.get("ticker") or url),
                        default=None,
                    )
                except Exception as exc:
                    fetch_status = "transport_failed"
                    issues.append(
                        CollectorIssue(
                            stage="get_contract_terms",
                            venue=VenueName.KALSHI,
                            source_id=str(series.get("ticker") or url),
                            detail=str(exc),
                        )
                    )
                    document, failed = None, True
                if failed or not isinstance(document, dict):
                    fetch_status = "transport_failed" if fetch_status != "transport_failed" else fetch_status
                else:
                    sha256 = str(document.get("sha256") or "") or None
                    fetch_status = "ok"
                self._kalshi_contract_terms_cache[url] = {
                    "sha256": sha256,
                    "fetch_status": fetch_status,
                }
        family = lookup_kalshi_contract_family(url=url, sha256=sha256)
        attached = dict(series)
        attached["contract_family"] = {**family, "fetch_status": fetch_status}
        return attached

    async def _load_kalshi_markets(
        self,
        event: _NormalizedEvent,
        *,
        issues: list[CollectorIssue],
    ) -> tuple[list[_NormalizedMarket], list[InventoryMarket], dict[str, Any] | None, int, bool]:
        assert self.kalshi is not None
        # Cheap archetype inventory from nested/list payloads only. Series
        # metadata is not required to recognise family/period/line/outcomes.
        # Get Series and Get Market wait for Approved Catalogue candidates.
        series: dict[str, Any] | None = None
        nested = event.raw.get("markets")
        raw_markets: list[dict[str, Any]]
        if isinstance(nested, list) and nested:
            raw_markets = [item for item in nested if isinstance(item, dict)]
        else:
            try:
                payload, markets_failed = await self._wait_provider(
                    self.kalshi.list_markets(event.canonical.source_event_id),
                    stage="list_markets",
                    venue=VenueName.KALSHI,
                    source_id=event.canonical.source_event_id,
                    default={"markets": []},
                )
                if markets_failed:
                    return [], [], series, 0, True
                raw_markets = _extract_matchbook_items(payload, "markets")
            except Exception as exc:
                issues.append(
                    CollectorIssue(
                        stage="list_markets",
                        venue=VenueName.KALSHI,
                        source_id=event.canonical.source_event_id,
                        detail=str(exc),
                    )
                )
                return [], [], series, 0, True
        # Settlement enrichment is deferred until a cross-venue catalogue
        # candidate exists. Nested wording that is already complete still
        # classifies here; incomplete Match Result stays fail-closed until
        # candidate Get Market / series fallback runs.
        inventory: list[InventoryMarket] = []
        normalized: list[_NormalizedMarket] = []
        try:
            canonicals = self.kalshi_normalizer.assemble_canonical_markets(
                event.canonical,
                raw_markets,
                series=series,
                event_payload=event.raw if isinstance(event.raw, dict) else None,
            )
        except (VenueNormalizationError, ValueError) as exc:
            issues.append(
                CollectorIssue(
                    stage="normalize_market",
                    venue=VenueName.KALSHI,
                    source_id=event.canonical.source_event_id,
                    detail=str(exc),
                )
            )
            for payload in raw_markets:
                inventory.append(
                    InventoryMarket(
                        venue=VenueName.KALSHI,
                        source_event_id=event.canonical.source_event_id,
                        source_market_id=raw_market_id(payload, VenueName.KALSHI) or "kalshi",
                        raw_name=raw_market_name(payload, VenueName.KALSHI),
                        raw_market_type=raw_market_type(payload, VenueName.KALSHI),
                        raw_runner_labels=raw_runner_labels(payload, VenueName.KALSHI),
                        normalize_error=str(exc),
                    )
                )
            return [], inventory, series, 0, False
        payloads_by_ticker = {
            str(item.get("ticker") or ""): item
            for item in raw_markets
            if item.get("ticker")
        }
        grouped: dict[str, list[dict[str, Any]]] = {}
        for market in canonicals:
            constituents: list[dict[str, Any]] = []
            seen_tickers: set[str] = set()
            for runner in market.runners:
                ticker = runner.source_runner_id.rsplit(":", 1)[0]
                if not ticker or ticker in seen_tickers:
                    continue
                seen_tickers.add(ticker)
                payload = payloads_by_ticker.get(ticker)
                if payload is not None:
                    constituents.append(payload)
            grouped[market.source_market_id] = constituents or [
                {"ticker": market.source_market_id, "title": market.family.value}
            ]
            wrapper = {
                **(grouped[market.source_market_id][0] if grouped[market.source_market_id] else {}),
                "grouped_payloads": grouped[market.source_market_id],
                "series": series,
            }
            normalized.append(_NormalizedMarket(wrapper, market))
            first_payload = grouped[market.source_market_id][0] if grouped[market.source_market_id] else {}
            labels: list[str] = []
            for payload in grouped[market.source_market_id]:
                for label in raw_runner_labels(payload, VenueName.KALSHI):
                    if label and label not in labels:
                        labels.append(label)
            for runner in market.runners:
                if runner.label and runner.label not in labels:
                    labels.append(runner.label)
                outcome = runner.outcome.value
                if outcome and outcome not in labels:
                    labels.append(outcome)
            inventory.append(
                InventoryMarket(
                    venue=VenueName.KALSHI,
                    source_event_id=event.canonical.source_event_id,
                    source_market_id=market.source_market_id,
                    raw_name=raw_market_name(first_payload, VenueName.KALSHI) or market.family.value,
                    raw_market_type=raw_market_type(first_payload, VenueName.KALSHI),
                    raw_runner_labels=labels,
                    canonical=market,
                )
            )
        unused = [
            payload
            for ticker, payload in payloads_by_ticker.items()
            if all(ticker not in runner.source_runner_id for market in canonicals for runner in market.runners)
        ]
        for payload in unused:
            inventory.append(
                InventoryMarket(
                    venue=VenueName.KALSHI,
                    source_event_id=event.canonical.source_event_id,
                    source_market_id=raw_market_id(payload, VenueName.KALSHI) or "kalshi",
                    raw_name=raw_market_name(payload, VenueName.KALSHI),
                    raw_market_type=raw_market_type(payload, VenueName.KALSHI),
                    raw_runner_labels=raw_runner_labels(payload, VenueName.KALSHI),
                    normalize_error="unsupported_or_ungrouped_kalshi_market",
                )
            )
        return normalized, inventory, series, 0, False

    async def _enrich_kalshi_catalogue_candidate_settlement(
        self,
        k_events: list[_NormalizedEvent],
        *,
        venue_markets: dict[VenueName, list[_NormalizedMarket]],
        kalshi_side: _VenueSideFetch,
        pair_specs: tuple[tuple[VenueName, VenueName], ...],
        issues: list[CollectorIssue],
    ) -> None:
        """Get Market / series settlement work only for Approved Catalogue candidates.

        Cheap inventory already recognised family/period/line/outcomes from
        nested payloads. Enrich ordinary Match Result rules only when another
        venue offers the same catalogue archetype and settlement is not already
        fail-closed (cancel/fair-price). After enrichment, reassemble and fail
        closed if settlement is still unproven. Depth stays behind
        ``scan_eligible_pair``.
        """

        if self.kalshi is None or not kalshi_side.markets or not pair_specs:
            return
        candidates: dict[str, _NormalizedMarket] = {}
        for left_venue, right_venue in pair_specs:
            if VenueName.KALSHI not in {left_venue, right_venue}:
                continue
            left_markets = venue_markets.get(left_venue) or []
            right_markets = venue_markets.get(right_venue) or []
            if not left_markets or not right_markets:
                continue
            for left_market in left_markets:
                for right_market in right_markets:
                    if not _kalshi_match_result_catalogue_candidate(
                        left_market.canonical, right_market.canonical
                    ):
                        continue
                    kalshi_market = (
                        left_market if left_venue is VenueName.KALSHI else right_market
                    )
                    candidates.setdefault(
                        kalshi_market.canonical.source_market_id, kalshi_market
                    )
        if not candidates:
            return
        by_event: dict[str, list[_NormalizedMarket]] = {}
        for market in candidates.values():
            event_id = market.canonical.event.source_event_id
            by_event.setdefault(event_id, []).append(market)
        for event_id, markets in by_event.items():
            event = _event_for_source(k_events, event_id)
            if event is None:
                continue
            raw_markets = _kalshi_grouped_payloads(markets)
            series: dict[str, Any] | None = None
            if _kalshi_markets_still_need_series_fallback(event, raw_markets):
                series = await self._fetch_kalshi_series_metadata(
                    event,
                    issues=issues,
                    attach_contract_family=True,
                )
            await self._enrich_kalshi_match_result_rules(
                event,
                raw_markets,
                series=series,
                issues=issues,
            )
            self._rebuild_kalshi_candidate_markets(
                event,
                markets,
                kalshi_side=kalshi_side,
                raw_markets=raw_markets,
                series=series,
                issues=issues,
            )

    async def _fetch_kalshi_series_metadata(
        self,
        event: _NormalizedEvent,
        *,
        issues: list[CollectorIssue],
        attach_contract_family: bool,
    ) -> dict[str, Any] | None:
        """Coalesced Get Series for candidate settlement fallback or approved fees.

        Successful metadata is cached on this collector and on the Kalshi
        client. Transient failures are not stored as success. The leader runs
        on the caller's task so provider capacity stays attached to live HTTP.
        """

        if self.kalshi is None:
            return None
        ticker = str(event.raw.get("series_ticker") or "").strip()
        if not ticker:
            return None
        cache_key = (ticker, attach_contract_family)
        cached = self._kalshi_series_ok.get(cache_key)
        if cached is not None:
            return cached
        existing = self._kalshi_series_inflight.get(cache_key)
        if existing is not None:
            return await existing

        loop = asyncio.get_running_loop()
        future: asyncio.Future[dict[str, Any] | None] = loop.create_future()
        leader = self._kalshi_series_inflight.setdefault(cache_key, future)
        if leader is not future:
            return await leader

        try:
            client = self.kalshi
            if client is None:
                resolved: dict[str, Any] | None = None
            else:
                try:
                    series, failed = await self._wait_provider(
                        client.get_series(ticker),
                        stage="get_series",
                        venue=VenueName.KALSHI,
                        source_id=ticker,
                        default=None,
                    )
                except Exception as exc:
                    issues.append(
                        CollectorIssue(
                            stage="get_series",
                            venue=VenueName.KALSHI,
                            source_id=ticker,
                            detail=str(exc),
                        )
                    )
                    resolved = None
                else:
                    if failed or not isinstance(series, dict):
                        resolved = None
                    else:
                        if attach_contract_family:
                            series = await self._attach_kalshi_contract_family(
                                series, issues=issues
                            )
                        self._kalshi_series_ok[cache_key] = series
                        resolved = series
            if not future.done():
                future.set_result(resolved)
            return resolved
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
            raise
        finally:
            if self._kalshi_series_inflight.get(cache_key) is future:
                self._kalshi_series_inflight.pop(cache_key, None)
            if not future.done():
                future.cancel()

    def _rebuild_kalshi_candidate_markets(
        self,
        event: _NormalizedEvent,
        markets: list[_NormalizedMarket],
        *,
        kalshi_side: _VenueSideFetch,
        raw_markets: list[dict[str, Any]],
        series: dict[str, Any] | None,
        issues: list[CollectorIssue],
    ) -> None:
        try:
            canonicals = self.kalshi_normalizer.assemble_canonical_markets(
                event.canonical,
                raw_markets,
                series=series,
                event_payload=event.raw if isinstance(event.raw, dict) else None,
            )
        except (VenueNormalizationError, ValueError) as exc:
            issues.append(
                CollectorIssue(
                    stage="normalize_market",
                    venue=VenueName.KALSHI,
                    source_id=event.canonical.source_event_id,
                    detail=str(exc),
                )
            )
            return
        by_id = {item.source_market_id: item for item in canonicals}
        for market in markets:
            rebuilt = by_id.get(market.canonical.source_market_id)
            if rebuilt is None:
                continue
            market.canonical = rebuilt
            if isinstance(series, dict):
                market.raw["series"] = series
            for item in kalshi_side.inventory:
                if item.source_market_id == rebuilt.source_market_id:
                    item.canonical = rebuilt

    async def _enrich_kalshi_match_result_rules(
        self,
        event: _NormalizedEvent,
        raw_markets: list[dict[str, Any]],
        *,
        series: dict[str, Any] | None,
        issues: list[CollectorIssue],
    ) -> None:
        """Fetch documented Get Market rules for ordinary Match Result only.

        Nested list payloads often omit `rules_primary` or carry generic text
        that does not complete settlement. Fetch only ordinary Match Result
        tickers whose current wording is missing or incomplete. Prefer
        documented Get Market rule fields for that ticker. Do not infer
        regulation from GAME/Opta names. Missing or still-ambiguous rules stay
        incomplete. Records SAFE per-layer classification metadata only.
        """

        if not raw_markets:
            return
        event_payload = event.raw if isinstance(event.raw, dict) else None
        incomplete, complete = self.kalshi_normalizer.match_result_rule_enrichment_tickers(
            event.canonical,
            raw_markets,
            series=series,
            event_payload=event_payload,
        )
        self._kalshi_rule_enrichment["skipped_complete"] += len(complete)
        by_ticker = {
            str(item.get("ticker") or "").strip(): item
            for item in raw_markets
            if str(item.get("ticker") or "").strip()
        }
        nested_before_merge = {
            ticker: {key: payload.get(key) for key in KALSHI_CONTRACT_RULE_KEYS}
            for ticker, payload in by_ticker.items()
        }
        fixture_label = f"{event.canonical.home_team} vs {event.canonical.away_team}"
        for ticker in complete:
            self._record_kalshi_rule_layers(
                fixture_label=fixture_label,
                ticker=ticker,
                nested=nested_before_merge.get(ticker),
                event_payload=event_payload,
                series=series,
                get_market_payload=None,
                get_market_status="not_called_already_complete",
                skipped_because_complete=True,
            )
        getter = getattr(self.kalshi, "get_market", None)
        if not incomplete:
            return
        if getter is None:
            for ticker in incomplete:
                self._record_kalshi_rule_layers(
                    fixture_label=fixture_label,
                    ticker=ticker,
                    nested=nested_before_merge.get(ticker),
                    event_payload=event_payload,
                    series=series,
                    get_market_payload=None,
                    get_market_status="not_called_no_client",
                    skipped_because_complete=False,
                )
            return
        self._kalshi_rule_enrichment["attempted"] += len(incomplete)
        results = await asyncio.gather(
            *[
                self._wait_provider(
                    getter(ticker),
                    stage="get_market",
                    venue=VenueName.KALSHI,
                    source_id=ticker,
                    default=None,
                )
                for ticker in incomplete
            ],
            return_exceptions=True,
        )
        for ticker, result in zip(incomplete, results, strict=True):
            target = by_ticker.get(ticker)
            get_payload: dict[str, Any] | None = None
            get_status = "transport_failed"
            if isinstance(result, Exception):
                self._kalshi_rule_enrichment["failed"] += 1
                self._kalshi_get_market_failed_tickers.add(ticker)
                issues.append(
                    CollectorIssue(
                        stage="get_market",
                        venue=VenueName.KALSHI,
                        source_id=ticker,
                        detail=str(result),
                    )
                )
            else:
                payload, failed = result if isinstance(result, tuple) else (None, True)
                if failed:
                    self._kalshi_rule_enrichment["failed"] += 1
                    self._kalshi_get_market_failed_tickers.add(ticker)
                    get_status = "transport_failed"
                elif not isinstance(payload, dict):
                    self._kalshi_rule_enrichment["failed"] += 1
                    get_status = "empty_payload"
                else:
                    get_payload = payload
                    get_status = "ok"
                    nonempty_rules = any(
                        str(payload.get(key) or "").strip() for key in KALSHI_CONTRACT_RULE_KEYS
                    )
                    if target is not None and merge_kalshi_contract_rules(target, payload):
                        self._kalshi_rule_enrichment["applied"] += 1
                    elif nonempty_rules:
                        self._kalshi_rule_enrichment["unchanged_existing"] += 1
                    else:
                        self._kalshi_rule_enrichment["rules_empty"] += 1
                        self._kalshi_rule_enrichment["empty"] += 1
            self._record_kalshi_rule_layers(
                fixture_label=fixture_label,
                ticker=ticker,
                nested=nested_before_merge.get(ticker),
                event_payload=event_payload,
                series=series,
                get_market_payload=get_payload,
                get_market_status=get_status,
                skipped_because_complete=False,
            )

    def _record_kalshi_rule_layers(
        self,
        *,
        fixture_label: str,
        ticker: str,
        nested: dict[str, Any] | None,
        event_payload: dict[str, Any] | None,
        series: dict[str, Any] | None,
        get_market_payload: dict[str, Any] | None,
        get_market_status: str | None,
        skipped_because_complete: bool,
    ) -> None:
        if len(self._kalshi_rule_layer_diagnostics) >= KALSHI_RULE_DIAGNOSTIC_CAP:
            return
        called = get_market_status in {"ok", "transport_failed", "empty_payload"}
        self._kalshi_rule_layer_diagnostics.append(
            {
                "fixture_label": fixture_label,
                "ticker": ticker,
                "skipped_because_complete": skipped_because_complete,
                "get_market_called": called,
                "get_market_status": get_market_status,
                "layers": safe_kalshi_match_result_rule_layers(
                    nested=nested,
                    event_payload=event_payload,
                    series=series,
                    get_market_payload=get_market_payload,
                    get_market_status=get_market_status,
                ),
            }
        )

    async def _try_kalshi_observation(
        self,
        event: _NormalizedEvent,
        market: _NormalizedMarket,
        *,
        series: dict[str, Any] | None,
        issues: list[CollectorIssue],
    ) -> tuple[VenueMarketObservation | None, int]:
        assert self.kalshi is not None
        payloads = market.raw.get("grouped_payloads")
        if not isinstance(payloads, list) or not payloads:
            payloads = [market.raw]
        books_by_ticker: dict[str, dict[str, Any]] = {}
        fetched = 0
        latency_ms = 0
        failed = False
        tickers = sorted(
            {
                runner.source_runner_id.rsplit(":", 1)[0]
                for runner in market.canonical.runners
            }
        )

        async def _one(ticker: str) -> None:
            nonlocal fetched, latency_ms, failed
            if failed or not ticker:
                if ticker:
                    failed = True
                return
            try:
                started = perf_counter()
                raw_book, book_failed = await self._wait_provider(
                    self.kalshi.get_order_book(
                        event.canonical.source_event_id,
                        ticker,
                    ),
                    stage="order_book",
                    venue=VenueName.KALSHI,
                    source_id=ticker,
                    default=None,
                )
                if book_failed or raw_book is None:
                    failed = True
                    return
                latency_ms += _elapsed_ms(started)
                books_by_ticker[ticker] = raw_book
                fetched += 1
            except Exception as exc:
                issues.append(
                    CollectorIssue(
                        stage="order_book",
                        venue=VenueName.KALSHI,
                        source_id=ticker,
                        detail=str(exc),
                    )
                )
                failed = True

        if tickers:
            await asyncio.gather(*[_one(ticker) for ticker in tickers])
        if failed:
            return None, fetched
        evaluated_at = datetime.now(UTC)
        age = retrieval_quote_age(retrieved_at=evaluated_at, evaluated_at=evaluated_at)
        fee_meta = resolve_kalshi_fee_metadata(
            event=event.raw if isinstance(event.raw, dict) else None,
            series=series if isinstance(series, dict) else None,
        )
        try:
            observation = self.kalshi_builder.build(
                event.raw,
                payloads,
                books_by_ticker,
                series=series,
                observed_at=evaluated_at,
                source_latency_ms=latency_ms,
                quote_age_ms=age.quote_age_ms,
                quote_age_basis=age.basis,
                quote_age_reason=age.reason,
                fee_snapshot=fee_meta,
            )
        except (VenueNormalizationError, ValueError) as exc:
            issues.append(
                CollectorIssue(
                    stage="build_observation",
                    venue=VenueName.KALSHI,
                    source_id=market.canonical.source_market_id,
                    detail=str(exc),
                )
            )
            return None, fetched
        return observation, fetched


def _as_normalized(item) -> _NormalizedEvent | None:
    if item is None:
        return None
    return _NormalizedEvent(item.raw, item.canonical)


def _event_for_source(
    events: list[_NormalizedEvent],
    source_event_id: str,
) -> _NormalizedEvent | None:
    for event in events:
        if event.canonical.source_event_id == source_event_id:
            return event
    return events[0] if events else None


def _promote_polymarket_cluster_markets(
    markets: list[_NormalizedMarket],
    inventory: list[InventoryMarket],
) -> tuple[list[_NormalizedMarket], list[InventoryMarket]]:
    if len(markets) < 2:
        return markets, inventory
    payloads = [item.raw for item in markets if isinstance(item.raw, dict)]
    canonicals = [item.canonical for item in markets]
    promoted = promote_polymarket_complete_match_result(canonicals, payloads)
    original_ids = {item.canonical.source_market_id for item in markets}
    promoted_ids = {item.source_market_id for item in promoted}
    if promoted_ids == original_ids:
        return markets, inventory
    by_id = {item.canonical.source_market_id: item for item in markets}
    rebuilt: list[_NormalizedMarket] = []
    for market in promoted:
        existing = by_id.get(market.source_market_id)
        if existing is not None:
            rebuilt.append(existing)
            continue
        constituents = [
            item.raw
            for item in markets
            if item.canonical.source_market_id not in promoted_ids
            and item.canonical.family is market.family
        ]
        wrapper = {
            "id": market.source_market_id,
            "question": "Match result",
            "sportsMarketType": "moneyline",
            "assembled_match_result": True,
            "grouped_payloads": constituents,
        }
        rebuilt.append(_NormalizedMarket(wrapper, market))
    kept_ids = {item.canonical.source_market_id for item in rebuilt}
    rebuilt_inventory = [
        item for item in inventory if item.source_market_id in kept_ids or item.canonical is None
    ]
    for market in rebuilt:
        if any(item.source_market_id == market.canonical.source_market_id for item in rebuilt_inventory):
            continue
        rebuilt_inventory.append(
            InventoryMarket(
                venue=VenueName.POLYMARKET,
                source_event_id=market.canonical.event.source_event_id,
                source_market_id=market.canonical.source_market_id,
                raw_name=market.canonical.family.value,
                canonical=market.canonical,
            )
        )
    return rebuilt, rebuilt_inventory


def cluster_needs_one_sided_catalogue_markets(cluster: FixtureCluster) -> bool:
    """Whether UNIVERSE must fetch markets for a single-venue cluster.

    Approved Match Register rows are cross-venue. One-sided metadata is not a
    global UNIVERSE requirement. Opt in here only for an explicit approved
    workflow; do not use this to re-enable fetching every single-venue book.
    """

    del cluster
    return False


def _fixture_from_cluster(
    cluster: FixtureCluster,
    *,
    seen_at: datetime,
    polymarket_events: list[_NormalizedEvent],
    queried_series_ids: list[str] | None,
    leftover: bool = False,
    single_venue_deferred: bool = False,
) -> DiscoveredFixture:
    anchor = cluster.anchor
    canonical = anchor.canonical
    mb_event = _as_normalized(cluster.matchbook)
    state = matchbook_fixture_state(mb_event.raw) if mb_event is not None else None
    competition = resolve_target_competition(canonical.competition)
    if competition is None and mb_event is not None:
        scoped = scope_matchbook_event(mb_event.raw)
        competition = scoped.competition
    two_plus = cluster.venue_count >= 2
    unmatched_reason = (
        None
        if two_plus
        else (
            None
            if cluster.polymarket is not None
            else _unmatched_polymarket_reason(
                competition,
                polymarket_events=polymarket_events,
                queried_series_ids=queried_series_ids,
            )
        )
    )
    if leftover:
        evaluation_state = MarketEvaluationState.NOT_EVALUATED_SCAN_DEADLINE
        evaluation_reason = SCAN_BUDGET_EXHAUSTED_REASON
        no_comparison = NOT_EVALUATED_SCAN_DEADLINE_REASON if two_plus else unmatched_reason
        opportunity_state = "not_evaluated"
        equivalent_count = None
    elif single_venue_deferred:
        evaluation_state = MarketEvaluationState.SINGLE_VENUE_NO_CROSS_VENUE_CANDIDATE
        evaluation_reason = SINGLE_VENUE_NO_CROSS_VENUE_REASON
        no_comparison = unmatched_reason or SINGLE_VENUE_NO_CROSS_VENUE_REASON
        opportunity_state = "not_evaluated"
        equivalent_count = None
    else:
        evaluation_state = MarketEvaluationState.NOT_EVALUATED_SCAN_DEADLINE
        evaluation_reason = None
        no_comparison = unmatched_reason
        opportunity_state = "unmatched"
        equivalent_count = None
    return DiscoveredFixture(
        source=anchor.venue,
        source_event_id=anchor.source_event_id,
        canonical_event_id=cluster_canonical_event_id(cluster),
        home_team=canonical.home_team,
        away_team=canonical.away_team,
        competition=canonical.competition,
        target_competition_code=competition.code.value if competition else None,
        kickoff_utc=canonical.kickoff_utc,
        matchbook_matched=cluster.matchbook is not None,
        polymarket_matched=cluster.polymarket is not None,
        kalshi_matched=cluster.kalshi is not None,
        fixture_status=state.venue_status if state is not None else None,
        fixture_status_source=VenueName.MATCHBOOK if state is not None else None,
        in_running=state.in_running if state is not None else None,
        live_score_supported=state.live_score_supported if state is not None else False,
        home_score=state.home_score if state is not None else None,
        away_score=state.away_score if state is not None else None,
        last_seen_at=seen_at,
        matched_equivalent_count=equivalent_count,
        no_comparison_reason=no_comparison,
        solver_is_arbitrage=False,
        opportunity_state=opportunity_state,
        market_evaluation_state=evaluation_state.value,
        market_evaluation_reason=evaluation_reason,
        event_match_confidence=cluster.event_match_confidence,
        event_match_threshold=cluster.event_match_threshold,
    )


def _opportunity_state(fixture: DiscoveredFixture) -> str:
    if fixture.market_evaluation_state != MarketEvaluationState.EVALUATED.value:
        return "not_evaluated"
    if fixture.headline_band == HeadlineBand.QUALIFYING.value or fixture.solver_is_arbitrage:
        return "qualifying"
    if fixture.headline_band == HeadlineBand.NEAR_EXECUTABLE.value:
        return "near"
    if fixture.matched_equivalent_count:
        return "matched"
    return "unmatched"


def _merge_counts(*groups: dict[str, int]) -> dict[str, int]:
    merged: dict[str, int] = {}
    for group in groups:
        for key, value in group.items():
            merged[key] = merged.get(key, 0) + value
    return merged


def _unique_cap(values: list[str], limit: int) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
        if len(result) >= limit:
            break
    return result


def _target_coverage(clusters: list[FixtureCluster]) -> dict[str, dict[str, int]]:
    coverage = {
        code.value: {"matchbook": 0, "polymarket": 0, "kalshi": 0}
        for code in TargetCompetitionCode
    }
    for cluster in clusters:
        for venue_event, venue_key in (
            (cluster.matchbook, "matchbook"),
            (cluster.polymarket, "polymarket"),
            (cluster.kalshi, "kalshi"),
        ):
            if venue_event is None:
                continue
            resolved = resolve_target_competition(venue_event.canonical.competition)
            if resolved is None:
                continue
            coverage[resolved.code.value][venue_key] += 1
    return coverage


def _resolved_queried_series_ids(
    polymarket_event_filters: dict[str, Any] | None,
    explicit: list[str] | None,
) -> list[str] | None:
    if polymarket_event_filters and "series_id" in polymarket_event_filters:
        value = str(polymarket_event_filters.get("series_id") or "").strip()
        return [value] if value else []
    return list(explicit) if explicit is not None else None


def _polymarket_event_target(event: _NormalizedEvent) -> TargetCompetition | None:
    resolved = resolve_target_competition(event.canonical.competition)
    if resolved is not None:
        return resolved
    series_items = event.raw.get("series", event.raw.get("series_id"))
    if isinstance(series_items, dict):
        series_items = [series_items]
    if isinstance(series_items, str):
        for item in TARGET_COMPETITIONS:
            if item.polymarket_gamma_series_id == series_items.strip():
                return item
        return resolve_target_competition(series_items)
    if isinstance(series_items, list):
        for series in series_items:
            if not isinstance(series, dict):
                continue
            series_id = str(series.get("id", series.get("series_id", ""))).strip()
            for item in TARGET_COMPETITIONS:
                if item.polymarket_gamma_series_id and item.polymarket_gamma_series_id == series_id:
                    return item
            title = series.get("title") or series.get("name")
            resolved = resolve_target_competition(str(title) if title else None)
            if resolved is not None:
                return resolved
    return None


def _unmatched_polymarket_reason(
    competition: TargetCompetition | None,
    *,
    polymarket_events: list[_NormalizedEvent],
    queried_series_ids: list[str] | None,
) -> str:
    if competition is None:
        return UNMATCHED_POLYMARKET_COVERAGE
    series_id = competition.polymarket_gamma_series_id
    if queried_series_ids and series_id and series_id not in queried_series_ids:
        return SERIES_NOT_QUERIED
    for event in polymarket_events:
        target = _polymarket_event_target(event)
        if target is not None and target.code == competition.code:
            return EVENT_IDENTITY_MISMATCH
    return UNMATCHED_POLYMARKET_COVERAGE


def _matchbook_discovery_issues(payload: dict[str, Any]) -> list[CollectorIssue]:
    if not payload.get("truncated"):
        return []
    detail = payload.get("truncation-detail") or payload.get("truncation_detail")
    return [
        CollectorIssue(
            stage="matchbook_discovery",
            venue=VenueName.MATCHBOOK,
            detail=str(detail or "Matchbook event list truncated at safety cap"),
        )
    ]


def _matchbook_market_listing_issues(payload: dict[str, Any]) -> list[CollectorIssue]:
    if not payload.get("truncated"):
        return []
    detail = payload.get("truncation-detail") or payload.get("truncation_detail")
    return [
        CollectorIssue(
            stage="list_markets",
            venue=VenueName.MATCHBOOK,
            source_id=str(payload.get("event-id") or payload.get("event_id") or "") or None,
            detail=str(detail or "Matchbook market list truncated at safety cap"),
        )
    ]


def _best_observed_back(observation: VenueMarketObservation) -> Decimal | None:
    prices = [
        book.best_back.decimal_odds
        for book in observation.outcome_books
        if book.best_back is not None
    ]
    return max(prices) if prices else None


def _outcome_context(market: _NormalizedMarket) -> str:
    labels = [runner.outcome.value for runner in market.canonical.runners]
    return "/".join(labels)


def _apply_fixture_headline(
    fixture: DiscoveredFixture,
    applies: list[tuple[_NormalizedMarket, VenueMarketObservation, VenueMarketObservation, PaperScanDecision]],
) -> None:
    candidates = [
        candidate_from_decision(
            decision,
            family=left_market.canonical.family.value,
            line=left_market.canonical.line,
        )
        for left_market, _left_obs, _right_obs, decision in applies
    ]
    fixture.qualifying_market_count = sum(
        1 for item in candidates if headline_band_for(item) is HeadlineBand.QUALIFYING
    )
    fixture.near_executable_market_count = sum(
        1 for item in candidates if headline_band_for(item) is HeadlineBand.NEAR_EXECUTABLE
    )
    headline = select_fixture_headline(candidates)
    fixture.headline_band = headline.band.value
    if headline.candidate is None:
        fixture.current_net_edge = None
        fixture.solver_is_arbitrage = False
        fixture.best_arb_market = None
        fixture.execution_risk_score = None
        fixture.execution_risk_band = None
        fixture.execution_risk_reasons = []
        if fixture.matched_equivalent_count and fixture.no_comparison_reason is None:
            fixture.no_comparison_reason = headline.reason or NO_EXECUTABLE_ARB
        return
    winner_index = candidates.index(headline.candidate)
    left_market, left_obs, right_obs, decision = applies[winner_index]
    _apply_backend_comparison(
        fixture,
        left_market=left_market,
        left_observation=left_obs,
        right_observation=right_obs,
        decision=decision,
        qualifying=headline.band is HeadlineBand.QUALIFYING,
        best_arb_market=headline.best_arb_market,
    )


def _apply_backend_comparison(
    fixture: DiscoveredFixture,
    *,
    left_market: _NormalizedMarket,
    left_observation: VenueMarketObservation,
    right_observation: VenueMarketObservation,
    decision: PaperScanDecision,
    qualifying: bool,
    best_arb_market: str | None,
) -> None:
    """Copy the selected executable headline onto the discovery row."""

    current_net = None
    if decision.payoff_scan is not None:
        current_net = quantized_edge(decision.payoff_scan.solution.roi)
    elif decision.depth_scan is not None:
        implied = decision.depth_scan.solution.implied_probability_sum
        if implied > 0:
            current_net = net_edge_from_implied_sum(implied)
    trigger = decision.minimum_net_edge
    distance = (
        distance_to_trigger_pp(current_net, trigger) if current_net is not None else None
    )
    fixture.market_family = left_market.canonical.family.value
    fixture.outcome_context = _outcome_context(left_market)
    fixture.best_arb_market = best_arb_market
    for observation in (left_observation, right_observation):
        price = _best_observed_back(observation)
        if observation.venue is VenueName.MATCHBOOK:
            fixture.best_matchbook_price = price
        elif observation.venue is VenueName.POLYMARKET:
            fixture.best_polymarket_price = price
        elif observation.venue is VenueName.KALSHI:
            fixture.best_kalshi_price = price
    fixture.current_net_edge = current_net
    fixture.trigger_net_edge = trigger
    fixture.distance_to_trigger_pp = distance
    fixture.quote_age_ms = decision.quote_age_ms
    fixture.quote_age_basis = decision.quote_age_basis
    if decision.execution_risk is not None:
        fixture.execution_risk_score = decision.execution_risk.score
        fixture.execution_risk_band = decision.execution_risk.band
        fixture.execution_risk_reasons = list(decision.execution_risk.reasons)
    else:
        fixture.execution_risk_score = None
        fixture.execution_risk_band = None
        fixture.execution_risk_reasons = []
    fixture.solver_is_arbitrage = qualifying
    fixture.no_comparison_reason = None


def _is_baseline_match_result(market: CanonicalMarket) -> bool:
    """Regulation-time exhaustive HOME/DRAW/AWAY Match Result, fail closed otherwise."""

    if market.family is not MarketFamily.MATCH_RESULT:
        return False
    if market.period is not FootballPeriod.FULL_TIME:
        return False
    settlement = market.settlement
    if settlement.period is not FootballPeriod.FULL_TIME:
        return False
    if settlement.scope is not SettlementScope.REGULATION_TIME:
        return False
    if settlement.extra_time_included is not False or settlement.penalties_included is not False:
        return False
    if not settlement.is_economically_complete():
        return False
    outcomes = {runner.outcome for runner in market.runners}
    return outcomes == {CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY}


def _prioritize_baseline_markets(markets: list[_NormalizedMarket]) -> list[_NormalizedMarket]:
    return sorted(
        markets,
        key=lambda item: (
            0
            if _is_baseline_match_result(item.canonical)
            else 1
            if is_ordinary_full_time_1x2(item.canonical)
            else 2
        ),
    )


_CATALOGUE_DEPTH_FAMILIES = operational_kalshi_families()


def _cheap_non_unique_canonical_detail(
    venue: VenueName,
    market: _NormalizedMarket,
    name: str,
    payload: dict[str, Any],
) -> str | None:
    """Fail-closed uniqueness from already-parsed inventory. No extra depth fetch."""

    if venue is not VenueName.MATCHBOOK:
        return None
    outcomes = [runner.outcome for runner in market.canonical.runners]
    if len(outcomes) == len(set(outcomes)):
        return None
    return matchbook_unsupported_market_detail(
        name or market.canonical.source_market_id,
        MATCHBOOK_NON_UNIQUE_CANONICAL_REASON,
        market_type=raw_market_type(payload, venue),
    )


def _prioritize_catalogue_depth_markets(
    markets: list[_NormalizedMarket],
) -> list[_NormalizedMarket]:
    """Approved catalogue families share depth priority; BTTS is not behind 1X2."""

    return sorted(
        markets,
        key=lambda item: (
            0 if item.canonical.family in _CATALOGUE_DEPTH_FAMILIES else 1,
            item.canonical.source_market_id,
        ),
    )


def _kalshi_match_result_catalogue_candidate(
    left: CanonicalMarket, right: CanonicalMarket
) -> bool:
    """True when a Kalshi Match Result could become ApprovedEquivalent after enrichment.

    Series metadata is not required for this cheap archetype check. Nested
    cancel/fair-price wording already fail-closes and must not trigger Get Market.
    """

    if VenueName.KALSHI not in {left.source_venue, right.source_venue}:
        return False
    kalshi = left if left.source_venue is VenueName.KALSHI else right
    if kalshi.family is not MarketFamily.MATCH_RESULT:
        return False
    if kalshi.settlement.unknown_reason == KALSHI_UNMODELLED_CANCEL_RESCHEDULE_FAIR_PRICE_REASON:
        return False
    assessment = classify_pair(left, right)
    if assessment.archetype is None:
        return False
    if assessment.reason == KALSHI_UNMODELLED_CANCEL_RESCHEDULE_FAIR_PRICE_REASON:
        return False
    if assessment.state in {
        CatalogueApprovalState.KNOWN_CONTRADICTION,
        CatalogueApprovalState.UNSUPPORTED,
        CatalogueApprovalState.APPROVED_PARAMETER_MISMATCH,
    }:
        return False
    return True


def _kalshi_grouped_payloads(markets: list[_NormalizedMarket]) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    seen: set[str] = set()
    for market in markets:
        grouped = market.raw.get("grouped_payloads") if isinstance(market.raw, dict) else None
        items = grouped if isinstance(grouped, list) else [market.raw]
        for item in items:
            if not isinstance(item, dict):
                continue
            ticker = str(item.get("ticker") or "").strip()
            if ticker and ticker in seen:
                continue
            if ticker:
                seen.add(ticker)
            payloads.append(item)
    return payloads


def _kalshi_markets_still_need_series_fallback(
    event: _NormalizedEvent,
    raw_markets: list[dict[str, Any]],
) -> bool:
    """True when Get Market did not complete ordinary Match Result settlement."""

    incomplete, _complete = KalshiNormalizer().match_result_rule_enrichment_tickers(
        event.canonical,
        raw_markets,
        series=None,
        event_payload=event.raw if isinstance(event.raw, dict) else None,
    )
    return bool(incomplete)


def _kalshi_markets_needing_depth(
    eligible_pairs: list[
        tuple[VenueName, VenueName, _NormalizedMarket, _NormalizedMarket, MarketMatchResult]
    ],
) -> list[_NormalizedMarket]:
    """Deduped Kalshi legs from catalogue-eligible pairs only."""

    needed: dict[str, _NormalizedMarket] = {}
    for left_venue, right_venue, left_market, right_market, _match in eligible_pairs:
        if left_venue is VenueName.KALSHI:
            needed.setdefault(left_market.canonical.source_market_id, left_market)
        if right_venue is VenueName.KALSHI:
            needed.setdefault(right_market.canonical.source_market_id, right_market)
    return list(needed.values())


def _kalshi_market_has_failed_ticker(market: _NormalizedMarket, failed: set[str]) -> bool:
    """Skip depth when Get Market timed out/failed for a constituent ticker."""

    if not failed:
        return False
    for payload in _kalshi_grouped_payloads([market]):
        ticker = str(payload.get("ticker") or "").strip()
        if ticker and ticker in failed:
            return True
    source_id = str(market.canonical.source_market_id or "").strip()
    return source_id in failed


def _is_baseline_match_result_pair(
    left: _NormalizedMarket, right: _NormalizedMarket
) -> bool:
    return _is_baseline_match_result(left.canonical) and _is_baseline_match_result(
        right.canonical
    )


def _is_priority_match_result_pair(
    left: _NormalizedMarket, right: _NormalizedMarket
) -> bool:
    if _is_baseline_match_result_pair(left, right):
        return True
    return allow_unknown_settlement_for_ordinary_1x2(left.canonical, right.canonical)


def _select_prioritized_market_pairs(
    pairs: list[tuple[_NormalizedMarket, _NormalizedMarket, MarketMatchResult]],
    max_market_pairs_per_event: int,
) -> list[tuple[_NormalizedMarket, _NormalizedMarket, MarketMatchResult]]:
    """Keep baseline MATCH_RESULT pairs even when the per-event pair cap is tight."""

    baseline = [item for item in pairs if _is_priority_match_result_pair(item[0], item[1])]
    others = [item for item in pairs if not _is_priority_match_result_pair(item[0], item[1])]
    remaining = max(0, max_market_pairs_per_event - len(baseline))
    return baseline + others[:remaining]


def _greedy_unique_market_pairs(
    left: list[_NormalizedMarket],
    right: list[_NormalizedMarket],
    *,
    matcher: MarketMatcher,
) -> list[tuple[_NormalizedMarket, _NormalizedMarket, MarketMatchResult]]:
    candidates: list[tuple[float, int, int, MarketMatchResult]] = []
    for left_index, left_item in enumerate(left):
        for right_index, right_item in enumerate(right):
            match = matcher.match(left_item.canonical, right_item.canonical)
            if match.matched:
                candidates.append((match.confidence, left_index, right_index, match))
    candidates.sort(
        key=lambda item: (
            1
            if _is_priority_match_result_pair(left[item[1]], right[item[2]])
            else 0,
            item[0],
        ),
        reverse=True,
    )

    used_left: set[int] = set()
    used_right: set[int] = set()
    result: list[tuple[_NormalizedMarket, _NormalizedMarket, MarketMatchResult]] = []
    for _, left_index, right_index, match in candidates:
        if left_index in used_left or right_index in used_right:
            continue
        used_left.add(left_index)
        used_right.add(right_index)
        result.append((left[left_index], right[right_index], match))
    return result


def _extract_matchbook_items(payload: dict[str, Any], key: str) -> list[dict[str, Any]]:
    value = payload.get(key, [])
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _payload_events(payload: Any, venue: VenueName) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)], {}
    if isinstance(payload, dict):
        return _extract_matchbook_items(payload, "events"), dict(payload)
    return [], {}


def _kalshi_family_key_from_event(event: _NormalizedEvent) -> str | None:
    raw = event.raw if isinstance(event.raw, dict) else {}
    return family_key_from_kalshi_series(raw.get("series_ticker")) or family_key_from_kalshi_series(
        raw.get("event_ticker")
    )


def _mark_unprocessed_kalshi_families_incomplete(
    side: _VenueSideFetch, remaining_events: list[_NormalizedEvent]
) -> None:
    """Budget/not-queried remainder is incomplete even when series discovery was ok."""

    for event in remaining_events:
        family_key = _kalshi_family_key_from_event(event)
        if family_key:
            side.kalshi_incomplete_family_keys.add(family_key)


def _combined_kalshi_series_ok_rows(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Evidence for a successful combined Kalshi list_events (no per-series report).

    Configured series for competitions present in the payload are marked ok.
    This does not invent a timeout as success. Per-series discovery remains
    the production path and supplies explicit timeout/not-queried rows.
    """

    seen_codes: set[str] = set()
    for item in events:
        if not isinstance(item, dict):
            continue
        ticker = str(item.get("series_ticker") or item.get("event_ticker") or "").strip()
        competition = resolve_target_competition_from_kalshi_ticker(ticker)
        if competition is not None:
            seen_codes.add(competition.code.value)
    if not seen_codes:
        return []
    rows: list[dict[str, Any]] = []
    for item in TARGET_COMPETITIONS:
        if item.code.value not in seen_codes:
            continue
        for ticker in KALSHI_SERIES_TICKERS_BY_CODE.get(item.code, ()):
            series = str(ticker).strip()
            if not series:
                continue
            count = sum(
                1
                for event in events
                if isinstance(event, dict)
                and str(event.get("series_ticker") or "").strip() == series
            )
            rows.append(
                {
                    "series": series,
                    "status": "ok",
                    "retryable": False,
                    "event_count": count,
                    "reason": None,
                }
            )
    return rows


def _discovery_event_id(item: dict[str, Any], venue: VenueName) -> str:
    if venue is VenueName.KALSHI:
        return str(item.get("event_ticker") or item.get("ticker") or item.get("id") or "").strip()
    return str(item.get("id") or "").strip()


def _discovery_series_ids(venue: VenueName, client: Any, filters: dict[str, Any]) -> list[str]:
    if venue is VenueName.KALSHI:
        if filters.get("cursor") or filters.get("series_ticker"):
            return []
        explicit = filters.get("series_tickers")
        if explicit:
            tickers = [str(item).strip() for item in explicit if str(item).strip()]
        else:
            settings = getattr(client, "settings", None)
            tickers = [
                str(item).strip()
                for item in list(getattr(settings, "kalshi_series_tickers", []) or [])
                if str(item).strip()
            ]
        return tickers if len(tickers) > 1 else []
    if venue is VenueName.POLYMARKET:
        if filters.get("series_id") is not None:
            return []
        explicit = filters.get("series_ids")
        if explicit:
            ids = [str(item).strip() for item in explicit if str(item).strip()]
        else:
            settings = getattr(client, "settings", None)
            resolver = getattr(settings, "resolved_polymarket_series_ids", None)
            ids = [
                str(item).strip()
                for item in (resolver() if callable(resolver) else [])
                if str(item).strip()
            ]
        return ids if len(ids) > 1 else []
    return []


def _merge_series_rows(
    existing: list[dict[str, Any]],
    incoming: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    by_series: dict[str, dict[str, Any]] = {}
    for item in existing:
        series = str(item.get("series") or "").strip()
        if series:
            by_series[series] = item
    for item in incoming:
        series = str(item.get("series") or "").strip()
        if series:
            by_series[series] = item
    return list(by_series.values()) if by_series else list(incoming)


def _collector_series_failure_kind(exc: BaseException) -> tuple[str, bool]:
    text = str(exc).casefold()
    if "401" in text or "403" in text or "auth" in text:
        return "auth_failure", False
    if "unsupported" in text or "404" in text:
        return "unsupported", False
    if "429" in text or "rate-limited" in text:
        return "rate_limited", True
    if "timeout" in text or "timed out" in text:
        return HEALTH_DISCOVERY_TIMEOUT, True
    return "unavailable", True


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))


def _inventory_from_observation(observation: VenueMarketObservation) -> InventoryMarket:
    market = observation.market
    metadata = observation.metadata if isinstance(observation.metadata, dict) else {}
    labels = metadata.get("raw_runner_labels")
    runner_labels = (
        [str(item) for item in labels if str(item).strip()] if isinstance(labels, list) else []
    )
    return InventoryMarket(
        venue=observation.venue,
        source_event_id=market.event.source_event_id,
        source_market_id=market.source_market_id,
        raw_name=str(metadata.get("raw_market_name") or market.family.value),
        raw_market_type=str(metadata.get("raw_market_type") or "") or None,
        raw_runner_labels=runner_labels,
        canonical=market,
        observation=observation,
    )


def finalisation_reserve_seconds(cycle_budget: float) -> float:
    """Hold back a slice of the cycle so leftover assembly beats the coordinator hard timeout."""

    if cycle_budget <= 0:
        return 0.0
    return min(SCAN_FINALISATION_RESERVE_SECONDS, cycle_budget * 0.2)


def clustering_market_eval_reserve_seconds(remaining_soft: float | None) -> float:
    """Seconds reserved for market evaluation after identity clustering.

    Adaptive: a 150s UNIVERSE chunk keeps a meaningful evaluation slice when
    cross-venue candidates can exist. Sub-5s remaining budgets (tests and
    leftover seconds) do not starve clustering.
    """

    if remaining_soft is None or remaining_soft <= 0:
        return 0.0
    if remaining_soft <= TINY_CYCLE_FOR_CLUSTERING_RESERVE_SECONDS:
        return 0.0
    reserve = min(
        CLUSTERING_MARKET_EVAL_RESERVE_SECONDS,
        remaining_soft * CLUSTERING_MARKET_EVAL_RESERVE_FRACTION,
    )
    return min(reserve, max(0.0, remaining_soft - MIN_CLUSTERING_STAGE_SECONDS))


def clustering_stage_deadline_mono(
    *,
    remaining_soft: float | None,
    hard_deadline: float | None,
) -> float | None:
    """Monotonic timestamp when clustering must yield to market evaluation."""

    now = monotonic()
    reserve = clustering_market_eval_reserve_seconds(remaining_soft)
    stage_deadline = None if remaining_soft is None else now + max(0.0, remaining_soft - reserve)
    candidates = [item for item in (stage_deadline, hard_deadline) if item is not None]
    return min(candidates) if candidates else None


def _append_deadline_leftovers(
    leftover_clusters: list[FixtureCluster],
    *,
    discovered_fixtures: list[DiscoveredFixture],
    issues: list[CollectorIssue],
    started_at: datetime,
    polymarket_events: list[_NormalizedEvent],
    queried_series_ids: list[str] | None,
) -> None:
    if leftover_clusters and not any(
        issue.detail == "scan_cycle_deadline_reached" for issue in issues
    ):
        issues.append(CollectorIssue(stage="collect", detail="scan_cycle_deadline_reached"))
    seen = {item.canonical_event_id for item in discovered_fixtures}
    for cluster in leftover_clusters:
        canonical_id = cluster_canonical_event_id(cluster)
        if canonical_id in seen:
            continue
        discovered_fixtures.append(
            _fixture_from_cluster(
                cluster,
                seen_at=started_at,
                polymarket_events=polymarket_events,
                queried_series_ids=queried_series_ids,
                leftover=True,
            )
        )
        seen.add(canonical_id)


def _fixture_identity_aliases(
    clusters: list[FixtureCluster],
    discovered_fixtures: list[DiscoveredFixture],
    decisions: list[PaperScanDecision],
) -> dict[str, str]:
    current_ids = {item.canonical_event_id for item in discovered_fixtures}
    aliases: dict[str, str] = {}
    for cluster in clusters:
        canonical_id = cluster_canonical_event_id(cluster)
        if canonical_id not in current_ids:
            continue
        aliases.update(cluster_identity_aliases(cluster))
    for decision in decisions:
        cluster_id = (decision.fixture_canonical_event_id or "").strip()
        decision_id = (decision.canonical_event_id or "").strip()
        if cluster_id and cluster_id in current_ids:
            aliases[cluster_id] = cluster_id
            if decision_id:
                aliases[decision_id] = cluster_id
    return aliases


def _matching_coverage(
    clusters: list[FixtureCluster],
    discovered_fixtures: list[DiscoveredFixture],
    *,
    pair_counts: dict[str, int],
    equivalent: int,
    qualifying: int,
    fixture_markets: dict[str, list[FixtureMarketInventoryRow]] | None = None,
) -> dict[str, Any]:
    """Honest current-cycle matching counts. Zero is a valid empty result."""

    single_venue = sum(1 for cluster in clusters if cluster.venue_count < 2)
    cross_venue = sum(1 for cluster in clusters if cluster.venue_count >= 2)
    inventory_cross_venue = sum(
        1
        for item in discovered_fixtures
        if sum(
            bool(flag)
            for flag in (
                item.matchbook_matched,
                item.polymarket_matched,
                item.kalshi_matched,
            )
        )
        >= 2
    )
    if cross_venue == 0:
        meaning = "no_multi_venue_identity_match"
    elif equivalent == 0:
        meaning = "multi_venue_identity_without_settlement_equivalent"
    else:
        meaning = "cross_venue_equivalent_present"
    catalogue_summaries = [
        item.catalogue_coverage
        for item in discovered_fixtures
        if item.catalogue_coverage is not None
    ]
    return {
        "fixtures": len(discovered_fixtures),
        "single_venue_clusters": single_venue,
        "cross_venue_clusters": cross_venue,
        "matched_event_pairs": sum(pair_counts.values()),
        "inventory_cross_venue_fixtures": inventory_cross_venue,
        "equivalent_markets": equivalent,
        "qualifying_arbs": qualifying,
        "matching_state": meaning,
        "zero_equivalent_reason_counts": zero_equivalent_reason_counts(
            discovered_fixtures, fixture_markets
        ),
        "catalogue_by_archetype": aggregate_coverage_by_archetype(catalogue_summaries),
    }


def canonical_work_set_authority(
    *,
    cluster_ids: list[str],
    clustering_truncated: bool,
    retry_series: dict[str, list[str]] | None,
    venue_health: dict[str, str],
    enabled: frozenset[VenueName] | set[VenueName],
    issues: list[CollectorIssue],
    series_results: dict[str, list[dict[str, Any]]] | None,
    provider_cancels: int,
) -> tuple[bool, str | None]:
    """Whether the current cluster IDs are a complete work-set for reconciliation.

    Missing PENDING ids may be retired only when this returns True. Every
    enabled venue must be explicitly healthy (`ok`). A genuinely healthy empty
    cluster set is authoritative; unknown/degraded/blank/failure is not.
    """

    _ = cluster_ids
    if clustering_truncated:
        return False, "clustering_truncated"
    if any(issue.detail == "scan_cycle_deadline_reached" for issue in issues):
        return False, "deadline_truncation"
    if retry_series and any(bool(value) for value in retry_series.values()):
        return False, "retry_series_partial"
    if provider_cancels > 0:
        return False, "incomplete_discovery"
    for venue in enabled:
        health = str(venue_health.get(venue.value, "") or "").strip()
        if health == HEALTH_OK:
            continue
        if health in {HEALTH_AUTH_FAILURE, "auth_failure"}:
            return False, "auth_failure"
        if health in _PROVIDER_FAILURE_VENUE_HEALTH:
            return False, "provider_failure"
        return False, "incomplete_discovery"
    for rows in (series_results or {}).values():
        for row in rows or []:
            if not isinstance(row, dict):
                continue
            status = str(row.get("status") or "").strip()
            if bool(row.get("retryable")) or status in _RETRYABLE_SERIES_STATUSES:
                return False, "retry_series_partial"
    return True, None


def universe_sweep_completeness(
    *,
    leftover_n: int,
    evaluated_n: int,
    clusters_before_resume: int,
    skipped_by_resume: int,
    deadline_hit: bool,
    generation_resume: bool,
    clustering_truncated: bool = False,
) -> str:
    """Distinguish deadline leftovers, a genuine empty universe, and skip leaks.

    A closed generation's skip/cursor must never make a new sweep look complete.
    Incomplete clustering is not a completed generation even when leftover_n is 0.
    """

    if clustering_truncated:
        return UNIVERSE_COMPLETENESS_DEADLINE_LEFTOVER
    if (
        not generation_resume
        and skipped_by_resume > 0
        and evaluated_n == 0
        and leftover_n == 0
        and clusters_before_resume > 0
    ):
        return UNIVERSE_COMPLETENESS_STALE_GENERATION_STATE
    if leftover_n > 0 or deadline_hit:
        return UNIVERSE_COMPLETENESS_DEADLINE_LEFTOVER
    if clusters_before_resume == 0 and evaluated_n == 0:
        return UNIVERSE_COMPLETENESS_EMPTY_UNIVERSE
    return UNIVERSE_COMPLETENESS_COMPLETE


def _filter_known_source_events(
    known_source_events: dict[str, list[dict[str, Any]]],
    enabled: frozenset[VenueName],
) -> dict[str, list[dict[str, Any]]]:
    allowed = {venue.value for venue in enabled}
    filtered: dict[str, list[dict[str, Any]]] = {}
    for canonical_id, rows in known_source_events.items():
        kept = [
            row
            for row in rows
            if str(row.get("venue") or "").strip().casefold() in allowed
        ]
        if kept:
            filtered[canonical_id] = kept
    return filtered


def _raw_events_from_discovery_snapshot(
    snapshot: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    def _rows(key: str) -> list[dict[str, Any]]:
        payload = snapshot.get(key) or []
        return [item for item in payload if isinstance(item, dict)]

    return (
        _rows(VenueName.MATCHBOOK.value),
        _rows(VenueName.POLYMARKET.value),
        _rows(VenueName.KALSHI.value),
    )


def _raw_events_from_known_source_events(
    known_source_events: dict[str, list[dict[str, Any]]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[dict[str, Any]]]:
    matchbook: list[dict[str, Any]] = []
    polymarket: list[dict[str, Any]] = []
    kalshi: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for rows in known_source_events.values():
        for row in rows:
            venue = str(row.get("venue") or "").strip().casefold()
            raw = row.get("raw")
            source_id = str(row.get("source_event_id") or "").strip()
            if not isinstance(raw, dict):
                continue
            key = (venue, source_id or str(raw.get("id") or ""))
            if key in seen:
                continue
            seen.add(key)
            if venue == VenueName.MATCHBOOK.value:
                matchbook.append(raw)
            elif venue == VenueName.POLYMARKET.value:
                polymarket.append(raw)
            elif venue == VenueName.KALSHI.value:
                kalshi.append(raw)
    return matchbook, polymarket, kalshi


def _select_lane_clusters(
    clusters: list[FixtureCluster],
    *,
    identity_scope: set[str] | None,
    skip_event_ids: set[str],
    resume_cursor: str | None,
    scan_lane: str | None,
    seen_at: datetime,
    polymarket_events: list[_NormalizedEvent],
    queried_series_ids: list[str] | None,
) -> list[FixtureCluster]:
    selected: list[FixtureCluster] = []
    for cluster in clusters:
        canonical_id = cluster_canonical_event_id(cluster)
        if identity_scope is not None and canonical_id not in identity_scope:
            continue
        if canonical_id in skip_event_ids:
            continue
        selected.append(cluster)
    if scan_lane == ScanLane.HOT.value:
        decorated: list[tuple[tuple, FixtureCluster]] = []
        for cluster in selected:
            fixture = _fixture_from_cluster(
                cluster,
                seen_at=seen_at,
                polymarket_events=polymarket_events,
                queried_series_ids=queried_series_ids,
            )
            decorated.append((hot_sort_key(fixture), cluster))
        decorated.sort(key=lambda item: item[0])
        return [cluster for _key, cluster in decorated]
    if scan_lane == ScanLane.UNIVERSE.value:
        selected.sort(key=universe_cluster_sort_key)
    # Skip IDs are the source of truth for already-finished work. An empty skip
    # means remaining clusters are still work — do not treat resume_cursor as
    # "already evaluated" or a complete leftover-0 cycle will skip the universe.
    _ = resume_cursor
    return selected


def _fixture_source_events(
    clusters: list[FixtureCluster],
    discovered_fixtures: list[DiscoveredFixture],
) -> dict[str, list[dict[str, Any]]]:
    current_ids = {item.canonical_event_id for item in discovered_fixtures}
    payload: dict[str, list[dict[str, Any]]] = {}
    for cluster in clusters:
        canonical_id = cluster_canonical_event_id(cluster)
        if canonical_id not in current_ids:
            continue
        payload[canonical_id] = [
            {
                "venue": item.venue.value,
                "source_event_id": item.source_event_id,
                "raw": item.raw,
            }
            for item in cluster_member_events(cluster)
        ]
    return payload
