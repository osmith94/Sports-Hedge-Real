"""Issue #550: HOT/BACKGROUND scan interval is independent of reprice age.

UNIVERSE keeps one discovery-refresh variable. PAPER / read-only.
Deterministic clocks and fixture catalogue rows. Not live quotes.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_issue344_price_engine import NEAR_KICKOFF, NOW, _engine, _hda_row

from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.price_engine import PriceEnginePriority
from sports_hedge.application.provider_access import DEFAULT_PROVIDER_CONCURRENCY
from sports_hedge.application.scan_lanes import (
    DEFAULT_BACKGROUND_SCAN_INTERVAL_SECONDS,
    DEFAULT_HOT_SCAN_INTERVAL_SECONDS,
    DEFAULT_UNIVERSE_DISCOVERY_INTERVAL_SECONDS,
)
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.persistence.operator_scanner_settings import (
    DEFAULT_BACKGROUND_REPRICE_AFTER_SECONDS,
    DEFAULT_HOT_REPRICE_AFTER_SECONDS,
    DEFAULT_HOT_SCAN_INTERVAL_SECONDS as SETTINGS_HOT_SCAN,
    DEFAULT_UNIVERSE_DISCOVERY_REFRESH_SECONDS,
    SqliteOperatorScannerSettingsStore,
    env_operator_scanner_settings,
)
from test_dual_cadence_scheduler import FakeClock


def test_defaults_split_scan_interval_from_reprice_and_discovery_refresh() -> None:
    settings = Settings()
    assert settings.paper_hot_scan_interval_seconds == 10
    assert settings.paper_background_scan_interval_seconds == 10
    assert settings.paper_live_refresh_hot_interval_seconds == DEFAULT_HOT_REPRICE_AFTER_SECONDS == 30
    assert settings.paper_background_price_interval_seconds == DEFAULT_BACKGROUND_REPRICE_AFTER_SECONDS == 600
    assert (
        settings.paper_universe_discovery_interval_seconds
        == DEFAULT_UNIVERSE_DISCOVERY_REFRESH_SECONDS
        == DEFAULT_UNIVERSE_DISCOVERY_INTERVAL_SECONDS
        == 3600
    )
    assert settings.paper_active_trade_interval_seconds == 5
    assert settings.paper_universe_worker_cooldown_seconds == 8
    assert DEFAULT_HOT_SCAN_INTERVAL_SECONDS == SETTINGS_HOT_SCAN == 10
    assert DEFAULT_BACKGROUND_SCAN_INTERVAL_SECONDS == 10
    resolved = env_operator_scanner_settings(settings)
    assert resolved.hot_scan_interval_seconds == 10
    assert resolved.hot_reprice_after_seconds == 30
    assert resolved.hot_cadence_seconds == 30
    assert resolved.background_scan_interval_seconds == 10
    assert resolved.background_reprice_after_seconds == 600
    assert resolved.background_cadence_seconds == 600
    assert resolved.universe_discovery_refresh_seconds == 3600
    assert resolved.universe_cadence_seconds == 3600
    assert settings.paper_scan_matchbook_concurrency == 4
    assert settings.paper_scan_kalshi_concurrency == 4
    assert settings.paper_scan_polymarket_concurrency == 8
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.MATCHBOOK] == 4
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.KALSHI] == 4
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.POLYMARKET] == 8


def test_legacy_cadence_columns_migrate_to_reprice_and_discovery_refresh(tmp_path: Path) -> None:
    db = tmp_path / "legacy-cadence.sqlite"
    connection = sqlite3.connect(db)
    connection.execute(
        """
        CREATE TABLE operator_scanner_settings (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            min_net_edge TEXT NOT NULL,
            max_execution_risk INTEGER NOT NULL,
            hot_cadence_seconds INTEGER NOT NULL,
            background_cadence_seconds INTEGER NOT NULL,
            universe_cadence_seconds INTEGER NOT NULL,
            scanner_stopped INTEGER NOT NULL,
            source TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO operator_scanner_settings (
            id, min_net_edge, max_execution_risk, hot_cadence_seconds,
            background_cadence_seconds, universe_cadence_seconds,
            scanner_stopped, source, updated_at
        ) VALUES (1, '0.01', 60, 20, 90, 1800, 0, 'operator', ?)
        """,
        (datetime.now(UTC).isoformat(),),
    )
    connection.commit()
    connection.close()
    store = SqliteOperatorScannerSettingsStore(db)
    loaded = store.load()
    assert loaded is not None
    assert loaded.hot_reprice_after_seconds == 20
    assert loaded.hot_cadence_seconds == 20
    assert loaded.hot_scan_interval_seconds == 10
    assert loaded.background_reprice_after_seconds == 90
    assert loaded.background_cadence_seconds == 90
    assert loaded.background_scan_interval_seconds == 10
    assert loaded.universe_discovery_refresh_seconds == 1800
    assert loaded.universe_cadence_seconds == 1800
    store.close()


def test_background_reprice_age_does_not_skip_recent_rows() -> None:
    """BACKGROUND coverage is a cursor, not a reprice-after age gate."""

    clock = FakeClock(NOW)
    engine, _mb, _ks, _layer = _engine(
        [_hda_row("aged"), _hda_row("fresh")],
        clock=clock,
        hot_interval=30,
        background_interval=600,
    )
    aged = engine.item("amc-aged")
    fresh = engine.item("amc-fresh")
    assert aged is not None and fresh is not None
    aged.last_priced_at = None
    fresh.last_priced_at = NOW
    claimed = engine.due_items(PriceEnginePriority.BACKGROUND, now=NOW + timedelta(seconds=1))
    ids = [item.identity.catalogue_row_id for item in claimed]
    assert ids == ["amc-aged", "amc-fresh"]
    cursor = engine.coverage_cursor(PriceEnginePriority.BACKGROUND)
    assert cursor.cursor_after_id == "amc-fresh"


def test_hot_reprice_age_is_independent_of_worker_scans() -> None:
    clock = FakeClock(NOW)
    engine, _mb, _ks, _layer = _engine(
        [
            _hda_row("hot-new", kickoff=NEAR_KICKOFF),
            _hda_row("hot-priced", kickoff=NEAR_KICKOFF),
        ],
        clock=clock,
        hot_interval=30,
        background_interval=600,
    )
    never = engine.item("amc-hot-new")
    priced = engine.item("amc-hot-priced")
    assert never is not None and priced is not None
    never.last_priced_at = None
    priced.last_priced_at = NOW
    early = engine.due_items(PriceEnginePriority.HOT, now=NOW + timedelta(seconds=1))
    early_ids = [item.identity.catalogue_row_id for item in early]
    assert early_ids == ["amc-hot-new", "amc-hot-priced"]
    cursor = engine.coverage_cursor(PriceEnginePriority.HOT)
    assert cursor.hold_until is not None


@pytest.mark.asyncio
async def test_background_slice_schedules_next_scan_not_reprice_age() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator.configure_from_settings()
    coordinator._clock = clock
    assert coordinator.status.background.scan_interval_seconds == 10
    assert coordinator.status.background.reprice_after_seconds == 600
    await coordinator.run_price_engine_slice(PriceEnginePriority.BACKGROUND)
    assert coordinator._next_background_due == NOW
    due = coordinator.plan_background_tick(now=NOW)
    assert due.lane == "background"


def test_hot_target_refresh_reschedules_without_calling_providers(tmp_path: Path) -> None:
    clock = FakeClock(NOW)
    store = SqliteOperatorScannerSettingsStore(tmp_path / "hot-scan.sqlite")
    coordinator = LiveRefreshCoordinator(clock=clock, operator_settings_store=store)
    coordinator.configure_from_settings()
    coordinator._clock = clock
    coordinator._next_hot_due = NOW
    coordinator._next_background_due = NOW + timedelta(seconds=3)
    legacy = coordinator.apply_operator_scan_settings(
        min_net_edge=Decimal("0.01"),
        max_execution_risk=60,
        hot_scan_interval_seconds=15,
        hot_reprice_after_seconds=30,
        background_scan_interval_seconds=20,
        background_reprice_after_seconds=90,
        universe_discovery_refresh_seconds=3600,
    )
    assert legacy.hot_scan_interval_seconds == 15
    assert coordinator._next_hot_due == NOW
    assert coordinator._next_background_due == NOW + timedelta(seconds=3)
    saved = coordinator.apply_operator_scan_settings(
        min_net_edge=Decimal("0.01"),
        max_execution_risk=60,
        hot_target_refresh_seconds=20,
        universe_discovery_refresh_seconds=3600,
    )
    assert saved.hot_target_refresh_seconds == 20
    assert coordinator._next_hot_due == NOW + timedelta(seconds=20)
    assert coordinator.plan_hot_tick(now=NOW).reason == "waiting"
    assert coordinator.status.hot.cadence_seconds == 20
    assert coordinator.status.hot.target_refresh_seconds == 20
    assert coordinator.status.interval_seconds == 20
    assert "next scan" not in (coordinator.status.hot.operator_summary or "")
    store.close()


def test_terminal_complete_universe_uses_discovery_refresh_not_incomplete_cooldown() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator.configure_from_settings()
    coordinator._clock = clock
    coordinator._universe_generation_id = 4
    coordinator._universe_generation_started_at = NOW
    finished = NOW + timedelta(seconds=3)
    coordinator._close_universe_generation(finished)
    assert coordinator._next_universe_due == finished + timedelta(seconds=3600)
    assert coordinator.status.universe.discovery_refresh_seconds == 3600
    coordinator._universe_generation_id = 5
    coordinator._universe_generation_started_at = finished
    coordinator._pause_universe_generation(finished)
    assert coordinator._next_universe_due == finished + timedelta(seconds=8)
