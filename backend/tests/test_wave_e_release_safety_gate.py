"""Wave E release-safety gate (#171 items 17 + 18).

Independent validation of LIVE_PAPER auto-capture and the hard
no-live-execution boundary on the consolidated Wave B head. These tests
do not weaken freshness, equivalence, economics, allocator, or treasury
gates. They add persist-seam coverage where the fail-closed matrix was
only proven at scan/allocator seams.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from sports_hedge.accounting.paper_journal import DataProvenance
from sports_hedge.api.main import app
from sports_hedge.application.executable_liquidity import DEFAULT_OPENING_MAX_QUOTE_AGE_MS
from sports_hedge.application.market_observation import PolymarketObservationBuilder
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import (
    DEFAULT_EXECUTABLE_QUOTE_AGE_MS,
    DEFAULT_HOT_TTL_SECONDS,
    DEFAULT_UNIVERSE_TTL_SECONDS,
    ScanLane,
    freshness_class,
)
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.watchlist.models import OpportunityClassification, OpportunityStatus
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.liquidity import PaperLiquiditySnapshot, default_pools
from sports_hedge.paper.trades import PaperLegFillKind, PaperTradeState
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.venues import KalshiClient, MatchbookClient, PolymarketClient
from test_paper_scan_pipeline import OBSERVED, polymarket_payloads
from test_step8f_automatic_paper_entry import (
    FX,
    _assert_allocator_sized,
    _matchbook_btts,
    _kalshi_btts,
    _kalshi_costs,
    _standing,
)

REPO = Path(__file__).resolve().parents[2]
FRONTEND = REPO / "frontend"
VENUE_DIR = Path(__file__).resolve().parents[1] / "src" / "sports_hedge" / "venues"
WRITE_TOKENS = (
    "place_order",
    "cancel_order",
    "sign_order",
    "submit_order",
    "sign_wallet",
    "place_bet",
)
GEO_TOKENS = ("geobypass", "geo-bypass", "vpn", "location-mask")


def _empty_standing() -> PaperLiquiditySnapshot:
    return PaperLiquiditySnapshot(
        pools=default_pools(
            matchbook_gbp=Decimal("0"),
            polymarket_usd=Decimal("0"),
            kalshi_usd=Decimal("0"),
        ),
        updated_at=OBSERVED,
    )


def _ops(tmp_path: Path, *, autofill: bool = True):
    ledger = SqlitePaperLedger(
        tmp_path / "paper.sqlite",
        seed_gbp=Decimal("1000"),
        usd_gbp_per_unit=Decimal("0.75"),
        fx_source="test",
    )
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    settings = Settings(
        max_slippage_bps=0,
        fx_spread_bps=0,
        simulated_latency_ms=0,
        paper_autofill_enabled=autofill,
    )
    scan = PaperScanService(intelligence, settings=settings)
    # Production default: executable quote freshness is 1000ms, not radar TTL.
    watchlist = WatchlistService(SqliteWatchlistRepository())
    ops = PaperOperationsService(
        watchlist=watchlist,
        alerts=PriorityAlertService(),
        settings=settings,
        ledger=ledger,
    )
    return scan, watchlist, ops, repository, ledger


def _scan(
    scan: PaperScanService,
    *,
    left=None,
    right=None,
    venue_costs=None,
    fx_snapshots=FX,
    maximum_execution_risk: int = 100,
    liquidity_snapshot=None,
):
    return scan.scan_pair(
        left if left is not None else _matchbook_btts(),
        right if right is not None else _kalshi_btts(),
        venue_costs=venue_costs if venue_costs is not None else _kalshi_costs(),
        fx_snapshots=fx_snapshots,
        maximum_execution_risk=maximum_execution_risk,
        liquidity_snapshot=_standing() if liquidity_snapshot is None else liquidity_snapshot,
    )


def _history(scan, decision):
    if not decision.canonical_market_id:
        return []
    return scan.market_intelligence.market_history(
        canonical_market_id=decision.canonical_market_id
    )


def _observe_persist(scan, watchlist, ops, decision, *, provenance=DataProvenance.LIVE_PAPER, **kwargs):
    watchlist.observe_paper_decision(decision, _history(scan, decision))
    ops.persist_triggered_chain(decision, provenance=provenance, **kwargs)
    return decision


def _fill_journals(ops) -> list:
    return [
        entry
        for entry in ops.journal.list_entries()
        if entry.source not in {"paper_treasury_seed"}
    ]


def _assert_zero_capture(ops, ledger) -> None:
    assert ops.list_active_trades() == []
    assert _fill_journals(ops) == []
    snap = ledger.treasury.snapshot()
    for venue, currency in (
        (VenueName.MATCHBOOK, "GBP"),
        (VenueName.POLYMARKET, "USD"),
        (VenueName.KALSHI, "USD"),
    ):
        assert snap.pool(venue, currency).locked_capital == 0


def test_wave_e_positive_live_paper_chain_opens_once_with_locks_and_journal(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops(tmp_path)
    try:
        before = ledger.treasury.snapshot()
        decision = _scan(scan)
        assert decision.market_match.matched is True
        assert decision.market_match.confidence == 1.0
        assert decision.eligible_for_paper_simulation is True, decision.rejection_reasons
        assert decision.depth_scan is not None
        assert decision.depth_scan.solution.is_arbitrage is True
        assert decision.venue_costs
        assert all(cost.is_economically_known() for cost in decision.venue_costs)
        assert any(snapshot.currency == "USD" for snapshot in decision.fx_snapshots)
        assert decision.execution_risk is not None
        assert decision.execution_risk.score <= decision.maximum_execution_risk
        assert decision.quote_age_ms is not None
        assert decision.quote_age_ms < DEFAULT_EXECUTABLE_QUOTE_AGE_MS
        assert decision.quote_age_ms < DEFAULT_OPENING_MAX_QUOTE_AGE_MS
        assert decision.quote_age_ms < DEFAULT_HOT_TTL_SECONDS * 1000
        assert decision.allocation is not None and decision.allocation.accepted
        assert decision.allocation.recommended_size > 0
        assert freshness_class(
            lane=ScanLane.HOT,
            last_scanned_at=decision.scanned_at,
            now=decision.scanned_at,
            quote_age_ms=decision.quote_age_ms,
        ) == "executable"

        watchlist.observe_paper_decision(decision, _history(scan, decision))
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER)

        active = ops.list_active_trades()
        assert len(active) == 1
        trade = active[0]
        assert trade.state is PaperTradeState.OPEN
        assert trade.provenance is DataProvenance.LIVE_PAPER
        assert trade.paper_only is True
        assert trade.places_orders is False
        kinds = {leg.fill_kind for leg in trade.legs}
        assert kinds == {PaperLegFillKind.INTERNAL_SIMULATED}
        assert PaperLegFillKind.MANUAL_EXTERNAL not in kinds
        _assert_allocator_sized(ops, trade)

        after = ledger.treasury.snapshot()
        locked_gbp = next(leg.filled_stake for leg in trade.legs if leg.venue is VenueName.MATCHBOOK)
        locked_usd = next(leg.filled_stake for leg in trade.legs if leg.venue is VenueName.KALSHI)
        mb = after.pool(VenueName.MATCHBOOK, "GBP")
        ks = after.pool(VenueName.KALSHI, "USD")
        assert locked_gbp > 0 and locked_usd > 0
        assert mb.locked_capital == locked_gbp
        assert mb.available_cash == before.pool(VenueName.MATCHBOOK, "GBP").available_cash - locked_gbp
        assert ks.locked_capital == locked_usd
        assert ks.available_cash == before.pool(VenueName.KALSHI, "USD").available_cash - locked_usd
        assert after.pool(VenueName.POLYMARKET, "USD").locked_capital == 0

        journals = _fill_journals(ops)
        assert journals
        lock_sources = {entry.source for entry in journals}
        assert "paper_fill_simulator" in lock_sources or "paper_simulated_external" in lock_sources
        assert all(entry.provenance is DataProvenance.LIVE_PAPER for entry in journals)
        assert all(entry.trade_id == trade.trade_id for entry in journals)
        native_locked = {
            (posting.dimensions.venue, posting.dimensions.currency): posting.amount_native
            for entry in journals
            for posting in entry.postings
            if "LOCKED" in posting.account_code
        }
        assert native_locked[VenueName.MATCHBOOK, "GBP"] == locked_gbp
        assert native_locked[VenueName.KALSHI, "USD"] == locked_usd

        health = TestClient(app).get("/health").json()
        venues = TestClient(app).get("/venues").json()
        assert health["mode"] == "paper"
        assert health["execution_enabled"] is False
        assert {row["venue"]: row["capabilities"]["execution_enabled"] for row in venues} == {
            "matchbook": False,
            "polymarket": False,
            "kalshi": False,
            "smarkets": False,
        }
        assert {row["venue"]: row["integration"] for row in venues}["matchbook"] == "official_api"
        assert {row["venue"]: row["integration"] for row in venues}["polymarket"] == "public_market_data"
        assert {row["venue"]: row["integration"] for row in venues}["kalshi"] == "official_api"
    finally:
        repository.close()
        ledger.close()


def test_wave_e_repeated_hot_observations_are_idempotent(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops(tmp_path)
    try:
        decision = _scan(scan)
        watchlist.observe_paper_decision(decision, _history(scan, decision))
        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER)
        first = ops.list_active_trades()[0]
        first_journals = list(ops.journal.list_entries())
        first_snap = ledger.treasury.snapshot()
        realised_before = first.realised_pnl_gbp
        for _ in range(3):
            watchlist.observe_paper_decision(decision, _history(scan, decision))
            ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER)
        active = ops.list_active_trades()
        assert len(active) == 1
        assert active[0].trade_id == first.trade_id
        assert active[0].state is PaperTradeState.OPEN
        assert active[0].realised_pnl_gbp == realised_before
        assert len(ops.journal.list_entries()) == len(first_journals)
        after = ledger.treasury.snapshot()
        for venue, currency in (
            (VenueName.MATCHBOOK, "GBP"),
            (VenueName.POLYMARKET, "USD"),
            (VenueName.KALSHI, "USD"),
        ):
            assert after.pool(venue, currency).locked_capital == first_snap.pool(
                venue, currency
            ).locked_capital
            assert after.pool(venue, currency).available_cash == first_snap.pool(
                venue, currency
            ).available_cash
    finally:
        repository.close()
        ledger.close()


def test_wave_e_stale_quote_inside_radar_ttl_does_not_auto_open(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops(tmp_path)
    try:
        radar_age_ms = 5_000
        assert DEFAULT_EXECUTABLE_QUOTE_AGE_MS == 1000
        assert DEFAULT_OPENING_MAX_QUOTE_AGE_MS == 2000
        assert DEFAULT_HOT_TTL_SECONDS == 90
        assert radar_age_ms > DEFAULT_OPENING_MAX_QUOTE_AGE_MS
        assert radar_age_ms < DEFAULT_HOT_TTL_SECONDS * 1000
        assert freshness_class(
            lane=ScanLane.HOT,
            last_scanned_at=datetime.now(UTC),
            now=datetime.now(UTC),
            quote_age_ms=radar_age_ms,
            hot_ttl_seconds=DEFAULT_HOT_TTL_SECONDS,
            universe_ttl_seconds=DEFAULT_UNIVERSE_TTL_SECONDS,
        ) == "radar_current"

        stale_left = _matchbook_btts()
        stale_left = stale_left.model_copy(update={"quote_age_ms": radar_age_ms})
        stale_right = _kalshi_btts()
        stale_right = stale_right.model_copy(update={"quote_age_ms": radar_age_ms})
        decision = _scan(scan, left=stale_left, right=stale_right)
        assert decision.eligible_for_paper_simulation is False
        assert "stale_quote" in decision.rejection_reasons
        _observe_persist(scan, watchlist, ops, decision)
        _assert_zero_capture(ops, ledger)
    finally:
        repository.close()
        ledger.close()


def test_wave_e_unverified_mapping_does_not_auto_open(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops(tmp_path)
    try:
        pm_event, pm_market, pm_books = polymarket_payloads()
        unmatched = PolymarketObservationBuilder().build(
            {**pm_event, "title": "Arsenal vs Tottenham Hotspur"},
            pm_market,
            pm_books,
            observed_at=OBSERVED,
            quote_age_ms=180,
        )
        decision = _scan(scan, right=unmatched)
        assert decision.market_match.matched is False
        assert "market_not_equivalent" in decision.rejection_reasons
        assert decision.eligible_for_paper_simulation is False
        _observe_persist(scan, watchlist, ops, decision)
        _assert_zero_capture(ops, ledger)
    finally:
        repository.close()
        ledger.close()


def test_wave_e_missing_fee_does_not_auto_open(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops(tmp_path)
    try:
        decision = _scan(scan, venue_costs=[])
        assert decision.eligible_for_paper_simulation is False
        assert any(reason.startswith("missing_venue_cost:") for reason in decision.rejection_reasons)
        _observe_persist(scan, watchlist, ops, decision)
        _assert_zero_capture(ops, ledger)
    finally:
        repository.close()
        ledger.close()


def test_wave_e_missing_fx_does_not_auto_open(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops(tmp_path)
    try:
        decision = _scan(scan, fx_snapshots=[])
        assert decision.eligible_for_paper_simulation is False
        assert any(reason.startswith("missing_fx_rate:") for reason in decision.rejection_reasons)
        _observe_persist(scan, watchlist, ops, decision)
        _assert_zero_capture(ops, ledger)
    finally:
        repository.close()
        ledger.close()


def test_wave_e_insufficient_depth_does_not_auto_open(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops(tmp_path)
    try:
        pm_event, pm_market, _books = polymarket_payloads()
        thin = PolymarketObservationBuilder().build(
            pm_event,
            pm_market,
            {
                "yes-token": {"asset_id": "yes-token", "bids": [], "asks": []},
                "no-token": {"asset_id": "no-token", "bids": [], "asks": []},
            },
            observed_at=OBSERVED,
            quote_age_ms=180,
        )
        decision = _scan(scan, right=thin)
        assert decision.eligible_for_paper_simulation is False
        assert decision.depth_scan is None or decision.depth_scan.solution.is_arbitrage is False
        _observe_persist(scan, watchlist, ops, decision)
        _assert_zero_capture(ops, ledger)
    finally:
        repository.close()
        ledger.close()


def test_wave_e_insufficient_capital_does_not_auto_open(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops(tmp_path)
    try:
        decision = _scan(scan, liquidity_snapshot=_empty_standing())
        assert decision.allocation is None or not decision.allocation.accepted
        assert decision.eligible_for_paper_simulation is False
        _observe_persist(scan, watchlist, ops, decision)
        _assert_zero_capture(ops, ledger)
    finally:
        repository.close()
        ledger.close()


def test_wave_e_execution_risk_alone_does_not_block_paper(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops(tmp_path)
    try:
        decision = _scan(scan, maximum_execution_risk=0)
        assert decision.eligible_for_paper_simulation is True
        assert "execution_risk_above_threshold" not in decision.rejection_reasons
        assert decision.execution_risk is not None
        _observe_persist(scan, watchlist, ops, decision)
        assert ops.list_active_trades()
    finally:
        repository.close()
        ledger.close()


def test_wave_e_allocator_reject_or_size_zero_does_not_auto_open(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops(tmp_path)
    try:
        decision = scan.scan_pair(
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
            fx_snapshots=FX,
            maximum_execution_risk=100,
        )
        assert decision.eligible_for_paper_simulation is True
        assert decision.allocation is None
        _observe_persist(scan, watchlist, ops, decision)
        _assert_zero_capture(ops, ledger)
        assert any("allocator_size_required" in reason for reason in ops._entry_rejections.values())
    finally:
        repository.close()
        ledger.close()


def test_wave_e_near_only_does_not_auto_open(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops(tmp_path)
    try:
        # Same registered MB↔K BTTS pair as the positive chain, but Kalshi
        # bids are worsened so the implied No ask sits under the 0.50% trigger.
        # Kalshi taker Yes/No asks are 1 - opposite bid.
        near_k = _kalshi_btts(yes_price="0.462", no_price="0.56")
        before = ledger.treasury.snapshot()
        decision = _scan(scan, right=near_k)
        assert decision.market_match.matched is True
        assert decision.depth_scan is not None
        assert decision.depth_scan.solution.is_arbitrage is True
        assert 0 < decision.depth_scan.solution.roi < decision.minimum_net_edge
        assert decision.eligible_for_paper_simulation is False
        assert decision.rejection_reasons == ["net_edge_below_threshold"]

        watched = watchlist.observe_paper_decision(decision, _history(scan, decision))
        assert watched is not None
        assert watched.status is OpportunityStatus.APPROACHING
        assert watched.classification is OpportunityClassification.NEAR_OPPORTUNITY
        assert watched.is_arbitrage is False
        assert watched.status is not OpportunityStatus.TRIGGERED

        ops.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER)
        _assert_zero_capture(ops, ledger)
        after = ledger.treasury.snapshot()
        for venue, currency in (
            (VenueName.MATCHBOOK, "GBP"),
            (VenueName.POLYMARKET, "USD"),
            (VenueName.KALSHI, "USD"),
        ):
            assert after.pool(venue, currency).available_cash == before.pool(
                venue, currency
            ).available_cash
            assert after.pool(venue, currency).locked_capital == before.pool(
                venue, currency
            ).locked_capital
    finally:
        repository.close()
        ledger.close()


def test_wave_e_disabled_venue_leg_does_not_auto_open(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops(tmp_path)
    try:
        decision = _scan(scan)
        assert decision.eligible_for_paper_simulation is True
        watchlist.observe_paper_decision(decision, _history(scan, decision))
        ops.persist_triggered_chain(
            decision,
            provenance=DataProvenance.LIVE_PAPER,
            refreshed_venues=(VenueName.MATCHBOOK,),
        )
        _assert_zero_capture(ops, ledger)
    finally:
        repository.close()
        ledger.close()


def test_wave_e_labelled_demo_replay_does_not_inherit_autofill(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops(tmp_path, autofill=True)
    try:
        decision = _scan(scan)
        assert decision.eligible_for_paper_simulation is True
        _observe_persist(scan, watchlist, ops, decision, provenance=DataProvenance.FIXTURE_DEMO)
        _assert_zero_capture(ops, ledger)
    finally:
        repository.close()
        ledger.close()


def test_wave_e_hard_no_live_execution_boundary() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    assert settings.paper_autofill_enabled is False
    assert settings.paper_auto_unwind_enabled is False
    for client in (MatchbookClient, PolymarketClient, KalshiClient):
        assert client.capabilities.execution_enabled is False
        for token in WRITE_TOKENS:
            assert not hasattr(client, token)
    matchbook = (VENUE_DIR / "matchbook.py").read_text(encoding="utf-8")
    polymarket = (VENUE_DIR / "polymarket.py").read_text(encoding="utf-8")
    kalshi = (VENUE_DIR / "kalshi.py").read_text(encoding="utf-8")
    base = (VENUE_DIR / "base.py").read_text(encoding="utf-8")
    assert "There is intentionally no order placement" in base
    assert matchbook.count("self._client.post(") == 1
    assert "/bpapi/rest/security/session" in matchbook
    assert "Phase 1 deliberately contains no authentication, wallet, signing" in polymarket
    assert "unauthenticated market-data endpoints only" in kalshi
    for source in (matchbook, polymarket, kalshi, base):
        for token in WRITE_TOKENS:
            assert token not in source
        for token in GEO_TOKENS:
            assert token not in source.casefold()

    ops_src = (REPO / "backend/src/sports_hedge/application/paper_operations.py").read_text(
        encoding="utf-8"
    )
    manager = (REPO / "backend/src/sports_hedge/paper/position_management/manager.py").read_text(
        encoding="utf-8"
    )
    for source in (ops_src, manager):
        for token in WRITE_TOKENS:
            assert token not in source

    health = TestClient(app).get("/health").json()
    venues = TestClient(app).get("/venues").json()
    assert health["mode"] == "paper"
    assert health["execution_enabled"] is False
    assert health["paper_auto_unwind_enabled"] is False
    assert all(row["capabilities"]["execution_enabled"] is False for row in venues)


def test_wave_e_opportunity_monitor_and_auto_unwind_stay_paper() -> None:
    monitor = (FRONTEND / "components" / "opportunity-monitor.tsx").read_text(encoding="utf-8")
    display = (FRONTEND / "lib" / "opportunity-monitor-display.ts").read_text(encoding="utf-8")
    page = (FRONTEND / "app" / "page.tsx").read_text(encoding="utf-8")
    for source in (monitor, display):
        for token in (*WRITE_TOKENS, "simulate-fill", "/unwind", "/settle", "geobypass"):
            assert token not in source
    assert "Current radar only" in monitor
    assert "Paper describes execution mode, not this table" in monitor
    assert "<OpportunityMonitor" in page
    assert "Open paper positions" in page

    models = (
        REPO / "backend/src/sports_hedge/paper/position_management/models.py"
    ).read_text(encoding="utf-8")
    assert "after authoritative settlement" in models
    assert "complete_validated_unwind" in (
        REPO / "backend/src/sports_hedge/paper/position_management/manager.py"
    ).read_text(encoding="utf-8")
    launcher = REPO / "scripts" / "windows" / "Start-SportsHedge-Demo.ps1"
    text = launcher.read_text(encoding="utf-8")
    assert '$env:SPORTS_HEDGE_MODE = "paper"' in text or "SPORTS_HEDGE_MODE" in text
    assert '$env:SPORTS_HEDGE_EXECUTION_ENABLED = "false"' in text
    assert "no live execution" in text
    assert "no automatic authoritative settlement" in text
    assert "place_order" not in text
    assert "cancel_order" not in text
