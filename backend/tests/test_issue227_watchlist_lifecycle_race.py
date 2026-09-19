"""Issue #227 / Wave N3: watchlist lifecycle serialization under paper persistence.

K4 head `023ae08` reproduced a deterministic overlap:

- persist completes durable OPEN + capital locks + FILLED
- a genuinely-new same-economics `WatchlistService.observe()` read TRIGGERED,
  then upserted TRIGGERED over FILLED
- OPEN + locks + fill journals remained durable
- later serialized retry repaired status, but current-state truth was briefly
  inconsistent
- shared sqlite3.Connection also raised InterfaceError under contention

Synchronization boundary: SqliteWatchlistRepository RLock + BEGIN IMMEDIATE
around observe / record_paper_fill / close / expire / freshness mutation.
Observation statuses cannot overwrite PAPER_FILLING/PARTIAL/FILLED/CLOSED/EXPIRED.

Data class: deterministic fixture/demo paper-scan payloads. Not live,
historical, or modelled venue quotes. Phase 1 remains paper-only / read-only
toward venues.
"""

from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from sports_hedge.accounting.paper_journal import DataProvenance
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.watchlist.models import LifecycleEventType, OpportunityStatus
from sports_hedge.arbitrage.watchlist.ranking import opportunity_id_for_canonical_market
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.trades import PaperTradeAuditEventType, PaperTradeState
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from test_issue208_paper_crash_consistency import (
    _count_type,
    _fill_complete_events,
    _fill_journals,
    _lock_rows,
    _state_report,
)
from test_near_arbitrage_watchlist import EDGE_080, OBSERVED, _observation
from test_step8f_automatic_paper_entry import (
    FX as AUTOFILL_FX,
    _matchbook_btts,
    _kalshi_btts,
    _kalshi_costs,
    _standing,
)


def _file_ops_bundle(tmp_path: Path, *, autofill: bool = True):
    ledger = SqlitePaperLedger(
        tmp_path / "paper.sqlite",
        seed_gbp=Decimal("1000"),
        usd_gbp_per_unit=Decimal("0.75"),
        fx_source="test",
    )
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    settings = Settings(
        max_slippage_bps=0,
        fx_spread_bps=0,
        simulated_latency_ms=0,
        paper_autofill_enabled=autofill,
    )
    scan = PaperScanService(intelligence, settings=settings)
    watch_path = tmp_path / "watchlist.sqlite"
    watchlist = WatchlistService(
        SqliteWatchlistRepository(watch_path),
        max_quote_age_ms=10_000,
    )
    ops = PaperOperationsService(
        watchlist=watchlist,
        alerts=PriorityAlertService(),
        settings=settings,
        ledger=ledger,
    )
    return scan, watchlist, ops, repository, ledger, watch_path


def _qualifying_pair(scan, extra=None):
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
    return decision


def _new_scan_same_economics(scan, *, observed_at: datetime | None = None):
    decision = _qualifying_pair(scan)
    if observed_at is not None:
        decision = decision.model_copy(update={"scanned_at": observed_at})
    decision.paper_audit_record_id = None
    return decision


def _assert_open_filled(report: dict[str, Any], *, audit_rows: int | None = None) -> None:
    assert report["open_trades"] == 1, report
    assert report["trade_state"] == PaperTradeState.OPEN.value, report
    assert report["locks"] == 2, report
    assert report["fill_journals"] == 2, report
    assert report["watch_status"] == OpportunityStatus.FILLED.value, report
    assert report["autofill_events"] == 1, report
    assert report["fills_recorded_events"] == 1, report
    assert report["fill_complete_events"] == 1, report
    if audit_rows is not None:
        assert report["audit_rows"] == audit_rows, report


def test_forced_open_locks_observe_overlap_does_not_regress_filled(tmp_path: Path) -> None:
    """Barrier overlap: persist has durable OPEN+locks, FILLED not yet written,
    genuinely-new same-economics observe races record_paper_fill.
    """

    scan, watchlist, ops, repository, ledger, _watch_path = _file_ops_bundle(tmp_path)
    audit = SqlitePaperScanRepository(tmp_path / "paper-audit.sqlite")
    try:
        first = _qualifying_pair(scan)
        first.paper_audit_record_id = str(uuid4())
        history = scan.market_intelligence.market_history(
            canonical_market_id=first.canonical_market_id
        )
        seeded = watchlist.observe_paper_decision(first, history)
        opportunity_id = seeded.opportunity_id
        assert seeded.status is OpportunityStatus.TRIGGERED
        phase_seed = _state_report(
            ops=ops, ledger=ledger, watchlist=watchlist, opportunity_id=opportunity_id, audit=audit
        )
        assert phase_seed["open_trades"] == 0
        assert phase_seed["watch_status"] == OpportunityStatus.TRIGGERED.value
        assert phase_seed["locks"] == 0

        open_ready = threading.Event()
        overlap = threading.Barrier(2)
        errors: list[BaseException] = []
        phases: dict[str, dict[str, Any]] = {"seed": phase_seed}
        original_record = watchlist.record_paper_fill

        def racing_record(*args: Any, **kwargs: Any):
            open_ready.set()
            overlap.wait(timeout=5)
            return original_record(*args, **kwargs)

        watchlist.record_paper_fill = racing_record  # type: ignore[method-assign]
        second = _new_scan_same_economics(
            scan, observed_at=datetime.now(UTC) + timedelta(seconds=1)
        )
        second.paper_audit_record_id = str(uuid4())

        def persist_worker() -> None:
            try:
                ops.persist_triggered_chain(first, provenance=DataProvenance.LIVE_PAPER)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def observe_worker() -> None:
            try:
                assert open_ready.wait(timeout=5), "persist never reached watchlist FILLED"
                phases["after_open_before_filled"] = _state_report(
                    ops=ops,
                    ledger=ledger,
                    watchlist=watchlist,
                    opportunity_id=opportunity_id,
                    audit=audit,
                )
                overlap.wait(timeout=5)
                watchlist.observe_paper_decision(second, history)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(persist_worker), pool.submit(observe_worker)]
            for future in as_completed(futures):
                future.result()

        assert errors == [], errors
        before_filled = phases["after_open_before_filled"]
        assert before_filled["open_trades"] == 1, before_filled
        assert before_filled["trade_state"] == PaperTradeState.OPEN.value, before_filled
        assert before_filled["locks"] == 2, before_filled
        assert before_filled["fill_journals"] == 2, before_filled
        assert before_filled["watch_status"] == OpportunityStatus.PAPER_FILLING.value, before_filled

        after = _state_report(
            ops=ops, ledger=ledger, watchlist=watchlist, opportunity_id=opportunity_id, audit=audit
        )
        _assert_open_filled(after)
        observations = watchlist.repository.list_observations(opportunity_id)
        assert len(observations) >= 2, observations
        events = watchlist.repository.list_events(opportunity_id=opportunity_id, limit=100)
        assert (
            sum(1 for item in events if item.event_type is LifecycleEventType.PAPER_FILL_COMPLETE)
            == 1
        )
        trade = ops.list_active_trades()[0]
        assert trade.state is PaperTradeState.OPEN
        assert (
            _count_type(
                [item.event_type for item in trade.audit],
                PaperTradeAuditEventType.PAPER_AUTOFILL,
            )
            == 1
        )

        watchlist.record_paper_fill = original_record  # type: ignore[method-assign]
        ops.persist_triggered_chain(first, provenance=DataProvenance.LIVE_PAPER)
        retry = _state_report(
            ops=ops, ledger=ledger, watchlist=watchlist, opportunity_id=opportunity_id, audit=audit
        )
        _assert_open_filled(retry)
        assert retry["spendable"] == after["spendable"]
        assert retry["locked"] == after["locked"]
    finally:
        repository.close()
        ledger.close()
        audit.close()
        watchlist.repository.close()


def test_reopened_connection_observe_cannot_regress_filled(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, watch_path = _file_ops_bundle(tmp_path)
    try:
        first = _qualifying_pair(scan)
        history = scan.market_intelligence.market_history(
            canonical_market_id=first.canonical_market_id
        )
        seeded = watchlist.observe_paper_decision(first, history)
        opportunity_id = seeded.opportunity_id
        open_ready = threading.Event()
        overlap = threading.Barrier(2)
        errors: list[BaseException] = []
        original_record = watchlist.record_paper_fill

        def racing_record(*args: Any, **kwargs: Any):
            open_ready.set()
            overlap.wait(timeout=5)
            return original_record(*args, **kwargs)

        watchlist.record_paper_fill = racing_record  # type: ignore[method-assign]
        second = _new_scan_same_economics(
            scan, observed_at=datetime.now(UTC) + timedelta(seconds=2)
        )

        def persist_worker() -> None:
            try:
                ops.persist_triggered_chain(first, provenance=DataProvenance.LIVE_PAPER)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def observe_worker() -> None:
            other = WatchlistService(
                SqliteWatchlistRepository(watch_path),
                max_quote_age_ms=10_000,
            )
            try:
                assert open_ready.wait(timeout=5)
                overlap.wait(timeout=5)
                other.observe_paper_decision(second, history)
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)
            finally:
                other.repository.close()

        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(persist_worker), pool.submit(observe_worker)]
            for future in as_completed(futures):
                future.result()

        assert errors == [], errors
        after = _state_report(
            ops=ops, ledger=ledger, watchlist=watchlist, opportunity_id=opportunity_id
        )
        _assert_open_filled(after)
        reopened = SqliteWatchlistRepository(watch_path)
        try:
            row = reopened.get(opportunity_id)
            assert row is not None
            assert row.status is OpportunityStatus.FILLED
        finally:
            reopened.close()
    finally:
        repository.close()
        ledger.close()
        watchlist.repository.close()


def test_soak_contention_no_interfaceerror_keeps_single_open_filled(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, watch_path = _file_ops_bundle(tmp_path)
    extras: list[WatchlistService] = []
    try:
        first = _qualifying_pair(scan)
        history = scan.market_intelligence.market_history(
            canonical_market_id=first.canonical_market_id
        )
        seeded = watchlist.observe_paper_decision(first, history)
        opportunity_id = seeded.opportunity_id
        ops.persist_triggered_chain(first, provenance=DataProvenance.LIVE_PAPER)
        baseline = _state_report(
            ops=ops, ledger=ledger, watchlist=watchlist, opportunity_id=opportunity_id
        )
        _assert_open_filled(baseline)
        template = _new_scan_same_economics(scan)
        extras = [
            WatchlistService(
                SqliteWatchlistRepository(watch_path),
                max_quote_age_ms=10_000,
            )
            for _ in range(3)
        ]

        start = threading.Barrier(8)
        errors: list[BaseException] = []
        saw_regressed = threading.Event()

        def check(row) -> None:
            if row is None or row.status is OpportunityStatus.TRIGGERED:
                saw_regressed.set()

        def observe_shared(worker_id: int) -> None:
            try:
                start.wait(timeout=5)
                for step in range(20):
                    decision = template.model_copy(
                        update={
                            "scanned_at": datetime.now(UTC)
                            + timedelta(milliseconds=worker_id * 40 + step)
                        }
                    )
                    watchlist.observe_paper_decision(decision, history)
                    check(watchlist.repository.get(opportunity_id))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def observe_reopened(service: WatchlistService, worker_id: int) -> None:
            try:
                start.wait(timeout=5)
                for step in range(20):
                    decision = template.model_copy(
                        update={
                            "scanned_at": datetime.now(UTC)
                            + timedelta(milliseconds=400 + worker_id * 40 + step)
                        }
                    )
                    service.observe_paper_decision(decision, history)
                    check(service.repository.get(opportunity_id))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        def persist_worker() -> None:
            try:
                start.wait(timeout=5)
                for _ in range(20):
                    ops.persist_triggered_chain(first, provenance=DataProvenance.LIVE_PAPER)
                    watchlist.triggered(limit=10)
                    watchlist.activity(limit=25)
                    check(watchlist.repository.get(opportunity_id))
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(observe_shared, idx) for idx in range(3)]
            futures.extend(
                pool.submit(observe_reopened, extras[idx], idx) for idx in range(3)
            )
            futures.extend(pool.submit(persist_worker) for _ in range(2))
            for future in as_completed(futures):
                future.result()

        assert errors == [], errors
        assert not saw_regressed.is_set()
        after = _state_report(
            ops=ops, ledger=ledger, watchlist=watchlist, opportunity_id=opportunity_id
        )
        _assert_open_filled(after)
        assert after["spendable"] == baseline["spendable"]
        assert after["locked"] == baseline["locked"]
        assert len(ops.list_active_trades()) == 1
        assert len(_lock_rows(ledger, opportunity_id=opportunity_id)) == 2
        assert len(_fill_journals(ops)) == 2
        assert len(_fill_complete_events(watchlist, opportunity_id)) == 1
    finally:
        for extra in extras:
            extra.repository.close()
        repository.close()
        ledger.close()
        watchlist.repository.close()


def test_new_scan_after_filled_appends_observation_without_status_regression(
    tmp_path: Path,
) -> None:
    scan, watchlist, ops, repository, ledger, _watch_path = _file_ops_bundle(tmp_path)
    audit = SqlitePaperScanRepository(tmp_path / "paper-audit.sqlite")
    try:
        first = _qualifying_pair(scan)
        first.paper_audit_record_id = str(uuid4())
        history = scan.market_intelligence.market_history(
            canonical_market_id=first.canonical_market_id
        )
        watchlist.observe_paper_decision(first, history)
        ops.persist_triggered_chain(first, provenance=DataProvenance.LIVE_PAPER)
        opportunity_id = opportunity_id_for_canonical_market(first.canonical_market_id)
        before = watchlist.repository.list_observations(opportunity_id)
        second = _new_scan_same_economics(
            scan, observed_at=datetime.now(UTC) + timedelta(seconds=3)
        )
        second.paper_audit_record_id = str(uuid4())
        observed = watchlist.observe_paper_decision(second, history)
        assert observed.status is OpportunityStatus.FILLED
        after_obs = watchlist.repository.list_observations(opportunity_id)
        assert len(after_obs) == len(before) + 1
        assert after_obs[-1].status is OpportunityStatus.FILLED
        ops.persist_triggered_chain(second, provenance=DataProvenance.LIVE_PAPER)
        after = _state_report(
            ops=ops, ledger=ledger, watchlist=watchlist, opportunity_id=opportunity_id, audit=audit
        )
        _assert_open_filled(after)
    finally:
        repository.close()
        ledger.close()
        audit.close()
        watchlist.repository.close()


def test_unfilled_triggered_can_still_requalify_to_approaching() -> None:
    service = WatchlistService(SqliteWatchlistRepository(), max_quote_age_ms=10_000)
    try:
        triggered = service.observe(
            _observation(
                edge=Decimal("0.012"),
                eligible=True,
                guaranteed_profit_gbp=Decimal("1.10"),
            )
        )
        assert triggered.status is OpportunityStatus.TRIGGERED
        moved = service.observe(
            _observation(
                edge=EDGE_080,
                eligible=False,
                observed_at=OBSERVED + timedelta(seconds=2),
            )
        )
        assert moved.status is OpportunityStatus.APPROACHING
    finally:
        service.repository.close()


def test_observation_upsert_cannot_overwrite_filled_status(tmp_path: Path) -> None:
    repository = SqliteWatchlistRepository(tmp_path / "wl.sqlite")
    service = WatchlistService(repository, max_quote_age_ms=10_000)
    try:
        triggered = service.observe(
            _observation(
                edge=Decimal("0.012"),
                eligible=True,
                guaranteed_profit_gbp=Decimal("1.10"),
            )
        )
        filled = service.record_paper_fill(
            triggered.opportunity_id,
            stage=OpportunityStatus.FILLED,
            occurred_at=OBSERVED + timedelta(seconds=1),
        )
        assert filled.status is OpportunityStatus.FILLED
        regressing = filled.model_copy(
            update={
                "status": OpportunityStatus.TRIGGERED,
                "classification": triggered.classification,
                "is_arbitrage": True,
            }
        )
        repository.upsert_opportunity(regressing)
        stored = repository.get(triggered.opportunity_id)
        assert stored is not None
        assert stored.status is OpportunityStatus.FILLED
        assert stored.is_arbitrage is False
    finally:
        repository.close()
