"""UNIVERSE catalogues from metadata. Executable books belong to the price engine.

A PM-heavy discovery set used to fan out get_order_book for every listed
Polymarket market on a cross-venue fixture. That is the owner-live pattern of
hundreds of book_depth calls inside UNIVERSE.

This regression is deterministic fixture/demo data. It is not live quotes,
historical odds, or modelled probabilities. PAPER / read-only.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import timedelta
from typing import Any

import pytest

from sports_hedge.application.collector import (
    DEFAULT_PROVIDER_CONCURRENCY,
    ReadOnlyCrossVenueCollector,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.price_engine import CataloguePriceEngine, PriceEnginePriority
from sports_hedge.application.provider_access import (
    DEFAULT_PROVIDER_CONCURRENCY as ACCESS_CONCURRENCY,
    PRICE_ENGINE_ACTIVE_TRADE_LANE,
    ProviderAccessLayer,
    ProviderPriority,
)
from sports_hedge.application.scan_lanes import DEFAULT_HOT_HORIZON, ScanLane
from sports_hedge.config import Settings
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.approved_register import CANONICAL_BTTS_FT
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore
from test_issue316_catalogue_registry import (
    AWAY,
    COMPETITION,
    HOME,
    KICKOFF,
    NOW,
    _all_books,
    _costs,
    _fx,
    _mb_event,
)
from test_issue341_approved_market_catalogue import (
    FOUR_KEYS,
    _kalshi_events,
    _mb_markets,
    _series_map,
)
from test_issue_147_market_evaluation_state import THREE_LEAGUE_FIXTURES
from test_issue293_owner_live_overlap import OverlapKalshi, OverlapMatchbook


PROP_MARKETS = 80
PM_CROSS_EVENT = "pm-brentford-chelsea"
CROSS_TEAMS = {HOME.casefold(), AWAY.casefold()}


def _pm_prop(index: int) -> dict[str, Any]:
    yes = str(10**20 + index * 2)
    no = str(10**20 + index * 2 + 1)
    return {
        "id": f"pm-prop-{index}",
        "question": f"Will there be a pitch invasion {index}?",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": f'["{yes}", "{no}"]',
        "description": "Novelty prop. Not an approved family.",
        "feesEnabled": False,
    }


def _single_venue_events() -> list[tuple[str, str, str]]:
    events: list[tuple[str, str, str]] = []
    for competition, home, away in THREE_LEAGUE_FIXTURES:
        if {home.casefold(), away.casefold()} & CROSS_TEAMS:
            continue
        events.append((competition, home, away))
    return events


class _PricableMatchbook(OverlapMatchbook):
    def __init__(self, events: list[dict[str, Any]], markets_by_id: dict[str, list[dict[str, Any]]]) -> None:
        super().__init__(events, markets_by_id)
        self.get_market_calls: list[tuple[str, str]] = []

    async def get_market(
        self,
        event_id: int | str,
        market_id: int | str,
        **filters: Any,
    ) -> dict[str, Any]:
        del filters
        self.get_market_calls.append((str(event_id), str(market_id)))
        for market in self.markets_by_id.get(str(event_id), []):
            if str(market.get("id")) == str(market_id):
                return deepcopy(market)
        from sports_hedge.venues.matchbook import MatchbookMarketGoneError

        raise MatchbookMarketGoneError(event_id, market_id, 404)


class _Polymarket:
    def __init__(self, singles: list[tuple[str, str, str]]) -> None:
        self.singles = singles
        self.list_events_calls = 0
        self.list_markets_calls: list[str] = []
        self.book_calls: list[tuple[str, str, str]] = []
        self.cross_markets = [_pm_prop(index) for index in range(PROP_MARKETS)]

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        self.list_events_calls += 1
        events = [
            {
                "id": PM_CROSS_EVENT,
                "title": f"{HOME} vs {AWAY}",
                "startTime": KICKOFF.isoformat(),
                "competition": COMPETITION,
            }
        ]
        for index, (competition, home, away) in enumerate(self.singles):
            events.append(
                {
                    "id": f"pm-single-{index}",
                    "title": f"{home} vs {away}",
                    "startTime": (KICKOFF + timedelta(days=index + 1)).isoformat(),
                    "competition": competition,
                }
            )
        return events

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del filters
        self.list_markets_calls.append(str(event_id))
        if str(event_id) == PM_CROSS_EVENT:
            return list(self.cross_markets)
        return [_pm_prop(10_000 + index) for index in range(4)]

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del filters
        token = str(outcome_id or "")
        self.book_calls.append((str(event_id), str(market_id), token))
        return {
            "asset_id": token,
            "timestamp": int(NOW.timestamp() * 1000),
            "bids": [{"price": "0.49", "size": "100"}],
            "asks": [{"price": "0.51", "size": "100"}],
        }


def _potential_cross_venue_book_calls(polymarket: _Polymarket) -> int:
    total = 0
    for market in polymarket.cross_markets:
        tokens = market["clobTokenIds"].strip("[]").split(",")
        total += len([item for item in tokens if item.strip()])
    return total


@pytest.mark.asyncio
async def test_pm_heavy_universe_catalogues_without_book_fanout_and_background_prices() -> None:
    singles = _single_venue_events()
    assert len(singles) >= 20
    matchbook = _PricableMatchbook([_mb_event()], {str(_mb_event()["id"]): _mb_markets()})
    kalshi = OverlapKalshi(_kalshi_events(), series_by_ticker=_series_map(), books=_all_books())
    polymarket = _Polymarket(singles)
    potential_books = _potential_cross_venue_book_calls(polymarket)
    assert potential_books >= 160
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        catalogue_store=store,
        provider_concurrency=dict(DEFAULT_PROVIDER_CONCURRENCY),
    )
    settings = Settings()
    try:
        report = await collector.collect_and_scan(
            scan_lane=ScanLane.UNIVERSE.value,
            enabled_venues=[VenueName.MATCHBOOK, VenueName.POLYMARKET, VenueName.KALSHI],
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
            max_event_pairs=200,
            unbounded_cycle=True,
        )
        classes = report.scan_diagnostics["provider_call_classes"]
        assert polymarket.book_calls == []
        assert kalshi.book_calls == []
        assert report.scan_diagnostics["stages"]["book_depth"]["calls"] == 0
        assert classes["executable_book_depth"] == 0
        assert classes["identity_metadata"] > 0
        assert report.scan_diagnostics["universe_executable_pricing_deferred"] is True
        assert report.scan_diagnostics["paper_decision_timing"] == "deferred_to_price_engine"
        assert report.paper_decisions == []
        assert PM_CROSS_EVENT in polymarket.list_markets_calls
        assert all(not item.startswith("pm-single-") for item in polymarket.list_markets_calls)
        assert report.scan_diagnostics["provider_concurrency"] == {
            "matchbook": 4,
            "polymarket": 8,
            "kalshi": 4,
        }
        assert DEFAULT_PROVIDER_CONCURRENCY == ACCESS_CONCURRENCY
        assert settings.paper_scan_matchbook_concurrency == 4
        assert settings.paper_scan_polymarket_concurrency == 8
        assert settings.paper_scan_kalshi_concurrency == 4
        assert settings.sports_hedge_mode == "paper"
        assert settings.sports_hedge_execution_enabled is False
        assert DEFAULT_HOT_HORIZON == timedelta(minutes=60)
        assert ProviderPriority.ACTIVE_TRADE < ProviderPriority.HOT < ProviderPriority.UNIVERSE
        assert ProviderPriority.UNIVERSE < ProviderPriority.BACKGROUND
        assert PRICE_ENGINE_ACTIVE_TRADE_LANE == "active_trade"

        rows = store.list_active()
        assert {row.register_canonical_key for row in rows} == FOUR_KEYS
        btts = next(row for row in rows if row.register_canonical_key == CANONICAL_BTTS_FT)
        assert btts.matchbook_market_id
        assert btts.kalshi_market_tickers
        catalogue_tickers = {
            ticker
            for row in rows
            for ticker in row.kalshi_market_tickers
        }
        catalogue_matchbook = {(row.matchbook_event_id, row.matchbook_market_id) for row in rows}

        hot_row = btts.model_copy(
            update={
                "catalogue_row_id": f"{btts.catalogue_row_id}:near",
                "canonical_event_id": f"{btts.canonical_event_id}:near",
                "kickoff_utc": NOW + timedelta(minutes=20),
                "content_version": 1,
            }
        )
        store.upsert_catalogue_row(hot_row)

        engine = CataloguePriceEngine(
            catalogue_store=store,
            matchbook=matchbook,
            kalshi=kalshi,
            polymarket=polymarket,
            paper_scan=PaperScanService(MarketIntelligenceService(repository)),
            clock=lambda: NOW,
            provider_access=ProviderAccessLayer(limits=dict(DEFAULT_PROVIDER_CONCURRENCY)),
            venue_costs=_costs(),
            fx_snapshots=_fx(),
        )
        engine.reconstruct()
        far_runtime = engine.item(btts.catalogue_row_id)
        near_runtime = engine.item(hot_row.catalogue_row_id)
        assert far_runtime is not None and far_runtime.priority is PriceEnginePriority.BACKGROUND
        assert near_runtime is not None and near_runtime.priority is PriceEnginePriority.HOT
        background_ids = {
            item.identity.catalogue_row_id
            for item in engine.due_items(PriceEnginePriority.BACKGROUND, now=NOW)
        }
        hot_ids = {
            item.identity.catalogue_row_id
            for item in engine.due_items(PriceEnginePriority.HOT, now=NOW)
        }
        assert btts.catalogue_row_id in background_ids
        assert hot_row.catalogue_row_id in hot_ids
        assert hot_row.catalogue_row_id not in background_ids
        assert btts.catalogue_row_id not in hot_ids

        discovery_events = polymarket.list_events_calls
        matchbook_events = matchbook.list_events_calls
        kalshi_events = kalshi.list_events_calls
        matchbook_lists = list(matchbook.list_markets_calls)
        await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
        await engine.run_slice(PriceEnginePriority.HOT, now=NOW)

        assert polymarket.list_events_calls == discovery_events
        assert matchbook.list_events_calls == matchbook_events
        assert kalshi.list_events_calls == kalshi_events
        assert matchbook.list_markets_calls == matchbook_lists
        assert polymarket.book_calls == []
        assert kalshi.book_calls
        assert set(kalshi.book_calls) <= catalogue_tickers
        assert set(matchbook.get_market_calls) <= catalogue_matchbook
        assert ("316001", btts.matchbook_market_id) in matchbook.get_market_calls or (
            btts.matchbook_event_id,
            btts.matchbook_market_id,
        ) in matchbook.get_market_calls
        assert potential_books > len(polymarket.book_calls)
    finally:
        repository.close()
        store.close()
