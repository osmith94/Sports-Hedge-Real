"""Issue #446: auto-settlement diagnostics, legacy identity, and manual result close."""

from __future__ import annotations

import inspect
from datetime import timedelta
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from sports_hedge.accounting.paper_journal import DataProvenance, gbp_is_balanced
from sports_hedge.application.live_refresh import LiveRefreshCoordinator
from sports_hedge.application.paper_operations import PaperOperationsError
from sports_hedge.application.paper_settlement_agent import PaperSettlementAgent
from sports_hedge.application.provider_access import (
    DEFAULT_PROVIDER_CONCURRENCY,
    PRICE_ENGINE_SETTLEMENT_LANE,
)
from sports_hedge.arbitrage.allocation.adapters import exposures_from_trades
from sports_hedge.domain.football import MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import MarketAction
from sports_hedge.paper.canonical_results import (
    PAPER_MANUAL_SETTLEMENT_SOURCE,
    canonical_result_space,
    manual_settlement_source_id,
)
from sports_hedge.paper.result_resolution import PAPER_AUTO_SETTLEMENT_SOURCE
from sports_hedge.paper.settlement import PaperSettlementError, compute_paper_settlement
from sports_hedge.paper.trades import (
    PaperManualSettlementRequest,
    PaperTrade,
    PaperTradeAuditEventType,
    PaperTradeState,
    SettlementReconciliationStatus,
)
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from sports_hedge.paper.models import FxRateSnapshot
from test_paper_auto_settlement import (
    NOW,
    FakeKalshi,
    FakeMatchbook,
    _graded_event,
    _graded_matchbook_btts,
    _kalshi_settled,
    _leg,
    _trade,
)
from test_paper_trade_lifecycle import (
    _ops,
    independent_realised_pnl_gbp,
)
from venue_cost_helpers import matchbook_kalshi_costs, profit_commission_cost


YESTERDAY = NOW - timedelta(days=1)


def _filled_trade(**kwargs) -> PaperTrade:
    trade = _trade(**kwargs)
    trade.home_team = "Newcastle"
    trade.away_team = "Arsenal"
    trade.fixture_label = "Newcastle v Arsenal"
    trade.opened_at = YESTERDAY
    trade.last_updated_at = YESTERDAY
    trade.provenance = DataProvenance.LIVE_PAPER
    trade.fx_snapshots = [
        FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1"), source="test", captured_at=NOW),
        FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"), source="test", captured_at=NOW),
    ]
    trade.venue_costs = matchbook_kalshi_costs(captured_at=NOW)
    for leg in trade.legs:
        if leg.opening_action is None:
            leg.opening_action = (
                MarketAction.BUY if leg.venue is VenueName.KALSHI else MarketAction.BACK
            )
        if leg.canonical_state is None:
            leg.canonical_state = leg.outcome
    return trade


def _persist_open(ops, trade: PaperTrade) -> PaperTrade:
    assert ops.trades is not None
    ops.trades.save(trade)
    if ops.ledger is not None:
        for leg in trade.legs:
            if leg.filled_stake <= 0:
                continue
            from sports_hedge.treasury.models import TreasuryLockRequest

            rate = ops.ledger.treasury.lock_fx_rate(leg.venue, leg.currency)
            ops.ledger.treasury.lock_capital(
                [
                    TreasuryLockRequest(
                        venue=leg.venue,
                        native_currency=leg.currency,
                        amount_native=leg.filled_stake,
                        lock_id=leg.fill_id or f"lock-{leg.venue.value}-{leg.outcome}",
                        trade_id=trade.trade_id,
                        opportunity_id=trade.opportunity_id,
                        fx_rate_gbp_per_unit=rate,
                    )
                ],
                occurred_at=trade.opened_at,
            )
    return ops.trades.get(trade.trade_id)


@pytest.mark.asyncio
async def test_active_trade_worker_runs_settlement_on_restart_and_cadence() -> None:
    src = inspect.getsource(LiveRefreshCoordinator._maybe_run_paper_settlement)
    assert "PaperSettlementAgent" in src
    assert "due is not None and now < due" in src
    loop = inspect.getsource(LiveRefreshCoordinator._active_trade_loop)
    assert "await self._maybe_run_paper_settlement()" in loop
    start = inspect.getsource(LiveRefreshCoordinator.start_server_loop)
    assert start.count("asyncio.create_task") == 4
    assert "settlement-worker" not in start
    assert PRICE_ENGINE_SETTLEMENT_LANE == "settlement"
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.MATCHBOOK] == 4
    assert DEFAULT_PROVIDER_CONCURRENCY[VenueName.KALSHI] == 4
    agent_src = inspect.getsource(PaperSettlementAgent)
    assert "place_order" not in agent_src
    assert "cancel_order" not in agent_src


@pytest.mark.asyncio
async def test_legacy_kalshi_ticker_without_event_id_auto_settles(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "legacy-kalshi.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    try:
        trade = _filled_trade(
            extra_legs=[
                _leg(
                    venue=VenueName.KALSHI,
                    outcome="yes",
                    source_event_id="",
                    source_market_id="KXEPLGAME-26SEP20NEWCHE-BTTS",
                    source_contract_id="KXEPLGAME-26SEP20NEWCHE-BTTS",
                ),
                _leg(
                    venue=VenueName.KALSHI,
                    outcome="no",
                    source_event_id="",
                    source_market_id="KXEPLGAME-26SEP20NEWCHE-BTTS",
                    source_contract_id="KXEPLGAME-26SEP20NEWCHE-BTTS",
                ),
            ]
        )
        trade.legs[0].source_event_id = None
        trade.legs[1].source_event_id = None
        _persist_open(ops, trade)
        kalshi = FakeKalshi(
            {"KXEPLGAME-26SEP20NEWCHE-BTTS": _kalshi_settled(result="yes")}
        )
        agent = PaperSettlementAgent(
            operations=ops,
            matchbook=None,
            kalshi=kalshi,
            provider_access=None,
            clock=lambda: NOW,
        )
        result = await agent.run_cycle(now=NOW)
        assert trade.trade_id in result.settled_trade_ids
        closed = ops.trades.get(trade.trade_id)
        assert closed.state is PaperTradeState.CLOSED
        assert closed.settlement_source == PAPER_AUTO_SETTLEMENT_SOURCE
        assert closed.settlement_reconciliation_status is SettlementReconciliationStatus.SETTLED
    finally:
        repository.close()
        ledger.close()


@pytest.mark.asyncio
async def test_legacy_matchbook_identity_recovered_from_catalogue(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "legacy-mb.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    try:
        trade = _filled_trade(
            extra_legs=[
                _leg(
                    venue=VenueName.MATCHBOOK,
                    outcome="yes",
                    source_event_id="newcastle-chelsea-2026-09-20",
                    source_market_id="2001",
                    source_runner_id="301",
                ),
                _leg(
                    venue=VenueName.MATCHBOOK,
                    outcome="no",
                    source_event_id="newcastle-chelsea-2026-09-20",
                    source_market_id="2001",
                    source_runner_id="302",
                ),
            ]
        )
        _persist_open(ops, trade)
        catalogue = SimpleNamespace(
            list_rows_for_event=lambda _event_id: [
                SimpleNamespace(
                    canonical_event_id="newcastle-chelsea-2026-09-20",
                    matchbook_event_id="1001",
                    matchbook_market_id="2001",
                    kalshi_event_ticker=None,
                    kalshi_market_tickers=[],
                )
            ]
        )
        matchbook = FakeMatchbook(_graded_matchbook_btts(winner_id=301), _graded_event(home=1, away=1))
        agent = PaperSettlementAgent(
            operations=ops,
            matchbook=matchbook,
            kalshi=None,
            provider_access=None,
            clock=lambda: NOW,
            catalogue=catalogue,
        )
        result = await agent.run_cycle(now=NOW)
        assert trade.trade_id in result.settled_trade_ids
        assert matchbook.market_calls == [("1001", "2001")]
        persisted = ops.trades.get(trade.trade_id)
        assert persisted.state is PaperTradeState.CLOSED
        assert persisted.legs[0].source_event_id == "1001"
    finally:
        repository.close()
        ledger.close()


@pytest.mark.asyncio
async def test_no_result_stays_active_with_visible_blocker_without_audit_spam(
    tmp_path: Path,
) -> None:
    ledger = SqlitePaperLedger(tmp_path / "blocked.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    try:
        trade = _filled_trade()
        _persist_open(ops, trade)
        matchbook = FakeMatchbook(
            {"id": 2001, "status": "open", "runners": []},
            {"id": 1001, "status": "open"},
        )
        kalshi = FakeKalshi(
            {"KXEPLGAME-26SEP20NEWCHE-BTTS": {"ticker": "x", "status": "open"}}
        )
        agent = PaperSettlementAgent(
            operations=ops,
            matchbook=matchbook,
            kalshi=kalshi,
            provider_access=None,
            clock=lambda: NOW,
        )
        first = await agent.run_cycle(now=NOW)
        assert first.settled_trade_ids == []
        open_trade = ops.trades.get(trade.trade_id)
        assert open_trade.state is PaperTradeState.OPEN
        assert open_trade.settlement_reconciliation_status is SettlementReconciliationStatus.BLOCKED
        assert open_trade.settlement_blocker == "incomplete_provider_result"
        assert open_trade.last_settlement_check_at == NOW
        blocked_events = [
            event
            for event in open_trade.audit
            if event.event_type is PaperTradeAuditEventType.SETTLEMENT_BLOCKED
        ]
        assert len(blocked_events) == 1
        later = NOW + timedelta(seconds=30)
        second = await agent.run_cycle(now=later)
        assert second.settled_trade_ids == []
        again = ops.trades.get(trade.trade_id)
        assert again.last_settlement_check_at == later
        blocked_events = [
            event
            for event in again.audit
            if event.event_type is PaperTradeAuditEventType.SETTLEMENT_BLOCKED
        ]
        assert len(blocked_events) == 1
        assert again.state is PaperTradeState.OPEN
    finally:
        repository.close()
        ledger.close()


def test_manual_1x2_offers_home_draw_away_even_when_draw_leg_omitted() -> None:
    trade = _filled_trade(
        family=MarketFamily.MATCH_RESULT,
        extra_legs=[
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="home",
                source_event_id="1001",
                source_market_id="11",
            ),
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="away",
                source_event_id="1001",
                source_market_id="11",
            ),
        ],
    )
    space = canonical_result_space(trade)
    assert [choice.value for choice in space.choices] == ["home", "draw", "away"]
    assert space.choices[0].label == "Newcastle win"
    assert space.choices[1].label == "Draw"
    assert space.choices[2].label == "Arsenal win"
    computation = compute_paper_settlement(trade, winning_outcome="draw")
    assert all(item.won is False for item in computation.legs)
    assert computation.realised_pnl_gbp == independent_realised_pnl_gbp(trade, "draw")


def test_manual_result_spaces_for_approved_families() -> None:
    btts = _filled_trade()
    assert [choice.value for choice in canonical_result_space(btts).choices] == ["yes", "no"]
    total = _filled_trade(family=MarketFamily.TOTAL_GOALS)
    total.line = Decimal("2.5")
    assert [choice.label for choice in canonical_result_space(total).choices] == [
        "Over 2.5",
        "Under 2.5",
    ]
    ftts = _filled_trade(family=MarketFamily.FIRST_TEAM_TO_SCORE)
    assert [choice.value for choice in canonical_result_space(ftts).choices] == [
        "home",
        "away",
        "no_goal",
    ]
    winner = _filled_trade(family=MarketFamily.GAME_WINNER)
    assert [choice.value for choice in canonical_result_space(winner).choices] == ["home", "away"]
    assert "draw" not in canonical_result_space(winner).values
    spread = _filled_trade(family=MarketFamily.POINT_SPREAD)
    spread.line = Decimal("-3.5")
    assert [choice.label for choice in canonical_result_space(spread).choices] == [
        "Newcastle covers -3.5",
        "Arsenal covers +3.5",
    ]
    assert spread.line == Decimal("-3.5")
    nfl_total = _filled_trade(family=MarketFamily.TOTAL_POINTS)
    nfl_total.line = Decimal("47.5")
    assert [choice.label for choice in canonical_result_space(nfl_total).choices] == [
        "Over 47.5",
        "Under 47.5",
    ]
    corners = _filled_trade(family=MarketFamily.CORNERS)
    assert canonical_result_space(corners).unsupported_reason == "unsupported_manual_result_family"


def test_point_spread_manual_labels_negate_away_side_home_line() -> None:
    """#429 stores the home signed line; away display is the negated Decimal."""

    extra_legs = [
        _leg(
            venue=VenueName.MATCHBOOK,
            outcome="home",
            source_event_id="1001",
            source_market_id="spread",
        ),
        _leg(
            venue=VenueName.MATCHBOOK,
            outcome="away",
            source_event_id="1001",
            source_market_id="spread",
        ),
    ]
    favorite = _filled_trade(family=MarketFamily.POINT_SPREAD, extra_legs=extra_legs)
    favorite.line = Decimal("-3.5")
    favorite_space = canonical_result_space(favorite)
    assert [choice.value for choice in favorite_space.choices] == ["home", "away"]
    assert [choice.label for choice in favorite_space.choices] == [
        "Newcastle covers -3.5",
        "Arsenal covers +3.5",
    ]
    assert favorite.line == Decimal("-3.5")
    home_win = compute_paper_settlement(favorite, winning_outcome="home")
    away_win = compute_paper_settlement(favorite, winning_outcome="away")
    assert all(item.won is (item.outcome == "home") for item in home_win.legs)
    assert all(item.won is (item.outcome == "away") for item in away_win.legs)
    assert home_win.realised_pnl_gbp == independent_realised_pnl_gbp(favorite, "home")
    assert away_win.realised_pnl_gbp == independent_realised_pnl_gbp(favorite, "away")
    assert favorite.line == Decimal("-3.5")

    dog = _filled_trade(family=MarketFamily.POINT_SPREAD, extra_legs=extra_legs)
    dog.line = Decimal("6.5")
    dog_space = canonical_result_space(dog)
    assert [choice.label for choice in dog_space.choices] == [
        "Newcastle covers +6.5",
        "Arsenal covers -6.5",
    ]
    assert dog.line == Decimal("6.5")
    dog_home = compute_paper_settlement(dog, winning_outcome="home")
    dog_away = compute_paper_settlement(dog, winning_outcome="away")
    assert all(item.won is (item.outcome == "home") for item in dog_home.legs)
    assert all(item.won is (item.outcome == "away") for item in dog_away.legs)
    assert dog.line == Decimal("6.5")
    assert dog_home.realised_pnl_gbp == independent_realised_pnl_gbp(dog, "home")
    assert dog_away.realised_pnl_gbp == independent_realised_pnl_gbp(dog, "away")


def test_back_buy_normalization_is_settlement_correct() -> None:
    trade = _filled_trade(
        family=MarketFamily.MATCH_RESULT,
        extra_legs=[
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="home",
                source_event_id="1001",
                source_market_id="11",
            ),
            _leg(
                venue=VenueName.KALSHI,
                outcome="home",
                source_event_id="KX",
                source_market_id="KX-HOME",
                source_contract_id="KX-HOME",
            ),
        ],
    )
    assert trade.legs[0].opening_action is MarketAction.BACK
    assert trade.legs[1].opening_action is MarketAction.BUY
    assert trade.legs[0].canonical_state == "home"
    won = compute_paper_settlement(trade, winning_outcome="home")
    assert all(item.won for item in won.legs)
    lost = compute_paper_settlement(trade, winning_outcome="away")
    assert all(item.won is False for item in lost.legs)
    lay = trade.model_copy(
        update={
            "legs": [
                trade.legs[0].model_copy(update={"opening_action": MarketAction.LAY}),
                trade.legs[1],
            ]
        }
    )
    with pytest.raises(PaperSettlementError, match="opening_lay_sell_not_supported"):
        compute_paper_settlement(lay, winning_outcome="home")


def test_manual_settle_uses_stored_odds_treasury_and_is_idempotent(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "manual.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    try:
        trade = _filled_trade(family=MarketFamily.MATCH_RESULT)
        trade.legs = [
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="home",
                source_event_id="1001",
                source_market_id="11",
                stake=Decimal("10"),
            ),
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="away",
                source_event_id="1001",
                source_market_id="11",
                stake=Decimal("10"),
            ),
        ]
        for leg in trade.legs:
            leg.opening_action = MarketAction.BACK
            leg.canonical_state = leg.outcome
            leg.filled_odds = Decimal("3.00")
            leg.displayed_odds = Decimal("9.99")
        trade.venue_costs = [
            profit_commission_cost(VenueName.MATCHBOOK, Decimal("0.02"), captured_at=NOW)
        ]
        _persist_open(ops, trade)
        options = ops.settlement_options(trade.trade_id)
        assert [choice.value for choice in options.choices] == ["home", "draw", "away"]
        persisted = ops.trades.get(trade.trade_id)
        expected = independent_realised_pnl_gbp(persisted, "home")
        mutated = persisted.model_copy(deep=True)
        mutated.legs[0].filled_odds = Decimal("9.99")
        wrong = independent_realised_pnl_gbp(mutated, "home")
        assert expected != wrong
        settled = ops.settle_manual_result(
            trade.trade_id,
            PaperManualSettlementRequest(winning_outcome="home", operator_note="FT 1-0"),
        )
        assert settled.state is PaperTradeState.CLOSED
        assert settled.settlement_source == PAPER_MANUAL_SETTLEMENT_SOURCE
        assert settled.settlement_source_id == manual_settlement_source_id(trade.trade_id)
        assert settled.provenance is DataProvenance.LIVE_PAPER
        assert settled.realised_pnl_gbp == expected
        assert settled.realised_pnl_gbp != wrong
        assert settled.capital_locked_gbp == Decimal("0")
        assert settled.trade_id not in {item.trade_id for item in ops.list_active_trades()}
        assert exposures_from_trades(ops.list_active_trades()) == []
        postings = ops.journal.postings(opportunity_id=trade.opportunity_id)
        assert gbp_is_balanced(postings)
        settle_entries = [
            entry
            for entry in ops.journal.list_entries(opportunity_id=trade.opportunity_id)
            if entry.source == "paper_settlement"
        ]
        assert len(settle_entries) == 1
        assert settle_entries[0].provenance is DataProvenance.LIVE_PAPER
        snapshot = ledger.treasury.snapshot()
        for pool in snapshot.pools:
            assert pool.locked_capital == 0
        again = ops.settle_manual_result(
            trade.trade_id,
            PaperManualSettlementRequest(winning_outcome="home"),
        )
        assert again.state is PaperTradeState.CLOSED
        settle_entries = [
            entry
            for entry in ops.journal.list_entries(opportunity_id=trade.opportunity_id)
            if entry.source == "paper_settlement"
        ]
        assert len(settle_entries) == 1
        with pytest.raises(PaperOperationsError, match="conflicting_settlement"):
            ops.settle_manual_result(
                trade.trade_id,
                PaperManualSettlementRequest(winning_outcome="draw"),
            )
        with pytest.raises(PaperOperationsError, match="unknown_trade"):
            ops.settle_manual_result(
                "missing",
                PaperManualSettlementRequest(winning_outcome="home"),
            )
    finally:
        repository.close()
        ledger.close()


def test_manual_nfl_game_winner_rejects_tie(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "nfl.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    try:
        trade = _filled_trade(family=MarketFamily.GAME_WINNER)
        trade.legs = [
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="home",
                source_event_id="9",
                source_market_id="90",
            )
        ]
        trade.legs[0].opening_action = MarketAction.BACK
        trade.legs[0].canonical_state = "home"
        trade.venue_costs = [
            profit_commission_cost(VenueName.MATCHBOOK, Decimal("0.02"), captured_at=NOW)
        ]
        _persist_open(ops, trade)
        with pytest.raises(PaperOperationsError, match="canonical_outcome_not_determined"):
            ops.settle_manual_result(
                trade.trade_id,
                PaperManualSettlementRequest(winning_outcome="draw"),
            )
        still = ops.trades.get(trade.trade_id)
        assert still.state is PaperTradeState.OPEN
        closed = ops.settle_manual_result(
            trade.trade_id,
            PaperManualSettlementRequest(winning_outcome="home"),
        )
        assert closed.state is PaperTradeState.CLOSED
        assert closed.settlement_outcome == "home"
    finally:
        repository.close()
        ledger.close()


def test_developer_settle_still_rejects_unknown_outcomes() -> None:
    trade = _filled_trade()
    with pytest.raises(PaperSettlementError, match="settlement_outcome_not_on_trade"):
        compute_paper_settlement(trade, winning_outcome="not-on-this-market")
    with pytest.raises(PaperSettlementError, match="settlement_outcome_not_on_trade"):
        compute_paper_settlement(trade, winning_outcome="__conflict__")
