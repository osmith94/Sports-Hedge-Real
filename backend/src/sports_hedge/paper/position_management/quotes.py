"""Exact-identity reverse quotes. No fuzzy remapping at close time."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Iterable, Sequence

from sports_hedge.application.market_observation import VenueMarketObservation
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import MarketAction, OrderRole, VenueCostSnapshot
from sports_hedge.fees.kalshi import kalshi_closing_cost_from_series
from sports_hedge.fees.polymarket import polymarket_cost_from_market
from sports_hedge.fees.resolver import UnknownRequiredCostError, VenueCostResolver
from sports_hedge.paper.unwind.mechanics import close_action_for, mechanics_for_venue
from sports_hedge.paper.unwind.models import OpenPaperLeg, OpenPaperPosition, ReverseQuote


def reverse_quotes_for_position(
    position: OpenPaperPosition,
    observations: Sequence[VenueMarketObservation],
    *,
    cost_resolver: VenueCostResolver | None = None,
    evaluated_at: datetime | None = None,
) -> list[ReverseQuote]:
    """Build reverse quotes only from stored source IDs + settlement fingerprint."""

    when = evaluated_at or datetime.now(UTC)
    index = _index_observations(observations)
    quotes: list[ReverseQuote] = []
    for leg in position.legs:
        observation = index.get(_observation_key(leg))
        if observation is None:
            continue
        fingerprint = observation.market.settlement.deterministic_key()
        if fingerprint != position.settlement_fingerprint_key:
            continue
        if str(observation.market.event.source_event_id) != leg.source_event_id:
            continue
        if observation.market.source_market_id != leg.source_market_id:
            continue
        book = next(
            (
                item
                for item in observation.outcome_books
                if item.source_runner_id == leg.source_runner_id
            ),
            None,
        )
        if book is None or not book.lay_levels:
            continue
        try:
            close_action = close_action_for(leg.opening_action, mechanics_for_venue(leg.venue))
        except ValueError:
            continue
        closing_cost = _closing_cost_for_observation(
            observation,
            action=close_action,
            cost_resolver=cost_resolver,
            captured_at=when,
        )
        quotes.append(
            ReverseQuote(
                venue=leg.venue,
                source_event_id=leg.source_event_id,
                source_market_id=leg.source_market_id,
                source_runner_id=leg.source_runner_id,
                canonical_outcome=leg.canonical_outcome,
                settlement_fingerprint_key=fingerprint,
                native_currency=observation.native_currency,
                levels=list(book.lay_levels),
                quote_age_ms=observation.quote_age_ms,
                quote_age_basis=str(
                    (observation.metadata or {}).get("quote_age_basis") or "retrieval"
                ),
                quoted_at=observation.observed_at,
                closing_cost=closing_cost,
            )
        )
    return quotes


class LatestObservationCatalog:
    """Process-local latest books keyed by exact venue/source identity."""

    def __init__(self) -> None:
        self._items: dict[tuple[VenueName, str, str], VenueMarketObservation] = {}

    def remember(self, observations: Iterable[VenueMarketObservation]) -> None:
        for observation in observations:
            key = (
                observation.venue,
                str(observation.market.event.source_event_id),
                observation.market.source_market_id,
            )
            self._items[key] = observation

    def observations(self) -> list[VenueMarketObservation]:
        return list(self._items.values())

    def clear(self) -> None:
        self._items.clear()


def _index_observations(
    observations: Sequence[VenueMarketObservation],
) -> dict[tuple[VenueName, str, str], VenueMarketObservation]:
    index: dict[tuple[VenueName, str, str], VenueMarketObservation] = {}
    for observation in observations:
        index[
            (
                observation.venue,
                str(observation.market.event.source_event_id),
                observation.market.source_market_id,
            )
        ] = observation
    return index


def _observation_key(leg: OpenPaperLeg) -> tuple[VenueName, str, str]:
    return (leg.venue, leg.source_event_id, leg.source_market_id)


def _closing_cost_for_observation(
    observation: VenueMarketObservation,
    *,
    action: MarketAction,
    cost_resolver: VenueCostResolver | None,
    captured_at: datetime,
) -> VenueCostSnapshot:
    metadata = observation.metadata if isinstance(observation.metadata, dict) else {}
    if observation.venue is VenueName.KALSHI:
        fee_meta = metadata.get("kalshi_fee")
        if isinstance(fee_meta, dict):
            return kalshi_closing_cost_from_series(
                fee_meta,
                captured_at=captured_at,
                source_market_id=observation.market.source_market_id,
            )
        from sports_hedge.fees.cost import CostKnownStatus, FeeBasis, FeeScope

        return VenueCostSnapshot(
            venue=VenueName.KALSHI,
            action=action,
            fee_basis=FeeBasis.UNKNOWN,
            known_status=CostKnownStatus.UNKNOWN,
            captured_at=captured_at,
            source="kalshi_fee_schedule:missing",
            order_role=OrderRole.TAKER,
            fee_scope=FeeScope.PER_QUOTE,
            currency="USD",
            detail="Kalshi SELL close requires current event/series fee metadata",
        )
    if observation.venue is VenueName.POLYMARKET:
        fee_meta = metadata.get("polymarket_fee")
        payload = fee_meta if isinstance(fee_meta, dict) else None
        return polymarket_cost_from_market(
            payload,
            action=MarketAction.SELL,
            captured_at=captured_at,
            source_market_id=observation.market.source_market_id,
        )
    if cost_resolver is None:
        from sports_hedge.fees.cost import CostKnownStatus, FeeBasis, FeeScope

        return VenueCostSnapshot(
            venue=observation.venue,
            action=action,
            fee_basis=FeeBasis.UNKNOWN,
            known_status=CostKnownStatus.UNKNOWN,
            captured_at=captured_at,
            source="missing_closing_cost_resolver",
            currency=observation.native_currency,
            fee_scope=FeeScope.PER_QUOTE,
            detail="Matchbook closing cost resolver missing",
        )
    try:
        return cost_resolver.resolve(
            venue=observation.venue,
            market_class=observation.market.family,
            action=action,
            as_of=captured_at,
        )
    except UnknownRequiredCostError:
        from sports_hedge.fees.cost import CostKnownStatus, FeeBasis, FeeScope

        return VenueCostSnapshot(
            venue=observation.venue,
            action=action,
            fee_basis=FeeBasis.UNKNOWN,
            known_status=CostKnownStatus.UNKNOWN,
            captured_at=captured_at,
            source="unknown_required_venue_cost",
            currency=observation.native_currency,
            fee_scope=FeeScope.PER_QUOTE,
            detail="Required closing venue cost is unknown",
        )
