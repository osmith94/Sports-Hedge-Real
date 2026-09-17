"""Combined Wave A overlap regressions.

These tests exist because #175/#177/#178 all touch paper.py, collector.py,
live_refresh.py, fixture_current_state.py and frontend status/API. A clean
cherry-pick is not proof that accepted semantics still hold together.

Accepted heads composed here:
- #176 latest-100 append-only audit is not radar
- #175 HOT 25s / <=30s envelope, persist-outside-envelope, retry idempotency
- #177 provenance-specific lifecycle including Matchbook graded
- #178 independent HOT/UNIVERSE venue controls, fail-closed all-off, no
  auto-capture on disabled/stale venues
"""

from __future__ import annotations

import inspect
from datetime import timedelta
from pathlib import Path

from sports_hedge.api import paper as paper_api
from sports_hedge.application.collector import CollectionReport, ReadOnlyCrossVenueCollector
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.live_refresh import (
    LiveRefreshCoordinator,
    SCAN_CYCLE_RETURN_GRACE_SECONDS,
)
from sports_hedge.application.scan_lanes import (
    EVICTION_TERMINAL_FROM_MATCHBOOK,
    ScanLane,
)
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.trades import PaperTradeState
from sports_hedge.persistence.lane_venue_settings import SqliteLaneVenueSettingsStore
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from test_fixture_lifecycle_eviction import NOW, _fixture, _report
from test_hot_scan_reliability import _hot_leftover_report, _ops_bundle
from test_paper_audit_repository import SCANNED, make_record
from test_step8f_automatic_paper_entry import (
    FX as AUTOFILL_FX,
    _matchbook_btts,
    _polymarket_btts,
    _standing,
)
from venue_cost_helpers import matchbook_polymarket_costs


def test_one_coordinator_owns_one_current_state_store() -> None:
    coordinator = LiveRefreshCoordinator()
    first = coordinator.fixture_current_state()
    second = coordinator.fixture_current_state()
    assert first is second
    assert isinstance(first, FixtureCurrentStateStore)
    assert coordinator.fixture_current_state() is coordinator._fixture_state


def test_hot_envelope_and_venue_participation_coexist() -> None:
    settings = Settings()
    assert settings.paper_scan_hot_cycle_timeout_seconds == 25
    assert (
        settings.paper_scan_hot_cycle_timeout_seconds + SCAN_CYCLE_RETURN_GRACE_SECONDS
        == 30
    )
    tick_src = inspect.getsource(paper_api.server_owned_refresh_tick)
    collect_src = inspect.getsource(paper_api._collect_report)
    persist_src = inspect.getsource(paper_api._persist_collection_report)
    execute_src = inspect.getsource(paper_api._execute_collection)
    assert "_collect_report(" in tick_src
    assert tick_src.index("persist_scheduled_collection_report") > tick_src.index(
        "run_cycle"
    )
    assert "enabled_venues=list(resolved.enabled_venues)" in tick_src
    assert "_persist_decision" not in collect_src
    assert "refreshed_venues=report.enabled_venues" in persist_src
    assert "enabled_venues=enabled_venues" in execute_src
    assert "acknowledge_task_cancellation" in collect_src
    assert "should_skip_market_work" in inspect.getsource(
        ReadOnlyCrossVenueCollector._scan_cluster
    )


def test_public_status_keeps_venues_and_evicts_matchbook_graded(tmp_path: Path) -> None:
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW)
    coordinator.reset()
    coordinator.bind_venue_store(SqliteLaneVenueSettingsStore(tmp_path / "venues.sqlite"))
    coordinator.apply_venue_participation(
        [VenueName.MATCHBOOK, VenueName.KALSHI],
        [VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI],
    )
    coordinator.record_report(
        _report(
            [
                _fixture(
                    "leeds-newcastle",
                    kickoff=NOW - timedelta(hours=2),
                    fixture_status="graded",
                    fixture_status_source=VenueName.MATCHBOOK,
                    in_running=False,
                )
            ],
            scan_lane=ScanLane.HOT.value,
        ),
        scan_lane=ScanLane.HOT,
    )
    status = coordinator.public_status()
    assert status.venue_participation is not None
    assert status.hot.pending_venues == [VenueName.MATCHBOOK, VenueName.KALSHI]
    assert status.universe.pending_venues == [
        VenueName.MATCHBOOK,
        VenueName.POLYMARKET,
        VenueName.KALSHI,
    ]
    assert status.discovered_fixtures == []
    assert coordinator.fixture_current_state().tombstone_for("leeds-newcastle") is not None
    tombstone = coordinator.fixture_current_state().tombstone_for("leeds-newcastle")
    assert tombstone is not None
    assert tombstone.reason == EVICTION_TERMINAL_FROM_MATCHBOOK
    assert tombstone.provider_status == "graded"


def test_disabled_polymarket_cycle_cannot_clear_matchbook_graded_tombstone() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(
        _report(
            [
                _fixture(
                    "qpr-boro",
                    kickoff=NOW - timedelta(hours=1),
                    fixture_status="graded",
                    fixture_status_source=VenueName.MATCHBOOK,
                )
            ]
        ),
        scan_lane=ScanLane.HOT,
        now=NOW,
    )
    later = NOW + timedelta(minutes=2)
    store.upsert_from_report(
        CollectionReport(
            started_at=later,
            completed_at=later,
            discovered_fixtures=[
                _fixture(
                    "qpr-boro",
                    kickoff=NOW - timedelta(hours=1),
                    fixture_status="unknown",
                    fixture_status_source=VenueName.POLYMARKET,
                    source=VenueName.POLYMARKET,
                    matchbook_matched=False,
                    polymarket_matched=True,
                )
            ],
            enabled_venues=[VenueName.POLYMARKET],
            scan_lane=ScanLane.HOT.value,
            fixture_identity_aliases={"qpr-boro": "qpr-boro"},
            fixture_source_events={
                "qpr-boro": [
                    {
                        "venue": VenueName.POLYMARKET.value,
                        "source_event_id": "src-qpr-boro",
                        "raw": {"id": "src-qpr-boro", "status": "unknown"},
                    }
                ]
            },
        ),
        scan_lane=ScanLane.HOT,
        now=later,
    )
    assert store.inventory(later) == []
    remaining = store.tombstone_for("qpr-boro")
    assert remaining is not None
    assert remaining.reason == EVICTION_TERMINAL_FROM_MATCHBOOK
    assert remaining.provider_status == "graded"


async def test_scheduled_persist_retry_respects_disabled_venues_and_stays_idempotent(
    tmp_path: Path,
    monkeypatch,
) -> None:
    scan, watchlist, ops, _repository, ledger = _ops_bundle(tmp_path, autofill=True)
    audit = SqlitePaperScanRepository(tmp_path / "paper-audit.sqlite")
    try:
        decision = scan.scan_pair(
            _matchbook_btts(),
            _polymarket_btts(),
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=AUTOFILL_FX,
            maximum_execution_risk=100,
            liquidity_snapshot=_standing(),
        )
        assert decision.eligible_for_paper_simulation is True, decision.rejection_reasons
        assert any(leg.venue is VenueName.POLYMARKET for leg in decision.fill_legs)

        coordinator = LiveRefreshCoordinator()
        coordinator.reset()
        disabled_pm = _hot_leftover_report(cancelled=False).model_copy(
            update={
                "paper_decisions": [decision],
                "enabled_venues": [VenueName.MATCHBOOK, VenueName.KALSHI],
            }
        )

        def operations_factory(watchlist_arg=None, alerts=None):
            del alerts
            if watchlist_arg is not None:
                ops.watchlist = watchlist_arg
            return ops

        monkeypatch.setattr(paper_api, "get_paper_operations_service", operations_factory)
        await paper_api.persist_scheduled_collection_report(
            coordinator,
            disabled_pm,
            service=scan,
            audit=audit,
            watchlist=watchlist,
            scan_lane=ScanLane.HOT,
        )
        assert ops.list_active_trades() == []
        assert coordinator.status.hot.last_error is None
        assert coordinator.status.hot.persist_ok is True

        enabled = disabled_pm.model_copy(
            update={
                "enabled_venues": [
                    VenueName.MATCHBOOK,
                    VenueName.POLYMARKET,
                    VenueName.KALSHI,
                ]
            }
        )
        await paper_api.persist_scheduled_collection_report(
            coordinator,
            enabled,
            service=scan,
            audit=audit,
            watchlist=watchlist,
            scan_lane=ScanLane.HOT,
        )
        opened = ops.list_active_trades()
        assert len(opened) == 1
        trade = opened[0]
        assert trade.state is PaperTradeState.OPEN
        assert trade.paper_only is True
        assert trade.places_orders is False
        await paper_api.persist_scheduled_collection_report(
            coordinator,
            enabled,
            service=scan,
            audit=audit,
            watchlist=watchlist,
            scan_lane=ScanLane.HOT,
        )
        retried = ops.list_active_trades()
        assert len(retried) == 1
        assert retried[0].trade_id == trade.trade_id
        assert retried[0].state is PaperTradeState.OPEN
    finally:
        ledger.close()


def test_latest_n_audit_survives_graded_eviction_and_all_off(tmp_path: Path) -> None:
    audit_path = tmp_path / "paper-audit.sqlite"
    repository = SqlitePaperScanRepository(audit_path)
    marker = make_record(
        record_id="wave-a-overlap-audit-marker",
        canonical_event_id="leeds-newcastle",
        canonical_market_id="mkt-leeds-newcastle",
        scanned_at=SCANNED,
    )
    repository.append_scan(marker)
    for offset in range(1, 6):
        repository.append_scan(
            make_record(
                record_id=f"newer-{offset}",
                canonical_event_id=f"evt-newer-{offset}",
                canonical_market_id=f"mkt-newer-{offset}",
                scanned_at=SCANNED + timedelta(seconds=offset),
            )
        )
    window = repository.list_scans(limit=5)
    assert marker.record_id not in [row.record_id for row in window]
    assert marker.record_id in [row.record_id for row in repository.list_scans(limit=20)]

    coordinator = LiveRefreshCoordinator(clock=lambda: NOW)
    coordinator.reset()
    coordinator.bind_venue_store(SqliteLaneVenueSettingsStore(tmp_path / "venues-all-off.sqlite"))
    coordinator.apply_venue_participation([], [])
    coordinator.record_report(
        _report(
            [
                _fixture(
                    "leeds-newcastle",
                    kickoff=NOW - timedelta(hours=4),
                    fixture_status="graded",
                    fixture_status_source=VenueName.MATCHBOOK,
                )
            ]
        ),
        scan_lane=ScanLane.UNIVERSE,
    )
    status = coordinator.public_status()
    assert status.discovered_fixtures == []
    assert status.hot.active_venues == []
    assert status.universe.active_venues == []
    assert status.matching_venue is None or "leeds-newcastle" not in [
        item.canonical_event_id for item in status.discovered_fixtures
    ]
    still = repository.list_scans(limit=20)
    assert marker.record_id in [row.record_id for row in still]


def test_paper_only_boundary_not_weakened_by_combined_overlap() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    collector_src = inspect.getsource(ReadOnlyCrossVenueCollector)
    for banned in ("place_order", "cancel_order", "sign_order", "submit_order"):
        assert banned not in collector_src
    paper_src = inspect.getsource(paper_api)
    assert "PAPER-ONLY" in paper_src
    assert "enabled_venues" in paper_src
    assert "public_status" in paper_src
    assert "Newest-first paper scan *audit* window" in paper_src
