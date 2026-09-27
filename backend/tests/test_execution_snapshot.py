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
    _stale,
)
from test_issue316_catalogue_registry import _costs, _fx
from test_issue344_price_engine import NOW, FakeKalshi, FakeMatchbook, _engine, _row

from sports_hedge.application.execution_reprice import (
    EXECUTION_REPRICE_DUPLICATE,
    EXECUTION_REPRICE_FAILED,
    EXECUTION_REPRICE_SKEW,
    EXECUTION_REPRICE_STALE,
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
    ExecutionRetrieval,
    execution_max_snapshot_skew_ms,
    leg_quotes_from_decision,
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


def test_explicit_zero_snapshot_skew_stays_zero() -> None:
    zero = Settings(paper_execution_max_snapshot_skew_ms=0)
    assert zero.paper_execution_max_snapshot_skew_ms == 0
    assert execution_max_snapshot_skew_ms(zero) == 0
    strict = Settings(paper_execution_max_snapshot_skew_ms=100)
    assert execution_max_snapshot_skew_ms(strict) == 100

    class _StricterFreshness:
        paper_execution_max_snapshot_skew_ms = 500
        paper_entry_max_quote_age_ms = 0

    assert execution_max_snapshot_skew_ms(_StricterFreshness()) == 0


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
    by_venue = {item.venue: item for item in snapshot.retrievals}
    for leg in snapshot.legs:
        matched = by_venue[leg.venue]
        assert leg.retrieved_at == matched.retrieved_at
        assert leg.retrieval_native_id == matched.native_id
    assert {leg.retrieved_at for leg in snapshot.legs} == {
        by_venue["matchbook"].retrieved_at,
        by_venue["kalshi"].retrieved_at,
    }
    payload = json.loads(snapshot.to_json())
    assert payload["venue_costs"]
    assert any(item["source"] for item in payload["venue_costs"])
    assert any(item["captured_at"] for item in payload["venue_costs"])
    assert any(
        item["currency"] == "USD" and item["source"] == "test_fx" for item in payload["fx_rates"]
    )
    assert payload["minimum_net_edge"] is not None
    assert payload["timing"]["assembly_ms"] >= 0
    assert payload["accepted"] is False
    assert payload["rejection_reason"] == EXECUTION_REPRICE_SKEW


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
        assert payload["minimum_net_edge"] is not None
        assert payload["capital_constraint"] is not None
        assert payload["capital_constraint"]["paper_only"] is True
        assert payload["venue_costs"]
        assert any(
            item["fee_basis"] and item["source"] and item["captured_at"]
            for item in payload["venue_costs"]
        )
        assert any(
            item["currency"] == "USD" and item["gbp_per_unit"] for item in payload["fx_rates"]
        )
        for leg in payload["legs"]:
            assert leg["retrieval_native_id"]
            assert leg["retrieved_at"]
        audits = bundle.watchlist.repository.list_execution_snapshot_audits(trade.opportunity_id)
        audits = sorted(audits, key=lambda row: int(row["execution_cycle"] or 0))
        assert [row["accepted"] for row in audits] == [1, 0]
        assert audits[0]["cycle_outcome"] == "filled"
        assert audits[0]["tranche_id"] == "opening"
        assert audits[0]["trade_id"] == trade.trade_id
        assert audits[1]["cycle_outcome"] == "rejected"
        assert json.loads(audits[0]["snapshot_json"])["snapshot_id"] == payload["snapshot_id"]
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
        assert bundle.matchbook.get_market_calls == [
            (EVENT, MARKET),
            (EVENT, MARKET),
            (EVENT, MARKET),
        ]
        assert bundle.kalshi.book_calls == [TICKER, TICKER, TICKER]
    finally:
        bundle.repository.close()
        bundle.ledger.close()
        bundle.watchlist.repository.close()


class _Venue:
    def __init__(self, value: str) -> None:
        self.value = value


class _Level:
    def __init__(self, odds: str, stake: str) -> None:
        self.decimal_odds = Decimal(odds)
        self.available_stake = Decimal(stake)


class _Leg:
    def __init__(self, venue: str, outcome: str, market_id: str, runner_id: str) -> None:
        self.venue = _Venue(venue)
        self.outcome = outcome
        self.source_market_id = market_id
        self.source_runner_id = runner_id
        self.displayed_odds = Decimal("2.5")
        self.requested_stake = Decimal("10")
        self.quote_age_ms = 20
        self.levels = [_Level("2.5", "40")]


def test_each_leg_keeps_its_own_native_book_timestamp() -> None:
    home_at = NOW
    draw_at = NOW + timedelta(milliseconds=40)
    away_at = NOW + timedelta(milliseconds=80)
    token_at = NOW + timedelta(milliseconds=15)
    retrievals = (
        ExecutionRetrieval("kalshi", "EVT-HOME", "EVT", home_at),
        ExecutionRetrieval("kalshi", "EVT-DRAW", "EVT", draw_at),
        ExecutionRetrieval("kalshi", "EVT-AWAY", "EVT", away_at),
        ExecutionRetrieval("polymarket", "token-away", "pm", token_at),
    )
    decision = type(
        "Decision",
        (),
        {
            "fill_legs": [
                _Leg("kalshi", "home", "EVT-HOME", "EVT-HOME:yes"),
                _Leg("kalshi", "draw", "EVT", "EVT-DRAW:yes"),
                _Leg("kalshi", "away", "EVT-AWAY", "EVT-AWAY:yes"),
                _Leg("polymarket", "away", "4521504", "token-away"),
            ]
        },
    )()
    quotes = leg_quotes_from_decision(decision, retrievals=retrievals)
    by_outcome = {(quote.venue, quote.outcome): quote for quote in quotes}
    assert by_outcome[("kalshi", "home")].retrieved_at == home_at
    assert by_outcome[("kalshi", "draw")].retrieved_at == draw_at
    assert by_outcome[("kalshi", "away")].retrieved_at == away_at
    assert by_outcome[("polymarket", "away")].retrieved_at == token_at
    assert by_outcome[("kalshi", "draw")].retrieval_native_id == "EVT-DRAW"
    assert len({quote.retrieved_at for quote in quotes if quote.venue == "kalshi"}) == 3


@pytest.mark.asyncio
async def test_rejected_price2_snapshot_is_durable_audit_evidence(tmp_path, monkeypatch) -> None:
    rich = _market()
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="rejected-snapshot",
        matchbook_payloads=[_stale(rich), _stale(rich)],
        kalshi_books=[_book("0.20", "0.70"), _book("0.20", "0.70")],
    )
    try:
        assert bundle.operations.list_active_trades() == []
        rows = bundle.watchlist.repository.list_opportunities()
        assert len(rows) == 1
        audits = bundle.watchlist.repository.list_execution_snapshot_audits(rows[0].opportunity_id)
        assert len(audits) == 1
        record = audits[0]
        assert record["accepted"] == 0
        assert record["rejection_reason"] == EXECUTION_REPRICE_STALE
        payload = json.loads(record["snapshot_json"])
        assert payload["accepted"] is False
        assert payload["rejection_reason"] == EXECUTION_REPRICE_STALE
        assert payload["retrievals"]
        assert {item["native_id"] for item in payload["retrievals"]} >= {MARKET, TICKER}
        assert payload["legs"]
        assert any(leg["displayed_odds"] for leg in payload["legs"])
        assert payload["net_edge"] is not None
        assert payload["timing"]["assembly_ms"] >= 0
        assert payload["timing"]["calls"]
        diagnostics = json.loads(record["diagnostics_json"])
        assert diagnostics["assembly_ms"] >= 0
        assert "quote_age_ms" in diagnostics
        missed = [
            event
            for event in bundle.watchlist.activity(opportunity_id=rows[0].opportunity_id, limit=20)
            if str(event.detail or "").startswith(EXECUTION_REPRICE_STALE)
        ]
        assert missed
        assert "snapshot_json" not in (missed[0].detail or "")
    finally:
        bundle.repository.close()
        bundle.ledger.close()
        bundle.watchlist.repository.close()


@pytest.mark.asyncio
async def test_provider_failure_persists_partial_snapshot(tmp_path, monkeypatch) -> None:
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="partial-snapshot",
        matchbook_payloads=[_stale(_market()), _fresh(_market())],
        kalshi_books=[_book("0.20", "0.70"), None],
    )
    try:
        assert bundle.operations.list_active_trades() == []
        rows = bundle.watchlist.repository.list_opportunities()
        audits = bundle.watchlist.repository.list_execution_snapshot_audits(rows[0].opportunity_id)
        assert len(audits) == 1
        payload = json.loads(audits[0]["snapshot_json"])
        assert payload["accepted"] is False
        assert payload["rejection_reason"] == EXECUTION_REPRICE_FAILED
        venues = {item["venue"] for item in payload["retrievals"]}
        assert "matchbook" in venues
        assert audits[0]["diagnostics_json"]
    finally:
        bundle.repository.close()
        bundle.ledger.close()
        bundle.watchlist.repository.close()
