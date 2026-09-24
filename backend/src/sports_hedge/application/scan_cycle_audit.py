"""Completed HOT / BACKGROUND / UNIVERSE scan-cycle audit rows.

One append-only row per completed refresh cycle, including cycles with zero
paper_decisions. Distinct from per-market ``paper_scan_records``.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sports_hedge.application.collector import CollectionReport, CollectorIssue
from sports_hedge.application.executable_liquidity import decision_is_solver_arbitrage
from sports_hedge.application.lane_venues import is_provider_health_failure
from sports_hedge.application.scan_lanes import OPERATOR_BACKGROUND_PRICING_LABEL, ScanLane
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.audit import PaperScanCycleRecord, scan_cycle_identity

BACKGROUND_CYCLE_LANE = "background"
PRICE_ENGINE_ITEM_COMPLETION_CAPTURE = "item_completion_capture"
_PRICE_ENGINE_VENUES = (VenueName.MATCHBOOK, VenueName.KALSHI)

SCAN_CYCLE_DEADLINE_DETAIL = "scan_cycle_deadline_reached"
UNSUPPORTED_MARKET_STAGES = frozenset({"normalize_market"})
PER_ITEM_SKIP_STAGES = frozenset({"normalize_market", "normalize_event"})
PROVIDER_FAILURE_STAGES = frozenset(
    {
        "list_events",
        "list_markets",
        "get_order_book",
        "get_series",
        "get_market",
        "get_contract_terms",
        "order_book",
    }
)


def price_engine_slice_count_fields(result: Any) -> dict[str, Any]:
    """Counts for HOT/BACKGROUND cycle history.

    ``fixture_count`` here is considered catalogue/market rows, not unique
    fixtures. Unique fixture count is omitted rather than invented.
    """

    evaluated = list(getattr(result, "evaluated", []) or [])
    failed = list(getattr(result, "failed", []) or [])
    deferred = list(getattr(result, "deferred", []) or [])
    not_started = list(getattr(result, "not_started", []) or [])
    retry_wait = list(getattr(result, "retry_wait", []) or [])
    revalidation = list(getattr(result, "revalidation", []) or [])
    considered = list(
        dict.fromkeys(
            [*evaluated, *failed, *deferred, *not_started, *retry_wait, *revalidation]
        )
    )
    leftover = len(not_started) + len(deferred) + len(retry_wait)
    return {
        "evaluated": evaluated,
        "evaluated_count": len(evaluated),
        "not_evaluated_count": leftover,
        "fixture_count": len(considered),
        "count_unit": "catalogue_rows",
        "deferred": deferred,
        "not_started": not_started,
    }


def coerce_cycle_lane(scan_lane: ScanLane | str | None) -> str:
    if scan_lane is ScanLane.HOT:
        return ScanLane.HOT.value
    if isinstance(scan_lane, ScanLane):
        return ScanLane.UNIVERSE.value
    text = str(scan_lane or "").strip().casefold()
    if text == ScanLane.HOT.value:
        return ScanLane.HOT.value
    if text == BACKGROUND_CYCLE_LANE:
        return BACKGROUND_CYCLE_LANE
    return ScanLane.UNIVERSE.value


def build_background_price_engine_cycle_report(
    result: Any,
    *,
    started_at: datetime,
    completed_at: datetime,
) -> CollectionReport:
    """Wrap an already-produced BACKGROUND price-engine result for cycle history.

    Telemetry only: no discovery, no extra provider call, no fabricated work.
    Counts come from the slice result that already ran.
    """

    decisions = list(getattr(result, "decisions", []) or [])
    counts = price_engine_slice_count_fields(result)
    qualifying = sum(1 for decision in decisions if decision_is_solver_arbitrage(decision))
    return CollectionReport(
        started_at=started_at,
        completed_at=completed_at,
        matching_venues=list(_PRICE_ENGINE_VENUES),
        enabled_venues=list(_PRICE_ENGINE_VENUES),
        paper_decisions=decisions,
        issues=list(getattr(result, "issues", []) or []),
        scan_lane=BACKGROUND_CYCLE_LANE,
        qualifying_arbs=qualifying,
        venue_health=dict(getattr(result, "venue_health", {}) or {}),
        operation_health=dict(getattr(result, "operation_health", {}) or {}),
        operator_summary=OPERATOR_BACKGROUND_PRICING_LABEL,
        scan_diagnostics={
            "price_engine": True,
            "priority": BACKGROUND_CYCLE_LANE,
            **counts,
            "legacy_hot_collector": False,
            PRICE_ENGINE_ITEM_COMPLETION_CAPTURE: True,
            "persist_failures": list(getattr(result, "persist_failures", []) or []),
            **(
                result.viability_diagnostics()
                if hasattr(result, "viability_diagnostics")
                else {}
            ),
            **(
                {"cycle_diagnostic": dict(result.diagnostic)}
                if isinstance(getattr(result, "diagnostic", None), dict)
                else {}
            ),
        },
    )


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
    fixture_count = len(report.discovered_fixtures)
    raw_fixture_count = diagnostics.get("fixture_count")
    if raw_fixture_count is not None:
        try:
            fixture_count = int(raw_fixture_count)
        except (TypeError, ValueError):
            pass
    last_error = cycle_last_error(report, diagnostics)
    skipped_unsupported = sum(
        1 for issue in report.issues if issue_is_unsupported_market_skip(issue)
    )
    degraded = bool(
        not_evaluated_count > 0
        or last_error
        or any(is_provider_health_failure(value) for value in report.venue_health.values())
    )
    operator_summary = _cycle_operator_summary(
        report.operator_summary,
        last_error=last_error,
        not_evaluated_count=not_evaluated_count,
        skipped_unsupported=skipped_unsupported,
        matched_event_pairs=report.matched_event_pairs,
        matched_market_pairs=report.matched_market_pairs,
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
        fixture_count=fixture_count,
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
        operator_summary=operator_summary,
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


def cycle_last_error(report: CollectionReport, diagnostics: dict | None = None) -> str | None:
    payload = diagnostics if diagnostics is not None else dict(report.scan_diagnostics or {})
    persist_error = payload.get("persist_error")
    if persist_error:
        return str(persist_error)
    for issue in report.issues:
        if issue_is_provider_failure(issue):
            detail = issue.detail
            if detail:
                return str(detail)
    return None


def issue_is_deadline_partial(issue: CollectorIssue | object) -> bool:
    detail = str(getattr(issue, "detail", "") or "").strip()
    return detail == SCAN_CYCLE_DEADLINE_DETAIL


def issue_is_unsupported_market_skip(issue: CollectorIssue | object) -> bool:
    stage = str(getattr(issue, "stage", "") or "").strip()
    detail = str(getattr(issue, "detail", "") or "")
    if stage in UNSUPPORTED_MARKET_STAGES:
        return True
    return "Unsupported Matchbook market:" in detail


def issue_is_per_item_skip(issue: CollectorIssue | object) -> bool:
    if issue_is_deadline_partial(issue) or issue_is_unsupported_market_skip(issue):
        return True
    stage = str(getattr(issue, "stage", "") or "").strip()
    return stage in PER_ITEM_SKIP_STAGES


def issue_is_provider_failure(issue: CollectorIssue | object) -> bool:
    if issue_is_per_item_skip(issue):
        return False
    stage = str(getattr(issue, "stage", "") or "").strip()
    detail = str(getattr(issue, "detail", "") or "").casefold()
    if stage in PROVIDER_FAILURE_STAGES:
        return True
    if "timeout" in detail or "429" in detail:
        return True
    return False


def _cycle_operator_summary(
    existing: str | None,
    *,
    last_error: str | None,
    not_evaluated_count: int,
    skipped_unsupported: int,
    matched_event_pairs: int,
    matched_market_pairs: int,
) -> str | None:
    notes: list[str] = []
    if last_error:
        notes.append(f"provider failure · {last_error}")
    elif not_evaluated_count > 0:
        notes.append(f"partial · {not_evaluated_count} not evaluated")
    elif skipped_unsupported:
        notes.append(f"{skipped_unsupported} unsupported markets skipped")
    elif matched_event_pairs > 0 and matched_market_pairs == 0:
        notes.append("evaluated · 0 equivalent markets")
    if existing and existing.strip():
        if notes:
            extra = " · ".join(notes)
            if extra not in existing:
                return f"{existing.strip()} · {extra}"
        return existing.strip()
    return " · ".join(notes) or None
