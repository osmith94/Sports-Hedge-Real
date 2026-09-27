"""Post-kickoff operator IN PLAY and zero-equivalent current-radar closure.

Data class: deterministic fixture current-state payloads. Not live venue data.
Provider ``in_running`` is provenance and must not be rewritten.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest
from test_issue200_universe_hot_promotion import _market_row

from sports_hedge.application.collector import CollectionReport, DiscoveredFixture
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.price_engine import CataloguePriceEngine, PriceEnginePriority
from sports_hedge.application.scan_lanes import (
    EVICTION_NO_CURRENT_EQUIVALENT_MARKETS_POST_KICKOFF,
    EVICTION_TERMINAL_FROM_MATCHBOOK,
    HOT_REASON_IN_PLAY,
    HOT_REASON_POST_KICKOFF_STATUS_PENDING,
    ScanLane,
    authoritative_post_kickoff_zero_equivalents,
    current_radar_eviction_reason,
    hot_reason_labels,
    kickoff_horizon_reason_label,
)
from sports_hedge.domain.models import VenueName

NOW = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)
EVENT = "evt/cze-cro"
KICKOFF = NOW - timedelta(minutes=25)


def _fixture(
    *,
    when: datetime = NOW,
    kickoff: datetime = KICKOFF,
    in_running: bool | None = False,
    fixture_status: str | None = "open",
    evaluation: str = "evaluated",
    reason: str | None = None,
    equivalents: int | None = 5,
    canonical_id: str = EVENT,
) -> DiscoveredFixture:
    return DiscoveredFixture(
        source=VenueName.MATCHBOOK,
        source_event_id=f"src-{canonical_id}",
        canonical_event_id=canonical_id,
        home_team="Czech Republic",
        away_team="Croatia",
        competition="World Cup",
        kickoff_utc=kickoff,
        last_seen_at=when,
        in_running=in_running,
        fixture_status=fixture_status,
        fixture_status_source=VenueName.MATCHBOOK if fixture_status else None,
        matchbook_matched=True,
        polymarket_matched=True,
        market_evaluation_state=evaluation,
        market_evaluation_reason=reason,
        matched_equivalent_count=equivalents,
        opportunity_state="matched" if evaluation == "evaluated" else "not_evaluated",
    )


def _report(
    fixture: DiscoveredFixture,
    *,
    when: datetime,
    markets: bool,
) -> CollectionReport:
    rows = [_market_row(arb=False, edge=None)] if markets else []
    return CollectionReport(
        started_at=when,
        completed_at=when,
        discovered_fixtures=[fixture],
        fixture_markets={fixture.canonical_event_id: rows},
        scan_lane=ScanLane.HOT.value,
        fixture_identity_aliases={
            fixture.canonical_event_id: fixture.canonical_event_id,
            fixture.source_event_id: fixture.canonical_event_id,
        },
    )


def _inventory(store: FixtureCurrentStateStore, when: datetime = NOW) -> dict[str, DiscoveredFixture]:
    return {item.canonical_event_id: item for item in store.inventory(when)}


def test_active_post_kickoff_is_operator_in_play_without_rewriting_in_running() -> None:
    store = FixtureCurrentStateStore()
    fixture = _fixture(in_running=False, equivalents=5)
    store.upsert_from_report(
        _report(fixture, when=NOW, markets=True),
        scan_lane=ScanLane.HOT,
        now=NOW,
    )

    row = _inventory(store)[EVENT]
    assert row.scan_lane == ScanLane.HOT.value
    assert row.hot_reasons == [HOT_REASON_IN_PLAY]
    assert HOT_REASON_POST_KICKOFF_STATUS_PENDING not in (row.hot_reasons or [])
    assert (row.matched_equivalent_count or 0) > 0
    assert row.market_evaluation_state == "evaluated"
    assert row.in_running is False
    assert row.fixture_status == "open"
    assert fixture.in_running is False
    assert fixture.fixture_status == "open"
    roster = store.console_projection(NOW).hot_roster
    assert [item.canonical_event_id for item in roster] == [EVENT]
    assert roster[0].hot_reasons == [HOT_REASON_IN_PLAY]
    assert store.hot_identity_scope(NOW) == [EVENT]
    assert current_radar_eviction_reason(fixture, NOW) is None


def test_zero_equivalent_success_leaves_hot_radar_without_fabricating_completion() -> None:
    store = FixtureCurrentStateStore()
    active = _fixture(equivalents=5, in_running=None, fixture_status="open")
    store.upsert_from_report(
        _report(active, when=NOW, markets=True),
        scan_lane=ScanLane.HOT,
        now=NOW,
    )
    assert EVENT in _inventory(store)

    later = NOW + timedelta(seconds=30)
    closed = _fixture(
        when=later,
        equivalents=0,
        in_running=None,
        fixture_status="open",
        evaluation="evaluated",
        reason=None,
    )
    assert authoritative_post_kickoff_zero_equivalents(closed, later) is True
    assert current_radar_eviction_reason(closed, later) == (
        EVICTION_NO_CURRENT_EQUIVALENT_MARKETS_POST_KICKOFF
    )
    store.upsert_from_report(
        _report(closed, when=later, markets=False),
        scan_lane=ScanLane.HOT,
        now=later,
    )

    assert store.inventory(later) == []
    assert store.hot_identity_scope(later) == []
    assert store.current_radar_rows(later) == []
    tombstone = store.tombstone_for(EVENT)
    assert tombstone is not None
    assert tombstone.reason == EVICTION_NO_CURRENT_EQUIVALENT_MARKETS_POST_KICKOFF
    assert tombstone.provider_status == "open"
    assert tombstone.provider_status not in {"completed", "finished", "closed", "graded"}
    assert closed.in_running is None
    assert closed.fixture_status == "open"
    assert active.in_running is None
    assert active.fixture_status == "open"


@pytest.mark.parametrize(
    ("evaluation", "reason", "equivalents"),
    [
        ("not_evaluated_scan_deadline", "scan_budget_exhausted", 0),
        ("market_fetch_unavailable", "list_markets_unavailable", 0),
        ("evaluated", "not_evaluated_scan_deadline", 0),
        ("evaluated", "provider_timeout", 0),
        ("not_evaluated_scan_deadline", None, None),
    ],
)
def test_incomplete_refresh_keeps_last_equivalents_and_does_not_end_fixture(
    evaluation: str,
    reason: str | None,
    equivalents: int | None,
) -> None:
    store = FixtureCurrentStateStore()
    active = _fixture(equivalents=5, in_running=False, fixture_status="open")
    store.upsert_from_report(
        _report(active, when=NOW, markets=True),
        scan_lane=ScanLane.HOT,
        now=NOW,
    )
    later = NOW + timedelta(seconds=20)
    failed = _fixture(
        when=later,
        evaluation=evaluation,
        reason=reason,
        equivalents=equivalents,
        in_running=False,
        fixture_status="open",
    )
    assert authoritative_post_kickoff_zero_equivalents(failed, later) is False
    assert current_radar_eviction_reason(failed, later) is None
    store.upsert_from_report(
        _report(failed, when=later, markets=False),
        scan_lane=ScanLane.HOT,
        now=later,
    )

    row = _inventory(store, later)[EVENT]
    assert row.scan_lane == ScanLane.HOT.value
    assert (row.matched_equivalent_count or 0) > 0
    assert row.in_running is False
    assert row.fixture_status == "open"
    assert store.tombstone_for(EVENT) is None
    assert failed.in_running is False
    assert failed.fixture_status == "open"
    assert HOT_REASON_IN_PLAY in (row.hot_reasons or [])


def test_provider_in_running_stays_in_play_and_is_not_rewritten() -> None:
    store = FixtureCurrentStateStore()
    live = _fixture(in_running=True, equivalents=0, fixture_status="in-play")
    assert hot_reason_labels(
        live,
        NOW,
        membership=ScanLane.HOT,
        lifecycle=ScanLane.HOT,
        qualifying_promotion=False,
    ) == [HOT_REASON_IN_PLAY]
    store.upsert_from_report(
        _report(live, when=NOW, markets=True),
        scan_lane=ScanLane.HOT,
        now=NOW,
    )
    row = _inventory(store)[EVENT]
    assert row.scan_lane == ScanLane.HOT.value
    assert row.hot_reasons == [HOT_REASON_IN_PLAY]
    assert row.in_running is True
    assert live.in_running is True
    assert store.tombstone_for(EVENT) is None
    assert current_radar_eviction_reason(live, NOW) is None


def test_pre_kickoff_horizon_is_unchanged_when_equivalents_are_zero() -> None:
    store = FixtureCurrentStateStore()
    soon = _fixture(
        kickoff=NOW + timedelta(minutes=40),
        equivalents=0,
        in_running=False,
        fixture_status="open",
    )
    assert current_radar_eviction_reason(soon, NOW) is None
    assert authoritative_post_kickoff_zero_equivalents(soon, NOW) is False
    store.upsert_from_report(
        _report(soon, when=NOW, markets=False),
        scan_lane=ScanLane.HOT,
        now=NOW,
    )
    row = _inventory(store)[EVENT]
    assert row.scan_lane == ScanLane.HOT.value
    assert row.hot_reasons == [kickoff_horizon_reason_label()]
    assert row.in_running is False
    assert HOT_REASON_IN_PLAY not in (row.hot_reasons or [])
    assert store.tombstone_for(EVENT) is None


def test_explicit_terminal_still_evicts_immediately() -> None:
    store = FixtureCurrentStateStore()
    done = _fixture(
        equivalents=5,
        in_running=False,
        fixture_status="closed",
    )
    store.upsert_from_report(
        _report(done, when=NOW, markets=True),
        scan_lane=ScanLane.HOT,
        now=NOW,
    )
    assert store.inventory(NOW) == []
    tombstone = store.tombstone_for(EVENT)
    assert tombstone is not None
    assert tombstone.reason == EVICTION_TERMINAL_FROM_MATCHBOOK
    assert done.in_running is False
    assert done.fixture_status == "closed"


def test_later_equivalents_restore_without_writing_in_running() -> None:
    store = FixtureCurrentStateStore()
    later = NOW + timedelta(seconds=30)
    closed = _fixture(when=later, equivalents=0, in_running=None, fixture_status="open")
    store.upsert_from_report(
        _report(closed, when=later, markets=False),
        scan_lane=ScanLane.HOT,
        now=later,
    )
    assert store.tombstone_for(EVENT) is not None
    restored_at = later + timedelta(seconds=30)
    restored = _fixture(
        when=restored_at,
        equivalents=2,
        in_running=None,
        fixture_status="open",
    )
    store.upsert_from_report(
        _report(restored, when=restored_at, markets=True),
        scan_lane=ScanLane.HOT,
        now=restored_at,
    )
    row = _inventory(store, restored_at)[EVENT]
    assert row.hot_reasons == [HOT_REASON_IN_PLAY]
    assert row.in_running is None
    assert restored.in_running is None
    assert store.tombstone_for(EVENT) is None


def test_market_closure_revokes_hot_priority_without_changing_cadence() -> None:
    store = FixtureCurrentStateStore()
    engine = CataloguePriceEngine(
        fixture_state=store,
        clock=lambda: NOW,
        hot_interval_seconds=30,
        background_interval_seconds=600,
    )
    identity = SimpleNamespace(
        canonical_event_id=EVENT,
        kickoff_utc=KICKOFF,
        catalogue_row_id="row-cze-cro",
    )
    assert engine.classify_priority(identity) is PriceEnginePriority.HOT
    closed = _fixture(equivalents=0, in_running=None, fixture_status="open")
    store.upsert_from_report(
        _report(closed, when=NOW, markets=False),
        scan_lane=ScanLane.HOT,
        now=NOW,
    )
    assert engine.classify_priority(identity) is PriceEnginePriority.BACKGROUND
    assert engine._hot_interval == 30
    assert engine._background_interval == 600
