from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sports_hedge.arbitrage.depth import DepthQuoteCandidate, DepthScanResult
from sports_hedge.arbitrage.models import ArbitrageSolution, ArbitrageStake
from sports_hedge.arbitrage.watchlist.adapter import observation_from_paper_decision
from sports_hedge.arbitrage.watchlist.economics import distance_to_trigger_pp
from sports_hedge.arbitrage.watchlist.models import (
    LifecycleEventType,
    OpportunityStatus,
    WatchLeg,
    WatchObservation,
)
from sports_hedge.arbitrage.watchlist.ranking import rank_near_opportunities
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.models import MarketSnapshot
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision

TRIGGER = Decimal("0.01")
EDGE_080 = Decimal("0.008")
OBSERVED = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)


def _legs(
    *, gbp_stake_home: Decimal = Decimal("50"), gbp_stake_away: Decimal = Decimal("40")
) -> list[WatchLeg]:
    return [
        WatchLeg(
            outcome="home",
            venue=VenueName.MATCHBOOK,
            source_market_id="mb-1",
            currency="GBP",
            native_stake=gbp_stake_home,
            gbp_per_unit=Decimal("1"),
            gbp_stake=gbp_stake_home,
            net_decimal_odds=Decimal("2.10"),
            cumulative_depth_gbp=Decimal("80"),
        ),
        WatchLeg(
            outcome="away",
            venue=VenueName.POLYMARKET,
            source_market_id="pm-1",
            currency="USD",
            native_stake=gbp_stake_away / Decimal("0.75"),
            gbp_per_unit=Decimal("0.75"),
            gbp_stake=gbp_stake_away,
            net_decimal_odds=Decimal("2.05"),
            cumulative_depth_gbp=Decimal("60"),
        ),
    ]


def _observation(
    *,
    market_id: str = "mkt-newcastle-chelsea-btts",
    edge: Decimal | None = EDGE_080,
    trigger: Decimal = TRIGGER,
    quote_age_ms: int | None = 120,
    eligible: bool = False,
    solver_is_arbitrage: bool = True,
    rejection_reasons: list[str] | None = None,
    observed_at: datetime = OBSERVED,
    competition: str = "Premier League",
    market_family: MarketFamily = MarketFamily.BOTH_TEAMS_TO_SCORE,
    limiting_depth: Decimal = Decimal("60"),
    guaranteed_profit_gbp: Decimal | None = None,
) -> WatchObservation:
    implied = None
    if edge is not None:
        implied = Decimal("1") / (Decimal("1") + edge)
    return WatchObservation(
        observed_at=observed_at,
        canonical_event_id="evt-newcastle-chelsea",
        canonical_market_id=market_id,
        settlement_key="regulation_time|full_time",
        competition=competition,
        home_team="Newcastle United",
        away_team="Chelsea",
        market_family=market_family,
        period=FootballPeriod.FULL_TIME,
        legs=_legs(),
        trigger_net_edge=trigger,
        current_net_edge=edge,
        implied_probability_sum=implied,
        solver_is_arbitrage=solver_is_arbitrage,
        eligible_for_paper_simulation=eligible,
        rejection_reasons=list(rejection_reasons or []),
        execution_risk_score=18,
        quote_age_ms=quote_age_ms,
        limiting_depth_gbp=limiting_depth,
        limiting_leg_outcome="away",
        capital_required_gbp=Decimal("90"),
        guaranteed_profit_gbp=guaranteed_profit_gbp,
        expected_lock_minutes=Decimal("120"),
        kickoff_utc=datetime(2026, 9, 20, 15, 0, tzinfo=UTC),
    )


def test_080_candidate_against_100_trigger_is_020pp_away() -> None:
    distance = distance_to_trigger_pp(EDGE_080, TRIGGER)
    assert distance == Decimal("0.2000")
    service = WatchlistService(SqliteWatchlistRepository())
    opportunity = service.observe(_observation(rejection_reasons=["net_edge_below_threshold"]))
    assert opportunity.distance_to_trigger_pp == Decimal("0.2000")
    assert opportunity.status == OpportunityStatus.APPROACHING
    assert opportunity.classification.value == "near_opportunity"
    assert opportunity.is_arbitrage is False
    assert opportunity.guaranteed_profit_gbp is None


def test_below_threshold_candidate_never_receives_triggered_status() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    opportunity = service.observe(
        _observation(
            edge=EDGE_080,
            eligible=True,
            solver_is_arbitrage=True,
            rejection_reasons=["net_edge_below_threshold"],
        )
    )
    assert opportunity.current_net_edge == EDGE_080
    assert opportunity.trigger_net_edge == TRIGGER
    assert opportunity.status != OpportunityStatus.TRIGGERED
    assert opportunity.is_arbitrage is False
    assert "triggered" not in opportunity.classification.value


def test_threshold_crossing_creates_lifecycle_event() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    first = service.observe(
        _observation(observed_at=OBSERVED, rejection_reasons=["net_edge_below_threshold"])
    )
    crossed = service.observe(
        _observation(
            edge=Decimal("0.012"),
            eligible=True,
            solver_is_arbitrage=True,
            observed_at=OBSERVED + timedelta(seconds=15),
            guaranteed_profit_gbp=Decimal("1.23"),
        )
    )
    events = service.activity(opportunity_id=first.opportunity_id)
    types = [event.event_type for event in events]
    assert LifecycleEventType.CANDIDATE_FIRST_SEEN in types
    assert LifecycleEventType.TRIGGER_CROSSED in types
    assert crossed.status == OpportunityStatus.TRIGGERED
    assert crossed.is_arbitrage is True
    assert crossed.guaranteed_profit_gbp == Decimal("1.23")
    assert crossed.capital_required_gbp * crossed.current_net_edge != Decimal("1.23")
    original_first_seen = events[-1]
    service.observe(
        _observation(
            edge=Decimal("0.011"),
            eligible=True,
            observed_at=OBSERVED + timedelta(seconds=30),
        )
    )
    replay = service.activity(opportunity_id=first.opportunity_id)
    assert replay[-1].event_id == original_first_seen.event_id
    assert replay[-1].event_type == LifecycleEventType.CANDIDATE_FIRST_SEEN


def test_stale_quote_is_rejected_and_flagged() -> None:
    service = WatchlistService(SqliteWatchlistRepository(), max_quote_age_ms=1000)
    opportunity = service.observe(_observation(quote_age_ms=1500, edge=EDGE_080))
    assert opportunity.status == OpportunityStatus.REJECTED
    assert "stale_quote" in opportunity.rejection_reasons
    types = [event.event_type for event in service.activity()]
    assert LifecycleEventType.REJECTED_STALE_QUOTE in types


def test_missing_costs_fail_closed() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    opportunity = service.observe(
        _observation(
            edge=Decimal("0.02"),
            eligible=True,
            solver_is_arbitrage=True,
            rejection_reasons=["missing_fx_rate:USD", "missing_fee_snapshot:matchbook"],
        )
    )
    assert opportunity.status == OpportunityStatus.REJECTED
    assert opportunity.is_arbitrage is False
    assert opportunity.guaranteed_profit_gbp is None
    types = [event.event_type for event in service.activity()]
    assert LifecycleEventType.REJECTED_MISSING_COSTS in types


def test_multi_currency_candidate_never_sums_native_currencies_directly() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    observation = _observation()
    native_sum = sum((leg.native_stake or Decimal("0") for leg in observation.legs), Decimal("0"))
    gbp_sum = sum((leg.gbp_stake or Decimal("0") for leg in observation.legs), Decimal("0"))
    assert {leg.currency for leg in observation.legs} == {"GBP", "USD"}
    assert native_sum != gbp_sum
    opportunity = service.observe(observation)
    assert opportunity.capital_required_gbp == Decimal("90")
    assert opportunity.capital_required_gbp == gbp_sum
    assert opportunity.capital_required_gbp != native_sum
    usd_leg = next(leg for leg in opportunity.legs if leg.currency == "USD")
    assert usd_leg.gbp_per_unit == Decimal("0.75")
    assert usd_leg.native_stake != usd_leg.gbp_stake


def test_top_n_ranking_is_stable() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    service.observe(
        _observation(
            market_id="mkt-b",
            edge=Decimal("0.008"),
            quote_age_ms=200,
            limiting_depth=Decimal("40"),
            observed_at=OBSERVED,
        )
    )
    service.observe(
        _observation(
            market_id="mkt-a",
            edge=Decimal("0.008"),
            quote_age_ms=100,
            limiting_depth=Decimal("40"),
            observed_at=OBSERVED + timedelta(seconds=1),
        )
    )
    service.observe(
        _observation(
            market_id="mkt-c",
            edge=Decimal("0.009"),
            quote_age_ms=400,
            limiting_depth=Decimal("10"),
            observed_at=OBSERVED + timedelta(seconds=2),
        )
    )
    service.observe(
        _observation(
            market_id="mkt-triggered",
            edge=Decimal("0.015"),
            eligible=True,
            quote_age_ms=50,
            observed_at=OBSERVED + timedelta(seconds=3),
            guaranteed_profit_gbp=Decimal("2.50"),
        )
    )
    first = [item.canonical_market_id for item in service.top_near(limit=2)]
    second = [item.canonical_market_id for item in service.top_near(limit=2)]
    assert first == second == ["mkt-c", "mkt-a"]
    ranked = rank_near_opportunities(service.repository.list_opportunities(), limit=3)
    assert [item.canonical_market_id for item in ranked] == ["mkt-c", "mkt-a", "mkt-b"]
    triggered = service.triggered(limit=10)
    assert [item.canonical_market_id for item in triggered] == ["mkt-triggered"]
    assert all(item.status != OpportunityStatus.TRIGGERED for item in service.top_near(limit=10))


def test_filters_and_paper_fill_lifecycle_stay_paper_only() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    approaching = service.observe(_observation(market_id="mkt-pl"))
    service.observe(
        _observation(
            market_id="mkt-laliga",
            competition="La Liga",
            market_family=MarketFamily.MATCH_RESULT,
            edge=Decimal("0.007"),
        )
    )
    filtered = service.top_near(
        limit=10,
        competition="Premier League",
        venue=VenueName.MATCHBOOK,
        market_family=MarketFamily.BOTH_TEAMS_TO_SCORE,
    )
    assert [item.canonical_market_id for item in filtered] == ["mkt-pl"]
    triggered = service.observe(
        _observation(
            market_id="mkt-pl",
            edge=Decimal("0.012"),
            eligible=True,
            observed_at=OBSERVED + timedelta(seconds=5),
            guaranteed_profit_gbp=Decimal("1.10"),
        )
    )
    filling = service.record_paper_fill(
        triggered.opportunity_id,
        stage=OpportunityStatus.PAPER_FILLING,
        occurred_at=OBSERVED + timedelta(seconds=6),
        detail="paper_mode_only",
    )
    assert filling.status == OpportunityStatus.PAPER_FILLING
    types = [
        event.event_type for event in service.activity(opportunity_id=approaching.opportunity_id)
    ]
    assert LifecycleEventType.PAPER_FILL_ATTEMPTED in types
    assert LifecycleEventType.TRIGGER_CROSSED in types


def test_unknown_quote_age_fails_closed_and_is_not_near() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    opportunity = service.observe(_observation(quote_age_ms=None, edge=EDGE_080))
    assert opportunity.status == OpportunityStatus.REJECTED
    assert "unknown_quote_age" in opportunity.rejection_reasons
    assert opportunity.classification.value != "near_opportunity"
    assert service.top_near(limit=10) == []
    details = [event.detail for event in service.activity()]
    assert "unknown_quote_age" in details


def test_missing_depth_is_rejected_not_classified_near() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    opportunity = service.observe(
        _observation(
            rejection_reasons=["missing_executable_outcome_depth"],
            solver_is_arbitrage=False,
        )
    )
    assert opportunity.status == OpportunityStatus.REJECTED
    assert opportunity.classification.value != "near_opportunity"
    assert service.top_near(limit=10) == []


def test_unknown_venue_currency_is_rejected_without_inventing_gbp() -> None:

    decision = PaperScanDecision(
        canonical_event_id="evt-1",
        canonical_market_id="mkt-currency",
        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=[]),
        quote_age_ms=120,
        minimum_net_edge=TRIGGER,
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
        depth_scan=DepthScanResult(
            solution=ArbitrageSolution(
                is_arbitrage=True,
                implied_probability_sum=Decimal("0.992"),
                total_stake=Decimal("90"),
                guaranteed_return=Decimal("91.23"),
                guaranteed_profit=Decimal("1.23"),
                roi=EDGE_080,
                stakes=[
                    ArbitrageStake(
                        outcome="yes",
                        venue=VenueName.MATCHBOOK,
                        source_market_id="mb",
                        stake=Decimal("50"),
                        net_decimal_odds=Decimal("2.1"),
                        state_return=Decimal("105"),
                    ),
                    ArbitrageStake(
                        outcome="no",
                        venue=VenueName.POLYMARKET,
                        source_market_id="pm",
                        stake=Decimal("40"),
                        net_decimal_odds=Decimal("2.05"),
                        state_return=Decimal("82"),
                    ),
                ],
            ),
            selected_quotes=[
                DepthQuoteCandidate(
                    outcome="yes",
                    venue=VenueName.MATCHBOOK,
                    source_market_id="mb",
                    source_runner_id="y",
                    gross_weighted_odds=Decimal("2.12"),
                    net_decimal_odds=Decimal("2.10"),
                    cumulative_depth=Decimal("80"),
                    levels_consumed=1,
                ),
                DepthQuoteCandidate(
                    outcome="no",
                    venue=VenueName.POLYMARKET,
                    source_market_id="pm",
                    source_runner_id="n",
                    gross_weighted_odds=Decimal("2.05"),
                    net_decimal_odds=Decimal("2.05"),
                    cumulative_depth=Decimal("60"),
                    levels_consumed=1,
                ),
            ],
        ),
    )
    mapped = observation_from_paper_decision(decision, history=())
    assert mapped is not None
    assert mapped.quote_age_ms == 120
    assert "unknown_venue_currency:matchbook" in mapped.rejection_reasons
    assert "unknown_venue_currency:polymarket" in mapped.rejection_reasons
    assert mapped.legs == []
    opportunity = WatchlistService(SqliteWatchlistRepository()).observe(mapped)
    assert opportunity.status == OpportunityStatus.REJECTED

    history = [
        MarketSnapshot(
            observed_at=OBSERVED,
            venue=VenueName.MATCHBOOK,
            canonical_event_id="evt-1",
            canonical_market_id="mkt-currency",
            canonical_outcome="yes",
            market_family=MarketFamily.BOTH_TEAMS_TO_SCORE,
            decimal_odds=Decimal("2.1"),
            metadata={"native_currency": "GBP"},
        ),
        MarketSnapshot(
            observed_at=OBSERVED,
            venue=VenueName.POLYMARKET,
            canonical_event_id="evt-1",
            canonical_market_id="mkt-currency",
            canonical_outcome="no",
            market_family=MarketFamily.BOTH_TEAMS_TO_SCORE,
            decimal_odds=Decimal("2.05"),
            metadata={"native_currency": "USD"},
        ),
    ]
    provenanced = observation_from_paper_decision(decision, history)
    assert provenanced is not None
    assert {leg.currency for leg in provenanced.legs} == {"GBP", "USD"}
    assert provenanced.guaranteed_profit_gbp == Decimal("1.23")
    assert "unknown_venue_currency:matchbook" not in provenanced.rejection_reasons


def test_solver_guaranteed_profit_is_not_replaced_by_edge_times_capital() -> None:
    service = WatchlistService(SqliteWatchlistRepository())
    opportunity = service.observe(
        _observation(
            edge=Decimal("0.012"),
            eligible=True,
            guaranteed_profit_gbp=Decimal("1.23"),
        )
    )
    assert opportunity.status == OpportunityStatus.TRIGGERED
    assert opportunity.guaranteed_profit_gbp == Decimal("1.23")
    assert opportunity.capital_required_gbp == Decimal("90")
    assert opportunity.guaranteed_profit_gbp != Decimal("90") * Decimal("0.012")


def test_adapter_preserves_unknown_quote_age_instead_of_zero() -> None:

    decision = PaperScanDecision(
        canonical_event_id="evt-1",
        canonical_market_id="mkt-age",
        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=[]),
        quote_age_ms=None,
        minimum_net_edge=TRIGGER,
    )
    mapped = observation_from_paper_decision(decision, history=())
    assert mapped is not None
    assert mapped.quote_age_ms is None
    opportunity = WatchlistService(SqliteWatchlistRepository()).observe(mapped)
    assert opportunity.status == OpportunityStatus.REJECTED
    assert "unknown_quote_age" in opportunity.rejection_reasons
