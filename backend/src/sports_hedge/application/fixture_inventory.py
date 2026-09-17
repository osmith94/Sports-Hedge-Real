from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from sports_hedge.application.complete_set import (
    INCOMPLETE_OUTCOME_REASON,
    PUSH_STATE_REASON,
    SOLVER_INELIGIBLE_REASON,
    SPLIT_LINE_REASON,
    UNKNOWN_DRAW_VOID_REASON,
    UNSUPPORTED_STATE_PAYOFF_FEE_BASIS,
    UNPROVEN_HANDICAP_REASON,
    UNPROVEN_SETTLEMENT_REASON,
    scan_eligible_pair,
    scan_ineligibility_reason,
    solver_eligible_market,
    solver_model_for_pair,
    generalized_payoff_eligible_market,
)
from sports_hedge.application.market_observation import VenueMarketObservation
from sports_hedge.domain.football import (
    CanonicalMarket,
    FootballPeriod,
    MarketFamily,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import MarketAction, VenueCostSnapshot
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.fees.labels import operator_fee_label
from sports_hedge.fees.polymarket import polymarket_cost_from_market
from sports_hedge.fees.resolver import MATCHBOOK_OVERRIDE_TIER, UnknownRequiredCostError, VenueCostResolver
from sports_hedge.matching.markets import MarketMatchResult, MarketMatcher
from sports_hedge.normalization.venues import matchbook_raw_market_type
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
    fee_label: str | None = None
    fee_basis: str | None = None
    fee_rate: Decimal | None = None
    fee_formula_name: str | None = None
    fee_account_assumption: bool = False
    fx_status: str | None = None
    raw_market_name: str | None = None
    raw_market_type: str | None = None
    raw_runner_labels: list[str] = Field(default_factory=list)


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
    solver_model: str | None = None
    current_net_edge: Decimal | None = None
    trigger_net_edge: Decimal | None = None
    distance_to_trigger_pp: Decimal | None = None
    solver_is_arbitrage: bool = False
    matchbook: VenueMarketFacts | None = None
    polymarket: VenueMarketFacts | None = None
    kalshi: VenueMarketFacts | None = None
    pair_results: list["InventoryPairResult"] = Field(default_factory=list)
    scan_lane: str | None = None
    last_scanned_at: datetime | None = None
    radar_freshness: str | None = None


class InventoryPairResult(BaseModel):
    left_venue: VenueName
    right_venue: VenueName
    entered_solver: bool = False
    solver_model: str | None = None
    current_net_edge: Decimal | None = None
    rejection_reasons: list[str] = Field(default_factory=list)
    solver_is_arbitrage: bool = False


class InventoryMarket(BaseModel):
    venue: VenueName
    source_event_id: str
    source_market_id: str
    raw_name: str
    raw_market_type: str | None = None
    raw_runner_labels: list[str] = Field(default_factory=list)
    canonical: CanonicalMarket | None = None
    observation: VenueMarketObservation | None = None
    normalize_error: str | None = None


def solver_eligible_pair(left: CanonicalMarket, right: CanonicalMarket, match: MarketMatchResult) -> bool:
    """Complete-set eligibility only. Use scan_eligible_pair for live scan routing."""

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
    kalshi_markets: list[InventoryMarket] | None = None,
    matcher: MarketMatcher | None = None,
    decisions_by_source_ids: dict[tuple[str, str], PaperScanDecision] | None = None,
    decisions_by_pair: dict[tuple[str, str, str, str], PaperScanDecision] | None = None,
    venue_costs: list[VenueCostSnapshot] | None = None,
    fx_snapshots: list[FxRateSnapshot] | None = None,
    cost_resolver: VenueCostResolver | None = None,
) -> list[FixtureMarketInventoryRow]:
    """Group every discovered market. Never drops unsupported or rejected rows."""

    matcher = matcher or MarketMatcher()
    decisions = decisions_by_source_ids or {}
    pair_decisions = decisions_by_pair or {}
    kalshi_markets = kalshi_markets or []
    rows: list[FixtureMarketInventoryRow] = []

    unmatched_left: list[InventoryMarket] = []
    unmatched_right: list[InventoryMarket] = []
    for item in matchbook_markets:
        if item.canonical is None:
            rows.append(
                _unnormalized_row(
                    item,
                    venue_costs=venue_costs,
                    fx_snapshots=fx_snapshots,
                    cost_resolver=cost_resolver,
                )
            )
        else:
            unmatched_left.append(item)
    for item in polymarket_markets:
        if item.canonical is None:
            rows.append(
                _unnormalized_row(
                    item,
                    venue_costs=venue_costs,
                    fx_snapshots=fx_snapshots,
                    cost_resolver=cost_resolver,
                )
            )
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
                cost_resolver=cost_resolver,
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
                    cost_resolver=cost_resolver,
                )
            )
            break

    for index, left in enumerate(leftover_left):
        if index not in grouped_left:
            rows.append(
                _venue_only_row(
                    left,
                    venue_costs=venue_costs,
                    fx_snapshots=fx_snapshots,
                    cost_resolver=cost_resolver,
                )
            )
    for index, right in enumerate(leftover_right):
        if index not in grouped_right:
            rows.append(
                _venue_only_row(
                    right,
                    venue_costs=venue_costs,
                    fx_snapshots=fx_snapshots,
                    cost_resolver=cost_resolver,
                )
            )

    unmatched_kalshi: list[InventoryMarket] = []
    for item in kalshi_markets:
        if item.canonical is None:
            rows.append(
                _unnormalized_row(
                    item,
                    venue_costs=venue_costs,
                    fx_snapshots=fx_snapshots,
                    cost_resolver=cost_resolver,
                )
            )
        else:
            unmatched_kalshi.append(item)
    attached: set[int] = set()
    for row in rows:
        kalshi_index = _matching_kalshi_index(
            row,
            unmatched_kalshi,
            matcher,
            matchbook_markets=matchbook_markets,
            polymarket_markets=polymarket_markets,
            exclude=attached,
        )
        if kalshi_index is None:
            continue
        attached.add(kalshi_index)
        kalshi_item = unmatched_kalshi[kalshi_index]
        _attach_kalshi(
            row,
            kalshi_item,
            matcher=matcher,
            matchbook_markets=matchbook_markets,
            polymarket_markets=polymarket_markets,
            pair_decisions=pair_decisions,
            venue_costs=venue_costs,
            fx_snapshots=fx_snapshots,
            cost_resolver=cost_resolver,
        )
    leftover_kalshi = [
        item for index, item in enumerate(unmatched_kalshi) if index not in attached
    ]
    leftover_pm_only = [
        row for row in rows if row.polymarket is not None and row.matchbook is None and row.kalshi is None
    ]
    used_pm_rows: set[int] = set()
    used_kalshi: set[int] = set()
    for kalshi_index, kalshi_item in enumerate(leftover_kalshi):
        for row_index, row in enumerate(leftover_pm_only):
            if row_index in used_pm_rows:
                continue
            pm_item = _inventory_from_facts(row.polymarket, VenueName.POLYMARKET, polymarket_markets)
            if pm_item is None or pm_item.canonical is None or kalshi_item.canonical is None:
                continue
            match = matcher.match(pm_item.canonical, kalshi_item.canonical)
            if not match.matched:
                continue
            used_pm_rows.add(row_index)
            used_kalshi.add(kalshi_index)
            decision = pair_decisions.get(
                (
                    VenueName.POLYMARKET.value,
                    pm_item.source_market_id,
                    VenueName.KALSHI.value,
                    kalshi_item.source_market_id,
                )
            ) or decisions.get((pm_item.source_market_id, kalshi_item.source_market_id))
            replacement = _paired_row(
                pm_item,
                kalshi_item,
                match,
                decision=decision,
                venue_costs=venue_costs,
                fx_snapshots=fx_snapshots,
                cost_resolver=cost_resolver,
            )
            replacement.polymarket = row.polymarket
            replacement.kalshi = _facts_from_inventory(
                kalshi_item,
                venue_costs=venue_costs,
                fx_snapshots=fx_snapshots,
                cost_resolver=cost_resolver,
            )
            replacement.matchbook = None
            rows[rows.index(row)] = replacement
            break
    for index, item in enumerate(leftover_kalshi):
        if index not in used_kalshi and index not in attached:
            rows.append(
                _venue_only_row(
                    item,
                    venue_costs=venue_costs,
                    fx_snapshots=fx_snapshots,
                    cost_resolver=cost_resolver,
                )
            )

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
    cost_resolver: VenueCostResolver | None,
) -> FixtureMarketInventoryRow:
    facts = _facts_from_inventory(
        item,
        venue_costs=venue_costs,
        fx_snapshots=fx_snapshots,
        cost_resolver=cost_resolver,
    )
    return FixtureMarketInventoryRow(
        display_name=item.raw_name or item.source_market_id,
        family=None,
        comparison_status=InventoryComparisonStatus.UNSUPPORTED_FAMILY,
        reason=item.normalize_error or "unsupported_family",
        rejection_reasons=[item.normalize_error] if item.normalize_error else ["unsupported_family"],
        matchbook=facts if item.venue is VenueName.MATCHBOOK else None,
        polymarket=facts if item.venue is VenueName.POLYMARKET else None,
        kalshi=facts if item.venue is VenueName.KALSHI else None,
    )


def _venue_only_row(
    item: InventoryMarket,
    *,
    venue_costs: list[VenueCostSnapshot] | None,
    fx_snapshots: list[FxRateSnapshot] | None,
    cost_resolver: VenueCostResolver | None,
) -> FixtureMarketInventoryRow:
    canonical = item.canonical
    status = InventoryComparisonStatus.VENUE_ONLY
    reason = "venue_only"
    reasons = ["venue_only"]
    if canonical is None:
        return _unnormalized_row(
            item,
            venue_costs=venue_costs,
            fx_snapshots=fx_snapshots,
            cost_resolver=cost_resolver,
        )
    if canonical.family is MarketFamily.UNKNOWN:
        status = InventoryComparisonStatus.UNSUPPORTED_FAMILY
        reason = "unsupported_family"
        reasons = ["unsupported_family"]
    elif not solver_eligible_market(canonical) and not generalized_payoff_eligible_market(canonical):
        status = InventoryComparisonStatus.UNSUPPORTED_OUTCOME_MODEL
        reason = scan_ineligibility_reason(canonical)
        reasons = [reason]
    facts = _facts_from_inventory(
        item,
        venue_costs=venue_costs,
        fx_snapshots=fx_snapshots,
        cost_resolver=cost_resolver,
    )
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
        kalshi=facts if item.venue is VenueName.KALSHI else None,
    )


def _paired_row(
    left: InventoryMarket,
    right: InventoryMarket,
    match: MarketMatchResult,
    *,
    decision: PaperScanDecision | None,
    venue_costs: list[VenueCostSnapshot] | None,
    fx_snapshots: list[FxRateSnapshot] | None,
    cost_resolver: VenueCostResolver | None,
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

    def facts_for(item: InventoryMarket) -> VenueMarketFacts:
        return _facts_from_inventory(
            item,
            venue_costs=venue_costs,
            fx_snapshots=fx_snapshots,
            cost_resolver=cost_resolver,
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
        solver_model=solver_model_for_pair(left.canonical, right.canonical)
        if left.canonical is not None and right.canonical is not None and entered
        else (
            decision.solver_model
            if decision is not None and decision.solver_model
            else None
        ),
        current_net_edge=_decision_net_edge(decision) if entered else None,
        trigger_net_edge=decision.minimum_net_edge if decision is not None and entered else None,
        distance_to_trigger_pp=_decision_distance(decision) if entered else None,
        solver_is_arbitrage=_decision_is_arb(decision) if entered else False,
        matchbook=facts_for(left) if left.venue is VenueName.MATCHBOOK else (
            facts_for(right) if right.venue is VenueName.MATCHBOOK else None
        ),
        polymarket=facts_for(left) if left.venue is VenueName.POLYMARKET else (
            facts_for(right) if right.venue is VenueName.POLYMARKET else None
        ),
        kalshi=facts_for(left) if left.venue is VenueName.KALSHI else (
            facts_for(right) if right.venue is VenueName.KALSHI else None
        ),
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
    if not match.matched:
        if "settlement_mismatch" in match.reasons or "incomplete_settlement" in match.reasons:
            reason = next(
                item
                for item in match.reasons
                if item in {"settlement_mismatch", "incomplete_settlement"}
            )
            return InventoryComparisonStatus.SETTLEMENT_MISMATCH, reason, list(match.reasons), False
        left_scan_blocked = not solver_eligible_market(left_market) and not generalized_payoff_eligible_market(
            left_market
        )
        right_scan_blocked = not solver_eligible_market(right_market) and not generalized_payoff_eligible_market(
            right_market
        )
        if left_scan_blocked or right_scan_blocked:
            ineligible = scan_ineligibility_reason(left_market if left_scan_blocked else right_market)
            return (
                InventoryComparisonStatus.UNSUPPORTED_OUTCOME_MODEL,
                ineligible,
                [ineligible, *match.reasons],
                False,
            )
        return InventoryComparisonStatus.OTHER, match.reasons[0] if match.reasons else "not_equivalent", list(match.reasons), False
    if not scan_eligible_pair(left_market, right_market, match):
        from sports_hedge.catalogue.admission import assess_catalogue_admission

        admission = assess_catalogue_admission(left_market, right_market)
        if not admission.allowed:
            reason = admission.rejection_reason or "catalogue_review_required"
            return (
                InventoryComparisonStatus.OTHER,
                reason,
                [reason, admission.assessment.reason, *match.reasons],
                False,
            )
        ineligible = (
            scan_ineligibility_reason(left_market)
            if not solver_eligible_market(left_market) and not generalized_payoff_eligible_market(left_market)
            else scan_ineligibility_reason(right_market)
        )
        return (
            InventoryComparisonStatus.UNSUPPORTED_OUTCOME_MODEL,
            ineligible,
            [ineligible, *match.reasons],
            False,
        )

    entered = decision is not None and scan_eligible_pair(left_market, right_market, match)
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
    if reason.startswith("catalogue_"):
        return InventoryComparisonStatus.OTHER
    if reason.startswith("missing_venue_cost") or reason in {
        "missing_costs",
        "legacy_fee_snapshot_not_cost_truth",
        UNSUPPORTED_STATE_PAYOFF_FEE_BASIS,
    }:
        return InventoryComparisonStatus.MISSING_COSTS
    if reason.startswith("missing_fx") or reason.startswith("missing_fx_rate"):
        return InventoryComparisonStatus.MISSING_FX
    if reason in {"stale_quote", "unknown_quote_age"} or reason.startswith("stale") or "quote_age" in reason:
        return InventoryComparisonStatus.STALE
    if reason in {
        "noncanonical_outcome_space",
        "unsupported_outcome_model",
        INCOMPLETE_OUTCOME_REASON,
        SOLVER_INELIGIBLE_REASON,
        PUSH_STATE_REASON,
        UNPROVEN_SETTLEMENT_REASON,
        UNPROVEN_HANDICAP_REASON,
        SPLIT_LINE_REASON,
        UNKNOWN_DRAW_VOID_REASON,
    }:
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
    cost_resolver: VenueCostResolver | None,
    decision: PaperScanDecision | None = None,
) -> VenueMarketFacts:
    canonical = item.canonical
    observation = item.observation
    fee = _inventory_fee_fields(item, venue_costs, cost_resolver=cost_resolver)
    currency = observation.native_currency if observation is not None else None
    fx_truth = decision.fx_snapshots if decision is not None else fx_snapshots
    metadata = observation.metadata if observation is not None else {}
    raw_name = item.raw_name or _metadata_str(metadata, "raw_market_name")
    raw_type = item.raw_market_type or _metadata_str(metadata, "raw_market_type")
    runner_labels = list(item.raw_runner_labels)
    if not runner_labels:
        labels = metadata.get("raw_runner_labels") if isinstance(metadata, dict) else None
        if isinstance(labels, list):
            runner_labels = [str(label) for label in labels if str(label).strip()]
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
        fx_status=_fx_status(currency, fx_truth),
        raw_market_name=raw_name or None,
        raw_market_type=raw_type,
        raw_runner_labels=runner_labels,
        **fee,
    )


def _metadata_str(metadata: Any, key: str) -> str | None:
    if not isinstance(metadata, dict):
        return None
    value = metadata.get(key)
    if value is None or str(value).strip() == "":
        return None
    return str(value).strip()


def _inventory_fee_fields(
    item: InventoryMarket,
    venue_costs: list[VenueCostSnapshot] | None,
    *,
    cost_resolver: VenueCostResolver | None,
) -> dict[str, Any]:
    status, source, snapshot = _fee_status(item, venue_costs, cost_resolver=cost_resolver)
    rate = None
    if snapshot is not None:
        rate = snapshot.rate
        if rate is None:
            rate = snapshot.formula_parameters.get("rate")
    return {
        "fee_status": status,
        "fee_source": source,
        "fee_label": operator_fee_label(snapshot, status=status),
        "fee_basis": snapshot.fee_basis.value if snapshot is not None else None,
        "fee_rate": rate,
        "fee_formula_name": snapshot.formula_name if snapshot is not None else None,
        "fee_account_assumption": bool(
            snapshot is not None and snapshot.account_or_fee_tier == MATCHBOOK_OVERRIDE_TIER
        ),
    }


def _fee_status(
    item: InventoryMarket,
    venue_costs: list[VenueCostSnapshot] | None,
    *,
    cost_resolver: VenueCostResolver | None,
) -> tuple[str | None, str | None, VenueCostSnapshot | None]:
    """Mirror paper-scan cost truth: explicit snapshots win; otherwise resolve.

    Explicit ``venue_costs`` are fail-closed for absent venues, matching
    ``PaperScanService._resolve_costs``. When the scan path would auto-resolve
    from Matchbook registry, Kalshi metadata, or Polymarket per-market CLOB
    metadata, inventory must show the same known/unknown status rather than a
    false ``fee missing``.
    """

    if venue_costs is not None:
        for snapshot in venue_costs:
            if snapshot.venue is item.venue:
                if snapshot.is_economically_known():
                    return "known", snapshot.source, snapshot
                return "unknown", snapshot.source, snapshot
        return "missing", f"missing_venue_cost:{item.venue.value}", None

    snapshot, source = _resolve_inventory_cost(item, cost_resolver=cost_resolver)
    if snapshot is not None:
        if snapshot.is_economically_known():
            return "known", snapshot.source, snapshot
        return "unknown", snapshot.source, snapshot
    return "missing", source or f"missing_venue_cost:{item.venue.value}", None


def _resolve_inventory_cost(
    item: InventoryMarket,
    *,
    cost_resolver: VenueCostResolver | None,
) -> tuple[VenueCostSnapshot | None, str | None]:
    observation = item.observation
    as_of = observation.observed_at if observation is not None else datetime.now(UTC)
    if item.venue is VenueName.KALSHI:
        metadata = observation.metadata if observation is not None else {}
        fee_meta = metadata.get("kalshi_fee") if isinstance(metadata, dict) else None
        if isinstance(fee_meta, dict):
            snapshot = kalshi_cost_from_series(
                fee_meta,
                captured_at=as_of,
                source_market_id=item.source_market_id,
            )
            return snapshot, snapshot.source
        return None, "unknown_required_venue_cost:kalshi"
    if item.venue is VenueName.POLYMARKET:
        metadata = observation.metadata if observation is not None else {}
        fee_meta = metadata.get("polymarket_fee") if isinstance(metadata, dict) else None
        if isinstance(fee_meta, dict):
            snapshot = polymarket_cost_from_market(
                fee_meta,
                captured_at=as_of,
                source_market_id=item.source_market_id,
            )
            return snapshot, snapshot.source
        return None, "unknown_required_venue_cost:polymarket"
    if cost_resolver is None:
        return None, "missing_venue_cost"
    family = item.canonical.family if item.canonical is not None else None
    if family is None or family is MarketFamily.UNKNOWN:
        return None, f"unknown_required_venue_cost:{item.venue.value}"
    action = (
        MarketAction.BUY
        if item.venue in {VenueName.POLYMARKET, VenueName.KALSHI}
        else MarketAction.BACK
    )
    try:
        snapshot = cost_resolver.resolve(
            venue=item.venue,
            market_class=family,
            action=action,
            as_of=as_of,
        )
    except UnknownRequiredCostError as exc:
        return None, exc.reason
    return snapshot, snapshot.source


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
    if decision is None:
        return None
    if decision.payoff_scan is not None:
        from sports_hedge.arbitrage.watchlist.economics import quantized_edge

        return quantized_edge(decision.payoff_scan.solution.roi)
    if decision.depth_scan is None:
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
    if decision is None or not decision.eligible_for_paper_simulation:
        return False
    if decision.payoff_scan is not None:
        return bool(decision.payoff_scan.solution.is_arbitrage)
    if decision.depth_scan is None:
        return False
    return bool(decision.depth_scan.solution.is_arbitrage)


def sort_fixture_inventory_rows(rows: list[FixtureMarketInventoryRow]) -> list[FixtureMarketInventoryRow]:
    return _sort_rows(rows)


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


def _kalshi_catalogue_admission(
    row: FixtureMarketInventoryRow,
    kalshi_item: InventoryMarket,
    *,
    matchbook_markets: list[InventoryMarket],
    polymarket_markets: list[InventoryMarket],
):
    if kalshi_item.canonical is None:
        return None
    from sports_hedge.catalogue.admission import assess_catalogue_admission

    for canonical in _row_canonicals(
        row,
        matchbook_markets=matchbook_markets,
        polymarket_markets=polymarket_markets,
    ):
        return assess_catalogue_admission(canonical, kalshi_item.canonical)
    return None


def _row_canonicals(
    row: FixtureMarketInventoryRow,
    *,
    matchbook_markets: list[InventoryMarket],
    polymarket_markets: list[InventoryMarket],
) -> list[CanonicalMarket]:
    canonicals: list[CanonicalMarket] = []
    for facts, venue, source in (
        (row.matchbook, VenueName.MATCHBOOK, matchbook_markets),
        (row.polymarket, VenueName.POLYMARKET, polymarket_markets),
    ):
        item = _inventory_from_facts(facts, venue, source)
        if item is not None and item.canonical is not None:
            canonicals.append(item.canonical)
    return canonicals


def _kalshi_related_to_row(row: FixtureMarketInventoryRow, item: InventoryMarket) -> bool:
    if row.family is None or item.canonical is None:
        return False
    if item.canonical.family.value != row.family:
        return False
    if row.period and item.canonical.period.value != row.period:
        return False
    if row.line is not None and item.canonical.line != row.line:
        return False
    if row.matchbook is None and row.polymarket is None:
        return False
    settlement_keys = {
        facts.settlement_key
        for facts in (row.matchbook, row.polymarket)
        if facts is not None and facts.settlement_key
    }
    kalshi_key = item.canonical.settlement.deterministic_key()
    if settlement_keys and kalshi_key not in settlement_keys:
        if item.canonical.family is MarketFamily.MATCH_RESULT:
            return True
        return False
    return True


def _kalshi_match_results(
    row: FixtureMarketInventoryRow,
    kalshi_item: InventoryMarket,
    matcher: MarketMatcher,
    *,
    matchbook_markets: list[InventoryMarket],
    polymarket_markets: list[InventoryMarket],
) -> list[MarketMatchResult]:
    if kalshi_item.canonical is None:
        return []
    return [
        matcher.match(canonical, kalshi_item.canonical)
        for canonical in _row_canonicals(
            row,
            matchbook_markets=matchbook_markets,
            polymarket_markets=polymarket_markets,
        )
    ]


def _matching_kalshi_index(
    row: FixtureMarketInventoryRow,
    kalshi_markets: list[InventoryMarket],
    matcher: MarketMatcher,
    *,
    matchbook_markets: list[InventoryMarket],
    polymarket_markets: list[InventoryMarket],
    exclude: set[int] | None = None,
) -> int | None:
    if row.family is None:
        return None
    skipped = exclude or set()
    related: list[tuple[bool, int]] = []
    for index, item in enumerate(kalshi_markets):
        if index in skipped:
            continue
        if not _kalshi_related_to_row(row, item):
            continue
        matches = _kalshi_match_results(
            row,
            item,
            matcher,
            matchbook_markets=matchbook_markets,
            polymarket_markets=polymarket_markets,
        )
        equivalent = any(match.matched for match in matches)
        if not equivalent and row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT:
            continue
        related.append((equivalent, index))
    if not related:
        return None
    for equivalent, index in related:
        if equivalent:
            return index
    return related[0][1]


def _clear_stale_venue_only(row: FixtureMarketInventoryRow) -> None:
    if row.reason == "venue_only":
        row.reason = None
    if "venue_only" in row.rejection_reasons:
        row.rejection_reasons = [item for item in row.rejection_reasons if item != "venue_only"]
    if "venue_only" in row.match_reasons:
        row.match_reasons = [item for item in row.match_reasons if item != "venue_only"]


def _apply_decision_fx(row: FixtureMarketInventoryRow, decision: PaperScanDecision) -> None:
    for facts in (row.matchbook, row.polymarket, row.kalshi):
        if facts is None:
            continue
        facts.fx_status = _fx_status(facts.native_currency, decision.fx_snapshots)


def _attach_kalshi(
    row: FixtureMarketInventoryRow,
    kalshi_item: InventoryMarket,
    *,
    matcher: MarketMatcher,
    matchbook_markets: list[InventoryMarket],
    polymarket_markets: list[InventoryMarket],
    pair_decisions: dict[tuple[str, str, str, str], PaperScanDecision],
    venue_costs: list[VenueCostSnapshot] | None,
    fx_snapshots: list[FxRateSnapshot] | None,
    cost_resolver: VenueCostResolver | None,
) -> None:
    pair_summaries: list[InventoryPairResult] = []
    best_decision: PaperScanDecision | None = None
    for facts, venue in (
        (row.matchbook, VenueName.MATCHBOOK),
        (row.polymarket, VenueName.POLYMARKET),
    ):
        if facts is None:
            continue
        decision = pair_decisions.get(
            (venue.value, facts.source_market_id, VenueName.KALSHI.value, kalshi_item.source_market_id)
        )
        if decision is None:
            continue
        entered = bool(decision.eligible_for_paper_simulation or decision.solver_model)
        if decision.solver_model and "market_not_equivalent" not in decision.rejection_reasons:
            entered = True
        pair_summaries.append(
            InventoryPairResult(
                left_venue=venue,
                right_venue=VenueName.KALSHI,
                entered_solver=entered and bool(decision.solver_model),
                solver_model=decision.solver_model,
                current_net_edge=_decision_net_edge(decision),
                rejection_reasons=list(decision.rejection_reasons),
                solver_is_arbitrage=_decision_is_arb(decision),
            )
        )
        if best_decision is None:
            best_decision = decision
        elif _decision_net_edge(decision) is not None and (
            _decision_net_edge(best_decision) is None
            or (_decision_net_edge(decision) or Decimal("-1"))
            > (_decision_net_edge(best_decision) or Decimal("-1"))
        ):
            best_decision = decision
    matches = _kalshi_match_results(
        row,
        kalshi_item,
        matcher,
        matchbook_markets=matchbook_markets,
        polymarket_markets=polymarket_markets,
    )
    proven = any(match.matched for match in matches)
    row.kalshi = _facts_from_inventory(
        kalshi_item,
        venue_costs=venue_costs,
        fx_snapshots=fx_snapshots,
        cost_resolver=cost_resolver,
        decision=best_decision,
    )
    row.pair_results = pair_summaries
    if best_decision is not None:
        _apply_decision_fx(row, best_decision)
    if proven:
        for match in matches:
            if not match.matched:
                continue
            for reason in match.reasons:
                if reason not in row.match_reasons:
                    row.match_reasons.append(reason)
    if not proven:
        mismatch_reasons = [reason for match in matches for reason in match.reasons]
        for reason in mismatch_reasons:
            if reason not in row.rejection_reasons:
                row.rejection_reasons.append(reason)
            if reason not in row.match_reasons:
                row.match_reasons.append(reason)
        if mismatch_reasons:
            if row.comparison_status is InventoryComparisonStatus.VENUE_ONLY:
                row.comparison_status = InventoryComparisonStatus.OTHER
            if row.reason in {None, "venue_only"}:
                row.reason = (
                    "outcome_space_mismatch"
                    if "outcome_space_mismatch" in mismatch_reasons
                    else mismatch_reasons[0]
                )
        return
    _clear_stale_venue_only(row)
    catalogue = _kalshi_catalogue_admission(
        row,
        kalshi_item,
        matchbook_markets=matchbook_markets,
        polymarket_markets=polymarket_markets,
    )
    if catalogue is not None and not catalogue.allowed:
        reason = catalogue.rejection_reason or "catalogue_review_required"
        row.comparison_status = InventoryComparisonStatus.OTHER
        row.reason = reason
        if reason not in row.rejection_reasons:
            row.rejection_reasons.append(reason)
        detail = catalogue.assessment.reason
        if detail and detail not in row.rejection_reasons:
            row.rejection_reasons.append(detail)
        row.entered_solver = False
        row.solver_model = None
        row.current_net_edge = None
        row.solver_is_arbitrage = False
        return
    if pair_summaries:
        best = max(
            pair_summaries,
            key=lambda item: item.current_net_edge or Decimal("-1"),
        )
        if best.entered_solver and not row.entered_solver:
            row.entered_solver = True
            row.solver_model = best.solver_model
            row.current_net_edge = best.current_net_edge
            row.solver_is_arbitrage = best.solver_is_arbitrage
        if best.entered_solver or not best.rejection_reasons:
            row.comparison_status = InventoryComparisonStatus.MATCHED_EQUIVALENT
            if row.reason == "venue_only":
                row.reason = None
        return
    row.comparison_status = InventoryComparisonStatus.MATCHED_EQUIVALENT
    if row.reason == "venue_only":
        row.reason = None


def _inventory_from_facts(
    facts: VenueMarketFacts | None,
    venue: VenueName,
    source: list[InventoryMarket],
) -> InventoryMarket | None:
    if facts is None:
        return None
    for item in source:
        if item.source_market_id == facts.source_market_id and item.venue is venue:
            return item
    return None


def raw_market_name(payload: dict[str, Any], venue: VenueName) -> str:
    if venue is VenueName.MATCHBOOK:
        return str(payload.get("name") or payload.get("id") or "Matchbook market")
    if venue is VenueName.KALSHI:
        return str(
            payload.get("title")
            or payload.get("yes_sub_title")
            or payload.get("ticker")
            or "Kalshi market"
        )
    return str(
        payload.get("question")
        or payload.get("title")
        or payload.get("id")
        or payload.get("conditionId")
        or "Polymarket market"
    )


def raw_market_type(payload: dict[str, Any], venue: VenueName) -> str | None:
    if venue is VenueName.MATCHBOOK:
        return matchbook_raw_market_type(payload)
    if venue is VenueName.KALSHI:
        value = payload.get("market_type") or payload.get("type")
        return str(value).strip() if value else None
    value = payload.get("sportsMarketType") or payload.get("sports_market_type") or payload.get("marketType")
    return str(value).strip() if value else None


def raw_runner_labels(payload: dict[str, Any], venue: VenueName) -> list[str]:
    labels: list[str] = []
    if venue is VenueName.KALSHI:
        for key in ("yes_sub_title", "yes_subtitle", "no_sub_title", "title"):
            value = payload.get(key)
            if value and str(value).strip() and str(value).strip() not in labels:
                labels.append(str(value).strip())
        return labels
    runners = payload.get("runners")
    if isinstance(runners, list):
        for runner in runners:
            if isinstance(runner, dict):
                name = str(runner.get("name") or runner.get("label") or "").strip()
                if name:
                    labels.append(name)
    if labels:
        return labels
    outcomes = payload.get("outcomes")
    if isinstance(outcomes, list):
        return [str(item).strip() for item in outcomes if str(item).strip()]
    return labels


def raw_market_id(payload: dict[str, Any], venue: VenueName) -> str:
    if venue is VenueName.MATCHBOOK:
        return str(payload.get("id") or "")
    if venue is VenueName.KALSHI:
        return str(payload.get("ticker") or payload.get("event_ticker") or payload.get("id") or "")
    return str(payload.get("id") or payload.get("conditionId") or payload.get("condition_id") or "")
