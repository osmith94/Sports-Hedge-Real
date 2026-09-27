"""Integrated Price-2, placement-threshold and consumed-liquidity scenario.

Fixture books only. One opportunity opens under Max One-Time, a repeated
displayed book only offers depth that PAPER has not already consumed, Max
Opportunity stops the next cycle while residual depth still exists, and a
second opportunity on the same event is limited by Max Event. Risk score 99
and the retired allocator caps do not shrink those amounts.
"""

from __future__ import annotations

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
from test_iterative_price2_paper_fills import _audits

from sports_hedge.application.execution_reprice import CYCLE_ALLOCATION_CEILING
from sports_hedge.arbitrage.allocation.adapters import (
    balances_from_treasury,
    exposures_from_trades,
    request_from_paper_decision,
)
from sports_hedge.arbitrage.allocation.engine import allocate
from sports_hedge.arbitrage.allocation.models import AllocationConstraintKind
from sports_hedge.arbitrage.allocation.policy import policy_from_settings
from sports_hedge.config import Settings
from sports_hedge.paper.placement_room import REPORTING_GBP_QUANTUM
from sports_hedge.paper.trades import PaperTradeTrancheKind
from sports_hedge.risk.execution import ExecutionRiskResult, ExecutionRiskScorer

HIDDEN = {
    AllocationConstraintKind.MIN_FREE_RESERVE,
    AllocationConstraintKind.MAX_POOL_FRACTION,
    AllocationConstraintKind.FIXTURE_CONCENTRATION,
    AllocationConstraintKind.PORTFOLIO_CAP,
    AllocationConstraintKind.VENUE_LIMIT,
    AllocationConstraintKind.PER_OPPORTUNITY_LIMIT,
    AllocationConstraintKind.EXTERNAL_LEG_CAP,
}
HUNDRED = Decimal("100")
TWO_HUNDRED = Decimal("200")
FIFTY = Decimal("50")


def _force_risk_99(monkeypatch: pytest.MonkeyPatch) -> None:
    def score(self, data):
        del self
        return ExecutionRiskResult(
            score=99,
            band="extreme",
            reasons=["integration_probe"],
            inputs=data,
        )

    monkeypatch.setattr(ExecutionRiskScorer, "score", score)


def _liquidity(row: dict) -> list[dict]:
    payload = json.loads(row["snapshot_json"])
    return list(payload["paper_liquidity"])


def _row_for(rows: list[dict], venue: str) -> dict:
    matched = [item for item in rows if item["venue"] == venue]
    assert len(matched) == 1
    return matched[0]


@pytest.mark.asyncio
async def test_price2_cycles_respect_placement_room_and_consumed_depth(
    tmp_path, monkeypatch
) -> None:
    _force_risk_99(monkeypatch)
    # Matchbook shows a £200 GBP level. Kalshi is deep enough that two £100
    # cycles still leave genuine residual depth, so cycle 3 is stopped by
    # Max Opportunity rather than an empty book.
    displayed = "200"
    book = _book("0.20", "0.70", "1000.00")
    market = _market(displayed)
    bundle = await _run(
        tmp_path,
        monkeypatch,
        name="integrated-caps",
        matchbook_payloads=[
            _stale(market),
            _fresh(market),
            _fresh(market),
            _fresh(market),
            _fresh(market),
        ],
        kalshi_books=[book, book, book, book, book],
        treasury=Decimal("500"),
        max_event_gbp=250,
        max_opportunity_gbp=200,
        max_one_time_gbp=100,
        max_allocated_per_trade_gbp=1.0,
        extra_settings={
            "max_execution_risk": 0,
            "allocation_min_reserve_fraction": 0.99,
            "allocation_max_pool_fraction_per_opportunity": 0.01,
            "allocation_max_open_capital_fraction": 0.01,
            "allocation_max_same_fixture_fraction": 0.01,
            "max_total_exposure_gbp": 1.0,
            "allocation_matchbook_limit_gbp": 1.0,
            "allocation_external_leg_cap_native": 1.0,
            "priority_safety_haircut": 0.5,
        },
    )
    try:
        assert Settings().sports_hedge_execution_enabled is False
        trade = _open_trade(bundle)
        assert trade.paper_only is True
        assert trade.places_orders is False
        assert trade.entry_risk is not None
        assert trade.entry_risk.score == 99
        opening = next(item for item in trade.tranches if item.kind is PaperTradeTrancheKind.OPENING)
        top_ups = [item for item in trade.tranches if item.kind is PaperTradeTrancheKind.TOP_UP]
        assert len(trade.tranches) == 2
        assert len(top_ups) == 1
        assert opening.capital_locked_gbp is not None
        assert top_ups[0].capital_locked_gbp is not None
        assert opening.capital_locked_gbp <= HUNDRED
        assert top_ups[0].capital_locked_gbp <= HUNDRED
        assert opening.capital_locked_gbp >= REPORTING_GBP_QUANTUM
        assert top_ups[0].capital_locked_gbp >= REPORTING_GBP_QUANTUM
        locked = trade.capital_locked_gbp or Decimal("0")
        tranche_sum = opening.capital_locked_gbp + top_ups[0].capital_locked_gbp
        # Trade lock and tranche figures keep raw ledger precision. They may
        # differ by a unit in the last place, and neither may exceed the cap
        # or leave a deployable penny of opportunity room.
        assert locked <= TWO_HUNDRED
        assert tranche_sum <= TWO_HUNDRED
        assert TWO_HUNDRED - locked < REPORTING_GBP_QUANTUM
        assert TWO_HUNDRED - tranche_sum < REPORTING_GBP_QUANTUM
        assert abs(locked - tranche_sum) < REPORTING_GBP_QUANTUM
        assert opening.execution_snapshot_id
        assert top_ups[0].execution_snapshot_id
        assert opening.execution_snapshot_id != top_ups[0].execution_snapshot_id
        assert top_ups[0].idempotency_key == top_ups[0].execution_snapshot_id

        audits = _audits(bundle, trade.opportunity_id)
        assert [row["cycle_outcome"] for row in audits] == [
            "filled",
            "filled",
            CYCLE_ALLOCATION_CEILING,
        ]
        assert audits[-1]["tranche_id"] is None
        filled = [row for row in audits if row["cycle_outcome"] == "filled"]
        assert len(filled) == 2
        before_ceiling = Decimal(json.loads(filled[-1]["snapshot_json"])["cumulative_capital_gbp"])
        after_ceiling = Decimal(json.loads(audits[-1]["snapshot_json"])["cumulative_capital_gbp"])
        assert before_ceiling == after_ceiling == locked
        first = _liquidity(filled[0])
        second = _liquidity(filled[1])
        third = _liquidity(audits[-1])
        for label, rows in (("open", first), ("topup", second), ("capped", third)):
            matchbook = _row_for(rows, "matchbook")
            assert Decimal(matchbook["observed"]) == TWO_HUNDRED, label
            consumed = Decimal(matchbook["previously_consumed"])
            incremental = Decimal(matchbook["incremental"])
            assert incremental == TWO_HUNDRED - consumed
            if label == "open":
                assert consumed == Decimal("0")
            else:
                assert consumed > Decimal("0")
                assert incremental < TWO_HUNDRED
        # Cycle 3 still has unused displayed depth. The stop is the opportunity cap.
        assert Decimal(_row_for(third, "matchbook")["incremental"]) > Decimal("0")
        assert Decimal(_row_for(third, "kalshi")["incremental"]) > Decimal("0")
        assert trade.active_trade_phase.value == "monitoring_cap_reached"

        execution = next(
            item
            for item in bundle.scan.seen
            if item.execution_risk is not None and item.execution_risk.score == 99
        )
        sibling = execution.model_copy(
            update={"canonical_market_id": f"{execution.canonical_market_id}:total"}
        )
        request = request_from_paper_decision(
            sibling,
            policy=policy_from_settings(bundle.settings),
            balances=balances_from_treasury(bundle.ledger.treasury.snapshot()),
            open_positions=exposures_from_trades([trade]),
            opportunity_id="sibling-opportunity",
        )
        assert request is not None
        assert request.discretionary_placement is True
        assert request.execution_risk_score == 99
        sized = allocate(request)
        assert sized.accepted
        assert sized.event_deployed_gbp == locked
        assert sized.event_room_gbp == FIFTY
        assert sized.opportunity_deployed_gbp == Decimal("0")
        assert locked <= TWO_HUNDRED
        assert TWO_HUNDRED - locked < REPORTING_GBP_QUANTUM
        assert sized.maximum_validated_capital <= FIFTY
        assert sized.recommended_committed_capital <= FIFTY
        assert sized.maximum_validated_capital >= FIFTY - REPORTING_GBP_QUANTUM
        stake_reporting = sum(
            (stake.capital_reporting for stake in sized.recommended_stakes),
            Decimal("0"),
        )
        assert stake_reporting <= FIFTY
        assert len(bundle.operations.list_active_trades()) == 1
        assert sized.limiting_constraint is AllocationConstraintKind.MAX_EVENT
        assert HIDDEN.isdisjoint(item.kind for item in sized.hard_constraints)
        assert sized.execution_risk_score == 99
    finally:
        bundle.repository.close()
        bundle.ledger.close()
        bundle.watchlist.repository.close()
