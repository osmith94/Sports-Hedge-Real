"""Wave 37: STREAM populated books → coalesced MB → existing Price-2 opening.

Fake transports only. Execution stays disarmed. Empty WS [] is not a pass.
"""

from __future__ import annotations

import asyncio
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

from sports_hedge.application.execution_reprice import ExecutionCaptureResult
from sports_hedge.application.provider_access import ProviderAccessLayer
from sports_hedge.application.stream.handoff import StreamPrice2Handoff
from sports_hedge.application.stream.runtime import StreamRuntime
from sports_hedge.application.stream.shadow import StreamCandidate
from sports_hedge.application.stream.transport import FakeMarketWsTransport
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.execution.package import execution_armed
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.models import PaperScanDecision

NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)


def _decision() -> PaperScanDecision:
    return PaperScanDecision(
        canonical_market_id="mkt-stream",
        canonical_event_id="evt-stream-1",
        eligible_for_paper_simulation=True,
        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=["fixture"]),
        solver_model="simple_complete_set",
        minimum_net_edge=Decimal("0.01"),
        scanned_at=NOW,
    )


@pytest.mark.asyncio
async def test_handoff_uses_existing_capture_and_does_not_place_orders(monkeypatch) -> None:
    monkeypatch.setattr(
        "sports_hedge.application.stream.handoff.execution_reprice_permitted",
        lambda _decision: True,
    )
    calls: list[str] = []

    async def capture(decision, runtime):
        calls.append(runtime.identity.catalogue_row_id)
        assert decision.canonical_event_id == "evt-stream-1"
        return ExecutionCaptureResult(
            entry_decision=decision,
            snapshot=SimpleNamespace(accepted=True, snapshot_id="exec:stream:1"),
            price2_accepted=True,
            paper_opened=True,
            orders_placed=False,
        )

    handoff = StreamPrice2Handoff(capture=capture)
    pin = __import__(
        "sports_hedge.application.stream.pin", fromlist=["streamable_markets"]
    ).streamable_markets([_row()])
    await handoff.maybe_open(
        pin=pin,
        candidates=[
            StreamCandidate(
                catalogue_row_id="amc-stream-1",
                register_canonical_key="MATCH_RESULT_FT",
                trustworthy=True,
                net_edge="0.04",
                rejection_reasons=(),
                stream_quote_at=NOW.isoformat(),
                matchbook_quote_at=NOW.isoformat(),
                pair_age_ms=10,
                skew_ms=5,
            )
        ],
        decisions={"amc-stream-1": _decision()},
    )
    assert calls == ["amc-stream-1"]
    assert handoff.paper_opened is True
    assert handoff.orders_placed is False
    assert execution_armed(Settings()) is False


@pytest.mark.asyncio
async def test_empty_ws_array_does_not_count_as_populated_price2() -> None:
    transport = FakeMarketWsTransport()
    fetches: list[tuple[str, str]] = []

    async def fetch(event_id: str, market_id: str) -> dict[str, Any]:
        fetches.append((event_id, market_id))
        return _mb_payload()

    capture_calls = 0

    async def capture(decision, runtime):
        nonlocal capture_calls
        capture_calls += 1
        del decision, runtime
        raise AssertionError("empty WS must not hand off to Price-2")

    runtime = StreamRuntime(
        catalogue_loader=lambda _event: [_row()],
        matchbook_fetch=fetch,
        transport_factory=lambda: transport,
        provider_access=ProviderAccessLayer(limits={VenueName.MATCHBOOK: 4}),
        periodic_seconds=60.0,
        price2_handoff=StreamPrice2Handoff(capture=capture),
    )
    await runtime.select_fixture("evt-stream-1")
    await _wait_until(lambda: transport.connect_count == 1)
    transport.push(json.dumps([]))
    await asyncio.sleep(0.05)
    status = runtime.status()
    assert status.populated_pm_book_count == 0
    assert capture_calls == 0
    assert status.price2_attempt_count == 0
    assert runtime.orders_placed is False
    await runtime.shutdown()


@pytest.mark.asyncio
async def test_populated_books_and_coalesced_mb_schedule_existing_price2(monkeypatch) -> None:
    monkeypatch.setattr(
        "sports_hedge.application.stream.handoff.execution_reprice_permitted",
        lambda _decision: True,
    )
    transport = FakeMarketWsTransport()
    fetches: list[tuple[str, str]] = []

    async def fetch(event_id: str, market_id: str) -> dict[str, Any]:
        fetches.append((event_id, market_id))
        return _mb_payload()

    calls: list[str] = []

    async def capture(decision, runtime):
        calls.append(runtime.identity.catalogue_row_id)
        return ExecutionCaptureResult(
            entry_decision=decision,
            snapshot=SimpleNamespace(accepted=True, snapshot_id="exec:stream:2"),
            price2_accepted=True,
            paper_opened=True,
            orders_placed=False,
        )

    runtime = await _pin_runtime(transport, fetch)
    runtime.handoff.bind_capture(capture)

    def _compare(pin, books, quotes, *, now=None):
        del now
        from sports_hedge.application.stream.shadow import StreamCandidate as Candidate

        candidate = Candidate(
            catalogue_row_id="amc-stream-1",
            register_canonical_key="MATCH_RESULT_FT",
            trustworthy=True,
            net_edge="0.04",
            rejection_reasons=(),
            stream_quote_at=datetime.now(UTC).isoformat(),
            matchbook_quote_at=datetime.now(UTC).isoformat(),
            pair_age_ms=20,
            skew_ms=5,
        )
        runtime.shadow.candidates = [candidate]
        runtime.shadow.decisions = {"amc-stream-1": _decision()}
        return [candidate]

    runtime.shadow.compare = _compare  # type: ignore[method-assign]
    transport.push(
        json.dumps(
            [
                json.loads(_book(TOKEN_HOME)),
                json.loads(_book(TOKEN_DRAW)),
                json.loads(_book(TOKEN_AWAY)),
            ]
        )
    )
    await _wait_until(lambda: runtime.coalescer.stats.request_count >= 1)
    await _wait_until(lambda: runtime.handoff.attempt_count >= 1)
    status = runtime.status()
    assert status.populated_pm_book_count == 3
    assert status.coalesced_matchbook_quote_count >= 1
    assert status.price2_attempt_count >= 1
    assert calls == ["amc-stream-1"]
    assert status.paper_opened is True
    assert status.orders_placed is False
    assert status.phase == "wave37_stream_price2"
    await runtime.shutdown()


def _stream_pin():
    return __import__(
        "sports_hedge.application.stream.pin", fromlist=["streamable_markets"]
    ).streamable_markets([_row()])


async def _open_one(handoff: StreamPrice2Handoff, capture) -> StreamPrice2Handoff:
    handoff.bind_capture(capture)
    await handoff.maybe_open(
        pin=_stream_pin(),
        candidates=[
            StreamCandidate(
                catalogue_row_id="amc-stream-1",
                register_canonical_key="MATCH_RESULT_FT",
                trustworthy=True,
                net_edge="0.04",
                rejection_reasons=(),
                stream_quote_at=NOW.isoformat(),
                matchbook_quote_at=NOW.isoformat(),
                pair_age_ms=10,
                skew_ms=5,
            )
        ],
        decisions={"amc-stream-1": _decision()},
    )
    return handoff


@pytest.mark.asyncio
async def test_eligible_without_persisted_fill_is_not_paper_opened(monkeypatch) -> None:
    monkeypatch.setattr(
        "sports_hedge.application.stream.handoff.execution_reprice_permitted",
        lambda _decision: True,
    )

    async def capture(decision, runtime):
        del runtime
        assert decision.eligible_for_paper_simulation is True
        return ExecutionCaptureResult(
            entry_decision=decision,
            snapshot=SimpleNamespace(accepted=True, snapshot_id="exec:eligible-no-fill"),
            price2_accepted=True,
            paper_opened=False,
            orders_placed=False,
        )

    handoff = await _open_one(StreamPrice2Handoff(), capture)
    assert handoff.accepted_count == 1
    assert handoff.paper_opened is False
    assert "amc-stream-1" in handoff.accepted_rows
    assert handoff.paper_opened_rows == set()
    assert handoff.orders_placed is False
    assert execution_armed(Settings()) is False


@pytest.mark.asyncio
async def test_armed_flag_without_submit_is_not_orders_placed(monkeypatch) -> None:
    monkeypatch.setattr(
        "sports_hedge.application.stream.handoff.execution_reprice_permitted",
        lambda _decision: True,
    )
    monkeypatch.setattr(
        "sports_hedge.execution.package.execution_armed",
        lambda _settings: True,
    )

    async def capture(decision, runtime):
        del runtime
        return ExecutionCaptureResult(
            entry_decision=decision,
            snapshot=SimpleNamespace(accepted=True, snapshot_id="exec:armed-no-order"),
            price2_accepted=True,
            paper_opened=True,
            orders_placed=False,
            orders_submitted=0,
        )

    handoff = await _open_one(StreamPrice2Handoff(), capture)
    assert handoff.accepted_count == 1
    assert handoff.paper_opened is True
    assert handoff.orders_placed is False
    assert execution_armed(Settings()) is False
