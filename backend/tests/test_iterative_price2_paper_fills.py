"""Iterative PAPER fills: one new Price-2 snapshot per tranche.

Fixture/demo books only. No live orders. Each cycle reads the scripted
books again and stops when the next snapshot fails an existing gate.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from decimal import Decimal
from pathlib import Path

import pytest
from test_execution_reprice_before_paper_entry import (
    _book,
    _fresh,
    _market,
    _open_trade,
    _run,
    _stale,
)

from sports_hedge.application.active_trade_recovery import consumed_native_by_level
from sports_hedge.application.adaptive_scheduler import (
    LANE_ACTIVE,
    LANE_EXECUTION_CANDIDATE,
    SchedulerWork,
    rank_scheduler_work,
)
from sports_hedge.application.execution_reprice import (
    CYCLE_ALLOCATION_CEILING,
    CYCLE_DUPLICATE,
    CYCLE_NO_INCREMENTAL_LIQUIDITY,
    EXECUTION_REPRICE_FAILED,
    EXECUTION_REPRICE_NO_LONGER_QUALIFYING,
    EXECUTION_REPRICE_SKEW,
    EXECUTION_REPRICE_STALE,
    ExecutionRepriceResult,
    continue_iterative_paper_fills,
)
from sports_hedge.application.execution_snapshot import ExecutionSnapshot
from sports_hedge.application.paper_operations import (
    PaperOperationsError,
    PaperOperationsService,
)
from sports_hedge.config import Settings
from sports_hedge.paper.trades import PaperTradeAuditEventType, PaperTradeTrancheKind
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger


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
    thin = _market("450")
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="iterate-depth",
        matchbook_payloads=[_stale(rich), _fresh(rich), _fresh(thin), _fresh(thin)],
        kalshi_books=[
            _book("0.20", "0.70", "500.00"),
            _book("0.20", "0.70", "500.00"),
            _book("0.20", "0.70", "450.00"),
            _book("0.40", "0.49", "450.00"),
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


def _liquidity(row: dict) -> list[dict]:
    payload = json.loads(row["snapshot_json"])
    return list(payload.get("paper_liquidity") or [])


def _assert_liquidity_math(rows: list[dict]) -> None:
    for row in rows:
        payload = json.loads(row["snapshot_json"])
        for item in payload.get("paper_liquidity") or []:
            observed = Decimal(item["observed"])
            used = Decimal(item["previously_consumed"])
            incremental = Decimal(item["incremental"])
            assert incremental == max(Decimal("0"), observed - used)
            assert incremental >= 0
        if row["cycle_outcome"] == "filled":
            assert row["tranche_id"]
            assert payload.get("cumulative_capital_gbp") not in (None, "")


@pytest.mark.asyncio
async def test_identical_book_does_not_refill_consumed_liquidity(tmp_path, monkeypatch) -> None:
    """Same displayed book: fill, residual, then stop. No repeated full turnover."""

    book_200 = _market("200")
    kalshi = _book("0.20", "0.70", "200.00")
    fresh = [_fresh(book_200) for _ in range(6)]
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="iterate-identical",
        matchbook_payloads=[_stale(book_200), *fresh],
        kalshi_books=[kalshi, *([kalshi] * 6)],
    )
    try:
        trade = _open_trade(bundle)
        assert Settings().sports_hedge_execution_enabled is False
        assert trade.places_orders is False
        audits = _audits(bundle, trade.opportunity_id)
        _assert_liquidity_math(audits)
        assert audits[0]["cycle_outcome"] == "filled"
        assert audits[0]["tranche_id"] == "opening"
        filled = [row for row in audits if row["cycle_outcome"] == "filled"]
        assert len(filled) == len({row["snapshot_id"] for row in filled})
        assert len(trade.tranches) == len(filled)
        assert all(item.execution_snapshot_id for item in trade.tranches)
        assert len({item.execution_snapshot_id for item in trade.tranches}) == len(trade.tranches)
        stopped = audits[-1]
        assert stopped["cycle_outcome"] == CYCLE_NO_INCREMENTAL_LIQUIDITY
        assert stopped["tranche_id"] is None
        last_levels = _liquidity(stopped)
        assert last_levels
        exhausted = [item for item in last_levels if Decimal(item["incremental"]) == 0]
        assert exhausted
        for item in exhausted:
            assert Decimal(item["previously_consumed"]) >= Decimal(item["observed"])
        opening_levels = {
            (item["venue"], item["outcome"], item["odds"]): Decimal(item["observed"])
            for item in _liquidity(audits[0])
        }
        for row in audits[1:]:
            for item in _liquidity(row):
                key = (item["venue"], item["outcome"], item["odds"])
                if key in opening_levels:
                    assert Decimal(item["observed"]) == opening_levels[key]
        native_filled: dict[tuple, Decimal] = {}
        for leg in trade.legs:
            if leg.filled_stake <= 0:
                continue
            key = (leg.venue.value, leg.outcome)
            native_filled[key] = native_filled.get(key, Decimal("0")) + leg.filled_stake
        observed_by_leg: dict[tuple, Decimal] = {}
        for item in _liquidity(audits[0]):
            key = (item["venue"], item["outcome"])
            observed_by_leg[key] = observed_by_leg.get(key, Decimal("0")) + Decimal(
                item["observed"]
            )
        for key, filled_stake in native_filled.items():
            assert filled_stake <= observed_by_leg[key]
        assert len(bundle.ledger.trades.list_all()) == 1
    finally:
        _close(bundle)


@pytest.mark.asyncio
async def test_larger_follow_up_book_allows_only_incremental_depth(tmp_path, monkeypatch) -> None:
    first = _market("200")
    grown = _market("300")
    small = _book("0.20", "0.70", "200.00")
    large = _book("0.20", "0.70", "300.00")
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="iterate-increment",
        matchbook_payloads=[_stale(first), _fresh(first), _fresh(grown), _fresh(grown)],
        kalshi_books=[small, small, large, large],
    )
    try:
        trade = _open_trade(bundle)
        audits = _audits(bundle, trade.opportunity_id)
        _assert_liquidity_math(audits)
        assert audits[0]["cycle_outcome"] == "filled"
        grown_rows = [
            row
            for row in audits
            if any(Decimal(item["observed"]) > Decimal("200") for item in _liquidity(row))
        ]
        assert grown_rows
        for row in grown_rows:
            for item in _liquidity(row):
                observed = Decimal(item["observed"])
                used = Decimal(item["previously_consumed"])
                assert Decimal(item["incremental"]) == max(Decimal("0"), observed - used)
                if observed > used:
                    assert Decimal(item["incremental"]) < observed or used == 0
        top_ups = [item for item in trade.tranches if item.kind is PaperTradeTrancheKind.TOP_UP]
        if top_ups:
            assert top_ups[0].execution_snapshot_id
            assert top_ups[0].execution_snapshot_id != trade.tranches[0].execution_snapshot_id
    finally:
        _close(bundle)


@pytest.mark.asyncio
async def test_skew_follow_up_stops_without_a_fill(tmp_path, monkeypatch) -> None:
    rich = _market()
    book = _book("0.20", "0.70")
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="iterate-skew",
        matchbook_payloads=[_stale(rich), _fresh(rich), _fresh(rich)],
        kalshi_books=[book, book, None],
    )
    try:
        trade = _open_trade(bundle)
        before = len(trade.tranches)

        class _SkewEngine:
            async def reprice_for_paper_entry(self, runtime, *, venues=()):
                del runtime, venues
                snapshot = ExecutionSnapshot(
                    catalogue_row_id="skew-row",
                    started_at=trade.opened_at,
                    retrievals=(),
                    accepted=False,
                    rejection_reason=EXECUTION_REPRICE_SKEW,
                    snapshot_id="skew-follow-up",
                )
                return ExecutionRepriceResult(
                    reason=EXECUTION_REPRICE_SKEW,
                    snapshot=snapshot,
                )

        plan = bundle.operations._plans[trade.opportunity_id]
        runtime = bundle.engine.item(bundle.row.catalogue_row_id)
        await continue_iterative_paper_fills(
            opportunity_id=trade.opportunity_id,
            runtime=runtime,
            engine=_SkewEngine(),
            watchlist=bundle.watchlist,
            pricing_lane="execution_candidate",
            entry_decision=plan.decision,
        )
        reloaded = bundle.operations.trades.get(trade.trade_id)
        assert reloaded is not None
        assert len(reloaded.tranches) == before
        audits = _audits(bundle, trade.opportunity_id)
        assert audits[-1]["rejection_reason"] == EXECUTION_REPRICE_SKEW
        assert audits[-1]["accepted"] == 0
        assert audits[-1]["tranche_id"] is None
    finally:
        _close(bundle)


@pytest.mark.asyncio
async def test_treasury_ceiling_blocks_a_later_tranche(tmp_path, monkeypatch) -> None:
    rich = _market()
    book = _book("0.20", "0.70")
    real = PaperOperationsService._assert_spendable_treasury
    seen = {"n": 0}

    def _limited(self, legs):
        seen["n"] += 1
        if seen["n"] > 1:
            raise PaperOperationsError("insufficient_spendable_treasury")
        return real(self, legs)

    monkeypatch.setattr(PaperOperationsService, "_assert_spendable_treasury", _limited)
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="iterate-treasury",
        matchbook_payloads=[_stale(rich), _fresh(rich), _fresh(rich), _fresh(rich)],
        kalshi_books=[book, book, book, book],
    )
    try:
        trade = _open_trade(bundle)
        top_ups = [
            item for item in trade.tranches if item.kind is PaperTradeTrancheKind.TOP_UP
        ]
        assert top_ups == []
        audits = _audits(bundle, trade.opportunity_id)
        assert audits[-1]["cycle_outcome"] == CYCLE_NO_INCREMENTAL_LIQUIDITY
        assert audits[-1]["tranche_id"] is None
        assert trade.capital_locked_gbp == trade.tranches[0].capital_locked_gbp
    finally:
        _close(bundle)


def test_active_trade_outranks_execution_candidate() -> None:
    active = rank_scheduler_work(SchedulerWork(lane=LANE_ACTIVE, work_id="open", seq=2))
    candidate = rank_scheduler_work(
        SchedulerWork(lane=LANE_EXECUTION_CANDIDATE, work_id="reprice", seq=1)
    )
    assert active.rank_key < candidate.rank_key


@pytest.mark.asyncio
async def test_active_plan_cannot_top_up_without_a_snapshot(tmp_path, monkeypatch) -> None:
    rich = _market()
    book = _book("0.20", "0.70")
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="iterate-no-bypass",
        matchbook_payloads=[_stale(rich), _fresh(rich), _stale(rich)],
        kalshi_books=[book, book, book],
    )
    try:
        trade = _open_trade(bundle)
        before = [item.tranche_id for item in trade.tranches]
        stored = bundle.operations._plans[trade.opportunity_id]
        refresh = stored.model_copy(
            update={"execution_authoritative": False, "execution_snapshot_json": None}
        )
        bundle.operations.maybe_top_up_open_trade(
            trade,
            plan=refresh,
            require_current_plan=True,
        )
        reloaded = bundle.operations.trades.get(trade.trade_id)
        assert reloaded is not None
        assert [item.tranche_id for item in reloaded.tranches] == before
        assert Settings().sports_hedge_execution_enabled is False
    finally:
        _close(bundle)


def _ledger_path(ledger) -> Path:
    row = ledger._connection.execute("PRAGMA database_list").fetchone()
    return Path(row["file"])


def _locked_pools(ledger) -> dict:
    return {
        (pool.venue, pool.native_currency): pool.locked_capital
        for pool in ledger.treasury.snapshot().pools
    }


def test_top_up_snapshot_identity_is_in_the_commit_source() -> None:
    fill_src = inspect.getsource(PaperOperationsService.fill_from_execution_snapshot)
    commit_src = inspect.getsource(PaperOperationsService._commit_top_up_tranche)
    assert "tranche.execution_snapshot_id = snapshot_id" not in fill_src
    assert "tranche.idempotency_key = snapshot_id" not in fill_src
    identity_at = commit_src.find("execution_snapshot_id=snapshot_id")
    save_at = commit_src.find("self.trades.save")
    assert identity_at >= 0
    assert save_at > identity_at


@pytest.mark.asyncio
async def test_top_up_snapshot_identity_survives_restart(tmp_path, monkeypatch) -> None:
    rich = _market("500")
    thin = _market("450")
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="iterate-atomic",
        matchbook_payloads=[_stale(rich), _fresh(rich), _fresh(thin), _fresh(thin)],
        kalshi_books=[
            _book("0.20", "0.70", "500.00"),
            _book("0.20", "0.70", "500.00"),
            _book("0.20", "0.70", "450.00"),
            _book("0.40", "0.49", "450.00"),
        ],
    )
    try:
        trade = _open_trade(bundle)
        opening = next(
            item for item in trade.tranches if item.kind is PaperTradeTrancheKind.OPENING
        )
        top_up = next(
            item for item in trade.tranches if item.kind is PaperTradeTrancheKind.TOP_UP
        )
        assert opening.idempotency_key.startswith("opening:")
        assert opening.execution_snapshot_id
        assert opening.idempotency_key != opening.execution_snapshot_id
        consumed = consumed_native_by_level(trade)
        path = _ledger_path(bundle.ledger)
        reopened = SqlitePaperLedger(path, auto_seed=True)
        try:
            loaded = reopened.trades.get(trade.trade_id)
            assert loaded is not None
            reloaded_top = next(
                item for item in loaded.tranches if item.kind is PaperTradeTrancheKind.TOP_UP
            )
            assert reloaded_top.execution_snapshot_id == top_up.execution_snapshot_id
            assert reloaded_top.idempotency_key == top_up.execution_snapshot_id
            assert consumed_native_by_level(loaded) == consumed
            assert any(
                event.event_type is PaperTradeAuditEventType.EXECUTION_SNAPSHOT
                and top_up.execution_snapshot_id in (event.detail or "")
                for event in loaded.audit
            )
            restarted = PaperOperationsService(
                watchlist=bundle.watchlist,
                settings=bundle.settings,
                ledger=reopened,
            )
            decision = bundle.operations._plans[trade.opportunity_id].decision
            audit = next(
                row
                for row in _audits(bundle, trade.opportunity_id)
                if row["snapshot_id"] == top_up.execution_snapshot_id
            )
            locked = loaded.capital_locked_gbp
            again = restarted.fill_from_execution_snapshot(
                trade.opportunity_id,
                decision,
                snapshot_id=top_up.execution_snapshot_id,
                snapshot_json=audit["snapshot_json"],
            )
            assert again == CYCLE_DUPLICATE
            after = reopened.trades.get(trade.trade_id)
            assert after is not None
            assert len(after.tranches) == len(loaded.tranches)
            assert after.capital_locked_gbp == locked
        finally:
            reopened.close()
    finally:
        _close(bundle)


@pytest.mark.asyncio
async def test_snapshot_bind_uses_one_trade_save_and_rolls_back(tmp_path, monkeypatch) -> None:
    rich = _market()
    book = _book("0.20", "0.70")
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="iterate-atomic-save",
        matchbook_payloads=[_stale(rich), _fresh(rich), _stale(rich)],
        kalshi_books=[book, book, book],
    )
    try:
        trade = _open_trade(bundle)
        before_ids = [item.tranche_id for item in trade.tranches]
        before_capital = trade.capital_locked_gbp
        before_locks = _locked_pools(bundle.ledger)
        audit = _audits(bundle, trade.opportunity_id)[0]
        payload = json.loads(audit["snapshot_json"])
        decision = bundle.operations._plans[trade.opportunity_id].decision
        real_save = bundle.operations.trades.save

        def _boom(item):
            if any(tranche.execution_snapshot_id == "atomic-rollback" for tranche in item.tranches):
                raise RuntimeError("injected_snapshot_save_failure")
            return real_save(item)

        rollback_payload = json.loads(json.dumps(payload))
        rollback_payload["snapshot_id"] = "atomic-rollback"
        bundle.operations.trades.save = _boom
        with pytest.raises(RuntimeError, match="injected_snapshot_save_failure"):
            bundle.operations.fill_from_execution_snapshot(
                trade.opportunity_id,
                decision,
                snapshot_id="atomic-rollback",
                snapshot_json=json.dumps(rollback_payload),
            )
        bundle.operations.trades.save = real_save
        rolled = bundle.operations.trades.get(trade.trade_id)
        assert rolled is not None
        assert [item.tranche_id for item in rolled.tranches] == before_ids
        assert not any(item.execution_snapshot_id == "atomic-rollback" for item in rolled.tranches)
        assert rolled.capital_locked_gbp == before_capital
        assert _locked_pools(bundle.ledger) == before_locks

        snapshot_id = "atomic-top-up-snapshot"
        payload["snapshot_id"] = snapshot_id
        snapshot_json = json.dumps(payload)
        saves = {"bound": 0, "unbound_new_tranche": 0}

        def _counting(item):
            new = [tranche for tranche in item.tranches if tranche.tranche_id not in before_ids]
            bound = [
                tranche
                for tranche in new
                if tranche.execution_snapshot_id == snapshot_id
                and tranche.idempotency_key == snapshot_id
            ]
            if new and len(bound) != len(new):
                saves["unbound_new_tranche"] += 1
            if bound:
                saves["bound"] += 1
            return real_save(item)

        bundle.operations.trades.save = _counting
        filled = bundle.operations.fill_from_execution_snapshot(
            trade.opportunity_id,
            decision,
            snapshot_id=snapshot_id,
            snapshot_json=snapshot_json,
        )
        assert filled == "filled"
        assert saves["bound"] == 1
        assert saves["unbound_new_tranche"] == 0
        bundle.operations.trades.save = real_save
        loaded = bundle.operations.trades.get(trade.trade_id)
        assert loaded is not None
        successful = next(
            item for item in loaded.tranches if item.execution_snapshot_id == snapshot_id
        )
        assert successful.idempotency_key == snapshot_id
        assert loaded.capital_locked_gbp != before_capital
        assert _locked_pools(bundle.ledger) != before_locks
    finally:
        _close(bundle)
