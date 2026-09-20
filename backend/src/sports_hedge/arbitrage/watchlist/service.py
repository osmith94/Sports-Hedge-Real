from __future__ import annotations

from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sports_hedge.application.quote_freshness import (
    effective_quote_age_ms,
    require_aware_instant,
)
from sports_hedge.arbitrage.watchlist.adapter import observation_from_paper_decision
from sports_hedge.arbitrage.watchlist.economics import (
    LIFECYCLE_STATUSES_PROTECTED_FROM_OBSERVATION,
    NET_PROXIMITY_BAND_PP,
    classification_for,
    classify_status,
    distance_to_trigger_pp,
    insufficiency_reasons,
    missing_cost_reasons,
    quote_is_execution_fresh,
    semantic_reasons,
)
from sports_hedge.arbitrage.watchlist.models import (
    ORPHANED_PAPER_FILLING_RECONCILED,
    OPERATOR_ACTIVITY_EVENT_TYPES,
    LifecycleEventType,
    NearOpportunity,
    OpportunityLifecycleEvent,
    OpportunityStatus,
    PaperFillAttempt,
    PaperFillAttemptStatus,
    WatchObservation,
    format_hot_promotion_detail,
    hot_promotion_lifecycle_event_id,
    hot_promotion_opportunity_id,
    paper_fill_lifecycle_event_id,
    strike_distance_narrative,
    OpportunityObservationPoint,
)
from sports_hedge.arbitrage.watchlist.ranking import (
    filter_tracked_to_cohort,
    opportunity_id_for_canonical_market,
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
        approaching_band_pp: Decimal = NET_PROXIMITY_BAND_PP,
        max_quote_age_ms: int = 2000,
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
        self._active_bound_attempts: set[str] = set()

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
        with self.repository.transaction():
            return self._observe_locked(observation)

    def _observe_locked(self, observation: WatchObservation) -> NearOpportunity:
        opportunity_id = opportunity_id_for_canonical_market(observation.canonical_market_id)
        previous = self.repository.get(opportunity_id)
        status, reasons = classify_status(
            observation,
            approaching_band_pp=self.approaching_band_pp,
            max_quote_age_ms=self.max_quote_age_ms,
            previous=previous.status if previous is not None else None,
        )
        if (
            previous is not None
            and previous.status in LIFECYCLE_STATUSES_PROTECTED_FROM_OBSERVATION
        ):
            status = previous.status
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
            mapping_confidence=observation.mapping_confidence,
            mapping_matched=observation.mapping_matched,
            mapping_reasons=list(observation.mapping_reasons),
            mapping_provenance=observation.mapping_provenance,
            mapping_review_candidate=observation.mapping_review_candidate,
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
        with self.repository.transaction():
            return self._record_paper_fill_locked(
                opportunity_id, stage=stage, occurred_at=occurred_at, detail=detail
            )

    def _record_paper_fill_locked(
        self,
        opportunity_id: str,
        *,
        stage: OpportunityStatus,
        occurred_at,
        detail: str | None = None,
        bind_snapshot: bool = False,
        decision_at=None,
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
        if current.status is OpportunityStatus.FILLED and stage is OpportunityStatus.FILLED:
            existing = self.repository.get_started_paper_fill_attempt(opportunity_id)
            if existing is not None:
                self._finish_paper_fill_attempt(
                    existing,
                    status=PaperFillAttemptStatus.COMPLETE,
                    occurred_at=occurred_at,
                    detail=detail,
                )
            self._active_bound_attempts.discard(opportunity_id)
            return current
        if current.status is OpportunityStatus.REJECTED and stage is OpportunityStatus.PAPER_FILLING:
            if self.has_active_bound_attempt(opportunity_id):
                current = self._revive_presentation_stale_for_bound_entry(current)
            if current.status is OpportunityStatus.REJECTED:
                raise ValueError("paper fill can only be recorded for a triggered paper opportunity")
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
        attempt = self._ensure_paper_fill_attempt(
            opportunity_id,
            occurred_at=occurred_at,
            bound_snapshot=bind_snapshot,
            decision_at=decision_at,
            detail=detail,
        )
        if stage is OpportunityStatus.FILLED:
            self._finish_paper_fill_attempt(
                attempt,
                status=PaperFillAttemptStatus.COMPLETE,
                occurred_at=occurred_at,
                detail=detail,
            )
        updated = current.model_copy(
            update={
                "status": stage,
                "classification": classification_for(stage),
                "last_seen_at": occurred_at,
            }
        )
        self.repository.upsert_opportunity(updated)
        event_detail = detail or "paper_mode_only"
        if attempt.attempt_id not in event_detail:
            event_detail = f"attempt_id={attempt.attempt_id}; {event_detail}"
        self.repository.append_event(
            OpportunityLifecycleEvent(
                event_id=paper_fill_lifecycle_event_id(
                    opportunity_id, event_type, attempt.attempt_id
                ),
                opportunity_id=opportunity_id,
                occurred_at=occurred_at,
                event_type=event_type,
                status=stage,
                current_net_edge=updated.current_net_edge,
                distance_to_trigger_pp=updated.distance_to_trigger_pp,
                detail=event_detail,
            )
        )
        return updated

    def begin_paper_fill_attempt(
        self,
        opportunity_id: str,
        *,
        occurred_at,
        detail: str | None = None,
        bind_snapshot: bool = False,
        decision_at=None,
    ) -> NearOpportunity:
        """Mark a fill attempt before freshness/fill evaluation.

        `bind_snapshot=True` is autofill-only and requires an explicit durable
        bound attempt. Generic PAPER_FILLING is not bound-autofill authority.
        Presentation-stale REJECTED rows are not revived to start a new attempt.
        """

        with self.repository.transaction():
            current = self.repository.get(opportunity_id)
            if current is None:
                raise ValueError(f"unknown opportunity: {opportunity_id}")
            existing = self.repository.get_started_paper_fill_attempt(opportunity_id)
            if bind_snapshot:
                if existing is not None and existing.bound_snapshot:
                    allowed = current.status in {
                        OpportunityStatus.TRIGGERED,
                        OpportunityStatus.PAPER_FILLING,
                    }
                else:
                    allowed = current.status is OpportunityStatus.TRIGGERED
                if not allowed:
                    raise ValueError(
                        "bound snapshot attempt requires a current TRIGGERED snapshot"
                    )
            return self._record_paper_fill_locked(
                opportunity_id,
                stage=OpportunityStatus.PAPER_FILLING,
                occurred_at=occurred_at,
                detail=detail or "paper_fill_attempted_bound_snapshot",
                bind_snapshot=bind_snapshot,
                decision_at=decision_at,
            )

    def allows_bound_snapshot_entry(
        self, current: NearOpportunity, *, bound_autofill: bool = False
    ) -> bool:
        if current.status in {
            OpportunityStatus.TRIGGERED,
            OpportunityStatus.PAPER_FILLING,
            OpportunityStatus.PARTIAL,
        }:
            return True
        if not bound_autofill:
            return False
        return (
            current.status is OpportunityStatus.REJECTED
            and _presentation_stale_only(current.rejection_reasons)
            and self.has_active_bound_attempt(current.opportunity_id)
        )

    def has_active_bound_attempt(self, opportunity_id: str) -> bool:
        """True only for a durable STARTED attempt that is explicitly bound."""

        attempt = self.repository.get_started_paper_fill_attempt(opportunity_id)
        return attempt is not None and attempt.bound_snapshot

    def latest_paper_fill_attempt(self, opportunity_id: str) -> PaperFillAttempt | None:
        attempts = self.repository.list_paper_fill_attempts(opportunity_id)
        return attempts[0] if attempts else None

    def clear_bound_autofill_attempt(self, opportunity_id: str) -> None:
        self._active_bound_attempts.discard(opportunity_id)

    def reconcile_orphaned_paper_filling(
        self,
        opportunity_id: str,
        *,
        occurred_at,
        detail: str = ORPHANED_PAPER_FILLING_RECONCILED,
    ) -> NearOpportunity | None:
        """Fail-close PAPER_FILLING when the original attempt cannot continue."""

        with self.repository.transaction():
            current = self.repository.get(opportunity_id)
            if current is None or current.status is not OpportunityStatus.PAPER_FILLING:
                return current
            attempt = self.repository.get_started_paper_fill_attempt(opportunity_id)
            if attempt is not None:
                self._finish_paper_fill_attempt(
                    attempt,
                    status=PaperFillAttemptStatus.REJECTED,
                    occurred_at=occurred_at,
                    detail=detail,
                )
            reasons = list(dict.fromkeys([*current.rejection_reasons, detail]))
            updated = current.model_copy(
                update={
                    "status": OpportunityStatus.REJECTED,
                    "classification": classification_for(OpportunityStatus.REJECTED),
                    "is_arbitrage": False,
                    "guaranteed_profit_gbp": None,
                    "rejection_reasons": reasons,
                    "last_seen_at": occurred_at,
                }
            )
            self.repository.upsert_opportunity(updated, force_status=True)
            event_id = paper_fill_lifecycle_event_id(
                opportunity_id,
                LifecycleEventType.PAPER_FILL_REJECTED,
                None if attempt is None else attempt.attempt_id,
            )
            event_detail = detail
            if attempt is not None and attempt.attempt_id not in event_detail:
                event_detail = f"attempt_id={attempt.attempt_id}; {detail}"
            self.repository.append_event(
                OpportunityLifecycleEvent(
                    event_id=event_id,
                    opportunity_id=opportunity_id,
                    occurred_at=occurred_at,
                    event_type=LifecycleEventType.PAPER_FILL_REJECTED,
                    status=updated.status,
                    current_net_edge=updated.current_net_edge,
                    distance_to_trigger_pp=updated.distance_to_trigger_pp,
                    detail=event_detail,
                )
            )
            self._active_bound_attempts.discard(opportunity_id)
            return updated

    def _ensure_paper_fill_attempt(
        self,
        opportunity_id: str,
        *,
        occurred_at,
        bound_snapshot: bool,
        decision_at=None,
        detail: str | None,
    ) -> PaperFillAttempt:
        existing = self.repository.get_started_paper_fill_attempt(opportunity_id)
        if existing is not None:
            if bound_snapshot and not existing.bound_snapshot:
                raise ValueError("bound snapshot attempt requires a current TRIGGERED snapshot")
            if bound_snapshot:
                self._active_bound_attempts.add(opportunity_id)
            return existing
        attempt = PaperFillAttempt(
            attempt_id=str(uuid4()),
            opportunity_id=opportunity_id,
            bound_snapshot=bound_snapshot,
            status=PaperFillAttemptStatus.STARTED,
            started_at=occurred_at,
            decision_at=decision_at,
            detail=detail,
        )
        self.repository.upsert_paper_fill_attempt(attempt)
        if bound_snapshot:
            self._active_bound_attempts.add(opportunity_id)
        return attempt

    def _finish_paper_fill_attempt(
        self,
        attempt: PaperFillAttempt,
        *,
        status: PaperFillAttemptStatus,
        occurred_at,
        detail: str | None = None,
    ) -> PaperFillAttempt:
        finished = attempt.model_copy(
            update={
                "status": status,
                "finished_at": occurred_at,
                "detail": detail or attempt.detail,
            }
        )
        self.repository.upsert_paper_fill_attempt(finished)
        if finished.status is not PaperFillAttemptStatus.STARTED:
            self._active_bound_attempts.discard(attempt.opportunity_id)
        return finished

    def _revive_presentation_stale_for_bound_entry(
        self, current: NearOpportunity
    ) -> NearOpportunity:
        if not _presentation_stale_only(current.rejection_reasons):
            return current
        revived = current.model_copy(
            update={
                "status": OpportunityStatus.TRIGGERED,
                "classification": classification_for(OpportunityStatus.TRIGGERED),
                "is_arbitrage": True,
                "rejection_reasons": [
                    reason for reason in current.rejection_reasons if reason != "stale_quote"
                ],
            }
        )
        self.repository.upsert_opportunity(revived)
        return revived

    def record_paper_fill_rejection(
        self,
        opportunity_id: str,
        *,
        occurred_at,
        detail: str,
        reject_triggered: bool = False,
    ) -> NearOpportunity | None:
        """Surface a failed paper-entry attempt without OPEN/PARTIAL/FILLED mutation.

        `reject_triggered=True` is for persist_triggered_chain capture skips
        only. Operator/manual-external `_fail_entry` must leave TRIGGERED so
        Tenet 16 confirmation can continue.
        """

        with self.repository.transaction():
            current = self.repository.get(opportunity_id)
            if current is None:
                return None
            attempt = self.repository.get_started_paper_fill_attempt(opportunity_id)
            if attempt is None:
                attempt = self.latest_paper_fill_attempt(opportunity_id)
            if attempt is not None and attempt.status is PaperFillAttemptStatus.STARTED:
                self._finish_paper_fill_attempt(
                    attempt,
                    status=PaperFillAttemptStatus.REJECTED,
                    occurred_at=occurred_at,
                    detail=detail,
                )
            status = current.status
            if current.status is OpportunityStatus.PAPER_FILLING or (
                reject_triggered and current.status is OpportunityStatus.TRIGGERED
            ):
                reasons = list(dict.fromkeys([*current.rejection_reasons, detail]))
                current = current.model_copy(
                    update={
                        "status": OpportunityStatus.REJECTED,
                        "classification": classification_for(OpportunityStatus.REJECTED),
                        "is_arbitrage": False,
                        "guaranteed_profit_gbp": None,
                        "rejection_reasons": reasons,
                        "last_seen_at": occurred_at,
                    }
                )
                self.repository.upsert_opportunity(current, force_status=True)
                status = current.status
            event_detail = detail
            attempt_id = None if attempt is None else attempt.attempt_id
            if attempt_id and attempt_id not in event_detail:
                event_detail = f"attempt_id={attempt_id}; {detail}"
            self.repository.append_event(
                OpportunityLifecycleEvent(
                    event_id=paper_fill_lifecycle_event_id(
                        opportunity_id,
                        LifecycleEventType.PAPER_FILL_REJECTED,
                        attempt_id,
                    ),
                    opportunity_id=opportunity_id,
                    occurred_at=occurred_at,
                    event_type=LifecycleEventType.PAPER_FILL_REJECTED,
                    status=status,
                    current_net_edge=current.current_net_edge,
                    distance_to_trigger_pp=current.distance_to_trigger_pp,
                    detail=event_detail,
                )
            )
            self._active_bound_attempts.discard(opportunity_id)
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

    def record_hot_promotion(
        self,
        *,
        canonical_event_id: str,
        occurred_at,
        episode: int,
        fixture_label: str | None = None,
        market_family: str | None = None,
        pricing_lane: str | None = None,
        current_net_edge: Decimal | None = None,
        distance_to_trigger_pp: Decimal | None = None,
        opportunity_id: str | None = None,
        detail: str | None = None,
    ) -> OpportunityLifecycleEvent:
        """Persist one BACKGROUND→HOT promotion episode. Idempotent per episode.

        Does not observe, qualify, capture, or change scheduler membership.
        """

        if episode <= 0:
            raise ValueError("hot promotion episode must be positive")
        evaluated = require_aware_instant(occurred_at, "occurred_at")
        event_opportunity_id = opportunity_id or hot_promotion_opportunity_id(canonical_event_id)
        event = OpportunityLifecycleEvent(
            event_id=hot_promotion_lifecycle_event_id(canonical_event_id, episode),
            opportunity_id=event_opportunity_id,
            occurred_at=evaluated,
            event_type=LifecycleEventType.PROMOTED_TO_HOT,
            status=OpportunityStatus.WATCHING,
            current_net_edge=current_net_edge,
            distance_to_trigger_pp=distance_to_trigger_pp,
            detail=detail
            or format_hot_promotion_detail(
                canonical_event_id=canonical_event_id,
                fixture_label=fixture_label,
                market_family=market_family,
                pricing_lane=pricing_lane,
                current_net_edge=current_net_edge,
                distance_to_trigger_pp=distance_to_trigger_pp,
            ),
        )
        self.repository.append_event(event)
        return event

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
        collection_cohort_ids: set[str] | None = None,
    ) -> list[NearOpportunity]:
        """Current radar board from the dual-cadence current-state merge.

        Pass `collection_cohort_ids` from FixtureCurrentStateStore radar
        identities. An empty set is an honest empty current snapshot. Omit
        the argument only for unit tests of ranking/freshness against persisted
        rows. This does not delete persisted observations or lifecycle history.
        Tracked does not fail-close on executable quote age; that gate stays on
        Near / Triggered / paper entry. Radar rows may be `radar_current`.
        Read/ranking paths never persist REJECTED for wall-clock quote age.
        """

        items = self._filtered(
            competition=competition,
            venue=venue,
            market_family=market_family,
        )
        if collection_cohort_ids is not None:
            items = filter_tracked_to_cohort(items, collection_cohort_ids)
        evaluated = require_aware_instant(as_of or self._clock(), "as_of")
        presented = []
        for item in items:
            effective = effective_quote_age_ms(item.quote_age_ms, item.last_seen_at, evaluated)
            presented.append(item.model_copy(update={"quote_age_ms": effective}))
        return rank_tracked_opportunities(presented, limit=limit)

    def activity(
        self,
        *,
        limit: int = 100,
        opportunity_id: str | None = None,
        since=None,
        operator_signal: bool = False,
        event_types: Sequence[LifecycleEventType] | None = None,
    ) -> list[OpportunityLifecycleEvent]:
        selected_types: Sequence[LifecycleEventType] | None = event_types
        if operator_signal:
            selected_types = tuple(OPERATOR_ACTIVITY_EVENT_TYPES)
        events = self.repository.list_events(
            limit=limit if opportunity_id else limit * 2,
            opportunity_id=opportunity_id,
            since=since,
            event_types=selected_types,
        )
        if opportunity_id is not None:
            return events
        demo_ids = {
            item.opportunity_id
            for item in self.repository.list_opportunities()
            if item.data_kind == "demo_fixture_replay"
        }
        return [event for event in events if event.opportunity_id not in demo_ids][:limit]

    def operator_activity(
        self,
        *,
        limit: int = 100,
        opportunity_id: str | None = None,
        since=None,
    ) -> list[OpportunityLifecycleEvent]:
        """Primary operator timeline. Noise events stay persisted in ``activity()``."""

        return self.activity(
            limit=limit,
            opportunity_id=opportunity_id,
            since=since,
            operator_signal=True,
        )

    def _freshness_filtered(
        self,
        *,
        competition: str | None,
        venue: VenueName | None,
        market_family: MarketFamily | None,
        as_of: datetime | None,
    ) -> list[NearOpportunity]:
        """Executable Near/Triggered query: in-memory freshness filter, no writes."""

        evaluated = require_aware_instant(as_of or self._clock(), "as_of")
        presented: list[NearOpportunity] = []
        for item in self._filtered(
            competition=competition,
            venue=venue,
            market_family=market_family,
        ):
            current = self._present_freshness(item, evaluated)
            if self._execution_fresh(current):
                presented.append(current)
        return presented

    def _execution_fresh(self, item: NearOpportunity) -> bool:
        # Labelled DEMO / FIXTURE REPLAY books are frozen snapshots, not live
        # venue quotes. Live paper still fail-closes on unknown/stale age.
        if item.data_kind == "demo_fixture_replay":
            return True
        return quote_is_execution_fresh(item.quote_age_ms, self.max_quote_age_ms)

    def _present_freshness(self, item: NearOpportunity, as_of: datetime) -> NearOpportunity:
        """Attach current wall-clock quote age. Never persist lifecycle changes."""

        effective = effective_quote_age_ms(item.quote_age_ms, item.last_seen_at, as_of)
        return item.model_copy(update={"quote_age_ms": effective})

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
        with self.repository.transaction():
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
                if not self._fill_attempt_started(current.opportunity_id):
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

    def _fill_attempt_started(self, opportunity_id: str) -> bool:
        return self.has_active_bound_attempt(opportunity_id)


def _presentation_stale_only(reasons: list[str]) -> bool:
    """True when radar wall-clock aging is the only recorded rejection."""

    if "stale_quote" not in reasons:
        return False
    if "unknown_quote_age" in reasons:
        return False
    if missing_cost_reasons(reasons) or semantic_reasons(reasons):
        return False
    blocking = {
        "missing_executable_outcome_depth",
        "incomplete_outcome_set",
        "missing_net_edge",
        "execution_risk_above_threshold",
        "market_not_equivalent",
    }
    return not any(reason in blocking for reason in reasons)


_opportunity_id = opportunity_id_for_canonical_market


