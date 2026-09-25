"""A pagination-capped Polymarket series keeps its UNIVERSE generation open.

Drives the real collector through LiveRefreshCoordinator chunks. Provider
payloads are deterministic fixtures, not owner-live Gamma evidence.
"""

from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

import pytest

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import (
    STARTUP_READINESS_COLD_UNREADY,
    STARTUP_READINESS_FRESH_GENERATION_READY,
    ScanLane,
)
from sports_hedge.application.live_refresh import DualCadencePlan, LiveRefreshCoordinator
from sports_hedge.application.universe_checkpoint import SWEEP_OK, SWEEP_PENDING, series_work_key
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.persistence.universe_checkpoint import SqliteUniverseCheckpointStore
from test_dual_cadence_scheduler import NOW, FakeClock

EPL = "10188"
LALIGA = "10193"
CODES = ["premier_league", "la_liga"]
PAGE_LIMIT = 3
PAGES_PER_CHUNK = 2
KICKOFF = NOW + timedelta(days=3)


class _Settings:
    polymarket_gamma_page_limit = PAGE_LIMIT
    polymarket_gamma_max_pages_per_series = PAGES_PER_CHUNK


def _event(event_id: str, home: str, away: str) -> dict[str, Any]:
    return {
        "id": event_id,
        "title": f"{home} vs {away}",
        "startTime": KICKOFF.isoformat().replace("+00:00", "Z"),
        "competition": "Premier League",
    }


def _epl_pages() -> list[list[dict[str, Any]]]:
    pages = []
    for page, size in enumerate((PAGE_LIMIT, PAGE_LIMIT, PAGE_LIMIT, 1)):
        pages.append(
            [
                _event(f"epl-{page}-{index}", f"Home {page}{index}", f"Away {page}{index}")
                for index in range(size)
            ]
        )
    return pages


class PagedGamma:
    """One page per call. Records (series, page_index) for every request."""

    def __init__(self) -> None:
        self.settings = _Settings()
        self.pages = {
            EPL: _epl_pages(),
            LALIGA: [[_event("laliga-1", "Real Madrid", "Barcelona")]],
        }
        self.calls: list[tuple[str, int]] = []
        self.last_pages_attempted = 0

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        series = str(filters.get("series_id") or "")
        page = int(filters.get("discovery_page_index") or 0)
        self.calls.append((series, page))
        self.last_pages_attempted = 1
        series_pages = self.pages.get(series, [])
        return list(series_pages[page]) if page < len(series_pages) else []

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return []

    async def get_order_book(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {"bids": [], "asks": []}


def _collector(client: PagedGamma) -> ReadOnlyCrossVenueCollector:
    collector = ReadOnlyCrossVenueCollector(
        matchbook=None,
        polymarket=client,
        kalshi=None,
        paper_scan=PaperScanService(MarketIntelligenceService(SqliteMarketIntelligenceRepository())),
        cycle_timeout_seconds=None,
    )
    original = collector._scan_cluster

    async def evaluated(cluster: Any, **kwargs: Any) -> Any:
        fixture, decisions, inventory, counts, fetched, pairs = await original(cluster, **kwargs)
        fixture = fixture.model_copy(
            update={"market_evaluation_state": "evaluated", "last_scanned_at": NOW}
        )
        return fixture, decisions, inventory, counts, fetched, pairs

    collector._scan_cluster = evaluated  # type: ignore[method-assign]
    return collector


def _coordinator(clock: FakeClock, store: SqliteUniverseCheckpointStore) -> LiveRefreshCoordinator:
    coordinator = LiveRefreshCoordinator(clock=clock, universe_checkpoint_store=store)
    coordinator._clock = clock
    coordinator.configure_from_settings()
    coordinator.arm_startup_pricing_barrier()
    return coordinator


async def _run_chunk(
    coordinator: LiveRefreshCoordinator,
    collector: ReadOnlyCrossVenueCollector,
    plan: DualCadencePlan | None = None,
) -> Any:
    async def runner() -> Any:
        on_discovery, on_fixture, on_work = coordinator.universe_collect_callbacks()
        return await collector.collect_and_scan(
            scan_lane=ScanLane.UNIVERSE.value,
            unbounded_cycle=True,
            enabled_venues=[VenueName.POLYMARKET],
            selected_competition_codes=CODES,
            polymarket_discovery_now=NOW,
            on_discovery_complete=on_discovery,
            on_fixture_evaluated=on_fixture,
            on_canonical_work_set=on_work,
            **(
                {
                    "reuse_discovery": plan.reuse_discovery,
                    "discovery_snapshot": plan.discovery_snapshot,
                    "retry_series": plan.retry_series,
                    "series_page_cursors": plan.series_page_cursors,
                    "skip_event_ids": plan.skip_event_ids,
                    "generation_resume": plan.generation_resume,
                    "prior_series_results": coordinator.universe_series_results_snapshot(),
                }
                if plan is not None
                else {}
            ),
        )

    return await coordinator.run_cycle(runner, timeout_seconds=None, scan_lane=ScanLane.UNIVERSE)


def _epl_unit(coordinator: LiveRefreshCoordinator) -> Any:
    return coordinator._universe_series_work[series_work_key("polymarket", EPL)]


@pytest.mark.asyncio
async def test_pagination_capped_series_keeps_generation_open_and_resumes_from_cursor(
    tmp_path: Path,
) -> None:
    clock = FakeClock(NOW)
    store = SqliteUniverseCheckpointStore(tmp_path / "cap.sqlite")
    coordinator = _coordinator(clock, store)
    client = PagedGamma()
    collector = _collector(client)

    first = await _run_chunk(coordinator, collector)
    assert client.calls == [(EPL, 0), (LALIGA, 0), (EPL, 1)]
    rows = {row["series"]: row for row in first.series_results["polymarket"]}
    assert rows[EPL]["status"] == "pagination_capped"
    assert rows[EPL]["next_page_index"] == PAGES_PER_CHUNK
    assert rows[EPL]["retained_event_count"] == 2 * PAGE_LIMIT
    assert rows[LALIGA]["status"] == "ok"
    assert first.scan_diagnostics["canonical_work_set_authoritative"] is False

    unit = _epl_unit(coordinator)
    assert unit.state == SWEEP_PENDING
    assert unit.next_page_index == PAGES_PER_CHUNK
    assert unit.next_retry_at is None
    assert coordinator._universe_series_is_terminal_unlocked() is False
    assert coordinator._universe_sweep_is_complete_unlocked() is False
    assert coordinator._universe_generation_started_at is not None
    assert coordinator.status.universe.worker_state != "complete"
    assert coordinator.startup_pricing_ready() is False
    assert coordinator.startup_pricing_readiness() == STARTUP_READINESS_COLD_UNREADY
    assert coordinator._startup_generation_retry_blocked_unlocked() is False

    checkpoint = store.load()
    assert checkpoint is not None
    persisted = checkpoint["series_work"][series_work_key("polymarket", EPL)]
    assert persisted["next_page_index"] == PAGES_PER_CHUNK
    assert persisted["state"] == SWEEP_PENDING
    restarted = _coordinator(clock, store)
    restored = _epl_unit(restarted)
    assert restored.state == SWEEP_PENDING
    assert restored.next_page_index == PAGES_PER_CHUNK
    assert restarted._universe_sweep_is_complete_unlocked() is False
    assert restarted.startup_pricing_ready() is False
    restart_plan = restarted.plan_universe_tick(now=clock.now)
    # The restarted process has no discovery snapshot holding pages 0..1, so
    # it rediscovers from page 0 instead of skipping evidence it never saw.
    assert restart_plan.reuse_discovery is False
    assert restart_plan.series_page_cursors == {}
    restart_client = PagedGamma()
    await _run_chunk(restarted, _collector(restart_client), restart_plan)
    assert restart_client.calls == [(EPL, 0), (LALIGA, 0), (EPL, 1)]
    assert _epl_unit(restarted).state == SWEEP_PENDING
    assert _epl_unit(restarted).next_page_index == PAGES_PER_CHUNK
    assert restarted._universe_generation_started_at is not None
    assert restarted.startup_pricing_ready() is False

    plan = coordinator.plan_universe_tick(now=clock.now)
    assert plan.lane == ScanLane.UNIVERSE.value
    assert plan.reuse_discovery is True
    assert plan.retry_series == {"polymarket": [EPL]}
    assert plan.series_page_cursors == {"polymarket": {EPL: PAGES_PER_CHUNK}}
    client.calls.clear()
    resumed = await _run_chunk(coordinator, collector, plan)
    assert client.calls == [(EPL, 2), (EPL, 3)]
    resumed_rows = {row["series"]: row for row in resumed.series_results["polymarket"]}
    assert resumed_rows[EPL]["status"] == "ok"
    assert resumed_rows[EPL]["start_page_index"] == PAGES_PER_CHUNK
    assert resumed_rows[LALIGA]["status"] == "ok"
    # Pages 0..1 from the reused snapshot, La Liga, and resumed pages 2..3.
    assert resumed.raw_polymarket_events == 2 * PAGE_LIMIT + 1 + PAGE_LIMIT + 1

    diagnostics = coordinator.status.universe.last_diagnostics or {}
    healed = diagnostics["series_work"][series_work_key("polymarket", EPL)]
    assert healed["state"] == SWEEP_OK
    assert healed["next_page_index"] is None
    assert coordinator._universe_generation_started_at is None
    assert coordinator.status.universe.worker_state == "complete"
    assert coordinator.startup_pricing_ready() is True
    assert coordinator.startup_pricing_readiness() == STARTUP_READINESS_FRESH_GENERATION_READY


@pytest.mark.asyncio
async def test_clear_and_update_drops_the_page_cursor_and_restarts_from_page_zero(
    tmp_path: Path,
) -> None:
    clock = FakeClock(NOW)
    store = SqliteUniverseCheckpointStore(tmp_path / "clear.sqlite")
    coordinator = _coordinator(clock, store)
    client = PagedGamma()
    collector = _collector(client)
    await _run_chunk(coordinator, collector)
    assert _epl_unit(coordinator).next_page_index == PAGES_PER_CHUNK

    audit = coordinator.clear_universe_working_set(run_after=True)
    assert audit["reuse_discovery"] is False
    assert coordinator._universe_series_work == {}
    assert coordinator._universe_discovery_snapshot is None
    plan = coordinator.plan_universe_tick(now=clock.now)
    assert plan.series_page_cursors == {}
    assert plan.reuse_discovery is False

    client.calls.clear()
    await _run_chunk(coordinator, collector)
    assert client.calls[0] == (EPL, 0)
    assert _epl_unit(coordinator).next_page_index == PAGES_PER_CHUNK
    assert coordinator._universe_generation_started_at is not None
