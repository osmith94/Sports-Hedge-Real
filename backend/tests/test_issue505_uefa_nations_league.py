"""Issue #505: onboard UEFA Nations League from live read-only provider evidence.

Census 2026-09-22 against owner-live ``cac3b25814c08707e66d7b9ac41564658a874aa0``:

- Polymarket Gamma GET /sports: sport ``unl``, series ``11446``, name UEFA Nations League.
  Fixture event ``1003661`` slug ``unl-nld-ger-2026-09-24``. Group/champion outrights
  such as ``994203`` have series=null / sport=null.
- Kalshi GET /series: match-level ``KXUEFANLGAME`` / ``KXUEFANLBTTS`` /
  ``KXUEFANLTOTAL`` / ``KXUEFANLFTTS``. Live GAME includes
  ``KXUEFANLGAME-26SEP24NEDGER`` and ``KXUEFANLGAME-26SEP24NORDEN``.
- Matchbook soccer id=15 COMPETITION tags: UEFA Nations League A/B/D.
  Netherlands vs Germany event ``34043854514400023``.

Reuses generic football MATCH_RESULT / BTTS / TOTAL / FTTS, #501 identity
throughput, #503 venue fees, catalogue/HOT exact IDs. No new scanner and no
concurrency increase. PAPER / read-only. Deterministic fixture/demo providers
in this file; identifiers above are live census facts, not quotes.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from test_issue293_owner_live_overlap import OverlapKalshi, OverlapMatchbook
from test_issue316_catalogue_registry import _DisabledPolymarket
from venue_cost_helpers import matchbook_polymarket_costs

from sports_hedge.application.approved_market_catalogue import (
    ApprovedMarketCatalogueRow,
    CatalogueRowState,
    OutcomeNativeId,
    derived_price_engine_working_set,
    required_outcomes_for_key,
)
from sports_hedge.application.capture_replay import DEFAULT_KALSHI_BOOK
from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.fixture_inventory import InventoryComparisonStatus
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.provider_access import DEFAULT_PROVIDER_CONCURRENCY
from sports_hedge.application.target_competitions import (
    DEFAULT_OPERATOR_COMPETITION_CODES,
    KALSHI_SERIES_TICKERS_BY_CODE,
    OPERATOR_COMPETITION_REGISTRY_VERSION,
    PRINCIPAL_OPERATOR_COMPETITION_COUNT,
    SEASON_PROPOSITION_NOT_FIXTURE,
    VERIFIED_ALL_3,
    TargetCompetitionCode,
    competition_has_verified_cross_venue_mapping,
    kalshi_series_tickers_for_codes,
    operator_competition_catalog,
    polymarket_series_ids_for_codes,
    resolve_target_competition,
    resolve_target_competition_from_kalshi_ticker,
    resolve_target_competition_from_series_id,
    scope_kalshi_event,
    scope_matchbook_event,
    scope_polymarket_event,
)
from sports_hedge.catalogue.corpus import GAMEWIN_TEMPLATE, REGULATION
from sports_hedge.config import Settings
from sports_hedge.domain.football import CanonicalEvent
from sports_hedge.domain.models import VenueName
from sports_hedge.facts.aliases import resolve_team_name, resolve_team_name_for_competition
from sports_hedge.facts.identity import canonical_team_id
from sports_hedge.facts.team_registry import (
    INTERNATIONAL_FRIENDLIES,
    UEFA_NATIONS_LEAGUE,
    clubs_for,
)
from sports_hedge.fees.kalshi import KALSHI_QUADRATIC_FORMULA, kalshi_cost_from_series
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.matching.approved_register import (
    CANONICAL_BTTS_FT,
    CANONICAL_FTTS_FT,
    CANONICAL_MATCH_RESULT_FT,
    CANONICAL_TOTAL_GOALS_FT,
)
from sports_hedge.matching.events import EventMatcher, known_target_competition_mismatch
from sports_hedge.paper.models import FxRateSnapshot
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore

KICKOFF = datetime(2026, 9, 24, 18, 45, tzinfo=UTC)
NORWAY_KICKOFF = datetime(2026, 9, 24, 18, 45, tzinfo=UTC)
UNL = "uefa_nations_league"
SCOPE = [UNL]
MB_NL_GER = "34043854514400023"
PM_NL_GER = "1003661"
KALSHI_NL_GER = "KXUEFANLGAME-26SEP24NEDGER"
KALSHI_NOR_DEN = "KXUEFANLGAME-26SEP24NORDEN"


def _mb_event(
    event_id: str | int,
    name: str,
    competition: str,
    *,
    kickoff: datetime = KICKOFF,
) -> dict[str, Any]:
    return {
        "id": int(event_id),
        "name": name,
        "start": kickoff.isoformat().replace("+00:00", ".000Z"),
        "status": "open",
        "sport-id": 15,
        "meta-tags": [
            {"name": "Soccer", "type": "SPORT"},
            {"name": competition, "type": "COMPETITION"},
        ],
    }


def _runner(runner_id: int, name: str, odds: str = "2.10") -> dict[str, Any]:
    return {
        "id": runner_id,
        "name": name,
        "prices": [
            {"side": "back", "odds": odds, "available-amount": "80"},
            {"side": "lay", "odds": "2.20", "available-amount": "80"},
        ],
    }


def _mb_markets(*, home: str, away: str) -> list[dict[str, Any]]:
    return [
        {
            "id": 101,
            "name": "Match Odds",
            "runners": [
                _runner(1, home, "2.40"),
                _runner(2, "Draw", "3.40"),
                _runner(3, away, "2.90"),
            ],
        },
        {
            "id": 102,
            "name": "Both Teams To Score",
            "runners": [_runner(11, "Yes", "1.90"), _runner(12, "No", "1.95")],
        },
        {
            "id": 103,
            "name": "Over/Under 2.5 Goals",
            "line": "2.5",
            "runners": [_runner(21, "Over 2.5", "1.85"), _runner(22, "Under 2.5", "2.05")],
        },
        {
            "id": 104,
            "name": "First Team To Score",
            "runners": [
                _runner(31, home, "1.80"),
                _runner(32, away, "2.10"),
                _runner(33, "No Goal", "11.0"),
            ],
        },
        {
            "id": 105,
            "name": "Correct Score",
            "runners": [_runner(41, "1-0", "8.0"), _runner(42, "0-0", "9.0")],
        },
        {
            "id": 106,
            "name": "Half Time",
            "runners": [_runner(51, home, "3.0"), _runner(52, "Draw", "2.2"), _runner(53, away, "3.5")],
        },
    ]


def _kalshi_event(
    ticker: str,
    series: str,
    title: str,
    markets: list[dict[str, Any]],
    *,
    kickoff: datetime = KICKOFF,
) -> dict[str, Any]:
    return {
        "event_ticker": ticker,
        "series_ticker": series,
        "title": title,
        "category": "Sports",
        "strike_date": kickoff.isoformat(),
        "milestone": {"start_date": kickoff.isoformat()},
        "product_metadata": {"competition": "UEFA Nations League", "competition_scope": "Game"},
        "markets": markets,
    }


def _kalshi_game(ticker: str, home: str, away: str, *, home_code: str, away_code: str) -> dict[str, Any]:
    return _kalshi_event(
        ticker,
        "KXUEFANLGAME",
        f"{home} vs {away}",
        [
            {
                "ticker": f"{ticker}-{home_code}",
                "event_ticker": ticker,
                "title": f"{home} wins",
                "yes_sub_title": home,
                "rules_primary": GAMEWIN_TEMPLATE,
            },
            {
                "ticker": f"{ticker}-TIE",
                "event_ticker": ticker,
                "title": "Tie is the result",
                "yes_sub_title": "Draw",
                "rules_primary": GAMEWIN_TEMPLATE,
            },
            {
                "ticker": f"{ticker}-{away_code}",
                "event_ticker": ticker,
                "title": f"{away} wins",
                "yes_sub_title": away,
                "rules_primary": GAMEWIN_TEMPLATE,
            },
        ],
    )


def _kalshi_btts(ticker: str, home: str, away: str) -> dict[str, Any]:
    return _kalshi_event(
        ticker,
        "KXUEFANLBTTS",
        f"{home} vs {away}: BTTS",
        [
            {
                "ticker": f"{ticker}-BTTS",
                "event_ticker": ticker,
                "title": "Both Teams To Score",
                "yes_sub_title": "Yes",
                "rules_primary": REGULATION,
            }
        ],
    )


def _kalshi_total(ticker: str, home: str, away: str) -> dict[str, Any]:
    return _kalshi_event(
        ticker,
        "KXUEFANLTOTAL",
        f"{home} vs {away}",
        [
            {
                "ticker": f"{ticker}-2.5",
                "event_ticker": ticker,
                "title": f"{home} vs {away} Total Goals 2.5",
                "yes_sub_title": "Over 2.5",
                "strike": "2.5",
                "rules_primary": REGULATION,
            }
        ],
    )


def _kalshi_ftts(ticker: str, home: str, away: str, *, home_code: str, away_code: str) -> dict[str, Any]:
    return _kalshi_event(
        ticker,
        "KXUEFANLFTTS",
        f"{home} vs {away}",
        [
            {
                "ticker": f"{ticker}-{home_code}",
                "event_ticker": ticker,
                "title": "First team to score",
                "yes_sub_title": home,
                "rules_primary": REGULATION,
            },
            {
                "ticker": f"{ticker}-{away_code}",
                "event_ticker": ticker,
                "title": "First team to score",
                "yes_sub_title": away,
                "rules_primary": REGULATION,
            },
            {
                "ticker": f"{ticker}-NG",
                "event_ticker": ticker,
                "title": "First team to score",
                "yes_sub_title": "No Goal",
                "rules_primary": REGULATION,
            },
        ],
    )


UNL_SERIES = {
    "KXUEFANLGAME": {
        "ticker": "KXUEFANLGAME",
        "title": "UEFA Nations League Game",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
        "contract_terms_url": "https://assets.kalshi.com/contract_terms/SOCCERGAMEWIN.pdf",
    },
    "KXUEFANLBTTS": {
        "ticker": "KXUEFANLBTTS",
        "title": "BTTS",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
        "contract_terms_url": "https://assets.kalshi.com/contract_terms/SOCCERBTTS.pdf",
    },
    "KXUEFANLTOTAL": {
        "ticker": "KXUEFANLTOTAL",
        "title": "Point Total",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
        "contract_terms_url": "https://assets.kalshi.com/contract_terms/SOCCERTOTAL.pdf",
    },
    "KXUEFANLFTTS": {
        "ticker": "KXUEFANLFTTS",
        "title": "First Team to Score",
        "fee_type": "quadratic",
        "fee_multiplier": 1,
        "contract_terms_url": "https://assets.kalshi.com/contract_terms/SOCCERTEAMTOSCOREFIRST.pdf",
    },
}


def _books() -> dict[str, dict[str, Any]]:
    tickers = [
        f"{KALSHI_NL_GER}-NED",
        f"{KALSHI_NL_GER}-TIE",
        f"{KALSHI_NL_GER}-GER",
        "KXUEFANLBTTS-26SEP24NEDGER-BTTS",
        "KXUEFANLTOTAL-26SEP24NEDGER-2.5",
        "KXUEFANLFTTS-26SEP24NEDGER-NED",
        "KXUEFANLFTTS-26SEP24NEDGER-GER",
        "KXUEFANLFTTS-26SEP24NEDGER-NG",
        f"{KALSHI_NOR_DEN}-NOR",
        f"{KALSHI_NOR_DEN}-TIE",
        f"{KALSHI_NOR_DEN}-DEN",
    ]
    return {ticker: deepcopy(DEFAULT_KALSHI_BOOK) for ticker in tickers}


def _costs() -> list[Any]:
    captured = KICKOFF
    return [
        *matchbook_polymarket_costs(captured_at=captured),
        *[kalshi_cost_from_series(series, captured_at=captured) for series in UNL_SERIES.values()],
    ]


def _fx() -> list[FxRateSnapshot]:
    return [
        FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"), source="test_fx", captured_at=KICKOFF),
        FxRateSnapshot(currency="GBP", gbp_per_unit=Decimal(1), source="functional_currency", captured_at=KICKOFF),
    ]


def _canonical(home: str, away: str, competition: str, venue: VenueName, source_id: str) -> CanonicalEvent:
    return CanonicalEvent(
        competition=competition,
        home_team=home,
        away_team=away,
        kickoff_utc=KICKOFF,
        source_venue=venue,
        source_event_id=source_id,
    )


def test_nations_league_is_selectable_under_uefa_and_not_default() -> None:
    catalog = {row["code"]: row for row in operator_competition_catalog()}
    assert len(catalog) == PRINCIPAL_OPERATOR_COMPETITION_COUNT == 33
    row = catalog[UNL]
    assert row["display_name"] == "UEFA Nations League"
    assert row["selector_label"] == "Nations League"
    assert row["group_id"] == "uefa"
    assert row["group_label"] == "UEFA"
    assert row["selectable"] is True
    assert row["default_selected"] is False
    assert row["verification_status"] == VERIFIED_ALL_3
    assert row["market_scope"] == "FIXTURE_MATCH"
    item = resolve_target_competition("UEFA Nations League")
    assert item is not None
    assert competition_has_verified_cross_venue_mapping(item)
    assert item.code not in DEFAULT_OPERATOR_COMPETITION_CODES
    assert OPERATOR_COMPETITION_REGISTRY_VERSION == 6
    settings = Settings()
    assert "11446" not in settings.resolved_polymarket_series_ids()
    assert "KXUEFANLGAME" not in settings.kalshi_series_tickers
    assert polymarket_series_ids_for_codes([UNL]) == ["11446"]
    assert kalshi_series_tickers_for_codes([UNL]) == [
        "KXUEFANLGAME",
        "KXUEFANLBTTS",
        "KXUEFANLTOTAL",
        "KXUEFANLFTTS",
    ]
    assert KALSHI_SERIES_TICKERS_BY_CODE[TargetCompetitionCode.UEFA_NATIONS_LEAGUE] == (
        "KXUEFANLGAME",
        "KXUEFANLBTTS",
        "KXUEFANLTOTAL",
        "KXUEFANLFTTS",
    )


@pytest.mark.parametrize(
    "label",
    (
        "UEFA Nations League",
        "Nations League",
        "UNL",
        "UEFA Nations League 2026/27",
        "UEFA Nations League A",
        "UEFA Nations League B",
        "UEFA Nations League D",
    ),
)
def test_observed_nations_league_aliases_resolve(label: str) -> None:
    resolved = resolve_target_competition(label)
    assert resolved is not None
    assert resolved.code is TargetCompetitionCode.UEFA_NATIONS_LEAGUE


@pytest.mark.parametrize(
    "label",
    (
        "CONCACAF Nations League",
        "Concacaf Nations League",
        "UEFA Women's Nations League",
        "UEFA Nations League C",
        "Volleyball Nations League",
        "International Friendlies",
        "UEFA Euro Qualification",
        "Europe WC Qualifiers",
        "UEFA Champions League",
        "Six Nations",
    ),
)
def test_neighbor_labels_are_not_nations_league(label: str) -> None:
    resolved = resolve_target_competition(label)
    if resolved is None:
        return
    assert resolved.code is not TargetCompetitionCode.UEFA_NATIONS_LEAGUE


def test_provider_mapping_uses_observed_identifiers_only() -> None:
    assert resolve_target_competition_from_series_id("11446") is not None
    assert resolve_target_competition_from_series_id("11446").code is TargetCompetitionCode.UEFA_NATIONS_LEAGUE
    assert resolve_target_competition_from_series_id("10673") is None
    assert resolve_target_competition("unl") is not None
    assert resolve_target_competition("conl") is None
    for ticker in ("KXUEFANLGAME", "KXUEFANLBTTS", "KXUEFANLTOTAL", "KXUEFANLFTTS", KALSHI_NL_GER):
        resolved = resolve_target_competition_from_kalshi_ticker(ticker)
        assert resolved is not None
        assert resolved.code is TargetCompetitionCode.UEFA_NATIONS_LEAGUE
    for ticker in (
        "KXUEFANL",
        "KXUEFANLSPREAD",
        "KXUEFANLSCORE",
        "KXUEFANL1H",
        "KXUEFANLTEAMTOTAL",
        "KXUEFANLADVANCE",
        "KXUEFANLMOV",
        "KXCONCACAFNL",
        "KXCONCACAFNL-27A",
    ):
        assert resolve_target_competition_from_kalshi_ticker(ticker) is None


def test_matchbook_live_division_tags_scope_when_selected() -> None:
    for competition in (
        "UEFA Nations League A",
        "UEFA Nations League B",
        "UEFA Nations League D",
    ):
        decision = scope_matchbook_event(
            _mb_event(MB_NL_GER, "Netherlands vs Germany", competition),
            selected_codes=SCOPE,
        )
        assert decision.allowed is True
        assert decision.competition is not None
        assert decision.competition.code is TargetCompetitionCode.UEFA_NATIONS_LEAGUE
    unselected = scope_matchbook_event(
        _mb_event(MB_NL_GER, "Netherlands vs Germany", "UEFA Nations League A"),
    )
    assert unselected.allowed is False
    concacaf = scope_matchbook_event(
        _mb_event("9", "USA vs Mexico", "CONCACAF Nations League"),
        selected_codes=SCOPE,
    )
    assert concacaf.allowed is False
    league_c = scope_matchbook_event(
        _mb_event("10", "Georgia vs Bulgaria", "UEFA Nations League C"),
        selected_codes=SCOPE,
    )
    assert league_c.allowed is False


def test_polymarket_fixture_series_and_outrights() -> None:
    fixture = scope_polymarket_event(
        {
            "id": PM_NL_GER,
            "title": "Netherlands vs. Germany",
            "slug": "unl-nld-ger-2026-09-24",
            "startTime": KICKOFF.isoformat(),
            "sport": {"sport": "unl", "series": "11446", "name": "UEFA Nations League"},
            "series": [{"id": "11446", "title": "UEFA Nations League", "ticker": "soccer-unl"}],
        },
        selected_codes=SCOPE,
    )
    assert fixture.allowed is True
    assert fixture.competition is not None
    assert fixture.competition.code is TargetCompetitionCode.UEFA_NATIONS_LEAGUE

    more_markets = scope_polymarket_event(
        {
            "id": "1003798",
            "title": "Netherlands vs. Germany - More Markets",
            "slug": "unl-nld-ger-2026-09-24-more-markets",
            "startTime": KICKOFF.isoformat(),
            "series": [{"id": "11446", "title": "UEFA Nations League"}],
        },
        selected_codes=SCOPE,
    )
    assert more_markets.allowed is True

    winner = scope_polymarket_event(
        {
            "id": "994203",
            "title": "UEFA Nations League Winner 2026-27",
            "slug": "uefa-nations-league-winner-2026-27",
            "series": None,
            "sport": None,
            "competition": "UEFA Nations League",
        },
        selected_codes=SCOPE,
    )
    assert winner.allowed is False
    assert winner.reason == SEASON_PROPOSITION_NOT_FIXTURE

    group = scope_polymarket_event(
        {
            "id": "994211",
            "title": "UEFA Nations League 2026-27: Group B4 Winner",
            "slug": "uefa-nations-league-2026-27-group-b4-winner",
            "competition": "UEFA Nations League",
        },
        selected_codes=SCOPE,
    )
    assert group.allowed is False
    assert group.reason == SEASON_PROPOSITION_NOT_FIXTURE

    relegation = scope_polymarket_event(
        {
            "id": "994335",
            "title": "UEFA Nations League A: Teams relegated (2026-27)",
            "slug": "uefa-nations-league-a-teams-relegated-2026-27",
            "competition": "UEFA Nations League",
        },
        selected_codes=SCOPE,
    )
    assert relegation.allowed is False

    unlabeled_winner = scope_polymarket_event(
        {
            "id": "994203",
            "title": "UEFA Nations League Winner 2026-27",
            "slug": "uefa-nations-league-winner-2026-27",
            "series": None,
            "sport": None,
        },
        selected_codes=SCOPE,
    )
    assert unlabeled_winner.allowed is False

    concacaf = scope_polymarket_event(
        {
            "id": "994373",
            "title": "CONCACAF Nations League 2026-27 Winner",
            "sport": {"sport": "conl", "series": "10673", "name": "CONCACAF Nations League"},
            "series": [{"id": "10673", "title": "CONCACAF Nations League"}],
        },
        selected_codes=SCOPE,
    )
    assert concacaf.allowed is False


def test_kalshi_game_scopes_and_season_series_does_not() -> None:
    game = scope_kalshi_event(
        {
            "event_ticker": KALSHI_NL_GER,
            "series_ticker": "KXUEFANLGAME",
            "title": "Netherlands vs Germany",
            "product_metadata": {"competition": "UEFA Nations League"},
        },
        selected_codes=SCOPE,
    )
    assert game.allowed is True
    norway = scope_kalshi_event(
        {
            "event_ticker": KALSHI_NOR_DEN,
            "series_ticker": "KXUEFANLGAME",
            "title": "Norway vs Denmark",
        },
        selected_codes=SCOPE,
    )
    assert norway.allowed is True
    season = scope_kalshi_event(
        {"event_ticker": "KXUEFANL-27", "series_ticker": "KXUEFANL", "title": "UEFA Nations League"},
        selected_codes=SCOPE,
    )
    assert season.allowed is False
    assert season.reason == SEASON_PROPOSITION_NOT_FIXTURE
    spread = scope_kalshi_event(
        {
            "event_ticker": "KXUEFANLSPREAD-26SEP24NEDGER",
            "series_ticker": "KXUEFANLSPREAD",
            "title": "Netherlands vs Germany",
            "product_metadata": {"competition": "UEFA Nations League"},
        },
        selected_codes=SCOPE,
    )
    assert spread.allowed is False


def test_country_identity_reuses_national_teams_and_fails_closed() -> None:
    assert clubs_for(UEFA_NATIONS_LEAGUE) == clubs_for(INTERNATIONAL_FRIENDLIES)
    assert resolve_team_name("Netherlands") == resolve_team_name("Holland") == "netherlands"
    assert resolve_team_name("Turkiye") == resolve_team_name("Turkey") == "turkey"
    assert resolve_team_name("Ireland") == resolve_team_name("Republic of Ireland") == "republic of ireland"
    assert resolve_team_name("Northern Ireland") == "northern ireland"
    assert resolve_team_name("Bosnia-Herzegovina") == "bosnia and herzegovina"
    assert resolve_team_name("Czechia") == "czech republic"
    assert resolve_team_name_for_competition("Netherlands", UEFA_NATIONS_LEAGUE) == "netherlands"
    assert canonical_team_id("Netherlands", UEFA_NATIONS_LEAGUE) == canonical_team_id(
        "Holland", INTERNATIONAL_FRIENDLIES
    )
    assert resolve_team_name("Macedonia") == "macedonia"
    assert resolve_team_name("North Macedonia") == "north macedonia"


def test_friendlies_do_not_cross_match_nations_league() -> None:
    assert known_target_competition_mismatch("International Friendlies", "UEFA Nations League") is True
    matcher = EventMatcher()
    result = matcher.match(
        _canonical("Netherlands", "Germany", "International Friendlies", VenueName.MATCHBOOK, "1"),
        _canonical("Netherlands", "Germany", "UEFA Nations League A", VenueName.KALSHI, "2"),
    )
    assert result.matched is False


def test_generic_venue_fee_path_not_sport_specific() -> None:
    unl = kalshi_cost_from_series(UNL_SERIES["KXUEFANLGAME"], captured_at=KICKOFF, source_market_id=KALSHI_NL_GER)
    epl = kalshi_cost_from_series(
        {
            "ticker": "KXEPLGAME",
            "fee_type": "quadratic",
            "fee_multiplier": 1,
        },
        captured_at=KICKOFF,
        source_market_id="KXEPLGAME-X",
    )
    nfl = kalshi_cost_from_series(
        {
            "ticker": "KXNFLGAME",
            "fee_type": "quadratic_with_maker_fees",
            "fee_multiplier": 1,
        },
        captured_at=KICKOFF,
        source_market_id="KXNFLGAME-X",
    )
    assert unl.formula_name == epl.formula_name == KALSHI_QUADRATIC_FORMULA
    assert unl.known_status == epl.known_status
    assert unl.formula_parameters["coefficient"] == epl.formula_parameters["coefficient"]
    assert nfl.formula_name == KALSHI_QUADRATIC_FORMULA
    assert "uefa" not in (unl.detail or "").casefold()
    assert "nations" not in (unl.detail or "").casefold()


def test_provider_concurrency_and_soccer_nfl_defaults_unchanged() -> None:
    settings = Settings()
    assert DEFAULT_PROVIDER_CONCURRENCY == {
        VenueName.MATCHBOOK: 4,
        VenueName.POLYMARKET: 8,
        VenueName.KALSHI: 4,
    }
    assert settings.paper_scan_matchbook_concurrency == 4
    assert settings.paper_scan_kalshi_concurrency == 4
    assert settings.sports_hedge_mode == "paper"
    assert settings.sports_hedge_execution_enabled is False
    assert tuple(code.value for code in DEFAULT_OPERATOR_COMPETITION_CODES) == (
        "premier_league",
        "championship",
        "la_liga",
        "carabao_cup",
        "fa_cup",
        "international_friendlies",
        "bundesliga",
        "serie_a",
    )
    catalog = {row["code"]: row for row in operator_competition_catalog()}
    assert catalog["nfl"]["selectable"] is True
    assert catalog["nfl"]["default_selected"] is False
    assert catalog["nba"]["selectable"] is True
    assert catalog["nba"]["default_selected"] is False
    assert catalog["premier_league"]["selectable"] is True


def test_catalogue_exact_id_hot_reconstruction_uses_generic_football_keys() -> None:
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        row = ApprovedMarketCatalogueRow(
            catalogue_row_id="amc-unl-nld-ger",
            register_canonical_key=CANONICAL_MATCH_RESULT_FT,
            canonical_event_id="evt-unl-nld-ger",
            competition="UEFA Nations League",
            home_canonical="Netherlands",
            away_canonical="Germany",
            kickoff_utc=KICKOFF,
            matchbook_event_id=MB_NL_GER,
            matchbook_market_id="101",
            matchbook_runner_ids=[
                OutcomeNativeId(outcome=outcome, native_id=f"mb-{outcome}")
                for outcome in required_outcomes_for_key(CANONICAL_MATCH_RESULT_FT)
            ],
            kalshi_event_ticker=KALSHI_NL_GER,
            kalshi_market_tickers=[f"{KALSHI_NL_GER}-NED", f"{KALSHI_NL_GER}-TIE", f"{KALSHI_NL_GER}-GER"],
            polymarket_event_id=PM_NL_GER,
            polymarket_market_id="pm-nld-ger-1x2",
            family="match_result",
            period="full_time",
            required_outcomes=required_outcomes_for_key(CANONICAL_MATCH_RESULT_FT),
            row_state=CatalogueRowState.ACTIVE,
            first_catalogued_at=KICKOFF,
        )
        stored = store.upsert_catalogue_row(row)
        assert stored.register_canonical_key == CANONICAL_MATCH_RESULT_FT
        assert stored.matchbook_event_id == MB_NL_GER
        assert stored.kalshi_event_ticker == KALSHI_NL_GER
        assert stored.polymarket_event_id == PM_NL_GER
        working = derived_price_engine_working_set([stored])
        assert len(working) == 1
        assert working[0].catalogue_row_id == "amc-unl-nld-ger"
        assert working[0].matchbook_event_id == MB_NL_GER
        assert working[0].kalshi_event_ticker == KALSHI_NL_GER
        assert working[0].register_canonical_key == CANONICAL_MATCH_RESULT_FT
        assert working[0].register_canonical_key in {
            CANONICAL_MATCH_RESULT_FT,
            CANONICAL_BTTS_FT,
            CANONICAL_TOTAL_GOALS_FT,
            CANONICAL_FTTS_FT,
        }
    finally:
        store.close()


@pytest.mark.asyncio
async def test_current_fixtures_normalize_and_only_supported_families_attach() -> None:
    matchbook = OverlapMatchbook(
        [
            _mb_event(MB_NL_GER, "Netherlands vs Germany", "UEFA Nations League A"),
            _mb_event("34042288719400023", "Norway vs Denmark", "UEFA Nations League A", kickoff=NORWAY_KICKOFF),
            _mb_event("5003", "England vs France", "International Friendlies"),
        ],
        {
            MB_NL_GER: _mb_markets(home="Netherlands", away="Germany"),
            "34042288719400023": _mb_markets(home="Norway", away="Denmark"),
            "5003": _mb_markets(home="England", away="France"),
        },
    )
    kalshi = OverlapKalshi(
        [
            _kalshi_game(KALSHI_NL_GER, "Netherlands", "Germany", home_code="NED", away_code="GER"),
            _kalshi_btts("KXUEFANLBTTS-26SEP24NEDGER", "Netherlands", "Germany"),
            _kalshi_total("KXUEFANLTOTAL-26SEP24NEDGER", "Netherlands", "Germany"),
            _kalshi_ftts(
                "KXUEFANLFTTS-26SEP24NEDGER",
                "Netherlands",
                "Germany",
                home_code="NED",
                away_code="GER",
            ),
            _kalshi_game(KALSHI_NOR_DEN, "Norway", "Denmark", home_code="NOR", away_code="DEN"),
        ],
        series_by_ticker=UNL_SERIES,
        books=_books(),
    )
    repository = SqliteMarketIntelligenceRepository()
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
            selected_competition_codes=SCOPE,
            unbounded_cycle=True,
        )
        ids = {item.source_event_id: item for item in report.discovered_fixtures}
        assert MB_NL_GER in ids
        assert "34042288719400023" in ids
        assert "5003" not in ids
        netherlands = ids[MB_NL_GER]
        assert netherlands.target_competition_code == UNL
        assert netherlands.kalshi_matched is True
        rows = report.fixture_markets.get(netherlands.canonical_event_id, [])
        paper_families = {row.family for row in rows if row.family and row.entered_solver}
        assert paper_families >= {
            "match_result",
            "both_teams_to_score",
            "total_goals",
            "first_team_to_score",
        }
        # Correct Score / HT remain visible as recognized inventory but must not
        # enter PAPER/solver. Do not widen equivalence for extra derivatives.
        for row in rows:
            if row.family in {"correct_score", "exact_score", "half_time_full_time"}:
                assert row.entered_solver is False
                assert row.comparison_status is not InventoryComparisonStatus.MATCHED_EQUIVALENT
            if row.period == "first_half":
                assert row.entered_solver is False
        assert "exact_score" not in {row.family for row in rows}
        norway = ids["34042288719400023"]
        assert norway.target_competition_code == UNL
        assert norway.kalshi_matched is True
    finally:
        repository.close()
