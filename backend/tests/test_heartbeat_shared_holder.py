"""Heartbeat reads must not install FastAPI Depends markers on the shared holder.

PAPER / read-only. In-memory ledger and watchlist. Not live venue quotes.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.params import Depends as DependsMarker

from sports_hedge.api import paper as paper_api
from sports_hedge.api.priority_alerts import get_priority_alert_service
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import get_settings
from sports_hedge.paper.active_trade_journal import (
    ActiveTradeEventType,
    ActiveTradeReasonCode,
)
from sports_hedge.paper.trades import PaperTrade, PaperTradeState
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


def _holder() -> tuple[PaperOperationsService, SqlitePaperLedger, SqliteWatchlistRepository]:
    repository = SqliteWatchlistRepository(":memory:")
    ledger = SqlitePaperLedger(":memory:")
    watchlist = WatchlistService(repository)
    alerts = PriorityAlertService(settings=get_settings())
    holder = PaperOperationsService(
        watchlist=watchlist,
        alerts=alerts,
        ledger=ledger,
        catalogue=None,
        settings=get_settings(),
    )
    return holder, ledger, repository


def test_direct_depends_call_cannot_replace_the_cached_watchlist(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder, ledger, repository = _holder()
    monkeypatch.setattr(paper_api, "get_paper_journal_holder", lambda: holder)
    poison = get_watchlist_service()
    assert isinstance(poison.repository, DependsMarker)
    assert not hasattr(poison.repository, "list_opportunities")
    assert not hasattr(poison.repository, "get")
    try:
        with pytest.raises(TypeError, match="unresolved FastAPI Depends"):
            paper_api.get_paper_operations_service(poison, get_priority_alert_service())
        assert holder.watchlist.repository is repository
        assert holder.watchlist.repository.list_opportunities() == []
        assert holder.watchlist.repository.get("missing") is None
    finally:
        ledger.close()
        repository.close()


def test_repeated_heartbeat_keeps_the_same_real_repository(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    holder, ledger, repository = _holder()
    alerts = holder.alerts
    trade = PaperTrade(
        trade_id="t-heartbeat",
        opportunity_id="opp-heartbeat",
        canonical_event_id="evt-heartbeat",
        state=PaperTradeState.OPEN,
        opened_at=NOW,
        last_updated_at=NOW,
        capital_locked_gbp=Decimal("12.50"),
    )
    ledger.trades.save(trade)
    assert holder.record_active_lifecycle_event(
        trade,
        event_type=ActiveTradeEventType.PROMOTED_TO_ACTIVE,
        reason_code=ActiveTradeReasonCode.PROMOTED,
        operator_copy="promoted for heartbeat",
        occurred_at=NOW,
        dedupe_key="promoted:t-heartbeat",
        raise_on_error=True,
    )
    monkeypatch.setattr(paper_api, "get_paper_journal_holder", lambda: holder)
    holder.settings = object()  # type: ignore[assignment]
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW)
    try:
        heartbeats = [coordinator.public_heartbeat() for _ in range(5)]
        assert holder.watchlist.repository is repository
        assert not isinstance(holder.watchlist.repository, DependsMarker)
        assert not isinstance(holder.watchlist, DependsMarker)
        assert holder.alerts is alerts
        assert holder.ledger is ledger
        assert holder.catalogue is None
        assert holder.settings is get_settings()
        assert holder.watchlist.repository.list_opportunities() == []
        assert holder.watchlist.repository.get("missing") is None
        for heartbeat in heartbeats:
            assert heartbeat.system_load.active_trade.capital_locked_gbp == pytest.approx(12.5)
            assert heartbeat.active_trade_timeline
            assert heartbeat.active_trade_timeline[0].operator_copy == "promoted for heartbeat"
            assert heartbeat.active_trade_timeline[0].trade_id == "t-heartbeat"
    finally:
        ledger.close()
        repository.close()
