"""MLB Stage 1: identity and discovery, with settlement fail-closed."""

from __future__ import annotations

import asyncio
import inspect
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from sports_hedge.application.approved_market_catalogue import (
    ApprovedMarketCatalogueRow,
    CatalogueRowState,
)
from sports_hedge.application.catalogue_maintenance import family_key_from_kalshi_series
from sports_hedge.application.collector import (
    DEFAULT_PROVIDER_CONCURRENCY,
    matchbook_scope_discovery_params,
)
from sports_hedge.application.fixture_clusters import VenueEvent, _compatible_index_pair, _index_record
from sports_hedge.application.hot_identity import same_hot_scheduling_unit, scheduling_team_key
from sports_hedge.application.price_engine import CataloguePriceEngine
from sports_hedge.application.target_competitions import (
    DEFAULT_OPERATOR_COMPETITION_CODES,
    OPERATOR_COMPETITION_REGISTRY_VERSION,
    PRINCIPAL_OPERATOR_COMPETITION_COUNT,
    TargetCompetitionCode,
    kalshi_series_tickers_for_codes,
    operator_competition_catalog,
    polymarket_series_ids_for_codes,
    resolve_target_competition_from_kalshi_ticker,
    scope_kalshi_event,
    scope_matchbook_event,
    scope_polymarket_event,
)
from sports_hedge.catalogue.classify import classify_pair
from sports_hedge.catalogue.states import CatalogueApprovalState
from sports_hedge.domain.football import MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.matching.approved_register import registered_canonical_key
from sports_hedge.matching.events import EventMatcher
from sports_hedge.mlb.constants import (
    CANONICAL_MLB_GAME_WINNER,
    CANONICAL_MLB_TOTAL_RUNS,
    MATCHBOOK_MLB_COMPETITION_TAG_ID,
    MLB_LINE_MISMATCH_REASON,
    MLB_SETTLEMENT_NOT_EXECUTABLE,
)
from sports_hedge.mlb.normalize import (
    kalshi_mlb_event,
    kalshi_mlb_markets,
    matchbook_mlb_event,
    matchbook_mlb_market,
    polymarket_mlb_event,
    polymarket_mlb_market,
)
from sports_hedge.mlb.teams import resolve_mlb_team
from sports_hedge.normalization.venues import VenueNormalizationError
from sports_hedge.paper.result_resolution import _family_blocker
from sports_hedge.venues.matchbook import select_baseball_sport_id

KICKOFF = "2026-09-24T16:35:00Z"
GAME2 = "2026-09-24T23:10:00Z"
HOME_ID = "11111111-1111-1111-1111-111111111111"
AWAY_ID = "22222222-2222-2222-2222-222222222222"
PIT = "pittsburgh pirates"
STL = "st louis cardinals"


def _kalshi_game(ticker: str, start: str, *, title_suffix: str = "") -> dict:
    title = f"St. Louis vs Pittsburgh {title_suffix}".strip()
    return {
        "event_ticker": ticker,
        "series_ticker": "KXMLBGAME",
        "title": title,
        "milestone": {
            "start_date": start,
            "title": title,
            "details": {
                "league": "MLB",
                "home_team_id": HOME_ID,
                "away_team_id": AWAY_ID,
            },
        },
        "markets": [
            {
                "ticker": f"{ticker}-PIT",
                "yes_sub_title": "Pittsburgh wins",
                "custom_strike": {"baseball_team": HOME_ID},
            },
            {
                "ticker": f"{ticker}-STL",
                "yes_sub_title": "St. Louis wins",
                "custom_strike": {"baseball_team": AWAY_ID},
            },
        ],
    }


def _polymarket_event(event_id: str, start: str, *, slug: str, title: str) -> dict:
    return {
        "id": event_id,
        "slug": slug,
        "title": title,
        "startTime": start,
        "sport": {"sport": "mlb", "series": "3"},
        "teams": [
            {"name": "Cardinals", "ordering": "away", "league": "mlb", "abbreviation": "STL"},
            {"name": "Pirates", "ordering": "home", "league": "mlb", "abbreviation": "PIT"},
        ],
    }


def _matchbook_event(event_id: str, start: str, *, name: str) -> dict:
    return {
        "id": event_id,
        "name": name,
        "start": start,
        "meta-tags": [
            {"id": "3", "type": "SPORT", "name": "Baseball"},
            {
                "id": MATCHBOOK_MLB_COMPETITION_TAG_ID,
                "type": "COMPETITION",
                "name": "Major League Baseball",
            },
        ],
    }


def _moneyline_markets():
    kalshi_payload = _kalshi_game("KXMLBGAME-26SEP241235STLPIT", KICKOFF)
    kalshi_event = kalshi_mlb_event(kalshi_payload)
    kalshi_market = kalshi_mlb_markets(kalshi_event, kalshi_payload["markets"])[0]
    pm_event = polymarket_mlb_event(
        _polymarket_event(
            "pm-stl-pit",
            KICKOFF,
            slug="mlb-stl-pit-2026-09-24",
            title="Cardinals vs Pirates",
        )
    )
    pm_market = polymarket_mlb_market(
        pm_event,
        {
            "id": "pm-market-ml",
            "sportsMarketType": "moneyline",
            "question": "Cardinals vs Pirates",
            "outcomes": ["Cardinals", "Pirates"],
            "clobTokenIds": ["token-stl-real", "token-pit-real"],
        },
    )
    mb_event = matchbook_mlb_event(
        _matchbook_event(
            "34422443059100081",
            "2026-09-24T16:35:00.000Z",
            name="St. Louis Cardinals at Pittsburgh Pirates",
        )
    )
    mb_market = matchbook_mlb_market(
        mb_event,
        {
            "id": "mb-ml",
            "name": "Moneyline",
            "market-type": "money_line",
            "runners": [
                {"id": "r-stl", "name": "St. Louis Cardinals"},
                {"id": "r-pit", "name": "Pittsburgh Pirates"},
            ],
        },
    )
    return kalshi_market, pm_market, mb_market


def test_three_venues_cluster_on_curated_aliases() -> None:
    kalshi, polymarket, matchbook = _moneyline_markets()
    assert kalshi.event.home_team == polymarket.event.home_team == matchbook.event.home_team == PIT
    assert kalshi.event.away_team == polymarket.event.away_team == matchbook.event.away_team == STL
    matcher = EventMatcher()
    for left, right in (
        (kalshi.event, polymarket.event),
        (polymarket.event, matchbook.event),
        (matchbook.event, kalshi.event),
    ):
        result = matcher.match(left, right)
        assert result.matched is True
        assert result.confidence == 1.0


def test_doubleheader_game_keys_never_cross_cluster() -> None:
    first = kalshi_mlb_event(_kalshi_game("KXMLBGAME-26SEP241235STLPIT", KICKOFF, title_suffix="Game 1"))
    second = kalshi_mlb_event(_kalshi_game("KXMLBGAME-26SEP241910STLPIT", GAME2, title_suffix="Game 2"))
    close = kalshi_mlb_event(
        _kalshi_game("KXMLBGAME-26SEP241238STLPIT", "2026-09-24T16:38:00Z", title_suffix="Game 2")
    )
    assert first.scheduled_game_key != second.scheduled_game_key
    assert "game-1" in (first.scheduled_game_key or "")
    assert "game-2" in (second.scheduled_game_key or "")
    matcher = EventMatcher()
    assert matcher.could_match(first, second) is False
    assert matcher.match(first, second).reasons == ["mlb_doubleheader_or_start_mismatch"]
    assert matcher.could_match(first, close) is False
    missing = first.model_copy(update={"scheduled_game_key": None})
    assert matcher.match(missing, first).reasons == ["mlb_game_identity_ambiguous"]
    assert same_hot_scheduling_unit(first, close) is False
    assert same_hot_scheduling_unit(first, first.model_copy()) is True
    left = _index_record(0, VenueEvent(venue=VenueName.KALSHI, raw={}, canonical=first, source_event_id="g1"))
    right = _index_record(1, VenueEvent(venue=VenueName.POLYMARKET, raw={}, canonical=second, source_event_id="g2"))
    assert _compatible_index_pair(left, right, window_seconds=60 * 60 * 12) is False


def test_non_mlb_baseball_fails_scope_closed() -> None:
    kbo = scope_polymarket_event(
        {"id": "kbo-1", "title": "KBO", "sport": {"sport": "kbo", "series": "10370"}},
        selected_codes=["mlb"],
    )
    assert kbo.allowed is False
    college = scope_kalshi_event(
        {"series_ticker": "KXNCAABBGAME-26APR01", "title": "College baseball"},
        selected_codes=["mlb"],
    )
    assert college.allowed is False
    assert college.reason == "rejected_non_mlb_baseball"
    minors = scope_matchbook_event(
        {
            "name": "Durham Bulls at Columbus Clippers",
            "meta-tags": [
                {"type": "SPORT", "name": "Baseball"},
                {"type": "COMPETITION", "name": "Triple-A", "id": "999"},
            ],
        },
        selected_codes=["mlb"],
    )
    assert minors.allowed is False
    world_series = scope_matchbook_event(
        _matchbook_event("ws", KICKOFF, name="MLB World Series 2026"),
        selected_codes=["mlb"],
    )
    assert world_series.allowed is False
    assert world_series.reason == "mlb_market_family_not_stage1"
    with pytest.raises(VenueNormalizationError):
        polymarket_mlb_event(
            {
                **_polymarket_event("props", KICKOFF, slug="mlb-player-props-2026-09-24", title="player props"),
            }
        )


def test_only_stage1_families_are_structural_and_none_are_executable() -> None:
    kalshi, polymarket, matchbook = _moneyline_markets()
    for market in (kalshi, polymarket, matchbook):
        assert market.family is MarketFamily.GAME_WINNER
    assert registered_canonical_key(kalshi, polymarket) is None
    assert registered_canonical_key(kalshi, matchbook) is None
    assert registered_canonical_key(polymarket, matchbook) is None
    assessment = classify_pair(kalshi, polymarket)
    assert assessment.state is CatalogueApprovalState.UNSUPPORTED
    assert assessment.reason == MLB_SETTLEMENT_NOT_EXECUTABLE
    assert assessment.execution_eligible is False
    total_payload = _kalshi_game("KXMLBTOTAL-26SEP241235STLPIT", KICKOFF)
    total_payload["series_ticker"] = "KXMLBTOTAL"
    total_event = kalshi_mlb_event(total_payload)
    over = kalshi_mlb_markets(
        total_event,
        [{"ticker": "KXMLBTOTAL-26SEP241235STLPIT-7", "title": "Over 7.5 runs scored", "floor_strike": "7.5"}],
    )[0]
    other = kalshi_mlb_markets(
        total_event,
        [{"ticker": "KXMLBTOTAL-26SEP241235STLPIT-8", "title": "Over 8.5 runs scored", "floor_strike": "8.5"}],
    )[0]
    assert over.family is MarketFamily.TOTAL_RUNS
    assert registered_canonical_key(over, other) is None
    line_assessment = classify_pair(over, other)
    assert line_assessment.reason == MLB_LINE_MISMATCH_REASON
    with pytest.raises(VenueNormalizationError):
        kalshi_mlb_markets(
            total_event,
            [{"ticker": "KXMLBTOTAL-26SEP241235STLPIT-7", "title": "Over 7 runs scored", "floor_strike": "7"}],
        )
    with pytest.raises(VenueNormalizationError):
        polymarket_mlb_market(
            polymarket.event,
            {
                "id": "spread",
                "sportsMarketType": "spreads",
                "question": "Run line",
                "outcomes": ["Cardinals", "Pirates"],
                "clobTokenIds": ["token-a", "token-b"],
            },
        )
    with pytest.raises(VenueNormalizationError):
        polymarket_mlb_market(
            polymarket.event,
            {
                "id": "fake",
                "sportsMarketType": "moneyline",
                "outcomes": ["Cardinals", "Pirates"],
                "conditionId": "cond",
                "clobTokenIds": ["cond:0", "cond:1"],
            },
        )


def test_kalshi_prefixes_do_not_admit_neighbours_or_soccer_suffixes() -> None:
    assert resolve_target_competition_from_kalshi_ticker("KXMLBGAME-26SEP241235STLPIT").code is TargetCompetitionCode.MLB
    assert resolve_target_competition_from_kalshi_ticker("KXMLBTOTAL-26SEP241235STLPIT-7").code is TargetCompetitionCode.MLB
    for ticker in (
        "KXMLBSPREAD-26SEP241235STLPIT",
        "KXMLBF5-26SEP241235STLPIT",
        "KXMLBF5TOTAL-26SEP241235STLPIT",
        "KXMLB-26",
        "KXKBOGAME-26SEP24",
        "KXNCAABBGAME-26APR01",
        "KXMLBTEAMTOTAL-26SEP24",
    ):
        resolved = resolve_target_competition_from_kalshi_ticker(ticker)
        assert resolved is None or resolved.code is not TargetCompetitionCode.MLB
        assert scope_kalshi_event({"series_ticker": ticker, "title": "MLB"}, selected_codes=["mlb"]).allowed is False
    assert family_key_from_kalshi_series("KXMLBGAME-26SEP241235STLPIT") == CANONICAL_MLB_GAME_WINNER
    assert family_key_from_kalshi_series("KXMLBTOTAL-26SEP241235STLPIT") == CANONICAL_MLB_TOTAL_RUNS
    assert family_key_from_kalshi_series("KXMLBSPREAD-26SEP241235STLPIT") is None
    assert family_key_from_kalshi_series("KXEPLTOTAL-26SEP24") == "TOTAL_GOALS_FT"


def test_mlb_is_selectable_not_default_and_not_paper_executable() -> None:
    catalog = {row["code"]: row for row in operator_competition_catalog()}
    assert len(catalog) == PRINCIPAL_OPERATOR_COMPETITION_COUNT == 37
    assert OPERATOR_COMPETITION_REGISTRY_VERSION == 9
    row = catalog["mlb"]
    assert row["selectable"] is True
    assert row["default_selected"] is False
    assert row["paper_executable"] is False
    assert row["group_id"] == "mlb"
    assert TargetCompetitionCode.MLB not in DEFAULT_OPERATOR_COMPETITION_CODES
    assert polymarket_series_ids_for_codes(["mlb"]) == ["3"]
    assert kalshi_series_tickers_for_codes(["mlb"]) == ["KXMLBGAME", "KXMLBTOTAL"]


def test_matchbook_mlb_only_scope_uses_baseball_sport_and_tag() -> None:
    mlb_only = matchbook_scope_discovery_params(["mlb"], baseball_sport_id="3")
    assert mlb_only["sport-ids"] == "3"
    assert mlb_only["tag-ids"] == MATCHBOOK_MLB_COMPETITION_TAG_ID
    mixed = matchbook_scope_discovery_params(
        ["mlb", "nba"],
        baseball_sport_id="3",
        basketball_sport_id="4",
    )
    assert mixed["sport-ids"] == "4,3"
    assert "tag-ids" not in mixed
    nba_only = matchbook_scope_discovery_params(["nba"], basketball_sport_id="4")
    assert nba_only["tag-ids"] != MATCHBOOK_MLB_COMPETITION_TAG_ID
    sport_id = select_baseball_sport_id(
        [
            {"id": 9, "name": "Base"},
            {"id": 3, "name": "Baseball"},
            {"id": 1, "name": "American Football"},
        ]
    )
    assert sport_id == 3


def test_ambiguous_and_historical_team_labels_fail_closed() -> None:
    assert resolve_mlb_team("Chicago").ambiguous is True
    assert resolve_mlb_team("Chicago C").canonical == "chicago cubs"
    assert resolve_mlb_team("Chicago WS").canonical == "chicago white sox"
    assert resolve_mlb_team("A's").canonical == "athletics"
    assert resolve_mlb_team("Oakland Athletics").rejected is True
    assert scheduling_team_key("Cardinals", mlb=True) == STL
    assert scheduling_team_key("Cardinals") != STL


def test_universe_does_not_price_books_and_background_uses_exact_ids(monkeypatch: pytest.MonkeyPatch) -> None:
    import sports_hedge.application.catalogue_maintenance as maintenance

    source = inspect.getsource(maintenance)
    assert "get_order_book" not in source
    assert "list_events" not in source
    kalshi, _polymarket, _matchbook = _moneyline_markets()
    assert registered_canonical_key(kalshi, kalshi) is None

    class KalshiBooks:
        def __init__(self) -> None:
            self.tickers: list[str] = []

        async def get_order_book(self, event_ticker: str, ticker: str, **_filters):
            del event_ticker
            self.tickers.append(ticker)
            return {"orderbook": {"yes": [], "no": []}}

    books = KalshiBooks()

    async def direct(self, venue, *, lane, stage, source_id, coro, runtime=None):
        del self, venue, lane, stage, source_id, runtime
        return await coro, None

    monkeypatch.setattr(CataloguePriceEngine, "_provider_call", direct)
    engine = CataloguePriceEngine(kalshi=books, provider_access=object())
    now = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)
    row = ApprovedMarketCatalogueRow(
        catalogue_row_id="mlb-row",
        register_canonical_key=CANONICAL_MLB_GAME_WINNER,
        canonical_event_id="mlb-evt",
        competition="MLB",
        home_canonical="Pittsburgh Pirates",
        away_canonical="St. Louis Cardinals",
        kickoff_utc=datetime(2026, 9, 24, 16, 35, tzinfo=UTC),
        kalshi_event_ticker="KXMLBGAME-26SEP241235STLPIT",
        kalshi_series_ticker="KXMLBGAME",
        kalshi_market_tickers=[
            "KXMLBGAME-26SEP241235STLPIT-PIT",
            "KXMLBGAME-26SEP241235STLPIT-STL",
        ],
        required_outcomes=["home", "away"],
        family="game_winner",
        period="full_time",
        row_state=CatalogueRowState.ACTIVE,
        first_catalogued_at=now,
    )
    runtime = engine.reconstruct([row])[0]
    runtime.list_events_calls = 0
    result = asyncio.run(
        engine._refresh_kalshi_constituents(runtime, lane="background", skip_bound=True)
    )
    assert books.tickers == [
        "KXMLBGAME-26SEP241235STLPIT-PIT",
        "KXMLBGAME-26SEP241235STLPIT-STL",
    ]
    assert runtime.list_events_calls == 0
    assert set(result) == set(books.tickers)


def test_provider_concurrency_caps_are_unchanged() -> None:
    assert DEFAULT_PROVIDER_CONCURRENCY == {
        VenueName.MATCHBOOK: 4,
        VenueName.POLYMARKET: 8,
        VenueName.KALSHI: 4,
    }


def test_mlb_paper_settlement_fails_closed() -> None:
    trade = SimpleNamespace(
        competition="mlb",
        market_family=MarketFamily.GAME_WINNER,
        period=None,
        settlement_key=CANONICAL_MLB_GAME_WINNER,
        line=None,
    )
    assert _family_blocker(trade) == MLB_SETTLEMENT_NOT_EXECUTABLE
