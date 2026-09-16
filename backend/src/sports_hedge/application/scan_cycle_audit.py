"""Completed HOT / UNIVERSE scan-cycle audit rows.

One append-only row per completed refresh cycle, including cycles with zero
paper_decisions. Distinct from per-market ``paper_scan_records``.
"""

from __future__ import annotations

from sports_hedge.application.collector import CollectionReport
from sports_hedge.application.lane_venues import is_provider_health_failure
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.paper.audit import PaperScanCycleRecord, scan_cycle_identity


def coerce_cycle_lane(scan_lane: ScanLane | str | None) -> str:
    if scan_lane is ScanLane.HOT:
        return ScanLane.HOT.value
    if isinstance(scan_lane, ScanLane):
        return ScanLane.UNIVERSE.value
    text = str(scan_lane or "").strip().casefold()
    if text == ScanLane.HOT.value:
        return ScanLane.HOT.value
    return ScanLane.UNIVERSE.value


def build_paper_scan_cycle_record(
    report: CollectionReport,
    *,
    scan_lane: ScanLane | str | None = None,
) -> PaperScanCycleRecord:
    lane = coerce_cycle_lane(scan_lane or report.scan_lane)
    diagnostics = dict(report.scan_diagnostics or {})
    evaluated_count = _count_or_diagnostic(
        report,
        diagnostics,
        state="evaluated",
        diagnostic_key="evaluated_count",
    )
    not_evaluated_count = _count_or_diagnostic(
        report,
        diagnostics,
        state="not_evaluated_scan_deadline",
        diagnostic_key="not_evaluated_count",
    )
    duration_ms = max(
        0, int((report.completed_at - report.started_at).total_seconds() * 1000)
    )
    last_error = _cycle_last_error(report, diagnostics)
    degraded = bool(
        not_evaluated_count > 0
        or last_error
        or any(is_provider_health_failure(value) for value in report.venue_health.values())
    )
    generation_id = diagnostics.get("universe_generation_id")
    work_used = diagnostics.get("generation_work_used_s")
    generation_resume = diagnostics.get("generation_resume")
    completeness = diagnostics.get("completeness")
    return PaperScanCycleRecord(
        cycle_id=scan_cycle_identity(lane, report.started_at, report.completed_at),
        started_at=report.started_at,
        completed_at=report.completed_at,
        scan_lane=lane,
        duration_ms=duration_ms,
        fixture_count=len(report.discovered_fixtures),
        evaluated_count=evaluated_count,
        not_evaluated_count=not_evaluated_count,
        matched_event_pairs=report.matched_event_pairs,
        matched_market_pairs=report.matched_market_pairs,
        paper_decision_count=len(report.paper_decisions),
        qualifying_arb_count=report.qualifying_arbs,
        venue_health=dict(report.venue_health),
        degraded=degraded,
        last_error=last_error,
        universe_generation_id=None if generation_id is None else int(generation_id),
        resume_cursor=report.resume_cursor or diagnostics.get("resume_cursor"),
        completeness=None if completeness is None else str(completeness),
        generation_resume=None if generation_resume is None else bool(generation_resume),
        generation_work_used_s=None if work_used is None else float(work_used),
        operator_summary=report.operator_summary or None,
    )


def _count_or_diagnostic(
    report: CollectionReport,
    diagnostics: dict,
    *,
    state: str,
    diagnostic_key: str,
) -> int:
    counted = sum(
        1 for item in report.discovered_fixtures if item.market_evaluation_state == state
    )
    raw = diagnostics.get(diagnostic_key)
    if raw is None:
        return counted
    try:
        return int(raw)
    except (TypeError, ValueError):
        return counted


def _cycle_last_error(report: CollectionReport, diagnostics: dict) -> str | None:
    persist_error = diagnostics.get("persist_error")
    if persist_error:
        return str(persist_error)
    if report.issues:
        detail = report.issues[0].detail
        if detail:
            return str(detail)
    return None
