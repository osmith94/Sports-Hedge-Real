"""Phase 2: diagnostic evidence, Polymarket normalization, register fail-closed.

Fixture and synthetic markets only. Not a live catalogue and not quotes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sports_hedge.application.collector import (
    _NormalizedMarket,
    observe_venue_pair_markets,
)
from sports_hedge.application.opportunity_viability import (
    IDENTITY_RELATIONSHIP_SCOPE,
    POST_MARKET_RELATIONSHIP_SCOPE,
    build_market_relationship_evidence,
    identity_viability_evidence,
    market_relationship_not_collected,
)
from sports_hedge.domain.football import (
    CanonicalEvent,
    CanonicalMarket,
    CanonicalOutcome,
    CanonicalRunner,
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.approved_register import registered_canonical_key
from sports_hedge.matching.bulk_market_pairs import greedy_unique_market_matches
from sports_hedge.matching.markets import MarketMatcher

KICKOFF = datetime(2026, 9, 26, 18, 45, tzinfo=UTC)
def _event(venue: VenueName, source_id: str) -> CanonicalEvent:
    return CanonicalEvent(
        competition="UEFA Nations League",
        home_team="England",
        away_team="Spain",
        kickoff_utc=KICKOFF,
        source_venue=venue,
        source_event_id=source_id,
    )


def _settlement(line: Decimal | None = None) -> SettlementFingerprint:
    return SettlementFingerprint(
        scope=SettlementScope.REGULATION_TIME,
        period=FootballPeriod.FULL_TIME,
        line=line,
        push_possible=False if line is None else line != line.to_integral_value(),
        extra_time_included=False,
        penalties_included=False,
    )


def _market(
    event: CanonicalEvent,
    *,
    family: MarketFamily,
    source_id: str,
    outcomes: list[CanonicalOutcome],
    line: Decimal | None = None,
) -> CanonicalMarket:
    return CanonicalMarket(
        event=event,
        source_venue=event.source_venue,
        source_market_id=source_id,
        family=family,
        period=FootballPeriod.FULL_TIME,
        line=line,
        settlement=_settlement(line),
        runners=[
            CanonicalRunner(
                source_runner_id=f"{source_id}-{outcome.value}",
                outcome=outcome,
                label=outcome.value,
            )
            for outcome in outcomes
        ],
    )


def _mb_k_board() -> tuple[list[CanonicalMarket], list[CanonicalMarket]]:
    matchbook = _event(VenueName.MATCHBOOK, "mb-eng-esp")
    kalshi = _event(VenueName.KALSHI, "KXUEFANLGAME-26SEP26ENGESP")
    lines = ("1.5", "2.5", "3.5", "4.5", "5.5", "6.5")
    left = [
        _market(
            matchbook,
            family=MarketFamily.MATCH_RESULT,
            source_id="mb-1x2",
            outcomes=[CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY],
        ),
        _market(
            matchbook,
            family=MarketFamily.BOTH_TEAMS_TO_SCORE,
            source_id="mb-btts",
            outcomes=[CanonicalOutcome.YES, CanonicalOutcome.NO],
        ),
    ]
    right = [
        _market(
            kalshi,
            family=MarketFamily.MATCH_RESULT,
            source_id="k-1x2",
            outcomes=[CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY],
        ),
        _market(
            kalshi,
            family=MarketFamily.BOTH_TEAMS_TO_SCORE,
            source_id="k-btts",
            outcomes=[CanonicalOutcome.YES, CanonicalOutcome.NO],
        ),
    ]
    for line in lines:
        left.append(
            _market(
                matchbook,
                family=MarketFamily.TOTAL_GOALS,
                source_id=f"mb-ou-{line}",
                outcomes=[CanonicalOutcome.OVER, CanonicalOutcome.UNDER],
                line=Decimal(line),
            )
        )
        right.append(
            _market(
                kalshi,
                family=MarketFamily.TOTAL_GOALS,
                source_id=f"k-ou-{line}",
                outcomes=[CanonicalOutcome.OVER, CanonicalOutcome.UNDER],
                line=Decimal(line),
            )
        )
    return left, right


def _pair_keys(left: list[CanonicalMarket], right: list[CanonicalMarket]) -> list[str]:
    matcher = MarketMatcher()
    chosen = greedy_unique_market_matches(left, right, matcher)
    keys: list[str] = []
    for left_index, right_index, match in chosen:
        key = registered_canonical_key(left[left_index], right[right_index])
        assert match.matched is True
        assert key is not None
        keys.append(key)
    return keys


def test_identity_evidence_is_not_catalogue_truth() -> None:
    evidence = identity_viability_evidence("evt:eng-esp", ["matchbook", "kalshi", "polymarket"])
    assert evidence["evidence_stage"] == "identity"
    assert evidence["relationship_fields_scope"] == IDENTITY_RELATIONSHIP_SCOPE
    assert evidence["attempted_relationships"] == []
    assert evidence["registered_relationships"] == []
    nested = evidence["market_relationship_evidence"]
    assert nested["status"] == "not_collected"
    assert nested["selected_relationship_count"] is None
    assert nested["persisted_catalogue_count"] is None
    assert nested["reason"] == "identity_stage_before_market_processing"


def test_eight_matchbook_kalshi_relationships_are_post_market_evidence() -> None:
    left, right = _mb_k_board()
    keys = _pair_keys(left, right)
    assert len(keys) == 8
    observation = {
        "venue_pair": "matchbook/kalshi",
        "matcher_invoked": True,
        "matcher_call_count": 8,
        "selected_count": 8,
        "registered_keys": keys,
        "rejection_reason": None,
    }
    evidence = build_market_relationship_evidence(
        [observation],
        discovered_archetypes=["match_result", "both_teams_to_score", "total_goals"],
        persisted_catalogue_keys=keys,
    )
    assert evidence["evidence_stage"] == "post_market"
    assert evidence["status"] == "collected"
    assert evidence["attempted_relationships"] == ["matchbook/kalshi"]
    assert evidence["registered_relationships"] == keys
    assert evidence["selected_relationship_count"] == 8
    assert evidence["persisted_catalogue_count"] == 8
    assert evidence["discovered_archetypes"]
    assert POST_MARKET_RELATIONSHIP_SCOPE == "post_market_catalogue"


def test_matcher_not_invoked_is_not_an_attempted_relationship() -> None:
    observation = observe_venue_pair_markets(
        VenueName.POLYMARKET,
        VenueName.KALSHI,
        [_NormalizedMarket({}, _mb_k_board()[0][0].model_copy(update={"source_venue": VenueName.POLYMARKET}))],
        [_NormalizedMarket({}, _mb_k_board()[1][0])],
        matcher_calls=0,
        selected=[],
    )
    assert observation["matcher_invoked"] is False
    assert observation["rejection_reason"] == "matcher_not_invoked_no_shared_register_key"
    evidence = build_market_relationship_evidence(
        [observation],
        discovered_archetypes=["match_result"],
        persisted_catalogue_keys=[],
    )
    assert evidence["attempted_relationships"] == []
    assert evidence["registered_relationships"] == []
    assert evidence["selected_relationship_count"] == 0
    assert evidence["persisted_catalogue_count"] == 0


def test_not_collected_counts_are_unset_and_a_real_zero_is_zero() -> None:
    missing = market_relationship_not_collected("market_processing_not_run")
    assert missing["selected_relationship_count"] is None
    assert missing["persisted_catalogue_count"] is None
    empty = build_market_relationship_evidence(
        [
            {
                "venue_pair": "matchbook/kalshi",
                "matcher_invoked": True,
                "selected_count": 0,
                "registered_keys": [],
                "rejection_reason": "no_registered_relationship",
            }
        ],
        discovered_archetypes=[],
        persisted_catalogue_keys=[],
    )
    assert empty["selected_relationship_count"] == 0
    assert empty["persisted_catalogue_count"] == 0
    assert empty["attempted_relationships"] == ["matchbook/kalshi"]

