"""Issue #362: tiny current System Load summary from in-memory public status.

PAPER / read-only. No live HTTP. Fixture/demo clocks only.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from test_issue344_price_engine import _hold_slot

from sports_hedge.api import main as main_api
from sports_hedge.api import paper as paper_api
from sports_hedge.api.main import app
from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.live_refresh import (
    LaneRefreshStatus,
    LiveRefreshCoordinator,
    LiveRefreshStatus,
    get_live_refresh_coordinator,
)
from sports_hedge.application.provider_access import (
    ProviderAccessLayer,
    reset_shared_provider_access,
    set_shared_provider_access,
)
from sports_hedge.application.scanner_observability import (
    PriceEnginePublicStatus,
    PriceEngineTierStatus,
)
from sports_hedge.application.system_load import (
    SYSTEM_LOAD_JSON_BUDGET_BYTES,
    cadence_utilisation,
    system_load_from_status,
    system_load_payload_bytes,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.persistence.universe_checkpoint import SqliteUniverseCheckpointStore

NOW = datetime(2026, 9, 19, 21, 0, tzinfo=UTC)


def _status(**overrides: Any) -> LiveRefreshStatus:
    payload: dict[str, Any] = {
        "server_loop_enabled": False,
        "interval_seconds": 30,
        "hot": LaneRefreshStatus(
            cadence_seconds=30,
            fixture_count=8,
            last_duration_ms=3800,
        ),
        "universe": LaneRefreshStatus(
            cadence_seconds=600,
            generation_budget_seconds=150,
            generation_work_used_s=42,
            canonical_evaluated=24,
            canonical_work_total=30,
            canonical_remaining=6,
            fixture_count=12,
        ),
        "background": LaneRefreshStatus(cadence_seconds=90),
        "price_engine": PriceEnginePublicStatus(
            hot=PriceEngineTierStatus(
                working_set=18,
                due=4,
                in_flight=1,
                retry_wait=2,
                deferred=0,
            ),
            background=PriceEngineTierStatus(working_set=31),
        ),
        "provider_access": {
            "inflight": {"matchbook": 2, "kalshi": 1, "polymarket": 0},
            "waiting": {"matchbook": 0, "kalshi": 0, "polymarket": 0},
            "limits": {"matchbook": 4, "kalshi": 4, "polymarket": 8},
        },
    }
    payload.update(overrides)
    return LiveRefreshStatus(**payload)


def test_system_load_uses_existing_public_status_fields_only() -> None:
    load = system_load_from_status(_status())
    assert load.hot.fixtures == 8
    assert load.hot.working_set == 18
    assert load.hot.due == 4
    assert load.hot.in_flight == 1
    assert load.hot.retry_wait == 2
    assert load.hot.deferred == 0
    assert load.hot.last_cycle_ms == 3800
    assert load.hot.cadence_seconds == 30
    assert load.hot.cadence_utilisation == pytest.approx(0.1267)
    assert load.matchbook.inflight == 2
    assert load.matchbook.limit == 4
    assert load.matchbook.waiting == 0
    assert load.kalshi.inflight == 1
    assert load.kalshi.limit == 4
    assert load.universe.evaluated == 24
    assert load.universe.total == 30
    assert load.universe.remaining == 6
    assert load.universe.generation_work_used_s == 42
    assert load.universe.generation_budget_seconds == 150
    assert load.universe.cadence_seconds == 600
    assert load.background.cadence_seconds == 90
    assert load.background.working_set == 31
    assert load.catalogue_items == 49
    dumped = json.dumps(load.model_dump(mode="json"))
    assert "p50" not in dumped
    assert "p95" not in dumped
    assert "safe" not in dumped.casefold()
    assert "unsafe" not in dumped.casefold()
    assert "health_score" not in dumped
    assert system_load_payload_bytes(load) < SYSTEM_LOAD_JSON_BUDGET_BYTES


def test_catalogue_items_count_hot_plus_background_not_fixtures() -> None:
    load = system_load_from_status(
        _status(
            hot=LaneRefreshStatus(cadence_seconds=30, fixture_count=1, last_duration_ms=1200),
            price_engine=PriceEnginePublicStatus(
                hot=PriceEngineTierStatus(working_set=3),  # TOTAL 2.5 / 3.5 / 4.5
                background=PriceEngineTierStatus(working_set=0),
            ),
        )
    )
    assert load.hot.fixtures == 1
    assert load.hot.working_set == 3
    assert load.catalogue_items == 3
    assert load.catalogue_items != load.hot.fixtures


def test_hot_cadence_utilisation_handles_missing_and_zero_safely() -> None:
    assert cadence_utilisation(None, 30) is None
    assert cadence_utilisation(3800, None) is None
    assert cadence_utilisation(3800, 0) is None
    assert cadence_utilisation(0, 30) == 0.0
    assert cadence_utilisation(float("nan"), 30) is None
    assert cadence_utilisation(3800, float("inf")) is None
    missing = system_load_from_status(
        _status(hot=LaneRefreshStatus(cadence_seconds=30, fixture_count=1, last_duration_ms=None))
    )
    assert missing.hot.cadence_utilisation is None
    assert missing.hot.last_cycle_ms is None
    zero_cadence = system_load_from_status(
        _status(hot=LaneRefreshStatus(cadence_seconds=0, last_duration_ms=3800))
    )
    assert zero_cadence.hot.cadence_utilisation is None


def test_system_load_module_is_in_memory_projection_only() -> None:
    import sports_hedge.application.system_load as system_load_mod

    source = inspect.getsource(system_load_mod)
    lowered = source.casefold()
    assert "list_events" not in lowered
    assert "get_order_book" not in lowered
    assert "collect_and_scan" not in lowered
    assert "sqlite" not in lowered
    assert "matchbook" in lowered  # venue key only
    assert "httpx" not in lowered


@pytest.mark.asyncio
async def test_provider_limits_inflight_waiting_match_access_snapshot() -> None:
    reset_shared_provider_access()
    access = ProviderAccessLayer(
        {VenueName.MATCHBOOK: 4, VenueName.KALSHI: 4, VenueName.POLYMARKET: 8}
    )
    set_shared_provider_access(access)
    gate = asyncio.Event()
    holders = [
        asyncio.create_task(_hold_slot(access, VenueName.MATCHBOOK, gate)) for _ in range(2)
    ]
    kalshi_holders = [
        asyncio.create_task(_hold_slot(access, VenueName.KALSHI, gate)) for _ in range(4)
    ]
    await asyncio.sleep(0.05)
    queued = asyncio.create_task(_hold_slot(access, VenueName.KALSHI, gate))
    await asyncio.sleep(0.05)
    snap = access.snapshot()
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW)
    load = coordinator.public_status().system_load
    assert load.matchbook.inflight == snap.inflight[VenueName.MATCHBOOK.value]
    assert load.matchbook.limit == snap.limits[VenueName.MATCHBOOK.value]
    assert load.matchbook.waiting == snap.waiting[VenueName.MATCHBOOK.value]
    assert load.kalshi.inflight == snap.inflight[VenueName.KALSHI.value]
    assert load.kalshi.limit == snap.limits[VenueName.KALSHI.value]
    assert load.kalshi.waiting == snap.waiting[VenueName.KALSHI.value]
    assert load.matchbook.inflight == 2
    assert load.matchbook.limit == 4
    assert load.kalshi.inflight == 4
    assert load.kalshi.waiting >= 1
    gate.set()
    await asyncio.gather(*holders, queued, *kalshi_holders)
    reset_shared_provider_access()


@pytest.mark.asyncio
async def test_live_refresh_system_load_does_not_scan_or_call_providers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store = SqliteUniverseCheckpointStore(tmp_path / "ckpt.sqlite")
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW, universe_checkpoint_store=store)
    calls = {"collect_and_scan": 0, "collect_report": 0, "load": 0}

    async def boom_collect(*_args: Any, **_kwargs: Any) -> None:
        calls["collect_and_scan"] += 1
        raise AssertionError("system load must not collect_and_scan")

    async def boom_report(*_args: Any, **_kwargs: Any) -> None:
        calls["collect_report"] += 1
        raise AssertionError("system load must not _collect_report")

    original_load = store.load

    def counting_load() -> Any:
        calls["load"] += 1
        return original_load()

    monkeypatch.setattr(ReadOnlyCrossVenueCollector, "collect_and_scan", boom_collect)
    monkeypatch.setattr(paper_api, "_collect_report", boom_report)
    monkeypatch.setattr(store, "load", counting_load)
    monkeypatch.setattr(main_api, "get_live_refresh_coordinator", lambda: coordinator)
    monkeypatch.setattr(paper_api, "get_live_refresh_coordinator", lambda: coordinator)

    source = inspect.getsource(paper_api.live_refresh_status)
    assert "collect_and_scan" not in source
    assert "list_events" not in source
    assert "get_order_book" not in source

    transport = httpx.ASGITransport(app=main_api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        before = dict(calls)
        status = await client.get("/paper/live-refresh")
        assert status.status_code == 200
        payload = status.json()
        assert "system_load" in payload
        load = payload["system_load"]
        background_items = payload["price_engine"]["background"]["working_set"]
        assert load["catalogue_items"] == load["hot"]["working_set"] + background_items
        assert "p50" not in json.dumps(load)
        assert system_load_payload_bytes(
            system_load_from_status(payload)
        ) < SYSTEM_LOAD_JSON_BUDGET_BYTES
        assert calls == before


def test_live_refresh_status_endpoint_includes_compact_system_load() -> None:
    get_live_refresh_coordinator().reset()
    client = TestClient(app)
    status = client.get("/paper/live-refresh")
    assert status.status_code == 200
    payload = status.json()
    load = payload["system_load"]
    assert set(load) == {
        "active_trade",
        "hot",
        "background",
        "matchbook",
        "kalshi",
        "universe",
        "catalogue_items",
    }
    assert set(load["hot"]) == {
        "fixtures",
        "pricing_fixtures",
        "working_set",
        "due",
        "in_flight",
        "retry_wait",
        "deferred",
        "last_cycle_ms",
        "cadence_seconds",
        "cadence_utilisation",
        "health",
    }
    assert set(load["background"]) == {
        "working_set",
        "pricing_fixtures",
        "due",
        "cadence_seconds",
        "health",
    }
    assert set(load["universe"]) == {
        "evaluated",
        "total",
        "remaining",
        "generation_work_used_s",
        "generation_budget_seconds",
        "cadence_seconds",
        "selected_competition_count",
        "scope_version",
        "generation_scope_version",
        "worker_state",
        "health",
    }
    assert "capital_locked_gbp" in load["active_trade"]
    assert "health" in load["active_trade"]
    assert "discovered_fixtures" not in load
    assert "recent_scan_cycles" not in load
    assert "execution_enabled" not in payload
    dumped = json.dumps(load)
    assert "p50" not in dumped
    assert "p95" not in dumped
    assert system_load_payload_bytes(system_load_from_status(payload)) < SYSTEM_LOAD_JSON_BUDGET_BYTES
