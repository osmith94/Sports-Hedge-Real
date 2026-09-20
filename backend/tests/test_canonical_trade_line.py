"""Canonical market-line persistence for Active Trades.

Line comes from CanonicalMarket / MarketSnapshot.market_line only.
Never inferred from odds or parsed from provider display names.
"""

from __future__ import annotations

import inspect
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from sports_hedge.accounting.paper_journal import DataProvenance
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.arbitrage.watchlist.adapter import observation_from_paper_decision
from sports_hedge.arbitrage.watchlist.models import (
    OpportunityClassification,
    OpportunityStatus,
    WatchObservation,
)
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.domain.football import (
    LINE_PARAMETER_FAMILIES,
    FootballPeriod,
    MarketFamily,
    format_stored_line,
    line_push_possible,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.models import MarketSnapshot
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.active_trade_journal import ActiveTradeEventType, ActiveTradeReasonCode
from sports_hedge.paper.chain import PaperFillPlan
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.paper.trades import PaperTrade, PaperTradeState
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger


NOW = datetime(2026, 9, 20, 17, 0, tzinfo=UTC)


def test_format_stored_line_and_safe_half_line_semantics() -> None:
    assert format_stored_line(Decimal("2.5")) == "2.5"
    assert format_stored_line(Decimal("2.50")) == "2.5"
    assert format_stored_line(Decimal("2.0")) == "2"
    assert format_stored_line(Decimal("-0.5")) == "-0.5"
    assert format_stored_line(None) is None
    assert line_push_possible(Decimal("2.5")) is False
    assert line_push_possible(Decimal("2.0")) is True
    assert line_push_possible(Decimal("2.25")) is None
    assert MarketFamily.TOTAL_GOALS in LINE_PARAMETER_FAMILIES
    assert MarketFamily.MATCH_RESULT not in LINE_PARAMETER_FAMILIES


def test_adapter_copies_snapshot_market_line_not_odds() -> None:
    decision = PaperScanDecision(
        scanned_at=NOW,
        canonical_event_id="evt-1",
        canonical_market_id="mkt-total-2-5",
        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=[]),
    )
    history = [
        MarketSnapshot(
            observed_at=NOW,
            venue=VenueName.MATCHBOOK,
            canonical_event_id="evt-1",
            canonical_market_id="mkt-total-2-5",
            canonical_outcome="over",
            market_family=MarketFamily.TOTAL_GOALS,
            period=FootballPeriod.FULL_TIME,
            market_line=Decimal("2.5"),
            decimal_odds=Decimal("1.935"),
            metadata={"native_currency": "GBP"},
        )
    ]
    mapped = observation_from_paper_decision(decision, history)
    assert mapped is not None
    assert mapped.line == Decimal("2.5")
    assert mapped.line != history[0].decimal_odds
    src = inspect.getsource(observation_from_paper_decision)
    assert "snapshot.market_line" in src
    assert "decimal_odds" not in src.split("line=")[1][:80]


def test_adapter_does_not_invent_line_from_missing_snapshot() -> None:
    decision = PaperScanDecision(
        scanned_at=NOW,
        canonical_event_id="evt-1",
        canonical_market_id="mkt-total-goals-2.5-over",
        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=[]),
    )
    mapped = observation_from_paper_decision(decision, history=())
    assert mapped is not None
    assert mapped.line is None


def test_watchlist_persists_and_reloads_canonical_line() -> None:
    repo = SqliteWatchlistRepository()
    service = WatchlistService(repo)
    observed = WatchObservation(
        observed_at=NOW,
        canonical_event_id="evt-1",
        canonical_market_id="mkt-total-2-5",
        market_family=MarketFamily.TOTAL_GOALS,
        period=FootballPeriod.FULL_TIME,
        line=Decimal("2.5"),
        trigger_net_edge=Decimal("0.01"),
        current_net_edge=Decimal("0.012"),
        solver_is_arbitrage=True,
        eligible_for_paper_simulation=True,
    )
    stored = service.observe(observed)
    assert stored.line == Decimal("2.5")
    reloaded = repo.get(stored.opportunity_id)
    assert reloaded is not None
    assert reloaded.line == Decimal("2.5")


def test_watchlist_missing_line_stays_none_despite_name() -> None:
    repo = SqliteWatchlistRepository()
    service = WatchlistService(repo)
    observed = WatchObservation(
        observed_at=NOW,
        canonical_event_id="evt-1",
        canonical_market_id="total-goals-2.5-over-under",
        market_family=MarketFamily.TOTAL_GOALS,
        period=FootballPeriod.FULL_TIME,
        line=None,
        trigger_net_edge=Decimal("0.01"),
    )
    stored = service.observe(observed)
    assert stored.line is None
    reloaded = repo.get(stored.opportunity_id)
    assert reloaded is not None
    assert reloaded.line is None


def test_paper_trade_persists_canonical_line_and_does_not_parse_label() -> None:
    ledger = SqlitePaperLedger(":memory:")
    with_line = PaperTrade(
        trade_id="ptrade-line",
        opportunity_id="opp-line",
        market_family=MarketFamily.TOTAL_GOALS,
        market_label="total goals",
        line=Decimal("2.5"),
        state=PaperTradeState.OPEN,
        opened_at=NOW,
        last_updated_at=NOW,
    )
    ledger.trades.save(with_line)
    loaded = ledger.trades.get("ptrade-line")
    assert loaded is not None
    assert loaded.line == Decimal("2.5")

    historical = PaperTrade(
        trade_id="ptrade-legacy",
        opportunity_id="opp-legacy",
        market_family=MarketFamily.TOTAL_GOALS,
        market_label="Total Goals 2.5 Over/Under",
        line=None,
        state=PaperTradeState.OPEN,
        opened_at=NOW,
        last_updated_at=NOW,
    )
    ledger.trades.save(historical)
    legacy = ledger.trades.get("ptrade-legacy")
    assert legacy is not None
    assert legacy.line is None
    assert "2.5" in (legacy.market_label or "")


def test_paper_trade_line_column_added_without_guessing(tmp_path: Path) -> None:
    path = tmp_path / "legacy-line.sqlite"
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE paper_trades (
            trade_id TEXT PRIMARY KEY,
            opportunity_id TEXT NOT NULL UNIQUE,
            canonical_event_id TEXT,
            canonical_market_id TEXT,
            settlement_key TEXT,
            solver_model TEXT,
            market_family TEXT,
            period TEXT,
            competition TEXT,
            home_team TEXT,
            away_team TEXT,
            fixture_label TEXT,
            market_label TEXT,
            state TEXT NOT NULL,
            opened_at TEXT NOT NULL,
            last_updated_at TEXT NOT NULL,
            settled_at TEXT,
            guaranteed_profit_gbp_at_open TEXT,
            realised_pnl_gbp TEXT,
            capital_locked_native_json TEXT NOT NULL,
            capital_locked_gbp TEXT,
            settlement_outcome TEXT,
            settlement_source TEXT,
            settlement_source_id TEXT,
            settlement_detail TEXT,
            provenance TEXT NOT NULL,
            fx_snapshots_json TEXT NOT NULL,
            venue_costs_json TEXT NOT NULL,
            entry_risk_json TEXT,
            close_risks_json TEXT NOT NULL DEFAULT '[]',
            close_fills_json TEXT NOT NULL DEFAULT '[]',
            position_management_json TEXT
        )
        """
    )
    conn.execute(
        """
        INSERT INTO paper_trades (
            trade_id, opportunity_id, market_family, market_label, state,
            opened_at, last_updated_at, capital_locked_native_json, provenance,
            fx_snapshots_json, venue_costs_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "ptrade-old",
            "opp-old",
            "total_goals",
            "Total Goals 2.5",
            "OPEN",
            NOW.isoformat(),
            NOW.isoformat(),
            "{}",
            "fixture_demo",
            "[]",
            "[]",
        ),
    )
    conn.commit()
    conn.close()

    ledger = SqlitePaperLedger(path, auto_seed=False)
    cols = {
        row[1] for row in ledger._connection.execute("PRAGMA table_info(paper_trades)")
    }
    assert "line" in cols
    loaded = ledger.trades.get("ptrade-old")
    assert loaded is not None
    assert loaded.line is None
    assert loaded.market_label == "Total Goals 2.5"


def test_new_trade_shell_copies_opportunity_line() -> None:
    ops = PaperOperationsService(watchlist=WatchlistService())
    plan = PaperFillPlan(
        opportunity_id="opp-shell",
        canonical_event_id="evt-1",
        canonical_market_id="mkt-total-2-5",
        scanned_at=NOW,
        eligible_for_paper_simulation=True,
        settlement_equivalent=True,
        decision=PaperScanDecision(
            market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=[]),
            solver_model="simple_complete_set",
        ),
    )
    from sports_hedge.arbitrage.watchlist.models import NearOpportunity

    opportunity = NearOpportunity(
        opportunity_id="opp-shell",
        canonical_event_id="evt-1",
        canonical_market_id="mkt-total-2-5",
        market_family=MarketFamily.TOTAL_GOALS,
        period=FootballPeriod.FULL_TIME,
        line=Decimal("2.5"),
        home_team="Arsenal",
        away_team="Chelsea",
        status=OpportunityStatus.TRIGGERED,
        classification=OpportunityClassification.TRIGGERED_OPPORTUNITY,
        trigger_net_edge=Decimal("0.01"),
        first_seen_at=NOW,
        last_seen_at=NOW,
    )
    trade = ops._new_trade_shell(plan, opportunity, NOW, DataProvenance.FIXTURE_DEMO)
    assert trade.line == Decimal("2.5")
    missing = opportunity.model_copy(update={"line": None})
    blank = ops._new_trade_shell(plan, missing, NOW, DataProvenance.FIXTURE_DEMO)
    assert blank.line is None


def test_active_trade_journal_uses_stored_line_not_market_label() -> None:
    ledger = SqlitePaperLedger(":memory:")
    ops = PaperOperationsService(watchlist=WatchlistService(), ledger=ledger)
    trade = PaperTrade(
        trade_id="ptrade-journal-line",
        opportunity_id="opp-journal-line",
        market_family=MarketFamily.TOTAL_GOALS,
        market_label="total goals",
        line=Decimal("2.5"),
        state=PaperTradeState.OPEN,
        opened_at=NOW,
        last_updated_at=NOW,
    )
    assert ops.record_active_lifecycle_event(
        trade,
        event_type=ActiveTradeEventType.PROMOTED_TO_ACTIVE,
        reason_code=ActiveTradeReasonCode.PROMOTED,
        operator_copy="promoted",
        occurred_at=NOW,
        dedupe_key="journal-line-1",
    )
    events = ops.query_active_trade_events(trade_id=trade.trade_id)
    assert events[0].line == "2.5"
    assert events[0].line != trade.market_label
    src = inspect.getsource(PaperOperationsService.record_active_lifecycle_event)
    assert "format_stored_line(trade.line)" in src
    assert "trade.market_label" not in src
