"""Issue #357 / Wave 1A: Min-Net PAPER tolerance + HOT proximity.

`current_net_edge` is post-cost net ROI. `trigger_net_edge` /
`minimum_net_edge` is operator Min Net Arb. HOT proximity uses existing
`distance_to_trigger_pp`. Data class: deterministic fixture/demo paper-scan
payloads, not live venue quotes. Phase 1 remains paper-only.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from test_issue200_universe_hot_promotion import (
    CANONICAL_ID,
    NOW,
    _fixture,
    _market_row,
    _qualifying_universe_report,
    _report,
)
from test_near_arbitrage_watchlist import TRIGGER, _observation
from test_paper_entry_snapshot_freshness import T1, _freshness_bundle, _observe, _qualify
from test_step8f_automatic_paper_entry import (
    FX,
    _kalshi_btts,
    _kalshi_costs,
    _matchbook_btts,
    _ops_bundle,
    _standing,
)
from venue_cost_helpers import profit_commission_cost

from sports_hedge.accounting.paper_journal import DataProvenance
from sports_hedge.application.current_market_inventory import (
    stored_row_proves_net_proximity,
    stored_row_proves_qualifying_executable,
    stored_row_proves_surveillance_opportunity,
)
from sports_hedge.application.executable_liquidity import decision_net_edge
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.paper_operations import PaperOperationsError
from sports_hedge.application.price_engine import _decision_is_interesting
from sports_hedge.application.scan_lanes import ScanLane, classify_scan_lane
from sports_hedge.arbitrage.models import PayoffSolution
from sports_hedge.arbitrage.payoff_scan import PayoffScanResult
from sports_hedge.arbitrage.watchlist.economics import (
    MOVED_BELOW_MIN_NET_ARB,
    NET_PROXIMITY_BAND_PP,
    arrival_net_edge_after_venue_costs,
    classify_status,
    complete_set_roi_from_decimal_odds,
    distance_to_trigger_pp,
    evaluate_post_trigger_min_net_arb,
    is_net_proximity_hot,
    net_proximity_reason_label,
    qualifies_min_net_arb,
)
from sports_hedge.arbitrage.watchlist.models import OpportunityStatus, PaperFillAttemptStatus
from sports_hedge.domain.models import VenueName
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.entry_freshness import (
    SNAPSHOT_STALE_AT_DECISION,
    SNAPSHOT_STALE_AT_SIMULATED_ARRIVAL,
)
from sports_hedge.paper.fills import PaperFillConfig
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.paper.trades import PaperTradeState
from sports_hedge.risk.execution import ExecutionRiskInputs, ExecutionRiskResult

EDGE_120 = Decimal("0.012")
EDGE_105 = Decimal("0.0105")
EDGE_140 = Decimal("0.014")
EDGE_099 = Decimal("0.0099")
EDGE_080 = Decimal("0.008")
EDGE_049 = Decimal("0.0049")
ARRIVAL_GROSS_ODDS = Decimal("2.04")
HIGH_COMMISSION = Decimal("0.05")
LOW_COMMISSION = Decimal("0.01")


def _near_decision(*, roi: Decimal, trigger: Decimal = TRIGGER) -> PaperScanDecision:
    return PaperScanDecision(
        scanned_at=NOW,
        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=["register"]),
        payoff_scan=PayoffScanResult(
            solution=PayoffSolution(
                is_arbitrage=roi > 0,
                roi=roi,
                minimum_state_pnl=Decimal("0"),
                numerically_validated=True,
            )
        ),
        minimum_net_edge=trigger,
        solver_model="strict_complete_set",
        eligible_for_paper_simulation=False,
    )


def test_distance_contract_is_current_net_versus_trigger_net() -> None:
    assert NET_PROXIMITY_BAND_PP == Decimal("0.50")
    assert qualifies_min_net_arb(EDGE_120, TRIGGER) is True
    assert distance_to_trigger_pp(EDGE_080, TRIGGER) == Decimal("0.2000")
    assert is_net_proximity_hot(EDGE_080, TRIGGER) is True
    assert distance_to_trigger_pp(EDGE_049, TRIGGER) == Decimal("0.5100")
    assert is_net_proximity_hot(EDGE_049, TRIGGER) is False
    assert net_proximity_reason_label(Decimal("0.05")) == "NET PROXIMITY · 0.05pp TO TRIGGER"
    assert net_proximity_reason_label(Decimal("0.20")) == "NET PROXIMITY · 0.20pp TO TRIGGER"


def test_current_net_120_trigger_100_qualifies() -> None:
    status, _reasons = classify_status(
        _observation(edge=EDGE_120, eligible=True, quote_age_ms=80),
        approaching_band_pp=NET_PROXIMITY_BAND_PP,
        max_quote_age_ms=2000,
    )
    assert status is OpportunityStatus.TRIGGERED
    assert _decision_is_interesting(_near_decision(roi=EDGE_120)) is True
    row = _market_row(edge=EDGE_120, arb=True, trigger=TRIGGER)
    assert stored_row_proves_qualifying_executable(row) is True
    assert stored_row_proves_net_proximity(row) is False


def test_current_net_080_trigger_100_is_hot_proximity_without_fill() -> None:
    status, _reasons = classify_status(
        _observation(edge=EDGE_080, eligible=False, rejection_reasons=["net_edge_below_threshold"]),
        approaching_band_pp=NET_PROXIMITY_BAND_PP,
        max_quote_age_ms=2000,
    )
    assert status is OpportunityStatus.APPROACHING
    assert status is not OpportunityStatus.TRIGGERED
    assert is_net_proximity_hot(EDGE_080, TRIGGER) is True
    row = _market_row(edge=EDGE_080, arb=False, trigger=TRIGGER)
    assert stored_row_proves_qualifying_executable(row) is False
    assert stored_row_proves_net_proximity(row) is True
    assert stored_row_proves_surveillance_opportunity(row) is True
    assert _decision_is_interesting(_near_decision(roi=EDGE_080)) is True

    store = FixtureCurrentStateStore()
    fixture = _fixture(opportunity="near", arb=False, qualifying=0)
    store.upsert_from_report(
        _report([fixture], markets={CANONICAL_ID: [row]}),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    assert classify_scan_lane(fixture, NOW) is ScanLane.UNIVERSE
    assert CANONICAL_ID in store.hot_identity_scope(NOW)
    shown = next(item for item in store.inventory(NOW) if item.canonical_event_id == CANONICAL_ID)
    assert shown.hot_reasons == [net_proximity_reason_label(Decimal("0.20"))]


def test_current_net_049_trigger_100_is_not_economically_hot() -> None:
    status, _reasons = classify_status(
        _observation(edge=EDGE_049, eligible=False, rejection_reasons=["net_edge_below_threshold"]),
        approaching_band_pp=NET_PROXIMITY_BAND_PP,
        max_quote_age_ms=2000,
    )
    assert status is OpportunityStatus.WATCHING
    row = _market_row(edge=EDGE_049, arb=False, trigger=TRIGGER)
    assert stored_row_proves_net_proximity(row) is False
    assert stored_row_proves_surveillance_opportunity(row) is False
    assert stored_row_proves_qualifying_executable(row) is False
    assert _decision_is_interesting(_near_decision(roi=EDGE_049)) is False

    store = FixtureCurrentStateStore()
    fixture = _fixture(opportunity="watch", arb=False, qualifying=0)
    store.upsert_from_report(
        _report([fixture], markets={CANONICAL_ID: [row]}),
        scan_lane=ScanLane.UNIVERSE,
        now=NOW,
    )
    assert CANONICAL_ID not in store.hot_identity_scope(NOW)


def test_post_trigger_arrival_tolerance_unit_contract() -> None:
    assert (
        evaluate_post_trigger_min_net_arb(
            bound_net_edge=EDGE_120,
            arrival_net_edge=EDGE_105,
            trigger_net_edge=TRIGGER,
        )
        is None
    )
    assert (
        evaluate_post_trigger_min_net_arb(
            bound_net_edge=EDGE_120,
            arrival_net_edge=EDGE_140,
            trigger_net_edge=TRIGGER,
        )
        is None
    )
    assert (
        evaluate_post_trigger_min_net_arb(
            bound_net_edge=EDGE_120,
            arrival_net_edge=EDGE_099,
            trigger_net_edge=TRIGGER,
        )
        == MOVED_BELOW_MIN_NET_ARB
    )


def _bind_qualified(tmp_path: Path, *, age_ms: int = 80):
    scan, watchlist, ops, repository, ledger, _settings = _freshness_bundle(
        tmp_path, autofill=False
    )
    decision = _qualify(scan, age_ms=age_ms)
    bound = decision_net_edge(decision)
    assert bound is not None and bound >= decision.minimum_net_edge
    seeded = _observe(scan, watchlist, decision)
    assert seeded.status is OpportunityStatus.TRIGGERED
    ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER, autofill=False)
    watchlist.begin_paper_fill_attempt(
        seeded.opportunity_id,
        occurred_at=T1,
        bind_snapshot=True,
        decision_at=decision.scanned_at,
    )
    plan = ops._plans[seeded.opportunity_id]
    plan = plan.model_copy(
        update={
            "net_edge": EDGE_120,
            "decision": plan.decision.model_copy(update={"minimum_net_edge": TRIGGER}),
        }
    )
    ops._plans[seeded.opportunity_id] = plan
    return scan, watchlist, ops, repository, ledger, seeded, plan


def test_bound_120_arrival_105_fills(tmp_path: Path) -> None:
    _scan, watchlist, ops, repository, ledger, seeded, _plan = _bind_qualified(tmp_path)
    try:
        result = ops.simulate_fill(
            seeded.opportunity_id,
            simulate_external=True,
            provenance=DataProvenance.LIVE_PAPER,
            now=T1,
            config=PaperFillConfig(
                assumed_latency_ms=0,
                max_quote_age_ms=2000,
                modeled_arrival_net_edge=EDGE_105,
            ),
        )
        assert result.entry_complete is True
        assert ops.list_active_trades()[0].state is PaperTradeState.OPEN
        assert seeded.opportunity_id not in ops._entry_rejections
        filled = watchlist.repository.get(seeded.opportunity_id)
        assert filled is not None
        assert filled.status is OpportunityStatus.FILLED
    finally:
        repository.close()
        ledger.close()


def test_bound_120_arrival_140_fills(tmp_path: Path) -> None:
    _scan, _watchlist, ops, repository, ledger, seeded, _plan = _bind_qualified(tmp_path)
    try:
        ops.simulate_fill(
            seeded.opportunity_id,
            simulate_external=True,
            provenance=DataProvenance.LIVE_PAPER,
            now=T1,
            config=PaperFillConfig(
                assumed_latency_ms=0,
                max_quote_age_ms=2000,
                modeled_arrival_net_edge=EDGE_140,
            ),
        )
        assert ops.list_active_trades()[0].state is PaperTradeState.OPEN
    finally:
        repository.close()
        ledger.close()


def test_bound_120_arrival_099_blocks_with_moved_below_min_net_arb(tmp_path: Path) -> None:
    _scan, watchlist, ops, repository, ledger, seeded, _plan = _bind_qualified(tmp_path)
    try:
        with pytest.raises(PaperOperationsError, match=MOVED_BELOW_MIN_NET_ARB):
            ops.simulate_fill(
                seeded.opportunity_id,
                simulate_external=True,
                provenance=DataProvenance.LIVE_PAPER,
                now=T1,
                config=PaperFillConfig(
                    assumed_latency_ms=0,
                    max_quote_age_ms=2000,
                    modeled_arrival_net_edge=EDGE_099,
                ),
            )
        assert ops.list_active_trades() == []
        assert ops._entry_rejections[seeded.opportunity_id] == MOVED_BELOW_MIN_NET_ARB
        row = watchlist.repository.get(seeded.opportunity_id)
        assert row is not None
        assert MOVED_BELOW_MIN_NET_ARB in row.rejection_reasons
        assert SNAPSHOT_STALE_AT_DECISION not in row.rejection_reasons
        assert SNAPSHOT_STALE_AT_SIMULATED_ARRIVAL not in row.rejection_reasons
    finally:
        repository.close()
        ledger.close()


def test_post_trigger_does_not_reject_for_stale_snapshot_or_watchlist(
    tmp_path: Path,
) -> None:
    _scan, watchlist, ops, repository, ledger, seeded, plan = _bind_qualified(tmp_path, age_ms=80)
    try:
        stale_legs = [
            leg.model_copy(update={"quote_age_ms": 50_000}) for leg in plan.legs
        ]
        ops._plans[seeded.opportunity_id] = plan.model_copy(
            update={
                "legs": stale_legs,
                "quote_age_ms": 50_000,
                "quote_age_at_decision_ms": 50_000,
            }
        )
        current = watchlist.repository.get(seeded.opportunity_id)
        assert current is not None
        watchlist.repository.upsert_opportunity(
            current.model_copy(
                update={
                    "status": OpportunityStatus.REJECTED,
                    "rejection_reasons": ["stale_quote"],
                }
            ),
            force_status=True,
        )
        ops.simulate_fill(
            seeded.opportunity_id,
            simulate_external=True,
            provenance=DataProvenance.LIVE_PAPER,
            now=T1 + timedelta(seconds=5),
            config=PaperFillConfig(
                assumed_latency_ms=500,
                max_quote_age_ms=1000,
                modeled_arrival_net_edge=EDGE_105,
            ),
        )
        assert ops.list_active_trades()[0].state is PaperTradeState.OPEN
        assert SNAPSHOT_STALE_AT_DECISION not in ops._entry_rejections.values()
        assert SNAPSHOT_STALE_AT_SIMULATED_ARRIVAL not in ops._entry_rejections.values()
    finally:
        repository.close()
        ledger.close()


def test_post_trigger_does_not_use_execution_risk_as_veto(tmp_path: Path) -> None:
    _scan, _watchlist, ops, repository, ledger, seeded, plan = _bind_qualified(tmp_path)
    try:
        risk = ExecutionRiskResult(
            score=99,
            band="extreme",
            reasons=["execution_risk_above_threshold"],
            inputs=ExecutionRiskInputs(
                spread_bps=80,
                size_to_depth_ratio=1.2,
                quote_age_ms=80,
                recent_volatility_bps=90,
                leg_count=2,
                assumed_latency_ms=500,
            ),
        )
        ops._plans[seeded.opportunity_id] = plan.model_copy(
            update={
                "execution_risk_score": 99,
                "decision": plan.decision.model_copy(
                    update={
                        "execution_risk": risk,
                        "maximum_execution_risk": 10,
                        "rejection_reasons": ["execution_risk_above_threshold"],
                    }
                ),
            }
        )
        ops.simulate_fill(
            seeded.opportunity_id,
            simulate_external=True,
            provenance=DataProvenance.LIVE_PAPER,
            now=T1,
            config=PaperFillConfig(
                assumed_latency_ms=0,
                max_quote_age_ms=2000,
                modeled_arrival_net_edge=EDGE_105,
            ),
        )
        assert ops.list_active_trades()[0].state is PaperTradeState.OPEN
        assert "execution_risk_above_threshold" not in ops._entry_rejections.values()
    finally:
        repository.close()
        ledger.close()


def test_proximity_candidate_does_not_paper_fill(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        decision = scan.scan_pair(
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
            fx_snapshots=FX,
            maximum_execution_risk=100,
            liquidity_snapshot=_standing(),
            minimum_net_edge=Decimal("0.50"),
        )
        assert "net_edge_below_threshold" in decision.rejection_reasons or (
            decision_net_edge(decision) is not None
            and decision_net_edge(decision) < Decimal("0.50")
        )
        observed = watchlist.observe_paper_decision(
            decision,
            scan.market_intelligence.market_history(
                canonical_market_id=decision.canonical_market_id
            ),
        )
        ops.persist_triggered_chain(
            decision, provenance=DataProvenance.LIVE_PAPER, autofill=True
        )
        assert observed.status in {
            OpportunityStatus.APPROACHING,
            OpportunityStatus.WATCHING,
            OpportunityStatus.REJECTED,
        }
        assert observed.status is not OpportunityStatus.TRIGGERED
        assert ops.list_active_trades() == []
    finally:
        repository.close()
        ledger.close()


def test_duplicate_observation_opens_exactly_once(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, _settings = _freshness_bundle(tmp_path, autofill=True)
    try:
        decision = _qualify(scan, age_ms=80)
        first = _observe(scan, watchlist, decision)
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER, autofill=True)
        second = _observe(scan, watchlist, decision)
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER, autofill=True)
        trades = ops.list_active_trades()
        assert len(trades) == 1
        assert trades[0].state is PaperTradeState.OPEN
        assert first.opportunity_id == second.opportunity_id
        attempts = watchlist.repository.list_paper_fill_attempts(first.opportunity_id)
        complete = [item for item in attempts if item.status is PaperFillAttemptStatus.COMPLETE]
        assert len(complete) == 1
    finally:
        repository.close()
        ledger.close()


def test_qualifying_executable_still_uses_existing_architecture() -> None:
    store = FixtureCurrentStateStore()
    store.upsert_from_report(
        _qualifying_universe_report(), scan_lane=ScanLane.UNIVERSE, now=NOW
    )
    promoted = next(
        item for item in store.inventory(NOW) if item.canonical_event_id == CANONICAL_ID
    )
    assert classify_scan_lane(promoted, NOW) is ScanLane.UNIVERSE
    assert promoted.scan_lane == ScanLane.HOT.value
    assert "ARB PROMOTION" in (promoted.hot_reasons or [])
    assert not any(
        str(reason).startswith("NET PROXIMITY") for reason in (promoted.hot_reasons or [])
    )


def _commission_costs(rate: Decimal) -> list:
    return [
        profit_commission_cost(VenueName.MATCHBOOK, rate, captured_at=T1),
        profit_commission_cost(VenueName.KALSHI, rate, captured_at=T1),
    ]


def _arrival_legs(odds: Decimal = ARRIVAL_GROSS_ODDS):
    return (
        (VenueName.MATCHBOOK, "mb-yes", odds),
        (VenueName.KALSHI, "ks-no", odds),
    )


def test_odds_only_roi_is_not_post_cost_arrival_net() -> None:
    gross = complete_set_roi_from_decimal_odds(
        [ARRIVAL_GROSS_ODDS, ARRIVAL_GROSS_ODDS]
    )
    assert gross is not None
    assert gross > TRIGGER
    high_net = arrival_net_edge_after_venue_costs(
        arrival_legs=_arrival_legs(),
        venue_costs=_commission_costs(HIGH_COMMISSION),
        as_of=T1,
    )
    low_net = arrival_net_edge_after_venue_costs(
        arrival_legs=_arrival_legs(),
        venue_costs=_commission_costs(LOW_COMMISSION),
        as_of=T1,
    )
    assert high_net is not None and high_net < TRIGGER
    assert low_net is not None and low_net >= TRIGGER
    assert arrival_net_edge_after_venue_costs(
        arrival_legs=_arrival_legs(),
        venue_costs=[],
        as_of=T1,
    ) is None


def _bind_with_arrival_commission(tmp_path: Path, *, commission: Decimal):
    _scan, watchlist, ops, repository, ledger, seeded, plan = _bind_qualified(tmp_path)
    costs = _commission_costs(commission)
    legs = [
        leg.model_copy(
            update={
                "displayed_odds": ARRIVAL_GROSS_ODDS,
                "levels": [
                    BookLevel(
                        decimal_odds=ARRIVAL_GROSS_ODDS,
                        available_stake=Decimal("1000"),
                    )
                ],
            }
        )
        for leg in plan.legs
    ]
    ops._plans[seeded.opportunity_id] = plan.model_copy(
        update={"legs": legs, "venue_costs": costs}
    )
    return watchlist, ops, repository, ledger, seeded


def test_arrival_gross_above_trigger_but_fees_push_net_below_blocks(
    tmp_path: Path,
) -> None:
    gross = complete_set_roi_from_decimal_odds(
        [ARRIVAL_GROSS_ODDS, ARRIVAL_GROSS_ODDS]
    )
    assert gross is not None and gross > TRIGGER
    watchlist, ops, repository, ledger, seeded = _bind_with_arrival_commission(
        tmp_path, commission=HIGH_COMMISSION
    )
    try:
        with pytest.raises(PaperOperationsError, match=MOVED_BELOW_MIN_NET_ARB):
            ops.simulate_fill(
                seeded.opportunity_id,
                simulate_external=True,
                provenance=DataProvenance.LIVE_PAPER,
                now=T1,
                config=PaperFillConfig(assumed_latency_ms=0, slippage_bps=Decimal("0")),
            )
        assert ops.list_active_trades() == []
        assert ops._entry_rejections[seeded.opportunity_id] == MOVED_BELOW_MIN_NET_ARB
        row = watchlist.repository.get(seeded.opportunity_id)
        assert row is not None
        assert MOVED_BELOW_MIN_NET_ARB in row.rejection_reasons
    finally:
        repository.close()
        ledger.close()


def test_arrival_gross_above_trigger_and_post_cost_net_still_fills(
    tmp_path: Path,
) -> None:
    watchlist, ops, repository, ledger, seeded = _bind_with_arrival_commission(
        tmp_path, commission=LOW_COMMISSION
    )
    try:
        ops.simulate_fill(
            seeded.opportunity_id,
            simulate_external=True,
            provenance=DataProvenance.LIVE_PAPER,
            now=T1,
            config=PaperFillConfig(assumed_latency_ms=0, slippage_bps=Decimal("0")),
        )
        assert ops.list_active_trades()[0].state is PaperTradeState.OPEN
        assert seeded.opportunity_id not in ops._entry_rejections
        filled = watchlist.repository.get(seeded.opportunity_id)
        assert filled is not None
        assert filled.status is OpportunityStatus.FILLED
    finally:
        repository.close()
        ledger.close()
