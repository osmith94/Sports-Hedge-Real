from __future__ import annotations

import asyncio
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from logging import getLogger
from time import monotonic, perf_counter
from typing import Any, Iterator, Protocol

from pydantic import BaseModel, Field

from sports_hedge.application.executable_liquidity import (
    HeadlineBand,
    NO_EXECUTABLE_ARB,
    candidate_from_decision,
    headline_band_for,
    select_fixture_headline,
)
from sports_hedge.application.fixture_inventory import (
    FixtureMarketInventoryRow,
    InventoryMarket,
    assemble_fixture_inventory,
    inventory_summary,
    raw_market_id,
    raw_market_name,
    raw_market_type,
    raw_runner_labels,
    scan_eligible_pair,
)
from sports_hedge.application.fixture_state import matchbook_fixture_state
from sports_hedge.application.quote_freshness import (
    matchbook_market_quote_age,
    polymarket_books_quote_age,
    retrieval_quote_age,
)
from sports_hedge.application.market_observation import (
    KalshiObservationBuilder,
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
    VenueMarketObservation,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.fees.kalshi import resolve_kalshi_fee_metadata
from sports_hedge.application.fixture_clusters import (
    FixtureCluster,
    cluster_canonical_event_id,
    cluster_identity_aliases,
    cluster_venue_events,
    to_venue_event,
)
from sports_hedge.application.target_competitions import (
    EVENT_IDENTITY_MISMATCH,
    SERIES_NOT_QUERIED,
    UNMATCHED_POLYMARKET_COVERAGE,
    TARGET_COMPETITIONS,
    TargetCompetition,
    TargetCompetitionCode,
    filter_in_scope_events,
    resolve_target_competition,
    scope_matchbook_event,
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
from sports_hedge.fees.models import FeeSnapshot
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.markets import MarketMatchResult, MarketMatcher
from sports_hedge.normalization.venues import (
    KalshiNormalizer,
    MatchbookNormalizer,
    PolymarketNormalizer,
    VenueNormalizationError,
    promote_polymarket_complete_match_result,
)
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision
from sports_hedge.paper.preparation import PreparablePaperOpportunity

LOGGER = getLogger(__name__)


class MatchbookReadClient(Protocol):
    async def list_events(self, **filters: Any) -> dict[str, Any]: ...

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]: ...


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


NOT_EVALUATED_SCAN_DEADLINE_REASON = "not_evaluated_scan_deadline"
SCAN_BUDGET_EXHAUSTED_REASON = "scan_budget_exhausted"
MARKET_FETCH_UNAVAILABLE_REASON = "list_markets_unavailable"
DEFAULT_MAX_EVENT_PAIRS = 60
# Keep ~4s of the 45s operator cycle for leftover assembly before coordinator grace.
SCAN_FINALISATION_RESERVE_SECONDS = 4.0
MIN_PROVIDER_WAIT_SECONDS = 0.05
PROVIDER_CANCEL_DRAIN_SECONDS = 0.05


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
    matching_venue: VenueName = VenueName.POLYMARKET
    matching_venues: list[VenueName] = Field(default_factory=lambda: [VenueName.POLYMARKET])
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
    qualifying_arbs: int = Field(default=0, ge=0)
    paper_decisions: list[PaperScanDecision] = Field(default_factory=list)
    discovered_fixtures: list[DiscoveredFixture] = Field(default_factory=list)
    fixture_markets: dict[str, list[FixtureMarketInventoryRow]] = Field(default_factory=dict)
    fixture_identity_aliases: dict[str, str] = Field(default_factory=dict)
    issues: list[CollectorIssue] = Field(default_factory=list)
    scan_diagnostics: dict[str, Any] = Field(default_factory=dict)

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
    ) -> None:
        self.matchbook = matchbook
        self.polymarket = polymarket
        self.kalshi = kalshi
        self.paper_scan = paper_scan
        self.event_matcher = event_matcher or EventMatcher()
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
        self._op_issues: list[CollectorIssue] = []
        self._op_venue_health: dict[str, str] = {}
        self._op_venue_timeout = venue_timeout_seconds
        self._op_provider_timeout = provider_call_timeout_seconds
        self._op_deadline: float | None = None
        self._op_soft_deadline: float | None = None
        self._op_started_mono: float | None = None
        self._inflight: set[asyncio.Task[Any]] = set()
        self._stage_ms: dict[str, int] = {}
        self._timeouts_by_stage: dict[str, int] = {}
        self._provider_cancels = 0

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
    ) -> CollectionReport:
        if max_event_pairs <= 0 or max_market_pairs_per_event <= 0:
            raise ValueError("collector pair limits must be positive")
        started_at = datetime.now(UTC)
        issues: list[CollectorIssue] = []
        venue_health: dict[str, str] = {
            VenueName.MATCHBOOK.value: "unknown",
            VenueName.POLYMARKET.value: "unknown",
            VenueName.KALSHI.value: "unavailable" if self.kalshi is None else "unknown",
        }
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
        cycle_budget = (
            self._cycle_timeout_seconds if cycle_timeout_seconds is None else cycle_timeout_seconds
        )
        started_mono = monotonic()
        self._op_started_mono = started_mono
        self._stage_ms = {}
        self._timeouts_by_stage = {}
        self._provider_cancels = 0
        self._inflight = set()
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
        try:
            with self._stage("event_discovery"):
                mb_task = self._list_raw_events(
                    self.matchbook,
                    venue=VenueName.MATCHBOOK,
                    filters=matchbook_event_filters or {},
                    issues=issues,
                    venue_health=venue_health,
                )
                pm_task = self._list_raw_events(
                    self.polymarket,
                    venue=VenueName.POLYMARKET,
                    filters=polymarket_event_filters or {},
                    issues=issues,
                    venue_health=venue_health,
                )
                if self.kalshi is not None:
                    (
                        (raw_matchbook_events, matchbook_payload),
                        (raw_polymarket_events, _),
                        (raw_kalshi_events, _),
                    ) = await self._gather_bounded(
                        mb_task,
                        pm_task,
                        self._list_raw_events(
                            self.kalshi,
                            venue=VenueName.KALSHI,
                            filters={},
                            issues=issues,
                            venue_health=venue_health,
                        ),
                        default=(
                            ([], {}),
                            ([], {}),
                            ([], {}),
                        ),
                    )
                else:
                    (raw_matchbook_events, matchbook_payload), (raw_polymarket_events, _) = (
                        await self._gather_bounded(
                            mb_task,
                            pm_task,
                            default=(([], {}), ([], {})),
                        )
                    )
                    raw_kalshi_events = []
                    venue_health[VenueName.KALSHI.value] = "unavailable"
                issues.extend(_matchbook_discovery_issues(matchbook_payload))

            with self._stage("normalize_match"):
                mb_scope = filter_in_scope_events(
                    raw_matchbook_events, venue=VenueName.MATCHBOOK
                )
                pm_scope = filter_in_scope_events(
                    raw_polymarket_events, venue=VenueName.POLYMARKET
                )
                k_scope = filter_in_scope_events(raw_kalshi_events, venue=VenueName.KALSHI)
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
                matchbook_events = self._normalize_events(
                    mb_scope.allowed, venue=VenueName.MATCHBOOK, issues=issues
                )
                polymarket_events = self._normalize_events(
                    pm_scope.allowed, venue=VenueName.POLYMARKET, issues=issues
                )
                kalshi_events = self._normalize_events(
                    k_scope.allowed, venue=VenueName.KALSHI, issues=issues
                )
                queried_series_ids = _resolved_queried_series_ids(
                    polymarket_event_filters,
                    polymarket_queried_series_ids,
                )
                mb_items = [
                    to_venue_event(event, VenueName.MATCHBOOK) for event in matchbook_events
                ]
                pm_items = [
                    to_venue_event(event, VenueName.POLYMARKET) for event in polymarket_events
                ]
                k_items = [to_venue_event(event, VenueName.KALSHI) for event in kalshi_events]
                clusters, pair_counts = cluster_venue_events(
                    matchbook=mb_items,
                    polymarket=pm_items,
                    kalshi=k_items,
                    matcher=self.event_matcher,
                    max_event_pairs=max_event_pairs,
                )

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
            with self._stage("cluster_scan"):
                for index, cluster in enumerate(clusters):
                    if self._deadline_reached():
                        self._cancel_inflight()
                        _append_deadline_leftovers(
                            clusters[index:],
                            discovered_fixtures=discovered_fixtures,
                            issues=issues,
                            started_at=started_at,
                            polymarket_events=polymarket_events,
                            queried_series_ids=queried_series_ids,
                        )
                        break
                    fixture, cluster_decisions, inventory, market_counts, fetched, pairs = (
                        await self._scan_cluster(
                            cluster,
                            seen_at=started_at,
                            polymarket_events=polymarket_events,
                            queried_series_ids=queried_series_ids,
                            matchbook_market_filters=matchbook_market_filters or {},
                            polymarket_market_filters=polymarket_market_filters or {},
                            max_market_pairs_per_event=max_market_pairs_per_event,
                            scan_kwargs=scan_kwargs,
                            issues=issues,
                        )
                    )
                    decisions.extend(cluster_decisions)
                    normalized_matchbook_markets += market_counts.get(VenueName.MATCHBOOK, 0)
                    normalized_polymarket_markets += market_counts.get(VenueName.POLYMARKET, 0)
                    normalized_kalshi_markets += market_counts.get(VenueName.KALSHI, 0)
                    matched_market_pairs += pairs
                    order_books_fetched += fetched
                    discovered_fixtures.append(fixture)
                    if inventory:
                        fixture_markets[fixture.canonical_event_id] = inventory
        except asyncio.CancelledError:
            cancelled = True
            self._cancel_inflight()
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
        )

    def _deadline_reached(self) -> bool:
        return self._op_soft_deadline is not None and monotonic() >= self._op_soft_deadline

    def _provider_budget_exhausted(self) -> bool:
        remaining = self._remaining_soft()
        return remaining is not None and remaining < MIN_PROVIDER_WAIT_SECONDS

    def _remaining_soft(self) -> float | None:
        if self._op_soft_deadline is None:
            return None
        return self._op_soft_deadline - monotonic()

    def _remaining_assembly(self) -> float:
        if self._op_deadline is None:
            return PROVIDER_CANCEL_DRAIN_SECONDS
        return max(0.0, self._op_deadline - monotonic())

    def _timeout_budget(self, requested: float) -> float:
        """Cap a provider wait to the remaining soft scan budget, not the hard grace."""

        remaining = self._remaining_soft()
        if remaining is None:
            return requested
        if remaining < MIN_PROVIDER_WAIT_SECONDS:
            return 0.0
        return min(requested, remaining)

    @contextmanager
    def _stage(self, name: str) -> Iterator[None]:
        started = monotonic()
        try:
            yield
        finally:
            duration_ms = max(0, int((monotonic() - started) * 1000))
            self._stage_ms[name] = duration_ms
            LOGGER.info("scan_stage %s duration_ms=%s", name, duration_ms)

    def _cancel_inflight(self) -> None:
        for task in list(self._inflight):
            if not task.done():
                task.cancel()
                self._provider_cancels += 1

    async def _await_bounded(self, coro: Any, timeout: float) -> tuple[Any, bool]:
        """Wait up to timeout, then cancel without blocking on uncooperative providers."""

        if timeout <= 0:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            return None, True
        task = asyncio.create_task(coro)
        self._inflight.add(task)
        try:
            done, _pending = await asyncio.wait({task}, timeout=timeout)
            if task in done:
                self._inflight.discard(task)
                return task.result(), False
            task.cancel()
            self._provider_cancels += 1
            drain = min(PROVIDER_CANCEL_DRAIN_SECONDS, self._remaining_assembly())
            if drain > 0:
                await asyncio.wait({task}, timeout=drain)
            if task.done() and not task.cancelled():
                self._inflight.discard(task)
                return task.result(), False
            return None, True
        except asyncio.CancelledError:
            task.cancel()
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
        tasks = [asyncio.create_task(coro) for coro in coros]
        for task in tasks:
            self._inflight.add(task)
        done, pending = await asyncio.wait(tasks, timeout=remaining)
        for task in pending:
            task.cancel()
            self._provider_cancels += 1
        if pending:
            drain = min(PROVIDER_CANCEL_DRAIN_SECONDS, self._remaining_assembly())
            if drain > 0:
                await asyncio.wait(pending, timeout=drain)
        results: list[Any] = []
        defaults = default if isinstance(default, tuple) and len(default) == len(tasks) else None
        for index, task in enumerate(tasks):
            self._inflight.discard(task)
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
    ) -> None:
        timeout = (
            self._op_venue_timeout if stage == "list_events" else self._op_provider_timeout
        )
        self._timeouts_by_stage[stage] = self._timeouts_by_stage.get(stage, 0) + 1
        self._op_issues.append(
            CollectorIssue(
                stage=stage,
                venue=venue,
                source_id=source_id,
                detail=f"{stage}_timeout after {timeout:g}s",
            )
        )
        health = self._op_venue_health
        current = health.get(venue.value)
        if stage == "list_events":
            health[venue.value] = "timeout"
        elif current == "ok":
            health[venue.value] = "degraded"
        elif current in {None, "unknown"}:
            health[venue.value] = "timeout"

    async def _wait_provider(
        self,
        coro: Any,
        *,
        stage: str,
        venue: VenueName,
        source_id: str | None = None,
        default: Any,
    ) -> tuple[Any, bool]:
        timeout = self._timeout_budget(self._op_provider_timeout)
        if timeout <= 0:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            return default, True
        try:
            payload, timed_out = await self._await_bounded(coro, timeout)
            if timed_out:
                self._record_timeout(stage, venue, source_id)
                return default, True
            return payload, False
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._op_issues.append(
                CollectorIssue(stage=stage, venue=venue, source_id=source_id, detail=str(exc))
            )
            current = self._op_venue_health.get(venue.value)
            if current == "ok":
                self._op_venue_health[venue.value] = "degraded"
            return default, True

    async def _list_raw_events(
        self,
        client: Any,
        *,
        venue: VenueName,
        filters: dict[str, Any],
        issues: list[CollectorIssue],
        venue_health: dict[str, str],
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        timeout = self._timeout_budget(self._op_venue_timeout)
        if timeout <= 0:
            self._record_timeout("list_events", venue)
            return [], {}
        try:
            payload, timed_out = await self._await_bounded(client.list_events(**filters), timeout)
            if timed_out:
                self._record_timeout("list_events", venue)
                return [], {}
        except asyncio.CancelledError:
            self._record_timeout("list_events", venue)
            raise
        except Exception as exc:
            issues.append(CollectorIssue(stage="list_events", venue=venue, detail=str(exc)))
            venue_health[venue.value] = "unavailable"
            return [], {}
        venue_health[venue.value] = "ok"
        if isinstance(payload, list):
            return [item for item in payload if isinstance(item, dict)], {}
        if isinstance(payload, dict):
            return _extract_matchbook_items(payload, "events"), payload
        return [], {}

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
        qualifying = sum(1 for item in discovered_fixtures if item.solver_is_arbitrage)
        equivalent = sum(item.matched_equivalent_count or 0 for item in discovered_fixtures)
        coverage = _target_coverage(clusters)
        deadline_hit = leftover_n > 0 or cancelled or any(
            issue.detail == "scan_cycle_deadline_reached" for issue in issues
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
        matching_venues = [VenueName.POLYMARKET]
        if self.kalshi is not None:
            matching_venues.append(VenueName.KALSHI)
        total_ms = max(0, int((monotonic() - started_mono) * 1000))
        assembly_ms = max(0, int((monotonic() - assembly_started) * 1000))
        diagnostics = {
            "event_discovery_ms": self._stage_ms.get("event_discovery", 0),
            "normalize_match_ms": self._stage_ms.get("normalize_match", 0),
            "cluster_scan_ms": self._stage_ms.get("cluster_scan", 0),
            "assembly_ms": assembly_ms,
            "total_ms": total_ms,
            "cycle_budget_s": cycle_budget,
            "finalisation_reserve_s": reserve,
            "soft_deadline_reached": deadline_hit,
            "cancelled": cancelled,
            "clusters_total": len(clusters),
            "clusters_evaluated": evaluated_n,
            "clusters_leftover": leftover_n,
            "provider_timeouts": dict(self._timeouts_by_stage),
            "provider_cancels": self._provider_cancels,
        }
        LOGGER.info("scan_diagnostics %s", diagnostics)
        return CollectionReport(
            started_at=started_at,
            completed_at=completed_at,
            discovery_source=VenueName.MATCHBOOK,
            discovery_mode="venue_union",
            matching_venue=VenueName.POLYMARKET,
            matching_venues=matching_venues,
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
            qualifying_arbs=qualifying,
            paper_decisions=decisions,
            discovered_fixtures=[
                item.model_copy(update={"last_seen_at": completed_at})
                for item in discovered_fixtures
            ],
            fixture_markets=fixture_markets,
            fixture_identity_aliases=_fixture_identity_aliases(
                clusters, discovered_fixtures, decisions
            ),
            issues=issues,
            scan_diagnostics=diagnostics,
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
        fixture = _fixture_from_cluster(
            cluster,
            seen_at=seen_at,
            polymarket_events=polymarket_events,
            queried_series_ids=queried_series_ids,
        )
        mb_events = [_as_normalized(item) for item in cluster.events_for(VenueName.MATCHBOOK)]
        pm_events = [_as_normalized(item) for item in cluster.events_for(VenueName.POLYMARKET)]
        k_events = [_as_normalized(item) for item in cluster.events_for(VenueName.KALSHI)]
        mb_event = mb_events[0] if mb_events else None
        pm_event = pm_events[0] if pm_events else None
        matchbook_markets: list[_NormalizedMarket] = []
        matchbook_inventory: list[InventoryMarket] = []
        polymarket_markets: list[_NormalizedMarket] = []
        polymarket_inventory: list[InventoryMarket] = []
        kalshi_markets: list[_NormalizedMarket] = []
        kalshi_inventory: list[InventoryMarket] = []
        observations_by_market_id: dict[tuple[VenueName, str], VenueMarketObservation] = {}
        market_counts: dict[VenueName, int] = {}
        order_books_fetched = 0
        matchbook_retrieved_at = datetime.now(UTC)
        matchbook_market_latency_ms = 0
        polymarket_market_latency_ms = 0
        listed_venues: set[VenueName] = set()
        fetch_unavailable = False

        for mb_event in mb_events:
            if self._provider_budget_exhausted():
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
                matchbook_market_latency_ms = _elapsed_ms(mb_started)
                matchbook_retrieved_at = datetime.now(UTC)
                if mb_failed:
                    fetch_unavailable = True
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
                fetch_unavailable = True
                mb_listed = False
                mb_market_payload = {"markets": []}
            if mb_listed:
                listed_venues.add(VenueName.MATCHBOOK)
            raw_matchbook = _extract_matchbook_items(mb_market_payload, "markets")
            markets, inventory = self._inventory_markets(
                mb_event, raw_matchbook, venue=VenueName.MATCHBOOK, issues=issues
            )
            matchbook_markets.extend(markets)
            matchbook_inventory.extend(inventory)
            for left_market in markets:
                observation = self._try_matchbook_observation(
                    mb_event,
                    left_market,
                    retrieved_at=matchbook_retrieved_at,
                    latency_ms=matchbook_market_latency_ms,
                    issues=issues,
                )
                if observation is None:
                    continue
                observations_by_market_id[
                    (VenueName.MATCHBOOK, left_market.canonical.source_market_id)
                ] = observation
                for item in matchbook_inventory:
                    if item.source_market_id == left_market.canonical.source_market_id:
                        item.observation = observation
        if matchbook_markets:
            market_counts[VenueName.MATCHBOOK] = len(matchbook_markets)

        for pm_event in pm_events:
            if self._provider_budget_exhausted():
                break
            pm_listed = True
            try:
                pm_started = perf_counter()
                pm_market_payload, pm_failed = await self._wait_provider(
                    self.polymarket.list_markets(
                        pm_event.canonical.source_event_id,
                        **polymarket_market_filters,
                    ),
                    stage="list_markets",
                    venue=VenueName.POLYMARKET,
                    source_id=pm_event.canonical.source_event_id,
                    default=[],
                )
                polymarket_market_latency_ms += _elapsed_ms(pm_started)
                if pm_failed:
                    fetch_unavailable = True
                    pm_listed = False
                    if fixture.no_comparison_reason is None:
                        fixture.no_comparison_reason = MARKET_FETCH_UNAVAILABLE_REASON
                    pm_market_payload = []
            except Exception as exc:
                issues.append(
                    CollectorIssue(
                        stage="list_markets",
                        venue=VenueName.POLYMARKET,
                        source_id=pm_event.canonical.source_event_id,
                        detail=str(exc),
                    )
                )
                if fixture.no_comparison_reason is None:
                    fixture.no_comparison_reason = MARKET_FETCH_UNAVAILABLE_REASON
                fetch_unavailable = True
                pm_listed = False
                pm_market_payload = []
            if pm_listed:
                listed_venues.add(VenueName.POLYMARKET)
            markets, inventory = self._inventory_markets(
                pm_event,
                [item for item in pm_market_payload if isinstance(item, dict)],
                venue=VenueName.POLYMARKET,
                issues=issues,
            )
            polymarket_markets.extend(markets)
            polymarket_inventory.extend(inventory)
        polymarket_markets, polymarket_inventory = _promote_polymarket_cluster_markets(
            polymarket_markets, polymarket_inventory
        )
        for right_market in _prioritize_baseline_markets(polymarket_markets):
            if self._provider_budget_exhausted():
                break
            book_event = _event_for_source(
                pm_events, right_market.canonical.event.source_event_id
            ) or pm_event
            if book_event is None:
                continue
            books_by_token, poly_book_latency_ms, fetched, book_failed = (
                await self._fetch_polymarket_books(book_event, right_market, issues=issues)
            )
            order_books_fetched += fetched
            if book_failed:
                continue
            observation = self._try_polymarket_observation(
                book_event,
                right_market,
                books_by_token,
                latency_ms=polymarket_market_latency_ms + poly_book_latency_ms,
                issues=issues,
            )
            if observation is None:
                continue
            observations_by_market_id[
                (VenueName.POLYMARKET, right_market.canonical.source_market_id)
            ] = observation
            for item in polymarket_inventory:
                if item.source_market_id == right_market.canonical.source_market_id:
                    item.observation = observation
        if polymarket_markets:
            market_counts[VenueName.POLYMARKET] = len(polymarket_markets)

        if self.kalshi is not None:
            for k_event in k_events:
                if self._provider_budget_exhausted():
                    break
                markets, inventory, series, fetched, kalshi_failed = await self._load_kalshi_markets(
                    k_event, issues=issues
                )
                if kalshi_failed:
                    fetch_unavailable = True
                    if fixture.no_comparison_reason is None:
                        fixture.no_comparison_reason = MARKET_FETCH_UNAVAILABLE_REASON
                else:
                    listed_venues.add(VenueName.KALSHI)
                order_books_fetched += fetched
                kalshi_markets.extend(markets)
                kalshi_inventory.extend(inventory)
                for kalshi_market in _prioritize_baseline_markets(markets):
                    if self._provider_budget_exhausted():
                        break
                    observation, fetched = await self._try_kalshi_observation(
                        k_event, kalshi_market, series=series, issues=issues
                    )
                    order_books_fetched += fetched
                    if observation is None:
                        continue
                    observations_by_market_id[
                        (VenueName.KALSHI, kalshi_market.canonical.source_market_id)
                    ] = observation
                    for item in kalshi_inventory:
                        if item.source_market_id == kalshi_market.canonical.source_market_id:
                            item.observation = observation
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
        decisions: list[PaperScanDecision] = []
        decisions_by_source_ids: dict[tuple[str, str], PaperScanDecision] = {}
        decisions_by_pair: dict[tuple[str, str, str, str], PaperScanDecision] = {}
        matched_market_pairs = 0
        headline_applies: list[tuple] = []
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
            for left_market, right_market, match in market_pairs:
                if not scan_eligible_pair(left_market.canonical, right_market.canonical, match):
                    continue
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
            if cluster.venue_count >= 2 and matched_market_pairs == 0:
                fixture.no_comparison_reason = fixture.no_comparison_reason or (
                    MARKET_FETCH_UNAVAILABLE_REASON
                    if fetch_unavailable
                    else "no_settlement_equivalent_market_pair"
                )
        fixture.opportunity_state = _opportunity_state(fixture)
        return (
            fixture,
            decisions,
            inventory_rows,
            market_counts,
            order_books_fetched,
            matched_market_pairs,
        )

    def _normalize_events(
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
        for payload in payloads:
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
        books_by_token: dict[str, dict[str, Any]] = {}
        latency_ms = 0
        fetched = 0
        for runner in market.canonical.runners:
            try:
                started = perf_counter()
                raw_book, book_timed_out = await self._wait_provider(
                    self.polymarket.get_order_book(
                        event.canonical.source_event_id,
                        market.canonical.source_market_id,
                        runner.source_runner_id,
                    ),
                    stage="order_book",
                    venue=VenueName.POLYMARKET,
                    source_id=runner.source_runner_id,
                    default=None,
                )
                if book_timed_out or raw_book is None:
                    return books_by_token, latency_ms, fetched, True
                latency_ms += _elapsed_ms(started)
                books_by_token[runner.source_runner_id] = raw_book
                fetched += 1
            except Exception as exc:
                issues.append(
                    CollectorIssue(
                        stage="order_book",
                        venue=VenueName.POLYMARKET,
                        source_id=runner.source_runner_id,
                        detail=str(exc),
                    )
                )
                return books_by_token, latency_ms, fetched, True
        return books_by_token, latency_ms, fetched, False

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


    async def _load_kalshi_markets(
        self,
        event: _NormalizedEvent,
        *,
        issues: list[CollectorIssue],
    ) -> tuple[list[_NormalizedMarket], list[InventoryMarket], dict[str, Any] | None, int, bool]:
        assert self.kalshi is not None
        series: dict[str, Any] | None = None
        series_ticker = str(event.raw.get("series_ticker") or "").strip()
        if series_ticker:
            try:
                series, _series_failed = await self._wait_provider(
                    self.kalshi.get_series(series_ticker),
                    stage="get_series",
                    venue=VenueName.KALSHI,
                    source_id=series_ticker,
                    default=None,
                )
            except Exception as exc:
                issues.append(
                    CollectorIssue(
                        stage="get_series",
                        venue=VenueName.KALSHI,
                        source_id=series_ticker,
                        detail=str(exc),
                    )
                )
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
        inventory: list[InventoryMarket] = []
        normalized: list[_NormalizedMarket] = []
        try:
            canonicals = self.kalshi_normalizer.assemble_canonical_markets(
                event.canonical,
                raw_markets,
                series=series,
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
            inventory.append(
                InventoryMarket(
                    venue=VenueName.KALSHI,
                    source_event_id=event.canonical.source_event_id,
                    source_market_id=market.source_market_id,
                    raw_name=raw_market_name(first_payload, VenueName.KALSHI) or market.family.value,
                    raw_market_type=raw_market_type(first_payload, VenueName.KALSHI),
                    raw_runner_labels=raw_runner_labels(first_payload, VenueName.KALSHI)
                    or [runner.label for runner in market.runners],
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
        tickers = {
            runner.source_runner_id.rsplit(":", 1)[0] for runner in market.canonical.runners
        }
        for ticker in tickers:
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
                    return None, fetched
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


def _fixture_from_cluster(
    cluster: FixtureCluster,
    *,
    seen_at: datetime,
    polymarket_events: list[_NormalizedEvent],
    queried_series_ids: list[str] | None,
    leftover: bool = False,
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
        key=lambda item: 0 if _is_baseline_match_result(item.canonical) else 1,
    )


def _is_baseline_match_result_pair(
    left: _NormalizedMarket, right: _NormalizedMarket
) -> bool:
    return _is_baseline_match_result(left.canonical) and _is_baseline_match_result(
        right.canonical
    )


def _select_prioritized_market_pairs(
    pairs: list[tuple[_NormalizedMarket, _NormalizedMarket, MarketMatchResult]],
    max_market_pairs_per_event: int,
) -> list[tuple[_NormalizedMarket, _NormalizedMarket, MarketMatchResult]]:
    """Keep baseline MATCH_RESULT pairs even when the per-event pair cap is tight."""

    baseline = [item for item in pairs if _is_baseline_match_result_pair(item[0], item[1])]
    others = [item for item in pairs if not _is_baseline_match_result_pair(item[0], item[1])]
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
            if _is_baseline_match_result(left[item[1]].canonical)
            and _is_baseline_match_result(right[item[2]].canonical)
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


def _elapsed_ms(started: float) -> int:
    return max(0, round((perf_counter() - started) * 1000))


def finalisation_reserve_seconds(cycle_budget: float) -> float:
    """Hold back a slice of the cycle so leftover assembly beats the coordinator hard timeout."""

    if cycle_budget <= 0:
        return 0.0
    return min(SCAN_FINALISATION_RESERVE_SECONDS, cycle_budget * 0.2)


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
