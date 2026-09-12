from __future__ import annotations

from decimal import Decimal

from sports_hedge.arbitrage.priority_alerts.models import (
    OpportunitySurvivability,
    PriorityAlertCandidate,
    PrioritySeverity,
    VolatilityRegime,
)
from sports_hedge.arbitrage.priority_alerts.thresholds import PriorityAlertThresholds

_SEVERITY_DOWNGRADE = {
    PrioritySeverity.CRITICAL: PrioritySeverity.HIGH_PRIORITY,
    PrioritySeverity.HIGH_PRIORITY: PrioritySeverity.PRIORITY,
    PrioritySeverity.PRIORITY: PrioritySeverity.PRIORITY,
}


def assess_opportunity_survivability(
    candidate: PriorityAlertCandidate,
    thresholds: PriorityAlertThresholds,
) -> OpportunitySurvivability:
    """Attach supplied estimates. Does not invent a historical survival model."""

    supplied = candidate.survivability or OpportunitySurvivability()
    reasons = list(supplied.reasons)
    score = supplied.survivability_score
    required_latency = supplied.required_action_latency_seconds
    if candidate.has_external_leg() and required_latency is None:
        required_latency = supplied.expected_external_confirmation_latency_seconds
    action_p = supplied.survival_probability_at_required_latency
    if action_p is None and required_latency is not None:
        action_p = supplied.probability_for_horizon(required_latency)
    data_insufficient = (
        score is None
        and action_p is None
        and supplied.survival_probability_5s is None
        and supplied.survival_probability_15s is None
        and supplied.survival_probability_30s is None
        and supplied.survival_probability_60s is None
        and not supplied.horizons
        and supplied.recent_volatility is None
        and not supplied.components
    )
    if data_insufficient:
        reasons.append("survivability_scorer_not_attached")

    warning = False
    if (
        thresholds.minimum_survivability_score is not None
        and score is not None
        and score < thresholds.minimum_survivability_score
    ):
        warning = True
        reasons.append("survivability_score_below_threshold")
    floor = thresholds.survival_probability_floor()
    if floor is not None and action_p is not None and action_p < floor:
        warning = True
        reasons.append("survival_probability_below_threshold")

    if candidate.has_external_leg():
        compared = action_p
        if (
            compared is not None
            and compared < thresholds.external_survivability_warn_probability
        ):
            warning = True
            reasons.append("external_confirmation_latency_exceeds_survivability")

    regime = supplied.volatility_regime
    if data_insufficient and regime == VolatilityRegime.UNKNOWN:
        reasons.append("volatility_regime_unknown")

    return supplied.model_copy(
        update={
            "estimate_not_guarantee": True,
            "data_insufficient": data_insufficient,
            "required_action_latency_seconds": required_latency,
            "expected_action_latency_seconds": required_latency,
            "survival_probability_at_required_latency": action_p,
            "survival_probability_at_action_latency": action_p,
            "reasons": list(dict.fromkeys(reasons)),
            "low_survivability_warning": warning,
        }
    )


def apply_survivability_to_severity(
    severity: PrioritySeverity,
    survivability: OpportunitySurvivability,
) -> PrioritySeverity:
    if not survivability.low_survivability_warning:
        return severity
    return _SEVERITY_DOWNGRADE[severity]


def probability_at_expected_external_latency(
    survivability: OpportunitySurvivability,
) -> Decimal | None:
    if survivability.survival_probability_at_required_latency is not None:
        return survivability.survival_probability_at_required_latency
    latency = survivability.expected_external_confirmation_latency_seconds
    if latency is None:
        return None
    return survivability.probability_for_horizon(latency)
