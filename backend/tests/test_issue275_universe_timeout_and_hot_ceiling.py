"""Issue #275 owner-live blockers: UNIVERSE drain-timeout cap and 4h HOT ceiling.

Deterministic fakes. Not owner-live evidence. PAPER/read-only only.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from time import monotonic
from typing import Any

import pytest

from sports_hedge.application.collector import (
    PROVIDER_CANCEL_DRAIN_SECONDS,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.hot_identity import hot_scheduling_key
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import (
    DEFAULT_POST_KICKOFF_CURRENT_RADAR_CEILING,
    EVICTION_CLOCK_EXPIRED_CURRENT_RADAR,
    ScanLane,
    classify_scan_lane,
    current_radar_eviction_reason,
    hot_reason_labels,
)
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from test_concurrent_hot_universe_workers import _named_fixture
from test_dual_cadence_scheduler import NOW, _report
from test_fixture_lifecycle_eviction import _fixture
from test_tenet19_production_guarantees import (
    ScriptedKalshi,
    ScriptedMatchbook,
    ScriptedPolymarket,
    _k_event,
    _paper,
)

SLOW_BUT_VALID_SECONDS = 0.12
CONFIGURED_PROVIDER_TIMEOUT = 0.8
CONFIGURED_VENUE_TIMEOUT = 0.8
HUNG_PROVIDER_TIMEOUT = 0.25
BETIS_KICKOFF = datetime(2026, 9, 17, 17, 4, tzinfo=UTC)
BETIS_OBSERVED = datetime(2026, 9, 17, 23, 5, tzinfo=UTC)


def _prep_wait_state(collector: ReadOnlyCrossVenueCollector) -> None:
    collector._inflight = set()
    collector._provider_calls = 0
    collector._provider_cancels = 0
    collector._inflight_orphaned = 0
    collector._peak_inflight = 0
    collector._op_deadline = None
    collector._op_soft_deadline = None


def _unbounded_collector(
    kalshi: ScriptedKalshi,
    *,
    venue_timeout: float = CONFIGURED_VENUE_TIMEOUT,
    provider_timeout: float = CONFIGURED_PROVIDER_TIMEOUT,
    cycle_timeout: float | None = None,
) -> ReadOnlyCrossVenueCollector:
    return ReadOnlyCrossVenueCollector(
        matchbook=ScriptedMatchbook([]),
        polymarket=ScriptedPolymarket([]),
        kalshi=kalshi,
        paper_scan=_paper(),
        venue_timeout_seconds=venue_timeout,
        provider_call_timeout_seconds=provider_timeout,
        cycle_timeout_seconds=cycle_timeout,
    )


class SlowSeriesKalshi(ScriptedKalshi):
    def __init__(self, delay_seconds: float, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.delay_seconds = delay_seconds
        self.call_elapsed: list[float] = []

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        started = monotonic()
        await asyncio.sleep(self.delay_seconds)
        payload = await super().list_events(**filters)
        self.call_elapsed.append(monotonic() - started)
        return payload


class HungSeriesKalshi(ScriptedKalshi):
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        await asyncio.sleep(30)
        return {"events": [], "milestones": []}


def test_provider_cancel_drain_is_fifty_milliseconds() -> None:
    assert PROVIDER_CANCEL_DRAIN_SECONDS == 0.05
    assert DEFAULT_POST_KICKOFF_CURRENT_RADAR_CEILING == timedelta(hours=4)
    assert Settings.model_fields["paper_hot_post_kickoff_current_radar_ceiling_hours"].default == 4


def test_unbounded_remaining_assembly_is_not_the_cancel_drain_allowance() -> None:
    """Root cause: no collector hard deadline must not pretend 50ms remains."""

    collector = _unbounded_collector(ScriptedKalshi({"KXEPLGAME": [], "KXLALIGA": []}))
    _prep_wait_state(collector)
    remaining = collector._remaining_assembly()
    assert remaining is None
    assert remaining != PROVIDER_CANCEL_DRAIN_SECONDS
    assert collector._timeout_budget(CONFIGURED_PROVIDER_TIMEOUT) == pytest.approx(
        CONFIGURED_PROVIDER_TIMEOUT
    )
    assert collector._discovery_timeout_budget(CONFIGURED_VENUE_TIMEOUT) == pytest.approx(
        CONFIGURED_VENUE_TIMEOUT
    )
    assert collector._cancel_drain_seconds() == PROVIDER_CANCEL_DRAIN_SECONDS


def test_hot_remaining_assembly_still_caps_to_cycle_envelope() -> None:
    collector = _unbounded_collector(
        ScriptedKalshi({"KXEPLGAME": [], "KXLALIGA": []}),
        cycle_timeout=0.4,
    )
    _prep_wait_state(collector)
    collector._op_deadline = monotonic() + 0.08
    collector._op_soft_deadline = collector._op_deadline
    remaining = collector._remaining_assembly()
    assert remaining is not None
    assert remaining <= 0.08 + 0.01
    budget = collector._timeout_budget(8.0)
    assert 0 < budget <= remaining + 0.01
    assert budget < 1.0


@pytest.mark.asyncio
async def test_unbounded_await_bounded_allows_provider_wait_above_drain() -> None:
    collector = _unbounded_collector(ScriptedKalshi({"KXEPLGAME": [], "KXLALIGA": []}))
    _prep_wait_state(collector)

    async def slow() -> str:
        await asyncio.sleep(SLOW_BUT_VALID_SECONDS)
        return "ok"

    started = monotonic()
    payload, timed_out = await collector._await_bounded(slow(), timeout=CONFIGURED_PROVIDER_TIMEOUT)
    elapsed = monotonic() - started
    assert timed_out is False
    assert payload == "ok"
    assert elapsed >= SLOW_BUT_VALID_SECONDS
    assert elapsed < CONFIGURED_PROVIDER_TIMEOUT


@pytest.mark.asyncio
async def test_unbounded_await_bounded_still_times_out_hung_provider_at_configured_bound() -> None:
    collector = _unbounded_collector(ScriptedKalshi({"KXEPLGAME": [], "KXLALIGA": []}))
    _prep_wait_state(collector)

    async def hung() -> str:
        await asyncio.sleep(30)
        return "nope"

    started = monotonic()
    payload, timed_out = await collector._await_bounded(hung(), timeout=HUNG_PROVIDER_TIMEOUT)
    elapsed = monotonic() - started
    assert timed_out is True
    assert payload is None
    assert elapsed >= HUNG_PROVIDER_TIMEOUT
    assert elapsed < HUNG_PROVIDER_TIMEOUT + 0.4
    assert elapsed > PROVIDER_CANCEL_DRAIN_SECONDS * 2


@pytest.mark.asyncio
async def test_hot_await_bounded_still_caps_to_remaining_assembly() -> None:
    collector = _unbounded_collector(
        ScriptedKalshi({"KXEPLGAME": [], "KXLALIGA": []}),
        cycle_timeout=0.4,
    )
    _prep_wait_state(collector)
    collector._op_deadline = monotonic() + 0.08
    collector._op_soft_deadline = collector._op_deadline

    async def slow() -> str:
        await asyncio.sleep(SLOW_BUT_VALID_SECONDS)
        return "ok"

    started = monotonic()
    payload, timed_out = await collector._await_bounded(slow(), timeout=8.0)
    elapsed = monotonic() - started
    assert timed_out is True
    assert payload is None
    assert elapsed < SLOW_BUT_VALID_SECONDS
    assert elapsed < 0.25


@pytest.mark.asyncio
async def test_unbounded_universe_kalshi_series_slower_than_drain_succeeds() -> None:
    kalshi = SlowSeriesKalshi(
        SLOW_BUT_VALID_SECONDS,
        events_by_series={
            "KXEPLGAME": [_k_event("KXEPL-NEW-CHE", "Newcastle", "Chelsea")],
            "KXLALIGA": [_k_event("KXLALIGA-BET-GET", "Real Betis", "Getafe", series="KXLALIGA")],
        },
    )
    collector = _unbounded_collector(kalshi)
    started = monotonic()
    report = await collector.collect_and_scan(
        scan_lane=ScanLane.UNIVERSE.value,
        unbounded_cycle=True,
        enabled_venues=[VenueName.KALSHI],
        max_event_pairs=8,
    )
    elapsed = monotonic() - started
    assert elapsed >= SLOW_BUT_VALID_SECONDS
    assert elapsed < 5.0
    assert report.venue_health["kalshi"] != "discovery_timeout"
    series = report.series_results.get("kalshi") or []
    assert series
    assert all(row.get("status") != "discovery_timeout" for row in series)
    assert all(row.get("status") == "ok" for row in series)
    assert all(item >= SLOW_BUT_VALID_SECONDS for item in kalshi.call_elapsed)
    assert report.raw_kalshi_events >= 1


@pytest.mark.asyncio
async def test_unbounded_universe_hung_kalshi_series_times_out_at_configured_venue_bound() -> None:
    kalshi = HungSeriesKalshi(
        events_by_series={
            "KXEPLGAME": [_k_event("KXEPL-NEW-CHE", "Newcastle", "Chelsea")],
            "KXLALIGA": [_k_event("KXLALIGA-BET-GET", "Real Betis", "Getafe", series="KXLALIGA")],
        }
    )
    collector = _unbounded_collector(kalshi)
    started = monotonic()
    report = await collector.collect_and_scan(
        scan_lane=ScanLane.UNIVERSE.value,
        unbounded_cycle=True,
        enabled_venues=[VenueName.KALSHI],
        max_event_pairs=8,
    )
    elapsed = monotonic() - started
    assert report.venue_health["kalshi"] == "discovery_timeout"
    series = report.series_results.get("kalshi") or []
    assert series
    assert all(row.get("status") == "discovery_timeout" for row in series)
    # Two series, each bounded by the configured venue timeout, not 50ms and not hung forever.
    assert elapsed >= CONFIGURED_VENUE_TIMEOUT
    assert elapsed < (CONFIGURED_VENUE_TIMEOUT * 2) + 1.5
    assert elapsed > PROVIDER_CANCEL_DRAIN_SECONDS * 10


@pytest.mark.asyncio
async def test_hot_cycle_still_times_out_inside_its_envelope() -> None:
    kalshi = HungSeriesKalshi(
        events_by_series={
            "KXEPLGAME": [_k_event("KXEPL-NEW-CHE", "Newcastle", "Chelsea")],
            "KXLALIGA": [_k_event("KXLALIGA-BET-GET", "Real Betis", "Getafe", series="KXLALIGA")],
        }
    )
    collector = ReadOnlyCrossVenueCollector(
        matchbook=ScriptedMatchbook([]),
        polymarket=ScriptedPolymarket([]),
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(SqliteMarketIntelligenceRepository())),
        venue_timeout_seconds=8.0,
        provider_call_timeout_seconds=8.0,
        cycle_timeout_seconds=0.4,
    )
    started = monotonic()
    report = await collector.collect_and_scan(
        scan_lane=ScanLane.HOT.value,
        unbounded_cycle=False,
        cycle_timeout_seconds=0.4,
        enabled_venues=[VenueName.KALSHI],
        max_event_pairs=8,
    )
    elapsed = monotonic() - started
    assert elapsed < 1.5
    assert report.venue_health["kalshi"] in {"discovery_timeout", "timeout", "degraded", "unavailable"}


def test_in_running_two_hours_after_kickoff_remains_hot() -> None:
    fixture = _fixture(
        "live-2h",
        kickoff=NOW - timedelta(hours=2),
        in_running=True,
        fixture_status="open",
        fixture_status_source=VenueName.MATCHBOOK,
    )
    assert classify_scan_lane(fixture, NOW) is ScanLane.HOT
    assert fixture.in_running is True
    assert fixture.fixture_status == "open"
    assert current_radar_eviction_reason(fixture, NOW) is None
    assert hot_reason_labels(
        fixture,
        NOW,
        membership=ScanLane.HOT,
        lifecycle=ScanLane.HOT,
        qualifying_promotion=False,
    ) == ["IN PLAY"]


def test_in_running_beyond_four_hour_ceiling_drops_without_fabricating_completed() -> None:
    fixture = _fixture(
        "stale-live",
        kickoff=NOW - timedelta(hours=4, minutes=1),
        in_running=True,
        fixture_status="open",
        fixture_status_source=VenueName.MATCHBOOK,
    )
    assert classify_scan_lane(fixture, NOW) is ScanLane.DROP
    assert fixture.in_running is True
    assert fixture.fixture_status == "open"
    assert current_radar_eviction_reason(fixture, NOW) == EVICTION_CLOCK_EXPIRED_CURRENT_RADAR
    assert hot_reason_labels(
        fixture,
        NOW,
        membership=ScanLane.DROP,
        lifecycle=ScanLane.DROP,
        qualifying_promotion=False,
    ) == []


def test_open_beyond_four_hour_ceiling_drops() -> None:
    fixture = _fixture(
        "stale-open",
        kickoff=NOW - timedelta(hours=4, minutes=1),
        in_running=None,
        fixture_status="open",
        fixture_status_source=VenueName.MATCHBOOK,
    )
    assert classify_scan_lane(fixture, NOW) is ScanLane.DROP
    assert fixture.fixture_status == "open"
    assert fixture.in_running is None
    assert current_radar_eviction_reason(fixture, NOW) == EVICTION_CLOCK_EXPIRED_CURRENT_RADAR


def test_postponed_and_rescheduled_are_not_falsely_expired_from_old_kickoff() -> None:
    past = NOW - timedelta(days=5)
    postponed = _fixture("pp", kickoff=past, fixture_status="postponed")
    delayed = _fixture("dl", kickoff=past, fixture_status="delayed")
    rescheduled = _fixture("rs", kickoff=past, fixture_status="rescheduled")
    for fixture in (postponed, delayed, rescheduled):
        assert classify_scan_lane(fixture, NOW) is ScanLane.UNIVERSE
        assert current_radar_eviction_reason(fixture, NOW) is None
        assert fixture.fixture_status in {"postponed", "delayed", "rescheduled"}


def test_explicit_terminal_still_drops_immediately_inside_ceiling() -> None:
    fixture = _fixture(
        "done",
        kickoff=NOW - timedelta(minutes=20),
        fixture_status="finished",
        fixture_status_source=VenueName.MATCHBOOK,
    )
    assert classify_scan_lane(fixture, NOW) is ScanLane.DROP
    assert fixture.fixture_status == "finished"
    assert current_radar_eviction_reason(fixture, NOW) != EVICTION_CLOCK_EXPIRED_CURRENT_RADAR


def test_betis_getafe_stale_in_play_leaves_hot_scheduling_after_ceiling() -> None:
    store = FixtureCurrentStateStore()
    fixture = _named_fixture(
        "real-betis-getafe",
        home="Real Betis",
        away="Getafe",
        kickoff=BETIS_KICKOFF,
    ).model_copy(
        update={
            "in_running": True,
            "fixture_status": "open",
            "fixture_status_source": VenueName.MATCHBOOK,
            "last_seen_at": BETIS_OBSERVED,
        }
    )
    store.upsert_from_report(
        _report([fixture], when=BETIS_KICKOFF + timedelta(minutes=10), scan_lane=ScanLane.HOT.value),
        scan_lane=ScanLane.HOT,
        now=BETIS_KICKOFF + timedelta(minutes=10),
    )
    assert store.hot_identity_scope(BETIS_KICKOFF + timedelta(hours=2)) == ["real-betis-getafe"]
    later_scope = store.hot_identity_scope(BETIS_OBSERVED)
    assert later_scope == []
    assert store.inventory(BETIS_OBSERVED) == []
    assert store.current_radar_rows(BETIS_OBSERVED) == []
    assert fixture.in_running is True
    assert fixture.fixture_status == "open"
    assert current_radar_eviction_reason(fixture, BETIS_OBSERVED) == EVICTION_CLOCK_EXPIRED_CURRENT_RADAR
    tombstone = store.tombstone_for("real-betis-getafe")
    if tombstone is not None:
        assert tombstone.reason == EVICTION_CLOCK_EXPIRED_CURRENT_RADAR
        assert tombstone.provider_status != "completed"
        assert tombstone.provider_status != "finished"


def test_clock_expired_betis_aliases_do_not_fork_hot_units() -> None:
    store = FixtureCurrentStateStore()
    first = _named_fixture(
        "betis-a",
        home="Real Betis Balompié",
        away="Getafe CF",
        kickoff=BETIS_KICKOFF,
    ).model_copy(update={"in_running": True, "fixture_status": "open"})
    second = _named_fixture(
        "betis-b",
        home="Real Betis",
        away="Getafe",
        kickoff=BETIS_KICKOFF,
    ).model_copy(update={"in_running": True, "fixture_status": "open"})
    assert hot_scheduling_key(first) == hot_scheduling_key(second)
    live_at = BETIS_KICKOFF + timedelta(hours=1)
    first_report = _report([first], when=live_at, scan_lane=ScanLane.HOT.value)
    first_report = first_report.model_copy(
        update={
            "fixture_identity_aliases": {"betis-a": "betis-a", "mb-1": "betis-a"},
            "fixture_source_events": {
                "betis-a": [{"venue": "matchbook", "source_event_id": "mb-1", "raw": {"id": "mb-1"}}]
            },
        }
    )
    store.upsert_from_report(first_report, scan_lane=ScanLane.HOT, now=live_at)
    later = _report([second], when=live_at, scan_lane=ScanLane.HOT.value)
    later = later.model_copy(
        update={
            "fixture_identity_aliases": {
                "betis-b": "betis-b",
                "mb-1": "betis-b",
                "betis-a": "betis-b",
            },
            "fixture_source_events": {
                "betis-b": [{"venue": "matchbook", "source_event_id": "mb-1", "raw": {"id": "mb-1"}}]
            },
        }
    )
    store.upsert_from_report(later, scan_lane=ScanLane.HOT, now=live_at)
    unique, _lifecycle, _promoted = store.hot_membership_breakdown(live_at)
    assert unique == 1
    assert store.hot_identity_scope(BETIS_OBSERVED) == []
    assert store.inventory(BETIS_OBSERVED) == []
    assert classify_scan_lane(first, BETIS_OBSERVED) is ScanLane.DROP
    assert classify_scan_lane(second, BETIS_OBSERVED) is ScanLane.DROP
    assert first.fixture_status == "open"
    assert second.fixture_status == "open"


def test_unknown_three_hour_window_is_unchanged_inside_ceiling() -> None:
    pending = _fixture("pending", kickoff=NOW - timedelta(hours=2, minutes=59), in_running=None)
    assert classify_scan_lane(pending, NOW) is ScanLane.HOT
    late_unknown = _fixture("late", kickoff=NOW - timedelta(hours=3, minutes=1), in_running=None)
    assert classify_scan_lane(late_unknown, NOW) is ScanLane.DROP
    assert late_unknown.in_running is None
    assert late_unknown.fixture_status is None
    still_live = _fixture(
        "et",
        kickoff=NOW - timedelta(hours=3, minutes=30),
        in_running=True,
        fixture_status="open",
    )
    assert classify_scan_lane(still_live, NOW) is ScanLane.HOT
    assert still_live.in_running is True
