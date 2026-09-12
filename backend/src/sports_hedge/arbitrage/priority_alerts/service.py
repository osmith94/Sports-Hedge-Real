from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sports_hedge.accounting.dimensions import CapitalSource, StrategyBook
from sports_hedge.arbitrage.models import ArbitrageSolution
from sports_hedge.arbitrage.priority_alerts.models import (
    ExternalCounterpartyPlan,
    ExternalHedgeRevalidation,
    ExternalLegConfirmation,
    LegExecutionMode,
    ManualOverrideRecommendation,
    OperatorAction,
    PriorityAlert,
    PriorityAlertCandidate,
    PriorityAlertEvent,
    PriorityAlertEventType,
    PriorityAlertState,
    PriorityLeg,
    PrioritySeverity,
    SEVERITY_RANK,
)
from sports_hedge.arbitrage.priority_alerts.notifications import (
    EmailNotificationAdapter,
    InAppNotificationAdapter,
    NotificationAdapter,
)
from sports_hedge.arbitrage.priority_alerts.fixed_exposure import (
    revalidate_fixed_external_exposure,
)
from sports_hedge.arbitrage.priority_alerts.qualification import qualify_priority_alert
from sports_hedge.arbitrage.priority_alerts.sizing import (
    additional_capital_required,
    capital_required_by_venue_currency,
    size_for_limiting_stake,
)
from sports_hedge.arbitrage.priority_alerts.thresholds import PriorityAlertThresholds
from sports_hedge.arbitrage.solver import CompleteSetArbitrageSolver
from sports_hedge.config import Settings, get_settings


class PriorityAlertService:
    """Paper-only escalation layer. Never places, cancels, or signs venue orders."""

    def __init__(
        self,
        *,
        thresholds: PriorityAlertThresholds | None = None,
        settings: Settings | None = None,
        solver: CompleteSetArbitrageSolver | None = None,
        adapters: list[NotificationAdapter] | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        if self.settings.sports_hedge_mode != "paper":
            raise ValueError("Priority alerts are paper-mode only")
        if self.settings.sports_hedge_execution_enabled:
            raise ValueError("Priority alerts cannot run while live execution is enabled")
        self.thresholds = thresholds or PriorityAlertThresholds()
        self.solver = solver or CompleteSetArbitrageSolver()
        self.adapters = adapters or [InAppNotificationAdapter(), EmailNotificationAdapter()]
        self._alerts: dict[str, PriorityAlert] = {}
        self._by_opportunity: dict[str, str] = {}
        self._candidates: dict[str, PriorityAlertCandidate] = {}
        self._unconstrained: dict[str, ArbitrageSolution] = {}

    def current_alerts(self) -> list[PriorityAlert]:
        return [alert for alert in self._alerts.values() if alert.is_open]

    def get_alert(self, alert_id: str) -> PriorityAlert | None:
        return self._alerts.get(alert_id)

    def events(self, alert_id: str | None = None) -> list[PriorityAlertEvent]:
        if alert_id is not None:
            alert = self._alerts.get(alert_id)
            return list(alert.history) if alert else []
        events: list[PriorityAlertEvent] = []
        for alert in self._alerts.values():
            events.extend(alert.history)
        return events

    def ingest(self, candidate: PriorityAlertCandidate) -> PriorityAlert | None:
        qualification = qualify_priority_alert(
            candidate,
            self.thresholds,
            solver=self.solver,
        )
        existing_id = self._by_opportunity.get(candidate.opportunity_id)
        existing = self._alerts.get(existing_id) if existing_id else None
        now = datetime.now(UTC)

        if not qualification.qualifies:
            if existing is not None and existing.is_open:
                self._expire(existing, now, detail=";".join(qualification.reasons))
                return existing
            return None

        assert qualification.severity is not None
        assert qualification.recommendation is not None
        assert qualification.ordinary_solution is not None

        if existing is not None and existing.is_open:
            if not self._material_change(existing, qualification.severity, qualification.recommendation):
                return existing
            event_type = (
                PriorityAlertEventType.PRIORITY_ALERT_UPGRADED
                if SEVERITY_RANK[qualification.severity] > SEVERITY_RANK[existing.severity]
                else (
                    PriorityAlertEventType.PRIORITY_ALERT_DOWNGRADED
                    if SEVERITY_RANK[qualification.severity] < SEVERITY_RANK[existing.severity]
                    else PriorityAlertEventType.PRIORITY_ALERT_UPGRADED
                )
            )
            if (
                SEVERITY_RANK[qualification.severity] == SEVERITY_RANK[existing.severity]
                and qualification.recommendation.recommended_size
                < existing.recommendation.recommended_size
            ):
                event_type = PriorityAlertEventType.PRIORITY_ALERT_DOWNGRADED
            existing.severity = qualification.severity
            existing.recommendation = qualification.recommendation
            existing.net_guaranteed_edge = qualification.ordinary_solution.roi
            existing.updated_at = now
            existing.survivability = qualification.recommendation.survivability
            existing.prepared_override = None
            existing.prepared_external = None
            self._apply_external_workflow(existing, candidate)
            self._candidates[existing.alert_id] = candidate
            self._unconstrained[existing.alert_id] = qualification.ordinary_solution
            self._append_event(
                existing,
                event_type,
                now,
                detail="material_quote_change",
            )
            return existing

        alert = PriorityAlert(
            opportunity_id=candidate.opportunity_id,
            canonical_event_id=candidate.canonical_event_id,
            canonical_market_id=candidate.canonical_market_id,
            opened_at=now,
            updated_at=now,
            severity=qualification.severity,
            strategy_book=StrategyBook.ARBITRAGE,
            capital_source=(
                CapitalSource.MANUAL_EXTERNAL
                if candidate.has_external_leg()
                else CapitalSource.MANUAL_OVERRIDE
            ),
            lifecycle_state=(
                PriorityAlertState.AWAITING_EXTERNAL_LEG_CONFIRMATION
                if candidate.has_external_leg()
                else PriorityAlertState.OPEN
            ),
            operator_action=(
                OperatorAction.PREPARE_PROCEED_WITH_EXTERNAL_COUNTERPARTY
                if candidate.has_external_leg()
                else OperatorAction.PREPARE_MANUAL_OVERRIDE
            ),
            net_guaranteed_edge=qualification.ordinary_solution.roi,
            recommendation=qualification.recommendation,
            settlement_equivalent=candidate.settlement_equivalent,
            ordinary_arb_confirmed=True,
            paper_mode=True,
            commits_automated_legs=False,
            places_orders=False,
            eligibility_confirmed=candidate.eligibility_confirmed,
            survivability=qualification.recommendation.survivability,
        )
        self._alerts[alert.alert_id] = alert
        self._by_opportunity[candidate.opportunity_id] = alert.alert_id
        self._candidates[alert.alert_id] = candidate
        self._unconstrained[alert.alert_id] = qualification.ordinary_solution
        self._append_event(alert, PriorityAlertEventType.PRIORITY_ALERT_OPENED, now)
        return alert

    def prepare_manual_override(
        self,
        alert_id: str,
        requested_size: Decimal,
    ) -> ManualOverrideRecommendation:
        alert = self._alerts.get(alert_id)
        if alert is None or not alert.is_open:
            raise KeyError(alert_id)
        if alert.operator_action == OperatorAction.PREPARE_PROCEED_WITH_EXTERNAL_COUNTERPARTY:
            return ManualOverrideRecommendation(
                alert_id=alert.alert_id,
                opportunity_id=alert.opportunity_id,
                strategy_book=StrategyBook.ARBITRAGE,
                capital_source=CapitalSource.MANUAL_EXTERNAL,
                requested_size=requested_size,
                applied_size=Decimal("0"),
                accepted=False,
                capped=False,
                rejection_reason="external_leg_requires_counterparty_workflow",
                paper_mode=True,
                places_orders=False,
                commits_automated_legs=False,
            )
        candidate = self._candidates[alert_id]
        unconstrained = self._unconstrained[alert_id]
        maximum = alert.recommendation.maximum_validated_size
        capped = requested_size > maximum
        accepted = requested_size > 0 and requested_size <= maximum
        applied = maximum if capped else requested_size
        rejection_reason = None
        if requested_size <= 0:
            accepted = False
            applied = Decimal("0")
            rejection_reason = "requested_size_must_be_positive"
        elif capped:
            rejection_reason = "requested_size_exceeds_validated_maximum"

        ticket = ManualOverrideRecommendation(
            alert_id=alert.alert_id,
            opportunity_id=alert.opportunity_id,
            strategy_book=StrategyBook.ARBITRAGE,
            capital_source=CapitalSource.MANUAL_OVERRIDE,
            requested_size=requested_size,
            applied_size=applied if accepted or capped else Decimal("0"),
            accepted=accepted,
            capped=capped,
            rejection_reason=rejection_reason,
            limiting_leg_outcome=alert.recommendation.limiting_leg_outcome,
            quote_age_ms=alert.recommendation.quote_age_ms,
            fill_confidence=alert.recommendation.fill_confidence,
            paper_mode=True,
            places_orders=False,
        )
        if accepted or capped:
            sized = size_for_limiting_stake(
                candidate.legs,
                unconstrained,
                applied,
                solver=self.solver,
            )
            capital_required = capital_required_by_venue_currency(candidate.legs, sized)
            _, additional = additional_capital_required(
                capital_required,
                candidate.automated_pools,
                candidate.legs,
            )
            ticket.stake_plan = sized.stakes
            ticket.capital_required = capital_required
            ticket.additional_capital_required = additional
            ticket.guaranteed_payoff = sized.guaranteed_return
            ticket.guaranteed_profit = sized.guaranteed_profit
            ticket.guaranteed_roi = sized.roi
        if accepted:
            alert.prepared_override = ticket
            self._append_event(
                alert,
                PriorityAlertEventType.MANUAL_OVERRIDE_PREPARED,
                datetime.now(UTC),
                detail=f"requested={requested_size}",
            )
        return ticket

    def prepare_external_counterparty(
        self,
        alert_id: str,
        requested_size: Decimal,
    ) -> ExternalCounterpartyPlan:
        alert = self._alerts.get(alert_id)
        if alert is None or not alert.is_open:
            raise KeyError(alert_id)
        candidate = self._candidates[alert_id]
        if not candidate.has_external_leg():
            return ExternalCounterpartyPlan(
                alert_id=alert.alert_id,
                opportunity_id=alert.opportunity_id,
                requested_size=requested_size,
                applied_size=Decimal("0"),
                accepted=False,
                rejection_reason="no_external_operator_leg",
            )
        if not candidate.eligibility_confirmed:
            return ExternalCounterpartyPlan(
                alert_id=alert.alert_id,
                opportunity_id=alert.opportunity_id,
                requested_size=requested_size,
                applied_size=Decimal("0"),
                accepted=False,
                rejection_reason="eligibility_not_confirmed",
            )
        unconstrained = self._unconstrained[alert_id]
        maximum = alert.recommendation.maximum_validated_size
        capped = requested_size > maximum
        accepted = requested_size > 0 and requested_size <= maximum
        applied = maximum if capped else requested_size
        rejection_reason = None
        if requested_size <= 0:
            accepted = False
            applied = Decimal("0")
            rejection_reason = "requested_size_must_be_positive"
        elif capped:
            rejection_reason = "requested_size_exceeds_validated_maximum"

        plan = ExternalCounterpartyPlan(
            alert_id=alert.alert_id,
            opportunity_id=alert.opportunity_id,
            requested_size=requested_size,
            applied_size=applied if accepted or capped else Decimal("0"),
            accepted=accepted,
            capped=capped,
            rejection_reason=rejection_reason,
            external_legs=[
                leg
                for leg in candidate.legs
                if leg.execution_mode == LegExecutionMode.EXTERNAL_OPERATOR
            ],
            lifecycle_state=PriorityAlertState.AWAITING_EXTERNAL_LEG_CONFIRMATION,
            places_orders=False,
            commits_automated_legs=False,
        )
        if accepted or capped:
            sized = size_for_limiting_stake(
                candidate.legs,
                unconstrained,
                applied,
                solver=self.solver,
            )
            capital_required = capital_required_by_venue_currency(candidate.legs, sized)
            auto_draw, additional = additional_capital_required(
                capital_required,
                candidate.automated_pools,
                candidate.legs,
            )
            plan.stake_plan = sized.stakes
            plan.capital_required = capital_required
            plan.additional_capital_required = additional
            plan.auto_pool_draw = auto_draw
            plan.guaranteed_payoff = sized.guaranteed_return
            plan.guaranteed_profit = sized.guaranteed_profit
            plan.guaranteed_roi = sized.roi
            plan.survivability = alert.survivability
        if accepted:
            alert.prepared_external = plan
            alert.lifecycle_state = PriorityAlertState.AWAITING_EXTERNAL_LEG_CONFIRMATION
            alert.commits_automated_legs = False
            self._append_event(
                alert,
                PriorityAlertEventType.EXTERNAL_COUNTERPARTY_PREPARED,
                datetime.now(UTC),
                detail="hard_stop_before_automated_hedge",
            )
        return plan

    def confirm_external_leg(
        self,
        alert_id: str,
        confirmation: ExternalLegConfirmation,
        fresh_candidate: PriorityAlertCandidate,
    ) -> ExternalHedgeRevalidation:
        alert = self._alerts.get(alert_id)
        if alert is None or not alert.is_open:
            raise KeyError(alert_id)
        stored = self._candidates[alert_id]
        now = datetime.now(UTC)

        reasons: list[str] = []
        if not confirmation.eligibility_confirmed:
            reasons.append("eligibility_not_confirmed")
        if not fresh_candidate.eligibility_confirmed:
            reasons.append("eligibility_not_confirmed")
        matching = next(
            (
                leg
                for leg in stored.legs
                if leg.execution_mode == LegExecutionMode.EXTERNAL_OPERATOR
                and leg.outcome == confirmation.outcome
                and leg.venue == confirmation.venue
                and leg.source_market_id == confirmation.product_id
            ),
            None,
        )
        if matching is None:
            reasons.append("confirmation_does_not_match_external_leg")
        if confirmation.currency.upper() != (matching.native_currency if matching else ""):
            reasons.append("confirmation_currency_mismatch")

        alert.external_confirmation = confirmation
        self._append_event(
            alert,
            PriorityAlertEventType.EXTERNAL_LEG_CONFIRMATION_RECORDED,
            now,
            detail=confirmation.operator_counterparty_reference,
        )

        if reasons:
            return self._fail_external_revalidation(alert, confirmation, reasons, now)

        executed_reporting = confirmation.executed_size * matching.gbp_per_unit
        external_leg = matching.model_copy(
            update={
                "net_decimal_odds": confirmation.executed_price,
                "max_stake_reporting": executed_reporting,
                "native_max_stake": confirmation.executed_size,
                "execution_mode": LegExecutionMode.EXTERNAL_OPERATOR,
            }
        )
        hedge_legs = [
            leg
            for leg in fresh_candidate.legs
            if not (
                leg.outcome == matching.outcome and leg.venue == matching.venue
            )
        ]
        cost_reasons = _fresh_cost_reasons(fresh_candidate, [external_leg, *hedge_legs])
        if cost_reasons:
            return self._fail_external_revalidation(alert, confirmation, cost_reasons, now)

        sized = revalidate_fixed_external_exposure(
            executed_stake_reporting=executed_reporting,
            executed_price=confirmation.executed_price,
            external_leg=external_leg,
            hedge_legs=hedge_legs,
        )
        if not sized.is_arbitrage or sized.guaranteed_profit <= 0:
            fail_reasons = [sized.rejection_reason or "fresh_hedge_economics_failed"]
            return self._fail_external_revalidation(alert, confirmation, fail_reasons, now)

        external_stake = next(
            item.stake for item in sized.stakes if item.outcome == matching.outcome
        )
        if external_stake != executed_reporting:
            return self._fail_external_revalidation(
                alert,
                confirmation,
                ["confirmed_external_exposure_not_fully_hedgeable"],
                now,
            )

        rebuilt_legs = [external_leg, *hedge_legs]
        capital_required = capital_required_by_venue_currency(rebuilt_legs, sized)
        result = ExternalHedgeRevalidation(
            accepted=True,
            reasons=[],
            lifecycle_state=PriorityAlertState.HEDGE_REVALIDATED,
            confirmation=confirmation,
            net_guaranteed_edge=sized.roi,
            guaranteed_profit=sized.guaranteed_profit,
            capital_required=capital_required,
            stake_plan=sized.stakes,
            fixed_external_stake=executed_reporting,
            commits_automated_legs=False,
            places_orders=False,
        )
        alert.lifecycle_state = PriorityAlertState.HEDGE_REVALIDATED
        alert.hedge_revalidation = result
        alert.commits_automated_legs = False
        alert.external_confirmation = confirmation
        self._append_event(
            alert,
            PriorityAlertEventType.EXTERNAL_HEDGE_REVALIDATED,
            now,
            detail="paper_only_no_automated_commit",
        )
        return result

    def cancel_external_counterparty(self, alert_id: str) -> PriorityAlert:
        alert = self._alerts.get(alert_id)
        if alert is None:
            raise KeyError(alert_id)
        alert.prepared_external = None
        alert.external_confirmation = None
        alert.hedge_revalidation = None
        if any(
            leg.execution_mode == LegExecutionMode.EXTERNAL_OPERATOR
            for leg in self._candidates[alert_id].legs
        ):
            alert.lifecycle_state = PriorityAlertState.AWAITING_EXTERNAL_LEG_CONFIRMATION
        alert.updated_at = datetime.now(UTC)
        self._append_event(
            alert,
            PriorityAlertEventType.EXTERNAL_COUNTERPARTY_CANCELLED,
            alert.updated_at,
        )
        return alert

    def _apply_external_workflow(
        self,
        alert: PriorityAlert,
        candidate: PriorityAlertCandidate,
    ) -> None:
        if candidate.has_external_leg():
            alert.capital_source = CapitalSource.MANUAL_EXTERNAL
            alert.lifecycle_state = PriorityAlertState.AWAITING_EXTERNAL_LEG_CONFIRMATION
            alert.operator_action = OperatorAction.PREPARE_PROCEED_WITH_EXTERNAL_COUNTERPARTY
            alert.commits_automated_legs = False
            alert.places_orders = False
            alert.eligibility_confirmed = candidate.eligibility_confirmed
        else:
            alert.capital_source = CapitalSource.MANUAL_OVERRIDE
            alert.lifecycle_state = PriorityAlertState.OPEN
            alert.operator_action = OperatorAction.PREPARE_MANUAL_OVERRIDE

    def cancel_manual_override(self, alert_id: str) -> PriorityAlert:
        alert = self._alerts.get(alert_id)
        if alert is None:
            raise KeyError(alert_id)
        alert.prepared_override = None
        alert.updated_at = datetime.now(UTC)
        self._append_event(
            alert,
            PriorityAlertEventType.MANUAL_OVERRIDE_CANCELLED,
            alert.updated_at,
        )
        return alert

    def _expire(self, alert: PriorityAlert, when: datetime, *, detail: str | None) -> None:
        alert.expired_at = when
        alert.updated_at = when
        alert.prepared_override = None
        alert.prepared_external = None
        alert.lifecycle_state = PriorityAlertState.EXPIRED
        alert.commits_automated_legs = False
        self._append_event(
            alert,
            PriorityAlertEventType.PRIORITY_ALERT_EXPIRED,
            when,
            detail=detail,
        )

    def _append_event(
        self,
        alert: PriorityAlert,
        event_type: PriorityAlertEventType,
        when: datetime,
        *,
        detail: str | None = None,
    ) -> None:
        event = PriorityAlertEvent(
            event_type=event_type,
            occurred_at=when,
            alert_id=alert.alert_id,
            opportunity_id=alert.opportunity_id,
            severity=alert.severity,
            detail=detail,
        )
        alert.history.append(event)
        for adapter in self.adapters:
            adapter.notify(event, alert)

    def _material_change(
        self,
        existing: PriorityAlert,
        severity: PrioritySeverity,
        recommendation,
    ) -> bool:
        if existing.severity != severity:
            return True
        edge_delta = abs(existing.net_guaranteed_edge - recommendation.guaranteed_roi)
        if edge_delta >= self.thresholds.material_edge_delta:
            return True
        if existing.recommendation.recommended_size == 0:
            return True
        size_ratio = abs(
            existing.recommendation.recommended_size - recommendation.recommended_size
        ) / existing.recommendation.recommended_size
        return size_ratio >= self.thresholds.material_size_ratio

    def _fail_external_revalidation(
        self,
        alert: PriorityAlert,
        confirmation: ExternalLegConfirmation,
        reasons: list[str],
        now: datetime,
    ) -> ExternalHedgeRevalidation:
        result = ExternalHedgeRevalidation(
            accepted=False,
            reasons=reasons,
            lifecycle_state=PriorityAlertState.HEDGE_REVALIDATION_FAILED,
            confirmation=confirmation,
            commits_automated_legs=False,
            places_orders=False,
        )
        alert.lifecycle_state = result.lifecycle_state
        alert.hedge_revalidation = result
        alert.commits_automated_legs = False
        self._append_event(
            alert,
            PriorityAlertEventType.EXTERNAL_HEDGE_REVALIDATION_FAILED,
            now,
            detail=";".join(reasons),
        )
        return result


def _fresh_cost_reasons(
    candidate: PriorityAlertCandidate,
    legs: list[PriorityLeg],
) -> list[str]:
    required_venues = {leg.venue for leg in legs}
    fee_venues = {snapshot.venue for snapshot in candidate.fee_snapshots}
    reasons = [
        f"missing_fee_snapshot:{venue.value}"
        for venue in sorted(required_venues - fee_venues, key=lambda item: item.value)
    ]
    required_currencies = {leg.native_currency for leg in legs}
    fx_currencies = {snapshot.currency.upper() for snapshot in candidate.fx_snapshots}
    fx_currencies.add("GBP")
    reasons.extend(
        f"missing_fx_rate:{currency}"
        for currency in sorted(required_currencies - fx_currencies)
    )
    return reasons
