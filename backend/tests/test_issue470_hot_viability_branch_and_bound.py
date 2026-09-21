"""#470 HOT viability + conservative branch-and-bound pruning.

Data class: synthetic/fixture providers. Not live quotes.
PAPER / read-only. No concurrency increase. No threshold changes.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from typing import Any

import pytest
from test_dual_cadence_scheduler import FakeClock
from test_issue344_price_engine import (
    NEAR_KICKOFF,
    NOW,
    FakeKalshi,
    FakeMatchbook,
    StubPaperScan,
    _engine,
    _hda_row,
    _mb_match_odds,
    _runner,
)
from test_issue466_universe_indexed_clustering import _event

from sports_hedge.application.collector import MarketEvaluationState
from sports_hedge.application.fixture_clusters import VenueEvent, cluster_venue_events
from sports_hedge.application.opportunity_viability import (
    CROSS_VENUE_UNAVAILABLE,
    NO_CROSS_VENUE_CANDIDATE,
    assess_cluster_viability,
    assess_identity_viability,
    get_opportunity_viability_cache,
    reset_opportunity_viability_cache,
)
from sports_hedge.application.price_engine import (
    PriceEngineItemStatus,
    PriceEnginePriority,
    PriceEngineSliceResult,
)
from sports_hedge.application.provider_access import PRICE_ENGINE_ACTIVE_TRADE_LANE
from sports_hedge.application.scan_lanes import ScanLane, classify_scan_lane, is_explicit_terminal
from sports_hedge.arbitrage.arb_upper_bound import optimistic_net_edge_upper_bound
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.events import EventMatcher

POST_KICKOFF = NOW + timedelta(hours=1)


@pytest.fixture(autouse=True)
def _reset_viability_cache() -> Any:
    reset_opportunity_viability_cache()
    yield
    reset_opportunity_viability_cache()


def _expensive_kalshi_book() -> dict[str, Any]:
    return {
        "orderbook_fp": {
            "yes_dollars": [["0.001", "100.00"]],
            "no_dollars": [["0.001", "100.00"]],
        }
    }


def _cheap_kalshi_book() -> dict[str, Any]:
    return {
        "orderbook_fp": {
            "yes_dollars": [["0.40", "100.00"]],
            "no_dollars": [["0.49", "200.00"]],
        }
    }


def _named_match_odds(market_id: int, *, odds: str = "1.01") -> dict[str, Any]:
    return {
        "id": market_id,
        "name": "Match Odds",
        "status": "open",
        "runners": [
            _runner(market_id * 10 + 1, "Home", odds),
            _runner(market_id * 10 + 2, "Draw", odds),
            _runner(market_id * 10 + 3, "Away", odds),
        ],
    }


class PayloadKalshi(FakeKalshi):
    def __init__(self) -> None:
        super().__init__()
        self.payloads: dict[str, dict[str, Any]] = {}

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        ticker = str(market_id)
        if ticker in self.payloads:
            self.book_calls.append(ticker)
            if self.access is not None:
                snap = self.access.snapshot()
                self.inflight_matchbook_during_book.append(
                    snap.inflight.get(VenueName.MATCHBOOK.value, 0)
                )
            return self.payloads[ticker]
        return await super().get_order_book(event_id, market_id, outcome_id, **filters)


def test_lifecycle_classifier_does_not_infer_finished_from_elapsed_time() -> None:
    fixture = type(
        "Fix",
        (),
        {
            "fixture_status": None,
            "fixture_status_source": None,
            "in_running": None,
            "kickoff_utc": NOW - timedelta(hours=2),
        },
    )()
    assert classify_scan_lane(fixture, NOW) is ScanLane.HOT
    assert is_explicit_terminal(fixture) is False


def test_upper_bound_prunes_only_when_even_free_unknown_legs_miss_min_net() -> None:
    prune = optimistic_net_edge_upper_bound(
        required_outcomes=["home", "draw", "away"],
        known_implied={"home": Decimal("0.50"), "draw": Decimal("0.50")},
        unknown_outcomes=["away"],
        minimum_net_edge=Decimal("0.01"),
    )
    assert prune.prune is True
    assert prune.upper_bound_net_edge is not None
    assert prune.upper_bound_net_edge < Decimal("0.01")

    cont = optimistic_net_edge_upper_bound(
        required_outcomes=["home", "draw", "away"],
        known_implied={"home": Decimal("0.33"), "draw": Decimal("0.33")},
        unknown_outcomes=["away"],
        minimum_net_edge=Decimal("0.01"),
    )
    assert cont.prune is False
    assert cont.action == "continue"

    uncertain = optimistic_net_edge_upper_bound(
        required_outcomes=["home", "draw", "away"],
        known_implied={"home": Decimal("0.50")},
        unknown_outcomes=[],
        minimum_net_edge=Decimal("0.01"),
    )
    assert uncertain.certain is False
    assert uncertain.action == "uncertain"


def test_one_venue_pre_match_is_not_a_cross_venue_candidate() -> None:
    matchbook = [
        _event(
            VenueName.MATCHBOOK,
            "mb-one",
            home="Brentford",
            away="Chelsea",
            kickoff=NEAR_KICKOFF,
        )
    ]
    clusters, _counts = cluster_venue_events(
        matchbook=matchbook,
        polymarket=[],
        kalshi=[],
        matcher=EventMatcher(),
        max_event_pairs=8,
    )
    assert len(clusters) == 1
    assert clusters[0].venue_count == 1
    assessment = assess_cluster_viability(
        clusters[0],
        canonical_event_id="evt-one",
        active_event_ids=frozenset(),
    )
    assert assessment.skip_expensive_work is True
    assert assessment.reason == NO_CROSS_VENUE_CANDIDATE
    assert assessment.viable_venue_count == 1


def test_cluster_matchbook_closed_kalshi_listed_is_cross_venue_unavailable() -> None:
    kickoff = POST_KICKOFF - timedelta(hours=1)
    mb = VenueEvent(
        venue=VenueName.MATCHBOOK,
        raw={"id": "mb-closed", "status": "closed", "title": "Brentford vs Chelsea"},
        canonical=CanonicalEvent(
            sport="football",
            competition="Premier League",
            home_team="Brentford",
            away_team="Chelsea",
            kickoff_utc=kickoff,
            source_venue=VenueName.MATCHBOOK,
            source_event_id="mb-closed",
        ),
        source_event_id="mb-closed",
    )
    kalshi = VenueEvent(
        venue=VenueName.KALSHI,
        raw={"id": "k-still", "status": "open", "title": "Brentford vs Chelsea"},
        canonical=CanonicalEvent(
            sport="football",
            competition="Premier League",
            home_team="Brentford",
            away_team="Chelsea",
            kickoff_utc=kickoff,
            source_venue=VenueName.KALSHI,
            source_event_id="k-still",
        ),
        source_event_id="k-still",
    )
    clusters, _counts = cluster_venue_events(
        matchbook=[mb],
        polymarket=[],
        kalshi=[kalshi],
        matcher=EventMatcher(),
        max_event_pairs=8,
    )
    assert len(clusters) == 1
    assert clusters[0].venue_count == 2
    assessment = assess_cluster_viability(
        clusters[0],
        canonical_event_id="evt-closed-open",
    )
    assert assessment.skip_expensive_work is True
    assert assessment.reason == CROSS_VENUE_UNAVAILABLE
    assert assessment.viable_venue_count == 1
    fixture_like = type(
        "Fix",
        (),
        {
            "fixture_status": None,
            "in_running": None,
            "kickoff_utc": kickoff,
        },
    )()
    assert classify_scan_lane(fixture_like, POST_KICKOFF) is ScanLane.HOT
    assert is_explicit_terminal(fixture_like) is False


@pytest.mark.asyncio
async def test_matchbook_terminal_kalshi_still_listed_skips_subsequent_hot_books() -> None:
    row = _hda_row("gone1", kickoff=POST_KICKOFF)
    mb = FakeMatchbook()
    mb.gone.add(str(row.matchbook_market_id))
    kalshi = PayloadKalshi()
    engine, mb, kalshi, _layer = _engine(
        [row],
        matchbook=mb,
        kalshi=kalshi,
        clock=FakeClock(POST_KICKOFF),
        hot_interval=0,
    )
    first = await engine.run_slice(PriceEnginePriority.HOT)
    assert mb.get_market_calls
    assert kalshi.book_calls == []
    assert row.catalogue_row_id in first.revalidation
    cache = get_opportunity_viability_cache()
    assert cache.is_blocked(row.canonical_event_id, VenueName.MATCHBOOK)

    mb.get_market_calls.clear()
    second = await engine.run_slice(PriceEnginePriority.HOT)
    assert mb.get_market_calls == []
    assert kalshi.book_calls == []
    assert row.catalogue_row_id in second.skipped
    assert second.skip_reasons[row.catalogue_row_id] == CROSS_VENUE_UNAVAILABLE
    assert second.saved_provider_calls >= 1
    diag = second.viability_diagnostics()
    assert diag["skip_reason"] == CROSS_VENUE_UNAVAILABLE
    assert diag["viable_venue_count"] == 1
    assert diag["saved_provider_calls"] >= 1
    shown = engine.fixture_state.detail(row.canonical_event_id)
    assert shown is not None
    fixture = shown.fixture
    assert fixture.market_evaluation_state == MarketEvaluationState.CROSS_VENUE_UNAVAILABLE.value
    assert fixture.fixture_status is None
    assert is_explicit_terminal(fixture) is False


@pytest.mark.asyncio
async def test_open_active_trade_overrides_viability_prune() -> None:
    row = _hda_row("active1", kickoff=POST_KICKOFF)
    cache = get_opportunity_viability_cache()
    cache.mark_unavailable(row.canonical_event_id, VenueName.MATCHBOOK)
    identity_like = type(
        "Ident",
        (),
        {
            "canonical_event_id": row.canonical_event_id,
            "matchbook_event_id": row.matchbook_event_id,
            "matchbook_market_id": row.matchbook_market_id,
            "kalshi_event_ticker": row.kalshi_event_ticker,
            "kalshi_market_tickers": list(row.kalshi_market_tickers),
            "kalshi_outcome_ids": list(row.kalshi_outcome_ids),
            "polymarket_event_id": None,
            "polymarket_market_id": None,
            "polymarket_token_ids": [],
        },
    )()
    blocked = assess_identity_viability(identity_like, cache=cache)
    assert blocked.skip_expensive_work is True
    override = assess_identity_viability(
        identity_like, cache=cache, active_trade_lane=True
    )
    assert override.skip_expensive_work is False

    engine, mb, kalshi, _layer = _engine(
        [row],
        clock=FakeClock(POST_KICKOFF),
        hot_interval=0,
    )
    engine.set_operator_scope(None, exempt_event_ids={row.canonical_event_id})
    runtime = engine.item(row.catalogue_row_id)
    assert runtime is not None
    result = PriceEngineSliceResult()
    status = await engine._price_item(
        runtime, result, lane=PRICE_ENGINE_ACTIVE_TRADE_LANE
    )
    assert status is not PriceEngineItemStatus.SKIPPED
    assert mb.get_market_calls
    assert kalshi.book_calls


@pytest.mark.asyncio
async def test_near_threshold_bound_continues_remaining_kalshi_books() -> None:
    row = _hda_row("near1", kickoff=NEAR_KICKOFF)
    mb = FakeMatchbook()
    mb.payloads[str(row.matchbook_market_id)] = _mb_match_odds(int(row.matchbook_market_id))
    kalshi = PayloadKalshi()
    for ticker in row.kalshi_market_tickers:
        kalshi.payloads[ticker] = _cheap_kalshi_book()
    engine, mb, kalshi, _layer = _engine(
        [row],
        matchbook=mb,
        kalshi=kalshi,
        clock=FakeClock(NEAR_KICKOFF),
        hot_interval=0,
        paper_scan=StubPaperScan(),
    )
    result = await engine.run_slice(PriceEnginePriority.HOT)
    assert row.catalogue_row_id not in result.skipped
    assert result.skip_reasons.get(row.catalogue_row_id) != "upper_bound_below_min_net"
    assert set(kalshi.book_calls) == set(row.kalshi_market_tickers)
    if result.upper_bound_net_edge is not None:
        assert Decimal(str(result.upper_bound_net_edge)) >= Decimal("0.01")


@pytest.mark.asyncio
async def test_bound_below_min_net_stops_remaining_kalshi_tickers() -> None:
    row = _hda_row("bound1", kickoff=NEAR_KICKOFF)
    mb = FakeMatchbook()
    mb.payloads[str(row.matchbook_market_id)] = _named_match_odds(
        int(row.matchbook_market_id), odds="1.01"
    )
    kalshi = PayloadKalshi()
    for ticker in row.kalshi_market_tickers:
        kalshi.payloads[ticker] = _expensive_kalshi_book()
    engine, mb, kalshi, _layer = _engine(
        [row],
        matchbook=mb,
        kalshi=kalshi,
        clock=FakeClock(NEAR_KICKOFF),
        hot_interval=0,
        paper_scan=StubPaperScan(),
    )
    result = await engine.run_slice(PriceEnginePriority.HOT)
    assert row.catalogue_row_id in result.skipped
    assert result.skip_reasons[row.catalogue_row_id] == "upper_bound_below_min_net"
    assert 0 < len(kalshi.book_calls) < len(row.kalshi_market_tickers)
    assert result.saved_provider_calls >= 1
    assert result.upper_bound_net_edge is not None
    diag = result.viability_diagnostics()
    assert diag["skip_reason"] == "upper_bound_below_min_net"
    assert Decimal(str(diag["upper_bound_net_edge"])) < Decimal("0.01")
