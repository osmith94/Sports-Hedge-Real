"""ACTIVE TRADE lane: 5s exact-ID repricing of fully hedged OPEN paper trades.

No discovery, rematching, or equivalence re-proof. Uses the shared provider
access layer with priority above HOT. Does not raise provider concurrency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from threading import RLock

from sports_hedge.application.approved_market_catalogue import (
    DerivedPriceEngineItem,
    OutcomeNativeId,
)
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.trades import (
    PaperActiveTradePhase,
    PaperTrade,
    PaperTradeState,
)

ACTIVE_TRADE_LANE = "active_trade"
DEFAULT_ACTIVE_TRADE_CADENCE_SECONDS = 5
MAX_ACTIVE_TRADES_PER_TICK = 1


@dataclass
class ActiveTradeMembership:
    trade_id: str
    opportunity_id: str
    last_priced_at: datetime | None = None
    next_due_at: datetime | None = None
    phase: PaperActiveTradePhase = PaperActiveTradePhase.ACCUMULATING
    list_events_calls: int = 0
    list_markets_calls: int = 0


@dataclass
class ActiveTradeTickResult:
    trade_id: str
    opportunity_id: str
    phase: PaperActiveTradePhase
    topped_up: bool = False
    aborted_incomplete: bool = False
    list_events_calls: int = 0
    list_markets_calls: int = 0
    identity: DerivedPriceEngineItem | None = None
    detail: str = ""


class ActiveTradeRegistry:
    """In-process ACTIVE TRADE membership. Stop/Resume preserves this state."""

    def __init__(self) -> None:
        self._lock = RLock()
        self._members: dict[str, ActiveTradeMembership] = {}
        self._rr_cursor = 0

    def promote(self, trade: PaperTrade, *, now: datetime, cadence_seconds: int) -> ActiveTradeMembership:
        with self._lock:
            existing = self._members.get(trade.trade_id)
            if existing is not None:
                return existing
            member = ActiveTradeMembership(
                trade_id=trade.trade_id,
                opportunity_id=trade.opportunity_id,
                next_due_at=now,
                phase=trade.active_trade_phase or PaperActiveTradePhase.ACCUMULATING,
            )
            self._members[trade.trade_id] = member
            return member

    def drop(self, trade_id: str) -> None:
        with self._lock:
            self._members.pop(trade_id, None)

    def get(self, trade_id: str) -> ActiveTradeMembership | None:
        with self._lock:
            return self._members.get(trade_id)

    def members(self) -> list[ActiveTradeMembership]:
        with self._lock:
            return list(self._members.values())

    def due_members(self, now: datetime, *, limit: int = MAX_ACTIVE_TRADES_PER_TICK) -> list[ActiveTradeMembership]:
        with self._lock:
            due = [
                member
                for member in self._members.values()
                if member.next_due_at is None or now >= member.next_due_at
            ]
            if not due:
                return []
            due.sort(key=lambda item: (_aware_or_min(item.last_priced_at), item.trade_id))
            if self._rr_cursor >= len(due):
                self._rr_cursor = 0
            rotated = due[self._rr_cursor :] + due[: self._rr_cursor]
            self._rr_cursor = (self._rr_cursor + min(limit, len(rotated))) % max(len(due), 1)
            return rotated[:limit]

    def mark_priced(
        self,
        trade_id: str,
        *,
        now: datetime,
        cadence_seconds: int,
        phase: PaperActiveTradePhase,
        list_events_calls: int = 0,
        list_markets_calls: int = 0,
    ) -> None:
        with self._lock:
            member = self._members.get(trade_id)
            if member is None:
                return
            member.last_priced_at = now
            member.next_due_at = now + timedelta(seconds=cadence_seconds)
            member.phase = phase
            member.list_events_calls += list_events_calls
            member.list_markets_calls += list_markets_calls

    def next_due_at(self) -> datetime | None:
        with self._lock:
            dues = [member.next_due_at for member in self._members.values() if member.next_due_at]
            return min(dues) if dues else None

    def clear(self) -> None:
        with self._lock:
            self._members.clear()
            self._rr_cursor = 0


_RUNTIME_REGISTRY = ActiveTradeRegistry()


def get_active_trade_registry() -> ActiveTradeRegistry:
    return _RUNTIME_REGISTRY


def reset_active_trade_registry() -> None:
    _RUNTIME_REGISTRY.clear()


def active_trade_cadence_seconds(settings: Settings | None = None) -> int:
    resolved = settings or get_settings()
    return int(resolved.paper_active_trade_interval_seconds)


def identity_from_open_trade(trade: PaperTrade) -> DerivedPriceEngineItem | None:
    """Exact native IDs already on the OPEN trade. Never rediscovers markets."""

    if trade.state is not PaperTradeState.OPEN:
        return None
    matchbook_event = None
    matchbook_market = None
    matchbook_runners: list[OutcomeNativeId] = []
    kalshi_event = None
    kalshi_tickers: list[str] = []
    kalshi_outcomes: list[OutcomeNativeId] = []
    for leg in trade.legs:
        if leg.venue is VenueName.MATCHBOOK:
            matchbook_event = matchbook_event or leg.source_event_id
            matchbook_market = matchbook_market or leg.source_market_id
            if leg.source_runner_id:
                matchbook_runners.append(
                    OutcomeNativeId(outcome=leg.outcome, native_id=leg.source_runner_id)
                )
        elif leg.venue is VenueName.KALSHI:
            kalshi_event = kalshi_event or leg.source_event_id
            ticker = leg.source_contract_id or leg.source_market_id
            if ticker and ticker not in kalshi_tickers:
                kalshi_tickers.append(ticker)
            if ticker:
                kalshi_outcomes.append(OutcomeNativeId(outcome=leg.outcome, native_id=ticker))
    if not matchbook_market or not kalshi_tickers:
        return None
    return DerivedPriceEngineItem(
        catalogue_row_id=f"active-trade:{trade.trade_id}",
        content_version=0,
        canonical_event_id=trade.canonical_event_id or trade.trade_id,
        register_canonical_key=trade.canonical_market_id or trade.opportunity_id,
        matchbook_event_id=matchbook_event,
        matchbook_market_id=matchbook_market,
        matchbook_runner_ids=matchbook_runners,
        kalshi_event_ticker=kalshi_event,
        kalshi_market_tickers=kalshi_tickers,
        kalshi_outcome_ids=kalshi_outcomes,
        family=None if trade.market_family is None else trade.market_family.value,
        period=None if trade.period is None else trade.period.value,
        required_outcomes=sorted({leg.outcome for leg in trade.legs if leg.filled_stake > 0}),
        competition=trade.competition,
        home_canonical=trade.home_team,
        away_canonical=trade.away_team,
    )


def remaining_trade_room_gbp(trade: PaperTrade, cap_gbp: Decimal) -> Decimal:
    locked = trade.capital_locked_gbp or Decimal("0")
    room = cap_gbp - locked
    return room if room > 0 else Decimal("0")


def _aware_or_min(value: datetime | None) -> datetime:
    if value is None:
        return datetime.min.replace(tzinfo=UTC)
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value
