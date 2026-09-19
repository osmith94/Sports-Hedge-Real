"""Issue #346 Phase 4: item-completion economics + immediate paper capture.

Clock-injected. Deterministic fixture/demo providers. No live HTTP.
PAPER / read-only. Reuses persist_triggered_chain. No durable price queue.
"""

from __future__ import annotations

import inspect
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from sports_hedge.api import paper as paper_api
from sports_hedge.application.approved_market_catalogue import (
    FEE_STATUS_PARTIAL,
    FEE_STATUS_UNKNOWN,
    KalshiFeeSnapshotRecord,
)
from sports_hedge.application.capture_replay import FORBIDDEN_WRITE_METHODS
from sports_hedge.application.collector import CollectionReport
from sports_hedge.application.fixture_current_state import FixtureCurrentStateStore
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.price_engine import CataloguePriceEngine, PriceEnginePriority
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.arbitrage.watchlist.models import OpportunityStatus
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.paper.trades import PaperTradeState
from sports_hedge.persistence.paper import SqlitePaperScanRepository
from sports_hedge.venues import KalshiClient, MatchbookClient, PolymarketClient
from test_issue344_price_engine import (
    DISTANT_KICKOFF,
    FakeKalshi,
    NEAR_KICKOFF,
    NOW,
    _engine,
    _row,
)
from test_paper_eligible_auto_capture_contract import (
    MB_K,
    WRITE_TOKENS,
    _capture_rejections,
    _qualify,
)
from test_step8f_automatic_paper_entry import _ops_bundle


class PerItemScan:
    """Return a unique copy of a base decision per catalogue fixture."""

    def __init__(self, factory) -> None:
        self.factory = factory
        self.market_matcher = MarketMatcher()
        self.cost_resolver = None
        self.settings = None
        self.calls = 0
        self.seen: list[str] = []

    def scan_pair(self, *args: Any, **kwargs: Any) -> PaperScanDecision:
        del args
        self.calls += 1
        fixture = str(kwargs.get("fixture_canonical_event_id") or f"anon-{self.calls}")
        self.seen.append(fixture)
        return self.factory(fixture)


class EconomicsAwareScan:
    """Run existing cost/FX resolution against engine observations."""

    def __init__(self, inner: PaperScanService, base: PaperScanDecision) -> None:
        self.inner = inner
        self.base = base
        self.market_matcher = inner.market_matcher
        self.cost_resolver = inner.cost_resolver
        self.settings = inner.settings
        self.last: PaperScanDecision | None = None
        self.kalshi_fees: list[Any] = []

    def scan_pair(self, left, right, **kwargs: Any) -> PaperScanDecision:
        meta = right.metadata if isinstance(right.metadata, dict) else {}
        self.kalshi_fees.append(meta.get("kalshi_fee"))
        as_of = left.observed_at
        costs, cost_reasons = self.inner._resolve_costs(
            left,
            right,
            venue_costs=kwargs.get("venue_costs"),
            as_of=as_of,
        )
        fx, fx_reasons = self.inner._resolve_fx(
            left,
            right,
            fx_snapshots=kwargs.get("fx_snapshots"),
            as_of=as_of,
        )
        reasons = [*cost_reasons, *fx_reasons]
        if not any(cost.venue is VenueName.MATCHBOOK for cost in costs):
            reasons.append("missing_venue_cost:matchbook")
        if not any(snapshot.currency.upper() == "USD" for snapshot in fx):
            reasons.append("missing_fx_rate:USD")
        eligible = self.base.eligible_for_paper_simulation and not reasons
        decision = self.base.model_copy(
            update={
                "canonical_event_id": kwargs.get("fixture_canonical_event_id")
                or self.base.canonical_event_id,
                "fixture_canonical_event_id": kwargs.get("fixture_canonical_event_id"),
                "venue_costs": costs,
                "fx_snapshots": fx,
                "rejection_reasons": [*self.base.rejection_reasons, *reasons],
                "eligible_for_paper_simulation": eligible,
            }
        )
        self.last = decision
        return decision


def _unique_decision(scan, fixture_id: str, **updates: Any) -> PaperScanDecision:
    decision = _qualify(scan)
    payload = {
        "canonical_event_id": fixture_id,
        "fixture_canonical_event_id": fixture_id,
        "canonical_market_id": f"{decision.canonical_market_id}:{fixture_id}",
    }
    payload.update(updates)
    return decision.model_copy(update=payload)


def _audit(tmp_path: Path) -> SqlitePaperScanRepository:
    return SqlitePaperScanRepository(tmp_path / "paper-audit.sqlite")


def _bind(engine, *, scan, watchlist, ops, audit, monkeypatch) -> list:
    chain_calls: list[tuple[Any, dict[str, Any]]] = []
    real = ops.persist_triggered_chain

    def wrapped(decision, **kwargs):
        chain_calls.append((decision, kwargs))
        return real(decision, **kwargs)

    ops.persist_triggered_chain = wrapped

    def operations_factory(watchlist_arg=None, alerts=None):
        del alerts
        if watchlist_arg is not None:
            ops.watchlist = watchlist_arg
        return ops

    monkeypatch.setattr(paper_api, "get_paper_operations_service", operations_factory)
    paper_api.bind_price_engine_item_persist(
        engine, service=scan, audit=audit, watchlist=watchlist
    )
    return chain_calls


def _unknown_fee_snapshot() -> KalshiFeeSnapshotRecord:
    return KalshiFeeSnapshotRecord(
        snapshot_id="kfee:unknown-phase4",
        series_ticker="KXEPLBTTS",
        event_ticker="KXEPLBTTS-FEE",
        fee_resolution_status=FEE_STATUS_UNKNOWN,
        fee_resolution_error="missing_fee_type",
        captured_at=NOW,
        source="get_series",
    )


@pytest.mark.asyncio
async def test_first_completed_item_publishes_before_slow_sibling() -> None:
    published: list[str] = []

    async def on_item(decision, runtime) -> None:
        del decision
        published.append(runtime.identity.catalogue_row_id)

    fast = _row(suffix="fast", matchbook_market_id="316401", kalshi_event="KXEPLBTTS-FAST4")
    slow = _row(suffix="slow", matchbook_market_id="316402", kalshi_event="KXEPLBTTS-SLOW4")
    kalshi = FakeKalshi()
    kalshi.hang.add("KXEPLBTTS-SLOW4-BTTS")
    fixture_state = FixtureCurrentStateStore()
    engine, _mb, _ks, _layer = _engine(
        [fast, slow],
        kalshi=kalshi,
        fixture_state=fixture_state,
        on_item_decision=on_item,
    )
    result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    assert "amc-fast" in result.evaluated
    assert "amc-fast" in published
    assert published[0] == "amc-fast"
    assert "amc-slow" not in result.evaluated
    assert "amc-slow" not in published
    assert fixture_state.resolve_canonical_id(fast.canonical_event_id) is not None


@pytest.mark.asyncio
async def test_eligible_autofill_on_opens_at_item_completion(tmp_path: Path, monkeypatch) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    audit = _audit(tmp_path)
    try:
        row = _row(
            suffix="open",
            kickoff=NEAR_KICKOFF,
            matchbook_market_id="316410",
            kalshi_event="KXEPLBTTS-OPEN",
        )
        paper = PerItemScan(lambda fixture: _unique_decision(scan, fixture))
        engine, _mb, _ks, _layer = _engine([row], paper_scan=paper)
        chain_calls = _bind(
            engine, scan=scan, watchlist=watchlist, ops=ops, audit=audit, monkeypatch=monkeypatch
        )
        result = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
        assert "amc-open" in result.evaluated
        assert len(chain_calls) == 1
        trades = ops.list_active_trades()
        assert len(trades) == 1
        assert trades[0].state is PaperTradeState.OPEN
        assert trades[0].paper_only is True
        assert trades[0].places_orders is False
        snap = ledger.treasury.snapshot()
        assert snap.pool(VenueName.MATCHBOOK, "GBP").locked_capital > 0
        assert snap.pool(VenueName.KALSHI, "USD").locked_capital > 0
    finally:
        repository.close()
        ledger.close()
        audit.close()


@pytest.mark.asyncio
async def test_repeated_observation_is_idempotent(tmp_path: Path, monkeypatch) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    audit = _audit(tmp_path)
    try:
        row = _row(
            suffix="idem",
            kickoff=NEAR_KICKOFF,
            matchbook_market_id="316411",
            kalshi_event="KXEPLBTTS-IDEM",
        )
        paper = PerItemScan(lambda fixture: _unique_decision(scan, fixture))
        engine, _mb, _ks, _layer = _engine([row], paper_scan=paper, hot_interval=0)
        chain_calls = _bind(
            engine, scan=scan, watchlist=watchlist, ops=ops, audit=audit, monkeypatch=monkeypatch
        )
        first = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
        second = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
        assert "amc-idem" in first.evaluated
        assert "amc-idem" in second.evaluated
        trades = ops.list_active_trades()
        assert len(trades) == 1
        locked = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP").locked_capital
        assert locked > 0
        assert len(chain_calls) >= 2
        after = ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP").locked_capital
        assert after == locked
        assert len(ops.list_active_trades()) == 1
    finally:
        repository.close()
        ledger.close()
        audit.close()


@pytest.mark.asyncio
async def test_capture_gate_failure_records_durable_rejection(tmp_path: Path, monkeypatch) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    audit = _audit(tmp_path)
    try:
        row = _row(
            suffix="rej",
            kickoff=NEAR_KICKOFF,
            matchbook_market_id="316412",
            kalshi_event="KXEPLBTTS-REJ",
        )

        def factory(fixture: str) -> PaperScanDecision:
            decision = _unique_decision(scan, fixture)
            stale_legs = [
                leg.model_copy(update={"quote_age_ms": 50_000}) for leg in decision.fill_legs
            ]
            return decision.model_copy(update={"quote_age_ms": 50_000, "fill_legs": stale_legs})

        paper = PerItemScan(factory)
        engine, _mb, _ks, _layer = _engine([row], paper_scan=paper)
        _bind(engine, scan=scan, watchlist=watchlist, ops=ops, audit=audit, monkeypatch=monkeypatch)
        await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
        assert ops.list_active_trades() == []
        rows = watchlist.repository.list_opportunities()
        assert rows
        opportunity_id = rows[0].opportunity_id
        assert ops._entry_rejections.get(opportunity_id)
        assert _capture_rejections(watchlist, opportunity_id)
        watched = watchlist.repository.get(opportunity_id)
        assert watched is not None
        assert watched.status is OpportunityStatus.REJECTED
    finally:
        repository.close()
        ledger.close()
        audit.close()


@pytest.mark.asyncio
async def test_autofill_off_publishes_without_open_or_capture_attempt(
    tmp_path: Path, monkeypatch
) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=False)
    audit = _audit(tmp_path)
    try:
        row = _row(
            suffix="off",
            kickoff=NEAR_KICKOFF,
            matchbook_market_id="316413",
            kalshi_event="KXEPLBTTS-OFF",
        )
        paper = PerItemScan(lambda fixture: _unique_decision(scan, fixture))
        begin_calls: list[str] = []
        real_begin = watchlist.begin_paper_fill_attempt

        def capture_begin(*args: Any, **kwargs: Any):
            begin_calls.append("attempt")
            return real_begin(*args, **kwargs)

        watchlist.begin_paper_fill_attempt = capture_begin
        engine, _mb, _ks, _layer = _engine([row], paper_scan=paper)
        chain_calls = _bind(
            engine, scan=scan, watchlist=watchlist, ops=ops, audit=audit, monkeypatch=monkeypatch
        )
        result = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
        assert "amc-off" in result.evaluated
        assert chain_calls
        assert ops.list_active_trades() == []
        assert begin_calls == []
        watched = watchlist.repository.list_opportunities()
        assert watched
        assert watched[0].status is OpportunityStatus.TRIGGERED
        snap = ledger.treasury.snapshot()
        assert snap.pool(VenueName.MATCHBOOK, "GBP").locked_capital == 0
    finally:
        repository.close()
        ledger.close()
        audit.close()


@pytest.mark.asyncio
async def test_unknown_kalshi_fee_is_not_eligible_and_does_not_invent_zero(
    tmp_path: Path, monkeypatch
) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    audit = _audit(tmp_path)
    try:
        row = _row(
            suffix="fee",
            kickoff=NEAR_KICKOFF,
            matchbook_market_id="316414",
            kalshi_event="KXEPLBTTS-FEE",
        )
        row = row.model_copy(update={"kalshi_fee_snapshot_id": "kfee:unknown-phase4"})
        paper = EconomicsAwareScan(scan, _unique_decision(scan, "evt-fee"))
        engine, _mb, _ks, _layer = _engine([row], paper_scan=paper)
        engine.catalogue_store.upsert_fee_snapshot(_unknown_fee_snapshot())
        engine.catalogue_store.upsert_catalogue_row(row)
        engine.reconstruct()
        _bind(engine, scan=scan, watchlist=watchlist, ops=ops, audit=audit, monkeypatch=monkeypatch)
        result = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
        assert "amc-fee" in result.evaluated
        assert paper.last is not None
        assert paper.last.eligible_for_paper_simulation is False
        assert "unknown_required_venue_cost:kalshi" in paper.last.rejection_reasons
        assert paper.kalshi_fees == [None]
        assert not any(
            cost.venue is VenueName.KALSHI
            and cost.known_status.value == "known"
            and cost.rate == Decimal("0")
            for cost in paper.last.venue_costs
        )
        assert ops.list_active_trades() == []
    finally:
        repository.close()
        ledger.close()
        audit.close()


@pytest.mark.asyncio
async def test_missing_matchbook_commission_and_fx_fail_closed(
    tmp_path: Path, monkeypatch
) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    audit = _audit(tmp_path)
    try:
        row = _row(
            suffix="fx",
            kickoff=NEAR_KICKOFF,
            matchbook_market_id="316415",
            kalshi_event="KXEPLBTTS-FX",
        )
        paper = EconomicsAwareScan(scan, _unique_decision(scan, "evt-fx"))
        engine, _mb, _ks, _layer = _engine([row], paper_scan=paper)
        _bind(engine, scan=scan, watchlist=watchlist, ops=ops, audit=audit, monkeypatch=monkeypatch)
        result = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
        assert "amc-fx" in result.evaluated
        assert paper.last is not None
        assert paper.last.eligible_for_paper_simulation is False
        assert "missing_venue_cost:matchbook" in paper.last.rejection_reasons
        assert "missing_fx_rate:USD" in paper.last.rejection_reasons
        assert ops.list_active_trades() == []
    finally:
        repository.close()
        ledger.close()
        audit.close()


@pytest.mark.asyncio
async def test_stale_quote_blocks_open_with_existing_rejection(
    tmp_path: Path, monkeypatch
) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    audit = _audit(tmp_path)
    max_age = Settings().paper_entry_max_quote_age_ms
    try:
        row = _row(
            suffix="stale",
            kickoff=NEAR_KICKOFF,
            matchbook_market_id="316416",
            kalshi_event="KXEPLBTTS-STALE",
        )

        def factory(fixture: str) -> PaperScanDecision:
            decision = _unique_decision(scan, fixture)
            age = max_age
            stale_legs = [leg.model_copy(update={"quote_age_ms": age}) for leg in decision.fill_legs]
            return decision.model_copy(update={"quote_age_ms": age, "fill_legs": stale_legs})

        paper = PerItemScan(factory)
        engine, _mb, _ks, _layer = _engine([row], paper_scan=paper)
        _bind(engine, scan=scan, watchlist=watchlist, ops=ops, audit=audit, monkeypatch=monkeypatch)
        await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
        assert ops.list_active_trades() == []
        rows = watchlist.repository.list_opportunities()
        assert rows
        opportunity_id = rows[0].opportunity_id
        assert ops._entry_rejections.get(opportunity_id)
        rejected = _capture_rejections(watchlist, opportunity_id)
        assert rejected
    finally:
        repository.close()
        ledger.close()
        audit.close()


@pytest.mark.asyncio
async def test_background_item_publishes_promotes_and_captures_immediately(
    tmp_path: Path, monkeypatch
) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    audit = _audit(tmp_path)
    try:
        row = _row(
            suffix="bg",
            kickoff=DISTANT_KICKOFF,
            matchbook_market_id="316417",
            kalshi_event="KXEPLBTTS-BG",
        )
        fixture_state = FixtureCurrentStateStore()
        paper = PerItemScan(lambda fixture: _unique_decision(scan, fixture))
        engine, _mb, _ks, _layer = _engine(
            [row], paper_scan=paper, fixture_state=fixture_state
        )
        runtime = engine.item("amc-bg")
        assert runtime is not None
        assert runtime.priority is PriceEnginePriority.BACKGROUND
        assert row.canonical_event_id not in fixture_state.hot_identity_scope(NOW)
        _bind(engine, scan=scan, watchlist=watchlist, ops=ops, audit=audit, monkeypatch=monkeypatch)
        result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
        assert "amc-bg" in result.evaluated
        assert row.canonical_event_id in result.promotions
        assert row.canonical_event_id in fixture_state.hot_identity_scope(NOW)
        trades = ops.list_active_trades()
        assert len(trades) == 1
        assert trades[0].state is PaperTradeState.OPEN
    finally:
        repository.close()
        ledger.close()
        audit.close()


@pytest.mark.asyncio
async def test_sibling_persist_failure_does_not_stop_other_item(
    tmp_path: Path, monkeypatch
) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    audit = _audit(tmp_path)
    try:
        boom = _row(
            suffix="boom",
            kickoff=NEAR_KICKOFF,
            matchbook_market_id="316418",
            kalshi_event="KXEPLBTTS-BOOM",
        )
        ok = _row(
            suffix="ok",
            kickoff=NEAR_KICKOFF,
            matchbook_market_id="316419",
            kalshi_event="KXEPLBTTS-OK",
        )
        paper = PerItemScan(lambda fixture: _unique_decision(scan, fixture))
        engine, _mb, _ks, _layer = _engine([boom, ok], paper_scan=paper)
        _bind(engine, scan=scan, watchlist=watchlist, ops=ops, audit=audit, monkeypatch=monkeypatch)
        real_persist = paper_api.persist_price_engine_item_decision

        def selective(decision, **kwargs):
            if decision.canonical_event_id == boom.canonical_event_id:
                raise RuntimeError("item_persist_failed")
            return real_persist(decision, **kwargs)

        monkeypatch.setattr(paper_api, "persist_price_engine_item_decision", selective)
        result = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
        assert "amc-boom" in result.evaluated
        assert "amc-ok" in result.evaluated
        assert "amc-boom" in result.persist_failures
        assert "amc-ok" not in result.persist_failures
        boom_runtime = engine.item("amc-boom")
        ok_runtime = engine.item("amc-ok")
        assert boom_runtime is not None and boom_runtime.last_persist_error == "item_persist_failed"
        assert ok_runtime is not None and ok_runtime.last_persist_error is None
        trades = ops.list_active_trades()
        assert len(trades) == 1
        assert trades[0].canonical_event_id == ok.canonical_event_id
        assert any(issue.stage == "persist_capture" for issue in result.issues)
        assert result.scan_budget_exhausted is False
    finally:
        repository.close()
        ledger.close()
        audit.close()


@pytest.mark.asyncio
async def test_batch_end_does_not_recapture_item_completion_decision(
    tmp_path: Path, monkeypatch
) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    audit = _audit(tmp_path)
    try:
        row = _row(
            suffix="once",
            kickoff=NEAR_KICKOFF,
            matchbook_market_id="316420",
            kalshi_event="KXEPLBTTS-ONCE",
        )
        paper = PerItemScan(lambda fixture: _unique_decision(scan, fixture))
        engine, _mb, _ks, _layer = _engine([row], paper_scan=paper)
        chain_calls = _bind(
            engine, scan=scan, watchlist=watchlist, ops=ops, audit=audit, monkeypatch=monkeypatch
        )
        result = await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
        assert len(ops.list_active_trades()) == 1
        first_calls = len(chain_calls)
        report = CollectionReport(
            started_at=NOW,
            completed_at=NOW,
            matching_venues=list(MB_K),
            enabled_venues=list(MB_K),
            paper_decisions=list(result.decisions),
            scan_lane=ScanLane.HOT.value,
            scan_diagnostics={
                "price_engine": True,
                paper_api.PRICE_ENGINE_ITEM_COMPLETION_CAPTURE: True,
            },
        )
        paper_api._persist_collection_report(
            report,
            service=scan,
            audit=audit,
            watchlist=watchlist,
            scan_lane=ScanLane.HOT,
        )
        assert len(chain_calls) == first_calls
        assert len(ops.list_active_trades()) == 1
        cycles = audit.list_cycles(limit=5)
        assert cycles
    finally:
        repository.close()
        ledger.close()
        audit.close()


@pytest.mark.asyncio
async def test_opening_leg_failure_is_explicit_paper_fill_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    audit = _audit(tmp_path)
    try:
        row = _row(
            suffix="legs",
            kickoff=NEAR_KICKOFF,
            matchbook_market_id="316421",
            kalshi_event="KXEPLBTTS-LEGS",
        )

        def factory(fixture: str) -> PaperScanDecision:
            decision = _unique_decision(scan, fixture)
            return decision.model_copy(update={"fill_legs": []})

        paper = PerItemScan(factory)
        engine, _mb, _ks, _layer = _engine([row], paper_scan=paper)
        _bind(engine, scan=scan, watchlist=watchlist, ops=ops, audit=audit, monkeypatch=monkeypatch)
        await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
        assert ops.list_active_trades() == []
        rows = watchlist.repository.list_opportunities()
        assert rows
        opportunity_id = rows[0].opportunity_id
        assert ops._entry_rejections.get(opportunity_id) == "no_positive_opening_legs"
        assert _capture_rejections(watchlist, opportunity_id)
    finally:
        repository.close()
        ledger.close()
        audit.close()


@pytest.mark.asyncio
async def test_treasury_failure_is_explicit_paper_fill_rejected(
    tmp_path: Path, monkeypatch
) -> None:
    scan, watchlist, ops, repository, ledger = _ops_bundle(tmp_path, autofill=True)
    audit = _audit(tmp_path)
    try:
        row = _row(
            suffix="treas",
            kickoff=NEAR_KICKOFF,
            matchbook_market_id="316422",
            kalshi_event="KXEPLBTTS-TREAS",
        )
        paper = PerItemScan(lambda fixture: _unique_decision(scan, fixture))
        engine, _mb, _ks, _layer = _engine([row], paper_scan=paper)
        ledger._connection.execute("UPDATE paper_treasury_pools SET available_cash = '0'")
        ledger._connection.commit()
        _bind(engine, scan=scan, watchlist=watchlist, ops=ops, audit=audit, monkeypatch=monkeypatch)
        await engine.run_slice(PriceEnginePriority.HOT, now=NOW)
        assert ops.list_active_trades() == []
        rows = watchlist.repository.list_opportunities()
        assert rows
        opportunity_id = rows[0].opportunity_id
        assert ops._entry_rejections.get(opportunity_id)
        assert _capture_rejections(watchlist, opportunity_id)
    finally:
        repository.close()
        ledger.close()
        audit.close()


def test_phase4_preserves_paper_boundary_and_does_not_fork_capture() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    engine_src = inspect.getsource(CataloguePriceEngine)
    assert "persist_triggered_chain" not in engine_src
    assert "CREATE TABLE" not in engine_src
    assert "on_item_decision" in engine_src
    assert "PRICE_ENGINE_PERSIST_STAGE" in inspect.getsource(
        CataloguePriceEngine._handoff_item_decision
    )
    persist_src = inspect.getsource(paper_api.persist_price_engine_item_decision)
    assert "_persist_decision(" in persist_src
    assert "persist_triggered_chain" in persist_src
    chain_src = inspect.getsource(PaperOperationsService.persist_triggered_chain)
    assert "PAPER-ONLY autofill; no venue order placed" in chain_src
    tick_src = inspect.getsource(paper_api.server_owned_refresh_tick)
    assert "bind_price_engine_item_persist" in tick_src
    assert tick_src.index("bind_price_engine_item_persist") < tick_src.index(
        'resolved.lane == "background"'
    )
    assert paper_api.PRICE_ENGINE_ITEM_COMPLETION_CAPTURE in tick_src
    report_src = inspect.getsource(paper_api._persist_collection_report)
    assert "already_captured" in report_src
    for client in (MatchbookClient, PolymarketClient, KalshiClient):
        assert client.capabilities.execution_enabled is False
        for token in WRITE_TOKENS:
            assert not hasattr(client, token)
        for method in FORBIDDEN_WRITE_METHODS:
            assert not hasattr(client, method)
    assert FEE_STATUS_PARTIAL


def test_partial_fee_snapshot_is_not_attached_as_known() -> None:
    engine, _mb, _ks, _layer = _engine(
        [
            _row(
                suffix="partial",
                matchbook_market_id="316423",
                kalshi_event="KXEPLBTTS-PARTIAL",
            )
        ]
    )
    snapshot = KalshiFeeSnapshotRecord(
        snapshot_id="kfee:partial-phase4",
        series_ticker="KXEPLBTTS",
        fee_type="quadratic",
        fee_multiplier=None,
        fee_resolution_status=FEE_STATUS_PARTIAL,
        fee_resolution_error="partial_event_fee_override",
        captured_at=NOW,
        source="get_series",
    )
    engine.catalogue_store.upsert_fee_snapshot(snapshot)
    runtime = engine.item("amc-partial")
    assert runtime is not None
    runtime.identity.kalshi_fee_snapshot_id = snapshot.snapshot_id
    assert engine._fee_snapshot_payload(runtime.identity) is None
