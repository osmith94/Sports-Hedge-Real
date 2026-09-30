"""PAPER auto-settlement pause and multi-tranche manual settlement."""

from __future__ import annotations

import asyncio
import inspect
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest
from test_paper_auto_settlement import NOW, _leg
from test_paper_settlement_hotfix import _filled_trade, _persist_open
from test_paper_trade_lifecycle import _ops, independent_realised_pnl_gbp

from sports_hedge.accounting.paper_journal import gbp_is_balanced
from sports_hedge.api.paper import (
    complete_paper_unwind,
    paper_treasury_unwind,
    pause_settlement_scans,
    resume_settlement_scans,
)
from sports_hedge.application.live_refresh import (
    AUTO_SETTLE_PAUSED_SUMMARY,
    LiveRefreshCoordinator,
)
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.config import get_settings
from sports_hedge.domain.football import MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.canonical_results import (
    PAPER_MANUAL_SETTLEMENT_SOURCE,
    manual_settlement_source_id,
)
from sports_hedge.paper.trades import (
    PaperManualSettlementRequest,
    PaperTradeState,
)
from sports_hedge.persistence.operator_scanner_settings import (
    SqliteOperatorScannerSettingsStore,
)
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger


def _tranche_leg(
    *,
    venue: VenueName,
    outcome: str,
    fill_id: str,
    tranche_id: str,
    source_market_id: str,
):
    leg = _leg(
        venue=venue,
        outcome=outcome,
        source_event_id="1001" if venue is VenueName.MATCHBOOK else "KXEPLGAME-26SEP20NEWCHE",
        source_market_id=source_market_id,
        source_runner_id="301" if outcome == "yes" else "302",
        source_contract_id=None if venue is VenueName.MATCHBOOK else source_market_id,
        stake=Decimal(10),
    )
    leg.fill_id = fill_id
    leg.tranche_id = tranche_id
    return leg


def _multi_tranche_trade():
    return _filled_trade(
        family=MarketFamily.BOTH_TEAMS_TO_SCORE,
        extra_legs=[
            _tranche_leg(
                venue=VenueName.MATCHBOOK,
                outcome="yes",
                fill_id="mb-yes-1",
                tranche_id="opening",
                source_market_id="2001",
            ),
            _tranche_leg(
                venue=VenueName.MATCHBOOK,
                outcome="yes",
                fill_id="mb-yes-2",
                tranche_id="topup-1",
                source_market_id="2001",
            ),
            _tranche_leg(
                venue=VenueName.KALSHI,
                outcome="no",
                fill_id="ks-no-1",
                tranche_id="opening",
                source_market_id="KXEPLGAME-26SEP20NEWCHE-BTTS",
            ),
            _tranche_leg(
                venue=VenueName.KALSHI,
                outcome="no",
                fill_id="ks-no-2",
                tranche_id="topup-1",
                source_market_id="KXEPLGAME-26SEP20NEWCHE-BTTS",
            ),
        ],
    )


def test_manual_settlement_keeps_every_tranche_and_releases_paper_treasury(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "tranches.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    try:
        trade = _multi_tranche_trade()
        _persist_open(ops, trade)
        options = ops.settlement_options(trade.trade_id)
        assert len(options.legs) == 4
        assert [leg.fill_id for leg in options.legs] == [
            "mb-yes-1",
            "mb-yes-2",
            "ks-no-1",
            "ks-no-2",
        ]
        assert [leg.tranche_id for leg in options.legs] == [
            "opening",
            "topup-1",
            "opening",
            "topup-1",
        ]
        assert [(leg.venue, leg.outcome) for leg in options.legs].count(("matchbook", "yes")) == 2
        assert [(leg.venue, leg.outcome) for leg in options.legs].count(("kalshi", "no")) == 2
        persisted = ops.trades.get(trade.trade_id)
        expected = independent_realised_pnl_gbp(persisted, "yes")
        before_state = persisted.state
        settled = ops.settle_manual_result(
            trade.trade_id,
            PaperManualSettlementRequest(winning_outcome="yes", operator_note="FT BTTS"),
        )
        assert before_state is PaperTradeState.OPEN
        assert settled.state is PaperTradeState.CLOSED
        assert settled.settlement_source == PAPER_MANUAL_SETTLEMENT_SOURCE
        assert settled.settlement_source_id == manual_settlement_source_id(trade.trade_id)
        assert settled.realised_pnl_gbp == expected
        assert settled.capital_locked_gbp == Decimal(0)
        assert len(settled.legs) == 4
        postings = ops.journal.postings(opportunity_id=trade.opportunity_id)
        assert gbp_is_balanced(postings)
        snapshot = ledger.treasury.snapshot()
        for pool in snapshot.pools:
            assert pool.locked_capital == 0
    finally:
        repository.close()
        ledger.close()


class _RecordingAgent:
    calls: ClassVar[list[object]] = []
    started: ClassVar[asyncio.Event | None] = None
    release: ClassVar[asyncio.Event | None] = None

    def __init__(self, **kwargs: object) -> None:
        del kwargs

    async def run_cycle(self, now: object = None) -> SimpleNamespace:
        type(self).calls.append(now)
        started = type(self).started
        release = type(self).release
        if started is not None:
            started.set()
        if release is not None:
            await release.wait()
        return SimpleNamespace(settled_trade_ids=[])


def _coordinator(tmp_path: Path) -> tuple[LiveRefreshCoordinator, SqliteOperatorScannerSettingsStore]:
    store = SqliteOperatorScannerSettingsStore(tmp_path / "operator.sqlite")
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW, operator_settings_store=store)
    coordinator.configure_from_settings()
    return coordinator, store


@pytest.mark.asyncio
async def test_settlement_pause_blocks_auto_cycles_and_survives_reload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "sports_hedge.application.paper_settlement_agent.PaperSettlementAgent",
        _RecordingAgent,
    )
    monkeypatch.setattr(
        "sports_hedge.application.live_refresh._paper_settlement_dependencies",
        lambda: (object(), object()),
    )
    _RecordingAgent.calls = []
    _RecordingAgent.started = None
    _RecordingAgent.release = None
    coordinator, store = _coordinator(tmp_path)
    coordinator._next_settlement_due = NOW
    saved = coordinator.apply_settlement_scans_paused(True)
    assert saved.settlement_scans_paused is True
    assert saved.background_pricing_paused is False
    assert saved.universe_scans_paused is False
    assert saved.scanner_stopped is False
    assert coordinator.settlement_scans_paused is True
    assert AUTO_SETTLE_PAUSED_SUMMARY in (coordinator.status.settlement_operator_summary or "")
    await coordinator._maybe_run_paper_settlement()
    assert _RecordingAgent.calls == []
    blocked = {"operator_stopped", "background_paused", "universe_scheduled_paused"}
    assert coordinator.plan_hot_tick(now=NOW).reason not in blocked
    assert coordinator.plan_background_tick(now=NOW).reason not in blocked
    assert coordinator.plan_universe_tick(now=NOW).reason not in blocked
    assert coordinator.plan_active_trade_tick(now=NOW).reason not in blocked
    assert coordinator.background_pricing_paused is False
    assert coordinator.universe_scans_paused is False
    assert coordinator.operator_scanner_stopped is False

    reloaded = LiveRefreshCoordinator(clock=lambda: NOW, operator_settings_store=store)
    reloaded.configure_from_settings()
    assert reloaded.settlement_scans_paused is True
    assert reloaded.status.settlement_scans_paused is True
    reloaded._next_settlement_due = NOW
    await reloaded._maybe_run_paper_settlement()
    assert _RecordingAgent.calls == []

    resumed = reloaded.apply_settlement_scans_paused(False)
    assert resumed.settlement_scans_paused is False
    assert reloaded.settlement_scans_paused is False
    assert reloaded.status.settlement_operator_summary is None
    reloaded._next_settlement_due = NOW
    await reloaded._maybe_run_paper_settlement()
    assert len(_RecordingAgent.calls) == 1

    again = LiveRefreshCoordinator(clock=lambda: NOW, operator_settings_store=store)
    again.configure_from_settings()
    assert again.settlement_scans_paused is False
    store.close()


@pytest.mark.asyncio
async def test_in_flight_settlement_finishes_and_pause_blocks_the_next_scan(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "sports_hedge.application.paper_settlement_agent.PaperSettlementAgent",
        _RecordingAgent,
    )
    monkeypatch.setattr(
        "sports_hedge.application.live_refresh._paper_settlement_dependencies",
        lambda: (object(), object()),
    )
    started = asyncio.Event()
    release = asyncio.Event()
    _RecordingAgent.calls = []
    _RecordingAgent.started = started
    _RecordingAgent.release = release
    coordinator, store = _coordinator(tmp_path)
    coordinator._next_settlement_due = NOW
    task = asyncio.create_task(coordinator._maybe_run_paper_settlement())
    await asyncio.wait_for(started.wait(), timeout=2)
    coordinator.apply_settlement_scans_paused(True)
    assert coordinator._settlement_in_progress is True
    assert "in-flight scan may finish" in (coordinator.status.settlement_operator_summary or "")
    release.set()
    await task
    assert coordinator._settlement_in_progress is False
    assert len(_RecordingAgent.calls) == 1
    coordinator._next_settlement_due = NOW
    await coordinator._maybe_run_paper_settlement()
    assert len(_RecordingAgent.calls) == 1
    store.close()


def test_manual_settlement_still_runs_while_auto_settlement_is_paused(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paused-manual.sqlite")
    store = SqliteOperatorScannerSettingsStore(tmp_path / "operator.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    coordinator = LiveRefreshCoordinator(clock=lambda: NOW, operator_settings_store=store)
    coordinator.configure_from_settings()
    try:
        trade = _multi_tranche_trade()
        _persist_open(ops, trade)
        open_trade = ops.trades.get(trade.trade_id)
        audit_before = len(open_trade.audit)
        state_before = open_trade.state
        coordinator.apply_settlement_scans_paused(True)
        unchanged = ops.trades.get(trade.trade_id)
        assert unchanged.state is state_before
        assert len(unchanged.audit) == audit_before
        assert unchanged.capital_locked_native == open_trade.capital_locked_native
        settled = ops.settle_manual_result(
            trade.trade_id,
            PaperManualSettlementRequest(winning_outcome="no"),
        )
        assert coordinator.settlement_scans_paused is True
        assert settled.state is PaperTradeState.CLOSED
        assert settled.settlement_source == PAPER_MANUAL_SETTLEMENT_SOURCE
        snapshot = ledger.treasury.snapshot()
        for pool in snapshot.pools:
            assert pool.locked_capital == 0
    finally:
        repository.close()
        ledger.close()
        store.close()


def test_settlement_pause_introduces_no_live_execution() -> None:
    settings = get_settings()
    assert settings.sports_hedge_execution_enabled is False
    pause_src = inspect.getsource(LiveRefreshCoordinator.apply_settlement_scans_paused)
    cycle_src = inspect.getsource(LiveRefreshCoordinator._maybe_run_paper_settlement)
    route_src = "\n".join(
        (
            inspect.getsource(pause_settlement_scans),
            inspect.getsource(resume_settlement_scans),
            inspect.getsource(PaperOperationsService.settle_manual_result),
        )
    )
    for blob in (pause_src, cycle_src, route_src):
        assert "place_order" not in blob
        assert "cancel_order" not in blob
        assert "sign_wallet" not in blob
    assert "settlement_scans_paused" not in inspect.getsource(
        PaperOperationsService.settle_manual_result
    )
    assert "settlement_scans_paused" not in inspect.getsource(complete_paper_unwind)
    assert "settlement_scans_paused" not in inspect.getsource(paper_treasury_unwind)
    assert "Does not add a fifth create_task" in cycle_src
    assert "paper_settlement_interval_seconds" in cycle_src
