from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from inspect import getsource
from pathlib import Path

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.priority_alerts import get_priority_alert_service
from sports_hedge.arbitrage.models import ExecutableQuote
from sports_hedge.arbitrage.priority_alerts.models import (
    AutomatedPoolBalance,
    CapitalSource,
    ExternalLegConfirmation,
    LegExecutionMode,
    OpportunitySurvivability,
    OperatorAction,
    PriorityAlertCandidate,
    PriorityAlertEventType,
    PriorityAlertState,
    PriorityLeg,
    PrioritySeverity,
    SEVERITY_RANK,
    SurvivabilityComponent,
    SurvivabilityConfidence,
    VolatilityRegime,
)
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.priority_alerts.thresholds import PriorityAlertThresholds
from sports_hedge.arbitrage.solver import CompleteSetArbitrageSolver
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.models import FeeSnapshot
from sports_hedge.paper.models import FxRateSnapshot


SOLVER = CompleteSetArbitrageSolver()


def _leg(
    *,
    outcome: str,
    venue: VenueName,
    max_stake: Decimal,
    odds: Decimal = Decimal("2.2"),
    native_currency: str = "GBP",
    gbp_per_unit: Decimal = Decimal("1"),
    quote_age_ms: int = 180,
    source_market_id: str | None = None,
    execution_mode: LegExecutionMode = LegExecutionMode.INTERNAL,
) -> PriorityLeg:
    return PriorityLeg(
        outcome=outcome,
        venue=venue,
        source_market_id=source_market_id or f"{venue.value}-{outcome}",
        net_decimal_odds=odds,
        max_stake_reporting=max_stake,
        native_currency=native_currency,
        native_max_stake=max_stake / gbp_per_unit,
        gbp_per_unit=gbp_per_unit,
        levels_consumed=1,
        quote_age_ms=quote_age_ms,
        assumed_latency_ms=100,
        execution_mode=execution_mode,
    )


def _candidate(
    *,
    opportunity_id: str = "opp-btts-newcastle-chelsea",
    limiting: Decimal = Decimal("500"),
    hedge: Decimal = Decimal("5000"),
    odds: Decimal = Decimal("2.2"),
    quote_age_ms: int = 180,
    hedge_venue: VenueName = VenueName.MATCHBOOK,
    hedge_currency: str = "GBP",
    hedge_fx: Decimal = Decimal("1"),
    include_fees: bool = True,
    include_fx: bool = True,
    settlement_equivalent: bool = True,
    execution_risk_score: int = 12,
    pools: list[AutomatedPoolBalance] | None = None,
    limiting_mode: LegExecutionMode = LegExecutionMode.INTERNAL,
    eligibility_confirmed: bool = False,
    survivability: OpportunitySurvivability | None = None,
) -> PriorityAlertCandidate:
    legs = [
        _leg(
            outcome="yes",
            venue=VenueName.SMARKETS,
            max_stake=limiting,
            odds=odds,
            quote_age_ms=quote_age_ms,
            execution_mode=limiting_mode,
        ),
        _leg(
            outcome="no",
            venue=hedge_venue,
            max_stake=hedge,
            odds=odds,
            native_currency=hedge_currency,
            gbp_per_unit=hedge_fx,
            quote_age_ms=quote_age_ms,
        ),
    ]
    quotes = [leg.as_executable_quote() for leg in legs]
    solution = SOLVER.solve(quotes)
    fees = []
    fx = []
    if include_fees:
        fees = [
            FeeSnapshot(venue=VenueName.SMARKETS, profit_haircut_rate=Decimal("0.02")),
            FeeSnapshot(venue=hedge_venue, profit_haircut_rate=Decimal("0.02")),
        ]
    if include_fx:
        fx = [
            FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1"), source="test"),
            FxRateSnapshot(currency=hedge_currency, gbp_per_unit=hedge_fx, source="test"),
        ]
    return PriorityAlertCandidate(
        opportunity_id=opportunity_id,
        canonical_event_id="evt-newcastle-chelsea",
        canonical_market_id="mkt-btts",
        settlement_equivalent=settlement_equivalent,
        ordinary_solution=solution,
        legs=legs,
        fee_snapshots=fees,
        fx_snapshots=fx,
        automated_pools=pools or [],
        execution_risk_score=execution_risk_score,
        eligibility_confirmed=eligibility_confirmed,
        survivability=survivability,
    )


def _service(**threshold_overrides) -> PriorityAlertService:
    return PriorityAlertService(
        thresholds=PriorityAlertThresholds(**threshold_overrides),
        settings=Settings(),
    )


def test_exceptional_edge_and_depth_triggers_priority_alert() -> None:
    service = _service()
    alert = service.ingest(_candidate())
    assert alert is not None
    assert alert.is_open
    assert alert.ordinary_arb_confirmed is True
    assert alert.paper_mode is True
    assert alert.net_guaranteed_edge >= Decimal("0.08")
    assert alert.severity in {
        PrioritySeverity.HIGH_PRIORITY,
        PrioritySeverity.CRITICAL,
    }
    assert alert.history[0].event_type == PriorityAlertEventType.PRIORITY_ALERT_OPENED
    assert alert.recommendation.limiting_leg_outcome == "yes"
    assert alert.recommendation.fill_confidence.inputs.quote_age_ms == 180
    assert alert.survivability is not None
    assert alert.survivability.estimate_not_guarantee is True
    assert alert.survivability.data_insufficient is True
    assert alert.survivability.survivability_score is None
    assert alert.recommendation.survivability is not None
    assert alert.recommendation.survivability.data_insufficient is True


def test_ordinary_arb_does_not_trigger_when_priority_thresholds_not_met() -> None:
    service = _service()
    candidate = _candidate(odds=Decimal("2.05"))
    assert candidate.ordinary_solution.is_arbitrage is True
    assert candidate.ordinary_solution.roi < Decimal("0.03")
    assert service.ingest(candidate) is None
    assert service.current_alerts() == []


def test_limiting_leg_caps_validated_size_before_safety_haircut() -> None:
    service = _service(safety_haircut=Decimal("0.05"))
    alert = service.ingest(_candidate(limiting=Decimal("500"), hedge=Decimal("5000")))
    assert alert is not None
    recommendation = alert.recommendation
    assert recommendation.raw_limiting_depth == Decimal("500")
    assert recommendation.maximum_validated_size == Decimal("500")
    assert recommendation.limiting_leg_venue == VenueName.SMARKETS
    hedge_stake = next(
        stake.stake for stake in recommendation.stake_plan if stake.outcome == "no"
    )
    assert hedge_stake < Decimal("5000")


def test_safety_haircut_reduces_recommendation() -> None:
    service = _service(safety_haircut=Decimal("0.05"))
    alert = service.ingest(_candidate(limiting=Decimal("500"), hedge=Decimal("5000")))
    assert alert is not None
    assert alert.recommendation.safety_haircut == Decimal("0.05")
    assert alert.recommendation.recommended_size == Decimal("475")
    assert alert.recommendation.recommended_size < alert.recommendation.maximum_validated_size


def test_user_entered_amount_above_validated_maximum_is_rejected_and_capped() -> None:
    service = _service()
    alert = service.ingest(_candidate())
    assert alert is not None
    ticket = service.prepare_manual_override(alert.alert_id, Decimal("501"))
    assert ticket.accepted is False
    assert ticket.capped is True
    assert ticket.rejection_reason == "requested_size_exceeds_validated_maximum"
    assert ticket.applied_size == Decimal("500")
    assert ticket.places_orders is False
    assert ticket.capital_source == CapitalSource.MANUAL_OVERRIDE
    assert ticket.stake_plan
    assert alert.prepared_override is None

    accepted = service.prepare_manual_override(alert.alert_id, Decimal("475"))
    assert accepted.accepted is True
    assert accepted.capped is False
    assert accepted.applied_size == Decimal("475")
    assert alert.prepared_override is not None
    assert alert.history[-1].event_type == PriorityAlertEventType.MANUAL_OVERRIDE_PREPARED

    cancelled = service.cancel_manual_override(alert.alert_id)
    assert cancelled.prepared_override is None
    assert cancelled.history[-1].event_type == PriorityAlertEventType.MANUAL_OVERRIDE_CANCELLED


def test_quote_staleness_prevents_escalation() -> None:
    service = _service(maximum_quote_age_ms=2000)
    assert service.ingest(_candidate(quote_age_ms=5000)) is None
    assert service.current_alerts() == []


def test_missing_fees_and_fx_fail_closed() -> None:
    service = _service()
    missing_fees = _candidate(include_fees=False)
    assert service.ingest(missing_fees) is None

    missing_fx = _candidate(
        hedge_venue=VenueName.POLYMARKET,
        hedge_currency="USD",
        hedge_fx=Decimal("0.75"),
        include_fx=False,
    )
    assert service.ingest(missing_fx) is None
    assert service.current_alerts() == []


def test_multi_currency_capital_remains_separated() -> None:
    pools = [
        AutomatedPoolBalance(
            venue=VenueName.SMARKETS,
            currency="GBP",
            amount=Decimal("100"),
        ),
        AutomatedPoolBalance(
            venue=VenueName.POLYMARKET,
            currency="USD",
            amount=Decimal("100"),
        ),
    ]
    service = _service()
    alert = service.ingest(
        _candidate(
            hedge_venue=VenueName.POLYMARKET,
            hedge_currency="USD",
            hedge_fx=Decimal("0.75"),
            pools=pools,
        )
    )
    assert alert is not None
    currencies = {item.currency for item in alert.recommendation.capital_required}
    venues = {item.venue for item in alert.recommendation.capital_required}
    assert currencies == {"GBP", "USD"}
    assert VenueName.SMARKETS in venues
    assert VenueName.POLYMARKET in venues
    native_sum = sum((item.amount for item in alert.recommendation.capital_required), Decimal("0"))
    reporting = alert.recommendation.total_stake_reporting
    assert native_sum != reporting
    gbp = next(item for item in alert.recommendation.capital_required if item.currency == "GBP")
    usd = next(item for item in alert.recommendation.capital_required if item.currency == "USD")
    assert gbp.amount > 0
    assert usd.amount > 0
    extra_currencies = {item.currency for item in alert.recommendation.additional_capital_required}
    assert extra_currencies == {"GBP", "USD"}


def test_duplicate_alert_does_not_spam_lifecycle_history() -> None:
    service = _service()
    first = service.ingest(_candidate())
    second = service.ingest(_candidate())
    third = service.ingest(_candidate(odds=Decimal("2.201")))
    assert first is not None
    assert second is not None
    assert first.alert_id == second.alert_id
    opened = [
        event
        for event in first.history
        if event.event_type == PriorityAlertEventType.PRIORITY_ALERT_OPENED
    ]
    assert len(opened) == 1
    assert len(first.history) == 1
    assert third is not None
    assert third.alert_id == first.alert_id
    assert len(third.history) == 1

    expired = service.ingest(_candidate(quote_age_ms=9000))
    assert expired is not None
    assert expired.expired_at is not None
    assert expired.history[-1].event_type == PriorityAlertEventType.PRIORITY_ALERT_EXPIRED


def test_priority_alert_package_has_no_execution_path() -> None:
    package = Path(__file__).resolve().parents[1] / "src/sports_hedge/arbitrage/priority_alerts"
    combined = "\n".join(path.read_text() for path in package.glob("*.py"))
    for forbidden in ("place_order", "cancel_order", "wallet", "private_key", "signing", "vpn", "geobypass"):
        assert forbidden not in combined
    source = getsource(PriorityAlertService)
    assert "place_order" not in source
    quotes = [
        ExecutableQuote(
            outcome="yes",
            venue=VenueName.MATCHBOOK,
            source_market_id="m1",
            net_decimal_odds=Decimal("2.2"),
            max_stake=Decimal("500"),
        ),
        ExecutableQuote(
            outcome="no",
            venue=VenueName.POLYMARKET,
            source_market_id="m2",
            net_decimal_odds=Decimal("2.2"),
            max_stake=Decimal("5000"),
        ),
    ]
    assert SOLVER.solve(quotes).is_arbitrage is True


def test_priority_alert_api_is_read_only_prepare_and_detail() -> None:
    service = _service()
    app.dependency_overrides[get_priority_alert_service] = lambda: service
    client = TestClient(app)
    created = client.post("/priority-alerts/evaluate", json=_candidate().model_dump(mode="json"))
    assert created.status_code == 200
    body = created.json()
    assert body["alert_id"]
    alert_id = body["alert_id"]

    listed = client.get("/priority-alerts")
    assert listed.status_code == 200
    assert len(listed.json()) == 1

    detail = client.get(f"/priority-alerts/{alert_id}")
    assert detail.status_code == 200
    assert Decimal(detail.json()["recommendation"]["recommended_size"]) == Decimal("475")

    over = client.post(
        f"/priority-alerts/{alert_id}/manual-override",
        json={"requested_size": "900"},
    )
    assert over.status_code == 200
    assert over.json()["accepted"] is False
    assert over.json()["capped"] is True
    assert over.json()["places_orders"] is False

    ok = client.post(
        f"/priority-alerts/{alert_id}/manual-override",
        json={"requested_size": "400"},
    )
    assert ok.status_code == 200
    assert ok.json()["accepted"] is True

    cancel = client.post(f"/priority-alerts/{alert_id}/manual-override/cancel")
    assert cancel.status_code == 200
    app.dependency_overrides.clear()


def test_external_operator_leg_does_not_draw_auto_pool_and_hard_stops_hedge() -> None:
    pools = [
        AutomatedPoolBalance(
            venue=VenueName.SMARKETS,
            currency="GBP",
            amount=Decimal("10000"),
        ),
        AutomatedPoolBalance(
            venue=VenueName.MATCHBOOK,
            currency="GBP",
            amount=Decimal("100"),
        ),
    ]
    service = _service()
    assert (
        service.ingest(
            _candidate(
                limiting_mode=LegExecutionMode.EXTERNAL_OPERATOR,
                pools=pools,
                eligibility_confirmed=False,
            )
        )
        is None
    )

    alert = service.ingest(
        _candidate(
            limiting_mode=LegExecutionMode.EXTERNAL_OPERATOR,
            pools=pools,
            eligibility_confirmed=True,
        )
    )
    assert alert is not None
    assert alert.capital_source == CapitalSource.MANUAL_EXTERNAL
    assert not alert.capital_source.draws_automated_pool()
    assert alert.capital_source.implies_sports_hedge_custody() is False
    assert alert.lifecycle_state == PriorityAlertState.AWAITING_EXTERNAL_LEG_CONFIRMATION
    assert alert.operator_action == OperatorAction.PREPARE_PROCEED_WITH_EXTERNAL_COUNTERPARTY
    assert alert.commits_automated_legs is False
    assert alert.places_orders is False
    assert "PLACE BET" not in alert.operator_action
    assert alert.recommendation.has_external_leg is True

    smarkets = next(
        item
        for item in alert.recommendation.capital_required
        if item.venue == VenueName.SMARKETS
    )
    assert smarkets.capital_source == CapitalSource.MANUAL_EXTERNAL
    assert all(
        item.venue != VenueName.SMARKETS for item in alert.recommendation.auto_pool_draw
    )
    assert all(
        item.capital_source != CapitalSource.AUTO_POOL
        or item.venue != VenueName.SMARKETS
        for item in alert.recommendation.additional_capital_required
    )
    external_extra = next(
        item
        for item in alert.recommendation.additional_capital_required
        if item.venue == VenueName.SMARKETS
    )
    assert external_extra.amount == smarkets.amount
    assert external_extra.capital_source == CapitalSource.MANUAL_EXTERNAL

    rejected = service.prepare_manual_override(alert.alert_id, Decimal("475"))
    assert rejected.accepted is False
    assert rejected.rejection_reason == "external_leg_requires_counterparty_workflow"
    assert rejected.commits_automated_legs is False

    plan = service.prepare_external_counterparty(alert.alert_id, Decimal("475"))
    assert plan.accepted is True
    assert plan.operator_action == OperatorAction.PREPARE_PROCEED_WITH_EXTERNAL_COUNTERPARTY
    assert plan.commits_automated_legs is False
    assert plan.places_orders is False
    assert plan.lifecycle_state == PriorityAlertState.AWAITING_EXTERNAL_LEG_CONFIRMATION
    assert not any(item.venue == VenueName.SMARKETS for item in plan.auto_pool_draw)


def test_external_confirmation_revalidates_fresh_hedge_and_fails_closed() -> None:
    service = _service()
    original = _candidate(
        limiting_mode=LegExecutionMode.EXTERNAL_OPERATOR,
        eligibility_confirmed=True,
    )
    alert = service.ingest(original)
    assert alert is not None
    plan = service.prepare_external_counterparty(alert.alert_id, Decimal("475"))
    assert plan.accepted is True

    confirmation = ExternalLegConfirmation(
        outcome="yes",
        venue=VenueName.SMARKETS,
        product_id="smarkets-yes",
        operator_counterparty_reference="ext-op-42",
        executed_price=Decimal("2.2"),
        executed_size=Decimal("475"),
        currency="GBP",
        executed_at=datetime(2026, 9, 11, 22, 0, tzinfo=UTC),
        eligibility_confirmed=True,
        external_reference="ticket-99",
        evidence="paper-fill-note",
    )
    failed = service.confirm_external_leg(
        alert.alert_id,
        confirmation,
        _candidate(
            limiting_mode=LegExecutionMode.EXTERNAL_OPERATOR,
            odds=Decimal("1.50"),
            eligibility_confirmed=True,
        ),
    )
    assert failed.accepted is False
    assert failed.commits_automated_legs is False
    assert failed.lifecycle_state == PriorityAlertState.HEDGE_REVALIDATION_FAILED
    assert alert.lifecycle_state == PriorityAlertState.HEDGE_REVALIDATION_FAILED

    recovered = _candidate(
        limiting_mode=LegExecutionMode.EXTERNAL_OPERATOR,
        eligibility_confirmed=True,
    )
    ok = service.confirm_external_leg(alert.alert_id, confirmation, recovered)
    assert ok.accepted is True
    assert ok.commits_automated_legs is False
    assert ok.places_orders is False
    assert ok.confirmation.operator_counterparty_reference == "ext-op-42"
    assert ok.confirmation.executed_price == Decimal("2.2")
    assert ok.confirmation.executed_size == Decimal("475")
    assert ok.confirmation.currency == "GBP"
    assert ok.confirmation.external_reference == "ticket-99"
    assert ok.fixed_external_stake == Decimal("475")
    external_stake = next(item.stake for item in ok.stake_plan if item.outcome == "yes")
    assert external_stake == Decimal("475")
    assert alert.lifecycle_state == PriorityAlertState.HEDGE_REVALIDATED
    assert alert.commits_automated_legs is False


def test_confirmed_external_size_is_fully_hedgeable_when_fresh_depth_covers_it() -> None:
    service = _service()
    alert = service.ingest(
        _candidate(
            limiting_mode=LegExecutionMode.EXTERNAL_OPERATOR,
            eligibility_confirmed=True,
        )
    )
    assert alert is not None
    confirmation = ExternalLegConfirmation(
        outcome="yes",
        venue=VenueName.SMARKETS,
        product_id="smarkets-yes",
        operator_counterparty_reference="ext-op-full",
        executed_price=Decimal("2.2"),
        executed_size=Decimal("475"),
        currency="GBP",
        executed_at=datetime(2026, 9, 11, 22, 0, tzinfo=UTC),
        eligibility_confirmed=True,
    )
    result = service.confirm_external_leg(
        alert.alert_id,
        confirmation,
        _candidate(
            limiting_mode=LegExecutionMode.EXTERNAL_OPERATOR,
            hedge=Decimal("5000"),
            eligibility_confirmed=True,
        ),
    )
    assert result.accepted is True
    assert result.commits_automated_legs is False
    assert result.fixed_external_stake == Decimal("475")
    hedge_stake = next(item.stake for item in result.stake_plan if item.outcome == "no")
    assert hedge_stake == Decimal("475")
    assert hedge_stake <= Decimal("5000")


def test_confirmed_external_size_fails_when_fresh_hedge_cannot_cover_full_exposure() -> None:
    service = _service()
    alert = service.ingest(
        _candidate(
            limiting_mode=LegExecutionMode.EXTERNAL_OPERATOR,
            eligibility_confirmed=True,
        )
    )
    assert alert is not None
    confirmation = ExternalLegConfirmation(
        outcome="yes",
        venue=VenueName.SMARKETS,
        product_id="smarkets-yes",
        operator_counterparty_reference="ext-op-partial",
        executed_price=Decimal("2.2"),
        executed_size=Decimal("475"),
        currency="GBP",
        executed_at=datetime(2026, 9, 11, 22, 0, tzinfo=UTC),
        eligibility_confirmed=True,
    )
    shallow = _candidate(
        limiting_mode=LegExecutionMode.EXTERNAL_OPERATOR,
        hedge=Decimal("200"),
        eligibility_confirmed=True,
    )
    unconstrained = SOLVER.solve(
        [
            ExecutableQuote(
                outcome="yes",
                venue=VenueName.SMARKETS,
                source_market_id="smarkets-yes",
                net_decimal_odds=Decimal("2.2"),
                max_stake=Decimal("475"),
            ),
            ExecutableQuote(
                outcome="no",
                venue=VenueName.MATCHBOOK,
                source_market_id="matchbook-no",
                net_decimal_odds=Decimal("2.2"),
                max_stake=Decimal("200"),
            ),
        ]
    )
    assert unconstrained.is_arbitrage is True
    scaled_external = next(item.stake for item in unconstrained.stakes if item.outcome == "yes")
    assert scaled_external < Decimal("475")

    result = service.confirm_external_leg(alert.alert_id, confirmation, shallow)
    assert result.accepted is False
    assert result.commits_automated_legs is False
    assert "confirmed_external_exposure_not_fully_hedgeable" in result.reasons
    assert result.lifecycle_state == PriorityAlertState.HEDGE_REVALIDATION_FAILED


def test_missing_survivability_does_not_invent_a_historical_score() -> None:
    alert = _service().ingest(_candidate())
    assert alert is not None
    assert alert.survivability is not None
    assert "survivability_scorer_not_attached" in alert.survivability.reasons
    assert alert.survivability.low_survivability_warning is False
    assert alert.survivability.survivability_score is None
    assert alert.survivability.survival_probability_at_required_latency is None
    assert alert.survivability.required_action_latency_seconds is None
    assert alert.survivability.recent_volatility is None
    assert alert.survivability.survivability_confidence == SurvivabilityConfidence.UNKNOWN
    assert alert.survivability.volatility_regime == VolatilityRegime.UNKNOWN
    assert alert.survivability.components == []
    assert alert.recommendation.fill_confidence.score > 0
    dumped = alert.survivability.model_dump()
    assert dumped["survivability_score"] is None
    assert dumped["survival_probability_at_required_latency"] is None
    assert dumped["required_action_latency_seconds"] is None
    assert dumped["survivability_confidence"] == SurvivabilityConfidence.UNKNOWN
    assert dumped["survivability_reasons"] == alert.survivability.reasons


def test_manual_external_low_survivability_warns_and_downgrades_severity() -> None:
    service = _service()
    without = service.ingest(
        _candidate(
            limiting_mode=LegExecutionMode.EXTERNAL_OPERATOR,
            eligibility_confirmed=True,
        )
    )
    assert without is not None
    baseline = without.severity

    alert = service.ingest(
        _candidate(
            opportunity_id="opp-external-low-survivability",
            limiting_mode=LegExecutionMode.EXTERNAL_OPERATOR,
            eligibility_confirmed=True,
            survivability=OpportunitySurvivability(
                survivability_score=22,
                expected_external_confirmation_latency_seconds=Decimal("30"),
                survival_probability_30s=Decimal("0.18"),
                volatility_regime=VolatilityRegime.TURBULENT,
                recent_volatility=Decimal("0.042"),
                survivability_confidence=SurvivabilityConfidence.LOW,
                data_insufficient=False,
                reasons=["rapid_cross_venue_convergence"],
                components=[
                    SurvivabilityComponent(
                        name="cross_venue_convergence",
                        assessment="low",
                        value=Decimal("0.18"),
                        reason="rapid_cross_venue_convergence",
                    )
                ],
            ),
        )
    )
    assert alert is not None
    assert alert.survivability is not None
    assert alert.survivability.estimate_not_guarantee is True
    assert alert.survivability.low_survivability_warning is True
    assert "external_confirmation_latency_exceeds_survivability" in alert.survivability.reasons
    assert alert.survivability.survival_probability_at_required_latency == Decimal("0.18")
    assert alert.survivability.survival_probability_at_action_latency == Decimal("0.18")
    assert alert.survivability.required_action_latency_seconds == Decimal("30")
    assert alert.survivability.expected_action_latency_seconds == Decimal("30")
    assert alert.survivability.expected_external_confirmation_latency_seconds == Decimal("30")
    assert alert.survivability.volatility_regime == VolatilityRegime.TURBULENT
    assert alert.survivability.recent_volatility == Decimal("0.042")
    assert alert.survivability.survivability_confidence == SurvivabilityConfidence.LOW
    assert alert.survivability.components[0].assessment == "low"
    assert "rapid_cross_venue_convergence" in alert.survivability.survivability_reasons
    assert alert.recommendation.fill_confidence.band != alert.survivability.survivability_confidence
    assert SEVERITY_RANK[alert.severity] < SEVERITY_RANK[baseline]
    assert alert.severity != PrioritySeverity.CRITICAL
    plan = service.prepare_external_counterparty(alert.alert_id, Decimal("475"))
    assert plan.survivability is not None
    assert plan.survivability.low_survivability_warning is True
    assert plan.survivability.required_action_latency_seconds == Decimal("30")
    assert alert.commits_automated_legs is False
