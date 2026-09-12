from __future__ import annotations

from collections.abc import Mapping, Sequence
from decimal import Decimal
from itertools import product

from pydantic import BaseModel, Field

from sports_hedge.arbitrage.depth import DepthQuoteCandidate, DepthQuoteSource, prefix_depth_candidates
from sports_hedge.arbitrage.models import PayoffProblem, PayoffSolution
from sports_hedge.arbitrage.payoff_solver import GeneralizedMaxMinSolver, payoff_leg_from_back
from sports_hedge.domain.models import VenueName
from sports_hedge.application.complete_set import (
    DNB_STATES,
    INTEGER_TOTAL_STATES,
    GeneralizedStateModel,
)


class PayoffScanResult(BaseModel):
    solution: PayoffSolution
    selected_quotes: list[DepthQuoteCandidate] = Field(default_factory=list)
    combinations_evaluated: int = Field(default=0, ge=0)
    cost_rejection_reasons: list[str] = Field(default_factory=list)


class DepthAwarePayoffScanner:
    """Prefix-depth search feeding the generalized max-min solver. Backs/buys only."""

    def __init__(self, solver: GeneralizedMaxMinSolver | None = None) -> None:
        self.solver = solver or GeneralizedMaxMinSolver()

    def scan(
        self,
        sources: list[DepthQuoteSource],
        *,
        state_model: GeneralizedStateModel,
        capital_limit: Decimal | None = None,
        venue_capital_limits: Mapping[VenueName, Decimal] | None = None,
        configured_slippage_bps: Decimal | None = None,
    ) -> PayoffScanResult:
        states, mapping = _state_mapping(state_model)
        options: list[list[DepthQuoteCandidate]] = []
        cost_reasons: list[str] = []
        for source in sources:
            if source.outcome not in mapping:
                continue
            candidates, reasons = prefix_depth_candidates(
                source,
                configured_slippage_bps=configured_slippage_bps,
            )
            cost_reasons.extend(reasons)
            if candidates:
                options.append(candidates)

        cost_reasons = list(dict.fromkeys(cost_reasons))
        if len(options) < 1:
            return PayoffScanResult(
                solution=PayoffSolution(
                    is_arbitrage=False,
                    rejection_reason=cost_reasons[0] if cost_reasons else "missing_executable_outcome_depth",
                ),
                cost_rejection_reasons=cost_reasons,
            )

        best: PayoffSolution | None = None
        best_quotes: list[DepthQuoteCandidate] = []
        combinations_evaluated = 0
        limits = dict(venue_capital_limits) if venue_capital_limits else None

        for combination in product(*options):
            combinations_evaluated += 1
            problem = _problem_from_quotes(
                combination,
                states=states,
                mapping=mapping,
                capital_limit=capital_limit,
                venue_capital_limits=limits,
            )
            if problem is None:
                continue
            solution = self.solver.solve(problem)
            if best is None or _better(solution, best):
                best = solution
                best_quotes = list(combination)

        if best is None:
            return PayoffScanResult(
                solution=PayoffSolution(
                    is_arbitrage=False,
                    rejection_reason=cost_reasons[0] if cost_reasons else "malformed_payoff_problem",
                ),
                combinations_evaluated=combinations_evaluated,
                cost_rejection_reasons=cost_reasons,
            )
        return PayoffScanResult(
            solution=best,
            selected_quotes=best_quotes,
            combinations_evaluated=combinations_evaluated,
            cost_rejection_reasons=cost_reasons,
        )


def _state_mapping(
    state_model: GeneralizedStateModel,
) -> tuple[tuple[str, ...], dict[str, tuple[tuple[str, ...], tuple[str, ...]]]]:
    if state_model is GeneralizedStateModel.DRAW_NO_BET:
        mapping = {
            "home": (("home",), ("draw",)),
            "away": (("away",), ("draw",)),
        }
        return DNB_STATES, mapping
    if state_model is GeneralizedStateModel.INTEGER_TOTAL_GOALS:
        mapping = {
            "over": (("over",), ("push",)),
            "under": (("under",), ("push",)),
        }
        return INTEGER_TOTAL_STATES, mapping
    raise ValueError("unsupported_generalized_state_model")


def _problem_from_quotes(
    quotes: Sequence[DepthQuoteCandidate],
    *,
    states: Sequence[str],
    mapping: Mapping[str, tuple[tuple[str, ...], tuple[str, ...]]],
    capital_limit: Decimal | None,
    venue_capital_limits: dict[VenueName, Decimal] | None,
) -> PayoffProblem | None:
    legs = []
    for index, quote in enumerate(quotes):
        win_refund = mapping.get(quote.outcome)
        if win_refund is None:
            return None
        win_states, refund_states = win_refund
        legs.append(
            payoff_leg_from_back(
                leg_id=f"{quote.venue.value}:{quote.source_market_id}:{quote.outcome}:{index}",
                venue=quote.venue,
                source_market_id=quote.source_market_id,
                source_runner_id=quote.source_runner_id,
                runner_outcome=quote.outcome,
                max_stake=quote.cumulative_depth,
                net_decimal_odds=quote.net_decimal_odds,
                states=states,
                win_states=win_states,
                refund_states=refund_states,
                metadata={"levels_consumed": str(quote.levels_consumed)},
            )
        )
    try:
        return PayoffProblem(
            states=list(states),
            legs=legs,
            capital_limit=capital_limit,
            venue_capital_limits=venue_capital_limits,
        )
    except ValueError:
        return None


def _better(candidate: PayoffSolution, incumbent: PayoffSolution) -> bool:
    if candidate.numerically_validated != incumbent.numerically_validated:
        return candidate.numerically_validated
    if candidate.is_arbitrage != incumbent.is_arbitrage:
        return candidate.is_arbitrage
    if candidate.minimum_state_pnl != incumbent.minimum_state_pnl:
        return candidate.minimum_state_pnl > incumbent.minimum_state_pnl
    if candidate.roi != incumbent.roi:
        return candidate.roi > incumbent.roi
    return candidate.total_capital_used > incumbent.total_capital_used
