"""Paper execution and fill simulation.

Phase 1 contains no live order placement implementation.
"""

from sports_hedge.paper.fills import (
    FillMode,
    PaperFillConfig,
    PaperFillRecord,
    PaperNativeStakeTotals,
    PaperOpportunityFills,
    PaperOpportunityLeg,
)
from sports_hedge.paper.simulator import PaperFillSimulator

__all__ = [
    "FillMode",
    "PaperFillConfig",
    "PaperFillRecord",
    "PaperFillSimulator",
    "PaperNativeStakeTotals",
    "PaperOpportunityFills",
    "PaperOpportunityLeg",
]
