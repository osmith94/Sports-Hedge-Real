"""Issue #313: combined-path proof for exact #310 + #311 + #312 composition.

One coordinator plus one shared provider runtime proves the three
architect-accepted owner-live seams survive together:

- PR #312 / #308: an open UNIVERSE generation retains an early
  ApprovedEquivalent relationship
- PR #310 / #307: HOT heartbeat continues independently with scope-empty
  status; a lifecycle HOT fixture can run without waiting for UNIVERSE
  completion; provider live-call cap remains bounded
- PR #311 / #309 identity convergence is covered by its own suite; this
  combined path keeps one canonical HOT scheduling unit

Fixture/demo doubles. Not owner-live quotes, not historical books, not
modelled probabilities. PAPER / read-only. Execution disabled.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest

from sports_hedge.application.collector import (
    DEFAULT_PROVIDER_CONCURRENCY,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.provider_access import ProviderAccessLayer
from sports_hedge.application.scan_lanes import (
    DEFAULT_UNIVERSE_TTL_SECONDS,
    ScanLane,
    WORKER_WAITING,
)
from sports_hedge.config import Settings, get_settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient
from test_current_market_inventory import NOW, _decision
from test_dual_cadence_scheduler import FakeClock, _fixture as _hot_fixture, _report
from test_issue307_kalshi_metadata_pressure import (
    FORBIDDEN_WRITE_METHODS,
    _DisabledPolymarket,
    _Kalshi,
    _ignore_cancel_until,
    _kalshi_btts_event,
    _prep_capacity_collector,
)
from test_issue308_generation_aware_current_state import (
    FIXTURE_A,
    FIXTURE_B,
    GENERATION_ID,
    TWENTY_MINUTES,
    _evaluated_fixture,
    _inventory_row,
    _row,
)


def test_paper_execution_boundary_and_unbounded_universe_contract() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    assert settings.paper_universe_current_state_ttl_seconds == 360
    assert DEFAULT_UNIVERSE_TTL_SECONDS == 360
    assert settings.paper_scan_provider_timeout_seconds == 8
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.MATCHBOOK] == 4
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.KALSHI] == 4
    for client in (MatchbookClient, KalshiClient, PolymarketClient):
        for method in FORBIDDEN_WRITE_METHODS:
            assert not hasattr(client, method), f"{client.__name__}.{method} must not exist"


@pytest.mark.asyncio
async def test_open_universe_retains_relationship_while_hot_heartbeats_and_provider_cap_holds() -> None:
    """One coordinator/runtime: UNIVERSE open + HOT independent + bounded live calls."""

    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator._universe_generation_id = GENERATION_ID
    coordinator._universe_generation_started_at = NOW
    coordinator._universe_progress_generation_id = GENERATION_ID
    coordinator._next_hot_due = NOW
    coordinator._next_universe_due = NOW
    coordinator._stop = asyncio.Event()

    access = ProviderAccessLayer(
        {
            VenueName.MATCHBOOK: 4,
            VenueName.POLYMARKET: 8,
            VenueName.KALSHI: 4,
        }
    )
    live = {"n": 0, "peak": 0}
    lock = asyncio.Lock()
    release = asyncio.Event()

    class StubbornMatchbook:
        async def list_events(self, **filters: Any) -> dict[str, Any]:
            del filters
            return {"events": []}

        async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
            del filters
            async with lock:
                live["n"] += 1
                live["peak"] = max(live["peak"], live["n"])
            try:
                await _ignore_cancel_until(release)
                return {"markets": [], "id": event_id}
            finally:
                async with lock:
                    live["n"] -= 1

    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=StubbornMatchbook(),
        polymarket=_DisabledPolymarket(),
        kalshi=_Kalshi([_kalshi_btts_event()]),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        provider_call_timeout_seconds=0.05,
        provider_access=access,
        provider_concurrency={
            VenueName.MATCHBOOK: 4,
            VenueName.POLYMARKET: 8,
            VenueName.KALSHI: 4,
        },
    )
    _prep_capacity_collector(collector, access, timeout=0.05)

    try:
        fixture_a = _evaluated_fixture(
            FIXTURE_A,
            when=NOW,
            equivalent=1,
            arb=False,
            opportunity="matched",
        )
        coordinator.record_universe_fixture_progress(
            None,
            fixture_a,
            [
                _decision("mkt-btts", when=NOW).model_copy(
                    update={
                        "canonical_event_id": FIXTURE_A,
                        "fixture_canonical_event_id": FIXTURE_A,
                    }
                )
            ],
            [_row(arb=False, edge=Decimal("-0.004"))],
        )
        store = coordinator.fixture_current_state()
        early = _inventory_row(store, FIXTURE_A, NOW)
        assert early is not None
        assert early.matched_equivalent_count >= 1
        assert early.opportunity_state != "unmatched"
        assert coordinator._universe_generation_started_at is not None

        universe_started = asyncio.Event()
        universe_hold = asyncio.Event()
        hot_ticks: list[str] = []

        async def universe_runner() -> Any:
            universe_started.set()
            await universe_hold.wait()
            return _report([], when=clock.now, scan_lane=ScanLane.UNIVERSE.value)

        async def tick(plan=None) -> Any:
            if plan is None:
                return None
            if plan.lane == ScanLane.HOT.value:
                hot_ticks.append(plan.reason)
                return _report(
                    [
                        _hot_fixture(
                            "live",
                            kickoff=clock.now - timedelta(minutes=1),
                            in_running=True,
                        )
                    ],
                    when=clock.now,
                    scan_lane=ScanLane.HOT.value,
                )
            return None

        never = coordinator.status.hot
        assert never.last_heartbeat_at is None
        assert never.last_plan_reason is None

        universe_task = asyncio.create_task(
            coordinator.run_cycle(
                universe_runner, timeout_seconds=None, scan_lane=ScanLane.UNIVERSE
            )
        )
        await universe_started.wait()
        assert coordinator._universe_in_progress is True

        hot_task = asyncio.create_task(coordinator._hot_loop(tick))
        await asyncio.sleep(0.15)
        hot = coordinator.status.hot
        assert hot.last_heartbeat_at is not None
        assert hot.last_plan_reason == "hot_scope_empty"
        assert hot.worker_state == WORKER_WAITING
        assert "scope empty" in (hot.operator_summary or "").casefold()
        assert "worker alive" in (hot.operator_summary or "").casefold()
        assert "no provider call" in (hot.operator_summary or "").casefold()
        assert hot_ticks == []
        assert coordinator._universe_in_progress is True

        live_fixture = _hot_fixture(
            "live",
            kickoff=clock.now - timedelta(minutes=1),
            in_running=True,
        )
        coordinator.record_report(
            _report([live_fixture], when=clock.now, scan_lane=ScanLane.HOT.value),
            scan_lane=ScanLane.HOT,
            advance_hot_due=False,
        )
        await asyncio.sleep(0.2)
        assert hot_ticks
        assert coordinator._universe_in_progress is True

        first_wave = [
            asyncio.create_task(
                collector._wait_provider(
                    collector.matchbook.list_markets(index),
                    stage="list_markets",
                    venue=VenueName.MATCHBOOK,
                    source_id=str(index),
                    default=None,
                )
            )
            for index in range(8)
        ]
        await asyncio.sleep(0.12)
        assert live["n"] == 2
        assert live["peak"] == 2
        assert access.snapshot().inflight["matchbook"] == 2
        assert access.lower_in_use[VenueName.MATCHBOOK] == 2
        assert access.limits[VenueName.MATCHBOOK] == 4
        assert coordinator._universe_in_progress is True
        assert coordinator.status.hot.last_plan_reason is not None

        release.set()
        await asyncio.gather(*first_wave)
        assert live["peak"] == 2
        assert live["n"] == 0
        assert collector._provider_peak_inflight[VenueName.MATCHBOOK] <= 4

        clock.advance(TWENTY_MINUTES)
        late = _evaluated_fixture(
            FIXTURE_B, when=clock.now, equivalent=0, opportunity="unmatched"
        )
        coordinator.record_universe_fixture_progress(None, late, [], [])
        retained = _inventory_row(store, FIXTURE_A, clock.now)
        assert retained is not None
        assert retained.matched_equivalent_count >= 1
        assert retained.opportunity_state == "matched"
        assert retained.solver_is_arbitrage is False
        assert FIXTURE_A not in store.hot_identity_scope(clock.now)
        assert get_settings().paper_universe_current_state_ttl_seconds == 360
        assert coordinator._universe_in_progress is True

        coordinator._stop.set()
        universe_hold.set()
        hot_task.cancel()
        try:
            await hot_task
        except asyncio.CancelledError:
            pass
        await universe_task
        assert Settings().sports_hedge_execution_enabled is False
    finally:
        repository.close()
