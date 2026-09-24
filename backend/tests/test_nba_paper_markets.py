"""NBA PAPER onboarding: captured #453 fixtures, GAME_WINNER PAPER cell, fail-closed remainder.

Captured public payloads from 2026-09-21. PAPER / read-only. Not owner-live quotes.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sports_hedge.application.approved_market_catalogue import (
    derived_price_engine_working_set,
    required_outcomes_for_key,
)
from sports_hedge.application.catalogue_maintenance import (
    pair_identity_from_markets,
    persist_universe_catalogue_pass,
)
from sports_hedge.application.collector import matchbook_scope_discovery_params
from sports_hedge.application.complete_set import solver_model_for_pair
from sports_hedge.application.price_engine import (
    _canonical_kalshi_market,
    _canonical_polymarket_market,
    _sport_for_identity,
)
from sports_hedge.application.provider_access import DEFAULT_PROVIDER_CONCURRENCY
from sports_hedge.application.target_competitions import (
    PRINCIPAL_OPERATOR_COMPETITION_COUNT,
    TargetCompetitionCode,
    default_operator_competition_code_values,
    kalshi_series_tickers_for_codes,
    normalize_selected_competition_codes,
    operator_competition_catalog,
    polymarket_series_ids_for_codes,
    resolve_target_competition,
    resolve_target_competition_from_kalshi_ticker,
    scope_kalshi_event,
    scope_polymarket_event,
    selected_includes_nba,
    selected_includes_nfl,
    selected_includes_soccer,
)
from sports_hedge.catalogue.admission import (
    catalogue_allows_live_execution,
    catalogue_allows_solver,
)
from sports_hedge.catalogue.classify import classify_pair
from sports_hedge.catalogue.states import CatalogueApprovalState
from sports_hedge.domain.football import (
    CanonicalEvent,
    CanonicalMarket,
    CanonicalOutcome,
    CanonicalRunner,
    FootballPeriod,
    MarketFamily,
)
from sports_hedge.domain.models import VenueName
from sports_hedge.facts.aliases import football_alias_registry, resolve_team_name
from sports_hedge.matching.approved_register import (
    canonical_key_for_market,
    registered_canonical_key,
)
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.nba.constants import (
    CANONICAL_NBA_GAME_WINNER,
    CANONICAL_NBA_POINT_SPREAD,
    CANONICAL_NBA_TOTAL_POINTS,
    MATCHBOOK_NBA_COMPETITION_TAG_ID,
    NBA_EXCEPTIONAL_SETTLEMENT_CAVEAT,
    NBA_NORMAL_COMPLETION_NOT_PROVEN,
    NBA_POLYMARKET_EVIDENCE_REQUIRED,
    NBA_SETTLEMENT_FAIL_CLOSED_REASON,
    NBA_SPORT,
    NBA_UNSUPPORTED_FAMILY_REASON,
)
from sports_hedge.nba.detect import is_nba_payload
from sports_hedge.nba.labels import (
    NBA_SETTLEMENT_CAVEAT_OPERATOR_TEXT,
    cover_explanation,
    nba_operator_side_label,
    total_explanation,
)
from sports_hedge.nba.settlement import (
    nba_exceptional_status_blocker,
    nba_lifecycle_audit_detail,
    nba_lifecycle_observation,
    nba_paper_settlement,
    nba_tied_score_blocker,
)
from sports_hedge.nba.teams import NBA_ABBREVIATIONS, resolve_nba_team
from sports_hedge.normalization.venues import (
    KalshiNormalizer,
    MatchbookNormalizer,
    PolymarketNormalizer,
    VenueNormalizationError,
    _kalshi_market_family,
    _matchbook_market_family,
    _polymarket_market_family,
)
from sports_hedge.paper.result_resolution import resolve_paper_trade_settlement
from sports_hedge.paper.trades import (
    PaperLegFillKind,
    PaperTrade,
    PaperTradeAuditEvent,
    PaperTradeAuditEventType,
    PaperTradeLeg,
    PaperTradeState,
)
from sports_hedge.persistence.approved_market_catalogue import SqliteApprovedMarketCatalogueStore
from sports_hedge.venues.matchbook import (
    MatchbookDiscoveryError,
    select_american_football_sport_id,
    select_basketball_sport_id,
    select_football_sport_id,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "nba"
TIPOFF = datetime(2026, 10, 20, 19, 0, tzinfo=UTC)
NOW = datetime(2026, 9, 21, 8, 40, tzinfo=UTC)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _kalshi_bosdet() -> dict:
    event = copy.deepcopy(_load("kalshi_event_game_bosdet_open.json")["payload"])
    event["milestone"] = copy.deepcopy(_load("kalshi_milestone_bosdet_tipoff.json")["payload"])
    return event


def _pm_nyksas() -> dict:
    return copy.deepcopy(_load("polymarket_event_nyk_sas_finals_g5.json")["payload"])


def _pm_family(payload: dict, sports_type: str) -> dict:
    return next(item for item in payload["markets"] if item["sportsMarketType"] == sports_type)


def _normalize_kalshi_game(event_payload: dict | None = None):
    payload = event_payload or _kalshi_bosdet()
    event = KalshiNormalizer().normalize_event(payload)
    markets = KalshiNormalizer().assemble_canonical_markets(
        event, payload["markets"], event_payload=payload
    )
    return event, markets[0]


def _normalize_pm_family(sports_type: str, payload: dict | None = None):
    raw = payload or _pm_nyksas()
    event = PolymarketNormalizer().normalize_event(raw)
    market = PolymarketNormalizer().normalize_market(event, _pm_family(raw, sports_type))
    return event, market, _pm_family(raw, sports_type)


def _synthetic_pm_bosdet() -> dict:
    """Matcher-only clone of captured Gamma moneyline shape for BOS@DET.

    Not a captured live Polymarket book. Used to prove GAME_WINNER PAPER
    identity against the captured Kalshi BOSDET event at milestone tipoff.
    """

    captured = _pm_nyksas()
    moneyline = _pm_family(captured, "moneyline")
    return {
        "id": "synthetic-bosdet-pm",
        "startTime": "2026-10-20T19:00:00Z",
        "sport": captured["sport"],
        "teams": [
            {
                "name": "Celtics",
                "abbreviation": "bos",
                "alias": "Celtics",
                "ordering": "away",
                "league": "nba",
            },
            {
                "name": "Pistons",
                "abbreviation": "det",
                "alias": "Pistons",
                "ordering": "home",
                "league": "nba",
            },
        ],
        "markets": [
            {
                **moneyline,
                "id": "synthetic-bosdet-ml",
                "question": "Celtics vs. Pistons",
                "outcomes": '["Celtics", "Pistons"]',
                "sportsMarketType": "moneyline",
                "line": None,
            }
        ],
    }


def _nba_game_winner_trade() -> PaperTrade:
    return PaperTrade(
        trade_id="nba-1",
        opportunity_id="opp-nba",
        canonical_event_id="KXNBAGAME-26OCT20BOSDET",
        competition="NBA",
        home_team="Detroit Pistons",
        away_team="Boston Celtics",
        market_family=MarketFamily.GAME_WINNER,
        period=FootballPeriod.FULL_TIME,
        state=PaperTradeState.OPEN,
        opened_at=NOW,
        last_updated_at=NOW,
        legs=[
            PaperTradeLeg(
                venue=VenueName.POLYMARKET,
                outcome="home",
                currency="USD",
                requested_stake=Decimal(1),
                filled_stake=Decimal(1),
                displayed_odds=Decimal("1.9"),
                filled_odds=Decimal("1.9"),
                source_market_id="synthetic-bosdet-ml",
                source_event_id="synthetic-bosdet-pm",
                source_runner_id="110136933550893624733134445460153301975615510734202337526927943993346922198810",
                fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
            ),
            PaperTradeLeg(
                venue=VenueName.KALSHI,
                outcome="away",
                currency="USD",
                requested_stake=Decimal(1),
                filled_stake=Decimal(1),
                displayed_odds=Decimal("2.1"),
                filled_odds=Decimal("2.1"),
                source_market_id="KXNBAGAME-26OCT20BOSDET-BOS",
                source_event_id="KXNBAGAME-26OCT20BOSDET",
                source_contract_id="KXNBAGAME-26OCT20BOSDET-BOS",
                fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
            ),
        ],
    )


def _kalshi_bos_finalized() -> dict[str, dict]:
    return {
        "KXNBAGAME-26OCT20BOSDET-BOS": {
            "ticker": "KXNBAGAME-26OCT20BOSDET-BOS",
            "status": "finalized",
            "result": "yes",
        }
    }


def _pm_resolved(*, celtics_win: bool, prices: list[str] | None = None) -> dict:
    if prices is None:
        prices = ["0", "1"] if celtics_win else ["1", "0"]
    return {
        "id": "synthetic-bosdet-ml",
        "closed": True,
        "umaResolutionStatus": "resolved",
        "outcomes": ["Pistons", "Celtics"],
        "outcomePrices": prices,
    }


def _with_nba_pre_result(trade: PaperTrade) -> PaperTrade:
    proven = trade.model_copy(update={"audit": []})
    in_play = nba_lifecycle_observation(["open", "active"], observed_at=NOW)
    proven.audit.append(
        PaperTradeAuditEvent(
            occurred_at=NOW,
            event_type=PaperTradeAuditEventType.NBA_LIFECYCLE_OBSERVED,
            detail=nba_lifecycle_audit_detail(in_play),
        )
    )
    return proven


def test_thirty_franchises_and_city_ambiguity() -> None:
    assert len(NBA_ABBREVIATIONS) == 30
    assert resolve_nba_team("BOS").canonical == "boston celtics"
    assert resolve_nba_team("Celtics").abbreviation == "BOS"
    assert resolve_nba_team("NYK").abbreviation == "NYK"
    assert resolve_nba_team("Knicks").abbreviation == "NYK"
    assert resolve_nba_team("Nets").abbreviation == "BKN"
    assert resolve_nba_team("Lakers").abbreviation == "LAL"
    assert resolve_nba_team("Clippers").abbreviation == "LAC"
    assert resolve_nba_team("Los Angeles L").abbreviation == "LAL"
    assert resolve_nba_team("Los Angeles C").abbreviation == "LAC"
    ny = resolve_nba_team("New York")
    assert ny.ambiguous is True and ny.canonical is None
    la = resolve_nba_team("Los Angeles")
    assert la.ambiguous is True and la.canonical is None
    assert resolve_nba_team("NY").ambiguous is True
    assert resolve_nba_team("LA").ambiguous is True
    assert resolve_nba_team("Seattle SuperSonics").rejected is True
    assert resolve_nba_team("New Jersey Nets").rejected is True
    assert resolve_nba_team("Charlotte Bobcats").rejected is True
    assert resolve_nba_team("New Orleans Hornets").rejected is True


def test_nba_aliases_do_not_leak_into_soccer() -> None:
    assert resolve_team_name("Celtics") == "celtics"
    assert football_alias_registry.resolve("Lakers") == "lakers"
    assert resolve_nba_team("Arsenal").ok is False
    assert resolve_nba_team("Newcastle United").ok is False
    from sports_hedge.application.hot_identity import scheduling_team_key

    assert scheduling_team_key("Kings") != "sacramento kings"
    assert scheduling_team_key("Jazz") != "utah jazz"


def test_nba_is_permanently_selectable_and_not_default() -> None:
    catalog = {row["code"]: row for row in operator_competition_catalog()}
    assert "nba" in catalog
    assert catalog["nba"]["selectable"] is True
    assert catalog["nba"]["selector_label"] == "NBA"
    assert catalog["nba"]["group_id"] == "nba"
    assert catalog["nba"]["default_selected"] is False
    assert "nba" not in default_operator_competition_code_values()
    assert selected_includes_nba(["nba"]) is True
    assert selected_includes_nba(None) is False
    assert selected_includes_nfl(["nba"]) is False
    assert selected_includes_soccer(["nba"]) is False
    assert selected_includes_soccer(["premier_league", "nba"]) is True
    assert PRINCIPAL_OPERATOR_COMPETITION_COUNT == 37
    assert resolve_target_competition("NBA") is not None
    assert resolve_target_competition("WNBA") is None
    assert resolve_target_competition("NCAAB") is not None
    assert resolve_target_competition_from_kalshi_ticker("KXNBAGAME") is not None
    assert resolve_target_competition_from_kalshi_ticker("KXNBATEAMTOTAL") is None
    assert TargetCompetitionCode.NBA.value == "nba"
    assert normalize_selected_competition_codes(["nba"]) == ("nba",)
    assert polymarket_series_ids_for_codes(["nba"]) == ["10345"]
    assert kalshi_series_tickers_for_codes(["nba"]) == [
        "KXNBAGAME",
        "KXNBASPREAD",
        "KXNBATOTAL",
    ]
    assert "KXNBAGAME" not in kalshi_series_tickers_for_codes(
        default_operator_competition_code_values()
    )


def test_kalshi_bosdet_uses_milestone_tipoff_not_occurrence() -> None:
    event, market = _normalize_kalshi_game()
    assert event.sport == NBA_SPORT
    assert event.home_team == "detroit pistons"
    assert event.away_team == "boston celtics"
    assert event.kickoff_utc == TIPOFF
    assert event.kickoff_utc != datetime(2026, 10, 20, 22, 0, tzinfo=UTC)
    assert event.source_event_id == "KXNBAGAME-26OCT20BOSDET"
    assert market.family is MarketFamily.GAME_WINNER
    assert market.period is FootballPeriod.FULL_TIME
    assert market.line is None
    assert {runner.outcome for runner in market.runners} == {
        CanonicalOutcome.HOME,
        CanonicalOutcome.AWAY,
    }
    assert CanonicalOutcome.DRAW not in {runner.outcome for runner in market.runners}
    assert {runner.source_runner_id for runner in market.runners} == {
        "KXNBAGAME-26OCT20BOSDET-BOS:YES",
        "KXNBAGAME-26OCT20BOSDET-DET:YES",
    }
    without_milestone = copy.deepcopy(_load("kalshi_event_game_bosdet_open.json")["payload"])
    with pytest.raises(VenueNormalizationError, match="milestone"):
        KalshiNormalizer().normalize_event(without_milestone)


def test_kalshi_new_york_city_label_resolves_via_nyk_ticker() -> None:
    event_payload = copy.deepcopy(_load("kalshi_event_game_phinyk_open.json")["payload"])
    event_payload["milestone"] = {
        "id": "synthetic-phinyk-milestone",
        "type": "basketball_game",
        "title": "Philadelphia at New York",
        "start_date": "2026-10-21T02:00:00Z",
        "details": {
            "league": "NBA",
            "status": "scheduled",
            "home_team_id": "nyk-uuid",
            "away_team_id": "phi-uuid",
        },
    }
    for market in event_payload["markets"]:
        if str(market.get("ticker", "")).endswith("-NYK"):
            market["custom_strike"] = {"basketball_team": "nyk-uuid"}
        elif str(market.get("ticker", "")).endswith("-PHI"):
            market["custom_strike"] = {"basketball_team": "phi-uuid"}
    event, market = _normalize_kalshi_game(event_payload)
    assert event.home_team == "new york knicks"
    assert event.away_team == "philadelphia 76ers"
    assert market.family is MarketFamily.GAME_WINNER


def test_polymarket_captured_moneyline_spread_and_total() -> None:
    event, moneyline, _raw_ml = _normalize_pm_family("moneyline")
    assert event.home_team == "san antonio spurs"
    assert event.away_team == "new york knicks"
    assert event.source_event_id == "567958"
    assert moneyline.source_market_id == "2459554"
    assert {runner.source_runner_id for runner in moneyline.runners} == {
        "110136933550893624733134445460153301975615510734202337526927943993346922198810",
        "71785764076184626205908849503698513178149020682140821352707942313255784790169",
    }
    _, spread, _raw_spread = _normalize_pm_family("spreads")
    assert spread.family is MarketFamily.POINT_SPREAD
    assert spread.line == Decimal("-5.5")
    assert canonical_key_for_market(spread) == f"{CANONICAL_NBA_POINT_SPREAD}:-5.5"
    assert "must win by 6+" in nba_operator_side_label(spread, CanonicalOutcome.HOME)
    _, total, _ = _normalize_pm_family("totals")
    assert total.family is MarketFamily.TOTAL_POINTS
    assert total.line == Decimal("218.5")
    assert canonical_key_for_market(total) == f"{CANONICAL_NBA_TOTAL_POINTS}:218.5"
    assert "219+ combined points" in nba_operator_side_label(total, CanonicalOutcome.OVER)
    lac = PolymarketNormalizer().normalize_event(
        copy.deepcopy(_load("polymarket_event_lac_lal_derby.json")["payload"])
    )
    assert lac.home_team == "los angeles lakers"
    assert lac.away_team == "los angeles clippers"
    bkn = PolymarketNormalizer().normalize_event(
        copy.deepcopy(_load("polymarket_event_bkn_nyk_derby.json")["payload"])
    )
    assert bkn.home_team == "new york knicks"
    assert bkn.away_team == "brooklyn nets"


def test_game_winner_kalshi_polymarket_is_paper_admitted() -> None:
    _, kalshi = _normalize_kalshi_game()
    _, pm, _ = _normalize_pm_family("moneyline", _synthetic_pm_bosdet())
    matcher = MarketMatcher()
    result = matcher.match(kalshi, pm)
    assert result.matched, result.reasons
    assert registered_canonical_key(kalshi, pm) == CANONICAL_NBA_GAME_WINNER
    assert NBA_EXCEPTIONAL_SETTLEMENT_CAVEAT in result.reasons
    assert catalogue_allows_solver(kalshi, pm)
    assert catalogue_allows_live_execution(kalshi, pm) is False
    assessment = classify_pair(kalshi, pm)
    assert assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
    assert assessment.settlement_assumption == "normal_full_game_completion"
    assert solver_model_for_pair(kalshi, pm) == "simple_complete_set"


def test_spread_and_total_kalshi_polymarket_stay_fail_closed() -> None:
    kalshi_event = _kalshi_bosdet()
    kalshi_event["series_ticker"] = "KXNBASPREAD"
    kalshi_event["event_ticker"] = "KXNBASPREAD-26OCT20BOSDET"
    spread_payload = {
        "ticker": "KXNBASPREAD-26OCT20BOSDET-DET7",
        "title": "Detroit wins by over 5.5",
        "yes_sub_title": "Detroit",
    }
    event = KalshiNormalizer().normalize_event(kalshi_event)
    kalshi_spread = KalshiNormalizer().assemble_canonical_markets(
        event, [spread_payload], event_payload=kalshi_event
    )[0]
    _, pm_spread, _ = _normalize_pm_family("spreads")
    # Different fixtures; even same-line structural keys must not PAPER-match
    # across Kalshi↔Polymarket for NBA spreads.
    assert registered_canonical_key(kalshi_spread, pm_spread) is None
    _, pm_total, _ = _normalize_pm_family("totals")
    kalshi_event["series_ticker"] = "KXNBATOTAL"
    kalshi_event["event_ticker"] = "KXNBATOTAL-26OCT20BOSDET"
    total_payload = {
        "ticker": "KXNBATOTAL-26OCT20BOSDET-218",
        "title": "Over 218.5 points",
        "yes_sub_title": "Over",
    }
    total_event = KalshiNormalizer().normalize_event(kalshi_event)
    kalshi_total = KalshiNormalizer().assemble_canonical_markets(
        total_event, [total_payload], event_payload=kalshi_event
    )[0]
    assert kalshi_total.line == Decimal("218.5")
    assert registered_canonical_key(kalshi_total, pm_total) is None
    assert MarketMatcher().match(kalshi_total, pm_total).matched is False


def test_integer_lines_and_period_props_outrights_rejected() -> None:
    payload = _pm_nyksas()
    event = PolymarketNormalizer().normalize_event(payload)
    integer = copy.deepcopy(_pm_family(payload, "spreads"))
    integer["line"] = -5
    integer["question"] = "Spread: Spurs (-5)"
    with pytest.raises(VenueNormalizationError, match="half-point"):
        PolymarketNormalizer().normalize_market(event, integer)
    integer_total = copy.deepcopy(_pm_family(payload, "totals"))
    integer_total["line"] = 218
    with pytest.raises(VenueNormalizationError, match="half-point"):
        PolymarketNormalizer().normalize_market(event, integer_total)
    team_total = copy.deepcopy(_pm_family(payload, "moneyline"))
    team_total["sportsMarketType"] = "team_totals"
    with pytest.raises(VenueNormalizationError, match="unsupported Polymarket NBA market type"):
        PolymarketNormalizer().normalize_market(event, team_total)
    milestone = _load("kalshi_milestone_bosdet_tipoff.json")["payload"]
    for ticker in ("KXNBATEAMTOTAL", "KXNBA1H", "KXNBASUMMER"):
        with pytest.raises(VenueNormalizationError, match="unsupported NBA Kalshi series"):
            KalshiNormalizer().normalize_event(
                {
                    "event_ticker": f"{ticker}-26OCT20BOSDET",
                    "series_ticker": ticker,
                    "title": "Boston vs Detroit",
                    "milestone": milestone,
                }
            )


def test_matchbook_championship_and_wnba_are_not_nba_game_books() -> None:
    championship = _load("matchbook_event_nba_championship_2026.json")["payload"]
    assert is_nba_payload(championship) is True
    with pytest.raises(VenueNormalizationError, match="outright"):
        MatchbookNormalizer().normalize_event(championship)
    wnba = _load("matchbook_event_wnba_dream_liberty_shape.json")["payload"]
    assert is_nba_payload(wnba) is False
    sports = [
        {"id": 15, "name": "Football"},
        {"id": 1, "name": "American Football"},
        _load("matchbook_lookups_sports_basketball.json")["payload"],
    ]
    assert select_football_sport_id(sports) == 15
    assert select_american_football_sport_id(sports) == 1
    assert select_basketball_sport_id(sports) == 4
    with pytest.raises(MatchbookDiscoveryError, match="Basketball"):
        select_basketball_sport_id([{"id": 15, "name": "Football"}])


def test_soccer_recognisers_do_not_classify_nba_markets() -> None:
    kalshi_game = _kalshi_bosdet()["markets"][0]
    with pytest.raises(VenueNormalizationError, match="soccer recogniser does not classify NBA markets"):
        _kalshi_market_family(
            kalshi_game, home_team="Detroit Pistons", away_team="Boston Celtics"
        )
    championship = _load("matchbook_event_nba_championship_2026.json")["payload"]
    with pytest.raises(VenueNormalizationError, match="soccer recogniser does not classify NBA markets"):
        _matchbook_market_family(
            "Winner",
            championship,
            home_team="Oklahoma City Thunder",
            away_team="San Antonio Spurs",
        )
    pm_market = _pm_family(_pm_nyksas(), "moneyline")
    with pytest.raises(VenueNormalizationError, match="soccer recogniser does not classify NBA markets"):
        _polymarket_market_family(
            pm_market["question"],
            pm_market,
            home_team="San Antonio Spurs",
            away_team="New York Knicks",
        )


def test_generic_new_york_and_los_angeles_never_choose_a_franchise() -> None:
    with pytest.raises(VenueNormalizationError):
        MatchbookNormalizer().normalize_event(
            {
                "id": "ny-ambiguous",
                "name": "Boston Celtics at New York",
                "start": "2026-10-20T19:00:00.000Z",
                "sport-id": 4,
                "meta-tags": [
                    {"name": "Basketball", "type": "SPORT"},
                    {"name": "NBA", "type": "COMPETITION", "id": 406202315670010},
                ],
            }
        )
    with pytest.raises(VenueNormalizationError):
        MatchbookNormalizer().normalize_event(
            {
                "id": "la-ambiguous",
                "name": "Boston Celtics at Los Angeles",
                "start": "2026-10-20T19:00:00.000Z",
                "sport-id": 4,
                "meta-tags": [
                    {"name": "Basketball", "type": "SPORT"},
                    {"name": "NBA", "type": "COMPETITION", "id": 406202315670010},
                ],
            }
        )


def test_nearby_games_do_not_collapse_on_close_tipoff() -> None:
    left, _ = _normalize_kalshi_game()
    other = left.model_copy(update={"kickoff_utc": datetime(2026, 10, 20, 19, 6, tzinfo=UTC)})
    assert EventMatcher().match(left, other).matched is False
    same_window = left.model_copy(update={"kickoff_utc": datetime(2026, 10, 20, 19, 5, tzinfo=UTC)})
    assert EventMatcher().match(left, same_window).matched is True


def test_automatic_settlement_fails_closed_without_lifecycle_proof() -> None:
    assert nba_tied_score_blocker(100, 100) == NBA_SETTLEMENT_FAIL_CLOSED_REASON
    assert nba_exceptional_status_blocker("cancelled") == NBA_SETTLEMENT_FAIL_CLOSED_REASON
    assert nba_exceptional_status_blocker("fair price") == NBA_SETTLEMENT_FAIL_CLOSED_REASON
    assert nba_exceptional_status_blocker("50-50") == NBA_SETTLEMENT_FAIL_CLOSED_REASON
    trade = _nba_game_winner_trade()
    kalshi_only = resolve_paper_trade_settlement(trade, kalshi_markets=_kalshi_bos_finalized())
    assert kalshi_only.winning_outcome is None
    assert kalshi_only.blocker == NBA_NORMAL_COMPLETION_NOT_PROVEN
    proven = _with_nba_pre_result(trade)
    still_kalshi_only = resolve_paper_trade_settlement(
        proven,
        kalshi_markets=_kalshi_bos_finalized(),
    )
    assert still_kalshi_only.winning_outcome is None
    assert still_kalshi_only.blocker == NBA_POLYMARKET_EVIDENCE_REQUIRED
    cancelled = nba_lifecycle_observation(["cancelled"], observed_at=NOW)
    blocked_trade = proven.model_copy(update={"audit": list(proven.audit)})
    blocked_trade.audit.append(
        PaperTradeAuditEvent(
            occurred_at=NOW,
            event_type=PaperTradeAuditEventType.NBA_LIFECYCLE_OBSERVED,
            detail=nba_lifecycle_audit_detail(cancelled),
        )
    )
    later = resolve_paper_trade_settlement(
        blocked_trade,
        kalshi_markets=_kalshi_bos_finalized(),
        polymarket_market=_pm_resolved(celtics_win=True),
    )
    assert later.winning_outcome is None
    assert later.blocker == NBA_SETTLEMENT_FAIL_CLOSED_REASON


def test_nba_kalshi_polymarket_winner_requires_polymarket_evidence() -> None:
    proven = _with_nba_pre_result(_nba_game_winner_trade())
    missing_pm = resolve_paper_trade_settlement(
        proven,
        kalshi_markets=_kalshi_bos_finalized(),
    )
    assert missing_pm.winning_outcome is None
    assert missing_pm.blocker == NBA_POLYMARKET_EVIDENCE_REQUIRED
    split = resolve_paper_trade_settlement(
        proven,
        kalshi_markets=_kalshi_bos_finalized(),
        polymarket_market=_pm_resolved(celtics_win=True, prices=["0.5", "0.5"]),
    )
    assert split.winning_outcome is None
    assert split.blocker == NBA_SETTLEMENT_FAIL_CLOSED_REASON
    unknown = resolve_paper_trade_settlement(
        proven,
        kalshi_markets=_kalshi_bos_finalized(),
        polymarket_market={
            "id": "synthetic-bosdet-ml",
            "closed": True,
            "umaResolutionStatus": "disputed",
            "outcomes": ["Pistons", "Celtics"],
            "outcomePrices": ["0", "1"],
        },
    )
    assert unknown.winning_outcome is None
    assert unknown.blocker in {
        NBA_SETTLEMENT_FAIL_CLOSED_REASON,
        NBA_NORMAL_COMPLETION_NOT_PROVEN,
    }
    cancelled_pm = resolve_paper_trade_settlement(
        proven,
        kalshi_markets=_kalshi_bos_finalized(),
        polymarket_market={
            "id": "synthetic-bosdet-ml",
            "closed": True,
            "umaResolutionStatus": "resolved",
            "status": "cancelled",
            "outcomes": ["Pistons", "Celtics"],
            "outcomePrices": ["0.5", "0.5"],
        },
    )
    assert cancelled_pm.winning_outcome is None
    assert cancelled_pm.blocker == NBA_SETTLEMENT_FAIL_CLOSED_REASON
    ready = resolve_paper_trade_settlement(
        proven,
        kalshi_markets=_kalshi_bos_finalized(),
        polymarket_market=_pm_resolved(celtics_win=True),
    )
    assert ready.blocker is None
    assert ready.winning_outcome == "away"
    conflict = resolve_paper_trade_settlement(
        proven,
        kalshi_markets=_kalshi_bos_finalized(),
        polymarket_market=_pm_resolved(celtics_win=False),
    )
    assert conflict.winning_outcome is None
    assert conflict.blocker == "conflicting_provider_results"


def test_nba_spread_and_total_trades_are_rejected_by_settlement() -> None:
    winner = _nba_game_winner_trade()
    spread = winner.model_copy(update={"market_family": MarketFamily.POINT_SPREAD, "line": Decimal("-5.5")})
    total = winner.model_copy(update={"market_family": MarketFamily.TOTAL_POINTS, "line": Decimal("218.5")})
    proven_spread = _with_nba_pre_result(spread)
    proven_total = _with_nba_pre_result(total)
    spread_resolution = resolve_paper_trade_settlement(
        proven_spread,
        kalshi_markets=_kalshi_bos_finalized(),
        polymarket_market=_pm_resolved(celtics_win=True),
    )
    total_resolution = resolve_paper_trade_settlement(
        proven_total,
        kalshi_markets=_kalshi_bos_finalized(),
        polymarket_market=_pm_resolved(celtics_win=True),
    )
    assert spread_resolution.winning_outcome is None
    assert spread_resolution.blocker == NBA_UNSUPPORTED_FAMILY_REASON
    assert total_resolution.winning_outcome is None
    assert total_resolution.blocker == NBA_UNSUPPORTED_FAMILY_REASON
    _, kalshi_spread = _normalize_kalshi_game()  # GAME_WINNER — use PM spread instead
    _, pm_spread, _ = _normalize_pm_family("spreads")
    _, pm_total, _ = _normalize_pm_family("totals")
    kalshi_spread_market = kalshi_spread.model_copy(
        update={"family": MarketFamily.POINT_SPREAD, "line": Decimal("-5.5")}
    )
    assert registered_canonical_key(kalshi_spread_market, pm_spread) is None
    kalshi_total_market = kalshi_spread.model_copy(
        update={"family": MarketFamily.TOTAL_POINTS, "line": Decimal("218.5")}
    )
    assert registered_canonical_key(kalshi_total_market, pm_total) is None


def test_matchbook_nba_only_scope_uses_competition_tag() -> None:
    nba_only = matchbook_scope_discovery_params(
        ["nba"],
        basketball_sport_id="4",
        football_sport_id="15",
        american_football_sport_id="1",
    )
    assert nba_only["sport-ids"] == "4"
    assert nba_only["tag-ids"] == MATCHBOOK_NBA_COMPETITION_TAG_ID
    mixed = matchbook_scope_discovery_params(
        ["nba", "premier_league", "nfl"],
        basketball_sport_id="4",
        football_sport_id="15",
        american_football_sport_id="1",
    )
    assert mixed["sport-ids"] == "15,1,4"
    assert "tag-ids" not in mixed
    soccer_only = matchbook_scope_discovery_params(["premier_league"], football_sport_id="15")
    assert soccer_only == {}
    nba_and_ncaab = matchbook_scope_discovery_params(
        ["nba", "ncaab"],
        basketball_sport_id="4",
    )
    assert nba_and_ncaab["sport-ids"] == "4"
    assert "tag-ids" not in nba_and_ncaab
    from inspect import getsource

    from sports_hedge.application.collector import ReadOnlyCrossVenueCollector

    discovery_src = getsource(ReadOnlyCrossVenueCollector._matchbook_discovery_params_for_scope)
    soccer_only_gate = (
        "if (\n            not selected_includes_nfl(codes)\n"
        "            and not selected_includes_nba(codes)\n"
        "            and not selected_includes_ncaab(codes)\n"
        "            and not selected_includes_mlb(codes)\n"
        "            and not selected_includes_tennis(codes)\n        ):"
    )
    assert soccer_only_gate in discovery_src
    assert discovery_src.index(soccer_only_gate) < discovery_src.index(
        "resolve_football_sport_id"
    )
    assert discovery_src.index(soccer_only_gate) < discovery_src.index(
        "resolve_basketball_sport_id"
    )


def test_scope_diagnostics_report_basketball_for_nba() -> None:
    pm = scope_polymarket_event(
        {
            "id": "567958",
            "title": "Knicks vs. Spurs",
            "sport": {"sport": "nba", "series": "10345", "name": "NBA"},
        },
        selected_codes=["nba"],
    )
    assert pm.allowed is True
    assert pm.sport == "basketball"
    kalshi = scope_kalshi_event(
        {"series_ticker": "KXNBAGAME", "ticker": "KXNBAGAME-26OCT20BOSDET", "title": "Boston vs Detroit"},
        selected_codes=["nba"],
    )
    assert kalshi.allowed is True
    assert kalshi.sport == "basketball"
    soccer = scope_polymarket_event(
        {
            "id": "pm-epl",
            "title": "Arsenal vs Chelsea",
            "series": [{"id": "10188", "title": "Premier League"}],
        }
    )
    assert soccer.sport == "football"


def test_missing_and_synthetic_polymarket_tokens_cannot_become_executable_paper() -> None:
    payload = _pm_nyksas()
    event = PolymarketNormalizer().normalize_event(payload)
    market_payload = copy.deepcopy(_pm_family(payload, "moneyline"))
    market_payload["clobTokenIds"] = []
    with pytest.raises(VenueNormalizationError, match="exact CLOB token"):
        PolymarketNormalizer().normalize_market(event, market_payload)
    condition = str(market_payload["conditionId"])
    market_payload["clobTokenIds"] = [f"{condition}:0", f"{condition}:1"]
    with pytest.raises(VenueNormalizationError, match="not executable"):
        PolymarketNormalizer().normalize_market(event, market_payload)


def test_operator_plain_english_labels() -> None:
    assert "must win by 6+" in cover_explanation(team="San Antonio Spurs", signed_line=Decimal("-5.5"))
    assert "may lose by up to 5, or win" in cover_explanation(
        team="New York Knicks", signed_line=Decimal("5.5")
    )
    assert total_explanation(side=CanonicalOutcome.OVER, line=Decimal("218.5")) == (
        "Over 218.5 · 219+ combined points"
    )
    assert "normal completed NBA game" in NBA_SETTLEMENT_CAVEAT_OPERATOR_TEXT


def test_provider_concurrency_unchanged() -> None:
    assert DEFAULT_PROVIDER_CONCURRENCY == {
        VenueName.MATCHBOOK: 4,
        VenueName.POLYMARKET: 8,
        VenueName.KALSHI: 4,
    }


def test_captured_payloads_are_nba() -> None:
    assert is_nba_payload(_kalshi_bosdet()) is True
    assert is_nba_payload(_pm_nyksas()) is True
    assert is_nba_payload(_load("polymarket_sports_nba.json")["payload"]) is True
    assert is_nba_payload(_load("kalshi_series_kxnbaspread.json")["payload"]) is True
    assert is_nba_payload(_load("kalshi_series_kxnbatotal.json")["payload"]) is True


def test_matchbook_pairs_are_not_paper_admitted() -> None:
    _, kalshi = _normalize_kalshi_game()
    _, pm, _ = _normalize_pm_family("moneyline", _synthetic_pm_bosdet())
    event = CanonicalEvent(
        sport=NBA_SPORT,
        competition="NBA",
        home_team="detroit pistons",
        away_team="boston celtics",
        kickoff_utc=TIPOFF,
        source_venue=VenueName.MATCHBOOK,
        source_event_id="mb-nba-game",
    )
    matchbook = CanonicalMarket(
        event=event,
        source_venue=VenueName.MATCHBOOK,
        source_market_id="mb-nba-ml",
        family=MarketFamily.GAME_WINNER,
        period=FootballPeriod.FULL_TIME,
        line=None,
        settlement=nba_paper_settlement(family=MarketFamily.GAME_WINNER),
        runners=[
            CanonicalRunner(source_runner_id="mb-home", outcome=CanonicalOutcome.HOME, label="Pistons"),
            CanonicalRunner(source_runner_id="mb-away", outcome=CanonicalOutcome.AWAY, label="Celtics"),
        ],
    )
    assert registered_canonical_key(matchbook, kalshi) is None
    assert registered_canonical_key(matchbook, pm) is None
    assert MarketMatcher().match(matchbook, kalshi).matched is False
    assert pair_identity_from_markets(matchbook, kalshi) is None
    assert pair_identity_from_markets(matchbook, pm) is None


def test_game_winner_catalogue_preserves_exact_native_ids() -> None:
    _, kalshi = _normalize_kalshi_game()
    _, pm, pm_raw = _normalize_pm_family("moneyline", _synthetic_pm_bosdet())
    identity = pair_identity_from_markets(
        kalshi,
        pm,
        kalshi_event_payload=_kalshi_bosdet(),
        kalshi_series_payload={
            "ticker": "KXNBAGAME",
            "fee_type": "quadratic_with_maker_fees",
            "fee_multiplier": "1",
        },
        polymarket_market_payload=pm_raw,
    )
    assert identity is not None
    assert identity.register_canonical_key == CANONICAL_NBA_GAME_WINNER
    assert required_outcomes_for_key(CANONICAL_NBA_GAME_WINNER) == ["home", "away"]
    store = SqliteApprovedMarketCatalogueStore(":memory:")
    try:
        persist_universe_catalogue_pass(
            store,
            canonical_event_id="evt-bosdet",
            competition="NBA",
            home_canonical="detroit pistons",
            away_canonical="boston celtics",
            kickoff_utc=TIPOFF,
            pairs=[identity],
            now=NOW,
            generation_id="g1",
            family_discovery=None,
            terminal=False,
            allow_disappearance=False,
        )
        row = store.get_active("evt-bosdet", CANONICAL_NBA_GAME_WINNER)
        assert row is not None
        assert row.family == MarketFamily.GAME_WINNER.value
        assert row.kalshi_event_ticker == "KXNBAGAME-26OCT20BOSDET"
        assert row.polymarket_event_id == "synthetic-bosdet-pm"
        assert row.polymarket_market_id == "synthetic-bosdet-ml"
        assert {item.native_id for item in row.polymarket_token_ids} == {
            "110136933550893624733134445460153301975615510734202337526927943993346922198810",
            "71785764076184626205908849503698513178149020682140821352707942313255784790169",
        }
        working = derived_price_engine_working_set([row])
        assert _sport_for_identity(working[0]) == NBA_SPORT
        reconstructed_pm = _canonical_polymarket_market(working[0])
        reconstructed_k = _canonical_kalshi_market(working[0])
        assert reconstructed_pm is not None
        assert reconstructed_k is not None
        assert reconstructed_pm.event.sport == NBA_SPORT
        assert reconstructed_k.event.sport == NBA_SPORT
        assert reconstructed_pm.source_market_id == "synthetic-bosdet-ml"
        assert reconstructed_k.event.source_event_id == "KXNBAGAME-26OCT20BOSDET"
    finally:
        store.close()
