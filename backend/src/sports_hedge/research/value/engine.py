from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from sports_hedge.fees.cost import require_aware_utc
from sports_hedge.research.value.contracts import (
    CanonicalProposition,
    ScenarioEvidence,
    ScenarioValueResult,
    ValueEnginePolicy,
    ValueStatus,
    VenueQuote,
)
from sports_hedge.research.value.economics import (
    cost_adjusted_implied_probability,
    expected_profit_per_unit,
    probability_edge_percentage_points,
    quote_cost_rejection,
    quote_effective_economics,
    raw_implied_probability,
)
from sports_hedge.research.value.quotes import (
    has_known_costs,
    has_sufficient_depth,
    is_fresh,
    quote_age_seconds,
    quote_timing_rejection,
    select_best_quote,
    select_reference_quote,
    semantically_eligible,
)
from sports_hedge.research.value.scoring import value_signal_score


def _inspectable_cost_fields(quote: VenueQuote | None) -> dict[str, object]:
    if quote is None:
        return {}
    cost = quote.cost
    return {
        "fee_basis": cost.fee_basis.value,
        "fee_scope": cost.fee_scope.value,
        "fee_snapshot_id": cost.snapshot_id,
        "cost_known_status": cost.known_status.value,
        "cost_source": cost.source,
        "cost_currency": cost.currency,
        "cost_captured_at": cost.captured_at,
        "cost_effective_from": cost.effective_from,
        "order_role": cost.order_role.value,
        "action": quote.action.value,
    }


class ScenarioValueEngine:
    """Rank scenario signals by odds-weighted analytical value.

    Isolated from the arbitrage solver. Positive VALUE is directional expected
    value and can still lose; it is not a guaranteed settlement-state payoff.
    Per-quote effective price only; market-net and period-netted commissions
    are unsupported.
    """

    def __init__(self, policy: ValueEnginePolicy | None = None) -> None:
        self.policy = policy or ValueEnginePolicy()

    def evaluate(
        self,
        proposition: CanonicalProposition,
        evidence: ScenarioEvidence,
        quotes: list[VenueQuote],
        *,
        as_of: datetime,
    ) -> ScenarioValueResult:
        require_aware_utc(as_of, "as_of")
        eligible, semantic_reason = semantically_eligible(proposition, quotes)
        if not eligible:
            return self._rejected(
                evidence,
                ValueStatus.SEMANTICS_MISMATCH,
                semantic_reason or "settlement_mismatch",
            )

        reference = select_reference_quote(
            eligible,
            reference_venue=self.policy.reference_venue,
        )

        fresh = [
            quote for quote in eligible if is_fresh(quote, as_of, self.policy.max_quote_age_seconds)
        ]
        if not fresh:
            reasons = {quote_timing_rejection(quote, as_of) for quote in eligible}
            reason = "future_quote" if reasons == {"future_quote"} else "stale_quote"
            return self._rejected(
                evidence,
                ValueStatus.STALE_QUOTE,
                reason,
                reference=reference,
            )

        liquid = [
            quote for quote in fresh if has_sufficient_depth(quote, self.policy.min_available_depth)
        ]
        if not liquid:
            return self._rejected(
                evidence,
                ValueStatus.INSUFFICIENT_LIQUIDITY,
                "insufficient_liquidity",
                reference=reference,
            )

        costed = [quote for quote in liquid if has_known_costs(quote, as_of)]
        if not costed:
            reasons = {quote_cost_rejection(quote, as_of) for quote in liquid}
            reason = next(iter(reasons)) if len(reasons) == 1 else "missing_costs"
            return self._rejected(
                evidence,
                ValueStatus.MISSING_COSTS,
                reason or "missing_costs",
                reference=reference,
                cost_quote=liquid[0],
            )

        best = select_best_quote(costed)
        economics = quote_effective_economics(best, as_of=as_of)
        net_odds = economics.net_decimal_equivalent
        raw_p = raw_implied_probability(best.displayed_decimal_odds)
        cost_p = cost_adjusted_implied_probability(net_odds)
        point_ev = expected_profit_per_unit(evidence.model_probability, net_odds)
        conservative_p = evidence.conservative_probability()
        conservative_ev = expected_profit_per_unit(conservative_p, net_odds)
        edge_pp = probability_edge_percentage_points(evidence.model_probability, cost_p)
        age = quote_age_seconds(best, as_of)
        depth = best.available_depth
        assert depth is not None

        score, components = value_signal_score(
            probability_edge_pp=edge_pp,
            sample_size=evidence.sample_size,
            min_sample_size=self.policy.min_sample_size,
            confidence=evidence.confidence,
            stability=evidence.stability,
            regime_relevance=evidence.regime_relevance,
            data_quality=evidence.data_quality,
            quote_age_seconds=age,
            max_quote_age_seconds=self.policy.max_quote_age_seconds,
            available_depth=depth,
            min_available_depth=self.policy.min_available_depth,
        )

        status, reason = self._classify(evidence, conservative_ev)
        return ScenarioValueResult(
            status=status,
            model_probability=evidence.model_probability,
            conservative_model_probability=conservative_p,
            src=evidence.src,
            sample_size=evidence.sample_size,
            confidence=evidence.confidence,
            stability=evidence.stability,
            data_quality=evidence.data_quality,
            regime_relevance=evidence.regime_relevance,
            raw_market_implied_probability=raw_p,
            cost_adjusted_market_probability=cost_p,
            probability_edge_pp=edge_pp,
            expected_profit_per_unit=point_ev,
            expected_roi=point_ev,
            best_price=best.displayed_decimal_odds,
            best_net_price=net_odds,
            best_venue=best.venue,
            reference_price=None if reference is None else reference.displayed_decimal_odds,
            reference_venue=None if reference is None else reference.venue,
            quote_age_seconds=age,
            available_depth=depth,
            value_signal_score=score,
            score_components=components,
            rejection_reason=reason,
            **_inspectable_cost_fields(best),
        )

    def _classify(
        self,
        evidence: ScenarioEvidence,
        conservative_ev: Decimal,
    ) -> tuple[ValueStatus, str | None]:
        if evidence.sample_size < self.policy.min_sample_size:
            return ValueStatus.INSUFFICIENT_EVIDENCE, "low_sample_size"
        if evidence.confidence < self.policy.min_confidence:
            return ValueStatus.INSUFFICIENT_EVIDENCE, "low_confidence"
        if evidence.stability < self.policy.min_stability:
            return ValueStatus.INSUFFICIENT_EVIDENCE, "low_stability"
        if evidence.regime_relevance < self.policy.min_regime_relevance:
            return ValueStatus.INSUFFICIENT_EVIDENCE, "low_regime_relevance"
        width = evidence.interval_width()
        if width is not None and width > self.policy.max_probability_interval_width:
            return ValueStatus.INSUFFICIENT_EVIDENCE, "uncertainty_too_wide"
        if conservative_ev > 0:
            return ValueStatus.VALUE, None
        return ValueStatus.NO_VALUE, "no_positive_edge"

    def _rejected(
        self,
        evidence: ScenarioEvidence,
        status: ValueStatus,
        reason: str,
        *,
        reference: VenueQuote | None = None,
        cost_quote: VenueQuote | None = None,
    ) -> ScenarioValueResult:
        return ScenarioValueResult(
            status=status,
            model_probability=evidence.model_probability,
            conservative_model_probability=evidence.conservative_probability(),
            src=evidence.src,
            sample_size=evidence.sample_size,
            confidence=evidence.confidence,
            stability=evidence.stability,
            data_quality=evidence.data_quality,
            regime_relevance=evidence.regime_relevance,
            reference_price=None if reference is None else reference.displayed_decimal_odds,
            reference_venue=None if reference is None else reference.venue,
            rejection_reason=reason,
            **_inspectable_cost_fields(cost_quote or reference),
        )
