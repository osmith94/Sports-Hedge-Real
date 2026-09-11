from __future__ import annotations

from functools import lru_cache

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field

from sports_hedge.arbitrage.dislocations.models import (
    BurstPriorityDecision,
    DislocationState,
    RateBudget,
    ScanCandidate,
    ScanSchedule,
)
from sports_hedge.arbitrage.dislocations.service import EventDrivenDislocationService
from sports_hedge.config import get_settings

router = APIRouter(prefix="/arbitrage/dislocations", tags=["arbitrage-dislocations"])


class DislocationScheduleRequest(BaseModel):
    candidates: list[ScanCandidate]
    budget: RateBudget = Field(default_factory=lambda: RateBudget(max_scans=10))


@lru_cache
def get_dislocation_service() -> EventDrivenDislocationService:
    return EventDrivenDislocationService(settings=get_settings())


@router.post("/evaluate", response_model=BurstPriorityDecision)
def evaluate_dislocation(
    candidate: ScanCandidate,
    service: EventDrivenDislocationService = Depends(get_dislocation_service),
) -> BurstPriorityDecision:
    """Read-only burst-priority decision. Does not collect, ingest or execute."""

    return service.evaluate(candidate)


@router.post("/schedule", response_model=ScanSchedule)
def schedule_dislocation_scans(
    request: DislocationScheduleRequest,
    service: EventDrivenDislocationService = Depends(get_dislocation_service),
) -> ScanSchedule:
    """Rank many live games under an authorised-API rate budget."""

    return service.schedule(request.candidates, request.budget)


@router.post("/observe", response_model=DislocationState)
def observe_dislocation(
    candidate: ScanCandidate,
    service: EventDrivenDislocationService = Depends(get_dislocation_service),
) -> DislocationState:
    """Update inspectable dislocation/reaction state. Paper read-model only."""

    return service.observe(candidate)
