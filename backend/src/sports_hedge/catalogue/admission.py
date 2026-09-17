"""Shared HOT/UNIVERSE catalogue gate for solver/paper admission.

Only APPROVED_EQUIVALENT may enter the normal arbitrage solver or paper
simulation. This module is scan-lane independent and does not change matcher
semantics.
"""

from __future__ import annotations

from pydantic import BaseModel

from sports_hedge.catalogue.classify import CataloguePairAssessment, classify_pair
from sports_hedge.catalogue.states import CatalogueApprovalState
from sports_hedge.domain.football import CanonicalMarket

CATALOGUE_SHARED_BY = ("hot", "universe")


class CatalogueAdmission(BaseModel):
    allowed: bool
    assessment: CataloguePairAssessment
    rejection_reason: str | None = None
    catalogue_shared_by: tuple[str, ...] = CATALOGUE_SHARED_BY


def catalogue_rejection_reason(assessment: CataloguePairAssessment) -> str:
    return f"catalogue_{assessment.state.value}"


def assess_catalogue_admission(
    left: CanonicalMarket, right: CanonicalMarket
) -> CatalogueAdmission:
    assessment = classify_pair(left, right)
    if assessment.state is CatalogueApprovalState.APPROVED_EQUIVALENT:
        return CatalogueAdmission(allowed=True, assessment=assessment)
    return CatalogueAdmission(
        allowed=False,
        assessment=assessment,
        rejection_reason=catalogue_rejection_reason(assessment),
    )


def catalogue_allows_solver(left: CanonicalMarket, right: CanonicalMarket) -> bool:
    return assess_catalogue_admission(left, right).allowed
