"""Polymarket public market-channel protocol helpers for STREAM Phase 1.

Official public CLOB market WS only. Never the authenticated user/order channel.
Asset IDs are exact CLOB token IDs, not Gamma event or condition IDs.
"""

from __future__ import annotations

import json
from decimal import Decimal
from typing import Any, Mapping

POLYMARKET_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
MARKET_CHANNEL_TYPE = "market"
SUBSCRIBE_OPERATION = "subscribe"
UNSUBSCRIBE_OPERATION = "unsubscribe"
PING_TEXT = "PING"
PONG_TEXT = "PONG"
EVENT_BOOK = "book"
EVENT_PRICE_CHANGE = "price_change"
EVENT_BEST_BID_ASK = "best_bid_ask"
EVENT_LAST_TRADE_PRICE = "last_trade_price"
EVENT_TICK_SIZE_CHANGE = "tick_size_change"
EVENT_NEW_MARKET = "new_market"
EVENT_MARKET_RESOLVED = "market_resolved"
KNOWN_EVENT_TYPES = frozenset(
    {
        EVENT_BOOK,
        EVENT_PRICE_CHANGE,
        EVENT_BEST_BID_ASK,
        EVENT_LAST_TRADE_PRICE,
        EVENT_TICK_SIZE_CHANGE,
        EVENT_NEW_MARKET,
        EVENT_MARKET_RESOLVED,
    }
)
# Depth may be applied only from a full book snapshot or a sized price_change
# ladder update. best_bid_ask is telemetry, never executable depth.
DEPTH_EVENT_TYPES = frozenset({EVENT_BOOK, EVENT_PRICE_CHANGE})
MAX_STREAM_FIXTURES = 1
MAX_STREAM_TOKENS = 24
PING_INTERVAL_SECONDS = 10.0
PONG_TIMEOUT_SECONDS = 15.0
STALE_AFTER_SECONDS = 15.0
RECONNECT_BACKOFF_START_SECONDS = 0.5
RECONNECT_BACKOFF_MAX_SECONDS = 30.0
STREAM_LANE = "stream"
# Absolute implied-probability points (0.02 = two percentage points), not odds %.
PRICE_MOVE_PROBABILITY_POINTS = Decimal("0.02")
# Ignore sub-threshold probability jitter when deciding Matchbook refresh.
NOISE_PROBABILITY_POINTS = Decimal("0.0005")
# Executable ask-size change (shares) that is economically material.
DEPTH_CHANGE_SHARES = Decimal("50")
MATCHBOOK_CREDIBLE_AGE_SECONDS = 15.0
PERIODIC_RECONCILE_SECONDS = 30.0
RESYNC_MIN_INTERVAL_SECONDS = 2.0
FUTURE_TIMESTAMP_SLACK_MS = 2000
TRIGGER_BASELINE = "baseline"
TRIGGER_PRICE_MOVE = "price_move"
TRIGGER_POTENTIAL_EDGE = "potential_edge"
TRIGGER_DEPTH_CHANGE = "depth_change"
TRIGGER_RECONNECT = "reconnect"
TRIGGER_PERIODIC = "periodic"
TRIGGER_REASONS = frozenset(
    {
        TRIGGER_BASELINE,
        TRIGGER_PRICE_MOVE,
        TRIGGER_POTENTIAL_EDGE,
        TRIGGER_DEPTH_CHANGE,
        TRIGGER_RECONNECT,
        TRIGGER_PERIODIC,
    }
)


def market_subscribe_payload(token_ids: list[str], *, operation: str | None = None) -> dict[str, Any]:
    assets = [str(token).strip() for token in token_ids if str(token).strip()]
    payload: dict[str, Any] = {
        "assets_ids": assets,
        "custom_feature_enabled": True,
    }
    if operation:
        payload["operation"] = operation
    else:
        payload["type"] = MARKET_CHANNEL_TYPE
    return payload


def encode_ws_message(payload: Mapping[str, Any] | str) -> str:
    if isinstance(payload, str):
        return payload
    return json.dumps(payload, separators=(",", ":"))


def parse_ws_message(raw: str) -> dict[str, Any] | list[Any] | str:
    text = str(raw or "")
    if text == PONG_TEXT or text == PING_TEXT:
        return text
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ValueError("stream_ws_json_invalid") from exc
    if isinstance(parsed, dict):
        return parsed
    if isinstance(parsed, list):
        if not all(isinstance(item, dict) for item in parsed):
            raise ValueError("stream_ws_array_item_not_object")
        return parsed
    raise ValueError("stream_ws_payload_not_object")


def iter_ws_events(parsed: dict[str, Any] | list[Any] | str) -> list[dict[str, Any]]:
    if isinstance(parsed, str):
        return []
    if isinstance(parsed, dict):
        return [parsed]
    return [item for item in parsed if isinstance(item, dict)]


def message_event_type(payload: Mapping[str, Any]) -> str:
    return str(payload.get("event_type") or payload.get("type") or "").strip()


def message_timestamp_ms(payload: Mapping[str, Any]) -> int | None:
    raw = payload.get("timestamp")
    if raw is None:
        return None
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if value <= 0:
        return None
    if value < 10_000_000_000:
        return value * 1000
    return value
