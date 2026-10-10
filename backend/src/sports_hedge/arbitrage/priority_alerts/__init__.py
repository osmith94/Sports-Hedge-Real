from sports_hedge.arbitrage.priority_alerts.models import (
    CapitalSource,
    ExternalCounterpartyPlan,
    FillConfidence,
    ManualOverrideRecommendation,
    OperatorAction,
    PriorityAlert,
    PriorityAlertEventType,
    PriorityAlertState,
    PrioritySeverity,
)
from sports_hedge.arbitrage.priority_alerts.thresholds import PriorityAlertThresholds

# Do not import service here. Package init used to pull
# service → qualification → sizing → allocation.adapters while adapters was
# still importing these models, which broke pytest collection.

__all__ = [
    "CapitalSource",
    "ExternalCounterpartyPlan",
    "FillConfidence",
    "ManualOverrideRecommendation",
    "OperatorAction",
    "PriorityAlert",
    "PriorityAlertEventType",
    "PriorityAlertService",
    "PriorityAlertState",
    "PriorityAlertThresholds",
    "PrioritySeverity",
]


def __getattr__(name: str):
    if name == "PriorityAlertService":
        from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService

        return PriorityAlertService
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
