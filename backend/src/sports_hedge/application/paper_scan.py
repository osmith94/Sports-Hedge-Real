from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sports_hedge.application.market_observation import VenueMarketObservation
from sports_hedge.application.quote_freshness import (
    conservative_combined_age_ms,
    conservative_combined_basis,
    require_aware_instant,
)
from sports_hedge.arbitrage.depth import DepthAwareCompleteSetScanner, DepthQuoteSource
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.football import CanonicalOutcome, SettlementScope
from sports_hedge.arbitrage.priority_alerts.models import LegExecutionMode
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import MarketAction, VenueCostSnapshot
from sports_hedge.fees.effective import CostRuleError, apply_venue_costs
from sports_hedge.fees.models import FeeSnapshot
from sports_hedge.fees.resolver import UnknownRequiredCostError, VenueCostResolver
from sports_hedge.fx.models import FxRateUnavailable
from sports_hedge.fx.service import FxRateService
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.normalization.identity import (
    canonical_matched_event_id,
    canonical_matched_market_id,
    canonical_source_event_id,
    canonical_source_market_id,
)
from sports_hedge.paper.fills import PaperOpportunityLeg
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision
from sports_hedge.risk.execution import ExecutionRiskInputs, ExecutionRiskScorer


class PaperScanService:
    """Orchestrate strict matching, snapshot capture and paper-only arb analysis."""

    def __init__(
        self,
        market_intelligence: MarketIntelligenceService,
        *,
        market_matcher: MarketMatcher | None = None,
        depth_scanner: DepthAwareCompleteSetScanner | None = None,
        risk_scorer: ExecutionRiskScorer | None = None,
        settings: Settings | None = None,
        fx_service: FxRateService | None = None,
        cost_resolver: VenueCostResolver | None = None,
    ) -> None:
        self.market_intelligence = market_intelligence
        self.market_matcher = market_matcher or MarketMatcher()
        self.depth_scanner = depth_scanner or DepthAwareCompleteSetScanner()
        self.risk_scorer = risk_scorer or ExecutionRiskScorer()
        self.settings = settings or get_settings()
        self.fx_service = fx_service
        self.cost_resolver = cost_resolver

    def record_observation(self, observation: VenueMarketObservation) -> int:
        event_id = canonical_source_event_id(observation.market.event)
        market_id = canonical_source_market_id(event_id, observation.market)
        return self._record_with_ids(observation, event_id=event_id, market_id=market_id)

    def scan_pair(
        self,
        left: VenueMarketObservation,
        right: VenueMarketObservation,
        *,
        fee_snapshots: list[FeeSnapshot] | None = None,
        venue_costs: list[VenueCostSnapshot] | None = None,
        fx_snapshots: list[FxRateSnapshot] | None = None,
        capital_limit_gbp: Decimal | None = None,
        minimum_net_edge: Decimal = Decimal("0.005"),
        maximum_execution_risk: int = 60,
        minimum_mapping_confidence: float = 0.98,
        assumed_latency_ms: int = 500,
        recent_volatility_bps: float = 0.0,
    ) -> PaperScanDecision:
        if minimum_net_edge < 0:
            raise ValueError("minimum_net_edge must be non-negative")
        if not 0 <= maximum_execution_risk <= 100:
            raise ValueError("maximum_execution_risk must be between 0 and 100")
        if not 0 <= minimum_mapping_confidence <= 1:
            raise ValueError("minimum_mapping_confidence must be between 0 and 1")

        match = self.market_matcher.match(left.market, right.market)
        fees = list(fee_snapshots or [])
        rejections: list[str] = []
        assumption_labels: list[str] = []
        evaluated_at = datetime.now(UTC)
        costs, cost_resolve_reasons = self._resolve_costs(
            left,
            right,
            venue_costs=venue_costs,
            as_of=evaluated_at,
        )
        fx, fx_resolve_reasons = self._resolve_fx(
            left,
            right,
            fx_snapshots=fx_snapshots,
            as_of=evaluated_at,
        )
        rejections.extend(cost_resolve_reasons)
        rejections.extend(fx_resolve_reasons)
        quote_age_ms = conservative_combined_age_ms(left.quote_age_ms, right.quote_age_ms)
        quote_age_basis = conservative_combined_basis(
            left.metadata.get("quote_age_basis") if isinstance(left.metadata, dict) else None,
            right.metadata.get("quote_age_basis") if isinstance(right.metadata, dict) else None,
        )
        for snapshot in fees:
            rejections.extend(
                _cost_clock_reasons(snapshot.captured_at, kind="fee", as_of=evaluated_at)
            )
        for snapshot in costs:
            rejections.extend(
                _cost_clock_reasons(snapshot.captured_at, kind="fee", as_of=evaluated_at)
            )
        for snapshot in fx:
            rejections.extend(
                _cost_clock_reasons(snapshot.captured_at, kind="fx", as_of=evaluated_at)
            )
        for observation in (left, right):
            reason = observation.metadata.get("quote_age_reason")
            if isinstance(reason, str) and reason.strip():
                rejections.append(reason)
        if quote_age_ms is None and "unknown_quote_age" not in rejections:
            rejections.append("unknown_quote_age")

        if not match.matched:
            recorded = self.record_observation(left) + self.record_observation(right)
            return PaperScanDecision(
                market_match=match,
                snapshots_recorded=recorded,
                rejection_reasons=["market_not_equivalent", *match.reasons],
                fee_snapshots=fees,
                venue_costs=costs,
                fx_snapshots=fx,
                cost_assumption_labels=assumption_labels,
                minimum_net_edge=minimum_net_edge,
                maximum_execution_risk=maximum_execution_risk,
                quote_age_ms=quote_age_ms,
                quote_age_basis=quote_age_basis,
            )

        event_id = canonical_matched_event_id([left.market.event, right.market.event])
        market_id = canonical_matched_market_id(event_id, [left.market, right.market])
        recorded = self._record_with_ids(left, event_id=event_id, market_id=market_id)
        recorded += self._record_with_ids(right, event_id=event_id, market_id=market_id)

        if left.venue == right.venue:
            rejections.append("same_venue_pair")
        if match.confidence < minimum_mapping_confidence:
            rejections.append("mapping_confidence_below_threshold")
        if (
            left.market.settlement.scope == SettlementScope.UNKNOWN
            or right.market.settlement.scope == SettlementScope.UNKNOWN
        ):
            rejections.append("unknown_settlement_scope")

        expected_outcomes = [runner.outcome for runner in left.market.runners]
        if any(outcome == CanonicalOutcome.OTHER for outcome in expected_outcomes):
            rejections.append("noncanonical_outcome_space")

        if costs:
            cost_map = {snapshot.venue: snapshot for snapshot in costs}
        else:
            cost_map = {}
            if fees:
                rejections.append("legacy_fee_snapshot_not_cost_truth")
        scan_costs: dict[VenueName, VenueCostSnapshot] = {}
        missing_fees = False
        for observation in (left, right):
            cost = cost_map.get(observation.venue)
            if cost is None:
                rejections.append(f"missing_venue_cost:{observation.venue.value}")
                missing_fees = True
                continue
            action_reason = _action_mismatch(observation.venue, cost.action)
            if action_reason:
                rejections.append(action_reason)
                missing_fees = True
                continue
            scan_costs[observation.venue] = cost
            try:
                apply_venue_costs(
                    cost,
                    gross_decimal_odds=Decimal("2"),
                    require_gbp=False,
                    as_of=evaluated_at,
                )
            except CostRuleError as exc:
                rejections.append(exc.reason)
                missing_fees = True

        fx_map = {snapshot.currency: snapshot for snapshot in fx}
        missing_fx = sorted(
            {
                observation.native_currency
                for observation in (left, right)
                if observation.native_currency not in fx_map
            }
        )
        if missing_fx:
            rejections.extend(f"missing_fx_rate:{currency}" for currency in missing_fx)
        cost_clock_blocked = any(
            reason.startswith("future_") or reason.startswith("invalid_")
            for reason in rejections
        )
        if missing_fees or missing_fx or cost_clock_blocked:
            return PaperScanDecision(
                market_match=match,
                canonical_event_id=event_id,
                canonical_market_id=market_id,
                snapshots_recorded=recorded,
                rejection_reasons=_dedupe(rejections),
                fee_snapshots=fees,
                venue_costs=list(scan_costs.values()) or costs,
                fx_snapshots=fx,
                cost_assumption_labels=assumption_labels,
                minimum_net_edge=minimum_net_edge,
                maximum_execution_risk=maximum_execution_risk,
                quote_age_ms=quote_age_ms,
                quote_age_basis=quote_age_basis,
            )

        sources: list[DepthQuoteSource] = []
        configured_spread = Decimal(self.settings.fx_spread_bps)
        configured_fx_slip = Decimal("0")
        configured_book_slip = Decimal(self.settings.max_slippage_bps)
        if configured_book_slip > 0:
            assumption_labels.append(f"configured_book_slippage_bps:{configured_book_slip}")
        effective_fx: dict[str, Decimal] = {}
        for snapshot in fx:
            rate, labels = snapshot.effective_gbp_per_unit(
                configured_spread_bps=configured_spread,
                configured_conversion_slippage_bps=configured_fx_slip,
            )
            effective_fx[snapshot.currency] = rate
            assumption_labels.extend(labels)
            if snapshot.check_status:
                assumption_labels.append(
                    f"fx_{snapshot.currency}:{snapshot.source}:{snapshot.check_status}"
                )
        for observation in (left, right):
            rate = effective_fx[observation.native_currency]
            cost = scan_costs[observation.venue]
            for book in observation.outcome_books:
                if not book.back_levels:
                    continue
                sources.append(
                    DepthQuoteSource(
                        outcome=book.outcome.value,
                        venue=observation.venue,
                        source_market_id=observation.market.source_market_id,
                        source_runner_id=book.source_runner_id,
                        levels=[
                            BookLevel(
                                decimal_odds=level.decimal_odds,
                                available_stake=level.available_stake * rate,
                            )
                            for level in book.back_levels
                        ],
                        cost=cost,
                    )
                )

        depth_scan = self.depth_scanner.scan(
            sources,
            expected_outcomes=[outcome.value for outcome in expected_outcomes],
            capital_limit=capital_limit_gbp,
            configured_slippage_bps=configured_book_slip if configured_book_slip > 0 else None,
        )
        rejections.extend(depth_scan.cost_rejection_reasons)
        solution = depth_scan.solution
        if not solution.is_arbitrage:
            rejections.append(solution.rejection_reason or "no_arbitrage")
        elif solution.roi < minimum_net_edge:
            rejections.append("net_edge_below_threshold")

        risk_inputs = self._risk_inputs(
            left,
            right,
            depth_scan=depth_scan,
            assumed_latency_ms=assumed_latency_ms,
            recent_volatility_bps=recent_volatility_bps,
            quote_age_ms=quote_age_ms if quote_age_ms is not None else 10**9,
        )
        risk = None
        if risk_inputs is None:
            rejections.append("missing_risk_evidence")
        else:
            risk = self.risk_scorer.score(risk_inputs)
            if risk.score > maximum_execution_risk:
                rejections.append("execution_risk_above_threshold")

        execution_modes = {
            left.venue: _default_execution_mode(left.venue),
            right.venue: _default_execution_mode(right.venue),
        }
        fill_legs = _fill_legs_from_observations(
            left,
            right,
            depth_scan=depth_scan,
            effective_fx=effective_fx,
        )

        return PaperScanDecision(
            market_match=match,
            canonical_event_id=event_id,
            canonical_market_id=market_id,
            snapshots_recorded=recorded,
            depth_scan=depth_scan,
            execution_risk=risk,
            eligible_for_paper_simulation=not rejections,
            rejection_reasons=_dedupe(rejections),
            fee_snapshots=fees,
            venue_costs=list(scan_costs.values()),
            fx_snapshots=fx,
            cost_assumption_labels=_dedupe(assumption_labels),
            minimum_net_edge=minimum_net_edge,
            maximum_execution_risk=maximum_execution_risk,
            quote_age_ms=quote_age_ms,
            quote_age_basis=quote_age_basis,
            fill_legs=fill_legs,
            execution_modes=execution_modes,
        )

    def _record_with_ids(
        self,
        observation: VenueMarketObservation,
        *,
        event_id: str,
        market_id: str,
    ) -> int:
        snapshots = observation.snapshots(
            canonical_event_id=event_id,
            canonical_market_id=market_id,
        )
        for snapshot in snapshots:
            self.market_intelligence.record_snapshot(snapshot)
        return len(snapshots)

    def _risk_inputs(
        self,
        left: VenueMarketObservation,
        right: VenueMarketObservation,
        *,
        depth_scan,
        assumed_latency_ms: int,
        recent_volatility_bps: float,
        quote_age_ms: int,
    ) -> ExecutionRiskInputs | None:
        selected_quotes = list(depth_scan.selected_quotes)
        if len(selected_quotes) < 2:
            return None

        observation_by_venue = {left.venue: left, right.venue: right}
        spread_bps = 0.0
        size_to_depth_ratio = 0.0
        hedge_liquidity_ratio = 1.0
        stakes = {stake.outcome: stake for stake in depth_scan.solution.stakes}

        for selected in selected_quotes:
            observation = observation_by_venue.get(selected.venue)
            book = (
                observation.book_for(CanonicalOutcome(selected.outcome))
                if observation is not None
                else None
            )
            if book is None:
                return None
            spread_bps = max(spread_bps, book.probability_spread_bps)
            stake = stakes.get(selected.outcome)
            if stake is None or stake.stake <= 0 or selected.cumulative_depth <= 0:
                return None
            ratio = float(stake.stake / selected.cumulative_depth)
            size_to_depth_ratio = max(size_to_depth_ratio, ratio)
            hedge_liquidity_ratio = min(
                hedge_liquidity_ratio,
                min(float(selected.cumulative_depth / stake.stake), 1.0),
            )

        observed_at = max(left.observed_at, right.observed_at)
        minutes_to_kickoff = max(
            0.0,
            (left.market.event.kickoff_utc - observed_at).total_seconds() / 60.0,
        )
        return ExecutionRiskInputs(
            spread_bps=spread_bps,
            size_to_depth_ratio=size_to_depth_ratio,
            quote_age_ms=quote_age_ms,
            recent_volatility_bps=recent_volatility_bps,
            leg_count=len(selected_quotes),
            minutes_to_kickoff=minutes_to_kickoff,
            assumed_latency_ms=assumed_latency_ms,
            hedge_liquidity_ratio=hedge_liquidity_ratio,
        )

    def _resolve_costs(
        self,
        left: VenueMarketObservation,
        right: VenueMarketObservation,
        *,
        venue_costs: list[VenueCostSnapshot] | None,
        as_of: datetime,
    ) -> tuple[list[VenueCostSnapshot], list[str]]:
        if venue_costs is not None:
            return list(venue_costs), []
        if self.cost_resolver is None:
            return [], []
        resolved: list[VenueCostSnapshot] = []
        reasons: list[str] = []
        for observation in (left, right):
            action = (
                MarketAction.BUY
                if observation.venue is VenueName.POLYMARKET
                else MarketAction.BACK
            )
            try:
                resolved.append(
                    self.cost_resolver.resolve(
                        venue=observation.venue,
                        market_class=observation.market.family,
                        action=action,
                        as_of=as_of,
                    )
                )
            except UnknownRequiredCostError as exc:
                reasons.append(exc.reason)
        return resolved, reasons

    def _resolve_fx(
        self,
        left: VenueMarketObservation,
        right: VenueMarketObservation,
        *,
        fx_snapshots: list[FxRateSnapshot] | None,
        as_of: datetime,
    ) -> tuple[list[FxRateSnapshot], list[str]]:
        if fx_snapshots is not None:
            return _with_gbp_rate(list(fx_snapshots), as_of=as_of), []
        if self.fx_service is None:
            return _with_gbp_rate([], as_of=as_of), []
        currencies = {left.native_currency, right.native_currency, "GBP"}
        try:
            return self.fx_service.paper_snapshots(currencies, as_of=as_of), []
        except FxRateUnavailable as exc:
            return _with_gbp_rate([], as_of=as_of), [exc.reason]


def _cost_clock_reasons(captured_at: datetime, *, kind: str, as_of: datetime) -> list[str]:
    try:
        captured = require_aware_instant(captured_at, "captured_at")
        evaluated = require_aware_instant(as_of, "as_of")
    except ValueError:
        return [f"invalid_{kind}_snapshot_time"]
    if captured > evaluated:
        return [f"future_{kind}_snapshot"]
    return []


def _with_gbp_rate(rates: list[FxRateSnapshot], *, as_of: datetime | None = None) -> list[FxRateSnapshot]:
    if not any(rate.currency.upper() == "GBP" for rate in rates):
        rates.append(
            FxRateSnapshot(
                currency="GBP",
                gbp_per_unit=Decimal("1"),
                source="functional_currency",
                captured_at=as_of or datetime.now(UTC),
            )
        )
    return rates


def _dedupe(values: list[str]) -> list[str]:
    return list(dict.fromkeys(values))


def _default_execution_mode(venue: VenueName) -> LegExecutionMode:
    if venue is VenueName.POLYMARKET:
        return LegExecutionMode.EXTERNAL_OPERATOR
    return LegExecutionMode.INTERNAL


def _fill_legs_from_observations(
    left,
    right,
    *,
    depth_scan,
    effective_fx: dict[str, Decimal],
) -> list[PaperOpportunityLeg]:
    selected = {(quote.outcome, quote.venue) for quote in depth_scan.selected_quotes}
    stakes = {stake.outcome: stake for stake in depth_scan.solution.stakes}
    legs: list[PaperOpportunityLeg] = []
    for observation in (left, right):
        rate = effective_fx.get(observation.native_currency)
        if rate is None or rate <= 0:
            continue
        for book in observation.outcome_books:
            outcome = book.outcome.value
            if (outcome, observation.venue) not in selected or not book.back_levels:
                continue
            best = max(book.back_levels, key=lambda item: item.decimal_odds)
            stake = stakes.get(outcome)
            visible = sum((level.available_stake for level in book.back_levels), Decimal("0"))
            requested = stake.stake / rate if stake is not None and stake.stake > 0 else visible
            if visible > 0:
                requested = min(requested, visible)
            if requested <= 0:
                continue
            legs.append(
                PaperOpportunityLeg(
                    outcome=outcome,
                    venue=observation.venue,
                    source_market_id=observation.market.source_market_id,
                    source_runner_id=book.source_runner_id,
                    currency=observation.native_currency,
                    requested_stake=requested,
                    displayed_odds=best.decimal_odds,
                    levels=list(book.back_levels),
                    quote_age_ms=observation.quote_age_ms,
                    quote_captured_at=observation.observed_at,
                )
            )
    return legs


def _action_mismatch(venue: VenueName, action: MarketAction) -> str | None:
    if venue is VenueName.POLYMARKET and action is not MarketAction.BUY:
        return f"unsupported_action:{venue.value}"
    if venue in {VenueName.MATCHBOOK, VenueName.SMARKETS} and action is not MarketAction.BACK:
        return f"unsupported_action:{venue.value}"
    return None
