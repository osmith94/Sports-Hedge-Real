"""Issue #401: PAPER auto-settlement from authoritative provider results.

Never infers completion from elapsed kickoff time. Reuses settle() / 8E.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from sports_hedge.accounting.paper_journal import DataProvenance, gbp_is_balanced
from sports_hedge.application.paper_settlement_agent import PaperSettlementAgent
from sports_hedge.arbitrage.allocation.adapters import exposures_from_trades
from sports_hedge.arbitrage.allocation.engine import allocate
from sports_hedge.arbitrage.allocation.models import BankrollAllocationPolicy
from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.paper.result_resolution import (
    PAPER_AUTO_SETTLEMENT_SOURCE,
    resolve_paper_trade_settlement,
)
from sports_hedge.paper.trades import (
    PaperLegFillKind,
    PaperSettlementRequest,
    PaperTrade,
    PaperTradeLeg,
    PaperTradeState,
)
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from test_paper_trade_lifecycle import (
    _ops,
    assert_settlement_arithmetic,
    independent_realised_pnl_gbp,
)
from test_step8c_bankroll_allocator import (
    _assert_not_open_count_rejection,
    _demo_request,
    _fill_high,
)

NOW = datetime(2026, 9, 20, 18, 0, tzinfo=UTC)
KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)


def _leg(
    *,
    venue: VenueName,
    outcome: str,
    source_event_id: str,
    source_market_id: str,
    source_runner_id: str | None = None,
    source_contract_id: str | None = None,
    stake: Decimal = Decimal("10"),
) -> PaperTradeLeg:
    return PaperTradeLeg(
        venue=venue,
        outcome=outcome,
        currency="GBP" if venue is VenueName.MATCHBOOK else "USD",
        requested_stake=stake,
        filled_stake=stake,
        displayed_odds=Decimal("2.0"),
        filled_odds=Decimal("2.0"),
        source_market_id=source_market_id,
        source_event_id=source_event_id,
        source_runner_id=source_runner_id,
        source_contract_id=source_contract_id,
        fill_id=f"fill-{venue.value}-{outcome}",
        fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
    )


def _trade(
    *,
    family: MarketFamily = MarketFamily.BOTH_TEAMS_TO_SCORE,
    settlement_key: str = "regulation_time|full_time||False|False|False||",
    extra_legs: list[PaperTradeLeg] | None = None,
) -> PaperTrade:
    legs = extra_legs or [
        _leg(
            venue=VenueName.MATCHBOOK,
            outcome="yes",
            source_event_id="1001",
            source_market_id="2001",
            source_runner_id="301",
        ),
        _leg(
            venue=VenueName.MATCHBOOK,
            outcome="no",
            source_event_id="1001",
            source_market_id="2001",
            source_runner_id="302",
        ),
        _leg(
            venue=VenueName.KALSHI,
            outcome="yes",
            source_event_id="KXEPLGAME-26SEP20NEWCHE",
            source_market_id="KXEPLGAME-26SEP20NEWCHE-BTTS",
            source_contract_id="KXEPLGAME-26SEP20NEWCHE-BTTS",
        ),
        _leg(
            venue=VenueName.KALSHI,
            outcome="no",
            source_event_id="KXEPLGAME-26SEP20NEWCHE",
            source_market_id="KXEPLGAME-26SEP20NEWCHE-BTTS",
            source_contract_id="KXEPLGAME-26SEP20NEWCHE-BTTS",
        ),
    ]
    return PaperTrade(
        trade_id="trade-1",
        opportunity_id="opp-1",
        canonical_event_id="newcastle-chelsea-2026-09-20",
        canonical_market_id="btts-ft",
        settlement_key=settlement_key,
        market_family=family,
        period=FootballPeriod.FULL_TIME,
        state=PaperTradeState.OPEN,
        opened_at=NOW,
        last_updated_at=NOW,
        legs=legs,
    )


def _graded_matchbook_btts(*, winner_id: int = 301, status: str = "graded") -> dict:
    return {
        "id": 2001,
        "name": "Both Teams To Score",
        "status": status,
        "runners": [
            {"id": 301, "name": "Yes", "status": "WINNER" if winner_id == 301 else "LOSER"},
            {"id": 302, "name": "No", "status": "WINNER" if winner_id == 302 else "LOSER"},
        ],
    }


def _graded_event(*, home: int = 2, away: int = 1, status: str = "graded") -> dict:
    return {
        "id": 1001,
        "name": "Newcastle United vs Chelsea",
        "status": status,
        "start": KICKOFF.isoformat(),
        "home-score": home,
        "away-score": away,
    }


def _kalshi_settled(*, result: str = "yes", status: str = "settled") -> dict:
    return {
        "ticker": "KXEPLGAME-26SEP20NEWCHE-BTTS",
        "status": status,
        "result": result,
    }


class FakeMatchbook:
    def __init__(self, market: dict, event: dict) -> None:
        self.market = market
        self.event = event
        self.market_calls: list[tuple[str, str]] = []
        self.event_calls: list[str] = []

    async def get_market(self, event_id, market_id, **filters):
        del filters
        self.market_calls.append((str(event_id), str(market_id)))
        return dict(self.market)

    async def get_event(self, event_id, **filters):
        del filters
        self.event_calls.append(str(event_id))
        return dict(self.event)


class FakeKalshi:
    def __init__(self, markets: dict[str, dict]) -> None:
        self.markets = markets
        self.calls: list[str] = []

    async def get_market(self, ticker: str, *, use_cache: bool = True):
        del use_cache
        self.calls.append(ticker)
        return dict(self.markets[ticker])


def test_btts_scores_and_graded_venues_resolve_yes() -> None:
    resolved = resolve_paper_trade_settlement(
        _trade(),
        matchbook_market=_graded_matchbook_btts(winner_id=301),
        matchbook_event=_graded_event(home=2, away=1),
        kalshi_markets={"KXEPLGAME-26SEP20NEWCHE-BTTS": _kalshi_settled(result="yes")},
    )
    assert resolved.is_ready
    assert resolved.winning_outcome == "yes"
    assert resolved.evidence["kickoff_not_used"] is True


def test_1x2_home_win_from_authoritative_scores() -> None:
    trade = _trade(
        family=MarketFamily.MATCH_RESULT,
        extra_legs=[
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="home",
                source_event_id="1001",
                source_market_id="11",
                source_runner_id="1",
            ),
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="draw",
                source_event_id="1001",
                source_market_id="11",
                source_runner_id="2",
            ),
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="away",
                source_event_id="1001",
                source_market_id="11",
                source_runner_id="3",
            ),
        ],
    )
    resolved = resolve_paper_trade_settlement(
        trade,
        matchbook_market={"id": 11, "status": "graded", "runners": []},
        matchbook_event=_graded_event(home=2, away=1, status="graded"),
    )
    assert resolved.winning_outcome == "home"


def test_half_line_totals_over_from_scores() -> None:
    trade = _trade(
        family=MarketFamily.TOTAL_GOALS,
        settlement_key="regulation_time|full_time|2.5|False|False|False||",
        extra_legs=[
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="over",
                source_event_id="1001",
                source_market_id="55",
                source_runner_id="8",
            ),
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="under",
                source_event_id="1001",
                source_market_id="55",
                source_runner_id="9",
            ),
        ],
    )
    resolved = resolve_paper_trade_settlement(
        trade,
        matchbook_market={"id": 55, "status": "graded", "runners": []},
        matchbook_event=_graded_event(home=2, away=1, status="finished"),
    )
    assert resolved.winning_outcome == "over"


def test_integer_total_line_fails_closed() -> None:
    trade = _trade(
        family=MarketFamily.TOTAL_GOALS,
        settlement_key="regulation_time|full_time|2|True|False|False||",
        extra_legs=[
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="over",
                source_event_id="1001",
                source_market_id="55",
                source_runner_id="8",
            ),
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="under",
                source_event_id="1001",
                source_market_id="55",
                source_runner_id="9",
            ),
        ],
    )
    resolved = resolve_paper_trade_settlement(
        trade,
        matchbook_event=_graded_event(home=3, away=0, status="graded"),
    )
    assert resolved.winning_outcome is None
    assert resolved.blocker == "unsupported_total_line"


def test_ftts_both_teams_scored_fails_closed_without_first_goal() -> None:
    trade = _trade(
        family=MarketFamily.FIRST_TEAM_TO_SCORE,
        extra_legs=[
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="home",
                source_event_id="1001",
                source_market_id="77",
                source_runner_id="1",
            ),
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="away",
                source_event_id="1001",
                source_market_id="77",
                source_runner_id="2",
            ),
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="no_goal",
                source_event_id="1001",
                source_market_id="77",
                source_runner_id="3",
            ),
        ],
    )
    resolved = resolve_paper_trade_settlement(
        trade,
        matchbook_market={"id": 77, "status": "graded", "runners": []},
        matchbook_event=_graded_event(home=2, away=1, status="graded"),
    )
    assert resolved.blocker == "ftts_requires_first_goal_evidence"


def test_ftts_home_only_scorer_is_home() -> None:
    trade = _trade(
        family=MarketFamily.FIRST_TEAM_TO_SCORE,
        extra_legs=[
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="home",
                source_event_id="1001",
                source_market_id="77",
                source_runner_id="1",
            ),
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="away",
                source_event_id="1001",
                source_market_id="77",
                source_runner_id="2",
            ),
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="no_goal",
                source_event_id="1001",
                source_market_id="77",
                source_runner_id="3",
            ),
        ],
    )
    resolved = resolve_paper_trade_settlement(
        trade,
        matchbook_market={
            "id": 77,
            "status": "graded",
            "runners": [
                {"id": 1, "name": "Newcastle", "status": "WINNER"},
                {"id": 2, "name": "Chelsea", "status": "LOSER"},
                {"id": 3, "name": "No Goal", "status": "LOSER"},
            ],
        },
        matchbook_event=_graded_event(home=2, away=0, status="graded"),
    )
    assert resolved.winning_outcome == "home"


def test_elapsed_kickoff_with_open_market_does_not_settle() -> None:
    resolved = resolve_paper_trade_settlement(
        _trade(),
        matchbook_market={"id": 2001, "status": "open", "runners": []},
        matchbook_event={
            "id": 1001,
            "status": "open",
            "start": (NOW - timedelta(hours=5)).isoformat(),
            "in-running-flag": True,
        },
        kalshi_markets={"KXEPLGAME-26SEP20NEWCHE-BTTS": {"ticker": "x", "status": "open"}},
    )
    assert resolved.is_ready is False
    assert resolved.blocker == "incomplete_provider_result"


def test_postponed_event_fails_closed() -> None:
    resolved = resolve_paper_trade_settlement(
        _trade(),
        matchbook_event={"id": 1001, "status": "postponed", "home-score": 0, "away-score": 0},
        matchbook_market=_graded_matchbook_btts(),
    )
    assert resolved.blocker == "provider_status_postponed"


def test_conflicting_venue_results_fail_closed() -> None:
    resolved = resolve_paper_trade_settlement(
        _trade(),
        matchbook_market=_graded_matchbook_btts(winner_id=301),
        matchbook_event=_graded_event(home=2, away=1),
        kalshi_markets={"KXEPLGAME-26SEP20NEWCHE-BTTS": _kalshi_settled(result="no")},
    )
    assert resolved.blocker == "conflicting_provider_results"


def test_void_matchbook_runner_fails_closed() -> None:
    resolved = resolve_paper_trade_settlement(
        _trade(),
        matchbook_market={
            "id": 2001,
            "status": "graded",
            "runners": [
                {"id": 301, "name": "Yes", "status": "VOID"},
                {"id": 302, "name": "No", "status": "VOID"},
            ],
        },
        matchbook_event=_graded_event(status="abandoned"),
    )
    assert resolved.blocker in {"void_matchbook_runner", "provider_status_abandoned"}


def test_missing_native_ids_fail_closed() -> None:
    trade = _trade(
        extra_legs=[
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="yes",
                source_event_id="newcastle-chelsea-2026-09-20",
                source_market_id="2001",
                source_runner_id="301",
            )
        ]
    )
    trade.canonical_event_id = "newcastle-chelsea-2026-09-20"
    resolved = resolve_paper_trade_settlement(trade, matchbook_event=_graded_event())
    assert resolved.blocker == "missing_durable_provider_identity"


@pytest.mark.asyncio
async def test_finished_btts_trade_closes_with_same_pnl_as_explicit_settle(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "paper.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        expected = independent_realised_pnl_gbp(trade, "yes")
        assert_settlement_arithmetic(trade, "yes", expected)
        matchbook = FakeMatchbook(_graded_matchbook_btts(winner_id=301), _graded_event(home=1, away=1))
        kalshi_ticker = next(
            leg.source_contract_id or leg.source_market_id
            for leg in trade.legs
            if leg.venue is VenueName.KALSHI
        )
        kalshi = FakeKalshi({str(kalshi_ticker): _kalshi_settled(result="yes")})
        agent = PaperSettlementAgent(
            operations=ops,
            matchbook=matchbook,
            kalshi=kalshi,
            provider_access=None,
            clock=lambda: NOW,
        )
        first = await agent.run_cycle(now=NOW)
        assert trade.trade_id in first.settled_trade_ids
        closed = ops.trades.get(trade.trade_id)
        assert closed is not None
        assert closed.state is PaperTradeState.CLOSED
        assert closed.settlement_source == PAPER_AUTO_SETTLEMENT_SOURCE
        assert closed.settlement_outcome == "yes"
        assert closed.realised_pnl_gbp == expected
        assert closed.capital_locked_gbp == Decimal("0")
        assert closed.trade_id not in {item.trade_id for item in ops.list_active_trades()}
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
            if pool.locked_capital > 0:
                raise AssertionError(f"treasury still locked {pool.venue} {pool.native_currency}")
        exposures = exposures_from_trades(ops.list_active_trades())
        assert exposures == []
        policy = BankrollAllocationPolicy(max_concurrent_open_opportunities=1)
        allowed = allocate(_demo_request(open_positions=exposures, policy=policy))
        assert allowed.accepted is True
        second = await agent.run_cycle(now=NOW)
        assert second.settled_trade_ids == []
        settle_entries = [
            entry
            for entry in ops.journal.list_entries(opportunity_id=trade.opportunity_id)
            if entry.source == "paper_settlement"
        ]
        assert len(settle_entries) == 1
        again = ops.settle(
            trade.trade_id,
            PaperSettlementRequest(
                winning_outcome="yes",
                source=PAPER_AUTO_SETTLEMENT_SOURCE,
                source_id=closed.settlement_source_id or "",
                provenance=DataProvenance.LIVE_PAPER,
            ),
        )
        assert again.state is PaperTradeState.CLOSED
        assert again.realised_pnl_gbp == expected
    finally:
        repository.close()
        ledger.close()


@pytest.mark.asyncio
async def test_unfinished_and_ambiguous_evidence_leaves_trade_open(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "open.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        matchbook = FakeMatchbook(
            {"id": 2001, "status": "open", "runners": []},
            {"id": 1001, "status": "open", "start": KICKOFF.isoformat()},
        )
        kalshi_ticker = next(
            leg.source_contract_id or leg.source_market_id
            for leg in trade.legs
            if leg.venue is VenueName.KALSHI
        )
        kalshi = FakeKalshi({str(kalshi_ticker): {"ticker": kalshi_ticker, "status": "open"}})
        agent = PaperSettlementAgent(
            operations=ops,
            matchbook=matchbook,
            kalshi=kalshi,
            provider_access=None,
            clock=lambda: NOW,
        )
        result = await agent.run_cycle(now=NOW)
        assert result.settled_trade_ids == []
        assert any(item.blocker == "incomplete_provider_result" for item in result.blocked)
        assert ops.list_active_trades()[0].state is PaperTradeState.OPEN
        matchbook.market = _graded_matchbook_btts(winner_id=301)
        matchbook.event = _graded_event(home=2, away=1)
        kalshi.markets[str(kalshi_ticker)] = _kalshi_settled(result="no")
        conflicted = await agent.run_cycle(now=NOW)
        assert conflicted.settled_trade_ids == []
        assert any(item.blocker == "conflicting_provider_results" for item in conflicted.blocked)
        assert ops.list_active_trades()[0].state is PaperTradeState.OPEN
        assert ledger.treasury.snapshot().pool(VenueName.MATCHBOOK, "GBP").locked_capital > 0
    finally:
        repository.close()
        ledger.close()


def test_closed_trade_no_longer_counts_toward_open_positions(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "cap.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=True)
    try:
        trade = ops.list_active_trades()[0]
        before = exposures_from_trades(ops.list_active_trades())
        assert len(before) == 1
        ops.settle(
            trade.trade_id,
            PaperSettlementRequest(
                winning_outcome="yes",
                source="fixture_test",
                source_id="manual",
                provenance=DataProvenance.FIXTURE_DEMO,
            ),
        )
        after = exposures_from_trades(ops.list_active_trades())
        assert after == []
        # #403 retired the open-count cap. Closed trades still drop from
        # open-position capital accounting (#404); leftover count kwargs
        # must not restore a rejection while the trade is open.
        leftover = BankrollAllocationPolicy(max_concurrent_open_opportunities=1)
        still_open = allocate(
            _demo_request(open_positions=before, policy=leftover, fill=_fill_high())
        )
        assert still_open.accepted is True
        _assert_not_open_count_rejection(still_open)
        accepted = allocate(
            _demo_request(open_positions=after, policy=leftover, fill=_fill_high())
        )
        assert accepted.accepted is True
        _assert_not_open_count_rejection(accepted)
    finally:
        repository.close()
        ledger.close()
