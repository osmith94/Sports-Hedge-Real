from __future__ import annotations

from pydantic import BaseModel, Field

from sports_hedge.domain.football import CanonicalMarket
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.learned_rules import (
    MappingProvenance,
    participant_identity_preserved,
)
from sports_hedge.matching.paper_assumed import (
    OWNER_APPROVED_PAPER_EQUIVALENCE_REASON,
    PAPER_ASSUMED_REASON,
    paper_assumed_match_reasons,
)
from sports_hedge.matching.approved_register import (
    NOT_REGISTERED_REASON,
    REGISTER_ADMITTED_REASON,
    register_key_reason,
    registered_canonical_key,
    structural_mismatch_reasons,
)
from sports_hedge.matching.ordinary_1x2 import (
    GAMEWIN_ORDINARY_1X2_AUDIT_REASON,
    UNKNOWN_SETTLEMENT_ALLOWED_REASON,
    allow_unknown_settlement_for_ordinary_1x2,
    kalshi_gamewin_scope_unavailable,
    ordinary_1x2_match_reasons,
)


class MarketMatchResult(BaseModel):
    matched: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str]
    provenance: MappingProvenance = Field(default_factory=MappingProvenance)


class MarketMatcher:
    """Runtime PAPER matcher. Fixture identity first, then the register.

    After canonical fixture identity is established, the Approved Match
    Register is the sole PAPER market-equivalence authority. Same registered
    canonical key plus required structural parameters is admitted. Settlement
    fingerprints, mapping confidence, learned market labels, and mapping
    review are not runtime equivalence permission.
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
        from sports_hedge.nba.detect import is_nba_canonical_event
        from sports_hedge.nba.teams import is_canonical_nba_team
        from sports_hedge.ncaab.detect import is_ncaab_canonical_event
        from sports_hedge.ncaab.teams import is_canonical_ncaab_team
        from sports_hedge.nfl.detect import is_nfl_canonical_event
        from sports_hedge.nfl.teams import is_canonical_nfl_team

        if is_nfl_canonical_event(left.event) or is_nfl_canonical_event(right.event):
            if not (
                is_canonical_nfl_team(left.event.home_team)
                and is_canonical_nfl_team(left.event.away_team)
                and left.event.home_team == right.event.home_team
                and left.event.away_team == right.event.away_team
            ):
                return MarketMatchResult(
                    matched=False,
                    confidence=event_result.confidence,
                    reasons=["event_mismatch", "participant_identity_unproven", *event_result.reasons],
                    provenance=event_result.provenance,
                )
        elif is_ncaab_canonical_event(left.event) or is_ncaab_canonical_event(right.event):
            if not (
                is_canonical_ncaab_team(left.event.home_team)
                and is_canonical_ncaab_team(left.event.away_team)
                and left.event.home_team == right.event.home_team
                and left.event.away_team == right.event.away_team
            ):
                return MarketMatchResult(
                    matched=False,
                    confidence=event_result.confidence,
                    reasons=["event_mismatch", "participant_identity_unproven", *event_result.reasons],
                    provenance=event_result.provenance,
                )
        elif is_nba_canonical_event(left.event) or is_nba_canonical_event(right.event):
            if not (
                is_canonical_nba_team(left.event.home_team)
                and is_canonical_nba_team(left.event.away_team)
                and left.event.home_team == right.event.home_team
                and left.event.away_team == right.event.away_team
            ):
                return MarketMatchResult(
                    matched=False,
                    confidence=event_result.confidence,
                    reasons=["event_mismatch", "participant_identity_unproven", *event_result.reasons],
                    provenance=event_result.provenance,
                )
        elif not (
            participant_identity_preserved(left.event.home_team, right.event.home_team)
            and participant_identity_preserved(left.event.away_team, right.event.away_team)
        ):
            return MarketMatchResult(
                matched=False,
                confidence=event_result.confidence,
                reasons=["event_mismatch", "participant_identity_unproven", *event_result.reasons],
                provenance=event_result.provenance,
            )

        key = registered_canonical_key(left, right)
        if key is not None:
            match_reasons = list(event_result.reasons)
            match_reasons.extend(paper_assumed_match_reasons())
            if REGISTER_ADMITTED_REASON not in match_reasons:
                match_reasons.append(REGISTER_ADMITTED_REASON)
            key_reason = register_key_reason(key)
            if key_reason not in match_reasons:
                match_reasons.append(key_reason)
            if PAPER_ASSUMED_REASON not in match_reasons:
                match_reasons.append(PAPER_ASSUMED_REASON)
            if OWNER_APPROVED_PAPER_EQUIVALENCE_REASON not in match_reasons:
                match_reasons.append(OWNER_APPROVED_PAPER_EQUIVALENCE_REASON)
            from sports_hedge.nba.settlement import nba_market_uses_paper_caveat, nba_paper_audit_reasons
            from sports_hedge.ncaab.settlement import ncaab_market_uses_paper_caveat, ncaab_paper_audit_reasons
            from sports_hedge.nfl.settlement import nfl_market_uses_paper_caveat, nfl_paper_audit_reasons

            if nfl_market_uses_paper_caveat(left) or nfl_market_uses_paper_caveat(right):
                for reason in nfl_paper_audit_reasons():
                    if reason not in match_reasons:
                        match_reasons.append(reason)
            if nba_market_uses_paper_caveat(left) or nba_market_uses_paper_caveat(right):
                for reason in nba_paper_audit_reasons():
                    if reason not in match_reasons:
                        match_reasons.append(reason)
            if ncaab_market_uses_paper_caveat(left) or ncaab_market_uses_paper_caveat(right):
                for reason in ncaab_paper_audit_reasons():
                    if reason not in match_reasons:
                        match_reasons.append(reason)
            if allow_unknown_settlement_for_ordinary_1x2(left, right):
                for reason in ordinary_1x2_match_reasons():
                    if reason not in match_reasons:
                        match_reasons.append(reason)
                if kalshi_gamewin_scope_unavailable(left) or kalshi_gamewin_scope_unavailable(
                    right
                ):
                    if GAMEWIN_ORDINARY_1X2_AUDIT_REASON not in match_reasons:
                        match_reasons.append(GAMEWIN_ORDINARY_1X2_AUDIT_REASON)
                    if UNKNOWN_SETTLEMENT_ALLOWED_REASON not in match_reasons:
                        match_reasons.append(UNKNOWN_SETTLEMENT_ALLOWED_REASON)
            return MarketMatchResult(
                matched=True,
                confidence=event_result.confidence,
                reasons=match_reasons,
                provenance=event_result.provenance,
            )

        reasons = structural_mismatch_reasons(left, right)
        if not reasons:
            reasons = [NOT_REGISTERED_REASON]
        return MarketMatchResult(
            matched=False,
            confidence=event_result.confidence,
            reasons=reasons,
            provenance=event_result.provenance,
        )
