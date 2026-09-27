"""Validated Price-2 snapshot a future live arm can consume before any order.

The object is the execution-quality record of one hedge. It is built only from
the exact books just retrieved. It does not carry discovery prices, depth, or
stakes. ``accepted`` is the gate: a live layer must refuse to submit orders
when it is false. This module does not place venue orders.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

from sports_hedge.application.executable_liquidity import decision_net_edge

# Initial PAPER validation bound. Cross-venue and constituent books must be
# retrieved within this window of each other. It is intentionally tighter than
# the 2,000 ms quote-age gate: two individually fresh books can still be too
# far apart to be one execution snapshot. 500 ms covers ordinary concurrent
# provider latency without treating a multi-hundred-millisecond leg as coherent.
DEFAULT_MAX_SNAPSHOT_SKEW_MS = 500


@dataclass(frozen=True)
class ExecutionRetrieval:
    """One exact native book and the instant it was retrieved."""

    venue: str
    native_id: str
    event_id: str | None
    retrieved_at: datetime


@dataclass(frozen=True)
class ExecutionLegQuote:
    """Executable price and depth taken from the Price-2 scan of those books."""

    venue: str
    outcome: str
    native_market_id: str
    native_runner_id: str
    displayed_odds: str
    available_depth: str
    requested_stake: str
    levels: tuple[tuple[str, str], ...]
    quote_age_ms: int | None
    retrieved_at: datetime | None


@dataclass
class ExecutionSnapshot:
    """One complete-set execution reprice, accepted only when skew and economics pass."""

    catalogue_row_id: str
    started_at: datetime
    retrievals: tuple[ExecutionRetrieval, ...]
    canonical_market_id: str | None = None
    evaluated_at: datetime | None = None
    legs: tuple[ExecutionLegQuote, ...] = ()
    oldest_quote_age_ms: int | None = None
    max_quote_age_ms: int = 2000
    max_skew_ms: int = DEFAULT_MAX_SNAPSHOT_SKEW_MS
    net_edge: str | None = None
    guaranteed_profit: str | None = None
    accepted: bool = False
    rejection_reason: str | None = None
    snapshot_id: str = ""

    def __post_init__(self) -> None:
        if not self.snapshot_id:
            self.snapshot_id = f"exec:{self.catalogue_row_id}:{uuid4()}"

    @property
    def earliest_retrieval_at(self) -> datetime | None:
        if not self.retrievals:
            return None
        return min(item.retrieved_at for item in self.retrievals)

    @property
    def latest_retrieval_at(self) -> datetime | None:
        if not self.retrievals:
            return None
        return max(item.retrieved_at for item in self.retrievals)

    @property
    def skew_ms(self) -> int | None:
        """latest_retrieval_at - earliest_retrieval_at, in milliseconds."""

        earliest = self.earliest_retrieval_at
        latest = self.latest_retrieval_at
        if earliest is None or latest is None:
            return None
        return max(0, int((latest - earliest).total_seconds() * 1000))

    def skew_exceeded(self) -> bool:
        skew = self.skew_ms
        if skew is None:
            return True
        return skew > self.max_skew_ms

    def audit_line(self) -> str:
        earliest = self.earliest_retrieval_at
        latest = self.latest_retrieval_at
        return " ".join(
            (
                f"snapshot_id={self.snapshot_id}",
                f"skew_ms={self.skew_ms if self.skew_ms is not None else 'unknown'}",
                f"max_skew_ms={self.max_skew_ms}",
                f"earliest_retrieval_at={earliest.isoformat() if earliest else 'unknown'}",
                f"latest_retrieval_at={latest.isoformat() if latest else 'unknown'}",
                f"retrievals={len(self.retrievals)}",
                f"accepted={str(self.accepted).lower()}",
            )
        )

    def to_json(self) -> str:
        """Stable record of native ids, retrieval times, prices, and depth."""

        payload = {
            "snapshot_id": self.snapshot_id,
            "catalogue_row_id": self.catalogue_row_id,
            "canonical_market_id": self.canonical_market_id,
            "started_at": self.started_at.isoformat(),
            "evaluated_at": self.evaluated_at.isoformat() if self.evaluated_at else None,
            "earliest_retrieval_at": (
                self.earliest_retrieval_at.isoformat() if self.earliest_retrieval_at else None
            ),
            "latest_retrieval_at": (
                self.latest_retrieval_at.isoformat() if self.latest_retrieval_at else None
            ),
            "skew_ms": self.skew_ms,
            "max_skew_ms": self.max_skew_ms,
            "oldest_quote_age_ms": self.oldest_quote_age_ms,
            "max_quote_age_ms": self.max_quote_age_ms,
            "net_edge": self.net_edge,
            "guaranteed_profit": self.guaranteed_profit,
            "accepted": self.accepted,
            "rejection_reason": self.rejection_reason,
            "retrievals": [
                {
                    "venue": item.venue,
                    "native_id": item.native_id,
                    "event_id": item.event_id,
                    "retrieved_at": item.retrieved_at.isoformat(),
                }
                for item in self.retrievals
            ],
            "legs": [
                {
                    "venue": leg.venue,
                    "outcome": leg.outcome,
                    "native_market_id": leg.native_market_id,
                    "native_runner_id": leg.native_runner_id,
                    "displayed_odds": leg.displayed_odds,
                    "available_depth": leg.available_depth,
                    "requested_stake": leg.requested_stake,
                    "quote_age_ms": leg.quote_age_ms,
                    "retrieved_at": leg.retrieved_at.isoformat() if leg.retrieved_at else None,
                    "levels": [{"odds": odds, "depth": depth} for odds, depth in leg.levels],
                }
                for leg in self.legs
            ],
        }
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def execution_max_snapshot_skew_ms(settings: Any) -> int:
    """Configured skew bound, never looser than the active quote-age gate."""

    configured = DEFAULT_MAX_SNAPSHOT_SKEW_MS
    freshness = 2000
    if settings is not None:
        configured = int(
            getattr(settings, "paper_execution_max_snapshot_skew_ms", configured) or configured
        )
        freshness = int(getattr(settings, "paper_entry_max_quote_age_ms", freshness) or freshness)
    return max(0, min(configured, freshness))


def leg_quotes_from_decision(
    decision: Any,
    *,
    retrieved_at_by_venue: dict[str, datetime],
) -> tuple[ExecutionLegQuote, ...]:
    """Prices, depth, and stakes from the Price-2 decision only."""

    quotes: list[ExecutionLegQuote] = []
    for leg in decision.fill_legs:
        levels = tuple(
            (str(level.decimal_odds), str(level.available_stake)) for level in (leg.levels or [])
        )
        depth = sum((level.available_stake for level in (leg.levels or [])), Decimal(0))
        venue = leg.venue.value
        quotes.append(
            ExecutionLegQuote(
                venue=venue,
                outcome=str(leg.outcome),
                native_market_id=str(leg.source_market_id),
                native_runner_id=str(leg.source_runner_id),
                displayed_odds=str(leg.displayed_odds),
                available_depth=str(depth),
                requested_stake=str(leg.requested_stake),
                levels=levels,
                quote_age_ms=leg.quote_age_ms,
                retrieved_at=retrieved_at_by_venue.get(venue),
            )
        )
    return tuple(quotes)


def snapshot_economics(decision: Any) -> tuple[str | None, str | None]:
    edge = decision_net_edge(decision)
    profit = _guaranteed_profit(decision)
    return (None if edge is None else str(edge), None if profit is None else str(profit))


def _guaranteed_profit(decision: Any) -> Decimal | None:
    allocation = decision.allocation
    if allocation is not None and allocation.accepted and allocation.guaranteed_profit > 0:
        return allocation.guaranteed_profit
    depth = decision.depth_scan
    if depth is not None and depth.solution.is_arbitrage:
        return depth.solution.guaranteed_profit
    payoff = decision.payoff_scan
    if payoff is not None and payoff.solution.is_arbitrage:
        return payoff.solution.minimum_state_pnl
    return None
