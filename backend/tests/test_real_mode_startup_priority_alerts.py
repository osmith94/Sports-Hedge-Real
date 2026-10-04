"""REAL startup must not construct the paper-only priority alert service.

Data class: in-process lifespan, local sqlite, and fixture scan decisions.
Not live venue quotes. No order submit, cancel, or edit. Execution stays off
in the REAL startup case. PAPER alert ingestion remains the paper service.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api import main as main_api
from sports_hedge.api import paper as paper_api
from sports_hedge.api.paper import (
    get_paper_journal_holder,
    get_paper_ledger,
    get_paper_operations_service,
    operations_priority_alerts,
)
from sports_hedge.api.priority_alerts import get_priority_alert_service
from sports_hedge.api.watchlist import get_watchlist_repository
from sports_hedge.application.execution_reprice import _paper_operations
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.arbitrage.depth import DepthQuoteCandidate, DepthScanResult
from sports_hedge.arbitrage.models import ArbitrageSolution
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.persistence.approved_market_catalogue import (
    get_approved_market_catalogue_store,
)
from sports_hedge.accounting.paper_journal import DataProvenance


class _Coordinator:
    def bind_universe_checkpoint_store(self, store: object) -> None:
        return None

    def configure_from_settings(self) -> None:
        return None

    async def start_server_loop(self, tick: object) -> None:
        return None

    async def stop_server_loop(self) -> None:
        return None

    def health_live_refresh_fields(self) -> dict[str, object]:
        return {"server_loop_enabled": False}

    def fixture_detail(self, canonical_event_id: str) -> None:
        return None


class _Schedule:
    async def start(self) -> None:
        return None

    async def stop(self) -> None:
        return None


def _memory_stores(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PAPER_LEDGER_DB_PATH", ":memory:")
    monkeypatch.setenv("WATCHLIST_DB_PATH", ":memory:")
    monkeypatch.setenv("PAPER_SETTINGS_DB_PATH", ":memory:")
    monkeypatch.setenv("SPORTS_HEDGE_EXECUTION_ENABLED", "false")


def _clear_process_caches() -> None:
    get_settings.cache_clear()
    get_priority_alert_service.cache_clear()
    get_paper_journal_holder.cache_clear()
    get_paper_ledger.cache_clear()
    get_watchlist_repository.cache_clear()
    get_approved_market_catalogue_store.cache_clear()


def _close_cached_stores() -> None:
    if get_paper_ledger.cache_info().currsize:
        get_paper_ledger().close()
    if get_watchlist_repository.cache_info().currsize:
        get_watchlist_repository().close()
    if get_approved_market_catalogue_store.cache_info().currsize:
        get_approved_market_catalogue_store().close()


def _arb_decision() -> PaperScanDecision:
    quote = dict(
        gross_weighted_odds=Decimal("2.2"),
        net_decimal_odds=Decimal("2.1"),
        cumulative_depth=Decimal("50"),
        levels_consumed=1,
    )
    return PaperScanDecision(
        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=["test"]),
        canonical_event_id="evt-priority-decouple",
        canonical_market_id="mkt-priority-decouple",
        eligible_for_paper_simulation=True,
        quote_age_ms=100,
        depth_scan=DepthScanResult(
            solution=ArbitrageSolution(
                is_arbitrage=True,
                implied_probability_sum=Decimal("0.9"),
                total_stake=Decimal("10"),
                guaranteed_return=Decimal("11"),
                guaranteed_profit=Decimal("1"),
                roi=Decimal("0.1"),
            ),
            selected_quotes=[
                DepthQuoteCandidate(
                    outcome="yes",
                    venue=VenueName.MATCHBOOK,
                    source_market_id="mb-1",
                    source_runner_id="r1",
                    **quote,
                ),
                DepthQuoteCandidate(
                    outcome="no",
                    venue=VenueName.POLYMARKET,
                    source_market_id="pm-1",
                    source_runner_id="r2",
                    **quote,
                ),
            ],
        ),
    )


def _patch_lifespan(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _aclose() -> None:
        return None

    monkeypatch.setattr(main_api, "get_live_refresh_coordinator", lambda: _Coordinator())
    monkeypatch.setattr(main_api, "get_universe_checkpoint_store", lambda: object())
    monkeypatch.setattr(main_api, "get_accounting_schedule", lambda: _Schedule())
    monkeypatch.setattr(main_api, "aclose_shared_provider_runtime", _aclose)
    monkeypatch.setattr(main_api, "aclose_shared_matchbook_client", _aclose)


def test_operations_dependency_does_not_depend_on_the_paper_alert_factory() -> None:
    dependency = inspect.signature(get_paper_operations_service).parameters["alerts"].default
    assert dependency.dependency is operations_priority_alerts
    assert "get_priority_alert_service()" not in inspect.getsource(
        paper_api.persist_price_engine_item_capture
    )
    assert "get_priority_alert_service()" not in inspect.getsource(_paper_operations)


def test_priority_alert_service_still_rejects_real_mode() -> None:
    real = Settings(sports_hedge_mode="real", sports_hedge_execution_enabled=False)
    with pytest.raises(ValueError, match="Priority alerts are paper-mode only"):
        PriorityAlertService(settings=real)
    armed = Settings(sports_hedge_mode="real", sports_hedge_execution_enabled=True)
    with pytest.raises(ValueError, match="Priority alerts are paper-mode only"):
        PriorityAlertService(settings=armed)


def test_paper_mode_still_builds_and_ingests_priority_alerts() -> None:
    settings = Settings(sports_hedge_mode="paper", sports_hedge_execution_enabled=False)
    repository = SqliteWatchlistRepository(":memory:")
    try:
        operations = PaperOperationsService(
            watchlist=WatchlistService(repository),
            settings=settings,
        )
        assert isinstance(operations.alerts, PriorityAlertService)
        assert operations.alerts.settings is settings
        ingested: list[object] = []
        original = operations.alerts.ingest

        def _spy(candidate: object) -> object:
            ingested.append(candidate)
            return original(candidate)

        operations.alerts.ingest = _spy  # type: ignore[method-assign]
        candidate = operations.persist_triggered_chain(
            _arb_decision(),
            provenance=DataProvenance.FIXTURE_DEMO,
            autofill=False,
        )
        assert len(ingested) == 1
        assert candidate is ingested[0]
        assert getattr(candidate, "canonical_market_id") == "mkt-priority-decouple"
    finally:
        repository.close()


def test_real_operations_skip_alert_ingestion_without_constructing_the_service() -> None:
    settings = Settings(sports_hedge_mode="real", sports_hedge_execution_enabled=False)
    constructed: list[object] = []
    original = PriorityAlertService.__init__

    def _track(self: PriorityAlertService, *args: object, **kwargs: object) -> None:
        constructed.append(self)
        original(self, *args, **kwargs)

    repository = SqliteWatchlistRepository(":memory:")
    try:
        PriorityAlertService.__init__ = _track  # type: ignore[method-assign]
        operations = PaperOperationsService(
            watchlist=WatchlistService(repository),
            settings=settings,
        )
        assert operations.alerts is None
        assert operations.settings is settings
        assert settings.sports_hedge_execution_enabled is False
        candidate = operations.persist_triggered_chain(
            _arb_decision(),
            provenance=DataProvenance.FIXTURE_DEMO,
            autofill=False,
        )
        assert candidate is not None
        assert constructed == []
    finally:
        PriorityAlertService.__init__ = original  # type: ignore[method-assign]
        repository.close()


def test_paper_journal_holder_uses_the_shared_priority_alert_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPORTS_HEDGE_MODE", "paper")
    _memory_stores(monkeypatch)
    _clear_process_caches()
    try:
        holder = get_paper_journal_holder()
        alerts = operations_priority_alerts()
        assert isinstance(alerts, PriorityAlertService)
        assert holder.alerts is alerts
        assert holder.alerts is get_priority_alert_service()
        assert holder.settings.sports_hedge_mode == "paper"
        assert holder.settings.sports_hedge_execution_enabled is False
    finally:
        _close_cached_stores()
        _clear_process_caches()


def test_real_lifespan_recovers_orphans_without_priority_alerts(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SPORTS_HEDGE_MODE", "real")
    _memory_stores(monkeypatch)
    _clear_process_caches()
    _patch_lifespan(monkeypatch)
    constructed: list[object] = []
    alert_factory_calls: list[str] = []
    recovered: list[PaperOperationsService] = []
    original_init = PriorityAlertService.__init__
    original_recover = PaperOperationsService.recover_orphaned_live_executions
    original_factory = paper_api.get_priority_alert_service

    def _track(self: PriorityAlertService, *args: object, **kwargs: object) -> None:
        constructed.append(self)
        original_init(self, *args, **kwargs)

    def _factory() -> PriorityAlertService:
        alert_factory_calls.append("get_priority_alert_service")
        return original_factory()

    def _recover(self: PaperOperationsService) -> list[dict[str, object]]:
        recovered.append(self)
        assert self.alerts is None
        assert self.settings.sports_hedge_mode == "real"
        assert self.settings.sports_hedge_execution_enabled is False
        return original_recover(self)

    PriorityAlertService.__init__ = _track  # type: ignore[method-assign]
    PaperOperationsService.recover_orphaned_live_executions = _recover  # type: ignore[method-assign]
    monkeypatch.setattr(paper_api, "get_priority_alert_service", _factory)
    try:
        with TestClient(main_api.app) as client:
            assert len(recovered) == 1
            assert recovered[0].alerts is None
            assert constructed == []
            assert alert_factory_calls == []
            health = client.get("/health")
            assert health.status_code == 200
            body = health.json()
            assert body["status"] == "ok"
            assert body["mode"] == "real"
            assert body["execution_enabled"] is False
            assert body["execution"]["configured_execution_enabled"] is False
            assert body["execution"]["live_execution_ready"] is False
            missing = client.get("/operations/fixtures/missing-real-startup-event")
            assert missing.status_code == 404
            near = client.get("/paper/watchlist/near")
            assert near.status_code == 200
            assert near.json() == []
            wired = get_paper_operations_service(
                recovered[0].watchlist,
                operations_priority_alerts(),
            )
            assert wired.alerts is None
            assert _paper_operations(recovered[0].watchlist).alerts is None
            assert constructed == []
            assert alert_factory_calls == []
    finally:
        PriorityAlertService.__init__ = original_init  # type: ignore[method-assign]
        PaperOperationsService.recover_orphaned_live_executions = original_recover  # type: ignore[method-assign]
        _close_cached_stores()
        _clear_process_caches()
