"""BACKGROUND prices catalogue rows, then PAPER entry reprices exact IDs once.

Deterministic fixture/demo books, not live quotes. Thresholds, fees, and
execution rules are the current Settings defaults except where a case sets
the existing risk cap or treasury seed. Autofill is the production
LIVE_PAPER flag. A capture-eligible row gets one discovery scan and one
execution reprice. Dull and risk-capped rows do not take the second read.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from test_dual_cadence_scheduler import FakeClock
from test_issue316_catalogue_registry import _costs, _fx
from test_issue344_price_engine import (
    NOW,
    FakeKalshi,
    FakeMatchbook,
    _engine,
    _mb_btts,
    _row,
)

from sports_hedge.api import paper as paper_api
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.price_engine import PriceEnginePriority
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.trades import PaperTradeState
from sports_hedge.persistence.liquidity import SqlitePaperLiquidityRepository
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger

DULL_COUNT = 6
RICH_MARKET = "316199"
RICH_EVENT = "8899"
RICH_TICKER = "KXEPLBTTS-RICH-BTTS"


class _Books(FakeKalshi):
    def __init__(self) -> None:
        super().__init__()
        self.by_ticker: dict[str, dict] = {}

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: object,
    ) -> dict:
        del event_id, outcome_id, filters
        ticker = str(market_id)
        self.book_calls.append(ticker)
        return self.by_ticker[ticker]


class _CountingScan(PaperScanService):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.scan_calls = 0

    def scan_pair(self, left, right, **kwargs):
        self.scan_calls += 1
        return super().scan_pair(left, right, **kwargs)


def _book(yes: str, no: str) -> dict:
    return {
        "orderbook_fp": {
            "yes_dollars": [[yes, "500.00"]],
            "no_dollars": [[no, "500.00"]],
        }
    }


def _market(market_id: str) -> dict:
    market = _mb_btts(int(market_id), odds="2.20")
    market["runners"][0]["prices"][0]["available-amount"] = "500"
    market["runners"][1]["prices"] = [
        {"side": "back", "odds": "1.80", "available-amount": "500"},
        {"side": "lay", "odds": "1.82", "available-amount": "500"},
    ]
    return market


def _catalogue(include_rich: bool):
    rows = []
    matchbook = FakeMatchbook()
    kalshi = _Books()
    for index in range(DULL_COUNT):
        market_id = str(316100 + index)
        event = f"KXEPLBTTS-D{index}"
        rows.append(
            _row(
                suffix=f"d{index}",
                matchbook_event_id=str(8800 + index),
                matchbook_market_id=market_id,
                kalshi_event=event,
            )
        )
        matchbook.payloads[market_id] = _market(market_id)
        kalshi.by_ticker[f"{event}-BTTS"] = _book("0.40", "0.49")
    if include_rich:
        rows.append(
            _row(
                suffix="rich",
                matchbook_event_id=RICH_EVENT,
                matchbook_market_id=RICH_MARKET,
                kalshi_event="KXEPLBTTS-RICH",
            )
        )
        matchbook.payloads[RICH_MARKET] = _market(RICH_MARKET)
        kalshi.by_ticker[RICH_TICKER] = _book("0.20", "0.70")
    return rows, matchbook, kalshi


async def _background_entry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    include_rich: bool,
    treasury_seed: Decimal,
    max_execution_risk: int | None = None,
):
    defaults = Settings()
    settings = Settings(
        paper_autofill_enabled=True,
        max_execution_risk=(
            defaults.max_execution_risk
            if max_execution_risk is None
            else max_execution_risk
        ),
    )
    rows, matchbook, kalshi = _catalogue(include_rich)
    repository = SqliteMarketIntelligenceRepository()
    scan = _CountingScan(
        MarketIntelligenceService(repository),
        settings=settings,
        liquidity=SqlitePaperLiquidityRepository(
            tmp_path / f"{name}-liquidity.sqlite",
            matchbook_gbp=Decimal(5000),
            polymarket_usd=Decimal(5000),
            kalshi_usd=Decimal(5000),
        ),
    )
    ledger = SqlitePaperLedger(
        tmp_path / f"{name}-paper.sqlite",
        seed_gbp=treasury_seed,
        usd_gbp_per_unit=Decimal("0.75"),
        fx_source="test",
    )
    watchlist = WatchlistService(
        SqliteWatchlistRepository(tmp_path / f"{name}.watch"),
        max_quote_age_ms=10_000,
    )
    operations = PaperOperationsService(
        watchlist=watchlist,
        alerts=PriorityAlertService(),
        settings=settings,
        ledger=ledger,
    )
    captured: list = []
    real_chain = operations.persist_triggered_chain

    def persist_triggered_chain(decision, **kwargs):
        captured.append(decision)
        return real_chain(decision, **kwargs)

    operations.persist_triggered_chain = persist_triggered_chain

    def operations_factory(watchlist_arg=None, alerts=None):
        del alerts
        if watchlist_arg is not None:
            operations.watchlist = watchlist_arg
        operations.settings = settings
        return operations

    monkeypatch.setattr(paper_api, "get_paper_operations_service", operations_factory)
    engine, _, _, _layer = _engine(
        rows,
        matchbook=matchbook,
        kalshi=kalshi,
        paper_scan=scan,
        clock=FakeClock(NOW),
    )
    engine.venue_costs = _costs()
    engine.fx_snapshots = _fx()
    paper_api.bind_price_engine_item_persist(
        engine,
        service=scan,
        audit=SqlitePaperScanRepository(tmp_path / f"{name}-audit.sqlite"),
        watchlist=watchlist,
    )
    try:
        result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
        await engine.drain_item_captures()
        return result, scan, matchbook, kalshi, operations, captured
    finally:
        repository.close()


def _open_trades(operations: PaperOperationsService):
    return [
        trade
        for trade in operations.list_active_trades()
        if trade.state is PaperTradeState.OPEN
    ]


def _assert_no_rediscovery(
    matchbook: FakeMatchbook,
    kalshi: _Books,
    rows: int,
    *,
    extra_exact_reads: int = 0,
) -> None:
    assert matchbook.list_events_calls == 0
    assert kalshi.list_events_calls == 0
    assert matchbook.list_markets_calls == []
    assert kalshi.list_markets_calls == []
    assert len(matchbook.get_market_calls) == rows + extra_exact_reads
    assert len(kalshi.book_calls) == rows + extra_exact_reads


@pytest.mark.asyncio
async def test_background_opens_one_paper_trade_after_one_execution_reprice(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    defaults = Settings()
    assert defaults.min_net_edge == 0.01
    assert defaults.max_execution_risk == 60
    assert defaults.paper_autofill_enabled is False
    assert defaults.sports_hedge_mode == "paper"
    assert defaults.sports_hedge_execution_enabled is False

    result, scan, matchbook, kalshi, operations, captured = await _background_entry(
        tmp_path,
        monkeypatch,
        name="funded",
        include_rich=True,
        treasury_seed=Decimal(5000),
    )
    expected_rows = DULL_COUNT + 1
    assert len(result.evaluated) == expected_rows
    assert len(result.decisions) == expected_rows
    assert scan.scan_calls == expected_rows + 1
    _assert_no_rediscovery(matchbook, kalshi, expected_rows, extra_exact_reads=1)
    assert matchbook.get_market_calls.count((RICH_EVENT, RICH_MARKET)) == 2
    assert kalshi.book_calls.count(RICH_TICKER) == 2

    dull = [item for item in result.decisions if not item.eligible_for_paper_simulation]
    rich = [item for item in result.decisions if item.eligible_for_paper_simulation]
    assert len(dull) == DULL_COUNT
    assert len(rich) == 1
    qualifying = rich[0]
    assert all(item.minimum_net_edge == Decimal("0.01") for item in result.decisions)
    assert all("no_positive_edge" in item.rejection_reasons for item in dull)
    assert qualifying.rejection_reasons == []
    assert qualifying.allocation is not None and qualifying.allocation.accepted
    assert qualifying.execution_risk is not None
    assert qualifying.execution_risk.score <= defaults.max_execution_risk
    assert qualifying not in captured
    execution = next(
        item for item in captured if item.canonical_market_id == qualifying.canonical_market_id
    )
    assert execution.eligible_for_paper_simulation is True

    opened = _open_trades(operations)
    assert len(opened) == 1
    assert opened[0].state is PaperTradeState.OPEN
    assert opened[0].capital_locked_gbp is not None
    assert opened[0].capital_locked_gbp > 0
    assert opened[0].canonical_market_id == qualifying.canonical_market_id


@pytest.mark.asyncio
async def test_subthreshold_background_row_creates_no_paper_trade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, scan, matchbook, kalshi, operations, captured = await _background_entry(
        tmp_path,
        monkeypatch,
        name="dull",
        include_rich=False,
        treasury_seed=Decimal(5000),
    )
    assert scan.scan_calls == DULL_COUNT
    assert len(result.decisions) == DULL_COUNT
    assert all(not item.eligible_for_paper_simulation for item in result.decisions)
    assert all("no_positive_edge" in item.rejection_reasons for item in result.decisions)
    assert len(captured) == DULL_COUNT
    assert _open_trades(operations) == []
    _assert_no_rediscovery(matchbook, kalshi, DULL_COUNT)


@pytest.mark.asyncio
async def test_qualifying_background_row_without_treasury_creates_no_paper_trade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, scan, matchbook, kalshi, operations, captured = await _background_entry(
        tmp_path,
        monkeypatch,
        name="unfunded",
        include_rich=True,
        treasury_seed=Decimal("0.01"),
    )
    qualifying = next(item for item in result.decisions if item.eligible_for_paper_simulation)
    assert qualifying.allocation is not None and qualifying.allocation.accepted
    assert qualifying not in captured
    assert any(
        item.canonical_market_id == qualifying.canonical_market_id for item in captured
    )
    assert scan.scan_calls == DULL_COUNT + 2
    assert _open_trades(operations) == []
    assert operations._entry_rejections
    assert any(
        reason == "insufficient_spendable_treasury"
        for reason in operations._entry_rejections.values()
    )
    _assert_no_rediscovery(matchbook, kalshi, DULL_COUNT + 1, extra_exact_reads=1)


@pytest.mark.asyncio
async def test_qualifying_background_row_over_risk_cap_creates_no_paper_trade(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    result, scan, matchbook, kalshi, operations, captured = await _background_entry(
        tmp_path,
        monkeypatch,
        name="risk",
        include_rich=True,
        treasury_seed=Decimal(5000),
        max_execution_risk=0,
    )
    assert scan.scan_calls == DULL_COUNT + 1
    assert all(not item.eligible_for_paper_simulation for item in result.decisions)
    assert any(
        "execution_risk_above_threshold" in item.rejection_reasons for item in result.decisions
    )
    assert len(captured) == DULL_COUNT + 1
    assert _open_trades(operations) == []
    _assert_no_rediscovery(matchbook, kalshi, DULL_COUNT + 1)
