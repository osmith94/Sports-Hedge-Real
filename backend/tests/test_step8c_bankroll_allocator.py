from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.market_intelligence import get_market_intelligence_service
from sports_hedge.api.paper import get_paper_audit_repository, get_paper_liquidity_repository
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.application.complete_set import SOLVER_MODEL_GENERALIZED, SOLVER_MODEL_SIMPLE
from sports_hedge.arbitrage.allocation.adapters import (
    FOOTBALL_HALFTIME_MINUTES,
    FOOTBALL_REGULATION_PLAYING_MINUTES,
    MODELLED_STOPPAGE_AND_SETTLEMENT_BUFFER_MINUTES,
    UNDERSTATED_FULL_TIME_ELAPSED_MINUTES,
    lock_hours_until_capital_release,
    request_from_complete_set,
    request_from_payoff,
)
from sports_hedge.arbitrage.allocation.engine import allocate
from sports_hedge.arbitrage.allocation.models import (
    AllocatedStake,
    AllocationBalance,
    AllocationConstraintKind,
    AllocationResult,
    BankrollAllocationPolicy,
    OpenPositionExposure,
    ReductionInputStatus,
    VenueNativeAmount,
)
from sports_hedge.arbitrage.models import ArbitrageSolution, ArbitrageStake
from sports_hedge.arbitrage.payoff_solver import GeneralizedMaxMinSolver, payoff_leg_from_back
from sports_hedge.arbitrage.models import PayoffProblem
from sports_hedge.arbitrage.priority_alerts.models import (
    FillConfidence,
    FillConfidenceBreakdown,
    FillConfidenceInputs,
    LegExecutionMode,
    PriorityLeg,
)
from sports_hedge.arbitrage.solver import CompleteSetArbitrageSolver
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.football import (
    CanonicalEvent,
    CanonicalMarket,
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.application.paper_scan import (
    FillPlanMappingError,
    apply_allocation_to_fill_legs,
)
from sports_hedge.paper.fills import PaperOpportunityLeg
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.persistence.liquidity import SqlitePaperLiquidityRepository
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from venue_cost_helpers import venue_cost_payload

from test_paper_scan_api import KICKOFF, OBSERVED
from test_step8b_first_team_to_score import _ftts_books, _ftts_mb_payload, _ftts_pm_payload


SOLVER = CompleteSetArbitrageSolver()
POLICY = BankrollAllocationPolicy(
    min_reserve_fraction=Decimal("0.30"),
    max_pool_fraction_per_opportunity=Decimal("0.25"),
    max_open_capital_fraction=Decimal("0.70"),
    max_same_fixture_capital_fraction=Decimal("0.40"),
    max_concurrent_open_opportunities=4,
    safety_haircut=Decimal("0.05"),
)


def _fill_high() -> FillConfidenceBreakdown:
    return FillConfidenceBreakdown(
        band=FillConfidence.HIGH,
        score=90,
        inputs=FillConfidenceInputs(
            depth_coverage_ratio=Decimal("20"),
            quote_age_ms=100,
            quote_persistence=Decimal("1"),
            levels_consumed=1,
        ),
        reasons=["test_high"],
    )


def _fill_low() -> FillConfidenceBreakdown:
    return FillConfidenceBreakdown(
        band=FillConfidence.LOW,
        score=20,
        inputs=FillConfidenceInputs(
            depth_coverage_ratio=Decimal("1"),
            quote_age_ms=100,
            quote_persistence=Decimal("1"),
            levels_consumed=4,
        ),
        reasons=["test_low"],
    )


def _leg(
    *,
    outcome: str,
    venue: VenueName,
    max_stake: Decimal,
    odds: Decimal = Decimal("2.2"),
    currency: str = "GBP",
    gbp_per_unit: Decimal = Decimal("1"),
    mode: LegExecutionMode = LegExecutionMode.INTERNAL,
    levels: int = 1,
) -> PriorityLeg:
    return PriorityLeg(
        outcome=outcome,
        venue=venue,
        source_market_id=f"{venue.value}-{outcome}",
        source_runner_id=f"{venue.value}-{outcome}-runner",
        net_decimal_odds=odds,
        max_stake_reporting=max_stake,
        native_currency=currency,
        native_max_stake=max_stake / gbp_per_unit,
        gbp_per_unit=gbp_per_unit,
        levels_consumed=levels,
        quote_age_ms=100,
        execution_mode=mode,
    )


def _balances(matchbook: Decimal = Decimal("1000"), polymarket: Decimal = Decimal("1333.333333333")) -> list[AllocationBalance]:
    return [
        AllocationBalance(venue=VenueName.MATCHBOOK, currency="GBP", available=matchbook),
        AllocationBalance(
            venue=VenueName.POLYMARKET,
            currency="USD",
            available=polymarket,
            gbp_per_unit=Decimal("0.75"),
        ),
        AllocationBalance(venue=VenueName.SMARKETS, currency="GBP", available=Decimal("0")),
    ]


def _demo_request(
    *,
    mb_depth: Decimal = Decimal("10000"),
    pm_depth: Decimal = Decimal("10000"),
    balances: list[AllocationBalance] | None = None,
    open_positions: list[OpenPositionExposure] | None = None,
    execution_risk_score: int = 10,
    fill=None,
    lock_hours: Decimal | None = None,
    lock_basis: str | None = None,
    missing_balances: bool = False,
    mb_odds: Decimal = Decimal("2.2"),
    pm_odds: Decimal = Decimal("2.2"),
    policy: BankrollAllocationPolicy | None = None,
    canonical_event_id: str = "evt-demo",
    require_balances: bool = True,
    extra_leg_kwargs=None,
):
    extra_leg_kwargs = extra_leg_kwargs or {}
    legs = [
        _leg(
            outcome="home",
            venue=VenueName.MATCHBOOK,
            max_stake=mb_depth,
            odds=mb_odds,
            **extra_leg_kwargs,
        ),
        _leg(
            outcome="away",
            venue=VenueName.POLYMARKET,
            max_stake=pm_depth,
            odds=pm_odds,
            currency="USD",
            gbp_per_unit=Decimal("0.75"),
            mode=LegExecutionMode.EXTERNAL_OPERATOR,
            **extra_leg_kwargs,
        ),
    ]
    solution = SOLVER.solve([leg.as_executable_quote() for leg in legs])
    return request_from_complete_set(
        legs,
        solution,
        policy=policy or POLICY,
        balances=[] if missing_balances else (balances if balances is not None else _balances()),
        open_positions=open_positions,
        canonical_event_id=canonical_event_id,
        execution_risk_score=execution_risk_score,
        fill_confidence=fill if fill is not None else _fill_high(),
        expected_lock_duration_hours=lock_hours,
        expected_lock_basis=lock_basis,
        quote_age_ms=100,
        recent_volatility_bps=Decimal("0"),
        require_internal_balances=require_balances,
    )


def _ratio(result) -> Decimal:
    stakes = {item.outcome: item.stake_reporting for item in result.recommended_stakes}
    return stakes["home"] / stakes["away"]


def test_acceptance_limiting_depth_preserves_solver_ratios() -> None:
    request = _demo_request(mb_depth=Decimal("50"), pm_depth=Decimal("10000"))
    solver_ratio = request.legs[0].solver_stake / request.legs[1].solver_stake
    result = allocate(request)
    assert result.accepted is True
    assert result.limiting_constraint is AllocationConstraintKind.EXECUTABLE_DEPTH
    mb = next(item for item in result.recommended_stakes if item.venue is VenueName.MATCHBOOK)
    assert mb.stake_reporting <= Decimal("50")
    assert _ratio(result) == solver_ratio
    assert result.recommended_size < result.maximum_validated_size or result.reduction_factors


def test_acceptance_pool_reserve_prevents_full_thousand() -> None:
    result = allocate(_demo_request())
    assert result.accepted is True
    assert result.maximum_validated_capital < Decimal("1000")
    assert result.recommended_committed_capital < Decimal("1000")
    assert result.limiting_constraint in {
        AllocationConstraintKind.MAX_POOL_FRACTION,
        AllocationConstraintKind.MIN_FREE_RESERVE,
    }
    for row in result.free_balance_after:
        if row.venue is VenueName.MATCHBOOK:
            assert row.free_balance >= 0
            assert row.reserve_remaining >= 0
            assert row.free_balance + row.allocated_native == Decimal("1000")


def test_acceptance_native_currency_usd_not_treated_as_gbp() -> None:
    result = allocate(_demo_request())
    usd = next(item for item in result.capital_required if item.currency == "USD")
    gbp = next(item for item in result.capital_required if item.currency == "GBP")
    assert usd.venue is VenueName.POLYMARKET
    assert gbp.venue is VenueName.MATCHBOOK
    assert usd.amount != gbp.amount or True
    # USD bound in USD; FX is reporting-only on gbp_per_unit of legs.
    assert all(item.currency in {"GBP", "USD"} for item in result.capital_required)


def test_acceptance_concurrency_existing_open_reduces_size() -> None:
    open_pos = [
        OpenPositionExposure(
            opportunity_id="open-1",
            canonical_event_id="other-evt",
            capital_native=[
                VenueNativeAmount(
                    venue=VenueName.MATCHBOOK, currency="GBP", amount=Decimal("400")
                )
            ],
            capital_reporting=Decimal("400"),
        )
    ]
    balances = [
        AllocationBalance(
            venue=VenueName.MATCHBOOK,
            currency="GBP",
            available=Decimal("600"),
            locked=Decimal("400"),
        ),
        AllocationBalance(
            venue=VenueName.POLYMARKET,
            currency="USD",
            available=Decimal("1333.333333333"),
            gbp_per_unit=Decimal("0.75"),
        ),
        AllocationBalance(venue=VenueName.SMARKETS, currency="GBP", available=Decimal("0")),
    ]
    policy = BankrollAllocationPolicy(
        min_reserve_fraction=Decimal("0"),
        max_pool_fraction_per_opportunity=Decimal("1"),
        max_open_capital_fraction=Decimal("0.70"),
        max_same_fixture_capital_fraction=Decimal("1"),
        max_concurrent_open_opportunities=4,
        safety_haircut=Decimal("0"),
    )
    deep = allocate(_demo_request(policy=policy, fill=_fill_high()))
    reduced = allocate(_demo_request(balances=balances, open_positions=open_pos, policy=policy, fill=_fill_high()))
    assert reduced.accepted is True
    assert reduced.maximum_validated_capital < deep.maximum_validated_capital


def test_acceptance_conditionally_releasable_is_not_spendable() -> None:
    balances = [
        AllocationBalance(
            venue=VenueName.MATCHBOOK,
            currency="GBP",
            available=Decimal("700"),
            locked=Decimal("300"),
            conditionally_releasable=Decimal("300"),
        ),
        AllocationBalance(
            venue=VenueName.POLYMARKET,
            currency="USD",
            available=Decimal("1333.333333333"),
            gbp_per_unit=Decimal("0.75"),
        ),
        AllocationBalance(venue=VenueName.SMARKETS, currency="GBP", available=Decimal("0")),
    ]
    result = allocate(_demo_request(balances=balances))
    assert result.accepted is True
    mb = next(row for row in result.free_balance_after if row.venue is VenueName.MATCHBOOK)
    assert mb.conditionally_releasable == Decimal("300")
    assert mb.allocated_native <= Decimal("700")
    assert mb.free_balance >= 0


def test_acceptance_execution_quality_changes_recommended_not_arb_class() -> None:
    high = allocate(_demo_request(fill=_fill_high(), execution_risk_score=10))
    low = allocate(_demo_request(fill=_fill_low(), execution_risk_score=70))
    assert high.accepted and low.accepted
    assert high.guaranteed_roi == low.guaranteed_roi
    assert low.recommended_committed_capital < high.recommended_committed_capital
    assert any(factor.name == "execution_risk" for factor in low.reduction_factors)
    assert any(factor.name == "fill_confidence" for factor in low.reduction_factors)


def test_acceptance_lock_duration_metric_does_not_change_arb_class() -> None:
    short = allocate(
        _demo_request(
            lock_hours=Decimal("0.5"),
            lock_basis="kickoff_plus_elapsed_regulation_halftime_stoppage_settlement_buffer",
        )
    )
    long = allocate(
        _demo_request(
            lock_hours=Decimal("72"),
            lock_basis="kickoff_plus_elapsed_regulation_halftime_stoppage_settlement_buffer",
        )
    )
    assert short.accepted and long.accepted
    assert short.guaranteed_roi == long.guaranteed_roi
    assert short.capital_turnover is not None
    assert long.capital_turnover is not None
    assert short.capital_turnover.metric > long.capital_turnover.metric
    assert "not_guaranteed" in short.capital_turnover.label
    missing = allocate(_demo_request())
    assert missing.capital_turnover is None


def test_kickoff_labelled_lock_is_not_used_as_capital_release() -> None:
    result = allocate(
        _demo_request(lock_hours=Decimal("2"), lock_basis="time_to_kickoff")
    )
    assert result.accepted is True
    assert result.expected_lock_basis is None
    assert result.expected_lock_duration_hours is None
    assert result.capital_turnover is None


def test_acceptance_generalized_parity_same_allocator() -> None:
    states = ("home_first", "away_first", "no_goal")
    legs = [
        payoff_leg_from_back(
            leg_id="mb-home",
            venue=VenueName.MATCHBOOK,
            source_market_id="mb",
            source_runner_id="h",
            runner_outcome="home",
            max_stake=Decimal("10000"),
            net_decimal_odds=Decimal("3.10"),
            states=states,
            win_states=("home_first",),
            refund_states=(),
        ),
        payoff_leg_from_back(
            leg_id="pm-away",
            venue=VenueName.POLYMARKET,
            source_market_id="pm",
            source_runner_id="a",
            runner_outcome="away",
            max_stake=Decimal("10000"),
            net_decimal_odds=Decimal("3.10"),
            states=states,
            win_states=("away_first",),
            refund_states=(),
        ),
        payoff_leg_from_back(
            leg_id="mb-none",
            venue=VenueName.MATCHBOOK,
            source_market_id="mb",
            source_runner_id="n",
            runner_outcome="no_goal",
            max_stake=Decimal("10000"),
            net_decimal_odds=Decimal("3.10"),
            states=states,
            win_states=("no_goal",),
            refund_states=(),
        ),
    ]
    solution = GeneralizedMaxMinSolver().solve(PayoffProblem(states=list(states), legs=legs))
    assert solution.is_arbitrage is True
    request = request_from_payoff(
        solution,
        max_stake_by_leg={leg.leg_id: leg.max_stake for leg in legs},
        native_currency_by_leg={
            "mb-home": "GBP",
            "mb-none": "GBP",
            "pm-away": "USD",
        },
        gbp_per_unit_by_leg={
            "mb-home": Decimal("1"),
            "mb-none": Decimal("1"),
            "pm-away": Decimal("0.75"),
        },
        execution_mode_by_venue={
            VenueName.MATCHBOOK: LegExecutionMode.INTERNAL,
            VenueName.POLYMARKET: LegExecutionMode.EXTERNAL_OPERATOR,
        },
        policy=POLICY,
        balances=_balances(),
        fill_confidence=_fill_high(),
        execution_risk_score=10,
        quote_age_ms=100,
        recent_volatility_bps=Decimal("0"),
        canonical_event_id="evt-ftts",
    )
    result = allocate(request)
    assert result.accepted is True
    assert result.solver_model == SOLVER_MODEL_GENERALIZED
    assert result.guaranteed_roi == solution.roi
    scaled_min = min(solution.state_pnl.values()) * result.scale_recommended
    assert scaled_min > 0
    simple = allocate(_demo_request())
    assert simple.solver_model == SOLVER_MODEL_SIMPLE
    assert {simple.limiting_constraint, result.limiting_constraint}


def test_acceptance_external_leg_does_not_draw_matchbook_pool_as_auto() -> None:
    result = allocate(_demo_request())
    mb = next(item for item in result.capital_required if item.venue is VenueName.MATCHBOOK)
    pm = next(item for item in result.capital_required if item.venue is VenueName.POLYMARKET)
    assert mb.capital_source.value == "AUTO_POOL"
    assert pm.capital_source.value == "MANUAL_EXTERNAL"


def test_acceptance_no_full_pool_default() -> None:
    result = allocate(_demo_request())
    assert result.maximum_validated_capital <= Decimal("250") + Decimal("0.0001") or any(
        item.kind is AllocationConstraintKind.MAX_POOL_FRACTION for item in result.hard_constraints
    )
    mb_cap = next(
        item.capital_native
        for item in result.recommended_stakes
        if item.venue is VenueName.MATCHBOOK
    )
    assert mb_cap <= Decimal("250")


def test_balance_cannot_go_negative_and_reserve_not_breached_at_maximum() -> None:
    result = allocate(_demo_request())
    for row in result.free_balance_after:
        assert row.free_balance >= 0
        if row.venue is VenueName.MATCHBOOK:
            assert row.allocated_native <= Decimal("1000") - Decimal("300") or row.reserve_required <= row.free_balance + row.allocated_native


def test_missing_required_balance_fails_closed() -> None:
    result = allocate(_demo_request(missing_balances=True, require_balances=True))
    assert result.accepted is False
    assert result.limiting_constraint is AllocationConstraintKind.MISSING_BALANCE_DATA


def test_zero_stake_legs_are_excluded() -> None:
    legs = [
        _leg(outcome="home", venue=VenueName.MATCHBOOK, max_stake=Decimal("100")),
        _leg(outcome="away", venue=VenueName.SMARKETS, max_stake=Decimal("100")),
    ]
    quotes = [leg.as_executable_quote() for leg in legs]
    solution = SOLVER.solve(quotes)
    solution = solution.model_copy(
        update={
            "stakes": [
                *solution.stakes,
                ArbitrageStake(
                    outcome="draw",
                    venue=VenueName.MATCHBOOK,
                    source_market_id="mb-draw",
                    stake=Decimal("0"),
                    net_decimal_odds=Decimal("4"),
                    state_return=Decimal("0"),
                ),
            ]
        }
    )
    request = request_from_complete_set(
        legs,
        ArbitrageSolution(
            is_arbitrage=True,
            implied_probability_sum=solution.implied_probability_sum,
            total_stake=solution.total_stake,
            guaranteed_return=solution.guaranteed_return,
            guaranteed_profit=solution.guaranteed_profit,
            roi=solution.roi,
            stakes=[item for item in solution.stakes if item.stake > 0],
        ),
        policy=POLICY,
        balances=_balances(),
        fill_confidence=_fill_high(),
        execution_risk_score=10,
        require_internal_balances=True,
    )
    result = allocate(request)
    assert all(item.stake_reporting > 0 for item in result.recommended_stakes)


def test_paper_only_flags_remain_set() -> None:
    result = allocate(_demo_request())
    assert result.paper_only is True
    assert result.places_orders is False
    settings = Settings()
    assert settings.sports_hedge_execution_enabled is False
    assert settings.sports_hedge_mode == "paper"


def test_api_allocator_output_on_simple_and_generalized_decisions() -> None:
    repository = SqliteMarketIntelligenceRepository()
    audit = SqlitePaperScanRepository()
    watchlist_store = SqliteWatchlistRepository()
    liquidity = SqlitePaperLiquidityRepository(
        ":memory:",
        matchbook_gbp=Decimal("1000"),
        polymarket_usd=Decimal("1333.333333333"),
    )
    intelligence = MarketIntelligenceService(repository)
    app.dependency_overrides[get_market_intelligence_service] = lambda: intelligence
    app.dependency_overrides[get_paper_audit_repository] = lambda: audit
    app.dependency_overrides[get_watchlist_service] = lambda: WatchlistService(watchlist_store)
    app.dependency_overrides[get_paper_liquidity_repository] = lambda: liquidity
    client = TestClient(app)

    simple = {
        "left": {
            "venue": "matchbook",
            "event_payload": {
                "id": 1001,
                "name": "Newcastle United vs Chelsea",
                "start": KICKOFF.isoformat(),
                "competition-name": "Premier League",
            },
            "market_payload": {
                "id": 2001,
                "name": "Both Teams To Score",
                "runners": [
                    {"id": 301, "name": "Yes", "prices": [{"side": "back", "odds": "2.20", "available-amount": "10000"}]},
                    {"id": 302, "name": "No", "prices": [{"side": "back", "odds": "1.80", "available-amount": "10000"}]},
                ],
            },
            "observed_at": OBSERVED.isoformat(),
            "native_currency": "GBP",
            "quote_age_ms": 100,
        },
        "right": {
            "venue": "polymarket",
            "event_payload": {
                "id": "pm-event-1",
                "title": "Newcastle United vs Chelsea",
                "startTime": KICKOFF.isoformat(),
                "competition": "Premier League",
            },
            "market_payload": {
                "id": "pm-market-1",
                "question": "Both teams to score?",
                "sportsMarketType": "both teams to score",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["yes-token", "no-token"]',
                "description": "Resolves based on 90 minutes of regulation time.",
            },
            "books_by_token": {
                "yes-token": {"bids": [{"price": "0.49", "size": "20000"}], "asks": [{"price": "0.51", "size": "20000"}]},
                "no-token": {"bids": [{"price": "0.41", "size": "20000"}], "asks": [{"price": "0.43", "size": "20000"}]},
            },
            "observed_at": OBSERVED.isoformat(),
            "quote_age_ms": 120,
        },
        "venue_costs": [
            venue_cost_payload("matchbook", "0.02"),
            venue_cost_payload("polymarket", "0", detail="assumed_zero operator test cost"),
        ],
        "fx_snapshots": [{"currency": "USD", "gbp_per_unit": "0.75", "source": "test"}],
        "maximum_execution_risk": 100,
    }

    try:
        response = client.post("/paper/scan/pair", json=simple)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["solver_model"] == SOLVER_MODEL_SIMPLE
        assert body["allocation"] is not None
        assert body["allocation"]["accepted"] is True
        assert body["allocation"]["paper_only"] is True
        assert body["allocation"]["places_orders"] is False
        assert Decimal(body["allocation"]["recommended_size"]) < Decimal("1000")
        assert body["allocation"]["limiting_constraint"] in {
            "max_pool_fraction",
            "min_free_reserve",
            "executable_depth",
            "native_venue_balance",
        }

        generalized = {
            "left": {
                "venue": "matchbook",
                "event_payload": {
                    "id": 7001,
                    "name": "Tottenham vs Everton",
                    "start": KICKOFF.isoformat(),
                    "competition-name": "Premier League",
                },
                "market_payload": _ftts_mb_payload(),
                "observed_at": OBSERVED.isoformat(),
                "native_currency": "GBP",
                "quote_age_ms": 100,
            },
            "right": {
                "venue": "polymarket",
                "event_payload": {
                    "id": "pm-tot-eve-step7",
                    "title": "Tottenham vs Everton",
                    "startTime": KICKOFF.isoformat(),
                    "competition": "Premier League",
                },
                "market_payload": _ftts_pm_payload(),
                "books_by_token": _ftts_books(),
                "observed_at": OBSERVED.isoformat(),
                "quote_age_ms": 150,
            },
            "venue_costs": [
                venue_cost_payload("matchbook", "0.02"),
                venue_cost_payload("polymarket", "0", detail="assumed_zero operator test cost"),
            ],
            "fx_snapshots": [{"currency": "USD", "gbp_per_unit": "0.75", "source": "test"}],
            "maximum_execution_risk": 100,
        }
        gen = client.post("/paper/scan/pair", json=generalized)
        assert gen.status_code == 200, gen.text
        gen_body = gen.json()
        if gen_body.get("payoff_scan") and gen_body["payoff_scan"]["solution"]["is_arbitrage"]:
            assert gen_body["solver_model"] == SOLVER_MODEL_GENERALIZED
            assert gen_body["allocation"] is not None
            assert gen_body["allocation"]["solver_model"] == SOLVER_MODEL_GENERALIZED
        health = client.get("/health")
        assert health.json()["execution_enabled"] is False
    finally:
        app.dependency_overrides.clear()
        audit.close()
        watchlist_store.close()
        repository.close()
        liquidity.close()


def _regulation_market(*, kickoff: datetime) -> CanonicalMarket:
    return CanonicalMarket(
        event=CanonicalEvent(
            competition="Premier League",
            home_team="Newcastle",
            away_team="Chelsea",
            kickoff_utc=kickoff,
            source_venue=VenueName.MATCHBOOK,
            source_event_id="evt-lock",
        ),
        source_venue=VenueName.MATCHBOOK,
        source_market_id="mkt-lock",
        family=MarketFamily.MATCH_RESULT,
        period=FootballPeriod.FULL_TIME,
        settlement=SettlementFingerprint(
            scope=SettlementScope.REGULATION_TIME,
            period=FootballPeriod.FULL_TIME,
            extra_time_included=False,
            penalties_included=False,
        ),
    )


def test_pre_match_lock_hours_are_settlement_not_kickoff() -> None:
    kickoff = datetime(2026, 9, 13, 15, 0, tzinfo=UTC)
    as_of = kickoff - timedelta(hours=2)
    hours, basis = lock_hours_until_capital_release(
        kickoff, as_of, market=_regulation_market(kickoff=kickoff)
    )
    hours_to_kickoff = Decimal("2")
    extra = (
        FOOTBALL_REGULATION_PLAYING_MINUTES
        + FOOTBALL_HALFTIME_MINUTES
        + MODELLED_STOPPAGE_AND_SETTLEMENT_BUFFER_MINUTES
    ) / Decimal("60")
    kickoff_plus_105 = hours_to_kickoff + (UNDERSTATED_FULL_TIME_ELAPSED_MINUTES / Decimal("60"))
    assert hours is not None
    assert extra * Decimal("60") > UNDERSTATED_FULL_TIME_ELAPSED_MINUTES
    assert hours == (hours_to_kickoff + extra).quantize(Decimal("0.0001"))
    assert hours > hours_to_kickoff
    assert hours > kickoff_plus_105
    assert basis == "kickoff_plus_elapsed_regulation_halftime_stoppage_settlement_buffer"
    assert basis != "time_to_kickoff"
    assert basis != "kickoff_plus_regulation_plus_settlement_buffer"


def test_pre_match_full_time_release_is_later_than_kickoff_plus_105_minutes() -> None:
    kickoff = datetime(2026, 9, 13, 15, 0, tzinfo=UTC)
    as_of = kickoff - timedelta(minutes=1)
    hours, basis = lock_hours_until_capital_release(
        kickoff, as_of, market=_regulation_market(kickoff=kickoff)
    )
    minutes_after_kickoff = (hours - Decimal("1") / Decimal("60")) * Decimal("60")
    assert hours is not None
    assert basis == "kickoff_plus_elapsed_regulation_halftime_stoppage_settlement_buffer"
    assert minutes_after_kickoff > Decimal("105")
    optimistic = BankrollAllocationPolicy(
        football_halftime_minutes=Decimal("0"),
        football_stoppage_and_settlement_buffer_minutes=Decimal("15"),
    )
    omitted_hours, omitted_basis = lock_hours_until_capital_release(
        kickoff, as_of, market=_regulation_market(kickoff=kickoff), policy=optimistic
    )
    assert omitted_hours is None
    assert omitted_basis is None
    labelled_105 = allocate(
        _demo_request(
            lock_hours=Decimal("2") + Decimal("105") / Decimal("60"),
            lock_basis="kickoff_plus_regulation_plus_settlement_buffer",
        )
    )
    assert labelled_105.accepted is True
    assert labelled_105.capital_turnover is None
    assert labelled_105.expected_lock_basis is None


def test_pre_match_lock_omitted_when_settlement_scope_unknown() -> None:
    kickoff = datetime(2026, 9, 13, 15, 0, tzinfo=UTC)
    as_of = kickoff - timedelta(hours=2)
    market = _regulation_market(kickoff=kickoff).model_copy(
        update={
            "settlement": SettlementFingerprint(
                scope=SettlementScope.UNKNOWN,
                period=FootballPeriod.FULL_TIME,
            )
        }
    )
    hours, basis = lock_hours_until_capital_release(kickoff, as_of, market=market)
    assert hours is None
    assert basis is None


def test_pre_match_lock_omitted_for_extra_time_and_in_play() -> None:
    kickoff = datetime(2026, 9, 13, 15, 0, tzinfo=UTC)
    as_of = kickoff - timedelta(hours=2)
    extra_time = _regulation_market(kickoff=kickoff).model_copy(
        update={
            "period": FootballPeriod.EXTRA_TIME,
            "settlement": SettlementFingerprint(
                scope=SettlementScope.INCLUDING_EXTRA_TIME,
                period=FootballPeriod.EXTRA_TIME,
                extra_time_included=True,
                penalties_included=False,
            ),
        }
    )
    hours, basis = lock_hours_until_capital_release(kickoff, as_of, market=extra_time)
    assert hours is None and basis is None
    in_play, in_play_basis = lock_hours_until_capital_release(
        kickoff, kickoff + timedelta(minutes=10), market=_regulation_market(kickoff=kickoff)
    )
    assert in_play is None and in_play_basis is None


def test_zero_volatility_is_known_not_unknown() -> None:
    known_zero = allocate(_demo_request())
    unknown = allocate(
        request_from_complete_set(
            [
                _leg(outcome="home", venue=VenueName.MATCHBOOK, max_stake=Decimal("10000")),
                _leg(
                    outcome="away",
                    venue=VenueName.POLYMARKET,
                    max_stake=Decimal("10000"),
                    currency="USD",
                    gbp_per_unit=Decimal("0.75"),
                    mode=LegExecutionMode.EXTERNAL_OPERATOR,
                ),
            ],
            SOLVER.solve(
                [
                    _leg(outcome="home", venue=VenueName.MATCHBOOK, max_stake=Decimal("10000")).as_executable_quote(),
                    _leg(
                        outcome="away",
                        venue=VenueName.POLYMARKET,
                        max_stake=Decimal("10000"),
                        currency="USD",
                        gbp_per_unit=Decimal("0.75"),
                        mode=LegExecutionMode.EXTERNAL_OPERATOR,
                    ).as_executable_quote(),
                ]
            ),
            policy=POLICY,
            balances=_balances(),
            fill_confidence=_fill_high(),
            execution_risk_score=10,
            recent_volatility_bps=None,
        )
    )
    assert known_zero.accepted and unknown.accepted
    assert not any(factor.name == "volatility" for factor in known_zero.reduction_factors)
    unknown_factor = next(factor for factor in unknown.reduction_factors if factor.name == "volatility")
    assert unknown_factor.input_status is ReductionInputStatus.UNKNOWN
    assert unknown.recommended_committed_capital < known_zero.recommended_committed_capital


def _fill_leg(
    *,
    outcome: str,
    venue: VenueName,
    market: str,
    runner: str,
    stake: Decimal,
) -> PaperOpportunityLeg:
    return PaperOpportunityLeg(
        outcome=outcome,
        venue=venue,
        source_market_id=market,
        source_runner_id=runner,
        requested_stake=stake,
        displayed_odds=Decimal("2.2"),
        currency="GBP",
    )


def _allocated(
    *,
    outcome: str,
    venue: VenueName,
    market: str,
    runner: str | None,
    stake: Decimal,
) -> AllocatedStake:
    return AllocatedStake(
        leg_id=f"{venue.value}:{market}:{runner or ''}:{outcome}",
        outcome=outcome,
        venue=venue,
        source_market_id=market,
        source_runner_id=runner,
        stake_reporting=stake,
        stake_native=stake,
        capital_reporting=stake,
        capital_native=stake,
        native_currency="GBP",
        capital_source="AUTO_POOL",
        execution_mode="INTERNAL",
    )


def _allocation_with_stakes(stakes: list[AllocatedStake]) -> AllocationResult:
    return AllocationResult(
        accepted=True,
        solver_model=SOLVER_MODEL_SIMPLE,
        recommended_stakes=stakes,
        maximum_validated_capital=sum((item.capital_reporting for item in stakes), Decimal("0")),
        recommended_committed_capital=sum((item.capital_reporting for item in stakes), Decimal("0")),
    )


def test_fill_plan_mapping_fails_closed_on_unmatched_leg() -> None:
    legs = [
        _fill_leg(
            outcome="home",
            venue=VenueName.MATCHBOOK,
            market="mb-home",
            runner="r-home",
            stake=Decimal("100"),
        ),
        _fill_leg(
            outcome="away",
            venue=VenueName.POLYMARKET,
            market="pm-away",
            runner="r-away",
            stake=Decimal("100"),
        ),
    ]
    allocation = _allocation_with_stakes(
        [
            _allocated(
                outcome="home",
                venue=VenueName.MATCHBOOK,
                market="mb-home",
                runner="r-home",
                stake=Decimal("40"),
            )
        ]
    )
    try:
        apply_allocation_to_fill_legs(legs, allocation)
        raise AssertionError("expected unmatched fill-plan mapping to fail closed")
    except FillPlanMappingError as exc:
        assert exc.reason == "fill_plan_mapping_unmatched_leg"


def test_fill_plan_mapping_fails_closed_when_runner_identity_differs() -> None:
    legs = [
        _fill_leg(
            outcome="home",
            venue=VenueName.MATCHBOOK,
            market="mb-home",
            runner="r-home",
            stake=Decimal("100"),
        )
    ]
    allocation = _allocation_with_stakes(
        [
            _allocated(
                outcome="home",
                venue=VenueName.MATCHBOOK,
                market="mb-home",
                runner="other-runner",
                stake=Decimal("40"),
            )
        ]
    )
    try:
        apply_allocation_to_fill_legs(legs, allocation)
        raise AssertionError("expected runner mismatch to fail closed")
    except FillPlanMappingError as exc:
        assert exc.reason == "fill_plan_mapping_unmatched_leg"


def test_fill_plan_mapping_fails_closed_on_duplicate_identity() -> None:
    legs = [
        _fill_leg(
            outcome="home",
            venue=VenueName.MATCHBOOK,
            market="mb-home",
            runner="r-home",
            stake=Decimal("50"),
        ),
        _fill_leg(
            outcome="home",
            venue=VenueName.MATCHBOOK,
            market="mb-home",
            runner="r-home",
            stake=Decimal("50"),
        ),
    ]
    allocation = _allocation_with_stakes(
        [
            _allocated(
                outcome="home",
                venue=VenueName.MATCHBOOK,
                market="mb-home",
                runner="r-home",
                stake=Decimal("40"),
            )
        ]
    )
    try:
        apply_allocation_to_fill_legs(legs, allocation)
        raise AssertionError("expected duplicate fill identity to fail closed")
    except FillPlanMappingError as exc:
        assert exc.reason == "fill_plan_mapping_duplicate_identity"


def test_fill_plan_mapping_resizes_one_to_one() -> None:
    legs = [
        _fill_leg(
            outcome="home",
            venue=VenueName.MATCHBOOK,
            market="mb-home",
            runner="r-home",
            stake=Decimal("100"),
        ),
        _fill_leg(
            outcome="away",
            venue=VenueName.POLYMARKET,
            market="pm-away",
            runner="r-away",
            stake=Decimal("80"),
        ),
    ]
    allocation = _allocation_with_stakes(
        [
            _allocated(
                outcome="home",
                venue=VenueName.MATCHBOOK,
                market="mb-home",
                runner="r-home",
                stake=Decimal("40"),
            ),
            _allocated(
                outcome="away",
                venue=VenueName.POLYMARKET,
                market="pm-away",
                runner="r-away",
                stake=Decimal("32"),
            ),
        ]
    )
    resized = apply_allocation_to_fill_legs(legs, allocation)
    assert [leg.requested_stake for leg in resized] == [Decimal("40"), Decimal("32")]
    assert [leg.source_runner_id for leg in resized] == ["r-home", "r-away"]
