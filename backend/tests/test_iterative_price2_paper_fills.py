"""Iterative PAPER fills: one new Price-2 snapshot per tranche.

Fixture/demo books only. No live orders. Each cycle reads the scripted
books again and stops when the next snapshot fails an existing gate.
"""

from __future__ import annotations

import asyncio
import json
from decimal import Decimal

import pytest
from test_execution_reprice_before_paper_entry import (
    _book,
    _fresh,
    _market,
    _open_trade,
    _run,
    _stale,
)

from sports_hedge.application.execution_reprice import (
    CYCLE_ALLOCATION_CEILING,
    CYCLE_DUPLICATE,
    EXECUTION_REPRICE_FAILED,
    EXECUTION_REPRICE_NO_LONGER_QUALIFYING,
    EXECUTION_REPRICE_STALE,
    continue_iterative_paper_fills,
)
from sports_hedge.config import Settings
from sports_hedge.paper.trades import PaperTradeTrancheKind


def _close(bundle) -> None:
    bundle.repository.close()
    bundle.ledger.close()
    bundle.watchlist.repository.close()


def _audits(bundle, opportunity_id: str) -> list[dict]:
    rows = bundle.watchlist.repository.list_execution_snapshot_audits(opportunity_id)
    return sorted(rows, key=lambda row: int(row["execution_cycle"] or 0))


def _stakes(trade, tranche_id: str) -> dict:
    return {
        (leg.venue, leg.outcome): leg.requested_stake
        for leg in trade.legs
        if leg.tranche_id == tranche_id and leg.requested_stake > 0
    }


@pytest.mark.asyncio
async def test_persistent_book_fills_again_from_the_new_depth(tmp_path, monkeypatch) -> None:
    rich = _market("500")
    thin = _market("220")
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="iterate-depth",
        matchbook_payloads=[_stale(rich), _fresh(rich), _fresh(thin), _fresh(thin)],
        kalshi_books=[
            _book("0.20", "0.70", "500.00"),
            _book("0.20", "0.70", "500.00"),
            _book("0.20", "0.70", "220.00"),
            _book("0.40", "0.49", "220.00"),
        ],
    )
    try:
        assert len(bundle.operations.list_active_trades()) == 1
        assert len(bundle.ledger.trades.list_all()) == 1
        trade = _open_trade(bundle)
        assert trade.paper_only is True
        assert trade.places_orders is False
        assert Settings().sports_hedge_execution_enabled is False
        assert bundle.matchbook.list_events_calls == 0
        opening = next(
            item for item in trade.tranches if item.kind is PaperTradeTrancheKind.OPENING
        )
        top_ups = [
            item for item in trade.tranches if item.kind is PaperTradeTrancheKind.TOP_UP
        ]
        assert len(top_ups) == 1
        opening_stakes = _stakes(trade, opening.tranche_id)
        top_up_stakes = _stakes(trade, top_ups[0].tranche_id)
        assert opening_stakes != top_up_stakes
        assert sum(top_up_stakes.values()) < sum(opening_stakes.values())
        assert top_ups[0].execution_snapshot_id
        assert top_ups[0].execution_snapshot_id != opening.execution_snapshot_id
        assert top_ups[0].idempotency_key == top_ups[0].execution_snapshot_id
        locked = trade.capital_locked_gbp or Decimal("0")
        tranche_locked = sum((item.capital_locked_gbp for item in trade.tranches), Decimal("0"))
        assert locked == tranche_locked
        audits = _audits(bundle, trade.opportunity_id)
        assert [row["execution_cycle"] for row in audits] == [1, 2, 3]
        assert [row["accepted"] for row in audits] == [1, 1, 0]
        assert [row["cycle_outcome"] for row in audits] == ["filled", "filled", "rejected"]
        assert audits[0]["tranche_id"] == "opening"
        assert audits[1]["tranche_id"] == top_ups[0].tranche_id
        assert audits[2]["tranche_id"] is None
        assert audits[2]["rejection_reason"] == EXECUTION_REPRICE_NO_LONGER_QUALIFYING
        assert all(row["trade_id"] == trade.trade_id for row in audits[:2])
        snapshot_ids = [json.loads(row["snapshot_json"])["snapshot_id"] for row in audits]
        assert len(set(snapshot_ids)) == 3
        depths = []
        for row in audits[:2]:
            payload = json.loads(row["snapshot_json"])
            depths.append(max(Decimal(leg["available_depth"]) for leg in payload["legs"]))
        assert depths[1] < depths[0]
        assert len(bundle.matchbook.get_market_calls) == 4
        assert bundle.engine._execution_reprice_open == set()
    finally:
        _close(bundle)


@pytest.mark.asyncio
async def test_stale_follow_up_stops_without_another_fill(tmp_path, monkeypatch) -> None:
    rich = _market()
    book = _book("0.20", "0.70")
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="iterate-stale",
        matchbook_payloads=[_stale(rich), _fresh(rich), _stale(rich)],
        kalshi_books=[book, book, book],
    )
    try:
        trade = _open_trade(bundle)
        top_ups = [
            item for item in trade.tranches if item.kind is PaperTradeTrancheKind.TOP_UP
        ]
        assert top_ups == []
        audits = _audits(bundle, trade.opportunity_id)
        assert [row["cycle_outcome"] for row in audits] == ["filled", "rejected"]
        assert audits[1]["rejection_reason"] == EXECUTION_REPRICE_STALE
        assert len(bundle.matchbook.get_market_calls) == 3
    finally:
        _close(bundle)


@pytest.mark.asyncio
async def test_provider_failure_on_follow_up_stops(tmp_path, monkeypatch) -> None:
    rich = _market()
    book = _book("0.20", "0.70")
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="iterate-provider",
        matchbook_payloads=[_stale(rich), _fresh(rich), _fresh(rich)],
        kalshi_books=[book, book, None],
    )
    try:
        trade = _open_trade(bundle)
        assert len(trade.tranches) == 1
        audits = _audits(bundle, trade.opportunity_id)
        assert audits[-1]["accepted"] == 0
        assert audits[-1]["rejection_reason"] == EXECUTION_REPRICE_FAILED
        assert audits[-1]["cycle_outcome"] == "rejected"
        assert len(bundle.kalshi.book_calls) == 3
    finally:
        _close(bundle)


@pytest.mark.asyncio
async def test_allocation_ceiling_stops_without_another_tranche(tmp_path, monkeypatch) -> None:
    rich = _market()
    book = _book("0.20", "0.70")
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="iterate-cap",
        matchbook_payloads=[_stale(rich), _fresh(rich), _fresh(rich), _fresh(rich)],
        kalshi_books=[book, book, book, book],
        max_allocated_per_trade_gbp=1.0,
    )
    try:
        trade = _open_trade(bundle)
        assert trade.capital_locked_gbp is not None
        assert trade.capital_locked_gbp <= Decimal("1")
        assert trade.active_trade_phase.value == "monitoring_cap_reached"
        audits = _audits(bundle, trade.opportunity_id)
        assert audits[-1]["accepted"] == 1
        assert audits[-1]["cycle_outcome"] == CYCLE_ALLOCATION_CEILING
        assert audits[-1]["tranche_id"] is None
        assert all(row["cycle_outcome"] == "filled" for row in audits[:-1])
        assert len(bundle.matchbook.get_market_calls) == 4
        assert len(bundle.ledger.trades.list_all()) == 1
    finally:
        _close(bundle)


@pytest.mark.asyncio
async def test_same_snapshot_cannot_fill_twice(tmp_path, monkeypatch) -> None:
    rich = _market()
    book = _book("0.20", "0.70")
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="iterate-once",
        matchbook_payloads=[_stale(rich), _fresh(rich)],
        kalshi_books=[book, book],
    )
    try:
        trade = _open_trade(bundle)
        audits = _audits(bundle, trade.opportunity_id)
        filled = next(row for row in audits if row["cycle_outcome"] == "filled")
        decision = bundle.scan.seen[1]
        again = bundle.operations.fill_from_execution_snapshot(
            trade.opportunity_id,
            decision,
            snapshot_id=filled["snapshot_id"],
            snapshot_json=filled["snapshot_json"],
        )
        assert again == CYCLE_DUPLICATE
        reloaded = bundle.operations.trades.get(trade.trade_id)
        assert reloaded is not None
        assert len(reloaded.tranches) == 1
        assert len(bundle.ledger.trades.list_all()) == 1
    finally:
        _close(bundle)


@pytest.mark.asyncio
async def test_concurrent_follow_up_does_not_fetch_twice(tmp_path, monkeypatch) -> None:
    rich = _market()
    book = _book("0.20", "0.70")
    started = asyncio.Event()
    release = asyncio.Event()
    ready: dict = {}

    async def _hook(index: int) -> None:
        if index == 2:
            started.set()
            await release.wait()

    async def _drive():
        return await _run(
            tmp_path,
            monkeypatch,
            name="iterate-concurrent",
            matchbook_payloads=[_stale(rich), _fresh(rich), _stale(rich)],
            kalshi_books=[book, book, book],
            ready=ready,
            matchbook_hook=_hook,
        )

    driver = asyncio.create_task(_drive())
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        rows = ready["watchlist"].repository.list_opportunities()
        opportunity_id = rows[0].opportunity_id
        plan = ready["operations"]._plans[opportunity_id]
        runtime = ready["engine"].item(ready["row"].catalogue_row_id)
        second = asyncio.create_task(
            continue_iterative_paper_fills(
                opportunity_id=opportunity_id,
                runtime=runtime,
                engine=ready["engine"],
                watchlist=ready["watchlist"],
                pricing_lane=None,
                entry_decision=plan.decision,
            )
        )
        await asyncio.wait_for(second, timeout=2)
        release.set()
        bundle = await driver
        trade = _open_trade(bundle)
        assert len(trade.tranches) == 1
        assert len(bundle.matchbook.get_market_calls) == 3
        assert bundle.engine._execution_reprice_open == set()
        audits = _audits(bundle, trade.opportunity_id)
        assert [row["cycle_outcome"] for row in audits] == ["filled", "rejected"]
    finally:
        release.set()
        if not driver.done():
            await driver
