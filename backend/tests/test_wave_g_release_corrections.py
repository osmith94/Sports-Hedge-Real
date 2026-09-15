from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from sports_hedge.api import paper as paper_api
from sports_hedge.api.main import app
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.application.collector import CollectionReport
from sports_hedge.domain.models import VenueName
from sports_hedge.persistence.liquidity import SqlitePaperLiquidityRepository


def test_legacy_null_liquidity_balances_repair_to_zero_across_restart(tmp_path: Path) -> None:
    """Owner SQLite compatibility: NULL never becomes configured spendable bankroll."""

    database = tmp_path / "legacy-null-liquidity.sqlite"
    connection = sqlite3.connect(database)
    connection.executescript(
        """
        CREATE TABLE paper_liquidity_pools (
            venue TEXT PRIMARY KEY,
            native_currency TEXT,
            available TEXT,
            locked TEXT,
            transit TEXT,
            updated_at TEXT
        );
        """
    )
    observed = datetime(2026, 9, 15, 18, 45, tzinfo=UTC).isoformat()
    connection.executemany(
        """
        INSERT INTO paper_liquidity_pools (
            venue, native_currency, available, locked, transit, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        [
            ("matchbook", "GBP", None, None, None, observed),
            ("polymarket", "USD", "123", "4", "5", observed),
            ("kalshi", "USD", "77", "0", "0", observed),
            ("smarkets", "GBP", "0", "0", "0", observed),
        ],
    )
    connection.commit()
    connection.close()

    first = SqlitePaperLiquidityRepository(
        database,
        matchbook_gbp=Decimal("99999"),
        polymarket_usd=Decimal("99999"),
        kalshi_usd=Decimal("99999"),
    )
    try:
        snapshot = first.get()
        matchbook = snapshot.pool(VenueName.MATCHBOOK)
        assert matchbook.available == Decimal("0")
        assert matchbook.locked == Decimal("0")
        assert matchbook.transit == Decimal("0")
        assert snapshot.pool(VenueName.POLYMARKET).available == Decimal("123")
    finally:
        first.close()

    # The reconciliation is durable; reopening must neither crash nor seed defaults.
    second = SqlitePaperLiquidityRepository(
        database,
        matchbook_gbp=Decimal("88888"),
        polymarket_usd=Decimal("88888"),
        kalshi_usd=Decimal("88888"),
    )
    try:
        matchbook = second.get().pool(VenueName.MATCHBOOK)
        assert matchbook.available == Decimal("0")
        assert matchbook.locked == Decimal("0")
        assert matchbook.transit == Decimal("0")
    finally:
        second.close()

    connection = sqlite3.connect(database)
    repaired = connection.execute(
        "SELECT available, locked, transit FROM paper_liquidity_pools WHERE venue='matchbook'"
    ).fetchone()
    connection.close()
    assert repaired == ("0", "0", "0")


def test_explicit_collect_persists_after_bounded_scan_envelope(monkeypatch) -> None:
    """Manual diagnostic persistence must not be part of scan-cycle timeout accounting."""

    now = datetime(2026, 9, 15, 19, 0, tzinfo=UTC)
    report = CollectionReport(started_at=now, completed_at=now)

    class FakeCoordinator:
        def __init__(self) -> None:
            self.inside_scan_envelope = False
            self.persist_seen = False

        def remember_request(self, payload) -> None:
            del payload

        def running_cycle_venues(self):
            return (VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI)

        async def run_explicit_collect(self, runner):
            self.inside_scan_envelope = True
            try:
                return await runner()
            finally:
                self.inside_scan_envelope = False

    coordinator = FakeCoordinator()

    async def fake_collect_report(*args, **kwargs):
        del args, kwargs
        return report

    def fake_persist_collection_report(*args, **kwargs) -> None:
        del args, kwargs
        assert coordinator.inside_scan_envelope is False
        coordinator.persist_seen = True

    monkeypatch.setattr(paper_api, "get_live_refresh_coordinator", lambda: coordinator)
    monkeypatch.setattr(paper_api, "_collect_report", fake_collect_report)
    monkeypatch.setattr(paper_api, "_persist_collection_report", fake_persist_collection_report)

    app.dependency_overrides[paper_api.get_paper_scan_service] = lambda: object()
    app.dependency_overrides[paper_api.get_paper_audit_repository] = lambda: object()
    app.dependency_overrides[get_watchlist_service] = lambda: object()
    try:
        response = TestClient(app).post("/paper/collect", json={})
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200, response.text
    assert coordinator.persist_seen is True
