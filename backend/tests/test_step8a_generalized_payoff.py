from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from sports_hedge.application.complete_set import (
    SOLVER_MODEL_GENERALIZED,
    SOLVER_MODEL_SIMPLE,
    SPLIT_LINE_REASON,
    UNKNOWN_DRAW_VOID_REASON,
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
from sports_hedge.arbitrage.models import ExecutableQuote, PayoffLeg, PayoffProblem, PayoffSolution, PayoffStake
from sports_hedge.arbitrage.payoff_scan import DepthAwarePayoffScanner, PayoffScanResult
from sports_hedge.arbitrage.payoff_solver import GeneralizedMaxMinSolver, payoff_leg_from_back
from sports_hedge.arbitrage.solver import CompleteSetArbitrageSolver
from sports_hedge.domain.football import CanonicalOutcome, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import CostKnownStatus, FeeBasis, FeeScope, MarketAction, OrderRole, VenueCostSnapshot
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.normalization.venues import MatchbookNormalizer

from test_step7_safe_market_expansion import MB_EVENT, OBSERVED, PM_EVENT, _fx, _scan
from test_fixture_inventory import _inventory, _market
from venue_cost_helpers import matchbook_polymarket_costs


def _leg(
    *,
    leg_id: str,
    venue: VenueName,
    payoffs: dict[str, str],
    max_stake: str = "100",
    capital_per_unit: str = "1",
    source_market_id: str = "m",
) -> PayoffLeg:
    return PayoffLeg(
        leg_id=leg_id,
        venue=venue,
        source_market_id=source_market_id,
        source_runner_id=leg_id,
        runner_outcome=leg_id,
        max_stake=Decimal(max_stake),
        capital_per_unit=Decimal(capital_per_unit),
        payoff_per_unit={state: Decimal(value) for state, value in payoffs.items()},
    )


def test_payoff_problem_rejects_missing_and_extra_state_coverage() -> None:
    with pytest.raises(ValueError, match="complete state space"):
        PayoffProblem(
            states=["home", "draw", "away"],
            legs=[
                _leg(
                    leg_id="home-only",
                    venue=VenueName.MATCHBOOK,
                    payoffs={"home": "1.0", "away": "-1"},
                )
            ],
        )
    with pytest.raises(ValueError, match="complete state space"):
        PayoffProblem(
            states=["home", "away"],
            legs=[
                _leg(
                    leg_id="extra",
                    venue=VenueName.MATCHBOOK,
                    payoffs={"home": "1.0", "draw": "0", "away": "-1"},
                )
            ],
        )
    with pytest.raises(ValueError, match="duplicates"):
        PayoffProblem(
            states=["home", "home"],
            legs=[
                _leg(leg_id="dup", venue=VenueName.MATCHBOOK, payoffs={"home": "1"}),
            ],
        )


def test_solver_finds_positive_max_min_on_synthetic_three_state_refund() -> None:
    problem = PayoffProblem(
        states=["home", "draw", "away"],
        legs=[
            _leg(
                leg_id="home-dnb",
                venue=VenueName.MATCHBOOK,
                payoffs={"home": "1.2", "draw": "0", "away": "-1"},
            ),
            _leg(
                leg_id="away-dnb",
                venue=VenueName.POLYMARKET,
                payoffs={"home": "-1", "draw": "0", "away": "1.2"},
            ),
            _leg(
                leg_id="draw-cover",
                venue=VenueName.MATCHBOOK,
                source_market_id="draw-cover",
                payoffs={"home": "-1", "draw": "1.5", "away": "-1"},
            ),
        ],
        capital_limit=Decimal("90"),
    )
    result = GeneralizedMaxMinSolver().solve(problem)
    assert result.numerically_validated is True
    assert result.is_arbitrage is True
    assert result.minimum_state_pnl > 0
    assert result.rejection_reason is None
    assert set(result.state_pnl) == {"home", "draw", "away"}
    assert all(value > 0 for value in result.state_pnl.values())
    assert result.total_capital_used > 0
    assert result.roi == result.minimum_state_pnl / result.total_capital_used


def test_solver_no_arb_when_one_state_is_only_break_even() -> None:
    problem = PayoffProblem(
        states=["home", "draw", "away"],
        legs=[
            _leg(
                leg_id="home-dnb",
                venue=VenueName.MATCHBOOK,
                payoffs={"home": "1.2", "draw": "0", "away": "-1"},
            ),
            _leg(
                leg_id="away-dnb",
                venue=VenueName.POLYMARKET,
                payoffs={"home": "-1", "draw": "0", "away": "1.2"},
            ),
        ],
    )
    result = GeneralizedMaxMinSolver().solve(problem)
    assert result.numerically_validated is True
    assert result.is_arbitrage is False
    assert result.minimum_state_pnl <= 0
    assert result.state_pnl["draw"] == 0
    assert result.rejection_reason == "no_positive_edge"


def test_per_leg_max_stake_is_enforced() -> None:
    problem = PayoffProblem(
        states=["home", "draw", "away"],
        legs=[
            _leg(
                leg_id="home-dnb",
                venue=VenueName.MATCHBOOK,
                max_stake="5",
                payoffs={"home": "2", "draw": "0", "away": "-1"},
            ),
            _leg(
                leg_id="away-dnb",
                venue=VenueName.POLYMARKET,
                max_stake="40",
                payoffs={"home": "-1", "draw": "0", "away": "2"},
            ),
            _leg(
                leg_id="draw-cover",
                venue=VenueName.MATCHBOOK,
                source_market_id="draw-cover",
                max_stake="40",
                payoffs={"home": "-1", "draw": "2", "away": "-1"},
            ),
        ],
    )
    result = GeneralizedMaxMinSolver().solve(problem)
    by_id = {stake.leg_id: stake.stake for stake in result.selected_stakes}
    assert by_id["home-dnb"] <= Decimal("5")


def test_global_capital_limit_uses_capital_consumed_not_nominal_stake() -> None:
    problem = PayoffProblem(
        states=["a", "b"],
        legs=[
            _leg(
                leg_id="left",
                venue=VenueName.MATCHBOOK,
                max_stake="100",
                capital_per_unit="2",
                payoffs={"a": "1", "b": "0.2"},
            ),
            _leg(
                leg_id="right",
                venue=VenueName.POLYMARKET,
                max_stake="100",
                capital_per_unit="2",
                payoffs={"a": "0.2", "b": "1"},
            ),
        ],
        capital_limit=Decimal("10"),
    )
    result = GeneralizedMaxMinSolver().solve(problem)
    assert result.is_arbitrage is True
    nominal = sum((stake.stake for stake in result.selected_stakes), Decimal("0"))
    assert result.total_capital_used <= Decimal("10")
    assert result.total_capital_used == sum(
        (stake.capital_consumed for stake in result.selected_stakes), Decimal("0")
    )
    assert all(stake.capital_per_unit == Decimal("2") for stake in result.selected_stakes)
    assert nominal <= Decimal("5") + Decimal("0.00000001")


def test_solver_failure_and_non_finite_output_fail_closed() -> None:
    problem = PayoffProblem(
        states=["home", "away"],
        legs=[
            _leg(leg_id="a", venue=VenueName.MATCHBOOK, payoffs={"home": "1", "away": "-1"}),
            _leg(leg_id="b", venue=VenueName.POLYMARKET, payoffs={"home": "-1", "away": "1"}),
        ],
    )

    def boom(*_args: object, **_kwargs: object):
        raise RuntimeError("lp exploded")

    failed = GeneralizedMaxMinSolver(linprog=boom).solve(problem)
    assert failed.is_arbitrage is False
    assert failed.rejection_reason == "solver_failure"

    def nan_out(*_args: object, **_kwargs: object):
        return SimpleNamespace(success=True, x=[float("nan"), 1.0])

    non_finite = GeneralizedMaxMinSolver(linprog=nan_out).solve(problem)
    assert non_finite.is_arbitrage is False
    assert non_finite.rejection_reason == "non_finite_solver_output"

    def unsuccessful(*_args: object, **_kwargs: object):
        return SimpleNamespace(success=False, x=[1.0, 1.0])

    closed = GeneralizedMaxMinSolver(linprog=unsuccessful).solve(problem)
    assert closed.is_arbitrage is False
    assert closed.rejection_reason == "solver_failure"


def test_dnb_220_case_is_no_arb_because_draw_is_zero() -> None:
    false_profit = CompleteSetArbitrageSolver().solve(
        [
            ExecutableQuote(
                outcome="home",
                venue=VenueName.MATCHBOOK,
                source_market_id="naive-a",
                net_decimal_odds=Decimal("2.20"),
                max_stake=Decimal("80"),
            ),
            ExecutableQuote(
                outcome="away",
                venue=VenueName.POLYMARKET,
                source_market_id="naive-b",
                net_decimal_odds=Decimal("2.20"),
                max_stake=Decimal("80"),
            ),
        ]
    )
    assert false_profit.is_arbitrage is True
    decision, matchbook, _ = _scan(
        {
            "id": 8420,
            "name": "Draw No Bet",
            "runners": [
                {"id": 1, "name": "Tottenham", "prices": [{"side": "back", "odds": "2.20", "available-amount": "80"}]},
                {"id": 2, "name": "Everton", "prices": [{"side": "back", "odds": "2.20", "available-amount": "80"}]},
            ],
        },
        {
            "id": "pm-dnb-8a",
            "question": "Draw no bet",
            "sportsMarketType": "draw no bet",
            "outcomes": '["Tottenham", "Everton"]',
            "clobTokenIds": '["h", "a"]',
            "description": "Resolves based on 90 minutes of regulation time. Draw voids.",
        },
        {
            "h": {"asset_id": "h", "asks": [{"price": "0.45", "size": "200"}], "bids": [{"price": "0.40", "size": "200"}]},
            "a": {"asset_id": "a", "asks": [{"price": "0.45", "size": "200"}], "bids": [{"price": "0.40", "size": "200"}]},
        },
    )
    assert matchbook.market.family is MarketFamily.DRAW_NO_BET
    assert solver_eligible_market(matchbook.market) is False
    assert generalized_payoff_eligible_market(matchbook.market) is True
    assert decision.solver_model == SOLVER_MODEL_GENERALIZED
    assert decision.payoff_scan is not None
    assert decision.payoff_scan.solution.is_arbitrage is False
    assert decision.payoff_scan.solution.state_pnl["draw"] == 0
    assert decision.payoff_scan.solution.minimum_state_pnl <= 0
    assert decision.eligible_for_paper_simulation is False


def test_constructed_dnb_enters_only_when_every_state_is_positive() -> None:
    problem = PayoffProblem(
        states=["home", "draw", "away"],
        legs=[
            payoff_leg_from_back(
                leg_id="mb-home",
                venue=VenueName.MATCHBOOK,
                source_market_id="mb-dnb",
                source_runner_id="h",
                runner_outcome="home",
                max_stake=Decimal("50"),
                net_decimal_odds=Decimal("2.20"),
                states=["home", "draw", "away"],
                win_states=["home"],
                refund_states=["draw"],
            ),
            payoff_leg_from_back(
                leg_id="pm-away",
                venue=VenueName.POLYMARKET,
                source_market_id="pm-dnb",
                source_runner_id="a",
                runner_outcome="away",
                max_stake=Decimal("50"),
                net_decimal_odds=Decimal("2.20"),
                states=["home", "draw", "away"],
                win_states=["away"],
                refund_states=["draw"],
            ),
            payoff_leg_from_back(
                leg_id="mb-draw",
                venue=VenueName.MATCHBOOK,
                source_market_id="mb-draw",
                source_runner_id="d",
                runner_outcome="draw",
                max_stake=Decimal("50"),
                net_decimal_odds=Decimal("3.10"),
                states=["home", "draw", "away"],
                win_states=["draw"],
                refund_states=(),
            ),
        ],
    )
    result = GeneralizedMaxMinSolver().solve(problem)
    assert result.is_arbitrage is True
    assert all(pnl > 0 for pnl in result.state_pnl.values())
    two_leg = PayoffProblem(states=problem.states, legs=problem.legs[:2])
    rejected = GeneralizedMaxMinSolver().solve(two_leg)
    assert rejected.is_arbitrage is False
    assert rejected.state_pnl["draw"] == 0


def test_integer_totals_model_push_and_cannot_call_break_even_push_guaranteed() -> None:
    decision, matchbook, _ = _scan(
        {
            "id": 8421,
            "name": "Over/Under 2.0 Goals",
            "line": "2.0",
            "runners": [
                {"id": 1, "name": "Over 2.0", "prices": [{"side": "back", "odds": "2.20", "available-amount": "80"}]},
                {"id": 2, "name": "Under 2.0", "prices": [{"side": "back", "odds": "2.20", "available-amount": "80"}]},
            ],
        },
        {
            "id": "pm-tg-20-8a",
            "question": "Total goals 2.0",
            "sportsMarketType": "total goals",
            "line": "2.0",
            "outcomes": '["Over", "Under"]',
            "clobTokenIds": '["o", "u"]',
            "description": "Resolves based on 90 minutes of regulation time.",
        },
        {
            "o": {"asset_id": "o", "asks": [{"price": "0.45", "size": "200"}], "bids": [{"price": "0.40", "size": "200"}]},
            "u": {"asset_id": "u", "asks": [{"price": "0.45", "size": "200"}], "bids": [{"price": "0.40", "size": "200"}]},
        },
    )
    assert matchbook.market.settlement.push_possible is True
    assert decision.solver_model == SOLVER_MODEL_GENERALIZED
    assert decision.payoff_scan is not None
    assert set(decision.payoff_scan.solution.state_pnl) == {"over", "push", "under"}
    assert decision.payoff_scan.solution.state_pnl["push"] == 0
    assert decision.payoff_scan.solution.is_arbitrage is False
    assert decision.eligible_for_paper_simulation is False


def test_half_line_totals_still_use_complete_set_solver() -> None:
    decision, matchbook, _ = _scan(
        {
            "id": 8422,
            "name": "Over/Under 2.5 Goals",
            "runners": [
                {"id": 1, "name": "Over 2.5", "prices": [{"side": "back", "odds": "1.90", "available-amount": "40"}]},
                {"id": 2, "name": "Under 2.5", "prices": [{"side": "back", "odds": "1.95", "available-amount": "40"}]},
            ],
        },
        {
            "id": "pm-tg-25-8a",
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
    assert solver_eligible_market(matchbook.market) is True
    assert decision.solver_model == SOLVER_MODEL_SIMPLE
    assert decision.depth_scan is not None
    assert decision.payoff_scan is None


def test_quarter_line_totals_remain_fail_closed() -> None:
    decision, matchbook, _ = _scan(
        {
            "id": 8423,
            "name": "Over/Under 2.25 Goals",
            "line": "2.25",
            "runners": [
                {"id": 1, "name": "Over 2.25", "prices": [{"side": "back", "odds": "1.90", "available-amount": "40"}]},
                {"id": 2, "name": "Under 2.25", "prices": [{"side": "back", "odds": "1.95", "available-amount": "40"}]},
            ],
        },
        {
            "id": "pm-tg-225-8a",
            "question": "Total goals 2.25",
            "sportsMarketType": "total goals",
            "line": "2.25",
            "outcomes": '["Over", "Under"]',
            "clobTokenIds": '["oq", "uq"]',
            "description": "Resolves based on 90 minutes of regulation time.",
        },
        {
            "oq": {"asset_id": "oq", "asks": [{"price": "0.40", "size": "200"}], "bids": [{"price": "0.38", "size": "200"}]},
            "uq": {"asset_id": "uq", "asks": [{"price": "0.40", "size": "200"}], "bids": [{"price": "0.38", "size": "200"}]},
        },
    )
    assert matchbook.market.family is MarketFamily.TOTAL_GOALS
    assert scan_ineligibility_reason(matchbook.market) == SPLIT_LINE_REASON
    assert decision.depth_scan is None
    assert decision.payoff_scan is None
    assert SPLIT_LINE_REASON in decision.rejection_reasons
    assert decision.eligible_for_paper_simulation is False


def test_inventory_reports_generalized_payoff_for_dnb_and_integer_totals() -> None:
    dnb, mb_dnb, pm_dnb = _scan(
        {
            "id": 8424,
            "name": "Draw No Bet",
            "runners": [
                {"id": 1, "name": "Tottenham", "prices": [{"side": "back", "odds": "2.20", "available-amount": "80"}]},
                {"id": 2, "name": "Everton", "prices": [{"side": "back", "odds": "2.20", "available-amount": "80"}]},
            ],
        },
        {
            "id": "pm-dnb-inv",
            "question": "Draw no bet",
            "sportsMarketType": "draw no bet",
            "outcomes": '["Tottenham", "Everton"]',
            "clobTokenIds": '["h", "a"]',
            "description": "Resolves based on 90 minutes of regulation time. Draw voids.",
        },
        {
            "h": {"asset_id": "h", "asks": [{"price": "0.45", "size": "200"}], "bids": [{"price": "0.40", "size": "200"}]},
            "a": {"asset_id": "a", "asks": [{"price": "0.45", "size": "200"}], "bids": [{"price": "0.40", "size": "200"}]},
        },
    )
    totals, mb_tot, pm_tot = _scan(
        {
            "id": 8425,
            "name": "Over/Under 2.0 Goals",
            "line": "2.0",
            "runners": [
                {"id": 1, "name": "Over 2.0", "prices": [{"side": "back", "odds": "2.20", "available-amount": "80"}]},
                {"id": 2, "name": "Under 2.0", "prices": [{"side": "back", "odds": "2.20", "available-amount": "80"}]},
            ],
        },
        {
            "id": "pm-tg-inv",
            "question": "Total goals 2.0",
            "sportsMarketType": "total goals",
            "line": "2.0",
            "outcomes": '["Over", "Under"]',
            "clobTokenIds": '["o", "u"]',
            "description": "Resolves based on 90 minutes of regulation time.",
        },
        {
            "o": {"asset_id": "o", "asks": [{"price": "0.45", "size": "200"}], "bids": [{"price": "0.40", "size": "200"}]},
            "u": {"asset_id": "u", "asks": [{"price": "0.45", "size": "200"}], "bids": [{"price": "0.40", "size": "200"}]},
        },
    )
    rows = assemble_fixture_inventory(
        [_inventory(mb_dnb.market, name="Draw No Bet"), _inventory(mb_tot.market, name="TG 2.0")],
        [_inventory(pm_dnb.market, name="DNB"), _inventory(pm_tot.market, name="TG 2.0")],
        decisions_by_source_ids={
            (mb_dnb.market.source_market_id, pm_dnb.market.source_market_id): dnb,
            (mb_tot.market.source_market_id, pm_tot.market.source_market_id): totals,
        },
        venue_costs=matchbook_polymarket_costs(),
        fx_snapshots=_fx(),
    )
    dnb_row = next(row for row in rows if row.family == "draw_no_bet")
    tot_row = next(row for row in rows if row.family == "total_goals")
    assert dnb_row.entered_solver is True
    assert dnb_row.solver_model == SOLVER_MODEL_GENERALIZED
    assert dnb_row.solver_is_arbitrage is False
    assert dnb_row.reason
    assert tot_row.entered_solver is True
    assert tot_row.solver_model == SOLVER_MODEL_GENERALIZED
    assert tot_row.solver_is_arbitrage is False
    assert tot_row.reason


def test_matchbook_lays_never_enter_generalized_solver() -> None:
    decision, matchbook, _ = _scan(
        {
            "id": 8426,
            "name": "Draw No Bet",
            "runners": [
                {
                    "id": 1,
                    "name": "Tottenham",
                    "prices": [
                        {"side": "back", "odds": "2.20", "available-amount": "80"},
                        {"side": "lay", "odds": "1.10", "available-amount": "5000"},
                    ],
                },
                {
                    "id": 2,
                    "name": "Everton",
                    "prices": [
                        {"side": "back", "odds": "2.20", "available-amount": "80"},
                        {"side": "lay", "odds": "1.10", "available-amount": "5000"},
                    ],
                },
            ],
        },
        {
            "id": "pm-dnb-lays",
            "question": "Draw no bet",
            "sportsMarketType": "draw no bet",
            "outcomes": '["Tottenham", "Everton"]',
            "clobTokenIds": '["h", "a"]',
            "description": "Resolves based on 90 minutes of regulation time. Draw voids.",
        },
        {
            "h": {"asset_id": "h", "asks": [{"price": "0.45", "size": "200"}], "bids": [{"price": "0.10", "size": "5000"}]},
            "a": {"asset_id": "a", "asks": [{"price": "0.45", "size": "200"}], "bids": [{"price": "0.10", "size": "5000"}]},
        },
    )
    home = matchbook.book_for(CanonicalOutcome.HOME)
    assert home is not None and home.best_lay is not None
    assert decision.payoff_scan is not None
    for quote in decision.payoff_scan.selected_quotes:
        assert quote.net_decimal_odds > Decimal("1")
        assert quote.cumulative_depth <= Decimal("80") * Decimal("2")


def test_correct_score_first_goal_and_ah_remain_excluded() -> None:
    correct = _market(
        VenueName.MATCHBOOK,
        family=MarketFamily.CORRECT_SCORE,
        source_id="cs",
        outcomes=[CanonicalOutcome.OTHER, CanonicalOutcome.OTHER],
    )
    ah = _market(
        VenueName.MATCHBOOK,
        family=MarketFamily.ASIAN_HANDICAP,
        source_id="ah",
        outcomes=[CanonicalOutcome.HOME, CanonicalOutcome.AWAY],
        line=Decimal("-0.5"),
    )
    assert generalized_payoff_eligible_market(correct) is False
    assert generalized_payoff_eligible_market(ah) is False
    assert solver_eligible_market(correct) is False
    assert solver_eligible_market(ah) is False
    event = MatchbookNormalizer().normalize_event(MB_EVENT)
    with pytest.raises(Exception, match="Unsupported Matchbook market"):
        MatchbookNormalizer().normalize_market(
            event,
            {"id": 999, "name": "First Team To Score", "runners": [{"id": 1, "name": "Tottenham"}]},
        )


def test_unknown_draw_void_semantics_fail_closed() -> None:
    dnb = _market(
        VenueName.MATCHBOOK,
        family=MarketFamily.DRAW_NO_BET,
        source_id="dnb-unknown",
        outcomes=[CanonicalOutcome.HOME, CanonicalOutcome.AWAY],
    )
    dnb.settlement.push_possible = None
    assert generalized_payoff_eligible_market(dnb) is False
    assert scan_ineligibility_reason(dnb) == UNKNOWN_DRAW_VOID_REASON


def test_paper_only_execution_flag_unchanged() -> None:
    from fastapi.testclient import TestClient

    from sports_hedge.api.main import app

    health = TestClient(app).get("/health")
    assert health.json()["execution_enabled"] is False


def _safe_cost(venue: VenueName) -> VenueCostSnapshot:
    return VenueCostSnapshot.per_quote_profit_commission(
        venue,
        Decimal("0.02") if venue is VenueName.MATCHBOOK else Decimal("0"),
        action=MarketAction.BUY if venue is VenueName.POLYMARKET else MarketAction.BACK,
        source="test",
        currency="USD" if venue is VenueName.POLYMARKET else "GBP",
    )


def _source(outcome: str, venue: VenueName, runner: str, market: str) -> DepthQuoteSource:
    return DepthQuoteSource(
        outcome=outcome,
        venue=venue,
        source_market_id=market,
        source_runner_id=runner,
        levels=[BookLevel(decimal_odds=Decimal("2.20"), available_stake=Decimal("80"))],
        cost=_safe_cost(venue),
    )


class _ZeroMiddleSolver:
    def solve(self, problem: PayoffProblem) -> PayoffSolution:
        selected: list[PayoffStake] = []
        for index, leg in enumerate(problem.legs):
            stake = Decimal("0") if index == 1 else Decimal("10")
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
            state_pnl={"home": Decimal("1"), "draw": Decimal("1"), "away": Decimal("1")},
            minimum_state_pnl=Decimal("1"),
            roi=Decimal("0.05"),
            numerically_validated=True,
        )


def test_zero_stake_lp_legs_are_not_selected_quotes_fills_or_risk() -> None:
    scanner = DepthAwarePayoffScanner(solver=_ZeroMiddleSolver())
    sources = [
        _source("home", VenueName.MATCHBOOK, "mb-h", "mb-dnb"),
        _source("away", VenueName.POLYMARKET, "pm-a", "pm-dnb"),
        _source("home", VenueName.POLYMARKET, "pm-h", "pm-dnb"),
    ]
    result = scanner.scan(sources, state_model=GeneralizedStateModel.DRAW_NO_BET)
    runner_ids = {quote.source_runner_id for quote in result.selected_quotes}
    assert "pm-a" not in runner_ids
    assert runner_ids == {"mb-h", "pm-h"}
    assert all(stake.stake > 0 for stake in result.solution.selected_stakes)
    assert all(stake.source_runner_id != "pm-a" for stake in result.solution.selected_stakes)

    extra_zero = DepthQuoteCandidate(
        outcome="away",
        venue=VenueName.POLYMARKET,
        source_market_id="pm-dnb",
        source_runner_id="pm-a",
        gross_weighted_odds=Decimal("2.20"),
        net_decimal_odds=Decimal("2.20"),
        cumulative_depth=Decimal("80"),
        levels_consumed=1,
    )
    dirty = PayoffScanResult(
        solution=result.solution,
        selected_quotes=[*result.selected_quotes, extra_zero],
    )
    intelligence = MarketIntelligenceService(SqliteMarketIntelligenceRepository())
    service = PaperScanService(intelligence)
    matchbook = MatchbookObservationBuilder().build(
        MB_EVENT,
        {
            "id": "mb-dnb",
            "name": "Draw No Bet",
            "runners": [
                {"id": "mb-h", "name": "Tottenham", "prices": [{"side": "back", "odds": "2.20", "available-amount": "80"}]},
                {"id": "mb-a", "name": "Everton", "prices": [{"side": "back", "odds": "2.20", "available-amount": "80"}]},
            ],
        },
        observed_at=OBSERVED,
        quote_age_ms=120,
    )
    polymarket = PolymarketObservationBuilder().build(
        PM_EVENT,
        {
            "id": "pm-dnb",
            "question": "Draw no bet",
            "sportsMarketType": "draw no bet",
            "outcomes": '["Tottenham", "Everton"]',
            "clobTokenIds": '["pm-h", "pm-a"]',
            "description": "Resolves based on 90 minutes of regulation time. Draw voids.",
        },
        {
            "pm-h": {"asset_id": "pm-h", "asks": [{"price": "0.45", "size": "80"}], "bids": [{"price": "0.40", "size": "80"}]},
            "pm-a": {"asset_id": "pm-a", "asks": [{"price": "0.45", "size": "80"}], "bids": [{"price": "0.40", "size": "80"}]},
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
    assert all(leg.source_runner_id != "pm-a" for leg in fills)
    assert {leg.source_runner_id for leg in fills} == {"mb-h", "pm-h"}
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


def test_unsupported_fee_basis_fails_closed_on_generalized_path() -> None:
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
    intelligence = MarketIntelligenceService(repository)
    service = PaperScanService(intelligence)
    matchbook = MatchbookObservationBuilder().build(
        MB_EVENT,
        {
            "id": 8500,
            "name": "Draw No Bet",
            "runners": [
                {"id": 1, "name": "Tottenham", "prices": [{"side": "back", "odds": "2.20", "available-amount": "80"}]},
                {"id": 2, "name": "Everton", "prices": [{"side": "back", "odds": "2.20", "available-amount": "80"}]},
            ],
        },
        observed_at=OBSERVED,
        quote_age_ms=120,
    )
    polymarket = PolymarketObservationBuilder().build(
        PM_EVENT,
        {
            "id": "pm-dnb-payout",
            "question": "Draw no bet",
            "sportsMarketType": "draw no bet",
            "outcomes": '["Tottenham", "Everton"]',
            "clobTokenIds": '["h", "a"]',
            "description": "Resolves based on 90 minutes of regulation time. Draw voids.",
        },
        {
            "h": {"asset_id": "h", "asks": [{"price": "0.45", "size": "200"}], "bids": [{"price": "0.40", "size": "200"}]},
            "a": {"asset_id": "a", "asks": [{"price": "0.45", "size": "200"}], "bids": [{"price": "0.40", "size": "200"}]},
        },
        observed_at=OBSERVED,
        quote_age_ms=150,
    )
    try:
        decision = service.scan_pair(
            matchbook,
            polymarket,
            venue_costs=[payout, stake_fee],
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
        )
    finally:
        repository.close()
    assert decision.solver_model == SOLVER_MODEL_GENERALIZED
    assert UNSUPPORTED_STATE_PAYOFF_FEE_BASIS in decision.rejection_reasons
    assert decision.payoff_scan is None
    assert decision.eligible_for_paper_simulation is False
    assert decision.fill_legs == []

    unsafe_scan = DepthAwarePayoffScanner().scan(
        [
            _source("home", VenueName.MATCHBOOK, "mb-h", "mb-dnb").model_copy(update={"cost": payout}),
            _source("away", VenueName.POLYMARKET, "pm-a", "pm-dnb"),
        ],
        state_model=GeneralizedStateModel.DRAW_NO_BET,
    )
    assert unsafe_scan.solution.is_arbitrage is False
    assert unsafe_scan.solution.rejection_reason == UNSUPPORTED_STATE_PAYOFF_FEE_BASIS
    assert unsafe_scan.solution.state_pnl == {} or all(
        value == 0 for value in unsafe_scan.solution.state_pnl.values()
    )
    assert not unsafe_scan.selected_quotes


def test_materially_invalid_lp_output_is_rejected_not_repaired() -> None:
    problem = PayoffProblem(
        states=["home", "away"],
        legs=[
            _leg(leg_id="a", venue=VenueName.MATCHBOOK, max_stake="5", payoffs={"home": "1", "away": "-1"}),
            _leg(leg_id="b", venue=VenueName.POLYMARKET, max_stake="5", payoffs={"home": "-1", "away": "1"}),
        ],
        capital_limit=Decimal("10"),
    )

    def negative(*_args: object, **_kwargs: object):
        return SimpleNamespace(success=True, x=[-1.0, 2.0])

    rejected_negative = GeneralizedMaxMinSolver(linprog=negative).solve(problem)
    assert rejected_negative.is_arbitrage is False
    assert rejected_negative.rejection_reason == "numerical_validation_failed"

    def over_max(*_args: object, **_kwargs: object):
        return SimpleNamespace(success=True, x=[6.0, 2.0])

    rejected_over = GeneralizedMaxMinSolver(linprog=over_max).solve(problem)
    assert rejected_over.is_arbitrage is False
    assert rejected_over.rejection_reason == "numerical_validation_failed"

    wide = PayoffProblem(
        states=["home", "away"],
        legs=[
            _leg(leg_id="a", venue=VenueName.MATCHBOOK, max_stake="20", payoffs={"home": "1", "away": "-0.1"}),
            _leg(leg_id="b", venue=VenueName.POLYMARKET, max_stake="20", payoffs={"home": "-0.1", "away": "1"}),
        ],
        capital_limit=Decimal("10"),
    )

    def capital_violating(*_args: object, **_kwargs: object):
        return SimpleNamespace(success=True, x=[8.0, 8.0])

    rejected_capital = GeneralizedMaxMinSolver(linprog=capital_violating).solve(wide)
    assert rejected_capital.is_arbitrage is False
    assert rejected_capital.rejection_reason == "numerical_validation_failed"

    def epsilon_noise(*_args: object, **_kwargs: object):
        return SimpleNamespace(success=True, x=[-1e-12, 4.0])

    noisy = GeneralizedMaxMinSolver(linprog=epsilon_noise).solve(problem)
    assert noisy.rejection_reason != "numerical_validation_failed"
    assert all(stake.stake >= 0 for stake in noisy.selected_stakes)
