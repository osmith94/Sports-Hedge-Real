"""Owner-live Windows liveness after competition sharding.

Startup UNIVERSE generation 0 is due immediately, together with HOT,
BACKGROUND and ACTIVE TRADE. A dense same-kickoff unresolved bridge used to
partition with no await and starve cheap handlers for well over 0.25s while
the process stayed alive.

Data class: synthetic fixture events. Not live, historical, or modelled quotes.
PAPER / read-only. EventMatcher thresholds and provider concurrency are unchanged.
"""

from __future__ import annotations

import asyncio
import time
from datetime import UTC, datetime, timedelta

import pytest
from loop_liveness_harness import CallbackProfiler

from sports_hedge.application.collector import DEFAULT_PROVIDER_CONCURRENCY
from sports_hedge.application.event_loop_activity import LOOP_ACTIVITY, reset_loop_activity
from sports_hedge.application.fixture_clusters import VenueEvent
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.application.target_competitions import TARGET_COMPETITIONS
from sports_hedge.application.universe_identity_shards import (
    UNRESOLVED_COMPETITION,
    cluster_events_sharded,
    partition_identity_shards,
    partition_identity_shards_cooperative,
)
from sports_hedge.config import get_settings
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.events import DEFAULT_EVENT_MATCH_THRESHOLD, EventMatcher
from sports_hedge.persistence.lane_venue_settings import SqliteLaneVenueSettingsStore
from sports_hedge.persistence.operator_scanner_settings import SqliteOperatorScannerSettingsStore
from sports_hedge.persistence.operator_universe_scope import SqliteOperatorUniverseScopeStore
from sports_hedge.persistence.universe_checkpoint import SqliteUniverseCheckpointStore

KICKOFF = datetime(2026, 9, 21, 15, 0, tzinfo=UTC)
COMPETITIONS = [item.display_name for item in TARGET_COMPETITIONS]
PROBE_INTERVAL_S = 0.02
LIVENESS_BOUND_S = 0.25


def _event(
    venue: VenueName,
    source_event_id: str,
    *,
    home: str,
    away: str,
    competition: str,
) -> VenueEvent:
    canonical = CanonicalEvent(
        sport="football",
        competition=competition,
        home_team=home,
        away_team=away,
        kickoff_utc=KICKOFF,
        source_venue=venue,
        source_event_id=source_event_id,
    )
    return VenueEvent(
        venue=venue,
        raw={"id": source_event_id, "title": f"{home} vs {away}"},
        canonical=canonical,
        source_event_id=source_event_id,
    )


def _same_kickoff_universe(*, per_competition: int, unresolved: int) -> list[VenueEvent]:
    """One kickoff window across every registry competition, plus an unresolved pile.

    Names do not share a 3-letter canopy, so the bridge scans the full pair
    space instead of attaching early. That is the pathological owner shape.
    """

    return list(_iter_same_kickoff_universe(per_competition=per_competition, unresolved=unresolved))


def _iter_same_kickoff_universe(*, per_competition: int, unresolved: int):
    venues = (VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI)
    for competition_index, competition in enumerate(COMPETITIONS):
        for fixture_index in range(per_competition):
            home = f"Homeclub{competition_index} Side{fixture_index}"
            away = f"Awayclub{competition_index} Side{fixture_index}"
            for venue in venues:
                yield _event(
                    venue,
                    f"{venue.value}-{competition_index}-{fixture_index}",
                    home=home,
                    away=away,
                    competition=competition,
                )
    for index in range(unresolved):
        yield _event(
            VenueName.POLYMARKET,
            f"unresolved-{index}",
            home=f"Zzxq{index} Wanderers",
            away=f"Qqyy{index} Athletic",
            competition="",
        )


async def _same_kickoff_universe_yielding(
    *, per_competition: int, unresolved: int
) -> list[VenueEvent]:
    """Build the pathological set without a single synchronous burst.

    Constructing every canonical event before the first await starved the
    loop for longer than the #519 0.25s bound while the partition phase
    itself stayed under that bound. Yielding here keeps the probe on the
    bridge and the cluster pass, which are the production sync slices.
    """

    items: list[VenueEvent] = []
    for index, item in enumerate(
        _iter_same_kickoff_universe(per_competition=per_competition, unresolved=unresolved),
        start=1,
    ):
        items.append(item)
        if index % 128 == 0:
            await asyncio.sleep(0)
    return items


def _signature(items: list[VenueEvent]) -> list[tuple[str, tuple[str, ...], int]]:
    partition = partition_identity_shards(items, kickoff_tolerance=timedelta(minutes=5))
    return [
        (
            shard.shard_id,
            tuple(item.source_event_id for item in shard.events),
            shard.unresolved_attached,
        )
        for shard in partition.shards
    ]


async def _scheduling_gaps(stop: asyncio.Event, gaps: list[float]) -> None:
    """Lateness of a short timer. Diagnostic only; CallbackProfiler is the SLA.

    ``wait_for(..., timeout=)`` overruns include (a) one blocking callback,
    (b) every other ready callback in the same ``_run_once``, and (c) the host
    not scheduling this process. Linux CI after ~3,700 tests has hit ~0.31s
    here while the same head's Windows liveness job and CallbackProfiler both
    stayed under 0.25s.
    """

    while not stop.is_set():
        started = time.perf_counter()
        try:
            await asyncio.wait_for(stop.wait(), timeout=PROBE_INTERVAL_S)
        except TimeoutError:
            pass
        elapsed = time.perf_counter() - started
        if stop.is_set() and elapsed + 1e-6 < PROBE_INTERVAL_S:
            return
        gaps.append(max(0.0, elapsed - PROBE_INTERVAL_S))


@pytest.mark.asyncio
async def test_cooperative_partition_matches_sync_attachment() -> None:
    items = [
        _event(
            VenueName.MATCHBOOK,
            "mb-arsenal",
            home="Arsenal",
            away="Tottenham",
            competition="English Premier League",
        ),
        _event(
            VenueName.KALSHI,
            "k-madrid",
            home="Real Madrid",
            away="Barcelona",
            competition="Spain La Liga",
        ),
        _event(
            VenueName.POLYMARKET,
            "pm-bridge",
            home="Arsenal",
            away="Wanderers",
            competition="",
        ),
        _event(
            VenueName.POLYMARKET,
            "pm-nowhere",
            home="Zzxq Wanderers",
            away="Qqyy Athletic",
            competition="",
        ),
    ]
    sync_items = [
        _event(
            item.venue,
            item.source_event_id,
            home=item.canonical.home_team,
            away=item.canonical.away_team,
            competition=item.canonical.competition,
        )
        for item in items
    ]
    cooperative = await partition_identity_shards_cooperative(
        items, kickoff_tolerance=timedelta(minutes=5)
    )
    assert _signature(sync_items) == [
        (
            shard.shard_id,
            tuple(item.source_event_id for item in shard.events),
            shard.unresolved_attached,
        )
        for shard in cooperative.shards
    ]
    premier = next(
        shard
        for shard in cooperative.shards
        if shard.competition_code != UNRESOLVED_COMPETITION and "premier" in shard.shard_id
    )
    assert premier.unresolved_attached == 1
    assert any(shard.unresolved for shard in cooperative.shards)


@pytest.mark.asyncio
async def test_startup_dense_shard_keeps_api_scheduling_gap_under_quarter_second(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Server-owned loops plus a pathological UNIVERSE bridge stay under 0.25s.

    Ground truth is ``CallbackProfiler`` (each asyncio callback). A wait_for
    timer probe is recorded for diagnosis; Linux CI host lateness has exceeded
    0.25s here without a >=0.25s application callback.

    ACTIVE TRADE still has to mark ``scheduler`` during the bridge. The first
    paper-settlement ledger import is stubbed so Windows CI does not confuse
    that one-shot thread work with worker starvation. STREAM is not started.
    """

    monkeypatch.setenv("PAPER_LIVE_REFRESH_ENABLED", "true")
    get_settings.cache_clear()
    # The serving process imports the paper API before the loop accepts
    # requests. Load it here so the probe measures worker slices, not the
    # one-shot module import.
    import sports_hedge.api.paper  # noqa: F401

    reset_loop_activity()
    operator = SqliteOperatorScannerSettingsStore(":memory:")
    venues = SqliteLaneVenueSettingsStore(":memory:")
    scope = SqliteOperatorUniverseScopeStore(":memory:")
    checkpoint = SqliteUniverseCheckpointStore(":memory:")
    coordinator = LiveRefreshCoordinator(
        operator_settings_store=operator,
        venue_settings_store=venues,
        universe_scope_store=scope,
        universe_checkpoint_store=checkpoint,
    )
    coordinator._seconds_until_hot = lambda: 0.01  # type: ignore[method-assign]
    coordinator._seconds_until_background = lambda: 0.01  # type: ignore[method-assign]
    coordinator._seconds_until_active_trade = lambda: 0.01  # type: ignore[method-assign]
    async def skip_first_ledger_import() -> None:
        # First real settlement builds the paper SQLite ledger in a worker
        # thread. On Windows CI that import can outlast this dense UNIVERSE
        # bridge, so ACTIVE TRADE never reaches its scheduler mark even though
        # HOT/BACKGROUND keep ticking. That is a test race, not lane
        # starvation. issue572 stubs the same hook. Cadence is unchanged.
        await asyncio.sleep(0)

    monkeypatch.setattr(coordinator, "_maybe_run_paper_settlement", skip_first_ledger_import)
    done = asyncio.Event()
    baseline: dict[tuple[str, str], int] = {}
    partition_holder: dict[str, object] = {}

    async def tick(plan=None) -> None:
        lane = getattr(plan, "lane", None)
        if lane != ScanLane.UNIVERSE.value:
            await asyncio.sleep(0)
            return
        if partition_holder:
            await asyncio.sleep(0)
            return
        baseline.update(LOOP_ACTIVITY.mark_counts)
        dense = await _same_kickoff_universe_yielding(per_competition=50, unresolved=1800)
        partition_holder["partition"] = await partition_identity_shards_cooperative(
            dense, kickoff_tolerance=timedelta(minutes=5)
        )
        modest = await _same_kickoff_universe_yielding(per_competition=3, unresolved=40)
        matchbook = [item for item in modest if item.venue is VenueName.MATCHBOOK]
        polymarket = [item for item in modest if item.venue is VenueName.POLYMARKET]
        kalshi = [item for item in modest if item.venue is VenueName.KALSHI]
        await cluster_events_sharded(
            matchbook=matchbook,
            polymarket=polymarket,
            kalshi=kalshi,
            matcher=EventMatcher(),
            max_event_pairs=10_000_000,
        )
        done.set()

    gaps: list[float] = []
    stop_probe = asyncio.Event()
    profile_report = ""
    longest_s = 0.0
    longest_non_gc_s = 0.0
    longest_iteration_s = 0.0
    over = []
    try:
        settings = get_settings()
        assert settings.paper_live_refresh_enabled is True
        assert settings.sports_hedge_execution_enabled is False
        assert DEFAULT_EVENT_MATCH_THRESHOLD == 0.92
        assert DEFAULT_PROVIDER_CONCURRENCY == {
            VenueName.MATCHBOOK: 4,
            VenueName.POLYMARKET: 8,
            VenueName.KALSHI: 4,
        }
        await coordinator.start_server_loop(tick)
        assert coordinator.universe_due_immediately() is True
        reset_loop_activity()
        assert coordinator._hot_task is not None
        assert coordinator._universe_task is not None
        assert coordinator._background_task is not None
        assert coordinator._active_trade_task is not None
        with CallbackProfiler(record_over_s=0.05, sample_after_s=0.08) as profiler:
            probe = asyncio.create_task(_scheduling_gaps(stop_probe, gaps))
            await asyncio.wait_for(done.wait(), timeout=60)
            stop_probe.set()
            await probe
            longest_s = profiler.profile.longest_s
            longest_non_gc_s = profiler.profile.longest_non_gc_s
            longest_iteration_s = profiler.profile.longest_iteration_s
            over = profiler.profile.over(LIVENESS_BOUND_S)
            profile_report = profiler.profile.report()
    finally:
        stop_probe.set()
        await coordinator.stop_server_loop()
        for store in (operator, venues, scope, checkpoint):
            store.close()
        get_settings.cache_clear()

    partition = partition_holder["partition"]
    assert hasattr(partition, "shards")
    assert len(partition.shards) >= len(COMPETITIONS)
    assert any(shard.unresolved for shard in partition.shards)
    assert partition.unresolved_attached == 0
    assert ("universe", "partition") in LOOP_ACTIVITY.phases_seen
    assert ("universe", "candidate_build") in LOOP_ACTIVITY.phases_seen
    assert ("universe", "consider") in LOOP_ACTIVITY.phases_seen
    for lane in ("hot", "background", "active_trade"):
        during = LOOP_ACTIVITY.mark_counts.get((lane, "scheduler"), 0)
        before = baseline.get((lane, "scheduler"), 0)
        assert during > before, (
            f"{lane} did not run during the dense UNIVERSE bridge "
            f"(scheduler marks during={during} baseline={before} "
            f"all={dict(LOOP_ACTIVITY.mark_counts)})"
        )
    assert ("active_trade", "paper_settlement") in LOOP_ACTIVITY.phases_seen
    assert gaps, "health-equivalent probe never woke while workers ran"
    worst = max(gaps)
    phase = LOOP_ACTIVITY.longest
    print(
        "issue517_startup_dense "
        f"probes={len(gaps)} probe_worst_ms={int(worst * 1000)} "
        f"longest_callback_ms={int(longest_s * 1000)} "
        f"longest_non_gc_callback_ms={int(longest_non_gc_s * 1000)} "
        f"longest_iteration_ms={int(longest_iteration_s * 1000)} "
        f"callbacks_over_250ms={len(over)} "
        f"phase_clock={None if phase is None else phase}"
    )
    assert over == [], (
        f"{len(over)} event-loop callbacks >= 0.25s during startup UNIVERSE\n{profile_report}"
    )
    assert longest_non_gc_s < LIVENESS_BOUND_S, profile_report
    # Closed phase-clock slices are application work. The wait_for probe is not:
    # Linux CI after the full suite recorded probe 0.308s / longest_phase=partition
    # without this profiler, while Windows liveness on the same head stayed at
    # longest_callback 146ms. Do not treat host timer lateness as a stall.
    assert phase is not None
    assert phase.elapsed_s < LIVENESS_BOUND_S, phase
