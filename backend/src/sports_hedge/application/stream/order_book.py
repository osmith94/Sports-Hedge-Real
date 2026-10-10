"""In-memory Polymarket CLOB ladders. Snapshots then sized deltas only."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from sports_hedge.application.stream.protocol import (
    EVENT_BEST_BID_ASK,
    EVENT_BOOK,
    EVENT_PRICE_CHANGE,
    message_event_type,
    message_timestamp_ms,
)


class StreamBookError(ValueError):
    """Book is not healthy enough to treat as current executable depth."""


def _decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed


def _levels(raw: Any) -> dict[str, str]:
    levels: dict[str, str] = {}
    if not isinstance(raw, list):
        return levels
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        price = _decimal(item.get("price"))
        size = _decimal(item.get("size"))
        if price is None or size is None or price <= 0:
            continue
        key = format(price, "f")
        if size <= 0:
            levels.pop(key, None)
            continue
        levels[key] = format(size, "f")
    return levels


def _apply_change(levels: dict[str, str], price: Any, size: Any) -> None:
    parsed_price = _decimal(price)
    parsed_size = _decimal(size)
    if parsed_price is None or parsed_price <= 0 or parsed_size is None:
        raise StreamBookError("invalid_price_change_level")
    key = format(parsed_price, "f")
    if parsed_size <= 0:
        levels.pop(key, None)
        return
    levels[key] = format(parsed_size, "f")


@dataclass
class TokenLadder:
    token_id: str
    bids: dict[str, str] = field(default_factory=dict)
    asks: dict[str, str] = field(default_factory=dict)
    last_snapshot_ts_ms: int | None = None
    last_delta_ts_ms: int | None = None
    last_applied_ts_ms: int | None = None
    last_applied_wall: datetime | None = None
    last_hash: str | None = None
    healthy: bool = False
    degraded_reason: str | None = None

    def snapshot_payload(self) -> dict[str, Any]:
        return {
            "bids": [{"price": price, "size": size} for price, size in sorted(self.bids.items())],
            "asks": [{"price": price, "size": size} for price, size in sorted(self.asks.items())],
        }

    def mark_unhealthy(self, reason: str) -> None:
        self.healthy = False
        self.degraded_reason = reason
        self.bids.clear()
        self.asks.clear()
        self.last_applied_ts_ms = None
        self.last_snapshot_ts_ms = None
        self.last_delta_ts_ms = None
        self.last_hash = None

    def best_bid(self) -> Decimal | None:
        if not self.healthy or not self.bids:
            return None
        return max(Decimal(price) for price in self.bids)

    def best_ask(self) -> Decimal | None:
        if not self.healthy or not self.asks:
            return None
        return min(Decimal(price) for price in self.asks)

    def implied_probability(self) -> Decimal | None:
        """Taker buy YES uses best ask as implied probability on the binary CLOB."""
        return self.best_ask()

    def executable_ask_size(self) -> Decimal:
        if not self.healthy or not self.asks:
            return Decimal("0")
        total = Decimal("0")
        for size in self.asks.values():
            total += Decimal(size)
        return total


class StreamOrderBooks:
    """Per-token ladders. Missing snapshot / out-of-order events fail closed."""

    def __init__(self, allowed_tokens: list[str]) -> None:
        self.allowed = {str(token).strip() for token in allowed_tokens if str(token).strip()}
        self.books: dict[str, TokenLadder] = {
            token: TokenLadder(token_id=token) for token in self.allowed
        }
        self.unknown_token_count = 0
        self.bad_message_count = 0
        self.ignored_bbo_count = 0
        self.resync_count = 0

    def reset(self) -> None:
        for book in self.books.values():
            book.mark_unhealthy("awaiting_snapshot")
            book.last_applied_wall = None

    def healthy_token_count(self) -> int:
        return sum(1 for book in self.books.values() if book.healthy)

    def apply_message(
        self,
        payload: Mapping[str, Any],
        *,
        received_at: datetime | None = None,
    ) -> str:
        wall = received_at or datetime.now(UTC)
        event = message_event_type(payload)
        if event == EVENT_BEST_BID_ASK:
            self.ignored_bbo_count += 1
            return "ignored_best_bid_ask"
        if event == EVENT_BOOK:
            return self._apply_book(payload, wall)
        if event == EVENT_PRICE_CHANGE:
            return self._apply_price_change(payload, wall)
        return "ignored_event"

    def oldest_applied_wall(self, token_ids: list[str] | tuple[str, ...] | None = None) -> datetime | None:
        selected = list(token_ids) if token_ids is not None else list(self.books)
        walls: list[datetime] = []
        for token in selected:
            book = self.books.get(token)
            if book is None or not book.healthy or book.last_applied_wall is None:
                return None
            walls.append(book.last_applied_wall)
        if not walls:
            return None
        return min(walls)

    def oldest_applied_ts_ms(self, token_ids: list[str] | tuple[str, ...] | None = None) -> int | None:
        selected = list(token_ids) if token_ids is not None else list(self.books)
        stamps: list[int] = []
        for token in selected:
            book = self.books.get(token)
            if book is None or not book.healthy or book.last_applied_ts_ms is None:
                return None
            stamps.append(book.last_applied_ts_ms)
        if not stamps:
            return None
        return min(stamps)

    def _book_for(self, token_id: str) -> TokenLadder | None:
        token = str(token_id or "").strip()
        if not token:
            return None
        if token not in self.allowed:
            self.unknown_token_count += 1
            return None
        return self.books[token]

    def _reject_order(self, book: TokenLadder, reason: str) -> str:
        book.mark_unhealthy(reason)
        self.resync_count += 1
        self.bad_message_count += 1
        return "out_of_order"

    def _accept(self, book: TokenLadder, ts: int, wall: datetime, *, snapshot: bool, digest: str | None = None) -> None:
        book.last_applied_ts_ms = ts
        book.last_applied_wall = wall
        if snapshot:
            book.last_snapshot_ts_ms = ts
            if digest is not None:
                book.last_hash = digest
        else:
            book.last_delta_ts_ms = ts
        book.healthy = True
        book.degraded_reason = None

    def _apply_book(self, payload: Mapping[str, Any], wall: datetime) -> str:
        token = str(payload.get("asset_id") or "").strip()
        book = self._book_for(token)
        if book is None:
            self.bad_message_count += 1
            return "unknown_token"
        bids = payload.get("bids")
        asks = payload.get("asks")
        if not isinstance(bids, list) or not isinstance(asks, list):
            self.bad_message_count += 1
            book.mark_unhealthy("incomplete_book_snapshot")
            self.resync_count += 1
            return "incomplete_snapshot"
        ts = message_timestamp_ms(payload)
        if ts is None:
            return self._reject_order(book, "missing_event_timestamp")
        digest = str(payload.get("hash") or "").strip() or None
        if (
            digest
            and digest == book.last_hash
            and book.healthy
            and book.last_applied_ts_ms == ts
        ):
            book.last_applied_wall = wall
            return "duplicate_snapshot"
        if book.last_applied_ts_ms is not None and ts < book.last_applied_ts_ms:
            return self._reject_order(book, "out_of_order_snapshot")
        book.bids = _levels(bids)
        book.asks = _levels(asks)
        self._accept(book, ts, wall, snapshot=True, digest=digest)
        return "snapshot"

    def _apply_price_change(self, payload: Mapping[str, Any], wall: datetime) -> str:
        changes = payload.get("price_changes")
        if not isinstance(changes, list) or not changes:
            self.bad_message_count += 1
            return "missing_price_changes"
        ts = message_timestamp_ms(payload)
        if ts is None:
            self.bad_message_count += 1
            self.resync_count += 1
            return "missing_event_timestamp"
        applied = 0
        for change in changes:
            if not isinstance(change, Mapping):
                self.bad_message_count += 1
                continue
            token = str(change.get("asset_id") or "").strip()
            book = self._book_for(token)
            if book is None:
                continue
            if not book.healthy:
                book.mark_unhealthy("delta_before_snapshot")
                self.resync_count += 1
                self.bad_message_count += 1
                continue
            if book.last_applied_ts_ms is not None and ts < book.last_applied_ts_ms:
                self._reject_order(book, "out_of_order_delta")
                continue
            side = str(change.get("side") or "").strip().upper()
            try:
                if side == "BUY":
                    _apply_change(book.bids, change.get("price"), change.get("size"))
                elif side == "SELL":
                    _apply_change(book.asks, change.get("price"), change.get("size"))
                else:
                    raise StreamBookError("unknown_price_change_side")
            except StreamBookError:
                book.mark_unhealthy("invalid_delta")
                self.resync_count += 1
                self.bad_message_count += 1
                continue
            self._accept(book, ts, wall, snapshot=False)
            applied += 1
        if applied == 0:
            return "delta_rejected"
        return "delta"
