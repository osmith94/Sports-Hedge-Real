from __future__ import annotations

from fastapi import APIRouter, HTTPException, status

from sports_hedge.application.collector import FixtureDetailReadModel
from sports_hedge.application.live_refresh import get_live_refresh_coordinator

router = APIRouter(prefix="/operations", tags=["operations"])


@router.get("/fixtures/{canonical_event_id}", response_model=FixtureDetailReadModel)
def fixture_detail(canonical_event_id: str) -> FixtureDetailReadModel:
    """Read-only fixture drill-down from the last Matchbook-led collection."""

    detail = get_live_refresh_coordinator().fixture_detail(canonical_event_id)
    if detail is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No collected fixture with that canonical event id. Run a paper collection first.",
        )
    return detail
