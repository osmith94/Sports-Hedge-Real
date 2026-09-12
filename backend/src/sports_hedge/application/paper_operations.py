from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sports_hedge.accounting.dimensions import CapitalSource
from sports_hedge.accounting.paper_journal import (
    DataProvenance,
    PaperJournal,
    PaperJournalEntry,
    cash_lock_postings,
    gbp_is_balanced,
)
from sports_hedge.accounting.strategy_books import DimensionedPosting
from sports_hedge.arbitrage.priority_alerts.fixed_exposure import revalidate_fixed_external_exposure
from sports_hedge.arbitrage.priority_alerts.models import (
    ExternalLegConfirmation,
    LegExecutionMode,
    PriorityAlertCandidate,
    PriorityLeg,
)
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.watchlist.models import OpportunityStatus
from sports_hedge.arbitrage.watchlist.service import WatchlistService, _opportunity_id
from sports_hedge.config import Settings, get_settings
from sports_hedge.paper.chain import (
    PaperChainStep,
    PaperChainTrace,
    PaperFillPlan,
    SimulatePaperFillResult,
)
from sports_hedge.paper.fills import PaperFillConfig, PaperOpportunityFills, PaperOpportunityLeg
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.paper.simulator import PaperFillSimulator


class PaperOperationsError(ValueError):
    """Fail-closed paper operational chain."""


class PaperOperationsService:
    """Wire scan → optional priority alert → explicit paper fill → journal.

    Never places venue orders, signs wallets, or treats MANUAL_EXTERNAL as automated.
    """

    def __init__(
        self,
        *,
        watchlist: WatchlistService,
        alerts: PriorityAlertService | None = None,
        journal: PaperJournal | None = None,
        simulator: PaperFillSimulator | None = None,
        settings: Settings | None = None,
    ) -> None:
        self.watchlist = watchlist
        self.alerts = alerts or PriorityAlertService()
        self.journal = journal or PaperJournal()
        self.simulator = simulator or PaperFillSimulator()
        self.settings = settings or get_settings()
        self._plans: dict[str, PaperFillPlan] = {}
        self._external_confirmations: dict[str, ExternalLegConfirmation] = {}

    def persist_triggered_chain(
        self,
        decision: PaperScanDecision,
        *,
        provenance: DataProvenance = DataProvenance.LIVE_PAPER,
    ) -> PriorityAlertCandidate | None:
        if not decision.canonical_market_id:
            return None
        opportunity_id = _opportunity_id(decision.canonical_market_id)
        if decision.fill_legs:
            self._plans[opportunity_id] = PaperFillPlan(
                opportunity_id=opportunity_id,
                canonical_event_id=decision.canonical_event_id,
                canonical_market_id=decision.canonical_market_id,
                scanned_at=decision.scanned_at,
                quote_age_ms=decision.quote_age_ms,
                eligible_for_paper_simulation=decision.eligible_for_paper_simulation,
                settlement_equivalent=decision.market_match.matched,
                legs=list(decision.fill_legs),
                execution_modes={
                    venue: LegExecutionMode(mode)
                    for venue, mode in decision.execution_modes.items()
                },
                venue_costs=list(decision.venue_costs),
                fx_snapshots=list(decision.fx_snapshots),
                decision=decision,
                provenance=provenance,
            )
        if not decision.eligible_for_paper_simulation or decision.depth_scan is None:
            return None
        if not decision.depth_scan.solution.is_arbitrage:
            return None
        candidate = _candidate_from_decision(decision, opportunity_id)
        self.alerts.ingest(candidate)
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
    ) -> SimulatePaperFillResult:
        simulated_at = now or datetime.now(UTC)
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
            raise PaperOperationsError("stale_before_fill")

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
        if external_venues:
            if confirmation is None:
                raise PaperOperationsError("manual_external_confirmation_required")
            if confirmation.venue not in external_venues:
                raise PaperOperationsError("external_confirmation_venue_mismatch")
            self._external_confirmations[opportunity_id] = confirmation
            hedge_valid = self._revalidate_remaining_hedge(plan, confirmation)
            if not hedge_valid:
                raise PaperOperationsError("remaining_hedge_revalidation_failed")

        internal_legs = [
            leg for leg in plan.legs if modes.get(leg.venue, LegExecutionMode.INTERNAL)
            is LegExecutionMode.INTERNAL
        ]
        if not internal_legs:
            raise PaperOperationsError("no_internal_paper_legs")

        fills = self.simulator.simulate(
            internal_legs,
            fill_config,
            opportunity_id=opportunity_id,
            now=simulated_at,
        )
        stage = _fill_stage(fills)
        if stage is None:
            raise PaperOperationsError(
                fills.rejection_reasons[0] if fills.rejection_reasons else "paper_fill_rejected"
            )
        opportunity = self.watchlist.record_paper_fill(
            opportunity_id,
            stage=stage,
            occurred_at=simulated_at,
            detail=operator_note,
        )
        journals = self._post_fills(
            plan,
            fills,
            confirmation=confirmation if external_venues else None,
            capital_source=capital_source,
            occurred_at=simulated_at,
            provenance=provenance,
        )
        alert = None
        existing_id = self.alerts._by_opportunity.get(opportunity_id)
        if existing_id:
            alert = self.alerts.get_alert(existing_id)
        postings = self.journal.postings(opportunity_id=opportunity_id)
        native_totals = _native_totals(postings)
        trace = PaperChainTrace(
            opportunity_id=opportunity_id,
            steps=_trace_steps(alert is not None, confirmation is not None, bool(journals)),
            scan_eligible=plan.eligible_for_paper_simulation,
            watchlist_status=opportunity.status,
            alert_id=alert.alert_id if alert is not None else None,
            fill_ids=[fill.fill_id for fill in fills.fills],
            journal_ids=[entry.journal_id for entry in journals],
            balanced_gbp=gbp_is_balanced(postings),
            native_totals=native_totals,
            provenance=provenance,
            detail="scan -> alert -> explicit simulated fill -> fill records -> balanced postings",
        )
        return SimulatePaperFillResult(
            opportunity=opportunity,
            fills=fills,
            journals=journals,
            alert=alert,
            hedge_still_valid=hedge_valid,
            trace=trace,
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
    ) -> list[PaperJournalEntry]:
        entries: list[PaperJournalEntry] = []
        fx = {item.currency: item for item in plan.fx_snapshots}
        for fill in fills.fills:
            if fill.filled_stake <= 0:
                continue
            rate = Decimal("1") if fill.currency == "GBP" else fx[fill.currency].gbp_per_unit
            amount_gbp = fill.filled_stake * rate
            entries.append(
                self.journal.append(
                    PaperJournalEntry(
                        source="paper_fill_simulator",
                        source_id=fill.fill_id,
                        occurred_at=occurred_at,
                        description="PAPER-ONLY simulated internal fill cash lock",
                        opportunity_id=plan.opportunity_id,
                        provenance=provenance,
                        postings=cash_lock_postings(
                            venue=fill.venue,
                            currency=fill.currency,
                            amount_native=fill.filled_stake,
                            amount_gbp=amount_gbp,
                            fx_rate_gbp_per_unit=rate,
                            opportunity_id=plan.opportunity_id,
                            capital_source=capital_source,
                            canonical_event_id=plan.canonical_event_id,
                            position_id=fill.fill_id,
                        ),
                    )
                )
            )
        if confirmation is not None:
            rate = (
                Decimal("1")
                if confirmation.currency == "GBP"
                else fx[confirmation.currency].gbp_per_unit
            )
            entries.append(
                self.journal.append(
                    PaperJournalEntry(
                        source="manual_external_confirmation",
                        source_id=f"{plan.opportunity_id}:{confirmation.operator_counterparty_reference}",
                        occurred_at=confirmation.executed_at,
                        description="Operator-recorded MANUAL_EXTERNAL fill; Sports Hedge did not place this leg",
                        opportunity_id=plan.opportunity_id,
                        provenance=provenance,
                        postings=cash_lock_postings(
                            venue=confirmation.venue,
                            currency=confirmation.currency,
                            amount_native=confirmation.executed_size,
                            amount_gbp=confirmation.executed_size * rate,
                            fx_rate_gbp_per_unit=rate,
                            opportunity_id=plan.opportunity_id,
                            capital_source=CapitalSource.MANUAL_EXTERNAL,
                            canonical_event_id=plan.canonical_event_id,
                            position_id=confirmation.operator_counterparty_reference,
                        ),
                    )
                )
            )
        return entries


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


def _trace_steps(has_alert: bool, has_external: bool, has_journal: bool) -> list[PaperChainStep]:
    steps = [PaperChainStep.SCAN_DECISION]
    if has_alert:
        steps.append(PaperChainStep.PRIORITY_ALERT)
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
    return leg.displayed_odds


def _priority_leg_from_plan(
    plan: PaperFillPlan, leg: PaperOpportunityLeg, *, gbp_per_unit: Decimal
) -> PriorityLeg:
    native_max = sum((level.available_stake for level in leg.levels), Decimal("0")) or leg.requested_stake
    return PriorityLeg(
        outcome=leg.outcome,
        venue=leg.venue,
        source_market_id=leg.source_market_id,
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
