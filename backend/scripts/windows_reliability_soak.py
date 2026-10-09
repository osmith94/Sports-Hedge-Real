"""Execution-disabled synthetic soak for a Windows operator machine.

This is not a live scan and not a windows-latest result unless the process
itself is running on Windows. Synthetic ``RealisticUniverse`` providers do not
stand in for live venue traffic.

The harness refuses to start when execution is enabled. It does not submit
venue orders, does not call the real paper settlement path, and does not turn
garbage collection off. HOT, BACKGROUND, and UNIVERSE workers are the
production scheduler. The UNIVERSE tick runs ``collect_and_scan`` against the
synthetic providers. HOT and BACKGROUND ticks record overlap and do not call
live books. ACTIVE trade monitoring increments a counter in place of
``_maybe_run_paper_settlement``.

A fresh price slot is checked, once the timed section has stopped, against a
persisted economics quote. That check is the same clocks as the issue 14
regression: an 80ms slot quote must not make the stored edge executable.

Default duration is 1800 seconds. A shorter ``--seconds`` run only proves the
process starts. UNIVERSE due time stays on the production interval (default
180 seconds).

Setup, from the repository root, with execution left disabled::

    python backend/scripts/windows_reliability_soak.py --seconds 1800
"""

from __future__ import annotations

import argparse
import asyncio
import gc
import json
import os
import platform
import sys
import threading
import time
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seconds", type=float, default=1800.0)
    parser.add_argument("--fixtures", type=int, default=941)
    parser.add_argument("--watched", type=int, default=100)
    parser.add_argument("--stress-watched", type=int, default=500)
    parser.add_argument("--latency", type=float, default=0.0)
    parser.add_argument("--heartbeat-interval", type=float, default=2.0)
    parser.add_argument("--stress-every", type=float, default=60.0)
    parser.add_argument("--out", type=Path, default=Path("windows-soak-result.json"))
    return parser.parse_args()


def _execution_enabled() -> bool:
    raw = os.environ.get("SPORTS_HEDGE_EXECUTION_ENABLED", "").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _install_paths() -> None:
    backend = Path(__file__).resolve().parents[1]
    for path in (backend / "tests", backend / "src"):
        entry = str(path)
        if entry in sys.path:
            sys.path.remove(entry)
        sys.path.insert(0, entry)


def _percentile(samples: list[float], pct: float) -> float | None:
    if not samples:
        return None
    ordered = sorted(samples)
    rank = min(len(ordered) - 1, max(0, int(round((pct / 100) * (len(ordered) - 1)))))
    return ordered[rank]


def _ms(value: float | None) -> float | None:
    if value is None:
        return None
    return round(value * 1000, 1)


def _rss_mb() -> float | None:
    if os.name == "nt":
        try:
            import ctypes
            from ctypes import wintypes

            class _Counters(ctypes.Structure):
                _fields_ = [
                    ("cb", wintypes.DWORD),
                    ("PageFaultCount", wintypes.DWORD),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            counters = _Counters()
            counters.cb = ctypes.sizeof(counters)
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                ctypes.windll.kernel32.GetCurrentProcess(),
                ctypes.byref(counters),
                counters.cb,
            )
            if ok:
                return round(counters.WorkingSetSize / 1_000_000, 1)
        except Exception:
            return None
        return None
    try:
        with open("/proc/self/status", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("VmRSS:"):
                    return round(int(line.split()[1]) / 1024, 1)
    except OSError:
        return None
    return None


def _summary(samples: list[float]) -> dict[str, float | int | None]:
    return {
        "n": len(samples),
        "p50_ms": _ms(_percentile(samples, 50)),
        "p95_ms": _ms(_percentile(samples, 95)),
        "max_ms": _ms(max(samples) if samples else None),
    }


def main() -> None:
    args = _parse_args()
    if args.seconds <= 0 or args.fixtures <= 0:
        raise SystemExit("seconds and fixtures must be positive")
    if _execution_enabled():
        raise SystemExit("refusing to soak with SPORTS_HEDGE_EXECUTION_ENABLED set")
    _install_paths()
    os.environ["PAPER_LIVE_REFRESH_ENABLED"] = "true"

    from sports_hedge.api.watchlist import get_bet_ticket_operations, tracked_markets
    from sports_hedge.application.collector import MarketEvaluationState
    from sports_hedge.application.event_loop_activity import (
        LOOP_ACTIVITY,
        install_gc_pause_monitor,
    )
    from sports_hedge.application.live_refresh import get_live_refresh_coordinator
    from sports_hedge.application.scan_lanes import ScanLane
    from sports_hedge.arbitrage.watchlist.models import WatchLeg, WatchObservation
    from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
    from sports_hedge.arbitrage.watchlist.service import WatchlistService
    from sports_hedge.config import Settings, get_settings
    from sports_hedge.domain.football import FootballPeriod, MarketFamily
    from sports_hedge.domain.models import VenueName
    from sports_hedge.paper.models import FxRateSnapshot
    from loop_liveness_harness import RealisticUniverse
    from test_issue14_monitor_freshness import (
        T0,
        _decision,
        _named_row,
        _observation,
        _publish,
    )
    from test_issue200_universe_hot_promotion import _fixture, _market_row, _report
    from test_issue572_event_loop_stalls import _collector

    get_settings.cache_clear()
    settings = get_settings()
    if bool(settings.sports_hedge_execution_enabled) or Settings().sports_hedge_execution_enabled:
        raise SystemExit("refusing to soak with sports_hedge_execution_enabled")

    install_gc_pause_monitor()
    gc_before = [dict(stat) for stat in gc.get_stats()]
    rss_samples: list[float] = []
    started_rss = _rss_mb()
    if started_rss is not None:
        rss_samples.append(started_rss)
    cpu_started = time.process_time()
    wall_started = time.perf_counter()

    stress_watched = min(args.stress_watched, args.fixtures)
    watched = min(args.watched, stress_watched)

    async def kalshi_off() -> dict[str, object]:
        universe = RealisticUniverse(args.fixtures, latency_s=args.latency)
        collector, repository = _collector(universe)
        try:
            report = await collector.collect_and_scan(
                fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
                maximum_execution_risk=100,
                max_event_pairs=10_000_000,
                scan_lane=ScanLane.UNIVERSE.value,
                universe_generation_id=1,
                unbounded_cycle=True,
                enabled_venues=[VenueName.MATCHBOOK, VenueName.POLYMARKET],
            )
        finally:
            repository.close()
        kalshi_calls = {key: count for key, count in universe.calls.items() if key.startswith("kalshi.")}
        health = report.venue_health
        kalshi_health = health.get("kalshi") if isinstance(health, dict) else None
        return {
            "fixtures": args.fixtures,
            "kalshi_calls": kalshi_calls,
            "kalshi_call_total": sum(kalshi_calls.values()),
            "matchbook_list_events": int(universe.calls["matchbook.list_events"]),
            "polymarket_list_events": int(universe.calls["polymarket.list_events"]),
            "kalshi_health": None if kalshi_health is None else str(kalshi_health),
            "enabled_venues": [str(item) for item in report.enabled_venues],
            "providers": "synthetic RealisticUniverse",
        }

    print("phase=kalshi_off", flush=True)
    kalshi_report = asyncio.run(kalshi_off())
    if int(kalshi_report["kalshi_call_total"]) != 0:
        raise SystemExit(f"Kalshi OFF made calls: {kalshi_report['kalshi_calls']}")
    print(
        f"phase=kalshi_off kalshi_calls=0 "
        f"matchbook_list_events={kalshi_report['matchbook_list_events']} "
        f"polymarket_list_events={kalshi_report['polymarket_list_events']}",
        flush=True,
    )

    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: datetime.now(UTC))
    store = coordinator.fixture_current_state()
    seed_started = time.perf_counter()
    seeded_at = datetime.now(UTC)
    # Wall clock is 2026-10-08 in this investigation. The shared fixture helper's
    # kickoff is three days after 2026-09-16, which is already outside the radar
    # ceiling. Keep the seeded catalogue pre-kickoff for the soak window.
    kickoff = seeded_at + timedelta(hours=8)

    def _row(event_id: str):
        row = _market_row(edge=Decimal("0.004"), arb=False, trigger=Decimal("0.01"), quote_age_ms=80)
        assert row.matchbook is not None and row.polymarket is not None
        return row.model_copy(
            update={
                "matchbook": row.matchbook.model_copy(update={"source_market_id": f"mb-{event_id}"}),
                "polymarket": row.polymarket.model_copy(
                    update={"source_market_id": f"pm-{event_id}"}
                ),
            }
        )

    def _observe(event_id: str, when: datetime) -> WatchObservation:
        return WatchObservation(
            observed_at=when,
            canonical_event_id=event_id,
            canonical_market_id=f"mkt-{event_id}",
            competition="NFL",
            home_team="Home",
            away_team="Away",
            market_family=MarketFamily.BOTH_TEAMS_TO_SCORE,
            period=FootballPeriod.FULL_TIME,
            venues=[VenueName.MATCHBOOK, VenueName.POLYMARKET],
            legs=[
                WatchLeg(
                    outcome="yes",
                    venue=VenueName.MATCHBOOK,
                    source_market_id=f"mb-{event_id}",
                    currency="GBP",
                    net_decimal_odds=Decimal("1.90"),
                ),
                WatchLeg(
                    outcome="no",
                    venue=VenueName.POLYMARKET,
                    source_market_id=f"pm-{event_id}",
                    currency="USD",
                    net_decimal_odds=Decimal("2.05"),
                ),
            ],
            trigger_net_edge=Decimal("0.01"),
            current_net_edge=Decimal("0.004"),
            quote_age_ms=400,
            quote_age_basis="source",
            data_kind="live_paper",
        )

    for index in range(args.fixtures):
        event_id = f"fx-{index:04d}"
        when = seeded_at
        report = _report(
            [
                _fixture(
                    event_id,
                    when=when,
                    kickoff=kickoff,
                    opportunity="near",
                    arb=False,
                    qualifying=0,
                )
            ],
            when=when,
            markets={event_id: [_row(event_id)]},
            decisions=[_decision(event_id, f"mkt-{event_id}", when=when)],
        )
        store.upsert_from_report(report, scan_lane=ScanLane.UNIVERSE, now=when, pricing_refresh=False)
        if index < stress_watched:
            service.observe(_observe(event_id, when))
    seed_s = time.perf_counter() - seed_started
    print(f"phase=seed fixtures={args.fixtures} watched={stress_watched} seed_s={seed_s:.1f}", flush=True)

    def _tracked_payload(limit: int, active_service: WatchlistService) -> list[dict[str, object]]:
        rows = tracked_markets(
            limit=limit,
            competition=None,
            venue=None,
            market_family=None,
            service=active_service,
            operations=get_bet_ticket_operations(),
        )
        dumped: list[dict[str, object]] = []
        for row in rows:
            dumped.append(row.model_dump(mode="json"))
        return dumped

    stop = threading.Event()
    heartbeats: list[float] = []
    heartbeat_gaps: list[float] = []
    heartbeat_errors = 0
    tracked_100: list[float] = []
    tracked_500: list[float] = []
    tracked_rows: dict[str, int] = {"normal": 0, "stress": 0}
    tracked_errors = 0
    probe_waits: list[float] = []
    reprice_samples: list[float] = []
    reprice_errors = 0

    def heartbeat() -> None:
        nonlocal heartbeat_errors
        inflight = False
        previous: float | None = None
        while not stop.is_set():
            tick = time.perf_counter()
            if previous is not None:
                heartbeat_gaps.append(tick - previous)
            previous = tick
            rss = _rss_mb()
            if rss is not None:
                rss_samples.append(rss)
            if inflight:
                heartbeat_errors += 1
            else:
                inflight = True
                began = time.perf_counter()
                try:
                    coordinator.public_heartbeat()
                except Exception:
                    heartbeat_errors += 1
                else:
                    heartbeats.append(time.perf_counter() - began)
                inflight = False
            remaining = args.heartbeat_interval - (time.perf_counter() - tick)
            if remaining > 0:
                stop.wait(remaining)

    def tracked() -> None:
        nonlocal tracked_errors
        next_stress = time.perf_counter() + args.stress_every
        while not stop.is_set():
            tick = time.perf_counter()
            limit = watched
            bucket = tracked_100
            label = "normal"
            if tick >= next_stress:
                limit = stress_watched
                bucket = tracked_500
                label = "stress"
                next_stress = tick + args.stress_every
            began = time.perf_counter()
            try:
                body = _tracked_payload(limit, service)
                elapsed = time.perf_counter() - began
                bucket.append(elapsed)
                tracked_rows[label] = len(body)
            except Exception:
                tracked_errors += 1
            remaining = args.heartbeat_interval - (time.perf_counter() - tick)
            if remaining > 0:
                stop.wait(remaining)

    def contender() -> None:
        while not stop.is_set():
            began = time.perf_counter()
            acquired = store._lock.acquire(timeout=30)
            waited = time.perf_counter() - began
            if acquired:
                store._lock.release()
                probe_waits.append(waited)
            if stop.wait(0.05):
                return

    def repricer() -> None:
        nonlocal reprice_errors
        event_id = "fx-0000"
        step = 0
        while not stop.is_set():
            step += 1
            when = datetime.now(UTC)
            report = _report(
                [
                    _fixture(
                        event_id,
                        when=when,
                        kickoff=kickoff,
                        opportunity="near",
                        arb=False,
                        qualifying=0,
                    )
                ],
                when=when,
                markets={event_id: [_row(event_id)]},
                decisions=[_decision(event_id, f"mkt-{event_id}", when=when)],
            )
            began = time.perf_counter()
            try:
                store.upsert_from_report(
                    report,
                    scan_lane=ScanLane.UNIVERSE,
                    now=when,
                    pricing_refresh=True,
                )
            except Exception:
                reprice_errors += 1
            else:
                reprice_samples.append(time.perf_counter() - began)
            if stop.wait(1.0):
                return

    def retain_catalogue() -> None:
        """Re-stamp discovery time so the 360s UNIVERSE TTL does not drop the poll set.

        This is not a provider scan and not a BACKGROUND pricing pass.
        """

        while not stop.wait(60):
            when = datetime.now(UTC)
            for index in range(args.fixtures):
                if stop.is_set():
                    return
                event_id = f"fx-{index:04d}"
                report = _report(
                    [
                        _fixture(
                            event_id,
                            when=when,
                            kickoff=kickoff,
                            opportunity="near",
                            arb=False,
                            qualifying=0,
                        )
                    ],
                    when=when,
                    markets={event_id: [_row(event_id)]},
                    decisions=[_decision(event_id, f"mkt-{event_id}", when=when)],
                )
                store.upsert_from_report(
                    report,
                    scan_lane=ScanLane.UNIVERSE,
                    now=when,
                    pricing_refresh=False,
                )

    lane_counts = {"hot": 0, "background": 0, "active": 0, "universe": 0}
    lane_reasons: dict[str, int] = {}
    universe_skipped = 0
    cycles: list[dict[str, object]] = []
    cycle_errors = 0
    universe_running = False

    async def fake_settlement(*_args: object, **_kwargs: object) -> None:
        lane_counts["active"] += 1

    async def run_lanes() -> int:
        nonlocal universe_running, universe_skipped, cycle_errors
        universe = RealisticUniverse(args.fixtures, latency_s=args.latency, hot_every=25)
        collector, scan_repository = _collector(universe)
        coordinator.mark_startup_pricing_ready()
        coordinator._maybe_run_paper_settlement = fake_settlement  # type: ignore[method-assign]

        async def tick(plan: object = None) -> None:
            nonlocal universe_running, universe_skipped, cycle_errors
            lane = getattr(plan, "lane", None)
            reason = getattr(plan, "reason", None)
            lane_reasons[f"{lane}:{reason}"] = lane_reasons.get(f"{lane}:{reason}", 0) + 1
            if lane == ScanLane.UNIVERSE.value:
                lane_counts["universe"] += 1
                if universe_running:
                    universe_skipped += 1
                    return
                universe_running = True
                began = time.perf_counter()
                error: str | None = None
                discovered = 0
                evaluated = 0
                try:
                    async def runner() -> object:
                        on_discovery, on_fixture, on_work = coordinator.universe_collect_callbacks()
                        return await collector.collect_and_scan(
                            fx_snapshots=[
                                FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))
                            ],
                            maximum_execution_risk=100,
                            max_event_pairs=10_000_000,
                            scan_lane=ScanLane.UNIVERSE.value,
                            universe_generation_id=1,
                            unbounded_cycle=True,
                            enabled_venues=[
                                VenueName.MATCHBOOK,
                                VenueName.KALSHI,
                                VenueName.POLYMARKET,
                            ],
                            on_discovery_complete=on_discovery,
                            on_fixture_evaluated=on_fixture,
                            on_canonical_work_set=on_work,
                        )

                    report = await coordinator.run_cycle(
                        runner, timeout_seconds=None, scan_lane=ScanLane.UNIVERSE
                    )
                    discovered = len(report.discovered_fixtures)
                    evaluated = sum(
                        1
                        for item in report.discovered_fixtures
                        if item.market_evaluation_state == MarketEvaluationState.EVALUATED.value
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    error = type(exc).__name__
                    cycle_errors += 1
                finally:
                    universe_running = False
                if len(cycles) < 200:
                    cycles.append(
                        {
                            "elapsed_s": round(time.perf_counter() - began, 3),
                            "discovered": discovered,
                            "evaluated": evaluated,
                            "orphans": int(getattr(collector, "_inflight_orphaned", 0)),
                            "error": error,
                            "synthetic_kalshi_calls": int(
                                sum(
                                    count
                                    for key, count in universe.calls.items()
                                    if str(key).startswith("kalshi.")
                                )
                            ),
                        }
                    )
                return
            if lane in {ScanLane.HOT.value, "background"}:
                lane_counts[str(lane)] += 1
            await asyncio.sleep(0)

        await coordinator.start_server_loop(tick)
        if not coordinator.status.server_loop_enabled:
            raise RuntimeError("server loop did not start; PAPER_LIVE_REFRESH_ENABLED was not applied")
        try:
            await asyncio.sleep(args.seconds)
        finally:
            await coordinator.stop_server_loop()
            scan_repository.close()
        current = asyncio.current_task()
        return len([task for task in asyncio.all_tasks() if task is not current and not task.done()])

    threads = [
        threading.Thread(target=heartbeat, name="browser-heartbeat", daemon=True),
        threading.Thread(target=tracked, name="tracked-poll", daemon=True),
        threading.Thread(target=contender, name="lock-probe", daemon=True),
        threading.Thread(target=repricer, name="background-price", daemon=True),
        threading.Thread(target=retain_catalogue, name="catalogue-retain", daemon=True),
    ]
    for thread in threads:
        thread.start()
    print(f"phase=soak seconds={args.seconds} fixtures={args.fixtures}", flush=True)
    pending_tasks = 0
    try:
        pending_tasks = asyncio.run(run_lanes())
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=5)

    def stale_economics() -> dict[str, object]:
        """80ms price-slot quote must not relabel a 1,500ms stored edge."""

        probe_coordinator = get_live_refresh_coordinator()
        probe_coordinator.reset()
        probe_repository = SqliteWatchlistRepository()
        clock = {"at": T0}
        probe_service = WatchlistService(
            probe_repository, clock=lambda: clock["at"], max_quote_age_ms=2_000
        )
        probe_store = probe_coordinator.fixture_current_state()
        try:
            fresh_quote = _named_row("both_teams_to_score", "mb-a", "pm-a")
            _publish(
                probe_store,
                [fresh_quote],
                [_decision("issue14-dolphins-bengals", "mkt-a", when=T0)],
                T0,
                pricing_refresh=False,
            )
            probe_service.observe(
                _observation("mkt-a", ("mb-a", "pm-a"), T0).model_copy(
                    update={"quote_age_ms": 1_500, "current_net_edge": Decimal("0.018")}
                )
            )
            priced_at = T0 + timedelta(seconds=45)
            _publish(
                probe_store,
                [fresh_quote],
                [_decision("issue14-dolphins-bengals", "mkt-a", when=priced_at)],
                priced_at,
                pricing_refresh=True,
            )
            clock["at"] = priced_at
            body = {
                str(row["canonical_market_id"]): row
                for row in _tracked_payload(100, probe_service)
            }
            aged = body["mkt-a"]
            executable = aged["freshness_class"] == "executable" or bool(aged["bet_actionable"])
            return {
                "freshness_class": aged["freshness_class"],
                "bet_actionable": aged["bet_actionable"],
                "quote_age_ms": aged["quote_age_ms"],
                "current_net_edge": str(aged["current_net_edge"]),
                "price_lane": aged.get("price_lane"),
                "stale_edge_became_executable": executable,
            }
        finally:
            probe_coordinator.reset()
            probe_repository.close()

    print("phase=stale_economics", flush=True)
    stale = stale_economics()
    if stale["stale_edge_became_executable"]:
        raise SystemExit(f"fresh price made stale economics executable: {stale}")

    get_settings.cache_clear()
    finished = get_settings()
    wall_s = time.perf_counter() - wall_started
    cpu_s = time.process_time() - cpu_started
    gc_after = [dict(stat) for stat in gc.get_stats()]
    gc_pauses = {
        name: {
            "count": stat.count,
            "total_ms": round(stat.total_s * 1000, 1),
            "max_ms": round(stat.max_s * 1000, 1),
        }
        for name, stat in LOOP_ACTIVITY.gc_pauses.items()
    }
    starved = sum(1 for gap in heartbeat_gaps if gap > args.heartbeat_interval + 0.5)
    evaluated_short = [
        cycle for cycle in cycles if cycle["error"] is None and int(cycle["discovered"]) != args.fixtures
    ]
    payload = {
        "data_class": "synthetic_fixture",
        "live_providers": False,
        "execution_enabled_at_end": bool(finished.sports_hedge_execution_enabled),
        "orders_submitted": 0,
        "platform": platform.platform(),
        "python": sys.version.split()[0],
        "windows": os.name == "nt",
        "seconds_requested": args.seconds,
        "wall_s": round(wall_s, 1),
        "cpu_s": round(cpu_s, 1),
        "rss_mb": {
            "start": started_rss,
            "max": max(rss_samples) if rss_samples else None,
            "end": rss_samples[-1] if rss_samples else None,
            "samples": len(rss_samples),
        },
        "seed_s": round(seed_s, 3),
        "fixtures": args.fixtures,
        "watched_poll": watched,
        "stress_poll": stress_watched,
        "universe_interval_s": "production default (180s unless settings change it)",
        "lane_ticks": lane_counts,
        "lane_plan_reasons": lane_reasons,
        "universe_cycles_recorded": len(cycles),
        "universe_cycles_skipped_while_busy": universe_skipped,
        "universe_cycle_errors": cycle_errors,
        "universe_cycles_short_of_catalogue": len(evaluated_short),
        "universe_cycle_elapsed": _summary(
            [float(cycle["elapsed_s"]) for cycle in cycles if cycle["error"] is None]
        ),
        "universe_cycles": cycles,
        "heartbeat": _summary(heartbeats),
        "heartbeat_gap": _summary(heartbeat_gaps),
        "heartbeat_gaps_over_interval_plus_500ms": starved,
        "heartbeat_errors": heartbeat_errors,
        "tracked_100": _summary(tracked_100),
        "tracked_500": _summary(tracked_500),
        "tracked_rows_last": tracked_rows,
        "tracked_errors": tracked_errors,
        "fixture_lock_wait": _summary(probe_waits),
        "background_reprice": _summary(reprice_samples),
        "background_reprice_errors": reprice_errors,
        "background_reprice_note": "pricing_refresh upsert of one fixture, not a provider lane pass",
        "gc_stats_before": gc_before,
        "gc_stats_after": gc_after,
        "gc_pauses": gc_pauses,
        "pending_asyncio_tasks_after_stop": pending_tasks,
        "kalshi_off": kalshi_report,
        "stale_economics": stale,
        "active_trade": "counter replaced _maybe_run_paper_settlement; no settlement write",
        "note": (
            "Synthetic providers. A non-Windows run is not the required Windows soak. "
            "HOT and BACKGROUND ticks record scheduler overlap and do not walk live books."
        ),
    }
    text = json.dumps(payload, indent=2, sort_keys=True)
    args.out.write_text(text + "\n", encoding="utf-8")
    print(text, flush=True)
    if not payload["windows"] and args.seconds >= 1800:
        print(
            "SOAK_PROCESS_FINISHED_NON_WINDOWS: do not treat this file as the Windows evidence",
            flush=True,
        )


if __name__ == "__main__":
    main()
