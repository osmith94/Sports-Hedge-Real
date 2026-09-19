"""Issue #316: four-family Phase-1 registry + paper-assumed locked families.

Deterministic fixture/demo providers. Not owner-live quotes. PAPER / read-only.
Execution disabled. Do not invent Kalshi availability or settlement semantics.

Phase-1 owner-live operationalises MATCH_RESULT / BTTS / TOTAL (safe exact half-line)
/ FTTS. Issue #326 admits all four in PAPER mode once canonical identity matches.
Independently proven siblings remain APPROVED_EQUIVALENT. Non-target families must
not trigger expensive work.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from sports_hedge.application.capture_replay import (
    DEFAULT_KALSHI_BOOK,
    FORBIDDEN_WRITE_METHODS,
    ReplayBundle,
    assert_no_write_methods,
    attempt_live_read_only_capture,
    catalogue_relevant_kalshi_series,
    replay_bundle,
)
from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.fixture_inventory import InventoryComparisonStatus
from sports_hedge.application.hot_market_relationships import relationships_from_fixture_markets
from sports_hedge.application.mapping_census import census_from_report
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.catalogue.admission import (
    catalogue_allows_live_execution,
    catalogue_allows_solver,
)
from sports_hedge.catalogue.classify import PayloadSide, classify_payload_pair
from sports_hedge.catalogue.corpus import (
    CANCEL_RESCHEDULE_FAIR_PRICE,
    ET_RULES,
    GAMEWIN_TEMPLATE,
    REGULATION,
)
from sports_hedge.catalogue.coverage_rows import fixture_catalogue_coverage
from sports_hedge.catalogue.registry import (
    REGISTRY_PARENT_COMMIT,
    REGISTRY_SHARED_BY,
    REGISTRY_VERSION,
    CatalogueCoverageState,
    family_is_phase1_expensive_work,
    kalshi_series_suffixes,
    phase1_four_family_archetypes,
    registry_cell,
    render_registry_markdown,
    target_archetypes,
    target_market_families,
)
from sports_hedge.catalogue.states import CatalogueApprovalState, CatalogueArchetype
from sports_hedge.config import Settings
from sports_hedge.domain.football import MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.facts.aliases import resolve_team_name
from sports_hedge.facts.identity import canonical_team_id
from sports_hedge.facts.team_registry import (
    BUNDESLIGA,
    LA_LIGA,
    PREMIER_LEAGUE,
    SERIE_A,
    clubs_for,
)
from sports_hedge.fees.kalshi import kalshi_cost_from_series
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.venues.kalshi import KalshiClient
from sports_hedge.venues.matchbook import MatchbookClient
from sports_hedge.venues.polymarket import PolymarketClient
from test_issue293_owner_live_overlap import OverlapKalshi, OverlapMatchbook
from venue_cost_helpers import matchbook_polymarket_costs

KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 18, 12, 0, tzinfo=UTC)
HOME = "Brentford"
AWAY = "Chelsea"
COMPETITION = "Premier League"
MB_EVENT_ID = 316001


def test_paper_boundary_holds() -> None:
    settings = Settings()
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    assert_no_write_methods()
    for client in (MatchbookClient, KalshiClient, PolymarketClient):
        for method in FORBIDDEN_WRITE_METHODS:
            assert not hasattr(client, method)


def test_registry_covers_ten_archetypes_and_four_phase1_families() -> None:
    archetypes = target_archetypes()
    assert len(archetypes) == 11
    assert CatalogueArchetype.MATCH_RESULT_1X2 in archetypes
    assert CatalogueArchetype.TEAM_TO_SCORE in archetypes
    assert CatalogueArchetype.TEAM_CLEAN_SHEET in archetypes
    assert REGISTRY_SHARED_BY == ("hot", "universe")
    assert REGISTRY_PARENT_COMMIT == "cf541aa857b19efb2d96cce54489c916bc1a38ba"
    assert REGISTRY_VERSION == "v4"
    four = phase1_four_family_archetypes()
    assert four == (
        CatalogueArchetype.MATCH_RESULT_1X2,
        CatalogueArchetype.BOTH_TEAMS_TO_SCORE,
        CatalogueArchetype.TOTAL_GOALS_HALF_LINE,
        CatalogueArchetype.FIRST_TEAM_TO_SCORE,
    )
    cell = registry_cell(CatalogueArchetype.MATCH_RESULT_1X2, "matchbook_kalshi")
    assert cell.target_archetype is True
    assert cell.phase1_four_family is True
    assert cell.venue_available is True
    assert cell.settlement_proven is False
    assert cell.paper_assumed_operational is True
    assert cell.operational_state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    dnb = registry_cell(CatalogueArchetype.DRAW_NO_BET, "matchbook_kalshi")
    assert dnb.venue_available is False
    assert dnb.phase1_four_family is False
    assert dnb.diagnostic_state is CatalogueCoverageState.VENUE_UNAVAILABLE
    assert family_is_phase1_expensive_work(MarketFamily.DRAW_NO_BET) is False
    assert family_is_phase1_expensive_work(MarketFamily.MATCH_RESULT) is True
    assert "GAME" in kalshi_series_suffixes()
    assert "BTTS" in kalshi_series_suffixes()
    assert "TOTAL" in kalshi_series_suffixes()
    assert "FTTS" in kalshi_series_suffixes()
    assert "DNB" not in kalshi_series_suffixes()
    markdown = render_registry_markdown()
    assert "PAPER_ASSUMED_EQUIVALENT" in markdown
    assert "VENUE_UNAVAILABLE" in markdown


def test_hot_and_universe_share_registry_families() -> None:
    families = target_market_families()
    assert MarketFamily.MATCH_RESULT in families
    from sports_hedge.catalogue.classify import classify_pair

    assert "scan_lane" not in classify_pair.__code__.co_varnames
    series = catalogue_relevant_kalshi_series()
    assert "KXEPLGAME" in series
    assert "KXEPLBTTS" in series
    assert "KXEPLTOTAL" in series
    assert "KXEPLFTTS" in series
    assert "KXEPLDNB" not in series


def test_gamewin_is_paper_assumed_and_proven_90m_is_approved() -> None:
    mb_event = _mb_event()
    unknown = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=mb_event, markets=[_mb_match_odds()]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=_kalshi_game_event(rules=GAMEWIN_TEMPLATE),
            markets=list(_kalshi_game_event(rules=GAMEWIN_TEMPLATE)["markets"]),
            series=_series("KXEPLGAME"),
        ),
    )
    assert unknown.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert unknown.settlement_assumption == "regulation_time"
    assert unknown.execution_eligible is False
    mb = PayloadSide(venue=VenueName.MATCHBOOK, event=mb_event, markets=[_mb_match_odds()])
    kalshi_unknown = PayloadSide(
        venue=VenueName.KALSHI,
        event=_kalshi_game_event(rules=GAMEWIN_TEMPLATE),
        markets=list(_kalshi_game_event(rules=GAMEWIN_TEMPLATE)["markets"]),
        series=_series("KXEPLGAME"),
    )
    from sports_hedge.catalogue.classify import normalize_payload_side

    left = normalize_payload_side(mb)
    right = normalize_payload_side(kalshi_unknown)
    assert catalogue_allows_solver(left, right) is True
    assert catalogue_allows_live_execution(left, right) is False
    proven = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=mb_event, markets=[_mb_match_odds()]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=_kalshi_game_event(rules=REGULATION),
            markets=list(_kalshi_game_event(rules=REGULATION)["markets"]),
            series=_series("KXEPLGAME"),
        ),
    )
    assert proven.state is CatalogueApprovalState.APPROVED_EQUIVALENT
    et = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=mb_event, markets=[_mb_match_odds()]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=_kalshi_game_event(rules=ET_RULES),
            markets=list(_kalshi_game_event(rules=ET_RULES)["markets"]),
            series=_series("KXEPLGAME"),
        ),
    )
    assert et.state is CatalogueApprovalState.UNSUPPORTED
    assert et.paper_mode_admitted is False
    assert et.state is not CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    fair = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=mb_event, markets=[_mb_match_odds()]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=_kalshi_game_event(rules=REGULATION, secondary=CANCEL_RESCHEDULE_FAIR_PRICE),
            markets=list(
                _kalshi_game_event(rules=REGULATION, secondary=CANCEL_RESCHEDULE_FAIR_PRICE)["markets"]
            ),
            series=_series("KXEPLGAME"),
        ),
    )
    assert fair.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert fair.state is not CatalogueApprovalState.APPROVED_EQUIVALENT
    assert fair.execution_eligible is False
    assert fair.paper_mode_admitted is True
    incomplete = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=mb_event, markets=[_mb_match_odds()]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=_kalshi_game_event(rules=GAMEWIN_TEMPLATE, drop_draw=True),
            markets=list(
                _kalshi_game_event(rules=GAMEWIN_TEMPLATE, drop_draw=True)["markets"]
            ),
            series=_series("KXEPLGAME"),
        ),
    )
    assert incomplete.state is not CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert incomplete.state is not CatalogueApprovalState.APPROVED_EQUIVALENT
    totals = classify_payload_pair(
        PayloadSide(venue=VenueName.MATCHBOOK, event=mb_event, markets=[_mb_totals("2.5")]),
        PayloadSide(
            venue=VenueName.KALSHI,
            event=_kalshi_total_event("3.5"),
            markets=list(_kalshi_total_event("3.5")["markets"]),
            series=_series("KXEPLTOTAL"),
        ),
    )
    assert totals.state is CatalogueApprovalState.APPROVED_PARAMETER_MISMATCH


def test_team_registry_covers_target_competitions_and_collapses_fc_suffix() -> None:
    assert resolve_team_name("Brentford FC") == resolve_team_name("Brentford")
    assert resolve_team_name("Chelsea FC") == resolve_team_name("Chelsea")
    assert canonical_team_id("Brentford FC") == canonical_team_id("Brentford")
    assert {club.canonical_name for club in clubs_for(PREMIER_LEAGUE)} >= {
        "Arsenal",
        "Brentford",
        "Chelsea",
    }
    assert any(club.canonical_name == "Athletic Club" for club in clubs_for(LA_LIGA))
    assert any(club.canonical_name == "Bayern Munich" for club in clubs_for(BUNDESLIGA))
    assert any(club.canonical_name == "AC Milan" for club in clubs_for(SERIE_A))
    assert resolve_team_name("FC Bayern München") == "bayern munich"
    assert resolve_team_name("AC Monza") == "monza"
    assert resolve_team_name("Chelsea Women") == "chelsea women"


@pytest.mark.asyncio
async def test_four_family_fixture_surfaces_paper_assumed_1x2_and_proven_siblings(
    tmp_path: Any,
) -> None:
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): _mb_markets_with_exotic()})
    kalshi = OverlapKalshi(
        [
            _kalshi_game_event(rules=GAMEWIN_TEMPLATE),
            _kalshi_btts_event(),
            _kalshi_total_event("2.5"),
            _kalshi_ftts_event(),
        ],
        series_by_ticker={
            "KXEPLGAME": _series("KXEPLGAME"),
            "KXEPLBTTS": _series("KXEPLBTTS"),
            "KXEPLTOTAL": _series("KXEPLTOTAL"),
            "KXEPLFTTS": _series("KXEPLFTTS"),
        },
        books=_all_books(),
    )
    report = await _collect(matchbook, kalshi)
    matched = [
        item
        for item in report.discovered_fixtures
        if item.matchbook_matched and item.kalshi_matched
    ]
    assert len(matched) == 1
    fixture = matched[0]
    assert fixture.matched_equivalent_count == 4
    rows = report.fixture_markets[fixture.canonical_event_id]
    comparable = {
        row.family: row.comparison_status
        for row in rows
        if row.comparison_status
        in {
            InventoryComparisonStatus.MATCHED_EQUIVALENT,
            InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT,
        }
    }
    assert comparable["match_result"] is InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT
    assert comparable["both_teams_to_score"] is InventoryComparisonStatus.MATCHED_EQUIVALENT
    assert comparable["total_goals"] is InventoryComparisonStatus.MATCHED_EQUIVALENT
    assert comparable["first_team_to_score"] is InventoryComparisonStatus.MATCHED_EQUIVALENT
    coverage = fixture.catalogue_coverage or fixture_catalogue_coverage(
        rows,
        matchbook_matched=True,
        kalshi_matched=True,
        target_competition_code="premier_league",
    )
    by_label = {row.display_label: row for row in coverage.rows}
    assert by_label["1X2"].state is CatalogueCoverageState.PAPER_ASSUMED_EQUIVALENT
    assert "regulation" in by_label["1X2"].reason.casefold() or by_label[
        "1X2"
    ].reason == "paper_assumed_equivalent"
    assert by_label["BTTS"].state is CatalogueCoverageState.APPROVED_EQUIVALENT
    assert by_label["FTTS"].state is CatalogueCoverageState.APPROVED_EQUIVALENT
    total_row = next(row for row in coverage.rows if row.display_label.startswith("TOTAL"))
    assert total_row.state is CatalogueCoverageState.APPROVED_EQUIVALENT
    assert by_label["DNB"].state is CatalogueCoverageState.VENUE_UNAVAILABLE
    assert by_label["DOUBLE CHANCE"].state is CatalogueCoverageState.VENUE_UNAVAILABLE
    census = census_from_report(report, data_class="deterministic_fixture")
    assert census.catalogue_by_archetype["match_result_1x2"]["paper_assumed_equivalent"] >= 1
    assert census.catalogue_by_archetype["both_teams_to_score"]["approved_equivalent"] >= 1
    assert census.catalogue_by_archetype["draw_no_bet"]["venue_unavailable"] >= 1
    book_tickers = set(kalshi.book_calls)
    assert "KXEPLGAME-26SEP20BRECHE-BRE" in book_tickers
    assert "KXEPLBTTS-26SEP20BRECHE-BTTS" in book_tickers
    assert "KXEPLTOTAL-26SEP20BRECHE-2.5" in book_tickers
    assert "KXEPLFTTS-26SEP20BRECHE-BRE" in book_tickers
    assert "DNB" not in "".join(kalshi.book_calls)
    quoted_mb = {
        row.matchbook.source_market_id
        for row in rows
        if row.matchbook and row.matchbook.best_backs
    }
    assert "316010" in quoted_mb
    assert "316020" in quoted_mb
    assert "316050" not in quoted_mb
    relationships = [
        item
        for group in relationships_from_fixture_markets(report.fixture_markets).values()
        for item in group
    ]
    proofs = {item.family: item.proof_status for item in relationships}
    assert proofs.get("match_result") == InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT.value
    assert proofs.get("both_teams_to_score") == InventoryComparisonStatus.MATCHED_EQUIVALENT.value

    bundle_path = tmp_path / "issue316-four-family.replay-bundle.json"
    capture = await attempt_live_read_only_capture(
        matchbook_client=OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): _mb_markets()}),
        kalshi_client=OverlapKalshi(
            [
                _kalshi_game_event(rules=GAMEWIN_TEMPLATE),
                _kalshi_btts_event(),
                _kalshi_total_event("2.5"),
                _kalshi_ftts_event(),
            ],
            series_by_ticker={
                "KXEPLGAME": _series("KXEPLGAME"),
                "KXEPLBTTS": _series("KXEPLBTTS"),
                "KXEPLTOTAL": _series("KXEPLTOTAL"),
                "KXEPLFTTS": _series("KXEPLFTTS"),
            },
            books=_all_books(),
        ),
        fx_snapshots=_fx(),
        venue_costs=_costs(),
        bundle_out=bundle_path,
    )
    assert capture.same_event_overlap_found is True
    saved = ReplayBundle.model_validate_json(bundle_path.read_text(encoding="utf-8"))
    series = {str(event.get("series_ticker")) for event in saved.kalshi.events} or {
        str(saved.kalshi.event.get("series_ticker") if saved.kalshi.event else "")
    }
    assert "KXEPLGAME" in series or any(
        "GAME" in str(market.get("ticker") or "") for market in saved.kalshi.markets
    )
    assert "KXEPLBTTS" in series
    replay_report, summary = await replay_bundle(saved)
    assert summary.network_used is False
    replay_fixture = next(
        item
        for item in replay_report.discovered_fixtures
        if item.matchbook_matched and item.kalshi_matched
    )
    replay_rows = replay_report.fixture_markets[replay_fixture.canonical_event_id]
    replay_families = {
        row.family
        for row in replay_rows
        if row.comparison_status
        in {
            InventoryComparisonStatus.MATCHED_EQUIVALENT,
            InventoryComparisonStatus.PAPER_ASSUMED_EQUIVALENT,
        }
    }
    assert replay_families == {
        "match_result",
        "both_teams_to_score",
        "total_goals",
        "first_team_to_score",
    }


@pytest.mark.asyncio
async def test_exact_line_mismatch_blocks_totals_without_dropping_siblings() -> None:
    matchbook = OverlapMatchbook([_mb_event()], {str(MB_EVENT_ID): [_mb_match_odds(), _mb_totals("2.5"), _mb_btts()]})
    kalshi = OverlapKalshi(
        [
            _kalshi_game_event(rules=GAMEWIN_TEMPLATE),
            _kalshi_btts_event(),
            _kalshi_total_event("3.5"),
        ],
        series_by_ticker={
            "KXEPLGAME": _series("KXEPLGAME"),
            "KXEPLBTTS": _series("KXEPLBTTS"),
            "KXEPLTOTAL": _series("KXEPLTOTAL"),
        },
        books=_all_books(),
    )
    report = await _collect(matchbook, kalshi)
    fixture = next(
        item
        for item in report.discovered_fixtures
        if item.matchbook_matched and item.kalshi_matched
    )
    rows = report.fixture_markets[fixture.canonical_event_id]
    totals = [row for row in rows if row.family == "total_goals"]
    assert totals
    assert all(
        row.comparison_status is not InventoryComparisonStatus.MATCHED_EQUIVALENT
        for row in totals
    )
    assert any(
        row.family == "both_teams_to_score"
        and row.comparison_status is InventoryComparisonStatus.MATCHED_EQUIVALENT
        for row in rows
    )
    coverage = {row.display_label: row for row in (fixture.catalogue_coverage.rows if fixture.catalogue_coverage else [])}
    assert coverage["1X2"].state is CatalogueCoverageState.PAPER_ASSUMED_EQUIVALENT
    total_row = next(row for row in coverage.values() if row.display_label.startswith("TOTAL"))
    assert total_row.state is CatalogueCoverageState.APPROVED_PARAMETER_MISMATCH


def _fx() -> list[FxRateSnapshot]:
    return [
        FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test_fx", captured_at=NOW),
        FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal("1"), source="functional_currency", captured_at=NOW),
    ]


def _series(ticker: str) -> dict[str, Any]:
    return {
        "ticker": ticker,
        "title": "Premier League",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
    }


def _costs() -> list[Any]:
    return [
        *matchbook_polymarket_costs(captured_at=NOW),
        kalshi_cost_from_series(_series("KXEPLGAME"), captured_at=NOW),
        kalshi_cost_from_series(_series("KXEPLBTTS"), captured_at=NOW),
        kalshi_cost_from_series(_series("KXEPLTOTAL"), captured_at=NOW),
        kalshi_cost_from_series(_series("KXEPLFTTS"), captured_at=NOW),
    ]


def _back(odds: str, amount: str = "80") -> dict[str, str]:
    return {"side": "back", "odds": odds, "available-amount": amount}


def _runner(runner_id: int, name: str, odds: str = "2.10") -> dict[str, Any]:
    return {"id": runner_id, "name": name, "prices": [_back(odds)]}


def _mb_event() -> dict[str, Any]:
    return {
        "id": MB_EVENT_ID,
        "name": f"{HOME} vs {AWAY}",
        "start": KICKOFF.isoformat(),
        "competition-name": COMPETITION,
    }


def _mb_match_odds() -> dict[str, Any]:
    return {
        "id": 316010,
        "name": "Match Odds",
        "runners": [_runner(1, HOME, "2.40"), _runner(2, "Draw", "3.40"), _runner(3, AWAY, "2.90")],
    }


def _mb_btts() -> dict[str, Any]:
    return {
        "id": 316020,
        "name": "Both Teams To Score",
        "runners": [_runner(11, "Yes", "1.90"), _runner(12, "No", "1.95")],
    }


def _mb_totals(line: str) -> dict[str, Any]:
    return {
        "id": 316030 if line == "2.5" else 316031,
        "name": f"Over/Under {line} Goals",
        "line": line,
        "runners": [_runner(21, f"Over {line}", "1.85"), _runner(22, f"Under {line}", "2.05")],
    }


def _mb_ftts() -> dict[str, Any]:
    return {
        "id": 316040,
        "name": "First Team To Score",
        "runners": [_runner(31, HOME, "2.20"), _runner(32, AWAY, "2.30"), _runner(33, "No Goal", "8.00")],
    }


def _mb_dnb() -> dict[str, Any]:
    return {
        "id": 316050,
        "name": "Draw No Bet",
        "runners": [_runner(41, HOME, "1.70"), _runner(42, AWAY, "2.10")],
    }


def _mb_markets() -> list[dict[str, Any]]:
    return [_mb_match_odds(), _mb_btts(), _mb_totals("2.5"), _mb_ftts()]


def _mb_markets_with_exotic() -> list[dict[str, Any]]:
    return [*_mb_markets(), _mb_dnb()]


def _kalshi_event(ticker: str, series: str, markets: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "event_ticker": ticker,
        "series_ticker": series,
        "title": f"{HOME} vs {AWAY}",
        "category": "Sports",
        "strike_date": KICKOFF.isoformat(),
        "milestone": {"start_date": KICKOFF.isoformat()},
        "product_metadata": {"competition": COMPETITION, "competition_scope": "Game"},
        "markets": markets,
    }


def _kalshi_game_event(
    *, rules: str, secondary: str | None = None, drop_draw: bool = False
) -> dict[str, Any]:
    ticker = "KXEPLGAME-26SEP20BRECHE"
    title = f"{HOME} vs {AWAY}"
    markets = [
        {
            "ticker": "KXEPLGAME-26SEP20BRECHE-BRE",
            "event_ticker": ticker,
            "title": title,
            "yes_sub_title": HOME,
            "rules_primary": rules,
            "rules_secondary": secondary,
        },
        {
            "ticker": "KXEPLGAME-26SEP20BRECHE-DRAW",
            "event_ticker": ticker,
            "title": title,
            "yes_sub_title": "Draw",
            "rules_primary": rules,
            "rules_secondary": secondary,
        },
        {
            "ticker": "KXEPLGAME-26SEP20BRECHE-CHE",
            "event_ticker": ticker,
            "title": title,
            "yes_sub_title": AWAY,
            "rules_primary": rules,
            "rules_secondary": secondary,
        },
    ]
    if drop_draw:
        markets = [markets[0], markets[2]]
    return _kalshi_event(ticker, "KXEPLGAME", markets)


def _kalshi_btts_event() -> dict[str, Any]:
    ticker = "KXEPLBTTS-26SEP20BRECHE"
    return _kalshi_event(
        ticker,
        "KXEPLBTTS",
        [
            {
                "ticker": "KXEPLBTTS-26SEP20BRECHE-BTTS",
                "event_ticker": ticker,
                "title": "Both Teams To Score",
                "yes_sub_title": "Yes",
                "rules_primary": REGULATION,
            }
        ],
    )


def _kalshi_total_event(line: str) -> dict[str, Any]:
    ticker = "KXEPLTOTAL-26SEP20BRECHE"
    return _kalshi_event(
        ticker,
        "KXEPLTOTAL",
        [
            {
                "ticker": f"KXEPLTOTAL-26SEP20BRECHE-{line}",
                "event_ticker": ticker,
                "title": f"{HOME} vs {AWAY} Total Goals {line}",
                "yes_sub_title": f"Over {line}",
                "rules_primary": REGULATION,
                "strike": line,
            }
        ],
    )


def _kalshi_ftts_event() -> dict[str, Any]:
    ticker = "KXEPLFTTS-26SEP20BRECHE"
    return _kalshi_event(
        ticker,
        "KXEPLFTTS",
        [
            {
                "ticker": "KXEPLFTTS-26SEP20BRECHE-BRE",
                "event_ticker": ticker,
                "title": "First team to score",
                "yes_sub_title": HOME,
                "rules_primary": REGULATION,
            },
            {
                "ticker": "KXEPLFTTS-26SEP20BRECHE-CHE",
                "event_ticker": ticker,
                "title": "First team to score",
                "yes_sub_title": AWAY,
                "rules_primary": REGULATION,
            },
            {
                "ticker": "KXEPLFTTS-26SEP20BRECHE-NG",
                "event_ticker": ticker,
                "title": "First team to score",
                "yes_sub_title": "No Goal",
                "rules_primary": REGULATION,
            },
        ],
    )


def _all_books() -> dict[str, dict[str, Any]]:
    tickers = [
        "KXEPLGAME-26SEP20BRECHE-BRE",
        "KXEPLGAME-26SEP20BRECHE-DRAW",
        "KXEPLGAME-26SEP20BRECHE-CHE",
        "KXEPLBTTS-26SEP20BRECHE-BTTS",
        "KXEPLTOTAL-26SEP20BRECHE-2.5",
        "KXEPLTOTAL-26SEP20BRECHE-3.5",
        "KXEPLFTTS-26SEP20BRECHE-BRE",
        "KXEPLFTTS-26SEP20BRECHE-CHE",
        "KXEPLFTTS-26SEP20BRECHE-NG",
    ]
    return {ticker: deepcopy(DEFAULT_KALSHI_BOOK) for ticker in tickers}


class _DisabledPolymarket:
    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        del filters
        return []

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return []


async def _collect(matchbook: OverlapMatchbook, kalshi: OverlapKalshi):
    repository = SqliteMarketIntelligenceRepository()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=_DisabledPolymarket(),
        kalshi=kalshi,
        paper_scan=PaperScanService(MarketIntelligenceService(repository)),
    )
    try:
        return await collector.collect_and_scan(
            venue_costs=_costs(),
            fx_snapshots=_fx(),
            maximum_execution_risk=100,
            enabled_venues=[VenueName.MATCHBOOK, VenueName.KALSHI],
            unbounded_cycle=True,
        )
    finally:
        repository.close()
