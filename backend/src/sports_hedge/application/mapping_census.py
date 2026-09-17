"""Aggregate mapping/equivalence census from a real CollectionReport.

This is a read-only verification layer over the production collector. It does
not change settlement matching and does not invent counts from mocks.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.application.collector import CollectionReport, MarketEvaluationState
from sports_hedge.application.fixture_inventory import InventoryComparisonStatus
from sports_hedge.application.scan_cycle_audit import (
    issue_is_deadline_partial,
    issue_is_provider_failure,
    issue_is_unsupported_market_skip,
)
from sports_hedge.config import Settings

CENSUS_DATA_CLASS_FIXTURE = "deterministic_fixture"
CENSUS_DATA_CLASS_OWNER_LIVE = "owner_live_read_only"
OWNER_LIVE_CENSUS_ENV = "SPORTS_HEDGE_OWNER_LIVE_CENSUS"
OWNER_LIVE_BANNER = "OWNER-LIVE / READ-ONLY UNIVERSE MAPPING CENSUS"
FIXTURE_BANNER = "DETERMINISTIC FIXTURE MAPPING CENSUS"


class MappingCensus(BaseModel):
    """Safe aggregate mapping counts. Never includes credentials or tokens."""

    data_class: str
    paper_mode: str = "paper"
    execution_enabled: bool = False
    scan_lane: str | None = None
    generation_resume: bool = False
    universe_generation_id: int | None = None
    discovered_fixtures: int = Field(ge=0)
    cross_venue_matched_events: int = Field(ge=0)
    normalized_markets_by_venue: dict[str, int] = Field(default_factory=dict)
    equivalent_market_pairs: int = Field(ge=0)
    market_family_breakdown: dict[str, int] = Field(default_factory=dict)
    comparison_status_breakdown: dict[str, int] = Field(default_factory=dict)
    evaluated_zero_equivalent_fixtures: int = Field(ge=0)
    unsupported_market_skips: int = Field(ge=0)
    skip_failure_reasons: dict[str, int] = Field(default_factory=dict)
    qualifying_arbs: int = Field(ge=0)
    leftover_not_evaluated: int = Field(default=0, ge=0)
    venue_health: dict[str, str] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


def census_from_report(
    report: CollectionReport,
    *,
    data_class: str,
    settings: Settings | None = None,
) -> MappingCensus:
    """Count mapping/equivalence from a completed collector report."""

    resolved = settings or Settings()
    diagnostics = dict(report.scan_diagnostics or {})
    family_counts: Counter[str] = Counter()
    status_counts: Counter[str] = Counter()
    for rows in (report.fixture_markets or {}).values():
        for row in rows:
            status = str(row.comparison_status.value if row.comparison_status else "unknown")
            status_counts[status] += 1
            if row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT:
                family_counts[str(row.family or "unknown")] += 1

    skip_reasons: Counter[str] = Counter()
    unsupported = 0
    for issue in report.issues:
        key = _issue_reason_key(issue)
        skip_reasons[key] += 1
        if issue_is_unsupported_market_skip(issue):
            unsupported += 1

    evaluated_zero = sum(
        1
        for item in report.discovered_fixtures
        if item.market_evaluation_state == MarketEvaluationState.EVALUATED.value
        and (item.matched_equivalent_count or 0) == 0
    )
    leftover = sum(
        1
        for item in report.discovered_fixtures
        if item.market_evaluation_state
        == MarketEvaluationState.NOT_EVALUATED_SCAN_DEADLINE.value
    )
    cross_venue = sum(
        1
        for item in report.discovered_fixtures
        if sum(
            [
                bool(item.matchbook_matched),
                bool(item.polymarket_matched),
                bool(item.kalshi_matched),
            ]
        )
        >= 2
    )
    notes = [
        "PAPER MODE · EXECUTION DISABLED",
        (
            "Deterministic fixture counts. Not owner-live evidence."
            if data_class == CENSUS_DATA_CLASS_FIXTURE
            else "Owner-live/read-only diagnostic. Distinct from deterministic fixture census."
        ),
    ]
    if data_class == CENSUS_DATA_CLASS_OWNER_LIVE:
        notes.append("One bounded UNIVERSE collect. No venue writes. No credential output.")
    return MappingCensus(
        data_class=data_class,
        paper_mode=resolved.sports_hedge_mode,
        execution_enabled=bool(resolved.sports_hedge_execution_enabled),
        scan_lane=report.scan_lane,
        generation_resume=bool(diagnostics.get("generation_resume")),
        universe_generation_id=_optional_int(diagnostics.get("universe_generation_id")),
        discovered_fixtures=len(report.discovered_fixtures),
        cross_venue_matched_events=cross_venue,
        normalized_markets_by_venue={
            "matchbook": int(report.normalized_matchbook_markets),
            "polymarket": int(report.normalized_polymarket_markets),
            "kalshi": int(report.normalized_kalshi_markets),
        },
        equivalent_market_pairs=int(report.matched_market_pairs),
        market_family_breakdown=dict(sorted(family_counts.items())),
        comparison_status_breakdown=dict(sorted(status_counts.items())),
        evaluated_zero_equivalent_fixtures=evaluated_zero,
        unsupported_market_skips=unsupported,
        skip_failure_reasons=dict(sorted(skip_reasons.items())),
        qualifying_arbs=int(report.qualifying_arbs),
        leftover_not_evaluated=leftover,
        venue_health=dict(report.venue_health or {}),
        notes=notes,
    )


def render_census(census: MappingCensus) -> str:
    """Operator-facing aggregate text. No secrets."""

    banner = (
        OWNER_LIVE_BANNER
        if census.data_class == CENSUS_DATA_CLASS_OWNER_LIVE
        else FIXTURE_BANNER
    )
    lines = [
        banner,
        f"data_class={census.data_class}",
        f"paper_mode={census.paper_mode} execution_enabled={census.execution_enabled}",
        f"scan_lane={census.scan_lane or 'n/a'} generation_resume={census.generation_resume}",
        f"discovered_fixtures={census.discovered_fixtures}",
        f"cross_venue_matched_events={census.cross_venue_matched_events}",
        f"normalized_markets_by_venue={_fmt_counts(census.normalized_markets_by_venue)}",
        f"equivalent_market_pairs={census.equivalent_market_pairs}",
        f"market_family_breakdown={_fmt_counts(census.market_family_breakdown)}",
        f"comparison_status_breakdown={_fmt_counts(census.comparison_status_breakdown)}",
        f"evaluated_zero_equivalent_fixtures={census.evaluated_zero_equivalent_fixtures}",
        f"unsupported_market_skips={census.unsupported_market_skips}",
        f"skip_failure_reasons={_fmt_counts(census.skip_failure_reasons)}",
        f"qualifying_arbs={census.qualifying_arbs}",
        f"leftover_not_evaluated={census.leftover_not_evaluated}",
        f"venue_health={_fmt_counts(census.venue_health)}",
    ]
    lines.extend(f"note: {note}" for note in census.notes)
    return "\n".join(lines) + "\n"


def census_as_public_dict(census: MappingCensus) -> dict[str, Any]:
    return census.model_dump(mode="json")


def _issue_reason_key(issue: object) -> str:
    stage = str(getattr(issue, "stage", "") or "other").strip() or "other"
    venue_obj = getattr(issue, "venue", None)
    venue = str(getattr(venue_obj, "value", venue_obj) or "none")
    if issue_is_unsupported_market_skip(issue):
        return f"unsupported_market:{venue}"
    if issue_is_deadline_partial(issue):
        return "deadline_leftover"
    if issue_is_provider_failure(issue):
        return f"provider_failure:{stage}:{venue}"
    return f"{stage}:{venue}"


def _optional_int(value: object) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _fmt_counts(values: dict[str, Any]) -> str:
    if not values:
        return "{}"
    parts = [f"{key}={values[key]}" for key in values]
    return "{" + ", ".join(parts) + "}"
