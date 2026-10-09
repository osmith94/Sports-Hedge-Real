"""Issue #29: optional heuristic risk evidence is not a paper Price-1/Price-2 veto.

Deterministic fixture/demo MB↔PM books. Not live venue quotes. Execution stays
disarmed. Data class: modelled paper-scan payloads.
"""

from __future__ import annotations

import inspect
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sports_hedge.application.complete_set import SOLVER_MODEL_SIMPLE, generalized_state_model
from sports_hedge.application.execution_reprice import (
    execution_entry_block,
    execution_reprice_permitted,
)
from sports_hedge.application.executable_liquidity import decision_is_solver_arbitrage, decision_net_edge
from sports_hedge.application.market_observation import (
    KalshiObservationBuilder,
    MatchbookObservationBuilder,
    PolymarketObservationBuilder,
)
from sports_hedge.application.paper_scan import PaperScanService, _paper_blocking_reasons
from sports_hedge.arbitrage.models import PayoffSolution
from sports_hedge.arbitrage.payoff_scan import PayoffScanResult
from sports_hedge.config import Settings
from sports_hedge.domain.football import MarketFamily
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.venues import KalshiClient, MatchbookClient, PolymarketClient
from test_execution_reprice_before_paper_entry import _decision
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
        stale = PaperScanService(
            MarketIntelligenceService(SqliteMarketIntelligenceRepository()),
            settings=Settings(paper_entry_max_quote_age_ms=50),
        ).scan_pair(
            matchbook,
            polymarket,
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=_fx(),
        )
        empty_under = {
            "o25": books["o25"],
            "u25": {"asset_id": "u25", "asks": [], "bids": [{"price": "0.53", "size": "500"}]},
        }
        thin_pm = PolymarketObservationBuilder().build(
            BVB_PM_EVENT, pm_market, empty_under, observed_at=NOW, quote_age_ms=150
        )
        missing_depth = service.scan_pair(
            matchbook, thin_pm, venue_costs=matchbook_polymarket_costs(), fx_snapshots=_fx()
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
