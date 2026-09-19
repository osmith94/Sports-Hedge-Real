"""Fixture and UNIVERSE coverage rows from the Issue #316 registry.

Diagnostics only. Paper-mode solver admission remains
`catalogue_allows_solver` (APPROVED_EQUIVALENT or PAPER_ASSUMED_EQUIVALENT).
Live execution stays independently proven APPROVED_EQUIVALENT only.
"""

from __future__ import annotations

from collections import Counter
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.catalogue.classify import venue_pair_key
from sports_hedge.catalogue.registry import (
    REGISTRY_SHARED_BY,
    REGISTRY_VERSION,
    CatalogueCoverageState,
    display_label,
    family_to_registry_archetype,
    registry_cell,
    target_archetypes,
    venue_status,
)
from sports_hedge.catalogue.states import CatalogueArchetype
from sports_hedge.domain.football import MarketFamily, line_push_possible
from sports_hedge.domain.models import VenueName


class FixtureArchetypeCoverage(BaseModel):
    archetype: CatalogueArchetype
    display_label: str
    state: CatalogueCoverageState
    reason: str
    operational_approved: bool = False
    line: str | None = None
    matchbook_present: bool = False
    kalshi_present: bool = False
    polymarket_present: bool = False
    catalogue_shared_by: tuple[str, ...] = REGISTRY_SHARED_BY
    registry_version: str = REGISTRY_VERSION

    def operator_line(self) -> str:
        label = f"{self.display_label:<16}"
        state = self.state.value.upper()
        reason = self.reason.replace("_", " ")
        return f"{label}{state} — {reason}"


class FixtureCatalogueCoverage(BaseModel):
    venue_pair: str | None = None
    rows: list[FixtureArchetypeCoverage] = Field(default_factory=list)
    approved_equivalent: int = 0
    paper_assumed_equivalent: int = 0
    review_required: int = 0
    unsupported: int = 0
    venue_unavailable: int = 0
    not_listed: int = 0
    registry_version: str = REGISTRY_VERSION
    catalogue_shared_by: tuple[str, ...] = REGISTRY_SHARED_BY

    def operator_lines(self) -> list[str]:
        return [row.operator_line() for row in self.rows]


def coverage_venue_pair(
    *,
    matchbook: bool,
    kalshi: bool,
    polymarket: bool,
) -> str | None:
    venues: list[VenueName] = []
    if matchbook:
        venues.append(VenueName.MATCHBOOK)
    if kalshi:
        venues.append(VenueName.KALSHI)
    if polymarket:
        venues.append(VenueName.POLYMARKET)
    if len(venues) < 2:
        return None
    if len(venues) == 2:
        return venue_pair_key(venues[0], venues[1])
    return venue_pair_key(VenueName.MATCHBOOK, VenueName.KALSHI)


def fixture_catalogue_coverage(
    inventory_rows: list[Any] | None,
    *,
    matchbook_matched: bool = False,
    kalshi_matched: bool = False,
    polymarket_matched: bool = False,
    target_competition_code: str | None = None,
) -> FixtureCatalogueCoverage:
    """One diagnostic row per Tenet-20 target archetype for a fixture."""

    pair = coverage_venue_pair(
        matchbook=matchbook_matched,
        kalshi=kalshi_matched,
        polymarket=polymarket_matched,
    ) or "matchbook_kalshi"
    observed = _index_inventory(inventory_rows or [])
    rows: list[FixtureArchetypeCoverage] = []
    for archetype in target_archetypes():
        rows.append(
            _row_for_archetype(
                archetype,
                pair,
                observed=observed,
                matchbook_matched=matchbook_matched,
                kalshi_matched=kalshi_matched,
                polymarket_matched=polymarket_matched,
                target_competition_code=target_competition_code,
            )
        )
    return FixtureCatalogueCoverage(
        venue_pair=pair,
        rows=rows,
        approved_equivalent=_count(rows, CatalogueCoverageState.APPROVED_EQUIVALENT),
        paper_assumed_equivalent=_count(rows, CatalogueCoverageState.PAPER_ASSUMED_EQUIVALENT),
        review_required=_count(rows, CatalogueCoverageState.REVIEW_REQUIRED),
        unsupported=_count(rows, CatalogueCoverageState.UNSUPPORTED),
        venue_unavailable=_count(rows, CatalogueCoverageState.VENUE_UNAVAILABLE),
        not_listed=_count(rows, CatalogueCoverageState.NOT_LISTED),
    )


def aggregate_coverage_by_archetype(
    fixtures: list[FixtureCatalogueCoverage],
) -> dict[str, dict[str, int]]:
    totals: dict[str, Counter[str]] = {item.value: Counter() for item in target_archetypes()}
    for coverage in fixtures:
        for row in coverage.rows:
            totals[row.archetype.value][row.state.value] += 1
    return {archetype: dict(sorted(counts.items())) for archetype, counts in totals.items()}


def _count(rows: list[FixtureArchetypeCoverage], state: CatalogueCoverageState) -> int:
    return sum(1 for row in rows if row.state is state)


def _index_inventory(inventory_rows: list[Any]) -> dict[CatalogueArchetype, list[Any]]:
    indexed: dict[CatalogueArchetype, list[Any]] = {}
    for row in inventory_rows:
        family = getattr(row, "family", None)
        if family is None:
            continue
        try:
            market_family = family if isinstance(family, MarketFamily) else MarketFamily(str(family))
        except ValueError:
            continue
        integer = None
        if market_family is MarketFamily.TOTAL_GOALS:
            integer = line_push_possible(getattr(row, "line", None)) is True
        archetype = family_to_registry_archetype(market_family, integer_line=integer)
        if archetype is None:
            continue
        indexed.setdefault(archetype, []).append(row)
    return indexed


def _row_for_archetype(
    archetype: CatalogueArchetype,
    venue_pair: str,
    *,
    observed: dict[CatalogueArchetype, list[Any]],
    matchbook_matched: bool,
    kalshi_matched: bool,
    polymarket_matched: bool,
    target_competition_code: str | None,
) -> FixtureArchetypeCoverage:
    cell = registry_cell(archetype, venue_pair)
    hits = observed.get(archetype, [])
    line = _line_label(hits)
    mb_present = any(getattr(row, "matchbook", None) is not None for row in hits)
    kalshi_present = any(getattr(row, "kalshi", None) is not None for row in hits)
    pm_present = any(getattr(row, "polymarket", None) is not None for row in hits)
    if not cell.venue_available:
        return FixtureArchetypeCoverage(
            archetype=archetype,
            display_label=display_label(archetype, line=line),
            state=CatalogueCoverageState.VENUE_UNAVAILABLE,
            reason=cell.reason,
            matchbook_present=mb_present,
            kalshi_present=kalshi_present,
            polymarket_present=pm_present,
        )
    kalshi_status = venue_status(archetype, "kalshi")
    if (
        venue_pair == "matchbook_kalshi"
        and target_competition_code
        and target_competition_code in kalshi_status.kalshi_series_missing_competitions
    ):
        return FixtureArchetypeCoverage(
            archetype=archetype,
            display_label=display_label(archetype, line=line),
            state=CatalogueCoverageState.VENUE_UNAVAILABLE,
            reason=(
                f"Kalshi {kalshi_status.kalshi_series_suffix or archetype.value} "
                f"series is not configured for {target_competition_code}"
            ),
            matchbook_present=mb_present,
            kalshi_present=kalshi_present,
            polymarket_present=pm_present,
        )

    if hits:
        state, reason, operational = _state_from_hits(hits)
        pair_sides = {
            "matchbook_kalshi": (mb_present, kalshi_present),
            "matchbook_polymarket": (mb_present, pm_present),
            "kalshi_polymarket": (kalshi_present, pm_present),
        }
        left_present, right_present = pair_sides.get(venue_pair, (True, True))
        if (
            not (left_present and right_present)
            and state is CatalogueCoverageState.REVIEW_REQUIRED
        ):
            state = CatalogueCoverageState.NOT_LISTED
            reason = "venue_can_offer_archetype_but_this_fixture_has_no_listed_market"
            operational = False
        return FixtureArchetypeCoverage(
            archetype=archetype,
            display_label=display_label(archetype, line=line),
            state=state,
            reason=reason,
            operational_approved=operational,
            line=line,
            matchbook_present=mb_present,
            kalshi_present=kalshi_present,
            polymarket_present=pm_present,
        )

    return FixtureArchetypeCoverage(
        archetype=archetype,
        display_label=display_label(archetype),
        state=CatalogueCoverageState.NOT_LISTED,
        reason="venue_can_offer_archetype_but_this_fixture_has_no_listed_market",
        matchbook_present=matchbook_matched if "matchbook" in venue_pair else False,
        kalshi_present=kalshi_matched if "kalshi" in venue_pair else False,
        polymarket_present=polymarket_matched if "polymarket" in venue_pair else False,
    )


def _state_from_hits(hits: list[Any]) -> tuple[CatalogueCoverageState, str, bool]:
    from sports_hedge.application.fixture_inventory import (
        InventoryComparisonStatus,
        inventory_is_comparable_opportunity,
    )

    equivalent = [
        row
        for row in hits
        if inventory_is_comparable_opportunity(getattr(row, "comparison_status", None))
    ]
    paper = [
        row
        for row in equivalent
        if getattr(row, "comparison_status", None)
        is InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT
    ]
    approved = [
        row
        for row in equivalent
        if getattr(row, "comparison_status", None)
        is InventoryComparisonStatus.MATCHED_EQUIVALENT
    ]
    if paper and not approved:
        reason = getattr(paper[0], "reason", None) or "paper_assumed_equivalent"
        return CatalogueCoverageState.PAPER_ASSUMED_EQUIVALENT, str(reason), False
    if approved or equivalent:
        reason = getattr((approved or equivalent)[0], "reason", None) or "approved_equivalent"
        return CatalogueCoverageState.APPROVED_EQUIVALENT, str(reason), True
    mismatch = _parameter_mismatch_from_hits(hits)
    if mismatch is not None:
        return mismatch
    for row in hits:
        raw_reason = str(getattr(row, "reason", None) or "")
        status = getattr(row, "comparison_status", None)
        folded = raw_reason.casefold()
        token_blob = " ".join(
            [
                folded,
                *[str(item).casefold() for item in getattr(row, "match_reasons", []) or []],
                *[str(item).casefold() for item in getattr(row, "rejection_reasons", []) or []],
            ]
        )
        if "parameter" in token_blob or "line_mismatch" in token_blob:
            return CatalogueCoverageState.APPROVED_PARAMETER_MISMATCH, raw_reason or "line_mismatch", False
        if "contradiction" in token_blob or folded == "settlement_mismatch":
            return CatalogueCoverageState.KNOWN_CONTRADICTION, raw_reason, False
        if status is InventoryComparisonStatus.SETTLEMENT_MISMATCH:
            return CatalogueCoverageState.KNOWN_CONTRADICTION, raw_reason, False
        if "unsupported" in token_blob or status is InventoryComparisonStatus.UNSUPPORTED_FAMILY:
            return CatalogueCoverageState.UNSUPPORTED, raw_reason, False
        if (
            "catalogue_review_required" in token_blob
            or "incomplete_settlement" in token_blob
            or "review_required" in token_blob
        ):
            return CatalogueCoverageState.REVIEW_REQUIRED, _human_review_reason(raw_reason), False
    first = hits[0]
    raw_reason = str(getattr(first, "reason", None) or "catalogue_not_approved")
    return CatalogueCoverageState.REVIEW_REQUIRED, _human_review_reason(raw_reason), False


def _parameter_mismatch_from_hits(
    hits: list[Any],
) -> tuple[CatalogueCoverageState, str, bool] | None:
    """Exact-line mismatch is visible even when inventory keeps venue-only rows."""

    mb_lines = {
        _line_token(row)
        for row in hits
        if getattr(row, "matchbook", None) is not None and _line_token(row)
    }
    kalshi_lines = {
        _line_token(row)
        for row in hits
        if getattr(row, "kalshi", None) is not None and _line_token(row)
    }
    pm_lines = {
        _line_token(row)
        for row in hits
        if getattr(row, "polymarket", None) is not None and _line_token(row)
    }
    venue_lines = [group for group in (mb_lines, kalshi_lines, pm_lines) if group]
    if len(venue_lines) < 2:
        return None
    if any(left != right for left in venue_lines for right in venue_lines):
        return (
            CatalogueCoverageState.APPROVED_PARAMETER_MISMATCH,
            "line_mismatch",
            False,
        )
    return None


def _line_token(row: Any) -> str:
    line = getattr(row, "line", None)
    return "" if line is None else str(line)


def _human_review_reason(reason: str) -> str:
    folded = reason.casefold()
    if "incomplete_settlement" in folded or "catalogue_review_required" in folded:
        return "Kalshi settlement proof missing"
    return reason


def _line_label(hits: list[Any]) -> str | None:
    from sports_hedge.application.fixture_inventory import inventory_is_comparable_opportunity

    fallback: str | None = None
    for row in hits:
        line = getattr(row, "line", None)
        if line is None or str(line) == "":
            continue
        token = str(line)
        if inventory_is_comparable_opportunity(getattr(row, "comparison_status", None)):
            return token
        if fallback is None:
            fallback = token
    return fallback
