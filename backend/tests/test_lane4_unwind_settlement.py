"""Lane 4: OPEN paper position → validated unwind and separate settlement.

Fixture-replay OPEN trades. PAPER-ONLY; no venue writes.
Data class: DEMO / FIXTURE REPLAY plus modelled reverse-side close quotes.
"""

from __future__ import annotations

from collections import defaultdict
from decimal import Decimal
from pathlib import Path

import pytest

def _gbp_book_is_balanced(postings) -> bool:
    """Journal entries balance at append time; combined Decimal(28) dust can remain."""

    signed = sum((item.signed_gbp for item in postings), Decimal("0"))
    return abs(signed) < Decimal("1e-18")
from sports_hedge.application.demo_fixtures import DEMO_FX, tighten_reverse_quotes
from sports_hedge.application.demo_walkthrough import (
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
from sports_hedge.paper.settlement import compute_paper_settlement
from sports_hedge.paper.trades import (
    PAPER_UNWIND_SOURCE,
    PaperSettlementRequest,
    PaperTradeAuditEventType,
    PaperTradeState,
    paper_unwind_source_id,
)
from sports_hedge.paper.unwind.models import UnwindPolicy, UnwindRecommendation
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger


SEED = Decimal("1000")
FX = Decimal("0.80")
HOLD_POLICY = UnwindPolicy(
    min_retained_exit_pnl_gbp=Decimal("100000"),
    max_profit_give_up_gbp=Decimal("0"),
    max_execution_risk=100,
)
UNWIND_POLICY = UnwindPolicy(max_profit_give_up_gbp=Decimal("1000"), max_execution_risk=100)


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
        tmp_path / "lane4.sqlite",
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
    return demo, ops, ledger, repository


def _pool_tuple(ledger: SqlitePaperLedger, venue: VenueName, currency: str) -> tuple[Decimal, Decimal, Decimal, Decimal]:
    pool = ledger.treasury.snapshot().pool(venue, currency)
    return (
        pool.available_cash,
        pool.locked_capital,
        pool.realised_pnl_native,
        pool.cumulative_fees_native,
    )


def _treasury_fingerprint(ledger: SqlitePaperLedger) -> dict[tuple[VenueName, str], tuple[Decimal, Decimal, Decimal, Decimal]]:
    snap = ledger.treasury.snapshot()
    return {
        (pool.venue, pool.native_currency): (
            pool.available_cash,
            pool.locked_capital,
            pool.realised_pnl_native,
            pool.cumulative_fees_native,
        )
        for pool in snap.pools
    }


def _open_mb_pm(demo: DemoWalkthroughService):
    opened = demo.replay(FixtureReplayRequest(venue_pair="matchbook_kalshi", close_via="hold"))
    assert opened.trade is not None
    assert opened.trade.state is PaperTradeState.OPEN
    assert opened.trade.close_fills == []
    assert opened.quotes
    return opened


def test_hold_and_inferior_unwind_release_nothing(tmp_path: Path) -> None:
    demo, ops, ledger, repository = _bundle(tmp_path)
    try:
        opened = _open_mb_pm(demo)
        trade = opened.trade
        assert trade is not None
        quotes = tighten_reverse_quotes(opened.quotes)
        before = _treasury_fingerprint(ledger)
        locked_mb, locked_k = (
            before[(VenueName.MATCHBOOK, "GBP")][1],
            before[(VenueName.KALSHI, "USD")][1],
        )
        assert locked_mb > 0
        assert locked_k > 0
        journals_before = [
            entry.source_id
            for entry in ops.journal.list_entries(opportunity_id=trade.opportunity_id)
            if entry.source == PAPER_UNWIND_SOURCE
        ]
        decision = ops.evaluate_unwind(
            trade.trade_id,
            quotes=quotes,
            fx=list(DEMO_FX),
            policy=HOLD_POLICY,
        )
        assert decision.close_plan.fully_executable is True
        assert decision.recommendation is UnwindRecommendation.HOLD
        assert decision.spendable is False
        assert decision.estimated_time_to_release.settles_or_releases_capital is False
        with pytest.raises(PaperOperationsError, match="unwind_not_eligible"):
            ops.complete_validated_unwind(
                trade.trade_id,
                quotes=quotes,
                fx=list(DEMO_FX),
                policy=HOLD_POLICY,
            )
        persisted = ops.trades.get(trade.trade_id)
        assert persisted is not None
        assert persisted.state is PaperTradeState.OPEN
        assert persisted.close_fills == []
        assert persisted.capital_locked_native
        assert _treasury_fingerprint(ledger) == before
        journals_after = [
            entry.source_id
            for entry in ops.journal.list_entries(opportunity_id=trade.opportunity_id)
            if entry.source == PAPER_UNWIND_SOURCE
        ]
        assert journals_after == journals_before
    finally:
        repository.close()
        ledger.close()


def test_validated_unwind_and_settlement_capital_arithmetic(tmp_path: Path) -> None:
    demo, ops, ledger, repository = _bundle(tmp_path)
    try:
        opened = _open_mb_pm(demo)
        trade = opened.trade
        assert trade is not None
        quotes = tighten_reverse_quotes(opened.quotes)
        opening_legs = {leg.fill_id: (leg.filled_stake, leg.venue, leg.currency) for leg in trade.legs}
        assert all(fill_id for fill_id in opening_legs)
        pre_mb = _pool_tuple(ledger, VenueName.MATCHBOOK, "GBP")
        pre_k = _pool_tuple(ledger, VenueName.KALSHI, "USD")
        assert pre_mb[1] > 0
        assert pre_k[1] > 0

        closed = ops.complete_validated_unwind(
            trade.trade_id,
            quotes=quotes,
            fx=list(DEMO_FX),
            policy=UNWIND_POLICY,
        )
        assert closed.state is PaperTradeState.CLOSED
        assert closed.settlement_source == PAPER_UNWIND_SOURCE
        assert closed.settlement_source_id == paper_unwind_source_id(trade.trade_id)
        assert closed.settlement_outcome is None
        assert closed.close_fills
        assert len(closed.close_fills) == len([leg for leg in closed.legs if leg.filled_stake > 0])
        for leg in closed.legs:
            original = opening_legs[leg.fill_id]
            assert (leg.filled_stake, leg.venue, leg.currency) == original
        close_ids = {item.fill_id for item in closed.close_fills}
        assert close_ids.isdisjoint(set(opening_legs))
        assert all(item.fill_id == f"close:{item.opening_fill_id}" for item in closed.close_fills)
        assert all(item.opening_fill_id in opening_legs for item in closed.close_fills)
        assert any(
            event.event_type is PaperTradeAuditEventType.CLOSE_FILLS_RECORDED for event in closed.audit
        )

        by_pool: dict[tuple[VenueName, str], dict[str, Decimal]] = defaultdict(
            lambda: {"pnl": Decimal("0"), "fee": Decimal("0"), "gbp": Decimal("0")}
        )
        for fill in closed.close_fills:
            key = (fill.venue, fill.native_currency)
            by_pool[key]["pnl"] += fill.native_close_pnl
            by_pool[key]["fee"] += fill.closing_fee_native
            by_pool[key]["gbp"] += fill.gbp_close_pnl
            assert fill.gbp_close_pnl == fill.native_close_pnl * fill.fx_rate_gbp_per_unit
            assert fill.paper_only is True

        assert closed.realised_pnl_gbp == sum((item.gbp_close_pnl for item in closed.close_fills), Decimal("0"))
        post_mb = _pool_tuple(ledger, VenueName.MATCHBOOK, "GBP")
        post_k = _pool_tuple(ledger, VenueName.KALSHI, "USD")
        mb_pnl = by_pool[(VenueName.MATCHBOOK, "GBP")]["pnl"]
        mb_fee = by_pool[(VenueName.MATCHBOOK, "GBP")]["fee"]
        k_pnl = by_pool[(VenueName.KALSHI, "USD")]["pnl"]
        k_fee = by_pool[(VenueName.KALSHI, "USD")]["fee"]
        assert post_mb[1] == Decimal("0")
        assert post_k[1] == Decimal("0")
        assert post_mb[0] == pre_mb[0] + pre_mb[1] + mb_pnl
        assert post_k[0] == pre_k[0] + pre_k[1] + k_pnl
        assert post_mb[2] == pre_mb[2] + mb_pnl
        assert post_k[2] == pre_k[2] + k_pnl
        assert post_mb[3] == pre_mb[3] + mb_fee
        assert post_k[3] == pre_k[3] + k_fee
        assert closed.capital_locked_native == {}
        assert _gbp_book_is_balanced(ops.journal.postings(opportunity_id=trade.opportunity_id))
        unwind_journals = [
            entry
            for entry in ops.journal.list_entries(opportunity_id=trade.opportunity_id)
            if entry.source == PAPER_UNWIND_SOURCE
        ]
        assert unwind_journals
        reloaded = ops.trades.get(trade.trade_id)
        assert reloaded is not None
        assert len(reloaded.close_fills) == len(closed.close_fills)

        again = ops.complete_validated_unwind(
            trade.trade_id,
            quotes=quotes,
            fx=list(DEMO_FX),
            policy=UNWIND_POLICY,
        )
        assert again.state is PaperTradeState.CLOSED
        assert len(again.close_fills) == len(closed.close_fills)
        assert _pool_tuple(ledger, VenueName.MATCHBOOK, "GBP") == post_mb
        assert _pool_tuple(ledger, VenueName.KALSHI, "USD") == post_k
        assert [
            entry.source_id
            for entry in ops.journal.list_entries(opportunity_id=trade.opportunity_id)
            if entry.source == PAPER_UNWIND_SOURCE
        ] == [entry.source_id for entry in unwind_journals]
        assert any(
            event.event_type is PaperTradeAuditEventType.UNWIND_IDEMPOTENT for event in again.audit
        )
        with pytest.raises(PaperOperationsError, match="already_unwound"):
            ops.settle(
                trade.trade_id,
                PaperSettlementRequest(
                    winning_outcome=trade.legs[0].outcome,
                    source="fixture_test",
                    source_id="must-not-settle-unwound",
                ),
            )
        assert ops.trades.get(trade.trade_id).state is PaperTradeState.CLOSED
        assert _pool_tuple(ledger, VenueName.MATCHBOOK, "GBP") == post_mb

        second = demo.replay(FixtureReplayRequest(venue_pair="matchbook_kalshi", close_via="hold"))
        settle_trade = second.trade
        assert settle_trade is not None
        assert settle_trade.trade_id != trade.trade_id
        assert settle_trade.state is PaperTradeState.OPEN
        settle_pre_mb = _pool_tuple(ledger, VenueName.MATCHBOOK, "GBP")
        settle_pre_k = _pool_tuple(ledger, VenueName.KALSHI, "USD")
        winning = next(leg.outcome for leg in settle_trade.legs if leg.filled_stake > 0)
        computation = compute_paper_settlement(settle_trade, winning_outcome=winning)
        expected_gbp = computation.realised_pnl_gbp
        settled = ops.settle(
            settle_trade.trade_id,
            PaperSettlementRequest(
                winning_outcome=winning,
                source="lane4_fixture",
                source_id=f"settle:{settle_trade.trade_id}",
            ),
        )
        assert settled.state is PaperTradeState.CLOSED
        assert settled.settlement_outcome == winning
        assert settled.settlement_source == "lane4_fixture"
        assert settled.settlement_source_id == f"settle:{settle_trade.trade_id}"
        assert settled.realised_pnl_gbp == expected_gbp
        assert settled.close_fills == []
        won = [leg for leg in computation.legs if leg.won]
        lost = [leg for leg in computation.legs if not leg.won]
        assert won and lost
        for leg in won:
            assert leg.net_payoff > 0
            assert leg.native_pnl == leg.net_payoff - leg.filled_stake
        for leg in lost:
            assert leg.net_payoff == Decimal("0")
            assert leg.native_pnl == -leg.filled_stake
        settle_post_mb = _pool_tuple(ledger, VenueName.MATCHBOOK, "GBP")
        settle_post_k = _pool_tuple(ledger, VenueName.KALSHI, "USD")
        assert settle_post_mb[1] == Decimal("0")
        assert settle_post_k[1] == Decimal("0")
        native_by_pool: dict[tuple[str, str], dict[str, Decimal]] = defaultdict(
            lambda: {"payoff": Decimal("0"), "pnl": Decimal("0"), "fee": Decimal("0"), "stake": Decimal("0")}
        )
        for leg in computation.legs:
            key = (leg.venue, leg.currency)
            native_by_pool[key]["payoff"] += leg.net_payoff
            native_by_pool[key]["pnl"] += leg.native_pnl
            native_by_pool[key]["fee"] += leg.venue_fee
            native_by_pool[key]["stake"] += leg.filled_stake
        mb_set = native_by_pool[("matchbook", "GBP")]
        k_set = native_by_pool[("kalshi", "USD")]
        assert settle_post_mb[0] == settle_pre_mb[0] + mb_set["payoff"]
        assert settle_post_k[0] == settle_pre_k[0] + k_set["payoff"]
        assert settle_post_mb[2] == settle_pre_mb[2] + mb_set["pnl"]
        assert settle_post_k[2] == settle_pre_k[2] + k_set["pnl"]
        assert settle_post_mb[3] == settle_pre_mb[3] + mb_set["fee"]
        assert settle_post_k[3] == settle_pre_k[3] + k_set["fee"]
        assert _gbp_book_is_balanced(ops.journal.postings(opportunity_id=settle_trade.opportunity_id))
        settle_journals = [
            entry
            for entry in ops.journal.list_entries(opportunity_id=settle_trade.opportunity_id)
            if entry.source == "paper_settlement"
        ]
        assert len(settle_journals) == 1

        duplicate = ops.settle(
            settle_trade.trade_id,
            PaperSettlementRequest(
                winning_outcome=winning,
                source="lane4_fixture",
                source_id=f"settle:{settle_trade.trade_id}",
            ),
        )
        assert duplicate.state is PaperTradeState.CLOSED
        assert len(
            [
                entry
                for entry in ops.journal.list_entries(opportunity_id=settle_trade.opportunity_id)
                if entry.source == "paper_settlement"
            ]
        ) == 1
        assert _pool_tuple(ledger, VenueName.MATCHBOOK, "GBP") == settle_post_mb
        assert _pool_tuple(ledger, VenueName.KALSHI, "USD") == settle_post_k
        with pytest.raises(PaperOperationsError, match="already_settled"):
            ops.complete_validated_unwind(
                settle_trade.trade_id,
                quotes=tighten_reverse_quotes(second.quotes),
                fx=list(DEMO_FX),
                policy=UNWIND_POLICY,
            )
        assert ops.trades.get(settle_trade.trade_id).state is PaperTradeState.CLOSED
        assert _pool_tuple(ledger, VenueName.MATCHBOOK, "GBP") == settle_post_mb
        assert _pool_tuple(ledger, VenueName.KALSHI, "USD") == settle_post_k
    finally:
        repository.close()
        ledger.close()
