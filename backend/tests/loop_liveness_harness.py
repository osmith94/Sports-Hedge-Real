"""Ground-truth event-loop liveness tooling for #572.

``CallbackProfiler`` times every asyncio callback the loop actually runs. That
is the real synchronous slice: the time between two points where the loop can
serve another coroutine (for example ``GET /health``). It does not depend on
``mark_loop_phase`` bookkeeping, which can span awaits and can be attributed to
whichever lane marked last.

A sampler thread records the loop thread's Python stack while one callback has
been running for longer than ``sample_after_s``. The deepest repository frames
of those samples name the exact blocking call.

``RealisticUniverse`` is a synthetic owner-shaped workload: N distinct football
fixtures across three registry competitions, each listed on Matchbook, Kalshi
and Polymarket with the four locked Matchbook↔Kalshi families (1X2, BTTS,
exact-line TOTAL, FTTS) plus non-catalogue Matchbook exotics.

Data class: synthetic fixture/demo providers. Not live, historical, or modelled
venue quotes. PAPER / read-only.
"""

from __future__ import annotations

import asyncio
import sys
import threading
import time
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from sports_hedge.domain.models import VenueHealth, VenueName

_REPO_MARK = "sports_hedge"


@dataclass
class SlowCallback:
    elapsed_s: float
    label: str
    lane: str
    phase: str


@dataclass
class CallbackProfile:
    callbacks: int = 0
    total_s: float = 0.0
    slow: list[SlowCallback] = field(default_factory=list)
    samples: Counter = field(default_factory=Counter)
    leaf_samples: Counter = field(default_factory=Counter)

    @property
    def longest_s(self) -> float:
        return max((item.elapsed_s for item in self.slow), default=0.0)

    def over(self, bound_s: float) -> list[SlowCallback]:
        return [item for item in self.slow if item.elapsed_s >= bound_s]

    def report(self, *, top: int = 12) -> str:
        lines = [
            f"callbacks={self.callbacks} busy_s={self.total_s:.3f} "
            f"longest_ms={int(self.longest_s * 1000)}"
        ]
        for item in sorted(self.slow, key=lambda s: s.elapsed_s, reverse=True)[:top]:
            lines.append(
                f"  {int(item.elapsed_s * 1000):>6}ms lane={item.lane} phase={item.phase} "
                f"{item.label}"
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

    def __enter__(self) -> CallbackProfiler:
        from sports_hedge.application.event_loop_activity import LOOP_ACTIVITY

        self._loop_thread = threading.get_ident()
        original = asyncio.events.Handle._run
        self._original = original
        profiler = self

        def timed_run(handle: asyncio.Handle) -> None:
            if threading.get_ident() != profiler._loop_thread:
                return original(handle)
            started = time.perf_counter()
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
                if elapsed >= profiler.record_over_s:
                    profile.slow.append(
                        SlowCallback(elapsed, _callback_label(handle), lane, phase)
                    )

        asyncio.events.Handle._run = timed_run  # type: ignore[method-assign]
        self._sampler = threading.Thread(target=self._sample, daemon=True)
        self._sampler.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self._stop.set()
        if self._sampler is not None:
            self._sampler.join(timeout=2)
        if self._original is not None:
            asyncio.events.Handle._run = self._original  # type: ignore[method-assign]

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
    """A cheap coroutine that must keep running, like ``GET /health``."""

    def __init__(self, interval_s: float = 0.02) -> None:
        self.interval_s = interval_s
        self.lateness: list[float] = []
        self.beats = 0
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        while not self._stop.is_set():
            due = loop.time() + self.interval_s
            await asyncio.sleep(self.interval_s)
            self.beats += 1
            self.lateness.append(max(0.0, loop.time() - due))

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
