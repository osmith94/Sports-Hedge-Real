"""Approved-market catalogue census (Core Tenet 20).

Classifier and pairwise matrix live here. Production solver/paper admission
requires APPROVED_EQUIVALENT through the shared HOT/UNIVERSE gate. Matcher
recognition semantics, HOT/UNIVERSE concurrency, paper autofill, and execution
are unchanged.
"""

from sports_hedge.catalogue.admission import (
    CatalogueAdmission,
    assess_catalogue_admission,
    catalogue_allows_solver,
)
from sports_hedge.catalogue.classify import (
    CataloguePairAssessment,
    classify_pair,
    classify_payload_pair,
)
from sports_hedge.catalogue.states import (
    CENSUS_V1_FAMILIES,
    CatalogueApprovalState,
    CatalogueArchetype,
)

__all__ = [
    "CENSUS_V1_FAMILIES",
    "CatalogueAdmission",
    "CatalogueApprovalState",
    "CatalogueArchetype",
    "CataloguePairAssessment",
    "assess_catalogue_admission",
    "catalogue_allows_solver",
    "classify_pair",
    "classify_payload_pair",
]
