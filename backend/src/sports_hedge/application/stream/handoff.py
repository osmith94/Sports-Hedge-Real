"""STREAM → existing Price-2 → existing opening. No second solver or dispatcher.

Coalesced Matchbook exact-ID quotes plus a populated native-token Polymarket
book are the trigger. Opening still uses CataloguePriceEngine.reprice_for_paper_entry
and capture_with_execution_reprice. Execution stays disarmed unless the
existing live gates are already armed outside this module.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from sports_hedge.application.execution_reprice import (
    execution_reprice_permitted,
    price2_entry_authorized,
)
from sports_hedge.application.price_engine import PriceEngineRuntimeItem
from sports_hedge.application.stream.pin import StreamFixturePin, StreamMarketPin
from sports_hedge.application.stream.shadow import StreamCandidate
from sports_hedge.paper.models import PaperScanDecision

Price2Capture = Callable[[PaperScanDecision, PriceEngineRuntimeItem], Awaitable[Any]]


class StreamPrice2Handoff:
    """One in-flight Price-2 attempt per catalogue row. Never places orders itself."""

    def __init__(self, capture: Price2Capture | None = None) -> None:
        self.capture = capture
        self.in_flight: set[str] = set()
        self.attempt_count = 0
        self.accepted_count = 0
        self.last_error: str | None = None
        self.paper_opened = False
        self.orders_placed = False
        self.last_snapshot_id: str | None = None
        self.accepted_rows: set[str] = set()
        self.paper_opened_rows: set[str] = set()

    def bind_capture(self, capture: Price2Capture | None) -> None:
        self.capture = capture

    async def maybe_open(
        self,
        *,
        pin: StreamFixturePin,
        candidates: list[StreamCandidate],
        decisions: dict[str, PaperScanDecision],
    ) -> None:
        for candidate in candidates:
            decision = decisions.get(candidate.catalogue_row_id)
            if decision is None or not execution_reprice_permitted(decision):
                continue
            market = market_pin(pin, candidate.catalogue_row_id)
            if market is None or market.unavailable_reason:
                continue
            await self._one(market, decision)

    async def _one(self, market: StreamMarketPin, decision: PaperScanDecision) -> None:
        catalogue_row_id = market.identity.catalogue_row_id
        if catalogue_row_id in self.in_flight:
            return
        self.in_flight.add(catalogue_row_id)
        self.attempt_count += 1
        try:
            capture = self.capture or _shared_capture()
            if capture is None:
                self.last_error = "price2_engine_unavailable"
                return
            runtime = PriceEngineRuntimeItem(identity=market.identity)
            captured = await capture(decision, runtime)
            price2_accepted, paper_opened, orders_placed, snapshot_id = recorded_handoff_outcome(
                captured
            )
            if price2_accepted:
                self.accepted_count += 1
                self.accepted_rows.add(catalogue_row_id)
                if snapshot_id:
                    self.last_snapshot_id = snapshot_id
            if paper_opened:
                self.paper_opened = True
                self.paper_opened_rows.add(catalogue_row_id)
            if orders_placed:
                self.orders_placed = True
            self.last_error = None
        except Exception as exc:  # noqa: BLE001 — STREAM must not kill the observer
            self.last_error = type(exc).__name__
        finally:
            self.in_flight.discard(catalogue_row_id)


def recorded_handoff_outcome(captured: Any) -> tuple[bool, bool, bool, str | None]:
    """Price-2 accepted, persisted paper open, venue orders, snapshot id.

    Eligibility flags and execution_armed are not evidence. STREAM reports
    only what the capture result recorded.
    """

    if captured is None:
        return False, False, False, None
    snapshot = getattr(captured, "snapshot", None)
    price2_accepted = bool(getattr(captured, "price2_accepted", False))
    if not price2_accepted:
        price2_accepted = price2_entry_authorized(snapshot)
    paper_opened = bool(getattr(captured, "paper_opened", False))
    if getattr(captured, "paper_trade_id", None):
        paper_opened = True
    orders_placed = bool(getattr(captured, "orders_placed", False))
    submitted = getattr(captured, "orders_submitted", 0) or 0
    try:
        if int(submitted) > 0:
            orders_placed = True
    except (TypeError, ValueError):
        pass
    snapshot_id = getattr(snapshot, "snapshot_id", None)
    return price2_accepted, paper_opened, orders_placed, snapshot_id


def market_pin(pin: StreamFixturePin, catalogue_row_id: str) -> StreamMarketPin | None:
    for market in pin.markets:
        if market.identity.catalogue_row_id == catalogue_row_id:
            return market
    return None


def _shared_capture() -> Price2Capture | None:
    from sports_hedge.application.execution_reprice import capture_with_execution_reprice
    from sports_hedge.application.live_refresh import get_live_refresh_coordinator

    coordinator = get_live_refresh_coordinator()
    engine = getattr(coordinator, "_price_engine", None)
    if engine is None:
        return None
    bound = getattr(engine, "on_item_decision", None)
    if callable(bound):
        return bound
    service = getattr(engine, "paper_scan", None)
    watchlist = getattr(engine, "watchlist", None)
    if service is None or watchlist is None:
        return None

    async def _capture(decision: PaperScanDecision, runtime: PriceEngineRuntimeItem) -> Any:
        return await capture_with_execution_reprice(
            decision,
            runtime=runtime,
            engine=engine,
            service=service,
            watchlist=watchlist,
            pricing_lane="stream",
        )

    return _capture
