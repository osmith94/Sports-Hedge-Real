"""Ground-truth event-loop liveness tooling for #572.

``CallbackProfiler`` times every asyncio callback the loop actually runs. That
is the real synchronous slice: the time between two points where the loop can
serve another coroutine (for example ``GET /health``). It does not depend on
``mark_loop_phase`` bookkeeping, which can span awaits and can be attributed to
whichever lane marked last.

A sampler thread records the loop thread's Python stack while one callback has
been running for longer than ``sample_after_s``. The deepest repository frames
of those samples name the exact blocking call. Each slow callback also carries
the GC time spent inside it, and ``longest_iteration_s`` is the busiest single
loop iteration (all callbacks that ran between two selector waits), which is
what a health request queued behind them actually waits for.

Liveness SLA (see ``extreme_stress_liveness_failures``):

- Normal scanner SLA, every workload: no non-GC synchronous slice >= 0.25s.
  ``HeartbeatProbe.worst_non_gc_s`` is the health-timer form of that SLA.
  Raw ``worst_s`` still reports wall-clock lateness, which can include
  interpreter gen2 of the workload's own unfrozen allocations even after
  ``gc.freeze()`` of earlier tests. That is not permission to raise 0.25s.
- Representative ~275-fixture workload: no callback >= 0.25s including GC.
  Heartbeat non-GC lateness stays < 0.25s. The 0.25s bound is not relaxed.
- Extreme synthetic stress only (for example 1,600-fixture identity): a
  scheduling gap may reach < 0.50s only when the part of it that is not
  Python garbage collection stays < 0.25s, and no single GC pause reaches
  0.50s.

``RealisticUniverse`` is a synthetic owner-shaped workload: N distinct football
fixtures across three registry competitions, each listed on Matchbook, Kalshi
and Polymarket with the four locked Matchbook↔Kalshi families (1X2, BTTS,
exact-line TOTAL, FTTS) plus non-catalogue Matchbook exotics.

Data class: synthetic fixture/demo providers. Not live, historical, or modelled
venue quotes. PAPER / read-only.
"""

from __future__ import annotations

import asyncio
import gc
import sys
import threading
import time
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Self

from sports_hedge.domain.models import VenueHealth, VenueName

_REPO_MARK = "sports_hedge"


@dataclass
class SlowCallback:
    elapsed_s: float
    label: str
    lane: str
    phase: str
    gc_s: float = 0.0


@dataclass
class CallbackProfile:
    callbacks: int = 0
    total_s: float = 0.0
    slow: list[SlowCallback] = field(default_factory=list)
    samples: Counter = field(default_factory=Counter)
    leaf_samples: Counter = field(default_factory=Counter)
    longest_iteration_s: float = 0.0

    @property
    def longest_s(self) -> float:
        return max((item.elapsed_s for item in self.slow), default=0.0)

    @property
    def longest_non_gc_s(self) -> float:
        return max((item.elapsed_s - item.gc_s for item in self.slow), default=0.0)

    def over(self, bound_s: float) -> list[SlowCallback]:
        return [item for item in self.slow if item.elapsed_s >= bound_s]

    def report(self, *, top: int = 12) -> str:
        header = (
            f"callbacks={self.callbacks} busy_s={self.total_s:.3f} "
            f"longest_ms={int(self.longest_s * 1000)} "
            f"longest_non_gc_ms={int(self.longest_non_gc_s * 1000)} "
            f"longest_iteration_ms={int(self.longest_iteration_s * 1000)}"
        )
        lines = [header]
        for item in sorted(self.slow, key=lambda s: s.elapsed_s, reverse=True)[:top]:
            lines.append(
                f"  {int(item.elapsed_s * 1000):>6}ms (gc {int(item.gc_s * 1000)}ms) "
                f"lane={item.lane} phase={item.phase} {item.label}"
            )
        if self.leaf_samples:
            lines.append("  hottest repository frames while a callback was long:")
            for frame, count in self.leaf_samples.most_common(top):
                lines.append(f"    {count:>5}  {frame}")
        return "\n".join(lines)


def _callback_label(handle: asyncio.Handle) -> str:
    callback = getattr(handle, "_callback", None)
    owner = getattr(callback, "__self__", None)
    if isinstance(owner, asyncio.Task):
        coro = owner.get_coro()
        code = getattr(coro, "cr_code", None) or getattr(coro, "gi_code", None)
        if code is not None:
            return f"task:{code.co_qualname}"
        return f"task:{owner.get_name()}"
    return repr(callback)[:120]


class CallbackProfiler:
    """Patch ``asyncio.Handle._run`` for the loop thread and time every callback."""

    def __init__(
        self,
        *,
        record_over_s: float = 0.05,
        sample_after_s: float = 0.1,
        sample_interval_s: float = 0.01,
    ) -> None:
        self.record_over_s = record_over_s
        self.sample_after_s = sample_after_s
        self.sample_interval_s = sample_interval_s
        self.profile = CallbackProfile()
        self._original = None
        self._loop_thread: int | None = None
        self._current_started: float | None = None
        self._stop = threading.Event()
        self._sampler: threading.Thread | None = None
        self._gc_total_s = 0.0
        self._gc_started: float | None = None
        self._original_run_once = None
        self._iteration_busy_s = 0.0

    def _gc_callback(self, phase: str, _info: dict[str, Any]) -> None:
        if threading.get_ident() != self._loop_thread:
            return
        if phase == "start":
            self._gc_started = time.perf_counter()
        elif self._gc_started is not None:
            self._gc_total_s += time.perf_counter() - self._gc_started
            self._gc_started = None

    def __enter__(self) -> Self:
        from sports_hedge.application.event_loop_activity import LOOP_ACTIVITY

        self._loop_thread = threading.get_ident()
        original = asyncio.events.Handle._run
        self._original = original
        profiler = self
        gc.callbacks.append(self._gc_callback)
        original_run_once = asyncio.base_events.BaseEventLoop._run_once
        self._original_run_once = original_run_once

        def timed_run_once(loop: asyncio.AbstractEventLoop) -> None:
            if threading.get_ident() != profiler._loop_thread:
                return original_run_once(loop)
            profiler._iteration_busy_s = 0.0
            try:
                return original_run_once(loop)
            finally:
                profile = profiler.profile
                profile.longest_iteration_s = max(
                    profile.longest_iteration_s, profiler._iteration_busy_s
                )

        def timed_run(handle: asyncio.Handle) -> None:
            if threading.get_ident() != profiler._loop_thread:
                return original(handle)
            started = time.perf_counter()
            gc_before = profiler._gc_total_s
            profiler._current_started = started
            lane = LOOP_ACTIVITY.current.lane
            phase = LOOP_ACTIVITY.current.phase
            try:
                return original(handle)
            finally:
                profiler._current_started = None
                elapsed = time.perf_counter() - started
                profile = profiler.profile
                profile.callbacks += 1
                profile.total_s += elapsed
                profiler._iteration_busy_s += elapsed
                if elapsed >= profiler.record_over_s:
                    profile.slow.append(
                        SlowCallback(
                            elapsed,
                            _callback_label(handle),
                            lane,
                            phase,
                            gc_s=profiler._gc_total_s - gc_before,
                        )
                    )

        asyncio.events.Handle._run = timed_run  # type: ignore[method-assign]
        asyncio.base_events.BaseEventLoop._run_once = timed_run_once  # type: ignore[method-assign]
        self._sampler = threading.Thread(target=self._sample, daemon=True)
        self._sampler.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._sampler is not None:
            self._sampler.join(timeout=2)
        if self._gc_callback in gc.callbacks:
            gc.callbacks.remove(self._gc_callback)
        if self._original is not None:
            asyncio.events.Handle._run = self._original  # type: ignore[method-assign]
        if self._original_run_once is not None:
            asyncio.base_events.BaseEventLoop._run_once = self._original_run_once  # type: ignore[method-assign]

    def _sample(self) -> None:
        while not self._stop.wait(self.sample_interval_s):
            started = self._current_started
            if started is None or (time.perf_counter() - started) < self.sample_after_s:
                continue
            frame = sys._current_frames().get(self._loop_thread or -1)
            stack: list[str] = []
            while frame is not None:
                code = frame.f_code
                if _REPO_MARK in code.co_filename:
                    short = code.co_filename.split(_REPO_MARK, 1)[-1].lstrip("/\\")
                    stack.append(f"{short}:{frame.f_lineno} {code.co_qualname}")
                frame = frame.f_back
            if not stack:
                continue
            self.profile.leaf_samples[stack[0]] += 1
            self.profile.samples[" <- ".join(stack[:6])] += 1


class HeartbeatProbe:
    """A cheap coroutine that must keep running, like ``GET /health``.

    Each beat records wall-clock lateness and the event-loop-thread GC that
    ran in that interval, using the same subtraction as
    ``probe_gc_attributed_gaps``. ``worst_s`` is raw lateness; the 0.25s
    scanner SLA is ``worst_non_gc_s``.
    """

    def __init__(self, interval_s: float = 0.02) -> None:
        self.interval_s = interval_s
        self.lateness: list[float] = []
        self.gaps: list[SchedulingGap] = []
        self.beats = 0
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        from sports_hedge.application.event_loop_activity import (
            gc_pause_total_seconds,
            install_gc_pause_monitor,
        )

        install_gc_pause_monitor()
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            due = loop.time() + self.interval_s
            gc_before = gc_pause_total_seconds()
            await asyncio.sleep(self.interval_s)
            gc_after = gc_pause_total_seconds()
            gap = max(0.0, loop.time() - due)
            gc_s = max(0.0, gc_after - gc_before)
            self.beats += 1
            self.lateness.append(gap)
            self.gaps.append(SchedulingGap(gap, gc_s))

    def start(self) -> HeartbeatProbe:
        self._task = asyncio.create_task(self._run())
        return self

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task

    @property
    def worst_s(self) -> float:
        return max(self.lateness, default=0.0)

    @property
    def worst_non_gc_s(self) -> float:
        return max((gap.non_gc_s for gap in self.gaps), default=0.0)

    def report(self) -> str:
        worst = max(self.gaps, key=lambda gap: gap.gap_s, default=SchedulingGap(0.0, 0.0))
        return (
            f"heartbeat_worst_ms={int(self.worst_s * 1000)} "
            f"heartbeat_worst_non_gc_ms={int(self.worst_non_gc_s * 1000)} "
            f"heartbeat_worst_gap_gc_ms={int(worst.gc_s * 1000)} "
            f"beats={self.beats}"
        )


def stream_runtime_is_unstarted() -> bool:
    """STREAM Phase 1 stays off until Send to STREAM. Liveness benches must not start it."""

    from sports_hedge.application.stream import runtime as stream_runtime

    shared = stream_runtime._SHARED
    if shared is None:
        return True
    status = shared.status()
    return (
        not status.enabled
        and status.connection_status in {"not_selected", "disabled"}
        and shared.observer is None
        and status.paper_opened is False
        and status.orders_placed is False
    )


NORMAL_LIVENESS_SLA_S = 0.25
EXTREME_STRESS_GC_ALLOWANCE_S = 0.50


@dataclass(frozen=True)
class SchedulingGap:
    """Lateness of one short timer wake-up and the loop-thread GC inside that cycle."""

    gap_s: float
    gc_s: float

    @property
    def non_gc_s(self) -> float:
        return max(0.0, self.gap_s - self.gc_s)


async def probe_gc_attributed_gaps(
    stop: asyncio.Event, *, interval_s: float = 0.005
) -> list[SchedulingGap]:
    """Health-style wake-up lateness with the exact GC time recorded in each cycle.

    The probe always has a pending wake-up, so every loop stall delays some
    sample. GC comes from the production GC monitor's monotonic total, so
    attribution is a subtraction, not a timestamp match. Read order charges any
    GC between a clock read and a counter read to the gap, not to GC; GC that
    finished before the wake-up was due can be over-credited by at most
    ``interval_s``.
    """

    from sports_hedge.application.event_loop_activity import (
        gc_pause_total_seconds,
        install_gc_pause_monitor,
    )

    install_gc_pause_monitor()
    loop = asyncio.get_running_loop()
    gaps: list[SchedulingGap] = []
    while not stop.is_set():
        due = loop.time() + interval_s
        gc_before = gc_pause_total_seconds()
        await asyncio.sleep(interval_s)
        gc_after = gc_pause_total_seconds()
        woke = loop.time()
        gaps.append(SchedulingGap(max(0.0, woke - due), max(0.0, gc_after - gc_before)))
    return gaps


def extreme_stress_liveness_failures(
    gaps: list[SchedulingGap],
    callbacks: list[SlowCallback],
    *,
    longest_gc_pause_s: float,
) -> list[str]:
    """Extreme synthetic stress policy. Application code keeps the normal 0.25s SLA.

    - any gap whose non-GC part is >= 0.25s fails;
    - any callback whose non-GC time is >= 0.25s fails;
    - any gap >= 0.50s fails, GC or not;
    - any single GC pause >= 0.50s fails.
    A gap >= 0.25s passes only when GC explains everything above 0.25s.
    """

    failures: list[str] = []
    for gap in gaps:
        if gap.non_gc_s >= NORMAL_LIVENESS_SLA_S:
            failures.append(
                f"non-GC scheduling gap {int(gap.non_gc_s * 1000)}ms "
                f"(gap {int(gap.gap_s * 1000)}ms, gc {int(gap.gc_s * 1000)}ms)"
            )
        if gap.gap_s >= EXTREME_STRESS_GC_ALLOWANCE_S:
            failures.append(
                f"scheduling gap {int(gap.gap_s * 1000)}ms >= "
                f"{int(EXTREME_STRESS_GC_ALLOWANCE_S * 1000)}ms (gc {int(gap.gc_s * 1000)}ms)"
            )
    for item in callbacks:
        non_gc = item.elapsed_s - item.gc_s
        if non_gc >= NORMAL_LIVENESS_SLA_S:
            failures.append(
                f"non-GC callback {int(non_gc * 1000)}ms "
                f"(callback {int(item.elapsed_s * 1000)}ms, gc {int(item.gc_s * 1000)}ms) "
                f"lane={item.lane} phase={item.phase} {item.label}"
            )
    if longest_gc_pause_s >= EXTREME_STRESS_GC_ALLOWANCE_S:
        failures.append(
            f"GC pause {int(longest_gc_pause_s * 1000)}ms >= "
            f"{int(EXTREME_STRESS_GC_ALLOWANCE_S * 1000)}ms"
        )
    return failures


def extreme_stress_summary(
    label: str,
    gaps: list[SchedulingGap],
    callbacks: list[SlowCallback],
    *,
    longest_gc_pause_s: float,
) -> str:
    """One line stating whether any >=0.25s excursion was GC-attributed."""

    excursions = [gap for gap in gaps if gap.gap_s >= NORMAL_LIVENESS_SLA_S]
    gc_attributed = [gap for gap in excursions if gap.non_gc_s < NORMAL_LIVENESS_SLA_S]
    worst = max(gaps, key=lambda gap: gap.gap_s, default=SchedulingGap(0.0, 0.0))
    worst_non_gc_gap = max((gap.non_gc_s for gap in gaps), default=0.0)
    worst_non_gc_callback = max(
        (item.elapsed_s - item.gc_s for item in callbacks), default=0.0
    )
    return (
        f"{label} probes={len(gaps)} worst_gap_ms={int(worst.gap_s * 1000)} "
        f"worst_gap_gc_ms={int(worst.gc_s * 1000)} "
        f"worst_non_gc_gap_ms={int(worst_non_gc_gap * 1000)} "
        f"worst_non_gc_callback_ms={int(worst_non_gc_callback * 1000)} "
        f"longest_gc_pause_ms={int(longest_gc_pause_s * 1000)} "
        f"excursions_over_250ms={len(excursions)} "
        f"gc_attributed_excursions={len(gc_attributed)} "
        f"non_gc_excursions={len(excursions) - len(gc_attributed)}"
    )


# ---------------------------------------------------------------------------
# Realistic owner-shaped universe
# ---------------------------------------------------------------------------

REGULATION = (
    "If the match is abandoned or postponed the market resolves per Kalshi rules. "
    "Only 90 minutes of regulation time plus stoppage time count."
)
COMPETITIONS: tuple[tuple[str, str, str], ...] = (
    ("Premier League", "KXEPL", "10188"),
    ("Spain La Liga", "KXLALIGA", "10193"),
    ("English Championship", "KXEFLCHAMPIONSHIP", "10355"),
)
TOTAL_LINES = ("0.5", "1.5", "2.5", "3.5", "4.5", "5.5")
MB_EXOTICS = (
    "Draw No Bet",
    "Double Chance",
    "Half Time",
    "Half Time/Full Time",
    "Correct Score",
    "Asian Handicap -0.5",
    "Asian Handicap +0.5",
    "Asian Handicap -1.5",
    "Asian Handicap +1.5",
    "Winning Margin",
    "Total Corners",
    "Total Cards",
)
_WORDS = (
    "Ashford", "Brampton", "Carlow", "Dunmore", "Elstree", "Fenwick", "Garston",
    "Harlow", "Ilkley", "Jarrow", "Kendal", "Lydney", "Malden", "Newlyn",
    "Oakham", "Penrith", "Quorn", "Redcar", "Selby", "Tadley", "Ulverston",
    "Ventnor", "Whitby", "Yeovil", "Zennor",
)
_SUFFIX = ("Rovers", "Albion", "Athletic", "Wanderers", "Town", "United", "City", "Rangers")


def _team(index: int, side: int) -> str:
    word = _WORDS[(index * 7 + side * 3) % len(_WORDS)]
    word2 = _WORDS[(index // len(_WORDS) + side * 11) % len(_WORDS)]
    suffix = _SUFFIX[(index + side * 5) % len(_SUFFIX)]
    return f"{word}{word2[:3].lower()} {suffix}"


@dataclass(frozen=True)
class SyntheticFixture:
    index: int
    competition: str
    kalshi_prefix: str
    gamma_series: str
    home: str
    away: str
    kickoff: datetime

    @property
    def mb_event_id(self) -> int:
        return 5_720_000 + self.index

    @property
    def kalshi_code(self) -> str:
        return f"26SEP{self.kickoff.day:02d}F{self.index:04d}"

    @property
    def pm_event_id(self) -> str:
        return f"pm-572-{self.index}"


def synthetic_fixtures(
    count: int, *, now: datetime | None = None, hot_every: int | None = None
) -> list[SyntheticFixture]:
    """``hot_every=k`` puts every k-th fixture 30 minutes from kickoff (lifecycle HOT)."""

    current = now or datetime.now(UTC)
    base = current.replace(minute=0, second=0, microsecond=0) + timedelta(days=1)
    fixtures: list[SyntheticFixture] = []
    for index in range(count):
        competition, prefix, gamma = COMPETITIONS[index % len(COMPETITIONS)]
        kickoff = base + timedelta(hours=(index // len(COMPETITIONS)) % 10, days=index // 30)
        if hot_every and index % hot_every == 0:
            kickoff = current.replace(second=0, microsecond=0) + timedelta(
                minutes=30 + index % 7
            )
        fixtures.append(
            SyntheticFixture(
                index=index,
                competition=competition,
                kalshi_prefix=prefix,
                gamma_series=gamma,
                home=_team(index, 0),
                away=_team(index, 1),
                kickoff=kickoff,
            )
        )
    return fixtures


def _mb_runner(runner_id: int, name: str, odds: str) -> dict[str, Any]:
    return {
        "id": runner_id,
        "name": name,
        "prices": [
            {"side": "back", "odds": odds, "available-amount": "120"},
            {"side": "lay", "odds": f"{float(odds) + 0.04:.2f}", "available-amount": "120"},
        ],
    }


def matchbook_markets(fixture: SyntheticFixture) -> list[dict[str, Any]]:
    base = fixture.mb_event_id * 100
    markets: list[dict[str, Any]] = [
        {
            "id": base + 1,
            "name": "Match Odds",
            "runners": [
                _mb_runner(base * 10 + 1, fixture.home, "2.40"),
                _mb_runner(base * 10 + 2, "Draw", "3.40"),
                _mb_runner(base * 10 + 3, fixture.away, "2.90"),
            ],
        },
        {
            "id": base + 2,
            "name": "Both Teams To Score",
            "runners": [
                _mb_runner(base * 10 + 11, "Yes", "1.90"),
                _mb_runner(base * 10 + 12, "No", "1.95"),
            ],
        },
        {
            "id": base + 3,
            "name": "First Team To Score",
            "runners": [
                _mb_runner(base * 10 + 21, fixture.home, "2.20"),
                _mb_runner(base * 10 + 22, fixture.away, "2.30"),
                _mb_runner(base * 10 + 23, "No Goal", "8.00"),
            ],
        },
    ]
    for offset, line in enumerate(TOTAL_LINES):
        markets.append(
            {
                "id": base + 10 + offset,
                "name": f"Over/Under {line} Goals",
                "line": line,
                "runners": [
                    _mb_runner(base * 10 + 100 + offset * 2, f"Over {line}", "1.85"),
                    _mb_runner(base * 10 + 101 + offset * 2, f"Under {line}", "2.05"),
                ],
            }
        )
    for offset, name in enumerate(MB_EXOTICS):
        markets.append(
            {
                "id": base + 40 + offset,
                "name": name,
                "runners": [
                    _mb_runner(base * 10 + 400 + offset * 3, fixture.home, "2.10"),
                    _mb_runner(base * 10 + 401 + offset * 3, fixture.away, "2.60"),
                ],
            }
        )
    return markets


def _kalshi_market(
    fixture: SyntheticFixture, family: str, suffix: str, sub_title: str, **extra: Any
) -> dict[str, Any]:
    event_ticker = f"{fixture.kalshi_prefix}{family}-{fixture.kalshi_code}"
    return {
        "ticker": f"{event_ticker}-{suffix}",
        "event_ticker": event_ticker,
        "title": f"{fixture.home} vs {fixture.away}",
        "yes_sub_title": sub_title,
        "rules_primary": REGULATION,
        **extra,
    }


def kalshi_events(fixture: SyntheticFixture) -> list[dict[str, Any]]:
    families: dict[str, list[dict[str, Any]]] = {
        "GAME": [
            _kalshi_market(fixture, "GAME", "H", fixture.home),
            _kalshi_market(fixture, "GAME", "TIE", "Draw"),
            _kalshi_market(fixture, "GAME", "A", fixture.away),
        ],
        "BTTS": [_kalshi_market(fixture, "BTTS", "BTTS", "Yes", title="Both Teams To Score")],
        "TOTAL": [
            _kalshi_market(
                fixture,
                "TOTAL",
                line,
                f"Over {line}",
                title=f"{fixture.home} vs {fixture.away} Total Goals {line}",
                strike=line,
            )
            for line in TOTAL_LINES
        ],
        "FTTS": [
            _kalshi_market(fixture, "FTTS", "H", fixture.home, title="First team to score"),
            _kalshi_market(fixture, "FTTS", "A", fixture.away, title="First team to score"),
            _kalshi_market(fixture, "FTTS", "NG", "No Goal", title="First team to score"),
        ],
    }
    events = []
    for family, markets in families.items():
        events.append(
            {
                "event_ticker": f"{fixture.kalshi_prefix}{family}-{fixture.kalshi_code}",
                "series_ticker": f"{fixture.kalshi_prefix}{family}",
                "title": f"{fixture.home} vs {fixture.away}",
                "category": "Sports",
                "strike_date": fixture.kickoff.isoformat(),
                "milestone": {"start_date": fixture.kickoff.isoformat()},
                "product_metadata": {
                    "competition": fixture.competition,
                    "competition_scope": "Game",
                },
                "markets": markets,
            }
        )
    return events


def polymarket_markets(fixture: SyntheticFixture) -> list[dict[str, Any]]:
    event_id = fixture.pm_event_id
    return [
        {
            "id": f"{event_id}-win-home",
            "question": f"Will {fixture.home} win on {fixture.kickoff.date().isoformat()}?",
            "sportsMarketType": "moneyline",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": f'["{event_id}-wh-y", "{event_id}-wh-n"]',
            "description": "Resolves on 90 minutes of regulation time.",
            "feesEnabled": False,
        },
        {
            "id": f"{event_id}-draw",
            "question": f"Will {fixture.home} vs. {fixture.away} end in a draw?",
            "sportsMarketType": "moneyline",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": f'["{event_id}-d-y", "{event_id}-d-n"]',
            "description": "Resolves on 90 minutes of regulation time.",
            "feesEnabled": False,
        },
        {
            "id": f"{event_id}-btts",
            "question": "Both teams to score?",
            "sportsMarketType": "both teams to score",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": f'["{event_id}-b-y", "{event_id}-b-n"]',
            "description": "Resolves based on 90 minutes of regulation time.",
            "feesEnabled": False,
        },
    ]


DEFAULT_KALSHI_BOOK = {
    "orderbook_fp": {
        "yes_dollars": [["0.40", "500.00"]],
        "no_dollars": [["0.55", "500.00"]],
    }
}


class RealisticUniverse:
    """Owner-shaped providers with a fixed per-call latency (asyncio.sleep)."""

    def __init__(
        self, fixture_count: int, *, latency_s: float = 0.02, hot_every: int | None = None
    ) -> None:
        self.fixtures = synthetic_fixtures(fixture_count, hot_every=hot_every)
        self.latency_s = latency_s
        self.calls: Counter = Counter()
        self._mb_events = [
            {
                "id": item.mb_event_id,
                "name": f"{item.home} vs {item.away}",
                "start": item.kickoff.isoformat(),
                "competition-name": item.competition,
            }
            for item in self.fixtures
        ]
        self._mb_markets = {str(item.mb_event_id): matchbook_markets(item) for item in self.fixtures}
        self._kalshi_events = [event for item in self.fixtures for event in kalshi_events(item)]
        self._kalshi_by_ticker = {
            str(event["event_ticker"]): event for event in self._kalshi_events
        }
        self._pm_events = [
            {
                "id": item.pm_event_id,
                "title": f"{item.home} vs. {item.away}",
                "startTime": item.kickoff.isoformat(),
                "competition": item.competition,
                "series": [{"id": item.gamma_series, "title": item.competition}],
            }
            for item in self.fixtures
        ]
        self._pm_markets = {item.pm_event_id: polymarket_markets(item) for item in self.fixtures}

    async def pace(self, key: str) -> None:
        self.calls[key] += 1
        if self.latency_s > 0:
            await asyncio.sleep(self.latency_s)

    def matchbook(self) -> RealisticMatchbook:
        return RealisticMatchbook(self)

    def kalshi(self) -> RealisticKalshi:
        return RealisticKalshi(self)

    def polymarket(self) -> RealisticPolymarket:
        return RealisticPolymarket(self)

    def series(self) -> dict[str, dict[str, Any]]:
        return {
            f"{prefix}{family}": {
                "ticker": f"{prefix}{family}",
                "title": f"{competition} {family}",
                "fee_type": "quadratic",
                "fee_multiplier": 1,
            }
            for competition, prefix, _gamma in COMPETITIONS
            for family in ("GAME", "BTTS", "TOTAL", "FTTS")
        }


class RealisticMatchbook:
    def __init__(self, universe: RealisticUniverse) -> None:
        self.universe = universe

    async def health(self) -> VenueHealth:
        return VenueHealth(
            venue=VenueName.MATCHBOOK, ok=True, authenticated=True, checked_at=datetime.now(UTC)
        )

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        await self.universe.pace("matchbook.list_events")
        return {"events": deepcopy(self.universe._mb_events)}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        await self.universe.pace("matchbook.list_markets")
        return {"markets": deepcopy(self.universe._mb_markets.get(str(event_id), []))}

    async def get_market(self, event_id: int | str, market_id: int | str, **filters: Any):
        del filters
        await self.universe.pace("matchbook.get_market")
        for market in self.universe._mb_markets.get(str(event_id), []):
            if str(market.get("id")) == str(market_id):
                return deepcopy(market)
        from sports_hedge.venues.matchbook import MatchbookMarketGoneError

        raise MatchbookMarketGoneError(event_id, market_id, 404)


class RealisticKalshi:
    def __init__(self, universe: RealisticUniverse) -> None:
        self.universe = universe

    async def health(self) -> VenueHealth:
        return VenueHealth(
            venue=VenueName.KALSHI, ok=True, authenticated=False, checked_at=datetime.now(UTC)
        )

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        await self.universe.pace("kalshi.list_events")
        events = self.universe._kalshi_events
        series_tickers = filters.get("series_tickers")
        if series_tickers:
            allowed = {str(item).strip() for item in series_tickers if str(item).strip()}
            events = [item for item in events if str(item.get("series_ticker")) in allowed]
        series_ticker = filters.get("series_ticker")
        if series_ticker:
            events = [item for item in events if str(item.get("series_ticker")) == series_ticker]
        return {"events": deepcopy(events)}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        await self.universe.pace("kalshi.list_markets")
        event = self.universe._kalshi_by_ticker.get(str(event_id))
        return {"markets": deepcopy((event or {}).get("markets") or [])}

    async def get_market(self, ticker: str) -> dict[str, Any]:
        await self.universe.pace("kalshi.get_market")
        event_ticker = str(ticker).rsplit("-", 1)[0]
        event = self.universe._kalshi_by_ticker.get(event_ticker) or {}
        for market in event.get("markets") or []:
            if str(market.get("ticker")) == str(ticker):
                return deepcopy(market)
        raise LookupError(ticker)

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        await self.universe.pace("kalshi.get_series")
        return deepcopy(self.universe.series().get(str(series_ticker), {"ticker": series_ticker}))

    async def get_order_book(self, event_id: int | str, market_id: int | str, *_a: Any, **_k: Any):
        del event_id
        await self.universe.pace("kalshi.get_order_book")
        return deepcopy(DEFAULT_KALSHI_BOOK)


class RealisticPolymarket:
    def __init__(self, universe: RealisticUniverse) -> None:
        self.universe = universe

    async def health(self) -> VenueHealth:
        return VenueHealth(
            venue=VenueName.POLYMARKET, ok=True, authenticated=False, checked_at=datetime.now(UTC)
        )

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        await self.universe.pace("polymarket.list_events")
        return deepcopy(self.universe._pm_events)

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del filters
        await self.universe.pace("polymarket.list_markets")
        return deepcopy(self.universe._pm_markets.get(str(event_id), []))

    async def get_order_book(
        self, event_id: int | str, market_id: int | str, outcome_id: Any = None, **_k: Any
    ) -> dict[str, Any]:
        del event_id, market_id
        await self.universe.pace("polymarket.get_order_book")
        return {
            "asset_id": str(outcome_id),
            "timestamp": int(datetime.now(UTC).timestamp() * 1000) - 20,
            "bids": [{"price": "0.45", "size": "200"}],
            "asks": [{"price": "0.47", "size": "200"}],
        }
