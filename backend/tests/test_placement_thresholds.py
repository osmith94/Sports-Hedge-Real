"""Max Event, Max Opportunity and Max One-Time placement authority."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from inspect import getsource

from sports_hedge.api.paper import put_operator_scanner_settings
from sports_hedge.arbitrage.allocation.adapters import request_from_complete_set
from sports_hedge.arbitrage.allocation.engine import (
    DISCRETIONARY_PAPER_CONSTRAINTS,
    allocate,
    allocate_requested_size,
)
from sports_hedge.arbitrage.allocation.models import (
    AllocationBalance,
    AllocationConstraintKind,
    BankrollAllocationPolicy,
    OpenPositionExposure,
    VenueNativeAmount,
)
from sports_hedge.arbitrage.allocation.policy import policy_from_settings
from sports_hedge.arbitrage.priority_alerts.models import LegExecutionMode, PriorityLeg
from sports_hedge.arbitrage.solver import CompleteSetArbitrageSolver
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.placement_room import (
    current_event_deployment_gbp,
    recovery_capital_room_gbp,
    remaining_event_room_gbp,
    remaining_room_gbp,
)
from sports_hedge.paper.trades import PaperTrade, PaperTradeState
from sports_hedge.persistence.operator_scanner_settings import (
    SqliteOperatorScannerSettingsStore,
    bind_runtime_operator_scanner_settings_store,
)

SOLVER = CompleteSetArbitrageSolver()
NOW = datetime(2026, 9, 27, tzinfo=UTC)
HIDDEN = {
    AllocationConstraintKind.MIN_FREE_RESERVE,
    AllocationConstraintKind.MAX_POOL_FRACTION,
    AllocationConstraintKind.FIXTURE_CONCENTRATION,
    AllocationConstraintKind.PORTFOLIO_CAP,
    AllocationConstraintKind.VENUE_LIMIT,
    AllocationConstraintKind.PER_OPPORTUNITY_LIMIT,
    AllocationConstraintKind.EXTERNAL_LEG_CAP,
}


def _policy(**overrides: object) -> BankrollAllocationPolicy:
    values: dict[str, object] = {
        "min_reserve_fraction": Decimal("0.30"),
        "max_pool_fraction_per_opportunity": Decimal("0.25"),
        "max_open_capital_fraction": Decimal("0.70"),
        "max_same_fixture_capital_fraction": Decimal("0.40"),
        "safety_haircut": Decimal("0.05"),
        "max_event_reporting": Decimal("500"),
        "max_opportunity_reporting": Decimal("300"),
        "max_one_time_reporting": Decimal("100"),
    }
    values.update(overrides)
    return BankrollAllocationPolicy(**values)


def _leg(outcome: str, venue: VenueName, max_stake: Decimal) -> PriorityLeg:
    return PriorityLeg(
        outcome=outcome,
        venue=venue,
        source_market_id=f"{venue.value}-{outcome}",
        source_runner_id=f"{venue.value}-{outcome}-runner",
        net_decimal_odds=Decimal("2.2"),
        max_stake_reporting=max_stake,
        native_currency="GBP",
        native_max_stake=max_stake,
        gbp_per_unit=Decimal("1"),
        levels_consumed=1,
        quote_age_ms=100,
        execution_mode=LegExecutionMode.INTERNAL,
    )


def _request(
    *,
    depth: Decimal = Decimal("10000"),
    available: Decimal = Decimal("100000"),
    policy: BankrollAllocationPolicy | None = None,
    event_id: str = "evt-newcastle-arsenal",
    market_id: str = "mkt-btts",
    event_deployed: Decimal | None = Decimal("0"),
    opportunity_deployed: Decimal | None = Decimal("0"),
    open_positions: list[OpenPositionExposure] | None = None,
    execution_risk_score: int = 10,
    require_balances: bool = True,
):
    legs = [
        _leg("yes", VenueName.MATCHBOOK, depth),
        _leg("no", VenueName.SMARKETS, depth),
    ]
    solution = SOLVER.solve([leg.as_executable_quote() for leg in legs])
    assert solution.is_arbitrage
    balances = [
        AllocationBalance(venue=VenueName.MATCHBOOK, currency="GBP", available=available),
        AllocationBalance(venue=VenueName.SMARKETS, currency="GBP", available=available),
    ]
    request = request_from_complete_set(
        legs,
        solution,
        policy=policy or _policy(),
        balances=balances,
        open_positions=open_positions or [],
        canonical_event_id=event_id,
        execution_risk_score=execution_risk_score,
        require_internal_balances=require_balances,
    )
    return request.model_copy(
        update={
            "canonical_market_id": market_id,
            "event_deployed_reporting": event_deployed,
            "opportunity_deployed_reporting": opportunity_deployed,
            "discretionary_placement": True,
        }
    )


def _trade(
    *,
    event_id: str,
    state: PaperTradeState,
    locked: Decimal | None,
    trade_id: str = "trade-1",
    market_id: str = "mkt-1",
) -> PaperTrade:
    return PaperTrade(
        trade_id=trade_id,
        opportunity_id=trade_id,
        canonical_event_id=event_id,
        canonical_market_id=market_id,
        state=state,
        opened_at=NOW,
        last_updated_at=NOW,
        capital_locked_gbp=locked,
    )


def test_case_a_one_time_limits_one_tranche_and_opportunity_accumulates() -> None:
    first = allocate(_request())
    assert first.accepted
    assert first.maximum_validated_capital == Decimal("100")
    assert first.limiting_constraint is AllocationConstraintKind.MAX_ONE_TIME
    second = allocate(_request(event_deployed=Decimal("100"), opportunity_deployed=Decimal("100")))
    assert second.maximum_validated_capital == Decimal("100")
    third = allocate(_request(event_deployed=Decimal("200"), opportunity_deployed=Decimal("200")))
    assert third.maximum_validated_capital == Decimal("100")
    blocked = allocate(_request(event_deployed=Decimal("300"), opportunity_deployed=Decimal("300")))
    assert blocked.accepted is False
    assert blocked.limiting_constraint is AllocationConstraintKind.MAX_OPPORTUNITY
    assert blocked.opportunity_room_gbp == Decimal("0")


def test_case_b_event_room_limits_a_new_opportunity() -> None:
    result = allocate(
        _request(
            policy=_policy(
                max_event_reporting=Decimal("250"),
                max_opportunity_reporting=Decimal("300"),
                max_one_time_reporting=Decimal("100"),
            ),
            event_deployed=Decimal("200"),
        )
    )
    assert result.maximum_validated_capital == Decimal("50")
    assert result.limiting_constraint is AllocationConstraintKind.MAX_EVENT


def test_case_c_executable_depth_can_be_tighter_than_thresholds() -> None:
    result = allocate(_request(depth=Decimal("17.5")))
    assert result.maximum_validated_capital == Decimal("35")
    assert result.limiting_constraint is AllocationConstraintKind.EXECUTABLE_DEPTH


def test_case_d_native_treasury_can_be_tighter_than_thresholds() -> None:
    probe = _request()
    tight_need = probe.legs[0].capital_native
    available = tight_need * (Decimal("60") / probe.committed_capital_at_solver_size)
    result = allocate(_request(available=available))
    assert result.maximum_validated_capital == Decimal("60")
    assert result.limiting_constraint is AllocationConstraintKind.NATIVE_VENUE_BALANCE


def test_case_e_risk_score_and_recommendation_do_not_shrink_placement() -> None:
    low = allocate(_request(execution_risk_score=0))
    high = allocate(_request(execution_risk_score=99))
    assert low.accepted and high.accepted
    assert high.maximum_validated_capital == low.maximum_validated_capital == Decimal("100")
    assert high.recommended_committed_capital == high.maximum_validated_capital
    assert high.execution_risk_score == 99
    assert high.limiting_constraint in DISCRETIONARY_PAPER_CONSTRAINTS


def test_hidden_allocator_constraints_do_not_reduce_discretionary_placement() -> None:
    hidden = _policy(
        per_opportunity_limit_reporting=Decimal("1"),
        portfolio_cap_reporting=Decimal("1"),
        venue_limits_native={VenueName.MATCHBOOK: Decimal("1")},
        external_leg_cap_native=Decimal("1"),
        max_event_reporting=Decimal("100000"),
        max_opportunity_reporting=Decimal("100000"),
        max_one_time_reporting=Decimal("100000"),
    )
    same_event = OpenPositionExposure(
        opportunity_id="sibling",
        canonical_event_id="evt-newcastle-arsenal",
        canonical_market_id="mkt-other",
        capital_reporting=Decimal("0"),
        capital_native=[
            VenueNativeAmount(venue=VenueName.MATCHBOOK, currency="GBP", amount=Decimal("400"))
        ],
    )
    request = _request(
        policy=hidden,
        available=Decimal("1000"),
        execution_risk_score=99,
        open_positions=[same_event],
    )
    result = allocate(request)
    assert result.accepted
    assert result.maximum_validated_capital == Decimal("2000")
    assert result.limiting_constraint is AllocationConstraintKind.NATIVE_VENUE_BALANCE
    assert result.recommended_committed_capital == result.maximum_validated_capital
    assert HIDDEN.isdisjoint(item.kind for item in result.hard_constraints)


def test_hedge_ratios_survive_a_scaled_tranche() -> None:
    request = _request()
    result = allocate(request)
    solver = {leg.leg_id: leg.solver_stake for leg in request.legs}
    sized = {stake.leg_id: stake.stake_reporting for stake in result.recommended_stakes}
    left, right = request.legs
    assert sized[left.leg_id] / sized[right.leg_id] == solver[left.leg_id] / solver[right.leg_id]


def test_another_event_does_not_consume_event_room() -> None:
    other = OpenPositionExposure(
        opportunity_id="other-event",
        canonical_event_id="evt-other",
        canonical_market_id="mkt-other",
        capital_reporting=Decimal("400"),
    )
    result = allocate(_request(event_deployed=None, open_positions=[other]))
    assert result.event_deployed_gbp == Decimal("0")
    assert result.maximum_validated_capital == Decimal("100")


def test_event_helper_counts_locks_not_state_labels() -> None:
    event = "evt-newcastle-arsenal"
    trades = [
        _trade(event_id=event, state=PaperTradeState.OPEN, locked=Decimal("150"), trade_id="a"),
        _trade(event_id=event, state=PaperTradeState.PARTIAL, locked=Decimal("40"), trade_id="b"),
        _trade(event_id=event, state=PaperTradeState.CLOSED, locked=Decimal("500"), trade_id="c"),
        _trade(event_id="evt-other", state=PaperTradeState.OPEN, locked=Decimal("80"), trade_id="d"),
        _trade(event_id=event, state=PaperTradeState.PENDING, locked=Decimal("25"), trade_id="e"),
        _trade(
            event_id=event,
            state=PaperTradeState.AWAITING_MANUAL_EXTERNAL,
            locked=Decimal("10"),
            trade_id="f",
        ),
        _trade(event_id=event, state=PaperTradeState.OPEN, locked=None, trade_id="g"),
        _trade(event_id=event, state=PaperTradeState.PARTIAL, locked=Decimal("0"), trade_id="h"),
    ]
    assert current_event_deployment_gbp(trades, event) == Decimal("225")
    assert remaining_event_room_gbp(trades, event, Decimal("250")) == Decimal("25")
    assert remaining_room_gbp(Decimal("100"), Decimal("400")) == Decimal("0")


def test_missing_event_identity_fails_closed() -> None:
    result = allocate(_request(event_id="", event_deployed=None))
    assert result.accepted is False
    assert result.limiting_constraint is AllocationConstraintKind.MISSING_REPORTING_GBP


def test_legacy_per_trade_cap_does_not_govern_runtime_policy() -> None:
    store = SqliteOperatorScannerSettingsStore(":memory:")
    bind_runtime_operator_scanner_settings_store(store)
    try:
        policy = policy_from_settings(Settings(max_allocated_per_trade_gbp=100))
        assert policy.per_opportunity_limit_reporting is None
        assert policy.max_event_reporting == Decimal("100")
        assert policy.max_opportunity_reporting == Decimal("100")
        assert policy.max_one_time_reporting == Decimal("100")
    finally:
        bind_runtime_operator_scanner_settings_store(None)
        store.close()


def test_existing_settings_row_migrates_conservatively_and_reloads(tmp_path) -> None:
    store = SqliteOperatorScannerSettingsStore(tmp_path / "operator.sqlite")
    saved = store.save_settings(
        min_net_edge=Decimal("0.01"),
        max_execution_risk=60,
        max_allocated_per_trade_gbp=Decimal("100"),
    )
    assert saved.max_event_gbp == Decimal("100")
    assert saved.max_opportunity_gbp == Decimal("100")
    assert saved.max_one_time_gbp == Decimal("100")
    reloaded = SqliteOperatorScannerSettingsStore(tmp_path / "operator.sqlite").load()
    assert reloaded is not None
    assert reloaded.max_one_time_gbp == Decimal("100")
    updated = store.save_settings(
        min_net_edge=Decimal("0.01"),
        max_execution_risk=60,
        max_event_gbp=Decimal("400"),
        max_opportunity_gbp=Decimal("250"),
        max_one_time_gbp=Decimal("80"),
    )
    assert updated.max_event_gbp == Decimal("400")
    assert updated.max_opportunity_gbp == Decimal("250")
    assert updated.max_one_time_gbp == Decimal("80")
    store.close()


def test_settings_edit_source_does_not_call_venues() -> None:
    source = getsource(put_operator_scanner_settings)
    assert "get_market" not in source
    assert "place_order" not in source


def test_recovery_room_is_opportunity_only() -> None:
    assert recovery_capital_room_gbp(Decimal("300"), Decimal("250")) == Decimal("50")
    assert recovery_capital_room_gbp(Decimal("100"), None) == Decimal("100")
    dust = Decimal("199.9999999999999999999999999")
    assert recovery_capital_room_gbp(Decimal("200"), dust) == Decimal("200") - dust


DUST_DEPLOYED = Decimal("199.9999999999999999999999999")
EVENT_DUST_DEPLOYED = Decimal("249.9999999999999999999999999")


def _reporting_total(result) -> Decimal:
    return sum((stake.capital_reporting for stake in result.recommended_stakes), Decimal("0"))


def test_sub_penny_opportunity_room_is_not_deployable() -> None:
    assert remaining_room_gbp(Decimal("200"), DUST_DEPLOYED) == Decimal("0")
    result = allocate(
        _request(
            policy=_policy(
                max_event_reporting=Decimal("1000"),
                max_opportunity_reporting=Decimal("200"),
                max_one_time_reporting=Decimal("1000"),
            ),
            event_deployed=DUST_DEPLOYED,
            opportunity_deployed=DUST_DEPLOYED,
        )
    )
    assert result.accepted is False
    assert result.opportunity_room_gbp == Decimal("0")
    assert result.limiting_constraint is AllocationConstraintKind.MAX_OPPORTUNITY


def test_sub_penny_event_room_is_not_deployable() -> None:
    assert remaining_room_gbp(Decimal("250"), EVENT_DUST_DEPLOYED) == Decimal("0")
    result = allocate(
        _request(
            policy=_policy(
                max_event_reporting=Decimal("250"),
                max_opportunity_reporting=Decimal("1000"),
                max_one_time_reporting=Decimal("1000"),
            ),
            event_deployed=EVENT_DUST_DEPLOYED,
            opportunity_deployed=Decimal("0"),
        )
    )
    assert result.accepted is False
    assert result.event_room_gbp == Decimal("0")
    assert result.limiting_constraint is AllocationConstraintKind.MAX_EVENT


def test_exact_event_room_cannot_recompose_above_the_cap() -> None:
    result = allocate(
        _request(
            policy=_policy(
                max_event_reporting=Decimal("50"),
                max_opportunity_reporting=Decimal("1000"),
                max_one_time_reporting=Decimal("1000"),
            ),
            event_deployed=Decimal("0"),
            opportunity_deployed=Decimal("0"),
        )
    )
    assert result.accepted
    assert result.maximum_validated_capital <= Decimal("50")
    assert result.recommended_committed_capital <= Decimal("50")
    assert _reporting_total(result) <= Decimal("50")
    assert result.maximum_validated_capital != Decimal("50.00000000000000000000000001")


def test_one_penny_of_opportunity_room_stays_deployable() -> None:
    assert remaining_room_gbp(Decimal("100.01"), Decimal("100")) == Decimal("0.01")
    assert remaining_room_gbp(Decimal("100"), Decimal("100") - Decimal("0.009")) == Decimal("0")
    result = allocate(
        _request(
            policy=_policy(
                max_event_reporting=Decimal("1000"),
                max_opportunity_reporting=Decimal("100.01"),
                max_one_time_reporting=Decimal("1000"),
            ),
            event_deployed=Decimal("0"),
            opportunity_deployed=Decimal("100"),
        )
    )
    assert result.accepted
    assert result.opportunity_room_gbp == Decimal("0.01")
    assert result.maximum_validated_capital > 0
    assert result.maximum_validated_capital <= Decimal("0.01")
    assert _reporting_total(result) <= Decimal("0.01")


def test_requested_reporting_size_cannot_recompose_above_the_request() -> None:
    request = _request(
        policy=_policy(
            max_event_reporting=Decimal("1000"),
            max_opportunity_reporting=Decimal("1000"),
            max_one_time_reporting=Decimal("1000"),
        )
    )
    baseline = allocate(request)
    assert baseline.accepted
    assert baseline.maximum_validated_capital > Decimal("100")
    sized = allocate_requested_size(request, Decimal("100"))
    assert sized.accepted
    assert sized.recommended_committed_capital <= Decimal("100")
    assert _reporting_total(sized) <= Decimal("100")


def test_live_execution_remains_disabled() -> None:
    assert Settings().sports_hedge_execution_enabled is False
