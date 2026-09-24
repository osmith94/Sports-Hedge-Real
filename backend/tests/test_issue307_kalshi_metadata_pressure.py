"""Issue #307: Kalshi metadata pressure, catalogue-gated enrichment, orphan cleanup.

Owner-live on #306: Kalshi get_series/get_market ConnectTimeouts starved UNIVERSE
while cluster concurrency 8 reused the same series. These tests are fixture/demo
doubles. They are not live books, not a historical backtest, and not modelled
probabilities. PAPER / read-only. Execution disabled.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from time import monotonic
from typing import Any

import httpx
import pytest

from sports_hedge.application.collector import (
    DEFAULT_PROVIDER_CONCURRENCY,
    ReadOnlyCrossVenueCollector,
    ScanAttribution,
)
from sports_hedge.application.complete_set import scan_eligible_pair
from sports_hedge.application.live_refresh import LiveRefreshCoordinator, _merge_top_level_venue_health
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.provider_access import ProviderAccessLayer
from sports_hedge.application.scan_lanes import DEFAULT_HOT_TTL_SECONDS, ScanLane, WORKER_IDLE, WORKER_WAITING
from sports_hedge.catalogue.admission import catalogue_allows_solver
from sports_hedge.catalogue.classify import PayloadSide, classify_payload_pair, normalize_payload_side
from sports_hedge.catalogue.states import CatalogueApprovalState
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.venues.kalshi import KalshiClient, KalshiDiscoveryError
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient
from venue_cost_helpers import matchbook_polymarket_costs
from test_dual_cadence_scheduler import NOW as SCHED_NOW, FakeClock, _fixture, _report

KICKOFF = datetime(2026, 9, 18, 19, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
REGULATION = (
    "Resolves on 90 minutes of regulation time. Extra time and penalties do not count."
)
BTTS_TICKER = "KXEPLBTTS-26SEP18BRECFC-BTTS"
GAME_TICKERS = (
    "KXEPLGAME-26SEP18BRECFC-BRE",
    "KXEPLGAME-26SEP18BRECFC-DRAW",
    "KXEPLGAME-26SEP18BRECFC-CHE",
)
BTTS_SERIES = {
    "ticker": "KXEPLBTTS",
    "title": "Premier League Both Teams To Score",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
}
GAME_SERIES = {
    "ticker": "KXEPLGAME",
    "title": "Premier League",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
}
FORBIDDEN_WRITE_METHODS = (
    "place_order",
    "cancel_order",
    "place_bet",
    "sign_wallet",
    "submit_order",
)


def _fx() -> list[FxRateSnapshot]:
    return [
        FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test_fx"),
        FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1"), source="functional_currency"),
    ]


def _costs(series: dict[str, Any] = BTTS_SERIES) -> list[Any]:
    return [
        *matchbook_polymarket_costs(),
        kalshi_cost_from_series(series, captured_at=NOW),
    ]


def _book() -> dict[str, Any]:
    return {
        "orderbook_fp": {
            "yes_dollars": [["0.40", "100.00"]],
            "no_dollars": [["0.49", "200.00"]],
        }
    }


def _mb_event() -> dict[str, Any]:
    return {
        "id": 30701,
        "name": "Brentford vs Chelsea",
        "start": KICKOFF.isoformat(),
        "competition-name": "Premier League",
    }


def _mb_btts() -> dict[str, Any]:
    return {
        "id": 30720,
        "name": "Both Teams To Score",
        "runners": [
            {
                "id": 1,
                "name": "Yes",
                "prices": [
                    {"side": "back", "odds": "2.20", "available-amount": "80"},
                    {"side": "lay", "odds": "2.22", "available-amount": "80"},
                ],
            },
            {
                "id": 2,
                "name": "No",
                "prices": [
                    {"side": "back", "odds": "1.80", "available-amount": "80"},
                    {"side": "lay", "odds": "1.82", "available-amount": "80"},
                ],
            },
        ],
    }


def _mb_1x2() -> dict[str, Any]:
    return {
        "id": 30721,
        "name": "Match Odds",
        "runners": [
            {
                "id": 1,
                "name": "Brentford",
                "prices": [
                    {"side": "back", "odds": "2.40", "available-amount": "80"},
                    {"side": "lay", "odds": "2.42", "available-amount": "80"},
                ],
            },
            {
                "id": 2,
                "name": "Draw",
                "prices": [
                    {"side": "back", "odds": "3.40", "available-amount": "80"},
                    {"side": "lay", "odds": "3.42", "available-amount": "80"},
                ],
            },
            {
                "id": 3,
                "name": "Chelsea",
                "prices": [
                    {"side": "back", "odds": "3.10", "available-amount": "80"},
                    {"side": "lay", "odds": "3.12", "available-amount": "80"},
                ],
            },
        ],
    }


def _kalshi_btts_event() -> dict[str, Any]:
    return {
        "event_ticker": "KXEPLBTTS-26SEP18BRECFC",
        "series_ticker": "KXEPLBTTS",
        "title": "Brentford vs Chelsea",
        "category": "Sports",
        "strike_date": KICKOFF.isoformat(),
        "markets": [
            {
                "ticker": BTTS_TICKER,
                "event_ticker": "KXEPLBTTS-26SEP18BRECFC",
                "title": "Both Teams To Score",
                "yes_sub_title": "Yes",
                "rules_primary": REGULATION,
            }
        ],
    }


def _kalshi_game_event(*, rules: str | None = None) -> dict[str, Any]:
    markets = []
    for ticker, subtitle in zip(
        GAME_TICKERS, ("Brentford", "Draw", "Chelsea"), strict=True
    ):
        item = {
            "ticker": ticker,
            "event_ticker": "KXEPLGAME-26SEP18BRECFC",
            "title": "Brentford vs Chelsea",
            "yes_sub_title": subtitle,
        }
        if rules is not None:
            item["rules_primary"] = rules
        markets.append(item)
    return {
        "event_ticker": "KXEPLGAME-26SEP18BRECFC",
        "series_ticker": "KXEPLGAME",
        "title": "Brentford vs Chelsea",
        "category": "Sports",
        "strike_date": KICKOFF.isoformat(),
        "markets": markets,
    }


class _DisabledPolymarket:
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return []

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return []


class _Matchbook:
    def __init__(self, markets: list[dict[str, Any]] | None = None) -> None:
        self.markets = markets if markets is not None else [_mb_btts()]
        self.list_markets_calls = 0

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {"events": [_mb_event()]}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        self.list_markets_calls += 1
        return {"markets": list(self.markets)}


class _Kalshi:
    def __init__(
        self,
        events: list[dict[str, Any]],
        *,
        hang_get_market: bool = False,
        fail_get_market: bool = False,
        get_market_rules: str = REGULATION,
        hang_series: bool = False,
    ) -> None:
        self.events = events
        self.series_calls: list[str] = []
        self.get_market_calls: list[str] = []
        self.book_calls: list[str] = []
        self.hang_get_market = hang_get_market
        self.fail_get_market = fail_get_market
        self.get_market_rules = get_market_rules
        self.hang_series = hang_series

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {"events": [dict(item) for item in self.events]}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": []}

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        self.series_calls.append(str(series_ticker))
        if self.hang_series:
            await asyncio.sleep(30)
        if str(series_ticker) == "KXEPLBTTS":
            return dict(BTTS_SERIES)
        return {**GAME_SERIES, "ticker": str(series_ticker)}

    async def get_market(self, ticker: str) -> dict[str, Any]:
        self.get_market_calls.append(str(ticker))
        if self.hang_get_market:
            await asyncio.sleep(30)
        if self.fail_get_market:
            raise httpx.ConnectTimeout("Kalshi get_market ConnectTimeout")
        return {
            "ticker": str(ticker),
            "rules_primary": self.get_market_rules,
        }

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, outcome_id, filters
        ticker = str(market_id)
        self.book_calls.append(ticker)
        return _book()


async def _collect(
    matchbook: _Matchbook,
    kalshi: _Kalshi,
    *,
    provider_timeout: float = 8.0,
    cycle_timeout: float = 8.0,
    series: dict[str, Any] = BTTS_SERIES,
):
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=_DisabledPolymarket(),
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        provider_call_timeout_seconds=provider_timeout,
        provider_concurrency={
            VenueName.MATCHBOOK: 1,
            VenueName.POLYMARKET: 1,
            VenueName.KALSHI: 1,
        },
    )
    try:
        return await collector.collect_and_scan(
            venue_costs=_costs(series),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
            enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
            cycle_timeout_seconds=cycle_timeout,
        )
    finally:
        repository.close()


def _client(handler) -> tuple[KalshiClient, httpx.AsyncClient]:
    settings = Settings()
    transport = httpx.MockTransport(handler)
    http = httpx.AsyncClient(transport=transport)
    return KalshiClient(settings, client=http), http


def test_paper_and_execution_boundaries_hold() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    for client in (MatchbookClient, KalshiClient, PolymarketClient):
        for method in FORBIDDEN_WRITE_METHODS:
            assert not hasattr(client, method), f"{client.__name__}.{method} must not exist"


def test_timeouts_and_concurrency_were_not_raised() -> None:
    settings = Settings()
    assert settings.paper_scan_provider_timeout_seconds == 8
    assert settings.paper_scan_manual_diagnostic_timeout_seconds == 20
    assert settings.paper_scan_kalshi_concurrency == 4
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.KALSHI] == 4
    assert DEFAULT_HOT_TTL_SECONDS == 90
    assert ScanLane.HOT.value == "hot"
    assert ScanLane.UNIVERSE.value == "universe"


@pytest.mark.asyncio
async def test_eight_concurrent_get_series_share_one_http_call() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "series": {
                    "ticker": "KXEPLGAME",
                    "title": "Premier League",
                    "fee_type": "quadratic",
                    "fee_multiplier": 1,
                }
            },
        )

    venue, http = _client(handler)
    original = venue._get

    async def slow_get(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        await asyncio.sleep(0.05)
        return await original(path, params=params)

    venue._get = slow_get  # type: ignore[method-assign]
    try:
        results = await asyncio.gather(*[venue.get_series("KXEPLGAME") for _ in range(8)])
    finally:
        await http.aclose()

    assert seen == ["/trade-api/v2/series/KXEPLGAME"]
    assert all(item["ticker"] == "KXEPLGAME" for item in results)
    assert all(item is results[0] for item in results)
    cached = await venue.get_series("KXEPLGAME")
    assert cached is results[0]
    assert seen == ["/trade-api/v2/series/KXEPLGAME"]


@pytest.mark.asyncio
async def test_transient_get_series_failure_is_not_success_cached() -> None:
    seen: list[str] = []
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise httpx.ConnectTimeout("Kalshi get_series ConnectTimeout")
        return httpx.Response(
            200,
            json={"series": {"ticker": "KXEPLGAME", "title": "Premier League"}},
        )

    venue, http = _client(handler)
    original = venue._get

    async def slow_get(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        await asyncio.sleep(0.02)
        return await original(path, params=params)

    venue._get = slow_get  # type: ignore[method-assign]
    try:
        first = await asyncio.gather(
            *[venue.get_series("KXEPLGAME") for _ in range(4)],
            return_exceptions=True,
        )
        assert all(isinstance(item, httpx.ConnectTimeout) for item in first)
        assert "KXEPLGAME" not in venue._series_cache
        recovered = await venue.get_series("KXEPLGAME")
    finally:
        await http.aclose()

    assert recovered["ticker"] == "KXEPLGAME"
    assert seen == [
        "/trade-api/v2/series/KXEPLGAME",
        "/trade-api/v2/series/KXEPLGAME",
    ]


@pytest.mark.asyncio
async def test_concurrent_get_market_misses_single_flight() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(
            200,
            json={
                "market": {
                    "ticker": "KXEPLGAME-BRE",
                    "rules_primary": REGULATION,
                }
            },
        )

    venue, http = _client(handler)
    original = venue._get

    async def slow_get(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        await asyncio.sleep(0.05)
        return await original(path, params=params)

    venue._get = slow_get  # type: ignore[method-assign]
    try:
        results = await asyncio.gather(
            *[venue.get_market("KXEPLGAME-BRE") for _ in range(6)]
        )
    finally:
        await http.aclose()

    assert seen == ["/trade-api/v2/markets/KXEPLGAME-BRE"]
    assert all(item is results[0] for item in results)
    assert results[0]["rules_primary"] == REGULATION


@pytest.mark.asyncio
async def test_get_series_empty_ticker_does_not_http() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.url.path)
        return httpx.Response(500)

    venue, http = _client(handler)
    try:
        with pytest.raises(KalshiDiscoveryError, match="series ticker"):
            await venue.get_series("  ")
    finally:
        await http.aclose()
    assert seen == []


@pytest.mark.asyncio
async def test_unapproved_non_intersecting_kalshi_skips_get_market_and_books() -> None:
    kalshi = _Kalshi([_kalshi_game_event(), _kalshi_btts_event()])
    report = await _collect(_Matchbook([_mb_btts()]), kalshi)
    assert kalshi.get_market_calls == []
    assert GAME_SERIES["ticker"] not in kalshi.series_calls
    assert all(ticker not in kalshi.book_calls for ticker in GAME_TICKERS)
    assert kalshi.book_calls == [BTTS_TICKER]
    policy = report.scan_diagnostics["kalshi_order_book_policy"]
    assert policy["eligible_markets"] == 1
    assert policy["skipped_unapproved"] >= 1
    assert Settings().sports_hedge_execution_enabled is False


@pytest.mark.asyncio
async def test_approved_incomplete_1x2_enriches_then_can_fetch_depth() -> None:
    assessment = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_mb_1x2()]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=_kalshi_game_event(),
            markets=list(_kalshi_game_event()["markets"]),
        ),
    )
    assert assessment.state is not CatalogueApprovalState.APPROVED_EQUIVALENT
    kalshi = _Kalshi([_kalshi_game_event()], get_market_rules=REGULATION)
    report = await _collect(
        _Matchbook([_mb_1x2()]),
        kalshi,
        series=GAME_SERIES,
    )
    assert sorted(kalshi.get_market_calls) == sorted(GAME_TICKERS)
    assert all(ticker in kalshi.book_calls for ticker in GAME_TICKERS)
    fixture = next(
        item
        for item in report.discovered_fixtures
        if item.matchbook_matched and item.kalshi_matched
    )
    rows = report.fixture_markets[fixture.canonical_event_id]
    assert any(row.family == "match_result" and row.entered_solver for row in rows)
    mb = PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_mb_1x2()])
    enriched = PayloadSide(
        venue=VenueName.KALSHI,
        event=_kalshi_game_event(rules=REGULATION),
        markets=list(_kalshi_game_event(rules=REGULATION)["markets"]),
        series=GAME_SERIES,
    )
    assert classify_payload_pair(mb, enriched).state is CatalogueApprovalState.APPROVED_EQUIVALENT
    left_m = normalize_payload_side(mb)
    right_m = normalize_payload_side(enriched)
    match = MarketMatcher().match(left_m, right_m)
    assert catalogue_allows_solver(left_m, right_m) is True
    assert scan_eligible_pair(left_m, right_m, match) is True


@pytest.mark.asyncio
async def test_enrichment_timeout_fails_closed_without_depth() -> None:
    kalshi = _Kalshi([_kalshi_game_event()], hang_get_market=True)
    report = await _collect(
        _Matchbook([_mb_1x2()]),
        kalshi,
        provider_timeout=0.15,
        cycle_timeout=1.0,
        series=GAME_SERIES,
    )
    assert sorted(set(kalshi.get_market_calls)) == sorted(GAME_TICKERS)
    assert kalshi.book_calls == []
    fixture = next(
        item
        for item in report.discovered_fixtures
        if item.matchbook_matched and item.kalshi_matched
    )
    rows = report.fixture_markets.get(fixture.canonical_event_id) or []
    assert all(not row.entered_solver for row in rows)
    assert report.venue_health["matchbook"] == "ok"
    assert report.venue_health["kalshi"] in {"degraded", "timeout", "market_timeout"}


@pytest.mark.asyncio
async def test_orphaned_provider_task_exception_is_consumed() -> None:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=_Matchbook(),
        polymarket=_DisabledPolymarket(),
        kalshi=_Kalshi([_kalshi_btts_event()]),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        provider_call_timeout_seconds=8.0,
        cycle_timeout_seconds=None,
    )
    collector._inflight = set()
    collector._provider_calls = 0
    collector._provider_cancels = 0
    collector._inflight_orphaned = 0
    collector._peak_inflight = 0
    collector._op_deadline = None
    collector._op_soft_deadline = None
    loop = asyncio.get_running_loop()
    contexts: list[dict[str, Any]] = []
    previous = loop.get_exception_handler()

    def _handler(_loop: asyncio.AbstractEventLoop, context: dict[str, Any]) -> None:
        contexts.append(context)

    loop.set_exception_handler(_handler)
    try:

        async def stubborn() -> str:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await asyncio.sleep(0.08)
                raise RuntimeError("late ConnectTimeout") from None
            return "nope"

        started = monotonic()
        payload, timed_out = await collector._await_bounded(stubborn(), timeout=0.05)
        elapsed = monotonic() - started
        assert timed_out is True
        assert payload is None
        assert elapsed < 0.4
        assert collector._inflight_orphaned == 1
        await asyncio.sleep(0.2)
        assert not any(
            "never retrieved" in str(item.get("message") or "").casefold()
            or "never retrieved" in str(item.get("exception") or "").casefold()
            for item in contexts
        )
        assert all(
            "never retrieved" not in str(item).casefold() for item in contexts
        )
    finally:
        loop.set_exception_handler(previous)
        repository.close()


@pytest.mark.asyncio
async def test_kalshi_only_timeout_leaves_matchbook_health_ok() -> None:
    kalshi = _Kalshi([_kalshi_game_event()], hang_get_market=True)
    report = await _collect(
        _Matchbook([_mb_1x2()]),
        kalshi,
        provider_timeout=0.15,
        cycle_timeout=1.0,
        series=GAME_SERIES,
    )
    assert report.venue_health["matchbook"] == "ok"
    assert report.venue_health["kalshi"] != "ok"
    assert kalshi.book_calls == []
    # Top-level HOT/UNIVERSE merge is per venue. A Kalshi-only timeout cannot
    # paint Matchbook degraded. Owner-live Matchbook degradation needs separate
    # Matchbook evidence.
    merged = _merge_top_level_venue_health(
        {"matchbook": "ok", "kalshi": "ok", "polymarket": "ok"},
        {
            "matchbook": "ok",
            "kalshi": str(report.venue_health["kalshi"]),
            "polymarket": "ok",
        },
    )
    assert merged["matchbook"] == "ok"
    assert merged["kalshi"] != "ok"


def test_merge_does_not_copy_kalshi_failure_onto_matchbook() -> None:
    merged = _merge_top_level_venue_health(
        {"matchbook": "ok", "kalshi": "ok", "polymarket": "ok"},
        {"matchbook": "ok", "kalshi": "market_timeout", "polymarket": "ok"},
    )
    assert merged["matchbook"] == "ok"
    assert merged["kalshi"] == "degraded"
    assert merged["polymarket"] == "ok"


async def _ignore_cancel_until(event: asyncio.Event) -> None:
    """Stay alive after asyncio cancel, like a stuck HTTP connect."""

    while not event.is_set():
        task = asyncio.current_task()
        if task is not None:
            while task.cancelling():
                task.uncancel()
        try:
            await asyncio.wait_for(event.wait(), timeout=0.02)
        except TimeoutError:
            continue
        except asyncio.CancelledError:
            continue


def _prep_capacity_collector(
    collector: ReadOnlyCrossVenueCollector,
    access: ProviderAccessLayer,
    *,
    timeout: float = 0.05,
) -> None:
    collector._provider_access = access
    collector._op_request_lane = ScanLane.UNIVERSE.value
    collector._op_provider_timeout = timeout
    collector._op_venue_timeout = timeout
    collector._op_deadline = None
    collector._op_soft_deadline = None
    collector._op_issues = []
    collector._op_venue_health = {
        VenueName.MATCHBOOK.value: "ok",
        VenueName.POLYMARKET.value: "ok",
        VenueName.KALSHI.value: "ok",
    }
    collector._op_operation_health = {}
    collector._attribution = ScanAttribution()
    collector._inflight = set()
    collector._provider_inflight = {venue: 0 for venue in DEFAULT_PROVIDER_CONCURRENCY}
    collector._provider_peak_inflight = {venue: 0 for venue in DEFAULT_PROVIDER_CONCURRENCY}
    collector._provider_calls = 0
    collector._provider_cancels = 0
    collector._inflight_orphaned = 0
    collector._peak_inflight = 0
    collector._timeouts_by_stage = {}


@pytest.mark.asyncio
async def test_provider_lease_stays_occupied_until_cancel_resistant_task_finishes() -> None:
    layer = ProviderAccessLayer(
        {
            VenueName.MATCHBOOK: 1,
            VenueName.POLYMARKET: 8,
            VenueName.KALSHI: 4,
        }
    )
    started = asyncio.Event()
    release = asyncio.Event()

    async def stubborn() -> str:
        started.set()
        await _ignore_cancel_until(release)
        return "ok"

    async def first() -> str:
        async with layer.acquire(VenueName.MATCHBOOK, lane="universe") as lease:
            task = asyncio.create_task(stubborn())
            done, _pending = await asyncio.wait({task}, timeout=0.05)
            if task not in done:
                task.cancel()
                assert lease.hold_until_task(task) is True
            return "timed_out"

    assert await first() == "timed_out"
    await started.wait()
    assert layer.snapshot().inflight["matchbook"] == 1

    second_started = asyncio.Event()

    async def second() -> None:
        async with layer.acquire(VenueName.MATCHBOOK, lane="universe"):
            second_started.set()

    waiter = asyncio.create_task(second())
    await asyncio.sleep(0.08)
    assert not second_started.is_set()
    assert layer.snapshot().inflight["matchbook"] == 1
    release.set()
    await asyncio.wait_for(waiter, timeout=1.0)
    assert second_started.is_set()
    assert layer.snapshot().inflight["matchbook"] == 0


@pytest.mark.asyncio
async def test_matchbook_list_markets_peak_live_stays_within_concurrency() -> None:
    access = ProviderAccessLayer(
        {
            VenueName.MATCHBOOK: 4,
            VenueName.POLYMARKET: 8,
            VenueName.KALSHI: 4,
        }
    )
    live = {"n": 0, "peak": 0}
    lock = asyncio.Lock()
    release = asyncio.Event()

    class StubbornMatchbook:
        async def list_events(self, **filters: Any) -> dict[str, Any]:
            del filters
            return {"events": []}

        async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
            del filters
            async with lock:
                live["n"] += 1
                live["peak"] = max(live["peak"], live["n"])
            try:
                await _ignore_cancel_until(release)
                return {"markets": [], "id": event_id}
            finally:
                async with lock:
                    live["n"] -= 1

    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=StubbornMatchbook(),
        polymarket=_DisabledPolymarket(),
        kalshi=_Kalshi([_kalshi_btts_event()]),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        provider_call_timeout_seconds=0.05,
        provider_access=access,
        provider_concurrency={
            VenueName.MATCHBOOK: 4,
            VenueName.POLYMARKET: 8,
            VenueName.KALSHI: 4,
        },
    )
    _prep_capacity_collector(collector, access, timeout=0.05)
    try:
        first_wave = [
            asyncio.create_task(
                collector._wait_provider(
                    collector.matchbook.list_markets(index),
                    stage="list_markets",
                    venue=VenueName.MATCHBOOK,
                    source_id=str(index),
                    default=None,
                )
            )
            for index in range(2)
        ]
        await asyncio.sleep(0.08)
        assert live["n"] == 2
        assert access.snapshot().inflight["matchbook"] == 2
        assert access.lower_in_use[VenueName.MATCHBOOK] == 2
        assert access.limits[VenueName.MATCHBOOK] == 4
        results = await asyncio.gather(*first_wave)
        assert all(timed_out for _payload, timed_out in results)
        assert live["n"] == 2
        assert access.snapshot().inflight["matchbook"] == 2

        retry_started = asyncio.Event()

        async def retry_wave() -> list[tuple[Any, bool]]:
            retry_started.set()
            return list(
                await asyncio.gather(
                    *[
                        collector._wait_provider(
                            collector.matchbook.list_markets(100 + index),
                            stage="list_markets",
                            venue=VenueName.MATCHBOOK,
                            source_id=str(100 + index),
                            default=None,
                        )
                        for index in range(2)
                    ]
                )
            )

        retry_task = asyncio.create_task(retry_wave())
        await retry_started.wait()
        await asyncio.sleep(0.12)
        assert live["n"] == 3
        assert live["peak"] == 3
        assert access.snapshot().inflight["matchbook"] == 3
        assert access.peak_lower_in_use[VenueName.MATCHBOOK] <= 3
        assert access.limits[VenueName.MATCHBOOK] == 4
        assert not retry_task.done()
        release.set()
        await asyncio.wait_for(retry_task, timeout=1.0)
        assert live["peak"] == 3
        assert live["n"] == 0
        assert access.snapshot().inflight["matchbook"] == 0
        assert collector._provider_peak_inflight[VenueName.MATCHBOOK] <= 4
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_cancel_resistant_get_series_single_flight_does_not_accumulate_http() -> None:
    live = {"n": 0, "peak": 0}
    calls = {"n": 0}
    release = asyncio.Event()
    lock = asyncio.Lock()

    async def hang_get(path: str, *, params: dict[str, Any] | None = None) -> dict[str, Any]:
        del params
        calls["n"] += 1
        async with lock:
            live["n"] += 1
            live["peak"] = max(live["peak"], live["n"])
        try:
            await _ignore_cancel_until(release)
            return {
                "series": {
                    "ticker": "KXEPLGAME",
                    "title": "Premier League",
                    "fee_type": "quadratic",
                    "fee_multiplier": 1,
                }
            }
        finally:
            async with lock:
                live["n"] -= 1

    settings = Settings()
    venue = KalshiClient(settings, client=httpx.AsyncClient())
    venue._get = hang_get  # type: ignore[method-assign]
    access = ProviderAccessLayer(
        {
            VenueName.MATCHBOOK: 4,
            VenueName.POLYMARKET: 8,
            VenueName.KALSHI: 4,
        }
    )
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=_Matchbook(),
        polymarket=_DisabledPolymarket(),
        kalshi=venue,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        provider_call_timeout_seconds=0.05,
        provider_access=access,
    )
    _prep_capacity_collector(collector, access, timeout=0.05)
    try:
        first = [
            asyncio.create_task(
                collector._wait_provider(
                    venue.get_series("KXEPLGAME"),
                    stage="get_series",
                    venue=VenueName.KALSHI,
                    source_id="KXEPLGAME",
                    default=None,
                )
            )
            for _ in range(4)
        ]
        await asyncio.sleep(0.08)
        assert live["n"] == 1
        assert calls["n"] == 1
        await asyncio.gather(*first)
        assert live["n"] == 1
        retry = [
            asyncio.create_task(
                collector._wait_provider(
                    venue.get_series("KXEPLGAME"),
                    stage="get_series",
                    venue=VenueName.KALSHI,
                    source_id="KXEPLGAME",
                    default=None,
                )
            )
            for _ in range(4)
        ]
        await asyncio.sleep(0.08)
        assert live["n"] == 1
        assert live["peak"] == 1
        assert calls["n"] == 1
        release.set()
        await asyncio.gather(*retry)
        assert live["peak"] == 1
        assert calls["n"] == 1
    finally:
        repository.close()
        await venue._client.aclose()


@pytest.mark.asyncio
async def test_hot_empty_scope_heartbeat_while_universe_runs_then_starts_independently() -> None:
    clock = FakeClock(SCHED_NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator._next_hot_due = SCHED_NOW
    coordinator._next_universe_due = SCHED_NOW
    coordinator._stop = asyncio.Event()
    universe_started = asyncio.Event()
    universe_hold = asyncio.Event()
    hot_ticks: list[str] = []

    async def universe_runner() -> Any:
        universe_started.set()
        await universe_hold.wait()
        return _report([], when=clock.now, scan_lane=ScanLane.UNIVERSE.value)

    async def tick(plan=None) -> Any:
        if plan is None:
            return None
        if plan.lane == ScanLane.HOT.value:
            hot_ticks.append(plan.reason)
            return _report(
                [
                    _fixture(
                        "live",
                        kickoff=SCHED_NOW - timedelta(minutes=1),
                        in_running=True,
                    )
                ],
                when=clock.now,
                scan_lane=ScanLane.HOT.value,
            )
        return None

    never = coordinator.status.hot
    assert never.last_heartbeat_at is None
    assert never.last_plan_reason is None
    assert never.worker_state == WORKER_IDLE

    universe_task = asyncio.create_task(
        coordinator.run_cycle(
            universe_runner, timeout_seconds=None, scan_lane=ScanLane.UNIVERSE
        )
    )
    await universe_started.wait()
    assert coordinator._universe_in_progress is True

    hot_task = asyncio.create_task(coordinator._hot_loop(tick))
    await asyncio.sleep(0.15)
    hot = coordinator.status.hot
    assert hot.last_heartbeat_at is not None
    assert hot.last_plan_reason == "hot_scope_empty"
    assert hot.worker_state == WORKER_WAITING
    assert "scope empty" in (hot.operator_summary or "").casefold()
    assert "worker alive" in (hot.operator_summary or "").casefold()
    assert hot_ticks == []
    assert coordinator._universe_in_progress is True

    live = _fixture("live", kickoff=SCHED_NOW - timedelta(minutes=1), in_running=True)
    coordinator.record_report(
        _report([live], when=SCHED_NOW, scan_lane=ScanLane.HOT.value),
        scan_lane=ScanLane.HOT,
        advance_hot_due=False,
    )
    await asyncio.sleep(0.2)
    assert hot_ticks
    assert coordinator._universe_in_progress is True

    coordinator._stop.set()
    universe_hold.set()
    hot_task.cancel()
    try:
        await hot_task
    except asyncio.CancelledError:
        pass
    await universe_task
    assert Settings().sports_hedge_execution_enabled is False
