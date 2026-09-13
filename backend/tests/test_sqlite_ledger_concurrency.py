from __future__ import annotations

import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.paper.trades import PaperTradeState

from test_paper_trade_lifecycle import _ops


def test_concurrent_summary_and_list_active_reads_do_not_raise(tmp_path: Path) -> None:
    path = tmp_path / "paper-concurrent.sqlite"
    ledger = SqlitePaperLedger(path)
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        baseline = ops.list_active_trades()
        assert len(baseline) == 1
        assert baseline[0].legs
        assert baseline[0].audit
        trade_id = baseline[0].trade_id
        locked = dict(baseline[0].capital_locked_native)
        errors: list[BaseException] = []

        def worker() -> None:
            try:
                for _ in range(40):
                    summary = ops.book_summary()
                    active = ops.list_active_trades()
                    snapshot = ledger.treasury.snapshot()
                    assert summary.open_count >= 1
                    assert len(active) == 1
                    assert active[0].trade_id == trade_id
                    assert active[0].legs
                    assert snapshot.session is not None
            except BaseException as exc:  # noqa: BLE001 — capture InterfaceError for the assertion
                errors.append(exc)

        with ThreadPoolExecutor(max_workers=12) as pool:
            futures = [pool.submit(worker) for _ in range(12)]
            for future in as_completed(futures):
                future.result()

        assert errors == []
        reloaded = ops.list_active_trades()
        assert len(reloaded) == 1
        assert reloaded[0].trade_id == trade_id
        assert reloaded[0].state is PaperTradeState.OPEN
        assert reloaded[0].capital_locked_native == locked
        assert len(reloaded[0].legs) == len(baseline[0].legs)
    finally:
        repository.close()
        ledger.close()


def test_pre_risk_snapshot_sqlite_loads_without_reset(tmp_path: Path) -> None:
    path = tmp_path / "legacy-risk.sqlite"
    connection = sqlite3.connect(path)
    connection.executescript(
        """
        CREATE TABLE paper_trades (
            trade_id TEXT PRIMARY KEY,
            opportunity_id TEXT NOT NULL UNIQUE,
            canonical_event_id TEXT,
            canonical_market_id TEXT,
            settlement_key TEXT,
            solver_model TEXT,
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
            source_market_id TEXT NOT NULL,
            fill_id TEXT,
            fill_kind TEXT NOT NULL,
            capital_source TEXT NOT NULL,
            execution_mode TEXT NOT NULL
        );
        CREATE TABLE paper_trade_events (
            event_id TEXT PRIMARY KEY,
            trade_id TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            event_type TEXT NOT NULL,
            detail TEXT
        );
        """
    )
    connection.execute(
        """
        INSERT INTO paper_trades (
            trade_id, opportunity_id, state, opened_at, last_updated_at,
            capital_locked_native_json, provenance, fx_snapshots_json, venue_costs_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ptrade-legacy",
            "opp-legacy",
            "OPEN",
            "2026-09-13T12:00:00+00:00",
            "2026-09-13T12:00:00+00:00",
            '{"GBP": "100"}',
            "live_paper",
            "[]",
            "[]",
        ),
    )
    connection.execute(
        """
        INSERT INTO paper_trade_legs (
            trade_id, venue, outcome, currency, requested_stake, filled_stake,
            source_market_id, fill_kind, capital_source, execution_mode
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ptrade-legacy",
            "matchbook",
            "home",
            "GBP",
            "100",
            "100",
            "mb-1",
            "PAPER_SIMULATED_EXTERNAL",
            "AUTO_POOL",
            "INTERNAL",
        ),
    )
    connection.execute(
        """
        INSERT INTO paper_trade_events (event_id, trade_id, occurred_at, event_type, detail)
        VALUES (?, ?, ?, ?, ?)
        """,
        ("evt-legacy", "ptrade-legacy", "2026-09-13T12:00:00+00:00", "trade_opened", "legacy"),
    )
    connection.commit()
    connection.close()

    ledger = SqlitePaperLedger(path, auto_seed=False)
    try:
        active = ledger.trades.list_active()
        assert len(active) == 1
        trade = active[0]
        assert trade.trade_id == "ptrade-legacy"
        assert trade.entry_risk is None
        assert trade.close_risks == []
        assert len(trade.legs) == 1
        assert trade.legs[0].source_market_id == "mb-1"
        assert trade.audit[0].detail == "legacy"
    finally:
        ledger.close()
