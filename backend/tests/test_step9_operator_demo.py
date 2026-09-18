from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sports_hedge.accounting.paper_journal import DataProvenance
from sports_hedge.api.main import app
from sports_hedge.api.paper import get_demo_walkthrough_service
from sports_hedge.application.complete_set import SOLVER_MODEL_GENERALIZED, SOLVER_MODEL_SIMPLE
from sports_hedge.application.demo_fixtures import DEMO_DATA_KIND, DEMO_FIXTURE_LABEL, DEMO_FX, tighten_reverse_quotes
from sports_hedge.paper.unwind.models import UnwindPolicy
from sports_hedge.application.demo_walkthrough import (
    DemoCloseRequest,
    DemoResetRequest,
    DemoWalkthroughService,
    FixtureReplayRequest,
)
from sports_hedge.application.paper_operations import PaperOperationsError, PaperOperationsService
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.trades import PaperLegFillKind, PaperTradeState
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.application.demo_launcher_pid import decide_demo_stop_action
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient

SEED = Decimal("1000")
FX = Decimal("0.80")
USD_SEED = (SEED / FX).quantize(Decimal("0.00000001"))
REPO_ROOT = Path(__file__).resolve().parents[2]


def _settings() -> Settings:
    return Settings(
        max_slippage_bps=0,
        fx_spread_bps=0,
        simulated_latency_ms=0,
        paper_autofill_enabled=False,
        paper_treasury_seed_gbp=1000,
        paper_treasury_demo_usd_gbp_per_unit=0.80,
        paper_treasury_demo_fx_source="paper_demo_fx_snapshot",
    )


def _bundle(tmp_path: Path):
    ledger = SqlitePaperLedger(
        tmp_path / "paper.sqlite",
        seed_gbp=SEED,
        usd_gbp_per_unit=FX,
        fx_source="paper_demo_fx_snapshot",
    )
    settings = _settings()
    repository = SqliteMarketIntelligenceRepository()
    scan = PaperScanService(MarketIntelligenceService(repository), settings=settings)
    watchlist = WatchlistService(SqliteWatchlistRepository(), max_quote_age_ms=10_000)
    ops = PaperOperationsService(
        watchlist=watchlist,
        alerts=PriorityAlertService(),
        settings=settings,
        ledger=ledger,
    )
    demo = DemoWalkthroughService(
        operations=ops,
        scan=scan,
        watchlist=watchlist,
        ledger=ledger,
        settings=settings,
    )
    return demo, ops, watchlist, ledger, repository


def _assert_three_pools(snapshot) -> None:
    by_venue = {pool.venue: pool for pool in snapshot.pools}
    assert set(by_venue) == {VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI}
    mb = by_venue[VenueName.MATCHBOOK]
    pm = by_venue[VenueName.POLYMARKET]
    ks = by_venue[VenueName.KALSHI]
    assert mb.native_currency == "GBP"
    assert mb.seed_native == SEED
    assert mb.available_cash == SEED
    assert mb.locked_capital == 0
    assert pm.native_currency == "USD"
    assert ks.native_currency == "USD"
    assert pm.seed_native == USD_SEED
    assert ks.seed_native == USD_SEED
    assert pm.available_cash == USD_SEED
    assert ks.available_cash == USD_SEED
    assert pm.gbp_carrying_value == SEED
    assert ks.gbp_carrying_value == SEED
    assert pm.fx_source == "paper_demo_fx_snapshot"
    assert ks.fx_source == "paper_demo_fx_snapshot"


def test_fresh_demo_reset_seeds_three_separated_pools(tmp_path: Path) -> None:
    demo, ops, watchlist, ledger, repository = _bundle(tmp_path)
    try:
        snapshot = demo.reset(DemoResetRequest(reason="fresh start"))
        _assert_three_pools(snapshot)
        assert snapshot.execution_enabled is False
        assert snapshot.paper_only is True
        assert snapshot.data_kind == "live_paper"
        health = TestClient(app).get("/health").json()
        assert health["execution_enabled"] is False
        assert health["mode"] == "paper"
    finally:
        repository.close()
        ledger.close()


def test_reset_fails_closed_while_open_then_reinitialize_reseeds(tmp_path: Path) -> None:
    demo, ops, watchlist, ledger, repository = _bundle(tmp_path)
    try:
        opened = demo.replay(
            FixtureReplayRequest(venue_pair="matchbook_polymarket", close_via="hold")
        )
        assert opened.trade is not None
        assert opened.trade.state is PaperTradeState.OPEN
        locked_before = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP").locked_capital
        assert locked_before > 0
        with pytest.raises(PaperOperationsError, match="active_treasury_locks|open_paper_positions"):
            demo.reset(DemoResetRequest(reinitialize_store=False))
        assert ops.list_active_trades()
        assert ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP").locked_capital == locked_before
        snapshot = demo.reset(
            DemoResetRequest(
                reinitialize_store=True,
                reason="explicit operator demo store reinitialize",
            )
        )
        _assert_three_pools(snapshot)
        assert ops.list_active_trades() == []
        abandoned = [trade for trade in ops.list_closed_trades() if trade.settlement_source == "demo_reset"]
        assert abandoned
        assert all(trade.realised_pnl_gbp is None for trade in abandoned)
    finally:
        repository.close()
        ledger.close()


def test_fixture_replay_is_labelled_and_filtered_from_live_watchlists(tmp_path: Path) -> None:
    demo, ops, watchlist, ledger, repository = _bundle(tmp_path)
    try:
        result = demo.replay(FixtureReplayRequest(close_via="hold"))
        assert result.label == DEMO_FIXTURE_LABEL
        assert result.data_kind == DEMO_DATA_KIND
        assert result.trade is not None
        stored = watchlist.repository.get(result.trade.opportunity_id)
        assert stored is not None
        assert stored.data_kind == DEMO_DATA_KIND
        assert watchlist.top_near() == []
        assert watchlist.triggered() == []
        snapshot = demo.snapshot()
        assert snapshot.live_near == []
        assert snapshot.live_triggered == []
        assert any("DEMO / FIXTURE" in note for note in snapshot.notes)
    finally:
        repository.close()
        ledger.close()


def test_simple_mb_pm_opens_after_locks_then_settles(tmp_path: Path) -> None:
    demo, ops, watchlist, ledger, repository = _bundle(tmp_path)
    try:
        before = ledger.treasury.snapshot()
        opened = demo.replay(
            FixtureReplayRequest(venue_pair="matchbook_polymarket", solver="simple", close_via="hold")
        )
        trade = opened.trade
        assert trade is not None
        assert trade.state is PaperTradeState.OPEN
        assert trade.places_orders is False
        assert opened.decision is not None
        assert opened.decision.solver_model == SOLVER_MODEL_SIMPLE
        assert trade.guaranteed_profit_gbp_at_open is not None
        kinds = {leg.fill_kind for leg in trade.legs}
        assert PaperLegFillKind.INTERNAL_SIMULATED in kinds
        assert PaperLegFillKind.PAPER_SIMULATED_EXTERNAL in kinds
        assert PaperLegFillKind.MANUAL_EXTERNAL not in kinds
        after_open = ledger.treasury.snapshot()
        assert after_open.pool(VenueName.MATCHBOOK, "GBP").locked_capital > 0
        assert after_open.pool(VenueName.POLYMARKET, "USD").locked_capital > 0
        assert after_open.pool(VenueName.KALSHI, "USD").locked_capital == 0
        assert after_open.pool(VenueName.KALSHI, "USD").available_cash == before.pool(
            VenueName.KALSHI, "USD"
        ).available_cash
        if opened.unwind is not None:
            assert opened.unwind.estimated_time_to_release.settles_or_releases_capital is False
            assert opened.unwind.spendable is False
        closed = demo.close_open_trade(trade.trade_id, DemoCloseRequest(close_via="settlement"))
        assert closed.trade is not None
        assert closed.trade.state is PaperTradeState.CLOSED
        assert closed.trade.realised_pnl_gbp is not None
        assert closed.journal_balanced is True
        released = ledger.treasury.snapshot()
        assert released.pool(VenueName.MATCHBOOK, "GBP").locked_capital == 0
        assert released.pool(VenueName.POLYMARKET, "USD").locked_capital == 0
        assert released.execution_enabled is False
    finally:
        repository.close()
        ledger.close()


def test_generalized_replay_has_no_fabricated_implied_sum(tmp_path: Path) -> None:
    demo, ops, watchlist, ledger, repository = _bundle(tmp_path)
    try:
        result = demo.replay(
            FixtureReplayRequest(
                venue_pair="matchbook_polymarket",
                solver="generalized",
                close_via="hold",
            )
        )
        assert result.trade is not None
        assert result.decision is not None
        assert result.decision.solver_model == SOLVER_MODEL_GENERALIZED
        stored = watchlist.repository.get(result.trade.opportunity_id)
        assert stored is not None
        assert stored.implied_probability_sum is None
        assert result.trade.state is PaperTradeState.OPEN
        assert all(leg.filled_stake > 0 for leg in result.trade.legs)
    finally:
        repository.close()
        ledger.close()


def test_generalized_replay_rejected_for_unsupported_venue_pair(tmp_path: Path) -> None:
    demo, _ops, _watchlist, ledger, repository = _bundle(tmp_path)
    get_demo_walkthrough_service.cache_clear()
    app.dependency_overrides[get_demo_walkthrough_service] = lambda: demo
    client = TestClient(app)
    try:
        for pair in ("matchbook_kalshi", "polymarket_kalshi"):
            denied = client.post(
                "/paper/demo/fixture-replay",
                json={"venue_pair": pair, "solver": "generalized", "close_via": "hold"},
            )
            assert denied.status_code == 422
            assert "Matchbook" in str(denied.json()["detail"])
    finally:
        app.dependency_overrides.clear()
        get_demo_walkthrough_service.cache_clear()
        repository.close()
        ledger.close()


def test_mb_kalshi_internal_fills_and_settlement(tmp_path: Path) -> None:
    demo, ops, watchlist, ledger, repository = _bundle(tmp_path)
    try:
        result = demo.replay(
            FixtureReplayRequest(venue_pair="matchbook_kalshi", close_via="settlement")
        )
        assert result.trade is not None
        assert result.trade.state is PaperTradeState.CLOSED
        venues = {leg.venue for leg in result.trade.legs}
        assert venues == {VenueName.MATCHBOOK, VenueName.KALSHI}
        kinds = {leg.fill_kind for leg in result.trade.legs}
        assert kinds == {PaperLegFillKind.INTERNAL_SIMULATED}
        assert result.journal_balanced is True
        snap = ledger.treasury.snapshot()
        assert snap.pool(VenueName.MATCHBOOK, "GBP").locked_capital == 0
        assert snap.pool(VenueName.KALSHI, "USD").locked_capital == 0
    finally:
        repository.close()
        ledger.close()


def test_pm_kalshi_without_matchbook_uses_simulated_external(tmp_path: Path) -> None:
    demo, ops, watchlist, ledger, repository = _bundle(tmp_path)
    try:
        result = demo.replay(
            FixtureReplayRequest(venue_pair="polymarket_kalshi", close_via="settlement")
        )
        assert result.trade is not None
        venues = {leg.venue for leg in result.trade.legs}
        assert venues == {VenueName.POLYMARKET, VenueName.KALSHI}
        by_venue = {leg.venue: leg.fill_kind for leg in result.trade.legs}
        assert by_venue[VenueName.POLYMARKET] is PaperLegFillKind.PAPER_SIMULATED_EXTERNAL
        assert by_venue[VenueName.KALSHI] is PaperLegFillKind.INTERNAL_SIMULATED
        assert result.trade.state is PaperTradeState.CLOSED
    finally:
        repository.close()
        ledger.close()


def test_repeated_simulate_does_not_duplicate_open_trade(tmp_path: Path) -> None:
    demo, ops, watchlist, ledger, repository = _bundle(tmp_path)
    try:
        opened = demo.replay(FixtureReplayRequest(close_via="hold"))
        assert opened.trade is not None
        first_id = opened.trade.trade_id
        locked = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP").locked_capital
        ops.simulate_fill(
            opened.trade.opportunity_id,
            simulate_external=True,
            operator_note="repeat refresh",
        )
        active = ops.list_active_trades()
        assert len(active) == 1
        assert active[0].trade_id == first_id
        assert ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP").locked_capital == locked
        demo.close_open_trade(opened.trade.trade_id, DemoCloseRequest(close_via="settlement"))
        second = demo.replay(FixtureReplayRequest(close_via="hold"))
        assert second.trade is not None
        assert second.trade.trade_id != first_id
        assert second.trade.opportunity_id != opened.trade.opportunity_id
    finally:
        repository.close()
        ledger.close()


def test_qualify_only_then_confirm_ten_pounds_then_hold_unwind_excludes_settlement(
    tmp_path: Path,
) -> None:
    demo, ops, watchlist, ledger, repository = _bundle(tmp_path)
    try:
        ten = Decimal("10")
        before = ledger.treasury.snapshot()
        qualified = demo.replay(FixtureReplayRequest(close_via="hold", qualify_only=True))
        assert qualified.trade is None
        assert qualified.qualify_only is True
        assert qualified.opportunity_id
        assert qualified.preparable_opportunities
        after_preview_seed = ledger.treasury.snapshot()
        assert after_preview_seed.pool(VenueName.MATCHBOOK, "GBP").locked_capital == before.pool(
            VenueName.MATCHBOOK, "GBP"
        ).locked_capital
        preview = ops.prepare_fixed_deployment(qualified.opportunity_id, ten)
        assert preview.accepted is True
        assert preview.prepared_deployment_id
        assert preview.locks_treasury is False
        still = ledger.treasury.snapshot()
        assert still.pool(VenueName.MATCHBOOK, "GBP").available_cash == before.pool(
            VenueName.MATCHBOOK, "GBP"
        ).available_cash
        opened = ops.simulate_fill(
            qualified.opportunity_id,
            simulate_external=True,
            prepared_deployment_id=preview.prepared_deployment_id,
            requested_size_gbp=ten,
            provenance=DataProvenance.FIXTURE_DEMO,
        )
        assert opened.entry_complete is True
        trade = ops.list_active_trades()[0]
        preview_stakes = {(leg.venue, leg.outcome): leg.stake_native for leg in preview.legs}
        trade_stakes = {(leg.venue, leg.outcome): leg.filled_stake for leg in trade.legs}
        assert trade_stakes == preview_stakes
        hold_locked = dict(trade.capital_locked_native)
        snap = demo.snapshot()
        assert snap.hold_vs_unwind is not None
        assert ops.list_active_trades()[0].capital_locked_native == hold_locked
        closed = demo.close_open_trade(trade.trade_id, DemoCloseRequest(close_via="unwind"))
        assert closed.trade is not None
        assert closed.trade.state is PaperTradeState.CLOSED
        retry = demo.close_open_trade(trade.trade_id, DemoCloseRequest(close_via="unwind"))
        assert retry.trade is not None
        assert retry.trade.trade_id == trade.trade_id
        with pytest.raises(PaperOperationsError):
            demo.close_open_trade(trade.trade_id, DemoCloseRequest(close_via="settlement"))
        report = ledger.reconcile()
        assert report.ok
    finally:
        repository.close()
        ledger.close()


def test_labelled_fixture_replay_does_not_masquerade_as_live_auto_capture(tmp_path: Path) -> None:
    settings = Settings(
        max_slippage_bps=0,
        fx_spread_bps=0,
        simulated_latency_ms=0,
        paper_autofill_enabled=True,
        paper_treasury_seed_gbp=1000,
        paper_treasury_demo_usd_gbp_per_unit=0.80,
        paper_treasury_demo_fx_source="paper_demo_fx_snapshot",
    )
    ledger = SqlitePaperLedger(
        tmp_path / "paper.sqlite",
        seed_gbp=SEED,
        usd_gbp_per_unit=FX,
        fx_source="paper_demo_fx_snapshot",
    )
    repository = SqliteMarketIntelligenceRepository()
    watchlist = WatchlistService(SqliteWatchlistRepository(), max_quote_age_ms=10_000)
    ops = PaperOperationsService(
        watchlist=watchlist,
        alerts=PriorityAlertService(),
        settings=settings,
        ledger=ledger,
    )
    demo = DemoWalkthroughService(
        operations=ops,
        scan=PaperScanService(MarketIntelligenceService(repository), settings=settings),
        watchlist=watchlist,
        ledger=ledger,
        settings=settings,
    )
    try:
        before = ledger.treasury.snapshot()
        qualified = demo.replay(FixtureReplayRequest(close_via="hold", qualify_only=True))
        assert qualified.trade is None
        assert qualified.qualify_only is True
        assert ops.list_active_trades() == []
        stored = watchlist.repository.get(qualified.opportunity_id)
        assert stored is not None
        assert stored.data_kind == DEMO_DATA_KIND
        fill_journals = [
            entry
            for entry in ops.journal.list_entries()
            if entry.source not in {"paper_treasury_seed"}
        ]
        assert fill_journals == []
        after = ledger.treasury.snapshot()
        assert after.pool(VenueName.MATCHBOOK, "GBP").locked_capital == before.pool(
            VenueName.MATCHBOOK, "GBP"
        ).locked_capital
        assert after.pool(VenueName.POLYMARKET, "USD").locked_capital == before.pool(
            VenueName.POLYMARKET, "USD"
        ).locked_capital
    finally:
        repository.close()
        ledger.close()


def test_qualify_ten_pounds_accepts_with_production_fx_and_slippage(tmp_path: Path) -> None:
    settings = Settings(
        paper_treasury_seed_gbp=1000,
        paper_treasury_demo_usd_gbp_per_unit=0.80,
        paper_treasury_demo_fx_source="paper_demo_fx_snapshot",
        paper_autofill_enabled=False,
    )
    assert settings.max_slippage_bps == 25
    assert settings.fx_spread_bps == 10
    ledger = SqlitePaperLedger(
        tmp_path / "paper.sqlite",
        seed_gbp=SEED,
        usd_gbp_per_unit=FX,
        fx_source="paper_demo_fx_snapshot",
    )
    repository = SqliteMarketIntelligenceRepository()
    watchlist = WatchlistService(SqliteWatchlistRepository(), max_quote_age_ms=10_000)
    ops = PaperOperationsService(
        watchlist=watchlist,
        alerts=PriorityAlertService(),
        settings=settings,
        ledger=ledger,
    )
    demo = DemoWalkthroughService(
        operations=ops,
        scan=PaperScanService(MarketIntelligenceService(repository), settings=settings),
        watchlist=watchlist,
        ledger=ledger,
        settings=settings,
    )
    try:
        qualified = demo.replay(FixtureReplayRequest(close_via="hold", qualify_only=True))
        preview = ops.prepare_fixed_deployment(qualified.opportunity_id, Decimal("10"))
        assert preview.accepted is True, preview.rejection_reason
        assert preview.applied_size_gbp == Decimal("10")
        assert preview.native_requirements_reconciled is True
        assert preview.resized is False
    finally:
        repository.close()
        ledger.close()


def test_aged_demo_confirm_keeps_fixture_provenance_and_ten_pound_legs(tmp_path: Path) -> None:
    """Production watchlist cap is 1000ms; labelled replay must still confirm after operator delay."""

    settings = Settings(
        paper_treasury_seed_gbp=1000,
        paper_treasury_demo_usd_gbp_per_unit=0.80,
        paper_treasury_demo_fx_source="paper_demo_fx_snapshot",
        paper_autofill_enabled=False,
    )
    ledger = SqlitePaperLedger(
        tmp_path / "paper.sqlite",
        seed_gbp=SEED,
        usd_gbp_per_unit=FX,
        fx_source="paper_demo_fx_snapshot",
    )
    repository = SqliteMarketIntelligenceRepository()
    watchlist = WatchlistService(SqliteWatchlistRepository(), max_quote_age_ms=1000)
    ops = PaperOperationsService(
        watchlist=watchlist,
        alerts=PriorityAlertService(),
        settings=settings,
        ledger=ledger,
    )
    demo = DemoWalkthroughService(
        operations=ops,
        scan=PaperScanService(MarketIntelligenceService(repository), settings=settings),
        watchlist=watchlist,
        ledger=ledger,
        settings=settings,
    )
    try:
        ten = Decimal("10")
        qualified = demo.replay(FixtureReplayRequest(close_via="hold", qualify_only=True))
        stored = watchlist.repository.get(qualified.opportunity_id)
        assert stored is not None
        assert stored.data_kind == DEMO_DATA_KIND
        preview = ops.prepare_fixed_deployment(qualified.opportunity_id, ten)
        assert preview.accepted is True, preview.rejection_reason
        aged = stored.last_seen_at + timedelta(seconds=5)
        opened = ops.simulate_fill(
            qualified.opportunity_id,
            simulate_external=True,
            prepared_deployment_id=preview.prepared_deployment_id,
            requested_size_gbp=ten,
            provenance=DataProvenance.LIVE_PAPER,
            now=aged,
        )
        assert opened.entry_complete is True
        trade = ops.list_active_trades()[0]
        assert trade.provenance is DataProvenance.FIXTURE_DEMO
        preview_stakes = {(leg.venue, leg.outcome): leg.stake_native for leg in preview.legs}
        trade_stakes = {(leg.venue, leg.outcome): leg.filled_stake for leg in trade.legs}
        assert trade_stakes == preview_stakes
        retry = ops.simulate_fill(
            qualified.opportunity_id,
            simulate_external=True,
            prepared_deployment_id=preview.prepared_deployment_id,
            requested_size_gbp=ten,
            now=aged + timedelta(seconds=1),
        )
        assert retry.trade_id == trade.trade_id
        assert retry.trace.detail is not None
        assert "idempotent" in retry.trace.detail
        assert len(ops.list_active_trades()) == 1
        mb_preview = next(leg.stake_native for leg in preview.legs if leg.venue is VenueName.MATCHBOOK)
        assert ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP").locked_capital == mb_preview
    finally:
        repository.close()
        ledger.close()


def test_ledger_reconciliation_api_is_read_only(tmp_path: Path) -> None:
    demo, _ops, _watchlist, ledger, repository = _bundle(tmp_path)
    get_demo_walkthrough_service.cache_clear()
    app.dependency_overrides[get_demo_walkthrough_service] = lambda: demo
    from sports_hedge.api.paper import get_paper_ledger

    app.dependency_overrides[get_paper_ledger] = lambda: ledger
    client = TestClient(app)
    try:
        response = client.get("/paper/ledger/reconciliation")
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["ok"] is True
        assert body["paper_only"] is True
        assert body["execution_enabled"] is False
        assert body["places_orders"] is False
        assert body["gbp_journals_balanced"] is True
    finally:
        app.dependency_overrides.clear()
        get_demo_walkthrough_service.cache_clear()
        repository.close()
        ledger.close()


def test_kalshi_unwind_fails_closed_without_inventing_fees(tmp_path: Path) -> None:
    demo, ops, watchlist, ledger, repository = _bundle(tmp_path)
    try:
        opened = demo.replay(
            FixtureReplayRequest(venue_pair="matchbook_kalshi", close_via="hold")
        )
        assert opened.trade is not None
        with pytest.raises(PaperOperationsError, match="unwind"):
            demo.close_open_trade(
                opened.trade.trade_id, DemoCloseRequest(close_via="unwind")
            )
        still_open = ops._get_trade_by_opportunity(opened.trade.opportunity_id)
        assert still_open is not None
        assert still_open.state is PaperTradeState.OPEN
        assert ledger.treasury.snapshot().pool(VenueName.KALSHI, "USD").locked_capital > 0
    finally:
        repository.close()
        ledger.close()


def test_validated_unwind_releases_mb_pm_locks(tmp_path: Path) -> None:
    demo, ops, watchlist, ledger, repository = _bundle(tmp_path)
    try:
        opened = demo.replay(
            FixtureReplayRequest(venue_pair="matchbook_polymarket", close_via="hold")
        )
        assert opened.trade is not None
        assert opened.unwind is not None
        assert opened.unwind.spendable is False
        assert opened.unwind.places_orders is False
        assert opened.unwind.estimated_time_to_release.settles_or_releases_capital is False
        before_locks = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP").locked_capital
        assert before_locks > 0
        quotes = tighten_reverse_quotes(opened.quotes)
        closed = ops.complete_validated_unwind(
            opened.trade.trade_id,
            quotes=quotes,
            fx=list(DEMO_FX),
            policy=UnwindPolicy(max_profit_give_up_gbp=Decimal("1000")),
        )
        assert closed.state is PaperTradeState.CLOSED
        assert closed.settlement_source == "paper_unwind"
        assert closed.realised_pnl_gbp is not None
        snap = ledger.treasury.snapshot()
        assert snap.pool(VenueName.MATCHBOOK, "GBP").locked_capital == 0
        assert snap.pool(VenueName.POLYMARKET, "USD").locked_capital == 0
    finally:
        repository.close()
        ledger.close()


def test_demo_api_reset_and_replay_and_places_orders_rejected(tmp_path: Path) -> None:
    demo, ops, watchlist, ledger, repository = _bundle(tmp_path)
    get_demo_walkthrough_service.cache_clear()
    app.dependency_overrides[get_demo_walkthrough_service] = lambda: demo
    client = TestClient(app)
    try:
        reset = client.post("/paper/demo/reset", json={"reason": "api start"})
        assert reset.status_code == 200
        body = reset.json()
        assert body["execution_enabled"] is False
        assert len(body["pools"]) == 3
        denied = client.post(
            "/paper/demo/fixture-replay",
            json={"places_orders": True, "close_via": "hold"},
        )
        assert denied.status_code == 422
        replay = client.post(
            "/paper/demo/fixture-replay",
            json={
                "venue_pair": "matchbook_polymarket",
                "solver": "simple",
                "close_via": "hold",
                "places_orders": False,
            },
        )
        assert replay.status_code == 200
        payload = replay.json()
        assert payload["label"] == DEMO_FIXTURE_LABEL
        assert payload["execution_enabled"] is False
        assert payload["trade"]["state"] == "OPEN"
        walk = client.get("/paper/demo/walkthrough")
        assert walk.status_code == 200
        assert walk.json()["live_triggered"] == []
    finally:
        app.dependency_overrides.clear()
        get_demo_walkthrough_service.cache_clear()
        repository.close()
        ledger.close()


def test_no_venue_write_paths_and_execution_disabled() -> None:
    for venue_cls in (MatchbookClient, PolymarketClient, KalshiClient):
        assert not hasattr(venue_cls, "place_order")
        assert not hasattr(venue_cls, "cancel_order")
        assert not hasattr(venue_cls, "sign")
    health = TestClient(app).get("/health").json()
    assert health["execution_enabled"] is False


def test_windows_launcher_scripts_encode_paper_only_contract() -> None:
    start_ps1 = (REPO_ROOT / "scripts/windows/Start-SportsHedge-Demo.ps1").read_text(encoding="utf-8")
    stop_ps1 = (REPO_ROOT / "scripts/windows/Stop-SportsHedge-Demo.ps1").read_text(encoding="utf-8")
    start_bat = (REPO_ROOT / "scripts/windows/Start-SportsHedge-Demo.bat").read_text(encoding="utf-8")
    stop_bat = (REPO_ROOT / "scripts/windows/Stop-SportsHedge-Demo.bat").read_text(encoding="utf-8")
    assert "Start-SportsHedge-Demo.ps1" in start_bat
    assert "Stop-SportsHedge-Demo.ps1" in stop_bat
    assert "SPORTS_HEDGE_EXECUTION_ENABLED" in start_ps1
    assert '"false"' in start_ps1
    assert "PAPER_AUTOFILL_ENABLED" in start_ps1
    assert '$env:PAPER_AUTOFILL_ENABLED = "true"' in start_ps1
    assert "AUTO PAPER CAPTURE ON" in start_ps1
    assert "PAPER_LIVE_REFRESH_ENABLED" in start_ps1
    assert '$env:PAPER_LIVE_REFRESH_ENABLED = "true"' in start_ps1
    assert '$env:ACCOUNTING_SCHEDULE_ENABLED = "true"' in start_ps1
    assert '$env:SPORTS_HEDGE_EXECUTION_ENABLED = "false"' in start_ps1
    assert '"true"' in start_ps1
    assert "WindowStyle Hidden" in start_ps1
    assert "/health" in start_ps1
    assert "demo-backend.pid" in start_ps1
    assert "demo-frontend.pid" in start_ps1
    assert "MessageBox" in start_ps1
    assert "127.0.0.1:3000/" in start_ps1
    assert "127.0.0.1:3000/demo" not in start_ps1
    assert "Wait-HttpOk -Url $BackendHealth -Label \"Sports Hedge backend\" | Out-Null" in start_ps1
    assert (
        "Wait-HttpOk -Url $FrontendHealth -Label \"Sports Hedge operator console\" | Out-Null"
        in start_ps1
    )
    identity_ps1 = (REPO_ROOT / "scripts/windows/Demo-LauncherIdentity.ps1").read_text(
        encoding="utf-8"
    )
    assert "command_tokens" in identity_ps1
    assert "ConvertTo-Json" in identity_ps1
    assert "git_head" in identity_ps1
    assert "repo_root" in identity_ps1
    assert "Get-DemoStartAction" in identity_ps1
    assert "Demo-LauncherIdentity.ps1" in start_ps1
    assert "Demo-LauncherIdentity.ps1" in stop_ps1
    assert "vercel" not in start_ps1.lower()
    assert "place_order" not in start_ps1
    assert "MATCHBOOK_PASSWORD" not in start_ps1
    assert "Stop-DemoPid" in stop_ps1
    assert "Stop-Process" in identity_ps1
    assert "Get-CimInstance" in identity_ps1
    assert "CommandLine" in identity_ps1
    assert "Test-DemoPidOwned" in identity_ps1
    assert "unrelated process was not killed" in identity_ps1
    assert "logs" in stop_ps1
    docs = (REPO_ROOT / "docs/DEMO_READINESS.md").read_text(encoding="utf-8")
    assert "PAPER_SIMULATED_EXTERNAL" in docs
    assert "DEMO / FIXTURE REPLAY" in docs
    assert "production readiness" in docs.lower()
    assert "Refresh Live Discovery" in docs
    assert "Tenet 18" in docs or "execution atomicity" in docs.lower()
    assert "ACCOUNTING_SCHEDULE_ENABLED" in docs
    runbook = (REPO_ROOT / "docs/DEMO_RUNBOOK.md").read_text(encoding="utf-8")
    assert "ACCOUNTING_SCHEDULE_ENABLED" in runbook
    assert "16:15" in runbook
    assert "paper_demo_fx_snapshot" in runbook
    assert "demo_fixture_replay" in runbook
    assert "Retry confirm" in runbook
    assert "already persisted" in runbook


def test_stale_demo_pid_is_not_killed() -> None:
    identity = {
        "pid": 4242,
        "path": r"C:\Sports-Hedge\backend\.venv\Scripts\python.exe",
        "command_tokens": ["uvicorn", "sports_hedge.api.main:app"],
    }
    assert (
        decide_demo_stop_action(
            identity,
            live_pid=4242,
            live_name="python",
            live_path=identity["path"],
            live_command_line=r'"C:\Sports-Hedge\backend\.venv\Scripts\python.exe" -m uvicorn sports_hedge.api.main:app --host 127.0.0.1 --port 8000',
        )
        == "stop"
    )
    assert (
        decide_demo_stop_action(
            identity,
            live_pid=4242,
            live_name="notepad",
            live_path=r"C:\Windows\System32\notepad.exe",
            live_command_line=r"C:\Windows\System32\notepad.exe",
        )
        == "stale"
    )
    assert decide_demo_stop_action(identity, live_pid=None) == "missing"
    assert (
        decide_demo_stop_action(
            {"pid": 4242},
            live_pid=4242,
            live_name="python",
            live_path=identity["path"],
            live_command_line="python -m uvicorn sports_hedge.api.main:app",
        )
        == "stale"
    )
    identity_ps1 = (REPO_ROOT / "scripts/windows/Demo-LauncherIdentity.ps1").read_text(
        encoding="utf-8"
    )
    stop_index = identity_ps1.index("Stop-Process -Id")
    assert identity_ps1.index("Test-DemoPidOwned") < stop_index
    assert identity_ps1.index('$action -ne "stop"') < stop_index


def test_demo_operator_surface_wires_live_discovery_and_solver_guard() -> None:
    ui = (REPO_ROOT / "frontend/components/demo-walkthrough.tsx").read_text(encoding="utf-8")
    assert "runPaperCollection" in ui
    assert "Refresh Live Discovery" in ui
    assert "getLiveRefreshStatus" in ui
    assert "setInterval" in ui
    assert 'disabled={!pairSupportsGeneralized(pair)}' in ui
    assert 'pair === "matchbook_polymarket"' in ui
    assert "effectiveSolver" in ui
    assert "DEMO / FIXTURE REPLAY" in ui
