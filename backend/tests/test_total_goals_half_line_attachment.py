"""Safe half-line Total Goals attachment: listing → normalize → inventory → catalogue.

Live owner-live soak showed Kalshi TOTAL present on an approved fixture while
the Matchbook leg was absent (coverage NOT_LISTED /
venue_can_offer_archetype_but_this_fixture_has_no_listed_market).

Root cause (deterministic, not owner-live quotes):
1. Matchbook GET /events/{id}/markets is paged (default 20; we request 100) and
   the client previously returned only the first page, so Over/Under Goals after
   player-prop noise never entered inventory.
2. Live Matchbook also lists the same contract as name ``Total`` /
   grading-type ``point-total`` with Over/Under runners. That name was outside
   the recogniser, so the listed book was dropped before TOTAL_GOALS inventory.

PAPER / read-only. Exact-line matching is unchanged. Integer/quarter stay
deferred. Provider concurrency is unchanged (pages are sequential inside one
list_markets call).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import httpx
import pytest

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.fixture_inventory import InventoryComparisonStatus
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.scan_lanes import ScanLane
from sports_hedge.catalogue.corpus import REGULATION
from sports_hedge.catalogue.coverage_rows import fixture_catalogue_coverage
from sports_hedge.catalogue.registry import CatalogueCoverageState
from sports_hedge.catalogue.states import CatalogueArchetype
from sports_hedge.config import Settings
from sports_hedge.domain.football import FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.approved_register import (
    CANONICAL_BTTS_FT,
    CANONICAL_FTTS_FT,
    CANONICAL_MATCH_RESULT_FT,
    canonical_key_for_market,
)
from sports_hedge.normalization.venues import MatchbookNormalizer, VenueNormalizationError
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore
from sports_hedge.venues.matchbook import MatchbookClient
from test_issue293_owner_live_overlap import OverlapKalshi, OverlapMatchbook
from test_issue316_catalogue_registry import (
    MB_EVENT_ID,
    _DisabledPolymarket,
    _all_books,
    _costs,
    _fx,
    _kalshi_btts_event,
    _kalshi_ftts_event,
    _kalshi_game_event,
    _kalshi_total_event,
    _mb_btts,
    _mb_event,
    _mb_ftts,
    _mb_match_odds,
    _runner,
    _series,
)
from test_issue341_approved_market_catalogue import _collect_catalogue
from test_matchbook_event_discovery import FOOTBALL_SPORT_ID, _event
from test_matchbook_total_goals_scope import _offending_matchbook_market

SCAN_AT = datetime(2026, 9, 20, 12, 0, tzinfo=UTC)
KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
EVENT_ID = 8801


def _settings(**overrides: Any) -> Settings:
    return Settings(
        matchbook_username="test-user",
        matchbook_password="test-password",
        **overrides,
    )


def _player_prop(index: int) -> dict[str, Any]:
    return {
        "id": 900_000 + index,
        "name": f"Player {index} to Score",
        "market-type": "other",
        "runners": [
            _runner(index * 10, "Yes", "3.50"),
            _runner(index * 10 + 1, "No", "1.30"),
        ],
    }


def _live_total_market(*, line: str = "2.5", market_id: int = 316030) -> dict[str, Any]:
    """Live-shaped Matchbook match total: terse name, point-total, runner line."""

    return {
        "id": market_id,
        "name": "Total",
        "market-type": "point-total",
        "grading-type": "point-total",
        "handicap": line,
        "runners": [
            _runner(21, f"Over {line}", "1.85"),
            _runner(22, f"Under {line}", "2.05"),
        ],
    }


def _kalshi_family_events(total_line: str = "2.5") -> list[dict[str, Any]]:
    return [
        _kalshi_game_event(rules=REGULATION),
        _kalshi_btts_event(),
        _kalshi_total_event(total_line),
        _kalshi_ftts_event(),
    ]


def test_live_named_total_normalizes_as_full_match_half_line() -> None:
    event = MatchbookNormalizer().normalize_event(_mb_event())
    market = MatchbookNormalizer().normalize_market(event, _live_total_market())
    assert market.family is MarketFamily.TOTAL_GOALS
    assert market.period is FootballPeriod.FULL_TIME
    assert market.line == Decimal("2.5")
    assert canonical_key_for_market(market) == "TOTAL_GOALS_FT:2.5"


def test_live_named_total_extracts_line_from_runners_when_handicap_absent() -> None:
    event = MatchbookNormalizer().normalize_event(_mb_event())
    payload = _live_total_market()
    payload.pop("handicap")
    market = MatchbookNormalizer().normalize_market(event, payload)
    assert market.family is MarketFamily.TOTAL_GOALS
    assert market.line == Decimal("2.5")
    assert canonical_key_for_market(market) == "TOTAL_GOALS_FT:2.5"


def test_first_half_total_without_goals_phrase_stays_unsupported() -> None:
    event = MatchbookNormalizer().normalize_event(_mb_event())
    with pytest.raises(VenueNormalizationError, match="1st Half Total"):
        MatchbookNormalizer().normalize_market(
            event,
            {
                "id": 316039,
                "name": "1st Half Total",
                "runners": [_runner(41, "Over 1.5"), _runner(42, "Under 1.5")],
            },
        )


def test_first_half_total_with_point_total_type_stays_unsupported() -> None:
    event = MatchbookNormalizer().normalize_event(_mb_event())
    with pytest.raises(VenueNormalizationError, match="1st Half Total"):
        MatchbookNormalizer().normalize_market(
            event,
            {
                "id": 316040,
                "name": "1st Half Total",
                "market-type": "point-total",
                "grading-type": "point-total",
                "runners": [_runner(41, "Over 1.5"), _runner(42, "Under 1.5")],
            },
        )


def test_integer_and_quarter_live_totals_are_not_register_keys() -> None:
    event = MatchbookNormalizer().normalize_event(_mb_event())
    integer = MatchbookNormalizer().normalize_market(event, _live_total_market(line="2.0", market_id=316031))
    quarter = MatchbookNormalizer().normalize_market(event, _live_total_market(line="2.25", market_id=316032))
    assert integer.family is MarketFamily.TOTAL_GOALS
    assert quarter.family is MarketFamily.TOTAL_GOALS
    assert canonical_key_for_market(integer) is None
    assert canonical_key_for_market(quarter) is None


def test_participant_total_still_cannot_be_full_match_total() -> None:
    event = MatchbookNormalizer().normalize_event(_mb_event())
    market = MatchbookNormalizer().normalize_market(event, _offending_matchbook_market())
    assert market.family is MarketFamily.TEAM_TOTAL
    assert canonical_key_for_market(market) is None


class _PagedTotalsTransport:
    """First page is player-prop noise; safe half-line Total is on page 2."""

    def __init__(self) -> None:
        self.market_offsets: list[int] = []
        first_page = [_player_prop(index) for index in range(100)]
        first_page[0] = _mb_match_odds()
        first_page[1] = _mb_btts()
        first_page[2] = _mb_ftts()
        self._pages = {
            0: first_page,
            100: [
                _live_total_market(),
                {
                    "id": 316031,
                    "name": "Over/Under 2.0 Goals",
                    "line": "2.0",
                    "runners": [_runner(121, "Over 2.0", "1.85"), _runner(122, "Under 2.0", "2.05")],
                },
                {
                    "id": 316032,
                    "name": "Over/Under 2.25 Goals",
                    "line": "2.25",
                    "runners": [_runner(131, "Over 2.25", "1.85"), _runner(132, "Under 2.25", "2.05")],
                },
            ],
        }

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.path == "/bpapi/rest/security/session":
            return httpx.Response(200, json={"session-token": "paper-session"})
        if request.url.path == "/edge/rest/lookups/sports":
            return httpx.Response(
                200,
                json={
                    "total": 1,
                    "sports": [{"id": FOOTBALL_SPORT_ID, "name": "Football", "type": "SPORT"}],
                },
            )
        if request.url.path == "/edge/rest/events":
            return httpx.Response(
                200,
                json={
                    "offset": 0,
                    "per-page": 100,
                    "total": 1,
                    "events": [
                        _event(
                            EVENT_ID,
                            "Brentford vs Chelsea",
                            sport="Football",
                            competition="Premier League",
                            start=KICKOFF,
                        )
                    ],
                },
            )
        if request.url.path == f"/edge/rest/events/{EVENT_ID}/markets":
            offset = int(request.url.params.get("offset", "0"))
            self.market_offsets.append(offset)
            page = self._pages.get(offset, [])
            return httpx.Response(
                200,
                json={
                    "offset": offset,
                    "per-page": 100,
                    "total": 103,
                    "markets": page,
                },
            )
        return httpx.Response(404)


@pytest.mark.asyncio
async def test_matchbook_list_markets_pages_past_player_props_to_half_line_total() -> None:
    transport = _PagedTotalsTransport()
    settings = _settings()
    mock = httpx.MockTransport(transport.handler)
    async with httpx.AsyncClient(transport=mock, base_url=settings.matchbook_base_url) as http:
        venue = MatchbookClient(settings, client=http, clock=lambda: SCAN_AT)
        payload = await venue.list_markets(EVENT_ID)

    ids = [str(item["id"]) for item in payload["markets"]]
    assert transport.market_offsets == [0, 100]
    assert "316010" in ids
    assert "316030" in ids
    assert payload["truncated"] is False
    names = {str(item["name"]) for item in payload["markets"]}
    assert "Total" in names
    assert "Over/Under 2.0 Goals" in names


def test_missing_matchbook_half_line_is_the_observed_not_listed_diagnostic() -> None:
    """Document the soak symptom: Kalshi 2.5 present, Matchbook absent."""

    from sports_hedge.application.fixture_inventory import (
        FixtureMarketInventoryRow,
        VenueMarketFacts,
    )

    kalshi_only = FixtureMarketInventoryRow(
        display_name="Total Goals 2.5",
        family="total_goals",
        period="full_time",
        line=Decimal("2.5"),
        comparison_status=InventoryComparisonStatus.VENUE_ONLY,
        reason="venue_only",
        kalshi=VenueMarketFacts(
            venue=VenueName.KALSHI,
            source_event_id="KXEPLTOTAL-26SEP20BRECHE",
            source_market_id="KXEPLTOTAL-26SEP20BRECHE-2.5",
            family="total_goals",
            line=Decimal("2.5"),
        ),
    )
    coverage = fixture_catalogue_coverage(
        [kalshi_only],
        matchbook_matched=True,
        kalshi_matched=True,
        target_competition_code="premier_league",
    )
    row = next(item for item in coverage.rows if item.archetype is CatalogueArchetype.TOTAL_GOALS_HALF_LINE)
    assert row.kalshi_present is True
    assert row.matchbook_present is False
    assert row.state is CatalogueCoverageState.NOT_LISTED
    assert row.reason == "venue_can_offer_archetype_but_this_fixture_has_no_listed_market"


@pytest.mark.asyncio
async def test_live_named_total_attaches_to_same_fixture_inventory_and_catalogue() -> None:
    matchbook = OverlapMatchbook(
        [_mb_event()],
        {
            str(MB_EVENT_ID): [
                _mb_match_odds(),
                _mb_btts(),
                _live_total_market(),
                _live_total_market(line="2.0", market_id=316031),
                _live_total_market(line="2.25", market_id=316032),
                _offending_matchbook_market(),
                _mb_ftts(),
                *[_player_prop(index) for index in range(8)],
            ]
        },
    )
    kalshi = OverlapKalshi(
        _kalshi_family_events(),
        series_by_ticker={
            "KXEPLGAME": _series("KXEPLGAME"),
            "KXEPLBTTS": _series("KXEPLBTTS"),
            "KXEPLTOTAL": _series("KXEPLTOTAL"),
            "KXEPLFTTS": _series("KXEPLFTTS"),
        },
        books=_all_books(),
    )
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        report = await _collect_catalogue(matchbook, kalshi, store)
        fixture = report.discovered_fixtures[0]
        rows = report.fixture_markets[fixture.canonical_event_id]
        comparable = {
            row.family: row
            for row in rows
            if row.comparison_status is InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT
            or row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT
        }
        assert {"match_result", "both_teams_to_score", "total_goals", "first_team_to_score"} <= set(
            comparable
        )
        totals = comparable["total_goals"]
        assert totals.line == Decimal("2.5")
        assert totals.matchbook is not None
        assert totals.kalshi is not None
        assert totals.matchbook.source_market_id == "316030"
        assert totals.matchbook.raw_market_name == "Total"
        assert "2.5" in (totals.kalshi.source_market_id or "")
        integer_rows = [
            row
            for row in rows
            if row.family == "total_goals" and row.line == Decimal("2.0")
        ]
        quarter_rows = [
            row
            for row in rows
            if row.family == "total_goals" and row.line == Decimal("2.25")
        ]
        assert integer_rows
        assert all(
            row.comparison_status
            not in {
                InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT,
                InventoryComparisonStatus.MATCHED_EQUIVALENT,
            }
            for row in integer_rows
        )
        assert quarter_rows
        assert all(
            row.comparison_status
            not in {
                InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT,
                InventoryComparisonStatus.MATCHED_EQUIVALENT,
            }
            for row in quarter_rows
        )
        team_rows = [row for row in rows if row.family == "team_total"]
        assert team_rows
        assert all(row.kalshi is None for row in team_rows)

        coverage = fixture_catalogue_coverage(
            rows,
            matchbook_matched=True,
            kalshi_matched=True,
            target_competition_code="premier_league",
        )
        half = next(
            item for item in coverage.rows if item.archetype is CatalogueArchetype.TOTAL_GOALS_HALF_LINE
        )
        assert half.matchbook_present is True
        assert half.kalshi_present is True
        assert half.state in {
            CatalogueCoverageState.PAPER_ASSUMED_EQUIVALENT,
            CatalogueCoverageState.APPROVED_EQUIVALENT,
        }

        active = {row.register_canonical_key: row for row in store.list_active()}
        assert CANONICAL_MATCH_RESULT_FT in active
        assert CANONICAL_BTTS_FT in active
        assert "TOTAL_GOALS_FT:2.5" in active
        assert CANONICAL_FTTS_FT in active
        assert "TOTAL_GOALS_FT:2.0" not in active
        assert "TOTAL_GOALS_FT:2.25" not in active
        assert active["TOTAL_GOALS_FT:2.5"].matchbook_market_id == "316030"
        assert Settings().sports_hedge_execution_enabled is False
        assert Settings().paper_scan_matchbook_concurrency == 4
    finally:
        store.close()


@pytest.mark.asyncio
async def test_paged_matchbook_half_line_reaches_collector_inventory() -> None:
    transport = _PagedTotalsTransport()
    settings = _settings()
    repository = SqliteMarketIntelligenceRepository()
    mock = httpx.MockTransport(transport.handler)
    async with httpx.AsyncClient(transport=mock, base_url=settings.matchbook_base_url) as http:
        matchbook = MatchbookClient(settings, client=http, clock=lambda: SCAN_AT)
        kalshi = OverlapKalshi(
            _kalshi_family_events(),
            series_by_ticker={
                "KXEPLGAME": _series("KXEPLGAME"),
                "KXEPLBTTS": _series("KXEPLBTTS"),
                "KXEPLTOTAL": _series("KXEPLTOTAL"),
                "KXEPLFTTS": _series("KXEPLFTTS"),
            },
            books=_all_books(),
        )
        collector = ReadOnlyCrossVenueCollector(
            matchbook=matchbook,
            polymarket=_DisabledPolymarket(),
            kalshi=kalshi,
            paper_scan=PaperScanService(MarketIntelligenceService(repository)),
        )
        try:
            report = await collector.collect_and_scan(
                venue_costs=_costs(),
                fx_snapshots=_fx(),
                maximum_execution_risk=100,
                enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
                unbounded_cycle=True,
                scan_lane=ScanLane.UNIVERSE.value,
            )
        finally:
            repository.close()

    assert transport.market_offsets == [0, 100]
    fixture = next(item for item in report.discovered_fixtures if item.matchbook_matched)
    rows = report.fixture_markets[fixture.canonical_event_id]
    totals = [
        row
        for row in rows
        if row.family == "total_goals"
        and row.line == Decimal("2.5")
        and row.matchbook is not None
        and row.kalshi is not None
    ]
    assert totals
    assert totals[0].comparison_status in {
        InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT,
        InventoryComparisonStatus.MATCHED_EQUIVALENT,
    }
    assert totals[0].matchbook is not None
    assert totals[0].matchbook.raw_market_name == "Total"
    assert not any(
        issue.stage == "list_markets" and "truncated" in str(issue.detail).casefold()
        for issue in report.issues
    )
