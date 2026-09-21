"""Read-only PAPER capital-optimiser HTTP surface.

POST never locks treasury, never opens trades, and never places venue orders.
The optimiser is not on the scanning critical path.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from sports_hedge.api.paper import get_paper_ledger
from sports_hedge.arbitrage.allocation.models import (
    AllocationBalance,
    BankrollAllocationPolicy,
    OpenPositionExposure,
)
from sports_hedge.arbitrage.allocation.policy import policy_from_settings
from sports_hedge.arbitrage.capital_optimiser.models import (
    CapitalOptimiserOpportunity,
    CapitalOptimiserRequest,
    CapitalOptimiserResult,
)
from sports_hedge.arbitrage.capital_optimiser.service import (
    PaperCapitalOptimiser,
    request_from_treasury,
)
from sports_hedge.config import get_settings
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger

router = APIRouter(prefix="/paper/capital-optimiser", tags=["paper"])


class CapitalOptimiserApiRequest(BaseModel):
    opportunities: list[CapitalOptimiserOpportunity]
    use_active_treasury: bool = True
    balances: list[AllocationBalance] | None = None
    open_positions: list[OpenPositionExposure] = Field(default_factory=list)
    min_net_arb: Decimal | None = Field(default=None, ge=0)
    approved_fx: dict[str, Decimal] | None = None
    fx_source: str | None = None
    fx_as_of: datetime | None = None
    policy: BankrollAllocationPolicy | None = None


@router.post("/run", response_model=CapitalOptimiserResult)
def run_paper_capital_optimiser(
    body: CapitalOptimiserApiRequest,
    ledger: SqlitePaperLedger = Depends(get_paper_ledger),
) -> CapitalOptimiserResult:
    """Modelled global PAPER allocation. Does not mutate treasury or place orders."""

    settings = get_settings()
    policy = body.policy or policy_from_settings(settings)
    min_net = body.min_net_arb
    if min_net is None:
        min_net = Decimal(str(settings.min_net_edge))
    if body.use_active_treasury and body.balances is None:
        snapshot = ledger.treasury.snapshot()
        request = request_from_treasury(
            snapshot,
            opportunities=body.opportunities,
            policy=policy,
            open_positions=body.open_positions,
            min_net_arb=min_net,
        )
    else:
        request = CapitalOptimiserRequest(
            opportunities=body.opportunities,
            balances=body.balances or [],
            open_positions=body.open_positions,
            policy=policy,
            min_net_arb=min_net,
            approved_fx=body.approved_fx or {"GBP": Decimal("1")},
            fx_source=body.fx_source or "",
            fx_as_of=body.fx_as_of,
        )
    return PaperCapitalOptimiser().optimise(request)


@router.get("/health")
def capital_optimiser_health() -> dict[str, object]:
    return {
        "module": "paper_capital_optimiser",
        "paper_only": True,
        "places_orders": False,
        "mutates_treasury": False,
        "on_scan_critical_path": False,
        "execution_enabled": False,
        "mode": "paper",
        "data_kind": "modelled",
        "formulation": "linear_program",
        "mixed_integer": False,
    }
