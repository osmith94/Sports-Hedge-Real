from __future__ import annotations

from decimal import Decimal
from enum import StrEnum

from pydantic import BaseModel, Field

from sports_hedge.arbitrage.watchlist.economics import (
    NEAR_ELIGIBLE_REASONS,
    quantized_edge,
)
from sports_hedge.fees.cost import MarketAction, OrderRole, VenueCostSnapshot
from sports_hedge.paper.models import PaperScanDecision

NO_EXECUTABLE_ARB = "no_executable_arb"
PASSIVE_MAKER_NOT_EXECUTABLE = "passive_maker_not_executable"
OPENING_LAY_NOT_SUPPORTED = "opening_lay_not_supported"
STALE_PASSIVE_LIQUIDITY = "stale_passive_liquidity"
DEFAULT_OPENING_MAX_QUOTE_AGE_MS = 2000
"""Aligned with Settings.paper_entry_max_quote_age_ms default."""

FAMILY_LABELS = {
    "match_result": "Match Result",
    "both_teams_to_score": "BTTS",
    "total_goals": "Total Goals",
    "first_team_to_score": "First Team To Score",
    "draw_no_bet": "Draw No Bet",
}

OUTCOME_LABELS = {
    "home": "Home",
    "away": "Away",
    "draw": "Draw",
    "yes": "Yes",
    "no": "No",
    "over": "Over",
    "under": "Under",
    "no_goal": "No Goal",
}

PASSIVE_REJECTION_REASONS = frozenset(
    {
        PASSIVE_MAKER_NOT_EXECUTABLE,
        STALE_PASSIVE_LIQUIDITY,
        OPENING_LAY_NOT_SUPPORTED,
        "opening_maker_not_supported",
    }
)
STALE_REJECTION_REASONS = frozenset(
    {
        "stale_quote",
        "unknown_quote_age",
        STALE_PASSIVE_LIQUIDITY,
        "stale_pre_event_quote",
        "stale_quote_age",
    }
)
HARD_NON_EXECUTABLE_REASONS = frozenset(
    {
        *PASSIVE_REJECTION_REASONS,
        *STALE_REJECTION_REASONS,
        "missing_executable_outcome_depth",
        "execution_risk_above_threshold",
        "fill_confidence_below_threshold",
        "insufficient_depth",
        "missing_risk_evidence",
        "paper_assumed_equivalent",
        "paper_assumed_not_live_execution_eligible",
    }
)


class LiquidityRole(StrEnum):
    TAKER = "taker"
    MAKER = "maker"
    UNKNOWN = "unknown"


class HeadlineBand(StrEnum):
    QUALIFYING = "qualifying"
    NEAR_EXECUTABLE = "near_executable"
    OBSERVED_NOT_EXECUTABLE = "observed_not_executable"
    NO_EXECUTABLE_ARB = "no_executable_arb"


class FixtureHeadlineCandidate(BaseModel):
    """One market-level comparison considered for the fixture-row headline.

    Scanner coverage is not reduced: every candidate remains available for
    drilldown. Only QUALIFYING or NEAR_EXECUTABLE bands may headline.
    """

    family: str | None = None
    line: Decimal | None = None
    selection: str | None = None
    current_net_edge: Decimal | None = None
    trigger_net_edge: Decimal | None = None
    eligible_for_paper_simulation: bool = False
    solver_is_arbitrage: bool = False
    rejection_reasons: list[str] = Field(default_factory=list)
    liquidity_role: LiquidityRole = LiquidityRole.TAKER
    quote_age_ms: int | None = Field(default=None, ge=0)


class FixtureHeadline(BaseModel):
    band: HeadlineBand
    candidate: FixtureHeadlineCandidate | None = None
    reason: str | None = None
    best_arb_market: str | None = None


def quote_freshness_from_age(*, quote_age_ms: int | None, max_quote_age_ms: int) -> bool:
    """True when the quote is within the freshness cap.

    Unchanged odds are irrelevant. Missing age is fail-closed (not fresh).
    """

    if max_quote_age_ms < 0:
        raise ValueError("max_quote_age_ms must be non-negative")
    if quote_age_ms is None:
        return False
    return quote_age_ms < max_quote_age_ms


def liquidity_role_from_costs(costs: list[VenueCostSnapshot]) -> LiquidityRole:
    """Opening role implied by cost snapshots. Maker anywhere is maker."""

    if not costs:
        return LiquidityRole.UNKNOWN
    if any(cost.order_role is OrderRole.MAKER for cost in costs):
        return LiquidityRole.MAKER
    if any(cost.order_role is OrderRole.UNKNOWN for cost in costs):
        return LiquidityRole.UNKNOWN
    if any(cost.action in {MarketAction.LAY, MarketAction.SELL} for cost in costs):
        return LiquidityRole.MAKER
    return LiquidityRole.TAKER


def opening_liquidity_rejection_reasons(
    costs: list[VenueCostSnapshot],
    *,
    quote_age_ms: int | None = None,
    max_quote_age_ms: int | None = None,
) -> list[str]:
    """Fail-closed opening-arb reasons for passive/maker and unrevalidated quotes."""

    reasons: list[str] = []
    role = liquidity_role_from_costs(costs)
    if any(cost.action in {MarketAction.LAY, MarketAction.SELL} for cost in costs):
        reasons.append(OPENING_LAY_NOT_SUPPORTED)
    if role is LiquidityRole.MAKER:
        reasons.append(PASSIVE_MAKER_NOT_EXECUTABLE)
    if role is LiquidityRole.UNKNOWN and any(
        cost.order_role is OrderRole.UNKNOWN for cost in costs
    ):
        reasons.append("unknown_order_role")
    stale = False
    if max_quote_age_ms is not None and not quote_freshness_from_age(
        quote_age_ms=quote_age_ms, max_quote_age_ms=max_quote_age_ms
    ):
        stale = True
        reasons.append("stale_quote" if quote_age_ms is not None else "unknown_quote_age")
    if role is LiquidityRole.MAKER and stale:
        reasons.append(STALE_PASSIVE_LIQUIDITY)
    return _dedupe(reasons)


def decision_net_edge(decision: PaperScanDecision) -> Decimal | None:
    if decision.payoff_scan is not None:
        return quantized_edge(decision.payoff_scan.solution.roi)
    if decision.depth_scan is None:
        return None
    implied = decision.depth_scan.solution.implied_probability_sum
    if implied <= 0:
        return None
    from sports_hedge.arbitrage.watchlist.economics import net_edge_from_implied_sum

    return net_edge_from_implied_sum(implied)


def decision_is_solver_arbitrage(decision: PaperScanDecision) -> bool:
    if decision.payoff_scan is not None:
        return bool(decision.payoff_scan.solution.is_arbitrage)
    if decision.depth_scan is None:
        return False
    return bool(decision.depth_scan.solution.is_arbitrage)


def decision_selection(decision: PaperScanDecision) -> str | None:
    if decision.payoff_scan is not None:
        stakes = decision.payoff_scan.solution.selected_stakes
        if stakes:
            best = max(stakes, key=lambda item: item.stake)
            return best.runner_outcome or best.leg_id
    if decision.depth_scan is not None:
        stakes = decision.depth_scan.solution.stakes
        if stakes:
            best = max(stakes, key=lambda item: item.net_decimal_odds)
            return best.outcome
    return None


def best_arb_market_label(
    family: str | None,
    *,
    selection: str | None = None,
    line: Decimal | None = None,
) -> str | None:
    if not family:
        return None
    label = FAMILY_LABELS.get(family, family.replace("_", " ").title())
    selection_label = _selection_label(selection, family=family, line=line)
    if selection_label:
        return f"{label} · {selection_label}"
    return label


def headline_band_for(candidate: FixtureHeadlineCandidate) -> HeadlineBand:
    reasons = set(candidate.rejection_reasons)
    if candidate.liquidity_role is LiquidityRole.MAKER:
        return HeadlineBand.OBSERVED_NOT_EXECUTABLE
    if candidate.liquidity_role is not LiquidityRole.TAKER:
        return HeadlineBand.OBSERVED_NOT_EXECUTABLE
    if reasons & HARD_NON_EXECUTABLE_REASONS:
        return HeadlineBand.OBSERVED_NOT_EXECUTABLE
    if candidate.current_net_edge is None:
        return HeadlineBand.NO_EXECUTABLE_ARB
    qualifying = (
        candidate.eligible_for_paper_simulation
        and candidate.solver_is_arbitrage
        and (
            candidate.trigger_net_edge is None
            or candidate.current_net_edge >= candidate.trigger_net_edge
        )
    )
    if qualifying:
        return HeadlineBand.QUALIFYING
    leftover = [reason for reason in reasons if reason not in NEAR_ELIGIBLE_REASONS]
    if leftover:
        return HeadlineBand.OBSERVED_NOT_EXECUTABLE
    if candidate.trigger_net_edge is not None and candidate.current_net_edge < candidate.trigger_net_edge:
        return HeadlineBand.NEAR_EXECUTABLE
    if candidate.solver_is_arbitrage and not candidate.eligible_for_paper_simulation:
        return HeadlineBand.OBSERVED_NOT_EXECUTABLE
    return HeadlineBand.NEAR_EXECUTABLE


def candidate_from_decision(
    decision: PaperScanDecision,
    *,
    family: str | None,
    line: Decimal | None = None,
    selection: str | None = None,
) -> FixtureHeadlineCandidate:
    role = liquidity_role_from_costs(decision.venue_costs)
    reasons = list(decision.rejection_reasons)
    if role is LiquidityRole.MAKER and PASSIVE_MAKER_NOT_EXECUTABLE not in reasons:
        reasons.append(PASSIVE_MAKER_NOT_EXECUTABLE)
    return FixtureHeadlineCandidate(
        family=family,
        line=line,
        selection=selection or decision_selection(decision),
        current_net_edge=decision_net_edge(decision),
        trigger_net_edge=decision.minimum_net_edge,
        eligible_for_paper_simulation=decision.eligible_for_paper_simulation,
        solver_is_arbitrage=decision_is_solver_arbitrage(decision),
        rejection_reasons=reasons,
        liquidity_role=role,
        quote_age_ms=decision.quote_age_ms,
    )


def select_fixture_headline(candidates: list[FixtureHeadlineCandidate]) -> FixtureHeadline:
    """Pick the fixture-row Best Arb without discarding other comparisons."""

    ranked: list[tuple[int, Decimal, FixtureHeadlineCandidate, HeadlineBand]] = []
    for candidate in candidates:
        band = headline_band_for(candidate)
        if band not in {HeadlineBand.QUALIFYING, HeadlineBand.NEAR_EXECUTABLE}:
            continue
        edge = candidate.current_net_edge if candidate.current_net_edge is not None else Decimal("-1")
        rank = 0 if band is HeadlineBand.QUALIFYING else 1
        ranked.append((rank, -edge, candidate, band))
    if not ranked:
        observed = any(
            headline_band_for(item) is HeadlineBand.OBSERVED_NOT_EXECUTABLE for item in candidates
        )
        reason = NO_EXECUTABLE_ARB
        if observed:
            first = next(
                (
                    item
                    for item in candidates
                    if headline_band_for(item) is HeadlineBand.OBSERVED_NOT_EXECUTABLE
                ),
                None,
            )
            if first is not None and first.rejection_reasons:
                reason = first.rejection_reasons[0]
            else:
                reason = NO_EXECUTABLE_ARB
        return FixtureHeadline(band=HeadlineBand.NO_EXECUTABLE_ARB, reason=reason)
    ranked.sort(key=lambda item: (item[0], item[1]))
    _, _, winner, band = ranked[0]
    return FixtureHeadline(
        band=band,
        candidate=winner,
        best_arb_market=best_arb_market_label(
            winner.family, selection=winner.selection, line=winner.line
        ),
    )


def should_replace_fixture_headline(
    current: FixtureHeadline | None,
    incoming: FixtureHeadline,
) -> bool:
    if incoming.band not in {HeadlineBand.QUALIFYING, HeadlineBand.NEAR_EXECUTABLE}:
        return False
    if current is None or current.candidate is None:
        return True
    if current.band not in {HeadlineBand.QUALIFYING, HeadlineBand.NEAR_EXECUTABLE}:
        return True
    current_rank = 0 if current.band is HeadlineBand.QUALIFYING else 1
    incoming_rank = 0 if incoming.band is HeadlineBand.QUALIFYING else 1
    if incoming_rank < current_rank:
        return True
    if incoming_rank > current_rank:
        return False
    current_edge = current.candidate.current_net_edge or Decimal("-1")
    incoming_edge = incoming.candidate.current_net_edge if incoming.candidate else Decimal("-1")
    return incoming_edge > current_edge


def _selection_label(selection: str | None, *, family: str, line: Decimal | None) -> str | None:
    if not selection:
        return None
    key = selection.strip().lower()
    if family == "total_goals" and key in {"over", "under"} and line is not None:
        line_text = format(line, "f").rstrip("0").rstrip(".") if "." in format(line, "f") else format(line, "f")
        return f"{OUTCOME_LABELS[key]} {line_text}"
    return OUTCOME_LABELS.get(key, selection.replace("_", " ").title())


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for item in values:
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
    return result
