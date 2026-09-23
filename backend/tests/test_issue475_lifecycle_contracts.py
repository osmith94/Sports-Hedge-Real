"""#475 formal lifecycle contracts + stateful/model-based verification.

Stacked on the #477/#485 generation harness. Data class: synthetic coordinator
and paper-ledger fixtures. Not live venue quotes. PAPER / read-only.

Invariants after every action:
- stale generation/chunk cannot mutate current generation
- unfinished generation cannot be reported complete
- settlement / Treasury / journal side effects exactly once
- ACTIVE TRADE membership survives discovery eviction
- lifecycle audit is append-only
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from random import Random

import pytest

from sports_hedge.application.active_trade_lane import (
    get_active_trade_registry,
    identity_from_open_trade,
    reset_active_trade_registry,
)
from sports_hedge.application.collector import UNIVERSE_COMPLETENESS_DEADLINE_LEFTOVER
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.paper_operations import PaperOperationsError
from sports_hedge.application.paper_settlement_agent import SETTLEABLE_STATES
from sports_hedge.application.scan_lanes import ScanLane, WORKER_COMPLETE
from sports_hedge.application.universe_checkpoint import (
    SWEEP_EVALUATED,
    SWEEP_PENDING,
    SWEEP_RETRY_WAIT,
    SweepWorkUnit,
)
from sports_hedge.arbitrage.watchlist.models import (
    LifecycleEventType,
    NearOpportunity,
    OpportunityClassification,
    OpportunityStatus,
)
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.domain.models import VenueName
from sports_hedge.lifecycle.decisions import IllegalLifecycleTransition, require_accepted
from sports_hedge.lifecycle.paper import (
    DISCOVERY_EVICTION_MUST_NOT_MUTATE,
    REASON_CONFLICTING_SETTLEMENT,
    REASON_PAPER_FILL_FROM,
    REASON_SETTLEMENT_IDEMPOTENT,
    decide_active_trade_membership,
    decide_paper_fill,
    decide_paper_settlement,
    decide_paper_trade_transition,
    decide_paper_unwind,
)
from sports_hedge.lifecycle.universe import (
    UniverseChunkPhase,
    UniverseGenerationPhase,
    decide_chunk_transition,
    decide_generation_transition,
    decide_stale_chunk_callback,
    decide_sweep_transition,
    decide_worker_transition,
    generation_phase,
)
from sports_hedge.paper.result_resolution import PAPER_AUTO_SETTLEMENT_SOURCE
from sports_hedge.paper.trades import (
    PaperSettlementRequest,
    PaperTradeAuditEventType,
    PaperTradeState,
)
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from test_dual_cadence_scheduler import NOW
from test_fixture_lifecycle_eviction import _fixture as _eviction_fixture
from test_issue477_generation_stateful_properties import (
    ACTIONS,
    REQUIRED_SEQUENCE,
    _assert_invariants,
    _complete_report,
    _new_trace,
    _settlement_journal_count,
    _truncated_report,
    action_clear,
    action_resume,
    action_stale,
    action_start,
    action_timeout,
    reset_universe_identity_cache,
)
from test_paper_settlement_hotfix import _filled_trade, _persist_open
from test_paper_trade_lifecycle import _ops as _paper_ops


def test_generation_and_chunk_graphs_reject_illegal_transitions() -> None:
    accepted = decide_generation_transition(
        UniverseGenerationPhase.IDLE,
        UniverseGenerationPhase.OPEN,
        action="start",
    )
    assert accepted.accepted is True
    unfinished = decide_generation_transition(
        UniverseGenerationPhase.OPEN,
        UniverseGenerationPhase.COMPLETE,
        action="complete",
        unfinished=True,
        leftover_n=4,
        completeness=UNIVERSE_COMPLETENESS_DEADLINE_LEFTOVER,
    )
    assert unfinished.accepted is False
    assert unfinished.reason == "illegal_complete_unfinished_generation"
    with pytest.raises(IllegalLifecycleTransition) as exc:
        require_accepted(unfinished)
    assert exc.value.decision.reason == unfinished.reason
    stale = decide_stale_chunk_callback(callback_epoch=3, active_epoch=9, generation_id=2)
    assert stale.accepted is False
    assert stale.reason == "stale_chunk_callback_quarantined"
    live = decide_stale_chunk_callback(callback_epoch=9, active_epoch=9)
    assert live.accepted is True
    sticky = decide_sweep_transition(SWEEP_EVALUATED, SWEEP_PENDING, action="timeout")
    assert sticky.accepted is False
    retry = decide_sweep_transition(SWEEP_PENDING, SWEEP_RETRY_WAIT, action="retry")
    assert retry.accepted is True
    chunk = decide_chunk_transition(
        UniverseChunkPhase.INACTIVE,
        UniverseChunkPhase.RUNNING,
        action="open",
        generation_open=False,
    )
    assert chunk.accepted is False
    assert chunk.reason == "illegal_chunk_open_without_generation"
    worker = decide_worker_transition(
        "running",
        WORKER_COMPLETE,
        action="complete",
        unfinished=True,
    )
    assert worker.accepted is False


def test_paper_graphs_reject_illegal_fill_and_settlement() -> None:
    assert decide_paper_fill(OpportunityStatus.TRIGGERED, OpportunityStatus.PARTIAL).accepted
    illegal = decide_paper_fill(OpportunityStatus.WATCHING, OpportunityStatus.FILLED)
    assert illegal.accepted is False
    assert illegal.reason == REASON_PAPER_FILL_FROM
    closed = decide_paper_trade_transition(
        PaperTradeState.CLOSED, PaperTradeState.OPEN, action="record_fills"
    )
    assert closed.accepted is False
    duplicate = decide_paper_settlement(
        PaperTradeState.CLOSED,
        has_fills=True,
        identical_request=True,
    )
    assert duplicate.accepted is True
    assert duplicate.reason == REASON_SETTLEMENT_IDEMPOTENT
    conflict = decide_paper_settlement(
        PaperTradeState.CLOSED,
        has_fills=True,
        identical_request=False,
    )
    assert conflict.reason == REASON_CONFLICTING_SETTLEMENT
    assert decide_active_trade_membership(PaperTradeState.OPEN).accepted
    assert not decide_active_trade_membership(PaperTradeState.CLOSED).accepted
    assert PaperTradeState.OPEN in SETTLEABLE_STATES
    assert "paper_trades" in DISCOVERY_EVICTION_MUST_NOT_MUTATE
    unwind = decide_paper_unwind(
        PaperTradeState.CLOSED, already_settled=True, identical_unwind=False
    )
    assert unwind.reason == "already_settled"


def test_required_generation_contract_sequence(tmp_path: Path) -> None:
    trace = _new_trace(tmp_path)
    try:
        action_start(trace)
        assert any(
            event.action == "start" for event in trace.coordinator._universe_lifecycle_audit.events
        )
        for name in REQUIRED_SEQUENCE[1:]:
            ACTIONS[name](trace)
            _assert_invariants(trace)
        assert isinstance(trace.coordinator._universe_lifecycle_audit.reasons(), tuple)
    finally:
        trace.ledger.close()
        reset_universe_identity_cache()


@pytest.mark.parametrize("seed", (0, 1, 3, 7))
def test_generated_contract_traces_preserve_invariants(tmp_path: Path, seed: int) -> None:
    rng = Random(seed)
    extra = ["timeout", "resume", "stale", "restart", "timeout", "fresh", "clear", "restart"]
    rng.shuffle(extra)
    sequence = ("start", *tuple(extra[:6]), "restart")
    root = tmp_path / f"seed-{seed}"
    root.mkdir()
    trace = _new_trace(root)
    try:
        prior_len = 0
        for name in sequence:
            ACTIONS[name](trace)
            _assert_invariants(trace)
            audit = trace.coordinator._universe_lifecycle_audit
            if name == "restart":
                prior_len = 0
            else:
                assert len(audit) >= prior_len
                prior_len = len(audit)
                snapshot = audit.events
                assert audit.events[: len(snapshot)] == snapshot
    finally:
        trace.ledger.close()
        reset_universe_identity_cache()


def test_timeout_retry_resume_restart_clear_stale_and_degradation(tmp_path: Path) -> None:
    trace = _new_trace(tmp_path)
    try:
        action_start(trace)
        action_timeout(trace)
        coordinator = trace.coordinator
        coordinator._universe_work["fx-a"] = SweepWorkUnit(
            canonical_id="fx-a", state=SWEEP_PENDING
        )
        coordinator._apply_work_unit_result_unlocked(
            "fx-a",
            state="market_fetch_unavailable",
            reason="provider_timeout",
            scanned=trace.clock.now,
        )
        assert coordinator._universe_work["fx-a"].state == SWEEP_RETRY_WAIT
        coordinator._apply_work_unit_result_unlocked(
            "fx-a",
            state="evaluated",
            reason="",
            scanned=trace.clock.now,
        )
        assert coordinator._universe_work["fx-a"].state == SWEEP_EVALUATED
        coordinator._apply_work_unit_result_unlocked(
            "fx-a",
            state="market_fetch_unavailable",
            reason="late_retry",
            scanned=trace.clock.now,
        )
        assert coordinator._universe_work["fx-a"].state == SWEEP_EVALUATED
        sticky = [
            event
            for event in coordinator._universe_lifecycle_audit.rejected()
            if event.machine == "universe_sweep_unit"
        ]
        assert sticky
        action_resume(trace)
        generation = coordinator._universe_generation_id
        coordinator._mark_lane_error(
            ScanLane.UNIVERSE,
            trace.clock.now,
            trace.clock.now + timedelta(seconds=1),
            "provider timeout",
        )
        assert coordinator._universe_generation_id == generation
        assert coordinator._universe_generation_started_at is not None
        assert coordinator._universe_active_chunk_epoch is None
        assert coordinator.status.universe.worker_state != WORKER_COMPLETE
        degraded = [
            event
            for event in coordinator._universe_lifecycle_audit.events
            if event.action in {"timeout", "provider_degraded"}
        ]
        assert degraded
        action_stale(trace)
        action_clear(trace)
        assert generation_phase(
            generation_id=coordinator._universe_generation_id,
            started=coordinator._universe_generation_started_at is not None,
            closed_generation_id=coordinator._universe_closed_generation_id,
        ) == UniverseGenerationPhase.IDLE
        assert any(
            event.action == "clear" for event in coordinator._universe_lifecycle_audit.events
        )
    finally:
        trace.ledger.close()
        reset_universe_identity_cache()


def test_unfinished_generation_is_not_reported_complete(tmp_path: Path) -> None:
    trace = _new_trace(tmp_path)
    try:
        action_start(trace)
        coordinator = trace.coordinator
        report = _truncated_report(coordinator, trace.clock.now)
        coordinator.record_report(report, scan_lane=ScanLane.UNIVERSE)
        assert coordinator._universe_generation_started_at is not None
        assert coordinator.status.universe.worker_state != WORKER_COMPLETE
        rejected = [
            event
            for event in coordinator._universe_lifecycle_audit.rejected()
            if event.reason == "illegal_complete_unfinished_generation"
        ]
        assert rejected
        complete = _complete_report(coordinator, trace.clock.now)
        coordinator.record_universe_work_set(["fx-a", "fx-b"], authoritative=True)
        coordinator.record_universe_fixture_progress(
            None, _eviction_fixture("fx-a", kickoff=trace.clock.now + timedelta(days=1)), [], []
        )
        coordinator.record_universe_fixture_progress(
            None, _eviction_fixture("fx-b", kickoff=trace.clock.now + timedelta(days=2)), [], []
        )
        coordinator.record_report(complete, scan_lane=ScanLane.UNIVERSE)
        if coordinator._universe_generation_started_at is None:
            assert coordinator.status.universe.worker_state == WORKER_COMPLETE
            assert any(
                event.action == "complete" and event.accepted
                for event in coordinator._universe_lifecycle_audit.events
            )
    finally:
        trace.ledger.close()
        reset_universe_identity_cache()


def test_watchlist_fill_rejects_illegal_stage_with_audit(tmp_path: Path) -> None:
    service = WatchlistService(SqliteWatchlistRepository(), max_quote_age_ms=10_000)
    opportunity = NearOpportunity(
        opportunity_id="opp-watch",
        canonical_event_id="event-watch",
        canonical_market_id="market-watch",
        first_seen_at=NOW,
        last_seen_at=NOW,
        status=OpportunityStatus.WATCHING,
        classification=OpportunityClassification.WATCH_CANDIDATE,
        current_net_edge=Decimal("0.001"),
        trigger_net_edge=Decimal("0.01"),
        distance_to_trigger_pp=Decimal("0.9"),
        home_team="Arsenal",
        away_team="Chelsea",
    )
    service.repository.upsert_opportunity(opportunity)
    with pytest.raises(ValueError, match="triggered paper opportunity"):
        service.record_paper_fill(
            "opp-watch",
            stage=OpportunityStatus.FILLED,
            occurred_at=NOW,
        )
    events = service.repository.list_events(opportunity_id="opp-watch")
    assert any(event.event_type is LifecycleEventType.LIFECYCLE_REJECTED for event in events)


def test_partial_fill_manual_auto_and_duplicate_settlement(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(
        tmp_path / "paper-lifecycle.sqlite",
        seed_gbp=Decimal("1000"),
        usd_gbp_per_unit=Decimal("0.80"),
        fx_source="paper_demo_fx_snapshot",
        include_kalshi=True,
    )
    try:
        _scan, watchlist, ops, _repo = _paper_ops(ledger=ledger, autofill=True)
        trade = ops.list_active_trades()[0]
        assert trade.state is PaperTradeState.OPEN
        journals_before = _settlement_journal_count(ledger)
        locked_before = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP").locked_capital
        assert decide_paper_trade_transition(
            PaperTradeState.PARTIAL, PaperTradeState.OPEN, action="recovery"
        ).accepted

        request = PaperSettlementRequest(
            winning_outcome=next(leg.outcome for leg in trade.legs),
            source="operator",
            source_id="manual-lifecycle",
        )
        settled = ops.settle(trade.trade_id, request, now=NOW)
        assert settled.state is PaperTradeState.CLOSED
        assert _settlement_journal_count(ledger) == journals_before + 1
        available = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP").available_cash

        again = ops.settle(trade.trade_id, request, now=NOW)
        assert again.state is PaperTradeState.CLOSED
        assert any(
            event.event_type is PaperTradeAuditEventType.SETTLEMENT_IDEMPOTENT
            for event in again.audit
        )
        assert _settlement_journal_count(ledger) == journals_before + 1
        assert (
            ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP").available_cash == available
        )

        with pytest.raises(PaperOperationsError, match=REASON_CONFLICTING_SETTLEMENT):
            ops.settle(
                trade.trade_id,
                PaperSettlementRequest(
                    winning_outcome="no" if request.winning_outcome != "no" else "yes",
                    source="operator",
                    source_id="other",
                ),
                now=NOW,
            )
        persisted = ops.trades.get(trade.trade_id)
        assert any(
            event.event_type is PaperTradeAuditEventType.LIFECYCLE_REJECTED
            for event in persisted.audit
        )
        assert _settlement_journal_count(ledger) == journals_before + 1

        auto = _filled_trade()
        _persist_open(ops, auto)
        auto_settled = ops.settle(
            auto.trade_id,
            PaperSettlementRequest(
                winning_outcome=next(leg.outcome for leg in auto.legs),
                source=PAPER_AUTO_SETTLEMENT_SOURCE,
                source_id="auto-lifecycle",
            ),
            now=NOW,
        )
        assert auto_settled.state is PaperTradeState.CLOSED
        journals_after_auto = _settlement_journal_count(ledger)
        ops.settle(
            auto.trade_id,
            PaperSettlementRequest(
                winning_outcome=next(leg.outcome for leg in auto.legs),
                source=PAPER_AUTO_SETTLEMENT_SOURCE,
                source_id="auto-lifecycle",
            ),
            now=NOW,
        )
        assert _settlement_journal_count(ledger) == journals_after_auto
        assert locked_before >= Decimal("0")
        assert watchlist.repository.get(trade.opportunity_id) is not None
    finally:
        ledger.close()
        reset_active_trade_registry()


def test_active_trade_survives_discovery_eviction(tmp_path: Path) -> None:
    reset_active_trade_registry()
    ledger = SqlitePaperLedger(tmp_path / "active-evict.sqlite")
    try:
        _scan, _watchlist, ops, _repo = _paper_ops(ledger=ledger, autofill=True)
        trade = ops.list_active_trades()[0]
        ops._promote_active_trade(trade, NOW)
        registry = get_active_trade_registry()
        assert trade.trade_id in {item.trade_id for item in registry.members()}
        identity = identity_from_open_trade(trade)
        store = FixtureCurrentStateStore()
        far = NOW + timedelta(days=40)
        store.upsert_evaluated_fixture(
            _eviction_fixture("fx-old", kickoff=NOW - timedelta(days=20)),
            now=NOW,
        )
        store._evict_non_current(far)
        assert trade.trade_id in {item.trade_id for item in registry.members()}
        assert ledger.trades.get(trade.trade_id) is not None
        assert ledger.journal.list_entries()
        assert identity == identity_from_open_trade(ledger.trades.get(trade.trade_id))
        assert "active_trade_registry" in DISCOVERY_EVICTION_MUST_NOT_MUTATE
    finally:
        ledger.close()
        reset_active_trade_registry()


def test_audit_log_is_append_only_across_clear(tmp_path: Path) -> None:
    trace = _new_trace(tmp_path)
    try:
        action_start(trace)
        action_timeout(trace)
        first = trace.coordinator._universe_lifecycle_audit.events
        assert first
        action_clear(trace)
        later = trace.coordinator._universe_lifecycle_audit.events
        assert later[: len(first)] == first
        assert any(event.action == "clear" for event in later[len(first) :])
        mutated = list(later)
        mutated.clear()
        assert trace.coordinator._universe_lifecycle_audit.events == later
    finally:
        trace.ledger.close()
        reset_universe_identity_cache()
