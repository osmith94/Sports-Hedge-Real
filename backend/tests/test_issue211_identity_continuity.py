"""Issue #211: canonical fixture identity continuity and stale-promotion correction.

Data class: deterministic fixture/demo current-state and paper-scan payloads.
Not live, historical, or modelled venue quotes.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from test_dual_cadence_scheduler import FakeClock
from test_issue200_universe_hot_promotion import (
    CANONICAL_ID,
    DISTANT_KICKOFF,
    NOW,
    CountingMatchbook,
    CountingPolymarket,
    _decision,
    _fixture,
    _market_row,
    _qualifying_universe_report,
    _report,
)
from test_read_only_collector import FakeMatchbook, FakePolymarket, KICKOFF as FAKE_KICKOFF
from venue_cost_helpers import matchbook_polymarket_costs

from sports_hedge.application.collector import (
    CollectionReport,
    MarketEvaluationState,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.current_market_inventory import (
    current_slots_prove_qualifying_opportunity,
)
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.fixture_inventory import InventoryComparisonStatus
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import (
    DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    DEFAULT_HOT_TTL_SECONDS,
    DEFAULT_UNIVERSE_TTL_SECONDS,
    ScanLane,
    classify_scan_lane,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot


MB_ANCHORED = "evt:matchbook-anchored"
PM_ANCHORED = "evt:polymarket-anchored"
MB_SOURCE = "1001"
PM_SOURCE = "pm-event-1"


def _identity_report(
    canonical_id: str,
    *,
    source: VenueName,
    source_event_id: str,
    aliases: dict[str, str],
    source_events: list[dict[str, Any]],
    when: datetime = NOW,
    scan_lane: str = ScanLane.UNIVERSE.value,
    markets: dict[str, list] | None = None,
    evaluation: str = "evaluated",
    opportunity: str = "unmatched",
    arb: bool = False,
    qualifying: int = 0,
    matchbook_matched: bool = False,
    polymarket_matched: bool = False,
    home: str = "Newcastle United",
    away: str = "Chelsea",
    kickoff: datetime = DISTANT_KICKOFF,
    fixture_status: str | None = None,
    in_running: bool | None = None,
) -> CollectionReport:
    fixture = _fixture(
        canonical_id,
        kickoff=kickoff,
        evaluation=evaluation,
        opportunity=opportunity,
        arb=arb,
        qualifying=qualifying,
        when=when,
        in_running=in_running,
        fixture_status=fixture_status,
    ).model_copy(
        update={
            "source": source,
            "source_event_id": source_event_id,
            "home_team": home,
            "away_team": away,
            "matchbook_matched": matchbook_matched,
            "polymarket_matched": polymarket_matched,
        }
    )
    return CollectionReport(
        started_at=when,
        completed_at=when,
        paper_decisions=[_decision(canonical_id, f"mkt-{canonical_id}", when=when)]
        if evaluation == "evaluated"
        else [],
        discovered_fixtures=[fixture],
        fixture_markets=markets or {},
        scan_lane=scan_lane,
        venue_health={"matchbook": "ok", "polymarket": "ok", "kalshi": "ok"},
        operator_summary="issue-211",
        fixture_identity_aliases=aliases,
        fixture_source_events={canonical_id: source_events},
    )


def _mb_pm_qualifying_report(
    canonical_id: str = MB_ANCHORED,
    *,
    when: datetime = NOW,
    scan_lane: str = ScanLane.UNIVERSE.value,
) -> CollectionReport:
    return _identity_report(
        canonical_id,
        source=VenueName.MATCHBOOK,
        source_event_id=MB_SOURCE,
        aliases={
            canonical_id: canonical_id,
            MB_SOURCE: canonical_id,
            PM_SOURCE: canonical_id,
            f"pair-{canonical_id}": canonical_id,
        },
        source_events=[
            {
                "venue": "matchbook",
                "source_event_id": MB_SOURCE,
                "raw": {"id": MB_SOURCE, "name": "Newcastle United vs Chelsea"},
            },
            {
                "venue": "polymarket",
                "source_event_id": PM_SOURCE,
                "raw": {"id": PM_SOURCE, "title": "Newcastle United vs Chelsea"},
            },
        ],
        when=when,
        scan_lane=scan_lane,
        markets={canonical_id: [_market_row()]},
        opportunity="qualifying",
        arb=True,
        qualifying=1,
        matchbook_matched=True,
        polymarket_matched=True,
    )


def _pm_only_report(
    canonical_id: str = PM_ANCHORED,
    *,
    when: datetime = NOW,
    scan_lane: str = ScanLane.UNIVERSE.value,
    evaluation: str = "evaluated",
    markets: dict[str, list] | None = None,
) -> CollectionReport:
    return _identity_report(
        canonical_id,
        source=VenueName.POLYMARKET,
        source_event_id=PM_SOURCE,
        aliases={
            canonical_id: canonical_id,
            PM_SOURCE: canonical_id,
        },
        source_events=[
            {
                "venue": "polymarket",
                "source_event_id": PM_SOURCE,
                "raw": {"id": PM_SOURCE, "title": "Newcastle United vs Chelsea"},
            },
        ],
        when=when,
        scan_lane=scan_lane,
        markets={} if markets is None else markets,
        evaluation=evaluation,
        opportunity="unmatched",
        matchbook_matched=False,
        polymarket_matched=True,
    )


def _live_ids(store: FixtureCurrentStateStore, now: datetime = NOW) -> set[str]:
    return {item.canonical_event_id for item in store.inventory(now)}


def _surviving_id(store: FixtureCurrentStateStore, *identities: str) -> str:
    resolved = {store.resolve_canonical_id(item) for item in identities}
    resolved.discard(None)
    assert len(resolved) == 1
    surviving = next(iter(resolved))
    assert surviving is not None
    return surviving


# ---------------------------------------------------------------------------
# Confirmed audit failures — reproduce then correct
# ---------------------------------------------------------------------------


def test_sequential_matchbook_to_polymarket_reanchor_does_not_fork_identity() -> None:
    """Matchbook+PM then PM-only re-anchor must merge, not create a second evt: row."""

    store = FixtureCurrentStateStore()
    store.upsert_from_report(_mb_pm_qualifying_report(), scan_lane=ScanLane.UNIVERSE, now=NOW)
    first_id = _surviving_id(store, MB_ANCHORED, MB_SOURCE, PM_SOURCE)
    assert store.hot_identity_scope(NOW) == [first_id]
    assert store.resolve_canonical_id(PM_SOURCE) == first_id

    later = NOW + timedelta(seconds=30)
    store.upsert_from_report(
        _pm_only_report(when=later),
        scan_lane=ScanLane.UNIVERSE,
        now=later,
    )

    surviving = _surviving_id(store, MB_ANCHORED, PM_ANCHORED, MB_SOURCE, PM_SOURCE)
    assert surviving == first_id
    assert store.resolve_canonical_id(PM_ANCHORED) == first_id
    assert store.resolve_canonical_id(PM_SOURCE) == first_id
    assert len(store._rows) == 1
    assert _live_ids(store, later) == {first_id}
    hot = store.hot_identity_scope(later)
    assert hot.count(first_id) <= 1
    assert PM_ANCHORED not in hot or hot == [first_id]
    assert len(hot) <= 1


def test_pm_only_then_matchbook_return_stays_one_canonical_id() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(_pm_only_report(), scan_lane=ScanLane.UNIVERSE, now=NOW)
    first_id = _surviving_id(store, PM_ANCHORED, PM_SOURCE)
    later = NOW + timedelta(seconds=30)
    store.upsert_from_report(
        _mb_pm_qualifying_report(when=later),
        scan_lane=ScanLane.UNIVERSE,
        now=later,
    )
    surviving = _surviving_id(store, MB_ANCHORED, PM_ANCHORED, MB_SOURCE, PM_SOURCE)
    assert surviving == first_id
    assert len(store._rows) == 1
    assert store.hot_identity_scope(later) == [first_id]


def test_duplicate_source_event_in_same_scan_stays_one_id() -> None:
    store = FixtureCurrentStateStore()
    first = _mb_pm_qualifying_report()
    duplicate = _identity_report(
        "evt:duplicate-matchbook",
        source=VenueName.MATCHBOOK,
        source_event_id="1002",
        aliases={
            "evt:duplicate-matchbook": "evt:duplicate-matchbook",
            "1002": "evt:duplicate-matchbook",
            PM_SOURCE: "evt:duplicate-matchbook",
        },
        source_events=[
            {
                "venue": "matchbook",
                "source_event_id": "1002",
                "raw": {"id": "1002", "name": "Newcastle United vs Chelsea"},
            },
            {
                "venue": "polymarket",
                "source_event_id": PM_SOURCE,
                "raw": {"id": PM_SOURCE, "title": "Newcastle United vs Chelsea"},
            },
        ],
        markets={"evt:duplicate-matchbook": [_market_row()]},
        opportunity="qualifying",
        arb=True,
        qualifying=1,
        matchbook_matched=True,
        polymarket_matched=True,
    )
    merged = first.model_copy(
        update={
            "discovered_fixtures": list(first.discovered_fixtures) + list(duplicate.discovered_fixtures),
            "fixture_markets": {**first.fixture_markets, **duplicate.fixture_markets},
            "fixture_identity_aliases": {
                **first.fixture_identity_aliases,
                **duplicate.fixture_identity_aliases,
            },
            "fixture_source_events": {
                **first.fixture_source_events,
                **duplicate.fixture_source_events,
            },
        }
    )
    store.upsert_from_report(merged, scan_lane=ScanLane.UNIVERSE, now=NOW)
    surviving = _surviving_id(store, MB_ANCHORED, "evt:duplicate-matchbook", PM_SOURCE)
    assert len(store._rows) == 1
    assert store.hot_identity_scope(NOW) == [surviving]


def test_evaluated_empty_hot_inventory_demotes_stale_promotion() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(_qualifying_universe_report(), scan_lane=ScanLane.UNIVERSE, now=NOW)
    assert CANONICAL_ID in store.hot_identity_scope(NOW)

    later = NOW + timedelta(seconds=30)
    empty_hot = _fixture(
        opportunity="unmatched",
        arb=False,
        qualifying=0,
        when=later,
    )
    store.upsert_from_report(
        _report(
            [empty_hot],
            when=later,
            scan_lane=ScanLane.HOT.value,
            markets={},
            decisions=[_decision(CANONICAL_ID, f"mkt-{CANONICAL_ID}", when=later)],
        ),
        scan_lane=ScanLane.HOT,
        now=later,
    )
    assert CANONICAL_ID not in store.hot_identity_scope(later)
    hot, universe = store.membership_counts(later)
    assert hot == 0
    assert universe == 1
    record = store._rows[CANONICAL_ID]
    assert not current_slots_prove_qualifying_opportunity(
        record.live_market_slots(),
        now=later,
        hot_ttl_seconds=DEFAULT_HOT_TTL_SECONDS,
        universe_ttl_seconds=DEFAULT_UNIVERSE_TTL_SECONDS,
        max_quote_age_ms=DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    )


def test_provider_timeout_unevaluated_hot_does_not_falsely_demote() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(_qualifying_universe_report(), scan_lane=ScanLane.UNIVERSE, now=NOW)
    later = NOW + timedelta(seconds=30)
    timeout = _fixture(
        evaluation=MarketEvaluationState.NOT_EVALUATED_SCAN_DEADLINE.value,
        opportunity="not_evaluated",
        arb=False,
        qualifying=0,
        when=later,
    )
    store.upsert_from_report(
        _report(
            [timeout],
            when=later,
            scan_lane=ScanLane.HOT.value,
            markets={},
            decisions=[],
        ),
        scan_lane=ScanLane.HOT,
        now=later,
    )
    assert CANONICAL_ID in store.hot_identity_scope(later)

    store.clear()
    store.upsert_from_report(_qualifying_universe_report(), scan_lane=ScanLane.UNIVERSE, now=NOW)
    unavailable = _fixture(
        evaluation=MarketEvaluationState.MARKET_FETCH_UNAVAILABLE.value,
        opportunity="not_evaluated",
        arb=False,
        qualifying=0,
        when=later,
    )
    store.upsert_from_report(
        _report(
            [unavailable],
            when=later,
            scan_lane=ScanLane.HOT.value,
            markets={},
            decisions=[],
        ),
        scan_lane=ScanLane.HOT,
        now=later,
    )
    assert CANONICAL_ID in store.hot_identity_scope(later)


def test_qualify_demote_requalify_fresh_same_identity() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(_qualifying_universe_report(), scan_lane=ScanLane.UNIVERSE, now=NOW)
    demoted_at = NOW + timedelta(seconds=30)
    store.upsert_from_report(
        _report(
            [_fixture(opportunity="unmatched", arb=False, qualifying=0, when=demoted_at)],
            when=demoted_at,
            scan_lane=ScanLane.HOT.value,
            markets={},
        ),
        scan_lane=ScanLane.HOT,
        now=demoted_at,
    )
    assert CANONICAL_ID not in store.hot_identity_scope(demoted_at)

    requalify_at = demoted_at + timedelta(seconds=30)
    store.upsert_from_report(
        _qualifying_universe_report(when=requalify_at),
        scan_lane=ScanLane.HOT,
        now=requalify_at,
    )
    assert store.hot_identity_scope(requalify_at) == [CANONICAL_ID]
    assert len(store._rows) == 1


def test_delayed_older_hot_snapshot_cannot_become_selected_or_due_time() -> None:
    store = FixtureCurrentStateStore()
    universe_at = NOW + timedelta(seconds=10)
    store.upsert_from_report(
        _qualifying_universe_report(when=universe_at),
        scan_lane=ScanLane.UNIVERSE,
        now=universe_at,
    )
    assert CANONICAL_ID in store.hot_identity_scope(universe_at)
    record = store._rows[CANONICAL_ID]
    assert record.hot is None
    assert record.universe is not None
    assert record.universe.last_scanned_at == universe_at

    delayed_hot = NOW
    store.upsert_from_report(
        _report(
            [_fixture(arb=True, qualifying=1, opportunity="qualifying", when=delayed_hot)],
            when=delayed_hot,
            scan_lane=ScanLane.HOT.value,
            markets={CANONICAL_ID: [_market_row()]},
        ),
        scan_lane=ScanLane.HOT,
        now=delayed_hot,
    )
    record = store._rows[CANONICAL_ID]
    assert record.hot is None
    selected = record.selected_observation(ScanLane.HOT)
    assert selected is None or selected.last_scanned_at >= universe_at
    _lane, scanned = record.scheduler_lane_scan(
        ScanLane.HOT, record.status_fixture(universe_at + timedelta(seconds=1))
    )
    assert scanned >= universe_at
    rows = store.current_radar_rows(universe_at + timedelta(seconds=1))
    assert rows
    assert rows[0].last_scanned_at >= universe_at
    assert rows[0].last_scanned_at != delayed_hot


def test_delayed_older_universe_snapshot_cannot_change_status_or_due_time() -> None:
    store = FixtureCurrentStateStore()
    hot_at = NOW + timedelta(seconds=10)
    store.upsert_from_report(
        _qualifying_universe_report(when=hot_at),
        scan_lane=ScanLane.HOT,
        now=hot_at,
    )
    delayed_universe = NOW
    store.upsert_from_report(
        _qualifying_universe_report(when=delayed_universe),
        scan_lane=ScanLane.UNIVERSE,
        now=delayed_universe,
    )
    record = store._rows[CANONICAL_ID]
    assert record.universe is None or record.universe.last_scanned_at >= hot_at
    status = record.status_observation()
    assert status is not None
    assert status.last_scanned_at >= hot_at


def test_same_key_nonqualifying_hot_still_demotes() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(_qualifying_universe_report(), scan_lane=ScanLane.UNIVERSE, now=NOW)
    later = NOW + timedelta(seconds=30)
    store.upsert_from_report(
        _report(
            [_fixture(opportunity="matched", arb=False, qualifying=0, when=later)],
            when=later,
            scan_lane=ScanLane.HOT.value,
            markets={CANONICAL_ID: [_market_row(edge=Decimal("0.002"), arb=False)]},
        ),
        scan_lane=ScanLane.HOT,
        now=later,
    )
    assert CANONICAL_ID not in store.hot_identity_scope(later)


def test_opportunity_hot_crossing_t60m_stays_one_membership() -> None:
    store = FixtureCurrentStateStore()
    kickoff = NOW + timedelta(minutes=90)
    store.upsert_from_report(
        _qualifying_universe_report(kickoff=kickoff),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    assert CANONICAL_ID in store.hot_identity_scope(NOW)
    assert classify_scan_lane(_fixture(kickoff=kickoff), NOW) is ScanLane.UNIVERSE

    later = NOW + timedelta(minutes=35)
    crossed = _fixture(
        kickoff=kickoff,
        arb=True,
        qualifying=1,
        opportunity="qualifying",
        when=later,
    )
    assert classify_scan_lane(crossed, later) is ScanLane.HOT
    store.upsert_from_report(
        _report(
            [crossed],
            when=later,
            scan_lane=ScanLane.HOT.value,
            markets={CANONICAL_ID: [_market_row()]},
        ),
        scan_lane=ScanLane.HOT,
        now=later,
    )
    assert store.hot_identity_scope(later) == [CANONICAL_ID]
    hot, universe = store.membership_counts(later)
    assert hot == 1
    assert universe == 0
    assert len(store._rows) == 1


def test_terminal_matchbook_tombstone_wins_and_later_pm_cannot_resurrect() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(_mb_pm_qualifying_report(), scan_lane=ScanLane.UNIVERSE, now=NOW)
    first_id = _surviving_id(store, MB_ANCHORED, PM_SOURCE)
    assert first_id in store.hot_identity_scope(NOW)

    closed_at = NOW + timedelta(seconds=30)
    closed = _identity_report(
        MB_ANCHORED,
        source=VenueName.MATCHBOOK,
        source_event_id=MB_SOURCE,
        aliases={
            MB_ANCHORED: MB_ANCHORED,
            MB_SOURCE: MB_ANCHORED,
            PM_SOURCE: MB_ANCHORED,
        },
        source_events=[
            {
                "venue": "matchbook",
                "source_event_id": MB_SOURCE,
                "raw": {"id": MB_SOURCE, "status": "closed"},
            }
        ],
        when=closed_at,
        scan_lane=ScanLane.HOT.value,
        markets={MB_ANCHORED: [_market_row()]},
        opportunity="qualifying",
        arb=True,
        qualifying=1,
        matchbook_matched=True,
        polymarket_matched=True,
        fixture_status="closed",
        kickoff=NOW - timedelta(minutes=10),
    )
    store.upsert_from_report(closed, scan_lane=ScanLane.HOT, now=closed_at)
    assert store.hot_identity_scope(closed_at) == []
    assert store.tombstone_for(MB_ANCHORED) is not None
    assert store.tombstone_for(PM_SOURCE) is not None

    later = closed_at + timedelta(seconds=30)
    store.upsert_from_report(_pm_only_report(when=later), scan_lane=ScanLane.UNIVERSE, now=later)
    store.upsert_from_report(
        _mb_pm_qualifying_report(canonical_id=PM_ANCHORED, when=later),
        scan_lane=ScanLane.UNIVERSE,
        now=later,
    )
    assert store.resolve_canonical_id(MB_ANCHORED) is None
    assert store.resolve_canonical_id(PM_ANCHORED) is None
    assert store.resolve_canonical_id(PM_SOURCE) is None
    assert store.hot_identity_scope(later) == []
    assert store._rows == {}


def test_different_opponents_do_not_merge_without_source_overlap() -> None:
    store = FixtureCurrentStateStore()
    newcastle = _identity_report(
        "evt:ncl-che",
        source=VenueName.MATCHBOOK,
        source_event_id="mb-ncl",
        aliases={"evt:ncl-che": "evt:ncl-che", "mb-ncl": "evt:ncl-che"},
        source_events=[
            {
                "venue": "matchbook",
                "source_event_id": "mb-ncl",
                "raw": {"id": "mb-ncl"},
            }
        ],
        markets={"evt:ncl-che": [_market_row()]},
        opportunity="qualifying",
        arb=True,
        qualifying=1,
        matchbook_matched=True,
        home="Newcastle United",
        away="Chelsea",
    )
    arsenal = _identity_report(
        "evt:ars-liv",
        source=VenueName.POLYMARKET,
        source_event_id="pm-ars",
        aliases={"evt:ars-liv": "evt:ars-liv", "pm-ars": "evt:ars-liv"},
        source_events=[
            {
                "venue": "polymarket",
                "source_event_id": "pm-ars",
                "raw": {"id": "pm-ars"},
            }
        ],
        home="Arsenal",
        away="Liverpool",
        polymarket_matched=True,
    )
    store.upsert_from_report(newcastle, scan_lane=ScanLane.UNIVERSE, now=NOW)
    store.upsert_from_report(
        arsenal,
        scan_lane=ScanLane.UNIVERSE,
        now=NOW + timedelta(seconds=5),
    )
    assert store.resolve_canonical_id("evt:ncl-che") == "evt:ncl-che"
    assert store.resolve_canonical_id("evt:ars-liv") == "evt:ars-liv"
    assert store.resolve_canonical_id("mb-ncl") != store.resolve_canonical_id("pm-ars")
    assert len(store._rows) == 2


def test_radar_current_executable_stale_still_promotes_but_not_as_executable() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(_qualifying_universe_report(), scan_lane=ScanLane.UNIVERSE, now=NOW)
    still_radar = NOW + timedelta(seconds=30)
    assert still_radar - NOW > timedelta(milliseconds=DEFAULT_EXECUTABLE_QUOTE_AGE_MS)
    assert CANONICAL_ID in store.hot_identity_scope(still_radar)
    rows = store.current_radar_rows(still_radar)
    assert rows
    assert rows[0].freshness != "executable"


def test_hot_partial_refresh_does_not_drop_other_family_from_universe() -> None:
    from sports_hedge.application.fixture_inventory import (
        FixtureMarketInventoryRow,
        InventoryPairResult,
        VenueMarketFacts,
        VenueQuoteFact,
    )

    def moneyline_row() -> FixtureMarketInventoryRow:
        facts_mb = VenueMarketFacts(
            venue=VenueName.MATCHBOOK,
            source_event_id="matchbook-t3d-qualifying",
            source_market_id="mb-ml",
            family="match_result",
            period="full_time",
            settlement_key="regulation_time|full_time",
            settlement_complete=True,
            best_backs=[
                VenueQuoteFact(outcome="home", decimal_odds=Decimal("2.40"), size_at_touch=Decimal(100)),
                VenueQuoteFact(outcome="draw", decimal_odds=Decimal("3.50"), size_at_touch=Decimal(100)),
                VenueQuoteFact(outcome="away", decimal_odds=Decimal("3.10"), size_at_touch=Decimal(100)),
            ],
            quote_age_ms=80,
            quote_age_basis="source",
            native_currency="GBP",
        )
        facts_pm = facts_mb.model_copy(
            update={
                "venue": VenueName.POLYMARKET,
                "source_event_id": "polymarket-t3d-qualifying",
                "source_market_id": "pm-ml",
                "native_currency": "USD",
            }
        )
        return FixtureMarketInventoryRow(
            display_name="Match Result",
            family="match_result",
            period="full_time",
            comparison_status=InventoryComparisonStatus.MATCHED_EQUIVALENT,
            rejection_reasons=[],
            match_reasons=[],
            entered_solver=True,
            solver_model="strict_complete_set",
            current_net_edge=Decimal("-0.004"),
            trigger_net_edge=Decimal("0.01"),
            solver_is_arbitrage=False,
            matchbook=facts_mb,
            polymarket=facts_pm,
            pair_results=[
                InventoryPairResult(
                    left_venue=VenueName.MATCHBOOK,
                    right_venue=VenueName.POLYMARKET,
                    entered_solver=True,
                    solver_model="strict_complete_set",
                    current_net_edge=Decimal("-0.004"),
                    rejection_reasons=[],
                    solver_is_arbitrage=False,
                )
            ],
        )

    store = FixtureCurrentStateStore()
    fixture = _fixture(arb=True, qualifying=1, opportunity="qualifying")
    store.upsert_from_report(
        _report(
            [fixture],
            markets={CANONICAL_ID: [_market_row(), moneyline_row()]},
        ),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    later = NOW + timedelta(seconds=30)
    store.upsert_from_report(
        _report(
            [_fixture(opportunity="qualifying", arb=True, qualifying=1, when=later)],
            when=later,
            scan_lane=ScanLane.HOT.value,
            markets={CANONICAL_ID: [_market_row()]},
        ),
        scan_lane=ScanLane.HOT,
        now=later,
    )
    families = {row.family for row in store.detail(CANONICAL_ID, now=later).markets}
    assert families == {"both_teams_to_score", "match_result"}
    assert CANONICAL_ID in store.hot_identity_scope(later)


@pytest.mark.asyncio
async def test_collector_duplicate_matchbook_events_cluster_to_one_fixture() -> None:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=FakeMatchbook(duplicate_event=True),
        polymarket=FakePolymarket(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        cycle_timeout_seconds=8,
    )
    try:
        report = await collector.collect_and_scan(
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            maximum_execution_risk=100,
            max_event_pairs=8,
        )
        newcastle = [
            item
            for item in report.discovered_fixtures
            if item.home_team == "Newcastle United" and item.away_team == "Chelsea"
        ]
        assert len(newcastle) == 1
        store = FixtureCurrentStateStore()
        store.upsert_from_report(report, scan_lane=ScanLane.UNIVERSE, now=report.completed_at)
        assert len([row for row in store.inventory(report.completed_at) if row.home_team == "Newcastle United"]) == 1
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_collector_sequential_reanchor_merges_into_existing_identity() -> None:
    repository = SqliteMarketIntelligenceRepository()
    matchbook = CountingMatchbook()
    polymarket = CountingPolymarket()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        cycle_timeout_seconds=8,
    )
    coordinator = LiveRefreshCoordinator(clock=FakeClock(FAKE_KICKOFF - timedelta(days=3)))
    coordinator.reset()
    try:
        first = await collector.collect_and_scan(
            scan_lane=ScanLane.UNIVERSE.value,
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            maximum_execution_risk=100,
            max_event_pairs=8,
        )
        coordinator.record_report(first, scan_lane=ScanLane.UNIVERSE)
        store = coordinator.fixture_current_state()
        newcastle = [
            item
            for item in store.inventory(first.completed_at)
            if item.home_team == "Newcastle United" and item.away_team == "Chelsea"
        ]
        assert len(newcastle) == 1
        first_id = newcastle[0].canonical_event_id
        assert first_id in store.hot_identity_scope(first.completed_at)
        assert store.resolve_canonical_id("pm-event-1") == first_id

        second = await collector.collect_and_scan(
            scan_lane=ScanLane.UNIVERSE.value,
            enabled_venues=[VenueName.POLYMARKET],
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            maximum_execution_risk=100,
            max_event_pairs=8,
        )
        coordinator.record_report(second, scan_lane=ScanLane.UNIVERSE)
        later_rows = [
            item
            for item in store.inventory(second.completed_at)
            if item.home_team == "Newcastle United" and item.away_team == "Chelsea"
        ]
        assert len(later_rows) == 1
        surviving = later_rows[0].canonical_event_id
        assert surviving == first_id
        assert store.resolve_canonical_id("pm-event-1") == first_id
        assert len([key for key in store._rows if store._rows[key].status_fixture() and store._rows[key].status_fixture().home_team == "Newcastle United"]) == 1
        hot = store.hot_identity_scope(second.completed_at)
        assert hot.count(first_id) <= 1
        assert all(
            store.resolve_canonical_id(item) in {first_id, None} or item == first_id
            for item in hot
        )
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_issue200_hot_known_source_no_rediscovery_still_holds() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator.reset()
    coordinator._clock = clock
    coordinator.record_report(_qualifying_universe_report(), scan_lane=ScanLane.UNIVERSE)
    clock.advance(30)
    plan = coordinator.plan_tick(now=clock.now)
    assert plan.lane == "hot"
    assert CANONICAL_ID in plan.identity_scope
    matchbook = CountingMatchbook()
    polymarket = CountingPolymarket()
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        cycle_timeout_seconds=5,
    )
    try:
        report = await collector.collect_and_scan(
            scan_lane=plan.lane,
            identity_scope=plan.identity_scope,
            known_source_events=plan.known_source_events,
            cycle_timeout_seconds=plan.collector_timeout_seconds,
            max_event_pairs=8,
        )
        assert matchbook.list_events_calls == 0
        assert polymarket.list_events_calls == 0
        assert report.scan_lane == ScanLane.HOT.value
    finally:
        repository.close()
