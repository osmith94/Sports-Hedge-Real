from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest

from sports_hedge.application.collector import ReadOnlyCrossVenueCollector
from sports_hedge.application.paper_scan import PaperScanService
from sports_hedge.application.target_competitions import (
    TARGET_COMPETITIONS,
    UNMATCHED_POLYMARKET_COVERAGE,
    TargetCompetitionCode,
    polymarket_series_ids_for_targets,
    resolve_target_competition,
    resolve_target_competition_from_kalshi_ticker,
    resolve_target_competition_from_series_id,
    scope_kalshi_event,
    scope_matchbook_event,
    scope_polymarket_event,
)
from sports_hedge.config import Settings
from sports_hedge.market_intelligence.repository import SqliteMarketIntelligenceRepository
from sports_hedge.market_intelligence.service import MarketIntelligenceService
from sports_hedge.paper.models import FxRateSnapshot
from venue_cost_helpers import matchbook_kalshi_costs, matchbook_polymarket_costs
from registered_kalshi import FakeKalshiBTTS


KICKOFF = datetime(2026, 9, 20, 15, 0, tzinfo=UTC)


VARIANT_LABELS = {
    TargetCompetitionCode.PREMIER_LEAGUE: (
        "Premier League",
        "English Premier League",
        "EPL",
        "Barclays Premier League",
        "England Premier League",
        "Premier League 2025/26",
    ),
    TargetCompetitionCode.CHAMPIONSHIP: (
        "Championship",
        "EFL Championship",
        "Sky Bet Championship",
        "English Championship",
        "The Championship",
        "EFL Championship 2025/26",
    ),
    TargetCompetitionCode.LA_LIGA: (
        "La Liga",
        "LaLiga",
        "Primera Division",
        "Primera División",
        "Spanish La Liga",
        "Spain La Liga",
    ),
    TargetCompetitionCode.CARABAO_CUP: (
        "Carabao Cup",
        "EFL Cup",
        "League Cup",
        "English League Cup",
        "Football League Cup",
        "Carabao Cup 2026/27",
        "EFL CUP",
    ),
    TargetCompetitionCode.FA_CUP: (
        "FA Cup",
        "The FA Cup",
        "Emirates FA Cup",
        "English FA Cup",
        "FA Cup 2026/27",
    ),
    TargetCompetitionCode.INTERNATIONAL_FRIENDLIES: (
        "International Friendlies",
        "International Friendly",
        "FIFA Friendlies",
        "FIFA Friendly",
        "Senior International Friendlies",
        "Men's International Friendlies",
    ),
    TargetCompetitionCode.BUNDESLIGA: (
        "Bundesliga",
        "German Bundesliga",
        "Germany Bundesliga",
        "1. Bundesliga",
        "Bundesliga 1",
        "Fußball-Bundesliga",
        "Bundesliga 2026/27",
    ),
    TargetCompetitionCode.SERIE_A: (
        "Serie A",
        "Italian Serie A",
        "Italy Serie A",
        "Serie A TIM",
        "Serie A Enilive",
        "Serie A 2026/27",
    ),
    TargetCompetitionCode.CHAMPIONS_LEAGUE: (
        "Champions League",
        "UEFA Champions League",
        "UCL",
    ),
    TargetCompetitionCode.EUROPA_LEAGUE: (
        "Europa League",
        "UEFA Europa League",
        "UEL",
    ),
    TargetCompetitionCode.CONFERENCE_LEAGUE: (
        "Conference League",
        "UEFA Conference League",
        "UEFA Europa Conference League",
    ),
    TargetCompetitionCode.SUPER_LIG: (
        "Süper Lig",
        "Turkish Süper Lig",
        "Super Lig",
    ),
    TargetCompetitionCode.MLS: (
        "MLS",
        "Major League Soccer",
        "US Major League Soccer",
        "USA MLS",
    ),
    TargetCompetitionCode.LEAGUE_ONE: (
        "League One",
        "EFL League One",
        "Sky Bet League One",
    ),
    TargetCompetitionCode.COPA_DEL_REY: (
        "Copa del Rey",
        "Spanish Copa del Rey",
    ),
    TargetCompetitionCode.DFB_POKAL: (
        "DFB-Pokal",
        "DFB Pokal",
        "German Cup",
    ),
    TargetCompetitionCode.COPPA_ITALIA: (
        "Coppa Italia",
        "Italian Cup",
    ),
    TargetCompetitionCode.LIGUE_1: (
        "Ligue 1",
        "French Ligue 1",
        "France Ligue 1",
    ),
    TargetCompetitionCode.EREDIVISIE: (
        "Eredivisie",
        "Dutch Eredivisie",
    ),
    TargetCompetitionCode.PRIMEIRA_LIGA: (
        "Primeira Liga",
        "Liga Portugal",
    ),
    TargetCompetitionCode.SCOTTISH_PREMIERSHIP: (
        "Scottish Premiership",
        "Cinch Premiership",
    ),
    TargetCompetitionCode.BELGIAN_PRO_LEAGUE: (
        "Belgian Pro League",
        "Jupiler Pro League",
    ),
    TargetCompetitionCode.LIGA_MX: (
        "Liga MX",
        "Mexico Liga MX",
    ),
    TargetCompetitionCode.BRASILEIRAO: (
        "Brasileirão",
        "Brazilian Serie A",
        "Campeonato Brasileiro Série A",
    ),
    TargetCompetitionCode.ARGENTINA_PRIMERA: (
        "Liga Profesional",
        "Argentine Primera",
        "Primera División Argentina",
    ),
    TargetCompetitionCode.COPA_LIBERTADORES: (
        "Copa Libertadores",
        "CONMEBOL Libertadores",
    ),
    TargetCompetitionCode.SAUDI_PRO_LEAGUE: (
        "Saudi Pro League",
        "Roshn Saudi League",
    ),
    TargetCompetitionCode.J1_LEAGUE: (
        "J1 League",
        "J-League",
        "Japan J1",
    ),
    TargetCompetitionCode.SOUTH_AFRICAN_PREMIERSHIP: (
        "South African Premiership",
        "South Africa Premiership",
    ),
    TargetCompetitionCode.LEAGUE_TWO: (
        "League Two",
        "EFL League Two",
    ),
}

REJECTED_LABELS = (
    "Women's Cricket",
    "Rugby Union",
    "3. Liga",
    "Germany 3. Liga",
    "USL Championship",
    "LaLiga2",
    "",
    "EFL Trophy",
    "Vertu Trophy",
    "2. Bundesliga",
    "Bundesliga 2",
    "Women's Bundesliga",
    "Frauen-Bundesliga",
    "Austria Bundesliga",
    "Serie B",
    "Serie A Femminile",
    "Women's Serie A",
    "FA Trophy",
    "FA Vase",
    "FA Youth Cup",
    "Women's FA Cup",
    "Adobe Women's FA Cup",
    "U21 International Friendlies",
    "U20 International Friendlies",
    "U19 International Friendlies",
    "Youth International Friendlies",
    "Women's International Friendlies",
    "Club Friendlies",
    "Club Friendly",
    "Friendly",
    "Scottish League Cup",
)


@pytest.mark.parametrize(
    ("label", "code"),
    [
        (label, code)
        for code, labels in VARIANT_LABELS.items()
        for label in labels
    ],
)
def test_target_competition_aliases_are_accepted(label: str, code: TargetCompetitionCode) -> None:
    resolved = resolve_target_competition(label)
    assert resolved is not None
    assert resolved.code == code


@pytest.mark.parametrize("label", REJECTED_LABELS)
def test_unknown_or_adjacent_competitions_fail_closed(label: str) -> None:
    assert resolve_target_competition(label) is None


def test_matchbook_scope_excludes_unrelated_sport_even_if_label_looks_plausible() -> None:
    cricket = scope_matchbook_event(
        {
            "id": 1,
            "name": "England Women vs India Women",
            "sport-name": "Cricket",
            "competition-name": "Premier League",
        }
    )
    assert cricket.allowed is False
    assert cricket.reason == "non_football_sport"


@pytest.mark.parametrize(
    ("label", "code"),
    [
        ("Carabao Cup", TargetCompetitionCode.CARABAO_CUP),
        ("EFL Cup", TargetCompetitionCode.CARABAO_CUP),
        ("FA Cup", TargetCompetitionCode.FA_CUP),
        ("International Friendlies", TargetCompetitionCode.INTERNATIONAL_FRIENDLIES),
        ("Bundesliga", TargetCompetitionCode.BUNDESLIGA),
        ("Serie A", TargetCompetitionCode.SERIE_A),
    ],
)
def test_matchbook_scope_accepts_new_target_competitions(
    label: str, code: TargetCompetitionCode
) -> None:
    decision = scope_matchbook_event(
        {
            "id": 10,
            "name": "Home vs Away",
            "sport-name": "Football",
            "competition-name": label,
        }
    )
    assert decision.allowed is True
    assert decision.competition is not None
    assert decision.competition.code == code


@pytest.mark.parametrize(
    "label",
    (
        "EFL Trophy",
        "Vertu Trophy",
        "2. Bundesliga",
        "Serie B",
        "Women's FA Cup",
        "Club Friendlies",
        "U21 International Friendlies",
    ),
)
def test_matchbook_scope_rejects_out_of_scope_and_near_neighbors(label: str) -> None:
    decision = scope_matchbook_event(
        {
            "id": 11,
            "name": "Home vs Away",
            "sport-name": "Football",
            "competition-name": label,
        }
    )
    assert decision.allowed is False
    assert decision.reason == "unknown_or_ambiguous_competition"


@pytest.mark.parametrize(
    "label",
    ("League One", "League Two", "EFL League One", "Ligue 1", "MLS", "US Major League Soccer"),
)
def test_unselected_registered_competitions_are_out_of_scope(label: str) -> None:
    decision = scope_matchbook_event(
        {
            "id": 12,
            "name": "Home vs Away",
            "sport-name": "Football",
            "competition-name": label,
        }
    )
    assert decision.allowed is False
    assert decision.reason == "out_of_scope_competition"


@pytest.mark.parametrize(
    ("series_id", "code"),
    [
        ("10188", TargetCompetitionCode.PREMIER_LEAGUE),
        ("10355", TargetCompetitionCode.CHAMPIONSHIP),
        ("10193", TargetCompetitionCode.LA_LIGA),
        ("10329", TargetCompetitionCode.CARABAO_CUP),
        ("10314", TargetCompetitionCode.FA_CUP),
        ("10238", TargetCompetitionCode.INTERNATIONAL_FRIENDLIES),
        ("10194", TargetCompetitionCode.BUNDESLIGA),
        ("10203", TargetCompetitionCode.SERIE_A),
        ("10204", TargetCompetitionCode.CHAMPIONS_LEAGUE),
        ("10209", TargetCompetitionCode.EUROPA_LEAGUE),
        ("10437", TargetCompetitionCode.CONFERENCE_LEAGUE),
        ("10292", TargetCompetitionCode.SUPER_LIG),
        ("10189", TargetCompetitionCode.MLS),
        ("11435", TargetCompetitionCode.LEAGUE_ONE),
        ("11436", TargetCompetitionCode.LEAGUE_TWO),
        ("10316", TargetCompetitionCode.COPA_DEL_REY),
        ("10317", TargetCompetitionCode.DFB_POKAL),
        ("10287", TargetCompetitionCode.COPPA_ITALIA),
        ("10195", TargetCompetitionCode.LIGUE_1),
        ("10286", TargetCompetitionCode.EREDIVISIE),
        ("10330", TargetCompetitionCode.PRIMEIRA_LIGA),
        ("10674", TargetCompetitionCode.SCOTTISH_PREMIERSHIP),
        ("12351", TargetCompetitionCode.BELGIAN_PRO_LEAGUE),
        ("10290", TargetCompetitionCode.LIGA_MX),
        ("10359", TargetCompetitionCode.BRASILEIRAO),
        ("10312", TargetCompetitionCode.ARGENTINA_PRIMERA),
        ("10289", TargetCompetitionCode.COPA_LIBERTADORES),
        ("10361", TargetCompetitionCode.SAUDI_PRO_LEAGUE),
        ("10360", TargetCompetitionCode.J1_LEAGUE),
        ("12360", TargetCompetitionCode.SOUTH_AFRICAN_PREMIERSHIP),
    ],
)
def test_verified_polymarket_series_ids_resolve(
    series_id: str, code: TargetCompetitionCode
) -> None:
    resolved = resolve_target_competition_from_series_id(series_id)
    assert resolved is not None
    assert resolved.code == code
    assert resolved.polymarket_gamma_series_id == series_id


@pytest.mark.parametrize(
    "series_id",
    (
        "10670",  # Gamma bl2 = 2. Bundesliga
        "10676",  # Gamma itsb = Serie B
        "12410",  # Gamma clf = Club Friendlies
        "11863",  # Gamma ecu1 = LigaPro Serie A
        "10307",  # Stale 2026-09-16 FA Cup snapshot; live efa series is 10314
        "99999",
    ),
)
def test_unverified_or_near_neighbor_polymarket_series_ids_are_not_claimed(
    series_id: str,
) -> None:
    assert resolve_target_competition_from_series_id(series_id) is None


def test_polymarket_scope_uses_verified_series_coverage() -> None:
    bun = scope_polymarket_event(
        {
            "id": "pm-bun-1",
            "title": "Bayern Munich vs Dortmund",
            "series": [{"id": "10194", "title": "Bundesliga"}],
        }
    )
    assert bun.allowed is True
    assert bun.competition is not None
    assert bun.competition.code == TargetCompetitionCode.BUNDESLIGA

    second_div = scope_polymarket_event(
        {
            "id": "pm-bl2-1",
            "title": "Aachen vs Essen",
            "competition": "2. Bundesliga",
            "series": [{"id": "10670", "title": "2. Bundesliga"}],
        }
    )
    assert second_div.allowed is False


@pytest.mark.parametrize(
    ("ticker", "code"),
    [
        ("KXEPLGAME", TargetCompetitionCode.PREMIER_LEAGUE),
        ("KXEFLCHAMPIONSHIPGAME", TargetCompetitionCode.CHAMPIONSHIP),
        ("KXLALIGAGAME", TargetCompetitionCode.LA_LIGA),
        ("KXEFLCUPGAME", TargetCompetitionCode.CARABAO_CUP),
        ("KXEFLCUPBTTS", TargetCompetitionCode.CARABAO_CUP),
        ("KXFACUPGAME", TargetCompetitionCode.FA_CUP),
        ("KXINTLFRIENDLYGAME", TargetCompetitionCode.INTERNATIONAL_FRIENDLIES),
        ("KXBUNDESLIGAGAME", TargetCompetitionCode.BUNDESLIGA),
        ("KXBUNDESLIGABTTS", TargetCompetitionCode.BUNDESLIGA),
        ("KXSERIEAGAME", TargetCompetitionCode.SERIE_A),
        ("KXSERIEATOTAL", TargetCompetitionCode.SERIE_A),
        ("KXUCLGAME", TargetCompetitionCode.CHAMPIONS_LEAGUE),
        ("KXUELGAME", TargetCompetitionCode.EUROPA_LEAGUE),
        ("KXUECLGAME", TargetCompetitionCode.CONFERENCE_LEAGUE),
        ("KXSUPERLIGGAME", TargetCompetitionCode.SUPER_LIG),
        ("KXMLSGAME", TargetCompetitionCode.MLS),
        ("KXEFLL1GAME", TargetCompetitionCode.LEAGUE_ONE),
        ("KXCOPADELREYGAME", TargetCompetitionCode.COPA_DEL_REY),
        ("KXDFBPOKALGAME", TargetCompetitionCode.DFB_POKAL),
        ("KXCOPPAITALIAGAME", TargetCompetitionCode.COPPA_ITALIA),
        ("KXLIGUE1GAME", TargetCompetitionCode.LIGUE_1),
        ("KXEREDIVISIEGAME", TargetCompetitionCode.EREDIVISIE),
        ("KXLIGAPORTUGALGAME", TargetCompetitionCode.PRIMEIRA_LIGA),
        ("KXSCOTTISHPREMGAME", TargetCompetitionCode.SCOTTISH_PREMIERSHIP),
        ("KXBELGIANPLGAME", TargetCompetitionCode.BELGIAN_PRO_LEAGUE),
        ("KXLIGAMXGAME", TargetCompetitionCode.LIGA_MX),
        ("KXBRASILEIROGAME", TargetCompetitionCode.BRASILEIRAO),
        ("KXARGPREMDIVGAME", TargetCompetitionCode.ARGENTINA_PRIMERA),
        ("KXCONMEBOLLIBGAME", TargetCompetitionCode.COPA_LIBERTADORES),
        ("KXSAUDIPLGAME", TargetCompetitionCode.SAUDI_PRO_LEAGUE),
        ("KXJLEAGUEGAME", TargetCompetitionCode.J1_LEAGUE),
    ],
)
def test_verified_kalshi_tickers_resolve(ticker: str, code: TargetCompetitionCode) -> None:
    resolved = resolve_target_competition_from_kalshi_ticker(ticker)
    assert resolved is not None
    assert resolved.code == code


@pytest.mark.parametrize(
    "ticker",
    (
        "KXBUNDESLIGA2GAME",
        "KXBUNDESLIGA2BTTS",
        "KXSERIEAWGAME",
        "KXSERIEBGAME",
        "KXBRASILEIROBGAME",
        "KXLIGUE2GAME",
        "KXEREDIVISIEWGAME",
        "KXJ2LEAGUEGAME",
        "KXCONMEBOLSUDGAME",
        "KXCLUBFGAME",
        "KXEFLTROPHYGAME",
        "KXFIFAWGAME",
        "KXUCLWGAME",
        "KXMLSASTGAME",
        "KXDENSUPERLIGAGAME",
    ),
)
def test_kalshi_near_neighbor_tickers_are_not_claimed(ticker: str) -> None:
    assert resolve_target_competition_from_kalshi_ticker(ticker) is None


def test_kalshi_scope_accepts_verified_series_and_rejects_neighbors() -> None:
    accepted = scope_kalshi_event(
        {
            "event_ticker": "KXFACUPGAME-26JAN10ARSLIV",
            "series_ticker": "KXFACUPGAME",
            "title": "Arsenal vs Liverpool",
        }
    )
    assert accepted.allowed is True
    assert accepted.competition is not None
    assert accepted.competition.code == TargetCompetitionCode.FA_CUP

    rejected = scope_kalshi_event(
        {
            "event_ticker": "KXSERIEAWGAME-26JAN10JUVERO",
            "series_ticker": "KXSERIEAWGAME",
            "title": "Juventus vs Roma",
            "competition": "Serie A Femminile",
        }
    )
    assert rejected.allowed is False


def test_display_name_for_league_cup_is_carabao_cup() -> None:
    resolved = resolve_target_competition("EFL Cup")
    assert resolved is not None
    assert resolved.display_name == "Carabao Cup"


def _event(
    event_id: int,
    name: str,
    competition: str,
    *,
    sport: str | None = "Football",
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": event_id,
        "name": name,
        "start": KICKOFF.isoformat(),
        "competition-name": competition,
        "status": "open",
    }
    if sport is not None:
        payload["sport-name"] = sport
    return payload


def _btts_market(market_id: int) -> dict[str, Any]:
    return {
        "id": market_id,
        "name": "Both Teams To Score",
        "runners": [
            {
                "id": market_id * 10 + 1,
                "name": "Yes",
                "prices": [
                    {"side": "back", "odds": "2.20", "available-amount": "100"},
                    {"side": "lay", "odds": "2.22", "available-amount": "100"},
                ],
            },
            {
                "id": market_id * 10 + 2,
                "name": "No",
                "prices": [
                    {"side": "back", "odds": "1.80", "available-amount": "100"},
                    {"side": "lay", "odds": "1.82", "available-amount": "100"},
                ],
            },
        ],
    }


class ScopedMatchbook:
    def __init__(self) -> None:
        self.list_markets_calls: list[str] = []

    async def list_events(self, **filters: Any) -> dict[str, Any]:
        del filters
        return {
            "events": [
                _event(1001, "Newcastle United vs Chelsea", "Premier League"),
                _event(2001, "Leeds United vs Leicester City", "EFL Championship"),
                _event(3001, "Real Madrid vs Barcelona", "La Liga"),
                _event(5001, "Arsenal vs Liverpool", "Carabao Cup"),
                _event(5002, "Manchester City vs Chelsea", "FA Cup"),
                _event(5003, "England vs France", "International Friendlies"),
                _event(5004, "Bayern Munich vs Dortmund", "Bundesliga"),
                _event(5005, "Inter vs Milan", "Serie A"),
                _event(4001, "England Women vs India Women", "Women's Cricket", sport="Cricket"),
                _event(4002, "England vs France", "Rugby Union", sport="Rugby Union"),
                _event(4003, "Aachen vs Essen", "3. Liga"),
                _event(4004, "Charlton vs Bolton", "League One"),
                _event(4005, "Salford vs Grimsby", "League Two"),
                _event(4006, "Peterborough vs Wigan", "EFL Trophy"),
            ]
        }

    async def list_markets(self, event_id: int | str, **filters: Any) -> dict[str, Any]:
        del filters
        self.list_markets_calls.append(str(event_id))
        return {"markets": [_btts_market(int(event_id))]}


class PartialPolymarket:
    """EPL coverage only. Championship/La Liga stay unmatched without invented markets."""

    def __init__(self) -> None:
        self.list_events_filters: list[dict[str, Any]] = []
        self.book_calls: list[str] = []

    async def list_events(self, **filters: Any) -> list[dict[str, Any]]:
        self.list_events_filters.append(dict(filters))
        return [
            {
                "id": "pm-epl-1",
                "title": "Newcastle United vs Chelsea",
                "startTime": KICKOFF.isoformat(),
                "competition": "Premier League",
                "series": [{"title": "Premier League"}],
            }
        ]

    async def list_markets(self, event_id: int | str, **filters: Any) -> list[dict[str, Any]]:
        del event_id, filters
        return [
            {
                "id": "pm-market-1",
                "question": "Both teams to score?",
                "sportsMarketType": "both teams to score",
                "outcomes": '["Yes", "No"]',
                "clobTokenIds": '["yes-token", "no-token"]',
                "description": "Resolves based on 90 minutes of regulation time.",
            }
        ]

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
        now_ms = int(datetime.now(UTC).timestamp() * 1000)
        if token == "yes-token":
            return {
                "asset_id": token,
                "timestamp": now_ms - 200,
                "bids": [{"price": "0.49", "size": "250"}],
                "asks": [{"price": "0.51", "size": "250"}],
            }
        return {
            "asset_id": token,
            "timestamp": now_ms - 180,
            "bids": [{"price": "0.41", "size": "300"}],
            "asks": [{"price": "0.43", "size": "300"}],
        }


@pytest.mark.asyncio
async def test_collector_scopes_discovery_and_keeps_unmatched_coverage_truthful() -> None:
    repository = SqliteMarketIntelligenceRepository()
    intelligence = MarketIntelligenceService(repository)
    matchbook = ScopedMatchbook()
    polymarket = PartialPolymarket()
    collector = ReadOnlyCrossVenueCollector(
        matchbook=matchbook,
        polymarket=polymarket,
        kalshi=FakeKalshiBTTS(
            [("Premier League", "Newcastle United", "Chelsea", KICKOFF)]
        ),
        paper_scan=PaperScanService(intelligence),
    )
    try:
        report = await collector.collect_and_scan(
            venue_costs=matchbook_kalshi_costs() + matchbook_polymarket_costs(),
            fx_snapshots=[FxRateSnapshot(currency="USD", gbp_per_unit=Decimal("0.75"))],
            capital_limit_gbp=Decimal("100"),
            maximum_execution_risk=100,
        )
        ids = {item.source_event_id: item for item in report.discovered_fixtures}
        assert set(ids) == {"1001", "2001", "3001", "5001", "5002", "5003", "5004", "5005"}
        assert "4001" not in ids
        assert "4002" not in ids
        assert "4003" not in ids
        assert "4004" not in ids
        assert "4005" not in ids
        assert "4006" not in ids
        assert report.skipped_out_of_scope >= 6
        assert not any(issue.stage == "target_competition" for issue in report.issues)
        assert "3. Liga" in report.rejected_competition_labels or report.skipped_out_of_scope >= 6

        epl = ids["1001"]
        assert epl.polymarket_matched is True
        assert epl.target_competition_code == "premier_league"
        assert epl.matched_market_count == 1
        assert epl.market_family == "both_teams_to_score"
        assert epl.outcome_context == "yes/no"
        assert epl.best_matchbook_price is not None
        assert epl.best_kalshi_price is not None
        assert epl.current_net_edge is not None
        assert epl.trigger_net_edge == Decimal("0.005")
        assert epl.distance_to_trigger_pp is not None
        assert epl.quote_age_ms is not None
        assert epl.no_comparison_reason is None
        assert epl.solver_is_arbitrage is True

        championship = ids["2001"]
        assert championship.target_competition_code == "championship"
        assert championship.polymarket_matched is False
        assert championship.no_comparison_reason == UNMATCHED_POLYMARKET_COVERAGE
        assert championship.current_net_edge is None
        assert championship.solver_is_arbitrage is False

        la_liga = ids["3001"]
        assert la_liga.target_competition_code == "la_liga"
        assert la_liga.polymarket_matched is False
        assert la_liga.no_comparison_reason == UNMATCHED_POLYMARKET_COVERAGE

        carabao = ids["5001"]
        assert carabao.target_competition_code == "carabao_cup"
        assert carabao.polymarket_matched is False
        assert carabao.no_comparison_reason == UNMATCHED_POLYMARKET_COVERAGE

        fa_cup = ids["5002"]
        assert fa_cup.target_competition_code == "fa_cup"
        assert fa_cup.polymarket_matched is False
        assert fa_cup.no_comparison_reason == UNMATCHED_POLYMARKET_COVERAGE

        friendlies = ids["5003"]
        assert friendlies.target_competition_code == "international_friendlies"
        assert friendlies.polymarket_matched is False
        assert friendlies.no_comparison_reason == UNMATCHED_POLYMARKET_COVERAGE

        bundesliga = ids["5004"]
        assert bundesliga.target_competition_code == "bundesliga"
        assert bundesliga.polymarket_matched is False
        assert bundesliga.no_comparison_reason == UNMATCHED_POLYMARKET_COVERAGE

        serie_a = ids["5005"]
        assert serie_a.target_competition_code == "serie_a"
        assert serie_a.polymarket_matched is False
        assert serie_a.no_comparison_reason == UNMATCHED_POLYMARKET_COVERAGE
        assert set(matchbook.list_markets_calls) == {
            "1001",
            "2001",
            "3001",
            "5001",
            "5002",
            "5003",
            "5004",
            "5005",
        }
    finally:
        repository.close()


def test_principal_register_has_thirty_two_rows_and_still_excludes_efl_trophy() -> None:
    codes = {item.code for item in TARGET_COMPETITIONS}
    assert len(TARGET_COMPETITIONS) == 32
    assert len(codes) == 32
    assert TargetCompetitionCode.NFL in codes
    assert TargetCompetitionCode.NBA in codes
    assert TargetCompetitionCode.LEAGUE_ONE in codes
    assert TargetCompetitionCode.LEAGUE_TWO in codes
    assert TargetCompetitionCode.LIGUE_1 in codes
    assert TargetCompetitionCode.SOUTH_AFRICAN_PREMIERSHIP in codes
    assert resolve_target_competition("League One") is not None
    assert resolve_target_competition("League Two") is not None
    for label in ("EFL Trophy", "Vertu Trophy"):
        assert resolve_target_competition(label) is None


def test_default_settings_query_verified_coverage_without_inventing_tickers() -> None:
    settings = Settings()
    assert settings.resolved_polymarket_series_ids() == polymarket_series_ids_for_targets()
    assert settings.resolved_polymarket_series_ids()[:3] == ["10188", "10355", "10193"]
    tickers = settings.kalshi_series_tickers
    assert "KXEPLGAME" in tickers
    assert "KXEFLCUPGAME" in tickers
    assert "KXFACUPGAME" in tickers
    assert "KXINTLFRIENDLYGAME" in tickers
    assert "KXBUNDESLIGAGAME" in tickers
    assert "KXSERIEAGAME" in tickers
    assert "KXINTLFRIENDLYFTTS" not in tickers
    assert "KXBUNDESLIGA2GAME" not in tickers
    assert "KXSERIEAWGAME" not in tickers
    assert "KXCLUBFGAME" not in tickers
    assert "KXNFLGAME" not in tickers
    assert "KXNBAGAME" not in tickers
    assert "12185" not in settings.resolved_polymarket_series_ids()
    assert "10345" not in settings.resolved_polymarket_series_ids()
