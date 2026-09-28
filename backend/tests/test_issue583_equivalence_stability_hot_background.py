"""Issue #583: UNIVERSE relationship truth must survive HOT/BACKGROUND pricing.

Deterministic current-state fixtures. Not live, historical, or modelled quotes.
PAPER only: solver/paper eligibility stays fail-closed when economics are invalid.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from test_current_market_inventory import (
    NOW,
    _decision,
    _facts,
    _pair,
)
from test_issue308_generation_aware_current_state import (
    FIXTURE_A,
    GENERATION_ID,
    _evaluated_fixture,
    _inventory_row,
)

from sports_hedge.application.collector import CollectionReport
from sports_hedge.application.current_market_inventory import (
    candidate_from_current_slot,
    equivalent_comparison_count,
)
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.fixture_inventory import (
    FixtureMarketInventoryRow,
    InventoryComparisonStatus,
    inventory_is_comparable_opportunity,
)
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.domain.models import VenueName


def _mb_k_row(
    *,
    status: InventoryComparisonStatus = InventoryComparisonStatus.MATCHED_EQUIVALENT,
    reason: str | None = None,
    edge: Decimal | None = Decimal("0.008"),
    quote_age_ms: int | None = 80,
    arb: bool = False,
) -> FixtureMarketInventoryRow:
    family = "both_teams_to_score"
    comparable = status in {
        InventoryComparisonStatus.MATCHED_EQUIVALENT,
        InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT,
    }
    return FixtureMarketInventoryRow(
        display_name="BTTS",
        family=family,
        period="full_time",
        comparison_status=status,
        reason=reason,
        rejection_reasons=[] if reason is None else [reason],
        match_reasons=[],
        entered_solver=comparable and edge is not None,
        solver_model="strict_complete_set" if comparable else None,
        current_net_edge=edge,
        trigger_net_edge=Decimal("0.01"),
        distance_to_trigger_pp=Decimal("0.014") if edge is not None else None,
        solver_is_arbitrage=arb,
        matchbook=_facts(
            VenueName.MATCHBOOK,
            source_market_id="mb-btts",
            family=family,
            quote_age_ms=quote_age_ms,
        ),
        kalshi=_facts(
            VenueName.KALSHI,
            source_market_id="k-btts",
            family=family,
            quote_age_ms=quote_age_ms,
        ),
        pair_results=[_pair(VenueName.MATCHBOOK, VenueName.KALSHI, edge=edge, arb=arb)],
    )


def _seed_universe(store: FixtureCurrentStateStore, *, when=NOW) -> None:
    store.open_universe_generation(GENERATION_ID, started_at=when)
    fixture = _evaluated_fixture(FIXTURE_A, when=when, equivalent=1)
    store.upsert_evaluated_fixture(
        fixture,
        markets=[_mb_k_row()],
        decisions=[
            _decision("mkt-btts", when=when).model_copy(
                update={
                    "canonical_event_id": FIXTURE_A,
                    "fixture_canonical_event_id": FIXTURE_A,
                }
            )
        ],
        aliases={FIXTURE_A: FIXTURE_A, f"mb-{FIXTURE_A}": FIXTURE_A},
        scan_lane=ScanLane.UNIVERSE,
        now=when,
        universe_generation_id=GENERATION_ID,
    )


def _pricing_upsert(
    store: FixtureCurrentStateStore,
    *,
    when,
    markets: list[FixtureMarketInventoryRow],
    lane: ScanLane,
    equivalent: int = 0,
    opportunity: str = "matched",
) -> None:
    fixture = _evaluated_fixture(
        FIXTURE_A,
        when=when,
        equivalent=equivalent,
        opportunity=opportunity,
    )
    store.upsert_from_report(
        CollectionReport(
            started_at=when,
            completed_at=when,
            paper_decisions=[
                _decision("mkt-btts", when=when).model_copy(
                    update={
                        "canonical_event_id": FIXTURE_A,
                        "fixture_canonical_event_id": FIXTURE_A,
                    }
                )
            ],
            discovered_fixtures=[fixture],
            fixture_markets={FIXTURE_A: markets},
            fixture_identity_aliases={
                FIXTURE_A: FIXTURE_A,
                f"mb-{FIXTURE_A}": FIXTURE_A,
            },
            scan_lane=lane.value,
            scan_diagnostics={"price_engine": True, "priority": lane.value},
        ),
        scan_lane=lane,
        now=when,
        pricing_refresh=True,
    )


def _assert_equivalent_one(store: FixtureCurrentStateStore, when) -> None:
    row = _inventory_row(store, FIXTURE_A, when)
    assert row is not None
    assert row.matched_equivalent_count == 1
    slots = list((store._rows[FIXTURE_A].markets or {}).values())
    assert equivalent_comparison_count(
        [slot.row for slot in slots if not slot.evaluated_absent]
    ) == 1
    assert all(
        inventory_is_comparable_opportunity(slot.row.comparison_status)
        for slot in slots
        if not slot.evaluated_absent
    )
    proving = {slot.universe_generation_id for slot in slots}
    assert proving == {GENERATION_ID}


def test_issue583_hot_background_reprice_keeps_universe_equivalent() -> None:
    store = FixtureCurrentStateStore()
    _seed_universe(store)
    _assert_equivalent_one(store, NOW)
    universe_obs = store._rows[FIXTURE_A].universe
    assert universe_obs is not None
    assert universe_obs.universe_generation_id == GENERATION_ID

    hot_neg = NOW + timedelta(seconds=10)
    _pricing_upsert(
        store,
        when=hot_neg,
        markets=[_mb_k_row(edge=Decimal("-0.0202"))],
        lane=ScanLane.HOT,
        equivalent=1,
    )
    _assert_equivalent_one(store, hot_neg)
    negative = _inventory_row(store, FIXTURE_A, hot_neg)
    assert negative.current_net_edge == Decimal("-0.0202")
    assert negative.solver_is_arbitrage is False

    hot_fx = NOW + timedelta(seconds=20)
    _pricing_upsert(
        store,
        when=hot_fx,
        markets=[
            _mb_k_row(
                status=InventoryComparisonStatus.MISSING_FX,
                reason="missing_fx_rate:USD",
                edge=None,
            )
        ],
        lane=ScanLane.HOT,
    )
    _assert_equivalent_one(store, hot_fx)
    fx_row = _inventory_row(store, FIXTURE_A, hot_fx)
    assert fx_row.solver_is_arbitrage is False
    slot = next(iter((store._rows[FIXTURE_A].markets or {}).values()))
    assert candidate_from_current_slot(slot, now=hot_fx).eligible_for_paper_simulation is False
    assert "missing_fx_rate:USD" in slot.row.rejection_reasons

    hot_cost = NOW + timedelta(seconds=30)
    _pricing_upsert(
        store,
        when=hot_cost,
        markets=[
            _mb_k_row(
                status=InventoryComparisonStatus.MISSING_COSTS,
                reason="missing_venue_cost:KALSHI",
                edge=None,
            )
        ],
        lane=ScanLane.HOT,
    )
    _assert_equivalent_one(store, hot_cost)
    cost_slot = next(iter((store._rows[FIXTURE_A].markets or {}).values()))
    assert candidate_from_current_slot(cost_slot, now=hot_cost).eligible_for_paper_simulation is False

    hot_stale = NOW + timedelta(seconds=40)
    _pricing_upsert(
        store,
        when=hot_stale,
        markets=[
            _mb_k_row(
                status=InventoryComparisonStatus.STALE,
                reason="unknown_quote_age",
                edge=None,
                quote_age_ms=None,
            )
        ],
        lane=ScanLane.HOT,
    )
    _assert_equivalent_one(store, hot_stale)
    stale_slot = next(iter((store._rows[FIXTURE_A].markets or {}).values()))
    assert candidate_from_current_slot(stale_slot, now=hot_stale).eligible_for_paper_simulation is False

    background_at = NOW + timedelta(seconds=50)
    _pricing_upsert(
        store,
        when=background_at,
        markets=[_mb_k_row(edge=Decimal("-0.011"))],
        lane=ScanLane.UNIVERSE,
        equivalent=1,
    )
    _assert_equivalent_one(store, background_at)
    after_background = store._rows[FIXTURE_A].universe
    assert after_background is not None
    assert after_background.universe_generation_id == GENERATION_ID
    assert after_background.pricing_refresh is False
    assert after_background.last_scanned_at == NOW

    healthy_hot = NOW + timedelta(seconds=60)
    _pricing_upsert(
        store,
        when=healthy_hot,
        markets=[_mb_k_row(edge=Decimal("-0.004"))],
        lane=ScanLane.HOT,
        equivalent=1,
    )
    _assert_equivalent_one(store, healthy_hot)
    healthy = _inventory_row(store, FIXTURE_A, healthy_hot)
    assert healthy.current_net_edge == Decimal("-0.004")
    assert healthy.solver_is_arbitrage is False

    gone_at = NOW + timedelta(seconds=70)
    gone = _evaluated_fixture(FIXTURE_A, when=gone_at, equivalent=0, opportunity="unmatched")
    store.upsert_evaluated_fixture(
        gone,
        markets=[],
        decisions=[],
        aliases={FIXTURE_A: FIXTURE_A, f"mb-{FIXTURE_A}": FIXTURE_A},
        scan_lane=ScanLane.UNIVERSE,
        now=gone_at,
        universe_generation_id=GENERATION_ID,
    )
    gone_row = _inventory_row(store, FIXTURE_A, gone_at)
    assert gone_row is not None
    assert gone_row.matched_equivalent_count == 0
    assert gone_row.opportunity_state == "unmatched"


def test_issue583_background_pricing_does_not_stamp_open_generation() -> None:
    store = FixtureCurrentStateStore()
    _seed_universe(store)
    later_open = GENERATION_ID + 1
    store.close_universe_generation(GENERATION_ID, closed_at=NOW + timedelta(seconds=5))
    store.open_universe_generation(later_open, started_at=NOW + timedelta(seconds=8))
    _pricing_upsert(
        store,
        when=NOW + timedelta(seconds=12),
        markets=[_mb_k_row(edge=Decimal("-0.01"))],
        lane=ScanLane.UNIVERSE,
        equivalent=1,
    )
    slots = list((store._rows[FIXTURE_A].markets or {}).values())
    assert {slot.universe_generation_id for slot in slots} == {GENERATION_ID}
    universe_obs = store._rows[FIXTURE_A].universe
    assert universe_obs is not None
    assert universe_obs.universe_generation_id == GENERATION_ID
