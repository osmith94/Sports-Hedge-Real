"""Issue #308: generation-aware ApprovedEquivalent vs quote freshness.

Owner-live: a ~21-minute UNIVERSE generation finished 77/77, but early fixture
rows showed Last refresh 17m ago with Equivalent=0 / unmatched after market
slots were pruned by the 360s lane TTL.

Deterministic current-state fixtures. Not live, historical, or modelled quotes.
Does not raise paper_universe_current_state_ttl_seconds.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from sports_hedge.application.current_market_inventory import (
    candidate_from_current_slot,
)
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.fixture_inventory import FixtureMarketInventoryRow
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.scan_lanes import (
    DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    DEFAULT_UNIVERSE_TTL_SECONDS,
    FRESHNESS_EXECUTABLE,
    ScanLane,
)
from sports_hedge.config import Settings, get_settings
from test_current_market_inventory import (
    NOW,
    _decision,
    _fixture,
    _market_row,
)
from test_dual_cadence_scheduler import FakeClock
from test_issue200_universe_hot_promotion import _qualifying_universe_report

GENERATION_ID = 7
FIXTURE_A = "evt:early-equivalent"
FIXTURE_B = "evt:late-universe-work"
TWENTY_MINUTES = 20 * 60
SEVENTEEN_MINUTES = 17 * 60


def _row(*, arb: bool = False, edge: Decimal | None = Decimal("0.012")) -> FixtureMarketInventoryRow:
    return _market_row(
        family="both_teams_to_score",
        edge=edge,
        arb=arb,
        quote_age_ms=80,
    )


def _evaluated_fixture(
    canonical_id: str,
    *,
    when,
    equivalent: int = 1,
    arb: bool = False,
    opportunity: str = "matched",
):
    fixture = _fixture(
        equivalent=equivalent,
        when=when,
        arb=arb,
        opportunity=opportunity,
        qualifying=1 if arb else 0,
        best_arb="BTTS" if arb else None,
    )
    return fixture.model_copy(
        update={
            "canonical_event_id": canonical_id,
            "source_event_id": f"mb-{canonical_id}",
            "last_scanned_at": when,
            "kickoff_utc": NOW + timedelta(days=3),
        }
    )


def _upsert_universe(
    store: FixtureCurrentStateStore,
    canonical_id: str,
    *,
    when,
    markets: list[FixtureMarketInventoryRow],
    equivalent: int = 1,
    arb: bool = False,
    opportunity: str = "matched",
    generation_id: int | None = GENERATION_ID,
) -> None:
    fixture = _evaluated_fixture(
        canonical_id,
        when=when,
        equivalent=equivalent,
        arb=arb,
        opportunity=opportunity,
    )
    store.upsert_evaluated_fixture(
        fixture,
        markets=markets,
        decisions=[_decision("mkt-btts", when=when).model_copy(
            update={
                "canonical_event_id": canonical_id,
                "fixture_canonical_event_id": canonical_id,
            }
        )],
        aliases={canonical_id: canonical_id, f"mb-{canonical_id}": canonical_id},
        scan_lane=ScanLane.UNIVERSE,
        now=when,
        universe_generation_id=generation_id,
    )


def _inventory_row(store: FixtureCurrentStateStore, canonical_id: str, when):
    rows = {item.canonical_event_id: item for item in store.inventory(when)}
    return rows.get(canonical_id)


def test_universe_ttl_config_is_unchanged() -> None:
    settings = get_settings()
    assert settings.paper_universe_current_state_ttl_seconds == 360
    assert DEFAULT_UNIVERSE_TTL_SECONDS == 360
    field = Settings.model_fields["paper_universe_current_state_ttl_seconds"]
    assert field.default == 360
    assert DEFAULT_EXECUTABLE_QUOTE_AGE_MS == 1000


def test_twenty_minute_universe_generation_keeps_early_approved_equivalent() -> None:
    store = FixtureCurrentStateStore()
    store.open_universe_generation(GENERATION_ID, started_at=NOW)
    _upsert_universe(
        store,
        FIXTURE_A,
        when=NOW,
        markets=[_row(arb=True, edge=Decimal("0.012"))],
        arb=True,
        opportunity="qualifying",
    )

    later = NOW + timedelta(minutes=1)
    _upsert_universe(store, FIXTURE_B, when=later, markets=[_row(edge=Decimal("-0.004"))])

    complete_at = NOW + timedelta(seconds=TWENTY_MINUTES)
    early = _inventory_row(store, FIXTURE_A, complete_at)
    assert early is not None
    assert early.matchbook_matched is True
    assert early.kalshi_matched is True
    assert early.matched_equivalent_count >= 1
    assert early.opportunity_state != "unmatched"
    assert early.opportunity_state == "matched"
    assert early.solver_is_arbitrage is False
    assert early.qualifying_market_count == 0
    assert early.current_net_edge is None
    assert complete_at - NOW > timedelta(seconds=DEFAULT_UNIVERSE_TTL_SECONDS)

    store.close_universe_generation(GENERATION_ID, closed_at=complete_at)
    after_close = _inventory_row(store, FIXTURE_A, complete_at)
    assert after_close is not None
    assert after_close.matched_equivalent_count >= 1
    assert after_close.opportunity_state == "matched"


def test_stale_quotes_keep_relationship_but_block_solver_paper_and_hot() -> None:
    store = FixtureCurrentStateStore()
    store.open_universe_generation(GENERATION_ID, started_at=NOW)
    _upsert_universe(
        store,
        FIXTURE_A,
        when=NOW,
        markets=[_row(arb=True, edge=Decimal("0.012"))],
        arb=True,
        opportunity="qualifying",
    )

    executable_stale = NOW + timedelta(seconds=5)
    assert executable_stale - NOW > timedelta(milliseconds=DEFAULT_EXECUTABLE_QUOTE_AGE_MS)
    row = _inventory_row(store, FIXTURE_A, executable_stale)
    assert row is not None
    assert row.matched_equivalent_count >= 1
    assert row.qualifying_market_count == 0
    assert row.solver_is_arbitrage is False
    assert row.current_net_edge is None
    detail = store.detail(FIXTURE_A, now=executable_stale)
    assert detail is not None
    assert detail.markets
    slot = next(iter((store._rows[FIXTURE_A].markets or {}).values()))
    candidate = candidate_from_current_slot(slot, now=executable_stale)
    assert candidate.eligible_for_paper_simulation is False
    assert detail.markets[0].radar_freshness != FRESHNESS_EXECUTABLE

    radar_expired = NOW + timedelta(seconds=SEVENTEEN_MINUTES)
    aged = _inventory_row(store, FIXTURE_A, radar_expired)
    assert aged is not None
    assert aged.matched_equivalent_count >= 1
    assert aged.opportunity_state == "matched"
    assert aged.solver_is_arbitrage is False
    assert FIXTURE_A not in store.hot_identity_scope(radar_expired)
    radar = store.current_radar_rows(radar_expired)
    assert all(item.canonical_event_id != FIXTURE_A or item.freshness != FRESHNESS_EXECUTABLE for item in radar)


def test_next_generation_removes_relationship_when_market_gone() -> None:
    store = FixtureCurrentStateStore()
    store.open_universe_generation(GENERATION_ID, started_at=NOW)
    _upsert_universe(
        store,
        FIXTURE_A,
        when=NOW,
        markets=[_row(arb=True)],
        arb=True,
        opportunity="qualifying",
    )
    closed_at = NOW + timedelta(seconds=TWENTY_MINUTES)
    store.close_universe_generation(GENERATION_ID, closed_at=closed_at)

    next_generation = GENERATION_ID + 1
    reeval_at = closed_at + timedelta(seconds=8)
    store.open_universe_generation(next_generation, started_at=reeval_at)
    gone = _evaluated_fixture(FIXTURE_A, when=reeval_at, equivalent=0, opportunity="unmatched")
    store.upsert_evaluated_fixture(
        gone,
        markets=[],
        decisions=[],
        aliases={FIXTURE_A: FIXTURE_A},
        scan_lane=ScanLane.UNIVERSE,
        now=reeval_at,
        universe_generation_id=next_generation,
    )
    row = _inventory_row(store, FIXTURE_A, reeval_at)
    assert row is not None
    assert row.matched_equivalent_count == 0
    assert row.opportunity_state == "unmatched"


def test_omitted_later_generation_does_not_extend_proving_close_ttl() -> None:
    """gen8 opens 8s later but never sees A: A still expires at gen7_close + 360s."""

    store = FixtureCurrentStateStore()
    store.open_universe_generation(GENERATION_ID, started_at=NOW)
    _upsert_universe(store, FIXTURE_A, when=NOW, markets=[_row()])
    gen7_close = NOW + timedelta(seconds=TWENTY_MINUTES)
    store.close_universe_generation(GENERATION_ID, closed_at=gen7_close)

    gen8 = GENERATION_ID + 1
    gen8_start = gen7_close + timedelta(seconds=8)
    store.open_universe_generation(gen8, started_at=gen8_start)
    _upsert_universe(store, FIXTURE_B, when=gen8_start, markets=[_row()], generation_id=gen8)

    still_current = gen7_close + timedelta(seconds=DEFAULT_UNIVERSE_TTL_SECONDS - 1)
    row = _inventory_row(store, FIXTURE_A, still_current)
    assert row is not None
    assert row.matched_equivalent_count >= 1
    assert row.opportunity_state == "matched"
    assert row.solver_is_arbitrage is False
    assert row.current_net_edge is None
    assert FIXTURE_A not in store.hot_identity_scope(still_current)

    expired = gen7_close + timedelta(seconds=DEFAULT_UNIVERSE_TTL_SECONDS + 1)
    assert expired > gen8_start
    gone = _inventory_row(store, FIXTURE_A, expired)
    if gone is not None:
        assert gone.matched_equivalent_count == 0
        assert gone.opportunity_state == "unmatched"


def test_repeated_omitted_generations_cannot_keep_relationship_forever() -> None:
    store = FixtureCurrentStateStore()
    store.open_universe_generation(GENERATION_ID, started_at=NOW)
    _upsert_universe(store, FIXTURE_A, when=NOW, markets=[_row()])
    gen7_close = NOW + timedelta(seconds=TWENTY_MINUTES)
    store.close_universe_generation(GENERATION_ID, closed_at=gen7_close)

    gen8_start = gen7_close + timedelta(seconds=8)
    store.open_universe_generation(GENERATION_ID + 1, started_at=gen8_start)
    gen8_close = gen8_start + timedelta(seconds=TWENTY_MINUTES)
    store.close_universe_generation(GENERATION_ID + 1, closed_at=gen8_close)
    gen9_start = gen8_close + timedelta(seconds=8)
    store.open_universe_generation(GENERATION_ID + 2, started_at=gen9_start)

    during_gen8 = gen7_close + timedelta(seconds=DEFAULT_UNIVERSE_TTL_SECONDS + 1)
    gone_during_gen8 = _inventory_row(store, FIXTURE_A, during_gen8)
    if gone_during_gen8 is not None:
        assert gone_during_gen8.matched_equivalent_count == 0

    after_later_closes = gen9_start + timedelta(seconds=30)
    gone = _inventory_row(store, FIXTURE_A, after_later_closes)
    if gone is not None:
        assert gone.matched_equivalent_count == 0
        assert gone.opportunity_state == "unmatched"


def test_later_generation_reproof_starts_new_lifecycle() -> None:
    store = FixtureCurrentStateStore()
    store.open_universe_generation(GENERATION_ID, started_at=NOW)
    _upsert_universe(store, FIXTURE_A, when=NOW, markets=[_row()])
    gen7_close = NOW + timedelta(seconds=TWENTY_MINUTES)
    store.close_universe_generation(GENERATION_ID, closed_at=gen7_close)

    gen8 = GENERATION_ID + 1
    gen8_start = gen7_close + timedelta(seconds=8)
    store.open_universe_generation(gen8, started_at=gen8_start)
    _upsert_universe(store, FIXTURE_A, when=gen8_start, markets=[_row()], generation_id=gen8)

    past_gen7_ttl = gen7_close + timedelta(seconds=DEFAULT_UNIVERSE_TTL_SECONDS + 1)
    still_gen8 = _inventory_row(store, FIXTURE_A, past_gen7_ttl)
    assert still_gen8 is not None
    assert still_gen8.matched_equivalent_count >= 1

    gen8_complete = gen8_start + timedelta(seconds=TWENTY_MINUTES)
    during_gen8 = _inventory_row(store, FIXTURE_A, gen8_complete)
    assert during_gen8 is not None
    assert during_gen8.matched_equivalent_count >= 1
    store.close_universe_generation(gen8, closed_at=gen8_complete)
    after_close = _inventory_row(store, FIXTURE_A, gen8_complete)
    assert after_close is not None
    assert after_close.matched_equivalent_count >= 1

    expired = gen8_complete + timedelta(seconds=DEFAULT_UNIVERSE_TTL_SECONDS + 1)
    gone = _inventory_row(store, FIXTURE_A, expired)
    if gone is not None:
        assert gone.matched_equivalent_count == 0
        assert gone.opportunity_state == "unmatched"


def test_post_generation_idle_ttl_expires_relationship() -> None:
    store = FixtureCurrentStateStore()
    store.open_universe_generation(GENERATION_ID, started_at=NOW)
    _upsert_universe(store, FIXTURE_A, when=NOW, markets=[_row()])
    closed_at = NOW + timedelta(seconds=TWENTY_MINUTES)
    store.close_universe_generation(GENERATION_ID, closed_at=closed_at)

    still_current = closed_at + timedelta(seconds=DEFAULT_UNIVERSE_TTL_SECONDS - 1)
    assert _inventory_row(store, FIXTURE_A, still_current).matched_equivalent_count >= 1

    expired = closed_at + timedelta(seconds=DEFAULT_UNIVERSE_TTL_SECONDS + 1)
    row = _inventory_row(store, FIXTURE_A, expired)
    if row is not None:
        assert row.matched_equivalent_count == 0
        assert row.opportunity_state == "unmatched"


def test_unstamped_slots_still_follow_lane_ttl() -> None:
    store = FixtureCurrentStateStore()
    _upsert_universe(
        store,
        FIXTURE_A,
        when=NOW,
        markets=[_row()],
        generation_id=None,
    )
    expired = NOW + timedelta(seconds=DEFAULT_UNIVERSE_TTL_SECONDS + 1)
    row = _inventory_row(store, FIXTURE_A, expired)
    if row is not None:
        assert row.matched_equivalent_count == 0
        assert row.opportunity_state == "unmatched"


def test_hot_does_not_promote_from_expired_price_evidence() -> None:
    store = FixtureCurrentStateStore()
    store.open_universe_generation(GENERATION_ID, started_at=NOW)
    store.upsert_from_report(
        _qualifying_universe_report(when=NOW),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
        universe_generation_id=GENERATION_ID,
    )
    aged = NOW + timedelta(seconds=DEFAULT_UNIVERSE_TTL_SECONDS + 1)
    scope = store.hot_identity_scope(aged)
    assert "t3d-qualifying" not in scope
    row = _inventory_row(store, "t3d-qualifying", aged)
    assert row is not None
    assert row.matched_equivalent_count >= 1


def test_coordinator_streamed_progress_keeps_early_equivalent() -> None:
    clock = FakeClock(NOW)
    coordinator = LiveRefreshCoordinator(clock=clock)
    coordinator._clock = clock
    coordinator._universe_generation_id = GENERATION_ID
    coordinator._universe_generation_started_at = NOW
    coordinator._universe_progress_generation_id = GENERATION_ID

    fixture_a = _evaluated_fixture(
        FIXTURE_A,
        when=NOW,
        equivalent=1,
        arb=True,
        opportunity="qualifying",
    )
    coordinator.record_universe_fixture_progress(
        None,
        fixture_a,
        [_decision("mkt-btts", when=NOW).model_copy(
            update={"canonical_event_id": FIXTURE_A, "fixture_canonical_event_id": FIXTURE_A}
        )],
        [_row(arb=True)],
    )

    clock.advance(TWENTY_MINUTES)
    late = _evaluated_fixture(FIXTURE_B, when=clock.now, equivalent=0, opportunity="unmatched")
    coordinator.record_universe_fixture_progress(None, late, [], [])

    store = coordinator.fixture_current_state()
    row = _inventory_row(store, FIXTURE_A, clock.now)
    assert row is not None
    assert row.matched_equivalent_count >= 1
    assert row.opportunity_state == "matched"
    assert row.solver_is_arbitrage is False
    assert FIXTURE_A not in store.hot_identity_scope(clock.now)
    assert get_settings().paper_universe_current_state_ttl_seconds == 360


def test_inventory_does_not_fabricate_zero_equivalent_while_generation_proved_it() -> None:
    store = FixtureCurrentStateStore()
    store.open_universe_generation(GENERATION_ID, started_at=NOW)
    _upsert_universe(store, FIXTURE_A, when=NOW, markets=[_row()])
    aged = NOW + timedelta(seconds=SEVENTEEN_MINUTES)
    row = _inventory_row(store, FIXTURE_A, aged)
    assert row is not None
    assert (aged - NOW).total_seconds() > DEFAULT_UNIVERSE_TTL_SECONDS
    assert row.matched_equivalent_count != 0
    assert row.opportunity_state != "unmatched"
    assert row.matchbook_matched is True
