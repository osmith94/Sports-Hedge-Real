"""Event-driven price dislocation burst scanner (Agent AE, paper-only).

This package is isolated from the collector and venue adapters. Event labels
come from Market Intelligence :class:`AnnotationCategory` / annotations; callers
supply quotes and optional near-arb distance through typed seams. This module
does not decide executable arbitrage.
"""

from sports_hedge.arbitrage.dislocations.engine import evaluate_candidate
from sports_hedge.arbitrage.dislocations.models import (
    BurstPriorityDecision,
    CanonicalEventContext,
    DislocationState,
    EventAnnotationInput,
    NearArbSignal,
    RateBudget,
    ScanCandidate,
    ScanPriority,
    ScanSchedule,
    ScanTrigger,
    VenueQuoteSnapshot,
    event_annotation_from_market_intelligence,
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
    "EventDrivenDislocationService",
    "NearArbSignal",
    "RateBudget",
    "ScanCandidate",
    "ScanPriority",
    "ScanSchedule",
    "ScanTrigger",
    "VenueQuoteSnapshot",
    "evaluate_candidate",
    "event_annotation_from_market_intelligence",
    "event_category_from_label",
    "schedule_scans",
]
