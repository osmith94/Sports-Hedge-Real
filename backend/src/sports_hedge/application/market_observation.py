from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from pydantic import BaseModel, Field, model_validator

from sports_hedge.application.quote_freshness import require_aware_instant
from sports_hedge.domain.football import CanonicalMarket, CanonicalOutcome
from sports_hedge.domain.models import VenueName
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.market_intelligence.models import MarketSnapshot
from sports_hedge.normalization.venues import KalshiNormalizer, MatchbookNormalizer, PolymarketNormalizer


class OutcomeOrderBook(BaseModel):
    outcome: CanonicalOutcome
    source_runner_id: str
    back_levels: list[BookLevel] = Field(default_factory=list)
    lay_levels: list[BookLevel] = Field(default_factory=list)
    raw_book: dict[str, Any] = Field(default_factory=dict)

    @property
    def best_back(self) -> BookLevel | None:
        if not self.back_levels:
            return None
        return max(self.back_levels, key=lambda item: item.decimal_odds)

    @property
    def best_lay(self) -> BookLevel | None:
        if not self.lay_levels:
            return None
        return min(self.lay_levels, key=lambda item: item.decimal_odds)

    @property
    def total_back_depth(self) -> Decimal:
        return sum((level.available_stake for level in self.back_levels), Decimal("0"))

    @property
    def probability_spread_bps(self) -> float:
        back = self.best_back
        lay = self.best_lay
        if back is None or lay is None:
            return 0.0
        back_probability = Decimal("1") / back.decimal_odds
        lay_probability = Decimal("1") / lay.decimal_odds
        return float(abs(back_probability - lay_probability) * Decimal("10000"))


class VenueMarketObservation(BaseModel):
    market: CanonicalMarket
    observed_at: datetime
    native_currency: str
    outcome_books: list[OutcomeOrderBook]
    source_latency_ms: int = Field(default=0, ge=0)
    quote_age_ms: int | None = Field(default=None, ge=0)
    metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_observation(self) -> "VenueMarketObservation":
        self.observed_at = require_aware_instant(self.observed_at, "observed_at")
        self.native_currency = self.native_currency.upper()
        if self.market.event.kickoff_utc.tzinfo is None:
            raise ValueError("kickoff_utc must be timezone-aware")
        self.market.event.kickoff_utc = require_aware_instant(
            self.market.event.kickoff_utc,
            "kickoff_utc",
        )
        outcomes = [book.outcome for book in self.outcome_books]
        if len(outcomes) != len(set(outcomes)):
            raise ValueError("outcome_books must contain unique canonical outcomes")
        market_outcomes = {runner.outcome for runner in self.market.runners}
        if not set(outcomes).issubset(market_outcomes):
            raise ValueError("outcome_books contain outcomes outside the canonical market")
        return self

    @property
    def venue(self) -> VenueName:
        return self.market.source_venue

    def book_for(self, outcome: CanonicalOutcome) -> OutcomeOrderBook | None:
        return next((book for book in self.outcome_books if book.outcome == outcome), None)

    def snapshots(
        self,
        *,
        canonical_event_id: str,
        canonical_market_id: str,
    ) -> list[MarketSnapshot]:
        snapshots: list[MarketSnapshot] = []
        for book in self.outcome_books:
            best_back = book.best_back
            if best_back is None:
                continue
            best_lay = book.best_lay
            total_liquidity = sum(
                (level.available_stake for level in [*book.back_levels, *book.lay_levels]),
                Decimal("0"),
            )
            snapshots.append(
                MarketSnapshot(
                    observed_at=self.observed_at,
                    venue=self.venue,
                    canonical_event_id=canonical_event_id,
                    canonical_market_id=canonical_market_id,
                    canonical_outcome=book.outcome.value,
                    market_family=self.market.family,
                    period=self.market.period,
                    market_line=self.market.line,
                    settlement_scope=self.market.settlement.scope,
                    settlement_key=self.market.settlement.deterministic_key(),
                    competition=self.market.event.competition,
                    home_team=self.market.event.home_team,
                    away_team=self.market.event.away_team,
                    source_event_id=self.market.event.source_event_id,
                    source_market_id=self.market.source_market_id,
                    source_outcome_id=book.source_runner_id,
                    kickoff_utc=self.market.event.kickoff_utc,
                    decimal_odds=best_back.decimal_odds,
                    best_back_odds=best_back.decimal_odds,
                    best_lay_odds=best_lay.decimal_odds if best_lay else None,
                    back_size=best_back.available_stake,
                    lay_size=best_lay.available_stake if best_lay else None,
                    total_liquidity=total_liquidity,
                    source_latency_ms=self.source_latency_ms,
                    order_book=[
                        {
                            "side": "back",
                            "odds": str(level.decimal_odds),
                            "available_stake": str(level.available_stake),
                        }
                        for level in sorted(
                            book.back_levels,
                            key=lambda item: item.decimal_odds,
                            reverse=True,
                        )
                    ]
                    + [
                        {
                            "side": "lay",
                            "odds": str(level.decimal_odds),
                            "available_stake": str(level.available_stake),
                        }
                        for level in sorted(book.lay_levels, key=lambda item: item.decimal_odds)
                    ],
                    metadata={
                        **self.metadata,
                        "native_currency": self.native_currency,
                        "quote_age_ms": self.quote_age_ms,
                    },
                )
            )
        return snapshots


class MatchbookObservationBuilder:
    def __init__(self, normalizer: MatchbookNormalizer | None = None) -> None:
        self.normalizer = normalizer or MatchbookNormalizer()

    def build(
        self,
        event_payload: dict[str, Any],
        market_payload: dict[str, Any],
        *,
        observed_at: datetime | None = None,
        native_currency: str = "GBP",
        source_latency_ms: int = 0,
        quote_age_ms: int | None = None,
        quote_age_basis: str | None = None,
        quote_age_reason: str | None = None,
    ) -> VenueMarketObservation:
        event = self.normalizer.normalize_event(event_payload)
        market = self.normalizer.normalize_market(event, market_payload)
        raw_runners = {
            str(runner.get("id")): runner
            for runner in market_payload.get("runners", [])
            if isinstance(runner, dict) and runner.get("id") is not None
        }
        books: list[OutcomeOrderBook] = []
        for runner in market.runners:
            raw_runner = raw_runners.get(runner.source_runner_id, {})
            back_levels: list[BookLevel] = []
            lay_levels: list[BookLevel] = []
            for price in raw_runner.get("prices", []) or []:
                if not isinstance(price, dict):
                    continue
                level = _matchbook_price_level(price)
                if level is None:
                    continue
                side, book_level = level
                if side in {"back", "win"}:
                    back_levels.append(book_level)
                elif side in {"lay", "lose"}:
                    lay_levels.append(book_level)
            books.append(
                OutcomeOrderBook(
                    outcome=runner.outcome,
                    source_runner_id=runner.source_runner_id,
                    back_levels=back_levels,
                    lay_levels=lay_levels,
                    raw_book={"prices": raw_runner.get("prices", []) or []},
                )
            )
        return VenueMarketObservation(
            market=market,
            observed_at=observed_at or datetime.now(UTC),
            native_currency=native_currency,
            outcome_books=books,
            source_latency_ms=source_latency_ms,
            quote_age_ms=quote_age_ms,
            metadata=_quote_metadata(
                "exchange_back_lay",
                basis=quote_age_basis,
                reason=quote_age_reason,
            ),
        )


class PolymarketObservationBuilder:
    def __init__(self, normalizer: PolymarketNormalizer | None = None) -> None:
        self.normalizer = normalizer or PolymarketNormalizer()

    def build(
        self,
        event_payload: dict[str, Any],
        market_payload: dict[str, Any],
        books_by_token: Mapping[str, dict[str, Any]],
        *,
        observed_at: datetime | None = None,
        source_latency_ms: int = 0,
        quote_age_ms: int | None = None,
        quote_age_basis: str | None = None,
        quote_age_reason: str | None = None,
    ) -> VenueMarketObservation:
        event = self.normalizer.normalize_event(event_payload)
        market = self.normalizer.normalize_market(event, market_payload)
        books: list[OutcomeOrderBook] = []
        for runner in market.runners:
            raw_book = dict(books_by_token.get(runner.source_runner_id, {}))
            back_levels = _polymarket_buy_levels(raw_book.get("asks", []) or [])
            lay_levels = _polymarket_sell_levels(raw_book.get("bids", []) or [])
            books.append(
                OutcomeOrderBook(
                    outcome=runner.outcome,
                    source_runner_id=runner.source_runner_id,
                    back_levels=back_levels,
                    lay_levels=lay_levels,
                    raw_book=raw_book,
                )
            )
        return VenueMarketObservation(
            market=market,
            observed_at=observed_at or datetime.now(UTC),
            native_currency="USD",
            outcome_books=books,
            source_latency_ms=source_latency_ms,
            quote_age_ms=quote_age_ms,
            metadata=_quote_metadata(
                "clob_token_probability",
                basis=quote_age_basis,
                reason=quote_age_reason,
            ),
        )


class KalshiObservationBuilder:
    """Convert Kalshi YES/NO bid ladders into executable BUY economics.

    A YES bid at X is not treated as Matchbook BACK. The documented binary
    complement is: a NO bid at X is the executable YES ask at 1-X (and vice versa).
    """

    def __init__(self, normalizer: KalshiNormalizer | None = None) -> None:
        self.normalizer = normalizer or KalshiNormalizer()

    def build(
        self,
        event_payload: dict[str, Any],
        market_payloads: list[dict[str, Any]] | dict[str, Any],
        books_by_ticker: Mapping[str, dict[str, Any]],
        *,
        series: dict[str, Any] | None = None,
        observed_at: datetime | None = None,
        source_latency_ms: int = 0,
        quote_age_ms: int | None = None,
        quote_age_basis: str | None = None,
        quote_age_reason: str | None = None,
        fee_snapshot: dict[str, Any] | None = None,
    ) -> VenueMarketObservation:
        event = self.normalizer.normalize_event(event_payload, series=series)
        payloads = market_payloads if isinstance(market_payloads, list) else [market_payloads]
        assembled = self.normalizer.assemble_canonical_markets(event, payloads, series=series)
        if len(assembled) != 1:
            raise ValueError("Kalshi observation builder requires one canonical market group")
        market = assembled[0]
        books: list[OutcomeOrderBook] = []
        for runner in market.runners:
            ticker, side = _kalshi_runner_ticker_side(runner.source_runner_id)
            raw_book = dict(books_by_ticker.get(ticker, {}))
            back_levels = _kalshi_buy_levels(raw_book, side=side)
            books.append(
                OutcomeOrderBook(
                    outcome=runner.outcome,
                    source_runner_id=runner.source_runner_id,
                    back_levels=back_levels,
                    lay_levels=[],
                    raw_book={
                        **raw_book,
                        "market_ticker": ticker,
                        "event_ticker": event.source_event_id,
                        "contract_side": side,
                        "complement": (
                            "yes_ask = 1 - no_bid" if side == "YES" else "no_ask = 1 - yes_bid"
                        ),
                    },
                )
            )
        metadata = _quote_metadata(
            "kalshi_binary_complement_buy",
            basis=quote_age_basis,
            reason=quote_age_reason,
        )
        if fee_snapshot:
            metadata["kalshi_fee"] = fee_snapshot
        return VenueMarketObservation(
            market=market,
            observed_at=observed_at or datetime.now(UTC),
            native_currency="USD",
            outcome_books=books,
            source_latency_ms=source_latency_ms,
            quote_age_ms=quote_age_ms,
            metadata=metadata,
        )


def _kalshi_runner_ticker_side(source_runner_id: str) -> tuple[str, str]:
    if source_runner_id.endswith(":YES"):
        return source_runner_id[: -len(":YES")], "YES"
    if source_runner_id.endswith(":NO"):
        return source_runner_id[: -len(":NO")], "NO"
    return source_runner_id, "YES"


def _kalshi_buy_levels(raw_book: Mapping[str, Any], *, side: str) -> list[BookLevel]:
    orderbook = raw_book.get("orderbook_fp")
    if not isinstance(orderbook, dict):
        orderbook = raw_book if "yes_dollars" in raw_book or "no_dollars" in raw_book else {}
    yes_bids = _kalshi_price_levels(orderbook.get("yes_dollars"))
    no_bids = _kalshi_price_levels(orderbook.get("no_dollars"))
    if side == "YES":
        opposite = no_bids
    else:
        opposite = yes_bids
    result: list[BookLevel] = []
    for price, quantity in opposite:
        ask = Decimal("1") - price
        if ask <= 0 or ask >= 1 or quantity <= 0:
            continue
        result.append(
            BookLevel(
                decimal_odds=Decimal("1") / ask,
                available_stake=ask * quantity,
            )
        )
    return sorted(result, key=lambda item: item.decimal_odds, reverse=True)


def _kalshi_price_levels(levels: Any) -> list[tuple[Decimal, Decimal]]:
    result: list[tuple[Decimal, Decimal]] = []
    if not isinstance(levels, list):
        return result
    for level in levels:
        if not isinstance(level, (list, tuple)) or len(level) < 2:
            continue
        try:
            price = Decimal(str(level[0]))
            quantity = Decimal(str(level[1]))
        except (InvalidOperation, ValueError):
            continue
        if price <= 0 or price >= 1 or quantity <= 0:
            continue
        result.append((price, quantity))
    return result


def _matchbook_price_level(price: dict[str, Any]) -> tuple[str, BookLevel] | None:
    side = str(price.get("side", "")).strip().casefold()
    odds = _decimal_from(price, "odds", "price")
    amount = _decimal_from(
        price,
        "available-amount",
        "available_amount",
        "availableAmount",
        "available",
        "size",
    )
    if side not in {"back", "lay", "win", "lose"}:
        return None
    if odds is None or odds <= 1 or amount is None or amount <= 0:
        return None
    return side, BookLevel(decimal_odds=odds, available_stake=amount)


def _polymarket_buy_levels(levels: list[Any]) -> list[BookLevel]:
    result: list[BookLevel] = []
    for level in levels:
        if not isinstance(level, dict):
            continue
        probability = _decimal_from(level, "price")
        shares = _decimal_from(level, "size")
        if probability is None or shares is None or not Decimal("0") < probability < Decimal("1"):
            continue
        if shares <= 0:
            continue
        result.append(
            BookLevel(
                decimal_odds=Decimal("1") / probability,
                available_stake=probability * shares,
            )
        )
    return sorted(result, key=lambda item: item.decimal_odds, reverse=True)


def _polymarket_sell_levels(levels: list[Any]) -> list[BookLevel]:
    result: list[BookLevel] = []
    for level in levels:
        if not isinstance(level, dict):
            continue
        probability = _decimal_from(level, "price")
        shares = _decimal_from(level, "size")
        if probability is None or shares is None or not Decimal("0") < probability < Decimal("1"):
            continue
        if shares <= 0:
            continue
        result.append(
            BookLevel(
                decimal_odds=Decimal("1") / probability,
                available_stake=probability * shares,
            )
        )
    return sorted(result, key=lambda item: item.decimal_odds)


def _decimal_from(payload: Mapping[str, Any], *keys: str) -> Decimal | None:
    for key in keys:
        value = payload.get(key)
        if value is None:
            continue
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            continue
    return None


def _quote_metadata(
    price_model: str,
    *,
    basis: str | None,
    reason: str | None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {"price_model": price_model}
    if basis:
        metadata["quote_age_basis"] = basis
    if reason:
        metadata["quote_age_reason"] = reason
    return metadata
