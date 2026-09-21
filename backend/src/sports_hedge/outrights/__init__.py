"""Competition/season outrights — Phase 1A observation identity + catalogue.

PAPER / read-only. No venue writes. No Approved Match Register admission.
"""

from sports_hedge.domain.market_scope import MarketScope
from sports_hedge.domain.outrights import (
    CanonicalCompetitionSeasonRef,
    CanonicalSeasonMarketIdentity,
    OutrightMarketFamily,
    OutrightNativeListing,
    OutrightSettlementFingerprint,
    ParticipantType,
    SeasonEquivalenceState,
)
from sports_hedge.outrights.catalogue import (
    OBSERVATION_KEY_PREFIX,
    SeasonCatalogueError,
    persist_season_observation,
    season_observation_key,
)
from sports_hedge.outrights.equivalence import compare_season_identities
from sports_hedge.outrights.identity import (
    canonical_season_market_id,
    canonical_season_subject_id,
)

__all__ = [
    "OBSERVATION_KEY_PREFIX",
    "CanonicalCompetitionSeasonRef",
    "CanonicalSeasonMarketIdentity",
    "MarketScope",
    "OutrightMarketFamily",
    "OutrightNativeListing",
    "OutrightSettlementFingerprint",
    "ParticipantType",
    "SeasonCatalogueError",
    "SeasonEquivalenceState",
    "canonical_season_market_id",
    "canonical_season_subject_id",
    "compare_season_identities",
    "persist_season_observation",
    "season_observation_key",
]
