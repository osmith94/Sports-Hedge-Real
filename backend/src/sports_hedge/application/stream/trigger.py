"""Significance-aware STREAM Matchbook refresh decisions.

Book maintenance is separate: this module only decides when to enqueue a
lowest-priority exact-ID Matchbook GET. Thresholds are absolute probability
points and share size, never sport/league branches or odds-percent moves.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Callable

from sports_hedge.application.stream.coalescer import MatchbookQuote
from sports_hedge.application.stream.order_book import StreamOrderBooks, TokenLadder
from sports_hedge.application.stream.pin import StreamFixturePin, StreamMarketPin
from sports_hedge.application.stream.protocol import (
    DEPTH_CHANGE_SHARES,
    MATCHBOOK_CREDIBLE_AGE_SECONDS,
    NOISE_PROBABILITY_POINTS,
    PRICE_MOVE_PROBABILITY_POINTS,
    TRIGGER_BASELINE,
    TRIGGER_DEPTH_CHANGE,
    TRIGGER_PERIODIC,
    TRIGGER_POTENTIAL_EDGE,
    TRIGGER_PRICE_MOVE,
    TRIGGER_RECONNECT,
)

MarketKey = tuple[str, str]
EdgeProbe = Callable[[StreamMarketPin, datetime], Decimal | None]


@dataclass(frozen=True)
class TokenView:
    implied_prob: Decimal | None
    executable_size: Decimal
    healthy: bool


@dataclass(frozen=True)
class TriggerDecision:
    reason: str
    keys: tuple[MarketKey, ...]
    probability_delta: Decimal | None
    suppressed: bool = False


@dataclass(frozen=True)
class StreamBookSignal:
    results: tuple[str, ...]
    books_complete: bool
    baseline_ready: bool
    recovery_kind: str | None
    applied_tokens: tuple[str, ...] = ()


def view_for(book: TokenLadder) -> TokenView:
    return TokenView(
        implied_prob=book.implied_probability(),
        executable_size=book.executable_ask_size(),
        healthy=book.healthy,
    )


class StreamTriggerPolicy:
    """Decide Matchbook requests from book *changes*, not from every applied tick."""

    def __init__(
        self,
        *,
        price_move_points: Decimal = PRICE_MOVE_PROBABILITY_POINTS,
        noise_points: Decimal = NOISE_PROBABILITY_POINTS,
        depth_shares: Decimal = DEPTH_CHANGE_SHARES,
        matchbook_credible_age_seconds: float = MATCHBOOK_CREDIBLE_AGE_SECONDS,
        edge_probe: EdgeProbe | None = None,
    ) -> None:
        self.price_move_points = price_move_points
        self.noise_points = noise_points
        self.depth_shares = depth_shares
        self.matchbook_credible_age_seconds = matchbook_credible_age_seconds
        self.edge_probe = edge_probe
        self.last: dict[str, TokenView] = {}
        self._baseline_complete = False
        self.suppressed_count = 0

    def note_reset(self) -> None:
        self.last.clear()
        self._baseline_complete = False

    def periodic(self, pin: StreamFixturePin) -> TriggerDecision:
        return TriggerDecision(
            reason=TRIGGER_PERIODIC,
            keys=_streamable_keys(pin),
            probability_delta=None,
        )

    def evaluate(
        self,
        *,
        pin: StreamFixturePin,
        books: StreamOrderBooks,
        quotes: dict[str, MatchbookQuote],
        now: datetime,
        signal: StreamBookSignal,
    ) -> TriggerDecision | None:
        current = {
            token: view_for(books.books[token])
            for token in pin.token_ids
            if token in books.books
        }
        if signal.baseline_ready and signal.books_complete:
            self.last = current
            self._baseline_complete = True
            reason = TRIGGER_RECONNECT if signal.recovery_kind == TRIGGER_RECONNECT else TRIGGER_BASELINE
            return TriggerDecision(reason=reason, keys=_streamable_keys(pin), probability_delta=None)
        if not signal.books_complete or not self._baseline_complete:
            self.last = current
            return None

        max_prob_delta = Decimal("0")
        material_depth = False
        changed_tokens: list[str] = []
        for token, view in current.items():
            previous = self.last.get(token)
            delta = _prob_delta(previous, view)
            if delta > max_prob_delta:
                max_prob_delta = delta
            if _depth_material(previous, view, self.depth_shares):
                material_depth = True
            if previous != view:
                changed_tokens.append(token)
        self.last = current
        if not changed_tokens:
            self.suppressed_count += 1
            return TriggerDecision(
                reason="",
                keys=(),
                probability_delta=max_prob_delta,
                suppressed=True,
            )

        keys = _keys_for_tokens(pin, changed_tokens) or _streamable_keys(pin)
        if max_prob_delta >= self.price_move_points:
            return TriggerDecision(
                reason=TRIGGER_PRICE_MOVE,
                keys=keys,
                probability_delta=max_prob_delta,
            )
        if material_depth:
            return TriggerDecision(
                reason=TRIGGER_DEPTH_CHANGE,
                keys=keys,
                probability_delta=max_prob_delta,
            )
        if max_prob_delta <= self.noise_points and not material_depth:
            # Size/price jitter below both economic thresholds: keep the book, skip MB.
            self.suppressed_count += 1
            return TriggerDecision(
                reason="",
                keys=(),
                probability_delta=max_prob_delta,
                suppressed=True,
            )

        stale_or_missing = False
        positive_edge = False
        for event_id, market_id in keys:
            quote = quotes.get(f"{event_id}:{market_id}")
            if quote is None or _quote_age_seconds(quote, now) > self.matchbook_credible_age_seconds:
                stale_or_missing = True
                continue
            market = _market_for(pin, event_id, market_id)
            if market is None or self.edge_probe is None:
                continue
            edge = self.edge_probe(market, now)
            if edge is not None and edge > 0:
                positive_edge = True
        if positive_edge:
            return TriggerDecision(
                reason=TRIGGER_POTENTIAL_EDGE,
                keys=keys,
                probability_delta=max_prob_delta,
            )
        if stale_or_missing:
            return TriggerDecision(
                reason=TRIGGER_BASELINE,
                keys=keys,
                probability_delta=max_prob_delta,
            )
        self.suppressed_count += 1
        return TriggerDecision(
            reason="",
            keys=(),
            probability_delta=max_prob_delta,
            suppressed=True,
        )


def _prob_delta(previous: TokenView | None, current: TokenView) -> Decimal:
    if previous is None or previous.implied_prob is None or current.implied_prob is None:
        return Decimal("0")
    return abs(current.implied_prob - previous.implied_prob)


def _depth_material(previous: TokenView | None, current: TokenView, boundary: Decimal) -> bool:
    if previous is None:
        return False
    old = previous.executable_size
    new = current.executable_size
    if abs(new - old) >= boundary:
        return True
    lo, hi = (old, new) if old <= new else (new, old)
    return lo < boundary <= hi


def _quote_age_seconds(quote: MatchbookQuote, now: datetime) -> float:
    return max(0.0, (now - quote.retrieved_at).total_seconds())


def _streamable_keys(pin: StreamFixturePin) -> tuple[MarketKey, ...]:
    keys: list[MarketKey] = []
    for market in pin.markets:
        if market.unavailable_reason:
            continue
        event_id = str(market.identity.matchbook_event_id).strip()
        market_id = str(market.identity.matchbook_market_id).strip()
        if event_id and market_id:
            keys.append((event_id, market_id))
    return tuple(keys)


def _keys_for_tokens(pin: StreamFixturePin, tokens: list[str]) -> tuple[MarketKey, ...]:
    wanted = set(tokens)
    keys: list[MarketKey] = []
    seen: set[MarketKey] = set()
    for market in pin.markets:
        if market.unavailable_reason:
            continue
        if wanted.isdisjoint(market.token_ids):
            continue
        event_id = str(market.identity.matchbook_event_id).strip()
        market_id = str(market.identity.matchbook_market_id).strip()
        key = (event_id, market_id)
        if not event_id or not market_id or key in seen:
            continue
        seen.add(key)
        keys.append(key)
    return tuple(keys)


def _market_for(pin: StreamFixturePin, event_id: str, market_id: str) -> StreamMarketPin | None:
    for market in pin.markets:
        if (
            str(market.identity.matchbook_event_id) == event_id
            and str(market.identity.matchbook_market_id) == market_id
        ):
            return market
    return None
