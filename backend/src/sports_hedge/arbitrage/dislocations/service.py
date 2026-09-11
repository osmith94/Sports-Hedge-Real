from __future__ import annotations

from datetime import datetime

from sports_hedge.arbitrage.dislocations.engine import evaluate_candidate
from sports_hedge.arbitrage.dislocations.models import (
    BurstPriorityDecision,
    DislocationState,
    EventAnnotationInput,
    NearArbSignal,
    RateBudget,
    ScanCandidate,
    ScanSchedule,
    VenueQuoteSnapshot,
)
from sports_hedge.arbitrage.dislocations.scheduler import schedule_scans
from sports_hedge.arbitrage.dislocations.tracker import DislocationTracker
from sports_hedge.config import Settings, get_settings

FORBIDDEN_OPERATIONS = (
    "place_order",
    "cancel_order",
    "place_bet",
    "sign_wallet",
    "submit_order",
    "wallet",
)


class EventDrivenDislocationService:
    """Paper-only facade for burst priority, scheduling and dislocation state."""

    def __init__(
        self,
        *,
        settings: Settings | None = None,
        tracker: DislocationTracker | None = None,
    ) -> None:
        self._settings = settings or get_settings()
        if self._settings.sports_hedge_mode != "paper":
            raise ValueError("Event-driven dislocation scanning is paper-only")
        if self._settings.sports_hedge_execution_enabled:
            raise ValueError("Live execution is unavailable for dislocation scanning")
        self.tracker = tracker or DislocationTracker()

    def evaluate(
        self,
        candidate: ScanCandidate,
        *,
        budget: RateBudget | None = None,
    ) -> BurstPriorityDecision:
        self._assert_paper_only()
        return evaluate_candidate(candidate, budget=budget)

    def schedule(
        self,
        candidates: list[ScanCandidate],
        budget: RateBudget,
    ) -> ScanSchedule:
        self._assert_paper_only()
        return schedule_scans(candidates, budget)

    def observe(
        self,
        candidate: ScanCandidate,
        *,
        as_of: datetime | None = None,
    ) -> DislocationState:
        self._assert_paper_only()
        return self.tracker.observe(
            canonical_event_id=candidate.event.canonical_event_id,
            canonical_market_id=candidate.event.canonical_market_id,
            as_of=as_of or candidate.event.as_of,
            quotes=candidate.quotes,
            annotation=candidate.annotation,
            near_arb=candidate.near_arb,
        )

    def observe_quotes(
        self,
        *,
        canonical_event_id: str,
        canonical_market_id: str,
        as_of: datetime,
        quotes: list[VenueQuoteSnapshot],
        annotation: EventAnnotationInput | None = None,
        near_arb: NearArbSignal | None = None,
    ) -> DislocationState:
        self._assert_paper_only()
        return self.tracker.observe(
            canonical_event_id=canonical_event_id,
            canonical_market_id=canonical_market_id,
            as_of=as_of,
            quotes=quotes,
            annotation=annotation,
            near_arb=near_arb,
        )

    def _assert_paper_only(self) -> None:
        if self._settings.sports_hedge_mode != "paper":
            raise ValueError("Event-driven dislocation scanning is paper-only")
        if self._settings.sports_hedge_execution_enabled:
            raise ValueError("Live execution is unavailable for dislocation scanning")
        for name in FORBIDDEN_OPERATIONS:
            if hasattr(self, name):
                raise RuntimeError(f"Forbidden execution capability present: {name}")
