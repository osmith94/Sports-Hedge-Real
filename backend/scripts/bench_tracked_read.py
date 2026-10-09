"""Apples-to-apples read of ``/paper/watchlist/tracked``.

Fixture/demo catalogue only. No provider calls. Refuses to run when
``SPORTS_HEDGE_EXECUTION_ENABLED`` is true.

The three SHAs under comparison do not do the same work:

- ``main`` looks up radar meta once per returned row and has no price clock.
- ``f3b255c`` does that lookup and also walks the catalogue once per row for
  ``market_price_clock``.
- ``463a7d2`` builds radar meta and price clocks in one pass.

Request time therefore includes diagnostic clock fields on the two PR SHAs
and not on ``main``. The script records that difference instead of subtracting
it.

Setup, from any checkout, with this script (it does not have to live in the
SHA under test)::

    python3 backend/scripts/bench_tracked_read.py \\
        --src /path/to/sha/backend/src \\
        --tests /path/to/sha/backend/tests \\
        --watched 100 --repeats 20 --label main

A background thread repeatedly applies ``pricing_refresh=True`` for one
fixture. That is the current-state write a BACKGROUND reprice performs. It is
not a provider-backed lane pass. A second thread calls ``public_heartbeat``
on the same single-flight 2s cadence the browser uses for ``/paper/live-refresh``.
That call is the body of the live-refresh poll. While tracked reads run back
to back, one heartbeat sample can span the whole burst, so the sample count
matters more than a single maximum. A third thread times how long it waits
to acquire ``fixture_current_state``.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--src", type=Path, required=True)
    parser.add_argument("--tests", type=Path, required=True)
    parser.add_argument("--watched", type=int, required=True)
    parser.add_argument("--repeats", type=int, default=20)
    parser.add_argument("--fixtures", type=int, default=941)
    parser.add_argument("--label", required=True)
    parser.add_argument("--heartbeat-interval", type=float, default=2.0)
    return parser.parse_args()


def _install_paths(src: Path, tests: Path) -> None:
    for path in (str(tests.resolve()), str(src.resolve())):
        if path in sys.path:
            sys.path.remove(path)
        sys.path.insert(0, path)


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


def main() -> None:
    args = _parse_args()
    if os.environ.get("SPORTS_HEDGE_EXECUTION_ENABLED", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        raise SystemExit("refusing to benchmark with SPORTS_HEDGE_EXECUTION_ENABLED set")
    _install_paths(args.src, args.tests)

    from fastapi.testclient import TestClient

    from sports_hedge.api.main import app
    from sports_hedge.api.watchlist import get_watchlist_service
    from sports_hedge.application.event_loop_activity import TimedRLock
    from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
    from sports_hedge.application.live_refresh import get_live_refresh_coordinator
    from sports_hedge.application.scan_lanes import ScanLane
    from sports_hedge.arbitrage.watchlist.models import WatchLeg, WatchObservation
    from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
    from sports_hedge.arbitrage.watchlist.service import WatchlistService
    from sports_hedge.config import get_settings
    from sports_hedge.domain.football import FootballPeriod, MarketFamily
    from sports_hedge.domain.models import VenueName
    from test_issue200_universe_hot_promotion import (  # noqa: E402
        NOW,
        _decision,
        _fixture,
        _market_row,
        _report,
    )

    settings = get_settings()
    if bool(getattr(settings, "sports_hedge_execution_enabled", False)):
        raise SystemExit("refusing to benchmark with sports_hedge_execution_enabled")

    holds: list[tuple[str, str, float, float]] = []
    probe_waits: list[float] = []
    local = threading.local()
    original_acquire = TimedRLock.acquire
    original_release = TimedRLock.release

    def acquire(self: TimedRLock, blocking: bool = True, timeout: float = -1) -> bool:
        depth = getattr(local, "depth", 0)
        started = time.perf_counter()
        acquired = original_acquire(self, blocking, timeout)
        waited = time.perf_counter() - started
        if not acquired:
            if self.name == "fixture_current_state":
                probe_waits.append(waited)
            return False
        if depth == 0:
            local.hold_start = time.perf_counter()
            local.hold_lock = self.name
        local.depth = depth + 1
        return True

    def release(self: TimedRLock) -> None:
        depth = getattr(local, "depth", 1) - 1
        local.depth = depth
        if depth == 0 and getattr(local, "hold_lock", None) == "fixture_current_state":
            holds.append(
                (
                    threading.current_thread().name,
                    self.name,
                    local.hold_start,
                    time.perf_counter() - local.hold_start,
                )
            )
        original_release(self)

    TimedRLock.acquire = acquire  # type: ignore[method-assign]
    TimedRLock.release = release  # type: ignore[method-assign]

    def _row(event_id: str):
        row = _market_row(edge=Decimal("0.004"), arb=False, trigger=Decimal("0.01"), quote_age_ms=80)
        assert row.matchbook is not None and row.polymarket is not None
        return row.model_copy(
            update={
                "matchbook": row.matchbook.model_copy(update={"source_market_id": f"mb-{event_id}"}),
                "polymarket": row.polymarket.model_copy(update={"source_market_id": f"pm-{event_id}"}),
            }
        )

    def _observe(event_id: str) -> WatchObservation:
        return WatchObservation(
            observed_at=NOW,
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

    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    # The browser heartbeat reads coordinator.now(). Leaving that on the wall
    # clock evicts this September fixture catalogue. Freeze it on the same
    # instant the watchlist uses. The background upsert still passes its own
    # later timestamp, which is the reprice under test.
    coordinator._clock = lambda: NOW
    repository = SqliteWatchlistRepository()
    service = WatchlistService(repository, clock=lambda: NOW)
    store = coordinator.fixture_current_state()
    seed_started = time.perf_counter()
    for index in range(args.fixtures):
        event_id = f"fx-{index:04d}"
        report = _report(
            [_fixture(event_id, when=NOW, opportunity="near", arb=False, qualifying=0)],
            when=NOW,
            markets={event_id: [_row(event_id)]},
            decisions=[_decision(event_id, f"mkt-{event_id}", when=NOW)],
        )
        store.upsert_from_report(report, scan_lane=ScanLane.UNIVERSE, now=NOW, pricing_refresh=False)
        if index < args.watched:
            service.observe(_observe(event_id))
    seed_s = time.perf_counter() - seed_started

    app.dependency_overrides[get_watchlist_service] = lambda: service
    client = TestClient(app)
    stop = threading.Event()
    updates: list[float] = []
    update_errors = 0
    heartbeats: list[float] = []
    heartbeat_gaps: list[float] = []
    heartbeat_skips = 0

    def contender() -> None:
        while not stop.is_set():
            began = time.perf_counter()
            acquired = store._lock.acquire(timeout=60)
            waited = time.perf_counter() - began
            if acquired:
                store._lock.release()
                probe_waits.append(waited)
            time.sleep(0.001)

    def updater() -> None:
        nonlocal update_errors
        event_id = "fx-0000"
        step = 0
        while not stop.is_set():
            step += 1
            when = NOW + timedelta(seconds=step)
            report = _report(
                [_fixture(event_id, when=when, opportunity="near", arb=False, qualifying=0)],
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
                update_errors += 1
            else:
                updates.append(time.perf_counter() - began)

    def heartbeat() -> None:
        nonlocal heartbeat_skips
        inflight = False
        previous: float | None = None
        while not stop.is_set():
            tick = time.perf_counter()
            if previous is not None:
                heartbeat_gaps.append(tick - previous)
            previous = tick
            if inflight:
                heartbeat_skips += 1
            else:
                inflight = True
                began = time.perf_counter()
                try:
                    coordinator.public_heartbeat()
                except Exception:
                    pass
                else:
                    heartbeats.append(time.perf_counter() - began)
                inflight = False
            remaining = args.heartbeat_interval - (time.perf_counter() - tick)
            if remaining > 0 and not stop.wait(remaining):
                continue
            if stop.is_set():
                return

    threads = [
        threading.Thread(target=contender, name="lock-probe", daemon=True),
        threading.Thread(target=updater, name="background-price", daemon=True),
        threading.Thread(target=heartbeat, name="browser-heartbeat", daemon=True),
    ]
    for thread in threads:
        thread.start()
    samples: list[float] = []
    occupancies: list[float] = []
    request_longest_holds: list[float] = []
    priced = 0
    rows_returned = 0
    has_price_field = False
    try:
        for index in range(args.repeats + 1):
            before = len(holds)
            began = time.perf_counter()
            response = client.get(f"/paper/watchlist/tracked?limit={args.watched}")
            elapsed = time.perf_counter() - began
            body = response.json()
            # Sync endpoints run on the TestClient worker, not MainThread.
            mine = [
                held
                for thread_name, _lock_name, _started, held in holds[before:]
                if thread_name not in {"lock-probe", "background-price", "browser-heartbeat"}
            ]
            if index == 0:
                continue
            samples.append(elapsed)
            occupancies.append(sum(mine))
            request_longest_holds.append(max(mine) if mine else 0.0)
            rows_returned = len(body)
            if body:
                has_price_field = "last_priced_at" in body[0]
                priced = sum(1 for row in body if row.get("last_priced_at"))
            if response.status_code != 200:
                raise SystemExit(f"tracked status {response.status_code}")
            print(
                f"sample {index} rows {len(body)} elapsed_ms {elapsed * 1000:.1f} "
                f"lock_occupancy_ms {sum(mine) * 1000:.1f}",
                flush=True,
            )
    finally:
        stop.set()
        for thread in threads:
            thread.join(timeout=2)
        app.dependency_overrides.clear()
        coordinator.reset()
        repository.close()
        TimedRLock.acquire = original_acquire  # type: ignore[method-assign]
        TimedRLock.release = original_release  # type: ignore[method-assign]

    def _role(role: str) -> list[float]:
        return [held for thread_name, _lock_name, _started, held in holds if thread_name == role]

    background_holds = _role("background-price")
    heartbeat_holds = _role("browser-heartbeat")
    result = {
        "label": args.label,
        "sha": subprocess.check_output(
            ["git", "-C", str(args.src), "rev-parse", "HEAD"], text=True
        ).strip(),
        "fixtures": args.fixtures,
        "watched": args.watched,
        "repeats": len(samples),
        "warmup_discarded": 1,
        "seed_s": round(seed_s, 2),
        "rows_returned": rows_returned,
        "response_has_last_priced_at": has_price_field,
        "rows_with_last_priced_at": priced,
        "has_market_price_clock": hasattr(FixtureCurrentStateStore, "market_price_clock"),
        "has_tracked_annotations": hasattr(FixtureCurrentStateStore, "tracked_annotations"),
        "execution_enabled": False,
        "percentile": "nearest-rank on sorted samples, rank round(p/100*(n-1))",
        "http_p50_ms": _ms(_percentile(samples, 50)),
        "http_p95_ms": _ms(_percentile(samples, 95)),
        "http_max_ms": _ms(max(samples) if samples else None),
        "request_lock_occupancy_p50_ms": _ms(_percentile(occupancies, 50)),
        "request_lock_occupancy_p95_ms": _ms(_percentile(occupancies, 95)),
        "request_longest_hold_p95_ms": _ms(_percentile(request_longest_holds, 95)),
        "probe_lock_wait_p50_ms": _ms(_percentile(probe_waits, 50)),
        "probe_lock_wait_p95_ms": _ms(_percentile(probe_waits, 95)),
        "probe_lock_wait_max_ms": _ms(max(probe_waits) if probe_waits else None),
        "probe_samples": len(probe_waits),
        "background_upserts": len(updates),
        "background_errors": update_errors,
        "background_p50_ms": _ms(_percentile(updates, 50)),
        "background_p95_ms": _ms(_percentile(updates, 95)),
        "background_max_ms": _ms(max(updates) if updates else None),
        "background_lock_hold_p95_ms": _ms(_percentile(background_holds, 95)),
        "heartbeat_samples": len(heartbeats),
        "heartbeat_p50_ms": _ms(_percentile(heartbeats, 50)),
        "heartbeat_p95_ms": _ms(_percentile(heartbeats, 95)),
        "heartbeat_max_ms": _ms(max(heartbeats) if heartbeats else None),
        "heartbeat_gap_p95_ms": _ms(_percentile(heartbeat_gaps, 95)),
        "heartbeat_skips": heartbeat_skips,
        "heartbeat_lock_hold_p95_ms": _ms(_percentile(heartbeat_holds, 95)),
        "heartbeat_interval_s": args.heartbeat_interval,
    }
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()
