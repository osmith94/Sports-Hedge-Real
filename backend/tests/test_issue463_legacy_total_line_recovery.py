"""Issue #463: recover a legacy PAPER total line from exact catalogue identity.

Do not weaken unsupported_total_line. Never infer the line from scores, labels,
or elapsed time. Manual GET and POST must share the same recovery path.
"""

from __future__ import annotations

import inspect
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest

from sports_hedge.accounting.paper_journal import DataProvenance, gbp_is_balanced
from sports_hedge.application.paper_operations import PaperOperationsError, PaperOperationsService
from sports_hedge.application.paper_settlement_agent import PaperSettlementAgent
from sports_hedge.arbitrage.allocation.adapters import exposures_from_trades
from sports_hedge.domain.football import MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.cost import MarketAction
from sports_hedge.paper.canonical_results import (
    PAPER_MANUAL_SETTLEMENT_SOURCE,
    UNSUPPORTED_TOTAL_LINE,
    canonical_result_space,
    manual_settlement_source_id,
)
from sports_hedge.paper.provider_identity import (
    LEGACY_MARKET_LINE_RECOVERED_DETAIL,
    recover_legacy_market_line,
    recover_persisted_catalogue_identity,
    recover_persisted_provider_identity,
)
from sports_hedge.paper.trades import (
    PaperManualSettlementRequest,
    PaperTrade,
    PaperTradeAuditEventType,
    PaperTradeState,
)
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from test_paper_auto_settlement import NOW, _leg
from test_paper_settlement_hotfix import _filled_trade, _persist_open
from test_paper_trade_lifecycle import _ops, independent_realised_pnl_gbp


def _row(**overrides: object) -> SimpleNamespace:
    payload: dict[str, object] = {
        "catalogue_row_id": "amc-total-2-5",
        "canonical_event_id": "newcastle-chelsea-2026-09-20",
        "matchbook_event_id": "1001",
        "matchbook_market_id": "316030",
        "kalshi_event_ticker": "KXEPLGAME-26SEP20NEWCHE",
        "kalshi_market_tickers": [
            "KXEPLGAME-26SEP20NEWCHE-T2",
            "KXEPLGAME-26SEP20NEWCHE-T2U",
        ],
        "family": "total_goals",
        "period": "full_time",
        "line": "2.5",
        "register_canonical_key": "TOTAL_GOALS_FT:2.5",
        "polymarket_market_id": None,
        "polymarket_condition_id": None,
        "polymarket_token_ids": [],
        "polymarket_clob_token_ids": [],
    }
    payload.update(overrides)
    return SimpleNamespace(**payload)


def _catalogue(*rows: SimpleNamespace) -> SimpleNamespace:
    return SimpleNamespace(list_rows_for_event=lambda _event_id: list(rows))


def _legacy_total_trade(**overrides: object) -> PaperTrade:
    trade = _filled_trade(
        family=MarketFamily.TOTAL_GOALS,
        extra_legs=[
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="over",
                source_event_id="1001",
                source_market_id="316030",
                source_runner_id="401",
            ),
            _leg(
                venue=VenueName.KALSHI,
                outcome="under",
                source_event_id="KXEPLGAME-26SEP20NEWCHE",
                source_market_id="KXEPLGAME-26SEP20NEWCHE-T2U",
                source_contract_id="KXEPLGAME-26SEP20NEWCHE-T2U",
            ),
        ],
    )
    trade.line = None
    trade.market_label = "Total Goals n.a."
    for key, value in overrides.items():
        setattr(trade, key, value)
    return trade


def _recovery_events(trade: PaperTrade) -> list:
    return [
        event
        for event in trade.audit
        if event.event_type is PaperTradeAuditEventType.LEGACY_MARKET_LINE_RECOVERED
    ]


def test_canonical_result_space_still_fail_closed_without_recovered_line() -> None:
    trade = _legacy_total_trade()
    trade.market_label = "Total Goals 2.5 Over/Under"
    space = canonical_result_space(trade)
    assert space.unsupported_reason == UNSUPPORTED_TOTAL_LINE
    assert space.choices == []


def test_exact_matchbook_kalshi_identity_recovers_half_line() -> None:
    trade = _legacy_total_trade()
    recovered, changed = recover_legacy_market_line(
        trade, catalogue_rows=[_row()], now=NOW
    )
    assert changed is True
    assert recovered.line == Decimal("2.5")
    events = _recovery_events(recovered)
    assert len(events) == 1
    assert LEGACY_MARKET_LINE_RECOVERED_DETAIL in (events[0].detail or "")
    assert "catalogue_row_id=amc-total-2-5" in (events[0].detail or "")
    assert "register_canonical_key=TOTAL_GOALS_FT:2.5" in (events[0].detail or "")
    space = canonical_result_space(recovered)
    assert space.unsupported_reason is None
    assert [choice.label for choice in space.choices] == ["Over 2.5", "Under 2.5"]


def test_persisted_line_is_preferred_over_catalogue() -> None:
    trade = _legacy_total_trade()
    trade.line = Decimal("3.5")
    recovered, changed = recover_legacy_market_line(trade, catalogue_rows=[_row()])
    assert changed is False
    assert recovered.line == Decimal("3.5")
    assert _recovery_events(recovered) == []


def test_ambiguous_two_matching_rows_fail_closed() -> None:
    trade = _legacy_total_trade()
    rows = [
        _row(),
        _row(
            catalogue_row_id="amc-total-3-5",
            matchbook_market_id="316031",
            kalshi_market_tickers=["KXEPLGAME-26SEP20NEWCHE-T2U"],
            line="3.5",
            register_canonical_key="TOTAL_GOALS_FT:3.5",
        ),
    ]
    recovered, changed = recover_legacy_market_line(trade, catalogue_rows=rows)
    assert changed is False
    assert recovered.line is None
    assert canonical_result_space(recovered).unsupported_reason == UNSUPPORTED_TOTAL_LINE


def test_integer_push_line_fails_closed() -> None:
    trade = _legacy_total_trade()
    recovered, changed = recover_legacy_market_line(
        trade,
        catalogue_rows=[
            _row(line="2", register_canonical_key="TOTAL_GOALS_FT:2"),
        ],
    )
    assert changed is False
    assert recovered.line is None
    assert canonical_result_space(recovered).unsupported_reason == UNSUPPORTED_TOTAL_LINE


def test_no_matching_catalogue_row_fails_closed() -> None:
    trade = _legacy_total_trade()
    recovered, changed = recover_legacy_market_line(
        trade,
        catalogue_rows=[
            _row(matchbook_market_id="999999", kalshi_market_tickers=["OTHER-T2"]),
        ],
    )
    assert changed is False
    assert recovered.line is None


def test_family_mismatch_and_near_id_do_not_match() -> None:
    trade = _legacy_total_trade()
    btts, btts_changed = recover_legacy_market_line(
        trade,
        catalogue_rows=[
            _row(
                family="both_teams_to_score",
                line=None,
                register_canonical_key="BTTS_FT",
            )
        ],
    )
    assert btts_changed is False
    near, near_changed = recover_legacy_market_line(
        trade,
        catalogue_rows=[_row(matchbook_market_id="31603", kalshi_market_tickers=[])],
    )
    assert near_changed is False
    assert btts.line is None
    assert near.line is None


def test_event_ticker_alone_is_not_a_unique_market_identity() -> None:
    trade = _legacy_total_trade()
    trade.legs[0].source_market_id = "unknown"
    trade.legs[1].source_market_id = "unknown"
    trade.legs[1].source_contract_id = None
    recovered, changed = recover_legacy_market_line(trade, catalogue_rows=[_row()])
    assert changed is False
    assert recovered.line is None


def test_nfl_point_spread_and_total_points_reuse_the_same_path() -> None:
    spread = _filled_trade(
        family=MarketFamily.POINT_SPREAD,
        extra_legs=[
            _leg(
                venue=VenueName.MATCHBOOK,
                outcome="home",
                source_event_id="9",
                source_market_id="spread-mkt",
            )
        ],
    )
    spread.line = None
    recovered_spread, spread_changed = recover_legacy_market_line(
        spread,
        catalogue_rows=[
            _row(
                matchbook_market_id="spread-mkt",
                kalshi_market_tickers=[],
                family="point_spread",
                line="-3.5",
                register_canonical_key="NFL_POINT_SPREAD_FT:-3.5",
            )
        ],
        now=NOW,
    )
    assert spread_changed is True
    assert recovered_spread.line == Decimal("-3.5")
    nfl_total = _filled_trade(
        family=MarketFamily.TOTAL_POINTS,
        extra_legs=[
            _leg(
                venue=VenueName.KALSHI,
                outcome="over",
                source_event_id="KX",
                source_market_id="KX-TOTAL-47",
                source_contract_id="KX-TOTAL-47",
            )
        ],
    )
    nfl_total.line = None
    recovered_total, total_changed = recover_legacy_market_line(
        nfl_total,
        catalogue_rows=[
            _row(
                matchbook_market_id=None,
                kalshi_market_tickers=["KX-TOTAL-47"],
                family="total_points",
                line="47.5",
                register_canonical_key="NFL_TOTAL_POINTS_FT:47.5",
            )
        ],
        now=NOW,
    )
    assert total_changed is True
    assert recovered_total.line == Decimal("47.5")


def test_polymarket_native_ids_are_exact_matches() -> None:
    trade = _filled_trade(
        family=MarketFamily.TOTAL_GOALS,
        extra_legs=[
            _leg(
                venue=VenueName.POLYMARKET,
                outcome="over",
                source_event_id="pm-event",
                source_market_id="pm-market-2-5",
                source_contract_id="pm-condition-2-5",
            )
        ],
    )
    trade.line = None
    recovered, changed = recover_legacy_market_line(
        trade,
        catalogue_rows=[
            _row(
                matchbook_market_id=None,
                kalshi_market_tickers=[],
                polymarket_market_id="pm-market-2-5",
                polymarket_condition_id="pm-condition-2-5",
            )
        ],
        now=NOW,
    )
    assert changed is True
    assert recovered.line == Decimal("2.5")


def test_recovery_reuses_provider_identity_helper() -> None:
    assert recover_persisted_catalogue_identity is not recover_persisted_provider_identity
    src = inspect.getsource(recover_persisted_catalogue_identity)
    assert "recover_persisted_provider_identity" in src
    assert "recover_legacy_market_line" in src


def test_settlement_options_recover_and_persist_over_under_line(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "options.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    try:
        trade = _legacy_total_trade()
        _persist_open(ops, trade)
        ops.catalogue = _catalogue(_row())
        options = ops.settlement_options(trade.trade_id)
        assert options.unsupported_reason is None
        assert options.line == Decimal("2.5")
        assert [choice.label for choice in options.choices] == ["Over 2.5", "Under 2.5"]
        persisted = ops.trades.get(trade.trade_id)
        assert persisted.line == Decimal("2.5")
        events = _recovery_events(persisted)
        assert len(events) == 1
        again = ops.settlement_options(trade.trade_id)
        assert again.line == Decimal("2.5")
        assert len(_recovery_events(ops.trades.get(trade.trade_id))) == 1
    finally:
        repository.close()
        ledger.close()


def test_manual_settle_recovers_without_prior_get(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "post-only.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    try:
        trade = _legacy_total_trade()
        _persist_open(ops, trade)
        ops.catalogue = _catalogue(_row())
        with pytest.raises(PaperOperationsError, match=UNSUPPORTED_TOTAL_LINE):
            PaperOperationsService(
                watchlist=ops.watchlist,
                alerts=ops.alerts,
                settings=ops.settings,
                ledger=ledger,
            ).settle_manual_result(
                trade.trade_id,
                PaperManualSettlementRequest(winning_outcome="over"),
            )
        settled = ops.settle_manual_result(
            trade.trade_id,
            PaperManualSettlementRequest(winning_outcome="over"),
        )
        assert settled.state is PaperTradeState.CLOSED
        assert settled.line == Decimal("2.5")
        assert settled.settlement_outcome == "over"
        assert settled.settlement_source == PAPER_MANUAL_SETTLEMENT_SOURCE
        assert settled.settlement_source_id == manual_settlement_source_id(trade.trade_id)
        assert _recovery_events(settled)
    finally:
        repository.close()
        ledger.close()


def test_ambiguous_integer_and_missing_rows_keep_trade_open(tmp_path: Path) -> None:
    ledger = SqlitePaperLedger(tmp_path / "fail-closed.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    try:
        cases = [
            (
                _catalogue(
                    _row(),
                    _row(
                        catalogue_row_id="amc-3-5",
                        matchbook_market_id="316031",
                        kalshi_market_tickers=["KXEPLGAME-26SEP20NEWCHE-T2U"],
                        line="3.5",
                        register_canonical_key="TOTAL_GOALS_FT:3.5",
                    ),
                ),
                "ambiguous",
            ),
            (_catalogue(_row(line="2", register_canonical_key="TOTAL_GOALS_FT:2")), "integer"),
            (_catalogue(_row(matchbook_market_id="nope", kalshi_market_tickers=[])), "missing"),
        ]
        for index, (catalogue, _label) in enumerate(cases, start=1):
            trade = _legacy_total_trade(trade_id=f"trade-{index}", opportunity_id=f"opp-{index}")
            for leg in trade.legs:
                leg.fill_id = f"{trade.trade_id}-{leg.venue.value}-{leg.outcome}"
            _persist_open(ops, trade)
            ops.catalogue = catalogue
            options = ops.settlement_options(trade.trade_id)
            assert options.unsupported_reason == UNSUPPORTED_TOTAL_LINE
            assert options.choices == []
            with pytest.raises(PaperOperationsError, match=UNSUPPORTED_TOTAL_LINE):
                ops.settle_manual_result(
                    trade.trade_id,
                    PaperManualSettlementRequest(winning_outcome="over"),
                )
            still = ops.trades.get(trade.trade_id)
            assert still.state is PaperTradeState.OPEN
            assert still.line is None
            assert _recovery_events(still) == []
    finally:
        repository.close()
        ledger.close()


def test_manual_settle_after_recovery_uses_stored_odds_treasury_and_is_idempotent(
    tmp_path: Path,
) -> None:
    ledger = SqlitePaperLedger(tmp_path / "treasury.sqlite")
    _scan, _watchlist, ops, repository = _ops(ledger=ledger, autofill=False)
    try:
        trade = _legacy_total_trade()
        for leg in trade.legs:
            leg.opening_action = (
                MarketAction.BUY if leg.venue is VenueName.KALSHI else MarketAction.BACK
            )
            leg.canonical_state = leg.outcome
            leg.filled_odds = Decimal("2.00")
            leg.displayed_odds = Decimal("9.99")
        _persist_open(ops, trade)
        ops.catalogue = _catalogue(_row())
        options = ops.settlement_options(trade.trade_id)
        assert [choice.label for choice in options.choices] == ["Over 2.5", "Under 2.5"]
        persisted = ops.trades.get(trade.trade_id)
        expected = independent_realised_pnl_gbp(persisted, "over")
        settled = ops.settle_manual_result(
            trade.trade_id,
            PaperManualSettlementRequest(winning_outcome="over", operator_note="FT 2-1"),
        )
        assert settled.state is PaperTradeState.CLOSED
        assert settled.realised_pnl_gbp == expected
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
            PaperManualSettlementRequest(winning_outcome="over"),
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
                PaperManualSettlementRequest(winning_outcome="under"),
            )
    finally:
        repository.close()
        ledger.close()


def test_no_provider_writes_on_manual_recovery_paths() -> None:
    options_src = inspect.getsource(PaperOperationsService.settlement_options)
    settle_src = inspect.getsource(PaperOperationsService.settle_manual_result)
    recover_src = inspect.getsource(recover_legacy_market_line)
    combined = options_src + settle_src + recover_src
    assert "place_order" not in combined
    assert "cancel_order" not in combined
    assert "get_market" not in combined
    agent_src = inspect.getsource(PaperSettlementAgent)
    assert "place_order" not in agent_src
    assert "cancel_order" not in agent_src
