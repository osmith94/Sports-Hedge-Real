"""Issue #210: Wave K1 fail-closed canonical equivalence.

Frozen-base reproduction at `2696c1ef2bfb81ac7b5a980f02d61f122ac5fdd7`:
- C1 90-minute + unparsed ET/tie/shootout -> REGULATION_TIME (false equivalent)
- C2 `including extra time penalties do not count` -> INCLUDING_PENALTIES
- C3 `including penalties` + 90-minute / ET-does-not-count -> INCLUDING_PENALTIES
- C4 `Total Goals Over 2.5 - Arsenal` -> TOTAL_GOALS

Data class: deterministic fixture/demo current-state and paper-scan payloads.
Not live, historical, or modelled venue quotes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from sports_hedge.application.complete_set import scan_eligible_pair, solver_eligible_market
from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)
from sports_hedge.application.paper_scan import PaperScanService
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
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.learned_rules import (
    LearnedMappingApplicator,
    MappingFieldScope,
    MappingGuardrails,
    MappingRule,
    MappingRuleSource,
    MappingRuleType,
    participant_identity_preserved,
)
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.normalization.identity import kickoff_bucket
from sports_hedge.normalization.venues import (
    MatchbookNormalizer,
    PolymarketNormalizer,
    classify_settlement_wording,
)
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import matchbook_polymarket_costs


KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
OBSERVED = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)
REGULATION = "Resolves based on 90 minutes of regulation time."
ET_AND_PENALTIES = "Winner including extra time and penalties."
ET_ONLY = "Resolves including extra time."
GAMMA_POSTPONE = (
    "If the game is postponed, this market will remain open until the game has been completed. "
    "If the game is canceled entirely, with no make-up game, this market will resolve No. "
    "This market refers only to the outcome within the first 90 minutes of regular play plus stoppage time."
)

C1_STRINGS = (
    "Resolves based on 90 minutes of regulation time. If the match is tied, extra time and a penalty shootout decide the winner.",
    "90 minutes of regulation time. If scores are level, the game goes to extra time then penalties.",
    "Regulation time. In the event of a tie, extra time applies.",
    "This market refers only to the first 90 minutes. If the match is tied a shootout decides it.",
    "Resolves on 90 minutes of regulation. If the game is tied, a shoot-out decides the winner.",
)
C2_STRINGS = (
    "including extra time penalties do not count",
    "Resolves including extra time penalties do not count",
    "Winner including extra time penalties",
    "including extra time penalties don't count",
)
C3_STRINGS = (
    "Resolves based on 90 minutes of regulation time. Extra time does not count. Including penalties.",
    "90 minutes of regulation time. Extra time does not count including penalties.",
    "Resolves including penalties. Extra time does not count. 90 minutes of regulation time.",
    "Regulation time including penalties. Extra time is not included.",
)
C4_TITLES = (
    "Total Goals Over 2.5 - Arsenal",
    "Arsenal Total Goals Over 2.5",
    "Total Goals Over 2.5 Arsenal",
    "Arsenal team total 2.5",
)

MB_EVENT = {
    "id": 21001,
    "name": "Newcastle United vs Arsenal",
    "start": KICKOFF.isoformat(),
    "competition-name": "Premier League",
}
PM_EVENT = {
    "id": "pm-issue-210",
    "title": "Newcastle United vs. Arsenal",
    "startTime": KICKOFF.isoformat(),
    "series": [{"title": "Premier League"}],
}


def _fx() -> list[FxRateSnapshot]:
    return [
        FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test_fx"),
        FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1"), source="functional_currency"),
    ]


def _mb_1x2() -> dict[str, Any]:
    return {
        "id": 21101,
        "name": "Match Odds",
        "runners": [
            {
                "id": 1,
                "name": "Newcastle United",
                "prices": [{"side": "back", "odds": "2.10", "available-amount": "80"}],
            },
            {
                "id": 2,
                "name": "Draw",
                "prices": [{"side": "back", "odds": "3.40", "available-amount": "80"}],
            },
            {
                "id": 3,
                "name": "Arsenal",
                "prices": [{"side": "back", "odds": "3.50", "available-amount": "80"}],
            },
        ],
    }


def _mb_btts() -> dict[str, Any]:
    return {
        "id": 21102,
        "name": "Both Teams To Score",
        "runners": [
            {
                "id": 11,
                "name": "Yes",
                "prices": [{"side": "back", "odds": "1.80", "available-amount": "100"}],
            },
            {
                "id": 12,
                "name": "No",
                "prices": [{"side": "back", "odds": "2.10", "available-amount": "100"}],
            },
        ],
    }


def _mb_totals(name: str = "Over/Under 2.5 Goals") -> dict[str, Any]:
    return {
        "id": 21103,
        "name": name,
        "runners": [
            {
                "id": 21,
                "name": "Over 2.5",
                "prices": [{"side": "back", "odds": "1.90", "available-amount": "120"}],
            },
            {
                "id": 22,
                "name": "Under 2.5",
                "prices": [{"side": "back", "odds": "2.00", "available-amount": "120"}],
            },
        ],
    }


def _mb_dnb() -> dict[str, Any]:
    return {
        "id": 21104,
        "name": "Draw No Bet",
        "runners": [
            {
                "id": 31,
                "name": "Newcastle United",
                "prices": [{"side": "back", "odds": "1.50", "available-amount": "50"}],
            },
            {
                "id": 32,
                "name": "Arsenal",
                "prices": [{"side": "back", "odds": "2.60", "available-amount": "50"}],
            },
        ],
    }


def _mb_double_chance() -> dict[str, Any]:
    return {
        "id": 21105,
        "name": "Double Chance",
        "runners": [
            {"id": 41, "name": "Home or Draw", "prices": [{"side": "back", "odds": "1.30", "available-amount": "40"}]},
            {"id": 42, "name": "Home or Away", "prices": [{"side": "back", "odds": "1.25", "available-amount": "40"}]},
            {"id": 43, "name": "Draw or Away", "prices": [{"side": "back", "odds": "1.40", "available-amount": "40"}]},
        ],
    }


def _mb_to_qualify() -> dict[str, Any]:
    return {
        "id": 21106,
        "name": "To Qualify",
        "runners": [
            {"id": 51, "name": "Newcastle United", "prices": [{"side": "back", "odds": "1.70", "available-amount": "40"}]},
            {"id": 52, "name": "Arsenal", "prices": [{"side": "back", "odds": "2.20", "available-amount": "40"}]},
        ],
    }


def _pm_1x2(description: str, *, market_id: str = "pm-1x2") -> dict[str, Any]:
    return {
        "id": market_id,
        "question": "Match result?",
        "sportsMarketType": "moneyline",
        "outcomes": '["Newcastle United", "Draw", "Arsenal"]',
        "clobTokenIds": '["h", "d", "a"]',
        "description": description,
    }


def _pm_btts(description: str = REGULATION) -> dict[str, Any]:
    return {
        "id": "pm-btts",
        "question": "Both teams to score?",
        "sportsMarketType": "both teams to score",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["yes", "no"]',
        "description": description,
    }


def _pm_totals(question: str, description: str = REGULATION, *, market_id: str = "pm-tg") -> dict[str, Any]:
    return {
        "id": market_id,
        "question": question,
        "sportsMarketType": "total goals",
        "line": "2.5",
        "outcomes": '["Over", "Under"]',
        "clobTokenIds": '["o", "u"]',
        "description": description,
    }


def _pm_books(*tokens: str) -> dict[str, dict[str, Any]]:
    return {
        token: {
            "asset_id": token,
            "asks": [{"price": "0.40", "size": "200"}],
            "bids": [{"price": "0.38", "size": "200"}],
        }
        for token in tokens
    }


def _scan(mb_market: dict[str, Any], pm_market: dict[str, Any], books: dict[str, dict[str, Any]]):
    repository = SqliteMarketIntelligenceRepository()
    service = PaperScanService(MarketIntelligenceService(repository))
    matchbook = MatchbookObservationBuilder().build(
        MB_EVENT, mb_market, observed_at=OBSERVED, quote_age_ms=120
    )
    polymarket = PolymarketObservationBuilder().build(
        PM_EVENT, pm_market, books, observed_at=OBSERVED, quote_age_ms=150
    )
    try:
        decision = service.scan_pair(
            matchbook,
            polymarket,
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
        )
        return decision, matchbook, polymarket
    finally:
        repository.close()


def _assert_not_in_solver(decision, left, right) -> None:
    match = MarketMatcher().match(left.market, right.market)
    assert match.matched is False
    assert scan_eligible_pair(left.market, right.market, match) is False
    assert decision.market_match.matched is False
    assert decision.solver_model is None
    assert decision.eligible_for_paper_simulation is False
    assert decision.depth_scan is None
    assert "market_not_equivalent" in decision.rejection_reasons


@pytest.mark.parametrize("text", C1_STRINGS)
def test_c1_unparsed_et_tie_shootout_fails_closed(text: str) -> None:
    scope, extra_time, penalties = classify_settlement_wording(text)
    assert scope is SettlementScope.UNKNOWN
    assert extra_time is None
    assert penalties is None


@pytest.mark.parametrize("text", C2_STRINGS)
def test_c2_missing_conjunction_does_not_parse_as_et_penalties(text: str) -> None:
    scope, extra_time, penalties = classify_settlement_wording(text)
    assert scope is not SettlementScope.INCLUDING_PENALTIES
    assert extra_time is not True or penalties is not True
    assert scope is SettlementScope.UNKNOWN


@pytest.mark.parametrize("text", C3_STRINGS)
def test_c3_including_penalties_cannot_override_regulation_context(text: str) -> None:
    scope, extra_time, penalties = classify_settlement_wording(text)
    assert scope is SettlementScope.UNKNOWN
    assert extra_time is None
    assert penalties is None


@pytest.mark.parametrize("title", C4_TITLES)
def test_c4_polymarket_named_team_total_is_not_match_total(title: str) -> None:
    event = PolymarketNormalizer().normalize_event(PM_EVENT)
    market = PolymarketNormalizer().normalize_market(
        event,
        _pm_totals(title, market_id=f"pm-{title}"),
    )
    assert market.family is MarketFamily.TEAM_TOTAL
    assert solver_eligible_market(market) is False


def test_positive_settlement_controls_remain_distinct() -> None:
    assert classify_settlement_wording(REGULATION) == (
        SettlementScope.REGULATION_TIME,
        False,
        False,
    )
    assert classify_settlement_wording(
        "Resolves on 90 minutes of regulation time. Extra time and penalties do not count."
    ) == (SettlementScope.REGULATION_TIME, False, False)
    assert classify_settlement_wording(ET_ONLY) == (
        SettlementScope.INCLUDING_EXTRA_TIME,
        True,
        False,
    )
    assert classify_settlement_wording(ET_AND_PENALTIES) == (
        SettlementScope.INCLUDING_PENALTIES,
        True,
        True,
    )
    assert classify_settlement_wording("Resolves including penalties after extra time.") == (
        SettlementScope.INCLUDING_PENALTIES,
        True,
        True,
    )
    assert classify_settlement_wording("Resolves including extra time without penalties.") == (
        SettlementScope.INCLUDING_EXTRA_TIME,
        True,
        False,
    )
    assert classify_settlement_wording(GAMMA_POSTPONE) == (
        SettlementScope.REGULATION_TIME,
        False,
        False,
    )


def test_c1_false_pair_does_not_enter_solver() -> None:
    decision, matchbook, polymarket = _scan(
        _mb_1x2(),
        _pm_1x2(C1_STRINGS[0]),
        _pm_books("h", "d", "a"),
    )
    assert polymarket.market.settlement.scope is SettlementScope.UNKNOWN
    _assert_not_in_solver(decision, matchbook, polymarket)


def test_c2_false_pair_does_not_match_true_et_penalties() -> None:
    left_event = PolymarketNormalizer().normalize_event(PM_EVENT)
    true_et_pen = PolymarketNormalizer().normalize_market(
        left_event,
        _pm_1x2(ET_AND_PENALTIES, market_id="pm-true-et-pen"),
    )
    false_c2 = PolymarketNormalizer().normalize_market(
        left_event,
        _pm_1x2(C2_STRINGS[0], market_id="pm-c2"),
    )
    match = MarketMatcher().match(true_et_pen, false_c2)
    assert false_c2.settlement.scope is SettlementScope.UNKNOWN
    assert true_et_pen.settlement.scope is SettlementScope.INCLUDING_PENALTIES
    assert match.matched is False
    assert scan_eligible_pair(true_et_pen, false_c2, match) is False

    mb_qualify = MatchbookNormalizer().normalize_market(
        MatchbookNormalizer().normalize_event(MB_EVENT),
        _mb_to_qualify(),
    )
    qualify_vs_c2 = MarketMatcher().match(mb_qualify, false_c2)
    assert qualify_vs_c2.matched is False


def test_c3_false_pair_does_not_enter_solver_against_et_penalties() -> None:
    left_event = PolymarketNormalizer().normalize_event(PM_EVENT)
    true_et_pen = PolymarketNormalizer().normalize_market(
        left_event,
        _pm_1x2(ET_AND_PENALTIES, market_id="pm-true-et-pen-c3"),
    )
    false_c3 = PolymarketNormalizer().normalize_market(
        left_event,
        _pm_1x2(C3_STRINGS[0], market_id="pm-c3"),
    )
    match = MarketMatcher().match(true_et_pen, false_c3)
    assert false_c3.settlement.scope is SettlementScope.UNKNOWN
    assert match.matched is False
    assert scan_eligible_pair(true_et_pen, false_c3, match) is False

    decision, matchbook, polymarket = _scan(
        _mb_1x2(),
        _pm_1x2(C3_STRINGS[0], market_id="pm-c3-scan"),
        _pm_books("h", "d", "a"),
    )
    _assert_not_in_solver(decision, matchbook, polymarket)


def test_c4_team_total_does_not_enter_solver_against_match_total() -> None:
    decision, matchbook, polymarket = _scan(
        _mb_totals(),
        _pm_totals(C4_TITLES[0], market_id="pm-arsenal-total"),
        _pm_books("o", "u"),
    )
    assert matchbook.market.family is MarketFamily.TOTAL_GOALS
    assert polymarket.market.family is MarketFamily.TEAM_TOTAL
    _assert_not_in_solver(decision, matchbook, polymarket)


def test_positive_controls_legitimate_equivalent_markets_still_match() -> None:
    one_x_two, mb_1x2, pm_1x2 = _scan(
        _mb_1x2(),
        _pm_1x2(REGULATION),
        _pm_books("h", "d", "a"),
    )
    assert MarketMatcher().match(mb_1x2.market, pm_1x2.market).matched is True
    assert scan_eligible_pair(mb_1x2.market, pm_1x2.market, one_x_two.market_match) is True
    assert one_x_two.solver_model == "simple_complete_set"

    btts, mb_btts, pm_btts = _scan(_mb_btts(), _pm_btts(), _pm_books("yes", "no"))
    assert btts.market_match.matched is True
    assert btts.solver_model == "simple_complete_set"

    totals, mb_totals, pm_totals = _scan(
        _mb_totals(),
        _pm_totals("Total Goals Over 2.5"),
        _pm_books("o", "u"),
    )
    assert mb_totals.market.family is MarketFamily.TOTAL_GOALS
    assert pm_totals.market.family is MarketFamily.TOTAL_GOALS
    assert totals.market_match.matched is True
    assert totals.solver_model == "simple_complete_set"

    both_named, mb_named, pm_named = _scan(
        _mb_totals(),
        _pm_totals("Newcastle United vs Arsenal Total Goals Over 2.5", market_id="pm-both"),
        _pm_books("o", "u"),
    )
    assert pm_named.market.family is MarketFamily.TOTAL_GOALS
    assert both_named.market_match.matched is True


def test_positive_controls_dnb_double_chance_to_qualify_remain_mismatched() -> None:
    dnb, mb_dnb, pm_1x2 = _scan(_mb_dnb(), _pm_1x2(REGULATION, market_id="pm-vs-dnb"), _pm_books("h", "d", "a"))
    assert dnb.market_match.matched is False
    assert "market_family_mismatch" in dnb.rejection_reasons or "market_not_equivalent" in dnb.rejection_reasons
    assert dnb.solver_model is None

    dc, mb_dc, _pm = _scan(
        _mb_double_chance(),
        _pm_1x2(REGULATION, market_id="pm-vs-dc"),
        _pm_books("h", "d", "a"),
    )
    assert dc.market_match.matched is False
    assert dc.solver_model is None

    qualify, mb_q, pm_et = _scan(
        _mb_to_qualify(),
        _pm_1x2(ET_AND_PENALTIES, market_id="pm-vs-qualify"),
        _pm_books("h", "d", "a"),
    )
    assert mb_q.market.family is MarketFamily.TO_QUALIFY
    assert qualify.market_match.matched is False
    assert qualify.solver_model is None


def test_abandon_void_postponement_unparsed_fails_closed() -> None:
    abandoned = classify_settlement_wording(
        f"{REGULATION} If the match is abandoned, all bets are void."
    )
    postponed_void = classify_settlement_wording(
        f"{REGULATION} If postponed, this market will void."
    )
    assert abandoned == (SettlementScope.UNKNOWN, None, None)
    assert postponed_void == (SettlementScope.UNKNOWN, None, None)

    decision, matchbook, polymarket = _scan(
        _mb_1x2(),
        _pm_1x2(f"{REGULATION} If the match is abandoned, all bets are void.", market_id="pm-abandon"),
        _pm_books("h", "d", "a"),
    )
    _assert_not_in_solver(decision, matchbook, polymarket)

    remain_open, mb_reg, pm_gamma = _scan(
        _mb_1x2(),
        _pm_1x2(GAMMA_POSTPONE, market_id="pm-gamma-postpone"),
        _pm_books("h", "d", "a"),
    )
    assert pm_gamma.market.settlement.scope is SettlementScope.REGULATION_TIME
    assert remain_open.market_match.matched is True
    assert remain_open.solver_model == "simple_complete_set"
    assert "Draw voids" in "Resolves based on 90 minutes of regulation time. Draw voids."
    assert classify_settlement_wording(f"{REGULATION} Draw voids.") == (
        SettlementScope.REGULATION_TIME,
        False,
        False,
    )


def test_learned_mapping_cannot_override_participant_identity() -> None:
    assert participant_identity_preserved("Arsenal", "Arsenal FC") is True
    assert participant_identity_preserved("Athletic Club", "Athletic Bilbao") is True
    assert participant_identity_preserved("Arsenal", "Chelsea") is False

    rule = MappingRule(
        rule_id="maprule:issue210-identity",
        created_at=OBSERVED,
        operator="oliver",
        source=MappingRuleSource.OPERATOR_MANUAL,
        rule_type=MappingRuleType.VENUE_NAME_CONVENTION,
        venue=VenueName.POLYMARKET,
        field_scope=MappingFieldScope.TEAM_NAME,
        raw_pattern="Chelsea",
        canonical_transformation="Newcastle United",
        guardrails=MappingGuardrails(sport="football", competition_code="premier_league"),
        evidence="must not rewrite a different club into fixture identity",
    )
    matcher = MarketMatcher(EventMatcher(learned_applicator=LearnedMappingApplicator(rules=[rule])))
    mb = MatchbookNormalizer().normalize_market(
        MatchbookNormalizer().normalize_event(MB_EVENT),
        _mb_1x2(),
    )
    pm_event = {
        "id": "pm-chelsea-ars",
        "title": "Chelsea vs. Arsenal",
        "startTime": KICKOFF.isoformat(),
        "series": [{"title": "Premier League"}],
    }
    pm = PolymarketNormalizer().normalize_market(
        PolymarketNormalizer().normalize_event(pm_event),
        {
            "id": "pm-wrong-home",
            "question": "Match result?",
            "sportsMarketType": "moneyline",
            "outcomes": '["Chelsea", "Draw", "Arsenal"]',
            "clobTokenIds": '["h", "d", "a"]',
            "description": REGULATION,
        },
    )
    native = MarketMatcher().match(mb, pm)
    overridden = matcher.match(mb, pm)
    assert native.matched is False
    assert overridden.matched is False
    assert participant_identity_preserved("Chelsea", "Newcastle United") is False

    suffix = MappingRule(
        rule_id="maprule:issue210-suffix",
        created_at=OBSERVED,
        operator="oliver",
        source=MappingRuleSource.OPERATOR_MANUAL,
        rule_type=MappingRuleType.VENUE_SUFFIX_STRIP,
        venue=VenueName.POLYMARKET,
        field_scope=MappingFieldScope.TEAM_NAME,
        raw_pattern="fc",
        canonical_transformation="strip_suffix",
        guardrails=MappingGuardrails(sport="football", competition_code="premier_league"),
        evidence="safe FC suffix remains allowed",
    )
    suffix_matcher = MarketMatcher(
        EventMatcher(learned_applicator=LearnedMappingApplicator(rules=[suffix]))
    )
    mb_event = CanonicalEvent(
        competition="Premier League",
        home_team="Leeds United",
        away_team="Chelsea",
        kickoff_utc=KICKOFF,
        source_venue=VenueName.MATCHBOOK,
        source_event_id="mb-leeds",
    )
    pm_event_canon = CanonicalEvent(
        competition="Premier League",
        home_team="Leeds United FC",
        away_team="Chelsea FC",
        kickoff_utc=KICKOFF,
        source_venue=VenueName.POLYMARKET,
        source_event_id="pm-leeds",
    )
    settlement = SettlementFingerprint(
        scope=SettlementScope.REGULATION_TIME,
        period=FootballPeriod.FULL_TIME,
        extra_time_included=False,
        penalties_included=False,
    )
    runners = [
        CanonicalRunner(source_runner_id="h", outcome=CanonicalOutcome.HOME, label="Home"),
        CanonicalRunner(source_runner_id="d", outcome=CanonicalOutcome.DRAW, label="Draw"),
        CanonicalRunner(source_runner_id="a", outcome=CanonicalOutcome.AWAY, label="Away"),
    ]
    safe = suffix_matcher.match(
        CanonicalMarket(
            event=mb_event,
            source_venue=VenueName.MATCHBOOK,
            source_market_id="mb-1x2",
            family=MarketFamily.MATCH_RESULT,
            period=FootballPeriod.FULL_TIME,
            settlement=settlement,
            runners=runners,
        ),
        CanonicalMarket(
            event=pm_event_canon,
            source_venue=VenueName.POLYMARKET,
            source_market_id="pm-1x2",
            family=MarketFamily.MATCH_RESULT,
            period=FootballPeriod.FULL_TIME,
            settlement=settlement,
            runners=runners,
        ),
    )
    assert safe.matched is True


def test_fixture_specific_alias_cannot_swap_in_a_different_club() -> None:
    kickoff = kickoff_bucket(KICKOFF).isoformat()
    rule = MappingRule(
        rule_id="maprule:issue210-fixture-swap",
        created_at=OBSERVED,
        operator="oliver",
        source=MappingRuleSource.OPERATOR_MANUAL,
        rule_type=MappingRuleType.FIXTURE_SPECIFIC_ALIAS,
        venue=VenueName.POLYMARKET,
        field_scope=MappingFieldScope.TEAM_NAME,
        raw_pattern=f"home=chelsea|away=liverpool|kickoff={kickoff}|src=pm-swap",
        canonical_transformation="home=newcastle united|away=arsenal",
        guardrails=MappingGuardrails(sport="football", competition_code="premier_league"),
        evidence="fixture alias must not replace participant identity",
    )
    matcher = MarketMatcher(EventMatcher(learned_applicator=LearnedMappingApplicator(rules=[rule])))
    mb = MatchbookNormalizer().normalize_market(
        MatchbookNormalizer().normalize_event(MB_EVENT),
        _mb_1x2(),
    )
    pm = PolymarketNormalizer().normalize_market(
        PolymarketNormalizer().normalize_event(
            {
                "id": "pm-swap",
                "title": "Chelsea vs. Liverpool",
                "startTime": KICKOFF.isoformat(),
                "series": [{"title": "Premier League"}],
            }
        ),
        {
            "id": "pm-swap-1x2",
            "question": "Match result?",
            "sportsMarketType": "moneyline",
            "outcomes": '["Chelsea", "Draw", "Liverpool"]',
            "clobTokenIds": '["h", "d", "a"]',
            "description": REGULATION,
        },
    )
    result = matcher.match(mb, pm)
    assert result.matched is False
    assert scan_eligible_pair(mb, pm, result) is False


def test_near_name_competition_fuzz_is_not_paper_eligible() -> None:
    mb = MatchbookNormalizer().normalize_market(
        MatchbookNormalizer().normalize_event(MB_EVENT),
        _mb_1x2(),
    )
    pm = PolymarketNormalizer().normalize_market(
        PolymarketNormalizer().normalize_event(
            {
                **PM_EVENT,
                "id": "pm-pl2",
                "series": [{"title": "Premier League 2"}],
            }
        ),
        _pm_1x2(REGULATION, market_id="pm-pl2-1x2"),
    )
    event = EventMatcher().match(mb.event, pm.event)
    market = MarketMatcher().match(mb, pm)
    assert event.confidence >= 0.92 or "competition_fuzzy" in event.reasons
    assert market.matched is False
    assert "competition_identity_unproven" in market.reasons
    assert scan_eligible_pair(mb, pm, market) is False

    mapped = MarketMatcher().match(
        mb,
        PolymarketNormalizer().normalize_market(
            PolymarketNormalizer().normalize_event(PM_EVENT),
            _pm_1x2(REGULATION, market_id="pm-mapped-pl"),
        ),
    )
    assert mapped.matched is True
    assert "competition_identity_unproven" not in mapped.reasons
