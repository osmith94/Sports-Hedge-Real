"""Issue #21: capture must not full-scan the watchlist; live dispatch must yield.

Data class: fixture/demo watchlist rows and fake execution transports.
No provider calls, no real venue writes. Default process execution stays off.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from loop_liveness_harness import HeartbeatProbe

from sports_hedge.api.paper import recover_orphaned_live_executions_at_startup
from sports_hedge.application.execution_reprice import capture_with_execution_reprice
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.arbitrage.watchlist.economics import classification_for
from sports_hedge.arbitrage.watchlist.models import (
    ORPHANED_PAPER_FILLING_RECONCILED,
    NearOpportunity,
    OpportunityStatus,
    PaperFillAttemptStatus,
)
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.execution.clients import DeterministicExecutionTransport
from sports_hedge.execution.dispatch import run_blocking
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from test_paper_entry_snapshot_freshness import (
    T1,
    _freshness_bundle,
    _observe,
    _qualify,
)
from test_real_wave3_live_dispatch import (
    _armed,
    _clients,
    _hedge_legs,
    _install_solver,
    _ops,
    _persist,
    _plan,
)

NOW = datetime(2026, 10, 9, 12, tzinfo=UTC)
WATCHLIST_SIZE = 500
LIVENESS_BOUND_S = 0.25
FAKE_VENUE_DELAY_S = 0.35


def _decision(canonical_market_id: str) -> PaperScanDecision:
    return PaperScanDecision(
        scanned_at=NOW,
        canonical_market_id=canonical_market_id,
        market_match=MarketMatchResult(
            matched=True,
            confidence=1.0,
            reasons=["register"],
            register_key="MATCH_RESULT_FT",
        ),
    )


def _seed_watch(
    repo: SqliteWatchlistRepository,
    *,
    n: int,
    status: OpportunityStatus = OpportunityStatus.WATCHING,
) -> None:
    for index in range(n):
        repo.upsert_opportunity(
            NearOpportunity(
                opportunity_id=f"watch:mkt-{index}",
                canonical_event_id=f"evt-{index}",
                canonical_market_id=f"mkt-{index}",
                status=status,
                classification=classification_for(status),
                is_arbitrage=status is OpportunityStatus.TRIGGERED,
                first_seen_at=NOW,
                last_seen_at=NOW,
            ),
            force_status=True,
        )


def test_persist_triggered_chain_does_not_list_the_watchlist() -> None:
    source = inspect.getsource(PaperOperationsService.persist_triggered_chain)
    assert "reconcile_orphaned_paper_fills" not in source
    assert "list_opportunities" not in source
    startup = inspect.getsource(recover_orphaned_live_executions_at_startup)
    assert "reconcile_orphaned_paper_fills" in startup
    capture = inspect.getsource(capture_with_execution_reprice)
    assert "asyncio.to_thread" in capture
    assert "persist_price_engine_item_capture" in capture


def test_routine_capture_does_not_scan_full_watchlist() -> None:
    repo = SqliteWatchlistRepository()
    watchlist = WatchlistService(repo)
    _seed_watch(repo, n=WATCHLIST_SIZE)
    ops = PaperOperationsService(watchlist=watchlist, settings=Settings())
    calls = {"n": 0, "rows": 0}
    original = repo.list_opportunities

    def wrapped(*, status=None):
        rows = original(status=status)
        calls["n"] += 1
        calls["rows"] += len(rows)
        return rows

    repo.list_opportunities = wrapped  # type: ignore[method-assign]
    times: list[float] = []
    for _ in range(5):
        started = time.perf_counter()
        ops.persist_triggered_chain(_decision("mkt-capture"), autofill=False, now=NOW)
        times.append((time.perf_counter() - started) * 1000)
    assert calls["n"] == 0
    assert calls["rows"] == 0
    assert ops.settings.sports_hedge_execution_enabled is False
    assert times  # comparable 500-row timings are recorded for the PR, not a SLA


def test_unrelated_capture_does_not_reconcile_another_orphan() -> None:
    repo = SqliteWatchlistRepository()
    watchlist = WatchlistService(repo)
    orphan_id = "watch:mkt-orphan"
    repo.upsert_opportunity(
        NearOpportunity(
            opportunity_id=orphan_id,
            canonical_event_id="evt-orphan",
            canonical_market_id="mkt-orphan",
            status=OpportunityStatus.TRIGGERED,
            classification=classification_for(OpportunityStatus.TRIGGERED),
            is_arbitrage=True,
            first_seen_at=NOW,
            last_seen_at=NOW,
        ),
        force_status=True,
    )
    watchlist.begin_paper_fill_attempt(
        orphan_id,
        occurred_at=NOW,
        bind_snapshot=True,
        decision_at=NOW,
    )
    ops = PaperOperationsService(watchlist=watchlist, settings=Settings())
    ops.persist_triggered_chain(_decision("mkt-other"), autofill=False, now=NOW)
    leftover = repo.get(orphan_id)
    assert leftover is not None
    assert leftover.status is OpportunityStatus.PAPER_FILLING
    recovered = ops.reconcile_orphaned_paper_fills(now=NOW + timedelta(seconds=1))
    assert recovered == [orphan_id]
    closed = repo.get(orphan_id)
    assert closed is not None
    assert closed.status is OpportunityStatus.REJECTED
    assert ORPHANED_PAPER_FILLING_RECONCILED in closed.rejection_reasons


def test_targeted_orphan_still_reconciles_on_that_opportunity_capture(
    tmp_path: Path,
) -> None:
    watchlist_db = tmp_path / "watchlist.sqlite"
    scan, watchlist, ops, repository, ledger, settings = _freshness_bundle(
        tmp_path, autofill=False, watchlist_db=watchlist_db
    )
    try:
        decision = _qualify(scan, age_ms=300)
        seeded = _observe(scan, watchlist, decision)
        ops.persist_triggered_chain(decision, autofill=False)
        watchlist.begin_paper_fill_attempt(
            seeded.opportunity_id,
            occurred_at=T1,
            bind_snapshot=True,
            decision_at=decision.scanned_at,
        )
        assert watchlist.repository.get(seeded.opportunity_id).status is OpportunityStatus.PAPER_FILLING
        assert ops.list_active_trades() == []
    finally:
        watchlist.repository.close()
        repository.close()
        ledger.close()

    scan2, watchlist2, ops2, repository2, ledger2, _settings2 = _freshness_bundle(
        tmp_path, autofill=True, watchlist_db=watchlist_db
    )
    try:
        later = T1 + timedelta(seconds=2)
        fresh = _qualify(scan2, age_ms=300, scanned_at=later)
        _observe(scan2, watchlist2, fresh)
        ops2.persist_triggered_chain(
            fresh,
            autofill=True,
            now=later + timedelta(milliseconds=200),
        )
        first = watchlist2.latest_paper_fill_attempt(seeded.opportunity_id)
        assert first is not None
        assert first.status is PaperFillAttemptStatus.COMPLETE
        row = watchlist2.repository.get(seeded.opportunity_id)
        assert row is not None
        assert row.status is OpportunityStatus.FILLED
        assert len(ops2.list_active_trades()) == 1
        assert settings.sports_hedge_execution_enabled is False
    finally:
        watchlist2.repository.close()
        repository2.close()
        ledger2.close()


@pytest.mark.asyncio
async def test_run_blocking_on_running_loop_stalls_heartbeat() -> None:
    """Failing-first proof: joining a delayed fake transport on the loop stalls."""

    async def delayed() -> str:
        await asyncio.sleep(FAKE_VENUE_DELAY_S)
        return "done"

    probe = HeartbeatProbe(interval_s=0.02).start()
    await asyncio.sleep(0.05)
    assert run_blocking(delayed()) == "done"
    await probe.stop()
    assert probe.worst_s >= LIVENESS_BOUND_S


@pytest.mark.asyncio
async def test_price2_persist_off_loop_keeps_heartbeat_with_fake_delayed_venues(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Scanner handoff: persist/dispatch off the loop while fake venues delay."""

    _install_solver(monkeypatch)
    matchbook = DeterministicExecutionTransport(delay_seconds=FAKE_VENUE_DELAY_S)
    kalshi = DeterministicExecutionTransport(delay_seconds=FAKE_VENUE_DELAY_S)
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(_armed(), runtime=_clients(matchbook, kalshi), ledger=ledger)
    probe = HeartbeatProbe(interval_s=0.02).start()
    await asyncio.sleep(0.05)
    await asyncio.to_thread(_persist, ops, _plan(*_hedge_legs()))
    await probe.stop()
    assert probe.worst_s < LIVENESS_BOUND_S
    assert len(matchbook.calls) == 1
    assert len(kalshi.calls) == 1
    trade = ledger.trades.get_by_opportunity(_plan(*_hedge_legs()).opportunity_id)
    assert trade is not None
    assert trade.places_orders is True
    assert ops.settings.sports_hedge_mode == "real"


def test_duplicate_live_reservation_still_blocks_second_submit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _install_solver(monkeypatch)
    matchbook = DeterministicExecutionTransport()
    kalshi = DeterministicExecutionTransport()
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(_armed(), runtime=_clients(matchbook, kalshi), ledger=ledger)
    plan = _plan(*_hedge_legs())
    _persist(ops, plan)
    _persist(ops, plan)
    assert len(matchbook.calls) == 1
    assert len(kalshi.calls) == 1
    assert len(ops.list_active_trades()) == 1
