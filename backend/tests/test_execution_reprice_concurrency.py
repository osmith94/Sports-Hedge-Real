"""Price-2 execution reads are one contemporaneous complete set.

Fixture/demo providers only. Caps stay in force. No live orders.
"""

from __future__ import annotations

import asyncio
import time
from datetime import timedelta
from decimal import Decimal

import pytest
from test_execution_reprice_before_paper_entry import (
    EVENT,
    MARKET,
    TICKER,
    _book,
    _fresh,
    _market,
    _run,
    _stamp,
)
from test_issue316_catalogue_registry import _costs, _fx
from test_issue344_price_engine import (
    NOW,
    FakeKalshi,
    FakeMatchbook,
    _engine,
    _hda_row,
    _row,
)

from sports_hedge.application.adaptive_scheduler import SchedulerWork, rank_scheduler_work
from sports_hedge.application.approved_market_catalogue import OutcomeNativeId
from sports_hedge.application.execution_reprice import (
    EXECUTION_REPRICE_FAILED,
    EXECUTION_REPRICE_STALE,
    execution_entry_block,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.provider_access import ProviderAccessLayer
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.persistence.liquidity import SqlitePaperLiquidityRepository
from sports_hedge.venues import KalshiClient, MatchbookClient, PolymarketClient


class OverlappingClock:
    """Advance by the longest in-flight delay, not the sum of sequential calls."""

    def __init__(self) -> None:
        self.current = NOW
        self._inflight = 0
        self._batch_ms = 0
        self._release = asyncio.Event()

    def now(self):
        return self.current

    async def io(self, delay_ms: int) -> None:
        self._inflight += 1
        self._batch_ms = max(self._batch_ms, delay_ms)
        release = self._release
        await asyncio.sleep(0)
        self._inflight -= 1
        if self._inflight == 0:
            advance = self._batch_ms
            self._batch_ms = 0
            self.current += timedelta(milliseconds=advance)
            self._release = asyncio.Event()
            release.set()
        else:
            await release.wait()


def _overlaps(spans: list[tuple[str, str, float]]) -> bool:
    starts = [stamp for _name, phase, stamp in spans if phase == "start"]
    ends = [stamp for _name, phase, stamp in spans if phase == "end"]
    return bool(starts and ends) and max(starts) < min(ends)


async def _gate(name: str, started: list[str], release: asyncio.Event, expected: int) -> None:
    started.append(name)
    if len(started) >= expected:
        release.set()
    await asyncio.wait_for(release.wait(), timeout=1)


def test_execution_work_outranks_ordinary_hot_and_background_but_not_active_trades() -> None:
    engine, _mb, _ks, _layer = _engine([_row(suffix="rank")])
    runtime = engine.item("amc-rank")
    assert runtime is not None
    execution = engine._execution_scheduler_work(
        runtime,
        (VenueName.MATCHBOOK, VenueName.KALSHI),
    )
    ordinary = SchedulerWork(
        lane="hot",
        viable_venue_count=2,
        viability_assessed=True,
        now_mono=execution.now_mono,
    )
    background = SchedulerWork(lane="background", now_mono=execution.now_mono)
    active = SchedulerWork(lane="active_trade", now_mono=execution.now_mono)
    execution_rank = rank_scheduler_work(execution)
    assert execution.qualifying is True
    assert execution_rank.value_class.value == "hot_viable_near"
    assert execution_rank.band < rank_scheduler_work(ordinary).band
    assert execution_rank.band < rank_scheduler_work(background).band
    assert execution_rank.band > rank_scheduler_work(active).band
    assert Settings().sports_hedge_execution_enabled is False
    for client in (MatchbookClient, KalshiClient, PolymarketClient):
        assert client.capabilities.execution_enabled is False


@pytest.mark.asyncio
async def test_execution_reprice_is_granted_ahead_of_a_waiting_background_call() -> None:
    access = ProviderAccessLayer(
        {VenueName.MATCHBOOK: 1, VenueName.KALSHI: 1, VenueName.POLYMARKET: 1}
    )
    release_holder = asyncio.Event()
    holder_in = asyncio.Event()

    async def _hold() -> None:
        async with access.acquire_wait(
            VenueName.KALSHI,
            lane="background",
            stage="order_book",
            timeout=2,
        ) as lease:
            assert lease is not None
            holder_in.set()
            await release_holder.wait()

    holder = asyncio.create_task(_hold())
    await holder_in.wait()
    background_granted = asyncio.Event()
    execution_granted = asyncio.Event()

    async def _background() -> None:
        async with access.acquire_wait(
            VenueName.KALSHI,
            lane="background",
            stage="order_book",
            timeout=2,
        ) as lease:
            if lease is not None:
                background_granted.set()

    async def _execution() -> None:
        now_mono = access._clock()
        work = SchedulerWork(
            lane="hot",
            qualifying=True,
            near_threshold=True,
            viable_venue_count=2,
            viability_assessed=True,
            required_venues=(VenueName.KALSHI,),
            due_mono=now_mono,
            deadline_mono=now_mono + 2,
            now_mono=now_mono,
        )
        async with access.acquire_wait(
            VenueName.KALSHI,
            lane="hot",
            stage="order_book",
            timeout=2,
            work=work,
        ) as lease:
            if lease is not None:
                execution_granted.set()

    background = asyncio.create_task(_background())
    await asyncio.sleep(0)
    execution = asyncio.create_task(_execution())
    await asyncio.sleep(0)
    release_holder.set()
    await asyncio.wait_for(execution_granted.wait(), timeout=1)
    assert background_granted.is_set() is False
    await execution
    await asyncio.wait_for(background_granted.wait(), timeout=1)
    await holder
    await background


@pytest.mark.asyncio
async def test_concurrent_price2_stays_fresh_where_serial_assembly_would_be_stale(
    tmp_path,
) -> None:
    clock = OverlappingClock()
    delay_ms = 1200
    spans: list[tuple[str, str, float]] = []
    release = asyncio.Event()
    started: list[str] = []
    payload = _market()
    book = _book("0.20", "0.70")

    class _Matchbook(FakeMatchbook):
        async def get_market(self, event_id, market_id, **filters):
            del filters
            self.get_market_calls.append((str(event_id), str(market_id)))
            spans.append(("matchbook", "start", time.monotonic()))
            stamped = _stamp(payload, clock.now())
            await _gate("matchbook", started, release, 2)
            await clock.io(delay_ms)
            spans.append(("matchbook", "end", time.monotonic()))
            return stamped

    class _Kalshi(FakeKalshi):
        async def get_order_book(self, event_id, market_id, outcome_id=None, **filters):
            del event_id, outcome_id, filters
            self.book_calls.append(str(market_id))
            spans.append((str(market_id), "start", time.monotonic()))
            await _gate(str(market_id), started, release, 2)
            await clock.io(delay_ms)
            spans.append((str(market_id), "end", time.monotonic()))
            return book

    settings = Settings(paper_autofill_enabled=True)
    scan = PaperScanService(
        MarketIntelligenceService(SqliteMarketIntelligenceRepository()),
        settings=settings,
        liquidity=SqlitePaperLiquidityRepository(
            tmp_path / "fresh-liquidity.sqlite",
            matchbook_gbp=Decimal(5000),
            polymarket_usd=Decimal(5000),
            kalshi_usd=Decimal(5000),
        ),
    )
    row = _row(
        suffix="fresh",
        matchbook_event_id=EVENT,
        matchbook_market_id=MARKET,
        kalshi_event="KXEPLBTTS-RICH",
    )
    engine, matchbook, kalshi, layer = _engine(
        [row],
        matchbook=_Matchbook(),
        kalshi=_Kalshi(),
        paper_scan=scan,
        clock=clock.now,
    )
    engine.venue_costs = _costs()
    engine.fx_snapshots = _fx()
    engine._slice_remaining = lambda: 0
    runtime = engine.item(row.catalogue_row_id)
    assert runtime is not None
    result = await engine.reprice_for_paper_entry(
        runtime,
        venues=(VenueName.MATCHBOOK, VenueName.KALSHI),
    )
    assert _overlaps(spans)
    assert clock.current - NOW == timedelta(milliseconds=delay_ms)
    assert delay_ms * 2 > 2000
    assert result.decision is not None
    assert result.decision.eligible_for_paper_simulation is True
    assert result.decision.quote_age_ms is not None
    assert result.decision.quote_age_ms < 2000
    assert execution_entry_block(result.decision) is None
    assert result.diagnostics is not None
    assert result.diagnostics.assembly_ms >= 0
    assert "started_at=" in result.diagnostics.compact()
    assert result.diagnostics.oldest_quote_age_ms() == result.decision.quote_age_ms
    assert matchbook.get_market_calls == [(EVENT, MARKET)]
    assert kalshi.book_calls == [TICKER]
    assert matchbook.list_events_calls == 0
    assert kalshi.list_events_calls == 0
    assert layer.limits[VenueName.MATCHBOOK] == 4
    assert layer.limits[VenueName.KALSHI] == 4
    assert layer._peak_inflight[VenueName.MATCHBOOK] <= 4
    assert layer._peak_inflight[VenueName.KALSHI] <= 4


@pytest.mark.asyncio
async def test_kalshi_complete_set_tickers_overlap() -> None:
    spans: list[tuple[str, str, float]] = []
    release = asyncio.Event()
    started: list[str] = []
    row = _hda_row("hda1")

    class _Kalshi(FakeKalshi):
        async def get_order_book(self, event_id, market_id, outcome_id=None, **filters):
            del event_id, outcome_id, filters
            ticker = str(market_id)
            self.book_calls.append(ticker)
            spans.append((ticker, "start", time.monotonic()))
            await _gate(ticker, started, release, 3)
            spans.append((ticker, "end", time.monotonic()))
            return _book("0.20", "0.70")

    engine, _mb, kalshi, _layer = _engine([row], kalshi=_Kalshi())
    runtime = engine.item(row.catalogue_row_id)
    assert runtime is not None
    work = engine._execution_scheduler_work(
        runtime,
        (VenueName.MATCHBOOK, VenueName.KALSHI),
    )
    books = await engine._execution_fetch_kalshi(
        runtime.identity,
        lane="hot",
        runtime=runtime,
        scheduler_work=work,
    )
    assert books is not None
    assert set(books) == set(kalshi.book_calls)
    assert len(kalshi.book_calls) == 3
    assert _overlaps(spans)


@pytest.mark.asyncio
async def test_polymarket_token_books_overlap() -> None:
    spans: list[tuple[str, str, float]] = []
    release = asyncio.Event()
    started: list[str] = []
    tokens = [
        OutcomeNativeId(outcome="home", native_id="713856789012345678901"),
        OutcomeNativeId(outcome="draw", native_id="713856789012345678902"),
        OutcomeNativeId(outcome="away", native_id="713856789012345678903"),
    ]
    row = _hda_row("pm1").model_copy(
        update={
            "polymarket_event_id": "1016065",
            "polymarket_market_id": "4521504",
            "polymarket_token_ids": tokens,
        }
    )

    class _Polymarket:
        def __init__(self) -> None:
            self.calls: list[str] = []

        async def get_order_book(self, event_id, market_id, token_id, **filters):
            del event_id, market_id, filters
            token = str(token_id)
            self.calls.append(token)
            spans.append((token, "start", time.monotonic()))
            await _gate(token, started, release, 3)
            spans.append((token, "end", time.monotonic()))
            return {"bids": [], "asks": []}

    client = _Polymarket()
    engine, _mb, _ks, _layer = _engine([row])
    engine.polymarket = client
    runtime = engine.item(row.catalogue_row_id)
    assert runtime is not None
    work = engine._execution_scheduler_work(
        runtime,
        (VenueName.MATCHBOOK, VenueName.POLYMARKET),
    )
    books = await engine._execution_fetch_polymarket(
        runtime.identity,
        lane="hot",
        runtime=runtime,
        scheduler_work=work,
    )
    assert books is not None
    assert client.calls == [item.native_id for item in tokens]
    assert _overlaps(spans)


@pytest.mark.asyncio
async def test_kalshi_cap_still_serialises_complete_set_reads() -> None:
    inflight = 0
    peak = 0
    row = _hda_row("cap1")

    class _Kalshi(FakeKalshi):
        async def get_order_book(self, event_id, market_id, outcome_id=None, **filters):
            nonlocal inflight, peak
            del event_id, outcome_id, filters
            self.book_calls.append(str(market_id))
            inflight += 1
            peak = max(peak, inflight)
            await asyncio.sleep(0.02)
            inflight -= 1
            return _book("0.20", "0.70")

    access = ProviderAccessLayer(
        {VenueName.MATCHBOOK: 4, VenueName.KALSHI: 1, VenueName.POLYMARKET: 1}
    )
    engine, _mb, kalshi, layer = _engine([row], kalshi=_Kalshi(), access=access)
    runtime = engine.item(row.catalogue_row_id)
    assert runtime is not None
    work = engine._execution_scheduler_work(
        runtime,
        (VenueName.MATCHBOOK, VenueName.KALSHI),
    )
    books = await engine._execution_fetch_kalshi(
        runtime.identity,
        lane="hot",
        runtime=runtime,
        scheduler_work=work,
    )
    assert books is not None
    assert len(kalshi.book_calls) == 3
    assert peak == 1
    assert layer._peak_inflight[VenueName.KALSHI] == 1
    assert layer.limits[VenueName.KALSHI] == 1


@pytest.mark.asyncio
async def test_still_stale_execution_books_fail_closed_when_fetched_together(tmp_path) -> None:
    clock = OverlappingClock()
    release = asyncio.Event()
    started: list[str] = []

    class _Matchbook(FakeMatchbook):
        async def get_market(self, event_id, market_id, **filters):
            del filters
            self.get_market_calls.append((str(event_id), str(market_id)))
            await _gate("matchbook", started, release, 2)
            await clock.io(100)
            return _stamp(_market(), NOW - timedelta(seconds=30))

    class _Kalshi(FakeKalshi):
        async def get_order_book(self, event_id, market_id, outcome_id=None, **filters):
            del event_id, outcome_id, filters
            self.book_calls.append(str(market_id))
            await _gate(str(market_id), started, release, 2)
            await clock.io(100)
            return _book("0.20", "0.70")

    row = _row(
        suffix="stale",
        matchbook_event_id=EVENT,
        matchbook_market_id=MARKET,
        kalshi_event="KXEPLBTTS-RICH",
    )
    settings = Settings()
    scan = PaperScanService(
        MarketIntelligenceService(SqliteMarketIntelligenceRepository()),
        settings=settings,
        liquidity=SqlitePaperLiquidityRepository(
            tmp_path / "stale-liquidity.sqlite",
            matchbook_gbp=Decimal(5000),
            polymarket_usd=Decimal(5000),
            kalshi_usd=Decimal(5000),
        ),
    )
    engine, _mb, _ks, _layer = _engine(
        [row],
        matchbook=_Matchbook(),
        kalshi=_Kalshi(),
        paper_scan=scan,
        clock=clock.now,
    )
    engine.venue_costs = _costs()
    engine.fx_snapshots = _fx()
    runtime = engine.item(row.catalogue_row_id)
    assert runtime is not None
    result = await engine.reprice_for_paper_entry(
        runtime,
        venues=(VenueName.MATCHBOOK, VenueName.KALSHI),
    )
    assert result.decision is not None
    assert execution_entry_block(result.decision) == EXECUTION_REPRICE_STALE
    assert result.diagnostics is not None
    assert result.diagnostics.oldest_quote_age_ms() is not None
    assert result.diagnostics.oldest_quote_age_ms() >= 2000


@pytest.mark.asyncio
async def test_provider_failure_during_concurrent_reprice_does_not_fill() -> None:
    calls: list[str] = []
    row = _hda_row("fail1")

    class _Kalshi(FakeKalshi):
        async def get_order_book(self, event_id, market_id, outcome_id=None, **filters):
            del event_id, outcome_id, filters
            ticker = str(market_id)
            calls.append(ticker)
            self.book_calls.append(ticker)
            if ticker.endswith("DRAW"):
                return None
            return _book("0.20", "0.70")

    engine, matchbook, kalshi, _layer = _engine([row], kalshi=_Kalshi())
    runtime = engine.item(row.catalogue_row_id)
    assert runtime is not None
    result = await engine.reprice_for_paper_entry(
        runtime,
        venues=(VenueName.MATCHBOOK, VenueName.KALSHI),
    )
    assert result.decision is None
    assert result.reason == EXECUTION_REPRICE_FAILED
    assert result.diagnostics is not None
    assert result.diagnostics.reason == EXECUTION_REPRICE_FAILED
    assert len(calls) == 3
    assert len(matchbook.get_market_calls) == 1
    assert len(kalshi.book_calls) == 3


@pytest.mark.asyncio
async def test_fresh_price2_still_opens_exactly_one_paper_trade(tmp_path, monkeypatch) -> None:
    rich = _market()
    book = _book("0.20", "0.70")
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="one-fill",
        matchbook_payloads=[_fresh(rich), _fresh(rich)],
        kalshi_books=[book, book],
    )
    try:
        assert len(bundle.operations.list_active_trades()) == 1
        assert bundle.matchbook.get_market_calls == [(EVENT, MARKET), (EVENT, MARKET)]
        assert bundle.kalshi.book_calls == [TICKER, TICKER]
        assert bundle.matchbook.list_events_calls == 0
        assert Settings().sports_hedge_execution_enabled is False
    finally:
        bundle.repository.close()
        bundle.ledger.close()
