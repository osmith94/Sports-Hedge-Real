from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from time import perf_counter
from typing import Any, Protocol

from pydantic import BaseModel, Field

from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.domain.football import CanonicalEvent, CanonicalMarket
from sports_hedge.domain.models import VenueName
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


class CollectionReport(BaseModel):
    started_at: datetime
    completed_at: datetime
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
        fee_snapshots: list[FeeSnapshot] | None = None,
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

        matchbook_events = self._normalize_events(
            raw_matchbook_events,
            venue=VenueName.MATCHBOOK,
            issues=issues,
        )
        polymarket_events = self._normalize_events(
            raw_polymarket_events,
            venue=VenueName.POLYMARKET,
            issues=issues,
        )
        event_pairs = _greedy_unique_event_pairs(
            matchbook_events,
            polymarket_events,
            matcher=self.event_matcher,
        )[:max_event_pairs]

        decisions: list[PaperScanDecision] = []
        normalized_matchbook_markets = 0
        normalized_polymarket_markets = 0
        matched_market_pairs = 0
        order_books_fetched = 0

        for left_event, right_event, _ in event_pairs:
            try:
                mb_started = perf_counter()
                mb_market_payload = await self.matchbook.list_markets(
                    left_event.canonical.source_event_id,
                    **(matchbook_market_filters or {}),
                )
                matchbook_market_latency_ms = _elapsed_ms(mb_started)
            except Exception as exc:
                issues.append(
                    CollectorIssue(
                        stage="list_markets",
                        venue=VenueName.MATCHBOOK,
                        source_id=left_event.canonical.source_event_id,
                        detail=str(exc),
                    )
                )
                continue

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
                continue

            left_markets = self._normalize_markets(
                left_event,
                _extract_matchbook_items(mb_market_payload, "markets"),
                venue=VenueName.MATCHBOOK,
                issues=issues,
            )
            right_markets = self._normalize_markets(
                right_event,
                [item for item in pm_market_payload if isinstance(item, dict)],
                venue=VenueName.POLYMARKET,
                issues=issues,
            )
            normalized_matchbook_markets += len(left_markets)
            normalized_polymarket_markets += len(right_markets)

            market_pairs = _greedy_unique_market_pairs(
                left_markets,
                right_markets,
                matcher=self.market_matcher,
            )[:max_market_pairs_per_event]
            matched_market_pairs += len(market_pairs)

            for left_market, right_market, _ in market_pairs:
                observed_at = datetime.now(UTC)
                try:
                    matchbook_observation = self.matchbook_builder.build(
                        left_event.raw,
                        left_market.raw,
                        observed_at=observed_at,
                        source_latency_ms=matchbook_market_latency_ms,
                    )
                except (VenueNormalizationError, ValueError) as exc:
                    issues.append(
                        CollectorIssue(
                            stage="build_observation",
                            venue=VenueName.MATCHBOOK,
                            source_id=left_market.canonical.source_market_id,
                            detail=str(exc),
                        )
                    )
                    continue

                books_by_token: dict[str, dict[str, Any]] = {}
                poly_book_latency_ms = 0
                book_failed = False
                for runner in right_market.canonical.runners:
                    try:
                        book_started = perf_counter()
                        raw_book = await self.polymarket.get_order_book(
                            right_event.canonical.source_event_id,
                            right_market.canonical.source_market_id,
                            runner.source_runner_id,
                        )
                        poly_book_latency_ms += _elapsed_ms(book_started)
                        books_by_token[runner.source_runner_id] = raw_book
                        order_books_fetched += 1
                    except Exception as exc:
                        issues.append(
                            CollectorIssue(
                                stage="order_book",
                                venue=VenueName.POLYMARKET,
                                source_id=runner.source_runner_id,
                                detail=str(exc),
                            )
                        )
                        book_failed = True
                        break
                if book_failed:
                    continue

                try:
                    polymarket_observation = self.polymarket_builder.build(
                        right_event.raw,
                        right_market.raw,
                        books_by_token,
                        observed_at=observed_at,
                        source_latency_ms=polymarket_market_latency_ms + poly_book_latency_ms,
                        quote_age_ms=_polymarket_quote_age_ms(books_by_token, observed_at),
                    )
                except (VenueNormalizationError, ValueError) as exc:
                    issues.append(
                        CollectorIssue(
                            stage="build_observation",
                            venue=VenueName.POLYMARKET,
                            source_id=right_market.canonical.source_market_id,
                            detail=str(exc),
                        )
                    )
                    continue

                decisions.append(
                    self.paper_scan.scan_pair(
                        matchbook_observation,
                        polymarket_observation,
                        fee_snapshots=fee_snapshots,
                        fx_snapshots=fx_snapshots,
                        capital_limit_gbp=capital_limit_gbp,
                        minimum_net_edge=minimum_net_edge,
                        maximum_execution_risk=maximum_execution_risk,
                        minimum_mapping_confidence=minimum_mapping_confidence,
                        assumed_latency_ms=assumed_latency_ms,
                        recent_volatility_bps=recent_volatility_bps,
                    )
                )

        completed_at = datetime.now(UTC)
        return CollectionReport(
            started_at=started_at,
            completed_at=completed_at,
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

    def _normalize_markets(
        self,
        event: _NormalizedEvent,
        payloads: list[dict[str, Any]],
        *,
        venue: VenueName,
        issues: list[CollectorIssue],
    ) -> list[_NormalizedMarket]:
        result: list[_NormalizedMarket] = []
        normalizer = (
            self.matchbook_normalizer if venue == VenueName.MATCHBOOK else self.polymarket_normalizer
        )
        for payload in payloads:
            source_id = str(payload.get("id", payload.get("conditionId", ""))) or None
            try:
                result.append(
                    _NormalizedMarket(
                        payload,
                        normalizer.normalize_market(event.canonical, payload),
                    )
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
        return result


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


def _polymarket_quote_age_ms(
    books_by_token: dict[str, dict[str, Any]],
    observed_at: datetime,
) -> int:
    timestamps: list[int] = []
    for book in books_by_token.values():
        raw = book.get("timestamp")
        if raw is None:
            continue
        try:
            value = int(str(raw))
        except ValueError:
            continue
        # CLOB timestamps are normally Unix milliseconds; accept seconds defensively.
        if value < 10_000_000_000:
            value *= 1000
        timestamps.append(value)
    if not timestamps:
        return 0
    latest_ms = max(timestamps)
    observed_ms = int(observed_at.timestamp() * 1000)
    return max(0, observed_ms - latest_ms)
