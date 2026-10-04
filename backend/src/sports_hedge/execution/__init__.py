"""Live execution seam. Paper autofill stays on simulate_fill unless REAL is armed."""

from sports_hedge.execution.clients import (
    DeterministicExecutionTransport,
    KalshiExecutionClient,
    MatchbookExecutionClient,
    PolymarketExecutionClient,
)
from sports_hedge.execution.models import (
    LiveExecutionPackage,
    LivePackageOutcome,
    VenueOrderRequest,
    VenueOrderResult,
    VenueOrderStatus,
)
from sports_hedge.execution.package import execute_live_package, execution_armed

__all__ = [
    "DeterministicExecutionTransport",
    "KalshiExecutionClient",
    "LiveExecutionPackage",
    "LivePackageOutcome",
    "MatchbookExecutionClient",
    "PolymarketExecutionClient",
    "VenueOrderRequest",
    "VenueOrderResult",
    "VenueOrderStatus",
    "execute_live_package",
    "execution_armed",
]
