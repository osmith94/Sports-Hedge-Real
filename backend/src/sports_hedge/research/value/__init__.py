"""Odds-weighted scenario value engine.

Paper/research only. Directional and probabilistic: a positive value signal can
still lose. This is not an arbitrage solver and must not place bets.
"""

from sports_hedge.fees.cost import MarketAction, VenueCostSnapshot
from sports_hedge.research.value.contracts import (
    CanonicalProposition,
    DataQuality,
    ScenarioEvidence,
    ScenarioValueResult,
    ValueEnginePolicy,
    ValueStatus,
    VenueQuote,
)
from sports_hedge.research.value.engine import ScenarioValueEngine

PAPER_RESEARCH_ONLY = True

__all__ = [
    "PAPER_RESEARCH_ONLY",
    "CanonicalProposition",
    "DataQuality",
    "MarketAction",
    "ScenarioEvidence",
    "ScenarioValueEngine",
    "ScenarioValueResult",
    "ValueEnginePolicy",
    "ValueStatus",
    "VenueCostSnapshot",
    "VenueQuote",
]
