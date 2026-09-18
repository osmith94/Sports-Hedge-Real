from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from time import monotonic

from sports_hedge.application.complete_set import (
    SOLVER_MODEL_SIMPLE,
    UNSUPPORTED_STATE_PAYOFF_FEE_BASIS,
    complete_set_outcomes,
    generalized_payoff_eligible_pair,
    generalized_state_model_for_pair,
    scan_ineligibility_reason,
    solver_eligible_market,
    solver_model_for_pair,
)
from sports_hedge.application.market_observation import VenueMarketObservation
from sports_hedge.application.executable_liquidity import (
    opening_liquidity_rejection_reasons,
)
from sports_hedge.application.quote_freshness import (
    conservative_combined_age_ms,
    conservative_combined_basis,
    require_aware_instant,
)
from sports_hedge.arbitrage.depth import DepthAwareCompleteSetScanner, DepthQuoteSource
from sports_hedge.arbitrage.payoff_scan import (
    DepthAwarePayoffScanner,
    PayoffScanResult,
    is_state_safe_fee,
)
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
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.learned_rules import LearnedMappingApplicator
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.matching.approved_register import registered_structural_match
from sports_hedge.matching.paper_assumed import PAPER_NONBLOCKING_REJECTION_REASONS
from sports_hedge.normalization.identity import (
    canonical_matched_event_id,
    canonical_matched_market_id,
    canonical_source_event_id,
    canonical_source_market_id,
)
from sports_hedge.paper.fills import PaperOpportunityLeg
from sports_hedge.paper.liquidity import PaperLiquiditySnapshot
from sports_hedge.arbitrage.allocation.adapters import (
    balances_from_liquidity,
    estimated_time_to_release,
    exposures_from_trades,
    request_from_paper_decision,
)
from sports_hedge.arbitrage.allocation.engine import allocate
from sports_hedge.arbitrage.allocation.models import AllocatedStake, AllocationResult
from sports_hedge.arbitrage.allocation.policy import policy_from_settings
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision
from sports_hedge.paper.position_management.quotes import LatestObservationCatalog
from sports_hedge.paper.trades import PaperTrade
from sports_hedge.persistence.liquidity import SqlitePaperLiquidityRepository
from sports_hedge.risk.execution import ExecutionRiskInputs, ExecutionRiskScorer


class PaperScanService:
    """Orchestrate strict matching, snapshot capture and paper-only arb analysis."""

    def __init__(
        self,
        market_intelligence: MarketIntelligenceService,
        *,
        market_matcher: MarketMatcher | None = None,
        depth_scanner: DepthAwareCompleteSetScanner | None = None,
        payoff_scanner: DepthAwarePayoffScanner | None = None,
        risk_scorer: ExecutionRiskScorer | None = None,
        settings: Settings | None = None,
        fx_service: FxRateService | None = None,
        cost_resolver: VenueCostResolver | None = None,
        liquidity: SqlitePaperLiquidityRepository | None = None,
        open_trades: list[PaperTrade] | None = None,
        mapping_rule_store: object | None = None,
        reverse_catalog: LatestObservationCatalog | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.market_intelligence = market_intelligence
        if market_matcher is None:
            applicator = (
                LearnedMappingApplicator(mapping_rule_store)
                if mapping_rule_store is not None
                else None
            )
            market_matcher = MarketMatcher(EventMatcher(learned_applicator=applicator))
        self.market_matcher = market_matcher
        self.depth_scanner = depth_scanner or DepthAwareCompleteSetScanner()
        self.payoff_scanner = payoff_scanner or DepthAwarePayoffScanner()
        self.risk_scorer = risk_scorer or ExecutionRiskScorer()
        self.settings = settings or get_settings()
        self.fx_service = fx_service
        self.cost_resolver = cost_resolver
        self.liquidity = liquidity
        self.open_trades = open_trades or []
        self.reverse_catalog = reverse_catalog
        self._clock = clock or (lambda: datetime.now(UTC))
        self.last_scan_phase_ms: dict[str, int] = {
            "mapping_equivalence": 0,
            "fees_fx_risk": 0,
            "solver_allocation": 0,
        }

    def _stamp_scan_phases(
        self,
        *,
        mapping_ms: int,
        fee_started: float,
        solver_started: float | None = None,
    ) -> None:
        now = monotonic()
        self.last_scan_phase_ms = {
            "mapping_equivalence": max(0, mapping_ms),
            "fees_fx_risk": max(0, int(((solver_started or now) - fee_started) * 1000)),
            "solver_allocation": (
                0 if solver_started is None else max(0, int((now - solver_started) * 1000))
            ),
        }

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
        liquidity_snapshot: PaperLiquiditySnapshot | None = None,
        minimum_net_edge: Decimal = Decimal("0.005"),
        maximum_execution_risk: int = 60,
        minimum_mapping_confidence: float = 0.98,
        assumed_latency_ms: int = 500,
        recent_volatility_bps: float = 0.0,
        open_trades: list[PaperTrade] | None = None,
        conditionally_releasable: dict | None = None,
        fixture_canonical_event_id: str | None = None,
    ) -> PaperScanDecision:
        if minimum_net_edge < 0:
            raise ValueError("minimum_net_edge must be non-negative")
        if not 0 <= maximum_execution_risk <= 100:
            raise ValueError("maximum_execution_risk must be between 0 and 100")
        if not 0 <= minimum_mapping_confidence <= 1:
            raise ValueError("minimum_mapping_confidence must be between 0 and 1")

        map_started = monotonic()
        match = self.market_matcher.match(left.market, right.market)
        mapping_ms = max(0, int((monotonic() - map_started) * 1000))
        register_admitted = match.matched and registered_structural_match(
            left.market, right.market
        )

        def mapping_review_evidence():
            """Deprecated scan-path audit. Never an admission gate for register rows."""

            if register_admitted:
                return None
            nonlocal mapping_ms
            from sports_hedge.application.mapping_review import evidence_from_markets

            review_started = monotonic()
            candidate = evidence_from_markets(
                left.market,
                right.market,
                matcher=self.market_matcher,
                match=match,
                left_raw=left.metadata if isinstance(left.metadata, dict) else None,
                right_raw=right.metadata if isinstance(right.metadata, dict) else None,
            )
            mapping_ms += max(0, int((monotonic() - review_started) * 1000))
            return candidate
        fee_started = monotonic()
        solver_started: float | None = None
        fees = list(fee_snapshots or [])
        rejections: list[str] = []
        assumption_labels: list[str] = []
        evaluated_at = self._clock()
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
        from sports_hedge.fees.labels import cost_assumption_labels_for_snapshots

        assumption_labels.extend(cost_assumption_labels_for_snapshots(costs))
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
            mapping_review_candidate = mapping_review_evidence()
            self._stamp_scan_phases(mapping_ms=mapping_ms, fee_started=fee_started)
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
                mapping_review_candidate=mapping_review_candidate,
            )

        pair_event_id = canonical_matched_event_id([left.market.event, right.market.event])
        fixture_id = (fixture_canonical_event_id or "").strip() or None
        event_id = fixture_id or pair_event_id
        market_id = canonical_matched_market_id(pair_event_id, [left.market, right.market])
        recorded = self._record_with_ids(left, event_id=event_id, market_id=market_id)
        recorded += self._record_with_ids(right, event_id=event_id, market_id=market_id)

        if left.venue == right.venue:
            rejections.append("same_venue_pair")
        if (not register_admitted) and match.confidence < minimum_mapping_confidence:
            rejections.append("mapping_confidence_below_threshold")
        if (
            left.market.settlement.scope == SettlementScope.UNKNOWN
            or right.market.settlement.scope == SettlementScope.UNKNOWN
        ):
            from sports_hedge.matching.paper_assumed import (
                allow_unknown_settlement_for_paper_assumed,
            )

            if not allow_unknown_settlement_for_paper_assumed(left.market, right.market):
                rejections.append("unknown_settlement_scope")

        from sports_hedge.catalogue.admission import assess_catalogue_admission

        catalogue_admission = assess_catalogue_admission(left.market, right.market)
        if not catalogue_admission.allowed:
            rejections.append(
                catalogue_admission.rejection_reason or "catalogue_review_required"
            )
            mapping_review_candidate = mapping_review_evidence()
            self._stamp_scan_phases(mapping_ms=mapping_ms, fee_started=fee_started)
            return PaperScanDecision(
                market_match=match,
                canonical_event_id=event_id,
                canonical_market_id=market_id,
                fixture_canonical_event_id=fixture_id,
                snapshots_recorded=recorded,
                rejection_reasons=_dedupe(rejections),
                fee_snapshots=fees,
                venue_costs=costs,
                fx_snapshots=fx,
                cost_assumption_labels=assumption_labels,
                minimum_net_edge=minimum_net_edge,
                maximum_execution_risk=maximum_execution_risk,
                quote_age_ms=quote_age_ms,
                quote_age_basis=quote_age_basis,
                mapping_review_candidate=mapping_review_candidate,
                solver_model=None,
            )

        solver_model = solver_model_for_pair(left.market, right.market)
        if solver_model is None:
            ineligible = scan_ineligibility_reason(left.market)
            if solver_eligible_market(left.market) or generalized_payoff_eligible_pair(
                left.market, right.market
            ):
                ineligible = scan_ineligibility_reason(right.market)
            rejections.append(ineligible)
            mapping_review_candidate = mapping_review_evidence()
            self._stamp_scan_phases(mapping_ms=mapping_ms, fee_started=fee_started)
            return PaperScanDecision(
                market_match=match,
                canonical_event_id=event_id,
                canonical_market_id=market_id,
                fixture_canonical_event_id=fixture_id,
                snapshots_recorded=recorded,
                rejection_reasons=_dedupe(rejections),
                fee_snapshots=fees,
                venue_costs=costs,
                fx_snapshots=fx,
                cost_assumption_labels=assumption_labels,
                minimum_net_edge=minimum_net_edge,
                maximum_execution_risk=maximum_execution_risk,
                quote_age_ms=quote_age_ms,
                quote_age_basis=quote_age_basis,
                mapping_review_candidate=mapping_review_candidate,
                solver_model=None,
            )

        expected_space = complete_set_outcomes(left.market.family) if solver_model == SOLVER_MODEL_SIMPLE else None
        if expected_space is not None:
            expected_outcomes = sorted(expected_space, key=lambda outcome: outcome.value)
        else:
            expected_outcomes = sorted(
                {book.outcome for book in [*left.outcome_books, *right.outcome_books]},
                key=lambda outcome: outcome.value,
            )
        if any(outcome == CanonicalOutcome.OTHER for outcome in expected_outcomes):
            rejections.append("noncanonical_outcome_space")

        if venue_costs is None and fees:
            rejections.append("legacy_fee_snapshot_not_cost_truth")
        if costs:
            cost_map = {snapshot.venue: snapshot for snapshot in costs}
        else:
            cost_map = {}
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
            if solver_model != SOLVER_MODEL_SIMPLE and not is_state_safe_fee(cost):
                rejections.append(UNSUPPORTED_STATE_PAYOFF_FEE_BASIS)
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
            mapping_review_candidate = mapping_review_evidence()
            self._stamp_scan_phases(mapping_ms=mapping_ms, fee_started=fee_started)
            return PaperScanDecision(
                market_match=match,
                canonical_event_id=event_id,
                canonical_market_id=market_id,
                fixture_canonical_event_id=fixture_id,
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
                mapping_review_candidate=mapping_review_candidate,
                solver_model=solver_model,
            )

        solver_started = monotonic()
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
                # Back/buy only. Matchbook lay levels stay on the observation
                # for display/risk and never enter either solver path.
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

        standing = liquidity_snapshot
        fx_source = next(
            (snapshot.source for snapshot in fx if snapshot.currency.upper() == "USD"),
            "backend_fx",
        )
        if standing is None and self.liquidity is not None:
            standing = self.liquidity.get(gbp_per_unit=effective_fx, fx_source=fx_source)
        venue_capital_limits = None
        if standing is not None:
            venue_capital_limits = standing.solver_gbp_limits(effective_fx)

        depth_scan = None
        payoff_scan: PayoffScanResult | None = None
        slip = configured_book_slip if configured_book_slip > 0 else None
        if solver_model == SOLVER_MODEL_SIMPLE:
            depth_scan = self.depth_scanner.scan(
                sources,
                expected_outcomes=[outcome.value for outcome in expected_outcomes],
                capital_limit=capital_limit_gbp,
                venue_capital_limits=venue_capital_limits,
                configured_slippage_bps=slip,
            )
            rejections.extend(depth_scan.cost_rejection_reasons)
            solution = depth_scan.solution
            if not solution.is_arbitrage:
                rejections.append(solution.rejection_reason or "no_arbitrage")
            elif solution.roi < minimum_net_edge:
                rejections.append("net_edge_below_threshold")
        else:
            state_model = generalized_state_model_for_pair(left.market, right.market)
            assert state_model is not None
            payoff_scan = self.payoff_scanner.scan(
                sources,
                state_model=state_model,
                capital_limit=capital_limit_gbp,
                venue_capital_limits=venue_capital_limits,
                configured_slippage_bps=slip,
            )
            rejections.extend(payoff_scan.cost_rejection_reasons)
            payoff = payoff_scan.solution
            if not payoff.is_arbitrage:
                rejections.append(payoff.rejection_reason or "no_arbitrage")
            elif payoff.roi < minimum_net_edge:
                rejections.append("net_edge_below_threshold")

        liquidity_rejections = opening_liquidity_rejection_reasons(
            list(scan_costs.values()),
            quote_age_ms=quote_age_ms,
            max_quote_age_ms=int(self.settings.paper_entry_max_quote_age_ms),
        )
        rejections.extend(liquidity_rejections)

        risk_inputs = None
        risk = None
        if not liquidity_rejections:
            risk_inputs = self._risk_inputs(
                left,
                right,
                depth_scan=depth_scan,
                payoff_scan=payoff_scan,
                assumed_latency_ms=assumed_latency_ms,
                recent_volatility_bps=recent_volatility_bps,
                quote_age_ms=quote_age_ms if quote_age_ms is not None else 10**9,
            )
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
            payoff_scan=payoff_scan,
            effective_fx=effective_fx,
        )
        from sports_hedge.catalogue.states import CatalogueApprovalState

        if (
            catalogue_admission.assessment.state
            is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
        ):
            rejections.append("paper_assumed_equivalent")

        draft = PaperScanDecision(
            market_match=match,
            canonical_event_id=event_id,
            canonical_market_id=market_id,
            fixture_canonical_event_id=fixture_id,
            snapshots_recorded=recorded,
            depth_scan=depth_scan,
            payoff_scan=payoff_scan,
            execution_risk=risk,
            execution_risk_inputs=risk_inputs,
            eligible_for_paper_simulation=not _paper_blocking_reasons(rejections),
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
            solver_model=solver_model,
        )
        trades = open_trades if open_trades is not None else self.open_trades
        allocation, alloc_reasons = self._allocate_draft(
            draft,
            left=left,
            standing=standing,
            effective_fx=effective_fx,
            recent_volatility_bps=recent_volatility_bps,
            open_trades=trades,
            conditionally_releasable=conditionally_releasable,
        )
        if allocation is not None:
            draft = draft.model_copy(update={"allocation": allocation})
            if allocation.accepted:
                try:
                    draft = draft.model_copy(
                        update={"fill_legs": apply_allocation_to_fill_legs(fill_legs, allocation)}
                    )
                except FillPlanMappingError as exc:
                    alloc_reasons.append(f"allocation_failed:{exc.reason}")
        rejections.extend(alloc_reasons)
        mapping_review_candidate = mapping_review_evidence()
        self._stamp_scan_phases(
            mapping_ms=mapping_ms,
            fee_started=fee_started,
            solver_started=solver_started,
        )
        return draft.model_copy(
            update={
                "eligible_for_paper_simulation": not _paper_blocking_reasons(rejections),
                "rejection_reasons": _dedupe(rejections),
                "mapping_review_candidate": mapping_review_candidate,
                "scanned_at": datetime.now(UTC),
            }
        )

    def _allocate_draft(
        self,
        draft: PaperScanDecision,
        *,
        left: VenueMarketObservation,
        standing: PaperLiquiditySnapshot | None,
        effective_fx: dict[str, Decimal],
        recent_volatility_bps: float,
        open_trades: list[PaperTrade] | None,
        conditionally_releasable: dict | None,
    ) -> tuple[AllocationResult | None, list[str]]:
        arb = (draft.depth_scan is not None and draft.depth_scan.solution.is_arbitrage) or (
            draft.payoff_scan is not None and draft.payoff_scan.solution.is_arbitrage
        )
        if not arb or standing is None:
            return None, []
        policy = policy_from_settings(self.settings)
        balances = balances_from_liquidity(
            standing,
            gbp_per_unit=effective_fx,
            conditionally_releasable=conditionally_releasable,
        )
        estimate = estimated_time_to_release(
            left.market.event.kickoff_utc,
            draft.scanned_at,
            market=left.market,
            policy=policy,
        )
        request = request_from_paper_decision(
            draft,
            policy=policy,
            balances=balances,
            open_positions=exposures_from_trades(open_trades or []),
            expected_lock_duration_hours=estimate.hours if estimate else None,
            expected_lock_basis=estimate.estimate_basis if estimate else None,
            recent_volatility_bps=(
                Decimal(str(recent_volatility_bps))
                if recent_volatility_bps is not None
                else None
            ),
        )
        if request is None:
            return None, ["allocation_failed:unsupported_solver_vector"]
        result = allocate(request)
        if not result.accepted:
            reason = result.rejection_reason or (
                result.limiting_constraint.value if result.limiting_constraint else "allocation_failed"
            )
            return result, [f"allocation_failed:{reason}"]
        return result, []

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
        if self.reverse_catalog is not None:
            self.reverse_catalog.remember([observation])
        return len(snapshots)

    def _risk_inputs(
        self,
        left: VenueMarketObservation,
        right: VenueMarketObservation,
        *,
        depth_scan,
        payoff_scan=None,
        assumed_latency_ms: int,
        recent_volatility_bps: float,
        quote_age_ms: int,
    ) -> ExecutionRiskInputs | None:
        selected_quotes = []
        stake_by_key: dict[tuple[str, VenueName], Decimal] = {}
        if depth_scan is not None:
            selected_quotes = list(depth_scan.selected_quotes)
            for stake in depth_scan.solution.stakes:
                stake_by_key[(stake.outcome, stake.venue)] = stake.stake
        elif payoff_scan is not None:
            selected_quotes = list(payoff_scan.selected_quotes)
            for stake in payoff_scan.solution.selected_stakes:
                if stake.stake <= 0:
                    continue
                stake_by_key[_payoff_leg_key(stake)] = (
                    stake_by_key.get(_payoff_leg_key(stake), Decimal("0")) + stake.stake
                )
        if depth_scan is not None and len(selected_quotes) < 2:
            return None

        observation_by_venue = {left.venue: left, right.venue: right}
        spread_bps = 0.0
        size_to_depth_ratio = 0.0
        hedge_liquidity_ratio = 1.0
        used = 0

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
            if depth_scan is not None:
                stake_amount = stake_by_key.get((selected.outcome, selected.venue))
                if stake_amount is None or stake_amount <= 0 or selected.cumulative_depth <= 0:
                    return None
            else:
                stake_amount = stake_by_key.get(_quote_leg_key(selected))
                if stake_amount is None or stake_amount <= 0 or selected.cumulative_depth <= 0:
                    continue
            used += 1
            ratio = float(stake_amount / selected.cumulative_depth)
            size_to_depth_ratio = max(size_to_depth_ratio, ratio)
            hedge_liquidity_ratio = min(
                hedge_liquidity_ratio,
                min(float(selected.cumulative_depth / stake_amount), 1.0),
            )

        if used < 2:
            return None

        observed_at = max(left.observed_at, right.observed_at)
        minutes_to_kickoff = max(
            0.0,
            (left.market.event.kickoff_utc - observed_at).total_seconds() / 60.0,
        )
        measured_latency_ms = max(left.source_latency_ms, right.source_latency_ms)
        return ExecutionRiskInputs(
            spread_bps=spread_bps,
            size_to_depth_ratio=size_to_depth_ratio,
            quote_age_ms=quote_age_ms,
            recent_volatility_bps=recent_volatility_bps,
            leg_count=used,
            minutes_to_kickoff=minutes_to_kickoff,
            assumed_latency_ms=max(assumed_latency_ms, measured_latency_ms),
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
        resolved: list[VenueCostSnapshot] = []
        reasons: list[str] = []
        for observation in (left, right):
            action = (
                MarketAction.BUY
                if observation.venue in {VenueName.POLYMARKET, VenueName.KALSHI}
                else MarketAction.BACK
            )
            if observation.venue is VenueName.KALSHI:
                metadata = observation.metadata if isinstance(observation.metadata, dict) else {}
                fee_meta = metadata.get("kalshi_fee")
                if isinstance(fee_meta, dict):
                    from sports_hedge.fees.kalshi import kalshi_cost_from_series

                    snapshot = kalshi_cost_from_series(
                        fee_meta,
                        captured_at=as_of,
                        source_market_id=observation.market.source_market_id,
                    )
                    resolved.append(snapshot)
                    if not snapshot.is_economically_known():
                        reasons.append("unknown_required_venue_cost:kalshi")
                    continue
                reasons.append("unknown_required_venue_cost:kalshi")
                continue
            if observation.venue is VenueName.POLYMARKET:
                metadata = observation.metadata if isinstance(observation.metadata, dict) else {}
                fee_meta = metadata.get("polymarket_fee")
                if isinstance(fee_meta, dict):
                    from sports_hedge.fees.polymarket import polymarket_cost_from_market

                    snapshot = polymarket_cost_from_market(
                        fee_meta,
                        action=action,
                        captured_at=as_of,
                        source_market_id=observation.market.source_market_id,
                    )
                    resolved.append(snapshot)
                    if not snapshot.is_economically_known():
                        reasons.append("unknown_required_venue_cost:polymarket")
                    continue
                reasons.append("unknown_required_venue_cost:polymarket")
                continue
            if self.cost_resolver is None:
                continue
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
        if fx_snapshots:
            return _with_gbp_rate(list(fx_snapshots), as_of=as_of), []
        if self.fx_service is None:
            return _with_gbp_rate([], as_of=as_of), []
        currencies = {left.native_currency, right.native_currency, "GBP"}
        try:
            snapshots = self.fx_service.paper_snapshots(currencies, as_of=as_of)
        except FxRateUnavailable as exc:
            return _with_gbp_rate([], as_of=as_of), [exc.reason]
        if any(item.source == "paper_demo_fx_snapshot" for item in snapshots):
            return _with_gbp_rate([], as_of=as_of), ["missing_fx_rate:USD"]
        return snapshots, []


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


def _paper_blocking_reasons(reasons: list[str]) -> list[str]:
    """Audit labels such as paper_assumed_equivalent do not block PAPER admission."""

    return [reason for reason in reasons if reason not in PAPER_NONBLOCKING_REJECTION_REASONS]


def _default_execution_mode(venue: VenueName) -> LegExecutionMode:
    if venue is VenueName.POLYMARKET:
        return LegExecutionMode.EXTERNAL_OPERATOR
    return LegExecutionMode.INTERNAL


def _fill_legs_from_observations(
    left,
    right,
    *,
    depth_scan,
    payoff_scan=None,
    effective_fx: dict[str, Decimal],
) -> list[PaperOpportunityLeg]:
    selected: set[tuple[str, VenueName]] = set()
    stakes: dict[tuple[str, VenueName], Decimal] = {}
    if depth_scan is not None:
        selected = {(quote.outcome, quote.venue) for quote in depth_scan.selected_quotes}
        for stake in depth_scan.solution.stakes:
            stakes[(stake.outcome, stake.venue)] = stake.stake
    elif payoff_scan is not None:
        for stake in payoff_scan.solution.selected_stakes:
            if stake.stake <= 0:
                continue
            key = _payoff_leg_key(stake)
            selected.add(key)
            stakes[key] = stakes.get(key, Decimal("0")) + stake.stake
    legs: list[PaperOpportunityLeg] = []
    for observation in (left, right):
        rate = effective_fx.get(observation.native_currency)
        if rate is None or rate <= 0:
            continue
        for book in observation.outcome_books:
            outcome = book.outcome.value
            if depth_scan is not None:
                select_key: tuple = (outcome, observation.venue)
            else:
                select_key = (
                    outcome,
                    observation.venue,
                    observation.market.source_market_id,
                    book.source_runner_id,
                )
            if select_key not in selected or not book.back_levels:
                continue
            best = max(book.back_levels, key=lambda item: item.decimal_odds)
            stake_amount = stakes.get(select_key)
            visible = sum((level.available_stake for level in book.back_levels), Decimal("0"))
            if stake_amount is None or stake_amount <= 0:
                if depth_scan is None:
                    continue
                requested = visible
            else:
                requested = stake_amount / rate
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


class FillPlanMappingError(ValueError):
    """Allocator stakes cannot be mapped 1:1 onto the paper fill plan."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _fill_map_identity(
    *,
    venue: VenueName,
    source_market_id: str,
    source_runner_id: str | None,
    outcome: str,
) -> tuple[VenueName, str, str, str]:
    return (venue, source_market_id, source_runner_id or "", outcome)


def apply_allocation_to_fill_legs(
    legs: list[PaperOpportunityLeg], allocation: AllocationResult
) -> list[PaperOpportunityLeg]:
    """Resize every positive planned fill leg from allocated native stakes.

    Mapping is exact one-to-one on venue + source market + source runner
    (when present) + outcome. Missing or duplicate identities fail closed.
    """

    positive_legs = [leg for leg in legs if leg.requested_stake > 0]
    positive_stakes = [stake for stake in allocation.recommended_stakes if stake.stake_native > 0]
    fill_by_id: dict[tuple[VenueName, str, str, str], PaperOpportunityLeg] = {}
    for leg in positive_legs:
        key = _fill_map_identity(
            venue=leg.venue,
            source_market_id=leg.source_market_id,
            source_runner_id=leg.source_runner_id,
            outcome=leg.outcome,
        )
        if key in fill_by_id:
            raise FillPlanMappingError("fill_plan_mapping_duplicate_identity")
        fill_by_id[key] = leg
    stake_by_id: dict[tuple[VenueName, str, str, str], AllocatedStake] = {}
    for stake in positive_stakes:
        key = _fill_map_identity(
            venue=stake.venue,
            source_market_id=stake.source_market_id,
            source_runner_id=stake.source_runner_id,
            outcome=stake.outcome,
        )
        if key in stake_by_id:
            raise FillPlanMappingError("fill_plan_mapping_duplicate_identity")
        stake_by_id[key] = stake
    if fill_by_id.keys() != stake_by_id.keys():
        raise FillPlanMappingError("fill_plan_mapping_unmatched_leg")
    resized: list[PaperOpportunityLeg] = []
    for leg in positive_legs:
        key = _fill_map_identity(
            venue=leg.venue,
            source_market_id=leg.source_market_id,
            source_runner_id=leg.source_runner_id,
            outcome=leg.outcome,
        )
        match = stake_by_id[key]
        resized.append(leg.model_copy(update={"requested_stake": match.stake_native}))
    return resized


def _payoff_leg_key(stake) -> tuple[str, VenueName, str, str]:
    return (
        stake.runner_outcome or "",
        stake.venue,
        stake.source_market_id,
        stake.source_runner_id or "",
    )


def _quote_leg_key(quote) -> tuple[str, VenueName, str, str]:
    return (quote.outcome, quote.venue, quote.source_market_id, quote.source_runner_id)


def _action_mismatch(venue: VenueName, action: MarketAction) -> str | None:
    if venue in {VenueName.POLYMARKET, VenueName.KALSHI} and action is not MarketAction.BUY:
        return f"unsupported_action:{venue.value}"
    if venue in {VenueName.MATCHBOOK, VenueName.SMARKETS} and action is not MarketAction.BACK:
        return f"unsupported_action:{venue.value}"
    return None
