from __future__ import annotations

from pydantic import BaseModel, Field

from sports_hedge.domain.football import CanonicalMarket
from sports_hedge.matching.events import EventMatcher


class MarketMatchResult(BaseModel):
    matched: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str]


class MarketMatcher:
    """Strict economic-equivalence matcher.

    Event labels may be fuzzy, but market economics are not. Settlement semantics,
    family, period and line must match exactly before a pair is considered tradable.
    """

    def __init__(self, event_matcher: EventMatcher | None = None) -> None:
        self.event_matcher = event_matcher or EventMatcher()

    def match(self, left: CanonicalMarket, right: CanonicalMarket) -> MarketMatchResult:
        event_result = self.event_matcher.match(left.event, right.event)
        if not event_result.matched:
            return MarketMatchResult(
                matched=False,
                confidence=event_result.confidence,
                reasons=["event_mismatch", *event_result.reasons],
            )

        reasons: list[str] = []
        if left.family != right.family:
            reasons.append("market_family_mismatch")
        if left.period != right.period:
            reasons.append("period_mismatch")
        if left.line != right.line:
            reasons.append("line_mismatch")
        left_complete = left.settlement.is_economically_complete()
        right_complete = right.settlement.is_economically_complete()
        if not left_complete or not right_complete:
            reasons.append("incomplete_settlement")
        elif left.settlement.deterministic_key() != right.settlement.deterministic_key():
            reasons.append("settlement_mismatch")

        left_outcomes = {runner.outcome for runner in left.runners}
        right_outcomes = {runner.outcome for runner in right.runners}
        if left_outcomes != right_outcomes:
            reasons.append("outcome_space_mismatch")

        if reasons:
            return MarketMatchResult(
                matched=False,
                confidence=min(event_result.confidence, left.confidence, right.confidence),
                reasons=reasons,
            )

        return MarketMatchResult(
            matched=True,
            confidence=min(event_result.confidence, left.confidence, right.confidence),
            reasons=event_result.reasons,
        )
