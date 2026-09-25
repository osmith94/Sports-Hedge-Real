from __future__ import annotations

import inspect
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from pydantic import ValidationError

from sports_hedge.api.paper import PaperCollectionRequest
from sports_hedge.application.collector import (
    DEFAULT_MAX_EVENT_PAIRS,
    MARKET_FETCH_UNAVAILABLE_REASON,
    MarketEvaluationState,
    ReadOnlyCrossVenueCollector,
    SCAN_BUDGET_EXHAUSTED_REASON,
    SCAN_FINALISATION_RESERVE_SECONDS,
    _is_baseline_match_result,
    _select_prioritized_market_pairs,
    finalisation_reserve_seconds,
)
from sports_hedge.application.live_refresh import SCAN_CYCLE_RETURN_GRACE_SECONDS
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.target_competitions import TargetCompetitionCode
from sports_hedge.config import Settings
from sports_hedge.domain.football import (
    CanonicalEvent,
    CanonicalMarket,
    CanonicalOutcome,
    CanonicalRunner,
    FootballPeriod,
    MarketFamily,
    SettlementFingerprint,
    SettlementScope,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.markets import MarketMatchResult, MarketMatcher
from sports_hedge.normalization.venues import (
    PolymarketNormalizer,
    promote_polymarket_complete_match_result,
)
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import matchbook_kalshi_costs, matchbook_polymarket_costs


KICKOFF = datetime(2026, 9, 15, 19, 0, tzinfo=UTC)
REGULATION = "This market resolves from the result after 90 minutes plus stoppage time."
EXTRA_TIME = "Winner including extra time and penalties."
KALSHI_SERIES = {
    "ticker": "KXLALIGA",
    "title": "La Liga",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "settlement_sources": [{"name": "Opta"}],
}


def _prices(back: str) -> list[dict[str, str]]:
    return [{"side": "back", "odds": back, "available-amount": "80"}]


def _pm_book(token: str) -> dict[str, Any]:
    now_ms = int(datetime.now(UTC).timestamp() * 1000)
    yes = token.startswith("yes") or token in {"h", "d", "a", "elche-yes", "draw-yes", "rm-yes"}
    return {
        "asset_id": token,
        "timestamp": now_ms - 120,
        "bids": [{"price": "0.49" if yes else "0.41", "size": "250"}],
        "asks": [{"price": "0.51" if yes else "0.43", "size": "250"}],
    }


def _moneyline_payload(
    market_id: str,
    question: str,
    yes_token: str,
    no_token: str,
    *,
    description: str = REGULATION,
) -> dict[str, Any]:
    return {
        "id": market_id,
        "question": question,
        "sportsMarketType": "moneyline",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": f'["{yes_token}", "{no_token}"]',
        "description": description,
        "feesEnabled": False,
    }


def _elche_pm_event() -> dict[str, Any]:
    return {
        "id": "pm-elche-rm",
        "title": "Elche CF vs. Real Madrid CF",
        "startTime": KICKOFF.isoformat(),
        "competition": "La Liga",
        "series": [{"id": 10193, "title": "La Liga"}],
    }


def _elche_mb_event() -> dict[str, Any]:
    return {
        "id": 14701,
        "name": "Elche CF vs Real Madrid CF",
        "start": KICKOFF.isoformat(),
        "competition-name": "La Liga",
    }


def _elche_match_odds(*, include_btts: bool = False) -> list[dict[str, Any]]:
    markets = [
        {
            "id": 9101,
            "name": "Both Teams To Score",
            "runners": [
                {"id": 11, "name": "Yes", "prices": _prices("1.90")},
                {"id": 12, "name": "No", "prices": _prices("1.95")},
            ],
        },
        {
            "id": 9102,
            "name": "Match Odds",
            "runners": [
                {"id": 1, "name": "Elche CF", "prices": _prices("6.50")},
                {"id": 2, "name": "Draw", "prices": _prices("4.80")},
                {"id": 3, "name": "Real Madrid CF", "prices": _prices("1.45")},
            ],
        },
    ]
    return markets if include_btts else markets[1:]


def _elche_pm_moneylines(*, include_draw: bool = True, draw_description: str = REGULATION) -> list[dict[str, Any]]:
    payloads = [
        _moneyline_payload(
            "pm-elche-win",
            "Will Elche CF win on 2026-09-15?",
            "elche-yes",
            "elche-no",
        ),
        _moneyline_payload(
            "pm-rm-win",
            "Will Real Madrid CF win on 2026-09-15?",
            "rm-yes",
            "rm-no",
        ),
    ]
    if include_draw:
        payloads.insert(
            1,
            _moneyline_payload(
                "pm-draw",
                "Will the match be a draw on 2026-09-15?",
                "draw-yes",
                "draw-no",
                description=draw_description,
            ),
        )
    return payloads


def _elche_kalshi_event() -> dict[str, Any]:
    return {
        "event_ticker": "KXLALIGA-26SEP15ELCREA",
        "series_ticker": "KXLALIGA",
        "title": "Elche CF vs Real Madrid CF",
        "category": "Sports",
        "strike_date": KICKOFF.isoformat(),
        "markets": [
            {
                "ticker": "KXLALIGA-26SEP15ELCREA-ELC",
                "event_ticker": "KXLALIGA-26SEP15ELCREA",
                "title": "Elche CF vs Real Madrid CF",
                "yes_sub_title": "Elche CF",
                "rules_primary": REGULATION,
            },
            {
                "ticker": "KXLALIGA-26SEP15ELCREA-DRAW",
                "event_ticker": "KXLALIGA-26SEP15ELCREA",
                "title": "Elche CF vs Real Madrid CF",
                "yes_sub_title": "Draw",
                "rules_primary": REGULATION,
            },
            {
                "ticker": "KXLALIGA-26SEP15ELCREA-REA",
                "event_ticker": "KXLALIGA-26SEP15ELCREA",
                "title": "Elche CF vs Real Madrid CF",
                "yes_sub_title": "Real Madrid CF",
                "rules_primary": REGULATION,
            },
        ],
    }


class ElcheMatchbook:
    def __init__(self, *, markets: list[dict[str, Any]] | None = None) -> None:
        self._markets = markets if markets is not None else _elche_match_odds(include_btts=True)

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {"events": [_elche_mb_event()]}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": self._markets}


class ElchePolymarket:
    def __init__(self, *, markets: list[dict[str, Any]] | None = None) -> None:
        self._markets = markets if markets is not None else _elche_pm_moneylines()
        self.book_calls: list[str] = []

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return [_elche_pm_event()]

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return list(self._markets)

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id, filters
        token = str(outcome_id)
        self.book_calls.append(token)
        return _pm_book(token)


class ElcheKalshi:
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {"events": [_elche_kalshi_event()]}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": []}

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, outcome_id, filters
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.18", "100.00"]],
                "no_dollars": [["0.80", "200.00"]],
            }
        }

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        ticker = str(series_ticker).upper()
        if ticker.startswith("KXLALIGA"):
            return KALSHI_SERIES
        return {
            "ticker": "KXEPLGAME",
            "title": "Premier League",
            "fee_type": "quadratic",
            "fee_multiplier": 1,
            "settlement_sources": [{"name": "Opta"}],
        }


class TwoFixtureMatchbook(ElcheMatchbook):
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        newcastle = {
            "id": 1001,
            "name": "Newcastle United vs Chelsea",
            "start": KICKOFF.isoformat(),
            "competition-name": "Premier League",
        }
        return {"events": [newcastle, _elche_mb_event()]}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        if str(event_id) == "1001":
            return {
                "markets": [
                    {
                        "id": 2001,
                        "name": "Match Odds",
                        "runners": [
                            {"id": 1, "name": "Newcastle United", "prices": _prices("2.10")},
                            {"id": 2, "name": "Draw", "prices": _prices("3.40")},
                            {"id": 3, "name": "Chelsea", "prices": _prices("3.60")},
                        ],
                    }
                ]
            }
        return {"markets": self._markets}


class TwoFixturePolymarket(ElchePolymarket):
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        newcastle = {
            "id": "pm-event-1",
            "title": "Newcastle United vs Chelsea",
            "startTime": KICKOFF.isoformat(),
            "competition": "Premier League",
            "series": [{"id": 10188, "title": "Premier League"}],
        }
        return [newcastle, _elche_pm_event()]

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del filters
        if str(event_id) == "pm-event-1":
            return [
                {
                    "id": "pm-1x2",
                    "question": "Match result?",
                    "sportsMarketType": "moneyline",
                    "outcomes": '["Newcastle United", "Draw", "Chelsea"]',
                    "clobTokenIds": '["h", "d", "a"]',
                    "description": REGULATION,
                    "feesEnabled": False,
                }
            ]
        return list(self._markets)


class TwoFixtureKalshi(ElcheKalshi):
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        newcastle = {
            "event_ticker": "KXEPLGAME-26SEP15NEWCHE",
            "series_ticker": "KXEPLGAME",
            "title": "Newcastle United vs Chelsea",
            "category": "Sports",
            "strike_date": KICKOFF.isoformat(),
            "markets": [
                {
                    "ticker": "KXEPLGAME-26SEP15NEWCHE-NEW",
                    "event_ticker": "KXEPLGAME-26SEP15NEWCHE",
                    "title": "Newcastle United vs Chelsea",
                    "yes_sub_title": "Newcastle United",
                    "rules_primary": REGULATION,
                },
                {
                    "ticker": "KXEPLGAME-26SEP15NEWCHE-DRAW",
                    "event_ticker": "KXEPLGAME-26SEP15NEWCHE",
                    "title": "Newcastle United vs Chelsea",
                    "yes_sub_title": "Draw",
                    "rules_primary": REGULATION,
                },
                {
                    "ticker": "KXEPLGAME-26SEP15NEWCHE-CHE",
                    "event_ticker": "KXEPLGAME-26SEP15NEWCHE",
                    "title": "Newcastle United vs Chelsea",
                    "yes_sub_title": "Chelsea",
                    "rules_primary": REGULATION,
                },
            ],
        }
        return {"events": [newcastle, _elche_kalshi_event()]}


def _costs() -> list[Any]:
    captured = datetime.now(UTC)
    return [
        *matchbook_polymarket_costs("0.02", "0", captured_at=captured),
        kalshi_cost_from_series(KALSHI_SERIES, captured_at=captured),
    ]


def _fx() -> list[FxRateSnapshot]:
    return [FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.80"))]


def _elche_row(report) -> Any:
    return next(
        item
        for item in report.discovered_fixtures
        if "elche" in item.home_team.lower() and "madrid" in item.away_team.lower()
    )


@pytest.mark.asyncio
async def test_deadline_leftover_three_venue_elche_is_not_evaluated() -> None:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=TwoFixtureMatchbook(),
        polymarket=TwoFixturePolymarket(),
        kalshi=TwoFixtureKalshi(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    scans = {"count": 0}
    original = collector._scan_cluster

    async def wrapped(*args: Any, **kwargs: Any):
        scans["count"] += 1
        return await original(*args, **kwargs)

    collector._scan_cluster = wrapped  # type: ignore[method-assign]
    collector._deadline_reached = lambda: scans["count"] >= 1  # type: ignore[method-assign]
    try:
        report = await collector.collect_and_scan(
            polymarket_discovery_now=KICKOFF,
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
        )
        leftover = _elche_row(report)
        assert leftover.matchbook_matched is True
        assert leftover.polymarket_matched is True
        assert leftover.kalshi_matched is True
        assert leftover.market_evaluation_state == MarketEvaluationState.NOT_EVALUATED_SCAN_DEADLINE
        assert leftover.market_evaluation_reason == SCAN_BUDGET_EXHAUSTED_REASON
        assert leftover.matched_equivalent_count is None
        assert leftover.opportunity_state == "not_evaluated"
        assert leftover.no_comparison_reason == "not_evaluated_scan_deadline"
        assert leftover.best_arb_market is None
        assert leftover.current_net_edge is None
        assert leftover.execution_risk_score is None
        assert any(issue.detail == "scan_cycle_deadline_reached" for issue in report.issues)
        evaluated = next(item for item in report.discovered_fixtures if item is not leftover)
        assert evaluated.market_evaluation_state == MarketEvaluationState.EVALUATED
        assert evaluated.matched_equivalent_count is not None
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_evaluated_zero_equivalents_is_not_the_unevaluated_state() -> None:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=ElcheMatchbook(markets=_elche_match_odds()),
        polymarket=ElchePolymarket(
            markets=[
                {
                    "id": "pm-btts",
                    "question": "Both teams to score?",
                    "sportsMarketType": "both teams to score",
                    "outcomes": '["Yes", "No"]',
                    "clobTokenIds": '["yes-token", "no-token"]',
                    "description": REGULATION,
                    "feesEnabled": False,
                }
            ]
        ),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        report = await collector.collect_and_scan(
            polymarket_discovery_now=KICKOFF,
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
        )
        fixture = _elche_row(report)
        assert fixture.matchbook_matched is True
        assert fixture.polymarket_matched is True
        assert fixture.market_evaluation_state == MarketEvaluationState.EVALUATED
        assert fixture.matched_equivalent_count == 0
        assert fixture.opportunity_state == "unmatched"
        assert fixture.no_comparison_reason == "no_normalized_market_family_overlap"
        assert fixture.market_evaluation_reason is None
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_elche_regulation_match_result_yields_equivalent_pair() -> None:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=ElcheMatchbook(markets=_elche_match_odds()),
        polymarket=ElchePolymarket(markets=_elche_pm_moneylines()),
        kalshi=ElcheKalshi(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        report = await collector.collect_and_scan(
            polymarket_discovery_now=KICKOFF,
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
        )
        fixture = _elche_row(report)
        assert fixture.market_evaluation_state == MarketEvaluationState.EVALUATED
        assert fixture.matched_equivalent_count >= 1
        assert fixture.opportunity_state in {"matched", "near", "qualifying"}
        families = {
            row.family
            for rows in report.fixture_markets.values()
            for row in rows
            if row.comparison_status == "matched_equivalent"
        }
        assert "match_result" in families
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_baseline_match_result_is_kept_when_pair_cap_is_one() -> None:
    repository = SqliteMarketIntelligenceRepository()
    pm_markets = _elche_pm_moneylines() + [
        {
            "id": "pm-btts",
            "question": "Both teams to score?",
            "sportsMarketType": "both teams to score",
            "outcomes": '["Yes", "No"]',
            "clobTokenIds": '["yes-token", "no-token"]',
            "description": REGULATION,
            "feesEnabled": False,
        }
    ]
    collector = ReadOnlyCrossVenueCollector(
        matchbook=ElcheMatchbook(markets=_elche_match_odds(include_btts=True)),
        polymarket=ElchePolymarket(markets=pm_markets),
        kalshi=ElcheKalshi(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        report = await collector.collect_and_scan(
            polymarket_discovery_now=KICKOFF,
            venue_costs=matchbook_kalshi_costs() + matchbook_polymarket_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
            max_market_pairs_per_event=1,
        )
        fixture = _elche_row(report)
        assert fixture.market_evaluation_state == MarketEvaluationState.EVALUATED
        assert fixture.matched_equivalent_count >= 1
        assert fixture.market_family == MarketFamily.MATCH_RESULT
        assert report.paper_decisions
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_list_markets_timeout_is_market_fetch_unavailable_not_equivalent_zero() -> None:
    from test_scan_timeout import HungMarketsMatchbook
    from test_read_only_collector import FakePolymarket

    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=HungMarketsMatchbook(),
        polymarket=FakePolymarket(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        venue_timeout_seconds=1.0,
        provider_call_timeout_seconds=0.2,
        cycle_timeout_seconds=2.0,
    )
    try:
        report = await collector.collect_and_scan(
            polymarket_discovery_now=KICKOFF,
            venue_costs=matchbook_polymarket_costs("0.02", "0.02"),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
        )
        two_venue = next(
            item
            for item in report.discovered_fixtures
            if item.matchbook_matched and item.polymarket_matched
        )
        assert two_venue.market_evaluation_state == MarketEvaluationState.MARKET_FETCH_UNAVAILABLE
        assert two_venue.matched_equivalent_count is None
        assert two_venue.opportunity_state == "not_evaluated"
        assert two_venue.no_comparison_reason == MARKET_FETCH_UNAVAILABLE_REASON
    finally:
        repository.close()


def test_polymarket_elche_yes_contracts_promote_only_when_exhaustive() -> None:
    normalizer = PolymarketNormalizer()
    event = normalizer.normalize_event(_elche_pm_event())
    payloads = _elche_pm_moneylines()
    markets = [normalizer.normalize_market(event, payload) for payload in payloads]
    promoted = promote_polymarket_complete_match_result(markets, payloads)
    assert len(promoted) == 1
    assert [runner.outcome for runner in promoted[0].runners] == [
        CanonicalOutcome.HOME,
        CanonicalOutcome.DRAW,
        CanonicalOutcome.AWAY,
    ]
    assert _is_baseline_match_result(promoted[0]) is True


def test_incomplete_elche_yes_set_stays_fail_closed_binaries() -> None:
    normalizer = PolymarketNormalizer()
    event = normalizer.normalize_event(_elche_pm_event())
    payloads = _elche_pm_moneylines(include_draw=False)
    markets = [normalizer.normalize_market(event, payload) for payload in payloads]
    promoted = promote_polymarket_complete_match_result(markets, payloads)
    assert len(promoted) == 2
    for market in promoted:
        assert {runner.outcome for runner in market.runners} == {
            CanonicalOutcome.YES,
            CanonicalOutcome.NO,
        }
        assert _is_baseline_match_result(market) is False


def test_settlement_mismatch_elche_yes_set_stays_fail_closed() -> None:
    normalizer = PolymarketNormalizer()
    event = normalizer.normalize_event(_elche_pm_event())
    payloads = _elche_pm_moneylines(draw_description=EXTRA_TIME)
    markets = [normalizer.normalize_market(event, payload) for payload in payloads]
    promoted = promote_polymarket_complete_match_result(markets, payloads)
    assert len(promoted) == 3
    fingerprints = {market.settlement.deterministic_key() for market in promoted}
    assert len(fingerprints) == 2
    assert all(not _is_baseline_match_result(market) for market in promoted)


def _canonical_market(
    *,
    family: MarketFamily,
    source_id: str,
    outcomes: tuple[CanonicalOutcome, ...],
    extra_time: bool = False,
) -> CanonicalMarket:
    event = CanonicalEvent(
        competition="La Liga",
        home_team="Elche CF",
        away_team="Real Madrid CF",
        kickoff_utc=KICKOFF,
        source_venue=VenueName.MATCHBOOK if source_id.startswith("mb") else VenueName.POLYMARKET,
        source_event_id="evt",
    )
    settlement = SettlementFingerprint(
        scope=SettlementScope.INCLUDING_EXTRA_TIME if extra_time else SettlementScope.REGULATION_TIME,
        period=FootballPeriod.FULL_TIME,
        extra_time_included=extra_time,
        penalties_included=extra_time,
        push_possible=False,
    )
    return CanonicalMarket(
        event=event,
        source_venue=event.source_venue,
        source_market_id=source_id,
        family=family,
        period=FootballPeriod.FULL_TIME,
        settlement=settlement,
        runners=[
            CanonicalRunner(source_runner_id=f"{source_id}-{outcome.value}", outcome=outcome, label=outcome.value)
            for outcome in outcomes
        ],
    )


def test_select_prioritized_pairs_keeps_match_result_under_cap() -> None:
    class _Wrap:
        def __init__(self, canonical: CanonicalMarket) -> None:
            self.canonical = canonical

    btts_left = _Wrap(
        _canonical_market(
            family=MarketFamily.BOTH_TEAMS_TO_SCORE,
            source_id="mb-btts",
            outcomes=(CanonicalOutcome.YES, CanonicalOutcome.NO),
        )
    )
    btts_right = _Wrap(
        _canonical_market(
            family=MarketFamily.BOTH_TEAMS_TO_SCORE,
            source_id="pm-btts",
            outcomes=(CanonicalOutcome.YES, CanonicalOutcome.NO),
        )
    )
    mr_left = _Wrap(
        _canonical_market(
            family=MarketFamily.MATCH_RESULT,
            source_id="mb-1x2",
            outcomes=(CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY),
        )
    )
    mr_right = _Wrap(
        _canonical_market(
            family=MarketFamily.MATCH_RESULT,
            source_id="pm-1x2",
            outcomes=(CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY),
        )
    )
    match = MarketMatchResult(matched=True, confidence=1.0, reasons=[])
    pairs = [
        (btts_left, btts_right, match),
        (mr_left, mr_right, match),
    ]
    selected = _select_prioritized_market_pairs(pairs, 1)
    assert len(selected) == 1
    assert selected[0][0].canonical.family is MarketFamily.MATCH_RESULT


def test_baseline_match_result_rejects_extra_time_and_incomplete_sets() -> None:
    complete = _canonical_market(
        family=MarketFamily.MATCH_RESULT,
        source_id="mb-1x2",
        outcomes=(CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY),
    )
    extra = _canonical_market(
        family=MarketFamily.MATCH_RESULT,
        source_id="mb-et",
        outcomes=(CanonicalOutcome.HOME, CanonicalOutcome.DRAW, CanonicalOutcome.AWAY),
        extra_time=True,
    )
    binary = _canonical_market(
        family=MarketFamily.MATCH_RESULT,
        source_id="pm-home",
        outcomes=(CanonicalOutcome.YES, CanonicalOutcome.NO),
    )
    assert _is_baseline_match_result(complete) is True
    assert _is_baseline_match_result(extra) is False
    assert _is_baseline_match_result(binary) is False
    assert MarketMatcher().match(complete, extra).matched is False


THREE_LEAGUE_FIXTURES: list[tuple[str, str, str]] = [
    ("Premier League", "Arsenal", "Chelsea"),
    ("Premier League", "Liverpool", "Manchester City"),
    ("Premier League", "Newcastle United", "Tottenham Hotspur"),
    ("Premier League", "Aston Villa", "Brighton and Hove Albion"),
    ("Premier League", "Everton", "Fulham"),
    ("Premier League", "West Ham United", "Wolverhampton Wanderers"),
    ("Premier League", "Crystal Palace", "Brentford"),
    ("Premier League", "Bournemouth", "Nottingham Forest"),
    ("Premier League", "Manchester United", "Southampton"),
    ("Premier League", "Fulham", "Everton"),
    ("Championship", "Leeds United", "Leicester City"),
    ("Championship", "Burnley", "Sheffield United"),
    ("Championship", "Sunderland", "Coventry City"),
    ("Championship", "Middlesbrough", "Norwich City"),
    ("Championship", "West Bromwich Albion", "Hull City"),
    ("Championship", "Stoke City", "Derby County"),
    ("Championship", "Queens Park Rangers", "Swansea City"),
    ("Championship", "Bristol City", "Preston North End"),
    ("Championship", "Blackburn Rovers", "Watford"),
    ("Championship", "Oxford United", "Portsmouth"),
    ("La Liga", "Elche CF", "Real Madrid CF"),
    ("La Liga", "Athletic Club", "Barcelona"),
    ("La Liga", "Atletico Madrid", "Sevilla"),
    ("La Liga", "Real Sociedad", "Real Betis"),
    ("La Liga", "Villarreal", "Valencia"),
    ("La Liga", "Girona", "Osasuna"),
    ("La Liga", "Celta Vigo", "Getafe"),
    ("La Liga", "Mallorca", "Rayo Vallecano"),
    ("La Liga", "Espanyol", "Deportivo Alaves"),
    ("La Liga", "Las Palmas", "Leganes"),
]


def _btts_matchbook_market(market_id: int) -> dict[str, Any]:
    return {
        "id": market_id,
        "name": "Both Teams To Score",
        "runners": [
            {"id": market_id * 10 + 1, "name": "Yes", "prices": _prices("2.20")},
            {"id": market_id * 10 + 2, "name": "No", "prices": _prices("1.80")},
        ],
    }


def _btts_polymarket_market(index: int) -> dict[str, Any]:
    yes_token = f"yes-{index}"
    no_token = f"no-{index}"
    return {
        "id": f"pm-btts-{index}",
        "question": "Both teams to score?",
        "sportsMarketType": "both teams to score",
        "outcomes": '["Yes", "No"]',
        "clobTokenIds": f'["{yes_token}", "{no_token}"]',
        "description": REGULATION,
        "feesEnabled": False,
    }


class ThreeLeagueResponsiveMatchbook:
    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {
            "events": [
                {
                    "id": 20000 + index,
                    "name": f"{home} vs {away}",
                    "start": KICKOFF.isoformat(),
                    "competition-name": competition,
                }
                for index, (competition, home, away) in enumerate(THREE_LEAGUE_FIXTURES)
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        return {"markets": [_btts_matchbook_market(int(event_id))]}


class ThreeLeagueResponsivePolymarket:
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return [
            {
                "id": f"pm-{index}",
                "title": f"{home} vs {away}",
                "startTime": KICKOFF.isoformat(),
                "competition": competition,
                "series": [{"id": "10188" if competition == "Premier League" else "10355" if competition == "Championship" else "10193", "title": competition}],
            }
            for index, (competition, home, away) in enumerate(THREE_LEAGUE_FIXTURES)
        ]

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del filters
        index = int(str(event_id).removeprefix("pm-"))
        return [_btts_polymarket_market(index)]

    async def get_order_book(
        self,
        event_id: int | str,
        market_id: int | str,
        outcome_id: int | str | None = None,
        **filters: Any,
    ) -> dict[str, Any]:
        del event_id, market_id, filters
        return _pm_book(str(outcome_id))


def test_collect_default_event_capacity_is_sixty_and_timeouts_stay_bounded() -> None:
    collect_default = inspect.signature(
        ReadOnlyCrossVenueCollector.collect_and_scan
    ).parameters["max_event_pairs"].default
    init_defaults = inspect.signature(ReadOnlyCrossVenueCollector.__init__).parameters
    assert collect_default == DEFAULT_MAX_EVENT_PAIRS == 60
    assert PaperCollectionRequest().max_event_pairs == 60
    assert PaperCollectionRequest(max_event_pairs=100).max_event_pairs == 100
    with pytest.raises(ValidationError):
        PaperCollectionRequest(max_event_pairs=101)
    assert init_defaults["cycle_timeout_seconds"].default == 45.0
    assert init_defaults["venue_timeout_seconds"].default == 15.0
    assert init_defaults["provider_call_timeout_seconds"].default == 8.0
    assert Settings.model_fields["paper_scan_cycle_timeout_seconds"].default == 45
    assert Settings.model_fields["paper_scan_venue_timeout_seconds"].default == 15
    assert Settings.model_fields["paper_scan_provider_timeout_seconds"].default == 8
    assert SCAN_CYCLE_RETURN_GRACE_SECONDS == 5.0
    assert SCAN_FINALISATION_RESERVE_SECONDS == 4.0
    assert finalisation_reserve_seconds(45) == 4.0
    assert finalisation_reserve_seconds(45) + SCAN_CYCLE_RETURN_GRACE_SECONDS == 9.0


@pytest.mark.asyncio
async def test_thirty_three_league_fixtures_are_not_truncated_by_event_pair_capacity() -> None:
    assert len(THREE_LEAGUE_FIXTURES) == 30
    assert len({item[0] for item in THREE_LEAGUE_FIXTURES}) == 3
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=ThreeLeagueResponsiveMatchbook(),
        polymarket=ThreeLeagueResponsivePolymarket(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        report = await collector.collect_and_scan(
            polymarket_discovery_now=KICKOFF,
            venue_costs=matchbook_polymarket_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
        )
        assert len(report.discovered_fixtures) == 30
        by_league = {}
        for item in report.discovered_fixtures:
            by_league[item.target_competition_code] = by_league.get(item.target_competition_code, 0) + 1
            assert item.market_evaluation_state == MarketEvaluationState.EVALUATED
            assert item.matchbook_matched is True
            assert item.polymarket_matched is True
        assert by_league == {
            TargetCompetitionCode.PREMIER_LEAGUE.value: 10,
            TargetCompetitionCode.CHAMPIONSHIP.value: 10,
            TargetCompetitionCode.LA_LIGA.value: 10,
        }
        assert report.matched_event_pairs == 30
        assert all(
            issue.detail != "scan_cycle_deadline_reached" for issue in report.issues
        )
    finally:
        repository.close()
