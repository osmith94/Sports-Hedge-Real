from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

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
from sports_hedge.treasury.service import PaperTreasuryError
from sports_hedge.treasury.models import TreasuryLockRequest, UnwindReleaseLeg, ValidatedUnwindResult
from sports_hedge.accounting.strategy_books import DimensionedPosting
from sports_hedge.arbitrage.priority_alerts.fixed_exposure import revalidate_fixed_external_exposure
from sports_hedge.arbitrage.priority_alerts.models import (
    ExternalLegConfirmation,
    LegExecutionMode,
    PriorityAlertCandidate,
    PriorityLeg,
)
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.application.paper_scan import FillPlanMappingError, apply_allocation_to_fill_legs
from sports_hedge.arbitrage.watchlist.models import NearOpportunity, OpportunityStatus
from sports_hedge.arbitrage.watchlist.service import WatchlistService, _opportunity_id
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import MarketAction
from sports_hedge.paper.chain import (
    PaperChainStep,
    PaperChainTrace,
    PaperFillPlan,
    SimulatePaperFillResult,
)
from sports_hedge.paper.fills import PaperFillConfig, PaperFillRecord, PaperOpportunityFills, PaperOpportunityLeg
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision
from sports_hedge.paper.settlement import PaperSettlementError, compute_paper_settlement
from sports_hedge.paper.simulator import PaperFillSimulator
from sports_hedge.paper.trades import (
    PaperLegFillKind,
    PaperSettlementRequest,
    PaperTrade,
    PaperTradeAuditEvent,
    PaperTradeAuditEventType,
    PaperTradeBookSummary,
    PaperTradeDetail,
    PaperTradeLeg,
    PaperTradeState,
)
from sports_hedge.paper.unwind import (
    PaperUnwindEngine,
    UnwindDecision,
    UnwindEvaluationRequest,
    UnwindIdentityError,
    position_from_trade,
)
from sports_hedge.paper.unwind.models import CapitalScarcityInput, RemainingLockSource, ReverseQuote, UnwindPolicy, UnwindRecommendation
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger, SqlitePaperTradeRepository


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
        self._external_confirmations: dict[str, ExternalLegConfirmation] = {}
        self._entry_rejections: dict[str, str] = {}

    def persist_triggered_chain(
        self,
        decision: PaperScanDecision,
        *,
        provenance: DataProvenance = DataProvenance.LIVE_PAPER,
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
            if self.settings.paper_autofill_enabled:
                try:
                    self._require_allocator_sized_plan(opportunity_id)
                    self.simulate_fill(
                        opportunity_id,
                        simulate_external=True,
                        provenance=provenance,
                        operator_note="PAPER-ONLY autofill; no venue order placed",
                    )
                except PaperOperationsError:
                    pass
        return candidate

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
    ) -> SimulatePaperFillResult:
        simulated_at = now or datetime.now(UTC)
        require_complete = simulate_external
        existing = self._get_trade_by_opportunity(opportunity_id)
        if existing is not None and existing.state in {
            PaperTradeState.OPEN,
            PaperTradeState.PARTIAL,
            PaperTradeState.CLOSED,
        }:
            return self._result_from_existing_trade(existing, simulated_at)

        plan = self._plans.get(opportunity_id)
        if plan is None:
            raise PaperOperationsError("missing_paper_fill_plan")
        current = self.watchlist.repository.get(opportunity_id)
        if current is None:
            raise PaperOperationsError("unknown_opportunity")
        presented = self.watchlist._present_freshness(current, simulated_at)
        if presented.status not in {
            OpportunityStatus.TRIGGERED,
            OpportunityStatus.PAPER_FILLING,
            OpportunityStatus.PARTIAL,
        }:
            self._fail_entry(opportunity_id, "stale_before_fill", simulated_at)
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

        opportunity = self.watchlist.record_paper_fill(
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
        trade.capital_locked_gbp = Decimal("0")
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
    ) -> PaperTradeDetail:
        """PAPER-ONLY: post a fully validated 8D close through 8E. Never places orders."""

        if self.ledger is None or self.trades is None:
            raise PaperOperationsError("paper_trade_repository_unavailable")
        occurred = now or datetime.now(UTC)
        decision = self.evaluate_unwind(
            trade_id,
            quotes=quotes,
            fx=fx,
            policy=policy,
            scarcity=scarcity,
            evaluated_at=occurred,
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
        releases: list[UnwindReleaseLeg] = []
        for close_leg, open_leg in zip(decision.close_plan.legs, position.legs, strict=True):
            if not open_leg.fill_id:
                raise PaperOperationsError("missing_lock_identity")
            rate = self._lock_fx_rate(open_leg.venue, open_leg.native_currency, {
                item.currency: item for item in (fx if fx is not None else trade.fx_snapshots)
            })
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
            self.ledger.treasury.post_unwind(
                ValidatedUnwindResult(
                    trade_id=trade.trade_id,
                    close_completed=True,
                    opportunity_id=trade.opportunity_id,
                    source="paper_unwind",
                    source_id=f"unwind:{trade.trade_id}",
                    reason="validated paper unwind",
                    releases=releases,
                ),
                now=occurred,
            )
        except PaperTreasuryError as exc:
            raise PaperOperationsError(str(exc)) from exc
        trade.state = PaperTradeState.CLOSED
        trade.settled_at = occurred
        trade.last_updated_at = occurred
        trade.realised_pnl_gbp = decision.validated_exit_pnl_gbp
        trade.settlement_source = "paper_unwind"
        trade.settlement_source_id = f"unwind:{trade.trade_id}"
        trade.settlement_detail = (
            f"validated unwind {decision.recommendation.value}; "
            "capital released only after 8E postings"
        )
        trade.capital_locked_native = {}
        trade.capital_locked_gbp = Decimal("0")
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
            trade.capital_locked_gbp = Decimal("0")
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
        minutes = hours * Decimal("60")
        basis_label = allocation.expected_lock_basis or "8C modelled estimate"
        return position.model_copy(
            update={
                "remaining_lock_minutes": minutes,
                "remaining_lock_basis": RemainingLockSource.MODELLED,
                "remaining_lock_detail": (
                    f"{basis_label}; advisory only; does not release capital"
                ),
            }
        )

    def book_summary(self) -> PaperTradeBookSummary:
        active = self.list_active_trades()
        closed = self.list_closed_trades()
        native: dict[str, Decimal] = {}
        gbp_locked = Decimal("0")
        gbp_ok = True
        for trade in active:
            for currency, amount in trade.capital_locked_native.items():
                native[currency] = native.get(currency, Decimal("0")) + amount
            if trade.capital_locked_gbp is None:
                gbp_ok = False
            else:
                gbp_locked += trade.capital_locked_gbp
        realised = sum((trade.realised_pnl_gbp or Decimal("0") for trade in closed), Decimal("0"))
        return PaperTradeBookSummary(
            open_count=sum(1 for trade in active if trade.state is not PaperTradeState.AWAITING_MANUAL_EXTERNAL),
            closed_count=len(closed),
            awaiting_manual_external_count=sum(
                1 for trade in active if trade.state is PaperTradeState.AWAITING_MANUAL_EXTERNAL
            ),
            capital_locked_native=native,
            capital_locked_gbp=gbp_locked if gbp_ok else None,
            realised_pnl_gbp=realised if closed else Decimal("0"),
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
            needed[key] = needed.get(key, Decimal("0")) + leg.requested_stake
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
        gbp = Decimal("0")
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
            filled_stake = Decimal("0")
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
                native[plan_leg.currency] = native.get(plan_leg.currency, Decimal("0")) + filled_stake
                rate = Decimal("1") if plan_leg.currency == "GBP" else fx[plan_leg.currency].gbp_per_unit
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
        elif partial:
            trade.state = PaperTradeState.PARTIAL
            trade.guaranteed_profit_gbp_at_open = None
        else:
            trade.state = PaperTradeState.PENDING
            trade.guaranteed_profit_gbp_at_open = None
        if autofill:
            trade.audit.append(
                PaperTradeAuditEvent(
                    occurred_at=occurred_at,
                    event_type=PaperTradeAuditEventType.PAPER_AUTOFILL,
                    detail="configurable paper autofill; simulated only",
                )
            )
        if simulate_external and any(
            leg.fill_kind is PaperLegFillKind.PAPER_SIMULATED_EXTERNAL for leg in legs
        ):
            trade.audit.append(
                PaperTradeAuditEvent(
                    occurred_at=occurred_at,
                    event_type=PaperTradeAuditEventType.PAPER_SIMULATED_EXTERNAL_FILL,
                    detail="PAPER_SIMULATED_EXTERNAL is not MANUAL_EXTERNAL confirmation",
                )
            )
        if confirmation is not None:
            trade.audit.append(
                PaperTradeAuditEvent(
                    occurred_at=occurred_at,
                    event_type=PaperTradeAuditEventType.MANUAL_EXTERNAL_CONFIRMED,
                    detail=f"reference={confirmation.operator_counterparty_reference}",
                )
            )
            trade.audit.append(
                PaperTradeAuditEvent(
                    occurred_at=occurred_at,
                    event_type=PaperTradeAuditEventType.HEDGE_REVALIDATED,
                    detail="remaining hedge revalidated after MANUAL_EXTERNAL confirmation",
                )
            )
        trade.audit.append(
            PaperTradeAuditEvent(
                occurred_at=occurred_at,
                event_type=PaperTradeAuditEventType.FILLS_RECORDED,
                detail=f"state={trade.state.value}",
            )
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
        gbp_per_unit = Decimal("1") if confirmation.currency.upper() == "GBP" else rate_snap.gbp_per_unit
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
            rate = Decimal("1") if leg.currency == "GBP" else (snap.gbp_per_unit if snap else None)
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
                            Decimal("1") if currency == "GBP" else amount_gbp / native
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
            return Decimal("1")
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
                filled_stake=Decimal("0"),
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
            totals.get(posting.dimensions.currency, Decimal("0")) + posting.amount_native
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
    native_max = sum((level.available_stake for level in leg.levels), Decimal("0")) or leg.requested_stake
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
        rate = fx.get(currency, Decimal("1"))
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
