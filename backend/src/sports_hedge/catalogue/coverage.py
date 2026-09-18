"""Family-level before/after coverage for catalogue census v1.

Before = R4 MarketMatcher match plus current solver_model_for_pair admission.
After = Tenet 20 catalogue state. Fixture/demo corpus only.
"""

from __future__ import annotations

from collections import Counter

from pydantic import BaseModel, Field

from sports_hedge.catalogue.classify import classify_payload_pair
from sports_hedge.catalogue.corpus import CorpusEntry, census_corpus
from sports_hedge.catalogue.states import CatalogueApprovalState, CatalogueArchetype

CENSUS_PARENT_COMMIT = "620d1807473a5bfaa08a5023d2a28f4da756efe3"


class FamilyCoverage(BaseModel):
    archetype: CatalogueArchetype
    known_good: int = 0
    known_bad: int = 0
    approved_equivalent: int = 0
    paper_assumed_equivalent: int = 0
    review_required: int = 0
    parameter_mismatch: int = 0
    known_contradiction: int = 0
    unsupported: int = 0
    before_solver_admitted: int = 0
    after_approved: int = 0
    matcher_catalogue_conflicts: int = 0


class CatalogueCoverageReport(BaseModel):
    data_class: str = "deterministic_fixture"
    parent_commit: str = CENSUS_PARENT_COMMIT
    paper_mode: str = "paper"
    execution_enabled: bool = False
    catalogue_shared_by: tuple[str, ...] = ("hot", "universe")
    corpus_size: int = 0
    known_good_retained: int = 0
    known_bad_still_rejected: int = 0
    unexpected_known_good_regressions: int = 0
    unexpected_known_bad_approvals: int = 0
    review_required_count: int = 0
    paper_assumed_count: int = 0
    unsupported_count: int = 0
    matcher_catalogue_conflicts: int = 0
    families: dict[str, FamilyCoverage] = Field(default_factory=dict)
    conflict_entry_ids: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


def coverage_from_corpus(
    entries: tuple[CorpusEntry, ...] | None = None,
) -> CatalogueCoverageReport:
    corpus = entries or census_corpus()
    family_rows: dict[CatalogueArchetype, FamilyCoverage] = {
        archetype: FamilyCoverage(archetype=archetype) for archetype in CatalogueArchetype
    }
    known_good_ok = 0
    known_bad_ok = 0
    good_regressions = 0
    bad_approvals = 0
    review = 0
    paper_assumed = 0
    unsupported = 0
    conflicts = 0
    conflict_ids: list[str] = []
    for entry in corpus:
        assessment = classify_payload_pair(entry.left, entry.right)
        row = family_rows[entry.archetype]
        if entry.known_kind == "known_good":
            row.known_good += 1
        elif entry.known_kind == "paper_assumed":
            pass
        else:
            row.known_bad += 1
        state = assessment.state
        if state is CatalogueApprovalState.APPROVED_EQUIVALENT:
            row.approved_equivalent += 1
        elif state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT:
            row.paper_assumed_equivalent += 1
            paper_assumed += 1
        elif state is CatalogueApprovalState.REVIEW_REQUIRED:
            row.review_required += 1
            review += 1
        elif state is CatalogueApprovalState.APPROVED_PARAMETER_MISMATCH:
            row.parameter_mismatch += 1
        elif state is CatalogueApprovalState.KNOWN_CONTRADICTION:
            row.known_contradiction += 1
        else:
            row.unsupported += 1
            unsupported += 1
        before = bool(assessment.matcher_matched and assessment.solver_model)
        if before:
            row.before_solver_admitted += 1
        if state is CatalogueApprovalState.APPROVED_EQUIVALENT:
            row.after_approved += 1
        if assessment.known_conflict_with_current_matcher:
            row.matcher_catalogue_conflicts += 1
            conflicts += 1
            conflict_ids.append(entry.entry_id)
        if entry.known_kind == "known_good":
            if state is entry.expected_state:
                known_good_ok += 1
            else:
                good_regressions += 1
        elif entry.known_kind == "paper_assumed":
            if state is not entry.expected_state:
                good_regressions += 1
            elif state is CatalogueApprovalState.APPROVED_EQUIVALENT:
                bad_approvals += 1
        else:
            if state is entry.expected_state and state is not CatalogueApprovalState.APPROVED_EQUIVALENT:
                known_bad_ok += 1
            elif state is CatalogueApprovalState.APPROVED_EQUIVALENT:
                bad_approvals += 1
            elif state is not entry.expected_state:
                good_regressions += 1
    return CatalogueCoverageReport(
        corpus_size=len(corpus),
        known_good_retained=known_good_ok,
        known_bad_still_rejected=known_bad_ok,
        unexpected_known_good_regressions=good_regressions,
        unexpected_known_bad_approvals=bad_approvals,
        review_required_count=review,
        paper_assumed_count=paper_assumed,
        unsupported_count=unsupported,
        matcher_catalogue_conflicts=conflicts,
        families={item.value: family_rows[item] for item in CatalogueArchetype},
        conflict_entry_ids=conflict_ids,
        notes=[
            "PAPER MODE · EXECUTION DISABLED",
            "Deterministic fixture corpus. Not owner-live evidence.",
            "HOT and UNIVERSE share this catalogue; classifier has no scan_lane.",
            (
                "Before = matcher.matched and solver_model_for_pair capability. "
                "After = Tenet 20 catalogue state. Production paper scan admits "
                "APPROVED_EQUIVALENT and PAPER_ASSUMED_EQUIVALENT (Matchbook↔Kalshi "
                "locked four families, settlement_assumption=regulation_time). "
                "GAMEWIN-unknown ordinary 1X2 and structurally matched BTTS/TOTAL/FTTS "
                "without independent settlement proof are paper-assumed, never "
                "live-execution eligible. Extra-time / penalties / to-qualify stay "
                "fail-closed. Kalshi fair-price wording does not block PAPER admission."
            ),
        ],
    )


def render_coverage_markdown(report: CatalogueCoverageReport | None = None) -> str:
    coverage = report or coverage_from_corpus()
    lines = [
        f"Corpus size: {coverage.corpus_size}",
        f"Known-good retained: {coverage.known_good_retained}",
        f"Known-bad still rejected: {coverage.known_bad_still_rejected}",
        f"Unexpected known-good regressions: {coverage.unexpected_known_good_regressions}",
        f"Unexpected known-bad approvals: {coverage.unexpected_known_bad_approvals}",
        f"REVIEW_REQUIRED: {coverage.review_required_count}",
        f"PAPER_ASSUMED_EQUIVALENT: {coverage.paper_assumed_count}",
        f"UNSUPPORTED: {coverage.unsupported_count}",
        f"Matcher vs catalogue conflicts: {coverage.matcher_catalogue_conflicts}",
        "",
        "| Archetype | Known-good | Before solver | After approved | REVIEW_REQUIRED | UNSUPPORTED | Conflicts |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for key, row in coverage.families.items():
        lines.append(
            "| "
            + " | ".join(
                [
                    key,
                    str(row.known_good),
                    str(row.before_solver_admitted),
                    str(row.after_approved),
                    str(row.review_required),
                    str(row.unsupported),
                    str(row.matcher_catalogue_conflicts),
                ]
            )
            + " |"
        )
    if coverage.conflict_entry_ids:
        lines.append("")
        lines.append("Conflict corpus ids: " + ", ".join(coverage.conflict_entry_ids))
    return "\n".join(lines)


def family_count_histogram(report: CatalogueCoverageReport) -> dict[str, int]:
    counts: Counter[str] = Counter()
    for row in report.families.values():
        counts["approved_equivalent"] += row.approved_equivalent
        counts["paper_assumed_equivalent"] += row.paper_assumed_equivalent
        counts["review_required"] += row.review_required
        counts["parameter_mismatch"] += row.parameter_mismatch
        counts["known_contradiction"] += row.known_contradiction
        counts["unsupported"] += row.unsupported
    return dict(counts)
