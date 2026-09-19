from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.paper import get_paper_ledger, get_paper_operations_service
from sports_hedge.application.complete_set import SOLVER_MODEL_GENERALIZED, SOLVER_MODEL_SIMPLE
from sports_hedge.application.market_observation import MatchbookObservationBuilder
from sports_hedge.accounting.dimensions import CapitalSource
from sports_hedge.accounting.paper_journal import DataProvenance
from sports_hedge.application.paper_operations import PaperOperationsError
from sports_hedge.arbitrage.allocation.engine import allocate, allocate_requested_size
from sports_hedge.arbitrage.allocation.models import AllocationConstraintKind
from sports_hedge.arbitrage.priority_alerts.models import LegExecutionMode
from sports_hedge.arbitrage.watchlist.service import _opportunity_id
from sports_hedge.domain.football import (
    CanonicalEvent,
    CanonicalMarket,
    CanonicalOutcome,
    CanonicalRunner,
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.paper.trades import PaperTradeState
from test_paper_scan_pipeline import KICKOFF
from test_step7_safe_market_expansion import MB_EVENT
from test_step8b_first_team_to_score import _ftts_mb_payload
from test_step8c_bankroll_allocator import POLICY, _balances, _demo_request
from test_step8f_automatic_paper_entry import (
    OBSERVED,
    _matchbook_btts,
    _observe_and_persist,
    _ops_bundle,
    _kalshi_btts,
    _kalshi_costs,
    _kalshi_ftts_observation,
)

TEN = Decimal("10")


def test_allocate_requested_ten_pounds_preserves_solver_ratios() -> None:
    request = _demo_request(policy=POLICY)
    baseline = allocate(request)
    assert baseline.accepted
    assert baseline.maximum_validated_capital >= TEN
    prepared = allocate_requested_size(request, TEN)
    assert prepared.accepted is True
    assert prepared.places_orders is False
    assert prepared.paper_only is True
    assert prepared.recommended_committed_capital == TEN
    assert prepared.recommended_size == TEN
    scale = TEN / request.committed_capital_at_solver_size
    assert prepared.scale_recommended == scale
    reporting = sum((stake.capital_reporting for stake in prepared.recommended_stakes), Decimal("0"))
    assert reporting == TEN
    by_leg = {stake.outcome: stake for stake in prepared.recommended_stakes}
    for leg in request.legs:
        stake = by_leg[leg.outcome]
        assert stake.stake_reporting == leg.solver_stake * scale
        assert stake.capital_native == stake.capital_reporting / leg.gbp_per_unit
        if leg.venue is VenueName.MATCHBOOK:
            assert stake.native_currency == "GBP"
        if leg.venue is VenueName.POLYMARKET:
            assert stake.native_currency == "USD"
            assert stake.capital_source.value == "MANUAL_EXTERNAL"


def test_thin_native_pool_cannot_borrow_from_other_venue_currency() -> None:
    request = _demo_request(
        policy=POLICY,
        balances=_balances(matchbook=Decimal("1"), polymarket=Decimal("100000")),
    )
    prepared = allocate_requested_size(request, TEN)
    assert prepared.accepted is False
    assert prepared.rejection_reason == "requested_size_exceeds_validated_maximum"
    assert prepared.limiting_constraint in {
        AllocationConstraintKind.NATIVE_VENUE_BALANCE,
        AllocationConstraintKind.MIN_FREE_RESERVE,
        AllocationConstraintKind.MAX_POOL_FRACTION,
    }
    assert prepared.recommended_stakes == []
    assert prepared.capital_required == []
    # Hard constraints remain keyed by native venue/currency; USD surplus does not fund GBP.
    native_constraints = [
        item
        for item in prepared.hard_constraints
        if item.kind
        in {
            AllocationConstraintKind.NATIVE_VENUE_BALANCE,
            AllocationConstraintKind.MIN_FREE_RESERVE,
            AllocationConstraintKind.MAX_POOL_FRACTION,
        }
        and item.venue is VenueName.MATCHBOOK
    ]
    assert native_constraints
    assert all(item.currency == "GBP" for item in native_constraints)


def test_requested_size_above_allocator_maximum_is_rejected_not_silently_resized() -> None:
    request = _demo_request(policy=POLICY)
    baseline = allocate(request)
    huge = baseline.maximum_validated_capital + Decimal("1")
    prepared = allocate_requested_size(request, huge)
    assert prepared.accepted is False
    assert prepared.rejection_reason == "requested_size_exceeds_validated_maximum"
    assert prepared.maximum_validated_capital == baseline.maximum_validated_capital
    assert prepared.recommended_committed_capital == 0


def test_settlement_equivalence_is_rules_and_outcome_space_not_labels() -> None:
    kickoff = KICKOFF

    def event(venue: VenueName) -> CanonicalEvent:
        return CanonicalEvent(
            competition="Premier League",
            home_team="Newcastle United",
            away_team="Chelsea",
            kickoff_utc=kickoff,
            source_venue=venue,
            source_event_id=venue.value,
        )

    complete = SettlementFingerprint(
        scope=SettlementScope.REGULATION_TIME,
        period=FootballPeriod.FULL_TIME,
        extra_time_included=False,
        penalties_included=False,
        push_possible=False,
    )
    left = CanonicalMarket(
        event=event(VenueName.MATCHBOOK),
        source_venue=VenueName.MATCHBOOK,
        source_market_id="mb-1x2",
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        settlement=complete,
        runners=[
            CanonicalRunner(source_runner_id="h", outcome=CanonicalOutcome.HOME, label="Newcastle"),
            CanonicalRunner(source_runner_id="d", outcome=CanonicalOutcome.DRAW, label="Draw"),
            CanonicalRunner(source_runner_id="a", outcome=CanonicalOutcome.AWAY, label="Chelsea"),
        ],
    )
    incomplete_space = CanonicalMarket(
        event=event(VenueName.POLYMARKET),
        source_venue=VenueName.POLYMARKET,
        source_market_id="pm-binary-named-match-result",
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        settlement=complete,
        runners=[
            CanonicalRunner(source_runner_id="h", outcome=CanonicalOutcome.HOME, label="Newcastle"),
            CanonicalRunner(source_runner_id="a", outcome=CanonicalOutcome.AWAY, label="Chelsea"),
        ],
    )
    mismatch = MarketMatcher().match(left, incomplete_space)
    assert mismatch.matched is False
    assert "outcome_space_mismatch" in mismatch.reasons


def _persist_qualified(tmp_path: Path, *, left, right):
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=False)
    decision = _observe_and_persist(
        scan,
        watchlist,
        ops,
        left,
        right,
        venue_costs=_kalshi_costs(),
    )
    return scan, watchlist, ops, repository, ledger, decision


def test_prepare_ten_pound_btts_shows_exact_legs_without_opening(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, decision = _persist_qualified(
        tmp_path, left=_matchbook_btts(), right=_kalshi_btts()
    )
    try:
        assert decision.solver_model == SOLVER_MODEL_SIMPLE
        assert decision.eligible_for_paper_simulation is True
        opportunity_id = _opportunity_id(decision.canonical_market_id)
        before = ledger.treasury.snapshot()
        preview = ops.prepare_fixed_deployment(opportunity_id, TEN)
        assert preview.accepted is True
        assert preview.opens_trade is False
        assert preview.locks_treasury is False
        assert preview.places_orders is False
        assert preview.paper_only is True
        assert preview.settlement_equivalent is True
        assert preview.requested_size_gbp == TEN
        assert preview.applied_size_gbp == TEN
        assert preview.native_requirements_reconciled is True
        reporting = sum((leg.capital_reporting for leg in preview.legs), Decimal("0"))
        assert reporting == TEN
        venues = {leg.venue for leg in preview.legs}
        assert VenueName.MATCHBOOK in venues
        assert VenueName.KALSHI in venues
        for leg in preview.legs:
            assert leg.displayed_odds is not None and leg.displayed_odds > 1
            assert leg.stake_native > 0
            assert leg.cost_status == "modelled"
            assert leg.venue_fee is not None
            if leg.venue is VenueName.MATCHBOOK:
                assert leg.native_currency == "GBP"
                assert leg.capital_native == leg.capital_reporting
            if leg.venue is VenueName.KALSHI:
                assert leg.native_currency == "USD"
                assert leg.capital_native == leg.capital_reporting / Decimal("0.75")
                assert leg.execution_mode == LegExecutionMode.INTERNAL.value
                assert leg.capital_source is CapitalSource.AUTO_POOL
            else:
                assert leg.execution_mode == LegExecutionMode.INTERNAL.value
                assert leg.capital_source is CapitalSource.AUTO_POOL
        for item in preview.capital_required:
            if item.venue is VenueName.KALSHI:
                assert item.capital_source is CapitalSource.AUTO_POOL
            else:
                assert item.capital_source is CapitalSource.AUTO_POOL
        native_required = {(item.venue, item.currency): item.amount for item in preview.capital_required}
        after = ledger.treasury.snapshot()
        for (venue, currency), amount in native_required.items():
            pool = after.pool(venue, currency)
            assert pool.available_cash >= amount
            assert pool.available_cash == before.pool(venue, currency).available_cash
            assert pool.locked_capital == before.pool(venue, currency).locked_capital
        assert ops.list_active_trades() == []
        assert all(trade.state is not PaperTradeState.OPEN for trade in ops.list_closed_trades())
    finally:
        repository.close()
        ledger.close()


def test_prepare_ten_pound_generalized_ftts_without_opening(tmp_path: Path) -> None:
    matchbook = MatchbookObservationBuilder().build(
        MB_EVENT, _ftts_mb_payload(), observed_at=OBSERVED, quote_age_ms=120
    )
    kalshi = _kalshi_ftts_observation()
    scan, watchlist, ops, repository, ledger, decision = _persist_qualified(
        tmp_path, left=matchbook, right=kalshi
    )
    try:
        assert decision.solver_model == SOLVER_MODEL_GENERALIZED
        opportunity_id = _opportunity_id(decision.canonical_market_id)
        before = ledger.treasury.snapshot()
        preview = ops.prepare_fixed_deployment(opportunity_id, TEN)
        assert preview.accepted is True, preview.rejection_reason
        assert preview.applied_size_gbp == pytest.approx(TEN, abs=Decimal("0.01"))
        assert preview.solver_model == SOLVER_MODEL_GENERALIZED
        assert preview.opens_trade is False
        assert sum((leg.capital_reporting for leg in preview.legs), Decimal("0")) == pytest.approx(
            TEN, abs=Decimal("0.01")
        )
        assert all(leg.stake_native > 0 for leg in preview.legs)
        after = ledger.treasury.snapshot()
        assert after.pool(VenueName.MATCHBOOK, "GBP").available_cash == before.pool(
            VenueName.MATCHBOOK, "GBP"
        ).available_cash
        assert ops.list_active_trades() == []
    finally:
        repository.close()
        ledger.close()


def test_prepare_rejects_insufficient_capital_before_open(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, decision = _persist_qualified(
        tmp_path, left=_matchbook_btts(), right=_kalshi_btts()
    )
    try:
        opportunity_id = _opportunity_id(decision.canonical_market_id)
        before = ledger.treasury.snapshot()
        preview = ops.prepare_fixed_deployment(opportunity_id, Decimal("1000000"))
        assert preview.accepted is False
        assert preview.rejection_reason == "requested_size_exceeds_validated_maximum"
        assert preview.applied_size_gbp == 0
        assert preview.legs == []
        after = ledger.treasury.snapshot()
        assert after.pool(VenueName.MATCHBOOK, "GBP").available_cash == before.pool(
            VenueName.MATCHBOOK, "GBP"
        ).available_cash
        assert after.pool(VenueName.POLYMARKET, "USD").available_cash == before.pool(
            VenueName.POLYMARKET, "USD"
        ).available_cash
        assert ops.list_active_trades() == []
    finally:
        repository.close()
        ledger.close()


def test_prepare_deployment_api_and_fixture_preparable_list(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, decision = _persist_qualified(
        tmp_path, left=_matchbook_btts(), right=_kalshi_btts()
    )
    try:
        app.dependency_overrides[get_paper_operations_service] = lambda: ops
        app.dependency_overrides[get_paper_ledger] = lambda: ledger
        client = TestClient(app)
        opportunity_id = _opportunity_id(decision.canonical_market_id)
        missing = client.post(
            "/paper/prepare-deployment",
            json={"opportunity_id": "watch:unknown", "requested_size_gbp": "10"},
        )
        assert missing.status_code == 409
        response = client.post(
            "/paper/prepare-deployment",
            json={
                "opportunity_id": opportunity_id,
                "requested_size_gbp": "10",
                "operator_note": "PAPER-ONLY lane 2 prepare",
            },
        )
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["accepted"] is True
        assert body["opens_trade"] is False
        assert body["locks_treasury"] is False
        assert body["places_orders"] is False
        assert Decimal(body["applied_size_gbp"]) == TEN
        assert Decimal(body["requested_size_gbp"]) == TEN
        assert body["native_requirements_reconciled"] is True
        assert len(body["legs"]) >= 2
        for leg in body["legs"]:
            assert leg["venue"]
            assert leg["native_currency"]
            assert leg["outcome"]
            assert Decimal(leg["stake_native"]) > 0
            assert Decimal(leg["displayed_odds"]) > 1
            assert "capital_source" in leg
            assert "execution_mode" in leg
            assert leg["capital_source"] != "MANUAL_EXTERNAL"
            if leg["venue"] == "polymarket":
                assert leg["capital_source"] == "PAPER_SIMULATED_EXTERNAL"
                assert leg["execution_mode"] == "EXTERNAL_OPERATOR"
        preparable = ops.list_preparable(decision.canonical_event_id)
        assert any(item.opportunity_id == opportunity_id for item in preparable)
        active = client.get("/paper/trades/active").json()
        assert active == []
    finally:
        app.dependency_overrides.clear()
        repository.close()
        ledger.close()


def test_confirm_prepared_ten_pounds_locks_preview_legs_not_allocator(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, decision = _persist_qualified(
        tmp_path, left=_matchbook_btts(), right=_kalshi_btts()
    )
    try:
        opportunity_id = _opportunity_id(decision.canonical_market_id)
        plan = ops._plans[opportunity_id]
        allocation = plan.decision.allocation
        assert allocation is not None and allocation.accepted
        assert allocation.recommended_committed_capital > TEN
        allocator_stakes = {
            (stake.venue, stake.outcome): stake.stake_native
            for stake in allocation.recommended_stakes
            if stake.stake_native > 0
        }

        before = ledger.treasury.snapshot()
        preview = ops.prepare_fixed_deployment(opportunity_id, TEN)
        assert preview.accepted is True
        assert preview.prepared_deployment_id
        assert preview.applied_size_gbp == TEN
        preview_stakes = {(leg.venue, leg.outcome): leg.stake_native for leg in preview.legs}
        assert preview_stakes != allocator_stakes
        after_preview = ledger.treasury.snapshot()
        for venue, currency in ((VenueName.MATCHBOOK, "GBP"), (VenueName.KALSHI, "USD")):
            assert after_preview.pool(venue, currency).available_cash == before.pool(
                venue, currency
            ).available_cash
            assert after_preview.pool(venue, currency).locked_capital == before.pool(
                venue, currency
            ).locked_capital
        assert ops.list_active_trades() == []

        result = ops.simulate_fill(
            opportunity_id,
            simulate_external=True,
            prepared_deployment_id=preview.prepared_deployment_id,
            requested_size_gbp=TEN,
            provenance=DataProvenance.FIXTURE_DEMO,
        )
        assert result.entry_complete is True
        assert result.prepared_deployment_id == preview.prepared_deployment_id
        trades = ops.list_active_trades()
        assert len(trades) == 1
        trade = trades[0]
        assert trade.state is PaperTradeState.OPEN
        trade_stakes = {(leg.venue, leg.outcome): leg.filled_stake for leg in trade.legs}
        assert trade_stakes == preview_stakes
        assert trade_stakes != allocator_stakes
        after = ledger.treasury.snapshot()
        locked_by_currency: dict[str, Decimal] = {}
        for leg in preview.legs:
            locked_by_currency[leg.native_currency] = (
                locked_by_currency.get(leg.native_currency, Decimal("0")) + leg.stake_native
            )
        for currency, amount in locked_by_currency.items():
            venue = VenueName.MATCHBOOK if currency == "GBP" else VenueName.KALSHI
            assert after.pool(venue, currency).locked_capital - before.pool(
                venue, currency
            ).locked_capital == amount
            assert after.pool(venue, currency).available_cash == before.pool(
                venue, currency
            ).available_cash - amount

        journals = list(ops.journal.list_entries())
        retry = ops.simulate_fill(
            opportunity_id,
            simulate_external=True,
            prepared_deployment_id=preview.prepared_deployment_id,
            requested_size_gbp=TEN,
            provenance=DataProvenance.FIXTURE_DEMO,
        )
        assert retry.trade_id == trade.trade_id
        assert len(ops.list_active_trades()) == 1
        assert list(ops.journal.list_entries()) == journals
        retry_snap = ledger.treasury.snapshot()
        for venue, currency in ((VenueName.MATCHBOOK, "GBP"), (VenueName.KALSHI, "USD")):
            assert retry_snap.pool(venue, currency).locked_capital == after.pool(
                venue, currency
            ).locked_capital
    finally:
        repository.close()
        ledger.close()


def test_stale_or_changed_prepared_preview_fails_closed(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, decision = _persist_qualified(
        tmp_path, left=_matchbook_btts(), right=_kalshi_btts()
    )
    try:
        opportunity_id = _opportunity_id(decision.canonical_market_id)
        preview = ops.prepare_fixed_deployment(opportunity_id, TEN)
        assert preview.accepted is True
        plan = ops._plans[opportunity_id]
        ops._plans[opportunity_id] = plan.model_copy(
            update={
                "legs": [
                    leg.model_copy(
                        update={
                            "displayed_odds": (leg.displayed_odds or Decimal("2")) + Decimal("0.05")
                        }
                    )
                    for leg in plan.legs
                ]
            }
        )
        with pytest.raises(PaperOperationsError, match="prepared_deployment_stale"):
            ops.simulate_fill(
                opportunity_id,
                simulate_external=True,
                prepared_deployment_id=preview.prepared_deployment_id,
                provenance=DataProvenance.FIXTURE_DEMO,
            )
        assert ops.list_active_trades() == []
        with pytest.raises(PaperOperationsError, match="unknown_prepared_deployment"):
            ops.simulate_fill(
                opportunity_id,
                simulate_external=True,
                prepared_deployment_id="pdep:not-a-real-preview",
                provenance=DataProvenance.FIXTURE_DEMO,
            )
        assert ops.list_active_trades() == []
    finally:
        repository.close()
        ledger.close()


def test_confirm_prepared_deployment_api_locks_requested_size(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger, decision = _persist_qualified(
        tmp_path, left=_matchbook_btts(), right=_kalshi_btts()
    )
    try:
        app.dependency_overrides[get_paper_operations_service] = lambda: ops
        app.dependency_overrides[get_paper_ledger] = lambda: ledger
        client = TestClient(app)
        opportunity_id = _opportunity_id(decision.canonical_market_id)
        before = ledger.treasury.snapshot()
        prepared = client.post(
            "/paper/prepare-deployment",
            json={
                "opportunity_id": opportunity_id,
                "requested_size_gbp": "10",
                "operator_note": "PAPER-ONLY lane 2 prepare",
            },
        )
        assert prepared.status_code == 200, prepared.text
        body = prepared.json()
        assert body["accepted"] is True
        assert body["prepared_deployment_id"]
        assert Decimal(body["applied_size_gbp"]) == TEN
        after_preview = ledger.treasury.snapshot()
        assert after_preview.pool(VenueName.MATCHBOOK, "GBP").locked_capital == before.pool(
            VenueName.MATCHBOOK, "GBP"
        ).locked_capital
        assert client.get("/paper/trades/active").json() == []

        unknown = client.post(
            "/paper/simulate-fill",
            json={
                "opportunity_id": opportunity_id,
                "prepared_deployment_id": "pdep:missing",
                "simulate_external": True,
                "provenance": "fixture_demo",
            },
        )
        assert unknown.status_code == 409
        assert client.get("/paper/trades/active").json() == []

        confirmed = client.post(
            "/paper/simulate-fill",
            json={
                "opportunity_id": opportunity_id,
                "prepared_deployment_id": body["prepared_deployment_id"],
                "requested_size_gbp": "10",
                "simulate_external": True,
                "operator_note": "PAPER-ONLY confirm accepted prepared size",
                "provenance": "fixture_demo",
            },
        )
        assert confirmed.status_code == 200, confirmed.text
        fill_body = confirmed.json()
        assert fill_body["entry_complete"] is True
        assert fill_body["prepared_deployment_id"] == body["prepared_deployment_id"]
        active = client.get("/paper/trades/active").json()
        assert len(active) == 1
        preview_stakes = {
            (leg["venue"], leg["outcome"]): Decimal(leg["stake_native"]) for leg in body["legs"]
        }
        trade_stakes = {
            (leg["venue"], leg["outcome"]): Decimal(leg["filled_stake"]) for leg in active[0]["legs"]
        }
        assert trade_stakes == preview_stakes
        retry = client.post(
            "/paper/simulate-fill",
            json={
                "opportunity_id": opportunity_id,
                "prepared_deployment_id": body["prepared_deployment_id"],
                "simulate_external": True,
                "provenance": "fixture_demo",
            },
        )
        assert retry.status_code == 200, retry.text
        assert len(client.get("/paper/trades/active").json()) == 1
    finally:
        app.dependency_overrides.clear()
        repository.close()
        ledger.close()
