from __future__ import annotations

import inspect
import sqlite3
import time
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from sports_hedge.api import paper as paper_api
from sports_hedge.api.main import app
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.application.collector import CollectionReport
from sports_hedge.application.live_refresh import get_live_refresh_coordinator
from sports_hedge.config import get_settings
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
            self.persist_outcome_seen = False

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

        def record_persist_outcome(self, **kwargs) -> None:
            del kwargs
            assert self.inside_scan_envelope is False
            self.persist_outcome_seen = True

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
    assert response.json()["scan_diagnostics"]["collection_kind"] == "manual_diagnostic"
    assert response.json()["scan_diagnostics"]["scheduled_fast_full_unchanged"] is True
    assert response.json()["fixture_markets"] == {}
    assert coordinator.persist_seen is True
    assert coordinator.persist_outcome_seen is True
    collect_src = inspect.getsource(paper_api.collect_read_only_market_data)
    assert "_collect_report(" in collect_src
    assert "_execute_collection(" not in collect_src
    assert "background_tasks.add_task" in collect_src
    assert "paper_scan_manual_diagnostic_timeout_seconds" in collect_src
    assert "cycle_timeout_seconds=diagnostic_timeout" in collect_src
    assert collect_src.index("background_tasks.add_task") > collect_src.index(
        "run_explicit_collect"
    )
    assert collect_src.index("return report") > collect_src.index("background_tasks.add_task")
    assert "persist_scheduled_collection_report(" not in collect_src


def test_explicit_collect_slow_failing_persist_returns_partial_200_not_504(
    monkeypatch,
) -> None:
    """Production path: persist after a completed/partial scan cannot become HTTP 504."""

    from sports_hedge.application import live_refresh as live_refresh_mod

    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    monkeypatch.setattr(live_refresh_mod, "SCAN_CYCLE_RETURN_GRACE_SECONDS", 0.05)
    settings = get_settings()
    monkeypatch.setattr(settings, "paper_scan_cycle_timeout_seconds", 0.1)
    envelope = (
        settings.paper_scan_cycle_timeout_seconds
        + live_refresh_mod.SCAN_CYCLE_RETURN_GRACE_SECONDS
    )
    assert envelope < 0.25

    now = datetime(2026, 9, 15, 19, 5, tzinfo=UTC)
    report = CollectionReport(
        started_at=now,
        completed_at=now,
        operator_summary="partial leftover after soft deadline",
        scan_diagnostics={"soft_deadline_reached": True, "partial": True},
    )
    persist_state: dict[str, object] = {}

    async def fake_collect_report(*args, **kwargs):
        del args, kwargs
        return report

    def slow_then_fail_persist(*args, **kwargs) -> None:
        del args, kwargs
        persist_state["lock_locked"] = coordinator._lock.locked()
        time.sleep(0.25)
        raise RuntimeError("audit_write_failed")

    monkeypatch.setattr(paper_api, "_collect_report", fake_collect_report)
    monkeypatch.setattr(paper_api, "_persist_collection_report", slow_then_fail_persist)

    app.dependency_overrides[paper_api.get_paper_scan_service] = lambda: object()
    app.dependency_overrides[paper_api.get_paper_audit_repository] = lambda: object()
    app.dependency_overrides[get_watchlist_service] = lambda: object()
    try:
        response = TestClient(app).post("/paper/collect", json={})
        persist_ok = coordinator.status.universe.persist_ok
        persist_error = coordinator.status.universe.last_persist_error
        last_error = coordinator.status.last_error
    finally:
        app.dependency_overrides.clear()
        coordinator.reset()

    assert persist_state["lock_locked"] is False
    assert response.status_code == 200, response.text
    assert response.status_code != 504
    assert "scan_cycle_timeout" not in response.text
    body = response.json()
    assert body["operator_summary"] == "partial leftover after soft deadline"
    assert body["scan_diagnostics"]["soft_deadline_reached"] is True
    # Persist outcome is live-refresh honesty, not a delayed collect body.
    assert body["scan_diagnostics"].get("persist_ok") is None
    assert "persist_error" not in body["scan_diagnostics"]
    assert persist_ok is False
    assert persist_error == "audit_write_failed"
    assert last_error is None


class _AsgiBodySentProbe:
    """Record when the ASGI HTTP body is fully sent, before Starlette background tasks."""

    def __init__(self, app) -> None:
        self.app = app
        self.request_started_at: float | None = None
        self.body_sent_at: float | None = None
        self.status_code: int | None = None

    async def __call__(self, scope, receive, send):
        if scope.get("type") == "http":
            self.request_started_at = time.monotonic()

        async def tracked_send(message):
            message_type = message.get("type")
            if message_type == "http.response.start":
                self.status_code = message.get("status")
            if message_type == "http.response.body" and not message.get("more_body", False):
                self.body_sent_at = time.monotonic()
            await send(message)

        await self.app(scope, receive, tracked_send)


def test_explicit_collect_slow_persist_cannot_hold_response_past_scan_budget(
    monkeypatch,
) -> None:
    """Production contract: browser-visible body completes within scan budget.

    Scale the clock down. A persist phase longer than the scan-response budget
    must not delay ASGI body completion. TestClient itself waits for background
    tasks; this test therefore timestamps `http.response.body`, not client
    return. Do not increase PAPER_COLLECTION_TIMEOUT_MS to pass this.
    """

    from sports_hedge.application import live_refresh as live_refresh_mod

    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    monkeypatch.setattr(live_refresh_mod, "SCAN_CYCLE_RETURN_GRACE_SECONDS", 0.05)
    settings = get_settings()
    monkeypatch.setattr(settings, "paper_scan_cycle_timeout_seconds", 0.1)
    scan_budget = (
        settings.paper_scan_cycle_timeout_seconds
        + live_refresh_mod.SCAN_CYCLE_RETURN_GRACE_SECONDS
    )
    persist_delay = 0.45
    assert persist_delay > scan_budget

    now = datetime(2026, 9, 15, 19, 10, tzinfo=UTC)
    report = CollectionReport(
        started_at=now,
        completed_at=now,
        operator_summary="partial leftover after soft deadline",
        scan_diagnostics={"soft_deadline_reached": True, "partial": True},
    )
    persist_state: dict[str, float] = {}

    async def fake_collect_report(*args, **kwargs):
        del args, kwargs
        return report

    def slow_persist(*args, **kwargs) -> None:
        del args, kwargs
        persist_state["start"] = time.monotonic()
        time.sleep(persist_delay)
        persist_state["end"] = time.monotonic()

    monkeypatch.setattr(paper_api, "_collect_report", fake_collect_report)
    monkeypatch.setattr(paper_api, "_persist_collection_report", slow_persist)

    probe = _AsgiBodySentProbe(app)
    app.dependency_overrides[paper_api.get_paper_scan_service] = lambda: object()
    app.dependency_overrides[paper_api.get_paper_audit_repository] = lambda: object()
    app.dependency_overrides[get_watchlist_service] = lambda: object()
    try:
        response = TestClient(probe).post("/paper/collect", json={})
        persist_ok = coordinator.status.universe.persist_ok
    finally:
        app.dependency_overrides.clear()
        coordinator.reset()

    assert response.status_code == 200, response.text
    assert probe.status_code == 200
    assert probe.request_started_at is not None
    assert probe.body_sent_at is not None
    assert "start" in persist_state
    response_elapsed = probe.body_sent_at - probe.request_started_at
    assert response_elapsed < scan_budget
    assert response_elapsed < persist_delay
    assert probe.body_sent_at <= persist_state["start"]
    assert persist_state["end"] - persist_state["start"] >= persist_delay * 0.9
    body = response.json()
    assert body["operator_summary"] == "partial leftover after soft deadline"
    assert body["scan_diagnostics"]["soft_deadline_reached"] is True
    assert body["scan_diagnostics"].get("persist_ok") is None
    assert persist_ok is True
    frontend_api = Path(__file__).resolve().parents[2] / "frontend" / "lib" / "api.ts"
    assert "PAPER_COLLECTION_TIMEOUT_MS = 60_000" in frontend_api.read_text(
        encoding="utf-8"
    )
