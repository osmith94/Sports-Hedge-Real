"""Narrow FTTS generalized-arb → ordinary complete-set Priority Alert bridge.

Issue #331 / architect review: FTTS uses the generalized payoff solver, while
Priority Alerts ingest `depth_scan`. After a complete HOME/AWAY/NO_GOAL
generalized arbitrage on a register-admitted FTTS_FT row, re-solve the
selected executable quotes with the ordinary complete-set solver and attach
that as `depth_scan`.

The bridge is keyed from Approved Match Register identity
(`approved_match_register` + `canonical_key=FTTS_FT`), not merely
generalized_payoff + HOME/AWAY/NO_GOAL shape.

Do not use this for DNB, integer totals, or other push/refund models.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal

from sports_hedge.arbitrage.depth import DepthQuoteCandidate, DepthScanResult
from sports_hedge.arbitrage.payoff_scan import PayoffScanResult
from sports_hedge.arbitrage.solver import CompleteSetArbitrageSolver
from sports_hedge.domain.football import CanonicalOutcome, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.approved_register import (
    CANONICAL_FTTS_FT,
    REGISTER_ADMITTED_REASON,
    register_key_reason,
)
from sports_hedge.paper.models import PaperScanDecision

FTTS_ORDINARY_OUTCOMES = frozenset(
    {
        CanonicalOutcome.HOME.value,
        CanonicalOutcome.AWAY.value,
        CanonicalOutcome.NO_GOAL.value,
    }
)


def _best_quote_per_ftts_outcome(
    quotes: list[DepthQuoteCandidate],
) -> list[DepthQuoteCandidate] | None:
    """One executable quote per HOME/AWAY/NO_GOAL. Highest net odds wins."""

    best: dict[str, DepthQuoteCandidate] = {}
    for quote in quotes:
        if quote.outcome not in FTTS_ORDINARY_OUTCOMES:
            continue
        current = best.get(quote.outcome)
        if current is None or quote.net_decimal_odds > current.net_decimal_odds:
            best[quote.outcome] = quote
    if set(best) != FTTS_ORDINARY_OUTCOMES:
        return None
    return [best[outcome] for outcome in sorted(FTTS_ORDINARY_OUTCOMES)]


def project_ftts_payoff_to_ordinary_depth(
    payoff_scan: PayoffScanResult | None,
    *,
    capital_limit: Decimal | None = None,
    venue_capital_limits: Mapping[VenueName, Decimal] | None = None,
    solver: CompleteSetArbitrageSolver | None = None,
) -> DepthScanResult | None:
    """Re-solve selected FTTS quotes as an ordinary 3-way complete set."""

    if payoff_scan is None or not payoff_scan.solution.is_arbitrage:
        return None
    selected = _best_quote_per_ftts_outcome(list(payoff_scan.selected_quotes))
    if selected is None:
        return None
    quotes = [quote.as_executable_quote() for quote in selected]
    ordinary = (solver or CompleteSetArbitrageSolver()).solve(
        quotes,
        capital_limit=capital_limit,
        venue_capital_limits=venue_capital_limits,
    )
    if not ordinary.is_arbitrage:
        return None
    return DepthScanResult(
        solution=ordinary,
        selected_quotes=selected,
        combinations_evaluated=max(1, payoff_scan.combinations_evaluated),
        cost_rejection_reasons=list(payoff_scan.cost_rejection_reasons),
    )


def ftts_register_admitted(decision: PaperScanDecision) -> bool:
    """True only for an Approved Match Register FTTS_FT row."""

    reasons = decision.market_match.reasons
    return (
        REGISTER_ADMITTED_REASON in reasons
        and register_key_reason(CANONICAL_FTTS_FT) in reasons
    )


def ftts_family_from_decision(decision: PaperScanDecision) -> bool:
    if not ftts_register_admitted(decision):
        return False
    if decision.solver_model != "generalized_payoff":
        return False
    if decision.payoff_scan is None:
        return False
    outcomes = {quote.outcome for quote in decision.payoff_scan.selected_quotes}
    return outcomes == FTTS_ORDINARY_OUTCOMES


def attach_ftts_ordinary_depth(
    decision: PaperScanDecision,
    *,
    capital_limit: Decimal | None = None,
    venue_capital_limits: Mapping[VenueName, Decimal] | None = None,
) -> PaperScanDecision:
    """Return a copy with ordinary depth_scan when FTTS generalized arb projects."""

    if decision.depth_scan is not None and decision.depth_scan.solution.is_arbitrage:
        return decision
    if not ftts_family_from_decision(decision):
        return decision
    projected = project_ftts_payoff_to_ordinary_depth(
        decision.payoff_scan,
        capital_limit=capital_limit,
        venue_capital_limits=venue_capital_limits,
    )
    if projected is None:
        return decision
    return decision.model_copy(update={"depth_scan": projected})


def is_complete_ftts_pair(left_family: MarketFamily, right_family: MarketFamily) -> bool:
    return (
        left_family is MarketFamily.FIRST_TEAM_TO_SCORE
        and right_family is MarketFamily.FIRST_TEAM_TO_SCORE
    )
