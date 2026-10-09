"""Read-only compact Price-2 observations for the operator Activity feed.

Projects already-stored ``execution_snapshot_audits`` rows and genuine
``paper_fill_rejected`` lifecycle events whose detail starts with an
``execution_reprice_`` reason and does not name a snapshot. It does not
reprice, call providers, or write audits.
"""

from __future__ import annotations

import json
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, Field

from sports_hedge.application.quote_freshness import require_aware_instant
from sports_hedge.arbitrage.watchlist.models import OpportunityLifecycleEvent

PRICE2_ACTIVITY_MAX_OPPORTUNITY_IDS = 100
PRICE2_ACTIVITY_DEFAULT_LIMIT = 200
PRICE2_ACTIVITY_MAX_LIMIT = 500

Price2AttemptStatus = Literal["accepted", "rejected", "incomplete_unavailable"]
Price2Source = Literal["execution_snapshot_audit", "lifecycle_rejection"]


class Price2LegProjection(BaseModel):
    """One stored Price-2 quote leg. Missing fields stay null — never invented."""

    venue: str | None = None
    outcome: str | None = None
    displayed_odds: str | None = None
    requested_stake: str | None = None
    stake_currency: str | None = None
    retrieved_at: datetime | None = None
    quote_age_ms: int | None = None
    slot_wait_ms: int | None = None
    io_ms: int | None = None


class Price2ActivityObservation(BaseModel):
    """One actual Price-2 attempt. Not a Price-1 qualifying event and not a fill."""

    observation_id: str
    source: Price2Source
    opportunity_id: str
    snapshot_id: str | None = None
    execution_cycle: int | None = None
    cycle_outcome: str | None = None
    trade_id: str | None = None
    tranche_id: str | None = None
    occurred_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    elapsed_ms: int | None = None
    status: Price2AttemptStatus
    accepted: bool | None = None
    filled: bool = False
    net_edge: str | None = None
    guaranteed_profit: str | None = None
    execution_size: str | None = None
    execution_size_currency: str | None = None
    oldest_quote_age_ms: int | None = None
    skew_ms: int | None = None
    rejection_reason: str | None = None
    fixture_label: str | None = None
    market_family: str | None = None
    canonical_event_id: str | None = None
    canonical_market_id: str | None = None
    legs: list[Price2LegProjection] = Field(default_factory=list)
    data_kind: Literal["historical_recorded"] = "historical_recorded"


def normalize_opportunity_ids(raw: str | None) -> list[str]:
    """Deduplicate, cap, and reject a missing list so the reader cannot scan globally."""

    if raw is None:
        raise ValueError("opportunity_ids is required")
    seen: set[str] = set()
    ids: list[str] = []
    for part in raw.split(","):
        item = part.strip()
        if not item or item in seen:
            continue
        seen.add(item)
        ids.append(item)
        if len(ids) > PRICE2_ACTIVITY_MAX_OPPORTUNITY_IDS:
            raise ValueError(
                f"at most {PRICE2_ACTIVITY_MAX_OPPORTUNITY_IDS} opportunity_ids"
            )
    if not ids:
        raise ValueError("opportunity_ids is required")
    return ids


def project_audit_row(row: dict[str, Any]) -> Price2ActivityObservation:
    """Compact projection of one stored snapshot audit. Ignores frozen accounts."""

    snapshot = _object(_parse_json(row.get("snapshot_json")))
    diagnostics = _object(_parse_json(row.get("diagnostics_json")))
    started_at = _instant(snapshot.get("started_at")) or _instant(diagnostics.get("started_at"))
    finished_at = _instant(snapshot.get("evaluated_at")) or _instant(row.get("occurred_at"))
    occurred_at = _instant(row.get("occurred_at")) or finished_at or started_at
    if occurred_at is None:
        raise ValueError("execution snapshot audit missing occurred_at")
    accepted = _as_bool(row.get("accepted"))
    cycle_outcome = _text(row.get("cycle_outcome") or snapshot.get("cycle_outcome"))
    trade_id = _text(row.get("trade_id") or snapshot.get("trade_id"))
    frozen = snapshot.get("frozen_orders") if isinstance(snapshot.get("frozen_orders"), list) else []
    timing = snapshot.get("timing") if isinstance(snapshot.get("timing"), dict) else {}
    calls = _timing_calls(timing, diagnostics)
    legs = _project_legs(snapshot.get("legs"), frozen, calls)
    size, size_currency = _execution_size(legs)
    assembly_ms = _int(timing.get("assembly_ms"))
    if assembly_ms is None:
        assembly_ms = _int(diagnostics.get("assembly_ms"))
    return Price2ActivityObservation(
        observation_id=str(row.get("snapshot_id") or snapshot.get("snapshot_id") or ""),
        source="execution_snapshot_audit",
        opportunity_id=str(row.get("opportunity_id") or ""),
        snapshot_id=_text(row.get("snapshot_id") or snapshot.get("snapshot_id")),
        execution_cycle=_int(row.get("execution_cycle") or snapshot.get("execution_cycle")),
        cycle_outcome=cycle_outcome,
        trade_id=trade_id,
        tranche_id=_text(row.get("tranche_id") or snapshot.get("tranche_id")),
        occurred_at=occurred_at,
        started_at=started_at,
        finished_at=finished_at,
        elapsed_ms=_elapsed_ms(started_at, finished_at, assembly_ms),
        status="accepted" if accepted else "rejected",
        accepted=accepted,
        filled=_filled(cycle_outcome, trade_id),
        net_edge=_text(snapshot.get("net_edge")),
        guaranteed_profit=_text(snapshot.get("guaranteed_profit")),
        execution_size=size,
        execution_size_currency=size_currency,
        oldest_quote_age_ms=_int(snapshot.get("oldest_quote_age_ms")),
        skew_ms=_int(snapshot.get("skew_ms")),
        rejection_reason=_text(row.get("rejection_reason") or snapshot.get("rejection_reason")),
        canonical_market_id=_text(row.get("canonical_market_id") or snapshot.get("canonical_market_id")),
        legs=legs,
    )


def project_lifecycle_rejection(
    event: OpportunityLifecycleEvent,
) -> Price2ActivityObservation | None:
    """Price-2 miss with no snapshot. Returns None when the event is not evidence."""

    reason = _lifecycle_reprice_reason(event.detail)
    if reason is None:
        return None
    if event.detail and "snapshot_id=" in event.detail:
        return None
    return Price2ActivityObservation(
        observation_id=event.event_id,
        source="lifecycle_rejection",
        opportunity_id=event.opportunity_id,
        occurred_at=event.occurred_at,
        status="incomplete_unavailable",
        accepted=False,
        filled=False,
        rejection_reason=reason,
        fixture_label=event.fixture_label,
        market_family=event.market_family,
        canonical_event_id=event.canonical_event_id,
        canonical_market_id=event.canonical_market_id,
        net_edge=None,
        guaranteed_profit=None,
        legs=[],
    )


def merge_price2_observations(
    audits: list[Price2ActivityObservation],
    rejections: list[Price2ActivityObservation],
) -> list[Price2ActivityObservation]:
    """Newest-first. Lifecycle rows that duplicate a snapshot stay dropped."""

    known_snapshots = {
        item.snapshot_id for item in audits if item.snapshot_id
    }
    extra = [
        item
        for item in rejections
        if item.snapshot_id is None or item.snapshot_id not in known_snapshots
    ]
    merged = [*audits, *extra]
    merged.sort(
        key=lambda item: (
            item.occurred_at,
            item.snapshot_id or "",
            item.observation_id,
        ),
        reverse=True,
    )
    return merged


def _lifecycle_reprice_reason(detail: str | None) -> str | None:
    if not detail:
        return None
    token = detail.strip().split(None, 1)[0]
    if token.startswith("execution_reprice_"):
        return token
    return None


def _project_legs(
    raw_legs: Any,
    frozen_orders: list[Any],
    calls: dict[str, tuple[int | None, int | None]],
) -> list[Price2LegProjection]:
    if not isinstance(raw_legs, list):
        return []
    currencies = _currencies_by_venue(frozen_orders)
    legs: list[Price2LegProjection] = []
    for item in raw_legs:
        if not isinstance(item, dict):
            continue
        venue = _text(item.get("venue"))
        wait_io = calls.get(venue or "")
        retrieved = _instant(item.get("retrieved_at"))
        legs.append(
            Price2LegProjection(
                venue=venue,
                outcome=_text(item.get("outcome")),
                displayed_odds=_text(item.get("displayed_odds")),
                requested_stake=_text(item.get("requested_stake")),
                stake_currency=currencies.get(venue or "") or _text(item.get("currency")),
                retrieved_at=retrieved,
                quote_age_ms=_int(item.get("quote_age_ms")),
                slot_wait_ms=None if wait_io is None else wait_io[0],
                io_ms=None if wait_io is None else wait_io[1],
            )
        )
    return legs


def _currencies_by_venue(frozen_orders: list[Any]) -> dict[str, str]:
    found: dict[str, str] = {}
    for item in frozen_orders:
        if not isinstance(item, dict):
            continue
        venue = _text(item.get("venue"))
        currency = _text(item.get("currency"))
        if venue and currency and venue not in found:
            found[venue] = currency
    return found


def _timing_calls(timing: dict[str, Any], diagnostics: dict[str, Any]) -> dict[str, tuple[int | None, int | None]]:
    rows = timing.get("calls")
    if not isinstance(rows, list):
        rows = diagnostics.get("calls")
    by_venue: dict[str, tuple[int | None, int | None]] = {}
    if not isinstance(rows, list):
        return by_venue
    for item in rows:
        if not isinstance(item, dict):
            continue
        venue = _text(item.get("venue"))
        if not venue:
            continue
        wait = _int(item.get("slot_wait_ms"))
        io_ms = _int(item.get("io_ms"))
        previous = by_venue.get(venue)
        if previous is None:
            by_venue[venue] = (wait, io_ms)
            continue
        by_venue[venue] = (
            _max_optional(previous[0], wait),
            _max_optional(previous[1], io_ms),
        )
    return by_venue


def _execution_size(legs: list[Price2LegProjection]) -> tuple[str | None, str | None]:
    if not legs:
        return None, None
    currencies = {leg.stake_currency for leg in legs}
    if None in currencies or len(currencies) != 1:
        return None, None
    total = Decimal(0)
    for leg in legs:
        stake = _decimal(leg.requested_stake)
        if stake is None:
            return None, None
        total += stake
    currency = next(iter(currencies))
    return str(total), currency


def _filled(cycle_outcome: str | None, trade_id: str | None) -> bool:
    if cycle_outcome == "filled":
        return True
    return bool(trade_id)


def _elapsed_ms(
    started_at: datetime | None,
    finished_at: datetime | None,
    assembly_ms: int | None,
) -> int | None:
    if started_at is not None and finished_at is not None:
        return max(0, int((finished_at - started_at).total_seconds() * 1000))
    return assembly_ms


def _parse_json(raw: Any) -> Any:
    if raw is None or raw == "":
        return None
    if isinstance(raw, (dict, list)):
        return raw
    if not isinstance(raw, str):
        return None
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        return None


def _object(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _text(value: Any) -> str | None:
    if value is None or value == "":
        return None
    return str(value)


def _int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return value in (1, "1", "true", "True")


def _decimal(value: Any) -> Decimal | None:
    if value is None or value == "":
        return None
    try:
        return Decimal(str(value))
    except (InvalidOperation, ValueError):
        return None


def _max_optional(left: int | None, right: int | None) -> int | None:
    if left is None:
        return right
    if right is None:
        return left
    return max(left, right)


def _instant(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        try:
            return require_aware_instant(value, "price2_activity")
        except ValueError:
            return None
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    try:
        return require_aware_instant(parsed, "price2_activity")
    except ValueError:
        return None
