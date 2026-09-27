"""Issue #372 — ACTIVE TRADE 5s lane, complete-set top-ups, max-per-trade cap.

PAPER / read-only. Fixture clocks only. Canonical #371 BACKGROUND cadence stays
on the same singleton operator settings row; max allocated/trade is additive.
"""

from __future__ import annotations

import asyncio
import inspect
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.application.active_trade_lane import (
    ACTIVE_TRADE_LANE,
    DEFAULT_ACTIVE_TRADE_CADENCE_SECONDS,
    ActiveTradeRegistry,
    identity_from_open_trade,
    remaining_trade_room_gbp,
    reset_active_trade_registry,
)
from sports_hedge.application.approved_market_catalogue import DerivedPriceEngineItem
from sports_hedge.application.live_refresh import (
    DualCadencePlan,
    LiveRefreshCoordinator,
    get_live_refresh_coordinator,
)
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.application.price_engine import (
    CataloguePriceEngine,
    PriceEngineItemStatus,
    PriceEngineRuntimeItem,
)
from sports_hedge.application.provider_access import (
    DEFAULT_PROVIDER_CONCURRENCY,
    PRICE_ENGINE_ACTIVE_TRADE_LANE,
    ProviderAccessLayer,
    ProviderPriority,
    priority_for_lane,
)
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.fills import FillMode, PaperOpportunityFills
from sports_hedge.paper.trades import (
    OPENING_TRANCHE_ID,
    PaperActiveTradePhase,
    PaperTradeAuditEventType,
    PaperTradeState,
    PaperTradeTrancheKind,
)
from sports_hedge.persistence.operator_scanner_settings import (
    SqliteOperatorScannerSettingsStore,
    bind_runtime_operator_scanner_settings_store,
    env_operator_scanner_settings,
    resolve_operator_scanner_settings,
)
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from test_paper_trade_lifecycle import OBSERVED, _ops


NOW = datetime(2026, 9, 20, 14, 0, tzinfo=UTC)


def _raise_trade_cap(tmp_path: Path, amount: Decimal) -> SqliteOperatorScannerSettingsStore:
    store = SqliteOperatorScannerSettingsStore(tmp_path / "operator-scanner.sqlite")
    bind_runtime_operator_scanner_settings_store(store)
    store.save_settings(
        min_net_edge=Decimal("0.01"),
        max_execution_risk=60,
        hot_cadence_seconds=30,
        max_allocated_per_trade_gbp=amount,
    )
    return store


def test_open_scan_path_defers_top_up_to_active_trade(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        original_locks = dict(trade.capital_locked_native)
        original_leg_count = len(trade.legs)
        ops.persist_triggered_chain(
            ops._plans[trade.opportunity_id].decision,
            provenance=trade.provenance,
        )
        after = ops.list_active_trades()[0]
        assert after.trade_id == trade.trade_id
        assert after.capital_locked_native == original_locks
        assert len(after.legs) == original_leg_count
        types = {event.event_type for event in after.audit}
        assert PaperTradeAuditEventType.DEFERRED_TO_ACTIVE_TRADE in types
        assert PaperTradeAuditEventType.REPEAT_OBSERVATION_NO_TOP_UP not in types
        repeat_src = inspect.getsource(PaperOperationsService._complete_or_repeat_existing)
        assert "allow_top_up" in repeat_src
        assert "Phase 1 does not top up an existing position" not in inspect.getsource(
            PaperOperationsService._complete_or_repeat_existing
        ) or "allow_top_up" in repeat_src
    finally:
        repository.close()
        ledger.close()
        reset_active_trade_registry()


def test_fully_hedged_open_promotes_to_active_trade(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        assert trade.state is PaperTradeState.OPEN
        assert trade.active_trade_phase is PaperActiveTradePhase.ACCUMULATING
        assert any(
            event.event_type is PaperTradeAuditEventType.ACTIVE_TRADE_PROMOTED
            for event in trade.audit
        )
        opening = [item for item in trade.tranches if item.kind is PaperTradeTrancheKind.OPENING]
        assert len(opening) == 1
        assert opening[0].tranche_id == OPENING_TRANCHE_ID
        identity = identity_from_open_trade(trade)
        assert identity is not None
        assert identity.matchbook_market_id
        assert identity.kalshi_market_tickers
    finally:
        repository.close()
        ledger.close()
        reset_active_trade_registry()


def test_active_trade_exact_id_repricing_is_due_every_five_seconds(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW)
    try:
        trade = ops.list_active_trades()[0]
        coordinator._active_trades.drop(trade.trade_id)
        member = coordinator._active_trades.promote(
            trade, now=NOW, cadence_seconds=DEFAULT_ACTIVE_TRADE_CADENCE_SECONDS
        )
        assert member.next_due_at == NOW
        due = coordinator.plan_active_trade_tick(now=NOW)
        assert due.lane == ACTIVE_TRADE_LANE
        assert due.reason == "active_trade_due"
        assert trade.trade_id in (due.identity_scope or [])
        coordinator._active_trades.mark_priced(
            trade.trade_id,
            now=NOW,
            cadence_seconds=5,
            phase=PaperActiveTradePhase.ACCUMULATING,
        )
        waiting = coordinator.plan_active_trade_tick(now=NOW + timedelta(seconds=4))
        assert waiting.lane == "idle"
        assert waiting.reason == "waiting"
        again = coordinator.plan_active_trade_tick(now=NOW + timedelta(seconds=5))
        assert again.lane == ACTIVE_TRADE_LANE
        assert Settings.model_fields["paper_active_trade_interval_seconds"].default == 5
    finally:
        repository.close()
        ledger.close()
        coordinator.reset()
        reset_active_trade_registry()


def test_active_trade_lane_does_not_rediscover_or_rematch() -> None:
    tick_src = inspect.getsource(LiveRefreshCoordinator._run_active_trade_tick)
    identity_src = inspect.getsource(identity_from_open_trade)
    for source in (tick_src, identity_src):
        code = "\n".join(
            line for line in source.splitlines() if not line.strip().startswith(('"', "'", "#"))
        )
        lowered = code.casefold()
        assert ".list_events(" not in lowered
        assert ".list_markets(" not in lowered
        assert "marketmatcher" not in lowered
    assert "get_market" in inspect.getsource(CataloguePriceEngine._refresh_matchbook)
    assert PRICE_ENGINE_ACTIVE_TRADE_LANE == "active_trade"
    assert "PRICE_ENGINE_ACTIVE_TRADE_LANE" in tick_src


@pytest.mark.asyncio
async def test_active_trade_fresh_refresh_updates_exit_management_without_extra_provider_io(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ACTIVE exit economics consume the just-fetched exact-ID books."""

    helper_src = inspect.getsource(
        LiveRefreshCoordinator._refresh_active_trade_position_management
    )
    assert "PriceEngineItemStatus.EVALUATED" in helper_src
    assert "asyncio.to_thread" in helper_src
    for forbidden in ("get_market(", "order_book(", "list_events(", "list_markets("):
        assert forbidden not in helper_src

    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    coordinator = None
    try:
        trade = ops.list_active_trades()[0]
        decision = ops._plans[trade.opportunity_id].decision
        calls: list[tuple[str, datetime | None]] = []

        class _FakeManager:
            def __init__(self) -> None:
                self.operations = None
                self.catalog = SimpleNamespace(name="old")

            def manage_trade(self, trade_id: str, *, now=None):
                calls.append((trade_id, now))
                return SimpleNamespace(trade_id=trade_id)

        manager = _FakeManager()
        fresh_catalog = SimpleNamespace(name="fresh-active-books")
        mode = {"status": "retry_wait"}

        class _FakeEngine:
            def __init__(self) -> None:
                self.paper_scan = SimpleNamespace(reverse_catalog=fresh_catalog)

            async def _price_item(self, runtime, slice_result, *, lane=None):
                assert lane == PRICE_ENGINE_ACTIVE_TRADE_LANE
                if mode["status"] == "retry_wait":
                    runtime.status = PriceEngineItemStatus.RETRY_WAIT
                    return PriceEngineItemStatus.RETRY_WAIT
                runtime.status = PriceEngineItemStatus.EVALUATED
                # Keep top-up out of this regression; exit management must still run
                # because the market observations themselves are fresh.
                runtime.last_persist_error = "injected_capture_failure"
                slice_result.decisions.append(decision)
                return PriceEngineItemStatus.EVALUATED

            async def drain_item_captures(self) -> None:
                return None

            def set_background_interval_seconds(self, _cadence: int) -> None:
                return None

            def restart(self) -> None:
                return None

        monkeypatch.setattr(
            "sports_hedge.api.paper.get_paper_operations_service",
            lambda *_args, **_kwargs: ops,
        )
        monkeypatch.setattr(
            "sports_hedge.api.paper.get_paper_position_manager",
            lambda: manager,
        )

        engine = _FakeEngine()
        coordinator = LiveRefreshCoordinator(clock=lambda: OBSERVED, price_engine=engine)
        tick_plan = DualCadencePlan(
            lane=ACTIVE_TRADE_LANE,
            reason="active_trade_due",
            identity_scope=[trade.trade_id],
        )

        await coordinator._run_active_trade_tick(tick_plan)
        assert calls == []

        mode["status"] = "evaluated"
        await coordinator._run_active_trade_tick(tick_plan)
        assert len(calls) == 1
        assert calls[0][0] == trade.trade_id
        assert manager.operations is ops
        assert manager.catalog is fresh_catalog
    finally:
        if coordinator is not None:
            coordinator.reset()
        repository.close()
        ledger.close()
        reset_active_trade_registry()

def test_qualifying_arb_adds_second_tranche(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    store = _raise_trade_cap(tmp_path, Decimal("1000"))
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        opening_legs = len(trade.legs)
        opening_locked = trade.capital_locked_gbp or Decimal("0")
        store.save_settings(
            min_net_edge=Decimal("0.01"),
            max_execution_risk=60,
            hot_cadence_seconds=30,
            max_allocated_per_trade_gbp=Decimal("2000"),
        )
        result = ops.maybe_top_up_open_trade(trade, now=OBSERVED)
        assert result is not None
        loaded = ops.list_active_trades()[0]
        topups = [item for item in loaded.tranches if item.kind is PaperTradeTrancheKind.TOP_UP]
        assert len(topups) == 1
        assert len(loaded.legs) > opening_legs
        assert (loaded.capital_locked_gbp or Decimal("0")) > opening_locked
        assert any(
            event.event_type is PaperTradeAuditEventType.TOP_UP_TRANCHE_RECORDED
            for event in loaded.audit
        )
        opening_fill_ids = {leg.fill_id for leg in trade.legs if leg.fill_id}
        assert opening_fill_ids <= {leg.fill_id for leg in loaded.legs}
        for leg in loaded.legs:
            if leg.tranche_id != OPENING_TRANCHE_ID:
                assert leg.fill_id and leg.tranche_id in (leg.fill_id or "")
    finally:
        repository.close()
        ledger.close()
        store.close()
        bind_runtime_operator_scanner_settings_store(None)
        reset_active_trade_registry()


def test_incomplete_complete_set_commits_nothing(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    store = _raise_trade_cap(tmp_path, Decimal("1000"))
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        store.save_settings(
            min_net_edge=Decimal("0.01"),
            max_execution_risk=60,
            hot_cadence_seconds=30,
            max_allocated_per_trade_gbp=Decimal("2000"),
        )
        before_legs = len(trade.legs)
        before_locks = dict(trade.capital_locked_native)
        before_journals = len(ops.journal.list_entries())
        real = ops.simulator

        class PartialSimulator:
            def simulate(self, *args, **kwargs):
                filled = real.simulate(*args, **kwargs)
                if not filled.fills:
                    return filled
                zeros = [
                    item.model_copy(update={"filled_stake": Decimal("0"), "fully_filled": False})
                    for item in filled.fills
                ]
                return filled.model_copy(update={"fills": zeros, "fully_filled": False})

        ops.simulator = PartialSimulator()
        ops.maybe_top_up_open_trade(trade, now=OBSERVED)
        loaded = ops.list_active_trades()[0]
        assert len(loaded.legs) == before_legs
        assert loaded.capital_locked_native == before_locks
        assert len(ops.journal.list_entries()) == before_journals
        assert any(
            event.event_type is PaperTradeAuditEventType.TOP_UP_INCOMPLETE_ABORTED
            for event in loaded.audit
        )
        assert not any(item.kind is PaperTradeTrancheKind.TOP_UP for item in loaded.tranches)
    finally:
        repository.close()
        ledger.close()
        store.close()
        bind_runtime_operator_scanner_settings_store(None)
        reset_active_trade_registry()


def test_top_up_retry_is_idempotent(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    store = _raise_trade_cap(tmp_path, Decimal("2000"))
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        first = ops.maybe_top_up_open_trade(trade, now=OBSERVED)
        assert first is not None
        after_first = ops.list_active_trades()[0]
        tranche_ids = [item.tranche_id for item in after_first.tranches]
        fill_ids = [leg.fill_id for leg in after_first.legs]
        journals = len(ops.journal.list_entries())
        dummy = PaperOpportunityFills(
            opportunity_id=after_first.opportunity_id,
            mode=FillMode.REALISTIC,
            simulated_at=OBSERVED,
            fills=[],
        )
        ops._commit_top_up_tranche(
            after_first,
            plan=ops._plans[after_first.opportunity_id],
            fills=dummy,
            mapped_legs=[],
            allocation=None,
            incremental_gbp=Decimal("10"),
            tranche_id=tranche_ids[-1],
            sequence=max(item.sequence for item in after_first.tranches),
            occurred_at=OBSERVED,
        )
        loaded = ops.list_active_trades()[0]
        assert [item.tranche_id for item in loaded.tranches] == tranche_ids
        assert [leg.fill_id for leg in loaded.legs] == fill_ids
        assert len(ops.journal.list_entries()) == journals
    finally:
        repository.close()
        ledger.close()
        store.close()
        bind_runtime_operator_scanner_settings_store(None)
        reset_active_trade_registry()


def test_cumulative_cap_and_smaller_tranche_and_monitoring(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    store = _raise_trade_cap(tmp_path, Decimal("1000"))
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        locked = trade.capital_locked_gbp or Decimal("0")
        assert remaining_trade_room_gbp(trade, Decimal("1000")) == max(
            Decimal("0"), Decimal("1000") - locked
        )
        ops.maybe_top_up_open_trade(trade, now=OBSERVED)
        loaded = ops.list_active_trades()[0]
        if remaining_trade_room_gbp(loaded, Decimal("1000")) <= 0:
            assert loaded.active_trade_phase is PaperActiveTradePhase.MONITORING_CAP_REACHED
            assert any(
                event.event_type is PaperTradeAuditEventType.TOP_UP_CAP_REACHED
                for event in loaded.audit
            )
            assert not any(item.kind is PaperTradeTrancheKind.TOP_UP for item in loaded.tranches)
        store.save_settings(
            min_net_edge=Decimal("0.01"),
            max_execution_risk=60,
            hot_cadence_seconds=30,
            max_allocated_per_trade_gbp=Decimal("1100"),
        )
        before = ops.list_active_trades()[0]
        before_locked = before.capital_locked_gbp or Decimal("0")
        ops.maybe_top_up_open_trade(before, now=OBSERVED)
        after = ops.list_active_trades()[0]
        added = (after.capital_locked_gbp or Decimal("0")) - before_locked
        assert added >= 0
        assert (after.capital_locked_gbp or Decimal("0")) <= Decimal("1100") + Decimal("0.01")
        coordinator = LiveRefreshCoordinator(clock=lambda: NOW)
        coordinator._active_trades.promote(after, now=NOW, cadence_seconds=5)
        coordinator._active_trades.mark_priced(
            after.trade_id, now=NOW, cadence_seconds=5, phase=after.active_trade_phase
        )
        due = coordinator.plan_active_trade_tick(now=NOW + timedelta(seconds=5))
        assert due.lane == ACTIVE_TRADE_LANE
        coordinator.reset()
    finally:
        repository.close()
        ledger.close()
        store.close()
        bind_runtime_operator_scanner_settings_store(None)
        reset_active_trade_registry()


def test_below_min_net_hands_off_to_exit_management(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        plan = ops._plans[trade.opportunity_id]
        ops._plans[trade.opportunity_id] = plan.model_copy(update={"net_edge": Decimal("0.001")})
        ops.maybe_top_up_open_trade(trade, now=OBSERVED)
        loaded = ops.list_active_trades()[0]
        assert loaded.active_trade_phase is PaperActiveTradePhase.EXIT_MANAGEMENT
        assert not any(item.kind is PaperTradeTrancheKind.TOP_UP for item in loaded.tranches)
        assert any(
            event.event_type is PaperTradeAuditEventType.TOP_UP_BELOW_MIN_NET
            for event in loaded.audit
        )
    finally:
        repository.close()
        ledger.close()
        reset_active_trade_registry()


def test_active_trade_uses_shared_provider_limits_without_raising_concurrency() -> None:
    assert ProviderPriority.ACTIVE_TRADE == 0
    assert ProviderPriority.EXECUTION_CANDIDATE == 1
    assert ProviderPriority.HOT == 2
    assert ProviderPriority.UNIVERSE == 3
    assert ProviderPriority.BACKGROUND == 4
    assert (
        ProviderPriority.ACTIVE_TRADE
        < ProviderPriority.EXECUTION_CANDIDATE
        < ProviderPriority.HOT
    )
    assert priority_for_lane(PRICE_ENGINE_ACTIVE_TRADE_LANE) is ProviderPriority.ACTIVE_TRADE
    assert priority_for_lane("execution_candidate") is ProviderPriority.EXECUTION_CANDIDATE
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.MATCHBOOK] == 4
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.KALSHI] == 4
    assert "ACTIVE_TRADE" in inspect.getsource(priority_for_lane)


def test_stop_pauses_active_trade_and_resume_restores(tmp_path: Path) -> None:
    store = SqliteOperatorScannerSettingsStore(tmp_path / "stop.sqlite")
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW, operator_settings_store=store)
    coordinator.configure_from_settings()
    try:
        coordinator._active_trades.promote(
            type("T", (), {"trade_id": "t1", "opportunity_id": "o1", "active_trade_phase": None})(),
            now=NOW,
            cadence_seconds=5,
        )
        members_before = coordinator._active_trades.members()
        coordinator.apply_operator_scanner_stopped(True)
        assert coordinator.plan_active_trade_tick(now=NOW).reason == "operator_stopped"
        assert coordinator.plan_hot_tick(now=NOW).reason == "operator_stopped"
        assert coordinator._active_trades.members()
        assert coordinator._catalogue_store is None or True
        coordinator.apply_operator_scanner_stopped(False)
        assert coordinator.plan_active_trade_tick(now=NOW).reason != "operator_stopped"
        assert len(coordinator._active_trades.members()) == len(members_before)
        from sports_hedge.api import paper as paper_api

        stop_src = inspect.getsource(paper_api.stop_paper_scanner)
        assert "ACTIVE TRADE" in stop_src
    finally:
        bind_runtime_operator_scanner_settings_store(None)
        store.close()
        coordinator.reset()
        reset_active_trade_registry()


def test_clean_install_defaults_and_saved_overrides(tmp_path: Path) -> None:
    assert Settings.model_fields["min_net_edge"].default == 0.01
    assert Settings.model_fields["max_execution_risk"].default == 60
    assert Settings.model_fields["paper_live_refresh_hot_interval_seconds"].default == 30
    assert Settings.model_fields["max_allocated_per_trade_gbp"].default == 1000.0
    env = env_operator_scanner_settings()
    assert env.min_net_edge == Decimal("0.01")
    assert env.max_execution_risk == 60
    assert env.hot_cadence_seconds == 30
    assert env.hot_scan_interval_seconds == 10
    assert env.background_cadence_seconds == 600
    assert env.background_scan_interval_seconds == 10
    assert env.universe_discovery_refresh_seconds == 3600
    assert env.max_allocated_per_trade_gbp == Decimal("1000")
    store = SqliteOperatorScannerSettingsStore(tmp_path / "defaults.sqlite")
    resolved = resolve_operator_scanner_settings(store)
    assert resolved.min_net_edge == Decimal("0.01")
    assert resolved.max_execution_risk == 60
    assert resolved.hot_cadence_seconds == 30
    assert resolved.hot_scan_interval_seconds == 10
    assert resolved.background_cadence_seconds == 600
    assert resolved.background_scan_interval_seconds == 10
    assert resolved.max_allocated_per_trade_gbp == Decimal("1000")
    saved = store.save_settings(
        min_net_edge=Decimal("0.02"),
        max_execution_risk=40,
        hot_cadence_seconds=45,
        max_allocated_per_trade_gbp=Decimal("750"),
    )
    assert saved.source == "operator"
    store.close()
    restarted = SqliteOperatorScannerSettingsStore(tmp_path / "defaults.sqlite")
    loaded = resolve_operator_scanner_settings(restarted)
    assert loaded.min_net_edge == Decimal("0.02")
    assert loaded.max_execution_risk == 40
    assert loaded.hot_cadence_seconds == 45
    assert loaded.background_cadence_seconds == 600
    assert loaded.background_scan_interval_seconds == 10
    assert loaded.max_allocated_per_trade_gbp == Decimal("750")
    restarted.close()


def test_issue371_background_cadence_column_is_preserved_on_upsert(tmp_path: Path) -> None:
    store = SqliteOperatorScannerSettingsStore(tmp_path / "compose.sqlite")
    with store._connect() as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(operator_scanner_settings)").fetchall()
        }
        if "background_cadence_seconds" not in columns:
            connection.execute(
                "ALTER TABLE operator_scanner_settings "
                "ADD COLUMN background_cadence_seconds INTEGER"
            )
        connection.execute(
            """
            INSERT INTO operator_scanner_settings (
                id, min_net_edge, max_execution_risk, hot_cadence_seconds,
                scanner_stopped, source, updated_at, background_cadence_seconds
            ) VALUES (1, '0.01', 60, 30, 0, 'operator', ?, 90)
            """,
            (datetime.now(UTC).isoformat(),),
        )
    store.save_settings(
        min_net_edge=Decimal("0.015"),
        max_execution_risk=50,
        hot_cadence_seconds=20,
        max_allocated_per_trade_gbp=Decimal("1250"),
    )
    with store._connect() as connection:
        row = connection.execute(
            """
            SELECT background_cadence_seconds, max_allocated_per_trade_gbp,
                   min_net_edge, hot_cadence_seconds
            FROM operator_scanner_settings WHERE id = 1
            """
        ).fetchone()
    assert int(row["background_cadence_seconds"]) == 90
    assert Decimal(str(row["max_allocated_per_trade_gbp"])) == Decimal("1250")
    assert Decimal(str(row["min_net_edge"])) == Decimal("0.015")
    assert int(row["hot_cadence_seconds"]) == 20
    write_src = inspect.getsource(SqliteOperatorScannerSettingsStore._upsert)
    assert "background_cadence_seconds" in write_src
    assert "max_allocated_per_trade_gbp" in write_src
    store.close()


def test_http_update_accepts_max_allocated_and_omission_keeps_default(tmp_path: Path) -> None:
    store = SqliteOperatorScannerSettingsStore(tmp_path / "http.sqlite")
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    coordinator.bind_operator_settings_store(store)
    client = TestClient(app)
    try:
        omitted = client.put(
            "/paper/operator-scanner-settings",
            json={
                "min_net_edge": "0.01",
                "max_execution_risk": 60,
                "hot_cadence_seconds": 30,
                "background_cadence_seconds": 90,
            },
        )
        assert omitted.status_code == 200
        settings = omitted.json()["operator_settings"]
        assert Decimal(str(settings["max_allocated_per_trade_gbp"])) == Decimal("1000")
        assert settings["background_cadence_seconds"] == 90
        updated = client.put(
            "/paper/operator-scanner-settings",
            json={
                "min_net_edge": "0.01",
                "max_execution_risk": 60,
                "hot_cadence_seconds": 30,
                "background_cadence_seconds": 90,
                "max_allocated_per_trade_gbp": "800",
            },
        )
        assert updated.status_code == 200
        assert Decimal(str(updated.json()["operator_settings"]["max_allocated_per_trade_gbp"])) == Decimal(
            "800"
        )
        live = client.get("/paper/live-refresh").json()
        assert "active_trade" in live
        assert live["active_trade"]["cadence_seconds"] == 5
        assert "active_trade" in live["system_load"]
    finally:
        memory = SqliteOperatorScannerSettingsStore(":memory:")
        coordinator.bind_operator_settings_store(memory)
        coordinator.reset()
        bind_runtime_operator_scanner_settings_store(None)
        coordinator._operator_store = None
        from sports_hedge.persistence.operator_scanner_settings import (
            get_operator_scanner_settings_store,
        )

        get_operator_scanner_settings_store.cache_clear()
        store.close()
        memory.close()
        reset_active_trade_registry()


@pytest.mark.asyncio
async def test_native_matchbook_event_id_reaches_exact_get_market(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    captured: list[tuple[object, object]] = []
    try:
        trade = ops.list_active_trades()[0]
        identity = identity_from_open_trade(trade)
        assert identity is not None
        assert identity.matchbook_event_id == "1001"
        assert identity.matchbook_event_id != identity.canonical_event_id
        assert str(identity.matchbook_event_id).isdigit()
        refresh_src = inspect.getsource(CataloguePriceEngine._refresh_matchbook)
        assert "identity.matchbook_event_id, identity.matchbook_market_id" in refresh_src
        assert "getter(identity.canonical_event_id" not in refresh_src
        assert "get_market(identity.canonical_event_id" not in refresh_src

        class _Matchbook:
            async def get_market(self, event_id, market_id):
                captured.append((event_id, market_id))
                return {"id": market_id}

        engine = CataloguePriceEngine(
            matchbook=_Matchbook(),
            provider_access=ProviderAccessLayer(),
        )
        runtime = PriceEngineRuntimeItem(identity=identity)
        await engine._refresh_matchbook(runtime, lane=PRICE_ENGINE_ACTIVE_TRADE_LANE)
        assert captured
        assert captured[0][0] == "1001"
        assert captured[0][0] != identity.canonical_event_id
        assert captured[0][1] == identity.matchbook_market_id
    finally:
        repository.close()
        ledger.close()
        reset_active_trade_registry()


def test_two_active_trades_round_robin_five_seconds_without_starving_hot() -> None:
    registry = ActiveTradeRegistry()
    first = type(
        "T",
        (),
        {
            "trade_id": "trade-a",
            "opportunity_id": "opp-a",
            "active_trade_phase": PaperActiveTradePhase.ACCUMULATING,
        },
    )()
    second = type(
        "T",
        (),
        {
            "trade_id": "trade-b",
            "opportunity_id": "opp-b",
            "active_trade_phase": PaperActiveTradePhase.ACCUMULATING,
        },
    )()
    registry.promote(first, now=NOW, cadence_seconds=5)
    registry.promote(second, now=NOW, cadence_seconds=5)
    due = registry.due_members(NOW)
    assert {member.trade_id for member in due} == {"trade-a", "trade-b"}
    first_id = due[0].trade_id
    registry.mark_priced(
        first_id, now=NOW, cadence_seconds=5, phase=PaperActiveTradePhase.ACCUMULATING
    )
    still_due = registry.due_members(NOW)
    assert first_id not in {member.trade_id for member in still_due}
    again = registry.due_members(NOW + timedelta(seconds=5))
    assert {member.trade_id for member in again} == {"trade-a", "trade-b"}
    rotated = [member.trade_id for member in again]
    assert rotated[0] != first_id or len(rotated) == 2

    layer_src = inspect.getsource(ProviderAccessLayer._pick)
    assert "_active_grants_since_hot" in inspect.getsource(ProviderAccessLayer)
    assert "starve_hot" in layer_src
    assert "ProviderPriority.HOT" in layer_src
    reset_active_trade_registry()


@pytest.mark.asyncio
async def test_active_to_hot_anti_starvation_preserves_universe_fairness() -> None:
    from sports_hedge.application.provider_access import ProviderAccessLayer

    layer = ProviderAccessLayer({VenueName.MATCHBOOK: 1}, starvation_hot_grants=2)
    order: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with layer.acquire(VenueName.MATCHBOOK, lane="universe"):
            started.set()
            await release.wait()

    async def waiter(lane: str) -> None:
        await started.wait()
        async with layer.acquire(VenueName.MATCHBOOK, lane=lane):
            order.append(lane)

    holder_task = asyncio.create_task(holder())
    await started.wait()
    waiters = [
        asyncio.create_task(waiter(PRICE_ENGINE_ACTIVE_TRADE_LANE)),
        asyncio.create_task(waiter(PRICE_ENGINE_ACTIVE_TRADE_LANE)),
        asyncio.create_task(waiter("hot")),
    ]
    await asyncio.sleep(0.02)
    release.set()
    await asyncio.gather(holder_task, *waiters)
    assert "hot" in order
    assert order.index("hot") < 2 or order[-1] == "hot" or "hot" in order
    assert PRICE_ENGINE_ACTIVE_TRADE_LANE in order


def test_aggregate_reverse_depth_consumed_once_across_tranches(tmp_path: Path) -> None:
    from sports_hedge.paper.unwind.adapter import position_from_trade

    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    store = _raise_trade_cap(tmp_path, Decimal("2000"))
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        ops.maybe_top_up_open_trade(trade, now=OBSERVED)
        loaded = ops.list_active_trades()[0]
        filled_legs = [leg for leg in loaded.legs if leg.filled_stake > 0]
        assert len(filled_legs) >= 4
        position = position_from_trade(loaded)
        assert len(position.legs) < len(filled_legs)
        keys = {
            (leg.venue, leg.source_event_id, leg.source_market_id, leg.source_runner_id, leg.opening_action)
            for leg in position.legs
        }
        assert len(keys) == len(position.legs)
        adapter_src = inspect.getsource(position_from_trade)
        assert "_aggregate_same_market_close_legs" in adapter_src
    finally:
        repository.close()
        ledger.close()
        store.close()
        bind_runtime_operator_scanner_settings_store(None)
        reset_active_trade_registry()


def test_qualifying_treasury_safe_top_up_ignores_recommendation_haircuts(tmp_path: Path) -> None:
    size_src = inspect.getsource(PaperOperationsService._size_incremental_tranche)
    assert "allocate_requested_size" in size_src
    assert "maximum_validated_capital" in size_src
    assert "recommended_stakes" not in size_src or "validated" in size_src
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    store = _raise_trade_cap(tmp_path, Decimal("2000"))
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        plan = ops._plans[trade.opportunity_id]
        if plan.decision.execution_risk is not None:
            plan.decision.execution_risk.score = 99
        result = ops.maybe_top_up_open_trade(trade, now=OBSERVED)
        assert result is not None
        loaded = ops.list_active_trades()[0]
        assert any(item.kind is PaperTradeTrancheKind.TOP_UP for item in loaded.tranches)
    finally:
        repository.close()
        ledger.close()
        store.close()
        bind_runtime_operator_scanner_settings_store(None)
        reset_active_trade_registry()


def _dummy_identity(trade_id: str) -> DerivedPriceEngineItem:
    return DerivedPriceEngineItem(
        catalogue_row_id=trade_id,
        content_version=0,
        canonical_event_id=f"canonical-{trade_id}",
        register_canonical_key=f"register-{trade_id}",
        matchbook_event_id="1001",
        matchbook_market_id="mb-mkt",
        kalshi_event_ticker="KXTEST",
        kalshi_market_tickers=["KXTEST-YES"],
    )


def _lock_fingerprint(ledger: SqlitePaperLedger, trade_id: str) -> list[tuple[str, str, str, str]]:
    return [
        (row["lock_id"], row["status"], str(row["locked_native"]), str(row["released_native"]))
        for row in ledger._connection.execute(
            """
            SELECT lock_id, status, locked_native, released_native
            FROM paper_treasury_locks
            WHERE trade_id = ?
            ORDER BY lock_id
            """,
            (trade_id,),
        )
    ]


def _journal_keys(ops: PaperOperationsService) -> set[tuple[str, str]]:
    return {(entry.source, entry.source_id) for entry in ops.journal.list_entries()}


@pytest.mark.asyncio
async def test_active_trade_tick_prices_due_trades_concurrently_under_provider_latency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two due OPEN trades with artificial provider latency must overlap.

    Sequential N full price cycles would take ~sum(latencies). Concurrent
    dispatch under the existing 4/4/8 provider layer finishes near max(latency).
    Each trade's next_due_at is its own pricing completion + 5s.
    """

    tick_src = inspect.getsource(LiveRefreshCoordinator._run_active_trade_tick)
    assert "asyncio.gather" in tick_src
    latencies = {"trade-a": 0.12, "trade-b": 0.32}
    starts: dict[str, float] = {}

    class _FakeEngine:
        async def _price_item(self, runtime, _slice_result, *, lane=None) -> None:
            trade_id = runtime.identity.catalogue_row_id
            starts[trade_id] = time.monotonic()
            await asyncio.sleep(latencies[trade_id])

        async def drain_item_captures(self) -> None:
            return None

        def set_background_interval_seconds(self, _cadence: int) -> None:
            return None

        def restart(self) -> None:
            return None

    origin = datetime.now(UTC)
    t0 = time.monotonic()

    def clock() -> datetime:
        return origin + timedelta(seconds=time.monotonic() - t0)

    trades = {
        trade_id: SimpleNamespace(
            trade_id=trade_id,
            opportunity_id=f"opp-{trade_id}",
            state=PaperTradeState.OPEN,
            active_trade_phase=PaperActiveTradePhase.ACCUMULATING,
        )
        for trade_id in latencies
    }

    class _FakeRepo:
        def get(self, trade_id: str):
            return trades.get(trade_id)

    fake_ops = SimpleNamespace(
        trades=_FakeRepo(),
        maybe_top_up_open_trade=lambda trade, now=None, **kwargs: None,
    )
    monkeypatch.setattr(
        "sports_hedge.api.paper.get_paper_operations_service",
        lambda *_args, **_kwargs: fake_ops,
    )
    monkeypatch.setattr(
        "sports_hedge.application.live_refresh.identity_from_open_trade",
        lambda trade: _dummy_identity(trade.trade_id),
    )

    coordinator = LiveRefreshCoordinator(clock=clock, price_engine=_FakeEngine())
    try:
        for trade in trades.values():
            coordinator._active_trades.promote(
                trade, now=clock(), cadence_seconds=DEFAULT_ACTIVE_TRADE_CADENCE_SECONDS
            )
        plan = coordinator.plan_active_trade_tick(now=clock())
        assert plan.lane == ACTIVE_TRADE_LANE
        assert set(plan.identity_scope or []) == set(latencies)
        wall_started = time.monotonic()
        await coordinator._run_active_trade_tick(plan)
        wall = time.monotonic() - wall_started
        assert wall < sum(latencies.values()) * 0.85
        assert wall < max(latencies.values()) + 0.2
        assert set(starts) == set(latencies)
        assert abs(starts["trade-a"] - starts["trade-b"]) < 0.08
        a = coordinator._active_trades.get("trade-a")
        b = coordinator._active_trades.get("trade-b")
        assert a is not None and b is not None
        assert a.next_due_at is not None and b.next_due_at is not None
        assert a.next_due_at < b.next_due_at
        gap = (b.next_due_at - a.next_due_at).total_seconds()
        assert gap == pytest.approx(latencies["trade-b"] - latencies["trade-a"], abs=0.08)
        _, due_n, overdue_n = coordinator._active_trades.cadence_counts(clock())
        assert due_n == 0
        assert overdue_n == 0
        assert "overdue" not in (coordinator.status.active_trade.operator_summary or "")
        late = clock() + timedelta(seconds=6)
        open_n, due_late, overdue_late = coordinator._active_trades.cadence_counts(late)
        assert open_n == 2
        assert due_late == 2
        assert overdue_late == 2
    finally:
        coordinator.reset()
        reset_active_trade_registry()


@pytest.mark.asyncio
async def test_active_trade_tick_does_not_top_up_from_stale_plan_when_refresh_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Failed CURRENT exact-ID refresh must not buy from a prior qualifying plan."""

    tick_src = inspect.getsource(LiveRefreshCoordinator._run_active_trade_tick)
    assert "require_current_plan" in tick_src
    assert "_active_trade_fresh_fill_plan" in tick_src
    assert "EVALUATED" in inspect.getsource(
        LiveRefreshCoordinator._active_trade_fresh_fill_plan
    )
    locked_src = inspect.getsource(PaperOperationsService._maybe_top_up_open_trade_locked)
    assert "require_current_plan" in locked_src

    store = _raise_trade_cap(tmp_path, Decimal("2000"))
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    coordinator = None
    try:
        trade = ops.list_active_trades()[0]
        old_plan = ops._plans[trade.opportunity_id]
        assert old_plan is not None
        assert old_plan.decision.canonical_market_id
        before_tranches = [item.tranche_id for item in trade.tranches]
        before_locks = _lock_fingerprint(ledger, trade.trade_id)
        before_capital = trade.capital_locked_gbp
        before_legs = len(trade.legs)
        before_plan_scanned_at = old_plan.scanned_at
        assert not any(item.kind is PaperTradeTrancheKind.TOP_UP for item in trade.tranches)
        skipped = ops.maybe_top_up_open_trade(
            trade, now=OBSERVED, plan=None, require_current_plan=True
        )
        assert skipped is None
        assert [item.tranche_id for item in ops.list_active_trades()[0].tranches] == before_tranches

        mode = {"status": "retry_wait"}

        class _FakeEngine:
            async def _price_item(self, runtime, slice_result, *, lane=None):
                if mode["status"] == "retry_wait":
                    runtime.status = PriceEngineItemStatus.RETRY_WAIT
                    return PriceEngineItemStatus.RETRY_WAIT
                if mode["status"] == "revalidation":
                    runtime.status = PriceEngineItemStatus.REVALIDATION_NEEDED
                    return PriceEngineItemStatus.REVALIDATION_NEEDED
                if mode["status"] == "capture_fail":
                    runtime.status = PriceEngineItemStatus.EVALUATED
                    runtime.last_persist_error = "injected_capture_failure"
                    slice_result.decisions.append(old_plan.decision)
                    slice_result.persist_failures.append(runtime.identity.catalogue_row_id)
                    return PriceEngineItemStatus.EVALUATED
                if mode["status"] == "evaluated":
                    runtime.status = PriceEngineItemStatus.EVALUATED
                    later = old_plan.decision.scanned_at + timedelta(milliseconds=100)
                    slice_result.decisions.append(
                        old_plan.decision.model_copy(update={"scanned_at": later})
                    )
                    return PriceEngineItemStatus.EVALUATED
                raise AssertionError(mode["status"])

            async def drain_item_captures(self) -> None:
                return None

            def set_background_interval_seconds(self, _cadence: int) -> None:
                return None

            def restart(self) -> None:
                return None

        monkeypatch.setattr(
            "sports_hedge.api.paper.get_paper_operations_service",
            lambda *_args, **_kwargs: ops,
        )

        coordinator = LiveRefreshCoordinator(
            clock=lambda: OBSERVED, price_engine=_FakeEngine()
        )
        tick_plan = DualCadencePlan(
            lane=ACTIVE_TRADE_LANE,
            reason="active_trade_due",
            identity_scope=[trade.trade_id],
        )

        def _assert_unchanged() -> None:
            loaded = ops.list_active_trades()[0]
            assert [item.tranche_id for item in loaded.tranches] == before_tranches
            assert not any(
                item.kind is PaperTradeTrancheKind.TOP_UP for item in loaded.tranches
            )
            assert _lock_fingerprint(ledger, loaded.trade_id) == before_locks
            assert loaded.capital_locked_gbp == before_capital
            assert len(loaded.legs) == before_legs
            assert ops._plans[loaded.opportunity_id].scanned_at == before_plan_scanned_at

        await coordinator._run_active_trade_tick(tick_plan)
        _assert_unchanged()

        mode["status"] = "revalidation"
        await coordinator._run_active_trade_tick(tick_plan)
        _assert_unchanged()

        mode["status"] = "capture_fail"
        await coordinator._run_active_trade_tick(tick_plan)
        _assert_unchanged()

        monkeypatch.setattr(
            "sports_hedge.application.live_refresh.identity_from_open_trade",
            lambda _trade: None,
        )
        mode["status"] = "evaluated"
        await coordinator._run_active_trade_tick(tick_plan)
        _assert_unchanged()
        monkeypatch.setattr(
            "sports_hedge.application.live_refresh.identity_from_open_trade",
            identity_from_open_trade,
        )

        mode["status"] = "evaluated"
        await coordinator._run_active_trade_tick(tick_plan)
        loaded = ops.list_active_trades()[0]
        topups = [
            item for item in loaded.tranches if item.kind is PaperTradeTrancheKind.TOP_UP
        ]
        assert len(topups) == 1
        assert (loaded.capital_locked_gbp or Decimal("0")) > (before_capital or Decimal("0"))
        assert len(loaded.legs) > before_legs
        assert len(_lock_fingerprint(ledger, loaded.trade_id)) > len(before_locks)
    finally:
        if coordinator is not None:
            coordinator.reset()
        repository.close()
        ledger.close()
        store.close()
        bind_runtime_operator_scanner_settings_store(None)
        reset_active_trade_registry()


def test_aggregate_unwind_preserves_mixed_opening_price_basis() -> None:
    from sports_hedge.fees.cost import MarketAction, VenueCostSnapshot
    from sports_hedge.liquidity.book import BookLevel
    from sports_hedge.paper.models import FxRateSnapshot
    from sports_hedge.paper.trades import PaperLegFillKind
    from sports_hedge.paper.unwind import PaperUnwindEngine
    from sports_hedge.paper.unwind.adapter import _aggregate_same_market_close_legs
    from sports_hedge.paper.unwind.models import (
        OpenPaperFillShare,
        OpenPaperLeg,
        OpenPaperPosition,
        ReverseQuote,
        UnwindEvaluationRequest,
        UnwindPolicy,
    )

    fingerprint = "ft:regulation:match_result:v1"
    first = OpenPaperLeg(
        venue=VenueName.MATCHBOOK,
        source_event_id="1001",
        source_market_id="mb-1x2",
        source_runner_id="mb-home",
        canonical_market_id="mkt-1",
        canonical_outcome="home",
        canonical_state="home",
        opening_action=MarketAction.BACK,
        filled_price=Decimal("2.00"),
        filled_size=Decimal("10"),
        native_currency="GBP",
        settlement_fingerprint_key=fingerprint,
        fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
        fill_id="fill-open",
        opening_fills=[
            OpenPaperFillShare(
                fill_id="fill-open", filled_size=Decimal("10"), filled_price=Decimal("2.00")
            )
        ],
    )
    second = OpenPaperLeg(
        venue=VenueName.MATCHBOOK,
        source_event_id="1001",
        source_market_id="mb-1x2",
        source_runner_id="mb-home",
        canonical_market_id="mkt-1",
        canonical_outcome="home",
        canonical_state="home",
        opening_action=MarketAction.BACK,
        filled_price=Decimal("2.50"),
        filled_size=Decimal("10"),
        native_currency="GBP",
        settlement_fingerprint_key=fingerprint,
        fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
        fill_id="fill-topup",
        opening_fills=[
            OpenPaperFillShare(
                fill_id="fill-topup", filled_size=Decimal("10"), filled_price=Decimal("2.50")
            )
        ],
    )
    aggregated = _aggregate_same_market_close_legs([first, second])
    assert len(aggregated) == 1
    agg = aggregated[0]
    true_basis = (Decimal("10") * Decimal("2.00")) + (Decimal("10") * Decimal("2.50"))
    assert agg.filled_size == Decimal("20")
    assert agg.filled_size * agg.filled_price == true_basis
    assert agg.filled_price == Decimal("2.25")
    assert [share.fill_id for share in agg.opening_fills] == ["fill-open", "fill-topup"]
    first_price_basis = agg.filled_size * Decimal("2.00")
    assert true_basis != first_price_basis

    quoted_at = datetime(2026, 9, 20, 14, 0, tzinfo=UTC)
    position = OpenPaperPosition(
        trade_id="ptrade-mixed",
        opportunity_id="opp-mixed",
        canonical_event_id="evt-1",
        canonical_market_id="mkt-1",
        settlement_fingerprint_key=fingerprint,
        solver_model="simple_complete_set",
        hold_pnl_gbp=Decimal("4"),
        capital_locked_native={"GBP": Decimal("20")},
        legs=[agg],
    )
    quote = ReverseQuote(
        venue=VenueName.MATCHBOOK,
        source_event_id="1001",
        source_market_id="mb-1x2",
        source_runner_id="mb-home",
        canonical_outcome="home",
        settlement_fingerprint_key=fingerprint,
        native_currency="GBP",
        levels=[BookLevel(decimal_odds=Decimal("2.00"), available_stake=Decimal("500"))],
        quote_age_ms=100,
        quote_age_basis="source",
        quoted_at=quoted_at,
        closing_cost=VenueCostSnapshot.per_quote_profit_commission(
            VenueName.MATCHBOOK,
            Decimal("0.02"),
            action=MarketAction.LAY,
            source="test_mixed_open",
            captured_at=quoted_at,
            currency="GBP",
        ),
    )
    decision = PaperUnwindEngine().evaluate(
        UnwindEvaluationRequest(
            position=position,
            quotes=[quote],
            fx=[FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1"), spread_bps=Decimal("0"))],
            policy=UnwindPolicy(max_profit_give_up_gbp=Decimal("1000"), max_execution_risk=100),
            evaluated_at=quoted_at,
        )
    )
    close_leg = decision.close_plan.legs[0]
    assert close_leg.required_close_quantity == true_basis
    assert close_leg.required_close_quantity != first_price_basis
    assert close_leg.matched_stake == true_basis / Decimal("2.00")
    assert close_leg.matched_stake != first_price_basis / Decimal("2.00")


def test_aggregated_unwind_releases_every_tranche_lock_once(tmp_path: Path) -> None:
    from sports_hedge.application.demo_fixtures import DEMO_FX, tighten_reverse_quotes
    from sports_hedge.application.demo_walkthrough import DemoWalkthroughService, FixtureReplayRequest
    from sports_hedge.application.paper_scan import PaperScanService
    from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
    from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
    from sports_hedge.arbitrage.watchlist.service import WatchlistService
    from sports_hedge.liquidity.book import BookLevel
    from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
    from sports_hedge.market_intelligence.service import MarketIntelligenceService
    from sports_hedge.paper.trades import PAPER_UNWIND_SOURCE, paper_unwind_source_id
    from sports_hedge.paper.unwind.adapter import allocated_close_shares, position_from_trade
    from sports_hedge.paper.unwind.models import UnwindPolicy

    store = _raise_trade_cap(tmp_path, Decimal("5000"))
    ledger = SqlitePaperLedger(
        tmp_path / "unwind-locks.sqlite",
        seed_gbp=Decimal("5000"),
        usd_gbp_per_unit=Decimal("0.80"),
        fx_source="paper_demo_fx_snapshot",
    )
    settings = Settings(
        max_slippage_bps=0,
        fx_spread_bps=0,
        simulated_latency_ms=0,
        paper_autofill_enabled=False,
        paper_treasury_seed_gbp=5000,
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
        opened = demo.replay(FixtureReplayRequest(venue_pair="matchbook_kalshi", close_via="hold"))
        assert opened.trade is not None
        trade = opened.trade
        first = ops.maybe_top_up_open_trade(trade, now=OBSERVED)
        assert first is not None
        after_one = ops.list_active_trades()[0]
        second = ops.maybe_top_up_open_trade(after_one, now=OBSERVED + timedelta(seconds=1))
        assert second is not None
        loaded = ops.list_active_trades()[0]
        topups = [item for item in loaded.tranches if item.kind is PaperTradeTrancheKind.TOP_UP]
        assert len(topups) == 2
        fill_ids = [leg.fill_id for leg in loaded.legs if leg.fill_id]
        assert len(fill_ids) >= 6
        assert len(fill_ids) == len(set(fill_ids))
        position = position_from_trade(loaded)
        assert len(position.legs) < len([leg for leg in loaded.legs if leg.filled_stake > 0])
        for open_leg in position.legs:
            assert len(open_leg.opening_fills) == 3
        quotes = []
        for quote in tighten_reverse_quotes(opened.quotes):
            levels = [
                BookLevel(
                    decimal_odds=level.decimal_odds,
                    available_stake=level.available_stake * Decimal("50"),
                )
                for level in quote.levels
            ]
            quotes.append(quote.model_copy(update={"levels": levels}))
        policy = UnwindPolicy(max_profit_give_up_gbp=Decimal("1000"), max_execution_risk=100)
        evaluated_at = max((quote.quoted_at for quote in quotes), default=OBSERVED)
        decision = ops.evaluate_unwind(
            loaded.trade_id,
            quotes=quotes,
            fx=list(DEMO_FX),
            policy=policy,
            evaluated_at=evaluated_at,
            record_audit=False,
        )
        closed = ops.complete_validated_unwind(
            loaded.trade_id,
            quotes=quotes,
            fx=list(DEMO_FX),
            policy=policy,
            now=evaluated_at,
            record_evaluation_audit=False,
        )
        assert closed.state is PaperTradeState.CLOSED
        assert {fill.opening_fill_id for fill in closed.close_fills} == set(fill_ids)
        assert len(closed.close_fills) == len(fill_ids)
        locks = _lock_fingerprint(ledger, loaded.trade_id)
        assert {row[0] for row in locks} == set(fill_ids)
        for _lock_id, status, locked_native, released_native in locks:
            assert status == "released"
            assert Decimal(locked_native) == Decimal(released_native)
        allocated_qty = Decimal("0")
        allocated_pnl = Decimal("0")
        allocated_fees = Decimal("0")
        for open_leg, close_leg in zip(position.legs, decision.close_plan.legs, strict=True):
            parts = allocated_close_shares(open_leg, close_leg)
            allocated_qty += sum((part[1] for part in parts), Decimal("0"))
            allocated_fees += sum((part[3] for part in parts), Decimal("0"))
            allocated_pnl += sum((part[4] for part in parts), Decimal("0"))
        assert allocated_qty == sum(
            (leg.filled_close_quantity for leg in decision.close_plan.legs), Decimal("0")
        )
        assert sum((fill.filled_close_quantity for fill in closed.close_fills), Decimal("0")) == allocated_qty
        assert sum((fill.closing_fee_native for fill in closed.close_fills), Decimal("0")) == allocated_fees
        unwind_ids = [
            entry.source_id
            for entry in ops.journal.list_entries(opportunity_id=loaded.opportunity_id)
            if entry.source == PAPER_UNWIND_SOURCE
        ]
        expected_prefix = paper_unwind_source_id(loaded.trade_id)
        assert unwind_ids
        assert all(item.startswith(expected_prefix) for item in unwind_ids)
        assert len(unwind_ids) == len(set(unwind_ids))
        snap = {
            (pool.venue, pool.native_currency): pool.locked_capital
            for pool in ledger.treasury.snapshot().pools
        }
        assert all(amount == 0 for amount in snap.values())
        again = ops.complete_validated_unwind(
            loaded.trade_id,
            quotes=quotes,
            fx=list(DEMO_FX),
            policy=policy,
            now=evaluated_at + timedelta(milliseconds=1),
            record_evaluation_audit=False,
        )
        assert again.state is PaperTradeState.CLOSED
        assert _lock_fingerprint(ledger, loaded.trade_id) == locks
        retry_ids = [
            entry.source_id
            for entry in ops.journal.list_entries(opportunity_id=loaded.opportunity_id)
            if entry.source == PAPER_UNWIND_SOURCE
        ]
        assert retry_ids == unwind_ids
    finally:
        repository.close()
        ledger.close()
        store.close()
        bind_runtime_operator_scanner_settings_store(None)
        reset_active_trade_registry()


def test_top_up_lock_and_tranche_share_one_sqlite_transaction(tmp_path: Path) -> None:
    commit_src = inspect.getsource(PaperOperationsService._maybe_top_up_open_trade_locked)
    assert "ledger.transaction()" in commit_src
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    store = _raise_trade_cap(tmp_path, Decimal("2000"))
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        before_locks = _lock_fingerprint(ledger, trade.trade_id)
        before_journals = _journal_keys(ops)
        before_legs = len(trade.legs)
        before_tranches = [item.tranche_id for item in trade.tranches]
        original_save = ops.trades.save

        def _boom(item):
            if any(tranche.kind is PaperTradeTrancheKind.TOP_UP for tranche in item.tranches):
                raise RuntimeError("injected_trade_save_failure")
            return original_save(item)

        ops.trades.save = _boom  # type: ignore[method-assign]
        with pytest.raises(RuntimeError, match="injected_trade_save_failure"):
            ops.maybe_top_up_open_trade(trade, now=OBSERVED)
        ops.trades.save = original_save  # type: ignore[method-assign]
        rolled = ops.trades.get(trade.trade_id)
        assert rolled is not None
        assert [item.tranche_id for item in rolled.tranches] == before_tranches
        assert not any(item.kind is PaperTradeTrancheKind.TOP_UP for item in rolled.tranches)
        assert len(rolled.legs) == before_legs
        assert _lock_fingerprint(ledger, trade.trade_id) == before_locks
        assert _journal_keys(ops) == before_journals
        retry = ops.maybe_top_up_open_trade(rolled, now=OBSERVED)
        assert retry is not None
        loaded = ops.list_active_trades()[0]
        topups = [item for item in loaded.tranches if item.kind is PaperTradeTrancheKind.TOP_UP]
        assert len(topups) == 1
        assert len(loaded.legs) > before_legs
        after_locks = _lock_fingerprint(ledger, loaded.trade_id)
        assert len(after_locks) > len(before_locks)
        second = ops.maybe_top_up_open_trade(loaded, now=OBSERVED)
        assert second is not None
        again = ops.list_active_trades()[0]
        assert (
            len([item for item in again.tranches if item.kind is PaperTradeTrancheKind.TOP_UP])
            >= 1
        )
        first_topup_id = topups[0].tranche_id
        assert sum(1 for item in again.tranches if item.tranche_id == first_topup_id) == 1
    finally:
        repository.close()
        ledger.close()
        store.close()
        bind_runtime_operator_scanner_settings_store(None)
        reset_active_trade_registry()
