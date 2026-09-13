"""Issue #141: Operations Console paper-session reset and fail-closed treasury edits.

Ordinary treasury edits stay fail-closed while locks/open trades exist.
Reset paper session uses the existing demo cleanup seam. Data here is
persisted paper / fixture-replay, never live venue cash.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from sports_hedge.api.main import app
from sports_hedge.api.paper import (
    get_demo_walkthrough_service,
    get_paper_ledger,
    get_paper_liquidity_repository,
)
from sports_hedge.application.demo_walkthrough import (
    DemoResetRequest,
    DemoWalkthroughService,
    FixtureReplayRequest,
)
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.trades import PaperTradeState
from sports_hedge.persistence.liquidity import SqlitePaperLiquidityRepository
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient

SEED = Decimal("1000")
FX = Decimal("0.80")
USD_SEED = (SEED / FX).quantize(Decimal("0.00000001"))
REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "frontend"


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


def _ledger(path: Path) -> SqlitePaperLedger:
    return SqlitePaperLedger(
        path,
        seed_gbp=SEED,
        usd_gbp_per_unit=FX,
        fx_source="paper_demo_fx_snapshot",
        include_kalshi=True,
    )


def _liquidity(path: Path) -> SqlitePaperLiquidityRepository:
    return SqlitePaperLiquidityRepository(
        path,
        matchbook_gbp=SEED,
        polymarket_usd=USD_SEED,
        kalshi_usd=USD_SEED,
    )


def _bundle(tmp_path: Path):
    ledger = _ledger(tmp_path / "paper.sqlite")
    liquidity = _liquidity(tmp_path / "liquidity.sqlite")
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
        liquidity=liquidity,
        settings=settings,
    )
    return demo, ops, ledger, liquidity, repository


def _override(ledger: SqlitePaperLedger, liquidity: SqlitePaperLiquidityRepository, demo=None):
    app.dependency_overrides[get_paper_ledger] = lambda: ledger
    app.dependency_overrides[get_paper_liquidity_repository] = lambda: liquidity
    if demo is not None:
        get_demo_walkthrough_service.cache_clear()
        app.dependency_overrides[get_demo_walkthrough_service] = lambda: demo


def _clear_overrides() -> None:
    app.dependency_overrides.clear()
    get_demo_walkthrough_service.cache_clear()


def _pools(body: dict) -> dict[str, dict]:
    return {pool["venue"]: pool for pool in body["pools"]}


def _client():
    return TestClient(app)


def test_save_without_locks_changes_native_amounts_and_persists_after_reopen(tmp_path: Path) -> None:
    ledger_path = tmp_path / "paper.sqlite"
    liq_path = tmp_path / "liquidity.sqlite"
    ledger = _ledger(ledger_path)
    liquidity = _liquidity(liq_path)
    _override(ledger, liquidity)
    client = _client()
    try:
        edited = client.post(
            "/paper/treasury/pools",
            json={
                "reason": "operator paper treasury edit",
                "pools": [
                    {"venue": "matchbook", "available": "1500"},
                    {"venue": "polymarket", "available": "0"},
                    {"venue": "kalshi", "available": "1250"},
                ],
            },
        )
        assert edited.status_code == 200, edited.text
        body = edited.json()
        assert body["execution_enabled"] is False
        venues = _pools(body)
        assert Decimal(venues["matchbook"]["available_cash"]) == Decimal("1500")
        assert Decimal(venues["polymarket"]["available_cash"]) == Decimal("0")
        assert Decimal(venues["kalshi"]["available_cash"]) == Decimal("1250")
        assert Decimal(venues["matchbook"]["locked_capital"]) == 0
        solver = {pool.venue: pool for pool in liquidity.get().pools}
        assert solver[VenueName.MATCHBOOK].available == Decimal("1500")
        assert solver[VenueName.POLYMARKET].available == Decimal("0")
        assert solver[VenueName.KALSHI].available == Decimal("1250")
        refresh = client.get("/paper/treasury")
        assert Decimal(_pools(refresh.json())["polymarket"]["available_cash"]) == Decimal("0")
        session_id = body["session"]["session_id"]
        journal_ids = {entry.journal_id for entry in ledger.journal.list_entries()}
        assert journal_ids
    finally:
        _clear_overrides()
        ledger.close()

    reopened = _ledger(ledger_path)
    liquidity2 = _liquidity(liq_path)
    _override(reopened, liquidity2)
    client = _client()
    try:
        again = client.get("/paper/treasury")
        assert again.status_code == 200
        venues = _pools(again.json())
        assert again.json()["session"]["session_id"] == session_id
        assert Decimal(venues["matchbook"]["available_cash"]) == Decimal("1500")
        assert Decimal(venues["polymarket"]["available_cash"]) == Decimal("0")
        assert Decimal(venues["kalshi"]["available_cash"]) == Decimal("1250")
        solver = {pool.venue: pool for pool in liquidity2.get().pools}
        assert solver[VenueName.MATCHBOOK].available == Decimal("1500")
        assert solver[VenueName.POLYMARKET].available == Decimal("0")
        persisted_ids = {entry.journal_id for entry in reopened.journal.list_entries()}
        assert journal_ids <= persisted_ids
        report = reopened.reconcile()
        assert report.ok
        health = client.get("/health").json()
        assert health["execution_enabled"] is False
        assert health["mode"] == "paper"
    finally:
        _clear_overrides()
        reopened.close()


def test_save_rejected_while_locks_exist_leaves_balances_and_journal_unchanged(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "paper.sqlite")
    liquidity = _liquidity(tmp_path / "liquidity.sqlite")
    _override(ledger, liquidity)
    client = _client()
    try:
        seed = client.get("/paper/treasury").json()
        before_venues = _pools(seed)
        before_journals = [(entry.source, entry.source_id) for entry in ledger.journal.list_entries()]
        locked = client.post(
            "/paper/treasury/locks",
            json=[
                {
                    "venue": "matchbook",
                    "native_currency": "GBP",
                    "amount_native": "54.43",
                    "lock_id": "stale-mb-lock",
                    "trade_id": "ptrade-stale",
                    "fx_rate_gbp_per_unit": "1",
                },
                {
                    "venue": "polymarket",
                    "native_currency": "USD",
                    "amount_native": "63.27",
                    "lock_id": "stale-pm-lock",
                    "trade_id": "ptrade-stale",
                    "fx_rate_gbp_per_unit": "0.80",
                },
            ],
        )
        assert locked.status_code == 200, locked.text
        after_lock = _pools(locked.json())
        assert Decimal(after_lock["matchbook"]["locked_capital"]) == Decimal("54.43")
        assert Decimal(after_lock["polymarket"]["locked_capital"]) == Decimal("63.27")
        journals_after_lock = [(entry.source, entry.source_id) for entry in ledger.journal.list_entries()]

        blocked = client.post(
            "/paper/treasury/pools",
            json={
                "reason": "operator paper treasury edit",
                "pools": [
                    {"venue": "matchbook", "available": "0"},
                    {"venue": "polymarket", "available": "0"},
                    {"venue": "kalshi", "available": "0"},
                ],
            },
        )
        assert blocked.status_code == 409
        detail = str(blocked.json()["detail"])
        assert "active_treasury_locks" in detail
        assert "lock_id=stale-mb-lock,stale-pm-lock" in detail or (
            "lock_id=stale-mb-lock" in detail and "stale-pm-lock" in detail
        )
        assert "trade_id=ptrade-stale" in detail

        unchanged = _pools(client.get("/paper/treasury").json())
        assert Decimal(unchanged["matchbook"]["available_cash"]) == Decimal(after_lock["matchbook"]["available_cash"])
        assert Decimal(unchanged["matchbook"]["locked_capital"]) == Decimal("54.43")
        assert Decimal(unchanged["polymarket"]["available_cash"]) == Decimal(after_lock["polymarket"]["available_cash"])
        assert Decimal(unchanged["polymarket"]["locked_capital"]) == Decimal("63.27")
        assert Decimal(unchanged["kalshi"]["available_cash"]) == Decimal(before_venues["kalshi"]["available_cash"])
        after_journals = [(entry.source, entry.source_id) for entry in ledger.journal.list_entries()]
        assert after_journals == journals_after_lock
        assert before_journals != after_journals
    finally:
        _clear_overrides()
        ledger.close()


def test_legacy_liquidity_reset_cannot_masquerade_as_treasury_reset(tmp_path: Path) -> None:
    ledger = _ledger(tmp_path / "paper.sqlite")
    liquidity = _liquidity(tmp_path / "liquidity.sqlite")
    _override(ledger, liquidity)
    client = _client()
    try:
        client.post(
            "/paper/treasury/locks",
            json=[
                {
                    "venue": "matchbook",
                    "native_currency": "GBP",
                    "amount_native": "54.43",
                    "lock_id": "legacy-reset-lock",
                    "trade_id": "ptrade-legacy",
                    "fx_rate_gbp_per_unit": "1",
                }
            ],
        )
        before = client.get("/paper/treasury").json()
        before_venues = _pools(before)
        reset = client.post("/paper/liquidity-pools/reset")
        assert reset.status_code == 200
        after = client.get("/paper/treasury").json()
        after_venues = _pools(after)
        assert after["session"]["session_id"] == before["session"]["session_id"]
        assert Decimal(after_venues["matchbook"]["locked_capital"]) == Decimal("54.43")
        assert Decimal(after_venues["matchbook"]["available_cash"]) == Decimal(
            before_venues["matchbook"]["available_cash"]
        )
        solver = {pool["venue"]: pool for pool in reset.json()["pools"]}
        assert Decimal(solver["matchbook"]["available"]) == Decimal("1000")
        assert Decimal(solver["matchbook"]["locked"]) == 0
        assert Decimal(after_venues["matchbook"]["locked_capital"]) != Decimal(solver["matchbook"]["locked"])
    finally:
        _clear_overrides()
        ledger.close()


def test_explicit_paper_session_reset_releases_locks_archives_trade_and_is_idempotent(
    tmp_path: Path,
) -> None:
    demo, ops, ledger, liquidity, repository = _bundle(tmp_path)
    _override(ledger, liquidity, demo)
    client = _client()
    try:
        opened = demo.replay(
            FixtureReplayRequest(venue_pair="matchbook_polymarket", close_via="hold")
        )
        assert opened.trade is not None
        assert opened.trade.state is PaperTradeState.OPEN
        trade_id = opened.trade.trade_id
        mb_locked = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP").locked_capital
        pm_locked = ledger.treasury.snapshot().pool(VenueName.POLYMARKET, "USD").locked_capital
        assert mb_locked > 0
        assert pm_locked > 0
        journals_before = {entry.journal_id for entry in ledger.journal.list_entries()}
        first_session = ledger.treasury.snapshot().session.session_id

        blocked = client.post(
            "/paper/treasury/pools",
            json={"pools": [{"venue": "polymarket", "available": "0"}]},
        )
        assert blocked.status_code == 409
        assert "active_treasury_locks" in str(blocked.json()["detail"]) or "open_paper_positions" in str(
            blocked.json()["detail"]
        )
        assert trade_id in str(blocked.json()["detail"])

        first = client.post(
            "/paper/demo/reset",
            json={
                "reinitialize_store": True,
                "reason": "explicit operator paper session reset",
            },
        )
        assert first.status_code == 200, first.text
        body = first.json()
        assert body["execution_enabled"] is False
        assert body["paper_only"] is True
        venues = {pool["venue"]: pool for pool in body["pools"]}
        assert Decimal(venues["matchbook"]["available_cash"]) == SEED
        assert Decimal(venues["matchbook"]["locked_capital"]) == 0
        assert Decimal(venues["polymarket"]["available_cash"]) == USD_SEED
        assert Decimal(venues["polymarket"]["locked_capital"]) == 0
        assert Decimal(venues["kalshi"]["available_cash"]) == USD_SEED
        assert Decimal(venues["kalshi"]["locked_capital"]) == 0
        assert ops.list_active_trades() == []
        abandoned = [trade for trade in ops.list_closed_trades() if trade.settlement_source == "demo_reset"]
        assert abandoned
        assert all(trade.realised_pnl_gbp is None for trade in abandoned)
        assert all(":archived:" in trade.trade_id for trade in ledger.trades.list_all())
        assert first_session != ledger.treasury.snapshot().session.session_id
        journals_after = {entry.journal_id for entry in ledger.journal.list_entries()}
        assert journals_before <= journals_after
        release_events = [
            event
            for event in ledger.treasury.list_events(limit=10_000)
            if event.event_type.value == "release" and event.trade_id == trade_id
        ]
        assert len(release_events) >= 1
        assert all(event.source == "paper_demo_reset" for event in release_events)
        solver = {pool.venue: pool for pool in liquidity.get().pools}
        assert solver[VenueName.MATCHBOOK].available == SEED
        assert solver[VenueName.MATCHBOOK].locked == 0
        assert solver[VenueName.POLYMARKET].available == USD_SEED
        assert solver[VenueName.POLYMARKET].locked == 0

        second = client.post(
            "/paper/demo/reset",
            json={
                "reinitialize_store": True,
                "reason": "explicit operator paper session reset",
            },
        )
        assert second.status_code == 200, second.text
        again = {pool["venue"]: pool for pool in second.json()["pools"]}
        assert Decimal(again["matchbook"]["locked_capital"]) == 0
        assert Decimal(again["polymarket"]["locked_capital"]) == 0
        assert Decimal(again["matchbook"]["available_cash"]) == SEED
        still_releases = [
            event
            for event in ledger.treasury.list_events(limit=10_000)
            if event.event_type.value == "release" and event.trade_id == trade_id
        ]
        assert still_releases == release_events or len(still_releases) == len(release_events)

        saved = client.post(
            "/paper/treasury/pools",
            json={
                "reason": "operator paper treasury edit after session reset",
                "pools": [
                    {"venue": "matchbook", "available": "1750"},
                    {"venue": "polymarket", "available": "10"},
                    {"venue": "kalshi", "available": "1250"},
                ],
            },
        )
        assert saved.status_code == 200, saved.text
        saved_venues = _pools(saved.json())
        assert Decimal(saved_venues["matchbook"]["available_cash"]) == Decimal("1750")
        assert Decimal(saved_venues["polymarket"]["available_cash"]) == Decimal("10")
        assert Decimal(saved_venues["matchbook"]["locked_capital"]) == 0
        report = ledger.reconcile()
        assert report.ok
        health = client.get("/health").json()
        assert health["execution_enabled"] is False
        assert health["mode"] == "paper"
        for venue_cls in (MatchbookClient, PolymarketClient, KalshiClient):
            assert not hasattr(venue_cls, "place_order")
            assert not hasattr(venue_cls, "cancel_order")
    finally:
        _clear_overrides()
        repository.close()
        ledger.close()


def test_paper_session_reset_with_stale_locks_then_save_works(tmp_path: Path) -> None:
    demo, _, ledger, liquidity, repository = _bundle(tmp_path)
    _override(ledger, liquidity, demo)
    client = _client()
    try:
        demo.reset(DemoResetRequest(reason="fresh start"))
        client.post(
            "/paper/treasury/locks",
            json=[
                {
                    "venue": "matchbook",
                    "native_currency": "GBP",
                    "amount_native": "54.43",
                    "lock_id": "ops-mb-lock",
                    "trade_id": "ptrade-ops",
                    "fx_rate_gbp_per_unit": "1",
                },
                {
                    "venue": "polymarket",
                    "native_currency": "USD",
                    "amount_native": "63.27",
                    "lock_id": "ops-pm-lock",
                    "trade_id": "ptrade-ops",
                    "fx_rate_gbp_per_unit": "0.80",
                },
            ],
        )
        reset = client.post(
            "/paper/demo/reset",
            json={"reinitialize_store": True, "reason": "explicit operator paper session reset"},
        )
        assert reset.status_code == 200, reset.text
        venues = {pool["venue"]: pool for pool in reset.json()["pools"]}
        assert Decimal(venues["matchbook"]["locked_capital"]) == 0
        assert Decimal(venues["polymarket"]["locked_capital"]) == 0
        saved = client.post(
            "/paper/treasury/pools",
            json={
                "pools": [
                    {"venue": "matchbook", "available": "900"},
                    {"venue": "polymarket", "available": "0"},
                    {"venue": "kalshi", "available": "1250"},
                ]
            },
        )
        assert saved.status_code == 200, saved.text
        venues = _pools(saved.json())
        assert Decimal(venues["matchbook"]["available_cash"]) == Decimal("900")
        assert Decimal(venues["polymarket"]["available_cash"]) == Decimal("0")
        solver = {pool.venue: pool for pool in liquidity.get().pools}
        assert solver[VenueName.MATCHBOOK].available == Decimal("900")
        assert solver[VenueName.POLYMARKET].available == Decimal("0")
        assert solver[VenueName.MATCHBOOK].locked == 0
        assert ledger.reconcile().ok
    finally:
        _clear_overrides()
        repository.close()
        ledger.close()


def test_operations_console_reset_wiring_does_not_call_legacy_liquidity_reset() -> None:
    pools = (FRONTEND / "components" / "liquidity-pools.tsx").read_text(encoding="utf-8")
    api = (FRONTEND / "lib/api.ts").read_text(encoding="utf-8")
    assert "resetPaperLiquidityPools" not in pools
    assert "Reset defaults" not in pools
    assert "Reset paper session" in pools
    assert "Confirm reset paper session" in pools
    assert "resetPaperSession" in pools
    assert "active_treasury_locks" in pools
    assert "open_paper_positions" in pools
    assert "/paper/" in pools
    assert "resetPaperSession" in api
    assert "reinitialize_store: true" in api
    assert "savePaperTreasuryPools" in pools
    health = TestClient(app).get("/health").json()
    assert health["execution_enabled"] is False
    assert health["mode"] == "paper"
