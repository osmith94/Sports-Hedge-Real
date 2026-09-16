"""Issue #226: terminal tombstone continuity + cross-venue canonical identity repair.

Reproduces the Wave M2 Issue #220 three-step counterexamples, then locks the
repair. Data class: deterministic fixture/demo current-state payloads. Not live,
historical, or modelled venue quotes.
"""

from __future__ import annotations

import random
from datetime import datetime, timedelta
from typing import Any, Callable

import pytest
from test_issue200_universe_hot_promotion import (
    DISTANT_KICKOFF,
    NOW,
    _decision,
    _fixture,
    _market_row,
    _qualifying_universe_report,
)
from test_issue211_identity_continuity import (
    CANONICAL_ID,
    MB_ANCHORED,
    PM_ANCHORED,
    PM_SOURCE,
    _mb_pm_qualifying_report,
    _pm_only_report,
)

from sports_hedge.application.collector import CollectionReport, MarketEvaluationState
from sports_hedge.application.current_market_inventory import (
    current_slots_prove_qualifying_opportunity,
)
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.scan_lanes import (
    DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    DEFAULT_HOT_TTL_SECONDS,
    DEFAULT_UNIVERSE_TTL_SECONDS,
    ScanLane,
)
from sports_hedge.domain.models import VenueName


ApplyFn = Callable[[CollectionReport, ScanLane, datetime], FixtureCurrentStateStore]

BHA_MB = "evt:mb-bha-eve"
BHA_PM = "evt:pm-bha-eve"
BHA_MB_SRC = "mb-5005"
BHA_PM_SRC = "pm-bha-eve"

NCL_MB = "evt:mb-ncl-che"
NCL_KAL = "evt:kal-ncl-che"
NCL_MB_SRC = "mb-1001"
NCL_PM_SRC = "pm-ncl-che"
NCL_KAL_SRC = "kal-ncl-che"

ARS_MB = "evt:mb-ars-che-1500"
ARS_SPLIT = "evt:mb-ars-che-1505"
ARS_MB_SRC = "mb-ars-1500"
ARS_SPLIT_SRC = "mb-ars-1505"
ARS_PM_SRC = "pm-ars-1500"
ARS_SPLIT_PM = "pm-ars-1505"


def _identity_report(
    canonical_id: str,
    *,
    source: VenueName,
    source_event_id: str,
    aliases: dict[str, str],
    source_events: list[dict[str, Any]],
    when: datetime = NOW,
    scan_lane: str = ScanLane.UNIVERSE.value,
    qualifying: bool = False,
    fixture_status: str | None = None,
    fixture_status_source: VenueName | None = None,
    home: str = "Brighton",
    away: str = "Everton",
    kickoff: datetime = DISTANT_KICKOFF,
    evaluation: str = "evaluated",
    markets: dict[str, list] | None = None,
) -> CollectionReport:
    opportunity = "qualifying" if qualifying else "unmatched"
    fixture = _fixture(
        canonical_id,
        kickoff=kickoff,
        evaluation=evaluation,
        opportunity=opportunity,
        arb=qualifying,
        qualifying=1 if qualifying else 0,
        when=when,
        fixture_status=fixture_status,
    ).model_copy(
        update={
            "source": source,
            "source_event_id": source_event_id,
            "home_team": home,
            "away_team": away,
            "fixture_status_source": fixture_status_source,
            "matchbook_matched": any(
                item.get("venue") == "matchbook" for item in source_events
            )
            or source is VenueName.MATCHBOOK,
            "polymarket_matched": any(
                item.get("venue") == "polymarket" for item in source_events
            )
            or source is VenueName.POLYMARKET,
            "kalshi_matched": any(item.get("venue") == "kalshi" for item in source_events)
            or source is VenueName.KALSHI,
        }
    )
    evaluated = evaluation == "evaluated"
    if markets is None:
        markets = {canonical_id: [_market_row()]} if qualifying and evaluated else {}
    return CollectionReport(
        started_at=when,
        completed_at=when,
        paper_decisions=[_decision(canonical_id, f"mkt-{canonical_id}", when=when)]
        if evaluated
        else [],
        discovered_fixtures=[fixture],
        fixture_markets=markets,
        scan_lane=scan_lane,
        venue_health={"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"},
        operator_summary="issue-226",
        fixture_identity_aliases=aliases,
        fixture_source_events={canonical_id: source_events},
    )


def _mb_pm_cluster(
    canonical_id: str,
    mb_src: str,
    pm_src: str,
    *,
    when: datetime = NOW,
    home: str = "Brighton",
    away: str = "Everton",
    kal_src: str | None = None,
    scan_lane: str = ScanLane.UNIVERSE.value,
    qualifying: bool = True,
) -> CollectionReport:
    aliases = {canonical_id: canonical_id, mb_src: canonical_id, pm_src: canonical_id}
    events = [
        {"venue": "matchbook", "source_event_id": mb_src, "raw": {"id": mb_src}},
        {"venue": "polymarket", "source_event_id": pm_src, "raw": {"id": pm_src}},
    ]
    if kal_src:
        aliases[kal_src] = canonical_id
        events.append({"venue": "kalshi", "source_event_id": kal_src, "raw": {"id": kal_src}})
    return _identity_report(
        canonical_id,
        source=VenueName.MATCHBOOK,
        source_event_id=mb_src,
        aliases=aliases,
        source_events=events,
        when=when,
        scan_lane=scan_lane,
        qualifying=qualifying,
        home=home,
        away=away,
    )


def _single_venue(
    canonical_id: str,
    *,
    source: VenueName,
    source_event_id: str,
    when: datetime,
    qualifying: bool = True,
    home: str = "Brighton",
    away: str = "Everton",
    extra_aliases: dict[str, str] | None = None,
    extra_events: list[dict[str, Any]] | None = None,
    scan_lane: str = ScanLane.UNIVERSE.value,
    fixture_status: str | None = None,
    fixture_status_source: VenueName | None = None,
    kickoff: datetime = DISTANT_KICKOFF,
    evaluation: str = "evaluated",
    markets: dict[str, list] | None = None,
) -> CollectionReport:
    aliases = {canonical_id: canonical_id, source_event_id: canonical_id}
    if extra_aliases:
        aliases.update(extra_aliases)
    events = [
        {
            "venue": source.value,
            "source_event_id": source_event_id,
            "raw": {"id": source_event_id, "status": fixture_status},
        }
    ]
    if extra_events:
        events.extend(extra_events)
    return _identity_report(
        canonical_id,
        source=source,
        source_event_id=source_event_id,
        aliases=aliases,
        source_events=events,
        when=when,
        scan_lane=scan_lane,
        qualifying=qualifying,
        fixture_status=fixture_status,
        fixture_status_source=fixture_status_source,
        home=home,
        away=away,
        kickoff=kickoff,
        evaluation=evaluation,
        markets=markets,
    )


def _store_apply() -> tuple[FixtureCurrentStateStore, ApplyFn]:
    store = FixtureCurrentStateStore()

    def apply(report: CollectionReport, lane: ScanLane, when: datetime) -> FixtureCurrentStateStore:
        store.upsert_from_report(report, scan_lane=lane, now=when)
        return store

    return store, apply


def _coordinator_apply() -> tuple[FixtureCurrentStateStore, ApplyFn]:
    coordinator = LiveRefreshCoordinator()
    coordinator.reset()
    store = coordinator.fixture_current_state()

    def apply(report: CollectionReport, lane: ScanLane, when: datetime) -> FixtureCurrentStateStore:
        del when
        coordinator.record_report(report, scan_lane=lane)
        return store

    return store, apply


SEAMS = (
    pytest.param(_store_apply, id="store"),
    pytest.param(_coordinator_apply, id="coordinator"),
)


def _live_ids(store: FixtureCurrentStateStore, now: datetime) -> list[str]:
    return sorted(item.canonical_event_id for item in store.inventory(now))


def _assert_tombstoned(store: FixtureCurrentStateStore, *identities: str) -> None:
    for identity in identities:
        assert store.tombstone_for(identity) is not None, identity
        assert store.resolve_canonical_id(identity) is None, identity


# ---------------------------------------------------------------------------
# Counterexample A — Matchbook-only terminal then PM resurrection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seam", SEAMS)
@pytest.mark.parametrize("terminal_status", ["closed", "graded", "settled"])
def test_matchbook_only_terminal_retains_pm_alias_and_cannot_resurrect(
    seam: Callable[[], tuple[FixtureCurrentStateStore, ApplyFn]],
    terminal_status: str,
) -> None:
    store, apply = seam()
    apply(
        _mb_pm_cluster(BHA_MB, BHA_MB_SRC, BHA_PM_SRC, when=NOW, home="Brighton", away="Everton"),
        ScanLane.UNIVERSE,
        NOW,
    )
    assert _live_ids(store, NOW) == [BHA_MB]
    assert store.hot_identity_scope(NOW) == [BHA_MB]
    assert store.resolve_canonical_id(BHA_PM_SRC) == BHA_MB

    closed_at = NOW + timedelta(seconds=30)
    apply(
        _single_venue(
            BHA_MB,
            source=VenueName.MATCHBOOK,
            source_event_id=BHA_MB_SRC,
            when=closed_at,
            scan_lane=ScanLane.HOT.value,
            fixture_status=terminal_status,
            fixture_status_source=VenueName.MATCHBOOK,
            kickoff=NOW - timedelta(minutes=10),
            qualifying=True,
        ),
        ScanLane.HOT,
        closed_at,
    )
    tombstone = store.tombstone_for(BHA_MB)
    assert tombstone is not None
    assert {BHA_MB, BHA_MB_SRC, BHA_PM_SRC} <= set(tombstone.aliases)
    _assert_tombstoned(store, BHA_MB, BHA_MB_SRC, BHA_PM_SRC)
    assert store.hot_identity_scope(closed_at) == []
    assert store._rows == {}

    later = closed_at + timedelta(seconds=30)
    apply(
        _single_venue(
            BHA_PM,
            source=VenueName.POLYMARKET,
            source_event_id=BHA_PM_SRC,
            when=later,
            qualifying=True,
            home="Brighton",
            away="Everton",
        ),
        ScanLane.UNIVERSE,
        later,
    )
    _assert_tombstoned(store, BHA_MB, BHA_PM, BHA_MB_SRC, BHA_PM_SRC)
    assert _live_ids(store, later) == []
    assert store.hot_identity_scope(later) == []
    assert store._rows == {}
    hot, universe = store.membership_counts(later)
    assert hot == 0
    assert universe == 0


@pytest.mark.parametrize("seam", SEAMS)
def test_terminal_omitting_kalshi_alias_still_blocks_later_kalshi_only(
    seam: Callable[[], tuple[FixtureCurrentStateStore, ApplyFn]],
) -> None:
    store, apply = seam()
    apply(
        _mb_pm_cluster(
            BHA_MB,
            BHA_MB_SRC,
            BHA_PM_SRC,
            when=NOW,
            kal_src="kal-bha-eve",
        ),
        ScanLane.UNIVERSE,
        NOW,
    )
    closed_at = NOW + timedelta(seconds=30)
    apply(
        _single_venue(
            BHA_MB,
            source=VenueName.MATCHBOOK,
            source_event_id=BHA_MB_SRC,
            when=closed_at,
            scan_lane=ScanLane.HOT.value,
            fixture_status="closed",
            fixture_status_source=VenueName.MATCHBOOK,
            kickoff=NOW - timedelta(minutes=5),
        ),
        ScanLane.HOT,
        closed_at,
    )
    later = closed_at + timedelta(seconds=20)
    apply(
        _single_venue(
            "evt:kal-bha-eve",
            source=VenueName.KALSHI,
            source_event_id="kal-bha-eve",
            when=later,
            qualifying=True,
        ),
        ScanLane.UNIVERSE,
        later,
    )
    _assert_tombstoned(store, BHA_MB, BHA_PM_SRC, "kal-bha-eve", "evt:kal-bha-eve")
    assert store.hot_identity_scope(later) == []
    assert store._rows == {}


# ---------------------------------------------------------------------------
# Counterexample B — Kalshi-only fork then overlapping alias convergence
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("seam", SEAMS)
def test_kalshi_only_then_overlapping_aliases_converge_to_one_hot_row(
    seam: Callable[[], tuple[FixtureCurrentStateStore, ApplyFn]],
) -> None:
    store, apply = seam()
    apply(
        _mb_pm_cluster(
            NCL_MB,
            NCL_MB_SRC,
            NCL_PM_SRC,
            when=NOW,
            home="Newcastle United",
            away="Chelsea",
        ),
        ScanLane.UNIVERSE,
        NOW,
    )
    assert store.hot_identity_scope(NOW) == [NCL_MB]

    kal_at = NOW + timedelta(seconds=30)
    apply(
        _single_venue(
            NCL_KAL,
            source=VenueName.KALSHI,
            source_event_id=NCL_KAL_SRC,
            when=kal_at,
            qualifying=True,
            home="Newcastle United",
            away="Chelsea",
        ),
        ScanLane.UNIVERSE,
        kal_at,
    )
    # Fail-closed until trusted overlap exists: disjoint Kalshi id may appear.
    overlap_at = kal_at + timedelta(seconds=30)
    apply(
        _single_venue(
            NCL_KAL,
            source=VenueName.KALSHI,
            source_event_id=NCL_KAL_SRC,
            when=overlap_at,
            qualifying=True,
            home="Newcastle United",
            away="Chelsea",
            extra_aliases={
                NCL_MB_SRC: NCL_KAL,
                NCL_PM_SRC: NCL_KAL,
                NCL_MB: NCL_KAL,
            },
            extra_events=[
                {"venue": "matchbook", "source_event_id": NCL_MB_SRC, "raw": {"id": NCL_MB_SRC}},
                {"venue": "polymarket", "source_event_id": NCL_PM_SRC, "raw": {"id": NCL_PM_SRC}},
            ],
        ),
        ScanLane.UNIVERSE,
        overlap_at,
    )
    surviving = {
        store.resolve_canonical_id(item)
        for item in (NCL_MB, NCL_KAL, NCL_MB_SRC, NCL_PM_SRC, NCL_KAL_SRC)
    }
    surviving.discard(None)
    assert len(surviving) == 1
    canonical = next(iter(surviving))
    assert canonical is not None
    assert _live_ids(store, overlap_at) == [canonical]
    assert store.hot_identity_scope(overlap_at) == [canonical]
    hot, universe = store.membership_counts(overlap_at)
    assert hot == 1
    assert universe == 0
    assert len(store._rows) == 1


@pytest.mark.parametrize("seam", SEAMS)
def test_mb_pm_kalshi_source_subset_transitions_stay_one_identity(
    seam: Callable[[], tuple[FixtureCurrentStateStore, ApplyFn]],
) -> None:
    store, apply = seam()
    apply(
        _mb_pm_cluster(
            NCL_MB,
            NCL_MB_SRC,
            NCL_PM_SRC,
            when=NOW,
            kal_src=NCL_KAL_SRC,
            home="Newcastle United",
            away="Chelsea",
        ),
        ScanLane.UNIVERSE,
        NOW,
    )
    first = store.resolve_canonical_id(NCL_MB)
    later = NOW + timedelta(seconds=20)
    apply(
        _single_venue(
            NCL_KAL,
            source=VenueName.KALSHI,
            source_event_id=NCL_KAL_SRC,
            when=later,
            qualifying=True,
            home="Newcastle United",
            away="Chelsea",
        ),
        ScanLane.UNIVERSE,
        later,
    )
    later_pm = later + timedelta(seconds=20)
    apply(
        _single_venue(
            "evt:pm-ncl-che",
            source=VenueName.POLYMARKET,
            source_event_id=NCL_PM_SRC,
            when=later_pm,
            qualifying=True,
            home="Newcastle United",
            away="Chelsea",
        ),
        ScanLane.UNIVERSE,
        later_pm,
    )
    later_mb = later_pm + timedelta(seconds=20)
    apply(
        _mb_pm_cluster(
            NCL_MB,
            NCL_MB_SRC,
            NCL_PM_SRC,
            when=later_mb,
            kal_src=NCL_KAL_SRC,
            home="Newcastle United",
            away="Chelsea",
        ),
        ScanLane.UNIVERSE,
        later_mb,
    )
    for identity in (NCL_MB, NCL_KAL, NCL_MB_SRC, NCL_PM_SRC, NCL_KAL_SRC, "evt:pm-ncl-che"):
        assert store.resolve_canonical_id(identity) == first
    assert len(store._rows) == 1
    assert store.hot_identity_scope(later_mb) == [first]


def test_distinct_same_city_fixtures_do_not_merge_or_steal_aliases() -> None:
    store = FixtureCurrentStateStore()
    kickoff = NOW + timedelta(hours=4)
    split = kickoff + timedelta(minutes=5)
    store.upsert_from_report(
        _mb_pm_cluster(
            ARS_MB,
            ARS_MB_SRC,
            ARS_PM_SRC,
            when=NOW,
            home="Arsenal",
            away="Chelsea",
        ).model_copy(
            update={
                "discovered_fixtures": [
                    _fixture(
                        ARS_MB,
                        kickoff=kickoff,
                        evaluation="evaluated",
                        opportunity="qualifying",
                        arb=True,
                        qualifying=1,
                    ).model_copy(
                        update={
                            "source": VenueName.MATCHBOOK,
                            "source_event_id": ARS_MB_SRC,
                            "home_team": "Arsenal",
                            "away_team": "Chelsea",
                        }
                    )
                ]
            }
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    store.upsert_from_report(
        _identity_report(
            ARS_SPLIT,
            source=VenueName.MATCHBOOK,
            source_event_id=ARS_SPLIT_SRC,
            aliases={ARS_SPLIT: ARS_SPLIT, ARS_SPLIT_SRC: ARS_SPLIT, ARS_SPLIT_PM: ARS_SPLIT},
            source_events=[
                {"venue": "matchbook", "source_event_id": ARS_SPLIT_SRC, "raw": {"id": ARS_SPLIT_SRC}},
                {"venue": "polymarket", "source_event_id": ARS_SPLIT_PM, "raw": {"id": ARS_SPLIT_PM}},
            ],
            when=NOW + timedelta(seconds=5),
            qualifying=True,
            home="Arsenal",
            away="Chelsea",
            kickoff=split,
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW + timedelta(seconds=5),
    )
    assert store.resolve_canonical_id(ARS_MB) == ARS_MB
    assert store.resolve_canonical_id(ARS_SPLIT) == ARS_SPLIT
    assert len(store._rows) == 2

    later = NOW + timedelta(seconds=40)
    store.upsert_from_report(
        _single_venue(
            "evt:kal-ars-che-1500",
            source=VenueName.KALSHI,
            source_event_id="kal-ars-1500",
            when=later,
            qualifying=True,
            home="Arsenal",
            away="Chelsea",
            extra_aliases={ARS_MB_SRC: "evt:kal-ars-che-1500", ARS_PM_SRC: "evt:kal-ars-che-1500"},
            extra_events=[
                {"venue": "matchbook", "source_event_id": ARS_MB_SRC, "raw": {"id": ARS_MB_SRC}},
                {"venue": "polymarket", "source_event_id": ARS_PM_SRC, "raw": {"id": ARS_PM_SRC}},
            ],
            kickoff=kickoff,
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=later,
    )
    surviving_1500 = {
        store.resolve_canonical_id(item)
        for item in (ARS_MB, ARS_MB_SRC, ARS_PM_SRC, "evt:kal-ars-che-1500", "kal-ars-1500")
    }
    surviving_1500.discard(None)
    assert len(surviving_1500) == 1
    assert store.resolve_canonical_id(ARS_SPLIT) == ARS_SPLIT
    assert store.resolve_canonical_id(ARS_SPLIT_SRC) == ARS_SPLIT
    assert store.resolve_canonical_id(ARS_SPLIT) not in surviving_1500
    assert len(store._rows) == 2


def test_ambiguous_overlap_across_two_live_fixtures_fail_closed() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(
        _mb_pm_cluster(NCL_MB, NCL_MB_SRC, NCL_PM_SRC, home="Newcastle United", away="Chelsea"),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    store.upsert_from_report(
        _mb_pm_cluster(
            BHA_MB,
            BHA_MB_SRC,
            BHA_PM_SRC,
            when=NOW + timedelta(seconds=5),
            home="Brighton",
            away="Everton",
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW + timedelta(seconds=5),
    )
    later = NOW + timedelta(seconds=40)
    store.upsert_from_report(
        _single_venue(
            NCL_KAL,
            source=VenueName.KALSHI,
            source_event_id=NCL_KAL_SRC,
            when=later,
            qualifying=True,
            home="Newcastle United",
            away="Chelsea",
            extra_aliases={
                NCL_MB_SRC: NCL_KAL,
                BHA_MB_SRC: NCL_KAL,
            },
            extra_events=[
                {"venue": "matchbook", "source_event_id": NCL_MB_SRC, "raw": {"id": NCL_MB_SRC}},
                {"venue": "matchbook", "source_event_id": BHA_MB_SRC, "raw": {"id": BHA_MB_SRC}},
            ],
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=later,
    )
    assert store.resolve_canonical_id(NCL_MB) == NCL_MB
    assert store.resolve_canonical_id(BHA_MB) == BHA_MB
    assert store.resolve_canonical_id(NCL_MB_SRC) == NCL_MB
    assert store.resolve_canonical_id(BHA_MB_SRC) == BHA_MB
    assert len(store._rows) >= 2
    assert store.resolve_canonical_id(NCL_MB) != store.resolve_canonical_id(BHA_MB)


def test_disjoint_mb_and_pm_rows_converge_when_cluster_overlap_arrives() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(
        _single_venue(
            NCL_MB,
            source=VenueName.MATCHBOOK,
            source_event_id=NCL_MB_SRC,
            when=NOW,
            qualifying=True,
            home="Newcastle United",
            away="Chelsea",
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    store.upsert_from_report(
        _single_venue(
            "evt:pm-ncl-che",
            source=VenueName.POLYMARKET,
            source_event_id=NCL_PM_SRC,
            when=NOW + timedelta(seconds=10),
            qualifying=True,
            home="Newcastle United",
            away="Chelsea",
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW + timedelta(seconds=10),
    )
    assert len(store._rows) == 2
    later = NOW + timedelta(seconds=40)
    store.upsert_from_report(
        _mb_pm_cluster(
            NCL_MB,
            NCL_MB_SRC,
            NCL_PM_SRC,
            when=later,
            kal_src=NCL_KAL_SRC,
            home="Newcastle United",
            away="Chelsea",
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=later,
    )
    surviving = {
        store.resolve_canonical_id(item)
        for item in (NCL_MB, "evt:pm-ncl-che", NCL_MB_SRC, NCL_PM_SRC, NCL_KAL_SRC)
    }
    surviving.discard(None)
    assert len(surviving) == 1
    assert len(store._rows) == 1
    assert store.hot_identity_scope(later) == [next(iter(surviving))]


def test_kalshi_fork_then_matchbook_terminal_overlap_stays_tombstoned() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(
        _mb_pm_cluster(NCL_MB, NCL_MB_SRC, NCL_PM_SRC, home="Newcastle United", away="Chelsea"),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    kal_at = NOW + timedelta(seconds=20)
    store.upsert_from_report(
        _single_venue(
            NCL_KAL,
            source=VenueName.KALSHI,
            source_event_id=NCL_KAL_SRC,
            when=kal_at,
            qualifying=True,
            home="Newcastle United",
            away="Chelsea",
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=kal_at,
    )
    closed_at = kal_at + timedelta(seconds=20)
    store.upsert_from_report(
        _single_venue(
            NCL_MB,
            source=VenueName.MATCHBOOK,
            source_event_id=NCL_MB_SRC,
            when=closed_at,
            scan_lane=ScanLane.HOT.value,
            fixture_status="closed",
            fixture_status_source=VenueName.MATCHBOOK,
            kickoff=NOW - timedelta(minutes=10),
            qualifying=True,
            home="Newcastle United",
            away="Chelsea",
        ),
        scan_lane=ScanLane.HOT,
        now=closed_at,
    )
    overlap_at = closed_at + timedelta(seconds=20)
    store.upsert_from_report(
        _single_venue(
            NCL_KAL,
            source=VenueName.KALSHI,
            source_event_id=NCL_KAL_SRC,
            when=overlap_at,
            qualifying=True,
            home="Newcastle United",
            away="Chelsea",
            extra_aliases={NCL_MB_SRC: NCL_KAL, NCL_PM_SRC: NCL_KAL},
            extra_events=[
                {"venue": "matchbook", "source_event_id": NCL_MB_SRC, "raw": {"id": NCL_MB_SRC}},
                {"venue": "polymarket", "source_event_id": NCL_PM_SRC, "raw": {"id": NCL_PM_SRC}},
            ],
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=overlap_at,
    )
    _assert_tombstoned(store, NCL_MB, NCL_KAL, NCL_MB_SRC, NCL_PM_SRC, NCL_KAL_SRC)
    assert store.hot_identity_scope(overlap_at) == []
    assert store._rows == {}


# ---------------------------------------------------------------------------
# K2 invariants still hold
# ---------------------------------------------------------------------------


def test_issue211_connected_pm_reanchor_still_merges() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(_mb_pm_qualifying_report(), scan_lane=ScanLane.UNIVERSE, now=NOW)
    later = NOW + timedelta(seconds=30)
    store.upsert_from_report(_pm_only_report(when=later), scan_lane=ScanLane.UNIVERSE, now=later)
    assert store.resolve_canonical_id(PM_ANCHORED) == MB_ANCHORED
    assert store.resolve_canonical_id(PM_SOURCE) == MB_ANCHORED
    assert len(store._rows) == 1


def test_evaluated_empty_still_demotes_and_unavailable_does_not() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(_qualifying_universe_report(), scan_lane=ScanLane.UNIVERSE, now=NOW)
    empty_at = NOW + timedelta(seconds=30)
    store.upsert_from_report(
        _identity_report(
            CANONICAL_ID,
            source=VenueName.MATCHBOOK,
            source_event_id=CANONICAL_ID,
            aliases={CANONICAL_ID: CANONICAL_ID},
            source_events=[
                {"venue": "matchbook", "source_event_id": CANONICAL_ID, "raw": {"id": CANONICAL_ID}}
            ],
            when=empty_at,
            scan_lane=ScanLane.HOT.value,
            qualifying=False,
            markets={},
        ),
        scan_lane=ScanLane.HOT,
        now=empty_at,
    )
    assert CANONICAL_ID not in store.hot_identity_scope(empty_at)

    store.clear()
    store.upsert_from_report(_qualifying_universe_report(), scan_lane=ScanLane.UNIVERSE, now=NOW)
    later = NOW + timedelta(seconds=30)
    store.upsert_from_report(
        _identity_report(
            CANONICAL_ID,
            source=VenueName.MATCHBOOK,
            source_event_id=CANONICAL_ID,
            aliases={CANONICAL_ID: CANONICAL_ID},
            source_events=[
                {"venue": "matchbook", "source_event_id": CANONICAL_ID, "raw": {"id": CANONICAL_ID}}
            ],
            when=later,
            scan_lane=ScanLane.HOT.value,
            evaluation=MarketEvaluationState.MARKET_FETCH_UNAVAILABLE.value,
            qualifying=False,
            markets={},
        ),
        scan_lane=ScanLane.HOT,
        now=later,
    )
    assert CANONICAL_ID in store.hot_identity_scope(later)
    record = store._rows[CANONICAL_ID]
    assert current_slots_prove_qualifying_opportunity(
        record.live_market_slots(),
        now=later,
        hot_ttl_seconds=DEFAULT_HOT_TTL_SECONDS,
        universe_ttl_seconds=DEFAULT_UNIVERSE_TTL_SECONDS,
        max_quote_age_ms=DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    )


# ---------------------------------------------------------------------------
# Seeded long sequences
# ---------------------------------------------------------------------------


class _FixtureWorld:
    def __init__(self, index: int) -> None:
        self.index = index
        if index == 0:
            self.home, self.away = "Brighton", "Everton"
            self.kickoff = DISTANT_KICKOFF
        elif index == 1:
            self.home, self.away = "Newcastle United", "Chelsea"
            self.kickoff = DISTANT_KICKOFF + timedelta(minutes=5)
        else:
            self.home, self.away = "Arsenal", "Chelsea"
            self.kickoff = DISTANT_KICKOFF + timedelta(hours=1)
        self.mb = f"evt:mb-{index}"
        self.pm = f"evt:pm-{index}"
        self.kal = f"evt:kal-{index}"
        self.mb_src = f"mb-{1000 + index}"
        self.pm_src = f"pm-{index}"
        self.kal_src = f"kal-{index}"
        self.ids = {self.mb, self.pm, self.kal, self.mb_src, self.pm_src, self.kal_src}
        self.co_observed: set[str] = set()
        self.tombstoned = False
        self.tombstone_ids: set[str] = set()

    def mark_observed(self, ids: set[str], *, clustered: bool) -> None:
        present = ids & self.ids
        if not present:
            return
        if clustered or present & self.co_observed:
            self.co_observed.update(present)


def _chaos_report(
    world: _FixtureWorld,
    kind: str,
    when: datetime,
    rng: random.Random,
) -> tuple[CollectionReport, ScanLane]:
    home, away, kickoff = world.home, world.away, world.kickoff
    if kind == "mb_pm":
        return (
            _mb_pm_cluster(
                world.mb,
                world.mb_src,
                world.pm_src,
                when=when,
                home=home,
                away=away,
                qualifying=True,
            ),
            ScanLane.UNIVERSE,
        )
    if kind == "mb_pm_kal":
        return (
            _mb_pm_cluster(
                world.mb,
                world.mb_src,
                world.pm_src,
                when=when,
                home=home,
                away=away,
                kal_src=world.kal_src,
                qualifying=True,
            ),
            ScanLane.UNIVERSE,
        )
    if kind == "pm_only":
        return (
            _single_venue(
                world.pm,
                source=VenueName.POLYMARKET,
                source_event_id=world.pm_src,
                when=when,
                qualifying=rng.random() < 0.7,
                home=home,
                away=away,
            ),
            ScanLane.UNIVERSE,
        )
    if kind == "kal_only":
        return (
            _single_venue(
                world.kal,
                source=VenueName.KALSHI,
                source_event_id=world.kal_src,
                when=when,
                qualifying=True,
                home=home,
                away=away,
            ),
            ScanLane.UNIVERSE,
        )
    if kind == "overlap_kal":
        return (
            _single_venue(
                world.kal,
                source=VenueName.KALSHI,
                source_event_id=world.kal_src,
                when=when,
                qualifying=True,
                home=home,
                away=away,
                extra_aliases={
                    world.mb_src: world.kal,
                    world.pm_src: world.kal,
                    world.mb: world.kal,
                },
                extra_events=[
                    {"venue": "matchbook", "source_event_id": world.mb_src, "raw": {"id": world.mb_src}},
                    {"venue": "polymarket", "source_event_id": world.pm_src, "raw": {"id": world.pm_src}},
                ],
            ),
            ScanLane.UNIVERSE,
        )
    if kind == "terminal_omit":
        status = rng.choice(["closed", "graded", "settled"])
        return (
            _single_venue(
                world.mb,
                source=VenueName.MATCHBOOK,
                source_event_id=world.mb_src,
                when=when,
                scan_lane=ScanLane.HOT.value,
                fixture_status=status,
                fixture_status_source=VenueName.MATCHBOOK,
                kickoff=when - timedelta(minutes=10),
                qualifying=True,
                home=home,
                away=away,
            ),
            ScanLane.HOT,
        )
    if kind == "eval_empty":
        return (
            _single_venue(
                world.mb,
                source=VenueName.MATCHBOOK,
                source_event_id=world.mb_src,
                when=when,
                scan_lane=ScanLane.HOT.value,
                qualifying=False,
                markets={},
                home=home,
                away=away,
            ),
            ScanLane.HOT,
        )
    if kind == "unavailable":
        return (
            _single_venue(
                world.mb,
                source=VenueName.MATCHBOOK,
                source_event_id=world.mb_src,
                when=when,
                scan_lane=ScanLane.HOT.value,
                evaluation=MarketEvaluationState.MARKET_FETCH_UNAVAILABLE.value,
                qualifying=False,
                markets={},
                home=home,
                away=away,
            ),
            ScanLane.HOT,
        )
    if kind == "delayed":
        return (
            _mb_pm_cluster(
                world.mb,
                world.mb_src,
                world.pm_src,
                when=when - timedelta(seconds=45),
                home=home,
                away=away,
            ),
            ScanLane.UNIVERSE,
        )
    return (
        _single_venue(
            world.mb,
            source=VenueName.MATCHBOOK,
            source_event_id=world.mb_src,
            when=when,
            qualifying=True,
            home=home,
            away=away,
            kickoff=kickoff,
        ),
        ScanLane.UNIVERSE,
    )


def _report_venues(report: CollectionReport) -> set[str]:
    venues: set[str] = set()
    for rows in report.fixture_source_events.values():
        for row in rows:
            venue = str(row.get("venue") or "").strip()
            if venue:
                venues.add(venue)
    return venues


def _report_ids(report: CollectionReport) -> set[str]:
    ids: set[str] = set()
    for fixture in report.discovered_fixtures:
        ids.add(fixture.canonical_event_id)
        ids.add(fixture.source_event_id)
    ids.update(report.fixture_identity_aliases)
    for rows in report.fixture_source_events.values():
        for row in rows:
            source_id = str(row.get("source_event_id") or "").strip()
            if source_id:
                ids.add(source_id)
    return {item for item in ids if item}


def _assert_chaos_invariants(
    store: FixtureCurrentStateStore,
    worlds: list[_FixtureWorld],
    now: datetime,
) -> None:
    hot = store.hot_identity_scope(now)
    assert len(hot) == len(set(hot))
    hot_n, _universe_n = store.membership_counts(now)
    assert hot_n == len(hot)
    live = set(_live_ids(store, now))

    for world in worlds:
        resolved = {store.resolve_canonical_id(item) for item in world.co_observed}
        resolved.discard(None)
        assert len(resolved) <= 1, (world.index, world.co_observed, resolved, live)
        if world.tombstoned:
            for identity in world.tombstone_ids:
                assert store.tombstone_for(identity) is not None, identity
                assert store.resolve_canonical_id(identity) is None, identity
            assert not (world.tombstone_ids & live)

    for left, right in zip(worlds, worlds[1:], strict=False):
        left_live = {store.resolve_canonical_id(item) for item in left.ids}
        right_live = {store.resolve_canonical_id(item) for item in right.ids}
        left_live.discard(None)
        right_live.discard(None)
        assert not (left_live & right_live), (left.index, right.index, left_live, right_live)


@pytest.mark.parametrize("seed", [220, 221, 7, 42, 99, 12345])
@pytest.mark.parametrize("seam", SEAMS)
def test_seeded_mb_pm_kalshi_identity_sequences(
    seed: int,
    seam: Callable[[], tuple[FixtureCurrentStateStore, ApplyFn]],
) -> None:
    store, apply = seam()
    rng = random.Random(seed)
    worlds = [_FixtureWorld(0), _FixtureWorld(1), _FixtureWorld(2)]
    kinds = (
        "mb_pm",
        "mb_pm_kal",
        "pm_only",
        "kal_only",
        "overlap_kal",
        "terminal_omit",
        "eval_empty",
        "unavailable",
        "delayed",
        "mb_only",
    )
    now = NOW
    for step in range(80):
        world = worlds[rng.randrange(len(worlds))]
        kind = kinds[rng.randrange(len(kinds))]
        if kind == "terminal_omit" and not world.co_observed:
            kind = "mb_pm"
        now = now + timedelta(seconds=20)
        report, lane = _chaos_report(world, kind, now, rng)
        observed = _report_ids(report)
        clustered = len(_report_venues(report)) >= 2
        world.mark_observed(observed, clustered=clustered)
        apply(report, lane, report.completed_at)
        if kind == "terminal_omit":
            world.tombstoned = True
            world.tombstone_ids = set(world.co_observed)
        _assert_chaos_invariants(store, worlds, report.completed_at)
        if kind == "delayed":
            record_id = store.resolve_canonical_id(world.mb)
            if record_id is not None:
                record = store._rows[record_id]
                status = record.status_observation()
                if status is not None:
                    assert status.last_scanned_at >= now - timedelta(seconds=20) or status.last_scanned_at <= now
    assert store.generation >= 80
