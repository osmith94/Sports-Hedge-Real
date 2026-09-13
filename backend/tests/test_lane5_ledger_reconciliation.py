"""Lane 5: ledger reconciliation, persistence/restart, adversarial lifecycle.

Certifies financial truth on the audited PR #115 paper subledger. This is not
the #116 Finance UI. Data here is modelled/fixture paper, never live venue cash.
"""

from __future__ import annotations

import asyncio
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sports_hedge.accounting.dimensions import (
    AttributionScope,
    CapitalSource,
    CashState,
    PostingDimensions,
    PostingSide,
    cash_account,
)
from sports_hedge.accounting.paper_journal import DataProvenance, PaperJournalEntry, PaperJournalPosting
from sports_hedge.accounting.reconciliation import LedgerReconciliationError, reconcile_paper_ledger
from sports_hedge.api.main import app
from sports_hedge.api.paper import get_paper_ledger
from sports_hedge.application.live_refresh import get_live_refresh_coordinator
from sports_hedge.arbitrage.watchlist.models import LifecycleEventType
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.config import get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.paper.fills import PaperFillConfig, PaperOpportunityLeg
from sports_hedge.paper.simulator import PaperFillSimulator
from sports_hedge.paper.trades import PaperSettlementRequest, PaperTradeState
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.treasury.models import (
    PaperTreasuryEvent,
    PaperTreasuryEventType,
    TreasuryLockRequest,
    UnwindReleaseLeg,
    ValidatedUnwindResult,
)
from sports_hedge.treasury.service import PaperTreasuryError
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient
from test_paper_trade_lifecycle import OBSERVED, _ops, independent_realised_pnl_gbp
from test_read_only_collector import FakePolymarket


NOW = datetime(2026, 9, 13, 13, 0, tzinfo=UTC)
SEED = Decimal("1000")
FX = Decimal("0.80")


def _lock_snapshot(ledger: SqlitePaperLedger) -> list[tuple[str, ...]]:
    session = ledger.treasury.active_session()
    assert session is not None
    rows = ledger._connection.execute(
        """
        SELECT lock_id, venue, native_currency, locked_native, released_native, status, trade_id
        FROM paper_treasury_locks
        WHERE session_id = ?
        ORDER BY lock_id
        """,
        (session.session_id,),
    ).fetchall()
    return [
        (
            row["lock_id"],
            row["venue"],
            row["native_currency"],
            row["locked_native"],
            row["released_native"],
            row["status"],
            row["trade_id"],
        )
        for row in rows
    ]


def _journal_facts(ledger: SqlitePaperLedger) -> list[tuple[str, str, str]]:
    return [
        (entry.source, entry.source_id, entry.description)
        for entry in ledger.journal.list_entries()
    ]


def _leg_facts(trade) -> list[tuple[object, ...]]:
    return [
        (
            leg.venue,
            leg.outcome,
            leg.currency,
            str(leg.requested_stake),
            str(leg.filled_stake),
            str(leg.filled_odds) if leg.filled_odds is not None else None,
            leg.fill_id,
            leg.fill_kind,
            leg.capital_source,
        )
        for leg in trade.legs
    ]


def test_seed_treasury_reconciles_and_keeps_native_pools_separate() -> None:
    ledger = SqlitePaperLedger(seed_gbp=SEED, usd_gbp_per_unit=FX)
    try:
        report = ledger.reconcile()
        assert report.ok
        assert report.gbp_journals_balanced is True
        sources = {entry.source for entry in ledger.journal.list_entries()}
        assert sources == {"paper_treasury_seed"}
        snap = ledger.treasury.snapshot()
        assert snap.pool(VenueName.MATCHBOOK, "GBP").available_cash == SEED
        assert snap.pool(VenueName.POLYMARKET, "USD").identity != snap.pool(
            VenueName.KALSHI, "USD"
        ).identity
        with pytest.raises(ValueError, match="must not be summed"):
            snap.combined_cash_gbp()
        assert report.native_locked["matchbook/GBP"] == "0"
    finally:
        ledger.close()


def test_contribution_lock_and_adjust_are_distinct_journal_sources() -> None:
    ledger = SqlitePaperLedger(seed_gbp=SEED, usd_gbp_per_unit=FX)
    try:
        ledger.treasury.lock_capital(
            [
                TreasuryLockRequest(
                    venue=VenueName.MATCHBOOK,
                    native_currency="GBP",
                    amount_native=Decimal("100"),
                    lock_id="lock-distinct",
                    trade_id="ptrade-distinct",
                    opportunity_id="opp-distinct",
                    source="paper_fill_simulator",
                    fx_rate_gbp_per_unit=Decimal("1"),
                )
            ],
            occurred_at=NOW,
        )
        ledger.treasury.post_unwind(
            ValidatedUnwindResult(
                trade_id="ptrade-distinct",
                close_completed=True,
                opportunity_id="opp-distinct",
                source="paper_unwind",
                source_id="unwind-distinct",
                releases=[
                    UnwindReleaseLeg(
                        venue=VenueName.MATCHBOOK,
                        native_currency="GBP",
                        lock_id="lock-distinct",
                        amount_native=Decimal("100"),
                        realised_pnl_native=Decimal("0"),
                        fee_native=Decimal("0"),
                        fx_rate_gbp_per_unit=Decimal("1"),
                    )
                ],
            ),
            now=NOW,
        )
        ledger.treasury.set_available_amounts(
            {VenueName.MATCHBOOK: Decimal("1200")},
            reason="operator paper treasury edit",
            now=NOW,
        )
        report = ledger.reconcile()
        assert report.ok
        sources = {entry.source for entry in ledger.journal.list_entries()}
        assert {
            "paper_treasury_seed",
            "paper_fill_simulator",
            "paper_unwind",
            "paper_treasury_adjust",
        } <= sources
        assert sources.isdisjoint({"watchlist", "paper_scan", "gpt", "llm"})
        matchbook = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP")
        assert matchbook.available_cash == Decimal("1200")
        assert matchbook.locked_capital == Decimal("0")
    finally:
        ledger.close()


def test_open_trade_journals_locks_and_survives_stop_restart(tmp_path: Path) -> None:
    path = tmp_path / "lane5-open.sqlite"
    ledger = SqlitePaperLedger(path)
    _scan, watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        report = ledger.reconcile()
        assert report.ok
        trades = ops.list_active_trades()
        assert len(trades) == 1
        trade = trades[0]
        assert trade.state is PaperTradeState.OPEN
        assert trade.entry_risk is not None
        assert trade.legs
        sources = {entry.source for entry in ledger.journal.list_entries()}
        assert "paper_fill_simulator" in sources
        assert "paper_treasury_seed" in sources
        assert watchlist.repository.get(trade.opportunity_id) is not None
        gl_ids = {(entry.source, entry.source_id) for entry in ledger.journal.list_entries()}
        for event in watchlist.activity(opportunity_id=trade.opportunity_id):
            assert (event.event_type.value, event.event_id) not in gl_ids
        before_legs = _leg_facts(trade)
        before_risk = trade.entry_risk.model_dump(mode="json")
        before_locks = _lock_snapshot(ledger)
        before_journals = _journal_facts(ledger)
        before_locked = dict(trade.capital_locked_native)
        pools = {
            f"{pool.venue.value}/{pool.native_currency}": (
                pool.available_cash,
                pool.locked_capital,
            )
            for pool in ledger.treasury.snapshot().pools
        }
    finally:
        repository.close()
        ledger.close()

    reopened = SqlitePaperLedger(path, auto_seed=False)
    try:
        again = reconcile_paper_ledger(reopened)
        assert again.ok
        loaded = reopened.trades.list_active()
        assert len(loaded) == 1
        trade = loaded[0]
        assert trade.state is PaperTradeState.OPEN
        assert _leg_facts(trade) == before_legs
        assert trade.entry_risk is not None
        assert trade.entry_risk.model_dump(mode="json") == before_risk
        assert dict(trade.capital_locked_native) == before_locked
        assert _lock_snapshot(reopened) == before_locks
        assert _journal_facts(reopened) == before_journals
        snap = reopened.treasury.snapshot()
        for pool in snap.pools:
            key = f"{pool.venue.value}/{pool.native_currency}"
            assert (pool.available_cash, pool.locked_capital) == pools[key]
    finally:
        reopened.close()


def test_settled_trade_restart_preserves_history_and_realised_pnl(tmp_path: Path) -> None:
    path = tmp_path / "lane5-closed.sqlite"
    ledger = SqlitePaperLedger(path)
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        winning = sorted({leg.outcome for leg in trade.legs})[0]
        expected = independent_realised_pnl_gbp(trade, winning)
        settled = ops.settle(
            trade.trade_id,
            PaperSettlementRequest(
                winning_outcome=winning,
                source="fixture_test",
                source_id="lane5-settle",
                settled_at=OBSERVED,
                provenance=DataProvenance.FIXTURE_DEMO,
            ),
        )
        assert settled.state is PaperTradeState.CLOSED
        assert settled.realised_pnl_gbp == expected
        report = ledger.reconcile()
        assert report.ok
        assert ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP").locked_capital == 0
        closed_id = settled.trade_id
        realised = settled.realised_pnl_gbp
        journals = _journal_facts(ledger)
        assert any(source == "paper_settlement" for source, _sid, _desc in journals)
        duplicate = ops.settle(
            closed_id,
            PaperSettlementRequest(
                winning_outcome=winning,
                source="fixture_test",
                source_id="lane5-settle",
                provenance=DataProvenance.FIXTURE_DEMO,
            ),
        )
        assert duplicate.realised_pnl_gbp == realised
        assert ledger.reconcile().ok
        settle_entries = [
            entry for entry in ledger.journal.list_entries() if entry.source == "paper_settlement"
        ]
        assert len(settle_entries) == 1
    finally:
        repository.close()
        ledger.close()

    reopened = SqlitePaperLedger(path, auto_seed=False)
    try:
        assert reopened.trades.list_active() == []
        closed = reopened.trades.list_closed()
        assert len(closed) == 1
        assert closed[0].trade_id == closed_id
        assert closed[0].state is PaperTradeState.CLOSED
        assert closed[0].realised_pnl_gbp == realised
        assert closed[0].settlement_source_id == "lane5-settle"
        assert _journal_facts(reopened) == journals
        assert reopened.reconcile().ok
        assert reopened.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP").locked_capital == 0
    finally:
        reopened.close()


def test_rejected_and_aborted_decisions_stay_out_of_the_gl(tmp_path: Path) -> None:
    from sports_hedge.application.paper_operations import PaperOperationsError

    path = tmp_path / "lane5-reject.sqlite"
    ledger = SqlitePaperLedger(path)
    _scan, watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    try:
        opportunity_id = next(iter(ops._plans))
        before = _journal_facts(ledger)
        assert ledger.reconcile().ok
        with pytest.raises(
            PaperOperationsError,
            match="stale_before_fill|manual_external_confirmation_required",
        ):
            ops.simulate_fill(opportunity_id, simulate_external=False, now=OBSERVED)
        assert ops.list_active_trades() == []
        assert opportunity_id in ops._entry_rejections
        events = watchlist.activity(opportunity_id=opportunity_id)
        assert any(item.event_type is LifecycleEventType.PAPER_FILL_REJECTED for item in events)
        assert _journal_facts(ledger) == before
        assert ledger.reconcile().ok
        sources = {entry.source for entry in ledger.journal.list_entries()}
        assert "paper_fill_simulator" not in sources
        assert "gpt" not in sources
    finally:
        repository.close()
        ledger.close()

    reopened = SqlitePaperLedger(path, auto_seed=False)
    try:
        sources = {entry.source for entry in reopened.journal.list_entries()}
        assert sources == {"paper_treasury_seed"}
        assert reopened.trades.list_active() == []
        reopened.reconcile()
    finally:
        reopened.close()


def test_operational_watchlist_log_is_not_the_gl(tmp_path: Path) -> None:
    from sports_hedge.arbitrage.watchlist.models import (
        OpportunityLifecycleEvent,
        OpportunityStatus,
    )

    watch_path = tmp_path / "lane5-watch.sqlite"
    ledger_path = tmp_path / "lane5-gl.sqlite"
    repo = SqliteWatchlistRepository(watch_path)
    ledger = SqlitePaperLedger(ledger_path)
    try:
        repo.append_event(
            OpportunityLifecycleEvent(
                opportunity_id="opp-ops-log",
                occurred_at=NOW,
                event_type=LifecycleEventType.PAPER_FILL_REJECTED,
                status=OpportunityStatus.TRIGGERED,
                detail="stale_quote",
            )
        )
        gl_ids = {(entry.source, entry.source_id) for entry in ledger.journal.list_entries()}
        stored = repo.list_events(opportunity_id="opp-ops-log")
        assert stored
        assert (stored[0].event_type.value, stored[0].event_id) not in gl_ids
        assert ledger.reconcile().ok
    finally:
        repo.close()
        ledger.close()

    again = SqliteWatchlistRepository(watch_path)
    try:
        events = again.list_events(opportunity_id="opp-ops-log")
        assert len(events) == 1
        assert events[0].event_type is LifecycleEventType.PAPER_FILL_REJECTED
        assert events[0].detail == "stale_quote"
    finally:
        again.close()


def test_adversarial_insufficient_capital_duplicate_entry_and_partial_fill(
    tmp_path: Path,
) -> None:
    ledger = SqlitePaperLedger(tmp_path / "lane5-adv.sqlite")
    _scan, watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        duplicate = ops.simulate_fill(
            trade.opportunity_id,
            simulate_external=True,
            provenance=DataProvenance.FIXTURE_DEMO,
            now=OBSERVED,
        )
        assert duplicate.trade_id == trade.trade_id
        assert duplicate.trace.detail.startswith("idempotent")
        assert len(ops.list_active_trades()) == 1
        lock_count = ledger._connection.execute(
            "SELECT COUNT(*) AS n FROM paper_treasury_locks WHERE trade_id = ?",
            (trade.trade_id,),
        ).fetchone()["n"]
        assert lock_count == len([leg for leg in trade.legs if leg.filled_stake > 0])
        with pytest.raises(PaperTreasuryError, match="insufficient_available_cash"):
            ledger.treasury.lock_capital(
                [
                    TreasuryLockRequest(
                        venue=VenueName.MATCHBOOK,
                        native_currency="GBP",
                        amount_native=SEED + Decimal("1"),
                        lock_id="lock-too-big",
                        fx_rate_gbp_per_unit=Decimal("1"),
                    )
                ],
                occurred_at=NOW,
            )
        assert ledger.reconcile().ok
        simulator = PaperFillSimulator()
        partial = simulator.simulate(
            [
                PaperOpportunityLeg(
                    outcome="home",
                    venue=VenueName.MATCHBOOK,
                    source_market_id="mb-thin",
                    source_runner_id="home",
                    currency="GBP",
                    requested_stake=Decimal("100"),
                    displayed_odds=Decimal("2.10"),
                    levels=[BookLevel(decimal_odds=Decimal("2.10"), available_stake=Decimal("10"))],
                    quote_age_ms=50,
                    quote_captured_at=OBSERVED,
                )
            ],
            PaperFillConfig(),
            opportunity_id="opp-thin",
            now=OBSERVED,
        )
        assert partial.fills[0].fully_filled is False
        assert partial.fills[0].remaining_stake > 0
        stale = simulator.simulate(
            [
                PaperOpportunityLeg(
                    outcome="home",
                    venue=VenueName.MATCHBOOK,
                    source_market_id="mb-stale",
                    source_runner_id="home",
                    currency="GBP",
                    requested_stake=Decimal("10"),
                    displayed_odds=Decimal("2.10"),
                    levels=[BookLevel(decimal_odds=Decimal("2.10"), available_stake=Decimal("100"))],
                    quote_age_ms=50_000,
                    quote_captured_at=OBSERVED,
                )
            ],
            PaperFillConfig(max_quote_age_ms=1_000, assumed_latency_ms=0),
            opportunity_id="opp-stale",
            now=OBSERVED,
        )
        assert stale.rejection_reasons
        assert ops.list_active_trades()[0].trade_id == trade.trade_id
        assert ledger.reconcile().ok
        assert watchlist.repository.get(trade.opportunity_id) is not None
    finally:
        repository.close()
        ledger.close()


def test_prior_sqlite_schema_migrates_without_error(tmp_path: Path) -> None:
    path = tmp_path / "lane5-legacy.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE paper_trades (
            trade_id TEXT PRIMARY KEY,
            opportunity_id TEXT NOT NULL UNIQUE,
            canonical_event_id TEXT,
            canonical_market_id TEXT,
            settlement_key TEXT,
            market_family TEXT,
            period TEXT,
            competition TEXT,
            home_team TEXT,
            away_team TEXT,
            fixture_label TEXT,
            market_label TEXT,
            state TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            last_updated_at TEXT NOT NULL,
            settled_at TEXT,
            guaranteed_profit_gbp_at_open TEXT,
            realised_pnl_gbp TEXT,
            capital_locked_native_json TEXT NOT NULL,
            capital_locked_gbp TEXT,
            settlement_outcome TEXT,
            settlement_source TEXT,
            settlement_source_id TEXT,
            settlement_detail TEXT,
            provenance TEXT NOT NULL,
            fx_snapshots_json TEXT NOT NULL,
            venue_costs_json TEXT NOT NULL
        );
        CREATE TABLE paper_trade_legs (
            trade_id TEXT NOT NULL,
            venue TEXT NOT NULL,
            outcome TEXT NOT NULL,
            currency TEXT NOT NULL,
            requested_stake TEXT NOT NULL,
            filled_stake TEXT NOT NULL,
            displayed_odds TEXT,
            filled_odds TEXT,
            source_market_id TEXT NOT NULL
        );
        CREATE TABLE paper_trade_events (
            event_id TEXT PRIMARY KEY,
            trade_id TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            event_type TEXT NOT NULL,
            detail TEXT
        );
        CREATE TABLE paper_treasury_sessions (
            session_id TEXT PRIMARY KEY,
            opened_at TEXT NOT NULL,
            closed_at TEXT,
            active INTEGER NOT NULL,
            provenance TEXT NOT NULL,
            reason TEXT NOT NULL,
            seed_gbp TEXT NOT NULL,
            fx_rate_usd_gbp TEXT NOT NULL,
            fx_source TEXT NOT NULL,
            fx_as_of TEXT NOT NULL,
            include_kalshi INTEGER NOT NULL
        );
        CREATE TABLE paper_treasury_pools (
            pool_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            venue TEXT NOT NULL,
            native_currency TEXT NOT NULL,
            seed_native TEXT NOT NULL,
            available_cash TEXT NOT NULL,
            locked_capital TEXT NOT NULL,
            realised_pnl_native TEXT NOT NULL,
            cumulative_fees_native TEXT NOT NULL
        );
        CREATE TABLE paper_treasury_locks (
            lock_id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            pool_id TEXT NOT NULL,
            trade_id TEXT,
            opportunity_id TEXT,
            venue TEXT NOT NULL,
            native_currency TEXT NOT NULL,
            locked_native TEXT NOT NULL,
            released_native TEXT NOT NULL,
            status TEXT NOT NULL
        );
        """
    )
    opened = "2026-09-01T12:00:00+00:00"
    connection.execute(
        """
        INSERT INTO paper_trades (
            trade_id, opportunity_id, state, opened_at, last_updated_at,
            capital_locked_native_json, provenance, fx_snapshots_json, venue_costs_json
        ) VALUES (?, ?, 'OPEN', ?, ?, '{"GBP": "40"}', 'live_paper', '[]', '[]')
        """,
        ("ptrade-legacy", "opp-legacy", opened, opened),
    )
    connection.execute(
        """
        INSERT INTO paper_trade_legs (
            trade_id, venue, outcome, currency, requested_stake, filled_stake, source_market_id
        ) VALUES (?, 'matchbook', 'home', 'GBP', '40', '40', 'mb-1')
        """,
        ("ptrade-legacy",),
    )
    connection.execute(
        """
        INSERT INTO paper_treasury_sessions (
            session_id, opened_at, closed_at, active, provenance, reason, seed_gbp,
            fx_rate_usd_gbp, fx_source, fx_as_of, include_kalshi
        ) VALUES ('sess-legacy', ?, NULL, 1, 'live_paper', 'legacy', '1000', '0.80',
                  'paper_demo_fx_snapshot', ?, 1)
        """,
        (opened, opened),
    )
    connection.execute(
        """
        INSERT INTO paper_treasury_pools (
            pool_id, session_id, venue, native_currency, seed_native, available_cash,
            locked_capital, realised_pnl_native, cumulative_fees_native
        ) VALUES ('pool-mb', 'sess-legacy', 'matchbook', 'GBP', '1000', '960', '40', '0', '0')
        """
    )
    connection.execute(
        """
        INSERT INTO paper_treasury_locks (
            lock_id, session_id, pool_id, trade_id, opportunity_id, venue,
            native_currency, locked_native, released_native, status
        ) VALUES ('lock-legacy', 'sess-legacy', 'pool-mb', 'ptrade-legacy', 'opp-legacy',
                  'matchbook', 'GBP', '40', '0', 'open')
        """
    )
    connection.commit()
    connection.close()

    ledger = SqlitePaperLedger(path, auto_seed=False)
    try:
        trades = ledger.trades.list_active()
        assert len(trades) == 1
        assert trades[0].trade_id == "ptrade-legacy"
        assert trades[0].legs[0].fill_kind.value == "INTERNAL_SIMULATED"
        assert ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP").locked_capital == Decimal(
            "40"
        )
        cols = {row[1] for row in ledger._connection.execute("PRAGMA table_info(paper_trades)")}
        assert "entry_risk_json" in cols
        journal_tables = {
            row[0]
            for row in ledger._connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert "paper_journal_entries" in journal_tables
        with pytest.raises(LedgerReconciliationError):
            ledger.reconcile()
    finally:
        ledger.close()


def test_concurrent_dashboard_reads_keep_open_trade_and_reconcile(tmp_path: Path) -> None:
    path = tmp_path / "lane5-concurrent.sqlite"
    ledger = SqlitePaperLedger(path)
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade_id = ops.list_active_trades()[0].trade_id
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(25):
                    summary = ops.book_summary()
                    active = ops.list_active_trades()
                    snap = ledger.treasury.snapshot()
                    assert summary.open_count == 1
                    assert active[0].trade_id == trade_id
                    assert snap.pool(VenueName.MATCHBOOK, "GBP").locked_capital > 0
            except BaseException as exc:  # noqa: BLE001
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=8) as pool:
            futures = [pool.submit(worker) for _ in range(8)]
            for future in as_completed(futures):
                future.result()
        assert errors == []
        assert ledger.reconcile().ok
        assert ops.list_active_trades()[0].trade_id == trade_id
    finally:
        repository.close()
        ledger.close()


def test_hanging_provider_does_not_post_phantom_cash(monkeypatch: pytest.MonkeyPatch) -> None:
    get_live_refresh_coordinator().reset()
    settings = get_settings()
    monkeypatch.setattr(settings, "paper_scan_venue_timeout_seconds", 0.4)
    monkeypatch.setattr(settings, "paper_scan_provider_timeout_seconds", 0.4)
    monkeypatch.setattr(settings, "paper_scan_cycle_timeout_seconds", 3)
    ledger = get_paper_ledger()
    before_journals = _journal_facts(ledger)
    before_pools = {
        f"{pool.venue.value}/{pool.native_currency}": (pool.available_cash, pool.locked_capital)
        for pool in ledger.treasury.snapshot().pools
    }
    polymarket = FakePolymarket()

    async def hang_events(self, **filters):
        del self, filters
        await asyncio.sleep(30)
        return {"events": []}

    async def pm_events(self, **filters):
        del self
        return await polymarket.list_events(**filters)

    async def pm_markets(self, event_id, **filters):
        del self
        return await polymarket.list_markets(event_id, **filters)

    async def pm_book(self, event_id, market_id, outcome_id=None, **filters):
        del self
        return await polymarket.get_order_book(event_id, market_id, outcome_id, **filters)

    async def empty_kalshi(self, **filters):
        del self, filters
        return {"events": []}

    monkeypatch.setattr(MatchbookClient, "list_events", hang_events)
    monkeypatch.setattr(PolymarketClient, "list_events", pm_events)
    monkeypatch.setattr(PolymarketClient, "list_markets", pm_markets)
    monkeypatch.setattr(PolymarketClient, "get_order_book", pm_book)
    monkeypatch.setattr(KalshiClient, "list_events", empty_kalshi)

    client = TestClient(app)
    response = client.post("/paper/collect", json={"maximum_execution_risk": 100})
    assert response.status_code == 200
    assert response.json()["venue_health"]["matchbook"] == "timeout"
    assert _journal_facts(ledger) == before_journals
    after_pools = {
        f"{pool.venue.value}/{pool.native_currency}": (pool.available_cash, pool.locked_capital)
        for pool in ledger.treasury.snapshot().pools
    }
    assert after_pools == before_pools
    ledger.reconcile()
    get_live_refresh_coordinator().reset()


def test_cash_in_transit_is_deferred_not_an_accounting_error(tmp_path: Path) -> None:
    """#116 will add treasury transfers. Transit must not fail closed today."""

    ledger = SqlitePaperLedger(
        tmp_path / "lane5-transit.sqlite",
        seed_gbp=SEED,
        usd_gbp_per_unit=FX,
        fx_source="paper_demo_fx_snapshot",
    )
    try:
        snap = ledger.treasury.snapshot()
        assert snap.session is not None
        pool = snap.pool(VenueName.MATCHBOOK, "GBP")
        amount = Decimal("5")
        dims = PostingDimensions(
            attribution=AttributionScope.SHARED_UNALLOCATED,
            capital_source=CapitalSource.SHARED_UNALLOCATED,
            venue=VenueName.MATCHBOOK,
            currency="GBP",
        )
        posted = ledger.journal.append(
            PaperJournalEntry(
                source="paper_treasury_transfer",
                source_id="issue-116-deferred-transit",
                occurred_at=NOW,
                description="Forward-compatible cash-in-transit; #116 owns transfer accounting",
                opportunity_id="issue-116",
                postings=[
                    PaperJournalPosting(
                        account_code=cash_account(VenueName.MATCHBOOK, "GBP", CashState.TRANSIT),
                        side=PostingSide.DEBIT,
                        amount_native=amount,
                        amount_gbp=amount,
                        fx_rate_gbp_per_unit=Decimal("1"),
                        dimensions=dims,
                    ),
                    PaperJournalPosting(
                        account_code=cash_account(VenueName.MATCHBOOK, "GBP", CashState.AVAILABLE),
                        side=PostingSide.CREDIT,
                        amount_native=amount,
                        amount_gbp=amount,
                        fx_rate_gbp_per_unit=Decimal("1"),
                        dimensions=dims,
                    ),
                ],
            )
        )
        with ledger.transaction():
            ledger._connection.execute(
                "UPDATE paper_treasury_pools SET available_cash = ? WHERE pool_id = ?",
                (str(pool.available_cash - amount), pool.pool_id),
            )
            ledger.treasury._insert_event(
                PaperTreasuryEvent(
                    event_id="evt-deferred-transit",
                    session_id=snap.session.session_id,
                    pool_id=pool.pool_id,
                    venue=VenueName.MATCHBOOK,
                    native_currency="GBP",
                    event_type=PaperTreasuryEventType.CORRECTION,
                    native_amount=amount,
                    occurred_at=NOW,
                    source="paper_treasury_transfer",
                    source_id="issue-116-deferred-transit",
                    reason="unsupported cash-in-transit pending issue 116",
                    journal_id=posted.journal_id,
                )
            )
        report = ledger.reconcile()
        assert report.ok
        assert report.native_transit["matchbook/GBP"] == "5"
        assert any(item.startswith("unsupported_transit:matchbook/GBP=") for item in report.deferred)
        assert not any("unexpected_transit" in item for item in report.mismatches)
    finally:
        ledger.close()

