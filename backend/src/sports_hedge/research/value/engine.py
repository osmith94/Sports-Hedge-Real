from __future__ import annotations

from datetime import datetime
from decimal import Decimal

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
    quote_net_odds,
    raw_implied_probability,
)
from sports_hedge.research.value.quotes import (
    has_known_costs,
    has_sufficient_depth,
    is_fresh,
    quote_age_seconds,
    select_best_quote,
    select_reference_quote,
    semantically_eligible,
)
from sports_hedge.research.value.scoring import value_signal_score


class ScenarioValueEngine:
    """Rank scenario signals by odds-weighted analytical value.

    Isolated from the arbitrage solver. Positive VALUE can still lose.
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
            return self._rejected(
                evidence,
                ValueStatus.STALE_QUOTE,
                "stale_quote",
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

        costed = [quote for quote in liquid if has_known_costs(quote)]
        if not costed:
            return self._rejected(
                evidence,
                ValueStatus.MISSING_COSTS,
                "missing_costs",
                reference=reference,
            )

        best = select_best_quote(costed)
        net_odds = quote_net_odds(best)
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
        )
