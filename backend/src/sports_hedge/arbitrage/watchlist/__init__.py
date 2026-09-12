"""Paper-only near-arbitrage watchlist and opportunity lifecycle read model.

Items below the configured net-arb threshold are watch candidates, not arbitrage.
This package does not place, cancel, or otherwise touch venue orders.
"""

from sports_hedge.arbitrage.watchlist.models import (
    LifecycleEventType,
    NearOpportunity,
    OpportunityLifecycleEvent,
    OpportunityStatus,
    WatchObservation,
)
from sports_hedge.arbitrage.watchlist.ranking import rank_near_opportunities
from sports_hedge.arbitrage.watchlist.service import WatchlistService

__all__ = [
    "LifecycleEventType",
    "NearOpportunity",
    "OpportunityLifecycleEvent",
    "OpportunityStatus",
    "WatchObservation",
    "WatchlistService",
    "rank_near_opportunities",
]
