"""Compact current System Load projection from already-public in-memory status.

Issue #362: current summary only. No historical P50/P95, no safe/unsafe score,
no provider calls, no catalogue rebuild, and no second observability subsystem.
"""

from __future__ import annotations

import json
import math
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.application.provider_access import DEFAULT_PROVIDER_CONCURRENCY
from sports_hedge.domain.models import VenueName

_MATCHBOOK = VenueName.MATCHBOOK.value
_KALSHI = VenueName.KALSHI.value
SYSTEM_LOAD_JSON_BUDGET_BYTES = 1024


class ProviderSlotLoad(BaseModel):
    """Configured slot use for one venue. Inflight/waiting are current counts."""

    inflight: int = Field(default=0, ge=0)
    limit: int = Field(default=0, ge=0)
    waiting: int = Field(default=0, ge=0)
    wait_ms: int = Field(default=0, ge=0)
    latency_ms: int = Field(default=0, ge=0)
    deadline_misses: int = Field(default=0, ge=0)
    saturated: bool = False


class HotLoad(BaseModel):
    """HOT roster vs price-engine working set. Fixture count is not item count.

    ``fixtures`` remains lifecycle/promoted membership. ``pricing_fixtures`` is
    the unique canonical fixtures actually in the HOT price-engine tier.
    """

    fixtures: int = Field(default=0, ge=0)
    pricing_fixtures: int = Field(default=0, ge=0)
    working_set: int = Field(default=0, ge=0)
    due: int = Field(default=0, ge=0)
    in_flight: int = Field(default=0, ge=0)
    retry_wait: int = Field(default=0, ge=0)
    deferred: int = Field(default=0, ge=0)
    last_cycle_ms: int | None = Field(default=None, ge=0)
    cadence_seconds: int = Field(default=0, ge=0)
    cadence_utilisation: float | None = None
    health: str = "unknown"


class BackgroundLoad(BaseModel):
    """BACKGROUND pricing cadence and working set. Independent of UNIVERSE."""

    working_set: int = Field(default=0, ge=0)
    pricing_fixtures: int = Field(default=0, ge=0)
    due: int = Field(default=0, ge=0)
    cadence_seconds: int = Field(default=0, ge=0)
    health: str = "unknown"


class UniverseLoad(BaseModel):
    """UNIVERSE catalogue progress from already-public lane counters."""

    evaluated: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    remaining: int = Field(default=0, ge=0)
    generation_work_used_s: float | None = Field(default=None, ge=0)
    generation_budget_seconds: float | None = Field(default=None, ge=0)
    cadence_seconds: int = Field(default=0, ge=0)
    selected_competition_count: int = Field(default=0, ge=0)
    scope_version: int | None = Field(default=None, ge=0)
    generation_scope_version: int | None = Field(default=None, ge=0)
    worker_state: str = "unknown"
    health: str = "unknown"


class ActiveTradeLoad(BaseModel):
    """ACTIVE TRADE exact-ID membership vs 5s cadence."""

    open_trades: int = Field(default=0, ge=0)
    due: int = Field(default=0, ge=0)
    overdue: int = Field(default=0, ge=0)
    last_cycle_ms: int | None = Field(default=None, ge=0)
    cadence_seconds: int = Field(default=0, ge=0)
    capital_locked_gbp: float | None = Field(default=None, ge=0)
    health: str = "unknown"


class SystemLoadSummary(BaseModel):
    """Tiny current-state load card. Display numbers only; not a health score."""

    active_trade: ActiveTradeLoad = Field(default_factory=ActiveTradeLoad)
    hot: HotLoad = Field(default_factory=HotLoad)
    background: BackgroundLoad = Field(default_factory=BackgroundLoad)
    matchbook: ProviderSlotLoad = Field(default_factory=ProviderSlotLoad)
    kalshi: ProviderSlotLoad = Field(default_factory=ProviderSlotLoad)
    universe: UniverseLoad = Field(default_factory=UniverseLoad)
    catalogue_items: int = Field(default=0, ge=0)


def cadence_utilisation(
    last_duration_ms: Any,
    cadence_seconds: Any,
) -> float | None:
    """last_duration / cadence. Missing or zero cadence is blank, never NaN."""

    cadence = _optional_float(cadence_seconds)
    duration_ms = _optional_float(last_duration_ms)
    if cadence is None or cadence <= 0 or duration_ms is None:
        return None
    ratio = duration_ms / (cadence * 1000.0)
    if not math.isfinite(ratio):
        return None
    return round(ratio, 4)


def system_load_from_status(
    status: Any,
    *,
    universe_work_used_s: float | None = None,
    active_trade_locked_gbp: float | None = None,
) -> SystemLoadSummary:
    """Project System Load from already-public in-memory / read-model fields."""

    hot = getattr(status, "hot", None)
    background = getattr(status, "background", None)
    universe = getattr(status, "universe", None)
    active_trade = getattr(status, "active_trade", None)
    engine = getattr(status, "price_engine", None)
    access = getattr(status, "provider_access", None)
    universe_scope = getattr(status, "universe_scope", None)
    if isinstance(status, dict):
        hot = status.get("hot", hot)
        background = status.get("background", background)
        universe = status.get("universe", universe)
        active_trade = status.get("active_trade", active_trade)
        engine = status.get("price_engine", engine)
        access = status.get("provider_access", access)
        universe_scope = status.get("universe_scope", universe_scope)
    hot_engine = _attr(engine, "hot")
    background_engine = _attr(engine, "background")
    hot_working = _count(_attr(hot_engine, "working_set"))
    background_working = _count(_attr(background_engine, "working_set"))
    last_cycle_ms = _optional_int(_attr(hot, "last_duration_ms"))
    cadence_seconds = _count(_attr(hot, "cadence_seconds"))
    work_used = _optional_float(universe_work_used_s)
    if work_used is None:
        work_used = _optional_float(_attr(universe, "generation_work_used_s"))
    evaluated = _first_count(
        _attr(universe, "canonical_evaluated"),
        _attr(universe, "evaluated_count"),
    )
    total = _first_count(
        _attr(universe, "canonical_work_total"),
        _attr(universe, "discovered_total"),
        _attr(universe, "fixture_count"),
    )
    remaining = _attr(universe, "canonical_remaining")
    if remaining is None:
        remaining = _attr(universe, "remaining")
    if remaining is None:
        remaining = max(0, total - evaluated)
    else:
        remaining = _count(remaining)
    return SystemLoadSummary(
        active_trade=ActiveTradeLoad(
            open_trades=_count(_attr(active_trade, "fixture_count")),
            due=_count(_attr(active_trade, "remaining")),
            overdue=_count(_attr(active_trade, "not_evaluated_count")),
            last_cycle_ms=_optional_int(_attr(active_trade, "last_duration_ms")),
            cadence_seconds=_count(_attr(active_trade, "cadence_seconds")),
            capital_locked_gbp=_optional_float(active_trade_locked_gbp),
            health=_lane_health(active_trade, None),
        ),
        hot=HotLoad(
            fixtures=_count(_attr(hot, "fixture_count")),
            pricing_fixtures=_count(_attr(hot_engine, "pricing_fixtures")),
            working_set=hot_working,
            due=_count(_attr(hot_engine, "due")),
            in_flight=_count(_attr(hot_engine, "in_flight")),
            retry_wait=_count(_attr(hot_engine, "retry_wait")),
            deferred=_count(_attr(hot_engine, "deferred")),
            last_cycle_ms=last_cycle_ms,
            cadence_seconds=cadence_seconds,
            cadence_utilisation=cadence_utilisation(last_cycle_ms, cadence_seconds),
            health=_lane_health(hot, hot_engine),
        ),
        background=BackgroundLoad(
            working_set=background_working,
            pricing_fixtures=_count(_attr(background_engine, "pricing_fixtures")),
            due=_count(_attr(background_engine, "due")),
            cadence_seconds=_count(_attr(background, "cadence_seconds")),
            health=_lane_health(background, background_engine),
        ),
        matchbook=_provider_slot(access, _MATCHBOOK),
        kalshi=_provider_slot(access, _KALSHI),
        universe=UniverseLoad(
            evaluated=evaluated,
            total=total,
            remaining=remaining,
            generation_work_used_s=work_used,
            generation_budget_seconds=_optional_float(
                _attr(universe, "generation_budget_seconds")
            ),
            cadence_seconds=_count(_attr(universe, "cadence_seconds")),
            selected_competition_count=_count(_attr(universe_scope, "selected_count")),
            scope_version=_optional_int(_attr(universe_scope, "scope_version")),
            generation_scope_version=_optional_int(
                _attr(universe_scope, "generation_scope_version")
            ),
            worker_state=_worker_state(universe),
            health=_lane_health(universe, None),
        ),
        catalogue_items=hot_working + background_working,
    )


def system_load_payload_bytes(summary: SystemLoadSummary) -> int:
    return len(json.dumps(summary.model_dump(mode="json"), separators=(",", ":")).encode("utf-8"))


def _provider_slot(access: Any, venue: str) -> ProviderSlotLoad:
    payload = access if isinstance(access, dict) else {}
    inflight_map = payload.get("inflight") if isinstance(payload.get("inflight"), dict) else {}
    waiting_map = payload.get("waiting") if isinstance(payload.get("waiting"), dict) else {}
    limits_map = payload.get("limits") if isinstance(payload.get("limits"), dict) else {}
    queue_map = payload.get("queue") if isinstance(payload.get("queue"), dict) else {}
    default_limit = DEFAULT_PROVIDER_CONCURRENCY.get(VenueName(venue), 0)
    if venue in limits_map:
        limit = _count(limits_map.get(venue))
    else:
        limit = int(default_limit)
    venue_queue = queue_map.get(venue) if isinstance(queue_map.get(venue), dict) else {}
    waiting = _count(waiting_map.get(venue))
    return ProviderSlotLoad(
        inflight=_count(inflight_map.get(venue)),
        limit=limit,
        waiting=waiting,
        wait_ms=_count(venue_queue.get("wait_age_ms")),
        latency_ms=_count(venue_queue.get("service_latency_ms")),
        deadline_misses=_count(venue_queue.get("deadline_misses")),
        saturated=bool(venue_queue.get("saturated")) if venue_queue else waiting > 0 and _count(inflight_map.get(venue)) >= limit and limit > 0,
    )


_DEGRADED_VENUE = frozenset(
    {"unavailable", "timeout", "degraded", "error", "failed", "discovery_timeout", "market_timeout"}
)


def _worker_state(lane: Any) -> str:
    if _attr(lane, "cycle_in_progress") is True:
        return "running"
    worker = str(_attr(lane, "worker_state") or "").strip().casefold()
    return worker or "unknown"


def _venue_health_degraded(source: Any) -> bool:
    health = _attr(source, "venue_health")
    if not isinstance(health, dict):
        return False
    return any(str(value).strip().casefold() in _DEGRADED_VENUE for value in health.values())


def _lane_health(lane: Any, engine_tier: Any | None) -> str:
    """Operator health from existing lane/engine fields. Not a safe/unsafe score."""

    if lane is None and engine_tier is None:
        return "unknown"
    if _attr(lane, "last_error") or _attr(engine_tier, "last_error"):
        return "degraded"
    if _attr(lane, "degraded") is True:
        return "degraded"
    worker = _worker_state(lane)
    if worker == "degraded" or _venue_health_degraded(lane) or _venue_health_degraded(engine_tier):
        return "degraded"
    if worker == "running":
        return "running"
    if worker in {"waiting", "idle", "complete"}:
        return "healthy"
    if lane is None:
        return "unknown"
    return "healthy"


def _attr(value: Any, name: str) -> Any:
    if value is None:
        return None
    if isinstance(value, dict):
        return value.get(name)
    return getattr(value, name, None)


def _count(value: Any) -> int:
    number = _optional_int(value)
    return 0 if number is None else number


def _first_count(*values: Any) -> int:
    for value in values:
        if value is None:
            continue
        return _count(value)
    return 0


def _optional_int(value: Any) -> int | None:
    if value is None or value is False:
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if number < 0:
        return None
    return number


def _optional_float(value: Any) -> float | None:
    if value is None or value is False:
        return None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(number) or number < 0:
        return None
    return number
