from __future__ import annotations

import threading
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

from sports_hedge.accounting.dimensions import CapitalSource
from sports_hedge.accounting.paper_journal import (
    DataProvenance,
    DuplicateJournalError,
    PaperJournal,
    PaperJournalEntry,
    cash_lock_postings,
    gbp_is_balanced,
    settlement_leg_postings,
)
from sports_hedge.accounting.strategy_books import DimensionedPosting
from sports_hedge.application.executable_liquidity import decision_net_edge
from sports_hedge.application.paper_scan import FillPlanMappingError, apply_allocation_to_fill_legs
from sports_hedge.arbitrage.allocation.adapters import (
    balances_from_treasury,
    exposures_from_trades,
    request_from_paper_decision,
)
from sports_hedge.arbitrage.allocation.engine import allocate, allocate_requested_size
from sports_hedge.arbitrage.allocation.policy import policy_from_settings
from sports_hedge.arbitrage.priority_alerts.fixed_exposure import revalidate_fixed_external_exposure
from sports_hedge.arbitrage.priority_alerts.models import (
    ExternalLegConfirmation,
    LegExecutionMode,
    PriorityAlertCandidate,
    PriorityLeg,
)
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.watchlist.models import NearOpportunity, OpportunityStatus
from sports_hedge.arbitrage.watchlist.service import WatchlistService, _opportunity_id
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import MarketAction
from sports_hedge.fees.effective import CostRuleError, apply_venue_costs
from sports_hedge.paper.bet_ticket import (
    BetTicketExecutionSeam,
    BetTicketFxAssumption,
    BetTicketSurvivability,
    BetTicketTreasuryRemaining,
    RecommendedPaperDeployment,
    bet_ticket_action,
    current_bet_deployability,
)
from sports_hedge.paper.chain import (
    PaperChainStep,
    PaperChainTrace,
    PaperFillPlan,
    SimulatePaperFillResult,
)
from sports_hedge.paper.fills import (
    PaperFillConfig,
    PaperFillRecord,
    PaperOpportunityFills,
    PaperOpportunityLeg,
)
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision
from sports_hedge.paper.preparation import (
    PreparablePaperOpportunity,
    PreparedPaperDeployment,
    PreparedPaperLeg,
    preparation_capital_source,
)
from sports_hedge.paper.risk_snapshot import (
    PaperRiskSnapshotKind,
    snapshot_from_execution_risk,
    snapshot_from_scan_decision,
)
from sports_hedge.paper.settlement import PaperSettlementError, compute_paper_settlement
from sports_hedge.paper.simulator import PaperFillSimulator
from sports_hedge.paper.trades import (
    PAPER_UNWIND_SOURCE,
    PaperLegFillKind,
    PaperSettlementRequest,
    PaperTrade,
    PaperTradeAuditEvent,
    PaperTradeAuditEventType,
    PaperTradeBookSummary,
    PaperTradeDetail,
    PaperTradeLeg,
    PaperTradeState,
    paper_unwind_source_id,
)
from sports_hedge.paper.unwind import (
    PaperUnwindEngine,
    UnwindDecision,
    UnwindEvaluationRequest,
    UnwindIdentityError,
    close_fills_from_decision,
    position_from_trade,
)
from sports_hedge.paper.unwind.models import (
    CapitalScarcityInput,
    RemainingLockSource,
    ReverseQuote,
    UnwindPolicy,
    UnwindRecommendation,
)
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger, SqlitePaperTradeRepository
from sports_hedge.treasury.models import (
    TreasuryLockRequest,
    UnwindReleaseLeg,
    ValidatedUnwindResult,
)
from sports_hedge.treasury.service import PaperTreasuryError


def paper_trade_id(opportunity_id: str) -> str:
    """Path-safe trade id. Colons break Next.js / FastAPI path segments."""

    slug = opportunity_id.replace(":", "-").replace("/", "-")
    return f"ptrade-{slug}"


def manual_external_fill_id(opportunity_id: str, operator_counterparty_reference: str) -> str:
    """Stable fill/lock identity for an operator-recorded MANUAL_EXTERNAL leg.

    The operator/counterparty reference stays in the id so it remains auditable.
    Trade persistence and treasury locking must use this exact string.
    """

    return f"manual-external:{opportunity_id}:{operator_counterparty_reference}"


class PaperOperationsError(ValueError):
    """Fail-closed paper operational chain."""


_AUTOFILL_GATE_REASONS = frozenset(
    {
        "allocator_size_required",
        "fill_plan_not_allocator_sized",
        "stale_before_fill",
        "stale_quote",
        "missing_paper_fill_plan",
        "no_positive_opening_legs",
        "no_internal_paper_legs",
        "paper_fill_rejected",
        "incomplete_opening_hedge",
        "insufficient_spendable_treasury",
        "missing_treasury_pool",
        "manual_external_confirmation_required",
        "external_confirmation_venue_mismatch",
        "remaining_hedge_revalidation_failed",
        "unknown_opportunity",
    }
)


def _is_expected_autofill_gate(exc: BaseException) -> bool:
    """True for fail-closed non-captures; false for persist/crash inconsistency."""

    reason = str(exc)
    if reason.startswith("inconsistent_paper_state"):
        return False
    if reason in _AUTOFILL_GATE_REASONS:
        return True
    return reason.startswith("allocation_failed:")


class PaperOperationsService:
    """Wire scan → optional autofill/priority alert → paper fill → journal → settlement.

    Never places venue orders, signs wallets, or treats MANUAL_EXTERNAL as automated.
    PAPER_SIMULATED_EXTERNAL is a paper-only stand-in for a future external leg.
    """

    def __init__(
        self,
        *,
        watchlist: WatchlistService,
        alerts: PriorityAlertService | None = None,
        journal: PaperJournal | None = None,
        simulator: PaperFillSimulator | None = None,
        settings: Settings | None = None,
        ledger: SqlitePaperLedger | None = None,
        trades: SqlitePaperTradeRepository | None = None,
    ) -> None:
        self.watchlist = watchlist
        self.alerts = alerts or PriorityAlertService()
        self.simulator = simulator or PaperFillSimulator()
        self.settings = settings or get_settings()
        self.ledger = ledger
        if ledger is not None:
            self.journal = journal or ledger.journal
            self.trades = trades or ledger.trades
        else:
            self.journal = journal or PaperJournal()
            self.trades = trades
        self._plans: dict[str, PaperFillPlan] = {}
        self._preparations: dict[str, PreparedPaperDeployment] = {}
        self._latest_preparation_by_opportunity: dict[str, str] = {}
        self._external_confirmations: dict[str, ExternalLegConfirmation] = {}
        self._entry_rejections: dict[str, str] = {}
        self._fill_persist_lock = threading.RLock()

    def persist_triggered_chain(
        self,
        decision: PaperScanDecision,
        *,
        provenance: DataProvenance = DataProvenance.LIVE_PAPER,
        autofill: bool | None = None,
        refreshed_venues: list[VenueName] | tuple[VenueName, ...] | None = None,
    ) -> PriorityAlertCandidate | None:
        if not decision.canonical_market_id:
            return None
        opportunity_id = _opportunity_id(decision.canonical_market_id)
        opening_legs = [leg for leg in decision.fill_legs if leg.requested_stake > 0]
        if opening_legs:
            self._plans[opportunity_id] = PaperFillPlan(
                opportunity_id=opportunity_id,
                canonical_event_id=decision.canonical_event_id,
                canonical_market_id=decision.canonical_market_id,
                scanned_at=decision.scanned_at,
                quote_age_ms=decision.quote_age_ms,
                eligible_for_paper_simulation=decision.eligible_for_paper_simulation,
                settlement_equivalent=decision.market_match.matched,
                legs=opening_legs,
                execution_modes={
                    venue: LegExecutionMode(mode)
                    for venue, mode in decision.execution_modes.items()
                },
                venue_costs=list(decision.venue_costs),
                fx_snapshots=list(decision.fx_snapshots),
                decision=decision,
                provenance=provenance,
            )
        candidate = None
        solver_arb = _solver_is_arbitrage(decision)
        if decision.eligible_for_paper_simulation and solver_arb:
            if decision.depth_scan is not None and decision.depth_scan.solution.is_arbitrage:
                candidate = _candidate_from_decision(decision, opportunity_id)
                self.alerts.ingest(candidate)
            if self._should_autofill(autofill=autofill, provenance=provenance) and self._venues_refreshed_this_cycle(
                opening_legs, refreshed_venues
            ):
                try:
                    self._require_allocator_sized_plan(opportunity_id)
                except PaperOperationsError:
                    # Allocator/plan gates are fail-closed non-captures, not persist crashes.
                    pass
                else:
                    try:
                        self.simulate_fill(
                            opportunity_id,
                            simulate_external=True,
                            provenance=provenance,
                            operator_note="PAPER-ONLY autofill; no venue order placed",
                        )
                    except PaperOperationsError as exc:
                        if _is_expected_autofill_gate(exc):
                            pass
                        else:
                            raise
        return candidate

    def _should_autofill(
        self,
        *,
        autofill: bool | None,
        provenance: DataProvenance,
    ) -> bool:
        """Inherit the global paper-autofill flag only for LIVE_PAPER.

        Explicit True remains an operator/test override. Explicit False always
        wins. Fixture replay must pass False (or omit inherit) so a labelled
        DEMO / FIXTURE REPLAY cannot masquerade as live auto-capture.
        """

        if autofill is False:
            return False
        if autofill is True:
            return True
        return (
            self.settings.paper_autofill_enabled
            and provenance is DataProvenance.LIVE_PAPER
        )

    def _venues_refreshed_this_cycle(
        self,
        legs: list,
        refreshed_venues: list[VenueName] | tuple[VenueName, ...] | None,
    ) -> bool:
        """Stale/disabled-venue legs are never executable-fresh auto-capture truth."""

        if refreshed_venues is None:
            return True
        allowed = frozenset(refreshed_venues)
        return all(getattr(leg, "venue", None) in allowed for leg in legs)

    def list_preparable(self, canonical_event_id: str | None = None) -> list[PreparablePaperOpportunity]:
        rows: list[PreparablePaperOpportunity] = []
        for plan in self._plans.values():
            if canonical_event_id is not None and plan.canonical_event_id != canonical_event_id:
                continue
            watch = self.watchlist.repository.get(plan.opportunity_id)
            semantic, blocked = bet_ticket_action(
                plan=plan,
                watch=watch,
                solver_is_arbitrage=_solver_is_arbitrage(plan.decision),
            )
            recommended = None
            maximum = None
            actionable = False
            if semantic:
                try:
                    rec = self._recommend_from_plan(plan, watch)
                    actionable = rec.bet_actionable
                    blocked = rec.bet_blocked_reason
                    if rec.accepted:
                        recommended = rec.recommended_size_gbp
                    maximum = rec.maximum_validated_size_gbp
                except PaperOperationsError as exc:
                    actionable = False
                    blocked = str(exc)
            market, settlement = _ticket_market_labels(plan, watch)
            rows.append(
                PreparablePaperOpportunity(
                    opportunity_id=plan.opportunity_id,
                    canonical_event_id=plan.canonical_event_id,
                    canonical_market_id=plan.canonical_market_id,
                    solver_model=plan.decision.solver_model,
                    eligible_for_paper_simulation=plan.eligible_for_paper_simulation,
                    settlement_equivalent=plan.settlement_equivalent,
                    bet_actionable=actionable,
                    bet_blocked_reason=blocked,
                    recommended_size_gbp=recommended,
                    maximum_validated_size_gbp=maximum,
                    market_label=market,
                    settlement_definition=settlement,
                )
            )
        return rows

    def annotate_bet_ticket_actions(
        self, items: list[NearOpportunity]
    ) -> list[NearOpportunity]:
        """Attach BET actionability at read time. Does not persist, lock, or OPEN."""

        annotated: list[NearOpportunity] = []
        for item in items:
            plan = self._plans.get(item.opportunity_id)
            semantic, blocked = bet_ticket_action(
                plan=plan,
                watch=item,
                solver_is_arbitrage=_solver_is_arbitrage(plan.decision) if plan is not None else False,
            )
            if not semantic or plan is None:
                annotated.append(
                    item.model_copy(
                        update={
                            "bet_actionable": False,
                            "bet_blocked_reason": blocked,
                        }
                    )
                )
                continue
            try:
                rec = self._recommend_from_plan(plan, item)
                annotated.append(
                    item.model_copy(
                        update={
                            "bet_actionable": rec.bet_actionable,
                            "bet_blocked_reason": rec.bet_blocked_reason,
                        }
                    )
                )
            except PaperOperationsError as exc:
                annotated.append(
                    item.model_copy(
                        update={
                            "bet_actionable": False,
                            "bet_blocked_reason": str(exc),
                        }
                    )
                )
        return annotated

    def recommend_paper_deployment(self, opportunity_id: str) -> RecommendedPaperDeployment:
        """Allocator recommended GBP size. No treasury mutation, no OPEN, no stored preview."""

        plan = self._plans.get(opportunity_id)
        if plan is None:
            raise PaperOperationsError("missing_paper_fill_plan")
        watch = self.watchlist.repository.get(opportunity_id)
        return self._recommend_from_plan(plan, watch)

    def _recommend_from_plan(
        self, plan: PaperFillPlan, watch: NearOpportunity | None
    ) -> RecommendedPaperDeployment:
        opportunity_id = plan.opportunity_id
        semantic, blocked = bet_ticket_action(
            plan=plan,
            watch=watch,
            solver_is_arbitrage=_solver_is_arbitrage(plan.decision),
        )
        if not semantic:
            return RecommendedPaperDeployment(
                opportunity_id=opportunity_id,
                accepted=False,
                bet_actionable=False,
                bet_blocked_reason=blocked,
            )
        request, rejected = self._allocation_request_or_reject(plan)
        if request is None:
            return RecommendedPaperDeployment(
                opportunity_id=opportunity_id,
                accepted=False,
                bet_actionable=False,
                bet_blocked_reason=rejected,
            )
        baseline = allocate(request)
        deployable, reason = current_bet_deployability(
            semantically_qualified=True,
            semantic_blocked_reason=None,
            allocation_accepted=baseline.accepted,
            recommended_size=baseline.recommended_size,
            allocation_rejection_reason=baseline.rejection_reason,
            limiting_constraint_detail=baseline.limiting_constraint_detail,
        )
        return RecommendedPaperDeployment(
            opportunity_id=opportunity_id,
            accepted=deployable,
            bet_actionable=deployable,
            bet_blocked_reason=reason,
            recommended_size_gbp=baseline.recommended_size if deployable else Decimal(0),
            maximum_validated_size_gbp=baseline.maximum_validated_size,
            limiting_constraint=baseline.limiting_constraint,
            limiting_constraint_detail=baseline.limiting_constraint_detail,
            reduction_factors=[factor.name for factor in baseline.reduction_factors],
        )

    def prepare_fixed_deployment(
        self,
        opportunity_id: str,
        requested_size_gbp: Decimal,
        *,
        operator_note: str = "PAPER-ONLY fixed-size preparation; does not OPEN or lock",
    ) -> PreparedPaperDeployment:
        """Scale a qualified opportunity to an operator-chosen GBP size.

        Does not lock treasury, simulate fills, or OPEN a trade.
        """

        if requested_size_gbp <= 0:
            raise PaperOperationsError("requested_size_must_be_positive")
        preview = self._compute_prepared_deployment(
            opportunity_id,
            requested_size_gbp,
            operator_note=operator_note,
        )
        if preview.accepted:
            preview = preview.model_copy(
                update={"prepared_deployment_id": f"pdep:{uuid4().hex}"}
            )
            assert preview.prepared_deployment_id is not None
            self._preparations[preview.prepared_deployment_id] = preview
            self._latest_preparation_by_opportunity[opportunity_id] = (
                preview.prepared_deployment_id
            )
        return preview

    def _allocation_request_or_reject(
        self, plan: PaperFillPlan
    ) -> tuple[object | None, str | None]:
        decision = plan.decision
        if not plan.settlement_equivalent or not decision.market_match.matched:
            return None, "market_not_equivalent"
        if not _solver_is_arbitrage(decision):
            return None, "solver_not_arbitrage"
        blockers = [
            reason
            for reason in decision.rejection_reasons
            if not reason.startswith("allocation_failed")
        ]
        if blockers:
            return None, blockers[0]
        if self.ledger is None:
            raise PaperOperationsError("missing_spendable_treasury")
        snap = self.ledger.treasury.snapshot()
        fx = {item.currency.upper(): item.gbp_per_unit for item in decision.fx_snapshots}
        fx.setdefault("GBP", Decimal(1))
        balances = balances_from_treasury(snap, gbp_per_unit=fx)
        if any(row.gbp_per_unit is None for row in balances if row.currency != "GBP"):
            return None, "missing_fx_rate"
        request = request_from_paper_decision(
            decision,
            policy=policy_from_settings(self.settings),
            balances=balances,
            open_positions=exposures_from_trades(self.list_active_trades()),
        )
        if request is None:
            return None, "allocation_failed:unsupported_solver_vector"
        return request, None

    def _compute_prepared_deployment(
        self,
        opportunity_id: str,
        requested_size_gbp: Decimal,
        *,
        operator_note: str,
    ) -> PreparedPaperDeployment:
        plan = self._plans.get(opportunity_id)
        if plan is None:
            raise PaperOperationsError("missing_paper_fill_plan")
        request, reject_reason = self._allocation_request_or_reject(plan)
        if request is None:
            return self._rejected_deployment(
                opportunity_id,
                requested_size_gbp,
                reject_reason or "not_preparable",
                plan=plan,
                operator_note=operator_note,
            )
        baseline = allocate(request)
        result = allocate_requested_size(request, requested_size_gbp)
        decision = plan.decision
        snap = self.ledger.treasury.snapshot() if self.ledger is not None else None
        recommended = baseline.recommended_size if baseline.accepted else Decimal(0)
        legs = self._prepared_legs(plan, result.recommended_stakes)
        required = [
            item.model_copy(
                update={
                    "capital_source": preparation_capital_source(
                        plan.execution_modes.get(
                            item.venue, LegExecutionMode.INTERNAL
                        ).value,
                        item.capital_source,
                    )
                }
            )
            for item in result.capital_required
        ]
        reconciled = False
        if result.accepted and snap is not None:
            reporting_sum = sum((leg.capital_reporting for leg in legs), Decimal(0))
            # Scaling native legs to GBP can leave sub-tick dust (observed ~1e-27).
            # That is not a resize; a true mismatch is pounds or cents, not dust.
            reconciled = abs(reporting_sum - result.recommended_committed_capital) <= Decimal(
                "0.00000001"
            )
            for item in required:
                try:
                    pool = snap.pool(item.venue, item.currency)
                except KeyError:
                    reconciled = False
                    break
                if pool.available_cash < item.amount:
                    reconciled = False
                    break
            if not reconciled:
                return self._rejected_deployment(
                    opportunity_id,
                    requested_size_gbp,
                    "native_requirements_not_reconciled",
                    plan=plan,
                    operator_note=operator_note,
                    maximum=result.maximum_validated_size,
                    recommended=recommended,
                    constraint=result.limiting_constraint,
                    detail=result.limiting_constraint_detail,
                )
        ticket = self._ticket_fields(plan, result)
        return PreparedPaperDeployment(
            opportunity_id=opportunity_id,
            accepted=result.accepted and reconciled,
            requested_size_gbp=requested_size_gbp,
            operator_entered_size_gbp=requested_size_gbp,
            recommended_size_gbp=recommended,
            applied_size_gbp=result.recommended_committed_capital if result.accepted else Decimal(0),
            maximum_validated_size_gbp=result.maximum_validated_size,
            resized=False,
            rejection_reason=None if result.accepted else result.rejection_reason,
            limiting_constraint=result.limiting_constraint,
            limiting_constraint_detail=result.limiting_constraint_detail,
            legs=legs if result.accepted else [],
            capital_required=required if result.accepted else [],
            native_requirements_reconciled=reconciled,
            guaranteed_profit_gbp=result.guaranteed_profit if result.accepted else Decimal(0),
            guaranteed_roi=result.guaranteed_roi,
            solver_model=decision.solver_model,
            settlement_equivalent=plan.settlement_equivalent,
            operator_note=operator_note,
            **ticket,
        )

    def _prepared_legs(self, plan: PaperFillPlan, stakes) -> list[PreparedPaperLeg]:
        fill_by_id: dict[tuple, PaperOpportunityLeg] = {}
        for leg in plan.legs:
            key = (leg.venue, leg.source_market_id, leg.source_runner_id or "", leg.outcome)
            fill_by_id[key] = leg
        cost_by_venue = {cost.venue: cost for cost in plan.venue_costs}
        prepared: list[PreparedPaperLeg] = []
        for stake in stakes:
            if stake.stake_native <= 0:
                continue
            key = (stake.venue, stake.source_market_id, stake.source_runner_id or "", stake.outcome)
            fill = fill_by_id.get(key)
            odds = fill.displayed_odds if fill is not None else None
            fee = None
            net_payoff = None
            fee_basis = None
            cost_status = "modelled"
            cost = cost_by_venue.get(stake.venue)
            if cost is not None and odds is not None:
                try:
                    economics = apply_venue_costs(
                        cost,
                        gross_decimal_odds=odds,
                        stake=stake.stake_native,
                        require_gbp=False,
                    )
                    fee = economics.venue_fee
                    net_payoff = economics.net_payoff
                    fee_basis = economics.fee_basis.value
                except CostRuleError as exc:
                    cost_status = exc.reason
                    fee_basis = cost.fee_basis.value
            elif cost is None:
                cost_status = "missing_venue_cost"
            depth_native = None
            depth_pct = None
            if fill is not None and fill.levels:
                depth_native = sum((level.available_stake for level in fill.levels), Decimal(0))
                if depth_native > 0:
                    depth_pct = (stake.stake_native / depth_native) * Decimal(100)
            fx_rate = None
            fx_source = None
            for snap in plan.fx_snapshots:
                if snap.currency.upper() == stake.native_currency.upper():
                    fx_rate = snap.gbp_per_unit
                    fx_source = snap.source
                    break
            if stake.native_currency.upper() == "GBP":
                fx_rate = Decimal(1)
                fx_source = fx_source or "functional_gbp"
            prepared.append(
                PreparedPaperLeg(
                    venue=stake.venue,
                    native_currency=stake.native_currency,
                    outcome=stake.outcome,
                    source_market_id=stake.source_market_id,
                    source_runner_id=stake.source_runner_id,
                    displayed_odds=odds,
                    stake_native=stake.stake_native,
                    stake_reporting=stake.stake_reporting,
                    capital_native=stake.capital_native,
                    capital_reporting=stake.capital_reporting,
                    venue_fee=fee,
                    net_payoff=net_payoff,
                    fee_basis=fee_basis,
                    cost_status=cost_status,
                    capital_source=preparation_capital_source(
                        stake.execution_mode, stake.capital_source
                    ),
                    execution_mode=stake.execution_mode,
                    action=cost.action.value if cost is not None else None,
                    displayed_depth_native=depth_native,
                    depth_consumed_pct=depth_pct,
                    fx_gbp_per_unit=fx_rate,
                    fx_source=fx_source,
                )
            )
        return prepared

    def _rejected_deployment(
        self,
        opportunity_id: str,
        requested_size_gbp: Decimal,
        reason: str,
        *,
        plan: PaperFillPlan,
        operator_note: str,
        maximum: Decimal | None = None,
        recommended: Decimal | None = None,
        constraint=None,
        detail: str | None = None,
    ) -> PreparedPaperDeployment:
        ticket = self._ticket_fields(plan, None)
        return PreparedPaperDeployment(
            opportunity_id=opportunity_id,
            accepted=False,
            requested_size_gbp=requested_size_gbp,
            operator_entered_size_gbp=requested_size_gbp,
            recommended_size_gbp=recommended or Decimal(0),
            applied_size_gbp=Decimal(0),
            maximum_validated_size_gbp=maximum or Decimal(0),
            rejection_reason=reason,
            limiting_constraint=constraint,
            limiting_constraint_detail=detail or reason,
            solver_model=plan.decision.solver_model,
            settlement_equivalent=plan.settlement_equivalent,
            operator_note=operator_note,
            **ticket,
        )

    def _ticket_fields(self, plan: PaperFillPlan, result) -> dict:
        watch = self.watchlist.repository.get(plan.opportunity_id)
        market, settlement = _ticket_market_labels(plan, watch)
        risk = plan.decision.execution_risk
        survivability = _ticket_survivability(result)
        fx_rows = [
            BetTicketFxAssumption(
                currency=item.currency,
                gbp_per_unit=item.gbp_per_unit,
                source=item.source,
                source_date=item.source_date.isoformat() if item.source_date else None,
                valuation_date=item.valuation_date.isoformat() if item.valuation_date else None,
                check_status=item.check_status,
            )
            for item in plan.fx_snapshots
        ]
        treasury = []
        if result is not None:
            treasury = [
                BetTicketTreasuryRemaining.from_balance(row) for row in result.free_balance_after
            ]
        venues = list(dict.fromkeys(leg.venue.value for leg in plan.legs))
        return {
            "gross_edge": watch.gross_edge if watch is not None else None,
            "net_edge": (
                watch.current_net_edge if watch is not None else decision_net_edge(plan.decision)
            ),
            "market_label": market,
            "settlement_definition": settlement,
            "venue_pair": venues,
            "quote_age_ms": plan.decision.quote_age_ms,
            "quote_age_basis": plan.decision.quote_age_basis,
            "execution_risk_score": risk.score if risk is not None else None,
            "execution_risk_band": risk.band if risk is not None else None,
            "execution_risk_reasons": list(risk.reasons) if risk is not None else [],
            "survivability": survivability,
            "fx_assumptions": fx_rows,
            "treasury_remaining": treasury,
            "execution_seam": BetTicketExecutionSeam(
                execution_enabled=self.settings.sports_hedge_execution_enabled,
            ),
        }

    def _bind_prepared_allocation(
        self,
        opportunity_id: str,
        plan: PaperFillPlan,
        *,
        prepared_deployment_id: str | None,
        requested_size_gbp: Decimal | None,
    ) -> tuple[PaperFillPlan, str | None]:
        """Revalidate an operator-accepted size and bind those exact native legs.

        Does not mutate treasury. Does not trust stale prepared quotes. If current
        economics no longer match the accepted preview, fail closed rather than
        silently resizing.
        """

        stored: PreparedPaperDeployment | None = None
        if prepared_deployment_id is not None:
            stored = self._preparations.get(prepared_deployment_id)
            if stored is None:
                raise PaperOperationsError("unknown_prepared_deployment")
            if stored.opportunity_id != opportunity_id:
                raise PaperOperationsError("prepared_deployment_opportunity_mismatch")
            if not stored.accepted or stored.prepared_deployment_id is None:
                raise PaperOperationsError("prepared_deployment_not_accepted")
            if (
                requested_size_gbp is not None
                and requested_size_gbp != stored.requested_size_gbp
            ):
                raise PaperOperationsError("prepared_deployment_size_mismatch")
            requested_size_gbp = stored.requested_size_gbp
        elif requested_size_gbp is not None:
            latest_id = self._latest_preparation_by_opportunity.get(opportunity_id)
            candidate = self._preparations.get(latest_id) if latest_id else None
            if (
                candidate is not None
                and candidate.accepted
                and candidate.requested_size_gbp == requested_size_gbp
            ):
                stored = candidate
                prepared_deployment_id = candidate.prepared_deployment_id
        else:
            return plan, None

        current = self._compute_prepared_deployment(
            opportunity_id,
            requested_size_gbp,
            operator_note="PAPER-ONLY confirmation revalidation; does not OPEN until fill",
        )
        if not current.accepted:
            raise PaperOperationsError(
                current.rejection_reason or "prepared_deployment_not_accepted"
            )
        if current.applied_size_gbp != requested_size_gbp:
            raise PaperOperationsError("prepared_deployment_stale")
        if stored is not None and _prepared_fingerprint(stored) != _prepared_fingerprint(
            current
        ):
            raise PaperOperationsError("prepared_deployment_stale")
        return (
            _plan_with_prepared_stakes(plan, current),
            stored.prepared_deployment_id if stored is not None else prepared_deployment_id,
        )

    def _note_repeat_observation(self, trade: PaperTrade, when: datetime) -> PaperTrade:
        """Record a later scan/submit against an existing trade without topping up."""

        if self.trades is None:
            return trade
        plan = self._plans.get(trade.opportunity_id)
        current_score = None
        current_edge = None
        if plan is not None:
            snapshot = snapshot_from_scan_decision(
                plan.decision,
                kind=PaperRiskSnapshotKind.ENTRY,
                recorded_at=when,
                opportunity_id=trade.opportunity_id,
                trade_id=trade.trade_id,
            )
            if snapshot is not None:
                current_score = snapshot.score
                current_edge = snapshot.net_edge
        parts = [
            "repeat observation; analysis/risk may update; Phase 1 does not top up an existing position",
        ]
        if current_score is not None:
            parts.append(f"current_score={current_score}")
        if current_edge is not None:
            parts.append(f"current_net_edge={current_edge}")
        trade.audit.append(
            PaperTradeAuditEvent(
                occurred_at=when,
                event_type=PaperTradeAuditEventType.REPEAT_OBSERVATION_NO_TOP_UP,
                detail="; ".join(parts),
            )
        )
        return self.trades.save(trade)

    def _complete_or_repeat_existing(
        self,
        trade: PaperTrade,
        *,
        simulated_at: datetime,
        operator_note: str,
    ) -> SimulatePaperFillResult | None:
        """Complete missing durable side effects for an existing trade, or repeat.

        PENDING/PARTIAL fall through so the opening fill path can finish them.
        """

        if trade.state is PaperTradeState.OPEN:
            self._repair_opening_side_effects(trade, occurred_at=simulated_at)
            self._record_watchlist_fill(
                trade.opportunity_id,
                stage=OpportunityStatus.FILLED,
                occurred_at=simulated_at,
                detail=operator_note,
            )
            if not self._opening_side_effects_complete(trade):
                raise PaperOperationsError("inconsistent_paper_state:incomplete_opening_side_effects")
        noted = self._note_repeat_observation(trade, simulated_at)
        return self._result_from_existing_trade(noted, simulated_at)

    def _lock_source_for_leg(self, leg: PaperTradeLeg) -> tuple[str, CapitalSource]:
        if leg.fill_kind is PaperLegFillKind.PAPER_SIMULATED_EXTERNAL:
            return "paper_simulated_external", CapitalSource.PAPER_SIMULATED_EXTERNAL
        if leg.fill_kind is PaperLegFillKind.MANUAL_EXTERNAL:
            return "manual_external_confirmation", CapitalSource.MANUAL_EXTERNAL
        return "paper_fill_simulator", CapitalSource.AUTO_POOL

    def _lock_requests_from_trade(self, trade: PaperTrade) -> list[TreasuryLockRequest]:
        requests: list[TreasuryLockRequest] = []
        for leg in trade.legs:
            if leg.filled_stake <= 0 or not leg.fill_id:
                continue
            source, capital = self._lock_source_for_leg(leg)
            rate = self._lock_fx_rate(
                leg.venue, leg.currency, {item.currency: item for item in trade.fx_snapshots}
            )
            requests.append(
                TreasuryLockRequest(
                    venue=leg.venue,
                    native_currency=leg.currency,
                    amount_native=leg.filled_stake,
                    lock_id=leg.fill_id,
                    fill_id=leg.fill_id,
                    trade_id=trade.trade_id,
                    opportunity_id=trade.opportunity_id,
                    source=source,
                    reason="PAPER-ONLY capital lock on validated paper fill",
                    fx_rate_gbp_per_unit=rate,
                    capital_source=capital.value,
                )
            )
        return requests

    def _repair_opening_side_effects(self, trade: PaperTrade, *, occurred_at: datetime) -> None:
        if self.ledger is None:
            return
        requests = self._lock_requests_from_trade(trade)
        if not requests:
            if any(leg.filled_stake > 0 for leg in trade.legs):
                raise PaperOperationsError("inconsistent_paper_state:missing_lock_identity")
            return
        try:
            self.ledger.treasury.lock_capital(
                requests, occurred_at=occurred_at, provenance=trade.provenance
            )
        except PaperTreasuryError as exc:
            raise PaperOperationsError(f"inconsistent_paper_state:{exc}") from exc

    def _opening_side_effects_complete(self, trade: PaperTrade) -> bool:
        if trade.state is not PaperTradeState.OPEN:
            return False
        watch = self.watchlist.repository.get(trade.opportunity_id)
        if watch is None or watch.status is not OpportunityStatus.FILLED:
            return False
        if self.ledger is None:
            return True
        for leg in trade.legs:
            if leg.filled_stake <= 0:
                continue
            if not leg.fill_id:
                return False
            lock = self.ledger.treasury._lock_row(leg.fill_id)
            if lock is None:
                return False
            source, _capital = self._lock_source_for_leg(leg)
            if self.journal.get(source, leg.fill_id) is None:
                return False
        return True

    def _record_watchlist_fill(
        self,
        opportunity_id: str,
        *,
        stage: OpportunityStatus,
        occurred_at: datetime,
        detail: str,
    ):
        try:
            return self.watchlist.record_paper_fill(
                opportunity_id,
                stage=stage,
                occurred_at=occurred_at,
                detail=detail,
            )
        except ValueError as exc:
            current = self.watchlist.repository.get(opportunity_id)
            if current is not None and current.status is OpportunityStatus.FILLED:
                return current
            raise PaperOperationsError(str(exc)) from exc

    def _append_trade_event_once(
        self,
        trade: PaperTrade,
        *,
        event_type: PaperTradeAuditEventType,
        occurred_at: datetime,
        detail: str | None,
    ) -> None:
        event_id = f"{trade.trade_id}:{event_type.value}"
        if any(event.event_id == event_id for event in trade.audit):
            return
        trade.audit.append(
            PaperTradeAuditEvent(
                event_id=event_id,
                occurred_at=occurred_at,
                event_type=event_type,
                detail=detail,
            )
        )

    def simulate_fill(
        self,
        opportunity_id: str,
        *,
        config: PaperFillConfig | None = None,
        capital_source: CapitalSource = CapitalSource.AUTO_POOL,
        confirm_external: ExternalLegConfirmation | None = None,
        now: datetime | None = None,
        provenance: DataProvenance = DataProvenance.LIVE_PAPER,
        operator_note: str = "PAPER-ONLY explicit simulate fill",
        simulate_external: bool = False,
        prepared_deployment_id: str | None = None,
        requested_size_gbp: Decimal | None = None,
    ) -> SimulatePaperFillResult:
        simulated_at = now or datetime.now(UTC)
        require_complete = simulate_external
        with self._fill_persist_lock:
            return self._simulate_fill_locked(
                opportunity_id,
                config=config,
                capital_source=capital_source,
                confirm_external=confirm_external,
                simulated_at=simulated_at,
                provenance=provenance,
                operator_note=operator_note,
                simulate_external=simulate_external,
                prepared_deployment_id=prepared_deployment_id,
                requested_size_gbp=requested_size_gbp,
                require_complete=require_complete,
            )

    def _simulate_fill_locked(
        self,
        opportunity_id: str,
        *,
        config: PaperFillConfig | None,
        capital_source: CapitalSource,
        confirm_external: ExternalLegConfirmation | None,
        simulated_at: datetime,
        provenance: DataProvenance,
        operator_note: str,
        simulate_external: bool,
        prepared_deployment_id: str | None,
        requested_size_gbp: Decimal | None,
        require_complete: bool,
    ) -> SimulatePaperFillResult:
        existing = self._get_trade_by_opportunity(opportunity_id)
        if existing is not None:
            completing_awaiting = (
                existing.state is PaperTradeState.AWAITING_MANUAL_EXTERNAL
                and confirm_external is not None
            )
            if not completing_awaiting:
                completed = self._complete_or_repeat_existing(
                    existing,
                    simulated_at=simulated_at,
                    operator_note=operator_note,
                )
                if completed is not None:
                    return completed

        plan = self._plans.get(opportunity_id)
        if plan is None:
            raise PaperOperationsError("missing_paper_fill_plan")
        current = self.watchlist.repository.get(opportunity_id)
        if current is None:
            raise PaperOperationsError("unknown_opportunity")
        if current.data_kind == "demo_fixture_replay":
            provenance = DataProvenance.FIXTURE_DEMO
        presented = self.watchlist._present_freshness(current, simulated_at)
        if presented.status not in {
            OpportunityStatus.TRIGGERED,
            OpportunityStatus.PAPER_FILLING,
            OpportunityStatus.PARTIAL,
        }:
            self._fail_entry(opportunity_id, "stale_before_fill", simulated_at)
        bound_prepared_id: str | None = None
        if prepared_deployment_id is not None or requested_size_gbp is not None:
            plan, bound_prepared_id = self._bind_prepared_allocation(
                opportunity_id,
                plan,
                prepared_deployment_id=prepared_deployment_id,
                requested_size_gbp=requested_size_gbp,
            )
        opening_legs = [leg for leg in plan.legs if leg.requested_stake > 0]
        if not opening_legs:
            self._fail_entry(opportunity_id, "no_positive_opening_legs", simulated_at)

        fill_config = config or PaperFillConfig(
            assumed_latency_ms=self.settings.simulated_latency_ms,
            max_quote_age_ms=self.watchlist.max_quote_age_ms,
            slippage_bps=Decimal(self.settings.max_slippage_bps),
        )
        modes = plan.execution_modes
        external_venues = {
            venue for venue, mode in modes.items() if mode is LegExecutionMode.EXTERNAL_OPERATOR
        }
        confirmation = confirm_external or self._external_confirmations.get(opportunity_id)
        hedge_valid: bool | None = None
        if external_venues and not simulate_external:
            if confirmation is None:
                self._persist_awaiting_external(plan, current, simulated_at, provenance)
                self._fail_entry(opportunity_id, "manual_external_confirmation_required", simulated_at)
            if confirmation.venue not in external_venues:
                self._fail_entry(opportunity_id, "external_confirmation_venue_mismatch", simulated_at)
            self._external_confirmations[opportunity_id] = confirmation
            hedge_valid = self._revalidate_remaining_hedge(plan, confirmation)
            if not hedge_valid:
                self._fail_entry(opportunity_id, "remaining_hedge_revalidation_failed", simulated_at)

        if simulate_external:
            fill_legs = list(opening_legs)
        else:
            fill_legs = [
                leg
                for leg in opening_legs
                if modes.get(leg.venue, LegExecutionMode.INTERNAL) is LegExecutionMode.INTERNAL
            ]
        if not fill_legs:
            self._fail_entry(opportunity_id, "no_internal_paper_legs", simulated_at)
        if require_complete:
            try:
                self._assert_spendable_treasury(opening_legs)
            except PaperOperationsError as exc:
                self._fail_entry(opportunity_id, str(exc), simulated_at)

        fills = self.simulator.simulate(
            fill_legs,
            fill_config,
            opportunity_id=opportunity_id,
            now=simulated_at,
        )
        fills = _with_stable_fill_ids(fills, opportunity_id, modes, simulate_external=simulate_external)
        if require_complete and not _complete_opening_fills(fills, opening_legs):
            reason = fills.rejection_reasons[0] if fills.rejection_reasons else "incomplete_opening_hedge"
            self._fail_entry(opportunity_id, reason, simulated_at)
        stage = _fill_stage(fills)
        if stage is None:
            reason = fills.rejection_reasons[0] if fills.rejection_reasons else "paper_fill_rejected"
            self._fail_entry(opportunity_id, reason, simulated_at)
        if require_complete and stage is not OpportunityStatus.FILLED:
            self._fail_entry(opportunity_id, "incomplete_opening_hedge", simulated_at)

        confirmed = confirmation if external_venues and not simulate_external else None
        try:
            if self.ledger is not None:
                with self.ledger.transaction():
                    journals = self._post_fills(
                        plan,
                        fills,
                        confirmation=confirmed,
                        capital_source=capital_source,
                        occurred_at=simulated_at,
                        provenance=provenance,
                        simulate_external=simulate_external,
                    )
                    trade = self._persist_open_trade(
                        plan,
                        current,
                        fills,
                        confirmation=confirmed,
                        occurred_at=simulated_at,
                        provenance=provenance,
                        simulate_external=simulate_external,
                        autofill=simulate_external,
                        require_complete=require_complete,
                    )
            else:
                journals = self._post_fills(
                    plan,
                    fills,
                    confirmation=confirmed,
                    capital_source=capital_source,
                    occurred_at=simulated_at,
                    provenance=provenance,
                    simulate_external=simulate_external,
                )
                trade = self._persist_open_trade(
                    plan,
                    current,
                    fills,
                    confirmation=confirmed,
                    occurred_at=simulated_at,
                    provenance=provenance,
                    simulate_external=simulate_external,
                    autofill=simulate_external,
                    require_complete=require_complete,
                )
        except (PaperOperationsError, PaperTreasuryError) as exc:
            self._fail_entry(opportunity_id, str(exc), simulated_at)

        if require_complete and trade is not None and trade.state is not PaperTradeState.OPEN:
            self._fail_entry(opportunity_id, "incomplete_opening_hedge", simulated_at)

        opportunity = self._record_watchlist_fill(
            opportunity_id,
            stage=stage,
            occurred_at=simulated_at,
            detail=operator_note,
        )
        self._entry_rejections.pop(opportunity_id, None)
        alert = None
        existing_id = self.alerts._by_opportunity.get(opportunity_id)
        if existing_id:
            alert = self.alerts.get_alert(existing_id)
        postings = self.journal.postings(opportunity_id=opportunity_id)
        native_totals = _native_totals(postings)
        steps = _trace_steps(
            alert is not None,
            confirmation is not None and not simulate_external,
            bool(journals),
            simulate_external=simulate_external,
        )
        trace = PaperChainTrace(
            opportunity_id=opportunity_id,
            steps=steps,
            scan_eligible=plan.eligible_for_paper_simulation,
            watchlist_status=opportunity.status,
            alert_id=alert.alert_id if alert is not None else None,
            fill_ids=[fill.fill_id for fill in fills.fills],
            journal_ids=[entry.journal_id for entry in journals],
            balanced_gbp=gbp_is_balanced(postings),
            native_totals=native_totals,
            provenance=provenance,
            detail=(
                "scan -> paper autofill -> simulated fills -> balanced postings"
                if simulate_external
                else "scan -> alert -> explicit simulated fill -> fill records -> balanced postings"
            ),
        )
        result = SimulatePaperFillResult(
            opportunity=opportunity,
            fills=fills,
            journals=journals,
            alert=alert,
            hedge_still_valid=hedge_valid,
            trace=trace,
            solver_model=plan.decision.solver_model,
            entry_complete=trade is not None and trade.state is PaperTradeState.OPEN,
            allocated_requested_stakes=_allocated_stake_labels(opening_legs),
            prepared_deployment_id=bound_prepared_id,
        )
        result.trade_id = trade.trade_id if trade is not None else None
        return result

    def settle(
        self,
        trade_id: str,
        request: PaperSettlementRequest,
        *,
        now: datetime | None = None,
    ) -> PaperTradeDetail:
        if self.trades is None:
            raise PaperOperationsError("paper_trade_repository_unavailable")
        trade = self.trades.get(trade_id)
        if trade is None:
            raise PaperOperationsError("unknown_trade")
        settled_at = request.settled_at or now or datetime.now(UTC)
        if trade.state is PaperTradeState.CLOSED:
            if trade.settlement_source == PAPER_UNWIND_SOURCE:
                raise PaperOperationsError("already_unwound")
            if (
                trade.settlement_outcome == request.winning_outcome
                and trade.settlement_source == request.source
                and trade.settlement_source_id == request.source_id
            ):
                trade.audit.append(
                    PaperTradeAuditEvent(
                        occurred_at=settled_at,
                        event_type=PaperTradeAuditEventType.SETTLEMENT_IDEMPOTENT,
                        detail="identical settlement request ignored",
                    )
                )
                self.trades.save(trade)
                return self.trade_detail(trade_id)
            raise PaperOperationsError("conflicting_settlement")
        if trade.state is PaperTradeState.AWAITING_MANUAL_EXTERNAL:
            raise PaperOperationsError("cannot_settle_unconfirmed_external")
        if not any(leg.filled_stake > 0 for leg in trade.legs):
            raise PaperOperationsError("cannot_settle_unfilled_trade")
        try:
            computation = compute_paper_settlement(trade, winning_outcome=request.winning_outcome)
        except PaperSettlementError as exc:
            raise PaperOperationsError(str(exc)) from exc

        if self.ledger is not None:
            try:
                self.ledger.treasury.apply_settlement(
                    trade, computation, request, settled_at=settled_at
                )
            except (PaperTreasuryError, DuplicateJournalError) as exc:
                raise PaperOperationsError(str(exc)) from exc
        else:
            source_id = f"settle:{trade.trade_id}:{request.source}:{request.source_id}"
            postings = []
            for leg in computation.legs:
                venue = VenueName(leg.venue)
                capital = CapitalSource(leg.capital_source)
                postings.extend(
                    settlement_leg_postings(
                        venue=venue,
                        currency=leg.currency,
                        stake_native=leg.filled_stake,
                        net_payoff_native=leg.net_payoff,
                        venue_fee_native=leg.venue_fee,
                        amount_gbp_per_native=leg.fx_rate_gbp_per_unit,
                        opportunity_id=trade.opportunity_id,
                        capital_source=capital,
                        canonical_event_id=trade.canonical_event_id,
                        position_id=trade.trade_id,
                        won=leg.won,
                    )
                )
            entry = PaperJournalEntry(
                source="paper_settlement",
                source_id=source_id,
                occurred_at=settled_at,
                description=(
                    f"PAPER-ONLY settlement outcome={request.winning_outcome} "
                    f"source={request.source}:{request.source_id}"
                ),
                opportunity_id=trade.opportunity_id,
                trade_id=trade.trade_id,
                provenance=request.provenance,
                postings=postings,
            )
            try:
                self.journal.append_idempotent(entry)
            except DuplicateJournalError as exc:
                raise PaperOperationsError("conflicting_journal_facts") from exc

        trade.state = PaperTradeState.CLOSED
        trade.settled_at = settled_at
        trade.last_updated_at = settled_at
        trade.realised_pnl_gbp = computation.realised_pnl_gbp
        trade.settlement_outcome = request.winning_outcome
        trade.settlement_source = request.source
        trade.settlement_source_id = request.source_id
        trade.settlement_detail = request.detail
        trade.capital_locked_native = {}
        trade.capital_locked_gbp = Decimal(0)
        trade.audit.append(
            PaperTradeAuditEvent(
                occurred_at=settled_at,
                event_type=PaperTradeAuditEventType.SETTLED,
                detail=(
                    f"outcome={request.winning_outcome} realised_pnl_gbp={computation.realised_pnl_gbp} "
                    f"source={request.source}"
                ),
            )
        )
        self.trades.save(trade)
        if self.watchlist.repository.get(trade.opportunity_id) is not None:
            try:
                self.watchlist.close(trade.opportunity_id, occurred_at=settled_at, detail="paper settlement")
            except Exception:
                pass
        return self.trade_detail(trade_id)

    def list_active_trades(self) -> list[PaperTrade]:
        if self.trades is None:
            return []
        return self.trades.list_active()

    def list_closed_trades(self) -> list[PaperTrade]:
        if self.trades is None:
            return []
        return self.trades.list_closed()

    def trade_detail(self, trade_id: str) -> PaperTradeDetail:
        if self.trades is None:
            raise PaperOperationsError("paper_trade_repository_unavailable")
        trade = self.trades.get(trade_id)
        if trade is None:
            raise PaperOperationsError("unknown_trade")
        return PaperTradeDetail(
            **trade.model_dump(),
            journals=self.journal.list_entries(opportunity_id=trade.opportunity_id),
        )

    def evaluate_unwind(
        self,
        trade_id: str,
        *,
        quotes: list[ReverseQuote],
        fx=None,
        policy: UnwindPolicy | None = None,
        scarcity: CapitalScarcityInput | None = None,
        evaluated_at: datetime | None = None,
        record_audit: bool = True,
    ) -> UnwindDecision:
        """Analytical close plan. Does not post journals or mutate pool balances."""

        if self.trades is None:
            raise PaperOperationsError("paper_trade_repository_unavailable")
        trade = self.trades.get(trade_id)
        if trade is None:
            raise PaperOperationsError("unknown_trade")
        if trade.state is PaperTradeState.CLOSED:
            raise PaperOperationsError("trade_already_closed")
        try:
            position = position_from_trade(trade)
        except UnwindIdentityError as exc:
            raise PaperOperationsError(str(exc)) from exc
        position = self._overlay_modelled_remaining_lock(trade, position)
        snapshots = list(fx) if fx is not None else list(trade.fx_snapshots)
        decision = PaperUnwindEngine().evaluate(
            UnwindEvaluationRequest(
                position=position,
                quotes=quotes,
                fx=snapshots,
                policy=policy or UnwindPolicy(),
                scarcity=scarcity or CapitalScarcityInput(),
                evaluated_at=evaluated_at,
            )
        )
        if record_audit:
            trade.audit.append(
                PaperTradeAuditEvent(
                    occurred_at=evaluated_at or datetime.now(UTC),
                    event_type=PaperTradeAuditEventType.CLOSE_PLAN_EVALUATED,
                    detail=(
                        f"{decision.recommendation.value}:{decision.decision_reason};"
                        "conditionally_releasable_is_not_spendable"
                    ),
                )
            )
            self.trades.save(trade)
        return decision

    def complete_validated_unwind(
        self,
        trade_id: str,
        *,
        quotes: list[ReverseQuote],
        fx=None,
        policy: UnwindPolicy | None = None,
        scarcity: CapitalScarcityInput | None = None,
        now: datetime | None = None,
        record_evaluation_audit: bool = True,
    ) -> PaperTradeDetail:
        """PAPER-ONLY: post a fully validated 8D close through 8E. Never places orders."""

        if self.ledger is None or self.trades is None:
            raise PaperOperationsError("paper_trade_repository_unavailable")
        occurred = now or datetime.now(UTC)
        trade = self.trades.get(trade_id)
        if trade is None:
            raise PaperOperationsError("unknown_trade")
        unwind_id = paper_unwind_source_id(trade.trade_id)
        if trade.state is PaperTradeState.CLOSED:
            if (
                trade.settlement_source == PAPER_UNWIND_SOURCE
                and trade.settlement_source_id == unwind_id
            ):
                trade.audit.append(
                    PaperTradeAuditEvent(
                        occurred_at=occurred,
                        event_type=PaperTradeAuditEventType.UNWIND_IDEMPOTENT,
                        detail="identical unwind request ignored",
                    )
                )
                self.trades.save(trade)
                return self.trade_detail(trade_id)
            if trade.settlement_source == PAPER_UNWIND_SOURCE:
                raise PaperOperationsError("already_unwound")
            raise PaperOperationsError("already_settled")
        decision = self.evaluate_unwind(
            trade_id,
            quotes=quotes,
            fx=fx,
            policy=policy,
            scarcity=scarcity,
            evaluated_at=occurred,
            record_audit=record_evaluation_audit,
        )
        if decision.recommendation is not UnwindRecommendation.UNWIND_ELIGIBLE:
            raise PaperOperationsError(f"unwind_not_eligible:{decision.decision_reason}")
        if not decision.close_plan.fully_executable:
            raise PaperOperationsError("close_not_fully_executable")
        trade = self.trades.get(trade_id)
        if trade is None:
            raise PaperOperationsError("unknown_trade")
        try:
            position = position_from_trade(trade)
        except UnwindIdentityError as exc:
            raise PaperOperationsError(str(exc)) from exc
        if len(decision.close_plan.legs) != len(position.legs):
            raise PaperOperationsError("unwind_leg_mismatch")
        fx_map = {
            item.currency: item for item in (fx if fx is not None else trade.fx_snapshots)
        }
        releases: list[UnwindReleaseLeg] = []
        fx_rates: dict[tuple, Decimal] = {}
        for close_leg, open_leg in zip(decision.close_plan.legs, position.legs, strict=True):
            if not open_leg.fill_id:
                raise PaperOperationsError("missing_lock_identity")
            rate = self._lock_fx_rate(open_leg.venue, open_leg.native_currency, fx_map)
            fx_rates[(close_leg.venue, close_leg.native_currency.upper())] = rate
            releases.append(
                UnwindReleaseLeg(
                    venue=close_leg.venue,
                    native_currency=close_leg.native_currency,
                    lock_id=open_leg.fill_id,
                    amount_native=open_leg.filled_size,
                    realised_pnl_native=close_leg.native_close_pnl,
                    fee_native=close_leg.closing_fee,
                    fx_rate_gbp_per_unit=rate,
                )
            )
        try:
            close_fills = close_fills_from_decision(position, decision, fx_rates=fx_rates)
        except UnwindIdentityError as exc:
            raise PaperOperationsError(str(exc)) from exc
        try:
            self.ledger.treasury.post_unwind(
                ValidatedUnwindResult(
                    trade_id=trade.trade_id,
                    close_completed=True,
                    opportunity_id=trade.opportunity_id,
                    source=PAPER_UNWIND_SOURCE,
                    source_id=unwind_id,
                    reason="validated paper unwind",
                    releases=releases,
                ),
                now=occurred,
            )
        except PaperTreasuryError as exc:
            raise PaperOperationsError(str(exc)) from exc
        opening_legs = [leg.model_copy() for leg in trade.legs]
        trade.legs = opening_legs
        trade.close_fills = close_fills
        trade.state = PaperTradeState.CLOSED
        trade.settled_at = occurred
        trade.last_updated_at = occurred
        trade.realised_pnl_gbp = decision.validated_exit_pnl_gbp
        trade.settlement_outcome = None
        trade.settlement_source = PAPER_UNWIND_SOURCE
        trade.settlement_source_id = unwind_id
        trade.settlement_detail = (
            f"validated unwind {decision.recommendation.value}; "
            "capital released only after 8E postings"
        )
        trade.capital_locked_native = {}
        trade.capital_locked_gbp = Decimal(0)
        trade.audit.append(
            PaperTradeAuditEvent(
                occurred_at=occurred,
                event_type=PaperTradeAuditEventType.CLOSE_FILLS_RECORDED,
                detail=f"close_fills={len(close_fills)} distinct from opening legs",
            )
        )
        trade.audit.append(
            PaperTradeAuditEvent(
                occurred_at=occurred,
                event_type=PaperTradeAuditEventType.UNWIND_COMPLETED,
                detail=(
                    f"exit_pnl_gbp={decision.validated_exit_pnl_gbp} "
                    f"unwind_cost_gbp={decision.unwind_cost_gbp}"
                ),
            )
        )
        unwind_risk = snapshot_from_execution_risk(
            decision.execution_risk,
            kind=PaperRiskSnapshotKind.UNWIND,
            recorded_at=occurred,
            opportunity_id=trade.opportunity_id,
            trade_id=trade.trade_id,
            maximum_execution_risk=(policy or UnwindPolicy()).max_execution_risk,
        )
        if unwind_risk is not None:
            trade.close_risks.append(unwind_risk)
            trade.audit.append(
                PaperTradeAuditEvent(
                    occurred_at=occurred,
                    event_type=PaperTradeAuditEventType.UNWIND_RISK_RECORDED,
                    detail=f"score={unwind_risk.score} band={unwind_risk.band}",
                )
            )
        self.trades.save(trade)
        if self.watchlist.repository.get(trade.opportunity_id) is not None:
            try:
                self.watchlist.close(trade.opportunity_id, occurred_at=occurred, detail="paper unwind")
            except Exception:
                pass
        return self.trade_detail(trade_id)

    def abandon_open_trades_for_demo_reset(self, *, reason: str, now: datetime | None = None) -> None:
        if self.trades is None:
            return
        occurred = now or datetime.now(UTC)
        for trade in self.trades.list_active():
            trade.state = PaperTradeState.CLOSED
            trade.last_updated_at = occurred
            trade.settled_at = occurred
            trade.settlement_source = "demo_reset"
            trade.settlement_source_id = reason
            trade.settlement_detail = (
                "explicit demo store reinitialize; not a market settlement; "
                "capital returned without realised betting P&L"
            )
            trade.realised_pnl_gbp = None
            trade.capital_locked_native = {}
            trade.capital_locked_gbp = Decimal(0)
            trade.audit.append(
                PaperTradeAuditEvent(
                    occurred_at=occurred,
                    event_type=PaperTradeAuditEventType.DEMO_STORE_REINITIALIZED,
                    detail=reason,
                )
            )
            self.trades.save(trade)

    def _overlay_modelled_remaining_lock(self, trade: PaperTrade, position):
        plan = self._plans.get(trade.opportunity_id)
        allocation = plan.decision.allocation if plan is not None else None
        hours = None if allocation is None else allocation.expected_lock_duration_hours
        if hours is None or hours <= 0:
            return position
        minutes = hours * Decimal(60)
        basis_label = allocation.expected_lock_basis or "8C modelled estimate"
        estimate = getattr(allocation, "estimated_time_to_release", None)
        confidence = None if estimate is None else getattr(estimate, "confidence", None)
        return position.model_copy(
            update={
                "remaining_lock_minutes": minutes,
                "remaining_lock_basis": RemainingLockSource.MODELLED,
                "remaining_lock_confidence": confidence,
                "remaining_lock_detail": (
                    f"{basis_label}; advisory only; does not release capital"
                ),
            }
        )

    def book_summary(self) -> PaperTradeBookSummary:
        active = self.list_active_trades()
        closed = self.list_closed_trades()
        native: dict[str, Decimal] = {}
        gbp_locked = Decimal(0)
        gbp_ok = True
        for trade in active:
            for currency, amount in trade.capital_locked_native.items():
                native[currency] = native.get(currency, Decimal(0)) + amount
            if trade.capital_locked_gbp is None:
                gbp_ok = False
            else:
                gbp_locked += trade.capital_locked_gbp
        realised = sum((trade.realised_pnl_gbp or Decimal(0) for trade in closed), Decimal(0))
        return PaperTradeBookSummary(
            open_count=sum(1 for trade in active if trade.state is not PaperTradeState.AWAITING_MANUAL_EXTERNAL),
            closed_count=len(closed),
            awaiting_manual_external_count=sum(
                1 for trade in active if trade.state is PaperTradeState.AWAITING_MANUAL_EXTERNAL
            ),
            capital_locked_native=native,
            capital_locked_gbp=gbp_locked if gbp_ok else None,
            realised_pnl_gbp=realised if closed else Decimal(0),
            gbp_unavailable_reason=None if gbp_ok else "missing_fx_on_one_or_more_open_trades",
        )

    def _require_allocator_sized_plan(self, opportunity_id: str) -> None:
        plan = self._plans.get(opportunity_id)
        if plan is None:
            self._fail_entry(opportunity_id, "missing_paper_fill_plan")
        allocation = plan.decision.allocation
        if allocation is None or not allocation.accepted:
            self._fail_entry(opportunity_id, "allocator_size_required")
        try:
            mapped = apply_allocation_to_fill_legs(plan.legs, allocation)
        except FillPlanMappingError as exc:
            self._fail_entry(opportunity_id, f"allocation_failed:{exc.reason}")
        mapped_stakes = {
            (leg.venue, leg.source_market_id, leg.source_runner_id, leg.outcome): leg.requested_stake
            for leg in mapped
        }
        plan_stakes = {
            (leg.venue, leg.source_market_id, leg.source_runner_id, leg.outcome): leg.requested_stake
            for leg in plan.legs
            if leg.requested_stake > 0
        }
        if mapped_stakes != plan_stakes:
            self._fail_entry(opportunity_id, "fill_plan_not_allocator_sized")

    def _assert_spendable_treasury(self, legs: list[PaperOpportunityLeg]) -> None:
        if self.ledger is None:
            return
        snap = self.ledger.treasury.snapshot()
        needed: dict[tuple[VenueName, str], Decimal] = {}
        for leg in legs:
            key = (leg.venue, leg.currency)
            needed[key] = needed.get(key, Decimal(0)) + leg.requested_stake
        for (venue, currency), amount in needed.items():
            try:
                pool = snap.pool(venue, currency)
            except KeyError as exc:
                raise PaperOperationsError("missing_treasury_pool") from exc
            if pool.available_cash < amount:
                raise PaperOperationsError("insufficient_spendable_treasury")

    def _fail_entry(
        self,
        opportunity_id: str,
        reason: str,
        occurred_at: datetime | None = None,
    ) -> None:
        self._record_entry_rejection(opportunity_id, reason, occurred_at=occurred_at)
        raise PaperOperationsError(reason)

    def _record_entry_rejection(
        self,
        opportunity_id: str,
        reason: str,
        occurred_at: datetime | None = None,
    ) -> None:
        self._entry_rejections[opportunity_id] = reason
        self.watchlist.record_paper_fill_rejection(
            opportunity_id,
            occurred_at=occurred_at or datetime.now(UTC),
            detail=reason,
        )

    def _get_trade_by_opportunity(self, opportunity_id: str) -> PaperTrade | None:
        if self.trades is None:
            return None
        return self.trades.get_by_opportunity(opportunity_id)

    def _result_from_existing_trade(self, trade: PaperTrade, when: datetime) -> SimulatePaperFillResult:
        opportunity = self.watchlist.repository.get(trade.opportunity_id)
        if opportunity is None:
            raise PaperOperationsError("unknown_opportunity")
        from sports_hedge.paper.fills import FillMode

        fills = PaperOpportunityFills(
            opportunity_id=trade.opportunity_id,
            mode=FillMode.REALISTIC,
            simulated_at=when,
            fills=[],
        )
        journals = self.journal.list_entries(opportunity_id=trade.opportunity_id)
        postings = self.journal.postings(opportunity_id=trade.opportunity_id)
        trace = PaperChainTrace(
            opportunity_id=trade.opportunity_id,
            steps=[PaperChainStep.SIMULATED_FILL, PaperChainStep.JOURNAL_POSTING],
            scan_eligible=True,
            watchlist_status=opportunity.status,
            fill_ids=[leg.fill_id for leg in trade.legs if leg.fill_id],
            journal_ids=[entry.journal_id for entry in journals],
            balanced_gbp=gbp_is_balanced(postings),
            native_totals=_native_totals(postings),
            provenance=trade.provenance,
            detail="idempotent existing paper trade; no additional fills posted",
        )
        result = SimulatePaperFillResult(
            opportunity=opportunity,
            fills=fills,
            journals=journals,
            trace=trace,
            solver_model=trade.solver_model,
            entry_complete=trade.state is PaperTradeState.OPEN,
            allocated_requested_stakes=_allocated_stake_labels_from_trade(trade),
        )
        result.trade_id = trade.trade_id
        return result

    def _persist_awaiting_external(
        self,
        plan: PaperFillPlan,
        opportunity: NearOpportunity,
        occurred_at: datetime,
        provenance: DataProvenance,
    ) -> PaperTrade | None:
        if self.trades is None:
            return None
        existing = self.trades.get_by_opportunity(plan.opportunity_id)
        if existing is not None:
            return existing
        trade = self._new_trade_shell(plan, opportunity, occurred_at, provenance)
        trade.state = PaperTradeState.AWAITING_MANUAL_EXTERNAL
        trade.legs = _unfilled_legs_from_plan(plan)
        trade.audit.append(
            PaperTradeAuditEvent(
                occurred_at=occurred_at,
                event_type=PaperTradeAuditEventType.AWAITING_MANUAL_EXTERNAL,
                detail="EXTERNAL_OPERATOR leg requires explicit MANUAL_EXTERNAL confirmation",
            )
        )
        return self.trades.save(trade)

    def _persist_open_trade(
        self,
        plan: PaperFillPlan,
        opportunity: NearOpportunity,
        fills: PaperOpportunityFills,
        *,
        confirmation: ExternalLegConfirmation | None,
        occurred_at: datetime,
        provenance: DataProvenance,
        simulate_external: bool,
        autofill: bool,
        require_complete: bool = False,
    ) -> PaperTrade | None:
        if self.trades is None:
            return None
        trade = self.trades.get_by_opportunity(plan.opportunity_id) or self._new_trade_shell(
            plan, opportunity, occurred_at, provenance
        )
        fill_by_key = {
            (fill.venue, fill.outcome, fill.source_market_id, fill.source_runner_id): fill
            for fill in fills.fills
        }
        legs: list[PaperTradeLeg] = []
        native: dict[str, Decimal] = {}
        gbp = Decimal(0)
        fx = {item.currency: item for item in plan.fx_snapshots}
        for plan_leg in plan.legs:
            if plan_leg.requested_stake <= 0:
                continue
            mode = plan.execution_modes.get(plan_leg.venue, LegExecutionMode.INTERNAL)
            fill = fill_by_key.get(
                (plan_leg.venue, plan_leg.outcome, plan_leg.source_market_id, plan_leg.source_runner_id)
            )
            fill_kind = PaperLegFillKind.UNFILLED
            capital = CapitalSource.AUTO_POOL
            filled_stake = Decimal(0)
            filled_odds = None
            fill_id = None
            requested = plan_leg.requested_stake
            if fill is not None and fill.filled_stake > 0:
                filled_stake = fill.filled_stake
                filled_odds = fill.weighted_odds or fill.displayed_odds
                fill_id = fill.fill_id
                if simulate_external and mode is LegExecutionMode.EXTERNAL_OPERATOR:
                    fill_kind = PaperLegFillKind.PAPER_SIMULATED_EXTERNAL
                    capital = CapitalSource.PAPER_SIMULATED_EXTERNAL
                else:
                    fill_kind = PaperLegFillKind.INTERNAL_SIMULATED
                    capital = CapitalSource.AUTO_POOL
            if confirmation is not None and plan_leg.venue == confirmation.venue:
                fill_kind = PaperLegFillKind.MANUAL_EXTERNAL
                capital = CapitalSource.MANUAL_EXTERNAL
                filled_stake = confirmation.executed_size
                filled_odds = confirmation.executed_price
                fill_id = manual_external_fill_id(
                    plan.opportunity_id, confirmation.operator_counterparty_reference
                )
                requested = confirmation.executed_size
            legs.append(
                PaperTradeLeg(
                    venue=plan_leg.venue,
                    outcome=plan_leg.outcome,
                    currency=plan_leg.currency,
                    requested_stake=requested,
                    filled_stake=filled_stake,
                    displayed_odds=plan_leg.displayed_odds,
                    filled_odds=filled_odds,
                    source_market_id=plan_leg.source_market_id,
                    source_event_id=plan.canonical_event_id,
                    source_runner_id=plan_leg.source_runner_id,
                    source_contract_id=plan_leg.source_runner_id if plan_leg.venue is VenueName.POLYMARKET else None,
                    opening_action=(
                        MarketAction.BUY
                        if plan_leg.venue in {VenueName.POLYMARKET, VenueName.KALSHI}
                        else MarketAction.BACK
                    ),
                    canonical_state=plan_leg.outcome,
                    settlement_fingerprint_key=opportunity.settlement_key,
                    fill_id=fill_id,
                    fill_kind=fill_kind,
                    capital_source=capital,
                    execution_mode=mode.value,
                )
            )
            if filled_stake > 0:
                native[plan_leg.currency] = native.get(plan_leg.currency, Decimal(0)) + filled_stake
                rate = Decimal(1) if plan_leg.currency == "GBP" else fx[plan_leg.currency].gbp_per_unit
                gbp += filled_stake * rate

        fully = bool(legs) and all(
            leg.filled_stake > 0 and leg.fill_kind is not PaperLegFillKind.UNFILLED for leg in legs
        )
        partial = any(leg.filled_stake > 0 for leg in legs) and not fully
        if require_complete and not fully:
            raise PaperOperationsError("incomplete_opening_hedge")
        trade.legs = legs
        trade.capital_locked_native = native
        trade.capital_locked_gbp = gbp
        trade.last_updated_at = occurred_at
        trade.provenance = provenance
        trade.fx_snapshots = list(plan.fx_snapshots)
        trade.venue_costs = list(plan.venue_costs)
        if fully:
            trade.state = PaperTradeState.OPEN
            trade.guaranteed_profit_gbp_at_open = _opening_guaranteed_profit(plan)
            if trade.entry_risk is None:
                snapshot = snapshot_from_scan_decision(
                    plan.decision,
                    kind=PaperRiskSnapshotKind.ENTRY,
                    recorded_at=occurred_at,
                    opportunity_id=plan.opportunity_id,
                    trade_id=trade.trade_id,
                )
                if snapshot is not None:
                    trade.entry_risk = snapshot
                    self._append_trade_event_once(
                        trade,
                        event_type=PaperTradeAuditEventType.ENTRY_RISK_RECORDED,
                        occurred_at=occurred_at,
                        detail=(
                            f"score={snapshot.score} band={snapshot.band} "
                            f"threshold={snapshot.maximum_execution_risk}"
                        ),
                    )
        elif partial:
            trade.state = PaperTradeState.PARTIAL
            trade.guaranteed_profit_gbp_at_open = None
        else:
            trade.state = PaperTradeState.PENDING
            trade.guaranteed_profit_gbp_at_open = None
        if autofill:
            self._append_trade_event_once(
                trade,
                event_type=PaperTradeAuditEventType.PAPER_AUTOFILL,
                occurred_at=occurred_at,
                detail="configurable paper autofill; simulated only",
            )
        if simulate_external and any(
            leg.fill_kind is PaperLegFillKind.PAPER_SIMULATED_EXTERNAL for leg in legs
        ):
            self._append_trade_event_once(
                trade,
                event_type=PaperTradeAuditEventType.PAPER_SIMULATED_EXTERNAL_FILL,
                occurred_at=occurred_at,
                detail="PAPER_SIMULATED_EXTERNAL is not MANUAL_EXTERNAL confirmation",
            )
        if confirmation is not None:
            self._append_trade_event_once(
                trade,
                event_type=PaperTradeAuditEventType.MANUAL_EXTERNAL_CONFIRMED,
                occurred_at=occurred_at,
                detail=f"reference={confirmation.operator_counterparty_reference}",
            )
            self._append_trade_event_once(
                trade,
                event_type=PaperTradeAuditEventType.HEDGE_REVALIDATED,
                occurred_at=occurred_at,
                detail="remaining hedge revalidated after MANUAL_EXTERNAL confirmation",
            )
        self._append_trade_event_once(
            trade,
            event_type=PaperTradeAuditEventType.FILLS_RECORDED,
            occurred_at=occurred_at,
            detail=f"state={trade.state.value}",
        )
        return self.trades.save(trade)

    def _new_trade_shell(
        self,
        plan: PaperFillPlan,
        opportunity: NearOpportunity,
        occurred_at: datetime,
        provenance: DataProvenance,
    ) -> PaperTrade:
        home = opportunity.home_team
        away = opportunity.away_team
        fixture = f"{home} v {away}" if home and away else None
        market_label = None
        if opportunity.market_family is not None:
            market_label = opportunity.market_family.value.replace("_", " ")
        return PaperTrade(
            trade_id=paper_trade_id(plan.opportunity_id),
            opportunity_id=plan.opportunity_id,
            canonical_event_id=plan.canonical_event_id,
            canonical_market_id=plan.canonical_market_id,
            settlement_key=opportunity.settlement_key,
            solver_model=plan.decision.solver_model,
            market_family=opportunity.market_family,
            period=opportunity.period,
            competition=opportunity.competition,
            home_team=home,
            away_team=away,
            fixture_label=fixture,
            market_label=market_label,
            state=PaperTradeState.PENDING,
            opened_at=occurred_at,
            last_updated_at=occurred_at,
            guaranteed_profit_gbp_at_open=None,
            provenance=provenance,
            fx_snapshots=list(plan.fx_snapshots),
            venue_costs=list(plan.venue_costs),
            audit=[
                PaperTradeAuditEvent(
                    event_id=f"{paper_trade_id(plan.opportunity_id)}:{PaperTradeAuditEventType.TRADE_OPENED.value}",
                    occurred_at=occurred_at,
                    event_type=PaperTradeAuditEventType.TRADE_OPENED,
                    detail="paper trade opened; guaranteed opening profit is recorded only after the complete hedge validates",
                )
            ],
        )

    def _revalidate_remaining_hedge(
        self,
        plan: PaperFillPlan,
        confirmation: ExternalLegConfirmation,
    ) -> bool:
        fx = {item.currency: item for item in plan.fx_snapshots}
        rate_snap = fx.get(confirmation.currency.upper())
        if confirmation.currency.upper() != "GBP" and rate_snap is None:
            return False
        gbp_per_unit = Decimal(1) if confirmation.currency.upper() == "GBP" else rate_snap.gbp_per_unit
        executed_reporting = confirmation.executed_size * gbp_per_unit
        external = next(
            (
                _priority_leg_from_plan(plan, leg, gbp_per_unit=gbp_per_unit)
                for leg in plan.legs
                if leg.venue == confirmation.venue and leg.outcome == confirmation.outcome
            ),
            None,
        )
        if external is None:
            return False
        hedge_legs = []
        for leg in plan.legs:
            if plan.execution_modes.get(leg.venue) is LegExecutionMode.EXTERNAL_OPERATOR:
                continue
            snap = fx.get(leg.currency)
            rate = Decimal(1) if leg.currency == "GBP" else (snap.gbp_per_unit if snap else None)
            if rate is None:
                return False
            hedge_legs.append(_priority_leg_from_plan(plan, leg, gbp_per_unit=rate))
        solution = revalidate_fixed_external_exposure(
            executed_stake_reporting=executed_reporting,
            executed_price=confirmation.executed_price,
            external_leg=external,
            hedge_legs=hedge_legs,
        )
        return solution.is_arbitrage and solution.guaranteed_profit > 0

    def _post_fills(
        self,
        plan: PaperFillPlan,
        fills: PaperOpportunityFills,
        *,
        confirmation: ExternalLegConfirmation | None,
        capital_source: CapitalSource,
        occurred_at: datetime,
        provenance: DataProvenance,
        simulate_external: bool = False,
    ) -> list[PaperJournalEntry]:
        fx = {item.currency: item for item in plan.fx_snapshots}
        modes = plan.execution_modes
        trade_id = paper_trade_id(plan.opportunity_id)
        lock_requests: list[TreasuryLockRequest] = []
        journal_specs: list[tuple[str, str, datetime, str, VenueName, str, Decimal, Decimal, CapitalSource]] = []
        for fill in fills.fills:
            if fill.filled_stake <= 0:
                continue
            rate = self._lock_fx_rate(fill.venue, fill.currency, fx)
            amount_gbp = fill.filled_stake * rate
            mode = modes.get(fill.venue, LegExecutionMode.INTERNAL)
            simulated_external = simulate_external and mode is LegExecutionMode.EXTERNAL_OPERATOR
            source = "paper_simulated_external" if simulated_external else "paper_fill_simulator"
            leg_capital = (
                CapitalSource.PAPER_SIMULATED_EXTERNAL if simulated_external else capital_source
            )
            description = (
                "PAPER-ONLY simulated EXTERNAL_OPERATOR leg; not MANUAL_EXTERNAL confirmation"
                if simulated_external
                else "PAPER-ONLY simulated internal fill cash lock"
            )
            lock_requests.append(
                TreasuryLockRequest(
                    venue=fill.venue,
                    native_currency=fill.currency,
                    amount_native=fill.filled_stake,
                    lock_id=fill.fill_id,
                    trade_id=trade_id,
                    opportunity_id=plan.opportunity_id,
                    source=source,
                    reason=description,
                    fx_rate_gbp_per_unit=rate,
                    capital_source=leg_capital.value,
                )
            )
            journal_specs.append(
                (
                    source,
                    fill.fill_id,
                    occurred_at,
                    description,
                    fill.venue,
                    fill.currency,
                    fill.filled_stake,
                    amount_gbp,
                    leg_capital,
                )
            )
        if confirmation is not None:
            rate = self._lock_fx_rate(confirmation.venue, confirmation.currency, fx)
            fill_id = manual_external_fill_id(
                plan.opportunity_id, confirmation.operator_counterparty_reference
            )
            lock_requests.append(
                TreasuryLockRequest(
                    venue=confirmation.venue,
                    native_currency=confirmation.currency,
                    amount_native=confirmation.executed_size,
                    lock_id=fill_id,
                    fill_id=fill_id,
                    trade_id=trade_id,
                    opportunity_id=plan.opportunity_id,
                    source="manual_external_confirmation",
                    reason="Operator-recorded MANUAL_EXTERNAL fill; Sports Hedge did not place this leg",
                    fx_rate_gbp_per_unit=rate,
                    capital_source=CapitalSource.MANUAL_EXTERNAL.value,
                )
            )
            journal_specs.append(
                (
                    "manual_external_confirmation",
                    fill_id,
                    confirmation.executed_at,
                    "Operator-recorded MANUAL_EXTERNAL fill; Sports Hedge did not place this leg",
                    confirmation.venue,
                    confirmation.currency,
                    confirmation.executed_size,
                    confirmation.executed_size * rate,
                    CapitalSource.MANUAL_EXTERNAL,
                )
            )
        if self.ledger is not None and lock_requests:
            try:
                return self.ledger.treasury.lock_capital(
                    lock_requests, occurred_at=occurred_at, provenance=provenance
                )
            except PaperTreasuryError as exc:
                raise PaperOperationsError(str(exc)) from exc
        entries: list[PaperJournalEntry] = []
        for spec in journal_specs:
            source, source_id, when, description, venue, currency, native, amount_gbp, leg_capital = spec
            posted, _created = self.journal.append_idempotent(
                PaperJournalEntry(
                    source=source,
                    source_id=source_id,
                    occurred_at=when,
                    description=description,
                    opportunity_id=plan.opportunity_id,
                    trade_id=trade_id,
                    provenance=provenance,
                    postings=cash_lock_postings(
                        venue=venue,
                        currency=currency,
                        amount_native=native,
                        amount_gbp=amount_gbp,
                        fx_rate_gbp_per_unit=(
                            Decimal(1) if currency == "GBP" else amount_gbp / native
                        ),
                        opportunity_id=plan.opportunity_id,
                        capital_source=leg_capital,
                        canonical_event_id=plan.canonical_event_id,
                        position_id=source_id,
                    ),
                )
            )
            entries.append(posted)
        return entries

    def _lock_fx_rate(
        self,
        venue: VenueName,
        currency: str,
        fx: dict[str, FxRateSnapshot],
    ) -> Decimal:
        if self.ledger is not None:
            try:
                return self.ledger.treasury.lock_fx_rate(venue, currency)
            except PaperTreasuryError as exc:
                raise PaperOperationsError(str(exc)) from exc
        if currency.upper() == "GBP":
            return Decimal(1)
        return fx[currency].gbp_per_unit


def _unfilled_legs_from_plan(plan: PaperFillPlan) -> list[PaperTradeLeg]:
    """Planned legs awaiting fill or MANUAL_EXTERNAL confirmation. No cash is locked."""

    legs: list[PaperTradeLeg] = []
    for plan_leg in plan.legs:
        mode = plan.execution_modes.get(plan_leg.venue, LegExecutionMode.INTERNAL)
        capital = (
            CapitalSource.MANUAL_EXTERNAL
            if mode is LegExecutionMode.EXTERNAL_OPERATOR
            else CapitalSource.AUTO_POOL
        )
        legs.append(
            PaperTradeLeg(
                venue=plan_leg.venue,
                outcome=plan_leg.outcome,
                currency=plan_leg.currency,
                requested_stake=plan_leg.requested_stake,
                filled_stake=Decimal(0),
                displayed_odds=plan_leg.displayed_odds,
                filled_odds=None,
                source_market_id=plan_leg.source_market_id,
                fill_id=None,
                fill_kind=PaperLegFillKind.UNFILLED,
                capital_source=capital,
                execution_mode=mode.value,
            )
        )
    return legs


def _with_stable_fill_ids(
    fills: PaperOpportunityFills,
    opportunity_id: str,
    modes: dict[VenueName, LegExecutionMode],
    *,
    simulate_external: bool,
) -> PaperOpportunityFills:
    rewritten: list[PaperFillRecord] = []
    for fill in fills.fills:
        mode = modes.get(fill.venue, LegExecutionMode.INTERNAL)
        prefix = (
            "paper-sim-ext"
            if simulate_external and mode is LegExecutionMode.EXTERNAL_OPERATOR
            else "paper-fill"
        )
        rewritten.append(
            fill.model_copy(
                update={"fill_id": f"{prefix}:{opportunity_id}:{fill.venue.value}:{fill.outcome}"}
            )
        )
    return fills.model_copy(update={"fills": rewritten})


def _fill_stage(fills: PaperOpportunityFills) -> OpportunityStatus | None:
    if not fills.fills:
        return None
    if any(fill.rejection_reason and fill.filled_stake <= 0 for fill in fills.fills):
        return None
    if fills.fully_filled:
        return OpportunityStatus.FILLED
    if any(fill.filled_stake > 0 for fill in fills.fills):
        return OpportunityStatus.PARTIAL
    return None


def _trace_steps(
    has_alert: bool,
    has_external: bool,
    has_journal: bool,
    *,
    simulate_external: bool = False,
) -> list[PaperChainStep]:
    steps = [PaperChainStep.SCAN_DECISION]
    if has_alert:
        steps.append(PaperChainStep.PRIORITY_ALERT)
    if simulate_external:
        steps.append(PaperChainStep.PAPER_AUTOFILL)
    if has_external:
        steps.extend([PaperChainStep.EXTERNAL_CONFIRMATION, PaperChainStep.HEDGE_REVALIDATION])
    steps.extend([PaperChainStep.SIMULATED_FILL, PaperChainStep.WATCHLIST_FILL])
    if has_journal:
        steps.extend([PaperChainStep.JOURNAL_POSTING, PaperChainStep.RECONCILIATION])
    return steps


def _native_totals(postings: list[DimensionedPosting]) -> dict[str, Decimal]:
    totals: dict[str, Decimal] = {}
    for posting in postings:
        if posting.side.value != "debit":
            continue
        if not posting.account_code.startswith("ASSET:CASH:LOCKED"):
            continue
        totals[posting.dimensions.currency] = (
            totals.get(posting.dimensions.currency, Decimal(0)) + posting.amount_native
        )
    return totals


def _net_odds_for_leg(plan: PaperFillPlan, leg: PaperOpportunityLeg) -> Decimal:
    if plan.decision.depth_scan is not None:
        for quote in plan.decision.depth_scan.selected_quotes:
            if quote.outcome == leg.outcome and quote.venue == leg.venue:
                return quote.net_decimal_odds
    if plan.decision.payoff_scan is not None:
        for quote in plan.decision.payoff_scan.selected_quotes:
            if (
                quote.venue is leg.venue
                and quote.outcome == leg.outcome
                and quote.source_market_id == leg.source_market_id
                and quote.source_runner_id == leg.source_runner_id
            ):
                return quote.net_decimal_odds
    return leg.displayed_odds


def _priority_leg_from_plan(
    plan: PaperFillPlan, leg: PaperOpportunityLeg, *, gbp_per_unit: Decimal
) -> PriorityLeg:
    native_max = sum((level.available_stake for level in leg.levels), Decimal(0)) or leg.requested_stake
    return PriorityLeg(
        outcome=leg.outcome,
        venue=leg.venue,
        source_market_id=leg.source_market_id,
        source_runner_id=leg.source_runner_id,
        net_decimal_odds=_net_odds_for_leg(plan, leg),
        max_stake_reporting=native_max * gbp_per_unit,
        native_currency=leg.currency,
        native_max_stake=native_max,
        gbp_per_unit=gbp_per_unit,
        quote_age_ms=leg.quote_age_ms or 0,
    )


def _candidate_from_decision(decision: PaperScanDecision, opportunity_id: str) -> PriorityAlertCandidate:
    assert decision.depth_scan is not None
    fx = {item.currency: item.gbp_per_unit for item in decision.fx_snapshots}
    legs: list[PriorityLeg] = []
    for quote in decision.depth_scan.selected_quotes:
        fill = next((item for item in decision.fill_legs if item.outcome == quote.outcome), None)
        currency = fill.currency if fill is not None else "GBP"
        rate = fx.get(currency, Decimal(1))
        mode = LegExecutionMode(decision.execution_modes.get(quote.venue, LegExecutionMode.INTERNAL))
        legs.append(
            PriorityLeg(
                outcome=quote.outcome,
                venue=quote.venue,
                source_market_id=quote.source_market_id,
                source_runner_id=quote.source_runner_id,
                net_decimal_odds=quote.net_decimal_odds,
                max_stake_reporting=quote.cumulative_depth,
                native_currency=currency,
                native_max_stake=quote.cumulative_depth / rate if rate else quote.cumulative_depth,
                gbp_per_unit=rate,
                levels_consumed=quote.levels_consumed,
                quote_age_ms=decision.quote_age_ms or 0,
                execution_mode=mode,
            )
        )
    return PriorityAlertCandidate(
        opportunity_id=opportunity_id,
        canonical_event_id=decision.canonical_event_id,
        canonical_market_id=decision.canonical_market_id,
        settlement_equivalent=decision.market_match.matched,
        ordinary_solution=decision.depth_scan.solution,
        legs=legs,
        fee_snapshots=list(decision.fee_snapshots),
        venue_costs=list(decision.venue_costs),
        fx_snapshots=list(decision.fx_snapshots),
        execution_risk_score=decision.execution_risk.score if decision.execution_risk else 0,
        eligibility_confirmed=not any(leg.execution_mode is LegExecutionMode.EXTERNAL_OPERATOR for leg in legs),
    )


def _ticket_market_labels(
    plan: PaperFillPlan, watch: NearOpportunity | None
) -> tuple[str | None, str | None]:
    if watch is not None:
        family = _humanize_token(watch.market_family)
        period = _humanize_token(watch.period)
        settlement = " · ".join(
            part for part in (watch.settlement_key, period, family) if part
        )
        return family, settlement or None
    market_id = plan.canonical_market_id
    return market_id, "settlement-equivalent" if plan.settlement_equivalent else None


def _humanize_token(value: object | None) -> str | None:
    if value is None:
        return None
    text = value.value if hasattr(value, "value") else str(value)
    text = text.replace("_", " ").strip()
    return text or None


def _ticket_survivability(result) -> BetTicketSurvivability:
    raw = getattr(result, "survivability", None) if result is not None else None
    if raw is None:
        return BetTicketSurvivability(available=False)
    score = getattr(raw, "survivability_score", None)
    warning = getattr(raw, "low_survivability_warning", None)
    regime = getattr(raw, "volatility_regime", None)
    regime_value = regime.value if hasattr(regime, "value") else (str(regime) if regime else None)
    if score is None and warning is None and regime_value is None:
        return BetTicketSurvivability(available=False)
    return BetTicketSurvivability(
        available=True,
        survivability_score=score,
        low_survivability_warning=warning,
        volatility_regime=regime_value,
    )


def _solver_is_arbitrage(decision: PaperScanDecision) -> bool:
    if decision.depth_scan is not None and decision.depth_scan.solution.is_arbitrage:
        return True
    return bool(decision.payoff_scan is not None and decision.payoff_scan.solution.is_arbitrage)


def _complete_opening_fills(
    fills: PaperOpportunityFills,
    required: list[PaperOpportunityLeg],
) -> bool:
    if not fills.fully_filled or len(fills.fills) != len(required):
        return False
    fill_ids = {
        (fill.venue, fill.outcome, fill.source_market_id, fill.source_runner_id)
        for fill in fills.fills
        if fill.filled_stake > 0 and fill.fully_filled
    }
    required_ids = {
        (leg.venue, leg.outcome, leg.source_market_id, leg.source_runner_id)
        for leg in required
    }
    return fill_ids == required_ids


def _opening_guaranteed_profit(plan: PaperFillPlan) -> Decimal | None:
    allocation = plan.decision.allocation
    if allocation is not None and allocation.accepted and allocation.guaranteed_profit > 0:
        return allocation.guaranteed_profit
    if plan.decision.depth_scan is not None and plan.decision.depth_scan.solution.is_arbitrage:
        return plan.decision.depth_scan.solution.guaranteed_profit
    if plan.decision.payoff_scan is not None and plan.decision.payoff_scan.solution.is_arbitrage:
        return plan.decision.payoff_scan.solution.minimum_state_pnl
    return None


def _allocated_stake_labels(legs: list[PaperOpportunityLeg]) -> dict[str, Decimal]:
    labels: dict[str, Decimal] = {}
    for leg in legs:
        labels[f"{leg.venue.value}:{leg.outcome}"] = leg.requested_stake
    return labels


def _allocated_stake_labels_from_trade(trade: PaperTrade) -> dict[str, Decimal]:
    return {
        f"{leg.venue.value}:{leg.outcome}": leg.requested_stake
        for leg in trade.legs
        if leg.requested_stake > 0
    }


def _dec_fingerprint(value: Decimal | None) -> str:
    if value is None:
        return ""
    return format(value, "f")


def _prepared_fingerprint(preview: PreparedPaperDeployment) -> tuple:
    legs = tuple(
        (
            leg.venue.value,
            leg.outcome,
            leg.source_market_id,
            leg.source_runner_id or "",
            _dec_fingerprint(leg.stake_native),
            _dec_fingerprint(leg.capital_native),
            _dec_fingerprint(leg.displayed_odds),
        )
        for leg in preview.legs
    )
    return (
        preview.accepted,
        _dec_fingerprint(preview.requested_size_gbp),
        _dec_fingerprint(preview.applied_size_gbp),
        legs,
    )


def _plan_with_prepared_stakes(
    plan: PaperFillPlan,
    preview: PreparedPaperDeployment,
) -> PaperFillPlan:
    by_key = {
        (leg.venue, leg.outcome, leg.source_market_id, leg.source_runner_id or ""): leg
        for leg in preview.legs
    }
    resized: list[PaperOpportunityLeg] = []
    seen: set[tuple] = set()
    for plan_leg in plan.legs:
        key = (
            plan_leg.venue,
            plan_leg.outcome,
            plan_leg.source_market_id,
            plan_leg.source_runner_id or "",
        )
        prepared = by_key.get(key)
        if prepared is None:
            resized.append(plan_leg.model_copy(update={"requested_stake": Decimal(0)}))
            continue
        seen.add(key)
        resized.append(plan_leg.model_copy(update={"requested_stake": prepared.stake_native}))
    if set(by_key) - seen:
        raise PaperOperationsError("prepared_deployment_stale")
    return plan.model_copy(update={"legs": resized})

