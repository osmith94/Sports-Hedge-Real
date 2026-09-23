from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal

from sports_hedge.application.complete_set import SOLVER_MODEL_GENERALIZED
from sports_hedge.arbitrage.min_net_threshold import OUTRIGHT_MIN_NET_EDGE_UNCONFIGURED
from sports_hedge.arbitrage.watchlist.models import (
    OpportunityClassification,
    OpportunityStatus,
    WatchLeg,
    WatchObservation,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import VenueCostSnapshot
from sports_hedge.fees.effective import CostRuleError, apply_venue_costs
from sports_hedge.matching.paper_assumed import PAPER_NONBLOCKING_REJECTION_REASONS

EDGE_QUANT = Decimal("0.00000001")
PP_QUANT = Decimal("0.0001")
PERCENTAGE_POINTS = Decimal("100")
# Operator-facing HOT proximity band. Not a second threshold setting: this is
# the same 0.50pp near-arb distance already used by watchlist APPROACHING.
NET_PROXIMITY_BAND_PP = Decimal("0.50")
MOVED_BELOW_MIN_NET_ARB = "moved_below_min_net_arb"

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
# Quote age is an execution/paper-entry gate, not an economic radar status.
FRESHNESS_NONBLOCKING_REASONS = frozenset({"stale_quote", "unknown_quote_age"})
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
    `current_net_edge` is post-cost net ROI. `trigger_net_edge` is operator Min Net Arb.
    """

    return quantized_pp((trigger_net_edge - current_net_edge) * PERCENTAGE_POINTS)


def qualifies_min_net_arb(current_net_edge: Decimal, trigger_net_edge: Decimal | None) -> bool:
    """True when post-cost net ROI meets the configured Min Net Arb trigger.

    An unconfigured trigger fails closed and never qualifies.
    """

    if trigger_net_edge is None:
        return False
    return current_net_edge >= trigger_net_edge


def is_net_proximity_hot(current_net_edge: Decimal, trigger_net_edge: Decimal | None) -> bool:
    """Economically HOT, not yet a fill: below trigger but within 0.50pp.

    Uses existing `distance_to_trigger_pp`. Never treats gross edge or a hard-coded
    zero as the trigger. Unconfigured trigger is not proximity.
    """

    if trigger_net_edge is None:
        return False
    if qualifies_min_net_arb(current_net_edge, trigger_net_edge):
        return False
    distance = distance_to_trigger_pp(current_net_edge, trigger_net_edge)
    return Decimal("0") < distance <= NET_PROXIMITY_BAND_PP


def net_proximity_reason_label(distance_pp: Decimal) -> str:
    """Operator-facing HOT reason, e.g. `NET PROXIMITY · 0.05pp TO TRIGGER`."""

    quantized = quantized_pp(distance_pp)
    formatted = format(quantized, "f").rstrip("0").rstrip(".")
    if "." not in formatted:
        formatted = f"{formatted}.00"
    elif len(formatted.split(".", 1)[1]) == 1:
        formatted = f"{formatted}0"
    return f"NET PROXIMITY · {formatted}pp TO TRIGGER"


def evaluate_post_trigger_min_net_arb(
    *,
    bound_net_edge: Decimal,
    arrival_net_edge: Decimal,
    trigger_net_edge: Decimal,
) -> str | None:
    """Once a bound decision already meets Min Net Arb, only arrival below it blocks.

    Favourable movement, or movement that remains >= the same configured trigger,
    is allowed. Returns `moved_below_min_net_arb` when simulated arrival net
    economics fall below that trigger. Does not inspect quote age or execution risk.
    """

    if not qualifies_min_net_arb(bound_net_edge, trigger_net_edge):
        raise ValueError(
            "post-trigger min-net tolerance requires a bound decision at or above Min Net Arb"
        )
    if qualifies_min_net_arb(arrival_net_edge, trigger_net_edge):
        return None
    return MOVED_BELOW_MIN_NET_ARB


def complete_set_roi_from_decimal_odds(odds: Sequence[Decimal]) -> Decimal | None:
    """Complete-set ROI from decimal odds. This is not post-cost net."""

    if not odds:
        return None
    implied = Decimal("0")
    for value in odds:
        if value <= 0:
            return None
        implied += Decimal("1") / value
    if implied <= 0:
        return None
    return net_edge_from_implied_sum(implied)


def arrival_net_edge_after_venue_costs(
    *,
    arrival_legs: Sequence[tuple[VenueName, str | None, Decimal]],
    venue_costs: Sequence[VenueCostSnapshot],
    as_of: datetime | None = None,
) -> Decimal | None:
    """Post-cost complete-set net ROI from arrival gross odds.

    Applies the same per-quote `apply_venue_costs` model as paper scan / depth.
    Missing or inapplicable costs fail closed (None). Never returns odds-only ROI.
    """

    if not arrival_legs:
        return None
    net_odds: list[Decimal] = []
    for venue, source_market_id, gross_odds in arrival_legs:
        if gross_odds <= 1:
            return None
        cost = _cost_snapshot_for_arrival(venue_costs, venue, source_market_id)
        if cost is None:
            return None
        try:
            economics = apply_venue_costs(
                cost,
                gross_decimal_odds=gross_odds,
                require_gbp=False,
                as_of=as_of,
            )
        except CostRuleError:
            return None
        net_odds.append(economics.net_decimal_equivalent)
    return complete_set_roi_from_decimal_odds(net_odds)


def _cost_snapshot_for_arrival(
    venue_costs: Sequence[VenueCostSnapshot],
    venue: VenueName,
    source_market_id: str | None,
) -> VenueCostSnapshot | None:
    matches = [item for item in venue_costs if item.venue is venue]
    if not matches:
        return None
    if len(matches) == 1:
        return matches[0]
    if source_market_id is None:
        return None
    by_market = [item for item in matches if item.source_market_id == source_market_id]
    if len(by_market) == 1:
        return by_market[0]
    return None


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


LIFECYCLE_STATUSES_PROTECTED_FROM_OBSERVATION = frozenset(
    {
        OpportunityStatus.PAPER_FILLING,
        OpportunityStatus.PARTIAL,
        OpportunityStatus.FILLED,
        OpportunityStatus.CLOSED,
        OpportunityStatus.EXPIRED,
    }
)


def quote_is_execution_fresh(quote_age_ms: int | None, max_quote_age_ms: int) -> bool:
    """True when the exact quote snapshot is fresh enough to paper-fill."""

    if max_quote_age_ms < 0:
        raise ValueError("max_quote_age_ms must be non-negative")
    if quote_age_ms is None:
        return False
    return quote_age_ms < max_quote_age_ms


def classify_status(
    observation: WatchObservation,
    *,
    approaching_band_pp: Decimal,
    max_quote_age_ms: int,
    previous: OpportunityStatus | None = None,
) -> tuple[OpportunityStatus, list[str]]:
    if max_quote_age_ms < 0:
        raise ValueError("max_quote_age_ms must be non-negative")
    if previous in LIFECYCLE_STATUSES_PROTECTED_FROM_OBSERVATION:
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
    if observation.current_net_edge is None:
        rejections.append("missing_net_edge")
        return OpportunityStatus.REJECTED, _dedupe([*reasons, *rejections])
    if (
        observation.solver_model != SOLVER_MODEL_GENERALIZED
        and observation.implied_probability_sum is None
    ):
        rejections.append("missing_net_edge")
        return OpportunityStatus.REJECTED, _dedupe([*reasons, *rejections])
    hard_depth = [reason for reason in depth if reason in HARD_DEPTH_REASONS]
    if hard_depth:
        return OpportunityStatus.REJECTED, _dedupe([*reasons, *hard_depth])
    if "execution_risk_above_threshold" in reasons:
        return OpportunityStatus.REJECTED, _dedupe(reasons)

    # Paper-admission audit labels (paper_assumed_equivalent, etc.) do not
    # block LIVE_PAPER capture. They must not leftover-REJECT a scan-eligible
    # Matchbook/Kalshi decision before persist_triggered_chain runs.
    # Quote age is recorded on the observation and gated at paper entry; it
    # must not overwrite economic/radar status.
    leftover = [
        reason
        for reason in reasons
        if reason not in NEAR_ELIGIBLE_REASONS
        and reason not in PAPER_NONBLOCKING_REJECTION_REASONS
        and reason not in FRESHNESS_NONBLOCKING_REASONS
    ]
    if leftover:
        return OpportunityStatus.REJECTED, _dedupe(reasons)
    if (
        observation.trigger_net_edge is None
        or OUTRIGHT_MIN_NET_EDGE_UNCONFIGURED in reasons
    ):
        return OpportunityStatus.REJECTED, _dedupe(
            [*reasons, OUTRIGHT_MIN_NET_EDGE_UNCONFIGURED]
        )

    # Economic trigger crossing is independent of whether the exact quote is
    # currently execution-fresh. Paper fill still fail-closes on freshness.
    triggered = (
        observation.solver_is_arbitrage
        and observation.current_net_edge >= observation.trigger_net_edge
    )
    if triggered:
        return OpportunityStatus.TRIGGERED, _dedupe(reasons)

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
