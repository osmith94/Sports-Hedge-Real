"""Issue #208 / #213: paper persistence crash-consistency and truthful recovery.

Failure-injection at production seams. Data class: deterministic fixture/demo
paper-scan payloads. Not live, historical, or modelled venue quotes.
Phase 1 remains paper-only / read-only toward venues.
"""

from __future__ import annotations

import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest

from sports_hedge.accounting.paper_journal import DataProvenance
from sports_hedge.api import paper as paper_api
from sports_hedge.application.demo_fixtures import DEMO_FX, tighten_reverse_quotes
from sports_hedge.application.demo_walkthrough import DemoWalkthroughService, FixtureReplayRequest
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.paper_operations import PaperOperationsError, PaperOperationsService
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.watchlist.models import LifecycleEventType, OpportunityStatus
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.trades import (
    PAPER_UNWIND_SOURCE,
    PaperTrade,
    PaperTradeAuditEvent,
    PaperTradeAuditEventType,
    PaperTradeState,
)
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.treasury.models import TreasuryLockRequest
from test_hot_scan_reliability import _hot_leftover_report
from test_lane4_unwind_settlement import UNWIND_POLICY
from test_step8f_automatic_paper_entry import (
    FX as AUTOFILL_FX,
    _matchbook_btts,
    _observe_and_persist,
    _ops_bundle,
    _kalshi_btts,
    _kalshi_costs,
    _standing,
)


FILL_JOURNAL_SOURCES = {
    "paper_fill_simulator",
    "paper_simulated_external",
    "manual_external_confirmation",
}


def _fill_journals(ops: PaperOperationsService) -> list:
    return [
        entry
        for entry in ops.journal.list_entries()
        if entry.source in FILL_JOURNAL_SOURCES
    ]


def _lock_rows(ledger: SqlitePaperLedger, *, opportunity_id: str | None = None) -> list:
    if opportunity_id is None:
        return list(ledger._connection.execute("SELECT * FROM paper_treasury_locks ORDER BY lock_id"))
    return list(
        ledger._connection.execute(
            "SELECT * FROM paper_treasury_locks WHERE opportunity_id = ? ORDER BY lock_id",
            (opportunity_id,),
        )
    )


def _spendable(ledger: SqlitePaperLedger) -> dict[tuple[VenueName, str], Decimal]:
    snap = ledger.treasury.snapshot()
    return {
        (pool.venue, pool.native_currency): pool.available_cash
        for pool in snap.pools
    }


def _locked(ledger: SqlitePaperLedger) -> dict[tuple[VenueName, str], Decimal]:
    snap = ledger.treasury.snapshot()
    return {
        (pool.venue, pool.native_currency): pool.locked_capital
        for pool in snap.pools
    }


def _audit_types(ops: PaperOperationsService, trade_id: str) -> list[PaperTradeAuditEventType]:
    trade = ops.trades.get(trade_id)
    assert trade is not None
    return [event.event_type for event in trade.audit]


def _count_type(events: list[PaperTradeAuditEventType], kind: PaperTradeAuditEventType) -> int:
    return sum(1 for item in events if item is kind)


def _watch_status(watchlist: WatchlistService, opportunity_id: str) -> OpportunityStatus | None:
    row = watchlist.repository.get(opportunity_id)
    return None if row is None else row.status


def _fill_complete_events(watchlist: WatchlistService, opportunity_id: str) -> list:
    return [
        event
        for event in watchlist.repository.list_events(opportunity_id=opportunity_id, limit=100)
        if event.event_type is LifecycleEventType.PAPER_FILL_COMPLETE
    ]


def _qualifying_decision(scan, watchlist, extra=None):
    kwargs = dict(
        venue_costs=_kalshi_costs(),
        fx_snapshots=AUTOFILL_FX,
        maximum_execution_risk=100,
        liquidity_snapshot=_standing(),
    )
    if extra:
        kwargs.update(extra)
    decision = scan.scan_pair(_matchbook_btts(), _kalshi_btts(), **kwargs)
    assert decision.eligible_for_paper_simulation is True, decision.rejection_reasons
    assert decision.allocation is not None and decision.allocation.accepted
    watchlist.observe_paper_decision(
        decision,
        scan.market_intelligence.market_history(canonical_market_id=decision.canonical_market_id),
    )
    return decision


def _state_report(
    *,
    ops: PaperOperationsService,
    ledger: SqlitePaperLedger,
    watchlist: WatchlistService,
    opportunity_id: str | None,
    audit: SqlitePaperScanRepository | None = None,
) -> dict[str, Any]:
    trades = ops.list_active_trades()
    trade = trades[0] if trades else None
    oid = opportunity_id or (trade.opportunity_id if trade is not None else None)
    return {
        "audit_rows": 0 if audit is None else len(audit.list_scans(limit=100)),
        "open_trades": len(trades),
        "trade_state": None if trade is None else trade.state.value,
        "locks": len(_lock_rows(ledger, opportunity_id=oid)) if oid else len(_lock_rows(ledger)),
        "fill_journals": len(_fill_journals(ops)),
        "watch_status": None if oid is None else (_watch_status(watchlist, oid).value if _watch_status(watchlist, oid) else None),
        "spendable": _spendable(ledger),
        "locked": _locked(ledger),
        "autofill_events": 0
        if trade is None
        else _count_type(_audit_types(ops, trade.trade_id), PaperTradeAuditEventType.PAPER_AUTOFILL),
        "fills_recorded_events": 0
        if trade is None
        else _count_type(_audit_types(ops, trade.trade_id), PaperTradeAuditEventType.FILLS_RECORDED),
        "fill_complete_events": 0 if oid is None else len(_fill_complete_events(watchlist, oid)),
    }


# ---------------------------------------------------------------------------
# Already-passing invariants (must remain green)
# ---------------------------------------------------------------------------


def test_exact_same_stamped_retry_does_not_duplicate_open_locks_or_journals(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        decision = _observe_and_persist(
            scan,
            watchlist,
            ops,
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
        )
        first = _state_report(
            ops=ops,
            ledger=ledger,
            watchlist=watchlist,
            opportunity_id=next(iter(ops._plans)),
        )
        assert first["open_trades"] == 1
        assert first["locks"] == 2
        assert first["fill_journals"] == 2
        assert first["watch_status"] == OpportunityStatus.FILLED.value
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER)
        second = _state_report(
            ops=ops,
            ledger=ledger,
            watchlist=watchlist,
            opportunity_id=next(iter(ops._plans)),
        )
        assert second["open_trades"] == 1
        assert second["locks"] == first["locks"]
        assert second["fill_journals"] == first["fill_journals"]
        assert second["spendable"] == first["spendable"]
        assert second["locked"] == first["locked"]
        assert second["autofill_events"] == 1
        assert second["fills_recorded_events"] == 1
    finally:
        repository.close()
        ledger.close()


def test_new_scan_same_economics_appends_audit_without_new_open(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    audit = SqlitePaperScanRepository(tmp_path / "paper-audit.sqlite")
    try:
        first = scan.scan_pair(
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
            fx_snapshots=AUTOFILL_FX,
            maximum_execution_risk=100,
            liquidity_snapshot=_standing(),
        )
        first.paper_audit_record_id = str(uuid4())
        paper_api._persist_decision(
            first,
            service=scan,
            audit=audit,
            watchlist=watchlist,
            operations=ops,
        )
        assert len(audit.list_scans(limit=10)) == 1
        assert len(ops.list_active_trades()) == 1
        second = scan.scan_pair(
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
            fx_snapshots=AUTOFILL_FX,
            maximum_execution_risk=100,
            liquidity_snapshot=_standing(),
        )
        second.paper_audit_record_id = None
        paper_api._persist_decision(
            second,
            service=scan,
            audit=audit,
            watchlist=watchlist,
            operations=ops,
        )
        assert len(audit.list_scans(limit=10)) == 2
        assert len(ops.list_active_trades()) == 1
        trade = ops.list_active_trades()[0]
        assert _count_type(_audit_types(ops, trade.trade_id), PaperTradeAuditEventType.PAPER_AUTOFILL) == 1
        assert _count_type(_audit_types(ops, trade.trade_id), PaperTradeAuditEventType.FILLS_RECORDED) == 1
        assert len(_lock_rows(ledger, opportunity_id=trade.opportunity_id)) == 2
        assert len(_fill_journals(ops)) == 2
    finally:
        repository.close()
        ledger.close()
        audit.close()


def test_allocator_resize_on_retry_does_not_top_up_existing_open(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        decision = _observe_and_persist(
            scan,
            watchlist,
            ops,
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
        )
        trade = ops.list_active_trades()[0]
        original_stakes = {(leg.venue, leg.outcome): leg.filled_stake for leg in trade.legs}
        original_locks = dict(trade.capital_locked_native)
        original_journals = len(_fill_journals(ops))
        fatter = [
            leg.model_copy(update={"requested_stake": leg.requested_stake * 2})
            for leg in decision.fill_legs
        ]
        fatter_decision = decision.model_copy(update={"fill_legs": fatter})
        ops.persist_triggered_chain(fatter_decision, provenance=DataProvenance.LIVE_PAPER)
        after = ops.list_active_trades()[0]
        assert after.trade_id == trade.trade_id
        assert {(leg.venue, leg.outcome): leg.filled_stake for leg in after.legs} == original_stakes
        assert after.capital_locked_native == original_locks
        assert len(_fill_journals(ops)) == original_journals
    finally:
        repository.close()
        ledger.close()


def test_stale_evidence_cannot_create_a_new_open(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        stale_left = _matchbook_btts().model_copy(update={"quote_age_ms": 5_000})
        stale_right = _kalshi_btts().model_copy(update={"quote_age_ms": 5_000})
        decision = scan.scan_pair(
            stale_left,
            stale_right,
            venue_costs=_kalshi_costs(),
            fx_snapshots=AUTOFILL_FX,
            maximum_execution_risk=100,
            liquidity_snapshot=_standing(),
        )
        assert decision.eligible_for_paper_simulation is False
        watchlist.observe_paper_decision(
            decision,
            scan.market_intelligence.market_history(canonical_market_id=decision.canonical_market_id),
        )
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER)
        assert ops.list_active_trades() == []
        assert _lock_rows(ledger) == []
        assert _fill_journals(ops) == []
    finally:
        repository.close()
        ledger.close()


def test_disabled_venue_insufficient_capital_missing_fx_fee_risk_leak_zero_locks(
    tmp_path: Path,
) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        before_spendable = _spendable(ledger)
        decision = _qualifying_decision(scan, watchlist)
        ops.persist_triggered_chain(
            decision,
            provenance=DataProvenance.LIVE_PAPER,
            refreshed_venues=(VenueName.MATCHBOOK,),
        )
        assert ops.list_active_trades() == []
        assert _lock_rows(ledger) == []
        assert _fill_journals(ops) == []
        assert _spendable(ledger) == before_spendable

        empty = scan.scan_pair(
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
            fx_snapshots=AUTOFILL_FX,
            maximum_execution_risk=100,
            liquidity_snapshot=_standing(
                matchbook_gbp=Decimal("0"),
                polymarket_usd=Decimal("0"),
                kalshi_usd=Decimal("0"),
            ),
        )
        watchlist.observe_paper_decision(
            empty,
            scan.market_intelligence.market_history(canonical_market_id=empty.canonical_market_id),
        )
        ops.persist_triggered_chain(empty, provenance=DataProvenance.LIVE_PAPER)
        assert ops.list_active_trades() == []
        assert _lock_rows(ledger) == []

        missing_fx = scan.scan_pair(
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
            fx_snapshots=[],
            maximum_execution_risk=100,
            liquidity_snapshot=_standing(),
        )
        watchlist.observe_paper_decision(
            missing_fx,
            scan.market_intelligence.market_history(canonical_market_id=missing_fx.canonical_market_id),
        )
        ops.persist_triggered_chain(missing_fx, provenance=DataProvenance.LIVE_PAPER)
        assert ops.list_active_trades() == []
        assert _lock_rows(ledger) == []

        missing_fee = scan.scan_pair(
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=[],
            fx_snapshots=AUTOFILL_FX,
            maximum_execution_risk=100,
            liquidity_snapshot=_standing(),
        )
        watchlist.observe_paper_decision(
            missing_fee,
            scan.market_intelligence.market_history(canonical_market_id=missing_fee.canonical_market_id),
        )
        ops.persist_triggered_chain(missing_fee, provenance=DataProvenance.LIVE_PAPER)
        assert ops.list_active_trades() == []
        assert _lock_rows(ledger) == []

        risky = scan.scan_pair(
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
            fx_snapshots=AUTOFILL_FX,
            maximum_execution_risk=0,
            liquidity_snapshot=_standing(),
        )
        assert "execution_risk_above_threshold" not in risky.rejection_reasons
        watchlist.observe_paper_decision(
            risky,
            scan.market_intelligence.market_history(canonical_market_id=risky.canonical_market_id),
        )
        ops.persist_triggered_chain(risky, provenance=DataProvenance.LIVE_PAPER)
        opened = ops.list_active_trades()
        assert len(opened) == 1
        assert opened[0].paper_only is True
        assert opened[0].places_orders is False
        assert opened[0].capital_locked_gbp is not None
        assert opened[0].capital_locked_gbp > 0
    finally:
        repository.close()
        ledger.close()


# ---------------------------------------------------------------------------
# 1. persist_ok must stay false when qualifying autofill fails with no OPEN
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_persist_ok_false_when_qualifying_autofill_raises_and_no_open(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    audit = SqlitePaperScanRepository(tmp_path / "paper-audit.sqlite")
    try:
        decision = scan.scan_pair(
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
            fx_snapshots=AUTOFILL_FX,
            maximum_execution_risk=100,
            liquidity_snapshot=_standing(),
        )
        assert decision.eligible_for_paper_simulation is True
        decision.paper_audit_record_id = str(uuid4())
        coordinator = LiveRefreshCoordinator()
        coordinator.reset()
        report = _hot_leftover_report(cancelled=False).model_copy(
            update={"paper_decisions": [decision]}
        )

        def operations_factory(watchlist_arg=None, alerts=None):
            del alerts
            if watchlist_arg is not None:
                ops.watchlist = watchlist_arg
            return ops

        monkeypatch.setattr(paper_api, "get_paper_operations_service", operations_factory)

        def fail_fill(*args: Any, **kwargs: Any):
            raise PaperOperationsError("injected_autofill_failure")

        monkeypatch.setattr(ops, "simulate_fill", fail_fill)
        await paper_api.persist_scheduled_collection_report(
            coordinator,
            report,
            service=scan,
            audit=audit,
            watchlist=watchlist,
            scan_lane=ScanLane.HOT,
        )
        actual = {
            "persist_ok": coordinator.status.hot.persist_ok,
            "last_persist_error": coordinator.status.hot.last_persist_error,
            "audit_rows": len(audit.list_scans(limit=10)),
            "open_trades": len(ops.list_active_trades()),
            "locks": len(_lock_rows(ledger)),
            "fill_journals": len(_fill_journals(ops)),
        }
        expected = {
            "persist_ok": False,
            "audit_rows": 1,
            "open_trades": 0,
            "locks": 0,
            "fill_journals": 0,
        }
        assert actual["persist_ok"] is expected["persist_ok"], actual
        assert actual["last_persist_error"]
        assert "injected_autofill_failure" in actual["last_persist_error"]
        assert actual["audit_rows"] == expected["audit_rows"]
        assert actual["open_trades"] == expected["open_trades"]
        assert actual["locks"] == expected["locks"]
        assert actual["fill_journals"] == expected["fill_journals"]
        assert report.scan_diagnostics["persist_ok"] is False
    finally:
        repository.close()
        ledger.close()
        audit.close()


# ---------------------------------------------------------------------------
# 2. Partial OPEN/lock/journal recovery must complete or hard-fail
# ---------------------------------------------------------------------------


def test_open_without_locks_is_repaired_on_retry_not_healthy_short_circuit(
    tmp_path: Path,
) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        _observe_and_persist(
            scan,
            watchlist,
            ops,
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
        )
        trade = ops.list_active_trades()[0]
        opportunity_id = trade.opportunity_id
        seed_spendable = {
            (VenueName.MATCHBOOK, "GBP"): Decimal("1000"),
            (VenueName.POLYMARKET, "USD"): (Decimal("1000") / Decimal("0.75")),
            (VenueName.KALSHI, "USD"): (Decimal("1000") / Decimal("0.75")),
        }
        session = ledger.treasury.active_session()
        assert session is not None
        for row in _lock_rows(ledger, opportunity_id=opportunity_id):
            ledger._connection.execute("DELETE FROM paper_treasury_locks WHERE lock_id = ?", (row["lock_id"],))
        for entry in list(_fill_journals(ops)):
            ledger._connection.execute(
                "DELETE FROM paper_journal_entries WHERE source = ? AND source_id = ?",
                (entry.source, entry.source_id),
            )
            ledger._connection.execute(
                "DELETE FROM paper_treasury_events WHERE source = ? AND source_id = ?",
                (entry.source, entry.source_id),
            )
        ledger._connection.execute(
            """
            UPDATE paper_treasury_pools
            SET available_cash = seed_native, locked_capital = '0'
            WHERE session_id = ?
            """,
            (session.session_id,),
        )
        ledger._connection.commit()
        ledger.reload_journal()
        after_corrupt = _state_report(
            ops=ops, ledger=ledger, watchlist=watchlist, opportunity_id=opportunity_id
        )
        assert after_corrupt["open_trades"] == 1
        assert after_corrupt["locks"] == 0
        assert after_corrupt["fill_journals"] == 0
        assert after_corrupt["spendable"][(VenueName.MATCHBOOK, "GBP")] == seed_spendable[(VenueName.MATCHBOOK, "GBP")]

        ops.persist_triggered_chain(
            ops._plans[opportunity_id].decision, provenance=DataProvenance.LIVE_PAPER
        )
        repaired = _state_report(
            ops=ops, ledger=ledger, watchlist=watchlist, opportunity_id=opportunity_id
        )
        assert repaired["open_trades"] == 1, repaired
        assert repaired["locks"] == 2, repaired
        assert repaired["fill_journals"] == 2, repaired
        assert repaired["watch_status"] == OpportunityStatus.FILLED.value, repaired
        assert repaired["locked"][(VenueName.MATCHBOOK, "GBP")] > 0
        assert repaired["spendable"][(VenueName.MATCHBOOK, "GBP")] < seed_spendable[(VenueName.MATCHBOOK, "GBP")]
        assert repaired["autofill_events"] == 1
        assert repaired["fills_recorded_events"] == 1
    finally:
        repository.close()
        ledger.close()


def test_lock_without_journal_is_repaired_or_atomic_not_duplicate_lock(
    tmp_path: Path,
) -> None:
    ledger = SqlitePaperLedger(
        tmp_path / "lock-journal.sqlite",
        seed_gbp=Decimal("1000"),
        usd_gbp_per_unit=Decimal("0.75"),
        fx_source="test",
    )
    try:
        request = TreasuryLockRequest(
            venue=VenueName.MATCHBOOK,
            native_currency="GBP",
            amount_native=Decimal("250"),
            lock_id="lock-missing-journal",
            trade_id="ptrade-missing-journal",
            opportunity_id="opp-missing-journal",
            fx_rate_gbp_per_unit=Decimal("1"),
        )
        session = ledger.treasury.active_session()
        assert session is not None
        real_append = ledger.journal.append_idempotent

        def crash_after_lock(entry, *args: Any, **kwargs: Any):
            raise RuntimeError("crash_after_lock_before_journal")

        ledger.journal.append_idempotent = crash_after_lock  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="crash_after_lock_before_journal"):
            ledger.treasury._lock_one(
                session,
                request,
                occurred_at=datetime.now(UTC),
                provenance=DataProvenance.LIVE_PAPER,
            )
        leftover = _lock_rows(ledger)
        leftover_journals = [
            entry
            for entry in ledger.journal.list_entries()
            if entry.source_id == request.lock_id
        ]
        assert leftover == [], {
            "expected": "no committed lock without journal",
            "actual_locks": [row["lock_id"] for row in leftover],
            "actual_journals": leftover_journals,
        }

        ledger.journal.append_idempotent = real_append  # type: ignore[method-assign]
        # Pathological leftover from a prior non-atomic seam: lock row, no journal.
        pool = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP")
        ledger._connection.execute(
            """
            UPDATE paper_treasury_pools
            SET available_cash = ?, locked_capital = ?
            WHERE pool_id = ?
            """,
            (str(pool.available_cash - Decimal("250")), str(pool.locked_capital + Decimal("250")), f"{session.session_id}:matchbook/GBP"),
        )
        ledger._connection.execute(
            """
            INSERT INTO paper_treasury_locks (
                lock_id, session_id, pool_id, trade_id, opportunity_id,
                venue, native_currency, locked_native, released_native, status,
                source, capital_source, fill_id
            ) VALUES (?, ?, ?, ?, ?, 'matchbook', 'GBP', '250', '0', 'open',
                      'paper_fill_simulator', 'AUTO_POOL', ?)
            """,
            (
                request.lock_id,
                session.session_id,
                f"{session.session_id}:matchbook/GBP",
                request.trade_id,
                request.opportunity_id,
                request.lock_id,
            ),
        )
        ledger._connection.commit()
        posted = ledger.treasury.lock_capital([request])
        assert posted
        assert ledger.journal.get(request.source, request.lock_id) is not None
        locks = _lock_rows(ledger)
        assert len(locks) == 1
        snap = ledger.treasury.snapshot()
        assert snap.pool(VenueName.MATCHBOOK, "GBP").locked_capital == Decimal("250")
        assert snap.pool(VenueName.MATCHBOOK, "GBP").available_cash == Decimal("750")
    finally:
        ledger.close()


# ---------------------------------------------------------------------------
# 3. Crash after first/second lock/journal boundary
# ---------------------------------------------------------------------------


def test_crash_after_first_lock_rolls_back_or_retries_to_full_set(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        decision = _qualifying_decision(scan, watchlist)
        calls = {"n": 0}
        real_lock = ledger.treasury._lock_one

        def crash_after_first(*args: Any, **kwargs: Any):
            posted = real_lock(*args, **kwargs)
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("crash_after_first_lock_journal")
            return posted

        ledger.treasury._lock_one = crash_after_first  # type: ignore[method-assign]
        with pytest.raises(Exception):
            ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER)
        after_crash = _state_report(
            ops=ops,
            ledger=ledger,
            watchlist=watchlist,
            opportunity_id=next(iter(ops._plans)),
        )
        assert after_crash["open_trades"] == 0, after_crash
        # Either both lock+journal rolled back, or at most complete lock+journal pairs.
        assert after_crash["locks"] == after_crash["fill_journals"], after_crash
        assert after_crash["locks"] in {0, 1}, after_crash
        for row in _lock_rows(ledger):
            assert ledger.journal.get(row["source"], row["lock_id"]) is not None

        ledger.treasury._lock_one = real_lock  # type: ignore[method-assign]
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER)
        after_retry = _state_report(
            ops=ops,
            ledger=ledger,
            watchlist=watchlist,
            opportunity_id=next(iter(ops._plans)),
        )
        assert after_retry["open_trades"] == 1, after_retry
        assert after_retry["locks"] == 2, after_retry
        assert after_retry["fill_journals"] == 2, after_retry
        assert after_retry["watch_status"] == OpportunityStatus.FILLED.value, after_retry
    finally:
        repository.close()
        ledger.close()


def test_crash_after_journals_before_open_save_retries_to_one_open(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        decision = _qualifying_decision(scan, watchlist)
        real_save = ops._persist_open_trade

        def crash_before_open(*args: Any, **kwargs: Any):
            raise RuntimeError("crash_after_journals_before_open")

        ops._persist_open_trade = crash_before_open  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="crash_after_journals_before_open"):
            ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER)
        after_crash = _state_report(
            ops=ops,
            ledger=ledger,
            watchlist=watchlist,
            opportunity_id=next(iter(ops._plans)),
        )
        assert after_crash["open_trades"] == 0, after_crash
        assert after_crash["locks"] == after_crash["fill_journals"], after_crash
        ops._persist_open_trade = real_save  # type: ignore[method-assign]
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER)
        after_retry = _state_report(
            ops=ops,
            ledger=ledger,
            watchlist=watchlist,
            opportunity_id=next(iter(ops._plans)),
        )
        assert after_retry["open_trades"] == 1, after_retry
        assert after_retry["locks"] == 2, after_retry
        assert after_retry["fill_journals"] == 2, after_retry
        assert after_retry["watch_status"] == OpportunityStatus.FILLED.value, after_retry
    finally:
        repository.close()
        ledger.close()


# ---------------------------------------------------------------------------
# 4. Crash after OPEN/locks before watchlist FILLED
# ---------------------------------------------------------------------------


def test_crash_after_open_before_watchlist_filled_converges_on_retry(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        decision = _qualifying_decision(scan, watchlist)
        real_fill = watchlist.record_paper_fill

        def crash_watchlist(*args: Any, **kwargs: Any):
            raise RuntimeError("crash_after_open_before_watchlist")

        watchlist.record_paper_fill = crash_watchlist  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="crash_after_open_before_watchlist"):
            ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER)
        opportunity_id = next(iter(ops._plans))
        after_crash = _state_report(
            ops=ops, ledger=ledger, watchlist=watchlist, opportunity_id=opportunity_id
        )
        assert after_crash["open_trades"] == 1, after_crash
        assert after_crash["locks"] == 2, after_crash
        assert after_crash["watch_status"] == OpportunityStatus.PAPER_FILLING.value, after_crash
        watchlist.record_paper_fill = real_fill  # type: ignore[method-assign]
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER)
        after_retry = _state_report(
            ops=ops, ledger=ledger, watchlist=watchlist, opportunity_id=opportunity_id
        )
        assert after_retry["open_trades"] == 1, after_retry
        assert after_retry["locks"] == 2, after_retry
        assert after_retry["fill_journals"] == 2, after_retry
        assert after_retry["watch_status"] == OpportunityStatus.FILLED.value, after_retry
        assert after_retry["autofill_events"] == 1, after_retry
        assert after_retry["fills_recorded_events"] == 1, after_retry
        assert after_retry["fill_complete_events"] == 1, after_retry
    finally:
        repository.close()
        ledger.close()


@pytest.mark.asyncio
async def test_crash_after_audit_before_open_retries_without_duplicate_audit(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    audit = SqlitePaperScanRepository(tmp_path / "paper-audit.sqlite")
    try:
        decision = scan.scan_pair(
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
            fx_snapshots=AUTOFILL_FX,
            maximum_execution_risk=100,
            liquidity_snapshot=_standing(),
        )
        decision.paper_audit_record_id = str(uuid4())
        coordinator = LiveRefreshCoordinator()
        coordinator.reset()
        report = _hot_leftover_report(cancelled=False).model_copy(
            update={"paper_decisions": [decision]}
        )

        def operations_factory(watchlist_arg=None, alerts=None):
            del alerts
            if watchlist_arg is not None:
                ops.watchlist = watchlist_arg
            return ops

        monkeypatch.setattr(paper_api, "get_paper_operations_service", operations_factory)
        real_chain = ops.persist_triggered_chain
        calls = {"n": 0}

        def crash_after_audit(*args: Any, **kwargs: Any):
            calls["n"] += 1
            if calls["n"] == 1:
                raise RuntimeError("crash_after_audit_before_open")
            return real_chain(*args, **kwargs)

        monkeypatch.setattr(ops, "persist_triggered_chain", crash_after_audit)
        await paper_api.persist_scheduled_collection_report(
            coordinator,
            report,
            service=scan,
            audit=audit,
            watchlist=watchlist,
            scan_lane=ScanLane.HOT,
        )
        after_crash = {
            "audit_rows": len(audit.list_scans(limit=10)),
            "open_trades": len(ops.list_active_trades()),
            "persist_ok": coordinator.status.hot.persist_ok,
        }
        assert after_crash["audit_rows"] == 1, after_crash
        assert after_crash["open_trades"] == 0, after_crash
        assert after_crash["persist_ok"] is False, after_crash

        monkeypatch.setattr(ops, "persist_triggered_chain", real_chain)
        await paper_api.persist_scheduled_collection_report(
            coordinator,
            report,
            service=scan,
            audit=audit,
            watchlist=watchlist,
            scan_lane=ScanLane.HOT,
        )
        after_retry = _state_report(
            ops=ops,
            ledger=ledger,
            watchlist=watchlist,
            opportunity_id=next(iter(ops._plans)),
            audit=audit,
        )
        assert after_retry["audit_rows"] == 1, after_retry
        assert after_retry["open_trades"] == 1, after_retry
        assert after_retry["locks"] == 2, after_retry
        assert after_retry["watch_status"] == OpportunityStatus.FILLED.value, after_retry
        assert coordinator.status.hot.persist_ok is True
    finally:
        repository.close()
        ledger.close()
        audit.close()


# ---------------------------------------------------------------------------
# 5. Concurrent same-stamped persistence is fully idempotent
# ---------------------------------------------------------------------------


def test_concurrent_duplicate_retry_is_idempotent_without_valueerror(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        decision = _qualifying_decision(scan, watchlist)
        errors: list[BaseException] = []
        barrier = threading.Barrier(2)

        def worker() -> None:
            try:
                barrier.wait(timeout=5)
                ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(worker) for _ in range(2)]
            for future in as_completed(futures):
                future.result()

        assert errors == [], errors
        opportunity_id = next(iter(ops._plans))
        actual = _state_report(
            ops=ops, ledger=ledger, watchlist=watchlist, opportunity_id=opportunity_id
        )
        assert actual["open_trades"] == 1, actual
        assert actual["locks"] == 2, actual
        assert actual["fill_journals"] == 2, actual
        assert actual["watch_status"] == OpportunityStatus.FILLED.value, actual
        assert actual["autofill_events"] == 1, actual
        assert actual["fills_recorded_events"] == 1, actual
        assert actual["fill_complete_events"] == 1, actual
    finally:
        repository.close()
        ledger.close()


# ---------------------------------------------------------------------------
# Unwind after prior failure/retry remains idempotent
# ---------------------------------------------------------------------------


def test_unwind_after_failure_retry_is_idempotent_one_realised_pnl(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(
        tmp_path / "unwind-retry.sqlite",
        seed_gbp=Decimal("1000"),
        usd_gbp_per_unit=Decimal("0.80"),
        fx_source="paper_demo_fx_snapshot",
    )
    settings = Settings(
        max_slippage_bps=0,
        fx_spread_bps=0,
        simulated_latency_ms=0,
        paper_autofill_enabled=False,
        paper_treasury_seed_gbp=1000,
        paper_treasury_demo_usd_gbp_per_unit=0.80,
        paper_treasury_demo_fx_source="paper_demo_fx_snapshot",
    )
    repository = SqliteMarketIntelligenceRepository()
    scan = PaperScanService(MarketIntelligenceService(repository), settings=settings)
    watchlist = WatchlistService(SqliteWatchlistRepository(), max_quote_age_ms=10_000)
    ops = PaperOperationsService(
        watchlist=watchlist,
        alerts=PriorityAlertService(),
        settings=settings,
        ledger=ledger,
    )
    demo = DemoWalkthroughService(
        operations=ops,
        scan=scan,
        watchlist=watchlist,
        ledger=ledger,
        settings=settings,
    )
    try:
        real_fill = ops.simulate_fill
        calls = {"n": 0}

        def crash_first_fill(*args: Any, **kwargs: Any):
            calls["n"] += 1
            if calls["n"] == 1:
                raise PaperOperationsError("injected_confirm_crash")
            return real_fill(*args, **kwargs)

        ops.simulate_fill = crash_first_fill  # type: ignore[method-assign]
        with pytest.raises(PaperOperationsError, match="injected_confirm_crash"):
            demo.replay(FixtureReplayRequest(venue_pair="matchbook_kalshi", close_via="hold"))
        ops.simulate_fill = real_fill  # type: ignore[method-assign]
        opened = demo.replay(FixtureReplayRequest(venue_pair="matchbook_kalshi", close_via="hold"))
        trade = opened.trade
        assert trade is not None
        assert trade.state is PaperTradeState.OPEN
        quotes = tighten_reverse_quotes(opened.quotes)
        first = ops.complete_validated_unwind(
            trade.trade_id,
            quotes=quotes,
            fx=list(DEMO_FX),
            policy=UNWIND_POLICY,
        )
        assert first.state is PaperTradeState.CLOSED
        assert first.realised_pnl_gbp is not None
        pnl = first.realised_pnl_gbp
        unwind_journals = [
            entry.source_id
            for entry in ops.journal.list_entries(opportunity_id=trade.opportunity_id)
            if entry.source == PAPER_UNWIND_SOURCE
        ]
        assert unwind_journals
        again = ops.complete_validated_unwind(
            trade.trade_id,
            quotes=quotes,
            fx=list(DEMO_FX),
            policy=UNWIND_POLICY,
        )
        assert again.state is PaperTradeState.CLOSED
        assert again.realised_pnl_gbp == pnl
        unwind_again = [
            entry.source_id
            for entry in ops.journal.list_entries(opportunity_id=trade.opportunity_id)
            if entry.source == PAPER_UNWIND_SOURCE
        ]
        assert unwind_again == unwind_journals
        assert ops.list_active_trades() == []
    finally:
        repository.close()
        ledger.close()


# ---------------------------------------------------------------------------
# Architect correction: paper_trade_events IntegrityError is identity-proven
# ---------------------------------------------------------------------------


def test_paper_trade_event_duplicate_same_identity_is_idempotent(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "same-event.sqlite")
    when = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    event = PaperTradeAuditEvent(
        event_id="ptrade-1:fills_recorded",
        occurred_at=when,
        event_type=PaperTradeAuditEventType.FILLS_RECORDED,
        detail="state=OPEN",
    )
    trade = PaperTrade(
        trade_id="ptrade-1",
        opportunity_id="opp-1",
        state=PaperTradeState.OPEN,
        opened_at=when,
        last_updated_at=when,
        audit=[event, event],
    )
    try:
        ledger.trades.save(trade)
        ledger.trades.save(
            trade.model_copy(
                update={"audit": [event, event.model_copy()]}
            )
        )
        rows = list(
            ledger._connection.execute(
                "SELECT event_id, trade_id, event_type, detail FROM paper_trade_events"
            )
        )
        assert len(rows) == 1, rows
        assert rows[0]["event_id"] == "ptrade-1:fills_recorded"
        assert rows[0]["trade_id"] == "ptrade-1"
        assert rows[0]["event_type"] == PaperTradeAuditEventType.FILLS_RECORDED.value
        assert rows[0]["detail"] == "state=OPEN"
        reloaded = ledger.trades.get("ptrade-1")
        assert reloaded is not None
        assert [item.event_id for item in reloaded.audit] == ["ptrade-1:fills_recorded"]
    finally:
        ledger.close()


def test_paper_trade_event_unrelated_integrity_error_propagates(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "unrelated-event.sqlite")
    when = datetime(2026, 9, 16, 12, 0, tzinfo=UTC)
    first = PaperTradeAuditEvent(
        event_id="evt-a",
        occurred_at=when,
        event_type=PaperTradeAuditEventType.FILLS_RECORDED,
        detail="shared-detail",
    )
    trade = PaperTrade(
        trade_id="ptrade-1",
        opportunity_id="opp-1",
        state=PaperTradeState.OPEN,
        opened_at=when,
        last_updated_at=when,
        audit=[first],
    )
    try:
        ledger.trades.save(trade)
        ledger._connection.execute(
            "CREATE UNIQUE INDEX paper_trade_events_detail_unique "
            "ON paper_trade_events(detail)"
        )
        second = PaperTradeAuditEvent(
            event_id="evt-b",
            occurred_at=when,
            event_type=PaperTradeAuditEventType.TRADE_OPENED,
            detail="shared-detail",
        )
        with pytest.raises(sqlite3.IntegrityError):
            ledger.trades.save(trade.model_copy(update={"audit": [first, second]}))
        remaining = list(
            ledger._connection.execute(
                "SELECT event_id, trade_id, event_type, detail FROM paper_trade_events"
            )
        )
        assert [row["event_id"] for row in remaining] == ["evt-a"]
        assert remaining[0]["detail"] == "shared-detail"

        clash = PaperTrade(
            trade_id="ptrade-2",
            opportunity_id="opp-2",
            state=PaperTradeState.OPEN,
            opened_at=when,
            last_updated_at=when,
            audit=[
                PaperTradeAuditEvent(
                    event_id="evt-a",
                    occurred_at=when,
                    event_type=PaperTradeAuditEventType.TRADE_OPENED,
                    detail="other-detail",
                )
            ],
        )
        with pytest.raises(sqlite3.IntegrityError):
            ledger.trades.save(clash)
        after_clash = list(
            ledger._connection.execute(
                "SELECT event_id, trade_id, event_type, detail FROM paper_trade_events"
            )
        )
        assert [row["event_id"] for row in after_clash] == ["evt-a"]
        assert after_clash[0]["trade_id"] == "ptrade-1"
        assert after_clash[0]["event_type"] == PaperTradeAuditEventType.FILLS_RECORDED.value
        assert after_clash[0]["detail"] == "shared-detail"
    finally:
        ledger.close()


# ---------------------------------------------------------------------------
# Architect correction: interrupted PENDING/PARTIAL retry is not a healthy repeat
# ---------------------------------------------------------------------------


def test_interrupted_pending_partial_retry_completes_or_fails_closed(tmp_path: Path) -> None:
    for interrupted_state in (PaperTradeState.PENDING, PaperTradeState.PARTIAL):
        case_dir = tmp_path / interrupted_state.value
        case_dir.mkdir()
        scan, watchlist, ops, repository, ledger = _ops_bundle(case_dir, autofill=False)
        try:
            decision = _qualifying_decision(scan, watchlist)
            ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER)
            opportunity_id = next(iter(ops._plans))
            plan = ops._plans[opportunity_id]
            opportunity = ops.watchlist.repository.get(opportunity_id)
            assert opportunity is not None
            shell = ops._new_trade_shell(
                plan, opportunity, datetime.now(UTC), DataProvenance.LIVE_PAPER
            )
            shell.state = interrupted_state
            ops.trades.save(shell)
            before = _state_report(
                ops=ops, ledger=ledger, watchlist=watchlist, opportunity_id=opportunity_id
            )
            assert before["trade_state"] == interrupted_state.value, before
            assert before["locks"] == 0, before
            assert before["fill_journals"] == 0, before
            assert before["watch_status"] != OpportunityStatus.FILLED.value, before

            retry = ops.simulate_fill(
                opportunity_id,
                simulate_external=True,
                provenance=DataProvenance.LIVE_PAPER,
            )
            loaded = ops.list_active_trades()
            assert len(loaded) == 1, loaded
            assert loaded[0].trade_id == shell.trade_id
            assert loaded[0].state is PaperTradeState.OPEN, loaded[0].state
            actual = _state_report(
                ops=ops, ledger=ledger, watchlist=watchlist, opportunity_id=opportunity_id
            )
            expected = {
                "open_trades": 1,
                "trade_state": PaperTradeState.OPEN.value,
                "locks": 2,
                "fill_journals": 2,
                "watch_status": OpportunityStatus.FILLED.value,
                "autofill_events": 1,
                "fills_recorded_events": 1,
                "fill_complete_events": 1,
            }
            for key, value in expected.items():
                assert actual[key] == value, (interrupted_state, key, actual)
            assert retry.entry_complete is True
            assert retry.trade_id == shell.trade_id
            assert PaperTradeAuditEventType.REPEAT_OBSERVATION_NO_TOP_UP not in _audit_types(
                ops, shell.trade_id
            )
        finally:
            repository.close()
            ledger.close()

