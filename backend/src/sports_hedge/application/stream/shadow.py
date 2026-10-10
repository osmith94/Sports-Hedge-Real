"""Diagnostic STREAM comparison. Reuses observation builders and paper scan_pair.

Never opens paper, never calls Price-2, never places orders.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sports_hedge.application.executable_liquidity import decision_net_edge
from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.price_engine import (
    _canonical_matchbook_market,
    _canonical_polymarket_market,
)
from sports_hedge.application.quote_freshness import matchbook_market_quote_age
from sports_hedge.application.stream.coalescer import MatchbookQuote
from sports_hedge.application.stream.order_book import StreamOrderBooks
from sports_hedge.application.stream.pin import StreamFixturePin
from sports_hedge.application.stream.protocol import FUTURE_TIMESTAMP_SLACK_MS, STALE_AFTER_SECONDS
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import PaperScanDecision


@dataclass(frozen=True)
class StreamCandidate:
    catalogue_row_id: str
    register_canonical_key: str
    trustworthy: bool
    net_edge: str | None
    rejection_reasons: tuple[str, ...]
    stream_quote_at: str | None
    matchbook_quote_at: str | None
    pair_age_ms: int | None
    skew_ms: int | None
    data_class: str = "stream_shadow_candidate"


class StreamShadowComparer:
    def __init__(self, paper_scan: PaperScanService | None = None) -> None:
        self.matchbook_builder = MatchbookObservationBuilder()
        self.polymarket_builder = PolymarketObservationBuilder()
        self.paper_scan = paper_scan or _isolated_paper_scan()
        self.candidates: list[StreamCandidate] = []

    def compare(
        self,
        pin: StreamFixturePin,
        books: StreamOrderBooks,
        quotes: dict[str, MatchbookQuote],
        *,
        now: datetime | None = None,
    ) -> list[StreamCandidate]:
        evaluated = now or datetime.now(UTC)
        results: list[StreamCandidate] = []
        for market in pin.markets:
            if market.unavailable_reason:
                continue
            identity = market.identity
            quote = quotes.get(f"{identity.matchbook_event_id}:{identity.matchbook_market_id}")
            token_payloads = {
                token: books.books[token].snapshot_payload()
                for token in market.token_ids
                if token in books.books and books.books[token].healthy
            }
            stream_ts, stream_reason = _oldest_required_stream_ts(
                books, market.token_ids, now=evaluated
            )
            if quote is None or stream_ts is None or len(token_payloads) != len(market.token_ids):
                results.append(
                    StreamCandidate(
                        catalogue_row_id=identity.catalogue_row_id,
                        register_canonical_key=identity.register_canonical_key,
                        trustworthy=False,
                        net_edge=None,
                        rejection_reasons=(stream_reason or "incomplete_stream_or_matchbook_pair",),
                        stream_quote_at=_iso(stream_ts),
                        matchbook_quote_at=_iso(None if quote is None else quote.retrieved_at),
                        pair_age_ms=None,
                        skew_ms=None,
                    )
                )
                continue
            mb_market = _canonical_matchbook_market(identity)
            pm_market = _canonical_polymarket_market(identity)
            if mb_market is None or pm_market is None:
                results.append(
                    StreamCandidate(
                        catalogue_row_id=identity.catalogue_row_id,
                        register_canonical_key=identity.register_canonical_key,
                        trustworthy=False,
                        net_edge=None,
                        rejection_reasons=("canonical_identity_unavailable",),
                        stream_quote_at=_iso(stream_ts),
                        matchbook_quote_at=_iso(quote.retrieved_at),
                        pair_age_ms=None,
                        skew_ms=None,
                    )
                )
                continue
            mb_age = matchbook_market_quote_age(
                quote.payload, retrieved_at=quote.retrieved_at, evaluated_at=evaluated
            )
            pm_age_ms = max(0, int((evaluated - stream_ts).total_seconds() * 1000))
            if pm_age_ms > int(STALE_AFTER_SECONDS * 1000):
                results.append(
                    StreamCandidate(
                        catalogue_row_id=identity.catalogue_row_id,
                        register_canonical_key=identity.register_canonical_key,
                        trustworthy=False,
                        net_edge=None,
                        rejection_reasons=("stream_constituent_stale",),
                        stream_quote_at=_iso(stream_ts),
                        matchbook_quote_at=_iso(quote.retrieved_at),
                        pair_age_ms=pm_age_ms,
                        skew_ms=abs(int((quote.retrieved_at - stream_ts).total_seconds() * 1000)),
                    )
                )
                continue
            skew_ms = abs(int((quote.retrieved_at - stream_ts).total_seconds() * 1000))
            pair_age_ms = max(pm_age_ms, int(mb_age.quote_age_ms or 0))
            try:
                matchbook_obs = self.matchbook_builder.build_from_canonical(
                    mb_market,
                    quote.payload,
                    observed_at=evaluated,
                    quote_age_ms=mb_age.quote_age_ms,
                    quote_age_basis=mb_age.basis or "retrieval",
                    quote_age_reason=mb_age.reason,
                )
                polymarket_obs = self.polymarket_builder.build(
                    {
                        "id": identity.polymarket_event_id,
                        "title": f"{identity.home_canonical} vs {identity.away_canonical}",
                    },
                    {
                        "id": identity.polymarket_market_id,
                        "conditionId": identity.polymarket_condition_id,
                        "clobTokenIds": list(market.token_ids),
                    },
                    token_payloads,
                    canonical=pm_market,
                    observed_at=evaluated,
                    quote_age_ms=pm_age_ms,
                    quote_age_basis="stream_book",
                    quote_age_reason="stream_shadow_not_price2",
                )
                decision = self.paper_scan.scan_pair(
                    matchbook_obs,
                    polymarket_obs,
                    fixture_canonical_event_id=pin.canonical_event_id,
                )
            except Exception as exc:
                results.append(
                    StreamCandidate(
                        catalogue_row_id=identity.catalogue_row_id,
                        register_canonical_key=identity.register_canonical_key,
                        trustworthy=False,
                        net_edge=None,
                        rejection_reasons=(f"shadow_compare_failed:{type(exc).__name__}",),
                        stream_quote_at=_iso(stream_ts),
                        matchbook_quote_at=_iso(quote.retrieved_at),
                        pair_age_ms=pair_age_ms,
                        skew_ms=skew_ms,
                    )
                )
                continue
            results.append(_candidate_from_decision(identity, decision, stream_ts, quote, pair_age_ms, skew_ms))
        self.candidates = results
        return results


def _candidate_from_decision(
    identity: Any,
    decision: PaperScanDecision,
    stream_ts: datetime,
    quote: MatchbookQuote,
    pair_age_ms: int,
    skew_ms: int,
) -> StreamCandidate:
    reasons = tuple(str(item) for item in (decision.rejection_reasons or []))
    edge = decision_net_edge(decision)
    trustworthy = not reasons and edge is not None
    return StreamCandidate(
        catalogue_row_id=identity.catalogue_row_id,
        register_canonical_key=identity.register_canonical_key,
        trustworthy=trustworthy,
        net_edge=None if edge is None else format(Decimal(str(edge)), "f"),
        rejection_reasons=reasons or ("stream_shadow_not_executable",),
        stream_quote_at=_iso(stream_ts),
        matchbook_quote_at=_iso(quote.retrieved_at),
        pair_age_ms=pair_age_ms,
        skew_ms=skew_ms,
    )


def _isolated_paper_scan() -> PaperScanService:
    return PaperScanService(MarketIntelligenceService(SqliteMarketIntelligenceRepository()))


def _oldest_required_stream_ts(
    books: StreamOrderBooks,
    token_ids: tuple[str, ...],
    *,
    now: datetime,
) -> tuple[datetime | None, str | None]:
    stamps: list[int] = []
    now_ms = int(now.timestamp() * 1000)
    for token in token_ids:
        book = books.books.get(token)
        if book is None or not book.healthy:
            return None, "incomplete_stream_or_matchbook_pair"
        ts = book.last_applied_ts_ms
        if ts is None:
            return None, "missing_token_timestamp"
        if ts > now_ms + FUTURE_TIMESTAMP_SLACK_MS:
            return None, "future_token_timestamp"
        stamps.append(ts)
    if not stamps:
        return None, "incomplete_stream_or_matchbook_pair"
    oldest = min(stamps)
    return datetime.fromtimestamp(oldest / 1000, tz=UTC), None


def _iso(value: datetime | None) -> str | None:
    if value is None:
        return None
    return value.astimezone(UTC).isoformat()
