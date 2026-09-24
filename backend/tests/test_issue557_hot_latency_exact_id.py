"""Issue #557 HOT latency-first exact-ID consumer.

Data class: synthetic/fixture providers and barriers. Not live quotes.
PAPER / read-only.

Production HOT ``run_slice`` uses the shared stage scheduler. These tests
show that path keeps HOT lane priority, slice-local coalescing, isolate-
without-retry errors, and ACTIVE TRADE on ``_price_item``. Caps and the 8s
timeout stay put.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta
from typing import Any

import pytest
from test_issue344_price_engine import (
    NOW,
    FakeKalshi,
    FakeMatchbook,
    StubPaperScan,
    _engine,
    _hda_row,
    _kalshi_book,
    _mb_btts,
    _row,
)
from test_issue470_hot_viability_branch_and_bound import (
    _expensive_kalshi_book,
    _named_match_odds,
)
from test_issue554_background_exact_id_planner import (
    _btts_rows,
    _CountingMatchbook,
    _StallKalshi,
)

from sports_hedge.application.exact_id_stage_scheduler import run_staged_exact_id_slice
from sports_hedge.application.hot_latency_exact_id import (
    HOT_LATENCY_CALL_SHAPE,
    run_hot_latency_exact_id_slice,
)
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.price_engine import (
    CataloguePriceEngine,
    PriceEnginePriority,
    PriceEngineSliceResult,
)
from sports_hedge.application.provider_access import (
    DEFAULT_PROVIDER_CONCURRENCY,
    ProviderPriority,
)
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName

pytestmark = pytest.mark.asyncio

NEAR = NOW + timedelta(minutes=20)


def _hot_rows(count: int, *, suffix: str, id_base: int) -> list[Any]:
    rows = _btts_rows(count, suffix=suffix, id_base=id_base)
    return [row.model_copy(update={"kickoff_utc": NEAR}) for row in rows]


def _open_remaining() -> float | None:
    return None


async def _run_hot(engine: CataloguePriceEngine) -> PriceEngineSliceResult:
    engine.reconstruct()
    due = engine.due_items(PriceEnginePriority.HOT, now=NOW)
    result = PriceEngineSliceResult()
    await run_hot_latency_exact_id_slice(
        engine,
        due,
        result,
        remaining=_open_remaining,
    )
    return result


async def test_production_hot_slice_uses_staged_consumer_and_coalesces() -> None:
    first = _row(
        suffix="prod-a",
        kickoff=NEAR,
        matchbook_event_id="8811",
        matchbook_market_id="316081",
        kalshi_event="KXHOTPROD",
    )
    second = _row(
        suffix="prod-b",
        kickoff=NEAR,
        matchbook_event_id="8811",
        matchbook_market_id="316081",
        kalshi_event="KXHOTPROD",
    )
    engine, matchbook, kalshi, _layer = _engine([first, second], hot_interval=0)
    result = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
    assert set(result.evaluated) == {first.catalogue_row_id, second.catalogue_row_id}
    assert len(matchbook.get_market_calls) == 1
    assert len(kalshi.book_calls) == 1
    assert result.coalesced_provider_calls >= 1
    assert result.pricing_call_shape == HOT_LATENCY_CALL_SHAPE
    price_engine_source = inspect.getsource(CataloguePriceEngine.run_slice)
    assert "run_hot_latency_exact_id_slice" in price_engine_source


async def test_hot_consumer_keeps_matchbook_moving_while_kalshi_slots_stall() -> None:
    rows = _hot_rows(16, suffix="hotstall", id_base=1)
    kalshi = _StallKalshi()
    matchbook = _CountingMatchbook(kalshi)
    engine, matchbook, kalshi, _layer = _engine(
        rows,
        matchbook=matchbook,
        kalshi=kalshi,
        timeout=3,
        hot_interval=0,
    )

    async def _release_after_progress() -> int:
        while matchbook.calls_while_kalshi_stalled < 8:
            await asyncio.sleep(0)
        seen = matchbook.calls_while_kalshi_stalled
        kalshi.release.set()
        return seen

    seen, result = await asyncio.wait_for(
        asyncio.gather(_release_after_progress(), _run_hot(engine)),
        timeout=4,
    )
    assert seen >= 8
    assert result.pricing_call_shape == HOT_LATENCY_CALL_SHAPE
    assert set(result.evaluated) == {row.catalogue_row_id for row in rows}
    assert matchbook.list_events_calls == 0
    assert matchbook.list_markets_calls == []
    assert kalshi.list_events_calls == 0
    assert kalshi.list_markets_calls == []


async def test_production_hot_slice_keeps_matchbook_moving_while_kalshi_slots_stall() -> None:
    rows = _hot_rows(16, suffix="seqstall", id_base=50)
    kalshi = _StallKalshi()
    matchbook = _CountingMatchbook(kalshi)
    engine, matchbook, kalshi, _layer = _engine(
        rows,
        matchbook=matchbook,
        kalshi=kalshi,
        timeout=3,
        hot_interval=0,
    )

    async def _release_after_stall() -> int:
        while kalshi.stall_inflight < 4:
            await asyncio.sleep(0)
        for _ in range(40):
            await asyncio.sleep(0)
        seen = matchbook.calls_while_kalshi_stalled
        kalshi.release.set()
        return seen

    seen, result = await asyncio.wait_for(
        asyncio.gather(
            _release_after_stall(),
            engine.run_slice(PriceEnginePriority.HOT, now=NOW),
        ),
        timeout=4,
    )
    assert seen >= 8
    assert set(result.evaluated) == {row.catalogue_row_id for row in rows}
    assert result.pricing_call_shape == HOT_LATENCY_CALL_SHAPE


async def test_hot_consumer_exception_does_not_retry_or_stop_other_rows() -> None:
    good = [
        _row(
            suffix=f"hot-ok-{index}",
            kickoff=NEAR,
            matchbook_market_id=str(620000 + index),
            kalshi_event=f"KXHOTOK{index}",
        )
        for index in range(3)
    ]
    bad = _row(
        suffix="hot-boom",
        kickoff=NEAR,
        matchbook_market_id="620099",
        kalshi_event="KXHOTBOOM",
    )

    class _BoomScan(StubPaperScan):
        def scan_pair(self, *args: Any, **kwargs: Any):
            if kwargs.get("fixture_canonical_event_id") == bad.canonical_event_id:
                raise RuntimeError("row exploded")
            return super().scan_pair(*args, **kwargs)

    engine, _mb, _ks, _layer = _engine(good + [bad], paper_scan=_BoomScan(), hot_interval=0)
    result = await _run_hot(engine)
    assert set(result.evaluated) == {row.catalogue_row_id for row in good}
    assert bad.catalogue_row_id in result.failed
    assert bad.catalogue_row_id not in result.retry_wait
    runtime = engine.item(bad.catalogue_row_id)
    assert runtime is not None
    assert runtime.retry_attempt == 0
    assert runtime.next_retry_at is None
    assert runtime.status.value == "failed"
    assert "row exploded" in (runtime.last_error_detail or "")
    assert engine.item(good[0].catalogue_row_id).retry_attempt == 0


async def test_hot_consumer_coalesces_inside_one_slice_and_refetches_on_the_next() -> None:
    first = _row(
        suffix="hot-dup-a",
        kickoff=NEAR,
        matchbook_event_id="8812",
        matchbook_market_id="316082",
        kalshi_event="KXHOTSHARE2",
    )
    second = _row(
        suffix="hot-dup-b",
        kickoff=NEAR,
        matchbook_event_id="8812",
        matchbook_market_id="316082",
        kalshi_event="KXHOTSHARE2",
    )
    engine, matchbook, kalshi, layer = _engine([first, second], hot_interval=0)
    result = await _run_hot(engine)
    assert set(result.evaluated) == {first.catalogue_row_id, second.catalogue_row_id}
    assert matchbook.get_market_calls == [("8812", "316082")]
    assert kalshi.book_calls == ["KXHOTSHARE2-BTTS"]
    assert result.coalesced_provider_calls >= 1
    assert result.issued_provider_calls == 2
    assert result.provider_stage_calls.get("matchbook:get_market") == 1
    assert result.provider_stage_calls.get("kalshi:order_book") == 1
    assert layer._hot_grants_since_universe[VenueName.MATCHBOOK] > 0
    assert layer._hot_grants_since_universe[VenueName.KALSHI] > 0
    await _run_hot(engine)
    assert len(matchbook.get_market_calls) == 2
    assert len(kalshi.book_calls) == 2


async def test_hot_consumer_still_prunes_impossible_kalshi_books() -> None:
    row = _hda_row("hot-bound", kickoff=NEAR)
    matchbook = FakeMatchbook()
    matchbook.payloads[str(row.matchbook_market_id)] = _named_match_odds(
        int(row.matchbook_market_id), odds="1.01"
    )

    class _PayloadKalshi(FakeKalshi):
        def __init__(self) -> None:
            super().__init__()
            self.payloads: dict[str, dict[str, Any]] = {}

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
            return self.payloads.get(ticker) or _expensive_kalshi_book()

    kalshi = _PayloadKalshi()
    for ticker in row.kalshi_market_tickers:
        kalshi.payloads[ticker] = _expensive_kalshi_book()
    engine, _mb, kalshi, _layer = _engine(
        [row],
        matchbook=matchbook,
        kalshi=kalshi,
        paper_scan=StubPaperScan(),
        hot_interval=0,
    )
    result = await _run_hot(engine)
    assert row.catalogue_row_id in result.skipped
    assert result.skip_reasons[row.catalogue_row_id] == "upper_bound_below_min_net"
    assert 0 < len(kalshi.book_calls) < len(row.kalshi_market_tickers)
    assert result.saved_provider_calls >= 1


async def test_finished_hot_row_publishes_before_a_stalled_sibling() -> None:
    fast = _row(
        suffix="hot-fast",
        kickoff=NEAR,
        matchbook_event_id="8820",
        matchbook_market_id="316200",
        kalshi_event="KXHOTFAST",
    )
    slow = _row(
        suffix="hot-slow",
        kickoff=NEAR,
        matchbook_event_id="8821",
        matchbook_market_id="316201",
        kalshi_event="KXHOTSLOW",
    )
    published = asyncio.Event()

    class _WatchScan(StubPaperScan):
        def scan_pair(self, *args: Any, **kwargs: Any):
            decision = super().scan_pair(*args, **kwargs)
            if kwargs.get("fixture_canonical_event_id") == fast.canonical_event_id:
                published.set()
            return decision

    class _SlowKalshi(FakeKalshi):
        def __init__(self) -> None:
            super().__init__()
            self.release = asyncio.Event()

        async def get_order_book(
            self,
            event_id: int | str,
            market_id: int | str,
            outcome_id: int | str | None = None,
            **filters: Any,
        ) -> dict[str, Any]:
            del outcome_id, filters
            ticker = str(market_id)
            self.book_calls.append(ticker)
            if str(event_id) == slow.kalshi_event_ticker:
                await self.release.wait()
            return _kalshi_book()

    kalshi = _SlowKalshi()
    engine, _mb, kalshi, _layer = _engine(
        [slow, fast],
        kalshi=kalshi,
        paper_scan=_WatchScan(),
        hot_interval=0,
        timeout=3,
    )

    async def _release_after_publish() -> None:
        await published.wait()
        while not any(ticker.startswith("KXHOTSLOW") for ticker in kalshi.book_calls):
            await asyncio.sleep(0)
        assert not kalshi.release.is_set()
        kalshi.release.set()

    _released, result = await asyncio.wait_for(
        asyncio.gather(_release_after_publish(), _run_hot(engine)),
        timeout=4,
    )
    assert fast.catalogue_row_id in result.evaluated
    assert slow.catalogue_row_id in result.evaluated
    assert len(result.decisions) == 2


async def test_hot_consumer_is_granted_ahead_of_a_queued_background_read() -> None:
    class _HangMatchbook(FakeMatchbook):
        def __init__(self) -> None:
            super().__init__()
            self.entered = 0
            self.four = asyncio.Event()
            self.release_one = asyncio.Event()
            self.release_rest = asyncio.Event()
            self.hot_entered = asyncio.Event()
            self.hot_release = asyncio.Event()
            self.hot_market_id = ""

        async def get_market(
            self,
            event_id: int | str,
            market_id: int | str,
            **filters: Any,
        ) -> dict[str, Any]:
            del filters
            market = str(market_id)
            self.get_market_calls.append((str(event_id), market))
            if market == self.hot_market_id:
                self.hot_entered.set()
                await self.hot_release.wait()
                return _mb_btts(int(market))
            self.entered += 1
            ordinal = self.entered
            if ordinal == 4:
                self.four.set()
            if ordinal == 1:
                await self.release_one.wait()
            else:
                await self.release_rest.wait()
            return _mb_btts(int(market))

    background_rows = _btts_rows(6, suffix="bgwait", id_base=700)
    hot_row = _row(
        suffix="hot-priority",
        kickoff=NEAR,
        matchbook_event_id="8900",
        matchbook_market_id="316900",
        kalshi_event="KXHOTPRIO",
    )
    matchbook = _HangMatchbook()
    matchbook.hot_market_id = str(hot_row.matchbook_market_id)
    engine, matchbook, _kalshi, layer = _engine(
        [*background_rows, hot_row],
        matchbook=matchbook,
        timeout=3,
        hot_interval=0,
    )
    background_entered = asyncio.Event()

    async def _queued_background() -> None:
        async with layer.acquire_wait(
            VenueName.MATCHBOOK,
            lane="background",
            stage="get_market",
            timeout=3,
        ) as lease:
            assert lease is not None
            background_entered.set()
            await matchbook.release_rest.wait()

    async def _pressure() -> None:
        await matchbook.four.wait()
        engine.reconstruct()
        hot_due = engine.due_items(PriceEnginePriority.HOT, now=NOW)
        assert [item.identity.catalogue_row_id for item in hot_due] == [hot_row.catalogue_row_id]
        hot_result = PriceEngineSliceResult()
        hot_task = asyncio.create_task(
            run_hot_latency_exact_id_slice(
                engine,
                hot_due,
                hot_result,
                remaining=_open_remaining,
            )
        )
        background_task = asyncio.create_task(_queued_background())
        for _ in range(100):
            waiting = layer.snapshot().waiting_by_lane
            hot_waiting = waiting["hot"][VenueName.MATCHBOOK.value]
            background_waiting = waiting["background"][VenueName.MATCHBOOK.value]
            if hot_waiting >= 1 and background_waiting >= 1:
                break
            await asyncio.sleep(0)
        else:
            raise AssertionError("HOT and BACKGROUND did not both queue for Matchbook")
        matchbook.release_one.set()
        await matchbook.hot_entered.wait()
        assert not background_entered.is_set()
        hot_index = next(
            index
            for index, (_event_id, market_id) in enumerate(matchbook.get_market_calls)
            if market_id == matchbook.hot_market_id
        )
        assert hot_index == 4
        matchbook.hot_release.set()
        matchbook.release_rest.set()
        await hot_task
        await background_task
        assert hot_result.evaluated == [hot_row.catalogue_row_id]

    await asyncio.wait_for(
        asyncio.gather(
            _pressure(),
            engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW),
        ),
        timeout=4,
    )
    assert ProviderPriority.ACTIVE_TRADE < ProviderPriority.HOT < ProviderPriority.BACKGROUND


async def test_hot_consumer_keeps_provider_caps_and_exact_ids_only() -> None:
    rows = _hot_rows(6, suffix="hotcap", id_base=800)
    engine, matchbook, kalshi, layer = _engine(rows, timeout=2, hot_interval=0)
    result = await _run_hot(engine)
    assert len(result.evaluated) == 6
    assert layer._peak_inflight[VenueName.MATCHBOOK] <= 4
    assert layer._peak_inflight[VenueName.KALSHI] <= 4
    assert layer._peak_inflight[VenueName.POLYMARKET] <= 8
    assert DEFAULT_PROVIDER_CONCURRENCY == {
        VenueName.MATCHBOOK: 4,
        VenueName.POLYMARKET: 8,
        VenueName.KALSHI: 4,
    }
    assert Settings.model_fields["paper_scan_provider_timeout_seconds"].default == 8
    assert engine._provider_timeout == 2
    scheduler_source = inspect.getsource(run_staged_exact_id_slice)
    hot_source = inspect.getsource(run_hot_latency_exact_id_slice)
    for source in (scheduler_source, hot_source):
        assert "list_events(" not in source
        assert "list_markets(" not in source
    assert matchbook.list_events_calls == 0
    assert kalshi.list_events_calls == 0
    assert ScanLane.HOT.value == "hot"


async def test_active_trade_still_prices_through_the_sequential_item_path() -> None:
    source = inspect.getsource(LiveRefreshCoordinator)
    assert "lane=PRICE_ENGINE_ACTIVE_TRADE_LANE" in source
    assert "_price_item(" in source
    assert "run_hot_latency_exact_id_slice" not in source
    assert "run_staged_exact_id_slice" not in source
    assert "run_background_exact_id_slice" not in source
