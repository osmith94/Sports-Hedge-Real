"""Execution-grade Price-2 snapshot: skew, audit phases, and one fill record.

Fixture/demo books. No live orders. The 2,000 ms freshness gate stays in place.
"""

from __future__ import annotations

import asyncio
import json
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
from test_issue344_price_engine import NOW, FakeKalshi, FakeMatchbook, _engine, _row

from sports_hedge.application.execution_reprice import (
    EXECUTION_REPRICE_DUPLICATE,
    EXECUTION_REPRICE_SKEW,
    PHASE_BOOK_RETRIEVED,
    PHASE_DISCOVERY_PRICE,
    PHASE_FILL_ATTEMPTED,
    PHASE_FILL_COMPLETE,
    PHASE_PAPER_ELIGIBLE,
    PHASE_REPRICE_STARTED,
    PHASE_SNAPSHOT_COMPLETE,
)
from sports_hedge.application.execution_snapshot import (
    DEFAULT_MAX_SNAPSHOT_SKEW_MS,
    execution_max_snapshot_skew_ms,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.trades import PaperTradeAuditEventType
from sports_hedge.persistence.liquidity import SqlitePaperLiquidityRepository


def test_default_snapshot_skew_is_tighter_than_quote_age_and_clamped() -> None:
    settings = Settings()
    assert settings.paper_entry_max_quote_age_ms == 2000
    assert settings.paper_execution_max_snapshot_skew_ms == DEFAULT_MAX_SNAPSHOT_SKEW_MS
    assert DEFAULT_MAX_SNAPSHOT_SKEW_MS == 500
    assert execution_max_snapshot_skew_ms(settings) == 500
    tighter = settings.model_copy(update={"paper_entry_max_quote_age_ms": 400})
    assert execution_max_snapshot_skew_ms(tighter) == 400
    assert settings.sports_hedge_execution_enabled is False


@pytest.mark.asyncio
async def test_snapshot_skew_fails_closed_when_quote_ages_still_pass(tmp_path) -> None:
    clock_at = NOW
    matchbook_returned = asyncio.Event()

    class _Clock:
        def now(self):
            return clock_at

    class _Matchbook(FakeMatchbook):
        async def get_market(self, event_id, market_id, **filters):
            del filters
            self.get_market_calls.append((str(event_id), str(market_id)))
            matchbook_returned.set()
            return _stamp(_market(), NOW)

    class _Kalshi(FakeKalshi):
        async def get_order_book(self, event_id, market_id, outcome_id=None, **filters):
            nonlocal clock_at
            del event_id, outcome_id, filters
            self.book_calls.append(str(market_id))
            await matchbook_returned.wait()
            await asyncio.sleep(0)
            clock_at = NOW + timedelta(milliseconds=600)
            return _book("0.20", "0.70")

    settings = Settings(paper_autofill_enabled=True)
    scan = PaperScanService(
        MarketIntelligenceService(SqliteMarketIntelligenceRepository()),
        settings=settings,
        liquidity=SqlitePaperLiquidityRepository(
            tmp_path / "skew-liquidity.sqlite",
            matchbook_gbp=Decimal(5000),
            polymarket_usd=Decimal(5000),
            kalshi_usd=Decimal(5000),
        ),
    )
    row = _row(
        suffix="skew",
        matchbook_event_id=EVENT,
        matchbook_market_id=MARKET,
        kalshi_event="KXEPLBTTS-RICH",
    )
    engine, _mb, _ks, _layer = _engine(
        [row],
        matchbook=_Matchbook(),
        kalshi=_Kalshi(),
        paper_scan=scan,
        clock=_Clock().now,
    )
    engine.venue_costs = _costs()
    engine.fx_snapshots = _fx()
    runtime = engine.item(row.catalogue_row_id)
    assert runtime is not None
    result = await engine.reprice_for_paper_entry(
        runtime,
        venues=(VenueName.MATCHBOOK, VenueName.KALSHI),
    )
    assert result.decision is None
    assert result.reason == EXECUTION_REPRICE_SKEW
    snapshot = result.snapshot
    assert snapshot is not None
    assert snapshot.accepted is False
    assert snapshot.skew_ms == 600
    assert snapshot.skew_ms > snapshot.max_skew_ms
    assert snapshot.oldest_quote_age_ms is not None
    assert snapshot.oldest_quote_age_ms < 2000
    assert len(snapshot.retrievals) == 2
    assert snapshot.legs
    assert all(leg.displayed_odds for leg in snapshot.legs)
    assert snapshot.earliest_retrieval_at is not None
    assert snapshot.latest_retrieval_at is not None
    assert snapshot.latest_retrieval_at - snapshot.earliest_retrieval_at == timedelta(
        milliseconds=600
    )


@pytest.mark.asyncio
async def test_in_flight_reprice_does_not_start_a_second_read() -> None:
    started = asyncio.Event()
    release = asyncio.Event()
    calls = 0

    class _Matchbook(FakeMatchbook):
        async def get_market(self, event_id, market_id, **filters):
            nonlocal calls
            del filters
            calls += 1
            self.get_market_calls.append((str(event_id), str(market_id)))
            started.set()
            await release.wait()
            return _fresh(_market())

    class _Kalshi(FakeKalshi):
        async def get_order_book(self, event_id, market_id, outcome_id=None, **filters):
            del event_id, outcome_id, filters
            self.book_calls.append(str(market_id))
            return _book("0.20", "0.70")

    row = _row(
        suffix="dup",
        matchbook_event_id=EVENT,
        matchbook_market_id=MARKET,
        kalshi_event="KXEPLBTTS-RICH",
    )
    engine, _mb, _ks, _layer = _engine([row], matchbook=_Matchbook(), kalshi=_Kalshi())
    runtime = engine.item(row.catalogue_row_id)
    assert runtime is not None
    first = asyncio.create_task(
        engine.reprice_for_paper_entry(
            runtime,
            venues=(VenueName.MATCHBOOK, VenueName.KALSHI),
        )
    )
    await started.wait()
    second = await engine.reprice_for_paper_entry(
        runtime,
        venues=(VenueName.MATCHBOOK, VenueName.KALSHI),
    )
    assert second.duplicate is True
    assert second.reason == EXECUTION_REPRICE_DUPLICATE
    assert second.decision is None
    assert calls == 1
    release.set()
    await first
    assert calls == 1


@pytest.mark.asyncio
async def test_paper_fill_records_price2_prices_depth_and_audit_phases(
    tmp_path, monkeypatch, caplog
) -> None:
    caplog.set_level("INFO", logger="sports_hedge.execution_reprice")
    rich = _market()
    book = _book("0.20", "0.70")
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="snapshot-fill",
        matchbook_payloads=[_fresh(rich), _fresh(rich)],
        kalshi_books=[book, book],
    )
    try:
        trade = bundle.operations.list_active_trades()[0]
        assert len(bundle.operations.list_active_trades()) == 1
        execution = bundle.scan.seen[-1]
        by_outcome = {(leg.venue, leg.outcome): leg for leg in trade.legs}
        for leg in execution.fill_legs:
            recorded = by_outcome[(leg.venue, leg.outcome)]
            assert recorded.displayed_odds == leg.displayed_odds
            assert recorded.source_market_id == leg.source_market_id
        snapshot_events = [
            event
            for event in trade.audit
            if event.event_type is PaperTradeAuditEventType.EXECUTION_SNAPSHOT
        ]
        assert len(snapshot_events) == 1
        payload = json.loads(snapshot_events[0].detail or "{}")
        assert payload["accepted"] is True
        assert payload["skew_ms"] is not None
        assert payload["skew_ms"] <= 500
        assert payload["legs"]
        assert any(Decimal(leg["available_depth"]) > 0 for leg in payload["legs"])
        assert any(leg["displayed_odds"] for leg in payload["legs"])
        text = caplog.text
        for phase in (
            PHASE_DISCOVERY_PRICE,
            PHASE_REPRICE_STARTED,
            PHASE_BOOK_RETRIEVED,
            PHASE_SNAPSHOT_COMPLETE,
            PHASE_PAPER_ELIGIBLE,
            PHASE_FILL_ATTEMPTED,
            PHASE_FILL_COMPLETE,
        ):
            assert f"phase={phase}" in text
        assert bundle.matchbook.get_market_calls == [(EVENT, MARKET), (EVENT, MARKET)]
        assert bundle.kalshi.book_calls == [TICKER, TICKER]
    finally:
        bundle.repository.close()
        bundle.ledger.close()
