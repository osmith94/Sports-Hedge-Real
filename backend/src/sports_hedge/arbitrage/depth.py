from __future__ import annotations

from decimal import Decimal
from itertools import product

from pydantic import BaseModel, Field

from sports_hedge.arbitrage.models import ArbitrageSolution, ExecutableQuote
from sports_hedge.arbitrage.solver import CompleteSetArbitrageSolver
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import VenueCostSnapshot
from sports_hedge.fees.effective import CostRuleError, apply_venue_costs
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.paper.fills import apply_odds_haircut


class DepthQuoteSource(BaseModel):
    outcome: str
    venue: VenueName
    source_market_id: str
    source_runner_id: str
    levels: list[BookLevel]
    cost: VenueCostSnapshot


class DepthQuoteCandidate(BaseModel):
    outcome: str
    venue: VenueName
    source_market_id: str
    source_runner_id: str
    gross_weighted_odds: Decimal
    net_decimal_odds: Decimal
    cumulative_depth: Decimal
    levels_consumed: int = Field(ge=1)

    def as_executable_quote(self) -> ExecutableQuote:
        return ExecutableQuote(
            outcome=self.outcome,
            venue=self.venue,
            source_market_id=self.source_market_id,
            net_decimal_odds=self.net_decimal_odds,
            max_stake=self.cumulative_depth,
        )


class DepthScanResult(BaseModel):
    solution: ArbitrageSolution
    selected_quotes: list[DepthQuoteCandidate] = Field(default_factory=list)
    combinations_evaluated: int = Field(default=0, ge=0)
    cost_rejection_reasons: list[str] = Field(default_factory=list)


class DepthAwareCompleteSetScanner:
    """Conservative prefix-depth search over executable back/buy books.

    Each order-book prefix is represented by its weighted-average price and full
    cumulative depth. If the reciprocal solver ultimately uses less than that
    cumulative depth, retaining the prefix average is conservative because deeper
    levels can only worsen (or equal) the average price in a properly ordered back
    book. The search is deliberately small and deterministic for Phase 1.
    """

    def __init__(self, solver: CompleteSetArbitrageSolver | None = None) -> None:
        self.solver = solver or CompleteSetArbitrageSolver()

    def scan(
        self,
        sources: list[DepthQuoteSource],
        *,
        expected_outcomes: list[str],
        capital_limit: Decimal | None = None,
        configured_slippage_bps: Decimal | None = None,
    ) -> DepthScanResult:
        expected = list(dict.fromkeys(expected_outcomes))
        if len(expected) < 2:
            return DepthScanResult(
                solution=ArbitrageSolution(
                    is_arbitrage=False,
                    implied_probability_sum=Decimal("1"),
                    rejection_reason="incomplete_outcome_set",
                )
            )

        options_by_outcome: dict[str, list[DepthQuoteCandidate]] = {outcome: [] for outcome in expected}
        cost_reasons: list[str] = []
        for source in sources:
            if source.outcome not in options_by_outcome:
                continue
            candidates, reasons = _prefix_candidates(
                source,
                configured_slippage_bps=configured_slippage_bps,
            )
            cost_reasons.extend(reasons)
            options_by_outcome[source.outcome].extend(candidates)

        cost_reasons = list(dict.fromkeys(cost_reasons))
        if any(not options for options in options_by_outcome.values()):
            reason = cost_reasons[0] if cost_reasons else "missing_executable_outcome_depth"
            return DepthScanResult(
                solution=ArbitrageSolution(
                    is_arbitrage=False,
                    implied_probability_sum=Decimal("1"),
                    rejection_reason=reason,
                ),
                cost_rejection_reasons=cost_reasons,
            )

        best_solution: ArbitrageSolution | None = None
        best_candidates: list[DepthQuoteCandidate] = []
        combinations_evaluated = 0

        ordered_options = [options_by_outcome[outcome] for outcome in expected]
        for combination in product(*ordered_options):
            combinations_evaluated += 1
            solution = self.solver.solve(
                [candidate.as_executable_quote() for candidate in combination],
                capital_limit=capital_limit,
            )
            if not solution.is_arbitrage:
                continue
            if best_solution is None or _better(solution, best_solution):
                best_solution = solution
                best_candidates = list(combination)

        if best_solution is None:
            best_top = [
                max(options_by_outcome[outcome], key=lambda item: item.net_decimal_odds)
                for outcome in expected
            ]
            rejected = self.solver.solve(
                [candidate.as_executable_quote() for candidate in best_top],
                capital_limit=capital_limit,
            )
            return DepthScanResult(
                solution=rejected,
                selected_quotes=best_top,
                combinations_evaluated=combinations_evaluated,
                cost_rejection_reasons=cost_reasons,
            )

        return DepthScanResult(
            solution=best_solution,
            selected_quotes=best_candidates,
            combinations_evaluated=combinations_evaluated,
            cost_rejection_reasons=cost_reasons,
        )


def _prefix_candidates(
    source: DepthQuoteSource,
    *,
    configured_slippage_bps: Decimal | None,
) -> tuple[list[DepthQuoteCandidate], list[str]]:
    ordered = sorted(source.levels, key=lambda item: item.decimal_odds, reverse=True)
    candidates: list[DepthQuoteCandidate] = []
    cost_reasons: list[str] = []
    cumulative_stake = Decimal("0")
    cumulative_return = Decimal("0")
    for index, level in enumerate(ordered, start=1):
        cumulative_stake += level.available_stake
        cumulative_return += level.available_stake * level.decimal_odds
        gross_average = cumulative_return / cumulative_stake
        try:
            economics = apply_venue_costs(
                source.cost,
                gross_decimal_odds=gross_average,
                require_gbp=False,
            )
        except CostRuleError as exc:
            cost_reasons.append(exc.reason)
            continue
        net_average = economics.net_decimal_equivalent
        if configured_slippage_bps:
            net_average = apply_odds_haircut(net_average, configured_slippage_bps)
        if net_average <= 1:
            continue
        candidates.append(
            DepthQuoteCandidate(
                outcome=source.outcome,
                venue=source.venue,
                source_market_id=source.source_market_id,
                source_runner_id=source.source_runner_id,
                gross_weighted_odds=gross_average,
                net_decimal_odds=net_average,
                cumulative_depth=cumulative_stake,
                levels_consumed=index,
            )
        )
    return candidates, list(dict.fromkeys(cost_reasons))


def _better(candidate: ArbitrageSolution, incumbent: ArbitrageSolution) -> bool:
    if candidate.guaranteed_profit != incumbent.guaranteed_profit:
        return candidate.guaranteed_profit > incumbent.guaranteed_profit
    if candidate.roi != incumbent.roi:
        return candidate.roi > incumbent.roi
    return candidate.total_stake > incumbent.total_stake
