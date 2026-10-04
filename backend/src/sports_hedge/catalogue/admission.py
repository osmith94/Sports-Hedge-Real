"""Shared HOT/UNIVERSE catalogue gate.

``CataloguePairAssessment.admitted`` is the only admission decision. The
names below are compatibility views of that value. They are not stored and
cannot diverge.

REVIEW_REQUIRED, UNSUPPORTED, parameter mismatch and known contradiction
cannot reach the solver. This module is scan-lane independent.
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, computed_field

from sports_hedge.catalogue.classify import CataloguePairAssessment, classify_pair
from sports_hedge.catalogue.states import CatalogueApprovalState
from sports_hedge.domain.football import CanonicalMarket
from sports_hedge.matching.markets import MarketMatchResult

CATALOGUE_SHARED_BY = ("hot", "universe")
PAPER_MODE_ADMITTED_STATES = frozenset(
    {
        CatalogueApprovalState.APPROVED_EQUIVALENT,
        CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT,
    }
)


class CatalogueAdmission(BaseModel):
    model_config = ConfigDict(extra="ignore")

    assessment: CataloguePairAssessment
    rejection_reason: str | None = None
    catalogue_shared_by: tuple[str, ...] = CATALOGUE_SHARED_BY
    settlement_assumption: str | None = None

    @computed_field
    @property
    def admitted(self) -> bool:
        return self.assessment.admitted

    @computed_field
    @property
    def allowed(self) -> bool:
        return self.assessment.admitted

    @computed_field
    @property
    def paper_mode_admitted(self) -> bool:
        return self.assessment.admitted

    @computed_field
    @property
    def live_execution_eligible(self) -> bool:
        return self.assessment.admitted


def catalogue_rejection_reason(assessment: CataloguePairAssessment) -> str:
    return f"catalogue_{assessment.state.value}"


def assess_catalogue_admission(
    left: CanonicalMarket,
    right: CanonicalMarket,
    match: MarketMatchResult | None = None,
) -> CatalogueAdmission:
    """Admit from one classification. Does not resolve the register again."""

    assessment = classify_pair(left, right, match=match)
    if assessment.admitted:
        return CatalogueAdmission(
            assessment=assessment,
            settlement_assumption=assessment.settlement_assumption,
        )
    rejection = catalogue_rejection_reason(assessment)
    if assessment.state in {
        CatalogueApprovalState.APPROVED_EQUIVALENT,
        CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT,
    }:
        rejection = "catalogue_not_registered"
    return CatalogueAdmission(
        assessment=assessment,
        rejection_reason=rejection,
    )


def catalogue_allows_solver(
    left: CanonicalMarket,
    right: CanonicalMarket,
    match: MarketMatchResult | None = None,
) -> bool:
    """Compatibility name for the canonical admitted decision."""

    return assess_catalogue_admission(left, right, match=match).admitted


def catalogue_allows_live_execution(
    left: CanonicalMarket,
    right: CanonicalMarket,
    match: MarketMatchResult | None = None,
) -> bool:
    """Compatibility name for the same admitted decision. Places no order."""

    return assess_catalogue_admission(left, right, match=match).admitted
