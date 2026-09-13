from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal

from sports_hedge.application.quote_freshness import (
    effective_quote_age_ms,
    require_aware_instant,
)
from sports_hedge.arbitrage.watchlist.adapter import observation_from_paper_decision
from sports_hedge.arbitrage.watchlist.economics import (
    classification_for,
    classify_status,
    distance_to_trigger_pp,
    insufficiency_reasons,
    missing_cost_reasons,
)
from sports_hedge.arbitrage.watchlist.models import (
    LifecycleEventType,
    NearOpportunity,
    OpportunityLifecycleEvent,
    OpportunityStatus,
    WatchObservation,
    strike_distance_narrative,
    OpportunityObservationPoint,
)
from sports_hedge.arbitrage.watchlist.ranking import (
    rank_near_opportunities,
    rank_tracked_opportunities,
    rank_triggered_opportunities,
)
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.domain.football import MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.models import MarketSnapshot
from sports_hedge.paper.models import PaperScanDecision


class WatchlistService:
    """Paper-only near-opportunity tracker. No venue order placement."""

    def __init__(
        self,
        repository: SqliteWatchlistRepository | None = None,
        *,
        approaching_band_pp: Decimal = Decimal("0.50"),
        max_quote_age_ms: int = 1000,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        if approaching_band_pp < 0:
            raise ValueError("approaching_band_pp must be non-negative")
        if max_quote_age_ms < 0:
            raise ValueError("max_quote_age_ms must be non-negative")
        self.repository = repository or SqliteWatchlistRepository()
        self.approaching_band_pp = approaching_band_pp
        self.max_quote_age_ms = max_quote_age_ms
        self._clock = clock or (lambda: datetime.now(UTC))

    def observe_paper_decision(
        self,
        decision: PaperScanDecision,
        history: Sequence[MarketSnapshot] = (),
        *,
        quote_age_ms: int | None = None,
        quote_age_basis: str | None = None,
    ) -> NearOpportunity | None:
        observation = observation_from_paper_decision(
            decision,
            history,
            quote_age_ms=quote_age_ms,
            quote_age_basis=quote_age_basis,
        )
        if observation is None:
            return None
        return self.observe(observation)

    def observe(self, observation: WatchObservation) -> NearOpportunity:
        opportunity_id = _opportunity_id(observation.canonical_market_id)
        previous = self.repository.get(opportunity_id)
        status, reasons = classify_status(
            observation,
            approaching_band_pp=self.approaching_band_pp,
            max_quote_age_ms=self.max_quote_age_ms,
            previous=previous.status if previous is not None else None,
        )
        distance = None
        if observation.current_net_edge is not None:
            distance = distance_to_trigger_pp(
                observation.current_net_edge,
                observation.trigger_net_edge,
            )
        guaranteed = None
        is_arbitrage = False
        if status == OpportunityStatus.TRIGGERED:
            is_arbitrage = True
            guaranteed = observation.guaranteed_profit_gbp

        opportunity = NearOpportunity(
            opportunity_id=opportunity_id,
            canonical_event_id=observation.canonical_event_id,
            canonical_market_id=observation.canonical_market_id,
            settlement_key=observation.settlement_key,
            competition=observation.competition,
            home_team=observation.home_team,
            away_team=observation.away_team,
            market_family=observation.market_family,
            period=observation.period,
            venues=observation.venues,
            legs=observation.legs,
            status=status,
            classification=classification_for(status),
            is_arbitrage=is_arbitrage,
            trigger_net_edge=observation.trigger_net_edge,
            current_net_edge=observation.current_net_edge,
            gross_edge=observation.gross_edge,
            distance_to_trigger_pp=distance,
            implied_probability_sum=observation.implied_probability_sum,
            quote_age_ms=observation.quote_age_ms,
            quote_age_basis=observation.quote_age_basis,
            limiting_depth_gbp=observation.limiting_depth_gbp,
            limiting_leg_outcome=observation.limiting_leg_outcome,
            capital_required_gbp=observation.capital_required_gbp,
            guaranteed_profit_gbp=guaranteed,
            execution_risk_score=observation.execution_risk_score,
            expected_lock_minutes=observation.expected_lock_minutes,
            first_seen_at=previous.first_seen_at
            if previous is not None
            else observation.observed_at,
            last_seen_at=observation.observed_at,
            rejection_reasons=reasons,
            insufficiency_reasons=insufficiency_reasons(reasons),
            fixture_discovery_source=observation.fixture_discovery_source,
            fixture_status=observation.fixture_status,
            in_running=observation.in_running,
            live_score_supported=observation.live_score_supported,
            home_score=observation.home_score if observation.live_score_supported else None,
            away_score=observation.away_score if observation.live_score_supported else None,
            data_kind=observation.data_kind,
        )
        self.repository.append_observation(
            OpportunityObservationPoint(
                opportunity_id=opportunity_id,
                observed_at=observation.observed_at,
                current_net_edge=observation.current_net_edge,
                distance_to_trigger_pp=distance,
                quote_age_ms=observation.quote_age_ms,
                status=status,
            )
        )
        history = self.repository.list_observations(opportunity_id)
        narrative, previous_edge, previous_distance = strike_distance_narrative(history)
        opportunity = opportunity.model_copy(
            update={
                "strike_narrative": narrative,
                "previous_net_edge": previous_edge,
                "previous_distance_to_trigger_pp": previous_distance,
                "observation_count": len(history),
            }
        )
        self.repository.upsert_opportunity(opportunity)
        self._append_lifecycle(previous, opportunity, observation)
        return opportunity

    def record_paper_fill(
        self,
        opportunity_id: str,
        *,
        stage: OpportunityStatus,
        occurred_at,
        detail: str | None = None,
    ) -> NearOpportunity:
        if stage not in {
            OpportunityStatus.PAPER_FILLING,
            OpportunityStatus.PARTIAL,
            OpportunityStatus.FILLED,
        }:
            raise ValueError("paper fill stage must be PAPER_FILLING, PARTIAL, or FILLED")
        current = self.repository.get(opportunity_id)
        if current is None:
            raise ValueError(f"unknown opportunity: {opportunity_id}")
        if current.status not in {
            OpportunityStatus.TRIGGERED,
            OpportunityStatus.PAPER_FILLING,
            OpportunityStatus.PARTIAL,
        }:
            raise ValueError("paper fill can only be recorded for a triggered paper opportunity")

        event_type = {
            OpportunityStatus.PAPER_FILLING: LifecycleEventType.PAPER_FILL_ATTEMPTED,
            OpportunityStatus.PARTIAL: LifecycleEventType.PAPER_FILL_PARTIAL,
            OpportunityStatus.FILLED: LifecycleEventType.PAPER_FILL_COMPLETE,
        }[stage]
        updated = current.model_copy(
            update={
                "status": stage,
                "classification": classification_for(stage),
                "last_seen_at": occurred_at,
            }
        )
        self.repository.upsert_opportunity(updated)
        self.repository.append_event(
            OpportunityLifecycleEvent(
                opportunity_id=opportunity_id,
                occurred_at=occurred_at,
                event_type=event_type,
                status=stage,
                current_net_edge=updated.current_net_edge,
                distance_to_trigger_pp=updated.distance_to_trigger_pp,
                detail=detail or "paper_mode_only",
            )
        )
        return updated

    def record_paper_fill_rejection(
        self,
        opportunity_id: str,
        *,
        occurred_at,
        detail: str,
    ) -> NearOpportunity | None:
        """Surface a failed paper-entry attempt without OPEN/PARTIAL/FILLED mutation."""

        current = self.repository.get(opportunity_id)
        if current is None:
            return None
        self.repository.append_event(
            OpportunityLifecycleEvent(
                opportunity_id=opportunity_id,
                occurred_at=occurred_at,
                event_type=LifecycleEventType.PAPER_FILL_REJECTED,
                status=current.status,
                current_net_edge=current.current_net_edge,
                distance_to_trigger_pp=current.distance_to_trigger_pp,
                detail=detail,
            )
        )
        return current

    def close(
        self, opportunity_id: str, *, occurred_at, detail: str | None = None
    ) -> NearOpportunity:
        return self._terminal(
            opportunity_id,
            status=OpportunityStatus.CLOSED,
            event_type=LifecycleEventType.CLOSED,
            occurred_at=occurred_at,
            detail=detail,
        )

    def expire(
        self, opportunity_id: str, *, occurred_at, detail: str | None = None
    ) -> NearOpportunity:
        return self._terminal(
            opportunity_id,
            status=OpportunityStatus.EXPIRED,
            event_type=LifecycleEventType.EXPIRED,
            occurred_at=occurred_at,
            detail=detail,
        )

    def top_near(
        self,
        *,
        limit: int = 25,
        competition: str | None = None,
        venue: VenueName | None = None,
        market_family: MarketFamily | None = None,
        as_of: datetime | None = None,
    ) -> list[NearOpportunity]:
        return rank_near_opportunities(
            self._freshness_filtered(
                competition=competition,
                venue=venue,
                market_family=market_family,
                as_of=as_of,
            ),
            limit=limit,
        )

    def triggered(
        self,
        *,
        limit: int = 25,
        competition: str | None = None,
        venue: VenueName | None = None,
        market_family: MarketFamily | None = None,
        as_of: datetime | None = None,
    ) -> list[NearOpportunity]:
        return rank_triggered_opportunities(
            self._freshness_filtered(
                competition=competition,
                venue=venue,
                market_family=market_family,
                as_of=as_of,
            ),
            limit=limit,
        )

    def tracked(
        self,
        *,
        limit: int = 100,
        competition: str | None = None,
        venue: VenueName | None = None,
        market_family: MarketFamily | None = None,
        as_of: datetime | None = None,
    ) -> list[NearOpportunity]:
        """Canonical markets currently on the watchlist, including below-break-even net edges."""

        return rank_tracked_opportunities(
            self._freshness_filtered(
                competition=competition,
                venue=venue,
                market_family=market_family,
                as_of=as_of,
            ),
            limit=limit,
        )

    def activity(
        self,
        *,
        limit: int = 100,
        opportunity_id: str | None = None,
        since=None,
    ) -> list[OpportunityLifecycleEvent]:
        events = self.repository.list_events(
            limit=limit if opportunity_id else limit * 2,
            opportunity_id=opportunity_id,
            since=since,
        )
        if opportunity_id is not None:
            return events
        demo_ids = {
            item.opportunity_id
            for item in self.repository.list_opportunities()
            if item.data_kind == "demo_fixture_replay"
        }
        return [event for event in events if event.opportunity_id not in demo_ids][:limit]

    def _freshness_filtered(
        self,
        *,
        competition: str | None,
        venue: VenueName | None,
        market_family: MarketFamily | None,
        as_of: datetime | None,
    ) -> list[NearOpportunity]:
        evaluated = require_aware_instant(as_of or self._clock(), "as_of")
        return [
            self._present_freshness(item, evaluated)
            for item in self._filtered(
                competition=competition,
                venue=venue,
                market_family=market_family,
            )
        ]

    def _present_freshness(self, item: NearOpportunity, as_of: datetime) -> NearOpportunity:
        fill_or_terminal = {
            OpportunityStatus.PAPER_FILLING,
            OpportunityStatus.PARTIAL,
            OpportunityStatus.FILLED,
            OpportunityStatus.CLOSED,
            OpportunityStatus.EXPIRED,
        }
        effective = effective_quote_age_ms(item.quote_age_ms, item.last_seen_at, as_of)
        if item.status in fill_or_terminal:
            return item.model_copy(update={"quote_age_ms": effective})

        stale = effective is None or effective >= self.max_quote_age_ms
        if not stale:
            return item.model_copy(update={"quote_age_ms": effective})

        reason = "unknown_quote_age" if effective is None else "stale_quote"
        reasons = list(dict.fromkeys([*item.rejection_reasons, reason]))
        persisted = item.model_copy(
            update={
                "status": OpportunityStatus.REJECTED,
                "classification": classification_for(OpportunityStatus.REJECTED),
                "is_arbitrage": False,
                "guaranteed_profit_gbp": None,
                "rejection_reasons": reasons,
            }
        )
        if item.status in {
            OpportunityStatus.WATCHING,
            OpportunityStatus.APPROACHING,
            OpportunityStatus.TRIGGERED,
        }:
            self.repository.upsert_opportunity(persisted)
            events = [
                self._event(
                    persisted, LifecycleEventType.REJECTED_STALE_QUOTE, detail=reason
                ).model_copy(update={"occurred_at": as_of})
            ]
            if item.status == OpportunityStatus.TRIGGERED:
                events.append(
                    self._event(
                        persisted,
                        LifecycleEventType.TRIGGER_LOST_BEFORE_FILL,
                        detail="quotes_aged_out_before_paper_fill",
                    ).model_copy(update={"occurred_at": as_of})
                )
            for event in events:
                self.repository.append_event(event)
        return persisted.model_copy(update={"quote_age_ms": effective})

    def _filtered(
        self,
        *,
        competition: str | None,
        venue: VenueName | None,
        market_family: MarketFamily | None,
    ) -> list[NearOpportunity]:
        items = [
            item
            for item in self.repository.list_opportunities()
            if item.data_kind != "demo_fixture_replay"
        ]
        if competition is not None:
            needle = competition.casefold()
            items = [
                item
                for item in items
                if item.competition is not None and item.competition.casefold() == needle
            ]
        if venue is not None:
            items = [item for item in items if venue in item.venues]
        if market_family is not None:
            items = [item for item in items if item.market_family == market_family]
        return items

    def _terminal(
        self,
        opportunity_id: str,
        *,
        status: OpportunityStatus,
        event_type: LifecycleEventType,
        occurred_at,
        detail: str | None,
    ) -> NearOpportunity:
        current = self.repository.get(opportunity_id)
        if current is None:
            raise ValueError(f"unknown opportunity: {opportunity_id}")
        updated = current.model_copy(
            update={
                "status": status,
                "classification": classification_for(status),
                "is_arbitrage": False,
                "guaranteed_profit_gbp": None,
                "last_seen_at": occurred_at,
            }
        )
        self.repository.upsert_opportunity(updated)
        self.repository.append_event(
            OpportunityLifecycleEvent(
                opportunity_id=opportunity_id,
                occurred_at=occurred_at,
                event_type=event_type,
                status=status,
                current_net_edge=updated.current_net_edge,
                distance_to_trigger_pp=updated.distance_to_trigger_pp,
                detail=detail,
            )
        )
        return updated

    def _append_lifecycle(
        self,
        previous: NearOpportunity | None,
        current: NearOpportunity,
        observation: WatchObservation,
    ) -> None:
        events: list[OpportunityLifecycleEvent] = []
        if previous is None:
            events.append(
                self._event(
                    current,
                    LifecycleEventType.CANDIDATE_FIRST_SEEN,
                    detail="watch_candidate_first_seen",
                )
            )
            if current.status == OpportunityStatus.TRIGGERED:
                events.append(
                    self._event(
                        current,
                        LifecycleEventType.TRIGGER_CROSSED,
                        detail="eligible_at_or_above_configured_trigger",
                    )
                )
        else:
            if (
                previous.distance_to_trigger_pp is not None
                and current.distance_to_trigger_pp is not None
                and current.distance_to_trigger_pp < previous.distance_to_trigger_pp
                and current.status
                in {
                    OpportunityStatus.WATCHING,
                    OpportunityStatus.APPROACHING,
                    OpportunityStatus.TRIGGERED,
                }
            ):
                events.append(
                    self._event(
                        current,
                        LifecycleEventType.MOVED_CLOSER_TO_TRIGGER,
                        detail="distance_to_trigger_decreased",
                    )
                )
            if (
                previous.distance_to_trigger_pp is not None
                and current.distance_to_trigger_pp is not None
                and current.distance_to_trigger_pp > previous.distance_to_trigger_pp
                and current.status in {OpportunityStatus.WATCHING, OpportunityStatus.APPROACHING}
            ):
                events.append(
                    self._event(
                        current,
                        LifecycleEventType.MOVED_FURTHER_FROM_TRIGGER,
                        detail="distance_to_trigger_increased",
                    )
                )
            if (
                previous.status != OpportunityStatus.TRIGGERED
                and current.status == OpportunityStatus.TRIGGERED
            ):
                events.append(
                    self._event(
                        current,
                        LifecycleEventType.TRIGGER_CROSSED,
                        detail="eligible_at_or_above_configured_trigger",
                    )
                )
            if previous.status == OpportunityStatus.TRIGGERED and current.status in {
                OpportunityStatus.WATCHING,
                OpportunityStatus.APPROACHING,
                OpportunityStatus.REJECTED,
            }:
                events.append(
                    self._event(
                        current,
                        LifecycleEventType.TRIGGER_LOST_BEFORE_FILL,
                        detail="trigger_lost_before_paper_fill",
                    )
                )

        events.extend(self._rejection_events(previous, current, observation))
        for event in events:
            self.repository.append_event(event)

    def _rejection_events(
        self,
        previous: NearOpportunity | None,
        current: NearOpportunity,
        observation: WatchObservation,
    ) -> list[OpportunityLifecycleEvent]:
        if current.status != OpportunityStatus.REJECTED:
            return []
        previous_reasons = set(previous.rejection_reasons) if previous is not None else set()
        emitted: list[OpportunityLifecycleEvent] = []
        if "stale_quote" in current.rejection_reasons and "stale_quote" not in previous_reasons:
            emitted.append(
                self._event(current, LifecycleEventType.REJECTED_STALE_QUOTE, detail="stale_quote")
            )
        if (
            "unknown_quote_age" in current.rejection_reasons
            and "unknown_quote_age" not in previous_reasons
        ):
            emitted.append(
                self._event(
                    current,
                    LifecycleEventType.REJECTED_STALE_QUOTE,
                    detail="unknown_quote_age",
                )
            )
        if missing_cost_reasons(current.rejection_reasons) and not missing_cost_reasons(
            list(previous_reasons)
        ):
            emitted.append(
                self._event(
                    current,
                    LifecycleEventType.REJECTED_MISSING_COSTS,
                    detail="missing_fee_or_fx_fail_closed",
                )
            )
        if any(
            reason
            in {
                "missing_executable_outcome_depth",
                "incomplete_outcome_set",
                "missing_net_edge",
            }
            for reason in current.rejection_reasons
        ) and not any(
            reason
            in {
                "missing_executable_outcome_depth",
                "incomplete_outcome_set",
                "missing_net_edge",
            }
            for reason in previous_reasons
        ):
            emitted.append(
                self._event(
                    current,
                    LifecycleEventType.REJECTED_INSUFFICIENT_DEPTH,
                    detail="insufficient_executable_depth",
                )
            )
        semantic = {
            "market_not_equivalent",
            "event_mismatch",
            "settlement_mismatch",
            "unknown_settlement_scope",
            "incomplete_settlement",
            "noncanonical_outcome_space",
            "mapping_confidence_below_threshold",
            "same_venue_pair",
        }
        if any(reason in semantic for reason in current.rejection_reasons) and not any(
            reason in semantic for reason in previous_reasons
        ):
            emitted.append(
                self._event(
                    current,
                    LifecycleEventType.REJECTED_SEMANTICS,
                    detail="settlement_or_mapping_rejected",
                )
            )
        if (
            "execution_risk_above_threshold" in current.rejection_reasons
            and "execution_risk_above_threshold" not in previous_reasons
        ):
            emitted.append(
                self._event(
                    current,
                    LifecycleEventType.REJECTED_EXECUTION_RISK,
                    detail="execution_risk_above_threshold",
                )
            )
        if not emitted and (previous is None or previous.status != OpportunityStatus.REJECTED):
            emitted.append(
                self._event(
                    current,
                    LifecycleEventType.REJECTED_SEMANTICS,
                    detail=",".join(current.rejection_reasons) or "rejected",
                )
            )
        return emitted

    def _event(
        self,
        opportunity: NearOpportunity,
        event_type: LifecycleEventType,
        *,
        detail: str | None,
    ) -> OpportunityLifecycleEvent:
        return OpportunityLifecycleEvent(
            opportunity_id=opportunity.opportunity_id,
            occurred_at=opportunity.last_seen_at,
            event_type=event_type,
            status=opportunity.status,
            current_net_edge=opportunity.current_net_edge,
            distance_to_trigger_pp=opportunity.distance_to_trigger_pp,
            detail=detail,
        )


def _opportunity_id(canonical_market_id: str) -> str:
    return f"watch:{canonical_market_id}"
