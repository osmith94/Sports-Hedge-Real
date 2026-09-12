from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sports_hedge.arbitrage.dislocations.engine import (
    DISLOCATION_DISPERSION,
    assess_quote_eligibility,
)
from sports_hedge.arbitrage.dislocations.models import (
    DislocationState,
    EventAnnotationInput,
    NearArbSignal,
    VenueQuoteSnapshot,
    VenueReactionState,
    require_aware_utc,
)

MATERIAL_REPRICE = Decimal("0.01")
CONVERGENCE = Decimal("0.01")


class DislocationTracker:
    """Read-only reaction/dislocation state. Does not infer trader causality."""

    def __init__(self) -> None:
        self._states: dict[tuple[str, str], DislocationState] = {}

    def observe(
        self,
        *,
        canonical_event_id: str,
        canonical_market_id: str,
        as_of: datetime,
        quotes: list[VenueQuoteSnapshot],
        annotation: EventAnnotationInput | None = None,
        near_arb: NearArbSignal | None = None,
    ) -> DislocationState:
        as_of = require_aware_utc(as_of, "as_of")
        key = (canonical_event_id, canonical_market_id)
        state = self._states.get(key) or DislocationState(
            canonical_event_id=canonical_event_id,
            canonical_market_id=canonical_market_id,
        )
        if annotation is not None:
            state.event_occurred_at = annotation.occurred_at
            state.event_retrieved_at = annotation.retrieved_at

        executable: list[VenueQuoteSnapshot] = []
        for quote in quotes:
            eligibility = assess_quote_eligibility(quote, annotation)
            venue_key = quote.venue.value
            venue_state = state.venues.get(venue_key) or VenueReactionState(venue=quote.venue)
            if quote.suspended:
                if not venue_state.suspended:
                    venue_state.suspended_at = as_of
                venue_state.suspended = True
            elif venue_state.suspended:
                venue_state.suspended = False
                venue_state.reopened_at = as_of
            if eligibility.executable:
                executable.append(quote)
                if (
                    annotation is not None
                    and quote.quote_timestamp >= annotation.occurred_at
                    and venue_state.first_fresh_post_event_quote_at is None
                ):
                    venue_state.first_fresh_post_event_quote_at = quote.quote_timestamp
                if (
                    annotation is not None
                    and venue_state.last_implied_probability is not None
                    and quote.implied_probability is not None
                    and abs(quote.implied_probability - venue_state.last_implied_probability)
                    >= MATERIAL_REPRICE
                    and venue_state.first_material_repricing_at is None
                    and quote.quote_timestamp >= annotation.occurred_at
                ):
                    venue_state.first_material_repricing_at = quote.quote_timestamp
            if quote.implied_probability is not None:
                venue_state.last_implied_probability = quote.implied_probability
            venue_state.last_quote_timestamp = quote.quote_timestamp
            state.venues[venue_key] = venue_state

        dispersion = _dispersion(executable)
        state.current_dispersion = dispersion
        if dispersion is not None and (
            state.dispersion_peak is None or dispersion > state.dispersion_peak
        ):
            state.dispersion_peak = dispersion
            state.dispersion_peak_at = as_of
        if dispersion is not None and dispersion >= DISLOCATION_DISPERSION:
            if state.dislocation_started_at is None:
                state.dislocation_started_at = as_of
            state.dislocation_ended_at = None
        elif (
            state.dislocation_started_at is not None
            and state.dislocation_ended_at is None
            and dispersion is not None
            and dispersion <= CONVERGENCE
        ):
            state.dislocation_ended_at = as_of
        if state.dislocation_started_at is not None:
            end = state.dislocation_ended_at or as_of
            state.dislocation_duration_seconds = (
                end - state.dislocation_started_at
            ).total_seconds()
        if near_arb is not None:
            state.near_arb_duration_seconds = near_arb.near_arb_duration_seconds
            state.validated_arb_duration_seconds = near_arb.validated_arb_duration_seconds
        state.causality_claim = None
        self._states[key] = state
        return state.model_copy(deep=True)

    def get(self, canonical_event_id: str, canonical_market_id: str) -> DislocationState | None:
        state = self._states.get((canonical_event_id, canonical_market_id))
        return None if state is None else state.model_copy(deep=True)


def _dispersion(quotes: list[VenueQuoteSnapshot]) -> Decimal | None:
    by_outcome: dict[str, list[Decimal]] = {}
    for quote in quotes:
        if quote.implied_probability is None:
            continue
        by_outcome.setdefault(quote.canonical_outcome, []).append(quote.implied_probability)
    peak: Decimal | None = None
    for probabilities in by_outcome.values():
        if len(probabilities) < 2:
            continue
        spread = max(probabilities) - min(probabilities)
        if peak is None or spread > peak:
            peak = spread
    return peak
