"""Event-driven price dislocation burst scanner (Agent AE, paper-only).

This package is intentionally isolated from the collector, Agent P event
ingestion (`market_intelligence`) and Agent Z near-arb watchlist. Callers
supply typed seams: :class:`EventAnnotationInput`, :class:`VenueQuoteSnapshot`
and :class:`NearArbSignal`.
"""

from sports_hedge.arbitrage.dislocations.engine import evaluate_candidate
from sports_hedge.arbitrage.dislocations.models import (
    BurstPriorityDecision,
    CanonicalEventContext,
    DislocationState,
    EventAnnotationInput,
    EventCategory,
    NearArbSignal,
    RateBudget,
    ScanCandidate,
    ScanPriority,
    ScanSchedule,
    VenueQuoteSnapshot,
    event_category_from_label,
)
from sports_hedge.arbitrage.dislocations.scheduler import schedule_scans
from sports_hedge.arbitrage.dislocations.service import EventDrivenDislocationService
from sports_hedge.arbitrage.dislocations.tracker import DislocationTracker

__all__ = [
    "BurstPriorityDecision",
    "CanonicalEventContext",
    "DislocationState",
    "DislocationTracker",
    "EventAnnotationInput",
    "EventCategory",
    "EventDrivenDislocationService",
    "NearArbSignal",
    "RateBudget",
    "ScanCandidate",
    "ScanPriority",
    "ScanSchedule",
    "VenueQuoteSnapshot",
    "evaluate_candidate",
    "event_category_from_label",
    "schedule_scans",
]
