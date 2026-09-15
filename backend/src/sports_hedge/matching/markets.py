from __future__ import annotations

from pydantic import BaseModel, Field

from sports_hedge.domain.football import CanonicalMarket
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.learned_rules import (
    MappingProvenance,
    MappingRuleType,
    economic_mismatch_reasons,
)


class MarketMatchResult(BaseModel):
    matched: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str]
    provenance: MappingProvenance = Field(default_factory=MappingProvenance)


class MarketMatcher:
    """Strict economic-equivalence matcher.

    Event labels may be fuzzy, but market economics are not. Settlement semantics,
    family, period and line must match exactly before a pair is considered tradable.
    Learned naming rules may help event identity; they cannot override settlement,
    period, line, family or outcome-model mismatch.
    """

    def __init__(self, event_matcher: EventMatcher | None = None) -> None:
        self.event_matcher = event_matcher or EventMatcher()

    def match(self, left: CanonicalMarket, right: CanonicalMarket) -> MarketMatchResult:
        event_result = self.event_matcher.match(
            left.event,
            right.event,
            left_market=left,
            right_market=right,
        )
        if not event_result.matched:
            return MarketMatchResult(
                matched=False,
                confidence=event_result.confidence,
                reasons=["event_mismatch", *event_result.reasons],
                provenance=event_result.provenance,
            )

        reasons = economic_mismatch_reasons(left, right)
        if reasons:
            return MarketMatchResult(
                matched=False,
                confidence=min(event_result.confidence, left.confidence, right.confidence),
                reasons=reasons,
                provenance=event_result.provenance,
            )

        match_reasons = list(event_result.reasons)
        if (
            event_result.provenance.rule_type
            is MappingRuleType.VENUE_MARKET_LABEL_CONVENTION
        ):
            match_reasons.append("operator_verified_market_label")
        return MarketMatchResult(
            matched=True,
            confidence=min(event_result.confidence, left.confidence, right.confidence),
            reasons=match_reasons,
            provenance=event_result.provenance,
        )
