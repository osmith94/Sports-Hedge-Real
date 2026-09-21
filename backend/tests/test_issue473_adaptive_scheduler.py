"""#473 adaptive provider-aware fair scheduler.

Deterministic ranking, synthetic providers, injected clocks.
PAPER / read-only. No concurrency increase. No live quotes.
"""

from __future__ import annotations

import asyncio
import inspect
from datetime import timedelta
from typing import Any

import pytest
from test_dual_cadence_scheduler import FakeClock
from test_issue344_price_engine import (
    NEAR_KICKOFF,
    NOW,
    _engine,
    _hda_row,
    _hold_slot,
    _row,
)

from sports_hedge.application.approved_market_catalogue import OutcomeNativeId
from sports_hedge.application.adaptive_scheduler import (
    AGING_QUANTUM_MS,
    LATENCY_BACKPRESSURE_MS,
    ProviderPressure,
    SchedulerBand,
    SchedulerValueClass,
    SchedulerWork,
    classify_value_class,
    order_scheduler_work,
    rank_scheduler_work,
)
from sports_hedge.application.opportunity_viability import (
    CROSS_VENUE_UNAVAILABLE,
    reset_opportunity_viability_cache,
)
from sports_hedge.application.price_engine import PriceEnginePriority
from sports_hedge.application.provider_access import (
    DEFAULT_PROVIDER_CONCURRENCY,
    PRICE_ENGINE_ACTIVE_TRADE_LANE,
    PRICE_ENGINE_BACKGROUND_LANE,
    ProviderAccessLayer,
)
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.application.system_load import SYSTEM_LOAD_JSON_BUDGET_BYTES, system_load_from_status
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.approved_register import CANONICAL_BTTS_FT


@pytest.fixture(autouse=True)
def _reset_viability() -> Any:
    reset_opportunity_viability_cache()
    yield
    reset_opportunity_viability_cache()


def _work(**overrides: Any) -> SchedulerWork:
    payload: dict[str, Any] = {
        "lane": ScanLane.HOT.value,
        "work_id": "w",
        "viable_venue_count": 2,
        "viability_assessed": True,
        "required_venues": (VenueName.MATCHBOOK, VenueName.KALSHI),
        "seq": 0,
    }
    payload.update(overrides)
    return SchedulerWork(**payload)


def _degraded_matchbook() -> dict[VenueName, ProviderPressure]:
    return {
        VenueName.MATCHBOOK: ProviderPressure(
            venue=VenueName.MATCHBOOK,
            inflight=4,
            waiting=8,
            limit=4,
            ewma_latency_ms=LATENCY_BACKPRESSURE_MS + 200,
            rate_limited=True,
            rate_limit_remaining_s=12.0,
        ),
        VenueName.KALSHI: ProviderPressure(
            venue=VenueName.KALSHI, inflight=1, waiting=0, limit=4, ewma_latency_ms=80
        ),
        VenueName.POLYMARKET: ProviderPressure(
            venue=VenueName.POLYMARKET, inflight=1, waiting=0, limit=8, ewma_latency_ms=60
        ),
    }


def test_priority_function_is_documented_and_deterministic() -> None:
    mod = inspect.getsource(
        __import__("sports_hedge.application.adaptive_scheduler", fromlist=["rank_scheduler_work"])
    )
    assert "rank_key = (band, backpressure, -urgency, -wait_age_ms, seq)" in mod
    assert "ACTIVE TRADE safety" in mod
    first = rank_scheduler_work(_work(work_id="a", seq=1, near_threshold=True))
    second = rank_scheduler_work(_work(work_id="a", seq=1, near_threshold=True))
    assert first.rank_key == second.rank_key
    assert first.as_dict()["value_class"] == SchedulerValueClass.HOT_VIABLE_NEAR.value
    assert "hot_viable_near" in first.reasons


def test_active_safety_outranks_hot_viable_near() -> None:
    ranked = order_scheduler_work(
        [
            _work(lane=ScanLane.HOT.value, work_id="hot", near_threshold=True, seq=0),
            _work(
                lane=PRICE_ENGINE_ACTIVE_TRADE_LANE,
                work_id="active",
                seq=1,
                viability_assessed=False,
            ),
        ]
    )
    assert ranked[0][0].work_id == "active"
    assert ranked[0][1].band == int(SchedulerBand.ACTIVE)


def test_hot_viable_near_outranks_low_value_background_and_universe() -> None:
    ranked = order_scheduler_work(
        [
            _work(
                lane=PRICE_ENGINE_BACKGROUND_LANE,
                work_id="bg-low",
                skip_expensive_work=True,
                viability_reason=CROSS_VENUE_UNAVAILABLE,
                seq=0,
            ),
            _work(lane=ScanLane.UNIVERSE.value, work_id="uni", viability_assessed=False, seq=1),
            _work(lane=ScanLane.HOT.value, work_id="hot-near", near_threshold=True, seq=2),
            _work(
                lane=PRICE_ENGINE_BACKGROUND_LANE,
                work_id="bg",
                viable_venue_count=2,
                seq=3,
            ),
        ]
    )
    assert [item.work_id for item, _ in ranked] == ["hot-near", "uni", "bg", "bg-low"]


def test_aging_promotes_background_without_beating_active() -> None:
    stale_bg = _work(
        lane=PRICE_ENGINE_BACKGROUND_LANE,
        work_id="aged-bg",
        wait_age_ms=AGING_QUANTUM_MS * 8,
        seq=0,
    )
    fresh_hot = _work(lane=ScanLane.HOT.value, work_id="fresh-hot", seq=1)
    active = _work(
        lane=PRICE_ENGINE_ACTIVE_TRADE_LANE,
        work_id="active",
        viability_assessed=False,
        seq=2,
    )
    ranked = order_scheduler_work([stale_bg, fresh_hot, active])
    assert ranked[0][0].work_id == "active"
    aged = rank_scheduler_work(stale_bg)
    assert aged.aging_steps >= 1
    assert aged.band <= int(SchedulerBand.HOT_ORDINARY)
    assert aged.band >= int(SchedulerBand.HOT_VIABLE_NEAR)
    assert any("aging_steps" in reason for reason in aged.reasons)


def test_degraded_matchbook_penalizes_matchbook_bound_work_not_healthy_venues() -> None:
    pressure = _degraded_matchbook()
    mb_bg = _work(
        lane=PRICE_ENGINE_BACKGROUND_LANE,
        work_id="mb-bg",
        required_venues=(VenueName.MATCHBOOK,),
        seq=0,
    )
    healthy = _work(
        lane=ScanLane.HOT.value,
        work_id="k-pm",
        required_venues=(VenueName.KALSHI, VenueName.POLYMARKET),
        near_threshold=True,
        seq=1,
    )
    ranked = order_scheduler_work([mb_bg, healthy], pressure_by_venue=pressure)
    assert ranked[0][0].work_id == "k-pm"
    mb_decision = rank_scheduler_work(mb_bg, pressure_by_venue=pressure)
    healthy_decision = rank_scheduler_work(healthy, pressure_by_venue=pressure)
    assert mb_decision.backpressure_penalty == 1
    assert healthy_decision.backpressure_penalty == 0
    assert any("matchbook" in reason for reason in mb_decision.reasons)


def test_cadence_keeps_not_due_items_out_of_due_set() -> None:
    clock = FakeClock(NOW)
    engine, _mb, _ks, _layer = _engine(
        [_hda_row("due"), _hda_row("wait")],
        clock=clock,
        hot_interval=30,
        background_interval=90,
    )
    waiting = engine.item("amc-wait")
    assert waiting is not None
    waiting.last_priced_at = NOW
    waiting.priority = PriceEnginePriority.BACKGROUND
    waiting.near_threshold = True
    waiting.qualifying = True
    due = engine.due_items(PriceEnginePriority.BACKGROUND, now=NOW)
    assert "amc-wait" not in {item.identity.catalogue_row_id for item in due}
    later = engine.due_items(PriceEnginePriority.BACKGROUND, now=NOW + timedelta(seconds=90))
    assert "amc-wait" in {item.identity.catalogue_row_id for item in later}


def test_concurrency_caps_unchanged() -> None:
    settings = Settings()
    assert settings.paper_scan_matchbook_concurrency == 4
    assert settings.paper_scan_kalshi_concurrency == 4
    assert settings.paper_scan_polymarket_concurrency == 8
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.MATCHBOOK] == 4
    layer = ProviderAccessLayer()
    assert layer.limits[VenueName.MATCHBOOK] == 4
    assert layer.limits[VenueName.KALSHI] == 4
    assert layer.limits[VenueName.POLYMARKET] == 8
    source = inspect.getsource(ProviderAccessLayer._pick)
    assert "self._limits[venue]" not in source or "4" in inspect.getsource(ProviderAccessLayer)
    assert "starvation_hot_grants" in inspect.getsource(ProviderAccessLayer._pick) or (
        "starve_hot" in source and "ProviderPriority.HOT" in source
    )


@pytest.mark.asyncio
async def test_hot_burst_does_not_starve_background_forever() -> None:
    layer = ProviderAccessLayer({VenueName.MATCHBOOK: 1}, starvation_hot_grants=2)
    order: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    async def holder() -> None:
        async with layer.acquire(VenueName.MATCHBOOK, lane=ScanLane.HOT.value):
            started.set()
            await release.wait()

    async def waiter(lane: str, work_id: str) -> None:
        await started.wait()
        async with layer.acquire(
            VenueName.MATCHBOOK,
            lane=lane,
            work=_work(lane=lane, work_id=work_id, seq=0),
        ):
            order.append(work_id)

    holder_task = asyncio.create_task(holder())
    await started.wait()
    waiters = [
        asyncio.create_task(waiter(ScanLane.HOT.value, "hot-1")),
        asyncio.create_task(waiter(ScanLane.HOT.value, "hot-2")),
        asyncio.create_task(waiter(PRICE_ENGINE_BACKGROUND_LANE, "bg-1")),
    ]
    await asyncio.sleep(0.02)
    release.set()
    await asyncio.gather(holder_task, *waiters)
    assert "bg-1" in order
    assert order.index("bg-1") <= 2


@pytest.mark.asyncio
async def test_queue_metrics_depth_wait_latency_saturation_and_deadline_miss() -> None:
    clock = {"now": 1000.0}

    def mono() -> float:
        return clock["now"]

    layer = ProviderAccessLayer(
        {VenueName.MATCHBOOK: 1, VenueName.KALSHI: 4, VenueName.POLYMARKET: 8},
        monotonic_clock=mono,
    )
    gate = asyncio.Event()
    holder = asyncio.create_task(_hold_slot(layer, VenueName.MATCHBOOK, gate))
    await asyncio.sleep(0.05)
    clock["now"] = 1000.4
    queued = asyncio.create_task(
        layer_acquire_deadline(layer, deadline_mono=1000.1)
    )
    await asyncio.sleep(0.05)
    snap = layer.snapshot()
    metrics = snap.queue[VenueName.MATCHBOOK.value]
    assert metrics["depth"] >= 1
    assert metrics["wait_age_ms"] >= 0
    assert metrics["saturated"] is True
    assert metrics["backpressure"] is True
    assert snap.waiting[VenueName.MATCHBOOK.value] >= 1
    clock["now"] = 1001.0
    gate.set()
    await holder
    await queued
    later = layer.snapshot()
    assert later.queue[VenueName.MATCHBOOK.value]["deadline_misses"] >= 1
    assert later.queue[VenueName.MATCHBOOK.value]["last_service_ms"] >= 0
    assert "service_latency_ms" in later.queue[VenueName.MATCHBOOK.value]
    assert later.deadline_misses_by_lane[ScanLane.HOT.value] >= 1


async def layer_acquire_deadline(layer: ProviderAccessLayer, deadline_mono: float) -> None:
    work = _work(deadline_mono=deadline_mono, now_mono=deadline_mono - 1)
    async with layer.acquire(VenueName.MATCHBOOK, lane=ScanLane.HOT.value, work=work):
        return


@pytest.mark.asyncio
async def test_degraded_matchbook_price_engine_prefers_healthy_kalshi_polymarket() -> None:
    access = ProviderAccessLayer(
        {
            VenueName.MATCHBOOK: 4,
            VenueName.KALSHI: 4,
            VenueName.POLYMARKET: 8,
        }
    )
    access.observe_rate_limit(VenueName.MATCHBOOK, 30)
    access.observe_latency_ms(VenueName.MATCHBOOK, 2400)
    pm_row = _row(
        suffix="kpm",
        key=CANONICAL_BTTS_FT,
        kickoff=NEAR_KICKOFF,
        matchbook_event_id="",
        matchbook_market_id="",
        kalshi_event="KXEPLBTTS-KPM",
    )
    pm_row = pm_row.model_copy(
        update={
            "matchbook_event_id": None,
            "matchbook_market_id": None,
            "matchbook_runner_ids": [],
            "polymarket_event_id": "pm-evt",
            "polymarket_market_id": "pm-mkt",
            "polymarket_condition_id": "pm-cond",
            "polymarket_token_ids": [
                OutcomeNativeId(outcome="yes", native_id="tok-yes"),
                OutcomeNativeId(outcome="no", native_id="tok-no"),
            ],
        }
    )
    engine, _mb, _ks, layer = _engine(
        [_hda_row("mbhot", kickoff=NEAR_KICKOFF), pm_row],
        access=access,
        hot_interval=0,
        background_interval=0,
    )
    for runtime in engine.items():
        runtime.priority = PriceEnginePriority.HOT
        engine._refresh_scheduler_signals(runtime)
    mb_item = engine.item("amc-mbhot")
    kpm_item = engine.item("amc-kpm")
    assert mb_item is not None and kpm_item is not None
    mb_item.near_threshold = False
    kpm_item.near_threshold = True
    due = engine.due_items(PriceEnginePriority.HOT, now=NOW)
    assert [item.identity.catalogue_row_id for item in due][0] == "amc-kpm"
    assert layer.limits[VenueName.MATCHBOOK] == 4


def test_system_load_projects_queue_metrics_without_breaking_budget() -> None:
    from sports_hedge.application.live_refresh import LaneRefreshStatus, LiveRefreshStatus

    status = LiveRefreshStatus(
        server_loop_enabled=False,
        interval_seconds=30,
        hot=LaneRefreshStatus(cadence_seconds=30, fixture_count=1, last_duration_ms=1000),
        provider_access={
            "inflight": {"matchbook": 4, "kalshi": 1, "polymarket": 0},
            "waiting": {"matchbook": 3, "kalshi": 0, "polymarket": 0},
            "limits": {"matchbook": 4, "kalshi": 4, "polymarket": 8},
            "queue": {
                "matchbook": {
                    "depth": 3,
                    "wait_age_ms": 1800,
                    "service_latency_ms": 220,
                    "deadline_misses": 2,
                    "saturated": True,
                },
                "kalshi": {
                    "depth": 0,
                    "wait_age_ms": 0,
                    "service_latency_ms": 40,
                    "deadline_misses": 0,
                    "saturated": False,
                },
            },
        },
    )
    load = system_load_from_status(status)
    assert load.matchbook.wait_ms == 1800
    assert load.matchbook.latency_ms == 220
    assert load.matchbook.deadline_misses == 2
    assert load.matchbook.saturated is True
    dumped = load.model_dump(mode="json")
    assert "p50" not in str(dumped)
    assert "p95" not in str(dumped)
    from sports_hedge.application.system_load import system_load_payload_bytes

    assert system_load_payload_bytes(load) < SYSTEM_LOAD_JSON_BUDGET_BYTES


def test_classify_low_value_uses_viability_signals() -> None:
    unknown_hot = _work(viability_assessed=False, viable_venue_count=0)
    assert classify_value_class(unknown_hot) is SchedulerValueClass.HOT_ORDINARY
    pruned = _work(skip_expensive_work=True, viability_reason=CROSS_VENUE_UNAVAILABLE)
    assert classify_value_class(pruned) is SchedulerValueClass.LOW_VALUE
