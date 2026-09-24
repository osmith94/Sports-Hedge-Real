"""Market evidence must not poison fixture viability.

Synthetic providers and catalogue rows. Not live quotes. PAPER / read-only.
"""

from __future__ import annotations

from datetime import timedelta

import pytest
from test_issue293_owner_live_overlap import OverlapKalshi, OverlapMatchbook
from test_issue316_catalogue_registry import (
    AWAY,
    COMPETITION,
    HOME,
    _costs,
    _fx,
    _mb_event,
)
from test_issue341_approved_market_catalogue import (
    FOUR_KEYS,
    _kalshi_events,
    _mb_handicap,
    _mb_markets,
    _series_map,
)
from test_issue344_price_engine import NOW, FakeMatchbook, _engine, _row
from test_issue466_universe_indexed_clustering import _event

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.fixture_clusters import VenueEvent, cluster_venue_events
from sports_hedge.application.opportunity_viability import (
    CROSS_VENUE_UNAVAILABLE,
    assess_cluster_viability,
    assess_identity_viability,
    get_opportunity_viability_cache,
    reset_opportunity_viability_cache,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.price_engine import PriceEnginePriority
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.approved_register import (
    CANONICAL_BTTS_FT,
    CANONICAL_MATCH_RESULT_FT,
)
from sports_hedge.matching.events import EventMatcher
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore

CANONICAL = "evt-sibling-scope"
GONE_MARKET = "9004"


def _siblings():
    base = _row(suffix="scope", kickoff=NOW + timedelta(days=3))
    specs = (
        ("mr", CANONICAL_MATCH_RESULT_FT, "9001", "KXSCOPE-MR"),
        ("btts", CANONICAL_BTTS_FT, "9002", "KXSCOPE-BTTS"),
        ("t25", "TOTAL_GOALS_FT:2.5", "9003", "KXSCOPE-T25"),
        ("t45", "TOTAL_GOALS_FT:4.5", GONE_MARKET, "KXSCOPE-T45"),
    )
    rows = []
    for suffix, key, market_id, ticker in specs:
        rows.append(
            base.model_copy(
                update={
                    "catalogue_row_id": f"amc-scope-{suffix}",
                    "register_canonical_key": key,
                    "canonical_event_id": CANONICAL,
                    "matchbook_event_id": "mb-scope",
                    "matchbook_market_id": market_id,
                    "kalshi_event_ticker": ticker,
                    "kalshi_market_tickers": [ticker],
                }
            )
        )
    return rows


def _open_cluster():
    kickoff = NOW + timedelta(days=3)
    matchbook = [
        _event(
            VenueName.MATCHBOOK,
            "mb-scope",
            home="Brentford",
            away="Chelsea",
            kickoff=kickoff,
        )
    ]
    kalshi = [
        _event(
            VenueName.KALSHI,
            "k-scope",
            home="Brentford",
            away="Chelsea",
            kickoff=kickoff,
        )
    ]
    clusters, _counts = cluster_venue_events(
        matchbook=matchbook,
        polymarket=[],
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=8,
    )
    assert len(clusters) == 1
    return clusters[0]


@pytest.mark.asyncio
async def test_one_matchbook_market_404_does_not_poison_sibling_relationships() -> None:
    reset_opportunity_viability_cache()
    rows = _siblings()
    gone = rows[-1]
    healthy = rows[:-1]
    matchbook = FakeMatchbook()
    matchbook.gone.add(GONE_MARKET)
    poisoned, _mb, _ks, _layer = _engine(
        [gone],
        matchbook=matchbook,
        timeout=8,
        background_interval=0,
    )
    first = await poisoned.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    cache = get_opportunity_viability_cache()
    assert gone.catalogue_row_id in first.revalidation
    assert cache.is_blocked(CANONICAL, VenueName.MATCHBOOK) is False
    assert cache.market_is_blocked(CANONICAL, VenueName.MATCHBOOK, GONE_MARKET)
    assert cache.state(CANONICAL, VenueName.MATCHBOOK) is None

    matchbook.get_market_calls.clear()
    siblings, _mb2, _ks2, _layer2 = _engine(
        healthy,
        matchbook=matchbook,
        timeout=8,
        background_interval=0,
    )
    second = await siblings.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    called = {market_id for _event_id, market_id in matchbook.get_market_calls}
    assert called == {"9001", "9002", "9003"}
    assert GONE_MARKET not in called
    for row in healthy:
        assert second.skip_reasons.get(row.catalogue_row_id) != CROSS_VENUE_UNAVAILABLE
    gone_view = assess_identity_viability(gone, cache=cache)
    assert gone_view.skip_expensive_work is True
    assert gone_view.reason == CROSS_VENUE_UNAVAILABLE
    cluster = _open_cluster()
    assessment = assess_cluster_viability(cluster, canonical_event_id=CANONICAL, cache=cache)
    assert assessment.skip_expensive_work is False
    assert assessment.reason is None
    assert cache.is_blocked(CANONICAL, VenueName.MATCHBOOK) is False
    assert cache.market_is_blocked(CANONICAL, VenueName.MATCHBOOK, GONE_MARKET)


def test_terminal_matchbook_event_still_blocks_the_venue() -> None:
    reset_opportunity_viability_cache()
    kickoff = NOW - timedelta(hours=1)
    matchbook = VenueEvent(
        venue=VenueName.MATCHBOOK,
        raw={"id": "mb-terminal", "status": "closed"},
        canonical=CanonicalEvent(
            sport="football",
            competition="Premier League",
            home_team="Brentford",
            away_team="Chelsea",
            kickoff_utc=kickoff,
            source_venue=VenueName.MATCHBOOK,
            source_event_id="mb-terminal",
        ),
        source_event_id="mb-terminal",
    )
    kalshi = VenueEvent(
        venue=VenueName.KALSHI,
        raw={"id": "k-open", "status": "open"},
        canonical=CanonicalEvent(
            sport="football",
            competition="Premier League",
            home_team="Brentford",
            away_team="Chelsea",
            kickoff_utc=kickoff,
            source_venue=VenueName.KALSHI,
            source_event_id="k-open",
        ),
        source_event_id="k-open",
    )
    clusters, _counts = cluster_venue_events(
        matchbook=[matchbook],
        polymarket=[],
        kalshi=[kalshi],
        matcher=EventMatcher(),
        max_event_pairs=8,
    )
    assessment = assess_cluster_viability(
        clusters[0],
        canonical_event_id="evt-terminal-event",
    )
    cache = get_opportunity_viability_cache()
    assert assessment.skip_expensive_work is True
    assert assessment.reason == CROSS_VENUE_UNAVAILABLE
    assert cache.is_blocked("evt-terminal-event", VenueName.MATCHBOOK)
    assert cache.event_evidence("evt-terminal-event", VenueName.MATCHBOOK).reason == "event_terminal"
    assert cache.market_records_for("evt-terminal-event") == ()


@pytest.mark.asyncio
async def test_provider_timeout_does_not_mark_the_fixture_unavailable() -> None:
    reset_opportunity_viability_cache()
    row = _siblings()[0]
    matchbook = FakeMatchbook()
    matchbook.hang.add(row.matchbook_market_id)
    engine, matchbook, _ks, _layer = _engine(
        [row],
        matchbook=matchbook,
        timeout=0.05,
        background_interval=0,
    )
    waited = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    cache = get_opportunity_viability_cache()
    assert row.catalogue_row_id in waited.retry_wait
    assert cache.is_blocked(CANONICAL, VenueName.MATCHBOOK) is False
    assert cache.market_is_blocked(CANONICAL, VenueName.MATCHBOOK, row.matchbook_market_id) is False
    assert cache.provider_issue(VenueName.MATCHBOOK) == "get_market_timeout"
    matchbook.hang.clear()
    matchbook.get_market_calls.clear()
    recovered = await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW + timedelta(seconds=30))
    assert row.matchbook_market_id in {market_id for _event, market_id in matchbook.get_market_calls}
    assert recovered.skip_reasons.get(row.catalogue_row_id) != CROSS_VENUE_UNAVAILABLE
    assert cache.is_blocked(CANONICAL, VenueName.MATCHBOOK) is False


def test_fresh_non_terminal_event_heals_stale_event_unavailability() -> None:
    reset_opportunity_viability_cache()
    cache = get_opportunity_viability_cache()
    cache.mark_unavailable("evt-heal", VenueName.MATCHBOOK, reason="event_unavailable")
    assert cache.is_blocked("evt-heal", VenueName.MATCHBOOK)
    kickoff = NOW + timedelta(days=2)
    matchbook = [
        _event(VenueName.MATCHBOOK, "mb-heal", home="Austria", away="Israel", kickoff=kickoff)
    ]
    matchbook[0].raw["status"] = "open"
    kalshi = [
        _event(VenueName.KALSHI, "k-heal", home="Austria", away="Israel", kickoff=kickoff)
    ]
    clusters, _counts = cluster_venue_events(
        matchbook=matchbook,
        polymarket=[],
        kalshi=kalshi,
        matcher=EventMatcher(),
        max_event_pairs=8,
    )
    assessment = assess_cluster_viability(
        clusters[0], canonical_event_id="evt-heal", cache=cache
    )
    assert assessment.skip_expensive_work is False
    assert cache.is_blocked("evt-heal", VenueName.MATCHBOOK) is False
    assert cache.event_evidence("evt-heal", VenueName.MATCHBOOK).reason == "event_current"
    assert cache.event_evidence("evt-heal", VenueName.MATCHBOOK).source_event_id == "mb-heal"


@pytest.mark.asyncio
async def test_hot_market_404_does_not_make_universe_skip_sibling_discovery() -> None:
    reset_opportunity_viability_cache()
    rows = _siblings()
    matchbook = FakeMatchbook()
    matchbook.gone.add(GONE_MARKET)
    engine, _mb, _ks, _layer = _engine(
        [rows[-1]],
        matchbook=matchbook,
        timeout=8,
        background_interval=0,
    )
    await engine.run_slice(PriceEnginePriority.BACKGROUND, now=NOW)
    cache = get_opportunity_viability_cache()
    cluster = _open_cluster()
    assessment = assess_cluster_viability(cluster, canonical_event_id=CANONICAL, cache=cache)
    assert cache.market_is_blocked(CANONICAL, VenueName.MATCHBOOK, GONE_MARKET)
    assert assessment.skip_expensive_work is False
    assert assessment.viable_venue_count >= 2
    healthy = assess_identity_viability(rows[0], cache=cache)
    assert healthy.skip_expensive_work is False


class _EmptyPolymarket:
    async def list_events(self, **filters):
        del filters
        return []

    async def list_markets(self, event_id, **filters):
        del event_id, filters
        return []

    async def get_order_book(self, event_id, market_id, outcome_id=None, **filters):
        del event_id, market_id, outcome_id, filters
        return {}


@pytest.mark.asyncio
async def test_universe_still_catalogues_siblings_when_one_market_is_gone() -> None:
    reset_opportunity_viability_cache()
    markets = [item for item in _mb_markets() if str(item["id"]) != "316010"]
    markets.append(_mb_handicap())
    matchbook = OverlapMatchbook([_mb_event()], {str(_mb_event()["id"]): markets})
    kalshi = OverlapKalshi(_kalshi_events(), series_by_ticker=_series_map())
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=_EmptyPolymarket(),
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(SqliteMarketIntelligenceRepository())),
        catalogue_store=store,
    )
    cache = get_opportunity_viability_cache()
    cache.mark_market_unavailable("pending", VenueName.MATCHBOOK, "316010", reason="market_gone")
    report = await collector.collect_and_scan(
        scan_lane="universe",
        enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
        venue_costs=_costs(),
        fx_snapshots=_fx(),
        maximum_execution_risk=100,
        max_event_pairs=8,
        unbounded_cycle=True,
    )
    rows = store.list_active()
    keys = {row.register_canonical_key for row in rows}
    assert CANONICAL_BTTS_FT in keys
    assert "TOTAL_GOALS_FT:2.5" in keys
    assert CANONICAL_MATCH_RESULT_FT not in keys
    assert "Asian Handicap" not in keys
    assert all(row.register_canonical_key in FOUR_KEYS for row in rows)
    fixture = next(
        item
        for item in report.discovered_fixtures
        if item.home_team == HOME and item.away_team == AWAY and item.competition == COMPETITION
    )
    assert fixture.market_evaluation_state != "cross_venue_unavailable"
    assert (fixture.matched_equivalent_count or 0) >= 2
    evidence = fixture.viability_evidence or {}
    matchbook_event = evidence["event_viability"]["matchbook"]
    assert matchbook_event["state"] == "viable"
    assert matchbook_event["evidence_scope"] == "event"
    assert evidence["final_reason"] != "cross_venue_unavailable"
    store.close()
