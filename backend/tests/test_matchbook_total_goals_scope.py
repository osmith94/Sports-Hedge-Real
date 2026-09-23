from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.complete_set import solver_eligible_market
from sports_hedge.application.fixture_inventory import (
    FX_STATUS_KNOWN,
    FX_STATUS_MISSING,
    FX_STATUS_NOT_REQUIRED,
    InventoryComparisonStatus,
    InventoryMarket,
    assemble_fixture_inventory,
)
from sports_hedge.application.market_observation import (
    KalshiObservationBuilder,
    MatchbookObservationBuilder,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.config import Settings
from sports_hedge.domain.football import CanonicalOutcome, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.markets import MarketMatchResult, MarketMatcher
from sports_hedge.normalization.venues import MatchbookNormalizer
from sports_hedge.paper.models import FxRateSnapshot, PaperScanDecision
from venue_cost_helpers import profit_commission_cost


KICKOFF = datetime(2026, 9, 14, 15, 0, tzinfo=UTC)
OBSERVED = datetime(2026, 9, 14, 16, 12, tzinfo=UTC)
REGULATION = "Resolves on 90 minutes of regulation time. Extra time and penalties do not count."

LEEDS_EVENT_ID = "34213468549700081"
OFFENDING_MARKET_ID = "34328274317601081"
MATCH_TOTAL_MARKET_ID = "34328274317601999"
KALSHI_EVENT_TICKER = "KXEPLTOTAL-26SEP14LEENEW"
KALSHI_MARKET_TICKER = "KXEPLTOTAL-26SEP14LEENEW-3"

KALSHI_SERIES = {
    "ticker": "KXEPLTOTAL",
    "title": "Premier League",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "settlement_sources": [{"name": "Opta"}],
}

MB_EVENT = {
    "id": LEEDS_EVENT_ID,
    "name": "Leeds United vs Newcastle United",
    "start": KICKOFF.isoformat(),
    "competition-name": "Premier League",
}

KALSHI_EVENT = {
    "event_ticker": KALSHI_EVENT_TICKER,
    "series_ticker": "KXEPLTOTAL",
    "title": "Leeds United vs Newcastle United",
    "category": "Sports",
    "strike_date": KICKOFF.isoformat(),
}


def _offending_matchbook_market() -> dict[str, Any]:
    """Live-shaped participant total that was wrongly bound as match Total 2.5."""

    return {
        "id": int(OFFENDING_MARKET_ID),
        "name": "Over/Under 2.5 Goals",
        "market-type": "other",
        "event-participant-id": 34213468549700100,
        "runners": [
            {
                "id": 11,
                "name": "Over 2.5",
                "event-participant-id": 34213468549700100,
                "prices": [
                    {"side": "back", "odds": "6.80", "available-amount": "41"},
                    {"side": "lay", "odds": "7.20", "available-amount": "10"},
                ],
            },
            {
                "id": 12,
                "name": "Under 2.5",
                "event-participant-id": 34213468549700100,
                "prices": [
                    {"side": "back", "odds": "1.16", "available-amount": "257"},
                    {"side": "lay", "odds": "1.18", "available-amount": "20"},
                ],
            },
        ],
    }


def _true_match_total_market() -> dict[str, Any]:
    return {
        "id": int(MATCH_TOTAL_MARKET_ID),
        "name": "Over/Under 2.5 Goals",
        "market-type": "other",
        "runners": [
            {
                "id": 21,
                "name": "Over 2.5",
                "prices": [
                    {"side": "back", "odds": "1.83", "available-amount": "2525"},
                    {"side": "lay", "odds": "1.85", "available-amount": "40"},
                ],
            },
            {
                "id": 22,
                "name": "Under 2.5",
                "prices": [
                    {"side": "back", "odds": "2.18", "available-amount": "2"},
                    {"side": "lay", "odds": "2.20", "available-amount": "5"},
                ],
            },
        ],
    }


def _named_team_total_market() -> dict[str, Any]:
    return {
        "id": 34328274317601082,
        "name": "Leeds United Over/Under 2.5 Goals",
        "market-type": "other",
        "runners": [
            {"id": 31, "name": "Over 2.5"},
            {"id": 32, "name": "Under 2.5"},
        ],
    }


def _kalshi_total_market() -> dict[str, Any]:
    return {
        "ticker": KALSHI_MARKET_TICKER,
        "event_ticker": KALSHI_EVENT_TICKER,
        "title": "Leeds United vs Newcastle United Total Goals 2.5",
        "yes_sub_title": "Over 2.5",
        "rules_primary": REGULATION,
    }


def _kalshi_total_book() -> dict[str, Any]:
    return {
        "orderbook_fp": {
            "yes_dollars": [["0.5305", "1.00"]],
            "no_dollars": [["0.4595", "3926.00"]],
        }
    }


def _inventory(observation, *, name: str, market_type: str | None = None) -> InventoryMarket:
    labels = [
        str(label)
        for label in (observation.metadata.get("raw_runner_labels") or [])
        if str(label).strip()
    ]
    return InventoryMarket(
        venue=observation.venue,
        source_event_id=observation.market.event.source_event_id,
        source_market_id=observation.market.source_market_id,
        raw_name=name,
        raw_market_type=market_type,
        raw_runner_labels=labels,
        canonical=observation.market,
        observation=observation,
    )


def _fx() -> list[FxRateSnapshot]:
    return [FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test")]


def _costs() -> list:
    captured = OBSERVED
    return [
        profit_commission_cost(VenueName.MATCHBOOK, "0.02", captured_at=captured),
        kalshi_cost_from_series(KALSHI_SERIES, captured_at=captured),
    ]


def test_offending_payload_cannot_be_full_match_total_goals() -> None:
    event = MatchbookNormalizer().normalize_event(MB_EVENT)
    offending = MatchbookNormalizer().normalize_market(event, _offending_matchbook_market())
    true_total = MatchbookNormalizer().normalize_market(event, _true_match_total_market())
    named = MatchbookNormalizer().normalize_market(event, _named_team_total_market())

    assert offending.family is MarketFamily.TEAM_TOTAL
    assert offending.source_market_id == OFFENDING_MARKET_ID
    assert solver_eligible_market(offending) is False
    assert true_total.family is MarketFamily.TOTAL_GOALS
    assert true_total.line == Decimal("2.5")
    assert true_total.period.value == "full_time"
    assert solver_eligible_market(true_total) is True
    assert named.family is MarketFamily.TEAM_TOTAL


def test_offending_market_does_not_match_or_scan_against_kalshi_match_total() -> None:
    offending = MatchbookObservationBuilder().build(
        MB_EVENT, _offending_matchbook_market(), observed_at=OBSERVED, quote_age_ms=11
    )
    true_total = MatchbookObservationBuilder().build(
        MB_EVENT, _true_match_total_market(), observed_at=OBSERVED, quote_age_ms=11
    )
    kalshi = KalshiObservationBuilder().build(
        KALSHI_EVENT,
        _kalshi_total_market(),
        {KALSHI_MARKET_TICKER: _kalshi_total_book()},
        series=KALSHI_SERIES,
        observed_at=OBSERVED,
        quote_age_ms=0,
        quote_age_basis="retrieval",
        fee_snapshot={"fee_type": "quadratic", "fee_multiplier": "1"},
    )

    matcher = MarketMatcher()
    assert matcher.match(offending.market, kalshi.market).matched is False
    assert "market_family_mismatch" in matcher.match(offending.market, kalshi.market).reasons
    assert matcher.match(true_total.market, kalshi.market).matched is True

    service = PaperScanService(MarketIntelligenceService(SqliteMarketIntelligenceRepository()))
    false_decision = service.scan_pair(
        offending, kalshi, venue_costs=_costs(), fx_snapshots=_fx(), maximum_execution_risk=100
    )
    true_decision = service.scan_pair(
        true_total, kalshi, venue_costs=_costs(), fx_snapshots=_fx(), maximum_execution_risk=100
    )
    assert false_decision.eligible_for_paper_simulation is False
    assert "market_not_equivalent" in false_decision.rejection_reasons or not matcher.match(
        offending.market, kalshi.market
    ).matched
    assert false_decision.depth_scan is None
    assert true_decision.market_match.matched is True
    assert true_decision.solver_model == "simple_complete_set"
    assert Settings().sports_hedge_execution_enabled is False


def test_inventory_attaches_kalshi_to_true_match_total_and_keeps_team_total_ineligible() -> None:
    offending = MatchbookObservationBuilder().build(
        MB_EVENT, _offending_matchbook_market(), observed_at=OBSERVED, quote_age_ms=11
    )
    true_total = MatchbookObservationBuilder().build(
        MB_EVENT, _true_match_total_market(), observed_at=OBSERVED, quote_age_ms=11
    )
    kalshi = KalshiObservationBuilder().build(
        KALSHI_EVENT,
        _kalshi_total_market(),
        {KALSHI_MARKET_TICKER: _kalshi_total_book()},
        series=KALSHI_SERIES,
        observed_at=OBSERVED,
        quote_age_ms=0,
        quote_age_basis="retrieval",
        fee_snapshot={"fee_type": "quadratic", "fee_multiplier": "1"},
    )
    service = PaperScanService(MarketIntelligenceService(SqliteMarketIntelligenceRepository()))
    decision = service.scan_pair(
        true_total, kalshi, venue_costs=_costs(), fx_snapshots=_fx(), maximum_execution_risk=100
    )
    rows = assemble_fixture_inventory(
        [
            _inventory(offending, name="Over/Under 2.5 Goals", market_type="other"),
            _inventory(true_total, name="Over/Under 2.5 Goals", market_type="other"),
        ],
        [],
        kalshi_markets=[
            _inventory(kalshi, name="Leeds United vs Newcastle United Total Goals 2.5"),
        ],
        decisions_by_pair={
            (
                VenueName.MATCHBOOK.value,
                MATCH_TOTAL_MARKET_ID,
                VenueName.KALSHI.value,
                KALSHI_MARKET_TICKER,
            ): decision
        },
        venue_costs=_costs(),
        fx_snapshots=None,
    )
    team_rows = [row for row in rows if row.family == MarketFamily.TEAM_TOTAL.value]
    match_rows = [
        row
        for row in rows
        if row.family == MarketFamily.TOTAL_GOALS.value and row.matchbook is not None
    ]
    assert team_rows
    assert all(row.entered_solver is False for row in team_rows)
    assert all(row.kalshi is None for row in team_rows)
    assert all(row.solver_is_arbitrage is False for row in team_rows)
    assert match_rows
    matched = match_rows[0]
    assert matched.matchbook is not None
    assert matched.matchbook.source_market_id == MATCH_TOTAL_MARKET_ID
    assert matched.kalshi is not None
    assert matched.kalshi.source_market_id == KALSHI_MARKET_TICKER
    assert matched.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT
    assert "venue_only" not in matched.rejection_reasons
    assert matched.reason != "venue_only"
    assert matched.matchbook.raw_market_name == "Over/Under 2.5 Goals"
    assert matched.matchbook.raw_market_type == "other"
    assert matched.matchbook.raw_runner_labels == ["Over 2.5", "Under 2.5"]
    assert matched.matchbook.fx_status == FX_STATUS_NOT_REQUIRED
    assert matched.kalshi.fx_status == FX_STATUS_KNOWN
    assert matched.entered_solver is True


def test_matched_row_does_not_keep_stale_venue_only_or_fx_missing() -> None:
    true_total = MatchbookObservationBuilder().build(
        MB_EVENT, _true_match_total_market(), observed_at=OBSERVED, quote_age_ms=11
    )
    kalshi = KalshiObservationBuilder().build(
        KALSHI_EVENT,
        _kalshi_total_market(),
        {KALSHI_MARKET_TICKER: _kalshi_total_book()},
        series=KALSHI_SERIES,
        observed_at=OBSERVED,
        quote_age_ms=0,
        quote_age_basis="retrieval",
        fee_snapshot={"fee_type": "quadratic", "fee_multiplier": "1"},
    )
    decision = PaperScanDecision(
        market_match=MarketMatchResult(matched=True, confidence=1.0, reasons=["teams_equivalent"]),
        solver_model="simple_complete_set",
        eligible_for_paper_simulation=True,
        fx_snapshots=_fx(),
    )
    rows = assemble_fixture_inventory(
        [_inventory(true_total, name="Over/Under 2.5 Goals", market_type="other")],
        [],
        kalshi_markets=[_inventory(kalshi, name="Total Goals 2.5")],
        decisions_by_pair={
            (
                VenueName.MATCHBOOK.value,
                MATCH_TOTAL_MARKET_ID,
                VenueName.KALSHI.value,
                KALSHI_MARKET_TICKER,
            ): decision
        },
        fx_snapshots=None,
    )
    row = next(item for item in rows if item.kalshi is not None and item.matchbook is not None)
    assert row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT
    assert "venue_only" not in row.rejection_reasons
    assert row.reason != "venue_only"
    assert row.kalshi is not None
    assert row.kalshi.fx_status == FX_STATUS_KNOWN
    assert row.matchbook is not None
    assert row.matchbook.fx_status == FX_STATUS_NOT_REQUIRED


def test_inventory_fx_missing_when_solver_also_lacked_usd() -> None:
    true_total = MatchbookObservationBuilder().build(
        MB_EVENT, _true_match_total_market(), observed_at=OBSERVED, quote_age_ms=11
    )
    kalshi = KalshiObservationBuilder().build(
        KALSHI_EVENT,
        _kalshi_total_market(),
        {KALSHI_MARKET_TICKER: _kalshi_total_book()},
        series=KALSHI_SERIES,
        observed_at=OBSERVED,
        quote_age_ms=0,
        fee_snapshot={"fee_type": "quadratic", "fee_multiplier": "1"},
    )
    rows = assemble_fixture_inventory(
        [_inventory(true_total, name="Over/Under 2.5 Goals", market_type="other")],
        [],
        kalshi_markets=[_inventory(kalshi, name="Total Goals 2.5")],
        fx_snapshots=None,
    )
    row = next(item for item in rows if item.kalshi is not None)
    assert row.kalshi is not None
    assert row.kalshi.fx_status == FX_STATUS_MISSING


class LeedsMatchbook:
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {
            "events": [
                {
                    **MB_EVENT,
                    "status": "open",
                    "in-running-flag": True,
                }
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": [_offending_matchbook_market(), _true_match_total_market()]}


class EmptyPolymarket:
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return []

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return []


class LeedsKalshi:
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {
            "events": [
                {
                    **KALSHI_EVENT,
                    "markets": [_kalshi_total_market()],
                }
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": [_kalshi_total_market()]}

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id, outcome_id, filters
        return _kalshi_total_book()

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        del series_ticker
        return KALSHI_SERIES


@pytest.mark.asyncio
async def test_collector_pairs_true_match_total_not_the_6_80_participant_book() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    collector = ReadOnlyCrossVenueCollector(
        matchbook=LeedsMatchbook(),
        polymarket=EmptyPolymarket(),
        kalshi=LeedsKalshi(),
        paper_scan=PaperScanService(intelligence),
    )
    try:
        report = await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
        )
        fixture = report.discovered_fixtures[0]
        rows = report.fixture_markets[fixture.canonical_event_id]
        team_rows = [row for row in rows if row.family == "team_total"]
        match_rows = [
            row
            for row in rows
            if row.family == "total_goals" and row.matchbook is not None and row.kalshi is not None
        ]
        assert team_rows
        assert all(row.matchbook and row.matchbook.source_market_id == OFFENDING_MARKET_ID for row in team_rows)
        assert all(not row.entered_solver for row in team_rows)
        assert all(row.kalshi is None for row in team_rows)
        assert match_rows
        matched = match_rows[0]
        assert matched.matchbook is not None
        assert matched.matchbook.source_market_id == MATCH_TOTAL_MARKET_ID
        over = next(quote for quote in matched.matchbook.best_backs if quote.outcome == CanonicalOutcome.OVER.value)
        assert over.decimal_odds == Decimal("1.83")
        assert all(
            not (
                decision.eligible_for_paper_simulation
                and any(
                    str(OFFENDING_MARKET_ID) in (decision.canonical_market_id or "")
                    for decision in report.paper_decisions
                )
            )
            for decision in report.paper_decisions
        )
        assert all(
            OFFENDING_MARKET_ID not in (decision.canonical_market_id or "")
            or not decision.eligible_for_paper_simulation
            for decision in report.paper_decisions
        )
        assert Settings().sports_hedge_execution_enabled is False
    finally:
        repository.close()
