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
    LifecycleEventType,
    NearOpportunity,
    OpportunityLifecycleEvent,
    OpportunityStatus,
    PaperFillAttempt,
    PaperFillAttemptStatus,
    WatchObservation,
    canonical_event_id_from_hot_opportunity_id,
    format_hot_promotion_detail,
    hot_promotion_lifecycle_event_id,
    hot_promotion_opportunity_id,
    lifecycle_identity_from_opportunity,
    paper_fill_lifecycle_event_id,
    qualifying_lifecycle_event_id,
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
        pricing_lane: str | None = None,
    ) -> NearOpportunity | None:
        observation = observation_from_paper_decision(
            decision,
            history,
            quote_age_ms=quote_age_ms,
            quote_age_basis=quote_age_basis,
            pricing_lane=pricing_lane,
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
        if (
            observation.current_net_edge is not None
            and observation.trigger_net_edge is not None
        ):
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
            line=observation.line,
            venues=observation.venues,
            legs=observation.legs,
            status=status,
            classification=classification_for(status),
            is_arbitrage=is_arbitrage,
            trigger_net_edge=observation.trigger_net_edge,
            min_net_edge_scope=observation.min_net_edge_scope,
            min_net_edge_source=observation.min_net_edge_source,
            min_net_edge_configured=observation.min_net_edge_configured,
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
            capture_eligible=_episode_capture_eligible(previous, status, observation),
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
        try:
            with self.repository.transaction():
                return self._record_paper_fill_locked(
                    opportunity_id, stage=stage, occurred_at=occurred_at, detail=detail
                )
        except ValueError as exc:
            self._append_lifecycle_rejection(opportunity_id, occurred_at, str(exc))
            raise

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
            from sports_hedge.lifecycle.paper import decide_paper_fill

            fill_decision = decide_paper_fill(OpportunityStatus.WATCHING, stage)
            raise ValueError(fill_decision.reason)
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
        from sports_hedge.lifecycle.paper import decide_paper_fill

        fill_decision = decide_paper_fill(
            current.status, stage, bound_snapshot=bind_snapshot
        )
        if not fill_decision.accepted:
            raise ValueError(fill_decision.reason)
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
                attempt_id=attempt.attempt_id,
                **lifecycle_identity_from_opportunity(updated),
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

        try:
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
        except ValueError as exc:
            self._append_lifecycle_rejection(opportunity_id, occurred_at, str(exc))
            raise

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
                    attempt_id=None if attempt is None else attempt.attempt_id,
                    **lifecycle_identity_from_opportunity(updated),
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
        zero_fill_fact: str | None = None,
        zero_fill_audit_detail: str | None = None,
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
                    attempt_id=attempt_id,
                    **lifecycle_identity_from_opportunity(current),
                )
            )
            self._active_bound_attempts.discard(opportunity_id)
            if zero_fill_fact:
                self.repository.append_event(
                    OpportunityLifecycleEvent(
                        event_id=paper_fill_lifecycle_event_id(
                            opportunity_id,
                            LifecycleEventType.ZERO_FILL_EXECUTION_MISS,
                            attempt_id,
                        ),
                        opportunity_id=opportunity_id,
                        occurred_at=occurred_at,
                        event_type=LifecycleEventType.ZERO_FILL_EXECUTION_MISS,
                        status=status,
                        current_net_edge=current.current_net_edge,
                        distance_to_trigger_pp=current.distance_to_trigger_pp,
                        detail=zero_fill_audit_detail
                        or f"fill_result=zero; zero_fill_fact={zero_fill_fact}; reason={detail}",
                        attempt_id=attempt_id,
                        **lifecycle_identity_from_opportunity(current),
                    )
                )
            return current

    def note_execution_reprice_miss(
        self,
        decision: PaperScanDecision,
        *,
        occurred_at,
        reason: str,
        pricing_lane: str | None = None,
        detail: str | None = None,
        quote_age_ms: int | None = None,
    ) -> OpportunityLifecycleEvent | None:
        """Record one fail-closed execution reprice without changing radar status.

        Discovery QUALIFYING stays in place when the second price fails, is
        still stale, or is no longer the capture snapshot. This does not open
        a paper trade and does not schedule another reprice.
        """

        if not decision.canonical_market_id:
            return None
        opportunity_id = opportunity_id_for_canonical_market(decision.canonical_market_id)
        current = self.repository.get(opportunity_id)
        if current is None:
            return None
        event = OpportunityLifecycleEvent(
            event_id=f"{opportunity_id}:{reason}:{uuid4()}",
            opportunity_id=opportunity_id,
            occurred_at=occurred_at,
            event_type=LifecycleEventType.PAPER_FILL_REJECTED,
            status=current.status,
            current_net_edge=current.current_net_edge,
            distance_to_trigger_pp=current.distance_to_trigger_pp,
            detail=detail or reason,
            gross_edge=current.gross_edge,
            limiting_depth_gbp=current.limiting_depth_gbp,
            guaranteed_profit_gbp=current.guaranteed_profit_gbp,
            quote_age_ms=current.quote_age_ms if quote_age_ms is None else quote_age_ms,
            pricing_lane=pricing_lane,
            **lifecycle_identity_from_opportunity(current),
        )
        self.repository.append_event(event)
        return event

    def record_execution_snapshot_audit(
        self,
        *,
        snapshot_id: str,
        opportunity_id: str | None,
        catalogue_row_id: str,
        canonical_market_id: str | None,
        occurred_at: datetime,
        accepted: bool,
        rejection_reason: str | None,
        snapshot_json: str,
        diagnostics_json: str | None = None,
        execution_cycle: int | None = None,
        cycle_outcome: str | None = None,
    ) -> None:
        """Store one Price-2 attempt outside the scanner lifecycle detail."""

        recorder = getattr(self.repository, "append_execution_snapshot_audit", None)
        if not callable(recorder):
            return
        recorder(
            snapshot_id=snapshot_id,
            opportunity_id=opportunity_id,
            catalogue_row_id=catalogue_row_id,
            canonical_market_id=canonical_market_id,
            occurred_at=occurred_at,
            accepted=accepted,
            rejection_reason=rejection_reason,
            snapshot_json=snapshot_json,
            diagnostics_json=diagnostics_json,
            execution_cycle=execution_cycle,
            cycle_outcome=cycle_outcome,
        )

    def next_execution_cycle(self, opportunity_id: str | None) -> int:
        """1-based cycle number for the next Price-2 attempt on this opportunity."""

        counter = getattr(self.repository, "next_execution_cycle", None)
        if not callable(counter):
            return 1
        return int(counter(opportunity_id))

    def link_execution_snapshot_fill(
        self,
        *,
        snapshot_id: str,
        trade_id: str,
        tranche_id: str | None,
        cycle_outcome: str,
    ) -> None:
        """Record which trade tranche, if any, consumed this snapshot."""

        linker = getattr(self.repository, "link_execution_snapshot_fill", None)
        if not callable(linker):
            return
        linker(
            snapshot_id=snapshot_id,
            trade_id=trade_id,
            tranche_id=tranche_id,
            cycle_outcome=cycle_outcome,
        )

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
            fixture_label=fixture_label,
            market_family=market_family,
            canonical_event_id=canonical_event_id,
            canonical_market_id=None,
            capture_eligible=None,
        )
        self.repository.append_event(event)
        return event

    def note_qualifying_radar_expiry(
        self,
        now: datetime,
        *,
        hot_ttl_seconds: int | None = None,
        universe_ttl_seconds: int | None = None,
    ) -> list[OpportunityLifecycleEvent]:
        """Close an open qualifying episode when the existing radar TTL has elapsed.

        Does not change opportunity status, classification, quote-freshness gates,
        or TTL values. Executable quote age alone stays `radar_current` inside the
        TTL and does not close the episode. Generic `expired` remains a separate
        terminal status and is not written here. ``occurred_at`` is the radar TTL
        boundary (`last_seen_at` plus the existing lane TTL), not the caller clock.
        """

        from sports_hedge.application.scan_lanes import (
            DEFAULT_HOT_TTL_SECONDS,
            DEFAULT_UNIVERSE_TTL_SECONDS,
            FRESHNESS_EXPIRED,
            freshness_class,
            observation_expires_at,
        )

        evaluated = require_aware_instant(now, "now")
        hot_ttl = DEFAULT_HOT_TTL_SECONDS if hot_ttl_seconds is None else hot_ttl_seconds
        universe_ttl = (
            DEFAULT_UNIVERSE_TTL_SECONDS if universe_ttl_seconds is None else universe_ttl_seconds
        )
        emitted: list[OpportunityLifecycleEvent] = []
        for opportunity, pricing_lane in self.repository.list_open_qualifying_episodes():
            lane = _radar_lane_for_pricing(pricing_lane)
            if (
                freshness_class(
                    lane=lane,
                    last_scanned_at=opportunity.last_seen_at,
                    now=evaluated,
                    quote_age_ms=opportunity.quote_age_ms,
                    max_quote_age_ms=self.max_quote_age_ms,
                    hot_ttl_seconds=hot_ttl,
                    universe_ttl_seconds=universe_ttl,
                )
                != FRESHNESS_EXPIRED
            ):
                continue
            # The durable time is the radar TTL boundary, not this maintenance call.
            expires = observation_expires_at(
                opportunity.last_seen_at,
                lane,
                hot_ttl_seconds=hot_ttl,
                universe_ttl_seconds=universe_ttl,
            )
            event = self._append_qualifying_radar_expiry(
                opportunity, expires, pricing_lane=pricing_lane
            )
            if event is not None:
                emitted.append(event)
        return emitted

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
        canonical_event_id: str | None = None,
        since=None,
        operator_signal: bool = False,
        event_types: Sequence[LifecycleEventType] | None = None,
    ) -> list[OpportunityLifecycleEvent]:
        """Persisted lifecycle. HOT opportunity ids also join the same canonical event."""

        resolved_canonical = canonical_event_id
        if resolved_canonical is None and opportunity_id is not None:
            resolved_canonical = canonical_event_id_from_hot_opportunity_id(opportunity_id)
        identity_query = opportunity_id is not None or resolved_canonical is not None
        events = self.repository.list_events(
            limit=limit if identity_query else limit * 2,
            opportunity_id=opportunity_id,
            canonical_event_id=resolved_canonical,
            since=since,
            event_types=None if operator_signal else event_types,
            operator_signal=operator_signal,
        )
        if identity_query:
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

    def _append_lifecycle_rejection(
        self, opportunity_id: str, occurred_at, reason: str
    ) -> None:
        current = self.repository.get(opportunity_id)
        if current is None:
            return
        self.repository.append_event(
            OpportunityLifecycleEvent(
                opportunity_id=opportunity_id,
                occurred_at=occurred_at,
                event_type=LifecycleEventType.LIFECYCLE_REJECTED,
                status=current.status,
                current_net_edge=current.current_net_edge,
                distance_to_trigger_pp=current.distance_to_trigger_pp,
                detail=reason,
                **lifecycle_identity_from_opportunity(current),
            )
        )

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
                    **lifecycle_identity_from_opportunity(updated),
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
                if not self._durable_fill_attempt_started(current.opportunity_id):
                    events.append(
                        self._event(
                            current,
                            LifecycleEventType.TRIGGER_LOST_BEFORE_FILL,
                            detail="trigger_lost_before_paper_fill",
                            capture_eligible=previous.capture_eligible,
                        )
                    )
                    if not previous.capture_eligible:
                        lost = self._qualifying_episode_event(
                            current,
                            observation,
                            LifecycleEventType.QUALIFYING_LOST,
                            detail=_qualifying_lost_detail(current),
                        )
                        if lost is not None:
                            events.append(lost)

        if _entered_qualifying_episode(previous, current) or self._qualifying_episode_closed_by_radar(
            previous, current
        ):
            detected = self._qualifying_episode_event(
                current,
                observation,
                LifecycleEventType.QUALIFYING_DETECTED,
                detail="solver_qualified",
            )
            if detected is not None:
                events.append(detected)

        if _entered_capture_eligible_triggered_episode(previous, current):
            events.append(
                self._event(
                    current,
                    LifecycleEventType.PAPER_ELIGIBLE,
                    detail="capture_eligible_triggered",
                    capture_eligible=True,
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

    def _qualifying_episode_closed_by_radar(
        self,
        previous: NearOpportunity | None,
        current: NearOpportunity,
    ) -> bool:
        """True when this repricing follows a radar-TTL close of the same TRIGGERED row.

        Status stays TRIGGERED. The next solver-qualified observation is a new episode.
        """

        if previous is None or previous.status is not OpportunityStatus.TRIGGERED:
            return False
        if current.status is not OpportunityStatus.TRIGGERED:
            return False
        boundary = self.repository.latest_qualifying_boundary(current.opportunity_id)
        return boundary is not None and boundary[0] is LifecycleEventType.QUALIFYING_EXPIRED

    def _append_qualifying_radar_expiry(
        self,
        opportunity: NearOpportunity,
        occurred_at: datetime,
        *,
        pricing_lane: str | None,
    ) -> OpportunityLifecycleEvent | None:
        detected = self.repository.count_events(
            opportunity.opportunity_id,
            LifecycleEventType.QUALIFYING_DETECTED,
        )
        if detected <= 0:
            return None
        if (
            self.repository.count_events(
                opportunity.opportunity_id,
                LifecycleEventType.QUALIFYING_EXPIRED,
            )
            >= detected
        ):
            return None
        venues = [venue.value for venue in opportunity.venues]
        event = self._event(
            opportunity,
            LifecycleEventType.QUALIFYING_EXPIRED,
            detail="radar expired",
            event_id=qualifying_lifecycle_event_id(
                opportunity.opportunity_id,
                LifecycleEventType.QUALIFYING_EXPIRED,
                detected,
            ),
            gross_edge=opportunity.gross_edge,
            limiting_depth_gbp=opportunity.limiting_depth_gbp,
            guaranteed_profit_gbp=opportunity.guaranteed_profit_gbp,
            quote_age_ms=opportunity.quote_age_ms,
            pricing_lane=pricing_lane,
            venue_pair=",".join(venues) if venues else None,
        )
        event.occurred_at = occurred_at
        self.repository.append_event(event)
        return event

    def _qualifying_episode_event(
        self,
        opportunity: NearOpportunity,
        observation: WatchObservation,
        event_type: LifecycleEventType,
        *,
        detail: str,
    ) -> OpportunityLifecycleEvent | None:
        """Durable qualifying episode boundary. Count is the persisted episode index."""

        detected = self.repository.count_events(
            opportunity.opportunity_id,
            LifecycleEventType.QUALIFYING_DETECTED,
        )
        episode = (
            detected + 1 if event_type is LifecycleEventType.QUALIFYING_DETECTED else detected
        )
        if episode <= 0:
            return None
        venues = [venue.value for venue in opportunity.venues]
        return self._event(
            opportunity,
            event_type,
            detail=detail,
            event_id=qualifying_lifecycle_event_id(
                opportunity.opportunity_id,
                event_type,
                episode,
            ),
            gross_edge=opportunity.gross_edge,
            limiting_depth_gbp=opportunity.limiting_depth_gbp,
            guaranteed_profit_gbp=opportunity.guaranteed_profit_gbp,
            quote_age_ms=opportunity.quote_age_ms,
            pricing_lane=observation.pricing_lane,
            venue_pair=",".join(venues) if venues else None,
        )

    def _event(
        self,
        opportunity: NearOpportunity,
        event_type: LifecycleEventType,
        *,
        detail: str | None,
        capture_eligible: bool | None = None,
        event_id: str | None = None,
        gross_edge: Decimal | None = None,
        limiting_depth_gbp: Decimal | None = None,
        guaranteed_profit_gbp: Decimal | None = None,
        quote_age_ms: int | None = None,
        pricing_lane: str | None = None,
        venue_pair: str | None = None,
    ) -> OpportunityLifecycleEvent:
        identity = lifecycle_identity_from_opportunity(
            opportunity, capture_eligible=capture_eligible
        )
        payload: dict[str, object] = {}
        if event_id is not None:
            payload["event_id"] = event_id
        return OpportunityLifecycleEvent(
            opportunity_id=opportunity.opportunity_id,
            occurred_at=opportunity.last_seen_at,
            event_type=event_type,
            status=opportunity.status,
            current_net_edge=opportunity.current_net_edge,
            distance_to_trigger_pp=opportunity.distance_to_trigger_pp,
            detail=detail,
            gross_edge=gross_edge,
            limiting_depth_gbp=limiting_depth_gbp,
            guaranteed_profit_gbp=guaranteed_profit_gbp,
            quote_age_ms=quote_age_ms,
            pricing_lane=pricing_lane,
            venue_pair=venue_pair,
            **identity,
            **payload,
        )

    def _fill_attempt_started(self, opportunity_id: str) -> bool:
        return self.has_active_bound_attempt(opportunity_id)

    def _durable_fill_attempt_started(self, opportunity_id: str) -> bool:
        """True when any STARTED paper-fill attempt exists, bound or unbound."""

        return self.repository.get_started_paper_fill_attempt(opportunity_id) is not None


def _episode_capture_eligible(
    previous: NearOpportunity | None,
    status: OpportunityStatus,
    observation: WatchObservation,
) -> bool:
    """Sticky capture eligibility for one TRIGGERED stay. Independent of economic trigger."""

    if status != OpportunityStatus.TRIGGERED:
        return False
    sticky = (
        previous is not None
        and previous.status == OpportunityStatus.TRIGGERED
        and previous.capture_eligible
    )
    return sticky or observation.eligible_for_paper_simulation


def note_expired_qualifying_episodes(
    service: WatchlistService,
    now: datetime,
    *,
    hot_ttl_seconds: int,
    universe_ttl_seconds: int,
) -> list[OpportunityLifecycleEvent]:
    """Server-owned close of open qualifying episodes past the existing radar TTL."""

    return service.note_qualifying_radar_expiry(
        now,
        hot_ttl_seconds=hot_ttl_seconds,
        universe_ttl_seconds=universe_ttl_seconds,
    )


def _radar_lane_for_pricing(pricing_lane: str | None):
    """Map a stored pricing lane onto the radar TTL lane already used by current state.

    HOT uses the hot radar TTL. BACKGROUND pricing is published on the non-hot
    radar lane, which uses the universe TTL. Missing lane uses that same non-hot TTL.
    """

    from sports_hedge.application.scan_lanes import ScanLane

    if (pricing_lane or "").strip().lower() == ScanLane.HOT.value:
        return ScanLane.HOT
    return ScanLane.UNIVERSE


def _entered_qualifying_episode(
    previous: NearOpportunity | None,
    current: NearOpportunity,
) -> bool:
    """True when this observation opens a solver-qualified TRIGGERED episode.

    QUALIFYING is the economic trigger (`solver_is_arbitrage` and net edge at
    or above the configured minimum). It is not paper-capture eligibility.
    The previous row is the durable episode boundary, including after restart.
    """

    if current.status != OpportunityStatus.TRIGGERED:
        return False
    return previous is None or previous.status != OpportunityStatus.TRIGGERED


def _qualifying_lost_detail(current: NearOpportunity) -> str:
    if current.rejection_reasons:
        reason = ", ".join(reason.replace("_", " ") for reason in current.rejection_reasons)
    else:
        reason = current.status.value.lower()
    return f"qualifying lost · {reason}"


def _entered_capture_eligible_triggered_episode(
    previous: NearOpportunity | None,
    current: NearOpportunity,
) -> bool:
    """Emit Paper eligible once when a TRIGGERED stay first becomes capture-eligible."""

    if current.status != OpportunityStatus.TRIGGERED or not current.capture_eligible:
        return False
    if previous is None:
        return True
    return not (previous.status == OpportunityStatus.TRIGGERED and previous.capture_eligible)


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


