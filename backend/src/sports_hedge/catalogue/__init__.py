"""Approved-market catalogue census (Core Tenet 20).

This package classifies venue pairs into operational equivalence states. It does
not change MarketMatcher admission, HOT/UNIVERSE concurrency, paper autofill, or
execution. HOT and UNIVERSE must share this single catalogue.
"""

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
    "CatalogueApprovalState",
    "CatalogueArchetype",
    "CataloguePairAssessment",
    "classify_pair",
    "classify_payload_pair",
]
