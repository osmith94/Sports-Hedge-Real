"""Issue #554 BACKGROUND exact-ID pricing planner.

Data class: synthetic/fixture providers and barriers. Not live quotes.
PAPER / read-only. Provider caps stay Matchbook 4 / Kalshi 4 / Polymarket 8.

The row-centric reference below is the previous BACKGROUND shape: a fixed
worker pool, each worker owning one catalogue row from Matchbook through
Kalshi. It is not the production scheduler anymore. It exists so the test can
show, with the same fakes, that the old shape stops issuing Matchbook reads
once every worker is stuck behind a full Kalshi stall, while the staged
planner keeps issuing them.
"""

from __future__ import annotations

import asyncio
import inspect
from typing import Any

import pytest
from test_dual_cadence_scheduler import FakeClock
from test_issue344_price_engine import (
    DISTANT_KICKOFF,
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
from test_issue348_phase5_observability import _flat_decision
from test_issue470_hot_viability_branch_and_bound import (
    _expensive_kalshi_book,
    _named_match_odds,
)

from sports_hedge.application.approved_market_catalogue import OutcomeNativeId
from sports_hedge.application.background_exact_id_planner import run_background_exact_id_slice
from sports_hedge.application.price_engine import PriceEnginePriority
from sports_hedge.application.provider_access import (
    PRICE_ENGINE_BACKGROUND_LANE,
    ProviderAccessLayer,
    ProviderPriority,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.approved_register import CANONICAL_BTTS_FT

pytestmark = pytest.mark.asyncio


class _StallKalshi(FakeKalshi):
    """The first four order-book calls occupy Kalshi until ``release`` is set."""

    def __init__(self) -> None:
        super().__init__()
        self.stall_budget = 4
        self.stall_inflight = 0
        self.release = asyncio.Event()

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
        if self.stall_budget <= 0:
            return _kalshi_book()
        self.stall_budget -= 1
        self.stall_inflight += 1
        try:
            await self.release.wait()
        finally:
            self.stall_inflight -= 1
        return _kalshi_book()


class _CountingMatchbook(FakeMatchbook):
    def __init__(self, kalshi: _StallKalshi) -> None:
        super().__init__()
        self.kalshi = kalshi
        self.calls_while_kalshi_stalled = 0

    async def get_market(
        self,
        event_id: int | str,
        market_id: int | str,
        **filters: Any,
    ) -> dict[str, Any]:
        del filters
        market = str(market_id)
        self.get_market_calls.append((str(event_id), market))
        if self.kalshi.stall_inflight >= 2 and not self.kalshi.release.is_set():
            self.calls_while_kalshi_stalled += 1
        return _mb_btts(int(market))


def _btts_rows(count: int, *, suffix: str, id_base: int) -> list[Any]:
    rows = []
    for index in range(count):
        number = id_base + index
        rows.append(
            _row(
                suffix=f"{suffix}-{index}",
                key=CANONICAL_BTTS_FT,
                kickoff=DISTANT_KICKOFF,
                matchbook_event_id=str(800000 + number),
                matchbook_market_id=str(500000 + number),
                kalshi_event=f"KX{suffix}{number}",
            )
        )
    return rows


async def _row_centric_reference(
    rows: list[Any],
    matchbook: _CountingMatchbook,
    kalshi: _StallKalshi,
    layer: ProviderAccessLayer,
) -> int:
    """Previous worker shape. Returns Matchbook calls issued during the Kalshi stall."""

    pending: asyncio.Queue[Any] = asyncio.Queue()
    for row in rows:
        pending.put_nowait(row)

    async def _worker() -> None:
        while True:
            try:
                row = pending.get_nowait()
            except asyncio.QueueEmpty:
                return
            async with layer.acquire_wait(
                VenueName.MATCHBOOK,
                lane=PRICE_ENGINE_BACKGROUND_LANE,
                stage="get_market",
                timeout=3,
            ):
                await matchbook.get_market(row.matchbook_event_id, row.matchbook_market_id)
            async with layer.acquire_wait(
                VenueName.KALSHI,
                lane=PRICE_ENGINE_BACKGROUND_LANE,
                stage="order_book",
                timeout=3,
            ):
                ticker = row.kalshi_market_tickers[0]
                await kalshi.get_order_book(row.kalshi_event_ticker, ticker)

    async def _release_after_stall() -> int:
        # BACKGROUND may occupy only the lower-priority Kalshi ceiling (2 of 4).
        while kalshi.stall_inflight < 2:
            await asyncio.sleep(0)
        for _ in range(40):
            await asyncio.sleep(0)
        seen = matchbook.calls_while_kalshi_stalled
        kalshi.release.set()
        return seen

    workers = [asyncio.create_task(_worker()) for _ in range(8)]
    seen, *_ = await asyncio.wait_for(
        asyncio.gather(_release_after_stall(), *workers),
        timeout=4,
    )
    return int(seen)


async def test_row_centric_shape_stops_matchbook_when_kalshi_slots_stall() -> None:
    rows = _btts_rows(16, suffix="old", id_base=1)
    kalshi = _StallKalshi()
    matchbook = _CountingMatchbook(kalshi)
    layer = ProviderAccessLayer(
        {VenueName.MATCHBOOK: 4, VenueName.KALSHI: 4, VenueName.POLYMARKET: 8}
    )
    seen = await _row_centric_reference(rows, matchbook, kalshi, layer)
    # Eight workers, two of which can sit in the lower-priority Kalshi ceiling.
    # The rest may finish the Matchbook read they already started, then wait.
    assert seen <= 6
    assert len(matchbook.get_market_calls) == 16


async def test_staged_planner_continues_matchbook_while_kalshi_slots_stall() -> None:
    rows = _btts_rows(16, suffix="new", id_base=1)
    kalshi = _StallKalshi()
    matchbook = _CountingMatchbook(kalshi)
    engine, matchbook, kalshi, _layer = _engine(
        rows,
        matchbook=matchbook,
        kalshi=kalshi,
        timeout=3,
    )

    async def _release_after_progress() -> int:
        while matchbook.calls_while_kalshi_stalled < 8:
            await asyncio.sleep(0)
        seen = matchbook.calls_while_kalshi_stalled
        kalshi.release.set()
        return seen

    seen, result = await asyncio.wait_for(
        asyncio.gather(
            _release_after_progress(),
            engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW),
        ),
        timeout=4,
    )
    assert seen >= 8
    assert result.pricing_call_shape == "provider_centric_staged_exact_id"
    assert set(result.evaluated) == {row.catalogue_row_id for row in rows}
    assert matchbook.list_events_calls == 0
    assert matchbook.list_markets_calls == []
    assert kalshi.list_events_calls == 0
    assert kalshi.list_markets_calls == []
    assert "list_events(" not in inspect.getsource(run_background_exact_id_slice)
    assert "list_markets(" not in inspect.getsource(run_background_exact_id_slice)


async def test_one_row_exception_does_not_stop_other_background_rows() -> None:
    good = [
        _row(
            suffix=f"ok-{index}",
            matchbook_market_id=str(610000 + index),
            kalshi_event=f"KXOK{index}",
        )
        for index in range(3)
    ]
    bad = _row(suffix="boom", matchbook_market_id="610099", kalshi_event="KXBOOM")

    class _BoomScan(StubPaperScan):
        def scan_pair(self, *args: Any, **kwargs: Any):
            if kwargs.get("fixture_canonical_event_id") == bad.canonical_event_id:
                raise RuntimeError("row exploded")
            return super().scan_pair(*args, **kwargs)

    engine, _mb, _ks, _layer = _engine(good + [bad], paper_scan=_BoomScan())
    result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    assert set(result.evaluated) == {row.catalogue_row_id for row in good}
    assert bad.catalogue_row_id in result.retry_wait
    runtime = engine.item(bad.catalogue_row_id)
    assert runtime is not None
    assert runtime.retry_attempt == 1
    assert "row exploded" in (runtime.last_error_detail or "")
    assert engine.item(good[0].catalogue_row_id).retry_attempt == 0


async def test_duplicate_exact_ids_are_issued_once_per_background_slice() -> None:
    first = _row(
        suffix="dup-a",
        matchbook_event_id="8801",
        matchbook_market_id="316020",
        kalshi_event="KXSHARE",
    )
    second = _row(
        suffix="dup-b",
        matchbook_event_id="8801",
        matchbook_market_id="316020",
        kalshi_event="KXSHARE",
    )
    engine, matchbook, kalshi, _layer = _engine([first, second])
    result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    assert set(result.evaluated) == {first.catalogue_row_id, second.catalogue_row_id}
    assert matchbook.get_market_calls == [("8801", "316020")]
    assert kalshi.book_calls == ["KXSHARE-BTTS"]
    assert result.coalesced_provider_calls >= 1
    assert result.issued_provider_calls == 2
    assert result.provider_stage_calls.get("matchbook:get_market") == 1
    assert result.provider_stage_calls.get("kalshi:order_book") == 1
    assert engine.item(first.catalogue_row_id).retry_attempt == 0
    assert engine.item(second.catalogue_row_id).retry_attempt == 0


async def test_coalesced_books_do_not_survive_the_next_slice() -> None:
    first = _row(suffix="slice-a", matchbook_market_id="316021", kalshi_event="KXSLICE")
    second = _row(
        suffix="slice-b",
        matchbook_market_id="316021",
        kalshi_event="KXSLICE",
        matchbook_event_id="8801",
    )
    engine, matchbook, kalshi, _layer = _engine(
        [first, second],
        background_interval=0,
        paper_scan=StubPaperScan(_flat_decision()),
    )
    await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    assert len(matchbook.get_market_calls) == 1
    await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    assert len(matchbook.get_market_calls) == 2
    assert len(kalshi.book_calls) == 2


async def test_coalesced_failure_fans_out_and_keeps_independent_retry_state() -> None:
    class _DownMatchbook(FakeMatchbook):
        async def get_market(
            self,
            event_id: int | str,
            market_id: int | str,
            **filters: Any,
        ) -> dict[str, Any]:
            del filters
            self.get_market_calls.append((str(event_id), str(market_id)))
            raise RuntimeError("mb down")

    first = _row(suffix="fail-a", matchbook_market_id="316030", kalshi_event="KXFAIL")
    second = _row(suffix="fail-b", matchbook_market_id="316030", kalshi_event="KXFAIL")
    matchbook = _DownMatchbook()
    engine, matchbook, _kalshi, _layer = _engine([first, second], matchbook=matchbook)
    result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    assert set(result.retry_wait) == {first.catalogue_row_id, second.catalogue_row_id}
    assert len(matchbook.get_market_calls) == 1
    assert result.coalesced_provider_calls >= 1
    assert engine.item(first.catalogue_row_id).retry_attempt == 1
    assert engine.item(second.catalogue_row_id).retry_attempt == 1


async def test_background_upper_bound_still_skips_impossible_kalshi_books() -> None:
    row = _hda_row("bg-bound", kickoff=DISTANT_KICKOFF)
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
    )
    result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    assert row.catalogue_row_id in result.skipped
    assert result.skip_reasons[row.catalogue_row_id] == "upper_bound_below_min_net"
    assert 0 < len(kalshi.book_calls) < len(row.kalshi_market_tickers)
    assert result.saved_provider_calls >= 1


async def test_background_provider_caps_stay_4_4_8() -> None:
    rows = _btts_rows(12, suffix="cap", id_base=300)
    engine, matchbook, kalshi, layer = _engine(rows, timeout=2)

    original_mb = matchbook.get_market
    original_k = kalshi.get_order_book

    async def _mb(event_id: int | str, market_id: int | str, **filters: Any) -> dict[str, Any]:
        assert layer.snapshot().inflight.get(VenueName.MATCHBOOK.value, 0) <= 4
        await asyncio.sleep(0)
        return await original_mb(event_id, market_id, **filters)

    async def _book(*args: Any, **kwargs: Any) -> dict[str, Any]:
        assert layer.snapshot().inflight.get(VenueName.KALSHI.value, 0) <= 4
        await asyncio.sleep(0)
        return await original_k(*args, **kwargs)

    matchbook.get_market = _mb  # type: ignore[method-assign]
    kalshi.get_order_book = _book  # type: ignore[method-assign]
    result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    assert len(result.evaluated) == 12
    assert layer._peak_inflight[VenueName.MATCHBOOK] <= 4
    assert layer._peak_inflight[VenueName.KALSHI] <= 4
    assert layer._peak_inflight[VenueName.POLYMARKET] <= 8


async def test_polymarket_cap_stays_at_8_under_the_background_planner() -> None:
    rows = []
    for index in range(10):
        base = _row(
            suffix=f"pm-{index}",
            matchbook_event_id=str(7700 + index),
            matchbook_market_id=str(420000 + index),
            kalshi_event=f"KXPM{index}",
        )
        rows.append(
            base.model_copy(
                update={
                    "kalshi_event_ticker": "",
                    "kalshi_market_tickers": [],
                    "kalshi_outcome_ids": [],
                    "polymarket_event_id": f"pm-event-{index}",
                    "polymarket_market_id": f"pm-market-{index}",
                    "polymarket_token_ids": [
                        OutcomeNativeId(outcome="yes", native_id=f"7{index:02d}000000000000000001"),
                        OutcomeNativeId(outcome="no", native_id=f"7{index:02d}000000000000000002"),
                    ],
                }
            )
        )

    class _Polymarket:
        def __init__(self, layer: ProviderAccessLayer) -> None:
            self.layer = layer
            self.current = 0
            self.peak = 0
            self.ready = asyncio.Event()
            self.calls = 0

        async def get_order_book(
            self,
            event_id: int | str,
            market_id: int | str,
            token_id: int | str,
            **filters: Any,
        ) -> dict[str, Any]:
            del event_id, market_id, token_id, filters
            self.calls += 1
            self.current += 1
            self.peak = max(self.peak, self.current)
            assert self.current <= 8
            assert self.layer.snapshot().inflight.get(VenueName.POLYMARKET.value, 0) <= 8
            if self.peak >= 8:
                self.ready.set()
            await self.ready.wait()
            self.current -= 1
            return {
                "bids": [{"price": "0.40", "size": "10"}],
                "asks": [{"price": "0.60", "size": "10"}],
            }

    engine, _mb, _ks, layer = _engine(rows, timeout=3)
    polymarket = _Polymarket(layer)
    engine.polymarket = polymarket
    await asyncio.wait_for(engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW), timeout=4)
    assert polymarket.peak == 8
    assert polymarket.calls == 20
    assert layer._peak_inflight[VenueName.POLYMARKET] <= 8


async def test_hot_acquire_is_granted_before_the_next_background_matchbook() -> None:
    class _HangMatchbook(FakeMatchbook):
        def __init__(self) -> None:
            super().__init__()
            self.entered = 0
            self.two = asyncio.Event()
            self.release_held = asyncio.Event()
            self.release_rest = asyncio.Event()

        async def get_market(
            self,
            event_id: int | str,
            market_id: int | str,
            **filters: Any,
        ) -> dict[str, Any]:
            del filters
            self.get_market_calls.append((str(event_id), str(market_id)))
            self.entered += 1
            ordinal = self.entered
            if ordinal == 2:
                self.two.set()
            if ordinal <= 2:
                await self.release_held.wait()
            else:
                await self.release_rest.wait()
            return _mb_btts(int(market_id))

    rows = _btts_rows(6, suffix="prio", id_base=400)
    matchbook = _HangMatchbook()
    engine, matchbook, _kalshi, layer = _engine(rows, matchbook=matchbook, timeout=3)

    async def _hot_then_release() -> None:
        await matchbook.two.wait()
        assert 2 <= layer.lower_in_use[VenueName.MATCHBOOK] <= 3

        async def _hot() -> None:
            async with layer.acquire_wait(
                VenueName.MATCHBOOK,
                lane="hot",
                stage="get_market",
                timeout=1,
            ) as lease:
                assert lease is not None
                assert layer.lower_in_use[VenueName.MATCHBOOK] <= 3
                assert layer.snapshot().inflight[VenueName.MATCHBOOK.value] >= 3
                matchbook.release_held.set()
                matchbook.release_rest.set()

        await _hot()

    _hot_result, slice_result = await asyncio.wait_for(
        asyncio.gather(
            _hot_then_release(),
            engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW),
        ),
        timeout=4,
    )
    assert len(slice_result.evaluated) == 6
    assert ProviderPriority.ACTIVE_TRADE < ProviderPriority.HOT < ProviderPriority.BACKGROUND


async def test_hot_slice_coalesces_exact_ids_inside_one_slice_only() -> None:
    from datetime import timedelta

    from sports_hedge.application.hot_latency_exact_id import HOT_LATENCY_CALL_SHAPE

    near = NOW + timedelta(minutes=20)
    first = _row(
        suffix="hot-a",
        kickoff=near,
        matchbook_event_id="8810",
        matchbook_market_id="316080",
        kalshi_event="KXHOTSHARE",
    )
    second = _row(
        suffix="hot-b",
        kickoff=near,
        matchbook_event_id="8810",
        matchbook_market_id="316080",
        kalshi_event="KXHOTSHARE",
    )
    engine, matchbook, kalshi, _layer = _engine([first, second], hot_interval=0)
    result = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
    assert set(result.evaluated) == {first.catalogue_row_id, second.catalogue_row_id}
    assert len(matchbook.get_market_calls) == 1
    assert len(kalshi.book_calls) == 1
    assert result.coalesced_provider_calls >= 1
    assert result.pricing_call_shape == HOT_LATENCY_CALL_SHAPE
    second_result = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
    assert len(matchbook.get_market_calls) == 2
    assert len(kalshi.book_calls) == 2
    assert second_result.coalesced_provider_calls >= 1


async def test_successful_background_rows_stay_out_until_their_reprice_age() -> None:
    row = _row(suffix="age", matchbook_market_id="316090", kalshi_event="KXAGE")
    clock = FakeClock(NOW)
    engine, matchbook, _kalshi, _layer = _engine(
        [row],
        clock=clock,
        background_interval=600,
        paper_scan=StubPaperScan(_flat_decision()),
    )
    first = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=clock.now)
    assert first.evaluated == [row.catalogue_row_id]
    cursor = engine.coverage_cursor(PriceEnginePriority.BACKGROUND)
    assert cursor.hold_until is not None
    cursor.hold_for_target(target_seconds=0, now=clock.now)
    second = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=clock.now)
    assert second.evaluated == [row.catalogue_row_id]
    assert len(matchbook.get_market_calls) == 2
