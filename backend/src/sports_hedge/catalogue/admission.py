"""Shared HOT/UNIVERSE catalogue gate for solver/paper admission.

Runtime PAPER comparison has exactly one authority: the Approved Match
Register. No register entry means no runtime match and no paper solver
admission, even when a legacy settlement fingerprint is independently
complete. Independently proven APPROVED_EQUIVALENT remains offline
census/onboarding knowledge until that venue archetype is registered.

Registered PAPER_ASSUMED_EQUIVALENT / APPROVED_EQUIVALENT rows carry
settlement_assumption=regulation_time. That historical paper label does not
by itself reject Real execution of an already-admitted relationship.

REVIEW_REQUIRED, UNSUPPORTED, parameter mismatch and known contradiction
cannot reach the solver. This module is scan-lane independent.
"""

from __future__ import annotations

from pydantic import BaseModel

from sports_hedge.catalogue.classify import CataloguePairAssessment, classify_pair
from sports_hedge.catalogue.states import CatalogueApprovalState
from sports_hedge.domain.football import CanonicalMarket

CATALOGUE_SHARED_BY = ("hot", "universe")
PAPER_MODE_ADMITTED_STATES = frozenset(
    {
        CatalogueApprovalState.APPROVED_EQUIVALENT,
        CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT,
    }
)


class CatalogueAdmission(BaseModel):
    allowed: bool
    assessment: CataloguePairAssessment
    rejection_reason: str | None = None
    catalogue_shared_by: tuple[str, ...] = CATALOGUE_SHARED_BY
    paper_mode_admitted: bool = False
    live_execution_eligible: bool = False
    settlement_assumption: str | None = None


def catalogue_rejection_reason(assessment: CataloguePairAssessment) -> str:
    return f"catalogue_{assessment.state.value}"


def assess_catalogue_admission(
    left: CanonicalMarket, right: CanonicalMarket
) -> CatalogueAdmission:
    assessment = classify_pair(left, right)
    from sports_hedge.matching.approved_register import registered_structural_match
    from sports_hedge.tennis.settlement import tennis_executable_block_reason

    block = tennis_executable_block_reason(left, right)
    if block is not None and registered_structural_match(left, right):
        return CatalogueAdmission(
            allowed=False,
            assessment=assessment,
            rejection_reason=block,
            paper_mode_admitted=False,
            live_execution_eligible=False,
        )
    if assessment.paper_mode_admitted and registered_structural_match(left, right):
        return CatalogueAdmission(
            allowed=True,
            assessment=assessment,
            paper_mode_admitted=True,
            live_execution_eligible=True,
            settlement_assumption=assessment.settlement_assumption,
        )
    rejection = catalogue_rejection_reason(assessment)
    if assessment.state in {
        CatalogueApprovalState.APPROVED_EQUIVALENT,
        CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT,
    }:
        rejection = "catalogue_not_registered"
    return CatalogueAdmission(
        allowed=False,
        assessment=assessment,
        rejection_reason=rejection,
        paper_mode_admitted=False,
        live_execution_eligible=False,
    )


def catalogue_allows_solver(left: CanonicalMarket, right: CanonicalMarket) -> bool:
    """Solver eligibility for an already-admitted registered relationship."""

    return assess_catalogue_admission(left, right).allowed


def catalogue_allows_live_execution(left: CanonicalMarket, right: CanonicalMarket) -> bool:
    """Real eligibility follows admission. Historical paper wording is not a block.

    Unregistered venues, structural mismatches, and other fail-closed catalogue
    states stay ineligible. No venue order is placed here.
    """

    return assess_catalogue_admission(left, right).live_execution_eligible
