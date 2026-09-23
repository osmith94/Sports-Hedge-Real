"""Adaptive, provider-aware fair scheduler for scanner work.

Issue #473. Process-memory only. PAPER / read-only. Not a durable work queue
and not scheduler authority over the catalogue (Tenet 19).

Work becomes eligible only when existing operator cadence says it is due.
This module ranks already-due work. It must not start HOT/BACKGROUND/UNIVERSE
items early, raise provider concurrency, or cancel UNIVERSE.

Priority function (lower ``rank_key`` is served first)
------------------------------------------------------

``rank_key = (band, backpressure, -urgency, -wait_age_ms, seq)``

Bands (after anti-starvation overrides):

    -2  starved UNIVERSE / BACKGROUND (grant-count fairness)
    -1  starved HOT versus a run of ACTIVE grants
     0  ACTIVE TRADE safety / settlement
     1  HOT cross-venue viable and near-threshold / qualifying
     2  HOT cross-venue viable
     3  HOT ordinary (lifecycle/surveillance, not yet valued)
     4  UNIVERSE discovery
     5  BACKGROUND near-threshold
     6  BACKGROUND ordinary
     7  low-value (skip_expensive / single-venue / pruned bound)

ACTIVE never ages out of band 0. Aging may promote bands 4–7 toward band 1
so discovery/BACKGROUND cannot starve forever, but cannot outrank unaged
HOT viable/near-threshold (floor is band 1, and HOT_VIABLE_NEAR stays 1).

Value inputs come from #487 viability/upper-bound signals plus last known
near-threshold economics. Provider backpressure uses observed latency,
queue depth, rate-limit/backoff and saturation of venues the work needs.

Weighted-fair aging: ``aging_steps = wait_age_ms // (quantum_ms * lane_weight)``
with HOT weight 8, UNIVERSE 2, BACKGROUND 1 (same 8:1 ratio as the existing
HOT starvation grant cap). Each step reduces band by 1 down to 1.

Diagnostics are explainable: every decision returns named components and
reason labels. Queue metrics live on the shared provider-access snapshot.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum, StrEnum
from typing import Any

from sports_hedge.application.opportunity_viability import (
    CROSS_VENUE_UNAVAILABLE,
    NO_CROSS_VENUE_CANDIDATE,
    UPPER_BOUND_BELOW_MIN_NET,
)
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.domain.models import VenueName

AGING_QUANTUM_MS = 3000
HOT_LANE_WEIGHT = 8
UNIVERSE_LANE_WEIGHT = 2
BACKGROUND_LANE_WEIGHT = 1
LATENCY_BACKPRESSURE_MS = 1500
URGENCY_ACTIVE = 100
URGENCY_DEADLINE_MISS = 50
URGENCY_OVERDUE_CAP = 40
URGENCY_IN_PLAY = 30
URGENCY_NEAR = 20

LANE_ACTIVE = "active_trade"
LANE_HOT = ScanLane.HOT.value
LANE_BACKGROUND = "background"
LANE_UNIVERSE = ScanLane.UNIVERSE.value


class SchedulerBand(IntEnum):
    STARVED_LOWER = -2
    STARVED_HOT = -1
    ACTIVE = 0
    HOT_VIABLE_NEAR = 1
    HOT_VIABLE = 2
    HOT_ORDINARY = 3
    UNIVERSE = 4
    BACKGROUND_NEAR = 5
    BACKGROUND = 6
    LOW_VALUE = 7


class SchedulerValueClass(StrEnum):
    ACTIVE_SAFETY = "active_safety"
    HOT_VIABLE_NEAR = "hot_viable_near"
    HOT_VIABLE = "hot_viable"
    HOT_ORDINARY = "hot_ordinary"
    UNIVERSE = "universe"
    BACKGROUND_NEAR = "background_near"
    BACKGROUND = "background"
    LOW_VALUE = "low_value"


@dataclass(frozen=True)
class ProviderPressure:
    """Current observed pressure for one venue. Never infers a venue outage."""

    venue: VenueName
    inflight: int = 0
    waiting: int = 0
    limit: int = 0
    ewma_latency_ms: int = 0
    rate_limited: bool = False
    rate_limit_remaining_s: float = 0.0
    backoff_remaining_s: float = 0.0

    @property
    def saturated(self) -> bool:
        return self.limit > 0 and self.inflight >= self.limit

    @property
    def backpressure(self) -> bool:
        if self.rate_limited or self.backoff_remaining_s > 0:
            return True
        if self.saturated:
            return True
        if self.ewma_latency_ms >= LATENCY_BACKPRESSURE_MS:
            return True
        if self.limit > 0 and self.waiting >= self.limit:
            return True
        return False


@dataclass(frozen=True)
class SchedulerWork:
    """One unit of already-due scanner work. Cadence eligibility is the caller's job."""

    lane: str
    work_id: str = ""
    viable_venue_count: int = 0
    skip_expensive_work: bool = False
    viability_reason: str | None = None
    viability_assessed: bool = False
    near_threshold: bool = False
    qualifying: bool = False
    in_play: bool = False
    required_venues: tuple[VenueName, ...] = ()
    due_mono: float | None = None
    deadline_mono: float | None = None
    cadence_seconds: float | None = None
    seq: int = 0
    wait_age_ms: int = 0
    now_mono: float = 0.0

    @property
    def normalized_lane(self) -> str:
        text = str(self.lane or "").strip().casefold()
        if text in {LANE_ACTIVE, "active-trade", "settlement"}:
            return LANE_ACTIVE
        if text == LANE_HOT:
            return LANE_HOT
        if text == LANE_BACKGROUND:
            return LANE_BACKGROUND
        return LANE_UNIVERSE


@dataclass(frozen=True)
class PriorityDecision:
    """Deterministic, explainable ranking of one work item."""

    rank_key: tuple[int, ...]
    band: int
    value_class: SchedulerValueClass
    aging_steps: int
    backpressure_penalty: int
    urgency_score: int
    lane_weight: int
    reasons: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return {
            "rank_key": list(self.rank_key),
            "band": int(self.band),
            "value_class": self.value_class.value,
            "aging_steps": int(self.aging_steps),
            "backpressure_penalty": int(self.backpressure_penalty),
            "urgency_score": int(self.urgency_score),
            "lane_weight": int(self.lane_weight),
            "reasons": list(self.reasons),
        }


def work_from_lane(
    lane: ScanLane | str | None,
    *,
    work_id: str = "",
    seq: int = 0,
) -> SchedulerWork:
    return SchedulerWork(lane=str(lane or ScanLane.UNIVERSE.value), work_id=work_id, seq=seq)


def lane_weight(lane: str) -> int:
    normalized = str(lane or "").strip().casefold()
    if normalized == LANE_HOT:
        return HOT_LANE_WEIGHT
    if normalized == LANE_UNIVERSE:
        return UNIVERSE_LANE_WEIGHT
    if normalized == LANE_BACKGROUND:
        return BACKGROUND_LANE_WEIGHT
    return HOT_LANE_WEIGHT


def classify_value_class(work: SchedulerWork) -> SchedulerValueClass:
    lane = work.normalized_lane
    if lane == LANE_ACTIVE:
        return SchedulerValueClass.ACTIVE_SAFETY
    low_value = _is_low_value(work)
    if lane == LANE_UNIVERSE:
        return SchedulerValueClass.UNIVERSE
    if low_value:
        return SchedulerValueClass.LOW_VALUE
    if lane == LANE_HOT:
        if work.near_threshold or work.qualifying:
            return SchedulerValueClass.HOT_VIABLE_NEAR
        if work.viable_venue_count >= 2:
            return SchedulerValueClass.HOT_VIABLE
        return SchedulerValueClass.HOT_ORDINARY
    if work.near_threshold or work.qualifying:
        return SchedulerValueClass.BACKGROUND_NEAR
    return SchedulerValueClass.BACKGROUND


def base_band(value_class: SchedulerValueClass) -> SchedulerBand:
    mapping = {
        SchedulerValueClass.ACTIVE_SAFETY: SchedulerBand.ACTIVE,
        SchedulerValueClass.HOT_VIABLE_NEAR: SchedulerBand.HOT_VIABLE_NEAR,
        SchedulerValueClass.HOT_VIABLE: SchedulerBand.HOT_VIABLE,
        SchedulerValueClass.HOT_ORDINARY: SchedulerBand.HOT_ORDINARY,
        SchedulerValueClass.UNIVERSE: SchedulerBand.UNIVERSE,
        SchedulerValueClass.BACKGROUND_NEAR: SchedulerBand.BACKGROUND_NEAR,
        SchedulerValueClass.BACKGROUND: SchedulerBand.BACKGROUND,
        SchedulerValueClass.LOW_VALUE: SchedulerBand.LOW_VALUE,
    }
    return mapping[value_class]


def aging_steps_for(wait_age_ms: int, *, lane: str, quantum_ms: int = AGING_QUANTUM_MS) -> int:
    if wait_age_ms <= 0:
        return 0
    weight = max(1, lane_weight(lane))
    quantum = max(1, int(quantum_ms) * weight)
    return max(0, int(wait_age_ms) // quantum)


def apply_aging(band: int, steps: int, *, value_class: SchedulerValueClass) -> int:
    if value_class is SchedulerValueClass.ACTIVE_SAFETY:
        return int(SchedulerBand.ACTIVE)
    if steps <= 0:
        return band
    # Floor at HOT_VIABLE_NEAR: aged BACKGROUND/UNIVERSE may compete with
    # ordinary HOT, but unaged HOT viable/near-threshold still sorts first
    # via remaining HOT items at bands 1–2 until grant-count starvation.
    return max(int(SchedulerBand.HOT_VIABLE_NEAR), band - steps)


def urgency_score(work: SchedulerWork) -> int:
    if work.normalized_lane == LANE_ACTIVE:
        return URGENCY_ACTIVE
    score = 0
    now = float(work.now_mono or 0.0)
    if work.deadline_mono is not None and now > float(work.deadline_mono):
        score += URGENCY_DEADLINE_MISS
    elif work.due_mono is not None and now > float(work.due_mono):
        overdue_ms = int((now - float(work.due_mono)) * 1000)
        score += min(URGENCY_OVERDUE_CAP, max(0, overdue_ms // 100))
    if work.in_play:
        score += URGENCY_IN_PLAY
    if work.near_threshold or work.qualifying:
        score += URGENCY_NEAR
    return score


def backpressure_penalty(
    work: SchedulerWork,
    pressure_by_venue: dict[VenueName, ProviderPressure] | None,
) -> tuple[int, tuple[str, ...]]:
    if work.normalized_lane == LANE_ACTIVE:
        return 0, ()
    if not pressure_by_venue or not work.required_venues:
        return 0, ()
    reasons: list[str] = []
    penalty = 0
    for venue in work.required_venues:
        pressure = pressure_by_venue.get(venue)
        if pressure is None or not pressure.backpressure:
            continue
        penalty = 1
        if pressure.rate_limited:
            reasons.append(f"{venue.value}_rate_limited")
        elif pressure.backoff_remaining_s > 0:
            reasons.append(f"{venue.value}_backoff")
        elif pressure.ewma_latency_ms >= LATENCY_BACKPRESSURE_MS:
            reasons.append(f"{venue.value}_high_latency")
        elif pressure.saturated:
            reasons.append(f"{venue.value}_saturated")
        else:
            reasons.append(f"{venue.value}_queue_depth")
    return penalty, tuple(reasons)


def rank_scheduler_work(
    work: SchedulerWork,
    *,
    pressure_by_venue: dict[VenueName, ProviderPressure] | None = None,
    starve_lower: bool = False,
    starve_hot: bool = False,
    quantum_ms: int = AGING_QUANTUM_MS,
) -> PriorityDecision:
    """Pure deterministic rank. Clock and wait age are inputs, not sampled here."""

    value_class = classify_value_class(work)
    reasons: list[str] = [value_class.value]
    band = int(base_band(value_class))
    lane = work.normalized_lane
    weight = lane_weight(lane)
    steps = 0
    if starve_lower and lane in {LANE_UNIVERSE, LANE_BACKGROUND}:
        band = int(SchedulerBand.STARVED_LOWER)
        reasons.append("starvation_grant")
    elif starve_hot and lane == LANE_HOT:
        band = int(SchedulerBand.STARVED_HOT)
        reasons.append("starvation_grant_hot")
    else:
        steps = aging_steps_for(work.wait_age_ms, lane=lane, quantum_ms=quantum_ms)
        aged = apply_aging(band, steps, value_class=value_class)
        if aged != band:
            reasons.append(f"aging_steps:{steps}")
        band = aged
    penalty, pressure_reasons = backpressure_penalty(work, pressure_by_venue)
    reasons.extend(pressure_reasons)
    urgency = urgency_score(work)
    if work.skip_expensive_work and lane != LANE_ACTIVE:
        reasons.append(work.viability_reason or "skip_expensive_work")
    rank_key = (
        band,
        penalty,
        -int(urgency),
        -max(0, int(work.wait_age_ms)),
        int(work.seq),
    )
    return PriorityDecision(
        rank_key=rank_key,
        band=band,
        value_class=value_class,
        aging_steps=steps,
        backpressure_penalty=penalty,
        urgency_score=urgency,
        lane_weight=weight,
        reasons=tuple(reasons),
    )


def order_scheduler_work(
    items: list[SchedulerWork],
    *,
    pressure_by_venue: dict[VenueName, ProviderPressure] | None = None,
    starve_lower: bool = False,
    starve_hot: bool = False,
    quantum_ms: int = AGING_QUANTUM_MS,
) -> list[tuple[SchedulerWork, PriorityDecision]]:
    ranked = [
        (
            item,
            rank_scheduler_work(
                item,
                pressure_by_venue=pressure_by_venue,
                starve_lower=starve_lower,
                starve_hot=starve_hot,
                quantum_ms=quantum_ms,
            ),
        )
        for item in items
    ]
    ranked.sort(key=lambda pair: pair[1].rank_key)
    return ranked


def pressure_from_snapshot(
    snapshot: Any,
    *,
    rate_limited_remaining_s: dict[str, float] | None = None,
    ewma_latency_ms: dict[str, int] | None = None,
    backoff_remaining_s: dict[str, float] | None = None,
) -> dict[VenueName, ProviderPressure]:
    inflight = _map(getattr(snapshot, "inflight", None) or {})
    waiting = _map(getattr(snapshot, "waiting", None) or {})
    limits = _map(getattr(snapshot, "limits", None) or {})
    if isinstance(snapshot, dict):
        inflight = _map(snapshot.get("inflight") or {})
        waiting = _map(snapshot.get("waiting") or {})
        limits = _map(snapshot.get("limits") or {})
    rate_limited_remaining_s = rate_limited_remaining_s or {}
    ewma_latency_ms = ewma_latency_ms or {}
    backoff_remaining_s = backoff_remaining_s or {}
    payload: dict[VenueName, ProviderPressure] = {}
    for venue in (VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI):
        remaining = float(rate_limited_remaining_s.get(venue.value, 0.0) or 0.0)
        payload[venue] = ProviderPressure(
            venue=venue,
            inflight=int(inflight.get(venue.value, 0) or 0),
            waiting=int(waiting.get(venue.value, 0) or 0),
            limit=int(limits.get(venue.value, 0) or 0),
            ewma_latency_ms=int(ewma_latency_ms.get(venue.value, 0) or 0),
            rate_limited=remaining > 0,
            rate_limit_remaining_s=max(0.0, remaining),
            backoff_remaining_s=max(
                0.0, float(backoff_remaining_s.get(venue.value, 0.0) or 0.0)
            ),
        )
    return payload


def _is_low_value(work: SchedulerWork) -> bool:
    if work.skip_expensive_work:
        return True
    reason = str(work.viability_reason or "").strip()
    if reason in {
        CROSS_VENUE_UNAVAILABLE,
        NO_CROSS_VENUE_CANDIDATE,
        UPPER_BOUND_BELOW_MIN_NET,
    }:
        return True
    if work.viability_assessed and work.normalized_lane != LANE_UNIVERSE and work.viable_venue_count < 2:
        return True
    return False


def _map(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return {}
