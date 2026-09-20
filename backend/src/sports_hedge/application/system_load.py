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
SYSTEM_LOAD_JSON_BUDGET_BYTES = 768


class ProviderSlotLoad(BaseModel):
    """Configured slot use for one venue. Inflight/waiting are current counts."""

    inflight: int = Field(default=0, ge=0)
    limit: int = Field(default=0, ge=0)
    waiting: int = Field(default=0, ge=0)


class HotLoad(BaseModel):
    """HOT roster vs price-engine working set. Fixture count is not item count."""

    fixtures: int = Field(default=0, ge=0)
    working_set: int = Field(default=0, ge=0)
    due: int = Field(default=0, ge=0)
    in_flight: int = Field(default=0, ge=0)
    retry_wait: int = Field(default=0, ge=0)
    deferred: int = Field(default=0, ge=0)
    last_cycle_ms: int | None = Field(default=None, ge=0)
    cadence_seconds: int = Field(default=0, ge=0)
    cadence_utilisation: float | None = None


class UniverseLoad(BaseModel):
    """UNIVERSE catalogue progress from already-public lane counters."""

    evaluated: int = Field(default=0, ge=0)
    total: int = Field(default=0, ge=0)
    remaining: int = Field(default=0, ge=0)
    generation_work_used_s: float | None = Field(default=None, ge=0)
    generation_budget_seconds: float | None = Field(default=None, ge=0)
    cadence_seconds: int = Field(default=0, ge=0)


class BackgroundLoad(BaseModel):
    """BACKGROUND price-engine cadence. Independent of UNIVERSE discovery."""

    cadence_seconds: int = Field(default=0, ge=0)
    working_set: int = Field(default=0, ge=0)
    due: int = Field(default=0, ge=0)


class SystemLoadSummary(BaseModel):
    """Tiny current-state load card. Display numbers only; not a health score."""

    hot: HotLoad = Field(default_factory=HotLoad)
    matchbook: ProviderSlotLoad = Field(default_factory=ProviderSlotLoad)
    kalshi: ProviderSlotLoad = Field(default_factory=ProviderSlotLoad)
    universe: UniverseLoad = Field(default_factory=UniverseLoad)
    background: BackgroundLoad = Field(default_factory=BackgroundLoad)
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
) -> SystemLoadSummary:
    """Project System Load from already-public in-memory / read-model fields."""

    hot = getattr(status, "hot", None)
    universe = getattr(status, "universe", None)
    engine = getattr(status, "price_engine", None)
    access = getattr(status, "provider_access", None)
    if isinstance(status, dict):
        hot = status.get("hot", hot)
        universe = status.get("universe", universe)
        engine = status.get("price_engine", engine)
        access = status.get("provider_access", access)
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
    background_lane = _attr(status, "background")
    if isinstance(status, dict):
        background_lane = status.get("background", background_lane)
    return SystemLoadSummary(
        hot=HotLoad(
            fixtures=_count(_attr(hot, "fixture_count")),
            working_set=hot_working,
            due=_count(_attr(hot_engine, "due")),
            in_flight=_count(_attr(hot_engine, "in_flight")),
            retry_wait=_count(_attr(hot_engine, "retry_wait")),
            deferred=_count(_attr(hot_engine, "deferred")),
            last_cycle_ms=last_cycle_ms,
            cadence_seconds=cadence_seconds,
            cadence_utilisation=cadence_utilisation(last_cycle_ms, cadence_seconds),
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
        ),
        background=BackgroundLoad(
            cadence_seconds=_count(_attr(background_lane, "cadence_seconds")),
            working_set=background_working,
            due=_count(_attr(background_engine, "due")),
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
    default_limit = DEFAULT_PROVIDER_CONCURRENCY.get(VenueName(venue), 0)
    if venue in limits_map:
        limit = _count(limits_map.get(venue))
    else:
        limit = int(default_limit)
    return ProviderSlotLoad(
        inflight=_count(inflight_map.get(venue)),
        limit=limit,
        waiting=_count(waiting_map.get(venue)),
    )


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
