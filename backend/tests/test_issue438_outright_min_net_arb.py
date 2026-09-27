"""Issue #438 / Outrights Phase 1B: separate Outright Min Net Arb.

Deterministic models only. This lane does not enable outright equivalence,
admit COMPETITION_SEASON catalogue rows, or fabricate an outright opportunity.
PAPER / read-only. No provider, capture, concurrency or settlement changes.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

from authorised_execution_plan import authorised_execution_plan
from pathlib import Path

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.application.live_refresh import get_live_refresh_coordinator
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.price_engine import CataloguePriceEngine
from sports_hedge.arbitrage.min_net_threshold import (
    FIXTURE_MIN_NET_EDGE_SOURCE,
    OUTRIGHT_MIN_NET_EDGE_SOURCE,
    OUTRIGHT_MIN_NET_EDGE_UNCONFIGURED,
    catalogue_market_scope,
    qualifies_with_resolved_threshold,
    resolve_min_net_threshold,
)
from sports_hedge.arbitrage.watchlist.adapter import observation_from_paper_decision
from sports_hedge.arbitrage.watchlist.economics import qualifies_min_net_arb
from sports_hedge.arbitrage.watchlist.models import OpportunityStatus
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.models import MarketScope
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.application.active_trade_lane import reset_active_trade_registry
from sports_hedge.paper.trades import PaperActiveTradePhase, PaperTradeTrancheKind
from sports_hedge.persistence.operator_scanner_settings import (
    SqliteOperatorScannerSettingsStore,
    bind_runtime_operator_scanner_settings_store,
    env_operator_scanner_settings,
    resolve_operator_scanner_settings,
)
from test_issue367_operator_scanner_controls import _unbind
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from test_issue372_active_trade_lane import _raise_trade_cap
from test_paper_scan_pipeline import (
    OBSERVED,
    kalshi_btts_observation,
    matchbook_payloads,
)
from sports_hedge.application.market_observation import MatchbookObservationBuilder
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from test_paper_trade_lifecycle import OBSERVED as TRADE_OBSERVED
from test_paper_trade_lifecycle import _ops
from venue_cost_helpers import matchbook_kalshi_costs


FIXTURE_TRIGGER = Decimal("0.01")
OUTRIGHT_TRIGGER = Decimal("0.02")
EDGE_BETWEEN = Decimal("0.015")
EDGE_ABOVE_OUTRIGHT = Decimal("0.025")
EDGE_BELOW_FIXTURE = Decimal("0.008")


def test_env_and_operator_defaults_leave_outright_unconfigured() -> None:
    settings = Settings()
    assert Settings.model_fields["min_net_edge"].default == 0.01
    assert Settings.model_fields["outright_min_net_edge"].default is None
    assert settings.outright_min_net_edge is None
    env = env_operator_scanner_settings(settings)
    assert env.min_net_edge == Decimal("0.01")
    assert env.outright_min_net_edge is None
    assert env.source == "env_default"


def test_resolve_fixture_scope_uses_only_fixture_threshold() -> None:
    decided = resolve_min_net_threshold(
        MarketScope.FIXTURE_MATCH,
        fixture_min_net_edge=FIXTURE_TRIGGER,
        outright_min_net_edge=OUTRIGHT_TRIGGER,
    )
    assert decided.market_scope is MarketScope.FIXTURE_MATCH
    assert decided.applied_threshold == FIXTURE_TRIGGER
    assert decided.threshold_source == FIXTURE_MIN_NET_EDGE_SOURCE
    assert decided.configured is True
    assert decided.fail_closed_reason is None
    assert qualifies_with_resolved_threshold(EDGE_BETWEEN, decided) is True
    assert qualifies_with_resolved_threshold(EDGE_BELOW_FIXTURE, decided) is False


def test_resolve_outright_scope_uses_only_outright_threshold() -> None:
    decided = resolve_min_net_threshold(
        MarketScope.COMPETITION_SEASON,
        fixture_min_net_edge=FIXTURE_TRIGGER,
        outright_min_net_edge=OUTRIGHT_TRIGGER,
    )
    assert decided.market_scope is MarketScope.COMPETITION_SEASON
    assert decided.applied_threshold == OUTRIGHT_TRIGGER
    assert decided.threshold_source == OUTRIGHT_MIN_NET_EDGE_SOURCE
    assert decided.configured is True
    assert qualifies_with_resolved_threshold(EDGE_BETWEEN, decided) is False
    assert qualifies_with_resolved_threshold(EDGE_ABOVE_OUTRIGHT, decided) is True


def test_missing_outright_threshold_fails_closed_without_using_fixture_min_net() -> None:
    decided = resolve_min_net_threshold(
        MarketScope.COMPETITION_SEASON,
        fixture_min_net_edge=FIXTURE_TRIGGER,
        outright_min_net_edge=None,
    )
    assert decided.configured is False
    assert decided.applied_threshold is None
    assert decided.fail_closed_reason == OUTRIGHT_MIN_NET_EDGE_UNCONFIGURED
    assert decided.threshold_source == OUTRIGHT_MIN_NET_EDGE_SOURCE
    assert qualifies_with_resolved_threshold(EDGE_ABOVE_OUTRIGHT, decided) is False
    assert qualifies_with_resolved_threshold(Decimal("1"), decided) is False
    assert qualifies_min_net_arb(EDGE_ABOVE_OUTRIGHT, decided.applied_threshold) is False


def test_fixture_min_net_does_not_apply_to_outright_scope() -> None:
    high_fixture = resolve_min_net_threshold(
        MarketScope.COMPETITION_SEASON,
        fixture_min_net_edge=Decimal("0.90"),
        outright_min_net_edge=Decimal("0.005"),
    )
    assert high_fixture.applied_threshold == Decimal("0.005")
    assert qualifies_with_resolved_threshold(Decimal("0.01"), high_fixture) is True


def test_outright_min_net_does_not_apply_to_fixture_qualification() -> None:
    high_outright = resolve_min_net_threshold(
        MarketScope.FIXTURE_MATCH,
        fixture_min_net_edge=Decimal("0.005"),
        outright_min_net_edge=Decimal("0.90"),
    )
    assert high_outright.applied_threshold == Decimal("0.005")
    assert qualifies_with_resolved_threshold(Decimal("0.01"), high_outright) is True


def test_no_duration_heuristic_selects_min_net_threshold() -> None:
    src = inspect.getsource(resolve_min_net_threshold)
    lowered = src.lower()
    assert "lock" not in lowered
    assert "annual" not in lowered
    assert "duration" not in lowered
    assert "horizon" not in lowered
    signature = inspect.signature(resolve_min_net_threshold)
    assert "fixture_min_net_edge" in signature.parameters
    assert "outright_min_net_edge" in signature.parameters
    assert "market_scope" in signature.parameters
    assert "lock_duration" not in signature.parameters
    assert "expected_lock_hours" not in signature.parameters


def test_catalogue_scope_reads_identity_and_does_not_invent_season() -> None:
    assert catalogue_market_scope() is MarketScope.FIXTURE_MATCH
    assert catalogue_market_scope(object()) is MarketScope.FIXTURE_MATCH

    class _FixtureIdentity:
        market_scope = MarketScope.FIXTURE_MATCH

    class _SeasonIdentity:
        market_scope = MarketScope.COMPETITION_SEASON

    assert catalogue_market_scope(_FixtureIdentity()) is MarketScope.FIXTURE_MATCH
    assert catalogue_market_scope(_SeasonIdentity()) is MarketScope.COMPETITION_SEASON
    engine_src = inspect.getsource(CataloguePriceEngine)
    assert "catalogue_market_scope" in engine_src
    helper_src = inspect.getsource(catalogue_market_scope)
    assert "return MarketScope.FIXTURE_MATCH" in helper_src
    assert "MarketScope(raw)" in helper_src


def test_operator_store_persists_outright_without_numeric_default(tmp_path: Path) -> None:
    store = SqliteOperatorScannerSettingsStore(tmp_path / "operator.sqlite")
    try:
        absent = resolve_operator_scanner_settings(store)
        assert absent.outright_min_net_edge is None
        saved = store.save_settings(
            min_net_edge=Decimal("0.0125"),
            max_execution_risk=40,
            hot_cadence_seconds=20,
            background_cadence_seconds=90,
            outright_min_net_edge=Decimal("0.018"),
        )
        assert saved.min_net_edge == Decimal("0.0125")
        assert saved.outright_min_net_edge == Decimal("0.018")
        kept = store.save_settings(
            min_net_edge=Decimal("0.009"),
            max_execution_risk=40,
            hot_cadence_seconds=20,
            background_cadence_seconds=90,
        )
        assert kept.min_net_edge == Decimal("0.009")
        assert kept.outright_min_net_edge == Decimal("0.018")
        cleared = store.save_settings(
            min_net_edge=Decimal("0.009"),
            max_execution_risk=40,
            hot_cadence_seconds=20,
            background_cadence_seconds=90,
            outright_min_net_edge=None,
        )
        assert cleared.outright_min_net_edge is None
    finally:
        store.close()


def test_malformed_outright_threshold_fails_closed_not_fixture_fallback(tmp_path: Path) -> None:
    store = SqliteOperatorScannerSettingsStore(tmp_path / "malformed.sqlite")
    try:
        store.save_settings(
            min_net_edge=Decimal("0.01"),
            max_execution_risk=60,
            hot_cadence_seconds=30,
            background_cadence_seconds=90,
            outright_min_net_edge=Decimal("0.02"),
        )
        with store._connect() as connection:
            connection.execute(
                "UPDATE operator_scanner_settings SET outright_min_net_edge = 'nope' WHERE id = 1"
            )
        loaded = store.load()
        assert loaded is not None
        assert loaded.min_net_edge == Decimal("0.01")
        assert loaded.outright_min_net_edge is None
    finally:
        store.close()


def test_update_http_persists_outright_and_omission_keeps_value(tmp_path: Path) -> None:
    store = SqliteOperatorScannerSettingsStore(tmp_path / "http.sqlite")
    coordinator = get_live_refresh_coordinator()
    coordinator.reset()
    coordinator.bind_operator_settings_store(store)
    client = TestClient(app)
    ticks: list[str] = []

    async def boom(_plan=None) -> None:
        ticks.append("tick")
        raise AssertionError("Update must not trigger scanner work")

    from sports_hedge.api import paper as paper_api

    original = paper_api.server_owned_refresh_tick
    paper_api.server_owned_refresh_tick = boom  # type: ignore[method-assign]
    try:
        set_value = client.put(
            "/paper/operator-scanner-settings",
            json={
                "min_net_edge": "0.0075",
                "outright_min_net_edge": "0.022",
                "max_execution_risk": 41,
                "hot_cadence_seconds": 20,
                "background_cadence_seconds": 90,
            },
        )
        assert set_value.status_code == 200
        body = set_value.json()["operator_settings"]
        assert Decimal(str(body["min_net_edge"])) == Decimal("0.0075")
        assert Decimal(str(body["outright_min_net_edge"])) == Decimal("0.022")
        omitted = client.put(
            "/paper/operator-scanner-settings",
            json={
                "min_net_edge": "0.008",
                "max_execution_risk": 41,
                "hot_cadence_seconds": 20,
                "background_cadence_seconds": 90,
            },
        )
        assert omitted.status_code == 200
        kept = omitted.json()["operator_settings"]
        assert Decimal(str(kept["min_net_edge"])) == Decimal("0.008")
        assert Decimal(str(kept["outright_min_net_edge"])) == Decimal("0.022")
        cleared = client.put(
            "/paper/operator-scanner-settings",
            json={
                "min_net_edge": "0.008",
                "outright_min_net_edge": None,
                "max_execution_risk": 41,
                "hot_cadence_seconds": 20,
                "background_cadence_seconds": 90,
            },
        )
        assert cleared.status_code == 200
        assert cleared.json()["operator_settings"]["outright_min_net_edge"] is None
        assert ticks == []
    finally:
        paper_api.server_owned_refresh_tick = original
        _unbind(coordinator, store)


def _scan_pair(
    *,
    minimum_net_edge: Decimal,
    market_scope: MarketScope = MarketScope.FIXTURE_MATCH,
    outright_min_net_edge: Decimal | None = None,
):
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    service = PaperScanService(intelligence)
    mb_event, mb_market = matchbook_payloads()
    matchbook = MatchbookObservationBuilder().build(
        mb_event, mb_market, observed_at=OBSERVED, quote_age_ms=120
    )
    kalshi = kalshi_btts_observation()
    try:
        return service.scan_pair(
            matchbook,
            kalshi,
            venue_costs=matchbook_kalshi_costs("0.02"),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            minimum_net_edge=minimum_net_edge,
            outright_min_net_edge=outright_min_net_edge,
            market_scope=market_scope,
            maximum_execution_risk=100,
        )
    finally:
        repository.close()


def test_fixture_scan_stamps_fixture_scope_and_ignores_outright_setting() -> None:
    decision = _scan_pair(
        minimum_net_edge=Decimal("0.50"),
        outright_min_net_edge=Decimal("0.001"),
        market_scope=MarketScope.FIXTURE_MATCH,
    )
    assert decision.min_net_edge_scope is MarketScope.FIXTURE_MATCH
    assert decision.min_net_edge_source == FIXTURE_MIN_NET_EDGE_SOURCE
    assert decision.min_net_edge_configured is True
    assert decision.minimum_net_edge == Decimal("0.50")
    assert "net_edge_below_threshold" in decision.rejection_reasons
    assert OUTRIGHT_MIN_NET_EDGE_UNCONFIGURED not in decision.rejection_reasons


def test_outright_scan_with_missing_threshold_fails_closed() -> None:
    decision = _scan_pair(
        minimum_net_edge=Decimal("0.001"),
        outright_min_net_edge=None,
        market_scope=MarketScope.COMPETITION_SEASON,
    )
    assert decision.min_net_edge_scope is MarketScope.COMPETITION_SEASON
    assert decision.min_net_edge_source == OUTRIGHT_MIN_NET_EDGE_SOURCE
    assert decision.min_net_edge_configured is False
    assert decision.minimum_net_edge is None
    assert OUTRIGHT_MIN_NET_EDGE_UNCONFIGURED in decision.rejection_reasons
    assert decision.eligible_for_paper_simulation is False


def test_outright_scan_uses_outright_threshold_not_fixture() -> None:
    decision = _scan_pair(
        minimum_net_edge=Decimal("0.001"),
        outright_min_net_edge=Decimal("0.50"),
        market_scope=MarketScope.COMPETITION_SEASON,
    )
    assert decision.minimum_net_edge == Decimal("0.50")
    assert decision.min_net_edge_scope is MarketScope.COMPETITION_SEASON
    assert decision.min_net_edge_source == OUTRIGHT_MIN_NET_EDGE_SOURCE
    assert "net_edge_below_threshold" in decision.rejection_reasons
    assert OUTRIGHT_MIN_NET_EDGE_UNCONFIGURED not in decision.rejection_reasons


def test_applied_threshold_and_scope_visible_on_opportunity_and_lifecycle() -> None:
    decision = _scan_pair(
        minimum_net_edge=Decimal("0.50"),
        market_scope=MarketScope.FIXTURE_MATCH,
    )
    observation = observation_from_paper_decision(decision)
    assert observation is not None
    observation.min_net_edge_scope = decision.min_net_edge_scope
    observation.min_net_edge_source = decision.min_net_edge_source
    observation.min_net_edge_configured = decision.min_net_edge_configured
    service = WatchlistService(SqliteWatchlistRepository())
    opportunity = service.observe(observation)
    assert opportunity.trigger_net_edge == Decimal("0.50")
    assert opportunity.min_net_edge_scope is MarketScope.FIXTURE_MATCH
    assert opportunity.min_net_edge_source == FIXTURE_MIN_NET_EDGE_SOURCE
    events = service.activity(limit=20)
    assert events
    stamped = [item for item in events if item.trigger_net_edge == Decimal("0.50")]
    assert stamped
    assert stamped[0].min_net_edge_scope == MarketScope.FIXTURE_MATCH.value
    assert stamped[0].min_net_edge_source == FIXTURE_MIN_NET_EDGE_SOURCE


def test_unconfigured_outright_watchlist_rejects() -> None:
    decision = _scan_pair(
        minimum_net_edge=Decimal("0.001"),
        market_scope=MarketScope.COMPETITION_SEASON,
        outright_min_net_edge=None,
    )
    observation = observation_from_paper_decision(decision)
    assert observation is not None
    assert observation.trigger_net_edge is None
    service = WatchlistService(SqliteWatchlistRepository())
    opportunity = service.observe(observation)
    assert opportunity.status is OpportunityStatus.REJECTED
    assert OUTRIGHT_MIN_NET_EDGE_UNCONFIGURED in opportunity.rejection_reasons


def test_active_trade_top_up_uses_stamped_trigger_not_live_fixture(
    tmp_path: Path,
) -> None:
    src = inspect.getsource(PaperOperationsService._maybe_top_up_open_trade_locked)
    assert "plan.decision.minimum_net_edge" in src
    assert "trigger = operator.min_net_edge" not in src
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    store = _raise_trade_cap(tmp_path, Decimal("2000"))
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        plan = ops._plans[trade.opportunity_id]
        asserted = plan.decision.minimum_net_edge
        assert asserted is not None
        assert asserted < Decimal("0.99")
        store.save_settings(
            min_net_edge=Decimal("0.99"),
            max_execution_risk=60,
            hot_cadence_seconds=30,
            max_allocated_per_trade_gbp=Decimal("2000"),
        )
        result = ops.maybe_top_up_open_trade(
            trade,
            now=TRADE_OBSERVED,
            plan=authorised_execution_plan(ops._plans[trade.opportunity_id], "outright-trigger"),
            require_current_plan=True,
        )
        assert result is not None
        loaded = ops.list_active_trades()[0]
        assert any(item.kind is PaperTradeTrancheKind.TOP_UP for item in loaded.tranches)
    finally:
        repository.close()
        ledger.close()
        store.close()
        bind_runtime_operator_scanner_settings_store(None)
        reset_active_trade_registry()


def test_active_trade_retains_stamped_outright_trigger_when_fixture_slider_changes(
    tmp_path: Path,
) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    store = _raise_trade_cap(tmp_path, Decimal("2000"))
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        plan = ops._plans[trade.opportunity_id]
        plan.decision.minimum_net_edge = Decimal("0.99")
        plan.decision.min_net_edge_scope = MarketScope.COMPETITION_SEASON
        plan.decision.min_net_edge_source = OUTRIGHT_MIN_NET_EDGE_SOURCE
        store.save_settings(
            min_net_edge=Decimal("0.001"),
            max_execution_risk=60,
            hot_cadence_seconds=30,
            max_allocated_per_trade_gbp=Decimal("2000"),
        )
        stamped = ops._plans[trade.opportunity_id]
        ops.maybe_top_up_open_trade(
            trade,
            now=TRADE_OBSERVED,
            plan=authorised_execution_plan(stamped, "outright-exit"),
            require_current_plan=True,
        )
        loaded = ops.list_active_trades()[0]
        assert loaded.active_trade_phase is PaperActiveTradePhase.EXIT_MANAGEMENT
        assert not any(item.kind is PaperTradeTrancheKind.TOP_UP for item in loaded.tranches)
    finally:
        repository.close()
        ledger.close()
        store.close()
        bind_runtime_operator_scanner_settings_store(None)
        reset_active_trade_registry()
