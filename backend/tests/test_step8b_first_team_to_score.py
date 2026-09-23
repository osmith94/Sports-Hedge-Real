from __future__ import annotations

from decimal import Decimal

import pytest

from sports_hedge.application.complete_set import (
    FIRST_TEAM_TO_SCORE_STATES,
    INCOMPLETE_OUTCOME_REASON,
    SOLVER_MODEL_GENERALIZED,
    SOLVER_MODEL_SIMPLE,
    STEP7_COMPLETE_SET_FAMILIES,
    UNSUPPORTED_STATE_PAYOFF_FEE_BASIS,
    GeneralizedStateModel,
    generalized_payoff_eligible_market,
    scan_ineligibility_reason,
    solver_eligible_market,
)
from sports_hedge.application.fixture_inventory import assemble_fixture_inventory
from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)
from sports_hedge.application.paper_scan import PaperScanService, _fill_legs_from_observations
from sports_hedge.arbitrage.depth import DepthQuoteCandidate, DepthQuoteSource
from sports_hedge.arbitrage.models import PayoffProblem, PayoffSolution, PayoffStake
from sports_hedge.arbitrage.payoff_scan import DepthAwarePayoffScanner, PayoffScanResult
from sports_hedge.arbitrage.payoff_solver import GeneralizedMaxMinSolver, payoff_leg_from_back
from sports_hedge.domain.football import CanonicalOutcome, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import CostKnownStatus, FeeBasis, FeeScope, MarketAction, OrderRole, VenueCostSnapshot
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.normalization.venues import MatchbookNormalizer, PolymarketNormalizer, VenueNormalizationError

from test_fixture_inventory import _inventory
from test_step7_safe_market_expansion import MB_EVENT, OBSERVED, PM_EVENT, _fx, _scan
from venue_cost_helpers import matchbook_polymarket_costs


def _ftts_mb_payload(*, market_id: int = 9601, include_no_goal: bool = True, name: str = "First Team To Score"):
    runners = [
        {"id": 1, "name": "Tottenham", "prices": [{"side": "back", "odds": "2.20", "available-amount": "80"}]},
        {"id": 2, "name": "Everton", "prices": [{"side": "back", "odds": "3.50", "available-amount": "80"}]},
    ]
    if include_no_goal:
        runners.append(
            {"id": 3, "name": "No Goal", "prices": [{"side": "back", "odds": "4.50", "available-amount": "80"}]}
        )
    return {"id": market_id, "name": name, "runners": runners}


def _ftts_pm_payload(
    *,
    market_id: str = "pm-ftts-8b",
    include_no_goal: bool = True,
    question: str = "First team to score",
    sports_type: str = "first team to score",
    description: str = "Resolves based on 90 minutes of regulation time.",
):
    if include_no_goal:
        outcomes = '["Tottenham", "Everton", "No Goal"]'
        tokens = '["h", "a", "n"]'
    else:
        outcomes = '["Tottenham", "Everton"]'
        tokens = '["h", "a"]'
    return {
        "id": market_id,
        "question": question,
        "sportsMarketType": sports_type,
        "outcomes": outcomes,
        "clobTokenIds": tokens,
        "description": description,
    }


def _ftts_books(*, include_no_goal: bool = True) -> dict[str, dict[str, object]]:
    books: dict[str, dict[str, object]] = {
        "h": {"asset_id": "h", "asks": [{"price": "0.28", "size": "200"}], "bids": [{"price": "0.26", "size": "200"}]},
        "a": {"asset_id": "a", "asks": [{"price": "0.28", "size": "200"}], "bids": [{"price": "0.26", "size": "200"}]},
    }
    if include_no_goal:
        books["n"] = {
            "asset_id": "n",
            "asks": [{"price": "0.22", "size": "200"}],
            "bids": [{"price": "0.20", "size": "200"}],
        }
    return books


def test_matchbook_team_level_first_team_to_score_normalizes_home_away_no_goal() -> None:
    event = MatchbookNormalizer().normalize_event(MB_EVENT)
    market = MatchbookNormalizer().normalize_market(event, _ftts_mb_payload())
    assert market.family is MarketFamily.FIRST_TEAM_TO_SCORE
    assert [runner.outcome for runner in market.runners] == [
        CanonicalOutcome.HOME,
        CanonicalOutcome.AWAY,
        CanonicalOutcome.NO_GOAL,
    ]
    assert market.settlement.push_possible is False
    assert market.settlement.extra_time_included is False
    assert market.settlement.penalties_included is False
    neither = MatchbookNormalizer().normalize_market(
        event,
        _ftts_mb_payload(market_id=9602, name="Team To Score First"),
    )
    assert market.family is neither.family
    neither.runners[2].label = "Neither"
    neither_market = MatchbookNormalizer().normalize_market(
        event,
        {
            "id": 9603,
            "name": "Team To Score First",
            "runners": [
                {"id": 1, "name": "Tottenham"},
                {"id": 2, "name": "Everton"},
                {"id": 3, "name": "Neither"},
            ],
        },
    )
    assert [runner.outcome for runner in neither_market.runners][-1] is CanonicalOutcome.NO_GOAL
    no_goals = MatchbookNormalizer().normalize_market(
        event,
        {
            "id": 9604,
            "name": "First Team To Score",
            "runners": [
                {"id": 1, "name": "Home"},
                {"id": 2, "name": "Away"},
                {"id": 3, "name": "No Goals"},
            ],
        },
    )
    assert {runner.outcome for runner in no_goals.runners} == {
        CanonicalOutcome.HOME,
        CanonicalOutcome.AWAY,
        CanonicalOutcome.NO_GOAL,
    }


def test_generic_home_away_aliases_do_not_broaden_unrelated_families() -> None:
    event = MatchbookNormalizer().normalize_event(MB_EVENT)
    match_odds = MatchbookNormalizer().normalize_market(
        event,
        {
            "id": 9605,
            "name": "Match Odds",
            "runners": [
                {"id": 1, "name": "Home"},
                {"id": 2, "name": "Draw"},
                {"id": 3, "name": "Away"},
            ],
        },
    )
    assert match_odds.family is MarketFamily.MATCH_RESULT
    assert [runner.outcome for runner in match_odds.runners] == [
        CanonicalOutcome.OTHER,
        CanonicalOutcome.DRAW,
        CanonicalOutcome.OTHER,
    ]
    dnb = MatchbookNormalizer().normalize_market(
        event,
        {
            "id": 9606,
            "name": "Draw No Bet",
            "runners": [
                {"id": 1, "name": "Home Team"},
                {"id": 2, "name": "Away Team"},
            ],
        },
    )
    assert dnb.family is MarketFamily.DRAW_NO_BET
    assert [runner.outcome for runner in dnb.runners] == [
        CanonicalOutcome.OTHER,
        CanonicalOutcome.OTHER,
    ]
    named = MatchbookNormalizer().normalize_market(
        event,
        {
            "id": 9607,
            "name": "Match Odds",
            "runners": [
                {"id": 1, "name": "Tottenham"},
                {"id": 2, "name": "Draw"},
                {"id": 3, "name": "Everton"},
            ],
        },
    )
    assert [runner.outcome for runner in named.runners] == [
        CanonicalOutcome.HOME,
        CanonicalOutcome.DRAW,
        CanonicalOutcome.AWAY,
    ]
    ftts = MatchbookNormalizer().normalize_market(
        event,
        {
            "id": 9608,
            "name": "First Team To Score",
            "runners": [
                {"id": 1, "name": "Home Team"},
                {"id": 2, "name": "Away Team"},
                {"id": 3, "name": "No Goal"},
            ],
        },
    )
    assert [runner.outcome for runner in ftts.runners] == [
        CanonicalOutcome.HOME,
        CanonicalOutcome.AWAY,
        CanonicalOutcome.NO_GOAL,
    ]


def test_polymarket_regulation_time_first_team_to_score_matches_matchbook() -> None:
    event = PolymarketNormalizer().normalize_event(PM_EVENT)
    market = PolymarketNormalizer().normalize_market(event, _ftts_pm_payload())
    assert market.family is MarketFamily.FIRST_TEAM_TO_SCORE
    assert [runner.outcome for runner in market.runners] == [
        CanonicalOutcome.HOME,
        CanonicalOutcome.AWAY,
        CanonicalOutcome.NO_GOAL,
    ]
    matchbook = MatchbookNormalizer().normalize_market(
        MatchbookNormalizer().normalize_event(MB_EVENT),
        _ftts_mb_payload(),
    )
    result = MarketMatcher().match(matchbook, market)
    assert result.matched is False
    assert "not_registered" in result.reasons
    assert generalized_payoff_eligible_market(market) is True
    assert solver_eligible_market(market) is False
    assert market.family not in STEP7_COMPLETE_SET_FAMILIES


def test_missing_no_goal_is_incomplete_state_set() -> None:
    decision, matchbook, _ = _scan(
        _ftts_mb_payload(include_no_goal=False),
        _ftts_pm_payload(include_no_goal=False),
        _ftts_books(include_no_goal=False),
    )
    assert matchbook.market.family is MarketFamily.FIRST_TEAM_TO_SCORE
    assert CanonicalOutcome.NO_GOAL not in {runner.outcome for runner in matchbook.market.runners}
    assert generalized_payoff_eligible_market(matchbook.market) is False
    assert scan_ineligibility_reason(matchbook.market) == INCOMPLETE_OUTCOME_REASON
    assert "catalogue_unsupported" in decision.rejection_reasons or "catalogue_review_required" in decision.rejection_reasons or "not_registered" in decision.rejection_reasons or "outcome_space_mismatch" in decision.rejection_reasons
    assert decision.payoff_scan is None
    assert decision.eligible_for_paper_simulation is False


def test_player_first_goalscorer_is_not_first_team_to_score() -> None:
    event = MatchbookNormalizer().normalize_event(MB_EVENT)
    player = MatchbookNormalizer().normalize_market(
        event,
        {
            "id": 9701,
            "name": "First Goalscorer",
            "runners": [
                {"id": 1, "name": "Harry Kane"},
                {"id": 2, "name": "Son Heung-Min"},
                {"id": 3, "name": "No Goal"},
            ],
        },
    )
    assert player.family is MarketFamily.PLAYER_PROPS
    assert player.family is not MarketFamily.FIRST_TEAM_TO_SCORE
    anytime = MatchbookNormalizer().normalize_market(
        event,
        {
            "id": 9702,
            "name": "Anytime Scorer",
            "runners": [{"id": 1, "name": "Harry Kane"}, {"id": 2, "name": "No Goal"}],
        },
    )
    assert anytime.family is MarketFamily.PLAYER_PROPS
    with pytest.raises(VenueNormalizationError):
        MatchbookNormalizer().normalize_market(
            event,
            {
                "id": 9703,
                "name": "First Goal",
                "runners": [
                    {"id": 1, "name": "Harry Kane"},
                    {"id": 2, "name": "Son Heung-Min"},
                    {"id": 3, "name": "No Goal"},
                ],
            },
        )
    pm_event = PolymarketNormalizer().normalize_event(PM_EVENT)
    pm_player = PolymarketNormalizer().normalize_market(
        pm_event,
        {
            "id": "pm-fg",
            "question": "First goalscorer?",
            "sportsMarketType": "first goalscorer",
            "outcomes": '["Harry Kane", "Son Heung-Min"]',
            "clobTokenIds": '["k", "s"]',
            "description": "Resolves based on 90 minutes of regulation time.",
        },
    )
    assert pm_player.family is MarketFamily.PLAYER_PROPS
    ftts = MatchbookNormalizer().normalize_market(event, _ftts_mb_payload(market_id=9704))
    result = MarketMatcher().match(player, ftts)
    assert result.matched is False
    assert "market_family_mismatch" in result.reasons
    assert MarketMatcher().match(pm_player, ftts).matched is False


def test_next_goal_remains_distinct_from_first_team_to_score() -> None:
    event = MatchbookNormalizer().normalize_event(MB_EVENT)
    next_goal = MatchbookNormalizer().normalize_market(
        event,
        {
            "id": 9801,
            "name": "Next Goal",
            "runners": [
                {"id": 1, "name": "Tottenham"},
                {"id": 2, "name": "Everton"},
                {"id": 3, "name": "No Goal"},
            ],
        },
    )
    ftts = MatchbookNormalizer().normalize_market(event, _ftts_mb_payload(market_id=9802))
    assert next_goal.family is MarketFamily.NEXT_GOAL
    assert ftts.family is MarketFamily.FIRST_TEAM_TO_SCORE
    assert MarketMatcher().match(next_goal, ftts).matched is False
    assert "market_family_mismatch" in MarketMatcher().match(next_goal, ftts).reasons
    pm_next = PolymarketNormalizer().normalize_market(
        PolymarketNormalizer().normalize_event(PM_EVENT),
        {
            "id": "pm-next",
            "question": "Next goal",
            "sportsMarketType": "next goal",
            "outcomes": '["Tottenham", "Everton", "No Goal"]',
            "clobTokenIds": '["h", "a", "n"]',
            "description": "Resolves based on 90 minutes of regulation time.",
        },
    )
    assert pm_next.family is MarketFamily.NEXT_GOAL
    assert MarketMatcher().match(ftts, pm_next).matched is False


def test_market_matcher_rejects_outcome_or_settlement_mismatch() -> None:
    matchbook = MatchbookNormalizer().normalize_market(
        MatchbookNormalizer().normalize_event(MB_EVENT),
        _ftts_mb_payload(),
    )
    pm_missing = PolymarketNormalizer().normalize_market(
        PolymarketNormalizer().normalize_event(PM_EVENT),
        _ftts_pm_payload(include_no_goal=False),
    )
    extra_time = PolymarketNormalizer().normalize_market(
        PolymarketNormalizer().normalize_event(PM_EVENT),
        _ftts_pm_payload(description="Resolves including extra time."),
    )
    outcome = MarketMatcher().match(matchbook, pm_missing)
    assert outcome.matched is False
    assert "outcome_space_mismatch" in outcome.reasons
    settlement = MarketMatcher().match(matchbook, extra_time)
    assert settlement.matched is False
    assert "not_registered" in settlement.reasons


def test_generalized_payoff_mapping_covers_home_away_no_goal_without_refunds() -> None:
    states = FIRST_TEAM_TO_SCORE_STATES
    mapping_scan = DepthAwarePayoffScanner().scan(
        [
            DepthQuoteSource(
                outcome="home",
                venue=VenueName.MATCHBOOK,
                source_market_id="mb",
                source_runner_id="h",
                levels=[BookLevel(decimal_odds=Decimal("2.20"), available_stake=Decimal("80"))],
                cost=_safe_cost(VenueName.MATCHBOOK),
            ),
            DepthQuoteSource(
                outcome="away",
                venue=VenueName.POLYMARKET,
                source_market_id="pm",
                source_runner_id="a",
                levels=[BookLevel(decimal_odds=Decimal("3.50"), available_stake=Decimal("80"))],
                cost=_safe_cost(VenueName.POLYMARKET),
            ),
            DepthQuoteSource(
                outcome="no_goal",
                venue=VenueName.MATCHBOOK,
                source_market_id="mb",
                source_runner_id="n",
                levels=[BookLevel(decimal_odds=Decimal("4.50"), available_stake=Decimal("80"))],
                cost=_safe_cost(VenueName.MATCHBOOK),
            ),
        ],
        state_model=GeneralizedStateModel.FIRST_TEAM_TO_SCORE,
    )
    home = payoff_leg_from_back(
        leg_id="home",
        venue=VenueName.MATCHBOOK,
        source_market_id="mb",
        source_runner_id="h",
        runner_outcome="home",
        max_stake=Decimal("80"),
        net_decimal_odds=Decimal("2.20"),
        states=states,
        win_states=["home_first"],
        refund_states=(),
    )
    away = payoff_leg_from_back(
        leg_id="away",
        venue=VenueName.POLYMARKET,
        source_market_id="pm",
        source_runner_id="a",
        runner_outcome="away",
        max_stake=Decimal("80"),
        net_decimal_odds=Decimal("3.50"),
        states=states,
        win_states=["away_first"],
        refund_states=(),
    )
    no_goal = payoff_leg_from_back(
        leg_id="no_goal",
        venue=VenueName.MATCHBOOK,
        source_market_id="mb",
        source_runner_id="n",
        runner_outcome="no_goal",
        max_stake=Decimal("80"),
        net_decimal_odds=Decimal("4.50"),
        states=states,
        win_states=["no_goal"],
        refund_states=(),
    )
    assert set(home.payoff_per_unit) == set(states)
    assert home.payoff_per_unit["home_first"] > 0
    assert home.payoff_per_unit["away_first"] == Decimal("-1")
    assert home.payoff_per_unit["no_goal"] == Decimal("-1")
    assert away.payoff_per_unit["away_first"] > 0
    assert away.payoff_per_unit["home_first"] == Decimal("-1")
    assert no_goal.payoff_per_unit["no_goal"] > 0
    assert no_goal.payoff_per_unit["home_first"] == Decimal("-1")
    assert all(value != 0 or state == "unused" for state, value in home.payoff_per_unit.items())
    assert mapping_scan.solution.state_pnl.keys() == set(states)


def test_two_team_structure_cannot_be_labelled_guaranteed() -> None:
    two_leg = PayoffProblem(
        states=list(FIRST_TEAM_TO_SCORE_STATES),
        legs=[
            payoff_leg_from_back(
                leg_id="home",
                venue=VenueName.MATCHBOOK,
                source_market_id="mb",
                source_runner_id="h",
                runner_outcome="home",
                max_stake=Decimal("80"),
                net_decimal_odds=Decimal("1.10"),
                states=FIRST_TEAM_TO_SCORE_STATES,
                win_states=["home_first"],
                refund_states=(),
            ),
            payoff_leg_from_back(
                leg_id="away",
                venue=VenueName.POLYMARKET,
                source_market_id="pm",
                source_runner_id="a",
                runner_outcome="away",
                max_stake=Decimal("80"),
                net_decimal_odds=Decimal("1.10"),
                states=FIRST_TEAM_TO_SCORE_STATES,
                win_states=["away_first"],
                refund_states=(),
            ),
        ],
    )
    result = GeneralizedMaxMinSolver().solve(two_leg)
    assert result.numerically_validated is True
    assert result.is_arbitrage is False
    assert result.minimum_state_pnl <= 0
    if any(stake.stake > 0 for stake in result.selected_stakes):
        assert result.state_pnl["no_goal"] < 0


def test_constructed_three_state_opportunity_is_arb_only_when_every_state_is_positive() -> None:
    decision, matchbook, _ = _scan(_ftts_mb_payload(), _ftts_pm_payload(), _ftts_books())
    assert matchbook.market.family is MarketFamily.FIRST_TEAM_TO_SCORE
    assert decision.solver_model == SOLVER_MODEL_GENERALIZED
    assert decision.payoff_scan is not None
    assert set(decision.payoff_scan.solution.state_pnl) == set(FIRST_TEAM_TO_SCORE_STATES)
    problem = PayoffProblem(
        states=list(FIRST_TEAM_TO_SCORE_STATES),
        legs=[
            payoff_leg_from_back(
                leg_id="home",
                venue=VenueName.MATCHBOOK,
                source_market_id="mb",
                source_runner_id="h",
                runner_outcome="home",
                max_stake=Decimal("50"),
                net_decimal_odds=Decimal("2.20"),
                states=FIRST_TEAM_TO_SCORE_STATES,
                win_states=["home_first"],
                refund_states=(),
            ),
            payoff_leg_from_back(
                leg_id="away",
                venue=VenueName.POLYMARKET,
                source_market_id="pm",
                source_runner_id="a",
                runner_outcome="away",
                max_stake=Decimal("50"),
                net_decimal_odds=Decimal("3.50"),
                states=FIRST_TEAM_TO_SCORE_STATES,
                win_states=["away_first"],
                refund_states=(),
            ),
            payoff_leg_from_back(
                leg_id="no_goal",
                venue=VenueName.MATCHBOOK,
                source_market_id="mb",
                source_runner_id="n",
                runner_outcome="no_goal",
                max_stake=Decimal("50"),
                net_decimal_odds=Decimal("4.50"),
                states=FIRST_TEAM_TO_SCORE_STATES,
                win_states=["no_goal"],
                refund_states=(),
            ),
        ],
    )
    result = GeneralizedMaxMinSolver().solve(problem)
    assert result.is_arbitrage is True
    assert result.numerically_validated is True
    assert all(pnl > 0 for pnl in result.state_pnl.values())
    break_even_no_goal = PayoffProblem(
        states=list(FIRST_TEAM_TO_SCORE_STATES),
        legs=problem.legs[:2],
    )
    rejected = GeneralizedMaxMinSolver().solve(break_even_no_goal)
    assert rejected.is_arbitrage is False


def test_unsupported_fee_basis_fails_closed_for_first_team_to_score() -> None:
    payout = VenueCostSnapshot(
        venue=VenueName.MATCHBOOK,
        action=MarketAction.BACK,
        fee_basis=FeeBasis.PAYOUT,
        known_status=CostKnownStatus.KNOWN,
        source="test",
        rate=Decimal("0.02"),
        fee_scope=FeeScope.PER_QUOTE,
        order_role=OrderRole.NOT_APPLICABLE,
        currency="GBP",
    )
    stake_fee = VenueCostSnapshot(
        venue=VenueName.POLYMARKET,
        action=MarketAction.BUY,
        fee_basis=FeeBasis.STAKE_OR_NOTIONAL,
        known_status=CostKnownStatus.KNOWN,
        source="test",
        rate=Decimal("0.01"),
        fee_scope=FeeScope.PER_QUOTE,
        order_role=OrderRole.NOT_APPLICABLE,
        currency="USD",
    )
    repository = SqliteMarketIntelligenceRepository()
    service = PaperScanService(MarketIntelligenceService(repository))
    matchbook = MatchbookObservationBuilder().build(
        MB_EVENT, _ftts_mb_payload(), observed_at=OBSERVED, quote_age_ms=120
    )
    from registered_kalshi import registered_right_observation, scan_costs_for

    right = registered_right_observation(
        PM_EVENT,
        _ftts_pm_payload(),
        _ftts_books(),
        observed_at=OBSERVED,
        matchbook_event=MB_EVENT,
    )
    try:
        decision = service.scan_pair(
            matchbook,
            right,
            venue_costs=scan_costs_for(right) + [payout, stake_fee],
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
        )
    finally:
        repository.close()
    assert decision.solver_model == SOLVER_MODEL_GENERALIZED
    assert UNSUPPORTED_STATE_PAYOFF_FEE_BASIS in decision.rejection_reasons
    assert decision.payoff_scan is None
    assert decision.eligible_for_paper_simulation is False
    unsafe = DepthAwarePayoffScanner().scan(
        [
            DepthQuoteSource(
                outcome="home",
                venue=VenueName.MATCHBOOK,
                source_market_id="mb",
                source_runner_id="h",
                levels=[BookLevel(decimal_odds=Decimal("2.20"), available_stake=Decimal("80"))],
                cost=payout,
            ),
            DepthQuoteSource(
                outcome="away",
                venue=VenueName.POLYMARKET,
                source_market_id="pm",
                source_runner_id="a",
                levels=[BookLevel(decimal_odds=Decimal("3.50"), available_stake=Decimal("80"))],
                cost=_safe_cost(VenueName.POLYMARKET),
            ),
            DepthQuoteSource(
                outcome="no_goal",
                venue=VenueName.MATCHBOOK,
                source_market_id="mb",
                source_runner_id="n",
                levels=[BookLevel(decimal_odds=Decimal("4.50"), available_stake=Decimal("80"))],
                cost=payout,
            ),
        ],
        state_model=GeneralizedStateModel.FIRST_TEAM_TO_SCORE,
    )
    assert unsafe.solution.rejection_reason == UNSUPPORTED_STATE_PAYOFF_FEE_BASIS
    assert not unsafe.selected_quotes


class _ZeroNoGoalSolver:
    def solve(self, problem: PayoffProblem) -> PayoffSolution:
        selected: list[PayoffStake] = []
        for leg in problem.legs:
            stake = Decimal("0") if leg.runner_outcome == "no_goal" else Decimal("10")
            if stake <= 0:
                continue
            selected.append(
                PayoffStake(
                    leg_id=leg.leg_id,
                    venue=leg.venue,
                    source_market_id=leg.source_market_id,
                    source_runner_id=leg.source_runner_id,
                    runner_outcome=leg.runner_outcome,
                    stake=stake,
                    capital_consumed=stake * leg.capital_per_unit,
                    capital_per_unit=leg.capital_per_unit,
                )
            )
        return PayoffSolution(
            is_arbitrage=True,
            selected_stakes=selected,
            total_capital_used=Decimal("20"),
            state_pnl={"home_first": Decimal("1"), "away_first": Decimal("1"), "no_goal": Decimal("1")},
            minimum_state_pnl=Decimal("1"),
            roi=Decimal("0.05"),
            numerically_validated=True,
        )


def test_zero_stake_first_team_legs_never_enter_risk_or_paper_fills() -> None:
    scanner = DepthAwarePayoffScanner(solver=_ZeroNoGoalSolver())
    sources = [
        DepthQuoteSource(
            outcome="home",
            venue=VenueName.MATCHBOOK,
            source_market_id="mb-ftts",
            source_runner_id="mb-h",
            levels=[BookLevel(decimal_odds=Decimal("2.20"), available_stake=Decimal("80"))],
            cost=_safe_cost(VenueName.MATCHBOOK),
        ),
        DepthQuoteSource(
            outcome="away",
            venue=VenueName.POLYMARKET,
            source_market_id="pm-ftts",
            source_runner_id="pm-a",
            levels=[BookLevel(decimal_odds=Decimal("3.50"), available_stake=Decimal("80"))],
            cost=_safe_cost(VenueName.POLYMARKET),
        ),
        DepthQuoteSource(
            outcome="no_goal",
            venue=VenueName.MATCHBOOK,
            source_market_id="mb-ftts",
            source_runner_id="mb-n",
            levels=[BookLevel(decimal_odds=Decimal("4.50"), available_stake=Decimal("80"))],
            cost=_safe_cost(VenueName.MATCHBOOK),
        ),
    ]
    result = scanner.scan(sources, state_model=GeneralizedStateModel.FIRST_TEAM_TO_SCORE)
    assert "mb-n" not in {quote.source_runner_id for quote in result.selected_quotes}
    dirty = PayoffScanResult(
        solution=result.solution,
        selected_quotes=[
            *result.selected_quotes,
            DepthQuoteCandidate(
                outcome="no_goal",
                venue=VenueName.MATCHBOOK,
                source_market_id="mb-ftts",
                source_runner_id="mb-n",
                gross_weighted_odds=Decimal("4.50"),
                net_decimal_odds=Decimal("4.50"),
                cumulative_depth=Decimal("80"),
                levels_consumed=1,
            ),
        ],
    )
    intelligence = MarketIntelligenceService(SqliteMarketIntelligenceRepository())
    service = PaperScanService(intelligence)
    matchbook = MatchbookObservationBuilder().build(
        MB_EVENT,
        {
            "id": "mb-ftts",
            "name": "First Team To Score",
            "runners": [
                {"id": "mb-h", "name": "Tottenham", "prices": [{"side": "back", "odds": "2.20", "available-amount": "80"}]},
                {"id": "mb-a", "name": "Everton", "prices": [{"side": "back", "odds": "3.50", "available-amount": "80"}]},
                {"id": "mb-n", "name": "No Goal", "prices": [{"side": "back", "odds": "4.50", "available-amount": "80"}]},
            ],
        },
        observed_at=OBSERVED,
        quote_age_ms=120,
    )
    polymarket = PolymarketObservationBuilder().build(
        PM_EVENT,
        {
            "id": "pm-ftts",
            "question": "First team to score",
            "sportsMarketType": "first team to score",
            "outcomes": '["Tottenham", "Everton", "No Goal"]',
            "clobTokenIds": '["pm-h", "pm-a", "pm-n"]',
            "description": "Resolves based on 90 minutes of regulation time.",
        },
        {
            "pm-h": {"asset_id": "pm-h", "asks": [{"price": "0.28", "size": "80"}], "bids": [{"price": "0.26", "size": "80"}]},
            "pm-a": {"asset_id": "pm-a", "asks": [{"price": "0.28", "size": "80"}], "bids": [{"price": "0.26", "size": "80"}]},
            "pm-n": {"asset_id": "pm-n", "asks": [{"price": "0.22", "size": "80"}], "bids": [{"price": "0.20", "size": "80"}]},
        },
        observed_at=OBSERVED,
        quote_age_ms=150,
    )
    fills = _fill_legs_from_observations(
        matchbook,
        polymarket,
        depth_scan=None,
        payoff_scan=dirty,
        effective_fx={"GBP": Decimal("1"), "USD": Decimal("0.75")},
    )
    assert all(leg.source_runner_id != "mb-n" for leg in fills)
    risk = service._risk_inputs(
        matchbook,
        polymarket,
        depth_scan=None,
        payoff_scan=dirty,
        assumed_latency_ms=500,
        recent_volatility_bps=0.0,
        quote_age_ms=150,
    )
    assert risk is not None
    assert risk.leg_count == 2


def test_half_line_totals_remain_simple_and_ftts_inventory_is_truthful() -> None:
    totals, mb_tot, pm_tot = _scan(
        {
            "id": 8422,
            "name": "Over/Under 2.5 Goals",
            "runners": [
                {"id": 1, "name": "Over 2.5", "prices": [{"side": "back", "odds": "1.90", "available-amount": "40"}]},
                {"id": 2, "name": "Under 2.5", "prices": [{"side": "back", "odds": "1.95", "available-amount": "40"}]},
            ],
        },
        {
            "id": "pm-tg-25-8b",
            "question": "Total goals 2.5",
            "sportsMarketType": "total goals",
            "line": "2.5",
            "outcomes": '["Over", "Under"]',
            "clobTokenIds": '["o25", "u25"]',
            "description": "Resolves based on 90 minutes of regulation time.",
        },
        {
            "o25": {"asset_id": "o25", "asks": [{"price": "0.40", "size": "200"}], "bids": [{"price": "0.38", "size": "200"}]},
            "u25": {"asset_id": "u25", "asks": [{"price": "0.40", "size": "200"}], "bids": [{"price": "0.38", "size": "200"}]},
        },
    )
    ftts, mb_ftts, pm_ftts = _scan(_ftts_mb_payload(), _ftts_pm_payload(), _ftts_books())
    missing, mb_missing, _ = _scan(
        _ftts_mb_payload(market_id=9610, include_no_goal=False),
        _ftts_pm_payload(market_id="pm-ftts-missing", include_no_goal=False),
        _ftts_books(include_no_goal=False),
    )
    assert solver_eligible_market(mb_tot.market) is True
    assert totals.solver_model == SOLVER_MODEL_SIMPLE
    rows = assemble_fixture_inventory(
        [_inventory(mb_tot.market, name="TG 2.5"), _inventory(mb_ftts.market, name="FTTS"), _inventory(mb_missing.market, name="FTTS missing")],
        [_inventory(pm_tot.market, name="TG 2.5"), _inventory(pm_ftts.market, name="FTTS")],
        decisions_by_source_ids={
            (mb_tot.market.source_market_id, pm_tot.market.source_market_id): totals,
            (mb_ftts.market.source_market_id, pm_ftts.market.source_market_id): ftts,
        },
        venue_costs=matchbook_polymarket_costs(),
        fx_snapshots=_fx(),
    )
    tot_row = next(row for row in rows if row.family == "total_goals")
    ftts_complete = next(row for row in rows if row.family == "first_team_to_score" and row.entered_solver)
    ftts_missing = next(row for row in rows if row.family == "first_team_to_score" and not row.entered_solver)
    assert tot_row.solver_model == SOLVER_MODEL_SIMPLE
    assert ftts_complete.solver_model == SOLVER_MODEL_GENERALIZED
    assert ftts_missing.reason == INCOMPLETE_OUTCOME_REASON
    assert missing.eligible_for_paper_simulation is False


def test_paper_only_execution_flag_unchanged() -> None:
    from fastapi.testclient import TestClient

    from sports_hedge.api.main import app

    health = TestClient(app).get("/health")
    assert health.json()["execution_enabled"] is False


def test_bare_first_goal_accepted_only_when_runners_are_team_level() -> None:
    event = MatchbookNormalizer().normalize_event(MB_EVENT)
    accepted = MatchbookNormalizer().normalize_market(
        event,
        {
            "id": 9901,
            "name": "First Goal",
            "runners": [
                {"id": 1, "name": "Tottenham"},
                {"id": 2, "name": "Everton"},
                {"id": 3, "name": "No Goal"},
            ],
        },
    )
    assert accepted.family is MarketFamily.FIRST_TEAM_TO_SCORE
    with pytest.raises(VenueNormalizationError):
        MatchbookNormalizer().normalize_market(
            event,
            {"id": 9902, "name": "First Goal", "runners": [{"id": 1, "name": "Tottenham"}]},
        )


def _safe_cost(venue: VenueName) -> VenueCostSnapshot:
    return VenueCostSnapshot.per_quote_profit_commission(
        venue,
        Decimal("0.02") if venue is VenueName.MATCHBOOK else Decimal("0"),
        action=MarketAction.BUY if venue is VenueName.POLYMARKET else MarketAction.BACK,
        source="test",
        currency="USD" if venue is VenueName.POLYMARKET else "GBP",
    )
