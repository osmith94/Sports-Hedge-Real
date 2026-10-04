"""Execution-time exact-ID reprice before a PAPER fill.

Discovery pricing may qualify from the latest known books, including when
those books are past the paper-entry quote-age gate. The fill uses one
contemporaneous complete-set read of the same persisted native IDs.
Fixture/demo books only. No live venue orders.
"""

from __future__ import annotations

import inspect
from copy import deepcopy
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_dual_cadence_scheduler import FakeClock
from test_issue316_catalogue_registry import _costs, _fx
from test_issue344_price_engine import NOW, FakeKalshi, FakeMatchbook, _engine, _mb_btts, _row

from sports_hedge.api import paper as paper_api
from sports_hedge.application.capture_replay import FORBIDDEN_WRITE_METHODS
from sports_hedge.application.executable_liquidity import decision_net_edge
from sports_hedge.application.execution_reprice import (
    EXECUTION_REPRICE_FAILED,
    EXECUTION_REPRICE_NO_LONGER_QUALIFYING,
    EXECUTION_REPRICE_STALE,
    execution_entry_block,
    execution_reprice_permitted,
)
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.price_engine import CataloguePriceEngine, PriceEnginePriority
from sports_hedge.arbitrage.models import PayoffSolution
from sports_hedge.arbitrage.payoff_scan import PayoffScanResult
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.watchlist.models import LifecycleEventType, OpportunityStatus
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.paper.trades import PaperTradeState
from sports_hedge.persistence.liquidity import SqlitePaperLiquidityRepository
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.venues import KalshiClient, MatchbookClient, PolymarketClient
from sports_hedge.venues.matchbook import MatchbookMarketGoneError

MARKET = "316199"
EVENT = "8899"
TICKER = "KXEPLBTTS-RICH-BTTS"
ENTRY_ORDER = (
    LifecycleEventType.QUALIFYING_DETECTED,
    LifecycleEventType.PAPER_ELIGIBLE,
    LifecycleEventType.PAPER_FILL_ATTEMPTED,
    LifecycleEventType.PAPER_FILL_COMPLETE,
)


def _decision(**updates: object) -> PaperScanDecision:
    decision = PaperScanDecision(
        scanned_at=NOW,
        canonical_market_id="mkt-exec",
        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=["register"]),
        payoff_scan=PayoffScanResult(
            solution=PayoffSolution(
                is_arbitrage=True,
                roi=Decimal("0.41"),
                minimum_state_pnl=Decimal("1"),
                numerically_validated=True,
            )
        ),
        minimum_net_edge=Decimal("0.01"),
        solver_model="strict_complete_set",
        eligible_for_paper_simulation=False,
        rejection_reasons=["stale_quote"],
    )
    if updates:
        return decision.model_copy(update=updates)
    return decision


def test_only_known_stale_quote_may_request_execution_reprice() -> None:
    stale = _decision()
    assert stale.eligible_for_paper_simulation is False
    assert execution_reprice_permitted(stale) is True

    fresh = _decision(eligible_for_paper_simulation=True, rejection_reasons=[])
    assert execution_reprice_permitted(fresh) is True

    # A diagnostic settlement label is a real rejection if a caller still
    # places it on a live decision. New scans do not emit it, so they do not
    # need a non-blocking exception list.
    assumed = _decision(rejection_reasons=["stale_quote", "paper_assumed_equivalent"])
    assert execution_reprice_permitted(assumed) is False

    blocked = {
        "missing_costs": ["stale_quote", "missing_venue_cost:kalshi"],
        "missing_fx": ["stale_quote", "missing_fx_rate:USD"],
        "semantics": ["stale_quote", "market_not_equivalent"],
        "no_depth": ["stale_quote", "missing_executable_outcome_depth"],
        "risk": ["execution_risk_above_threshold"],
        "unknown_age": ["unknown_quote_age"],
        "below_edge": ["net_edge_below_threshold"],
    }
    for reasons in blocked.values():
        assert execution_reprice_permitted(_decision(rejection_reasons=reasons)) is False

    assert execution_reprice_permitted(_decision(canonical_market_id=None)) is False
    unmatched = _decision(
        market_match=MarketMatchResult(matched=False, confidence=0.0, reasons=["no"])
    )
    assert execution_reprice_permitted(unmatched) is False
    assert (
        execution_reprice_permitted(
            _decision(
                payoff_scan=PayoffScanResult(
                    solution=PayoffSolution(is_arbitrage=False, roi=Decimal("0"))
                )
            )
        )
        is False
    )
    assert (
        execution_reprice_permitted(
            _decision(
                payoff_scan=PayoffScanResult(
                    solution=PayoffSolution(
                        is_arbitrage=True,
                        roi=Decimal("0.006"),
                        minimum_state_pnl=Decimal("0.01"),
                        numerically_validated=True,
                    )
                )
            )
        )
        is False
    )


def test_execution_entry_block_fail_closes_stale_and_lost_edge() -> None:
    assert execution_entry_block(_decision()) == EXECUTION_REPRICE_STALE
    assert (
        execution_entry_block(_decision(rejection_reasons=["unknown_quote_age"]))
        == EXECUTION_REPRICE_STALE
    )
    assert (
        execution_entry_block(
            _decision(eligible_for_paper_simulation=False, rejection_reasons=["no_positive_edge"])
        )
        == EXECUTION_REPRICE_NO_LONGER_QUALIFYING
    )
    assert (
        execution_entry_block(
            _decision(
                eligible_for_paper_simulation=True,
                rejection_reasons=[],
                payoff_scan=PayoffScanResult(
                    solution=PayoffSolution(
                        is_arbitrage=True,
                        roi=Decimal("0.006"),
                        minimum_state_pnl=Decimal("0.01"),
                        numerically_validated=True,
                    )
                ),
            )
        )
        == EXECUTION_REPRICE_NO_LONGER_QUALIFYING
    )
    assert (
        execution_entry_block(
            _decision(eligible_for_paper_simulation=True, rejection_reasons=[])
        )
        is None
    )


def test_reprice_source_does_not_discover_or_place_orders() -> None:
    source = "\n".join(
        inspect.getsource(item)
        for item in (
            CataloguePriceEngine.reprice_for_paper_entry,
            CataloguePriceEngine._reprice_exact_books,
            CataloguePriceEngine._execution_fetch_venues,
            CataloguePriceEngine._execution_fetch_matchbook,
            CataloguePriceEngine._execution_fetch_kalshi,
            CataloguePriceEngine._execution_fetch_polymarket,
        )
    )
    assert ".list_events(" not in source
    assert ".list_markets(" not in source
    assert "place_order" not in source
    assert "submit_order" not in source
    for client in (MatchbookClient, KalshiClient, PolymarketClient):
        assert client.capabilities.execution_enabled is False
        for method in FORBIDDEN_WRITE_METHODS:
            assert not hasattr(client, method)
    assert Settings().sports_hedge_execution_enabled is False


class _RecordingScan(PaperScanService):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.seen: list[PaperScanDecision] = []

    def scan_pair(self, left, right, **kwargs):
        decision = super().scan_pair(left, right, **kwargs)
        self.seen.append(decision)
        return decision


class _ScriptMatchbook(FakeMatchbook):
    def __init__(self, payloads: list[dict | None]) -> None:
        super().__init__()
        self._payloads = payloads

    async def get_market(self, event_id: int | str, market_id: int | str, **filters: object):
        del filters
        self.get_market_calls.append((str(event_id), str(market_id)))
        index = len(self.get_market_calls) - 1
        if index >= len(self._payloads):
            raise MatchbookMarketGoneError(event_id, market_id, 404)
        payload = self._payloads[index]
        if payload is None:
            raise MatchbookMarketGoneError(event_id, market_id, 404)
        hook = getattr(self, "on_index", None)
        if hook is not None:
            await hook(index)
        return payload


class _ScriptKalshi(FakeKalshi):
    def __init__(self, books: list[dict | None]) -> None:
        super().__init__()
        self._books = books

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: object,
    ):
        del event_id, outcome_id, filters
        ticker = str(market_id)
        self.book_calls.append(ticker)
        index = len(self.book_calls) - 1
        if index >= len(self._books):
            return None
        book = self._books[index]
        if book is None:
            return None
        return book


def _stamp(market: dict, at) -> dict:
    stamped = deepcopy(market)
    text = at.isoformat()
    for runner in stamped["runners"]:
        runner["last-updated"] = text
        for price in runner.get("prices") or []:
            if isinstance(price, dict):
                price["last-updated"] = text
    return stamped


def _market(amount: str = "500", *, odds: str = "2.20") -> dict:
    market = _mb_btts(int(MARKET), odds=odds)
    market["runners"][0]["prices"][0]["available-amount"] = amount
    market["runners"][1]["prices"] = [
        {"side": "back", "odds": "1.80", "available-amount": amount},
        {"side": "lay", "odds": "1.82", "available-amount": amount},
    ]
    return market


def _book(yes: str, no: str, size: str = "500.00") -> dict:
    return {
        "orderbook_fp": {
            "yes_dollars": [[yes, size]],
            "no_dollars": [[no, size]],
        }
    }


def _stale(market: dict) -> dict:
    return _stamp(market, NOW - timedelta(seconds=30))


def _fresh(market: dict) -> dict:
    return _stamp(market, NOW)


def _stakes(decision: PaperScanDecision) -> dict[tuple, Decimal]:
    return {
        (leg.venue, leg.outcome): leg.requested_stake
        for leg in decision.fill_legs
        if leg.requested_stake > 0
    }


def _events(watchlist: WatchlistService, opportunity_id: str):
    return list(reversed(watchlist.activity(opportunity_id=opportunity_id, limit=100)))


def _locks(bundle) -> tuple[Decimal, Decimal]:
    snap = bundle.ledger.treasury.snapshot()
    return (
        snap.pool(VenueName.MATCHBOOK, "GBP").locked_capital,
        snap.pool(VenueName.KALSHI, "USD").locked_capital,
    )


async def _run(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    name: str,
    matchbook_payloads: list[dict | None],
    kalshi_books: list[dict | None],
    treasury: Decimal = Decimal(5000),
    max_allocated_per_trade_gbp: float | None = None,
    max_one_time_gbp: float | None = None,
    max_event_gbp: float | None = None,
    max_opportunity_gbp: float | None = None,
    extra_settings: dict | None = None,
    ready: dict | None = None,
    matchbook_hook=None,
):
    settings_kwargs: dict[str, object] = {"paper_autofill_enabled": True}
    if max_allocated_per_trade_gbp is not None:
        settings_kwargs["max_allocated_per_trade_gbp"] = max_allocated_per_trade_gbp
        settings_kwargs["allocation_per_opportunity_limit_gbp"] = max_allocated_per_trade_gbp
    if max_event_gbp is not None:
        settings_kwargs["max_event_gbp"] = max_event_gbp
    if max_opportunity_gbp is not None:
        settings_kwargs["max_opportunity_gbp"] = max_opportunity_gbp
    if max_one_time_gbp is not None:
        settings_kwargs["max_one_time_gbp"] = max_one_time_gbp
    if extra_settings:
        settings_kwargs.update(extra_settings)
    settings = Settings(**settings_kwargs)
    repository = SqliteMarketIntelligenceRepository()
    scan = _RecordingScan(
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
        seed_gbp=treasury,
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

    def operations_factory(watchlist_arg=None, alerts=None):
        del alerts
        if watchlist_arg is not None:
            operations.watchlist = watchlist_arg
        operations.settings = settings
        return operations

    monkeypatch.setattr(paper_api, "get_paper_operations_service", operations_factory)
    row = _row(
        suffix="exec",
        matchbook_event_id=EVENT,
        matchbook_market_id=MARKET,
        kalshi_event="KXEPLBTTS-RICH",
    )
    matchbook = _ScriptMatchbook(matchbook_payloads)
    matchbook.on_index = matchbook_hook
    kalshi = _ScriptKalshi(kalshi_books)
    engine, _, _, _layer = _engine(
        [row],
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
    if ready is not None:
        ready.update(
            engine=engine,
            watchlist=watchlist,
            operations=operations,
            row=row,
            scan=scan,
        )
    result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    await engine.drain_item_captures()
    return SimpleNamespace(
        engine=engine,
        scan=scan,
        matchbook=matchbook,
        kalshi=kalshi,
        operations=operations,
        watchlist=watchlist,
        ledger=ledger,
        repository=repository,
        result=result,
        settings=settings,
        row=row,
    )


def _close(bundle) -> None:
    bundle.repository.close()
    bundle.ledger.close()


def _opportunity(bundle) -> str:
    rows = bundle.watchlist.repository.list_opportunities()
    assert len(rows) == 1
    return rows[0].opportunity_id


def _open_trade(bundle):
    trades = [
        trade
        for trade in bundle.operations.list_active_trades()
        if trade.state is PaperTradeState.OPEN
    ]
    assert len(trades) == 1
    return trades[0]


@pytest.mark.asyncio
async def test_stale_discovery_fresh_execution_opens_paper_trade(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rich = _market()
    book = _book("0.20", "0.70")
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="stale-fresh",
        matchbook_payloads=[_stale(rich), _fresh(rich)],
        kalshi_books=[book, book],
    )
    try:
        assert len(bundle.scan.seen) == 2
        discovery, execution = bundle.scan.seen
        assert discovery.eligible_for_paper_simulation is False
        assert "stale_quote" in discovery.rejection_reasons
        assert execution.eligible_for_paper_simulation is True
        assert "stale_quote" not in execution.rejection_reasons
        assert bundle.matchbook.list_events_calls == 0
        assert bundle.kalshi.list_events_calls == 0
        assert bundle.matchbook.list_markets_calls == []
        assert bundle.kalshi.list_markets_calls == []
        assert len(bundle.matchbook.get_market_calls) == 3
        assert bundle.kalshi.book_calls == [TICKER, TICKER, TICKER]
        assert bundle.result.decisions == [discovery]

        trade = _open_trade(bundle)
        assert trade.paper_only is True
        assert trade.places_orders is False
        assert trade.canonical_market_id == execution.canonical_market_id
        assert trade.entry_risk is not None
        assert trade.entry_risk.net_edge == decision_net_edge(execution)
        assert _locks(bundle)[0] > 0
        events = _events(bundle.watchlist, trade.opportunity_id)
        types = [event.event_type for event in events]
        assert [item for item in types if item in ENTRY_ORDER] == list(ENTRY_ORDER)
        paper_eligible = next(
            event for event in events if event.event_type is LifecycleEventType.PAPER_ELIGIBLE
        )
        qualifying = next(
            event for event in events if event.event_type is LifecycleEventType.QUALIFYING_DETECTED
        )
        assert qualifying.current_net_edge == decision_net_edge(discovery)
        assert paper_eligible.current_net_edge == decision_net_edge(execution)
        assert paper_eligible.capture_eligible is True
        assert qualifying.quote_age_ms is not None and qualifying.quote_age_ms >= 2000
        assert execution.quote_age_ms is not None and execution.quote_age_ms < 2000
        assert bundle.settings.sports_hedge_execution_enabled is False
    finally:
        _close(bundle)


@pytest.mark.asyncio
async def test_execution_reprice_below_minimum_does_not_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="edge-gone",
        matchbook_payloads=[_stale(_market()), _fresh(_market())],
        kalshi_books=[_book("0.20", "0.70"), _book("0.40", "0.49")],
    )
    try:
        discovery, execution = bundle.scan.seen
        assert discovery.eligible_for_paper_simulation is False
        assert "stale_quote" in discovery.rejection_reasons
        assert execution.eligible_for_paper_simulation is False
        assert decision_net_edge(execution) < execution.minimum_net_edge or (
            not execution.eligible_for_paper_simulation
        )
        assert bundle.operations.list_active_trades() == []
        assert _locks(bundle) == (Decimal(0), Decimal(0))
        assert len(bundle.matchbook.get_market_calls) == 2
        assert len(bundle.kalshi.book_calls) == 2
        opportunity_id = _opportunity(bundle)
        events = _events(bundle.watchlist, opportunity_id)
        types = [event.event_type for event in events]
        assert LifecycleEventType.QUALIFYING_DETECTED in types
        assert LifecycleEventType.PAPER_ELIGIBLE not in types
        assert LifecycleEventType.PAPER_FILL_COMPLETE not in types
        assert LifecycleEventType.QUALIFYING_LOST in types or (
            LifecycleEventType.TRIGGER_LOST_BEFORE_FILL in types
        )
        missed = [
            event
            for event in events
            if event.event_type is LifecycleEventType.PAPER_FILL_REJECTED
            and str(event.detail).startswith(EXECUTION_REPRICE_NO_LONGER_QUALIFYING)
        ]
        assert missed
        lost = next(
            event
            for event in events
            if event.event_type
            in {LifecycleEventType.QUALIFYING_LOST, LifecycleEventType.TRIGGER_LOST_BEFORE_FILL}
        )
        assert lost.current_net_edge == decision_net_edge(execution)
        assert lost.current_net_edge != decision_net_edge(discovery)
    finally:
        _close(bundle)


@pytest.mark.asyncio
async def test_execution_reprice_provider_failure_does_not_fill_or_retry(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="provider-fail",
        matchbook_payloads=[_stale(_market()), _fresh(_market())],
        kalshi_books=[_book("0.20", "0.70"), None],
    )
    try:
        assert len(bundle.scan.seen) == 1
        assert bundle.scan.seen[0].eligible_for_paper_simulation is False
        assert bundle.operations.list_active_trades() == []
        assert _locks(bundle) == (Decimal(0), Decimal(0))
        assert len(bundle.matchbook.get_market_calls) == 2
        assert bundle.kalshi.book_calls == [TICKER, TICKER]
        opportunity_id = _opportunity(bundle)
        missed = [
            event
            for event in _events(bundle.watchlist, opportunity_id)
            if event.event_type is LifecycleEventType.PAPER_FILL_REJECTED
        ]
        assert [event.detail for event in missed]
        assert all(
            str(event.detail).startswith(EXECUTION_REPRICE_FAILED) for event in missed
        )
        watched = bundle.watchlist.repository.get(opportunity_id)
        assert watched is not None
        assert watched.status is OpportunityStatus.TRIGGERED
        assert LifecycleEventType.PAPER_ELIGIBLE not in [
            event.event_type for event in _events(bundle.watchlist, opportunity_id)
        ]
    finally:
        _close(bundle)


@pytest.mark.asyncio
async def test_still_stale_execution_books_do_not_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rich = _market()
    book = _book("0.20", "0.70")
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="still-stale",
        matchbook_payloads=[_stale(rich), _stale(rich)],
        kalshi_books=[book, book],
    )
    try:
        assert len(bundle.scan.seen) == 2
        discovery, execution = bundle.scan.seen
        assert "stale_quote" in discovery.rejection_reasons
        assert discovery.eligible_for_paper_simulation is False
        assert "stale_quote" in execution.rejection_reasons
        assert execution.eligible_for_paper_simulation is False
        assert bundle.operations.list_active_trades() == []
        assert _locks(bundle) == (Decimal(0), Decimal(0))
        assert len(bundle.kalshi.book_calls) == 2
        opportunity_id = _opportunity(bundle)
        events = _events(bundle.watchlist, opportunity_id)
        rejected = [
            event
            for event in events
            if event.event_type is LifecycleEventType.PAPER_FILL_REJECTED
        ]
        assert len(rejected) == 1
        assert str(rejected[0].detail).startswith(EXECUTION_REPRICE_STALE)
        assert "matchbook.quote_age_ms=" in str(rejected[0].detail)
        assert "kalshi.quote_age_ms=" in str(rejected[0].detail)
        assert LifecycleEventType.PAPER_ELIGIBLE not in [event.event_type for event in events]
        assert LifecycleEventType.PAPER_FILL_ATTEMPTED not in [
            event.event_type for event in events
        ]
        watched = bundle.watchlist.repository.get(opportunity_id)
        assert watched is not None
        assert watched.status is OpportunityStatus.TRIGGERED
    finally:
        _close(bundle)


@pytest.mark.asyncio
async def test_fresh_discovery_still_reprices_once_before_fill(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rich = _market()
    book = _book("0.20", "0.70")
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="fresh-discovery",
        matchbook_payloads=[_fresh(rich), _fresh(rich)],
        kalshi_books=[book, book],
    )
    try:
        assert len(bundle.scan.seen) == 2
        discovery, execution = bundle.scan.seen
        assert discovery.eligible_for_paper_simulation is True
        assert execution.eligible_for_paper_simulation is True
        assert len(bundle.matchbook.get_market_calls) == 3
        assert len(bundle.kalshi.book_calls) == 3
        trade = _open_trade(bundle)
        types = [event.event_type for event in _events(bundle.watchlist, trade.opportunity_id)]
        assert [item for item in types if item in ENTRY_ORDER] == list(ENTRY_ORDER)
        assert types.index(LifecycleEventType.QUALIFYING_DETECTED) < types.index(
            LifecycleEventType.PAPER_ELIGIBLE
        )
    finally:
        _close(bundle)


@pytest.mark.asyncio
async def test_execution_depth_replaces_discovery_stakes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="resized",
        matchbook_payloads=[_stale(_market("500")), _fresh(_market("220"))],
        kalshi_books=[_book("0.20", "0.70", "500.00"), _book("0.20", "0.70", "220.00")],
    )
    try:
        discovery, execution = bundle.scan.seen
        assert _stakes(discovery) != _stakes(execution)
        trade = _open_trade(bundle)
        traded = {
            (leg.venue, leg.outcome): leg.requested_stake
            for leg in trade.legs
            if leg.requested_stake > 0
        }
        assert traded == _stakes(execution)
        assert traded != _stakes(discovery)
        assert trade.guaranteed_profit_gbp_at_open == execution.allocation.guaranteed_profit
        assert trade.guaranteed_profit_gbp_at_open != discovery.allocation.guaranteed_profit
    finally:
        _close(bundle)


@pytest.mark.asyncio
async def test_execution_edge_replaces_discovery_edge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="edge-change",
        matchbook_payloads=[_fresh(_market()), _fresh(_market())],
        kalshi_books=[_book("0.20", "0.70"), _book("0.32", "0.60")],
    )
    try:
        discovery, execution = bundle.scan.seen
        discovery_edge = decision_net_edge(discovery)
        execution_edge = decision_net_edge(execution)
        assert discovery.eligible_for_paper_simulation is True
        assert execution.eligible_for_paper_simulation is True
        assert discovery_edge > execution_edge >= execution.minimum_net_edge
        trade = _open_trade(bundle)
        assert trade.entry_risk is not None
        assert trade.entry_risk.net_edge == execution_edge
        assert trade.entry_risk.net_edge != discovery_edge
        paper_eligible = next(
            event
            for event in _events(bundle.watchlist, trade.opportunity_id)
            if event.event_type is LifecycleEventType.PAPER_ELIGIBLE
        )
        qualifying = next(
            event
            for event in _events(bundle.watchlist, trade.opportunity_id)
            if event.event_type is LifecycleEventType.QUALIFYING_DETECTED
        )
        assert qualifying.current_net_edge == discovery_edge
        assert paper_eligible.current_net_edge == execution_edge
        assert paper_eligible.capture_eligible is True
    finally:
        _close(bundle)


@pytest.mark.asyncio
async def test_exactly_one_complete_set_reprice(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    rich = _market()
    book = _book("0.20", "0.70")
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="once",
        matchbook_payloads=[_stale(rich), _fresh(rich)],
        kalshi_books=[book, book],
    )
    try:
        assert len(bundle.scan.seen) == 2
        assert len(bundle.matchbook.get_market_calls) == 3
        assert len(bundle.kalshi.book_calls) == 3
        assert bundle.engine._execution_reprice_open == set()
        trade = _open_trade(bundle)
        assert len(trade.legs) > 0
        assert len(trade.tranches) == 1
    finally:
        _close(bundle)


@pytest.mark.asyncio
async def test_missing_native_id_does_not_fetch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="missing-id",
        matchbook_payloads=[_fresh(_market())],
        kalshi_books=[_book("0.40", "0.49")],
    )
    try:
        runtime = bundle.engine.item("amc-exec")
        assert runtime is not None
        before_mb = len(bundle.matchbook.get_market_calls)
        before_k = len(bundle.kalshi.book_calls)
        runtime.identity = runtime.identity.model_copy(update={"kalshi_event_ticker": ""})
        refreshed = await bundle.engine.reprice_for_paper_entry(
            runtime,
            venues=(VenueName.MATCHBOOK, VenueName.KALSHI),
        )
        assert refreshed.decision is None
        assert refreshed.reason == EXECUTION_REPRICE_FAILED
        assert len(bundle.matchbook.get_market_calls) == before_mb
        assert len(bundle.kalshi.book_calls) == before_k
    finally:
        _close(bundle)
