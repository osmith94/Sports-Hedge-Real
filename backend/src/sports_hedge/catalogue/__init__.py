"""Approved-market catalogue census (Core Tenet 20).

Classifier, pairwise matrix, and the Issue #316/#326 versioned registry live here.
HOT and UNIVERSE share this catalogue. Paper-mode admission allows
APPROVED_EQUIVALENT and PAPER_ASSUMED_EQUIVALENT for the four locked
Matchbook↔Kalshi families. Live execution stays independently proven
APPROVED_EQUIVALENT and remains disabled in Phase 1.
"""

from sports_hedge.catalogue.admission import (
    CatalogueAdmission,
    assess_catalogue_admission,
    catalogue_allows_live_execution,
    catalogue_allows_solver,
)
from sports_hedge.catalogue.classify import (
    CataloguePairAssessment,
    classify_pair,
    classify_payload_pair,
)
from sports_hedge.catalogue.coverage_rows import (
    FixtureArchetypeCoverage,
    FixtureCatalogueCoverage,
    fixture_catalogue_coverage,
)
from sports_hedge.catalogue.registry import (
    REGISTRY_VERSION,
    CatalogueCoverageState,
    registry_cell,
    target_archetypes,
)
from sports_hedge.catalogue.states import (
    CENSUS_V1_FAMILIES,
    CatalogueApprovalState,
    CatalogueArchetype,
)

__all__ = [
    "CENSUS_V1_FAMILIES",
    "REGISTRY_VERSION",
    "CatalogueAdmission",
    "CatalogueApprovalState",
    "CatalogueArchetype",
    "CatalogueCoverageState",
    "CataloguePairAssessment",
    "FixtureArchetypeCoverage",
    "FixtureCatalogueCoverage",
    "assess_catalogue_admission",
    "catalogue_allows_live_execution",
    "catalogue_allows_solver",
    "classify_pair",
    "classify_payload_pair",
    "fixture_catalogue_coverage",
    "registry_cell",
    "target_archetypes",
]
