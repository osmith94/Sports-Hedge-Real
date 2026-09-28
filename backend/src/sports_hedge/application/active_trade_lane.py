"""ACTIVE TRADE lane: 5s exact-ID repricing of fully hedged OPEN paper trades.

No discovery, rematching, or equivalence re-proof. Uses the shared provider
access layer with priority above HOT. Does not raise provider concurrency.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from threading import RLock
from typing import Any

from sports_hedge.application.approved_market_catalogue import (
    DerivedPriceEngineItem,
    OutcomeNativeId,
    required_outcomes_for_key,
)
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.football import MarketFamily, format_stored_line
from sports_hedge.domain.models import VenueName
from sports_hedge.lifecycle.paper import decide_active_trade_membership
from sports_hedge.paper.trades import (
    PaperActiveTradePhase,
    PaperTrade,
    PaperTradeLeg,
)

ACTIVE_TRADE_LANE = "active_trade"
DEFAULT_ACTIVE_TRADE_CADENCE_SECONDS = 5
# Round-robin every currently due OPEN trade. Provider caps, not this bound,
# limit concurrency. 64 is only a pathological ceiling.
MAX_ACTIVE_TRADES_PER_TICK = 64


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

    def mark_due_now(
        self,
        trade_id: str,
        *,
        now: datetime,
        phase: PaperActiveTradePhase | None = None,
    ) -> None:
        """Schedule the next exact-ID refresh immediately. Used after a partial buy."""

        with self._lock:
            member = self._members.get(trade_id)
            if member is None:
                return
            member.next_due_at = now
            if phase is not None:
                member.phase = phase

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

    def cadence_counts(self, now: datetime) -> tuple[int, int, int]:
        """Return (open, due, overdue) membership counts at ``now``."""

        with self._lock:
            open_n = len(self._members)
            due_n = 0
            overdue_n = 0
            for member in self._members.values():
                if member.next_due_at is None or now >= member.next_due_at:
                    due_n += 1
                    if member.next_due_at is not None and now > member.next_due_at:
                        overdue_n += 1
            return open_n, due_n, overdue_n

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


def identity_from_open_trade(
    trade: PaperTrade,
    catalogue_rows: list[Any] | None = None,
) -> DerivedPriceEngineItem | None:
    """Exact native IDs already on the OPEN trade. Never rediscovers markets.

    ``catalogue_row_id`` stays ``active-trade:<trade_id>`` and ``content_version``
    stays 0. Those synthetic fields are not a catalogue-version gate. Kickoff,
    register key, exact line, fee-snapshot id, and the full approved outcome
    map are copied from the unique durable catalogue row whose native IDs are
    the ones this trade already filled. That row is persisted approved
    evidence. It is not a new equivalence proof and it is not a provider call.
    """

    identity = _identity_from_trade_legs(trade)
    if identity is None:
        return None
    return _attach_persisted_approved_identity(identity, trade, list(catalogue_rows or []))


def _identity_from_trade_legs(trade: PaperTrade) -> DerivedPriceEngineItem | None:
    if decide_active_trade_membership(trade.state).accepted is False:
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
            ticker, native_id = _kalshi_leg_native_ids(leg)
            if ticker and ticker not in kalshi_tickers:
                kalshi_tickers.append(ticker)
            if ticker and native_id:
                kalshi_outcomes.append(OutcomeNativeId(outcome=leg.outcome, native_id=native_id))
    if not matchbook_event or not matchbook_market or not kalshi_event or not kalshi_tickers:
        return None
    canonical = trade.canonical_event_id or trade.trade_id
    # Exact Matchbook get_market must receive the native event id. Never
    # substitute the canonical event id as a rediscovery fallback.
    if str(matchbook_event) == str(canonical) and not str(matchbook_event).isdigit():
        return None
    if str(kalshi_event) == str(canonical) and not str(kalshi_event).isdigit():
        return None
    filled = sorted({leg.outcome for leg in trade.legs if leg.filled_stake > 0})
    return DerivedPriceEngineItem(
        catalogue_row_id=f"active-trade:{trade.trade_id}",
        content_version=0,
        canonical_event_id=canonical,
        register_canonical_key=trade.canonical_market_id or trade.opportunity_id,
        matchbook_event_id=str(matchbook_event),
        matchbook_market_id=matchbook_market,
        matchbook_runner_ids=matchbook_runners,
        kalshi_event_ticker=kalshi_event,
        kalshi_market_tickers=kalshi_tickers,
        kalshi_outcome_ids=kalshi_outcomes,
        family=None if trade.market_family is None else trade.market_family.value,
        period=None if trade.period is None else trade.period.value,
        line=format_stored_line(trade.line),
        required_outcomes=filled,
        competition=trade.competition,
        home_canonical=trade.home_team,
        away_canonical=trade.away_team,
    )


def _kalshi_leg_native_ids(leg: PaperTradeLeg) -> tuple[str | None, str | None]:
    """Ticker plus the YES/NO runner id actually filled.

    Paper legs store the contract ticker on ``source_market_id`` and the
    sided runner (``TICKER:YES`` / ``TICKER:NO``) on ``source_runner_id``.
    Pricing the runner as the bare ticker treats every side as YES.
    """

    runner = str(leg.source_runner_id or "").strip()
    market = str(leg.source_contract_id or leg.source_market_id or "").strip()
    if runner.endswith(":YES") or runner.endswith(":NO"):
        ticker = runner.rsplit(":", 1)[0].strip() or market
        if not ticker:
            return None, None
        return ticker, runner
    ticker = market or runner
    if not ticker:
        return None, None
    return ticker, runner or ticker


def _attach_persisted_approved_identity(
    identity: DerivedPriceEngineItem,
    trade: PaperTrade,
    catalogue_rows: list[Any],
) -> DerivedPriceEngineItem:
    """Copy durable approved fields. Never replace the filled native IDs."""

    row = _unique_approved_row(trade, identity, catalogue_rows)
    if row is None:
        return identity
    register_key = str(getattr(row, "register_canonical_key", "") or "").strip()
    updates: dict[str, Any] = {}
    kickoff = getattr(row, "kickoff_utc", None)
    if kickoff is not None:
        updates["kickoff_utc"] = kickoff
    if register_key and not _looks_like_register_key(identity.register_canonical_key):
        updates["register_canonical_key"] = register_key
    row_line = format_stored_line(_decimal_or_none(getattr(row, "line", None)))
    if not row_line and register_key:
        _family, _period, key_line = _line_from_register_key(register_key)
        row_line = format_stored_line(_decimal_or_none(key_line))
    if identity.line is None and row_line:
        updates["line"] = row_line
    fee_id = str(getattr(row, "kalshi_fee_snapshot_id", "") or "").strip()
    if fee_id:
        updates["kalshi_fee_snapshot_id"] = fee_id
    outcomes = list(getattr(row, "kalshi_outcome_ids", None) or [])
    if outcomes and _outcomes_cover_trade_tickers(outcomes, identity.kalshi_market_tickers):
        updates["kalshi_outcome_ids"] = outcomes
    required = list(getattr(row, "required_outcomes", None) or [])
    if not required and register_key:
        required = required_outcomes_for_key(register_key)
    if required:
        updates["required_outcomes"] = list(required)
    family = getattr(row, "family", None)
    if family and not identity.family:
        updates["family"] = family if isinstance(family, str) else getattr(family, "value", str(family))
    period = getattr(row, "period", None)
    if period and not identity.period:
        updates["period"] = period if isinstance(period, str) else getattr(period, "value", str(period))
    if not identity.competition and getattr(row, "competition", None):
        updates["competition"] = row.competition
    if not identity.home_canonical and getattr(row, "home_canonical", None):
        updates["home_canonical"] = row.home_canonical
    if not identity.away_canonical and getattr(row, "away_canonical", None):
        updates["away_canonical"] = row.away_canonical
    # Synthetic ACTIVE identity is intentional. A catalogue content version is
    # not required, and this must not collide with the HOT working-set key.
    updates["catalogue_row_id"] = identity.catalogue_row_id
    updates["content_version"] = 0
    return identity.model_copy(update=updates)


def _unique_approved_row(
    trade: PaperTrade,
    identity: DerivedPriceEngineItem,
    catalogue_rows: list[Any],
) -> Any | None:
    matches: list[Any] = []
    seen: set[str] = set()
    trade_event = str(trade.canonical_event_id or "").strip()
    for row in catalogue_rows:
        row_id = str(getattr(row, "catalogue_row_id", "") or "").strip()
        if not row_id or row_id in seen:
            continue
        row_event = str(getattr(row, "canonical_event_id", "") or "").strip()
        if trade_event and row_event and trade_event != row_event:
            continue
        if not _row_matches_filled_native_ids(row, identity):
            continue
        if not _row_line_agrees(trade, row):
            continue
        family = _family_value(getattr(row, "family", None))
        trade_family = _family_value(trade.market_family)
        if trade_family and family and trade_family != family:
            continue
        seen.add(row_id)
        matches.append(row)
    if len(matches) != 1:
        return None
    return matches[0]


def _row_matches_filled_native_ids(row: Any, identity: DerivedPriceEngineItem) -> bool:
    row_market = str(getattr(row, "matchbook_market_id", "") or "").strip()
    trade_market = str(identity.matchbook_market_id or "").strip()
    if not row_market or not trade_market or row_market != trade_market:
        return False
    row_event = str(getattr(row, "matchbook_event_id", "") or "").strip()
    trade_event = str(identity.matchbook_event_id or "").strip()
    if row_event and trade_event and row_event != trade_event:
        return False
    row_kalshi_event = str(getattr(row, "kalshi_event_ticker", "") or "").strip()
    trade_kalshi_event = str(identity.kalshi_event_ticker or "").strip()
    if row_kalshi_event and trade_kalshi_event and row_kalshi_event != trade_kalshi_event:
        return False
    row_tickers = {
        str(item).strip()
        for item in list(getattr(row, "kalshi_market_tickers", None) or [])
        if str(item).strip()
    }
    trade_tickers = {str(item).strip() for item in identity.kalshi_market_tickers if str(item).strip()}
    return bool(trade_tickers) and trade_tickers <= row_tickers


def _row_line_agrees(trade: PaperTrade, row: Any) -> bool:
    if trade.line is None:
        return True
    row_line = _decimal_or_none(getattr(row, "line", None))
    if row_line is None:
        register_key = str(getattr(row, "register_canonical_key", "") or "")
        _family, _period, key_line = _line_from_register_key(register_key)
        row_line = _decimal_or_none(key_line)
    if row_line is None:
        return True
    return row_line == trade.line


def _outcomes_cover_trade_tickers(
    outcomes: list[Any],
    tickers: list[str],
) -> bool:
    covered: set[str] = set()
    for item in outcomes:
        native = str(getattr(item, "native_id", "") or "").strip()
        if not native:
            continue
        ticker = native.rsplit(":", 1)[0].strip() if native.endswith((":YES", ":NO")) else native
        if ticker:
            covered.add(ticker)
    needed = {str(item).strip() for item in tickers if str(item).strip()}
    return bool(needed) and needed <= covered


def _looks_like_register_key(value: str | None) -> bool:
    key = str(value or "").strip()
    if not key or key.startswith("mkt:") or key.startswith("evt:") or key.startswith("active-trade:"):
        return False
    if key in {"MATCH_RESULT_FT", "BTTS_FT", "FTTS_FT"}:
        return True
    return ":" in key and not key.startswith("mkt")


def _line_from_register_key(key: str) -> tuple[str | None, str | None, str | None]:
    if key.startswith("TOTAL_GOALS_FT:"):
        return MarketFamily.TOTAL_GOALS.value, "full_time", key.split(":", 1)[1]
    if key.startswith("MLB_TOTAL_RUNS_FT:"):
        return MarketFamily.TOTAL_RUNS.value, "full_time", key.split(":", 1)[1]
    if ":FT:" in key or (key.startswith(("NFL_", "NBA_", "NCAAB_")) and ":" in key):
        family = None
        if "TOTAL" in key:
            family = MarketFamily.TOTAL_POINTS.value
        elif "SPREAD" in key:
            family = MarketFamily.POINT_SPREAD.value
        return family, "full_time", key.split(":", 1)[1]
    return None, None, None


def _family_value(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, MarketFamily):
        return value.value
    text = str(value).strip()
    if not text:
        return None
    try:
        return MarketFamily(text).value
    except ValueError:
        return text


def _decimal_or_none(value: Any) -> Decimal | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text)
    except (InvalidOperation, ValueError):
        return None


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
