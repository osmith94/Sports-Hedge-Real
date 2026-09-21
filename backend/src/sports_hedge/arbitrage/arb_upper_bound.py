"""Conservative optimistic upper bound on achievable net arb.

Stop extra expensive provider calls only when a mathematically safe bound
proves Min Net Arb cannot be reached. Missing prices, fees, or FX make the
bound uncertain and must continue work.

Unknown remaining legs are modelled at implied probability 0 (free covering).
Known fees/FX can only reduce net edge, so omitting unknown costs is
optimistic and safe for pruning. Do not invent fees of zero to *declare*
an arb — that remains fail-closed in the solver.

PAPER / read-only. No threshold/equivalence changes.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sports_hedge.application.market_observation import (
    VenueMarketObservation,
    _kalshi_buy_levels,
)
from sports_hedge.arbitrage.watchlist.economics import net_edge_from_implied_sum

CONTINUE = "continue"
PRUNE = "prune"
UNCERTAIN = "uncertain"


@dataclass(frozen=True)
class UpperBoundDecision:
    """Value-of-information decision for remaining provider calls."""

    action: str
    upper_bound_net_edge: Decimal | None
    implied_sum: Decimal | None
    certain: bool
    reason: str | None = None

    @property
    def prune(self) -> bool:
        return self.action == PRUNE and self.certain


def optimistic_net_edge_upper_bound(
    *,
    required_outcomes: list[str],
    known_implied: Mapping[str, Decimal],
    unknown_outcomes: list[str],
    minimum_net_edge: Decimal,
) -> UpperBoundDecision:
    """Upper-bound net edge from known implieds plus free unknown legs.

    For each required outcome take the cheapest known implied probability.
    Remaining unknown outcomes that unknown legs can still cover contribute 0.
    If even that optimistic covering cannot beat Min Net Arb, prune.
    """

    if not required_outcomes:
        return UpperBoundDecision(
            action=UNCERTAIN,
            upper_bound_net_edge=None,
            implied_sum=None,
            certain=False,
            reason="missing_required_outcomes",
        )
    if minimum_net_edge < 0:
        return UpperBoundDecision(
            action=UNCERTAIN,
            upper_bound_net_edge=None,
            implied_sum=None,
            certain=False,
            reason="invalid_min_net_edge",
        )
    unknown = {str(item) for item in unknown_outcomes}
    optimistic: dict[str, Decimal] = {}
    for outcome, implied in known_implied.items():
        if implied < 0:
            return UpperBoundDecision(
                action=UNCERTAIN,
                upper_bound_net_edge=None,
                implied_sum=None,
                certain=False,
                reason="negative_implied",
            )
        key = str(outcome)
        current = optimistic.get(key)
        optimistic[key] = implied if current is None else min(current, implied)
    for outcome in unknown:
        optimistic[str(outcome)] = Decimal("0")
    implied_sum = Decimal("0")
    for outcome in required_outcomes:
        key = str(outcome)
        if key not in optimistic:
            return UpperBoundDecision(
                action=UNCERTAIN,
                upper_bound_net_edge=None,
                implied_sum=None,
                certain=False,
                reason="uncovered_required_outcome",
            )
        implied_sum += optimistic[key]
    if implied_sum <= 0:
        return UpperBoundDecision(
            action=CONTINUE,
            upper_bound_net_edge=None,
            implied_sum=implied_sum,
            certain=False,
            reason="unbounded_optimistic_cover",
        )
    try:
        upper = net_edge_from_implied_sum(implied_sum)
    except (InvalidOperation, ValueError):
        return UpperBoundDecision(
            action=UNCERTAIN,
            upper_bound_net_edge=None,
            implied_sum=implied_sum,
            certain=False,
            reason="implied_sum_unusable",
        )
    if upper >= minimum_net_edge:
        return UpperBoundDecision(
            action=CONTINUE,
            upper_bound_net_edge=upper,
            implied_sum=implied_sum,
            certain=True,
            reason="bound_meets_or_exceeds_min_net",
        )
    return UpperBoundDecision(
        action=PRUNE,
        upper_bound_net_edge=upper,
        implied_sum=implied_sum,
        certain=True,
        reason="upper_bound_below_min_net",
    )


def implied_from_matchbook_market(payload: Any) -> dict[str, Decimal]:
    """Gross back implied probabilities from a Matchbook market payload."""

    if not isinstance(payload, Mapping):
        return {}
    runners = payload.get("runners")
    if not isinstance(runners, list):
        return {}
    found: dict[str, Decimal] = {}
    for runner in runners:
        if not isinstance(runner, Mapping):
            continue
        outcome = _matchbook_runner_outcome(runner)
        if outcome is None:
            continue
        odds = _matchbook_best_back_odds(runner)
        if odds is None or odds <= 1:
            continue
        implied = Decimal("1") / odds
        current = found.get(outcome)
        found[outcome] = implied if current is None else min(current, implied)
    return found


def _matchbook_best_back_odds(runner: Mapping[str, Any]) -> Decimal | None:
    prices = runner.get("prices")
    if not isinstance(prices, list):
        return None
    best: Decimal | None = None
    for price in prices:
        if not isinstance(price, Mapping):
            continue
        side = str(price.get("side") or "").strip().casefold()
        if side not in {"back", "win"}:
            continue
        try:
            odds = Decimal(str(price.get("odds") or price.get("price") or ""))
        except (InvalidOperation, ValueError):
            continue
        if odds <= 1:
            continue
        best = odds if best is None else max(best, odds)
    return best


def _matchbook_runner_outcome(runner: Mapping[str, Any]) -> str | None:
    name = str(runner.get("name") or runner.get("runner-name") or "").strip().casefold()
    if name in {"home", "1", "yes"}:
        return "home" if name != "yes" else "yes"
    if name in {"draw", "x", "the draw"}:
        return "draw"
    if name in {"away", "2", "no"}:
        return "away" if name != "no" else "no"
    if "draw" in name:
        return "draw"
    if name in {"yes", "both teams to score"}:
        return "yes"
    return None


def implied_from_observation(observation: VenueMarketObservation) -> dict[str, Decimal]:
    """Gross best-back implied probabilities. Missing books are omitted."""

    payload: dict[str, Decimal] = {}
    for book in observation.outcome_books:
        best = book.best_back
        if best is None or best.decimal_odds is None or best.decimal_odds <= 1:
            continue
        payload[str(book.outcome.value)] = Decimal("1") / best.decimal_odds
    return payload


def implied_from_kalshi_book(payload: Any) -> Decimal | None:
    """Cheapest YES-buy implied from one Kalshi order-book payload."""

    if not isinstance(payload, Mapping):
        return None
    levels = _kalshi_buy_levels(payload, side="YES")
    if not levels:
        return None
    best = max(levels, key=lambda item: item.decimal_odds)
    if best.decimal_odds <= 1:
        return None
    return Decimal("1") / best.decimal_odds


def merge_known_implied(
    *groups: Mapping[str, Decimal] | None,
) -> dict[str, Decimal]:
    merged: dict[str, Decimal] = {}
    for group in groups:
        if not group:
            continue
        for outcome, implied in group.items():
            key = str(outcome)
            current = merged.get(key)
            merged[key] = implied if current is None else min(current, implied)
    return merged
