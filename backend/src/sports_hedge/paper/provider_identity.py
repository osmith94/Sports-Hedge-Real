"""Recover durable venue identity for PAPER auto-settlement.

Legacy OPEN trades may store a Kalshi ticker without ``source_event_id``, or a
Matchbook market id whose event id lives on the approved catalogue rather than
the persisted leg. Auto-settlement must consume those alternate fields instead
of failing closed as ``missing_durable_provider_identity``.

Never treats a canonical event slug as a native Matchbook event id.
"""

from __future__ import annotations

from typing import Any, Iterable

from sports_hedge.domain.models import VenueName
from sports_hedge.paper.trades import PaperTrade, PaperTradeLeg


def recover_persisted_provider_identity(
    trade: PaperTrade,
    *,
    catalogue_rows: Iterable[Any] | None = None,
) -> tuple[PaperTrade, bool]:
    """Return a copy with recovered native IDs when alternate fields suffice."""

    rows = list(catalogue_rows or [])
    changed = False
    recovered: list[PaperTradeLeg] = []
    for leg in trade.legs:
        updated = _recover_leg(trade, leg, rows)
        if updated is not leg:
            changed = True
        recovered.append(updated)
    if not changed:
        return trade, False
    return trade.model_copy(update={"legs": recovered}), True


def _recover_leg(
    trade: PaperTrade,
    leg: PaperTradeLeg,
    rows: list[Any],
) -> PaperTradeLeg:
    if leg.venue is VenueName.MATCHBOOK:
        return _recover_matchbook_leg(trade, leg, rows)
    if leg.venue is VenueName.KALSHI:
        return _recover_kalshi_leg(trade, leg, rows)
    return leg


def _recover_matchbook_leg(
    trade: PaperTrade,
    leg: PaperTradeLeg,
    rows: list[Any],
) -> PaperTradeLeg:
    event_id = _clean(leg.source_event_id)
    market_id = _usable_market_id(leg.source_market_id)
    canonical = _clean(trade.canonical_event_id)
    needs_event = not event_id or _is_canonical_slug(event_id, canonical)
    if not needs_event:
        return leg
    recovered_event = None
    recovered_market = market_id
    for row in _rows_for_event(trade, rows):
        row_market = _clean(getattr(row, "matchbook_market_id", None))
        row_event = _clean(getattr(row, "matchbook_event_id", None))
        if not row_event or _is_canonical_slug(row_event, canonical):
            continue
        if market_id and row_market and row_market != market_id:
            continue
        recovered_event = row_event
        recovered_market = recovered_market or row_market
        if market_id and row_market == market_id:
            break
        if recovered_event:
            break
    if recovered_event is None:
        return leg
    updates: dict[str, str] = {"source_event_id": recovered_event}
    if recovered_market and recovered_market != _clean(leg.source_market_id):
        updates["source_market_id"] = recovered_market
    return leg.model_copy(update=updates)


def _recover_kalshi_leg(
    trade: PaperTrade,
    leg: PaperTradeLeg,
    rows: list[Any],
) -> PaperTradeLeg:
    ticker = _clean(leg.source_contract_id) or _usable_market_id(leg.source_market_id)
    event_id = _clean(leg.source_event_id)
    canonical = _clean(trade.canonical_event_id)
    needs_event = not event_id or _is_canonical_slug(event_id, canonical)
    updates: dict[str, str] = {}
    if ticker and not _clean(leg.source_contract_id) and ticker != _clean(leg.source_market_id):
        updates["source_contract_id"] = ticker
    if needs_event:
        recovered_event = None
        for row in _rows_for_event(trade, rows):
            tickers = [
                _clean(item)
                for item in list(getattr(row, "kalshi_market_tickers", None) or [])
            ]
            row_event = _clean(getattr(row, "kalshi_event_ticker", None))
            if ticker and tickers and ticker not in tickers:
                continue
            if row_event:
                recovered_event = row_event
                break
        if recovered_event:
            updates["source_event_id"] = recovered_event
        elif ticker and needs_event and not event_id:
            # Ticker is sufficient for GET /markets/{ticker}; keep event blank.
            pass
    if not updates:
        return leg
    return leg.model_copy(update=updates)


def _rows_for_event(trade: PaperTrade, rows: list[Any]) -> list[Any]:
    canonical = _clean(trade.canonical_event_id)
    if not canonical:
        return list(rows)
    matched = [
        row
        for row in rows
        if _clean(getattr(row, "canonical_event_id", None)) in {canonical, ""}
        or _clean(getattr(row, "canonical_event_id", None)) is None
    ]
    return matched or list(rows)


def _usable_market_id(value: str | None) -> str | None:
    text = _clean(value)
    if not text or text.casefold() in {"unknown", "none", "null"}:
        return None
    return text


def _is_canonical_slug(value: str, canonical: str | None) -> bool:
    if not value:
        return False
    if value.isdigit():
        return False
    if canonical and value == canonical:
        return True
    return False


def _clean(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
