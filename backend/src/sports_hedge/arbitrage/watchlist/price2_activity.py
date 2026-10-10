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

from sports_hedge.application.execution_snapshot import (
    DETAILS_NOT_RECORDED_REASON,
    NATIVE_ORDER_FREEZE_REASONS,
)
from sports_hedge.application.quote_freshness import require_aware_instant
from sports_hedge.arbitrage.watchlist.models import OpportunityLifecycleEvent

EconomicsThresholdStatus = Literal[
    "below_configured_threshold",
    "meets_or_exceeds_configured_threshold",
    "threshold_not_recorded",
    "net_edge_not_recorded",
]
NativeFreezeStatus = Literal["frozen", "not_frozen", "details_not_recorded"]

PRICE2_ACTIVITY_MAX_OPPORTUNITY_IDS = 100
PRICE2_ACTIVITY_DEFAULT_LIMIT = 200
PRICE2_ACTIVITY_MAX_LIMIT = 500
PRICE2_ACTIVITY_RECENT_LIMIT = 50

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
    timing_match: Literal["native_id"] | None = None
    native_market_id: str | None = None
    native_runner_id: str | None = None
    native_frozen: bool | None = None
    freeze_status: NativeFreezeStatus = "details_not_recorded"
    freeze_reason: str | None = None
    observed_tick_size: str | None = None
    observed_minimum_shares: str | None = None
    intended_native_stake: str | None = None
    intended_native_shares: str | None = None
    intended_limit_price: str | None = None


class Price2VenueTiming(BaseModel):
    """Max slot wait/I/O across provider calls for one venue. Not a per-leg fact."""

    venue: str
    slot_wait_ms: int | None = None
    io_ms: int | None = None
    call_count: int = 0
    aggregation: Literal["venue_max"] = "venue_max"


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
    trade_linked: bool = False
    net_edge: str | None = None
    guaranteed_profit: str | None = None
    execution_size: str | None = None
    execution_size_currency: str | None = None
    oldest_quote_age_ms: int | None = None
    skew_ms: int | None = None
    rejection_reason: str | None = None
    minimum_net_edge: str | None = None
    minimum_net_edge_source: str | None = None
    economics_vs_threshold: EconomicsThresholdStatus | None = None
    native_order_freeze_recorded: bool = False
    fixture_label: str | None = None
    market_family: str | None = None
    canonical_event_id: str | None = None
    canonical_market_id: str | None = None
    legs: list[Price2LegProjection] = Field(default_factory=list)
    venue_timings: list[Price2VenueTiming] = Field(default_factory=list)
    data_kind: Literal["historical_recorded"] = "historical_recorded"


def normalize_opportunity_ids(raw: str | None, *, required: bool = True) -> list[str]:
    """Deduplicate and cap. Empty is allowed when a bounded recent window is used."""

    if raw is None or not raw.strip():
        if required:
            raise ValueError("opportunity_ids is required")
        return []
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
    if required and not ids:
        raise ValueError("opportunity_ids is required")
    return ids


def project_audit_row(row: dict[str, Any]) -> Price2ActivityObservation:
    """Compact projection of one stored snapshot audit. Ignores frozen accounts."""

    snapshot = _object(_parse_json(row.get("snapshot_json")))
    diagnostics = _object(_parse_json(row.get("diagnostics_json")))
    started_at = _instant(snapshot.get("started_at")) or _instant(diagnostics.get("started_at"))
    evaluated_at = _instant(snapshot.get("evaluated_at"))
    occurred_at = _instant(row.get("occurred_at")) or started_at
    if occurred_at is None:
        raise ValueError("execution snapshot audit missing occurred_at")
    accepted = _as_bool(row.get("accepted"))
    cycle_outcome = _text(row.get("cycle_outcome") or snapshot.get("cycle_outcome"))
    trade_id = _text(row.get("trade_id") or snapshot.get("trade_id"))
    frozen = snapshot.get("frozen_orders") if isinstance(snapshot.get("frozen_orders"), list) else []
    freeze_diagnostics_raw = snapshot.get("native_order_freeze_diagnostics")
    freeze_recorded = isinstance(freeze_diagnostics_raw, list)
    freeze_diagnostics = freeze_diagnostics_raw if freeze_recorded else None
    timing = snapshot.get("timing") if isinstance(snapshot.get("timing"), dict) else {}
    calls = _provider_calls(timing, diagnostics)
    legs = _project_legs(snapshot.get("legs"), frozen, calls, freeze_diagnostics)
    size, size_currency = _execution_size(legs)
    minimum_net_edge = _text(snapshot.get("minimum_net_edge"))
    net_edge = _text(snapshot.get("net_edge"))
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
        finished_at=evaluated_at,
        elapsed_ms=_quote_evaluation_elapsed_ms(started_at, evaluated_at),
        status="accepted" if accepted else "rejected",
        accepted=accepted,
        filled=_filled(cycle_outcome),
        trade_linked=bool(trade_id),
        net_edge=net_edge,
        guaranteed_profit=_text(snapshot.get("guaranteed_profit")),
        execution_size=size,
        execution_size_currency=size_currency,
        oldest_quote_age_ms=_int(snapshot.get("oldest_quote_age_ms")),
        skew_ms=_int(snapshot.get("skew_ms")),
        rejection_reason=_text(row.get("rejection_reason") or snapshot.get("rejection_reason")),
        minimum_net_edge=minimum_net_edge,
        minimum_net_edge_source=_text(snapshot.get("minimum_net_edge_source")),
        economics_vs_threshold=_economics_vs_threshold(net_edge, minimum_net_edge),
        native_order_freeze_recorded=freeze_recorded,
        canonical_market_id=_text(row.get("canonical_market_id") or snapshot.get("canonical_market_id")),
        legs=legs,
        venue_timings=_venue_timings(calls),
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
        trade_linked=False,
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
    calls: list[dict[str, Any]],
    freeze_diagnostics: list[Any] | None,
) -> list[Price2LegProjection]:
    if not isinstance(raw_legs, list):
        return []
    currencies = _currencies_by_venue(frozen_orders)
    used_diagnostics: set[int] = set()
    legs: list[Price2LegProjection] = []
    for item in raw_legs:
        if not isinstance(item, dict):
            continue
        venue = _text(item.get("venue"))
        matched = _call_for_leg(item, calls)
        retrieved = _instant(item.get("retrieved_at"))
        freeze = _freeze_for_leg(item, freeze_diagnostics, used_diagnostics)
        freeze_status, freeze_reason, native_frozen = _freeze_projection(freeze, freeze_diagnostics)
        legs.append(
            Price2LegProjection(
                venue=venue,
                outcome=_text(item.get("outcome")),
                displayed_odds=_text(item.get("displayed_odds")),
                requested_stake=_text(item.get("requested_stake")),
                stake_currency=currencies.get(venue or "") or _text(item.get("currency")),
                retrieved_at=retrieved,
                quote_age_ms=_int(item.get("quote_age_ms")),
                slot_wait_ms=None if matched is None else _int(matched.get("slot_wait_ms")),
                io_ms=None if matched is None else _int(matched.get("io_ms")),
                timing_match=None if matched is None else "native_id",
                native_market_id=_text(item.get("native_market_id")),
                native_runner_id=_text(item.get("native_runner_id")),
                native_frozen=native_frozen,
                freeze_status=freeze_status,
                freeze_reason=freeze_reason,
                observed_tick_size=_safe_numeric_field(freeze, "observed_tick_size"),
                observed_minimum_shares=_safe_numeric_field(freeze, "observed_minimum_shares"),
                intended_native_stake=_safe_numeric_field(freeze, "intended_native_stake"),
                intended_native_shares=_safe_numeric_field(freeze, "intended_native_shares"),
                intended_limit_price=_safe_numeric_field(freeze, "intended_limit_price"),
            )
        )
    return legs


def _freeze_for_leg(
    leg: dict[str, Any],
    freeze_diagnostics: list[Any] | None,
    used: set[int],
) -> dict[str, Any] | None:
    if freeze_diagnostics is None:
        return None
    identities = {
        value
        for value in (
            _text(leg.get("native_runner_id")),
            _text(leg.get("retrieval_native_id")),
        )
        if value
    }
    venue = _text(leg.get("venue"))
    outcome = _text(leg.get("outcome"))
    market = _text(leg.get("native_market_id"))
    ranked: list[tuple[int, int, dict[str, Any]]] = []
    for index, item in enumerate(freeze_diagnostics):
        if index in used or not isinstance(item, dict):
            continue
        if _text(item.get("venue")) != venue:
            continue
        score = 0
        runner = _text(item.get("native_runner_id"))
        if runner and runner in identities:
            score += 4
        if market and _text(item.get("native_market_id")) == market:
            score += 2
        if outcome and _text(item.get("outcome")) == outcome:
            score += 1
        if score:
            ranked.append((score, index, item))
    if not ranked:
        return None
    ranked.sort(key=lambda row: (-row[0], row[1]))
    _, index, item = ranked[0]
    used.add(index)
    return item


def _freeze_projection(
    freeze: dict[str, Any] | None,
    freeze_diagnostics: list[Any] | None,
) -> tuple[NativeFreezeStatus, str | None, bool | None]:
    if freeze_diagnostics is None:
        return "details_not_recorded", DETAILS_NOT_RECORDED_REASON, None
    if freeze is None:
        return "details_not_recorded", DETAILS_NOT_RECORDED_REASON, None
    frozen = freeze.get("frozen") is True
    reason = _text(freeze.get("reason"))
    if frozen:
        return "frozen", "frozen", True
    if reason in NATIVE_ORDER_FREEZE_REASONS and reason != "frozen":
        return "not_frozen", reason, False
    return "not_frozen", "native_order_translation_other", False


def _safe_numeric_field(freeze: dict[str, Any] | None, name: str) -> str | None:
    if freeze is None:
        return None
    value = freeze.get(name)
    if value is None or value == "":
        return None
    try:
        return format(Decimal(str(value)), "f")
    except (InvalidOperation, ValueError):
        return None


def _economics_vs_threshold(
    net_edge: str | None,
    minimum_net_edge: str | None,
) -> EconomicsThresholdStatus:
    if minimum_net_edge is None:
        return "threshold_not_recorded"
    if net_edge is None:
        return "net_edge_not_recorded"
    edge = _decimal(net_edge)
    floor = _decimal(minimum_net_edge)
    if edge is None or floor is None:
        return "threshold_not_recorded"
    if edge < floor:
        return "below_configured_threshold"
    return "meets_or_exceeds_configured_threshold"


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


def _provider_calls(timing: dict[str, Any], diagnostics: dict[str, Any]) -> list[dict[str, Any]]:
    rows = timing.get("calls")
    if not isinstance(rows, list):
        rows = diagnostics.get("calls")
    if not isinstance(rows, list):
        return []
    return [item for item in rows if isinstance(item, dict)]


def _call_for_leg(leg: dict[str, Any], calls: list[dict[str, Any]]) -> dict[str, Any] | None:
    identities = {
        value
        for value in (
            _text(leg.get("retrieval_native_id")),
            _text(leg.get("native_market_id")),
            _text(leg.get("native_runner_id")),
        )
        if value
    }
    if not identities:
        return None
    matches: list[dict[str, Any]] = []
    seen: set[int] = set()
    for call in calls:
        source_id = _text(call.get("source_id"))
        if source_id is None:
            continue
        if not _source_matches(source_id, identities):
            continue
        marker = id(call)
        if marker in seen:
            continue
        seen.add(marker)
        matches.append(call)
    if len(matches) == 1:
        return matches[0]
    return None


def _source_matches(source_id: str, identities: set[str]) -> bool:
    if source_id in identities:
        return True
    prefix = f"{source_id}:"
    for identity in identities:
        if identity.startswith(prefix) or source_id.startswith(f"{identity}:"):
            return True
    return False


def _venue_timings(calls: list[dict[str, Any]]) -> list[Price2VenueTiming]:
    by_venue: dict[str, Price2VenueTiming] = {}
    for call in calls:
        venue = _text(call.get("venue"))
        if not venue:
            continue
        current = by_venue.get(venue)
        wait = _int(call.get("slot_wait_ms"))
        io_ms = _int(call.get("io_ms"))
        if current is None:
            by_venue[venue] = Price2VenueTiming(
                venue=venue,
                slot_wait_ms=wait,
                io_ms=io_ms,
                call_count=1,
            )
            continue
        by_venue[venue] = current.model_copy(
            update={
                "slot_wait_ms": _max_optional(current.slot_wait_ms, wait),
                "io_ms": _max_optional(current.io_ms, io_ms),
                "call_count": current.call_count + 1,
            }
        )
    return list(by_venue.values())


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


def _filled(cycle_outcome: str | None) -> bool:
    return cycle_outcome == "filled"


def _quote_evaluation_elapsed_ms(
    started_at: datetime | None,
    evaluated_at: datetime | None,
) -> int | None:
    if started_at is None or evaluated_at is None:
        return None
    return max(0, int((evaluated_at - started_at).total_seconds() * 1000))


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
