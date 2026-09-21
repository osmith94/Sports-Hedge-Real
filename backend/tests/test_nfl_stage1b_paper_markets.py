"""NFL Stage 1B: golden Phase-1 fixtures, three PAPER families, fail-closed rejects.

Captured public payloads from 2026-09-20. PAPER / read-only. Not owner-live quotes.
"""

from __future__ import annotations

import copy
import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from sports_hedge.application.complete_set import solver_model_for_pair
from sports_hedge.application.target_competitions import (
    default_operator_competition_code_values,
    resolve_target_competition_from_kalshi_ticker,
    selected_includes_nfl,
)
from sports_hedge.catalogue.admission import catalogue_allows_live_execution, catalogue_allows_solver
from sports_hedge.catalogue.classify import classify_pair
from sports_hedge.catalogue.states import CatalogueApprovalState
from sports_hedge.domain.football import CanonicalOutcome, FootballPeriod, MarketFamily
from sports_hedge.domain.models import VenueName
from sports_hedge.facts.aliases import football_alias_registry, resolve_team_name
from sports_hedge.matching.approved_register import canonical_key_for_market, registered_canonical_key
from sports_hedge.matching.events import EventMatcher
from sports_hedge.matching.markets import MarketMatcher
from sports_hedge.nfl.constants import (
    CANONICAL_NFL_GAME_WINNER,
    CANONICAL_NFL_POINT_SPREAD,
    CANONICAL_NFL_TOTAL_POINTS,
    NFL_EXCEPTIONAL_SETTLEMENT_CAVEAT,
    NFL_NORMAL_COMPLETION_NOT_PROVEN,
    NFL_SETTLEMENT_FAIL_CLOSED_REASON,
    NFL_SPORT,
)
from sports_hedge.nfl.detect import is_nfl_payload
from sports_hedge.nfl.labels import (
    NFL_SETTLEMENT_CAVEAT_OPERATOR_TEXT,
    cover_explanation,
    nfl_operator_side_label,
    total_explanation,
)
from sports_hedge.nfl.settlement import (
    nfl_exceptional_status_blocker,
    nfl_lifecycle_audit_detail,
    nfl_lifecycle_observation,
    nfl_tied_score_blocker,
)
from sports_hedge.nfl.teams import NFL_ABBREVIATIONS, resolve_nfl_team
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
from sports_hedge.venues.matchbook import (
    MatchbookDiscoveryError,
    select_american_football_sport_id,
    select_football_sport_id,
)

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "nfl"
KICKOFF = datetime(2026, 9, 21, 0, 20, tzinfo=UTC)
NOW = datetime(2026, 9, 20, 21, 0, tzinfo=UTC)


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _kalshi_game_event() -> dict:
    event = copy.deepcopy(_load("kalshi_event_game_indkc_open.json")["payload"])
    event["milestone"] = copy.deepcopy(_load("kalshi_milestone_indkc_kickoff.json")["payload"])
    return event


def _kalshi_spread_event_and_market() -> tuple[dict, dict]:
    blob = _load("kalshi_market_spread_indkc_kc7.json")
    event = {
        **copy.deepcopy(blob["event"]),
        "series_ticker": "KXNFLSPREAD",
        "milestone": copy.deepcopy(_load("kalshi_milestone_indkc_kickoff.json")["payload"]),
    }
    return event, copy.deepcopy(blob["payload"])


def _kalshi_total_event_and_market() -> tuple[dict, dict]:
    blob = _load("kalshi_market_total_indkc_48.json")
    event = {
        **copy.deepcopy(blob["event"]),
        "series_ticker": "KXNFLTOTAL",
        "milestone": copy.deepcopy(_load("kalshi_milestone_indkc_kickoff.json")["payload"]),
    }
    return event, copy.deepcopy(blob["payload"])


def _pm_indkc() -> dict:
    return copy.deepcopy(_load("polymarket_event_indkc_upcoming.json")["payload"])


def _mb_indkc() -> dict:
    return copy.deepcopy(_load("matchbook_event_indkc_upcoming.json")["payload"])


def _mb_market(event: dict, *, name: str, handicap: float | int | None = None) -> dict:
    for market in event["markets"]:
        if market["name"] != name:
            continue
        if handicap is None or float(market.get("handicap") or 0) == float(handicap):
            return copy.deepcopy(market)
    raise AssertionError(f"missing Matchbook market {name} handicap={handicap}")


def _clone_mb_spread(event: dict, *, home_line: Decimal) -> dict:
    market = _mb_market(event, name="Handicap", handicap=4.5)
    market["handicap"] = float(abs(home_line))
    away_line = -home_line
    for runner in market["runners"]:
        if "Chiefs" in runner["name"]:
            runner["handicap"] = float(home_line)
            runner["name"] = f"Kansas City Chiefs {home_line:+}"
        else:
            runner["handicap"] = float(away_line)
            runner["name"] = f"Indianapolis Colts {away_line:+}"
    return market


def _clone_mb_total(event: dict, *, line: Decimal) -> dict:
    market = _mb_market(event, name="Total", handicap=45.5)
    market["handicap"] = float(line)
    for runner in market["runners"]:
        runner["handicap"] = float(line)
        side = "OVER" if str(runner["name"]).upper().startswith("OVER") else "UNDER"
        runner["name"] = f"{side} {line}"
    return market


def _normalize_kalshi_game():
    payload = _kalshi_game_event()
    event = KalshiNormalizer().normalize_event(payload)
    markets = KalshiNormalizer().assemble_canonical_markets(
        event, payload["markets"], event_payload=payload
    )
    return event, markets[0]


def _normalize_kalshi_spread():
    event_payload, market_payload = _kalshi_spread_event_and_market()
    event = KalshiNormalizer().normalize_event(event_payload)
    markets = KalshiNormalizer().assemble_canonical_markets(
        event, [market_payload], event_payload=event_payload
    )
    return event, markets[0]


def _normalize_kalshi_total():
    event_payload, market_payload = _kalshi_total_event_and_market()
    event = KalshiNormalizer().normalize_event(event_payload)
    markets = KalshiNormalizer().assemble_canonical_markets(
        event, [market_payload], event_payload=event_payload
    )
    return event, markets[0]


def _pm_clob_tokens(prefix: str) -> list[str]:
    # Captured Gamma fixtures omit CLOB books. Tests that prove executable
    # PAPER identity inject real-looking token IDs rather than condition_id:0/1.
    return [
        f"101{prefix}000111222333444555666777888999000111222333",
        f"202{prefix}000111222333444555666777888999000111222333",
    ]


def _normalize_pm_family(sports_type: str, *, inject_clob_tokens: bool = True):
    payload = _pm_indkc()
    event = PolymarketNormalizer().normalize_event(payload)
    market_payload = next(item for item in payload["markets"] if item["sportsMarketType"] == sports_type)
    if inject_clob_tokens:
        market_payload["clobTokenIds"] = _pm_clob_tokens(sports_type[:3])
    market = PolymarketNormalizer().normalize_market(event, market_payload)
    return event, market, market_payload


def _normalize_mb_family(market_payload: dict):
    event_payload = _mb_indkc()
    event = MatchbookNormalizer().normalize_event(event_payload)
    market = MatchbookNormalizer().normalize_market(event, market_payload)
    return event, market


def test_thirty_two_franchises_and_city_ambiguity() -> None:
    assert len(NFL_ABBREVIATIONS) == 32
    assert resolve_nfl_team("KC").canonical == "kansas city chiefs"
    assert resolve_nfl_team("Colts").abbreviation == "IND"
    ny = resolve_nfl_team("New York")
    assert ny.ambiguous is True and ny.canonical is None
    la = resolve_nfl_team("Los Angeles")
    assert la.ambiguous is True and la.canonical is None
    assert resolve_nfl_team("NY").ambiguous is True
    assert resolve_nfl_team("LA").ambiguous is True
    assert resolve_nfl_team("Washington Redskins").rejected is True
    assert resolve_nfl_team("San Diego Chargers").rejected is True
    assert resolve_nfl_team("New York Giants").abbreviation == "NYG"
    assert resolve_nfl_team("NY Jets").abbreviation == "NYJ"


def test_nfl_aliases_do_not_leak_into_soccer() -> None:
    assert resolve_team_name("Chiefs") == "chiefs"
    assert football_alias_registry.resolve("Chiefs") == "chiefs"
    assert resolve_nfl_team("Arsenal").ok is False
    assert resolve_nfl_team("Newcastle United").ok is False
    from sports_hedge.application.hot_identity import scheduling_team_key

    assert scheduling_team_key("Saints") != "new orleans saints"
    assert scheduling_team_key("Chiefs") != "kansas city chiefs"


def test_indkc_same_fixture_across_three_providers() -> None:
    kalshi_event, _ = _normalize_kalshi_game()
    pm_event, _, _ = _normalize_pm_family("moneyline")
    mb_event, _ = _normalize_mb_family(_mb_market(_mb_indkc(), name="Moneyline"))
    assert kalshi_event.sport == pm_event.sport == mb_event.sport == NFL_SPORT
    assert kalshi_event.home_team == pm_event.home_team == mb_event.home_team == "kansas city chiefs"
    assert kalshi_event.away_team == pm_event.away_team == mb_event.away_team == "indianapolis colts"
    assert kalshi_event.kickoff_utc == pm_event.kickoff_utc == mb_event.kickoff_utc == KICKOFF
    assert kalshi_event.kickoff_utc != datetime(2026, 9, 21, 3, 20, tzinfo=UTC)
    matcher = EventMatcher()
    assert matcher.match(kalshi_event, pm_event).matched
    assert matcher.match(kalshi_event, mb_event).matched
    assert matcher.match(pm_event, mb_event).matched


def test_game_winner_attaches_and_native_ids_are_retained() -> None:
    _, kalshi = _normalize_kalshi_game()
    _, pm, pm_raw = _normalize_pm_family("moneyline")
    _, mb = _normalize_mb_family(_mb_market(_mb_indkc(), name="Moneyline"))
    for market in (kalshi, pm, mb):
        assert market.family is MarketFamily.GAME_WINNER
        assert market.period is FootballPeriod.FULL_TIME
        assert market.line is None
        assert {runner.outcome for runner in market.runners} == {
            CanonicalOutcome.HOME,
            CanonicalOutcome.AWAY,
        }
        assert CanonicalOutcome.DRAW not in {runner.outcome for runner in market.runners}
        assert canonical_key_for_market(market) == CANONICAL_NFL_GAME_WINNER
    assert kalshi.event.source_event_id == "KXNFLGAME-26SEP20INDKC"
    assert {runner.source_runner_id for runner in kalshi.runners} == {
        "KXNFLGAME-26SEP20INDKC-KC:YES",
        "KXNFLGAME-26SEP20INDKC-IND:YES",
    }
    assert pm.event.source_event_id == "827222"
    assert pm.source_market_id == "3482783"
    assert _pm_indkc()["gameId"] == 19484
    assert {runner.source_runner_id for runner in pm.runners} == set(_pm_clob_tokens("mon"))
    assert all(
        "0x0ad30faec3cd25a8ee81a919c4241ef9ec076882435a0b23205cd2b31bf32e70" not in runner.source_runner_id
        for runner in pm.runners
    )
    assert mb.event.source_event_id == "33306877354500023"
    assert mb.source_market_id == "33306877358600023"
    assert {runner.source_runner_id for runner in mb.runners} == {
        "33306877359300023",
        "33306877359000023",
    }


def test_kc_minus_6_5_canonicalises_across_venues() -> None:
    _, kalshi = _normalize_kalshi_spread()
    _, pm, pm_raw = _normalize_pm_family("spreads")
    _, mb = _normalize_mb_family(_clone_mb_spread(_mb_indkc(), home_line=Decimal("-6.5")))
    for market in (kalshi, pm, mb):
        assert market.family is MarketFamily.POINT_SPREAD
        assert market.line == Decimal("-6.5")
        assert canonical_key_for_market(market) == f"{CANONICAL_NFL_POINT_SPREAD}:-6.5"
        assert "must win by 7+" in nfl_operator_side_label(market, CanonicalOutcome.HOME)
        assert "may lose by up to 6, or win" in nfl_operator_side_label(market, CanonicalOutcome.AWAY)
    assert kalshi.source_market_id == "KXNFLSPREAD-26SEP20INDKC-KC7"
    assert pm.source_market_id == "3516972"
    assert str(pm_raw["conditionId"]).startswith("0x47ee")
    matcher = MarketMatcher()
    for left, right in ((kalshi, pm), (kalshi, mb), (pm, mb)):
        result = matcher.match(left, right)
        assert result.matched, result.reasons
        assert NFL_EXCEPTIONAL_SETTLEMENT_CAVEAT in result.reasons
        assert registered_canonical_key(left, right) == f"{CANONICAL_NFL_POINT_SPREAD}:-6.5"
        assert catalogue_allows_solver(left, right)
        assert catalogue_allows_live_execution(left, right) is False
        assessment = classify_pair(left, right)
        assert assessment.state is CatalogueApprovalState.PAPER_ASSUMED_EQUIVALENT
        assert assessment.settlement_assumption == "normal_full_game_completion"
        assert solver_model_for_pair(left, right) == "simple_complete_set"


def test_total_47_5_canonicalises_across_venues() -> None:
    _, kalshi = _normalize_kalshi_total()
    _, pm, _ = _normalize_pm_family("totals")
    _, mb = _normalize_mb_family(_clone_mb_total(_mb_indkc(), line=Decimal("47.5")))
    for market in (kalshi, pm, mb):
        assert market.family is MarketFamily.TOTAL_POINTS
        assert market.line == Decimal("47.5")
        assert canonical_key_for_market(market) == f"{CANONICAL_NFL_TOTAL_POINTS}:47.5"
        assert "48+ combined points" in nfl_operator_side_label(market, CanonicalOutcome.OVER)
        assert "47 or fewer combined points" in nfl_operator_side_label(market, CanonicalOutcome.UNDER)
    matcher = MarketMatcher()
    assert matcher.match(kalshi, pm).matched
    assert matcher.match(kalshi, mb).matched
    assert matcher.match(pm, mb).matched
    assert NFL_EXCEPTIONAL_SETTLEMENT_CAVEAT in matcher.match(kalshi, pm).reasons


def test_operator_plain_english_labels() -> None:
    assert "must win by 7+" in cover_explanation(team="Kansas City Chiefs", signed_line=Decimal("-6.5"))
    assert "may lose by up to 6, or win" in cover_explanation(
        team="Indianapolis Colts", signed_line=Decimal("6.5")
    )
    assert total_explanation(side=CanonicalOutcome.OVER, line=Decimal("47.5")) == (
        "Over 47.5 · 48+ combined points"
    )
    assert total_explanation(side=CanonicalOutcome.UNDER, line=Decimal("47.5")) == (
        "Under 47.5 · 47 or fewer combined points"
    )
    assert "normal completed NFL game" in NFL_SETTLEMENT_CAVEAT_OPERATOR_TEXT


def test_integer_and_mismatched_half_lines_rejected() -> None:
    mb_event = _mb_indkc()
    with pytest.raises(VenueNormalizationError, match="half-point"):
        _normalize_mb_family(_mb_market(mb_event, name="Handicap", handicap=5))
    with pytest.raises(VenueNormalizationError, match="half-point"):
        _normalize_mb_family(_mb_market(mb_event, name="Total", handicap=46))
    _, kalshi_spread = _normalize_kalshi_spread()
    _, mb_45 = _normalize_mb_family(_mb_market(mb_event, name="Handicap", handicap=4.5))
    result = MarketMatcher().match(kalshi_spread, mb_45)
    assert result.matched is False
    assert registered_canonical_key(kalshi_spread, mb_45) is None
    assert catalogue_allows_solver(kalshi_spread, mb_45) is False


def test_period_overtime_team_total_and_prop_rejects() -> None:
    with pytest.raises(VenueNormalizationError, match="period"):
        _normalize_mb_family(_mb_market(_mb_indkc(), name="1st Half Moneyline"))
    live = copy.deepcopy(_load("polymarket_event_cletb_live.json")["payload"])
    team_total = next(item for item in live["markets"] if item["sportsMarketType"] == "team_totals")
    cle_event = PolymarketNormalizer().normalize_event(live)
    with pytest.raises(VenueNormalizationError, match="unsupported Polymarket NFL market type"):
        PolymarketNormalizer().normalize_market(cle_event, team_total)
    milestone = _load("kalshi_milestone_indkc_kickoff.json")["payload"]
    for ticker in ("KXNFLTEAMTOTAL", "KXNFLOT", "KXNFLGAMEFG"):
        with pytest.raises(VenueNormalizationError, match="unsupported NFL Kalshi series"):
            KalshiNormalizer().normalize_event(
                {
                    "event_ticker": f"{ticker}-26SEP20INDKC",
                    "series_ticker": ticker,
                    "title": "IND vs KC",
                    "milestone": milestone,
                }
            )
    event = MatchbookNormalizer().normalize_event(_mb_indkc())
    with pytest.raises(VenueNormalizationError, match="props"):
        MatchbookNormalizer().normalize_market(
            event,
            {
                "id": "prop-1",
                "name": "First Touchdown Scorer",
                "market-type": "money_line",
                "runners": [
                    {"id": "1", "name": "Kansas City Chiefs"},
                    {"id": "2", "name": "Indianapolis Colts"},
                ],
            },
        )


def test_soccer_recognisers_do_not_classify_nfl_markets() -> None:
    kalshi_game = _kalshi_game_event()["markets"][0]
    with pytest.raises(VenueNormalizationError, match="soccer recogniser does not classify NFL markets"):
        _kalshi_market_family(
            kalshi_game, home_team="Kansas City Chiefs", away_team="Indianapolis Colts"
        )
    mb = _mb_indkc()
    with pytest.raises(VenueNormalizationError, match="soccer recogniser does not classify NFL markets"):
        _matchbook_market_family(
            "Moneyline",
            mb,
            home_team="Kansas City Chiefs",
            away_team="Indianapolis Colts",
        )
    pm_market = next(item for item in _pm_indkc()["markets"] if item["sportsMarketType"] == "moneyline")
    with pytest.raises(VenueNormalizationError, match="soccer recogniser does not classify NFL markets"):
        _polymarket_market_family(
            pm_market["question"],
            pm_market,
            home_team="Kansas City Chiefs",
            away_team="Indianapolis Colts",
        )


def test_generic_new_york_and_los_angeles_never_choose_a_franchise() -> None:
    with pytest.raises(VenueNormalizationError):
        MatchbookNormalizer().normalize_event(
            {
                "id": "ny-ambiguous",
                "name": "New York at Kansas City Chiefs",
                "start": "2026-09-21T00:20:00.000Z",
                "sport-id": 1,
                "meta-tags": [
                    {"name": "American Football", "type": "SPORT"},
                    {"name": "NFL", "type": "COMPETITION"},
                ],
            }
        )
    with pytest.raises(VenueNormalizationError):
        MatchbookNormalizer().normalize_event(
            {
                "id": "la-ambiguous",
                "name": "Indianapolis Colts at Los Angeles",
                "start": "2026-09-21T00:20:00.000Z",
                "sport-id": 1,
                "meta-tags": [
                    {"name": "American Football", "type": "SPORT"},
                    {"name": "NFL", "type": "COMPETITION"},
                ],
            }
        )
    nyg = MatchbookNormalizer().normalize_event(_load("matchbook_event_nyglar_upcoming.json")["payload"])
    assert nyg.away_team == "new york giants"
    assert nyg.home_team == "los angeles rams"


def test_automatic_settlement_fails_closed_on_tie_and_cancel() -> None:
    assert nfl_tied_score_blocker(20, 20) == NFL_SETTLEMENT_FAIL_CLOSED_REASON
    assert nfl_exceptional_status_blocker("cancelled") == NFL_SETTLEMENT_FAIL_CLOSED_REASON
    assert nfl_exceptional_status_blocker("fair price") == NFL_SETTLEMENT_FAIL_CLOSED_REASON
    trade = PaperTrade(
        trade_id="nfl-1",
        opportunity_id="opp-nfl",
        canonical_event_id="33306877354500023",
        competition="NFL",
        home_team="Kansas City Chiefs",
        away_team="Indianapolis Colts",
        market_family=MarketFamily.GAME_WINNER,
        period=FootballPeriod.FULL_TIME,
        state=PaperTradeState.OPEN,
        opened_at=NOW,
        last_updated_at=NOW,
        legs=[
            PaperTradeLeg(
                venue=VenueName.MATCHBOOK,
                outcome="home",
                currency="GBP",
                requested_stake=Decimal("1"),
                filled_stake=Decimal("1"),
                displayed_odds=Decimal("1.9"),
                filled_odds=Decimal("1.9"),
                source_market_id="33306877358600023",
                source_event_id="33306877354500023",
                source_runner_id="33306877359000023",
                fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
            ),
            PaperTradeLeg(
                venue=VenueName.KALSHI,
                outcome="away",
                currency="USD",
                requested_stake=Decimal("1"),
                filled_stake=Decimal("1"),
                displayed_odds=Decimal("2.1"),
                filled_odds=Decimal("2.1"),
                source_market_id="KXNFLGAME-26SEP20INDKC-IND",
                source_event_id="KXNFLGAME-26SEP20INDKC",
                source_contract_id="KXNFLGAME-26SEP20INDKC-IND",
                fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
            ),
        ],
    )
    tied = resolve_paper_trade_settlement(
        trade,
        matchbook_event={
            "id": "33306877354500023",
            "status": "graded",
            "home-score": 20,
            "away-score": 20,
        },
        matchbook_market={"id": "33306877358600023", "status": "graded"},
        kalshi_markets={
            "KXNFLGAME-26SEP20INDKC-IND": {
                "ticker": "KXNFLGAME-26SEP20INDKC-IND",
                "status": "finalized",
            }
        },
    )
    assert tied.winning_outcome is None
    assert tied.blocker == NFL_SETTLEMENT_FAIL_CLOSED_REASON
    cancelled = resolve_paper_trade_settlement(
        trade,
        matchbook_event={
            "id": "33306877354500023",
            "status": "cancelled",
            "home-score": 24,
            "away-score": 17,
        },
        matchbook_market={"id": "33306877358600023", "status": "cancelled"},
    )
    assert cancelled.winning_outcome is None
    assert cancelled.blocker == NFL_SETTLEMENT_FAIL_CLOSED_REASON


def test_missing_and_synthetic_polymarket_tokens_cannot_become_executable_paper() -> None:
    with pytest.raises(VenueNormalizationError, match="exact CLOB token"):
        _normalize_pm_family("moneyline", inject_clob_tokens=False)
    payload = _pm_indkc()
    event = PolymarketNormalizer().normalize_event(payload)
    market_payload = next(item for item in payload["markets"] if item["sportsMarketType"] == "moneyline")
    condition = str(market_payload["conditionId"])
    market_payload["clobTokenIds"] = [f"{condition}:0", f"{condition}:1"]
    with pytest.raises(VenueNormalizationError, match="not executable"):
        PolymarketNormalizer().normalize_market(event, market_payload)


def test_graded_final_payload_alone_cannot_auto_settle_nfl() -> None:
    trade = PaperTrade(
        trade_id="nfl-2",
        opportunity_id="opp-nfl-2",
        canonical_event_id="33306877354500023",
        competition="NFL",
        home_team="Kansas City Chiefs",
        away_team="Indianapolis Colts",
        market_family=MarketFamily.GAME_WINNER,
        period=FootballPeriod.FULL_TIME,
        state=PaperTradeState.OPEN,
        opened_at=NOW,
        last_updated_at=NOW,
        legs=[
            PaperTradeLeg(
                venue=VenueName.MATCHBOOK,
                outcome="home",
                currency="GBP",
                requested_stake=Decimal("1"),
                filled_stake=Decimal("1"),
                displayed_odds=Decimal("1.9"),
                filled_odds=Decimal("1.9"),
                source_market_id="33306877358600023",
                source_event_id="33306877354500023",
                source_runner_id="33306877359000023",
                fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
            ),
            PaperTradeLeg(
                venue=VenueName.KALSHI,
                outcome="away",
                currency="USD",
                requested_stake=Decimal("1"),
                filled_stake=Decimal("1"),
                displayed_odds=Decimal("2.1"),
                filled_odds=Decimal("2.1"),
                source_market_id="KXNFLGAME-26SEP20INDKC-IND",
                source_event_id="KXNFLGAME-26SEP20INDKC",
                source_contract_id="KXNFLGAME-26SEP20INDKC-IND",
                fill_kind=PaperLegFillKind.INTERNAL_SIMULATED,
            ),
        ],
    )
    graded = {
        "id": "33306877354500023",
        "status": "graded",
        "home-score": 24,
        "away-score": 17,
    }
    blocked = resolve_paper_trade_settlement(
        trade,
        matchbook_event=graded,
        matchbook_market={"id": "33306877358600023", "status": "graded"},
        kalshi_markets={
            "KXNFLGAME-26SEP20INDKC-IND": {
                "ticker": "KXNFLGAME-26SEP20INDKC-IND",
                "status": "finalized",
                "result": "no",
            }
        },
    )
    assert blocked.winning_outcome is None
    assert blocked.blocker == NFL_NORMAL_COMPLETION_NOT_PROVEN

    postponed = nfl_lifecycle_observation(["postponed"], observed_at=NOW)
    trade.audit.append(
        PaperTradeAuditEvent(
            occurred_at=NOW,
            event_type=PaperTradeAuditEventType.NFL_LIFECYCLE_OBSERVED,
            detail=nfl_lifecycle_audit_detail(postponed),
        )
    )
    later = resolve_paper_trade_settlement(
        trade,
        matchbook_event=graded,
        matchbook_market={"id": "33306877358600023", "status": "graded"},
        kalshi_markets={
            "KXNFLGAME-26SEP20INDKC-IND": {
                "ticker": "KXNFLGAME-26SEP20INDKC-IND",
                "status": "finalized",
                "result": "no",
            }
        },
    )
    assert later.winning_outcome is None
    assert later.blocker == NFL_SETTLEMENT_FAIL_CLOSED_REASON

    proven = trade.model_copy(update={"audit": []})
    in_play = nfl_lifecycle_observation(["in_play"], observed_at=NOW)
    proven.audit.append(
        PaperTradeAuditEvent(
            occurred_at=NOW,
            event_type=PaperTradeAuditEventType.NFL_LIFECYCLE_OBSERVED,
            detail=nfl_lifecycle_audit_detail(in_play),
        )
    )
    ready = resolve_paper_trade_settlement(
        proven,
        matchbook_event=graded,
        matchbook_market={"id": "33306877358600023", "status": "graded"},
        kalshi_markets={
            "KXNFLGAME-26SEP20INDKC-IND": {
                "ticker": "KXNFLGAME-26SEP20INDKC-IND",
                "status": "finalized",
                "result": "no",
            }
        },
    )
    assert ready.blocker is None
    assert ready.winning_outcome == "home"


def test_event_matcher_threshold_stays_constructor_injected() -> None:
    assert EventMatcher().threshold == 0.92
    assert EventMatcher(threshold=0.80).threshold == 0.80


def test_matchbook_american_football_sport_id_is_not_soccer() -> None:
    sports = [
        {"id": 15, "name": "Football"},
        json.loads((FIXTURES / "matchbook_lookups_sports_american_football.json").read_text())["payload"],
    ]
    assert select_football_sport_id(sports) == 15
    assert select_american_football_sport_id(sports) == 1
    with pytest.raises(MatchbookDiscoveryError, match="American Football"):
        select_american_football_sport_id([{"id": 15, "name": "Football"}])


def test_nfl_is_selectable_not_default_and_kxnflgamefg_is_not_game() -> None:
    assert "nfl" not in default_operator_competition_code_values()
    assert selected_includes_nfl(["nfl"]) is True
    assert selected_includes_nfl(None) is False
    assert resolve_target_competition_from_kalshi_ticker("KXNFLGAME") is not None
    assert resolve_target_competition_from_kalshi_ticker("KXNFLGAMEFG") is None
    assert is_nfl_payload(_pm_indkc()) is True
    assert is_nfl_payload(_mb_indkc()) is True


def test_kalshi_spread_conversion_is_covering_minus_line() -> None:
    event, market = _normalize_kalshi_spread()
    covering = next(runner for runner in market.runners if runner.source_runner_id.endswith(":YES"))
    assert covering.outcome is CanonicalOutcome.HOME
    assert market.line == Decimal("-6.5")
    assert event.source_event_id == "KXNFLSPREAD-26SEP20INDKC"
