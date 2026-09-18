"""Issue #295: Kalshi order-book retrieval for approved catalogue pairs.

Live public Kalshi Trade API probes on 2026-09-18 for the owner-reported
tickers returned HTTP 200 in ~45ms with `orderbook_fp` present. The 8s
`order_book_timeout` on POST /paper/collect was therefore not provider
latency. Root cause: the collector fetched every nested Kalshi book before
catalogue admission, so approved BTTS waited behind unapproved 1X2/noise
under the 4-slot Kalshi limiter and the 20s manual-diagnostic envelope.

Architect follow-up on PR #297: leftover/unapproved Kalshi depth is not
fetched at all. Metadata/inventory remain visible. Live get_order_book
runs only for scan-eligible / Approved Market Catalogue pair legs.

These tests are fixture/demo. They are not live books and not a historical
backtest. PAPER / read-only. Polymarket off. Execution disabled.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic
from typing import Any

import pytest

from sports_hedge.application.collector import (
    DEFAULT_PROVIDER_CONCURRENCY,
    ScanAttribution,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.complete_set import scan_eligible_pair
from sports_hedge.application.fixture_inventory import InventoryComparisonStatus
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import DEFAULT_HOT_TTL_SECONDS, ScanLane
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
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient
from venue_cost_helpers import matchbook_polymarket_costs

KICKOFF = datetime(2026, 9, 18, 19, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
REGULATION = (
    "Resolves on 90 minutes of regulation time. Extra time and penalties do not count."
)
BTTS_TICKER = "KXEPLBTTS-26SEP18BRECFC-BTTS"
UNAPPROVED_TICKERS = (
    "KXEPLGAME-26SEP18BRECFC-BRE",
    "KXEPLGAME-26SEP18BRECFC-DRAW",
    "KXEPLGAME-26SEP18BRECFC-CHE",
)
FORBIDDEN_WRITE_METHODS = (
    "place_order",
    "cancel_order",
    "place_bet",
    "sign_wallet",
    "submit_order",
)
BTTS_SERIES = {
    "ticker": "KXEPLBTTS",
    "title": "Premier League Both Teams To Score",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
}


def _fx() -> list[FxRateSnapshot]:
    return [
        FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test_fx"),
        FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1"), source="functional_currency"),
    ]


def _costs() -> list[Any]:
    return [
        *matchbook_polymarket_costs(),
        kalshi_cost_from_series(BTTS_SERIES, captured_at=NOW),
    ]


def _mb_event() -> dict[str, Any]:
    return {
        "id": 29501,
        "name": "Brentford vs Chelsea",
        "start": KICKOFF.isoformat(),
        "competition-name": "Premier League",
    }


def _mb_btts() -> dict[str, Any]:
    return {
        "id": 29520,
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


def _kalshi_game_event() -> dict[str, Any]:
    return {
        "event_ticker": "KXEPLGAME-26SEP18BRECFC",
        "series_ticker": "KXEPLGAME",
        "title": "Brentford vs Chelsea",
        "category": "Sports",
        "strike_date": KICKOFF.isoformat(),
        "markets": [
            {
                "ticker": UNAPPROVED_TICKERS[0],
                "event_ticker": "KXEPLGAME-26SEP18BRECFC",
                "title": "Brentford vs Chelsea",
                "yes_sub_title": "Brentford",
                "rules_primary": REGULATION,
            },
            {
                "ticker": UNAPPROVED_TICKERS[1],
                "event_ticker": "KXEPLGAME-26SEP18BRECFC",
                "title": "Brentford vs Chelsea",
                "yes_sub_title": "Draw",
                "rules_primary": REGULATION,
            },
            {
                "ticker": UNAPPROVED_TICKERS[2],
                "event_ticker": "KXEPLGAME-26SEP18BRECFC",
                "title": "Brentford vs Chelsea",
                "yes_sub_title": "Chelsea",
                "rules_primary": REGULATION,
            },
        ],
    }


def _kalshi_events() -> list[dict[str, Any]]:
    return [_kalshi_game_event(), _kalshi_btts_event()]


def _book() -> dict[str, Any]:
    return {
        "orderbook_fp": {
            "yes_dollars": [["0.40", "100.00"]],
            "no_dollars": [["0.49", "200.00"]],
        }
    }


class _DisabledPolymarket:
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return []

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return []


class _Matchbook:
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {"events": [_mb_event()]}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": [_mb_btts()]}


class _Kalshi:
    def __init__(self, *, hang_btts: bool = False, hang_unapproved: bool = False) -> None:
        self.book_calls: list[str] = []
        self.hang_btts = hang_btts
        self.hang_unapproved = hang_unapproved

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {"events": _kalshi_events()}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": []}

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        return {**BTTS_SERIES, "ticker": series_ticker}

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
        if ticker in UNAPPROVED_TICKERS:
            if self.hang_unapproved:
                await asyncio.sleep(5)
            raise AssertionError(f"unapproved Kalshi book ticker {ticker} must not be fetched")
        if ticker != BTTS_TICKER:
            raise AssertionError(f"unexpected Kalshi book ticker {ticker}")
        if self.hang_btts:
            await asyncio.sleep(5)
        return _book()


async def _collect(kalshi: _Kalshi, *, provider_timeout: float = 8.0, cycle_timeout: float = 8.0):
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=_Matchbook(),
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
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
            enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
            cycle_timeout_seconds=cycle_timeout,
        )
    finally:
        repository.close()


def test_paper_and_execution_boundaries_hold() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    for client in (MatchbookClient, KalshiClient, PolymarketClient):
        for method in FORBIDDEN_WRITE_METHODS:
            assert not hasattr(client, method), f"{client.__name__}.{method} must not exist"


def test_timeout_and_lane_budgets_were_not_raised() -> None:
    settings = Settings()
    assert settings.paper_scan_provider_timeout_seconds == 8
    assert settings.paper_scan_manual_diagnostic_timeout_seconds == 20
    assert settings.paper_scan_hot_cycle_timeout_seconds == 25
    assert settings.paper_scan_kalshi_concurrency == 4
    assert DEFAULT_HOT_TTL_SECONDS == 90
    assert ScanLane.HOT.value == "hot"
    assert ScanLane.UNIVERSE.value == "universe"


def test_fixture_mb_kalshi_btts_is_approved_equivalent() -> None:
    assessment = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_mb_btts()]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=_kalshi_btts_event(),
            markets=list(_kalshi_btts_event()["markets"]),
            series=BTTS_SERIES,
        ),
    )
    assert assessment.state is CatalogueApprovalState.APPROVED_EQUIVALENT
    mb = normalize_payload_side(
        PayloadSide(venue=VenueName.MATCHBOOK, event=_mb_event(), markets=[_mb_btts()])
    )
    kalshi = normalize_payload_side(
        PayloadSide(
            venue=VenueName.KALSHI,
            event=_kalshi_btts_event(),
            markets=list(_kalshi_btts_event()["markets"]),
            series=BTTS_SERIES,
        )
    )
    match = MarketMatcher().match(mb, kalshi)
    assert catalogue_allows_solver(mb, kalshi) is True
    assert scan_eligible_pair(mb, kalshi, match) is True


@pytest.mark.asyncio
async def test_approved_btts_book_is_fetched_and_unapproved_game_is_not() -> None:
    kalshi = _Kalshi()
    report = await _collect(kalshi)
    assert kalshi.book_calls == [BTTS_TICKER]
    assert all(ticker not in kalshi.book_calls for ticker in UNAPPROVED_TICKERS)
    policy = report.scan_diagnostics["kalshi_order_book_policy"]
    assert policy["eligible_markets"] == 1
    assert policy["skipped_unapproved"] >= 1
    fixture = next(item for item in report.discovered_fixtures if item.matchbook_matched and item.kalshi_matched)
    assert fixture.no_comparison_reason != "order_book_unavailable"
    rows = report.fixture_markets[fixture.canonical_event_id]
    btts = [row for row in rows if row.family == "both_teams_to_score"]
    assert btts
    assert any(row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT for row in btts)
    assert any(row.entered_solver for row in btts)
    game_inventory = [
        row
        for row in rows
        if row.kalshi is not None
        and (
            "GAME" in row.kalshi.source_event_id
            or any(ticker in row.kalshi.source_market_id for ticker in UNAPPROVED_TICKERS)
        )
    ]
    assert game_inventory
    assert all(not row.entered_solver for row in game_inventory)
    assert all(row.kalshi is None or not row.kalshi.best_backs for row in game_inventory)
    btts_kalshi = next(row.kalshi for row in btts if row.kalshi is not None)
    assert btts_kalshi.fee_status
    assert btts_kalshi.fee_source
    assert report.paper_decisions
    assert Settings().sports_hedge_execution_enabled is False


@pytest.mark.asyncio
async def test_hanging_unapproved_game_tickers_are_never_called() -> None:
    kalshi = _Kalshi(hang_unapproved=True)
    started = monotonic()
    report = await _collect(kalshi, provider_timeout=0.2, cycle_timeout=1.2)
    elapsed = monotonic() - started
    assert kalshi.book_calls == [BTTS_TICKER]
    assert all(ticker not in kalshi.book_calls for ticker in UNAPPROVED_TICKERS)
    policy = report.scan_diagnostics["kalshi_order_book_policy"]
    assert policy["eligible_markets"] == 1
    assert policy["skipped_unapproved"] >= 1
    fixture = next(item for item in report.discovered_fixtures if item.matchbook_matched and item.kalshi_matched)
    rows = report.fixture_markets[fixture.canonical_event_id]
    btts = [row for row in rows if row.family == "both_teams_to_score"]
    assert any(row.entered_solver for row in btts)
    assert fixture.no_comparison_reason != "order_book_unavailable"
    game_inventory = [
        row
        for row in rows
        if row.kalshi is not None
        and (
            "GAME" in row.kalshi.source_event_id
            or any(ticker in row.kalshi.source_market_id for ticker in UNAPPROVED_TICKERS)
        )
    ]
    assert game_inventory
    assert all(not row.entered_solver for row in game_inventory)
    # Three hung GAME books at 5s each would exceed the 1.2s cycle if fetched.
    assert elapsed < 2.0


@pytest.mark.asyncio
async def test_approved_btts_timeout_fails_closed_with_precise_diagnostic() -> None:
    kalshi = _Kalshi(hang_btts=True)
    report = await _collect(kalshi, provider_timeout=0.25)
    assert BTTS_TICKER in kalshi.book_calls
    assert kalshi.book_calls[0] == BTTS_TICKER
    fixture = next(item for item in report.discovered_fixtures if item.matchbook_matched and item.kalshi_matched)
    assert fixture.no_comparison_reason == "order_book_unavailable"
    rows = report.fixture_markets.get(fixture.canonical_event_id) or []
    assert not any(row.entered_solver for row in rows)
    assert not report.paper_decisions
    details = [issue.detail for issue in report.issues if issue.stage == "order_book"]
    assert details
    assert any("order_book_timeout after 0.25s" in detail for detail in details)
    assert all("order_book_timeout after 8s" not in detail for detail in details)


@pytest.mark.asyncio
async def test_exhausted_remaining_soft_is_not_reported_as_eight_second_timeout() -> None:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=_Matchbook(),
        polymarket=_DisabledPolymarket(),
        kalshi=_Kalshi(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        provider_call_timeout_seconds=8.0,
    )
    collector._op_provider_timeout = 8.0
    collector._op_soft_deadline = monotonic() - 0.2
    collector._op_issues = []
    collector._timeouts_by_stage = {}
    collector._op_venue_health = {VenueName.KALSHI.value: "ok"}
    collector._op_operation_health = {}
    collector._attribution = ScanAttribution()
    collector._provider_inflight = {venue: 0 for venue in DEFAULT_PROVIDER_CONCURRENCY}

    async def never() -> dict[str, Any]:
        raise AssertionError("Kalshi HTTP must not start after remaining_soft is exhausted")

    try:
        payload, timed_out = await collector._wait_provider_unlocked(
            never(),
            stage="order_book",
            venue=VenueName.KALSHI,
            source_id=BTTS_TICKER,
            default=None,
        )
    finally:
        repository.close()
    assert timed_out is True
    assert payload is None
    assert collector._op_issues
    detail = collector._op_issues[0].detail
    assert "order_book_timeout after 0s" in detail
    assert "configured_timeout=8s" in detail
    assert "remaining_soft=" in detail
    assert "order_book_timeout after 8s" not in detail
