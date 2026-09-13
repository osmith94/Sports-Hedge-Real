from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from sports_hedge.application.executable_liquidity import (
    DEFAULT_OPENING_MAX_QUOTE_AGE_MS,
    FixtureHeadlineCandidate,
    HeadlineBand,
    LiquidityRole,
    NO_EXECUTABLE_ARB,
    PASSIVE_MAKER_NOT_EXECUTABLE,
    STALE_PASSIVE_LIQUIDITY,
    best_arb_market_label,
    candidate_from_decision,
    headline_band_for,
    opening_liquidity_rejection_reasons,
    quote_freshness_from_age,
    select_fixture_headline,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.arbitrage.depth import DepthScanResult
from sports_hedge.arbitrage.models import ArbitrageSolution, ArbitrageStake
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import MarketAction, OrderRole
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision
from venue_cost_helpers import matchbook_polymarket_costs, profit_commission_cost

from test_paper_scan_pipeline import matchbook_payloads, polymarket_payloads
from sports_hedge.application.market_observation import (
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)


OBSERVED = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)
TRIGGER = Decimal("0.005")


def _candidate(
    *,
    family: str = "match_result",
    selection: str = "away",
    edge: str,
    eligible: bool,
    arb: bool,
    reasons: list[str] | None = None,
    role: LiquidityRole = LiquidityRole.TAKER,
    quote_age_ms: int | None = 120,
    line: Decimal | None = None,
) -> FixtureHeadlineCandidate:
    return FixtureHeadlineCandidate(
        family=family,
        line=line,
        selection=selection,
        current_net_edge=Decimal(edge),
        trigger_net_edge=TRIGGER,
        eligible_for_paper_simulation=eligible,
        solver_is_arbitrage=arb,
        rejection_reasons=list(reasons or []),
        liquidity_role=role,
        quote_age_ms=quote_age_ms,
    )


def _scan_decision(
    *,
    edge: Decimal,
    eligible: bool,
    reasons: list[str],
    quote_age_ms: int | None = 120,
    venue_costs=None,
) -> PaperScanDecision:
    return PaperScanDecision(
        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=[]),
        eligible_for_paper_simulation=eligible,
        rejection_reasons=reasons,
        quote_age_ms=quote_age_ms,
        minimum_net_edge=TRIGGER,
        venue_costs=list(venue_costs or matchbook_polymarket_costs()),
        depth_scan=DepthScanResult(
            solution=ArbitrageSolution(
                is_arbitrage=True,
                implied_probability_sum=Decimal("1") / (Decimal("1") + edge),
                total_stake=Decimal("100"),
                guaranteed_return=Decimal("101"),
                guaranteed_profit=Decimal("1"),
                roi=edge,
                stakes=[
                    ArbitrageStake(
                        outcome="away",
                        venue=VenueName.MATCHBOOK,
                        source_market_id="mb",
                        stake=Decimal("50"),
                        net_decimal_odds=Decimal("2.10"),
                        state_return=Decimal("105"),
                    ),
                    ArbitrageStake(
                        outcome="home",
                        venue=VenueName.POLYMARKET,
                        source_market_id="pm",
                        stake=Decimal("50"),
                        net_decimal_odds=Decimal("2.05"),
                        state_return=Decimal("102.5"),
                    ),
                ],
            )
        ),
    )


def test_unchanged_price_with_fresh_timestamp_is_not_stale() -> None:
    assert quote_freshness_from_age(quote_age_ms=80, max_quote_age_ms=2000) is True
    assert quote_freshness_from_age(quote_age_ms=2000, max_quote_age_ms=2000) is False
    assert quote_freshness_from_age(quote_age_ms=None, max_quote_age_ms=2000) is False


def test_stale_unrevalidated_quote_is_never_executable_evidence() -> None:
    reasons = opening_liquidity_rejection_reasons(
        matchbook_polymarket_costs(),
        quote_age_ms=8_000,
        max_quote_age_ms=DEFAULT_OPENING_MAX_QUOTE_AGE_MS,
    )
    assert "stale_quote" in reasons
    candidate = _candidate(
        edge="0.08",
        eligible=False,
        arb=True,
        reasons=reasons,
        quote_age_ms=8_000,
    )
    assert headline_band_for(candidate) is HeadlineBand.OBSERVED_NOT_EXECUTABLE


def test_thin_high_edge_cannot_headline_over_deeper_executable_market() -> None:
    thin = _candidate(
        family="both_teams_to_score",
        selection="yes",
        edge="0.09",
        eligible=False,
        arb=True,
        reasons=["missing_executable_outcome_depth"],
    )
    deep = _candidate(
        family="match_result",
        selection="away",
        edge="0.012",
        eligible=True,
        arb=True,
    )
    headline = select_fixture_headline([thin, deep])
    assert headline.band is HeadlineBand.QUALIFYING
    assert headline.candidate is deep
    assert headline.best_arb_market == "Match Result · Away"
    assert headline_band_for(thin) is HeadlineBand.OBSERVED_NOT_EXECUTABLE


def test_stale_passive_high_edge_cannot_outrank_fresh_taker() -> None:
    maker_costs = [
        profit_commission_cost(
            VenueName.MATCHBOOK,
            Decimal("0.02"),
            captured_at=OBSERVED,
        ).model_copy(update={"order_role": OrderRole.MAKER}),
        profit_commission_cost(
            VenueName.POLYMARKET,
            Decimal("0"),
            captured_at=OBSERVED,
        ),
    ]
    passive_reasons = opening_liquidity_rejection_reasons(
        maker_costs,
        quote_age_ms=9_000,
        max_quote_age_ms=DEFAULT_OPENING_MAX_QUOTE_AGE_MS,
    )
    assert PASSIVE_MAKER_NOT_EXECUTABLE in passive_reasons
    assert STALE_PASSIVE_LIQUIDITY in passive_reasons
    stale_maker = candidate_from_decision(
        _scan_decision(
            edge=Decimal("0.11"),
            eligible=False,
            reasons=passive_reasons,
            quote_age_ms=9_000,
            venue_costs=maker_costs,
        ),
        family="match_result",
        selection="away",
    )
    fresh_taker = candidate_from_decision(
        _scan_decision(
            edge=Decimal("0.014"),
            eligible=True,
            reasons=[],
            quote_age_ms=90,
        ),
        family="both_teams_to_score",
        selection="yes",
    )
    headline = select_fixture_headline([stale_maker, fresh_taker])
    assert stale_maker.liquidity_role is LiquidityRole.MAKER
    assert headline_band_for(stale_maker) is HeadlineBand.OBSERVED_NOT_EXECUTABLE
    assert headline.band is HeadlineBand.QUALIFYING
    assert headline.candidate is not None
    assert headline.candidate.family == "both_teams_to_score"
    assert headline.best_arb_market == "BTTS · Yes"
    assert headline.candidate.current_net_edge == Decimal("0.014")


def test_only_non_executable_edges_yield_no_executable_arb() -> None:
    thin = _candidate(
        edge="0.20",
        eligible=False,
        arb=True,
        reasons=["missing_executable_outcome_depth"],
    )
    stale = _candidate(
        edge="0.15",
        eligible=False,
        arb=True,
        reasons=["stale_quote"],
        quote_age_ms=5_000,
    )
    maker = _candidate(
        edge="0.18",
        eligible=False,
        arb=True,
        reasons=[PASSIVE_MAKER_NOT_EXECUTABLE],
        role=LiquidityRole.MAKER,
    )
    headline = select_fixture_headline([thin, stale, maker])
    assert headline.band is HeadlineBand.NO_EXECUTABLE_ARB
    assert headline.candidate is None
    assert headline.reason in {NO_EXECUTABLE_ARB, "missing_executable_outcome_depth"}


def test_near_executable_can_headline_when_nothing_qualifies() -> None:
    near = _candidate(
        family="total_goals",
        selection="over",
        line=Decimal("2.5"),
        edge="0.003",
        eligible=False,
        arb=False,
        reasons=["net_edge_below_threshold"],
    )
    thin = _candidate(
        edge="0.08",
        eligible=False,
        arb=True,
        reasons=["execution_risk_above_threshold"],
    )
    headline = select_fixture_headline([thin, near])
    assert headline.band is HeadlineBand.NEAR_EXECUTABLE
    assert headline.best_arb_market == "Total Goals · Over 2.5"


def test_best_arb_market_labels() -> None:
    assert best_arb_market_label("match_result", selection="away") == "Match Result · Away"
    assert best_arb_market_label("both_teams_to_score", selection="yes") == "BTTS · Yes"
    assert (
        best_arb_market_label("total_goals", selection="over", line=Decimal("2.5"))
        == "Total Goals · Over 2.5"
    )
    assert (
        best_arb_market_label("first_team_to_score", selection="home")
        == "First Team To Score · Home"
    )


def test_paper_scan_rejects_maker_opening_role() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    service = PaperScanService(intelligence)
    mb_event, mb_market = matchbook_payloads()
    pm_event, pm_market, pm_books = polymarket_payloads()
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=OBSERVED, quote_age_ms=120
    )
    polymarket = PolymarketObservationBuilder().build(
        pm_event, pm_market, pm_books, observed_at=OBSERVED, quote_age_ms=180
    )
    captured = datetime.now(UTC)
    costs = [
        profit_commission_cost(
            VenueName.MATCHBOOK,
            Decimal("0.02"),
            captured_at=captured,
        ).model_copy(update={"order_role": OrderRole.MAKER}),
        profit_commission_cost(
            VenueName.POLYMARKET,
            Decimal("0"),
            captured_at=captured,
        ),
    ]
    try:
        decision = service.scan_pair(
            matchbook,
            polymarket,
            venue_costs=costs,
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test_fx")],
            maximum_execution_risk=100,
        )
        assert decision.eligible_for_paper_simulation is False
        assert PASSIVE_MAKER_NOT_EXECUTABLE in decision.rejection_reasons
        candidate = candidate_from_decision(decision, family="both_teams_to_score")
        assert headline_band_for(candidate) is HeadlineBand.OBSERVED_NOT_EXECUTABLE
    finally:
        repository.close()


def test_opening_lay_cost_is_not_taker_liquidity() -> None:
    from sports_hedge.application.executable_liquidity import liquidity_role_from_costs

    costs = [
        profit_commission_cost(
            VenueName.MATCHBOOK,
            Decimal("0.02"),
            captured_at=OBSERVED,
        ).model_copy(update={"action": MarketAction.LAY}),
        profit_commission_cost(
            VenueName.POLYMARKET,
            Decimal("0"),
            captured_at=OBSERVED,
        ),
    ]
    reasons = opening_liquidity_rejection_reasons(costs)
    assert "opening_lay_not_supported" in reasons
    assert liquidity_role_from_costs(costs) is LiquidityRole.MAKER
