"""Exact provider-native ID extraction. Missing IDs fail closed.

Never synthesize Kalshi tickers, Matchbook ids, Polymarket market ids, or
CLOB token IDs. Placeholder / demo tokens are rejected.
"""

from __future__ import annotations

import json
from typing import Any

from sports_hedge.domain.models import VenueName
from sports_hedge.domain.outrights import OutrightListingShape, OutrightNativeListing

PLACEHOLDER_CLOB_TOKENS = frozenset(
    {
        "y",
        "n",
        "h",
        "d",
        "a",
        "o",
        "u",
        "yes",
        "no",
        "yes-token",
        "no-token",
        "todo",
        "placeholder",
        "synthetic",
        "fake",
    }
)


class MissingNativeIdError(ValueError):
    """Required venue-native identifier is absent or placeholder."""


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _require(value: Any, *, reason: str) -> str:
    text = _text(value)
    if not text:
        raise MissingNativeIdError(reason)
    return text


def parse_clob_token_ids(raw: Any) -> tuple[str, str]:
    """Require both real CLOB token IDs. Never invent them."""

    tokens: Any = raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            raise MissingNativeIdError("missing_polymarket_clob_token_ids")
        try:
            tokens = json.loads(text)
        except json.JSONDecodeError as exc:
            raise MissingNativeIdError("invalid_polymarket_clob_token_ids") from exc
    if not isinstance(tokens, (list, tuple)) or len(tokens) != 2:
        raise MissingNativeIdError("missing_polymarket_clob_token_ids")
    yes_token = _text(tokens[0])
    no_token = _text(tokens[1])
    if not yes_token or not no_token:
        raise MissingNativeIdError("missing_polymarket_clob_token_ids")
    for token in (yes_token, no_token):
        lowered = token.casefold()
        if lowered in PLACEHOLDER_CLOB_TOKENS:
            raise MissingNativeIdError("placeholder_polymarket_clob_token_ids")
        if not token.isdigit():
            raise MissingNativeIdError("invalid_polymarket_clob_token_ids")
    if yes_token == no_token:
        raise MissingNativeIdError("duplicate_polymarket_clob_token_ids")
    return yes_token, no_token


def extract_kalshi_listing(payload: dict[str, Any]) -> OutrightNativeListing:
    market = payload.get("market") if isinstance(payload.get("market"), dict) else payload
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    event_ticker = _require(
        event.get("event_ticker") or market.get("event_ticker"),
        reason="missing_kalshi_event_ticker",
    )
    market_ticker = _require(market.get("ticker"), reason="missing_kalshi_market_ticker")
    series = _text(event.get("series_ticker") or market.get("series_ticker") or payload.get("series_ticker"))
    return OutrightNativeListing(
        venue=VenueName.KALSHI.value,
        native_event_id=event_ticker,
        native_market_id=market_ticker,
        native_runner_or_contract_id=market_ticker,
        listing_shape=OutrightListingShape.BINARY_YES_NO,
        kalshi_series_ticker=series or None,
    )


def extract_matchbook_listing(payload: dict[str, Any]) -> OutrightNativeListing:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    market = payload.get("market") if isinstance(payload.get("market"), dict) else payload
    runner = payload.get("runner") if isinstance(payload.get("runner"), dict) else {}
    event_id = _require(event.get("id") or payload.get("event_id"), reason="missing_matchbook_event_id")
    market_id = _require(market.get("id") or payload.get("market_id"), reason="missing_matchbook_market_id")
    runner_id = _require(
        runner.get("id") or payload.get("runner_id"),
        reason="missing_matchbook_runner_id",
    )
    participant = _text(
        runner.get("event-participant-id")
        or runner.get("event_participant_id")
        or payload.get("event_participant_id")
    )
    return OutrightNativeListing(
        venue=VenueName.MATCHBOOK.value,
        native_event_id=str(event_id),
        native_market_id=str(market_id),
        native_runner_or_contract_id=str(runner_id),
        listing_shape=OutrightListingShape.EXCHANGE_BACK_LAY,
        matchbook_participant_id=participant or None,
    )


def extract_polymarket_listing(payload: dict[str, Any]) -> OutrightNativeListing:
    event = payload.get("event") if isinstance(payload.get("event"), dict) else payload
    market = payload.get("market") if isinstance(payload.get("market"), dict) else payload
    event_id = _require(event.get("id") or payload.get("event_id"), reason="missing_polymarket_event_id")
    market_id = _require(market.get("id") or payload.get("market_id"), reason="missing_polymarket_market_id")
    tokens = parse_clob_token_ids(
        market.get("clobTokenIds") or market.get("clob_token_ids") or payload.get("clobTokenIds")
    )
    condition = _text(market.get("conditionId") or market.get("condition_id"))
    slug = _text(event.get("slug") or payload.get("slug"))
    ticker = _text(event.get("ticker") or payload.get("ticker"))
    return OutrightNativeListing(
        venue=VenueName.POLYMARKET.value,
        native_event_id=str(event_id),
        native_market_id=str(market_id),
        native_runner_or_contract_id=str(market_id),
        listing_shape=OutrightListingShape.BINARY_YES_NO,
        polymarket_condition_id=condition or None,
        polymarket_clob_token_ids=tokens,
        polymarket_event_slug=slug or None,
        polymarket_event_ticker=ticker or None,
    )


def extract_native_listing(venue: VenueName, payload: dict[str, Any]) -> OutrightNativeListing:
    if venue is VenueName.KALSHI:
        return extract_kalshi_listing(payload)
    if venue is VenueName.MATCHBOOK:
        return extract_matchbook_listing(payload)
    if venue is VenueName.POLYMARKET:
        return extract_polymarket_listing(payload)
    raise MissingNativeIdError(f"unsupported_outright_venue:{venue.value}")
