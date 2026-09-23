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

    items: list[VenueEvent] = []
    venues = (VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI)
    for competition_index, competition in enumerate(COMPETITIONS):
        for fixture_index in range(per_competition):
            home = f"Homeclub{competition_index} Side{fixture_index}"
            away = f"Awayclub{competition_index} Side{fixture_index}"
            for venue in venues:
                items.append(
                    _event(
                        venue,
                        f"{venue.value}-{competition_index}-{fixture_index}",
                        home=home,
                        away=away,
                        competition=competition,
                    )
                )
    for index in range(unresolved):
        items.append(
            _event(
                VenueName.POLYMARKET,
                f"unresolved-{index}",
                home=f"Zzxq{index} Wanderers",
                away=f"Qqyy{index} Athletic",
                competition="",
            )
        )
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
    """Lateness of a short sleep. A synchronous stretch shows up as overrun."""

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
    """Server-owned loops plus a pathological UNIVERSE bridge stay under 0.25s."""

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
        dense = _same_kickoff_universe(per_competition=50, unresolved=1800)
        partition_holder["partition"] = await partition_identity_shards_cooperative(
            dense, kickoff_tolerance=timedelta(minutes=5)
        )
        modest = _same_kickoff_universe(per_competition=3, unresolved=40)
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
        probe = asyncio.create_task(_scheduling_gaps(stop_probe, gaps))
        await asyncio.wait_for(done.wait(), timeout=60)
        stop_probe.set()
        await probe
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
        assert during > before, f"{lane} did not run during the dense UNIVERSE bridge"
    assert ("active_trade", "paper_settlement") in LOOP_ACTIVITY.phases_seen
    assert gaps, "health-equivalent probe never woke while workers ran"
    worst = max(gaps)
    assert worst < LIVENESS_BOUND_S, (
        f"event loop starved for {worst:.3f}s during startup UNIVERSE "
        f"(probes={len(gaps)} longest_phase="
        f"{None if LOOP_ACTIVITY.longest is None else LOOP_ACTIVITY.longest.phase})"
    )
    assert LOOP_ACTIVITY.longest is not None
    assert LOOP_ACTIVITY.longest.elapsed_s < LIVENESS_BOUND_S
