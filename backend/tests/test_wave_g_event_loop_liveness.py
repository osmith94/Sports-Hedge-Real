from __future__ import annotations

import asyncio
import inspect
import threading
import time
from datetime import UTC, datetime

import httpx
import pytest

from sports_hedge.api import paper as paper_api
from sports_hedge.api.main import app
from sports_hedge.application.collector import CollectionReport
from sports_hedge.application.scan_lanes import ScanLane


class _PersistCoordinator:
    def __init__(self, event_loop_thread_id: int) -> None:
        self.event_loop_thread_id = event_loop_thread_id
        self.persist_outcome: dict[str, object] | None = None

    def record_persist_outcome(self, **kwargs) -> None:
        # Coordinator state must be mutated back on the event-loop side, not in
        # the worker running synchronous SQLite / paper-chain persistence.
        assert threading.get_ident() == self.event_loop_thread_id
        self.persist_outcome = dict(kwargs)


@pytest.mark.asyncio
async def test_slow_persist_does_not_starve_health_or_live_refresh(monkeypatch) -> None:
    """Owner-Windows regression: persistence must not monopolize FastAPI's event loop."""

    event_loop_thread_id = threading.get_ident()
    coordinator = _PersistCoordinator(event_loop_thread_id)
    now = datetime(2026, 9, 15, 20, 15, tzinfo=UTC)
    report = CollectionReport(started_at=now, completed_at=now)

    persist_started = threading.Event()
    persist_finished = threading.Event()
    persist_thread_ids: list[int] = []
    persist_delay = 0.50

    def slow_persist(*args, **kwargs) -> None:
        del args, kwargs
        persist_thread_ids.append(threading.get_ident())
        persist_started.set()
        time.sleep(persist_delay)
        persist_finished.set()

    monkeypatch.setattr(paper_api, "_persist_collection_report", slow_persist)

    async def invoke_persist() -> None:
        result = paper_api.persist_scheduled_collection_report(
            coordinator,
            report,
            service=object(),
            audit=object(),
            watchlist=object(),
            scan_lane=ScanLane.UNIVERSE,
        )
        if inspect.isawaitable(result):
            await result

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        probe_started = time.monotonic()

        async def probe_server() -> tuple[httpx.Response, httpx.Response, float]:
            # Let persistence begin first. On the broken implementation this sleep
            # cannot resume until the synchronous persist finishes because the event
            # loop is blocked.
            await asyncio.sleep(0.02)
            health = await client.get("/health")
            live_refresh = await client.get("/paper/live-refresh")
            return health, live_refresh, time.monotonic() - probe_started

        probe_task = asyncio.create_task(probe_server())
        await asyncio.sleep(0)
        persist_task = asyncio.create_task(invoke_persist())
        health, live_refresh, probe_elapsed = await probe_task
        await persist_task

    assert persist_started.is_set()
    assert persist_finished.is_set()
    assert health.status_code == 200
    assert live_refresh.status_code == 200
    assert probe_elapsed < 0.25, (
        f"event loop stalled for {probe_elapsed:.3f}s while persistence ran "
        f"for {persist_delay:.2f}s"
    )
    assert persist_thread_ids
    assert persist_thread_ids[0] != event_loop_thread_id
    assert coordinator.persist_outcome is not None
    assert coordinator.persist_outcome["ok"] is True


def test_manual_and_scheduled_paths_share_awaitable_persist_wrapper() -> None:
    """Both scanner entry points must use the same event-loop-safe persistence seam."""

    assert inspect.iscoroutinefunction(paper_api.persist_scheduled_collection_report)

    explicit_src = inspect.getsource(paper_api.persist_explicit_collect_after_http_response)
    scheduled_src = inspect.getsource(paper_api.server_owned_refresh_tick)

    assert "await persist_scheduled_collection_report(" in explicit_src
    assert "await persist_scheduled_collection_report(" in scheduled_src
