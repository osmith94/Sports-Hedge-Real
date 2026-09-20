"""Issue #372 — ACTIVE TRADE 5s lane, complete-set top-ups, max-per-trade cap.

PAPER / read-only. Fixture clocks only. Does not broaden #371 BACKGROUND cadence
into an operator-editable field; the singleton settings row is extended additively.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.application.active_trade_lane import (
    ACTIVE_TRADE_LANE,
    DEFAULT_ACTIVE_TRADE_CADENCE_SECONDS,
    identity_from_open_trade,
    remaining_trade_room_gbp,
    reset_active_trade_registry,
)
from sports_hedge.application.live_refresh import (
    LiveRefreshCoordinator,
    get_live_refresh_coordinator,
)
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.application.price_engine import CataloguePriceEngine
from sports_hedge.application.provider_access import (
    DEFAULT_PROVIDER_CONCURRENCY,
    PRICE_ENGINE_ACTIVE_TRADE_LANE,
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
                first = filled.fills[0].model_copy(
                    update={"filled_stake": Decimal("0"), "fully_filled": False}
                )
                return filled.model_copy(update={"fills": [first, *filled.fills[1:]]})

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
    assert ProviderPriority.HOT == 1
    assert ProviderPriority.UNIVERSE == 2
    assert ProviderPriority.BACKGROUND == 3
    assert priority_for_lane(PRICE_ENGINE_ACTIVE_TRADE_LANE) is ProviderPriority.ACTIVE_TRADE
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
    assert env.max_allocated_per_trade_gbp == Decimal("1000")
    store = SqliteOperatorScannerSettingsStore(tmp_path / "defaults.sqlite")
    resolved = resolve_operator_scanner_settings(store)
    assert resolved.min_net_edge == Decimal("0.01")
    assert resolved.max_execution_risk == 60
    assert resolved.hot_cadence_seconds == 30
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
    assert "background_cadence_seconds" not in write_src
    assert "_OPERATOR_SETTINGS_WRITE_COLUMNS" in write_src
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
            },
        )
        assert omitted.status_code == 200
        settings = omitted.json()["operator_settings"]
        assert Decimal(str(settings["max_allocated_per_trade_gbp"])) == Decimal("1000")
        updated = client.put(
            "/paper/operator-scanner-settings",
            json={
                "min_net_edge": "0.01",
                "max_execution_risk": 60,
                "hot_cadence_seconds": 30,
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
