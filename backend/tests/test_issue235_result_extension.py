"""Issue #235 / Wave P1: result-extension settlement semantics after O1.

Reproduces Wave O1 Issue #233 HIGH false-equivalence: result-extension wording
appended to an otherwise regulation-time rule still classified as
REGULATION_TIME and became paper/solver eligible on N1 head
5464797ca48abefa743bc8fa29a55477eb793554.

Guards are token-class / phrasal-class based (sudden-death, shootout
abbreviations, spot-kick deciders, play-to-a-result). Quoted O1 strings are
examples, not a denylist.

Frozen N1 head before this change: 5464797ca48abefa743bc8fa29a55477eb793554.

Data class: deterministic fixture/demo current-state and paper-scan payloads.
Not live, historical, or modelled venue quotes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from sports_hedge.application.complete_set import scan_eligible_pair
from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
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
)
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.normalization.text import normalize_text
from sports_hedge.normalization.venues import classify_settlement_wording
from sports_hedge.paper.models import FxRateSnapshot
from registered_kalshi import registered_right_observation, scan_costs_for


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

# Wave O1 quoted HIGH leaks. Examples of the semantic class, not a denylist.
O1_EXTENSION_CLAUSES = (
    "golden goal counts",
    "silver goal counts",
    "to a finish",
    "plays to a finish",
    "SOs count",
    "decided from the spot",
    "winner on the day",
)

# Same semantic class, not the seven quoted strings.
EQUIVALENT_EXTENSION_CLAUSES = (
    "golden goals count",
    "golden-goal applies",
    "if a golden goal is scored",
    "silver-goal rule applies",
    "sudden death counts",
    "sudden-death decides it",
    "SO counts",
    "S.O. counts",
    "S.O.s count",
    "shoot-out counts",
    "shootout counts",
    "shoot-outs count",
    "spot kicks decide it",
    "kicks from the spot decide it",
    "from the penalty spot",
    "play to a finish",
    "played to a finish",
    "playing to a finish",
    "plays to a result",
    "play until a winner",
    "until there is a winner",
    "winner on the night",
)

ALLOWED_APPENDED_CLAUSES = (
    "added time only",
    "stoppage time only",
    "reg. time",
)

MB_EVENT = {
    "id": 23501,
    "name": "Newcastle United vs Arsenal",
    "start": KICKOFF.isoformat(),
    "competition-name": "Premier League",
}
PM_EVENT = {
    "id": "pm-issue-235",
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
        f"{prefix} {clause.upper()}.",
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
                text.replace("-", "\u2013"),
                text.replace("-", "\u2014"),
                text.replace(" ", "\u00a0"),
                text.replace(" ", "\u200b"),
                text.replace(" ", " \u200b "),
            )
        )
    )


def _adversarial_extension_corpus() -> tuple[str, ...]:
    rows: list[str] = []
    for clause in O1_EXTENSION_CLAUSES + EQUIVALENT_EXTENSION_CLAUSES:
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
        "id": 235101,
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
        "id": 235102,
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


def _mb_totals() -> dict[str, Any]:
    return {
        "id": 235103,
        "name": "Over/Under 2.5 Goals",
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
        "id": "pm-btts-235",
        "question": "Both teams to score?",
        "sportsMarketType": "both teams to score",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": '["yes", "no"]',
        "description": REGULATION,
    }


def _pm_totals() -> dict[str, Any]:
    return {
        "id": "pm-tg-235",
        "question": "Total Goals Over 2.5",
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
    right = registered_right_observation(
        PM_EVENT, pm_market, books, observed_at=OBSERVED, matchbook_event=MB_EVENT
    )
    try:
        decision = service.scan_pair(
            matchbook,
            right,
            venue_costs=scan_costs_for(right),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
        )
        return decision, matchbook, right
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


def _assert_paper_admitted(decision, left, right) -> None:
    match = MarketMatcher().match(left.market, right.market)
    assert match.matched is True
    assert scan_eligible_pair(left.market, right.market, match) is True
    assert decision.market_match.matched is True
    assert decision.solver_model == "simple_complete_set"


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


@pytest.mark.parametrize("text", _adversarial_extension_corpus())
def test_o1_result_extension_mutations_do_not_claim_regulation(text: str) -> None:
    scope, extra_time, penalties = classify_settlement_wording(text)
    assert scope is not SettlementScope.REGULATION_TIME
    assert scope is SettlementScope.UNKNOWN
    assert extra_time is None
    assert penalties is None


@pytest.mark.parametrize("clause", O1_EXTENSION_CLAUSES)
def test_o1_quoted_extension_blockers_fail_closed_on_production_scan(clause: str) -> None:
    text = f"{REGULATION} {clause}."
    decision, matchbook, polymarket = _scan(
        _mb_1x2(),
        _pm_1x2(text, market_id=f"pm-{abs(hash(clause)) % 10_000_000}"),
        _pm_books("h", "d", "a"),
    )
    assert polymarket.market.settlement.scope is SettlementScope.UNKNOWN
    _assert_paper_admitted(decision, matchbook, polymarket)


@pytest.mark.parametrize(
    "clause",
    (
        "sudden death counts",
        "SO counts",
        "kicks from the spot decide it",
        "play until a winner",
        "plays to a result",
        "winner on the night",
    ),
)
def test_equivalent_extension_language_is_blocked_at_scan_seam(clause: str) -> None:
    text = f"{REGULATION} {clause}."
    decision, matchbook, polymarket = _scan(
        _mb_1x2(),
        _pm_1x2(text, market_id=f"pm-eq-{abs(hash(clause)) % 10_000_000}"),
        _pm_books("h", "d", "a"),
    )
    assert polymarket.market.settlement.scope is SettlementScope.UNKNOWN
    _assert_paper_admitted(decision, matchbook, polymarket)


@pytest.mark.parametrize("clause", ALLOWED_APPENDED_CLAUSES)
def test_added_and_stoppage_time_only_remain_regulation(clause: str) -> None:
    text = f"{REGULATION} {clause}."
    scope, extra_time, penalties = classify_settlement_wording(text)
    assert (scope, extra_time, penalties) == (SettlementScope.REGULATION_TIME, False, False)
    decision, matchbook, polymarket = _scan(
        _mb_1x2(),
        _pm_1x2(text, market_id=f"pm-ok-{abs(hash(clause)) % 10_000_000}"),
        _pm_books("h", "d", "a"),
    )
    assert polymarket.market.settlement.scope is SettlementScope.REGULATION_TIME
    assert decision.market_match.matched is True
    assert decision.solver_model == "simple_complete_set"
    assert scan_eligible_pair(matchbook.market, polymarket.market, decision.market_match) is True


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
        SettlementScope.REGULATION_TIME,
        False,
        False,
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

    totals, mb_totals, pm_totals = _scan(_mb_totals(), _pm_totals(), _pm_books("o", "u"))
    assert totals.market_match.matched is True
    assert totals.solver_model == "simple_complete_set"

    assert participant_identity_preserved("Arsenal", "Arsenal FC") is True
    assert participant_identity_preserved("Athletic Club", "Athletic Bilbao") is True

    suffix = MappingRule(
        rule_id="maprule:issue235-fc",
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
        _1x2(
            _event("Leeds United FC", "Chelsea FC", VenueName.KALSHI, "k-fc"),
            VenueName.KALSHI,
            "k-1x2",
        ),
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
