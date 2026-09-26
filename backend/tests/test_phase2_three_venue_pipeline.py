"""Phase 2: diagnostic evidence, Polymarket normalization, register fail-closed.

Fixture and synthetic markets only. Not a live catalogue and not quotes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from sports_hedge.application.catalogue_maintenance import catalogue_row_id_for
from sports_hedge.application.collector import (
    _MatcherCallCounter,
    _NormalizedMarket,
    observe_venue_pair_markets,
)
from sports_hedge.application.complete_set import scan_eligible_pair
from sports_hedge.application.opportunity_viability import (
    IDENTITY_RELATIONSHIP_SCOPE,
    POST_MARKET_RELATIONSHIP_SCOPE,
    build_market_relationship_evidence,
    identity_viability_evidence,
    market_relationship_not_collected,
)
from sports_hedge.application.target_competitions import (
    TargetCompetitionCode,
    resolve_target_competition,
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
from sports_hedge.matching.approved_register import (
    APPROVED_PAPER_VENUE_PAIR,
    canonical_key_for_market,
    registered_canonical_key,
)
from sports_hedge.matching.bulk_market_pairs import greedy_unique_market_matches
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.normalization.venues import (
    PolymarketNormalizer,
    VenueNormalizationError,
    polymarket_moneyline_yes_outcome,
    promote_polymarket_complete_match_result,
)

KICKOFF = datetime(2026, 9, 26, 18, 45, tzinfo=UTC)

ENGLAND_SPAIN = {
    "id": "pm-eng-esp",
    "title": "England vs. Spain",
    "startTime": "2026-09-26T18:45:00Z",
    "series": [{"title": "UEFA Nations League"}],
}


def _moneyline(market_id: str, question: str, yes_token: str) -> dict:
    return {
        "id": market_id,
        "question": question,
        "sportsMarketType": "moneyline",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": f'["{yes_token}", "no-{yes_token}"]',
        "description": "Resolves from the result after 90 minutes plus stoppage time.",
    }


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


def test_england_spain_draw_question_maps_to_draw() -> None:
    assert polymarket_moneyline_yes_outcome(
        "Will England win on 2026-09-26?",
        home_team="England",
        away_team="Spain",
    ) is CanonicalOutcome.HOME
    assert polymarket_moneyline_yes_outcome(
        "Will Spain win on 2026-09-26?",
        home_team="England",
        away_team="Spain",
    ) is CanonicalOutcome.AWAY
    assert polymarket_moneyline_yes_outcome(
        "Will England vs. Spain end in a draw?",
        home_team="England",
        away_team="Spain",
    ) is CanonicalOutcome.DRAW


@pytest.mark.parametrize(
    "question",
    (
        "Will England or Spain win?",
        "Will England or draw?",
        "Will either team win?",
        "England vs Spain draw no bet",
        "Will there be no draw?",
        "Will England win or end in a draw?",
        "Will England vs. Spain not end in a draw?",
        "Double chance: England or Spain",
    ),
)
def test_ambiguous_polymarket_wording_is_not_a_standard_draw(question: str) -> None:
    assert (
        polymarket_moneyline_yes_outcome(
            question,
            home_team="England",
            away_team="Spain",
        )
        is None
    )


def test_three_england_spain_binaries_promote_to_one_match_result() -> None:
    normalizer = PolymarketNormalizer()
    event = normalizer.normalize_event(ENGLAND_SPAIN)
    payloads = [
        _moneyline("4521504", "Will England win on 2026-09-26?", "yes-home"),
        _moneyline("4521505", "Will England vs. Spain end in a draw?", "yes-draw"),
        _moneyline("4521506", "Will Spain win on 2026-09-26?", "yes-away"),
    ]
    markets = [normalizer.normalize_market(event, payload) for payload in payloads]
    promoted = promote_polymarket_complete_match_result(markets, payloads)
    assert len(promoted) == 1
    assert [runner.outcome for runner in promoted[0].runners] == [
        CanonicalOutcome.HOME,
        CanonicalOutcome.DRAW,
        CanonicalOutcome.AWAY,
    ]
    assert promoted[0].family is MarketFamily.MATCH_RESULT
    assert promoted[0].period is FootballPeriod.FULL_TIME
    assert canonical_key_for_market(promoted[0]) is None


def test_adversarial_wording_is_not_promoted_to_1x2() -> None:
    normalizer = PolymarketNormalizer()
    event = normalizer.normalize_event(ENGLAND_SPAIN)
    payloads = [
        _moneyline("a", "Will England or Spain win?", "yes-a"),
        _moneyline("b", "Will England or draw?", "yes-b"),
        _moneyline("c", "Will either team win?", "yes-c"),
    ]
    markets = [normalizer.normalize_market(event, payload) for payload in payloads]
    promoted = promote_polymarket_complete_match_result(markets, payloads)
    assert len(promoted) == 3
    for market in promoted:
        assert {runner.outcome for runner in market.runners} == {
            CanonicalOutcome.YES,
            CanonicalOutcome.NO,
        }


def _total_payload(question: str, *, sports_type: str = "totals", market_id: str = "ou") -> dict:
    return {
        "id": market_id,
        "question": question,
        "sportsMarketType": sports_type,
        "outcomes": '["Over", "Under"]',
        "clobTokenIds": '["yes-over", "yes-under"]',
        "description": "Resolves from full-time goals after 90 minutes plus stoppage time.",
    }


@pytest.mark.parametrize("line", ("1.5", "2.5", "3.5"))
def test_polymarket_structured_full_time_total_normalizes(line: str) -> None:
    normalizer = PolymarketNormalizer()
    event = normalizer.normalize_event(ENGLAND_SPAIN)
    market = normalizer.normalize_market(
        event,
        _total_payload(f"England vs. Spain: O/U {line}", market_id=f"ou-{line}"),
    )
    assert market.family is MarketFamily.TOTAL_GOALS
    assert market.line == Decimal(line)
    assert market.period is FootballPeriod.FULL_TIME
    assert {runner.outcome for runner in market.runners} == {
        CanonicalOutcome.OVER,
        CanonicalOutcome.UNDER,
    }
    assert canonical_key_for_market(market) is None


@pytest.mark.parametrize(
    ("question", "sports_type"),
    (
        ("England vs. Spain: O/U 2.5", ""),
        ("England: O/U 1.5", "totals"),
        ("England vs. Spain: First Half O/U 2.5", "totals"),
        ("England vs. Spain: Second Half O/U 1.5", "totals"),
        ("England vs. Spain: O/U 2", "totals"),
        ("England team total 1.5", "soccer_team_totals"),
        ("England vs. Spain: O/U 2.5", "first_half_totals"),
        ("England vs. Spain: O/U 2.5", "second_half_totals"),
    ),
)
def test_unsupported_polymarket_totals_stay_rejected(question: str, sports_type: str) -> None:
    normalizer = PolymarketNormalizer()
    event = normalizer.normalize_event(ENGLAND_SPAIN)
    with pytest.raises(VenueNormalizationError, match="Unsupported Polymarket"):
        normalizer.normalize_market(event, _total_payload(question, sports_type=sports_type))


def test_corner_and_card_wording_is_not_a_full_time_goals_total() -> None:
    normalizer = PolymarketNormalizer()
    event = normalizer.normalize_event(ENGLAND_SPAIN)
    corners = normalizer.normalize_market(
        event,
        _total_payload("England vs. Spain: Total Corners 8.5", sports_type="totals", market_id="corners"),
    )
    cards = normalizer.normalize_market(
        event,
        _total_payload("England vs. Spain: Total Cards 3.5", sports_type="totals", market_id="cards"),
    )
    assert corners.family is MarketFamily.CORNERS
    assert cards.family is MarketFamily.CARDS
    assert canonical_key_for_market(corners) is None
    assert canonical_key_for_market(cards) is None


def test_matchbook_kalshi_selection_is_unchanged_when_polymarket_markets_exist() -> None:
    left, right = _mb_k_board()
    without_pm = _pair_keys(left, right)
    polymarket = _event(VenueName.POLYMARKET, "1016065")
    extra = [
        _market(
            polymarket,
            family=MarketFamily.MATCH_RESULT,
            source_id="pm-1x2",
            outcomes=[CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY],
        ),
        _market(
            polymarket,
            family=MarketFamily.BOTH_TEAMS_TO_SCORE,
            source_id="pm-btts",
            outcomes=[CanonicalOutcome.YES, CanonicalOutcome.NO],
        ),
        _market(
            polymarket,
            family=MarketFamily.TOTAL_GOALS,
            source_id="pm-ou-2.5",
            outcomes=[CanonicalOutcome.OVER, CanonicalOutcome.UNDER],
            line=Decimal("2.5"),
        ),
    ]
    with_pm_present = _pair_keys(left, right)
    assert with_pm_present == without_pm
    assert len(without_pm) == 8
    assert all(canonical_key_for_market(market) is None for market in extra)
    matcher = _MatcherCallCounter(MarketMatcher())
    chosen = greedy_unique_market_matches(extra, right, matcher)
    assert chosen == []
    assert matcher.calls == 0
    for market in extra:
        assert registered_canonical_key(left[0], market) is None
        assert registered_canonical_key(market, right[0]) is None


def test_promoted_polymarket_match_result_is_not_register_eligible() -> None:
    normalizer = PolymarketNormalizer()
    event = normalizer.normalize_event(ENGLAND_SPAIN)
    payloads = [
        _moneyline("4521504", "Will England win on 2026-09-26?", "yes-home"),
        _moneyline("4521505", "Will England vs. Spain end in a draw?", "yes-draw"),
        _moneyline("4521506", "Will Spain win on 2026-09-26?", "yes-away"),
    ]
    promoted = normalizer.assemble_canonical_markets(event, payloads)
    assert len(promoted) == 1
    left, right = _mb_k_board()
    match = MarketMatcher().match(left[0], promoted[0])
    assert scan_eligible_pair(left[0], promoted[0], match) is False
    assert scan_eligible_pair(promoted[0], right[0], MarketMatcher().match(promoted[0], right[0])) is False
    assert APPROVED_PAPER_VENUE_PAIR == frozenset({VenueName.MATCHBOOK, VenueName.KALSHI})
    assert VenueName.POLYMARKET not in APPROVED_PAPER_VENUE_PAIR


def test_catalogue_row_identity_is_still_event_plus_canonical_key() -> None:
    first = catalogue_row_id_for("evt:eng-esp", "MATCH_RESULT_FT")
    second = catalogue_row_id_for("evt:eng-esp", "TOTAL_GOALS_FT:2.5")
    assert first.startswith("amc:")
    assert first != second
    assert catalogue_row_id_for("evt:eng-esp", "MATCH_RESULT_FT") == first


@pytest.mark.parametrize(
    "label",
    (
        "UEFA Nations League A",
        "UEFA Nations League B",
        "UEFA Nations League C",
        "UEFA Nations League D",
    ),
)
def test_nations_league_division_aliases_resolve(label: str) -> None:
    resolved = resolve_target_competition(label)
    assert resolved is not None
    assert resolved.code is TargetCompetitionCode.UEFA_NATIONS_LEAGUE


@pytest.mark.parametrize(
    "label",
    (
        "CONCACAF Nations League",
        "UEFA Women's Nations League",
        "Volleyball Nations League",
        "Six Nations",
        "Gulf Cup of Nations",
        "Africa Cup of Nations Qualification",
    ),
)
def test_unrelated_nations_labels_stay_rejected(label: str) -> None:
    resolved = resolve_target_competition(label)
    if resolved is None:
        return
    assert resolved.code is not TargetCompetitionCode.UEFA_NATIONS_LEAGUE
