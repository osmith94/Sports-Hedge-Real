"""Iterative LIVE Price-2 accumulation after a fully hedged opening.

Fake venue transports only. Execution stays local to these tests. Paper
iterative behaviour is unchanged and is covered by test_iterative_price2_paper_fills.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from sports_hedge.application.execution_reprice import (
    CYCLE_DUPLICATE,
    CYCLE_FILLED,
    CYCLE_NOT_OPEN,
    ExecutionRepriceResult,
    continue_iterative_paper_fills,
)
from sports_hedge.application.execution_snapshot import ExecutionSnapshot
from sports_hedge.application.paper_operations import (
    PaperOperationsError,
    PaperOperationsService,
)
from sports_hedge.arbitrage.watchlist.economics import classification_for
from sports_hedge.arbitrage.watchlist.models import NearOpportunity, OpportunityStatus
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.execution.clients import (
    DeterministicExecutionTransport,
    MatchbookExecutionClient,
    PolymarketExecutionClient,
)
from sports_hedge.execution.models import VenueOrderResult, VenueOrderStatus
from sports_hedge.execution.runtime import ExecutionRuntime
from sports_hedge.liquidity.book import BookLevel
from sports_hedge.matching.markets import MarketMatchResult
from sports_hedge.paper.chain import PaperFillPlan
from sports_hedge.paper.fills import PaperOpportunityLeg
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision
from sports_hedge.paper.trades import (
    OPENING_TRANCHE_ID,
    PaperActiveTradePhase,
    PaperTradeAuditEventType,
    PaperTradeState,
    PaperTradeTrancheKind,
)
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger

NOW = datetime(2026, 10, 10, 12, tzinfo=UTC)
MARKET = "mkt-live-iter"
SECRET = "mb-exec-secret"


class _Scripted:
    def __init__(self, results: list[VenueOrderResult | str] | None = None) -> None:
        self.results = list(results or [])
        self.calls: list[object] = []
        self.cancel_calls: list[object] = []
        self.test_only = True
        self.transport_kind = "scripted"

    async def dispatch(self, request):
        self.calls.append(request)
        if self.results:
            item = self.results.pop(0)
            if item == "hang":
                await asyncio.sleep(60)
            if item == "fail":
                return VenueOrderResult(
                    venue=request.venue,
                    client_order_id=request.client_order_id,
                    venue_order_id=None,
                    status=VenueOrderStatus.REJECTED,
                    requested_size=request.requested_size,
                    filled_size=Decimal(0),
                    requested_price=request.requested_price,
                    average_fill_price=None,
                    submitted_at=NOW,
                    updated_at=NOW,
                    note="polymarket_native_minimum",
                )
            if isinstance(item, VenueOrderResult):
                return item.model_copy(update={"client_order_id": request.client_order_id})
        return VenueOrderResult(
            venue=request.venue,
            client_order_id=request.client_order_id,
            venue_order_id=f"venue:{request.client_order_id}",
            status=VenueOrderStatus.FILLED,
            requested_size=request.requested_size,
            filled_size=request.requested_size,
            requested_price=request.requested_price,
            average_fill_price=request.requested_price,
            submitted_at=NOW,
            updated_at=NOW,
        )

    async def cancel(self, request):
        self.cancel_calls.append(request)
        return VenueOrderResult(
            venue=request.venue,
            client_order_id=request.client_order_id,
            venue_order_id="offer-resting",
            status=VenueOrderStatus.CANCELLED,
            requested_size=request.requested_size,
            filled_size=Decimal("1.00"),
            requested_price=request.requested_price,
            average_fill_price=request.requested_price,
            submitted_at=NOW,
            updated_at=NOW,
        )


def _levels(odds: str, stake: str) -> list[BookLevel]:
    return [BookLevel(decimal_odds=Decimal(odds), available_stake=Decimal(stake))]


def _leg(
    venue: VenueName,
    *,
    event_id: str,
    market_id: str,
    runner_id: str,
    stake: str,
    odds: str,
    outcome: str,
    currency: str,
    depth: str = "80",
) -> PaperOpportunityLeg:
    return PaperOpportunityLeg(
        outcome=outcome,
        venue=venue,
        source_market_id=market_id,
        source_runner_id=runner_id,
        source_event_id=event_id,
        currency=currency,
        requested_stake=Decimal(stake),
        displayed_odds=Decimal(odds),
        levels=_levels(odds, depth),
    )


def _hedge(*, stake: str = "4", depth: str = "80") -> list[PaperOpportunityLeg]:
    return [
        _leg(
            VenueName.MATCHBOOK,
            event_id="evt-mb",
            market_id="mb-mkt",
            runner_id="101",
            stake=stake,
            odds="2.10",
            outcome="home",
            currency="GBP",
            depth=depth,
        ),
        _leg(
            VenueName.POLYMARKET,
            event_id="evt-pm",
            market_id="pm-mkt",
            runner_id="0xabc",
            stake=stake,
            odds="2.40",
            outcome="away",
            currency="USD",
            depth=depth,
        ),
    ]


def _snapshot(snapshot_id: str, legs: list[PaperOpportunityLeg]) -> dict:
    frozen = []
    payload_legs = []
    for leg in legs:
        if leg.venue is VenueName.MATCHBOOK:
            frozen.append(
                {
                    "venue": "matchbook",
                    "native_event_id": leg.source_event_id,
                    "native_market_id": leg.source_market_id,
                    "native_runner_id": leg.source_runner_id,
                    "native_side": "back",
                    "ladder_odds": str(leg.displayed_odds),
                    "native_stake": str(leg.requested_stake),
                }
            )
        elif leg.venue is VenueName.POLYMARKET:
            frozen.append(
                {
                    "venue": "polymarket",
                    "native_event_id": leg.source_event_id,
                    "native_market_id": leg.source_market_id,
                    "native_runner_id": leg.source_runner_id,
                    "order_type": "FOK",
                    "limit_price": str(leg.displayed_odds),
                    "native_amount": str(leg.requested_stake),
                    "native_shares": str(leg.requested_stake),
                    "tick_size": "0.01",
                    "minimum_order_size": "1",
                }
            )
        payload_legs.append(
            {
                "venue": leg.venue.value,
                "outcome": leg.outcome,
                "native_market_id": leg.source_market_id,
                "native_runner_id": leg.source_runner_id,
                "displayed_odds": str(leg.displayed_odds),
                "available_depth": str(
                    sum((level.available_stake for level in leg.levels), Decimal(0))
                ),
                "requested_stake": str(leg.requested_stake),
                "levels": [
                    (str(level.decimal_odds), str(level.available_stake)) for level in leg.levels
                ],
            }
        )
    return {
        "snapshot_id": snapshot_id,
        "accepted": True,
        "password": SECRET,
        "frozen_orders": frozen,
        "legs": payload_legs,
    }


def _plan(
    *legs: PaperOpportunityLeg,
    snapshot_id: str = "exec:live:1",
    net_edge: str = "0.15",
) -> PaperFillPlan:
    listed = list(legs)
    snapshot = _snapshot(snapshot_id, listed)
    return PaperFillPlan(
        opportunity_id=f"watch:{MARKET}",
        canonical_event_id="evt-canonical",
        canonical_market_id=MARKET,
        scanned_at=NOW,
        eligible_for_paper_simulation=True,
        settlement_equivalent=True,
        net_edge=Decimal(net_edge),
        legs=listed,
        fx_snapshots=[
            FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal(1), captured_at=NOW),
            FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"), captured_at=NOW),
        ],
        decision=PaperScanDecision(
            canonical_market_id=MARKET,
            canonical_event_id="evt-canonical",
            eligible_for_paper_simulation=True,
            market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=["test"]),
            solver_model="simple_complete_set",
            fill_legs=listed,
            scanned_at=NOW,
            minimum_net_edge=Decimal("0.01"),
            fx_snapshots=[
                FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal(1), captured_at=NOW),
                FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"), captured_at=NOW),
            ],
        ),
        execution_authoritative=True,
        execution_snapshot_json=json.dumps(snapshot),
        provenance="live_paper",
    )


def _watch(service: WatchlistService) -> None:
    service.repository.upsert_opportunity(
        NearOpportunity(
            opportunity_id=f"watch:{MARKET}",
            canonical_event_id="evt-canonical",
            canonical_market_id=MARKET,
            status=OpportunityStatus.TRIGGERED,
            classification=classification_for(OpportunityStatus.TRIGGERED),
            is_arbitrage=True,
            first_seen_at=NOW,
            last_seen_at=NOW,
        ),
        force_status=True,
    )


def _armed() -> Settings:
    return Settings(sports_hedge_mode="real", sports_hedge_execution_enabled=True)


def _clients(matchbook, polymarket) -> ExecutionRuntime:
    return ExecutionRuntime(
        matchbook=MatchbookExecutionClient(matchbook),
        polymarket=PolymarketExecutionClient(polymarket),
    )


def _ops(
    settings: Settings, *, runtime: ExecutionRuntime, ledger: SqlitePaperLedger
) -> PaperOperationsService:
    watchlist = WatchlistService(SqliteWatchlistRepository())
    _watch(watchlist)
    return PaperOperationsService(
        watchlist=watchlist,
        settings=settings,
        ledger=ledger,
        execution_runtime=runtime,
    )


def _patch_size(monkeypatch: pytest.MonkeyPatch) -> None:
    def _sized(self, trade, plan, **_kwargs):
        mapped = [leg for leg in plan.legs if leg.requested_stake > 0]
        try:
            self._assert_spendable_treasury(mapped)
        except PaperOperationsError:
            return None
        incremental = Decimal(0)
        fx = {item.currency.upper(): item.gbp_per_unit for item in plan.fx_snapshots}
        fx.setdefault("GBP", Decimal(1))
        for leg in mapped:
            rate = Decimal(1) if leg.currency == "GBP" else fx[leg.currency]
            incremental += leg.requested_stake * rate
        allocation = SimpleNamespace(accepted=True, guaranteed_profit=Decimal("0.46"))
        return mapped, allocation, incremental

    monkeypatch.setattr(PaperOperationsService, "_size_incremental_tranche", _sized)


def _open(ops: PaperOperationsService, plan: PaperFillPlan) -> None:
    ops._plans[plan.opportunity_id] = plan
    ops.capture_opening_fill(plan.opportunity_id, now=NOW)


def test_execution_disabled_submits_zero_live_orders() -> None:
    matchbook = _Scripted()
    polymarket = _Scripted()
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(Settings(), runtime=_clients(matchbook, polymarket), ledger=ledger)
    ops.simulate_fill = lambda *_a, **_k: None  # type: ignore[method-assign]
    ops._plans[_plan(*_hedge()).opportunity_id] = _plan(*_hedge())
    ops.capture_opening_fill(f"watch:{MARKET}", now=NOW)
    assert matchbook.calls == []
    assert polymarket.calls == []
    assert Settings().sports_hedge_execution_enabled is False


def test_first_full_live_hedge_then_distinct_price2_top_up(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_size(monkeypatch)
    matchbook = _Scripted()
    polymarket = _Scripted()
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(_armed(), runtime=_clients(matchbook, polymarket), ledger=ledger)
    opening = _plan(*_hedge(), snapshot_id="exec:live:open")
    _open(ops, opening)
    trade = ledger.trades.get_by_opportunity(opening.opportunity_id)
    assert trade is not None
    assert trade.state is PaperTradeState.OPEN
    assert trade.places_orders is True
    assert trade.active_trade_phase is PaperActiveTradePhase.ACCUMULATING
    follow = _plan(*_hedge(stake="3", depth="70"), snapshot_id="exec:live:top1")
    ops.maybe_top_up_open_trade(trade, now=NOW, plan=follow, require_current_plan=True)
    loaded = ledger.trades.get(trade.trade_id)
    assert loaded is not None
    top_ups = [item for item in loaded.tranches if item.kind is PaperTradeTrancheKind.TOP_UP]
    assert len(top_ups) == 1
    assert top_ups[0].execution_snapshot_id == "exec:live:top1"
    assert top_ups[0].idempotency_key == "exec:live:top1"
    opening_ids = {leg.fill_id for leg in loaded.legs if leg.tranche_id == OPENING_TRANCHE_ID}
    top_ids = {leg.fill_id for leg in loaded.legs if leg.tranche_id == top_ups[0].tranche_id}
    assert opening_ids and top_ids
    assert opening_ids.isdisjoint(top_ids)
    assert len(matchbook.calls) == 2
    assert len(polymarket.calls) == 2
    assert matchbook.calls[0].client_order_id != matchbook.calls[1].client_order_id
    assert matchbook.calls[0].price2_snapshot_id != matchbook.calls[1].price2_snapshot_id
    packages = [
        event
        for event in loaded.audit
        if event.event_type is PaperTradeAuditEventType.LIVE_PACKAGE_RECORDED
    ]
    assert len(packages) == 2
    follow_again = _plan(*_hedge(stake="2", depth="10"), snapshot_id="exec:live:stop")
    monkeypatch.setattr(
        PaperOperationsService,
        "_size_incremental_tranche",
        lambda *_a, **_k: None,
    )
    ops.maybe_top_up_open_trade(loaded, now=NOW, plan=follow_again, require_current_plan=True)
    stopped = ledger.trades.get(trade.trade_id)
    assert stopped is not None
    top_ups = [item for item in stopped.tranches if item.kind is PaperTradeTrancheKind.TOP_UP]
    assert len(top_ups) == 1
    assert len(matchbook.calls) == 2


def test_small_indicative_profit_is_not_an_absolute_size_veto(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_size(monkeypatch)
    matchbook = _Scripted()
    polymarket = _Scripted()
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(_armed(), runtime=_clients(matchbook, polymarket), ledger=ledger)
    opening = _plan(*_hedge(stake="1"), snapshot_id="exec:live:tiny-open")
    _open(ops, opening)
    trade = ledger.trades.get_by_opportunity(opening.opportunity_id)
    follow = _plan(
        *_hedge(stake="1", depth="50"),
        snapshot_id="exec:live:tiny-top",
        net_edge="0.1543",
    )
    ops.maybe_top_up_open_trade(trade, now=NOW, plan=follow, require_current_plan=True)
    loaded = ledger.trades.get(trade.trade_id)
    assert loaded is not None
    assert any(item.kind is PaperTradeTrancheKind.TOP_UP for item in loaded.tranches)
    assert len(matchbook.calls) == 2


def test_caps_and_native_funds_stop_further_live_tranches(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_size(monkeypatch)
    matchbook = _Scripted()
    polymarket = _Scripted()
    ledger = SqlitePaperLedger(tmp_path / "live-cap.sqlite", seed_gbp=Decimal(10))
    ops = _ops(_armed(), runtime=_clients(matchbook, polymarket), ledger=ledger)
    opening = _plan(*_hedge(stake="4"), snapshot_id="exec:live:cap-open")
    _open(ops, opening)
    trade = ledger.trades.get_by_opportunity(opening.opportunity_id)
    assert trade is not None
    def _empty(self, legs):
        del legs
        raise PaperOperationsError("insufficient_spendable_treasury")

    monkeypatch.setattr(PaperOperationsService, "_assert_spendable_treasury", _empty)
    follow = _plan(*_hedge(stake="4", depth="40"), snapshot_id="exec:live:cap-top")
    ops.maybe_top_up_open_trade(trade, now=NOW, plan=follow, require_current_plan=True)
    loaded = ledger.trades.get(trade.trade_id)
    assert loaded is not None
    kinds = [item.kind for item in loaded.tranches if item.kind is PaperTradeTrancheKind.TOP_UP]
    assert kinds == []
    assert len(matchbook.calls) == 1


def test_duplicate_snapshot_and_restart_do_not_submit_again(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _patch_size(monkeypatch)
    database = tmp_path / "live-iter.sqlite"
    matchbook = _Scripted()
    polymarket = _Scripted()
    ledger = SqlitePaperLedger(database)
    ops = _ops(_armed(), runtime=_clients(matchbook, polymarket), ledger=ledger)
    opening = _plan(*_hedge(), snapshot_id="exec:live:dup-open")
    _open(ops, opening)
    trade = ledger.trades.get_by_opportunity(opening.opportunity_id)
    follow = _plan(*_hedge(stake="2", depth="60"), snapshot_id="exec:live:dup-top")
    ops.maybe_top_up_open_trade(trade, now=NOW, plan=follow, require_current_plan=True)
    ops.maybe_top_up_open_trade(trade, now=NOW, plan=follow, require_current_plan=True)
    assert len(matchbook.calls) == 2
    outcome = ops.fill_from_execution_snapshot(
        follow.opportunity_id,
        follow.decision,
        snapshot_id="exec:live:dup-top",
        snapshot_json=follow.execution_snapshot_json or "{}",
    )
    assert outcome == CYCLE_DUPLICATE
    ledger._connection.close()

    restarted_mb = _Scripted()
    restarted_pm = _Scripted()
    restarted = SqlitePaperLedger(database, auto_seed=False)
    again = _ops(_armed(), runtime=_clients(restarted_mb, restarted_pm), ledger=restarted)
    loaded = restarted.trades.get_by_opportunity(opening.opportunity_id)
    assert loaded is not None
    top_ups = [item for item in loaded.tranches if item.kind is PaperTradeTrancheKind.TOP_UP]
    assert len(top_ups) == 1
    assert top_ups[0].execution_snapshot_id == "exec:live:dup-top"
    again._plans[opening.opportunity_id] = follow
    again.capture_opening_fill(opening.opportunity_id, now=NOW)
    again.maybe_top_up_open_trade(loaded, now=NOW, plan=follow, require_current_plan=True)
    assert restarted_mb.calls == []
    assert restarted_pm.calls == []
    client_ids = {
        json.loads(event.detail)["orders"][0]["client_order_id"]
        for event in loaded.audit
        if event.event_type is PaperTradeAuditEventType.LIVE_PACKAGE_RECORDED and event.detail
    }
    assert len(client_ids) == 2


def test_partial_live_top_up_halts_discretionary_accumulation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_size(monkeypatch)
    matchbook = _Scripted()
    polymarket = DeterministicExecutionTransport(filled_fraction=Decimal("0.5"))
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(_armed(), runtime=_clients(matchbook, polymarket), ledger=ledger)
    opening = _plan(*_hedge(), snapshot_id="exec:live:part-open")
    # Full opening first: scripted fills 100%. Partial only on the follow-up PM transport
    # would also partial the opening. Use two-phase transports via results list instead.
    polymarket = _Scripted()
    ops = _ops(
        _armed(),
        runtime=_clients(_Scripted(), polymarket),
        ledger=SqlitePaperLedger(":memory:"),
    )
    _open(ops, opening)
    trade = ops.trades.get_by_opportunity(opening.opportunity_id)
    assert trade is not None and trade.state is PaperTradeState.OPEN
    polymarket.results = ["fail"]
    follow = _plan(*_hedge(stake="2", depth="50"), snapshot_id="exec:live:part-top")
    ops.maybe_top_up_open_trade(trade, now=NOW, plan=follow, require_current_plan=True)
    loaded = ops.trades.get(trade.trade_id)
    assert loaded is not None
    assert loaded.active_trade_phase is PaperActiveTradePhase.LIVE_PARTIAL_EXPOSURE
    before = len(loaded.tranches)
    third = _plan(*_hedge(stake="1", depth="40"), snapshot_id="exec:live:part-blocked")
    ops.maybe_top_up_open_trade(loaded, now=NOW, plan=third, require_current_plan=True)
    reloaded = ops.trades.get(trade.trade_id)
    assert reloaded is not None
    assert len(reloaded.tranches) == before


def test_resting_matchbook_remainder_does_not_top_up() -> None:
    matchbook = _Scripted(
        [
            VenueOrderResult(
                venue=VenueName.MATCHBOOK,
                client_order_id="pending",
                venue_order_id="offer-resting",
                status=VenueOrderStatus.OPEN,
                requested_size=Decimal(4),
                filled_size=Decimal("1.00"),
                requested_price=Decimal("2.10"),
                average_fill_price=Decimal("2.10"),
                submitted_at=NOW,
                updated_at=NOW,
            )
        ]
    )
    polymarket = _Scripted()
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(_armed(), runtime=_clients(matchbook, polymarket), ledger=ledger)
    opening = _plan(*_hedge(), snapshot_id="exec:live:rest-open")
    _open(ops, opening)
    trade = ledger.trades.get_by_opportunity(opening.opportunity_id)
    assert trade is not None
    assert trade.state is PaperTradeState.PARTIAL
    follow = _plan(*_hedge(stake="2"), snapshot_id="exec:live:rest-top")
    ops.maybe_top_up_open_trade(trade, now=NOW, plan=follow, require_current_plan=True)
    assert len(matchbook.calls) == 1


def test_unknown_fill_does_not_paper_simulate_or_top_up() -> None:
    class _Unknown:
        test_only = True
        transport_kind = "scripted"
        calls: list[object]

        def __init__(self) -> None:
            self.calls = []

        async def dispatch(self, request):
            self.calls.append(request)
            return VenueOrderResult(
                venue=request.venue,
                client_order_id=request.client_order_id,
                venue_order_id=None,
                status=VenueOrderStatus.FAILED,
                requested_size=request.requested_size,
                filled_size=None,
                requested_price=request.requested_price,
                average_fill_price=None,
                submitted_at=NOW,
                updated_at=NOW,
            )

    matchbook = _Unknown()
    polymarket = _Unknown()
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(_armed(), runtime=_clients(matchbook, polymarket), ledger=ledger)

    class _Simulator:
        def simulate(self, *_args, **_kwargs):
            raise AssertionError("unknown live fill must not paper-simulate")

    ops.simulator = _Simulator()  # type: ignore[assignment]
    opening = _plan(*_hedge(), snapshot_id="exec:live:unk-open")
    _open(ops, opening)
    trade = ledger.trades.get_by_opportunity(opening.opportunity_id)
    assert trade is not None
    assert trade.live_fill_unknown is True
    follow = _plan(*_hedge(stake="2"), snapshot_id="exec:live:unk-top")
    ops.maybe_top_up_open_trade(trade, now=NOW, plan=follow, require_current_plan=True)
    assert len(matchbook.calls) == 1


def test_operator_stop_blocks_live_top_up(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_size(monkeypatch)
    matchbook = _Scripted()
    polymarket = _Scripted()
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(_armed(), runtime=_clients(matchbook, polymarket), ledger=ledger)
    opening = _plan(*_hedge(), snapshot_id="exec:live:stop-open")
    _open(ops, opening)
    trade = ledger.trades.get_by_opportunity(opening.opportunity_id)
    from sports_hedge.persistence.operator_scanner_settings import (
        effective_operator_scanner_settings,
    )

    def _stopped(settings):
        current = effective_operator_scanner_settings(settings)
        return current.model_copy(update={"scanner_stopped": True})

    monkeypatch.setattr(
        "sports_hedge.application.paper_operations.effective_operator_scanner_settings",
        _stopped,
    )
    follow = _plan(*_hedge(stake="2", depth="50"), snapshot_id="exec:live:stop-top")
    ops.maybe_top_up_open_trade(trade, now=NOW, plan=follow, require_current_plan=True)
    assert len(matchbook.calls) == 1


@pytest.mark.asyncio
async def test_continue_iterative_live_loop_and_duplicate_guard(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _patch_size(monkeypatch)
    matchbook = _Scripted()
    polymarket = _Scripted()
    ledger = SqlitePaperLedger(":memory:")
    ops = _ops(_armed(), runtime=_clients(matchbook, polymarket), ledger=ledger)
    opening = _plan(*_hedge(), snapshot_id="exec:live:loop-open")
    _open(ops, opening)
    follow = _plan(*_hedge(stake="2", depth="50"), snapshot_id="exec:live:loop-top")
    monkeypatch.setattr(
        "sports_hedge.application.execution_reprice._paper_operations",
        lambda _watchlist: ops,
    )

    def _fill(self, opportunity_id, decision, **kwargs):
        del decision
        trade = self._get_trade_by_opportunity(opportunity_id)
        if trade is None:
            return CYCLE_NOT_OPEN
        if kwargs.get("snapshot_id") != "exec:live:loop-top":
            return CYCLE_NOT_OPEN
        self.maybe_top_up_open_trade(trade, now=NOW, plan=follow, require_current_plan=True)
        return CYCLE_FILLED

    monkeypatch.setattr(PaperOperationsService, "fill_from_execution_snapshot", _fill)

    class _Engine:
        def __init__(self) -> None:
            self.n = 0

        async def reprice_for_paper_entry(self, runtime, *, venues=()):
            del runtime, venues
            self.n += 1
            if self.n > 1:
                snapshot = ExecutionSnapshot(
                    catalogue_row_id="loop-row",
                    started_at=NOW,
                    retrievals=(),
                    accepted=False,
                    rejection_reason="execution_reprice_no_longer_qualifying",
                    snapshot_id="exec:live:loop-stop",
                )
                return ExecutionRepriceResult(
                    reason="execution_reprice_no_longer_qualifying",
                    snapshot=snapshot,
                    decision=follow.decision,
                )
            snapshot = ExecutionSnapshot(
                catalogue_row_id="loop-row",
                started_at=NOW,
                retrievals=(),
                accepted=True,
                snapshot_id="exec:live:loop-top",
            )
            return ExecutionRepriceResult(
                decision=follow.decision,
                snapshot=snapshot,
            )

    engine = _Engine()
    first = asyncio.create_task(
        continue_iterative_paper_fills(
            opportunity_id=opening.opportunity_id,
            runtime=SimpleNamespace(),
            engine=engine,
            watchlist=ops.watchlist,
            pricing_lane="execution_candidate",
            entry_decision=opening.decision,
        )
    )
    second = asyncio.create_task(
        continue_iterative_paper_fills(
            opportunity_id=opening.opportunity_id,
            runtime=SimpleNamespace(),
            engine=engine,
            watchlist=ops.watchlist,
            pricing_lane="hot",
            entry_decision=opening.decision,
        )
    )
    await asyncio.gather(first, second)
    loaded = ops.trades.get_by_opportunity(opening.opportunity_id)
    assert loaded is not None
    assert len([item for item in loaded.tranches if item.kind is PaperTradeTrancheKind.TOP_UP]) == 1
    assert len(matchbook.calls) == 2


def test_iteration_yields_the_event_loop() -> None:
    src = inspect.getsource(
        __import__(
            "sports_hedge.application.execution_reprice",
            fromlist=["continue_iterative_paper_fills"],
        ).continue_iterative_paper_fills
    )
    assert "asyncio.sleep(0)" in src
    assert "asyncio.to_thread" in src


def test_live_accumulation_helper_excludes_partial_exposure() -> None:
    src = inspect.getsource(PaperOperationsService._maybe_top_up_open_trade_locked)
    assert "live_hedge_is_complete" in src
    assert "LIVE_PARTIAL_EXPOSURE" in src
    from sports_hedge.application.live_refresh import _accumulation_eligible
    from sports_hedge.application.paper_operations import live_hedge_is_complete

    assert "iterative_trade_is_open" in inspect.getsource(_accumulation_eligible)
    assert callable(live_hedge_is_complete)
