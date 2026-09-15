"""Server-owned paper position manager.

Evaluates OPEN paper trades against exact reverse-side books. Automatic close
is paper-only and always goes through ``complete_validated_unwind()``.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Sequence
from datetime import UTC, datetime

from sports_hedge.application.paper_operations import PaperOperationsError, PaperOperationsService
from sports_hedge.arbitrage.allocation.policy import policy_from_settings
from sports_hedge.config import Settings, get_settings
from sports_hedge.fees.resolver import VenueCostResolver
from sports_hedge.paper.position_management.models import (
    CloseFeeByVenue,
    CompetingOpportunityInput,
    PositionManagementAutoAction,
    PositionManagementCycleResult,
    PositionManagementSnapshot,
)
from sports_hedge.paper.position_management.quotes import (
    LatestObservationCatalog,
    reverse_quotes_for_position,
)
from sports_hedge.paper.position_management.scarcity import build_capital_scarcity
from sports_hedge.paper.trades import (
    PaperLegFillKind,
    PaperTrade,
    PaperTradeAuditEvent,
    PaperTradeAuditEventType,
    PaperTradeState,
)
from sports_hedge.paper.unwind import PaperUnwindEngine, UnwindIdentityError, position_from_trade
from sports_hedge.paper.unwind.models import (
    CapitalScarcityInput,
    OpenPaperPosition,
    ReverseQuote,
    UnwindDecision,
    UnwindEvaluationRequest,
    UnwindPolicy,
    UnwindRecommendation,
    venue_currency_key,
)

QuoteProvider = Callable[[OpenPaperPosition], Sequence[ReverseQuote]]


class PaperPositionManager:
    """One server-owned paper position-management step. No venue writes."""

    def __init__(
        self,
        operations: PaperOperationsService,
        *,
        settings: Settings | None = None,
        observation_catalog: LatestObservationCatalog | None = None,
        engine: PaperUnwindEngine | None = None,
        cost_resolver: VenueCostResolver | None = None,
    ) -> None:
        self.operations = operations
        self.settings = settings or operations.settings or get_settings()
        self.catalog = observation_catalog or LatestObservationCatalog()
        self.engine = engine or PaperUnwindEngine()
        self.cost_resolver = cost_resolver
        self._in_flight: set[str] = set()
        self._lock = threading.Lock()

    def manage_open_positions(
        self,
        *,
        quotes_for: QuoteProvider | None = None,
        quotes_by_trade: dict[str, Sequence[ReverseQuote]] | None = None,
        competing: Sequence[CompetingOpportunityInput] | None = None,
        policy: UnwindPolicy | None = None,
        scarcity: CapitalScarcityInput | None = None,
        auto_unwind: bool | None = None,
        now: datetime | None = None,
        second_quotes_for: QuoteProvider | None = None,
    ) -> list[PositionManagementCycleResult]:
        occurred = now or datetime.now(UTC)
        enabled = self.settings.paper_auto_unwind_enabled if auto_unwind is None else auto_unwind
        unwind_policy = policy or UnwindPolicy()
        results: list[PositionManagementCycleResult] = []
        if self.operations.trades is None:
            return results
        for trade in self.operations.list_active_trades():
            results.append(
                self.manage_trade(
                    trade.trade_id,
                    quotes_for=quotes_for,
                    quotes=None if quotes_by_trade is None else quotes_by_trade.get(trade.trade_id),
                    competing=competing,
                    policy=unwind_policy,
                    scarcity=scarcity,
                    auto_unwind=enabled,
                    now=occurred,
                    second_quotes_for=second_quotes_for,
                )
            )
        return results

    def manage_trade(
        self,
        trade_id: str,
        *,
        quotes_for: QuoteProvider | None = None,
        quotes: Sequence[ReverseQuote] | None = None,
        competing: Sequence[CompetingOpportunityInput] | None = None,
        policy: UnwindPolicy | None = None,
        scarcity: CapitalScarcityInput | None = None,
        auto_unwind: bool | None = None,
        now: datetime | None = None,
        second_quotes_for: QuoteProvider | None = None,
    ) -> PositionManagementCycleResult:
        occurred = now or datetime.now(UTC)
        enabled = self.settings.paper_auto_unwind_enabled if auto_unwind is None else auto_unwind
        unwind_policy = policy or UnwindPolicy()
        if self.operations.trades is None:
            raise PaperOperationsError("paper_trade_repository_unavailable")
        with self._lock:
            if trade_id in self._in_flight:
                trade = self.operations.trades.get(trade_id)
                if trade is None:
                    raise PaperOperationsError("unknown_trade")
                snapshot = trade.position_management or _empty_snapshot(
                    trade_id, occurred, "concurrent_cycle_in_flight"
                )
                return PositionManagementCycleResult(
                    trade_id=trade_id,
                    snapshot=snapshot,
                    aborted_reason="concurrent_cycle_in_flight",
                )
            self._in_flight.add(trade_id)
        try:
            return self._manage_locked(
                trade_id,
                quotes_for=quotes_for,
                quotes=quotes,
                competing=list(competing or []),
                policy=unwind_policy,
                scarcity=scarcity,
                auto_unwind=enabled,
                occurred=occurred,
                second_quotes_for=second_quotes_for,
            )
        finally:
            with self._lock:
                self._in_flight.discard(trade_id)

    def _manage_locked(
        self,
        trade_id: str,
        *,
        quotes_for: QuoteProvider | None,
        quotes: Sequence[ReverseQuote] | None,
        competing: list[CompetingOpportunityInput],
        policy: UnwindPolicy,
        scarcity: CapitalScarcityInput | None,
        auto_unwind: bool,
        occurred: datetime,
        second_quotes_for: QuoteProvider | None,
    ) -> PositionManagementCycleResult:
        trade = self.operations.trades.get(trade_id)
        if trade is None:
            raise PaperOperationsError("unknown_trade")
        if trade.state is PaperTradeState.CLOSED:
            snapshot = trade.position_management or _empty_snapshot(
                trade_id, occurred, "trade_already_closed"
            )
            return PositionManagementCycleResult(trade_id=trade_id, snapshot=snapshot)

        awaiting = trade.state is PaperTradeState.AWAITING_MANUAL_EXTERNAL
        manual = _has_manual_external(trade)
        auto_close_allowed = (
            not awaiting
            and not manual
            and trade.state is PaperTradeState.OPEN
        )
        try:
            position = position_from_trade(trade)
        except UnwindIdentityError as exc:
            snapshot = _empty_snapshot(trade_id, occurred, str(exc), auto_unwind_enabled=auto_unwind)
            self._persist_snapshot(trade, snapshot, occurred, audit=True)
            return PositionManagementCycleResult(trade_id=trade_id, snapshot=snapshot)
        position = self.operations._overlay_modelled_remaining_lock(trade, position)

        resolved_quotes = self._resolve_quotes(
            position, quotes_for=quotes_for, quotes=quotes, occurred=occurred
        )
        resolved_scarcity = scarcity or self._scarcity_for(trade, competing)
        if not resolved_quotes:
            snapshot = _empty_snapshot(
                trade_id,
                occurred,
                "missing_reverse_quote",
                auto_unwind_enabled=auto_unwind,
            )
            snapshot = snapshot.model_copy(
                update={
                    "auto_close_allowed": auto_close_allowed,
                    "auto_action": (
                        PositionManagementAutoAction.SKIPPED_AWAITING_MANUAL_EXTERNAL
                        if awaiting
                        else PositionManagementAutoAction.SKIPPED_MANUAL_EXTERNAL
                        if manual
                        else PositionManagementAutoAction.ADVISORY_ONLY
                        if not auto_unwind
                        else PositionManagementAutoAction.NONE
                    ),
                    "opportunity_cost_gbp": resolved_scarcity.opportunity_cost_gbp,
                    "opportunity_cost_detail": resolved_scarcity.detail,
                    "capital_pressure": resolved_scarcity.pressure,
                    "hold_pnl_gbp": position.hold_pnl_gbp,
                }
            )
            previous = trade.position_management
            self._persist_snapshot(
                trade,
                snapshot,
                occurred,
                audit=_recommendation_changed(previous, snapshot),
            )
            return PositionManagementCycleResult(trade_id=trade_id, snapshot=snapshot)
        decision = self._evaluate(position, resolved_quotes, policy, resolved_scarcity, occurred)
        snapshot = snapshot_from_decision(
            decision,
            evaluated_at=occurred,
            auto_unwind_enabled=auto_unwind,
            auto_close_allowed=auto_close_allowed,
            auto_action=(
                PositionManagementAutoAction.SKIPPED_AWAITING_MANUAL_EXTERNAL
                if awaiting
                else PositionManagementAutoAction.SKIPPED_MANUAL_EXTERNAL
                if manual
                else PositionManagementAutoAction.ADVISORY_ONLY
                if not auto_unwind
                else PositionManagementAutoAction.NONE
            ),
            opportunity_cost_detail=resolved_scarcity.detail,
        )
        previous = trade.position_management
        material = _recommendation_changed(previous, snapshot)
        self._persist_snapshot(trade, snapshot, occurred, audit=material)

        if awaiting:
            return PositionManagementCycleResult(trade_id=trade_id, snapshot=snapshot)
        if manual:
            return PositionManagementCycleResult(trade_id=trade_id, snapshot=snapshot)
        if not auto_unwind or snapshot.recommendation is not UnwindRecommendation.UNWIND_ELIGIBLE:
            return PositionManagementCycleResult(trade_id=trade_id, snapshot=snapshot)

        return self._attempt_auto_unwind(
            trade_id,
            position=position,
            first=decision,
            first_quotes=resolved_quotes,
            quotes_for=second_quotes_for or quotes_for,
            policy=policy,
            scarcity=resolved_scarcity,
            occurred=occurred,
            snapshot=snapshot,
        )

    def _attempt_auto_unwind(
        self,
        trade_id: str,
        *,
        position: OpenPaperPosition,
        first: UnwindDecision,
        first_quotes: list[ReverseQuote],
        quotes_for: QuoteProvider | None,
        policy: UnwindPolicy,
        scarcity: CapitalScarcityInput,
        occurred: datetime,
        snapshot: PositionManagementSnapshot,
    ) -> PositionManagementCycleResult:
        trade = self.operations.trades.get(trade_id)
        if trade is None:
            raise PaperOperationsError("unknown_trade")
        self._append_audit(
            trade,
            occurred,
            PaperTradeAuditEventType.UNWIND_ATTEMPTED,
            "automatic paper unwind second revalidation",
        )
        self.operations.trades.save(trade)
        second_quotes, fresh_fail = self._fresh_quotes(
            position,
            quotes_for=quotes_for,
            first_quotes=first_quotes,
            occurred=occurred,
        )
        if fresh_fail is not None:
            return self._abort_revalidation(
                trade,
                first,
                occurred,
                scarcity,
                fresh_fail,
            )
        second = self._evaluate(position, second_quotes, policy, scarcity, occurred)
        abort = _revalidation_abort_reason(first, second)
        if abort is not None:
            return self._abort_revalidation(
                trade,
                second,
                occurred,
                scarcity,
                abort,
            )
        try:
            self.operations.complete_validated_unwind(
                trade_id,
                quotes=second_quotes,
                policy=policy,
                scarcity=scarcity,
                now=occurred,
                record_evaluation_audit=False,
            )
        except PaperOperationsError as exc:
            reason = str(exc)
            snapshot = snapshot_from_decision(
                second,
                evaluated_at=occurred,
                auto_unwind_enabled=True,
                auto_close_allowed=True,
                auto_action=PositionManagementAutoAction.UNWIND_ABORTED,
                opportunity_cost_detail=scarcity.detail,
            )
            self._persist_snapshot(trade, snapshot, occurred, audit=True, extra_detail=reason)
            self._append_audit(
                trade,
                occurred,
                PaperTradeAuditEventType.UNWIND_ABORTED,
                reason,
            )
            self.operations.trades.save(trade)
            return PositionManagementCycleResult(
                trade_id=trade_id,
                snapshot=snapshot,
                aborted_reason=reason,
            )
        closed = self.operations.trades.get(trade_id)
        if closed is None:
            raise PaperOperationsError("unknown_trade")
        snapshot = snapshot_from_decision(
            second,
            evaluated_at=occurred,
            auto_unwind_enabled=True,
            auto_close_allowed=True,
            auto_action=PositionManagementAutoAction.UNWIND_COMPLETED,
            opportunity_cost_detail=scarcity.detail,
        )
        closed.position_management = snapshot
        self.operations.trades.save(closed)
        return PositionManagementCycleResult(
            trade_id=trade_id,
            snapshot=snapshot,
            mutated=True,
        )

    def _abort_revalidation(
        self,
        trade: PaperTrade,
        decision: UnwindDecision,
        occurred: datetime,
        scarcity: CapitalScarcityInput,
        reason: str,
    ) -> PositionManagementCycleResult:
        snapshot = snapshot_from_decision(
            decision,
            evaluated_at=occurred,
            auto_unwind_enabled=True,
            auto_close_allowed=True,
            auto_action=PositionManagementAutoAction.UNWIND_ABORTED,
            opportunity_cost_detail=scarcity.detail,
        )
        snapshot = snapshot.model_copy(
            update={
                "recommendation": UnwindRecommendation.UNWIND_NOT_SAFE,
                "decision_reason": reason,
                "close_executable": False,
                "releasable_native": {},
            }
        )
        self._persist_snapshot(trade, snapshot, occurred, audit=True, extra_detail=reason)
        self._append_audit(
            trade,
            occurred,
            PaperTradeAuditEventType.UNWIND_ABORTED,
            reason,
        )
        self.operations.trades.save(trade)
        return PositionManagementCycleResult(
            trade_id=trade.trade_id,
            snapshot=snapshot,
            aborted_reason=reason,
        )

    def _evaluate(
        self,
        position: OpenPaperPosition,
        quotes: Sequence[ReverseQuote],
        policy: UnwindPolicy,
        scarcity: CapitalScarcityInput,
        occurred: datetime,
    ) -> UnwindDecision:
        return self.engine.evaluate(
            UnwindEvaluationRequest(
                position=position,
                quotes=list(quotes),
                fx=self._fx_for(position.trade_id),
                policy=policy,
                scarcity=scarcity,
                evaluated_at=occurred,
            )
        )

    def _resolve_quotes(
        self,
        position: OpenPaperPosition,
        *,
        quotes_for: QuoteProvider | None,
        quotes: Sequence[ReverseQuote] | None,
        occurred: datetime | None = None,
    ) -> list[ReverseQuote]:
        if quotes is not None:
            return list(quotes)
        if quotes_for is not None:
            return list(quotes_for(position))
        return reverse_quotes_for_position(
            position,
            self.catalog.observations(),
            cost_resolver=self.cost_resolver,
            evaluated_at=occurred or datetime.now(UTC),
        )

    def _fresh_quotes(
        self,
        position: OpenPaperPosition,
        *,
        quotes_for: QuoteProvider | None,
        first_quotes: Sequence[ReverseQuote],
        occurred: datetime,
    ) -> tuple[list[ReverseQuote], str | None]:
        """Independent second-pass reverse books. Never reuse first-pass facts."""

        if quotes_for is not None:
            second = list(quotes_for(position))
        else:
            second = reverse_quotes_for_position(
                position,
                self.catalog.observations(),
                cost_resolver=self.cost_resolver,
                evaluated_at=occurred,
            )
        return second, _second_pass_freshness_failure(position, list(first_quotes), second)

    def _scarcity_for(
        self,
        trade: PaperTrade,
        competing: Sequence[CompetingOpportunityInput],
    ) -> CapitalScarcityInput:
        locked = {
            venue_currency_key(leg.venue, leg.currency)
            for leg in trade.legs
            if leg.filled_stake > 0
        }
        snapshot = None
        if self.operations.ledger is not None:
            snapshot = self.operations.ledger.treasury.snapshot()
        fx = {item.currency: item.gbp_per_unit for item in trade.fx_snapshots}
        return build_capital_scarcity(
            treasury_snapshot=snapshot,
            policy=policy_from_settings(self.settings),
            competing=list(competing),
            locked_venue_keys=locked,
            gbp_per_unit=fx,
        )

    def _fx_for(self, trade_id: str):
        trade = self.operations.trades.get(trade_id) if self.operations.trades is not None else None
        if trade is None:
            return []
        return list(trade.fx_snapshots)

    def _persist_snapshot(
        self,
        trade: PaperTrade,
        snapshot: PositionManagementSnapshot,
        occurred: datetime,
        *,
        audit: bool,
        extra_detail: str | None = None,
    ) -> None:
        trade.position_management = snapshot
        if audit:
            self._append_audit(
                trade,
                occurred,
                PaperTradeAuditEventType.POSITION_MANAGEMENT_CHANGED,
                extra_detail
                or f"{snapshot.recommendation.value}:{snapshot.decision_reason}",
            )
        if self.operations.trades is not None:
            self.operations.trades.save(trade)

    def _append_audit(
        self,
        trade: PaperTrade,
        occurred: datetime,
        event_type: PaperTradeAuditEventType,
        detail: str,
    ) -> None:
        trade.audit.append(
            PaperTradeAuditEvent(
                occurred_at=occurred,
                event_type=event_type,
                detail=detail,
            )
        )


def snapshot_from_decision(
    decision: UnwindDecision,
    *,
    evaluated_at: datetime,
    auto_unwind_enabled: bool,
    auto_close_allowed: bool,
    auto_action: PositionManagementAutoAction,
    opportunity_cost_detail: str | None = None,
) -> PositionManagementSnapshot:
    ages = [leg.quote_age_ms for leg in decision.close_plan.legs if leg.quote_age_ms is not None]
    bases = [leg.quote_age_basis for leg in decision.close_plan.legs if leg.quote_age_basis]
    fees = [
        CloseFeeByVenue(
            venue=leg.venue.value,
            native_currency=leg.native_currency,
            closing_fee_native=leg.closing_fee,
            fee_snapshot_id=leg.fee_snapshot_id,
            deferred_profit_commission=leg.deferred_profit_commission,
        )
        for leg in decision.close_plan.legs
    ]
    return PositionManagementSnapshot(
        trade_id=decision.trade_id,
        recommendation=decision.recommendation,
        decision_reason=decision.decision_reason,
        evaluated_at=evaluated_at,
        hold_pnl_gbp=decision.hold_pnl_gbp,
        validated_exit_pnl_gbp=decision.validated_exit_pnl_gbp,
        unwind_cost_gbp=decision.unwind_cost_gbp,
        closing_fees=fees,
        releasable_native=dict(decision.conditionally_releasable_by_venue_currency),
        capital_pressure=decision.capital_pressure,
        opportunity_cost_gbp=decision.opportunity_cost_gbp,
        opportunity_cost_detail=opportunity_cost_detail,
        close_executable=decision.close_plan.fully_executable,
        close_execution_risk_score=None if decision.execution_risk is None else decision.execution_risk.score,
        quote_age_ms=max(ages) if ages else None,
        quote_age_basis=bases[0] if bases else None,
        remaining_lock_minutes=decision.estimated_time_to_release.remaining_lock_minutes,
        remaining_lock_basis=decision.estimated_time_to_release.basis,
        remaining_lock_source_class=decision.estimated_time_to_release.source_class,
        remaining_lock_confidence=decision.estimated_time_to_release.confidence,
        remaining_lock_detail=decision.estimated_time_to_release.detail,
        expected_settlement_at=decision.estimated_time_to_release.expected_settlement_at,
        remaining_lock_advisory=True,
        incremental_close_capital_status=decision.incremental_close_capital_status,
        auto_action=auto_action,
        auto_unwind_enabled=auto_unwind_enabled,
        auto_close_allowed=auto_close_allowed,
        data_kind="modelled_paper_position_management",
    )


def _has_manual_external(trade: PaperTrade) -> bool:
    return any(leg.fill_kind is PaperLegFillKind.MANUAL_EXTERNAL for leg in trade.legs)


def _recommendation_changed(
    previous: PositionManagementSnapshot | None,
    current: PositionManagementSnapshot,
) -> bool:
    if previous is None:
        return True
    return (
        previous.recommendation is not current.recommendation
        or previous.decision_reason != current.decision_reason
        or previous.auto_action is not current.auto_action
    )


def _revalidation_abort_reason(first: UnwindDecision, second: UnwindDecision) -> str | None:
    if second.recommendation is not UnwindRecommendation.UNWIND_ELIGIBLE:
        return f"second_revalidation_not_eligible:{second.decision_reason}"
    if not second.close_plan.fully_executable:
        return "second_revalidation_not_executable"
    if _facts_diverged(first, second):
        return "second_revalidation_facts_changed"
    return None


def _facts_diverged(first: UnwindDecision, second: UnwindDecision) -> bool:
    if len(first.close_plan.legs) != len(second.close_plan.legs):
        return True
    for left, right in zip(first.close_plan.legs, second.close_plan.legs, strict=True):
        if left.weighted_closing_price != right.weighted_closing_price:
            return True
        if left.closing_fee != right.closing_fee:
            return True
        if left.available_closing_capacity != right.available_closing_capacity:
            return True
        if left.quote_age_ms != right.quote_age_ms:
            return True
        if left.fee_snapshot_id != right.fee_snapshot_id:
            return True
    if first.validated_exit_pnl_gbp != second.validated_exit_pnl_gbp:
        return True
    return False


def _quote_identity(quote: ReverseQuote) -> tuple:
    return (quote.venue, quote.source_market_id, quote.source_runner_id, quote.canonical_outcome)


def _second_pass_freshness_failure(
    position: OpenPaperPosition,
    first_quotes: Sequence[ReverseQuote],
    second_quotes: Sequence[ReverseQuote],
) -> str | None:
    """Fail closed unless every required leg has a strictly newer reverse book."""

    first_by = {_quote_identity(item): item for item in first_quotes}
    second_by = {_quote_identity(item): item for item in second_quotes}
    for leg in position.legs:
        if leg.filled_size <= 0:
            continue
        key = (leg.venue, leg.source_market_id, leg.source_runner_id, leg.canonical_outcome)
        nxt = second_by.get(key)
        if nxt is None:
            return "second_revalidation_missing_reverse_quote"
        prev = first_by.get(key)
        if prev is None:
            continue
        if nxt.quoted_at <= prev.quoted_at:
            return "second_revalidation_not_fresh"
    return None


def _empty_snapshot(
    trade_id: str,
    occurred: datetime,
    reason: str,
    *,
    auto_unwind_enabled: bool = False,
) -> PositionManagementSnapshot:
    return PositionManagementSnapshot(
        trade_id=trade_id,
        recommendation=UnwindRecommendation.UNWIND_NOT_SAFE,
        decision_reason=reason,
        evaluated_at=occurred,
        auto_unwind_enabled=auto_unwind_enabled,
        auto_close_allowed=False,
    )
