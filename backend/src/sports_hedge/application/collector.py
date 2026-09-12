from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter
from typing import Any, Protocol

from pydantic import BaseModel, Field

from sports_hedge.application.fixture_inventory import (
    FixtureMarketInventoryRow,
    InventoryMarket,
    assemble_fixture_inventory,
    inventory_summary,
    raw_market_id,
    raw_market_name,
    scan_eligible_pair,
)
from sports_hedge.application.fixture_state import matchbook_fixture_state
from sports_hedge.application.quote_freshness import (
    matchbook_market_quote_age,
    polymarket_books_quote_age,
)
from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
    VenueMarketObservation,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.normalization.identity import canonical_source_event_id
from sports_hedge.application.target_competitions import (
    EVENT_IDENTITY_MISMATCH,
    SERIES_NOT_QUERIED,
    UNMATCHED_POLYMARKET_COVERAGE,
    TARGET_COMPETITIONS,
    ScopeDecision,
    TargetCompetition,
    resolve_target_competition,
    scope_matchbook_event,
)
from sports_hedge.arbitrage.watchlist.economics import (
    distance_to_trigger_pp,
    net_edge_from_implied_sum,
    quantized_edge,
)
from sports_hedge.domain.football import CanonicalEvent, CanonicalMarket
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import VenueCostSnapshot
from sports_hedge.fees.models import FeeSnapshot
from sports_hedge.matching.events import EventMatchResult, EventMatcher
from sports_hedge.matching.markets import MarketMatchResult, MarketMatcher
from sports_hedge.normalization.venues import (
    MatchbookNormalizer,
    PolymarketNormalizer,
    VenueNormalizationError,
)
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision


class MatchbookReadClient(Protocol):
    async def list_events(self, **filters: Any) -> dict[str, Any]: ...

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]: ...


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


class DiscoveredFixture(BaseModel):
    source: VenueName = VenueName.MATCHBOOK
    source_event_id: str
    canonical_event_id: str
    home_team: str
    away_team: str
    competition: str
    target_competition_code: str | None = None
    kickoff_utc: datetime
    polymarket_matched: bool = False
    fixture_status: str | None = None
    in_running: bool | None = None
    live_score_supported: bool = False
    home_score: int | None = None
    away_score: int | None = None
    last_seen_at: datetime
    matched_market_count: int = Field(default=0, ge=0)
    discovered_market_count: int = Field(default=0, ge=0)
    matched_equivalent_count: int = Field(default=0, ge=0)
    market_family: str | None = None
    outcome_context: str | None = None
    best_matchbook_price: Decimal | None = None
    best_polymarket_price: Decimal | None = None
    current_net_edge: Decimal | None = None
    trigger_net_edge: Decimal | None = None
    distance_to_trigger_pp: Decimal | None = None
    quote_age_ms: int | None = Field(default=None, ge=0)
    quote_age_basis: str | None = None
    no_comparison_reason: str | None = None
    solver_is_arbitrage: bool = False


class FixtureDetailReadModel(BaseModel):
    fixture: DiscoveredFixture
    markets: list[FixtureMarketInventoryRow] = Field(default_factory=list)
    data_class: str = "live_paper_when_collected"
    paper_mode: str = "paper"
    execution_enabled: bool = False


class CollectionReport(BaseModel):
    started_at: datetime
    completed_at: datetime
    discovery_source: VenueName = VenueName.MATCHBOOK
    matching_venue: VenueName = VenueName.POLYMARKET
    raw_matchbook_events: int = Field(default=0, ge=0)
    raw_polymarket_events: int = Field(default=0, ge=0)
    normalized_matchbook_events: int = Field(default=0, ge=0)
    normalized_polymarket_events: int = Field(default=0, ge=0)
    matched_event_pairs: int = Field(default=0, ge=0)
    normalized_matchbook_markets: int = Field(default=0, ge=0)
    normalized_polymarket_markets: int = Field(default=0, ge=0)
    matched_market_pairs: int = Field(default=0, ge=0)
    order_books_fetched: int = Field(default=0, ge=0)
    paper_decisions: list[PaperScanDecision] = Field(default_factory=list)
    discovered_fixtures: list[DiscoveredFixture] = Field(default_factory=list)
    fixture_markets: dict[str, list[FixtureMarketInventoryRow]] = Field(default_factory=dict)
    issues: list[CollectorIssue] = Field(default_factory=list)

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
    """Discover, match and paper-scan Matchbook/Polymarket markets.

    The collector only calls read methods on venue clients. Unsupported payloads are
    reported and skipped. Event and market pairing is one-to-one so a single venue
    object cannot silently participate in multiple cross-venue matches in one pass.
    """

    def __init__(
        self,
        *,
        matchbook: MatchbookReadClient,
        polymarket: PolymarketReadClient,
        paper_scan: PaperScanService,
        event_matcher: EventMatcher | None = None,
        market_matcher: MarketMatcher | None = None,
        matchbook_normalizer: MatchbookNormalizer | None = None,
        polymarket_normalizer: PolymarketNormalizer | None = None,
    ) -> None:
        self.matchbook = matchbook
        self.polymarket = polymarket
        self.paper_scan = paper_scan
        self.event_matcher = event_matcher or EventMatcher()
        self.market_matcher = market_matcher or MarketMatcher(self.event_matcher)
        self.matchbook_normalizer = matchbook_normalizer or MatchbookNormalizer()
        self.polymarket_normalizer = polymarket_normalizer or PolymarketNormalizer()
        self.matchbook_builder = MatchbookObservationBuilder(self.matchbook_normalizer)
        self.polymarket_builder = PolymarketObservationBuilder(self.polymarket_normalizer)

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
        max_event_pairs: int = 25,
        max_market_pairs_per_event: int = 50,
    ) -> CollectionReport:
        if max_event_pairs <= 0 or max_market_pairs_per_event <= 0:
            raise ValueError("collector pair limits must be positive")
        started_at = datetime.now(UTC)
        issues: list[CollectorIssue] = []

        matchbook_payload = await self.matchbook.list_events(**(matchbook_event_filters or {}))
        polymarket_payload = await self.polymarket.list_events(**(polymarket_event_filters or {}))
        raw_matchbook_events = _extract_matchbook_items(matchbook_payload, "events")
        raw_polymarket_events = [item for item in polymarket_payload if isinstance(item, dict)]
        issues.extend(_matchbook_discovery_issues(matchbook_payload))
        scoped_matchbook_events, scope_issues = _scope_matchbook_events(raw_matchbook_events)
        issues.extend(scope_issues)

        matchbook_events = self._normalize_events(
            scoped_matchbook_events,
            venue=VenueName.MATCHBOOK,
            issues=issues,
        )
        polymarket_events = self._normalize_events(
            raw_polymarket_events,
            venue=VenueName.POLYMARKET,
            issues=issues,
        )
        queried_series_ids = _resolved_queried_series_ids(
            polymarket_event_filters,
            polymarket_queried_series_ids,
        )
        event_pairs = _greedy_unique_event_pairs(
            matchbook_events,
            polymarket_events,
            matcher=self.event_matcher,
        )[:max_event_pairs]
        matched_matchbook_ids = {
            left.canonical.source_event_id for left, _right, _match in event_pairs
        }
        discovered_by_id = {
            event.canonical.source_event_id: _discovered_fixture(
                event,
                polymarket_matched=event.canonical.source_event_id in matched_matchbook_ids,
                seen_at=started_at,
                polymarket_events=polymarket_events,
                queried_series_ids=queried_series_ids,
            )
            for event in matchbook_events
        }

        paired_right_by_left = {
            left.canonical.source_event_id: right for left, right, _ in event_pairs
        }
        decisions: list[PaperScanDecision] = []
        normalized_matchbook_markets = 0
        normalized_polymarket_markets = 0
        matched_market_pairs = 0
        order_books_fetched = 0
        fixture_markets: dict[str, list[FixtureMarketInventoryRow]] = {}

        for left_event in matchbook_events:
            fixture = discovered_by_id.get(left_event.canonical.source_event_id)
            right_event = paired_right_by_left.get(left_event.canonical.source_event_id)
            try:
                mb_started = perf_counter()
                mb_market_payload = await self.matchbook.list_markets(
                    left_event.canonical.source_event_id,
                    **(matchbook_market_filters or {}),
                )
                matchbook_market_latency_ms = _elapsed_ms(mb_started)
                matchbook_retrieved_at = datetime.now(UTC)
            except Exception as exc:
                issues.append(
                    CollectorIssue(
                        stage="list_markets",
                        venue=VenueName.MATCHBOOK,
                        source_id=left_event.canonical.source_event_id,
                        detail=str(exc),
                    )
                )
                if fixture is not None:
                    fixture.no_comparison_reason = "list_markets_unavailable"
                continue

            raw_matchbook = _extract_matchbook_items(mb_market_payload, "markets")
            left_markets, matchbook_inventory = self._inventory_markets(
                left_event,
                raw_matchbook,
                venue=VenueName.MATCHBOOK,
                issues=issues,
            )
            normalized_matchbook_markets += len(left_markets)

            right_markets: list[_NormalizedMarket] = []
            polymarket_inventory: list[InventoryMarket] = []
            polymarket_market_latency_ms = 0
            if right_event is not None:
                try:
                    pm_started = perf_counter()
                    pm_market_payload = await self.polymarket.list_markets(
                        right_event.canonical.source_event_id,
                        **(polymarket_market_filters or {}),
                    )
                    polymarket_market_latency_ms = _elapsed_ms(pm_started)
                except Exception as exc:
                    issues.append(
                        CollectorIssue(
                            stage="list_markets",
                            venue=VenueName.POLYMARKET,
                            source_id=right_event.canonical.source_event_id,
                            detail=str(exc),
                        )
                    )
                    if fixture is not None:
                        fixture.no_comparison_reason = "list_markets_unavailable"
                    pm_market_payload = []
                right_markets, polymarket_inventory = self._inventory_markets(
                    right_event,
                    [item for item in pm_market_payload if isinstance(item, dict)],
                    venue=VenueName.POLYMARKET,
                    issues=issues,
                )
                normalized_polymarket_markets += len(right_markets)

            market_pairs = _greedy_unique_market_pairs(
                left_markets,
                right_markets,
                matcher=self.market_matcher,
            )[:max_market_pairs_per_event]
            matched_market_pairs += len(market_pairs)
            if fixture is not None:
                fixture.matched_market_count = len(market_pairs)
                if right_event is not None and not market_pairs:
                    fixture.no_comparison_reason = "no_settlement_equivalent_market_pair"

            decisions_by_source_ids: dict[tuple[str, str], PaperScanDecision] = {}
            observations_by_market_id: dict[tuple[VenueName, str], VenueMarketObservation] = {}

            for left_market in left_markets:
                observation = self._try_matchbook_observation(
                    left_event,
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

            for right_market in right_markets:
                if right_event is None:
                    break
                books_by_token, poly_book_latency_ms, fetched, book_failed = await self._fetch_polymarket_books(
                    right_event,
                    right_market,
                    issues=issues,
                )
                order_books_fetched += fetched
                if book_failed:
                    continue
                observation = self._try_polymarket_observation(
                    right_event,
                    right_market,
                    books_by_token,
                    latency_ms=polymarket_market_latency_ms + poly_book_latency_ms,
                    issues=issues,
                )
                if observation is None:
                    continue
                for item in polymarket_inventory:
                    if item.source_market_id == right_market.canonical.source_market_id:
                        item.observation = observation
                observations_by_market_id[
                    (VenueName.POLYMARKET, right_market.canonical.source_market_id)
                ] = observation

            for left_market, right_market, match in market_pairs:
                if not scan_eligible_pair(left_market.canonical, right_market.canonical, match):
                    continue
                matchbook_observation = observations_by_market_id.get(
                    (VenueName.MATCHBOOK, left_market.canonical.source_market_id)
                )
                polymarket_observation = observations_by_market_id.get(
                    (VenueName.POLYMARKET, right_market.canonical.source_market_id)
                )
                if matchbook_observation is None or polymarket_observation is None:
                    if fixture is not None and fixture.current_net_edge is None:
                        fixture.no_comparison_reason = "order_book_unavailable"
                    continue
                decision = self.paper_scan.scan_pair(
                    matchbook_observation,
                    polymarket_observation,
                    fee_snapshots=fee_snapshots,
                    venue_costs=venue_costs,
                    fx_snapshots=fx_snapshots,
                    capital_limit_gbp=capital_limit_gbp,
                    minimum_net_edge=minimum_net_edge,
                    maximum_execution_risk=maximum_execution_risk,
                    minimum_mapping_confidence=minimum_mapping_confidence,
                    assumed_latency_ms=assumed_latency_ms,
                    recent_volatility_bps=recent_volatility_bps,
                )
                state = matchbook_fixture_state(left_event.raw)
                stored = decision.model_copy(
                    update={
                        "fixture_discovery_source": VenueName.MATCHBOOK,
                        "fixture_status": state.venue_status,
                        "in_running": state.in_running,
                        "live_score_supported": state.live_score_supported,
                        "home_score": state.home_score,
                        "away_score": state.away_score,
                    }
                )
                decisions.append(stored)
                decisions_by_source_ids[
                    (
                        left_market.canonical.source_market_id,
                        right_market.canonical.source_market_id,
                    )
                ] = stored
                if fixture is not None:
                    _apply_backend_comparison(
                        fixture,
                        left_market=left_market,
                        matchbook_observation=matchbook_observation,
                        polymarket_observation=polymarket_observation,
                        decision=stored,
                    )

            inventory_rows = assemble_fixture_inventory(
                matchbook_inventory,
                polymarket_inventory,
                matcher=self.market_matcher,
                decisions_by_source_ids=decisions_by_source_ids,
                venue_costs=venue_costs,
                fx_snapshots=fx_snapshots,
            )
            if fixture is not None:
                discovered_count, equivalent_count, best_edge = inventory_summary(inventory_rows)
                fixture.discovered_market_count = discovered_count
                fixture.matched_equivalent_count = equivalent_count
                if fixture.current_net_edge is None and best_edge is not None:
                    fixture.current_net_edge = best_edge
                fixture_markets[fixture.canonical_event_id] = inventory_rows

        completed_at = datetime.now(UTC)
        discovered_fixtures = [
            item.model_copy(update={"last_seen_at": completed_at})
            for item in discovered_by_id.values()
        ]
        return CollectionReport(
            started_at=started_at,
            completed_at=completed_at,
            discovery_source=VenueName.MATCHBOOK,
            matching_venue=VenueName.POLYMARKET,
            raw_matchbook_events=len(raw_matchbook_events),
            raw_polymarket_events=len(raw_polymarket_events),
            normalized_matchbook_events=len(matchbook_events),
            normalized_polymarket_events=len(polymarket_events),
            matched_event_pairs=len(event_pairs),
            normalized_matchbook_markets=normalized_matchbook_markets,
            normalized_polymarket_markets=normalized_polymarket_markets,
            matched_market_pairs=matched_market_pairs,
            order_books_fetched=order_books_fetched,
            paper_decisions=decisions,
            discovered_fixtures=discovered_fixtures,
            fixture_markets=fixture_markets,
            issues=issues,
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
            self.matchbook_normalizer if venue == VenueName.MATCHBOOK else self.polymarket_normalizer
        )
        for payload in payloads:
            source_id = str(payload.get("id", "")) or None
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
            self.matchbook_normalizer if venue == VenueName.MATCHBOOK else self.polymarket_normalizer
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
                raw_book = await self.polymarket.get_order_book(
                    event.canonical.source_event_id,
                    market.canonical.source_market_id,
                    runner.source_runner_id,
                )
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


def _discovered_fixture(
    event: _NormalizedEvent,
    *,
    polymarket_matched: bool,
    seen_at: datetime,
    polymarket_events: list[_NormalizedEvent],
    queried_series_ids: list[str] | None,
) -> DiscoveredFixture:
    state = matchbook_fixture_state(event.raw)
    canonical = event.canonical
    scoped = scope_matchbook_event(event.raw)
    return DiscoveredFixture(
        source_event_id=canonical.source_event_id,
        canonical_event_id=canonical_source_event_id(canonical),
        home_team=canonical.home_team,
        away_team=canonical.away_team,
        competition=canonical.competition,
        target_competition_code=scoped.competition.code.value if scoped.competition else None,
        kickoff_utc=canonical.kickoff_utc,
        polymarket_matched=polymarket_matched,
        fixture_status=state.venue_status,
        in_running=state.in_running,
        live_score_supported=state.live_score_supported,
        home_score=state.home_score,
        away_score=state.away_score,
        last_seen_at=seen_at,
        no_comparison_reason=(
            None
            if polymarket_matched
            else _unmatched_polymarket_reason(
                scoped.competition,
                polymarket_events=polymarket_events,
                queried_series_ids=queried_series_ids,
            )
        ),
        solver_is_arbitrage=False,
    )


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


def _scope_matchbook_events(
    payloads: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[CollectorIssue]]:
    scoped: list[dict[str, Any]] = []
    issues: list[CollectorIssue] = []
    for payload in payloads:
        decision: ScopeDecision = scope_matchbook_event(payload)
        source_id = str(payload.get("id", "")) or None
        if not decision.allowed:
            issues.append(
                CollectorIssue(
                    stage="target_competition",
                    venue=VenueName.MATCHBOOK,
                    source_id=source_id,
                    detail=(
                        f"{decision.reason}: sport={decision.sport!r} "
                        f"competition={decision.label!r}"
                    ),
                )
            )
            continue
        scoped.append(payload)
    return scoped, issues


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


def _apply_backend_comparison(
    fixture: DiscoveredFixture,
    *,
    left_market: _NormalizedMarket,
    matchbook_observation: VenueMarketObservation,
    polymarket_observation: VenueMarketObservation,
    decision: PaperScanDecision,
) -> None:
    """Copy solver/scan fields onto the discovery row. No frontend economics."""

    current_net = None
    if decision.payoff_scan is not None:
        current_net = quantized_edge(decision.payoff_scan.solution.roi)
    elif decision.depth_scan is not None:
        implied = decision.depth_scan.solution.implied_probability_sum
        if implied > 0:
            current_net = net_edge_from_implied_sum(implied)
    replace = fixture.current_net_edge is None or (
        current_net is not None and current_net > fixture.current_net_edge
    )
    if not replace:
        return
    trigger = decision.minimum_net_edge
    distance = (
        distance_to_trigger_pp(current_net, trigger) if current_net is not None else None
    )
    solver_arb = bool(
        decision.eligible_for_paper_simulation
        and (
            (
                decision.payoff_scan is not None
                and decision.payoff_scan.solution.is_arbitrage
            )
            or (
                decision.depth_scan is not None
                and decision.depth_scan.solution.is_arbitrage
            )
        )
    )
    no_reason = None
    if current_net is None:
        no_reason = decision.rejection_reasons[0] if decision.rejection_reasons else "no_comparison"
    elif not solver_arb and decision.rejection_reasons:
        no_reason = None
    fixture.market_family = left_market.canonical.family.value
    fixture.outcome_context = _outcome_context(left_market)
    fixture.best_matchbook_price = _best_observed_back(matchbook_observation)
    fixture.best_polymarket_price = _best_observed_back(polymarket_observation)
    fixture.current_net_edge = current_net
    fixture.trigger_net_edge = trigger
    fixture.distance_to_trigger_pp = distance
    fixture.quote_age_ms = decision.quote_age_ms
    fixture.quote_age_basis = decision.quote_age_basis
    fixture.no_comparison_reason = no_reason
    fixture.solver_is_arbitrage = solver_arb
    if current_net is None and not fixture.no_comparison_reason:
        fixture.no_comparison_reason = "no_comparison"


def _greedy_unique_event_pairs(
    left: list[_NormalizedEvent],
    right: list[_NormalizedEvent],
    *,
    matcher: EventMatcher,
) -> list[tuple[_NormalizedEvent, _NormalizedEvent, EventMatchResult]]:
    candidates: list[tuple[float, int, int, EventMatchResult]] = []
    for left_index, left_item in enumerate(left):
        for right_index, right_item in enumerate(right):
            match = matcher.match(left_item.canonical, right_item.canonical)
            if match.matched:
                candidates.append((match.confidence, left_index, right_index, match))
    candidates.sort(key=lambda item: item[0], reverse=True)

    used_left: set[int] = set()
    used_right: set[int] = set()
    result: list[tuple[_NormalizedEvent, _NormalizedEvent, EventMatchResult]] = []
    for _, left_index, right_index, match in candidates:
        if left_index in used_left or right_index in used_right:
            continue
        used_left.add(left_index)
        used_right.add(right_index)
        result.append((left[left_index], right[right_index], match))
    return result


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
    candidates.sort(key=lambda item: item[0], reverse=True)

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
