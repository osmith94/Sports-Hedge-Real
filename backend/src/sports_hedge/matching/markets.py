from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

from pydantic import BaseModel, Field

from sports_hedge.domain.football import CanonicalMarket
from sports_hedge.matching.approved_register import (
    NOT_REGISTERED_REASON,
    REGISTER_ADMITTED_REASON,
    register_key_reason,
    registered_canonical_key,
    structural_mismatch_reasons,
)
from sports_hedge.matching.events import EventMatcher, EventMatchResult
from sports_hedge.matching.learned_rules import (
    MappingProvenance,
    participant_identity_preserved,
)
from sports_hedge.matching.ordinary_1x2 import (
    GAMEWIN_ORDINARY_1X2_AUDIT_REASON,
    UNKNOWN_SETTLEMENT_ALLOWED_REASON,
    allow_unknown_settlement_for_ordinary_1x2,
    kalshi_gamewin_scope_unavailable,
    ordinary_1x2_match_reasons,
)
from sports_hedge.matching.assumed_settlement import (
    OWNER_APPROVED_PAPER_EQUIVALENCE_REASON,
    PAPER_ASSUMED_REASON,
    paper_assumed_match_reasons,
)


class MarketMatchResult(BaseModel):
    matched: bool
    confidence: float = Field(ge=0.0, le=1.0)
    reasons: list[str]
    provenance: MappingProvenance = Field(default_factory=MappingProvenance)
    # Set once by MarketMatcher from the Approved Match Register. Catalogue
    # admission reads this instead of resolving the register again.
    register_key: str | None = None


class _MatchMemo:
    """Fixture-local cache for one bulk comparison.

    Event identity does not depend on which market is being compared:
    learned team rules ignore market arguments. Full match results are
    identical for markets that share an event object and the same
    economic fingerprint. Callers receive a fresh reasons list.
    """

    def __init__(self) -> None:
        self.events: dict[tuple[int, int], EventMatchResult] = {}
        self.results: dict[tuple[object, ...], MarketMatchResult] = {}
        self.fingerprints: dict[int, tuple[object, ...]] = {}
        # Keep markets alive so an id reused after collection cannot
        # observe another market's fingerprint or event result.
        self._owners: list[CanonicalMarket] = []


_ACTIVE_MATCH_MEMO: ContextVar[_MatchMemo | None] = ContextVar(
    "sports_hedge_match_memo",
    default=None,
)


def economic_match_fingerprint(market: CanonicalMarket) -> tuple[object, ...]:
    """Fields that can change a ``MarketMatcher`` result besides the event object."""

    settlement = market.settlement
    line = None if market.line is None else format(market.line, "f")
    outcomes = tuple(sorted({runner.outcome.value for runner in market.runners}))
    return (
        market.source_venue,
        market.family,
        market.period,
        line,
        settlement.deterministic_key(),
        settlement.unknown_reason,
        outcomes,
    )


def _copy_match_result(result: MarketMatchResult) -> MarketMatchResult:
    """Fresh reasons and provenance so a caller cannot poison the cache."""

    provenance = result.provenance.model_copy(
        update={"applied_rule_ids": list(result.provenance.applied_rule_ids)}
    )
    return MarketMatchResult(
        matched=result.matched,
        confidence=result.confidence,
        reasons=list(result.reasons),
        provenance=provenance,
        register_key=result.register_key,
    )


def _memo_fingerprint(memo: _MatchMemo, market: CanonicalMarket) -> tuple[object, ...]:
    cached = memo.fingerprints.get(id(market))
    if cached is None:
        cached = economic_match_fingerprint(market)
        memo.fingerprints[id(market)] = cached
        memo._owners.append(market)
    return cached


@contextmanager
def memoize_market_matches() -> Iterator[None]:
    """Reuse event and economic match results for the current task.

    Nested uses share the outer cache. The cache dies when the outermost
    block exits, including across awaits in that task. Other tasks do not
    see it.
    """

    if _ACTIVE_MATCH_MEMO.get() is not None:
        yield
        return
    token = _ACTIVE_MATCH_MEMO.set(_MatchMemo())
    try:
        yield
    finally:
        _ACTIVE_MATCH_MEMO.reset(token)


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
        memo = _ACTIVE_MATCH_MEMO.get()
        # Only the stock matcher is proven independent of market arguments
        # beyond the economic fingerprint. Subclasses and custom event
        # matchers stay uncached so they cannot observe a borrowed result.
        if (
            memo is None
            or type(self) is not MarketMatcher
            or type(self.event_matcher) is not EventMatcher
        ):
            return self._match_uncached(left, right)
        key = (
            id(left.event),
            id(right.event),
            _memo_fingerprint(memo, left),
            _memo_fingerprint(memo, right),
        )
        cached = memo.results.get(key)
        if cached is not None:
            return _copy_match_result(cached)
        event_result = None
        if type(self.event_matcher) is EventMatcher:
            event_key = (id(left.event), id(right.event))
            event_result = memo.events.get(event_key)
            if event_result is None:
                event_result = self.event_matcher.match(
                    left.event,
                    right.event,
                    left_market=left,
                    right_market=right,
                )
                memo.events[event_key] = event_result
        result = self._match_uncached(left, right, event_result=event_result)
        memo.results[key] = result
        return _copy_match_result(result)

    def _match_uncached(
        self,
        left: CanonicalMarket,
        right: CanonicalMarket,
        *,
        event_result: EventMatchResult | None = None,
    ) -> MarketMatchResult:
        if event_result is None:
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
        from sports_hedge.mlb.detect import is_mlb_canonical_event
        from sports_hedge.mlb.teams import is_canonical_mlb_team
        from sports_hedge.nfl.detect import is_nfl_canonical_event
        from sports_hedge.nfl.teams import is_canonical_nfl_team
        from sports_hedge.tennis.detect import is_tennis_canonical_event
        from sports_hedge.tennis.players import same_player_pair

        if is_tennis_canonical_event(left.event) or is_tennis_canonical_event(right.event):
            if not same_player_pair(
                left.event.home_team,
                left.event.away_team,
                right.event.home_team,
                right.event.away_team,
            ):
                return MarketMatchResult(
                    matched=False,
                    confidence=event_result.confidence,
                    reasons=["event_mismatch", "participant_identity_unproven", *event_result.reasons],
                    provenance=event_result.provenance,
                )
        elif is_mlb_canonical_event(left.event) or is_mlb_canonical_event(right.event):
            from sports_hedge.mlb.identity import mlb_scheduled_games_compatible

            games_compatible, _game_reason = mlb_scheduled_games_compatible(
                left.event.scheduled_game_key,
                right.event.scheduled_game_key,
            )
            if not (
                is_canonical_mlb_team(left.event.home_team)
                and is_canonical_mlb_team(left.event.away_team)
                and left.event.home_team == right.event.home_team
                and left.event.away_team == right.event.away_team
                and games_compatible
            ):
                return MarketMatchResult(
                    matched=False,
                    confidence=event_result.confidence,
                    reasons=["event_mismatch", "participant_identity_unproven", *event_result.reasons],
                    provenance=event_result.provenance,
                )
        elif is_nfl_canonical_event(left.event) or is_nfl_canonical_event(right.event):
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
        elif left.event.sport == "basketball" or right.event.sport == "basketball":
            from sports_hedge.ncaab.detect import is_ncaab_canonical_event
            from sports_hedge.ncaab.teams import is_canonical_ncaab_team

            if is_ncaab_canonical_event(left.event) or is_ncaab_canonical_event(right.event):
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
            else:
                from sports_hedge.nba.detect import is_nba_canonical_event
                from sports_hedge.nba.teams import is_canonical_nba_team

                if is_nba_canonical_event(left.event) or is_nba_canonical_event(right.event):
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
            from sports_hedge.tennis.settlement import tennis_executable_block_reason

            block = tennis_executable_block_reason(left, right)
            if block is not None:
                match_reasons = list(event_result.reasons)
                if block not in match_reasons:
                    match_reasons.append(block)
                if REGISTER_ADMITTED_REASON not in match_reasons:
                    match_reasons.append(REGISTER_ADMITTED_REASON)
                key_reason = register_key_reason(key)
                if key_reason not in match_reasons:
                    match_reasons.append(key_reason)
                return MarketMatchResult(
                    matched=True,
                    confidence=event_result.confidence,
                    reasons=match_reasons,
                    provenance=event_result.provenance,
                    register_key=key,
                )
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
            from sports_hedge.nba.settlement import (
                nba_market_uses_paper_caveat,
                nba_paper_audit_reasons,
            )
            from sports_hedge.ncaab.settlement import (
                ncaab_market_uses_paper_caveat,
                ncaab_paper_audit_reasons,
            )
            from sports_hedge.nfl.settlement import (
                nfl_market_uses_paper_caveat,
                nfl_paper_audit_reasons,
            )

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
                register_key=key,
            )

        reasons = structural_mismatch_reasons(left, right)
        if not reasons:
            from sports_hedge.mlb.settlement import mlb_pair_non_executable_reason

            settlement_reason = mlb_pair_non_executable_reason(left, right)
            reasons = [settlement_reason or NOT_REGISTERED_REASON]
        return MarketMatchResult(
            matched=False,
            confidence=event_result.confidence,
            reasons=reasons,
            provenance=event_result.provenance,
        )
