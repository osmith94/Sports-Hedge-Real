"""Paper-only unwind economics. No live execution and no treasury postings."""

from sports_hedge.paper.unwind.adapter import UnwindIdentityError, position_from_trade
from sports_hedge.paper.unwind.engine import PaperUnwindEngine
from sports_hedge.paper.unwind.mechanics import mechanics_for_venue, register_venue_close_mechanics
from sports_hedge.paper.unwind.models import (
    CapitalPressure,
    CapitalScarcityInput,
    ClosePlan,
    OpenPaperPosition,
    ReverseQuote,
    UnwindDecision,
    UnwindEvaluationRequest,
    UnwindPolicy,
    UnwindRecommendation,
    VenueCloseMechanics,
)

__all__ = [
    "CapitalPressure",
    "CapitalScarcityInput",
    "ClosePlan",
    "OpenPaperPosition",
    "PaperUnwindEngine",
    "ReverseQuote",
    "UnwindDecision",
    "UnwindEvaluationRequest",
    "UnwindIdentityError",
    "UnwindPolicy",
    "UnwindRecommendation",
    "VenueCloseMechanics",
    "mechanics_for_venue",
    "position_from_trade",
    "register_venue_close_mechanics",
]
