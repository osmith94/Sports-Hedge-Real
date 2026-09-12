from __future__ import annotations

from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.application.market_observation import VenueMarketObservation
from sports_hedge.domain.football import (
    CanonicalMarket,
    CanonicalOutcome,
    FootballPeriod,
    MarketFamily,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import VenueCostSnapshot
from sports_hedge.matching.markets import MarketMatchResult, MarketMatcher
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision


class InventoryComparisonStatus(StrEnum):
    MATCHED_EQUIVALENT = "matched_equivalent"
    VENUE_ONLY = "venue_only"
    SETTLEMENT_MISMATCH = "settlement_mismatch"
    UNSUPPORTED_OUTCOME_MODEL = "unsupported_outcome_model"
    UNSUPPORTED_FAMILY = "unsupported_family"
    MISSING_COSTS = "missing_costs"
    MISSING_FX = "missing_fx"
    STALE = "stale"
    OTHER = "other"


class VenueQuoteFact(BaseModel):
    outcome: str
    decimal_odds: Decimal | None = None
    size_at_touch: Decimal | None = None


class VenueMarketFacts(BaseModel):
    venue: VenueName
    source_event_id: str
    source_market_id: str
    family: str | None = None
    period: str | None = None
    line: Decimal | None = None
    settlement_key: str | None = None
    settlement_complete: bool | None = None
    best_backs: list[VenueQuoteFact] = Field(default_factory=list)
    usable_depth_at_touch: Decimal | None = None
    observed_at: str | None = None
    quote_age_ms: int | None = Field(default=None, ge=0)
    quote_age_basis: str | None = None
    native_currency: str | None = None
    fee_status: str | None = None
    fee_source: str | None = None
    fx_status: str | None = None


class FixtureMarketInventoryRow(BaseModel):
    display_name: str
    family: str | None = None
    period: str | None = None
    line: Decimal | None = None
    comparison_status: InventoryComparisonStatus
    reason: str | None = None
    rejection_reasons: list[str] = Field(default_factory=list)
    match_reasons: list[str] = Field(default_factory=list)
    entered_solver: bool = False
    current_net_edge: Decimal | None = None
    trigger_net_edge: Decimal | None = None
    distance_to_trigger_pp: Decimal | None = None
    solver_is_arbitrage: bool = False
    matchbook: VenueMarketFacts | None = None
    polymarket: VenueMarketFacts | None = None


class InventoryMarket(BaseModel):
    venue: VenueName
    source_event_id: str
    source_market_id: str
    raw_name: str
    canonical: CanonicalMarket | None = None
    observation: VenueMarketObservation | None = None
    normalize_error: str | None = None


def solver_eligible_market(market: CanonicalMarket) -> bool:
    """Phase 1 solver may inspect only complete canonical outcome spaces.

    Unsupported families and OTHER/correct-score-style runners stay visible in
    inventory but must not enter paper_scan / the complete-set solver.
    """

    if market.family in {MarketFamily.UNKNOWN, MarketFamily.CORRECT_SCORE}:
        return False
    if any(runner.outcome is CanonicalOutcome.OTHER for runner in market.runners):
        return False
    return True


def solver_eligible_pair(left: CanonicalMarket, right: CanonicalMarket, match: MarketMatchResult) -> bool:
    return bool(
        match.matched
        and solver_eligible_market(left)
        and solver_eligible_market(right)
    )


def market_group_key(market: CanonicalMarket) -> tuple[str, str, str]:
    line = "" if market.line is None else format(market.line, "f")
    return (market.family.value, market.period.value, line)


def market_display_name(
    *,
    family: str | None,
    period: str | None,
    line: Decimal | None,
    fallback: str,
) -> str:
    if not family:
        return fallback
    label = family.replace("_", " ").title()
    if line is not None:
        line_text = format(line, "f").rstrip("0").rstrip(".") if "." in format(line, "f") else format(line, "f")
        label = f"{label} {line_text}"
    if period and period not in {FootballPeriod.FULL_TIME.value, FootballPeriod.UNKNOWN.value}:
        label = f"{label} · {period.replace('_', ' ')}"
    return label


def assemble_fixture_inventory(
    matchbook_markets: list[InventoryMarket],
    polymarket_markets: list[InventoryMarket],
    *,
    matcher: MarketMatcher | None = None,
    decisions_by_source_ids: dict[tuple[str, str], PaperScanDecision] | None = None,
    venue_costs: list[VenueCostSnapshot] | None = None,
    fx_snapshots: list[FxRateSnapshot] | None = None,
) -> list[FixtureMarketInventoryRow]:
    """Group every discovered market. Never drops unsupported or rejected rows."""

    matcher = matcher or MarketMatcher()
    decisions = decisions_by_source_ids or {}
    rows: list[FixtureMarketInventoryRow] = []

    unmatched_left: list[InventoryMarket] = []
    unmatched_right: list[InventoryMarket] = []
    for item in matchbook_markets:
        if item.canonical is None:
            rows.append(_unnormalized_row(item, venue_costs=venue_costs, fx_snapshots=fx_snapshots))
        else:
            unmatched_left.append(item)
    for item in polymarket_markets:
        if item.canonical is None:
            rows.append(_unnormalized_row(item, venue_costs=venue_costs, fx_snapshots=fx_snapshots))
        else:
            unmatched_right.append(item)

    paired_indexes_left: set[int] = set()
    paired_indexes_right: set[int] = set()

    equivalent = _greedy_pairs(unmatched_left, unmatched_right, matcher, matched_only=True)
    for left_index, right_index, match in equivalent:
        paired_indexes_left.add(left_index)
        paired_indexes_right.add(right_index)
        left = unmatched_left[left_index]
        right = unmatched_right[right_index]
        rows.append(
            _paired_row(
                left,
                right,
                match,
                decision=decisions.get((left.source_market_id, right.source_market_id)),
                venue_costs=venue_costs,
                fx_snapshots=fx_snapshots,
            )
        )

    leftover_left = [item for index, item in enumerate(unmatched_left) if index not in paired_indexes_left]
    leftover_right = [item for index, item in enumerate(unmatched_right) if index not in paired_indexes_right]
    grouped_left: set[int] = set()
    grouped_right: set[int] = set()
    for left_index, left in enumerate(leftover_left):
        assert left.canonical is not None
        key = market_group_key(left.canonical)
        for right_index, right in enumerate(leftover_right):
            if right_index in grouped_right:
                continue
            assert right.canonical is not None
            if market_group_key(right.canonical) != key:
                continue
            grouped_left.add(left_index)
            grouped_right.add(right_index)
            match = matcher.match(left.canonical, right.canonical)
            rows.append(
                _paired_row(
                    left,
                    right,
                    match,
                    decision=None,
                    venue_costs=venue_costs,
                    fx_snapshots=fx_snapshots,
                )
            )
            break

    for index, left in enumerate(leftover_left):
        if index not in grouped_left:
            rows.append(_venue_only_row(left, venue_costs=venue_costs, fx_snapshots=fx_snapshots))
    for index, right in enumerate(leftover_right):
        if index not in grouped_right:
            rows.append(_venue_only_row(right, venue_costs=venue_costs, fx_snapshots=fx_snapshots))

    return _sort_rows(rows)


def inventory_summary(rows: list[FixtureMarketInventoryRow]) -> tuple[int, int, Decimal | None]:
    discovered = len(rows)
    equivalent = sum(
        1 for row in rows if row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT
    )
    edges = [row.current_net_edge for row in rows if row.current_net_edge is not None]
    best = max(edges) if edges else None
    return discovered, equivalent, best


def _greedy_pairs(
    left: list[InventoryMarket],
    right: list[InventoryMarket],
    matcher: MarketMatcher,
    *,
    matched_only: bool,
) -> list[tuple[int, int, MarketMatchResult]]:
    candidates: list[tuple[float, int, int, MarketMatchResult]] = []
    for left_index, left_item in enumerate(left):
        if left_item.canonical is None:
            continue
        for right_index, right_item in enumerate(right):
            if right_item.canonical is None:
                continue
            match = matcher.match(left_item.canonical, right_item.canonical)
            if matched_only and not match.matched:
                continue
            candidates.append((match.confidence, left_index, right_index, match))
    candidates.sort(key=lambda item: item[0], reverse=True)
    used_left: set[int] = set()
    used_right: set[int] = set()
    result: list[tuple[int, int, MarketMatchResult]] = []
    for _, left_index, right_index, match in candidates:
        if left_index in used_left or right_index in used_right:
            continue
        used_left.add(left_index)
        used_right.add(right_index)
        result.append((left_index, right_index, match))
    return result


def _unnormalized_row(
    item: InventoryMarket,
    *,
    venue_costs: list[VenueCostSnapshot] | None,
    fx_snapshots: list[FxRateSnapshot] | None,
) -> FixtureMarketInventoryRow:
    facts = _facts_from_inventory(
        item,
        venue_costs=venue_costs,
        fx_snapshots=fx_snapshots,
    )
    return FixtureMarketInventoryRow(
        display_name=item.raw_name or item.source_market_id,
        family=None,
        comparison_status=InventoryComparisonStatus.UNSUPPORTED_FAMILY,
        reason=item.normalize_error or "unsupported_family",
        rejection_reasons=[item.normalize_error] if item.normalize_error else ["unsupported_family"],
        matchbook=facts if item.venue is VenueName.MATCHBOOK else None,
        polymarket=facts if item.venue is VenueName.POLYMARKET else None,
    )


def _venue_only_row(
    item: InventoryMarket,
    *,
    venue_costs: list[VenueCostSnapshot] | None,
    fx_snapshots: list[FxRateSnapshot] | None,
) -> FixtureMarketInventoryRow:
    canonical = item.canonical
    status = InventoryComparisonStatus.VENUE_ONLY
    reason = "venue_only"
    reasons = ["venue_only"]
    if canonical is None:
        return _unnormalized_row(item, venue_costs=venue_costs, fx_snapshots=fx_snapshots)
    if canonical.family is MarketFamily.UNKNOWN:
        status = InventoryComparisonStatus.UNSUPPORTED_FAMILY
        reason = "unsupported_family"
        reasons = ["unsupported_family"]
    elif not solver_eligible_market(canonical):
        status = InventoryComparisonStatus.UNSUPPORTED_OUTCOME_MODEL
        reason = "unsupported_outcome_model"
        reasons = ["unsupported_outcome_model"]
    facts = _facts_from_inventory(item, venue_costs=venue_costs, fx_snapshots=fx_snapshots)
    return FixtureMarketInventoryRow(
        display_name=market_display_name(
            family=canonical.family.value,
            period=canonical.period.value,
            line=canonical.line,
            fallback=item.raw_name,
        ),
        family=canonical.family.value,
        period=canonical.period.value,
        line=canonical.line,
        comparison_status=status,
        reason=reason,
        rejection_reasons=reasons,
        matchbook=facts if item.venue is VenueName.MATCHBOOK else None,
        polymarket=facts if item.venue is VenueName.POLYMARKET else None,
    )


def _paired_row(
    left: InventoryMarket,
    right: InventoryMarket,
    match: MarketMatchResult,
    *,
    decision: PaperScanDecision | None,
    venue_costs: list[VenueCostSnapshot] | None,
    fx_snapshots: list[FxRateSnapshot] | None,
) -> FixtureMarketInventoryRow:
    canonical = left.canonical or right.canonical
    family = canonical.family.value if canonical else None
    period = canonical.period.value if canonical else None
    line = canonical.line if canonical else None
    status, reason, rejection_reasons, entered = _classify_pair(
        left,
        right,
        match,
        decision=decision,
    )
    return FixtureMarketInventoryRow(
        display_name=market_display_name(
            family=family,
            period=period,
            line=line,
            fallback=left.raw_name or right.raw_name,
        ),
        family=family,
        period=period,
        line=line,
        comparison_status=status,
        reason=reason,
        rejection_reasons=rejection_reasons,
        match_reasons=list(match.reasons),
        entered_solver=entered,
        current_net_edge=_decision_net_edge(decision) if entered else None,
        trigger_net_edge=decision.minimum_net_edge if decision is not None and entered else None,
        distance_to_trigger_pp=_decision_distance(decision) if entered else None,
        solver_is_arbitrage=_decision_is_arb(decision) if entered else False,
        matchbook=_facts_from_inventory(left, venue_costs=venue_costs, fx_snapshots=fx_snapshots),
        polymarket=_facts_from_inventory(right, venue_costs=venue_costs, fx_snapshots=fx_snapshots),
    )


def _classify_pair(
    left: InventoryMarket,
    right: InventoryMarket,
    match: MarketMatchResult,
    *,
    decision: PaperScanDecision | None,
) -> tuple[InventoryComparisonStatus, str | None, list[str], bool]:
    left_market = left.canonical
    right_market = right.canonical
    if left_market is None or right_market is None:
        return (
            InventoryComparisonStatus.UNSUPPORTED_FAMILY,
            "unsupported_family",
            ["unsupported_family"],
            False,
        )
    if left_market.family is MarketFamily.UNKNOWN or right_market.family is MarketFamily.UNKNOWN:
        return (
            InventoryComparisonStatus.UNSUPPORTED_FAMILY,
            "unsupported_family",
            ["unsupported_family"],
            False,
        )
    if not solver_eligible_market(left_market) or not solver_eligible_market(right_market):
        return (
            InventoryComparisonStatus.UNSUPPORTED_OUTCOME_MODEL,
            "unsupported_outcome_model",
            ["unsupported_outcome_model", *match.reasons],
            False,
        )
    if not match.matched:
        if "settlement_mismatch" in match.reasons or "incomplete_settlement" in match.reasons:
            reason = next(
                item
                for item in match.reasons
                if item in {"settlement_mismatch", "incomplete_settlement"}
            )
            return InventoryComparisonStatus.SETTLEMENT_MISMATCH, reason, list(match.reasons), False
        return InventoryComparisonStatus.OTHER, match.reasons[0] if match.reasons else "not_equivalent", list(match.reasons), False

    entered = decision is not None and solver_eligible_pair(left_market, right_market, match)
    rejections = list(decision.rejection_reasons) if decision is not None else []
    status = InventoryComparisonStatus.MATCHED_EQUIVALENT
    reason: str | None = None
    if rejections:
        mapped = _status_from_rejections(rejections)
        if mapped is not InventoryComparisonStatus.MATCHED_EQUIVALENT:
            status = mapped
            reason = next(
                (item for item in rejections if _rejection_maps_to(item) is mapped),
                rejections[0],
            )
        else:
            reason = rejections[0]
    return status, reason, rejections, entered


def _status_from_rejections(rejections: list[str]) -> InventoryComparisonStatus:
    mapped = [_rejection_maps_to(item) for item in rejections]
    for status in (
        InventoryComparisonStatus.MISSING_COSTS,
        InventoryComparisonStatus.MISSING_FX,
        InventoryComparisonStatus.STALE,
        InventoryComparisonStatus.UNSUPPORTED_OUTCOME_MODEL,
        InventoryComparisonStatus.UNSUPPORTED_FAMILY,
        InventoryComparisonStatus.SETTLEMENT_MISMATCH,
    ):
        if status in mapped:
            return status
    return InventoryComparisonStatus.MATCHED_EQUIVALENT


def _rejection_maps_to(reason: str) -> InventoryComparisonStatus:
    if reason.startswith("missing_venue_cost") or reason in {"missing_costs", "legacy_fee_snapshot_not_cost_truth"}:
        return InventoryComparisonStatus.MISSING_COSTS
    if reason.startswith("missing_fx") or reason.startswith("missing_fx_rate"):
        return InventoryComparisonStatus.MISSING_FX
    if reason in {"stale_quote", "unknown_quote_age"} or reason.startswith("stale") or "quote_age" in reason:
        return InventoryComparisonStatus.STALE
    if reason in {"noncanonical_outcome_space", "unsupported_outcome_model"}:
        return InventoryComparisonStatus.UNSUPPORTED_OUTCOME_MODEL
    if reason in {"unsupported_family", "market_family_mismatch"} and reason == "unsupported_family":
        return InventoryComparisonStatus.UNSUPPORTED_FAMILY
    if reason in {"settlement_mismatch", "incomplete_settlement", "unknown_settlement_scope"}:
        return InventoryComparisonStatus.SETTLEMENT_MISMATCH
    return InventoryComparisonStatus.MATCHED_EQUIVALENT


def _facts_from_inventory(
    item: InventoryMarket,
    *,
    venue_costs: list[VenueCostSnapshot] | None,
    fx_snapshots: list[FxRateSnapshot] | None,
) -> VenueMarketFacts:
    canonical = item.canonical
    observation = item.observation
    fee_status, fee_source = _fee_status(item.venue, venue_costs)
    currency = observation.native_currency if observation is not None else None
    return VenueMarketFacts(
        venue=item.venue,
        source_event_id=item.source_event_id,
        source_market_id=item.source_market_id,
        family=canonical.family.value if canonical else None,
        period=canonical.period.value if canonical else None,
        line=canonical.line if canonical else None,
        settlement_key=canonical.settlement.deterministic_key() if canonical else None,
        settlement_complete=(
            canonical.settlement.is_economically_complete() if canonical else None
        ),
        best_backs=_best_backs(observation),
        usable_depth_at_touch=_touch_depth(observation),
        observed_at=observation.observed_at.isoformat() if observation is not None else None,
        quote_age_ms=observation.quote_age_ms if observation is not None else None,
        quote_age_basis=(
            str(observation.metadata.get("quote_age_basis"))
            if observation is not None and observation.metadata.get("quote_age_basis")
            else None
        ),
        native_currency=currency,
        fee_status=fee_status,
        fee_source=fee_source,
        fx_status=_fx_status(currency, fx_snapshots),
    )


def _fee_status(
    venue: VenueName,
    venue_costs: list[VenueCostSnapshot] | None,
) -> tuple[str | None, str | None]:
    if not venue_costs:
        return "missing", "missing_venue_cost"
    for snapshot in venue_costs:
        if snapshot.venue is venue:
            if snapshot.is_economically_known():
                return "known", snapshot.source
            return "unknown", snapshot.source
    return "missing", f"missing_venue_cost:{venue.value}"


FUNCTIONAL_CURRENCY = "GBP"
FX_STATUS_NOT_REQUIRED = "not_required"
FX_STATUS_KNOWN = "known"
FX_STATUS_MISSING = "missing"


def _fx_status(currency: str | None, fx_snapshots: list[FxRateSnapshot] | None) -> str | None:
    """GBP is the functional currency: conversion is not required.

    Non-GBP natives remain fail-closed when a rate snapshot is absent.
    """

    if not currency:
        return None
    if currency.upper() == FUNCTIONAL_CURRENCY:
        return FX_STATUS_NOT_REQUIRED
    if not fx_snapshots:
        return FX_STATUS_MISSING
    if any(snapshot.currency.upper() == currency.upper() for snapshot in fx_snapshots):
        return FX_STATUS_KNOWN
    return FX_STATUS_MISSING


def _best_backs(observation: VenueMarketObservation | None) -> list[VenueQuoteFact]:
    if observation is None:
        return []
    facts: list[VenueQuoteFact] = []
    for book in observation.outcome_books:
        back = book.best_back
        facts.append(
            VenueQuoteFact(
                outcome=book.outcome.value,
                decimal_odds=back.decimal_odds if back else None,
                size_at_touch=back.available_stake if back else None,
            )
        )
    return facts


def _touch_depth(observation: VenueMarketObservation | None) -> Decimal | None:
    if observation is None:
        return None
    sizes = [
        book.best_back.available_stake
        for book in observation.outcome_books
        if book.best_back is not None
    ]
    if not sizes:
        return None
    return min(sizes)


def _decision_net_edge(decision: PaperScanDecision | None) -> Decimal | None:
    if decision is None or decision.depth_scan is None:
        return None
    implied = decision.depth_scan.solution.implied_probability_sum
    if implied <= 0:
        return None
    from sports_hedge.arbitrage.watchlist.economics import net_edge_from_implied_sum

    return net_edge_from_implied_sum(implied)


def _decision_distance(decision: PaperScanDecision | None) -> Decimal | None:
    current = _decision_net_edge(decision)
    if current is None or decision is None:
        return None
    from sports_hedge.arbitrage.watchlist.economics import distance_to_trigger_pp

    return distance_to_trigger_pp(current, decision.minimum_net_edge)


def _decision_is_arb(decision: PaperScanDecision | None) -> bool:
    if decision is None or decision.depth_scan is None:
        return False
    return bool(
        decision.eligible_for_paper_simulation and decision.depth_scan.solution.is_arbitrage
    )


def _sort_rows(rows: list[FixtureMarketInventoryRow]) -> list[FixtureMarketInventoryRow]:
    order = {status: index for index, status in enumerate(InventoryComparisonStatus)}
    return sorted(
        rows,
        key=lambda row: (
            order.get(row.comparison_status, 99),
            row.family or "",
            str(row.line or ""),
            row.display_name,
        ),
    )


def raw_market_name(payload: dict[str, Any], venue: VenueName) -> str:
    if venue is VenueName.MATCHBOOK:
        return str(payload.get("name") or payload.get("id") or "Matchbook market")
    return str(
        payload.get("question")
        or payload.get("title")
        or payload.get("id")
        or payload.get("conditionId")
        or "Polymarket market"
    )


def raw_market_id(payload: dict[str, Any], venue: VenueName) -> str:
    if venue is VenueName.MATCHBOOK:
        return str(payload.get("id") or "")
    return str(payload.get("id") or payload.get("conditionId") or payload.get("condition_id") or "")
