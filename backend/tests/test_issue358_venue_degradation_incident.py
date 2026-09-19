"""Issue #358: bounded Why? incident capture for degraded first-class venues.

Read-only. Fixture/demo clocks. Never calls Matchbook/Kalshi, never starts
HOT/BACKGROUND/UNIVERSE work, never hits /venues/health.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from sports_hedge.api import main as main_api
from sports_hedge.api import paper as paper_api
from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.live_refresh import LiveRefreshCoordinator, LiveRefreshStatus
from sports_hedge.application.provider_access import (
    HEALTH_AUTH_FAILURE,
    HEALTH_CAPACITY_SATURATED,
    HEALTH_DEFERRED,
    HEALTH_DISCOVERY_TIMEOUT,
    HEALTH_MARKET_TIMEOUT,
    HEALTH_WAITING,
    ProviderAccessSnapshot,
    get_shared_provider_access,
    reset_shared_provider_access,
)
from sports_hedge.application.scanner_observability import (
    PriceEnginePublicStatus,
    PriceEngineTierStatus,
)
from sports_hedge.application.serving_build import ServingBuildInfo
from sports_hedge.application.venue_degradation_incident import (
    MAX_STORED_INCIDENTS,
    VenueDegradationIncidentStore,
    is_ui_degraded_health,
)
from sports_hedge.paper.audit import PaperScanCycleRecord
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient

NOW = datetime(2026, 9, 19, 20, 0, tzinfo=UTC)
BUILD = ServingBuildInfo("abc123def", "cursor/degraded-venue-why-incident-2a2e", None, "env")


class _Clock:
    def __init__(self, moment: datetime = NOW) -> None:
        self.now = moment

    def __call__(self) -> datetime:
        return self.now


class _CycleRepo:
    def __init__(self, cycles: list[PaperScanCycleRecord] | None = None) -> None:
        self.cycles = list(cycles or [])

    def list_cycles(self, limit: int = 100) -> list[PaperScanCycleRecord]:
        return self.cycles[:limit]


@pytest.fixture(autouse=True)
def _reset_provider_layer() -> None:
    reset_shared_provider_access()
    yield
    reset_shared_provider_access()


def _cycle(index: int, *, lane: str = "hot", health: str = "ok") -> PaperScanCycleRecord:
    started = NOW - timedelta(minutes=index + 1)
    return PaperScanCycleRecord(
        cycle_id=f"cycle-{index}",
        started_at=started,
        completed_at=started + timedelta(seconds=8),
        scan_lane=lane,
        duration_ms=8000,
        fixture_count=4,
        evaluated_count=3,
        not_evaluated_count=1,
        matched_event_pairs=1,
        matched_market_pairs=1,
        paper_decision_count=0,
        qualifying_arb_count=0,
        venue_health={"matchbook": health, "polymarket": "ok", "kalshi": "ok"},
        degraded=health != "ok",
        last_error=None if health == "ok" else f"{health} after 8s",
        operator_summary=f"{lane} {health}",
    )


def _status(**updates: Any) -> LiveRefreshStatus:
    base = LiveRefreshStatus(server_loop_enabled=False, interval_seconds=30)
    return base.model_copy(update=updates)


def _paint(
    coordinator: LiveRefreshCoordinator,
    *,
    hot: dict[str, Any] | None = None,
    universe: dict[str, Any] | None = None,
    background: dict[str, Any] | None = None,
    price_engine: PriceEnginePublicStatus | None = None,
) -> None:
    update: dict[str, Any] = {}
    if hot is not None:
        update["hot"] = coordinator.status.hot.model_copy(update=hot)
    if universe is not None:
        update["universe"] = coordinator.status.universe.model_copy(update=universe)
    if background is not None:
        update["background"] = coordinator.status.background.model_copy(update=background)
    if price_engine is not None:
        coordinator._price_engine = None
        update["price_engine"] = price_engine
    coordinator.status = coordinator.status.model_copy(update=update)


def _healthy_lanes() -> dict[str, dict[str, Any]]:
    ok = {"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"}
    return {
        "hot": {"venue_health": dict(ok), "operation_health": {"matchbook": {"order_book": "ok"}}},
        "universe": {
            "venue_health": dict(ok),
            "operation_health": {"matchbook": {"list_events": "ok"}},
            "canonical_retryable": 0,
            "series_retryable": 0,
            "worker_state": "idle",
        },
        "background": {
            "venue_health": dict(ok),
            "operation_health": {"matchbook": {"order_book": "ok"}},
        },
    }


def test_ok_to_degraded_captures_once() -> None:
    store = VenueDegradationIncidentStore()
    healthy = _status(venue_health={"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"})
    degraded = _status(
        venue_health={"matchbook": "degraded", "polymarket": "ok", "kalshi": "ok"},
        hot=_status().hot.model_copy(
            update={
                "venue_health": {"matchbook": "ok"},
                "operation_health": {"matchbook": {"order_book": "ok"}},
                "last_error": None,
            }
        ),
        universe=_status().universe.model_copy(
            update={
                "venue_health": {"matchbook": HEALTH_DISCOVERY_TIMEOUT},
                "operation_health": {"matchbook": {"list_events": HEALTH_DISCOVERY_TIMEOUT}},
                "last_error": "list_events discovery_timeout",
            }
        ),
        recent_scan_cycles=[_cycle(i, health="degraded" if i == 0 else "ok") for i in range(12)],
    )
    assert store.observe(healthy, captured_at=NOW, build=BUILD) == {}
    first = store.observe(degraded, captured_at=NOW + timedelta(seconds=5), build=BUILD)
    assert set(first) == {"matchbook"}
    incident = first["matchbook"]
    assert incident["captured_at"] == "2026-09-19T20:00:05Z"
    assert incident["build"]["git_sha"] == "abc123def"
    assert incident["transition"]["previous_health"] == "ok"
    assert incident["transition"]["new_health"] == "degraded"
    assert len(incident["recent_scan_cycles"]) == 10
    assert "discovered_fixtures" not in incident
    mutated = degraded.model_copy(
        update={
            "universe": degraded.universe.model_copy(update={"last_error": "later noise"}),
        }
    )
    second = store.observe(mutated, captured_at=NOW + timedelta(seconds=10), build=BUILD)
    assert second["matchbook"]["captured_at"] == "2026-09-19T20:00:05Z"
    assert second["matchbook"]["universe"]["last_error"] == "list_events discovery_timeout"
    assert store.retained_count == 1


def test_recovery_then_new_degradation_captures_again() -> None:
    store = VenueDegradationIncidentStore()
    healthy = _status(venue_health={"matchbook": "ok", "kalshi": "ok", "polymarket": "ok"})
    degraded = _status(venue_health={"matchbook": "degraded", "kalshi": "ok", "polymarket": "ok"})
    store.observe(healthy, captured_at=NOW, build=BUILD)
    first = store.observe(degraded, captured_at=NOW + timedelta(seconds=1), build=BUILD)
    recovered = store.observe(healthy, captured_at=NOW + timedelta(seconds=2), build=BUILD)
    assert recovered == {}
    second = store.observe(degraded, captured_at=NOW + timedelta(seconds=3), build=BUILD)
    assert first["matchbook"]["captured_at"] != second["matchbook"]["captured_at"]
    assert store.retained_count == 2


def test_hot_ok_universe_discovery_timeout_is_not_hot_market_timeout() -> None:
    store = VenueDegradationIncidentStore()
    mixed = _status(
        venue_health={"matchbook": "degraded"},
        hot=_status().hot.model_copy(
            update={
                "venue_health": {"matchbook": "ok"},
                "operation_health": {"matchbook": {"order_book": "ok"}},
            }
        ),
        universe=_status().universe.model_copy(
            update={
                "venue_health": {"matchbook": HEALTH_DISCOVERY_TIMEOUT},
                "operation_health": {"matchbook": {"list_events": HEALTH_DISCOVERY_TIMEOUT}},
            }
        ),
    )
    hot_timeout = _status(
        venue_health={"matchbook": HEALTH_MARKET_TIMEOUT},
        hot=_status().hot.model_copy(
            update={
                "venue_health": {"matchbook": HEALTH_MARKET_TIMEOUT},
                "operation_health": {"matchbook": {"order_book": HEALTH_MARKET_TIMEOUT}},
                "last_error": "order_book market_timeout",
            }
        ),
        universe=_status().universe.model_copy(
            update={
                "venue_health": {"matchbook": "ok"},
                "operation_health": {"matchbook": {"list_events": "ok"}},
            }
        ),
    )
    mixed_incident = store.observe(mixed, captured_at=NOW, build=BUILD)["matchbook"]
    store.reset()
    hot_incident = store.observe(hot_timeout, captured_at=NOW, build=BUILD)["matchbook"]
    assert mixed_incident["classification"]["hot_ok"] is True
    assert mixed_incident["classification"]["universe_discovery_timeout"] is True
    assert mixed_incident["classification"]["hot_market_timeout"] is False
    assert mixed_incident["classification"]["mixed_lane_degraded"] is True
    assert hot_incident["classification"]["hot_ok"] is False
    assert hot_incident["classification"]["hot_market_timeout"] is True
    assert hot_incident["classification"]["universe_discovery_timeout"] is False


def test_capacity_waiting_deferred_remain_local_backpressure() -> None:
    store = VenueDegradationIncidentStore()
    status = _status(
        venue_health={"matchbook": "degraded"},
        hot=_status().hot.model_copy(update={"venue_health": {"matchbook": "ok"}}),
        universe=_status().universe.model_copy(
            update={
                "venue_health": {"matchbook": HEALTH_DISCOVERY_TIMEOUT},
                "operation_health": {"matchbook": {"list_events": HEALTH_DISCOVERY_TIMEOUT}},
                "canonical_retryable": 2,
                "series_retryable": 1,
                "worker_state": "waiting",
            }
        ),
        background=_status().background.model_copy(
            update={
                "venue_health": {"matchbook": HEALTH_DEFERRED},
                "operation_health": {"matchbook": {"order_book": HEALTH_CAPACITY_SATURATED}},
            }
        ),
        price_engine=PriceEnginePublicStatus(
            hot=PriceEngineTierStatus(working_set=3, venue_health={"matchbook": "ok"}),
            background=PriceEngineTierStatus(
                working_set=9,
                deferred=4,
                provider_capacity_saturated=4,
                retry_wait=2,
                operation_health={"matchbook": {"order_book": HEALTH_CAPACITY_SATURATED}},
            ),
        ),
        provider_access={
            "inflight": {"matchbook": 4},
            "waiting": {"matchbook": 3},
            "waiting_by_lane": {
                "hot": {"matchbook": 0},
                "universe": {"matchbook": 1},
                "background": {"matchbook": 2},
            },
            "limits": {"matchbook": 4},
        },
    )
    incident = store.observe(status, captured_at=NOW, build=BUILD)["matchbook"]
    assert incident["classification"]["local_backpressure"] is True
    assert incident["classification"]["universe_discovery_timeout"] is True
    assert incident["background"]["operation_health"]["matchbook"]["order_book"] == HEALTH_CAPACITY_SATURATED
    assert incident["provider_access"]["waiting"]["matchbook"] == 3
    assert incident["provider_access"]["waiting_by_lane"]["background"]["matchbook"] == 2
    assert incident["price_engine"]["background"]["provider_capacity_saturated"] == 4
    assert incident["active_catalogue_count"] == 12
    assert incident["universe"]["canonical_retryable"] == 2
    assert incident["universe"]["series_retryable"] == 1


def test_auth_unavailable_and_retry_wait_painting_are_identifiable() -> None:
    store = VenueDegradationIncidentStore()
    auth = _status(
        venue_health={"kalshi": HEALTH_AUTH_FAILURE},
        hot=_status().hot.model_copy(
            update={
                "venue_health": {"kalshi": HEALTH_AUTH_FAILURE},
                "operation_health": {"kalshi": {"order_book": HEALTH_AUTH_FAILURE}},
            }
        ),
    )
    retry = _status(
        venue_health={"matchbook": "retry_wait"},
        universe=_status().universe.model_copy(
            update={"venue_health": {"matchbook": "retry_wait"}, "canonical_retryable": 5}
        ),
    )
    auth_incident = store.observe(auth, captured_at=NOW, build=BUILD)["kalshi"]
    store.reset()
    retry_incident = store.observe(retry, captured_at=NOW, build=BUILD)["matchbook"]
    assert auth_incident["classification"]["auth_or_unavailable"] is True
    assert retry_incident["classification"]["retry_wait_painting"] is True
    assert is_ui_degraded_health("retry_wait")
    assert not is_ui_degraded_health("waiting")
    assert not is_ui_degraded_health(HEALTH_WAITING)
    assert not is_ui_degraded_health("disabled")


def test_incident_storage_is_bounded() -> None:
    store = VenueDegradationIncidentStore(max_incidents=MAX_STORED_INCIDENTS)
    healthy = _status(venue_health={"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"})
    degraded = _status(venue_health={"matchbook": "degraded", "polymarket": "ok", "kalshi": "ok"})
    for index in range(12):
        store.observe(healthy, captured_at=NOW + timedelta(seconds=index * 2), build=BUILD)
        store.observe(
            degraded,
            captured_at=NOW + timedelta(seconds=index * 2 + 1),
            build=BUILD,
        )
    assert store.retained_count == MAX_STORED_INCIDENTS
    assert len(store.observe(degraded, captured_at=NOW, build=BUILD)) == 1


def _install_coordinator(monkeypatch: pytest.MonkeyPatch, coordinator: LiveRefreshCoordinator) -> None:
    monkeypatch.setattr(main_api, "get_live_refresh_coordinator", lambda: coordinator)
    monkeypatch.setattr(paper_api, "get_live_refresh_coordinator", lambda: coordinator)


def test_live_refresh_poll_captures_transition_without_provider_or_scan_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = _Clock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    _install_coordinator(monkeypatch, coordinator)
    calls = {"provider": 0, "scan": 0, "health": 0}

    async def boom_provider(*_args: Any, **_kwargs: Any) -> None:
        calls["provider"] += 1
        raise AssertionError("Why? / live-refresh must not call providers")

    async def boom_scan(*_args: Any, **_kwargs: Any) -> None:
        calls["scan"] += 1
        raise AssertionError("Why? / live-refresh must not start scan work")

    async def boom_health(*_args: Any, **_kwargs: Any) -> None:
        calls["health"] += 1
        raise AssertionError("Why? / live-refresh must not call /venues/health clients")

    monkeypatch.setattr(MatchbookClient, "list_events", boom_provider)
    monkeypatch.setattr(MatchbookClient, "list_markets", boom_provider)
    monkeypatch.setattr(MatchbookClient, "get_market", boom_provider)
    monkeypatch.setattr(MatchbookClient, "get_order_book", boom_provider)
    monkeypatch.setattr(MatchbookClient, "health", boom_health)
    monkeypatch.setattr(KalshiClient, "list_events", boom_provider)
    monkeypatch.setattr(KalshiClient, "list_markets", boom_provider)
    monkeypatch.setattr(KalshiClient, "get_market", boom_provider)
    monkeypatch.setattr(KalshiClient, "get_order_book", boom_provider)
    monkeypatch.setattr(KalshiClient, "get_series", boom_provider)
    monkeypatch.setattr(KalshiClient, "health", boom_health)
    monkeypatch.setattr(PolymarketClient, "list_events", boom_provider)
    monkeypatch.setattr(PolymarketClient, "list_markets", boom_provider)
    monkeypatch.setattr(PolymarketClient, "health", boom_health)
    monkeypatch.setattr(ReadOnlyCrossVenueCollector, "collect_and_scan", boom_scan)
    monkeypatch.setattr(paper_api, "_collect_report", boom_scan)

    live_src = inspect.getsource(paper_api.live_refresh_status)
    helper_src = inspect.getsource(paper_api._status_with_scan_cycles)
    observe_src = inspect.getsource(LiveRefreshCoordinator.observe_degradation_incidents)
    assert "collect_and_scan" not in live_src
    assert "list_events" not in live_src
    assert "list_markets" not in helper_src
    assert "/venues/health" not in helper_src
    assert "run_slice" not in observe_src
    assert "bind_catalogue_store" not in helper_src

    _paint(coordinator, **_healthy_lanes())
    client = TestClient(main_api.app)
    before = dict(calls)
    first = client.get("/paper/live-refresh")
    assert first.status_code == 200
    assert first.json()["venue_degradation_incidents"] == {}

    _paint(
        coordinator,
        hot={
            "venue_health": {"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"},
            "operation_health": {"matchbook": {"order_book": "ok"}},
            "last_error": None,
            "last_started_at": NOW,
            "last_completed_at": NOW,
        },
        universe={
            "venue_health": {
                "matchbook": HEALTH_DISCOVERY_TIMEOUT,
                "polymarket": "ok",
                "kalshi": "ok",
            },
            "operation_health": {"matchbook": {"list_events": HEALTH_DISCOVERY_TIMEOUT}},
            "last_error": "discovery_timeout",
            "canonical_retryable": 1,
            "series_retryable": 1,
            "worker_state": "degraded",
        },
        background={
            "venue_health": {"matchbook": HEALTH_DEFERRED, "polymarket": "ok", "kalshi": "ok"},
            "operation_health": {"matchbook": {"order_book": HEALTH_CAPACITY_SATURATED}},
        },
    )
    snapshot = ProviderAccessSnapshot(
        inflight={"matchbook": 4, "polymarket": 0, "kalshi": 0},
        waiting={"matchbook": 2, "polymarket": 0, "kalshi": 0},
        waiting_by_lane={
            "hot": {"matchbook": 0, "polymarket": 0, "kalshi": 0},
            "universe": {"matchbook": 1, "polymarket": 0, "kalshi": 0},
            "background": {"matchbook": 1, "polymarket": 0, "kalshi": 0},
        },
        hot_grants_since_universe={"matchbook": 2, "polymarket": 0, "kalshi": 0},
        limits={"matchbook": 4, "polymarket": 8, "kalshi": 4},
    )
    monkeypatch.setattr(type(get_shared_provider_access()), "snapshot", lambda self: snapshot)

    clock.now = NOW + timedelta(seconds=5)
    degraded = client.get("/paper/live-refresh")
    assert degraded.status_code == 200
    body = degraded.json()
    assert body["venue_health"]["matchbook"] == "degraded"
    incidents = body["venue_degradation_incidents"]
    assert "matchbook" in incidents
    captured_at = incidents["matchbook"]["captured_at"]
    assert incidents["matchbook"]["classification"]["hot_ok"] is True
    assert incidents["matchbook"]["classification"]["universe_discovery_timeout"] is True
    assert incidents["matchbook"]["classification"]["hot_market_timeout"] is False
    assert incidents["matchbook"]["classification"]["local_backpressure"] is True
    assert incidents["matchbook"]["data_kind"] == "in_memory_transition_snapshot"

    _paint(
        coordinator,
        universe={
            "venue_health": {
                "matchbook": HEALTH_DISCOVERY_TIMEOUT,
                "polymarket": "ok",
                "kalshi": "ok",
            },
            "last_error": "must not replace the captured snapshot",
            "operation_health": {"matchbook": {"list_events": HEALTH_DISCOVERY_TIMEOUT}},
        },
    )
    clock.now = NOW + timedelta(seconds=25)
    again = client.get("/paper/live-refresh")
    assert again.json()["venue_degradation_incidents"]["matchbook"]["captured_at"] == captured_at
    assert (
        again.json()["venue_degradation_incidents"]["matchbook"]["universe"]["last_error"]
        == "discovery_timeout"
    )

    _paint(coordinator, **_healthy_lanes())
    clock.now = NOW + timedelta(seconds=40)
    recovered = client.get("/paper/live-refresh")
    assert recovered.json()["venue_degradation_incidents"] == {}
    assert recovered.json()["venue_health"]["matchbook"] == "ok"

    _paint(
        coordinator,
        hot={
            "venue_health": {
                "matchbook": HEALTH_MARKET_TIMEOUT,
                "polymarket": "ok",
                "kalshi": "ok",
            },
            "operation_health": {"matchbook": {"order_book": HEALTH_MARKET_TIMEOUT}},
            "last_error": "hot market timeout",
        },
        universe={
            "venue_health": {"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"},
            "operation_health": {"matchbook": {"list_events": "ok"}},
        },
    )
    clock.now = NOW + timedelta(seconds=50)
    second = client.get("/paper/live-refresh")
    new_incident = second.json()["venue_degradation_incidents"]["matchbook"]
    assert new_incident["captured_at"] != captured_at
    assert new_incident["classification"]["hot_market_timeout"] is True
    assert new_incident["classification"]["universe_discovery_timeout"] is False
    assert calls == before


@pytest.mark.asyncio
async def test_live_refresh_why_path_is_observer_only(monkeypatch: pytest.MonkeyPatch) -> None:
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW)
    _install_coordinator(monkeypatch, coordinator)
    source = inspect.getsource(paper_api.live_refresh_status)
    helper = inspect.getsource(paper_api._status_with_scan_cycles)
    assert "collect_and_scan" not in source
    assert "_collect_report" not in source
    assert "configure_from_settings" not in source
    assert "run_price_engine_slice" not in helper
    assert "run_slice" not in helper
    assert "list_events" not in helper
    transport = httpx.ASGITransport(app=main_api.app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        status = await client.get("/paper/live-refresh")
        assert status.status_code == 200
        assert "venue_degradation_incidents" in status.json()
        assert coordinator._hot_in_progress is False
        assert coordinator._universe_in_progress is False
        assert coordinator._background_in_progress is False
        assert coordinator._next_hot_due is None
        assert coordinator._next_universe_due is None
