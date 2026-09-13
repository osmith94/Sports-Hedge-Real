from __future__ import annotations

import asyncio
import sqlite3
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.application.live_refresh import get_live_refresh_coordinator
from sports_hedge.config import get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.trades import PaperTradeState
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient
from test_read_only_collector import FakePolymarket


def _legacy_paper_schema(connection: sqlite3.Connection) -> None:
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
            cumulative_fees_native TEXT NOT NULL,
            fx_rate_gbp_per_unit TEXT,
            fx_source TEXT,
            fx_as_of TEXT
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


def test_pre_risk_snapshot_sqlite_loads_locked_trade(tmp_path: Path) -> None:
    path = tmp_path / "legacy-paper.sqlite"
    connection = sqlite3.connect(path)
    _legacy_paper_schema(connection)
    opened = datetime(2026, 9, 1, 12, 0, tzinfo=UTC).isoformat()
    connection.execute(
        """
        INSERT INTO paper_trades (
            trade_id, opportunity_id, home_team, away_team, state, opened_at,
            last_updated_at, capital_locked_native_json, capital_locked_gbp,
            provenance, fx_snapshots_json, venue_costs_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ptrade-legacy",
            "opp-legacy",
            "Arsenal",
            "Fulham",
            "OPEN",
            opened,
            opened,
            '{"GBP": "40", "USD": "50"}',
            "80",
            "live_paper",
            '[{"currency":"USD","gbp_per_unit":"0.80","source":"paper_demo_fx_snapshot","captured_at":"2026-09-01T12:00:00"}]',
            "[]",
        ),
    )
    connection.execute(
        """
        INSERT INTO paper_trade_legs (
            trade_id, venue, outcome, currency, requested_stake, filled_stake,
            displayed_odds, filled_odds, source_market_id
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("ptrade-legacy", "matchbook", "home", "GBP", "40", "40", "1.0", "2.10", "mb-1"),
    )
    connection.execute(
        """
        INSERT INTO paper_treasury_sessions (
            session_id, opened_at, closed_at, active, provenance, reason, seed_gbp,
            fx_rate_usd_gbp, fx_source, fx_as_of, include_kalshi
        ) VALUES (?, ?, NULL, 1, 'live_paper', 'legacy', '1000', '0.80',
                  'paper_demo_fx_snapshot', ?, 1)
        """,
        ("sess-legacy", opened, opened),
    )
    connection.execute(
        """
        INSERT INTO paper_treasury_pools (
            pool_id, session_id, venue, native_currency, seed_native, available_cash,
            locked_capital, realised_pnl_native, cumulative_fees_native
        ) VALUES (?, ?, ?, ?, ?, ?, ?, '0', '0')
        """,
        ("pool-mb", "sess-legacy", "matchbook", "GBP", "1000", "960", "40"),
    )
    connection.execute(
        """
        INSERT INTO paper_treasury_locks (
            lock_id, session_id, pool_id, trade_id, opportunity_id, venue,
            native_currency, locked_native, released_native, status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, '0', 'open')
        """,
        (
            "lock-legacy",
            "sess-legacy",
            "pool-mb",
            "ptrade-legacy",
            "opp-legacy",
            "matchbook",
            "GBP",
            "40",
        ),
    )
    connection.commit()
    connection.close()

    ledger = SqlitePaperLedger(path, auto_seed=False)
    try:
        trades = ledger.trades.list_active()
        assert len(trades) == 1
        trade = trades[0]
        assert trade.trade_id == "ptrade-legacy"
        assert trade.state is PaperTradeState.OPEN
        assert trade.capital_locked_native["GBP"] == Decimal("40")
        assert trade.entry_risk is None
        assert trade.legs
        assert trade.legs[0].displayed_odds is None
        assert trade.legs[0].filled_odds is not None
        snapshot = ledger.treasury.snapshot()
        pool = snapshot.pool(VenueName.MATCHBOOK, "GBP")
        assert pool.locked_capital == Decimal("40")
    finally:
        ledger.close()


def test_collect_api_returns_degraded_matchbook_and_pm_fixtures(monkeypatch: pytest.MonkeyPatch) -> None:
    get_live_refresh_coordinator().reset()
    settings = get_settings()
    monkeypatch.setattr(settings, "paper_scan_venue_timeout_seconds", 0.4)
    monkeypatch.setattr(settings, "paper_scan_provider_timeout_seconds", 0.4)
    monkeypatch.setattr(settings, "paper_scan_cycle_timeout_seconds", 3)
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
    started = time.monotonic()
    response = client.post("/paper/collect", json={"maximum_execution_risk": 100})
    elapsed = time.monotonic() - started
    assert elapsed < 3.0
    assert response.status_code == 200
    body = response.json()
    assert body["venue_health"]["matchbook"] == "timeout"
    assert body["venue_health"]["polymarket"] == "ok"
    assert body["raw_matchbook_events"] == 0
    assert body["raw_polymarket_events"] >= 1
    assert body["discovered_fixtures"]
    assert any(item["polymarket_matched"] for item in body["discovered_fixtures"])
    assert not any(item.get("matchbook_matched") for item in body["discovered_fixtures"])

    status = client.get("/paper/live-refresh")
    assert status.status_code == 200
    payload = status.json()
    assert payload["last_completed_at"]
    assert payload["cycle_in_progress"] is False
    assert payload["venue_health"]["matchbook"] == "timeout"
    assert payload["discovered_fixtures"]
    get_live_refresh_coordinator().reset()
