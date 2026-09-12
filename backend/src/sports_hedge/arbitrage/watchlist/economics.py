from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_HALF_EVEN, Decimal

from sports_hedge.arbitrage.watchlist.models import (
    OpportunityClassification,
    OpportunityStatus,
    WatchLeg,
    WatchObservation,
)

EDGE_QUANT = Decimal("0.00000001")
PP_QUANT = Decimal("0.0001")
PERCENTAGE_POINTS = Decimal("100")

SEMANTIC_REASONS = (
    "market_not_equivalent",
    "event_mismatch",
    "settlement_mismatch",
    "incomplete_settlement",
    "unknown_settlement_scope",
    "noncanonical_outcome_space",
    "mapping_confidence_below_threshold",
    "same_venue_pair",
)
DEPTH_REASONS = (
    "missing_executable_outcome_depth",
    "incomplete_outcome_set",
    "no_positive_edge",
    "no_arbitrage",
)
WATCH_ONLY_REASONS = ("net_edge_below_threshold",)
NEAR_ELIGIBLE_REASONS = (
    "net_edge_below_threshold",
    "no_positive_edge",
    "no_arbitrage",
)
COST_CLOCK_REASONS = (
    "future_fee_snapshot",
    "future_fx_snapshot",
    "invalid_fee_snapshot_time",
    "invalid_fx_snapshot_time",
    "unsupported_fee_basis",
    "unsupported_fee_scope",
    "unknown_costs",
    "unknown_fee_scope",
    "legacy_fee_snapshot_not_cost_truth",
    "unconverted_currency",
    "unknown_order_role",
    "unsupported_action",
    "unsupported_state_payoff_fee_basis",
)


def quantized_edge(value: Decimal) -> Decimal:
    return value.quantize(EDGE_QUANT, rounding=ROUND_HALF_EVEN)


def quantized_pp(value: Decimal) -> Decimal:
    return value.quantize(PP_QUANT, rounding=ROUND_HALF_EVEN)


def net_edge_from_implied_sum(implied_probability_sum: Decimal) -> Decimal:
    if implied_probability_sum <= 0:
        raise ValueError("implied_probability_sum must be positive")
    return quantized_edge(Decimal("1") / implied_probability_sum - Decimal("1"))


def gross_edge_from_quotes(quotes: Sequence[object]) -> Decimal | None:
    """Gross complete-set edge from selected quotes. Missing odds fail closed."""

    if not quotes:
        return None
    implied = Decimal("0")
    for quote in quotes:
        gross = getattr(quote, "gross_weighted_odds", None)
        if gross is None or gross <= 0:
            return None
        implied += Decimal("1") / gross
    if implied <= 0:
        return None
    return quantized_edge(Decimal("1") / implied - Decimal("1"))


def distance_to_trigger_pp(current_net_edge: Decimal, trigger_net_edge: Decimal) -> Decimal:
    """Return how many percentage points the edge sits below (or above) the trigger.

    Example: trigger 1.00% (0.01) and current 0.80% (0.008) → 0.20pp.
    """

    return quantized_pp((trigger_net_edge - current_net_edge) * PERCENTAGE_POINTS)


def missing_cost_reasons(reasons: list[str]) -> list[str]:
    return [
        reason
        for reason in reasons
        if reason.startswith("missing_fee_snapshot:")
        or reason.startswith("missing_venue_cost:")
        or reason.startswith("missing_fx_rate:")
        or reason.startswith("unknown_venue_currency:")
        or reason.startswith("unsupported_fee_")
        or reason.startswith("unknown_costs")
        or reason in COST_CLOCK_REASONS
    ]


def semantic_reasons(reasons: list[str]) -> list[str]:
    return [reason for reason in reasons if reason in SEMANTIC_REASONS]


def depth_reasons(reasons: list[str]) -> list[str]:
    return [reason for reason in reasons if reason in DEPTH_REASONS]


HARD_DEPTH_REASONS = (
    "missing_executable_outcome_depth",
    "incomplete_outcome_set",
)


def capital_required_gbp(legs: list[WatchLeg]) -> Decimal | None:
    """Sum only already-converted GBP stakes. Never add mixed native currencies."""

    gbp_stakes = [leg.gbp_stake for leg in legs if leg.gbp_stake is not None]
    if not gbp_stakes:
        return None
    if any(leg.gbp_stake is None for leg in legs):
        return None
    return sum(gbp_stakes, Decimal("0"))


def native_amounts_are_commingled(legs: list[WatchLeg]) -> bool:
    currencies = {leg.currency for leg in legs if leg.native_stake is not None}
    return len(currencies) > 1


def classify_status(
    observation: WatchObservation,
    *,
    approaching_band_pp: Decimal,
    max_quote_age_ms: int,
    previous: OpportunityStatus | None = None,
) -> tuple[OpportunityStatus, list[str]]:
    fill_statuses = {
        OpportunityStatus.PAPER_FILLING,
        OpportunityStatus.PARTIAL,
        OpportunityStatus.FILLED,
        OpportunityStatus.CLOSED,
    }
    if previous in fill_statuses and previous != OpportunityStatus.PAPER_FILLING:
        return previous, list(observation.rejection_reasons)

    rejections: list[str] = []
    reasons = list(observation.rejection_reasons)
    costs = missing_cost_reasons(reasons)
    semantics = semantic_reasons(reasons)
    depth = depth_reasons(reasons)

    if costs:
        rejections.extend(costs)
        return OpportunityStatus.REJECTED, _dedupe([*reasons, *rejections])
    if semantics:
        rejections.extend(semantics)
        return OpportunityStatus.REJECTED, _dedupe([*reasons, *rejections])
    if observation.quote_age_ms is None:
        rejections.append("unknown_quote_age")
        return OpportunityStatus.REJECTED, _dedupe([*reasons, *rejections])
    if observation.quote_age_ms >= max_quote_age_ms:
        rejections.append("stale_quote")
        return OpportunityStatus.REJECTED, _dedupe([*reasons, *rejections])
    if observation.current_net_edge is None or observation.implied_probability_sum is None:
        rejections.append("missing_net_edge")
        return OpportunityStatus.REJECTED, _dedupe([*reasons, *rejections])
    hard_depth = [reason for reason in depth if reason in HARD_DEPTH_REASONS]
    if hard_depth:
        return OpportunityStatus.REJECTED, _dedupe([*reasons, *hard_depth])
    if "execution_risk_above_threshold" in reasons:
        return OpportunityStatus.REJECTED, _dedupe(reasons)

    leftover = [reason for reason in reasons if reason not in NEAR_ELIGIBLE_REASONS]
    if leftover:
        return OpportunityStatus.REJECTED, _dedupe(reasons)

    triggered = (
        observation.eligible_for_paper_simulation
        and observation.solver_is_arbitrage
        and observation.current_net_edge >= observation.trigger_net_edge
    )
    if triggered:
        return OpportunityStatus.TRIGGERED, _dedupe(reasons)

    if previous == OpportunityStatus.PAPER_FILLING:
        return OpportunityStatus.PAPER_FILLING, _dedupe(reasons)

    # Near-arb is only for candidates that passed every non-economic gate and
    # remain strictly below the configured trigger.
    if observation.current_net_edge >= observation.trigger_net_edge:
        return OpportunityStatus.REJECTED, _dedupe(reasons)

    distance = distance_to_trigger_pp(observation.current_net_edge, observation.trigger_net_edge)
    if Decimal("0") < distance <= approaching_band_pp:
        return OpportunityStatus.APPROACHING, _dedupe(reasons)
    return OpportunityStatus.WATCHING, _dedupe(reasons)


def classification_for(status: OpportunityStatus) -> OpportunityClassification:
    if status == OpportunityStatus.APPROACHING:
        return OpportunityClassification.NEAR_OPPORTUNITY
    if status == OpportunityStatus.WATCHING:
        return OpportunityClassification.WATCH_CANDIDATE
    if status == OpportunityStatus.TRIGGERED:
        return OpportunityClassification.TRIGGERED_OPPORTUNITY
    if status in {
        OpportunityStatus.PAPER_FILLING,
        OpportunityStatus.PARTIAL,
        OpportunityStatus.FILLED,
    }:
        return OpportunityClassification.PAPER_FILL
    if status == OpportunityStatus.CLOSED:
        return OpportunityClassification.CLOSED
    if status == OpportunityStatus.EXPIRED:
        return OpportunityClassification.EXPIRED
    return OpportunityClassification.REJECTED


def insufficiency_reasons(reasons: list[str]) -> list[str]:
    return [
        reason for reason in reasons if reason in WATCH_ONLY_REASONS or reason.startswith("net_")
    ]


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))
