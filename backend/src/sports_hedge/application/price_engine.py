"""Process-memory price engine over ACTIVE catalogue rows (Issue #344 Phase 3).

UNIVERSE catalogues. This engine prices every ACTIVE supported row. HOT is a
priority tier inside the engine, not exclusive membership.

In-flight leases and (2, 5, 10)s retry live in process memory. Restart rebuilds
from ACTIVE catalogue rows and resets short backoff. There is no durable
pricing-work queue and no Phase 4 capture-timing change.

PAPER / read-only. Exact persisted Matchbook/Kalshi IDs only — never
``list_events`` / ``list_markets`` rediscovery on this path.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from time import monotonic
from typing import Any

from sports_hedge.application.approved_market_catalogue import (
    ApprovedMarketCatalogueRow,
    DerivedPriceEngineItem,
    derived_price_engine_working_set,
    required_outcomes_for_key,
)
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
    VenueMarketObservation,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.provider_access import (
    HEALTH_CAPACITY_SATURATED,
    HEALTH_DEFERRED,
    PRICE_ENGINE_BACKGROUND_LANE,
    ProviderAccessLayer,
    ProviderLease,
    get_shared_provider_access,
)
from sports_hedge.application.quote_freshness import retrieval_quote_age
from sports_hedge.application.scan_lanes import (
    DEFAULT_HOT_INTERVAL_SECONDS,
    DEFAULT_UNIVERSE_INTERVAL_SECONDS,
    ScanLane,
    classify_scan_lane,
)
from sports_hedge.config import Settings, get_settings
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
    FAILED = "failed"


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
    last_priced_at: datetime | None = None
    list_events_calls: int = 0
    list_markets_calls: int = 0

    @property
    def item_key(self) -> str:
        return f"{self.identity.catalogue_row_id}:{self.identity.content_version}"


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
        return payload


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
    ) -> None:
        resolved = settings or get_settings()
        self.catalogue_store = catalogue_store
        self.matchbook = matchbook
        self.kalshi = kalshi
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
            resolved.paper_live_refresh_universe_interval_seconds
            if background_interval_seconds is None
            else background_interval_seconds
        )
        self.venue_costs = list(venue_costs or [])
        self.fx_snapshots = list(fx_snapshots or [])
        self._items: dict[str, PriceEngineRuntimeItem] = {}
        self.revalidation_requests: list[dict[str, str]] = []
        self.matchbook_builder = MatchbookObservationBuilder()
        self.kalshi_builder = KalshiObservationBuilder()
        self._peak_held_slots: dict[str, int] = {
            VenueName.MATCHBOOK.value: 0,
            VenueName.KALSHI.value: 0,
        }
        self._slice_remaining: Callable[[], float | None] | None = None

    def now(self) -> datetime:
        return self._clock()

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
            runtime.priority = self.classify_priority(runtime.identity)
            rebuilt.append(runtime)
        return rebuilt

    def restart(self) -> list[PriceEngineRuntimeItem]:
        """Process restart: reconstruct ACTIVE work and reset short backoff."""

        self._items.clear()
        self.revalidation_requests.clear()
        return self.reconstruct()

    def classify_priority(self, identity: DerivedPriceEngineItem) -> PriceEnginePriority:
        fixture = _fixture_like(identity)
        lifecycle = classify_scan_lane(fixture, self.now())
        if lifecycle is ScanLane.HOT:
            return PriceEnginePriority.HOT
        if self.fixture_state is not None:
            if identity.canonical_event_id in self.fixture_state.hot_identity_scope(self.now()):
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
        return result

    async def _price_item(
        self,
        runtime: PriceEngineRuntimeItem,
        result: PriceEngineSliceResult,
    ) -> PriceEngineItemStatus:
        identity = runtime.identity
        runtime.in_flight = True
        runtime.status = PriceEngineItemStatus.IN_FLIGHT
        runtime.list_events_calls = 0
        runtime.list_markets_calls = 0
        lane = (
            ScanLane.HOT.value
            if runtime.priority is PriceEnginePriority.HOT
            else PRICE_ENGINE_BACKGROUND_LANE
        )
        try:
            if not identity.matchbook_event_id or not identity.matchbook_market_id:
                return self._request_revalidation(runtime, "missing_matchbook_identity")
            if not identity.kalshi_event_ticker or not _kalshi_tickers(identity):
                return self._request_revalidation(runtime, "missing_kalshi_identity")

            matchbook_payload = await self._refresh_matchbook(runtime, lane=lane)
            if isinstance(matchbook_payload, PriceEngineItemStatus):
                return self._finalize_provider_status(runtime, matchbook_payload)

            kalshi_books = await self._refresh_kalshi_constituents(runtime, lane=lane)
            if isinstance(kalshi_books, PriceEngineItemStatus):
                return self._finalize_provider_status(runtime, kalshi_books)

            required = _required_tickers(identity)
            if any(ticker not in kalshi_books for ticker in required):
                return self._schedule_retry(runtime, PRICE_ENGINE_ITEM_TIMEOUT_REASON)

            return await self._evaluate_complete_item(
                runtime,
                matchbook_payload=matchbook_payload,
                kalshi_books=kalshi_books,
                result=result,
            )
        finally:
            runtime.in_flight = False

    async def _refresh_matchbook(
        self,
        runtime: PriceEngineRuntimeItem,
        *,
        lane: str,
    ) -> dict[str, Any] | PriceEngineItemStatus:
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
            return self._request_revalidation(runtime, f"{CATALOGUE_REVALIDATION_REASON}:gone")
        if status is not None:
            if status is PriceEngineItemStatus.RETRY_WAIT:
                runtime.last_error_stage = "get_market"
                runtime.last_error_detail = f"get_market_timeout after {self._provider_timeout:g}s"
            return status
        market = extract_matchbook_market_payload(payload)
        if market is None or matchbook_payload_is_terminal(market):
            return self._request_revalidation(runtime, f"{CATALOGUE_REVALIDATION_REASON}:gone")
        if str(market.get("id") or "") != str(identity.matchbook_market_id):
            return self._request_revalidation(runtime, f"{CATALOGUE_REVALIDATION_REASON}:identity")
        return market

    async def _refresh_kalshi_constituents(
        self,
        runtime: PriceEngineRuntimeItem,
        *,
        lane: str,
    ) -> dict[str, dict[str, Any]] | PriceEngineItemStatus:
        identity = runtime.identity
        client = self.kalshi
        getter = getattr(client, "get_order_book", None) if client is not None else None
        if not callable(getter):
            return self._request_revalidation(runtime, "kalshi_order_book_unavailable")
        books: dict[str, dict[str, Any]] = {}
        for ticker in _kalshi_tickers(identity):
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
            books[ticker] = payload
        return books

    async def _evaluate_complete_item(
        self,
        runtime: PriceEngineRuntimeItem,
        *,
        matchbook_payload: dict[str, Any],
        kalshi_books: Mapping[str, dict[str, Any]],
        result: PriceEngineSliceResult,
    ) -> PriceEngineItemStatus:
        identity = runtime.identity
        observed_at = self.now()
        try:
            matchbook_obs = self.matchbook_builder.build(
                _synthetic_matchbook_event(identity),
                matchbook_payload,
                observed_at=observed_at,
                quote_age_ms=0,
                quote_age_basis="retrieval",
            )
        except Exception as exc:
            return self._request_revalidation(runtime, f"{CATALOGUE_REVALIDATION_REASON}:{exc}")
        kalshi_market = _canonical_kalshi_market(identity)
        if kalshi_market is None:
            return self._request_revalidation(runtime, f"{CATALOGUE_REVALIDATION_REASON}:kalshi_identity")
        age = retrieval_quote_age(retrieved_at=observed_at, evaluated_at=observed_at)
        fee_snapshot = self._fee_snapshot_payload(identity)
        try:
            kalshi_obs = self.kalshi_builder.build_from_canonical(
                kalshi_market,
                kalshi_books,
                observed_at=observed_at,
                quote_age_ms=age.quote_age_ms,
                quote_age_basis=age.basis,
                quote_age_reason=age.reason,
                fee_snapshot=fee_snapshot,
            )
        except Exception as exc:
            runtime.last_error_stage = "build_observation"
            runtime.last_error_detail = str(exc)
            return self._schedule_retry(runtime, str(exc))

        decision: PaperScanDecision | None = None
        if self.paper_scan is not None:
            decision = self.paper_scan.scan_pair(
                matchbook_obs,
                kalshi_obs,
                venue_costs=self.venue_costs or None,
                fx_snapshots=self.fx_snapshots or None,
                fixture_canonical_event_id=identity.canonical_event_id,
            )
            result.decisions.append(decision)
        self._publish(
            runtime,
            matchbook_obs=matchbook_obs,
            kalshi_obs=kalshi_obs,
            decision=decision,
            result=result,
        )
        runtime.last_priced_at = observed_at
        runtime.retry_attempt = 0
        runtime.next_retry_at = None
        runtime.last_error_stage = None
        runtime.last_error_detail = None
        runtime.status = PriceEngineItemStatus.EVALUATED
        return PriceEngineItemStatus.EVALUATED

    def _publish(
        self,
        runtime: PriceEngineRuntimeItem,
        *,
        matchbook_obs: VenueMarketObservation,
        kalshi_obs: VenueMarketObservation,
        decision: PaperScanDecision | None,
        result: PriceEngineSliceResult,
    ) -> None:
        if self.fixture_state is None:
            return
        identity = runtime.identity
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
                if runtime.priority is PriceEnginePriority.HOT
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
            runtime=runtime,
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
            paper_decisions=decisions,
            discovered_fixtures=[fixture],
            fixture_markets={identity.canonical_event_id: rows},
            scan_lane=fixture.scan_lane,
            scan_diagnostics={
                "price_engine": True,
                "priority": runtime.priority.value,
                "catalogue_row_id": identity.catalogue_row_id,
            },
        )
        before = set(self.fixture_state.hot_identity_scope(observed_at))
        self.fixture_state.upsert_from_report(
            report,
            scan_lane=ScanLane.UNIVERSE
            if runtime.priority is PriceEnginePriority.BACKGROUND
            else ScanLane.HOT,
            now=observed_at,
        )
        after = set(self.fixture_state.hot_identity_scope(observed_at))
        if identity.canonical_event_id in after and identity.canonical_event_id not in before:
            result.promotions.append(identity.canonical_event_id)

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
                return payload, None
            except TimeoutError:
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
                return None, PriceEngineItemStatus.DEFERRED
            return None, PriceEngineItemStatus.NOT_STARTED
        async with access.acquire_wait(
            venue, lane=lane, stage=stage, timeout=slot_wait
        ) as lease:
            if lease is None:
                await _close_unused()
                if access.venue_saturated(venue):
                    return None, PriceEngineItemStatus.DEFERRED
                return None, PriceEngineItemStatus.NOT_STARTED
            inflight = access.snapshot().inflight.get(venue.value, 0)
            self._peak_held_slots[venue.value] = max(
                self._peak_held_slots[venue.value], inflight
            )
            task = asyncio.create_task(coro)
            done, _pending = await asyncio.wait({task}, timeout=timeout)
            if task in done:
                try:
                    return task.result(), None
                except MatchbookMarketGoneError:
                    raise
                except Exception:
                    return None, PriceEngineItemStatus.RETRY_WAIT
            task.cancel()
            held = False
            if isinstance(lease, ProviderLease):
                held = lease.hold_until_task(task)
            if not held and not task.done():
                task.add_done_callback(lambda done_task: done_task.exception() if done_task.done() else None)
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
        return {
            "item_count": len(self._items),
            "hot": sum(1 for item in self._items.values() if item.priority is PriceEnginePriority.HOT),
            "background": sum(
                1 for item in self._items.values() if item.priority is PriceEnginePriority.BACKGROUND
            ),
            "retry_wait": sum(
                1 for item in self._items.values() if item.status is PriceEngineItemStatus.RETRY_WAIT
            ),
            "revalidation_requests": list(self.revalidation_requests),
            "peak_held_slots": dict(self._peak_held_slots),
            "durable_queue": False,
        }


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
        "sport-name": "Football",
        "competition-name": identity.competition or "Premier League",
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
    if identity.family:
        try:
            return MarketFamily(identity.family)
        except ValueError:
            return None
    return None


def _overlay_decision_inventory(
    rows: list[FixtureMarketInventoryRow],
    *,
    runtime: PriceEngineRuntimeItem,
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
    return [*rows, _decision_inventory_row(runtime, matchbook_obs, kalshi_obs, decision, edge, is_arb)]


def _decision_inventory_row(
    runtime: PriceEngineRuntimeItem,
    matchbook_obs: VenueMarketObservation,
    kalshi_obs: VenueMarketObservation,
    decision: PaperScanDecision,
    edge: Decimal,
    is_arb: bool,
) -> FixtureMarketInventoryRow:
    identity = runtime.identity
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
DEFAULT_BACKGROUND_CADENCE_SECONDS = DEFAULT_UNIVERSE_INTERVAL_SECONDS
_ = (InvalidOperation, SCAN_BUDGET_EXHAUSTED_REASON, DEFERRED_STATUS)
