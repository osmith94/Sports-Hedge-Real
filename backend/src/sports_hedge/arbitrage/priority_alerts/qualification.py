from __future__ import annotations

from sports_hedge.arbitrage.priority_alerts.fill_confidence import meets_minimum
from sports_hedge.arbitrage.priority_alerts.models import (
    FillConfidence,
    PriorityAlertCandidate,
    PrioritySeverity,
    QualificationResult,
)
from sports_hedge.arbitrage.priority_alerts.sizing import recommend_manual_size
from sports_hedge.arbitrage.priority_alerts.survivability import (
    apply_survivability_to_severity,
    assess_opportunity_survivability,
)
from sports_hedge.arbitrage.priority_alerts.thresholds import PriorityAlertThresholds
from sports_hedge.arbitrage.solver import CompleteSetArbitrageSolver


def qualify_priority_alert(
    candidate: PriorityAlertCandidate,
    thresholds: PriorityAlertThresholds,
    *,
    solver: CompleteSetArbitrageSolver | None = None,
) -> QualificationResult:
    reasons: list[str] = []
    solver = solver or CompleteSetArbitrageSolver()

    if not candidate.settlement_equivalent:
        reasons.append("settlement_not_equivalent")
    if not candidate.ordinary_solution.is_arbitrage:
        reasons.append("ordinary_arb_not_confirmed")
    if candidate.ordinary_solution.guaranteed_profit <= 0:
        reasons.append("non_positive_minimum_payoff")
    if len(candidate.legs) < 2:
        reasons.append("incomplete_leg_set")
    if candidate.has_external_leg() and not candidate.eligibility_confirmed:
        reasons.append("eligibility_not_confirmed")

    required_venues = {leg.venue for leg in candidate.legs}
    fee_venues = {snapshot.venue for snapshot in candidate.fee_snapshots}
    missing_fees = sorted(venue.value for venue in required_venues - fee_venues)
    if missing_fees:
        reasons.extend(f"missing_fee_snapshot:{venue}" for venue in missing_fees)

    required_currencies = {leg.native_currency for leg in candidate.legs}
    fx_currencies = {snapshot.currency.upper() for snapshot in candidate.fx_snapshots}
    if "GBP" not in fx_currencies:
        # GBP is always available as the functional reporting currency.
        fx_currencies.add("GBP")
    missing_fx = sorted(currency for currency in required_currencies if currency not in fx_currencies)
    if missing_fx:
        reasons.extend(f"missing_fx_rate:{currency}" for currency in missing_fx)

    if reasons:
        return QualificationResult(qualifies=False, reasons=reasons)

    quotes = [leg.as_executable_quote() for leg in candidate.legs]
    unconstrained = solver.solve(quotes)
    if not unconstrained.is_arbitrage or unconstrained.guaranteed_profit <= 0:
        return QualificationResult(
            qualifies=False,
            reasons=["ordinary_arb_not_confirmed"],
            ordinary_solution=unconstrained,
        )

    quote_age_ms = max(leg.quote_age_ms for leg in candidate.legs)
    if quote_age_ms > thresholds.maximum_quote_age_ms:
        reasons.append("quote_stale")

    recommendation = recommend_manual_size(
        candidate.legs,
        unconstrained,
        thresholds=thresholds,
        execution_risk_score=candidate.execution_risk_score,
        automated_pools=candidate.automated_pools,
        solver=solver,
    )
    survivability = assess_opportunity_survivability(candidate, thresholds)
    recommendation = recommendation.model_copy(update={"survivability": survivability})

    if unconstrained.roi < thresholds.minimum_net_edge:
        reasons.append("net_edge_below_priority_threshold")
    if recommendation.guaranteed_profit < thresholds.minimum_expected_profit:
        reasons.append("expected_profit_below_priority_threshold")
    if recommendation.raw_limiting_depth < thresholds.minimum_executable_depth:
        reasons.append("executable_depth_below_priority_threshold")
    if candidate.execution_risk_score > thresholds.maximum_execution_risk:
        reasons.append("execution_risk_above_priority_threshold")
    if recommendation.fill_confidence.inputs.depth_coverage_ratio < thresholds.minimum_depth_coverage:
        reasons.append("depth_coverage_below_priority_threshold")
    if not meets_minimum(
        recommendation.fill_confidence.band,
        thresholds.minimum_fill_confidence,
    ):
        reasons.append("fill_confidence_below_priority_threshold")
    if recommendation.capital_efficiency < thresholds.minimum_capital_efficiency:
        reasons.append("capital_efficiency_below_priority_threshold")

    if reasons:
        return QualificationResult(
            qualifies=False,
            reasons=reasons,
            fill_confidence=recommendation.fill_confidence,
            recommendation=recommendation,
            ordinary_solution=unconstrained,
        )

    severity = _severity(
        edge=unconstrained.roi,
        fill=recommendation.fill_confidence.band,
        quote_age_ms=quote_age_ms,
        execution_risk_score=candidate.execution_risk_score,
        thresholds=thresholds,
    )
    severity = apply_survivability_to_severity(severity, survivability)
    return QualificationResult(
        qualifies=True,
        reasons=[],
        severity=severity,
        fill_confidence=recommendation.fill_confidence,
        recommendation=recommendation,
        ordinary_solution=unconstrained,
    )


def _severity(
    *,
    edge,
    fill: FillConfidence,
    quote_age_ms: int,
    execution_risk_score: int,
    thresholds: PriorityAlertThresholds,
) -> PrioritySeverity:
    if (
        edge >= thresholds.critical_edge
        and fill == FillConfidence.HIGH
        and quote_age_ms <= thresholds.critical_max_quote_age_ms
        and execution_risk_score <= thresholds.critical_max_execution_risk
    ):
        return PrioritySeverity.CRITICAL
    if (
        edge >= thresholds.high_priority_edge
        and FILL_OK_FOR_HIGH[fill]
        and execution_risk_score <= thresholds.high_priority_max_execution_risk
    ):
        return PrioritySeverity.HIGH_PRIORITY
    return PrioritySeverity.PRIORITY


FILL_OK_FOR_HIGH = {
    FillConfidence.LOW: False,
    FillConfidence.MEDIUM: True,
    FillConfidence.HIGH: True,
}
