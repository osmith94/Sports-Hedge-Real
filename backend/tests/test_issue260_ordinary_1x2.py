"""Ordinary Matchbook↔Kalshi 1X2 operational matching after contract-terms outcome B.

Fixture/demo only. Does not infer regulation from GAME/Opta/title.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.fixture_inventory import InventoryComparisonStatus
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
    GAMEWIN_ORDINARY_1X2_AUDIT_REASON,
    UNKNOWN_SETTLEMENT_ALLOWED_REASON,
    allow_unknown_settlement_for_ordinary_1x2,
)
from sports_hedge.normalization.kalshi_contract_terms import (
    GAMEWIN_SCOPE_UNAVAILABLE_REASON,
    SOCCERGAMEWIN_SHA256,
)
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import matchbook_polymarket_costs

KICKOFF = datetime(2026, 9, 20, 19, 0, tzinfo=UTC)
GAMEWIN_URL = "https://assets.kalshi.com/contract_terms/SOCCERGAMEWIN.pdf"
GAMEWIN_TEMPLATE = (
    "Series contract terms define result scope that may take one of the "
    "following: first half, regulation time, second half, extra time, or "
    "full match."
)
ET_COMPLETE = "Resolves including penalties after extra time."
KALSHI_GAME_SERIES = {
    "ticker": "KXEPLGAME",
    "title": "Premier League",
    "fee_type": "quadratic",
    "fee_multiplier": 1,
    "settlement_sources": [{"name": "Opta"}],
    "contract_terms_url": GAMEWIN_URL,
}

ORDINARY_FIXTURES = (
    ("9600", "Real Betis", "Getafe", "BETGET"),
    ("9610", "Monza", "Sassuolo", "MONSAS"),
    ("9620", "Brentford", "Chelsea", "BRECHE"),
    ("9630", "Arsenal", "Tottenham", "ARSTOT"),
    ("9640", "Liverpool", "Everton", "LIVEVE"),
    ("9650", "Brighton", "Fulham", "BRIFUL"),
    ("9660", "Newcastle", "West Ham", "NEWWES"),
    ("9670", "Crystal Palace", "Wolves", "CRYWOL"),
    ("9680", "Bournemouth", "Leeds", "BOULEE"),
    ("9690", "Aston Villa", "Nottingham Forest", "ASTNOT"),
    ("9710", "Athletic Bilbao", "Real Sociedad", "ATHSOC"),
    ("9720", "Villarreal", "Celta", "VILCEL"),
)
CONTROL_ET = ("9800", "Sevilla", "Valencia", "SEVVAL")
CONTROL_TWO_WAY = ("9810", "Atalanta", "Roma", "ATAROM")
CONTROL_FIRST_HALF = ("9820", "Lazio", "Napoli", "LAZNAP")
CONTROL_MB_ONLY = ("9830", "Wrong FC", "Other FC")
CONTROL_KALSHI_ONLY = ("Unrelated United", "Random City", "UNRRAN")


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


def _unknown_kalshi(*, gamewin: bool = True, **kwargs: Any) -> CanonicalMarket:
    return _market(
        VenueName.KALSHI,
        settlement=SettlementFingerprint(
            scope=SettlementScope.UNKNOWN,
            period=FootballPeriod.FULL_TIME,
            extra_time_included=None,
            penalties_included=None,
            push_possible=False,
            unknown_reason=GAMEWIN_SCOPE_UNAVAILABLE_REASON if gamewin else None,
        ),
        confidence=0.75,
        **kwargs,
    )


def test_mb_regulation_and_kalshi_gamewin_unknown_ordinary_1x2_match() -> None:
    mb = _market(VenueName.MATCHBOOK)
    kalshi = _unknown_kalshi()
    result = MarketMatcher().match(mb, kalshi)
    assert result.matched is True
    assert UNKNOWN_SETTLEMENT_ALLOWED_REASON in result.reasons
    assert GAMEWIN_ORDINARY_1X2_AUDIT_REASON in result.reasons
    assert result.confidence >= 0.98
    assert allow_unknown_settlement_for_ordinary_1x2(mb, kalshi) is True


def test_both_complete_regulation_still_uses_strict_comparison() -> None:
    mb = _market(VenueName.MATCHBOOK)
    kalshi = _market(VenueName.KALSHI, confidence=0.75)
    result = MarketMatcher().match(mb, kalshi)
    assert result.matched is True
    assert GAMEWIN_ORDINARY_1X2_AUDIT_REASON not in result.reasons
    assert allow_unknown_settlement_for_ordinary_1x2(mb, kalshi) is False


def test_kalshi_unknown_without_gamewin_placeholder_fails_closed() -> None:
    mb = _market(VenueName.MATCHBOOK)
    kalshi = _unknown_kalshi(gamewin=False)
    result = MarketMatcher().match(mb, kalshi)
    assert result.matched is False
    assert "incomplete_settlement" in result.reasons
    assert allow_unknown_settlement_for_ordinary_1x2(mb, kalshi) is False


def test_matchbook_extra_time_does_not_use_gamewin_unknown_tolerance() -> None:
    mb = _market(
        VenueName.MATCHBOOK,
        settlement=SettlementFingerprint(
            scope=SettlementScope.INCLUDING_EXTRA_TIME,
            period=FootballPeriod.FULL_TIME,
            extra_time_included=True,
            penalties_included=False,
            push_possible=False,
        ),
    )
    kalshi = _unknown_kalshi()
    result = MarketMatcher().match(mb, kalshi)
    assert result.matched is False
    assert "incomplete_settlement" in result.reasons
    assert allow_unknown_settlement_for_ordinary_1x2(mb, kalshi) is False


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
        unknown_reason=GAMEWIN_SCOPE_UNAVAILABLE_REASON,
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


def _mb_event(event_id: str, home: str, away: str) -> dict[str, Any]:
    return {
        "id": int(event_id),
        "name": f"{home} vs {away}",
        "start": KICKOFF.isoformat(),
        "competition-name": "Premier League",
    }


def _mb_match_odds(market_id: int, home: str, away: str) -> dict[str, Any]:
    return {
        "id": market_id,
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
    }


class EveningMatchbook:
    def __init__(self) -> None:
        self.list_events_calls = 0

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_events_calls += 1
        events = [_mb_event(event_id, home, away) for event_id, home, away, _suffix in ORDINARY_FIXTURES]
        events.append(_mb_event(*CONTROL_ET[:3]))
        events.append(_mb_event(*CONTROL_TWO_WAY[:3]))
        events.append(_mb_event(*CONTROL_FIRST_HALF[:3]))
        events.append(_mb_event(*CONTROL_MB_ONLY))
        return {"events": events}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        source = str(event_id)
        ordinary = next((item for item in ORDINARY_FIXTURES if item[0] == source), None)
        if ordinary is not None:
            _, home, away, _suffix = ordinary
            extra = []
            if source == "9600":
                extra.append(
                    {
                        "id": 9607,
                        "name": "To Qualify",
                        "runners": [
                            {
                                "id": 51,
                                "name": home,
                                "prices": [
                                    {"side": "back", "odds": "1.70", "available-amount": "80"}
                                ],
                            },
                            {
                                "id": 52,
                                "name": away,
                                "prices": [
                                    {"side": "back", "odds": "2.20", "available-amount": "80"}
                                ],
                            },
                        ],
                    }
                )
            return {"markets": [_mb_match_odds(int(source) + 1, home, away), *extra]}
        for control in (CONTROL_ET, CONTROL_TWO_WAY, CONTROL_FIRST_HALF):
            if control[0] == source:
                return {"markets": [_mb_match_odds(int(source) + 1, control[1], control[2])]}
        if source == CONTROL_MB_ONLY[0]:
            return {
                "markets": [
                    _mb_match_odds(int(source) + 1, CONTROL_MB_ONLY[1], CONTROL_MB_ONLY[2])
                ]
            }
        return {"markets": []}


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

    def _markets(
        self,
        home: str,
        away: str,
        suffix: str,
        *,
        include_draw: bool = True,
        title_suffix: str = "",
        rules_primary: str = GAMEWIN_TEMPLATE,
    ) -> tuple[str, list[dict[str, Any]]]:
        ticker = f"KXEPLGAME-26SEP20{suffix}"
        title = f"{home} vs {away}{title_suffix}"
        codes = [(home[:3].upper(), home)]
        if include_draw:
            codes.append(("DRAW", "Draw"))
        codes.append((away[:3].upper(), away))
        markets = []
        for code, subtitle in codes:
            markets.append(
                {
                    "ticker": f"{ticker}-{code}",
                    "event_ticker": ticker,
                    "title": title,
                    "yes_sub_title": subtitle,
                    "rules_primary": rules_primary,
                }
            )
        return ticker, markets

    def _event(
        self,
        home: str,
        away: str,
        suffix: str,
        *,
        include_draw: bool = True,
        title_suffix: str = "",
        rules_primary: str = GAMEWIN_TEMPLATE,
    ) -> dict[str, Any]:
        ticker, markets = self._markets(
            home,
            away,
            suffix,
            include_draw=include_draw,
            title_suffix=title_suffix,
            rules_primary=rules_primary,
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
        events = [
            self._event(home, away, suffix)
            for _event_id, home, away, suffix in ORDINARY_FIXTURES
        ]
        events.append(self._event(*CONTROL_ET[1:], rules_primary=ET_COMPLETE))
        events.append(self._event(*CONTROL_TWO_WAY[1:], include_draw=False))
        events.append(self._event(*CONTROL_FIRST_HALF[1:], title_suffix=" First Half"))
        events.append(self._event(*CONTROL_KALSHI_ONLY))
        return {"events": events}

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del event_id, filters
        return {"markets": []}

    async def get_series(self, series_ticker: str) -> dict[str, Any]:
        del series_ticker
        return KALSHI_GAME_SERIES

    async def get_contract_terms_document(self, url: str) -> dict[str, Any]:
        self.contract_terms_calls.append(str(url))
        return {
            "url": str(url),
            "sha256": SOCCERGAMEWIN_SHA256,
            "byte_length": 10,
        }

    async def get_order_book(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        del args, kwargs
        return {
            "orderbook_fp": {
                "yes_dollars": [["0.33", "100.00"]],
                "no_dollars": [["0.64", "200.00"]],
            }
        }


def _evening_collector() -> tuple[
    ReadOnlyCrossVenueCollector,
    SqliteMarketIntelligenceRepository,
    EveningMatchbook,
    EveningKalshi,
]:
    matchbook = EveningMatchbook()
    kalshi = EveningKalshi()
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=EmptyPolymarket(),
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    return collector, repository, matchbook, kalshi


async def _collect_evening(**kwargs: Any):
    collector, repository, matchbook, kalshi = _evening_collector()
    try:
        report = await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
            scan_lane=ScanLane.UNIVERSE.value,
            **kwargs,
        )
        census = census_from_report(report, data_class=CENSUS_DATA_CLASS_FIXTURE)
        forensics = forensics_from_report(
            report,
            data_class=CENSUS_DATA_CLASS_FIXTURE,
            venue_scope=VENUE_SCOPE_UNIVERSE,
            enabled_venues=[item.value for item in report.enabled_venues],
            detail=True,
        )
        return report, census, forensics, matchbook, kalshi
    finally:
        repository.close()


@pytest.mark.asyncio
async def test_evening_census_matches_ordinary_gamewin_unknown_1x2s() -> None:
    report, census, forensics, _matchbook, kalshi = await _collect_evening()
    assert len(ORDINARY_FIXTURES) == 12
    assert census.discovered_fixtures == 17
    assert census.cross_venue_matched_events == 15
    assert census.equivalent_market_pairs == 12
    assert census.market_family_breakdown.get("match_result") in {None, 0}
    assert census.ordinary_1x2_structural_admissions == 12
    assert census.qualifying_arbs == 0
    assert forensics.matchbook_kalshi_match_result.matched_equivalent == 0
    assert forensics.matchbook_kalshi_match_result.both_settlement_complete == 1
    assert kalshi.contract_terms_calls
    assert all(url == GAMEWIN_URL for url in kalshi.contract_terms_calls)

    rows = [row for items in report.fixture_markets.values() for row in items]
    ordinary_rows = [
        row
        for row in rows
        if row.family == "match_result"
        and GAMEWIN_ORDINARY_1X2_AUDIT_REASON in (row.match_reasons or [])
    ]
    assert len(ordinary_rows) == 12
    assert all(
        row.comparison_status is InventoryComparisonStatus.OTHER
        and row.reason == "catalogue_review_required"
        and row.entered_solver is False
        for row in ordinary_rows
    )

    qualify = [row for row in rows if row.family == "to_qualify"]
    assert qualify
    assert all(row.comparison_status.value != "matched_equivalent" for row in qualify)

    by_home = {item.home_team: item for item in report.discovered_fixtures}
    assert by_home[CONTROL_ET[1]].matched_equivalent_count == 0
    assert by_home[CONTROL_TWO_WAY[1]].matched_equivalent_count == 0
    assert by_home[CONTROL_FIRST_HALF[1]].matched_equivalent_count == 0
    assert by_home[CONTROL_MB_ONLY[1]].matched_equivalent_count == 0
    assert by_home[CONTROL_KALSHI_ONLY[0]].matched_equivalent_count == 0
    for _event_id, home, _away, _suffix in ORDINARY_FIXTURES:
        assert by_home[home].matched_equivalent_count == 0

    rendered = render_forensics(forensics)
    assert "settlement_unknown_not_contradictory" in rendered
    assert GAMEWIN_ORDINARY_1X2_AUDIT_REASON in rendered
    assert "settlement_status=unknown" in rendered
    assert "hda_complete=True" in rendered
    assert "ordinary_1x2_structural_admissions=12" in (
        f"ordinary_1x2_structural_admissions={census.ordinary_1x2_structural_admissions}"
    )

    solver_decisions = [
        decision
        for decision in report.paper_decisions
        if decision.solver_model is not None or decision.depth_scan is not None
    ]
    assert solver_decisions == []


@pytest.mark.asyncio
async def test_resumed_universe_still_reaches_ordinary_1x2_matches() -> None:
    baseline, _census, _forensics, _matchbook, _kalshi = await _collect_evening()
    skip = [
        item.canonical_event_id
        for item in baseline.discovered_fixtures
        if item.home_team in {ORDINARY_FIXTURES[0][1], ORDINARY_FIXTURES[1][1]}
    ]
    assert len(skip) == 2
    resumed, census, _forensics, _matchbook, _kalshi = await _collect_evening(
        skip_event_ids=skip,
        generation_resume=True,
        universe_generation_id=1,
    )
    assert census.generation_resume is True
    remaining_homes = {item.home_team for item in resumed.discovered_fixtures}
    assert ORDINARY_FIXTURES[0][1] not in remaining_homes
    assert ORDINARY_FIXTURES[1][1] not in remaining_homes
    assert census.equivalent_market_pairs == 10
    assert census.ordinary_1x2_structural_admissions == 10
    assert census.market_family_breakdown.get("match_result") in {None, 0}


@pytest.mark.asyncio
async def test_hot_and_universe_agree_on_unknown_1x2() -> None:
    universe, _census, _forensics, _matchbook, _kalshi = await _collect_evening()
    fixture = next(
        item
        for item in universe.discovered_fixtures
        if item.home_team == ORDINARY_FIXTURES[0][1]
    )
    known = {
        fixture.canonical_event_id: [
            {
                "venue": VenueName.MATCHBOOK.value,
                "source_event_id": str(ORDINARY_FIXTURES[0][0]),
                "raw": {
                    "id": int(ORDINARY_FIXTURES[0][0]),
                    "name": f"{ORDINARY_FIXTURES[0][1]} vs {ORDINARY_FIXTURES[0][2]}",
                    "start": KICKOFF.isoformat(),
                    "competition-name": "Premier League",
                },
            },
            {
                "venue": VenueName.KALSHI.value,
                "source_event_id": f"KXEPLGAME-26SEP20{ORDINARY_FIXTURES[0][3]}",
                "raw": deepcopy(EveningKalshi()._event(*ORDINARY_FIXTURES[0][1:])),
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
    try:
        hot = await hot_collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
            scan_lane=ScanLane.HOT.value,
            identity_scope=[fixture.canonical_event_id],
            known_source_events=known,
        )
        universe_row = next(
            row
            for row in universe.fixture_markets.get(fixture.canonical_event_id, [])
            if row.family == "match_result"
            and GAMEWIN_ORDINARY_1X2_AUDIT_REASON in (row.match_reasons or [])
        )
        hot_row = next(
            row
            for rows in hot.fixture_markets.values()
            for row in rows
            if row.family == "match_result"
            and GAMEWIN_ORDINARY_1X2_AUDIT_REASON in (row.match_reasons or [])
        )
        assert hot_matchbook.list_events_calls == 0
        assert hot_kalshi.list_events_calls == 0
        assert universe.matched_market_pairs == 12
        assert hot.matched_market_pairs == 1
        assert hot_row.comparison_status is InventoryComparisonStatus.OTHER
        assert universe_row.comparison_status is InventoryComparisonStatus.OTHER
        assert hot_row.reason == "catalogue_review_required"
        assert universe_row.reason == "catalogue_review_required"
        assert hot_row.entered_solver is False
        assert universe_row.entered_solver is False
        assert set(hot_row.match_reasons) >= {
            UNKNOWN_SETTLEMENT_ALLOWED_REASON,
            GAMEWIN_ORDINARY_1X2_AUDIT_REASON,
        }
        assert set(universe_row.match_reasons) == set(hot_row.match_reasons)
        assert not any(
            item.solver_model is not None or item.depth_scan is not None
            for item in (*universe.paper_decisions, *hot.paper_decisions)
        )
    finally:
        hot_repo.close()


def test_owner_live_matchbook_kalshi_only_scope() -> None:
    scope = resolve_census_venue_scope(
        settings=Settings(),
        environ={OWNER_LIVE_CENSUS_MATCHBOOK_KALSHI_ONLY_ENV: "1"},
    )
    assert scope.enabled_venues == [VenueName.MATCHBOOK, VenueName.KALSHI]
    assert scope.participation_source == "explicit_matchbook_kalshi_only"
