"""Issue #225 / Wave N1: adversarial fail-closed equivalence hardening.

Reproduces Wave M1 Issue #219 false-equivalence classes against production
normalization, matching, eligibility and solver seams. Guards are token-class
and polarity based; quoted strings are examples, not a denylist.

Frozen K1 head before this change: 80d0530b21f61be1c916ecee35d3465d6b102aab.

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
    MappingSideEvidence,
    infer_learned_rule,
    participant_identity_preserved,
    squad_categories_compatible,
)
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.normalization.text import normalize_text
from sports_hedge.normalization.venues import (
    MatchbookNormalizer,
    PolymarketNormalizer,
    VenueNormalizationError,
    classify_settlement_wording,
)
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import matchbook_polymarket_costs


KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
OBSERVED = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)
REGULATION = "Resolves based on 90 minutes of regulation time."
REGULATION_EXPLICIT = (
    "Resolves on 90 minutes of regulation time. Extra time and penalties do not count."
)
ET_ONLY = "Resolves including extra time."
ET_AND_PENALTIES = "Winner including extra time and penalties."
GAMMA_POSTPONE = (
    "If the game is postponed, this market will remain open until the game has been completed. "
    "If the game is canceled entirely, with no make-up game, this market will resolve No. "
    "This market refers only to the outcome within the first 90 minutes of regular play plus stoppage time."
)

ABBREV_EXTENSION_CLAUSES = (
    "AET counts",
    "A.E.T. counts",
    "aet count",
    "PKs count",
    "P.K.s count",
    "includes penalties",
    "after 120 minutes",
    "after 120 mins",
    "ET counts",
)

DOUBLE_NEGATION_CLAUSES = (
    "not excluding extra time",
    "not excluding extra-time",
    "does not exclude extra time",
    "not without extra time",
    "never excluding penalties",
)

INVALIDATION_CLAUSES = (
    "If the match is called off, all bets are refunded",
    "If the game is called off all bets are refunded",
    "All bets are refunded if the fixture is not completed",
    "If called off, stakes returned",
)

TEAM_TOTAL_SHORTHAND = (
    "O/U 2.5 - Home",
    "Over/Under 2.5 Goals - Home",
    "Team goals over 2.5",
    "Home Over/Under 2.5 Goals",
    "Total Goals Over 2.5 - Home",
    "O/U 2.5 Goals Home",
    "Away team goals over 2.5",
    "Home O/U 2.5",
)

SQUAD_VARIANTS = (
    ("Arsenal", "Arsenal U21"),
    ("Arsenal", "Arsenal Women"),
    ("Arsenal", "Arsenal Academy"),
    ("Arsenal", "Arsenal Reserves"),
    ("Arsenal", "Arsenal W"),
    ("Chelsea", "Chelsea U23"),
    ("Chelsea", "Chelsea under 21"),
    ("Barcelona", "Barcelona B"),
)

MB_EVENT = {
    "id": 22501,
    "name": "Newcastle United vs Arsenal",
    "start": KICKOFF.isoformat(),
    "competition-name": "Premier League",
}
PM_EVENT = {
    "id": "pm-issue-225",
    "title": "Newcastle United vs. Arsenal",
    "startTime": KICKOFF.isoformat(),
    "series": [{"title": "Premier League"}],
}


def _clause_orders(prefix: str, clause: str) -> tuple[str, ...]:
    clause = clause.strip().rstrip(".")
    return (
        f"{prefix} {clause}.",
        f"{clause}. {prefix}",
        f"{prefix} {clause.lower()}.",
    )


def _punct_mutations(text: str) -> tuple[str, ...]:
    normalized_spaces = " ".join(text.split())
    return tuple(
        dict.fromkeys(
            (
                text,
                normalized_spaces,
                text.replace("-", " "),
                text.replace(".", " "),
                text.replace(",", " "),
            )
        )
    )


def _adversarial_settlement_corpus() -> tuple[str, ...]:
    rows: list[str] = []
    for clause in ABBREV_EXTENSION_CLAUSES + DOUBLE_NEGATION_CLAUSES + INVALIDATION_CLAUSES:
        for ordered in _clause_orders(REGULATION, clause):
            rows.extend(_punct_mutations(ordered))
    return tuple(dict.fromkeys(rows))


def _fx() -> list[FxRateSnapshot]:
    return [
        FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test_fx"),
        FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1"), source="functional_currency"),
    ]


def _mb_1x2() -> dict[str, Any]:
    return {
        "id": 225101,
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
        "id": 225102,
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
        "id": 225103,
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


def _pm_1x2(description: str, *, market_id: str = "pm-1x2") -> dict[str, Any]:
    return {
        "id": market_id,
        "question": "Match result?",
        "sportsMarketType": "moneyline",
        "outcomes": '["Newcastle United", "Draw", "Arsenal"]',
        "clobTokenIds": '["h", "d", "a"]',
        "description": description,
    }


def _pm_btts() -> dict[str, Any]:
    return {
        "id": "pm-btts-225",
        "question": "Both teams to score?",
        "sportsMarketType": "both teams to score",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["yes", "no"]',
        "description": REGULATION,
    }


def _pm_totals(question: str, *, market_id: str = "pm-tg") -> dict[str, Any]:
    return {
        "id": market_id,
        "question": question,
        "sportsMarketType": "total goals",
        "line": "2.5",
        "outcomes": '["Over", "Under"]',
        "clobTokenIds": '["o", "u"]',
        "description": REGULATION,
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


def _event(home: str, away: str, venue: VenueName, source: str) -> CanonicalEvent:
    return CanonicalEvent(
        competition="Premier League",
        home_team=home,
        away_team=away,
        kickoff_utc=KICKOFF,
        source_venue=venue,
        source_event_id=source,
    )


def _1x2(event: CanonicalEvent, venue: VenueName, market_id: str) -> CanonicalMarket:
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
    return CanonicalMarket(
        event=event,
        source_venue=venue,
        source_market_id=market_id,
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        settlement=settlement,
        runners=runners,
    )


def _side(venue: VenueName, home: str, away: str, source_event_id: str) -> MappingSideEvidence:
    return MappingSideEvidence(
        venue=venue,
        source_event_id=source_event_id,
        source_market_id=f"{source_event_id}-1x2",
        raw_home_team=home,
        raw_away_team=away,
        raw_competition="Premier League",
        kickoff_utc=KICKOFF,
        family="match_result",
        period="full_time",
        settlement_key="regulation_time|full_time",
    )


@pytest.mark.parametrize("text", _adversarial_settlement_corpus())
def test_m1_settlement_mutations_do_not_claim_regulation(text: str) -> None:
    scope, extra_time, penalties = classify_settlement_wording(text)
    assert scope is not SettlementScope.REGULATION_TIME
    assert scope is SettlementScope.UNKNOWN
    assert extra_time is None
    assert penalties is None


@pytest.mark.parametrize(
    "text",
    (
        f"{REGULATION} AET counts.",
        f"{REGULATION} PKs count.",
        f"{REGULATION} Includes penalties.",
        f"{REGULATION} Settles after 120 minutes.",
        f"{REGULATION} This market is not excluding extra time.",
        f"{REGULATION} If the match is called off, all bets are refunded.",
    ),
)
def test_m1_quoted_blockers_fail_closed_on_production_scan(text: str) -> None:
    decision, matchbook, polymarket = _scan(
        _mb_1x2(),
        _pm_1x2(text, market_id=f"pm-{abs(hash(text)) % 10_000_000}"),
        _pm_books("h", "d", "a"),
    )
    assert polymarket.market.settlement.scope is SettlementScope.UNKNOWN
    _assert_not_in_solver(decision, matchbook, polymarket)


@pytest.mark.parametrize("title", TEAM_TOTAL_SHORTHAND)
def test_team_total_shorthand_is_never_match_total(title: str) -> None:
    pm_event = PolymarketNormalizer().normalize_event(PM_EVENT)
    pm_market = PolymarketNormalizer().normalize_market(pm_event, _pm_totals(title, market_id=f"pm-{title}"))
    assert pm_market.family is MarketFamily.TEAM_TOTAL
    assert solver_eligible_market(pm_market) is False

    mb_event = MatchbookNormalizer().normalize_event(MB_EVENT)
    try:
        mb_market = MatchbookNormalizer().normalize_market(mb_event, _mb_totals(title))
    except VenueNormalizationError:
        return
    assert mb_market.family is not MarketFamily.TOTAL_GOALS
    assert mb_market.family is MarketFamily.TEAM_TOTAL
    assert solver_eligible_market(mb_market) is False


def test_team_total_shorthand_does_not_join_match_total_or_enter_solver() -> None:
    decision, matchbook, polymarket = _scan(
        _mb_totals(),
        _pm_totals("Over/Under 2.5 Goals - Home", market_id="pm-home-ou"),
        _pm_books("o", "u"),
    )
    assert matchbook.market.family is MarketFamily.TOTAL_GOALS
    assert polymarket.market.family is MarketFamily.TEAM_TOTAL
    _assert_not_in_solver(decision, matchbook, polymarket)


@pytest.mark.parametrize(("senior", "variant"), SQUAD_VARIANTS)
def test_squad_category_is_not_identity_preserved(senior: str, variant: str) -> None:
    assert participant_identity_preserved(senior, variant) is False
    assert participant_identity_preserved(variant, senior) is False
    assert squad_categories_compatible(senior, variant) is False

    event = EventMatcher().match(
        _event(senior, "Chelsea", VenueName.MATCHBOOK, "mb-squad"),
        _event(variant, "Chelsea", VenueName.POLYMARKET, "pm-squad"),
    )
    assert event.matched is False
    assert "participant_squad_category_mismatch" in event.reasons

    market = MarketMatcher().match(
        _1x2(_event(senior, "Chelsea", VenueName.MATCHBOOK, "mb-squad"), VenueName.MATCHBOOK, "mb-1x2"),
        _1x2(_event(variant, "Chelsea", VenueName.POLYMARKET, "pm-squad"), VenueName.POLYMARKET, "pm-1x2"),
    )
    assert market.matched is False
    assert scan_eligible_pair(
        _1x2(_event(senior, "Chelsea", VenueName.MATCHBOOK, "mb-squad"), VenueName.MATCHBOOK, "mb-1x2"),
        _1x2(_event(variant, "Chelsea", VenueName.POLYMARKET, "pm-squad"), VenueName.POLYMARKET, "pm-1x2"),
        market,
    ) is False

    inferred = infer_learned_rule(
        _side(VenueName.POLYMARKET, variant, "Chelsea", "pm-squad"),
        _side(VenueName.MATCHBOOK, senior, "Chelsea", "mb-squad"),
        operator="oliver",
        source=MappingRuleSource.OPERATOR_MANUAL,
        evidence="must not collapse squad category",
    )
    assert inferred is None


def test_learned_rule_cannot_strip_squad_category_or_swap_opponents() -> None:
    u21_rule = MappingRule(
        rule_id="maprule:issue225-u21",
        created_at=OBSERVED,
        operator="oliver",
        source=MappingRuleSource.OPERATOR_MANUAL,
        rule_type=MappingRuleType.VENUE_SUFFIX_STRIP,
        venue=VenueName.POLYMARKET,
        field_scope=MappingFieldScope.TEAM_NAME,
        raw_pattern="u21",
        canonical_transformation="strip_suffix",
        guardrails=MappingGuardrails(sport="football", competition_code="premier_league"),
        evidence="U21 is not a safe FC-style token",
    )
    matcher = MarketMatcher(EventMatcher(learned_applicator=LearnedMappingApplicator(rules=[u21_rule])))
    result = matcher.match(
        _1x2(_event("Arsenal", "Chelsea", VenueName.MATCHBOOK, "mb-u21"), VenueName.MATCHBOOK, "mb-1x2"),
        _1x2(_event("Arsenal U21", "Chelsea", VenueName.POLYMARKET, "pm-u21"), VenueName.POLYMARKET, "pm-1x2"),
    )
    assert result.matched is False

    opponent = infer_learned_rule(
        _side(VenueName.POLYMARKET, "Chelsea", "Liverpool", "pm-opp"),
        _side(VenueName.MATCHBOOK, "Newcastle United", "Arsenal", "mb-opp"),
        operator="oliver",
        source=MappingRuleSource.OPERATOR_MANUAL,
        evidence="must not authorize opponent replacement",
    )
    assert opponent is None


def test_positive_controls_remain_paper_eligible() -> None:
    assert classify_settlement_wording(REGULATION) == (
        SettlementScope.REGULATION_TIME,
        False,
        False,
    )
    assert classify_settlement_wording(REGULATION_EXPLICIT) == (
        SettlementScope.REGULATION_TIME,
        False,
        False,
    )
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
    assert classify_settlement_wording("Resolves not including extra time.") == (
        SettlementScope.UNKNOWN,
        None,
        None,
    )
    assert classify_settlement_wording(GAMMA_POSTPONE) == (
        SettlementScope.REGULATION_TIME,
        False,
        False,
    )

    one_x_two, mb_1x2, pm_1x2 = _scan(_mb_1x2(), _pm_1x2(REGULATION), _pm_books("h", "d", "a"))
    assert one_x_two.market_match.matched is True
    assert one_x_two.solver_model == "simple_complete_set"
    assert scan_eligible_pair(mb_1x2.market, pm_1x2.market, one_x_two.market_match) is True

    btts, _mb_btts_obs, _pm_btts_obs = _scan(_mb_btts(), _pm_btts(), _pm_books("yes", "no"))
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

    assert participant_identity_preserved("Arsenal", "Arsenal FC") is True
    assert participant_identity_preserved("Athletic Club", "Athletic Bilbao") is True
    assert participant_identity_preserved("Leeds United", "Leeds United FC") is True

    suffix = MappingRule(
        rule_id="maprule:issue225-fc",
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
    safe = MarketMatcher(
        EventMatcher(learned_applicator=LearnedMappingApplicator(rules=[suffix]))
    ).match(
        _1x2(_event("Leeds United", "Chelsea", VenueName.MATCHBOOK, "mb-fc"), VenueName.MATCHBOOK, "mb-1x2"),
        _1x2(_event("Leeds United FC", "Chelsea FC", VenueName.POLYMARKET, "pm-fc"), VenueName.POLYMARKET, "pm-1x2"),
    )
    assert safe.matched is True

    fc_rule = infer_learned_rule(
        _side(VenueName.POLYMARKET, "Leeds United FC", "Chelsea FC", "pm-fc-inf"),
        _side(VenueName.MATCHBOOK, "Leeds United", "Chelsea", "mb-fc-inf"),
        operator="oliver",
        source=MappingRuleSource.OPERATOR_MANUAL,
        evidence="operator confirmed same Premier League fixture",
    )
    assert fc_rule is not None
    assert fc_rule.rule_type is MappingRuleType.VENUE_SUFFIX_STRIP
    assert normalize_text(fc_rule.raw_pattern) == "fc"
