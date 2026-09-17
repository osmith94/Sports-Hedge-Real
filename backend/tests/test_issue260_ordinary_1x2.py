"""Ordinary Matchbook↔Kalshi 1X2 operational matching after contract-terms outcome B.

Fixture/demo only. Does not infer regulation from GAME/Opta/title.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.mapping_census import CENSUS_DATA_CLASS_FIXTURE, census_from_report
from sports_hedge.application.mapping_forensics import (
    VENUE_SCOPE_UNIVERSE,
    forensics_from_report,
    render_forensics,
)
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.application.universe_mapping_census import (
    OWNER_LIVE_CENSUS_MATCHBOOK_KALSHI_ONLY_ENV,
    resolve_census_venue_scope,
)
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
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.matching.ordinary_1x2 import (
    UNKNOWN_SETTLEMENT_ALLOWED_REASON,
    allow_unknown_settlement_for_ordinary_1x2,
)
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import matchbook_polymarket_costs

KICKOFF = datetime(2026, 9, 20, 19, 0, tzinfo=UTC)
GAMEWIN_URL = "https://assets.kalshi.com/contract_terms/SOCCERGAMEWIN.pdf"
KALSHI_GAME_SERIES = {
    "ticker": "KXEPLGAME",
    "title": "Premier League",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "settlement_sources": [{"name": "Opta"}],
    "contract_terms_url": GAMEWIN_URL,
}

FIXTURES = (
    ("9600", "Real Betis", "Getafe", "BETGET"),
    ("9700", "Monza", "Sassuolo", "MONSAS"),
    ("9800", "Brentford", "Chelsea", "BRECHE"),
)


def _fx() -> list[FxRateSnapshot]:
    return [FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test")]


def _costs():
    captured = datetime.now(UTC)
    return [
        *matchbook_polymarket_costs("0.02", "0.02", captured_at=captured),
        kalshi_cost_from_series(KALSHI_GAME_SERIES, captured_at=captured),
    ]


def _event(venue: VenueName, home: str, away: str, source_id: str) -> CanonicalEvent:
    return CanonicalEvent(
        competition="Premier League",
        home_team=home,
        away_team=away,
        kickoff_utc=KICKOFF,
        source_venue=venue,
        source_event_id=source_id,
    )


def _runners(*outcomes: CanonicalOutcome) -> list[CanonicalRunner]:
    return [
        CanonicalRunner(source_runner_id=outcome.value, outcome=outcome, label=outcome.value)
        for outcome in outcomes
    ]


def _market(
    venue: VenueName,
    *,
    home: str = "Real Betis",
    away: str = "Getafe",
    source_id: str = "src",
    family: MarketFamily = MarketFamily.MATCH_RESULT,
    period: FootballPeriod = FootballPeriod.FULL_TIME,
    settlement: SettlementFingerprint | None = None,
    outcomes: tuple[CanonicalOutcome, ...] = (
        CanonicalOutcome.HOME,
        CanonicalOutcome.DRAW,
        CanonicalOutcome.AWAY,
    ),
    confidence: float = 1.0,
) -> CanonicalMarket:
    fingerprint = settlement or SettlementFingerprint(
        scope=SettlementScope.REGULATION_TIME,
        period=period,
        extra_time_included=False,
        penalties_included=False,
        push_possible=False,
    )
    return CanonicalMarket(
        event=_event(venue, home, away, source_id),
        source_venue=venue,
        source_market_id=f"{source_id}-{family.value}",
        family=family,
        period=period,
        settlement=fingerprint,
        runners=_runners(*outcomes),
        confidence=confidence,
    )


def _unknown_kalshi(**kwargs: Any) -> CanonicalMarket:
    return _market(
        VenueName.KALSHI,
        settlement=SettlementFingerprint(
            scope=SettlementScope.UNKNOWN,
            period=FootballPeriod.FULL_TIME,
            extra_time_included=None,
            penalties_included=None,
            push_possible=False,
        ),
        confidence=0.75,
        **kwargs,
    )


def test_mb_regulation_and_kalshi_unknown_ordinary_1x2_match() -> None:
    mb = _market(VenueName.MATCHBOOK)
    kalshi = _unknown_kalshi()
    result = MarketMatcher().match(mb, kalshi)
    assert result.matched is True
    assert UNKNOWN_SETTLEMENT_ALLOWED_REASON in result.reasons
    assert result.confidence >= 0.98
    assert allow_unknown_settlement_for_ordinary_1x2(mb, kalshi) is True


def test_polymarket_unknown_1x2_still_fails_closed() -> None:
    mb = _market(VenueName.MATCHBOOK)
    polymarket = _market(
        VenueName.POLYMARKET,
        settlement=SettlementFingerprint(
            scope=SettlementScope.UNKNOWN,
            period=FootballPeriod.FULL_TIME,
            extra_time_included=None,
            penalties_included=None,
            push_possible=False,
        ),
    )
    result = MarketMatcher().match(mb, polymarket)
    assert result.matched is False
    assert "incomplete_settlement" in result.reasons


def test_to_qualify_and_two_way_and_first_half_reject() -> None:
    mb = _market(VenueName.MATCHBOOK)
    qualify = _market(
        VenueName.KALSHI,
        family=MarketFamily.TO_QUALIFY,
        outcomes=(CanonicalOutcome.HOME_QUALIFY, CanonicalOutcome.AWAY_QUALIFY),
        settlement=SettlementFingerprint(
            scope=SettlementScope.INCLUDING_PENALTIES,
            period=FootballPeriod.FULL_TIME,
            extra_time_included=True,
            penalties_included=True,
            push_possible=False,
        ),
    )
    two_way = _unknown_kalshi(outcomes=(CanonicalOutcome.HOME, CanonicalOutcome.AWAY))
    first_half = _unknown_kalshi(period=FootballPeriod.FIRST_HALF)
    first_half.settlement = SettlementFingerprint(
        scope=SettlementScope.UNKNOWN,
        period=FootballPeriod.FIRST_HALF,
        extra_time_included=None,
        penalties_included=None,
        push_possible=False,
    )
    matcher = MarketMatcher()
    assert matcher.match(mb, qualify).matched is False
    assert "market_family_mismatch" in matcher.match(mb, qualify).reasons
    assert matcher.match(mb, two_way).matched is False
    assert "outcome_space_mismatch" in matcher.match(mb, two_way).reasons
    assert matcher.match(mb, first_half).matched is False
    assert "period_mismatch" in matcher.match(mb, first_half).reasons


def test_explicit_extra_time_contradiction_rejects() -> None:
    mb = _market(VenueName.MATCHBOOK)
    extra_time = _market(
        VenueName.KALSHI,
        settlement=SettlementFingerprint(
            scope=SettlementScope.INCLUDING_EXTRA_TIME,
            period=FootballPeriod.FULL_TIME,
            extra_time_included=True,
            penalties_included=False,
            push_possible=False,
        ),
    )
    result = MarketMatcher().match(mb, extra_time)
    assert result.matched is False
    assert "settlement_mismatch" in result.reasons


def test_unrelated_fixture_rejects() -> None:
    mb = _market(VenueName.MATCHBOOK, home="Real Betis", away="Getafe", source_id="9600")
    other = _unknown_kalshi(home="Monza", away="Sassuolo", source_id="9700")
    result = MarketMatcher().match(mb, other)
    assert result.matched is False
    assert "event_mismatch" in result.reasons


class EveningMatchbook:
    def __init__(self) -> None:
        self.list_events_calls = 0

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_events_calls += 1
        return {
            "events": [
                {
                    "id": int(event_id),
                    "name": f"{home} vs {away}",
                    "start": KICKOFF.isoformat(),
                    "competition-name": "Premier League",
                }
                for event_id, home, away, _suffix in FIXTURES
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        event = next(item for item in FIXTURES if item[0] == str(event_id))
        _, home, away, _suffix = event
        extra = []
        if event_id == "9600":
            extra.append(
                {
                    "id": 9607,
                    "name": "To Qualify",
                    "runners": [
                        {
                            "id": 51,
                            "name": home,
                            "prices": [{"side": "back", "odds": "1.70", "available-amount": "80"}],
                        },
                        {
                            "id": 52,
                            "name": away,
                            "prices": [{"side": "back", "odds": "2.20", "available-amount": "80"}],
                        },
                    ],
                }
            )
        return {
            "markets": [
                {
                    "id": int(event_id) + 1,
                    "name": "Match Odds",
                    "runners": [
                        {
                            "id": 1,
                            "name": home,
                            "prices": [{"side": "back", "odds": "2.10", "available-amount": "80"}],
                        },
                        {
                            "id": 2,
                            "name": "Draw",
                            "prices": [{"side": "back", "odds": "3.40", "available-amount": "80"}],
                        },
                        {
                            "id": 3,
                            "name": away,
                            "prices": [{"side": "back", "odds": "3.60", "available-amount": "80"}],
                        },
                    ],
                },
                *extra,
            ]
        }


class EmptyPolymarket:
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return []

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return []

    async def get_order_book(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {"asset_id": "x", "bids": [], "asks": []}


class EveningKalshi:
    def __init__(self) -> None:
        self.list_events_calls = 0
        self.contract_terms_calls: list[str] = []

    def _event(self, event_id: str, home: str, away: str, suffix: str) -> dict[str, Any]:
        ticker = f"KXEPLGAME-26SEP20{suffix}"
        markets = []
        for code, subtitle in ((home[:3].upper(), home), ("DRAW", "Draw"), (away[:3].upper(), away)):
            markets.append(
                {
                    "ticker": f"{ticker}-{code}",
                    "event_ticker": ticker,
                    "title": f"{home} vs {away}",
                    "yes_sub_title": subtitle,
                    "rules_primary": (
                        "Series contract terms define result scope that may take one of the "
                        "following: first half, regulation time, second half, extra time, or "
                        "full match."
                    ),
                }
            )
        return {
            "event_ticker": ticker,
            "series_ticker": "KXEPLGAME",
            "title": f"{home} vs {away}",
            "category": "Sports",
            "strike_date": KICKOFF.isoformat(),
            "markets": markets,
        }

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_events_calls += 1
        return {
            "events": [
                self._event(event_id, home, away, suffix)
                for event_id, home, away, suffix in FIXTURES
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": []}

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        del series_ticker
        return KALSHI_GAME_SERIES

    async def get_contract_terms_document(self, url: str) -> dict[str, Any]:
        self.contract_terms_calls.append(str(url))
        return {"url": str(url), "sha256": "aa" * 32, "byte_length": 10}

    async def get_order_book(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.33", "100.00"]],
                "no_dollars": [["0.64", "200.00"]],
            }
        }


@pytest.mark.asyncio
async def test_evening_census_matches_ordinary_unknown_1x2s() -> None:
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=EveningMatchbook(),
        polymarket=EmptyPolymarket(),
        kalshi=EveningKalshi(),
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        report = await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
            scan_lane=ScanLane.UNIVERSE.value,
        )
        census = census_from_report(report, data_class=CENSUS_DATA_CLASS_FIXTURE)
        forensics = forensics_from_report(
            report,
            data_class=CENSUS_DATA_CLASS_FIXTURE,
            venue_scope=VENUE_SCOPE_UNIVERSE,
            enabled_venues=[item.value for item in report.enabled_venues],
            detail=True,
        )
    finally:
        repository.close()

    assert census.discovered_fixtures == 3
    assert census.cross_venue_matched_events == 3
    assert census.equivalent_market_pairs == 3
    assert census.market_family_breakdown.get("match_result") == 3
    assert forensics.matchbook_kalshi_match_result.both_complete_3way == 3
    assert forensics.matchbook_kalshi_match_result.both_settlement_complete == 0
    assert forensics.matchbook_kalshi_match_result.matched_equivalent == 3
    rows = [row for items in report.fixture_markets.values() for row in items]
    qualify = [row for row in rows if row.family == "to_qualify"]
    assert qualify
    assert all(row.comparison_status.value != "matched_equivalent" for row in qualify)
    rendered = render_forensics(forensics)
    assert "settlement_unknown_not_contradictory" in rendered
    assert "settlement_status=unknown" in rendered
    assert "hda_complete=True" in rendered
    assert census.qualifying_arbs >= 0


@pytest.mark.asyncio
async def test_hot_and_universe_agree_on_unknown_1x2() -> None:
    from copy import deepcopy

    matchbook = EveningMatchbook()
    kalshi = EveningKalshi()
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=EmptyPolymarket(),
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        universe = await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
            scan_lane=ScanLane.UNIVERSE.value,
        )
        fixture = next(
            item
            for item in universe.discovered_fixtures
            if item.home_team == FIXTURES[0][1]
        )
        known = {
            fixture.canonical_event_id: [
                {
                    "venue": VenueName.MATCHBOOK.value,
                    "source_event_id": str(FIXTURES[0][0]),
                    "raw": {
                        "id": int(FIXTURES[0][0]),
                        "name": f"{FIXTURES[0][1]} vs {FIXTURES[0][2]}",
                        "start": KICKOFF.isoformat(),
                        "competition-name": "Premier League",
                    },
                },
                {
                    "venue": VenueName.KALSHI.value,
                    "source_event_id": f"KXEPLGAME-26SEP20{FIXTURES[0][3]}",
                    "raw": deepcopy(
                        EveningKalshi()._event(*FIXTURES[0])
                    ),
                },
            ]
        }
        hot_matchbook = EveningMatchbook()
        hot_kalshi = EveningKalshi()
        hot_repo = SqliteMarketIntelligenceRepository()
        hot_collector = ReadOnlyCrossVenueCollector(
            matchbook=hot_matchbook,
            polymarket=EmptyPolymarket(),
            kalshi=hot_kalshi,
            paper_scan=PaperScanService(MarketIntelligenceService(hot_repo)),
        )
        hot = await hot_collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
            scan_lane=ScanLane.HOT.value,
            identity_scope=[fixture.canonical_event_id],
            known_source_events=known,
        )
        assert hot_matchbook.list_events_calls == 0
        assert hot_kalshi.list_events_calls == 0
        assert universe.matched_market_pairs == 3
        assert hot.matched_market_pairs == 1
    finally:
        repository.close()
        hot_repo.close()


def test_owner_live_matchbook_kalshi_only_scope() -> None:
    scope = resolve_census_venue_scope(
        settings=Settings(),
        environ={OWNER_LIVE_CENSUS_MATCHBOOK_KALSHI_ONLY_ENV: "1"},
    )
    assert scope.enabled_venues == [VenueName.MATCHBOOK, VenueName.KALSHI]
    assert scope.participation_source == "explicit_matchbook_kalshi_only"
