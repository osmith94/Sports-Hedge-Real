from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest
from fastapi.testclient import TestClient

from sports_hedge.accounting.dimensions import CapitalSource
from sports_hedge.accounting.paper_journal import DataProvenance
from sports_hedge.api.main import app
from sports_hedge.api.market_intelligence import get_market_intelligence_service
from sports_hedge.api.paper import get_paper_audit_repository, get_paper_operations_service
from sports_hedge.api.priority_alerts import get_priority_alert_service
from sports_hedge.api.watchlist import get_watchlist_service
from sports_hedge.application.market_observation import MatchbookObservationBuilder
from sports_hedge.application.paper_operations import PaperOperationsError, PaperOperationsService
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.arbitrage.priority_alerts.models import ExternalLegConfirmation, LegExecutionMode
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.priority_alerts.thresholds import PriorityAlertThresholds
from sports_hedge.arbitrage.watchlist.models import OpportunityStatus
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.chain import PaperChainStep
from sports_hedge.paper.fills import PaperFillConfig
from test_paper_trade_lifecycle import _force_external_kalshi
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from test_paper_scan_pipeline import kalshi_btts_observation, matchbook_payloads
from venue_cost_helpers import matchbook_kalshi_costs


OBSERVED = datetime(2026, 9, 20, 13, 0, tzinfo=UTC)


def _scan_and_persist() -> tuple[
    PaperScanService,
    WatchlistService,
    PaperOperationsService,
    SqliteMarketIntelligenceRepository,
]:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    settings = Settings(max_slippage_bps=0, fx_spread_bps=0, simulated_latency_ms=0)
    scan = PaperScanService(intelligence, settings=settings)
    watchlist = WatchlistService(SqliteWatchlistRepository(), max_quote_age_ms=10_000)
    alerts = PriorityAlertService(
        thresholds=PriorityAlertThresholds(
            minimum_net_edge=Decimal("0.001"),
            minimum_expected_profit=Decimal("0.01"),
            minimum_executable_depth=Decimal("1"),
            maximum_quote_age_ms=10_000,
            maximum_execution_risk=100,
            minimum_depth_coverage=Decimal("0"),
            minimum_capital_efficiency=Decimal("0"),
        ),
        settings=settings,
    )
    ops = PaperOperationsService(
        watchlist=watchlist,
        alerts=alerts,
        settings=settings,
    )
    mb_event, mb_market = matchbook_payloads()
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=OBSERVED, quote_age_ms=120
    )
    kalshi = kalshi_btts_observation()
    decision = scan.scan_pair(
        matchbook,
        kalshi,
        venue_costs=matchbook_kalshi_costs(),
        fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), spread_bps=Decimal("0"))],
        maximum_execution_risk=100,
    )
    assert decision.eligible_for_paper_simulation is True
    watchlist.observe_paper_decision(decision, intelligence.market_history(
        canonical_market_id=decision.canonical_market_id
    ))
    ops.persist_triggered_chain(decision, provenance=DataProvenance.FIXTURE_DEMO)
    return scan, watchlist, ops, repository


def test_full_internal_paper_fill_posts_balanced_native_separated_journal() -> None:
    _scan, watchlist, ops, repository = _scan_and_persist()
    try:
        opportunity_id = next(iter(ops._plans))
        plan = ops._plans[opportunity_id]
        plan.execution_modes = {venue: LegExecutionMode.INTERNAL for venue in plan.execution_modes}
        result = ops.simulate_fill(
            opportunity_id,
            config=PaperFillConfig(assumed_latency_ms=0, max_quote_age_ms=10_000),
            provenance=DataProvenance.FIXTURE_DEMO,
        )
        assert result.opportunity.status is OpportunityStatus.FILLED
        assert result.fills.fully_filled is True
        assert result.trace.balanced_gbp is True
        assert result.places_orders is False
        assert "USD" in result.trace.native_totals
        assert "GBP" in result.trace.native_totals
        assert PaperChainStep.SCAN_DECISION in result.trace.steps
        assert PaperChainStep.JOURNAL_POSTING in result.trace.steps
        currencies = {entry.postings[0].dimensions.currency for entry in result.journals}
        assert currencies == {"GBP", "USD"}
        activity = watchlist.activity(opportunity_id=opportunity_id)
        assert any(item.status is OpportunityStatus.FILLED for item in activity)
    finally:
        repository.close()


def test_partial_paper_fill_records_partial_and_balances() -> None:
    _scan, _watchlist, ops, repository = _scan_and_persist()
    try:
        opportunity_id = next(iter(ops._plans))
        plan = ops._plans[opportunity_id]
        plan.execution_modes = {venue: LegExecutionMode.INTERNAL for venue in plan.execution_modes}
        thin = plan.legs[0]
        plan.legs[0] = thin.model_copy(
            update={
                "requested_stake": thin.requested_stake,
                "levels": [BookLevel(decimal_odds=thin.displayed_odds, available_stake=thin.requested_stake / 4)],
            }
        )
        result = ops.simulate_fill(
            opportunity_id,
            config=PaperFillConfig(assumed_latency_ms=0, max_quote_age_ms=10_000),
            provenance=DataProvenance.FIXTURE_DEMO,
        )
        assert result.opportunity.status is OpportunityStatus.PARTIAL
        assert result.fills.fully_filled is False
        assert result.trace.balanced_gbp is True
    finally:
        repository.close()


def test_stale_before_fill_is_rejected() -> None:
    _scan, _watchlist, ops, repository = _scan_and_persist()
    try:
        opportunity_id = next(iter(ops._plans))
        plan = ops._plans[opportunity_id]
        plan.execution_modes = {venue: LegExecutionMode.INTERNAL for venue in plan.execution_modes}
        stale_legs = [
            leg.model_copy(update={"quote_age_ms": 50_000}) for leg in plan.legs
        ]
        ops._plans[opportunity_id] = plan.model_copy(
            update={"legs": stale_legs, "quote_age_ms": 50_000, "quote_age_at_decision_ms": 50_000}
        )
        with pytest.raises(PaperOperationsError, match="snapshot_stale_at_decision"):
            ops.simulate_fill(
                opportunity_id,
                config=PaperFillConfig(assumed_latency_ms=0, max_quote_age_ms=10_000),
                now=OBSERVED,
            )
    finally:
        repository.close()


def test_manual_external_requires_confirmation_and_revalidates_hedge() -> None:
    _scan, _watchlist, ops, repository = _scan_and_persist()
    try:
        opportunity_id = next(iter(ops._plans))
        plan = _force_external_kalshi(ops._plans[opportunity_id])
        assert plan.execution_modes[VenueName.KALSHI] is LegExecutionMode.EXTERNAL_OPERATOR
        with pytest.raises(PaperOperationsError, match="manual_external_confirmation_required"):
            ops.simulate_fill(
                opportunity_id,
                config=PaperFillConfig(assumed_latency_ms=0, max_quote_age_ms=10_000),
            )

        external = next(leg for leg in plan.legs if leg.venue is VenueName.KALSHI)
        from sports_hedge.application.paper_operations import _net_odds_for_leg

        net_price = _net_odds_for_leg(plan, external)
        too_large = ExternalLegConfirmation(
            outcome=external.outcome,
            venue=external.venue,
            product_id=external.source_market_id,
            operator_counterparty_reference="ext-too-big",
            executed_price=net_price,
            executed_size=Decimal("1000000"),
            currency=external.currency,
            executed_at=OBSERVED,
            eligibility_confirmed=True,
        )
        with pytest.raises(PaperOperationsError, match="remaining_hedge_revalidation_failed"):
            ops.simulate_fill(
                opportunity_id,
                config=PaperFillConfig(assumed_latency_ms=0, max_quote_age_ms=10_000),
                confirm_external=too_large,
            )

        ok = too_large.model_copy(
            update={
                "executed_size": Decimal("5"),
                "operator_counterparty_reference": "ext-ok",
            }
        )
        result = ops.simulate_fill(
            opportunity_id,
            config=PaperFillConfig(assumed_latency_ms=0, max_quote_age_ms=10_000),
            confirm_external=ok,
            capital_source=CapitalSource.AUTO_POOL,
            provenance=DataProvenance.FIXTURE_DEMO,
        )
        assert result.hedge_still_valid is True
        assert result.trace.balanced_gbp is True
        sources = {entry.postings[0].dimensions.capital_source for entry in result.journals}
        assert CapitalSource.MANUAL_EXTERNAL in sources
        assert not any(fill.venue is VenueName.KALSHI for fill in result.fills.fills)
    finally:
        repository.close()


def test_simulate_fill_api_is_paper_only() -> None:
    repository = SqliteMarketIntelligenceRepository()
    audit = SqlitePaperScanRepository()
    watchlist_store = SqliteWatchlistRepository()
    intelligence = MarketIntelligenceService(repository)
    watchlist = WatchlistService(watchlist_store, max_quote_age_ms=10_000)
    ops = PaperOperationsService(watchlist=watchlist, settings=Settings(max_slippage_bps=0, fx_spread_bps=0))
    app.dependency_overrides[get_market_intelligence_service] = lambda: intelligence
    app.dependency_overrides[get_paper_audit_repository] = lambda: audit
    app.dependency_overrides[get_watchlist_service] = lambda: watchlist
    app.dependency_overrides[get_paper_operations_service] = lambda: ops
    app.dependency_overrides[get_priority_alert_service] = lambda: ops.alerts
    client = TestClient(app)
    try:
        missing = client.post("/paper/simulate-fill", json={"opportunity_id": "watch:unknown"})
        assert missing.status_code == 409
        assert "missing_paper_fill_plan" in missing.json()["detail"]
    finally:
        app.dependency_overrides.clear()
        audit.close()
        watchlist_store.close()
        repository.close()
