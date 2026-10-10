"""STREAM Phase 1 runtime. Default OFF. One fixture. Shadow-only."""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sports_hedge.application.stream.coalescer import StreamMatchbookCoalescer
from sports_hedge.application.stream.observer import (
    STATUS_DISABLED,
    STATUS_NOT_SELECTED,
    STATUS_PAUSED,
    PolymarketMarketObserver,
)
from sports_hedge.application.stream.pin import StreamFixturePin, StreamPinError, streamable_markets
from sports_hedge.application.stream.protocol import (
    MATCHBOOK_CREDIBLE_AGE_SECONDS,
    MAX_STREAM_FIXTURES,
    MAX_STREAM_TOKENS,
    PERIODIC_RECONCILE_SECONDS,
    PRICE_MOVE_PROBABILITY_POINTS,
)
from sports_hedge.application.stream.shadow import StreamShadowComparer
from sports_hedge.application.stream.status import StreamCandidateStatus, StreamStatus
from sports_hedge.application.stream.transport import TransportFactory
from sports_hedge.application.stream.trigger import (
    EdgeProbe,
    StreamBookSignal,
    StreamTriggerPolicy,
)

CatalogueLoader = Callable[[str], list[Any]]
MatchbookFetcher = Callable[[str, str], Awaitable[dict[str, Any]]]

PHASE1_LIMITATIONS = [
    "Phase 1 shadow-only: STREAM does not open paper, call Price-2, or place venue orders.",
    "At most one manually selected fixture. No automatic HOT/BACKGROUND promotion.",
    "Polymarket public market WS only. No user/order channel, wallet, or credentials.",
    "Matchbook exact-ID fetch is coalesced through shared provider access at STREAM priority.",
    "Matchbook refresh is significance-aware: baseline/reconnect, 2pp default probability-point moves, material depth, or potential net edge against a credible Matchbook snapshot. Negligible ticks update the book only.",
    "Periodic Matchbook reconciliation is lower priority than event-driven work and still uses the STREAM lane.",
    "Diagnostic GET /stream/status does not call providers.",
    "Candidate edges are telemetry, not executable quotes.",
]


class StreamRuntime:
    def __init__(
        self,
        *,
        catalogue_loader: CatalogueLoader | None = None,
        matchbook_fetch: MatchbookFetcher | None = None,
        transport_factory: TransportFactory | None = None,
        provider_access: Any = None,
        shadow: StreamShadowComparer | None = None,
        edge_probe: EdgeProbe | None = None,
        periodic_seconds: float = PERIODIC_RECONCILE_SECONDS,
    ) -> None:
        self._catalogue_loader = catalogue_loader
        self._matchbook_fetch = matchbook_fetch
        self._transport_factory = transport_factory
        self.pin: StreamFixturePin | None = None
        self.paused = False
        self.observer: PolymarketMarketObserver | None = None
        self.coalescer = StreamMatchbookCoalescer(
            fetch_market=matchbook_fetch,
            provider_access=provider_access,
            enabled=False,
        )
        self.coalescer.on_quote = self._refresh_candidates
        self.shadow = shadow or StreamShadowComparer()
        self.triggers = StreamTriggerPolicy(edge_probe=edge_probe or self._potential_edge)
        self.periodic_seconds = periodic_seconds
        self.paper_opened = False
        self.orders_placed = False
        self._lock = asyncio.Lock()
        self._periodic_task: asyncio.Task[None] | None = None

    def status(self) -> StreamStatus:
        pin = self.pin
        observer = self.observer
        selected = pin is not None
        if not selected:
            connection = STATUS_NOT_SELECTED
        elif self.paused:
            connection = STATUS_PAUSED
        elif observer is None:
            connection = STATUS_DISABLED
        else:
            connection = observer.connection_status(paused=self.paused, selected=True)
        stats = self.coalescer.stats
        books = None if observer is None else observer.books
        candidates = [
            StreamCandidateStatus(
                catalogue_row_id=item.catalogue_row_id,
                register_canonical_key=item.register_canonical_key,
                trustworthy=item.trustworthy,
                net_edge=item.net_edge,
                rejection_reasons=list(item.rejection_reasons),
                stream_quote_at=item.stream_quote_at,
                matchbook_quote_at=item.matchbook_quote_at,
                pair_age_ms=item.pair_age_ms,
                skew_ms=item.skew_ms,
                data_class=item.data_class,
            )
            for item in self.shadow.candidates
        ]
        return StreamStatus(
            enabled=selected and not self.paused,
            paused=self.paused,
            connection_status=connection,
            canonical_event_id=None if pin is None else pin.canonical_event_id,
            home_team=None if pin is None else pin.home_canonical,
            away_team=None if pin is None else pin.away_canonical,
            competition=None if pin is None else pin.competition,
            registered_market_count=0 if pin is None else len(pin.markets),
            subscribed_market_count=0 if pin is None else pin.subscribed_market_count,
            token_id_count=0 if pin is None else len(pin.token_ids),
            skipped_unavailable=0 if pin is None else pin.skipped_unavailable,
            reconnect_count=0 if observer is None else observer.reconnect_count,
            last_full_snapshot_at=_iso(None if observer is None else observer.last_snapshot_at),
            last_incremental_update_at=_iso(None if observer is None else observer.last_incremental_at),
            matchbook_request_count=stats.request_count,
            matchbook_rate_limited_count=stats.rate_limited_count,
            coalesced_event_count=stats.coalesced_count,
            dropped_event_count=stats.dropped_count,
            suppressed_event_count=stats.suppressed_count + self.triggers.suppressed_count,
            error_bad_message_count=0 if books is None else books.bad_message_count,
            unknown_token_count=0 if books is None else books.unknown_token_count,
            ignored_best_bid_ask_count=0 if books is None else books.ignored_bbo_count,
            resync_count=0 if books is None else books.resync_count,
            last_error=stats.last_error
            if observer is None
            else (observer.last_error or stats.last_error),
            last_trigger_reason=stats.last_trigger_reason,
            last_probability_delta=stats.last_probability_delta,
            last_dispatch_delay_ms=stats.last_dispatch_delay_ms,
            matchbook_requests_by_trigger=dict(stats.requests_by_trigger),
            price_move_probability_points=format(PRICE_MOVE_PROBABILITY_POINTS, "f"),
            token_cap=MAX_STREAM_TOKENS,
            max_fixtures=MAX_STREAM_FIXTURES,
            paper_opened=self.paper_opened,
            orders_placed=self.orders_placed,
            candidates=candidates,
            limitations=list(PHASE1_LIMITATIONS),
        )

    async def select_fixture(self, canonical_event_id: str) -> StreamStatus:
        event_id = str(canonical_event_id or "").strip()
        if not event_id:
            raise StreamPinError("canonical_event_id_required")
        rows = self._load_rows(event_id)
        pin = streamable_markets(rows)
        async with self._lock:
            await self._stop_session()
            self.pin = pin
            self.paused = False
            await self._start_session()
        return self.status()

    async def remove(self) -> StreamStatus:
        async with self._lock:
            await self._stop_session()
            self.pin = None
            self.paused = False
            self.shadow.candidates = []
        return self.status()

    async def pause(self) -> StreamStatus:
        async with self._lock:
            if self.pin is None:
                return self.status()
            self.paused = True
            await self._stop_session(keep_pin=True)
        return self.status()

    async def resume(self) -> StreamStatus:
        async with self._lock:
            if self.pin is None:
                return self.status()
            self.paused = False
            await self._start_session()
        return self.status()

    async def shutdown(self) -> None:
        async with self._lock:
            await self._stop_session()
            self.pin = None
            self.paused = False

    async def _start_session(self) -> None:
        pin = self.pin
        if pin is None or self.paused:
            return
        self.triggers.note_reset()
        self.coalescer.enabled = True
        if self._matchbook_fetch is not None:
            self.coalescer.bind_fetch(self._matchbook_fetch)
        else:
            self.coalescer.bind_fetch(self._default_matchbook_fetch)
        self.coalescer.start()
        observer = PolymarketMarketObserver(
            list(pin.token_ids),
            transport_factory=self._transport_factory,
            on_update=self._on_book_update,
        )
        self.observer = observer
        observer.start()
        self._periodic_task = asyncio.create_task(self._periodic_loop(), name="stream-periodic-mb")

    async def _stop_session(self, *, keep_pin: bool = False) -> None:
        del keep_pin
        self.coalescer.enabled = False
        periodic = self._periodic_task
        self._periodic_task = None
        if periodic is not None:
            periodic.cancel()
            try:
                await periodic
            except asyncio.CancelledError:
                pass
        await self.coalescer.stop()
        observer = self.observer
        self.observer = None
        self.triggers.note_reset()
        if observer is not None:
            await observer.stop()

    def _on_book_update(self, signal: StreamBookSignal) -> None:
        pin = self.pin
        observer = self.observer
        if pin is None or observer is None or not self.coalescer.enabled:
            return
        decision = self.triggers.evaluate(
            pin=pin,
            books=observer.books,
            quotes=self.coalescer.quotes,
            now=datetime.now(UTC),
            signal=signal,
        )
        if decision is None:
            return
        if decision.suppressed:
            self.coalescer.note_suppressed()
            return
        if not decision.keys:
            return
        self.coalescer.schedule(
            decision.keys,
            reason=decision.reason,
            probability_delta=decision.probability_delta,
        )

    async def _periodic_loop(self) -> None:
        try:
            while True:
                await asyncio.sleep(self.periodic_seconds)
                pin = self.pin
                if pin is None or not self.coalescer.enabled or self.paused:
                    continue
                decision = self.triggers.periodic(pin)
                self.coalescer.schedule(decision.keys, reason=decision.reason)
        except asyncio.CancelledError:
            raise

    def _refresh_candidates(self) -> None:
        pin = self.pin
        observer = self.observer
        if pin is None or observer is None:
            return
        if observer.connection_status(paused=self.paused, selected=True) not in {
            "subscribed",
            "stale",
            "degraded",
        }:
            if observer.status != "subscribed":
                return
        self.shadow.compare(pin, observer.books, self.coalescer.quotes)

    def _potential_edge(self, market: Any, now: datetime) -> Decimal | None:
        pin = self.pin
        observer = self.observer
        if pin is None or observer is None:
            return None
        quote = self.coalescer.quotes.get(
            f"{market.identity.matchbook_event_id}:{market.identity.matchbook_market_id}"
        )
        if quote is None:
            return None
        age = (now - quote.retrieved_at).total_seconds()
        if age > MATCHBOOK_CREDIBLE_AGE_SECONDS:
            return None
        results = self.shadow.compare(pin, observer.books, self.coalescer.quotes, now=now)
        for item in results:
            if item.catalogue_row_id != market.identity.catalogue_row_id:
                continue
            if not item.trustworthy or item.net_edge is None:
                return None
            edge = Decimal(item.net_edge)
            return edge if edge > 0 else None
        return None

    def _load_rows(self, canonical_event_id: str) -> list[Any]:
        loader = self._catalogue_loader
        if loader is None:
            from sports_hedge.persistence.approved_market_catalogue import (
                get_approved_market_catalogue_store,
            )

            return get_approved_market_catalogue_store().list_rows_for_event(canonical_event_id)
        return loader(canonical_event_id)

    async def _default_matchbook_fetch(self, event_id: str, market_id: str) -> dict[str, Any]:
        from sports_hedge.application.provider_runtime import get_shared_provider_runtime

        return await get_shared_provider_runtime().matchbook.get_market(event_id, market_id)


_LOCK = threading.Lock()
_SHARED: StreamRuntime | None = None


def get_stream_runtime() -> StreamRuntime:
    global _SHARED
    with _LOCK:
        if _SHARED is None:
            _SHARED = StreamRuntime()
        return _SHARED


def reset_stream_runtime(runtime: StreamRuntime | None = None) -> StreamRuntime:
    global _SHARED
    with _LOCK:
        _SHARED = runtime if runtime is not None else StreamRuntime()
        return _SHARED


def _iso(value: Any) -> str | None:
    if value is None:
        return None
    iso = getattr(value, "isoformat", None)
    if callable(iso):
        return iso()
    return str(value)
