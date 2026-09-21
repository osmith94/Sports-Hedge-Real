"""Explicit PAPER opportunity / trade / settlement lifecycle contracts.

Wraps existing WatchlistService and PaperOperationsService guards. Operator-
visible status labels are unchanged. Settlement/Treasury/journal side effects
are accepted at most once; identical retries are idempotent with an auditable
reason rather than a second posting.
"""

from __future__ import annotations

from sports_hedge.arbitrage.watchlist.models import OpportunityStatus
from sports_hedge.lifecycle.decisions import LifecycleDecision, allowed_decision, rejected_decision
from sports_hedge.paper.trades import PAPER_UNWIND_SOURCE, PaperTradeState

MACHINE_OPPORTUNITY = "paper_opportunity"
MACHINE_TRADE = "paper_trade"
MACHINE_SETTLEMENT = "paper_settlement"
MACHINE_UNWIND = "paper_unwind"
MACHINE_ACTIVE = "paper_active_trade"

PAPER_FILL_STAGES = frozenset(
    {
        OpportunityStatus.PAPER_FILLING,
        OpportunityStatus.PARTIAL,
        OpportunityStatus.FILLED,
    }
)
PAPER_FILL_FROM = frozenset(
    {
        OpportunityStatus.TRIGGERED,
        OpportunityStatus.PAPER_FILLING,
        OpportunityStatus.PARTIAL,
    }
)
PROTECTED_OPPORTUNITY_STATUSES = frozenset(
    {
        OpportunityStatus.PAPER_FILLING,
        OpportunityStatus.PARTIAL,
        OpportunityStatus.FILLED,
        OpportunityStatus.CLOSED,
        OpportunityStatus.EXPIRED,
    }
)

# Observation radar may move among these; fill/close states are sticky.
OPPORTUNITY_TRANSITIONS: dict[str, frozenset[str]] = {
    OpportunityStatus.WATCHING: frozenset(
        {
            OpportunityStatus.WATCHING,
            OpportunityStatus.APPROACHING,
            OpportunityStatus.TRIGGERED,
            OpportunityStatus.REJECTED,
            OpportunityStatus.EXPIRED,
            OpportunityStatus.CLOSED,
        }
    ),
    OpportunityStatus.APPROACHING: frozenset(
        {
            OpportunityStatus.WATCHING,
            OpportunityStatus.APPROACHING,
            OpportunityStatus.TRIGGERED,
            OpportunityStatus.REJECTED,
            OpportunityStatus.EXPIRED,
            OpportunityStatus.CLOSED,
        }
    ),
    OpportunityStatus.TRIGGERED: frozenset(
        {
            OpportunityStatus.WATCHING,
            OpportunityStatus.APPROACHING,
            OpportunityStatus.TRIGGERED,
            OpportunityStatus.PAPER_FILLING,
            OpportunityStatus.PARTIAL,
            OpportunityStatus.FILLED,
            OpportunityStatus.REJECTED,
            OpportunityStatus.EXPIRED,
            OpportunityStatus.CLOSED,
        }
    ),
    OpportunityStatus.PAPER_FILLING: frozenset(
        {
            OpportunityStatus.PAPER_FILLING,
            OpportunityStatus.PARTIAL,
            OpportunityStatus.FILLED,
            OpportunityStatus.REJECTED,
            OpportunityStatus.CLOSED,
            OpportunityStatus.EXPIRED,
        }
    ),
    OpportunityStatus.PARTIAL: frozenset(
        {
            OpportunityStatus.PAPER_FILLING,
            OpportunityStatus.PARTIAL,
            OpportunityStatus.FILLED,
            OpportunityStatus.CLOSED,
            OpportunityStatus.EXPIRED,
        }
    ),
    OpportunityStatus.FILLED: frozenset(
        {OpportunityStatus.FILLED, OpportunityStatus.CLOSED, OpportunityStatus.EXPIRED}
    ),
    OpportunityStatus.REJECTED: frozenset(
        {
            OpportunityStatus.REJECTED,
            OpportunityStatus.WATCHING,
            OpportunityStatus.APPROACHING,
            OpportunityStatus.TRIGGERED,
            OpportunityStatus.PAPER_FILLING,
            OpportunityStatus.EXPIRED,
            OpportunityStatus.CLOSED,
        }
    ),
    OpportunityStatus.CLOSED: frozenset({OpportunityStatus.CLOSED}),
    OpportunityStatus.EXPIRED: frozenset({OpportunityStatus.EXPIRED, OpportunityStatus.CLOSED}),
}

TRADE_TRANSITIONS: dict[str, frozenset[str]] = {
    PaperTradeState.PENDING: frozenset(
        {
            PaperTradeState.PENDING,
            PaperTradeState.PARTIAL,
            PaperTradeState.OPEN,
            PaperTradeState.AWAITING_MANUAL_EXTERNAL,
            PaperTradeState.CLOSED,
        }
    ),
    PaperTradeState.AWAITING_MANUAL_EXTERNAL: frozenset(
        {
            PaperTradeState.AWAITING_MANUAL_EXTERNAL,
            PaperTradeState.PENDING,
            PaperTradeState.PARTIAL,
            PaperTradeState.OPEN,
            PaperTradeState.CLOSED,
        }
    ),
    PaperTradeState.PARTIAL: frozenset(
        {PaperTradeState.PARTIAL, PaperTradeState.OPEN, PaperTradeState.CLOSED}
    ),
    PaperTradeState.OPEN: frozenset({PaperTradeState.OPEN, PaperTradeState.CLOSED}),
    PaperTradeState.CLOSED: frozenset({PaperTradeState.CLOSED}),
}

SETTLEABLE_TRADE_STATES = frozenset({PaperTradeState.OPEN, PaperTradeState.PARTIAL})
ACTIVE_TRADE_STATES = frozenset({PaperTradeState.OPEN, PaperTradeState.PARTIAL})

DISCOVERY_EVICTION_MAY_MUTATE = frozenset(
    {"radar_inventory", "fixture_aliases", "fixture_tombstones", "hot_membership"}
)
DISCOVERY_EVICTION_MUST_NOT_MUTATE = frozenset(
    {
        "paper_trades",
        "treasury_locks",
        "paper_journal",
        "active_trade_registry",
        "watchlist_opportunities",
        "watchlist_lifecycle_events",
        "active_trade_event_journal",
    }
)

REASON_PAPER_FILL_STAGE = "paper fill stage must be PAPER_FILLING, PARTIAL, or FILLED"
REASON_PAPER_FILL_FROM = "paper fill can only be recorded for a triggered paper opportunity"
REASON_BOUND_SNAPSHOT = "bound snapshot attempt requires a current TRIGGERED snapshot"
REASON_ALREADY_UNWOUND = "already_unwound"
REASON_ALREADY_SETTLED = "already_settled"
REASON_CONFLICTING_SETTLEMENT = "conflicting_settlement"
REASON_CANNOT_SETTLE_UNCONFIRMED = "cannot_settle_unconfirmed_external"
REASON_CANNOT_SETTLE_UNFILLED = "cannot_settle_unfilled_trade"
REASON_SETTLEMENT_IDEMPOTENT = "settlement_idempotent"
REASON_UNWIND_IDEMPOTENT = "unwind_idempotent"
REASON_CLOSED_TRADE_IMMUTABLE = "illegal_trade_transition:CLOSED->open_or_partial"


def _status(value: OpportunityStatus | PaperTradeState | str) -> str:
    return str(getattr(value, "value", value))


def decide_opportunity_transition(
    from_status: OpportunityStatus | str,
    to_status: OpportunityStatus | str,
    *,
    action: str,
) -> LifecycleDecision:
    src = _status(from_status)
    dst = _status(to_status)
    allowed = OPPORTUNITY_TRANSITIONS.get(src, frozenset())
    if dst not in allowed:
        return rejected_decision(
            from_state=src,
            to_state=dst,
            action=action,
            machine=MACHINE_OPPORTUNITY,
            reason=f"illegal_opportunity_transition:{src}->{dst}",
        )
    return allowed_decision(
        from_state=src,
        to_state=dst,
        action=action,
        machine=MACHINE_OPPORTUNITY,
    )


def decide_paper_fill(
    from_status: OpportunityStatus | str,
    stage: OpportunityStatus | str,
    *,
    bound_snapshot: bool = False,
) -> LifecycleDecision:
    src = from_status if isinstance(from_status, OpportunityStatus) else OpportunityStatus(str(from_status))
    dst = stage if isinstance(stage, OpportunityStatus) else OpportunityStatus(str(stage))
    if dst not in PAPER_FILL_STAGES:
        return rejected_decision(
            from_state=_status(src),
            to_state=_status(dst),
            action="paper_fill",
            machine=MACHINE_OPPORTUNITY,
            reason=REASON_PAPER_FILL_STAGE,
        )
    if src is OpportunityStatus.FILLED and dst is OpportunityStatus.FILLED:
        return allowed_decision(
            from_state=_status(src),
            to_state=_status(dst),
            action="paper_fill_idempotent",
            machine=MACHINE_OPPORTUNITY,
            reason="paper_fill_already_complete",
        )
    if src not in PAPER_FILL_FROM:
        return rejected_decision(
            from_state=_status(src),
            to_state=_status(dst),
            action="paper_fill",
            machine=MACHINE_OPPORTUNITY,
            reason=REASON_PAPER_FILL_FROM,
        )
    if bound_snapshot and src not in {OpportunityStatus.TRIGGERED, OpportunityStatus.PAPER_FILLING}:
        return rejected_decision(
            from_state=_status(src),
            to_state=_status(dst),
            action="bound_snapshot_fill",
            machine=MACHINE_OPPORTUNITY,
            reason=REASON_BOUND_SNAPSHOT,
        )
    return decide_opportunity_transition(src, dst, action="paper_fill")


def decide_paper_trade_transition(
    from_state: PaperTradeState | str,
    to_state: PaperTradeState | str,
    *,
    action: str,
) -> LifecycleDecision:
    src = _status(from_state)
    dst = _status(to_state)
    allowed = TRADE_TRANSITIONS.get(src, frozenset())
    if dst not in allowed:
        reason = REASON_CLOSED_TRADE_IMMUTABLE if src == PaperTradeState.CLOSED else (
            f"illegal_trade_transition:{src}->{dst}"
        )
        return rejected_decision(
            from_state=src,
            to_state=dst,
            action=action,
            machine=MACHINE_TRADE,
            reason=reason,
        )
    return allowed_decision(
        from_state=src,
        to_state=dst,
        action=action,
        machine=MACHINE_TRADE,
    )


def decide_paper_settlement(
    state: PaperTradeState | str,
    *,
    has_fills: bool,
    already_unwound: bool = False,
    identical_request: bool = False,
    unwind_source_id: str | None = None,
    request_source: str | None = None,
    request_source_id: str | None = None,
) -> LifecycleDecision:
    current = state if isinstance(state, PaperTradeState) else PaperTradeState(str(state))
    if current is PaperTradeState.CLOSED:
        if already_unwound or (
            request_source == PAPER_UNWIND_SOURCE and unwind_source_id is not None
        ):
            return rejected_decision(
                from_state=current.value,
                to_state=PaperTradeState.CLOSED.value,
                action="settle",
                machine=MACHINE_SETTLEMENT,
                reason=REASON_ALREADY_UNWOUND,
            )
        if identical_request:
            return allowed_decision(
                from_state=current.value,
                to_state=PaperTradeState.CLOSED.value,
                action="settle_idempotent",
                machine=MACHINE_SETTLEMENT,
                reason=REASON_SETTLEMENT_IDEMPOTENT,
            )
        return rejected_decision(
            from_state=current.value,
            to_state=PaperTradeState.CLOSED.value,
            action="settle",
            machine=MACHINE_SETTLEMENT,
            reason=REASON_CONFLICTING_SETTLEMENT,
            extra={"request_source": request_source, "request_source_id": request_source_id},
        )
    if current is PaperTradeState.AWAITING_MANUAL_EXTERNAL:
        return rejected_decision(
            from_state=current.value,
            to_state=PaperTradeState.CLOSED.value,
            action="settle",
            machine=MACHINE_SETTLEMENT,
            reason=REASON_CANNOT_SETTLE_UNCONFIRMED,
        )
    if not has_fills:
        return rejected_decision(
            from_state=current.value,
            to_state=PaperTradeState.CLOSED.value,
            action="settle",
            machine=MACHINE_SETTLEMENT,
            reason=REASON_CANNOT_SETTLE_UNFILLED,
        )
    return decide_paper_trade_transition(current, PaperTradeState.CLOSED, action="settle")


def decide_paper_unwind(
    state: PaperTradeState | str,
    *,
    identical_unwind: bool = False,
    already_unwound: bool = False,
    already_settled: bool = False,
) -> LifecycleDecision:
    current = state if isinstance(state, PaperTradeState) else PaperTradeState(str(state))
    if current is PaperTradeState.CLOSED:
        if identical_unwind:
            return allowed_decision(
                from_state=current.value,
                to_state=current.value,
                action="unwind_idempotent",
                machine=MACHINE_UNWIND,
                reason=REASON_UNWIND_IDEMPOTENT,
            )
        if already_unwound:
            return rejected_decision(
                from_state=current.value,
                to_state=current.value,
                action="unwind",
                machine=MACHINE_UNWIND,
                reason=REASON_ALREADY_UNWOUND,
            )
        return rejected_decision(
            from_state=current.value,
            to_state=current.value,
            action="unwind",
            machine=MACHINE_UNWIND,
            reason=REASON_ALREADY_SETTLED,
        )
    return decide_paper_trade_transition(current, PaperTradeState.CLOSED, action="unwind")


def decide_active_trade_membership(state: PaperTradeState | str) -> LifecycleDecision:
    current = state if isinstance(state, PaperTradeState) else PaperTradeState(str(state))
    if current in ACTIVE_TRADE_STATES:
        return allowed_decision(
            from_state=current.value,
            to_state=current.value,
            action="promote_active_trade",
            machine=MACHINE_ACTIVE,
        )
    return rejected_decision(
        from_state=current.value,
        to_state=current.value,
        action="promote_active_trade",
        machine=MACHINE_ACTIVE,
        reason="illegal_active_trade_state",
    )


def auto_settle_eligible(state: PaperTradeState | str) -> bool:
    current = state if isinstance(state, PaperTradeState) else PaperTradeState(str(state))
    return current in SETTLEABLE_TRADE_STATES
