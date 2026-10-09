"""Issue #29: optional heuristic risk evidence is not a paper Price-1/Price-2 veto.

Deterministic fixture/demo MB↔PM books. Not live venue quotes. Execution stays
disarmed. Data class: modelled paper-scan payloads.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sports_hedge.application.complete_set import SOLVER_MODEL_SIMPLE, generalized_state_model
from sports_hedge.application.current_market_inventory import (
    PROMOTION_FAIL_CLOSED_REASONS,
    stored_row_proves_qualifying_executable,
)
from sports_hedge.application.execution_reprice import (
    EXECUTION_REPRICE_SKEW,
    capture_with_execution_reprice,
    execution_entry_block,
    execution_reprice_permitted,
)
from sports_hedge.application.executable_liquidity import (
    HARD_NON_EXECUTABLE_REASONS,
    FixtureHeadlineCandidate,
    HeadlineBand,
    LiquidityRole,
    decision_is_solver_arbitrage,
    decision_net_edge,
    headline_band_for,
)
from sports_hedge.application.market_observation import (
    KalshiObservationBuilder,
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)
from sports_hedge.application.paper_scan import PaperScanService, _paper_blocking_reasons
from sports_hedge.application.price_engine import CataloguePriceEngine
from sports_hedge.arbitrage.allocation.engine import allocate
from sports_hedge.arbitrage.models import PayoffSolution
from sports_hedge.arbitrage.payoff_scan import PayoffScanResult
from sports_hedge.arbitrage.priority_alerts.qualification import qualify_priority_alert
from sports_hedge.arbitrage.watchlist.economics import (
    INFORMATIONAL_NONBLOCKING_REASONS,
    NET_PROXIMITY_BAND_PP,
    classify_status,
)
from sports_hedge.arbitrage.watchlist.models import OpportunityStatus
from sports_hedge.config import Settings
from sports_hedge.domain.football import MarketFamily
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.venues import KalshiClient, MatchbookClient, PolymarketClient
from test_issue200_universe_hot_promotion import _market_row
from test_near_arbitrage_watchlist import _observation
from test_placement_thresholds import _request
from test_execution_reprice_before_paper_entry import (
    _book,
    _close,
    _decision,
    _fresh,
    _market,
    _open_trade,
    _run,
)
from test_paper_scan_pipeline import kalshi_btts_payloads, matchbook_payloads
from test_step7_safe_market_expansion import OBSERVED
from venue_cost_helpers import matchbook_kalshi_costs, matchbook_polymarket_costs


KICKOFF = datetime.now(UTC) + timedelta(days=1)
NOW = datetime.now(UTC)
BVB_MB_EVENT = {
    "id": 88029,
    "name": "Borussia Dortmund vs Werder Bremen",
    "start": KICKOFF.isoformat(),
    "competition-name": "Bundesliga",
}
BVB_PM_EVENT = {
    "id": "pm-bvb-bre-29",
    "title": "Borussia Dortmund vs Werder Bremen",
    "startTime": KICKOFF.isoformat(),
    "competition": "Bundesliga",
}


def _fx() -> list[FxRateSnapshot]:
    return [
        FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test_fx"),
        FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1"), source="functional_currency"),
    ]


def _half_line_totals_payloads() -> tuple[dict, dict, dict[str, dict]]:
    mb_market = {
        "id": 84225,
        "name": "Over/Under 2.5 Goals",
        "runners": [
            {
                "id": 1,
                "name": "Over 2.5",
                "prices": [
                    {"side": "back", "odds": "1.90", "available-amount": "400"},
                    {"side": "lay", "odds": "1.92", "available-amount": "400"},
                ],
            },
            {
                "id": 2,
                "name": "Under 2.5",
                "prices": [
                    {"side": "back", "odds": "2.40", "available-amount": "400"},
                    {"side": "lay", "odds": "2.42", "available-amount": "400"},
                ],
            },
        ],
    }
    pm_market = {
        "id": "pm-tg-25-29",
        "question": "Total goals 2.5",
        "sportsMarketType": "total goals",
        "line": "2.5",
        "outcomes": '["Over", "Under"]',
        "clobTokenIds": '["o25", "u25"]',
        "description": "Resolves based on 90 minutes of regulation time.",
    }
    books = {
        "o25": {
            "asset_id": "o25",
            "asks": [{"price": "0.20", "size": "500"}],
            "bids": [{"price": "0.18", "size": "500"}],
        },
        "u25": {
            "asset_id": "u25",
            "asks": [{"price": "0.55", "size": "500"}],
            "bids": [{"price": "0.53", "size": "500"}],
        },
    }
    return mb_market, pm_market, books


class OptionalRiskUnavailableScan(PaperScanService):
    """Keep matcher/solver/fees/depth intact. Treat optional score inputs as absent."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.computed_risk_inputs = None

    def _risk_inputs(self, *args: object, **kwargs: object):
        computed = super()._risk_inputs(*args, **kwargs)
        self.computed_risk_inputs = computed
        return None


def _scan_pair(service: PaperScanService, *, venue_costs=None, **kwargs):
    mb_market, pm_market, books = _half_line_totals_payloads()
    matchbook = MatchbookObservationBuilder().build(
        BVB_MB_EVENT, mb_market, observed_at=NOW, quote_age_ms=120
    )
    polymarket = PolymarketObservationBuilder().build(
        BVB_PM_EVENT, pm_market, books, observed_at=NOW, quote_age_ms=150
    )
    decision = service.scan_pair(
        matchbook,
        polymarket,
        venue_costs=venue_costs if venue_costs is not None else matchbook_polymarket_costs(),
        fx_snapshots=_fx(),
        **kwargs,
    )
    return decision, matchbook, polymarket


def test_complete_mb_pm_totals_hedge_is_solver_valid_and_scores_optional_risk() -> None:
    repository = SqliteMarketIntelligenceRepository()
    service = PaperScanService(MarketIntelligenceService(repository))
    try:
        decision, matchbook, polymarket = _scan_pair(service)
    finally:
        repository.close()
    assert matchbook.market.family is MarketFamily.TOTAL_GOALS
    assert polymarket.market.family is MarketFamily.TOTAL_GOALS
    assert generalized_state_model(matchbook.market) is None
    assert decision.solver_model == SOLVER_MODEL_SIMPLE
    assert decision.market_match.matched is True
    assert decision_is_solver_arbitrage(decision) is True
    assert decision.eligible_for_paper_simulation is True
    assert decision.rejection_reasons == []
    assert decision.execution_risk is not None
    assert decision.execution_risk_inputs is not None
    assert "missing_risk_evidence" not in decision.rejection_reasons
    assert execution_reprice_permitted(decision) is True
    assert execution_entry_block(decision) is None
    assert Settings().sports_hedge_execution_enabled is False
    assert MatchbookClient.capabilities.execution_enabled is False
    assert PolymarketClient.capabilities.execution_enabled is False
    assert KalshiClient.capabilities.execution_enabled is False


def test_optional_risk_inputs_absent_is_the_only_price1_veto_on_a_solver_valid_totals_hedge() -> None:
    """Red before the patch: optional score evidence None vetoes Price-2.

    Integer-line TOTAL_GOALS is not register-admitted, so this uses the
    registered half-line totals complete-set path with real-shaped MB↔PM books.
    Matcher, solver, fees, FX, depth and quote age still run. Only `_risk_inputs`
    is forced absent after it would have scored.
    """

    repository = SqliteMarketIntelligenceRepository()
    service = OptionalRiskUnavailableScan(MarketIntelligenceService(repository))
    try:
        decision, _matchbook, _polymarket = _scan_pair(service)
    finally:
        repository.close()
    assert decision_is_solver_arbitrage(decision) is True
    assert service.computed_risk_inputs is not None
    assert service.computed_risk_inputs.leg_count >= 2
    assert decision.execution_risk is None
    assert decision.execution_risk_inputs is None
    assert "missing_risk_evidence" not in decision.rejection_reasons
    assert decision.rejection_reasons == []
    assert decision.eligible_for_paper_simulation is True
    assert execution_reprice_permitted(decision) is True
    assert execution_entry_block(decision) is None


def test_execution_reprice_permitted_when_missing_risk_evidence_is_the_only_label() -> None:
    blocked = _decision(
        eligible_for_paper_simulation=True,
        rejection_reasons=["missing_risk_evidence"],
        payoff_scan=PayoffScanResult(
            solution=PayoffSolution(
                is_arbitrage=True,
                roi=Decimal("0.41"),
                minimum_state_pnl=Decimal("1"),
                numerically_validated=True,
            )
        ),
    )
    assert decision_is_solver_arbitrage(blocked) is True
    assert decision_net_edge(blocked) is not None
    assert decision_net_edge(blocked) >= blocked.minimum_net_edge
    assert execution_reprice_permitted(blocked) is True
    assert "missing_risk_evidence" not in _paper_blocking_reasons(list(blocked.rejection_reasons))


def test_empty_solver_does_not_become_eligible_without_risk_evidence() -> None:
    repository = SqliteMarketIntelligenceRepository()
    service = OptionalRiskUnavailableScan(MarketIntelligenceService(repository))
    mb_event, mb_market = matchbook_payloads()
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=OBSERVED, quote_age_ms=120
    )
    event, market, _books, series = kalshi_btts_payloads()
    expensive = {
        market["ticker"]: {
            "orderbook_fp": {
                "yes_dollars": [["0.01", "10.00"]],
                "no_dollars": [["0.01", "10.00"]],
            }
        }
    }
    kalshi = KalshiObservationBuilder().build(
        event,
        market,
        expensive,
        series=series,
        observed_at=OBSERVED,
        quote_age_ms=80,
        quote_age_basis="retrieval",
        fee_snapshot={"fee_type": "quadratic", "fee_multiplier": "1"},
    )
    try:
        decision = service.scan_pair(
            matchbook,
            kalshi,
            venue_costs=matchbook_kalshi_costs("0.02"),
            fx_snapshots=_fx(),
        )
    finally:
        repository.close()
    assert decision_is_solver_arbitrage(decision) is False
    assert decision.eligible_for_paper_simulation is False
    assert "no_arbitrage" in decision.rejection_reasons or "no_positive_edge" in decision.rejection_reasons
    assert execution_reprice_permitted(decision) is False


def test_hard_gates_still_reject_without_using_risk_evidence() -> None:
    repository = SqliteMarketIntelligenceRepository()
    service = PaperScanService(MarketIntelligenceService(repository))
    mb_market, pm_market, books = _half_line_totals_payloads()
    matchbook = MatchbookObservationBuilder().build(
        BVB_MB_EVENT, mb_market, observed_at=NOW, quote_age_ms=120
    )
    polymarket = PolymarketObservationBuilder().build(
        BVB_PM_EVENT, pm_market, books, observed_at=NOW, quote_age_ms=150
    )
    try:
        missing_fx = service.scan_pair(
            matchbook, polymarket, venue_costs=matchbook_polymarket_costs()
        )
        missing_fee = service.scan_pair(matchbook, polymarket, fx_snapshots=_fx())
        below_min = service.scan_pair(
            matchbook,
            polymarket,
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=_fx(),
            minimum_net_edge=Decimal("0.90"),
        )
        stale_mb = MatchbookObservationBuilder().build(
            BVB_MB_EVENT, mb_market, observed_at=NOW, quote_age_ms=3000
        )
        stale_pm = PolymarketObservationBuilder().build(
            BVB_PM_EVENT, pm_market, books, observed_at=NOW, quote_age_ms=3100
        )
        stale = service.scan_pair(
            stale_mb,
            stale_pm,
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=_fx(),
        )
        mb_no_under = {
            "id": 84226,
            "name": "Over/Under 2.5 Goals",
            "runners": [
                {
                    "id": 1,
                    "name": "Over 2.5",
                    "prices": [
                        {"side": "back", "odds": "1.90", "available-amount": "400"},
                        {"side": "lay", "odds": "1.92", "available-amount": "400"},
                    ],
                },
                {"id": 2, "name": "Under 2.5", "prices": [{"side": "lay", "odds": "2.42", "available-amount": "400"}]},
            ],
        }
        empty_under = {
            "o25": books["o25"],
            "u25": {"asset_id": "u25", "asks": [], "bids": [{"price": "0.53", "size": "500"}]},
        }
        thin_mb = MatchbookObservationBuilder().build(
            BVB_MB_EVENT, mb_no_under, observed_at=NOW, quote_age_ms=120
        )
        thin_pm = PolymarketObservationBuilder().build(
            BVB_PM_EVENT, pm_market, empty_under, observed_at=NOW, quote_age_ms=150
        )
        missing_depth = service.scan_pair(
            thin_mb, thin_pm, venue_costs=matchbook_polymarket_costs(), fx_snapshots=_fx()
        )
        integer_mb = MatchbookObservationBuilder().build(
            BVB_MB_EVENT,
            {
                "id": 84210,
                "name": "Over/Under 2.0 Goals",
                "line": "2.0",
                "runners": [
                    {"id": 1, "name": "Over 2.0", "prices": [{"side": "back", "odds": "1.90", "available-amount": "400"}]},
                    {"id": 2, "name": "Under 2.0", "prices": [{"side": "back", "odds": "2.40", "available-amount": "400"}]},
                ],
            },
            observed_at=NOW,
            quote_age_ms=120,
        )
        integer_pm = PolymarketObservationBuilder().build(
            BVB_PM_EVENT,
            {
                "id": "pm-tg-20-29",
                "question": "Total goals 2.0",
                "sportsMarketType": "total goals",
                "line": "2.0",
                "outcomes": '["Over", "Under"]',
                "clobTokenIds": '["o20", "u20"]',
                "description": "Resolves based on 90 minutes of regulation time.",
            },
            {
                "o20": books["o25"],
                "u20": books["u25"],
            },
            observed_at=NOW,
            quote_age_ms=150,
        )
        unregistered = service.scan_pair(
            integer_mb, integer_pm, venue_costs=matchbook_polymarket_costs(), fx_snapshots=_fx()
        )
    finally:
        repository.close()

    assert missing_fx.eligible_for_paper_simulation is False
    assert any(reason.startswith("missing_fx_rate") for reason in missing_fx.rejection_reasons)
    assert missing_fee.eligible_for_paper_simulation is False
    assert any("missing_venue_cost" in reason or "unknown_required_venue_cost" in reason for reason in missing_fee.rejection_reasons)
    assert below_min.eligible_for_paper_simulation is False
    assert "net_edge_below_threshold" in below_min.rejection_reasons
    assert below_min.depth_scan is not None and below_min.depth_scan.solution.is_arbitrage is True
    assert stale.eligible_for_paper_simulation is False
    assert "stale_quote" in stale.rejection_reasons
    assert missing_depth.eligible_for_paper_simulation is False
    assert "missing_executable_outcome_depth" in missing_depth.rejection_reasons or (
        missing_depth.depth_scan is not None and missing_depth.depth_scan.solution.is_arbitrage is False
    )
    assert unregistered.eligible_for_paper_simulation is False
    assert "not_registered" in unregistered.rejection_reasons or "market_not_equivalent" in unregistered.rejection_reasons
    assert unregistered.depth_scan is None and unregistered.payoff_scan is None


def test_in_play_huge_price1_edge_still_requires_price2_permission_not_direct_fill() -> None:
    in_play_kickoff = NOW - timedelta(minutes=12)
    mb_event = dict(BVB_MB_EVENT, start=in_play_kickoff.isoformat())
    pm_event = dict(BVB_PM_EVENT, startTime=in_play_kickoff.isoformat())
    mb_market, pm_market, books = _half_line_totals_payloads()
    repository = SqliteMarketIntelligenceRepository()
    service = PaperScanService(MarketIntelligenceService(repository))
    matchbook = MatchbookObservationBuilder().build(mb_event, mb_market, observed_at=NOW, quote_age_ms=80)
    polymarket = PolymarketObservationBuilder().build(
        pm_event, pm_market, books, observed_at=NOW, quote_age_ms=90
    )
    try:
        decision = service.scan_pair(
            matchbook, polymarket, venue_costs=matchbook_polymarket_costs(), fx_snapshots=_fx()
        )
    finally:
        repository.close()
    assert decision_is_solver_arbitrage(decision) is True
    assert decision_net_edge(decision) is not None
    assert decision_net_edge(decision) > Decimal("0.10")
    assert execution_reprice_permitted(decision) is True
    source = inspect.getsource(
        __import__(
            "sports_hedge.application.execution_reprice", fromlist=["capture_with_execution_reprice"]
        ).capture_with_execution_reprice
    )
    assert "execution_reprice_permitted(decision)" in source
    assert "if not execution_reprice_permitted(decision):" in source


def test_max_risk_is_not_a_price1_admission_comparison() -> None:
    scan_src = inspect.getsource(PaperScanService.scan_pair)
    assert "maximum_execution_risk" in scan_src
    assert "execution_risk.score" not in scan_src
    assert "execution_risk_above_threshold" not in scan_src
    assert "fill_confidence_below_threshold" not in scan_src
    assert "rejections.append(\"missing_risk_evidence\")" not in scan_src


def test_missing_risk_evidence_is_informational_not_a_paper_block() -> None:
    assert _paper_blocking_reasons(["missing_risk_evidence"]) == []
    assert _paper_blocking_reasons(["no_positive_edge", "missing_risk_evidence"]) == [
        "no_positive_edge",
    ]


def test_combined_no_positive_edge_and_missing_risk_evidence_still_rejects_price2() -> None:
    """Solver no-arb remains the economic veto. Leftover risk label is not a substitute."""

    blocked = _decision(
        eligible_for_paper_simulation=False,
        rejection_reasons=["no_positive_edge", "missing_risk_evidence"],
        payoff_scan=PayoffScanResult(
            solution=PayoffSolution(is_arbitrage=False, roi=Decimal("-0.12"))
        ),
    )
    assert decision_is_solver_arbitrage(blocked) is False
    assert "no_positive_edge" in _paper_blocking_reasons(list(blocked.rejection_reasons))
    assert execution_reprice_permitted(blocked) is False
    assert execution_entry_block(blocked) is not None


def test_complete_btts_and_1x2_books_score_optional_risk_when_inputs_exist() -> None:
    repository = SqliteMarketIntelligenceRepository()
    service = PaperScanService(MarketIntelligenceService(repository))
    try:
        mb_btts = MatchbookObservationBuilder().build(
            BVB_MB_EVENT,
            {
                "id": 7102,
                "name": "Both Teams To Score",
                "runners": [
                    {"id": 11, "name": "Yes", "prices": [{"side": "back", "odds": "1.80", "available-amount": "400"}]},
                    {"id": 12, "name": "No", "prices": [{"side": "back", "odds": "2.20", "available-amount": "400"}]},
                ],
            },
            observed_at=NOW,
            quote_age_ms=110,
        )
        pm_btts = PolymarketObservationBuilder().build(
            BVB_PM_EVENT,
            {
                "id": "pm-btts-29",
                "question": "Both teams to score?",
                "sportsMarketType": "both teams to score",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["yes", "no"]',
                "description": "Resolves based on 90 minutes of regulation time.",
            },
            {
                "yes": {"asset_id": "yes", "asks": [{"price": "0.22", "size": "500"}], "bids": [{"price": "0.20", "size": "500"}]},
                "no": {"asset_id": "no", "asks": [{"price": "0.62", "size": "500"}], "bids": [{"price": "0.60", "size": "500"}]},
            },
            observed_at=NOW,
            quote_age_ms=130,
        )
        btts = service.scan_pair(
            mb_btts, pm_btts, venue_costs=matchbook_polymarket_costs(), fx_snapshots=_fx()
        )
        mb_1x2 = MatchbookObservationBuilder().build(
            BVB_MB_EVENT,
            {
                "id": 7101,
                "name": "Match Odds",
                "runners": [
                    {"id": 1, "name": "Borussia Dortmund", "prices": [{"side": "back", "odds": "1.70", "available-amount": "400"}]},
                    {"id": 2, "name": "Draw", "prices": [{"side": "back", "odds": "4.20", "available-amount": "400"}]},
                    {"id": 3, "name": "Werder Bremen", "prices": [{"side": "back", "odds": "5.50", "available-amount": "400"}]},
                ],
            },
            observed_at=NOW,
            quote_age_ms=90,
        )
        pm_1x2 = PolymarketObservationBuilder().build(
            BVB_PM_EVENT,
            {
                "id": "pm-1x2-29",
                "question": "Match result?",
                "sportsMarketType": "moneyline",
                "outcomes": '["Borussia Dortmund", "Draw", "Werder Bremen"]',
                "clobTokenIds": '["h", "d", "a"]',
                "description": "Resolves based on 90 minutes of regulation time.",
            },
            {
                "h": {"asset_id": "h", "asks": [{"price": "0.20", "size": "500"}], "bids": [{"price": "0.18", "size": "500"}]},
                "d": {"asset_id": "d", "asks": [{"price": "0.18", "size": "500"}], "bids": [{"price": "0.16", "size": "500"}]},
                "a": {"asset_id": "a", "asks": [{"price": "0.45", "size": "500"}], "bids": [{"price": "0.43", "size": "500"}]},
            },
            observed_at=NOW,
            quote_age_ms=95,
        )
        match_odds = service.scan_pair(
            mb_1x2, pm_1x2, venue_costs=matchbook_polymarket_costs(), fx_snapshots=_fx()
        )
    finally:
        repository.close()

    assert mb_btts.market.family is MarketFamily.BOTH_TEAMS_TO_SCORE
    assert btts.market_match.matched is True
    assert btts.solver_model == SOLVER_MODEL_SIMPLE
    assert decision_is_solver_arbitrage(btts) is True
    assert btts.execution_risk_inputs is not None
    assert btts.execution_risk is not None
    assert "missing_risk_evidence" not in btts.rejection_reasons
    assert mb_1x2.market.family is MarketFamily.MATCH_RESULT
    assert match_odds.market_match.matched is True
    assert match_odds.solver_model == SOLVER_MODEL_SIMPLE
    assert decision_is_solver_arbitrage(match_odds) is True
    assert match_odds.execution_risk_inputs is not None
    assert "missing_risk_evidence" not in match_odds.rejection_reasons


def test_optional_risk_reason_is_not_a_second_hard_display_or_promotion_gate() -> None:
    assert "missing_risk_evidence" not in HARD_NON_EXECUTABLE_REASONS
    assert "missing_risk_evidence" not in PROMOTION_FAIL_CLOSED_REASONS
    assert "missing_risk_evidence" in INFORMATIONAL_NONBLOCKING_REASONS
    candidate = FixtureHeadlineCandidate(
        family="total_goals",
        line=Decimal("2.5"),
        selection="over",
        current_net_edge=Decimal("0.041"),
        trigger_net_edge=Decimal("0.01"),
        eligible_for_paper_simulation=True,
        solver_is_arbitrage=True,
        rejection_reasons=["missing_risk_evidence"],
        liquidity_role=LiquidityRole.TAKER,
        quote_age_ms=80,
    )
    assert headline_band_for(candidate) is HeadlineBand.QUALIFYING
    status, reasons = classify_status(
        _observation(
            edge=Decimal("0.015"),
            eligible=True,
            quote_age_ms=80,
            solver_is_arbitrage=True,
            rejection_reasons=["missing_risk_evidence"],
        ),
        approaching_band_pp=NET_PROXIMITY_BAND_PP,
        max_quote_age_ms=2000,
    )
    assert status is OpportunityStatus.TRIGGERED
    assert "missing_risk_evidence" in reasons
    row = _market_row(rejection_reasons=["missing_risk_evidence"])
    assert stored_row_proves_qualifying_executable(row) is True
    combined, _combined_reasons = classify_status(
        _observation(
            edge=Decimal("0.015"),
            eligible=False,
            quote_age_ms=80,
            solver_is_arbitrage=False,
            rejection_reasons=["no_positive_edge", "missing_risk_evidence"],
        ),
        approaching_band_pp=NET_PROXIMITY_BAND_PP,
        max_quote_age_ms=2000,
    )
    assert combined is OpportunityStatus.REJECTED


def test_priority_alerts_and_max_risk_do_not_gate_price1_price2_paper_entry() -> None:
    scan_src = inspect.getsource(PaperScanService.scan_pair)
    reprice_src = inspect.getsource(capture_with_execution_reprice)
    finish_src = inspect.getsource(CataloguePriceEngine._finish_execution_reprice)
    qualify_src = inspect.getsource(qualify_priority_alert)
    assert "qualify_priority_alert" not in scan_src
    assert "qualify_priority_alert" not in reprice_src
    assert "qualify_priority_alert" not in finish_src
    assert "execution_risk_above_priority_threshold" in qualify_src
    assert "fill_confidence_below_priority_threshold" in qualify_src
    assert "execution_risk_above_priority_threshold" not in scan_src
    assert "fill_confidence_below_priority_threshold" not in scan_src
    low = allocate(_request(execution_risk_score=0))
    high = allocate(_request(execution_risk_score=99))
    assert low.accepted and high.accepted
    assert high.maximum_validated_capital == low.maximum_validated_capital
    assert high.recommended_committed_capital == high.maximum_validated_capital
    assert EXECUTION_REPRICE_SKEW == "execution_reprice_skew"
    assert "EXECUTION_REPRICE_SKEW" in finish_src
    assert "skew_exceeded" in finish_src


def test_deterministic_funnel_separates_sole_risk_from_solver_no_arb() -> None:
    repository = SqliteMarketIntelligenceRepository()
    service = PaperScanService(MarketIntelligenceService(repository))
    hidden = OptionalRiskUnavailableScan(MarketIntelligenceService(repository))
    try:
        qualifying, _, _ = _scan_pair(service)
        sole_risk, _, _ = _scan_pair(hidden)
        below = service.scan_pair(
            MatchbookObservationBuilder().build(
                BVB_MB_EVENT, _half_line_totals_payloads()[0], observed_at=NOW, quote_age_ms=120
            ),
            PolymarketObservationBuilder().build(
                BVB_PM_EVENT,
                _half_line_totals_payloads()[1],
                _half_line_totals_payloads()[2],
                observed_at=NOW,
                quote_age_ms=150,
            ),
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=_fx(),
            minimum_net_edge=Decimal("0.90"),
        )
        mb_event, mb_market = matchbook_payloads()
        matchbook = MatchbookObservationBuilder().build(
            mb_event, mb_market, observed_at=OBSERVED, quote_age_ms=120
        )
        event, market, _books, series = kalshi_btts_payloads()
        expensive = {
            market["ticker"]: {
                "orderbook_fp": {
                    "yes_dollars": [["0.01", "10.00"]],
                    "no_dollars": [["0.01", "10.00"]],
                }
            }
        }
        kalshi = KalshiObservationBuilder().build(
            event,
            market,
            expensive,
            series=series,
            observed_at=OBSERVED,
            quote_age_ms=80,
            quote_age_basis="retrieval",
            fee_snapshot={"fee_type": "quadratic", "fee_multiplier": "1"},
        )
        no_arb = service.scan_pair(
            matchbook,
            kalshi,
            venue_costs=matchbook_kalshi_costs("0.02"),
            fx_snapshots=_fx(),
        )
    finally:
        repository.close()

    leftover_both = _decision(
        canonical_market_id="hist-both",
        eligible_for_paper_simulation=False,
        rejection_reasons=["no_positive_edge", "missing_risk_evidence"],
        payoff_scan=PayoffScanResult(
            solution=PayoffSolution(is_arbitrage=False, roi=Decimal("-0.04"))
        ),
    )
    repeated = [
        ("totals-complete", qualifying),
        ("totals-complete", qualifying),
        ("totals-hidden-optional-risk", sole_risk),
        ("totals-below-min", below),
        ("btts-no-arb", no_arb),
        ("hist-both", leftover_both),
    ]
    unique = dict(repeated)
    buckets = {"sole_optional_risk": 0, "risk_plus_hard": 0, "other_blocker": 0, "price2_permitted": 0}
    for item in unique.values():
        blocking = _paper_blocking_reasons(list(item.rejection_reasons))
        arb = decision_is_solver_arbitrage(item)
        if execution_reprice_permitted(item):
            buckets["price2_permitted"] += 1
        elif "missing_risk_evidence" in item.rejection_reasons and not arb:
            buckets["risk_plus_hard"] += 1
        elif arb and not blocking:
            buckets["sole_optional_risk"] += 1
        else:
            buckets["other_blocker"] += 1

    assert len(unique) == 5
    assert buckets["price2_permitted"] >= 1
    assert buckets["risk_plus_hard"] == 1
    assert buckets["other_blocker"] >= 1
    assert no_arb.eligible_for_paper_simulation is False
    assert "no_arbitrage" in no_arb.rejection_reasons or "no_positive_edge" in no_arb.rejection_reasons
    assert "missing_risk_evidence" not in no_arb.rejection_reasons
    assert execution_reprice_permitted(no_arb) is False
    assert below.eligible_for_paper_simulation is False
    assert "net_edge_below_threshold" in below.rejection_reasons
    assert execution_reprice_permitted(qualifying) is True
    assert execution_reprice_permitted(sole_risk) is True


@pytest.mark.asyncio
async def test_absent_optional_risk_still_invokes_price2_and_records_authoritative_outcome(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original = PaperScanService._risk_inputs

    def hide(self, *args: object, **kwargs: object):
        original(self, *args, **kwargs)
        return None

    monkeypatch.setattr(PaperScanService, "_risk_inputs", hide)
    rich = _market()
    book = _book("0.20", "0.70")
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="issue29-price2",
        matchbook_payloads=[_fresh(rich), _fresh(rich)],
        kalshi_books=[book, book],
    )
    try:
        assert len(bundle.scan.seen) >= 2
        discovery, execution = bundle.scan.seen[0], bundle.scan.seen[1]
        assert discovery.execution_risk is None
        assert "missing_risk_evidence" not in discovery.rejection_reasons
        assert execution_reprice_permitted(discovery) is True
        assert execution.eligible_for_paper_simulation is True
        trade = _open_trade(bundle)
        assert trade.paper_only is True
        assert trade.places_orders is False
        assert bundle.settings.sports_hedge_execution_enabled is False
    finally:
        _close(bundle)
