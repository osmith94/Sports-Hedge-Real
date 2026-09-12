from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.complete_set import (
    INCOMPLETE_OUTCOME_REASON,
    STEP7_COMPLETE_SET_FAMILIES,
    SOLVER_INELIGIBLE_REASON,
    solver_eligible_market,
)
from sports_hedge.application.fixture_inventory import (
    InventoryComparisonStatus,
    assemble_fixture_inventory,
    solver_eligible_pair,
)
from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.domain.football import (
    CanonicalOutcome,
    MarketFamily,
    SettlementScope,
    line_push_possible,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.normalization.venues import MatchbookNormalizer, PolymarketNormalizer
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import matchbook_polymarket_costs

from test_fixture_inventory import _inventory, _market


KICKOFF = datetime(2026, 9, 12, 18, 45, tzinfo=UTC)
OBSERVED = datetime(2026, 9, 12, 16, 45, tzinfo=UTC)

MB_EVENT = {
    "id": 7001,
    "name": "Tottenham vs Everton",
    "start": KICKOFF.isoformat(),
    "competition-name": "Premier League",
}
PM_EVENT = {
    "id": "pm-tot-eve-step7",
    "title": "Tottenham vs Everton",
    "startTime": KICKOFF.isoformat(),
    "competition": "Premier League",
}


def _fx() -> list[FxRateSnapshot]:
    return [
        FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test_fx"),
        FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1"), source="functional_currency"),
    ]


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
    intelligence = MarketIntelligenceService(repository)
    service = PaperScanService(intelligence)
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


def test_line_push_possible_rejects_quarter_lines() -> None:
    assert line_push_possible(Decimal("2.0")) is True
    assert line_push_possible(Decimal("2.5")) is False
    assert line_push_possible(Decimal("-0.5")) is False
    assert line_push_possible(Decimal("-1")) is True
    assert line_push_possible(Decimal("2.25")) is None
    assert line_push_possible(Decimal("-0.75")) is None


def test_match_result_still_enters_complete_set_solver() -> None:
    decision, matchbook, _ = _scan(
        {
            "id": 7101,
            "name": "Match Odds",
            "runners": [
                {"id": 1, "name": "Tottenham", "prices": [{"side": "back", "odds": "2.10", "available-amount": "80"}]},
                {"id": 2, "name": "Draw", "prices": [{"side": "back", "odds": "3.40", "available-amount": "80"}]},
                {"id": 3, "name": "Everton", "prices": [{"side": "back", "odds": "3.60", "available-amount": "80"}]},
            ],
        },
        {
            "id": "pm-1x2",
            "question": "Match result?",
            "sportsMarketType": "moneyline",
            "outcomes": '["Tottenham", "Draw", "Everton"]',
            "clobTokenIds": '["h", "d", "a"]',
            "description": "Resolves based on 90 minutes of regulation time.",
        },
        _pm_books("h", "d", "a"),
    )
    assert matchbook.market.family is MarketFamily.MATCH_RESULT
    assert {runner.outcome for runner in matchbook.market.runners} == {
        CanonicalOutcome.HOME,
        CanonicalOutcome.DRAW,
        CanonicalOutcome.AWAY,
    }
    assert decision.market_match.matched is True
    assert decision.depth_scan is not None
    assert "unsupported_outcome_model" not in decision.rejection_reasons
    assert "incomplete_outcome_set" not in decision.rejection_reasons


def test_btts_complete_yes_no_set_enters_solver() -> None:
    decision, matchbook, polymarket = _scan(
        {
            "id": 7102,
            "name": "Both Teams To Score",
            "runners": [
                {"id": 11, "name": "Yes", "prices": [{"side": "back", "odds": "1.90", "available-amount": "50"}]},
                {"id": 12, "name": "No", "prices": [{"side": "back", "odds": "1.95", "available-amount": "50"}]},
            ],
        },
        {
            "id": "pm-btts",
            "question": "Both teams to score?",
            "sportsMarketType": "both teams to score",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["yes", "no"]',
            "description": "Resolves based on 90 minutes of regulation time.",
        },
        _pm_books("yes", "no"),
    )
    assert matchbook.market.family is MarketFamily.BOTH_TEAMS_TO_SCORE
    assert polymarket.market.family is MarketFamily.BOTH_TEAMS_TO_SCORE
    assert {runner.outcome for runner in matchbook.market.runners} == {
        CanonicalOutcome.YES,
        CanonicalOutcome.NO,
    }
    assert decision.market_match.matched is True
    assert decision.depth_scan is not None


def test_totals_different_lines_never_match_exact_line_can_scan() -> None:
    mb_25 = MatchbookNormalizer().normalize_market(
        MatchbookNormalizer().normalize_event(MB_EVENT),
        {
            "id": 7201,
            "name": "Over/Under 2.5 Goals",
            "runners": [{"id": 1, "name": "Over 2.5"}, {"id": 2, "name": "Under 2.5"}],
        },
    )
    pm_30 = PolymarketNormalizer().normalize_market(
        PolymarketNormalizer().normalize_event(PM_EVENT),
        {
            "id": "pm-tg-3",
            "question": "Total goals 3.0",
            "sportsMarketType": "total goals",
            "line": "3.0",
            "outcomes": '["Over", "Under"]',
            "clobTokenIds": '["o", "u"]',
            "description": "Resolves based on 90 minutes of regulation time.",
        },
    )
    pm_25 = PolymarketNormalizer().normalize_market(
        PolymarketNormalizer().normalize_event(PM_EVENT),
        {
            "id": "pm-tg-25",
            "question": "Total goals 2.5",
            "sportsMarketType": "total goals",
            "line": "2.5",
            "outcomes": '["Over", "Under"]',
            "clobTokenIds": '["o25", "u25"]',
            "description": "Resolves based on 90 minutes of regulation time.",
        },
    )
    matcher = MarketMatcher()
    assert matcher.match(mb_25, pm_30).matched is False
    assert "line_mismatch" in matcher.match(mb_25, pm_30).reasons
    assert matcher.match(mb_25, pm_25).matched is True
    decision, _, _ = _scan(
        {
            "id": 7202,
            "name": "Over/Under 2.5 Goals",
            "runners": [
                {"id": 1, "name": "Over 2.5", "prices": [{"side": "back", "odds": "1.90", "available-amount": "40"}]},
                {"id": 2, "name": "Under 2.5", "prices": [{"side": "back", "odds": "1.95", "available-amount": "40"}]},
            ],
        },
        {
            "id": "pm-tg-25-scan",
            "question": "Total goals 2.5",
            "sportsMarketType": "total goals",
            "line": "2.5",
            "outcomes": '["Over", "Under"]',
            "clobTokenIds": '["o25", "u25"]',
            "description": "Resolves based on 90 minutes of regulation time.",
        },
        _pm_books("o25", "u25"),
    )
    assert decision.market_match.matched is True
    assert decision.depth_scan is not None


def test_asian_handicap_requires_exact_line_and_compatible_push() -> None:
    mb = MatchbookNormalizer().normalize_market(
        MatchbookNormalizer().normalize_event(MB_EVENT),
        {
            "id": 7301,
            "name": "Asian Handicap -0.5",
            "line": "-0.5",
            "runners": [{"id": 1, "name": "Tottenham"}, {"id": 2, "name": "Everton"}],
        },
    )
    pm_same = PolymarketNormalizer().normalize_market(
        PolymarketNormalizer().normalize_event(PM_EVENT),
        {
            "id": "pm-ah-05",
            "question": "Asian handicap -0.5",
            "sportsMarketType": "handicap",
            "line": "-0.5",
            "outcomes": '["Tottenham", "Everton"]',
            "clobTokenIds": '["ah-h", "ah-a"]',
            "description": "Resolves based on 90 minutes of regulation time.",
        },
    )
    pm_line = PolymarketNormalizer().normalize_market(
        PolymarketNormalizer().normalize_event(PM_EVENT),
        {
            "id": "pm-ah-1",
            "question": "Asian handicap -1.0",
            "sportsMarketType": "handicap",
            "line": "-1.0",
            "outcomes": '["Tottenham", "Everton"]',
            "clobTokenIds": '["ah1-h", "ah1-a"]',
            "description": "Resolves based on 90 minutes of regulation time.",
        },
    )
    pm_et = PolymarketNormalizer().normalize_market(
        PolymarketNormalizer().normalize_event(PM_EVENT),
        {
            "id": "pm-ah-et",
            "question": "Asian handicap -0.5",
            "sportsMarketType": "handicap",
            "line": "-0.5",
            "outcomes": '["Tottenham", "Everton"]',
            "clobTokenIds": '["ah-h", "ah-a"]',
            "description": "Resolves including extra time.",
        },
    )
    matcher = MarketMatcher()
    assert mb.settlement.push_possible is False
    assert pm_same.settlement.push_possible is False
    assert pm_line.settlement.push_possible is True
    assert matcher.match(mb, pm_same).matched is True
    assert matcher.match(mb, pm_line).matched is False
    assert "line_mismatch" in matcher.match(mb, pm_line).reasons
    assert matcher.match(mb, pm_et).matched is False
    assert "settlement_mismatch" in matcher.match(mb, pm_et).reasons
    quarter = MatchbookNormalizer().normalize_market(
        MatchbookNormalizer().normalize_event(MB_EVENT),
        {
            "id": 7302,
            "name": "Asian Handicap -0.25",
            "line": "-0.25",
            "runners": [{"id": 1, "name": "Tottenham"}, {"id": 2, "name": "Everton"}],
        },
    )
    assert quarter.settlement.push_possible is None
    assert solver_eligible_market(quarter) is False


def test_dnb_complete_space_does_not_require_draw() -> None:
    mb = MatchbookNormalizer().normalize_market(
        MatchbookNormalizer().normalize_event(MB_EVENT),
        {
            "id": 7401,
            "name": "Draw No Bet",
            "runners": [{"id": 1, "name": "Tottenham"}, {"id": 2, "name": "Everton"}],
        },
    )
    pm = PolymarketNormalizer().normalize_market(
        PolymarketNormalizer().normalize_event(PM_EVENT),
        {
            "id": "pm-dnb",
            "question": "Draw no bet",
            "sportsMarketType": "draw no bet",
            "outcomes": '["Tottenham", "Everton"]',
            "clobTokenIds": '["h", "a"]',
            "description": "Resolves based on 90 minutes of regulation time. Draw voids.",
        },
    )
    assert mb.family is MarketFamily.DRAW_NO_BET
    assert mb.settlement.push_possible is True
    assert {runner.outcome for runner in mb.runners} == {CanonicalOutcome.HOME, CanonicalOutcome.AWAY}
    assert CanonicalOutcome.DRAW not in {runner.outcome for runner in mb.runners}
    assert MarketMatcher().match(mb, pm).matched is True
    assert solver_eligible_market(mb) is True
    decision, _, _ = _scan(
        {
            "id": 7402,
            "name": "Draw No Bet",
            "runners": [
                {"id": 1, "name": "Tottenham", "prices": [{"side": "back", "odds": "1.85", "available-amount": "40"}]},
                {"id": 2, "name": "Everton", "prices": [{"side": "back", "odds": "2.05", "available-amount": "40"}]},
            ],
        },
        {
            "id": "pm-dnb-scan",
            "question": "Draw no bet",
            "sportsMarketType": "draw no bet",
            "outcomes": '["Tottenham", "Everton"]',
            "clobTokenIds": '["h", "a"]',
            "description": "Resolves based on 90 minutes of regulation time. Draw voids.",
        },
        _pm_books("h", "a"),
    )
    assert decision.market_match.matched is True
    assert decision.depth_scan is not None


def test_to_qualify_is_distinct_from_regulation_match_result() -> None:
    mb_result = MatchbookNormalizer().normalize_market(
        MatchbookNormalizer().normalize_event(MB_EVENT),
        {
            "id": 7501,
            "name": "Match Odds",
            "runners": [
                {"id": 1, "name": "Tottenham"},
                {"id": 2, "name": "Draw"},
                {"id": 3, "name": "Everton"},
            ],
        },
    )
    mb_qualify = MatchbookNormalizer().normalize_market(
        MatchbookNormalizer().normalize_event(MB_EVENT),
        {
            "id": 7502,
            "name": "To Qualify",
            "runners": [{"id": 1, "name": "Tottenham"}, {"id": 2, "name": "Everton"}],
        },
    )
    pm_qualify = PolymarketNormalizer().normalize_market(
        PolymarketNormalizer().normalize_event(PM_EVENT),
        {
            "id": "pm-qualify",
            "question": "Who will to qualify?",
            "sportsMarketType": "to qualify",
            "outcomes": '["Tottenham", "Everton"]',
            "clobTokenIds": '["qh", "qa"]',
            "description": "Resolves including penalties after extra time.",
        },
    )
    pm_regulation = PolymarketNormalizer().normalize_market(
        PolymarketNormalizer().normalize_event(PM_EVENT),
        {
            "id": "pm-qualify-90",
            "question": "Who will to qualify?",
            "sportsMarketType": "to qualify",
            "outcomes": '["Tottenham", "Everton"]',
            "clobTokenIds": '["qh", "qa"]',
            "description": "Resolves based on 90 minutes of regulation time.",
        },
    )
    assert mb_qualify.family is MarketFamily.TO_QUALIFY
    assert mb_qualify.settlement.scope is SettlementScope.INCLUDING_PENALTIES
    assert mb_result.settlement.scope is SettlementScope.REGULATION_TIME
    assert {runner.outcome for runner in mb_qualify.runners} == {
        CanonicalOutcome.HOME_QUALIFY,
        CanonicalOutcome.AWAY_QUALIFY,
    }
    matcher = MarketMatcher()
    assert matcher.match(mb_result, mb_qualify).matched is False
    assert "market_family_mismatch" in matcher.match(mb_result, pm_qualify).reasons
    assert matcher.match(mb_qualify, pm_qualify).matched is True
    assert matcher.match(mb_qualify, pm_regulation).matched is False
    assert solver_eligible_market(pm_regulation) is False


def test_incomplete_runner_sets_fail_closed() -> None:
    incomplete = _market(
        VenueName.MATCHBOOK,
        family=MarketFamily.MATCH_RESULT,
        source_id="mb-home-away-only",
        outcomes=[CanonicalOutcome.HOME, CanonicalOutcome.AWAY],
    )
    complete = _market(
        VenueName.POLYMARKET,
        family=MarketFamily.MATCH_RESULT,
        source_id="pm-1x2",
    )
    assert solver_eligible_market(incomplete) is False
    match = MarketMatcher().match(incomplete, complete)
    assert match.matched is False
    assert "outcome_space_mismatch" in match.reasons
    yes_no = MatchbookNormalizer().normalize_market(
        MatchbookNormalizer().normalize_event(MB_EVENT),
        {
            "id": 7601,
            "name": "Match Odds",
            "runners": [{"id": 1, "name": "Yes"}, {"id": 2, "name": "No"}],
        },
    )
    assert yes_no.family is MarketFamily.MATCH_RESULT
    assert solver_eligible_market(yes_no) is False
    assert solver_ineligibility_is_incomplete(yes_no)


def solver_ineligibility_is_incomplete(market) -> bool:
    from sports_hedge.application.complete_set import solver_ineligibility_reason

    return solver_ineligibility_reason(market) == INCOMPLETE_OUTCOME_REASON


def test_unsupported_conventional_markets_stay_visible() -> None:
    next_goal = MatchbookNormalizer().normalize_market(
        MatchbookNormalizer().normalize_event(MB_EVENT),
        {
            "id": 7701,
            "name": "Next Goal",
            "runners": [
                {"id": 1, "name": "Tottenham"},
                {"id": 2, "name": "Everton"},
                {"id": 3, "name": "No Goal"},
            ],
        },
    )
    double_chance = MatchbookNormalizer().normalize_market(
        MatchbookNormalizer().normalize_event(MB_EVENT),
        {
            "id": 7702,
            "name": "Double Chance",
            "runners": [
                {"id": 1, "name": "Home or Draw"},
                {"id": 2, "name": "Home or Away"},
                {"id": 3, "name": "Draw or Away"},
            ],
        },
    )
    assert next_goal.family is MarketFamily.NEXT_GOAL
    assert next_goal.family not in STEP7_COMPLETE_SET_FAMILIES
    assert solver_eligible_market(next_goal) is False
    assert solver_eligible_market(double_chance) is False
    rows = assemble_fixture_inventory(
        [
            _inventory(next_goal, name="Next Goal"),
            _inventory(double_chance, name="Double Chance"),
            _inventory(
                _market(
                    VenueName.MATCHBOOK,
                    family=MarketFamily.CORRECT_SCORE,
                    source_id="mb-cs",
                    outcomes=[CanonicalOutcome.OTHER, CanonicalOutcome.OTHER],
                ),
                name="Correct Score",
            ),
        ],
        [],
    )
    assert len(rows) == 3
    assert all(row.entered_solver is False for row in rows)
    assert all(
        row.comparison_status is InventoryComparisonStatus.UNSUPPORTED_OUTCOME_MODEL for row in rows
    )
    assert all(row.reason for row in rows)


def test_correct_score_and_other_remain_excluded() -> None:
    correct = _market(
        VenueName.MATCHBOOK,
        family=MarketFamily.CORRECT_SCORE,
        source_id="mb-cs",
        outcomes=[CanonicalOutcome.OTHER, CanonicalOutcome.OTHER],
    )
    pm_correct = _market(
        VenueName.POLYMARKET,
        family=MarketFamily.CORRECT_SCORE,
        source_id="pm-cs",
        outcomes=[CanonicalOutcome.OTHER, CanonicalOutcome.OTHER],
    )
    matcher = MarketMatcher()
    assert solver_eligible_pair(correct, pm_correct, matcher.match(correct, pm_correct)) is False
    assert CanonicalOutcome.OTHER in {runner.outcome for runner in correct.runners}


def test_matchbook_lays_are_not_passed_into_step7_solver() -> None:
    decision, matchbook, _ = _scan(
        {
            "id": 7801,
            "name": "Both Teams To Score",
            "runners": [
                {
                    "id": 11,
                    "name": "Yes",
                    "prices": [
                        {"side": "back", "odds": "1.40", "available-amount": "50"},
                        {"side": "lay", "odds": "1.10", "available-amount": "5000"},
                    ],
                },
                {
                    "id": 12,
                    "name": "No",
                    "prices": [
                        {"side": "back", "odds": "1.40", "available-amount": "50"},
                        {"side": "lay", "odds": "1.10", "available-amount": "5000"},
                    ],
                },
            ],
        },
        {
            "id": "pm-btts-lays",
            "question": "Both teams to score?",
            "sportsMarketType": "both teams to score",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["yes", "no"]',
            "description": "Resolves based on 90 minutes of regulation time.",
        },
        {
            "yes": {
                "asset_id": "yes",
                "asks": [{"price": "0.70", "size": "50"}],
                "bids": [{"price": "0.10", "size": "5000"}],
            },
            "no": {
                "asset_id": "no",
                "asks": [{"price": "0.70", "size": "50"}],
                "bids": [{"price": "0.10", "size": "5000"}],
            },
        },
    )
    yes_book = matchbook.book_for(CanonicalOutcome.YES)
    assert yes_book is not None
    assert yes_book.best_lay is not None
    assert yes_book.best_lay.decimal_odds == Decimal("1.10")
    assert decision.depth_scan is not None
    for quote in decision.depth_scan.selected_quotes:
        assert quote.net_decimal_odds > Decimal("1.2")
        assert quote.venue in {VenueName.MATCHBOOK, VenueName.POLYMARKET}
    implied = decision.depth_scan.solution.implied_probability_sum
    assert implied > Decimal("1") or decision.depth_scan.solution.is_arbitrage is False


def test_fees_fx_depth_freshness_and_paper_boundary_unchanged() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    service = PaperScanService(intelligence)
    matchbook = MatchbookObservationBuilder().build(
        MB_EVENT,
        {
            "id": 7901,
            "name": "Match Odds",
            "runners": [
                {"id": 1, "name": "Tottenham", "prices": [{"side": "back", "odds": "2.10", "available-amount": "80"}]},
                {"id": 2, "name": "Draw", "prices": [{"side": "back", "odds": "3.40", "available-amount": "80"}]},
                {"id": 3, "name": "Everton", "prices": [{"side": "back", "odds": "3.60", "available-amount": "80"}]},
            ],
        },
        observed_at=OBSERVED,
        quote_age_ms=120,
    )
    polymarket = PolymarketObservationBuilder().build(
        PM_EVENT,
        {
            "id": "pm-1x2-gates",
            "question": "Match result?",
            "sportsMarketType": "moneyline",
            "outcomes": '["Tottenham", "Draw", "Everton"]',
            "clobTokenIds": '["h", "d", "a"]',
            "description": "Resolves based on 90 minutes of regulation time.",
        },
        _pm_books("h", "d", "a"),
        observed_at=OBSERVED,
        quote_age_ms=150,
    )
    try:
        missing_costs = service.scan_pair(matchbook, polymarket, fx_snapshots=_fx(), maximum_execution_risk=100)
        assert any(reason.startswith("missing_venue_cost") for reason in missing_costs.rejection_reasons)
        assert missing_costs.eligible_for_paper_simulation is False
        missing_fx = service.scan_pair(
            matchbook,
            polymarket,
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=[FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1"))],
            maximum_execution_risk=100,
        )
        assert any(reason.startswith("missing_fx_rate") for reason in missing_fx.rejection_reasons)
        stale = service.scan_pair(
            matchbook.model_copy(update={"quote_age_ms": None, "metadata": {"quote_age_reason": "unknown_quote_age"}}),
            polymarket,
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
        )
        assert "unknown_quote_age" in stale.rejection_reasons
    finally:
        repository.close()
    from sports_hedge.config import get_settings

    settings = get_settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False


class MultiFamilyMatchbook:
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {
            "events": [
                {
                    "id": 7001,
                    "name": "Tottenham vs Everton",
                    "start": KICKOFF.isoformat(),
                    "competition-name": "Premier League",
                    "status": "open",
                    "in-running-flag": False,
                }
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        back = {"side": "back", "odds": "1.90", "available-amount": "80"}
        return {
            "markets": [
                {
                    "id": 8101,
                    "name": "Match Odds",
                    "runners": [
                        {"id": 1, "name": "Tottenham", "prices": [back]},
                        {"id": 2, "name": "Draw", "prices": [back]},
                        {"id": 3, "name": "Everton", "prices": [back]},
                    ],
                },
                {
                    "id": 8102,
                    "name": "Both Teams To Score",
                    "runners": [
                        {"id": 11, "name": "Yes", "prices": [back]},
                        {"id": 12, "name": "No", "prices": [back]},
                    ],
                },
                {
                    "id": 8103,
                    "name": "Over/Under 2.5 Goals",
                    "runners": [
                        {"id": 21, "name": "Over 2.5", "prices": [back]},
                        {"id": 22, "name": "Under 2.5", "prices": [back]},
                    ],
                },
                {
                    "id": 8104,
                    "name": "Over/Under 3.5 Goals",
                    "runners": [
                        {"id": 23, "name": "Over 3.5", "prices": [back]},
                        {"id": 24, "name": "Under 3.5", "prices": [back]},
                    ],
                },
                {
                    "id": 8105,
                    "name": "Draw No Bet",
                    "runners": [
                        {"id": 31, "name": "Tottenham", "prices": [back]},
                        {"id": 32, "name": "Everton", "prices": [back]},
                    ],
                },
                {
                    "id": 8106,
                    "name": "Asian Handicap -0.5",
                    "line": "-0.5",
                    "runners": [
                        {"id": 41, "name": "Tottenham", "prices": [back]},
                        {"id": 42, "name": "Everton", "prices": [back]},
                    ],
                },
                {
                    "id": 8107,
                    "name": "To Qualify",
                    "runners": [
                        {"id": 51, "name": "Tottenham", "prices": [back]},
                        {"id": 52, "name": "Everton", "prices": [back]},
                    ],
                },
                {
                    "id": 8108,
                    "name": "Correct Score",
                    "runners": [{"id": 61, "name": "1-0", "prices": []}, {"id": 62, "name": "2-0", "prices": []}],
                },
                {
                    "id": 8109,
                    "name": "Next Goal",
                    "runners": [
                        {"id": 71, "name": "Tottenham", "prices": [back]},
                        {"id": 72, "name": "Everton", "prices": [back]},
                    ],
                },
                {
                    "id": 8110,
                    "name": "First Team To Score",
                    "runners": [{"id": 81, "name": "Tottenham", "prices": [back]}],
                },
            ]
        }


class MultiFamilyPolymarket:
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return [PM_EVENT]

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        regulation = "Resolves based on 90 minutes of regulation time."
        return [
            {
                "id": "pm-1x2",
                "question": "Match result?",
                "sportsMarketType": "moneyline",
                "outcomes": '["Tottenham", "Draw", "Everton"]',
                "clobTokenIds": '["h", "d", "a"]',
                "description": regulation,
            },
            {
                "id": "pm-btts",
                "question": "Both teams to score?",
                "sportsMarketType": "both teams to score",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["yes", "no"]',
                "description": regulation,
            },
            {
                "id": "pm-tg-25",
                "question": "Total goals 2.5",
                "sportsMarketType": "total goals",
                "line": "2.5",
                "outcomes": '["Over", "Under"]',
                "clobTokenIds": '["o25", "u25"]',
                "description": regulation,
            },
            {
                "id": "pm-tg-35",
                "question": "Total goals 3.5",
                "sportsMarketType": "total goals",
                "line": "3.5",
                "outcomes": '["Over", "Under"]',
                "clobTokenIds": '["o35", "u35"]',
                "description": regulation,
            },
            {
                "id": "pm-dnb",
                "question": "Draw no bet",
                "sportsMarketType": "draw no bet",
                "outcomes": '["Tottenham", "Everton"]',
                "clobTokenIds": '["dnb-h", "dnb-a"]',
                "description": regulation,
            },
            {
                "id": "pm-ah",
                "question": "Asian handicap -0.5",
                "sportsMarketType": "handicap",
                "line": "-0.5",
                "outcomes": '["Tottenham", "Everton"]',
                "clobTokenIds": '["ah-h", "ah-a"]',
                "description": regulation,
            },
            {
                "id": "pm-qualify",
                "question": "Who will to qualify?",
                "sportsMarketType": "to qualify",
                "outcomes": '["Tottenham", "Everton"]',
                "clobTokenIds": '["qh", "qa"]',
                "description": "Resolves including penalties after extra time.",
            },
            {
                "id": "pm-cs",
                "question": "Correct score?",
                "sportsMarketType": "correct score",
                "outcomes": '["1-0", "2-0"]',
                "clobTokenIds": '["cs1", "cs2"]',
                "description": regulation,
            },
        ]

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id, filters
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        return {
            "asset_id": str(outcome_id),
            "timestamp": now_ms - 120,
            "bids": [{"price": "0.40", "size": "100"}],
            "asks": [{"price": "0.42", "size": "100"}],
        }


@pytest.mark.asyncio
async def test_one_fixture_scans_every_supported_equivalent_pair() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    collector = ReadOnlyCrossVenueCollector(
        matchbook=MultiFamilyMatchbook(),
        polymarket=MultiFamilyPolymarket(),
        paper_scan=PaperScanService(intelligence),
    )
    try:
        report = await collector.collect_and_scan(
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
        )
        fixture = report.discovered_fixtures[0]
        rows = report.fixture_markets[fixture.canonical_event_id]
        scanned_families = {
            row.family for row in rows if row.entered_solver and row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT
        }
        assert scanned_families >= {
            "match_result",
            "both_teams_to_score",
            "total_goals",
            "draw_no_bet",
            "asian_handicap",
            "to_qualify",
        }
        totals = [row for row in rows if row.family == "total_goals" and row.entered_solver]
        assert {row.line for row in totals} == {Decimal("2.5"), Decimal("3.5")}
        next_goal = next(row for row in rows if row.family == "next_goal" or row.display_name == "Next Goal")
        assert next_goal.entered_solver is False
        assert next_goal.reason
        cs = next(row for row in rows if row.family == "correct_score")
        assert cs.entered_solver is False
        assert cs.comparison_status is InventoryComparisonStatus.UNSUPPORTED_OUTCOME_MODEL
        first_team = next(
            row for row in rows if "First Team To Score" in row.display_name or "first team" in (row.display_name or "").lower()
        )
        assert first_team.entered_solver is False
        assert first_team.comparison_status is InventoryComparisonStatus.UNSUPPORTED_FAMILY
        scanned_ids = {(d.canonical_market_id) for d in report.paper_decisions}
        assert len(scanned_ids) >= 7
        for decision in report.paper_decisions:
            assert SOLVER_INELIGIBLE_REASON not in decision.rejection_reasons
            assert "noncanonical_outcome_space" not in decision.rejection_reasons
            if decision.depth_scan is not None:
                for quote in decision.depth_scan.selected_quotes:
                    assert quote.net_decimal_odds > 1
    finally:
        repository.close()


def test_first_team_to_score_is_not_normalized_into_step7() -> None:
    event = MatchbookNormalizer().normalize_event(MB_EVENT)
    with pytest.raises(Exception, match="Unsupported Matchbook market"):
        MatchbookNormalizer().normalize_market(
            event,
            {"id": 999, "name": "First Team To Score", "runners": [{"id": 1, "name": "Tottenham"}]},
        )
