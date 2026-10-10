"""Wave 38: STREAM reuses shared fee/FX authority and reaches Price-2.

Fake native books only. Execution stays disarmed. Missing fee/FX fail closed.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace
from typing import Any

import pytest
from test_issue31_stream_phase1 import (
    TOKEN_AWAY,
    TOKEN_DRAW,
    TOKEN_HOME,
    _book,
    _mb_payload,
    _pin_runtime,
    _row,
    _wait_until,
)

from sports_hedge.application.approved_market_catalogue import (
    FEE_SOURCE_GAMMA_MARKET,
    polymarket_fee_snapshot_from_payload,
)
from sports_hedge.application.complete_set import SOLVER_MODEL_SIMPLE
from sports_hedge.application.execution_reprice import ExecutionCaptureResult
from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
    VenueMarketObservation,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.provider_access import ProviderAccessLayer
from sports_hedge.application.stream.coalescer import MatchbookQuote
from sports_hedge.application.stream.handoff import StreamPrice2Handoff
from sports_hedge.application.stream.order_book import StreamOrderBooks
from sports_hedge.application.stream.pin import streamable_markets
from sports_hedge.application.stream.runtime import StreamRuntime
from sports_hedge.application.stream.shadow import StreamCandidate, StreamShadowComparer
from sports_hedge.application.stream.transport import FakeMarketWsTransport
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.execution.package import execution_armed
from sports_hedge.fees.resolver import VenueCostResolver
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.arbitrage.depth import DepthScanResult
from sports_hedge.arbitrage.models import ArbitrageSolution, ArbitrageStake
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision
from test_paper_scan_pipeline import OBSERVED, matchbook_payloads, polymarket_payloads

NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


class _FeeStore:
    def __init__(self, record: Any) -> None:
        self.record = record

    def get_polymarket_fee_snapshot(self, snapshot_id: str) -> Any:
        if snapshot_id != self.record.snapshot_id:
            return None
        return self.record


class _RecordingScan:
    def __init__(self, decision: PaperScanDecision | None = None, inner: PaperScanService | None = None) -> None:
        self.decision = decision
        self.inner = inner
        self.calls: list[dict[str, Any]] = []
        self.settings = Settings(max_slippage_bps=0, fx_spread_bps=0)
        self.fx_service = None if inner is None else inner.fx_service
        self.cost_resolver = None if inner is None else inner.cost_resolver

    def scan_pair(self, left: VenueMarketObservation, right: VenueMarketObservation, **kwargs: Any) -> PaperScanDecision:
        self.calls.append({"left": left, "right": right, "kwargs": kwargs})
        if self.decision is not None:
            return self.decision
        assert self.inner is not None
        return self.inner.scan_pair(left, right, **kwargs)


def _fee_record():
    return polymarket_fee_snapshot_from_payload(
        {"id": "pm-mkt-1", "feesEnabled": False},
        captured_at=NOW,
        source=FEE_SOURCE_GAMMA_MARKET,
        source_market_id="pm-mkt-1",
    )


def _books_and_quotes(*, now: datetime, stale: bool = False) -> tuple[StreamOrderBooks, dict[str, MatchbookQuote]]:
    books = StreamOrderBooks([TOKEN_HOME, TOKEN_DRAW, TOKEN_AWAY])
    ts = str(int(now.timestamp() * 1000) - (20_000 if stale else 80))
    asks = {"asks": [{"price": "0.28", "size": "80"}], "bids": [{"price": "0.24", "size": "40"}]}
    for token in (TOKEN_HOME, TOKEN_DRAW, TOKEN_AWAY):
        books.apply_message(json.loads(_book(token, ts=ts, extra=asks)), received_at=now)
    quotes = {
        "8801:41001": MatchbookQuote(
            event_id="8801",
            market_id="41001",
            payload=_mb_payload(),
            retrieved_at=now,
        )
    }
    return books, quotes


def _qualifying_decision() -> PaperScanDecision:
    return PaperScanDecision(
        canonical_market_id="mkt-stream",
        canonical_event_id="evt-stream-1",
        eligible_for_paper_simulation=True,
        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=["register"]),
        solver_model=SOLVER_MODEL_SIMPLE,
        minimum_net_edge=Decimal("0.002"),
        scanned_at=NOW,
        quote_age_ms=80,
        depth_scan=DepthScanResult(
            solution=ArbitrageSolution(
                is_arbitrage=True,
                implied_probability_sum=Decimal("0.96"),
                total_stake=Decimal("100"),
                guaranteed_return=Decimal("104"),
                guaranteed_profit=Decimal("4"),
                roi=Decimal("0.04"),
                stakes=[
                    ArbitrageStake(
                        outcome="home",
                        venue=VenueName.MATCHBOOK,
                        source_market_id="41001",
                        stake=Decimal("40"),
                        net_decimal_odds=Decimal("2.10"),
                        state_return=Decimal("84"),
                    ),
                    ArbitrageStake(
                        outcome="draw",
                        venue=VenueName.POLYMARKET,
                        source_market_id="pm-mkt-1",
                        stake=Decimal("30"),
                        net_decimal_odds=Decimal("3.40"),
                        state_return=Decimal("102"),
                    ),
                    ArbitrageStake(
                        outcome="away",
                        venue=VenueName.POLYMARKET,
                        source_market_id="pm-mkt-1",
                        stake=Decimal("30"),
                        net_decimal_odds=Decimal("3.50"),
                        state_return=Decimal("105"),
                    ),
                ],
            )
        ),
    )


def _wired_scan() -> PaperScanService:
    return PaperScanService(
        MarketIntelligenceService(SqliteMarketIntelligenceRepository()),
        cost_resolver=VenueCostResolver(),
        settings=Settings(max_slippage_bps=0, fx_spread_bps=0),
        clock=lambda: NOW,
    )


def test_shadow_does_not_put_stream_shadow_into_quote_age_reason() -> None:
    record = _fee_record()
    scan = _RecordingScan(decision=_qualifying_decision())
    comparer = StreamShadowComparer(
        scan,  # type: ignore[arg-type]
        catalogue_store=_FeeStore(record),
        fx_snapshots=[
            FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="ecb_eurofxref", captured_at=NOW)
        ],
    )
    books, quotes = _books_and_quotes(now=NOW)
    row = _row(polymarket_fee_snapshot_id=record.snapshot_id)
    compared = comparer.compare(streamable_markets([row]), books, quotes, now=NOW)
    assert compared[0].economics_state == "qualified"
    assert "stream_shadow_not_price2" not in compared[0].rejection_reasons
    left_meta = scan.calls[0]["left"].metadata
    right_meta = scan.calls[0]["right"].metadata
    assert left_meta.get("quote_age_reason") != "stream_shadow_not_price2"
    assert right_meta.get("quote_age_reason") != "stream_shadow_not_price2"
    assert right_meta.get("stream_data_class") == "stream_shadow_not_price2"
    assert "fx_snapshots" in scan.calls[0]["kwargs"]
    assert right_meta["polymarket_fee"]["fees_enabled"] is False


def test_missing_fx_fails_closed_before_price2() -> None:
    record = _fee_record()
    comparer = StreamShadowComparer(
        _wired_scan(),
        catalogue_store=_FeeStore(record),
        fx_unavailable_reason="missing_fx_rate:USD",
    )
    books, quotes = _books_and_quotes(now=NOW)
    compared = comparer.compare(
        streamable_markets([_row(polymarket_fee_snapshot_id=record.snapshot_id)]),
        books,
        quotes,
        now=NOW,
    )
    assert compared[0].trustworthy is False
    assert compared[0].economics_state == "blocked"
    assert any("missing_fx_rate:USD" in reason for reason in compared[0].rejection_reasons)
    assert "stream_shadow_not_price2" not in compared[0].rejection_reasons


def test_missing_polymarket_fee_fails_closed() -> None:
    comparer = StreamShadowComparer(
        _wired_scan(),
        fx_snapshots=[
            FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="ecb_eurofxref", captured_at=NOW)
        ],
    )
    books, quotes = _books_and_quotes(now=NOW)
    compared = comparer.compare(streamable_markets([_row()]), books, quotes, now=NOW)
    assert compared[0].trustworthy is False
    assert any("unknown_required_venue_cost:polymarket" in reason for reason in compared[0].rejection_reasons)
    assert "stream_shadow_not_price2" not in compared[0].rejection_reasons


def test_stale_stream_book_fails_before_scan_pair() -> None:
    scan = _RecordingScan(decision=_qualifying_decision())
    comparer = StreamShadowComparer(scan)  # type: ignore[arg-type]
    books, quotes = _books_and_quotes(now=NOW, stale=True)
    compared = comparer.compare(streamable_markets([_row()]), books, quotes, now=NOW)
    assert scan.calls == []
    assert compared[0].data_valid is False
    assert "stream_constituent_stale" in compared[0].rejection_reasons


def test_paper_scan_ignores_diagnostic_quote_age_label() -> None:
    service = _wired_scan()
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=OBSERVED, quote_age_ms=120
    )
    polymarket = PolymarketObservationBuilder().build(
        pm_event,
        pm_market,
        pm_books,
        observed_at=OBSERVED,
        quote_age_ms=180,
        quote_age_reason="stream_shadow_not_price2",
        fee_snapshot={"fees_enabled": False, "fee_schedule": None, "source_market_id": "pm-event-1"},
    )
    decision = service.scan_pair(
        matchbook,
        polymarket,
        fx_snapshots=[
            FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="ecb_eurofxref", captured_at=OBSERVED)
        ],
        maximum_execution_risk=100,
    )
    assert "stream_shadow_not_price2" not in decision.rejection_reasons


@pytest.mark.asyncio
async def test_valid_candidate_reaches_price2_with_zero_orders() -> None:
    transport = FakeMarketWsTransport()
    record = _fee_record()
    scan = _RecordingScan(decision=_qualifying_decision())
    comparer = StreamShadowComparer(
        scan,  # type: ignore[arg-type]
        catalogue_store=_FeeStore(record),
        fx_snapshots=[
            FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="ecb_eurofxref", captured_at=NOW)
        ],
    )
    calls: list[str] = []

    async def capture(decision, runtime):
        calls.append(runtime.identity.catalogue_row_id)
        assert decision.eligible_for_paper_simulation is True
        return ExecutionCaptureResult(
            entry_decision=decision,
            snapshot=SimpleNamespace(accepted=True, snapshot_id="exec:stream:38"),
            price2_accepted=True,
            paper_opened=False,
            orders_placed=False,
        )

    async def fetch(event_id: str, market_id: str) -> dict[str, Any]:
        return _mb_payload()

    runtime = StreamRuntime(
        catalogue_loader=lambda _event: [_row(polymarket_fee_snapshot_id=record.snapshot_id)],
        matchbook_fetch=fetch,
        transport_factory=lambda: transport,
        provider_access=ProviderAccessLayer(limits={VenueName.MATCHBOOK: 4}),
        shadow=comparer,
        periodic_seconds=60.0,
        price2_handoff=StreamPrice2Handoff(capture=capture),
    )
    await runtime.select_fixture("evt-stream-1")
    await _wait_until(lambda: transport.connect_count == 1)
    now_ms = str(int(datetime.now(UTC).timestamp() * 1000))
    transport.push(
        json.dumps(
            [
                json.loads(_book(TOKEN_HOME, ts=now_ms, extra={"asks": [{"price": "0.28", "size": "80"}]})),
                json.loads(_book(TOKEN_DRAW, ts=now_ms, extra={"asks": [{"price": "0.28", "size": "80"}]})),
                json.loads(_book(TOKEN_AWAY, ts=now_ms, extra={"asks": [{"price": "0.28", "size": "80"}]})),
            ]
        )
    )
    await _wait_until(lambda: runtime.handoff.attempt_count >= 1, timeout=4.0)
    status = runtime.status()
    assert calls == ["amc-stream-1"]
    assert status.price2_attempt_count >= 1
    assert status.price2_accepted_count >= 1
    assert status.orders_placed is False
    assert runtime.orders_placed is False
    assert execution_armed(Settings()) is False
    assert scan.calls
    assert "stream_shadow_not_price2" not in (scan.calls[0]["right"].metadata.get("quote_age_reason") or "")
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_paused_status_does_not_treat_cached_candidates_as_current() -> None:
    transport = FakeMarketWsTransport()
    runtime = await _pin_runtime(transport, lambda _e, _m: _mb_payload())
    runtime.shadow.candidates = [
        StreamCandidate(
            catalogue_row_id="amc-stream-1",
            register_canonical_key="MATCH_RESULT_FT",
            trustworthy=True,
            net_edge="0.04",
            rejection_reasons=(),
            stream_quote_at=NOW.isoformat(),
            matchbook_quote_at=NOW.isoformat(),
            pair_age_ms=20,
            skew_ms=5,
            data_valid=True,
            economics_state="qualified",
        )
    ]
    await runtime.pause()
    status = runtime.status()
    assert status.connection_status == "paused"
    assert status.books_current is False
    assert status.candidates_current is False
    assert status.valid_pair_count == 0
    assert status.populated_pm_book_count == 0
    assert status.candidates[0].current is False
    assert status.metrics_scope == "session"
    assert status.execution_enabled is False
    await runtime.shutdown()
