"""Autonomous paper position management. Paper-only; no venue execution."""

from sports_hedge.paper.position_management.models import (
    CloseFeeByVenue,
    CompetingOpportunityInput,
    PendingUnwindConfirmation,
    PositionManagementAutoAction,
    PositionManagementCycleResult,
    PositionManagementSnapshot,
)

__all__ = [
    "CloseFeeByVenue",
    "CompetingOpportunityInput",
    "LatestObservationCatalog",
    "PaperPositionManager",
    "PendingUnwindConfirmation",
    "PositionManagementAutoAction",
    "PositionManagementCycleResult",
    "PositionManagementSnapshot",
    "build_capital_scarcity",
    "competing_from_paper_decisions",
    "reverse_quotes_for_position",
]


def __getattr__(name: str):
    # Manager/quotes/scarcity import paper operations and must stay lazy so
    # paper.trades can load without a circular import.
    if name == "PaperPositionManager":
        from sports_hedge.paper.position_management.manager import PaperPositionManager

        return PaperPositionManager
    if name == "LatestObservationCatalog":
        from sports_hedge.paper.position_management.quotes import LatestObservationCatalog

        return LatestObservationCatalog
    if name == "reverse_quotes_for_position":
        from sports_hedge.paper.position_management.quotes import reverse_quotes_for_position

        return reverse_quotes_for_position
    if name == "build_capital_scarcity":
        from sports_hedge.paper.position_management.scarcity import build_capital_scarcity

        return build_capital_scarcity
    if name == "competing_from_paper_decisions":
        from sports_hedge.paper.position_management.scarcity import competing_from_paper_decisions

        return competing_from_paper_decisions
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
