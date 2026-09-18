from __future__ import annotations

from pydantic import BaseModel, Field

from sports_hedge.domain.football import CanonicalMarket
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.learned_rules import (
    MappingProvenance,
    MappingRuleType,
    economic_mismatch_reasons,
    participant_identity_preserved,
)
from sports_hedge.matching.ordinary_1x2 import (
    PAPER_ASSUMED_1X2_REASON,
    allow_unknown_settlement_for_ordinary_1x2,
    ordinary_1x2_match_reasons,
)
from sports_hedge.matching.paper_assumed import (
    OWNER_APPROVED_PAPER_EQUIVALENCE_REASON,
    allow_unknown_settlement_for_paper_assumed,
    paper_assumed_locked_family,
    paper_assumed_match_reasons,
)


class MarketMatchResult(BaseModel):
    matched: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str]
    provenance: MappingProvenance = Field(default_factory=MappingProvenance)


class MarketMatcher:
    """Strict economic-equivalence matcher.

    Event labels may be fuzzy for discovery, but paper-eligible identity is not.
    Family, period, line and outcome space must match. Proven settlement
    contradictions fail closed. Matchbook↔Kalshi locked Phase-1 families
    (MATCH_RESULT / BTTS / exact-line TOTAL / FTTS) may match in PAPER mode
    when canonical identity holds and Kalshi settlement is unknown or
    fair-price-poisoned and not a proven extra-time/penalties contradiction.
    Learned naming rules may help aliases; they cannot override period, line,
    family, outcome-model mismatch, fixture participant identity, or a proven
    settlement contradiction.
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
        if "competition_fuzzy" in event_result.reasons:
            return MarketMatchResult(
                matched=False,
                confidence=event_result.confidence,
                reasons=[
                    "event_mismatch",
                    "competition_identity_unproven",
                    *event_result.reasons,
                ],
                provenance=event_result.provenance,
            )
        if not (
            participant_identity_preserved(left.event.home_team, right.event.home_team)
            and participant_identity_preserved(left.event.away_team, right.event.away_team)
        ):
            return MarketMatchResult(
                matched=False,
                confidence=event_result.confidence,
                reasons=["event_mismatch", "participant_identity_unproven", *event_result.reasons],
                provenance=event_result.provenance,
            )

        reasons = economic_mismatch_reasons(left, right)
        unknown_allowed = allow_unknown_settlement_for_paper_assumed(left, right)
        paper_assumed = paper_assumed_locked_family(left, right)
        if reasons:
            return MarketMatchResult(
                matched=False,
                confidence=min(event_result.confidence, left.confidence, right.confidence),
                reasons=reasons,
                provenance=event_result.provenance,
            )

        match_reasons = list(event_result.reasons)
        if unknown_allowed and (
            not left.settlement.is_economically_complete()
            or not right.settlement.is_economically_complete()
        ):
            if allow_unknown_settlement_for_ordinary_1x2(left, right):
                match_reasons.extend(ordinary_1x2_match_reasons())
        if paper_assumed:
            match_reasons.extend(paper_assumed_match_reasons())
            if PAPER_ASSUMED_1X2_REASON not in match_reasons:
                match_reasons.append(PAPER_ASSUMED_1X2_REASON)
            if OWNER_APPROVED_PAPER_EQUIVALENCE_REASON not in match_reasons:
                match_reasons.append(OWNER_APPROVED_PAPER_EQUIVALENCE_REASON)
        if (
            event_result.provenance.rule_type
            is MappingRuleType.VENUE_MARKET_LABEL_CONVENTION
        ):
            match_reasons.append("operator_verified_market_label")
        confidence_parts = [event_result.confidence]
        for market in (left, right):
            if unknown_allowed and not market.settlement.is_economically_complete():
                continue
            confidence_parts.append(market.confidence)
        return MarketMatchResult(
            matched=True,
            confidence=min(confidence_parts),
            reasons=match_reasons,
            provenance=event_result.provenance,
        )
