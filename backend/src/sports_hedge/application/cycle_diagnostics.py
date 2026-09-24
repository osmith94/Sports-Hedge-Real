"""Bounded per-cycle scan diagnostics.

Counters and compact samples only. Never raw books, payloads, or API bodies.
One report is built at the end of a cycle and stored as a single append-only
row. Retention is 7 days and a fixed row cap, whichever drops a row first.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

DIAGNOSTIC_SCHEMA_VERSION = 1
DIAGNOSTIC_RETENTION_DAYS = 7
DIAGNOSTIC_MAX_ROWS = 8000
DIAGNOSTIC_MAX_REPORT_BYTES = 16_384
DIAGNOSTIC_MAX_SAMPLES = 12
DIAGNOSTIC_MAX_SLOWEST = 5
DIAGNOSTIC_MAX_WORKER_ERRORS = 8
DIAGNOSTIC_MAX_EXACT_IDS = 4096
_BUCKET_EDGES_MS = (50, 250, 1000, 4000, 8000, 16000)
_SAMPLE_KEYS = ("failed", "retry_wait", "deferred", "revalidation", "not_started")

DIAGNOSTIC_DATA_NOTE = (
    "Cycle diagnostic counters from this completed scan. Not live quotes. "
    "Raw provider responses are not stored. "
    f"Retained {DIAGNOSTIC_RETENTION_DAYS} days or {DIAGNOSTIC_MAX_ROWS} rows, "
    "whichever bound is reached first."
)


def price_engine_throughput_ceiling(
    *,
    matchbook_slots: int = 4,
    kalshi_slots: int = 4,
    polymarket_slots: int = 8,
    worker_limit: int | None = None,
    matchbook_calls: int = 1,
    kalshi_calls: int = 1,
    polymarket_calls: int = 0,
    seconds_per_call: float = 8.0,
    window_seconds: float = 35.0,
) -> dict[str, Any]:
    """Steady-state exact-ID ceiling. Does not raise the MB4/K4/PM8 caps.

    One catalogue row performs its venue calls sequentially. Independent rows
    share the venue slot caps. The binding rate is the slowest of those hops
    and of the in-slice worker pool (Matchbook slots + Kalshi slots).
    """

    call_seconds = max(0.0, float(seconds_per_call))
    workers = (
        max(1, int(matchbook_slots) + int(kalshi_slots))
        if worker_limit is None
        else max(1, int(worker_limit))
    )
    sequential_calls = (
        max(0, int(matchbook_calls))
        + max(0, int(kalshi_calls))
        + max(0, int(polymarket_calls))
    )
    item_seconds = call_seconds * sequential_calls
    candidates: list[tuple[str, float]] = []
    if call_seconds > 0 and matchbook_calls > 0:
        candidates.append(
            ("matchbook_slots", matchbook_slots / (matchbook_calls * call_seconds))
        )
    if call_seconds > 0 and kalshi_calls > 0:
        candidates.append(("kalshi_slots", kalshi_slots / (kalshi_calls * call_seconds)))
    if call_seconds > 0 and polymarket_calls > 0:
        candidates.append(
            ("polymarket_slots", polymarket_slots / (polymarket_calls * call_seconds))
        )
    if item_seconds > 0:
        candidates.append(("worker_pool", workers / item_seconds))
    if not candidates:
        per_second = 0.0
        binding = "none"
    else:
        binding, per_second = min(candidates, key=lambda item: item[1])
    window = max(0.0, float(window_seconds))
    return {
        "binding_limit": binding,
        "evaluations_per_second": per_second,
        "evaluations_per_minute": per_second * 60.0,
        "evaluations_in_window": per_second * window,
        "window_seconds": window,
        "sequential_calls_per_item": sequential_calls,
        "sequential_item_seconds": item_seconds,
        "worker_limit": workers,
        "provider_slots": {
            "matchbook": int(matchbook_slots),
            "kalshi": int(kalshi_slots),
            "polymarket": int(polymarket_slots),
        },
        "seconds_per_call": call_seconds,
        "caps_unchanged": True,
    }


@dataclass
class _StageStats:
    count: int = 0
    total_ms: int = 0
    max_ms: int = 0
    success: int = 0
    timeout: int = 0
    rate_limit: int = 0
    capacity_deferred: int = 0
    not_started: int = 0
    error: int = 0
    buckets: list[int] = field(
        default_factory=lambda: [0] * (len(_BUCKET_EDGES_MS) + 1)
    )

    def observe(self, elapsed_ms: int, outcome: str) -> None:
        elapsed = max(0, int(elapsed_ms))
        self.count += 1
        self.total_ms += elapsed
        if elapsed > self.max_ms:
            self.max_ms = elapsed
        placed = False
        for index, edge in enumerate(_BUCKET_EDGES_MS):
            if elapsed <= edge:
                self.buckets[index] += 1
                placed = True
                break
        if not placed:
            self.buckets[-1] += 1
        if outcome == "success":
            self.success += 1
        elif outcome == "timeout":
            self.timeout += 1
        elif outcome == "rate_limit":
            self.rate_limit += 1
        elif outcome == "capacity_deferred":
            self.capacity_deferred += 1
        elif outcome == "not_started":
            self.not_started += 1
        else:
            self.error += 1

    def percentile_ms(self, fraction: float) -> int | None:
        """Bucket upper bound at or above the requested fraction. Not a sorted sample."""

        if self.count <= 0:
            return None
        target = max(1, math.ceil(fraction * self.count))
        seen = 0
        edges = list(_BUCKET_EDGES_MS) + [self.max_ms]
        for index, edge in enumerate(edges):
            seen += self.buckets[index]
            if seen >= target:
                return int(edge)
        return int(self.max_ms)

    def as_dict(self, *, venue: str, stage: str) -> dict[str, Any]:
        avg = int(self.total_ms / self.count) if self.count else 0
        return {
            "venue": venue,
            "stage": stage,
            "count": self.count,
            "total_ms": self.total_ms,
            "avg_ms": avg,
            "p50_ms": self.percentile_ms(0.50),
            "p95_ms": self.percentile_ms(0.95),
            "max_ms": self.max_ms,
            "success": self.success,
            "timeout": self.timeout,
            "rate_limit": self.rate_limit,
            "capacity_deferred": self.capacity_deferred,
            "not_started": self.not_started,
            "error": self.error,
            "percentile_method": "histogram_bucket_upper_bound",
        }


@dataclass
class CycleDiagnosticAccumulator:
    """In-slice counters. Dropped when the cycle report dict is built."""

    provider_io_ms_sum: int = 0
    slot_wait_ms_sum: int = 0
    local_ms_sum: int = 0
    repeated_exact_id_calls: int = 0
    saved_provider_calls: int = 0
    skipped_provider_calls: int = 0
    items_with_provider_calls: int = 0
    explicit_slice_wall: bool = False
    slice_wall_seconds: float | None = None
    worker_limit: int = 0
    _stages: dict[tuple[str, str], _StageStats] = field(default_factory=dict)
    _seen_exact_ids: set[str] = field(default_factory=set)
    _exact_ids_truncated: bool = False
    _started_items: set[str] = field(default_factory=set)
    _samples: dict[str, list[dict[str, str]]] = field(default_factory=dict)
    _slowest: list[dict[str, Any]] = field(default_factory=list)
    _worker_errors: list[dict[str, str]] = field(default_factory=list)
    _peak_inflight: dict[str, int] = field(default_factory=dict)

    def note_slice_budget(
        self,
        *,
        slice_wall_seconds: float | None,
        worker_limit: int,
    ) -> None:
        self.explicit_slice_wall = slice_wall_seconds is not None
        self.slice_wall_seconds = (
            None if slice_wall_seconds is None else float(slice_wall_seconds)
        )
        self.worker_limit = max(0, int(worker_limit))

    def note_saved_calls(self, saved: int, skipped: int) -> None:
        self.saved_provider_calls += max(0, int(saved))
        self.skipped_provider_calls += max(0, int(skipped))

    def note_local_ms(self, elapsed_ms: int) -> None:
        self.local_ms_sum += max(0, int(elapsed_ms))

    def note_inflight(self, venue: str, count: int) -> None:
        current = self._peak_inflight.get(venue, 0)
        if count > current:
            self._peak_inflight[venue] = int(count)

    def note_provider(
        self,
        *,
        venue: str,
        stage: str,
        outcome: str,
        elapsed_ms: int,
        slot_wait_ms: int = 0,
        source_id: str | None = None,
        row_id: str | None = None,
    ) -> None:
        self.provider_io_ms_sum += max(0, int(elapsed_ms))
        self.slot_wait_ms_sum += max(0, int(slot_wait_ms))
        key = (str(venue), str(stage))
        stats = self._stages.get(key)
        if stats is None:
            stats = _StageStats()
            self._stages[key] = stats
        stats.observe(elapsed_ms, outcome)
        if row_id and row_id not in self._started_items:
            self._started_items.add(row_id)
            self.items_with_provider_calls += 1
        exact = _exact_id_key(venue, stage, source_id)
        if exact is not None:
            if exact in self._seen_exact_ids:
                self.repeated_exact_id_calls += 1
            elif self._exact_ids_truncated or len(self._seen_exact_ids) >= DIAGNOSTIC_MAX_EXACT_IDS:
                self._exact_ids_truncated = True
            else:
                self._seen_exact_ids.add(exact)
        if outcome in {"timeout", "error"} or int(elapsed_ms) >= 1000:
            self._consider_slow(
                venue=str(venue),
                stage=str(stage),
                elapsed_ms=int(elapsed_ms),
                source_id=source_id,
                outcome=outcome,
            )

    def note_terminal(self, bucket: str, row_id: str, reason: str | None = None) -> None:
        if bucket not in _SAMPLE_KEYS:
            return
        samples = self._samples.setdefault(bucket, [])
        if len(samples) >= DIAGNOSTIC_MAX_SAMPLES:
            return
        samples.append(
            {
                "row_id": _clip(row_id, 80),
                "reason": _clip(reason or bucket, 160),
            }
        )

    def note_worker_error(self, exc: BaseException) -> None:
        if len(self._worker_errors) >= DIAGNOSTIC_MAX_WORKER_ERRORS:
            return
        self._worker_errors.append(
            {
                "type": type(exc).__name__,
                "message": _clip(str(exc), 160),
            }
        )

    def finish(
        self,
        *,
        lane: str,
        wall_ms: int,
        due: int,
        evaluated: int,
        skipped: int,
        revalidation: int,
        failed: int,
        retry_wait: int,
        deferred: int,
        not_started: int,
        decisions: int,
        qualifying: int,
        promotions: int,
        provider_limits: Mapping[str, int] | None = None,
        coalesced_provider_calls: int = 0,
        issued_provider_calls: int = 0,
        pricing_call_shape: str = "",
    ) -> dict[str, Any]:
        wall = max(0, int(wall_ms))
        per_second = (evaluated / (wall / 1000.0)) if wall else 0.0
        stages = [
            stats.as_dict(venue=venue, stage=stage)
            for (venue, stage), stats in sorted(self._stages.items())
        ]
        slowest = sorted(self._slowest, key=lambda item: item["elapsed_ms"], reverse=True)[
            :DIAGNOSTIC_MAX_SLOWEST
        ]
        report = {
            "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
            "data_kind": "scan_cycle_diagnostic",
            "lane": lane,
            "note": DIAGNOSTIC_DATA_NOTE,
            "wall_ms": wall,
            "due": int(due),
            "considered": int(
                evaluated
                + skipped
                + revalidation
                + failed
                + retry_wait
                + deferred
                + not_started
            ),
            "terminals": {
                "evaluated": int(evaluated),
                "skipped": int(skipped),
                "revalidation": int(revalidation),
                "failed": int(failed),
                "retry_wait": int(retry_wait),
                "deferred": int(deferred),
                "not_started": int(not_started),
            },
            "leftover_collapsed": int(not_started + deferred + retry_wait),
            "decisions": int(decisions),
            "qualifying": int(qualifying),
            "promoted_hot": int(promotions),
            "evaluations_per_second": round(per_second, 4),
            "provider_io_ms_sum": int(self.provider_io_ms_sum),
            "slot_wait_ms_sum": int(self.slot_wait_ms_sum),
            "local_evaluate_ms_sum": int(self.local_ms_sum),
            "timing_note": (
                "provider_io_ms_sum and slot_wait_ms_sum add overlapping worker "
                "time, so they may exceed wall_ms. local_evaluate_ms_sum is "
                "normalization and economics after a provider payload returned."
            ),
            "saved_provider_calls": int(self.saved_provider_calls),
            "skipped_provider_calls": int(self.skipped_provider_calls),
            "coalesced_provider_calls": int(coalesced_provider_calls),
            "issued_provider_calls": int(issued_provider_calls),
            "repeated_exact_id_calls": int(self.repeated_exact_id_calls),
            "distinct_exact_ids": len(self._seen_exact_ids),
            "exact_id_tracking_truncated": self._exact_ids_truncated,
            "call_shape": {
                "sequential_within_item": True,
                "pricing_call_shape": str(pricing_call_shape or ""),
                "worker_limit": int(self.worker_limit),
                "explicit_slice_wall": self.explicit_slice_wall,
                "slice_wall_seconds": self.slice_wall_seconds,
                "provider_limits": {
                    str(key): int(value) for key, value in (provider_limits or {}).items()
                },
                "provider_calls": sum(stage["count"] for stage in stages),
                "items_with_provider_calls": int(self.items_with_provider_calls),
                "peak_inflight": dict(self._peak_inflight),
            },
            "stages": stages,
            "slowest": slowest,
            "samples": {key: list(self._samples.get(key) or []) for key in _SAMPLE_KEYS},
            "worker_errors": list(self._worker_errors),
        }
        return _fit_report(report)

    def _consider_slow(
        self,
        *,
        venue: str,
        stage: str,
        elapsed_ms: int,
        source_id: str | None,
        outcome: str,
    ) -> None:
        entry = {
            "venue": venue,
            "stage": stage,
            "elapsed_ms": max(0, int(elapsed_ms)),
            "outcome": outcome,
            "source_id": _clip(source_id or "", 80),
        }
        self._slowest.append(entry)
        if len(self._slowest) > DIAGNOSTIC_MAX_SLOWEST * 4:
            self._slowest.sort(key=lambda item: item["elapsed_ms"], reverse=True)
            del self._slowest[DIAGNOSTIC_MAX_SLOWEST:]


def project_collection_diagnostic(report: Any) -> dict[str, Any]:
    """Project an already-finished collector report. No extra provider calls."""

    diagnostics = dict(getattr(report, "scan_diagnostics", None) or {})
    fixtures = list(getattr(report, "discovered_fixtures", None) or [])
    terminals = {
        "evaluated": 0,
        "skipped": 0,
        "revalidation": 0,
        "failed": 0,
        "retry_wait": 0,
        "deferred": 0,
        "not_started": 0,
    }
    for fixture in fixtures:
        state = str(getattr(fixture, "market_evaluation_state", "") or "")
        if state == "evaluated":
            terminals["evaluated"] += 1
        elif state in {"not_evaluated_scan_deadline"}:
            terminals["not_started"] += 1
        elif state in {"market_fetch_unavailable"}:
            terminals["failed"] += 1
        elif state in {
            "cross_venue_unavailable",
            "single_venue_no_cross_venue_candidate",
            "upper_bound_below_min_net",
        }:
            terminals["skipped"] += 1
        else:
            terminals["deferred"] += 1
    stages_in = dict(diagnostics.get("stages") or {})
    providers_in = dict(diagnostics.get("providers") or {})
    stages: list[dict[str, Any]] = []
    for name, bucket in sorted(stages_in.items()):
        if not isinstance(bucket, Mapping):
            continue
        count = int(bucket.get("calls") or 0)
        total = int(bucket.get("elapsed_ms") or 0)
        timeouts = int(bucket.get("timeouts") or 0)
        stages.append(
            {
                "venue": "mixed",
                "stage": str(name),
                "count": count,
                "total_ms": total,
                "avg_ms": int(total / count) if count else 0,
                "p50_ms": None,
                "p95_ms": None,
                "max_ms": None,
                "success": max(0, count - timeouts),
                "timeout": timeouts,
                "rate_limit": 0,
                "capacity_deferred": 0,
                "not_started": 0,
                "error": int(bucket.get("cancels") or 0),
                "percentile_method": "unavailable_collector_totals_only",
            }
        )
    provider_calls = 0
    for bucket in providers_in.values():
        if isinstance(bucket, Mapping):
            provider_calls += int(bucket.get("calls") or 0)
    wall = int(diagnostics.get("total_ms") or 0)
    if wall <= 0:
        started = getattr(report, "started_at", None)
        completed = getattr(report, "completed_at", None)
        if started is not None and completed is not None:
            wall = max(0, int((completed - started).total_seconds() * 1000))
    evaluated = terminals["evaluated"]
    per_second = (evaluated / (wall / 1000.0)) if wall else 0.0
    decisions = list(getattr(report, "paper_decisions", None) or [])
    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION,
        "data_kind": "scan_cycle_diagnostic",
        "lane": str(getattr(report, "scan_lane", "") or diagnostics.get("scan_lane") or ""),
        "note": DIAGNOSTIC_DATA_NOTE,
        "wall_ms": wall,
        "due": len(fixtures) or int(diagnostics.get("fixture_count") or 0),
        "considered": len(fixtures) or int(diagnostics.get("fixture_count") or 0),
        "terminals": terminals,
        "leftover_collapsed": terminals["not_started"]
        + terminals["deferred"]
        + terminals["retry_wait"],
        "decisions": len(decisions),
        "qualifying": int(getattr(report, "qualifying_arbs", 0) or 0),
        "promoted_hot": 0,
        "evaluations_per_second": round(per_second, 4),
        "provider_io_ms_sum": sum(int(stage["total_ms"]) for stage in stages),
        "slot_wait_ms_sum": 0,
        "local_evaluate_ms_sum": int(diagnostics.get("normalize_match_ms") or 0)
        + int(diagnostics.get("assembly_ms") or 0),
        "timing_note": (
            "UNIVERSE/collector projection uses existing stage totals. "
            "Percentiles are not reconstructed. No raw provider body is copied."
        ),
        "saved_provider_calls": 0,
        "skipped_provider_calls": 0,
        "coalesced_provider_calls": 0,
        "repeated_exact_id_calls": 0,
        "distinct_exact_ids": 0,
        "exact_id_tracking_truncated": False,
        "call_shape": {
            "sequential_within_item": False,
            "worker_limit": int(diagnostics.get("cluster_concurrency") or 0),
            "explicit_slice_wall": diagnostics.get("cycle_budget_s") is not None,
            "slice_wall_seconds": diagnostics.get("cycle_budget_s"),
            "provider_limits": dict(diagnostics.get("provider_concurrency") or {}),
            "provider_calls": provider_calls,
            "items_with_provider_calls": int(diagnostics.get("clusters_evaluated") or 0),
            "peak_inflight": dict(diagnostics.get("peak_provider_inflight_by_venue") or {}),
        },
        "stages": stages,
        "slowest": [],
        "samples": {key: [] for key in _SAMPLE_KEYS},
        "worker_errors": [],
        "collector_timeout_count": int(diagnostics.get("timeout_count") or 0),
        "completeness": diagnostics.get("completeness"),
    }


def stored_cycle_diagnostic(report: Any) -> dict[str, Any]:
    diagnostics = dict(getattr(report, "scan_diagnostics", None) or {})
    embedded = diagnostics.get("cycle_diagnostic")
    if isinstance(embedded, dict):
        return _fit_report(embedded)
    return _fit_report(project_collection_diagnostic(report))


def _exact_id_key(venue: str, stage: str, source_id: str | None) -> str | None:
    text = str(source_id or "").strip()
    if not text:
        return None
    return f"{venue}:{stage}:{text[:80]}"


def _clip(value: str, limit: int) -> str:
    text = str(value or "").replace("\n", " ").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"


def _fit_report(report: dict[str, Any]) -> dict[str, Any]:
    import json

    payload = json.dumps(report, separators=(",", ":"), default=str)
    if len(payload.encode("utf-8")) <= DIAGNOSTIC_MAX_REPORT_BYTES:
        return report
    trimmed = dict(report)
    trimmed["samples"] = {key: [] for key in _SAMPLE_KEYS}
    trimmed["slowest"] = []
    trimmed["worker_errors"] = list(report.get("worker_errors") or [])[:2]
    trimmed["truncated"] = True
    payload = json.dumps(trimmed, separators=(",", ":"), default=str)
    if len(payload.encode("utf-8")) <= DIAGNOSTIC_MAX_REPORT_BYTES:
        return trimmed
    trimmed["stages"] = list(report.get("stages") or [])[:12]
    for stage in trimmed["stages"]:
        if isinstance(stage, dict):
            stage.pop("percentile_method", None)
    payload = json.dumps(trimmed, separators=(",", ":"), default=str)
    if len(payload.encode("utf-8")) > DIAGNOSTIC_MAX_REPORT_BYTES:
        trimmed["stages"] = []
        trimmed["truncated"] = True
    return trimmed
