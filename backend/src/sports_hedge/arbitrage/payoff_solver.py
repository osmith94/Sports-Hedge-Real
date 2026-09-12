from __future__ import annotations

from collections.abc import Sequence
from decimal import ROUND_DOWN, Decimal, getcontext
from typing import Any, Callable

from sports_hedge.arbitrage.models import PayoffLeg, PayoffProblem, PayoffSolution, PayoffStake
from sports_hedge.domain.models import VenueName


getcontext().prec = 28

STAKE_QUANT = Decimal("0.00000001")
LP_ENGINE = "scipy.linprog.highs"

LinprogFn = Callable[..., Any]


def _rejected(
    reason: str,
    *,
    states: Sequence[str] | None = None,
    numerically_validated: bool = False,
) -> PayoffSolution:
    pnl = {state: Decimal("0") for state in (states or ())}
    return PayoffSolution(
        is_arbitrage=False,
        state_pnl=pnl,
        minimum_state_pnl=Decimal("0") if pnl else Decimal("0"),
        rejection_reason=reason,
        numerically_validated=numerically_validated,
        solver_engine=LP_ENGINE,
    )


def _to_float(value: Decimal) -> float:
    as_float = float(value)
    if as_float != as_float or as_float in {float("inf"), float("-inf")}:
        raise ValueError("non_finite_solver_input")
    return as_float


def _quantize_down(value: Decimal) -> Decimal:
    quantized = value.quantize(STAKE_QUANT, rounding=ROUND_DOWN)
    return quantized if quantized > 0 else Decimal("0")


def _finite_decimal(value: object) -> Decimal | None:
    try:
        as_float = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        as_float = None
    if as_float is not None and as_float != as_float:
        return None
    try:
        number = Decimal(str(value))
    except Exception:
        return None
    if number.is_nan() or number.is_infinite():
        return None
    return number


class GeneralizedMaxMinSolver:
    """Maximize the minimum net P&L across a complete, explicit state space.

    A numerical LP produces candidate stakes only. Every reported figure is
    recomputed with Decimal. Non-finite, infeasible, or post-validation failures
    fail closed. Profit is never labelled guaranteed unless every state P&L is
    strictly positive after that Decimal recompute.
    """

    def __init__(self, *, linprog: LinprogFn | None = None) -> None:
        self._linprog = linprog

    def solve(self, problem: PayoffProblem) -> PayoffSolution:
        try:
            validated = problem if isinstance(problem, PayoffProblem) else PayoffProblem.model_validate(problem)
        except Exception:
            return _rejected("malformed_payoff_problem")
        return self._solve_validated(validated)

    def _solve_validated(self, problem: PayoffProblem) -> PayoffSolution:
        linprog = self._linprog or _load_linprog()
        if linprog is None:
            return _rejected("solver_failure", states=problem.states)

        n = len(problem.legs)
        states = list(problem.states)
        c = [0.0] * n + [-1.0]
        a_ub: list[list[float]] = []
        b_ub: list[float] = []
        try:
            for state in states:
                row = [-_to_float(leg.payoff_per_unit[state]) for leg in problem.legs]
                row.append(1.0)
                a_ub.append(row)
                b_ub.append(0.0)
            if problem.capital_limit is not None:
                row = [_to_float(leg.capital_per_unit) for leg in problem.legs]
                row.append(0.0)
                a_ub.append(row)
                b_ub.append(_to_float(problem.capital_limit))
            if problem.venue_capital_limits:
                by_venue: dict[VenueName, list[int]] = {}
                for index, leg in enumerate(problem.legs):
                    by_venue.setdefault(leg.venue, []).append(index)
                for venue, indexes in by_venue.items():
                    if venue is VenueName.SMARKETS:
                        continue
                    if venue not in problem.venue_capital_limits:
                        continue
                    cap = problem.venue_capital_limits[venue]
                    if cap == 0:
                        return _rejected("insufficient_venue_capital", states=states)
                    row = [0.0] * (n + 1)
                    for index in indexes:
                        row[index] = _to_float(problem.legs[index].capital_per_unit)
                    a_ub.append(row)
                    b_ub.append(_to_float(cap))
            bounds = [(0.0, _to_float(leg.max_stake)) for leg in problem.legs]
            bounds.append((None, None))
        except ValueError:
            return _rejected("non_finite_solver_input", states=states)

        try:
            result = linprog(
                c,
                A_ub=a_ub,
                b_ub=b_ub,
                bounds=bounds,
                method="highs",
            )
        except Exception:
            return _rejected("solver_failure", states=states)

        try:
            if result is None or not getattr(result, "success", False):
                return _rejected("solver_failure", states=states)
            raw = getattr(result, "x", None)
            if raw is None or len(raw) < n:
                return _rejected("solver_failure", states=states)

            candidate: list[Decimal] = []
            for value in list(raw)[:n]:
                parsed = _finite_decimal(value)
                if parsed is None:
                    return _rejected("non_finite_solver_output", states=states)
                if parsed < 0:
                    parsed = Decimal("0")
                candidate.append(parsed)
        except Exception:
            return _rejected("solver_failure", states=states)

        return self._post_validate(problem, candidate)

    def _post_validate(self, problem: PayoffProblem, candidate: Sequence[Decimal]) -> PayoffSolution:
        stakes: list[Decimal] = []
        for stake, leg in zip(candidate, problem.legs, strict=True):
            clamped = min(stake, leg.max_stake)
            stakes.append(_quantize_down(clamped))

        def capital_used(values: Sequence[Decimal]) -> Decimal:
            return sum((value * leg.capital_per_unit for value, leg in zip(values, problem.legs, strict=True)), Decimal("0"))

        total_capital = capital_used(stakes)
        if problem.capital_limit is not None and total_capital > problem.capital_limit:
            if total_capital <= 0:
                return _rejected("insufficient_venue_capital", states=problem.states, numerically_validated=True)
            scale = problem.capital_limit / total_capital
            stakes = [_quantize_down(stake * scale) for stake in stakes]
            total_capital = capital_used(stakes)
            if total_capital > problem.capital_limit:
                return _rejected("numerical_validation_failed", states=problem.states, numerically_validated=True)

        if problem.venue_capital_limits:
            consumed: dict[VenueName, Decimal] = {}
            for stake, leg in zip(stakes, problem.legs, strict=True):
                consumed[leg.venue] = consumed.get(leg.venue, Decimal("0")) + stake * leg.capital_per_unit
            for venue, used in consumed.items():
                if venue is VenueName.SMARKETS:
                    continue
                cap = problem.venue_capital_limits.get(venue)
                if cap is None:
                    continue
                if used > cap:
                    return _rejected("numerical_validation_failed", states=problem.states, numerically_validated=True)

        state_pnl: dict[str, Decimal] = {}
        for state in problem.states:
            pnl = Decimal("0")
            for stake, leg in zip(stakes, problem.legs, strict=True):
                pnl += stake * leg.payoff_per_unit[state]
            if pnl.is_nan() or pnl.is_infinite():
                return _rejected("non_finite_solver_output", states=problem.states)
            state_pnl[state] = pnl

        minimum = min(state_pnl.values()) if state_pnl else Decimal("0")
        roi = minimum / total_capital if total_capital > 0 else Decimal("0")
        selected = [
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
            for stake, leg in zip(stakes, problem.legs, strict=True)
            if stake > 0
        ]
        is_arb = minimum > 0 and total_capital > 0
        return PayoffSolution(
            is_arbitrage=is_arb,
            selected_stakes=selected,
            total_capital_used=total_capital,
            state_pnl=state_pnl,
            minimum_state_pnl=minimum,
            roi=roi,
            rejection_reason=None if is_arb else "no_positive_edge",
            numerically_validated=True,
            solver_engine=LP_ENGINE,
        )


def _load_linprog() -> LinprogFn | None:
    try:
        from scipy.optimize import linprog
    except Exception:
        return None
    return linprog


def back_payoff_per_unit(
    *,
    states: Sequence[str],
    win_states: Sequence[str],
    refund_states: Sequence[str],
    net_decimal_odds: Decimal,
) -> dict[str, Decimal]:
    """Net P&L per unit back/buy stake: win profit, refund 0, otherwise lose stake."""

    if net_decimal_odds <= 1:
        raise ValueError("net_decimal_odds must exceed 1")
    profit = net_decimal_odds - Decimal("1")
    wins = set(win_states)
    refunds = set(refund_states)
    payoffs: dict[str, Decimal] = {}
    for state in states:
        if state in wins:
            payoffs[state] = profit
        elif state in refunds:
            payoffs[state] = Decimal("0")
        else:
            payoffs[state] = Decimal("-1")
    return payoffs


def payoff_leg_from_back(
    *,
    leg_id: str,
    venue: VenueName,
    source_market_id: str,
    source_runner_id: str | None,
    runner_outcome: str,
    max_stake: Decimal,
    net_decimal_odds: Decimal,
    states: Sequence[str],
    win_states: Sequence[str],
    refund_states: Sequence[str],
    capital_per_unit: Decimal = Decimal("1"),
    metadata: dict[str, Any] | None = None,
) -> PayoffLeg:
    return PayoffLeg(
        leg_id=leg_id,
        venue=venue,
        source_market_id=source_market_id,
        source_runner_id=source_runner_id,
        runner_outcome=runner_outcome,
        max_stake=max_stake,
        capital_per_unit=capital_per_unit,
        payoff_per_unit=back_payoff_per_unit(
            states=states,
            win_states=win_states,
            refund_states=refund_states,
            net_decimal_odds=net_decimal_odds,
        ),
        metadata=metadata or {},
    )
