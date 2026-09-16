"""Issue #245: HOT fixture roster read-model reasons.

Observability only. Does not change cadence, membership, matching, or
paper capture. Data class: deterministic fixture current-state payloads.
"""

from __future__ import annotations

from datetime import timedelta

from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.scan_lanes import (
    DEFAULT_HOT_HORIZON,
    HOT_REASON_ARB_PROMOTION,
    HOT_REASON_IN_PLAY,
    HOT_REASON_POST_KICKOFF_STATUS_PENDING,
    ScanLane,
    classify_scan_lane,
    hot_reason_labels,
    kickoff_horizon_reason_label,
)
from test_fixture_lifecycle_eviction import NOW, _fixture, _report
from test_issue200_universe_hot_promotion import (
    CANONICAL_ID,
    DISTANT_KICKOFF,
    _market_row,
    _qualifying_universe_report,
)


def test_hot_reason_labels_are_empty_for_universe_membership() -> None:
    fixture = _fixture("uni", kickoff=NOW + timedelta(days=3))
    assert classify_scan_lane(fixture, NOW) is ScanLane.UNIVERSE
    assert hot_reason_labels(
        fixture,
        NOW,
        membership=ScanLane.UNIVERSE,
        lifecycle=ScanLane.UNIVERSE,
        qualifying_promotion=False,
    ) == []


def test_in_play_reason_does_not_claim_arb_promotion() -> None:
    fixture = _fixture("live", kickoff=NOW - timedelta(minutes=20), in_running=True)
    assert classify_scan_lane(fixture, NOW) is ScanLane.HOT
    assert hot_reason_labels(
        fixture,
        NOW,
        membership=ScanLane.HOT,
        lifecycle=ScanLane.HOT,
        qualifying_promotion=True,
    ) == [HOT_REASON_IN_PLAY]


def test_pre_kickoff_horizon_reason_uses_configured_minutes() -> None:
    fixture = _fixture("soon", kickoff=NOW + timedelta(minutes=45))
    assert classify_scan_lane(fixture, NOW) is ScanLane.HOT
    assert hot_reason_labels(
        fixture,
        NOW,
        membership=ScanLane.HOT,
        lifecycle=ScanLane.HOT,
        qualifying_promotion=False,
    ) == [kickoff_horizon_reason_label(DEFAULT_HOT_HORIZON)]
    custom = timedelta(minutes=90)
    assert hot_reason_labels(
        fixture,
        NOW,
        membership=ScanLane.HOT,
        lifecycle=ScanLane.HOT,
        qualifying_promotion=False,
        hot_horizon=custom,
    ) == [kickoff_horizon_reason_label(custom)]


def test_post_kickoff_unknown_reason_without_fabricating_live_status() -> None:
    fixture = _fixture("pending", kickoff=NOW - timedelta(hours=2), in_running=None)
    assert classify_scan_lane(fixture, NOW) is ScanLane.HOT
    assert fixture.in_running is None
    assert hot_reason_labels(
        fixture,
        NOW,
        membership=ScanLane.HOT,
        lifecycle=ScanLane.HOT,
        qualifying_promotion=False,
    ) == [HOT_REASON_POST_KICKOFF_STATUS_PENDING]


def test_arb_promotion_only_when_lifecycle_would_otherwise_be_universe() -> None:
    distant = _fixture("far", kickoff=NOW + timedelta(days=3))
    assert classify_scan_lane(distant, NOW) is ScanLane.UNIVERSE
    assert hot_reason_labels(
        distant,
        NOW,
        membership=ScanLane.HOT,
        lifecycle=ScanLane.UNIVERSE,
        qualifying_promotion=True,
    ) == [HOT_REASON_ARB_PROMOTION]
    assert hot_reason_labels(
        distant,
        NOW,
        membership=ScanLane.HOT,
        lifecycle=ScanLane.UNIVERSE,
        qualifying_promotion=False,
    ) == []


def _inventory_by_id(store: FixtureCurrentStateStore):
    return {item.canonical_event_id: item for item in store.inventory(NOW)}


def test_inventory_stamps_hot_reasons_for_in_play_and_excludes_universe() -> None:
    store = FixtureCurrentStateStore()
    live = _fixture("live", kickoff=NOW - timedelta(minutes=10), in_running=True)
    distant = _fixture("uni", kickoff=NOW + timedelta(days=4))
    store.upsert_from_report(_report([live, distant]), scan_lane=ScanLane.UNIVERSE, now=NOW)

    rows = _inventory_by_id(store)
    assert rows["live"].scan_lane == ScanLane.HOT.value
    assert rows["live"].hot_reasons == [HOT_REASON_IN_PLAY]
    assert rows["uni"].scan_lane == ScanLane.UNIVERSE.value
    assert rows["uni"].hot_reasons == []


def test_inventory_stamps_kickoff_horizon_and_post_kickoff_reasons() -> None:
    store = FixtureCurrentStateStore()
    soon = _fixture("soon", kickoff=NOW + timedelta(minutes=30))
    pending = _fixture("pending", kickoff=NOW - timedelta(hours=1), in_running=None)
    store.upsert_from_report(_report([soon, pending]), scan_lane=ScanLane.HOT, now=NOW)

    rows = _inventory_by_id(store)
    assert rows["soon"].hot_reasons == [kickoff_horizon_reason_label()]
    assert rows["pending"].hot_reasons == [HOT_REASON_POST_KICKOFF_STATUS_PENDING]
    assert rows["pending"].in_running is None


def test_inventory_stamps_arb_promotion_from_current_state_not_history() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(_qualifying_universe_report(), scan_lane=ScanLane.UNIVERSE, now=NOW)
    promoted = _inventory_by_id(store)[CANONICAL_ID]
    assert classify_scan_lane(promoted, NOW) is ScanLane.UNIVERSE
    assert promoted.scan_lane == ScanLane.HOT.value
    assert promoted.hot_reasons == [HOT_REASON_ARB_PROMOTION]
    assert promoted.kickoff_utc == DISTANT_KICKOFF

    stale = FixtureCurrentStateStore()
    stale.upsert_from_report(
        _qualifying_universe_report(row=_market_row(quote_age_ms=5_000)),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    not_promoted = _inventory_by_id(stale)[CANONICAL_ID]
    assert not_promoted.scan_lane == ScanLane.UNIVERSE.value
    assert not_promoted.hot_reasons == []
