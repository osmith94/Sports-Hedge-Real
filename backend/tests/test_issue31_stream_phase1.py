"""Issue #31 STREAM Phase 1: one-fixture public Polymarket market WS shadow.

Deterministic fake WS. No live network, no paper OPEN, no venue orders.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.application.approved_market_catalogue import (
    ApprovedMarketCatalogueRow,
    CatalogueRowState,
    OutcomeNativeId,
)
from sports_hedge.application.provider_access import (
    PRICE_ENGINE_STREAM_LANE,
    ProviderAccessLayer,
    ProviderPriority,
    priority_for_lane,
)
from sports_hedge.application.stream.coalescer import StreamMatchbookCoalescer
from sports_hedge.application.stream.observer import PolymarketMarketObserver
from sports_hedge.application.stream.order_book import StreamOrderBooks
from sports_hedge.application.stream.pin import StreamPinError, streamable_markets
from sports_hedge.application.stream.protocol import (
    EVENT_BEST_BID_ASK,
    EVENT_BOOK,
    EVENT_PRICE_CHANGE,
    PING_TEXT,
)
from sports_hedge.application.stream.runtime import StreamRuntime, reset_stream_runtime
from sports_hedge.application.stream.transport import FakeMarketWsTransport, StreamTransportClosed
from sports_hedge.domain.models import VenueName
from sports_hedge.venues.rate_limit import ProviderRateLimitedError

NOW = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
KICKOFF = NOW + timedelta(hours=6)
TOKEN_HOME = "713856789012345678901234567890111"
TOKEN_DRAW = "713856789012345678901234567890222"
TOKEN_AWAY = "713856789012345678901234567890333"


def _row(**overrides: Any) -> ApprovedMarketCatalogueRow:
    payload = dict(
        catalogue_row_id="amc-stream-1",
        register_canonical_key="MATCH_RESULT_FT",
        canonical_event_id="evt-stream-1",
        competition="Premier League",
        home_canonical="Arsenal",
        away_canonical="Chelsea",
        kickoff_utc=KICKOFF,
        matchbook_event_id="8801",
        matchbook_market_id="41001",
        matchbook_runner_ids=[
            OutcomeNativeId(outcome="home", native_id="1"),
            OutcomeNativeId(outcome="draw", native_id="2"),
            OutcomeNativeId(outcome="away", native_id="3"),
        ],
        polymarket_event_id="pm-evt-1",
        polymarket_market_id="pm-mkt-1",
        polymarket_token_ids=[
            OutcomeNativeId(outcome="home", native_id=TOKEN_HOME),
            OutcomeNativeId(outcome="draw", native_id=TOKEN_DRAW),
            OutcomeNativeId(outcome="away", native_id=TOKEN_AWAY),
        ],
        family="match_result",
        period="full_time",
        required_outcomes=["home", "draw", "away"],
        row_state=CatalogueRowState.ACTIVE,
        first_catalogued_at=NOW,
        last_confirmed_at=NOW,
    )
    payload.update(overrides)
    return ApprovedMarketCatalogueRow(**payload)


def _book(token: str, *, ts: str = "1700000000000", extra: dict[str, Any] | None = None) -> str:
    payload = {
        "event_type": EVENT_BOOK,
        "asset_id": token,
        "market": "0xabc",
        "bids": [{"price": "0.48", "size": "30"}],
        "asks": [{"price": "0.52", "size": "25"}],
        "timestamp": ts,
        "hash": f"hash-{token}-{ts}",
    }
    if extra:
        payload.update(extra)
    return json.dumps(payload)


def _delta(token: str, *, price: str = "0.48", size: str = "10", ts: str = "1700000001000") -> str:
    return json.dumps(
        {
            "event_type": EVENT_PRICE_CHANGE,
            "market": "0xabc",
            "timestamp": ts,
            "price_changes": [
                {
                    "asset_id": token,
                    "price": price,
                    "size": size,
                    "side": "BUY",
                    "best_bid": price,
                    "best_ask": "0.52",
                }
            ],
        }
    )


def _mb_payload() -> dict[str, Any]:
    return {
        "id": 41001,
        "name": "Match Odds",
        "status": "open",
        "market-type": "one_x_two",
        "runners": [
            {"id": 1, "name": "Arsenal", "status": "open", "prices": [{"side": "back", "odds": "2.10", "available-amount": "50"}]},
            {"id": 2, "name": "Draw", "status": "open", "prices": [{"side": "back", "odds": "3.40", "available-amount": "50"}]},
            {"id": 3, "name": "Chelsea", "status": "open", "prices": [{"side": "back", "odds": "3.60", "available-amount": "50"}]},
        ],
    }


async def _wait_until(predicate, *, timeout: float = 2.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met")


def test_stream_priority_is_below_hot_background_and_universe() -> None:
    assert priority_for_lane(PRICE_ENGINE_STREAM_LANE) is ProviderPriority.STREAM
    assert ProviderPriority.STREAM > ProviderPriority.BACKGROUND
    assert ProviderPriority.STREAM > ProviderPriority.HOT
    assert ProviderPriority.STREAM > ProviderPriority.UNIVERSE


def test_pin_requires_exact_polymarket_tokens_and_rejects_fabricated() -> None:
    pin = streamable_markets([_row()])
    assert pin.token_ids == (TOKEN_HOME, TOKEN_DRAW, TOKEN_AWAY)
    with pytest.raises(StreamPinError) as missing:
        streamable_markets(
            [_row(polymarket_token_ids=[], polymarket_market_id="pm-mkt-1")]
        )
    assert missing.value.reason == "missing_polymarket_native_ids"
    with pytest.raises(StreamPinError) as fabricated:
        streamable_markets(
            [
                _row(
                    polymarket_token_ids=[
                        OutcomeNativeId(outcome="home", native_id="pm-mkt-1:0"),
                        OutcomeNativeId(outcome="draw", native_id="pm-mkt-1:1"),
                        OutcomeNativeId(outcome="away", native_id="pm-mkt-1:2"),
                    ]
                )
            ]
        )
    assert fabricated.value.reason == "missing_polymarket_native_ids"


def test_book_snapshot_then_delta_unknown_token_and_bbo_ignored() -> None:
    books = StreamOrderBooks([TOKEN_HOME])
    assert books.apply_message(json.loads(_delta(TOKEN_HOME))) == "delta_rejected"
    assert books.books[TOKEN_HOME].healthy is False
    assert books.apply_message(json.loads(_book(TOKEN_HOME))) == "snapshot"
    assert books.books[TOKEN_HOME].healthy is True
    assert books.apply_message(json.loads(_delta(TOKEN_HOME, size="12"))) == "delta"
    assert books.books[TOKEN_HOME].bids["0.48"] == "12"
    assert books.apply_message(json.loads(_delta(TOKEN_HOME, size="0"))) == "delta"
    assert "0.48" not in books.books[TOKEN_HOME].bids
    unknown = books.apply_message(json.loads(_book("not-subscribed")))
    assert unknown == "unknown_token"
    bbo = books.apply_message(
        {
            "event_type": EVENT_BEST_BID_ASK,
            "asset_id": TOKEN_HOME,
            "best_bid": "0.40",
            "best_ask": "0.60",
        }
    )
    assert bbo == "ignored_best_bid_ask"
    assert books.ignored_bbo_count == 1
    assert books.books[TOKEN_HOME].asks["0.52"] == "25"


def test_out_of_order_delta_resyncs_and_does_not_keep_stale_depth() -> None:
    books = StreamOrderBooks([TOKEN_HOME])
    books.apply_message(json.loads(_book(TOKEN_HOME, ts="1700000005000")))
    result = books.apply_message(json.loads(_delta(TOKEN_HOME, ts="1700000001000")))
    assert result == "delta_rejected"
    assert books.books[TOKEN_HOME].healthy is False
    assert books.books[TOKEN_HOME].bids == {}


@pytest.mark.asyncio
async def test_fake_ws_subscribe_unsubscribe_heartbeat_and_reconnect() -> None:
    transport = FakeMarketWsTransport()
    observer = PolymarketMarketObserver(
        [TOKEN_HOME],
        transport_factory=lambda: transport,
        ping_interval=0.05,
        pong_timeout=2.0,
        stale_after=30.0,
        jitter=lambda: 0.0,
    )
    observer.start()
    await _wait_until(lambda: transport.connect_count == 1 and any("assets_ids" in item for item in transport.sent))
    subscribe = json.loads(next(item for item in transport.sent if item.startswith("{")))
    assert subscribe["type"] == "market"
    assert subscribe["assets_ids"] == [TOKEN_HOME]
    transport.push(_book(TOKEN_HOME))
    await _wait_until(lambda: observer.status == "subscribed")
    await _wait_until(lambda: PING_TEXT in transport.sent)
    transport.fail(StreamTransportClosed("peer_closed"))
    await _wait_until(lambda: observer.reconnect_count >= 1)
    await _wait_until(lambda: transport.connect_count >= 2)
    assert observer.books.books[TOKEN_HOME].healthy is False
    transport.push(_book(TOKEN_HOME, ts="1700000090000"))
    await _wait_until(lambda: observer.status == "subscribed" and observer.books.books[TOKEN_HOME].healthy)
    await observer.stop()
    assert any("unsubscribe" in item for item in transport.sent)
    assert observer.status == "disabled"


@pytest.mark.asyncio
async def test_coalesce_burst_rate_limit_and_disabled_skips_provider() -> None:
    calls: list[tuple[str, str]] = []

    async def fetch(event_id: str, market_id: str) -> dict[str, Any]:
        calls.append((event_id, market_id))
        await asyncio.sleep(0.05)
        return _mb_payload()

    access = ProviderAccessLayer(limits={VenueName.MATCHBOOK: 4})
    coalescer = StreamMatchbookCoalescer(
        fetch_market=fetch, provider_access=access, enabled=True, slot_wait_seconds=1.0
    )
    coalescer.start()
    for _ in range(20):
        coalescer.schedule([("8801", "41001")])
    await _wait_until(lambda: coalescer.stats.request_count >= 1)
    await _wait_until(lambda: coalescer.stats.coalesced_count >= 1)
    assert coalescer.stats.request_count < 20
    await coalescer.stop()

    limited_calls = 0

    async def limited(event_id: str, market_id: str) -> dict[str, Any]:
        nonlocal limited_calls
        limited_calls += 1
        raise ProviderRateLimitedError(1.0, provider="matchbook")

    rate = StreamMatchbookCoalescer(
        fetch_market=limited, provider_access=access, enabled=True, slot_wait_seconds=0.2
    )
    rate.start()
    rate.schedule([("8801", "41001")])
    await _wait_until(lambda: rate.stats.rate_limited_count >= 1)
    await rate.stop()

    idle_calls = 0

    async def forbidden(event_id: str, market_id: str) -> dict[str, Any]:
        nonlocal idle_calls
        idle_calls += 1
        return _mb_payload()

    disabled = StreamMatchbookCoalescer(fetch_market=forbidden, provider_access=access, enabled=False)
    disabled.start()
    disabled.schedule([("8801", "41001")])
    await asyncio.sleep(0.05)
    await disabled.stop()
    assert idle_calls == 0


@pytest.mark.asyncio
async def test_stream_does_not_preempt_hot_or_active_trade() -> None:
    layer = ProviderAccessLayer(limits={VenueName.MATCHBOOK: 1})
    hot_got = asyncio.Event()

    async def occupy_then_hot() -> None:
        async with layer.acquire(VenueName.MATCHBOOK, lane="hot", stage="hot"):
            await asyncio.sleep(0.05)

    async def stream_waiter() -> None:
        async with layer.acquire(VenueName.MATCHBOOK, lane=PRICE_ENGINE_STREAM_LANE, stage="stream"):
            return

    occupy = asyncio.create_task(occupy_then_hot())
    await asyncio.sleep(0.01)
    stream = asyncio.create_task(stream_waiter())
    await asyncio.sleep(0.01)

    async def hot_second() -> None:
        async with layer.acquire(VenueName.MATCHBOOK, lane="hot", stage="hot"):
            hot_got.set()

    hot = asyncio.create_task(hot_second())
    await asyncio.wait_for(hot_got.wait(), timeout=1.0)
    assert not stream.done()
    await occupy
    await hot
    stream.cancel()
    with pytest.raises(asyncio.CancelledError):
        await stream


@pytest.mark.asyncio
async def test_runtime_pause_stop_fixture_change_and_no_paper_or_orders() -> None:
    transport = FakeMarketWsTransport()
    fetches: list[tuple[str, str]] = []

    async def fetch(event_id: str, market_id: str) -> dict[str, Any]:
        fetches.append((event_id, market_id))
        return _mb_payload()

    runtime = StreamRuntime(
        catalogue_loader=lambda _event: [_row()],
        matchbook_fetch=fetch,
        transport_factory=lambda: transport,
        provider_access=ProviderAccessLayer(limits={VenueName.MATCHBOOK: 4}),
    )
    idle = runtime.status()
    assert idle.connection_status == "not_selected"
    assert idle.enabled is False
    assert idle.paper_opened is False
    assert idle.orders_placed is False
    assert idle.diagnostic_reads_fetch_providers is False

    selected = await runtime.select_fixture("evt-stream-1")
    assert selected.canonical_event_id == "evt-stream-1"
    assert selected.token_id_count == 3
    await _wait_until(lambda: transport.connect_count == 1)
    for token in (TOKEN_HOME, TOKEN_DRAW, TOKEN_AWAY):
        transport.push(_book(token))
    await _wait_until(lambda: runtime.observer is not None and runtime.observer.status == "subscribed")
    await _wait_until(lambda: runtime.coalescer.stats.request_count >= 1)

    paused = await runtime.pause()
    assert paused.connection_status == "paused"
    fetch_count = len(fetches)
    transport.push(_delta(TOKEN_HOME, size="9"))
    await asyncio.sleep(0.05)
    assert len(fetches) == fetch_count

    resumed = await runtime.resume()
    assert resumed.enabled is True
    await runtime.remove()
    assert runtime.status().connection_status == "not_selected"
    assert runtime.paper_opened is False
    assert runtime.orders_placed is False
    assert "place_order" not in json.dumps(runtime.status().as_public_dict())


@pytest.mark.asyncio
async def test_ws_burst_does_not_block_hot_liveness_counter() -> None:
    ticks = 0

    async def hot_pulse() -> None:
        nonlocal ticks
        for _ in range(40):
            ticks += 1
            await asyncio.sleep(0)

    async def fetch(event_id: str, market_id: str) -> dict[str, Any]:
        return _mb_payload()

    transport = FakeMarketWsTransport()
    runtime = StreamRuntime(
        catalogue_loader=lambda _event: [_row()],
        matchbook_fetch=fetch,
        transport_factory=lambda: transport,
        provider_access=ProviderAccessLayer(limits={VenueName.MATCHBOOK: 4}),
    )
    await runtime.select_fixture("evt-stream-1")
    pulse = asyncio.create_task(hot_pulse())
    for _ in range(30):
        transport.push(_book(TOKEN_HOME, ts=str(1700000000000 + _)))
        await asyncio.sleep(0)
    await pulse
    assert ticks == 40
    await runtime.shutdown()


def test_status_api_does_not_start_stream_or_place_orders() -> None:
    reset_stream_runtime(
        StreamRuntime(
            catalogue_loader=lambda _event: [_row()],
            matchbook_fetch=_never_fetch,
            transport_factory=FakeMarketWsTransport,
        )
    )
    client = TestClient(app)
    payload = client.get("/stream/status").json()
    assert payload["connection_status"] == "not_selected"
    assert payload["enabled"] is False
    assert payload["paper_opened"] is False
    assert payload["orders_placed"] is False
    assert payload["diagnostic_reads_fetch_providers"] is False
    assert payload["phase"] == "phase_1_shadow_only"


async def _never_fetch(event_id: str, market_id: str) -> dict[str, Any]:
    raise AssertionError(f"provider fetch while disabled: {event_id}/{market_id}")
