from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

import pytest

from sports_hedge.application.complete_set import (
    SOLVER_MODEL_GENERALIZED,
    SOLVER_MODEL_SIMPLE,
    SPLIT_LINE_REASON,
    UNKNOWN_DRAW_VOID_REASON,
    generalized_payoff_eligible_market,
    scan_ineligibility_reason,
    solver_eligible_market,
)
from sports_hedge.application.fixture_inventory import assemble_fixture_inventory
from sports_hedge.arbitrage.models import ExecutableQuote, PayoffLeg, PayoffProblem
from sports_hedge.arbitrage.payoff_solver import GeneralizedMaxMinSolver, payoff_leg_from_back
from sports_hedge.arbitrage.solver import CompleteSetArbitrageSolver
from sports_hedge.domain.football import CanonicalOutcome, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.normalization.venues import MatchbookNormalizer

from test_step7_safe_market_expansion import MB_EVENT, _fx, _scan
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
