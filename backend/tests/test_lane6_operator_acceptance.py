"""Lane 6 operator-UI coherence and end-to-end acceptance harness.

Audits the Operations Console hierarchy, Live Scan Pulse states, and a labelled
DEMO / FIXTURE REPLAY lifecycle without substituting fixture rows for live
discovery. Live collect is attempted only when SPORTS_HEDGE_LIVE_LOGICAL=1.

Does not invent live opportunities or paper over an empty discovery set.
"""

from __future__ import annotations

import os
import re
import subprocess
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from sports_hedge.accounting.paper_journal import gbp_is_balanced
from sports_hedge.api.main import app
from sports_hedge.application.demo_fixtures import DEMO_DATA_KIND, DEMO_FIXTURE_LABEL, DEMO_FX, tighten_reverse_quotes
from sports_hedge.application.demo_walkthrough import (
    DemoCloseRequest,
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
from sports_hedge.paper.trades import PaperLegFillKind, PaperTradeState
from sports_hedge.paper.unwind.models import UnwindPolicy
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient

SEED = Decimal("1000")
FX = Decimal("0.80")
REPO_ROOT = Path(__file__).resolve().parents[2]
FRONTEND = REPO_ROOT / "frontend"
AUDITED_PR115_HEAD = "3d43b0a4e3c9d5584d918cec016b5fabe6a0057d"

OPERATOR_SURFACES = [
    FRONTEND / "app" / "page.tsx",
    FRONTEND / "app" / "paper" / "page.tsx",
    FRONTEND / "app" / "paper" / "[tradeId]" / "page.tsx",
    FRONTEND / "components" / "run-paper-scan.tsx",
    FRONTEND / "components" / "live-scan-pulse.tsx",
    FRONTEND / "components" / "liquidity-pools.tsx",
    FRONTEND / "components" / "capital-summary.tsx",
    FRONTEND / "components" / "paper-trade-book.tsx",
    FRONTEND / "components" / "activity-feed.tsx",
    FRONTEND / "components" / "opportunity-card.tsx",
    FRONTEND / "components" / "hold-vs-unwind.tsx",
    FRONTEND / "components" / "discovered-fixtures.tsx",
]

FORBIDDEN_OPERATOR_COPY = [
    "places_orders=",
    "POST /paper/",
    "8D ",
    "8E ",
    "8F ",
    "settles_or_releases_capital=",
    "backend_resolved",
]


def git_sha() -> str:
    return subprocess.check_output(
        ["git", "rev-parse", "HEAD"],
        cwd=REPO_ROOT,
        text=True,
    ).strip()


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


def test_lane6_records_exact_git_sha() -> None:
    sha = git_sha()
    assert re.fullmatch(r"[0-9a-f]{40}", sha), sha
    probe = subprocess.run(
        ["git", "cat-file", "-t", AUDITED_PR115_HEAD],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
    )
    if probe.returncode != 0:
        return
    merge_base = subprocess.check_output(
        ["git", "merge-base", "HEAD", AUDITED_PR115_HEAD],
        cwd=REPO_ROOT,
        text=True,
    ).strip()
    assert merge_base == AUDITED_PR115_HEAD, (
        f"harness is not based on audited PR #115 head {AUDITED_PR115_HEAD}; got {merge_base}"
    )


def test_operator_console_hierarchy_is_treasury_then_scan_then_discovery() -> None:
    page = (FRONTEND / "app" / "page.tsx").read_text(encoding="utf-8")
    order = [
        "<LiquidityPools",
        "<RunPaperScan",
        "<FixtureDiscoverySection",
        "<span>Tracked</span>",
        "Open paper positions",
        "<ActivityFeed",
        "<CapitalSummary",
        "Demo walkthrough · not live operations",
    ]
    positions = [page.index(marker) for marker in order]
    assert positions == sorted(positions)
    discovery = (FRONTEND / "components" / "fixture-discovery-section.tsx").read_text(encoding="utf-8")
    assert '<details className="discovery-disclosure">' in discovery
    assert "open=" not in discovery
    assert "Show discovery" in discovery
    assert "Hide discovery" in discovery
    assert "<DiscoveredFixturesPanel" in discovery
    assert "DEMO_NEAR_ARB" not in page
    assert "DEMO_EXECUTABLE" not in page
    assert "DEMO_ACTIVITY" not in page
    assert "DEMO_CAPITAL" not in page
    assert "usedFixture: false" in page
    sidebar = (FRONTEND / "components" / "sidebar.tsx").read_text(encoding="utf-8")
    assert 'href: "/"' in sidebar or 'href: "/",' in sidebar
    assert "PAPER MODE" in sidebar
    layout = (FRONTEND / "app" / "layout.tsx").read_text(encoding="utf-8")
    assert "PAPER MODE · NO EXECUTION" in layout


def test_live_scan_pulse_states_are_real_and_last_scan_is_not_invented() -> None:
    pulse = (FRONTEND / "components" / "live-scan-pulse.tsx").read_text(encoding="utf-8")
    scan = (FRONTEND / "components" / "run-paper-scan.tsx").read_text(encoding="utf-8")
    for phase in ('"idle"', '"scanning"', '"complete"', '"error"', '"degraded"', '"paused"'):
        assert phase in pulse
    assert "Scanning live venues" in pulse
    assert "Scan failed" in pulse
    assert "Partial venue failure" in pulse
    assert "Auto refresh off" in pulse
    assert "Waiting for first scan" in pulse
    assert "Last scan" in scan
    assert '"never"' in scan
    assert "Scanning…" in scan
    assert "setLastCompletedAt(new Date().toISOString())" not in scan
    assert "Keep prior last-scan facts" in scan
    assert 'phase === "scanning"' in pulse
    assert "Request in flight" in pulse


def test_normal_operator_ui_has_no_raw_debug_copy() -> None:
    combined = "\n".join(path.read_text(encoding="utf-8") for path in OPERATOR_SURFACES)
    for needle in FORBIDDEN_OPERATOR_COPY:
        assert needle not in combined, needle
    demo = (FRONTEND / "app" / "demo" / "page.tsx").read_text(encoding="utf-8")
    walkthrough = (FRONTEND / "components" / "demo-walkthrough.tsx").read_text(encoding="utf-8")
    assert "DEMO" in demo or "demo" in demo.lower()
    assert "FIXTURE" in walkthrough or "fixture" in walkthrough.lower()
    page = (FRONTEND / "app" / "page.tsx").read_text(encoding="utf-8")
    assert "No fabricated" in page or "No fixture balances are substituted" in (
        FRONTEND / "components" / "liquidity-pools.tsx"
    ).read_text(encoding="utf-8")
    discovered = (FRONTEND / "components" / "discovered-fixtures.tsx").read_text(encoding="utf-8")
    assert "No fabricated fixtures" in discovered
    capital = (FRONTEND / "components" / "capital-summary.tsx").read_text(encoding="utf-8")
    assert "live.lockedCapitalGbp" not in capital
    assert "tradeSummary?.capital_locked_gbp" in capital


def test_phase1_paper_boundary_and_health() -> None:
    for venue_cls in (MatchbookClient, PolymarketClient, KalshiClient):
        assert not hasattr(venue_cls, "place_order")
        assert not hasattr(venue_cls, "cancel_order")
        assert not hasattr(venue_cls, "sign")
    health = TestClient(app).get("/health").json()
    assert health["execution_enabled"] is False
    assert health["mode"] == "paper"


def test_uninterrupted_fixture_replay_walkthrough_records_locks_fills_and_persistence(
    tmp_path: Path,
) -> None:
    sha = git_sha()
    demo, ops, watchlist, ledger, repository = _bundle(tmp_path)
    sqlite_path = tmp_path / "paper.sqlite"
    try:
        opening = demo.reset(DemoResetRequest(reason="lane6 opening treasury"))
        assert opening.paper_only is True
        assert opening.execution_enabled is False
        assert opening.data_kind == "live_paper"
        before = {pool.venue: pool for pool in opening.treasury.pools}
        assert set(before) >= {VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI}
        assert before[VenueName.MATCHBOOK].available_cash == SEED
        assert before[VenueName.MATCHBOOK].locked_capital == 0
        assert before[VenueName.POLYMARKET].native_currency == "USD"
        assert before[VenueName.KALSHI].native_currency == "USD"

        opened = demo.replay(
            FixtureReplayRequest(venue_pair="matchbook_polymarket", solver="simple", close_via="hold")
        )
        assert opened.label == DEMO_FIXTURE_LABEL
        assert opened.data_kind == DEMO_DATA_KIND
        trade = opened.trade
        assert trade is not None
        assert trade.state is PaperTradeState.OPEN
        assert trade.places_orders is False
        assert trade.guaranteed_profit_gbp_at_open is not None
        assert trade.entry_risk is not None
        assert trade.legs
        kinds = {leg.fill_kind for leg in trade.legs}
        assert PaperLegFillKind.INTERNAL_SIMULATED in kinds
        assert PaperLegFillKind.PAPER_SIMULATED_EXTERNAL in kinds
        assert PaperLegFillKind.MANUAL_EXTERNAL not in kinds
        assert all(leg.filled_stake > 0 for leg in trade.legs)
        after = {pool.venue: pool for pool in opened.treasury.pools}
        assert after[VenueName.MATCHBOOK].locked_capital > 0
        assert after[VenueName.POLYMARKET].locked_capital > 0
        assert after[VenueName.MATCHBOOK].available_cash < before[VenueName.MATCHBOOK].available_cash
        assert after[VenueName.POLYMARKET].available_cash < before[VenueName.POLYMARKET].available_cash
        assert after[VenueName.KALSHI].locked_capital == 0
        stored = watchlist.repository.get(trade.opportunity_id)
        assert stored is not None
        assert stored.data_kind == DEMO_DATA_KIND
        assert watchlist.triggered() == []
        snapshot = demo.snapshot()
        assert snapshot.live_triggered == []
        assert snapshot.live_near == []
        detail = ops.trade_detail(trade.trade_id)
        assert detail.audit
        assert any(event.event_type.value == "trade_opened" for event in detail.audit)
        opportunity_id = trade.opportunity_id
        postings = ops.journal.postings(opportunity_id=opportunity_id)
        assert postings
        assert gbp_is_balanced(postings)
        trade_id = trade.trade_id
        mb_locked = after[VenueName.MATCHBOOK].locked_capital
        pm_locked = after[VenueName.POLYMARKET].locked_capital
    finally:
        repository.close()
        ledger.close()

    reopened = SqlitePaperLedger(
        sqlite_path,
        seed_gbp=SEED,
        usd_gbp_per_unit=FX,
        auto_seed=True,
    )
    try:
        persisted = reopened.trades.get(trade_id)
        assert persisted is not None
        assert persisted.state is PaperTradeState.OPEN
        treasury = reopened.treasury.snapshot()
        assert treasury.pool(VenueName.MATCHBOOK, "GBP").locked_capital == mb_locked
        assert treasury.pool(VenueName.POLYMARKET, "USD").locked_capital == pm_locked
        assert treasury.execution_enabled is False
    finally:
        reopened.close()
    assert re.fullmatch(r"[0-9a-f]{40}", sha)


def test_early_unwind_path_is_separate_from_settlement(tmp_path: Path) -> None:
    demo, ops, watchlist, ledger, repository = _bundle(tmp_path)
    try:
        opened = demo.replay(
            FixtureReplayRequest(venue_pair="matchbook_polymarket", close_via="hold")
        )
        assert opened.trade is not None
        assert opened.unwind is not None
        assert opened.unwind.spendable is False
        before = ledger.treasury.snapshot()
        assert before.pool(VenueName.MATCHBOOK, "GBP").locked_capital > 0
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
        released = ledger.treasury.snapshot()
        assert released.pool(VenueName.MATCHBOOK, "GBP").locked_capital == 0
        assert released.pool(VenueName.POLYMARKET, "USD").locked_capital == 0
        postings = ops.journal.postings(opportunity_id=closed.opportunity_id)
        signed = sum((item.signed_gbp for item in postings), Decimal("0"))
        # Unwind FX presentation can leave a sub-tick residue (observed ~1e-27).
        # That is not an invented posting; a missing leg would be pounds, not dust.
        assert abs(signed) < Decimal("0.00000001")
        assert closed.settlement_source != "demo_fixture_replay"
    finally:
        repository.close()
        ledger.close()


def test_settlement_path_releases_and_balances_without_unwind(tmp_path: Path) -> None:
    demo, ops, watchlist, ledger, repository = _bundle(tmp_path)
    try:
        opened = demo.replay(
            FixtureReplayRequest(venue_pair="matchbook_polymarket", solver="simple", close_via="hold")
        )
        assert opened.trade is not None
        assert opened.trade.state is PaperTradeState.OPEN
        closed = demo.close_open_trade(
            opened.trade.trade_id, DemoCloseRequest(close_via="settlement")
        )
        assert closed.trade is not None
        assert closed.trade.state is PaperTradeState.CLOSED
        assert closed.trade.settlement_source == "demo_fixture_replay"
        assert closed.trade.realised_pnl_gbp is not None
        assert closed.journal_balanced is True
        released = ledger.treasury.snapshot()
        assert released.pool(VenueName.MATCHBOOK, "GBP").locked_capital == 0
        assert released.pool(VenueName.POLYMARKET, "USD").locked_capital == 0
        postings = ops.journal.postings(opportunity_id=closed.trade.opportunity_id)
        assert gbp_is_balanced(postings)
        currencies = {posting.dimensions.currency for posting in postings}
        assert "GBP" in currencies
        assert "USD" in currencies
        native_usd_venues = {
            pool.venue: pool.available_cash
            for pool in released.pools
            if pool.native_currency == "USD"
        }
        assert VenueName.POLYMARKET in native_usd_venues
        assert VenueName.KALSHI in native_usd_venues
    finally:
        repository.close()
        ledger.close()


@pytest.mark.live_logical
def test_live_collect_is_bounded_and_does_not_fabricate_fixtures() -> None:
    if os.environ.get("SPORTS_HEDGE_LIVE_LOGICAL") != "1":
        pytest.skip("live logical collect is opt-in")
    client = TestClient(app)
    health = client.get("/health").json()
    assert health["execution_enabled"] is False
    treasury = client.get("/paper/treasury")
    assert treasury.status_code == 200
    response = client.post("/paper/collect", json={"maximum_execution_risk": 60})
    assert response.status_code in {200, 502, 503, 504}
    if response.status_code != 200:
        return
    body = response.json()
    assert "discovered_fixtures" in body
    status = client.get("/paper/live-refresh").json()
    assert status["last_completed_at"]
    live_ids = {row["canonical_event_id"] for row in status["discovered_fixtures"]}
    replay_ids = {
        row["canonical_event_id"]
        for row in status["discovered_fixtures"]
        if "replay" in (row.get("canonical_event_id") or "")
    }
    assert replay_ids == set()
    triggered = client.get("/paper/watchlist/triggered").json()
    for row in triggered:
        assert row.get("data_kind") != DEMO_DATA_KIND
    _ = live_ids
