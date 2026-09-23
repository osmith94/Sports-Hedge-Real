"""Recover durable venue identity for PAPER auto-settlement.

Legacy OPEN trades may store a Kalshi ticker without ``source_event_id``, or a
Matchbook market id whose event id lives on the approved catalogue rather than
the persisted leg. Auto-settlement must consume those alternate fields instead
of failing closed as ``missing_durable_provider_identity``.

Legacy TOTAL_GOALS / NFL line-parameter trades may also persist ``line=None``.
Recover that canonical line only from an exact unique catalogue native-identity
match. Never fuzzy-match labels, scores, or elapsed time.

Never treats a canonical event slug as a native Matchbook event id.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Iterable

from sports_hedge.domain.football import (
    FootballPeriod,
    MarketFamily,
    format_stored_line,
    line_push_possible,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.trades import (
    PaperTrade,
    PaperTradeAuditEvent,
    PaperTradeAuditEventType,
    PaperTradeLeg,
    PaperTradeState,
)

LEGACY_MARKET_LINE_RECOVERED_DETAIL = "legacy_market_line_recovered_from_catalogue"
LINE_RECOVERY_FAMILIES = frozenset(
    {
        MarketFamily.TOTAL_GOALS,
        MarketFamily.POINT_SPREAD,
        MarketFamily.TOTAL_POINTS,
    }
)
_SETTLEABLE_LINE_STATES = frozenset({PaperTradeState.OPEN, PaperTradeState.PARTIAL})


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


def recover_persisted_catalogue_identity(
    trade: PaperTrade,
    *,
    catalogue_rows: Iterable[Any] | None = None,
    now: datetime | None = None,
) -> tuple[PaperTrade, bool]:
    """Recover native provider IDs, then a unique canonical line if still missing."""

    rows = list(catalogue_rows or [])
    trade, ids_recovered = recover_persisted_provider_identity(trade, catalogue_rows=rows)
    trade, line_recovered = recover_legacy_market_line(
        trade, catalogue_rows=rows, now=now
    )
    return trade, ids_recovered or line_recovered


def recover_legacy_market_line(
    trade: PaperTrade,
    *,
    catalogue_rows: Iterable[Any] | None = None,
    now: datetime | None = None,
) -> tuple[PaperTrade, bool]:
    """Persist a unique catalogue line onto a legacy OPEN/PARTIAL PAPER trade.

    Prefers an already-stored ``trade.line``. Recovers only when exactly one
    catalogue row matches the trade's durable native market identity, family,
    and period, and that line remains safe (half-line, no push). Ambiguity,
    missing identity, family mismatch, and integer/push lines fail closed.
    """

    if trade.line is not None:
        return trade, False
    if trade.state not in _SETTLEABLE_LINE_STATES:
        return trade, False
    family = trade.market_family
    if family not in LINE_RECOVERY_FAMILIES:
        return trade, False
    rows = list(catalogue_rows or [])
    match = _unique_catalogue_line_match(trade, rows)
    if match is None:
        return trade, False
    line, row_id, register_key = match
    if line_push_possible(line) is not False:
        return trade, False
    when = now or datetime.now(UTC)
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    detail = (
        f"{LEGACY_MARKET_LINE_RECOVERED_DETAIL} "
        f"line={format_stored_line(line)} "
        f"catalogue_row_id={row_id or ''} "
        f"register_canonical_key={register_key or ''}"
    ).strip()
    audit = list(trade.audit)
    audit.append(
        PaperTradeAuditEvent(
            occurred_at=when,
            event_type=PaperTradeAuditEventType.LEGACY_MARKET_LINE_RECOVERED,
            detail=detail,
        )
    )
    return trade.model_copy(update={"line": line, "audit": audit, "last_updated_at": when}), True


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


def catalogue_rows_for_trade(catalogue: Any, trade: PaperTrade) -> list[Any]:
    """Load exact catalogue rows for the trade's canonical event. Empty if unknown."""

    event_id = _clean(trade.canonical_event_id)
    if catalogue is None or not event_id:
        return []
    getter = getattr(catalogue, "list_rows_for_event", None)
    if not callable(getter):
        return []
    try:
        rows = getter(event_id)
    except Exception:
        return []
    return list(rows or [])


def _unique_catalogue_line_match(
    trade: PaperTrade,
    rows: list[Any],
) -> tuple[Decimal, str | None, str | None] | None:
    tokens = _trade_native_market_tokens(trade)
    if not tokens.has_any:
        return None
    family = _family_value(trade.market_family)
    period = _period_value(trade.period)
    matches: list[tuple[Decimal, str | None, str | None]] = []
    seen_rows: set[tuple[Any, ...]] = set()
    for row in _rows_for_exact_event(trade, rows):
        if not _row_matches_exact_market_identity(row, tokens):
            continue
        row_family, row_period, line, register_key = _row_family_period_line(row)
        if not family or not row_family or row_family != family:
            continue
        if period and (not row_period or row_period != period):
            continue
        if line is None:
            continue
        identity = (
            _clean(getattr(row, "catalogue_row_id", None)),
            register_key,
            format_stored_line(line),
        )
        if identity in seen_rows:
            continue
        seen_rows.add(identity)
        matches.append(
            (
                line,
                _clean(getattr(row, "catalogue_row_id", None)),
                register_key,
            )
        )
    if not matches:
        return None
    unique_lines = {item[0] for item in matches}
    unique_keys = {item[2] for item in matches if item[2]}
    if len(unique_lines) != 1 or len(unique_keys) > 1:
        return None
    return matches[0]


def _rows_for_exact_event(trade: PaperTrade, rows: list[Any]) -> list[Any]:
    canonical = _clean(trade.canonical_event_id)
    if not canonical:
        return []
    return [
        row
        for row in rows
        if _clean(getattr(row, "canonical_event_id", None)) == canonical
    ]


def _trade_native_market_tokens(trade: PaperTrade) -> "_NativeTokens":
    matchbook_markets: set[str] = set()
    kalshi_tickers: set[str] = set()
    polymarket_ids: set[str] = set()
    for leg in trade.legs:
        if leg.venue is VenueName.MATCHBOOK:
            market_id = _usable_market_id(leg.source_market_id)
            if market_id:
                matchbook_markets.add(market_id)
            continue
        if leg.venue is VenueName.KALSHI:
            ticker = _clean(leg.source_contract_id) or _usable_market_id(leg.source_market_id)
            if ticker:
                kalshi_tickers.add(ticker)
            continue
        if leg.venue is VenueName.POLYMARKET:
            for value in (
                leg.source_market_id,
                leg.source_contract_id,
                leg.source_runner_id,
            ):
                token = _usable_market_id(value)
                if token:
                    polymarket_ids.add(token)
    return _NativeTokens(matchbook_markets, kalshi_tickers, polymarket_ids)


def _row_matches_exact_market_identity(row: Any, tokens: "_NativeTokens") -> bool:
    row_market = _usable_market_id(getattr(row, "matchbook_market_id", None))
    if row_market and row_market in tokens.matchbook_markets:
        return True
    tickers = {
        item
        for item in (_clean(value) for value in list(getattr(row, "kalshi_market_tickers", None) or []))
        if item
    }
    if tickers & tokens.kalshi_tickers:
        return True
    polymarket_ids = set()
    for attr in ("polymarket_market_id", "polymarket_condition_id"):
        value = _usable_market_id(getattr(row, attr, None))
        if value:
            polymarket_ids.add(value)
    for item in list(getattr(row, "polymarket_token_ids", None) or []):
        native = _usable_market_id(getattr(item, "native_id", None) or item)
        if native:
            polymarket_ids.add(native)
    for item in list(getattr(row, "polymarket_clob_token_ids", None) or []):
        native = _usable_market_id(item)
        if native:
            polymarket_ids.add(native)
    return bool(polymarket_ids & tokens.polymarket_ids)


def _row_family_period_line(
    row: Any,
) -> tuple[str | None, str | None, Decimal | None, str | None]:
    key = _clean(getattr(row, "register_canonical_key", None))
    family = _family_value(getattr(row, "family", None))
    period = _period_value(getattr(row, "period", None))
    line = _parse_decimal(getattr(row, "line", None))
    if key:
        key_family, key_period, key_line = _parse_register_canonical_key(key)
        family = family or key_family
        period = period or key_period
        if line is None:
            line = _parse_decimal(key_line)
    return family, period, line, key


def _parse_register_canonical_key(
    key: str,
) -> tuple[str | None, str | None, str | None]:
    if key.startswith("TOTAL_GOALS_FT:"):
        return MarketFamily.TOTAL_GOALS.value, FootballPeriod.FULL_TIME.value, key.split(":", 1)[1]
    if key.startswith("NFL_POINT_SPREAD_FT:"):
        return MarketFamily.POINT_SPREAD.value, FootballPeriod.FULL_TIME.value, key.split(":", 1)[1]
    if key.startswith("NFL_TOTAL_POINTS_FT:"):
        return MarketFamily.TOTAL_POINTS.value, FootballPeriod.FULL_TIME.value, key.split(":", 1)[1]
    if key == "NBA_GAME_WINNER_FT":
        return MarketFamily.GAME_WINNER.value, FootballPeriod.FULL_TIME.value, None
    if key.startswith("NBA_POINT_SPREAD_FT:"):
        return MarketFamily.POINT_SPREAD.value, FootballPeriod.FULL_TIME.value, key.split(":", 1)[1]
    if key.startswith("NBA_TOTAL_POINTS_FT:"):
        return MarketFamily.TOTAL_POINTS.value, FootballPeriod.FULL_TIME.value, key.split(":", 1)[1]
    return None, None, None


def _family_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, MarketFamily):
        return value.value
    text = _clean(value)
    if not text:
        return None
    try:
        return MarketFamily(text).value
    except ValueError:
        return text


def _period_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, FootballPeriod):
        return value.value
    text = _clean(value)
    if not text:
        return None
    try:
        return FootballPeriod(text).value
    except ValueError:
        return text


def _parse_decimal(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    text = _clean(value)
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


class _NativeTokens:
    def __init__(
        self,
        matchbook_markets: set[str],
        kalshi_tickers: set[str],
        polymarket_ids: set[str],
    ) -> None:
        self.matchbook_markets = matchbook_markets
        self.kalshi_tickers = kalshi_tickers
        self.polymarket_ids = polymarket_ids

    @property
    def has_any(self) -> bool:
        return bool(self.matchbook_markets or self.kalshi_tickers or self.polymarket_ids)
