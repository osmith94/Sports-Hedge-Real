from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status

from sports_hedge.application.collector import FixtureDetailReadModel, FixturePaperEntry
from sports_hedge.application.live_refresh import get_live_refresh_coordinator
from sports_hedge.api.paper import get_paper_operations_service
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.paper.trades import PaperTradeState

router = APIRouter(prefix="/operations", tags=["operations"])


@router.get("/fixtures/{canonical_event_id}", response_model=FixtureDetailReadModel)
def fixture_detail(
    canonical_event_id: str,
    operations: PaperOperationsService = Depends(get_paper_operations_service),
) -> FixtureDetailReadModel:
    """Read-only fixture drill-down from canonical current-state."""

    detail = get_live_refresh_coordinator().fixture_detail(canonical_event_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No collected fixture with that canonical event id. Run a paper collection first.",
        )
    identities = get_live_refresh_coordinator().fixture_identities(
        detail.fixture.canonical_event_id
    )
    trades = [
        trade
        for trade in [*operations.list_active_trades(), *operations.list_closed_trades()]
        if trade.canonical_event_id in identities
    ]
    entries = [
        FixturePaperEntry(
            opportunity_id=trade.opportunity_id,
            trade_id=trade.trade_id,
            state=trade.state.value,
            solver_model=trade.solver_model,
            fill_kinds=[leg.fill_kind.value for leg in trade.legs],
            guaranteed_profit_gbp_at_open=(
                trade.guaranteed_profit_gbp_at_open if trade.state is PaperTradeState.OPEN else None
            ),
            paper_only=trade.paper_only,
            places_orders=trade.places_orders,
            rejection_reason=operations._entry_rejections.get(trade.opportunity_id),
        )
        for trade in trades
    ]
    preparable = [
        item
        for item in operations.list_preparable()
        if item.canonical_event_id in identities
    ]
    return detail.model_copy(update={"paper_entries": entries, "preparable_opportunities": preparable})
