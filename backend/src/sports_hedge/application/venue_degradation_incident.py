"""Bounded in-memory Why? snapshots for first-class venue degradation.

Captures at the transition from healthy / mixed-normal into the UI degraded
state so transient lane evidence is retained before the operator clicks.

Read-only. Never calls Matchbook/Kalshi, never starts HOT/BACKGROUND/UNIVERSE
work, and never reads `/venues/health`. PAPER / in-memory only.
"""

from __future__ import annotations

import threading
from collections import deque
from datetime import UTC, datetime
from typing import Any, Mapping
from uuid import uuid4

from sports_hedge.application.lane_venues import (
    OPERATOR_SCAN_VENUES,
    is_operator_disabled_health,
    is_provider_health_failure,
)
from sports_hedge.application.provider_access import (
    HEALTH_AUTH_FAILURE,
    HEALTH_CAPACITY_SATURATED,
    HEALTH_DEFERRED,
    HEALTH_DISCOVERY_TIMEOUT,
    HEALTH_MARKET_TIMEOUT,
    HEALTH_TIMEOUT,
    HEALTH_UNAVAILABLE,
    HEALTH_WAITING,
)
from sports_hedge.application.serving_build import ServingBuildInfo, get_serving_build_info
from sports_hedge.domain.models import VenueName

INCIDENT_SCHEMA = "sports_hedge.venue_degradation_incident.v1"
INCIDENT_DATA_KIND = "in_memory_transition_snapshot"
FALLBACK_DATA_KIND = "already_polled_live_refresh_read_model"
MAX_SCAN_CYCLE_SUMMARIES = 10
MAX_STORED_INCIDENTS = 8
FIRST_CLASS_VENUES: tuple[str, ...] = tuple(venue.value for venue in OPERATOR_SCAN_VENUES)
INCIDENT_REF_KEYS = ("available", "captured_at", "incident_id")

_UI_DEGRADED_EXTRA = frozenset({"retry_wait", "partial"})
_LOCAL_BACKPRESSURE_HEALTH = frozenset(
    {HEALTH_WAITING, HEALTH_DEFERRED, HEALTH_CAPACITY_SATURATED, "deferred"}
)
_AUTH_UNAVAILABLE = frozenset({HEALTH_AUTH_FAILURE, HEALTH_UNAVAILABLE})
_PROVIDER_TIMEOUT = frozenset(
    {HEALTH_TIMEOUT, HEALTH_DISCOVERY_TIMEOUT, HEALTH_MARKET_TIMEOUT, "timeout"}
)
_LANE_SNAPSHOT_KEYS = (
    "venue_health",
    "operation_health",
    "last_error",
    "last_started_at",
    "last_completed_at",
    "last_duration_ms",
    "last_heartbeat_at",
    "last_plan_reason",
    "last_diagnostics",
    "worker_state",
    "degraded",
    "canonical_retryable",
    "canonical_final_failed",
    "canonical_remaining",
    "series_retryable",
    "series_final_failed",
    "evaluated_count",
    "not_evaluated_count",
    "operator_summary",
)
_CYCLE_SNAPSHOT_KEYS = (
    "cycle_id",
    "started_at",
    "completed_at",
    "scan_lane",
    "duration_ms",
    "fixture_count",
    "evaluated_count",
    "not_evaluated_count",
    "venue_health",
    "degraded",
    "last_error",
    "operator_summary",
    "universe_generation_id",
    "resume_cursor",
    "completeness",
    "generation_resume",
    "generation_work_used_s",
)
_PROVIDER_ACCESS_KEYS = ("inflight", "waiting", "waiting_by_lane", "limits")


def is_ui_degraded_health(value: str | None) -> bool:
    """True when the header would show a provider-degraded / timeout / unavailable state."""

    if not value or is_operator_disabled_health(value):
        return False
    if is_provider_health_failure(value):
        return True
    return value in _UI_DEGRADED_EXTRA


def _iso(moment: datetime) -> str:
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _dump(value: Any) -> Any:
    if value is None:
        return None
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        return dump(mode="json")
    if isinstance(value, datetime):
        return _iso(value)
    if isinstance(value, VenueName):
        return value.value
    return value


def _pick(payload: Mapping[str, Any] | None, keys: tuple[str, ...]) -> dict[str, Any]:
    source = dict(payload or {})
    return {key: source.get(key) for key in keys if key in source}


def _lane_snapshot(lane: Any) -> dict[str, Any]:
    dumped = _dump(lane)
    if not isinstance(dumped, dict):
        return {}
    return _pick(dumped, _LANE_SNAPSHOT_KEYS)


def _tier_snapshot(tier: Any) -> dict[str, Any]:
    dumped = _dump(tier)
    if not isinstance(dumped, dict):
        return {}
    return {
        "venue_health": dict(dumped.get("venue_health") or {}),
        "operation_health": dict(dumped.get("operation_health") or {}),
        "last_error": dumped.get("last_error"),
        "retry_wait": dumped.get("retry_wait"),
        "deferred": dumped.get("deferred"),
        "provider_capacity_saturated": dumped.get("provider_capacity_saturated"),
        "working_set": dumped.get("working_set"),
        "in_flight": dumped.get("in_flight"),
        "not_started_this_cadence": dumped.get("not_started_this_cadence"),
    }


def _cycle_summaries(cycles: Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for cycle in list(cycles or [])[:MAX_SCAN_CYCLE_SUMMARIES]:
        dumped = _dump(cycle)
        if isinstance(dumped, dict):
            rows.append(_pick(dumped, _CYCLE_SNAPSHOT_KEYS))
    return rows


def _provider_access_snapshot(payload: Mapping[str, Any] | None) -> dict[str, Any]:
    source = dict(payload or {})
    return {key: source.get(key) for key in _PROVIDER_ACCESS_KEYS}


def _operation_values(operation_health: Mapping[str, Any] | None, venue: str) -> list[str]:
    bucket = (operation_health or {}).get(venue)
    if isinstance(bucket, Mapping):
        return [str(value) for value in bucket.values()]
    if bucket is None:
        return []
    return [str(bucket)]


def _contains(values: list[str], tokens: frozenset[str]) -> bool:
    return any(value in tokens for value in values)


def classify_venue_degradation(status: Any, venue: str) -> dict[str, Any]:
    """Lane-aware labels so mixed HOT-ok / UNIVERSE-timeout is not a HOT outage."""

    hot = getattr(status, "hot", None)
    background = getattr(status, "background", None)
    universe = getattr(status, "universe", None)
    price_engine = getattr(status, "price_engine", None)
    hot_health = dict(getattr(hot, "venue_health", None) or {})
    background_health = dict(getattr(background, "venue_health", None) or {})
    universe_health = dict(getattr(universe, "venue_health", None) or {})
    top = dict(getattr(status, "venue_health", None) or {})
    hot_ops = _operation_values(getattr(hot, "operation_health", None), venue)
    background_ops = _operation_values(getattr(background, "operation_health", None), venue)
    universe_ops = _operation_values(getattr(universe, "operation_health", None), venue)
    pe_hot = getattr(price_engine, "hot", None)
    pe_background = getattr(price_engine, "background", None)
    pe_hot_ops = _operation_values(getattr(pe_hot, "operation_health", None), venue)
    pe_background_ops = _operation_values(getattr(pe_background, "operation_health", None), venue)
    all_ops = hot_ops + background_ops + universe_ops + pe_hot_ops + pe_background_ops
    lane_health = [
        hot_health.get(venue),
        background_health.get(venue),
        universe_health.get(venue),
        top.get(venue),
    ]
    access = dict(getattr(status, "provider_access", None) or {})
    waiting = dict(access.get("waiting") or {})
    waiting_by_lane = dict(access.get("waiting_by_lane") or {})
    waiting_count = int(waiting.get(venue) or 0)
    lane_waiting = 0
    for counts in waiting_by_lane.values():
        if isinstance(counts, Mapping):
            lane_waiting += int(counts.get(venue) or 0)
    background_deferred = int(getattr(pe_background, "deferred", 0) or 0)
    background_saturated = int(getattr(pe_background, "provider_capacity_saturated", 0) or 0)
    local_backpressure = bool(
        waiting_count
        or lane_waiting
        or background_deferred
        or background_saturated
        or _contains(all_ops, _LOCAL_BACKPRESSURE_HEALTH)
        or any(value in _LOCAL_BACKPRESSURE_HEALTH for value in lane_health if value)
    )
    hot_market_timeout = hot_health.get(venue) == HEALTH_MARKET_TIMEOUT or _contains(
        hot_ops + pe_hot_ops, frozenset({HEALTH_MARKET_TIMEOUT})
    )
    universe_discovery_timeout = universe_health.get(venue) == HEALTH_DISCOVERY_TIMEOUT or _contains(
        universe_ops, frozenset({HEALTH_DISCOVERY_TIMEOUT})
    )
    retry_wait_painting = any(value == "retry_wait" for value in lane_health if value) or _contains(
        all_ops, frozenset({"retry_wait"})
    )
    return {
        "hot_health": hot_health.get(venue),
        "background_health": background_health.get(venue),
        "universe_health": universe_health.get(venue),
        "top_level_health": top.get(venue),
        "hot_ok": hot_health.get(venue) == "ok",
        "hot_market_timeout": hot_market_timeout,
        "universe_discovery_timeout": universe_discovery_timeout,
        "provider_timeout": _contains(all_ops, _PROVIDER_TIMEOUT)
        or any(value in _PROVIDER_TIMEOUT for value in lane_health if value),
        "auth_or_unavailable": _contains(all_ops, _AUTH_UNAVAILABLE)
        or any(value in _AUTH_UNAVAILABLE for value in lane_health if value),
        "local_backpressure": local_backpressure,
        "retry_wait_painting": retry_wait_painting,
        "mixed_lane_degraded": bool(
            hot_health.get(venue) == "ok" and is_ui_degraded_health(universe_health.get(venue))
        ),
    }


def _active_catalogue_count(status: Any) -> int | None:
    price_engine = getattr(status, "price_engine", None)
    if price_engine is None:
        return None
    hot = getattr(price_engine, "hot", None)
    background = getattr(price_engine, "background", None)
    hot_count = getattr(hot, "working_set", None)
    background_count = getattr(background, "working_set", None)
    if hot_count is None and background_count is None:
        return None
    return int(hot_count or 0) + int(background_count or 0)


def build_venue_degradation_incident(
    status: Any,
    venue: str,
    *,
    previous_health: str | None,
    new_health: str | None,
    captured_at: datetime | None = None,
    build: ServingBuildInfo | None = None,
    snapshot_kind: str = INCIDENT_DATA_KIND,
) -> dict[str, Any]:
    """Compact JSON diagnostic. Omits discovered fixture rows to stay bounded."""

    moment = captured_at or datetime.now(UTC)
    identity = build or get_serving_build_info()
    price_engine = getattr(status, "price_engine", None)
    return {
        "schema": INCIDENT_SCHEMA,
        "data_kind": snapshot_kind,
        "incident_id": str(uuid4()),
        "captured_at": _iso(moment),
        "build": {
            "git_sha": identity.git_sha,
            "git_branch": identity.git_branch,
            "source": identity.source,
        },
        "affected_venue": venue,
        "transition": {
            "previous_health": previous_health,
            "new_health": new_health,
            "reason": f"{previous_health or 'unknown'}->{new_health or 'unknown'}",
        },
        "venue_health": dict(getattr(status, "venue_health", None) or {}),
        "hot": _lane_snapshot(getattr(status, "hot", None)),
        "background": _lane_snapshot(getattr(status, "background", None)),
        "universe": _lane_snapshot(getattr(status, "universe", None)),
        "price_engine": {
            "hot": _tier_snapshot(getattr(price_engine, "hot", None)),
            "background": _tier_snapshot(getattr(price_engine, "background", None)),
        },
        "provider_access": _provider_access_snapshot(getattr(status, "provider_access", None)),
        "recent_scan_cycles": _cycle_summaries(getattr(status, "recent_scan_cycles", None)),
        "active_catalogue_count": _active_catalogue_count(status),
        "classification": classify_venue_degradation(status, venue),
    }


def fallback_venue_degradation_incident(
    status: Any,
    venue: str,
    *,
    captured_at: datetime | None = None,
    build: ServingBuildInfo | None = None,
) -> dict[str, Any]:
    """Current already-built read model only. Does not become the transition snapshot."""

    top = dict(getattr(status, "venue_health", None) or {})
    return build_venue_degradation_incident(
        status,
        venue,
        previous_health=None,
        new_health=top.get(venue),
        captured_at=captured_at,
        build=build,
        snapshot_kind=FALLBACK_DATA_KIND,
    )


def incident_ref(incident: Mapping[str, Any]) -> dict[str, Any]:
    """Tiny live-refresh pointer. Never includes lane dumps or cycle rows."""

    return {
        "available": True,
        "captured_at": str(incident.get("captured_at") or ""),
        "incident_id": str(incident.get("incident_id") or ""),
    }


def is_compact_incident_ref(payload: Mapping[str, Any] | None) -> bool:
    keys = set(payload or {})
    return keys == set(INCIDENT_REF_KEYS)


class VenueDegradationIncidentStore:
    """Latest-per-venue snapshots plus a hard cap on retained incidents."""

    def __init__(self, *, max_incidents: int = MAX_STORED_INCIDENTS) -> None:
        self._lock = threading.Lock()
        self._previous_health: dict[str, str | None] = {}
        self._latest: dict[str, dict[str, Any]] = {}
        self._retained: deque[dict[str, Any]] = deque(maxlen=max(1, int(max_incidents)))
        self._max_incidents = max(1, int(max_incidents))

    def reset(self) -> None:
        with self._lock:
            self._previous_health.clear()
            self._latest.clear()
            self._retained.clear()

    @property
    def retained_count(self) -> int:
        with self._lock:
            return len(self._retained)

    def get(self, venue: str) -> dict[str, Any] | None:
        key = str(venue or "").strip().casefold()
        with self._lock:
            incident = self._latest.get(key)
            return dict(incident) if incident else None

    def observe(
        self,
        status: Any,
        *,
        captured_at: datetime | None = None,
        build: ServingBuildInfo | None = None,
    ) -> dict[str, dict[str, Any]]:
        """Capture once per OK→degraded edge. Returns compact refs, not full packets."""

        top = dict(getattr(status, "venue_health", None) or {})
        captured_at = captured_at or datetime.now(UTC)
        identity = build or get_serving_build_info()
        with self._lock:
            for venue in FIRST_CLASS_VENUES:
                current = top.get(venue)
                previous = self._previous_health.get(venue)
                if is_ui_degraded_health(current) and not is_ui_degraded_health(previous):
                    incident = build_venue_degradation_incident(
                        status,
                        venue,
                        previous_health=previous,
                        new_health=current,
                        captured_at=captured_at,
                        build=identity,
                    )
                    self._latest[venue] = incident
                    self._retained.append(incident)
                elif not is_ui_degraded_health(current):
                    self._latest.pop(venue, None)
                self._previous_health[venue] = current
            return {
                venue: incident_ref(incident)
                for venue, incident in self._latest.items()
                if is_ui_degraded_health(top.get(venue))
            }
