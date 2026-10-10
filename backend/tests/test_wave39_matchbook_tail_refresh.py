"""Wave 39: one bounded Matchbook tail refresh after a slow Polymarket Price-2.

Fixture/demo transports and injected retrieval clocks. No live orders.
Existing 500 ms skew and 2,000 ms quote-age gates stay unchanged.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from test_dual_cadence_scheduler import FakeClock
from test_issue344_price_engine import (
    NOW,
    FakeKalshi,
    FakeMatchbook,
    StubPaperScan,
    _engine,
    _hda_row,
    _mb_match_odds,
    _qualifying_decision,
    _row,
)
from test_execution_reprice_before_paper_entry import _stamp

from sports_hedge.application.approved_market_catalogue import OutcomeNativeId
from sports_hedge.application.execution_reprice import (
    EXECUTION_REPRICE_DUPLICATE,
    EXECUTION_REPRICE_FAILED,
    EXECUTION_REPRICE_NO_LONGER_QUALIFYING,
    EXECUTION_REPRICE_SKEW,
    EXECUTION_REPRICE_STALE,
)
from sports_hedge.application.execution_snapshot import (
    DEFAULT_MAX_SNAPSHOT_SKEW_MS,
    MATCHBOOK_TAIL_REFRESH_FAILED,
    MATCHBOOK_TAIL_REFRESH_SKIPPED_COHERENT,
    MATCHBOOK_TAIL_REFRESH_SKIPPED_INCOMPLETE,
    MATCHBOOK_TAIL_REFRESH_SKIPPED_MB_NOT_OLDEST,
    MATCHBOOK_TAIL_REFRESH_SKIPPED_NOT_MB_PM,
    MATCHBOOK_TAIL_REFRESH_SKIPPED_PM_INTERNAL_SKEW,
    MATCHBOOK_TAIL_REFRESH_SKIPPED_PM_STALE,
    MATCHBOOK_TAIL_REFRESH_SUCCESS,
)
from sports_hedge.application.price_engine import (
    CataloguePriceEngine,
    RetrievedVenuePayload,
)
from sports_hedge.arbitrage.models import PayoffSolution
from sports_hedge.arbitrage.payoff_scan import PayoffScanResult
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.venues.rate_limit import ProviderRateLimitedError

TOKEN_HOME = "713856789012345678901"
TOKEN_DRAW = "713856789012345678902"
TOKEN_AWAY = "713856789012345678903"
TOKENS = (
    OutcomeNativeId(outcome="home", native_id=TOKEN_HOME),
    OutcomeNativeId(outcome="draw", native_id=TOKEN_DRAW),
    OutcomeNativeId(outcome="away", native_id=TOKEN_AWAY),
)
OPERATOR_PM_MS = {TOKEN_HOME: 2680, TOKEN_DRAW: 2700, TOKEN_AWAY: 2720}
OPERATOR_TAIL_MS = 2780


def _eligible():
    return _qualifying_decision().model_copy(
        update={"eligible_for_paper_simulation": True, "rejection_reasons": []}
    )


def _ineligible():
    return _qualifying_decision().model_copy(
        update={
            "eligible_for_paper_simulation": False,
            "rejection_reasons": ["no_positive_edge"],
            "payoff_scan": PayoffScanResult(
                solution=PayoffSolution(is_arbitrage=False, roi=Decimal("0"))
            ),
        }
    )


class OddsAwareScan(StubPaperScan):
    def __init__(self) -> None:
        super().__init__(_eligible())
        self.seen_best: list[Decimal] = []
        self.settings = Settings(
            min_net_edge=0.002,
            max_one_time_gbp=3,
            max_event_gbp=5,
            max_opportunity_gbp=5,
            sports_hedge_execution_enabled=False,
        )

    def scan_pair(self, left, right, **kwargs):
        del kwargs
        self.calls += 1
        matchbook = left if left.market.source_venue is VenueName.MATCHBOOK else right
        best = Decimal("0")
        for book in matchbook.outcome_books:
            if book.best_back is not None:
                best = max(best, book.best_back.decimal_odds)
        self.seen_best.append(best)
        if best >= Decimal("4"):
            return _eligible()
        return _ineligible()


def _mb_pm_row(suffix: str = "w39"):
    return _hda_row(suffix).model_copy(
        update={
            "polymarket_event_id": "1016065",
            "polymarket_market_id": "4521504",
            "polymarket_token_ids": list(TOKENS),
        }
    )


def _pm_book(token: str, *, timestamp: Any = None) -> dict[str, Any]:
    book: dict[str, Any] = {
        "asset_id": token,
        "bids": [{"price": "0.38", "size": "200"}],
        "asks": [{"price": "0.40", "size": "200"}],
    }
    if timestamp is not None:
        book["timestamp"] = timestamp
    return book


def _rich_mb(market_id: int, *, odds: str = "5.20") -> dict[str, Any]:
    market = _mb_match_odds(market_id)
    market["id"] = market_id
    for runner in market["runners"]:
        runner["prices"][0]["odds"] = odds
        runner["prices"][0]["available-amount"] = "500"
    return _stamp(market, NOW)


class ScriptMatchbook(FakeMatchbook):
    def __init__(self, payloads: list[dict | None | Exception]) -> None:
        super().__init__()
        self._payloads = payloads
        self.started = asyncio.Event()

    async def get_market(self, event_id: int | str, market_id: int | str, **filters: object):
        del filters
        self.get_market_calls.append((str(event_id), str(market_id)))
        if len(self.get_market_calls) == 1:
            self.started.set()
        index = len(self.get_market_calls) - 1
        if index >= len(self._payloads):
            return None
        payload = self._payloads[index]
        if isinstance(payload, Exception):
            raise payload
        return payload


class ScriptPolymarket:
    def __init__(self, books: dict[str, dict | None] | None = None) -> None:
        self.calls: list[str] = []
        self.books = books or {token.native_id: _pm_book(token.native_id) for token in TOKENS}

    async def get_order_book(self, event_id, market_id, token_id, **filters):
        del event_id, market_id, filters
        token = str(token_id)
        self.calls.append(token)
        return self.books.get(token)


def _stamp_engine(
    engine: CataloguePriceEngine,
    *,
    mb_ms: list[int],
    pm_ms: dict[str, int],
) -> None:
    orig_mb = engine._execution_fetch_matchbook
    orig_pm = engine._execution_fetch_polymarket
    clock = engine._clock

    async def stamped_mb(*args: Any, **kwargs: Any):
        result = await orig_mb(*args, **kwargs)
        if result is None:
            return None
        call = len(engine.matchbook.get_market_calls)
        delay = mb_ms[min(call - 1, len(mb_ms) - 1)]
        at = NOW + timedelta(milliseconds=delay)
        if callable(clock) and hasattr(clock, "now"):
            clock.now = at
        return RetrievedVenuePayload(payload=result.payload, retrieved_at=at)

    async def stamped_pm(*args: Any, **kwargs: Any):
        result = await orig_pm(*args, **kwargs)
        if not result:
            return result
        stamped: dict[str, RetrievedVenuePayload] = {}
        latest = NOW
        for token, book in result.items():
            at = NOW + timedelta(milliseconds=pm_ms.get(token, 0))
            latest = max(latest, at)
            stamped[token] = RetrievedVenuePayload(payload=book.payload, retrieved_at=at)
        if callable(clock) and hasattr(clock, "now"):
            clock.now = latest
        return stamped

    engine._execution_fetch_matchbook = stamped_mb  # type: ignore[method-assign]
    engine._execution_fetch_polymarket = stamped_pm  # type: ignore[method-assign]


def _ready_engine(
    *,
    matchbook: FakeMatchbook,
    polymarket: ScriptPolymarket | None = None,
    paper_scan: Any = None,
    timeout: float = 0.2,
    row=None,
):
    clock = FakeClock(NOW)
    engine, mb, _ks, _layer = _engine(
        [row or _mb_pm_row()],
        matchbook=matchbook,
        kalshi=FakeKalshi(),
        paper_scan=paper_scan or StubPaperScan(_eligible()),
        clock=clock,
        timeout=timeout,
    )
    engine.polymarket = polymarket or ScriptPolymarket()
    if not hasattr(engine.paper_scan, "settings"):
        engine.paper_scan.settings = Settings()
    return engine, mb, clock


async def _reprice(engine: CataloguePriceEngine, row_id: str | None = None):
    runtime = engine.item(row_id or engine._items[next(iter(engine._items))].identity.catalogue_row_id)
    assert runtime is not None
    return await engine.reprice_for_paper_entry(
        runtime,
        venues=(VenueName.MATCHBOOK, VenueName.POLYMARKET),
    )


def _tail(result):
    assert result.snapshot is not None
    return result.snapshot.matchbook_tail_refresh


def test_wave39_does_not_relax_skew_age_or_arm_execution() -> None:
    settings = Settings()
    assert settings.paper_execution_max_snapshot_skew_ms == DEFAULT_MAX_SNAPSHOT_SKEW_MS == 500
    assert settings.paper_entry_max_quote_age_ms == 2000
    assert settings.sports_hedge_execution_enabled is False
    assert settings.sports_hedge_mode == "paper"
    source = inspect.getsource(CataloguePriceEngine._maybe_matchbook_tail_refresh)
    assert ".list_events(" not in source
    assert ".list_markets(" not in source
    assert "place_order" not in source
    assert source.count("await self._execution_fetch_matchbook") == 1


@pytest.mark.asyncio
async def test_operator_skew_recovers_with_one_matchbook_tail() -> None:
    market_id = int(_mb_pm_row().matchbook_market_id)
    rich = _rich_mb(market_id)
    matchbook = ScriptMatchbook([rich, rich])
    engine, mb, _clock = _ready_engine(matchbook=matchbook, paper_scan=OddsAwareScan())
    _stamp_engine(engine, mb_ms=[0, OPERATOR_TAIL_MS], pm_ms=OPERATOR_PM_MS)
    result = await _reprice(engine)
    tail = _tail(result)
    assert tail is not None
    assert tail.attempted is True
    assert tail.outcome == MATCHBOOK_TAIL_REFRESH_SUCCESS
    assert tail.original_skew_ms == 2720
    assert tail.final_skew_ms == OPERATOR_TAIL_MS - min(OPERATOR_PM_MS.values())
    assert result.snapshot is not None
    assert result.snapshot.skew_ms is not None
    assert result.snapshot.skew_ms <= 500
    assert result.snapshot.max_skew_ms == 500
    assert result.snapshot.max_quote_age_ms == 2000
    assert result.snapshot.accepted is True
    assert result.reason is None
    assert len(mb.get_market_calls) == 2
    assert engine.polymarket.calls == [TOKEN_HOME, TOKEN_DRAW, TOKEN_AWAY]
    mb_calls = [call for call in result.diagnostics.calls if call.venue == "matchbook"]
    assert len(mb_calls) == 2
    pm_retrieved = {native_id for native_id, _at in tail.polymarket_retrieved_at}
    assert pm_retrieved == {TOKEN_HOME, TOKEN_DRAW, TOKEN_AWAY}
    payload = json.loads(result.snapshot.to_json())
    assert payload["matchbook_tail_refresh"]["attempted"] is True
    assert payload["matchbook_tail_refresh"]["original_skew_ms"] == 2720
    assert "matchbook_tail_refresh_attempted=true" in result.snapshot.audit_line()
    assert mb.list_events_calls == 0
    assert Settings().sports_hedge_execution_enabled is False


@pytest.mark.asyncio
async def test_refreshed_matchbook_evaporates_theoretical_edge() -> None:
    market_id = int(_mb_pm_row().matchbook_market_id)
    scan = OddsAwareScan()
    matchbook = ScriptMatchbook([_rich_mb(market_id, odds="5.20"), _rich_mb(market_id, odds="1.10")])
    engine, mb, _clock = _ready_engine(matchbook=matchbook, paper_scan=scan)
    _stamp_engine(engine, mb_ms=[0, OPERATOR_TAIL_MS], pm_ms=OPERATOR_PM_MS)
    result = await _reprice(engine)
    tail = _tail(result)
    assert tail.outcome == MATCHBOOK_TAIL_REFRESH_SUCCESS
    assert scan.calls == 1
    assert scan.seen_best == [Decimal("1.10")]
    assert result.snapshot is not None
    assert result.snapshot.accepted is False
    assert result.reason == EXECUTION_REPRICE_NO_LONGER_QUALIFYING
    assert result.snapshot.frozen_orders == ()
    assert len(mb.get_market_calls) == 2
    assert engine.polymarket.calls == [TOKEN_HOME, TOKEN_DRAW, TOKEN_AWAY]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "second",
    [
        None,
        {},
        {"id": "999999", "name": "Match Odds", "status": "open", "runners": []},
        {"id": int(_mb_pm_row().matchbook_market_id), "name": "Match Odds", "status": "closed", "runners": []},
        ProviderRateLimitedError(1.0, provider="matchbook"),
    ],
)
async def test_failed_tail_rejects_without_retrying_polymarket(second) -> None:
    market_id = int(_mb_pm_row().matchbook_market_id)
    matchbook = ScriptMatchbook([_rich_mb(market_id), second])
    engine, mb, _clock = _ready_engine(matchbook=matchbook)
    _stamp_engine(engine, mb_ms=[0, OPERATOR_TAIL_MS], pm_ms=OPERATOR_PM_MS)
    result = await _reprice(engine)
    tail = _tail(result)
    assert tail.attempted is True
    assert tail.outcome == MATCHBOOK_TAIL_REFRESH_FAILED
    assert result.reason == EXECUTION_REPRICE_FAILED
    assert result.snapshot is not None
    assert result.snapshot.accepted is False
    assert len(mb.get_market_calls) == 2
    assert engine.polymarket.calls == [TOKEN_HOME, TOKEN_DRAW, TOKEN_AWAY]


@pytest.mark.asyncio
async def test_tail_timeout_fails_closed_without_loop() -> None:
    class _Hang(ScriptMatchbook):
        async def get_market(self, event_id, market_id, **filters):
            self.get_market_calls.append((str(event_id), str(market_id)))
            if len(self.get_market_calls) == 1:
                self.started.set()
                return _rich_mb(int(market_id))
            await asyncio.Event().wait()
            return None

    matchbook = _Hang([])
    engine, mb, _clock = _ready_engine(matchbook=matchbook, timeout=0.05)
    _stamp_engine(engine, mb_ms=[0, OPERATOR_TAIL_MS], pm_ms=OPERATOR_PM_MS)
    result = await _reprice(engine)
    assert _tail(result).outcome == MATCHBOOK_TAIL_REFRESH_FAILED
    assert result.reason == EXECUTION_REPRICE_FAILED
    assert len(mb.get_market_calls) == 2
    assert engine.polymarket.calls == [TOKEN_HOME, TOKEN_DRAW, TOKEN_AWAY]


@pytest.mark.asyncio
async def test_pm_source_stale_skips_tail_and_does_not_rescue() -> None:
    market_id = int(_mb_pm_row().matchbook_market_id)
    stale_ts = int((NOW - timedelta(seconds=5)).timestamp() * 1000)
    polymarket = ScriptPolymarket(
        {token.native_id: _pm_book(token.native_id, timestamp=stale_ts) for token in TOKENS}
    )
    matchbook = ScriptMatchbook([_rich_mb(market_id), _rich_mb(market_id)])
    engine, mb, _clock = _ready_engine(matchbook=matchbook, polymarket=polymarket)
    _stamp_engine(engine, mb_ms=[0, OPERATOR_TAIL_MS], pm_ms=OPERATOR_PM_MS)
    result = await _reprice(engine)
    assert _tail(result).outcome == MATCHBOOK_TAIL_REFRESH_SKIPPED_PM_STALE
    assert result.reason == EXECUTION_REPRICE_SKEW
    assert len(mb.get_market_calls) == 1


@pytest.mark.asyncio
async def test_pm_internal_skew_is_not_rescued_by_matchbook() -> None:
    market_id = int(_mb_pm_row().matchbook_market_id)
    matchbook = ScriptMatchbook([_rich_mb(market_id), _rich_mb(market_id)])
    engine, mb, _clock = _ready_engine(matchbook=matchbook)
    _stamp_engine(
        engine,
        mb_ms=[0, 800],
        pm_ms={TOKEN_HOME: 100, TOKEN_DRAW: 120, TOKEN_AWAY: 700},
    )
    result = await _reprice(engine)
    assert _tail(result).outcome == MATCHBOOK_TAIL_REFRESH_SKIPPED_PM_INTERNAL_SKEW
    assert result.reason == EXECUTION_REPRICE_SKEW
    assert len(mb.get_market_calls) == 1


@pytest.mark.asyncio
async def test_stale_matchbook_source_after_aligned_tail_still_rejects() -> None:
    market_id = int(_mb_pm_row().matchbook_market_id)
    fresh = _rich_mb(market_id)
    stale = _stamp(_rich_mb(market_id), NOW - timedelta(seconds=30))
    class _FreshnessScan(StubPaperScan):
        def scan_pair(self, left, right, **kwargs):
            del kwargs
            self.calls += 1
            ages = [left.quote_age_ms, right.quote_age_ms]
            if any(age is None or age > 2000 for age in ages):
                return _qualifying_decision().model_copy(
                    update={
                        "eligible_for_paper_simulation": False,
                        "rejection_reasons": ["stale_quote"],
                    }
                )
            return _eligible()

    matchbook = ScriptMatchbook([fresh, stale])
    engine, mb, _clock = _ready_engine(matchbook=matchbook, paper_scan=_FreshnessScan(_eligible()))
    _stamp_engine(engine, mb_ms=[0, OPERATOR_TAIL_MS], pm_ms=OPERATOR_PM_MS)
    result = await _reprice(engine)
    tail = _tail(result)
    assert tail.outcome == MATCHBOOK_TAIL_REFRESH_SUCCESS
    assert result.snapshot is not None
    assert result.snapshot.skew_ms is not None
    assert result.snapshot.skew_ms <= 500
    assert result.reason == EXECUTION_REPRICE_STALE
    assert result.snapshot.accepted is False


@pytest.mark.asyncio
async def test_no_tail_when_initial_skew_within_limit() -> None:
    market_id = int(_mb_pm_row().matchbook_market_id)
    matchbook = ScriptMatchbook([_rich_mb(market_id), _rich_mb(market_id)])
    engine, mb, _clock = _ready_engine(matchbook=matchbook)
    _stamp_engine(
        engine,
        mb_ms=[0, 400],
        pm_ms={TOKEN_HOME: 20, TOKEN_DRAW: 40, TOKEN_AWAY: 80},
    )
    result = await _reprice(engine)
    assert _tail(result).outcome == MATCHBOOK_TAIL_REFRESH_SKIPPED_COHERENT
    assert len(mb.get_market_calls) == 1
    assert result.snapshot is not None
    assert result.snapshot.accepted is True


@pytest.mark.asyncio
async def test_no_tail_for_matchbook_kalshi_or_incomplete_pm() -> None:
    btts = _row(suffix="mbks", matchbook_event_id="8899", matchbook_market_id="316199")
    matchbook = ScriptMatchbook([_stamp(_mb_match_odds(316199), NOW)])
    engine, mb, _ks, _layer = _engine(
        [btts],
        matchbook=matchbook,
        paper_scan=StubPaperScan(_eligible()),
        clock=FakeClock(NOW),
    )
    engine.paper_scan.settings = Settings()
    runtime = engine.item(btts.catalogue_row_id)
    assert runtime is not None
    result = await engine.reprice_for_paper_entry(
        runtime,
        venues=(VenueName.MATCHBOOK, VenueName.KALSHI),
    )
    assert result.snapshot is not None
    assert result.snapshot.matchbook_tail_refresh.outcome == MATCHBOOK_TAIL_REFRESH_SKIPPED_NOT_MB_PM
    assert len(mb.get_market_calls) == 1

    missing = ScriptPolymarket({TOKEN_HOME: _pm_book(TOKEN_HOME), TOKEN_DRAW: None, TOKEN_AWAY: _pm_book(TOKEN_AWAY)})
    row = _mb_pm_row("miss")
    market_id = int(row.matchbook_market_id)
    mb2 = ScriptMatchbook([_rich_mb(market_id), _rich_mb(market_id)])
    engine2, mb_client, _clock = _ready_engine(matchbook=mb2, polymarket=missing, row=row)
    _stamp_engine(engine2, mb_ms=[0, OPERATOR_TAIL_MS], pm_ms=OPERATOR_PM_MS)
    result2 = await _reprice(engine2, row.catalogue_row_id)
    assert _tail(result2).outcome == MATCHBOOK_TAIL_REFRESH_SKIPPED_INCOMPLETE
    assert result2.reason == EXECUTION_REPRICE_FAILED
    assert len(mb_client.get_market_calls) == 1


@pytest.mark.asyncio
async def test_no_tail_when_matchbook_is_not_the_oldest_book() -> None:
    market_id = int(_mb_pm_row().matchbook_market_id)
    matchbook = ScriptMatchbook([_rich_mb(market_id), _rich_mb(market_id)])
    engine, mb, _clock = _ready_engine(matchbook=matchbook)
    _stamp_engine(
        engine,
        mb_ms=[800, 900],
        pm_ms={TOKEN_HOME: 0, TOKEN_DRAW: 20, TOKEN_AWAY: 40},
    )
    result = await _reprice(engine)
    assert _tail(result).outcome == MATCHBOOK_TAIL_REFRESH_SKIPPED_MB_NOT_OLDEST
    assert result.reason == EXECUTION_REPRICE_SKEW
    assert len(mb.get_market_calls) == 1


@pytest.mark.asyncio
async def test_duplicate_suppression_allows_only_one_in_flight_reprice() -> None:
    market_id = int(_mb_pm_row().matchbook_market_id)
    release = asyncio.Event()

    class _Hold(ScriptMatchbook):
        async def get_market(self, event_id, market_id, **filters):
            del filters
            self.get_market_calls.append((str(event_id), str(market_id)))
            self.started.set()
            await release.wait()
            return _rich_mb(int(market_id))

    matchbook = _Hold([_rich_mb(market_id)])
    engine, mb, _clock = _ready_engine(matchbook=matchbook)
    runtime = engine.item(_mb_pm_row().catalogue_row_id)
    assert runtime is not None
    first = asyncio.create_task(
        engine.reprice_for_paper_entry(
            runtime, venues=(VenueName.MATCHBOOK, VenueName.POLYMARKET)
        )
    )
    await asyncio.wait_for(matchbook.started.wait(), timeout=1)
    duplicate = await engine.reprice_for_paper_entry(
        runtime, venues=(VenueName.MATCHBOOK, VenueName.POLYMARKET)
    )
    assert duplicate.reason == EXECUTION_REPRICE_DUPLICATE
    assert duplicate.duplicate is True
    release.set()
    await first
    assert len(mb.get_market_calls) >= 1


@pytest.mark.asyncio
async def test_iterative_price2_allows_one_tail_per_attempt() -> None:
    market_id = int(_mb_pm_row().matchbook_market_id)
    matchbook = ScriptMatchbook(
        [_rich_mb(market_id), _rich_mb(market_id), _rich_mb(market_id), _rich_mb(market_id)]
    )
    engine, mb, _clock = _ready_engine(matchbook=matchbook)
    _stamp_engine(
        engine,
        mb_ms=[0, OPERATOR_TAIL_MS, 0, OPERATOR_TAIL_MS],
        pm_ms=OPERATOR_PM_MS,
    )
    first_ok = await _reprice(engine)
    second_ok = await _reprice(engine)
    assert _tail(first_ok).outcome == MATCHBOOK_TAIL_REFRESH_SUCCESS
    assert _tail(second_ok).outcome == MATCHBOOK_TAIL_REFRESH_SUCCESS
    assert first_ok.snapshot.snapshot_id != second_ok.snapshot.snapshot_id
    assert len(mb.get_market_calls) == 4
    runtime = engine.item(_mb_pm_row().catalogue_row_id)
    assert runtime is not None
    work = engine._execution_scheduler_work(
        runtime, (VenueName.MATCHBOOK, VenueName.POLYMARKET)
    )
    assert work.lane == "execution_candidate"


@pytest.mark.asyncio
async def test_slow_tail_that_widens_skew_rejects() -> None:
    market_id = int(_mb_pm_row().matchbook_market_id)
    matchbook = ScriptMatchbook([_rich_mb(market_id), _rich_mb(market_id)])
    engine, mb, _clock = _ready_engine(matchbook=matchbook)
    _stamp_engine(engine, mb_ms=[0, 4000], pm_ms=OPERATOR_PM_MS)
    result = await _reprice(engine)
    assert _tail(result).outcome == MATCHBOOK_TAIL_REFRESH_SUCCESS
    assert result.reason == EXECUTION_REPRICE_SKEW
    assert result.snapshot.accepted is False
    assert len(mb.get_market_calls) == 2
