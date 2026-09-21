from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.paper import get_paper_ledger
from sports_hedge.arbitrage.allocation.models import (
    AllocationBalance,
    AllocationConstraintKind,
    AllocationLeg,
    AllocationRequest,
    BankrollAllocationPolicy,
    OpenPositionExposure,
    VenueNativeAmount,
)
from sports_hedge.arbitrage.capital_optimiser.models import (
    AllocationStrategyName,
    CapitalOptimiserOpportunity,
    CapitalOptimiserRequest,
    OpportunityExclusionReason,
    UnusedCapitalClass,
)
from sports_hedge.arbitrage.capital_optimiser.service import (
    PaperCapitalOptimiser,
    request_from_treasury,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger


FX = Decimal("0.75")
NOW = datetime(2026, 9, 21, 12, tzinfo=UTC)


def _policy(**overrides) -> BankrollAllocationPolicy:
    values = dict(
        min_reserve_fraction=Decimal("0"),
        max_pool_fraction_per_opportunity=Decimal("1"),
        max_open_capital_fraction=Decimal("1"),
        max_same_fixture_capital_fraction=Decimal("1"),
        safety_haircut=Decimal("0"),
        operator_recommended_cap_reporting=None,
        risk_limit_reporting=None,
    )
    values.update(overrides)
    return BankrollAllocationPolicy(**values)


def _balances(
    *,
    matchbook: Decimal = Decimal("100"),
    smarkets: Decimal = Decimal("100"),
    polymarket: Decimal = Decimal("0"),
    matchbook_locked: Decimal = Decimal("0"),
    matchbook_transit: Decimal = Decimal("0"),
) -> list[AllocationBalance]:
    rows = [
        AllocationBalance(
            venue=VenueName.MATCHBOOK,
            currency="GBP",
            available=matchbook,
            locked=matchbook_locked,
            transit=matchbook_transit,
        ),
        AllocationBalance(
            venue=VenueName.SMARKETS,
            currency="GBP",
            available=smarkets,
        ),
    ]
    if polymarket > 0:
        rows.append(
            AllocationBalance(
                venue=VenueName.POLYMARKET,
                currency="USD",
                available=polymarket,
                gbp_per_unit=FX,
            )
        )
    return rows


def _leg(
    *,
    outcome: str,
    venue: VenueName,
    native: Decimal,
    currency: str = "GBP",
    gbp_per_unit: Decimal = Decimal("1"),
    depth_multiplier: Decimal = Decimal("1"),
) -> AllocationLeg:
    reporting = native * gbp_per_unit
    return AllocationLeg(
        leg_id=f"{venue.value}:{outcome}",
        outcome=outcome,
        venue=venue,
        source_market_id=f"{venue.value}-mkt",
        source_runner_id=f"{venue.value}-{outcome}",
        solver_stake=reporting,
        max_stake=reporting * depth_multiplier,
        capital_per_unit=Decimal("1"),
        native_currency=currency,
        gbp_per_unit=gbp_per_unit,
    )


def _request(
    *,
    event: str,
    legs: list[AllocationLeg],
    profit: Decimal,
) -> AllocationRequest:
    capital = sum((leg.capital_reporting for leg in legs), Decimal("0"))
    return AllocationRequest(
        solver_model="test_fixture",
        is_arbitrage=True,
        canonical_event_id=event,
        roi=(profit / capital) if capital else Decimal("0"),
        guaranteed_profit_at_solver_size=profit,
        committed_capital_at_solver_size=capital,
        legs=legs,
        balances=[],
        open_positions=[],
        policy=_policy(),
        require_internal_balances=True,
        quote_age_ms=100,
        recent_volatility_bps=Decimal("0"),
    )


def _opp(
    opportunity_id: str,
    *,
    event: str,
    mb_native: Decimal,
    other_native: Decimal,
    profit: Decimal,
    other_venue: VenueName = VenueName.SMARKETS,
    other_currency: str = "GBP",
    other_fx: Decimal = Decimal("1"),
    approved: bool = True,
    is_arbitrage: bool = True,
    extra_request=None,
) -> CapitalOptimiserOpportunity:
    legs = [
        _leg(outcome="home", venue=VenueName.MATCHBOOK, native=mb_native),
        _leg(
            outcome="away",
            venue=other_venue,
            native=other_native,
            currency=other_currency,
            gbp_per_unit=other_fx,
        ),
    ]
    request = extra_request or _request(event=event, legs=legs, profit=profit)
    if extra_request is None:
        request = request.model_copy(update={"is_arbitrage": is_arbitrage})
    return CapitalOptimiserOpportunity(
        opportunity_id=opportunity_id,
        approved_equivalent=approved,
        fee_snapshot_ids=["fee-test-1"],
        request=request,
    )


def _run(opportunities, *, balances=None, policy=None, open_positions=None, min_net=Decimal("0"), fx=None, fx_source="test_fx"):
    request = CapitalOptimiserRequest(
        opportunities=opportunities,
        balances=balances if balances is not None else _balances(),
        open_positions=open_positions or [],
        policy=policy or _policy(),
        min_net_arb=min_net,
        approved_fx=fx or {"GBP": Decimal("1"), "USD": FX},
        fx_source=fx_source,
        fx_as_of=NOW,
    )
    return PaperCapitalOptimiser().optimise(request)


def test_global_lp_beats_greedy_when_opportunities_compete_across_venues() -> None:
    opportunities = [
        _opp("opp-a", event="evt-a", mb_native=Decimal("99"), other_native=Decimal("1"), profit=Decimal("10")),
        _opp("opp-b", event="evt-b", mb_native=Decimal("1"), other_native=Decimal("99"), profit=Decimal("10")),
        _opp("opp-c", event="evt-c", mb_native=Decimal("50"), other_native=Decimal("50"), profit=Decimal("12")),
    ]
    result = _run(opportunities)
    assert result.paper_only is True
    assert result.places_orders is False
    assert result.mutates_treasury is False
    assert result.on_scan_critical_path is False
    assert result.formulation.mixed_integer is False
    assert result.formulation.engine == "scipy.linprog.highs"
    assert result.greedy.strategy is AllocationStrategyName.GREEDY_SEQUENTIAL
    assert result.maximum_validated.strategy is AllocationStrategyName.GLOBAL_LINEAR_PROGRAM
    assert result.greedy.guaranteed_net_profit < result.maximum_validated.guaranteed_net_profit
    selected = {item.opportunity_id: item for item in result.maximum_validated.opportunities}
    assert set(selected) == {"opp-a", "opp-b", "opp-c"}
    assert abs(result.maximum_validated.guaranteed_net_profit - Decimal("22")) <= Decimal("0.0000002")
    assert selected["opp-c"].scale == Decimal("1")
    assert abs(selected["opp-a"].scale - Decimal("0.5")) <= Decimal("0.00000002")
    assert abs(selected["opp-b"].scale - Decimal("0.5")) <= Decimal("0.00000002")
    assert result.comparison.improvement_vs_greedy > 0
    assert result.independent.jointly_feasible is False
    assert result.independent.guaranteed_net_profit > result.maximum_validated.guaranteed_net_profit


def test_independent_per_opportunity_over_allocates_shared_native_pools() -> None:
    opportunities = [
        _opp("opp-a", event="evt-a", mb_native=Decimal("99"), other_native=Decimal("1"), profit=Decimal("10")),
        _opp("opp-b", event="evt-b", mb_native=Decimal("1"), other_native=Decimal("99"), profit=Decimal("10")),
        _opp("opp-c", event="evt-c", mb_native=Decimal("50"), other_native=Decimal("50"), profit=Decimal("12")),
    ]
    result = _run(opportunities)
    reasons = " ".join(result.independent.infeasibility_reasons)
    assert "native:matchbook:GBP" in reasons or "reserve:matchbook:GBP" in reasons


def test_higher_net_profit_after_fees_is_preferred_on_the_same_capital() -> None:
    cheap = _opp(
        "fee-cheap",
        event="evt-1",
        mb_native=Decimal("100"),
        other_native=Decimal("100"),
        profit=Decimal("8"),
    )
    expensive = _opp(
        "fee-expensive",
        event="evt-2",
        mb_native=Decimal("100"),
        other_native=Decimal("100"),
        profit=Decimal("3"),
    )
    result = _run(
        [cheap, expensive],
        balances=_balances(matchbook=Decimal("100"), smarkets=Decimal("100")),
    )
    selected = {item.opportunity_id for item in result.maximum_validated.opportunities}
    assert selected == {"fee-cheap"}
    assert result.maximum_validated.guaranteed_net_profit == Decimal("8")


def test_locked_and_transit_capital_are_not_spendable() -> None:
    opp = _opp(
        "needs-cash",
        event="evt-1",
        mb_native=Decimal("80"),
        other_native=Decimal("1"),
        profit=Decimal("5"),
    )
    result = _run(
        [opp],
            balances=_balances(
                matchbook=Decimal("0"),
                smarkets=Decimal("100"),
                matchbook_locked=Decimal("80"),
                matchbook_transit=Decimal("50"),
            ),
    )
    assert result.maximum_validated.selected_count == 0
    unused = next(
        item
        for item in result.maximum_validated.unused_capital
        if item.venue is VenueName.MATCHBOOK
    )
    assert unused.locked == Decimal("80")
    assert unused.transit == Decimal("50")
    assert unused.allocated_native == Decimal("0")
    assert "locked_capital_is_not_spendable" in unused.notes
    assert "transit_capital_is_not_spendable" in unused.notes


def test_reserve_and_native_pools_are_joint_constraints() -> None:
    opps = [
        _opp("a", event="e1", mb_native=Decimal("50"), other_native=Decimal("10"), profit=Decimal("4")),
        _opp("b", event="e2", mb_native=Decimal("50"), other_native=Decimal("10"), profit=Decimal("4")),
    ]
    result = _run(
        opps,
        balances=_balances(matchbook=Decimal("100"), smarkets=Decimal("100")),
        policy=_policy(min_reserve_fraction=Decimal("0.30")),
    )
    mb = next(
        item
        for item in result.maximum_validated.unused_capital
        if item.venue is VenueName.MATCHBOOK
    )
    assert mb.allocated_native <= Decimal("70")
    assert mb.reserve_remaining >= Decimal("30") - Decimal("0.00000001")
    kinds = {item.kind for item in result.maximum_validated.binding_constraints}
    assert AllocationConstraintKind.MIN_FREE_RESERVE in kinds or mb.classification in {
        UnusedCapitalClass.HELD_AS_RESERVE,
        UnusedCapitalClass.FULLY_ALLOCATED,
        UnusedCapitalClass.NO_REMAINING_ELIGIBLE_USE,
        UnusedCapitalClass.BLOCKED_BY_OTHER_CONSTRAINT,
    }


def test_usd_and_gbp_are_not_summed_and_missing_fx_fails_closed() -> None:
    usd_opp = _opp(
        "usd-leg",
        event="evt-fx",
        mb_native=Decimal("10"),
        other_native=Decimal("20"),
        profit=Decimal("2"),
        other_venue=VenueName.POLYMARKET,
        other_currency="USD",
        other_fx=FX,
    )
    missing = _run(
        [usd_opp],
        balances=_balances(matchbook=Decimal("100"), smarkets=Decimal("0"), polymarket=Decimal("100")),
        fx={"GBP": Decimal("1")},
        fx_source="ecb_test",
    )
    assert missing.excluded[0].reason is OpportunityExclusionReason.MISSING_FX_SNAPSHOT
    no_source = _run(
        [usd_opp],
        balances=_balances(matchbook=Decimal("100"), smarkets=Decimal("0"), polymarket=Decimal("100")),
        fx={"GBP": Decimal("1"), "USD": FX},
        fx_source="",
    )
    assert no_source.excluded[0].reason is OpportunityExclusionReason.MISSING_FX_SNAPSHOT
    ok = _run(
        [usd_opp],
        balances=_balances(matchbook=Decimal("100"), smarkets=Decimal("0"), polymarket=Decimal("40")),
        fx={"GBP": Decimal("1"), "USD": FX},
        fx_source="ecb_test",
    )
    selected = ok.maximum_validated.opportunities
    assert selected
    currencies = {item.currency for row in selected for item in row.capital_required}
    assert "USD" in currencies
    assert "GBP" in currencies


def test_below_min_net_and_unapproved_are_excluded() -> None:
    weak = _opp("weak", event="e1", mb_native=Decimal("10"), other_native=Decimal("10"), profit=Decimal("0.01"))
    unapproved = _opp(
        "unapproved",
        event="e2",
        mb_native=Decimal("10"),
        other_native=Decimal("10"),
        profit=Decimal("5"),
        approved=False,
    )
    result = _run([weak, unapproved], min_net=Decimal("0.10"))
    reasons = {item.opportunity_id: item.reason for item in result.excluded}
    assert reasons["weak"] is OpportunityExclusionReason.BELOW_MIN_NET_ARB
    assert reasons["unapproved"] is OpportunityExclusionReason.NOT_APPROVED_EQUIVALENT
    assert result.maximum_validated.selected_count == 0


def test_existing_open_locks_reduce_joint_room() -> None:
    opp = _opp("new", event="evt-new", mb_native=Decimal("80"), other_native=Decimal("10"), profit=Decimal("5"))
    opens = [
        OpenPositionExposure(
            opportunity_id="already-open",
            canonical_event_id="evt-old",
            capital_native=[
                VenueNativeAmount(venue=VenueName.MATCHBOOK, currency="GBP", amount=Decimal("40"))
            ],
            capital_reporting=Decimal("40"),
        )
    ]
    result = _run(
        [opp],
        balances=_balances(
            matchbook=Decimal("60"),
            smarkets=Decimal("100"),
            matchbook_locked=Decimal("40"),
        ),
        open_positions=opens,
        policy=_policy(max_open_capital_fraction=Decimal("0.70")),
    )
    mb = next(
        item
        for item in result.maximum_validated.unused_capital
        if item.venue is VenueName.MATCHBOOK
    )
    assert mb.allocated_native <= Decimal("60")


def test_optimiser_does_not_mutate_paper_treasury() -> None:
    ledger = SqlitePaperLedger(
        ":memory:",
        seed_gbp=Decimal("1000"),
        usd_gbp_per_unit=Decimal("0.80"),
        fx_source="paper_demo_fx_snapshot",
        include_kalshi=True,
    )
    before = ledger.treasury.snapshot()
    before_available = {
        (pool.venue, pool.native_currency): pool.available_cash for pool in before.pools
    }
    before_events = len(before.events)
    opportunities = [
        _opp("opp-a", event="evt-a", mb_native=Decimal("99"), other_native=Decimal("1"), profit=Decimal("10")),
        _opp("opp-b", event="evt-b", mb_native=Decimal("1"), other_native=Decimal("99"), profit=Decimal("10")),
    ]
    request = request_from_treasury(
        before,
        opportunities=opportunities,
        policy=_policy(),
        min_net_arb=Decimal("0"),
    )
    result = PaperCapitalOptimiser().optimise(request)
    after = ledger.treasury.snapshot()
    after_available = {
        (pool.venue, pool.native_currency): pool.available_cash for pool in after.pools
    }
    assert after_available == before_available
    assert len(after.events) == before_events
    assert result.mutates_treasury is False
    assert result.places_orders is False
    ledger.close()


def test_api_run_is_read_only_and_returns_comparison() -> None:
    client = TestClient(app)
    payload = {
        "use_active_treasury": False,
        "min_net_arb": "0",
        "fx_source": "test_fx",
        "approved_fx": {"GBP": "1"},
        "policy": {
            "min_reserve_fraction": "0",
            "max_pool_fraction_per_opportunity": "1",
            "max_open_capital_fraction": "1",
            "max_same_fixture_capital_fraction": "1",
            "safety_haircut": "0",
            "operator_recommended_cap_reporting": None,
            "risk_limit_reporting": None,
        },
        "balances": [
            {"venue": "matchbook", "currency": "GBP", "available": "100"},
            {"venue": "smarkets", "currency": "GBP", "available": "100"},
        ],
        "opportunities": [
            _opp("opp-a", event="evt-a", mb_native=Decimal("99"), other_native=Decimal("1"), profit=Decimal("10")).model_dump(mode="json"),
            _opp("opp-b", event="evt-b", mb_native=Decimal("1"), other_native=Decimal("99"), profit=Decimal("10")).model_dump(mode="json"),
            _opp("opp-c", event="evt-c", mb_native=Decimal("50"), other_native=Decimal("50"), profit=Decimal("12")).model_dump(mode="json"),
        ],
    }
    health = client.get("/paper/capital-optimiser/health")
    assert health.status_code == 200
    assert health.json()["places_orders"] is False
    assert health.json()["mutates_treasury"] is False
    response = client.post("/paper/capital-optimiser/run", json=payload)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["places_orders"] is False
    assert body["mutates_treasury"] is False
    assert body["on_scan_critical_path"] is False
    assert Decimal(body["comparison"]["improvement_vs_greedy"]) > 0
    profit = Decimal(body["maximum_validated"]["guaranteed_net_profit"])
    assert abs(profit - Decimal("22")) <= Decimal("0.0000002")


def test_api_run_against_treasury_snapshot_does_not_lock() -> None:
    ledger = SqlitePaperLedger(
        ":memory:",
        seed_gbp=Decimal("1000"),
        usd_gbp_per_unit=Decimal("0.80"),
        fx_source="paper_demo_fx_snapshot",
        include_kalshi=True,
    )
    app.dependency_overrides[get_paper_ledger] = lambda: ledger
    client = TestClient(app)
    try:
        before = ledger.treasury.snapshot()
        available = before.pool(VenueName.MATCHBOOK, "GBP").available_cash
        opp = _opp(
            "mb-only-pair",
            event="evt-1",
            mb_native=Decimal("10"),
            other_native=Decimal("10"),
            profit=Decimal("1"),
            other_venue=VenueName.POLYMARKET,
            other_currency="USD",
            other_fx=Decimal("0.80"),
        )
        response = client.post(
            "/paper/capital-optimiser/run",
            json={
                "use_active_treasury": True,
                "min_net_arb": "0",
                "opportunities": [opp.model_dump(mode="json")],
            },
        )
        assert response.status_code == 200, response.text
        after = ledger.treasury.snapshot()
        assert after.pool(VenueName.MATCHBOOK, "GBP").available_cash == available
        assert after.session.session_id == before.session.session_id
        lock_events = [item for item in after.events if item.event_type.value == "lock"]
        assert lock_events == []
        assert response.json()["data_kind"] == "modelled"
    finally:
        app.dependency_overrides.clear()
        ledger.close()


def test_scanner_module_does_not_reference_capital_optimiser() -> None:
    import inspect

    from sports_hedge.application import paper_scan, paper_operations

    assert "capital_optimiser" not in inspect.getsource(paper_scan)
    assert "capital_optimiser" not in inspect.getsource(paper_operations)


def test_recommended_plan_never_exceeds_maximum_validated() -> None:
    opportunities = [
        _opp("opp-a", event="evt-a", mb_native=Decimal("99"), other_native=Decimal("1"), profit=Decimal("10")),
        _opp("opp-c", event="evt-c", mb_native=Decimal("50"), other_native=Decimal("50"), profit=Decimal("12")),
    ]
    result = _run(opportunities, policy=_policy(safety_haircut=Decimal("0.20")))
    assert result.recommended.guaranteed_net_profit <= result.maximum_validated.guaranteed_net_profit
    for row in result.recommended.unused_capital:
        assert row.allocated_native <= row.available_before
        assert row.conditionally_releasable == Decimal("0") or "not_spendable" in " ".join(row.notes)
