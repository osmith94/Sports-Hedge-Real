"""Provider-neutral close mechanics. Kalshi can register the same buy/sell model."""

from __future__ import annotations

from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import MarketAction
from sports_hedge.paper.unwind.models import VenueCloseMechanics

_REGISTRY: dict[VenueName, VenueCloseMechanics] = {
    VenueName.MATCHBOOK: VenueCloseMechanics.EXCHANGE_BACK_LAY,
    VenueName.SMARKETS: VenueCloseMechanics.EXCHANGE_BACK_LAY,
    VenueName.POLYMARKET: VenueCloseMechanics.PREDICTION_BINARY_BUY_SELL,
    VenueName.KALSHI: VenueCloseMechanics.PREDICTION_BINARY_BUY_SELL,
}


def register_venue_close_mechanics(venue: VenueName, mechanics: VenueCloseMechanics) -> None:
    """Slot a later venue (for example Kalshi) into the same close engine."""

    _REGISTRY[venue] = mechanics


def mechanics_for_venue(venue: VenueName) -> VenueCloseMechanics:
    return _REGISTRY.get(venue, VenueCloseMechanics.UNSUPPORTED)


def close_action_for(opening: MarketAction, mechanics: VenueCloseMechanics) -> MarketAction:
    if mechanics is VenueCloseMechanics.EXCHANGE_BACK_LAY:
        if opening is not MarketAction.BACK:
            raise ValueError("exchange_close_requires_opening_back")
        return MarketAction.LAY
    if mechanics is VenueCloseMechanics.PREDICTION_BINARY_BUY_SELL:
        if opening is not MarketAction.BUY:
            raise ValueError("prediction_close_requires_opening_buy")
        return MarketAction.SELL
    raise ValueError("unsupported_close_mechanics")
