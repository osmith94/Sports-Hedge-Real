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
from sports_hedge.matching.bulk_market_pairs import greedy_unique_market_matches
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.markets import (
    MarketMatcher,
    MarketMatchResult,
    economic_match_fingerprint,
    memoize_market_matches,
)
from sports_hedge.normalization.venues import matchbook_raw_market_type
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision


def _load_attachment_dependencies():
    """Import Kalshi-attachment modules before the first universe slice.

    Catalogue admission and the sport registers import themselves on first
    use. Doing that inside ``assemble_fixture_inventory`` puts the import on
    the event-loop slice for the first fixture. Loading them with this
    module keeps the same work at process import.
    """

    from sports_hedge.catalogue.admission import assess_catalogue_admission
    from sports_hedge.catalogue.registry import target_market_families
    from sports_hedge.mlb.register import mlb_canonical_key_for_market
    from sports_hedge.mlb.settlement import mlb_pair_non_executable_reason
    from sports_hedge.nba.detect import NBA_MARKET_FAMILIES
    from sports_hedge.nba.register import nba_canonical_key_for_market
    from sports_hedge.nba.settlement import nba_market_uses_paper_caveat
    from sports_hedge.ncaab.detect import NCAAB_MARKET_FAMILIES
    from sports_hedge.ncaab.register import ncaab_canonical_key_for_market
    from sports_hedge.nfl.detect import NFL_MARKET_FAMILIES
    from sports_hedge.nfl.register import nfl_canonical_key_for_market
    from sports_hedge.nfl.settlement import nfl_market_uses_paper_caveat
    from sports_hedge.tennis.register import tennis_registered_canonical_key
    from sports_hedge.tennis.settlement import tennis_executable_block_reason

    return (
        assess_catalogue_admission,
        target_market_families,
        mlb_canonical_key_for_market,
        mlb_pair_non_executable_reason,
        NBA_MARKET_FAMILIES,
        nba_canonical_key_for_market,
        nba_market_uses_paper_caveat,
        NCAAB_MARKET_FAMILIES,
        ncaab_canonical_key_for_market,
        NFL_MARKET_FAMILIES,
        nfl_canonical_key_for_market,
        nfl_market_uses_paper_caveat,
        tennis_registered_canonical_key,
        tennis_executable_block_reason,
    )


(
    assess_catalogue_admission,
    *_ATTACHMENT_DEPENDENCIES,
) = _load_attachment_dependencies()


class InventoryComparisonStatus(StrEnum):
    MATCHED_EQUIVALENT = "matched_equivalent"
    PAPER_ASSUMED_EQUIVALENT = "paper_assumed_equivalent"
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
    source_runner_ids: list[str] = Field(default_factory=list)
    constituent_contract_ids: list[str] = Field(default_factory=list)
    canonical_identity: dict[str, Any] | None = None
    fee_snapshot: dict[str, Any] | None = None


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
    if family == "game_winner":
        return "Game winner"
    if family == "point_spread":
        return "Point spread"
    if family == "total_points":
        return "Total points"
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
    with memoize_market_matches():
        return _assemble_fixture_inventory(
            matchbook_markets,
            polymarket_markets,
            kalshi_markets=kalshi_markets,
            matcher=matcher,
            decisions=decisions,
            pair_decisions=pair_decisions,
            venue_costs=venue_costs,
            fx_snapshots=fx_snapshots,
            cost_resolver=cost_resolver,
        )


def _assemble_fixture_inventory(
    matchbook_markets: list[InventoryMarket],
    polymarket_markets: list[InventoryMarket],
    *,
    kalshi_markets: list[InventoryMarket],
    matcher: MarketMatcher,
    decisions: dict[tuple[str, str], PaperScanDecision],
    pair_decisions: dict[tuple[str, str, str, str], PaperScanDecision],
    venue_costs: list[VenueCostSnapshot] | None,
    fx_snapshots: list[FxRateSnapshot] | None,
    cost_resolver: VenueCostResolver | None,
) -> list[FixtureMarketInventoryRow]:
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
    # One structural index and equivalence cache for this fixture. Selection
    # still follows list order: first equivalent Kalshi leg, otherwise the
    # first related leg when the row is not already comparable.
    sources = _InventorySources(matchbook_markets, polymarket_markets)
    attachment_index = _KalshiAttachmentIndex(unmatched_kalshi, matcher, sources)
    attached: set[int] = set()
    for row in rows:
        kalshi_index = _matching_kalshi_index(
            row,
            unmatched_kalshi,
            matcher,
            matchbook_markets=matchbook_markets,
            polymarket_markets=polymarket_markets,
            exclude=attached,
            attachment_index=attachment_index,
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
            attachment_index=attachment_index,
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
            pm_item = sources.item_for(row.polymarket, VenueName.POLYMARKET, polymarket_markets)
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
        # leftover_kalshi is already the unattached unmatched_kalshi slice.
        # `attached` holds unmatched_kalshi indexes, not leftover indexes;
        # mixing them dropped unapproved GAME rows when BTTS was attached
        # at unmatched index 0.
        if index not in used_kalshi:
            rows.append(
                _venue_only_row(
                    item,
                    venue_costs=venue_costs,
                    fx_snapshots=fx_snapshots,
                    cost_resolver=cost_resolver,
                )
            )

    return _sort_rows(rows)


def inventory_is_comparable_opportunity(status: InventoryComparisonStatus | None) -> bool:
    """True for paper-mode comparable opportunities (proven or paper-assumed)."""

    return status in {
        InventoryComparisonStatus.MATCHED_EQUIVALENT,
        InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT,
    }


def inventory_is_hot_refreshable(status: InventoryComparisonStatus | None) -> bool:
    """HOT may quote-refresh proven and paper-assumed persisted relationships."""

    return inventory_is_comparable_opportunity(status)


def inventory_summary(rows: list[FixtureMarketInventoryRow]) -> tuple[int, int, Decimal | None]:
    discovered = len(rows)
    equivalent = sum(
        1 for row in rows if inventory_is_comparable_opportunity(row.comparison_status)
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
    if matched_only:
        return greedy_unique_market_matches(
            [item.canonical for item in left],
            [item.canonical for item in right],
            matcher,
        )
    candidates: list[tuple[float, int, int, MarketMatchResult]] = []
    for left_index, left_item in enumerate(left):
        if left_item.canonical is None:
            continue
        for right_index, right_item in enumerate(right):
            if right_item.canonical is None:
                continue
            match = matcher.match(left_item.canonical, right_item.canonical)
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
        from sports_hedge.catalogue.states import CatalogueApprovalState

        admission = assess_catalogue_admission(left_market, right_market)
        if (
            not admission.allowed
            and admission.assessment.state is not CatalogueApprovalState.UNSUPPORTED
        ):
            catalogue_reason = admission.rejection_reason or "catalogue_review_required"
            specific = admission.assessment.reason
            reason = specific or catalogue_reason
            return (
                InventoryComparisonStatus.OTHER,
                reason,
                [catalogue_reason, specific, *match.reasons],
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
    from sports_hedge.catalogue.admission import assess_catalogue_admission
    from sports_hedge.catalogue.states import CatalogueApprovalState

    admission = assess_catalogue_admission(left_market, right_market)
    status = InventoryComparisonStatus.MATCHED_EQUIVALENT
    if admission.assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT:
        status = InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT
    reason: str | None = admission.assessment.reason if status is InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT else None
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
        InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT,
    ):
        if status in mapped:
            return status
    return InventoryComparisonStatus.MATCHED_EQUIVALENT


def _rejection_maps_to(reason: str) -> InventoryComparisonStatus:
    if reason.startswith("catalogue_"):
        return InventoryComparisonStatus.OTHER
    if reason in {"paper_assumed_equivalent", "paper_assumed_not_live_execution_eligible"}:
        return InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT
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
    if canonical is not None and observation is None and item.venue is VenueName.KALSHI:
        for runner in canonical.runners:
            if runner.label and runner.label not in runner_labels:
                runner_labels.append(runner.label)
            outcome = runner.outcome.value
            if outcome and outcome not in runner_labels:
                runner_labels.append(outcome)
    source_runner_ids: list[str] = []
    constituent_contract_ids: list[str] = []
    canonical_identity: dict[str, Any] | None = None
    fee_snapshot: dict[str, Any] | None = None
    if canonical is not None:
        canonical_identity = canonical.model_dump(mode="json")
        for runner in canonical.runners:
            runner_id = str(runner.source_runner_id or "").strip()
            if not runner_id:
                continue
            source_runner_ids.append(runner_id)
            if item.venue is VenueName.KALSHI:
                ticker = runner_id.rsplit(":", 1)[0].strip()
                if ticker and ticker not in constituent_contract_ids:
                    constituent_contract_ids.append(ticker)
    if observation is not None and isinstance(observation.metadata, dict):
        fee_key = "kalshi_fee" if item.venue is VenueName.KALSHI else (
            "polymarket_fee" if item.venue is VenueName.POLYMARKET else None
        )
        if fee_key:
            snap = observation.metadata.get(fee_key)
            if isinstance(snap, dict):
                fee_snapshot = snap
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
        source_runner_ids=source_runner_ids,
        constituent_contract_ids=constituent_contract_ids,
        canonical_identity=canonical_identity,
        fee_snapshot=fee_snapshot,
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
    sources: _InventorySources | None = None,
) -> list[CanonicalMarket]:
    canonicals: list[CanonicalMarket] = []
    for facts, venue, source in (
        (row.matchbook, VenueName.MATCHBOOK, matchbook_markets),
        (row.polymarket, VenueName.POLYMARKET, polymarket_markets),
    ):
        if sources is None:
            item = _inventory_from_facts(facts, venue, source)
        else:
            item = sources.item_for(facts, venue, source)
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
        # Exact-line intersection for totals: 2.5↔2.5 only. A Matchbook 0.5
        # leftover must not consume the Kalshi 2.5 sibling.
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
        if item.canonical.family in {
            MarketFamily.MATCH_RESULT,
            MarketFamily.TOTAL_GOALS,
            MarketFamily.BOTH_TEAMS_TO_SCORE,
            MarketFamily.FIRST_TEAM_TO_SCORE,
        }:
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
    attachment_index: _KalshiAttachmentIndex | None = None,
) -> list[MarketMatchResult]:
    if kalshi_item.canonical is None:
        return []
    canonicals = _row_canonicals(
        row,
        matchbook_markets=matchbook_markets,
        polymarket_markets=polymarket_markets,
    )
    if attachment_index is None:
        return [matcher.match(canonical, kalshi_item.canonical) for canonical in canonicals]
    return [
        attachment_index.equivalent_result(canonical, kalshi_item.canonical)
        for canonical in canonicals
    ]


_SETTLEMENT_EXEMPT_KALSHI_FAMILIES = {
    MarketFamily.MATCH_RESULT.value,
    MarketFamily.TOTAL_GOALS.value,
    MarketFamily.BOTH_TEAMS_TO_SCORE.value,
    MarketFamily.FIRST_TEAM_TO_SCORE.value,
}


class _InventorySources:
    """First-hit source lookup for one fixture inventory build.

    ``_inventory_from_facts`` returns the first list item with the same venue
    and source id. Repeating that scan for every row and Kalshi candidate is
    the same answer as this map.
    """

    def __init__(
        self,
        matchbook_markets: list[InventoryMarket],
        polymarket_markets: list[InventoryMarket],
    ) -> None:
        self.matchbook_markets = matchbook_markets
        self.polymarket_markets = polymarket_markets
        self._matchbook = _first_source_hits(matchbook_markets, VenueName.MATCHBOOK)
        self._polymarket = _first_source_hits(polymarket_markets, VenueName.POLYMARKET)

    def item_for(
        self,
        facts: VenueMarketFacts | None,
        venue: VenueName,
        source: list[InventoryMarket],
    ) -> InventoryMarket | None:
        if facts is None:
            return None
        if venue is VenueName.MATCHBOOK and source is self.matchbook_markets:
            return self._matchbook.get(facts.source_market_id)
        if venue is VenueName.POLYMARKET and source is self.polymarket_markets:
            return self._polymarket.get(facts.source_market_id)
        return _inventory_from_facts(facts, venue, source)


def _first_source_hits(
    markets: list[InventoryMarket],
    venue: VenueName,
) -> dict[str, InventoryMarket]:
    found: dict[str, InventoryMarket] = {}
    for item in markets:
        if item.venue is not venue:
            continue
        found.setdefault(item.source_market_id, item)
    return found


class _KalshiAttachmentIndex:
    """Fixture-local Kalshi candidates and equivalence cache.

    Buckets keep ascending source indexes, so the first hit is the same leg
    the full list scan would have chosen. Equivalence is cached only for the
    stock ``MarketMatcher`` and ``EventMatcher`` pair, using the same event
    identity and economic fingerprint as the match memo. Other matchers call
    ``match`` on every related candidate.
    """

    def __init__(
        self,
        kalshi_markets: list[InventoryMarket],
        matcher: MarketMatcher,
        sources: _InventorySources,
    ) -> None:
        self.markets = kalshi_markets
        self.matcher = matcher
        self.sources = sources
        self._cacheable = (
            type(matcher) is MarketMatcher and type(matcher.event_matcher) is EventMatcher
        )
        self._equivalent: dict[tuple[object, ...], MarketMatchResult] = {}
        self._fingerprints: dict[int, tuple[object, ...]] = {}
        self._plans: dict[tuple[object, ...], tuple[list[int], list[int]]] = {}
        self._cursors: dict[tuple[object, ...], tuple[int, int]] = {}
        self._catalogue: dict[tuple[object, ...], Any] = {}
        self._owners: list[Any] = []
        self._row_canonicals: dict[int, list[CanonicalMarket]] = {}
        self._by_family: dict[str, list[int]] = {}
        self._by_family_period: dict[tuple[str, str], list[int]] = {}
        self._by_family_line: dict[tuple[str, Decimal], list[int]] = {}
        self._by_family_period_line: dict[tuple[str, str, Decimal], list[int]] = {}
        for index, item in enumerate(kalshi_markets):
            canonical = item.canonical
            if canonical is None:
                continue
            family = canonical.family.value
            period = canonical.period.value
            self._by_family.setdefault(family, []).append(index)
            self._by_family_period.setdefault((family, period), []).append(index)
            if canonical.line is not None:
                line = canonical.line
                self._by_family_line.setdefault((family, line), []).append(index)
                self._by_family_period_line.setdefault((family, period, line), []).append(index)

    def candidates(self, row: FixtureMarketInventoryRow) -> list[int]:
        family = row.family
        if not family:
            return []
        if row.period:
            if row.line is not None:
                return self._by_family_period_line.get((family, row.period, row.line), [])
            return self._by_family_period.get((family, row.period), [])
        if row.line is not None:
            return self._by_family_line.get((family, row.line), [])
        return self._by_family.get(family, [])

    def canonicals_for(self, row: FixtureMarketInventoryRow) -> list[CanonicalMarket]:
        cached = self._row_canonicals.get(id(row))
        if cached is not None:
            return cached
        canonicals = _row_canonicals(
            row,
            matchbook_markets=self.sources.matchbook_markets,
            polymarket_markets=self.sources.polymarket_markets,
            sources=self.sources,
        )
        self._row_canonicals[id(row)] = canonicals
        self._owners.append(row)
        return canonicals

    def fingerprint(self, market: CanonicalMarket) -> tuple[object, ...]:
        cached = self._fingerprints.get(id(market))
        if cached is None:
            cached = economic_match_fingerprint(market)
            self._fingerprints[id(market)] = cached
            self._owners.append(market)
        return cached

    def equivalent_result(self, left: CanonicalMarket, right: CanonicalMarket) -> MarketMatchResult:
        if not self._cacheable:
            return self.matcher.match(left, right)
        key = (
            id(left.event),
            self.fingerprint(left),
            id(right.event),
            self.fingerprint(right),
        )
        cached = self._equivalent.get(key)
        if cached is not None:
            return cached
        result = self.matcher.match(left, right)
        self._equivalent[key] = result
        self._owners.append(left)
        self._owners.append(right)
        return result

    def equivalent(self, left: CanonicalMarket, right: CanonicalMarket) -> bool:
        return self.equivalent_result(left, right).matched

    def select(self, row: FixtureMarketInventoryRow, exclude: set[int]) -> int | None:
        if row.family is None or (row.matchbook is None and row.polymarket is None):
            return None
        if not self._cacheable:
            return self._select_walk(row, exclude)
        canonicals = self.canonicals_for(row)
        if not canonicals:
            return self._select_walk(row, exclude)
        identity = (
            row.family,
            row.period or "",
            None if row.line is None else row.line,
            tuple((id(market.event), self.fingerprint(market)) for market in canonicals),
        )
        plan = self._plans.get(identity)
        if plan is None:
            plan = self._plan(row, canonicals)
            self._plans[identity] = plan
            self._owners.append(row)
        equivalent_indexes, related_indexes = plan
        # Exclusions only grow during one inventory build, so each identity
        # can resume after the leg it already considered.
        equivalent_cursor, related_cursor = self._cursors.get(identity, (0, 0))
        while (
            equivalent_cursor < len(equivalent_indexes)
            and equivalent_indexes[equivalent_cursor] in exclude
        ):
            equivalent_cursor += 1
        if equivalent_cursor < len(equivalent_indexes):
            self._cursors[identity] = (equivalent_cursor, related_cursor)
            return equivalent_indexes[equivalent_cursor]
        if inventory_is_comparable_opportunity(row.comparison_status):
            self._cursors[identity] = (equivalent_cursor, related_cursor)
            return None
        while related_cursor < len(related_indexes) and related_indexes[related_cursor] in exclude:
            related_cursor += 1
        self._cursors[identity] = (equivalent_cursor, related_cursor)
        if related_cursor < len(related_indexes):
            return related_indexes[related_cursor]
        return None

    def _plan(
        self,
        row: FixtureMarketInventoryRow,
        canonicals: list[CanonicalMarket],
    ) -> tuple[list[int], list[int]]:
        equivalent_indexes: list[int] = []
        related_indexes: list[int] = []
        for index in self.candidates(row):
            item = self.markets[index]
            if item.canonical is None or not self._is_related(row, item):
                continue
            if any(self.equivalent(canonical, item.canonical) for canonical in canonicals):
                equivalent_indexes.append(index)
            else:
                related_indexes.append(index)
        return equivalent_indexes, related_indexes

    def _select_walk(self, row: FixtureMarketInventoryRow, exclude: set[int]) -> int | None:
        comparable = inventory_is_comparable_opportunity(row.comparison_status)
        canonicals = self.canonicals_for(row)
        first_related: int | None = None
        for index in self.candidates(row):
            if index in exclude:
                continue
            item = self.markets[index]
            if item.canonical is None or not self._is_related(row, item):
                continue
            if any(self.equivalent(canonical, item.canonical) for canonical in canonicals):
                return index
            if comparable:
                continue
            if first_related is None:
                first_related = index
        return first_related

    def _is_related(self, row: FixtureMarketInventoryRow, item: InventoryMarket) -> bool:
        # Match result, totals, BTTS, and FTTS stay related across settlement
        # keys. The structural buckets already applied family, period, and
        # line, so those families do not need another settlement pass.
        if row.family in _SETTLEMENT_EXEMPT_KALSHI_FAMILIES:
            return item.canonical is not None
        return _kalshi_related_to_row(row, item)

    def catalogue_for(
        self,
        row: FixtureMarketInventoryRow,
        kalshi_item: InventoryMarket,
        *,
        matchbook_markets: list[InventoryMarket],
        polymarket_markets: list[InventoryMarket],
    ) -> Any:
        if not self._cacheable or kalshi_item.canonical is None:
            return _kalshi_catalogue_admission(
                row,
                kalshi_item,
                matchbook_markets=matchbook_markets,
                polymarket_markets=polymarket_markets,
            )
        canonicals = self.canonicals_for(row)
        if not canonicals:
            return _kalshi_catalogue_admission(
                row,
                kalshi_item,
                matchbook_markets=matchbook_markets,
                polymarket_markets=polymarket_markets,
            )
        left = canonicals[0]
        right = kalshi_item.canonical
        key = (
            id(left.event),
            self.fingerprint(left),
            id(right.event),
            self.fingerprint(right),
        )
        if key in self._catalogue:
            return self._catalogue[key]
        admission = _kalshi_catalogue_admission(
            row,
            kalshi_item,
            matchbook_markets=matchbook_markets,
            polymarket_markets=polymarket_markets,
        )
        self._catalogue[key] = admission
        self._owners.append(left)
        self._owners.append(right)
        return admission


def _matching_kalshi_index(
    row: FixtureMarketInventoryRow,
    kalshi_markets: list[InventoryMarket],
    matcher: MarketMatcher,
    *,
    matchbook_markets: list[InventoryMarket],
    polymarket_markets: list[InventoryMarket],
    exclude: set[int] | None = None,
    attachment_index: _KalshiAttachmentIndex | None = None,
) -> int | None:
    # The first equivalent Kalshi leg in list order wins. A non-equivalent
    # leg is used only when no equivalent leg exists, and only when this row
    # is not already a comparable opportunity. Stopping at the first
    # equivalent leg is the same answer as scanning the rest.
    skipped = exclude or set()
    if attachment_index is None:
        attachment_index = _KalshiAttachmentIndex(
            kalshi_markets,
            matcher,
            _InventorySources(matchbook_markets, polymarket_markets),
        )
    return attachment_index.select(row, skipped)


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
    attachment_index: _KalshiAttachmentIndex | None = None,
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
        attachment_index=attachment_index,
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
    if attachment_index is None:
        catalogue = _kalshi_catalogue_admission(
            row,
            kalshi_item,
            matchbook_markets=matchbook_markets,
            polymarket_markets=polymarket_markets,
        )
    else:
        catalogue = attachment_index.catalogue_for(
            row,
            kalshi_item,
            matchbook_markets=matchbook_markets,
            polymarket_markets=polymarket_markets,
        )
    if catalogue is not None and not catalogue.allowed:
        catalogue_reason = catalogue.rejection_reason or "catalogue_review_required"
        reason = catalogue.assessment.reason or catalogue_reason
        row.comparison_status = InventoryComparisonStatus.OTHER
        row.reason = reason
        if catalogue_reason not in row.rejection_reasons:
            row.rejection_reasons.append(catalogue_reason)
        if reason not in row.rejection_reasons:
            row.rejection_reasons.append(reason)
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
        if best.entered_solver and best_decision is not None and row.trigger_net_edge is None:
            # Keep current_net_edge and trigger_net_edge paired. Kalshi attach
            # can fill economics after a venue-only row; Wave 1A HOT proximity
            # must not see a net ROI with a missing operator Min Net Arb.
            row.trigger_net_edge = best_decision.minimum_net_edge
            if row.current_net_edge is None:
                row.current_net_edge = best.current_net_edge
        if best.entered_solver or not best.rejection_reasons:
            row.comparison_status = _comparable_status_from_catalogue(catalogue)
            row.reason = _comparable_reason(row.comparison_status, row.reason)
        return
    row.comparison_status = _comparable_status_from_catalogue(catalogue)
    row.reason = _comparable_reason(row.comparison_status, row.reason)


def _comparable_status_from_catalogue(catalogue: Any) -> InventoryComparisonStatus:
    from sports_hedge.catalogue.states import CatalogueApprovalState

    if (
        catalogue is not None
        and catalogue.assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    ):
        return InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT
    return InventoryComparisonStatus.MATCHED_EQUIVALENT


def _comparable_reason(
    status: InventoryComparisonStatus,
    current: str | None,
) -> str | None:
    if status is InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT:
        if current in {None, "venue_only"}:
            return "paper_assumed_equivalent"
        return current
    if current == "venue_only":
        return None
    return current


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
