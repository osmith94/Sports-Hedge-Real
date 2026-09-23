"""BACKGROUND evaluates catalogue rows. PAPER entry stays behind existing gates.

Deterministic fixture/demo books, not live quotes. Thresholds, fees, and
execution rules are the current Settings defaults. Autofill is enabled only
on the entry attempt, which is the existing PAPER entry path.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from sports_hedge.accounting.paper_journal import DataProvenance
from sports_hedge.application.market_observation import VenueMarketObservation
from sports_hedge.application.paper_operations import PaperOperationsService
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.price_engine import PriceEnginePriority
from sports_hedge.arbitrage.priority_alerts.service import PriorityAlertService
from sports_hedge.arbitrage.watchlist.repository import SqliteWatchlistRepository
from sports_hedge.arbitrage.watchlist.service import WatchlistService
from sports_hedge.config import Settings
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import PaperScanDecision
from sports_hedge.paper.trades import PaperTradeState
from sports_hedge.persistence.paper_ledger import SqlitePaperLedger
from test_dual_cadence_scheduler import FakeClock
from test_issue316_catalogue_registry import _costs, _fx
from test_issue344_price_engine import (
    NOW,
    FakeKalshi,
    FakeMatchbook,
    _engine,
    _mb_btts,
    _row,
)
from test_step8f_automatic_paper_entry import _standing

DULL_COUNT = 6


class _Books(FakeKalshi):
    def __init__(self) -> None:
        super().__init__()
        self.by_ticker: dict[str, dict] = {}

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: object,
    ) -> dict:
        del event_id, outcome_id, filters
        ticker = str(market_id)
        self.book_calls.append(ticker)
        return self.by_ticker[ticker]


def _book(yes: str, no: str) -> dict:
    return {
        "orderbook_fp": {
            "yes_dollars": [[yes, "500.00"]],
            "no_dollars": [[no, "500.00"]],
        }
    }


def _market(market_id: str) -> dict:
    market = _mb_btts(int(market_id), odds="2.20")
    market["runners"][0]["prices"][0]["available-amount"] = "500"
    market["runners"][1]["prices"] = [
        {"side": "back", "odds": "1.80", "available-amount": "500"},
        {"side": "lay", "odds": "1.82", "available-amount": "500"},
    ]
    return market


class _CapturingScan(PaperScanService):
    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self.pairs: list[tuple[VenueMarketObservation, VenueMarketObservation]] = []

    def scan_pair(self, left: VenueMarketObservation, right: VenueMarketObservation, **kwargs: object):
        self.pairs.append((left, right))
        return super().scan_pair(left, right, **kwargs)


def _persist(tmp_path: Path, name: str, decision: PaperScanDecision, history: list) -> list:
    ledger = SqlitePaperLedger(
        tmp_path / f"{name}.sqlite",
        seed_gbp=Decimal("5000"),
        usd_gbp_per_unit=Decimal("0.75"),
        fx_source="test",
    )
    watchlist = WatchlistService(
        SqliteWatchlistRepository(tmp_path / f"{name}.watch"),
        max_quote_age_ms=10_000,
    )
    operations = PaperOperationsService(
        watchlist=watchlist,
        alerts=PriorityAlertService(),
        settings=Settings(paper_autofill_enabled=True),
        ledger=ledger,
    )
    try:
        watchlist.observe_paper_decision(decision, history)
        operations.persist_triggered_chain(decision, provenance=DataProvenance.LIVE_PAPER)
        return list(operations.list_active_trades())
    finally:
        ledger.close()


@pytest.mark.asyncio
async def test_background_evaluates_without_entry_until_threshold_and_gates(
    tmp_path: Path,
) -> None:
    defaults = Settings()
    assert defaults.min_net_edge == 0.01
    assert defaults.max_execution_risk == 60
    assert defaults.paper_autofill_enabled is False
    assert defaults.sports_hedge_mode == "paper"
    assert defaults.sports_hedge_execution_enabled is False

    matchbook = FakeMatchbook()
    kalshi = _Books()
    rows = []
    for index in range(DULL_COUNT):
        market_id = str(316100 + index)
        event = f"KXEPLBTTS-D{index}"
        rows.append(
            _row(
                suffix=f"d{index}",
                matchbook_event_id=str(8800 + index),
                matchbook_market_id=market_id,
                kalshi_event=event,
            )
        )
        matchbook.payloads[market_id] = _market(market_id)
        kalshi.by_ticker[f"{event}-BTTS"] = _book("0.40", "0.49")
    rows.append(
        _row(
            suffix="rich",
            matchbook_event_id="8899",
            matchbook_market_id="316199",
            kalshi_event="KXEPLBTTS-RICH",
        )
    )
    matchbook.payloads["316199"] = _market("316199")
    kalshi.by_ticker["KXEPLBTTS-RICH-BTTS"] = _book("0.20", "0.70")

    repository = SqliteMarketIntelligenceRepository()
    scan = _CapturingScan(MarketIntelligenceService(repository), settings=Settings())
    try:
        engine, _, _, _layer = _engine(
            rows,
            matchbook=matchbook,
            kalshi=kalshi,
            paper_scan=scan,
            clock=FakeClock(NOW),
        )
        engine.venue_costs = _costs()
        engine.fx_snapshots = _fx()
        result = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
        assert len(result.evaluated) == DULL_COUNT + 1
        assert len(result.decisions) == DULL_COUNT + 1
        assert matchbook.list_events_calls == 0
        assert kalshi.list_events_calls == 0
        assert matchbook.list_markets_calls == []
        assert len(matchbook.get_market_calls) == DULL_COUNT + 1
        assert len(kalshi.book_calls) == DULL_COUNT + 1

        dull = [item for item in result.decisions if not item.eligible_for_paper_simulation]
        rich = [item for item in result.decisions if item.eligible_for_paper_simulation]
        assert len(dull) == DULL_COUNT
        assert len(rich) == 1
        qualifying = rich[0]
        assert all(item.minimum_net_edge == Decimal("0.01") for item in result.decisions)
        assert all("no_positive_edge" in item.rejection_reasons for item in dull)
        assert qualifying.rejection_reasons == []
        assert qualifying.execution_risk is not None
        assert qualifying.execution_risk.score <= defaults.max_execution_risk
        assert qualifying.depth_scan is not None
        assert qualifying.depth_scan.solution.roi > Decimal(str(defaults.min_net_edge))

        for decision in dull:
            history = scan.market_intelligence.market_history(
                canonical_market_id=decision.canonical_market_id
            )
            assert _persist(tmp_path, f"dull-{decision.canonical_market_id}", decision, history) == []

        history = scan.market_intelligence.market_history(
            canonical_market_id=qualifying.canonical_market_id
        )
        # BACKGROUND pricing does not size a paper position. Entry still
        # requires the existing allocator plus treasury and risk gates.
        assert qualifying.allocation is None
        assert _persist(tmp_path, "ungated", qualifying, history) == []

        left, right = next(
            pair
            for pair in scan.pairs
            if str(pair[0].market.source_market_id) == "316199"
        )
        entry_kwargs = {
            "venue_costs": _costs(),
            "fx_snapshots": _fx(),
            "fixture_canonical_event_id": qualifying.fixture_canonical_event_id,
            "minimum_net_edge": qualifying.minimum_net_edge,
            "maximum_execution_risk": defaults.max_execution_risk,
        }
        unfunded = scan.scan_pair(
            left,
            right,
            liquidity_snapshot=_standing(
                matchbook_gbp=Decimal("0"),
                polymarket_usd=Decimal("0"),
                kalshi_usd=Decimal("0"),
            ),
            **entry_kwargs,
        )
        assert "insufficient_venue_capital" in unfunded.rejection_reasons
        assert unfunded.eligible_for_paper_simulation is False
        unfunded_history = scan.market_intelligence.market_history(
            canonical_market_id=unfunded.canonical_market_id
        )
        assert _persist(tmp_path, "unfunded", unfunded, unfunded_history) == []

        assert qualifying.execution_risk is not None
        over_risk = scan.scan_pair(
            left,
            right,
            liquidity_snapshot=_standing(),
            **{
                **entry_kwargs,
                "maximum_execution_risk": qualifying.execution_risk.score - 1,
            },
        )
        assert "execution_risk_above_threshold" in over_risk.rejection_reasons
        assert over_risk.eligible_for_paper_simulation is False
        over_risk_history = scan.market_intelligence.market_history(
            canonical_market_id=over_risk.canonical_market_id
        )
        assert _persist(tmp_path, "over-risk", over_risk, over_risk_history) == []

        funded = scan.scan_pair(
            left,
            right,
            liquidity_snapshot=_standing(),
            **entry_kwargs,
        )
        assert funded.minimum_net_edge == Decimal(str(defaults.min_net_edge))
        assert funded.eligible_for_paper_simulation is True
        assert funded.allocation is not None and funded.allocation.accepted
        assert funded.execution_risk is not None
        assert funded.execution_risk.score <= defaults.max_execution_risk
        funded_history = scan.market_intelligence.market_history(
            canonical_market_id=funded.canonical_market_id
        )
        opened = _persist(tmp_path, "funded", funded, funded_history)
        assert len(opened) == 1
        assert opened[0].state is PaperTradeState.OPEN
        assert opened[0].capital_locked_gbp is not None
        assert opened[0].capital_locked_gbp > 0
    finally:
        repository.close()
