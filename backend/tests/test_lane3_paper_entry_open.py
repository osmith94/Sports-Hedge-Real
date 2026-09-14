"""Lane 3 proof: confirmed fixed-size paper entry → native treasury lock → OPEN.

Audits sections F/G against the #115 paper-entry path. Uses labelled
DEMO / FIXTURE REPLAY observations so live discovery/qualification is not
repaired here. Data class: fixture/demo modelled paper fills, not live venue
orders.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from test_paper_fill_simulator import _leg, _two_level_book
from test_step8f_automatic_paper_entry import (
    OBSERVED,
    _assert_allocator_sized,
    _kalshi_btts,
    _kalshi_costs,
    _matchbook_btts,
    _observe_and_persist,
    _ops_bundle,
    _pm_kalshi_costs,
    _polymarket_btts,
)
from test_step9_operator_demo import _bundle as _demo_bundle
from venue_cost_helpers import matchbook_polymarket_costs

from sports_hedge.accounting.dimensions import EconomicAccount
from sports_hedge.accounting.paper_journal import DataProvenance
from sports_hedge.api.main import app
from sports_hedge.api.paper import get_paper_ledger, get_paper_operations_service
from sports_hedge.application.demo_walkthrough import DemoWalkthroughService, FixtureReplayRequest
from sports_hedge.application.paper_operations import PaperOperationsError, PaperOperationsService
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.fills import FillMode, PaperFillConfig
from sports_hedge.paper.simulator import INSUFFICIENT_DEPTH, STALE_QUOTE, PaperFillSimulator
from sports_hedge.paper.trades import PaperLegFillKind, PaperTradeState
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.treasury.models import PaperTreasuryEventType

POOLS = (
    (VenueName.MATCHBOOK, "GBP"),
    (VenueName.POLYMARKET, "USD"),
    (VenueName.KALSHI, "USD"),
)
# Paper treasury USD seed is quantized to 8 d.p.; lock math may leave a trailing
# 1e-26 residue versus allocator stakes. Conservation (available+locked) is exact.
TREASURY_NATIVE_QUANTUM = Decimal("0.00000001")


def _native(value: Decimal) -> Decimal:
    return Decimal(value).quantize(TREASURY_NATIVE_QUANTUM)


def _deltas(before, after):
    out = {}
    for venue, currency in POOLS:
        b = before.pool(venue, currency)
        a = after.pool(venue, currency)
        out[(venue, currency)] = {
            "available_delta": a.available_cash - b.available_cash,
            "locked_delta": a.locked_capital - b.locked_capital,
            "available_before": b.available_cash,
            "available_after": a.available_cash,
            "locked_before": b.locked_capital,
            "locked_after": a.locked_capital,
        }
    return out


def _lock_rows(ledger, trade_id: str) -> list[dict]:
    rows = ledger._connection.execute(
        "SELECT * FROM paper_treasury_locks WHERE trade_id = ? ORDER BY lock_id",
        (trade_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def _assert_open_surface(trade, *, provenance: DataProvenance) -> None:
    assert trade.state is PaperTradeState.OPEN
    assert trade.paper_only is True
    assert trade.places_orders is False
    assert trade.provenance is provenance
    assert trade.fixture_label or (trade.home_team and trade.away_team)
    assert trade.market_label or trade.market_family
    assert trade.canonical_event_id
    assert trade.canonical_market_id
    assert len(trade.legs) >= 2
    assert all(leg.requested_stake > 0 for leg in trade.legs)
    assert all(leg.filled_stake > 0 for leg in trade.legs)
    assert all(leg.filled_odds is not None or leg.displayed_odds is not None for leg in trade.legs)
    assert all(leg.fill_kind is not PaperLegFillKind.UNFILLED for leg in trade.legs)
    assert all(leg.execution_mode for leg in trade.legs)
    assert all(leg.fill_id for leg in trade.legs)
    assert trade.guaranteed_profit_gbp_at_open is not None
    assert trade.guaranteed_profit_gbp_at_open > 0
    assert trade.entry_risk is not None
    assert trade.entry_risk.kind.value == "entry"
    assert trade.entry_risk.trade_id == trade.trade_id
    assert trade.entry_risk.opportunity_id == trade.opportunity_id
    assert PaperLegFillKind.MANUAL_EXTERNAL not in {leg.fill_kind for leg in trade.legs}


def test_lane3_mb_pm_arithmetic_lock_ids_and_open_fields(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        opening = ledger.treasury.snapshot()
        decision = _observe_and_persist(
            scan,
            watchlist,
            ops,
            _matchbook_btts(),
            _polymarket_btts(),
            venue_costs=matchbook_polymarket_costs(),
        )
        trades = ops.list_active_trades()
        assert len(trades) == 1
        trade = trades[0]
        _assert_open_surface(trade, provenance=DataProvenance.LIVE_PAPER)
        _assert_allocator_sized(ops, trade)

        requested = {(leg.venue, leg.currency): leg.requested_stake for leg in trade.legs}
        filled = {(leg.venue, leg.currency): leg.filled_stake for leg in trade.legs}
        assert requested == filled

        after = ledger.treasury.snapshot()
        deltas = _deltas(opening, after)
        mb_req = requested[(VenueName.MATCHBOOK, "GBP")]
        pm_req = requested[(VenueName.POLYMARKET, "USD")]

        for venue, currency in POOLS:
            assert _native(deltas[(venue, currency)]["available_delta"]) == _native(
                -deltas[(venue, currency)]["locked_delta"]
            )
            native_total_before = opening.pool(venue, currency).available_cash + opening.pool(
                venue, currency
            ).locked_capital
            native_total_after = after.pool(venue, currency).available_cash + after.pool(
                venue, currency
            ).locked_capital
            assert _native(native_total_after) == _native(native_total_before)

        assert _native(deltas[(VenueName.MATCHBOOK, "GBP")]["locked_delta"]) == _native(mb_req)
        assert _native(deltas[(VenueName.POLYMARKET, "USD")]["locked_delta"]) == _native(pm_req)
        assert deltas[(VenueName.KALSHI, "USD")]["available_delta"] == 0
        assert deltas[(VenueName.KALSHI, "USD")]["locked_delta"] == 0

        locks = _lock_rows(ledger, trade.trade_id)
        assert len(locks) == len(trade.legs)
        lock_ids = {row["lock_id"] for row in locks}
        fill_ids = {leg.fill_id for leg in trade.legs}
        assert lock_ids == fill_ids
        for row in locks:
            assert row["opportunity_id"] == trade.opportunity_id
            assert row["trade_id"] == trade.trade_id
            assert row["status"] == "open"
            assert row["source"] in {"paper_fill_simulator", "paper_simulated_external"}
            assert Decimal(row["locked_native"]) == filled[
                (VenueName(row["venue"]), row["native_currency"])
            ]

        lock_events = [
            event
            for event in ledger.treasury.list_events(limit=50)
            if event.trade_id == trade.trade_id
        ]
        assert lock_events
        assert {event.event_type for event in lock_events} == {PaperTreasuryEventType.LOCK}
        assert PaperTreasuryEventType.SEED not in {event.event_type for event in lock_events}
        assert PaperTreasuryEventType.REALISED_PNL not in {event.event_type for event in lock_events}

        journals = ops.journal.list_entries(opportunity_id=trade.opportunity_id)
        accounts = {posting.account_code for entry in journals for posting in entry.postings}
        assert EconomicAccount.EQUITY_PAPER_SEED.value not in accounts
        assert any(code.startswith("ASSET:CASH:LOCKED:") for code in accounts)
        assert any(code.startswith("ASSET:CASH:AVAILABLE:") for code in accounts)
        for entry in journals:
            for posting in entry.postings:
                assert posting.account_code != "EQUITY:OPENING"
                assert "FUNDING" not in posting.account_code
                assert "PNL" not in posting.account_code

        kinds = {leg.venue: leg.fill_kind for leg in trade.legs}
        assert kinds[VenueName.MATCHBOOK] is PaperLegFillKind.INTERNAL_SIMULATED
        assert kinds[VenueName.POLYMARKET] is PaperLegFillKind.PAPER_SIMULATED_EXTERNAL
        assert trade.guaranteed_profit_gbp_at_open == decision.allocation.guaranteed_profit

        print("LANE3_ARITHMETIC_MB_PM")
        print(f"trade_id={trade.trade_id}")
        print(f"opportunity_id={trade.opportunity_id}")
        print(f"lock_ids={sorted(lock_ids)}")
        print(f"opening_MB_GBP available={opening.pool(VenueName.MATCHBOOK, 'GBP').available_cash} locked={opening.pool(VenueName.MATCHBOOK, 'GBP').locked_capital}")
        print(f"opening_PM_USD available={opening.pool(VenueName.POLYMARKET, 'USD').available_cash} locked={opening.pool(VenueName.POLYMARKET, 'USD').locked_capital}")
        print(f"opening_K_USD available={opening.pool(VenueName.KALSHI, 'USD').available_cash} locked={opening.pool(VenueName.KALSHI, 'USD').locked_capital}")
        print(f"request_MB_GBP={mb_req} PM_USD={pm_req}")
        print(f"filled_MB_GBP={filled[(VenueName.MATCHBOOK, 'GBP')]} PM_USD={filled[(VenueName.POLYMARKET, 'USD')]}")
        print(f"post_MB_GBP available={after.pool(VenueName.MATCHBOOK, 'GBP').available_cash} locked={after.pool(VenueName.MATCHBOOK, 'GBP').locked_capital}")
        print(f"post_PM_USD available={after.pool(VenueName.POLYMARKET, 'USD').available_cash} locked={after.pool(VenueName.POLYMARKET, 'USD').locked_capital}")
        print(f"post_K_USD available={after.pool(VenueName.KALSHI, 'USD').available_cash} locked={after.pool(VenueName.KALSHI, 'USD').locked_capital}")
        print(f"guaranteed_gbp_at_open={trade.guaranteed_profit_gbp_at_open}")
        print(f"entry_risk_score={trade.entry_risk.score} band={trade.entry_risk.band}")
    finally:
        repository.close()
        ledger.close()


def test_lane3_mb_k_and_pm_k_lock_correct_pools(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        opening = ledger.treasury.snapshot()
        _observe_and_persist(
            scan,
            watchlist,
            ops,
            _matchbook_btts(),
            _kalshi_btts(),
            venue_costs=_kalshi_costs(),
        )
        trade = ops.list_active_trades()[0]
        _assert_open_surface(trade, provenance=DataProvenance.LIVE_PAPER)
        after = ledger.treasury.snapshot()
        mb = next(leg for leg in trade.legs if leg.venue is VenueName.MATCHBOOK)
        ks = next(leg for leg in trade.legs if leg.venue is VenueName.KALSHI)
        assert after.pool(VenueName.MATCHBOOK, "GBP").locked_capital == mb.filled_stake
        assert after.pool(VenueName.KALSHI, "USD").locked_capital == ks.filled_stake
        assert after.pool(VenueName.POLYMARKET, "USD").locked_capital == opening.pool(
            VenueName.POLYMARKET, "USD"
        ).locked_capital
        assert {leg.fill_kind for leg in trade.legs} == {PaperLegFillKind.INTERNAL_SIMULATED}
    finally:
        repository.close()
        ledger.close()

    pmk = tmp_path / "pmk"
    pmk.mkdir()
    scan, watchlist, ops, repository, ledger = _ops_bundle(pmk, autofill=True)
    try:
        opening = ledger.treasury.snapshot()
        _observe_and_persist(
            scan,
            watchlist,
            ops,
            _polymarket_btts(),
            _kalshi_btts(),
            venue_costs=_pm_kalshi_costs(),
        )
        trade = ops.list_active_trades()[0]
        _assert_open_surface(trade, provenance=DataProvenance.LIVE_PAPER)
        after = ledger.treasury.snapshot()
        pm = next(leg for leg in trade.legs if leg.venue is VenueName.POLYMARKET)
        ks = next(leg for leg in trade.legs if leg.venue is VenueName.KALSHI)
        assert after.pool(VenueName.POLYMARKET, "USD").locked_capital == pm.filled_stake
        assert after.pool(VenueName.KALSHI, "USD").locked_capital == ks.filled_stake
        assert after.pool(VenueName.MATCHBOOK, "GBP").locked_capital == opening.pool(
            VenueName.MATCHBOOK, "GBP"
        ).locked_capital
        kinds = {leg.venue: leg.fill_kind for leg in trade.legs}
        assert kinds[VenueName.KALSHI] is PaperLegFillKind.INTERNAL_SIMULATED
        assert kinds[VenueName.POLYMARKET] is PaperLegFillKind.PAPER_SIMULATED_EXTERNAL
        print("LANE3_ARITHMETIC_PM_K")
        print(f"trade_id={trade.trade_id} lock_ids={[leg.fill_id for leg in trade.legs]}")
        print(f"PM_USD {opening.pool(VenueName.POLYMARKET, 'USD').available_cash}->{after.pool(VenueName.POLYMARKET, 'USD').available_cash} lock+{pm.filled_stake}")
        print(f"K_USD {opening.pool(VenueName.KALSHI, 'USD').available_cash}->{after.pool(VenueName.KALSHI, 'USD').available_cash} lock+{ks.filled_stake}")
    finally:
        repository.close()
        ledger.close()


def test_lane3_realistic_fill_model_depth_slippage_latency_stale_partial() -> None:
    simulator = PaperFillSimulator()
    now = datetime(2026, 9, 11, 12, 0, tzinfo=UTC)
    depth = simulator.simulate_leg(
        _leg(requested="80", displayed="2.20", levels=_two_level_book()),
        PaperFillConfig(mode=FillMode.REALISTIC),
        now=now,
    )
    assert depth.mode is FillMode.REALISTIC
    assert depth.fully_filled is True
    assert depth.levels_consumed == 2
    assert depth.weighted_odds == Decimal("2.15")
    assert depth.slippage_bps > 0

    slipped = simulator.simulate_leg(
        _leg(requested="80", displayed="2.20", levels=_two_level_book()),
        PaperFillConfig(mode=FillMode.REALISTIC, slippage_bps=Decimal(25)),
        now=now,
    )
    assert slipped.weighted_odds < depth.weighted_odds
    assert slipped.filled_stake == Decimal(80)

    delayed = simulator.simulate_leg(
        _leg(requested="80", displayed="2.20", levels=_two_level_book(), quote_age_ms=100),
        PaperFillConfig(
            mode=FillMode.REALISTIC,
            assumed_latency_ms=250,
            ms_per_skipped_level=250,
            max_quote_age_ms=2000,
        ),
        now=now,
    )
    assert delayed.levels_consumed == 1
    assert delayed.weighted_odds == Decimal("2.10")

    stale = simulator.simulate_leg(
        _leg(requested="40", displayed="2.20", levels=_two_level_book(), quote_age_ms=800),
        PaperFillConfig(mode=FillMode.REALISTIC, assumed_latency_ms=500, max_quote_age_ms=1000),
        now=now,
    )
    assert stale.filled_stake == 0
    assert stale.rejection_reason == STALE_QUOTE

    partial = simulator.simulate_leg(
        _leg(requested="150", displayed="2.20", levels=_two_level_book()),
        PaperFillConfig(mode=FillMode.REALISTIC),
        now=now,
    )
    assert partial.fully_filled is False
    assert partial.filled_stake == Decimal(120)
    assert partial.remaining_stake == Decimal(30)
    assert partial.rejection_reason == INSUFFICIENT_DEPTH


def test_lane3_stale_partial_do_not_open_or_leak_locks(tmp_path: Path) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=False)
    try:
        _observe_and_persist(
            scan,
            watchlist,
            ops,
            _matchbook_btts(),
            _polymarket_btts(),
            venue_costs=matchbook_polymarket_costs(),
        )
        opportunity_id = next(iter(ops._plans))
        before = ledger.treasury.snapshot()
        plan = ops._plans[opportunity_id]
        thin = plan.legs[-1]
        plan.legs[-1] = thin.model_copy(
            update={
                "quote_age_ms": 50_000,
                "levels": [BookLevel(decimal_odds=thin.displayed_odds, available_stake=thin.requested_stake / 10)],
            }
        )
        with pytest.raises(PaperOperationsError):
            ops.simulate_fill(
                opportunity_id,
                simulate_external=True,
                provenance=DataProvenance.FIXTURE_DEMO,
                now=OBSERVED,
            )
        assert ops.list_active_trades() == []
        after = ledger.treasury.snapshot()
        for venue, currency in POOLS:
            assert after.pool(venue, currency).available_cash == before.pool(venue, currency).available_cash
            assert after.pool(venue, currency).locked_capital == before.pool(venue, currency).locked_capital
        assert _lock_rows(ledger, f"ptrade-{opportunity_id.replace(':', '-')}") == []
    finally:
        repository.close()
        ledger.close()


def test_lane3_retries_idempotent_and_identity_survives_reload(tmp_path: Path) -> None:
    db_path = tmp_path / "paper.sqlite"
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    try:
        decision = _observe_and_persist(
            scan,
            watchlist,
            ops,
            _matchbook_btts(),
            _polymarket_btts(),
            venue_costs=matchbook_polymarket_costs(),
        )
        trade = ops.list_active_trades()[0]
        first_locks = _lock_rows(ledger, trade.trade_id)
        first_journals = list(ops.journal.list_entries())
        first_snap = ledger.treasury.snapshot()
        original_risk = trade.entry_risk.model_dump(mode="json")
        trade_id = trade.trade_id
        opportunity_id = trade.opportunity_id
        fill_ids = [leg.fill_id for leg in trade.legs]

        ops.persist_triggered_chain(decision, provenance=DataProvenance.FIXTURE_DEMO)
        retry = ops.simulate_fill(
            opportunity_id,
            simulate_external=True,
            provenance=DataProvenance.FIXTURE_DEMO,
        )
        assert retry.trade_id == trade_id
        assert retry.entry_complete is True
        assert len(ops.list_active_trades()) == 1
        assert len(ops.journal.list_entries()) == len(first_journals)
        assert _lock_rows(ledger, trade_id) == first_locks
        after = ledger.treasury.snapshot()
        for venue, currency in POOLS:
            assert after.pool(venue, currency).available_cash == first_snap.pool(venue, currency).available_cash
            assert after.pool(venue, currency).locked_capital == first_snap.pool(venue, currency).locked_capital
    finally:
        repository.close()
        ledger.close()

    reopened = SqlitePaperLedger(db_path, auto_seed=False)
    try:
        loaded = reopened.trades.get(trade_id)
        assert loaded is not None
        assert loaded.state is PaperTradeState.OPEN
        assert loaded.opportunity_id == opportunity_id
        assert [leg.fill_id for leg in loaded.legs] == fill_ids
        assert loaded.entry_risk is not None
        assert loaded.entry_risk.model_dump(mode="json") == original_risk
        reopened.trades.save(loaded.model_copy(update={"entry_risk": loaded.entry_risk.model_copy(update={"score": 1})}))
        again = reopened.trades.get(trade_id)
        assert again is not None
        assert again.entry_risk is not None
        assert again.entry_risk.model_dump(mode="json") == original_risk
        locks = _lock_rows(reopened, trade_id)
        assert {row["lock_id"] for row in locks} == set(fill_ids)
        assert all(row["opportunity_id"] == opportunity_id for row in locks)
    finally:
        reopened.close()


def test_lane3_fixture_replay_with_production_fill_settings(tmp_path: Path) -> None:
    """Operator-relevant settings: realistic 25 bps / 500 ms latency, 1000 ms watchlist cap."""

    ledger = SqlitePaperLedger(
        tmp_path / "paper.sqlite",
        seed_gbp=Decimal(1000),
        usd_gbp_per_unit=Decimal("0.80"),
        fx_source="paper_demo_fx_snapshot",
    )
    settings = Settings()
    assert settings.max_slippage_bps == 25
    assert settings.simulated_latency_ms == 500
    assert settings.sports_hedge_execution_enabled is False
    repository = SqliteMarketIntelligenceRepository()
    scan = PaperScanService(MarketIntelligenceService(repository), settings=settings)
    watchlist = WatchlistService(SqliteWatchlistRepository(), max_quote_age_ms=1000)
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
    try:
        opening = ledger.treasury.snapshot()
        replay = demo.replay(FixtureReplayRequest(venue_pair="matchbook_polymarket", close_via="hold"))
        assert replay.label == "DEMO / FIXTURE REPLAY"
        assert replay.data_kind == "demo_fixture_replay"
        assert replay.execution_enabled is False
        trade = replay.trade
        assert trade is not None
        _assert_open_surface(trade, provenance=DataProvenance.FIXTURE_DEMO)
        after = ledger.treasury.snapshot()
        for leg in trade.legs:
            pool = after.pool(leg.venue, leg.currency)
            before_pool = opening.pool(leg.venue, leg.currency)
            assert pool.available_cash + pool.locked_capital == before_pool.available_cash + before_pool.locked_capital
            assert _native(pool.locked_capital) == _native(leg.filled_stake)
            assert _native(before_pool.available_cash - pool.available_cash) == _native(leg.filled_stake)
        assert after.pool(VenueName.KALSHI, "USD").locked_capital == 0
        fills_cfg = PaperFillConfig(
            assumed_latency_ms=settings.simulated_latency_ms,
            max_quote_age_ms=watchlist.max_quote_age_ms,
            slippage_bps=Decimal(settings.max_slippage_bps),
        )
        assert fills_cfg.mode is FillMode.REALISTIC
        print("LANE3_FIXTURE_REPLAY_PRODUCTION_SETTINGS")
        print(f"trade_id={trade.trade_id}")
        print(f"legs={[f'{leg.venue.value}:{leg.currency}:{leg.requested_stake}->{leg.filled_stake}@{leg.filled_odds} {leg.fill_kind}' for leg in trade.legs]}")
        print(f"locks={_lock_rows(ledger, trade.trade_id)}")
        print(f"guaranteed={trade.guaranteed_profit_gbp_at_open} entry_risk={trade.entry_risk.score}/{trade.entry_risk.band}")
    finally:
        repository.close()
        ledger.close()


def test_lane3_open_trade_api_and_no_execution_paths(tmp_path: Path) -> None:
    demo, ops, _watchlist, ledger, repository = _demo_bundle(tmp_path)
    client = TestClient(app)
    app.dependency_overrides[get_paper_operations_service] = lambda: ops
    app.dependency_overrides[get_paper_ledger] = lambda: ledger
    try:
        replay = demo.replay(FixtureReplayRequest(venue_pair="matchbook_polymarket", close_via="hold"))
        trade = replay.trade
        assert trade is not None
        body = client.get(f"/paper/trades/{trade.trade_id}").json()
        assert body["state"] == "OPEN"
        assert body["places_orders"] is False
        assert body["entry_risk"] is not None
        assert body["guaranteed_profit_gbp_at_open"] is not None
        assert body["fixture_label"] or body["home_team"]
        assert len(body["legs"]) >= 2
        treasury = client.get("/paper/treasury").json()
        assert treasury["execution_enabled"] is False
        assert treasury["mode"] == "paper"
        health = client.get("/health").json()
        assert health["execution_enabled"] is False
    finally:
        app.dependency_overrides.clear()
        repository.close()
        ledger.close()
